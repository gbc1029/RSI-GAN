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

Failure semantics (v4):
- planner / task / evaluator execution failure => the round is INVALID: archived
  but never a parent.
- bench score completely missing => imputed from same-parent siblings' median
  (else the parent's score); imputed scores are NOT used as the evaluator's anchor.
- partial coverage => score kept, ``score_status="partial"``, ``coverage`` recorded.

Checkpointing (v4): an outer-loop checkpoint is written after every inner round
and after every outer generation; ``resume=True`` continues and
``rollback_to_outer()`` reverts to the last outer boundary.
"""
from __future__ import annotations

import json
import os
import statistics
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

from gan.design.schema import default_config
from gan.framework import checkpoint as ckpt
from gan.framework.reward.evaluator_reward import build_feedback_digest
from gan.framework.reward.packet import RewardPacket
from gan.summary import build_diff_summary, make_feedback
from gan.framework.tree.store import Node, NodeValue, TreeStore

_MEASURED = ("ok", "partial")


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
        resume: bool = False,
        enable_checkpoint: bool = True,
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.domains = list(domains)
        self.cfg = cfg
        self.planner = planner
        self.evaluator = evaluator
        self.task_runner = task_runner
        self.broker = broker
        self.resume = bool(resume)
        self.enable_checkpoint = bool(enable_checkpoint)
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
        self._seed = int(os.environ.get("GAN_SEED", "0") or 0)
        self._start_outer = 1
        self._start_inner = 1

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
                "score_status": child.value.score_status,
                "coverage": child.value.coverage,
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
        """Build the TEXT digest for the *previous* round's issues."""
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

    # -- scoring / failure handling ---------------------------------------
    def _impute_score(self, parent: Optional[Node]) -> Optional[float]:
        """Impute a missing benchmark score: siblings' median, else parent score."""
        if parent is None:
            return None
        sibs = [
            n.value.score for n in self.task_tree.nodes.values()
            if n.parent_genid == parent.genid
            and n.value.score is not None
            and n.value.score_status in _MEASURED
        ]
        if len(sibs) >= 2:
            return float(statistics.median(sibs))
        return parent.value.score

    def _finalize_child(self, child: Node, parent: Optional[Node]) -> None:
        """Decide score/validity: measured / partial / imputed / invalid."""
        status = getattr(child.value, "score_status", "ok") or "ok"
        child.meta.setdefault("report_summary", {})
        if child.value.score is not None and status in _MEASURED:
            return
        if status == "invalid":
            child.valid_parent = False
            child.value.score_status = "invalid"
            return
        # bench missing -> impute; if no basis, mark invalid (cannot be parent)
        imputed = self._impute_score(parent)
        if imputed is None:
            child.valid_parent = False
            child.value.score_status = "invalid"
            child.meta["invalid"] = True
            child.meta["invalid_reason"] = child.meta.get("invalid_reason") or "bench_missing_no_impute"
            return
        child.value.score = imputed
        child.value.score_status = "imputed"
        child.meta["imputed"] = True
        child.meta["imputed_score"] = imputed

    def _archive_invalid(
        self, reason: str, parent: Optional[Node], parents: List[Node], genid: Any, detail: str = ""
    ) -> Node:
        node = Node(
            genid=genid,
            parent_genid=(parent.genid if parent is not None else None),
            parents=[p.genid for p in (parents or [])] or ([parent.genid] if parent is not None else []),
            valid_parent=False,
            status="invalid",
            value=NodeValue(score=None, score_status="invalid"),
            meta={"invalid": True, "invalid_reason": reason, "invalid_detail": detail[:500]},
        )
        self.task_tree.add_node(node)
        self.log_event({"type": "inner_invalid", "genid": genid, "reason": reason, "detail": detail[:300]})
        return node

    # -- checkpoints -------------------------------------------------------
    def _state_dict(self, outer: int, inner: int) -> Dict[str, Any]:
        return {
            "gen_counter": self._gen_counter,
            "last_feedback": self._last_feedback,
            "last_digest": self._last_digest,
            "digests": self._digests,
            "advantages": self._advantages,
            "prev_predicted": self._prev_predicted,
            "prev_benchmark": self._prev_benchmark,
            "seed": self._seed,
            "outer": outer,
            "inner": inner,
        }

    def _load_state(self, state: Dict[str, Any]) -> None:
        self._gen_counter = int(state.get("gen_counter", self._gen_counter))
        self._last_feedback = state.get("last_feedback")
        self._last_digest = state.get("last_digest")
        self._digests = list(state.get("digests") or [])
        self._advantages = list(state.get("advantages") or [])
        self._prev_predicted = state.get("prev_predicted")
        self._prev_benchmark = state.get("prev_benchmark")
        self._seed = int(state.get("seed", self._seed) or 0)

    def _save_checkpoint(self, boundary: str, outer: int, inner: int) -> None:
        if not self.enable_checkpoint:
            return
        ckpt.save_checkpoint(
            self.output_dir,
            boundary=boundary,
            state=self._state_dict(outer, inner),
            trees=ckpt.capture_trees(self.task_tree, self.planner_tree, self.evaluator_tree),
            designs=ckpt.snapshot_designs(self.output_dir, ["planner", "evaluator"]),
        )

    def _restore(self, boundary: str = "latest") -> bool:
        payload = ckpt.load_checkpoint(self.output_dir, boundary)
        if not payload:
            return False
        ckpt.restore_trees(self.output_dir, payload.get("trees") or {})
        ckpt.restore_designs(self.output_dir, payload.get("designs") or {})
        self.task_tree = TreeStore(self.output_dir, "task").load()
        self.planner_tree = TreeStore(self.output_dir, "planner").load()
        self.evaluator_tree = TreeStore(self.output_dir, "evaluator").load()
        self._load_state(payload.get("state") or {})
        return True

    def rollback_to_outer(self) -> bool:
        """Revert to the last outer-generation boundary (trees + role designs)."""
        ok = self._restore("outer")
        if ok:
            self.log_event({"type": "rollback", "boundary": "outer"})
        return ok

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

        if self.resume and len(self.task_tree):
            if self._restore("latest"):
                st = ckpt.load_checkpoint(self.output_dir, "latest") or {}
                s = st.get("state") or {}
                if st.get("boundary") == "outer":
                    self._start_outer = int(s.get("outer", 0)) + 1
                    self._start_inner = 1
                else:
                    self._start_outer = int(s.get("outer", 1))
                    self._start_inner = int(s.get("inner", 0)) + 1
                self.log_event({"type": "resume", "outer": self._start_outer, "inner": self._start_inner})

        if len(self.task_tree) == 0:
            self.task_tree.add_node(Node(genid="initial", value=NodeValue(score=None)))
            self.log_event({"type": "init", "task_tree": "initial"})

        for outer in range(self._start_outer, G + 1):
            no_improve = 0
            outer_improved = False
            self.log_event({"type": "outer_start", "outer": outer})
            # snapshot role designs so a non-improving self-improvement can be reverted
            designs_before = ckpt.snapshot_designs(self.output_dir, ["planner", "evaluator"])

            inner_start = self._start_inner if outer == self._start_outer else 1
            for inner in range(inner_start, I_max + 1):
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

                try:
                    plan_result = self.planner.plan(
                        parent_summary=self._parent_summary(parent),
                        parents=[self._parent_summary(p) for p in parents],
                        last_feedback=self._last_feedback,
                        evaluator_issues=evaluator_issues,
                        config=parent_cfg,
                        node_id=genid,
                        broker=self.broker,
                    ) or {}
                except Exception as e:
                    self._archive_invalid("planner_failed", parent, parents, genid, str(e))
                    self._save_checkpoint("inner", outer, inner)
                    continue

                try:
                    child = self.task_runner(plan=plan_result, parent=parent, genid=genid)
                except Exception as e:
                    self._archive_invalid("task_runner_failed", parent, parents, genid, str(e))
                    self._save_checkpoint("inner", outer, inner)
                    continue
                if child is None:
                    self._archive_invalid("task_runner_none", parent, parents, genid)
                    self._save_checkpoint("inner", outer, inner)
                    continue

                # DAG: record all parents used
                child.parents = [p.genid for p in parents]
                child.parent_genid = parent.genid
                plan_cfg = plan_result.get("config")
                child.meta["config_dict"] = plan_cfg if isinstance(plan_cfg, dict) else parent_cfg

                # score / imputation / validity
                self._finalize_child(child, parent)

                # evaluator (execute even for missing bench; pass imputed info, not as anchor)
                benchmark_for_eval = None if child.meta.get("imputed") else child.value.score
                try:
                    ctx = self.evaluator.evaluate(
                        run_summary={
                            "genid": genid,
                            "score": child.value.score,
                            "meta": child.meta,
                            "report_summary": child.meta.get("report_summary"),
                            "score_status": child.value.score_status,
                            "imputed": bool(child.meta.get("imputed")),
                        },
                        prev_feedback={**(self._last_feedback or {}), "digest": self._last_digest},
                        benchmark_score=benchmark_for_eval,
                        node_id=genid,
                        broker=self.broker,
                    )
                except Exception as e:
                    child.valid_parent = False
                    child.meta["invalid"] = True
                    child.meta["invalid_reason"] = "evaluator_failed"
                    child.meta["invalid_detail"] = str(e)[:500]
                    self.task_tree.add_node(child)
                    self.log_event({"type": "inner_invalid", "genid": genid,
                                    "reason": "evaluator_failed", "detail": str(e)[:300]})
                    self._save_checkpoint("inner", outer, inner)
                    continue

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

                # stagnation + advantage (only measured scores count as improvement)
                ps, cs = parent.value.score, child.value.score
                measured = child.value.score_status in _MEASURED
                improved = measured and (cs is not None) and (ps is None or (cs - ps) > eps)
                outer_improved = outer_improved or improved
                no_improve = 0 if improved else no_improve + 1
                if cs is not None and ps is not None:
                    self._advantages.append(cs - ps)

                self.log_event({
                    "type": "inner_done", "outer": outer, "inner": inner,
                    "genid": genid, "parent_genid": parent.genid, "parents": child.parents,
                    "score": cs, "parent_score": ps, "improved": improved,
                    "score_status": child.value.score_status, "coverage": child.value.coverage,
                    "imputed": bool(child.meta.get("imputed")),
                    "valid_parent": child.valid_parent,
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

                self._save_checkpoint("inner", outer, inner)

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

            # acceptance: revert role self-improvement if this outer did not improve
            if designs_before and not outer_improved:
                ckpt.restore_designs(self.output_dir, designs_before)
                self.log_event({"type": "self_improve_reverted", "outer": outer})

            self.evaluator_tree.add_node(Node(genid=f"eval_{outer}", meta={"num_digests": len(self._digests)}))
            self.planner_tree.add_node(Node(genid=f"plan_{outer}", meta={"advantages": self._advantages[-I_max:]}))
            self.log_event({"type": "outer_done", "outer": outer, "improved": outer_improved})
            self._save_checkpoint("outer", outer, I_max)

        return self.task_tree
