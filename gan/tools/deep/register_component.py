"""DEEP gate: register an existing component module in a registry.

Symmetric to ``unregister_component``. Edits the role's **workspace** copy of the
registry (entry appended) rather than committing immediately -- the change is
folded into the session's patch and goes through the normal commit validation
(allowlist + compile + registry) together with the rest of the edits.

Only an *existing* module can be registered: creating the file is a separate
deep step (``request_source_access`` + ``edit_source``). "Existing" means the repo
copy **or** the workspace copy created this session -- this tool never creates,
restores or deletes source, it only records an entry for source that already
exists (so it is deliberately NOT the inverse of ``unregister_component``, which
deletes the file).
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import _REGISTRY_FILES, entry_reason, write_registry_json

# registries a role may actually write (planner.json is in no role's write roots,
# so it is deliberately not offered in the schema enum)
_WRITABLE_REGISTRY = [f for f in _REGISTRY_FILES if f != "planner.json"]
# planner designs the TASK agent -> registers task components; evaluator designs
# itself -> registers eval_points
_OWN_REGISTRY = {"planner": "task.json", "evaluator": "evaluator.json"}


def _granted_coverage(broker, role, node, module_rel: str) -> bool:
    """True if this session's patch can carry ``module_rel``.

    ``build_patch_from_workspace`` walks ``granted_paths`` only, so a NEW file is
    committable only when it -- or one of its ancestor directories -- was granted.
    Without this check the entry would be written for a file that never reaches the
    commit, and the failure would surface as a confusing "module file not found".
    """
    for g in broker.granted_paths(role, node) or []:
        g = str(g).replace("\\", "/").rstrip("/")
        if g and (module_rel == g or module_rel.startswith(g + "/")):
            return True
    return False


def tool_info():
    return {
        "name": "register_component",
        "description": (
            "DEEP add: register an EXISTING component module in a registry so it can be "
            "selected. The module file must already exist and be editable by your role -- "
            "either committed source, or a file you created this session with edit_source "
            "(in that case its directory must be granted, otherwise it cannot be "
            "committed). This tool never creates or restores source. Applied to your "
            "workspace; committed with this session's patch after validation. "
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

    # A deep write is only meaningful in a session that has a patch channel. The
    # design context is set exactly in the sessions that turn the workspace into a
    # patch (plan / self_improve); without it the edit would land in the workspace
    # and then be silently discarded (R1). Refuse loudly instead.
    dctx = get_design_context()
    if dctx is None:
        return ("Error: deep registry changes are currently unavailable in this "
                "session; use `request_source_access` to view source. Do not retry.")

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
    # never trust a caller-supplied path: the allowlist matches prefix globs, so
    # "gan/components/task/../../<outside>" would otherwise pass the gate below and
    # let the existence/identity checks read a file outside gan/components/
    if not mod.endswith(".py") or any(p == ".." for p in mod.split("/")):
        return "Error: module must be a .py path relative to gan/components"
    module_rel = f"gan/components/{mod}"

    # 1) permission gate (same rule and order as unregister_component)
    if not frozen.is_allowed(role, reg_rel, "modify"):
        return f"Error: registry not editable for {role}: {reg_rel}"
    if not frozen.is_allowed(role, module_rel, "modify"):
        return f"Error: component source not editable for {role}: {module_rel}"

    # 2) the module must already exist -- in the repo (committed) OR in the workspace
    #    (created this session with edit_source). Registering never creates or restores
    #    source; it only records an entry for source that exists.
    src = broker.src_dir(role, node)
    ws_mod = os.path.join(src, module_rel)
    in_ws = os.path.isfile(ws_mod)
    in_repo = os.path.isfile(os.path.join(code_root, module_rel))
    if not (in_ws or in_repo):
        return (f"Error: module not found: {module_rel}. Create the file first "
                f"(edit_source), then register it.")
    if in_ws and not in_repo and not _granted_coverage(broker, role, node, module_rel):
        parent = mod.rsplit("/", 1)[0] if "/" in mod else ""
        hint = f"gan/components/{parent}/**" if parent else "gan/components/**"
        return (f"Error: module {module_rel} is new and not covered by any granted path, "
                f"so it would not be part of this session's patch. Grant its directory "
                f"first: request_source_access(paths=['{hint}'], intent='modify').")

    # 3) identity contract: name == file stem (tools are keyed by stem, so a
    #    mismatch assembles but never loads). Friendly check first; entry_reason
    #    below is the backstop.
    stem = Path(mod).stem
    if str(name) != stem:
        return (f"Error: name '{name}' must equal the module file stem '{stem}' "
                f"(tools are keyed by file stem; the component would never load)")

    # 4) validate the entry as it would appear in the registry (kind/dir + tool API).
    #    Validate the WORKSPACE copy whenever there is one: that is the version this
    #    session's patch commits, so validating the repo copy instead would reject a
    #    module the agent just extended with tool_info/tool_function (P3/H3a) and
    #    would accept one the agent just broke. ``entry_reason`` at commit time is the
    #    backstop for both directions.
    candidate = {"name": name, "kind": kind, "module": mod}
    comp_dir = Path(src) / "gan" / "components"
    if not (comp_dir / mod).is_file():
        comp_dir = Path(code_root) / "gan" / "components"
    reason = entry_reason(candidate, comp_dir)
    if reason:
        return f"Error: invalid entry ({reason})"

    # 5) bring the registry into the workspace and append the entry (read-modify-write
    #    on the workspace copy; the rest of the file is preserved).
    #    Only grant when the workspace copy is absent. NOTE(known, deferred): this
    #    local guard is NOT equivalent to ``grant(if_absent=True)`` -- a registry that
    #    exists in the workspace but was never granted stays out of ``granted_paths``,
    #    so the patch builder drops this registration silently. See
    #    docs/gan_tools_deep_write_fix.md (A2).
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
    write_registry_json(ws_reg, data)
    dctx.record("register_component", kind=kind, name=name,
                module=mod, registry=reg_name)
    return (f"Registered {kind} '{name}' ({module_rel}) in {reg_rel}. It will be "
            f"committed with this session's patch after validation; then use "
            f"select_component to enable it.")


op_info = tool_info
op_function = tool_function
