"""respond_issue - planner's explicit response to an evaluator issue.

Required for the acceptance half of the 2x2 matrix. A missing response is
treated by the check step as "silently ignored" (rejected_no_feedback).

B13 (batch 8): the response carries a REQUIRED structured ``response_kind``
stance. The free-text ``feedback`` stays in the schema for the planner's own
session record and human audit, but is deliberately NOT put into
``ctx.record`` — the recorded op is what every planner→evaluator channel
projects from, so rationale is structurally out of that surface (facts flow,
rhetoric does not).
"""
from gan.framework.context import get_plan_context

# B13 stance buckets (renamed in batch 8 to not collide with the evaluator's
# fix verdicts and to say the planner's side, not a ruling):
_STANCES = ("acted", "acted_differently", "out_of_scope", "disputed", "deferred")


def tool_info():
    return {
        "name": "respond_issue",
        "description": (
            "Respond to ONE evaluator issue. Must be called for every issue raised "
            "last round, with a structured stance (response_kind). The free-text "
            "feedback is for your own session record — it is NOT passed back to the "
            "evaluator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_id": {"type": "string"},
                "accepted": {"type": "boolean"},
                "response_kind": {
                    "type": "string",
                    "enum": list(_STANCES),
                    "description": (
                        "Your stance: acted = did what was suggested; "
                        "acted_differently = acknowledged it and fixed differently; "
                        "out_of_scope = declined, outside this agent's scope; "
                        "disputed = declined, the finding is contested; "
                        "deferred = acknowledged but not this round."
                    ),
                },
                "feedback": {"type": "string",
                             "description": "Your reasoning, for the session record only."},
            },
            "required": ["issue_id", "accepted", "response_kind"],
        },
    }


def tool_function(issue_id, accepted, response_kind, feedback="", **kwargs):
    ctx = get_plan_context()
    if ctx is None:
        return "Error: no plan context"
    kind = str(response_kind)
    if kind not in _STANCES:
        return (f"Error: response_kind must be one of: {', '.join(_STANCES)}")
    # record WITHOUT the free text: downstream planner→evaluator channels
    # project from records/responses and must stay rationale-free by construction
    ctx.add_response(issue_id, bool(accepted), feedback, response_kind=kind)
    ctx.record("respond_issue", issue_id=str(issue_id), accepted=bool(accepted),
               response_kind=kind)
    return (f"response recorded for issue '{issue_id}': accepted={bool(accepted)}, "
            f"stance={kind}. Feedback noted for the session record only.")


op_info = tool_info
op_function = tool_function
