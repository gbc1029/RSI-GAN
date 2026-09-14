"""set_prompt - update a prompt section in the node config (shallow, no source)."""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "set_prompt",
        "description": "Update a prompt section (config.prompts.<section>) in the node config.",
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["section", "text"],
        },
    }


def tool_function(section, text, **kwargs):
    ctx = get_plan_context()
    if ctx is None or ctx.config is None:
        return "Error: no plan context"
    ctx.config.set(f"prompts.{section}", text)
    ctx.record("set_prompt", section=section)
    return f"prompt '{section}' updated ({len(text)} chars)"


op_info = tool_info
op_function = tool_function
