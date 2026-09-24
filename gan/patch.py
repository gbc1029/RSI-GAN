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


def _read_lines(path: str, *, missing_ok: bool = False) -> List[str]:
    """Read a file for diffing.

    B6: "unreadable" must NEVER be silently translated into "empty file" —
    that produced whole-file add/delete patches from plain read failures
    (permission / disk hiccups), i.e. semantically wrong diffs handed to the
    applier. Only a genuinely ABSENT file may read as empty, and only where the
    caller's semantics say absence is meaningful (the repo side of a
    modification diff: absent => the workspace file is an ADDITION).
    Any other OSError propagates -> the session's patch build fails explicitly
    (B6 containment chosen: the generation fails via the existing
    planner_failed / evaluator_failed channel instead of a wrong patch).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
    # all other OSErrors (permission, disk, ...) propagate


def _iter_files(root: str):
    if os.path.isfile(root):
        yield root
    else:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                yield os.path.join(dirpath, name)


def _deletion_diff(repo_root: str, rel_file: str) -> str:
    a_lines = _read_lines(os.path.join(repo_root, rel_file))
    if not a_lines:
        return ""
    return "".join(difflib.unified_diff(a_lines, [], fromfile=f"a/{rel_file}", tofile="/dev/null"))


def build_patch_from_workspace(broker, role: str, node_id, rel_paths: Optional[List[str]] = None) -> str:
    """Unified diff of granted workspace copies vs the source repo.

    Handles modifications/additions AND deletions (files present in the repo but
    removed from the workspace, e.g. by ``unregister_component``).
    """
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
        if not os.path.exists(a_root):
            continue
        if not os.path.exists(b_root):
            # the whole granted file/dir was deleted in the workspace
            for a_file in _iter_files(a_root):
                rel_file = os.path.relpath(a_file, broker.repo_root).replace(os.sep, "/")
                chunks.append(_deletion_diff(broker.repo_root, rel_file))
            continue
        # modifications/additions
        b_files = set()
        for b_file in _iter_files(b_root):
            rel_file = os.path.relpath(b_file, src_root).replace(os.sep, "/")
            b_files.add(rel_file)
            # repo side ABSENT => the workspace file is a genuine ADDITION;
            # repo side PRESENT but unreadable => raise (B6)
            a_lines = _read_lines(os.path.join(broker.repo_root, rel_file), missing_ok=True)
            b_lines = _read_lines(b_file)
            if a_lines == b_lines:
                continue
            diff = difflib.unified_diff(
                a_lines, b_lines,
                fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}",
            )
            chunks.append("".join(diff))
        # deletions inside a granted directory
        if os.path.isdir(a_root):
            for a_file in _iter_files(a_root):
                rel_file = os.path.relpath(a_file, broker.repo_root).replace(os.sep, "/")
                if rel_file not in b_files:
                    chunks.append(_deletion_diff(broker.repo_root, rel_file))
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
