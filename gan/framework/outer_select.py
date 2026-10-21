"""Outer-level parent selection (docs/14 §5.2, §5.6).

Two-level sampling tournament over the MOME archive, followed by a
restore→heal→amplitude-check loop: the selected supernode's designs are written
back, healed against the current roles HEAD, and rejected (rolled back) when
healing strips too many of the parent's components — with a bounded retry and a
chain-fallback (return None) when nothing applies.
"""
from __future__ import annotations

import random
from copy import deepcopy
from typing import Any, Dict, List, Optional

from gan.framework import paths
from gan.framework.archive import MomeArchive
from gan.framework.supernode import load_supernode


def select_parent(archive: MomeArchive, rng: random.Random,
                  tournament_k: int = 3) -> Optional[Dict[str, Any]]:
    """Two-level sampling tournament (docs/14 §5.2).

    Level 1 draws a uniformly-random non-empty cell; level 2 draws a random
    elite from that cell's Pareto frontier. The highest $F_p$ (tie-break $F_e$)
    over ``tournament_k`` draws wins. Returns the entry (with ``cell``) or None.
    """
    cells = archive.nonempty_cells()
    if not cells:
        return None
    picked = []
    for _ in range(int(tournament_k)):
        ck = rng.choice(cells)
        elite = archive.cells[ck]["elite"]
        picked.append({**rng.choice(elite), "cell": ck})
    return max(picked, key=lambda e: (
        e.get("fitness_p") if e.get("fitness_p") is not None else float("-inf"),
        e.get("fitness_e") if e.get("fitness_e") is not None else float("-inf"),
    ))


def restore_role_design(output_dir: str, role: str, cfg: Optional[Dict[str, Any]],
                        roles_root: Optional[str], writer: Any,
                        tool_overlap_threshold: float = 0.5) -> Optional[Dict[str, Any]]:
    """Restore + heal one role's design (docs/14 §5.6); returns a report or None.

    The report carries ``stripped`` (heal removals), ``tool_overlap`` (Jaccard
    of the parent's component list surviving the heal) and ``too_divergent``.
    The restored+healed design is persisted (ledger-guarded) BEFORE the verdict,
    so the caller must roll back on ``too_divergent`` (see ``select_and_restore``).
    """
    if not isinstance(cfg, dict):
        return None
    from gan.design.store import DesignStore
    from gan.framework import task_execution as tx

    store = DesignStore(paths.design_root(output_dir), writer=writer)
    cfg = deepcopy(cfg)
    store.save(cfg, role)  # ledger-guarded; raises -> caller rolls back
    before_tools = list(cfg.get("tools") or [])
    stripped = (tx.heal_design_keys(cfg, role, roles_root)
                + tx.heal_design_slots(cfg, role, roles_root))
    if stripped:
        store.save(cfg, role)  # persist the healed design
    after_tools = list(cfg.get("tools") or [])
    if before_tools:
        overlap = len(set(before_tools) & set(after_tools)) / len(before_tools)
    else:
        overlap = 1.0
    too_divergent = bool(stripped) and overlap < tool_overlap_threshold
    return {"role": role, "stripped": stripped, "tool_overlap": overlap,
            "too_divergent": too_divergent}


def select_and_restore(output_dir: str, archive: MomeArchive,
                       roles_root: Optional[str], writer: Any,
                       rng: random.Random, tournament_k: int = 3,
                       max_tries: int = 3, tool_overlap_threshold: float = 0.5
                       ) -> tuple:
    """Pick a parent and restore its designs; returns (supernode, report).

    ``report`` = {"outer", "reports", "tries"}; the returned supernode is None
    on cold start / all-divergent (chain fallback). A divergent candidate is
    rolled back to the pre-attempt designs before the next try.
    """
    from gan.design.store import DesignStore

    store = DesignStore(paths.design_root(output_dir), writer=writer)
    backup: Dict[str, Optional[Dict[str, Any]]] = {}
    for role in ("planner", "evaluator"):
        try:
            backup[role] = store.load(role)
        except Exception:  # noqa: BLE001 -- advisory
            backup[role] = None

    def rollback() -> None:
        for role, cfg in backup.items():
            if cfg is not None:
                try:
                    store.save(cfg, role)
                except Exception:  # noqa: BLE001 -- advisory
                    pass

    tried: List[Dict[str, Any]] = []
    for _ in range(int(max_tries)):
        entry = select_parent(archive, rng, tournament_k)
        if entry is None:
            rollback()
            return None, {"outer": None, "reports": [], "tries": tried,
                          "reason": "no_candidate"}
        sn = load_supernode(output_dir, entry["outer"])
        designs = (sn or {}).get("designs")
        if not isinstance(designs, dict) or not isinstance(designs.get("planner"), dict):
            tried.append({"outer": entry["outer"], "reason": "no designs"})
            continue
        reports = []
        ok = True
        for role in ("planner", "evaluator"):
            rep = restore_role_design(output_dir, role, designs.get(role),
                                      roles_root, writer, tool_overlap_threshold)
            if rep is None:
                ok = False
                reports.append({"role": role, "reason": "no design"})
            else:
                reports.append(rep)
                if rep.get("too_divergent"):
                    ok = False
        if ok:
            return sn, {"outer": entry["outer"], "reports": reports, "tries": tried}
        tried.append({"outer": entry["outer"], "reason": "divergent", "reports": reports})
        rollback()
    rollback()
    return None, {"outer": None, "reports": [], "tries": tried,
                  "reason": "all_divergent"}
