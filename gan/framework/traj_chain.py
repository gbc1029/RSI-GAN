"""Trajectory row hash chain (docs/19 §12.28, CM F5 P2).

Parent-side rows are stamped ``(seq, prev_hash)`` by the WRITER (the role
process host / the task telemetry sink) before they touch disk — the child
never sees the chain state, so a rewritten, deleted or reordered row is a
DETECTABLE chain break instead of an invisible edit.

The chain is keyed by the LOGICAL stream (the base ``.jsonl`` path), NOT the
physical file: ``utils/trajectory_log`` rotates at ``DEFAULT_MAX_BYTES``
(``<path>.<n>``, ``.1`` oldest), and seq/prev_hash continue across parts.
Verification walks the rotated parts oldest-first plus the current file; the
``.llm_calls.jsonl`` sidecar is a separate stream (verified by its own base
path).

Canonicalization: rows are hashed as
``json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)`` where
``body`` is the record WITHOUT its own chain/envelope fields
(``seq``/``prev_hash``/``ts``/``time``). The same
canonicalization at verify time (parse the on-disk row, strip the two chain
fields, re-canonicalize) is byte-identical to the write-time hash, because
``default=str`` effects are already materialized in the stored JSON.

Threading: one sink writes one stream (the role host serve loop / the task
telemetry server each own their chain); a lock guards the multi-stream dict.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Tuple

# seq/prev_hash are the chain itself; ts/time are the WRITER's envelope
# stamps (trajectory_log.append enriches every row with them at write time —
# the sink hashes the record BEFORE that enrichment, so verify must exclude
# them too). The chain covers the row CONTENT.
CHAIN_FIELDS = ("seq", "prev_hash", "ts", "time")


def _canonical(record: Dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in CHAIN_FIELDS}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


def stamp_head(record: Dict[str, Any], seq: int, prev_hash: str) -> Dict[str, Any]:
    """Return a copy of ``record`` with the chain fields stamped."""
    out = dict(record)
    out["seq"] = seq
    out["prev_hash"] = prev_hash
    return out


def _parts_oldest_first(base_path: str) -> List[str]:
    parts = sorted(
        (p for p in glob.glob(base_path + ".*")
         if os.path.basename(p).split(".")[-1].isdigit()),
        key=lambda p: int(os.path.basename(p).split(".")[-1]))
    if os.path.isfile(base_path):
        parts.append(base_path)
    return parts


class TrajChain:
    """Mutable chain state for logical streams (base path -> (seq, head))."""

    def __init__(self) -> None:
        self._state: Dict[str, Tuple[int, str]] = {}
        self._lock = threading.Lock()

    def stamp(self, base_path: str, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            seq, prev = self._state.get(base_path, (0, ""))
            head = hashlib.sha256(
                (prev + "|" + str(seq + 1) + "|" + _canonical(record)
                 ).encode("utf-8", "replace")).hexdigest()[:32]
            self._state[base_path] = (seq + 1, head)
            return stamp_head(record, seq + 1, prev)

    def fingerprint(self, base_path: str) -> Tuple[int, str]:
        with self._lock:
            return self._state.get(base_path, (0, ""))

    def state(self) -> Dict[str, Tuple[int, str]]:
        with self._lock:
            return dict(self._state)


def verify_stream(base_path: str) -> Tuple[bool, int, str, str]:
    """Walk one logical stream oldest-first and recompute the chain.

    Returns ``(ok, seq, head, reason)``: on success ``(True, last_seq,
    last_head, "")`` — an EMPTY or missing stream is ``(True, 0, "", "")``
    (absence is not a break); on failure ``(False, 0, "", reason)`` with the
    first break. Tolerates legacy rows: the chain is anchored at the FIRST
    row carrying a ``seq``; unchained rows before it are skipped (old
    checkpoints/sessions must keep reading)."""
    seq, prev = 0, ""
    anchored = False
    n = 0
    for part in _parts_oldest_first(base_path):
        try:
            with open(part, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        return (False, 0, "", f"unparsable row {n} in {part}")
                    if not isinstance(rec, dict):
                        return (False, 0, "", f"non-dict row {n} in {part}")
                    if "seq" not in rec:
                        if not anchored:
                            continue  # legacy unchained prefix
                        return (False, 0, "",
                                f"unchained row {n} after anchor in {part}")
                    if not anchored:
                        anchored = True
                        seq, prev = 0, ""
                    row_seq = rec.get("seq")
                    row_prev = rec.get("prev_hash")
                    if not isinstance(row_seq, int) or row_seq != seq + 1:
                        return (False, 0, "", f"seq break at row {n} "
                                              f"(got {row_seq!r}, want {seq + 1})")
                    if not isinstance(row_prev, str) or row_prev != prev:
                        return (False, 0, "", f"hash break at row {n}")
                    prev = hashlib.sha256(
                        (prev + "|" + str(row_seq) + "|" + _canonical(rec)
                         ).encode("utf-8", "replace")).hexdigest()[:32]
                    seq = row_seq
        except OSError as e:
            return (False, 0, "", f"unreadable part {part}: {e}")
    return (True, seq, prev, "")


def stream_fingerprint(base_path: str) -> Tuple[int, str, str]:
    """(seq, head, reason) of a stream without holding writer state."""
    ok, seq, head, reason = verify_stream(base_path)
    if not ok:
        return (0, "", reason)
    return (seq, head, "")
