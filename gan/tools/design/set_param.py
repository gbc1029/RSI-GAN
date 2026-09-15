"""Design operator: set a free parameter under config.params."""
from gan.context import get_design_context


def tool_info():
    return {
        "name": "set_param",
        "description": "Set a free parameter under the design config 'params'.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {}},
            "required": ["name", "value"],
        },
    }


def tool_function(name, value, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    params = ctx.config.setdefault("params", {})
    params[name] = value
    ctx.record("set_param", name=name, value=value)
    return f"params.{name} = {value!r}"


op_info = tool_info
op_function = tool_function
