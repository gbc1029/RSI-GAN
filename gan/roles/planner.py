"""Planner role: generate improvements for the task agent.

The planner edits the *task agent's design config* (shallow) via design
operators, responds to the evaluator's issues (work tool), and may escalate to a
gated deep source edit.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from gan.framework.access import reset_access_context, set_access_context
from gan.design import load_seed
from gan.framework.context import DesignContext, reset_design_context, set_design_context
from gan.framework.context import PlanContext, reset_plan_context, set_plan_context
from gan.framework import code_repo
from gan.framework.receipt import render_receipt
from gan.patch import build_patch_from_workspace, has_deep_write
from gan.roles.base_role import Role, warn_dropped_workspace_edits
from gan.summary import validate_feedback


_DENY_DEEP_NOTE = (
    "\n## Deep source access is DISABLED this run\n"
    "`source_access.deny_deep=true`: the deep surface is frozen for this run. "
    "Do NOT call `request_source_access`, `register_component`, "
    "`unregister_component` or `update_component` -- they will be refused. "
    "Proceed with shallow design/config operators only."
)

_SHALLOW_PROMPT_ONLY_NOTE = (
    "\n## Shallow evolution is LIMITED to prompt edits this run\n"
    "`ablation.shallow_prompt_only=true`: only `set_prompt` / "
    "`set_config(key=\"prompt\")` are allowed. Component selection "
    "(`select_component` / `deselect_component`) and deep source access are "
    "frozen. Do not retry them."
)

_DISABLE_SHALLOW_NOTE = (
    "\n## Shallow design operators are DISABLED this run\n"
    "`ablation.disable_shallow=true`: the design config is frozen. "
    "`set_prompt` / `set_config` / `select_component` / `deselect_component` "
    "will be refused. Improve the agent only via deep source access "
    "(`request_source_access` + `edit_source`)."
)


class Planner(Role):
    def __init__(self, model: str, output_dir: str, instance: Optional[str] = None, **kwargs):
        super().__init__("planner", model, output_dir, load_seed("planner"), instance=instance, **kwargs)

    # -- instruction -------------------------------------------------------
    def _plan_instruction(
        self,
        parent_summary: Dict[str, Any],
        parent_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
        parents: Optional[List[Dict[str, Any]]] = None,
        task_brief: Optional[str] = None,
        receipt: Optional[Dict[str, Any]] = None,
        parent_predicted_score: Optional[float] = None,
        parent_benchmark_score: Optional[float] = None,
        cost_statement: str = "",
        deny_deep: bool = False,
        shallow_prompt_only: bool = False,
        disable_shallow: bool = False,
    ) -> str:
        parts = ["Improve the task agent's DESIGN for the next generation."]
        if task_brief:
            parts.append(f"\n## Task brief (the task the agent must solve)\n{task_brief}")
        rr = render_receipt(receipt)
        if rr:
            parts.append("\n" + rr)
        parts.append(
            "\n## Shallow surface (design config)\n"
            "Edit the task design ONLY via design operators: `set_prompt`, `set_config`, "
            "`select_component`, `deselect_component`, `set_param`. Config keys are "
            "extensible: `add_config_key(key, value_type, consumer, description, "
            "[enum], [initial_value])` declares a NEW key for the task config -- the "
            "`consumer` must be a registered, valid task tool that actually reads the "
            "key, and the declaration lands in the SAME patch as that consumer (a "
            "rejected patch rolls back both). `add_config_value(key, value)` extends "
            "the value enum of a declared enum key. Undeclared or consumer-less keys "
            "are stripped by the key heal before persist. Adding a NEW component or "
            "component LOGIC still requires a DEEP change: call "
            "`request_source_access` (gated) then edit the granted copies with `edit_source`."
        )
        if deny_deep:
            parts.append(_DENY_DEEP_NOTE)
        if shallow_prompt_only:
            parts.append(_SHALLOW_PROMPT_ONLY_NOTE)
        if disable_shallow:
            parts.append(_DISABLE_SHALLOW_NOTE)
        if parents and len(parents) > 1:
            parts.append(
                f"\n## Multiple candidate parents (you may CROSSOVER their designs)\n```json\n"
                f"{json.dumps(parents, ensure_ascii=False, indent=2)[:4000]}\n```"
            )
        else:
            parts.append(
                f"\n## Parent node summary\n```json\n"
                f"{json.dumps(parent_summary, ensure_ascii=False, indent=2)[:4000]}\n```"
            )
        if evaluator_issues:
            parts.append(
                "\n## Evaluator issues to respond to\n"
                "You MUST call `respond_issue(issue_id, accepted, response_kind, feedback)` with a structured `response_kind` (acted / acted_differently / out_of_scope / disputed / deferred) for EACH issue below."
                "\n```json\n"
                f"{json.dumps(evaluator_issues, ensure_ascii=False, indent=2)[:4000]}\n```"
            )
        if parent_feedback and parent_feedback.get("diff_summary") is not None:
            parts.append(
                "\n## Diff summary (what changed last round, sanitized)\n"
                f"```json\n{json.dumps(parent_feedback.get('diff_summary'), ensure_ascii=False)[:1500]}\n```"
            )
        if parent_predicted_score is not None or parent_benchmark_score is not None:
            parts.append(
                "\n## Selected parent calibration\n"
                f"```json\n{json.dumps({'predicted_score': parent_predicted_score, 'benchmark_score': parent_benchmark_score}, ensure_ascii=False)}\n```"
            )
        if cost_statement:
            # Framework-measured cost facts (docs/10 §2.3): injected verbatim,
            # aggregate-only and length-capped at the source. Numbers, not
            # assertions — the statement carries no instruction to change
            # behavior beyond what the loop's selection already rewards.
            parts.append(f"\n{cost_statement}")
        return "\n".join(parts)

    # -- API ---------------------------------------------------------------
    def plan(
        self,
        parent_summary: Optional[Dict[str, Any]] = None,
        parents: Optional[List[Dict[str, Any]]] = None,
        parent_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
        config: Any = None,
        node_id: Any = None,
        broker: Any = None,
        task_brief: Optional[str] = None,
        trajectory_genids: Optional[List[Any]] = None,
        receipt: Optional[Dict[str, Any]] = None,
        parent_predicted_score: Optional[float] = None,
        parent_benchmark_score: Optional[float] = None,
        patch_retry_k: int = 2,
        max_tool_calls: int = 40,
        tool_repeat_limit: Optional[int] = None,
        cost_statement: str = "",
        response_contract: Optional[str] = None,
    ) -> Dict[str, Any]:
        if parent_feedback is not None and not validate_feedback(parent_feedback):
            raise ValueError("incompatible feedback schema_version")

        task_design = config if isinstance(config, dict) else {}
        plan_ctx = PlanContext(config=task_design, node_id=node_id, output_dir=self.output_dir)
        # F4/C1: the framework's output contract rides the design context so
        # the AUTHOR sees it at the point of rewriting the task prompt
        # (soft constraint: known to exist; not a set_config-writable key).
        oc = ({"text": str(response_contract),
               "source": "framework; read-only, outside set_config keys"}
              if response_contract else None)
        design_ctx = DesignContext(role="task", config=task_design, node_id=node_id,
                                   output_contract=oc)
        tok_plan = set_plan_context(plan_ctx)
        tok_design = set_design_context(design_ctx)
        akey = self.access_key(node_id)
        tok_access = (set_access_context(broker, "planner", akey,
                                         trajectory_genids=trajectory_genids,
                                         seat="plan",
                                         session_cm_mode=getattr(self, "session_cm_mode", None))
                      if broker is not None else None)
        traj = self.session_trajectory(node_id)
        attempts = 0
        rejected = None
        exhausted = False
        # Receipt aggregation (§4.1): a session spans SEVERAL chat runs (main
        # + rejection retries); last_run_info only remembers the LAST one, so
        # a token stop / tool-cap stop in an early run was masked by a
        # successful later one. OR the flags across the session's runs.
        tb_exhausted_any = False
        truncated_any = False

        def _absorb_run_flags() -> None:
            nonlocal tb_exhausted_any, truncated_any
            _info = getattr(self, "last_run_info", {}) or {}
            tb_exhausted_any = tb_exhausted_any or bool(_info.get("token_budget_exhausted"))
            truncated_any = truncated_any or bool(_info.get("truncated"))

        try:
            hist = self.run(
                self._plan_instruction(
                    parent_summary or {}, parent_feedback, evaluator_issues,
                    parents, task_brief, receipt,
                    parent_predicted_score, parent_benchmark_score,
                    cost_statement=cost_statement,
                    deny_deep=bool(getattr(broker, "deny_deep", False))
                    if broker is not None else False,
                    shallow_prompt_only=bool(getattr(broker, "shallow_prompt_only", False))
                    if broker is not None else False,
                    disable_shallow=bool(getattr(broker, "disable_shallow", False))
                    if broker is not None else False),
                max_tool_calls=max_tool_calls, tool_repeat_limit=tool_repeat_limit,
                trajectory_file=traj)
            _absorb_run_flags()

            def _build_patch() -> str:
                if broker is None:
                    return ""
                if not has_deep_write(design_ctx.records + plan_ctx.records):
                    return ""
                # No catch — a patch-BUILD failure must never masquerade as
                # a legitimate empty patch. Any failure (unreadable workspace /
                # repo file, a builder defect) propagates and fails the session;
                # the loop then marks the generation planner_failed (B-level)
                # instead of applying a semantically wrong diff.
                if hasattr(broker, "build_patch"):
                    return broker.build_patch("planner", akey, seat="plan")
                return build_patch_from_workspace(broker, "planner", akey)

            patch_str = _build_patch()
            last_hash = None
            for attempt in range(patch_retry_k + 1):
                patch_str = _build_patch()
                if not patch_str or not self.code_root:
                    rejected = None
                    break
                if broker is not None and hasattr(broker, "check_patch"):
                    ok, reason = broker.check_patch(patch_str, role="planner", seat="plan")
                else:
                    ok, reason = code_repo.check_patch(
                        self.code_root, patch_str, role="planner", seat="plan")
                if ok:
                    rejected = None
                    break
                rejected = reason
                h = hash(patch_str)
                if h == last_hash or attempt >= patch_retry_k:
                    exhausted = True
                    break
                last_hash = h
                hist = self.run(
                    "# Patch rejected\nYour previous code patch was rejected by validation:\n"
                    f"{reason}\nFix the problem, then stop. Do not repeat the same action.",
                    msg_history=hist, max_tool_calls=max_tool_calls,
                    tool_repeat_limit=tool_repeat_limit, trajectory_file=traj)
                _absorb_run_flags()
                attempts += 1
        finally:
            reset_plan_context(tok_plan)
            reset_design_context(tok_design)
            if tok_access is not None:
                reset_access_context(tok_access)

        records = design_ctx.records + plan_ctx.records
        warn_dropped_workspace_edits(broker, "planner", akey, records,
                                     self.output_dir, patch_str,
                                     exhausted=exhausted, rejected=rejected)
        return {
            "records": records,
            "responses": plan_ctx.responses,
            "config": task_design,
            "patch": "" if rejected else patch_str,
            "patch_proposed": patch_str,
            "patch_rejection": rejected,
            "attempts": attempts,
            "truncated": truncated_any,
            "budget_exhausted": exhausted,
            # Reserved cost keys (docs/10 §10): the per-session token soft stop
            # fired in THIS session (ANY of its runs — evidence: the session
            # trajectory's token_budget_stop rows). The receipt's budget.kind
            # rides this.
            "token_budget_exhausted": tb_exhausted_any,
        }

    def self_improve(self, recent: Optional[Dict[str, Any]] = None, broker: Any = None,
                     max_tool_calls: int = 30, trajectory_genids: Optional[List[Any]] = None,
                     patch_retry_k: int = 2, tool_repeat_limit: Optional[int] = None):
        from gan.framework.trajectory import outer_session_index
        sessions = outer_session_index(self.output_dir, self.outer, "planner")
        sess_line = json.dumps(sessions, ensure_ascii=False) if sessions else "[]"
        rr = render_receipt((recent or {}).get("receipt"))
        cost_stmt = str((recent or {}).get("cost_statement") or "")
        instruction = (
            "Improve YOURSELF (the planner's own design) using only design operators "
            "(`set_prompt`/`set_config`/`select_component`/`deselect_component`/`set_param`; "
            "deep changes need `request_source_access`). You may also extend your own "
            "config's keys: `add_config_key` declares a new planner-config key (its "
            "`consumer` must be a registered planner tool that reads it; declaration "
            "and consumer land in the same patch) and `add_config_value` extends a "
            "declared enum key. Goal: plan better task agents over "
            "the long run. "
            "Do not repeat the same tool call; when done, stop.\n"
            f"\n## Your session trajectories this outer (use read_session_trajectory)\n{sess_line}\n"
            + (("\n" + rr + "\n") if rr else "")
            + ((f"\n{cost_stmt}\n") if cost_stmt else "")
            + f"\nRecent outcomes: {json.dumps({k: v for k, v in (recent or {}).items() if k not in ('receipt', 'cost_statement')}, ensure_ascii=False)[:2000]}"
        )
        # B-group ablation notes (docs/11): inform the planner up front so it
        # does not burn tool calls on surfaces frozen for this run.
        if getattr(broker, "deny_deep", False):
            instruction += _DENY_DEEP_NOTE
        if getattr(broker, "shallow_prompt_only", False):
            instruction += _SHALLOW_PROMPT_ONLY_NOTE
        if getattr(broker, "disable_shallow", False):
            instruction += _DISABLE_SHALLOW_NOTE
        # Scaffold (design context, self-seat access, session run, patch retry
        # loop, result contract) is shared with the evaluator in
        # Role._self_improve_session; only the instruction above is
        # planner-specific (generic structured outcomes dump — costs ride
        # inside ``recent``, docs/10 §11.5 B5).
        return self._self_improve_session(
            instruction, broker=broker, max_tool_calls=max_tool_calls,
            trajectory_genids=trajectory_genids, patch_retry_k=patch_retry_k,
            tool_repeat_limit=tool_repeat_limit)
