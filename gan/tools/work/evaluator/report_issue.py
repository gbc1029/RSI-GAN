"""report_issue - record a discovered issue (tracked through the issue-outcome classification)."""
from gan.framework.context import get_eval_context
from gan.framework.issue_keys import KEY_NAMES, normalize_issue_key, validate_issue_key

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
                "issue_id": {"type": "string",
                             "description": "Local handle for THIS round (i-1, i-2, ...). "
                                            "The framework prefixes it with the "
                                            "generation, so raising the same defect "
                                            "again needs a NEW local id."},
                # ``issue_key`` is the cross-round identity (the closed vocabulary
                # in gan/framework/issue_keys.py). The enum makes it a hard gate
                # at dispatch rather than a prompt request: a free slug could be
                # re-spelled every round and erase the continuity the ledger
                # exists to provide.
                "issue_key": {
                    "type": "string",
                    "enum": list(KEY_NAMES),
                    "description": "Which DEFECT this is, from the fixed vocabulary. "
                                   "Reuse the same key when re-raising a defect that "
                                   "is still unfixed, even if the wording differs.",
                },
                "description": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": {"type": "string",
                             "description": "POINTER, not argument: a short citation "
                                            "(location / quoted line) in the trajectory "
                                            "or code. Max 200 chars -- full reasoning "
                                            "belongs in your narrative, not here."},
                "suggested_fix": {"type": "string"},
            },
            "required": ["issue_id", "issue_key", "description"],
        },
    }


def tool_function(issue_id, description, severity="medium", evidence="",
                  suggested_fix="", issue_key=None, **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    err = validate_issue_key(issue_key)
    if err:
        # Recorded under the escape hatch instead of dropping the finding: a
        # mistyped key must not lose the defect, and the loop logs other.new.
        issue_key = normalize_issue_key(issue_key)
    ctx.add_issue({
        "issue_id": issue_id,
        "issue_key": issue_key,
        "description": description,
        "severity": severity,
        "evidence": clip_evidence(evidence),
        "suggested_fix": suggested_fix,
    })
    note = f" (key normalized to '{issue_key}': {err})" if err else ""
    return f"issue '{issue_id}' recorded{note}"



op_info = tool_info
op_function = tool_function
