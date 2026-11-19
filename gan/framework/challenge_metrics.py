"""Challenge metrics + the two-stage activation gate (domain-neutral).

Ported from the IMO branch's ``challenge_metrics.py`` with TWO deliberate
changes, both recorded in ``docs/21_balrog出题式挑战_设计与实施计划.md`` §7:

1. **The adversarial term is gone.** The IMO module exposed
   ``evaluator_adversarial_score = (b - a - c - d) / supported`` (b = verified
   unresolved). That formula pays the author for producing questions the task
   agent CANNOT fix, which is exactly the wrong pressure once the evaluator is
   the author: it rewards "make it hard" over "make it diagnostic". It is not
   returned at all, so it cannot be wired by accident.
2. **``challenge_calibration`` is added.** The evaluator still emits its own
   ``judge_fix`` prediction; the adjudicator supplies the mechanical truth. The
   agreement rate is the "does my own reading of the defect match reality"
   signal -- the same discipline as ``verdict_stability`` (bound by a mechanical
   checker, never by a predicate the model writes).

The three terms that DO feed fitness are ``precision_smoothed`` /
``actionability_rate`` / ``challenge_calibration``; ``VERIFIED_UNRESOLVED``
counts and ``repair_rate`` stay in the diagnostics column.

Two-stage activation (IMO discipline, kept): nothing is scored until
``supported >= min_supported`` AND ``determinate_rate >= min_determinate_rate``,
so an arm cannot be selected on three lucky challenges.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Optional

from gan.framework import challenge_adjudication as adjudication

#: every state the machine can settle on (settled rows only)
_STATES = (
    adjudication.VERIFIED_REPAIR,
    adjudication.VERIFIED_UNRESOLVED,
    adjudication.UNSUPPORTED_CHALLENGE,
    adjudication.REGRESSION_AFTER_FALSE_CHALLENGE,
    adjudication.UNDECIDABLE,
)

#: default fitness weights (docs/21 §7). Stages 2 sets the real values; the
#: numbers live here so the pilot cannot drift from the plan silently.
DEFAULT_WEIGHTS = {"precision": 0.10, "actionability": 0.05, "calibration": 0.05}


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return float(numerator / denominator) if denominator else None


def _state_of(row: Mapping[str, Any]) -> Optional[str]:
    result = row.get("adjudication")
    state = result.get("state") if isinstance(result, Mapping) else None
    return state if state in _STATES else None


def _predicted_fix(row: Mapping[str, Any]) -> Optional[bool]:
    """The evaluator's own ``judge_fix`` prediction attached to the row."""
    prediction = row.get("prediction")
    if not isinstance(prediction, Mapping):
        return None
    value = prediction.get("judge_fix")
    return value if isinstance(value, bool) else None


def _truth_repaired(state: str) -> Optional[bool]:
    """Adjudicator truth in the same vocabulary as the prediction.

    Only the two determinate states carry a truth: a challenge that was never
    supported (or that regressed after a false claim) says nothing about
    whether the DEFECT was repaired, and folding those into ``False`` would
    punish an honest evaluator for an unsupported claim twice.
    """
    if state == adjudication.VERIFIED_REPAIR:
        return True
    if state == adjudication.VERIFIED_UNRESOLVED:
        return False
    return None


