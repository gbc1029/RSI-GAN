"""DEEP gate: schedule a tool removal (registry entry + source file).

Unlike a shallow ``deselect_component``, this removes the registry entry and
deletes the tool module. It edits the designed role's **workspace** copy (registry
entry removed, module file deleted) rather than committing immediately — the
change is folded into the session's patch and goes through the normal commit
validation (allowlist + compile + registry) together with the rest of the edits.

Batch 6: name-keyed, and the scan targets exactly the registry this role designs
(planner -> task.json, evaluator -> evaluator.json) — with one single-writer
registry per role there is no other file the tool could be declared in, so the
old "scan all registry files" behaviour had no remaining purpose. The workspace
copy stays this session's source of truth (scanning the repo copy would still
match a component removed earlier in this session and would MISS one registered
earlier in this session).
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import parse_registry_file, write_registry_json

# role -> the registry it designs (single-writer file, batch 6)
_OWN_REGISTRY = {"planner": "task.json", "evaluator": "evaluator.json"}
_OWNING_DIR = {"planner": "task", "evaluator": "evaluator"}
_SEAT_OWNERSHIP = {
    ("planner", "plan"): ("task.json", "task"),
    ("planner", "self_improve"): ("planner.json", "planner"),
    ("evaluator", "self_improve"): ("evaluator.json", "evaluator"),
    ("planner", "legacy"): ("task.json", "task"),
    ("evaluator", "legacy"): ("evaluator.json", "evaluator"),
}


def _ownership(actx):
    return _SEAT_OWNERSHIP.get((actx.role, getattr(actx, "seat", "legacy")),
                               (_OWN_REGISTRY.get(actx.role), _OWNING_DIR.get(actx.role, actx.role)))


def tool_info():
    return {
        "name": "unregister_component",
        "description": (
            "DEEP delete: remove a registered tool from the registry you design and "
            "delete its source file. Applied to your workspace; committed with this "
            "session's patch after validation. Only tools your role may edit can be "
            "removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }


def tool_function(name, **kwargs):
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

    fn, owning_dir = _ownership(actx)
    if not fn:
        return f"Error: role '{role}' has no designable registry"
    rel = f"gan/registries/{fn}"
    src = broker.src_dir(role, node)
    ws_reg = os.path.join(src, rel)
    in_ws = os.path.isfile(ws_reg)
    ents, err = parse_registry_file(
        Path(ws_reg) if in_ws else Path(os.path.join(code_root, rel)))
    if err:
        if in_ws:
            # we wrote this file in-session: an unparseable workspace registry is
            # our own corruption, never "component not found"
            return f"Error: workspace registry is not parseable: {rel} ({err})"
        return f"Error: registry is not parseable: {rel} ({err})"
    match = [e for e in (ents or [])
             if isinstance(e, dict) and e.get("name") == name]
    if not match:
        return f"Error: tool not found: '{name}' in {rel}"
    if not frozen.is_allowed(role, rel, "modify", seat=getattr(actx, "seat", "legacy")):
        return f"Error: registry not editable for {role}: {rel}"
    mod = str(match[0].get("module") or "").replace("\\", "/").strip("/")
    # A registry entry is DATA, and with the workspace-first scan above it can come
    # from a file this session edited -- so never trust its path. Reject traversal
    # outright: the allowlist matches prefix globs, so it would accept
    # "gan/components/task/../../<outside>" and os.remove() would then delete a
    # file outside the workspace.
    if mod and (not mod.endswith(".py") or any(p == ".." for p in mod.split("/"))):
        return f"Error: unsafe module path in registry entry: {mod!r}"
    module_rel = f"gan/components/{mod}" if mod else ""
    if module_rel and not frozen.is_allowed(role, module_rel, "modify", seat=getattr(actx, "seat", "legacy")):
        return f"Error: component source not editable for {role}: {module_rel}"
    if mod and not mod.startswith(f"{owning_dir}/"):
        return (f"Error: module '{mod}' is outside the designed role's component tree "
                f"({owning_dir}/...)")

    # bring the registry (and module, if present) into the workspace.
    # ``if_absent`` never overwrites an existing workspace copy: re-granting the
    # registry would discard the removals (and edit_source edits) already applied
    # this session, and the resulting patch (entry restored, module still deleted)
    # is rejected by the registry gate. The MODULE is granted even when it is
    # absent from the workspace, so the deletion stays visible to the patch builder.
    broker.grant(role, node, [rel], intent="modify", reason="unregister", if_absent=True,
                 seat=getattr(actx, "seat", "legacy"))
    if module_rel:
        broker.grant(role, node, [module_rel], intent="modify", reason="unregister",
                     if_absent=True, seat=getattr(actx, "seat", "legacy"))

    ws_mod = os.path.join(src, module_rel) if module_rel else ""
    if ws_mod:
        # defence in depth (covers symlinks/other tricks): never delete outside src/
        src_real = os.path.realpath(src)
        ws_real = os.path.realpath(ws_mod)
        if ws_real != src_real and not ws_real.startswith(src_real + os.sep):
            return f"Error: module path escapes the workspace: {mod!r}"
    try:
        with open(ws_reg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error: cannot edit registry in workspace: {e}"
    data["components"] = [
        c for c in data.get("components", [])
        if not (isinstance(c, dict) and c.get("name") == name)
    ]
    write_registry_json(ws_reg, data)
    if ws_mod and os.path.isfile(ws_mod):
        os.remove(ws_mod)
    dctx.record("unregister_component", name=name, registry=fn, module=(mod or None))
    # batch 13 (U'): a still-selected name means the design claims a capability
    # whose removal is only SCHEDULED -- the two facts live on different
    # timelines (registry entry: patched; design slot: immediate) and are
    # calibrated only at the patch-exit heal / assembly report. Detection and
    # reminder only: the tool never edits the design itself (a rejected or
    # exhausted patch would leave the removal un-landed while the auto-deselection
    # persisted -- a permanent, unreported shadow).
    still_selected = False
    if dctx is not None and isinstance(getattr(dctx, "config", None), dict):
        tools = dctx.config.get("tools")
        if isinstance(tools, (list, tuple)):
            still_selected = name in [str(x) for x in tools]
    notice = (f" NOTE: '{name}' is still selected in your design (tools slot); "
              f"the removal above is only scheduled -- deselect it if that is "
              f"intended, otherwise the design and the registry will disagree "
              f"until the patch outcome is known."
              if still_selected else "")
    return (f"Scheduled removal of tool '{name}' ({rel}); it will be committed with "
            f"this session's patch after validation.{notice}")


op_info = tool_info
op_function = tool_function
