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
    # `process.dead-end` (the balrog/step-based analogue of a spinning policy).
    # Signal 1 -- `zero_displacement_share`: the episode never left its starting
    # neighbourhood. CALIBRATED on the 2026-11-11 six-generation A0 round (same
    # frozen plan), whose per-generation zero_displacement_share was
    #   gen0 (0.60) gens1-2 (0.70/0.90)   <- dead end
    #   gens3-4 (0.20/0.10)              <- moving, solved 6/10, 5/10
    #   gen5   (0.10)                    <- moved but wasted its steps
    # 0.5 sits between the two clusters with margin on both sides.
    #
    # Signal 2 was `obs_repeat_step_share >= 0.35` and it was WRONG IN SIGN
    # (REMOVED 2026-11-17, docs/20). It counts steps whose action changed nothing
    # observable, and the state key contains the FACING: a turning step always
    # changes it, so the spinning generation scored 5.4% while a healthy agent
    # that keeps trying and failing scored 36-53%. Measured over 31 archived
    # generations: it fired on 12/12 generations of the BEST arm and 0/8 of the
    # full arm. The replacement counts CYCLES instead -- the share of episodes
    # whose steps mostly revisit a (state, action) pair already tried earlier in
    # the same episode -- which a 4-step rotation hits on every lap (A0 gen8:
    # 70% of its episodes degenerate) and a genuine attempt never does (A1:
    # 0-20%).
    #
    # 0.40 is calibrated on TWO independent samples: the archived 3-arm round
    # (A1 max 20%, A0 gen4/gen8 70%, A2 gen9/gen10 60%) and the stage-2 fresh-plan
    # A1 run (3 generations, exactly 20% each). That is a 2x margin below the
    # worst dead generation and 2x above the healthy ceiling -- and the
    # calibration run used data the rule was NOT written on (docs/20 §8).
    "dead_end_share": 0.5,
    "dead_end_revisit_share": 0.4,
    # A single episode counts as cycle-degenerate when this share of its steps
    # revisit an already-tried (state, action) pair. Owned by the HARNESS
    # (`domains/balrog/utils.py DEGENERATE_REVISIT_SHARE`) and mirrored here for
    # the metric display only -- changing it here does NOT change the report.
    "degenerate_revisit_share": 0.8,
    # Traced episodes needed before a share claim is decidable (aborted episodes
    # carry no trace, so the denominator is smaller than the episode count).
    "behavior_min_episodes": 4,
    # `calibration.{under,over}-prediction` (2026-11-21). The evaluator's own
    # `predicted_score` against the realized score of the SAME generation.
    #
    # 0.15 = one episode of resolution (1/10 = 0.1 on the standard 10-episode
    # generation) plus margin. NOT yet calibrated against a real bias case: the
    # four archived runs predicted 0.55/0.60, 0.80/0.80, 0.40/0.40 and
    # 0.50/0.50, i.e. the maximum observed error is 0.05, five-fold inside the
    # line. Provisional by construction and recorded as such -- a case that fires
    # is what would calibrate it.
    "calibration_tol": 0.15,
    # `process.observability` (2026-11-21). The archived task transcript must
    # carry at least this many rows PER STEP: a step child writes its own
    # assistant/tool rows, so one step never lands as one row.
    #
    # Measured on three healthy runs (rows / steps_total):
    #   2.67, 2.02, 2.00 (run #8)  3.00 / 3.00 / 3.00 (run #7)  3.00 / 3.00 / 3.00 (run #6)
    # A floor of 1.0 therefore has a 2x margin on the healthy side, and the
    # historical failure it exists to catch (2026-11-08 `balrog_verify_1108a`:
    # 10 episodes / 98 step children archived as 10 rows, i.e. ~0.1) sits an
    # order of magnitude BELOW it.
    "observability_min_rows_per_step": 1.0,
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
        # The evaluator's own blind prediction for THIS generation, stamped on
        # the node by the loop before the verdicts are judged. `score` above is
        # the realized value, so the pair is {prediction, outcome} -- the only
        # facts `calibration.*` needs.
        "predicted_score": meta.get("predicted_score"),
        # Grant health of the generation's outer (stamped by the loop from the
        # broker's audit): True = at least one granted path was NOT readable in
        # the workspace, False = every grant verified readable, None = no grant
        # happened (or the audit predates the self-check) -> undecidable.
        "grant_unreadable": None,
        # Observability of the generation's own trace (stamped by the loop from
        # the archive + report): how many JSONL rows the evaluator can actually
        # read, against how many steps the run claims to have taken. None = the
        # archive or the report is absent -> `process.observability` undecidable.
        "trajectory_rows": None,
        "steps_total": None,
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


