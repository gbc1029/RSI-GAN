"""Design layer: the SHALLOW, evolvable design surface.

- ``schema.py``   : minimal per-role config schema (code-defined shape).
- ``store.py``    : read/write ``design/<role|task-node>/config.json``.
- ``seeds/``      : generation-0 prompts for ALL roles (via ``initial_config``).

The design layer holds **data** (config/selection/params), not implementations;
component implementations live in ``gan/components`` and are declared in
``gan/registries``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

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


def initial_config(role: str) -> Dict[str, Any]:
    """Generation-0 design config for ``role`` — the single entry point.

    ``schema.default_config`` (code-defined shape/defaults) + ``seeds/<role>.md``
    (seed prompt). Both the planner/evaluator self-design seeding
    (``gan/build.py``) and the task agent's initial config
    (``gan/framework/loop.py``) MUST go through here so the three roles cannot
    drift apart again (the task role used to skip its seed entirely).

    The seed is applied only when non-empty after ``strip()``: a missing or blank
    seed file must never yield an empty system prompt
    (``base_role.current_prompt()`` would otherwise silently return ``""``).

    Known residual: ``store.DesignStore.load`` still falls back to
    ``schema.default_config`` (not this function) when a design file is missing —
    importing this module from ``store.py`` would be circular. That fallback only
    fires for a missing file, which ``_seed_self_designs`` prevents.
    """
    cfg = default_config(role)
    seed = load_seed(role)
    if seed.strip():
        cfg["prompt"] = seed
    return cfg
