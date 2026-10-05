"""judge_fix - evaluator judges whether a prior-round issue was actually fixed.

"fixed" must be judged from the trajectory/code (NOT merely "a diff exists").
"""
from gan.framework.context import get_eval_context

# Same evidence discipline as report_issue -- the verdict
# evidence feeds the digest's "your evidence:" line and is fetched back by the
# evaluator itself each round; pointer discipline keeps it as a position fact.
# Soft constraint on purpose: evidence doubles as a work-assignment surface.
_EVIDENCE_CAP = 200


def clip_evidence(evidence: str) -> str:
    evidence = str(evidence or "")
    if len(evidence) > _EVIDENCE_CAP:
        return evidence[:_EVIDENCE_CAP] + "...[truncated]"
    return evidence


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
                "evidence": {"type": "string",
                             "description": "POINTER, not argument: a short citation "
                                            "(location / quoted line) in the new "
                                            "trajectory or granted code. Max 200 "
                                            "chars."},
            },
            "required": ["issue_id", "fixed"],
        },
    }


def tool_function(issue_id, fixed, evidence="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.add_fix_verdict(issue_id, bool(fixed), clip_evidence(evidence))
    return f"fix verdict recorded for '{issue_id}': fixed={bool(fixed)}"


op_info = tool_info
op_function = tool_function
