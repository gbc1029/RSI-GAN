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

Checkpointing (v5): ``checkpoint.json`` is the latest snapshot; outer boundaries
additionally write an immutable numbered ``checkpoints/outer_<N>.json``. An outer
checkpoint is written after that outer's self-improvement, so it carries the design
used by the next outer. ``resume=True`` continues and ``rollback_to_outer()`` reverts
to the last outer boundary (append-only ``op=reset`` marker, no log truncation).

Instances (v5): planner/evaluator are refreshed every outer generation
(``_refresh_roles``) from the active design, with a cleared source workspace; task
agents are refreshed every inner generation. Scheduling is a **single chain** — no
accept/reject and no parent selection for roles; ``outer_improved`` is logged only.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import uuid
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

from gan.design import initial_config
from gan.framework import checkpoint as ckpt
from gan.framework import paths, scores, trajectory
from gan.framework.loader import load_registry, resolve_domain
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
        planner: Any = None,
        evaluator: Any = None,
        task_runner: Callable[..., Node] = None,
        broker: Any = None,
        resume: bool = False,
        enable_checkpoint: bool = True,
        planner_factory: Optional[Callable[[int], Any]] = None,
        evaluator_factory: Optional[Callable[[int], Any]] = None,
        code_root: Optional[str] = None,
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
        self.planner_factory = planner_factory
        self.evaluator_factory = evaluator_factory
        self.keep_workdirs = bool(cfg.get("loop.keep_workdirs", False))
        self.self_improve_max_tool_calls = int(cfg.get("loop.self_improve_max_tool_calls", 30))
        self.plan_max_tool_calls = int(cfg.get("loop.plan_max_tool_calls", 40))
        self.patch_retry_k = int(cfg.get("loop.patch_retry_k", 2))
        self.code_root = os.path.abspath(code_root) if code_root else None
        # last round receipt (injected into the next round's plan/evaluate/self_improve)
        self._last_receipt: Optional[Dict[str, Any]] = None
        self._last_self_receipt: Optional[Dict[str, Any]] = None
        # run-attempt id: identifies THIS loop instantiation (one per outer worker /
        # in-process run). Outer-level trajectory files are keyed by it so that
        # re-running an outer never truncates or mixes a previous attempt's traces.
        self._attempt = uuid.uuid4().hex[:8]
        # role self-edit commits made by this run (audit; G2-lite)
        self._role_commits: Dict[str, str] = {}
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
        self._start_outer = 1
        self._start_inner = 1
        # Human-readable task description for the task domain (framework-injected;
        # NOT part of the evolvable design). Used by planner/evaluator prompts.
        self.task_brief = ""
        if self.domains:
            try:
                self.task_brief = resolve_domain(load_registry(), self.domains[0]).get("task_brief") or ""
            except Exception:
                self.task_brief = ""

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
        from utils import trajectory_log
        trajectory_log.append(paths.events_path(self.output_dir), event)

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
        # The model is NOT part of the evolvable design any more; it is resolved
        # centrally by gan/framework/models.py and injected by the runner.
        # Generation-0 prompt comes from gan/design/seeds/task.md, through the
        # SAME helper the planner/evaluator self-designs use.
        return initial_config("task")

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
        run_dir = paths.runs_dir(self.output_dir, genid)
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
        self, reason: str, parent: Optional[Node], parents: List[Node], genid: Any,
        detail: str = "", outer: Optional[int] = None, inner: Optional[int] = None,
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
        try:
            shutil.rmtree(paths.work_dir(self.output_dir, genid), ignore_errors=True)
        except Exception:
            pass
        self._write_invalid(genid, reason, detail, outer, inner)
        self.log_event({"type": "inner_invalid", "genid": genid, "reason": reason, "detail": detail[:300]})
        return node

    def _write_invalid(self, genid: Any, reason: str, detail: str = "",
                       outer: Optional[int] = None, inner: Optional[int] = None) -> None:
        """Persist a minimal record so a missing ``runs/<genid>`` is explainable."""
        try:
            d = paths.runs_dir(self.output_dir, genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "invalid.json"), "w", encoding="utf-8") as f:
                json.dump({"genid": str(genid), "reason": reason, "detail": detail[:1000],
                           "outer": outer, "inner": inner}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
            "role_commits": dict(self._role_commits),
            "attempt": self._attempt,
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
        self._role_commits = dict(state.get("role_commits") or {})

    def _save_checkpoint(self, boundary: str, outer: int, inner: int) -> None:
        if not self.enable_checkpoint:
            return
        code = None
        if self.code_root:
            from gan.framework import code_repo
            try:
                code = {"commit": code_repo.current_commit(self.code_root)}
            except Exception:
                code = None
        ckpt.save_checkpoint(
            self.output_dir,
            boundary=boundary,
            state=self._state_dict(outer, inner),
            trees=ckpt.capture_trees(self.task_tree, self.planner_tree, self.evaluator_tree),
            designs=ckpt.snapshot_designs(self.output_dir, ["planner", "evaluator"]),
            index=(outer if boundary == "outer" else None),
            code=code,
        )

    def _refresh_roles(self, outer: int) -> None:
        """Rebuild planner/evaluator for a new outer generation (instance refresh).

        Each outer generation is a fresh role *instance*: fresh design load,
        fresh toolset (so selections take effect), fresh chat history, and a
        cleared source workspace. The active design on disk is the one saved
        after the previous outer's self-improvement (single chain, no revert).
        Without a factory the injected instances are reused (offline/smoke mode).
        """
        if self.planner_factory is not None:
            self.planner = self.planner_factory(outer)
        if self.evaluator_factory is not None:
            self.evaluator = self.evaluator_factory(outer)
        for inst in (self.planner, self.evaluator):
            try:
                inst.attempt_id = self._attempt
            except Exception:
                pass
        if self.broker is not None:
            for role in ("planner", "evaluator"):
                try:
                    self.broker.clear_workspace(role)
                except Exception:
                    pass
        self.log_event({"type": "role_refresh", "outer": outer})

    def _source_access_log(self, outer: Any) -> List[Dict[str, Any]]:
        """Audit of source paths granted to roles for this outer instance."""
        if self.broker is None:
            return []
        akey = f"outer_{outer}"
        out: List[Dict[str, Any]] = []
        for role in ("planner", "evaluator"):
            out.extend(self.broker.grants.get((role, akey), []))
            out.extend(self.broker.grants.get((role, str(akey)), []))
        return out

    def _append_score(self, child: Node) -> None:
        if not self.enable_checkpoint:
            return
        try:
            scores.append_score(self.output_dir, {
                "genid": str(child.genid),
                "domain": (self.domains[0] if self.domains else None),
                "score": child.value.score,
                "score_status": child.value.score_status,
                "coverage": child.value.coverage,
                "report_sha": child.meta.get("report_sha"),
                "valid_parent": child.valid_parent,
            })
        except Exception:
            pass

    def _apply_self_patch(self, role: str, outer: int, res: Any) -> None:
        """Apply a role self-edit patch directly to the code tree (option B).

        The durable record is the code commit; no separate patch file is kept.
        Validation failure rolls back to the previous commit.
        """
        patch = (res or {}).get("patch") if isinstance(res, dict) else None
        if not patch:
            rejected = (res or {}).get("patch_rejection") if isinstance(res, dict) else None
            if rejected:
                self.log_event({"type": "self_improve_rejected", "role": role,
                                "outer": outer, "reason": str(rejected)[:300]})
            return
        if not self.code_root:
            return
        from gan.framework import code_repo
        prev = code_repo.current_commit(self.code_root)
        try:
            sha = code_repo.apply_self_patch(self.code_root, role, patch)
            self._role_commits[role] = sha
            self.log_event({"type": "self_improve_commit", "role": role,
                            "outer": outer, "commit": sha})
        except Exception as e:
            code_repo.checkout(self.code_root, prev)
            self.log_event({"type": "self_improve_apply_failed", "role": role,
                            "outer": outer, "error": str(e)[:300]})

    def _resync_workspace(self, outer: Any, files: List[str], drop_missing: bool = False) -> None:
        """Keep granted workspace copies in sync with the code baseline.

        After a code-base switch or task patch is applied to the code tree, the
        planner/evaluator workspaces (per outer) may hold stale copies; refresh
        only the granted files. With ``drop_missing``, a file absent from the
        (new) baseline is REMOVED from the workspace — a stale copy would fake a
        deletion diff.
        """
        code_root = getattr(self.broker, "repo_root", None)
        if not code_root:
            return
        akey = f"outer_{outer}"
        for role in ("planner", "evaluator"):
            try:
                src = self.broker.src_dir(role, akey)
            except Exception:
                continue
            for rel in files or []:
                s = os.path.join(code_root, rel)
                d = os.path.join(src, rel)
                if not os.path.isfile(s):
                    if drop_missing and os.path.isfile(d):
                        try:
                            os.remove(d)
                        except OSError:
                            pass
                    continue
                if os.path.isfile(d):
                    try:
                        shutil.copy2(s, d)
                    except OSError:
                        pass

    def _make_receipt(self, genid: Any, plan_result: Dict[str, Any],
                      parent_cfg: Dict[str, Any], child: Node, outer: Any) -> Dict[str, Any]:
        from gan.framework.receipt import build_receipt
        rejected = plan_result.get("patch_rejection") or child.meta.get("task_patch_rejected")
        exhausted = bool(plan_result.get("budget_exhausted"))
        truncated = bool(plan_result.get("truncated"))
        budget = {
            "exhausted": exhausted or truncated,
            "kind": "retries" if exhausted else ("max_tool_calls" if truncated else None),
            "attempts": plan_result.get("attempts", 0),
            "truncated": truncated,
        }
        refs = [
            paths.session_traj_file(self.output_dir, outer, genid, "planner"),
            paths.session_traj_file(self.output_dir, outer, genid, "task"),
            paths.session_traj_file(self.output_dir, outer, genid, "evaluator"),
        ]
        rec = build_receipt(
            genid=genid, role="planner", stage="plan",
            records=plan_result.get("records"),
            config=child.meta.get("config_dict"), parent_config=parent_cfg,
            patch=plan_result.get("patch_proposed", ""),
            patch_applied=bool(child.meta.get("patch_applied")),
            commit=child.meta.get("task_code_commit"),
            rejected_reason=rejected, budget=budget,
            grants=self._source_access_log(outer), trajectory_refs=refs,
        )
        try:
            d = paths.runs_dir(self.output_dir, genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "patch_receipt.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            if rejected and plan_result.get("patch_proposed"):
                with open(os.path.join(d, "patch_proposed.diff"), "w", encoding="utf-8") as f:
                    f.write(plan_result.get("patch_proposed") or "")
        except Exception:
            pass
        self.log_event({"type": "receipt", "genid": genid, "outer": outer,
                        "design_applied": rec["design"]["applied"],
                        "code_applied": rec["code_patch"]["applied"],
                        "rejected": bool(rejected), "budget": rec["budget"]})
        return rec

    def _make_self_receipt(self, role: str, outer: int, res: Any) -> Dict[str, Any]:
        from gan.framework.receipt import build_receipt
        r = res or {}
        rejected = r.get("patch_rejection")
        budget = {
            "exhausted": bool(r.get("budget_exhausted")) or bool(r.get("truncated")),
            "kind": "retries" if r.get("budget_exhausted") else ("max_tool_calls" if r.get("truncated") else None),
            "attempts": r.get("attempts", 0), "truncated": bool(r.get("truncated")),
        }
        rec = build_receipt(
            genid=f"outer_{outer}", role=role, stage="self_improve",
            records=r.get("records"), patch=r.get("patch_proposed", ""),
            patch_applied=bool(r.get("patch")), rejected_reason=rejected, budget=budget,
        )
        try:
            d = paths.runs_dir(self.output_dir, f"self_outer_{outer}_{role}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "patch_receipt.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self._last_self_receipt = rec
        self.log_event({"type": "self_receipt", "role": role, "outer": outer,
                        "code_applied": rec["code_patch"]["applied"], "rejected": bool(rejected)})
        return rec

    def _write_eval(self, child: Node, ctx: Any, packet: RewardPacket) -> None:
        """Persist the refined evaluator schema (issues/verdicts/prediction)."""
        try:
            rec = {
                "genid": str(child.genid),
                "score": child.value.score,
                "score_status": child.value.score_status,
                "predicted_score": getattr(ctx, "predicted_score", None),
                "issues": getattr(ctx, "issues", []),
                "fix_verdicts": getattr(ctx, "fix_verdicts", []),
                "penalties": dict(getattr(packet, "penalties", {}) or {}),
                "summary": getattr(ctx, "summary", ""),
                "weaknesses": list(getattr(ctx, "weaknesses", []) or []),
                "suggestions": list(getattr(ctx, "suggestions", []) or []),
            }
            d = paths.runs_dir(self.output_dir, child.genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "eval.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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

    # -- branch-per-node blood lineage (v5) ---------------------------------
    def _pin_outer_base(self, outer: int) -> None:
        """Anchor the outer's entry HEAD as ``outer_<O>_base`` (once per outer).

        HEAD drifts to the latest task-branch tip during the outer, so the base
        for ``parent == "initial"`` generations must be pinned BEFORE any task
        commit moves it. Idempotent (re-runs of the same outer).
        """
        if not self.code_root:
            return
        from gan.framework import code_repo
        ref = code_repo.outer_base_ref_name(outer)
        ok, mode = code_repo.ensure_branch(self.code_root, ref, code_repo.current_commit(self.code_root))
        self.log_event({"type": "code_branch", "kind": "outer_base", "outer": outer,
                        "ref": ref, "mode": mode, "ok": bool(ok)})

    def _resolve_base(self, parent: Any, outer: int) -> str:
        """Blood lineage: base = parent's code state (initial -> outer base).

        Legacy fallback: a node without a recorded code commit falls back to
        HEAD (behavior identical to the pre-branch time-line; audited).
        """
        from gan.framework import code_repo
        head = code_repo.current_commit(self.code_root)
        if str(parent.genid) == "initial":
            return code_repo.outer_base_ref_name(outer)
        sha = (parent.meta or {}).get("code_commit") or (parent.meta or {}).get("task_code_commit")
        if not sha:
            self.log_event({"type": "code_lineage_legacy", "outer": outer,
                            "parent_genid": str(parent.genid), "fallback_base": head})
            return head
        return sha

    def _align_code_base(self, parent: Any, outer: int, genid: Any) -> Optional[str]:
        """Checkout the selected parent's code state before planning; resync workspace.

        Returns the base (ref name or SHA) passed to the task patch application.
        Logs nothing when code_repo is off (legacy in-process path).
        """
        if not self.code_root:
            return None
        from gan.framework import code_repo
        base = self._resolve_base(parent, outer)
        checked = code_repo.checkout_base(self.code_root, base)
        self.log_event({"type": "code_lineage", "genid": str(genid),
                        "parent_genid": str(parent.genid),
                        "base": str(base), "checked": checked})
        granted_files: List[str] = []
        if self.broker is not None:
            for rec in self._source_access_log(outer):
                granted_files.extend(rec.get("paths", []) if isinstance(rec, dict) else [])
            # stale copies must not survive a base switch (a file that existed on
            # the previous tip but not on the new base would fake a deletion)
        self._resync_workspace(outer, granted_files, drop_missing=True)
        return str(base)

    # -- main loop ---------------------------------------------------------
    def run(self, only_outer: Optional[int] = None) -> TreeStore:
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
            # resume_boundary: "outer" (P1 — never resume from a crashed outer's
            # partial inner checkpoint) or "latest" (legacy, --force re-runs).
            boundary = getattr(self, "resume_boundary", "latest")
            if self._restore(boundary):
                st = ckpt.load_checkpoint(self.output_dir, boundary) or {}
                s = st.get("state") or {}
                if st.get("boundary") == "outer":
                    self._start_outer = int(s.get("outer", 0)) + 1
                    self._start_inner = 1
                else:
                    self._start_outer = int(s.get("outer", 1))
                    self._start_inner = int(s.get("inner", 0)) + 1
                self.log_event({"type": "resume", "boundary": boundary,
                                "attempt": self._attempt,
                                "outer": self._start_outer, "inner": self._start_inner})
                # ref reconciliation (audit only): genid -> recorded code_commit
                # vs task_* branch tips; missing refs never block a resume.
                if self.code_root:
                    from gan.framework import code_repo
                    commits = {}
                    for k, nd in self.task_tree.nodes.items():
                        sha = (nd.meta or {}).get("code_commit")
                        if sha:
                            commits[str(k)] = str(sha)
                    if commits:
                        rep = code_repo.reconcile_refs(self.code_root, commits)
                        if rep.get("missing") or rep.get("drifted"):
                            self.log_event({"type": "code_ref_missing", "report": rep})

        if len(self.task_tree) == 0:
            self.task_tree.add_node(Node(genid="initial", value=NodeValue(score=None)))
            self.log_event({"type": "init", "task_tree": "initial"})

        # only_outer: run exactly one outer generation (used by the per-outer worker)
        if only_outer is not None:
            start_outer = int(only_outer)
            end_outer = int(only_outer)
        else:
            start_outer = self._start_outer
            end_outer = G

        for outer in range(start_outer, end_outer + 1):
            no_improve = 0
            outer_improved = False
            outer_genids: List[Any] = []
            self.log_event({"type": "outer_start", "outer": outer})
            # fresh role instances (design + tools + chat + workspace) for this outer
            self._refresh_roles(outer)
            # pin the outer's entry HEAD as the base for parent=initial children
            # (HEAD drifts to task-branch tips during the outer)
            self._pin_outer_base(outer)

            inner_start = self._start_inner if (only_outer is None and outer == self._start_outer) else 1
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
                outer_genids.append(genid)
                # branch-per-node: align the code tree to the selected parent's
                # code state BEFORE planning (the planner diffs against the
                # parent's code, not the time-line HEAD).
                base = self._align_code_base(parent, outer, genid)
                parent_cfg = self._parent_config(parent)
                # Planner history follows the selected tree parent. The
                # rolling fields are loop-local execution state and may belong
                # to a different branch after parent selection.
                parent_meta = parent.meta or {}
                parent_feedback = parent_meta.get("feedback")
                parent_receipt = parent_meta.get("receipt")
                parent_predicted_score = parent_meta.get("predicted_score")
                parent_benchmark_score = parent_meta.get("benchmark_score")
                evaluator_issues = (parent_feedback or {}).get("issues")
                parent_ref = [] if str(parent.genid) == "initial" else [parent.genid]

                try:
                    plan_result = self.planner.plan(
                        parent_summary=self._parent_summary(parent),
                        parents=[self._parent_summary(p) for p in parents],
                        last_feedback=parent_feedback,
                        evaluator_issues=evaluator_issues,
                        config=parent_cfg,
                        node_id=genid,
                        broker=self.broker,
                        task_brief=self.task_brief,
                        trajectory_genids=parent_ref,
                        receipt=parent_receipt,
                        parent_predicted_score=parent_predicted_score,
                        parent_benchmark_score=parent_benchmark_score,
                        patch_retry_k=self.patch_retry_k,
                        max_tool_calls=self.plan_max_tool_calls,
                    ) or {}
                except Exception as e:
                    self._archive_invalid("planner_failed", parent, parents, genid, str(e), outer=outer, inner=inner)
                    self._save_checkpoint("inner", outer, inner)
                    continue

                try:
                    child = self.task_runner(plan=plan_result, parent=parent, genid=genid,
                                             base=base)
                except Exception as e:
                    self._archive_invalid("task_runner_failed", parent, parents, genid, str(e), outer=outer, inner=inner)
                    self._save_checkpoint("inner", outer, inner)
                    continue
                if child is None:
                    self._archive_invalid("task_runner_none", parent, parents, genid, outer=outer, inner=inner)
                    self._save_checkpoint("inner", outer, inner)
                    continue

                # DAG: record all parents used
                child.parents = [p.genid for p in parents]
                child.parent_genid = parent.genid
                plan_cfg = plan_result.get("config")
                child.meta["config_dict"] = plan_cfg if isinstance(plan_cfg, dict) else parent_cfg
                if base and self.code_root:
                    # audit: per-generation ref pinned by the patch application
                    self.log_event({"type": "code_branch", "genid": str(genid),
                                    "ref": f"task_{genid}",
                                    "commit": child.meta.get("code_commit"),
                                    "base": child.meta.get("base_commit"),
                                    "applied": bool(child.meta.get("patch_applied")),
                                    "ref_ok": child.meta.get("code_ref_ok", True),
                                    "ref_mode": child.meta.get("code_ref_mode", "")})

                # score / imputation / validity
                self._finalize_child(child, parent)

                # archive the current generation's task trajectory BEFORE evaluation,
                # so the evaluator can read it (current generation).
                try:
                    trajectory.collect(child.meta.get("run_dir", ""), f"gan_{genid}",
                                       self.output_dir, outer, genid)
                except Exception:
                    pass

                # evaluator (execute even for missing bench; pass imputed info, not as anchor)
                benchmark_for_eval = None if child.meta.get("imputed") else child.value.score
                # BLIND hygiene: never expose the objective score/accuracy in the
                # blind-phase run summary (score is revealed separately).
                meta_view = {k: v for k, v in (child.meta or {}).items()
                             if k not in ("report_summary", "delta_vs_parent")}
                report_view = {k: v for k, v in (child.meta.get("report_summary") or {}).items()
                               if k not in ("overall_accuracy", "random_guess_accuracy")}
                try:
                    ctx = self.evaluator.evaluate(
                        run_summary={
                            "genid": genid,
                            "meta": meta_view,
                            "report_summary": report_view,
                            "score_status": child.value.score_status,
                            "imputed": bool(child.meta.get("imputed")),
                            "receipt": self._last_receipt,
                        },
                        prev_feedback={**(self._last_feedback or {}), "digest": self._last_digest},
                        benchmark_score=benchmark_for_eval,
                        node_id=genid,
                        broker=self.broker,
                        task_brief=self.task_brief,
                        trajectory_genids=[genid] + parent_ref,
                    )
                except Exception as e:
                    child.valid_parent = False
                    child.meta["invalid"] = True
                    child.meta["invalid_reason"] = "evaluator_failed"
                    child.meta["invalid_detail"] = str(e)[:500]
                    self.task_tree.add_node(child)
                    try:
                        shutil.rmtree(paths.work_dir(self.output_dir, genid), ignore_errors=True)
                    except Exception:
                        pass
                    self._write_invalid(genid, "evaluator_failed", str(e), outer, inner)
                    self.log_event({"type": "inner_invalid", "genid": genid,
                                    "reason": "evaluator_failed", "detail": str(e)[:300]})
                    self._save_checkpoint("inner", outer, inner)
                    continue

                packet = self._build_packet(child, ctx)
                if getattr(ctx, "predicted_score", None) is not None:
                    child.value.potential = ctx.predicted_score
                # Persist calibration values on the node so a later branch can
                # use the selected parent's history rather than loop order.
                child.meta["predicted_score"] = getattr(ctx, "predicted_score", None)
                child.meta["benchmark_score"] = child.value.score
                run_dir = paths.runs_dir(self.output_dir, genid)
                os.makedirs(run_dir, exist_ok=True)
                packet.save(os.path.join(run_dir, "packet.json"))
                child.meta["packet_path"] = os.path.join(run_dir, "packet.json")

                child.source_access_log = self._source_access_log(outer)
                self.task_tree.add_node(child)
                self._append_score(child)
                self._write_eval(child, ctx, packet)
                self._last_receipt = self._make_receipt(genid, plan_result, parent_cfg, child, outer)
                child.meta["receipt"] = self._last_receipt
                if child.meta.get("task_patch_files") and self.broker is not None:
                    self._resync_workspace(outer, child.meta["task_patch_files"])
                if not self.keep_workdirs:
                    shutil.rmtree(paths.work_dir(self.output_dir, genid), ignore_errors=True)

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
                child.meta["feedback"] = self._last_feedback
                # The node was added before evaluator results and receipt were
                # available; append a durable meta update so replayed trees
                # retain all parent-context fields.
                self.task_tree.update(child.genid, meta=child.meta)
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
            # visible task trajectories for self-improvement: this outer's gens
            # + the direct parent of the last generation (read-only).
            si_refs = list(outer_genids)
            if outer_genids:
                last = self.task_tree.get(outer_genids[-1])
                p = getattr(last, "parent_genid", None) if last is not None else None
                if p is not None and str(p) != "initial" and p not in si_refs:
                    si_refs.append(p)
            try:
                res = self.evaluator.self_improve(recent={**recent_eval, "receipt": self._last_self_receipt},
                                                  broker=self.broker,
                                                  max_tool_calls=self.self_improve_max_tool_calls,
                                                  trajectory_genids=si_refs,
                                                  patch_retry_k=self.patch_retry_k)
                self._apply_self_patch("evaluator", outer, res)
                self._make_self_receipt("evaluator", outer, res)
            except Exception as e:
                self.log_event({"type": "self_improve_error", "role": "evaluator", "error": str(e)})
            try:
                res = self.planner.self_improve(recent={**recent_plan, "receipt": self._last_self_receipt},
                                                broker=self.broker,
                                                max_tool_calls=self.self_improve_max_tool_calls,
                                                trajectory_genids=si_refs,
                                                patch_retry_k=self.patch_retry_k)
                self._apply_self_patch("planner", outer, res)
                self._make_self_receipt("planner", outer, res)
            except Exception as e:
                self.log_event({"type": "self_improve_error", "role": "planner", "error": str(e)})

            # Acceptance: none. Single chain — the self-improved design is kept and
            # becomes the active design for the next outer's fresh instances.
            # `outer_improved` is recorded for analysis only (no revert).
            self.log_event({"type": "self_improve_kept", "outer": outer,
                            "improved": outer_improved})

            self.evaluator_tree.add_node(Node(genid=f"eval_{outer}", meta={"num_digests": len(self._digests)}))
            self.planner_tree.add_node(Node(genid=f"plan_{outer}", meta={"advantages": self._advantages[-I_max:]}))
            self.log_event({"type": "outer_done", "outer": outer, "improved": outer_improved})
            self._save_checkpoint("outer", outer, I_max)

        return self.task_tree
