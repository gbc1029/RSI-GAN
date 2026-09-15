"""Tool assembly.

Two activation modes (agreed design):
  * **always-on** (``gan/tools/...``): work tools + design operators + deep gate,
    loaded for every session of a design-editing role;
  * **opt-in** (``gan/components/...``): components selected by the role's design
    config (task ``skills``; evaluator ``eval_points``), resolved via the
    per-role registry.

The task agent has **no always-on tools** — its capabilities are all opt-in
skills. Planner/evaluator get always-on work tools + design ops + deep gate,
plus any opt-in components they selected.
"""
from __future__ import annotations

import glob
import os
import shutil
from typing import Any, Dict, List, Optional

from agent.tools import load_tools
from gan.framework.loader import config_dir
from gan.registries.loader import load_registry_for_role

_GAN_DIR = config_dir().parent
_TOOLS_DIR = _GAN_DIR / "tools"


def always_on_dirs(role: str) -> List[str]:
    if role == "task":
        return []  # task agent: no always-on tools
    dirs = [_TOOLS_DIR / "work" / role, _TOOLS_DIR / "design", _TOOLS_DIR / "deep"]
    return [str(d) for d in dirs if d.is_dir()]


def selected_module_paths(role: str, config: Optional[Dict[str, Any]]) -> List[str]:
    reg = load_registry_for_role(role)
    cfg = config or {}
    out: List[str] = []
    if role == "task":
        for name in cfg.get("skills") or []:
            p = reg.module_path("skill", name)
            if p:
                out.append(p)
    elif role == "evaluator":
        for name in cfg.get("eval_points") or []:
            p = reg.module_path("eval_point", name)
            if p:
                out.append(p)
    # planner has no opt-in work capabilities yet
    return out


def assemble_tools_dir(
    role: str,
    dest_dir: str,
    config: Optional[Dict[str, Any]] = None,
    include_always_on: bool = True,
) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    if include_always_on:
        for src in always_on_dirs(role):
            for f in glob.glob(os.path.join(src, "*.py")):
                if os.path.basename(f).startswith("__"):
                    continue
                shutil.copy2(f, os.path.join(dest_dir, os.path.basename(f)))
    for p in selected_module_paths(role, config):
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dest_dir, os.path.basename(p)))
    return dest_dir


def load_operators(names="all", ops_dirs: Optional[List[str]] = None, logging=print):
    tools = []
    for d in ops_dirs or []:
        tools.extend(load_tools(logging=logging, names=names, tools_dir=d))
    return tools
