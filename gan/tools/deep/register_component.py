"""DEEP gate: register an existing tool module in the role's registry.

Symmetric to ``unregister_component``. Edits the designed role's **workspace**
copy of the registry (entry appended) rather than committing immediately -- the
change is folded into the session's patch and goes through the normal commit
validation (allowlist + compile + registry) together with the rest of the edits.

Only an *existing* module can be registered: creating the file is a separate
deep step (``request_source_access`` + ``edit_source``). "Existing" means the repo
copy **or** the workspace copy created this session -- this tool never creates,
restores or deletes source, it only records an entry for source that already
exists (so it is deliberately NOT the inverse of ``unregister_component``, which
deletes the file).

One registry per role (no shared file), name-keyed entries, and the
role-directory binding -- a registry entry may only reference the designed role's
own component tree, so the entry lands where its writer can write and its reader
can read.
"""
import filecmp
import json
import os
from pathlib import Path
from typing import Any, Dict

from gan.framework import frozen
from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import (
    module_sha12,
    entry_reason, parse_registry_file, resolve_module, write_registry_json,
)

# One registry per role, and each role designs exactly one --
# planner designs the TASK agent -> registers task tools; evaluator designs
# itself -> registers evaluator tools. planner.json has no writer (no slots).
_OWN_REGISTRY = {"planner": "task.json", "evaluator": "evaluator.json"}
# role -> the component directory its registry may reference (role-directory
# binding; the allowlist already enforces the same split, this is the message)
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

_DESCRIPTION_CAP = 300  # catalog metadata, not rhetoric


def _collision_sources(role_designed: str, code_root: str, base: str) -> str:
    """Repo-relative sources that already produce basename ``base`` ("" = none).

    ``assemble_tools_dir`` copies this role's always-on files and
    its registered components into ONE flat toolset directory keyed by file name
    (last writer wins, nothing detects the fight), so a component whose stem
    equals an always-on tool's stem would silently shadow that frozen plumbing
    tool at the next assembly. This enumerates the same universe the assembly
    copies from (``always_on_dirs`` + the committed registry's resolvable
    modules) and reports who already holds the basename. Invalid entries are
    included on purpose: their file occupies the basename today and a later
    hand-fix of the entry would make the collision real.
    """
    from gan.tools.assembly import always_on_dirs, py_files_in
    sources = []
    for d in always_on_dirs(role_designed, code_root=code_root):
        sources.extend(py_files_in(d))
    comp_dir = Path(code_root) / "gan" / "components"
    reg = load_designed_registry(role_designed, code_root)
    for e in reg.entries:
        if isinstance(e, dict) and e.get("module"):
            p = resolve_module(comp_dir, e.get("module"))
            if p is not None:
                sources.append(str(p))
    hits = sorted({os.path.relpath(s, code_root).replace(os.sep, "/")
                   for s in sources if os.path.basename(s) == base})
    return ", ".join(hits)


def load_designed_registry(role_designed: str, code_root: str):
    """The committed per-role registry of the DESIGNED role (tolerant load)."""
    from gan.registries.loader import load_registry_for_role
    return load_registry_for_role(
        role_designed,
        registry_dir=Path(code_root) / "gan" / "registries",
        components_dir=Path(code_root) / "gan" / "components",
    )


def _granted_coverage(broker, role, node, module_rel: str) -> bool:
    """True if this session's patch can carry ``module_rel``.

    Thin alias for ``AccessBroker.covers`` -- the **single definition** of the
    predicate, shared with ``list_components`` (which reports ``patch_covered`` per
    orphan). ``build_patch_from_workspace`` walks ``granted_paths`` only, so a NEW
    file is committable only when it -- or one of its ancestor directories -- was
    granted. Without this check the entry would be written for a file that never
    reaches the commit, and the failure would surface as a confusing "module file
    not found".
    """
    return broker.covers(role, node, module_rel)


def tool_info():
    return {
        "name": "register_component",
        "description": (
            "DEEP add: register an EXISTING tool module in your role's registry so it "
            "can be selected. The module file must already exist and be editable by "
            "your role -- either committed source, or a file you created this session "
            "with edit_source (in that case its directory must be granted, otherwise "
            "it cannot be committed). This tool never creates or restores source. "
            "Applied to your workspace; committed with this session's patch after "
            "validation. The module must live under your designed role's component "
            "tree (planner -> gan/components/task/** via task.json; evaluator -> "
            "gan/components/evaluator/** via evaluator.json). A stem that would "
            "collide with an always-on tool or another registered component at "
            "assembly is refused (both would fight for one toolset basename). Pass "
            "description= to author the catalog line (<=300 chars); re-registering "
            "the same content carries the committed description over."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Must equal the module file stem (without .py)."},
                "module": {"type": "string",
                           "description": "Path relative to gan/components, e.g. "
                                          "task/skills/foo.py"},
                "description": {"type": "string",
                                "description": "Optional catalog description "
                                               "(<=300 chars)."},
            },
            "required": ["name", "module"],
        },
    }


