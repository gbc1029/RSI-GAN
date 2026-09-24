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
from typing import Any, Dict, List, Optional, Tuple

from gan.framework.loader import config_dir
from gan.registries.loader import load_registry_for_role

_GAN_DIR = config_dir().parent
_TOOLS_DIR = _GAN_DIR / "tools"


def gan_roots(code_root: Optional[str]):
    """Return (tools_dir, registry_dir, components_dir) for a code tree."""
    if code_root:
        gan = os.path.join(code_root, "gan")
        return (os.path.join(gan, "tools"), os.path.join(gan, "registries"),
                os.path.join(gan, "components"))
    return (str(_TOOLS_DIR), str(_GAN_DIR / "registries"), str(_GAN_DIR / "components"))


def py_files_in(src_dir: str) -> List[str]:
    """The ``*.py`` files an assembly copies from ``src_dir``.

    The single definition of "which files does a source dir contribute": a top-level
    ``*.py`` whose name does not start with ``__`` (``__init__.py`` and friends are
    never tool modules). ``assemble_tools_dir`` and the startup collision check both
    go through here, so the two cannot drift.
    """
    out: List[str] = []
    for f in glob.glob(os.path.join(src_dir, "*.py")):
        if os.path.basename(f).startswith("__"):
            continue
        out.append(f)
    return out


def always_on_dirs(role: str, code_root: Optional[str] = None) -> List[str]:
    if role == "task":
        return []  # task agent: no always-on tools
    tools_dir, _rdir, _cdir = gan_roots(code_root)
    dirs = [
        os.path.join(tools_dir, "work", role),
        os.path.join(tools_dir, "work", "common"),  # role-shared work tools
        os.path.join(tools_dir, "design"),
        os.path.join(tools_dir, "deep"),
    ]
    return [d for d in dirs if os.path.isdir(d)]


def selected_module_paths_reported(role: str, config: Optional[Dict[str, Any]],
                                   code_root: Optional[str] = None
                                   ) -> Tuple[List[str], List[Dict[str, str]]]:
    """B7: resolve the design's selected components AND report every skip.

    Returns ``(paths, skipped)``; ``skipped`` is a list of ``{"name", "reason"}``
    for each selected component that would have been silently dropped here
    (not registered / invalid entry / source file missing). The skip behavior of
    :func:`selected_module_paths` is unchanged for assembling callers, but
    reporting assembly sites can now surface the "design claims X, assembly
    delivered Y" gap.
    """
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    cfg = config or {}
    out: List[str] = []
    skipped: List[Dict[str, str]] = []
    if role == "task":
        kind, names = "skill", list(cfg.get("skills") or [])
    elif role == "evaluator":
        kind, names = "eval_point", list(cfg.get("eval_points") or [])
    else:
        kind, names = None, []  # planner has no opt-in work capabilities yet
    for name in names:
        p = reg.module_path(kind, name) if kind else None
        if p:
            out.append(p)
        else:
            skipped.append({"name": str(name),
                            "reason": (reg.reason(kind, name) if kind else None)
                                      or "not registered (or module missing/invalid)"})
    return out, skipped


def selected_module_paths(role: str, config: Optional[Dict[str, Any]],
                          code_root: Optional[str] = None) -> List[str]:
    return selected_module_paths_reported(role, config, code_root)[0]


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


def assemble_tools_dir_reported(
    role: str,
    dest_dir: str,
    config: Optional[Dict[str, Any]] = None,
    include_always_on: bool = True,
    clear: bool = False,
    code_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Materialize a role's toolset AND return the assembly report (B7).

    ``clear=True`` rebuilds the directory from scratch (removing previously
    assembled ``*.py`` files) so that deselected components actually disappear.
    ``code_root`` (per-run code tree) overrides where tools/components/registries
    are read from, so evolved code takes effect.

    Report: ``{"role", "dest", "always_on", "selected_requested",
    "selected_assembled", "skipped": [{"name", "reason"}]}`` — the single point
    of truth for "what the design selected vs what assembly actually delivered".
    """
    os.makedirs(dest_dir, exist_ok=True)
    if clear:
        _clear_tools_dir(dest_dir)
    always_on: List[str] = []
    if include_always_on:
        for src in always_on_dirs(role, code_root=code_root):
            for f in py_files_in(src):
                name = shutil.copy2(f, os.path.join(dest_dir, os.path.basename(f)))
                always_on.append(os.path.basename(name))
    cfg = config or {}
    slot = "skills" if role == "task" else ("eval_points" if role == "evaluator" else None)
    selected_requested = [str(x) for x in (cfg.get(slot) or [])] if slot else []
    paths, skipped = selected_module_paths_reported(role, config, code_root=code_root)
    selected_assembled: List[str] = []
    for p in paths:
        if not os.path.isfile(p):
            # defensive: registry resolved the path but the file vanished between
            # resolution and copy — this MUST be a skip-with-reason, never silence
            core = os.path.basename(p).rsplit(".", 1)[0]
            skipped.append({"name": core, "reason": "module file missing at assembly"})
            continue
        bn = shutil.copy2(p, os.path.join(dest_dir, os.path.basename(p)))
        selected_assembled.append(os.path.basename(bn))
    return {
        "role": role, "dest": dest_dir,
        "always_on": sorted(os.path.basename(f) for f in always_on),
        "selected_requested": selected_requested,
        "selected_assembled": selected_assembled,
        "skipped": skipped,
    }


def assemble_tools_dir(
    role: str,
    dest_dir: str,
    config: Optional[Dict[str, Any]] = None,
    include_always_on: bool = True,
    clear: bool = False,
    code_root: Optional[str] = None,
) -> str:
    """Materialize a role's toolset (bool-output compat wrapper).

    ``clear=True`` rebuilds the directory from scratch (removing previously
    assembled ``*.py`` files) so that deselected components actually disappear.
    ``code_root`` (per-run code tree) overrides where tools/components/registries
    are read from, so evolved code takes effect.
    """
    assemble_tools_dir_reported(role, dest_dir, config=config,
                                include_always_on=include_always_on,
                                clear=clear, code_root=code_root)
    return dest_dir
