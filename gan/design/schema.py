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
    # task agent: prompt + selected skills + memory strategy + free params
    "task": {
        "prompt": str,
        "skills": list,          # list[str]: names from the task component registry
        "memory": object,        # str | None
        "params": dict,
    },
    # planner
    "planner": {
        "prompt": str,
        "memory": object,
        "params": dict,
    },
    # evaluator: prompt + selected eval points + memory + params
    "evaluator": {
        "prompt": str,
        "eval_points": list,     # list[str]: names from the evaluator registry
        "memory": object,
        "params": dict,
    },
}

_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "task": {"prompt": "You are an agent.", "skills": [], "memory": None, "params": {}},
    "planner": {"prompt": "You are a planner that improves a task agent.", "memory": None,
                "params": {}},
    "evaluator": {"prompt": "You are an evaluator that finds benchmark-invisible problems.",
                  "eval_points": [], "memory": None, "params": {}},
}


def default_config(role: str) -> Dict[str, Any]:
    if role not in _DEFAULTS:
        raise KeyError(f"unknown role: {role}")
    return copy.deepcopy(_DEFAULTS[role])


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
