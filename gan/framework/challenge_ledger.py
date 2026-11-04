"""Blind, one-shot challenge lifecycle for the ``imo_proof`` pilot.

This module deliberately has no role or grader integration. It is the
framework-owned state carrier used by the challenge tool and settlement steps.
A challenge is locked before score/val evidence is available, can be
reserved only for the direct child of its producer node, and is consumed once.
All writes require the normal run ``WriterToken``.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from gan.framework import paths
from gan.framework.ledger import guard_writer

LEDGER_VERSION = 1
DOMAIN = "imo_proof"

STATUS_LOCKED = "locked"
STATUS_RESERVED = "reserved"
STATUS_CONSUMED = "consumed"
ADJUDICATION_FIELD = "adjudication"

_REQUIRED = ("qid", "defect_type", "claim", "evidence_ref", "repair_criterion")
_FORBIDDEN = {
    "score", "points", "actual_points", "predicted_max_points", "grader",
    "grader_reason", "grader_output", "val", "val_score", "test_score",
    "reference_solution", "grading_guidelines",
}


class ChallengeError(ValueError):
    """Invalid challenge data or an invalid lifecycle transition."""


def empty() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "domain": DOMAIN, "locks": {},
            "challenges": {}}


def _normalise(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return empty()
    if data.get("version") != LEDGER_VERSION or data.get("domain") != DOMAIN:
        return empty()
    locks = data.get("locks")
    challenges = data.get("challenges")
    if not isinstance(locks, dict) or not isinstance(challenges, dict):
        return empty()
    return data


def load(output_dir: str) -> Dict[str, Any]:
    path = paths.challenge_ledger_path(output_dir)
    if not os.path.isfile(path):
        return empty()
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return _normalise(json.load(stream))
    except (OSError, ValueError, TypeError):
        return empty()


def save(output_dir: str, data: Dict[str, Any], writer: Any) -> None:
    guard_writer(writer, "challenge ledger")
    clean = _normalise(data)
    path = paths.challenge_ledger_path(output_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(clean, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _text(value: Any, field: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ChallengeError(f"challenge {field} must be non-empty")
    return value


def validate_candidate(item: Mapping[str, Any], *, allowed_qids: Iterable[Any],
                       seen_qids: Iterable[Any] = ()) -> Dict[str, str]:
    """Validate one blind candidate without mutating or revealing ledger state."""
    if not isinstance(item, Mapping):
        raise ChallengeError("each challenge must be an object")
    forbidden = _FORBIDDEN.intersection(item)
    if forbidden:
        raise ChallengeError("blind challenge contains revealed field(s): "
                             + ", ".join(sorted(forbidden)))
    row = {field: _text(item.get(field), field) for field in _REQUIRED}
    allowed = {str(qid) for qid in allowed_qids}
    if row["qid"] not in allowed:
        raise ChallengeError(f"challenge qid is not an allowed train qid: {row['qid']}")
    if row["qid"] in {str(qid) for qid in seen_qids}:
        raise ChallengeError(f"duplicate challenge qid: {row['qid']}")
    return row


def _lock_key(outer: Any, parent_node: Any, evaluator_version: Any) -> str:
    return f"{outer}:{parent_node}:{evaluator_version}"


def _challenge_id(outer: Any, parent_node: Any, index: int) -> str:
    return f"ch-{outer}-{parent_node}-{index}"


def lock(
    data: Dict[str, Any], *, outer: Any, parent_node: Any,
    evaluator_version: Any, challenges: Sequence[Mapping[str, Any]],
    allowed_qids: Iterable[Any], max_k: int = 5,
) -> List[str]:
    """Blind-lock a bounded set of qid-bound challenges.

    The operation is idempotent for a resumed generation.  A second lock with
    the same producer key must contain the exact same payload; a different
    payload is rejected rather than silently replacing evidence.
    """
    if not isinstance(data, dict):
        raise ChallengeError("challenge ledger must be a mapping")
    if data.get("version") != LEDGER_VERSION or data.get("domain") != DOMAIN:
        raise ChallengeError("unsupported challenge ledger")
    if not isinstance(max_k, int) or max_k < 1:
        raise ChallengeError("max_k must be a positive integer")
    items = list(challenges or [])
    if len(items) > max_k:
        raise ChallengeError(f"challenge count exceeds max_k={max_k}")
    allowed = {str(qid) for qid in allowed_qids}
    key = _lock_key(outer, parent_node, evaluator_version)
    locks = data.setdefault("locks", {})
    records = data.setdefault("challenges", {})
    payload: List[Dict[str, str]] = []
    seen_qids = set()
    for item in items:
        row = validate_candidate(item, allowed_qids=allowed, seen_qids=seen_qids)
        seen_qids.add(row["qid"])
        payload.append(row)
    existing = locks.get(key)
    if existing is not None:
        if existing.get("payload") != payload:
            raise ChallengeError("resumed challenge lock payload does not match")
        return list(existing.get("challenge_ids") or [])

    ids: List[str] = []
    for index, row in enumerate(payload):
        challenge_id = _challenge_id(outer, parent_node, index)
        if challenge_id in records:
            raise ChallengeError(f"challenge id collision: {challenge_id}")
        records[challenge_id] = {
            "challenge_id": challenge_id,
            "outer": outer,
            "producer_node": str(parent_node),
            "producer_evaluator_version": str(evaluator_version),
            **row,
            "status": STATUS_LOCKED,
            "reserved_child_node": None,
            "consumed_child_node": None,
            ADJUDICATION_FIELD: None,
        }
        ids.append(challenge_id)
    locks[key] = {"payload": copy.deepcopy(payload), "challenge_ids": ids}
    return ids


def available_for_parent(data: Dict[str, Any], *, parent_node: Any
                         ) -> List[Dict[str, Any]]:
    """Return locked, not-yet-consumed challenges for a parent node."""
    return [
        row for row in (data.get("challenges", {}).values() or [])
        if isinstance(row, dict)
        and str(row.get("producer_node")) == str(parent_node)
        and row.get("status") == STATUS_LOCKED
    ]


def reserve_for_child(data: Dict[str, Any], *, parent_node: Any,
                      child_node: Any) -> List[Dict[str, Any]]:
    """Reserve the producer node's challenges for one direct child."""
    rows = [row for row in data.get("challenges", {}).values()
            if isinstance(row, dict) and str(row.get("producer_node")) == str(parent_node)]
    reserved: List[Dict[str, Any]] = []
    for row in rows:
        status = row.get("status")
        bound = row.get("reserved_child_node") or row.get("consumed_child_node")
        if status == STATUS_CONSUMED:
            if str(bound) != str(child_node):
                raise ChallengeError("challenge was already consumed by another child")
            reserved.append(row)
            continue
        if status == STATUS_RESERVED and str(bound) != str(child_node):
            raise ChallengeError("challenge is already reserved for another child")
        if status not in (STATUS_LOCKED, STATUS_RESERVED):
            raise ChallengeError(f"invalid challenge status: {status!r}")
        row["status"] = STATUS_RESERVED
        row["reserved_child_node"] = str(child_node)
        reserved.append(row)
    return reserved


