"""Challenge metrics + the two-stage activation gate (domain-neutral).

Ported from the IMO branch's ``challenge_metrics.py`` with TWO deliberate
changes, both recorded in ``docs/21_balrog出题式挑战_设计与实施计划.md`` §7:

1. **The adversarial term is gone.** The IMO module exposed
   ``evaluator_adversarial_score = (b - a - c - d) / supported`` (b = verified
   unresolved). That formula pays the author for producing questions the task
   agent CANNOT fix, which is exactly the wrong pressure once the evaluator is
   the author: it rewards "make it hard" over "make it diagnostic". It is not
   consumed by the authored-episode path. The qid-bound IMO path retains its
   original adversarial score fields for compatibility; the loop selects only
   one domain's scoring path.
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

WIRED PATH (docs/21 §16, 2026-11-21) -- what actually ships
-----------------------------------------------------------
Of the three terms above only the FIRST one is wired, and not as its own weight
axis: ``precision_smoothed`` (renamed ``claim_accuracy``: "was the defect I
claimed really there") is blended into ``verdict_stability`` by
``blend_verdict_stability`` with a sample-size anneal. ``actionability`` is
deliberately NOT wired (its denominator is the planner's response, so scoring it
as the evaluator's ability rewards picking easy-to-fix defects) and
``challenge_calibration`` is NOT wired either (``F_e`` already reads
``calibration_error`` at weight 0.6 -- the same latent variable, so a second term
would pay twice). ``fitness_terms`` below is kept for audit and for the IMO
parity record; it is NOT the wired path and nothing consumes its ``score``.
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
    """The evaluator's own ``judge_fix`` prediction attached to the row.

    NOTE (2026-11-21): nothing writes this key today, so ``challenge_calibration``
    reports ``None`` -- an honest ABSENCE, not a zero. Measured on the archived
    runs: the ledger rows carry ``adjudication`` but no ``prediction``, so the
    agreement rate has no samples. Left as-is rather than faked from the
    evaluator's outer-level ``predicted_score`` (a different quantity).
    """
    prediction = row.get("prediction")
    if not isinstance(prediction, Mapping):
        return None
    value = prediction.get("judge_fix")
    return value if isinstance(value, bool) else None


def _verdict_of(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The immutable adjudication block of a ledger row (or empty)."""
    result = row.get("adjudication")
    return result if isinstance(result, Mapping) else {}


