"""Design operator: select a registered tool into the design's ``tools`` slot.

Shallow: only selects among tools present in the role's registry
(``gan/registries/<role>.json``, loaded by
``gan/registries/loader.py:load_registry_for_role``). Registering a NEW tool is a
source-level (deep) change (``gan/tools/deep/register_component.py``).

Batch 6: the design config has ONE slot per role -- ``tools``. The former
``skills``/``eval_points`` slot names are legacy vocabulary for the same thing
and are accepted (normalized) so an agent or an older plan that names them still
works. Every registry entry is a callable tool; the former kind distinction
skill-vs-eval_point was only ever a routing label and is retired.

Authority (batch 5, P-3): the registry is resolved **workspace-first, committed
tree second** for the TASK design (the same effective view the deep tools scan),
because the framework heals the task design against the committed tree right
before persisting it (``gan/framework/task_execution.py:heal_design_slots``):
selecting a tool registered in this session's workspace is safe -- the child
actually gets it when the session's patch commits, and a rejected patch strips
the dangling name before the design is persisted (event
``design_dangling_stripped`` + receipt ``design_stripped``). Role self-designs
(planner/evaluator) are NOT healed yet, so they keep the committed-only
authority: for them a tool becomes selectable only after the patch commits.
"""
from gan.framework.context import get_design_context, session_overlay_root
from gan.registries.loader import load_registry_for_role

# legacy slot names accepted and folded into ``tools`` (batch 6 vocabulary)
_SLOT_ALIASES = {"skills": "tools", "eval_points": "tools"}


def tool_info():
    return {
        "name": "select_component",
        "description": (
            "Select a registered tool into the design's 'tools' slot (every role has "
            "exactly this one component slot; the former 'skills'/'eval_points' names "
            "are accepted as aliases). Only registered tools can be selected; a tool "
            "you registered this session is selectable right away for the task design, "
            "but the task agent only actually gets it once your patch commits."
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
    # Task design: the effective (workspace-first) registry, safe because the
    # design is healed against the committed tree before persist. Role
    # self-designs: committed-only (overlay stays None) -- see module docstring.
    overlay = session_overlay_root() if ctx.role == "task" else None
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
    if overlay is not None and ctx.role == "task":
        return (f"tools = {cur}. Note: '{name}' takes effect for the task agent "
                f"only when this session's patch commits; a rejected patch strips "
                f"it from the design before persisting.")
    return f"tools = {cur}"


op_info = tool_info
op_function = tool_function
