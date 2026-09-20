"""Work tool: workspace-confined source editor for planner/evaluator (DEEP).

Deep changes mean editing granted source. To keep the gate meaningful, edits are
CONFINED to the instance's granted workspace ``<workspace>/src`` (the files the
role explicitly obtained via ``request_source_access``). Any path that escapes
that root is rejected. The resulting diff is turned into a patch by
``gan/patch.py`` and applied to the next task generation.

Raw ``bash`` is intentionally NOT granted to roles: it cannot be confined to the
workspace. Only the base ``editor`` capabilities are exposed, path-checked here.
"""
import os

from agent.tools import edit as _edit
from gan.framework.context import get_access_context


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
    root = _root(actx)
    p = str(path or "").replace("\\", "/")
    cand = os.path.realpath(p) if os.path.isabs(p) else os.path.realpath(
        os.path.join(root, p.lstrip("/"))
    )
    if cand != root and not cand.startswith(root + os.sep):
        raise ValueError(f"path escapes the granted workspace: {path}")
    return cand


def tool_function(command, path, file_text=None, view_range=None,
                  old_str=None, new_str=None, insert_line=None, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        abs_path = _resolve(actx, path)
    except ValueError as e:
        return f"Error: {e}"
    return _edit.tool_function(
        command=command, path=abs_path, file_text=file_text, view_range=view_range,
        old_str=old_str, new_str=new_str, insert_line=insert_line,
    )


op_info = tool_info
op_function = tool_function
