"""Work tool: read a file from the role's granted source workspace (read-only).

Confined to ``<workspace>/src`` (see ``gan.framework.workspace``). Roles are not
given raw ``bash``; this plus list_dir/grep/edit_source is the read surface.
"""
import os

from gan.framework.context import get_access_context
from gan.framework.workspace import resolve


def tool_info():
    return {
        "name": "read_file",
        "description": "Read a file from YOUR granted source workspace (workspace src only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["path"],
        },
    }


def tool_function(path, max_chars=8000, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        p = resolve(actx, path)
    except ValueError as e:
        return f"Error: {e}"
    if not os.path.isfile(p):
        return f"Error: not a file: {path}"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"Error: {e}"
    if max_chars and len(text) > int(max_chars):
        text = text[: int(max_chars)] + "\n...[truncated]"
    return text


op_info = tool_info
op_function = tool_function
