"""ctx_pin — pin/unpin a block so assembly may never drop it

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
        "name": "ctx_pin",
        "description": ("Pin a context block by id so the view assembly must keep it (the framework guarantees respect); unpin to release. Pin sparingly — pinned blocks survive every compaction."),
        "input_schema": {
            "type": "object",
            "properties": {"block_id": {"type": "string"}, "note": {"type": "string"}, "unpin": {"type": "boolean"}},
            "required": [],
        },
    }


def tool_function(block_id, note='', unpin=False):
    handle = _handle()
    if handle is None:
        return "Error: no context-management session is active for this chat"
    out = handle.unpin(str(block_id)) if unpin else handle.pin(str(block_id), str(note or ""))
    return _json.dumps(out, ensure_ascii=False)


op_info = tool_info
op_function = tool_function
