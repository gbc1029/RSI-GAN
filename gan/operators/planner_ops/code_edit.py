"""code_edit - gated source modification: requests source access first.

This is the "fallback" operator that touches source. It triggers the
source-access gate so the requested files are transferred into the workspace
``src/``; the agent is expected to edit them with the ``edit``/``bash`` tools
afterwards. The action is recorded for the loop to collect a patch.
"""
from gan.access import get_access_context
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "code_edit",
        "description": (
            "Request source access in order to MODIFY the given paths. After granting, "
            "edit the copied files under workspace src/. Use only when config/operators "
            "cannot express the improvement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["paths", "reason"],
        },
    }


def tool_function(paths=None, reason="", **kwargs):
    actx = get_access_context()
    pctx = get_plan_context()
    if actx is None:
        return "Error: no access context"
    if isinstance(paths, str):
        paths = [paths]
    granted = actx.broker.grant(actx.role, actx.node_id, paths or [], intent="modify", reason=reason)
    if pctx is not None:
        pctx.record("code_edit", paths=granted, reason=reason)
    return (
        f"Granted modify access to: {granted or '[]'} (copied to workspace src/). "
        f"You may now edit these files."
    )


op_info = tool_info
op_function = tool_function