def _actionable_of(row: Mapping[str, Any]) -> Optional[bool]:
    """Did the ADJUDICATOR decide the claim?

    Lives inside ``adjudication`` on a real ledger row; the top-level key is
    accepted for hand-built fixtures. Measured 2026-11-21: reading only the
    top-level key made ``actionability_rate`` report 0.0 for every archived run
    -- and because this module had no caller, nothing noticed.

    READ IT AS "the verdict was decidable", NOT "the claim was fixable": the
    domain adapter sets it to true whenever both presence bits are known, and the
    state machine then REQUIRES it for any determinate state. The genuinely
    interesting question -- was the planner's change a transferable strategy
    rather than instance-specific knowledge -- is ``modification_scope``, counted
    below as ``transferable_share``.
    """
    for source in (row, _verdict_of(row)):
        value = source.get("actionable")
        if isinstance(value, bool):
            return value
    return None


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
    rows = list(rows or ())
    counts: Counter = Counter()
    scopes: Counter = Counter()
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
        scope = _verdict_of(row).get("modification_scope")
        scopes[str(scope)] += 1
        # actionability: see _actionable_of -- it is the DECIDABILITY of the
        # verdict, kept because the legacy three-term combination reads it.
        if _actionable_of(row) is True:
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

    # ISSUE ↔ CHALLENGE JOIN (docs/21 §19). ``join_rate`` is the number the soft
    # binding exists to make observable: "of the adjudicated challenges, how many
    # came with a cited issue whose issue_key matched". ``by_issue_key`` lets the
    # precision be read PER DEFECT IDENTITY, i.e. in the same units the issue
    # channel uses -- which is the precondition for ever merging the two into one
    # term (docs/21 §16) instead of averaging two sample sets.
    linked = 0
    by_key: Dict[str, Dict[str, int]] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        state = _state_of(row)
        if state is None:
            continue
        key = str(row.get("issue_key") or "").strip()
        if str(row.get("issue_id") or "").strip():
            linked += 1
        bucket = by_key.setdefault(key or "(unlinked)",
                                   {name: 0 for name in _STATES})
        bucket[state] += 1
    decided = sum(1 for row in (rows or ())
                  if isinstance(row, Mapping) and _state_of(row) is not None)

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
        # How many INDEPENDENT outers produced DETERMINATE settled rows. This is
        # the sample-size guard a raw count cannot express: 30 challenges from one
        # outer share one planner trajectory, so they are not 30 independent
        # observations (see ``blend_verdict_stability``). An outer whose only
        # challenges were UNDECIDABLE contributed no claim evidence, so counting
        # it would inflate the guard -- the exact "n looks big, evidence is thin"
        # mistake this field exists to prevent.
        "outer_count": len({row.get("outer") for row in (rows or ())
                            if isinstance(row, Mapping)
                            and _state_of(row) not in (None, adjudication.UNDECIDABLE)
                            and row.get("outer") is not None}),
        # --- fitness terms (docs/21 §7) ---
        "challenge_precision_raw": _ratio(a + b, supported),
        "challenge_precision_smoothed": float((a + b + 1) / (supported + 2)),
        "evaluator_adversarial_score": _ratio(b - a - c - d, supported),
        "planner_adversarial_score": _ratio(a - b - d, a + b + d),
        "precision_raw": _ratio(a + b, supported),
        "precision_smoothed": float((a + b + 1) / (supported + 2)),
        # Same quantity under the name the WIRED path uses: "was the defect I
        # claimed really there". ``precision`` is the historical label; keeping
        # both means the audit reads a term whose meaning is not in doubt.
        "claim_accuracy": float((a + b + 1) / (supported + 2)),
        "actionability_rate": _ratio(actionable, supported + u),
        "challenge_calibration": _ratio(agree, calibrated),
        # --- issue ↔ challenge join (docs/21 §19; diagnostics) ---
        "linked_challenges": linked,
        "unlinked_challenges": decided - linked,
        "join_rate": _ratio(linked, decided),
        "by_issue_key": by_key,
        # The diagnostic that actually answers "did the planner learn a METHOD":
        # share of determinate verdicts whose change was judged a transferable
        # strategy (the alternative scopes are instance_specific_memory /
        # answer_leakage / uncertain).
        "transferable_share": _ratio(scopes.get("transferable_strategy", 0), supported),
        "modification_scopes": dict(scopes),
        # --- diagnostics only: never a fitness term (docs/21 §7) ---
        "repair_rate_raw": _ratio(a, a + b),
        "repair_rate_smoothed": float((a + 1) / (a + b + 2)),
        "calibration_samples": calibrated,
        "prediction_rows": with_prediction,
        "mean_anchor_point_gain": (
            float(sum(gains) / len(gains)) if gains else None),
        "point_gain_count": len(gains),
    }


