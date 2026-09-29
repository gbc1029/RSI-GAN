"""Design operator: set the prompt (a config key)."""
from typing import Optional

from gan.framework.context import get_design_context
from gan.framework.receipt import prompt_change_facts


def _seed_prompt(role: str) -> Optional[str]:
    """The generation-0 prompt for ``role`` (B29 ``equals_seed``), or None.

    Uses the SAME authority the loop seeds with (``gan.design.initial_config``:
    schema default + non-empty seed file), so "reset to the seed" is judged
    against exactly the value a fresh run would start from. Best-effort: an
    unknown seed reports ``None`` (unknown) rather than a false negative.
    """
    try:
        from gan.design import initial_config
        return initial_config(role).get("prompt")
    except Exception:  # noqa: BLE001 -- advisory fact
        return None


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
    # B29: capture the value being replaced BEFORE the overwrite -- this is the
    # only point that sees both sides (the projections receive records only).
    # The record carries structural facts only: never the prompt text, which
    # stays in the design file + session trajectory (audit evidence), and never
    # in any evaluator-facing projection.
    prev = (ctx.config or {}).get("prompt") or ""
    ctx.config["prompt"] = text
    ctx.record("set_prompt", **prompt_change_facts(prev, text,
                                                   seed=_seed_prompt(ctx.role)))
    return "prompt updated"


op_info = tool_info
op_function = tool_function
