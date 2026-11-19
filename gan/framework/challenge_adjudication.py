"""Framework-owned challenge adjudication contract (domain-neutral).

Ported verbatim from the IMO pilot (``origin/challenge_design``) -- it contains
no domain knowledge and needed no change (docs/21 §8.1). A DOMAIN adapter
supplies the three verdict fields; this module is the ONE place that turns them
into a state, so every domain is judged by the same state machine.

Why the state is not inferred from scores: a points delta cannot establish
whether a natural-language (or behavioural) defect claim is present. IMO learned
this with ``predicted_max_points``; balrog would learn it with ``progression``.
Absent or uncertain evidence is conservatively ``UNDECIDABLE`` -- never counted
as "not fixed".
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

_MODIFICATION_SCOPES = frozenset({
    "transferable_strategy", "instance_specific_memory", "answer_leakage",
    "uncertain",
})


def _evidence_ok(evidence: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if bool(evidence.get("imputed")):
        return False
    # A generation may have PARTIAL coverage because an unrelated item failed.
    # Adjudication is item-bound, so prefer the item's own evidence (docs/21 §5.2)
    # instead of rejecting every item on that edge.
    status = evidence.get("qid_score_status", evidence.get("score_status"))
    if status not in {"ok"}:
        return False
    if evidence.get("points") is None:
        return False
    try:
        coverage = evidence.get("qid_coverage", evidence.get("coverage"))
        return float(coverage) >= 1.0
    except (TypeError, ValueError):
        return False


def adjudicate(challenge: Mapping[str, Any],
               parent_evidence: Optional[Mapping[str, Any]],
               child_evidence: Optional[Mapping[str, Any]],
               *,
               parent_present: Optional[bool] = None,
               child_present: Optional[bool] = None,
               actionable: Optional[bool] = None,
               modification_scope: Optional[str] = None,
               evidence_ref: str = "") -> Dict[str, Any]:
    """Return one auditable state without inferring claim truth from points."""
    item = str((challenge or {}).get("qid") or "")
    if not item or not _evidence_ok(parent_evidence) \
            or not _evidence_ok(child_evidence):
        state, reason = UNDECIDABLE, "missing_or_unusable_grader_evidence"
    elif parent_present is None or child_present is None or actionable is None:
        state, reason = UNDECIDABLE, "missing_fixed_adjudicator_verdict"
    elif (modification_scope is not None
          and modification_scope not in _MODIFICATION_SCOPES):
        state, reason = UNDECIDABLE, "missing_modification_scope"
    elif (modification_scope is not None
          and modification_scope != "transferable_strategy"):
        state, reason = UNDECIDABLE, "task_change_not_transferable"
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
        "qid": item,
        "actionable": (None if actionable is None else bool(actionable)),
        "modification_scope": modification_scope,
        "parent_present": (None if parent_present is None else bool(parent_present)),
        "child_present": (None if child_present is None else bool(child_present)),
        "evidence_ref": str(evidence_ref or ""),
    }


def validate_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a domain adjudicator's result before it enters the ledger."""
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


# --------------------------------------------------------------------------
# Per-domain adjudicator registry (docs/21 §8.2).
#
# The IMO port called ``domains.imo.challenge_adjudication`` from an
# ``is_imo_proof`` branch inside the loop. That shape cannot host a second
# domain: every new domain would add another ``if``, and a domain that forgot to
# register would silently fall through to "no verdict" (which the contract reads
# as UNDECIDABLE forever -- measured: balrog would never be adjudicable).
# --------------------------------------------------------------------------
_ADJUDICATORS: Dict[str, Any] = {}


def register_adjudicator(domain: str, fn: Any) -> None:
    """Register one domain adjudicator: ``fn(challenge, parent_ev, child_ev)``."""
    if not callable(fn):
        raise TypeError("challenge adjudicator must be callable")
    _ADJUDICATORS[str(domain)] = fn


def adjudicator_for(domain: str) -> Optional[Any]:
    """Look up a domain adjudicator, importing ``domains.<d>.challenge_adjudication``."""
    key = str(domain)
    if key in _ADJUDICATORS:
        return _ADJUDICATORS[key]
    try:
        import importlib
        module = importlib.import_module(f"domains.{key}.challenge_adjudication")
    except Exception:  # noqa: BLE001 -- a missing adapter is "not adjudicable"
        return None
    fn = getattr(module, "adjudicate", None)
    if callable(fn):
        _ADJUDICATORS[key] = fn
        return fn
    return None


__all__ = [
    "UNDECIDABLE", "VERIFIED_REPAIR", "VERIFIED_UNRESOLVED",
    "UNSUPPORTED_CHALLENGE", "REGRESSION_AFTER_FALSE_CHALLENGE",
    "adjudicate", "validate_result", "register_adjudicator", "adjudicator_for",
]
