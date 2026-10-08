"""Task component: write a solution file inside the polyglot exercise workspace.

Arch 2 (polyglot in GAN): the task child materializes the exercise's starter
files under its sandbox cwd and the agent writes the solution with this tool.
The writable set is the instance's `solution_paths` (repo-root-relative),
passed by the framework via GAN_TASK_WRITE_ROOTS -- the tool refuses anything
else, so the agent cannot touch tests, go.mod-style invalidators, or escape
the workspace.

The generated workspace diff becomes the prediction; the parent applies it
inside the eval container and scores resolved/total.
"""
from __future__ import annotations

import json
import os


def _write_roots() -> list:
    raw = os.environ.get("GAN_TASK_WRITE_ROOTS", "[]")
    try:
        roots = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [r for r in roots if isinstance(r, str) and r.strip()]


def _allowed(path: str) -> bool:
    roots = _write_roots()
    cwd = os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(cwd, path))
    if not target.startswith(cwd + os.sep):
        return False  # escapes the exercise workspace entirely
    if not roots:
        return False  # no declared writable set -> write nothing
    for root in roots:
        base = os.path.realpath(os.path.join(cwd, root))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def tool_info():
    return {
        "name": "write_file",
        "description": (
            "Create or overwrite a solution file inside the exercise workspace. "
            "Only the files declared as solution paths are writable; tests and "
            "config files are read-only. Use the exact relative path given in "
            "the task inputs."
        ),
        "params_schema": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
    }


def tool_function(path: str, content: str, **kwargs):
    if not path or not isinstance(path, str):
        return "Error: path is required"
    if not isinstance(content, str):
        return "Error: content must be a string"
    if not _allowed(path):
        return ("Error: path is not writable. Allowed: "
                f"{os.environ.get('GAN_TASK_WRITE_ROOTS', '[]')}")
    full = os.path.join(os.getcwd(), path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"OK: wrote {path} ({len(content)} chars)"


op_info = tool_info
op_function = tool_function
