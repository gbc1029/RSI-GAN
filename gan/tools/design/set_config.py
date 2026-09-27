"""Design operator: set an EXISTING config key (shallow).

Only keys already defined in the role schema may be set. Adding a NEW key is a
source-level (deep) change, not a shallow operator.

Slot keys (``skills`` / ``eval_points``, B25/H12) go through the SAME component
validation as ``select_component`` -- a raw list write here used to bypass the
registry check entirely, so an unregistered name could enter the design without
any rejection and silently lose the capability at assembly. The authority is the
same one ``select_component`` uses: the effective (workspace-first) registry for
the TASK design (which the framework heals before persisting), the committed
registry for role self-designs. The write is atomic: if ANY name fails, the
whole call is refused and the design is untouched.
"""
from gan.framework.context import get_design_context, session_overlay_root
from gan.design.schema import allowed_keys
from gan.registries.loader import load_registry_for_role

_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "set_config",
        "description": (
            "Set an existing design-config key (e.g. 'prompt', 'skills', 'params'). "
            "Adding a NEW key is not allowed here (requires a source-level change). "
            "For component slots ('skills', 'eval_points') every name must be a "
            "registered, valid component (same check as select_component); the whole "
            "call is refused otherwise."
        ),
        "input_schema": {
            "type": "object", "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key", "value"],
        },
    }


def tool_function(key, value, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    if key not in allowed_keys(ctx.role):
        return (f"Error: '{key}' is not in the {ctx.role} design schema; "
                f"adding new keys requires a source-level (deep) change")
    extra: dict = {}
    kind = _SLOT_KIND.get(key)
    if kind is not None:
        # B25/H12: slot keys are component references, not free values. Validate
        # exactly like select_component (same authority, same errors).
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            return f"Error: '{key}' must be a list of component name strings"
        if key not in ctx.config:
            return f"Error: slot '{key}' not in {ctx.role} design schema"
        overlay = session_overlay_root() if ctx.role == "task" else None
        reg = load_registry_for_role(ctx.role, overlay_root=overlay)
        for name in value:
            if not reg.has(kind, name):
                return (f"Error: {kind} '{name}' not registered for role {ctx.role}; "
                        f"use list_components to see the options, register_component "
                        f"(deep) to add a new one, or select_component to pick one")
            if not reg.is_valid(kind, name):
                return (f"Error: {kind} '{name}' is registered but INVALID "
                        f"({reg.reason(kind, name)}); fix the registry/component first")
        extra["selected"] = list(value)
    if key == "params" and isinstance(value, dict) and "model" in value:
        # Frozen framework setting: role model is not an evolvable design value.
        value = {k: v for k, v in value.items() if k != "model"}
    ctx.config[key] = value
    ctx.record("set_config", key=key, **extra)
    return f"{ctx.role}.{key} updated"


op_info = tool_info
op_function = tool_function
