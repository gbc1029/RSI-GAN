"""Patch helpers for the gated source-editing path.

When a planner requests source access and edits the copied files under
``<workspace>/src``, the loop turns the workspace diff into a unified patch and
the TaskRunner applies it (to a throwaway repo copy) for that generation.
"""
from __future__ import annotations

import difflib
import os
import subprocess
from pathlib import Path
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
                # n2: compiled caches are not source; diffing workspace-vs-repo
                # copies of them (or proposing their deletion when a copy is
                # absent) is pure patch noise, not a session edit.
                if name.endswith((".pyc", ".pyo")):
                    continue
                yield os.path.join(dirpath, name)


def _no_newline_marker_diff(a_lines: List[str], b_lines: List[str],
                            fromfile: str, tofile: str) -> str:
    """Unified diff that can express "no newline at end of file" (B22, batch 13).

    ``difflib.unified_diff`` cannot: it reads line objects with their terminators
    (``readlines`` keepends), so a file whose LAST line lacks ``\\n`` produces a
    structurally corrupt hunk (the no-terminator line glues onto the next line
    and ``git apply`` rejects the patch with "corrupt patch at line N" --
    verified empirically for both directions: committed-side missing and
    workspace-side missing).

    Fix, generation-side (marker semantics measured against real ``git apply``):
    git treats ``\\ No newline at end of file`` as a FLAG attached to the payload
    line that PRECEDES it -- the flag belongs to the side facing it: after a
    context or ``-`` line it means "a-side here has no newline"; after a ``+``
    line it means "b-side here has no newline"; the SAME line content can carry
    one flag on the ``-``/context row and another on the ``+`` row (git's own
    output for "both sides edited the last line of two no-newline files" is two
    flag pairs). So the wrapper:

    1. normalises the LAST element of each side into ``(content, flag)`` and
       pads it to ``content + "\\n"`` beforedifflib (making the hunk
       well-formed);
    2. builds the diff via ``difflib`` as usual;
    3. post-processes the LAST hunk only: appends the marker line after the
       payload lines belonging to the flagged side(s).

    Edits far from EOF never touch the last lines, so the padded diff equals
    the plain one and no marker is inserted. A misplaced marker is rejected by
    ``git apply`` -- the same loud rejection as before this fix, so the change
    can only improve applyability, never regress it.
    """
    MARK = "\\ No newline at end of file\n"
    a_last = a_lines[-1] if a_lines else None
    b_last = b_lines[-1] if b_lines else None
    a_missing = bool(a_last) and not a_last.endswith("\n")
    b_missing = bool(b_last) and not b_last.endswith("\n")
    if not (a_missing or b_missing):
        return "".join(difflib.unified_diff(a_lines, b_lines,
                                            fromfile=fromfile, tofile=tofile))
    a_pad = list(a_lines)
    b_pad = list(b_lines)
    if a_missing:
        a_pad[-1] = a_last + "\n"
    if b_missing:
        b_pad[-1] = b_last + "\n"
    if a_pad == b_pad:
        # identical after padding => the ONLY change is the trailing newline of
        # the shared last line: emit the canonical split pair, with the marker
        # following the payload line of the side that LACKS the newline (each
        # no-newline payload line must be immediately followed by the marker --
        # verified against git apply in both directions).
        lineno = len(a_pad)
        # a_pad[-1]/b_pad[-1] are newline-terminated after padding; the marker
        # (self-terminated) follows the payload line of the side lacking \n.
        minus = "-" + a_pad[-1] + (MARK if a_missing else "")
        plus = "+" + b_pad[-1] + (MARK if b_missing else "")
        return (f"--- {fromfile}\n+++ {tofile}\n"
                f"@@ -{lineno},1 +{lineno},1 @@\n" + minus + plus)
    out = list(difflib.unified_diff(a_pad, b_pad, fromfile=fromfile, tofile=tofile))
    if not out:
        return ""
    a_content = a_last.rstrip("\n")
    # Placement (verified against `git diff --no-index` reference output):
    # - a_missing XOR b_missing with EQUAL last content: git SPLITS the shared
    #   line into '-' + marker + '+' (the two sides genuinely differ by the
    #   newline byte);
    # - both missing: ONE marker after the payload line(s) -- after the shared
    #   context line when the content is identical, after the '-' and '+'
    #   respectively when the content differs (git emits two flag pairs);
    # - a_missing with DIFFERENT b content: marker after the '-' line only
    #   (the '+' line is b's, which has its own newline).
    hunk_start = max(i for i, l in enumerate(out) if l.startswith("@@"))
    idx_a = idx_b = None
    for i in range(len(out) - 1, hunk_start, -1):
        tag = out[i][:1]
        body = out[i][1:]
        if idx_a is None and tag in ("-", " ") and body.rstrip("\n") == a_content:
            idx_a = i
        if idx_b is None and tag == "+" and body.rstrip("\n") == (b_last.rstrip("\n")
                                                     if b_last else ""):
            idx_b = i
    flags: List[int] = []
    split_ctx = None
    if a_missing and idx_a is not None:
        flags.append(idx_a)
        if (not b_missing) and out[idx_a].startswith(" "):
            split_ctx = idx_a  # a lost the newline, b kept it: split the context
    if b_missing and idx_b is not None:
        flags.append(idx_b)
    if split_ctx is not None:
        ctx_line = out[split_ctx]
        body = ctx_line[1:]
        out[split_ctx] = "-" + body
        out.insert(split_ctx + 1, MARK)
        out.insert(split_ctx + 2, "+" + body)
        # later markers shift by two; the a-flag already sits at split_ctx
        flags = [i + 2 if i > split_ctx else i for i in flags if i != split_ctx]
    for pos in sorted(set(flags), reverse=True):
        out.insert(pos + 1, MARK)
    return "".join(out)


