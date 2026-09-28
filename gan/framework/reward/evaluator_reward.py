"""Evaluator feedback as TEXT (v2).

Design (agreed):
- The evaluator finds problems benchmark-only scoring cannot see.
- The planner's response ("accepted?/reason") and the evaluator's own fix verdict
  are organised as a **qualitative label** (the old 2x2), NOT a weighted numeric
  reward.
- The evaluator receives a **text digest** of what happened to its issues, and
  self-improves by narrative reflection over those digests. Blind-score
  calibration is also expressed as text, not a reward term.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Qualitative labels (diagnostic only; no numeric weights)
CELL_ACCEPTED_FIXED = "accepted_fixed"
CELL_ACCEPTED_UNFIXED = "accepted_unfixed"
CELL_REJECTED_WITH_FEEDBACK = "rejected_with_feedback"
CELL_REJECTED_NO_FEEDBACK = "rejected_no_feedback"
CELL_UNJUDGED = "unjudged"          # accepted, but no judge_fix verdict was given


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
    # B13 (batch 8): the structured stance (acted/acted_differently/out_of_scope/
    # disputed/deferred). NOTE: `feedback` is the planner's rationale — it is an
    # audit-only field and is dropped by summary.project_responses_for_evaluator
    # BEFORE the digest builder runs; it must never be rendered to the evaluator.
    feedback: Optional[str] = None
    response_kind: Optional[str] = None


@dataclass
class FixVerdict:
    """Whether an issue was actually fixed/improved (judged from trajectory/code)."""
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
    evidence: str = ""
    judged: bool = True               # False when no fix verdict was given

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "accepted": self.accepted,
            "has_feedback": self.has_feedback,
            "fixed": self.fixed,
            "cell": self.cell,
            "evidence": self.evidence,
            "judged": self.judged,
        }


# ---------------------------------------------------------------------------
# dict <-> dataclass converters
# ---------------------------------------------------------------------------
def issues_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[EvaluatorIssue]:
    return [EvaluatorIssue(**{k: v for k, v in it.items() if k in EvaluatorIssue.__dataclass_fields__})
            for it in (items or [])]


def responses_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[PlannerResponse]:
    return [PlannerResponse(**{k: v for k, v in it.items() if k in PlannerResponse.__dataclass_fields__})
            for it in (items or [])]


def verdicts_from_dicts(items: Optional[List[Dict[str, Any]]]) -> List[FixVerdict]:
    return [FixVerdict(**{k: v for k, v in it.items() if k in FixVerdict.__dataclass_fields__})
            for it in (items or [])]


# ---------------------------------------------------------------------------
# Qualitative classification (the old 2x2, now label-only)
# ---------------------------------------------------------------------------
def classify_issue(
    issue: EvaluatorIssue,
    response: Optional[PlannerResponse],
    verdict: Optional[FixVerdict],
) -> IssueOutcome:
    accepted = bool(response.accepted) if response is not None else False
    has_feedback = bool(
        response is not None and response.feedback and str(response.feedback).strip()
    )
    judged = verdict is not None
    fixed = bool(verdict.fixed) if judged else False

    if accepted:
        if not judged:
            # tri-state: accepted but the evaluator never called judge_fix -> UNJUDGED.
            # Never silently downgrade to accepted_unfixed (that would be an untrue
            # claim that the planner failed to fix it).
            cell = CELL_UNJUDGED
        else:
            cell = CELL_ACCEPTED_FIXED if fixed else CELL_ACCEPTED_UNFIXED
    else:
        cell = CELL_REJECTED_WITH_FEEDBACK if has_feedback else CELL_REJECTED_NO_FEEDBACK

    return IssueOutcome(
        issue_id=issue.issue_id,
        accepted=accepted,
        has_feedback=has_feedback,
        fixed=fixed,
        cell=cell,
        evidence=(verdict.evidence if verdict is not None else ""),
        judged=judged,
    )


def run_check_step(
    issues: List[EvaluatorIssue],
    responses: Optional[List[PlannerResponse]] = None,
    verdicts: Optional[List[FixVerdict]] = None,
) -> List[IssueOutcome]:
    """Resolve acceptance (planner response) + fix (evaluator verdict) per issue.

    A missing planner response is treated as "silently ignored"
    (``rejected_no_feedback``).
    """
    by_response = {r.issue_id: r for r in (responses or [])}
    by_verdict = {v.issue_id: v for v in (verdicts or [])}
    return [
        classify_issue(issue, by_response.get(issue.issue_id), by_verdict.get(issue.issue_id))
        for issue in issues
    ]


# ---------------------------------------------------------------------------
# Text digest (what the evaluator receives / reflects on)
# ---------------------------------------------------------------------------
_LABEL_TEXT = {
    CELL_ACCEPTED_FIXED: "accepted and actually fixed",
    CELL_ACCEPTED_UNFIXED: "accepted but NOT fixed",
    CELL_REJECTED_WITH_FEEDBACK: "rejected by planner with a reason",
    CELL_REJECTED_NO_FEEDBACK: "silently ignored by planner",
    CELL_UNJUDGED: "accepted but NOT judged this round (no judge_fix given)",
}


def build_feedback_digest(
    issues: Optional[List[Dict[str, Any]]] = None,
    responses: Optional[List[Dict[str, Any]]] = None,
    verdicts: Optional[List[Dict[str, Any]]] = None,
    predicted_score: Optional[float] = None,
    benchmark_score: Optional[float] = None,
    diff_summary: Optional[Dict[str, Any]] = None,
    patch_outcome: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a plain-text digest of what happened to the evaluator's issues.

    No scores/weights are attached to outcomes; the blind-vs-actual calibration
    is also stated textually.

    B13 (batch 8): ``responses`` must arrive ALREADY PROJECTED through
    ``summary.project_responses_for_evaluator`` — the free-text planner
    rationale is dropped at the channel boundary, so this builder receives and
    renders only structured facts (accepted / stance). ``patch_outcome`` is the
    session-level code-patch result ({"rejected", "attempts"}); a rejected patch
    is rendered as a tail line because it discounts every "acted" stance above.
    """
    lines: List[str] = ["# Feedback digest for your previous issues"]

    if predicted_score is not None or benchmark_score is not None:
        p = "?" if predicted_score is None else predicted_score
        b = "?" if benchmark_score is None else benchmark_score
        if isinstance(predicted_score, (int, float)) and isinstance(benchmark_score, (int, float)):
            delta = predicted_score - benchmark_score
            lines.append(
                f"- Blind prediction vs actual: you predicted {p}, actual was {b} "
                f"(delta {delta:+.3f}). Reflect on the direction/magnitude of your bias."
            )
        else:
            lines.append(f"- Blind prediction vs actual: predicted {p}, actual {b}.")

    i_list = issues_from_dicts(issues)
    outcomes = run_check_step(i_list, responses_from_dicts(responses), verdicts_from_dicts(verdicts))
    if not outcomes:
        lines.append("- (no issues were raised in that round)")
    else:
        # B13/G3: a planner may respond to the same issue more than once
        # (a composite stance); render every response instead of collapsing to
        # the first.
        responses_by_id: Dict[str, List[Dict[str, Any]]] = {}
        for r in (responses or []):
            if isinstance(r, dict):
                responses_by_id.setdefault(str(r.get("issue_id")), []).append(r)
        issues_by_id = {it.get("issue_id"): it for it in (issues or []) if isinstance(it, dict)}
        for o in outcomes:
            it = issues_by_id.get(o.issue_id, {})
            resp_list = responses_by_id.get(o.issue_id, [])
            lines.append(f"- issue {o.issue_id}: {it.get('description', '')}".rstrip())
            lines.append(f"    outcome: {_LABEL_TEXT.get(o.cell, o.cell)}")
            if resp_list:
                for resp in resp_list:
                    kind = str(resp.get("response_kind") or "unspecified")
                    line = f"    planner: accepted={bool(resp.get('accepted'))}, stance: {kind}"
                    # G2: name the contradiction explicitly — the planner claims
                    # to have acted but the fix verdict says otherwise. This is
                    # the strongest self-calibration signal in the 2x2 grid.
                    if (o.cell == CELL_ACCEPTED_UNFIXED
                            and kind in ("acted", "acted_differently")):
                        line += (" — claimed handled but not verified; check whether "
                                 "your issue is actually solvable")
                    lines.append(line)
            else:
                lines.append("    planner: (no response)")
            if o.evidence:
                lines.append(f"    your evidence: {o.evidence}")

    if diff_summary:
        ops = [o.get("op") for o in (diff_summary.get("ops") or [])]
        files = diff_summary.get("files") or []
        lines.append(f"- planner changes (sanitized): ops={ops or '[]'}, files={files or '[]'}")

    po = patch_outcome or {}
    if po.get("rejected"):
        # G1: session-level fact — a rejected patch discounts "acted" stances
        attempts = po.get("attempts", 0)
        lines.append(
            f"- NOTE: the planner's code patch was REJECTED this round "
            f"({attempts} attempt(s)); 'acted'/'acted_differently' stances above may "
            f"not have materialized in the executed agent."
        )

    lines.append(
        "- Reflect: were your issues valid and useful? Did you over/under-claim? "
        "Refine your eval points / criteria accordingly."
    )
    return "\n".join(lines)
