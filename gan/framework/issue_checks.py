"""Mechanical per-issue truth checkers (FRAMEWORK, frozen).

Why this module exists
----------------------
``verdict_stability`` used to compare the WHOLE outer's verdict vector against
ONE shared bit ("did this outer's mean score rise above the parent's?"). Two
consequences, both measured on the 2026-10 pilot:

* an honest evaluator scored 0.0 -- in A1 (``disable_planner``) nothing can fix
  an issue, so "not fixed" was the CORRECT answer for 19 of 20 verdicts, yet the
  score rose anyway (the task agent improved itself), so every ``false`` counted
  as a miss;
* the verdict was unverifiable in principle: the evaluator never made the claim
  the metric checked.

The replacement asks one question PER ISSUE: was the defect the evaluator named
actually gone in the child, and actually present in the parent? That truth comes
from here -- a pure function of data the framework already records on the node
(``report_summary``, ``toolset_report``, ``harness_rc``, ``cost_tokens``), never
from a model's opinion.

The binding is by ``issue_key`` (the closed vocabulary in
``gan/framework/issue_keys.py``), NOT by a predicate the evaluator writes. That
is deliberate: a model-authored predicate is a second thing the model can
optimise, and "which defect is this" is already fully determined by the key.

Three states, not two
---------------------
``PRESENT`` / ``ABSENT`` / ``UNKNOWN``. ``UNKNOWN`` means "this checker cannot
decide with the data at hand" and is EXCLUDED from the metric -- missing
evidence must never be scored as a miss (the same ruling that keeps a missing
``calibration_error`` neutral in ``F_e``).

``pair_truth`` returns ``None`` (not ``False``) whenever the pair is not
verifiable, and flags ``unsupported`` separately when the defect was ALREADY
absent in the parent -- i.e. the issue was raised about something that did not
exist. That case must not pay out a free "correct: not fixed" hit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

STATE_PRESENT = "present"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

# Defaults; the loop passes ``issues.check.*`` overrides through ``params``.
DEFAULT_PARAMS: Dict[str, float] = {
    "cost_ratio": 1.5,        # cost_tokens / token_budget_per_gen
    # Single predicted label's share of the run. Calibrated on the 2026-10 pilot:
    # the runs where the evaluator correctly reported leniency sat at 0.67-0.70
    # accept against a 0.50 ground-truth share, so 0.75 read a REAL bias as
    # "no defect" (and charged the evaluator with a fabricated issue).
    "bias_share": 0.65,
    "constant_share": 0.99,   # "the same label for every sample"
    # Minimum samples before a DISTRIBUTION claim is decidable. n=2 admits only
    # {0, 0.5, 1}, where an "accept share" or a "tilt reversal" is pure noise
    # (standard error ~0.35 vs ~0.09 at n=30).
    "min_samples": 10,
    # Same idea for `policy.constant-output`, with a lower bar: "every sample got
    # the same label" is EXACT about the recorded rows but still an inference
    # about the POLICY (a balanced 2-sample run is all-accept by chance 25% of the
    # time, 1.6% at n=6). Set to 0 to disable.
    "constant_min_samples": 6,
    "acc_tol": 0.05,          # overcorrection: "clearly worse than the parent"
}

_INFRA_REASONS = frozenset({
    "harness_failed", "no_report", "evaluator_failed", "planner_failed",
    "task_runner_failed", "task_runner_none",
})


@dataclass(frozen=True)
class Check:
    """One checker's verdict for one generation."""
    state: str
    metric: Any = None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.state != STATE_UNKNOWN


def _unknown(detail: str = "") -> Check:
    return Check(STATE_UNKNOWN, None, detail)


