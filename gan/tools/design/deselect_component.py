"""Design operator: remove a tool from the design's ``tools`` slot (SHALLOW).

Shallow: only edits the role's *design config* list, which determines what gets
assembled into the instance's toolset. It does NOT touch the component registry
or the component source files.

Removing a tool from the registry and deleting its source file is a DEEP
change (component removal, e.g. ``unregister_component``); adding or modifying
tool *logic* is also DEEP (source edit). See the design decision record.

Batch 6: one ``tools`` slot per role; the former ``skills``/``eval_points``
names are accepted as aliases.
"""
from gan.framework.context import get_design_context

_SLOT_ALIASES = {"skills": "tools", "eval_points": "tools"}


def tool_info():
    return {
        "name": "deselect_component",
        "description": (
            "Remove a tool from the design's 'tools' slot so it is no longer assembled "
            "into this role's toolset (the former 'skills'/'eval_points' slot names are "
            "accepted as aliases). SHALLOW: only edits the design config; the tool stays "
            "in the registry and its source is untouched."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {"type": "string", "enum": ["tools", "skills", "eval_points"]},
                "name": {"type": "string"},
            },
            "required": ["slot", "name"],
        },
    }


def tool_function(slot, name, **kwargs):
    ctx = get_design_context()
    if ctx is None:
        return "Error: no design context"
    slot = _SLOT_ALIASES.get(str(slot), str(slot))
    if slot != "tools":
        return f"Error: unknown slot '{slot}' (the design config has exactly one component slot: 'tools')"
    if "tools" not in ctx.config:
        return f"Error: slot 'tools' not in {ctx.role} design schema"
    cur = list(ctx.config.get("tools") or [])
    if name not in cur:
        return f"Error: '{name}' is not selected in tools"
    cur.remove(name)
    ctx.config["tools"] = cur
    ctx.record("deselect_component", slot="tools", name=name)
    return f"tools = {cur}"


op_info = tool_info
op_function = tool_function
