"""All context variables for the GAN framework (single place).

Tools are called by the LLM tool loop via ``tool_function(**tool_input)`` with no
explicit context argument, so per-session state travels via contextvars:

- ``PlanContext``   - planner's planning session (records + issue responses)
- ``EvalContext``   - evaluator's evaluation session (issues/verdicts/scores)
- ``DesignContext`` - the *target* design config that design operators edit
- ``AccessContext`` - source-access session (broker + role + node + seat)
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

    def add_response(self, issue_id: str, accepted: bool, feedback: str = "",
                     response_kind: str = "unspecified") -> None:
        # Responses carry the structured stance; `feedback` stays
        # for the session record only — the evaluator-facing projection drops it.
        self.responses.append({"issue_id": issue_id, "accepted": bool(accepted),
                               "feedback": feedback or "",
                               "response_kind": str(response_kind)})


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
    # `weaknesses` is the structured comment carrier of the `trajectory_quality`
    # eval point (an audit surface).
    weaknesses: List[str] = field(default_factory=list)
    # cost-budget state of this session, aggregated across all of its runs
    # (blind + reveal). Mirrors the plan/self receipts' budget flags.
    token_budget_exhausted: bool = False
    truncated: bool = False

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
        """Append one structured record; the framework stamps ``target_role``.

        ``target_role`` is the design this session EDITS
        (``self.role``) -- not who edits it. It is written AFTER ``detail`` so a
        caller cannot shadow it, and it is framework-owned: no design operator can
        write ``ctx.role``. Stamping it here rather than at each call site makes it
        impossible to forget (every operator, including future ones, carries it)
        and impossible to spoof, while keeping the label tied to the same field
        that already decides which design file is written/validated -- so a wrong
        label would imply a wrong edit, which the registry resolution and the
        persist path already police.
        """
        self.records.append({"op": op, **detail, "target_role": self.role})


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
    # The session seat controls which write roots this role may use.  It is
    # explicit because role-wide access is too broad for plan/evaluate/self_improve,
    # and it is REQUIRED + validated below: role/seat are supplied by framework code
    # (never by the agent), so a missing or misspelled seat is a bug that must
    # surface loudly instead of silently widening access to the role's union.
    seat: str
    # Explicit set of task generations whose trajectory this session may read
    # (resolved by the loop; avoids relying on the node already being in the tree).
    trajectory_genids: List[Any] = field(default_factory=list)
    # The session's effective ``cost.cm_mode`` (docs/10 §11.5): governs ONLY
    # the presentation of read_trajectory/read_session_trajectory (structural
    # index vs full blob). Stamped by the loop from the session owner's OWN
    # design; it never widens the visible set — trajectory_genids above and
    # redaction stay the sole permission surface (选择只能缩小).
    session_cm_mode: Optional[str] = None
    # The set of outers whose OWN session files this seat may read (the
    # read_session_trajectory face). Declared by the framework exactly like
    # ``trajectory_genids`` above — never provided by the agent. ``None``
    # keeps the legacy behavior (current outer only, derived from the node
    # id); the self seat declares {O, O-1}. Presentation (``session_cm_mode``)
    # never widens this set, and redaction stays the sole content filter.
    session_outers: Optional[frozenset] = None
    # fix3 sandbox lifecycle flag: set False by the framework's
    # begin/end_session bracket (access.py); tools and the workspace face
    # consult it to refuse work on a closed session. Framework-owned,
    # never agent-provided.
    active: bool = True

    def __post_init__(self) -> None:
        from gan.framework.frozen import assert_seat
        self.seat = assert_seat(self.role, self.seat)


def set_access_context(broker, role: str, node_id: Any, trajectory_genids=None, *,
                       seat: str, session_cm_mode: Optional[str] = None,
                       session_outers=None):
    return _ACCESS_CTX.set(AccessContext(broker, role, node_id,
                                          seat=seat,
                                          trajectory_genids=list(trajectory_genids or []),
                                          session_cm_mode=session_cm_mode,
                                          session_outers=(frozenset(session_outers)
                                                          if session_outers is not None
                                                          else None)))


def get_access_context() -> Optional[AccessContext]:
    value = _ACCESS_CTX.get()
    return value if value is not None and value.active else None


def reset_access_context(token) -> None:
    value = _ACCESS_CTX.get()
    if value is not None:
        value.active = False
    _ACCESS_CTX.reset(token)


def illegal_seat_error(actx: Any, tool: str) -> Optional[str]:
    """Return an error string when ``actx`` has no usable session seat, else None.

    The tool-layer counterpart of ``frozen.assert_seat``: role/seat are set by
    framework code, so a missing/illegal seat is a framework bug.  Tools must not
    raise (that would abort the agent session), so they return this string and
    refuse -- never guessing a (necessarily wider) write surface.  A best-effort
    audit event is recorded so the bug is visible.
    """
    from gan.framework.frozen import legal_seats
    role = getattr(actx, "role", "") if actx is not None else ""
    seat = getattr(actx, "seat", "") if actx is not None else ""
    if str(seat or "") in legal_seats(role):
        return None
    broker = getattr(actx, "broker", None) if actx is not None else None
    if broker is not None:
        try:
            broker.log_event({"type": "illegal_session_seat", "tool": tool,
                              "role": role, "seat": seat})
        except Exception:  # audit must never break the tool
            pass
    return (f"Error: illegal session seat {seat!r} for role {role!r} "
            f"(framework bug; legal: {legal_seats(role) or 'unknown role'})")


def session_overlay_root():
    """The access session's workspace root when it carries a ``gan/`` overlay.

    A session edits a **workspace copy** of the paths it granted; the deep tools
    scan that copy workspace-first, so selection-time validation must see
    the same effective registry or the read side and the write side disagree.
    Returns the workspace root (``broker.src_dir``) when it exists and contains a
    ``gan/`` directory, else ``None`` -- callers pass it to
    ``load_registry_for_role(overlay_root=...)``.

    Every role's design may use the overlay: the task design is healed right
    before persist via ``task_execution.heal_design_slots``; role self-designs
    get the same treatment at the successful patch-exit heal, with the
    exhausted-exit healing still backlog (docs/7 section 6.1).
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