def _flag(value: bool, metric: Any, detail: str) -> Check:
    return Check(STATE_PRESENT if value else STATE_ABSENT, metric, detail)


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------
def generation_facts(node: Any) -> Dict[str, Any]:
    """The measurable slice of one generation's node (never raises)."""
    meta = getattr(node, "meta", None) or {}
    value = getattr(node, "value", None)
    return {
        "genid": getattr(node, "genid", None),
        "score": getattr(value, "score", None),
        "score_status": (getattr(value, "score_status", None)
                         or meta.get("score_status")),
        "cost_tokens": getattr(value, "cost_tokens", None),
        "cost_wallclock_s": getattr(value, "cost_wallclock_s", None),
        "report_summary": dict(meta.get("report_summary") or {}),
        "harness_rc": meta.get("harness_rc"),
        "invalid_reason": meta.get("invalid_reason"),
        "toolset_report": meta.get("toolset_report"),
        # Grant health of the generation's outer (stamped by the loop from the
        # broker's audit): True = at least one granted path was NOT readable in
        # the workspace, False = every grant verified readable, None = no grant
        # happened (or the audit predates the self-check) -> undecidable.
        "grant_unreadable": None,
    }


def _share(dist: Any, label: str) -> Optional[float]:
    if not isinstance(dist, dict):
        return None
    v = dist.get(label)
    return float(v) if isinstance(v, (int, float)) else None


def _single_label(dist: Any, tol: float = 0.99) -> Optional[bool]:
    """True when one label covers (almost) the whole distribution."""
    if not isinstance(dist, dict) or not dist:
        return None
    vals = [float(v) for v in dist.values() if isinstance(v, (int, float))]
    if not vals:
        return None
    return max(vals) >= tol


