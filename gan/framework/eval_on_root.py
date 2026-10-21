"""Fixed-root evaluation for supernode scoring (docs/14 §5.5).

Pure orchestration + side-effect isolation helpers: ``run_eval_rounds`` runs
``eval_samples`` independent task rounds and aggregates mean/std/n_valid.  The
caller supplies ``run_once`` (one plan+task round) so this module never touches
the loop's planner / task tree / scores / role trajectories directly — the
isolation contract (no tree node, no scores row, no role-visible trajectory)
is therefore enforced by HOW the loop wires ``run_once``, and this module
guarantees the rounds share an isolated ``eval_*`` id/run-dir namespace.
"""
from __future__ import annotations

import statistics
from typing import Any, Callable, Dict, List, Optional

from gan.framework import paths


def eval_genid(outer: int, sample: int) -> str:
    """Isolated generation id for an evaluation round (never a tree node)."""
    return f"eval_outer{outer}_s{sample}"


def eval_run_dir(output_dir: str, outer: int, sample: int) -> str:
    """Isolated run dir (under work/), never a normal generation's dir."""
    return paths.work_dir(output_dir, eval_genid(outer, sample))


def run_eval_rounds(run_once: Callable[[int], Optional[float]],
                    samples: int) -> Dict[str, Any]:
    """Run ``samples`` independent rounds; aggregate mean/std/n_valid.

    ``run_once(sample)`` returns the round's benchmark score (or None on
    failure). A failed round shrinks ``n_valid`` but never aborts the batch.
    """
    scores: List[float] = []
    for s in range(1, int(samples) + 1):
        try:
            sc = run_once(s)
            if sc is not None:
                scores.append(float(sc))
        except Exception:  # noqa: BLE001 -- a failed round just shrinks n_valid
            continue
    if not scores:
        return {"mean": None, "std": None, "n_valid": 0, "scores": []}
    return {
        "mean": float(statistics.mean(scores)),
        "std": float(statistics.pstdev(scores)) if len(scores) > 1 else 0.0,
        "n_valid": len(scores),
        "scores": scores,
    }
