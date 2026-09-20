"""Work tool: regex search inside the role's granted source workspace (read-only)."""
import os
import re

from gan.framework.context import get_access_context
from gan.framework.workspace import resolve


def tool_info():
    return {
        "name": "grep",
        "description": (
            "Regex search inside YOUR granted source workspace (workspace src only). "
            "Returns matching lines as 'path:lineno: line'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    }


def tool_function(pattern, path=".", max_results=100, **kwargs):
    actx = get_access_context()
    if actx is None or getattr(actx, "broker", None) is None:
        return "Error: no access context"
    try:
        root = resolve(actx, path)
    except ValueError as e:
        return f"Error: {e}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    max_results = int(max_results or 100)

    files = []
    if os.path.isfile(root):
        files = [root]
    else:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                files.append(os.path.join(dirpath, name))

    root_dir = root if os.path.isdir(root) else os.path.dirname(root)
    hits = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, root_dir)
                        hits.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(hits) >= max_results:
                            return "\n".join(hits)
        except OSError:
            continue
    return "\n".join(hits) if hits else "(no matches)"


op_info = tool_info
op_function = tool_function