def compute(rows: Iterable[Mapping[str, Any]],
            point_gains: Optional[Mapping[str, float]] = None) -> dict:
    """Compute the metrics from immutable settled challenge rows."""
    counts: Counter = Counter()
    agree = 0
    calibrated = 0
    actionable = 0
    with_prediction = 0
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        state = _state_of(row)
        if state is None:
            continue
        counts[state] += 1
        # actionability: the mechanical "was this claim in the closed vocabulary
        # and about a family the parent actually played" check (stage 1 fills it)
        if row.get("actionable") is True:
            actionable += 1
        predicted = _predicted_fix(row)
        truth = _truth_repaired(state)
        if predicted is None:
            continue
        with_prediction += 1
        if truth is None:
            continue
        calibrated += 1
        if predicted is truth:
            agree += 1

    a = counts[adjudication.VERIFIED_REPAIR]
    b = counts[adjudication.VERIFIED_UNRESOLVED]
    c = counts[adjudication.UNSUPPORTED_CHALLENGE]
    d = counts[adjudication.REGRESSION_AFTER_FALSE_CHALLENGE]
    u = counts[adjudication.UNDECIDABLE]
    supported = a + b + c + d

    gains = []
    for gain in (point_gains or {}).values():
        try:
            gains.append(float(gain))
        except (TypeError, ValueError):
            continue

    return {
        "state_counts": {state: counts[state] for state in _STATES},
        "supported_challenges": supported,
        "undecidable_challenges": u,
        # --- fitness terms (docs/21 §7) ---
        "precision_raw": _ratio(a + b, supported),
        "precision_smoothed": float((a + b + 1) / (supported + 2)),
        "actionability_rate": _ratio(actionable, supported + u),
        "challenge_calibration": _ratio(agree, calibrated),
        # --- diagnostics only: never a fitness term (docs/21 §7) ---
        "repair_rate_raw": _ratio(a, a + b),
        "repair_rate_smoothed": float((a + 1) / (a + b + 2)),
        "calibration_samples": calibrated,
        "prediction_rows": with_prediction,
        "mean_anchor_point_gain": (
            float(sum(gains) / len(gains)) if gains else None),
        "point_gain_count": len(gains),
    }


def activation_status(rows_or_metrics: Any, *, min_supported: int = 30,
                      min_determinate_rate: float = 0.8) -> dict:
    """Cumulative gate: is there enough settled evidence to score the author?

    Accepts either the raw rows or an already-computed metrics dict, so a caller
    that needs both numbers does not compute them twice.
    """
    metrics = (rows_or_metrics if isinstance(rows_or_metrics, Mapping)
               else compute(rows_or_metrics))
    supported = int(metrics.get("supported_challenges") or 0)
    undecidable = int(metrics.get("undecidable_challenges") or 0)
    total = supported + undecidable
    rate = float(supported / total) if total else 0.0
    active = supported >= int(min_supported) and rate >= float(min_determinate_rate)
    return {
        "active": bool(active),
        "supported_challenges": supported,
        "undecidable_challenges": undecidable,
        "determinate_rate": rate,
        "min_supported": int(min_supported),
        "min_determinate_rate": float(min_determinate_rate),
    }


def fitness_terms(metrics: Mapping[str, Any], *,
                  active: Optional[bool] = None,
                  normalize: Optional[Callable[[str, Optional[float]], float]] = None,
                  weights: Optional[Mapping[str, float]] = None) -> dict:
    """Combine the three terms into ``F_e_adv``.

    ``active`` defaults to the cumulative gate's answer on these very metrics,
    so an inactive term set can never be scored by forgetting to pass it.

    ``normalize`` maps ``(term_name, raw_value)`` to a comparable number; the
    default is the identity (the three terms are already shares in [0, 1]). The
    z-score source is a stage-2 decision -- it needs cross-generation history
    that does not exist yet -- so the default is deliberately the honest simple
    one rather than a half-implemented z.
    """
    gate = activation_status(metrics)
    is_active = bool(gate["active"]) if active is None else bool(active)
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in DEFAULT_WEIGHTS})
    norm = normalize or (lambda _name, value: float(value or 0.0))
    terms = {}
    for name, key in (("precision", "precision_smoothed"),
                      ("actionability", "actionability_rate"),
                      ("calibration", "challenge_calibration")):
        raw = metrics.get(key)
        # a missing signal is NEUTRAL (0.0), never "failed"
        terms[name] = norm(name, raw) if isinstance(raw, (int, float)) else 0.0
    score = sum(w[name] * terms[name] for name in w) if is_active else 0.0
    return {
        "active": is_active,
        "gate": gate,
        "terms": terms,
        "weights": w,
        "score": float(score),
    }


__all__ = ["DEFAULT_WEIGHTS", "activation_status", "compute", "fitness_terms"]
