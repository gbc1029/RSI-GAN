"""Parse fixed challenge-adjudicator predictions into framework evidence."""
from __future__ import annotations

import json
import os

import pandas as pd


def report_challenge_adjudication(dname, questions_path=None):
    path = os.path.join(dname, "predictions.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    qids = {}
    if questions_path:
        questions = pd.read_csv(questions_path, dtype=str, keep_default_na=False)
        qids = dict(zip(questions["Challenge ID"].astype(str),
                        questions["Problem ID"].astype(str)))
    verdicts = {}
    invalid = []
    for _, row in frame.iterrows():
        challenge_id = str(row.get("Challenge ID", "")).strip()
        qid = str(qids.get(challenge_id, row.get("Problem ID", ""))).strip()
        try:
            value = json.loads(str(row.get("prediction", "")).strip())
        except (TypeError, ValueError):
            value = None
        valid = (
            challenge_id and qid and isinstance(value, dict)
            and set(value) == {"parent_present", "child_present",
                               "actionable", "reason"}
            and (value.get("parent_present") is None
                 or isinstance(value.get("parent_present"), bool))
            and (value.get("child_present") is None
                 or isinstance(value.get("child_present"), bool))
            and isinstance(value.get("actionable"), bool)
            and isinstance(value.get("reason"), str)
            and bool(value.get("reason", "").strip())
        )
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
