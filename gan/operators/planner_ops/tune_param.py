"""tune_param / apply_config - set a config key to a value (shallow modification)."""
from gan.operators.context import get_plan_context

_DESC = "Set a dotted config key to a value (e.g. 'agent.temperature' = 0.2)."


def _schema(name):
    return {
        "name": name,
        "description": _DESC,
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
            },
            "required": ["key", "value"],
        },
    }


def tool_info():
    return _schema("tune_param")


def tool_function(key, value, **kwargs):
    ctx = get_plan_context()
    if ctx is None or ctx.config is None:
        return "Error: no plan context"
    ctx.config.set(key, value)
    ctx.record("tune_param", key=key, value=value)
    return f"{key} = {value!r}"


op_info = tool_info
op_function = tool_function
