"""MOME archive for outer-level parent selection (docs/14 §3.2).

A derived index over ``ckpt/supernodes/*.json``: each supernode maps to a fixed
(X, Y, Z) cell and is kept in that cell's Pareto frontier over $(F_p, F_e)$,
with a per-cell capacity and a rolling reference window (the "season").

Bucket boundaries default to the FIXED palette of docs/14 §3.3 (so a supernode's
cell is stable across a whole run), but they are CONFIG-DRIVEN
(``mome.x_buckets`` / ``y_buckets`` / ``z_buckets``): an **int** asserts the
declared count of the built-in boundaries (a mismatch fails loud instead of
silently doing nothing), a **list** supplies explicit boundaries — that is the
ablation knob (coarser/finer cells).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from gan.framework import paths

# X-axis: top-1 op -> fixed category (6 buckets: 5 named ops + "other").
# Ordered: index == bucket id; the tail bucket is "other".
DEFAULT_X_OPS: Tuple[str, ...] = (
    "set_prompt",
    "set_config",
    "set_param",
    "select_component",
    "deselect_component",
)
# Back-compat alias (fitness.op_dist_vector uses it).
OP_BUCKETS = {op: i for i, op in enumerate(DEFAULT_X_OPS)}
_OP_OTHER = len(DEFAULT_X_OPS)

# Y-axis: average issue count/task -> 5 buckets (edges are INCLUSIVE uppers).
DEFAULT_Y_EDGES: Tuple[float, ...] = (0.0, 0.5, 1.0, 2.0)
# Z-axis: number of covered eval points -> 3 buckets.
DEFAULT_Z_EDGES: Tuple[float, ...] = (1.0, 2.0)


def _bucket_by_edges(value: float, edges: Tuple[float, ...]) -> int:
    """First edge >= value wins; ``len(edges) + 1`` buckets in total."""
    for i, e in enumerate(edges):
        if value <= e:
            return i
    return len(edges)


def _count_mismatch(*, key: str, declared: int, built_in: int, hint: str) -> ValueError:
    return ValueError(
        f"mome.{key}={declared} 与内置边界给出的 {built_in} 个桶不一致；"
        f"改桶数请改用 list 形式（{hint}）。"
    )


def resolve_buckets(x: Any = None, y: Any = None, z: Any = None
                    ) -> Tuple[Tuple[str, ...], Tuple[float, ...], Tuple[float, ...]]:
    """Normalize the three ``mome.*_buckets`` config values.

    ``None`` -> built-in palette. ``int`` -> assertion on the built-in count
    (fails loud on mismatch). ``list``/``tuple`` -> explicit boundaries:
    op names for X, inclusive upper edges for Y/Z.
    """
    x_ops = DEFAULT_X_OPS
    if x is not None:
        if isinstance(x, int) and not isinstance(x, bool):
            if int(x) != len(DEFAULT_X_OPS) + 1:
                raise _count_mismatch(key="x_buckets", declared=int(x),
                                      built_in=len(DEFAULT_X_OPS) + 1,
                                      hint="x_buckets: [set_prompt, set_config, ...]")
        else:
            x_ops = tuple(str(v) for v in x)

    y_edges = DEFAULT_Y_EDGES
    if y is not None:
        if isinstance(y, int) and not isinstance(y, bool):
            if int(y) != len(DEFAULT_Y_EDGES) + 1:
                raise _count_mismatch(key="y_buckets", declared=int(y),
                                      built_in=len(DEFAULT_Y_EDGES) + 1,
                                      hint="y_buckets: [0.0, 0.5, 1.0, 2.0]")
        else:
            y_edges = tuple(float(v) for v in y)

    z_edges = DEFAULT_Z_EDGES
    if z is not None:
        if isinstance(z, int) and not isinstance(z, bool):
            if int(z) != len(DEFAULT_Z_EDGES) + 1:
                raise _count_mismatch(key="z_buckets", declared=int(z),
                                      built_in=len(DEFAULT_Z_EDGES) + 1,
                                      hint="z_buckets: [1.0, 2.0]")
        else:
            z_edges = tuple(float(v) for v in z)
    return x_ops, y_edges, z_edges


def bucket_x(op_dist: Optional[Dict[str, Any]], ops: Optional[Tuple[str, ...]] = None) -> int:
    """top-1 op -> category id (``ops`` order); unknown ops land in "other"."""
    table = ops if ops is not None else DEFAULT_X_OPS
    if not op_dist:
        return len(table)
    top = max(op_dist.items(),
              key=lambda kv: (kv[1] if isinstance(kv[1], (int, float)) else 0))
    try:
        return table.index(str(top[0]))
    except ValueError:
        return len(table)


def bucket_y(issues_per_task: Optional[float],
             edges: Optional[Tuple[float, ...]] = None) -> int:
    """Average issue COUNT per task (no severity weighting) -> edge buckets."""
    if issues_per_task is None:
        return 0
    return _bucket_by_edges(float(issues_per_task),
                            edges if edges is not None else DEFAULT_Y_EDGES)


def bucket_z(eval_points_run: Optional[List[str]],
             edges: Optional[Tuple[float, ...]] = None) -> int:
    """number of covered eval points -> edge buckets."""
    return _bucket_by_edges(float(len(eval_points_run or [])),
                            edges if edges is not None else DEFAULT_Z_EDGES)


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
                 max_elites_per_cell: int = 3,
                 x_ops: Any = None, y_edges: Any = None, z_edges: Any = None):
        self.output_dir = os.path.abspath(output_dir)
        self.reference_window = max(1, int(reference_window))
        self.max_elites_per_cell = max(1, int(max_elites_per_cell))
        # mome.{x,y,z}_buckets (int = count assertion, list = explicit).
        self.x_ops, self.y_edges, self.z_edges = resolve_buckets(
            x_ops, y_edges, z_edges)
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
            x = bucket_x(entry.get("op_dist"), self.x_ops)
            y = bucket_y(entry.get("issues_per_task"), self.y_edges)
            z = bucket_z(entry.get("eval_points_run"), self.z_edges)
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
