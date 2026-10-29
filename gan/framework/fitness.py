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

# Which ``benchmark`` field carries the LEVEL compared across supernodes, in
# priority order. ``val_mean`` is the fixed-root evaluation's score: it is
# measured on the run-persistent EVALUATION anchor set (a held-out val split,
# identical for every supernode), so a high value cannot come from having been
# lucky with the generation's question draw. ``mean`` is the main-line train
# score whose question set changes per generation -- kept as the fallback so
# supernodes recorded before the val evaluation existed stay scoreable.
_LEVEL_KEYS = ("val_mean", "mean")
_LEVEL_STD_KEYS = {"val_mean": "val_std", "mean": "std"}


def _level_key(bench: Dict[str, Any]) -> str:
    """The benchmark field this supernode's fitness level is read from."""
    for k in _LEVEL_KEYS:
        if isinstance(bench.get(k), (int, float)):
            return k
    return _LEVEL_KEYS[-1]


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _window_key(refs: List[Dict[str, Any]]) -> Optional[str]:
    """Which level the reference WINDOW is measured in (None = empty window)."""
    n_val = sum(1 for r in refs if _is_num((r.get("benchmark") or {}).get("val_mean")))
    n_mean = sum(1 for r in refs if _is_num((r.get("benchmark") or {}).get("mean")))
    if n_val == 0 and n_mean == 0:
        return None
    # A tie prefers the val regime: it is the newer, held-out measurement.
    return "val_mean" if n_val * 2 >= (n_val + n_mean) else "mean"


def scale_conflict(sn: Dict[str, Any],
                   refs: List[Dict[str, Any]]) -> Optional[str]:
    """Why ``sn`` cannot be RANKED against ``refs`` (``None`` = it can).

    ``fitness_p`` is a comparison, so the candidate and the reference window must
    be measured with the same yardstick. A supernode whose fixed-root evaluation
    failed or was skipped has only ``benchmark.mean`` -- the main-line train
    score, whose question set is re-drawn every generation -- while a val-measured
    window holds ``benchmark.val_mean`` on a fixed held-out set. Z-scoring one
    against the other is the "mixed baseline" defect of docs/14 §10, so the entry
    is refused instead of quietly polluting the pool.

    A window that is entirely mean-based (e.g. an older run) is self-consistent
    and stays scoreable, and so does an empty/cold-start window.
    """
    key = _level_key(sn.get("benchmark") or {})
    win = _window_key(refs)
    if win is None or key == win:
        return None
    return (f"candidate level={key!r} but the reference window is {win!r} "
            f"({len(refs)} refs): refusing to mix measurement scales")


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


def scalar_vector(sn: Dict[str, Any],
                  level_key: Optional[str] = None) -> List[float]:
    """The scalar block of the feature vector (docs/14 §4.1).

    ``level_key`` pins WHICH benchmark field the level/spread columns read and
    must be passed whenever several supernodes are compared: choosing it per
    entry (the default) would put ``val_mean`` and ``mean`` in the SAME z-scored
    column as soon as one run contains both.
    """
    bench = sn.get("benchmark") or {}
    ev = sn.get("evaluator_stats") or {}
    key = level_key or _level_key(bench)
    vals = [
        bench.get(key),
        bench.get(_LEVEL_STD_KEYS.get(key, "std")),
        ev.get("calibration_error"),
        ev.get("issues_per_task"),
        bench.get("n_valid"),
    ]
    return [float(v) if _is_num(v) else 0.0 for v in vals]


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


def _scalar_zstats(refs: List[Dict[str, Any]],
                   level_key: Optional[str] = None) -> Tuple[List[float], List[float]]:
    if not refs:
        return [0.0] * _N_SCALARS, [1.0] * _N_SCALARS
    cols = list(zip(*[scalar_vector(r, level_key) for r in refs]))
    mu = [statistics.mean(c) for c in cols]
    sd = [statistics.pstdev(c) if len(c) > 1 else 1.0 for c in cols]
    return mu, sd


def feature_distance(sn: Dict[str, Any], other: Dict[str, Any],
                     scalar_mu: List[float], scalar_sd: List[float],
                     x_ops: Optional[Tuple[str, ...]] = None,
                     level_key: Optional[str] = None) -> float:
    """Weighted distance: op-dist Ruzicka + z-scored scalar block."""
    d_op = _ruzicka(
        op_dist_vector((sn.get("planner_stats") or {}).get("op_dist"), x_ops),
        op_dist_vector((other.get("planner_stats") or {}).get("op_dist"), x_ops),
    )
    sv, ov = scalar_vector(sn, level_key), scalar_vector(other, level_key)
    zs = [0.0 if sd == 0 else (x - mu) / sd
          for x, mu, sd in zip(sv, scalar_mu, scalar_sd)]
    zo = [0.0 if sd == 0 else (x - mu) / sd
          for x, mu, sd in zip(ov, scalar_mu, scalar_sd)]
    d_scalar = sum(abs(a - b) for a, b in zip(zs, zo)) / len(zs)
    return d_op + d_scalar


def _knn_dist(sn: Dict[str, Any], pool: List[Dict[str, Any]],
              mu: List[float], sd: List[float], k: int,
              x_ops: Optional[Tuple[str, ...]] = None,
              level_key: Optional[str] = None) -> float:
    if not pool:
        return 0.0
    ds = sorted(feature_distance(sn, r, mu, sd, x_ops, level_key) for r in pool)
    top = ds[: min(k, len(ds))]
    return sum(top) / len(top)


