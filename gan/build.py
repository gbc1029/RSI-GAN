"""Factory: assemble a runnable GanLoop from config + a domain."""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import List, Optional

from gan.framework.access import AccessBroker
from gan.framework import models as model_registry
from gan.framework import paths
from gan.framework.frozen import deny_paths as frozen_deny_paths
from gan.framework.loader import load_gan_loop_config
from gan.design import initial_config
from gan.design.store import DesignStore
from gan.framework.loop import GanLoop
from gan.roles.evaluator import Evaluator
from gan.roles.planner import Planner
from gan.framework.task_runner import DomainTaskRunner


def _seed_self_designs(output_dir: str) -> None:
    """First-time initialization of planner/evaluator self-designs.

    An existing design file is AUTHORITATIVE — including deliberately empty
    selections (``eval_points: []``) or an empty prompt — and is never rewritten.
    Only a MISSING file is seeded. An UNPARSEABLE file is kept aside as
    ``config.json.corrupt-<ts>`` (forensics) and then re-seeded (self-heal, so a
    corrupt file cannot crash-loop the per-outer worker). Safe to call any number
    of times (``build_gan_loop`` runs once per outer worker).
    """
    store = DesignStore(paths.design_root(output_dir))
    for role in ("planner", "evaluator"):
        path = store.path(role)
        if os.path.exists(path):
            try:
                store.load(role)
                continue  # valid existing design: authoritative, do not touch
            except Exception:
                shutil.copy2(path, f"{path}.corrupt-{int(time.time())}")
        # schema defaults + seed prompt; initial_config applies the non-empty
        # guard, so a missing/blank seed can no longer blank the prompt.
        cfg = initial_config(role)
        # judgment eval points are optional but selected by default (evolvable later)
        if role == "evaluator":
            from gan.registries.loader import load_registry_for_role
            reg = load_registry_for_role("evaluator")
            cfg["eval_points"] = reg.names("eval_point")
        store.save(cfg, role)


def ensure_code_root(repo_root: str, output_dir: str) -> str:
    """Materialize the per-run code tree once (idempotent across re-entry)."""
    from gan.framework import code_repo as code_repo_mod
    cr = paths.code_root(output_dir)
    if not os.path.isdir(os.path.join(cr, ".git")):
        commit = code_repo_mod.materialize(repo_root, cr)
        os.makedirs(os.path.dirname(paths.code_manifest(output_dir)), exist_ok=True)
        with open(paths.code_manifest(output_dir), "w", encoding="utf-8") as f:
            json.dump({"commit": commit, "repo_root": os.path.abspath(repo_root)}, f, indent=2)
    return cr


def build_gan_loop(
    repo_root: str,
    output_dir: str,
    domains: List[str],
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    cfg_overrides: Optional[dict] = None,
    preflight: bool = False,
    code_repo: bool = True,
) -> GanLoop:
    cfg = load_gan_loop_config(cfg_overrides)
    # Domain shape checks: `domains` is required input with NO default (the only
    # default lives in scripts/run_gan.py). Empty/multi fail fast before anything
    # is materialized; multi-domain evaluation is not implemented yet.
    if not domains or not [d for d in domains if str(d).strip()]:
        raise ValueError(f"domains is required and must be non-empty (got {domains!r}); "
                         f"refusing to run without an explicit task domain")
    if len(domains) != 1:
        raise ValueError(f"domains accepts exactly one domain (got {domains}); "
                         f"multi-domain evaluation is not implemented yet")
    domain = str(domains[0]).strip()
    if not domain:
        raise ValueError(f"domains parses to an empty domain (got {domains!r})")
    repo_root = os.path.abspath(repo_root)

    # Single source: gan/framework/models.yaml (no env, no fallback, no overrides).
    t_model = model_registry.resolve("gan.task")
    p_model = model_registry.resolve("gan.planner")
    e_model = model_registry.resolve("gan.evaluator")

    os.makedirs(output_dir, exist_ok=True)
    _seed_self_designs(output_dir)

    code_root = ensure_code_root(repo_root, output_dir) if code_repo else None

    # Fresh role instances per outer generation (design + tools + chat + workspace).
    def _planner_factory(outer: int):
        return Planner(p_model, output_dir, instance=f"outer_{outer}", code_root=code_root)

    def _evaluator_factory(outer: int):
        return Evaluator(e_model, output_dir, instance=f"outer_{outer}", code_root=code_root)

    runner = DomainTaskRunner(
        repo_root=repo_root,
        output_dir=output_dir,
        domain=domain,
        subset=subset,
        num_samples=num_samples,
        default_model=t_model,
        code_root=code_root,
    )
    # Grants/diffs are against the per-run code baseline when code_repo is on;
    # only the parent harness receives repo_root for loading benchmark labels.
    broker = AccessBroker(
        code_root or repo_root, output_dir,
        deny_paths=frozen_deny_paths(),
        max_files=int(cfg.get("source_access.max_files_per_grant", 500)),
        max_bytes=int(cfg.get("source_access.max_bytes_per_grant", 50 * 1024 * 1024)),
    )
    loop = GanLoop(
        output_dir, domains, cfg,
        task_runner=runner,
        broker=broker,
        planner_factory=_planner_factory,
        evaluator_factory=_evaluator_factory,
        code_root=code_root,
    )
    # explicit runtime record of the model configuration actually used
    loop.log_event({"type": "model_config",
                    **model_registry.describe(["gan.task", "gan.planner", "gan.evaluator"])})
    if code_root:
        loop.log_event({"type": "code_init", "code_root": code_root})
    if preflight:
        from gan.framework import preflight as preflight_mod
        results = preflight_mod.preflight(["gan.task", "gan.planner", "gan.evaluator"])
        loop.log_event({"type": "preflight", "results": results})
        if not preflight_mod.all_ok(results):
            raise RuntimeError(f"model preflight failed: {results}")
    return loop
