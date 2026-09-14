"""Shared gating tool: declare need for source-code access.

Default modification surface is config + operators. To view/modify source, the
agent must call this tool; the requested paths are then copied into the role
workspace ``src/`` and the grant is audited.
"""
from gan.access import get_access_context


def tool_info():
    return {
        "name": "request_source_access",
        "description": (
            "Declare that you need to VIEW or MODIFY source code. Source files are "
            "not available until you call this. After granting, the requested paths "
            "are copied into your workspace `src/` directory. Provide a reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative paths (files or dirs), e.g. ['task_agent.py'].",
                },
                "intent": {"type": "string", "enum": ["view", "modify"]},
                "reason": {"type": "string", "description": "Why source access is needed."},
            },
            "required": ["paths", "reason"],
        },
    }


def tool_function(paths=None, intent="view", reason="", **kwargs):
    ctx = get_access_context()
    if ctx is None:
        return "Error: no access context (request_source_access must run inside a role session)"
    if isinstance(paths, str):
        paths = [paths]
    granted = ctx.broker.grant(ctx.role, ctx.node_id, paths or [], intent=intent, reason=reason)
    return (
        f"Granted {intent} access to: {granted or '[]'} "
        f"(copied to workspace src/). Reason recorded."
    )


op_info = tool_info
op_function = tool_function
