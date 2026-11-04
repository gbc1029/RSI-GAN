"""Pilot-only metrics for settled IMO proof challenges.

These values are diagnostic in the current phase. They are deliberately
computed from framework-settled adjudication states and never alter fitness or
parent selection.
"""
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


__all__ = ["compute"]
