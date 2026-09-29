"""Startup preflight (FRAMEWORK, frozen).

Optional startup probes, run before the loop starts so that misconfiguration fails
fast instead of degrading silently at runtime:

- ``preflight``       : model availability — for every configured role key, make ONE
  minimal call and report availability. It NEVER substitutes a fallback model — a
  failure is reported (and callers may choose to abort), consistent with the
  no-fallback model policy.
- ``preflight_tools`` : registry + toolset integrity — validates each role's merged
  registry (invalid entries, duplicates, orphans), the always-on tool files (B7,
  split by OWNER: frozen plumbing is fatal, a role's own tools are reported and
  handed back to that role) and the basename collisions a real assembly would
  silently resolve by overwrite.
- ``repair_dangling_designs`` : B15 — a persisted design (planner/evaluator) whose
  ``tools`` slot names something the committed tree cannot deliver is REPAIRED at
  startup: the unreachable names are stripped, the previous file is kept aside as
  a forensic ``config.json.dangling-<ts>`` copy, and the fact is logged and
  carried into that role's next self receipt. Unlike the other probes this one
  repairs, because the design file is an agent-authored, losslessly repairable
  object and the repo already does exactly this for a CORRUPT design file
  (``gan/build.py:_seed_self_designs``); aborting would refuse service without
  undoing anything (the file outlives the session).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

from gan.framework import models as registry


def preflight(keys: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Probe each configured model key with a 1-token request.

    Returns ``{key: {"ok": bool, "model": str|None, "error": str|None}}``.
    """
    from agent.llm import get_response_from_llm

    chosen = list(keys) if keys is not None else list(registry.describe().keys())
    results: Dict[str, Dict[str, Any]] = {}
    for key in chosen:
        try:
            model = registry.resolve(key)
        except Exception as e:  # unresolved key
            results[key] = {"ok": False, "model": None, "error": f"unresolved: {e}"[:200]}
            continue
        try:
            get_response_from_llm("ping", model=model, temperature=0.0, max_tokens=1)
            results[key] = {"ok": True, "model": model, "error": None}
        except Exception as e:
            results[key] = {
                "ok": False, "model": model,
                "error": f"{type(e).__name__}: {e}"[:200],
            }
    return results


def all_ok(results: Dict[str, Dict[str, Any]]) -> bool:
    return all(bool(r.get("ok")) for r in results.values())


# -- registry / toolset integrity (startup, fail-fast) ------------------------
def assemble_collisions(
    role: str,
    code_root: Optional[str] = None,
    include_always_on: bool = True,
) -> Dict[str, List[str]]:
    """Basename collisions a real assembly would resolve by silent overwrite.

    ``assemble_tools_dir`` copies every source file to ``<dest>/<basename>``, so two
    different sources with the same file name end up fighting for one destination
    slot (last writer wins). Nothing in the framework detects this today, and the
    loser simply disappears from the toolset.

    Checks every source that *could* be assembled for this role — the always-on
    directories plus ALL registered component modules (not only the currently
    selected ones), so a latent collision is caught at registration time rather
    than surfacing later as a mysteriously missing tool.

    Which files a source dir contributes is decided by
    ``gan.tools.assembly.py_files_in`` — the same helper ``assemble_tools_dir``
    copies from, so this check cannot drift from the real assembly.

    Returns ``{basename: [source, ...]}`` for names produced by more than one source.
    """
    from gan.tools.assembly import always_on_dirs, gan_roots, py_files_in
    from gan.registries.loader import load_registry_for_role, resolve_module

    sources: List[str] = []
    if include_always_on:
        for src in always_on_dirs(role, code_root=code_root):
            sources.extend(py_files_in(src))
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    for e in reg.entries:
        if not isinstance(e, dict) or not e.get("module"):
            continue
        p = resolve_module(cdir, e.get("module"))
        if p is not None:
            sources.append(str(p))

    by_name: Dict[str, List[str]] = {}
    # de-dup by realpath first: the SAME module declared twice within a file is one
    # file, not a collision with itself (cross-file duplicates are impossible since
    # batch 6 -- one single-writer registry per role)
    for s in sorted({os.path.realpath(s) for s in sources}):
        by_name.setdefault(os.path.basename(s), []).append(s)
    return {k: v for k, v in by_name.items() if len(v) > 1}


