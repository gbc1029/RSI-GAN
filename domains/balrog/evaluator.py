import csv
import json
import logging
import multiprocessing
import os
import random
import re
import shutil
import traceback
from collections import defaultdict
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf
from tqdm import tqdm

from domains.balrog.dataset import InContextDataset
from domains.balrog.environments import make_env
from domains.balrog.utils import get_unique_seed
from utils.episode_seed import anchor_seeds_by_task, plan_episodes
from utils.trajectory_log import LLM_CALLS_SIDECAR_SUFFIX

logger = logging.getLogger(__name__)

# The GAN framework archives task evidence from exactly ONE place:
# ``<run>/outputs/<run_id>/agent_evals/chat_history_<qid>.jsonl``
# (gan/framework/trajectory.py ``collect``). Balrog writes one transcript per
# EPISODE under ``<output>/<env>/<task>/<task>_run_NN_chat_history.jsonl``, so
# without the mirror below every generation died as
# ``trajectory_archive_failed`` and the evaluator lost its evidence entirely.
# The episode IS balrog's question, so the qid is ``env__task__ep<NN>``.
AGENT_EVALS_DIRNAME = "agent_evals"


def episode_qid(env_name, task, episode_idx):
    """Filesystem-safe per-EPISODE id (balrog's analogue of a question id)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(task)).strip("_") or "task"
    return f"{env_name}__{safe}__ep{int(episode_idx):02d}"


def archive_episode_trajectory(output_dir, env_name, task, episode_idx):
    """Mirror one episode transcript into ``<output>/agent_evals/``.

    A COPY, never a move: the upstream per-task layout (CSV + transcript next to
    the episode JSON) stays as the domain's own evidence. ``copyfile`` (not
    ``copy2``) keeps this free of copystat/chmod traps on files the sandboxed
    uid wrote. Returns the mirrored path, or ``None`` when the episode produced
    no transcript (then the framework's archive failure is the honest signal).
    """
    src = os.path.join(
        output_dir,
        env_name,
        task,
        f"{task}_run_{int(episode_idx):02d}_chat_history.jsonl",
    )
    if not os.path.isfile(src):
        logging.warning(
            "no episode transcript to archive (%s/%s ep%d): %s",
            env_name, task, episode_idx, src,
        )
        return None
    dest_dir = os.path.join(output_dir, AGENT_EVALS_DIRNAME)
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = os.path.join(dest_dir, f"chat_history_{episode_qid(env_name, task, episode_idx)}.jsonl")
    shutil.copyfile(src, dest)
    # Per-call LLM audit rows ride a sidecar next to the transcript (agent/llm.py
    # -> agent/llm_withtools.py). Mirror it when present so the framework's
    # ``llm_calls`` sidecar fold-in finds it (advisory on absence: sandboxed
    # step children currently do not hand their sidecar back).
    src_sidecar = src + LLM_CALLS_SIDECAR_SUFFIX
    if os.path.isfile(src_sidecar):
        shutil.copyfile(src_sidecar, dest + LLM_CALLS_SIDECAR_SUFFIX)
    return dest

def to_jsonable(x):
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):  # numpy scalars (e.g., np.int64)
        return x.item()
    if isinstance(x, dict):
        return {k: to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    return x

class EvaluatorManager:
    """Manages evaluation of agents across multiple environments and tasks.

    The EvaluatorManager initializes evaluators for each specified environment and handles the execution
    of evaluation tasks either sequentially or in parallel using multiple workers.
    """

    def __init__(self, config, original_cwd="", output_dir="", sample_seed=None, anchor_ids=None):
        """Initialize the EvaluatorManager.

        Args:
            config (omegaconf.DictConfig): Configuration object containing evaluation settings.
            original_cwd (str, optional): Original current working directory. Defaults to "".
            output_dir (str, optional): Directory to save evaluation outputs. Defaults to ".".
            sample_seed (int, optional): This generation's rotating-episode seed
                (framework ``derive_sample_seed(base_seed, genid)``). ``None``
                keeps the legacy random-per-episode behaviour.
            anchor_ids (list, optional): Run-frozen ``<task>#<seed>`` episode ids
                (``ckpt/sample_seed.json``); the first slots of every task.
        """
        self.config = config
        self.original_cwd = original_cwd
        self.output_dir = output_dir or "."
        self.sample_seed = sample_seed
        # Malformed ids are REFUSED (utils.episode_seed.split_episode_id raises):
        # silently dropping one would shrink the anchor core while the run still
        # looked healthy.
        self.anchor_seeds = anchor_seeds_by_task(anchor_ids or [])

        self.env_names = config.envs.names.split("-")
        self.env_evaluators = {}
        self.episodes = []
        for env_name in self.env_names:
            evaluator = Evaluator(env_name, config, original_cwd=original_cwd, output_dir=self.output_dir)
            self.env_evaluators[env_name] = evaluator
            known_tasks = {str(t) for t in evaluator.tasks}
            # Fail closed: a plan naming a task the harness does not know means
            # domains.yaml ``episode_tasks`` and config.yaml drifted apart.
            unknown = [t for t in self.anchor_seeds if t not in known_tasks]
            if unknown:
                raise RuntimeError(
                    f"episode plan names task(s) absent from config.tasks.{env_name}_tasks: "
                    f"{unknown} -- domains.yaml episode_tasks and "
                    f"domains/balrog/config/config.yaml disagree"
                )
            for task in evaluator.tasks:
                task = str(task)
                slots = plan_episodes(
                    task, evaluator.num_episodes,
                    self.anchor_seeds.get(task, []), self.sample_seed,
                )
                for slot in slots:
                    episode_idx = slot["episode_idx"]
                    # Check if task has been completed
                    json_filename = os.path.join(
                        self.output_dir,
                        env_name,
                        task,
                        f"{task}_run_{episode_idx:02d}.json",
                    )
                    if os.path.exists(json_filename):
                        logging.info(f"Skipping completed task: {env_name}, {task}, episode {episode_idx}")
                    else:
                        self.episodes.append({**slot, "env": env_name, "task": task})
        self.num_workers = config.eval.num_workers

    def write_plan(self, output_dir):
        """Persist this generation's episode plan (audit; read back by task_runner)."""
        path = os.path.join(output_dir, "episode_plan.json")
        payload = {
            "sample_seed": self.sample_seed,
            "anchor_seeds": {k: list(v) for k, v in self.anchor_seeds.items()},
            "episodes": self.episodes,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
        return path

    def run(self, agent_factory):
        """Run the evaluation using the specified agent factory.

        Args:
            agent_factory (AgentFactory): Factory object to create agents for evaluation.

        Returns:
            dict: Results of the evaluation aggregated by environment name.
        """
        if self.num_workers > 1:
            results = self._run_parallel(agent_factory)
        else:
            results = self._run_sequential(agent_factory)

        # Episode-level failure isolation. ONE broken episode (a gateway 502, a
        # malformed env response, a crashed step child) must not void the whole
        # generation: it is recorded as an aborted row scored 0.0, exactly like
        # an episode the agent lost. But when EVERY episode aborted the honest
        # reading is "infrastructure", not "the agent plays terribly", so fail
        # loudly and let the framework mark the generation invalid instead of
        # feeding a 0.0 into the lineage (that would be a fabricated datapoint).
        planned = len(self.episodes)
        aborted = [log for logs in results.values() for log in logs if log.get("aborted")]
        if planned and len(aborted) >= planned:
            first = aborted[0].get("error") if aborted else "unknown"
            raise RuntimeError(
                f"all {planned} balrog episode(s) aborted -- treating this generation as an "
                f"infrastructure failure rather than a zero score (first error: {first})"
            )
        if aborted:
            logging.warning(
                "balrog: %d/%d episode(s) aborted and scored 0.0 (first error: %s)",
                len(aborted), planned, aborted[0].get("error"),
            )
        return results

    def _record_aborted(self, ep, error, tb=None):
        """Persist an ABORTED episode row and return its log.

        The row has the SAME shape a finished episode writes (``progression``,
        ``num_steps``, ``episode_id``, ``seed``, ``anchor``) so
        ``collect_and_summarize_results`` counts it as a played episode that
        scored 0.0 -- the anchor pairing then reads "this anchor missed", which
        is true, instead of the anchor silently disappearing from the report.
        """
        env_name, task, episode_idx = ep.get("env"), ep.get("task"), ep.get("episode_idx")
        log = {
            "task": task,
            "episode_id": ep.get("id") or f"{task}#{ep.get('seed')}",
            "seed": ep.get("seed"),
            "anchor": bool(ep.get("anchor")),
            "progression": 0.0,
            "num_steps": 0,
            "episode_return": 0.0,
            "failed_candidates": [],
            "action_frequency": {},
            "aborted": True,
            "error": f"{type(error).__name__}: {error}"[:300],
        }
        if tb:
            tail = [ln for ln in tb.strip().splitlines() if ln.strip()]
            if tail:
                log["error_traceback_tail"] = tail[-1][:300]
        if env_name is None or task is None:
            # Nothing to key the row on -- still never raise: the caller is an
            # except branch and a second exception would wedge the worker pool.
            return log
        path = os.path.join(
            self.output_dir, env_name, task, f"{task}_run_{int(episode_idx or 0):02d}.json"
        )
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(log, f, indent=4)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 -- the row is nice-to-have, the score is not
            logging.error("cannot persist aborted episode row %s: %s", path, e)
        # Mirror whatever transcript the episode produced before dying, so the
        # framework's evidence archive still sees this episode.
        try:
            archive_episode_trajectory(self.output_dir, env_name, task, episode_idx)
        except Exception as e:  # noqa: BLE001 -- audited, never fatal
            logging.error("aborted-episode archive failed (%s/%s ep%d): %s",
                          env_name, task, episode_idx, e)
        return log

    def _run_sequential(self, agent_factory):
        """Run the evaluation sequentially.

        Args:
            agent_factory (AgentFactory): Factory object to create agents for evaluation.

        Returns:
            dict: Results of the evaluation aggregated by environment name.
        """
        results = defaultdict(list)
        total_episodes = len(self.episodes)
        with tqdm(total=total_episodes, desc="Evaluating Episodes", position=0, disable=True) as pbar:
            for ep in self.episodes:
                env_name, task, episode_idx = ep["env"], ep["task"], ep["episode_idx"]
                evaluator = self.env_evaluators[env_name]

                # Create agent
                chat_history_file = os.path.join(self.output_dir, env_name, task, f"{task}_run_{episode_idx:02d}_chat_history.jsonl")
                Path(chat_history_file).parent.mkdir(exist_ok=True, parents=True)
                try:
                    agent = agent_factory.create_agent(chat_history_file=chat_history_file)
                    episode_log = evaluator.run_episode(
                        task, agent, position=1, episode_idx=episode_idx,
                        episode_seed=ep["seed"], episode_id=ep["id"], is_anchor=ep["anchor"],
                    )
                except Exception as e:  # noqa: BLE001 -- one episode must not void the run
                    tb = traceback.format_exc()
                    logging.error("episode %s aborted: %s\n%s", ep.get("id"), e, tb)
                    episode_log = self._record_aborted(ep, e, tb)
                results[env_name].append(episode_log)
                pbar.update(1)
        return results

    def _run_parallel(self, agent_factory):
        """Run the evaluation in parallel using multiple workers.

        Args:
            agent_factory (AgentFactory): Factory object to create agents for evaluation.

        Returns:
            dict: Results of the evaluation aggregated by environment name.
        """
        task_queue = multiprocessing.Queue()
        results_queue = multiprocessing.Queue()

        ctx = multiprocessing.get_context("fork")

        # Initially fill the task queue with tasks up to the number of workers
        for item in self.episodes[: self.num_workers]:
            task_queue.put(item)

        # Create a master progress bar
        pbar = tqdm(total=len(self.episodes), position=0, leave=True, disable=True)

        # Assign unique positions for progress bars
        positions = list(range(self.num_workers))

        processes = []
        for idx in range(self.num_workers):
            position = positions[idx]
            p = ctx.Process(
                target=self._worker,
                args=(task_queue, results_queue, agent_factory, position),
            )
            processes.append(p)
            p.start()

        results = defaultdict(list)
        tasks_completed = 0
        tasks_queued = self.num_workers

        total_tasks = len(self.episodes)

        while tasks_completed < total_tasks:
            result = results_queue.get()
            if result.get("aborted"):
                # A crashed episode arrives as a NORMAL row (see _worker) so it is
                # counted and scored 0.0; only the log line differs.
                logging.error(
                    "episode %s aborted in %s", result.get("episode_id"), result.get("process_num")
                )
                results[result["env_name"]].append(result)
            elif "error" in result:
                logging.error(f"Error in task {result['task']} processed by {result['process_num']}: {result['error']}")
                logging.error(f"Traceback:\n{result['traceback']}")
            else:
                results[result["env_name"]].append(result)
            tasks_completed += 1

            # Update progress bar
            pbar.update(1)
            pbar.set_description(f"Last task: {result['task']}, Process: {result.get('process_num', 'N/A')}")

            # Queue another task if there are any left
            if tasks_queued < total_tasks:
                task_queue.put(self.episodes[tasks_queued])
                tasks_queued += 1

        # Signal workers to stop
        for _ in range(self.num_workers):
            task_queue.put(None)

        # Wait for all processes to finish
        for p in processes:
            p.join()

        # Close the master bar when done
        pbar.close()

        return results

    def _worker(self, task_queue, results_queue, agent_factory, position):
        """Worker process for parallel evaluation.

        Args:
            task_queue (multiprocessing.Queue): Queue containing tasks to process.
            results_queue (multiprocessing.Queue): Queue to put the results.
            agent_factory (AgentFactory): Factory object to create agents.
            position (int): Position index for the progress bar.
        """
        seed = get_unique_seed(process_num=position)
        random.seed(seed)
        np.random.seed(seed)

        process_num = multiprocessing.current_process().name
        while True:
            item = task_queue.get()
            if item is None:
                break
            try:
                env_name, task, episode_idx = item["env"], item["task"], item["episode_idx"]
                evaluator = self.env_evaluators[env_name]

                # Create a fresh agent per episode so files don't collide across parallel runs
                chat_history_file = os.path.join(self.output_dir, env_name, task, f"{task}_run_{episode_idx:02d}_chat_history.jsonl")
                Path(chat_history_file).parent.mkdir(exist_ok=True, parents=True)
                agent = agent_factory.create_agent(chat_history_file=chat_history_file)

                result = evaluator.run_episode(
                    task,
                    agent,
                    process_num=process_num,
                    position=position + 1,
                    episode_idx=episode_idx,
                    episode_seed=item["seed"],
                    episode_id=item["id"],
                    is_anchor=item["anchor"],
                )
                result["process_num"] = process_num  # Include process number in result
                result["env_name"] = env_name
                results_queue.put(result)
            except Exception as e:
                tb = traceback.format_exc()
                logging.error(f"Error in worker processing episode {item.get('id')}: {e}\n{tb}")
                # A crashed episode is still a PLAYED (lost) episode: hand back a
                # normal log row so the master aggregates it instead of dropping it
                # (a dropped episode silently shrinks the denominator, which is the
                # opposite of what the framework's score means).
                aborted_log = self._record_aborted(item, e, tb)
                aborted_log["process_num"] = process_num
                aborted_log["env_name"] = item["env"]
                results_queue.put(aborted_log)


class Evaluator:
    """Evaluator for a single environment and task.

    The Evaluator handles the execution of evaluation episodes for a specific environment and task,
    including loading in-context learning episodes and running episodes with the agent.
    """

    def __init__(self, env_name, config, original_cwd="", output_dir="."):
        """Initialize the Evaluator.

        Args:
            env_name (str): Name of the environment to evaluate.
            config (omegaconf.DictConfig): Configuration object containing evaluation settings.
            original_cwd (str, optional): Original current working directory. Defaults to "".
            output_dir (str, optional): Directory to save evaluation outputs. Defaults to ".".
        """
        self.env_name = env_name.strip()
        self.config = config
        self.output_dir = output_dir
        self.tasks = config.tasks[f"{self.env_name}_tasks"]

        self.num_episodes = config.eval.num_episodes[self.env_name]
        self.num_workers = config.eval.num_workers
        self.max_steps_per_episode = config.eval.max_steps_per_episode

        self.dataset = InContextDataset(self.config, self.env_name, original_cwd=original_cwd)

    def run_episode(self, task, agent, process_num=None, position=0, episode_idx=0,
                    episode_seed=None, episode_id=None, is_anchor=False):
        """Run a single evaluation episode.

        Args:
            task (str): Task name.
            agent (Agent): Agent to evaluate.
            process_num (str, optional): Identifier of the process running the episode. Defaults to None.
            position (int, optional): Position index for the progress bar. Defaults to 0.
            episode_idx (int, optional): Index of the episode. Defaults to 0.
            episode_seed (int, optional): Seed from the EPISODE PLAN (see
                utils/episode_seed). It replaces the historical
                ``get_unique_seed(time_ns)`` draw, which made every generation a
                different episode set (unpaired comparison). ``None`` keeps the
                legacy behaviour for direct runs without a plan.
            episode_id (str, optional): ``<task>#<seed>``; only for the log.
            is_anchor (bool): Whether this slot is a run-frozen anchor episode.

        Returns:
            dict: Log of the episode containing statistics and results.
        """
        env = make_env(self.env_name, task, self.config)

        seed = episode_seed
        if seed is None:
            seed = self.config.envs.env_kwargs.seed
        if seed is None:
            seed = get_unique_seed(process_num=process_num, episode_idx=episode_idx)
        seed = int(seed)
        random.seed(seed)
        np.random.seed(seed)
        obs, info = env.reset(seed=seed)
        episode_log = {
            "task": task,
            # Episode identity: the key report.json's question_points uses and
            # the framework's frozen anchor core matches against.
            "episode_id": episode_id or f"{task}#{seed}",
            "seed": seed,
            "anchor": bool(is_anchor),
            "action_frequency": defaultdict(int),
            # "input_tokens": 0,
            # "output_tokens": 0,
        }

        instructions = None
        if self.env_name == "babyai":
            instructions = obs["mission"]
        instruction_prompt = env.get_instruction_prompt(instructions=instructions)

        episode_return = 0.0

        max_steps_per_episode = env.max_steps if self.max_steps_per_episode is None else self.max_steps_per_episode

        # Create a unique CSV filename for this episode
        csv_filename = os.path.join(self.output_dir, self.env_name, task, f"{task}_run_{episode_idx:02d}.csv")
        Path(csv_filename).parent.mkdir(exist_ok=True, parents=True)

        # Open the CSV file and write the header
        with open(csv_filename, mode="w", newline="", encoding="utf-8") as csv_file:
            csv_writer = csv.writer(csv_file, escapechar="˘", quoting=csv.QUOTE_MINIMAL)
            csv_writer.writerow(["Step", "Action", "Reasoning", "Observation", "Reward", "Done"])

            pbar_desc = f"Task: {task}, Proc: {process_num}"
            pbar = tqdm(
                total=max_steps_per_episode,
                desc=pbar_desc,
                position=position,
                leave=False,  # Keep the progress bar after completion
                dynamic_ncols=True,
                disable=True,
            )

            action = None
            for step in range(max_steps_per_episode):
                inputs = {
                    "domain": f"balrog_{self.env_name}",
                    "instruction": instruction_prompt,
                    "observation": to_jsonable({k: v for k, v in obs.items() if k != "image"}),
                    "prev_action": action,
                }
                response_str, _ = agent.forward(inputs)
                action = env.check_action_validity(response_str)
                reasoning = ""

                episode_log["action_frequency"][action] += 1

                obs, reward, terminated, truncated, info = env.step(action)
                done = terminated or truncated

                episode_return += reward  # pyright: ignore

                # Give feedback on the action (if not valid)
                obs["text"]["long_term_context"] = (
                    f"\n\nYour previous output did not contain a valid action. Defaulted to action: {action}\n\nObservation:\n"
                    + obs["text"]["long_term_context"]
                    if (action != response_str) and (self.config.eval.feedback_on_invalid_action)
                    else obs["text"]["long_term_context"]
                )
                action = response_str
                # Write the step data to the CSV file
                csv_writer.writerow(
                    [
                        step,
                        action,
                        reasoning,
                        obs["text"]["long_term_context"],
                        reward,
                        done,
                    ]
                )

                pbar.update(1)

                if self.config.eval.save_images and obs["image"]:
                    images_dir = os.path.join(self.output_dir, self.env_name, task, f"episode_{episode_idx:02d}")
                    Path(images_dir).mkdir(exist_ok=True, parents=True)
                    image_filename = os.path.join(images_dir, f"step_{step:04d}.png")
                    image = obs["image"]
                    image.save(image_filename)

                if done:
                    logging.info(f"Episode done with reward: {episode_return}")
                    episode_log["done"] = True
                    if pbar.n < pbar.total:
                        pbar.update(pbar.total - pbar.n)
                    pbar.set_postfix_str("DONE")
                    break

            if pbar.n < pbar.total:
                pbar.update(pbar.total - pbar.n)
            if "done" not in episode_log:
                pbar.set_postfix_str("DONE")
            pbar.close()

            episode_log["episode_return"] = episode_return
            episode_log["num_steps"] = step + 1  # pyright: ignore
            episode_log["failed_candidates"] = env.failed_candidates
            episode_log.update(env.get_stats())
            episode_log["process_num"] = process_num

            # Save the episode_log to a JSON file
            json_filename = os.path.join(
                self.output_dir,
                self.env_name,
                task,
                f"{task}_run_{episode_idx:02d}.json",
            )
            Path(json_filename).parent.mkdir(exist_ok=True, parents=True)
            with open(json_filename, "w") as f:
                json.dump(episode_log, f, indent=4)

        # Archive the episode transcript into the framework's evidence contract.
        # Never fatal: a mirror failure must not lose the episode's score; the
        # framework then reports the missing archive explicitly.
        try:
            archive_episode_trajectory(self.output_dir, self.env_name, task, episode_idx)
        except Exception as e:  # noqa: BLE001 -- audited, never fatal
            logging.error(
                "episode transcript archive failed (%s/%s ep%d): %s",
                self.env_name, task, episode_idx, e,
            )

        return episode_log
