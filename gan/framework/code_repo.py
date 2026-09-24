"""Per-run code repository (FRAMEWORK, frozen).

Materializes the *evolvable* code into a private git working tree under
``<output_dir>/ckpt/code`` so that agent source edits never touch the real repo.
Frozen (trust-anchor) files are copied but made read-only.

All patch application goes through here, and ``apply_self_patch`` re-checks the
patch's target paths against the role's allowlist (``frozen.is_allowed``) — this
is the second line of defence after the workspace confinement.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from gan.framework import frozen

# What is copied into the per-run code tree. Only what the worker/roles/task
# actually need at runtime (no README / requirements / scripts: never edited,
# never imported by the run).
_COPY_TOP = ["gan", "agent", "utils", "domains", "task_agent.py"]
_COPY_FILES: List[str] = []
_EXCLUDE_DIRS = {".git", "venv_nat", "outputs", "analysis", "misc", "baselines",
                 "logs", "__pycache__", "polyglot-benchmark", "SWE-bench"}
_EXCLUDE_PATTERNS = ("dataset*.csv", "*bench*.csv", "*.pyc", "*.pyo", "*.png")


def _ignore(_dirpath: str, names: List[str]) -> List[str]:
    out = []
    for n in names:
        if n in _EXCLUDE_DIRS:
            out.append(n)
            continue
        if any(fnmatch.fnmatch(n, p) for p in _EXCLUDE_PATTERNS):
            out.append(n)
    return out


def _git(code_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", code_root, *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[:500]}")
    return r


def _git_commit(code_root: str, message: str) -> str:
    lock = os.path.join(code_root, ".git", "index.lock")
    if os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass
    last = None
    for _ in range(2):
        try:
            _git(code_root, "add", "-A")
            last = None
            break
        except RuntimeError as e:
            last = e
    if last is not None:
        raise last
    _git(code_root, "-c", "user.name=gan", "-c", "user.email=gan@local",
         "commit", "-m", message, "--allow-empty", "--quiet")
    return current_commit(code_root)


def materialize(repo_root: str, code_root: str) -> str:
    """Copy the code tree into ``code_root``, lock frozen files, git init+commit."""
    if os.path.isdir(code_root):
        shutil.rmtree(code_root, ignore_errors=True)
    os.makedirs(code_root, exist_ok=True)
    for name in _COPY_TOP:
        src = os.path.join(repo_root, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(code_root, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=_ignore)
        else:
            shutil.copy2(src, dst)
    for name in _COPY_FILES:
        src = os.path.join(repo_root, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(code_root, name))

    _git(code_root, "init", "--quiet")
    _lock_frozen(code_root)
    return _git_commit(code_root, "initial")


def _lock_frozen(code_root: str) -> None:
    """Best-effort read-only for trust-anchor files (defence in depth)."""
    for dirpath, _dirs, files in os.walk(os.path.join(code_root, "gan", "framework")):
        for n in files:
            try:
                os.chmod(os.path.join(dirpath, n), 0o444)
            except OSError:
                pass
    for rel in frozen.TRUST_ANCHOR:
        if "*" in rel:
            continue
        p = os.path.join(code_root, rel)
        if os.path.isfile(p):
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass


def current_commit(code_root: str) -> str:
    """The current HEAD SHA of the code tree.

    Raises :class:`RepoIntegrityError` instead of returning an empty string:
    git-infra failure must not be conflated with "no commit" — every caller
    treats that SHA as the code baseline, so an empty string here silently
    disabled rollback/restore/checkpoint lineage (C1).
    """
    try:
        return _git(code_root, "rev-parse", "HEAD").stdout.strip()
    except Exception as e:
        raise RepoIntegrityError(
            f"cannot read HEAD of the code tree {code_root}: {e}"
        ) from e


def commit(code_root: str, message: str) -> str:
    return _git_commit(code_root, message)


def checkout(code_root: str, sha: Optional[str]) -> None:
    """Restore the code tree to ``sha`` (tracked files only).

    Raises :class:`RepoIntegrityError` on failure: callers use this to restore a
    pinned state (resume / self-patch recovery) — silently continuing after a
    failed checkout would run on the wrong base (C1/E3).
    """
    if not sha:
        return
    try:
        _git(code_root, "checkout", "-f", sha)
    except Exception as e:
        raise RepoIntegrityError(
            f"checkout {sha!r} failed in {code_root}: {e}"
        ) from e


# -- branch-per-node blood lineage (v5) ---------------------------------------
def task_ref_name(genid: Any) -> str:
    """Persistent lightweight branch ref for a task generation's code state."""
    return f"task_{genid}"


