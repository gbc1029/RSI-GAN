"""Unified model resolution (FRAMEWORK, frozen).

The ONLY place that reads ``gan/framework/models.yaml`` and ``GAN_MODEL_*``
environment variables. All callers (GAN: build/loop/task_runner; DGM-H:
scripts/dgmh) go through here, so the default / override precedence can never
diverge and model selection stays inside the frozen trust anchor.

Precedence for a key:
    explicit(arg) > env(GAN_MODEL_<KEY>) > models[<key>] > models.task
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional

from gan.framework.loader import Config

# Backward-compatible env aliases (first match wins).
_ENV_ALIASES: Dict[str, List[str]] = {
    "task": ["GAN_MODEL_TASK", "GAN_TASK_MODEL"],
    "planner": ["GAN_MODEL_PLANNER"],
    "evaluator": ["GAN_MODEL_EVALUATOR"],
    "meta": ["GAN_MODEL_META", "GAN_META_MODEL"],
    "polyglot_meta": ["GAN_MODEL_POLYGLOT_META"],
    "task_agent": ["GAN_MODEL_TASK_AGENT"],
}


@dataclass(frozen=True)
class ModelChoice:
    key: str
    model: Optional[str]
    source: str  # explicit | env:<NAME> | models.yaml | models.task | none


@lru_cache(maxsize=1)
def _config() -> Config:
    return Config.from_yaml(os.path.join(os.path.dirname(__file__), "models.yaml"))


def _raw(key: str) -> Optional[str]:
    val = _config().get(f"models.{key}")
    if val is None and key != "task":
        val = _config().get("models.task")
    return val


def _env_value(key: str):
    for name in _ENV_ALIASES.get(key, [f"GAN_MODEL_{key.upper()}"]):
        val = os.environ.get(name)
        if val:
            return name, val
    return None, None


def resolve(key: str, explicit: Optional[str] = None, domain: Optional[str] = None) -> ModelChoice:
    """Resolve the model for ``key`` with a fixed precedence."""
    if explicit:
        return ModelChoice(key, explicit, "explicit")
    env_name, env_val = _env_value(key)
    if env_val:
        return ModelChoice(key, env_val, f"env:{env_name}")
    # per-domain default (task only), applied before the generic task default
    if key == "task" and domain:
        domain_override = _config().get(f"domains.{domain}")
        if domain_override:
            return ModelChoice(key, domain_override, f"domains.{domain}")
    raw = _raw(key)
    if raw:
        source = "models.yaml" if _config().get(f"models.{key}") is not None else "models.task"
        return ModelChoice(key, raw, source)
    return ModelChoice(key, None, "none")


def resolve_all(
    keys: Optional[List[str]] = None,
    explicit: Optional[Dict[str, Optional[str]]] = None,
    domain: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    keys = keys or ["task", "planner", "evaluator"]
    explicit = explicit or {}
    return {k: resolve(k, explicit=explicit.get(k), domain=domain).model for k in keys}


def fallback() -> Optional[str]:
    return _config().get("fallback")


def env_name(key: str) -> str:
    return _ENV_ALIASES.get(key, [f"GAN_MODEL_{key.upper()}"])[0]


def describe(keys: Optional[List[str]] = None, explicit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Audit dict: {key: {model, source}} + fallback. Written to events.jsonl."""
    keys = keys or ["task", "planner", "evaluator"]
    explicit = explicit or {}
    return {
        "models": {k: resolve(k, explicit=explicit.get(k)).__dict__ for k in keys},
        "fallback": fallback(),
    }
