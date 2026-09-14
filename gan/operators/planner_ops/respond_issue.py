"""respond_issue - planner's explicit response to an evaluator issue.

Required for the acceptance half of the 2x2 matrix. A missing response is
treated by the check step as "silently ignored" (rejected_no_feedback).
"""
from gan.operators.context import get_plan_context


def tool_info():
    return {
        "name": "respond_issue",
        "description": (
            "Respond to ONE evaluator issue. Must be called for every issue raised "
            "last round. accepted=false MUST include a feedback reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_id": {"type": "string"},
                "accepted": {"type": "boolean"},
                "feedback": {"type": "string", "description": "Reason (required when rejected)."},
            },
            "required": ["issue_id", "accepted"],
        },
    }


def tool_function(issue_id, accepted, feedback="", **kwargs):
    ctx = get_plan_context()
    if ctx is None:
        return "Error: no plan context"
    ctx.add_response(issue_id, bool(accepted), feedback)
    return f"response recorded for issue '{issue_id}': accepted={bool(accepted)}"


op_info = tool_info
op_function = tool_function
