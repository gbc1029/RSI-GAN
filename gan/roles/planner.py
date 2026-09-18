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
from gan.patch import build_patch_from_workspace
from gan.roles.base_role import Role
from gan.summary import validate_feedback


class Planner(Role):
    def __init__(self, model: str, output_dir: str, **kwargs):
        super().__init__("planner", model, output_dir, load_seed("planner"), **kwargs)

    # -- instruction -------------------------------------------------------
    def _plan_instruction(
        self,
        parent_summary: Dict[str, Any],
        last_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
        parents: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        parts = ["Improve the task agent's DESIGN for the next generation."]
        parts.append(
            "\n## Shallow surface (design config)\n"
            "Edit the task design ONLY via design operators: `set_prompt`, `set_config`, "
            "`select_component`, `set_param`, `mint_operator`. Adding a new config key or a new "
            "component requires a DEEP change: `code_edit` (gated source edit)."
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
    ) -> Dict[str, Any]:
        if last_feedback is not None and not validate_feedback(last_feedback):
            raise ValueError("incompatible feedback schema_version")

        task_design = config if isinstance(config, dict) else {}
        plan_ctx = PlanContext(config=task_design, node_id=node_id, output_dir=self.output_dir)
        design_ctx = DesignContext(role="task", config=task_design, node_id=node_id)
        tok_plan = set_plan_context(plan_ctx)
        tok_design = set_design_context(design_ctx)
        tok_access = set_access_context(broker, "planner", node_id) if broker is not None else None
        try:
            self.run(self._plan_instruction(parent_summary or {}, last_feedback, evaluator_issues, parents))
        finally:
            reset_plan_context(tok_plan)
            reset_design_context(tok_design)
            if tok_access is not None:
                reset_access_context(tok_access)

        records = design_ctx.records + plan_ctx.records
        patch_str = ""
        if broker is not None and any(r.get("op") == "code_edit" for r in records):
            try:
                patch_str = build_patch_from_workspace(broker, "planner", node_id)
            except Exception:
                patch_str = ""

        return {
            "records": records,
            "responses": plan_ctx.responses,
            "config": task_design,
            "patch": patch_str,
        }

    def self_improve(self, recent: Optional[Dict[str, Any]] = None):
        cfg = self.load_self_config()
        dctx = DesignContext(role="planner", config=cfg, node_id="self")
        tok = set_design_context(dctx)
        try:
            instruction = (
                "Improve YOURSELF (the planner's own design) using only design operators "
                "(`set_prompt`/`set_config`/`select_component`/`set_param`/`mint_operator`; "
                "deep changes need `code_edit`). Goal: plan better task agents over the long run. "
                "Do not repeat the same tool call; when done, stop.\n"
                f"Recent outcomes: {json.dumps(recent or {}, ensure_ascii=False)[:2000]}"
            )
            self.run(instruction, max_tool_calls=8)
        finally:
            reset_design_context(tok)
        self.save_self_config(cfg)
        return {"records": dctx.records, "self_design": self.self_design_path()}
