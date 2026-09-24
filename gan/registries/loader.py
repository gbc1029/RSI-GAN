"""Per-role component registries (code + registry.json).

A **valid** component entry is a dict with required fields per kind:
``name`` (non-empty str), ``kind`` in ``KINDS``, ``module`` (path relative to
``gan/components`` that exists). ``description`` / ``params_schema`` are optional.

Beyond presence, an entry must satisfy the **identity contract** (the three names
must agree, otherwise a component assembles but silently never loads):

- ``name`` == the module's file stem (``agent/tools/__init__.py`` keys tools by
  ``tool_file.stem`` and filters by that name);
- ``kind`` agrees with the module's parent directory (``skills/`` / ``eval_points/``);
- the module actually exposes ``tool_info`` + ``tool_function`` (defined **or**
  re-exported), which ``load_tools`` requires.

Robustness (field-agnostic): malformed entries are **tolerated** at load time --
they are kept but marked invalid (with a reason) and are never usable
(selection/assembly/loading skip them). Nothing here raises on bad input; the
patch/commit layer is responsible for not *persisting* new invalidity.

``validate_registry`` additionally reports registry-level problems that a single
entry cannot see: duplicate ``(kind, name)`` in the merged registry and **orphan**
component files (present in ``gan/components`` but not registered).

Registering/modifying a component is a source-level ("deep") change.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gan.framework.loader import config_dir

_GAN_DIR = config_dir().parent
REGISTRY_DIR = _GAN_DIR / "registries"
COMPONENTS_DIR = _GAN_DIR / "components"
KINDS = ("skill", "eval_point")
_REQUIRED = ("name", "kind", "module")
# kind -> the component directory it must live under (identity contract, V2)
_KIND_DIR = {"skill": "skills", "eval_point": "eval_points"}
# every registry file that can declare a component (orphan detection is
# registry-set-wide, not per-role: a file is only an orphan if NO registry
# declares it)
_REGISTRY_FILES = ("shared.json", "task.json", "planner.json", "evaluator.json")


def parse_registry_file(path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Return (entries, None) or (None, error) if the file is unparseable.

    "Unparseable" = invalid JSON, or valid JSON that is not an object with a
    ``components`` list. Missing/empty file -> ([], None).
    """
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"invalid JSON: {e}"
    if data is None:
        return [], None
    if not isinstance(data, dict) or not isinstance(data.get("components", []), list):
        return None, "not an object with a 'components' list"
    return data["components"], None


def _exposes_tool_api(path: Path) -> bool:
    """True if the module binds both ``tool_info`` and ``tool_function``.

    Static (AST) check on purpose: importing a component executes its top-level
    code and needs a live runtime (contextvars/agent deps), which is not available
    at validation time. The recognised forms are exactly those whose **bound name**
    is ``tool_info`` / ``tool_function``:

    - ``def tool_info(): ...`` (and ``async def``);
    - ``from x import tool_info, tool_function`` (the shared-skill re-export form);
    - ``import x as tool_info`` / ``tool_info = <callable>`` (a top-level binding).

    Aliasing is judged by the *bound* name, not the imported one: Python binds
    ``asname`` when present, so ``from x import tool_info as ti`` does **not** expose
    ``tool_info``. That matches the runtime verdict — ``load_tools`` keys off
    ``hasattr(module, "tool_info")`` and logs ``missing tool_info/tool_function`` for
    exactly that form (verified against ``agent/tools/__init__.py``). Accepting the
    alias here would let a component pass validation and then silently never load.

    Deliberately syntactic: it answers "are the two names bound", not "are they
    callable" -- the latter stays ``load_tools``' runtime job.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    exposed = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exposed.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                # the BOUND name: an alias shadows the imported name
                exposed.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exposed.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exposed.add(node.target.id)
    return {"tool_info", "tool_function"} <= exposed


def entry_reason(entry: Any, components_dir: Path) -> Optional[str]:
    """Return None if valid, else a short reason string."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    for f in _REQUIRED:
        if f not in entry or entry[f] in (None, ""):
            return f"missing required field '{f}'"
    if entry["kind"] not in KINDS:
        return f"unknown kind '{entry['kind']}'"
    mod = str(entry["module"]).replace("\\", "/")
    p = (components_dir / mod)
    if not p.is_file():
        return f"module file not found: {mod}"
    # -- identity contract (V1-V3): the three names must agree -----------------
    stem = Path(mod).stem
    if str(entry["name"]) != stem:
        return (f"name '{entry['name']}' != module file stem '{stem}' "
                f"(tools are keyed by file stem; the component would never load)")
    want_dir = _KIND_DIR.get(str(entry["kind"]))
    parts = Path(mod).parts
    if want_dir and (len(parts) < 2 or parts[-2] != want_dir):
        return (f"kind '{entry['kind']}' requires the module directly under a "
                f"'{want_dir}/' directory, got: {mod}")
    if not _exposes_tool_api(p):
        return f"module does not expose tool_info/tool_function: {mod}"
    return None


