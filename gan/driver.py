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
    domains: Optional[List[str]] = None,
    task_domain: str = "paper_review",
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    inner: Optional[int] = None,
    cfg_overrides: Optional[dict] = None,
    preflight: bool = False,
) -> str:
    from gan.build import ensure_code_root
    from gan.framework.loader import load_gan_loop_config

    cfg = load_gan_loop_config(cfg_overrides)
    G = int(cfg.get("loop.outer_generations", 2))
    repo_root = os.path.abspath(repo_root)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    code_root = ensure_code_root(repo_root, output_dir)
    _log_event(output_dir, {"type": "driver_start", "code_root": code_root,
                            "outer_generations": G})

    if preflight:
        from gan.framework import preflight as preflight_mod
        results = preflight_mod.preflight(["gan.task", "gan.planner", "gan.evaluator"])
        _log_event(output_dir, {"type": "preflight", "results": results})
        if not preflight_mod.all_ok(results):
            raise RuntimeError(f"model preflight failed: {results}")

    env = dict(os.environ)
    env["PYTHONPATH"] = code_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep code_root clean (no __pycache__)

    for outer in range(1, G + 1):
        cmd = [
            sys.executable, "-m", "gan.outer_worker",
            "--repo_root", repo_root, "--output_dir", output_dir,
            "--outer", str(outer), "--task-domain", task_domain,
            "--subset", subset, "--num_samples", str(num_samples),
        ]
        if domains:
            cmd.extend(["--domains", ",".join(domains)])
        if inner is not None:
            cmd.extend(["--inner", str(inner)])
        proc = subprocess.run(cmd, cwd=code_root, env=env)
        if proc.returncode != 0:
            _log_event(output_dir, {"type": "outer_worker_failed", "outer": outer,
                                    "rc": proc.returncode})
            raise RuntimeError(f"outer worker failed at outer {outer} (rc={proc.returncode})")
        # role self-edits are applied+committed by the worker itself (option B)

    _log_event(output_dir, {"type": "driver_done", "code_root": code_root})
    return code_root
