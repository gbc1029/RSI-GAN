"""Fail-open policy runner (docs/10 §5.2 分三 失败/退出语义, 冻结).

The custom policy is AGENT CODE executed by the framework EVERY TURN — the
highest-risk auto-execution surface in the framework. Three layers bound it:

1. **AST negative list** (``policy_violations``): no LLM, no network, no
   subprocess, no dynamic exec, no filesystem writes. Checked at SNAPSHOT
   time (task_execution materialization refuses the file) and again at load.
2. **Execution budget** (``cm_policy_exec_ms``): the apply() call runs in a
   worker thread; an overrun is a failure (recorded, counted).
3. **Wedge breaker** (C1 same-class semantics): 2 consecutive failures
   (exception, timeout, non-list return) disable the policy for the session
   (``cm_policy_disabled``) — the session continues on the fixed pre-segment.

Fail-open: ANY failure leaves the session alive on the fixed pre-segment.
"""
from __future__ import annotations

import ast
import concurrent.futures
import importlib.util
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Imports a policy module must NEVER make (纯代码: 无 LLM、无网络、无子进程).
_BANNED_MODULES = (
    "agent.llm", "agent.llm_withtools", "litellm", "openai", "anthropic",
    "socket", "ssl", "urllib", "urllib.request", "http", "http.client",
    "requests", "subprocess", "multiprocessing", "ctypes", "asyncio.subprocess",
)
_BANNED_CALLS = ("eval", "exec", "compile", "__import__", "open", "system",
                 "popen", "spawn")


def policy_violations(path: str) -> List[str]:
    """AST static check for one policy module (no import happens). Returns a
    list of human-readable violations (empty = clean)."""
    problems: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as e:
        return [f"policy file unreadable/unparsable: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name in _BANNED_MODULES or root in _BANNED_MODULES:
                    problems.append(f"banned import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            if mod in _BANNED_MODULES or root in _BANNED_MODULES:
                problems.append(f"banned import: from {mod}")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in _BANNED_CALLS:
                problems.append(f"banned call: {name}()")
    return problems


def load_policy(path: str):
    """Import the policy module from an explicit file path (snapshot).
    Returns ``(module, methods, method_problems)``. The AST gate runs first
    (fail-closed); the ``METHODS`` harvest (P3, docs/10 §11.10) is ADDITIVE
    — its problems are returned, never raised.

    The sys.modules registration key is UNIQUE PER PATH (hash suffix): the
    in-process p/e seats run in PARALLEL threads and may load different
    policy modules concurrently — a fixed name would let them clobber each
    other's registration. Same path replaces its own entry (no unbounded
    growth: the per-turn reload reuses one key)."""
    import hashlib
    problems = policy_violations(path)
    if problems:
        raise ImportError("policy refused by AST gate: " + "; ".join(problems))
    name = ("cm_policy_user_"
            + hashlib.sha1(os.path.abspath(path).encode("utf-8", "replace")
                           ).hexdigest()[:10])
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load policy module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    from .assembly import harvest_methods
    methods, method_problems = harvest_methods(module)
    return module, methods, method_problems


def run_policy(path: Optional[str], handle: Any, exec_ms: int
               ) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any], List[str]]:
    """Execute the policy's apply() under the execution budget.

    Returns ``(records, over_ms, methods, method_problems)`` — the P3
    ``METHODS`` harvest rides the successful load (the caller merges it
    once per session). Raises on crash/timeout (the session's wedge breaker
    counts the failure and fails open)."""
    if not path:
        raise PolicyMissing("no policy path configured")
    module, methods, method_problems = load_policy(path)
    apply_fn = getattr(module, "apply", None)
    if not callable(apply_fn):
        raise PolicyMissing("policy module exposes no apply(ctx)")
    import time as _time
    t0 = _time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(apply_fn, handle)
        try:
            records = future.result(timeout=max(exec_ms, 1) / 1000.0)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise PolicyTimeout(f"policy exceeded {exec_ms}ms") from e
    over_ms = max(0, int((_time.monotonic() - t0) * 1000) - exec_ms)
    if records is None:
        records = []
    return records, over_ms, methods, method_problems


class PolicyMissing(Exception):
    pass


class PolicyTimeout(Exception):
    pass
