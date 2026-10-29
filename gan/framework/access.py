"""On-demand source-code access gating.

Default surface for planner/evaluator is **config + operators**. Source code is
only *transferred* into a role workspace after the agent explicitly calls the
``request_source_access`` tool. Because the files are physically absent from any
role workspace until granted, isolation is real (path absence + workspace
confinement), not a prompt convention.

Current rules:
- **Allowlist by role** (``gan/framework/frozen.py``): only paths inside the
  role's read/write roots may be granted; everything else is denied by default.
- **Anti-recursion / anti-blowup guard**: refuse the repo root, refuse any source
  that contains its own destination, ignore the output tree, and cap file count
  and total bytes per grant.
- Grants are audited to ``logs/events.jsonl`` and to the node metadata.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import shutil
import stat
import threading
import time
from typing import Any, Dict, List, Optional

from gan.framework import frozen, paths
from gan.framework.repo_roots import RepoRoots
from gan.framework.context import (  # noqa: F401
    AccessContext,
    get_access_context,
    reset_access_context,
    set_access_context,
)


class SourceAccessDisabled(RuntimeError):
    """Raised by ``AccessBroker.grant`` when ``source_access.deny_deep`` is on.

    The deep surface is frozen for the whole run (ablation arm): every deep
    source-access request is refused up front. Deep tools catch this and return
    a TERMINAL do-not-retry message instead of letting the agent burn turns.
    """


class AccessBroker:
    def __init__(
        self,
        repo_root: str | os.PathLike,
        output_dir: str | os.PathLike,
        workspaces_dir: Optional[str | os.PathLike] = None,
        deny_paths: Optional[List[str]] = None,
        deny_deep: bool = False,
        deep_view_only: bool = False,
        shallow_prompt_only: bool = False,
        disable_shallow: bool = False,
        prompt_rewrite_min_similarity: float = 0.0,
        prompt_rewrite_min_survival: float = 0.0,
        prompt_rewrite_max_growth: float = 0.0,
        max_files: int = 500,
        max_bytes: int = 50 * 1024 * 1024,
        roots: Optional[Any] = None,
    ):
        self.repo_root = os.path.abspath(str(repo_root))
        self._repo_real = os.path.realpath(self.repo_root)
        self.roots = roots if roots is not None else RepoRoots.single(self.repo_root)
        self.output_dir = os.path.abspath(str(output_dir))
        self.workspaces_dir = os.path.abspath(
            str(workspaces_dir) if workspaces_dir is not None
            else paths.workspaces_dir(self.output_dir)
        )
        # explicit extra denies (back-compat / tests); allowlist is authoritative
        self.deny_paths = list(deny_paths or [])
        self.deny_deep = deny_deep
        # B-group ablation switches (docs/11):
        #   deep_view_only     (B2) -- view grants ok, modify grants refused
        #   shallow_prompt_only(B3) -- deep surface frozen entirely (like
        #                             deny_deep) + shallow limited to prompt
        #   disable_shallow    (B4) -- shallow design operators frozen (deep ok)
        # B4 (disable_shallow) is enforced at the DESIGN tool layer, not here;
        # the broker only gates the deep (source-access) surface.
        self.deep_view_only = bool(deep_view_only)
        self.shallow_prompt_only = bool(shallow_prompt_only)
        self.disable_shallow = bool(disable_shallow)
        # R2 design-write limit (NOT an ablation switch): a prompt write below
        # this CHARACTER-level similarity to the text it replaces is a wholesale
        # rewrite and is refused by set_prompt.prompt_rewrite_error. Enforced at
        # the DESIGN tool layer (like B4), carried here so the role CHILD can
        # read it through broker_info; 0.0 = gate off.
        self.prompt_rewrite_min_similarity = float(prompt_rewrite_min_similarity or 0.0)
        # R2 survival semantics (survival = matched chars / previous length) +
        # the explicit per-write growth cap (0 = off). Mirrored to the child
        # through broker_info exactly like the legacy similarity key.
        self.prompt_rewrite_min_survival = float(prompt_rewrite_min_survival or 0.0)
        self.prompt_rewrite_max_growth = float(prompt_rewrite_max_growth or 0.0)
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)
        self.grants: Dict[tuple, List[Dict[str, Any]]] = {}
        self._trajectory_scopes: Dict[tuple, List[Any]] = {}
        # The result of the LAST grant call, empty before any grant. Readers
        # must not depend on grant() having run first: the attribute is part of
        # the documented audit surface (see the ``grant`` docstring).
        # THREAD-LOCAL on purpose: parallel role self-improvement sessions grant
        # through the SAME broker object, and a shared slot would let one
        # session read the other's skipped/denied audit (observed failure class).
        self._last_result_tls = threading.local()
        # Last patch build's three-way-merge audit (``merged`` / ``conflicts``).
        # Thread-local for the same reason as ``last_result``: the parallel
        # self-improve sessions build patches through one broker object.
        self._last_patch_tls = threading.local()

    @property
    def last_result(self) -> Dict[str, Any]:
        return getattr(self._last_result_tls, "value", None) or {}

    @last_result.setter
    def last_result(self, value: Dict[str, Any]) -> None:
        self._last_result_tls.value = value

    @property
    def last_patch_report(self) -> Dict[str, Any]:
        return getattr(self._last_patch_tls, "value", None) or {}

    @last_patch_report.setter
    def last_patch_report(self, value: Dict[str, Any]) -> None:
        self._last_patch_tls.value = value

    # -- workspace ---------------------------------------------------------
    def workspace(self, role: str, node_id: Any) -> str:
        p = os.path.join(self.workspaces_dir, role, str(node_id))
        os.makedirs(p, exist_ok=True)
        return p

    def src_dir(self, role: str, node_id: Any) -> str:
        p = os.path.join(self.workspace(role, node_id), "src")
        os.makedirs(p, exist_ok=True)
        return p

    def repo_dir(self, rel: str) -> str:
        """Repo dir owning ``rel`` (== repo_root in single layout)."""
        return self.roots.repo_dir(rel) or self.repo_root

    # -- grant-time baseline + backups (deep-write recovery) -----------------
    # ``.gan_base`` holds the bytes a workspace copy was made FROM. The patch
    # builder uses it as the a-side of a THREE-WAY merge, so a baseline that
    # moved after the grant (another session's commit, a role's own earlier
    # patch) no longer turns the session's edit into "patch does not apply".
    _BASE_DIRNAME = ".gan_base"
    _BACKUP_DIRNAME = "_backup"

    def base_dir(self, role: str, node_id: Any) -> str:
        p = os.path.join(self.workspace(role, node_id), self._BASE_DIRNAME)
        os.makedirs(p, exist_ok=True)
        return p

    def backup_root(self, role: str, node_id: Any) -> str:
        """Backup area for one role instance.

        Deliberately OUTSIDE ``workspaces/<role>/<node_id>`` -- that directory is
        purged on every session boundary, so a backup stored inside it would be
        deleted together with the thing it is meant to preserve.
        """
        p = os.path.join(self.workspaces_dir, self._BACKUP_DIRNAME,
                         str(role), str(node_id))
        os.makedirs(p, exist_ok=True)
        return p

    def _next_backup_seq(self, role: str, node_id: Any) -> str:
        root = self.backup_root(role, node_id)
        try:
            used = [int(n) for n in os.listdir(root) if n.isdigit()]
        except OSError:
            used = []
        return f"{max(used, default=0) + 1:03d}"

    @staticmethod
    def _safe_copy(src: str, dst: str) -> None:
        """Copy file CONTENT into ``dst`` without needing ownership of it.

        ``shutil.copy2`` = copyfile + copystat, and copystat's chmod/utime raise
        ``PermissionError: [Errno 1] Operation not permitted`` when ``dst`` already
        exists and belongs to another uid -- which is exactly the state of a
        workspace copy being refreshed (the file was created by the broker, the
        refresh may run as the role's own uid). Writing a temp sibling and
        RENAMING it over the target needs only directory write access, and is
        atomic besides.
        """
        parent = os.path.dirname(dst) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = os.path.join(parent, f".gan-tmp-{os.getpid()}-{os.urandom(4).hex()}")
        try:
            shutil.copyfile(src, tmp)
            try:
                shutil.copystat(src, tmp)
            except OSError:
                # metadata is cosmetic here; never fail a grant over it
                pass
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _snapshot_base(self, source: str, base_root: str, rel: str) -> None:
        """Record the grant-time baseline bytes (idempotent, best effort)."""
        try:
            if os.path.isdir(source):
                dest = os.path.join(base_root, rel)
                for dirpath, _dirs, files in os.walk(source):
                    for name in files:
                        full = os.path.join(dirpath, name)
                        sub = os.path.relpath(full, source).replace(os.sep, "/")
                        target = os.path.join(dest, *sub.split("/"))
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        self._safe_copy(full, target)
            else:
                dest = os.path.join(base_root, *rel.split("/"))
                self._safe_copy(source, dest)
        except OSError as exc:
            self.log_event({"type": "grant_base_snapshot_failed", "path": rel,
                            "error": f"{type(exc).__name__}: {exc}"[:160],
                            "ts": time.time()})

    def _backup_file(self, source: str, backup_dir: str, rel: str) -> None:
        """Keep the current workspace bytes before they are overwritten."""
        try:
            target = os.path.join(backup_dir, *rel.split("/"))
            self._safe_copy(source, target)
        except OSError as exc:
            self.log_event({"type": "workspace_backup_failed", "path": rel,
                            "error": f"{type(exc).__name__}: {exc}"[:160],
                            "ts": time.time()})

    def _backup_session_src(self, role: str, node_id: Any, reason: str) -> Optional[str]:
        """Preserve the files a session actually CHANGED before the workspace is
        purged. Files identical to their ``.gan_base`` snapshot are skipped, so a
        backup contains only real edits (small, and directly usable)."""
        root = os.path.join(self.workspaces_dir, str(role), str(node_id))
        src = os.path.join(root, "src")
        base = os.path.join(root, self._BASE_DIRNAME)
        if not os.path.isdir(src):
            return None
        changed: List[str] = []
        try:
            for dirpath, _dirs, files in os.walk(src):
                for name in files:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, src).replace(os.sep, "/")
                    base_file = os.path.join(base, *rel.split("/"))
                    try:
                        if os.path.isfile(base_file):
                            with open(full, "rb") as a, open(base_file, "rb") as b:
                                if a.read() == b.read():
                                    continue
                    except OSError:
                        pass
                    changed.append(rel)
        except OSError:
            return None
        if not changed:
            return None
        seq = self._next_backup_seq(role, node_id)
        dest = os.path.join(self.backup_root(role, node_id), seq)
        for rel in changed:
            self._backup_file(os.path.join(src, *rel.split("/")), dest, rel)
        self.log_event({"type": "workspace_backup", "reason": reason,
                        "role": str(role), "node_id": str(node_id),
                        "dir": dest, "files": sorted(changed)[:50],
                        "count": len(changed), "ts": time.time()})
        return dest

    def clear_workspace(self, role: str) -> None:
        """Drop a role's granted-source workspace (called on instance refresh).

        Granted source is copied into the workspace on demand, so clearing it is
        safe: it is never the only copy.

        A failed clear drops the role's grants and RE-RAISES so the caller can
        log it loudly: stale copies can no longer enter a patch. Their bytes do
        stay on disk, though -- a later default grant keeps the existing copy
        (``if_absent``), so recovery needs ``refresh=true`` or a manual clean-up.

        Every instance that still holds EDITED files is backed up first (under
        ``workspaces/_backup/<role>/<instance>/``, outside the cleared tree) --
        the outer boundary is otherwise a silent delete of any deep edit whose
        patch did not land.
        """
        p = os.path.join(self.workspaces_dir, role)
        for node_id in sorted(os.listdir(p)) if os.path.isdir(p) else []:
            if node_id.startswith("."):
                continue
            try:
                self._backup_session_src(role, node_id, "workspace_clear")
            except OSError as exc:
                self.log_event({"type": "workspace_backup_failed",
                                "role": str(role), "node_id": str(node_id),
                                "error": f"{type(exc).__name__}: {exc}"[:160],
                                "ts": time.time()})
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
            except OSError:
                # still drop the grants: never let them point at a workspace we
                # could not reset (that is the state that would win if_absent)
                self._drop_role_grants(role)
                raise
        self._drop_role_grants(role)

    @staticmethod
    def _purge_dir_fd(fd: int) -> None:
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
                try:
                    AccessBroker._purge_dir_fd(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)

    def _reset_session(self, role: str, node_id: Any) -> None:
        key = (str(role), str(node_id))
        root = os.path.join(self.workspaces_dir, str(role), str(node_id))
        # Preserve real edits before they are purged: the reset used to be silent,
        # so a rejected patch destroyed a session's work with no trace. The
        # backup is the "undo" the deep-write flow needs.
        if node_id is not None:
            self._backup_session_src(role, node_id, "session_reset")
        if os.path.lexists(root):
            if os.name == "posix":
                fd = os.open(
                    root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
                try:
                    self._purge_dir_fd(fd)
                finally:
                    os.close(fd)
            else:
                for name in os.listdir(root):
                    path = os.path.join(root, name)
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
        self.grants.pop(key, None)
        self._trajectory_scopes.pop(key, None)

    def begin_session(self, role: str, node_id: Any) -> None:
        """Start with no grant, trajectory, or workspace state from an earlier call."""
        self._reset_session(role, node_id)

    def end_session(self, role: str, node_id: Any) -> None:
        """Revoke one exact role/seat call's filesystem and read capabilities."""
        self._reset_session(role, node_id)

    def _drop_role_grants(self, role: str) -> None:
        for k in [k for k in self.grants if k[0] == role]:
            self.grants.pop(k, None)

    # -- path safety -------------------------------------------------------
    def _safe_join(self, root: str, rel: str) -> str:
        rel = str(rel).replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"unsafe path: {rel}")
        return os.path.join(root, *parts)

    def _output_ignored(self, dirpath: str, names: List[str]) -> List[str]:
        out_real = os.path.realpath(self.output_dir)
        ws_real = os.path.realpath(self.workspaces_dir)
        ign = []
        for n in names:
            full = os.path.realpath(os.path.join(dirpath, n))
            if full == out_real or full.startswith(out_real + os.sep):
                ign.append(n)
            elif full == ws_real or full.startswith(ws_real + os.sep):
                ign.append(n)
            elif n == "__pycache__":
                # Never pull compiled caches into a granted workspace copy
                # (byte-identical to the repo side, so no patch diff — pure
                # workspace noise).
                ign.append(n)
        return ign

    def _within_caps(self, source: str) -> bool:
        if os.path.isfile(source):
            try:
                return os.path.getsize(source) <= self.max_bytes
            except OSError:
                return False
        n = 0
        total = 0
        for dirpath, _dirs, files in os.walk(source):
            for name in files:
                n += 1
                if n > self.max_files:
                    return False
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
                if total > self.max_bytes:
                    return False
        return True

    def _grant_concrete(self, rel: str, src_root: str, granted: List[str],
                        denied_list: List[str], missing: List[str],
                        if_absent: bool = False,
                        skipped: Optional[List[str]] = None,
                        base_root: Optional[str] = None,
                        backup_dir: Optional[str] = None,
                        discarded: Optional[List[str]] = None) -> None:
        """Copy one concrete repo-relative path into the workspace (with guards).

        With ``if_absent``, an existing workspace copy is NEVER overwritten -- it may
        hold this session's ``edit_source`` edits or ``unregister_component``
        deletions. The path is still reported as granted (and listed in ``skipped``)
        so the patch builder keeps tracking it.

        ``base_root`` records the grant-time baseline (three-way merge input);
        ``backup_dir`` preserves the bytes about to be overwritten and the rel
        paths are appended to ``discarded``.
        """
        try:
            s = self._safe_join(self.repo_dir(rel), rel)
        except ValueError:
            denied_list.append(rel)
            return
        if not os.path.exists(s):
            missing.append(rel)
            return
        try:
            d = self._safe_join(src_root, rel)
        except ValueError:
            denied_list.append(rel)
            return
        # anti-recursion: never copy a tree into itself / the repo into a subdir
        rs, rd = os.path.realpath(s), os.path.realpath(d)
        if rs == self._repo_real or rd == rs or rd.startswith(rs + os.sep) or rs.startswith(rd + os.sep):
            denied_list.append(rel)
            return
        if not self._within_caps(s):
            denied_list.append(rel)
            return
        if if_absent and os.path.exists(d):
            # keep the workspace copy; report the path as available so the patch
            # builder keeps tracking it (the caller learns it was skipped)
            granted.append(rel)
            if skipped is not None:
                skipped.append(rel)
            return
        os.makedirs(os.path.dirname(d) or src_root, exist_ok=True)
        # Baseline snapshot FIRST (what we are copying FROM, for a later
        # three-way merge), then back up whatever the workspace currently holds
        # (refresh is destructive by design -- the backup makes it recoverable).
        if base_root:
            self._snapshot_base(s, base_root, rel)
        if backup_dir and not os.path.isdir(d) and os.path.exists(d):
            # Only a FILE copy can be silently overwritten here (a directory
            # grant merges into the existing tree, which copytree already
            # reports per file); back the victim up and say so.
            self._backup_file(d, backup_dir, rel)
            if discarded is not None:
                discarded.append(rel)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True, ignore=self._output_ignored,
                            copy_function=self._safe_copy)
        else:
            self._safe_copy(s, d)
        granted.append(rel)

    # -- deep-access policy ------------------------------------------------
    def _deep_access_policy(self, intent: str) -> Optional[str]:
        """Return the ablation policy that forbids this source-access request.

        ``None`` = allowed (falls through to the allowlist). B1 (deny_deep) and
        B3 (shallow_prompt_only) freeze the ENTIRE deep surface; B2
        (deep_view_only) only forbids ``modify`` while keeping ``view``.
        B4 (disable_shallow) does not appear here -- it gates the shallow
        design surface at the design-tool layer, not the source gate.
        """
        if self.deny_deep:
            return "source_access.deny_deep"
        if self.shallow_prompt_only:
            return "ablation.shallow_prompt_only"
        if self.deep_view_only and intent == "modify":
            return "ablation.deep_view_only"
        return None

    # -- grant -------------------------------------------------------------
    def grant(
        self,
        role: str,
        node_id: Any,
        paths_list: List[str],
        intent: str = "view",
        reason: str = "",
        if_absent: bool = False,
        *,
        seat: str,
    ) -> List[str]:

        """Copy requested repo paths into the role workspace ``src/``.

        Returns the list of actually granted (relative) paths. Denied paths are
        audited but their reason is NOT surfaced to the agent.

        ``if_absent=True`` makes the grant non-destructive: a path whose workspace
        copy already exists is left untouched (it may hold this session's edits or
        deletions) and is reported in ``last_result["skipped"]`` / the audit record.
        Callers that deliberately want a pristine re-copy must NOT pass it.
        """
        policy = self._deep_access_policy(intent)
        if policy is not None:
            # Ablation arm: freeze the deep surface (or just its write side) for
            # the run. Audit FIRST (the denial must be visible in events.jsonl),
            # set last_result (deep tools read it), then raise a dedicated
            # exception the deep tools translate into a TERMINAL do-not-retry
            # message. Deliberately NOT appended to self.grants:
            # granted_paths()/covers() walk the "paths" field of grant records,
            # and a denial must never surface as a grant.
            req = [str(p) for p in (paths_list or [])]
            rec = {
                "type": "source_access_denied",
                "role": role,
                "node_id": str(node_id),
                "paths": req,
                "intent": intent,
                "reason": reason,
                "policy": policy,
                "ts": time.time(),
            }
            self.log_event(rec)
            self.last_result = {"granted": [], "denied": req, "missing": []}
            raise SourceAccessDisabled(
                f"deep source access is disabled by {policy}")
        seat = frozen.assert_seat(role, seat)
        granted: List[str] = []
        denied_list: List[str] = []
        missing: List[str] = []
        skipped: List[str] = []
        # Overwritten workspace copies (a refresh) are backed up, never silently
        # destroyed; the baseline of every copy is snapshotted for a 3-way merge.
        discarded: List[str] = []
        src_root = self.src_dir(role, node_id)
        base_root = self.base_dir(role, node_id)
        backup_dir = None
        if not if_absent:
            backup_dir = os.path.join(self.backup_root(role, node_id),
                                      self._next_backup_seq(role, node_id))
        for raw in paths_list or []:
            rel = str(raw).replace("\\", "/").lstrip("/")
            if not rel or rel == ".":
                denied_list.append(rel or ".")
                continue
            if self.deny_paths and any(fnmatch.fnmatch(rel, pat) for pat in self.deny_paths):
                denied_list.append(rel)
                continue
            if not frozen.is_allowed(role, rel, intent, seat=seat):
                denied_list.append(rel)
                continue
            if frozen.has_glob(rel):
                # expand a glob pattern (e.g. from list_editable) to concrete files
                glob_root = self.repo_dir(rel)
                matches = sorted({
                    os.path.relpath(m, glob_root).replace(os.sep, "/")
                    for m in glob.glob(os.path.join(glob_root, rel), recursive=True)
                })
                if not matches:
                    missing.append(rel)
                    continue
                for m in matches:
                    if not frozen.is_allowed(role, m, intent, seat=seat):
                        denied_list.append(m)
                        continue
                    self._grant_concrete(m, src_root, granted, denied_list, missing,
                                         if_absent=if_absent, skipped=skipped,
                                         base_root=base_root, backup_dir=backup_dir,
                                         discarded=discarded)
            else:
                self._grant_concrete(rel, src_root, granted, denied_list, missing,
                                     if_absent=if_absent, skipped=skipped,
                                     base_root=base_root, backup_dir=backup_dir,
                                     discarded=discarded)

        rec = {
            "type": "source_access_grant",
            "role": role,
            "node_id": str(node_id),
            "paths": granted,
            "skipped": skipped,
            "discarded": discarded,
            "backup_dir": backup_dir if discarded else None,
            "denied": denied_list,
            "missing": missing,
            "intent": intent,
            "seat": seat,
            "reason": reason,
            "ts": time.time(),
            "workspace": src_root,
        }
        self.grants.setdefault((role, str(node_id)), []).append(rec)
        self.log_event(rec)
        self.last_result = {"granted": granted, "denied": denied_list,
                            "missing": missing, "skipped": skipped,
                            "discarded": discarded,
                            "backup_dir": backup_dir if discarded else None}
        return granted

    def granted_paths(self, role: str, node_id: Any, intent: Optional[str] = None) -> List[str]:
        """Return granted paths, optionally filtered by grant intent."""
        out: List[str] = []
        for rec in self.grants.get((role, str(node_id)), []):
            if intent is not None and rec.get("intent", "view") != intent:
                continue
            out.extend(rec.get("paths", []))
        return out

    def covers(self, role: str, node_id: Any, rel: str) -> bool:
        """True if this session's patch can carry the repo-relative path ``rel``.

        ``build_patch_from_workspace`` (``gan/patch.py``) walks ``granted_paths``
        only, so a NEW file is committable exactly when it -- or one of its ancestor
        directories -- was granted. The **single definition** of that predicate:
        ``register_component`` refuses to register an uncovered new module with it,
        and ``list_components`` reports ``patch_covered`` per orphan so the agent
        learns the same fact *before* the registration is attempted: "file exists
        in the workspace" is not "file reaches the commit".
        """
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel:
            return False
        for g in self.granted_paths(role, node_id, intent="modify") or []:
            g = str(g).replace("\\", "/").rstrip("/")
            if g and (rel == g or rel.startswith(g + "/")):
                return True
        return False

    # -- audit -------------------------------------------------------------
    def log_event(self, event: Dict[str, Any]) -> None:
        from utils import trajectory_log
        trajectory_log.append(paths.events_path(self.output_dir), event)

    # -- parent-owned reads -------------------------------------------------
    def set_trajectory_scope(self, role: str, node_id: Any,
                             generation_ids: Optional[List[Any]]) -> None:
        """Record the generation ids authorized for the active role call.

        The role process supplies only the request; the parent installs this
        scope from the trusted outer-loop call arguments before sending IPC.
        """
        self._trajectory_scopes[(str(role), str(node_id))] = list(generation_ids or [])

    def read_trajectory(self, role: str, node_id: Any, genid: Any,
                        max_chars: int, *, seat: str) -> str:
        from gan.framework.trajectory import read
        allowed = self._trajectory_scopes.get((str(role), str(node_id)), [])
        if str(genid) not in {str(g) for g in allowed}:
            raise PermissionError("generation is not visible to this role session")
        return read(self.output_dir, genid, max_chars=min(int(max_chars), 6000),
                    role="task")

    def read_session_trajectory(self, role: str, node_id: Any, outer: Any,
                                genid: Any, max_chars: int, *, seat: str) -> str:
        from gan.framework.trajectory import read_session
        if seat != "self_improve":
            raise PermissionError("session trajectories require self_improve seat")
        expected = str(node_id).split("__", 1)[0]
        if expected != f"outer_{outer}":
            raise PermissionError("session outer does not match the role instance")
        return read_session(self.output_dir, outer, genid, role,
                            min(int(max_chars), 6000))

    def session_trajectory_index(self, role: str, node_id: Any, outer: Any,
                                 *, seat: str) -> List[Dict[str, Any]]:
        from gan.framework.trajectory import outer_session_index
        if seat != "self_improve":
            raise PermissionError("session trajectories require self_improve seat")
        if str(node_id).split("__", 1)[0] != f"outer_{outer}":
            raise PermissionError("session outer does not match the role instance")
        return outer_session_index(self.output_dir, outer, role)

    def build_patch(self, role: str, node_id: Any, *, seat: str) -> str:
        from gan.patch import build_patch_from_workspace
        report: Dict[str, Any] = {}
        try:
            patch = build_patch_from_workspace(self, role, node_id, report=report)
        finally:
            # Three-way merges against a moved baseline and genuine conflicts are
            # commit-time FACTS: keep them on the broker (audit) instead of
            # widening the role-result contract, which is validated by exact
            # field set across the process boundary.
            self.last_patch_report = report
        if report.get("merged") or report.get("conflicts"):
            conflicts = report.get("conflicts") or []
            self.log_event({
                "type": "patch_baseline_drift", "role": role, "node_id": str(node_id),
                "merged": report.get("merged") or [],
                # markers are the only human-readable trace of a same-region
                # clash (the patch itself is refused, never written corrupt)
                "conflicts": [{"path": (c or {}).get("path"),
                               "markers": str((c or {}).get("markers") or "")[:400]}
                              for c in conflicts],
                "patch_refused": bool(conflicts),
                "ts": time.time(),
            })
        return patch

    def editable_paths(self, role: str, node_id: Any, cap: int = 300, *,
                       seat: str) -> Dict[str, List[str]]:
        from gan.framework import frozen
        limit = max(1, min(int(cap), 1000))
        return {
            "read_files": frozen.expand_roots(self.repo_root,
                                               frozen.read_roots(role), cap=limit),
            "write_files": frozen.expand_roots(
                self.repo_root, frozen.write_roots(role, seat), cap=limit),
        }
