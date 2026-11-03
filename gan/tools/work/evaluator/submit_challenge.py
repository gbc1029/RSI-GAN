"""submit_challenge - lock one blind, qid-bound proof challenge proposal."""
from gan.framework.context import get_eval_context
from gan.framework.challenge_ledger import ChallengeError, validate_candidate


def tool_info():
    return {
        "name": "submit_challenge",
        "description": (
            "Submit one falsifiable blind challenge for an IMO proof train question. "
            "Do not include grader scores, validation results, reference solutions, "
            "or post-reveal information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "qid": {"type": "string"},
                "defect_type": {"type": "string"},
                "claim": {"type": "string"},
                "evidence_ref": {"type": "string"},
                "repair_criterion": {"type": "string"},
            },
            "required": ["qid", "defect_type", "claim", "evidence_ref",
                          "repair_criterion"],
        },
    }


def tool_function(qid, defect_type, claim, evidence_ref, repair_criterion, **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    config = ctx.challenge_config()
    if config["domain"] != "imo_proof":
        return "Error: submit_challenge is enabled only for the imo_proof domain"
    if not config["blind_only"] or getattr(ctx, "revealed", False):
        return "Error: challenge submission is available only in the blind phase"
    if len(ctx.challenges) >= config["max_k"]:
        return f"Error: challenge limit exhausted (max_k={config['max_k']})"
    try:
        row = validate_candidate(
            {"qid": qid, "defect_type": defect_type, "claim": claim,
             "evidence_ref": evidence_ref, "repair_criterion": repair_criterion},
            allowed_qids=config["allowed_qids"],
            seen_qids=[item.get("qid") for item in ctx.challenges],
        )
    except ChallengeError as exc:
        return f"Error: {exc}"
    ctx.challenges.append(row)
    return f"challenge recorded for train qid '{row['qid']}'"


op_info = tool_info
op_function = tool_function
