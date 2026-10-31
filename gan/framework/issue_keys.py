"""Closed vocabulary for cross-round issue identity (FRAMEWORK, frozen).

Why a second identifier
-----------------------
``issue_id`` is a PER-ROUND handle: it names "the 2nd issue of this round" and
is renumbered whenever a round has no parent issue list to continue (a fresh
outer, a branch off ``initial``), so it cannot answer "is this the same defect
we raised three outers ago". Measured on the 2026-10 pilot/A6 runs, the SAME
defect was re-raised under different ids -- the cost problem appeared ten times
in A0 under ``i-2, i-4, i-5, i-4, i-1, i-3, i-6, i-1, i-1, i-1``, and the
evaluator-seat access bug three times in A6 under ``i-2, i-4, i-2``. Nothing in
the data could join those, so every outer "re-discovered" them.

``issue_key`` is that join. It is deliberately a CLOSED vocabulary, not a free
slug:

* a free slug can be re-spelled every round, which erases exactly the
  continuity the key exists to provide;
* a closed set is machine-checkable (unknown -> refused, the same discipline the
  reserved cost keys use) and lets the ledger aggregate across rounds, outers,
  runs and ablation arms;
* the fixed shape is also what makes ````carried_over```` a bounded prompt block
  (one line per key) instead of a growing transcript.

``owner`` routes the key to whoever can actually act on it:

* ``planner``   -- the design surface (prompt/config/components) can fix it.
* ``evaluator`` -- the evaluator's own behaviour (calibration, issue quality).
* ``framework`` -- a harness/access/session defect. The planner CANNOT fix
  these; routing them to it burns ``respond_issue`` budget and produces
  ``out_of_scope`` stances (observed verbatim in A6: "根源在文件访问/路径解析层，
  非 planner 的 prompt/config 算子可直接修复"). Framework-owned keys are
  recorded in the ledger but never carried into the planner's context.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ``other.new`` is the documented escape hatch: a real defect the vocabulary
# does not cover yet. It is a key like any other (so the ledger still counts
# it), but the loop logs it, because a recurring ``other.new`` is the signal
# that the vocabulary needs a new entry -- decided by a human, never by the
# agent (letting the agent mint keys is the free-slug failure mode again).
OTHER_KEY = "other.new"

OWNER_PLANNER = "planner"
OWNER_EVALUATOR = "evaluator"
OWNER_FRAMEWORK = "framework"

# key -> (owner, one-line meaning). Order is the prompt rendering order.
ISSUE_KEYS: Dict[str, Tuple[str, str]] = {
    # -- cost / process efficiency -----------------------------------------
    "cost.tokens": (OWNER_PLANNER,
                    "token spend far above what the task needs"),
    "cost.truncation": (OWNER_PLANNER,
                        "budget/tool-call cap exhausted, run cut off mid-work"),
    "process.dead-end": (OWNER_PLANNER,
                         "repeated calls / loops never backtracked"),
    "process.observability": (OWNER_FRAMEWORK,
                              "evaluator cannot see the task's turns/tool calls"),
    # -- task decision quality ---------------------------------------------
    "policy.accept-bias": (OWNER_PLANNER,
                           "predictions skew to the accept label"),
    "policy.reject-bias": (OWNER_PLANNER,
                           "predictions skew to the reject label"),
    "policy.constant-output": (OWNER_PLANNER,
                               "the same label/logic applied to every sample"),
    "policy.under-discrimination": (OWNER_PLANNER,
                                    "explicit reject signals present yet not acted on"),
    "policy.overcorrection": (OWNER_PLANNER,
                              "a fix overshot and inverted the decision"),
    # -- design / assembly -------------------------------------------------
    "design.toolset-mismatch": (OWNER_PLANNER,
                                "assembled tools do not serve this task"),
    "design.prompt-ineffective": (OWNER_PLANNER,
                                  "prompt/knowledge edit produced no behaviour change"),
    "design.contract-format": (OWNER_PLANNER,
                               "output contract violated (prose instead of the label)"),
    # -- evaluator's own behaviour -----------------------------------------
    "calibration.under-prediction": (OWNER_EVALUATOR,
                                     "blind score systematically below actual"),
    "calibration.over-prediction": (OWNER_EVALUATOR,
                                    "blind score systematically above actual"),
    "generalization.overfit": (OWNER_PLANNER,
                               "improvement fits the sample draw, not the task"),
    # -- framework-side ----------------------------------------------------
    "access.grant-broken": (OWNER_FRAMEWORK,
                            "a granted source path is not readable in the workspace"),
    "harness.infra": (OWNER_FRAMEWORK,
                      "gating/path/seat failure in the harness itself"),
    OTHER_KEY: (OWNER_PLANNER,
                "a real defect outside this vocabulary (log it: the set needs an entry)"),
}

KEY_NAMES: Tuple[str, ...] = tuple(ISSUE_KEYS)
MAX_KEY_CHARS = 40


def owner_of(key: object) -> str:
    """Who can act on ``key`` (``framework`` for unknown keys: never the planner)."""
    entry = ISSUE_KEYS.get(str(key or "").strip())
    return entry[0] if entry else OWNER_FRAMEWORK


def validate_issue_key(key: object) -> Optional[str]:
    """Return an error string for an illegal key, else ``None``."""
    if not isinstance(key, str):
        return f"issue_key must be a string (got {type(key).__name__})"
    k = key.strip()
    if not k:
        return "issue_key is required"
    if len(k) > MAX_KEY_CHARS:
        return f"issue_key exceeds {MAX_KEY_CHARS} chars (got {len(k)})"
    if k not in ISSUE_KEYS:
        return (f"issue_key '{k}' is not in the vocabulary; pick one of: "
                f"{', '.join(KEY_NAMES)}")
    return None


def normalize_issue_key(key: object) -> str:
    """The key to record: the validated value, or ``other.new`` (never dropped).

    A defect is never discarded because its key was mistyped -- that would lose
    the finding. It lands under the escape hatch instead, which the loop logs.
    """
    return key.strip() if validate_issue_key(key) is None else OTHER_KEY


def vocabulary_lines(owners: Optional[Tuple[str, ...]] = None) -> List[str]:
    """``- key (owner): meaning`` lines, optionally filtered by owner."""
    out: List[str] = []
    for key, (owner, meaning) in ISSUE_KEYS.items():
        if owners is not None and owner not in owners:
            continue
        out.append(f"- `{key}` ({owner}): {meaning}")
    return out


def vocabulary_text(owners: Optional[Tuple[str, ...]] = None) -> str:
    return "\n".join(vocabulary_lines(owners))
