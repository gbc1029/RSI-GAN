"""Work tool: read a file from the role's granted source workspace (read-only).

Confined to ``<workspace>/src`` (see ``gan.framework.workspace``). Roles are not
given raw ``bash``; this plus list_dir/grep/edit_source is the read surface.

``max_chars`` is param-driven with a hard ceiling (the read_trajectory
mechanism, docs/17 §17): the ceiling equals the chat loop's per-tool outer
budget entry for this tool, so a clamped request is still delivered in full.
"""
import os

from agent.tools.clip import clip_head
from gan.framework.context import get_access_context
from gan.framework.workspace import access_hint, resolve

READ_FILE_MAX_CEILING = 40_000


def tool_info():
    return {
        "name": "read_file",
        "description": "Read a file from YOUR granted source workspace (workspace src only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer",
                              "description": "Max chars to return. Default 8000; "
                                             "requests are clamped to 40000 "
                                             "(ceiling). A cut is marked with "
                                             "shown/total counts."},
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
    if os.path.isdir(p):
        return f"Error: {path} is a directory -- use list_dir."
    if not os.path.isfile(p):
        # No workspace copy: classify WHY (absent / grantable / outside the
        # role surface) with the same primitives the grant loop uses.
        hint = access_hint(actx, p)
        return hint or f"Error: not a file: {path}"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"Error: {e}"
    cap = min(int(max_chars) if max_chars else 8000, READ_FILE_MAX_CEILING)
    return clip_head(text, cap)


op_info = tool_info
op_function = tool_function
