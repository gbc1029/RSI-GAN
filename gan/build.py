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
from gan.framework.role_process import RoleProcess
from gan.design import initial_config
from gan.design.store import DesignStore
from gan.framework.loop import GanLoop
from gan.framework.task_runner import DomainTaskRunner


def _seed_self_designs(output_dir: str, drop_eval_points: Optional[List[str]] = None, ledger_writer=None) -> None:
    """First-time initialization of planner/evaluator self-designs.

    An existing design file is AUTHORITATIVE — including deliberately empty
    selections (``tools: []``) or an empty prompt — and is never rewritten.
    Only a MISSING file is seeded. An UNPARSEABLE file is kept aside as
    ``config.json.corrupt-<ts>`` (forensics) and then re-seeded (self-heal, so a
    corrupt file cannot crash-loop the per-outer worker). Safe to call any number
    of times (``build_gan_loop`` runs once per outer worker).
    """
    store = DesignStore(paths.design_root(output_dir), writer=ledger_writer)
    for role in ("planner", "evaluator"):
        path = store.path(role)
        if os.path.exists(path):
            try:
                store.load(role)
                continue  # valid existing design: authoritative, do not touch
            except Exception as e:
                # The reseed is a deliberate availability trade-off (a
                # corrupt config must not crash-loop the per-outer worker), but
                # it silently RESETS this role's evolution -- that must be LOUD:
                # one structured event + one stderr line, with the corrupt file
                # kept aside as .corrupt-<ts> for forensics.
                corrupt_backup = shutil.copy2(path, f"{path}.corrupt-{int(time.time())}")
                try:
                    from utils import trajectory_log as _tlog
                    _tlog.append(paths.events_path(output_dir), {
                        "type": "self_design_reseeded", "role": role,
                        "path": os.path.abspath(path), "backup": corrupt_backup,
                        "error": str(e)[:300]})
                except Exception as le:  # noqa: BLE001 -- audit write: stderr covers
                    print(f"[WARN] self_design_reseeded event write failed: {le}")
                print(f"[WARN] {role} design file unparseable ({type(e).__name__}: {e}); "
                      f"reseeded to gen-0 seed — this role's evolution was reset "
                      f"(backup: {corrupt_backup})")
        # schema defaults + seed prompt; initial_config applies the non-empty
        # guard, so a missing/blank seed can no longer blank the prompt.
        cfg = initial_config(role)
        # Capability tools are optional but selected by default (evolvable
        # later); the single ``tools`` slot, name-keyed registry. BOTH roles
        # preselect their full registry at gen-0 (unification): the planner's
        # seed prompt promises the ctx_* faces, but an empty tools slot meant
        # the promise was false at gen-0 while the evaluator's was true.
        if role in ("planner", "evaluator"):
            from gan.registries.loader import load_registry_for_role
            reg = load_registry_for_role(role)
            if role == "evaluator":
                drop = set(drop_eval_points or [])
                # E-group ablation (docs/11): seed WITHOUT the dropped eval_points.
                # Only affects a MISSING design file; an existing design file is
                # authoritative and is never rewritten (change the drop list by
                # deleting the design file or deselecting manually).
                cfg["tools"] = [n for n in reg.names() if n not in drop]
            else:
                cfg["tools"] = reg.names()
        store.save(cfg, role)


