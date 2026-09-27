"""Design operator: select a registered component into a design slot.

Shallow: only selects among components present in the role's registry
(``gan/registries/shared.json`` plus ``gan/registries/<role>.json``, loaded by
``gan/registries/loader.py:load_registry_for_role``). Registering a NEW component
is a source-level (deep) change (``gan/tools/deep/register_component.py``).

Authority (batch 5, P-3): the registry is resolved **workspace-first, committed
tree second** for the TASK design (the same effective view the deep tools scan),
because the framework heals the task design against the committed tree right
before persisting it (``gan/framework/task_execution.py:heal_design_slots``):
selecting a component registered in this session's workspace is safe -- the child
actually gets it when the session's patch commits, and a rejected patch strips
the dangling name before the design is persisted (event
``design_dangling_stripped`` + receipt ``design_stripped``). Role self-designs
(planner/evaluator) are NOT healed yet, so they keep the committed-only
authority: for them a component becomes selectable only after the patch commits.
"""
from gan.framework.context import get_design_context, session_overlay_root
from gan.registries.loader import load_registry_for_role

_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "select_component",
        "description": (
            "Select a registered component into a design slot. Slots: 'skills' (task), "
            "'eval_points' (evaluator). Only registered components can be selected; a "
            "component you registered this session is selectable right away for the "
            "task design, but the task agent only actually gets it once your patch "
            "commits."
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
    # Task design: the effective (workspace-first) registry, safe because the
    # design is healed against the committed tree before persist. Role
    # self-designs: committed-only (overlay stays None) -- see module docstring.
    overlay = session_overlay_root() if ctx.role == "task" else None
    reg = load_registry_for_role(ctx.role, overlay_root=overlay)
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
    if overlay is not None and ctx.role == "task":
        return (f"{slot} = {cur}. Note: '{name}' takes effect for the task agent "
                f"only when this session's patch commits; a rejected patch strips "
                f"it from the design before persisting.")
    return f"{slot} = {cur}"


op_info = tool_info
op_function = tool_function
