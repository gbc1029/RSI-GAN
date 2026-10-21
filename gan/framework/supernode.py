"""Per-outer supernode record (docs/13 §2.3).

One immutable JSON per outer at ``ckpt/supernodes/outer_<N>.json`` aggregating
the outer's design hashes, code commits, subtree, per-question benchmark
scores and role statistics. Pure data sink (no LLM, no git): the loop
assembles the payload and this module persists it atomically.

The canonical name is written once (skip-if-exists); a re-run of the same
outer writes an archival copy ``outer_<N>_<ms>.json`` — mirroring
``checkpoint.py`` so no supernode snapshot is ever overwritten.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

from gan.framework import paths

_OUTER_RE = re.compile(r"^outer_(\d+)\.json$")
_OUTER_ARCH_RE = re.compile(r"^outer_(\d+)_(\d+)\.json$")


def _atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def record_supernode(output_dir: str, outer: int, payload: Dict[str, Any],
                     writer: Any = None) -> str:
    """Persist an immutable per-outer supernode record; returns the path."""
    payload = dict(payload)
    payload.setdefault("outer", outer)
    payload["recorded_ts"] = time.time()
    target = paths.supernode_path(output_dir, outer)
    if os.path.exists(target):
        # canonical snapshot is immutable: archive this re-run's snapshot
        target = os.path.join(paths.supernodes_dir(output_dir),
                              f"outer_{outer}_{int(time.time() * 1000)}.json")
    _atomic_write_json(target, payload)
    return target


def update_supernode(output_dir: str, outer: int, payload: Dict[str, Any],
                     writer: Any = None) -> str:
    """Atomically OVERWRITE the canonical supernode record (in-place update).

    Used by the fixed-root evaluation to stamp the benchmark estimate back onto
    the just-recorded supernode — unlike ``record_supernode``, which never
    overwrites a canonical file.
    """
    payload = dict(payload)
    payload.setdefault("outer", outer)
    target = paths.supernode_path(output_dir, outer)
    _atomic_write_json(target, payload)
    return target


def load_supernode(output_dir: str, outer: int) -> Optional[Dict[str, Any]]:
    """Read the canonical supernode record for ``outer`` (None if absent)."""
    p = paths.supernode_path(output_dir, outer)
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)
