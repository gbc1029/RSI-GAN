"""Per-role component registries (code + registry.json).

A component is an entry ``{name, kind, module, description, params_schema}`` where
``module`` is a path relative to ``gan/components``. The design config selects
components by name. Shared components live in ``registries/shared.json`` and are
merged into every role's registry.

Registering/modifying a component is a source-level ("deep") change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from gan.framework.loader import config_dir

_GAN_DIR = config_dir().parent
REGISTRY_DIR = _GAN_DIR / "registries"
COMPONENTS_DIR = _GAN_DIR / "components"
KINDS = ("skill", "memory", "eval_point")


class ComponentRegistry:
    def __init__(self, role: str, entries: List[Dict[str, Any]], components_dir: Path):
        self.role = role
        self.entries = entries or []
        self.components_dir = Path(components_dir)

    def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self.entries if kind is None or e.get("kind") == kind]

    def names(self, kind: Optional[str] = None) -> List[str]:
        return [e["name"] for e in self.list(kind)]

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        for e in self.entries:
            if e.get("kind") == kind and e.get("name") == name:
                return e
        return None

    def has(self, kind: str, name: str) -> bool:
        return self.get(kind, name) is not None

    def module_path(self, kind: str, name: str) -> Optional[str]:
        e = self.get(kind, name)
        if not e:
            return None
        return str(self.components_dir / e["module"])


def _read_entries(path: Path) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return (json.load(f) or {}).get("components", [])


def load_registry_for_role(role: str) -> ComponentRegistry:
    """Merge shared registry with the role-specific registry."""
    entries = _read_entries(REGISTRY_DIR / "shared.json") + _read_entries(REGISTRY_DIR / f"{role}.json")
    return ComponentRegistry(role, entries, COMPONENTS_DIR)
