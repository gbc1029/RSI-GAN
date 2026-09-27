"""Work tool: enumerate the tools a role can select (registry + slot state).

Roles do not know their options a priori: which tools exist is registry data, not
prompt data. ``list_components`` answers, in one read-only call:

- ``registered``   : the role's registry **union** the committed baseline, with
  the exact name to pass to ``select_component``;
- ``selected``     : what the *current* design config already selects
  (the single ``tools`` slot);
- ``problems``     : registry problems (invalid entries, within-file duplicates);
- ``unregistered`` : tool *files* present under the role's component tree that no
  registry declares (orphans) -- candidates for ``register_component``, NOT
  selectable until registered.

**Which role's catalog** (H10). The registry that matters is the one
``select_component`` consults -- the **design target** (``DesignContext.role``), not
the access role. In the planner's *plan* session those differ (access ``planner``,
design target ``task``); keying the catalog off the access role made the planner
list its own (empty) catalog while every task tool it may select stayed invisible.
Both roles are reported: ``design_role`` (catalog) and ``access_role``
(session/workspace).

**Two roots** (B18/P3 residue). A session edits a **workspace** copy of the paths it
granted; that copy is what the patch commits. So the registry file and each tool
module resolve **workspace-first, committed-tree second** -- the same rule the deep
tools scan with -- and the output says which copy each item came from. The
workspace is only a *partial* copy, so the committed tree is the fallback and never
disappears from the listing. (Batch 6: one registry file per role -- no shared file,
no cross-file merging -- so the "origin" is simply which copy of that one file won.)

Per-entry fields (``registered``):

- ``origin``     : ``"workspace"`` (the session's copy declares it) or ``"code"``;
- ``pending``    : ``"added"`` / ``"removed"`` / ``"modified"`` / ``"none"`` --
  how the session's workspace differs from the committed baseline;
- ``valid`` / ``reason`` : validity of the copy the agent will act on (workspace
  copy when one exists, else the committed copy); for ``pending:"removed"`` it
  describes the committed copy, which is the one that still exists;
- ``selectable`` : **exactly what ``select_component`` will do now** (batch 5:
  the effective, workspace-first registry for the task design -- the design is
  healed against the committed tree before persist, so same-session selection is
  safe -- and the committed registry for role self-designs, which are not healed
  yet), so a tool registered this session IS selectable for the task design,
  and a tool scheduled for removal is no longer accepted;
- ``note``       : the human-readable form of any tension between the above.

``patch_covered`` on an ``unregistered`` candidate is the same predicate
``register_component`` enforces: a NEW file reaches the commit only if it (or an
ancestor directory) was granted (``AccessBroker.covers``). Reported here so the
agent learns it *before* attempting the registration.

Identity contract (do not break it): a tool's registered ``name``, its module
file stem, and the LLM-callable tool name MUST be the same string. The tool loop
keys tools by file stem, so a mismatch assembles a tool that silently never
loads. This tool therefore reports the registry ``name`` verbatim -- after the
identity unification that is already the callable name.

Read-only: this tool never grants access and never writes anything.
"""
from __future__ import annotations

import filecmp
import json
import os

from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import (
    entry_reason,
    load_registry_for_role,
    orphan_modules,
    parse_registry_file,
    resolve_registry_file,
    safe_module_rel,
    validate_registry,
)


