"""Fail-open policy runner (docs/10 §5.2 分三 失败/退出语义, 冻结).

The custom policy is AGENT CODE executed by the framework EVERY TURN — the
highest-risk auto-execution surface in the framework. Three layers bound it:

1. **AST negative list** (``policy_violations``): no LLM, no network, no
   subprocess, no dynamic exec, no filesystem writes. Checked at SNAPSHOT
   time (task_execution materialization refuses the file) and again at load.
2. **Execution budget** (``cm_policy_exec_ms``): the load and each apply()
   run in a worker thread; an overrun is a failure (recorded, counted).
3. **One-strike latch** (docs/19 §12.17, aligned with the assembly): the
   module is load-once per session, so byte-identical retries are
   deterministic waste — ONE failure (load crash, load timeout, run crash,
   contract violation) disables the policy for the session
   (``cm_policy_disabled``) and the session continues on the fixed
   pre-segment. The cross-session fingerprint latch owns "a fixed module
   deserves one fresh try".

Fail-open: ANY failure leaves the session alive on the fixed pre-segment.
"""
from __future__ import annotations

import ast
import concurrent.futures
import importlib.util
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    (fail-closed): banned modules/calls (no LLM, no network, no subprocess,
    no dynamic exec) AND the dual-posture import rule (stdlib + guarded
    ``cm`` imports only — the SAME gate ``load_assembly`` runs; without it a
    policy module could import any other ``gan.*`` in-process, a capability
    docs10 never declared: L2-06's guarded-only-cm clause, docs/19 §12.27).
    The ``METHODS`` harvest is ADDITIVE
    — its problems are returned, never raised.

    The sys.modules registration key is UNIQUE PER PATH (hash suffix): the
    in-process p/e seats run in PARALLEL threads and may load different
    policy modules concurrently — a fixed name would let them clobber each
    other's registration. Same path replaces its own entry (no unbounded
    growth: the per-turn reload reuses one key)."""
    import hashlib
    from .assembly import _gan_import_problems
    problems = policy_violations(path) + _gan_import_problems(path)
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


def timed_load_policy(path: str, exec_ms: int
                      ) -> Tuple[Callable[..., Any], Dict[str, Any], List[str]]:
    """Load the policy module ONCE under the same budget as its execution
    (load-once, docs/19 §12.17 — the policy no longer reloads per turn: the
    module is agent code, byte-identical retries are deterministic waste,
    and a mid-session edit must not change the seam). Returns
    ``(apply_fn, methods, method_problems)``. Raises :class:`PolicyTimeout`
    on budget overrun; load crashes (import errors, a missing ``apply``)
    propagate — the SESSION classifies and strikes on them, uniformly with
    the assembly side. The worker thread leaks exactly like a timed-out
    apply (Python cannot kill threads; CM-03 owns that) and is named
    ``cm_exec`` so the leak observability can tell it apart from framework
    pools."""
    import time as _time
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="cm_exec")

    def _load_and_extract() -> Tuple[Callable[..., Any], Dict[str, Any], List[str]]:
        module, methods, method_problems = load_policy(path)
        apply_fn = getattr(module, "apply", None)
        if not callable(apply_fn):
            raise PolicyMissing("policy module exposes no apply(ctx)")
        return apply_fn, methods, method_problems

    future = pool.submit(_load_and_extract)
    try:
        return future.result(timeout=max(exec_ms, 1) / 1000.0)
    except concurrent.futures.TimeoutError as e:
        future.cancel()
        raise PolicyTimeout(f"policy load exceeded {exec_ms}ms") from e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def apply_policy(apply_fn: Callable[..., Any], handle: Any, exec_ms: int
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """Execute ONE apply() under the per-turn budget (the module is already
    loaded — load-once caches the MODULE, never the run result). Returns
    ``(records, over_ms)``; raises :class:`PolicyTimeout` on overrun (the
    worker thread leaks exactly like a timed-out load)."""
    import time as _time
    t0 = _time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="cm_exec")
    future = pool.submit(apply_fn, handle)
    try:
        records = future.result(timeout=max(exec_ms, 1) / 1000.0)
    except concurrent.futures.TimeoutError as e:
        future.cancel()
        # Same rule as the assembly runner: the budget bounds THIS TURN.
        # ``with`` would block on ``shutdown(wait=True)`` for the hung policy
        # code, so the timeout could not end the turn.
        raise PolicyTimeout(f"policy exceeded {exec_ms}ms") from e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    over_ms = max(0, int((_time.monotonic() - t0) * 1000) - exec_ms)
    if records is None:
        records = []
    return records, over_ms


class PolicyMissing(Exception):
    pass


class PolicyTimeout(Exception):
    pass