def ensure_code_root(repo_root: str, output_dir: str) -> str:
    """Materialize the per-run code tree once (idempotent across re-entry).

    Raises when the tree was deleted externally (``.git`` gone while the code
    manifest is still present): silently re-materializing an initial tree would
    lose every evolution commit and pin invisible dead refs; the resume checkout
    would then fail silently and run on the wrong base. The operator must
    either restore the tree or explicitly reset the run.
    """
    from gan.framework import code_repo as code_repo_mod
    cr = paths.code_root(output_dir)
    manifest = paths.code_manifest(output_dir)
    if not os.path.isdir(os.path.join(cr, ".git")):
        if os.path.exists(manifest):
            raise RuntimeError(
                f"per-run code tree {cr} is missing (.git deleted) while {manifest} "
                f"still exists — refusing to silently re-materialize an initial tree "
                f"(evolution commits / task_* / outer_*_base refs would be lost and "
                f"resume would silently run on the wrong base). Restore the code tree "
                f"or delete the checkpoint + manifest to start over."
            )
        commit = code_repo_mod.materialize(repo_root, cr)
        code_repo_mod.write_manifest(output_dir, {
            "layout": "single",
            "commit": commit,
            "repo_root": os.path.abspath(repo_root),
        })
    return cr


def ensure_roots(repo_root: str, output_dir: str, layout: str = "single"):
    """Materialize the per-run code layout once (idempotent); single or split (docs/12)."""
    from gan.framework import code_repo as code_repo_mod
    from gan.framework.repo_roots import RepoRoots

    if layout == "split":
        base_dir = paths.base_root(output_dir)
        task_dir = paths.task_root(output_dir)
        roles_dir = paths.roles_root(output_dir)
        manifest = code_repo_mod.read_manifest(output_dir)
        task_git = os.path.join(task_dir, ".git")
        roles_git = os.path.join(roles_dir, ".git")
        if not (os.path.isdir(task_git) and os.path.isdir(roles_git)):
            if manifest.get("layout") == "split":
                raise RuntimeError(
                    f"split code repos missing ({task_dir} / {roles_dir}) while "
                    f"{paths.code_manifest(output_dir)} declares split layout — "
                    f"refusing to silently re-materialize. Restore the repos or "
                    f"delete the checkpoint + manifest to start over."
                )
            # Legacy single archive -> one-time migration (docs/12 §13.3): the
            # old single tree keeps its git history as the task repo; roles/base
            # are derived from it. Anything else materializes a fresh split tree.
            migrated = False
            if (manifest.get("layout") == "single"
                    and os.path.isdir(os.path.join(paths.code_root(output_dir), ".git"))):
                commits = code_repo_mod.migrate_single_to_split(repo_root, output_dir)
                migrated = True
            else:
                commits = code_repo_mod.materialize_split(repo_root, base_dir, task_dir, roles_dir)
            code_repo_mod.write_manifest(output_dir, {
                "layout": "split",
                "repo_root": os.path.abspath(repo_root),
                "base": {"path": base_dir},
                "task": {"commit": commits["task"]},
                "roles": {"commit": commits["roles"]},
                "planner": {"commit": commits["roles"]},
                "evaluator": {"commit": commits["roles"]},
            })
            if migrated:
                try:
                    from utils import trajectory_log as _tlog
                    _tlog.append(paths.events_path(output_dir), {
                        "type": "migration_event", "from_layout": "single",
                        "to_layout": "split", "task_commit": commits["task"],
                        "roles_commit": commits["roles"]})
                except Exception:  # noqa: BLE001 -- audit write: never fail the run
                    pass
        return RepoRoots(base=base_dir, task=task_dir, roles=roles_dir, layout="split")

    code_root = ensure_code_root(repo_root, output_dir)
    return RepoRoots.single(code_root)