def blend_verdict_stability(vs_issue: Any, *,
                            metrics: Mapping[str, Any],
                            weight_max: float = 0.10,
                            shrink_n0: float = 10.0,
                            min_outer: int = 3,
                            determinate_min: float = 0.8) -> dict:
    """Blend the challenge claim-accuracy into ``verdict_stability`` (docs/21 §16).

    WHY A BLEND AND NOT A NEW TERM. ``F_e`` already reads
    ``evaluator_stats.calibration_error`` with the LARGEST weight (0.6), and
    ``verdict_stability`` is the per-issue mechanical truth. A separate
    ``w3 * precision`` would (a) pay twice for the same evidence -- "is what I
    claimed actually true" is ONE latent variable, measured by two instruments --
    and (b) leave room for two standards over two sample sets. A convex blend
    instead lets the STRONGER instrument (a claim that survived independent
    instantiation) pull the same number, without changing the axis.

    WHY IT SHRINKS. The challenge evidence is tiny and self-selected (the author
    picks what to claim), so it may only move the number once there are enough
    INDEPENDENT outers and the rows are mostly determinate. ``w = weight_max *
    n / (n + shrink_n0)``: n=4 -> 0.029, n=30 -> 0.075, n=100 -> 0.091. That is
    deliberately an anneal, not a switch: an on/off gate would either ignore the
    signal forever (the ``supported >= 30`` trap this project already hit) or
    jump the fitness landscape.

    INERTNESS (the hard requirement): when no term applies, the returned value IS
    ``vs_issue`` unchanged -- same object, same float -- so a run with the
    challenge machinery off is byte-identical to one that predates it.

    ``vs_issue`` None means the issue channel had nothing mechanically verifiable
    this outer; the blend then still yields None unless it applies, in which case
    the challenge rate stands in as the only available truth.
    """
    n = int(metrics.get("supported_challenges") or 0)
    undecidable = int(metrics.get("undecidable_challenges") or 0)
    outers = int(metrics.get("outer_count") or 0)
    total = n + undecidable
    determinate = float(n / total) if total else 0.0
    claim = metrics.get("claim_accuracy")
    reasons = []
    if n <= 0:
        reasons.append("no_settled_challenges")
    if outers < int(min_outer):
        reasons.append(f"independent_outers<{int(min_outer)}")
    if determinate < float(determinate_min):
        reasons.append("determinate_rate_low")
    if not isinstance(claim, (int, float)) or isinstance(claim, bool):
        # nothing to blend IN: reporting a non-zero w here would document a
        # weight that is not used
        reasons.append("claim_accuracy_missing")
    w = 0.0
    if not reasons:
        w = float(weight_max) * (n / (n + float(shrink_n0)))
    base = float(vs_issue) if isinstance(vs_issue, (int, float)) \
        and not isinstance(vs_issue, bool) else None
    if w <= 0.0 or not isinstance(claim, (int, float)):
        value = base          # INERT: identical to the un-blended number
        applied = False
    elif base is None:
        value = float(claim)
        applied = True
    else:
        value = (1.0 - w) * base + w * float(claim)
        applied = True
    return {
        "value": value,
        "applied": bool(applied),
        "w": float(w),
        "vs_issue": base,
        "claim_accuracy": (float(claim) if isinstance(claim, (int, float)) else None),
        "samples": n,
        "outer_count": outers,
        "determinate_rate": determinate,
        "reason": ",".join(reasons),
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


__all__ = ["DEFAULT_WEIGHTS", "activation_status", "blend_verdict_stability",
           "compute", "fitness_terms", "challenge_gate"]


def challenge_gate(metrics: Mapping[str, Any], *, min_supported: int = 2,
                   min_repair_rate: float = 0.5,
                   min_anchor_gain: float = 0.0,
                   gain_key: str = "mean_anchor_point_gain") -> Dict[str, Any]:
    """Apply the §2.3 per-outer challenge gate without scoring it."""
    supported = int(metrics.get("supported_challenges") or 0)
    if supported < int(min_supported):
        return {"status": "insufficient_evidence", "passed": False}
    counts = metrics.get("state_counts") or {}
    regression = int(counts.get(adjudication.REGRESSION_AFTER_FALSE_CHALLENGE, 0) or 0)
    gain = metrics.get(gain_key)
    passed = bool(
        float(metrics.get("repair_rate_smoothed") or 0.0) > float(min_repair_rate)
        and isinstance(gain, (int, float))
        and float(gain) >= float(min_anchor_gain)
        and regression == 0)
    return {"status": "passed" if passed else "failed", "passed": passed,
            "false_challenge_regression": regression}
