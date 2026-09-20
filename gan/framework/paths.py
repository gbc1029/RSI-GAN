"""Canonical output-directory layout (FRAMEWORK, frozen).

Everything a run writes lives under ONE of the main buckets:

    <output_dir>/
      ckpt/        recovery + state
        checkpoint.json            latest snapshot (overwritten, atomic)
        outer_<N>.json             immutable per-outer snapshot
        inner_<N>_<i>.json         lightweight inner snapshot
        design/                    role designs (the evolvable *state*)
          planner/outer_<N>.json
          evaluator/outer_<N>.json
          task/<genid>.json
      logs/        append-only logs
        events.jsonl
        task_tree.jsonl / planner_tree.jsonl / evaluator_tree.jsonl
        task_runner.log
      trajectory/  conversation / execution traces (JSONL, per instance)
        outer_<O>/
          planner.jsonl            (outer-level self-improvement)
          evaluator.jsonl
          <inner_genid>/
            task.jsonl
            planner.jsonl
            evaluator.jsonl
      scores/      quick score index
        scores.jsonl
      runs/<genid>/   small evidence (packet.json, feedback_digest.md)
      work/<genid>/   EPHEMERAL run dir (repo copy, skills) -> pruned after run
      workspaces/<role>/...  granted source copies (cleared per outer)

This module is the single place that knows these paths; other framework modules
must go through it.
"""
from __future__ import annotations

import os
from typing import Optional


# -- ckpt -------------------------------------------------------------------
def ckpt_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "ckpt")


def code_root(output_dir: str) -> str:
    """Per-run git working tree holding the evolvable code (see code_repo)."""
    return os.path.join(ckpt_dir(output_dir), "code")


def code_manifest(output_dir: str) -> str:
    return os.path.join(ckpt_dir(output_dir), "code.json")


def checkpoint_latest(output_dir: str) -> str:
    return os.path.join(ckpt_dir(output_dir), "checkpoint.json")


def checkpoint_outer(output_dir: str, index: int) -> str:
    return os.path.join(ckpt_dir(output_dir), f"outer_{index}.json")


def checkpoint_inner(output_dir: str, outer: int, inner: int) -> str:
    return os.path.join(ckpt_dir(output_dir), f"inner_{outer}_{inner}.json")


def design_root(output_dir: str) -> str:
    return os.path.join(ckpt_dir(output_dir), "design")


# -- logs -------------------------------------------------------------------
def logs_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "logs")


def events_path(output_dir: str) -> str:
    return os.path.join(logs_dir(output_dir), "events.jsonl")


def tree_path(output_dir: str, name: str) -> str:
    return os.path.join(logs_dir(output_dir), f"{name}_tree.jsonl")


def log_file(output_dir: str, name: str) -> str:
    return os.path.join(logs_dir(output_dir), name)


# -- scores -----------------------------------------------------------------
def scores_path(output_dir: str) -> str:
    return os.path.join(output_dir, "scores", "scores.jsonl")


# -- trajectory -------------------------------------------------------------
def trajectory_root(output_dir: str) -> str:
    return os.path.join(output_dir, "trajectory")


def outer_traj_dir(output_dir: str, outer) -> str:
    return os.path.join(trajectory_root(output_dir), f"outer_{outer}")


def session_traj_file(output_dir: str, outer, genid: Optional[object], role: str) -> str:
    """Path of one session's JSONL trajectory, creating its directory."""
    base = outer_traj_dir(output_dir, outer)
    if genid is not None:
        base = os.path.join(base, str(genid))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{role}.jsonl")


# -- other ------------------------------------------------------------------
def runs_dir(output_dir: str, genid: Optional[object] = None) -> str:
    base = os.path.join(output_dir, "runs")
    return os.path.join(base, str(genid)) if genid is not None else base


def work_dir(output_dir: str, genid: object) -> str:
    return os.path.join(output_dir, "work", str(genid))


def workspaces_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "workspaces")


def ensure(output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
