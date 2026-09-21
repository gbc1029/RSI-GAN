"""Design operator: select a registered component into a design slot.

Shallow: only selects among components already present in the role's registry
(``gan/design/registries/<role>_registry.json``). Registering a NEW component is
a source-level (deep) change.
"""
from gan.framework.context import get_design_context
from gan.registries.loader import load_registry_for_role

_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "select_component",
        "description": (
            "Select a registered component into a design slot. Slots: 'skills' (task), "
            "'eval_points' (evaluator). Only registered components can be selected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {"type": "string", "enum": ["skills", "eval_points"]},
                "name": {"type": "string"},
            },
            "required": ["slot", "name"],
        },
    }


def tool_function(slot, name, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    reg = load_registry_for_role(ctx.role)
    kind = _SLOT_KIND.get(slot)
    if kind is None:
        return f"Error: unknown slot '{slot}'"
    if slot not in ctx.config:
        return f"Error: slot '{slot}' not in {ctx.role} design schema"
    if not reg.has(kind, name):
        return f"Error: {kind} '{name}' not registered for role {ctx.role}"
    if not reg.is_valid(kind, name):
        return (f"Error: {kind} '{name}' is registered but INVALID "
                f"({reg.reason(kind, name)}); fix the registry/component first")
    cur = list(ctx.config.get(slot) or [])
    if name not in cur:
        cur.append(name)
    ctx.config[slot] = cur
    ctx.record("select_component", slot=slot, name=name)
    return f"{slot} = {cur}"


op_info = tool_info
op_function = tool_function
