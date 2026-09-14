"""record_predicted_score - blind evaluation score (before benchmark is revealed)."""
from gan.operators.context import get_eval_context


def tool_info():
    return {
        "name": "record_predicted_score",
        "description": (
            "Record your BLIND prediction of the task agent's score (0..1) BEFORE the "
            "benchmark objective score is revealed. Used for calibration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"score": {"type": "number"}},
            "required": ["score"],
        },
    }


def tool_function(score, **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.predicted_score = float(score)
    return f"predicted_score recorded: {ctx.predicted_score}"


op_info = tool_info
op_function = tool_function
