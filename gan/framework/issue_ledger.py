"""Cross-outer issue ledger (FRAMEWORK, frozen): ``ckpt/issues/ledger.json``.

Why it exists
-------------
The evaluator's issue round-trip is per-round state: issues live on a node's
``meta.feedback`` and are consumed by the next round. At an outer boundary the
loop strips them (a parent selected from a DIFFERENT outer would otherwise carry
ids that collide with this outer's fresh ones), and a node's feedback only ever
holds ONE round's list -- so "what is still unfixed after six outers" had no
carrier at all. Every outer re-discovered the same defects.

This ledger is that carrier. It is keyed by the CLOSED vocabulary in
``gan/framework/issue_keys.py`` (not by ``issue_id``, which is a per-round
handle) and accumulates four observation kinds:

    raised          the evaluator reported this key in a round
    judged_fixed    a judge_fix verdict said the parent's issue was fixed
    judged_unfixed  ... said it was not
    stance          the planner's structured response_kind to the parent's issue

Persistence is a guarded write (B46 step 1): decision-adjacent state goes
through the per-run ``WriterToken``, exactly like the supernode records.

Two derived facts matter downstream
-----------------------------------
``status`` is a pure reducer over the accumulated evidence:

* ``fixed``        -- the last judgement said fixed and nothing re-raised it;
* ``not_actionable`` -- enough ``out_of_scope``/``disputed`` stances with zero
  confirmed fixes. Re-routing such a key to the planner every round is what
  produced the ten-times-repeated cost issue in A0: it burns ``respond_issue``
  budget and teaches the evaluator that its findings are ignored;
* ``open``         -- everything else (the only thing carried forward).

``carried_over`` renders the open keys as a bounded, read-only block. It is not
a second judging channel: the carried rows deliberately carry no ``issue_id``,
no ``description`` and no ``suggested_fix`` -- the incumbent evaluator is
expected to re-detect the defect and write a FRESH prescription, because the old
one was written against an older design and older code.

Truth accounting
----------------
Judged observations may carry ``truth`` (from ``gan/framework/issue_checks.py``)
and ``claimed``. ``apply_round`` folds them into per-key ``verified`` / ``hits``
counters and into ``outer_stats[<outer>]``, which is what
``verdict_stability = hits / verified`` reads. ``unsupported`` counts issues
raised about a defect that was ALREADY absent in the parent (the finding was not
real); they are excluded from the ratio so a fabricated issue cannot buy a free
"correct: not fixed" hit.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from gan.framework import paths
from gan.framework.issue_keys import (OWNER_FRAMEWORK, OTHER_KEY, owner_of)
from gan.framework.ledger import guard_writer

LEDGER_VERSION = 1

_KIND_RAISED = "raised"
_KIND_FIXED = "judged_fixed"
_KIND_UNFIXED = "judged_unfixed"
_KIND_STANCE = "stance"
_KINDS = (_KIND_RAISED, _KIND_FIXED, _KIND_UNFIXED, _KIND_STANCE)

STATUS_OPEN = "open"
STATUS_FIXED = "fixed"
STATUS_NOT_ACTIONABLE = "not_actionable"

_EMPTY: Dict[str, Any] = {"version": LEDGER_VERSION, "keys": {}}


# -- persistence -------------------------------------------------------------
def load(output_dir: str) -> Dict[str, Any]:
    """Read the ledger (an empty skeleton when absent or unreadable)."""
    p = paths.issues_ledger_path(output_dir)
    if not os.path.isfile(p):
        return {"version": LEDGER_VERSION, "keys": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 -- a corrupt ledger must not kill the run
        return {"version": LEDGER_VERSION, "keys": {}}
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        return {"version": LEDGER_VERSION, "keys": {}}
    data.setdefault("version", LEDGER_VERSION)
    return data


def _save(output_dir: str, data: Dict[str, Any], writer: Any) -> None:
    guard_writer(writer, "issue ledger")
    p = paths.issues_ledger_path(output_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# -- accumulation ------------------------------------------------------------
def _blank(key: str) -> Dict[str, Any]:
    return {
        "key": key,
        "owner": owner_of(key),
        "raised": 0,
        "judged": 0,
        "judged_fixed": 0,
        # Truth accounting (gan/framework/issue_checks.py): ``verified`` counts
        # the judged pairs whose defect state could be mechanically decided on
        # BOTH sides; ``hits`` counts those where the evaluator's claim matched.
        # ``unsupported`` counts issues raised about a defect that was already
        # absent in the parent -- excluded from the metric on purpose.
        "verified": 0,
        "hits": 0,
        "unsupported": 0,
        "stances": {},
        "first_outer": None,
        "last_outer": None,
        "last_genid": None,
        "last_verdict": None,
        "status": STATUS_OPEN,
        "not_actionable_reason": None,
    }


def _apply_one(rec: Dict[str, Any], obs: Dict[str, Any], outer: Any) -> None:
    kind = str(obs.get("kind") or "").strip()
    genid = obs.get("genid")
    if genid is not None:
        rec["last_genid"] = str(genid)
    if outer is not None:
        rec["last_outer"] = outer
        if rec["first_outer"] is None:
            rec["first_outer"] = outer
    if kind == _KIND_RAISED:
        rec["raised"] = int(rec.get("raised") or 0) + 1
        # A re-raise AFTER a confirmed fix means the fix did not hold.
        if rec.get("last_verdict") == "fixed":
            rec["last_verdict"] = "reopened"
    elif kind == _KIND_FIXED:
        rec["judged"] = int(rec.get("judged") or 0) + 1
        rec["judged_fixed"] = int(rec.get("judged_fixed") or 0) + 1
        rec["last_verdict"] = "fixed"
    elif kind == _KIND_UNFIXED:
        rec["judged"] = int(rec.get("judged") or 0) + 1
        rec["last_verdict"] = "unfixed"
    elif kind == _KIND_STANCE:
        stance = str(obs.get("stance") or "unspecified")
        stances = rec.get("stances")
        if not isinstance(stances, dict):
            stances = {}
        stances[stance] = int(stances.get(stance) or 0) + 1
        rec["stances"] = stances
    # Truth accounting rides the judged observations (the caller supplies it
    # from gan/framework/issue_checks.py; a missing ``truth`` stays unverified).
    if kind in (_KIND_FIXED, _KIND_UNFIXED):
        if obs.get("unsupported"):
            rec["unsupported"] = int(rec.get("unsupported") or 0) + 1
        truth = obs.get("truth")
        if isinstance(truth, bool):
            rec["verified"] = int(rec.get("verified") or 0) + 1
            if bool(obs.get("claimed")) == truth:
                rec["hits"] = int(rec.get("hits") or 0) + 1


def _classify(rec: Dict[str, Any], not_actionable_stances: int) -> None:
    """Derive ``status`` (a pure function of the accumulated counters)."""
    stances = rec.get("stances") or {}
    declined = int(stances.get("out_of_scope") or 0) + int(stances.get("disputed") or 0)
    raised = int(rec.get("raised") or 0)
    fixed = int(rec.get("judged_fixed") or 0)
    if declined >= int(not_actionable_stances) and fixed == 0 and raised >= 2:
        rec["status"] = STATUS_NOT_ACTIONABLE
        rec["not_actionable_reason"] = (
            f"{declined} declined stance(s) (out_of_scope/disputed) and no confirmed fix "
            f"after {raised} raise(s)")
    elif fixed > 0 and rec.get("last_verdict") == "fixed":
        rec["status"] = STATUS_FIXED
        rec["not_actionable_reason"] = None
    else:
        rec["status"] = STATUS_OPEN
        rec["not_actionable_reason"] = None


def apply_round(output_dir: str, *, outer: Any, observations: Iterable[Dict[str, Any]],
                writer: Any, not_actionable_stances: int = 2) -> Dict[str, Any]:
    """Fold one outer's observations into the ledger and persist it.

    ``observations`` are already JOINED by the caller (the loop owns the tree, so
    it is the only place that can resolve a verdict's ``issue_id`` back to the
    parent issue's key). Each item: ``{"key", "kind", "genid"[, "stance"]}``;
    unknown kinds and blank keys are ignored rather than raising, because this is
    an advisory sink -- a malformed observation must never kill a round.
    """
    data = load(output_dir)
    keys = data.setdefault("keys", {})
    touched: List[str] = []
    # Per-outer truth accounting. Kept in the ledger (not in memory) so a
    # resumed run and an offline audit read the SAME numbers the supernode got.
    stats = {"verified": 0, "hits": 0, "unsupported": 0,
             "judged": 0, "keys": []}
    for obs in observations or ():
        if not isinstance(obs, dict):
            continue
        key = str(obs.get("key") or "").strip()
        if not key or str(obs.get("kind") or "") not in _KINDS:
            continue
        rec = keys.get(key)
        if not isinstance(rec, dict):
            rec = _blank(key)
            keys[key] = rec
        rec["owner"] = owner_of(key)
        kind = str(obs.get("kind") or "")
        if kind in (_KIND_FIXED, _KIND_UNFIXED):
            stats["judged"] += 1
            if obs.get("unsupported"):
                stats["unsupported"] += 1
            if isinstance(obs.get("truth"), bool):
                stats["verified"] += 1
                if bool(obs.get("claimed")) == obs["truth"]:
                    stats["hits"] += 1
            if key not in stats["keys"]:
                stats["keys"].append(key)
        _apply_one(rec, obs, outer)
        touched.append(key)
    for key in set(touched):
        _classify(keys[key], not_actionable_stances)
    data["version"] = LEDGER_VERSION
    outer_stats = data.setdefault("outer_stats", {})
    if not isinstance(outer_stats, dict):
        outer_stats = {}
        data["outer_stats"] = outer_stats
    prev = outer_stats.get(str(outer)) or {}
    if isinstance(prev, dict):
        # A resumed outer re-folds its observations: keep the larger counts so a
        # partial replay can never shrink a bucket.
        for f in ("verified", "hits", "unsupported", "judged"):
            stats[f] = max(int(stats[f]), int(prev.get(f) or 0))
    outer_stats[str(outer)] = stats
    _save(output_dir, data, writer)
    return data


# -- projections -------------------------------------------------------------
def status_of(rec: Dict[str, Any]) -> str:
    return str(rec.get("status") or STATUS_OPEN)


def carried_over(output_dir: str, *, limit: int = 6,
                 audience: str = "planner") -> List[Dict[str, Any]]:
    """Open keys as a bounded, id-free list for the next round's context.

    ``audience="planner"`` drops ``framework``-owned keys: the planner has no
    operator that can fix an access/harness defect, so carrying them only burns
    ``respond_issue`` budget. ``audience="evaluator"`` keeps them (they are
    legitimate context for its own calibration).

    Rows carry NO ``issue_id``, ``description`` or ``suggested_fix`` on purpose
    -- see the module docstring.
    """
    data = load(output_dir)
    rows: List[Dict[str, Any]] = []
    for key, rec in (data.get("keys") or {}).items():
        if not isinstance(rec, dict):
            continue
        if status_of(rec) != STATUS_OPEN:
            continue
        owner = owner_of(key)
        if audience == "planner" and owner == OWNER_FRAMEWORK:
            continue
        stances = rec.get("stances") or {}
        last_stance = None
        if isinstance(stances, dict) and stances:
            last_stance = max(
                stances.items(),
                key=lambda kv: (int(kv[1] or 0), str(kv[0])))[0]
        rows.append({
            "issue_key": key,
            "owner": owner,
            "raised": int(rec.get("raised") or 0),
            "judged_fixed": int(rec.get("judged_fixed") or 0),
            "last_verdict": rec.get("last_verdict"),
            "last_stance": last_stance,
            "first_outer": rec.get("first_outer"),
            "last_outer": rec.get("last_outer"),
        })
    rows.sort(key=lambda r: (-r["raised"], -(r["last_outer"] or 0), r["issue_key"]))
    if limit and limit > 0:
        rows = rows[: int(limit)]
    return rows


def summary(output_dir: str) -> Dict[str, Any]:
    """Counters for the ``issue_ledger_updated`` event (audit, not decisions)."""
    data = load(output_dir)
    keys = data.get("keys") or {}
    by_status: Dict[str, int] = {}
    by_owner: Dict[str, int] = {}
    for key, rec in keys.items():
        if not isinstance(rec, dict):
            continue
        by_status[status_of(rec)] = by_status.get(status_of(rec), 0) + 1
        owner = owner_of(key)
        by_owner[owner] = by_owner.get(owner, 0) + 1
    return {"keys": len(keys), "by_status": by_status, "by_owner": by_owner}


def not_actionable_keys(output_dir: str) -> List[str]:
    data = load(output_dir)
    return sorted(k for k, rec in (data.get("keys") or {}).items()
                  if isinstance(rec, dict) and status_of(rec) == STATUS_NOT_ACTIONABLE)


def outer_verdict_stats(output_dir: str, outer: Any) -> Dict[str, Any]:
    """This outer's per-issue truth accounting, as ``verdict_stability`` input.

    ``verdict_stability = hits / verified`` -- the fraction of judged issues
    whose mechanical truth (``gan/framework/issue_checks.py``) matched the
    evaluator's ``fixed`` claim. ``None`` when nothing was verifiable this
    outer; ``fitness_e`` treats ``None`` as neutral (term skipped), never as a
    below-average hit-rate.
    """
    data = load(output_dir)
    stats = ((data.get("outer_stats") or {}).get(str(outer)) or {})
    verified = int(stats.get("verified") or 0)
    hits = int(stats.get("hits") or 0)
    return {
        "verdict_stability": (hits / verified) if verified > 0 else None,
        "verified": verified,
        "hits": hits,
        "unsupported": int(stats.get("unsupported") or 0),
        "judged": int(stats.get("judged") or 0),
    }


__all__ = [
    "LEDGER_VERSION", "STATUS_OPEN", "STATUS_FIXED", "STATUS_NOT_ACTIONABLE",
    "apply_round", "carried_over", "load", "not_actionable_keys",
    "outer_verdict_stats", "status_of", "summary", "OTHER_KEY",
]
