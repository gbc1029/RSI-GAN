"""Framework-owned proof challenge adjudication contract.

The proof grader supplies per-question points and coverage, but points alone do
not establish whether a natural-language challenge claim is present. This
module therefore refuses to infer defect truth from score changes. A fixed
adjudicator may provide ``parent_present``, ``child_present`` and ``actionable``
later; absent or uncertain evidence is conservatively ``UNDECIDABLE``.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

UNDECIDABLE = "UNDECIDABLE"
VERIFIED_REPAIR = "VERIFIED_REPAIR"
VERIFIED_UNRESOLVED = "VERIFIED_UNRESOLVED"
UNSUPPORTED_CHALLENGE = "UNSUPPORTED_CHALLENGE"
REGRESSION_AFTER_FALSE_CHALLENGE = "REGRESSION_AFTER_FALSE_CHALLENGE"

_STATES = frozenset({
    UNDECIDABLE, VERIFIED_REPAIR, VERIFIED_UNRESOLVED,
    UNSUPPORTED_CHALLENGE, REGRESSION_AFTER_FALSE_CHALLENGE,
})


def _evidence_ok(evidence: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if bool(evidence.get("imputed")):
        return False
    # A generation may have partial coverage because an unrelated qid failed.
    # Challenge settlement is qid-bound, so prefer the runner's per-qid
    # evidence when it is available instead of rejecting every qid on that
    # edge because of the generation-wide status.
    score_status = evidence.get("qid_score_status", evidence.get("score_status"))
    if score_status not in {"ok"}:
        return False
    if evidence.get("points") is None:
        return False
    try:
        coverage = evidence.get("qid_coverage", evidence.get("coverage"))
        return float(coverage) >= 1.0
    except (TypeError, ValueError):
        return False


def adjudicate(
    challenge: Mapping[str, Any],
    parent_evidence: Optional[Mapping[str, Any]],
    child_evidence: Optional[Mapping[str, Any]],
    *,
    parent_present: Optional[bool] = None,
    child_present: Optional[bool] = None,
    actionable: Optional[bool] = None,
    evidence_ref: str = "",
) -> Dict[str, Any]:
    """Return one auditable state without inferring claim truth from points."""
    qid = str((challenge or {}).get("qid") or "")
    if not qid or not _evidence_ok(parent_evidence) or not _evidence_ok(child_evidence):
        state, reason = UNDECIDABLE, "missing_or_unusable_grader_evidence"
    elif parent_present is None or child_present is None or actionable is None:
        state, reason = UNDECIDABLE, "missing_fixed_adjudicator_verdict"
    elif not bool(actionable):
        state, reason = UNDECIDABLE, "challenge_not_actionable"
    elif bool(parent_present) and not bool(child_present):
        state, reason = VERIFIED_REPAIR, "parent_present_child_absent"
    elif bool(parent_present) and bool(child_present):
        state, reason = VERIFIED_UNRESOLVED, "parent_present_child_present"
    elif not bool(parent_present) and not bool(child_present):
        state, reason = UNSUPPORTED_CHALLENGE, "parent_absent_child_absent"
    else:
        state, reason = REGRESSION_AFTER_FALSE_CHALLENGE, "parent_absent_child_present"
    return {
        "state": state,
        "reason": reason,
        "qid": qid,
        "actionable": (None if actionable is None else bool(actionable)),
        "parent_present": (None if parent_present is None else bool(parent_present)),
        "child_present": (None if child_present is None else bool(child_present)),
        "evidence_ref": str(evidence_ref or ""),
    }


def validate_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a fixed adjudicator result before it enters the ledger."""
    if not isinstance(result, Mapping):
        raise ValueError("challenge adjudication must be an object")
    state = str(result.get("state") or "")
    if state not in _STATES:
        raise ValueError(f"unknown challenge adjudication state: {state!r}")
    out = dict(result)
    out["state"] = state
    for key in ("parent_present", "child_present", "actionable"):
        if out.get(key) is not None and not isinstance(out[key], bool):
            raise ValueError(f"challenge adjudication {key} must be boolean or null")
    if state != UNDECIDABLE:
        if any(out.get(key) is None for key in
               ("parent_present", "child_present", "actionable")):
            raise ValueError(
                "determinate challenge adjudication requires all verdict fields")
        if out.get("actionable") is not True:
            raise ValueError(
                "determinate challenge adjudication requires actionable=true")
    return out


__all__ = [
    "UNDECIDABLE", "VERIFIED_REPAIR", "VERIFIED_UNRESOLVED",
    "UNSUPPORTED_CHALLENGE", "REGRESSION_AFTER_FALSE_CHALLENGE",
    "adjudicate", "validate_result",
]
