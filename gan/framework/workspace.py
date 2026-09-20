"""Workspace confinement helpers (FRAMEWORK, frozen).

Shared by the role work tools (edit_source / read_file / list_dir / grep) so
every source access is confined to the instance's granted workspace ``src``.
"""
from __future__ import annotations

import os


def src_root(actx) -> str:
    return os.path.realpath(actx.broker.src_dir(actx.role, actx.node_id))


def resolve(actx, path: str) -> str:
    """Resolve ``path`` inside the caller's workspace ``src`` root.

    Raises ValueError if the resolved path escapes the root.
    """
    root = src_root(actx)
    p = str(path or "").replace("\\", "/")
    cand = os.path.realpath(p) if os.path.isabs(p) else os.path.realpath(
        os.path.join(root, p.lstrip("/"))
    )
    if cand != root and not cand.startswith(root + os.sep):
        raise ValueError(f"path escapes the granted workspace: {path}")
    return cand
