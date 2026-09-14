"""judge_fix - evaluator judges whether a prior-round issue was actually fixed.

"fixed" must be judged from the trajectory/code (NOT merely "a diff exists").
"""
from gan.operators.context import get_eval_context


def tool_info():
    return {
        "name": "judge_fix",
        "description": (
            "Judge whether a previously-raised issue was actually fixed/improved, "
            "based on the new trajectory or (if granted) code. Cite evidence. Do NOT "
            "use 'a change exists' as the criterion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_id": {"type": "string"},
                "fixed": {"type": "boolean"},
                "evidence": {"type": "string"},
            },
            "required": ["issue_id", "fixed"],
        },
    }


def tool_function(issue_id, fixed, evidence="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.add_fix_verdict(issue_id, bool(fixed), evidence)
    return f"fix verdict recorded for '{issue_id}': fixed={bool(fixed)}"


op_info = tool_info
op_function = tool_function
