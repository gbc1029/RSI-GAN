"""Design layer: the SHALLOW, evolvable design surface.

- ``schema.py``   : minimal per-role config schema (code-defined shape).
- ``store.py``    : read/write ``design/<role|task-node>/config.json``.
- ``seeds/``      : seed prompts (initial design).

The design layer holds **data** (config/selection/params), not implementations;
component implementations live in ``gan/components`` and are declared in
``gan/registries``.
"""
from __future__ import annotations

from pathlib import Path

from gan.design.schema import (  # noqa: F401
    ROLE_SCHEMAS,
    allowed_keys,
    default_config,
    validate_config,
)
from gan.design.store import DesignStore  # noqa: F401

_SEEDS_DIR = Path(__file__).resolve().parent / "seeds"


def load_seed(role: str) -> str:
    """Load the seed prompt for a role (task/planner/evaluator)."""
    p = _SEEDS_DIR / f"{role}.md"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return ""
