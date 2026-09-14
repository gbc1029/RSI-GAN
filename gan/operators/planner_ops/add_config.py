"""add_config - add a new key to the config pool (config.custom.<key>)."""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "add_config",
        "description": (
            "Add a NEW config entry to the config pool (config.custom.<key>) with a "
            "rationale, e.g. a new context-management strategy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
                "rationale": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    }


def tool_function(key, value, rationale="", **kwargs):
    ctx = get_plan_context()
    if ctx is None or ctx.config is None:
        return "Error: no plan context"
    ctx.config.add_custom(key, value, rationale=rationale)
    ctx.record("add_config", key=key, value=value, rationale=rationale)
    return f"custom config '{key}' added"


op_info = tool_info
op_function = tool_function