# ---------------------------------------------------------------------------
# checkers (key -> facts -> Check)
# ---------------------------------------------------------------------------
def _chk_cost_tokens(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    budget = facts.get("token_budget_per_gen")
    cost = facts.get("cost_tokens")
    if cost is None or not isinstance(budget, (int, float)) or budget <= 0:
        return _unknown("no cost_tokens or no token_budget_per_gen")
    ratio = float(cost) / float(budget)
    limit = float(p.get("cost_ratio", DEFAULT_PARAMS["cost_ratio"]))
    return _flag(ratio > limit, round(ratio, 3),
                 f"{int(cost)} tokens = {ratio:.2f}x the {int(budget)} budget "
                 f"(limit {limit})")


def _chk_cost_truncation(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    cov = facts["report_summary"].get("coverage")
    if not isinstance(cov, (int, float)):
        return _unknown("no coverage")
    return _flag(float(cov) < 1.0, round(float(cov), 3),
                 f"coverage={cov} (some samples produced no usable prediction)")


def _chk_harness_infra(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    rc = facts.get("harness_rc")
    reason = facts.get("invalid_reason")
    if rc is None and reason is None:
        return _unknown("no harness_rc / invalid_reason")
    bad = (isinstance(rc, int) and rc != 0) or (reason in _INFRA_REASONS)
    return _flag(bad, rc, f"harness_rc={rc} invalid_reason={reason}")


def _chk_contract_format(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    ok = facts["report_summary"].get("contract_ok")
    if not isinstance(ok, bool):
        return _unknown("no contract_ok")
    return _flag(not ok, ok, f"contract_ok={ok}")


def _n_valid(facts: Dict[str, Any]) -> Optional[int]:
    n = facts["report_summary"].get("sample_n_valid")
    return int(n) if isinstance(n, (int, float)) else None


def _too_few(facts: Dict[str, Any], p: Dict[str, float]) -> Optional[Check]:
    """A distribution claim needs enough samples to mean anything."""
    n = _n_valid(facts)
    need = int(p.get("min_samples", DEFAULT_PARAMS["min_samples"]))
    if n is None:
        return _unknown("no sample_n_valid")
    if n < need:
        return _unknown(f"only {n} valid sample(s) (< min_samples {need}): "
                        f"a distribution claim is not decidable")
    return None


def _bias(facts: Dict[str, Any], p: Dict[str, float], label: str) -> Check:
    too_few = _too_few(facts, p)
    if too_few is not None:
        return too_few
    dist = (facts["report_summary"].get("label_distribution") or {}).get("prediction")
    share = _share(dist, label)
    if share is None:
        return _unknown("no prediction label distribution")
    limit = float(p.get("bias_share", DEFAULT_PARAMS["bias_share"]))
    return _flag(share >= limit, round(share, 3),
                 f"{label} share={share:.2f} of {_n_valid(facts)} samples "
                 f"(limit {limit})")


def _chk_constant_output(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    ld = facts["report_summary"].get("label_distribution") or {}
    pred, gt = ld.get("prediction"), ld.get("ground_truth")
    tol = float(p.get("constant_share", DEFAULT_PARAMS["constant_share"]))
    flat_pred = _single_label(pred, tol)
    if flat_pred is None:
        return _unknown("no prediction distribution")
    if not flat_pred:
        return Check(STATE_ABSENT, None, "prediction labels vary")
    # A single prediction label over a single ground-truth label is not
    # degenerate behaviour -- the task itself has one answer.
    if _single_label(gt, tol):
        return Check(STATE_ABSENT, None, "flat prediction, but the labels are flat too")
    # Flatness is EXACT for the recorded rows, but "the policy always answers X"
    # is an inference: two identical predictions happen by chance half the time
    # on a balanced set. A lower bar than the distribution check (flatness needs
    # far less data than a share estimate) but still a bar.
    n = _n_valid(facts)
    need = int(p.get("constant_min_samples", DEFAULT_PARAMS["constant_min_samples"]))
    if need > 0:
        if n is None:
            return _unknown("no sample_n_valid")
        if n < need:
            return _unknown(f"only {n} valid sample(s) (< constant_min_samples "
                            f"{need}): 'always the same label' is not yet evidence "
                            f"of a collapsed policy")
    return Check(STATE_PRESENT, True, f"one predicted label for {n} sample(s)")


def _chk_grant_broken(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    """A granted source path was not readable in the role workspace.

    Reads the grant-time self-check the broker stamps on every grant record
    (``unreadable``). ``None`` means no grant happened for this outer, or the
    audit record predates the self-check -- undecidable, never "absent".
    """
    flag = facts.get("grant_unreadable")
    if flag is None:
        return _unknown("no grant self-check for this outer")
    return _flag(bool(flag), flag,
                 "a granted path was unreadable in the workspace" if flag
                 else "every granted path was readable")


# Pair checkers: the defect is only DEFINED for a (parent, child) pair, so a
# single generation cannot decide it. ``policy.overcorrection`` is the first --
# "the fix overshot and the decision rule inverted" is a statement about the
# CHANGE plus the resulting state, not about either state alone.
def _pair_overcorrection(parent: Dict[str, Any], child: Dict[str, Any],
                         p: Dict[str, float]) -> Check:
    """A fix that overshot: the decision rule inverted AND the step made it worse.

    Measured case (the evaluator's own words, 2026-10 pilot): "the round-0 fix
    added one-sided 'default to reject' heuristics ... on n=2 the outcome degraded
    from 0.5 to 0.0, consistent with both decisions being inverted".

    Two conditions, both required:

    (a) **INVERTED** -- the child's accuracy dropped BELOW the accuracy of always
        predicting the minority label: ``acc_C <= (1 - majority_share_C) - acc_tol``.
        This is the signature of a decision rule that is anti-correlated with the
        truth (an over-rejecting reviewer on an accept-heavy set, or the 0.5 ->
        0.0 collapse above where every single decision was wrong). A merely
        biased rule that still beats chance does NOT qualify -- that is
        `policy.accept-bias`, a different key.
    (b) **WORSE** -- ``acc_C < acc_P - acc_tol``: this transition, not some
        earlier one, is what broke it.

    Ground truth is read from EACH side's own `label_distribution.ground_truth`
    (the question draw may differ), and both sides need ``min_samples`` -- at
    n=2 the label shares are {0, 0.5, 1}, i.e. noise.
    """
    for side, f in (("parent", parent), ("child", child)):
        too_few = _too_few(f, p)
        if too_few is not None:
            return _unknown(f"{side}: {too_few.detail}")
    acc_tol = float(p.get("acc_tol", DEFAULT_PARAMS["acc_tol"]))

    def _acc(f):
        v = f["report_summary"].get("overall_accuracy")
        return float(v) if isinstance(v, (int, float)) else None

    def _majority(f):
        gt = ((f["report_summary"].get("label_distribution") or {})
              .get("ground_truth") or {})
        vals = [float(v) for v in gt.values() if isinstance(v, (int, float))]
        return max(vals) if vals else None

    acc_p, acc_c = _acc(parent), _acc(child)
    maj_c = _majority(child)
    if acc_p is None or acc_c is None or maj_c is None:
        return _unknown("no overall_accuracy / ground_truth distribution")
    inverted_limit = (1.0 - maj_c) - acc_tol
    inverted = acc_c < inverted_limit
    worse = acc_c < acc_p - acc_tol
    detail = (f"acc {acc_p:.3f} -> {acc_c:.3f} (tol {acc_tol}); "
              f"inverted needs < {inverted_limit:.3f} given majority_share "
              f"{maj_c:.2f}")
    return Check(STATE_PRESENT if (inverted and worse) else STATE_ABSENT,
                 {"inverted": inverted, "worse": worse}, detail)


_PAIR_CHECKERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any],
                                    Dict[str, float]], Check]] = {
    "policy.overcorrection": _pair_overcorrection,
}

_CHECKERS = {
    "cost.tokens": _chk_cost_tokens,
    "cost.truncation": _chk_cost_truncation,
    "harness.infra": _chk_harness_infra,
    "design.contract-format": _chk_contract_format,
    "access.grant-broken": _chk_grant_broken,
    "policy.accept-bias": lambda f, p: _bias(f, p, "accept"),
    "policy.reject-bias": lambda f, p: _bias(f, p, "reject"),
    "policy.constant-output": _chk_constant_output,
}


def has_checker(key: object) -> bool:
    k = str(key or "")
    return k in _CHECKERS or k in _PAIR_CHECKERS


def checked_keys() -> Tuple[str, ...]:
    return tuple(_CHECKERS) + tuple(_PAIR_CHECKERS)


def check(key: object, facts: Dict[str, Any],
          params: Optional[Dict[str, float]] = None) -> Check:
    """Evaluate one key against one generation's facts (``UNKNOWN`` if none).

    A pair checker cannot decide anything from one side, so calling it here
    returns ``UNKNOWN`` -- ``pair_truth`` is the only correct entry for it.
    """
    k = str(key or "")
    fn = _CHECKERS.get(k)
    if fn is None:
        if k in _PAIR_CHECKERS:
            return _unknown("pair-only checker: needs the parent AND child facts")
        return _unknown("no checker for this issue_key")
    try:
        return fn(facts or {}, {**DEFAULT_PARAMS, **(params or {})})
    except Exception as e:  # noqa: BLE001 -- a checker must never break a round
        return _unknown(f"checker error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# pairing (the truth the metric consumes)
# ---------------------------------------------------------------------------
def pair_truth(key: object, parent_facts: Optional[Dict[str, Any]],
               child_facts: Optional[Dict[str, Any]],
               params: Optional[Dict[str, float]] = None
               ) -> Tuple[Optional[bool], str, str, bool]:
    """``(truth_fixed, parent_state, child_state, unsupported)``.

    ``truth_fixed`` is ``None`` when the pair cannot be verified:
    no checker for the key, either side ``UNKNOWN``, or the defect was already
    absent in the parent (``unsupported`` -- the issue was raised about
    something that did not exist, so it must not pay out a free "not fixed").
    """
    if not has_checker(key):
        return None, STATE_UNKNOWN, STATE_UNKNOWN, False
    k = str(key or "")
    resolved = {**DEFAULT_PARAMS, **(params or {})}
    pair_fn = _PAIR_CHECKERS.get(k)
    if pair_fn is not None:
        try:
            res = pair_fn(parent_facts or {}, child_facts or {}, resolved)
        except Exception as e:  # noqa: BLE001
            return None, STATE_UNKNOWN, STATE_UNKNOWN, False
        if not res.known:
            return None, STATE_UNKNOWN, STATE_UNKNOWN, False
        # Pair checkers define the defect AS the transition, so a "present"
        # result already means "the child is in the bad state and the parent was
        # not": the issue is fixed exactly when the child is ABSENT.
        return (res.state == STATE_ABSENT), STATE_PRESENT, res.state, False
    before = check(k, parent_facts or {}, params)
    after = check(k, child_facts or {}, params)
    if not before.known or not after.known:
        return None, before.state, after.state, False
    if before.state == STATE_ABSENT:
        return None, before.state, after.state, True
    return (after.state == STATE_ABSENT), before.state, after.state, False