def outer_base_ref_name(outer: int) -> str:
    """Anchor for the outer's entry HEAD (base for parent=initial children)."""
    return f"outer_{outer}_base"


def ensure_branch(code_root: str, ref_name: str, commit_sha: Optional[str] = None) -> Tuple[bool, str]:
    """Create a lightweight branch ``ref_name`` at ``commit_sha`` (default HEAD).

    Never overwrites: an existing ref pointing at the same commit is idempotent;
    pointing elsewhere is a conflict. Returns (ok, mode) with mode in
    {"created", "idempotent", "conflict"}.
    """
    target = (commit_sha or current_commit(code_root)).strip()
    if not target:
        return False, "conflict"
    existing = _git(code_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{ref_name}",
                    check=False)
    if existing.returncode == 0:
        existing_sha = existing.stdout.strip()
        return (existing_sha == target), ("idempotent" if existing_sha == target else "conflict")
    try:
        _git(code_root, "branch", ref_name, target)
        return True, "created"
    except RuntimeError:
        return False, "conflict"


def checkout_base(code_root: str, target: str) -> str:
    """Put the working tree exactly at ``target`` (a ref name or full SHA).

    The commit is resolved to a SHA first: checking out a branch NAME attaches
    HEAD to it, so any later commit would move the anchor ref (e.g. destroy the
    ``outer_<O>_base`` pin) — which must never happen. Returns the exact SHA.
    """
    r = _git(code_root, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}", check=False)
    sha = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else str(target)
    _git(code_root, "checkout", "-f", sha)
    _git(code_root, "clean", "-fd")
    return sha


def lineage_of(code_root: str, ref_name_or_sha: str) -> List[str]:
    """Ancestor chain (first-committed first) of a ref/SHA — the audit view."""
    r = _git(code_root, "rev-list", "--reverse", ref_name_or_sha)
    return [l for l in r.stdout.splitlines() if l.strip()]


def branch_tips(code_root: str, prefix: str = "task_") -> Dict[str, str]:
    """Map of ``task_*`` ref names to commit SHAs (ref-reconciliation + audit)."""
    r = _git(code_root, "branch", "--list", f"{prefix}*", "--format=%(refname:short) %(objectname)")
    out: Dict[str, str] = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def reconcile_refs(code_root: str, code_commits: Dict[str, str]) -> Dict[str, Any]:
    """Compare ``{genid: code_commit}`` against the ``task_*`` branch refs.

    Pure report (audit): missing/conflicting refs are reported, never fixed or
    blocking — legacy runs and externally cleaned branches must not break runs.
    """
    tips = branch_tips(code_root)
    missing, drifted = [], []
    for genid, sha in (code_commits or {}).items():
        ref = task_ref_name(genid)
        if ref not in tips:
            missing.append(ref)
        elif tips[ref] != sha:
            drifted.append(ref)
    extra = [r for r in tips if str(r)[len("task_"):] not in (code_commits or {})]
    return {"missing": missing, "drifted": drifted, "extra": extra}


