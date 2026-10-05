"""Design operator: add one value to a declared enum key."""
from __future__ import annotations
from gan.framework.context import get_design_context, session_overlay_root
from gan.design.schema import dynamic_key_spec, load_dynamic_specs, MAX_ENUM_VALUES, value_token_valid
from gan.tools.design.add_config_key import _write


def tool_info():
    return {"name":"add_config_value", "description":"Add a structured optional value to a declared enum config key.", "input_schema":{"type":"object","properties":{"key":{"type":"string"},"value":{}},"required":["key","value"]}}


def tool_function(key, value, **kwargs):
    ctx = get_design_context()
    if ctx is None: return "Error: no design context"
    role = ctx.role; overlay = session_overlay_root()
    specs = load_dynamic_specs(role, overlay)
    spec = specs.get(str(key))
    if spec is None: return f"Error: config key '{key}' is not a declared dynamic key"
    enum = spec.get("enum")
    if not isinstance(enum, list): return f"Error: config key '{key}' has no enum value domain"
    if not value_token_valid(value): return "Error: value must be a token matching [A-Za-z0-9_.-]{1,64}"
    if value in enum: return f"Error: value '{value}' already exists"
    if len(enum) >= MAX_ENUM_VALUES: return f"Error: enum limit reached ({MAX_ENUM_VALUES})"
    enum.append(value)
    err = _write(role, specs)
    if err: return err
    ctx.record("add_config_value", key=str(key), value=value)
    return f"config key '{key}' value '{value}' added"

op_info = tool_info
op_function = tool_function
