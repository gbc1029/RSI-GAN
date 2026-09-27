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

**Registry entries are DATA, not code.** Since the workspace-first scan (below) the
``module`` value can come from a registry copy an agent edited this session, so it
is sanitised before use (``safe_module_rel``): an absolute path or any ``..``
segment would otherwise let ``module_path`` hand ``shutil.copy2`` a file from
outside ``gan/components``, which ``load_tools`` would then import and expose.

**Overlay (workspace-first).** Every public entry point accepts ``overlay_root`` =
the session workspace root (``broker.src_dir(role, node)``). With it, each registry
file and each component module resolves workspace-first and committed-tree second --
the same rule the deep tools use to scan, so the read side (``list_components``) and
the write side (``register_component`` / ``unregister_component``) can never
disagree about what the session currently declares. The workspace is only a
*partial* copy (granted paths only, cleared at each outer boundary), which is why
the committed tree remains the fallback rather than a competing view.

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


def write_registry_json(path, data: Dict[str, Any]) -> None:
    """Rewrite a registry JSON, PRESERVING its trailing-newline convention.

    Session patches are generated with ``difflib.unified_diff``, which cannot
    express "no newline at end of file". A registry that loses its final newline
    therefore produces a patch ``git apply`` rejects as corrupt, silently blocking
    every register/unregister. Writing through this one helper keeps the invariant
    in a single place.
    """
    p = str(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            had_nl = f.read().endswith("\n")
    except OSError:
        had_nl = True
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        if had_nl:
            f.write("\n")


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


def _as_dirs(components_dir) -> List[Path]:
    """Normalise ``components_dir`` to an ordered search path (first hit wins).

    A single ``Path`` (every pre-overlay caller: the commit gate, preflight,
    assembly, ``register_component``) and a list (session workspace copy first,
    then the committed tree) are both accepted, so the overlay can be threaded
    through existing signatures without a second code path.
    """
    if components_dir is None:
        return [COMPONENTS_DIR]
    if isinstance(components_dir, (list, tuple)):
        out = [Path(d) for d in components_dir if d is not None]
        return out or [COMPONENTS_DIR]
    return [Path(components_dir)]


def safe_module_rel(mod: Any) -> Optional[str]:
    """Sanitise a registry ``module`` value to a safe relative path, or None.

    A registry entry is **DATA**, not code: the workspace-first scan means the
    value can come from a registry copy the agent edited this session. ``components_dir
    / mod`` with an absolute path or a ``..`` segment escapes ``gan/components``,
    and ``module_path`` feeds ``shutil.copy2`` in ``gan/tools/assembly.py`` -- so an
    unsafe value pulls an out-of-tree file into the toolset, where ``load_tools``
    imports it and exposes its tools. Rejected here: the **single definition** used
    by ``entry_reason``, ``module_path`` and ``declared_modules``.
    (``unregister_component`` keeps its own guard because it deletes, not reads.)
    """
    if not isinstance(mod, str):
        return None
    m = mod.replace("\\", "/").strip()
    if not m or m.startswith("/") or (len(m) > 1 and m[1] == ":"):
        return None
    parts = [p for p in m.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def resolve_module(components_dir, mod: Any) -> Optional[Path]:
    """First existing ``<dir>/<mod>`` across the search path (``mod`` sanitised)."""
    rel = safe_module_rel(mod)
    if rel is None:
        return None
    for d in _as_dirs(components_dir):
        p = d / rel
        if p.is_file():
            return p
    return None


def entry_reason(entry: Any, components_dir) -> Optional[str]:
    """Return None if valid, else a short reason string.

    ``components_dir`` is a single ``Path`` or an ordered search path (session
    workspace copy first, committed tree second) -- see :func:`_as_dirs`.
    """
    if not isinstance(entry, dict):
        return "entry is not an object"
    for f in _REQUIRED:
        if f not in entry or entry[f] in (None, ""):
            return f"missing required field '{f}'"
    if entry["kind"] not in KINDS:
        return f"unknown kind '{entry['kind']}'"
    mod = safe_module_rel(entry["module"])
    if mod is None:
        return (f"unsafe module path: {entry['module']!r} (must be a relative path "
                f"under gan/components, with no '..' or absolute prefix)")
    p = resolve_module(components_dir, mod)
    if p is None:
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
    def __init__(self, role: str, entries: List[Any], components_dir):
        self.role = role
        self.entries = [e for e in (entries or [])]
        # an ordered search path, not a single dir: workspace copy first (the
        # version this session's patch commits), committed tree second
        self.components_dir = _as_dirs(components_dir)

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
        """Path to the component module, or None if the entry is invalid/missing.

        Resolves across the search path (workspace copy first), so an assembly that
        was given a workspace overlay materialises the session's own version.
        """
        e = self.get(kind, name)
        if e is None or entry_reason(e, self.components_dir) is not None:
            return None
        p = resolve_module(self.components_dir, e.get("module"))
        return str(p) if p is not None else None

    def invalid(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for e in self.entries:
            r = entry_reason(e, self.components_dir)
            if r is not None:
                nm = e.get("name") if isinstance(e, dict) else None
                kd = e.get("kind") if isinstance(e, dict) else None
                out.append({"kind": str(kd), "name": str(nm), "reason": r})
        return out


def _overlay_registries(overlay_root) -> Optional[Path]:
    """The session workspace's ``gan/registries`` dir, or None if there is none."""
    if overlay_root is None:
        return None
    p = Path(overlay_root) / "gan" / "registries"
    return p if p.is_dir() else None


def _component_dirs(components_dir: Path, overlay_root) -> List[Path]:
    """Ordered component search path: workspace copy first, committed tree second."""
    dirs: List[Path] = []
    if overlay_root is not None:
        ws = Path(overlay_root) / "gan" / "components"
        if ws.is_dir():
            dirs.append(ws)
    dirs.append(components_dir)
    return dirs


def _pick_registry_file(fn: str, rdir: Path, ov_rdir: Optional[Path]) -> Path:
    if ov_rdir is not None:
        p = ov_rdir / fn
        if p.is_file():
            return p
    return rdir / fn


def resolve_registry_file(
    fn: str,
    registry_dir: Optional[Path] = None,
    overlay_root=None,
) -> Tuple[Path, str]:
    """Workspace-first resolution of ONE registry file -> ``(path, origin)``.

    ``origin`` is ``"workspace"`` when the session workspace holds a copy -- that
    is the copy this session's patch will commit -- else ``"code"``. Public so
    ``list_components`` can report provenance per file. What an *unparseable*
    workspace copy means stays the caller's decision: the deep tools treat it as a
    hard error, and ``list_components`` reports it rather than silently falling
    back (a silent fallback would hide the agent's own corruption).
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    ov_rdir = _overlay_registries(overlay_root)
    return _pick_registry_file(fn, rdir, ov_rdir), ("workspace" if ov_rdir is not None
                                                    and (ov_rdir / fn).is_file() else "code")


def load_registry_for_role(
    role: str,
    registry_dir: Optional[Path] = None,
    components_dir: Optional[Path] = None,
    tolerant: bool = True,
    overlay_root=None,
) -> ComponentRegistry:
    """Merge shared registry with the role-specific registry (tolerant by default).

    ``overlay_root`` is the session **workspace** root (``broker.src_dir(role,
    node)``). When given, every registry file and every component module resolves
    **workspace-first, committed-tree second** -- the same rule the deep tools use
    to scan (S1/P3), so the read side and the write side can never disagree about
    what the session currently declares. The workspace is only a *partial* copy
    (granted paths only, cleared at each outer boundary), which is exactly why the
    committed tree stays the fallback instead of being a competing view.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    ov_rdir = _overlay_registries(overlay_root)
    shared, e1 = parse_registry_file(_pick_registry_file("shared.json", rdir, ov_rdir))
    specific, e2 = parse_registry_file(_pick_registry_file(f"{role}.json", rdir, ov_rdir))
    if not tolerant and (e1 or e2):
        raise ValueError(f"unparseable registry: {e1 or e2}")
    entries = (shared or []) + (specific or [])
    return ComponentRegistry(role, entries, _component_dirs(cdir, overlay_root))


def declared_modules(registry_dir: Optional[Path] = None, overlay_root=None) -> set:
    """Module paths (relative to ``gan/components``) declared by ANY registry file.

    With ``overlay_root`` the workspace registry files are unioned in: a module
    declared only by a registry copy the session just edited **is** declared, so it
    must not be reported as an orphan. Unsafe ``module`` values are dropped (see
    ``safe_module_rel``) -- a traversal string must not mark a real file declared.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    ov_rdir = _overlay_registries(overlay_root)
    bases = [ov_rdir, rdir] if ov_rdir is not None else [rdir]
    declared = set()
    for fn in _REGISTRY_FILES:
        for base in bases:
            ents, err = parse_registry_file(base / fn)
            if err:
                continue
            for e in ents or []:
                if isinstance(e, dict):
                    rel = safe_module_rel(e.get("module"))
                    if rel:
                        declared.add(rel)
    return declared


def orphan_modules(registry_dir: Optional[Path] = None,
                   components_dir: Optional[Path] = None,
                   overlay_root=None) -> List[str]:
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

    With ``overlay_root`` both component trees are scanned (workspace first) and the
    declared set is the union, so a file the session just created shows up as a
    candidate without the committed copies becoming invisible.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    declared = declared_modules(rdir, overlay_root=overlay_root)
    # rel -> does ANY copy expose the tool API (a broken workspace copy must not
    # hide a committed file that is a genuine orphan candidate)
    candidates: Dict[str, bool] = {}
    for d in _component_dirs(cdir, overlay_root):
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if f.name == "__init__.py":
                continue
            rel = f.relative_to(d).as_posix()
            candidates[rel] = candidates.get(rel, False) or _exposes_tool_api(f)
    return sorted(rel for rel, exposes in candidates.items()
                  if exposes and rel not in declared)


def validate_registry(
    role: str,
    registry_dir: Optional[Path] = None,
    components_dir: Optional[Path] = None,
    overlay_root=None,
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

    ``overlay_root`` adds the session workspace layer (see
    ``load_registry_for_role``): a problem is then reported for the copy the patch
    will actually commit, and ``unparseable`` carries ``origin`` so a broken
    workspace copy is never mistaken for a broken committed one.
    """
    rdir = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    cdir = Path(components_dir) if components_dir is not None else COMPONENTS_DIR
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir,
                                 overlay_root=overlay_root)
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

    for rel in orphan_modules(rdir, cdir, overlay_root=overlay_root):
        out.append({"type": "orphan", "kind": "", "name": Path(rel).stem, "module": rel,
                    "reason": f"component file not registered in any registry: {rel}"})
    for fn in _REGISTRY_FILES:
        path, origin = resolve_registry_file(fn, rdir, overlay_root=overlay_root)
        _ents, err = parse_registry_file(path)
        if err:
            out.append({"type": "unparseable", "kind": "", "name": fn, "origin": origin,
                        "reason": f"registry file is not parseable: {err}"})
    return out
