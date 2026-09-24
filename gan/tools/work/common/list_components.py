"""Work tool: enumerate the components a role can select (registry + slot state).

Roles do not know their options a priori: which ``skills`` / ``eval_points`` exist
is registry data, not prompt data. ``list_components`` answers, in one read-only
call:

- ``registered``   : every entry of the role's merged registry (shared + role), with
  the exact name to pass to ``select_component``;
- ``selected``     : what the *current* design config already selects per slot;
- ``problems``     : registry problems (invalid entries, duplicates) -- an entry
  listed here cannot be selected until the registry/component is fixed;
- ``unregistered`` : component *files* present under ``gan/components`` that no
  registry file declares (orphans) -- candidates for ``register_component``, NOT
  selectable until registered.

Identity contract (do not break it): a component's registered ``name``, its module
file stem, and the LLM-callable tool name MUST be the same string. The tool loop
keys tools by file stem, so a mismatch assembles a component that silently never
loads. This tool therefore reports the registry ``name`` verbatim -- after the
identity unification that is already the callable name.

Read-only: this tool never grants access and never writes anything.
"""
from __future__ import annotations

import json
import os

from gan.framework.context import get_access_context, get_design_context
from gan.registries.loader import entry_reason, load_registry_for_role, validate_registry

_KINDS = ("skill", "eval_point")
# design-config slot -> the registry kind it selects from (planner has no slot)
_SLOT_KIND = {"skills": "skill", "eval_points": "eval_point"}


def tool_info():
    return {
        "name": "list_components",
        "description": (
            "List the components YOU may select: the role registry ('registered', "
            "with each entry's validity), what the current design config already "
            "selects ('selected'), registry 'problems' (invalid/duplicate entries), "
            "and component files that exist but are unregistered ('unregistered' -- "
            "candidates for register_component). Pass the reported 'name' verbatim "
            "to select_component: the registered name, the module file stem and the "
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
    role = getattr(actx, "role", None)
    if not role:
        return "Error: no role in access context"
    root = getattr(getattr(actx, "broker", None), "repo_root", None)
    if not root:
        return "Error: no code root available"
    if kind not in (None,) + _KINDS:
        return f"Error: unknown kind '{kind}' (expected one of: {', '.join(_KINDS)})"

    registry_dir = os.path.join(str(root), "gan", "registries")
    components_dir = os.path.join(str(root), "gan", "components")
    reg = load_registry_for_role(role, registry_dir=registry_dir, components_dir=components_dir)

    registered = []
    for entry in reg.list(kind):
        reason = entry_reason(entry, reg.components_dir)
        registered.append({
            "name": str(entry.get("name")),
            "kind": str(entry.get("kind")),
            "module": str(entry.get("module")),
            "valid": reason is None,
            "reason": reason,
            "description": str(entry.get("description") or ""),
        })

    dctx = get_design_context()
    cfg = getattr(dctx, "config", None)
    selected = {}
    if isinstance(cfg, dict):
        for slot in _SLOT_KIND:
            if slot in cfg:
                value = cfg.get(slot)
                selected[slot] = [str(x) for x in value] if isinstance(value, (list, tuple)) else []

    problems = validate_registry(role, registry_dir=registry_dir, components_dir=components_dir)
    return json.dumps({
        "role": role,
        "selected": selected,
        "registered": registered,
        "problems": [p for p in problems if p.get("type") != "orphan"],
        "unregistered": [p for p in problems if p.get("type") == "orphan"],
    }, ensure_ascii=False, indent=2)


op_info = tool_info
op_function = tool_function
