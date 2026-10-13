"""Baseline CM assembly — the SOURCE-level pipeline layer (docs/10 §11.7).

The per-turn seam is no longer welded into ``CMSession``: each ``cm_mode``
preset is a NAMED COMPOSITION over the session's frozen primitive methods,
written out as readable pipeline source. This module is the baseline
assembly shipped with the core; it is the reference implementation of the
management path the user asked for:

    view = context_manager(state)   ==   assemble(session, ...)(state)

through "原语/自定义方法 经排列组合、条件选择组建". The P2 source level (the
``cm_assembly`` design key — governance review pending) resolves an
agent-owned assembly through exactly this module's contract:

    assemble(session, msgs, outgoing, tokens_used, budget)
        -> (history_to_send, msg_to_send)

so config level (``cm_*`` keys -> session parameters + preset selection)
and source level (this file, later an editable component) are two entry
points into the SAME mechanism.

Invariants do NOT live here — every mutation goes through a session
primitive that validates before it mutates (拒绝即不动), and the post
segment (``finalize_turn``) owns the audit brief, the no-fabrication
render and the outgoing contract (the active instruction is never
substituted except the dedup prefix-elision). An assembly can therefore
reorder or extend stages safely; it cannot bypass the rules.

STDLIB-ONLY (snapshot import contract): this file ships in the ``cm``
snapshot (``_CM_CORE_FILES``) and must import as ``cm.assembly`` in the
task sandbox with no ``gan.*`` dependency. Sessions arrive as duck-typed
objects (the frozen primitive surface is documented on ``CMSession``).
"""
from __future__ import annotations

import ast
import concurrent.futures
import importlib.util
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

# An assembly function's frozen contract (positional, per CMSession.assemble_turn):
#   session  — the live CMSession (primitive surface + clamped parameters)
#   msgs     — the canonical history (raw dicts; never mutated — 双记)
#   outgoing — the raw user text for this call
#   tokens_used / budget — the chat loop's live usage + armed budget
AssembleFn = Callable[..., Tuple[List[Dict[str, Any]], str]]


def assemble_passthrough(session, msgs, outgoing,
                         tokens_used: int = 0,
                         budget: Optional[int] = None
                         ) -> Tuple[List[Dict[str, Any]], str]:
    """``passthrough`` preset — the identity view (pre-D behaviour).

    Register everything for the audit face, send everything, substitute
    nothing. The sent history is per-ENTRY copies: the LLM client's
    text->content conversion POPS keys from the dicts it is handed
    (agent/llm.py), so the canonical history must stay raw (双记)."""
    session.register_history(msgs)
    session.register_outgoing(outgoing)
    session.emit_view(0)
    return [dict(m) if isinstance(m, dict) else m for m in msgs], outgoing


def assemble_slice(session, msgs, outgoing,
                   tokens_used: int = 0,
                   budget: Optional[int] = None
                   ) -> Tuple[List[Dict[str, Any]], str]:
    """``slice`` preset — window governance.

    Keep the newest ``window_k`` assistant turns FULL (plus pinned blocks
    and notes; pin protection lives in the primitives), older turn/tool
    blocks become drop-floor-guarded placeholders, then elide the repeated
    per-turn system header off old turns (dedup)."""
    session.register_history(msgs)
    out_block = session.register_outgoing(outgoing)
    records: List[Dict[str, Any]] = []
    bc = session.budget_checkpoint(tokens_used, budget)
    if bc is not None:
        records.append(bc)
    records += session.window_defaults()
    if session.system_prefix:
        records += session.dedup_prefix(msgs)
    return session.finalize_turn(msgs, outgoing, out_block, records)


def assemble_compact_conservative(session, msgs, outgoing,
                                  tokens_used: int = 0,
                                  budget: Optional[int] = None
                                  ) -> Tuple[List[Dict[str, Any]], str]:
    """``compact_conservative`` preset — excerpt tier before the window.

    History turns older than the pin window whose mechanical excerpt
    actually saves >= MIN_COMPACT_SAVE chars become EXCERPTS (head+tail
    kept) instead of the slice preset's whole-turn placeholder. The excerpt
    pass runs BEFORE the window pass because the window skips
    already-substituted blocks (sticky forms): an excerpted turn is not
    placeholder-dropped on top of its excerpt (docs/10 §11.5 erratum — the
    compaction target is the old USER turn, which carries the embedded
    tool outputs in this chat protocol)."""
    session.register_history(msgs)
    out_block = session.register_outgoing(outgoing)
    records: List[Dict[str, Any]] = []
    bc = session.budget_checkpoint(tokens_used, budget)
    if bc is not None:
        records.append(bc)
    records += session.conservative_compaction()
    records += session.window_defaults()
    if session.system_prefix:
        records += session.dedup_prefix(msgs)
    return session.finalize_turn(msgs, outgoing, out_block, records)


def assemble_custom(session, msgs, outgoing,
                    tokens_used: int = 0,
                    budget: Optional[int] = None
                    ) -> Tuple[List[Dict[str, Any]], str]:
    """``custom`` preset — the baseline window pipeline plus the agent
    policy hook at the tail (fail-open runner, wedge breaker; the policy
    face's ops records ride the same audit path as stage ops)."""
    session.register_history(msgs)
    out_block = session.register_outgoing(outgoing)
    records: List[Dict[str, Any]] = []
    bc = session.budget_checkpoint(tokens_used, budget)
    if bc is not None:
        records.append(bc)
    records += session.window_defaults()
    if session.system_prefix:
        records += session.dedup_prefix(msgs)
    records += session.run_policy()
    return session.finalize_turn(msgs, outgoing, out_block, records)


