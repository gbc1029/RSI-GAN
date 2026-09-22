"""Outer-loop checkpointing (FRAMEWORK, frozen).

The task tree is event-sourced, but planner/evaluator are refreshed per outer and
loop scalars live only in memory. This module persists a full snapshot so a run can
be resumed or rolled back to an outer-generation boundary.

Snapshot contents:
- ``state``  : loop scalars (counters, digests, last feedback, position)
- ``trees``  : ``{name: [node_dict, ...]}`` for task/planner/evaluator
- ``designs``: ``{role: config}`` snapshots for planner/evaluator

Layout:
- ``ckpt/checkpoint.json``             : the latest snapshot (overwritten, atomic)
- ``ckpt/outer_<N>.json``              : immutable per-outer snapshots (never overwritten)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from gan.design.store import DesignStore
from gan.framework import paths

_TREE_NAMES = ("task", "planner", "evaluator")
_OUTER_RE = re.compile(r"^outer_(\d+)\.json$")            # canonical (first write)
_OUTER_ARCH_RE = re.compile(r"^outer_(\d+)_(\d+)\.json$")  # archival re-run snapshot (F5)


def _ckpt_path(output_dir: str) -> str:
    return paths.checkpoint_latest(output_dir)


def _checkpoints_dir(output_dir: str) -> str:
    return paths.ckpt_dir(output_dir)


def _outer_path(output_dir: str, index: int) -> str:
    return paths.checkpoint_outer(output_dir, index)


def _boundary_path(output_dir: str, boundary: str) -> str:
    return os.path.join(_checkpoints_dir(output_dir), f"{boundary}.json")


def _atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _outer_snapshots(output_dir: str) -> List[Dict[str, Any]]:
    """Every outer snapshot (canonical + archival re-run copies), orderable by time.

    Each entry: ``{"path", "index", "ts"}`` where ``ts`` is the payload's
    ``completed_ts`` when present (new format), else the file mtime (legacy).
    Discovery covers BOTH name patterns so a re-run's snapshot is reachable.
    """
    d = _checkpoints_dir(output_dir)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        m = _OUTER_RE.match(name)
        a = None if m else _OUTER_ARCH_RE.match(name)
        if not m and not a:
            continue
        path = os.path.join(d, name)
        index = int((m or a).group(1))
        ts = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                ts = json.load(f).get("completed_ts")
        except Exception:
            ts = None
        if ts is None:
            try:
                ts = os.path.getmtime(path)
            except OSError:
                ts = 0.0
        out.append({"path": path, "index": index, "ts": float(ts)})
    return out


def newest_outer_snapshot(output_dir: str) -> Optional[Dict[str, Any]]:
    """The chronologically newest outer snapshot (canonical or archival).

    Ordering is by ``completed_ts``/mtime — NOT by outer index: a re-run that
    starts from the middle produces a NEWER snapshot with a SMALLER index, and
    the resume lineage must follow the newest attempt (start = its index + 1),
    not the tail of the previous run.
    """
    snaps = _outer_snapshots(output_dir)
    return max(snaps, key=lambda s: s["ts"]) if snaps else None


def _max_outer_index(output_dir: str) -> Optional[int]:
    snaps = _outer_snapshots(output_dir)
    return max((s["index"] for s in snaps), default=None)


def list_outer_checkpoints(output_dir: str) -> List[int]:
    return sorted({s["index"] for s in _outer_snapshots(output_dir)})


def snapshot_designs(output_dir: str, roles: List[str]) -> Dict[str, Any]:
    """Snapshot role designs, only for roles that already have a design file."""
    store = DesignStore(paths.design_root(output_dir))
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
    store = DesignStore(paths.design_root(output_dir))
    for role, cfg in designs.items():
        store.save(cfg, role)


def save_checkpoint(
    output_dir: str,
    *,
    boundary: str,
    state: Dict[str, Any],
    trees: Dict[str, List[Dict[str, Any]]],
    designs: Dict[str, Any],
    index: Optional[int] = None,
    code: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist a checkpoint.

    ``checkpoint.json`` is always the *latest* snapshot (overwritten). Outer
    boundaries additionally write an **immutable, numbered** file
    ``ckpt/outer_<index>.json`` so per-generation history is never lost:
    the canonical name is written once (skip-if-exists); a RE-RUN of the same
    outer writes an archival copy ``outer_<index>_<ms>.json`` instead, so no
    snapshot is ever overwritten (F5).

    ``code`` (optional, G2-lite) records the code baseline the state was
    produced against — ``{"commit": <code_root HEAD>}`` — so a resume can
    verify/restore the code tree to the checkpointed state.
    """
    payload = {"boundary": boundary, "state": state, "trees": trees, "designs": designs}
    if code:
        payload["code"] = dict(code)
    path = _ckpt_path(output_dir)
    _atomic_write_json(path, payload)
    if boundary == "outer":
        if index is None:
            nxt = _max_outer_index(output_dir)
            index = (nxt or 0) + 1
        payload = dict(payload)
        payload["index"] = index
        payload["completed_ts"] = time.time()
        target = _outer_path(output_dir, index)
        if os.path.exists(target):
            # canonical snapshot is immutable: archive this re-run's snapshot instead
            target = os.path.join(
                _checkpoints_dir(output_dir), f"outer_{index}_{int(time.time() * 1000)}.json"
            )
        _atomic_write_json(target, payload)
    return path


def load_checkpoint(output_dir: str, boundary: str = "latest") -> Optional[Dict[str, Any]]:
    if boundary == "latest":
        path = _ckpt_path(output_dir)
    elif boundary == "outer":
        newest = newest_outer_snapshot(output_dir)
        if newest is not None:
            path = newest["path"]
        else:
            # legacy fallback: pre-numbering runs wrote checkpoints/outer.json
            path = _boundary_path(output_dir, "outer")
    else:
        path = _boundary_path(output_dir, boundary)
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
    """Revert trees to a snapshot WITHOUT truncating the append-only log.

    An ``op=reset`` event is appended; ``TreeStore`` replays it to rebuild the
    node set. The event history prior to the reset is preserved for audit.
    """
    for name in _TREE_NAMES:
        nodes = trees.get(name)
        if nodes is None:
            continue
        path = paths.tree_path(output_dir, name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"op": "reset", "nodes": nodes}, ensure_ascii=False) + "\n")
