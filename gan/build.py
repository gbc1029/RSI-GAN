"""Factory: assemble a runnable GanLoop from config + a domain."""
from __future__ import annotations

import os
from typing import List, Optional

from gan.access import AccessBroker
from gan.config.loader import load_gan_loop_config
from gan.loop import GanLoop
from gan.roles.evaluator import Evaluator
from gan.roles.planner import Planner
from gan.task_runner import DomainTaskRunner


def build_gan_loop(
    repo_root: str,
    output_dir: str,
    domains: Optional[List[str]] = None,
    task_domain: str = "paper_review",
    subset: str = "_filtered_100_train",
    num_samples: int = 2,
    task_model: Optional[str] = None,
    planner_model: Optional[str] = None,
    evaluator_model: Optional[str] = None,
    cfg_overrides: Optional[dict] = None,
) -> GanLoop:
    cfg = load_gan_loop_config(cfg_overrides)
    domains = domains or [task_domain]

    t_model = task_model or os.environ.get("GAN_TASK_MODEL") or cfg.get("models.task", "gpt-4o-mini")
    p_model = planner_model or os.environ.get("GAN_MODEL_PLANNER") or cfg.get("models.planner", t_model)
    e_model = evaluator_model or os.environ.get("GAN_MODEL_EVALUATOR") or cfg.get("models.evaluator", t_model)

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
    broker = AccessBroker(repo_root, output_dir)
    return GanLoop(output_dir, domains, cfg, planner, evaluator, runner, broker=broker)
