"""Metrics and two-stage activation gates for settled IMO proof challenges."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Optional

from gan.framework import challenge_adjudication as adjudication


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return float(numerator / denominator) if denominator else None


def compute(rows: Iterable[Mapping[str, Any]],
            point_gains: Optional[Mapping[str, float]] = None) -> dict:
    """Compute the §2.9 metrics from immutable settled challenge rows."""
    counts = Counter()
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        result = row.get("adjudication")
        state = result.get("state") if isinstance(result, Mapping) else None
        if state in {
            adjudication.VERIFIED_REPAIR,
            adjudication.VERIFIED_UNRESOLVED,
            adjudication.UNSUPPORTED_CHALLENGE,
            adjudication.REGRESSION_AFTER_FALSE_CHALLENGE,
            adjudication.UNDECIDABLE,
        }:
            counts[state] += 1

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
        "state_counts": {
            adjudication.VERIFIED_REPAIR: a,
            adjudication.VERIFIED_UNRESOLVED: b,
            adjudication.UNSUPPORTED_CHALLENGE: c,
            adjudication.REGRESSION_AFTER_FALSE_CHALLENGE: d,
            adjudication.UNDECIDABLE: u,
        },
        "supported_challenges": supported,
        "undecidable_challenges": u,
        "challenge_precision_raw": _ratio(a + b, supported),
        "challenge_precision_smoothed": float((a + b + 1) / (supported + 2)),
        "evaluator_adversarial_score": _ratio(b - a - c - d, supported),
        "repair_rate_raw": _ratio(a, a + b),
        "repair_rate_smoothed": float((a + 1) / (a + b + 2)),
        "planner_adversarial_score": _ratio(a - b - d, a + b + d),
        "mean_anchor_point_gain": (
            float(sum(gains) / len(gains)) if gains else None),
        "point_gain_count": len(gains),
    }


def challenge_gate(metrics: Mapping[str, Any], *, min_supported: int = 2,
                   min_repair_rate: float = 0.5,
                   min_anchor_gain: float = 0.0) -> Dict[str, Any]:
    """Apply the §2.3 per-outer challenge gate without scoring it."""
    supported = int(metrics.get("supported_challenges") or 0)
    if supported < int(min_supported):
        return {"status": "insufficient_evidence", "passed": False}
    counts = metrics.get("state_counts") or {}
    regression = int(counts.get(adjudication.REGRESSION_AFTER_FALSE_CHALLENGE, 0) or 0)
    gain = metrics.get("mean_anchor_point_gain")
    passed = bool(
        float(metrics.get("repair_rate_smoothed") or 0.0) > float(min_repair_rate)
        and isinstance(gain, (int, float))
        and float(gain) >= float(min_anchor_gain)
        and regression == 0)
    return {"status": "passed" if passed else "failed", "passed": passed,
            "false_challenge_regression": regression}


def activation_status(rows: Iterable[Mapping[str, Any]], *,
                      min_supported: int = 30,
                      min_determinate_rate: float = 0.8) -> Dict[str, Any]:
    """Return the cumulative gate for enabling adversarial fitness terms."""
    metrics = compute(rows)
    supported = int(metrics.get("supported_challenges") or 0)
    undecidable = int(metrics.get("undecidable_challenges") or 0)
    total = supported + undecidable
    rate = float(supported / total) if total else 0.0
    active = supported >= int(min_supported) and rate >= float(min_determinate_rate)
    return {"active": active, "supported_challenges": supported,
            "undecidable_challenges": undecidable,
            "determinate_rate": rate,
            "min_supported": int(min_supported),
            "min_determinate_rate": float(min_determinate_rate)}


__all__ = ["compute", "challenge_gate", "activation_status"]
