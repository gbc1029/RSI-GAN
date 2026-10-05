"""Per-role design schemas (minimal, code-defined + validated dynamic extensions)."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Optional

ROLE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "task": {"prompt": str, "tools": list, "params": dict},
    "planner": {"prompt": str, "tools": list, "params": dict},
    "evaluator": {"prompt": str, "tools": list, "params": dict},
}
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "task": {"prompt": "You are an agent.", "tools": [], "params": {}},
    "planner": {"prompt": "You are a planner that improves a task agent.", "tools": [], "params": {}},
    "evaluator": {"prompt": "You are an evaluator that finds benchmark-invisible problems.", "tools": [], "params": {}},
}
_LEGACY_SLOTS = {"skills": "tools", "eval_points": "tools"}
_RESERVED = set(ROLE_SCHEMAS["task"]) | {"model", "skills", "eval_points"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ALLOWED_TYPES = {"str", "int", "float", "bool", "list", "dict"}
MAX_DYNAMIC_KEYS = 16
MAX_ENUM_VALUES = 32
MAX_DESCRIPTION_CHARS = 200
MAX_VALUE_TOKEN_CHARS = 64
_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def default_config(role: str) -> Dict[str, Any]:
    if role not in _DEFAULTS:
        raise KeyError(f"unknown role: {role}")
    return copy.deepcopy(_DEFAULTS[role])


def _schema_path(role: str, overlay_root: Optional[str] = None, code_root: Optional[str] = None) -> Path:
    rel = Path("gan") / "design" / "schema_ext" / f"{role}.json"
    for root in (overlay_root, code_root):
        if root:
            p = Path(root) / rel
            if p.is_file():
                return p
    return Path(__file__).resolve().parent / "schema_ext" / f"{role}.json"


def load_dynamic_specs(role: str, overlay_root: Optional[str] = None,
                       code_root: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load the role's workspace-first dynamic-key catalog; malformed entries are ignored."""
    if role not in ROLE_SCHEMAS:
        return {}
    try:
        with open(_schema_path(role, overlay_root, code_root), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, spec in raw.items():
        if not isinstance(key, str) or not isinstance(spec, dict) or not _valid_spec(key, spec):
            continue
        out[key] = copy.deepcopy(spec)
    return out


def _valid_spec(key: str, spec: Dict[str, Any]) -> bool:
    if not _KEY_RE.fullmatch(key) or key in _RESERVED:
        return False
    if spec.get("type") not in _ALLOWED_TYPES:
        return False
    if not isinstance(spec.get("consumer"), str) or not spec["consumer"]:
        return False
    if len(str(spec.get("description", ""))) > MAX_DESCRIPTION_CHARS:
        return False
    enum = spec.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or len(enum) > MAX_ENUM_VALUES or len(set(map(repr, enum))) != len(enum):
            return False
    return True


def dynamic_key_spec(role: str, key: str, overlay_root: Optional[str] = None,
                     code_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return load_dynamic_specs(role, overlay_root, code_root).get(str(key))


def allowed_keys(role: str, overlay_root: Optional[str] = None,
                 code_root: Optional[str] = None) -> Set[str]:
    return (set(ROLE_SCHEMAS.get(role, {}).keys())
            | set(load_dynamic_specs(role, overlay_root, code_root)))


def value_type_name(value: Any) -> str:
    if isinstance(value, bool): return "bool"
    if isinstance(value, int): return "int"
    if isinstance(value, float): return "float"
    if isinstance(value, str): return "str"
    if isinstance(value, list): return "list"
    if isinstance(value, dict): return "dict"
    return "unknown"


def validate_dynamic_value(spec: Dict[str, Any], value: Any) -> Optional[str]:
    typ = spec.get("type")
    if value_type_name(value) != typ:
        return f"value type is {value_type_name(value)}, expected {typ}"
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        return f"value is not in enum {enum!r}"
    return None


def normalize_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return config
    legacy: List[Any] = []
    for k in ("skills", "eval_points"):
        v = config.get(k)
        if isinstance(v, (list, tuple)):
            legacy.extend(str(x) for x in v)
    if not legacy:
        return config
    out = {k: v for k, v in config.items() if k not in _LEGACY_SLOTS}
    cur = out.get("tools")
    merged = [str(x) for x in cur] if isinstance(cur, (list, tuple)) else []
    for x in legacy:
        if x not in merged: merged.append(x)
    out["tools"] = merged
    return out


def validate_config(role: str, config: Dict[str, Any], overlay_root: Optional[str] = None,
                    code_root: Optional[str] = None) -> List[str]:
    problems: List[str] = []
    known = allowed_keys(role, overlay_root, code_root)
    for k, value in config.items():
        if k not in known:
            problems.append(f"unknown key '{k}' for role '{role}' (declare it with add_config_key)")
            continue
        spec = dynamic_key_spec(role, k, overlay_root, code_root)
        if spec:
            err = validate_dynamic_value(spec, value)
            if err: problems.append(f"dynamic key '{k}': {err}")
    return problems


def validate_key_name(key: str) -> Optional[str]:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key): return "key must match ^[a-z][a-z0-9_]{0,31}$"
    if key in _RESERVED: return f"key '{key}' is reserved"
    return None


def value_token_valid(value: Any) -> bool:
    return isinstance(value, str) and bool(_VALUE_RE.fullmatch(value))
