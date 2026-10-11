"""ctx_note — scratchpad working notes (write / read / list)

Frozen-core wrapper (docs/10 §5.2.3, pool B): the CM session is framework
owned; this tool only adapts parameters. Without an active CM session the
tool degrades loudly (the design selected it but no seam is wired). Dual
import posture: role sessions import the framework package; the task child
imports the per-generation snapshot (``GAN_CM_CORE``).
"""
from __future__ import annotations

import json as _json


def _core():
    try:
        from gan.framework import cm as _cm
        return _cm
    except ImportError:  # task child: the per-generation snapshot
        import os as _os
        import sys as _sys
        core = _os.environ.get("GAN_CM_CORE")
        if core and _os.path.isdir(core):
            parent = _os.path.dirname(_os.path.abspath(core))
            if parent not in _sys.path:
                _sys.path.insert(0, parent)
        import cm as _cm
        return _cm


def _handle():
    cm = _core()
    return cm.current_handle("agent")


def tool_info():
    return {
        "name": "ctx_note",
        "description": ("Your session scratchpad. action=write with text stores a note (auto-pinned; never dropped by assembly); action=read with note_id returns it; action=list enumerates. Notes persist for this session only — cross-session knowledge belongs to the knowledge base."),
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["write", "read", "list"]}, "note_id": {"type": "string"}, "text": {"type": "string"}},
            "required": [],
        },
    }


def tool_function(action='list', note_id=None, text=None):
    handle = _handle()
    if handle is None:
        return "Error: no context-management session is active for this chat"
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


op_info = tool_info
op_function = tool_function
