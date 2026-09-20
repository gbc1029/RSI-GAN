"""Shared JSONL trajectory logging (substrate).

One JSON object per line, appended thread-safely, with **size-based rotation**.
Used by the shared agent runtime (``agent/base_agent.py``,
``agent/llm_withtools.py``) and the framework event logs, so GAN roles and task
agents (and DGM-H / polyglot task agents) all produce the same structured format.

Rotation: when a file exceeds ``max_bytes`` it is renamed to ``<path>.<n>`` and a
fresh file starts. Read a full stream oldest-first with :func:`read_all`.

Record shapes (superset; all records carry ``ts`` and ``kind``):
    {"kind": "log",         "level": "INFO", "text": ...}
    {"kind": "input",       "text": ...}
    {"kind": "output",      "text": ...}
    {"kind": "tool_call",   "tool": ..., "input": {...}}
    {"kind": "tool_output", "tool": ..., "output": "..."}
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _parts(path: str) -> List[str]:
    """Rotated parts of ``path``, oldest first (``path.1`` is the oldest)."""
    out = []
    for p in glob.glob(path + ".*"):
        suffix = p[len(path) + 1:]
        if suffix.isdigit():
            out.append((int(suffix), p))
    return [p for _n, p in sorted(out)]


def _rotate(path: str) -> None:
    nums = [int(p[len(path) + 1:]) for p in _parts(path)]
    nxt = (max(nums) + 1) if nums else 1
    try:
        os.replace(path, f"{path}.{nxt}")
    except OSError:
        pass


def append(path: str, record: Dict[str, Any], max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rec = {"ts": time.time(), **record}
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        if max_bytes and os.path.isfile(path) and os.path.getsize(path) >= int(max_bytes):
            _rotate(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def reset(path: Optional[str]) -> None:
    """Start a fresh instance log (truncate current + drop rotated parts)."""
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _LOCK:
        for p in _parts(path):
            try:
                os.remove(p)
            except OSError:
                pass
        with open(path, "w", encoding="utf-8") as f:
            f.write("")


def read_all(path: str) -> str:
    """Concatenate rotated parts + current file, oldest first."""
    chunks: List[str] = []
    for p in _parts(path):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                chunks.append(f.read())
        except OSError:
            pass
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            chunks.append(f.read())
    except OSError:
        pass
    return "".join(chunks)