def preflight_tools(
    roles: Iterable[str] = ("task", "planner", "evaluator"),
    code_root: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Validate each role's registry + toolset. Fail-fast probe (no model calls).

    Strict by design (unlike the differential commit gate): at startup there is no
    "pre-existing" state to preserve, so any problem is reported. A run with a
    broken registry would otherwise degrade silently — an unregistered or
    invalid component is skipped by selection/assembly/loading without any error.

    Returns ``{role: {"problems": [...], "collisions": {...}}}``.
    """
    from pathlib import Path
    from gan.registries.loader import validate_registry

    croot = code_root
    reg_dir = Path(os.path.join(croot, "gan", "registries")) if croot else None
    comp_dir = Path(os.path.join(croot, "gan", "components")) if croot else None
    out: Dict[str, Dict[str, Any]] = {}
    for role in roles:
        out[role] = {
            "problems": validate_registry(role, registry_dir=reg_dir, components_dir=comp_dir),
            "collisions": assemble_collisions(role, code_root=croot),
            # B30 (L4): catalog-metadata drift -- NON-fatal (bookkeeping, not a
            # capability loss and not something an interrupted run would silently
            # get wrong); reported here and on the planner's own receipt.
            "catalog_stale": catalog_stale(role, code_root=croot),
            # B7: always-on API exposure, split by owner (frozen = fatal below,
            # owned = reported and handed back to the role)
            "always_on": always_on_problems(role, code_root=croot),
        }
    return out


def always_on_problems(role: str, code_root: Optional[str] = None
                       ) -> Dict[str, List[Dict[str, str]]]:
    """B7: API-exposure check for the always-on tool files of ``role``.

    ``always_on_dirs``/``py_files_in`` decide WHICH files land in the toolset, and
    this check goes through the same two helpers, so it cannot drift from the real
    assembly. Each file is classified by OWNER via ``always_on_owner``:

    - ``frozen``: plumbing no role may modify (``work/common``, ``design``,
      ``deep``) -- a broken one silently removes a capability from every session
      and nobody inside the run can fix it, so it is FATAL at startup (the
      ``preflight_tools`` stance);
    - ``owned``: the role's own tools (``work/<role>``) -- an agent-authored
      artifact; reported, never fatal, and handed back to that role (the fix
      belongs in its next session, not in a refused startup).

    Returns ``{"frozen": [...], "owned": [...]}`` with ``{"name", "path",
    "reason", "owner"}`` items (``name`` = the tool basename in the toolset).
    """
    from gan.tools.assembly import always_on_index
    from gan.registries.loader import module_api_reason

    out: Dict[str, List[Dict[str, str]]] = {"frozen": [], "owned": []}
    for name, info in sorted(always_on_index(role, code_root=code_root).items()):
        reason = module_api_reason(info["path"])
        if reason is None:
            continue
        item = {"name": name, "path": str(info["path"]), "reason": reason,
                "owner": info.get("owner") or ""}
        out["owned" if info.get("owner") else "frozen"].append(item)
    return out


def catalog_stale(role: str, code_root: Optional[str] = None,
                  registry_dir=None, components_dir=None) -> List[Dict[str, str]]:
    """B30 (L4): entries whose description predates the current module bytes.

    Advisory by construction: the consumer is the OPERATOR (startup report) and the
    planner's own receipt -- never the evaluator, and never ``tools_ok``. A stale
    catalog description cannot change this generation's capability or score; it only
    misleads the next catalog reader, whose repair action (``update_component``)
    belongs to the planner.
    """
    from gan.registries.loader import load_registry_for_role, stale_metadata
    from gan.tools.assembly import gan_roots
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role,
                                 registry_dir=registry_dir or rdir,
                                 components_dir=components_dir or cdir)
    return stale_metadata(reg)


def repair_dangling_designs(design_root: str, code_root: Optional[str] = None,
                            roles: Iterable[str] = ("planner", "evaluator")
                            ) -> Dict[str, Any]:
    """B15: strip unreachable ``tools`` names from role designs, keep forensics.

    The committed tree is the authority for what a design can deliver
    (``heal_design_slots`` semantics: REMOVE only, never add or restore source).
    A persisted design that names something the tree cannot deliver would
    otherwise run an entire outer with a silently missing capability. The repair
    is lossless for every other key, the previous file is preserved as
    ``config.json.dangling-<ts>`` (mirroring ``_seed_self_designs``'s
    ``.corrupt-<ts>``), and each strip is reported so the owning role sees it in
    its next self receipt ("dangling slot names STRIPPED ... re-add only after the
    component is committed").

    Scope note: only ROLE designs are repaired. Task designs are per-generation
    artifacts (``design/task/<genid>/config.json``) whose authoritative copy
    travels in the tree meta and is already healed at persist time (B24); a
    startup pass must not rewrite that history.

    Returns ``{"checked": [{role, path}], "stripped": [{role, path, name, reason,
    forensic}], "errors": [...]}``.
    """
    from pathlib import Path
    import time
    from gan.design.store import DesignStore
    from gan.framework.task_execution import heal_design_slots

    out: Dict[str, Any] = {"checked": [], "stripped": [], "errors": []}
    store = DesignStore(design_root)
    for role in roles:
        path = store.path(role)
        if not os.path.exists(path):
            continue
        out["checked"].append({"role": role, "path": path})
        try:
            cfg = store.load(role)
            stripped = heal_design_slots(cfg, role, code_root)
        except Exception as e:  # noqa: BLE001 -- a broken design file is not a
            # startup fatality here: _seed_self_designs owns that repair and the
            # run continues on the seed; report and move on.
            out["errors"].append({"role": role, "path": path,
                                  "reason": f"{type(e).__name__}: {e}"[:160]})
            continue
        if not stripped:
            continue
        forensic = f"{path}.dangling-{int(time.time())}"
        try:
            os.replace(path, forensic)
        except OSError as e:
            forensic = f"<forensic copy failed: {e}>"[:160]
        store.save(cfg, role)
        for s in stripped:
            out["stripped"].append({"role": role, "path": path,
                                    "name": s.get("name"),
                                    "reason": str(s.get("reason"))[:120],
                                    "forensic": forensic})
    return out


def tools_ok(results: Dict[str, Dict[str, Any]]) -> bool:
    """True when nothing FATAL is wrong: registry problems, assembly collisions,
    or a broken FROZEN always-on file (B7).

    Role-owned always-on problems are deliberately excluded: they are an agent's
    own artifact, reported in ``always_on.owned`` and fed back to that role, so
    they must not refuse startup (fail-fast is reserved for state no participant
    can repair).
    """
    for r in results.values():
        if r.get("problems") or r.get("collisions"):
            return False
        if (r.get("always_on") or {}).get("frozen"):
            return False
    return True
