"""On-demand source-code access gating (design v1.2).

Default surface for planner/evaluator is **config + operators**. Source code is
only *transferred* into a role workspace after the agent explicitly calls the
``request_source_access`` tool. Because the files are physically absent from the
workspace until granted, a plain ``bash`` cannot read them (real isolation, not
a prompt convention).

v1.2 changes:
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
import time
from typing import Any, Dict, List, Optional

from gan.framework import frozen, paths
from gan.framework.context import (  # noqa: F401
    AccessContext,
    get_access_context,
    reset_access_context,
    set_access_context,
)


class AccessBroker:
    def __init__(
        self,
        repo_root: str | os.PathLike,
        output_dir: str | os.PathLike,
        workspaces_dir: Optional[str | os.PathLike] = None,
        deny_paths: Optional[List[str]] = None,
        auto_approve: bool = True,
        max_files: int = 500,
        max_bytes: int = 50 * 1024 * 1024,
    ):
        self.repo_root = os.path.abspath(str(repo_root))
        self._repo_real = os.path.realpath(self.repo_root)
        self.output_dir = os.path.abspath(str(output_dir))
        self.workspaces_dir = os.path.abspath(
            str(workspaces_dir) if workspaces_dir is not None
            else paths.workspaces_dir(self.output_dir)
        )
        # explicit extra denies (back-compat / tests); allowlist is authoritative
        self.deny_paths = list(deny_paths or [])
        self.auto_approve = auto_approve
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)
        self.grants: Dict[tuple, List[Dict[str, Any]]] = {}

    # -- workspace ---------------------------------------------------------
    def workspace(self, role: str, node_id: Any) -> str:
        p = os.path.join(self.workspaces_dir, role, str(node_id))
        os.makedirs(p, exist_ok=True)
        return p

    def src_dir(self, role: str, node_id: Any) -> str:
        p = os.path.join(self.workspace(role, node_id), "src")
        os.makedirs(p, exist_ok=True)
        return p

    def clear_workspace(self, role: str) -> None:
        """Drop a role's granted-source workspace (called on instance refresh).

        Granted source is copied into the workspace on demand, so clearing it is
        safe: it is never the only copy.
        """
        p = os.path.join(self.workspaces_dir, role)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
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
                        skipped: Optional[List[str]] = None) -> None:
        """Copy one concrete repo-relative path into the workspace (with guards).

        With ``if_absent``, an existing workspace copy is NEVER overwritten -- it may
        hold this session's ``edit_source`` edits or ``unregister_component``
        deletions. The path is still reported as granted (and listed in ``skipped``)
        so the patch builder keeps tracking it.
        """
        try:
            s = self._safe_join(self.repo_root, rel)
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
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True, ignore=self._output_ignored)
        else:
            shutil.copy2(s, d)
        granted.append(rel)

    # -- grant -------------------------------------------------------------
    def grant(
        self,
        role: str,
        node_id: Any,
        paths_list: List[str],
        intent: str = "view",
        reason: str = "",
        if_absent: bool = False,
    ) -> List[str]:
        """Copy requested repo paths into the role workspace ``src/``.

        Returns the list of actually granted (relative) paths. Denied paths are
        audited but their reason is NOT surfaced to the agent.

        ``if_absent=True`` makes the grant non-destructive: a path whose workspace
        copy already exists is left untouched (it may hold this session's edits or
        deletions) and is reported in ``last_result["skipped"]`` / the audit record.
        Callers that deliberately want a pristine re-copy must NOT pass it.
        """
        if not self.auto_approve:
            raise PermissionError("source access requires approval (auto_approve=False)")
        granted: List[str] = []
        denied_list: List[str] = []
        missing: List[str] = []
        skipped: List[str] = []
        src_root = self.src_dir(role, node_id)
        for raw in paths_list or []:
            rel = str(raw).replace("\\", "/").lstrip("/")
            if not rel or rel == ".":
                denied_list.append(rel or ".")
                continue
            if self.deny_paths and any(fnmatch.fnmatch(rel, pat) for pat in self.deny_paths):
                denied_list.append(rel)
                continue
            if not frozen.is_allowed(role, rel, intent):
                denied_list.append(rel)
                continue
            if frozen.has_glob(rel):
                # expand a glob pattern (e.g. from list_editable) to concrete files
                matches = sorted({
                    os.path.relpath(m, self.repo_root).replace(os.sep, "/")
                    for m in glob.glob(os.path.join(self.repo_root, rel), recursive=True)
                })
                if not matches:
                    missing.append(rel)
                    continue
                for m in matches:
                    if not frozen.is_allowed(role, m, intent):
                        denied_list.append(m)
                        continue
                    self._grant_concrete(m, src_root, granted, denied_list, missing,
                                         if_absent=if_absent, skipped=skipped)
            else:
                self._grant_concrete(rel, src_root, granted, denied_list, missing,
                                     if_absent=if_absent, skipped=skipped)

        rec = {
            "type": "source_access_grant",
            "role": role,
            "node_id": str(node_id),
            "paths": granted,
            "skipped": skipped,
            "denied": denied_list,
            "missing": missing,
            "intent": intent,
            "reason": reason,
            "ts": time.time(),
            "workspace": src_root,
        }
        self.grants.setdefault((role, str(node_id)), []).append(rec)
        self.log_event(rec)
        self.last_result = {"granted": granted, "denied": denied_list,
                            "missing": missing, "skipped": skipped}
        return granted

    def granted_paths(self, role: str, node_id: Any) -> List[str]:
        out: List[str] = []
        for rec in self.grants.get((role, str(node_id)), []):
            out.extend(rec.get("paths", []))
        return out

    # -- audit -------------------------------------------------------------
    def log_event(self, event: Dict[str, Any]) -> None:
        from utils import trajectory_log
        trajectory_log.append(paths.events_path(self.output_dir), event)
