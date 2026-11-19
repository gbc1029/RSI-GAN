"""respond_challenge - the planner's explicit stance on an evaluator challenge.

A challenge is a DIRECT-DEMAND: the evaluator claims a defect survived in the
parent generation and names what "repaired" would mean mechanically. The planner
must say what it did about it. The stance is REQUIRED and structured because the
framework needs to tell "attempted and failed" from "declined" -- without it every
unrepaired challenge looks identical (silently ignored), and the calibration
between the author's claim and the mechanical verdict becomes unmeasurable.

This records a STANCE, never a proof: the adjudicator decides afterwards, from
the child's own episode. A planner that says "acted" and did nothing simply gets
``VERIFIED_UNRESOLVED``.
"""
from gan.framework.context import get_plan_context

# Same five buckets as respond_issue -- the planner's side of the conversation,
# never a ruling (the ruling belongs to the mechanical adjudicator).
_STANCES = ("acted", "acted_differently", "out_of_scope", "disputed", "deferred")


def tool_info():
    return {
        "name": "respond_challenge",
        "description": (
            "Record your stance on ONE evaluator challenge (call once per "
            "challenge_id). The framework later checks mechanically, on a fresh "
            "same-family episode, whether the defect is actually gone -- this only "
            "states what you did or why you did not act."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "challenge_id": {"type": "string"},
                "response_kind": {
                    "type": "string",
                    "enum": list(_STANCES),
                    "description": (
                        "acted = changed the task agent to address it; "
                        "acted_differently = addressed it another way; "
                        "out_of_scope = outside what this agent can change; "
                        "disputed = you believe the finding is wrong; "
                        "deferred = acknowledged, not this round."
                    ),
                },
                "feedback": {
                    "type": "string",
                    "description": "Your reasoning, for the session record only.",
                },
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
    known = {str(row.get("challenge_id")) for row in (getattr(ctx, "challenges", None) or [])}
    if known and cid not in known:
        return f"Error: unknown challenge_id: {cid} (known: {', '.join(sorted(known))})"
    if kind not in _STANCES:
        return f"Error: response_kind must be one of: {', '.join(_STANCES)}"
    if any(str(row.get("challenge_id")) == cid
           for row in (getattr(ctx, "challenge_responses", None) or [])):
        return f"Error: challenge response already recorded: {cid}"
    ctx.add_challenge_response(cid, kind, feedback=feedback)
    # Rationale stays out of the framework record (mirror of respond_issue):
    # the record is what evaluator-facing channels project from.
    ctx.record("respond_challenge", challenge_id=cid, response_kind=kind)
    return f"challenge response recorded for '{cid}': stance={kind}"


op_info = tool_info
op_function = tool_function
