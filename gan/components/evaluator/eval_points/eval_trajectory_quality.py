"""eval_trajectory_quality - process-level judgement of the execution trajectory."""
from gan.context import get_eval_context


def tool_info():
    return {
        "name": "eval_trajectory_quality",
        "description": "Judge the execution trajectory (process quality: efficiency, dead ends, reproducibility).",
        "input_schema": {
            "type": "object",
            "properties": {
                "rating": {"type": "number", "description": "0..10 process score"},
                "comment": {"type": "string"},
            },
            "required": ["rating"],
        },
    }


def tool_function(rating, comment="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.eval_point_results.append({
        "point": "trajectory_quality", "rating": float(rating), "comment": comment,
    })
    if comment:
        ctx.weaknesses.append(comment)
    return f"trajectory_quality = {rating}"


op_info = tool_info
op_function = tool_function
