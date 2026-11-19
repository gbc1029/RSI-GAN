"""Blind, one-shot challenge lifecycle (framework-owned, domain-parameterised).

Ported from the IMO pilot (``origin/challenge_design``) with ONE structural
change (docs/21 §8.1): the domain is an explicit argument instead of the
hard-coded ``"imo_proof"`` constant, so balrog and imo_proof share one ledger
implementation and one set of invariants.

Invariants this file is the only writer for:

* a challenge is **blind-locked** (created before any score/val evidence exists);
* it can be **reserved** by exactly ONE direct child of its producer node;
* it is **consumed once** and then **settled once** (adjudication is immutable);
* every mutation goes through the run's ``WriterToken``;
* a resume with the same producer key must carry the SAME payload (a resumed
  generation may re-run the lock; a different payload is a bug, not an update).

Nothing here knows about domains' defect semantics: it carries
``defect_class`` / ``family`` / ``evidence_ref`` / ``repair_criterion`` opaquely.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from gan.framework import paths
from gan.framework.ledger import guard_writer

LEDGER_VERSION = 1

STATUS_LOCKED = "locked"
STATUS_MATERIALIZED = "materialized"
STATUS_RESERVED = "reserved"
STATUS_CONSUMED = "consumed"
ADJUDICATION_FIELD = "adjudication"

# Fields a blind challenge MUST carry. ``defect_class`` is the closed-vocabulary
# key the domain adjudicator dispatches on.
_REQUIRED = ("defect_class", "family", "evidence_ref", "repair_criterion")

# Anything that could have been read AFTER the reveal must never enter a blind
# challenge (the whole point of locking before the score is available).
_FORBIDDEN = {
    "score", "points", "actual_points", "predicted_max_points", "grader",
    "grader_reason", "grader_output", "val", "val_score", "test_score",
    "reference_solution", "grading_guidelines", "progression", "solved",
}

_PROTECTED_RESPONSE_KEYS = re.compile(
    r"(?:reference[_ -]?solution|grading[_ -]?guidelines|answer[_ -]?key|"
    r"ground[_ -]?truth|grader[_ -]?output)", re.IGNORECASE)


class ChallengeError(ValueError):
    """Invalid challenge data or an invalid lifecycle transition."""


def audit_hardcoding(episode_ids: Iterable[Any], patch: Any = "",
                     config: Any = None) -> Dict[str, Any]:
    """Reject an instance-specific answer/spec leak in a task response.

    The mechanical half of the anti-overfit gate (docs/21 §6 layer A): a planner
    may teach a general policy, but must not branch on a concrete episode id or
    embed a protected answer/grader field. Semantic "is this a strategy or a
    memorised answer" is a DIFFERENT question and is deliberately not asked
    here -- conflating them is what made the IMO adjudicator's ``actionable``
    flag useless (72/72 true).
    """
    ids = [str(i).strip() for i in (episode_ids or []) if str(i).strip()]
    try:
        blob = json.dumps(config, ensure_ascii=False, sort_keys=True,
                          default=str) if config is not None else ""
    except (TypeError, ValueError):
        blob = str(config or "")
    added = "\n".join(
        line[1:] for line in str(patch or "").splitlines()
        if line.startswith("+") and not line.startswith("+++"))
    text = f"{added}\n{blob}"
    for eid in ids:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(eid)
                     + r"(?![A-Za-z0-9_])", text):
            return {"triggered": True, "reason": "episode_id_embedded",
                    "episode_id": eid, "hard_gate": True,
                    "modification_scope": "instance_specific_memory"}
    match = _PROTECTED_RESPONSE_KEYS.search(text)
    if match:
        return {"triggered": True, "reason": "protected_answer_or_grader_field",
                "token": match.group(0), "hard_gate": True,
                "modification_scope": "answer_leakage"}
    return {"triggered": False, "reason": None, "hard_gate": False,
            "modification_scope": None}


def empty(domain: str) -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "domain": str(domain), "locks": {},
            "challenges": {}}


def _normalise(data: Any, domain: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return empty(domain)
    if data.get("version") != LEDGER_VERSION or data.get("domain") != str(domain):
        # A ledger written by another domain (or an older version) is not ours
        # to interpret: start empty rather than half-trust it.
        return empty(domain)
    if not isinstance(data.get("locks"), dict) or \
            not isinstance(data.get("challenges"), dict):
        return empty(domain)
    return data


def load(output_dir: str, domain: str) -> Dict[str, Any]:
    path = paths.challenge_ledger_path(output_dir)
    if not os.path.isfile(path):
        return empty(domain)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return _normalise(json.load(stream), domain)
    except (OSError, ValueError, TypeError):
        return empty(domain)


def save(output_dir: str, data: Dict[str, Any], writer: Any, domain: str) -> None:
    guard_writer(writer, "challenge ledger")
    clean = _normalise(data, domain)
    path = paths.challenge_ledger_path(output_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(clean, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChallengeError(f"challenge {field} must be non-empty")
    return text


def validate_candidate(item: Mapping[str, Any], *, allowed_families: Iterable[Any],
                       seen: Iterable[Any] = ()) -> Dict[str, str]:
    """Validate one blind candidate without mutating or revealing ledger state."""
    if not isinstance(item, Mapping):
        raise ChallengeError("each challenge must be an object")
    forbidden = _FORBIDDEN.intersection(item)
    if forbidden:
        raise ChallengeError("blind challenge contains revealed field(s): "
                             + ", ".join(sorted(forbidden)))
    row = {field: _text(item.get(field), field) for field in _REQUIRED}
    allowed = {str(f) for f in allowed_families}
    if allowed and row["family"] not in allowed:
        raise ChallengeError(
            f"challenge family is not an allowed family for this generation: {row['family']}")
    duplicate_key = (row["defect_class"], row["family"])
    if duplicate_key in {(str(c), str(f)) for c, f in seen}:
        raise ChallengeError(
            f"duplicate challenge for ({row['defect_class']}, {row['family']})")
    return row


def _lock_key(outer: Any, producer_node: Any, evaluator_version: Any) -> str:
    return f"{outer}:{producer_node}:{evaluator_version}"


def _challenge_id(outer: Any, producer_node: Any, index: int) -> str:
    return f"ch-{outer}-{producer_node}-{index}"


def lock(data: Dict[str, Any], *, domain: str, outer: Any, producer_node: Any,
         evaluator_version: Any, challenges: Sequence[Mapping[str, Any]],
         allowed_families: Iterable[Any], max_k: int = 2,
         qids: Optional[Sequence[Any]] = None) -> List[str]:
    """Blind-lock a bounded set of family-bound challenges (idempotent on resume).

    ``qids`` binds each challenge to the concrete GENERATION ITEM the claim is
    about (for an episode domain: the episode the producer actually played where
    the defect appeared). It is framework-owned evidence -- the model never
    supplies it, and it is what the adjudicator later reads the parent side from,
    so "the defect was present in the parent" cannot drift to a different
    instance than the one the author cited.
    """
    if not isinstance(data, dict):
        raise ChallengeError("challenge ledger must be a mapping")
    if data.get("version") != LEDGER_VERSION or data.get("domain") != str(domain):
        raise ChallengeError("unsupported challenge ledger")
    if not isinstance(max_k, int) or max_k < 1:
        raise ChallengeError("max_k must be a positive integer")
    items = list(challenges or [])
    if len(items) > max_k:
        raise ChallengeError(f"challenge count exceeds max_k={max_k}")
    bound = [str(q) for q in (qids or [])]
    if bound and len(bound) != len(items):
        raise ChallengeError("qids must align 1:1 with challenges")
    key = _lock_key(outer, producer_node, evaluator_version)
    locks = data.setdefault("locks", {})
    records = data.setdefault("challenges", {})
    payload: List[Dict[str, str]] = []
    seen: List[tuple] = []
    for item in items:
        row = validate_candidate(item, allowed_families=allowed_families, seen=seen)
        seen.append((row["defect_class"], row["family"]))
        payload.append(row)
    existing = locks.get(key)
    if existing is not None:
        if existing.get("payload") != payload:
            raise ChallengeError("resumed challenge lock payload does not match")
        return list(existing.get("challenge_ids") or [])

    ids: List[str] = []
    for index, row in enumerate(payload):
        challenge_id = _challenge_id(outer, producer_node, index)
        if challenge_id in records:
            raise ChallengeError(f"challenge id collision: {challenge_id}")
        records[challenge_id] = {
            "challenge_id": challenge_id,
            "outer": outer,
            "producer_node": str(producer_node),
            "producer_evaluator_version": str(evaluator_version),
            **row,
            "qid": (bound[index] if bound else ""),
            "status": STATUS_LOCKED,
            "instances": None,
            "reserved_child_node": None,
            "consumed_child_node": None,
            ADJUDICATION_FIELD: None,
        }
        ids.append(challenge_id)
    locks[key] = {"payload": copy.deepcopy(payload), "challenge_ids": ids,
                  "qids": list(bound)}
    return ids


def attach_instances(data: Dict[str, Any], *, challenge_id: Any,
                     instances: Mapping[str, Any]) -> Dict[str, Any]:
    """Framework-side materialisation: bind the concrete instances (idempotent).

    ``instances`` is ``{"train": {...}, "eval": {...}}`` as produced by the
    domain generator. The MODEL never chooses a seed or a level, which is what
    keeps "authoring" in the L1 regime (docs/21 §4).
    """
    row = data.get("challenges", {}).get(str(challenge_id))
    if not isinstance(row, dict):
        raise ChallengeError(f"unknown challenge id: {challenge_id}")
    if row.get("status") == STATUS_CONSUMED:
        raise ChallengeError("cannot attach instances to a consumed challenge")
    payload = dict(instances or {})
    existing = row.get("instances")
    if existing is not None:
        if existing != payload:
            raise ChallengeError("challenge instances are immutable")
        return dict(existing)
    row["instances"] = payload
    if row.get("status") == STATUS_LOCKED:
        row["status"] = STATUS_MATERIALIZED
    return payload


def available_for_parent(data: Dict[str, Any], *, parent_node: Any) -> List[Dict[str, Any]]:
    """Locked/materialised, not-yet-consumed challenges of one producer node."""
    return [
        row for row in (data.get("challenges", {}).values() or [])
        if isinstance(row, dict)
        and str(row.get("producer_node")) == str(parent_node)
        and row.get("status") in (STATUS_LOCKED, STATUS_MATERIALIZED)
    ]


def reserve_for_child(data: Dict[str, Any], *, parent_node: Any, child_node: Any,
                      challenge_ids: Optional[Iterable[Any]] = None
                      ) -> List[Dict[str, Any]]:
    """Reserve the producer node's challenges for ONE direct child."""
    wanted = (None if challenge_ids is None
              else {str(v) for v in challenge_ids})
    rows = [row for row in data.get("challenges", {}).values()
            if isinstance(row, dict)
            and str(row.get("producer_node")) == str(parent_node)
            and (wanted is None or str(row.get("challenge_id")) in wanted)]
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
        if status not in (STATUS_LOCKED, STATUS_MATERIALIZED, STATUS_RESERVED):
            raise ChallengeError(f"invalid challenge status: {status!r}")
        row["status"] = STATUS_RESERVED
        row["reserved_child_node"] = str(child_node)
        reserved.append(row)
    return reserved


def consume_for_child(data: Dict[str, Any], *, parent_node: Any, child_node: Any,
                      planner_version: Any = None,
                      challenge_ids: Optional[Iterable[Any]] = None
                      ) -> List[Dict[str, Any]]:
    """Consume a reservation exactly once; a resumed run is idempotent."""
    rows = reserve_for_child(data, parent_node=parent_node, child_node=child_node,
                             challenge_ids=challenge_ids)
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
    row = data.get("challenges", {}).get(str(challenge_id))
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
    "LEDGER_VERSION", "STATUS_LOCKED", "STATUS_MATERIALIZED", "STATUS_RESERVED",
    "STATUS_CONSUMED", "ChallengeError", "empty", "load", "save", "lock",
    "attach_instances", "validate_candidate", "available_for_parent",
    "reserve_for_child", "consume_for_child", "settle", "audit_hardcoding",
]