def changed_files(patch: str) -> List[str]:
    """Target files of a unified diff, including deletions (``+++ /dev/null``)."""
    lines = (patch or "").splitlines()
    out: List[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        if path == "/dev/null":
            for j in range(i - 1, max(-1, i - 6), -1):
                if lines[j].startswith("--- "):
                    p = lines[j][4:].strip()
                    if p.startswith("a/"):
                        p = p[2:]
                    if p != "/dev/null":
                        out.append(p)
                    break
            continue
        if path.startswith("b/"):
            path = path[2:]
        out.append(path)
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _apply_infra_error(*stds: str) -> bool:
    """True when a patch-applier stderr looks like an INFRASTRUCTURE failure
    (repo bump/index lock/OS) rather than the patch CONTENT being unappliable.
    Anything else keeps the historic agent-attributable classification."""
    lowered = " | ".join(s.lower() for s in stds if s)
    return any(sig in lowered for sig in (
        "fatal:", "not a git repository", "index.lock", "permission denied",
        "no space left", "os error", "disk quota",
    ))


def apply_patch_detail(code_root: str, patch: str) -> Tuple[bool, str]:
    """Apply a unified diff to the code tree. Returns ``(ok, detail)``.

    B4 — failure semantics are layered instead of one blurred ``False``:

    - ``ok`` with empty detail            : applied;
    - ``(False, <applier stderr>)``       : the patch CONTENT was refused (bad
      diff format / context mismatch / empty input). This is agent-attributable:
      the in-session retry should act on the concrete cause, which is why the
      detail is carried instead of the old blur "patch failed to apply";
    - :class:`RepoIntegrityError`         : infrastructure failure (broken git,
      locks, disk) — the tree state is unknowable, the run must abort rather
      than mislead the agent into editing a correct patch.
    """
    if not (patch or "").strip():
        return False, "empty patch"
    r = subprocess.run(["git", "-C", code_root, "apply", "--whitespace=nowarn", "-"],
                       input=patch, text=True, capture_output=True)
    if r.returncode == 0:
        return True, ""
    r2 = subprocess.run(["patch", "-p1", "--no-backup-if-mismatch", "-d", code_root],
                        input=patch, text=True, capture_output=True)
    if r2.returncode == 0:
        return True, ""
    detail = (r.stderr.strip() + "|" + r2.stderr.strip()).strip("|")
    if _apply_infra_error(r.stderr, r2.stderr):
        raise RepoIntegrityError(
            f"patch applier infrastructure failure in {code_root}: {detail[:300]} "
            f"— the code tree state is unknowable; the run cannot safely continue"
        )
    return False, detail[:400]


def apply_patch(code_root: str, patch: str) -> bool:
    """Bool wrapper kept for the task-child run-dir applier (task_execution)."""
    try:
        return apply_patch_detail(code_root, patch)[0]
    except RepoIntegrityError:
        # the throwaway task run-dir has no .git tree to trust/restore: degrade
        # to "patch not applied" exactly as before this change (the child run is
        # then evaluated without the patch, still recorded as applied=False)
        return False


def validate_python(code_root: str, rel_files: Iterable[str]) -> Tuple[bool, str]:
    files = [f for f in rel_files if f.endswith(".py")]
    if not files:
        return True, ""
    paths = [os.path.join(code_root, f) for f in files if os.path.isfile(os.path.join(code_root, f))]
    if not paths:
        return True, ""
    r = subprocess.run([sys.executable, "-m", "py_compile", *paths],
                       capture_output=True, text=True)
    return (r.returncode == 0, (r.stderr or r.stdout)[:2000])


class PatchRejected(Exception):
    """A patch was rejected by commit validation (allowlist/compile/registry).

    This is an *agent-attributable* failure: the patch content was bad and the
    session/patch-retry machinery may continue after it.
    """


class RepoIntegrityError(RuntimeError):
    """The per-run code tree is in an undefined state (git layer broken).

    Unlike :class:`PatchRejected` this is NOT agent-attributable: reading HEAD,
    a checkout or a hard rollback has failed, so the code tree's state can no
    longer be trusted. Every caller MUST let this propagate -- the outer worker
    exits non-zero and ``gan.driver`` aborts the whole run. Downgrading it to a
    per-generation "failed" event would let the loop continue on an unqualified
    (possibly dirty / wrong-base) tree and poison all subsequent lineage.
    """


def _hard_rollback(code_root: str, sha: Optional[str]) -> None:
    """Revert tracked files to ``sha`` and remove patch-introduced untracked files.

    Raises :class:`RepoIntegrityError` on failure: a failed rollback leaves the
    tree holding partially applied patch content, so the run must abort instead
    of continuing on an undefined baseline (C5).
    """
    try:
        if sha:
            _git(code_root, "checkout", "-f", sha)
        _git(code_root, "clean", "-fd")
    except Exception as e:
        raise RepoIntegrityError(
            f"hard rollback to {sha!r} failed in {code_root}: {e} — the code tree "
            f"is in an undefined state; the run cannot continue safely"
        ) from e


# -- registry validation (field-agnostic; keys are (file, kind, name)) --------
def registry_report(code_root: str) -> Dict[str, Any]:
    """Snapshot of registry health for a code tree (used for *differential* gating).

    Reports four independent problem classes so the commit layer can refuse a patch
    that makes any of them worse, while never blocking on pre-existing problems:

    - ``unparseable``: registry files that are not valid JSON / not an object with
      a ``components`` list;
    - ``invalid``    : entries failing ``entry_reason`` (incl. the identity contract
      name==stem, kind-vs-directory, and tool_info/tool_function exposure);
    - ``duplicate``  : ``(role, kind, name)`` declared twice in a role's *merged*
      registry (``shared.json`` silently shadowing ``<role>.json``);
    - ``orphan``     : component files declared by NO registry file.
    """
    from pathlib import Path
    from gan.registries.loader import (
        parse_registry_file, entry_reason, load_registry_for_role,
        orphan_modules, _REGISTRY_FILES,
    )

    reg_dir = os.path.join(code_root, "gan", "registries")
    comp_dir = Path(os.path.join(code_root, "gan", "components"))
    invalid = set()
    unparseable = set()
    duplicate = set()
    for fn in _REGISTRY_FILES:
        ents, err = parse_registry_file(Path(reg_dir) / fn)
        if err:
            unparseable.add(fn)
            continue
        for e in ents or []:
            reason = entry_reason(e, comp_dir)
            if reason is not None:
                kind = e.get("kind") if isinstance(e, dict) else None
                name = e.get("name") if isinstance(e, dict) else None
                # the reason is part of the key: an entry whose invalidity merely
                # CHANGED cause is still a new problem, and a (fn, kind, name)-only
                # key would miss it
                invalid.add((fn, str(kind), str(name), str(reason)))
    # merged-registry duplicates (per role, since shared.json merges into each)
    for role in ("task", "planner", "evaluator"):
        reg = load_registry_for_role(role, registry_dir=Path(reg_dir), components_dir=comp_dir)
        seen = set()
        for e in reg.entries:
            if not isinstance(e, dict):
                continue
            key = (str(e.get("kind")), str(e.get("name")))
            if key in seen:
                duplicate.add((role, key[0], key[1]))
            seen.add(key)
    # orphan: reuse the loader's single definition (component-looking file declared
    # by no registry) so the gate and selection-time validation cannot disagree
    orphan = set(orphan_modules(Path(reg_dir), comp_dir))
    return {"invalid": invalid, "unparseable": unparseable,
            "duplicate": duplicate, "orphan": orphan}


def _registry_worsened(before: Dict[str, Any], after: Dict[str, Any], strict: bool = True) -> str:
    """Return a rejection reason if the patch made registry health worse, else "".

    Differential by design: pre-existing problems never block (only *new* ones do),
    so enabling this gate cannot deadlock an already-dirty tree.
    """
    new_unp = after["unparseable"] - before["unparseable"]
    if new_unp:
        return f"registry became unparseable: {sorted(new_unp)}"
    if before["unparseable"] and strict:
        if after["unparseable"] or after["invalid"]:
            return ("registry was unparseable; strict mode requires the patch to make it "
                    "fully parseable and valid")
    new_inv = after["invalid"] - before["invalid"]
    if new_inv:
        items = "; ".join(f"{f}:{k}:{n} ({r})" for (f, k, n, r) in sorted(new_inv))
        return f"new invalid component(s): {items}"
    new_dup = after["duplicate"] - before["duplicate"]
    if new_dup:
        items = "; ".join(f"{r}:{k}:{n}" for (r, k, n) in sorted(new_dup))
        return f"new duplicate component(s) (merged registry): {items}"
    new_orph = after["orphan"] - before["orphan"]
    if new_orph:
        items = "; ".join(sorted(new_orph))
        return (f"component file(s) not registered in any registry: {items} "
                f"(register them with register_component, or remove the files)")
    return ""


def _patch_touches_registry(files: Iterable[str]) -> bool:
    return any(str(f).replace("\\", "/").startswith("gan/registries/") for f in files)


def _patch_touches_components(files: Iterable[str]) -> bool:
    """A patch that adds/edits component files must keep the registry consistent.

    Without this, adding ``gan/components/**/foo.py`` and forgetting the registry
    entry produced a silently-unselectable component (the gate only ran when the
    patch itself touched ``gan/registries/``).
    """
    return any(str(f).replace("\\", "/").startswith("gan/components/") for f in files)


def _needs_registry_check(files: Iterable[str]) -> bool:
    return _patch_touches_registry(files) or _patch_touches_components(files)


def check_patch(code_root: str, patch: str, strict_unparseable: bool = True) -> Tuple[bool, str]:
    """Dry-run: apply + validate + registry-compare, then ALWAYS roll back (no commit).

    Returns (ok, reason). Used for in-session retry before committing.
    """
    if not (patch or "").strip():
        return True, ""
    files = changed_files(patch)
    prev = current_commit(code_root)
    before = registry_report(code_root)
    ok, apply_err = apply_patch_detail(code_root, patch)
    if not ok:
        _hard_rollback(code_root, prev)
        return False, f"patch failed to apply: {apply_err}"
    ok, err = validate_python(code_root, files)
    if not ok:
        _hard_rollback(code_root, prev)
        return False, f"compile failed: {err}"
    reason = ""
    if _needs_registry_check(files):
        reason = _registry_worsened(before, registry_report(code_root), strict=strict_unparseable)
    _hard_rollback(code_root, prev)
    return (reason == ""), reason


def apply_code_patch(code_root: str, role: str, patch: str, commit_msg: str,
                     strict_unparseable: bool = True) -> str:
    """Allowlist + compile + registry validation, then commit (or raise PatchRejected).

    The single shared entry for BOTH self-edits and task (t) edits.
    """
    files = changed_files(patch)
    if not files:
        raise PatchRejected("empty patch")
    for f in files:
        if not frozen.is_allowed(role, f, "modify"):
            raise PatchRejected(f"patch touches non-editable path for {role}: {f}")
    prev = current_commit(code_root)
    before = registry_report(code_root)
    ok, apply_err = apply_patch_detail(code_root, patch)
    if not ok:
        _hard_rollback(code_root, prev)
        raise PatchRejected(f"patch failed to apply: {apply_err}")
    ok, err = validate_python(code_root, files)
    if not ok:
        _hard_rollback(code_root, prev)
        raise PatchRejected(f"compile failed: {err}")
    if _needs_registry_check(files):
        reason = _registry_worsened(before, registry_report(code_root), strict=strict_unparseable)
        if reason:
            _hard_rollback(code_root, prev)
            raise PatchRejected(reason)
    return commit(code_root, commit_msg)


def apply_self_patch(code_root: str, role: str, patch: str) -> str:
    """Validate and commit a role self-edit patch (thin wrapper over apply_code_patch)."""
    return apply_code_patch(code_root, role, patch, f"self-improve {role}")


def apply_task_patch(code_root: str, role: str, patch: str, genid: Any,
                     parent_genid: Any, base: str) -> Dict[str, Any]:
    """Branch-per-node task patch: align to ``base``, validate+commit, pin the ref.

    The code state of a generation is a git commit whose parent is its selected
    parent's code commit — the DAG **is** the blood lineage. Every generation
    gets a persistent lightweight ref ``task_<genid>`` (its code state anchor,
    GC-protected auditable lookup); a generation with a rejected patch or no
    patch aliases its parent's commit (no empty commits, no lineage gap).

    Returns ``{"code_commit", "base_commit", "applied", "ref_ok", "ref_mode"}``.
    """
    base_commit = checkout_base(code_root, base)
    applied = False
    if (patch or "").strip():
        try:
            sha = apply_code_patch(code_root, role, patch,
                                   f"task gen {genid} parent {parent_genid}")
            applied = True
        except PatchRejected:
            _hard_rollback(code_root, base_commit)
            sha = base_commit  # node's code state == parent's code (no lineage gap)
    else:
        sha = current_commit(code_root)
    ref_ok, ref_mode = ensure_branch(code_root, task_ref_name(genid), sha)
    return {"code_commit": sha, "base_commit": base_commit, "applied": applied,
            "ref_ok": ref_ok, "ref_mode": ref_mode}

