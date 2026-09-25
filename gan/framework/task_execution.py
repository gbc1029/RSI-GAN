"""Task execution (FRAMEWORK, frozen).

Design persistence + harness glue for one task-agent generation, moved out of
``gan/task_runner.py`` so the execution/measurement path is part of the frozen
framework. Role agents must not modify how a design is persisted or how the
domain harness/report is invoked.

Responsibilities:
- persist the task design snapshot (``DesignStore``);
- assemble the task toolset and runtime env (GAN_TASK_*);
- prepare the run dir (copy repo + apply patch when a deep change was requested);
- prepare questions-only input and parent-owned ground truth;
- invoke ``domains.harness`` and score its predictions in the parent process;
- read the report and compute the objective ``report_summary``
  (contract / coverage facts — NOT an evaluator eval point).
"""
from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from gan.design.store import DesignStore
from gan.patch import apply_patch
from gan.tools.assembly import assemble_tools_dir

_PY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
# Benchmark labels / heavy assets must NOT be present in a task run (leakage
# isolation). The parent harness reads labels from an explicit CLI path that is
# never forwarded to the sandboxed task worker.
_DOMAIN_IGNORE = shutil.ignore_patterns(
    "dataset*.csv", "*bench*.csv", "polyglot-benchmark", "SWE-bench",
    "saved", "predictions", "logs", "__pycache__", "*.pyc",
)
# Allowlist of what a task run actually needs (never copy the whole repo).
_RUNTIME_FILES = ["task_agent.py"]
_RUNTIME_DIRS = ["agent", "utils"]
_HARNESS_FILES = [
    "domains/__init__.py",
    "domains/harness.py",
    "domains/report.py",
    "domains/task_worker.py",
]
_PARENT_SCORED_DOMAINS = {"paper_review", "search_arena", "imo_grading"}


# -- design persistence -----------------------------------------------------
def persist_design(design_store: DesignStore, config: Dict[str, Any], genid: Any) -> str:
    return design_store.save(config or {}, "task", node_id=genid)


def assemble_task_env(
    base_env: Dict[str, str],
    *,
    design_path: str,
    run_dir: str,
    config: Dict[str, Any],
    code_root: Optional[str] = None,
    task_brief: Optional[str] = None,
) -> Dict[str, str]:
    """Build the environment and sandbox-visible runtime assets for TaskAgent.

    The design and assembled skills are copied below ``run_dir`` and referenced
    through their paths inside the task sandbox. Benchmark paths are explicitly
    removed from the inherited environment.
    """
    env = dict(base_env)
    env.pop("GAN_DATASET_ROOT", None)

    runtime_dir = os.path.join(run_dir, ".gan_runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    shutil.copy2(design_path, os.path.join(runtime_dir, "design.json"))

    skills_dir = os.path.join(runtime_dir, "skills")
    assemble_tools_dir("task", skills_dir, config=config, include_always_on=False,
                       code_root=code_root)
    env["GAN_TASK_DESIGN"] = "/workspace/.gan_runtime/design.json"
    env["GAN_TASK_SKILLS_DIR"] = "/workspace/.gan_runtime/skills"
    if task_brief:
        env["GAN_TASK_BRIEF"] = task_brief
    return env


# -- run dir ----------------------------------------------------------------
def prepare_run_dir(source_root: str, node_dir: str, patch_str: str, domain: Optional[str] = None) -> Tuple[str, bool]:
    """Return (run_dir, patch_applied).

    Builds a **minimal allowlist copy** of the code (agent runtime + the domain
    package, minus datasets) from ``source_root`` (the per-run code tree), so a
    task run cannot see benchmark labels and is far smaller than a full repo
    copy. A deep patch, if any, is applied inside the copy only.
    """
    run_dir = os.path.join(node_dir, "repo")
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir, exist_ok=True)

    for name in _RUNTIME_FILES + _HARNESS_FILES:
        src = os.path.join(source_root, name)
        if os.path.isfile(src):
            dst = os.path.join(run_dir, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for name in _RUNTIME_DIRS:
        src = os.path.join(source_root, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(run_dir, name), ignore=_PY_IGNORE)
    if domain:
        dom = domain.split("_")[0] if "imo_" in domain else domain
        src = os.path.join(source_root, "domains", dom)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(run_dir, "domains", dom), ignore=_DOMAIN_IGNORE)
    envf = os.path.join(source_root, ".env")
    if os.path.isfile(envf):
        shutil.copy2(envf, os.path.join(run_dir, ".env"))

    applied = bool(apply_patch(run_dir, patch_str)) if (patch_str or "").strip() else False
    return run_dir, applied


