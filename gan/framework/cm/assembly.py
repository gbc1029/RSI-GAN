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

    P1 scope: ``mode`` selects a shipped baseline preset. The P2 source
    level extends this resolution to a NAMED agent-owned assembly module
    (the ``cm_assembly`` design key, AST-gated + budgeted + wedge-broken,
    fail-open to the preset) — this signature is that seam, and the
    baseline presets remain the fail-open target."""
    fn = BASELINE_ASSEMBLY.get(mode)
    if fn is None:
        return BASELINE_ASSEMBLY["slice"], "slice"
    return fn, None
