"""Design operator: set the prompt (a config key)."""
import difflib
from typing import Optional

from gan.framework.context import (design_limits, get_design_context,
                                   shallow_op_error)
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


def prompt_rewrite_error(prev, new, seed=None) -> Optional[str]:
    """R2 gate: refuse a WHOLESALE rewrite of the prompt.

    Both prompt paths used to accept any non-empty text up to the length cap, so
    the CHEAPEST "improvement" a planner could emit was to replace the whole task
    prompt. Measured on the Stage-A matrix: the planner rewrote it 385 -> 998 ->
    1351 chars (line similarity 0.24 / 0.18) and the task agent started answering
    with full reviews instead of one word. Recovering the label from those reviews
    was not enough -- the arm stayed LAST even after format correction, i.e. the
    rewrite changed the JUDGEMENT too, not just the output shape.

    A write is judged by CHARACTER-level similarity to the text it replaces (the
    line-level measure in ``prompt_change_facts`` scores 0.0 for any edit to a
    single-line prompt and cannot tell a typo fix from a replacement):

    * below ``design.prompt_rewrite_min_similarity`` -> refused, design
      untouched, and NO record (a refused write must not look like a change);
    * ``0`` disables the gate entirely;
    * an EMPTY previous prompt (first write) is always allowed;
    * a write that restores the generation-0 seed is always allowed -- that is
      the documented known-good recovery, and blocking it would leave a drifted
      prompt with no way back.

    Best-effort: an unknown limit or a non-string side never raises.
    """
    limit = design_limits().get("prompt_rewrite_min_similarity") or 0.0
    if limit <= 0.0:
        return None
    prev_s, new_s = str(prev or ""), str(new or "")
    if not prev_s.strip() or prev_s == new_s:
        return None
    if seed is not None and new_s == str(seed):
        return None
    try:
        sim = difflib.SequenceMatcher(a=prev_s, b=new_s, autojunk=False).ratio()
    except Exception:  # noqa: BLE001 -- a gate must never break the tool
        return None
    if sim >= limit:
        return None
    return (
        f"Error: prompt rewrite rejected -- only {sim:.2f} of the previous prompt "
        f"text survives (minimum {limit:.2f}, design.prompt_rewrite_min_similarity). "
        f"Replacing the whole prompt is treated as an unjustified change. Make a "
        f"SMALL targeted edit instead (append or adjust a few lines) and state the "
        f"measurable hypothesis behind it; to roll back a drifted prompt, write the "
        f"generation-0 seed text exactly (that is always allowed).")


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
                        f"{PROMPT_MAX_CHARS} chars. Only a TARGETED edit is "
                        "accepted: a write that keeps less than "
                        "design.prompt_rewrite_min_similarity of the previous "
                        "text is refused as a wholesale rewrite (restoring the "
                        "generation-0 seed is always allowed)."),
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
    err = shallow_op_error("set_prompt")
    if err:
        return err
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
    seed = _seed_prompt(ctx.role)
    # R2: a wholesale rewrite is refused here, before mutation and before any
    # record, so the design stays exactly as it was.
    err = prompt_rewrite_error(prev, text, seed=seed)
    if err:
        return err
    ctx.config["prompt"] = text
    ctx.record("set_prompt", **prompt_change_facts(prev, text, seed=seed))
    return "prompt updated"


op_info = tool_info
op_function = tool_function
