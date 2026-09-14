import hashlib
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from hydra import compose, initialize_config_dir


def collect_and_summarize_results(output_dir):
    """Collect and summarize results from JSON files in the output directory.

    Args:
        output_dir (str): Directory containing per-episode results in JSON format.

    Returns:
        dict: A summary dictionary containing average progress, standard errors, and token usage.
    """
    results_summaries = defaultdict(list)

    # Load config
    # NOTE: Get the number of tasks that should have been run instead of what was successfully run
    config_dir = os.path.abspath(output_dir)
    with initialize_config_dir(config_dir=config_dir, version_base="1.1"):
        cfg = compose(config_name="config")
    expected_task_counts = cfg["eval"]["num_episodes"]

    # Collect per-episode results
    for env_name in os.listdir(output_dir):
        env_dir = os.path.join(output_dir, env_name)
        if not os.path.isdir(env_dir):
            continue
        for root, dirs, files in os.walk(env_dir):
            for filename in files:
                if (
                    filename.endswith(".json")
                    and not filename.endswith("_report.json")
                    and filename != "report.json"
                ):
                    json_filepath = os.path.join(root, filename)
                    with open(json_filepath, "r") as f:
                        episode_log = json.load(f)
                        results_summaries[env_name].append(episode_log)

    # Summarize results per environment and overall
    # overall_total_input_tokens = 0
    # overall_total_output_tokens = 0
    overall_env_summaries = {}
    env_avg_progressions = []
    # agent_config = None
    # client_config = None
    config_collected = False

    for env_name, episodes in results_summaries.items():
        env_episode_progress = []
        env_total_steps = 0
        # env_total_input_tokens = 0
        # env_total_output_tokens = 0
        # env_total_episodes = len(episodes)
        env_total_episodes = 0
        env_tasks = defaultdict(list)
        failed_action_candidates = []

        # Discover all expected tasks by scanning the directory structure
        env_dir = os.path.join(output_dir, env_name)
        expected_tasks = set()
        if os.path.isdir(env_dir):
            for env_subdir in os.listdir(env_dir):
                env_subdir_path = os.path.join(env_dir, env_subdir)
                if os.path.isdir(env_subdir_path):
                    task_dirs = [d for d in os.listdir(env_subdir_path) if os.path.isdir(os.path.join(env_subdir_path, d))]
                    # The env_subdir is a task dir
                    if not task_dirs:
                        full_task_name = f"{env_subdir}"
                        expected_tasks.add(full_task_name)
                    # Look for task directories within environment subdirectories
                    for task_dir in task_dirs:
                        task_dir_path = os.path.join(env_subdir_path, task_dir)
                        # Construct full task name as "env_subdir/task_dir"
                        full_task_name = f"{env_subdir}/{task_dir}"
                        expected_tasks.add(full_task_name)

        for episode_log in episodes:
            if (
                not config_collected
                and "client" in episode_log
                and "agent" in episode_log
            ):
                # agent_config = episode_log["agent"]
                # client_config = episode_log["client"]
                config_collected = True

            task_name = episode_log.get("task")
            env_tasks[task_name].append(episode_log)
            episode_progress = episode_log.get("progression", 0.0)
            env_episode_progress.append(episode_progress)
            env_total_steps += episode_log.get("num_steps", 0)
            # env_total_input_tokens += episode_log.get("input_tokens", 0)
            # env_total_output_tokens += episode_log.get("output_tokens", 0)

        # overall_total_input_tokens += env_total_input_tokens
        # overall_total_output_tokens += env_total_output_tokens

        env_task_summaries = {}
        for task_name, task_runs in env_tasks.items():
            task_episode_progress = [run.get("progression", 0.0) for run in task_runs]
            for run in task_runs:
                failed_action_candidates += run.get("failed_candidates", [])
            # task_count = len(task_runs)
            task_count = expected_task_counts.get(env_name, len(task_runs))
            env_total_episodes += task_count
            avg_task_progress = (
                sum(task_episode_progress) / task_count if task_count else 0.0
            )
            task_std_dev = (
                math.sqrt(
                    sum((x - avg_task_progress) ** 2 for x in task_episode_progress)
                    / task_count
                )
                if task_count > 1
                else 0.0
            )
            task_std_error = (
                task_std_dev / math.sqrt(task_count) if task_count > 1 else 0.0
            )

            env_task_summaries[task_name] = {
                "progression_percentage": 100 * avg_task_progress,
                "standard_error": 100 * task_std_error,
                "episodes_played": task_count,
            }

        # Add missing tasks with 0 progression
        for expected_task in expected_tasks:
            if expected_task not in env_task_summaries:
                task_count = expected_task_counts.get(env_name, 0)
                env_task_summaries[expected_task] = {
                    "progression_percentage": 0.0,
                    "standard_error": 0.0,
                    "episodes_played": task_count,
                }
                env_total_episodes += task_count

        avg_steps = env_total_steps / env_total_episodes if env_total_episodes else 0.0

        # Calculate mean and standard error for the environment
        env_avg_progress = (
            sum(env_episode_progress) / env_total_episodes
            if env_total_episodes
            else 0.0
        )
        env_avg_progressions.append(env_avg_progress)
        env_std_dev = (
            math.sqrt(
                sum((x - env_avg_progress) ** 2 for x in env_episode_progress)
                / env_total_episodes
            )
            if env_total_episodes > 1
            else 0.0
        )
        env_std_error = (
            env_std_dev / math.sqrt(env_total_episodes)
            if env_total_episodes > 1
            else 0.0
        )

        env_summary = {
            "progression_percentage": 100 * env_avg_progress,
            "standard_error": 100 * env_std_error,
            "average_steps": avg_steps,
            "episodes_played": env_total_episodes,
            "tasks": env_task_summaries,
            # "input_tokens": env_total_input_tokens,
            # "output_tokens": env_total_output_tokens,
            "failed_action_candidates": failed_action_candidates,
        }

        env_summary_filename = os.path.join(
            output_dir, env_name, f"{env_name}_report.json"
        )
        Path(env_summary_filename).parent.mkdir(parents=True, exist_ok=True)
        with open(env_summary_filename, "w") as f:
            json.dump(env_summary, f, indent=4)

        overall_env_summaries[env_name] = {
            "progression_percentage": env_summary["progression_percentage"],
            "standard_error": env_summary["standard_error"],
            "episodes_played": env_summary["episodes_played"],
            "failed_action_candidates": env_summary["failed_action_candidates"],
        }

    total_envs = len(env_avg_progressions)
    if total_envs > 0:
        overall_avg_progression = sum(env_avg_progressions) / total_envs
        env_standard_errors = [
            env_data["standard_error"] for env_data in overall_env_summaries.values()
        ]
        sum_of_squares = sum(se**2 for se in env_standard_errors)
        overall_std_error = math.sqrt(sum_of_squares) / total_envs
    else:
        overall_avg_progression = 0.0
        overall_std_error = 0.0

    summary = {
        "average_progress": 100 * overall_avg_progression,
        "standard_error": overall_std_error,
        "environments": overall_env_summaries,
        # "total_input_tokens": overall_total_input_tokens,
        # "total_output_tokens": overall_total_output_tokens,
        # "client": client_config,
        # "agent": agent_config,
    }

    summary_filename = os.path.join(output_dir, "report.json")
    with open(summary_filename, "w") as f:
        json.dump(summary, f, indent=4)
    return summary


def print_summary_table(summary):
    """Print a table summarizing overall and per-environment results.

    Args:
        summary (dict): Summary dictionary from `collect_and_summarize_results`.
    """
    print("\nSummary of Results:")
    print(
        f"Overall Average Progression: {summary['average_progress']:.2f}% ± {summary['standard_error']:.2f}%"
    )
    print("Per-Environment Results:")
    for env_name, env_data in summary["environments"].items():
        print(
            f"  {env_name}: {env_data['progression_percentage']:.2f}% ± {env_data['standard_error']:.2f}%, Episodes: {env_data['episodes_played']}"
        )


def get_unique_seed(process_num=None, episode_idx=0):
    """Generate a unique seed using process number, episode index, and high-resolution time."""
    pid = os.getpid()
    time_ns = time.time_ns()
    unique_str = f"{pid}_{process_num}_{episode_idx}_{time_ns}"
    hashed = hashlib.sha256(unique_str.encode()).hexdigest()
    seed = int(hashed[:8], 16)
    return seed