def consume_for_child(data: Dict[str, Any], *, parent_node: Any,
                      child_node: Any, planner_version: Any = None) -> List[Dict[str, Any]]:
    """Consume a reservation exactly once; repeated resume is idempotent."""
    rows = reserve_for_child(data, parent_node=parent_node, child_node=child_node)
    for row in rows:
        if row.get("status") == STATUS_RESERVED:
            row["status"] = STATUS_CONSUMED
            row["consumed_child_node"] = str(child_node)
            if planner_version is not None:
                row["consumer_planner_version"] = str(planner_version)
        elif (planner_version is not None
              and row.get("consumer_planner_version") is None):
            row["consumer_planner_version"] = str(planner_version)
    return rows


def settle(data: Dict[str, Any], *, challenge_id: Any,
           adjudication: Mapping[str, Any]) -> Dict[str, Any]:
    """Attach one immutable adjudication to an already-consumed challenge."""
    from gan.framework.challenge_adjudication import validate_result
    records = data.get("challenges", {})
    row = records.get(str(challenge_id))
    if not isinstance(row, dict):
        raise ChallengeError(f"unknown challenge id: {challenge_id}")
    if row.get("status") != STATUS_CONSUMED:
        raise ChallengeError("challenge must be consumed before adjudication")
    try:
        result = validate_result(adjudication)
    except ValueError as exc:
        raise ChallengeError(str(exc)) from exc
    existing = row.get(ADJUDICATION_FIELD)
    if existing is not None:
        if existing != result:
            raise ChallengeError("challenge adjudication is immutable")
        return dict(existing)
    row[ADJUDICATION_FIELD] = copy.deepcopy(result)
    return dict(result)


__all__ = [
    "LEDGER_VERSION", "DOMAIN", "STATUS_LOCKED", "STATUS_RESERVED",
    "STATUS_CONSUMED", "ChallengeError", "empty", "load", "save", "lock",
    "validate_candidate", "available_for_parent", "reserve_for_child",
    "consume_for_child", "settle",
]
