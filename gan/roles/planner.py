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
from gan.patch import build_patch_from_workspace
from gan.roles.base_role import Role
from gan.summary import validate_feedback


class Planner(Role):
    def __init__(self, model: str, output_dir: str, instance: Optional[str] = None, **kwargs):
        super().__init__("planner", model, output_dir, load_seed("planner"), instance=instance, **kwargs)

    # -- instruction -------------------------------------------------------
    def _plan_instruction(
        self,
        parent_summary: Dict[str, Any],
        last_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
        parents: Optional[List[Dict[str, Any]]] = None,
        task_brief: Optional[str] = None,
        receipt: Optional[Dict[str, Any]] = None,
        parent_predicted_score: Optional[float] = None,
        parent_benchmark_score: Optional[float] = None,
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
            "`select_component`, `deselect_component`, `set_param`. Adding a NEW config key, "
            "a NEW component, or component LOGIC requires a DEEP change: call "
            "`request_source_access` (gated) then edit the granted copies with `edit_source`."
        )
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
                "You MUST call `respond_issue(issue_id, accepted, feedback)` for EACH issue below."
                "\n```json\n"
                f"{json.dumps(evaluator_issues, ensure_ascii=False, indent=2)[:4000]}\n```"
            )
        if last_feedback and last_feedback.get("diff_summary") is not None:
            parts.append(
                "\n## Diff summary (what changed last round, sanitized)\n"
                f"```json\n{json.dumps(last_feedback.get('diff_summary'), ensure_ascii=False)[:1500]}\n```"
            )
        if parent_predicted_score is not None or parent_benchmark_score is not None:
            parts.append(
                "\n## Selected parent calibration\n"
                f"```json\n{json.dumps({'predicted_score': parent_predicted_score, 'benchmark_score': parent_benchmark_score}, ensure_ascii=False)}\n```"
            )
        return "\n".join(parts)

    # -- API ---------------------------------------------------------------
    def plan(
        self,
        parent_summary: Optional[Dict[str, Any]] = None,
        parents: Optional[List[Dict[str, Any]]] = None,
        last_feedback: Optional[Dict[str, Any]] = None,
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
    ) -> Dict[str, Any]:
        if last_feedback is not None and not validate_feedback(last_feedback):
            raise ValueError("incompatible feedback schema_version")

        task_design = config if isinstance(config, dict) else {}
        plan_ctx = PlanContext(config=task_design, node_id=node_id, output_dir=self.output_dir)
        design_ctx = DesignContext(role="task", config=task_design, node_id=node_id)
        tok_plan = set_plan_context(plan_ctx)
        tok_design = set_design_context(design_ctx)
        akey = self.access_key(node_id)
        tok_access = (set_access_context(broker, "planner", akey,
                                         trajectory_genids=trajectory_genids)
                      if broker is not None else None)
        traj = self.session_trajectory(node_id)
        attempts = 0
        rejected = None
        exhausted = False
        try:
            hist = self.run(
                self._plan_instruction(parent_summary or {}, last_feedback, evaluator_issues,
                                       parents, task_brief, receipt,
                                       parent_predicted_score, parent_benchmark_score),
                max_tool_calls=max_tool_calls, trajectory_file=traj)

            def _build_patch() -> str:
                if broker is None:
                    return ""
                if not any(r.get("op") == "code_edit"
                           or (r.get("op") == "request_source_access" and r.get("intent") == "modify")
                           for r in (design_ctx.records + plan_ctx.records)):
                    return ""
                # B6: no catch — a patch-BUILD failure must never masquerade as
                # a legitimate empty patch. Any failure (unreadable workspace /
                # repo file, a builder defect) propagates and fails the session;
                # the loop then marks the generation planner_failed (B-level)
                # instead of applying a semantically wrong diff.
                return build_patch_from_workspace(broker, "planner", akey)

            patch_str = _build_patch()
            last_hash = None
            for attempt in range(patch_retry_k + 1):
                patch_str = _build_patch()
                if not patch_str or not self.code_root:
                    rejected = None
                    break
                ok, reason = code_repo.check_patch(self.code_root, patch_str)
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
                    msg_history=hist, max_tool_calls=max_tool_calls, trajectory_file=traj)
                attempts += 1
        finally:
            reset_plan_context(tok_plan)
            reset_design_context(tok_design)
            if tok_access is not None:
                reset_access_context(tok_access)

        records = design_ctx.records + plan_ctx.records
        info = getattr(self, "last_run_info", {}) or {}
        return {
            "records": records,
            "responses": plan_ctx.responses,
            "config": task_design,
            "patch": "" if rejected else patch_str,
            "patch_proposed": patch_str,
            "patch_rejection": rejected,
            "attempts": attempts,
            "truncated": bool(info.get("truncated")),
            "budget_exhausted": exhausted,
        }

    def self_improve(self, recent: Optional[Dict[str, Any]] = None, broker: Any = None,
                     max_tool_calls: int = 30, trajectory_genids: Optional[List[Any]] = None,
                     patch_retry_k: int = 2):
        cfg = self.load_self_config()
        dctx = DesignContext(role="planner", config=cfg, node_id="self")
        tok = set_design_context(dctx)
        tok_access = (set_access_context(broker, "planner", self.access_key("self"),
                                         trajectory_genids=trajectory_genids)
                      if broker is not None else None)
        traj = self.session_trajectory(None)
        attempts = 0
        rejected = None
        exhausted = False
        patch_str = ""
        try:
            from gan.framework.trajectory import outer_session_index
            sessions = outer_session_index(self.output_dir, self.outer, "planner")
            sess_line = json.dumps(sessions, ensure_ascii=False) if sessions else "[]"
            rr = render_receipt((recent or {}).get("receipt"))
            instruction = (
                "Improve YOURSELF (the planner's own design) using only design operators "
                "(`set_prompt`/`set_config`/`select_component`/`deselect_component`/`set_param`; "
                "deep changes need `request_source_access`). Goal: plan better task agents over "
                "the long run. "
                "Do not repeat the same tool call; when done, stop.\n"
                f"\n## Your session trajectories this outer (use read_session_trajectory)\n{sess_line}\n"
                + (("\n" + rr + "\n") if rr else "")
                + f"\nRecent outcomes: {json.dumps({k: v for k, v in (recent or {}).items() if k != 'receipt'}, ensure_ascii=False)[:2000]}"
            )
            hist = self.run(instruction, max_tool_calls=max_tool_calls, trajectory_file=traj)

            def _build_patch() -> str:
                if broker is None or not any(
                    r.get("op") == "request_source_access" and r.get("intent") == "modify"
                    for r in dctx.records
                ):
                    return ""
                # B6: no catch — see the plan-session _build_patch comment.
                return build_patch_from_workspace(broker, "planner", self.access_key("self"))

            last_hash = None
            for attempt in range(patch_retry_k + 1):
                patch_str = _build_patch()
                if not patch_str or not self.code_root:
                    rejected = None
                    break
                ok, reason = code_repo.check_patch(self.code_root, patch_str)
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
                    "# Patch rejected\nYour previous patch was rejected by validation:\n"
                    f"{reason}\nFix the problem, then stop. Do not repeat the same action.",
                    msg_history=hist, max_tool_calls=max_tool_calls, trajectory_file=traj)
                attempts += 1
        finally:
            reset_design_context(tok)
            if tok_access is not None:
                reset_access_context(tok_access)
        self.save_self_config(cfg)
        info = getattr(self, "last_run_info", {}) or {}
        return {"records": dctx.records, "self_design": self.self_design_path(),
                "patch": "" if rejected else patch_str, "patch_proposed": patch_str,
                "patch_rejection": rejected,
                "attempts": attempts, "truncated": bool(info.get("truncated")),
                "budget_exhausted": exhausted}
