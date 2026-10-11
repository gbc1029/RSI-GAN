"""ctx_index — enumerate the session's context blocks (metadata only)

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
        "name": "ctx_index",
        "description": ("Enumerate the context blocks of this session (history turns, tool outputs, trajectory segments, your notes): id, kind, size, flags — metadata only, no content. Use it to see what can be fetched or pinned."),
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "description": "optional kind filter (turn_output|tool_output|traj_seg|kb_note|note|derived)"}},
            "required": [],
        },
    }


def tool_function(kind=None):
    handle = _handle()
    if handle is None:
        return "Error: no context-management session is active for this chat"
    out = handle.index([str(kind)] if kind else None)
    return _json.dumps(out, ensure_ascii=False)


op_info = tool_info
op_function = tool_function
