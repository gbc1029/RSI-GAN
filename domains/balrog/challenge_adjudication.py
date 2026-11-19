"""Mechanical challenge adjudicator for balrog (docs/21 §5.3).

ZERO LLM. The whole point of the balrog design is that "is the claimed defect
still there" is a MECHANICAL question once the defect class comes from a closed
vocabulary and the instance comes from the environment's own generator: the
per-episode facts the harness already records decide it.

WHAT THIS MODULE DOES (and only this)
-------------------------------------
1. maps ``challenge["defect_class"]`` to a PER-EPISODE predicate;
2. applies it to the parent's row (the old episode the parent actually played)
   and to the child's row (the challenge's ``instances.train``);
3. refuses to call it a repair when PROGRESSION DROPPED (decision D3: a defect
   can always be "fixed" by making the agent do less);
4. hands the three verdict fields to the framework state machine
   (``gan.framework.challenge_adjudication.adjudicate``), which owns the state
   vocabulary -- this module never invents a state.

WHY THE DEFECT LINES ARRIVE AS AN ARGUMENT
------------------------------------------
The thresholds live in ONE place (``domains.yaml`` ``output_contract.defect_lines``,
read through ``gan.framework.defect_lines``) because the ORDINARY issue channel
(``issue_checks._chk_dead_end``) uses the same numbers. Duplicating them here
would let the two channels disagree about the same episode -- exactly the failure
§3.3 exists to prevent. This module therefore REFUSES to judge when the lines are
not supplied (``UNDECIDABLE: defect_lines_undeclared``) instead of falling back
to a private default.

WHY ADMISSION IS NOT PART OF THIS
---------------------------------
Solvability / non-degeneracy / K-budget / dedup are the A and B layers
(``challenge_space`` + stage-1 ``challenge_questions``). They run BEFORE the
instance is used, and mixing them in here would make a wrong admission
indistinguishable from a wrong defect verdict -- the mistake that made the IMO
pilot's ``actionable`` flag 72/72 true and therefore useless.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from gan.framework import challenge_adjudication as contract

#: defect classes this adapter can decide FROM THE FACTS A REPORT ROW CARRIES.
#:
#: ``process.dead-end`` is the degenerate-cycle family: the episode either never
#: left its starting neighbourhood (``zero_displacement``: <= 2 distinct observed
#: states) or spent >= the declared share of its steps re-trying an already-tried
#: ``(state, action)`` pair. That is the SAME primitive
#: ``issue_checks._chk_dead_end`` aggregates generation-wide, which is what makes
#: the two-channel consistency assertion meaningful.
#:
#: ``policy.no-interaction`` and ``policy.blocked-forward`` are DELIBERATELY not
#: listed yet: their per-episode evidence is an ACTION-CLASS count, and the
#: report row does not carry one (the aggregate ``behavior.action_class_shares``
#: is generation-level). Claiming them now would mean either re-deriving the
#: trace (impossible from a row) or inventing a proxy -- and a proxy that agrees
#: with the ordinary channel by accident is worse than an honest "not yet".
#: They light up the moment the row carries ``action_class_counts`` (stage 1
#: extends the report); adding a name here is then a one-line change.
SUPPORTED_DEFECT_CLASSES = ("process.dead-end",)

#: default progression tolerance for the D3 no-regression check. 0 = "must not
#: get worse at all", which is legitimate because parent and child rows come from
#: the SAME family and (by admission) the same difficulty band.
DEFAULT_PROGRESSION_TOL = 0.0

#: the framework state machine refuses to call anything actionable unless the
#: change it attributes is transferable; a challenge built from the closed
#: defect vocabulary and generator-drawn instances is transferable BY
#: CONSTRUCTION (there is no instance-specific material to memorise).
ACTIONABILITY_BASIS = "transferable_strategy"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def episode_dead_end(row: Optional[Mapping[str, Any]],
                     lines: Mapping[str, float]) -> Optional[bool]:
    """Is THIS episode degenerate? ``None`` when its row cannot decide it.

    Two independent signals, OR-ed exactly like the generation-level checker:
    ``zero_displacement`` (never left the start) and ``revisit_share`` (cycling
    through already-tried ``(state, action)`` pairs). A row that carries neither
    fact is undecidable -- an ABORTED episode is the common case and must never
    be read as "the defect is gone".
    """
    if not isinstance(row, Mapping):
        return None
    decided = False
    present = False
    if isinstance(row.get("zero_displacement"), bool):
        decided = True
        present = present or bool(row["zero_displacement"])
    revisit = _num(row.get("revisit_share"))
    if revisit is not None:
        decided = True
        line = lines.get("cycle_degenerate_revisit_share")
        if line is not None:
            present = present or revisit >= float(line)
    return present if decided else None


def defect_present(defect_class: str, row: Optional[Mapping[str, Any]],
                   lines: Mapping[str, float]) -> Optional[bool]:
    """Dispatch one per-episode predicate; ``None`` = not decidable."""
    if defect_class == "process.dead-end":
        return episode_dead_end(row, lines)
    return None


def defect_families(rows: Any, defect_class: str,
                    lines: Mapping[str, float]) -> Dict[str, Dict[str, int]]:
    """``{family: {"hits": n, "decided": m}}`` over ONE generation's episode rows.

    The A layer's "the family must be one where the defect actually appeared"
    rule (docs/21 §6). It runs on the PARENT generation's own report, before any
    question exists, so the author cannot cite a family whose episodes were
    healthy -- the failure mode that would let it ask about a defect it cannot
    demonstrate.

    ``decided`` counts traced episodes (an aborted row decides nothing), so a
    caller can tell "no hits" from "no evidence" and refuse to offer a family
    whose only episodes were aborted.
    """
    out: Dict[str, Dict[str, int]] = {}
    for row in (rows or ()):
        if not isinstance(row, Mapping):
            continue
        family = str(row.get("task") or "")
        if not family:
            continue
        present = defect_present(defect_class, row, lines)
        if present is None:
            continue
        bucket = out.setdefault(family, {"hits": 0, "decided": 0})
        bucket["decided"] += 1
        if present:
            bucket["hits"] += 1
    return out


def adjudicate(challenge: Mapping[str, Any],
               parent_evidence: Optional[Mapping[str, Any]],
               child_evidence: Optional[Mapping[str, Any]],
               **_: Any) -> Dict[str, Any]:
    """Judge one challenge. Callable as ``(challenge, parent_ev, child_ev)``.

    ``challenge`` carries the ledger item plus two framework-injected keys the
    loop supplies at settle time:

    * ``defect_lines`` -- the declared thresholds (see the module docstring for
      why they are not read here);
    * ``parent_row`` / ``child_row`` -- the two per-episode report rows;
    * ``patch`` / ``config`` -- the child's change, for the hardcoding audit.
    """
    item = dict(challenge or {})
    qid = str(item.get("qid") or "")
    defect_class = str(item.get("defect_class") or "")
    lines = item.get("defect_lines")
    ref = str(item.get("evidence_ref") or "")

    def undecidable(reason: str) -> Dict[str, Any]:
        return {
            "state": contract.UNDECIDABLE,
            "reason": reason,
            "qid": qid,
            "actionable": None,
            "modification_scope": None,
            "parent_present": None,
            "child_present": None,
            "evidence_ref": ref,
        }

    if defect_class not in SUPPORTED_DEFECT_CLASSES:
        return undecidable(f"unsupported_defect_class:{defect_class or 'missing'}")
    if not isinstance(lines, Mapping) or not lines:
        return undecidable("defect_lines_undeclared")

    parent_row = item.get("parent_row")
    child_row = item.get("child_row")
    parent_present = defect_present(defect_class, parent_row, lines)
    child_present = defect_present(defect_class, child_row, lines)

    # D3: a "repair" that costs progression is not a repair. Checked BEFORE the
    # state machine so the state machine stays a pure function of three booleans.
    #
    # ONLY when the parent actually HAD the defect (``parent_present is True``).
    # Applying it to an ABSENT parent silently converts an unsupported challenge
    # into ``REGRESSION_AFTER_FALSE_CHALLENGE`` -- i.e. it punishes the planner for
    # an ordinary progression wobble on a defect it never had, which is both wrong
    # about the claim and wrong about the attribution (measured by this suite's
    # B section on real stage-4 reports before the guard was added).
    if parent_present is True and child_present is False:
        parent_points = None
        if isinstance(parent_evidence, Mapping):
            parent_points = _num(parent_evidence.get("points"))
        if parent_points is None and isinstance(parent_row, Mapping):
            parent_points = _num(parent_row.get("progression"))
        child_points = None
        if isinstance(child_evidence, Mapping):
            child_points = _num(child_evidence.get("points"))
        if child_points is None and isinstance(child_row, Mapping):
            child_points = _num(child_row.get("progression"))
        tol = _num(item.get("progression_tol"))
        tol = DEFAULT_PROGRESSION_TOL if tol is None else float(tol)
        if parent_points is not None and child_points is not None:
            if child_points < parent_points - tol:
                # The claimed defect is gone but the episode got WORSE: the
                # honest verdict is "not a repair", not "repaired".
                child_present = True

    scope = None
    try:
        from gan.framework.challenge_ledger import audit_hardcoding

        audit = audit_hardcoding(
            [qid, str(item.get("train_instance") or "")],
            patch=item.get("patch") or "",
            config=item.get("config"),
        )
        scope = audit.get("modification_scope")
    except Exception:  # noqa: BLE001 -- an unauditable change is "uncertain", not "clean"
        scope = "uncertain"
    if scope is None:
        scope = ACTIONABILITY_BASIS

    result = contract.adjudicate(
        item,
        parent_evidence,
        child_evidence,
        parent_present=parent_present,
        child_present=child_present,
        actionable=True if (parent_present is not None and child_present is not None) else None,
        modification_scope=scope,
        evidence_ref=ref,
    )
    result["actionability_basis"] = ACTIONABILITY_BASIS
    result["defect_class"] = defect_class
    return contract.validate_result(result)


__all__ = [
    "ACTIONABILITY_BASIS",
    "DEFAULT_PROGRESSION_TOL",
    "SUPPORTED_DEFECT_CLASSES",
    "adjudicate",
    "defect_families",
    "defect_present",
    "episode_dead_end",
]
