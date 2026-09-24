"""DEEP gate: register an existing component module in a registry.

Symmetric to ``unregister_component``. Edits the role's **workspace** copy of the
registry (entry appended) rather than committing immediately -- the change is
folded into the session's patch and goes through the normal commit validation
(allowlist + compile + registry) together with the rest of the edits.

Only an *existing* module can be registered: creating the file is a separate
deep step (``request_source_access`` + ``edit_source``).
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context
from gan.registries.loader import _REGISTRY_FILES, entry_reason

# registries a role may actually write (planner.json is in no role's write roots,
# so it is deliberately not offered in the schema enum)
_WRITABLE_REGISTRY = [f for f in _REGISTRY_FILES if f != "planner.json"]
# planner designs the TASK agent -> registers task components; evaluator designs
# itself -> registers eval_points
_OWN_REGISTRY = {"planner": "task.json", "evaluator": "evaluator.json"}


def tool_info():
    return {
        "name": "register_component",
        "description": (
            "DEEP add: register an EXISTING component module in a registry so it can be "
            "selected. The module file must already exist (create it first with "
            "request_source_access + edit_source) and be editable by your role. Applied "
            "to your workspace; committed with this session's patch after validation. "
            "kind: skill | eval_point. registry defaults to your role's own "
            "(planner -> task.json, evaluator -> evaluator.json)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "eval_point"]},
                "name": {"type": "string",
                         "description": "Must equal the module file stem (without .py)."},
                "module": {"type": "string",
                           "description": "Path relative to gan/components, e.g. "
                                          "task/skills/foo.py"},
                "registry": {"type": "string",
                             "enum": _WRITABLE_REGISTRY},
            },
            "required": ["kind", "name", "module"],
        },
    }


def tool_function(kind, name, module, registry=None, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    role = actx.role
    broker = actx.broker
    node = actx.node_id
    code_root = broker.repo_root

    if registry:
        reg_name = str(registry).strip()
        if reg_name not in _WRITABLE_REGISTRY:
            return (f"Error: unknown registry: {registry} "
                    f"(one of: {', '.join(_WRITABLE_REGISTRY)})")
    else:
        reg_name = _OWN_REGISTRY.get(role)
        if not reg_name:
            return f"Error: no default registry for role '{role}'; pass registry= explicitly"
    reg_rel = f"gan/registries/{reg_name}"

    mod = str(module or "").replace("\\", "/").strip("/")
    if not mod.endswith(".py"):
        return "Error: module must be a .py path relative to gan/components"
    module_rel = f"gan/components/{mod}"

    # 1) permission gate (same rule and order as unregister_component)
    if not frozen.is_allowed(role, reg_rel, "modify"):
        return f"Error: registry not editable for {role}: {reg_rel}"
    if not frozen.is_allowed(role, module_rel, "modify"):
        return f"Error: component source not editable for {role}: {module_rel}"

    # 2) the module must already exist: this registers a component, it does not
    #    create one (file creation stays with edit_source)
    if not os.path.isfile(os.path.join(code_root, module_rel)):
        return (f"Error: module not found: {module_rel}. Create the file first "
                f"(edit_source), then register it.")

    # 3) identity contract: name == file stem (tools are keyed by stem, so a
    #    mismatch assembles but never loads). Friendly check first; entry_reason
    #    below is the backstop.
    stem = Path(mod).stem
    if str(name) != stem:
        return (f"Error: name '{name}' must equal the module file stem '{stem}' "
                f"(tools are keyed by file stem; the component would never load)")

    # 4) validate the entry as it would appear in the registry (kind/dir + tool API)
    candidate = {"name": name, "kind": kind, "module": mod}
    reason = entry_reason(candidate, Path(os.path.join(code_root, "gan", "components")))
    if reason:
        return f"Error: invalid entry ({reason})"

    # 5) bring the registry into the workspace and append the entry (read-modify-write
    #    on the workspace copy; the rest of the file is preserved).
    #    Only grant when the workspace copy is absent: ``AccessBroker.grant``
    #    re-copies the repo file over the workspace copy, so re-granting within one
    #    session would silently discard the entries registered by earlier calls.
    src = broker.src_dir(role, node)
    ws_reg = os.path.join(src, reg_rel)
    if not os.path.isfile(ws_reg):
        broker.grant(role, node, [reg_rel], intent="modify", reason="register")
    try:
        with open(ws_reg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error: cannot edit registry in workspace: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("components", []), list):
        return f"Error: registry is not an object with a 'components' list: {reg_rel}"
    comps = data.setdefault("components", [])
    for c in comps:
        if isinstance(c, dict) and c.get("kind") == kind and c.get("name") == name:
            if str(c.get("module")).replace("\\", "/") == mod:
                return f"Already registered: {kind} '{name}' in {reg_rel}"
            return (f"Error: {kind} '{name}' already registered in {reg_rel} with a "
                    f"different module ({c.get('module')})")
    comps.append({"name": name, "kind": kind, "module": mod})
    with open(ws_reg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return (f"Registered {kind} '{name}' ({module_rel}) in {reg_rel}. It will be "
            f"committed with this session's patch after validation; then use "
            f"select_component to enable it.")


op_info = tool_info
op_function = tool_function
