"""Planner role: generate improvements for the task agent."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from gan.access import reset_access_context, set_access_context
from gan.config.loader import load_prompt
from gan.operators.context import PlanContext, reset_plan_context, set_plan_context
from gan.patch import build_patch_from_workspace
from gan.roles.base_role import Role
from gan.summary import validate_feedback


class Planner(Role):
    def __init__(self, model: str, output_dir: str, **kwargs):
        super().__init__("planner", model, output_dir, load_prompt("planner"), **kwargs)

    # -- instruction -------------------------------------------------------
    def _plan_instruction(
        self,
        parent_summary: Dict[str, Any],
        last_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        parts = ["Improve the task agent for the next generation."]
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
                "\n## Diff summary (what changed last round)\n"
                f"```json\n{json.dumps(last_feedback.get('diff_summary'), ensure_ascii=False)[:1500]}\n```"
            )
        parts.append(
            "\nPrefer config/operator changes. Only call `request_source_access`/`code_edit` "
            "if config+operators cannot express the improvement."
        )
        return "\n".join(parts)

    # -- API ---------------------------------------------------------------
    def plan(
        self,
        parent_summary: Optional[Dict[str, Any]] = None,
        last_feedback: Optional[Dict[str, Any]] = None,
        evaluator_issues: Optional[List[Dict[str, Any]]] = None,
        config: Any = None,
        node_id: Any = None,
        broker: Any = None,
    ) -> Dict[str, Any]:
        if last_feedback is not None and not validate_feedback(last_feedback):
            raise ValueError("incompatible feedback schema_version")

        ctx = PlanContext(config=config, node_id=node_id, output_dir=self.output_dir)
        tok_plan = set_plan_context(ctx)
        tok_access = set_access_context(broker, "planner", node_id) if broker is not None else None
        try:
            self.run(self._plan_instruction(parent_summary or {}, last_feedback, evaluator_issues))
        finally:
            reset_plan_context(tok_plan)
            if tok_access is not None:
                reset_access_context(tok_access)

        patch_str = ""
        if broker is not None and any(r.get("op") == "code_edit" for r in ctx.records):
            try:
                patch_str = build_patch_from_workspace(broker, "planner", node_id)
            except Exception:
                patch_str = ""

        return {
            "records": ctx.records,
            "responses": ctx.responses,
            "config": ctx.config,
            "patch": patch_str,
        }

    def self_improve(self, recent: Optional[Dict[str, Any]] = None):
        from gan.operators.context import PlanContext, reset_plan_context, set_plan_context

        cfg = self.load_self_config()
        ctx = PlanContext(config=cfg, node_id="self", output_dir=self.output_dir)
        tok = set_plan_context(ctx)
        try:
            instruction = (
                "Improve YOURSELF (the planner): update your own prompt/config/operator "
                "proposals to plan better task agents over the long run. Do not repeat the "
                "same tool call; when done, stop.\n"
                f"Recent outcomes: {json.dumps(recent or {}, ensure_ascii=False)[:2000]}"
            )
            self.run(instruction, max_tool_calls=8)
        finally:
            reset_plan_context(tok)
        self.save_self_config(cfg)
        return {"records": ctx.records, "self_config_path": self._self_config_path()}
