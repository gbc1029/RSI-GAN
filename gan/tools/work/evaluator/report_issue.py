"""report_issue - record a discovered issue (tracked through the issue-outcome classification)."""
from gan.framework.context import get_eval_context

# Soft constraint by design: evidence is a POINTER,
# not an argument -- the issue list rides json-sums directly into the planner's
# instruction (the evaluator-issues injection with its 4000-char budget), so
# reasoning prose here consumes the planner's decision surface the same way
# banned respond_issue rhetoric would. The seed already teaches the discipline;
# this only adds the mechanical leg (schema description + cap). Kept SOFT on
# purpose: evidence also doubles as a work-assignment surface (judge_fix /
# eval-point citations), hard structuring would break it.
_EVIDENCE_CAP = 200


def clip_evidence(evidence: str) -> str:
    evidence = str(evidence or "")
    if len(evidence) > _EVIDENCE_CAP:
        return evidence[:_EVIDENCE_CAP] + "...[truncated]"
    return evidence


def tool_info():
    return {
        "name": "report_issue",
        "description": "Report a problem found in the task agent's process/result/code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_id": {"type": "string"},
                "description": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": {"type": "string",
                             "description": "POINTER, not argument: a short citation "
                                            "(location / quoted line) in the trajectory "
                                            "or code. Max 200 chars -- full reasoning "
                                            "belongs in your narrative, not here."},
                "suggested_fix": {"type": "string"},
            },
            "required": ["issue_id", "description"],
        },
    }


def tool_function(issue_id, description, severity="medium", evidence="", suggested_fix="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.add_issue({
        "issue_id": issue_id,
        "description": description,
        "severity": severity,
        "evidence": clip_evidence(evidence),
        "suggested_fix": suggested_fix,
    })
    return f"issue '{issue_id}' recorded"


op_info = tool_info
op_function = tool_function
