"""report_issue - record a discovered issue (tracked through the 2x2 matrix)."""
from gan.operators.context import get_eval_context


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
                "evidence": {"type": "string"},
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
        "evidence": evidence,
        "suggested_fix": suggested_fix,
    })
    return f"issue '{issue_id}' recorded"


op_info = tool_info
op_function = tool_function
