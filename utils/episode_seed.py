"""Deterministic episode seeds for episode-based domains (balrog) -- ONE definition.

A balrog "question" is an EPISODE, identified by ``<task>#<seed>``: the task id
from ``domains/balrog/config/config.yaml`` plus the seed handed to
``env.reset(seed=...)``. Measured 2026-11-04 (both wired balrog domains, through
the full ``EnvWrapper -> domain wrapper -> gym/gymnasium`` chain): the same
``(task, seed)`` reproduces the same episode -- mission / descriptions / render
bytes / max_steps identical, and for babaisai the same grid. Seeding the GLOBAL
RNGs does nothing; ``reset(seed=...)`` is the only lever.

THREE LAYERS (the same design paper_review uses for questions):

* ``ANCHOR_KIND``  -- frozen for the whole run in ``ckpt/sample_seed.json``:
  paired child-vs-parent comparison and the evidence the LCB parent selector
  consumes (``loop._anchor_stats`` -> ``tree._select_parents_lcb``).
* ``ROTATE_KIND``  -- re-derived per generation from
  ``derive_sample_seed(base_seed, genid)``, so a generation cannot be tuned to
  one fixed episode set (the rotating half is what keeps the fixed half honest).
* ``EVAL_KIND``    -- frozen for the whole run, reserved for the fixed-root
  evaluation only (not wired for balrog yet; declared here so the recipe has a
  single home when it lands).

THE RECIPE IS A FROZEN CONTRACT: it is recorded in the seed file as
``EPISODE_RULE``. Changing ``derive_episode_seed`` invalidates every archived
run, so treat it as versioned.

This module is STDLIB-ONLY and lives in ``utils/`` because ``utils/`` is copied
into every sandboxed harness run: the parent (``gan.framework``) and the domain
harness (``domains.balrog``) must derive the same seeds from the same rules.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

EPISODE_SEED_MODULUS = 2 ** 31
ANCHOR_KIND = "anchor-ep"
ROTATE_KIND = "ep"
EVAL_KIND = "eval-ep"
EPISODE_ID_SEP = "#"
EPISODE_RULE = (
    "seed = int(sha256(f'{base}:{kind}:{task}:{index}')[:8]) % 2**31; "
    "anchor+eval use the RUN base seed, rotate uses derive_sample_seed(base, genid); "
    "id = '<task>#<seed>'"
)


def derive_episode_seed(base_seed: int, kind: str, task: str, index: int) -> int:
    """Deterministic, non-negative episode seed (pure function of its inputs)."""
    digest = hashlib.sha256(
        f"{int(base_seed)}:{kind}:{str(task)}:{int(index)}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % EPISODE_SEED_MODULUS


def episode_id(task: str, seed: int) -> str:
    """Stable episode id used in report.json and in ``anchor_ids``."""
    return f"{task}{EPISODE_ID_SEP}{int(seed)}"


def split_episode_id(raw: str) -> Tuple[str, int]:
    """``'<task>#<seed>'`` -> ``(task, seed)``; raises ValueError when malformed."""
    text = str(raw or "").strip()
    task, sep, seed = text.rpartition(EPISODE_ID_SEP)
    if not sep or not task or not seed:
        raise ValueError(f"not an episode id (expected '<task>#<seed>'): {raw!r}")
    try:
        value = int(seed)
    except ValueError as exc:
        raise ValueError(f"episode id seed is not an integer: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"episode id seed must be non-negative: {raw!r}")
    return task, value


def anchor_seeds_by_task(anchor_ids: Sequence[str]) -> Dict[str, List[int]]:
    """Group frozen anchor episode ids by task (order preserved).

    A malformed id is REFUSED rather than skipped: silently dropping it would
    shrink the anchor core while the run still looked healthy.
    """
    out: Dict[str, List[int]] = {}
    for raw in anchor_ids or []:
        task, seed = split_episode_id(raw)
        out.setdefault(task, []).append(seed)
    return out


def plan_episodes(task: str, num_episodes: int,
                  anchor_seeds: Optional[Sequence[int]] = None,
                  sample_seed: Optional[int] = None) -> List[Dict[str, object]]:
    """Episode slots for ONE task: anchors first, then rotating seeds.

    * slots ``0 .. min(a, n) - 1`` carry the frozen anchor seeds;
    * the remaining slots carry ``derive_episode_seed(sample_seed, ROTATE_KIND,
      task, idx)`` where ``idx`` is the EPISODE SLOT index (not the rotating
      counter), so the seed of a given slot never depends on how many anchors
      the plan happens to carry -- changing ``anchor_episodes_per_task`` between
      runs cannot silently move a rotating episode to another seed;
    * ``sample_seed is None`` (legacy run, no plan) -> ``seed=None`` and the
      caller keeps its historical random behaviour.
    """
    n = max(0, int(num_episodes or 0))
    anchors = [int(s) for s in (anchor_seeds or [])][:n]
    episodes: List[Dict[str, object]] = []
    for idx in range(n):
        if idx < len(anchors):
            seed: Optional[int] = anchors[idx]
            is_anchor = True
        elif sample_seed is None:
            seed, is_anchor = None, False
        else:
            seed = derive_episode_seed(int(sample_seed), ROTATE_KIND, task, idx)
            is_anchor = False
        episodes.append({
            "episode_idx": idx,
            "seed": seed,
            "anchor": is_anchor,
            "id": episode_id(task, seed) if seed is not None else None,
        })
    return episodes
