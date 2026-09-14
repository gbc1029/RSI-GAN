"""Evaluator outer-loop reward (v1.2).

Design
------
The evaluator exists to find problems that benchmark-only scoring cannot see
(process quality, reward hacking, detail defects, overfitting). Its own reward
therefore must NOT be "agree with the benchmark score", but measure the
*accuracy and usefulness* of its judgement. It has three parts:

1. Acceptance / Fix 2x2 matrix
   - "accepted"  is decided by the *check step* from the planner's response.
   - "fixed"     is judged by the evaluator from the trajectory / code
                 (NOT merely "was there any change").
   Matrix:
       accepted   & fixed      -> accepted_fixed       (+2.0)
       accepted   & not fixed  -> accepted_unfixed     (+1.0)
       rejected   & feedback   -> rejected_with_feedback (-1.0)  # planner rebutted with reason
       rejected   & no feedback-> rejected_no_feedback (+2.0)    # planner silently ignored -> evaluator vindicated

2. Blind-score calibration
   - In each inner round the evaluator first predicts the score *without*
     seeing the benchmark objective score, then the benchmark score is
     revealed. Calibration reward = -error_scale * |predicted - actual|.

3. Cheat-detection false-positive penalty (reward_hacking / rule_violation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_MATRIX: Dict[str, float] = {
    "accepted_fixed": 2.0,
    "accepted_unfixed": 1.0,
    "rejected_with_feedback": -1.0,
    "rejected_no_feedback": 2.0,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class EvaluatorIssue:
    issue_id: str
    description: str
    severity: str = "medium"          # low | medium | high
    evidence: str = ""
    suggested_fix: str = ""


@dataclass
class PlannerResponse:
    issue_id: str
    accepted: bool
    feedback: Optional[str] = None


@dataclass
class FixVerdict:
    """Judgement of whether an issue was actually fixed/improved.

    ``judged_by`` should be "evaluator" (from trajectory/code), not a mere
    "there was a diff" flag.
    """
    issue_id: str
    fixed: bool
    evidence: str = ""
    judged_by: str = "evaluator"


@dataclass
class IssueOutcome:
    issue_id: str
    accepted: bool
    has_feedback: bool
    fixed: bool
    cell: str
    reward: float
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "accepted": self.accepted,
            "has_feedback": self.has_feedback,
            "fixed": self.fixed,
            "cell": self.cell,
            "reward": self.reward,
            "evidence": self.evidence,
        }


@dataclass
class CalibrationRecord:
    predicted_score: float
    benchmark_score: float

    @property
    def error(self) -> float:
        return abs(float(self.predicted_score) - float(self.benchmark_score))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "predicted_score": self.predicted_score,
            "benchmark_score": self.benchmark_score,
            "error": self.error,
        }


@dataclass
class EvaluatorReward:
    issue_reward: float
    calibration_reward: float
    penalty_reward: float
    total: float
    outcomes: List[IssueOutcome] = field(default_factory=list)
    calibration: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_reward": self.issue_reward,
            "calibration_reward": self.calibration_reward,
            "penalty_reward": self.penalty_reward,
            "total": self.total,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "calibration": self.calibration,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# dict <-> dataclass converters (loop works with plain dicts)
# ---------------------------------------------------------------------------
def issues_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[EvaluatorIssue]:
    out = []
    for it in items or []:
        out.append(EvaluatorIssue(**{k: v for k, v in it.items() if k in EvaluatorIssue.__dataclass_fields__}))
    return out


def responses_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[PlannerResponse]:
    out = []
    for it in items or []:
        out.append(PlannerResponse(**{k: v for k, v in it.items() if k in PlannerResponse.__dataclass_fields__}))
    return out


def verdicts_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[FixVerdict]:
    out = []
    for it in items or []:
        out.append(FixVerdict(**{k: v for k, v in it.items() if k in FixVerdict.__dataclass_fields__}))
    return out


# ---------------------------------------------------------------------------
# 2x2 matrix classification
# ---------------------------------------------------------------------------
def classify_issue(
    issue: EvaluatorIssue,
    response: Optional[PlannerResponse],
    verdict: Optional[FixVerdict],
    matrix: Optional[Dict[str, float]] = None,
) -> IssueOutcome:
    matrix = matrix or DEFAULT_MATRIX
    accepted = bool(response.accepted) if response is not None else False
    has_feedback = bool(
        response is not None and response.feedback and str(response.feedback).strip()
    )
    fixed = bool(verdict.fixed) if verdict is not None else False

    if accepted:
        cell = "accepted_fixed" if fixed else "accepted_unfixed"
    else:
        cell = "rejected_with_feedback" if has_feedback else "rejected_no_feedback"

    return IssueOutcome(
        issue_id=issue.issue_id,
        accepted=accepted,
        has_feedback=has_feedback,
        fixed=fixed,
        cell=cell,
        reward=float(matrix.get(cell, 0.0)),
        evidence=(verdict.evidence if verdict is not None else ""),
    )


# ---------------------------------------------------------------------------
# Check step (runs inside the inner loop)
# ---------------------------------------------------------------------------
def run_check_step(
    issues: List[EvaluatorIssue],
    responses: Optional[List[PlannerResponse]] = None,
    verdicts: Optional[List[FixVerdict]] = None,
    matrix: Optional[Dict[str, float]] = None,
) -> List[IssueOutcome]:
    """Resolve acceptance (from planner response) + fix (from evaluator verdict).

    A missing planner response is treated as ``accepted=False, no feedback``
    (i.e. silently ignored -> ``rejected_no_feedback``), which the check step
    surfaces back to the evaluator.
    """
    by_response = {r.issue_id: r for r in (responses or [])}
    by_verdict = {v.issue_id: v for v in (verdicts or [])}
    outcomes: List[IssueOutcome] = []
    for issue in issues:
        outcomes.append(
            classify_issue(
                issue,
                by_response.get(issue.issue_id),
                by_verdict.get(issue.issue_id),
                matrix=matrix,
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def compute_calibration_reward(
    predicted_score: float,
    benchmark_score: float,
    error_scale: float = 1.0,
) -> float:
    return -float(error_scale) * abs(float(predicted_score) - float(benchmark_score))


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------
def compute_evaluator_reward(
    issues: List[EvaluatorIssue],
    responses: Optional[List[PlannerResponse]] = None,
    verdicts: Optional[List[FixVerdict]] = None,
    predicted_score: Optional[float] = None,
    benchmark_score: Optional[float] = None,
    matrix: Optional[Dict[str, float]] = None,
    calibration_cfg: Optional[Dict[str, Any]] = None,
    penalties: Optional[Dict[str, Any]] = None,
) -> EvaluatorReward:
    """Compute the evaluator's outer-loop reward for one inner round."""
    matrix = matrix or DEFAULT_MATRIX
    calibration_cfg = calibration_cfg or {}
    penalties = penalties or {}

    outcomes = run_check_step(issues, responses, verdicts, matrix=matrix)
    issue_reward = sum(o.reward for o in outcomes)

    calibration_reward = 0.0
    calibration_dict: Optional[Dict[str, Any]] = None
    if (
        calibration_cfg.get("enabled", True)
        and predicted_score is not None
        and benchmark_score is not None
    ):
        rec = CalibrationRecord(predicted_score, benchmark_score)
        calibration_reward = compute_calibration_reward(
            predicted_score,
            benchmark_score,
            error_scale=float(calibration_cfg.get("error_scale", 1.0)),
        ) * float(calibration_cfg.get("weight", 1.0))
        calibration_dict = rec.to_dict()

    penalty_reward = 0.0
    if penalties.get("reward_hacking_false_positive"):
        penalty_reward += float(penalties.get("reward_hacking_false_positive", 0.0))
    if penalties.get("rule_violation_false_positive"):
        penalty_reward += float(penalties.get("rule_violation_false_positive", 0.0))

    notes: List[str] = []
    cells = {o.cell for o in outcomes}
    if "rejected_no_feedback" in cells:
        notes.append(
            "check step found issue(s) silently ignored by planner (vindicates evaluator)"
        )
    if "rejected_with_feedback" in cells:
        notes.append("planner rebutted with reason (negative signal for evaluator)")

    return EvaluatorReward(
        issue_reward=issue_reward,
        calibration_reward=calibration_reward,
        penalty_reward=penalty_reward,
        total=issue_reward + calibration_reward + penalty_reward,
        outcomes=outcomes,
        calibration=calibration_dict,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Feedback rendering (shown to the evaluator on the next inner round)
# ---------------------------------------------------------------------------
def render_feedback(outcomes: List[IssueOutcome]) -> Dict[str, Any]:
    """Render the check-step result into the feedback the evaluator sees next round."""
    return {
        "schema_version": "v1",
        "issues": [
            {
                "issue_id": o.issue_id,
                "cell": o.cell,
                "accepted": o.accepted,
                "fixed": o.fixed,
                "reward": o.reward,
                "evidence": o.evidence,
            }
            for o in outcomes
        ],
    }
