"""respond_challenge - planner's explicit response to one proof challenge."""
from gan.framework.context import get_plan_context

_STANCES = ("acted", "acted_differently", "out_of_scope", "disputed", "deferred")


def tool_info():
    return {
        "name": "respond_challenge",
        "description": (
            "Record one response to a direct-parent proof challenge. Call exactly "
            "once for every challenge_id. This records your stance only; it does "
            "not prove that the defect was repaired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "challenge_id": {"type": "string"},
                "response_kind": {
                    "type": "string", "enum": list(_STANCES),
                    "description": (
                        "acted/acted_differently means a task change was attempted; "
                        "disputed/deferred/out_of_scope means no repair is claimed."
                    ),
                },
                "feedback": {"type": "string"},
            },
            "required": ["challenge_id", "response_kind"],
        },
    }


def tool_function(challenge_id, response_kind, feedback="", **kwargs):
    ctx = get_plan_context()
    if ctx is None:
        return "Error: no plan context"
    cid = str(challenge_id)
    kind = str(response_kind)
    if cid not in ctx.challenge_response_ids:
        return f"Error: unknown challenge_id: {cid}"
    if kind not in _STANCES:
        return f"Error: response_kind must be one of: {', '.join(_STANCES)}"
    if any(row.get("challenge_id") == cid for row in ctx.challenge_responses):
        return f"Error: challenge response already recorded: {cid}"
    ctx.add_challenge_response(cid, kind, feedback)
    # Keep rationale out of the framework record, matching respond_issue.
    ctx.record("respond_challenge", challenge_id=cid, response_kind=kind)
    return f"challenge response recorded for '{cid}': stance={kind}"


op_info = tool_info
op_function = tool_function
