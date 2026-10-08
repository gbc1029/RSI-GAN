"""Confined file-write primitive for the task sandbox (FRAMEWORK, frozen).

Arch 2 (polyglot in the GAN loop): a registered task component must not carry
raw file-write capability (the AST policy rejects it as agent-owned code), and
the writable-set confinement is a trust-anchor concern anyway. The actual
write therefore lives HERE: the calling component only validates arguments and
delegates, so even an agent-edited component cannot bypass the whitelist --
this module is outside every evolution channel.

The writable set is the framework-armed ``GAN_TASK_WRITE_ROOTS`` env var (a
JSON list of workspace-relative paths, set per question inside the sandbox by
``domains.task_worker._polyglot_prepare``). A path is writable iff it resolves
inside the sandbox cwd AND under a declared root. A missing/empty whitelist
means nothing is writable (fail closed).
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


def resolve_writable(path: str) -> str:
    """Return the absolute target for *path*, or raise ValueError/PermissionError."""
    if not path or not isinstance(path, str):
        raise ValueError("path is required")
    roots = _write_roots()
    cwd = os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(cwd, path))
    if not target.startswith(cwd + os.sep):
        raise PermissionError(f"path escapes the sandbox workspace: {path}")
    if not roots:
        raise PermissionError("no writable roots declared")
    for root in roots:
        base = os.path.realpath(os.path.join(cwd, root))
        if target == base or target.startswith(base + os.sep):
            return target
    raise PermissionError(f"path is not a declared solution path: {path}")


def write(relpath: str, content: str) -> int:
    """Create/overwrite *relpath* (UTF-8) inside the sandbox; returns chars."""
    target = resolve_writable(relpath)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return len(content)
