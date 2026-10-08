"""Model registry (FRAMEWORK, frozen).

The ONLY reader of ``gan/framework/models.yaml``. Pure role-key lookup:
no environment variables, no fallback model, no precedence chain. Callers pick a
role key (e.g. ``gan.task``, ``dgmh.meta``) and pass the resolved value down
explicitly at runtime.

Sections:
  gan.<task|planner|evaluator>
  dgmh.<meta|task>
  domains.<role>            (domain-specific, non-task roles)

Entries are either a plain model STRING or a mapping with a required ``model``
key plus optional call parameters (``reasoning_effort``). ``resolve()`` returns
the model STRING in both shapes, so every existing consumer (run/driver model
dicts, harness CLI, proxy scopes, ``Node.meta["model"]``) is unaffected;
``resolve_entry()`` returns the full normalized entry with validation. Unknown
mapping keys and unknown ``reasoning_effort`` values fail loudly here — before
a run starts, not as a mid-run gateway 400.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, List

from gan.framework.loader import Config

SECTIONS = ("gan", "dgmh", "domains")

# Mapping entries may only carry these keys besides the required ``model``.
_ENTRY_KEYS = ("model", "reasoning_effort")


@lru_cache(maxsize=1)
def _config() -> Config:
    return Config.from_yaml(os.path.join(os.path.dirname(__file__), "models.yaml"))


def _entry(key: str, full: bool) -> Any:
    """Normalized entry (``full=True``) or the bare model string."""
    val = _config().get(key)
    if not val:
        raise KeyError(f"model role not configured in models.yaml: {key}")
    if isinstance(val, str):
        return {"model": val} if full else val
    if not isinstance(val, dict):
        raise ValueError(f"models.yaml[{key}]: entry must be a model string "
                         f"or a mapping, got {type(val).__name__}")
    model = val.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"models.yaml[{key}]: mapping entry requires a "
                         f"non-empty 'model' key")
    unknown = sorted(set(val) - set(_ENTRY_KEYS))
    if unknown:
        raise ValueError(f"models.yaml[{key}]: unknown entry key(s) {unknown}; "
                         f"allowed: {list(_ENTRY_KEYS)}")
    if not full:
        return model
    entry: Dict[str, Any] = {"model": model}
    effort = val.get("reasoning_effort")
    if effort is not None:
        # Base layer owns the call contract (agent/llm.py applies the value);
        # import the STDLIB-ONLY contract module, not agent/llm.py (whose
        # litellm import is unsafe for restricted-uid importers), and keep it
        # lazy so string-only consumers stay cheap.
        from agent.llm_params import REASONING_EFFORTS
        effort = str(effort)
        if effort not in REASONING_EFFORTS:
            raise ValueError(f"models.yaml[{key}]: reasoning_effort "
                             f"{effort!r} not in {list(REASONING_EFFORTS)}")
        entry["reasoning_effort"] = effort
    return entry


def resolve(key: str) -> str:
    """Resolve a dotted role key to its model STRING; raise if not configured."""
    return _entry(key, full=False)


def resolve_entry(key: str) -> Dict[str, Any]:
    """Resolve a role key to the normalized entry: ``model`` (+ call params).

    Returns ``{"model": <str>}`` or ``{"model": <str>,
    "reasoning_effort": <str>}``.
    """
    return _entry(key, full=True)


def resolve_section(section: str) -> Dict[str, Any]:
    """All raw entries under a section, e.g. ``gan`` -> {task, planner, evaluator}."""
    data = _config().get(section) or {}
    if not isinstance(data, dict):
        raise KeyError(f"model section not configured: {section}")
    return {k: v for k, v in data.items()}


def describe(keys: List[str] | None = None) -> Dict[str, Dict[str, Any]]:
    """Audit snapshot; used for explicit runtime recording (model + call params)."""
    if keys is None:
        keys = [f"{s}.{r}" for s in SECTIONS for r in resolve_section(s)]
    return {k: resolve_entry(k) for k in keys}
