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
    try:
        return _git(code_root, "rev-parse", "HEAD").stdout.strip()
    except Exception:
        return ""


def commit(code_root: str, message: str) -> str:
    return _git_commit(code_root, message)


def checkout(code_root: str, sha: Optional[str]) -> None:
    if not sha:
        return
    try:
        _git(code_root, "checkout", "-f", sha)
    except Exception:
        pass


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


def apply_patch(code_root: str, patch: str) -> bool:
    if not (patch or "").strip():
        return False
    r = subprocess.run(["git", "-C", code_root, "apply", "--whitespace=nowarn", "-"],
                       input=patch, text=True, capture_output=True)
    if r.returncode == 0:
        return True
    r2 = subprocess.run(["patch", "-p1", "--no-backup-if-mismatch", "-d", code_root],
                        input=patch, text=True, capture_output=True)
    return r2.returncode == 0


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
    """A patch was rejected by commit validation (allowlist/compile/registry)."""


def _hard_rollback(code_root: str, sha: Optional[str]) -> None:
    """Revert tracked files to ``sha`` and remove patch-introduced untracked files."""
    try:
        if sha:
            _git(code_root, "checkout", "-f", sha)
        _git(code_root, "clean", "-fd")
    except Exception:
        pass


# -- registry validation (field-agnostic; keys are (file, kind, name)) --------
def registry_report(code_root: str) -> Dict[str, Any]:
    from pathlib import Path
    from gan.registries.loader import parse_registry_file, entry_reason

    reg_dir = os.path.join(code_root, "gan", "registries")
    comp_dir = Path(os.path.join(code_root, "gan", "components"))
    invalid = set()
    unparseable = set()
    for fn in ("shared.json", "task.json", "planner.json", "evaluator.json"):
        ents, err = parse_registry_file(Path(reg_dir) / fn)
        if err:
            unparseable.add(fn)
            continue
        for e in ents or []:
            if entry_reason(e, comp_dir) is not None:
                kind = e.get("kind") if isinstance(e, dict) else None
                name = e.get("name") if isinstance(e, dict) else None
                invalid.add((fn, str(kind), str(name)))
    return {"invalid": invalid, "unparseable": unparseable}


def _registry_worsened(before: Dict[str, Any], after: Dict[str, Any], strict: bool = True) -> str:
    new_unp = after["unparseable"] - before["unparseable"]
    if new_unp:
        return f"registry became unparseable: {sorted(new_unp)}"
    if before["unparseable"] and strict:
        if after["unparseable"] or after["invalid"]:
            return ("registry was unparseable; strict mode requires the patch to make it "
                    "fully parseable and valid")
    new_inv = after["invalid"] - before["invalid"]
    if new_inv:
        items = "; ".join(f"{f}:{k}:{n}" for (f, k, n) in sorted(new_inv))
        return f"new invalid component(s): {items}"
    return ""


def _patch_touches_registry(files: Iterable[str]) -> bool:
    return any(str(f).replace("\\", "/").startswith("gan/registries/") for f in files)


def check_patch(code_root: str, patch: str, strict_unparseable: bool = True) -> Tuple[bool, str]:
    """Dry-run: apply + validate + registry-compare, then ALWAYS roll back (no commit).

    Returns (ok, reason). Used for in-session retry before committing.
    """
    if not (patch or "").strip():
        return True, ""
    files = changed_files(patch)
    prev = current_commit(code_root)
    before = registry_report(code_root)
    if not apply_patch(code_root, patch):
        _hard_rollback(code_root, prev)
        return False, "patch failed to apply"
    ok, err = validate_python(code_root, files)
    if not ok:
        _hard_rollback(code_root, prev)
        return False, f"compile failed: {err}"
    reason = ""
    if _patch_touches_registry(files):
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
    if not apply_patch(code_root, patch):
        _hard_rollback(code_root, prev)
        raise PatchRejected("patch failed to apply")
    ok, err = validate_python(code_root, files)
    if not ok:
        _hard_rollback(code_root, prev)
        raise PatchRejected(f"compile failed: {err}")
    if _patch_touches_registry(files):
        reason = _registry_worsened(before, registry_report(code_root), strict=strict_unparseable)
        if reason:
            _hard_rollback(code_root, prev)
            raise PatchRejected(reason)
    return commit(code_root, commit_msg)


def apply_self_patch(code_root: str, role: str, patch: str) -> str:
    """Validate and commit a role self-edit patch (thin wrapper over apply_code_patch)."""
    return apply_code_patch(code_root, role, patch, f"self-improve {role}")

