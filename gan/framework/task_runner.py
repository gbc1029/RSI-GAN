"""Concrete task runners (thin glue).

Heavy lifting (design persistence, env assembly, repo copy/patch, harness/report
invocation and the objective ``report_summary``) lives in the frozen framework:
``gan/framework/task_execution.py``.

``DomainTaskRunner`` runs the original per-domain harness and performs shared
label-domain scoring in the parent process before returning a task-tree
``Node``. The task agent is **design-driven**: its design config
(shallow, from the planner) is persisted by the framework and passed to the
harness through ``GAN_TASK_DESIGN``; selected skills are exposed via
``GAN_TASK_SKILLS_DIR``. Deep changes (``code_edit``) are applied to a throwaway
repo copy for that generation only.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from gan.framework import task_execution as tx
from gan.framework import paths, scores, code_repo
from gan.framework.loader import load_registry, resolve_domain
from gan.design.store import DesignStore
from gan.framework.tree.store import Node, NodeValue

def _modify_depth(records: List[Dict[str, Any]]) -> int:
    from gan.patch import has_deep_write

    records = records or []
    if has_deep_write(records):
        return 2          # code-level change (incl. register/unregister)
    if records:
        return 1          # operator-level (shallow design)
    return 0


# A1 (batch 31): only a PROPOSED patch that produced no new code commit is a
# rejection. "No patch proposed this round" is the normal case and used to set
# the same string, so every no-patch generation reported rejected:true to the
# receipt / events / evaluator surfaces and the planner got a phantom
# "fix the reported problem" hint (docs/7 §2.1 A1).
TASK_PATCH_REJECTED_NO_COMMIT = "no new code commit (patch rejected or absent)"


def task_patch_rejection(patch_str: str, task_code_commit: Optional[str],
                         base_commit: Optional[str]) -> Optional[str]:
    """Classify the task-patch outcome: the rejection reason, or None.

    None covers BOTH success (a new commit landed) and "no patch was proposed";
    the two are distinguishable downstream via ``patch_applied`` and the
    receipt's ``code_patch.proposed`` -- never via this field. A proposed patch
    whose diff turns out empty also lands here (acted, produced nothing).
    """
    if not (patch_str or "").strip():
        return None
    if task_code_commit and base_commit and task_code_commit == base_commit:
        return TASK_PATCH_REJECTED_NO_COMMIT
    return None


class DomainTaskRunner:
    def __init__(
        self,
        repo_root: str,
        output_dir: str,
        domain: str = "paper_review",
        subset: str = "_filtered_100_train",
        num_samples: int = 2,
        default_model: str = "gpt-4o-mini",
        python: Optional[str] = None,
        timeout: int = 1800,
        code_root: Optional[str] = None,
    ):
        self.repo_root = os.path.abspath(repo_root)
        self.output_dir = os.path.abspath(output_dir)
        self.code_root = os.path.abspath(code_root) if code_root else None
        self.domain = domain
        self.subset = subset
        self.num_samples = num_samples
        self.default_model = default_model
        self.python = python or sys.executable
        self.timeout = timeout

        reg = load_registry()
        self.domain_cfg = resolve_domain(reg, domain)
        self.score_key = self.domain_cfg.get("score_key")
        self.output_contract = self.domain_cfg.get("output_contract") or {}
        self.task_brief = self.domain_cfg.get("task_brief") or ""

        self.design_store = DesignStore(paths.design_root(self.output_dir))
        self.log_path = paths.log_file(self.output_dir, "task_runner.log")

    # -- helpers -----------------------------------------------------------
    # NOTE: the model is NOT read from the (evolvable) design config any more.
    # It is resolved centrally by gan/framework/models.py and passed in as
    # ``default_model`` (frozen model selection).

    # -- runner ------------------------------------------------------------
    def __call__(self, plan: Optional[Dict[str, Any]] = None, parent: Optional[Node] = None,
                 genid: Any = None, base: Optional[str] = None) -> Node:
        plan = plan or {}
        records = plan.get("records", []) or []
        config = plan.get("config")
        if not isinstance(config, dict):
            config = {}
        patch_str = plan.get("patch", "") or ""

        node_dir = paths.work_dir(self.output_dir, genid)
        os.makedirs(node_dir, exist_ok=True)

        # Branch-per-node: the selected parent's code state (checked out by the
        # loop BEFORE planning) is the base; the task patch is committed on top
        # of it with a ``task_<genid>`` ref. A rejected/absent patch aliases the
        # base itself — the per-node lineage never has a gap. (When code_repo is
        # off, fall back to patching the run copy.)
        source_root = self.code_root or self.repo_root
        task_code_commit = None
        base_commit: Optional[str] = None
        code_ref_ok = True
        code_ref_mode = ""
        task_patch_files = []
        patch_applied = False
        task_patch_rejected = None
        if self.code_root and base:
            res = code_repo.apply_task_patch(
                self.code_root, "planner", patch_str, genid,
                (parent.genid if parent is not None else "initial"), base,
                seat="plan")
            task_code_commit = res["code_commit"]
            base_commit = res["base_commit"]
            code_ref_ok = bool(res["ref_ok"])
            code_ref_mode = str(res["ref_mode"])
            patch_applied = bool(res["applied"])
            if patch_applied:
                task_patch_files = code_repo.changed_files(patch_str)
            # A1 (batch 31): only a PROPOSED patch with no new commit is a
            # rejection -- "no patch proposed" must not masquerade as one.
            task_patch_rejected = task_patch_rejection(patch_str, task_code_commit, base_commit)
        elif (patch_str or "").strip() and self.code_root:
            # legacy path (no base resolved): pre-branch time-line HEAD behaviour
            try:
                task_code_commit = code_repo.apply_code_patch(
                    self.code_root, "planner", patch_str, f"task gen {genid}",
                    seat="plan")
                task_patch_files = code_repo.changed_files(patch_str)
                patch_applied = True
            except code_repo.PatchRejected as e:
                # B35 (batch 31): the exception text quotes the planner's own
                # artifacts (comments/identifiers inside the rejected diff) and
                # this field rides _EVALUATOR_META_KEYS -- meta carries the SAME
                # fixed generic string as the main path; the full text stays on
                # the local audit log only.
                task_patch_rejected = TASK_PATCH_REJECTED_NO_COMMIT
                try:
                    with open(self.log_path, "a", encoding="utf-8") as lf:
                        lf.write(f"[genid {genid}] task patch rejected (legacy path): {e}\n")
                except OSError:
                    pass  # best-effort local audit line; the meta string is durable
            patch_str = ""  # applied to code_root (or rejected -> nothing to apply)

        # H11/B24 (batch 5): heal the design BEFORE it is persisted. The patch has
        # now either committed or rolled back to the parent's code, so
        # ``source_root`` is the authority for what this generation can assemble.
        # The design object is shared with the loop (``config_dict``), so healing
        # in place strips a dangling slot name from the persisted design file, the
        # node meta and the parent->child inheritance chain together; the stripped
        # names are recorded (event + meta) so the planner's next receipt shows
        # what was dropped and why.
        design_stripped = tx.heal_design_slots(config, "task", source_root)
        if design_stripped:
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), dict(
                    {"type": "design_dangling_stripped", "genid": str(genid),
                     "role": "task", "slot": "tools"}, names=design_stripped))
            except Exception as e:  # noqa: BLE001 -- audit is advisory; stderr covers
                print(f"[WARN] design_dangling_stripped event write failed: {e}")

        # Framework: persist the design, prepare the minimal code copy, then
        # assemble sandbox-visible design/skills inside that copy.
        design_path = tx.persist_design(self.design_store, config, genid)
        model = self.default_model
        run_dir, patch_applied_copy = tx.prepare_run_dir(
            source_root, node_dir, patch_str, domain=self.domain,
        )
        env = tx.assemble_task_env(
            os.environ.copy(), design_path=design_path,
            run_dir=run_dir, config=config,
            code_root=self.code_root, task_brief=self.task_brief,
        )
        patch_applied = patch_applied or patch_applied_copy

        # B7: surface the per-inner task toolset assembly report (written by
        # assemble_task_env into the runtime dir). Recorded as node meta (so the
        # planner's next-round receipt and the evaluator's meta view carry it)
        # and as a task_toolset_assembled event. Skill-assembly gaps merely
        # degrade the child's capability — they must never silently vanish.
        toolset_report: Dict[str, Any] = {}
        toolset_report_path = os.path.join(run_dir, ".gan_runtime", "toolset_report.json")
        if os.path.isfile(toolset_report_path):
            try:
                with open(toolset_report_path, "r", encoding="utf-8") as f:
                    toolset_report = json.load(f)
            except Exception as e:  # noqa: BLE001 -- report is advisory meta
                toolset_report = {"error": f"unreadable toolset report: {e}"[:200]}
        if toolset_report:
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), dict(
                    {"type": "task_toolset_assembled", "genid": str(genid)},
                    **toolset_report))
            except Exception as e:  # noqa: BLE001 -- audit write failure: stderr covers
                print(f"[WARN] task_toolset_assembled event write failed: {e}")

        run_id = f"gan_{genid}"
        report_path = os.path.join(run_dir, "outputs", run_id, "report.json")
        if os.path.exists(report_path):
            os.remove(report_path)

        output_path = os.path.dirname(report_path)
        if tx.uses_parent_scoring(self.domain):
            questions_path, ground_truth_by_id = tx.prepare_questions(
                self.repo_root, run_dir, self.domain, self.subset, self.num_samples,
            )
            # The child sees questions only; scoring remains in this parent.
            rc, _out = tx.run_harness_and_report(
                self.python, run_dir, self.domain, run_id, self.subset,
                self.num_samples, model, env, self.timeout,
                questions_path=questions_path, scope_id=str(genid), log_path=self.log_path,
            )
            predictions_path = os.path.join(output_path, "predictions.csv")
            report = None
            if os.path.exists(predictions_path):
                try:
                    report = tx.write_parent_report(
                        predictions_path, report_path, self.domain, ground_truth_by_id,
                    )
                except Exception as e:
                    # B6 (adapted): a parent-scoring failure must not degrade
                    # silently; score reads None -> score_status="failed".
                    report = None
                    from utils.soft_fail import soft_fail
                    soft_fail(f"parent scoring failed for genid {genid}: {e}",
                              event_path=paths.events_path(self.output_dir),
                              event_type="parent_scoring_failed", genid=str(genid))
        else:
            # Preserve independent harness/evaluator domains outside G6a's scope.
            rc, _out = tx.run_harness_and_report(
                self.python, run_dir, self.domain, run_id, self.subset,
                self.num_samples, model, env, self.timeout,
                dataset_root=self.repo_root, scope_id=str(genid), log_path=self.log_path,
            )
            try:
                report = tx.run_existing_domain_report(self.domain, output_path, model)
            except Exception as e:
                # B6 (adapted): same visibility contract as the parent-scoring path.
                report = None
                from utils.soft_fail import soft_fail
                soft_fail(f"existing-domain report failed for genid {genid}: {e}",
                          event_path=paths.events_path(self.output_dir),
                          event_type="existing_domain_report_failed", genid=str(genid))
        # B27: merge the CHILD-reported load outcome into the toolset report (the
        # assembly stage can only report what it copied). Failures are classified
        # by owner: a component (or a role-owned always-on tool) is an agent-fixable
        # fact that rides into node meta -> the planner's next instruction; a
        # FROZEN always-on file is a framework bug, escalated and explicitly NOT
        # handed to an agent that cannot modify it.
        load_failed: List[Dict[str, Any]] = []
        load_report_path = os.path.join(run_dir, ".gan_runtime", "tools_load_report.json")
        if os.path.isfile(load_report_path):
            try:
                with open(load_report_path, "r", encoding="utf-8") as f:
                    load_report = json.load(f)
            except Exception as e:  # noqa: BLE001 -- advisory meta
                load_report = {"error": f"unreadable load report: {e}"[:200]}
            from gan.tools.assembly import load_failures
            load_failed = load_failures(load_report, "task",
                                        code_root=self.code_root)
            if toolset_report.get("skipped") is None:
                toolset_report["skipped"] = []
            toolset_report["load"] = {**load_report, "failed": load_failed}
            try:
                from gan.framework import paths as _paths
                from utils import trajectory_log as _tlog
                _tlog.append(_paths.events_path(self.output_dir), {
                    "type": "task_tools_loaded", "genid": str(genid),
                    "loaded": load_report.get("loaded") or [],
                    "failed": load_failed,
                    "framework_failed": [f for f in load_failed
                                         if f.get("kind") == "always_on_frozen"],
                })
            except Exception as e:  # noqa: BLE001 -- audit write failure
                print(f"[WARN] task_tools_loaded event write failed: {e}")

        if report is None and os.path.isfile(report_path):
            from utils.soft_fail import soft_fail
            soft_fail(f"report.json exists but is unparseable for genid {genid} "
                      f"(score will read None/failed): {report_path}")
        score = tx.extract_score(report, self.score_key)
        report_summary = tx.compute_report_summary(report, self.num_samples, self.output_contract)

        report_sha = scores.sha256_file(report_path)
        # copy small evidence out of the ephemeral work dir (which is pruned)
        try:
            evid = paths.runs_dir(self.output_dir, genid)
            os.makedirs(evid, exist_ok=True)
            for name in ("report.json", "predictions.csv"):
                src = os.path.join(os.path.dirname(report_path), name)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(evid, name))
        except Exception as e:
            from utils.soft_fail import soft_fail
            soft_fail(f"evidence copy failed for genid {genid}: {e}",
                      event_path=paths.events_path(self.output_dir),
                      event_type="evidence_copy_failed", genid=str(genid))

        # score status (unscored taxonomy; imputation is decided by the loop)
        if score is None:
            score_status = "failed"
            invalid_reason = None if report is not None else (
                "harness_failed" if rc != 0 else "no_report"
            )
        else:
            cov = report_summary.get("coverage")
            score_status = "partial" if (cov is not None and cov < 1.0) else "ok"
            invalid_reason = None

        parent_score = parent.value.score if parent is not None else None
        delta = (score - parent_score) if (score is not None and parent_score is not None) else None

        return Node(
            genid=genid,
            parent_genid=(parent.genid if parent is not None else None),
            curr_patch_files=[],
            value=NodeValue(score=score, score_status=score_status,
                            coverage=report_summary.get("coverage")),
            scores={self.domain: score},
            modify_depth=_modify_depth(records),
            meta={
                "run_dir": run_dir,
                "report_path": report_path,
                "report_sha": report_sha,
                "design_path": design_path,
                "harness_rc": rc,
                "model": model,
                "delta_vs_parent": delta,
                "patch_applied": patch_applied,
                "task_code_commit": task_code_commit,
                "code_commit": task_code_commit,
                "base_commit": base_commit,
                "code_ref_ok": code_ref_ok,
                "code_ref_mode": code_ref_mode,
                "task_patch_files": task_patch_files,
                "task_patch_rejected": task_patch_rejected,
                "records": records,
                "report_summary": report_summary,
                "score_status": score_status,
                "invalid_reason": invalid_reason,
                "toolset_report": toolset_report,
                "design_stripped": design_stripped or None,
            },
        )
