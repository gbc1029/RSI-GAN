"""DEEP gate: the single entry for source-level changes (gated + audited).

Merges the former ``request_source_access`` (view/modify) and ``code_edit``
operators: any change that is NOT expressible by shallow config/design operators
(new config keys, new/edited component implementations, new operator logic)
must go through here. Requested paths are copied into the role workspace
``src/`` so they can then be edited with the ``editor``/``bash`` skills.
"""
from gan.framework.context import get_access_context, get_design_context


def tool_info():
    return {
        "name": "request_source_access",
        "description": (
            "DEEP change: request source access to VIEW or MODIFY the given paths. Use only "
            "when shallow design/config operators cannot express the change (e.g. adding a new "
            "config key, a new component implementation, or editing an implementation). "
            "Requesting a path you already hold KEEPS your local copy; pass refresh=true to "
            "discard local edits and re-copy the pristine repo version."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "intent": {"type": "string", "enum": ["view", "modify"]},
                "reason": {"type": "string"},
                "refresh": {
                    "type": "boolean",
                    "description": ("Replace your workspace copies with the pristine repo "
                                    "version, DISCARDING local edits made this session. "
                                    "Default false (existing copies are kept as-is)."),
                },
            },
            "required": ["paths", "reason"],
        },
    }


def tool_function(paths=None, intent="view", reason="", refresh=False, **kwargs):
    actx = get_access_context()
    dctx = get_design_context()
    if actx is None:
        return "Error: no access context"
    if isinstance(paths, str):
        paths = [paths]
    # Default (if_absent): never overwrite a workspace copy that already exists -- it may
    # hold this session's edit_source edits or unregister_component deletions. refresh=True
    # is the explicit escape hatch that discards them and re-copies the repo version.
    granted = actx.broker.grant(actx.role, actx.node_id, paths or [], intent=intent,
                                reason=reason, if_absent=not refresh)
    kept = list((getattr(actx.broker, "last_result", None) or {}).get("skipped") or [])
    if dctx is not None:
        dctx.record("request_source_access", paths=granted, intent=intent, reason=reason)
    # NOTE: denied/missing paths are audited (events.jsonl + broker.last_result) but are
    # intentionally NOT surfaced here. Telling the agent which paths are frozen would steer
    # it toward the trust anchor / the gate itself (see DGM-H drift analysis). The agent
    # only learns what it actually received.
    if granted:
        try:
            src_root = actx.broker.src_dir(actx.role, actx.node_id)
        except Exception:
            src_root = None
        where = f" (workspace src: {src_root})" if src_root else ""
        kept_note = (f" Already in your workspace and kept as-is (not overwritten): {kept}."
                     if kept else "")
        if intent == "modify":
            return (f"Granted {intent} access to: {granted}{where}. "
                    f"Edit these copies with `edit_source`.{kept_note}")
        return f"Granted {intent} access to: {granted}{where}.{kept_note}"
    if paths:
        return ("No accessible paths (denied / not in your editable set). "
                "Call `list_editable` to see which paths you may request.")
    return "No source paths were provided for this request."


op_info = tool_info
op_function = tool_function
