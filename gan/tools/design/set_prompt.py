"""Design operator: set the prompt (a config key)."""
from gan.context import get_design_context


def tool_info():
    return {
        "name": "set_prompt",
        "description": "Replace the agent's prompt (design config key 'prompt').",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }


def tool_function(text="", **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    ctx.config["prompt"] = text
    ctx.record("set_prompt", chars=len(text or ""))
    return "prompt updated"


op_info = tool_info
op_function = tool_function
