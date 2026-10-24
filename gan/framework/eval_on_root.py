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
import time
from typing import Any, Callable, Dict, List, Optional

from gan.framework import paths


def eval_genid(outer: int, sample: int) -> str:
    """Isolated generation id for an evaluation round (never a tree node)."""
    return f"eval_outer{outer}_s{sample}"


def eval_run_dir(output_dir: str, outer: int, sample: int) -> str:
    """Isolated run dir (under work/), never a normal generation's dir."""
    return paths.work_dir(output_dir, eval_genid(outer, sample))


def run_eval_rounds(run_once: Callable[[int], Optional[float]],
                    samples: int,
                    timeout_s: Optional[float] = None,
                    on_error: Optional[Callable[[int, BaseException], None]] = None,
                    ) -> Dict[str, Any]:
    """Run ``samples`` independent rounds; aggregate mean/std/n_valid.

    ``run_once(sample)`` returns the round's benchmark score (or None on
    failure). A failed round shrinks ``n_valid`` but never aborts the batch.

    ``on_error(sample, exc)`` is called for a round that RAISED, so the caller can
    turn the failure into a durable event. Swallowing it silently made a broken
    evaluation indistinguishable from "no data": every fixed-root batch degraded
    to n_valid=0 with zero evidence of the cause.

    ``timeout_s`` (``eval.eval_timeout_s``) is a **batch** wall-clock budget,
    checked BETWEEN rounds: once spent, remaining rounds are skipped and
    ``timed_out`` is reported. A single in-flight round cannot be interrupted
    in-process, so one hung round still overruns the budget by its own runtime —
    the caller restores the main-line HEAD afterwards either way.
    """
    scores: List[float] = []
    timed_out = False
    t0 = time.monotonic()
    for s in range(1, int(samples) + 1):
        if timeout_s and timeout_s > 0 and (time.monotonic() - t0) >= timeout_s:
            timed_out = True
            break
        try:
            sc = run_once(s)
            if sc is not None:
                scores.append(float(sc))
        except Exception as e:  # noqa: BLE001 -- a failed round just shrinks n_valid
            if on_error is not None:
                try:
                    on_error(s, e)
                except Exception:  # noqa: BLE001 -- reporting never aborts a batch
                    pass
            continue
    if not scores:
        return {"mean": None, "std": None, "n_valid": 0, "scores": [],
                "timed_out": timed_out}
    return {
        "mean": float(statistics.mean(scores)),
        "std": float(statistics.pstdev(scores)) if len(scores) > 1 else 0.0,
        "n_valid": len(scores),
        "scores": scores,
        "timed_out": timed_out,
    }
