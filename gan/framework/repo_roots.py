"""Repo-root routing for the split layout (docs/12): map a path to its owning repo and resolve per-role roots (single layout == one tree)."""
from __future__ import annotations

import fnmatch
import os
from typing import List, Optional, Tuple

from gan.framework import frozen


def _norm(rel: str) -> str:
    return str(rel).replace("\\", "/").lstrip("/")


def _match(rel: str, patterns: List[str]) -> bool:
    rel = _norm(rel)
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        # allow a glob root like "gan/components/**" to match the dir itself
        if pat.endswith("/**") and fnmatch.fnmatch(rel, pat[:-3]):
            return True
    return False


def owner_repo(rel: str) -> str:
    """Owner repo for a relative path: task | planner | evaluator | base."""
    rel = _norm(rel)
    if _match(rel, frozen.PLANNER_SELF):
        return "planner"
    if _match(rel, frozen.EVALUATOR_SELF):
        return "evaluator"
    if _match(rel, frozen.TASK_SOURCE):
        return "task"
    return "base"


class RepoRoots:
    """The repository roots a run reads/writes (see module docstring)."""

    def __init__(
        self,
        base: Optional[str] = None,
        task: Optional[str] = None,
        roles: Optional[str] = None,
        *,
        planner: Optional[str] = None,
        evaluator: Optional[str] = None,
        layout: str = "single",
    ):
        self.base = base
        self.task = task
        self.roles = roles
        self.planner = planner
        self.evaluator = evaluator
        self.layout = layout

    @classmethod
    def single(cls, code_root: Optional[str]) -> "RepoRoots":
        """The legacy single-tree layout: every owner resolves to one root."""
        root = os.path.abspath(str(code_root)) if code_root else None
        return cls(base=root, task=root, roles=root, layout="single")

    # -- ownership ----------------------------------------------------------
    def _owner_dir(self, owner: str) -> Optional[str]:
        if owner == "base":
            return self.base
        if owner == "task":
            return self.task
        if owner == "planner":
            return self.planner or self.roles
        if owner == "evaluator":
            return self.evaluator or self.roles
        return None

    def repo_dir(self, rel: str) -> Optional[str]:
        return self._owner_dir(owner_repo(rel))

    def repo_dir_for_role(self, role: str) -> Optional[str]:
        if role == "task":
            return self.task
        if role == "planner":
            return self.planner or self.roles
        if role == "evaluator":
            return self.evaluator or self.roles
        return self.base

    # -- path resolution ------------------------------------------------------
    def _safe_join(self, root: Optional[str], rel: str) -> Optional[str]:
        if not root:
            return None
        rel = _norm(rel)
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return None
        return os.path.join(root, *parts)

    def resolve(self, rel: str, *, for_write: bool = False) -> Optional[str]:
        """Absolute path for ``rel``; reads fall back to base, writes resolve to the owner only."""
        owned = self.repo_dir(rel)
        p = self._safe_join(owned, rel)
        if for_write or self.base is None or self.base == owned:
            return p
        if p and os.path.exists(p):
            return p
        b = self._safe_join(self.base, rel)
        if b and os.path.exists(b):
            return b
        return p

    # -- per-role assembly roots ---------------------------------------------
    def tools_dirs(self, role: str) -> List[str]:
        """Ordered always-on tool source dirs: base (common/design/deep) then the role overlay."""
        dirs: List[str] = []
        if self.base:
            dirs.append(os.path.join(self.base, "gan", "tools"))
        role_root = self.repo_dir_for_role(role)
        if role_root and role_root != self.base:
            dirs.append(os.path.join(role_root, "gan", "tools"))
        return dirs

    def registry_dir(self, role: str) -> Optional[str]:
        root = self.repo_dir_for_role(role)
        return os.path.join(root, "gan", "registries") if root else None

    def components_dir(self, role: str) -> Optional[str]:
        root = self.repo_dir_for_role(role)
        return os.path.join(root, "gan", "components") if root else None

    def assembly_roots(self, role: str) -> Tuple[List[str], Optional[str], Optional[str]]:
        """Per-role ``(tools_dirs, registry_dir, components_dir)``."""
        return (self.tools_dirs(role), self.registry_dir(role), self.components_dir(role))
