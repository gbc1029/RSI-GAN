"""Design operator: set an EXISTING config key (shallow).

Only keys already defined in the role schema may be set. Adding a NEW key is a
source-level (deep) change, not a shallow operator.
"""
from gan.framework.context import get_design_context
from gan.design.schema import allowed_keys


def tool_info():
    return {
        "name": "set_config",
        "description": (
            "Set an existing design-config key (e.g. 'prompt', 'skills', 'memory', 'params'). "
            "Adding a NEW key is not allowed here (requires a source-level change)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key", "value"],
        },
    }


def tool_function(key, value, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    if key not in allowed_keys(ctx.role):
        return (f"Error: '{key}' is not in the {ctx.role} design schema; "
                f"adding new keys requires a source-level (deep) change")
    if key == "params" and isinstance(value, dict) and "model" in value:
        # Frozen framework setting: role model is not an evolvable design value.
        value = {k: v for k, v in value.items() if k != "model"}
    ctx.config[key] = value
    ctx.record("set_config", key=key)
    return f"{ctx.role}.{key} updated"


op_info = tool_info
op_function = tool_function