# -- dataset / harness / report --------------------------------------------
def uses_parent_scoring(domain: str) -> bool:
    return domain in _PARENT_SCORED_DOMAINS


def prepare_questions(
    dataset_root: str,
    run_dir: str,
    domain: str,
    subset: str,
    num_samples: int,
) -> Tuple[str, Dict[str, Any]]:
    """Write questions-only input and retain ground truth in parent memory."""
    utils_prefix = domain.split("_", 1)[1] + "_" if domain.startswith("imo_") else ""
    domain_folder = domain.split("_")[0] if "imo_" in domain else domain
    utils_module = importlib.import_module(
        f"domains.{domain_folder}.{utils_prefix}utils"
    )
    question_id_col = utils_module.QUESTION_ID
    ground_truth_key = utils_module.GROUND_TRUTH_KEY

    if "imo_" in domain:
        rel = f"domains/imo/{domain.split('_')[-1]}bench{subset}.csv"
    else:
        rel = f"domains/{domain}/dataset{subset}.csv"
    dataset = pd.read_csv(os.path.join(os.path.abspath(dataset_root), rel), dtype=str)
    if num_samples > 0:
        dataset = dataset[:num_samples]

    ground_truth_by_id = dict(
        zip(dataset[question_id_col].tolist(), dataset[ground_truth_key].tolist())
    )
    questions = dataset.drop(columns=[ground_truth_key])
    input_dir = os.path.join(run_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    questions_path = os.path.join(input_dir, "questions.csv")
    questions.to_csv(questions_path, index=False)
    return questions_path, ground_truth_by_id


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
    questions_path: Optional[str] = None,
    dataset_root: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Tuple[int, str]:
    """Run the frozen harness; parent-side reporting happens separately."""
    harness_cmd = [
        python, "-m", "domains.harness",
        "--domain", domain,
        "--model", model,
        "--run_id", run_id,
        "--subset", subset,
        "--num_samples", str(num_samples),
    ]
    if questions_path:
        harness_cmd.extend(["--questions_path", os.path.abspath(questions_path)])
    elif dataset_root:
        # Compatibility path for domains with their own evaluator/harness.
        harness_cmd.extend(["--dataset_root", os.path.abspath(dataset_root)])
    proc = subprocess.run(
        harness_cmd, cwd=run_dir, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    out = proc.stdout or ""
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(harness_cmd)} (cwd={run_dir})\n{out[-4000:]}\n")
    return proc.returncode, out


def write_parent_report(
    predictions_path: str,
    report_path: str,
    domain: str,
    ground_truth_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    """Score prediction-only output and persist the existing report schema."""
    from domains.report import compute_report_from_predictions

    predictions = pd.read_csv(predictions_path, dtype=str)
    report = compute_report_from_predictions(
        predictions, domain, ground_truth_by_id,
    )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4)
    return report


def run_existing_domain_report(domain: str, dname: str, model: str) -> Optional[Dict[str, Any]]:
    """Call the existing independent-domain report entry point in this parent."""
    if domain == "imo_proof":
        from domains.report import report_imo_proof
        report_imo_proof(dname=dname, model=model)
    elif "balrog" in domain:
        from domains.balrog.eval import report_balrog
        report_balrog(output_dir=dname)
    elif "genesis" in domain:
        from domains.genesis.eval import report_genesis
        report_genesis(output_dir=dname)
    return read_report(os.path.join(dname, "report.json"))


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
