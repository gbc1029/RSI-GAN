"""Work tool: enumerate the components a role can select (registry + slot state).

Roles do not know their options a priori: which ``skills`` / ``eval_points`` exist
is registry data, not prompt data. ``list_components`` answers, in one read-only
call:

- ``registered``   : the merged registry (shared + role) **union** the committed
  baseline, with the exact name to pass to ``select_component``;
- ``selected``     : what the *current* design config already selects per slot;
- ``problems``     : registry problems (invalid entries, duplicates) -- an entry
  listed here cannot be selected until the registry/component is fixed;
- ``unregistered`` : component *files* present under ``gan/components`` that no
  registry file declares (orphans) -- candidates for ``register_component``, NOT
  selectable until registered.

**Which role's catalog** (H10). The registry that matters is the one
``select_component`` consults -- the **design target** (``DesignContext.role``), not
the access role. In the planner's *plan* session those differ (access ``planner``,
design target ``task``); keying the catalog off the access role made the planner
list its own (empty) catalog while every task component it may select stayed
invisible. Both roles are reported: ``design_role`` (catalog) and ``access_role``
(session/workspace).

**Two roots** (B18/P3 residue). A session edits a **workspace** copy of the paths it
granted; that copy is what the patch commits. So each registry file and each
component module resolves **workspace-first, committed-tree second** -- the same
rule the deep tools scan with -- and the output says which copy each item came
from. The workspace is only a *partial* copy, so the committed tree is the fallback
and never disappears from the listing.

Per-entry fields (``registered``):

- ``origin``     : ``"workspace"`` (the session's copy declares it) or ``"code"``;
- ``pending``    : ``"added"`` / ``"removed"`` / ``"modified"`` / ``"none"`` --
  how the session's workspace differs from the committed baseline;
- ``valid`` / ``reason`` : validity of the copy the agent will act on (workspace
  copy when one exists, else the committed copy); for ``pending:"removed"`` it
  describes the committed copy, which is the one that still exists;
- ``selectable`` : **exactly what ``select_component`` will do now** (it reads the
  committed registry), so a component registered this session is not selectable
  until the patch lands, and a component scheduled for removal still is;
- ``note``       : the human-readable form of any tension between the above.

``patch_covered`` on an ``unregistered`` candidate is the same predicate
``register_component`` enforces: a NEW file reaches the commit only if it (or an
ancestor directory) was granted (``AccessBroker.covers``). Reported here so the
agent learns it *before* attempting the registration.

Identity contract (do not break it): a component's registered ``name``, its module
file stem, and the LLM-callable tool name MUST be the same string. The tool loop
keys tools by file stem, so a mismatch assembles a component that silently never
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
    _REGISTRY_FILES,
    entry_reason,
    load_registry_for_role,
    orphan_modules,
    parse_registry_file,
    resolve_registry_file,
    safe_module_rel,
    validate_registry,
)

_KINDS = ("skill", "eval_point")
# design-config slot -> the registry kind it selects from (planner has no slot)
_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "list_components",
        "description": (
            "List the components YOU may select: the role registry ('registered', "
            "with each entry's validity, where it came from and whether this "
            "session's workspace added/removed/modified it), what the current design "
            "config already selects ('selected'), registry 'problems' "
            "(invalid/duplicate entries), and component files that exist but are "
            "unregistered ('unregistered' -- candidates for register_component, with "
            "'patch_covered' telling you whether the file can reach the commit at "
            "all). 'design_role' is the catalog this lists; 'access_role' owns the "
            "workspace. 'selectable' mirrors exactly what select_component will "
            "accept right now. Pass the reported 'name' verbatim to "
            "select_component: the registered name, the module file stem and the "
            "callable tool name are the same string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": list(_KINDS)}},
        },
    }


def tool_function(kind=None, **kwargs):
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
    if kind not in (None,) + _KINDS:
        return f"Error: unknown kind '{kind}' (expected one of: {', '.join(_KINDS)})"

    dctx = get_design_context()
    # H10: the catalog that matters is the one `select_component` consults, i.e. the
    # DESIGN TARGET role -- not the access role. The planner's plan session designs
    # the task agent, so its catalog is task.json (+ shared.json); keying this off
    # the access role listed planner.json instead and hid every task component.
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
    # `pending` is the difference, and `selectable` must mirror the committed one
    # because that is what select_component reads.
    code_reg = load_registry_for_role(design_role, registry_dir=code_reg_dir,
                                      components_dir=code_comp_dir)
    eff_reg = load_registry_for_role(design_role, registry_dir=code_reg_dir,
                                     components_dir=code_comp_dir, overlay_root=overlay)

    def _by_key(reg):
        return {(str(e.get("kind")), str(e.get("name"))): e
                for e in reg.entries if isinstance(e, dict)}

    code_by_key = _by_key(code_reg)
    eff_by_key = _by_key(eff_reg)
    ws_keys = set()
    if ws_reg_dir:
        # only the two files that actually merge into the design role's view: a
        # same-named entry in another role's registry is not this entry's origin
        for fn in ("shared.json", f"{design_role}.json"):
            ents, err = parse_registry_file(os.path.join(ws_reg_dir, fn))
            if err:
                continue
            for e in ents or []:
                if isinstance(e, dict):
                    ws_keys.add((str(e.get("kind")), str(e.get("name"))))

    def _entry_changed(a, b) -> bool:
        return any(str(a.get(f) or "") != str(b.get(f) or "")
                   for f in ("kind", "module", "description"))

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
        "added": ("registered in this outer's workspace; selectable after this "
                  "session's patch commits"),
        "removed": ("scheduled for removal in this outer's workspace; the committed "
                    "copy is still selectable until the patch lands"),
        "modified": "this outer's workspace copy differs from the committed one",
    }

    def _selectable(k):
        """Mirror `select_component`: committed registry presence + validity."""
        e = code_by_key.get(k)
        if e is None:
            return False, "not in the committed registry (selectable after the patch commits)"
        r = entry_reason(e, code_reg.components_dir)
        return (r is None), (r or "")

    # -- registered (union: effective first, then pending removals) ---------
    registered = []

    def _append(k, entry, pending):
        ok, sreason = _selectable(k)
        reason = entry_reason(entry, eff_reg.components_dir if pending != "removed"
                              else code_reg.components_dir)
        registered.append({
            "name": str(entry.get("name")),
            "kind": str(entry.get("kind")),
            "module": str(entry.get("module")),
            "valid": reason is None,
            "reason": reason,
            "description": str(entry.get("description") or ""),
            "origin": "workspace" if k in ws_keys else "code",
            "pending": pending,
            "selectable": ok,
            "selectable_reason": None if ok else sreason,
            "note": _PENDING_NOTE.get(pending),
        })

    for e in eff_reg.entries:
        if not isinstance(e, dict):
            continue
        k = (str(e.get("kind")), str(e.get("name")))
        if kind is not None and k[0] != kind:
            continue
        if k not in code_by_key:
            _append(k, e, "added")
        elif _entry_changed(code_by_key[k], e) or _module_differs(e.get("module")):
            _append(k, e, "modified")
        else:
            _append(k, e, "none")
    for k, e in code_by_key.items():
        if k in eff_by_key or (kind is not None and k[0] != kind):
            continue
        _append(k, e, "removed")

    # -- selected (design config) ------------------------------------------
    cfg = getattr(dctx, "config", None)
    selected = {}
    if isinstance(cfg, dict):
        for slot in _SLOT_KIND:
            if slot in cfg:
                value = cfg.get(slot)
                selected[slot] = [str(x) for x in value] if isinstance(value, (list, tuple)) else []

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
    sources = {}
    for fn in _REGISTRY_FILES:
        path, origin = resolve_registry_file(fn, code_reg_dir, overlay_root=overlay)
        _ents, err = parse_registry_file(path)
        sources[fn] = {"origin": origin, "parse_error": err}

    notes = []
    for fn, info in sources.items():
        if info["parse_error"]:
            notes.append(
                f"registry file {fn} is unparseable in the {info['origin']} copy "
                f"({info['parse_error']}); the entries it declares are NOT listed below "
                f"and its modules may appear as orphans -- fix it before "
                f"registering/selecting")
    removed_n = sum(1 for r in registered if r["pending"] == "removed")
    if removed_n:
        notes.append(f"{removed_n} component(s) are scheduled for removal in this outer's "
                     f"workspace; they stay selectable from the committed registry until "
                     f"the patch lands")
    added_n = sum(1 for r in registered if r["pending"] == "added")
    if added_n:
        notes.append(f"{added_n} component(s) are registered only in this outer's "
                     f"workspace: the entry becomes selectable once the patch commits")
    uncovered = [u for u in unregistered if not u["patch_covered"]]
    if uncovered:
        notes.append(f"{len(uncovered)} unregistered file(s) lie outside every granted "
                     f"path: registering them cannot reach the commit until their parent "
                     f"directory is granted")
    dangling = [f"{slot}:{n}" for slot, names in selected.items() for n in names
                if ({"skills": "skill", "eval_points": "eval_point"}[slot], n) not in eff_by_key]
    if dangling:
        notes.append("selected but NOT in the registry (assembly will skip them): "
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
