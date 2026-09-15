"""Compose an agent's effective spec from its design config + registry."""
from __future__ import annotations

from typing import Any, Dict, List

from gan.registries.loader import ComponentRegistry


def _resolve(registry: ComponentRegistry, kind: str, names: List[str]) -> List[str]:
    out = []
    for name in names or []:
        p = registry.module_path(kind, name)
        if p:
            out.append(p)
    return out


def compose_task_design(config: Dict[str, Any], registry: ComponentRegistry) -> Dict[str, Any]:
    return {
        "prompt": config.get("prompt", ""),
        "skill_names": list(config.get("skills") or []),
        "skill_modules": _resolve(registry, "skill", list(config.get("skills") or [])),
        "memory": config.get("memory"),
        "params": dict(config.get("params") or {}),
    }


def compose_role_design(config: Dict[str, Any], registry: ComponentRegistry) -> Dict[str, Any]:
    return {
        "prompt": config.get("prompt", ""),
        "eval_point_names": list(config.get("eval_points") or []),
        "memory": config.get("memory"),
        "params": dict(config.get("params") or {}),
    }
