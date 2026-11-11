import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import hydra
from hydra.utils import get_original_cwd
from omegaconf import DictConfig

from domains.balrog.evaluator import EvaluatorManager
from domains.balrog.utils import collect_and_summarize_results, print_summary_table


@contextmanager
def redirect_to_file(filepath):
    original = sys.stdout
    with open(filepath, "w") as file:
        sys.stdout = file
        try:
            yield
        finally:
            sys.stdout = original

def harness_balrog(config, agent_factory=None, sample_seed=None, anchor_ids=None):
    """Run the balrog evaluation for one generation.

    ``sample_seed`` / ``anchor_ids`` are the EPISODE PLAN handed down by the GAN
    framework (``task_broker`` -> ``--sample_seed`` / ``--anchor_ids``):
    ``anchor_ids`` are run-frozen ``<task>#<seed>`` episode ids, and
    ``sample_seed`` derives the rotating (per-generation) half. ``None`` keeps
    the historical behaviour (random episode seeds) for direct/legacy runs.
    """
    # original_cwd = get_original_cwd()
    original_cwd = ""

    # Determine output directory
    if config.eval.resume_from is not None:
        output_dir = config.eval.resume_from
    else:
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f") if config.eval.run_id is None else config.eval.run_id
        run_name = f"{timestamp}"
        output_dir = os.path.join(config.eval.output_dir, run_name)

        # Create the directory if it doesn't exist
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Setup logger
    log_filename = os.path.join(output_dir, "eval.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_filename)],
        force=True,
    )

    # Create an EvaluatorManager and run evaluation
    evaluator_manager = EvaluatorManager(
        config, original_cwd=original_cwd, output_dir=output_dir,
        sample_seed=sample_seed, anchor_ids=anchor_ids,
    )
    # Audit the plan BEFORE any episode runs: a crashed generation must still
    # say which episodes it intended (task_runner folds this into an event).
    evaluator_manager.write_plan(output_dir)
    if agent_factory is None:
        from domains.balrog.agents import AgentFactory
        agent_factory = AgentFactory(config)
    # CM SEAM SIDECAR (docs/19 §12.28 F5 P1). The trusted per-step sink owns the
    # audit file, and there is exactly ONE of them per harness run -- the same
    # path the framework reads back (``paths.task_cm_events_path`` ->
    # ``task_runner._ingest_cm_events``). This is computed HERE because the
    # output dir is only final once the run_name/``resume_from`` rule above has
    # run; an in-process ``AgentFactory`` simply has no setter, so legacy/direct
    # balrog runs are unaffected. Before this, every balrog generation ingested
    # "sidecar file missing" and the seam events lived only in the harness's own
    # ``[f5] ... cm_events=N`` print -- the audit stream had a hole exactly where
    # the memory window's cost is decided.
    if hasattr(agent_factory, "set_cm_events_target"):
        agent_factory.set_cm_events_target(
            os.path.join(output_dir, "cm_events.jsonl"))
    with redirect_to_file(log_filename):
        evaluator_manager.run(agent_factory)

    return output_dir

def report_balrog(output_dir, planned_counts=None):
    # Collect and summarize results. ``planned_counts`` is the coverage
    # denominator; default = the run's own episode_plan.json, so the report no
    # longer depends on the harness having saved config.yaml into output_dir
    # (P2-15).
    summary = collect_and_summarize_results(output_dir, planned_counts=planned_counts)
    print_summary_table(summary)

@hydra.main(config_path="config", config_name="config", version_base="1.1")
def main(config: DictConfig):
    # Harness
    output_dir = harness_balrog(config)
    # Report
    report_balrog(output_dir)

if __name__ == "__main__":
    main()
