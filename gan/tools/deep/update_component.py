"""DEEP gate: update an existing registry entry's catalog metadata.

Companion to ``register_component`` (add) and ``unregister_component`` (delete):
the metadata of a LIVE entry had no sanctioned update path. ``register_component``
answers "Already registered" for an existing name, and raw ``edit_source`` writes
to the registry JSON are unvalidated at edit time, carry no structured record of
the semantic change, and are unguided (an ungranted registry copy is even
silently dropped by the patch builder). This tool is the sanctioned editor for
exactly that job: validate up front, record structurally, ride the normal patch
channel.

What it deliberately does NOT do:

- change ``module`` -- re-pointing an entry at a different file is a source-level
  change: edit the granted module with ``edit_source`` (the entry and its metadata
  survive untouched), or unregister and register anew;
- resurrect an entry removed this session -- the workspace registry copy is the
  session's source of truth (the same authority ``unregister_component`` scans
  with); a name missing from it is "not found", never re-added from the
  committed tree;
- touch anything but the designed role's own registry file (batch 6: one
  single-writer catalog per role).
"""
import json
import os
from pathlib import Path

from gan.framework import frozen
from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import (entry_reason, parse_registry_file,
                                   resolve_module, write_registry_json)

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
# mirrors register_component's cap: catalog metadata, not rhetoric
_DESCRIPTION_CAP = 300


def tool_info():
    return {
        "name": "update_component",
        "description": (
            "DEEP metadata update: change the catalog description (<=300 chars, "
            "empty string clears it) and/or params_schema of an EXISTING tool in "
            "your role's registry. This does NOT change what the tool is or does "
            "-- to modify a tool's source, edit the granted module with "
            "edit_source (the registry entry survives untouched); the entry's "
            "module path cannot be re-pointed here. Applied to your workspace; "
            "committed with this session's patch after validation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string",
                                "description": "New catalog description; empty "
                                               "string clears it. Omit to keep."},
                "params_schema": {"type": "object",
                                  "description": "New params_schema object. "
                                                 "Omit to keep."},
            },
            "required": ["name"],
        },
    }


def tool_function(name, description=None, params_schema=None, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    role = actx.role
    broker = actx.broker
    node = actx.node_id
    code_root = broker.repo_root

    # A deep write is only meaningful in a session that has a patch channel
    # (R1 -- same guard as register_component / unregister_component).
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

    # -- input validation BEFORE anything is granted or written ------------------
    if description is not None and not isinstance(description, str):
        return "Error: description must be a string"
    if isinstance(description, str) and len(description) > _DESCRIPTION_CAP:
        return (f"Error: description too long ({len(description)} chars; "
                f"cap {_DESCRIPTION_CAP})")
    if params_schema is not None and not isinstance(params_schema, dict):
        return "Error: params_schema must be an object (dict)"
    if description is None and params_schema is None:
        return "Error: nothing to update (pass description= and/or params_schema=)"

    if not frozen.is_allowed(role, rel, "modify", seat=getattr(actx, "seat", "legacy")):
        return f"Error: registry not editable for {role}: {rel}"

    # -- find the entry with the session's authority (workspace copy first,
    #    committed tree second -- the same rule unregister_component scans with) --
    in_ws = os.path.isfile(ws_reg)
    ents, err = parse_registry_file(
        Path(ws_reg) if in_ws else Path(os.path.join(code_root, rel)))
    if err:
        if in_ws:
            # we (or the agent) wrote this file in-session: our corruption
            return f"Error: workspace registry is not parseable: {rel} ({err})"
        return f"Error: registry is not parseable: {rel} ({err})"
    if not any(isinstance(e, dict) and e.get("name") == name for e in (ents or [])):
        return f"Error: tool not found: '{name}' in {rel}"

    # -- bring the registry into the workspace: covers-guided grant dance,
    #    identical to register_component (``broker.covers`` is the single
    #    definition of patch visibility; ``if_absent`` protects in-session edits)
    if not broker.covers(role, node, rel):
        broker.grant(role, node, [rel], intent="modify", reason="update",
                     seat=getattr(actx, "seat", "legacy"))
    if not os.path.isfile(ws_reg):
        broker.grant(role, node, [rel], intent="modify", reason="update",
                     if_absent=True, seat=getattr(actx, "seat", "legacy"))
    if not broker.covers(role, node, rel):
        return (f"Error: cannot write {rel}: the path is not patch-visible. "
                f"Nothing was updated. Inspect it with request_source_access and "
                f"retry; do not re-create the file by hand.")

    try:
        with open(ws_reg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error: cannot edit registry in workspace: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("components", []), list):
        return f"Error: registry is not an object with a 'components' list: {rel}"
    match = [e for e in data.get("components", [])
             if isinstance(e, dict) and e.get("name") == name]
    if not match:
        # the workspace copy is this session's truth and it does not declare the
        # name (removed this session) -- never resurrect it from the committed tree
        return (f"Error: tool not found: '{name}' in {rel} (the workspace copy does "
                f"not declare it; re-register it with register_component first)")
    entry = match[0]

    changed = []
    if description is not None:
        if description:
            entry["description"] = description
        else:
            entry.pop("description", None)
        changed.append("description")
    if params_schema is not None:
        entry["params_schema"] = params_schema
        changed.append("params_schema")
    # backstop (entries are DATA): the updated entry must still be a valid one.
    # A metadata edit cannot break the identity/role contracts, but validate the
    # result anyway across the workspace-first search path.
    # B30 (L1): updating the metadata is the act of declaring "this description
    # matches the CURRENT module", so refresh the stamp here -- this is the only
    # way the drift flag clears. If the module cannot be resolved the entry is
    # already invalid (reported by preflight); leave the previous stamp alone.
    from gan.registries.loader import module_sha12
    mod_path = resolve_module([Path(src) / "gan" / "components",
                               Path(code_root) / "gan" / "components"],
                              entry.get("module"))
    stamp = module_sha12(mod_path) if mod_path else None
    if stamp:
        entry["source_sha"] = stamp
    reason = entry_reason(entry,
                          [Path(src) / "gan" / "components",
                           Path(code_root) / "gan" / "components"],
                          owning_dir)
    if reason:
        return f"Error: update would produce an invalid entry ({reason})"
    write_registry_json(ws_reg, data)
    dctx.record("update_component", name=name, registry=fn, fields=changed,
                description_chars=(len(description)
                                   if isinstance(description, str) else None))
    return (f"Updated '{name}' in {rel} ({', '.join(changed)}). It will be "
            f"committed with this session's patch after validation.")


op_info = tool_info
op_function = tool_function
