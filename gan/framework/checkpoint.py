"""Outer-loop checkpointing (FRAMEWORK, frozen).

The task tree is event-sourced, but planner/evaluator are single mutable
instances and loop scalars live only in memory. This module persists a full
snapshot so a run can be resumed or rolled back to the last outer-generation
boundary.

Snapshot contents:
- ``state``  : loop scalars (counters, digests, last feedback, seed, position)
- ``trees``  : ``{name: [node_dict, ...]}`` for task/planner/evaluator
- ``designs``: ``{role: config}`` snapshots for planner/evaluator
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from gan.design.store import DesignStore

_TREE_NAMES = ("task", "planner", "evaluator")


def _ckpt_path(output_dir: str) -> str:
    return os.path.join(output_dir, "checkpoint.json")


def _boundary_path(output_dir: str, boundary: str) -> str:
    return os.path.join(output_dir, "checkpoints", f"{boundary}.json")


def snapshot_designs(output_dir: str, roles: List[str]) -> Dict[str, Any]:
    """Snapshot role designs, only for roles that already have a design file."""
    store = DesignStore(os.path.join(output_dir, "design"))
    out: Dict[str, Any] = {}
    for role in roles:
        p = store.path(role)
        if os.path.exists(p):
            try:
                out[role] = store.load(role)
            except Exception:
                pass
    return out


def restore_designs(output_dir: str, designs: Dict[str, Any]) -> None:
    if not designs:
        return
    store = DesignStore(os.path.join(output_dir, "design"))
    for role, cfg in designs.items():
        store.save(cfg, role)


def save_checkpoint(
    output_dir: str,
    *,
    boundary: str,
    state: Dict[str, Any],
    trees: Dict[str, List[Dict[str, Any]]],
    designs: Dict[str, Any],
) -> str:
    payload = {"boundary": boundary, "state": state, "trees": trees, "designs": designs}
    path = _ckpt_path(output_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if boundary == "outer":
        os.makedirs(os.path.join(output_dir, "checkpoints"), exist_ok=True)
        with open(_boundary_path(output_dir, "outer"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    return path


def load_checkpoint(output_dir: str, boundary: str = "latest") -> Optional[Dict[str, Any]]:
    path = _boundary_path(output_dir, boundary) if boundary != "latest" else _ckpt_path(output_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def capture_trees(task_tree: Any, planner_tree: Any, evaluator_tree: Any) -> Dict[str, List[Dict[str, Any]]]:
    stores = {"task": task_tree, "planner": planner_tree, "evaluator": evaluator_tree}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for name in _TREE_NAMES:
        st = stores[name]
        out[name] = [st.nodes[k].to_dict() for k in st.order]
    return out


def restore_trees(output_dir: str, trees: Dict[str, List[Dict[str, Any]]]) -> None:
    """Rewrite the per-tree JSONL event logs from a snapshot (add events only)."""
    for name in _TREE_NAMES:
        nodes = trees.get(name)
        if nodes is None:
            continue
        path = os.path.join(output_dir, f"{name}_tree.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for node in nodes:
                f.write(json.dumps({"op": "add", "node": node}, ensure_ascii=False) + "\n")
