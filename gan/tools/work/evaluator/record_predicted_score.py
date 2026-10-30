"""record_predicted_score - blind evaluation score (before benchmark is revealed)."""
from gan.framework.context import get_eval_context


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
    # Blind-discipline gate (docs/17 §16): the tool contract is "BEFORE the
    # benchmark is revealed" -- a reveal-phase record would be a post-hoc
    # value masquerading as a blind prediction (field audit smoke_1030_204345
    # gen_1: the blind phase died on an empty response, the prediction was
    # recorded after the reveal, and the fake +0.05 delta flowed into the
    # calibration narrative). The flags are instance-private attributes on
    # the EvalContext (set by evaluator.evaluate between the two runs) --
    # `asdict` keeps them out of the IPC result, so the exact-field-set
    # contract is untouched. C1 ablation (disable_blind) keeps the gate open
    # for the whole session: the score is known from turn one BY DESIGN there.
    if getattr(ctx, "revealed", False) and getattr(ctx, "blind_discipline", True):
        return ("Error: the blind prediction window is closed (the benchmark "
                "score has been revealed). record_predicted_score is a "
                "blind-phase instrument; your blind prediction -- or its "
                "absence -- stands for calibration. Continue with issue "
                "refinement / eval points instead.")
    ctx.predicted_score = float(score)
    return f"predicted_score recorded: {ctx.predicted_score}"


op_info = tool_info
op_function = tool_function
