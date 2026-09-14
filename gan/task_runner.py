"""Concrete task runners.

``DomainTaskRunner`` runs the ORIGINAL DGM-H per-domain harness (``domains.harness``)
+ ``domains.report`` for a generation and returns a task-tree ``Node`` with the
benchmark score read from ``report.json``.

Default modification surface (config) is injected via the ``GAN_TASK_MODEL``
environment variable, which ``domains/harness.py`` honours. Code patches
(``code_edit``) are applied to a throwaway repo copy for that generation only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from gan.config.loader import load_registry, resolve_domain
from gan.patch import apply_patch
from gan.tree.store import Node, NodeValue

_COPY_IGNORE = shutil.ignore_patterns(
    "venv_nat", "outputs", ".git", "__pycache__", "*.pyc",
    "polyglot-benchmark", "SWE-bench", "logs",
)

_CODE_OPS = {"code_edit"}
_CONFIG_ONLY_OPS = {"tune_param", "apply_config", "add_config"}


def _modify_depth(records: List[Dict[str, Any]]) -> int:
    ops = {r.get("op") for r in records or []}
    if ops & _CODE_OPS:
        return 2
    if ops - _CONFIG_ONLY_OPS:
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

    # -- helpers -----------------------------------------------------------
    def _resolve_model(self, config: Any) -> str:
        if config is not None:
            m = config.get("models.task") or config.get("agent.model")
            if m:
                return m
        return os.environ.get("GAN_TASK_MODEL") or self.default_model

    def _run(self, cmd: List[str], cwd: str, env: Dict[str, str]) -> int:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.timeout,
        )
        with open(os.path.join(self.output_dir, "task_runner.log"), "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(cmd)} (cwd={cwd})\n{proc.stdout[-4000:]}\n")
        return proc.returncode

    # -- runner ------------------------------------------------------------
    def __call__(self, plan: Optional[Dict[str, Any]] = None, parent: Optional[Node] = None, genid: Any = None) -> Node:
        plan = plan or {}
        records = plan.get("records", []) or []
        config = plan.get("config")
        patch_str = plan.get("patch", "") or ""

        node_dir = os.path.join(self.output_dir, "nodes", str(genid))
        os.makedirs(node_dir, exist_ok=True)
        if config is not None:
            with open(os.path.join(node_dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)

        model = self._resolve_model(config)
        env = os.environ.copy()
        env["GAN_TASK_MODEL"] = model

        run_dir = self.repo_root
        patch_applied = False
        if patch_str.strip():
            run_dir = os.path.join(node_dir, "repo")
            if os.path.exists(run_dir):
                shutil.rmtree(run_dir)
            shutil.copytree(self.repo_root, run_dir, ignore=_COPY_IGNORE)
            patch_applied = apply_patch(run_dir, patch_str)

        run_id = f"gan_{genid}"
        report_path = os.path.join(run_dir, "outputs", run_id, "report.json")
        if os.path.exists(report_path):
            os.remove(report_path)

        harness_cmd = [
            self.python, "-m", "domains.harness",
            "--domain", self.domain,
            "--run_id", run_id,
            "--subset", self.subset,
            "--num_samples", str(self.num_samples),
        ]
        rc = self._run(harness_cmd, run_dir, env)
        self._run([self.python, "-m", "domains.report", "--domain", self.domain,
                   "--dname", os.path.join("./outputs", run_id)], run_dir, env)

        score = None
        if os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                score = report.get(self.score_key)
            except Exception:
                score = None

        parent_score = parent.value.score if parent is not None else None
        delta = (score - parent_score) if (score is not None and parent_score is not None) else None

        return Node(
            genid=genid,
            parent_genid=(parent.genid if parent is not None else None),
            curr_patch_files=[os.path.join(node_dir, "config.json")],
            value=NodeValue(score=score),
            scores={self.domain: score},
            modify_depth=_modify_depth(records),
            meta={
                "run_dir": run_dir,
                "report_path": report_path,
                "harness_rc": rc,
                "model": model,
                "delta_vs_parent": delta,
                "patch_applied": patch_applied,
                "records": records,
            },
        )
