"""Model registry (FRAMEWORK, frozen).

The ONLY reader of ``gan/framework/models.yaml``. Pure role-key lookup:
no environment variables, no fallback model, no precedence chain. Callers pick a
role key (e.g. ``gan.task``, ``dgmh.meta``) and pass the resolved value down
explicitly at runtime.

Sections:
  gan.<task|planner|evaluator>
  dgmh.<meta|task>
  domains.<role>            (domain-specific, non-task roles)
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Dict, List

from gan.framework.loader import Config

SECTIONS = ("gan", "dgmh", "domains")


@lru_cache(maxsize=1)
def _config() -> Config:
    return Config.from_yaml(os.path.join(os.path.dirname(__file__), "models.yaml"))


def resolve(key: str) -> str:
    """Resolve a dotted role key (e.g. ``gan.task``); raise if not configured."""
    val = _config().get(key)
    if not val:
        raise KeyError(f"model role not configured in models.yaml: {key}")
    return val


def resolve_section(section: str) -> Dict[str, str]:
    """All role keys under a section, e.g. ``gan`` -> {task, planner, evaluator}."""
    data = _config().get(section) or {}
    if not isinstance(data, dict):
        raise KeyError(f"model section not configured: {section}")
    return {k: v for k, v in data.items()}


def describe(keys: List[str] | None = None) -> Dict[str, str]:
    """Audit snapshot; used for explicit runtime recording."""
    if keys is None:
        keys = [f"{s}.{r}" for s in SECTIONS for r in resolve_section(s)]
    return {k: resolve(k) for k in keys}
