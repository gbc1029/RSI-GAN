"""Design operator: set an EXISTING config key (shallow).

Only keys already defined in the role schema may be set. Adding a NEW key is a
source-level (deep) change, not a shallow operator.

The ``tools`` slot (batch 6: one component slot per role; legacy ``skills``/
``eval_points`` names are aliases) goes through the SAME tool validation as
``select_component`` -- a raw list write here used to bypass the registry check
entirely, so an unregistered name could enter the design without any rejection
and silently lose the capability at assembly. The authority is the same one
``select_component`` uses: the effective (workspace-first) registry, and since
batch 13 FOR EVERY ROLE (role self-designs included; the batch-5 committed-only
exception is retired -- safety rests on the successful-exit heal, the B7
assembly report and the ability to re-select next outer). The write is atomic:
if ANY name fails, the whole call is refused and the design is untouched.
"""
from gan.framework.context import get_design_context, session_overlay_root
from gan.framework.receipt import prompt_change_facts
from gan.design.schema import allowed_keys
from gan.registries.loader import load_registry_for_role

_SLOT_ALIASES = {"skills": "tools", "eval_points": "tools"}
_COMPONENT_SLOT = "tools"


def tool_info():
    return {
        "name": "set_config",
        "description": (
            "Set an existing design-config key (e.g. 'prompt', 'tools', 'params'). "
            "Adding a NEW key is not allowed here (requires a source-level change). "
            "For the component slot ('tools'; legacy 'skills'/'eval_points' aliases) "
            "every name must be a registered, valid tool (same check as "
            "select_component); the whole call is refused otherwise."
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
    key = _SLOT_ALIASES.get(str(key), str(key))
    if key not in allowed_keys(ctx.role):
        return (f"Error: '{key}' is not in the {ctx.role} design schema; "
                f"adding new keys requires a source-level (deep) change")
    extra: dict = {}
    if key == _COMPONENT_SLOT:
        # B25/H12: the component slot holds tool references, not free values.
        # Validate exactly like select_component (same authority, same errors).
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            return f"Error: '{key}' must be a list of tool names (strings)"
        if key not in ctx.config:
            return f"Error: slot '{key}' not in {ctx.role} design schema"
        # batch 13: the effective (workspace-first) registry for ALL roles --
        # mirrors select_component (same authority, same errors)
        overlay = session_overlay_root()
        reg = load_registry_for_role(ctx.role, overlay_root=overlay)
        for name in value:
            if not reg.has(name):
                return (f"Error: tool '{name}' not registered for role {ctx.role}; "
                        f"use list_components to see the catalog, register_component "
                        f"(deep) to add a new one, or select_component to pick one")
            if not reg.is_valid(name):
                return (f"Error: tool '{name}' is registered but INVALID "
                        f"({reg.reason(name)}); fix the registry/component first")
        extra["selected"] = list(value)
    if key == "params" and isinstance(value, dict) and "model" in value:
        # Frozen framework setting: role model is not an evolvable design value.
        value = {k: v for k, v in value.items() if k != "model"}
    if key == "prompt":
        # B29: ``set_config(key="prompt")`` is the same act as set_prompt and had
        # the same blind spot (it recorded only the key). Capture the replaced
        # value BEFORE the assignment and record the same structural facts.
        from gan.tools.design.set_prompt import _seed_prompt
        prev = (ctx.config or {}).get("prompt") or ""
        extra.update(prompt_change_facts(prev, value, seed=_seed_prompt(ctx.role)))
    ctx.config[key] = value
    ctx.record("set_config", key=key, **extra)
    return f"{ctx.role}.{key} updated"


op_info = tool_info
op_function = tool_function
