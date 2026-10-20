"""Dual-loop scheduler.

Outer loop = one meta generation (planner + evaluator self-improve).
Inner loop = one task-agent generation (plan -> task run -> evaluate -> commit).

Collaborators are duck-typed so the loop can be smoke-tested offline; every
call is made by KEYWORD (see ``run``), so the lists below only name the main
inputs:
    planner.plan(parent_summary, parents, parent_feedback, evaluator_issues, config, node_id, broker)
        -> {"records": [...], "responses": [...], "config": Config, "patch": str}
    task_runner(plan, parent, genid, base) -> Node
    evaluator.evaluate(run_summary, parent_feedback, benchmark_score, node_id, broker) -> EvalContext-like
    planner.self_improve(recent) / evaluator.self_improve(recent)

Evaluator feedback is TEXT (a digest), not a numeric reward.

Failure semantics:
- planner / task / evaluator execution failure => the round is INVALID: archived
  but never a parent.
- bench score completely missing => imputed from same-parent siblings' median
  (else the parent's score); imputed scores are NOT used as the evaluator's anchor.
- partial coverage => score kept, ``score_status="partial"``, ``coverage`` recorded.

Checkpointing: ``checkpoint.json`` is the latest snapshot; outer boundaries
additionally write an immutable numbered ``ckpt/outer_<N>.json``. An outer
checkpoint is written after that outer's self-improvement, so it carries the design
used by the next outer. ``resume=True`` continues and ``rollback_to_outer()`` reverts
to the last outer boundary (append-only ``op=reset`` marker, no log truncation).

Instances: planner/evaluator are refreshed every outer generation
(``_refresh_roles``) from the active design, with a cleared source workspace; task
agents are refreshed every inner generation. Scheduling is a **single chain** — no
accept/reject and no parent selection for roles; ``outer_improved`` is logged only.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import statistics
import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple

from gan.design import initial_config
from gan.framework import checkpoint as ckpt
from gan.framework import cost_ledger
from gan.framework import cost_keys
from gan.framework import paths, scores, trajectory
from gan.framework import ledger as ledger_mod
from gan.framework.loader import load_registry, resolve_domain
from gan.framework.reward.evaluator_reward import build_feedback_digest
from gan.framework.reward.packet import RewardPacket
from gan.summary import build_diff_summary, make_feedback, project_responses_for_evaluator
from gan.framework.tree.store import Node, NodeValue, TreeStore

# The allowlist of node-meta keys the EVALUATOR may see in its
# run summary. Everything else in `child.meta` is the planner's decision
# paperwork — notably `records`, which carries the planner's respond_issue
# free text (its argumentation to the evaluator). Default-deny: a new meta key
# is invisible to the evaluator until it is added here on purpose.
_EVALUATOR_META_KEYS = (
    "task_patch_files",        # which files this generation changed
    "patch_applied",
    "task_code_commit", "code_commit", "base_commit",
    "task_patch_rejected",     # patch outcome
    "toolset_report",          # design-claims vs assembly
    "design_stripped",         # healing record
    "harness_rc", "invalid_reason", "score_status", "imputed",
    "modify_depth",
    # deliberately NOT here: "records" (planner rationale), "receipt" (projected
    # separately, see _receipt_for_evaluator), "delta_vs_parent" (selection
    # pressure must not be fittable), "feedback", "run_dir"/"report_path"/
    # "report_sha"/"design_path"/"model" (paperwork, not judgement input).
)


def _receipt_for_evaluator(receipt):
    """The evaluator's projection of the plan receipt — outcomes only.

    The FULL receipt is the planner's decision input (``render_receipt``) and
    must not reach the evaluator wholesale: ``code_patch.rejected_reason``
    quotes the planner's own artifacts (comments/identifiers inside compile
    errors), ``grants`` carry agent free text, and ``design.ops`` duplicate the
    diff_summary channel. The evaluator calibrates on patch OUTCOMES
    (applied/rejected/budget), not on the planner's paperwork.
    """
    if not isinstance(receipt, dict):
        return {}
    cp = receipt.get("code_patch") or {}
    # Cost projection: TASK-ONLY (docs/10 §4). The planner's plan-session
    # cost is the planner's own business; the evaluator never sees it here
    # (its own costs arrive via its cost statement / self-improve payload).
    return {
        # The lagged facts below describe the PARENT round -- label them with
        # the receipt's own genid so the evaluator cannot misread them as
        # current-round events. ``toolset`` stays: it is inherited-baseline
        # context, not a current-round event.
        "round": receipt.get("genid"),
        "budget": receipt.get("budget") or {},
        "cost": {"task": (receipt.get("cost") or {}).get("task")},
        "design_stripped": receipt.get("design_stripped") or [],
        "toolset": receipt.get("toolset") or {},
        "code_patch": {"proposed": cp.get("proposed"), "applied": cp.get("applied")},
        "patch_rejected": bool(cp.get("rejected_reason")),
    }
from utils.soft_fail import soft_fail

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
        ledger_writer: Optional[Any] = None,
        roots: Optional[Any] = None,
    ):
        self.output_dir = os.path.abspath(output_dir)
        # B46 step-1 gate: the per-run WriterToken guards every decision-grade
        # artifact write (scores / design configs / checkpoints). build_gan_loop
        # mints one and threads it everywhere; a directly-constructed loop
        # auto-mints so legacy constructions keep working (the token gates the
        # WRITE API surface, it is not a secret).
        self.ledger_writer = ledger_writer or ledger_mod.open_run(self.output_dir)
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
        # Cost ledger (phases A/B, docs/10 §9): per-session cost records +
        # framework-rendered cost statements. ``cost.record_usage`` gates the
        # RUNTIME emit only -- the ledger still aggregates offline (A3).
        # ``cost.token_budget_per_gen`` is the statements' reference line here;
        # its clamp consumer arrives with the reserved-key phase (C).
        self.record_usage = bool(cfg.get("cost.record_usage", True))
        self.token_budget_per_gen = cfg.get("cost.token_budget_per_gen")
        # Session cost records settled by THIS loop (outer summary + audit).
        self._session_costs: List[Dict[str, Any]] = []
        self.self_improve_max_tool_calls = int(cfg.get("loop.self_improve_max_tool_calls", 30))
        self.plan_max_tool_calls = int(cfg.get("loop.plan_max_tool_calls", 40))
        # The evaluate seat has its OWN budget: it must at minimum complete the
        # seat's mandatory eval-points, so reusing the planner's (smaller) plan
        # budget starves it. Counted per phase (blind / reveal-thirds), with the
        # identical-call breaker (``tool_repeat_limit``) as the anti-waste net.
        self.evaluate_max_tool_calls = int(cfg.get("loop.evaluate_max_tool_calls", 30))
        self.tool_repeat_limit = int(cfg.get("loop.tool_repeat_limit", 3))
        self.patch_retry_k = int(cfg.get("loop.patch_retry_k", 2))
        # C batch: per-CALL tool budget stamped onto every refreshed role
        # instance (dispatch-level net lives in agent.llm_withtools; roles pass
        # the instance attribute through Role.run).
        self.tool_call_timeout_s = int(cfg.get("loop.tool_call_timeout_s", 600))
        self.code_root = os.path.abspath(code_root) if code_root else None
        # Split layout (docs/12): per-owner repo roots; single layout == code_root.
        from gan.framework.repo_roots import RepoRoots
        self.roots = roots if roots is not None else RepoRoots.single(self.code_root)
        # The rolling mirrors below carry PARENT-CHILD semantics only --
        # they are refreshed at parent selection (from parent.meta) and are NEVER
        # written at settle time. The durable per-round record lives on the child
        # node meta (``feedback`` / ``feedback_digest`` / ``receipt``), which the
        # next selection reads back -- so nothing here needs checkpointing.
        self._parent_receipt: Optional[Dict[str, Any]] = None
        # Per-role self receipts: one entry per role, so a role can never render
        # another role's receipt.
        self._self_receipts: Dict[str, Dict[str, Any]] = {}
        # Per-role design as it was BEFORE this outer's self-improve sessions
        # ran. The exit heal compares against it to tell a name the session
        # DROPPED from one that was never declared -- restoring the former is the
        # reverse-dangling alignment, restoring the latter would be a guess.
        # In-memory only (no disk write, no checkpoint).
        self._self_design_pre: Dict[str, Dict[str, Any]] = {}
        # Names stripped from a role design by the STARTUP repair; merged
        # into that role's first self receipt so the actionable note reaches it
        self.preflight_design_stripped: Dict[str, list] = {}
        # run-attempt id: identifies THIS loop instantiation (one per outer worker /
        # in-process run). Outer-level trajectory files are keyed by it so that
        # re-running an outer never truncates or mixes a previous attempt's traces.
        self._attempt = uuid.uuid4().hex[:8]
        # role self-edit commits made by this run (audit)
        self._role_commits: Dict[str, str] = {}
        os.makedirs(self.output_dir, exist_ok=True)
        self.task_tree = TreeStore(self.output_dir, "task").load()
        self.planner_tree = TreeStore(self.output_dir, "planner").load()
        self.evaluator_tree = TreeStore(self.output_dir, "evaluator").load()
        self._gen_counter = self._max_int_genid() + 1
        # rolling state
        self._parent_feedback: Optional[Dict[str, Any]] = None
        self._parent_digest: Optional[str] = None
        # Kept assigned + checkpointed only for state-file back-compat; the
        # digest reads the selected parent's meta instead.
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
            except Exception as e:
                # A resolution failure (broken registry / schema) must abort the
                # run — silently degrading the whole run to an empty task brief
                # would strip every prompt of its task definition with zero signal.
                # (Domains may legitimately declare no task_brief: an empty value
                # stays allowed and is recorded below.)
                raise RuntimeError(
                    f"cannot resolve task_brief for domain {self.domains[0]!r}: {e} "
                    f"— the framework-injected task definition is missing, the run "
                    f"cannot proceed"
                ) from e
            if not self.task_brief:
                self.log_event({"type": "task_brief_missing", "domain": self.domains[0]})

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
        cfg = deepcopy(cd) if cd else self._initial_task_config()
        # Fold legacy slot names so a resumed pre-unification checkpoint
        # cannot pass ``skills``/``eval_points`` down the inheritance chain
        from gan.design.schema import normalize_config
        return normalize_config(cfg)

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
                "weaknesses": getattr(ctx, "weaknesses", []),
            },
            penalties=dict(getattr(ctx, "penalties", {}) or {}),
        )

    def _settle_feedback_digest(
        self,
        plan_result: Dict[str, Any],
        ctx: Any,
        genid: Any,
        parent_feedback: Optional[Dict[str, Any]],
        parent_meta: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Build the TEXT digest pairing THIS round's planner responses with the
        SELECTED parent's issues (parent-child semantics; the planner
        session whose ``plan_result`` we are settling responded to exactly these
        issues). Calibration reads the parent's persisted predicted/benchmark
        (per-node) instead of loop-local rolling values.

        ``diff_summary`` is NOT an input: the planner-action inventory rides its
        own named projection (the ``feedback.diff_summary`` JSON block, written
        by the caller alongside this digest), not the calibration narrative."""
        if not parent_feedback:
            return None
        prev_issues = parent_feedback.get("issues") or []
        if not prev_issues:
            return None
        digest = build_feedback_digest(
            issues=prev_issues,
            # The ONLY planner→evaluator projection of issue
            # responses — free-text `feedback` never reaches the reward module.
            responses=project_responses_for_evaluator(plan_result.get("responses") or []),
            verdicts=getattr(ctx, "fix_verdicts", []) or [],
            predicted_score=(parent_meta or {}).get("predicted_score"),
            benchmark_score=(parent_meta or {}).get("benchmark_score"),
            # The patch outcome is session-level fact; a rejected patch means
            # "acted" stances above may not have materialized (rendered as a tail
            # line by the digest builder).
            patch_outcome={"rejected": bool(plan_result.get("patch_rejection")),
                           "attempts": plan_result.get("attempts", 0)},
            # The label rides the digest HEADER: the self_improve view
            # concatenates the last digests, and without it they cannot be
            # attributed to their planner rounds.
            round_label=str(genid),
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
        # Task-child cost (tokens + wall clock) from the proxy audit; recorded
        # on every settled child so packet.json / UCB cost_penalty see real
        # numbers instead of a permanent None. (Moved to cost_ledger, phase A.)
        cost = cost_ledger.task_cost_for_gen(self.output_dir, child.genid)
        if cost is not None:
            child.value.cost_tokens, child.value.cost_wallclock_s = cost
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

    # -- cost ledger (phases A/B, docs/10 §9) -------------------------------
    def _record_session_cost(self, role: str, seat: str, outer: Any,
                             genid: Any = None,
                             cm_session: Any = None) -> Optional[Dict[str, Any]]:
        """Settled-session cost record (A5): append to
        ``logs/cost_sessions.jsonl`` + a ``cost_session`` event.

        Audit-only: a ledger failure is surfaced via ``soft_fail`` and never
        fails the generation. genid-keyed seats (plan/evaluate) read the
        session trajectory directly; the self-improve seat resolves the role's
        newest outer-level file (canonical or attempt-keyed — the loop binds
        the attempt-keyed sink, which sorts newest)."""
        if not self.record_usage:
            return None
        try:
            if genid is not None:
                rec = cost_ledger.session_cost_from_file(
                    paths.session_traj_file(self.output_dir, outer, genid, role),
                    role=role, seat=seat, outer=outer, genid=genid,
                    output_dir=self.output_dir)
            else:
                recs = cost_ledger.self_session_costs(self.output_dir, outer, role)
                rec = recs[0] if recs else None
            if rec is None:
                return None
            if cm_session is not None:
                try:
                    cm_sum = cm_session.summary()
                    if cm_sum and cm_sum.get("turns"):
                        rec["cm"] = {k: cm_sum[k] for k in
                                     ("mode", "turns", "blocks", "ops_total",
                                      "ops_applied", "ops_refused",
                                      "policy_failures", "policy_disabled",
                                      "violations", "est_chars_saved",
                                      "emit_failures")
                                     if k in cm_sum}
                except Exception:  # noqa: BLE001 -- audit-only enrichment
                    pass
            # 幂等：同 key 记录已存在时不重复写入文件（防止 reconcile 重复
            # 结算产生重复行）。复用已有记录并保留会话级记账与事件暴露。
            key = (rec.get("role"), rec.get("seat"),
                   str(rec.get("outer")), str(rec.get("genid")), rec.get("attempt"))
            existing = [
                old for old in cost_ledger.read_session_costs(self.output_dir)
                if (old.get("role"), old.get("seat"),
                    str(old.get("outer")), str(old.get("genid")), old.get("attempt")) == key
            ]
            if existing:
                rec = existing[-1]
            else:
                cost_ledger.append_session_cost(self.output_dir, rec)
            self._session_costs.append(rec)
            self.log_event({"type": "cost_session",
                            **{k: rec.get(k) for k in
                               ("role", "seat", "outer", "genid", "attempt", "records",
                                "llm_calls", "tool_calls", "tool_outputs", "prompt_tokens",
                                "completion_tokens", "reasoning_tokens", "total_tokens",
                                "wallclock_s")}})
            return rec
        except Exception as e:  # noqa: BLE001 -- audit-only, never fatal
            soft_fail(f"cost_session record failed ({role}/{seat}, outer {outer}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_session_record_failed")
            return None

    def _write_gen_cost(self, genid: Any, outer: Any) -> None:
        """``runs/<genid>/cost.json`` (A5): the generation's settled sessions —
        task child from the proxy audit, plan/evaluate from trajectories, plus
        the task child's trajectory-side cost as the reconciliation sibling.
        Evidence file only (same bucket as packet.json / patch_receipt.json);
        a write failure is surfaced, never fatal."""
        if not self.record_usage:
            return
        try:
            task = cost_ledger.task_cost_for_gen(self.output_dir, genid)
            sess = cost_ledger.gen_session_costs(self.output_dir, outer, genid)
            task_traj = cost_ledger.task_trajectory_cost(self.output_dir, outer, genid)
            task_cm = cost_ledger.task_cm_summary(self.output_dir, genid)
            payload: Dict[str, Any] = {
                "genid": str(genid), "outer": outer,
                "task": ({"cost_tokens": task[0], "cost_wallclock_s": task[1],
                          "source": "proxy_audit"} if task is not None else None),
                # Task child's seam audit (docs/10 §11.5): ingested sidecar
                # events. None is now ALWAYS explained: the runner emits
                # cm_events_ingest_empty / the child emits
                # cm_session_degraded, so a bare null here would mean the
                # cost record was written before the ingest ran (a bug).
                "task_cm": (task_cm if task_cm is not None else
                            {"absent": True,
                             "reason": "no role=task cm events ingested for "
                                       "this generation (see cm_events_ingest_empty "
                                       "/ cm_session_degraded events)"}),
                "task_trajectory": ({"total_tokens": task_traj.get("total_tokens"),
                                     "wallclock_s": task_traj.get("wallclock_s"),
                                     "llm_calls": task_traj.get("llm_calls"),
                                     "tool_calls": task_traj.get("tool_calls"),
                                     "source": "trajectory"} if task_traj else None),
                "planner_plan": cost_ledger.compact(sess.get("planner_plan")),
                "evaluator_evaluate": cost_ledger.compact(sess.get("evaluator_evaluate")),
            }
            d = paths.runs_dir(self.output_dir, genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "cost.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001 -- evidence file, never fatal
            soft_fail(f"cost.json write failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_json_write_failed", genid=str(genid))

    def _cost_statement_plan(self, parent: Any, outer: Any, genid: Any,
                             history_genids: List[Any],
                             task_budgets: Optional[Dict[str, Any]] = None,
                             own_budget: Any = None) -> str:
        """Framework-rendered cost statement for the planner's plan session
        (B2). Advisory: a render failure degrades to an empty statement and is
        surfaced, never fatal. ``task_budgets``/``own_budget`` are the C-phase
        effective reserved keys (docs/10 §10): what the task child's session
        will be governed by + the planner's own session budget."""
        if not self.record_usage:
            return ""
        try:
            parent_cost = (cost_ledger.task_cost_for_gen(self.output_dir, parent.genid)
                           if str(parent.genid) != "initial" else None)
            bulk = cost_ledger.task_costs_for_gens(
                self.output_dir, [g for g in history_genids if g != genid])
            history = [{"genid": g, "cost_tokens": bulk[str(g)][0]}
                       for g in history_genids if str(g) in bulk]
            return cost_ledger.render_plan_statement(
                parent_genid=parent.genid,
                parent_task_cost=parent_cost,
                history_costs=history,
                own_last=cost_ledger.latest_session_cost(self.output_dir, "planner", "plan"),
                token_budget=self.token_budget_per_gen,
                task_budgets=task_budgets, own_budget=own_budget)
        except Exception as e:  # noqa: BLE001 -- advisory, never fatal
            soft_fail(f"plan cost statement build failed: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_statement_build_failed", seat="plan")
            return ""

    def _cost_statement_eval(self, genid: Any,
                             task_cost: Optional[tuple] = None,
                             own_budget: Any = None) -> str:
        if not self.record_usage:
            return ""
        try:
            return cost_ledger.render_evaluate_statement(
                genid=genid, task_cost=task_cost,
                own_last=cost_ledger.latest_session_cost(
                    self.output_dir, "evaluator", "evaluate", exclude_genid=genid),
                own_budget=own_budget,
                token_budget=self.token_budget_per_gen)
        except Exception as e:  # noqa: BLE001 -- advisory, never fatal
            soft_fail(f"evaluate cost statement build failed: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_statement_build_failed", seat="evaluate")
            return ""

    def _cost_statement_self(self, role: str, outer: Any) -> str:
        if not self.record_usage:
            return ""
        try:
            # cost×score per-round alignment: map every generation already
            # settled this outer to its measured score, so the statement can
            # pair each round's spend with its result (n/a when unpublished).
            scores_by_genid = {}
            for node in self.task_tree.nodes.values():
                score = getattr(node.value, "score", None)
                if score is not None:
                    scores_by_genid[str(node.genid)] = score
            return cost_ledger.render_self_statement(
                role=role, outer=outer,
                own_sessions=cost_ledger.self_session_costs(self.output_dir, outer, role),
                own_last=cost_ledger.latest_session_cost(self.output_dir, role, "self_improve"),
                scores_by_genid=scores_by_genid)
        except Exception as e:  # noqa: BLE001 -- advisory, never fatal
            soft_fail(f"self-improve cost statement build failed ({role}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_statement_build_failed", seat="self_improve")
            return ""

    def _cost_statement_accepted(self, fn: Any, name: str = "cost_statement") -> bool:
        """Signature probe: older role source (a resumed checkpoint's code
        tree) may predate the ``cost_statement`` kwarg — the statement is then
        silently absent for that session (audited, never a TypeError)."""
        try:
            return name in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def _accepts_kwarg(self, fn: Any, name: str) -> bool:
        return self._cost_statement_accepted(fn, name)

    def _effective_cost_keys(self, role: str,
                             fallbacks: Optional[Dict[str, Any]] = None
                             ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Effective reserved cost keys for a role session (docs/10 §10, phase C).

        Reads the role's OWN design (归位规则: whose keys live in whose design)
        through the frozen cost-key table — the single clamp authority. Advisory:
        any failure degrades to the fallbacks/defaults (never fatal).
        """
        try:
            from gan.design.store import DesignStore
            store = DesignStore(paths.design_root(self.output_dir))
            cfg = store.load(role)
        except Exception as e:  # noqa: BLE001 -- advisory
            soft_fail(f"cost-key design read failed ({role}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cost_keys_apply_failed", role=role)
            cfg = {}
        return cost_keys.effective_all(role, cfg, fallbacks=fallbacks)

    def _emit_cost_keys_applied(self, role: str, seat: str, genid: Any,
                                keys: Dict[str, Any],
                                clamps: List[Dict[str, Any]]) -> None:
        """C-phase audit fact (docs/10 §10.3 判据 1): which reserved keys are
        IN FORCE for this session, and which design requests were clamped."""
        self.log_event({"type": "cost_keys_applied", "role": role, "seat": seat,
                        "genid": str(genid), "keys": dict(keys or {}),
                        "clamped": list(clamps or [])})

    def _resolve_cm_module(self, role: str, stem: Any,
                           outer: Any = None) -> Optional[str]:
        """Resolve a design-named CM module
        (``cost.cm_policy`` / ``cost.cm_assembly``) to a loadable path —
        WORKSPACE-FIRST (the role's live self-edit surface for this outer),
        committed tree second, the same rule ``list_components`` and the deep
        tools scan with. Resolved ONCE at session build; the session runs
        that version all session long (no mid-session hot reload). No
        sort-order discovery: the named slot is the only form for in-process
        roles — an unnamed or unresolvable claim degrades LOUDLY (the caller
        emits ``cm_module_unresolved``)."""
        if not isinstance(stem, str) or not stem.strip():
            return None
        stem = stem.strip()
        rel = os.path.join("gan", "components", role, "cm", stem + ".py")
        if outer is not None and self.broker is not None:
            try:
                cand = os.path.join(self.broker.src_dir(role, f"outer_{outer}"),
                                    rel)
                if os.path.isfile(cand):
                    return cand
            except Exception:  # noqa: BLE001 -- advisory resolution; the
                # committed-tree fallback below still applies
                pass
        cm_root = self._roles_root()
        if cm_root:
            cand = os.path.join(cm_root, rel)
            if os.path.isfile(cand):
                return cand
        return None

    def _build_cm_session(self, role: str, keys: Optional[Dict[str, Any]],
                          outer: Any = None) -> Any:
        """Per-session context-management session (docs/10 §11 D11): built from
        this role's ALREADY-CLAMPED effective ``cm_*`` keys; events ride the
        framework event stream (role-attributed). Advisory: any failure
        degrades to no seam (pre-D behaviour), never fatal.

        The design may NAME agent-owned CM modules —
        resolved workspace-first at build time and handed to the session, so
        the execution contract (load gate, per-turn budget, wedge breaker,
        loud fallback) applies unchanged in-process. A named module that
        cannot be resolved is a LOUD claim-vs-tree gap
        (``cm_module_unresolved``): an assembly claim degrades to the
        baseline preset; ``cm_mode=custom`` without a resolvable policy
        degrades to the slice preset — the keys' ``cm_mode`` is rewritten
        too, so the degrade says what it actually runs (rewriting only a
        local variable would leave the session building ``custom`` and
        failing on the missing policy every turn)."""
        if keys is None:
            return None
        try:
            from gan.framework.cm import build_session
            cm_keys = {k: v for k, v in keys.items()
                       if isinstance(k, str) and k.startswith("cm_")}
            # Named slots: resolve claims against the role's OWN tree.
            assembly_path = None
            named_asm = cm_keys.get("cm_assembly")
            if isinstance(named_asm, str) and named_asm.strip():
                assembly_path = self._resolve_cm_module(role, named_asm, outer)
                if assembly_path is None:
                    soft_fail(
                        f"cm_assembly={named_asm.strip()!r} unresolved for "
                        f"{role} (no workspace/committed module); the "
                        "baseline preset runs instead",
                        event_path=paths.events_path(self.output_dir),
                        event_type="cm_module_unresolved", role=role,
                        kind="assembly", stem=named_asm.strip())
            policy_path = None
            if cm_keys.get("cm_mode") == "custom":
                named_pol = cm_keys.get("cm_policy")
                if isinstance(named_pol, str) and named_pol.strip():
                    policy_path = self._resolve_cm_module(role, named_pol, outer)
                if policy_path is None:
                    stem = (named_pol.strip()
                            if isinstance(named_pol, str) and named_pol.strip()
                            else None)
                    soft_fail(
                        f"cm_mode=custom without a resolvable policy for "
                        f"{role} (stem={stem!r}); degrading to the slice "
                        "preset for this session",
                        event_path=paths.events_path(self.output_dir),
                        event_type="cm_module_unresolved", role=role,
                        kind="policy", stem=stem)
                    # The degrade CLAIMS the slice preset — the keys must say
                    # it too, or apply_keys would still build a custom
                    # session whose every turn fails on the missing policy.
                    cm_keys = dict(cm_keys)
                    cm_keys["cm_mode"] = "slice"
            session = build_session(cm_keys, policy_path=policy_path,
                                    assembly_path=assembly_path)
            session.emit = (lambda r: (lambda e: self.log_event(
                {**e, "role": r})))(role)
            return session
        except Exception as e:  # noqa: BLE001 -- advisory
            soft_fail(f"cm session build failed ({role}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="cm_session_failed", role=role)
            return None

    def _traj_materialization_files(self, genids: Any, outer: Any = None,
                                    session_role: Optional[str] = None,
                                    session_outers: Any = None
                                    ) -> List[Tuple[str, str]]:
        """The seat's visible trajectory files as ``(label, path)`` pairs —
        the SAME set the read tools serve. Task generations ride the seat's
        ``trajectory_genids``; the self seat adds its OWN per-generation
        session files over the DECLARED own-session universe
        (``session_outers``: this outer + the previous outer, L65 ruling).
        Labels come from ``trajectory.session_label`` — the one place the
        label rule lives (genid-qualified for the current outer, outer-
        prefixed for extras) so the materialized address space and the read
        tool's fallback agree. Advisory: missing files are skipped by the
        materialization primitive."""
        out: List[Tuple[str, str]] = []
        seen = set()

        def _add(path: str, genid: Any) -> None:
            if not path or not os.path.isfile(path):
                return
            label = f"{genid}/{os.path.basename(path)}"
            if label not in seen:
                seen.add(label)
                out.append((label, path))

        for g in (genids or []):
            _add(trajectory._session_file(self.output_dir, g, "task"), g)
        if session_role and outer is not None:
            for label, path in trajectory.session_universe(
                    self.output_dir, session_role, outer,
                    extra_outers=session_outers):
                if label not in seen:
                    seen.add(label)
                    out.append((label, path))
        return out

    def _materialize_traj(self, cm_session: Any, genids: Any, outer: Any = None,
                          session_role: Optional[str] = None,
                          session_outers: Any = None) -> None:
        """Snapshot the seat's visible trajectories into its CM session (one
        scan; the read tools' index face and the ctx faces then share the
        same address space). Advisory: a failure leaves the session without
        materialization (the tools self-build) and SPEAKS; ``passthrough``
        sessions skip the scan entirely (identity view)."""
        if cm_session is None:
            return
        try:
            files = self._traj_materialization_files(genids, outer=outer,
                                                     session_role=session_role,
                                                     session_outers=session_outers)
            if files:
                cm_session.materialize_traj(files)
        except Exception as e:  # noqa: BLE001 -- advisory, must speak
            # the audit write itself must never turn an advisory failure
            # fatal: if the event path is unreachable, the raise stops here
            try:
                soft_fail(f"traj materialization failed: {e}",
                          event_path=paths.events_path(self.output_dir),
                          event_type="cm_traj_materialize_failed")
            except Exception:  # noqa: BLE001 -- audit sink unavailable
                pass

    def _task_budget_keys(self, parent: Any) -> Optional[Dict[str, Any]]:
        """Effective TASK-side reserved keys of the design the child will
        inherit (the selected parent's config) — the plan statement's
        "本代预算生效值" section (docs/10 §4). Ceiling applied."""
        try:
            cfg = self._parent_config(parent)
            effective, _clamps = cost_keys.effective_all(
                "task", cfg, ceiling_token_budget=self.token_budget_per_gen)
            out = {k: v for k, v in effective.items() if v is not None}
            return out or None
        except Exception:  # noqa: BLE001 -- advisory
            return None

    def _finalize_invalid_node(self, node: Node, reason: str, detail: str,
                               outer: Optional[int], inner: Optional[int]) -> None:
        """Shared tail of EVERY invalid-child path: archive the
        node, clean the work dir, persist the invalid record, log loudly -- and
        append a scores row so the generation is visible to score consumers.
        Authority evidence stays in ``runs/<genid>/``; the row (score None,
        score_status "invalid") only asserts that the generation existed and
        produced no measurable score."""
        self.task_tree.add_node(node)
        try:
            shutil.rmtree(paths.work_dir(self.output_dir, node.genid), ignore_errors=True)
        except Exception as e:
            soft_fail(f"work dir cleanup failed for genid {node.genid}: {e}")
        self._write_invalid(node.genid, reason, detail, outer, inner)
        self.log_event({"type": "inner_invalid", "genid": node.genid,
                        "reason": reason, "detail": detail[:300]})
        self._append_score(node)

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
        self._finalize_invalid_node(node, reason, detail, outer, inner)
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
        except Exception as e:
            soft_fail(f"invalid.json write failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="invalid_record_write_failed",
                      genid=str(genid), reason=reason)

    # -- checkpoints -------------------------------------------------------
    def _state_dict(self, outer: int, inner: int) -> Dict[str, Any]:
        return {
            "gen_counter": self._gen_counter,
            # parent_feedback / parent_digest / parent_receipt are NOT
            # checkpointed -- they are rebuilt from parent.meta at the next
            # selection; the per-round record lives on the tree.
            "digests": self._digests,
            # Per-role self receipts ride the checkpoint (consumed by the next
            # outer's self_improve; cannot be rebuilt from the tree).
            "self_receipts": dict(self._self_receipts),
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
        # Legacy ``last_feedback`` / ``last_digest`` state keys are deliberately
        # ignored (the mirrors are rebuilt from parent.meta at the next
        # selection).
        self._digests = list(state.get("digests") or [])
        self._self_receipts = dict(state.get("self_receipts") or {})
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
            # A git-layer failure must NOT be recorded as "no code layer" —
            # the resume path would then silently skip code-state restoration and
            # run on an undefined baseline. Let RepoIntegrityError abort the run.
            code = {"commit": code_repo.current_commit(self._task_root())}
            if self.roots.layout == "split":
                code["roles_commit"] = code_repo.current_commit(self._roles_root())
        ckpt.save_checkpoint(
            self.output_dir,
            boundary=boundary,
            state=self._state_dict(outer, inner),
            trees=ckpt.capture_trees(self.task_tree, self.planner_tree, self.evaluator_tree),
            designs=ckpt.snapshot_designs(self.output_dir, ["planner", "evaluator"]),
            index=(outer if boundary == "outer" else None),
            code=code,
            writer=self.ledger_writer,
        )

    def _refresh_roles(self, outer: int) -> None:
        """Rebuild planner/evaluator for a new outer generation (instance refresh).

        Each outer generation is a fresh role *instance*: fresh design load,
        fresh toolset (so selections take effect), fresh chat history, and a
        cleared source workspace. The active design on disk is the one saved
        after the previous outer's self-improvement (single chain, no revert).
        Without a factory the injected instances are reused (offline/smoke mode).
        """
        # Attempt-aware construction: the loop's per-instantiation attempt id
        # must be known BEFORE the role binds its trajectory sink (Role.__init__
        # resolves the OUTER-level file exactly once), otherwise session content
        # and stop records can land on different names. Signature probing keeps
        # legacy single-arg factories (tests, injected instances) working.
        if self.broker is not None:
            for role in ("planner", "evaluator"):
                self.broker.clear_workspace(role)
        for name, factory in (("planner", self.planner_factory),
                              ("evaluator", self.evaluator_factory)):
            if factory is None:
                continue
            try:
                params = inspect.signature(factory).parameters
                accepts_attempt = any(
                    p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    for p in list(params.values())[1:]
                ) or any(p.kind == p.VAR_POSITIONAL for p in params.values())
            except (TypeError, ValueError):
                accepts_attempt = False
            previous = getattr(self, name, None)
            replacement = (factory(outer, self._attempt) if accepts_attempt
                           else factory(outer))
            setattr(self, name, replacement)
            close = getattr(previous, "close", None)
            if previous is not replacement and callable(close):
                close()
        for inst in (self.planner, self.evaluator):
            try:
                inst.attempt_id = self._attempt
            except Exception as e:
                soft_fail(f"attempt_id assignment failed for "
                          f"{getattr(inst, 'role', '?')}: {e}")
            # Stamp the per-call tool budget (Role.run picks the attribute
            # up when the caller does not pass tool_timeout_s explicitly).
            try:
                inst.tool_timeout_s = self.tool_call_timeout_s
            except Exception as e:
                soft_fail(f"tool_timeout_s stamp failed for "
                          f"{getattr(inst, 'role', '?')}: {e}")
        self.log_event({"type": "role_refresh", "outer": outer})

    def _source_access_log(self, outer: Any) -> List[Dict[str, Any]]:
        """Audit of source paths granted to roles for this outer instance."""
        if self.broker is None:
            return []
        akey = f"outer_{outer}"
        out: List[Dict[str, Any]] = []
        for role, seats in (("planner", ("plan", "self_improve")),
                            ("evaluator", ("evaluate", "self_improve"))):
            for seat in seats:
                out.extend(self.broker.grants.get((role, f"{akey}__{seat}"), []))
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
            }, writer=self.ledger_writer)
        except Exception as e:
            soft_fail(f"scores append failed for genid {child.genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="scores_append_failed", genid=str(child.genid))

    def _apply_self_patch(self, role: str, outer: int, res: Any) -> None:
        """Apply a role self-edit patch directly to the code tree (option B).

        The durable record is the code commit; no separate patch file is kept.
        Validation failure rolls back to the previous commit.

        EVERY exit then heals the role's design file (``_heal_self_design``):
        the session already persisted its draft (``Role.save_self_config`` runs
        at session end, before the loop learns the outcome), so an exit that
        leaves it un-healed keeps a design that disagrees with the committed
        tree -- forward dangling names after a rejected patch, and silently
        dropped selections when a scheduled removal never landed. The heal's
        ``stage`` names the exit.
        """
        patch = (res or {}).get("patch") if isinstance(res, dict) else None
        if not patch:
            rejected = (res or {}).get("patch_rejection") if isinstance(res, dict) else None
            if rejected:
                self.log_event({"type": "self_improve_rejected", "role": role,
                                "outer": outer, "reason": str(rejected)[:300]})
            # No commit landed: the outer's entry commit is the authority for
            # what this role can assemble, and the draft on disk may already
            # disagree with it. Align it (a rejected or abandoned patch must not
            # leave the design claiming a state the tree never reached).
            self._heal_self_design(role, outer, res,
                                   stage="self_improve_exhausted",
                                   outcome=("exhausted" if rejected else "empty_patch"))
            return
        roles_root = self._roles_root()
        if not roles_root:
            return
        from gan.framework import code_repo
        prev = code_repo.current_commit(roles_root)
        try:
            sha = code_repo.apply_self_patch(roles_root, role, patch, seat="self_improve")
            self._role_commits[role] = sha
            self.log_event({"type": "self_improve_commit", "role": role,
                            "outer": outer, "commit": sha})
            # The successful commit makes the committed registry the new
            # authority, so any dangling name in the just-saved self design
            # (e.g. an unregister whose deselect was forgotten) is stripped and
            # the file re-saved -- "persisted design == committed tree
            # deliverable" holds for role designs on this path too. The stripped
            # names ride the self receipt (design_stripped) so the role's next
            # self-improve sees its own heal.
            self._heal_self_design(role, outer, res,
                                   stage="self_improve_success", outcome="applied")
        except code_repo.PatchRejected as e:
            # agent-attributable rejection: recover to the previous commit and
            # keep the run going (B-level; the receipt carries the reason).
            # The design is aligned to that same recovered commit.
            code_repo.checkout(roles_root, prev)
            self.log_event({"type": "self_improve_apply_failed", "role": role,
                            "outer": outer, "error": str(e)[:300]})
            self._heal_self_design(role, outer, res,
                                   stage="self_improve_exhausted",
                                   outcome="apply_failed")
        # RepoIntegrityError (git-layer failure during apply/rollback) and any
        # other unexpected error propagate: the outer worker exits non-zero and
        # the driver aborts the run — continuing on an undefined code tree would
        # poison the lineage.

    def _heal_self_design(self, role: str, outer: int, res: Any,
                          stage: str, outcome: str) -> None:
        """Align a role's just-persisted self design with the committed tree.

        Runs at EVERY self-improve exit, not only the successful one, because
        the design file is written by the SESSION (before the loop learns the
        outcome). Both heal directions are driven by the COMMITTED tree -- the
        code commit for a landed patch, the outer's entry commit otherwise --
        never by the session workspace, which the exit may be discarding:

        * forward (``heal_design_keys`` + ``heal_design_slots``): strip names the
          committed tree cannot deliver;
        * reverse (``heal_design_removals``): put back the selections the session
          dropped for a removal that never landed (``unregister_component`` was
          scheduled, the patch was dropped, the component is still committed) --
          the state a forward-only heal can never see, since nothing is dangling.

        The session's DRAFT is copied to ``paths.design_draft_path`` first
        (audit / replay only, never a lineage parent): it is the same bytes as
        the design file at this instant -- nothing writes a role design between
        the session end and this exit -- and the file is then overwritten by the
        healed version. ``DesignStore.save`` is already atomic (tmp +
        ``os.replace``), so that second write opens no new torn-file window:
        worst case the draft stays, i.e. the pre-change behaviour. The facts ride
        the self receipt: ``design_stripped`` / ``design_restored`` /
        ``design_heal`` (stage, outcome, converged, draft path, final ckpt path).

        **Convergence (nested / chained heal safety).** Both directions share ONE
        predicate -- "does the committed tree resolve this name" -- so the
        post-state is closed: forward leaves ``tools subset-of resolvable``, and
        the reverse direction may only add a name it has ALREADY resolved, so a
        pass can never manufacture a new dangling reference. Passing twice is a
        no-op, hence the heal is idempotent and chaining it (this exit -> the
        next outer's startup ``repair_dangling_designs``) cannot oscillate. The
        invariant is not merely asserted: ``_self_design_residue`` re-runs all
        three heal stages on the persisted result and any second-pass change is
        reported loudly (``design_heal_not_converged`` +
        ``design_heal.converged=false``), so a future edit that breaks the fixed
        point shows up in the event stream instead of silently drifting the design.

        Advisory: a heal failure is recorded loudly but never aborts the run --
        it leaves exactly the state the pre-heal code left (a possibly
        misaligned design that the next outer's startup repair can strip).
        """
        if not self.code_root or not isinstance(res, dict):
            return
        try:
            from gan.design.store import DesignStore
            from gan.framework.task_execution import (heal_design_keys,
                                                      heal_design_removals,
                                                      heal_design_slots)
            store = DesignStore(paths.design_root(self.output_dir),
                                writer=self.ledger_writer)
            # Draft snapshot BEFORE any heal: the attempt's own proposal.
            draft = self._archive_self_draft(role, outer)
            cfg = store.load(role)  # the draft save_self_config just wrote
            records = res.get("records")
            pre = self._self_design_pre.get(role)
            stripped = heal_design_keys(cfg, role, self._roles_root())
            stripped += heal_design_slots(cfg, role, self._roles_root())
            restored = heal_design_removals(cfg, role, self._roles_root(), records, pre)
            if stripped or restored:
                store.save(cfg, role)
                if stripped:
                    self.log_event({"type": "design_dangling_stripped", "role": role,
                                    "stage": stage, "outcome": outcome,
                                    "stripped": stripped})
                if restored:
                    self.log_event({"type": "design_selection_restored", "role": role,
                                    "stage": stage, "outcome": outcome,
                                    "restored": restored})
            # Convergence probe: heal must be a FIXED POINT (idempotent, and
            # unable to manufacture a new dangling by itself). A second pass over
            # the just-persisted result that still wants changes means the
            # invariant broke -- loud, never silent.
            residue = self._self_design_residue(cfg, role, records, pre)
            if residue:
                soft_fail(f"self-design heal did NOT converge ({role}, outer "
                          f"{outer}, stage={stage}): a second pass would still "
                          f"change {residue} -- the persisted design is not a "
                          f"fixed point",
                          event_path=paths.events_path(self.output_dir),
                          event_type="design_heal_not_converged", role=role,
                          outer=outer, stage=stage, residue=residue)
            res["design_stripped"] = stripped
            res["design_restored"] = restored
            res["design_heal"] = {
                "stage": stage,
                "outcome": outcome,
                "converged": not residue,
                "draft_path": draft,
                "ckpt_path": store.path(role),
            }
        except Exception as e:  # noqa: BLE001 -- see docstring: loud, never fatal
            soft_fail(f"self-design exit heal failed ({role}, outer {outer}, "
                      f"stage={stage}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="design_heal_failed", role=role, outer=outer,
                      stage=stage)

    def _archive_self_draft(self, role: str, outer: int) -> Optional[str]:
        """Audit snapshot of one role's session draft (never fatal).

        Called at the exit BEFORE the heal overwrites the design file, so the
        snapshot is byte-for-byte what the self-improve session proposed. Kept
        under ``ckpt/design_drafts/`` (never read by the framework, never a
        lineage parent) so an audit / replay can answer "what did this attempt
        do, and what did the heal then strip or restore?" without guessing from
        the overwritten file. A re-run of the same outer preserves the earlier
        attempt's snapshot under a millisecond stamp, mirroring the
        ``ckpt/outer_<N>.json`` convention.

        Deliberately a plain writer rather than ``DesignStore.save``: this is an
        audit artifact, not a decision-grade design write (B46's WriterToken
        guards the design/scores/checkpoints that the next generation consumes).
        """
        src = os.path.join(paths.design_root(self.output_dir), role, "config.json")
        if not os.path.isfile(src):
            return None
        try:
            import time as _time
            p = paths.design_draft_path(self.output_dir, role, outer)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.exists(p):
                os.replace(p, f"{p[:-len('.json')]}_{int(_time.time() * 1000)}.json")
            with open(src, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            payload = {
                "role": role,
                "outer": outer,
                "kind": "self_improve_draft",
                "note": ("audit/replay only -- this self-improve session's design "
                         "as it ended, BEFORE the outer-exit heal. The lineage "
                         "parent is design/<role>/config.json (healed / aligned "
                         "to the committed tree)."),
                "saved_at": _time.time(),
                "config": cfg,
            }
            tmp = f"{p}.save-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            return p
        except Exception as e:  # noqa: BLE001 -- audit-only, must not fail the exit
            soft_fail(f"design draft archive failed ({role}, outer {outer}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="design_draft_archive_failed", role=role, outer=outer)
            return None

    def _self_design_residue(self, cfg: Dict[str, Any], role: str,
                             records: Any, pre: Any) -> List[Dict[str, Any]]:
        """What a SECOND heal pass over an already-healed config would change.

        Empty = the config is a fixed point of the heal = the invariant "a heal
        cannot leave (or create) a state the heal would touch again" holds. Both
        directions share the single predicate "does the committed tree resolve
        this name", so forward pruning leaves ``tools subset-of resolvable`` and
        the reverse direction may only re-add a name it already resolved -- the
        composition is therefore idempotent by construction, and this probe is
        the tripwire that keeps it so (it also covers the chained case: a state
        the next outer's startup ``repair_dangling_designs`` would strip).

        Read-only: works on a copy, so probing never mutates the saved design.
        """
        from gan.framework.task_execution import (heal_design_keys,
                                                  heal_design_removals,
                                                  heal_design_slots)
        probe = deepcopy(cfg)
        residue: List[Dict[str, Any]] = heal_design_keys(probe, role, self._roles_root())
        residue += heal_design_slots(probe, role, self._roles_root())
        residue += heal_design_removals(probe, role, self._roles_root(), records, pre)
        return residue

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
        sync_failures: List[str] = []
        for role, seat in (("planner", "plan"), ("evaluator", "evaluate")):
            seat_key = f"{akey}__{seat}"
            try:
                src = self.broker.src_dir(role, seat_key)
            except Exception as e:
                sync_failures.append(f"src_dir({role}): {e}")
                continue
            for rel in files or []:
                s = os.path.join(code_root, rel)
                d = os.path.join(src, rel)
                if not os.path.isfile(s):
                    if drop_missing and os.path.isfile(d):
                        try:
                            os.remove(d)
                        except OSError as e:
                            sync_failures.append(f"drop {rel}: {e}")
                    continue
                if os.path.isfile(d):
                    try:
                        shutil.copy2(s, d)
                    except OSError as e:
                        sync_failures.append(f"copy {rel}: {e}")
        if sync_failures:
            soft_fail(f"workspace resync had {len(sync_failures)} failure(s) "
                      f"(outer_{outer}): " + "; ".join(sync_failures[:5]))

    def _component_drift(self, patch: str, applied: bool) -> List[Dict[str, Any]]:
        """Registered components whose MODULE this patch changed.

        ``module_changed`` restates what ``task_patch_files`` already tells the
        planner; the added value is the catalog annotation, computed with the SAME
        stamp comparison as (c) so the two can never disagree. Skipped entirely when
        the patch did not apply (nothing landed => no drift to report).
        """
        if not applied or not (patch or "").strip():
            return []
        from gan.framework import code_repo
        from gan.registries.loader import load_registry_for_role, stale_metadata
        try:
            changed = set(code_repo.changed_files(patch))
        except Exception:  # noqa: BLE001 -- advisory
            return []
        out: List[Dict[str, Any]] = []
        for role in ("task", "planner", "evaluator"):
            try:
                reg = load_registry_for_role(role, code_root=self.roots.repo_dir_for_role(role))
            except Exception:  # noqa: BLE001
                continue
            stale_names = {s["name"] for s in stale_metadata(reg)}
            for e in reg.entries:
                if not isinstance(e, dict):
                    continue
                name = str(e.get("name"))
                mod = str(e.get("module") or "")
                if not mod or f"gan/components/{mod}" not in changed:
                    continue
                out.append({
                    "name": name, "module": mod, "registry": f"{role}.json",
                    "module_changed": True,
                    "entry_touched": f"gan/registries/{role}.json" in changed,
                    "description_stale_after": name in stale_names,
                })
        return out

    def _catalog_stale(self, role: str) -> List[Dict[str, Any]]:
        """This role's stale catalog entries, for its own receipt."""
        try:
            from gan.framework.preflight import catalog_stale
            return catalog_stale(role, code_root=self.roots.repo_dir_for_role(role))
        except Exception:  # noqa: BLE001 -- advisory
            return []

    def _make_receipt(self, genid: Any, plan_result: Dict[str, Any],
                      parent_cfg: Dict[str, Any], child: Node, outer: Any,
                      plan_cost: Optional[Dict[str, Any]] = None,
                      cm_summary: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        from gan.framework.receipt import build_receipt
        rejected = plan_result.get("patch_rejection") or child.meta.get("task_patch_rejected")
        exhausted = bool(plan_result.get("budget_exhausted"))
        truncated = bool(plan_result.get("truncated"))
        tb_exhausted = bool(plan_result.get("token_budget_exhausted"))
        # Context management: a degraded CM session (policy disabled /
        # failures) is a budget-state fact of its own kind. Assembly
        # degradation degrades the seam the same way — the receipt must say
        # so too (the summary only carries the
        # assembly fields when an assembly was configured, so a baseline
        # session is unaffected).
        cm_degraded = bool(cm_summary and (
            cm_summary.get("policy_disabled")
            or cm_summary.get("policy_failures")
            or cm_summary.get("assembly_disabled")
            or cm_summary.get("assembly_failures")))
        budget = {
            "exhausted": exhausted or truncated or tb_exhausted,
            # Reserved cost keys (docs/10 §10): a token soft stop is a budget
            # outcome of its own kind (distinct from the tool-call cap).
            "kind": ("retries" if exhausted
                     else ("max_tool_calls" if truncated
                           else ("token_budget" if tb_exhausted
                                 else ("cm" if cm_degraded else None)))),
            "attempts": plan_result.get("attempts", 0),
            "truncated": truncated,
            "token_budget_exhausted": tb_exhausted,
        }
        if cm_summary:
            budget["cm"] = cm_summary
        # Cost facts of THIS round (B6): the task child (proxy audit) and the
        # planner's OWN plan session. The evaluator's session cost is
        # deliberately NOT here — the receipt feeds the PLANNER's next prompt,
        # and e's internal costs are outside the planner's view (docs/10 §4).
        cost: Optional[Dict[str, Any]] = None
        if self.record_usage:
            cost = {
                "task": ({"cost_tokens": child.value.cost_tokens,
                          "cost_wallclock_s": child.value.cost_wallclock_s}
                         if child.value.cost_tokens is not None else None),
                "planner_plan": cost_ledger.compact(plan_cost),
            }
        # Task-side reserved-key clamp records (docs/10 §10): the planner sees
        # its own clamped decisions (责任内化, docs/10 §6). Rides regardless of
        # record_usage (a clamp is a design fact, not a usage record); the
        # evaluator's projection (_receipt_for_evaluator) deliberately excludes it.
        ck_clamped = ((child.meta.get("cost_keys") or {}).get("clamped")) or []
        if ck_clamped:
            cost = dict(cost or {})
            cost["clamped"] = ck_clamped
        refs = [
            paths.session_traj_file(self.output_dir, outer, genid, "planner", seat="plan"),
            paths.session_traj_file(self.output_dir, outer, genid, "task"),
            paths.session_traj_file(self.output_dir, outer, genid, "evaluator",
                                    seat="evaluate"),
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
            # The planner (the task agent's designer) sees the "design
            # selected vs actually assembled" gap for the just-executed child in
            # the receipt it consumes at its next plan session.
            toolset=child.meta.get("toolset_report"),
            # Registered components whose module this patch changed, with
            # the catalog annotation ((c) makes its source of truth the same stamp,
            # and the evaluator's projection deliberately excludes both it and the
            # startup stale list -- bookkeeping, not a task fact).
            component_drift=self._component_drift(
                plan_result.get("patch_proposed", ""), 
                bool(child.meta.get("patch_applied"))),
            catalog_stale=self._catalog_stale("planner"),
            # The dangling slot names the framework stripped
            # from the design right before persisting it.
            design_stripped=child.meta.get("design_stripped"),
            # Cost facts of this round (B6): task child + the planner's OWN
            # plan session (never the evaluator's — see the cost dict above).
            cost=cost,
        )
        try:
            d = paths.runs_dir(self.output_dir, genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "patch_receipt.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            if rejected and plan_result.get("patch_proposed"):
                with open(os.path.join(d, "patch_proposed.diff"), "w", encoding="utf-8") as f:
                    f.write(plan_result.get("patch_proposed") or "")
        except Exception as e:
            soft_fail(f"patch_receipt.json write failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="receipt_write_failed", genid=str(genid), outer=outer)
        self.log_event({"type": "receipt", "genid": genid, "outer": outer,
                        "design_applied": rec["design"]["applied"],
                        "code_applied": rec["code_patch"]["applied"],
                        "rejected": bool(rejected), "budget": rec["budget"]})
        return rec

    def _make_self_receipt(self, role: str, outer: int, res: Any) -> Dict[str, Any]:
        from gan.framework.receipt import build_receipt
        r = res or {}
        rejected = r.get("patch_rejection")
        tb_exhausted = bool(r.get("token_budget_exhausted"))
        cm_summary = r.get("cm_summary") if isinstance(r, dict) else None
        cm_degraded = bool(cm_summary and (
            cm_summary.get("policy_disabled")
            or cm_summary.get("policy_failures")
            or cm_summary.get("assembly_disabled")
            or cm_summary.get("assembly_failures")))
        budget = {
            "exhausted": bool(r.get("budget_exhausted")) or bool(r.get("truncated")) or tb_exhausted,
            "kind": ("retries" if r.get("budget_exhausted")
                     else ("max_tool_calls" if r.get("truncated")
                           else ("token_budget" if tb_exhausted
                                 else ("cm" if cm_degraded else None)))),
            "attempts": r.get("attempts", 0), "truncated": bool(r.get("truncated")),
            "token_budget_exhausted": tb_exhausted,
        }
        if cm_summary:
            budget["cm"] = cm_summary
        rec = build_receipt(
            genid=f"outer_{outer}", role=role, stage="self_improve",
            records=r.get("records"), patch=r.get("patch_proposed", ""),
            patch_applied=bool(r.get("patch")), rejected_reason=rejected, budget=budget,
            # The exit heal (ANY exit -- see _heal_self_design) strips dangling
            # names from the self design and restores selections dropped for a
            # removal that never landed; the role sees its own heal at the next
            # self-improve. Merge the STARTUP repair for this role (consumed
            # once, so a one-off startup event does not repeat every outer).
            design_stripped=((r.get("design_stripped") or [])
                             + self.preflight_design_stripped.pop(role, [])),
            design_restored=r.get("design_restored"),
            design_heal=r.get("design_heal"),
            component_drift=self._component_drift(r.get("patch_proposed", ""),
                                                  bool(r.get("patch"))),
            catalog_stale=self._catalog_stale(role),
            # This outer's own instance assembly — carried into the NEXT
            # outer's self-improve receipt (this outer already injects it via
            # the self-improve `recent` payload).
            toolset=getattr(getattr(self, role, None), "assembly_report", None),
        )
        try:
            d = paths.runs_dir(self.output_dir, f"self_outer_{outer}_{role}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "patch_receipt.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
        except Exception as e:
            soft_fail(f"self patch_receipt.json write failed ({role}, outer {outer}): {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="receipt_write_failed", role=role, outer=outer)
        self._self_receipts[role] = rec
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
                "weaknesses": list(getattr(ctx, "weaknesses", []) or []),
                # The eval-point verdicts (point/rating/comment)
                # join the generation's evidence card -- a structured sink (not
                # just the trajectory replay) for the four shipped eval points;
                # audit-file only, no evaluator-projection key is added.
                "eval_point_results": list(getattr(ctx, "eval_point_results", []) or []),
            }
            d = paths.runs_dir(self.output_dir, child.genid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "eval.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
        except Exception as e:
            soft_fail(f"eval.json write failed for genid {child.genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="eval_json_write_failed", genid=str(child.genid))

    def _restore(self, boundary: str = "latest") -> bool:
        payload = ckpt.load_checkpoint(self.output_dir, boundary)
        if not payload:
            return False
        ckpt.restore_trees(self.output_dir, payload.get("trees") or {})
        ckpt.restore_designs(self.output_dir, payload.get("designs") or {},
                             writer=self.ledger_writer)
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

    # -- branch-per-node blood lineage ---------------------------------
    def _task_root(self) -> Optional[str]:
        """Task-code repo root (== code_root in single layout; falls back to it)."""
        roots = getattr(self, "roots", None)
        return roots.task if roots is not None else getattr(self, "code_root", None)

    def _roles_root(self) -> Optional[str]:
        """Role-code repo root (== code_root in single layout; falls back to it)."""
        roots = getattr(self, "roots", None)
        if roots is not None:
            return roots.roles if roots.roles else roots.task
        return getattr(self, "code_root", None)

    def _pin_outer_base(self, outer: int) -> None:
        """Anchor the outer's entry HEAD as ``outer_<O>_base`` (once per outer).

        HEAD drifts to the latest task-branch tip during the outer, so the base
        for ``parent == "initial"`` generations must be pinned BEFORE any task
        commit moves it. Idempotent (re-runs of the same outer).
        """
        task_root = self._task_root()
        if not task_root:
            return
        from gan.framework import code_repo
        ref = code_repo.outer_base_ref_name(outer)
        ok, mode = code_repo.ensure_branch(task_root, ref, code_repo.current_commit(task_root))
        self.log_event({"type": "code_branch", "kind": "outer_base", "outer": outer,
                        "ref": ref, "mode": mode, "ok": bool(ok)})

    def _resolve_base(self, parent: Any, outer: int) -> str:
        """Blood lineage: base = parent's code state (initial -> outer base).

        Legacy fallback: a node without a recorded code commit falls back to
        HEAD (behavior identical to the pre-branch time-line; audited).
        """
        from gan.framework import code_repo
        head = code_repo.current_commit(self._task_root())
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
        checked = code_repo.checkout_base(self._task_root(), base)
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
        # code_repo is referenced by the integrity guards on the session-level
        # exception handlers below (its failures must abort the run, not degrade
        # to per-generation "failed" events).
        from gan.framework import code_repo
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
            # resume_boundary: "outer" (never resume from a crashed outer's
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
                        rep = code_repo.reconcile_refs(self._task_root(), commits)
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
            _outer_t0 = time.time()
            # fresh role instances (design + tools + chat + workspace) for this outer
            self._refresh_roles(outer)
            # pin the outer's entry HEAD as the base for parent=initial children
            # (HEAD drifts to task-branch tips during the outer)
            self._pin_outer_base(outer)

            inner_start = self._start_inner if (only_outer is None and outer == self._start_outer) else 1
            for inner in range(inner_start, I_max + 1):
                _inner_t0 = time.time()
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
                # Mirrors carry parent-child semantics; the per-round
                # record lands on the child meta at settle (feedback /
                # feedback_digest / receipt) and the next selection reads it.
                self._parent_feedback = parent_feedback
                self._parent_receipt = parent_receipt
                self._parent_digest = parent_meta.get("feedback_digest")
                parent_predicted_score = parent_meta.get("predicted_score")
                parent_benchmark_score = parent_meta.get("benchmark_score")
                evaluator_issues = (parent_feedback or {}).get("issues")
                parent_ref = [] if str(parent.genid) == "initial" else [parent.genid]

                # Reserved cost keys for the PLAN seat (docs/10 §10, phase C):
                # effective values from the planner's OWN design (fallbacks =
                # the loop.yaml values), passed into the session + stamped on
                # the instance; the applied/clamped record rides the ledger.
                plan_ck, plan_clamps = self._effective_cost_keys("planner", {
                    "plan_max_tool_calls": self.plan_max_tool_calls,
                    "patch_retry_k": self.patch_retry_k,
                    "plan_token_budget": None,
                })
                # Stamp the token soft stop for THIS session only; _self_session
                # clears it before self-improve (the stamp is otherwise inherited
                # by a session that has no design key of its own).
                try:
                    self.planner.session_token_budget = plan_ck.get("plan_token_budget")
                    # slice 前端档位归座 (docs/10 §11.5): the PLAN session's
                    # read_trajectory presentation follows the PLANNER's own
                    # design cm_mode (stamped onto the AccessContext by the
                    # role) — visible sets are untouched.
                    self.planner.session_cm_mode = plan_ck.get("cm_mode")
                except Exception as e:  # noqa: BLE001 -- advisory stamp
                    soft_fail(f"session cost stamp failed (planner: token "
                              f"budget / cm_mode): {e}")
                # Context-management session for THIS plan call (docs/10 §11
                # D11); cleared right after the call (no stamp leak into
                # self-improve), like ``session_token_budget``.
                self.planner.cm_session = self._build_cm_session("planner", plan_ck,
                                                                 outer=outer)
                self._materialize_traj(self.planner.cm_session, parent_ref)
                self._emit_cost_keys_applied("planner", "plan", genid,
                                             plan_ck, plan_clamps)

                # Cost statement for the plan session (B2): framework-rendered
                # facts only; the role injects the text verbatim. Resume-safe:
                # an older role source without the kwarg keeps working (the
                # statement is then absent for that session — audited).
                cost_stmt_plan = self._cost_statement_plan(
                    parent, outer, genid, [g for g in outer_genids if g != genid],
                    task_budgets=self._task_budget_keys(parent),
                    own_budget=plan_ck.get("plan_token_budget"))
                plan_cost_kwargs: Dict[str, Any] = {}
                if cost_stmt_plan and self._cost_statement_accepted(self.planner.plan):
                    plan_cost_kwargs["cost_statement"] = cost_stmt_plan
                    # Injection fact (B2): what was rendered and how big — the
                    # offline audit reconciles this against the session's first
                    # `input` row.
                    self.log_event({"type": "cost_statement", "seat": "plan",
                                    "genid": str(genid), "bytes": len(cost_stmt_plan)})
                elif cost_stmt_plan:
                    self.log_event({"type": "cost_statement_skipped", "seat": "plan",
                                    "genid": str(genid),
                                    "reason": "role signature lacks cost_statement"})

                try:
                    plan_result = self.planner.plan(
                        parent_summary=self._parent_summary(parent),
                        parents=[self._parent_summary(p) for p in parents],
                        parent_feedback=parent_feedback,
                        evaluator_issues=evaluator_issues,
                        config=parent_cfg,
                        node_id=genid,
                        broker=self.broker,
                        task_brief=self.task_brief,
                        trajectory_genids=parent_ref,
                        receipt=parent_receipt,
                        parent_predicted_score=parent_predicted_score,
                        parent_benchmark_score=parent_benchmark_score,
                        patch_retry_k=plan_ck.get("patch_retry_k", self.patch_retry_k),
                        max_tool_calls=plan_ck.get("plan_max_tool_calls", self.plan_max_tool_calls),
                        tool_repeat_limit=self.tool_repeat_limit,
                        **plan_cost_kwargs,
                    ) or {}
                except code_repo.RepoIntegrityError:
                    raise  # undefined code tree: never degrade to planner_failed
                except Exception as e:
                    self.planner.cm_session = None  # no stamp leak
                    self._archive_invalid("planner_failed", parent, parents, genid, str(e), outer=outer, inner=inner)
                    self._save_checkpoint("inner", outer, inner)
                    continue
                # Settled plan session -> cost ledger (A5). Success-only: a
                # planner_failed round has no settled session to record.
                _plan_cm = getattr(self.planner, "cm_session", None)
                plan_cm_summary = (_plan_cm.summary()
                                   if _plan_cm is not None else None)
                plan_cost_rec = self._record_session_cost(
                    "planner", "plan", outer, genid,
                    cm_session=_plan_cm)
                self.planner.cm_session = None  # no stamp leak (§11 D11)

                try:
                    child = self.task_runner(plan=plan_result, parent=parent, genid=genid,
                                             base=base)
                except code_repo.RepoIntegrityError:
                    raise  # undefined code tree: never degrade to task_runner_failed
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
                # A failure here may NOT be swallowed — the trajectory is the
                # evaluator's anti-reward-hacking evidence AND the durable audit
                # record for this generation. Silently continuing would evaluate
                # blind on missing evidence while looking identical to a normal
                # run. Fail the generation explicitly (same channel as
                # evaluator_failed) instead.
                try:
                    trajectory.collect(child.meta.get("run_dir", ""), f"gan_{genid}",
                                       self.output_dir, outer, genid)
                except Exception as e:
                    child.valid_parent = False
                    child.meta["invalid"] = True
                    child.meta["invalid_reason"] = "trajectory_archive_failed"
                    child.meta["invalid_detail"] = str(e)[:500]
                    self.task_tree.add_node(child)
                    try:
                        shutil.rmtree(paths.work_dir(self.output_dir, genid), ignore_errors=True)
                    except Exception as e:
                        soft_fail(f"work dir cleanup failed for genid {genid}: {e}")
                    self._write_invalid(genid, "trajectory_archive_failed", str(e), outer, inner)
                    self.log_event({"type": "inner_invalid", "genid": genid,
                                    "reason": "trajectory_archive_failed",
                                    "detail": str(e)[:300]})
                    self._save_checkpoint("inner", outer, inner)
                    continue

                # evaluator (execute even for missing bench; pass imputed info, not as anchor)
                benchmark_for_eval = None if child.meta.get("imputed") else child.value.score
                # BLIND hygiene: never expose the objective score/accuracy in the
                # blind-phase run summary (score is revealed separately).
                # ALLOWLIST, not denylist — the planner's node meta
                # is mostly the planner's own paperwork, and `records` carries the
                # planner's respond_issue free text (its argumentation to the
                # evaluator). Any NEW meta key must be added here explicitly:
                # default-deny keeps rhetoric out of the evaluator by construction,
                # mirroring the per-role access allowlist.
                meta_view = {k: child.meta[k] for k in _EVALUATOR_META_KEYS
                             if k in (child.meta or {})}
                report_view = {k: v for k, v in (child.meta.get("report_summary") or {}).items()
                               if k not in ("overall_accuracy", "random_guess_accuracy")}
                # Cost statement for the evaluate session (B2): THIS round's
                # task cost as a process fact (no scores — blind discipline).
                task_cost_pair = ((child.value.cost_tokens, child.value.cost_wallclock_s)
                                  if child.value.cost_tokens is not None else None)
                # Reserved cost keys for the EVALUATE seat (docs/10 §10, C):
                # the evaluator's own design (blind/reveal stay SEPARATE — the
                # mandatory eval-points must never be starved), fallbacks =
                # the loop.yaml seat budget / the pre-C reveal rule.
                eval_ck, eval_clamps = self._effective_cost_keys("evaluator", {
                    "evaluate_blind_max_tool_calls": self.evaluate_max_tool_calls,
                    "evaluate_reveal_max_tool_calls": None,
                    "evaluate_token_budget": None,
                })
                try:
                    self.evaluator.session_token_budget = eval_ck.get("evaluate_token_budget")
                    # slice 前端档位归座 (docs/10 §11.5): the evaluator's OWN
                    # design governs its read_trajectory presentation (the
                    # evaluate seat previously had no DesignContext and was
                    # stuck on passthrough).
                    self.evaluator.session_cm_mode = eval_ck.get("cm_mode")
                except Exception as e:  # noqa: BLE001 -- advisory stamp
                    soft_fail(f"session cost stamp failed (evaluator: token "
                              f"budget / cm_mode): {e}")
                self._emit_cost_keys_applied("evaluator", "evaluate", genid,
                                             eval_ck, eval_clamps)
                # Context-management session for THIS evaluate call (§11 D11).
                self.evaluator.cm_session = self._build_cm_session("evaluator", eval_ck,
                                                                   outer=outer)
                self._materialize_traj(self.evaluator.cm_session, [genid] + parent_ref)
                cost_stmt_eval = self._cost_statement_eval(genid, task_cost_pair,
                                                           own_budget=eval_ck.get("evaluate_token_budget"))
                eval_cost_kwargs: Dict[str, Any] = {}
                if cost_stmt_eval and self._cost_statement_accepted(self.evaluator.evaluate):
                    eval_cost_kwargs["cost_statement"] = cost_stmt_eval
                    self.log_event({"type": "cost_statement", "seat": "evaluate",
                                    "genid": str(genid), "bytes": len(cost_stmt_eval)})
                elif cost_stmt_eval:
                    self.log_event({"type": "cost_statement_skipped", "seat": "evaluate",
                                    "genid": str(genid),
                                    "reason": "role signature lacks cost_statement"})
                if eval_ck.get("evaluate_reveal_max_tool_calls") is not None \
                        and self._accepts_kwarg(self.evaluator.evaluate,
                                                "reveal_max_tool_calls"):
                    # Only passed when the design set it (an older code tree
                    # without the kwarg keeps the pre-C reveal rule — audited).
                    eval_cost_kwargs["reveal_max_tool_calls"] = \
                        eval_ck["evaluate_reveal_max_tool_calls"]
                try:
                    ctx = self.evaluator.evaluate(
                        run_summary={
                            "genid": genid,
                            "meta": meta_view,
                            "report_summary": report_view,
                            "score_status": child.value.score_status,
                            "imputed": bool(child.meta.get("imputed")),
                            # Projection, not the whole receipt —
                            # `code_patch.rejected_reason` quotes the planner's own
                            # artifacts, `grants` carries agent free text, and
                            # `design.ops` duplicate the diff_summary channel.
                            "receipt": _receipt_for_evaluator(self._parent_receipt),
                        },
                        parent_feedback={**(parent_feedback or {}), "digest": self._parent_digest},
                        benchmark_score=benchmark_for_eval,
                        node_id=genid,
                        broker=self.broker,
                        task_brief=self.task_brief,
                        trajectory_genids=[genid] + parent_ref,
                        max_tool_calls=(eval_ck.get("evaluate_blind_max_tool_calls")
                                        or self.evaluate_max_tool_calls),
                        tool_repeat_limit=self.tool_repeat_limit,
                        **eval_cost_kwargs,
                    )
                except code_repo.RepoIntegrityError:
                    raise  # undefined code tree: never degrade to evaluator_failed
                except Exception as e:
                    self.evaluator.cm_session = None  # no stamp leak
                    child.valid_parent = False
                    child.meta["invalid"] = True
                    child.meta["invalid_reason"] = "evaluator_failed"
                    child.meta["invalid_detail"] = str(e)[:500]
                    # Same invalid tail as _archive_invalid --
                    # one shared helper so both paths (and their scores row)
                    # can never drift apart.
                    self._finalize_invalid_node(child, "evaluator_failed", str(e),
                                                outer, inner)
                    self._save_checkpoint("inner", outer, inner)
                    continue
                # Settled evaluate session -> cost ledger (A5).
                _eval_cm = getattr(self.evaluator, "cm_session", None)
                eval_cm_summary = (_eval_cm.summary()
                                   if _eval_cm is not None else None)
                eval_cost_rec = self._record_session_cost(
                    "evaluator", "evaluate", outer, genid,
                    cm_session=_eval_cm)
                self.evaluator.cm_session = None  # no stamp leak (§11 D11)

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
                self._write_gen_cost(genid, outer)
                child.meta["receipt"] = self._make_receipt(
                    genid, plan_result, parent_cfg, child, outer,
                    plan_cost=plan_cost_rec, cm_summary=plan_cm_summary)
                if child.meta.get("task_patch_files") and self.broker is not None:
                    self._resync_workspace(outer, child.meta["task_patch_files"])
                if not self.keep_workdirs:
                    shutil.rmtree(paths.work_dir(self.output_dir, genid), ignore_errors=True)

                # sanitized diff summary + text digest for the previous round
                # Ops-only: the report_path feed and the files bag are gone (the
                # parameter no longer exists to be misused), and the digest takes
                # no diff_summary -- the inventory's single evaluator-facing
                # carrier is the feedback.diff_summary JSON block.
                diff_summary = build_diff_summary(plan_result.get("records"))
                digest = self._settle_feedback_digest(
                    plan_result, ctx, genid,
                    parent_feedback=parent_feedback, parent_meta=parent_meta)
                if digest:
                    # Land the parent-scoped digest on the node so the
                    # next selection can serve it as ``_parent_digest``.
                    child.meta["feedback_digest"] = digest

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
                    "cost_tokens": child.value.cost_tokens,
                    "cost_wallclock_s": child.value.cost_wallclock_s,
                    "duration_s": round(time.time() - _inner_t0, 1),
                })

                # feedback for next round: planner gets issues + diff summary.
                # The durable record is the child node meta only; the
                # loop-local mirror is refreshed at SELECTION (parent.meta), so
                # a branching re-selection can never inherit the wrong branch's
                # feedback.
                child.meta["feedback"] = make_feedback(
                    issues=getattr(ctx, "issues", []),
                    diff_summary=diff_summary,
                    penalties=packet.penalties,
                )
                # receipt / feedback_digest / feedback are attached AFTER
                # add_node, so persist the updated meta explicitly to keep
                # replayed trees complete.
                self.task_tree.update(child.genid, meta=child.meta)
                self._prev_predicted = getattr(ctx, "predicted_score", None)
                self._prev_benchmark = cs

                self._save_checkpoint("inner", outer, inner)

                if no_improve >= patience:
                    self.log_event({"type": "stagnation_break", "outer": outer, "inner": inner})
                    break

            # ---------------- outer self-improvement ----------------
            # Each role's own instance assembly rides its self-improve
            # payload — the CURRENT outer's assembly state is visible at the
            # decision point (kept/adjusted eval_points / planner prompt).
            # The digests list is the experience stream; ``last_feedback`` is not
            # part of this payload.
            recent_eval = {"digests": self._digests[-I_max:],
                           "toolset_report": getattr(self.evaluator, "assembly_report", None)}
            recent_plan = {"advantages": self._advantages[-I_max:],
                           "task_tree_size": len(self.task_tree),
                           "toolset_report": getattr(self.planner, "assembly_report", None)}
            # Self-improve cost statements (B2): each role's OWN sessions this
            # outer (cost × quality alignment material) + the role's compact
            # ledger records; the OTHER role's costs are deliberately absent.
            if self.record_usage:
                for role, payload in (("evaluator", recent_eval), ("planner", recent_plan)):
                    try:
                        own = [cost_ledger.compact(r)
                               for r in cost_ledger.self_session_costs(self.output_dir, outer, role)]
                        payload["costs"] = {"own_sessions": [r for r in own if r]}
                        payload["cost_statement"] = self._cost_statement_self(role, outer)
                        # Injection fact (B2, self seat): same bytes audit the
                        # plan/evaluate statements get (docs/10 §11.5 gap fix).
                        if payload["cost_statement"]:
                            self.log_event({"type": "cost_statement",
                                            "seat": "self_improve", "role": role,
                                            "bytes": len(payload["cost_statement"])})
                    except Exception as e:  # noqa: BLE001 -- advisory, never fatal
                        soft_fail(f"self cost payload failed ({role}): {e}",
                                  event_path=paths.events_path(self.output_dir),
                                  event_type="cost_statement_build_failed",
                                  seat="self_improve")
            # visible task trajectories for self-improvement: this outer's gens
            # + the direct parent of the last generation (read-only).
            si_refs = list(outer_genids)
            if outer_genids:
                last = self.task_tree.get(outer_genids[-1])
                p = getattr(last, "parent_genid", None) if last is not None else None
                if p is not None and str(p) != "initial" and p not in si_refs:
                    si_refs.append(p)
            # Pre-session design snapshot: the reference the EXIT heal compares
            # against. Without it, "absent from the draft" cannot be told apart
            # from "never declared", and putting a name back would be a guess.
            # The roles only READ their design file up to this point (plan /
            # evaluate sessions persist the TASK design), so this is the true
            # pre-session state. Advisory: on failure the heal degrades to the
            # in-session `deselect_component` record as its evidence.
            try:
                from gan.design.store import DesignStore
                _pre_store = DesignStore(paths.design_root(self.output_dir),
                                         writer=self.ledger_writer)
                for _role in ("planner", "evaluator"):
                    self._self_design_pre[_role] = _pre_store.load(_role)
            except Exception as e:  # noqa: BLE001 -- never fatal
                soft_fail(f"pre-session design snapshot failed (outer {outer}): {e}",
                          event_path=paths.events_path(self.output_dir),
                          event_type="design_pre_snapshot_failed", outer=outer)
            # The two self-improve sessions are INDEPENDENT (each role reads its
            # own trajectory/design and writes only its own seat surface --
            # frozen.SEAT_WRITE guarantees the path sets are disjoint) and run
            # in PARALLEL threads; the expensive part is the LLM session, while
            # the fast apply+commit step serializes on code_repo._TREE_LOCK.
            # RepoIntegrityError still aborts the run; any other error degrades
            # to self_improve_error exactly as before.
            session_results: Dict[str, Any] = {}
            session_errors: Dict[str, Exception] = {}

            def _self_session(role: str, recent_payload: Dict[str, Any],
                              role_obj: Any, prev_receipt: Any) -> None:
                try:
                    # Clear the plan/evaluate seat's token-budget stamp: the
                    # self-improve session has NO design token key of its own
                    # in phase C (phase C defines no self-seat budget key at
                    # all — the session runs UNLIMITED token-wise; its only
                    # cap is self_improve_max_tool_calls) and must not
                    # inherit the stamp by accident (docs/10 §10.2 C11).
                    try:
                        role_obj.session_token_budget = None
                    except Exception:  # noqa: BLE001 -- advisory stamp
                        pass
                    # Context-management session for THIS self-improve call
                    # (docs/10 §11 D11): front-end + ctx tools only for p/e
                    # in the first increment (no assembly-time policy);
                    # cleared after the call (no leak across seats).
                    try:
                        # Returns (values, clamps) -- UNPACK the tuple (the
                        # plan/evaluate seats do; the bare assignment made
                        # ``ck_si`` the tuple itself, and ``.get`` raised
                        # AttributeError that the except below swallowed:
                        # every self-improve session ran with NO cm session
                        # and NO cm_mode stamp, silently -- observed in the
                        # 2026-10-12 real round: self rows in
                        # cost_sessions.jsonl carried ``cm: null``).
                        ck_si, _si_clamps = self._effective_cost_keys(role)
                        # slice 前端档位归座 (docs/10 §11.5): the self seat
                        # follows the role's OWN self-design (no leak from the
                        # plan/evaluate stamps above).
                        role_obj.session_cm_mode = ck_si.get("cm_mode")
                        role_obj.cm_session = self._build_cm_session(role, ck_si, outer=outer)
                        # self seat additionally materializes its OWN session
                        # files (the read_session_trajectory face) over the
                        # declared universe: this outer + the previous outer
                        # (L65 ruling — "self improve should read the previous
                        # outer's trajectory").
                        self._materialize_traj(role_obj.cm_session, si_refs,
                                               outer=outer, session_role=role,
                                               session_outers=([outer - 1]
                                                               if isinstance(outer, int)
                                                               and outer > 1 else []))
                    except Exception as e:  # noqa: BLE001 -- advisory stamp
                        # A swallowed degradation must SPEAK (no-silent-fail
                        # rule, AGENTS.md): without this event the session's
                        # cm: null row is indistinguishable from "no keys".
                        soft_fail(f"self-seat cm session build failed ({role}): {e}",
                                  event_path=paths.events_path(self.output_dir),
                                  event_type="cm_session_failed", role=role,
                                  seat="self_improve")
                        role_obj.cm_session = None
                    _si_res = role_obj.self_improve(
                        recent={**recent_payload, "receipt": prev_receipt},
                        broker=self.broker,
                        max_tool_calls=self.self_improve_max_tool_calls,
                        tool_repeat_limit=self.tool_repeat_limit,
                        trajectory_genids=si_refs,
                        patch_retry_k=self.patch_retry_k)
                    if isinstance(_si_res, dict)                             and getattr(role_obj, "cm_session", None) is not None:
                        try:
                            _si_res["cm_summary"] = role_obj.cm_session.summary()
                        except Exception:  # noqa: BLE001 -- audit-only
                            pass
                    session_results[role] = _si_res
                    # Settled self-improve session -> cost ledger (A5). Thread
                    # safety: trajectory_log.append is locked and list.append
                    # is GIL-atomic; the parallel sessions write DISJOINT
                    # trajectory files (seat-disjoint by SEAT_WRITE).
                    self._record_session_cost(
                        role, "self_improve", outer, None,
                        cm_session=getattr(role_obj, "cm_session", None))
                except Exception as e:  # noqa: BLE001 -- classified after join
                    session_errors[role] = e
                finally:
                    try:
                        role_obj.cm_session = None
                    except Exception:  # noqa: BLE001 -- advisory stamp
                        pass

            workers = [
                threading.Thread(target=_self_session,
                                 args=("evaluator", recent_eval, self.evaluator,
                                       self._self_receipts.get("evaluator")),
                                 name="self-improve-evaluator"),
                threading.Thread(target=_self_session,
                                 args=("planner", recent_plan, self.planner,
                                       self._self_receipts.get("planner")),
                                 name="self-improve-planner"),
            ]
            for t in workers:
                t.start()
            for t in workers:
                t.join()
            # RepoIntegrityError from either session: undefined code tree -- the
            # whole run must abort rather than continue on an unqualified tree.
            for role in ("evaluator", "planner"):
                err = session_errors.get(role)
                if isinstance(err, code_repo.RepoIntegrityError):
                    raise err
            for role in ("evaluator", "planner"):
                err = session_errors.get(role)
                if err is not None:
                    self.log_event({"type": "self_improve_error", "role": role,
                                    "error": str(err)})
            # Apply in the historical fixed order (evaluator first) so event
            # ordering and receipts keep their pre-parallel semantics. The
            # applies themselves serialize on the code_repo tree lock.
            for role in ("evaluator", "planner"):
                res = session_results.get(role)
                if res is not None:
                    self._apply_self_patch(role, outer, res)
                    self._make_self_receipt(role, outer, res)

            # Acceptance: none. Single chain — the self-improved design is kept and
            # becomes the active design for the next outer's fresh instances.
            # `outer_improved` is recorded for analysis only (no revert).
            self.log_event({"type": "self_improve_kept", "outer": outer,
                            "improved": outer_improved})

            self.evaluator_tree.add_node(Node(genid=f"eval_{outer}", meta={"num_digests": len(self._digests)}))
            self.planner_tree.add_node(Node(genid=f"plan_{outer}", meta={"advantages": self._advantages[-I_max:]}))
            # Cost reconciliation at the outer boundary (A5, advisory): the
            # proxy audit vs the session-trajectory sums. A mismatch is data
            # for the ledger's consumers, never a run-killer.
            if self.record_usage:
                try:
                    self.log_event({"type": "cost_reconciliation", "outer": outer,
                                    "report": cost_ledger.reconcile(self.output_dir)})
                except Exception as e:  # noqa: BLE001 -- advisory, never fatal
                    soft_fail(f"cost reconciliation failed (outer {outer}): {e}",
                              event_path=paths.events_path(self.output_dir),
                              event_type="cost_reconciliation_failed", outer=outer)
            self_own_costs: Dict[str, Any] = {}
            if self.record_usage:
                for role in ("planner", "evaluator"):
                    own = [c for c in self._session_costs
                           if c.get("role") == role and c.get("seat") == "self_improve"
                           and str(c.get("outer")) == str(outer)]
                    toks = [c.get("total_tokens") for c in own
                            if isinstance(c.get("total_tokens"), (int, float))]
                    self_own_costs[role] = {"sessions": len(own),
                                            "total_tokens": (sum(toks) if toks else None)}
            self.log_event({"type": "outer_done", "outer": outer, "improved": outer_improved,
                            "self_cost_tokens": self_own_costs,
                            "duration_s": round(time.time() - _outer_t0, 1)})
            self._save_checkpoint("outer", outer, I_max)

        return self.task_tree
