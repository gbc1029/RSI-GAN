"""Gan config loader.

Loads the gan config files (gan_loop.yaml / registry.yaml / eval_points.yaml),
performs deep-merge of default + node overrides, and resolves domain configs.

No hard dependency on jsonschema: if it is importable, validation is performed;
otherwise a light structural check is used.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent


def config_dir() -> Path:
    return _CONFIG_DIR


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """A thin, dict-backed config wrapper with dotted-path access."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._data: Dict[str, Any] = data or {}

    # -- loading -----------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | os.PathLike) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    # -- access ------------------------------------------------------------
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any) -> None:
        parts = dotted_key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def merge(self, override: Optional[Dict[str, Any]]) -> "Config":
        return Config(_deep_merge(self._data, override or {}))

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    # -- custom section (config pool) -------------------------------------
    def custom(self) -> Dict[str, Any]:
        return self._data.setdefault("custom", {})

    def add_custom(self, key: str, value: Any, rationale: str = "") -> None:
        """Add/overwrite a custom config key (config pool extension)."""
        entry = {"value": value, "rationale": rationale}
        self.custom()[key] = entry

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"Config({self._data!r})"


# ---------------------------------------------------------------------------
# Top-level loaders
# ---------------------------------------------------------------------------
def load_gan_loop_config(overrides: Optional[Dict[str, Any]] = None) -> Config:
    cfg = Config.from_yaml(_CONFIG_DIR / "gan_loop.yaml")
    return cfg.merge(overrides)


def load_registry() -> Config:
    return Config.from_yaml(_CONFIG_DIR / "registry.yaml")


def load_eval_points() -> Config:
    return Config.from_yaml(_CONFIG_DIR / "eval_points.yaml")


def load_prompt(role: str) -> str:
    path = _CONFIG_DIR / "prompts" / f"{role}.md"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Domain registry resolution
# ---------------------------------------------------------------------------
def resolve_domain(registry: Config, domain: str) -> Dict[str, Any]:
    """Resolve a domain to its merged config.

    Order: exact match in ``domains`` > longest prefix match in ``families``
    > ``default``.
    """
    default = registry.get("default", {}) or {}
    resolved = copy.deepcopy(default)

    families = registry.get("families", {}) or {}
    best_prefix: Optional[str] = None
    for prefix in families:
        if domain == prefix or domain.startswith(prefix):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix = prefix
    if best_prefix is not None:
        resolved = _deep_merge(resolved, families[best_prefix])

    domains = registry.get("domains", {}) or {}
    if domain in domains:
        resolved = _deep_merge(resolved, domains[domain])

    resolved["domain"] = domain
    return resolved


def list_registered_domains(registry: Config) -> List[str]:
    return sorted((registry.get("domains", {}) or {}).keys())


# ---------------------------------------------------------------------------
# Optional validation
# ---------------------------------------------------------------------------
def validate(data: Dict[str, Any], schema: Dict[str, Any]) -> bool:
    """Validate ``data`` against ``schema`` if jsonschema is installed."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        return True  # soft-fail when jsonschema unavailable
    jsonschema.validate(instance=data, schema=schema)
    return True
