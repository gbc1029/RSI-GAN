"""Design operator: set the prompt (a config key)."""
from typing import Optional

from gan.framework.context import get_design_context
from gan.framework.receipt import prompt_change_facts

# P-a (ops_review): prompt writes get a floor (non-empty) and a ceiling (length
# cap) at ONE shared chokepoint used by both prompt paths (``set_prompt`` and
# ``set_config(key="prompt")``). Seeds are <= ~6k chars; 20k leaves ~3.5x
# evolution headroom. An EMPTY prompt is not a legal state (the seed fallback
# rule treats blank as "use the default", i.e. no agent instructions), so a
# blank write is refused, never stored.
PROMPT_MAX_CHARS = 20_000


def prompt_write_error(text) -> Optional[str]:
    """Return an error string for an illegal prompt write, else ``None``.

    Shared by ``set_prompt`` and ``set_config(key="prompt")`` so the two
    entries cannot drift. Shape checks only -- the text is never inspected
    beyond shape, and the design is untouched on refusal (no record either:
    a refused write must not look like a change).
    """
    if not isinstance(text, str):
        return f"Error: prompt must be a string (got {type(text).__name__})"
    if not text.strip():
        return ("Error: prompt must be a non-empty string (a blank prompt "
                "leaves the agent without instructions; the seed fallback "
                "applies only to fresh runs)")
    if len(text) > PROMPT_MAX_CHARS:
        return (f"Error: prompt exceeds the {PROMPT_MAX_CHARS}-char limit "
                f"(got {len(text)})")
    return None


def _seed_prompt(role: str) -> Optional[str]:
    """The generation-0 prompt for ``role`` (``equals_seed``), or None.

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
        "description": ("Replace the agent's prompt (design config key 'prompt'). "
                        "Refuses empty/whitespace text and text over "
                        f"{PROMPT_MAX_CHARS} chars."),
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
    # P-a: floor (non-empty) + ceiling (length cap) BEFORE any mutation or
    # record -- a refused write must leave the design and the records clean.
    err = prompt_write_error(text)
    if err:
        return err
    # Capture the value being replaced BEFORE the overwrite -- this is the
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