# The shipped baseline presets (docs/10 §5.2 分一: the fixed pipeline order
# becomes the slice/compact_conservative/custom compositions; passthrough
# stays the identity). Frozen in v1 — editing a preset is a framework batch.
BASELINE_ASSEMBLY: Dict[str, AssembleFn] = {
    "passthrough": assemble_passthrough,
    "slice": assemble_slice,
    "compact_conservative": assemble_compact_conservative,
    "custom": assemble_custom,
}


def resolve(mode: str) -> Tuple[AssembleFn, Optional[str]]:
    """The unified assembly loader. Returns ``(assemble_fn, fallback_to)``.

    ``fallback_to`` is None for a known preset; an UNKNOWN mode fails open
    to the ``slice`` preset (the generic baseline pipeline) with a loud
    ``cm_assembly_fallback`` event emitted by the caller — the clamp
    authority makes unknown modes unreachable in production, and a direct
    construction must never degrade silently (no-silent-failure rule).

    P1 scope: ``mode`` selects a shipped baseline preset. P2 (this file's
    second half): ``load_assembly`` resolves a NAMED agent-owned assembly
    module through the same contract, budgeted and wedge-broken by the
    session — the baseline presets remain the fail-open target."""
    fn = BASELINE_ASSEMBLY.get(mode)
    if fn is None:
        return BASELINE_ASSEMBLY["slice"], "slice"
    return fn, None


# ---------------------------------------------------------------------------
# P2: the named agent-owned assembly (docs/10 §11.8)
# ---------------------------------------------------------------------------

class AssemblyError(Exception):
    """An agent-owned assembly violated its load-time contract (AST gate,
    dual-posture import rule, missing ``assemble``). Fail-open, loudly."""


class AssemblyTimeout(Exception):
    """The assembly exceeded its per-turn execution budget."""


# An assembly must be importable in BOTH postures (snapshot top-level ``cm``
# and in-process ``gan.framework.cm``), so its top-level imports are
# stdlib-only — plus the guarded dual-idiom import of the core itself. A
# ``gan.*`` import would work in-process and break the snapshot: banned.
def _gan_import_problems(path: str) -> List[str]:
    problems: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as e:
        return [f"assembly file unreadable/unparsable: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "gan":
                    problems.append(f"banned import: {alias.name} "
                                    "(dual-posture: stdlib + cm only)")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "gan" and node.level == 0:
                problems.append(f"banned import: from {node.module} "
                                "(dual-posture: stdlib + cm only)")
    return problems


def load_assembly(path: str) -> AssembleFn:
    """Import a named assembly module from an explicit file path (snapshot
    or component tree) and return its ``assemble`` function.

    Fail-closed at load: the policy AST gate runs first (no LLM, no network,
    no subprocess, no dynamic exec, no filesystem writes), then the
    dual-posture import rule (stdlib + guarded ``cm`` imports only), then
    the contract check (a callable ``assemble``). Any refusal raises
    :class:`AssemblyError` — the session turns that into ONE loud
    ``cm_assembly_fallback`` and runs the baseline preset for the session."""
    from .failopen import policy_violations
    problems = policy_violations(path) + _gan_import_problems(path)
    if problems:
        raise AssemblyError("assembly refused at load: " + "; ".join(problems)[:200])
    # sys.modules key UNIQUE PER PATH: parallel in-process sessions (p/e
    # self-improve threads) loading DIFFERENT assemblies must not clobber
    # each other's registration; same path replaces its own entry (the
    # session loads once, so growth is bounded by distinct modules anyway).
    import hashlib
    name = ("cm_assembly_user_"
            + hashlib.sha1(os.path.abspath(path).encode("utf-8", "replace")
                           ).hexdigest()[:10])
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssemblyError(f"cannot load assembly module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    fn = getattr(module, "assemble", None)
    if not callable(fn):
        raise AssemblyError(
            "assembly exposes no callable assemble(session, msgs, outgoing, "
            "tokens_used, budget)")
    return fn


def run_assembly(fn: AssembleFn, session: Any, msgs: List[Dict[str, Any]],
                 outgoing: str, tokens_used: int, budget: Optional[int],
                 exec_ms: int) -> Tuple[List[Dict[str, Any]], str]:
    """Execute one agent assembly under its per-turn execution budget
    (worker thread; same class as the policy runner). Emits
    ``cm_assembly_overrun`` when the wall exceeded the budget (the result is
    still honored if it arrived — the budget is advisory for terminating
    code, binding only through the timeout). Raises on crash/timeout: the
    session's wedge breaker counts the failure and re-runs the preset."""
    import time as _time
    t0 = _time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, session, msgs, outgoing, tokens_used, budget)
        try:
            result = future.result(timeout=max(exec_ms, 1) / 1000.0)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise AssemblyTimeout(f"assembly exceeded {exec_ms}ms") from e
    over_ms = max(0, int((_time.monotonic() - t0) * 1000) - exec_ms)
    if over_ms:
        session._emit({"type": "cm_assembly_overrun", "turn": session.turn,
                       "over_ms": over_ms})
    return result
