"""eval_rule_violation - detect cheating beyond benchmark rules."""
from gan.framework.context import get_eval_context


def tool_info():
    return {
        "name": "eval_rule_violation",
        "description": (
            "Report a rule violation / cheating (reading answers, tampering with the "
            "evaluation). Cite evidence; request source access if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "violation": {"type": "boolean"},
                "evidence": {"type": "string"},
            },
            "required": ["violation"],
        },
    }


def tool_function(violation, evidence="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    ctx.eval_point_results.append({
        "point": "rule_violation", "violation": bool(violation), "evidence": evidence,
    })
    if violation:
        ctx.penalties["rule_violation"] = 1
    return f"rule_violation={bool(violation)}"


op_info = tool_info
op_function = tool_function
