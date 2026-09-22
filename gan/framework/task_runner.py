"""Concrete task runners (thin glue).

Heavy lifting (design persistence, env assembly, repo copy/patch, harness/report
invocation and the objective ``report_summary``) lives in the frozen framework:
``gan/framework/task_execution.py``.

``DomainTaskRunner`` runs the ORIGINAL DGM-H per-domain harness
(``domains.harness``) + ``domains.report`` for a generation and returns a
task-tree ``Node``. The task agent is **design-driven**: its design config
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
    records = records or []
    if any(
        r.get("op") == "code_edit"
        or (r.get("op") == "request_source_access" and r.get("intent") == "modify")
        for r in records
    ):
        return 2
    if records:
        return 1
    return 0


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
                (parent.genid if parent is not None else "initial"), base)
            task_code_commit = res["code_commit"]
            base_commit = res["base_commit"]
            code_ref_ok = bool(res["ref_ok"])
            code_ref_mode = str(res["ref_mode"])
            patch_applied = bool(res["applied"])
            if patch_applied:
                task_patch_files = code_repo.changed_files(patch_str)
            if task_code_commit == base_commit and task_code_commit:
                task_patch_rejected = "no new code commit (patch rejected or absent)"
        elif (patch_str or "").strip() and self.code_root:
            # legacy path (no base resolved): pre-branch time-line HEAD behaviour
            try:
                task_code_commit = code_repo.apply_code_patch(
                    self.code_root, "planner", patch_str, f"task gen {genid}")
                task_patch_files = code_repo.changed_files(patch_str)
                patch_applied = True
            except code_repo.PatchRejected as e:
                task_patch_rejected = str(e)
            patch_str = ""  # applied to code_root (or rejected -> nothing to apply)

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

        run_id = f"gan_{genid}"
        report_path = os.path.join(run_dir, "outputs", run_id, "report.json")
        if os.path.exists(report_path):
            os.remove(report_path)

        # framework: harness + report invocation (frozen measurement path); model passed explicitly
        rc, _out = tx.run_harness_and_report(
            self.python, run_dir, self.domain, run_id, self.subset,
            self.num_samples, model, env, self.timeout,
            dataset_root=self.repo_root, log_path=self.log_path,
        )
        report = tx.read_report(report_path)
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
        except Exception:
            pass

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
            },
        )
