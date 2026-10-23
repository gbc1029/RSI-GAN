"""Fitness + novelty for outer parent selection (docs/14 §4).

Pure functions over supernode payloads (B1 shape): ``fitness_p`` is planner-
oriented (benchmark z-score vs the reference window), ``fitness_e`` is
evaluator-oriented (calibration error, negated) and never touches benchmark
level. ``novelty`` is the z-scored kNN distance to the reference window.

All z-scores are computed against the REFERENCE WINDOW only (not the whole
archive) so a season's baseline is stable; novelty is itself z-scored so
``lambda_novelty`` is commensurable with the benchmark z-score.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from gan.framework.archive import DEFAULT_X_OPS

_N_SCALARS = 5  # bench_mean, bench_std, calibration_error, issues_per_task, n_valid


def op_dist_vector(op_dist: Optional[Dict[str, Any]],
                   x_ops: Optional[Tuple[str, ...]] = None) -> List[float]:
    """Normalize ``op_dist`` to a fixed ``len(x_ops)+1`` probability vector."""
    table = x_ops if x_ops is not None else DEFAULT_X_OPS
    vec = [0.0] * (len(table) + 1)
    if not op_dist:
        return vec
    total = 0.0
    for op, cnt in op_dist.items():
        try:
            b = table.index(str(op))
        except ValueError:
            b = len(table)
        c = float(cnt) if isinstance(cnt, (int, float)) else 0.0
        vec[b] += c
        total += c
    if total > 0:
        vec = [v / total for v in vec]
    return vec


def scalar_vector(sn: Dict[str, Any]) -> List[float]:
    """The scalar block of the feature vector (docs/14 §4.1)."""
    bench = sn.get("benchmark") or {}
    ev = sn.get("evaluator_stats") or {}
    vals = [
        bench.get("mean"),
        bench.get("std"),
        ev.get("calibration_error"),
        ev.get("issues_per_task"),
        bench.get("n_valid"),
    ]
    return [float(v) if isinstance(v, (int, float)) else 0.0 for v in vals]


def z_ref(x: float, refs: List[float]) -> float:
    """z-score of ``x`` relative to ``refs`` (0 when the reference is degenerate)."""
    if len(refs) < 2:
        return 0.0
    mu = statistics.mean(refs)
    sd = statistics.pstdev(refs)
    if sd == 0.0:
        return 0.0
    return (x - mu) / sd


def _ruzicka(a: List[float], b: List[float]) -> float:
    num = sum(abs(x - y) for x, y in zip(a, b))
    den = sum(max(x, y) for x, y in zip(a, b))
    return num / den if den > 0 else 0.0


def _scalar_zstats(refs: List[Dict[str, Any]]) -> Tuple[List[float], List[float]]:
    if not refs:
        return [0.0] * _N_SCALARS, [1.0] * _N_SCALARS
    cols = list(zip(*[scalar_vector(r) for r in refs]))
    mu = [statistics.mean(c) for c in cols]
    sd = [statistics.pstdev(c) if len(c) > 1 else 1.0 for c in cols]
    return mu, sd


def feature_distance(sn: Dict[str, Any], other: Dict[str, Any],
                     scalar_mu: List[float], scalar_sd: List[float],
                     x_ops: Optional[Tuple[str, ...]] = None) -> float:
    """Weighted distance: op-dist Ruzicka + z-scored scalar block."""
    d_op = _ruzicka(
        op_dist_vector((sn.get("planner_stats") or {}).get("op_dist"), x_ops),
        op_dist_vector((other.get("planner_stats") or {}).get("op_dist"), x_ops),
    )
    sv, ov = scalar_vector(sn), scalar_vector(other)
    zs = [0.0 if sd == 0 else (x - mu) / sd
          for x, mu, sd in zip(sv, scalar_mu, scalar_sd)]
    zo = [0.0 if sd == 0 else (x - mu) / sd
          for x, mu, sd in zip(ov, scalar_mu, scalar_sd)]
    d_scalar = sum(abs(a - b) for a, b in zip(zs, zo)) / len(zs)
    return d_op + d_scalar


def _knn_dist(sn: Dict[str, Any], pool: List[Dict[str, Any]],
              mu: List[float], sd: List[float], k: int,
              x_ops: Optional[Tuple[str, ...]] = None) -> float:
    if not pool:
        return 0.0
    ds = sorted(feature_distance(sn, r, mu, sd, x_ops) for r in pool)
    top = ds[: min(k, len(ds))]
    return sum(top) / len(top)


def novelty(sn: Dict[str, Any], refs: List[Dict[str, Any]], k: int = 5,
            x_ops: Optional[Tuple[str, ...]] = None) -> float:
    """kNN distance to the reference window, then z-scored over that window."""
    if len(refs) < 2:
        return 0.0
    mu, sd = _scalar_zstats(refs)
    raw_s = _knn_dist(sn, refs, mu, sd, k, x_ops)
    refs_raw = [_knn_dist(r, [o for o in refs if o is not r], mu, sd, k, x_ops)
                for r in refs]
    rmu = statistics.mean(refs_raw)
    rsd = statistics.pstdev(refs_raw)
    if rsd == 0.0:
        return 0.0
    return (raw_s - rmu) / rsd


def _ref_scalars(refs: List[Dict[str, Any]], path: Tuple[str, ...],
                 key: str) -> List[float]:
    out: List[float] = []
    for r in refs:
        d: Any = r
        try:
            for part in path:
                d = d.get(part) or {}
            v = d.get(key)
        except (AttributeError, TypeError):
            v = None
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def fitness_p(sn: Dict[str, Any], refs: List[Dict[str, Any]],
              beta: float = 0.1, lambda_: float = 0.2, novelty_k: int = 5,
              x_ops: Optional[Tuple[str, ...]] = None) -> Optional[float]:
    """Planner fitness: benchmark mean minus variance, plus novelty (docs/14 §4.3)."""
    bench = sn.get("benchmark") or {}
    mean = bench.get("mean")
    if mean is None:
        return None
    std = bench.get("std")
    refs_mean = _ref_scalars(refs, ("benchmark",), "mean")
    refs_std = _ref_scalars(refs, ("benchmark",), "std")
    z_mean = z_ref(float(mean), refs_mean)
    z_std = z_ref(float(std) if isinstance(std, (int, float)) else 0.0, refs_std)
    return z_mean - beta * z_std + lambda_ * novelty(sn, refs, novelty_k, x_ops)


def fitness_e(sn: Dict[str, Any], refs: List[Dict[str, Any]],
              w1: float = 0.6, w2: float = 0.4, lambda_: float = 0.2,
              novelty_k: int = 5,
              x_ops: Optional[Tuple[str, ...]] = None) -> float:
    """Evaluator fitness: negated calibration error + verdict stability + novelty.

    Never touches benchmark level (docs/14 §3). Both scalars are z-scored
    against the reference window (verdict_stability was raw in the pre-2026-10
    revision, which silently shrank its effective weight to ``w2 * sigma_vs``).
    ``verdict_stability`` missing data stays neutral (term skipped, 0.0);
    ``fitness_partial`` is stamped by the caller.
    """
    ev = sn.get("evaluator_stats") or {}
    ce = ev.get("calibration_error")
    ce_val = float(ce) if isinstance(ce, (int, float)) else 0.0
    refs_ce = _ref_scalars(refs, ("evaluator_stats",), "calibration_error")
    z_ce = z_ref(ce_val, refs_ce)
    vs = ev.get("verdict_stability")
    vs_val = float(vs) if isinstance(vs, (int, float)) else None
    # verdict_stability is a [0,1] hit-rate; z-score it against the reference
    # window so its contribution is commensurable with z_ce and novelty (the
    # pre-normalization raw fraction had an effective weight of w2 * sigma_vs,
    # i.e. a fraction of the nominal w2). Missing data stays NEUTRAL: the term
    # is skipped (0.0), never scored as a below-average hit-rate.
    z_vs = (z_ref(vs_val, _ref_scalars(refs, ("evaluator_stats",),
                                       "verdict_stability"))
            if vs_val is not None else 0.0)
    return -w1 * z_ce + w2 * z_vs + lambda_ * novelty(sn, refs, novelty_k, x_ops)