def novelty(sn: Dict[str, Any], refs: List[Dict[str, Any]], k: int = 5,
            x_ops: Optional[Tuple[str, ...]] = None,
            level_key: Optional[str] = None) -> float:
    """kNN distance to the reference window, then z-scored over that window."""
    if len(refs) < 2:
        return 0.0
    mu, sd = _scalar_zstats(refs, level_key)
    raw_s = _knn_dist(sn, refs, mu, sd, k, x_ops, level_key)
    refs_raw = [_knn_dist(r, [o for o in refs if o is not r], mu, sd, k, x_ops, level_key)
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
    """Planner fitness: benchmark mean minus variance, plus novelty (docs/14 §4.3).

    Reads the LEVEL from ``benchmark.val_mean`` when the fixed-root evaluation
    produced one, and only falls back to the main-line ``benchmark.mean`` for
    supernodes recorded without it (older runs). The reference window is then
    read from the SAME key, so candidate and reference stay in one unit --
    z-scoring a held-out val score against train means would be meaningless.
    """
    bench = sn.get("benchmark") or {}
    level_key = _level_key(bench)
    mean = bench.get(level_key)
    if mean is None:
        return None
    # Refuse to rank a main-line-measured entry against a val-measured window
    # (and vice versa): that is the mixed-baseline defect, not a comparison.
    if scale_conflict(sn, refs) is not None:
        return None
    std_key = _LEVEL_STD_KEYS[level_key]
    std = bench.get(std_key)
    refs_mean = _ref_scalars(refs, ("benchmark",), level_key)
    refs_std = _ref_scalars(refs, ("benchmark",), std_key)
    z_mean = z_ref(float(mean), refs_mean)
    z_std = z_ref(float(std) if isinstance(std, (int, float)) else 0.0, refs_std)
    return z_mean - beta * z_std + lambda_ * novelty(sn, refs, novelty_k, x_ops,
                                                     level_key=level_key)


def fitness_e(sn: Dict[str, Any], refs: List[Dict[str, Any]],
              w1: float = 0.6, w2: float = 0.4, lambda_: float = 0.2,
              novelty_k: int = 5,
              x_ops: Optional[Tuple[str, ...]] = None) -> float:
    """Evaluator fitness: negated calibration error + verdict stability + novelty.

    Never touches benchmark level (docs/14 §3). Both scalars are z-scored
    against the reference window (verdict_stability was raw in the pre-2026-10
    revision, which silently shrank its effective weight to ``w2 * sigma_vs``).
    MISSING data stays neutral for BOTH scalars (term skipped, 0.0): a missing
    calibration_error used to be read as 0.0 == perfect calibration, i.e. a
    spurious reward (relevant for arms like A2 where the evaluator never runs).

    ``verdict_stability`` DEFINITION CHANGED (2026-10, second rewrite): it is now
    the PER-ISSUE hit-rate produced by the mechanical checkers in
    ``gan/framework/issue_checks.py`` -- "did the defect this issue named
    actually go from present (parent) to absent (child), and did the evaluator
    say so?" -- accumulated by ``loop._update_issue_ledger``.

    The previous definition compared one outer's whole verdict vector against a
    single shared bit ("did the mean score rise?"). That scored an honest
    evaluator 0.0 on A1 (planner disabled: nothing can be fixed, so 19/20
    "not fixed" claims were correct, but the score rose anyway) and asked the
    evaluator to be right about a claim it never made. Old supernodes keep their
    old value -- it cannot be recomputed -- and are told apart offline by
    ``evaluator_stats.verdict_stability_basis``; ``None`` (nothing mechanically
    verifiable this outer) remains neutral here, exactly like a missing
    calibration_error.
    """
    ev = sn.get("evaluator_stats") or {}
    ce = ev.get("calibration_error")
    ce_val = float(ce) if isinstance(ce, (int, float)) else None
    refs_ce = _ref_scalars(refs, ("evaluator_stats",), "calibration_error")
    z_ce = z_ref(ce_val, refs_ce) if ce_val is not None else 0.0
    vs = ev.get("verdict_stability")
    vs_val = float(vs) if isinstance(vs, (int, float)) else None
    # verdict_stability is a [0,1] per-issue hit-rate (mechanical truth from
    # issue_checks.py since the 2026-10 rewrite); z-score it against the
    # reference window so its contribution is commensurable with z_ce and
    # novelty (the pre-normalization raw fraction had an effective weight of
    # w2 * sigma_vs, i.e. a fraction of the nominal w2). Missing data stays
    # NEUTRAL: the term is skipped (0.0), never scored as a below-average
    # hit-rate -- in particular an outer with nothing mechanically verifiable
    # is not penalised for it.
    z_vs = (z_ref(vs_val, _ref_scalars(refs, ("evaluator_stats",),
                                       "verdict_stability"))
            if vs_val is not None else 0.0)
    # F_e never compares benchmark LEVELS, but novelty() shares the scalar feature
    # vector, so its benchmark columns must still come from ONE field -- pin them
    # to this candidate's level (every supernode has ``mean``, so the column is
    # always well defined).
    return (-w1 * z_ce + w2 * z_vs
            + lambda_ * novelty(sn, refs, novelty_k, x_ops,
                                level_key=_level_key(sn.get("benchmark") or {})))
