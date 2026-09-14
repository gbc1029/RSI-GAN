"""apply_config - set a dotted config key to a value (shallow modification)."""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "apply_config",
        "description": "Apply a config change: set a dotted config key to a value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
            },
            "required": ["key", "value"],
        },
    }


def tool_function(key, value, **kwargs):
    ctx = get_plan_context()
    if ctx is None or ctx.config is None:
        return "Error: no plan context"
    ctx.config.set(key, value)
    ctx.record("apply_config", key=key, value=value)
    return f"config {key} = {value!r}"


op_info = tool_info
op_function = tool_function
