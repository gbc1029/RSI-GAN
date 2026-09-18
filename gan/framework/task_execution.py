"""Task execution (FRAMEWORK, frozen).

Design persistence + harness glue for one task-agent generation, moved out of
``gan/task_runner.py`` so the execution/measurement path is part of the frozen
framework. Role agents must not modify how a design is persisted or how the
domain harness/report is invoked.

Responsibilities:
- persist the task design snapshot (``DesignStore``);
- assemble the task toolset and runtime env (GAN_TASK_*);
- prepare the run dir (copy repo + apply patch when a deep change was requested);
- invoke ``domains.harness`` + ``domains.report``;
- read the report and compute the objective ``report_summary``
  (contract / coverage facts — NOT an evaluator eval point).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from gan.design.store import DesignStore
from gan.patch import apply_patch
from gan.tools.assembly import assemble_tools_dir

_COPY_IGNORE = shutil.ignore_patterns(
    "venv_nat", "outputs", ".git", "__pycache__", "*.pyc",
    "polyglot-benchmark", "SWE-bench", "logs",
)


# -- design persistence -----------------------------------------------------
def persist_design(design_store: DesignStore, config: Dict[str, Any], genid: Any) -> str:
    return design_store.save(config or {}, "task", node_id=genid)


def assemble_task_env(
    base_env: Dict[str, str],
    *,
    design_path: str,
    node_dir: str,
    config: Dict[str, Any],
) -> Dict[str, str]:
    """Runtime env for the task agent (design/skills only; model is a CLI arg)."""
    env = dict(base_env)
    env["GAN_TASK_DESIGN"] = design_path
    skills_dir = os.path.join(node_dir, "skills")
    assemble_tools_dir("task", skills_dir, config=config, include_always_on=False)
    env["GAN_TASK_SKILLS_DIR"] = skills_dir
    return env


# -- run dir ----------------------------------------------------------------
def prepare_run_dir(repo_root: str, node_dir: str, patch_str: str) -> Tuple[str, bool]:
    """Return (run_dir, patch_applied). A patch uses a throwaway repo copy."""
    if not (patch_str or "").strip():
        return repo_root, False
    run_dir = os.path.join(node_dir, "repo")
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    shutil.copytree(repo_root, run_dir, ignore=_COPY_IGNORE)
    return run_dir, bool(apply_patch(run_dir, patch_str))


# -- harness / report -------------------------------------------------------
def run_harness_and_report(
    python: str,
    run_dir: str,
    domain: str,
    run_id: str,
    subset: str,
    num_samples: int,
    model: str,
    env: Dict[str, str],
    timeout: int,
    log_path: Optional[str] = None,
) -> Tuple[int, str]:
    """Run the (frozen) domain harness + report; return (harness_rc, output_tail)."""
    harness_cmd = [
        python, "-m", "domains.harness",
        "--domain", domain,
        "--model", model,
        "--run_id", run_id,
        "--subset", subset,
        "--num_samples", str(num_samples),
    ]
    proc = subprocess.run(
        harness_cmd, cwd=run_dir, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    subprocess.run(
        [python, "-m", "domains.report", "--domain", domain,
         "--dname", os.path.join("./outputs", run_id)],
        cwd=run_dir, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    out = proc.stdout or ""
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(harness_cmd)} (cwd={run_dir})\n{out[-4000:]}\n")
    return proc.returncode, out


def read_report(report_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def extract_score(report: Optional[Dict[str, Any]], score_key: str) -> Optional[float]:
    """Numeric objective score, or None when absent/NaN (score standard unchanged)."""
    if not isinstance(report, dict) or not score_key:
        return None
    val = report.get(score_key)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# -- objective contract / coverage facts ------------------------------------
def _normalize(value: Any, contract: Dict[str, Any]) -> str:
    if value is None:
        return ""
    s = str(value)
    if contract.get("strip", True):
        s = s.strip()
    if contract.get("case_insensitive", True):
        s = s.lower()
    return s


def compute_report_summary(
    report: Optional[Dict[str, Any]],
    requested: int,
    contract: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Framework-computed objective facts injected into run_summary.

    This is measurement, not an evaluator eval point.
    """
    contract = contract or {}
    requested = int(requested) if (requested and requested > 0) else None
    summary: Dict[str, Any] = {
        "sample_n_requested": requested,
        "sample_n_valid": None,
        "coverage": None,
        "invalid_rate": None,
        "contract_ok": None,
        "label_distribution": None,
        "overall_accuracy": None,
        "random_guess_accuracy": None,
    }
    if not isinstance(report, dict):
        return summary

    summary["overall_accuracy"] = report.get("overall_accuracy")
    summary["random_guess_accuracy"] = report.get("random_guess_accuracy")
    summary["label_distribution"] = report.get("label_distribution")

    n_valid = report.get("total")
    if isinstance(n_valid, (int, float)):
        n_valid = int(n_valid)
        summary["sample_n_valid"] = n_valid
        if requested:
            coverage = n_valid / requested
            summary["coverage"] = coverage
            summary["invalid_rate"] = 1.0 - coverage
    else:
        n_valid = None

    if contract.get("kind") == "label":
        allowed = {_normalize(a, contract) for a in (contract.get("allowed_labels") or [])}
        dist = (summary.get("label_distribution") or {}).get("prediction") or {}
        if dist:
            total_pred = sum(dist.values()) or 1
            ok = sum(v for k, v in dist.items() if _normalize(k, contract) in allowed)
            summary["contract_ok"] = bool(ok == total_pred)
        else:
            summary["contract_ok"] = None  # nothing to judge
    return summary
