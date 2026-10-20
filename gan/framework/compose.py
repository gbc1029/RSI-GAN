"""Composed view for split-layout runs.

In split layout the authoritative source of a file lives in exactly one of
three repos (base / task / roles); see ``gan/framework/repo_roots.py``.  Role
processes and imports need ONE coherent tree, so we materialize a derived
overlay ("composed view"): base files first, then task, then roles.  Owner
repos are disjoint, so a later layer winning on collision is irrelevant for
correctness; the view is derived, read-only, and re-materialized after each
patch apply — never edited directly.

Symlinks are preferred (single source of truth, no copy); where the platform
lacks the privilege (Windows without Developer Mode) we fall back to a hard
copy per file.  A ``.compose_manifest.json`` inside the view records, for every
file, which layer supplied it and whether it is a link or a copy.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from typing import Any, Dict, List

_LAYER_ORDER = ("base", "task", "roles")


def _walk_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in files:
            if n == ".compose_manifest.json":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return out


def _safe_join(root: str, rel: str) -> str:
    joined = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, joined]) != root_abs:
        raise ValueError(f"path escapes root: {rel}")
    return joined


def _is_privilege_error(e: OSError) -> bool:
    if getattr(e, "winerror", 0) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
        return True
    return e.errno in (errno.EPERM, errno.EACCES)


def _rmtree_force(path: str) -> None:
    """Remove ``path`` including read-only files (Windows: chmod first)."""
    if not os.path.isdir(path) and not os.path.islink(path):
        return
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o755)
            except OSError:
                pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), 0o666)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def build_composed_view(base: str, task: str, roles: str, dest: str,
                        prefer_symlink: bool = True) -> Dict[str, Any]:
    """Overlay base+task+roles into ``dest``; returns a summary manifest."""
    layers = {"base": base, "task": task, "roles": roles}
    _rmtree_force(dest)
    os.makedirs(dest, exist_ok=True)

    linked = copied = 0
    manifest: Dict[str, Dict[str, Any]] = {}
    mode = "link" if prefer_symlink else "copy"
    for layer in _LAYER_ORDER:
        root = layers[layer]
        for rel in _walk_files(root):
            src = _safe_join(root, rel)
            dst = _safe_join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) or os.path.islink(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            placed = False
            if mode == "link":
                try:
                    os.symlink(os.path.abspath(src), dst)
                    linked += 1
                    placed = True
                except OSError as e:
                    if not _is_privilege_error(e):
                        raise
                    mode = "copy"  # privilege unavailable -> copy from now on
            if not placed:
                shutil.copy2(src, dst)
                copied += 1
            manifest[rel] = {"layer": layer, "kind": "link" if placed else "copy"}

    with open(os.path.join(dest, ".compose_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"linked": linked, "copied": copied, "files": manifest}, f, indent=2)
    return {"files": len(manifest), "linked": linked, "copied": copied}
