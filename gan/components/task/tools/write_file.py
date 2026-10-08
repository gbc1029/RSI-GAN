"""Task component: write a solution file inside the polyglot exercise workspace.

Arch 2 (polyglot in GAN): the task child materializes the exercise's starter
files under its sandbox cwd and the agent writes the solution with this tool.
The writable set is the instance's `solution_paths` (repo-root-relative),
armed per question via GAN_TASK_WRITE_ROOTS and enforced by the FROZEN
framework primitive ``gan.framework.sandbox_write`` -- the AST policy rightly
rejects raw writes in agent-owned component code, and the confinement belongs
in the trust anchor: this component only validates arguments and delegates,
so even an agent-edited copy cannot widen the writable set.

The generated workspace diff becomes the prediction; the parent applies it
inside the eval container and scores resolved/total.
"""
from __future__ import annotations

import os

from gan.framework.sandbox_write import write as _sandbox_write


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
    if not isinstance(content, str):
        return "Error: content must be a string"
    try:
        n = _sandbox_write(path, content)
    except ValueError as e:
        return f"Error: {e}"
    except PermissionError as e:
        return (f"Error: {e}. Allowed: "
                f"{os.environ.get('GAN_TASK_WRITE_ROOTS', '[]')}")
    return f"OK: wrote {path} ({n} chars)"


op_info = tool_info
op_function = tool_function
