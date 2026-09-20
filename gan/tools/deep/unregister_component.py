"""DEEP gate: schedule a component removal (registry entry + source file).

Unlike a shallow ``deselect_component``, this removes the registry entry and
deletes the component module. It edits the role's **workspace** copy (registry
entry removed, module file deleted) rather than committing immediately — the
change is folded into the session's patch and goes through the normal commit
validation (allowlist + compile + registry) together with the rest of the edits.
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context
from gan.registries.loader import parse_registry_file

_REGISTRY_FILES = ["shared.json", "task.json", "planner.json", "evaluator.json"]


def tool_info():
    return {
        "name": "unregister_component",
        "description": (
            "DEEP delete: remove a registered component from its registry and delete its "
            "source file. Applied to your workspace; committed with this session's patch "
            "after validation. Only components your role may edit can be removed. "
            "kind: skill | eval_point | memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "eval_point", "memory"]},
                "name": {"type": "string"},
            },
            "required": ["kind", "name"],
        },
    }


def tool_function(kind, name, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    role = actx.role
    broker = actx.broker
    node = actx.node_id
    code_root = broker.repo_root

    for fn in _REGISTRY_FILES:
        rel = f"gan/registries/{fn}"
        ents, err = parse_registry_file(Path(os.path.join(code_root, rel)))
        if err or not ents:
            continue
        match = [e for e in ents
                 if isinstance(e, dict) and e.get("kind") == kind and e.get("name") == name]
        if not match:
            continue
        if not frozen.is_allowed(role, rel, "modify"):
            return f"Error: registry not editable for {role}: {rel}"
        mod = str(match[0].get("module") or "")
        module_rel = f"gan/components/{mod}" if mod else ""
        if module_rel and not frozen.is_allowed(role, module_rel, "modify"):
            return f"Error: component source not editable for {role}: {module_rel}"

        # bring the registry (and module, if present) into the workspace
        broker.grant(role, node, [rel], intent="modify", reason="unregister")
        if module_rel:
            broker.grant(role, node, [module_rel], intent="modify", reason="unregister")

        src = broker.src_dir(role, node)
        ws_reg = os.path.join(src, rel)
        ws_mod = os.path.join(src, module_rel) if module_rel else ""
        try:
            with open(ws_reg, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"Error: cannot edit registry in workspace: {e}"
        data["components"] = [
            c for c in data.get("components", [])
            if not (isinstance(c, dict) and c.get("kind") == kind and c.get("name") == name)
        ]
        with open(ws_reg, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if ws_mod and os.path.isfile(ws_mod):
            os.remove(ws_mod)
        return (f"Scheduled removal of {kind} '{name}' ({rel}); it will be committed with "
                f"this session's patch after validation.")
    return f"Error: component not found: {kind} {name}"


op_info = tool_info
op_function = tool_function
