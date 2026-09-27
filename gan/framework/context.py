"""All context variables for the GAN framework (single place).

Tools are called by the LLM tool loop via ``tool_function(**tool_input)`` with no
explicit context argument, so per-session state travels via contextvars:

- ``PlanContext``   - planner's planning session (records + issue responses)
- ``EvalContext``   - evaluator's evaluation session (issues/verdicts/scores)
- ``DesignContext`` - the *target* design config that design operators edit
- ``AccessContext`` - source-access session (broker + role + node)
"""
from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Planner session
# ---------------------------------------------------------------------------
_PLAN_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_plan_ctx", default=None)


@dataclass
class PlanContext:
    config: Any = None
    node_id: Any = None
    output_dir: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    responses: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, op: str, **detail: Any) -> None:
        self.records.append({"op": op, **detail})

    def add_response(self, issue_id: str, accepted: bool, feedback: str = "") -> None:
        self.responses.append({"issue_id": issue_id, "accepted": bool(accepted), "feedback": feedback or ""})


def set_plan_context(ctx: PlanContext):
    return _PLAN_CTX.set(ctx)


def get_plan_context() -> Optional[PlanContext]:
    return _PLAN_CTX.get()


def reset_plan_context(token) -> None:
    _PLAN_CTX.reset(token)


# ---------------------------------------------------------------------------
# Evaluator session
# ---------------------------------------------------------------------------
_EVAL_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_eval_ctx", default=None)


@dataclass
class EvalContext:
    predicted_score: Optional[float] = None
    issues: List[Dict[str, Any]] = field(default_factory=list)
    fix_verdicts: List[Dict[str, Any]] = field(default_factory=list)
    penalties: Dict[str, Any] = field(default_factory=dict)
    eval_point_results: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    weaknesses: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)

    def add_issue(self, issue: Dict[str, Any]) -> None:
        self.issues.append(issue)

    def add_fix_verdict(self, issue_id: str, fixed: bool, evidence: str = "") -> None:
        self.fix_verdicts.append({
            "issue_id": issue_id, "fixed": bool(fixed), "evidence": evidence or "",
            "judged_by": "evaluator",
        })


def set_eval_context(ctx: EvalContext):
    return _EVAL_CTX.set(ctx)


def get_eval_context() -> Optional[EvalContext]:
    return _EVAL_CTX.get()


def reset_eval_context(token) -> None:
    _EVAL_CTX.reset(token)


# ---------------------------------------------------------------------------
# Design session (target config for design operators)
# ---------------------------------------------------------------------------
_DESIGN_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_design_ctx", default=None)


@dataclass
class DesignContext:
    role: str                      # "task" | "planner" | "evaluator"
    config: Dict[str, Any]
    node_id: Any = None
    records: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, op: str, **detail: Any) -> None:
        self.records.append({"op": op, **detail})


def set_design_context(ctx: DesignContext):
    return _DESIGN_CTX.set(ctx)


def get_design_context() -> Optional[DesignContext]:
    return _DESIGN_CTX.get()


def reset_design_context(token) -> None:
    _DESIGN_CTX.reset(token)


# ---------------------------------------------------------------------------
# Source-access session
# ---------------------------------------------------------------------------
_ACCESS_CTX: contextvars.ContextVar = contextvars.ContextVar("gan_access_ctx", default=None)


@dataclass
class AccessContext:
    broker: Any
    role: str
    node_id: Any
    # Explicit set of task generations whose trajectory this session may read
    # (resolved by the loop; avoids relying on the node already being in the tree).
    trajectory_genids: List[Any] = field(default_factory=list)


def set_access_context(broker, role: str, node_id: Any, trajectory_genids=None):
    return _ACCESS_CTX.set(AccessContext(broker, role, node_id,
                                         list(trajectory_genids or [])))


def get_access_context() -> Optional[AccessContext]:
    return _ACCESS_CTX.get()


def reset_access_context(token) -> None:
    _ACCESS_CTX.reset(token)


def session_overlay_root():
    """The access session's workspace root when it carries a ``gan/`` overlay.

    A session edits a **workspace copy** of the paths it granted; the deep tools
    scan that copy workspace-first (S1/P3), so selection-time validation must see
    the same effective registry or the read side and the write side disagree.
    Returns the workspace root (``broker.src_dir``) when it exists and contains a
    ``gan/`` directory, else ``None`` -- callers pass it to
    ``load_registry_for_role(overlay_root=...)``.

    Callers decide WHICH design may use the overlay: the task design is healed
    against the committed tree before it is persisted
    (``task_execution.heal_design_slots``), so it may validate against the
    effective view; role self-designs are not healed yet and must keep the
    committed-only authority.
    """
    actx = get_access_context()
    if actx is None:
        return None
    broker = getattr(actx, "broker", None)
    if broker is None:
        return None
    try:
        src = broker.src_dir(actx.role, actx.node_id)
    except Exception:  # noqa: BLE001 -- a stub broker: no overlay, never a crash
        return None
    return src if (src and os.path.isdir(os.path.join(src, "gan"))) else None
