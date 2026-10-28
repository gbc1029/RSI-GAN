"""Task component: write a solution file inside the polyglot exercise workspace.

Arch 2 (polyglot in GAN): the task child materializes the exercise's starter
files under its sandbox cwd and the agent writes the solution with this tool.
The writable set is the instance's `solution_paths` (repo-root-relative),
armed per question via GAN_TASK_WRITE_ROOTS and enforced by the FROZEN
base-layer primitive ``agent.sandbox_write`` -- the AST policy rightly rejects
raw writes in agent-owned component code, and the confinement belongs in
frozen plumbing outside every evolution channel (``agent/`` is in no role's
writable source set): this component only validates arguments and delegates,
so even an agent-edited copy cannot widen the writable set.

The generated workspace diff becomes the prediction; the parent applies it
inside the eval container and scores resolved/total.
"""
from __future__ import annotations

import os

from agent.sandbox_write import write as _sandbox_write


def tool_info():
    return {
        "name": "write_file",
        "description": (
            "Create or overwrite a solution file inside the exercise workspace. "
            "Only the files declared as solution paths are writable; tests and "
            "config files are read-only. Use the exact relative path given in "
            "the task inputs. For LONG files prefer several calls with "
            "mode='append' (first call creates the file, later calls extend "
            "it) instead of one giant payload -- a single oversized write can "
            "exceed the output budget and get truncated mid-call."
        ),
        "params_schema": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["write", "append"],
                     "description": "write (default) overwrites; append adds "
                                    "to the existing file (creates if absent)"},
        },
    }


def tool_function(path: str, content: str, mode: str = "write", **kwargs):
    if not isinstance(content, str):
        return "Error: content must be a string"
    if mode not in ("write", "append"):
        return f"Error: mode must be 'write' or 'append', got {mode!r}"
    try:
        if mode == "append" and content:
            # Confinement stays in the frozen layer: resolve through the same
            # primitive the write path uses, read the current bytes, and hand
            # the COMBINED text back through the sandbox write -- no raw
            # writes here, the writable set never widens.
            from agent.sandbox_write import resolve_writable
            target = resolve_writable(path)
            existing = ""
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8") as f:
                    existing = f.read()
            n = _sandbox_write(path, existing + content)
        else:
            n = _sandbox_write(path, content)
    except ValueError as e:
        return f"Error: {e}"
    except PermissionError as e:
        return (f"Error: {e}. Allowed: "
                f"{os.environ.get('GAN_TASK_WRITE_ROOTS', '[]')}")
    verb = "appended to" if mode == "append" else "wrote"
    return f"OK: {verb} {path} ({n} chars)"


op_info = tool_info
op_function = tool_function
