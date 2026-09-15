"""Dual-loop scheduler.

Outer loop = one meta generation (planner + evaluator self-improve).
Inner loop = one task-agent generation (plan -> task run -> evaluate -> commit).

Collaborators are duck-typed so the loop can be smoke-tested offline:
    planner.plan(parent_summary, parents, last_feedback, evaluator_issues, config, node_id, broker)
        -> {"records": [...], "responses": [...], "config": Config, "patch": str}
    task_runner(plan, parent, genid) -> Node
    evaluator.evaluate(run_summary, prev_feedback, benchmark_score, node_id, broker) -> EvalContext-like
    planner.self_improve(recent) / evaluator.self_improve(recent)

Evaluator feedback is TEXT (a digest), not a numeric reward.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

from gan.design.schema import default_config
from gan.reward.evaluator_reward import build_feedback_digest
from gan.reward.packet import RewardPacket
from gan.summary import build_diff_summary, make_feedback
from gan.tree.store import Node, NodeValue, TreeStore


class GanLoop:
    def __init__(
        self,
        output_dir: str,
        domains: List[str],
        cfg: Any,
        planner: Any,
        evaluator: Any,
        task_runner: Callable[..., Node],
        broker: Any = None,
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.domains = list(domains)
        self.cfg = cfg
        self.planner = planner
        self.evaluator = evaluator
        self.task_runner = task_runner
        self.broker = broker
        os.makedirs(self.output_dir, exist_ok=True)
        self.task_tree = TreeStore(self.output_dir, "task").load()
        self.planner_tree = TreeStore(self.output_dir, "planner").load()
        self.evaluator_tree = TreeStore(self.output_dir, "evaluator").load()
        self._gen_counter = self._max_int_genid() + 1
        # rolling state
        self._last_feedback: Optional[Dict[str, Any]] = None
        self._last_digest: Optional[str] = None
        self._prev_predicted: Optional[float] = None
        self._prev_benchmark: Optional[float] = None
        self._digests: List[str] = []
        self._advantages: List[float] = []

    # -- helpers -----------------------------------------------------------
    def _max_int_genid(self) -> int:
        mx = -1
        for k in self.task_tree.nodes:
            try:
                mx = max(mx, int(k))
            except (TypeError, ValueError):
                continue
        return mx

    def log_event(self, event: Dict[str, Any]) -> None:
        with open(os.path.join(self.output_dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _parent_summary(self, parent: Node) -> Dict[str, Any]:
        return {
            "genid": parent.genid,
            "parents": parent.parents,
            "depth": parent.depth,
            "children": parent.children,
            "score": parent.value.score,
            "scores": parent.scores,
            "modify_depth": parent.modify_depth,
        }

    def _initial_task_config(self) -> Dict[str, Any]:
        cfg = default_config("task")
        cfg.setdefault("params", {})["model"] = self.cfg.get("models.task", "gpt-4o-mini")
        return cfg

    def _parent_config(self, parent: Optional[Node]) -> Dict[str, Any]:
        cd = parent.meta.get("config_dict") if (parent is not None and parent.meta) else None
        return deepcopy(cd) if cd else self._initial_task_config()

    def _build_packet(self, child: Node, ctx: Any) -> RewardPacket:
        return RewardPacket(
            numeric={
                "benchmark": child.value.score,
                "delta_vs_parent": child.meta.get("delta_vs_parent"),
                "cost_tokens": child.value.cost_tokens,
                "cost_wallclock_s": child.value.cost_wallclock_s,
            },
            textual={
                "summary": getattr(ctx, "summary", ""),
                "weaknesses": getattr(ctx, "weaknesses", []),
                "suggestions": getattr(ctx, "suggestions", []),
            },
            penalties=dict(getattr(ctx, "penalties", {}) or {}),
        )

    def _settle_feedback_digest(
        self,
        plan_result: Dict[str, Any],
        ctx: Any,
        diff_summary: Optional[Dict[str, Any]],
        genid: Any,
    ) -> Optional[str]:
        """Build the TEXT digest for the *previous* round's issues.

        Uses this round's planner ``responses`` and the evaluator's
        ``fix_verdicts`` (both now available). No numeric reward.
        """
        if self._last_feedback is None:
            return None
        prev_issues = self._last_feedback.get("issues") or []
        if not prev_issues:
            return None
        digest = build_feedback_digest(
            issues=prev_issues,
            responses=plan_result.get("responses") or [],
            verdicts=getattr(ctx, "fix_verdicts", []) or [],
            predicted_score=self._prev_predicted,
            benchmark_score=self._prev_benchmark,
            diff_summary=diff_summary,
        )
        run_dir = os.path.join(self.output_dir, "runs", str(genid))
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "feedback_digest.md"), "w", encoding="utf-8") as f:
            f.write(digest)
        self._digests.append(digest)
        self.log_event({"type": "feedback_digest", "genid": genid, "bytes": len(digest)})
        return digest

    # -- main loop ---------------------------------------------------------
    def run(self) -> TreeStore:
        cfg = self.cfg
        G = int(cfg.get("loop.outer_generations", 2))
        I_max = int(cfg.get("loop.inner_max", 3))
        patience = int(cfg.get("loop.stagnation_patience", 2))
        eps = float(cfg.get("loop.stagnation_epsilon", 0.005))
        bl = int(cfg.get("tree.branch_limit", 2))
        dl = int(cfg.get("tree.depth_limit", 5))
        method = cfg.get("tree.selection", "ucb")
        ucb_c = float(cfg.get("tree.ucb_c", 1.0))
        depth_penalty = float(cfg.get("tree.depth_penalty", 0.05))
        cost_penalty = float(cfg.get("tree.cost_penalty", 0.0))
        n_parents = int(cfg.get("tree.num_parents", 1))  # >1 enables crossover

        if len(self.task_tree) == 0:
            self.task_tree.add_node(Node(genid="initial", value=NodeValue(score=None)))
            self.log_event({"type": "init", "task_tree": "initial"})

        for outer in range(1, G + 1):
            no_improve = 0
            self.log_event({"type": "outer_start", "outer": outer})
            for inner in range(1, I_max + 1):
                parents = self.task_tree.select_parents(
                    k=n_parents, branch_limit=bl, depth_limit=dl, method=method,
                    ucb_c=ucb_c, depth_penalty=depth_penalty, cost_penalty=cost_penalty,
                )
                if not parents:
                    self.log_event({"type": "inner_skip", "reason": "no_candidate", "outer": outer, "inner": inner})
                    break
                parent = parents[0]

                genid = self._gen_counter
                self._gen_counter += 1
                parent_cfg = self._parent_config(parent)
                evaluator_issues = (self._last_feedback or {}).get("issues")

                plan_result = self.planner.plan(
                    parent_summary=self._parent_summary(parent),
                    parents=[self._parent_summary(p) for p in parents],
                    last_feedback=self._last_feedback,
                    evaluator_issues=evaluator_issues,
                    config=parent_cfg,
                    node_id=genid,
                    broker=self.broker,
                ) or {}

                child = self.task_runner(plan=plan_result, parent=parent, genid=genid)
                if child is None:
                    self.log_event({"type": "inner_skip", "reason": "task_runner_none", "outer": outer, "inner": inner})
                    continue
                # DAG: record all parents used
                child.parents = [p.genid for p in parents]
                child.parent_genid = parent.genid

                # lineage config snapshot (task design)
                plan_cfg = plan_result.get("config")
                child.meta["config_dict"] = plan_cfg if isinstance(plan_cfg, dict) else parent_cfg

                ctx = self.evaluator.evaluate(
                    run_summary={"genid": genid, "score": child.value.score, "meta": child.meta},
                    prev_feedback={**(self._last_feedback or {}), "digest": self._last_digest},
                    benchmark_score=child.value.score,
                    node_id=genid,
                    broker=self.broker,
                )

                packet = self._build_packet(child, ctx)
                if getattr(ctx, "predicted_score", None) is not None:
                    child.value.potential = ctx.predicted_score
                run_dir = os.path.join(self.output_dir, "runs", str(genid))
                os.makedirs(run_dir, exist_ok=True)
                packet.save(os.path.join(run_dir, "packet.json"))
                child.meta["packet_path"] = os.path.join(run_dir, "packet.json")

                self.task_tree.add_node(child)

                # sanitized diff summary + text digest for the previous round
                diff_summary = build_diff_summary(
                    plan_result.get("records"),
                    [child.meta.get("report_path")] if child.meta.get("report_path") else None,
                )
                self._last_digest = self._settle_feedback_digest(plan_result, ctx, diff_summary, genid)

                # stagnation + advantage
                ps, cs = parent.value.score, child.value.score
                improved = (cs is not None) and (ps is None or (cs - ps) > eps)
                no_improve = 0 if improved else no_improve + 1
                if cs is not None and ps is not None:
                    self._advantages.append(cs - ps)

                self.log_event({
                    "type": "inner_done", "outer": outer, "inner": inner,
                    "genid": genid, "parent_genid": parent.genid, "parents": child.parents,
                    "score": cs, "parent_score": ps, "improved": improved,
                    "no_improve": no_improve, "penalties": packet.penalties,
                    "modify_depth": child.modify_depth,
                })

                # feedback for next round: planner gets issues + diff summary
                self._last_feedback = make_feedback(
                    issues=getattr(ctx, "issues", []),
                    diff_summary=diff_summary,
                    penalties=packet.penalties,
                )
                self._prev_predicted = getattr(ctx, "predicted_score", None)
                self._prev_benchmark = cs

                if no_improve >= patience:
                    self.log_event({"type": "stagnation_break", "outer": outer, "inner": inner})
                    break

            # ---------------- outer self-improvement ----------------
            recent_eval = {"digests": self._digests[-I_max:], "last_feedback": self._last_feedback}
            recent_plan = {"advantages": self._advantages[-I_max:],
                           "task_tree_size": len(self.task_tree)}
            try:
                self.evaluator.self_improve(recent=recent_eval)
            except Exception as e:
                self.log_event({"type": "self_improve_error", "role": "evaluator", "error": str(e)})
            try:
                self.planner.self_improve(recent=recent_plan)
            except Exception as e:
                self.log_event({"type": "self_improve_error", "role": "planner", "error": str(e)})

            self.evaluator_tree.add_node(Node(genid=f"eval_{outer}", meta={"num_digests": len(self._digests)}))
            self.planner_tree.add_node(Node(genid=f"plan_{outer}", meta={"advantages": self._advantages[-I_max:]}))
            self.log_event({"type": "outer_done", "outer": outer})

        return self.task_tree