def tool_function(name, module, description=None, **kwargs):
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
    # and then be silently discarded. Refuse loudly instead.
    dctx = get_design_context()
    if dctx is None:
        return ("Error: deep registry changes are currently unavailable in this "
                "session; use `request_source_access` to view source. Do not retry.")

    # The registry is the one this role designs (single writer per file)
    reg_name, owning_dir = _ownership(actx)
    if not reg_name:
        return f"Error: role '{role}' has no designable registry"
    reg_rel = f"gan/registries/{reg_name}"

    mod = str(module or "").replace("\\", "/").strip("/")
    # never trust a caller-supplied path: the allowlist matches prefix globs, so
    # "gan/components/task/../../<outside>" would otherwise pass the gate below and
    # let the existence/identity checks read a file outside gan/components/
    if not mod.endswith(".py") or any(p == ".." for p in mod.split("/")):
        return "Error: module must be a .py path relative to gan/components"
    module_rel = f"gan/components/{mod}"

    # 1) role-directory binding (checked FIRST so the agent gets the most
    #     specific error): a registry entry may only reference its own role's
    #     component tree
    if not mod.startswith(f"{owning_dir}/"):
        return (f"Error: module '{mod}' is outside the designed role's component tree "
                f"({owning_dir}/...); {reg_name} entries must "
                f"reference gan/components/{owning_dir}/**")

    # 2) permission gate (same rule and order as unregister_component)
    if not frozen.is_allowed(role, reg_rel, "modify", seat=getattr(actx, "seat", "legacy")):
        return f"Error: registry not editable for {role}: {reg_rel}"
    if not frozen.is_allowed(role, module_rel, "modify", seat=getattr(actx, "seat", "legacy")):
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

    # Stamp the module version this entry's metadata is written
    # against. Only a METADATA write stamps (register/update) -- a module edited
    # on its own must NOT refresh the stamp, or the drift check could never fire.
    # The stamped file is the same one the validity checks below read
    # (workspace copy first), which is also the version this patch commits.
    module_file = ws_mod if in_ws else os.path.join(code_root, module_rel)

    # 3) identity contract: name == file stem (tools are keyed by stem, so a
    #    mismatch assembles but never loads). Friendly check first; entry_reason
    #    below is the backstop.
    stem = Path(mod).stem
    if str(name) != stem:
        return (f"Error: name '{name}' must equal the module file stem '{stem}' "
                f"(tools are keyed by file stem; the component would never load)")

    # 4) validate the entry as it would appear in the registry (role/dir + tool API).
    #    Validate the WORKSPACE copy whenever there is one: that is the version this
    #    session's patch commits, so validating the repo copy instead would reject a
    #    module the agent just extended with tool_info/tool_function and
    #    would accept one the agent just broke. ``entry_reason`` at commit time is the
    #    backstop for both directions.
    candidate = {"name": name, "module": mod}
    comp_dir = Path(src) / "gan" / "components"
    if not (comp_dir / mod).is_file():
        comp_dir = Path(code_root) / "gan" / "components"
    reason = entry_reason(candidate, comp_dir, owning_dir)
    if reason:
        return f"Error: invalid entry ({reason})"

    # 4.5) metadata validation: the description is catalog
    # metadata, not rhetoric -- refuse wrong type / over-length BEFORE anything
    # is granted or written (zero side effects on refusal).
    if description is not None and not isinstance(description, str):
        return "Error: description must be a string"
    if isinstance(description, str) and len(description) > _DESCRIPTION_CAP:
        return (f"Error: description too long ({len(description)} chars; "
                f"cap {_DESCRIPTION_CAP})")

    # 4.6) collision precheck. ``assemble_tools_dir`` copies this
    # role's always-on tools and its registered components into ONE flat toolset
    # directory keyed by file name (last writer wins, nothing detects the fight),
    # so a NEW component whose stem equals an always-on tool's stem would
    # silently shadow that frozen plumbing tool at the next assembly. Refuse
    # loudly here -- nothing has been granted or written yet. Skipped when the
    # name already exists: the duplicate scan below then delivers its precise
    # "Already registered" / different-module message instead.
    ws_reg = os.path.join(src, reg_rel)
    same_name_exists = False
    for _p in ([Path(ws_reg)] if os.path.isfile(ws_reg) else []) + \
              [Path(code_root) / "gan" / "registries" / reg_name]:
        _ents, _err = parse_registry_file(_p)
        if not _err and any(isinstance(x, dict) and x.get("name") == name
                            for x in (_ents or [])):
            same_name_exists = True
            break
    if not same_name_exists:
        holders = _collision_sources(owning_dir, code_root,
                                     f"{Path(mod).stem}.py")
        if holders:
            return (f"Error: cannot register '{name}': {Path(mod).stem}.py would "
                    f"collide with {holders} at assembly (both are copied into the "
                    f"role's toolset under the same file name; one silently "
                    f"disappears, and a component would shadow a frozen always-on "
                    f"tool). Pick a different module stem (name must equal the "
                    f"stem).")

    # 5) bring the registry into the workspace and append the entry (read-modify-write
    #    on the workspace copy; the rest of the file is preserved).
    #
    #    An ``if not isfile(ws_reg): grant`` guard would conflate
    #    "file exists in the workspace" with "path is patch-visible"
    #    (``build_patch_from_workspace`` walks ``granted_paths`` only): a workspace
    #    copy that was never granted (an ``edit_source`` scratch) would report
    #    success while the patch builder silently drops the whole registration.
    #    The grant decision now follows ``broker.covers`` (the single definition of
    #    patch visibility) and distinguishes three cases:
    #
    #    - covered AND the workspace copy exists: nothing to do (keep the session's
    #      edits -- this is what ``if_absent`` protects);
    #    - covered but the workspace copy is absent: ``grant(if_absent=True)`` copies
    #      the committed version in and keeps it patch-visible (normal first-touch);
    #    - NOT covered: the registry exists in the committed tree (the designed
    #      registry is part of the repo skeleton, so a plain grant copies
    #      the TRUTH over any scratch copy -- the resulting patch carries only the
    #      appended entry, not "delete every existing entry"). After the grant the
    #      path is patch-visible; if even then ``broker.covers`` says no, the state is
    #      genuinely unwritable (denied/oversized) and the tool refuses loudly with
    #      the gate's own reason instead of writing a registration it cannot commit.
    if not broker.covers(role, node, reg_rel):
        broker.grant(role, node, [reg_rel], intent="modify", reason="register",
                     seat=getattr(actx, "seat", "legacy"))
    if not os.path.isfile(ws_reg):
        broker.grant(role, node, [reg_rel], intent="modify", reason="register",
                     if_absent=True, seat=getattr(actx, "seat", "legacy"))
    if not broker.covers(role, node, reg_rel):
        denied = (getattr(broker, "last_result", {}) or {}).get("denied") or []
        why = f" (denied: {', '.join(denied[:3])})" if reg_rel in [str(d) for d in denied] else ""
        return (f"Error: cannot write {reg_rel}: the path is not patch-visible{why}. "
                f"Nothing was registered. Inspect it with request_source_access and "
                f"retry; do not re-create the file by hand.")
    try:
        with open(ws_reg, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error: cannot edit registry in workspace: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("components", []), list):
        return f"Error: registry is not an object with a 'components' list: {reg_rel}"
    comps = data.setdefault("components", [])
    for c in comps:
        if isinstance(c, dict) and c.get("name") == name:
            if str(c.get("module")).replace("\\", "/") == mod:
                return f"Already registered: '{name}' in {reg_rel}"
            return (f"Error: '{name}' already registered in {reg_rel} with a "
                    f"different module ({c.get('module')})")
    # Metadata preservation. A bare {"name","module"} append
    # silently strips the catalog description on every unregister->re-register
    # round trip. Carry the committed entry's description/params_schema over --
    # but ONLY when the implementation is unchanged (same module path,
    # byte-identical file): same bytes => same tool => the old description is
    # still true. A different implementation is a source modification; its
    # metadata must not ride along silently.
    new_entry: Dict[str, Any] = {"name": name, "module": mod}
    stamp = module_sha12(module_file)
    if stamp:
        new_entry["source_sha"] = stamp
    note = ""
    if description:
        new_entry["description"] = description
    else:
        prev_ents, _prev_err = parse_registry_file(
            Path(code_root) / "gan" / "registries" / reg_name)
        prev = next((e for e in (prev_ents or [])
                     if isinstance(e, dict) and e.get("name") == name), None)
        if prev is not None and \
                str(prev.get("module") or "").replace("\\", "/") == mod:
            ws_mod = os.path.join(src, module_rel)
            cand = ws_mod if os.path.isfile(ws_mod) \
                else os.path.join(code_root, module_rel)
            committed_mod = os.path.join(code_root, module_rel)
            same_impl = (os.path.realpath(cand) == os.path.realpath(committed_mod)
                         or filecmp.cmp(cand, committed_mod, shallow=False))
            if same_impl:
                carried = [f for f in ("description", "params_schema")
                           if prev.get(f) not in (None, "")]
                for f in carried:
                    new_entry[f] = prev[f]
                if carried:
                    note = (f" (description carried over from the committed entry: "
                            f"{', '.join(carried)})")
            else:
                note = (" Note: the committed implementation of this name differs "
                        "from the file you are registering; its description was "
                        "NOT carried. Pass description= here, or use "
                        "update_component once this patch commits.")
    comps.append(new_entry)
    write_registry_json(ws_reg, data)
    dctx.record("register_component", name=name, module=mod, registry=reg_name)
    # A planner self-improve session designs planner, while plan designs task.
    designed = getattr(dctx, "role", None)
    cross_note = ""
    if designed is not None and designed != owning_dir:
        cross_note = (f" NOTE: registered into {reg_name} for seat '{getattr(actx, 'seat', 'legacy')}' "
                      f"but the current design target is '{designed}'.")
    return (f"Registered tool '{name}' ({module_rel}) in {reg_rel}{note}. It will "
            f"be committed with this session's patch after validation. You can "
            f"select_component it right away (task design); the task agent actually "
            f"gets it only once the patch commits -- a rejected patch strips the "
            f"selection from the design before persisting.{cross_note}")


op_info = tool_info
op_function = tool_function
