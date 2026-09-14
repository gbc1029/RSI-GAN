"""Per-session contexts for planner operators and evaluator eval points.

Operators are called by the LLM tool loop via ``tool_function(**tool_input)``
with no explicit context argument, so state is carried in context variables set
by the role around a single ``chat_with_agent`` call.
"""
from __future__ import annotations

import contextvars
from typing import Any, Dict, List, Optional

_PLAN_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_plan_ctx", default=None)
_EVAL_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_eval_ctx", default=None)


# ---------------------------------------------------------------------------
# Planner / modification session
# ---------------------------------------------------------------------------
class PlanContext:
    def __init__(self, config: Any = None, node_id: Any = None, output_dir: Optional[str] = None):
        self.config = config
        self.node_id = node_id
        self.output_dir = output_dir
        self.records: List[Dict[str, Any]] = []
        # responses to the previous round's evaluator issues
        self.responses: List[Dict[str, Any]] = []

    def record(self, op: str, **detail: Any) -> None:
        self.records.append({"op": op, **detail})

    def add_response(self, issue_id: str, accepted: bool, feedback: str = "") -> None:
        self.responses.append({
            "issue_id": issue_id,
            "accepted": bool(accepted),
            "feedback": feedback or "",
        })


def set_plan_context(ctx: PlanContext):
    return _PLAN_CTX.set(ctx)


def get_plan_context() -> Optional[PlanContext]:
    return _PLAN_CTX.get()


def reset_plan_context(token) -> None:
    _PLAN_CTX.reset(token)


# ---------------------------------------------------------------------------
# Evaluator session
# ---------------------------------------------------------------------------
class EvalContext:
    def __init__(self) -> None:
        self.predicted_score: Optional[float] = None
        self.issues: List[Dict[str, Any]] = []
        self.fix_verdicts: List[Dict[str, Any]] = []   # judgement of prior-round issues
        self.penalties: Dict[str, Any] = {}
        self.eval_point_results: List[Dict[str, Any]] = []
        self.summary: str = ""
        self.weaknesses: List[str] = []
        self.suggestions: List[str] = []

    def add_issue(self, issue: Dict[str, Any]) -> None:
        self.issues.append(issue)

    def add_fix_verdict(self, issue_id: str, fixed: bool, evidence: str = "") -> None:
        self.fix_verdicts.append({
            "issue_id": issue_id,
            "fixed": bool(fixed),
            "evidence": evidence or "",
            "judged_by": "evaluator",
        })


def set_eval_context(ctx: EvalContext):
    return _EVAL_CTX.set(ctx)


def get_eval_context() -> Optional[EvalContext]:
    return _EVAL_CTX.get()


def reset_eval_context(token) -> None:
    _EVAL_CTX.reset(token)
