"""Design operator: declare a dynamic config key with a real consumer."""
from __future__ import annotations
import json, os
from gan.framework.context import get_design_context, get_access_context, session_overlay_root
from gan.design.schema import (ROLE_SCHEMAS, _ALLOWED_TYPES, _KEY_RE, _RESERVED,
                               MAX_DYNAMIC_KEYS, MAX_DESCRIPTION_CHARS, MAX_ENUM_VALUES,
                               _valid_spec, load_dynamic_specs, validate_dynamic_value,
                               value_token_valid)
from gan.registries.loader import load_registry_for_role
from gan.framework.write_auth import authorize_write


def tool_info():
    return {"name": "add_config_key", "description": "Declare a new dynamic config key. Requires a registered valid consumer component; declaration and consumer must land in the same patch.", "input_schema": {"type":"object", "properties": {"key":{"type":"string"}, "value_type":{"type":"string", "enum": sorted(_ALLOWED_TYPES)}, "consumer":{"type":"string"}, "description":{"type":"string"}, "enum":{"type":"array"}, "initial_value":{}}, "required":["key","value_type","consumer"]}}


def _path(role):
    return f"gan/design/schema_ext/{role}.json"


def _write(role, specs):
    actx = get_access_context()
    if actx is None: return "Error: no access context"
    rel = _path(role)
    err = authorize_write(actx, rel, "add_config_key")
    if err: return err
    p = os.path.join(actx.broker.src_dir(actx.role, actx.node_id), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".write-tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(specs, f, ensure_ascii=False, indent=2); f.write("\n")
    os.replace(tmp, p)
    return None


def tool_function(key, value_type, consumer, description="", enum=None, initial_value=None, **kwargs):
    ctx = get_design_context()
    if ctx is None: return "Error: no design context"
    key = str(key); role = ctx.role
    if key in ROLE_SCHEMAS.get(role, {}) or key in _RESERVED or not _KEY_RE.fullmatch(key):
        return f"Error: invalid or reserved config key '{key}'"
    overlay = session_overlay_root()
    specs = load_dynamic_specs(role, overlay)
    if key in specs: return f"Error: config key '{key}' already exists"
    if len(specs) >= MAX_DYNAMIC_KEYS: return f"Error: dynamic key limit reached ({MAX_DYNAMIC_KEYS})"
    if value_type not in _ALLOWED_TYPES: return f"Error: unsupported value_type '{value_type}'"
    if len(str(description or "")) > MAX_DESCRIPTION_CHARS: return f"Error: description exceeds {MAX_DESCRIPTION_CHARS} characters"
    reg = load_registry_for_role(role, overlay_root=overlay)
    if not reg.has(str(consumer)) or not reg.is_valid(str(consumer)):
        return f"Error: consumer '{consumer}' is not a registered valid {role} tool"
    spec = {"type": value_type, "consumer": str(consumer), "description": str(description or "")}
    if enum is not None:
        if value_type != "str" or not isinstance(enum, list) or len(enum) > MAX_ENUM_VALUES or any(not value_token_valid(x) for x in enum) or len(set(enum)) != len(enum):
            return "Error: enum must be unique structured string tokens and contain at most 32 values"
        spec["enum"] = list(enum)
    if initial_value is not None:
        err = validate_dynamic_value(spec, initial_value)
        if err: return f"Error: initial_value: {err}"
    if not _valid_spec(key, spec):
        # Belt and braces: the operator's own construction must satisfy the same
        # shape the load layer enforces on whatever lands in the schema_ext file.
        return "Error: declaration rejected by the schema spec validator"
    specs[key] = spec
    err = _write(role, specs)
    if err: return err
    if initial_value is not None:
        ctx.config[key] = initial_value
    ctx.record("add_config_key", key=key, value_type=value_type, consumer=str(consumer))
    return f"config key '{key}' declared"

op_info = tool_info
op_function = tool_function
