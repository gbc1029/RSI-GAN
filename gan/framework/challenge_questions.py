"""Challenge admission: the A layer (proposal legality) and the B layer's hook.

THREE LAYERS, and this module owns the first two (docs/21 §6):

* **A -- mechanical legality**: K budget, defect class in the domain's closed
  vocabulary, ``family`` must be a task where the defect ACTUALLY appeared in the
  parent generation, no duplicate ``(defect_class, family)``, no intersection with
  the frozen anchor / evaluation sets.
* **B -- solvability / non-degeneracy**: delegated to the domain's
  ``challenge_space`` (``materialize`` + ``validate_episode``): the instance must
  be solvable by the optimal policy inside the family's declared difficulty band.
* **C -- truth**: NOT here. The mechanical adjudicator owns it, after the child
  played the instance.

Why the split is not cosmetic (and must not be re-merged): a wrong ADMISSION and a
wrong VERDICT are indistinguishable once mixed -- the IMO pilot's ``actionable``
flag was 72/72 ``true`` exactly because admission never actually refused anything,
so "the challenge was unfair" and "the defect was misjudged" could not be told
apart. Keeping admission mechanical and pre-play makes a refusal auditable as its
own event.

Everything here runs on the PARENT generation's own report + the framework's
declared config: no model input beyond the four proposal fields, and no
post-reveal information (the ledger's blind lock is the second gate).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from gan.framework import challenge_adjudication as adjudication
from gan.framework import defect_lines as dl

#: fields the model may submit (docs/21 §4). Anything else is refused rather than
#: silently ignored: an extra field is how a revealed key would sneak in.
PROPOSAL_FIELDS = ("defect_class", "family", "evidence_ref", "repair_criterion")

#: OPTIONAL link fields (docs/21 §19, 2026-11-22). A challenge should be traceable
#: to the issue that recorded the finding it tests; with no link the two channels
#: can only be compared as unrelated sample sets. Kept OUT of ``PROPOSAL_FIELDS``
#: so the four author-supplied fields stay exactly as before and an OLD caller
#: (or an offline fixture) is still valid -- but permitted, so the loop can carry
#: them into the ledger.
LINK_FIELDS = ("issue_id", "issue_key")


def challenge_limits(cfg, domain):
    """Preserve the two existing domain defaults; explicit run values win."""
    proof = domain == "imo_proof"
    k = cfg.get("challenge.max_k")
    random_min = cfg.get("challenge.random_min")
    return (int(k) if k is not None else (5 if proof else 2),
            int(random_min) if random_min is not None else (10 if proof else 1))


class ChallengeAdmissionError(RuntimeError):
    """A proposal failed A or B. The caller logs ``challenge_rejected``."""


# ---------------------------------------------------------------------------
# config / domain hooks
# ---------------------------------------------------------------------------
def space_for(domain: str) -> Optional[Any]:
    """The domain's ``challenge_space`` module, or ``None`` (not authorable)."""
    return _submodule(domain, "challenge_space")


def _submodule(domain: str, name: str) -> Optional[Any]:
    import importlib

    key = str(domain or "")
    if not key:
        return None
    parts = key.split("_")
    for i in range(len(parts), 0, -1):
        pkg = "_".join(parts[:i])
        try:
            return importlib.import_module(f"domains.{pkg}.{name}")
        except Exception:  # noqa: BLE001 -- try the next prefix
            continue
    return None


