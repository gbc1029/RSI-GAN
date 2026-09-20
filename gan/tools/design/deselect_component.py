"""Design operator: remove a component from a design slot (SHALLOW).

Shallow: only edits the role's *design config* list, which determines what gets
assembled into the instance's toolset. It does NOT touch the component registry
or the component source files.

Removing a component from the registry and deleting its source file is a DEEP
change (component removal, e.g. ``unregister_component``); adding or modifying
component *logic* is also DEEP (source edit). See the design decision record.
"""
from gan.framework.context import get_design_context

_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "deselect_component",
        "description": (
            "Remove a component from a design slot so it is no longer assembled into this "
            "role's toolset. Slots: 'skills' (task), 'eval_points' (evaluator), or 'memory' "
            "(single name). SHALLOW: only edits the design config; the component stays in the "
            "registry and its source is untouched."
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
    if slot == "memory":
        if ctx.config.get("memory") == name:
            ctx.config["memory"] = None
            ctx.record("deselect_component", slot=slot, name=name)
            return "memory = None"
        return f"Error: memory is not '{name}'"
    if slot not in ctx.config:
        return f"Error: slot '{slot}' not in {ctx.role} design schema"
    cur = list(ctx.config.get(slot) or [])
    if name not in cur:
        return f"Error: '{name}' is not selected in {slot}"
    cur.remove(name)
    ctx.config[slot] = cur
    ctx.record("deselect_component", slot=slot, name=name)
    return f"{slot} = {cur}"


op_info = tool_info
op_function = tool_function
