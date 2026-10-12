"""ctx_budget — canonical face adapter (framework-synced stub).

Do NOT specialize this file: the face contract lives in the frozen core
(``gan/framework/cm/faces.py``, snapshot-delivered as ``cm.faces``). To
specialize, register a NEW component or move the logic into the cm policy
layer — editing this stub only creates drift against the canonical face
(source_sha stamping and the sync check in scripts/local/test_cm.py flag it).
"""
from __future__ import annotations


def _core():
    try:
        from gan.framework import cm as _cm
    except ImportError:  # task child: the per-generation snapshot
        import cm as _cm
    return _cm


_INFO, _FN = _core().make_face_tool("ctx_budget")


def tool_info():
    return dict(_INFO)


def tool_function(*args, **kwargs):
    return _FN(*args, **kwargs)


op_info = tool_info
op_function = tool_function
