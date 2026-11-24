"""Parse fixed challenge-adjudicator predictions into framework evidence."""
from __future__ import annotations

import json
import os

import pandas as pd

from domains.imo.challenge_adjudication_utils import format_input_dict
from domains.imo.challenge_contract import validate_verdict


def report_challenge_adjudication(dname, questions_path=None, expected_model=None):
    path = os.path.join(dname, "predictions.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    qids = {}
    inputs = {}
    if questions_path:
        questions = pd.read_csv(questions_path, dtype=str, keep_default_na=False)
        qids = dict(zip(questions["Challenge ID"].astype(str),
                        questions["Problem ID"].astype(str)))
        inputs = {str(row["Challenge ID"]): format_input_dict(row)
                  for _, row in questions.iterrows()}
    verdicts = {}
    invalid = []
    for _, row in frame.iterrows():
        challenge_id = str(row.get("Challenge ID", "")).strip()
        qid = str(qids.get(challenge_id, row.get("Problem ID", ""))).strip()
        try:
            value = json.loads(str(row.get("prediction", "")).strip())
        except (TypeError, ValueError):
            value = None
        try:
            if not challenge_id or not qid or challenge_id not in inputs:
                raise ValueError("missing_original_judge_input")
            validate_verdict(value, inputs[challenge_id])
            if expected_model is not None and value["model"] != expected_model:
                raise ValueError("unexpected_judge_model")
            valid = True
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            invalid.append(challenge_id or "<missing>")
            continue
        verdicts[challenge_id] = {
            "challenge_id": challenge_id,
            "qid": qid,
            **value,
        }
    report = {
        "challenge_adjudications": verdicts,
        "requested_total": len(frame),
        "total": len(verdicts),
        "coverage": (len(verdicts) / len(frame)) if len(frame) else None,
        "invalid_challenge_ids": invalid,
    }
    report_path = os.path.join(dname, "report.json")
    with open(report_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(report_path, 0o644)
    return report, report_path


__all__ = ["report_challenge_adjudication"]
