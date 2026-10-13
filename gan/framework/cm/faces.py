"""Canonical agent-face adapters for the ``ctx_*`` tool wrappers.

docs/10 face unification: the per-role files under
``gan/components/<role>/cm/`` used to be byte-identical full copies (14
files, zero divergence — pure maintenance debt). They are now
framework-synced STUBS that delegate here, so the face contract lives in
exactly one place — this frozen module. A stub can only forward: the face
list and the no-fabrication invariants are enforced by the handle (state.py
/ session.py), never by wrapper code, so there is nothing to gain and
everything to lose (drift, lying tool_info) from editing a stub. To
specialize a face, register a NEW component or move the logic into the cm
policy layer.

STDLIB-ONLY (import contract): this module is part of the snapshot package
(``cm.faces`` in the task sandbox) — add it to ``_CM_CORE_FILES`` so the
per-generation snapshot delivers it.
"""
from __future__ import annotations

import json as _json

_NO_SESSION = "Error: no context-management session is active for this chat"

# name -> (tool_info dict WITHOUT the name key duplicated, adapter fn).
# The adapter fns replicate the historical wrapper semantics EXACTLY
# (params, error strings, return shapes) — they are the single canonical
# copy of what used to be three byte-identical per-role files per face.


def _ctx_index(handle, kind=None):
    out = handle.index([str(kind)] if kind else None)
    return _json.dumps(out, ensure_ascii=False)


def _ctx_fetch(handle, block_id, form='full'):
    out = handle.fetch(str(block_id), str(form or "full"))
    if not out.get("ok"):
        return "Error: " + str(out.get("error"))
    return str(out.get("text") or "")


def _ctx_budget(handle):
    return _json.dumps(handle.budget_query(), ensure_ascii=False)


def _ctx_pin(handle, block_id, note='', unpin=False):
    out = handle.unpin(str(block_id)) if unpin else handle.pin(str(block_id), str(note or ""))
    return _json.dumps(out, ensure_ascii=False)


def _ctx_note(handle, action='list', note_id=None, text=None):
    action = str(action or "list")
    if action == "write":
        out = handle.note_write(str(text or ""))
    elif action == "read":
        out = handle.note_read(str(note_id or ""))
    else:
        out = handle.note_list()
    if action == "read" and out.get("ok"):
        return str(out.get("text") or "")
    return _json.dumps(out, ensure_ascii=False)


def _ctx_view(handle):
    return _json.dumps(handle.view_state(), ensure_ascii=False)


_SPECS: dict = {
    "ctx_index": {
        "description": ("Enumerate the context blocks of this session (history turns, tool outputs, trajectory segments, your notes): id, kind, size, flags — metadata only, no content. Use it to see what can be fetched or pinned."),
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "description": "optional kind filter (turn_output|tool_output|traj_seg|note|derived)"}},
            "required": [],
        },
        "params": ("kind=None",),
        "fn": _ctx_index,
    },
    "ctx_fetch": {
        "description": ("Fetch one context block by id (from ctx_index) into the conversation: form=full restores the whole content; head_tail/excerpt trim it. Fetching costs future tokens — fetch only what you need."),
        "input_schema": {
            "type": "object",
            "properties": {"block_id": {"type": "string"}, "form": {"type": "string", "enum": ["full", "head_tail", "excerpt"], "description": "default full"}},
            "required": [],
        },
        "params": ("block_id", "form='full'"),
        "fn": _ctx_fetch,
    },
    "ctx_budget": {
        "description": ("Report this session's cost accounting: tokens used (with reasoning split when available), tool calls, and the active budget keys with remaining headroom. Pull-based meter."),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "params": (),
        "fn": _ctx_budget,
    },
    "ctx_pin": {
        "description": ("Pin a context block by id so the view assembly must keep it (the framework guarantees respect); unpin to release. Pin sparingly — pinned blocks survive every compaction."),
        "input_schema": {
            "type": "object",
            "properties": {"block_id": {"type": "string"}, "note": {"type": "string"}, "unpin": {"type": "boolean"}},
            "required": [],
        },
        "params": ("block_id", "note=''", "unpin=False"),
        "fn": _ctx_pin,
    },
    "ctx_note": {
        "description": ("Your session scratchpad. action=write with text stores a note (auto-pinned; never dropped by assembly); action=read with note_id returns it; action=list enumerates. Notes persist for this session only — cross-session knowledge belongs to the knowledge base."),
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["write", "read", "list"]}, "note_id": {"type": "string"}, "text": {"type": "string"}},
            "required": [],
        },
        "params": ("action='list'", "note_id=None", "text=None"),
        "fn": _ctx_note,
    },
    "ctx_view": {
        "description": ("Show this session's view state: which blocks are full, elided, compacted or placeholder, and the per-turn assembly stats. Your window into what the framework kept."),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "params": (),
        "fn": _ctx_view,
    },
}


def make_face_tool(name: str):
    """Build ``(tool_info, tool_function)`` for one canonical ctx_* face.

    ``name`` MUST equal the calling stub's file stem (the identity contract:
    registered name == file stem == ``tool_info()["name"]`` — a mismatch is
    fail-closed at load time). Unknown names raise loudly: the face list is
    frozen, and a typo in a stub must never assemble a silently-degenerate
    tool."""
    from .state import current_handle

    spec = _SPECS.get(name)
    if spec is None:
        raise ValueError(
            f"make_face_tool: unknown face {name!r} (known: {sorted(_SPECS)})")

    fn = spec["fn"]

    def tool_function(*args, **kwargs):
        handle = current_handle("agent")
        if handle is None:
            return _NO_SESSION
        return fn(handle, *args, **kwargs)

    tool_info_dict = {
        "name": name,
        "description": spec["description"],
        "input_schema": spec["input_schema"],
    }
    return tool_info_dict, tool_function