def space_config(domain_cfg: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The domain's ``challenge_space`` declaration (``{}`` when absent)."""
    cfg = (domain_cfg or {}).get("challenge_space")
    return dict(cfg) if isinstance(cfg, Mapping) else {}


def family_params(domain_cfg: Optional[Mapping[str, Any]],
                  family: str) -> Optional[Dict[str, Any]]:
    """The declared band for ONE family, or ``None`` (that family cannot host one)."""
    params = space_config(domain_cfg).get("params") or {}
    if not isinstance(params, Mapping):
        return None
    row = params.get(str(family))
    return dict(row) if isinstance(row, Mapping) else None


def authorable(domain: str, domain_cfg: Optional[Mapping[str, Any]]
               ) -> Tuple[bool, str]:
    """Can this domain host a challenge at all? ``(ok, reason)``."""
    if not supported_classes(domain):
        return False, "no_adjudicable_defect_class"
    if space_for(domain) is None:
        return False, "no_challenge_space_module"
    cfg = space_config(domain_cfg)
    if not cfg:
        return False, "no_challenge_space_declared"
    if not isinstance(cfg.get("params"), Mapping) or not cfg.get("params"):
        return False, "no_family_band_declared"
    return True, ""


def supported_classes(domain: str) -> tuple:
    return adjudication.supported_classes_for(domain)


def citable_families(domain: str, defect_class: str,
                     report_rows: Iterable[Mapping[str, Any]],
                     contract: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Families where ``defect_class`` was OBSERVED in the given episode rows."""
    scan = adjudication.defect_scan_for(domain)
    if scan is None:
        return {}
    try:
        lines = dl.declared(dict(contract or {}))
    except RuntimeError as exc:
        raise ChallengeAdmissionError(
            f"defect_lines declaration is malformed: {exc}") from exc
    if not lines and str(defect_class) not in adjudication.classes_without_lines_for(domain):
        # Only the classes that READ a line need one; an absolute class stays
        # citable and adjudicable in a domain that declares no defect_lines.
        return {}
    out = scan(list(report_rows or ()), str(defect_class), lines)
    return dict(out or {})


# ---------------------------------------------------------------------------
# A layer
# ---------------------------------------------------------------------------
def parse_proposal(proposal: Mapping[str, Any]) -> Dict[str, str]:
    """Validate the SHAPE of one proposal (closed fields, non-empty strings).

    ``LINK_FIELDS`` are permitted and copied through verbatim (``issue_id`` may be
    empty = unlinked); everything else outside ``PROPOSAL_FIELDS`` is refused.
    """
    if not isinstance(proposal, Mapping):
        raise ChallengeAdmissionError("a challenge proposal must be an object")
    extra = {str(k) for k in proposal} - set(PROPOSAL_FIELDS) - set(LINK_FIELDS)
    if extra:
        raise ChallengeAdmissionError(
            "proposal carries undeclared field(s): " + ", ".join(sorted(extra)))
    out: Dict[str, str] = {}
    for field in PROPOSAL_FIELDS:
        value = proposal.get(field)
        text = str(value).strip() if isinstance(value, (str, int, float)) else ""
        if not text:
            raise ChallengeAdmissionError(f"proposal field '{field}' is required")
        out[field] = text
    for field in LINK_FIELDS:
        value = proposal.get(field)
        out[field] = str(value).strip() if isinstance(value, (str, int, float)) else ""
    return out


def validate_admission(
    proposal: Mapping[str, Any],
    *,
    domain: str,
    used: int,
    max_k: int,
    existing: Sequence[Mapping[str, Any]] = (),
    citable: Optional[Mapping[str, Mapping[str, int]]] = None,
    forbidden_ids: Iterable[str] = (),
) -> Dict[str, str]:
    """A layer for ONE proposal; returns the normalized row or raises.

    ``citable`` is the per-family hit table from
    :func:`citable_families` -- a mapping, not a set, so the refusal can say HOW
    much evidence existed instead of a bare "not allowed".
    """
    row = parse_proposal(proposal)
    if int(max_k) < 1:
        raise ChallengeAdmissionError("challenge authoring is disabled (max_k < 1)")
    if int(used) >= int(max_k):
        raise ChallengeAdmissionError(
            f"challenge budget exhausted (max_k={int(max_k)} already used)")
    classes = supported_classes(domain)
    if row["defect_class"] not in classes:
        raise ChallengeAdmissionError(
            f"defect_class {row['defect_class']!r} is not adjudicable for {domain}; "
            f"allowed: {', '.join(sorted(classes)) or '(none)'}")
    table = citable or {}
    seen = table.get(row["family"])
    if not seen or not int(seen.get("hits") or 0):
        detail = (f"(decided={int(seen.get('decided') or 0)}, hits=0)" if seen
                  else "(family not present in this generation's report)")
        raise ChallengeAdmissionError(
            f"family {row['family']!r} did not show {row['defect_class']} in the "
            f"generation being evaluated {detail}")
    key = (row["defect_class"], row["family"])
    for item in existing or ():
        if (str(item.get("defect_class")), str(item.get("family"))) == key:
            raise ChallengeAdmissionError(
                "duplicate challenge for "
                f"({row['defect_class']}, {row['family']})")
    # The framework picks the instance, but a caller that already knows the seed
    # space still gets a loud refusal rather than a silent overlap with the
    # frozen anchor/eval core.
    if str(row["evidence_ref"]) in {str(x) for x in (forbidden_ids or ())}:
        raise ChallengeAdmissionError(
            "evidence_ref names a frozen evaluation id; the challenge must cite the "
            "generation it was raised in")
    return row


# ---------------------------------------------------------------------------
# B layer (delegated to the domain generator)
# ---------------------------------------------------------------------------
def materialize_pair(proposal: Mapping[str, Any], *, domain: str,
                     domain_cfg: Optional[Mapping[str, Any]],
                     base_seed: int,
                     exclude_seeds: Iterable[int] = (),
                     exclude_fingerprints: Iterable[str] = (),
                     source_id: Optional[str] = None) -> Dict[str, Any]:
    """Draw the same-family instance PAIR for one admitted proposal.

    Every exclusion the framework knows about is fed in: the frozen anchor and
    evaluation seeds of the run, plus the fingerprints of instances already
    materialized for other challenges of this outer -- two challenges must not be
    answered by the same level.
    """
    space = space_for(domain)
    if space is None:
        raise ChallengeAdmissionError(f"domain {domain!r} has no challenge_space module")
    params = family_params(domain_cfg, str(proposal.get("family")))
    if params is None:
        raise ChallengeAdmissionError(
            f"family {proposal.get('family')!r} has no declared difficulty band")
    cfg = space_config(domain_cfg)
    if cfg.get("num_dists") is not None:
        params = {**params, "num_dists": cfg.get("num_dists")}
    max_attempts = int(cfg.get("max_attempts") or 32)
    try:
        return space.materialize(
            str(proposal.get("family")), base_seed=int(base_seed), params=params,
            exclude_seeds=list(exclude_seeds or ()),
            exclude_fingerprints=list(exclude_fingerprints or ()),
            **({"source_id": source_id} if source_id is not None else {}),
            max_attempts=max_attempts)
    except Exception as exc:  # noqa: BLE001 -- a generator refusal is an A/B refusal
        raise ChallengeAdmissionError(str(exc)) from exc


def batch_budget_check(*, k: int, num_samples: int, anchor_k: int,
                       random_min: int) -> None:
    """Startup check for the train batch (fail-closed, docs/21 §4.1)."""
    need = int(anchor_k) + int(k) + int(random_min)
    if int(k) < 0:
        raise ChallengeAdmissionError("challenge max_k must be >= 0")
    if need > int(num_samples):
        raise ChallengeAdmissionError(
            f"challenge budget does not fit the batch: anchor_k({anchor_k}) + "
            f"max_k({k}) + random_min({random_min}) = {need} > num_samples({num_samples}); "
            "raise num_samples or lower max_k (this is refused, never truncated)")


def lock_payload(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """The blind payload stored in the ledger (a JSON-able, ordered copy).

    ``LINK_FIELDS`` ride along so the lock is idempotent on the WHOLE proposal:
    a resumed round that re-locks with a different ``issue_id`` is a different
    claim and must be detected rather than silently accepted.
    """
    out = []
    for row in rows or ():
        out.append({field: str(row.get(field) or "")
                    for field in PROPOSAL_FIELDS + LINK_FIELDS})
    json.dumps(out)  # a payload that cannot be persisted must fail HERE
    return out


__all__ = [
    "PROPOSAL_FIELDS", "LINK_FIELDS", "ChallengeAdmissionError", "authorable",
    "batch_budget_check", "citable_families", "family_params", "lock_payload",
    "materialize_pair", "parse_proposal", "space_config", "space_for",
    "supported_classes", "validate_admission",
]
