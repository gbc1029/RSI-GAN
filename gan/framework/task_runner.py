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
import sys
from typing import Any, Dict, List, Optional

from gan.framework import task_execution as tx
from gan.framework.loader import load_registry, resolve_domain
from gan.design.store import DesignStore
from gan.framework.tree.store import Node, NodeValue

_CODE_OPS = {"code_edit"}


def _modify_depth(records: List[Dict[str, Any]]) -> int:
    ops = {r.get("op") for r in records or []}
    if ops & _CODE_OPS:
        return 2
    if ops:
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
    ):
        self.repo_root = os.path.abspath(repo_root)
        self.output_dir = os.path.abspath(output_dir)
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

        self.design_store = DesignStore(os.path.join(self.output_dir, "design"))
        self.log_path = os.path.join(self.output_dir, "task_runner.log")

    # -- helpers -----------------------------------------------------------
    def _resolve_model(self, config: Any) -> str:
        if isinstance(config, dict):
            m = (config.get("params") or {}).get("model")
            if m:
                return m
        return os.environ.get("GAN_TASK_MODEL") or self.default_model

    # -- runner ------------------------------------------------------------
    def __call__(self, plan: Optional[Dict[str, Any]] = None, parent: Optional[Node] = None, genid: Any = None) -> Node:
        plan = plan or {}
        records = plan.get("records", []) or []
        config = plan.get("config")
        if not isinstance(config, dict):
            config = {}
        patch_str = plan.get("patch", "") or ""

        node_dir = os.path.join(self.output_dir, "nodes", str(genid))
        os.makedirs(node_dir, exist_ok=True)

        # framework: design persistence + env/toolset assembly + run dir
        design_path = tx.persist_design(self.design_store, config, genid)
        model = self._resolve_model(config)
        env = tx.assemble_task_env(
            os.environ.copy(), model=model, design_path=design_path,
            node_dir=node_dir, config=config,
        )
        run_dir, patch_applied = tx.prepare_run_dir(self.repo_root, node_dir, patch_str)

        run_id = f"gan_{genid}"
        report_path = os.path.join(run_dir, "outputs", run_id, "report.json")
        if os.path.exists(report_path):
            os.remove(report_path)

        # framework: harness + report invocation (frozen measurement path)
        rc, _out = tx.run_harness_and_report(
            self.python, run_dir, self.domain, run_id, self.subset,
            self.num_samples, env, self.timeout, log_path=self.log_path,
        )
        report = tx.read_report(report_path)
        score = tx.extract_score(report, self.score_key)
        report_summary = tx.compute_report_summary(report, self.num_samples, self.output_contract)

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
            curr_patch_files=[design_path],
            value=NodeValue(score=score, score_status=score_status,
                            coverage=report_summary.get("coverage")),
            scores={self.domain: score},
            modify_depth=_modify_depth(records),
            meta={
                "run_dir": run_dir,
                "report_path": report_path,
                "design_path": design_path,
                "harness_rc": rc,
                "model": model,
                "delta_vs_parent": delta,
                "patch_applied": patch_applied,
                "records": records,
                "report_summary": report_summary,
                "score_status": score_status,
                "invalid_reason": invalid_reason,
            },
        )
