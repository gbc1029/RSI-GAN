"""Per-role design schemas (minimal, code-defined).

The schema is intentionally TINY: it is the shallow surface's shape. Growing the
schema (adding a key) is a deep/source-level change. Shallow operators may only
set values for keys that already exist here.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Set

# type markers are documentation only (not strictly enforced)
ROLE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # every role: prompt + selected tools + free params (batch 6 unification:
    # one ``tools`` slot per role; the former ``skills``/``eval_points`` slots are
    # legacy vocabulary for the same thing and are normalized on load)
    "task": {
        "prompt": str,
        "tools": list,           # list[str]: names from the task registry
        "params": dict,
    },
    "planner": {
        "prompt": str,
        "tools": list,           # planner capability tools (empty until populated)
        "params": dict,
    },
    "evaluator": {
        "prompt": str,
        "tools": list,           # list[str]: names from the evaluator registry
        "params": dict,
    },
}

_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "task": {"prompt": "You are an agent.", "tools": [], "params": {}},
    "planner": {"prompt": "You are a planner that improves a task agent.", "tools": [], "params": {}},
    "evaluator": {"prompt": "You are an evaluator that finds benchmark-invisible problems.",
                  "tools": [], "params": {}},
}

# legacy design-config slot names -> the unified ``tools`` slot (batch 6).
# Normalization runs on every load/restore so an older run's checkpoint cannot
# silently lose its selected tools after the vocabulary rename.
_LEGACY_SLOTS = {"skills": "tools", "eval_points": "tools"}


def default_config(role: str) -> Dict[str, Any]:
    if role not in _DEFAULTS:
        raise KeyError(f"unknown role: {role}")
    return copy.deepcopy(_DEFAULTS[role])


def normalize_config(config: Any) -> Dict[str, Any]:
    """Fold legacy slot names into the unified ``tools`` slot (batch 6).

    Runs on every design load/restore: an older run's checkpoint carries
    ``skills`` / ``eval_points``; without this fold the renamed vocabulary would
    silently drop its selected tools (the exact failure class the review kills).
    Merges multiple legacy sources in encounter order, drops the legacy keys, and
    passes through everything else unchanged. A ``tools`` key present in the
    config wins over legacy keys only if it is a non-empty list.
    """
    if not isinstance(config, dict):
        return config
    legacy: List[Any] = []
    for k in ("skills", "eval_points"):
        v = config.get(k)
        if isinstance(v, (list, tuple)):
            legacy.extend(str(x) for x in v)
    if not legacy:
        return config
    out = {k: v for k, v in config.items() if k not in _LEGACY_SLOTS}
    cur = out.get("tools")
    merged = [str(x) for x in cur] if isinstance(cur, (list, tuple)) else []
    for x in legacy:
        if x not in merged:
            merged.append(x)
    out["tools"] = merged
    return out


def allowed_keys(role: str) -> Set[str]:
    return set(ROLE_SCHEMAS.get(role, {}).keys())


def validate_config(role: str, config: Dict[str, Any]) -> List[str]:
    """Return a list of problems (unknown keys are 'deep' changes, not allowed here)."""
    problems: List[str] = []
    known = allowed_keys(role)
    for k in config.keys():
        if k not in known:
            problems.append(
                f"unknown key '{k}' for role '{role}' (adding new keys requires a source-level change)"
            )
    return problems
