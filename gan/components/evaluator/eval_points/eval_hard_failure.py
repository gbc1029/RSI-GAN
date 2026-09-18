"""eval_hard_failure - strong penalty for cannot-run / obviously-unreasonable output."""
from gan.framework.context import get_eval_context


def tool_info():
    return {
        "name": "eval_hard_failure",
        "description": "Flag a hard failure (cannot run / obviously unreasonable) -> strong penalty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "triggered": {"type": "boolean"},
                "comment": {"type": "string"},
            },
            "required": ["triggered"],
        },
    }


def tool_function(triggered, comment="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.eval_point_results.append({
        "point": "hard_failure", "triggered": bool(triggered), "comment": comment,
    })
    if triggered:
        ctx.penalties["cannot_run"] = 1
    return f"hard_failure={bool(triggered)}"


op_info = tool_info
op_function = tool_function