class ComponentRegistry:
    def __init__(self, role: str, entries: List[Any], components_dir: Path):
        self.role = role
        self.entries = [e for e in (entries or [])]
        self.components_dir = Path(components_dir)

    # -- queries -----------------------------------------------------------
    def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self.entries if isinstance(e, dict) and (kind is None or e.get("kind") == kind)]

    def names(self, kind: Optional[str] = None) -> List[str]:
        return [e["name"] for e in self.list(kind)]

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        for e in self.entries:
            if isinstance(e, dict) and e.get("kind") == kind and e.get("name") == name:
                return e
        return None

    def has(self, kind: str, name: str) -> bool:
        return self.get(kind, name) is not None

    def reason(self, kind: str, name: str) -> Optional[str]:
        e = self.get(kind, name)
        if e is None:
            return "not registered"
        return entry_reason(e, self.components_dir)

    def is_valid(self, kind: str, name: str) -> bool:
        return self.reason(kind, name) is None

    def module_path(self, kind: str, name: str) -> Optional[str]:
        """Path to the component module, or None if the entry is invalid/missing."""
        e = self.get(kind, name)
        if e is None or entry_reason(e, self.components_dir) is not None:
            return None
        return str(self.components_dir / str(e["module"]))

    def invalid(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for e in self.entries:
            r = entry_reason(e, self.components_dir)
            if r is not None:
                nm = e.get("name") if isinstance(e, dict) else None
                kd = e.get("kind") if isinstance(e, dict) else None
                out.append({"kind": str(kd), "name": str(nm), "reason": r})
        return out


def load_registry_for_role(
    role: str,
    registry_dir: Optional[Path] = None,
    components_dir: Optional[Path] = None,
    tolerant: bool = True,
) -> ComponentRegistry:
    """Merge shared registry with the role-specific registry (tolerant by default)."""
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    shared, e1 = parse_registry_file(rdir / "shared.json")
    specific, e2 = parse_registry_file(rdir / f"{role}.json")
    if not tolerant and (e1 or e2):
        raise ValueError(f"unparseable registry: {e1 or e2}")
    entries = (shared or []) + (specific or [])
    return ComponentRegistry(role, entries, cdir)


def declared_modules(registry_dir: Optional[Path] = None) -> set:
    """Module paths (relative to ``gan/components``) declared by ANY registry file."""
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    declared = set()
    for fn in _REGISTRY_FILES:
        ents, err = parse_registry_file(rdir / fn)
        if err:
            continue
        for e in ents or []:
            if isinstance(e, dict) and e.get("module"):
                declared.add(str(e["module"]).replace("\\", "/"))
    return declared


def orphan_modules(registry_dir: Optional[Path] = None,
                   components_dir: Optional[Path] = None) -> List[str]:
    """Component-looking files that NO registry file declares (sorted, relative).

    This is the **single definition** of "orphan", shared by selection-time
    validation (``validate_registry``) and the commit gate
    (``code_repo.registry_report``) so the two can never disagree.

    A file counts only if it **exposes the tool API** (``tool_info`` +
    ``tool_function``). ``gan/components/**`` legitimately holds helper modules
    shared by several components, so "unregistered file" alone would be a false
    positive -- and, because a new orphan is what the commit gate refuses, it would
    reject any patch that adds such a helper. A file without the tool API cannot be
    selected, assembled or loaded anyway, so it cannot produce the silent failure
    this check exists to catch.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    declared = declared_modules(rdir)
    out: List[str] = []
    if cdir.is_dir():
        for f in sorted(cdir.rglob("*.py")):
            if f.name == "__init__.py":
                continue
            rel = f.relative_to(cdir).as_posix()
            if rel not in declared and _exposes_tool_api(f):
                out.append(rel)
    return out


def validate_registry(
    role: str,
    registry_dir: Optional[Path] = None,
    components_dir: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """All problems visible for ``role``'s merged registry (empty list = healthy).

    Each problem is ``{"type", "kind", "name", "reason"}`` with ``type`` one of:

    - ``invalid``     : a single entry fails ``entry_reason`` (incl. the identity contract);
    - ``duplicate``   : ``(kind, name)`` appears twice after merging shared + role
      (``ComponentRegistry.get`` returns the first, so the later one is silently
      shadowed);
    - ``orphan``      : a component-looking file that NO registry file declares (see
      ``orphan_modules``) -- it can never be selected until it is registered;
    - ``unparseable`` : a registry file that is not valid JSON / not an object with a
      ``components`` list. Reported explicitly rather than skipped: silently ignoring
      it would both hide the broken file and make every module it declares look like
      an orphan.

    Orphan detection is registry-set-wide (a file is not an orphan if any registry
    declares it), so it does not depend on which role is being validated.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    out: List[Dict[str, str]] = [{"type": "invalid", **p} for p in reg.invalid()]

    # duplicate (kind, name) in the merged registry
    seen = set()
    for e in reg.entries:
        if not isinstance(e, dict):
            continue
        key = (str(e.get("kind")), str(e.get("name")))
        if key in seen:
            out.append({"type": "duplicate", "kind": key[0], "name": key[1],
                        "reason": "duplicate (kind,name) after merging shared + role "
                                  "registry; the first entry wins"})
        seen.add(key)

    for rel in orphan_modules(rdir, cdir):
        out.append({"type": "orphan", "kind": "", "name": Path(rel).stem, "module": rel,
                    "reason": f"component file not registered in any registry: {rel}"})
    for fn in _REGISTRY_FILES:
        _ents, err = parse_registry_file(rdir / fn)
        if err:
            out.append({"type": "unparseable", "kind": "", "name": fn,
                        "reason": f"registry file is not parseable: {err}"})
    return out