def build_gan_loop(
    repo_root: str,
    output_dir: str,
    domains: List[str],
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    cfg_overrides: Optional[dict] = None,
    preflight: bool = False,
    code_repo: bool = True,
    layout: Optional[str] = None,
    sample_seed_base: Optional[int] = None,
    anchor_ids: Optional[list] = None,
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
    # resolve_entry() validates the call parameters at STARTUP (unknown
    # reasoning_effort values fail here, not as a mid-run gateway 400).
    t_entry = model_registry.resolve_entry("gan.task")
    p_entry = model_registry.resolve_entry("gan.planner")
    e_entry = model_registry.resolve_entry("gan.evaluator")
    t_model, t_effort = t_entry["model"], t_entry.get("reasoning_effort")
    p_model, p_effort = p_entry["model"], p_entry.get("reasoning_effort")
    e_model, e_effort = e_entry["model"], e_entry.get("reasoning_effort")

    os.makedirs(output_dir, exist_ok=True)
    # B46 step-1 gate (docs/7 appendix 26): one WriterToken per run, minted at
    # the single assembly point and threaded explicitly to every guarded
    # writer (scores / design stores / checkpoints). Never exported via env,
    # never stored module-global.
    from gan.framework.ledger import open_run as _ledger_open_run
    ledger_writer = _ledger_open_run(output_dir)
    _seed_self_designs(output_dir, drop_eval_points=list(
        cfg.get("ablation.evaluator_drop_eval_points") or []),
        ledger_writer=ledger_writer)

    code_root = None
    roots = None
    if code_repo:
        # Layout precedence: explicit arg > existing code manifest (resume
        # auto-detect) > loop.yaml code_layout > single. An explicit arg that
        # contradicts the persisted manifest is a hard error — the operator must
        # omit layout to resume the run as its manifest declares.
        from gan.framework import code_repo as code_repo_mod
        manifest_layout = code_repo_mod.read_manifest(output_dir).get("layout")
        if layout is None:
            layout = manifest_layout or cfg.get("loop.code_layout", "single")
        elif manifest_layout and manifest_layout != layout:
            raise ValueError(
                f"layout={layout!r} conflicts with the existing code manifest "
                f"(layout={manifest_layout!r}); omit layout to auto-detect")
        if layout not in ("single", "split"):
            raise ValueError(f"layout must be 'single' or 'split' (got {layout!r})")
        roots = ensure_roots(repo_root, output_dir, layout)
        if layout == "split":
            # The derived overlay is what role processes and the harness run on;
            # authoritative source lives in base/task/roles (see docs/12).
            from gan.framework.compose import build_composed_view
            code_root = paths.compose_root(output_dir)
            build_composed_view(roots.base, roots.task, roots.roles, code_root)
        else:
            code_root = roots.task

    # The loop owns the composed-view lifecycle (rebuild after each task/roles
    # commit or checkout), but the task runner applies the task patch INSIDE
    # its run; hand it a late-bound callback so the view is fresh before the
    # harness launches.
    loop_holder: dict = {}

    def _rebuild_view() -> None:
        lp = loop_holder.get("loop")
        if lp is not None:
            lp._rebuild_composed_view()

    # Fresh role instances per outer generation (design + tools + chat + workspace).
    # The loop's per-instantiation attempt id rides along so the role binds its
    # OUTER-level trajectory file to the attempt-keyed name at construction
    # (session content and stop records share one file; see loop._refresh_roles).
    def _planner_factory(outer: int, attempt: str, instance: Optional[str] = None):
        return RoleProcess("planner", p_model, output_dir,
                           instance=(instance or f"outer_{outer}"),
                           code_root=code_root, attempt_id=attempt,
                           reasoning_effort=p_effort)

    def _evaluator_factory(outer: int, attempt: str, instance: Optional[str] = None):
        return RoleProcess("evaluator", e_model, output_dir,
                           instance=(instance or f"outer_{outer}"),
                           code_root=code_root, attempt_id=attempt,
                           reasoning_effort=e_effort)

    runner = DomainTaskRunner(
        repo_root=repo_root,
        output_dir=output_dir,
        domain=domain,
        subset=subset,
        num_samples=num_samples,
        default_model=t_model,
        # Task-child transport: the effort rides the task env (single-purpose
        # process; mirrors GAN_LLM_TIMEOUT_S). Explicit allowlists in
        # gan/framework/task_execution.py, domains/harness.py and
        # domains/task_worker.py carry it into the sandbox.
        reasoning_effort=t_effort,
        code_root=code_root,
        task_repo=roots.task if (roots is not None and layout == "split") else None,
        rebuild_view=_rebuild_view,
        sample_seed_base=sample_seed_base,
        anchor_ids=anchor_ids,
        # Reserved cost-key ceiling (docs/10 §10): bounds the task design's
        # per-question token budget (clamped into the design snapshot by
        # DomainTaskRunner before persist; cost.token_budget_per_gen).
        token_budget_per_gen=cfg.get("cost.token_budget_per_gen"),
        ledger_writer=ledger_writer,
    )
    # Grants/diffs are against the per-run code baseline when code_repo is on;
    # only the parent harness receives repo_root for loading benchmark labels.
    broker = AccessBroker(
        code_root or repo_root, output_dir,
        deny_paths=frozen_deny_paths(),
        deny_deep=bool(cfg.get("source_access.deny_deep", False)),
        deep_view_only=bool(cfg.get("ablation.deep_view_only", False)),
        shallow_prompt_only=bool(cfg.get("ablation.shallow_prompt_only", False)),
        disable_shallow=bool(cfg.get("ablation.disable_shallow", False)),
        max_files=int(cfg.get("source_access.max_files_per_grant", 500)),
        max_bytes=int(cfg.get("source_access.max_bytes_per_grant", 50 * 1024 * 1024)),
        roots=roots,
    )
    loop = GanLoop(
        output_dir, domains, cfg,
        task_runner=runner,
        broker=broker,
        planner_factory=_planner_factory,
        evaluator_factory=_evaluator_factory,
        code_root=code_root,
        ledger_writer=ledger_writer,
        roots=roots,
    )
    loop_holder["loop"] = loop
    # explicit runtime record of the model configuration actually used
    loop.log_event({"type": "model_config",
                    **model_registry.describe(["gan.task", "gan.planner", "gan.evaluator"])})
    if code_root:
        loop.log_event({"type": "code_init", "code_root": code_root, "layout": layout})
    # Repair a persisted role design that names something the committed tree
    # cannot deliver (strip + forensic copy + report), BEFORE the integrity probe
    # so that probe sees the repaired tree. Non-fatal by design: the design file
    # is an agent-authored, losslessly repairable object, and it outlives the
    # session (aborting would refuse service without undoing anything).
    from gan.framework import preflight as preflight_mod
    design_repair = preflight_mod.repair_dangling_designs(
        paths.design_root(output_dir), code_root=code_root,
        writer=ledger_writer)
    if design_repair["stripped"] or design_repair["errors"]:
        loop.log_event({"type": "design_dangling_stripped", "stage": "preflight",
                        **design_repair})
        # the owning role learns about it in its next self receipt
        for s in design_repair["stripped"]:
            loop.preflight_design_stripped.setdefault(s["role"], []).append(s)
    # Registry/toolset integrity. Pure local check (filesystem + AST, no model calls),
    # so unlike the optional model probe it runs by default — a broken registry
    # otherwise degrades silently: invalid/orphan components are skipped by
    # selection/assembly/loading without any error. Set `loop.toolset_preflight: false`
    # to log-only (escape hatch for an already-dirty code tree). The
    # always-on files are checked too, split by owner: a broken FROZEN plumbing
    # file is fatal here, a role's own tool is reported and handed back to it.
    tool_results = preflight_mod.preflight_tools(code_root=code_root)
    loop.log_event({"type": "preflight_tools", "results": tool_results})
    if not preflight_mod.tools_ok(tool_results):
        if cfg.get("loop.toolset_preflight", True):
            raise RuntimeError(f"toolset preflight failed: {tool_results}")
        loop.log_event({"type": "preflight_tools_warning",
                        "hint": "loop.toolset_preflight=false; not failing fast"})
    if preflight:
        results = preflight_mod.preflight(["gan.task", "gan.planner", "gan.evaluator"])
        loop.log_event({"type": "preflight", "results": results})
        if not preflight_mod.all_ok(results):
            raise RuntimeError(f"model preflight failed: {results}")
    return loop
