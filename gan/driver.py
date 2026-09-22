"""Outer-loop driver (glue): one worker subprocess per outer + self-edit apply.

The driver runs on the real repo (labels live here) and only orchestrates:
- materialize the per-run code tree once;
- spawn ``gan.outer_worker`` for each outer (cwd/PYTHONPATH = code_root);
- after each outer, apply that outer's role self-edit patches to the code tree
  (allowlist + compile validated, rolled back on failure);
- audit everything to ``logs/events.jsonl``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from gan.framework import paths


def _log_event(output_dir: str, event: Dict[str, Any]) -> None:
    from utils import trajectory_log
    trajectory_log.append(paths.events_path(output_dir), event)


def run_gan_driver(
    repo_root: str,
    output_dir: str,
    domains: List[str],
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    cfg_overrides: Optional[dict] = None,
    preflight: bool = False,
    resume: bool = False,
    force: bool = False,
) -> str:
    from gan.build import ensure_code_root
    from gan.framework import checkpoint as ckpt_mod
    from gan.framework.loader import load_gan_loop_config, load_registry

    if resume and force:
        raise ValueError("--resume and --force are mutually exclusive")

    cfg = load_gan_loop_config(cfg_overrides)
    G = int(cfg.get("loop.outer_generations", 2))
    repo_root = os.path.abspath(repo_root)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Re-entry policy (G1-A, P1). The resume lineage follows the chronologically
    # NEWEST outer snapshot (canonical or archival re-run copy): start =
    # that snapshot's outer index + 1 — NOT max(index), which would wrongly jump
    # to the tail of the previous run when a re-run started from the middle.
    newest = ckpt_mod.newest_outer_snapshot(output_dir)      # None on a fresh dir
    if newest and not (resume or force):
        raise RuntimeError(
            f"output_dir already contains outer snapshot(s) (latest completed "
            f"outer = {newest['index']}); re-running from outer 1 would pollute "
            f"the tree/UCB. Use --resume to continue at outer {newest['index'] + 1}, "
            f"or --force to re-run from outer 1 (legacy duplicate-prone behaviour, "
            f"explicit escape hatch).")

    # Domain shape checks (the ONLY default lives in scripts/run_gan.py; here the
    # value is required input). Empty/None and multi-value both fail fast before
    # any task execution. Multi-domain evaluation is not implemented yet.
    if not domains or not [d for d in domains if str(d).strip()]:
        raise ValueError(f"--domains is required and must be non-empty (got {domains!r}); "
                         f"refusing to run without an explicit task domain")
    if len(domains) != 1:
        raise ValueError(f"--domains accepts exactly one domain (got {domains}); "
                         f"multi-domain evaluation is not implemented yet")
    domain = domains[0].strip() if isinstance(domains[0], str) else domains[0]
    if not domain:
        raise ValueError(f"--domains parses to an empty domain (got {domains!r})")
    domain = str(domain)
    reg = load_registry()
    registered = set((reg.get("domains", {}) or {}).keys())
    families = set((reg.get("families", {}) or {}).keys())
    if domain not in registered and not any(domain.startswith(f) for f in families):
        _log_event(output_dir, {"type": "domain_unregistered", "domain": domain,
                                "hint": "not in domains.yaml domains/families; "
                                        "task_brief/output_contract fall back to default"})

    code_root = ensure_code_root(repo_root, output_dir)

    # P1 resume: the workers restore the newest OUTER-boundary checkpoint (by
    # completed_ts, following the newest attempt's lineage); verify the code tree
    # matches that snapshot's commit (G2-lite) BEFORE spawning.
    start = (int(newest["index"]) + 1) if (resume and newest) else 1
    _log_event(output_dir, {"type": "driver_start", "code_root": code_root,
                            "outer_generations": G,
                            "domain": domain,
                            "domains": list(domains),
                            "resume": resume, "force": force,
                            "latest_completed_outer": (newest["index"] if newest else None),
                            "start_outer": start})

    if resume:
        if start > G:
            _log_event(output_dir, {"type": "resume_complete",
                                    "latest_completed_outer": (newest["index"] if newest else None),
                                    "outer_generations": G})
            print(f"Nothing to resume: outer {newest['index']} is the newest snapshot; "
                  f"outer_generations={G} already covered.")
            return code_root
        payload = ckpt_mod.load_checkpoint(output_dir, "outer") or {}
        commit = (payload.get("code") or {}).get("commit")
        if code_root and commit:
            from gan.framework import code_repo
            cur = code_repo.current_commit(code_root)
            if cur != commit:
                code_repo.checkout(code_root, commit)
                _log_event(output_dir, {"type": "code_state_restored",
                                        "from": cur, "to": commit})
        else:
            _log_event(output_dir, {"type": "code_layer_absent",
                                    "hint": "checkpoint predates the code layer"})

    if preflight:
        from gan.framework import preflight as preflight_mod
        results = preflight_mod.preflight(["gan.task", "gan.planner", "gan.evaluator"])
        _log_event(output_dir, {"type": "preflight", "results": results})
        if not preflight_mod.all_ok(results):
            raise RuntimeError(f"model preflight failed: {results}")

    env = dict(os.environ)
    env["PYTHONPATH"] = code_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep code_root clean (no __pycache__)

    for outer in range(start, G + 1):
        cmd = [
            sys.executable, "-m", "gan.outer_worker",
            "--repo_root", repo_root, "--output_dir", output_dir,
            "--outer", str(outer), "--domains", domain,
            "--subset", subset, "--num_samples", str(num_samples),
        ]
        if resume:
            # P1: worker restores the latest OUTER-boundary checkpoint, never a
            # crashed outer's partial inner state.
            cmd += ["--resume-boundary", "outer"]
        proc = subprocess.run(cmd, cwd=code_root, env=env)
        if proc.returncode != 0:
            _log_event(output_dir, {"type": "outer_worker_failed", "outer": outer,
                                    "rc": proc.returncode})
            raise RuntimeError(f"outer worker failed at outer {outer} (rc={proc.returncode})")
        # role self-edits are applied+committed by the worker itself (option B)

    _log_event(output_dir, {"type": "driver_done", "code_root": code_root})
    return code_root
