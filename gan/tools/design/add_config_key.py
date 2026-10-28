"""Design operator: declare a dynamic config key with a real consumer."""
from __future__ import annotations
import json, os
from gan.framework.context import get_design_context, get_access_context, session_overlay_root, shallow_op_error
from gan.framework import cost_keys as _cost_keys
from gan.design.schema import (ROLE_SCHEMAS, _ALLOWED_TYPES, _KEY_RE, _RESERVED,
                               MAX_DYNAMIC_KEYS, MAX_DESCRIPTION_CHARS, MAX_ENUM_VALUES,
                               _valid_spec, load_dynamic_specs, validate_dynamic_value,
                               value_token_valid)
from gan.registries.loader import load_registry_for_role
from gan.framework.write_auth import authorize_write


def tool_info():
    return {"name": "add_config_key", "description": "Declare a new dynamic config key. consumer must be '<registered tool>@<read-site>': the tool part must be a registered valid consumer component, and the read-site part must name where that component (or framework code) ACTUALLY reads the key — a declared key nobody reads is a dead key. Declaration and consumer must land in the same patch. Intent matching a reserved cost subkey (cm_mode, per_question_token_budget, …) is refused: set those via set_config(key=\"cost\", value={…}) instead.", "input_schema": {"type":"object", "properties": {"key":{"type":"string"}, "value_type":{"type":"string", "enum": sorted(_ALLOWED_TYPES)}, "consumer":{"type":"string", "description":"'<registered tool>@<read-site>' — e.g. 'ctx_fetch@ctx_fetch.py: cfg[key]'"}, "description":{"type":"string"}, "enum":{"type":"array"}, "initial_value":{}}, "required":["key","value_type","consumer"]}}


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
    err = shallow_op_error("add_config_key", key=str(key))
    if err: return err
    key = str(key); role = ctx.role
    if key in ROLE_SCHEMAS.get(role, {}) or key in _RESERVED or not _KEY_RE.fullmatch(key):
        return f"Error: invalid or reserved config key '{key}'"
    overlay = session_overlay_root()
    specs = load_dynamic_specs(role, overlay)
    if key in specs: return f"Error: config key '{key}' already exists"
    if len(specs) >= MAX_DYNAMIC_KEYS: return f"Error: dynamic key limit reached ({MAX_DYNAMIC_KEYS})"
    if value_type not in _ALLOWED_TYPES: return f"Error: unsupported value_type '{value_type}'"
    if len(str(description or "")) > MAX_DESCRIPTION_CHARS: return f"Error: description exceeds {MAX_DESCRIPTION_CHARS} characters"
    # consumer = "<registered tool>@<read-site>" (docs/17 §6): the tool part is
    # registry-checked below; the read-site part is REQUIRED documentation of
    # where the key is actually consumed — the declaration-time step of locating
    # a reader is the only gate this scheme has (no AST check, no runtime probe
    # by decision). A registered consumer that never reads the key is a dead key
    # (observed: cost_cm_mode, small_1028_0046 outer 2/3).
    tool, at, site = str(consumer).partition("@")
    tool, site = tool.strip(), site.strip()
    if not at or not tool or not site:
        return ("Error: consumer must be '<registered tool>@<read-site>' — name where "
                f"'{key}' is actually consumed (file/logic). A registered consumer that "
                "never reads the key is a dead key (docs/17 §6).")
    if len(str(consumer)) > MAX_DESCRIPTION_CHARS:
        return f"Error: consumer exceeds {MAX_DESCRIPTION_CHARS} characters"
    reg = load_registry_for_role(role, overlay_root=overlay)
    if not reg.has(tool) or not reg.is_valid(tool):
        return f"Error: consumer tool '{tool}' is not a registered valid {role} tool"
    # Reserved cost subkeys are framework-consumed through the design 'cost'
    # slot (gan/framework/cost_keys.py — the ONLY authority for those intents):
    # a same-named dynamic key (exact, or a cost_-prefixed shadow like
    # cost_cm_mode -> cm_mode) can never be read by anything. Refuse and point
    # at the sanctioned channel.
    norm = key[5:] if key.startswith("cost_") else key
    if norm in _cost_keys.spec_keys(role):
        rspec = _cost_keys.specs(role).get(norm) or {}
        return (f"Error: '{key}' collides with the reserved cost subkey '{norm}' "
                f"(framework-consumed: {rspec.get('consumer', 'the cost slot')}). Set it "
                f"with set_config(key=\"cost\", value={{\"{norm}\": …}}) instead of "
                "declaring a key nothing reads.")
    spec = {"type": value_type, "consumer": f"{tool}@{site}", "description": str(description or "")}
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
    ctx.record("add_config_key", key=key, value_type=value_type,
               consumer=f"{tool}@{site}")
    # Reserved-intent warning (non-blocking, user decision 2026-10-28): a new
    # enum that substantially overlaps a reserved cost subkey's enum usually
    # means the intent already has a sanctioned channel — say so, don't refuse
    # (a genuinely distinct key with a similar value space stays legal).
    warning = ""
    if enum and value_type == "str":
        estr = {str(x) for x in enum}
        for ck, cspec in _cost_keys.specs(role).items():
            cen = cspec.get("enum")
            if not cen:
                continue
            cset = {str(x) for x in cen}
            union = len(estr | cset)
            if union and len(estr & cset) / union >= 0.75:
                warning = (f"WARNING: enum overlaps the reserved cost subkey '{ck}' "
                           f"(Jaccard {len(estr & cset) / union:.2f}) — prefer "
                           f"set_config(key=\"cost\", value={{\"{ck}\": …}}) unless the "
                           "new key is genuinely distinct")
                break
    return f"config key '{key}' declared" + (f" — {warning}" if warning else "")

op_info = tool_info
op_function = tool_function
