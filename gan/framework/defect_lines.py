"""Per-domain defect lines (docs/21 §3.3).

WHY THIS MODULE EXISTS
----------------------
The SAME defect ("this episode is cycling / never interacts / only bumps the
forward action") is measured by TWO consumers:

* ``gan/framework/issue_checks._chk_dead_end`` -- the ORDINARY issue channel,
  which aggregates per-generation ("how many of the traced episodes are
  degenerate") and feeds ``verdict_stability``;
* a domain challenge adjudicator (docs/21) -- which asks the question about ONE
  named episode.

Before this module the per-episode line lived in ``domains/balrog/utils.py``
(``DEGENERATE_REVISIT_SHARE = 0.80``) and was MIRRORED into ``loop.yaml`` for
display only. Two copies of a threshold that decides "is the defect fixed" is
exactly how the same defect ends up judged two different ways, so the line is
now declared ONCE -- in the domain's own ``domains.yaml``
``output_contract.defect_lines`` -- and this module is the single reader.

Design constraints that shaped it:

* **Domain knowledge stays in the domain registry**, not in framework code: the
  key names below are a closed vocabulary, but their VALUES are per-domain
  declarations (babyai and babaisai do not share an action vocabulary).
* **Absence means "not declared", never 0**: a domain that has not adopted the
  field must behave byte-identically to before, so ``lines_for()`` returns an
  empty mapping and every consumer falls back to its own historical default
  (which is stated in the caller, not duplicated here).
* **Fail-closed on a malformed declaration**: a line that is not a number in
  [0, 1] is a configuration bug, and silently clamping it would change a
  verdict, so it raises instead.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# The closed vocabulary of defect lines a domain may declare. A key outside this
# set is a typo that would otherwise be silently ignored (the defect it was
# meant to bound would simply never fire).
_DEFECT_LINE_KEYS = frozenset({
    # per-episode: share of the episode's steps that re-try an already-tried
    # (state, action) pair (docs/20 §4.2 -- the CYCLE detector)
    "cycle_degenerate_revisit_share",
    # per-episode: share of the episode's steps that were a ``go forward``
    # which changed nothing observable (blocked into a wall/object)
    "blocked_forward_share",
    # per-episode: an interaction action (pick up / drop / toggle) was never
    # attempted at all -- an ABSOLUTE count, not a share
    "no_interaction_max_steps",
})


def declared(contract: Optional[Mapping[str, Any]]) -> Dict[str, float]:
    """The domain's ``output_contract.defect_lines``, validated.

    Returns ``{}`` when the domain declares nothing (question domains and any
    domain that has not adopted the field). Raises ``RuntimeError`` for an
    unknown key or a value outside [0, 1], because either one silently changes
    what counts as a defect.
    """
    contract = contract or {}
    lines = contract.get("defect_lines")
    if not lines:
        return {}
    if not isinstance(lines, Mapping):
        raise RuntimeError("domains.yaml defect_lines must be a mapping")
    out: Dict[str, float] = {}
    for raw_key, raw_value in lines.items():
        key = str(raw_key)
        if key not in _DEFECT_LINE_KEYS:
            raise RuntimeError(
                f"domains.yaml defect_lines has an unknown key {key!r}; "
                f"allowed: {sorted(_DEFECT_LINE_KEYS)}")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            raise RuntimeError(
                f"domains.yaml defect_lines.{key} must be a number (got {raw_value!r})")
        if not 0.0 <= value <= 1.0:
            raise RuntimeError(
                f"domains.yaml defect_lines.{key} must be within [0, 1] (got {value})")
        out[key] = value
    return out


def get(contract: Optional[Mapping[str, Any]], key: str,
        default: Optional[float] = None) -> Optional[float]:
    """One declared line, or ``default`` when the domain did not declare it."""
    return declared(contract).get(key, default)


__all__ = ["declared", "get"]