def ablation_flags() -> Dict[str, bool]:
    """Read the run's B-group ablation flags from the access context's broker.

    Used by the DESIGN tool layer to enforce ``disable_shallow`` (B4: freeze the
    whole shallow surface) and ``shallow_prompt_only`` (B3: only prompt edits).
    ``deep_view_only`` (B2) and the full-deep freeze (B1/B3) are enforced at the
    broker grant layer, not here. Returns all-false when no access context /
    broker is present (framework plumbing, tests) -- never raise.
    """
    actx = get_access_context()
    broker = getattr(actx, "broker", None) if actx is not None else None
    return {
        "disable_shallow": bool(getattr(broker, "disable_shallow", False)),
        "shallow_prompt_only": bool(getattr(broker, "shallow_prompt_only", False)),
        "deep_view_only": bool(getattr(broker, "deep_view_only", False)),
    }


def shallow_op_error(op_name: str, key: Optional[str] = None) -> Optional[str]:
    """Return an error string when a B-group ablation forbids this shallow op.

    ``disable_shallow`` (B4) freezes the WHOLE shallow surface; the design tools
    refuse before mutating. ``shallow_prompt_only`` (B3) allows only
    ``set_prompt`` / ``set_config(key=\"prompt\")`` and refuses everything else.
    Returns ``None`` when the op is allowed (no ablation, or a prompt op under
    B3). Read-only: never mutates state, never raises.
    """
    flags = ablation_flags()
    if flags["disable_shallow"]:
        return ("Error: shallow design operators are DISABLED this run "
                "(ablation.disable_shallow=true); only deep source access "
                "(request_source_access + edit_source) is available.")
    if flags["shallow_prompt_only"]:
        is_prompt = (op_name == "set_prompt") or (
            op_name == "set_config" and str(key or "") == "prompt")
        if not is_prompt:
            return ("Error: only prompt edits are allowed this run "
                    "(ablation.shallow_prompt_only=true); component selection "
                    "and other config keys are frozen.")
    return None
