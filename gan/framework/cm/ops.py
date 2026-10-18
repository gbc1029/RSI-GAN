"""CM primitives — the frozen vocabulary (docs/10 §5.2 分二).

Every primitive VALIDATES FIRST and mutates the session's view ONLY on
success (拒绝即不动 — a policy failure must never leave a half-applied op).
The seam never re-checks: these functions ARE the enforcement for their own
op, and the post-segment only counts + emits.

FACES (双形态暴露): the agent face exposes index/fetch/budget_query/pin/
unpin/note/view_state (``ctx_view`` is the agent's 知情权 tool, docs/10
§5.2.3); the policy face may additionally drop/compact/substitute/order/peek.
``call`` is on BOTH faces: the entry to the
session's registered custom methods — it runs at the CALLER's privilege
(the method receives the caller's own handle, so no capability is added),
only the same-level extension surface.
Agent-side ``drop`` is deliberately NOT a
tool — eviction can only happen at the view layer (an agent "drop" call's
output would itself enter the history).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .blocks import (Block, Universe, View, cut, excerpt_saving, placeholder,
                       PLACEHOLDER_OVERHEAD, MIN_COMPACT_SAVE,
                       DERIVED_PIECE_KEEP, estimate_tokens)

AGENT_FACES = ("index", "fetch", "budget_query", "pin", "unpin", "note",
               "view_state", "call")
POLICY_FACES = ("index", "fetch", "peek", "pin", "unpin", "drop", "compact",
                "substitute", "order", "budget_query", "view_state", "note",
                "call")

_SHRINK_OPS = ("drop", "compact", "substitute")


def _faces_for(face: str) -> Tuple[str, ...]:
    return AGENT_FACES if face == "agent" else POLICY_FACES


def face_allows(face: str, op: str) -> bool:
    return op in _faces_for(face)


def allowed_ops(face: str) -> Dict[str, Any]:
    return {"op": "ops", "ok": True, "face": face, "ops": list(_faces_for(face))}


# ---------------------------------------------------------------------------
# view-mutating primitives (validate first, mutate only on success)
# ---------------------------------------------------------------------------

def drop(universe: Universe, view: View, block_id: str, rule: str,
         floor_chars: int) -> Dict[str, Any]:
    block = universe.get(block_id)
    if block is None:
        return {"op": "drop", "refs": [block_id], "ok": False, "error": "unknown block"}
    if block.pinned:
        return {"op": "drop", "refs": [block_id], "ok": False, "error": "pinned block"}
    if block.kind == "note":
        return {"op": "drop", "refs": [block_id], "ok": False,
                "error": "note blocks are never dropped"}
    if block.chars < floor_chars:
        return {"op": "drop", "refs": [block_id], "ok": False,
                "error": f"below drop floor ({floor_chars})"}
    view.set(block_id, "placeholder", rule or "drop")
    return {"op": "drop", "refs": [block_id], "ok": True,
            "rule": rule or "drop",
            "saved": max(0, block.chars - len(placeholder(block, rule)))}


def substitute(universe: Universe, view: View, block_id: str, form: str,
               rule: str, floor_chars: int) -> Dict[str, Any]:
    block = universe.get(block_id)
    if block is None:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": "unknown block"}
    if form not in ("full", "head_tail", "excerpt", "placeholder", "template"):
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": f"invalid form {form!r}"}
    if form == "placeholder" and block.pinned:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": "pinned block"}
    if form in ("placeholder", "head_tail", "excerpt", "template") \
            and block.chars < floor_chars:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": f"below drop floor ({floor_chars})"}
    if form == "full":
        view.set(block_id, "full", rule or "restore")
        return {"op": "substitute", "refs": [block_id], "ok": True,
                "rule": rule or "restore", "saved": 0}
    saved = max(0, block.chars - len(cut(block.text, 600))) \
        if form in ("excerpt", "head_tail") else 0
    view.set(block_id, form, rule or "substitute")
    return {"op": "substitute", "refs": [block_id], "ok": True,
            "rule": rule or "substitute", "saved": saved, "form": form}


def compact(universe: Universe, view: View, block_ids: List[str],
            strategy: str, floor_chars: int, turn: int) -> Dict[str, Any]:
    """Mechanical compaction (NO LLM). ``excerpt``/``first_last`` stay
    form-level (single-block strategies — the sticky excerpt achieves them).
    ``merge``/``template`` materialize a DERIVED block whose text is built
    HERE from the sources only — register_derived is not on any face, so
    no-fabrication holds by construction (docs/10 §15).

    Host = the first source bound to the canonical history (``msg_index``):
    its span renders the derived text IN FULL (the merge already did the
    compressing); every other canonical source becomes a ``merged-into``
    placeholder; stale references to consumed derived sources are re-pointed
    the same way (one view representation per piece of content). A derived
    source is PINNED on consumption — consumed nodes never re-compact, the
    lineage stays a tree, not a DAG. Validate first, mutate only on success."""
    if strategy not in ("excerpt", "first_last", "merge", "template"):
        return {"op": "compact", "refs": list(block_ids), "ok": False,
                "error": f"unknown strategy {strategy!r}"}
    if not block_ids:
        return {"op": "compact", "refs": [], "ok": False, "error": "no refs"}
    if len(set(block_ids)) != len(block_ids):
        return {"op": "compact", "refs": list(block_ids), "ok": False,
                "error": "duplicate refs"}
    saved, blocks = 0, []
    for bid in block_ids:
        block = universe.get(bid)
        if block is None:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "unknown block"}
        if block.pinned:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "pinned block"}
        if block.kind == "note":
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "note blocks are never dropped"}
        if view.form_of(block.block_id) != "full":
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": (f"block {block.block_id} already substituted "
                              f"({view.form_of(block.block_id)})")}
        if block.chars < floor_chars:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": f"below drop floor ({floor_chars})"}
        blocks.append(block)
    if strategy in ("excerpt", "first_last"):
        for block in blocks:
            saving = excerpt_saving(block)
            if saving < MIN_COMPACT_SAVE:
                return {"op": "compact", "refs": list(block_ids), "ok": False,
                        "error": ("compaction saves nothing "
                                  f"({block.block_id} saves {saving})")}
            saved += saving
        for block in blocks:
            view.set(block.block_id, "excerpt", f"compacted:{strategy}")
        return {"op": "compact", "refs": list(block_ids), "ok": True,
                "rule": f"compacted:{strategy}", "saved": saved}

    # -- materializing strategies: build the derived text from the sources --
    if strategy == "merge":
        if len(blocks) < 2:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "merge needs >= 2 blocks"}
        host = next((b for b in blocks if b.msg_index is not None), None)
        if host is None:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "no hostable source (none bound to history)"}
        derived_text = "\n".join(f"[{b.block_id}] {cut(b.text, DERIVED_PIECE_KEEP)}"
                                 for b in blocks)
    else:  # template — frozen metadata + key lines, one entry per source
        host = next((b for b in blocks if b.msg_index is not None), None)
        if host is None:
            return {"op": "compact", "refs": list(block_ids), "ok": False,
                    "error": "no hostable source (none bound to history)"}
        lines = [f"[derived template: "
                 f"sources={','.join(b.block_id for b in blocks)}]"]
        for b in blocks:
            seg_lines = b.text.splitlines() or [""]
            lines.append(f"- {b.block_id} kind={b.kind} chars={b.chars} "
                         f"token_est={estimate_tokens(b.text)} "
                         f"head={seg_lines[0][:120]!r} "
                         f"tail={seg_lines[-1][:120]!r}")
        derived_text = "\n".join(lines)
    saved = sum(b.chars for b in blocks) - len(derived_text)
    if saved < MIN_COMPACT_SAVE:
        return {"op": "compact", "refs": list(block_ids), "ok": False,
                "error": "compaction saves nothing"}

    # all gates passed — materialize and rewrite the view
    dblock = universe.register_derived("compact", derived_text, turn,
                                       [b.block_id for b in blocks])
    consumed = {b.block_id for b in blocks if b.kind == "derived"}
    for b in blocks:
        if b.kind == "derived":
            b.pinned = True   # consumed node: never re-compact (tree, not DAG)
    view.set(host.block_id, "derived_host", f"derived={dblock.block_id}")
    for b in blocks:
        if b.block_id != host.block_id and b.msg_index is not None:
            view.set(b.block_id, "placeholder",
                     f"merged-into:{dblock.block_id}")
    if consumed:
        # former hosts / pointers of a consumed derived re-point to the
        # newest representation (one view representation per content)
        for bid, rule in list(view.rules.items()):
            if bid == host.block_id or not isinstance(rule, str):
                continue
            target = None
            if rule.startswith("derived="):
                target = rule[len("derived="):]
            elif rule.startswith("merged-into:"):
                target = rule[len("merged-into:"):]
            if target in consumed:
                view.set(bid, "placeholder",
                         f"merged-into:{dblock.block_id}")
    return {"op": "compact", "refs": list(block_ids), "ok": True,
            "rule": f"compacted:{strategy}", "saved": saved,
            "derived": dblock.block_id}


def order(universe: Universe, view: View, block_ids: List[str]) -> Dict[str, Any]:
    """Validating no-op (evidence-verified, docs/10 §15): the view order IS
    the canonical history order — one block per message entry over an
    append-only history (register_history binds msg_index; notes and derived
    blocks never inject into it), so a semantics-bearing reorder has no safe
    target in this chat protocol (message structure is the seam's
    (history, msg) contract; tool_result adjacency must not break).
    ``order`` still validates refs so policies written against the full
    vocabulary behave identically. Revisit only if the block model gains
    multi-block messages, a structured tool-message protocol, or a
    floatable-block injection surface."""
    for bid in block_ids:
        block = universe.get(bid)
        if block is None:
            return {"op": "order", "refs": list(block_ids), "ok": False,
                    "error": "unknown block"}
    return {"op": "order", "refs": list(block_ids), "ok": True,
            "note": "v1 order is a no-op (creation order is the view order)"}


def pin(universe: Universe, view: View, block_id: str, pin_flag: bool,
        note: str = "") -> Dict[str, Any]:
    block = universe.get(block_id)
    if block is None:
        return {"op": "pin" if pin_flag else "unpin", "refs": [block_id],
                "ok": False, "error": "unknown block"}
    block.pinned = bool(pin_flag)
    rec: Dict[str, Any] = {"op": "pin" if pin_flag else "unpin",
                           "refs": [block_id], "ok": True}
    if note and rec["op"] == "pin":
        view.rules[block_id] = "pin:" + str(note)[:40]
    return rec


def peek(universe: Universe, block_id: str, cap: int = 16_000) -> Dict[str, Any]:
    """Policy-side read (decision input; capped like the C2 output cap)."""
    block = universe.get(block_id)
    if block is None:
        return {"op": "peek", "refs": [block_id], "ok": False, "error": "unknown block"}
    return {"op": "peek", "refs": [block_id], "ok": True,
            "text": block.text[:cap], "chars": block.chars}


def index_blocks(universe: Universe, kinds: Optional[List[str]] = None,
                 cap: int = 200) -> Dict[str, Any]:
    """Metadata-only enumeration (mirrors list_dir's entry cap)."""
    blocks = universe.ordered(kinds)[:cap]
    return {"op": "index", "ok": True, "blocks": [b.to_meta() for b in blocks]}


def view_state(universe: Universe, view: View) -> Dict[str, Any]:
    forms = {bid: view.form_of(bid) for bid in universe.blocks}
    return {"op": "view_state", "ok": True, "forms": forms}


# ---------------------------------------------------------------------------
# notes (scratchpad)
# ---------------------------------------------------------------------------

def note_write(session: Any, text: str, cap: int) -> Dict[str, Any]:
    text = str(text or "")
    if len(text) > cap:
        return {"op": "note", "ok": False,
                "error": f"note exceeds cap ({cap})"}
    block_id, note_id = session.register_note(text)
    if block_id is None:
        return {"op": "note", "ok": False, "error": note_id}
    return {"op": "note", "ok": True, "note_id": note_id, "block_id": block_id}


def note_read(notes: Dict[str, str], note_id: str) -> Dict[str, Any]:
    if note_id not in notes:
        return {"op": "note", "refs": [note_id], "ok": False, "error": "unknown note"}
    return {"op": "note", "refs": [note_id], "ok": True, "text": notes[note_id]}


def note_list(notes: Dict[str, str]) -> Dict[str, Any]:
    return {"op": "note", "ok": True,
            "notes": [{"id": k, "chars": len(v)} for k, v in sorted(notes.items())]}


def budget_query(reader: Any, tokens_used: Optional[int] = None,
                 budget: Optional[int] = None) -> Dict[str, Any]:
    """Live usage snapshot (meter 与它同一实现, §5.2 分二).

    ``reader`` is the loop-attached ledger of THIS chat run (attached by
    ``chat_with_agent``; docs/10 §11.5): the contract keys are
    ``tokens_used`` / ``budget`` / ``tool_calls`` / ``wallclock_s`` (all
    optional, 实付口径). Explicit arguments OVERRIDE the reader's same-named
    keys; ``None`` arguments never shadow reader data."""
    snap: Dict[str, Any] = {}
    if reader is not None:
        try:
            snap.update(reader() or {})
        except Exception as e:  # noqa: BLE001 -- advisory read
            snap = {"error": str(e)[:120]}
    if tokens_used is not None:
        snap["tokens_used"] = int(tokens_used)
    if budget is not None:
        snap["budget"] = budget
    return {"op": "budget_query", "ok": True, **snap}


def fetch(universe: Universe, view: View, block_id: str,
          form: str = "full") -> Dict[str, Any]:
    """Materialize a block into the view (None≠0: restoring costs future
    tokens but never fabricates) — fetch-forms other than full substitute."""
    block = universe.get(block_id)
    if block is None:
        return {"op": "fetch", "refs": [block_id], "ok": False,
                "error": "unknown block"}
    if form == "full":
        view.set(block_id, "full", "fetch")
        return {"op": "fetch", "refs": [block_id], "ok": True,
                "text": block.text, "chars": block.chars}
    return substitute(universe, view, block_id, form, "fetch", 0)
