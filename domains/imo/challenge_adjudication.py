"""Fixed IMO-proof challenge adjudicator.

The proof grader's 0--7 score is result evidence, not a root-cause verdict.
This adapter accepts only framework-owned, structured challenge evidence and
delegates the final state transition to the framework adjudication contract.
When the fixed evidence is absent, malformed, or inconsistent, the result is
conservatively ``UNDECIDABLE``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from gan.framework import challenge_adjudication as framework_adjudication

_ACTIONABILITY_BASES = frozenset({
    "transferable_strategy", "instance_specific_content", "uncertain",
})


def _mapping(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _artifact_verdict(evidence: Mapping[str, Any], challenge: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Read one fixed verdict from an inline or archived report artifact."""
    inline = _mapping(evidence.get("challenge_verdict"))
    if inline is not None:
        return inline

    report_path = evidence.get("report_path")
    if not isinstance(report_path, str) or not report_path:
        return None
    try:
        if not os.path.isfile(report_path):
            return None
        with open(report_path, "r", encoding="utf-8") as stream:
            report = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(report, Mapping):
        return None

    challenge_id = str(challenge.get("challenge_id") or "")
    rows = report.get("challenge_adjudications")
    if isinstance(rows, Mapping):
        return _mapping(rows.get(challenge_id))
    if isinstance(rows, list):
        qid = str(challenge.get("qid") or "")
        for row in rows:
            item = _mapping(row)
            if item is None:
                continue
            if challenge_id and str(item.get("challenge_id") or "") == challenge_id:
                return item
            if qid and str(item.get("qid") or "") == qid:
                return item
    return None


def _extract_side(verdict: Optional[Mapping[str, Any]], *, side: str,
                  challenge: Mapping[str, Any]) -> Optional[dict]:
    if verdict is None:
        return None
    expected_qid = str(challenge.get("qid") or "")
    if not expected_qid:
        return None
    if verdict.get("qid") not in (None, expected_qid):
        return None
    expected_challenge = str(challenge.get("challenge_id") or "")
    if expected_challenge and verdict.get("challenge_id") not in (None, expected_challenge):
        return None

    presence_key = f"{side}_present"
    if not isinstance(verdict.get(presence_key), bool):
        return None
    actionable = verdict.get("actionable")
    if not isinstance(actionable, bool):
        return None
    basis = verdict.get("actionability_basis")
    if basis not in _ACTIONABILITY_BASES:
        return None
    if actionable != (basis == "transferable_strategy"):
        return None
    return {presence_key: verdict[presence_key], "actionable": actionable,
            "actionability_basis": basis}


def adjudicate(
    challenge: Mapping[str, Any],
    parent_evidence: Optional[Mapping[str, Any]],
    child_evidence: Optional[Mapping[str, Any]],
) -> dict:
    """Adjudicate one IMO challenge using fixed parent/child verdict artifacts."""
    challenge = challenge if isinstance(challenge, Mapping) else {}
    parent_evidence = parent_evidence if isinstance(parent_evidence, Mapping) else {}
    child_evidence = child_evidence if isinstance(child_evidence, Mapping) else {}

    parent_verdict = _artifact_verdict(parent_evidence, challenge)
    child_verdict = _artifact_verdict(child_evidence, challenge)
    parent_side = _extract_side(parent_verdict, side="parent", challenge=challenge)
    child_side = _extract_side(child_verdict, side="child", challenge=challenge)

    parent_present = parent_side.get("parent_present") if parent_side else None
    child_present = child_side.get("child_present") if child_side else None
    actionable = None
    actionability_basis = None
    if parent_side is not None and child_side is not None:
        if (parent_side["actionable"] == child_side["actionable"]
                and parent_side["actionability_basis"] ==
                child_side["actionability_basis"]):
            actionable = parent_side["actionable"]
            actionability_basis = parent_side["actionability_basis"]

    result = framework_adjudication.adjudicate(
        challenge,
        parent_evidence,
        child_evidence,
        parent_present=parent_present,
        child_present=child_present,
        actionable=actionable,
        evidence_ref=(f"{parent_evidence.get('report_path', '')} -> "
                      f"{child_evidence.get('report_path', '')}"),
    )
    result["adjudicator"] = "imo_proof_fixed_v1"
    result["actionability_basis"] = actionability_basis
    return result


__all__ = ["adjudicate"]
