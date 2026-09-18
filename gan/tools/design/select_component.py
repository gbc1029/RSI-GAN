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
            "'eval_points' (evaluator), or 'memory' (single name). Only registered components "
            "can be selected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {"type": "string", "enum": ["skills", "eval_points", "memory"]},
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
    if slot == "memory":
        if not reg.has("memory", name):
            return f"Error: memory '{name}' not registered for role {ctx.role}"
        ctx.config["memory"] = name
        ctx.record("select_component", slot=slot, name=name)
        return f"memory = {name}"
    kind = _SLOT_KIND.get(slot)
    if kind is None:
        return f"Error: unknown slot '{slot}'"
    if slot not in ctx.config:
        return f"Error: slot '{slot}' not in {ctx.role} design schema"
    if not reg.has(kind, name):
        return f"Error: {kind} '{name}' not registered for role {ctx.role}"
    cur = list(ctx.config.get(slot) or [])
    if name not in cur:
        cur.append(name)
    ctx.config[slot] = cur
    ctx.record("select_component", slot=slot, name=name)
    return f"{slot} = {cur}"


op_info = tool_info
op_function = tool_function