def tool_info():
    return {
        "name": "list_components",
        "description": (
            "List the tools YOU may select: the role registry ('registered', with each "
            "entry's validity, where it came from and whether this session's workspace "
            "added/removed/modified it), what the current design config already selects "
            "('selected', the 'tools' slot), registry 'problems', and tool files that "
            "exist but are unregistered ('unregistered' -- candidates for "
            "register_component, with 'patch_covered' telling you whether the file can "
            "reach the commit at all). 'design_role' is the catalog this lists; "
            "'access_role' owns the workspace. 'selectable' mirrors exactly what "
            "select_component will accept right now. Pass the reported 'name' verbatim "
            "to select_component: the registered name, the module file stem and the "
            "callable tool name are the same string."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


def tool_function(**kwargs):
    actx = get_access_context()
    if actx is None:
        return "Error: no access context"
    access_role = getattr(actx, "role", None)
    if not access_role:
        return "Error: no role in access context"
    broker = getattr(actx, "broker", None)
    root = getattr(broker, "repo_root", None)
    if not root:
        return "Error: no code root available"

    dctx = get_design_context()
    # H10: the catalog that matters is the one `select_component` consults, i.e. the
    # DESIGN TARGET role -- not the access role. The planner's plan session designs
    # the task agent, so its catalog is task.json; keying this off the access role
    # listed planner.json instead and hid every task tool.
    design_role = getattr(dctx, "role", None) or access_role
    patch_channel = dctx is not None

    # -- roots -------------------------------------------------------------
    node = getattr(actx, "node_id", None)
    ws = None
    try:
        ws = broker.src_dir(access_role, node)
    except Exception:  # noqa: BLE001 -- a stub broker without a workspace: fall back
        ws = None
    ws_gan = os.path.join(ws, "gan") if ws else None
    overlay = ws if (ws_gan and os.path.isdir(ws_gan)) else None
    ws_reg_dir = os.path.join(overlay, "gan", "registries") if overlay else None
    ws_comp_dir = os.path.join(overlay, "gan", "components") if overlay else None
    code_reg_dir = os.path.join(str(root), "gan", "registries")
    code_comp_dir = os.path.join(str(root), "gan", "components")

    # committed baseline vs the effective (workspace-first) view. Both are needed:
    # `pending` is the difference, and `selectable` must mirror select_component.
    code_reg = load_registry_for_role(design_role, registry_dir=code_reg_dir,
                                      components_dir=code_comp_dir)
    eff_reg = load_registry_for_role(design_role, registry_dir=code_reg_dir,
                                     components_dir=code_comp_dir, overlay_root=overlay)

    def _by_key(reg):
        return {str(e.get("name")): e for e in reg.entries if isinstance(e, dict)}

    code_by_key = _by_key(code_reg)
    eff_by_key = _by_key(eff_reg)
    ws_declares = False
    if ws_reg_dir:
        ents, _err = parse_registry_file(os.path.join(ws_reg_dir, f"{design_role}.json"))
        ws_declares = isinstance(ents, list)

    def _entry_changed(a, b) -> bool:
        return any(str(a.get(f) or "") != str(b.get(f) or "")
                   for f in ("module", "description"))

    def _module_differs(mod) -> bool:
        """True when the workspace copy of a module differs from the committed one."""
        rel = safe_module_rel(mod)
        if rel is None or not (ws_comp_dir and code_comp_dir):
            return False
        a, b = os.path.join(code_comp_dir, rel), os.path.join(ws_comp_dir, rel)
        if not os.path.isfile(b):
            return False  # no workspace copy: this session changed nothing
        if not os.path.isfile(a):
            return True  # module created this session
        try:
            return not filecmp.cmp(a, b, shallow=False)
        except OSError:
            return False

    _PENDING_NOTE = {
        "added": ("registered in this outer's workspace; selectable now -- the task "
                  "agent actually gets it only when this session's patch commits "
                  "(a rejected patch strips the selection before persist)"),
        "removed": ("scheduled for removal in this outer's workspace; select_component "
                    "no longer accepts it"),
        "modified": "this outer's workspace copy differs from the committed one",
    }

    # Batch 5: `selectable` mirrors exactly what select_component accepts, and its
    # authority changed with P-3: the task design is overlay-validated AND healed
    # before persist (heal_design_slots), so its mirror is the effective view;
    # role self-designs are not healed yet and keep the committed-only authority.
    _sel_effective = (design_role == "task")

    def _selectable(name):
        if _sel_effective:
            e = eff_by_key.get(name)
            if e is None:
                return False, "not in the effective registry (select_component would reject it)"
            r = entry_reason(e, eff_reg.components_dir, design_role)
        else:
            e = code_by_key.get(name)
            if e is None:
                return False, "not in the committed registry (select_component would reject it)"
            r = entry_reason(e, code_reg.components_dir, design_role)
        return (r is None), (r or "")

    # -- registered (union: effective first, then pending removals) ---------
    registered = []

    def _append(name, entry, pending):
        ok, sreason = _selectable(name)
        reason = entry_reason(entry, eff_reg.components_dir if pending != "removed"
                              else code_reg.components_dir, design_role)
        registered.append({
            "name": str(entry.get("name")),
            "kind": str(entry.get("kind") or ""),   # legacy descriptive tag
            "module": str(entry.get("module")),
            "valid": reason is None,
            "reason": reason,
            "description": str(entry.get("description") or ""),
            "origin": "workspace" if ws_declares else "code",
            "pending": pending,
            "selectable": ok,
            "selectable_reason": None if ok else sreason,
            "note": _PENDING_NOTE.get(pending),
        })

    for e in eff_reg.entries:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name"))
        if name not in code_by_key:
            _append(name, e, "added")
        elif _entry_changed(code_by_key[name], e) or _module_differs(e.get("module")):
            _append(name, e, "modified")
        else:
            _append(name, e, "none")
    for name, e in code_by_key.items():
        if name in eff_by_key:
            continue
        _append(name, e, "removed")

    # -- selected (design config, the single ``tools`` slot) ----------------
    cfg = getattr(dctx, "config", None)
    selected = {}
    if isinstance(cfg, dict):
        value = cfg.get("tools")
        selected["tools"] = [str(x) for x in value] if isinstance(value, (list, tuple)) else []

    # -- unregistered (orphan candidates) ----------------------------------
    _covers = getattr(broker, "covers", None)

    def _patch_covered(rel: str) -> bool:
        if os.path.isfile(os.path.join(code_comp_dir, rel)):
            return True  # already committed: nothing has to be carried by the patch
        if _covers is None:
            return True
        return bool(_covers(access_role, node, f"gan/components/{rel}"))

    unregistered = []
    for rel in orphan_modules(code_reg_dir, code_comp_dir, overlay_root=overlay):
        in_ws = bool(ws_comp_dir) and os.path.isfile(os.path.join(ws_comp_dir, rel))
        covered = _patch_covered(rel)
        item = {
            "name": os.path.splitext(os.path.basename(rel))[0],
            "module": rel,
            "origin": "workspace" if in_ws else "code",
            "patch_covered": covered,
        }
        if not covered:
            item["note"] = ("not covered by any granted path: register_component would "
                            "refuse it because the file cannot reach the commit -- grant "
                            "its parent directory first "
                            "(request_source_access(..., intent='modify'))")
        unregistered.append(item)

    problems = validate_registry(design_role, registry_dir=code_reg_dir,
                                 components_dir=code_comp_dir, overlay_root=overlay)
    problems = [p for p in problems if p.get("type") != "orphan"]

    # -- provenance + warnings ---------------------------------------------
    path, origin = resolve_registry_file(f"{design_role}.json", code_reg_dir,
                                         overlay_root=overlay)
    _ents, err = parse_registry_file(path)
    sources = {f"{design_role}.json": {"origin": origin, "parse_error": err}}

    notes = []
    if err:
        notes.append(
            f"registry file {design_role}.json is unparseable in the {origin} copy "
            f"({err}); the entries it declares are NOT listed below and its modules "
            f"may appear as orphans -- fix it before registering/selecting")
    removed_n = sum(1 for r in registered if r["pending"] == "removed")
    if removed_n:
        notes.append(f"{removed_n} tool(s) are scheduled for removal in this outer's "
                     f"workspace; select_component no longer accepts them")
    added_n = sum(1 for r in registered if r["pending"] == "added")
    if added_n:
        notes.append(f"{added_n} tool(s) are registered only in this outer's "
                     f"workspace: selectable now, but the task agent gets them only "
                     f"when the patch commits")
    uncovered = [u for u in unregistered if not u["patch_covered"]]
    if uncovered:
        notes.append(f"{len(uncovered)} unregistered file(s) lie outside every granted "
                     f"path: registering them cannot reach the commit until their parent "
                     f"directory is granted")
    sel_by_key = eff_by_key if _sel_effective else code_by_key
    dangling = [f"tools:{n}" for n in (selected.get("tools") or []) if n not in sel_by_key]
    if dangling:
        if _sel_effective:
            notes.append("selected but NOT resolvable from the effective registry -- the "
                         "framework strips these from the design before persisting "
                         "(design_dangling_stripped): " + ", ".join(sorted(dangling)))
        else:
            notes.append("selected but NOT in the committed registry (assembly will skip "
                         "them; role designs are not healed yet): "
                         + ", ".join(sorted(dangling)))
    if not patch_channel:
        notes.append("no patch channel in this session (evaluate): deep registry/source "
                     "changes made now would be discarded -- do not attempt them")

    return json.dumps({
        # `role` is kept as the back-compat alias of `design_role` (the catalog owner)
        "role": design_role,
        "design_role": design_role,
        "access_role": access_role,
        "patch_channel": patch_channel,
        "roots": {"code": str(root), "workspace": overlay},
        "registry_sources": sources,
        "selected": selected,
        "registered": registered,
        "unregistered": unregistered,
        "problems": problems,
        "notes": notes,
    }, ensure_ascii=False, indent=2)


op_info = tool_info
op_function = tool_function
