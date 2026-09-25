"""Append-only score index (FRAMEWORK, frozen).

The authoritative score lives in the per-generation ``report.json``; this index
is a small, fast lookup table for selection/plots/audit. Rows are appended, never
rewritten. ``report_sha`` links an index row back to its evidence artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from gan.framework import paths


def _path(output_dir: str) -> str:
    return paths.scores_path(output_dir)


def sha256_file(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def append_score(output_dir: str, record: Dict[str, Any]) -> None:
    p = _path(output_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = dict(record)
    row.setdefault("ts", time.time())
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_scores(output_dir: str) -> List[Dict[str, Any]]:
    p = _path(output_dir)
    if not os.path.exists(p):
        return []
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # A-level sweep: a dropped row can skew stagnation stats --
                    # never silent
                    from utils.soft_fail import soft_fail
                    soft_fail(f"scores.jsonl corrupt line skipped: {e} — "
                              f"line: {line[:120]}")
                    continue
    return out
