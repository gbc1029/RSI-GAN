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

from gan.framework.loader import config_dir
from gan.registries.loader import load_registry_for_role

_GAN_DIR = config_dir().parent
_TOOLS_DIR = _GAN_DIR / "tools"


def _gan_roots(code_root: Optional[str]):
    """Return (tools_dir, registry_dir, components_dir) for a code tree."""
    if code_root:
        gan = os.path.join(code_root, "gan")
        return (os.path.join(gan, "tools"), os.path.join(gan, "registries"),
                os.path.join(gan, "components"))
    return (str(_TOOLS_DIR), str(_GAN_DIR / "registries"), str(_GAN_DIR / "components"))


def always_on_dirs(role: str, code_root: Optional[str] = None) -> List[str]:
    if role == "task":
        return []  # task agent: no always-on tools
    tools_dir, _rdir, _cdir = _gan_roots(code_root)
    dirs = [
        os.path.join(tools_dir, "work", role),
        os.path.join(tools_dir, "work", "common"),  # role-shared work tools
        os.path.join(tools_dir, "design"),
        os.path.join(tools_dir, "deep"),
    ]
    return [d for d in dirs if os.path.isdir(d)]


def selected_module_paths(role: str, config: Optional[Dict[str, Any]],
                          code_root: Optional[str] = None) -> List[str]:
    _tools, rdir, cdir = _gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
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


def _clear_tools_dir(dest_dir: str) -> None:
    """Remove previously assembled tool modules from a toolset dir.

    Only top-level ``*.py`` files and ``__pycache__`` are removed; anything else
    is left alone. The toolset dir is a dedicated, framework-managed directory,
    so a full rebuild is the intended way to make selections take effect (an
    additive copy cannot represent *removals*).
    """
    if not os.path.isdir(dest_dir):
        return
    for name in os.listdir(dest_dir):
        p = os.path.join(dest_dir, name)
        if name == "__pycache__":
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p) and name.endswith(".py"):
            os.remove(p)


def assemble_tools_dir(
    role: str,
    dest_dir: str,
    config: Optional[Dict[str, Any]] = None,
    include_always_on: bool = True,
    clear: bool = False,
    code_root: Optional[str] = None,
) -> str:
    """Materialize a role's toolset.

    ``clear=True`` rebuilds the directory from scratch (removing previously
    assembled ``*.py`` files) so that deselected components actually disappear.
    ``code_root`` (per-run code tree) overrides where tools/components/registries
    are read from, so evolved code takes effect.
    """
    os.makedirs(dest_dir, exist_ok=True)
    if clear:
        _clear_tools_dir(dest_dir)
    if include_always_on:
        for src in always_on_dirs(role, code_root=code_root):
            for f in glob.glob(os.path.join(src, "*.py")):
                if os.path.basename(f).startswith("__"):
                    continue
                shutil.copy2(f, os.path.join(dest_dir, os.path.basename(f)))
    for p in selected_module_paths(role, config, code_root=code_root):
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dest_dir, os.path.basename(p)))
    return dest_dir
