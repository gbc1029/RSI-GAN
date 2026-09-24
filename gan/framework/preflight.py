"""Startup preflight (FRAMEWORK, frozen).

Optional startup probes, run before the loop starts so that misconfiguration fails
fast instead of degrading silently at runtime:

- ``preflight``       : model availability — for every configured role key, make ONE
  minimal call and report availability. It NEVER substitutes a fallback model — a
  failure is reported (and callers may choose to abort), consistent with the
  no-fallback model policy.
- ``preflight_tools`` : registry + toolset integrity — validates each role's merged
  registry (invalid entries, duplicates, orphans) and the basename collisions a
  real assembly would silently resolve by overwrite.
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
    from gan.registries.loader import load_registry_for_role

    sources: List[str] = []
    if include_always_on:
        for src in always_on_dirs(role, code_root=code_root):
            sources.extend(py_files_in(src))
    _tools, rdir, cdir = gan_roots(code_root)
    reg = load_registry_for_role(role, registry_dir=rdir, components_dir=cdir)
    for e in reg.entries:
        if not isinstance(e, dict) or not e.get("module"):
            continue
        p = os.path.join(str(cdir), str(e["module"]).replace("\\", "/"))
        if os.path.isfile(p):
            sources.append(p)

    by_name: Dict[str, List[str]] = {}
    # de-dup by realpath first: the SAME module declared twice (e.g. in shared.json
    # and <role>.json) is one file, not a collision with itself
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
        }
    return out


def tools_ok(results: Dict[str, Dict[str, Any]]) -> bool:
    """True when no role has registry problems or assembly collisions."""
    for r in results.values():
        if r.get("problems") or r.get("collisions"):
            return False
    return True
