"""Work tool: workspace-confined source editor for planner/evaluator (DEEP).

Deep changes mean editing granted source. To keep the gate meaningful, edits are
CONFINED to the instance's granted workspace ``<workspace>/src`` (the files the
role explicitly obtained via ``request_source_access``). Any path that escapes
that root is rejected. The resulting diff is turned into a patch by
``gan/patch.py``: a plan session's patch lands on the next task generation, a
self_improve session's patch is committed to the code tree for the next outer.

Raw ``bash`` is intentionally NOT granted to roles: it cannot be confined to the
workspace. Only the base ``editor`` capabilities are exposed, path-checked here.
"""
import os
from typing import Optional

from agent.tools import edit as _edit
from gan.framework.context import get_access_context, get_design_context
from gan.framework.write_auth import authorize_write, resolve_workspace_path


def tool_info():
    return {
        "name": "edit_source",
        "description": (
            "View/create/edit files in YOUR granted source workspace (the files you obtained "
            "via request_source_access). Commands: view, create, str_replace, insert, undo_edit. "
            "Paths must stay inside your workspace src root; escapes are rejected. This is the "
            "DEEP edit surface (new component/logic)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "enum": ["view", "create", "str_replace", "insert", "undo_edit"]},
                "path": {"type": "string",
                         "description": "Path inside your workspace src root (relative or absolute)."},
                "file_text": {"type": "string"},
                "insert_line": {"type": "integer"},
                "new_str": {"type": "string"},
                "old_str": {"type": "string"},
                "view_range": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["command", "path"],
        },
    }


def _root(actx) -> str:
    return os.path.realpath(actx.broker.src_dir(actx.role, actx.node_id))


def _resolve(actx, path: str) -> str:
    return resolve_workspace_path(actx, path)


def _registered_component_note(abs_path: str) -> str:
    """One-line NOTE when the edited file IS a registered component.

    Advisory, best-effort, and READ-ONLY: an exception here must never break the
    edit. Scans ALL role registries rather than the caller's own -- "is this file
    a catalog entry?" is a catalog fact, independent of who is editing (and this
    avoids re-introducing the role-vs-target mismatch).
    """
    try:
        from gan.framework.context import session_overlay_root
        from gan.registries.loader import load_registry_for_role, module_sha12
        target = os.path.realpath(abs_path)
        for role in ("task", "planner", "evaluator"):
            reg = load_registry_for_role(role, overlay_root=session_overlay_root())
            for e in reg.entries:
                if not isinstance(e, dict):
                    continue
                mod_path = reg.module_path(str(e.get("name")))
                if not mod_path or os.path.realpath(str(mod_path)) != target:
                    continue
                stamped = e.get("source_sha")
                current = module_sha12(abs_path)
                if not stamped:
                    detail = "it carries no catalog stamp yet"
                elif current and current != str(stamped):
                    detail = ("its catalog description was written against a "
                              "DIFFERENT version of this module")
                else:
                    detail = "its catalog description still matches this version"
                return (f"\nNOTE: this file is the implementation of registered "
                        f"component '{e.get('name')}' ({role}.json); {detail}. "
                        f"Call update_component if the change is user-visible.")
    except Exception:  # noqa: BLE001 -- advisory probe must never fail an edit
        return ""
    return ""


def tool_function(command, path, file_text=None, view_range=None,
                  old_str=None, new_str=None, insert_line=None, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        abs_path = _resolve(actx, path)
    except ValueError as e:
        return f"Error: {e}"
    cmd = str(command)
    mutating = cmd in ("create", "str_replace", "insert", "undo_edit")
    # A deep edit is only legitimate on a path granted for MODIFY, and only
    # in a session that has a patch builder (evaluate is read-only: session
    # fact outranks the path grant). The escape route is real: re-requesting
    # with intent="modify" keeps the workspace copy (if_absent), so edits
    # survive and the modify record flips the patch gate.
    if mutating:
        denied = authorize_write(actx, abs_path, operation="edit")
        if denied:
            return denied
    out = _edit.tool_function(
        command=command, path=abs_path, file_text=file_text, view_range=view_range,
        old_str=old_str, new_str=new_str, insert_line=insert_line,
    )
    if mutating and not str(out).startswith("Error"):
        # Record the mutation: `path` is the workspace-relative form the patch
        # builder and covers() consume, so the projection and the patch gate
        # share one position fact. Sessions without a design context (evaluate)
        # skip it rather than write into a ledger nothing reads.
        dctx = get_design_context()
        if dctx is not None:
            rel = os.path.relpath(abs_path, _root(actx)).replace(os.sep, "/")
            dctx.record("edit_source", command=cmd, path=rel)
        # A MUTATING edit of a registered component is the moment
        # the catalog metadata may go stale -- say so here, where the actor
        # still has the context. Reads (view) deliberately produce no pressure.
        out = f"{out}{_registered_component_note(abs_path)}"
    return out


def _covering_intent(actx, abs_path: str) -> Optional[str]:
    """Intent of the grant covering ``abs_path`` ("modify" | "view" | None).

    Mirrors ``AccessBroker.covers``' walk (exact rel first, then ancestor
    directories) but reads the per-grant intent the broker already stores in
    its records (``access.py`` grant()); "modify" wins wherever it appears.
    """
    broker = actx.broker
    rel = os.path.relpath(abs_path, _root(actx)).replace(os.sep, "/")
    best = None
    for rec in broker.grants.get((actx.role, str(actx.node_id)), []):
        for g in rec.get("paths", []):
            g = str(g).replace("\\", "/").rstrip("/")
            if g and (rel == g or rel.startswith(g + "/")):
                if rec.get("intent") == "modify":
                    return "modify"
                best = best or "view"
    return best


op_info = tool_info
op_function = tool_function
