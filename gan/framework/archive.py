"""MOME archive for outer-level parent selection (docs/14 §3.2).

A derived index over ``ckpt/supernodes/*.json``: each supernode maps to a fixed
(X, Y, Z) cell and is kept in that cell's Pareto frontier over $(F_p, F_e)$,
with a per-cell capacity and a rolling reference window (the "season").

Bucket boundaries are FIXED (docs/14 §3.3), so a supernode's cell is stable
across the whole run; only ``reference_window`` / ``max_elites_per_cell`` are
tunable.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from gan.framework import paths

# X-axis: top-1 op -> fixed category (6 buckets).
OP_BUCKETS = {
    "set_prompt": 0,
    "set_config": 1,
    "set_param": 2,
    "select_component": 3,
    "deselect_component": 4,
}
_OP_OTHER = 5


def bucket_x(op_dist: Optional[Dict[str, Any]]) -> int:
    """top-1 op -> fixed category (6 buckets)."""
    if not op_dist:
        return _OP_OTHER
    top = max(op_dist.items(),
              key=lambda kv: (kv[1] if isinstance(kv[1], (int, float)) else 0))
    return OP_BUCKETS.get(str(top[0]), _OP_OTHER)


def bucket_y(issues_per_task: Optional[float]) -> int:
    """Average issue COUNT per task (no severity weighting) -> 5 fixed buckets."""
    if issues_per_task is None or issues_per_task <= 0:
        return 0
    if issues_per_task <= 0.5:
        return 1
    if issues_per_task <= 1.0:
        return 2
    if issues_per_task <= 2.0:
        return 3
    return 4


def bucket_z(eval_points_run: Optional[List[str]]) -> int:
    """number of covered eval points -> 3 buckets."""
    n = len(eval_points_run or [])
    if n <= 1:
        return 0
    if n == 2:
        return 1
    return 2


def cell_key(x: int, y: int, z: int) -> str:
    return f"x{x}|y{y}|z{z}"


def _dominates(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """``a`` dominates ``b`` when it is >= in both fitness dims and > in one."""
    pa, pe = a.get("fitness_p"), a.get("fitness_e")
    pb, peb = b.get("fitness_p"), b.get("fitness_e")
    if pa is None or pe is None or pb is None or peb is None:
        return False  # a missing fitness never dominates
    return pa >= pb and pe >= peb and (pa > pb or pe > peb)


def _pareto_prune(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for e in entries:
        if any(_dominates(k, e) for k in kept):
            continue
        kept = [k for k in kept if not _dominates(e, k)]
        kept.append(e)
    return kept


class MomeArchive:
    def __init__(self, output_dir: str, *, reference_window: int = 10,
                 max_elites_per_cell: int = 3):
        self.output_dir = os.path.abspath(output_dir)
        self.reference_window = max(1, int(reference_window))
        self.max_elites_per_cell = max(1, int(max_elites_per_cell))
        self.cells: Dict[str, Dict[str, Any]] = {}
        self.reference: List[int] = []
        self.load()

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        p = paths.archive_path(self.output_dir)
        if not os.path.isfile(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.cells = data.get("cells") or {}
        self.reference = [int(o) for o in (data.get("reference") or [])]

    def save(self) -> None:
        p = paths.archive_path(self.output_dir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "cells": self.cells, "reference": self.reference},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    # -- upsert -------------------------------------------------------------
    def upsert(self, entry: Dict[str, Any]) -> Optional[str]:
        """Insert one supernode entry; returns its cell key (or None if invalid).

        ``entry`` carries ``outer``, ``fitness_p``, ``fitness_e``,
        ``design_hash`` and either explicit ``x``/``y``/``z`` or the raw stats
        (``op_dist`` / ``issues_per_task`` / ``eval_points_run``) to bucket from.
        Re-upserting the same outer replaces its previous entry (idempotent).
        """
        outer = entry.get("outer")
        if outer is None:
            return None
        x, y, z = entry.get("x"), entry.get("y"), entry.get("z")
        if x is None or y is None or z is None:
            x = bucket_x(entry.get("op_dist"))
            y = bucket_y(entry.get("issues_per_task"))
            z = bucket_z(entry.get("eval_points_run"))
        key = cell_key(int(x), int(y), int(z))
        item = {
            "outer": int(outer),
            "fitness_p": entry.get("fitness_p"),
            "fitness_e": entry.get("fitness_e"),
            "design_hash": entry.get("design_hash"),
        }
        cell = self.cells.setdefault(key, {"elite": []})
        elite = [e for e in cell["elite"] if e.get("outer") != item["outer"]]
        elite.append(item)
        elite = _pareto_prune(elite)
        if len(elite) > self.max_elites_per_cell:
            elite.sort(
                key=lambda e: ((e.get("fitness_p") or 0.0) + (e.get("fitness_e") or 0.0)),
                reverse=True,
            )
            elite = elite[: self.max_elites_per_cell]
        cell["elite"] = elite
        # rolling reference window (insertion order)
        self.reference = [o for o in self.reference if o != int(outer)]
        self.reference.append(int(outer))
        if len(self.reference) > self.reference_window:
            self.reference = self.reference[-self.reference_window:]
        return key

    # -- queries ------------------------------------------------------------
    def nonempty_cells(self) -> List[str]:
        return [k for k, c in self.cells.items() if c.get("elite")]

    def elite_entries(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for k in self.nonempty_cells():
            for e in self.cells[k]["elite"]:
                out.append({**e, "cell": k})
        return out

    def rebuild(self, entries: List[Dict[str, Any]]) -> None:
        """Clear + replay ``entries`` (derived-index rebuild from supernodes)."""
        self.cells = {}
        self.reference = []
        for e in sorted(entries, key=lambda e: (e.get("outer") if e.get("outer") is not None else 0)):
            self.upsert(e)
