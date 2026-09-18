"""Factory: assemble a runnable GanLoop from config + a domain."""
from __future__ import annotations

import os
from typing import List, Optional

from gan.framework.access import AccessBroker
from gan.framework import models as model_registry
from gan.framework.frozen import deny_paths as frozen_deny_paths
from gan.framework.loader import load_gan_loop_config
from gan.design import load_seed
from gan.design.store import DesignStore
from gan.framework.loop import GanLoop
from gan.roles.evaluator import Evaluator
from gan.roles.planner import Planner
from gan.framework.task_runner import DomainTaskRunner


def _seed_self_designs(output_dir: str) -> None:
    """Seed planner/evaluator self-design prompts (and default eval points)."""
    store = DesignStore(os.path.join(output_dir, "design"))
    for role in ("planner", "evaluator"):
        cfg = store.load(role)
        if not cfg.get("prompt"):
            cfg["prompt"] = load_seed(role)
        # judgment eval points are optional but selected by default (evolvable later)
        if role == "evaluator" and not cfg.get("eval_points"):
            from gan.registries.loader import load_registry_for_role
            reg = load_registry_for_role("evaluator")
            cfg["eval_points"] = reg.names("eval_point")
        store.save(cfg, role)


def build_gan_loop(
    repo_root: str,
    output_dir: str,
    domains: Optional[List[str]] = None,
    task_domain: str = "paper_review",
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    cfg_overrides: Optional[dict] = None,
) -> GanLoop:
    cfg = load_gan_loop_config(cfg_overrides)
    domains = domains or [task_domain]

    # Single source: gan/framework/models.yaml (no env, no fallback, no overrides).
    t_model = model_registry.resolve("gan.task")
    p_model = model_registry.resolve("gan.planner")
    e_model = model_registry.resolve("gan.evaluator")

    os.makedirs(output_dir, exist_ok=True)
    _seed_self_designs(output_dir)

    planner = Planner(p_model, output_dir)
    evaluator = Evaluator(e_model, output_dir)
    runner = DomainTaskRunner(
        repo_root=repo_root,
        output_dir=output_dir,
        domain=task_domain,
        subset=subset,
        num_samples=num_samples,
        default_model=t_model,
    )
    broker = AccessBroker(repo_root, output_dir, deny_paths=frozen_deny_paths())
    loop = GanLoop(output_dir, domains, cfg, planner, evaluator, runner, broker=broker)
    # explicit runtime record of the model configuration actually used
    loop.log_event({"type": "model_config",
                    **model_registry.describe(["gan.task", "gan.planner", "gan.evaluator"])})
    return loop
