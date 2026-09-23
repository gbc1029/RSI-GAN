import os
import sys

# Add the project root to Python path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import argparse
import importlib
import importlib.util
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pandas as pd
from hydra import compose, initialize_config_dir


_TASK_RESULT_PREFIX = "__RSI_TASK_RESULT__"
QUESTION_TIMEOUT = 300


def get_dataset(domain, subset="", dataset_root=None):
    # Only the parent harness receives this path. It is never placed in the
    # environment or payload of the sandboxed TaskAgent worker.
    root = os.path.abspath(dataset_root) if dataset_root else os.getcwd()
    if "imo_" in domain:
        rel = f"domains/imo/{domain.split('_')[-1]}bench{subset}.csv"
    elif domain in ["search_arena", "paper_review"]:
        rel = f"domains/{domain}/dataset{subset}.csv"
    else:
        return None
    path = os.path.join(root, rel)
    return pd.read_csv(path, dtype=str)


def _sandbox_path(run_root, host_path):
    run_root = os.path.realpath(run_root)
    host_path = os.path.realpath(host_path)
    try:
        contained = os.path.commonpath([run_root, host_path]) == run_root
    except ValueError:
        contained = False
    if not contained:
        raise ValueError(f"TaskAgent path must stay below the run directory: {host_path}")
    return "/workspace/" + os.path.relpath(host_path, run_root).replace(os.sep, "/")


def _add_empty_parents(command, path, created):
    parent = os.path.dirname(path)
    pending = []
    while parent and parent != "/" and parent not in created:
        pending.append(parent)
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    for item in reversed(pending):
        command.extend(["--dir", item])
        created.add(item)


