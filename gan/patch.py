"""Patch helpers for the gated source-editing path.

When a planner requests source access and edits the copied files under
``<workspace>/src``, the loop turns the workspace diff into a unified patch. The
TaskRunner commits it to the per-run code tree by default; only when no code tree
is in play is it applied to the generation's minimal repo copy instead.
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def _read_lines(path: str, *, missing_ok: bool = False) -> List[str]:
    """Read a file for diffing.

    "Unreadable" must NEVER be silently translated into "empty file" —
    that produced whole-file add/delete patches from plain read failures
    (permission / disk hiccups), i.e. semantically wrong diffs handed to the
    applier. Only a genuinely ABSENT file may read as empty, and only where the
    caller's semantics say absence is meaningful (the repo side of a
    modification diff: absent => the workspace file is an ADDITION).
    Any other OSError propagates -> the session's patch build fails explicitly
    (Containment chosen: the generation fails via the existing
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
                # Compiled caches are not source; diffing workspace-vs-repo
                # copies of them (or proposing their deletion when a copy is
                # absent) is pure patch noise, not a session edit.
                if name.endswith((".pyc", ".pyo")):
                    continue
                yield os.path.join(dirpath, name)


def _no_newline_marker_diff(a_lines: List[str], b_lines: List[str],
                            fromfile: str, tofile: str) -> str:
    """Unified diff that can express "no newline at end of file".

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
       pads it to ``content + "\\n"`` before difflib (making the hunk
       well-formed);
    2. builds the diff via ``difflib`` as usual;
    3. post-processes the LAST hunk only: appends the marker line after the
       payload lines belonging to the flagged side(s).

    Edits far from EOF never touch the last lines, so the padded diff equals
    the plain one and no marker is inserted. A misplaced marker is rejected by
    ``git apply`` -- the same loud rejection as an unmarked diff, so the change
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
    # a_lines empty => a_last is None (new file with a no-newline last line on
    # the b side reaches this branch: b_missing pads b, a side stays empty).
    a_content = a_last.rstrip("\n") if a_last is not None else ""
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


def _three_way_merge(ours: str, base: str, theirs: str):
    """Merge ``ours`` (the session edit) with ``theirs`` (the moved baseline).

    ``git merge-file -p ours base theirs`` prints the merged text and exits with
    the CONFLICT COUNT (0 = clean). Returns ``(lines, clean)``; on a conflict the
    lines carry git's ``<<<<<<<``/``=======``/``>>>>>>>`` markers, which is the
    only place a conflict can be shown to a human without corrupting the patch.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--quiet", ours, base, theirs],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    return proc.stdout.splitlines(keepends=True), proc.returncode == 0


_MISSING = object()


def _json_semantic_merge(ours: str, base: str, theirs: str) -> Optional[str]:
    """Key-wise three-way merge for a top-level JSON OBJECT (registry files).

    Registry JSONs are the dominant deep-write target, and ``git merge-file``
    conflicts on ADJACENT lines -- exactly what "the session edited one field
    while another generation added a field" looks like. Merging at the KEY level
    is the honest reading of the intent: the session's field edit lands, the
    upstream addition survives.

    Returns the merged TEXT (formatted like ``registries.loader.write_registry_json``)
    or ``None`` when both sides changed the SAME key to different values -- a real
    conflict that must stay a loud refusal.
    """
    try:
        o, b, t = json.loads(ours), json.loads(base), json.loads(theirs)
    except (TypeError, ValueError):
        return None
    if not (isinstance(o, dict) and isinstance(b, dict) and isinstance(t, dict)):
        return None
    keys: List[str] = list(o.keys())
    for k in list(b.keys()) + list(t.keys()):
        if k not in keys:
            keys.append(k)
    merged: Dict[str, object] = {}
    for k in keys:
        ov, bv, tv = o.get(k, _MISSING), b.get(k, _MISSING), t.get(k, _MISSING)
        ours_changed = ov != bv
        theirs_changed = tv != bv
        if ours_changed and theirs_changed and ov != tv:
            return None
        value = tv if (theirs_changed and not ours_changed) else ov
        if value is not _MISSING:
            merged[k] = value
    text = json.dumps(merged, ensure_ascii=False, indent=2)
    if ours.endswith("\n"):
        text += "\n"
    return text


def _merge_drifted(base_root: Optional[str], ws_file: str, repo_root: str,
                   rel_file: str, a_lines: List[str], b_lines: List[str],
                   report: Optional[Dict[str, object]]):
    """Resolve a workspace copy whose grant-time baseline has since MOVED.

    Returns ``(b_lines, conflict_markers_or_None)``. Three cases, in order:

    1. ``base == current repo`` -- the baseline never moved: the plain two-way
       diff is already correct, return ``b_lines`` untouched (the overwhelmingly
       common case; zero behaviour change).
    2. ``base == workspace`` -- the session never edited the file: return the
       CURRENT repo lines, i.e. emit NO diff. Without this, a stale unedited copy
       produced a patch that silently REVERTED someone else's committed change.
    3. both differ -- three-way merge (line level, then a JSON key-level retry).
       A clean merge becomes the new b-side (the session's intent lands on top of
       the moved baseline); a conflict returns the markers so the caller can
       REFUSE the patch instead of emitting a diff that reverts the other side.
    """
    if not base_root:
        return b_lines, None
    base_file = os.path.join(base_root, *rel_file.split("/"))
    if not os.path.isfile(base_file):
        return b_lines, None
    base_lines = _read_lines(base_file)
    if base_lines == a_lines:
        return b_lines, None
    if base_lines == b_lines:
        return a_lines, None
    repo_file = os.path.join(repo_root, rel_file)
    merged, clean = _three_way_merge(ws_file, base_file, repo_file)
    if not clean and rel_file.endswith(".json"):
        text = _json_semantic_merge("".join(b_lines), "".join(base_lines),
                                    "".join(a_lines))
        if text is not None:
            merged, clean = text.splitlines(keepends=True), True
    if not clean:
        return b_lines, "".join((merged or [])[:80])
    if report is not None:
        report.setdefault("merged", []).append(rel_file)  # type: ignore[union-attr]
    return merged or b_lines, None


def build_patch_from_workspace(broker, role: str, node_id,
                               rel_paths: Optional[List[str]] = None,
                               report: Optional[Dict[str, object]] = None) -> str:
    """Unified diff of granted workspace copies vs the source repo.

    Handles modifications/additions AND deletions (files present in the repo but
    removed from the workspace, e.g. by ``unregister_component``).

    ``report`` (optional, filled in place) records which files needed a
    three-way merge (``merged``) and which genuinely conflicted (``conflicts``,
    ``{"path", "markers"}`` entries).

    A conflict REFUSES the whole patch (``""``): a valid-looking diff built from
    the workspace alone silently reverts the other side's committed change, and
    landing half a session's intent is the "lossy but green" failure this path
    exists to prevent. The session's bytes survive in the workspace backup, and
    ``refresh=true`` re-syncs onto the moved baseline.
    """
    src_root = broker.src_dir(role, node_id)
    try:
        base_root: Optional[str] = broker.base_dir(role, node_id)
    except Exception:  # a broker without baseline snapshots (tests, old trees)
        base_root = None
    granted = rel_paths if rel_paths is not None else broker.granted_paths(role, node_id, intent="modify")
    chunks: List[str] = []
    conflicts: List[Dict[str, str]] = []
    seen = set()
    for rel in granted:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        repo_root = broker.repo_dir(rel)
        a_root = os.path.join(repo_root, rel)
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
                rel_file = os.path.relpath(a_file, repo_root).replace(os.sep, "/")
                chunks.append(_deletion_diff(repo_root, rel_file))
            continue
        # modifications/additions
        b_files = set()
        for b_file in _iter_files(b_root):
            rel_file = os.path.relpath(b_file, src_root).replace(os.sep, "/")
            b_files.add(rel_file)
            # repo side ABSENT => the workspace file is a genuine ADDITION;
            # repo side PRESENT but unreadable => raise
            a_lines = _read_lines(os.path.join(repo_root, rel_file), missing_ok=True)
            b_lines = _read_lines(b_file)
            if a_lines:
                b_lines, conflict = _merge_drifted(base_root, b_file, repo_root, rel_file,
                                                   a_lines, b_lines, report)
                if conflict is not None and len(conflicts) < 5:
                    # Keep the markers (bounded) as evidence: they are the only
                    # way a same-region clash can be shown without writing a
                    # corrupt patch into the tree.
                    conflicts.append({"path": rel_file, "markers": conflict[:1200]})
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
                rel_file = os.path.relpath(a_file, repo_root).replace(os.sep, "/")
                if rel_file not in b_files:
                    chunks.append(_deletion_diff(repo_root, rel_file))
    if report is not None:
        report["conflicts"] = conflicts
    if conflicts:
        # Refuse the whole patch: a workspace-only diff for a conflicted file is
        # VALID but reverts the other side's committed change, and landing the
        # rest silently drops part of the session's intent.
        return ""
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

# Bookkeeping ops recorded through the same design-context channel that are
# neither design operators nor source writes: the planner's structured
# feedback-response stance (gan/tools/work/planner/respond_issue.py). Left
# unlisted, has_deep_write's unknown->DEEP default labels every post-feedback
# plan session a deep write and schedules a patch that can only come out
# empty (observed: run small_1028_0046 labelled 6/9 generations deep-intent
# with zero edit_source ops and view-only grants; the mislabel also fed the
# session patch gate, which then found nothing to commit).
_ALWAYS_SHALLOW = frozenset({"respond_issue"})

# Design-directory operators that are deep BY NATURE: they write a CODE-TREE
# file (``gan/design/schema_ext/<role>.json``), so their changes reach the
# committed tree only through the patch channel. Left in the auto-derived
# shallow set, a declaration-only session would build no patch and the schema_ext
# edit would be dropped at the outer boundary. (The normal flow still pairs the
# declaration with a deep op — the consumer component — but the lone-declaration
# session must patch too.)
_DEEP_BY_NATURE = frozenset({"add_config_key", "add_config_value"})


def _shallow_design_ops() -> frozenset:
    try:
        stems = {p.stem for p in _DESIGN_DIR.glob("*.py") if p.stem != "__init__"}
    except OSError:
        stems = set()
    shallow = (frozenset(stems) - _DEEP_BY_NATURE) or _FALLBACK_SHALLOW
    return shallow | _ALWAYS_SHALLOW


_SHALLOW_DESIGN_OPS = _shallow_design_ops()


def has_deep_write(records) -> bool:
    """True if any recorded op is a source-level (deep) write.

    Single source of truth for the session patch gates (planner plan /
    self_improve, evaluator self_improve) and ``task_runner._modify_depth``:

    - a **shallow** design operator never triggers a patch by itself;
    - ``request_source_access`` counts only with ``intent == "modify"`` (a ``view``
      grant copies a read-only file and must not schedule a patch);
    - **bookkeeping** ops recorded on the same channel but never writing source
      (e.g. ``respond_issue``) are shallow by name via ``_ALWAYS_SHALLOW``;
    - **everything else** (``register_component`` / ``unregister_component`` /
      ``update_component`` / ``edit_source`` / future deep tools) counts.

    Unknown ops defaulting to "deep" is deliberate: a deep tool that forgets to be
    listed here still gets its workspace edits patched, while a
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


# -- same-session multi-writer detection (layer 4) ----------------------------
# Tools whose recorded op names the file it wrote. ``update_component`` records
# the registry FILE NAME (``registry``); the rest record a repo-relative ``path``.
_WRITE_OPS = frozenset({
    "edit_source", "update_component", "register_component", "unregister_component",
})


def _op_target(r: Dict) -> Optional[str]:
    op = r.get("op")
    if op not in _WRITE_OPS:
        return None
    if op == "update_component":
        reg = r.get("registry")
        if not reg:
            return None
        reg = str(reg).replace("\\", "/")
        if "/" in reg:
            return reg.lstrip("/")
        return f"gan/registries/{reg}"
    path = r.get("path")
    if not path:
        return None
    return str(path).replace("\\", "/").lstrip("/")


def multi_writer_paths(records) -> Dict[str, List[str]]:
    """Files written by MORE THAN ONE tool inside one session.

    Why this exists: an agent that edits a registry JSON twice -- once through
    ``update_component`` (structured field bump) and once through
    ``edit_source`` (free-text ``str_replace``) -- produces ONE unified diff per
    file, so whichever write is applied last silently wins and the other
    disappears. The patch is not invalid, it is *lossy*, and the loss is
    invisible in the receipt.

    Derived purely from the session's existing op records (no new IPC surface,
    no new tool parameters): the caller stamps ``multi_writer`` facts onto the
    receipt and logs a ``same_field_overwrite`` event, so the conflict becomes
    auditable and the agent can be told.
    """
    touched: Dict[str, List[str]] = {}
    for r in records or []:
        if not isinstance(r, dict):
            continue
        target = _op_target(r)
        if not target:
            continue
        touched.setdefault(target, []).append(str(r.get("op")))
    return {p: sorted(set(ops)) for p, ops in touched.items() if len(set(ops)) > 1}


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