def _deletion_diff(repo_root: str, rel_file: str) -> str:
    a_lines = _read_lines(os.path.join(repo_root, rel_file))
    if not a_lines:
        return ""
    return _no_newline_marker_diff(a_lines, [], f"a/{rel_file}", "/dev/null")


def build_patch_from_workspace(broker, role: str, node_id, rel_paths: Optional[List[str]] = None) -> str:
    """Unified diff of granted workspace copies vs the source repo.

    Handles modifications/additions AND deletions (files present in the repo but
    removed from the workspace, e.g. by ``unregister_component``).
    """
    src_root = broker.src_dir(role, node_id)
    granted = rel_paths if rel_paths is not None else broker.granted_paths(role, node_id, intent="modify")
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
            # A granted path may be a newly-created file/directory.  It is
            # patch-visible when the workspace contains it; do not silently
            # discard it merely because the repo baseline lacks it.
            if os.path.exists(b_root):
                for b_file in _iter_files(b_root):
                    rel_file = os.path.relpath(b_file, src_root).replace(os.sep, "/")
                    b_lines = _read_lines(b_file)
                    chunks.append(_no_newline_marker_diff(
                        [], b_lines, fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}"))
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
            diff = _no_newline_marker_diff(
                a_lines, b_lines,
                fromfile=f"a/{rel_file}", tofile=f"b/{rel_file}",
            )
            chunks.append(diff)
        # deletions inside a granted directory
        if os.path.isdir(a_root):
            for a_file in _iter_files(a_root):
                rel_file = os.path.relpath(a_file, broker.repo_root).replace(os.sep, "/")
                if rel_file not in b_files:
                    chunks.append(_deletion_diff(broker.repo_root, rel_file))
    return "".join(chunks)


# -- deep-write detection -----------------------------------------------------
# Which recorded ops force the session patch builder. Expressed as a SHALLOW
# allowlist on purpose: any op that is not a shallow design operator defaults to
# DEEP, so a new deep tool needs no registration here (fail-safe). The shallow
# set is DERIVED from ``gan/tools/design/`` (frozen plumbing, identical in the
# per-run code tree) so a new design operator is automatically shallow; the
# literal fallback covers a packaging / layout failure.
_DESIGN_DIR = Path(__file__).resolve().parent / "tools" / "design"
_FALLBACK_SHALLOW = frozenset({
    "set_prompt", "set_config", "set_param",
    "select_component", "deselect_component",
})


def _shallow_design_ops() -> frozenset:
    try:
        stems = {p.stem for p in _DESIGN_DIR.glob("*.py") if p.stem != "__init__"}
    except OSError:
        stems = set()
    return frozenset(stems) or _FALLBACK_SHALLOW


_SHALLOW_DESIGN_OPS = _shallow_design_ops()


def has_deep_write(records) -> bool:
    """True if any recorded op is a source-level (deep) write.

    Single source of truth for the session patch gates (planner plan /
    self_improve, evaluator self_improve) and ``task_runner._modify_depth``:

    - a **shallow** design operator never triggers a patch by itself;
    - ``request_source_access`` counts only with ``intent == "modify"`` (a ``view``
      grant copies a read-only file and must not schedule a patch);
    - **everything else** (``register_component`` / ``unregister_component`` /
      ``code_edit`` / future deep tools) counts.

    Unknown ops defaulting to "deep" is deliberate: a deep tool that forgets to be
    listed here still gets its workspace edits patched (the R1 bug class), while a
    forgotten shallow operator only costs one empty diff.
    """
    for r in records or []:
        if not isinstance(r, dict):
            continue
        op = r.get("op")
        if op in _SHALLOW_DESIGN_OPS:
            continue
        if op == "request_source_access" and r.get("intent") != "modify":
            continue
        return True
    return False


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
