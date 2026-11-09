import hashlib
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from utils.trajectory_log import LLM_CALLS_SIDECAR_SUFFIX

# The GAN framework archives task evidence from exactly ONE place:
# ``<run>/outputs/<run_id>/agent_evals/chat_history_<qid>.jsonl``
# (gan/framework/trajectory.py ``collect``). Balrog writes one transcript per
# EPISODE under ``<output>/<env>/<task>/<task>_run_NN_chat_history.jsonl``, so
# the episode IS balrog's question and the qid is ``env__task__ep<NN>``.
AGENT_EVALS_DIRNAME = "agent_evals"


def episode_qid(env_name, task, episode_idx):
    """Filesystem-safe per-EPISODE id (balrog's analogue of a question id)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(task)).strip("_") or "task"
    return f"{env_name}__{safe}__ep{int(episode_idx):02d}"


def episode_llm_calls(output_dir, env_name, task, episode_idx):
    """Number of per-call LLM audit rows this episode produced (P2-18).

    Read from the episode's sidecar as mirrored into ``agent_evals/`` by
    ``evaluator.archive_episode_trajectory``. This is ATTRIBUTION ONLY: it says
    WHICH episode spent the calls. Money and tokens stay with the root-side
    proxy audit ledger -- the two are different measurements (the child counts
    its own calls, the proxy meters the gateway's usage) and must never be
    summed or reconciled against each other.

    ``None`` when the sidecar is absent (a pre-P2-18 round, an aborted episode
    that never reached the agent, or a legacy direct harness run).
    """
    if episode_idx is None or task is None:
        return None
    path = os.path.join(
        output_dir, AGENT_EVALS_DIRNAME,
        f"chat_history_{episode_qid(env_name, task, episode_idx)}.jsonl"
        f"{LLM_CALLS_SIDECAR_SUFFIX}",
    )
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return None


def planned_episode_counts(output_dir):
    """Per-``(env, task)`` PLANNED episode counts, read from the run's own plan.

    This is the coverage denominator. It used to come from re-composing
    ``config.yaml`` out of the output dir with hydra
    (``initialize_config_dir(output_dir)``), which only worked because the
    harness saves that file as its very last step -- any skipped save made the
    REPORT itself crash (P2-15). ``episode_plan.json`` is written BEFORE the
    first episode and lists exactly what was intended, so it is both a better
    source and an independent one.

    Returns ``{}`` when there is no plan (legacy direct harness run); callers
    then fall back to the counts that actually ran, exactly as the old code did
    when the config key was missing.
    """
    counts = {}
    try:
        with open(os.path.join(output_dir, "episode_plan.json"), "r", encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError, TypeError):
        return counts
    for ep in (plan.get("episodes") or []):
        if not isinstance(ep, dict):
            continue
        env = str(ep.get("env") or "")
        task = str(ep.get("task") or "")
        if env and task:
            counts[(env, task)] = counts.get((env, task), 0) + 1
    return counts


def collect_and_summarize_results(output_dir, planned_counts=None):
    """Collect and summarize results from JSON files in the output directory.

    Args:
        output_dir (str): Directory containing per-episode results in JSON format.
        planned_counts (dict, optional): ``{(env, task): n}`` planned episode
            counts. Defaults to the run's own ``episode_plan.json`` (see
            :func:`planned_episode_counts`); ``{}`` means "use what ran".

    Returns:
        dict: A summary dictionary containing average progress, standard errors, and token usage.
    """
    results_summaries = defaultdict(list)

    # Coverage denominator: the number of episodes that SHOULD have run. The
    # report must not depend on the harness having saved config.yaml (P2-15).
    if planned_counts is None:
        planned_counts = planned_episode_counts(output_dir)
    expected_task_counts = dict(planned_counts)

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
            # Denominator per TASK: the number of episodes PLANNED for this
            # (env, task) pair, so an episode that produced no log at all still
            # counts as a played-and-lost episode (the denominator must not
            # shrink). Falls back to what ran when there is no plan.
            task_count = expected_task_counts.get((env_name, task_name), len(task_runs))
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
                task_count = expected_task_counts.get((env_name, expected_task), 0)
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

    # --- per-EPISODE truth (the balrog analogue of per-question points) -------
    # The GAN framework reads exactly these three keys back from
    # runs/<genid>/report.json (gan/framework/loop.py:_read_per_question) to
    # compute anchor_correct / anchor_n and to feed the LCB parent selector.
    # Keys are episode ids ``<task>#<seed>`` -- the same ids frozen in
    # ckpt/sample_seed.json -- so an anchor episode can be paired across
    # generations. Episodes that never produced a log are simply absent here;
    # the framework scores a missing anchor as a miss.
    question_points = {}
    episodes = []
    aborted_episodes = []
    sidecar_missing = []
    llm_calls_total = 0
    action_distribution = Counter()
    steps_total = 0
    invalid_actions_total = 0
    _idx = 0
    for env_name, episode_logs in results_summaries.items():
        for episode_log in episode_logs:
            eid = episode_result_id(episode_log, _idx)
            _idx += 1
            while eid in question_points:  # defensive: never silently overwrite
                eid = f"{eid}+"
            points = max(0.0, min(1.0, float(episode_log.get("progression") or 0.0)))
            question_points[eid] = points
            row = {
                "id": eid,
                "env": env_name,
                "task": episode_log.get("task"),
                "seed": episode_log.get("seed"),
                "anchor": bool(episode_log.get("anchor")),
                "progression": points,
                "steps": episode_log.get("num_steps"),
                "invalid_actions": len(episode_log.get("failed_candidates") or []),
            }
            # Per-episode LLM CALL COUNT (P2-18). Attribution only -- see
            # episode_llm_calls(); the proxy audit ledger stays authoritative
            # for money and tokens.
            calls = episode_llm_calls(
                output_dir, env_name, episode_log.get("task"),
                episode_log.get("episode_idx"),
            )
            row["llm_calls"] = calls
            if calls is None:
                sidecar_missing.append(eid)
            else:
                llm_calls_total += calls
            # Objective facts for the evaluator (P1-9). The ACTION stream is
            # balrog's analogue of a predicted label: aggregated here so
            # ``policy.constant-output`` / ``policy.*-bias`` and
            # ``design.contract-format`` become decidable for this domain
            # (previously every one of them was UNKNOWN).
            freq = episode_log.get("action_frequency") or {}
            if isinstance(freq, dict):
                for act, n in freq.items():
                    try:
                        action_distribution[str(act)] += int(n)
                    except (TypeError, ValueError):
                        continue
            steps_total += int(episode_log.get("num_steps") or 0)
            invalid_actions_total += row["invalid_actions"]
            # An ABORTED episode is scored 0.0 (it was played and lost) but it is
            # flagged, so "the agent fails this task" and "the infrastructure
            # killed this episode" stay distinguishable offline.
            if episode_log.get("aborted"):
                row["aborted"] = True
                row["error"] = episode_log.get("error")
                aborted_episodes.append({"id": eid, "env": env_name,
                                         "task": episode_log.get("task"),
                                         "error": episode_log.get("error")})
            episodes.append(row)
    summary["question_points"] = question_points
    summary["question_ids_passed"] = sorted(
        k for k, v in question_points.items() if v >= 1.0 - 1e-9)
    summary["question_ids_failed"] = sorted(
        k for k, v in question_points.items() if v < 1.0 - 1e-9)
    summary["episodes"] = episodes
    summary["episodes_aborted"] = aborted_episodes
    # Guard denominator (P2-16b): the framework voids a round whose aborted share
    # exceeds ``domains.yaml max_aborted_ratio``; the ratio is stated here too so
    # offline analysis can see WHY a round was voided without recomputing it.
    summary["aborted_ratio"] = (
        len(aborted_episodes) / len(episodes) if episodes else 0.0)
    summary["episodes_sidecar_missing"] = sidecar_missing
    summary["llm_calls_total"] = llm_calls_total

    # --- episode-domain objective facts (P1-9) -------------------------------
    # The contract `compute_report_summary` (gan/framework/task_execution.py)
    # turns into `coverage` / `contract_ok` / `label_distribution` /
    # `overall_accuracy`. All of them are MEASUREMENTS over this run's own
    # episodes, never an interpretation:
    #   episodes_requested -- planned (episode_plan.json); the coverage
    #                         denominator that the runner's per-task
    #                         num_samples cannot express;
    #   episodes_played    -- rows that produced a log (aborted included);
    #   episodes_solved    -- progression == 1.0;
    #   invalid_action_rate -- failed_candidates / steps (contract violations);
    #   action_distribution -- sanitized action counts (constant-policy signal).
    summary["episodes_requested"] = int(sum(expected_task_counts.values())
                                        or len(episodes))
    summary["episodes_played"] = len(episodes)
    summary["episodes_solved"] = sum(
        1 for e in episodes if float(e["progression"]) >= 1.0 - 1e-9)
    summary["mean_progression"] = summary["average_progress"]
    summary["steps_total"] = steps_total
    summary["invalid_action_rate"] = (
        (invalid_actions_total / steps_total) if steps_total else 0.0)
    summary["action_distribution"] = dict(action_distribution)

    summary_filename = os.path.join(output_dir, "report.json")
    with open(summary_filename, "w") as f:
        json.dump(summary, f, indent=4)
    return summary


def episode_result_id(episode_log, fallback_index):
    """Episode id for a report row; legacy logs (pre-plan) get a stable fallback.

    New runs carry ``episode_id`` (``<task>#<seed>``). Old logs carry only the
    seed, so the same shape is reconstructed; logs predating seeds at all fall
    back to ``<task>#legacy<idx>`` -- unique, but never matching an anchor id,
    which is the honest outcome for an unpaired run.
    """
    eid = episode_log.get("episode_id")
    if eid:
        return str(eid)
    task = str(episode_log.get("task") or "task")
    seed = episode_log.get("seed")
    if isinstance(seed, int):
        return f"{task}#{seed}"
    return f"{task}#legacy{fallback_index}"


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
