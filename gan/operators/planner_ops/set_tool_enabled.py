"""set_tool_enabled - toggle a tool for the task agent (config)."""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "set_tool_enabled",
        "description": "Enable/disable a tool for the task agent (config.tools.<name>.enabled).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
        },
    }


def tool_function(name, enabled, **kwargs):
    ctx = get_plan_context()
    if ctx is None or ctx.config is None:
        return "Error: no plan context"
    ctx.config.set(f"tools.{name}.enabled", bool(enabled))
    ctx.record("set_tool_enabled", name=name, enabled=bool(enabled))
    return f"tool '{name}' enabled={bool(enabled)}"


op_info = tool_info
op_function = tool_function
