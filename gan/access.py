"""On-demand source-code access gating (design v1.1).

Default surface for planner/evaluator is **config + operators**. Source code is
only *transferred* into a role workspace after the agent explicitly calls the
``request_source_access`` tool. Because the files are physically absent from the
workspace until granted, a plain ``bash`` cannot read them (real isolation, not
a prompt convention).

Grants are audited to ``<output_dir>/events.jsonl`` and to the node metadata.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from gan.context import (  # noqa: F401
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
    ):
        self.repo_root = os.path.abspath(str(repo_root))
        self.output_dir = os.path.abspath(str(output_dir))
        self.workspaces_dir = os.path.abspath(
            str(workspaces_dir) if workspaces_dir is not None
            else os.path.join(self.output_dir, "workspaces")
        )
        self.deny_paths = list(deny_paths or [])
        self.auto_approve = auto_approve
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

    # -- path safety -------------------------------------------------------
    def _safe_join(self, root: str, rel: str) -> str:
        rel = str(rel).replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"unsafe path: {rel}")
        return os.path.join(root, *parts)

    def _is_denied(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) for pat in self.deny_paths)

    # -- grant -------------------------------------------------------------
    def grant(
        self,
        role: str,
        node_id: Any,
        paths: List[str],
        intent: str = "view",
        reason: str = "",
    ) -> List[str]:
        """Copy requested repo paths into the role workspace ``src/``.

        Returns the list of actually granted (relative) paths.
        """
        if not self.auto_approve:
            raise PermissionError("source access requires approval (auto_approve=False)")
        granted: List[str] = []
        denied_list: List[str] = []
        missing: List[str] = []
        src_root = self.src_dir(role, node_id)
        for rel in paths or []:
            rel = str(rel).replace("\\", "/").lstrip("/")
            if not rel:
                continue
            if self._is_denied(rel):
                denied_list.append(rel)
                continue
            try:
                s = self._safe_join(self.repo_root, rel)
            except ValueError:
                denied_list.append(rel)
                continue
            if not os.path.exists(s):
                missing.append(rel)
                continue
            d = self._safe_join(src_root, rel)
            os.makedirs(os.path.dirname(d) or src_root, exist_ok=True)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
            granted.append(rel)

        rec = {
            "type": "source_access_grant",
            "role": role,
            "node_id": str(node_id),
            "paths": granted,
            "denied": denied_list,
            "missing": missing,
            "intent": intent,
            "reason": reason,
            "ts": time.time(),
            "workspace": src_root,
        }
        self.grants.setdefault((role, str(node_id)), []).append(rec)
        self.log_event(rec)
        # expose last result for the calling tool to report
        self.last_result = {"granted": granted, "denied": denied_list, "missing": missing}
        return granted

    def granted_paths(self, role: str, node_id: Any) -> List[str]:
        out: List[str] = []
        for rec in self.grants.get((role, str(node_id)), []):
            out.extend(rec.get("paths", []))
        return out

    # -- audit -------------------------------------------------------------
    def log_event(self, event: Dict[str, Any]) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        with open(os.path.join(self.output_dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