def _sandbox_command(run_root, agent_path, trajectory_path):
    """Create a fail-closed Bubblewrap command for one TaskAgent question."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError(
            "TaskAgent sandbox is required, but 'bwrap' is not installed. "
            "Refusing to run TaskAgent without benchmark isolation."
        )

    agent_host = os.path.realpath(os.path.join(run_root, agent_path))
    _sandbox_path(run_root, agent_host)  # validates containment

    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--cap-drop", "ALL",
    ]

    # System runtime is read-only. No repository/data parent is mounted.
    created = {"/"}
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
        if os.path.exists(path):
            command.extend(["--ro-bind", path, path])
            created.add(path)

    # Support an interpreter installed in a venv/conda prefix outside /usr
    # without exposing its parent directories.
    python_prefix = os.path.realpath(sys.prefix)
    if not any(
        python_prefix == root or python_prefix.startswith(root + os.sep)
        for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64")
    ):
        _add_empty_parents(command, python_prefix, created)
        command.extend(["--ro-bind", python_prefix, python_prefix])
        created.add(python_prefix)

    command.extend([
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/workspace",
        "--dir", "/workspace/domains",
        "--ro-bind", agent_host, "/workspace/task_agent.py",
        "--ro-bind", os.path.join(run_root, "agent"), "/workspace/agent",
        "--ro-bind", os.path.join(run_root, "utils"), "/workspace/utils",
        "--ro-bind", os.path.join(run_root, "domains", "__init__.py"),
        "/workspace/domains/__init__.py",
        "--ro-bind", os.path.join(run_root, "domains", "task_worker.py"),
        "/workspace/domains/task_worker.py",
    ])
    # .gan_runtime only exists inside a GAN run copy (assembled by
    # gan.framework.task_execution). Legacy direct-harness sandbox runs do not
    # set GAN_TASK_DESIGN, so binding it must be conditional, not fatal.
    runtime_dir = os.path.join(run_root, ".gan_runtime")
    if os.path.isdir(runtime_dir):
        command.extend([
            "--ro-bind", runtime_dir,
            "/workspace/.gan_runtime",
        ])
    command.extend([
        # Expose only this question's trajectory file, never outputs/ or other
        # questions' trajectories, which may themselves contain benchmark data.
        "--bind", trajectory_path, "/workspace/trajectory.jsonl",
        "--tmpfs", "/workspace/scratch",
        "--chdir", "/workspace/scratch",
        "--setenv", "HOME", "/tmp",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PWD", "/workspace/scratch",
        "--setenv", "PYTHONPATH", "/workspace",
        sys.executable,
        "-m", "domains.task_worker",
    ])
    return command


def _run_sandboxed_agent(model, inputs, agent_path, trajectory_path):
    run_root = os.path.realpath(os.getcwd())
    trajectory_path = os.path.realpath(trajectory_path)
    _sandbox_path(run_root, trajectory_path)  # validates containment
    os.makedirs(os.path.dirname(trajectory_path), exist_ok=True)
    # A file bind must exist before Bubblewrap constructs the namespace.
    with open(trajectory_path, "a", encoding="utf-8"):
        pass

    payload = {
        "model": model,
        "inputs": inputs,
        "agent_path": "/workspace/task_agent.py",
        "trajectory_path": "/workspace/trajectory.jsonl",
    }
    child_env = dict(os.environ)
    for name in ("GAN_DATASET_ROOT", "PYTHONHOME", "PYTHONPATH", "OLDPWD"):
        child_env.pop(name, None)

    proc = subprocess.run(
        _sandbox_command(run_root, agent_path, trajectory_path),
        input=json.dumps(payload, ensure_ascii=False, default=str),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        timeout=QUESTION_TIMEOUT,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no worker output")[-2000:]
        raise RuntimeError(f"Sandboxed TaskAgent failed (rc={proc.returncode}): {detail}")
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(_TASK_RESULT_PREFIX):
            result = json.loads(line[len(_TASK_RESULT_PREFIX):])
            return result["prediction"]
    raise RuntimeError("Sandboxed TaskAgent returned no result sentinel")


def run_agent(TaskAgent, model, row, evals_folder, format_input_dict,
              question_id_col, sandbox_task_agent=False, agent_path="./task_agent.py"):
    question_id = row[question_id_col]
    chat_history_path = os.path.join(evals_folder, f"chat_history_{question_id}.jsonl")
    inputs = format_input_dict(row)
    if sandbox_task_agent:
        return _run_sandboxed_agent(model, inputs, agent_path, chat_history_path)
    agent = TaskAgent(model=model, chat_history_file=chat_history_path)
    prediction, _ = agent.forward(inputs)
    return prediction


def load_task_agent(agent_path: str):
    """
    agent_path can be:
      - a python file path: ./task_agent.py or /abs/path/task_agent.py
      - a module path: proofgrader.task_agent or my_pkg.my_agent
    Returns: TaskAgent class
    """
    # Case 1: looks like a file path or exists on disk
    if agent_path.endswith(".py") or os.path.exists(agent_path):
        abs_path = os.path.abspath(agent_path)
        spec = importlib.util.spec_from_file_location("agent_module", abs_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec from file: {abs_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "TaskAgent"):
            raise AttributeError(f"No TaskAgent found in file: {abs_path}")
        return mod.TaskAgent

    # Case 2: interpret as module path
    mod = importlib.import_module(agent_path)
    if not hasattr(mod, "TaskAgent"):
        raise AttributeError(f"No TaskAgent found in module: {agent_path}")
    return mod.TaskAgent

def harness(
    agent_path="./task_agent.py",
    output_dir="./outputs",
    run_id=None,
    domain="search_arena",
    num_samples=-1,
    save_interval=100,
    num_workers=5,
    resume_from=None,
    subset="",
    proofs_dname=None,
    model=None,
    dataset_root=None,
):
    # Dynamically import functions based on the domain
    utils_prefix = domain.split("_", 1)[1] + "_" if domain.startswith("imo_") else ""
    domain_folder = domain.split('_')[0] if "imo_" in domain else domain
    utils_module_path = f"domains.{domain_folder}.{utils_prefix}utils"
    utils_module = importlib.import_module(utils_module_path)
    format_input_dict = utils_module.format_input_dict
    question_id_col = utils_module.QUESTION_ID
    # Model is passed explicitly by the driver (from gan/framework/models.yaml).
    # No environment lookup, no fallback.
    if not model:
        raise ValueError(
            "domains.harness requires an explicit --model (resolved from "
            "gan/framework/models.yaml by the driver)."
        )

    # GAN calls provide dataset_root and must always use the sandbox. Legacy
    # direct harness calls retain their original same-process behavior.
    sandbox_task_agent = dataset_root is not None
    TaskAgent = None if sandbox_task_agent else load_task_agent(agent_path)

    # Specify output folder
    if resume_from:
        output_folder = os.path.abspath(resume_from)
    else:
        run_id = (
            datetime.now().strftime("%Y%m%d_%H%M%S_%f") if run_id is None else run_id
        )
        output_folder = os.path.join(os.getcwd(), output_dir, run_id)

    # Create output folder
    evals_folder = os.path.join(output_folder, "agent_evals")
    os.makedirs(evals_folder, exist_ok=True)
    output_path = os.path.join(output_folder, "predictions.csv")
    failures_path = os.path.join(evals_folder, "eval_failures.jsonl")

    # Load existing predictions if available
    if os.path.exists(output_path):
        existing_df = pd.read_csv(output_path, dtype=str)
        # A prediction only counts as "done" if it is non-null AND non-blank;
        # blank predictions (e.g. after an isolated per-question failure) must be retried.
        _done = existing_df["prediction"].notna() & (
            existing_df["prediction"].astype(str).str.strip() != ""
        )
        completed_ids = set(existing_df[_done][question_id_col])
    else:
        existing_df = None
        completed_ids = set()

    # Get dataset
    if proofs_dname:
        dataset = pd.read_csv(os.path.join(proofs_dname, "predictions.csv"), dtype=str)
        dataset["Response"] = dataset["prediction"].copy()
        dataset.drop(columns=["prediction"], inplace=True)
    else:
        dataset = get_dataset(
            domain=domain, subset=subset, dataset_root=dataset_root,
        )
    if num_samples > 0:
        dataset = dataset[:num_samples]

    # Add a prediction column
    if existing_df is not None:
        dataset = dataset.merge(
            existing_df[[question_id_col, "prediction"]], on=question_id_col, how="left"
        )
    else:
        dataset["prediction"] = None

    predictions = dataset["prediction"].tolist()
    futures = []

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        for i, row in dataset.iterrows():
            pred = row["prediction"]
            is_done = pd.notna(pred) and str(pred).strip() != ""
            if is_done or row[question_id_col] in completed_ids:  # pyright: ignore
                continue
            futures.append(
                (
                    i,
                    executor.submit(
                        run_agent,
                        TaskAgent, model, row, evals_folder,
                        format_input_dict, question_id_col,
                        sandbox_task_agent, agent_path,
                    ),
                )
            )

        failures = []
        for idx, future in futures:
            try:
                prediction = future.result()
            except Exception as e:
                # Per-question isolation: a single failure must not abort the batch.
                # Record a blank prediction (dropped by report -> lower coverage) and
                # log the failure for the record.
                prediction = ""
                try:
                    qid = dataset.at[idx, question_id_col]
                except Exception:
                    qid = str(idx)
                failures.append(
                    {"idx": int(idx), "question_id": str(qid), "error": str(e)[:500]}
                )
            predictions[idx] = prediction

            if (idx + 1) % save_interval == 0:
                dataset["prediction"] = predictions
                dataset.to_csv(output_path, index=False)
                if failures:
                    with open(failures_path, "w", encoding="utf-8") as f:
                        for rec in failures:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"Checkpoint saved to {output_path}")

    # Final save
    dataset["prediction"] = predictions
    dataset.to_csv(output_path, index=False)
    print(f"Final predictions saved to {output_path}")

    if failures:
        with open(failures_path, "w", encoding="utf-8") as f:
            for rec in failures:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{len(failures)} question(s) failed and were isolated (see eval_failures.jsonl)")

    return output_folder


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="Evaluate a system on the search arena dataset."
    )
    parser.add_argument(
        "--agent_path", type=str, default="./task_agent.py", help="Path to the agent"
    )
    parser.add_argument(
        "--output_dir", type=str, default="./outputs", help="Output directory"
    )
    parser.add_argument("--run_id", type=str, default=None, help="Run ID")
    parser.add_argument(
        "--domain",
        type=str,
        choices=[
            "search_arena",
            "paper_review",
            "balrog_babyai",
            "balrog_babaisai",
            "balrog_minihack",
            "balrog_nle",
            "genesis_go2walking",
            "genesis_go2walkback",
            "genesis_go2hop",
            "imo_grading",
            "imo_proof",
            "imo_proof_grading",  # To grade generated proofs with an agent
        ],
        required=True,
        help="Domain to evaluate",
    )
    parser.add_argument(
        "--num_samples",
        type=int,
        default=-1,
        help="Number of samples to evaluate, -1 for all",
    )
    parser.add_argument(
        "--save_interval", type=int, default=100, help="Save to CSV every n samples"
    )
    parser.add_argument(
        "--num_workers", type=int, default=5, help="Number of parallel workers"
    )
    parser.add_argument(
        "--resume_from",
        type=str,
        default=None,
        help="Path to an existing output folder to resume from",
    )
    parser.add_argument(
        "--subset", type=str, default="", help="Subset of the dataset to evaluate"
    )
    parser.add_argument(
        "--proofs_dname", type=str, default="", help="Path to the directory containing proofs to grade (for imo_proof_grading)"
    )
    parser.add_argument(
        "--model", type=str, required=True,
        help="Model id, resolved from gan/framework/models.yaml by the driver (passed explicitly; no env/fallback).",
    )
    parser.add_argument(
        "--dataset_root", type=str, default=None,
        help="Parent-only benchmark root. Providing it requires sandboxed TaskAgent execution.",
    )
    args = parser.parse_args()

    domain = args.domain
   # Make proofs_dname required for imo_proof_grading
    if domain == "imo_proof_grading" and not args.proofs_dname:
        parser.error("--proofs_dname is required when domain is 'imo_proof_grading'")

    # Human preferences domains
    if domain in ["search_arena", "paper_review", "imo_grading", "imo_proof", "imo_proof_grading"]:
        output_folder = harness(
            agent_path=args.agent_path,
            output_dir=args.output_dir,
            run_id=args.run_id,
            domain=args.domain,
            num_samples=args.num_samples,
            save_interval=args.save_interval,
            num_workers=args.num_workers,
            resume_from=args.resume_from,
            subset=args.subset,
            proofs_dname=args.proofs_dname,
            model=args.model,
            dataset_root=args.dataset_root,
        )

    # Balrog game domains
    elif "balrog" in domain:
        from domains.balrog.eval import harness_balrog

        env_name = domain.split("_")[-1]
        config_dir = os.path.join(os.getcwd(), "./domains/balrog/config")
        with initialize_config_dir(config_dir=config_dir, version_base="1.1"):
            cfg = compose(
                config_name="config",
                overrides=[
                    f"eval.output_dir={args.output_dir}",
                    f"eval.num_workers={args.num_workers}",
                    f"envs.names={env_name}",
                    f"eval.run_id={args.run_id if args.run_id is not None else 'null'}",
                    f"model={args.model}",
                ]
                + (
                    [f"eval.num_episodes.{env_name}={args.num_samples}"]
                    if args.num_samples > 0
                    else []
                )
                + (
                    [f"eval.resume_from={args.resume_from}"]
                    if args.resume_from is not None
                    else []
                ),
            )
            output_folder = harness_balrog(cfg)
            # Save cfg in output folder
            from omegaconf import OmegaConf
            OmegaConf.save(config=cfg, f=os.path.join(output_folder, "config.yaml"))

    # Genesis Robotic Control Domains
    elif "genesis" in domain:
        from domains.genesis.eval import harness_genesis

        env_name = domain.split("_")[-1]
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        config_dir = os.path.join(root_dir, "domains/genesis/config")
        with initialize_config_dir(config_dir=config_dir, version_base="1.1"):
            num_workers = 1
            cfg = compose(
                config_name="config",
                overrides=[
                    f"eval.output_dir={args.output_dir}",
                    f"eval.num_workers={num_workers}",
                    f"envs.names={env_name}",
                    f"eval.run_id={args.run_id if args.run_id is not None else 'null'}",
                    f"utils.root_dir={root_dir}",
                    f"model={args.model}",
                ]
                + (
                    [f"eval.num_episodes.{env_name}={args.num_samples}"]
                    if args.num_samples > 0
                    else []
                )
            )
            output_folder = harness_genesis(cfg)
            # Save cfg in output folder
            from omegaconf import OmegaConf
            OmegaConf.save(config=cfg, f=os.path.join(output_folder, "config.yaml"))
