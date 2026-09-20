"""Work tool: list a directory in the role's granted source workspace (read-only)."""
import os

from gan.framework.context import get_access_context
from gan.framework.workspace import resolve


def tool_info():
    return {
        "name": "list_dir",
        "description": "List entries of a directory in YOUR granted source workspace (workspace src only).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    }


def tool_function(path=".", **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        p = resolve(actx, path)
    except ValueError as e:
        return f"Error: {e}"
    if not os.path.isdir(p):
        return f"Error: not a directory: {path}"
    try:
        entries = sorted(os.listdir(p))
    except OSError as e:
        return f"Error: {e}"
    return "\n".join(entries)


op_info = tool_info
op_function = tool_function
