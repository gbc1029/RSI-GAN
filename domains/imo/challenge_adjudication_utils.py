"""Question contract for the fixed IMO challenge adjudicator."""

QUESTION_ID = "Challenge ID"
GROUND_TRUTH_KEY = None


def format_input_dict(row):
    return {
        "domain": "imo_challenge_adjudication",
        "problem": row["Problem"],
        "solution": row["Solution"],
        "grading_guidelines": row["Grading guidelines"],
        "parent_answer": row["Parent Response"],
        "child_answer": row["Child Response"],
        "defect_type": row["Defect Type"],
        "claim": row["Claim"],
        "evidence_ref": row["Evidence Ref"],
        "repair_criterion": row["Repair Criterion"],
    }

