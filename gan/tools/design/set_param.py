"""Design operator: set a free parameter under config.params."""
from gan.framework.context import get_design_context


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
    if str(name) == "model":
        # Frozen framework setting: the role model is resolved from the framework
        # config / environment, never from an evolvable design.
        return "Error: parameter 'model' is reserved and cannot be set here."
    params = ctx.config.setdefault("params", {})
    params[name] = value
    ctx.record("set_param", name=name, value=value)
    return f"params.{name} = {value!r}"


op_info = tool_info
op_function = tool_function