def _chk_dead_end(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    """The generation spent its step budget going nowhere.

    This is the checker ``process.dead-end`` never had, and its absence was
    MEASURED: on the 2026-11-11 six-generation round the evaluator raised the
    key in EVERY generation (6/6) and no generation ever carried a verdict,
    even after zero-displacement episodes fell from 9/10 to 1/10 -- i.e. the
    ledger kept charging an unfixable defect while the behaviour had already
    improved (``verdict_stability`` stayed UNKNOWN, so nothing caught it).

    Two shapes of "going nowhere", both read from the L1 ``behavior`` block:

    * ``zero_displacement_share`` — half the traced episodes visited at most two
      observable states (they never left their starting neighbourhood);
    * ``episodes_degenerate_revisit_share`` — half the traced episodes spent 80%
      of their steps re-trying a ``(state, action)`` pair the same episode had
      already tried, i.e. it is CYCLING (docs/20 §4.2). Deliberately a cycle
      detector, not a "wasted steps" counter: the latter was the previous second
      signal and had an inverted sign (a turn always changes the observable
      state, so the spinning generation scored 5.4% while the healthy arm scored
      36-53% — it fired on 12/12 generations of the best arm and 0/8 of the full
      arm).

    ``UNKNOWN`` without a behaviour block (a report from before L1, or a
    non-episode domain) or with too few TRACED episodes (aborted episodes carry
    no trace, so the denominator is smaller than the episode count). The
    thresholds are calibrated in ``DEFAULT_PARAMS`` against measured generations
    on TWO independent samples; they are not round numbers picked in advance.
    """
    beh = facts["report_summary"].get("behavior")
    if not isinstance(beh, dict) or not beh:
        return _unknown("no behavior block (pre-L1 report or a question domain)")
    n = beh.get("episodes")
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return _unknown("behavior block carries no traced-episode denominator")
    n = int(n)
    need = int(p.get("behavior_min_episodes", DEFAULT_PARAMS["behavior_min_episodes"]))
    if n < need:
        return _unknown(f"only {n} traced episode(s) (< behavior_min_episodes {need})")
    zs = beh.get("zero_displacement_share")
    rs = beh.get("obs_repeat_step_share")
    vs = beh.get("episodes_degenerate_revisit_share")
    zs = float(zs) if isinstance(zs, (int, float)) and not isinstance(zs, bool) else None
    rs = float(rs) if isinstance(rs, (int, float)) and not isinstance(rs, bool) else None
    vs = float(vs) if isinstance(vs, (int, float)) and not isinstance(vs, bool) else None
    if zs is None and vs is None:
        return _unknown("behaviour block carries neither zero_displacement nor "
                        "degenerate-revisit share (pre-docs/20 report?)")
    z_limit = float(p.get("dead_end_share", DEFAULT_PARAMS["dead_end_share"]))
    v_limit = float(p.get("dead_end_revisit_share",
                         DEFAULT_PARAMS["dead_end_revisit_share"]))
    # A signal that is ABSENT cannot fire AND cannot be read as "fixed": the
    # caller aggregates the pair, and None here means "no data", never ABSENT.
    fired = [name for name, val, limit in (
        ("zero_displacement", zs, z_limit),
        ("degenerate_revisit", vs, v_limit)) if val is not None and val >= limit]
    metric = {
        "zero_displacement_share": zs,
        "episodes_degenerate_revisit_share": vs,
        # REPORT ONLY (never a verdict input since 2026-11-17): the sign is
        # inverted -- see DEFAULT_PARAMS.
        "obs_repeat_step_share": rs,
        "revisit_step_share": beh.get("revisit_step_share"),
        "traced_episodes": n,
        "distinct_obs_states_mean": beh.get("distinct_obs_states_mean"),
    }
    return _flag(bool(fired), metric,
                 f"dead-end signals {fired or 'none'} out of "
                 f"[zero_displacement>={z_limit}, degenerate_revisit>={v_limit}]; "
                 f"zero_displacement_share={zs}, "
                 f"episodes_degenerate_revisit_share={vs}, "
                 f"traced={n}")


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


# ---------------------------------------------------------------------------
# CHECKERS ADDED 2026-11-21 (docs/21 §17): the three keys that had NONE
# ---------------------------------------------------------------------------
def _chk_calibration_under(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    """The evaluator's blind prediction was TOO LOW for this generation.

    Owner is the evaluator, so the "repair" this truth tracks is the evaluator's
    own next round: `pair_truth` asks "under-predicting in the parent -> not
    under-predicting in the child", which is exactly what `calibration_error`
    measures outer-wide. The per-ISSUE version exists so the ledger can hold a
    stance and a mechanical verdict for the claim the evaluator itself made.

    Both numbers come from the same generation: `predicted_score` (blind) and
    `score` (realized). A missing or failed score is UNKNOWN -- never "not
    mispredicted", which would pay a free hit to every unscored generation.
    """
    return _calibration(facts, p, direction="under")


def _chk_calibration_over(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    """The evaluator's blind prediction was TOO HIGH (the mirror image)."""
    return _calibration(facts, p, direction="over")


def _calibration(facts: Dict[str, Any], p: Dict[str, float], *,
                 direction: str) -> Check:
    pred = facts.get("predicted_score")
    score = facts.get("score")
    if not isinstance(pred, (int, float)) or isinstance(pred, bool) \
            or not isinstance(score, (int, float)) or isinstance(score, bool):
        return _unknown("needs both predicted_score and a realized score")
    if str(facts.get("score_status") or "") not in ("ok", "partial"):
        return _unknown(f"score_status={facts.get('score_status')!r}")
    tol = float(p.get("calibration_tol", DEFAULT_PARAMS["calibration_tol"]))
    diff = float(score) - float(pred)
    present = (diff > tol) if direction == "under" else ((-diff) > tol)
    return _flag(
        present,
        {"predicted": float(pred), "actual": float(score),
         "diff": diff, "tol": tol},
        f"predicted {float(pred):.3f} vs actual {float(score):.3f} "
        f"(diff {diff:+.3f}, tol {tol:.2f})",
    )


def _chk_observability(facts: Dict[str, Any], p: Dict[str, float]) -> Check:
    """The evaluator could NOT see the generation's own steps.

    This is the defect that blinded the evaluator for a whole round before anyone
    noticed (2026-11-08: the task agent truncates its transcript at construction,
    so a multi-step episode kept only its LAST step; 10 episodes / 98 step
    children archived as 10 rows). The framework-owned mitigation is the parent
    dropping such a reset, but nothing MEASURED the outcome until now.

    The rule is a ratio against the report's own step count, because a step child
    writes several rows (its assistant/tool turns), never one:
    ``rows < steps_total * observability_min_rows_per_step`` -> PRESENT.

    Both facts are stamped by the loop (archive + report are I/O). Missing either
    is UNKNOWN: a question-domain generation has no `steps_total`, and its
    per-question transcript cannot be judged by this line.
    """
    rows = facts.get("trajectory_rows")
    steps = facts.get("steps_total")
    if not isinstance(rows, int) or not isinstance(steps, (int, float)) or steps <= 0:
        return _unknown("needs the archived row count AND the report's steps_total")
    floor = float(p.get("observability_min_rows_per_step",
                        DEFAULT_PARAMS["observability_min_rows_per_step"]))
    ratio = float(rows) / float(steps)
    return _flag(ratio < floor, {"rows": int(rows), "steps": int(steps),
                                 "rows_per_step": ratio, "floor": floor},
                 f"{rows} rows for {int(steps)} steps ({ratio:.2f}/step, floor {floor:.2f})")


_PAIR_CHECKERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any],
                                    Dict[str, float]], Check]] = {
    "policy.overcorrection": _pair_overcorrection,
}

_CHECKERS = {
    "cost.tokens": _chk_cost_tokens,
    "cost.truncation": _chk_cost_truncation,
    "harness.infra": _chk_harness_infra,
    "design.contract-format": _chk_contract_format,
    "process.dead-end": _chk_dead_end,
    "process.observability": _chk_observability,
    "access.grant-broken": _chk_grant_broken,
    "calibration.under-prediction": _chk_calibration_under,
    "calibration.over-prediction": _chk_calibration_over,
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
