"""Work tool: regex search inside the role's granted source workspace (read-only)."""
import os
import re

from agent.tools.clip import clip_head
from gan.framework.context import get_access_context
from gan.framework.workspace import access_hint, resolve

# Char cap on the assembled output: max_results bounds LINES, this bounds
# CHARS (one minified 500k-char line passes a line-count cap untouched).
# == the chat loop's DEFAULT outer budget, so the inner cut (with its
# tailored "narrow it" advice) always fires before the outer one.
_GREP_OUT_CAP = 16_000


def tool_info():
    return {
        "name": "grep",
        "description": (
            "Regex search inside YOUR granted source workspace (workspace src only). "
            "Returns matching lines as 'path:lineno: line'. Up to max_results "
            "matches (default 100) and 16000 chars; narrow the pattern/path if "
            "the output is clipped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer",
                                "description": "Max matches (lines). Default 100."},
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
    if not os.path.exists(root):
        # A silent "(no matches)" here would be indistinguishable from a real
        # empty result -- classify the miss instead (grantable / outside / typo).
        hint = access_hint(actx, root)
        return hint or f"Error: no such path in your granted workspace: {path}"
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
    truncated_lines = False
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, root_dir)
                        hits.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(hits) >= max_results:
                            truncated_lines = True
                            break
        except OSError:
            continue
        if truncated_lines:
            break
    if not hits:
        return "(no matches)"
    text = "\n".join(hits)
    if truncated_lines:
        text += (f"\n...[stopped at max_results={max_results} matches; "
                 f"narrow the pattern/path for more]")
    return clip_head(text, _GREP_OUT_CAP)


op_info = tool_info
op_function = tool_function
