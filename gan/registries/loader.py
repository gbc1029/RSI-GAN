"""Per-role component registries (code + registry.json).

A **valid** component entry is a dict with required fields per kind:
``name`` (non-empty str), ``kind`` in ``KINDS``, ``module`` (path relative to
``gan/components`` that exists). ``description`` / ``params_schema`` are optional.

Robustness (field-agnostic): malformed entries are **tolerated** at load time --
they are kept but marked invalid (with a reason) and are never usable
(selection/assembly/loading skip them). Nothing here raises on bad input; the
patch/commit layer is responsible for not *persisting* new invalidity.

Registering/modifying a component is a source-level ("deep") change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gan.framework.loader import config_dir

_GAN_DIR = config_dir().parent
REGISTRY_DIR = _GAN_DIR / "registries"
COMPONENTS_DIR = _GAN_DIR / "components"
KINDS = ("skill", "eval_point")
_REQUIRED = ("name", "kind", "module")


def parse_registry_file(path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Return (entries, None) or (None, error) if the file is unparseable.

    "Unparseable" = invalid JSON, or valid JSON that is not an object with a
    ``components`` list. Missing/empty file -> ([], None).
    """
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"invalid JSON: {e}"
    if data is None:
        return [], None
    if not isinstance(data, dict) or not isinstance(data.get("components", []), list):
        return None, "not an object with a 'components' list"
    return data["components"], None


def entry_reason(entry: Any, components_dir: Path) -> Optional[str]:
    """Return None if valid, else a short reason string."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    for f in _REQUIRED:
        if f not in entry or entry[f] in (None, ""):
            return f"missing required field '{f}'"
    if entry["kind"] not in KINDS:
        return f"unknown kind '{entry['kind']}'"
    mod = str(entry["module"]).replace("\\", "/")
    p = (components_dir / mod)
    if not p.is_file():
        return f"module file not found: {mod}"
    return None


class ComponentRegistry:
    def __init__(self, role: str, entries: List[Any], components_dir: Path):
        self.role = role
        self.entries = [e for e in (entries or [])]
        self.components_dir = Path(components_dir)

    # -- queries -----------------------------------------------------------
    def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self.entries if isinstance(e, dict) and (kind is None or e.get("kind") == kind)]

    def names(self, kind: Optional[str] = None) -> List[str]:
        return [e["name"] for e in self.list(kind)]

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        for e in self.entries:
            if isinstance(e, dict) and e.get("kind") == kind and e.get("name") == name:
                return e
        return None

    def has(self, kind: str, name: str) -> bool:
        return self.get(kind, name) is not None

    def reason(self, kind: str, name: str) -> Optional[str]:
        e = self.get(kind, name)
        if e is None:
            return "not registered"
        return entry_reason(e, self.components_dir)

    def is_valid(self, kind: str, name: str) -> bool:
        return self.reason(kind, name) is None

    def module_path(self, kind: str, name: str) -> Optional[str]:
        """Path to the component module, or None if the entry is invalid/missing."""
        e = self.get(kind, name)
        if e is None or entry_reason(e, self.components_dir) is not None:
            return None
        return str(self.components_dir / str(e["module"]))

    def invalid(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for e in self.entries:
            r = entry_reason(e, self.components_dir)
            if r is not None:
                nm = e.get("name") if isinstance(e, dict) else None
                kd = e.get("kind") if isinstance(e, dict) else None
                out.append({"kind": str(kd), "name": str(nm), "reason": r})
        return out


def load_registry_for_role(
    role: str,
    registry_dir: Optional[Path] = None,
    components_dir: Optional[Path] = None,
    tolerant: bool = True,
) -> ComponentRegistry:
    """Merge shared registry with the role-specific registry (tolerant by default)."""
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    shared, e1 = parse_registry_file(rdir / "shared.json")
    specific, e2 = parse_registry_file(rdir / f"{role}.json")
    if not tolerant and (e1 or e2):
        raise ValueError(f"unparseable registry: {e1 or e2}")
    entries = (shared or []) + (specific or [])
    return ComponentRegistry(role, entries, cdir)
