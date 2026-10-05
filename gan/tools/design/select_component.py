"""Design operator: select a registered tool into the design's ``tools`` slot.

Shallow: only selects among tools present in the role's registry
(``gan/registries/<role>.json``, loaded by
``gan/registries/loader.py:load_registry_for_role``). Registering a NEW tool is a
source-level (deep) change (``gan/tools/deep/register_component.py``).

The design config has ONE slot per role -- ``tools``. The legacy
``skills``/``eval_points`` slot names are accepted (normalized) so an agent or
an older plan that names them still works. Every registry entry is a callable
tool; the legacy kind distinction (skill vs eval_point) was only ever a routing
label.

The registry is resolved **workspace-first, committed tree second** (the same
effective view the deep tools scan), for the task design AND for role
self-designs alike, including a tool registered in this very session. Safety
argument: the committed tree cannot deliver what the patch does not commit --
a successful patch commits it; a rejected/exhausted one leaves the selection
dangling, where it is visible (assembly report) and healed at the patch-exit
heal on the successful path. The exhausted-exit heal remains backlog
(docs/7 section 6.1). There is no committed-only exception for role
self-designs.
"""
from gan.framework.context import get_design_context, session_overlay_root
from gan.registries.loader import load_registry_for_role

# legacy slot names accepted and folded into ``tools``
_SLOT_ALIASES = {"skills": "tools", "eval_points": "tools"}


def tool_info():
    return {
        "name": "select_component",
        "description": (
            "Select a registered tool into the design's 'tools' slot (every role has "
            "exactly this one component slot; the former 'skills'/'eval_points' names "
            "are accepted as aliases). Only registered tools can be selected; a tool "
            "you registered this session is selectable right away, and the task agent "
            "actually gets it once your patch commits."
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
    # Every role selects against the effective (workspace-first) registry;
    # there is no committed-only exception for role self-designs.
    overlay = session_overlay_root()
    reg = load_registry_for_role(ctx.role, overlay_root=overlay)
    if "tools" not in ctx.config:
        return f"Error: slot 'tools' not in {ctx.role} design schema"
    if not reg.has(name):
        return f"Error: tool '{name}' not registered for role {ctx.role}"
    if not reg.is_valid(name):
        return (f"Error: tool '{name}' is registered but INVALID "
                f"({reg.reason(name)}); fix the registry/component first")
    cur = list(ctx.config.get("tools") or [])
    if name not in cur:
        cur.append(name)
    ctx.config["tools"] = cur
    ctx.record("select_component", slot="tools", name=name)
    if overlay is not None and ctx.role != "task":
        return (f"tools = {cur}. Note: '{name}' takes effect for your own design "
                f"once this session's patch commits; a rejected or exhausted patch "
                f"leaves it dangling until the next assembly reports it.")
    if overlay is not None and ctx.role == "task":
        return (f"tools = {cur}. Note: '{name}' takes effect for the task agent "
                f"only when this session's patch commits; a rejected patch strips "
                f"it from the design before persisting.")
    return f"tools = {cur}"


op_info = tool_info
op_function = tool_function
