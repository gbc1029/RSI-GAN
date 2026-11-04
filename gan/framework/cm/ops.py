"""CM primitives — the frozen vocabulary (docs/10 §5.2 分二).

Every primitive VALIDATES FIRST and mutates the session's view ONLY on
success (拒绝即不动 — a policy failure must never leave a half-applied op).
The seam never re-checks: these functions ARE the enforcement for their own
op, and the post-segment only counts + emits.

FACES (双形态暴露): the agent face exposes index/fetch/usage_query/pin/
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

from .blocks import (Block, Universe, View, bake, cut, form_saving,
                       PLACEHOLDER_OVERHEAD, MIN_COMPACT_SAVE,
                       DERIVED_PIECE_KEEP, estimate_tokens)

AGENT_FACES = ("index", "fetch", "usage_query", "pin", "unpin", "note",
               "view_state", "call")
# D8 ruled direction (b) thorough (docs/19 §12.27): the policy is the
# ADJUSTER — read + raw shrink primitives only. pin/unpin/note are the
# protect/create family: pin is the strongest survival lever (a pin-all
# policy silently neutralizes its own compaction), note-write is the
# create+auto-protect COMPOSITE of the two agent-owned faces' exclusive
# powers (the one union hole in the permission surface). Protection belongs
# to the agent tool face (ctx_pin, persistent intent) and the framework
# presets (structural pins); creation belongs to ctx_note.
POLICY_FACES = ("index", "fetch", "peek", "drop", "compact",
                "substitute", "order", "usage_query", "view_state",
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
    # ⑤ (docs/19 §12.21): a block not bound to canonical history never
    # renders, so eliding it saves nothing — saved counts render-visible
    # blocks only (the same口径 as render's msg_index skip).
    saved = (form_saving(block, "placeholder", rule or "drop")
             if block.msg_index is not None else 0)
    return {"op": "drop", "refs": [block_id], "ok": True,
            "rule": rule or "drop", "saved": saved}


def substitute(universe: Universe, view: View, block_id: str, form: str,
               rule: str, floor_chars: int,
               keep: Optional[int] = None) -> Dict[str, Any]:
    """``keep`` = the session's head_tail notch (``cm_head_tail_keep``),
    threaded explicitly by the caller — primitives stay session-free
    (docs/19 §12.21)."""
    block = universe.get(block_id)
    if block is None:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": "unknown block"}
    if form not in ("full", "head_tail", "placeholder", "template"):
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": f"invalid form {form!r}"}
    if form == "placeholder" and block.pinned:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": "pinned block"}
    if form in ("placeholder", "head_tail", "template") \
            and block.chars < floor_chars:
        return {"op": "substitute", "refs": [block_id], "ok": False,
                "error": f"below drop floor ({floor_chars})"}
    if form == "full":
        view.set(block_id, "full", rule or "restore")
        return {"op": "substitute", "refs": [block_id], "ok": True,
                "rule": rule or "restore", "saved": 0}
    # saved = the ACTUAL sent-text delta through bake() (single source, incl.
    # the render suffix); a block not bound to history never renders → saves
    # nothing (⑤, docs/19 §12.21).
    saved = (form_saving(block, form, rule or "substitute", keep=keep)
             if block.msg_index is not None else 0)
    view.set(block_id, form, rule or "substitute")
    return {"op": "substitute", "refs": [block_id], "ok": True,
            "rule": rule or "substitute", "saved": saved, "form": form}


def compact(universe: Universe, view: View, block_ids: List[str],
            strategy: str, floor_chars: int, turn: int,
            keep: Optional[int] = None) -> Dict[str, Any]:
    """Mechanical compaction (NO LLM). ``first_last`` stays form-level (a
    single-block strategy writing the sticky head_tail form at the session
    keep; the ``excerpt`` strategy/form retired — docs/19 §12.21).
    ``merge``/``template`` materialize a DERIVED block whose text is built
    HERE from the sources only — register_derived is not on any face, so
    no-fabrication holds by construction (docs/10 §15).

    Host = the first source bound to the canonical history (``msg_index``):
    its span renders the derived text IN FULL (the merge already did the
    compressing); every other canonical source becomes a ``merged-into``
    placeholder; stale references to consumed derived sources are re-pointed
    the same way (one view representation per piece of content). A derived
    source is PINNED on consumption — consumed nodes never re-compact, the
    lineage stays a tree, not a DAG. saved is the VIEW DELTA through bake()
    over render-visible blocks only (⑤); the GATE runs on a nominal rule
    (the derived id does not exist yet), the recorded saved is exact.
    Validate first, mutate only on success."""
    if strategy not in ("first_last", "merge", "template"):
        hint = (" (retired docs/19 §12.21; use 'first_last')"
                if strategy == "excerpt" else "")
        return {"op": "compact", "refs": list(block_ids), "ok": False,
                "error": f"unknown strategy {strategy!r}{hint}"}
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
    if strategy == "first_last":
        saved = 0
        for block in blocks:
            saving = form_saving(block, "head_tail",
                                 rule=f"compacted:{strategy}", keep=keep)
            if saving < MIN_COMPACT_SAVE:
                return {"op": "compact", "refs": list(block_ids), "ok": False,
                        "error": ("compaction saves nothing "
                                  f"({block.block_id} saves {saving})")}
            saved += saving
        for block in blocks:
            view.set(block.block_id, "head_tail", f"compacted:{strategy}")
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
                         f"head={clip_diag(seg_lines[0], 110)!r} "
                         f"tail={clip_diag(seg_lines[-1], 110)!r}")
        derived_text = "\n".join(lines)
    # view-delta gate over render-visible blocks (⑤): nominal rules (the
    # derived id does not exist yet)
    bound = [b for b in blocks if b.msg_index is not None]
    before = sum(len(bake(b, "full")) for b in bound)
    nominal_after = (len(bake(host, "derived_host", "derived=x",
                              host_text=derived_text))
                     + sum(len(bake(b, "placeholder", "merged-into:x"))
                           for b in bound if b is not host))
    if before - nominal_after < MIN_COMPACT_SAVE:
        return {"op": "compact", "refs": list(block_ids), "ok": False,
                "error": "compaction saves nothing"}

    # all gates passed — materialize and rewrite the view
    dblock = universe.register_derived("compact", derived_text, turn,
                                       [b.block_id for b in blocks])
    consumed = {b.block_id for b in blocks if b.kind == "derived"}
    for b in blocks:
        if b.kind == "derived":
            b.pinned = True   # consumed node: never re-compact (tree, not DAG)
    rule_host = f"derived={dblock.block_id}"
    rule_ph = f"merged-into:{dblock.block_id}"
    view.set(host.block_id, "derived_host", rule_host)
    for b in bound:
        if b is not host:
            view.set(b.block_id, "placeholder", rule_ph)
    # exact recorded saved: the real view delta with the REAL rules (bake
    # single source; render-visible blocks only)
    saved = (before
             - len(bake(host, "derived_host", rule_host,
                        host_text=derived_text))
             - sum(len(bake(b, "placeholder", rule_ph))
                   for b in bound if b is not host))
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
    """Pin/unpin with an optional SHORT annotation tag (docs/19 §12.19).

    The note is a ≤``PIN_NOTE_CAP`` tag stored in ``view.rules`` (readable
    back via ``view_state``); a longer note is a LOUD structured refusal
    that redirects to ``ctx_note`` — never a silent clip. Unpin clears a
    stale ``pin:`` tag (never a form rule). Validate first, mutate only on
    success."""
    block = universe.get(block_id)
    if block is None:
        return {"op": "pin" if pin_flag else "unpin", "refs": [block_id],
                "ok": False, "error": "unknown block"}
    note = str(note or "")
    if pin_flag and len(note) > PIN_NOTE_CAP:
        return {"op": "pin", "refs": [block_id], "ok": False,
                "error": (f"pin note exceeds {PIN_NOTE_CAP} chars "
                          "(short tag only; use ctx_note for longer rationale)")}
    block.pinned = bool(pin_flag)
    rec: Dict[str, Any] = {"op": "pin" if pin_flag else "unpin",
                           "refs": [block_id], "ok": True}
    if pin_flag and note:
        view.rules[block_id] = "pin:" + note
        rec["note_stored"] = note
    elif not pin_flag and str(view.rules.get(block_id, "")).startswith("pin:"):
        view.rules.pop(block_id, None)
    return rec


def clip_diag(text: str, cap: int) -> str:
    """Diagnostic-field clip with a COUNTED marker (docs/19 §12.19).

    A cut field reads ``...[+N chars]`` (total length stays <= ``cap``), an
    uncut field is byte-identical — the reader can always tell whether they
    saw everything. For error/diagnostic strings only; content surfaces use
    the ``cut``/``clip_head`` idioms."""
    text = str(text or "")
    if len(text) <= cap:
        return text
    keep = max(0, cap - 20)
    return text[:keep] + f"...[+{len(text) - keep} chars]"


def peek(universe: Universe, block_id: str, cap: int = 16_000) -> Dict[str, Any]:
    """Policy-side read (decision input; capped like the C2 output cap).

    Honest cap (docs/19 §12.19): a cut result carries ``truncated`` +
    ``shown_chars``; an uncut one carries neither (boundary-exact, like
    ``clip_head``). Full content has its own sanctioned path —
    ``fetch(form="full")`` — so peek takes NO widening parameter: it is the
    cheap bounded preview."""
    block = universe.get(block_id)
    if block is None:
        return {"op": "peek", "refs": [block_id], "ok": False, "error": "unknown block"}
    text = block.text
    if len(text) <= cap:
        return {"op": "peek", "refs": [block_id], "ok": True,
                "text": text, "chars": block.chars}
    return {"op": "peek", "refs": [block_id], "ok": True,
            "text": text[:cap], "chars": block.chars,
            "truncated": True, "shown_chars": cap}


# Index page bounds (docs/19 §12.19): the default is the context-cost guard,
# the ceiling is what a request may widen to — a clamp is NEVER silent
# (``cap_clamped``), and a capped page always reports what was not shown.
INDEX_CAP_DEFAULT = 200
INDEX_CAP_MAX = 500

# Pin annotation tag budget (docs/19 §12.19): the composed rule
# ("pin:" + note) stays inside every downstream rule surface
# (View.set's 60-char tag cap, the cm_view brief's 60).
PIN_NOTE_CAP = 40


def index_blocks(universe: Universe, kinds: Optional[List[str]] = None,
                 cap: int = INDEX_CAP_DEFAULT,
                 offset: int = 0) -> Dict[str, Any]:
    """Metadata-only enumeration (mirrors list_dir's entry cap).

    Honest capping + paging (docs/19 §12.19): ``total`` counts the kind-
    filtered set, ``returned``/``hidden``/``capped`` describe THIS page
    (``capped`` = the response does not include every match), and
    ``next_offset`` is None when no further page exists — the loop exit.
    A past-the-end offset returns a clean empty page, not an error; ``cap``
    widens up to ``INDEX_CAP_MAX`` (clamped with ``cap_clamped``, never
    silently); invalid offset/cap are structured refusals, never raises."""
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        return {"op": "index", "ok": False, "error": f"invalid cap {cap!r}"}
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return {"op": "index", "ok": False, "error": f"invalid offset {offset!r}"}
    clamped = False
    if cap > INDEX_CAP_MAX:
        cap, clamped = INDEX_CAP_MAX, True
    blocks = universe.ordered(kinds)
    total = len(blocks)
    page = blocks[offset:offset + cap]
    next_offset = offset + len(page) if offset + len(page) < total else None
    out: Dict[str, Any] = {"op": "index", "ok": True,
                           "blocks": [b.to_meta() for b in page],
                           "total": total, "returned": len(page),
                           "hidden": total - len(page), "capped": len(page) < total,
                           "offset": offset, "next_offset": next_offset}
    if clamped:
        out["cap_clamped"] = True
    return out


def view_state(universe: Universe, view: View,
               head_tail_keep: Optional[int] = None) -> Dict[str, Any]:
    """Forms + rule tags (docs/19 §12.19): the rule tags carry the pin
    annotations and the op reasons — the agent's read surface for what it
    pinned and why (the write came from ``ctx_pin``'s note). The session's
    head_tail notch rides along additively (docs/19 §12.21)."""
    forms = {bid: view.form_of(bid) for bid in universe.blocks}
    out = {"op": "view_state", "ok": True, "forms": forms,
           "rules": dict(view.rules)}
    if head_tail_keep is not None:
        out["head_tail_keep"] = head_tail_keep
    return out


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


def usage_query(reader: Any, tokens_used: Optional[int] = None,
                budget: Optional[int] = None) -> Dict[str, Any]:
    """Live usage snapshot (meter 与它同一实现, §5.2 分二).

    ``reader`` is the loop-attached ledger of THIS chat run (attached by
    ``chat_with_agent``; docs/10 §11.5): the contract keys are
    ``tokens_used`` / ``budget`` / ``tool_calls`` / ``wallclock_s`` plus the
    OPTIONAL ``input_tokens`` / ``output_tokens`` / ``reasoning_tokens``
    split (a counter the provider never reported stays ABSENT, never 0;
    all 实付口径). Explicit arguments OVERRIDE the reader's same-named
    keys; ``None`` arguments never shadow reader data. READ-ONLY: the cap
    semantics live on the runtime side (``budget``/``budget_armed`` report
    the soft stop, they do not enforce it)."""
    snap: Dict[str, Any] = {}
    if reader is not None:
        try:
            snap.update(reader() or {})
        except Exception as e:  # noqa: BLE001 -- advisory read
            snap = {"error": clip_diag(str(e), 120)}
    if tokens_used is not None:
        snap["tokens_used"] = int(tokens_used)
    if budget is not None:
        snap["budget"] = budget
    return {"op": "usage_query", "ok": True, **snap}


def fetch(universe: Universe, view: View, block_id: str,
          form: str = "full", keep: Optional[int] = None) -> Dict[str, Any]:
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
    return substitute(universe, view, block_id, form, "fetch", 0, keep=keep)
