"""Patch helpers for the gated source-editing path.

When a planner requests source access and edits the copied files under
``<workspace>/src``, the loop turns the workspace diff into a unified patch and
the TaskRunner applies it (to a throwaway repo copy) for that generation.
"""
from __future__ import annotations

import difflib
import os
import subprocess
from typing import List, Optional


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def _iter_files(root: str):
    if os.path.isfile(root):
        yield root
    else:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                yield os.path.join(dirpath, name)


def build_patch_from_workspace(broker, role: str, node_id, rel_paths: Optional[List[str]] = None) -> str:
    """Unified diff of granted workspace copies vs the source repo."""
    src_root = broker.src_dir(role, node_id)
    granted = rel_paths if rel_paths is not None else broker.granted_paths(role, node_id)
    chunks: List[str] = []
    seen = set()
    for rel in granted:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        a_root = os.path.join(broker.repo_root, rel)
        b_root = os.path.join(src_root, rel)
        if not os.path.exists(a_root) or not os.path.exists(b_root):
            continue
        for b_file in _iter_files(b_root):
            rel_file = os.path.relpath(b_file, src_root).replace(os.sep, "/")
            a_file = os.path.join(broker.repo_root, rel_file)
            a_lines = _read_lines(a_file)
            b_lines = _read_lines(b_file)
            if a_lines == b_lines:
                continue
            diff = difflib.unified_diff(
                a_lines, b_lines,
                fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}",
            )
            chunks.append("".join(diff))
    return "".join(chunks)


def apply_patch(repo_dir: str, patch_str: str) -> bool:
    """Apply a unified patch inside ``repo_dir`` using ``git apply`` (works on plain dirs)."""
    if not patch_str or not patch_str.strip():
        return False
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=repo_dir, input=patch_str, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        # fall back to `patch -p1`
        proc2 = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch"],
            cwd=repo_dir, input=patch_str, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return proc2.returncode == 0
    return True
