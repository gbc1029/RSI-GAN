import csv
import json
import logging
import multiprocessing
import os
import queue
import random
import shutil
import traceback
from collections import defaultdict
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf
from tqdm import tqdm

from domains.balrog.environments import make_env
from domains.balrog.utils import (
    AGENT_EVALS_DIRNAME,
    episode_qid,
    get_unique_seed,
)
from utils.episode_seed import anchor_seeds_by_task, plan_episodes
from utils.trajectory_log import LLM_CALLS_SIDECAR_SUFFIX

logger = logging.getLogger(__name__)

# ``AGENT_EVALS_DIRNAME`` / ``episode_qid`` live in domains.balrog.utils, not
# here: the REPORT (utils.collect_and_summarize_results) has to locate each
# episode's per-call sidecar, and evaluator imports utils (not the reverse), so
# one definition there keeps the two sides from drifting. They are re-exported
# below this import block for every existing caller.


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
    # ``llm_calls`` sidecar fold-in finds it.
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


# Parallel-episode polling (P2-16). The master blocks on the result queue, so a
# worker that DIES must not be waited on forever: poll, and stop only when no
# worker is left alive. A long-but-healthy episode may be silent for many
# minutes, so liveness -- never silence alone -- is the failure signal.
_EPISODE_POLL_S = 5.0
_WORKER_JOIN_S = 30.0


def _episode_key(record, env_name=None):
    """Stable identity of one episode: plan dicts and worker rows differ.

    Plan entries carry ``env``; worker rows carry ``env_name``. ``episode_idx``
    is the slot (present on both since P2-18); ``episode_id`` is the fallback
    for any older row so a delivered episode is never double-counted.
    """
    env = env_name or record.get("env") or record.get("env_name")
    idx = record.get("episode_idx")
    if idx is None:
        eid = record.get("episode_id") or record.get("id")
        if eid is not None:
            return (env, "id", eid)
    return (env, record.get("task"), idx)


def close_env(env, env_name=None, task=None, episode_idx=None):
    """Close an episode env; never fatal.

    Long-lived parallel workers reuse the process across episodes, so an
    un-closed env leaks its renderer/pygame surfaces for the whole round. A
    close failure must not void a score, but it must SPEAK.
    """
    close = getattr(env, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as e:  # noqa: BLE001 -- audited, never fatal
        logging.warning("env.close failed (%s/%s ep%s): %s",
                        env_name, task, episode_idx, e)


# Stable marker of the rendered window. The offline delivery tripwire
# (scripts/local/check_history_delivery.py) greps for it in the archived
# prompts, so if an evolved task_agent ever stops dumping ``inputs`` the loss
# of cross-step memory becomes visible instead of silent.
HISTORY_MARKER = "Recent steps (oldest first; you DID each action below):"


def _history_block(entries):
    """Numbered lines for ``[(observation, executed_action), ...]``, oldest first."""
    lines = [HISTORY_MARKER]
    for idx, (obs_text, action) in enumerate(entries, 1):
        flat = " ".join(str(obs_text or "").split())
        lines.append(f"{idx}. saw: {flat} | did: {action}")
    return "\n".join(lines)


def _obs_state_key(obs):
    """The OBSERVABLE state of one step, as a comparable string key.

    Deliberately built from what the MODEL sees (facing + the two relative
    descriptor blocks), not from the env's internal grid: the behaviour facts
    below answer "could the agent tell these two situations apart?", which is
    exactly the information a prompt-only policy is missing. Two different
    cells that yield the same relative descriptors collapse into one key on
    purpose -- the agent cannot distinguish them either.

    The invalid-action feedback prefix is STRIPPED: it is appended to
    ``long_term_context`` after a defaulted action, so keeping it would make
    the same position look like a new state and inflate the diversity count.
    """
    if not isinstance(obs, dict):
        return ""
    text = obs.get("text") or {}
    long_term = str(text.get("long_term_context") or "")
    marker = "\n\nObservation:\n"
    if marker in long_term:
        long_term = long_term.rsplit(marker, 1)[1]
    return "{}|{}|{}".format(obs.get("direction"), long_term,
                             text.get("short_term_context") or "")


def _behaviour_summary(state_keys, actions, num_steps):
    """Episode-level behaviour facts for the evaluator (L1, 2026-11-11).

    WHY (measured on the first A0 evolution round): the loop's improvement
    verdict only reads solved rate, whose 10-episode quantum is 0.1 -- the same
    generation scored 0.2 and 0.3 on an IDENTICAL code+prompt+episode set, so
    one flipped episode is indistinguishable from an improvement. The same
    round's prompt patch moved the turn share from 80.5% to 91.4% (a clear
    regression) while solved stayed at 0.3. These facts give the evaluator a
    quantity with hundreds of samples per generation instead of ten:

      ``distinct_obs_states``  how many different observable states the episode
                               visited (2 = it never moved: spinning between two
                               headings, or pushing a wall/object forever);
      ``obs_repeat_steps``     steps whose action produced NO observable change
                               (wall/object blocks);
      ``zero_displacement``    ``distinct_obs_states <= 2`` -- the episode never
                               went anywhere.

    ``zero_displacement`` is the gameable-resistant one: inaction cannot reduce
    it (sitting still IS two states), so the only way to lower it is to move.

    CYCLE facts (docs/20 §4.2, 2026-11-17) -- the fix for a MEASURED sign error.
    ``obs_repeat_steps`` counts INFORMATION change, not progress, and the state
    key contains the facing: a turning step always changes it, so a spinning
    agent scores ZERO on it (A0 gen8: 5.4% while turning in 92% of its steps)
    while a healthy agent that keeps trying and failing scores high (A1: 36-53%).
    The pair-based count below fixes the direction by asking a different
    question -- "have I already tried exactly this action from exactly this
    state?" -- which a 4-step rotation cycle hits on every lap (A0 gen8: 90.9%)
    and a genuine attempt never does (A1 gen0: 46.8%):

      ``revisit_steps``             steps whose (state, action) pair already
                                    occurred earlier in THIS episode;
      ``no_progress_action_counts`` the steps whose action changed nothing,
                                    keyed by the EXECUTED action (the framework
                                    splits these into blocked-forward vs
                                    failed-interaction via the domain's
                                    declared ``action_classes``).

    Both families are ABSENT (never zero) when the action trace is missing or
    length-mismatched, so a reader can tell "no data" from "measured 0" -- the
    same rule the aborted-episode row follows.
    """
    keys = [k for k in (state_keys or []) if k is not None]
    acts = list(actions or [])
    distinct = len(set(keys))
    repeats = sum(1 for i in range(1, len(keys)) if keys[i] == keys[i - 1])
    out = {
        "distinct_obs_states": distinct,
        "obs_repeat_steps": repeats,
        "zero_displacement": bool(distinct <= 2),
    }
    if acts and len(acts) == len(keys):
        seen = set()
        revisit = 0
        no_progress = {}
        for i, (k, a) in enumerate(zip(keys, acts)):
            if (k, a) in seen:
                revisit += 1
            seen.add((k, a))
            if i and k == keys[i - 1]:
                key = str(a)
                no_progress[key] = no_progress.get(key, 0) + 1
        out["revisit_steps"] = revisit
        out["revisit_share"] = revisit / len(acts)
        out["no_progress_action_counts"] = no_progress
    # A step count that disagrees with the key trace means the loop exited
    # before recording (defensive; the two are built in the same loop).
    if num_steps is not None and int(num_steps) != len(keys):
        out["obs_states_traced"] = len(keys)
    return out


def render_history(entries, max_chars=0):
    """Render the rolling window sent to the task child (upstream semantics).

    Upstream kept the last ``agent.max_text_history`` MESSAGES in the LLM
    conversation (one step = observation + action = 2 messages). Our step child
    has no conversation to keep, so the same window is re-rendered into the
    per-step payload. Plain text, one stable marker line, and NO instruction of
    its own: guidance belongs to the design prompt, which the roles may rewrite.

    ``max_chars`` drops from the OLDEST end first (the most recent step matters
    most); a single oversized block is hard-truncated with an explicit tail.
    """
    kept = list(entries or [])
    if not kept:
        return ""
    while max_chars and len(kept) > 1 and len(_history_block(kept)) > max_chars:
        kept.pop(0)
    block = _history_block(kept)
    if max_chars and len(block) > max_chars:
        block = block[:max_chars] + " ...[history truncated]"
    return block

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
            "episode_idx": episode_idx,
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

    def _absorb_result(self, result, results, delivered):
        """Fold one worker row into the aggregate; True when it counted.

        Every row a worker hands back is an EPISODE ROW -- including a crash
        (``_worker`` converts it into an aborted row). An aborted or errored row
        is appended like any other so the denominator never shrinks; only the
        log level differs.
        """
        key = _episode_key(result)
        if key in delivered:
            return False
        delivered.add(key)
        env_name = result.get("env_name") or (key[0] if key[0] else "unknown")
        if result.get("aborted"):
            logging.error("episode %s aborted in %s (%s)", result.get("episode_id"),
                          result.get("process_num"), result.get("error"))
        elif "error" in result:
            logging.error("error in task %s (process %s): %s",
                          result.get("task"), result.get("process_num"), result.get("error"))
        results[env_name].append(result)
        return True

    def _run_parallel(self, agent_factory):
        """Run the evaluation with ``num_workers`` parallel EPISODE workers.

        Three coordination rules, each fixing a way the old hand-off loop could
        lose or hang a round (P2-16):

        * **No deadlock.** The queue is preloaded with every episode plus one
          ``None`` sentinel per worker, so the work ahead of a sentinel still
          reaches a LIVE worker when another one dies. The old loop refilled the
          queue from the master and then blocked on ``results_queue.get()`` with
          no timeout, so a dead worker stalled the round forever.
        * **No silent shrinkage.** An episode that never came back is recorded
          as ABORTED in the master (a played and lost episode) instead of
          vanishing from the report; an episode whose JSON is already on disk is
          left alone (it really did finish).
        * **No orphans.** Workers are joined -- and terminated if they refuse to
          exit -- in ``finally``, so an exception in the master can no longer
          leave step-child-spawning processes behind.
        """
        ctx = multiprocessing.get_context("fork")
        task_queue = ctx.Queue()
        results_queue = ctx.Queue()

        total_tasks = len(self.episodes)
        workers = max(1, min(int(self.num_workers or 1), max(1, total_tasks)))

        for item in self.episodes:
            task_queue.put(item)
        for _ in range(workers):
            task_queue.put(None)

        pbar = tqdm(total=total_tasks, position=0, leave=True, disable=True)
        processes = []
        results = defaultdict(list)
        delivered = set()
        try:
            for idx in range(workers):
                p = ctx.Process(
                    target=self._worker,
                    args=(task_queue, results_queue, agent_factory, idx),
                )
                processes.append(p)
                p.start()

            while len(delivered) < total_tasks:
                try:
                    result = results_queue.get(timeout=_EPISODE_POLL_S)
                except queue.Empty:
                    if not any(p.is_alive() for p in processes):
                        # Drain whatever is still buffered, then stop: every
                        # worker is gone, so nothing more can arrive.
                        while True:
                            try:
                                pending = results_queue.get_nowait()
                            except queue.Empty:
                                break
                            if self._absorb_result(pending, results, delivered):
                                pbar.update(1)
                        break
                    continue
                if self._absorb_result(result, results, delivered):
                    pbar.update(1)
                    pbar.set_description(
                        f"Last task: {result.get('task')}, "
                        f"Process: {result.get('process_num', 'N/A')}"
                    )
        finally:
            for p in processes:
                p.join(timeout=_WORKER_JOIN_S)
            for p in processes:
                if p.is_alive():
                    logging.error("balrog worker pid %s did not exit; terminating", p.pid)
                    p.terminate()
                    p.join(timeout=5.0)
            pbar.close()

        missing = [ep for ep in self.episodes if _episode_key(ep) not in delivered]
        if missing:
            logging.error(
                "balrog: %d/%d episode(s) never came back from a worker -- recording "
                "them as aborted instead of shrinking the denominator",
                len(missing), total_tasks,
            )
            for ep in missing:
                path = os.path.join(
                    self.output_dir, ep["env"], ep["task"],
                    f"{ep['task']}_run_{int(ep['episode_idx']):02d}.json",
                )
                if os.path.exists(path):
                    # The episode DID finish on disk; only its result row was
                    # lost. Never overwrite real evidence with a 0.0 row.
                    logging.error("episode %s finished on disk but lost its result row; "
                                  "keeping the file", ep.get("id"))
                    continue
                log = self._record_aborted(
                    ep, RuntimeError("episode worker died before delivering a result"))
                # Append it as well: ``run()`` counts aborted rows to decide
                # whether the whole round is an infrastructure failure.
                results[ep["env"]].append(log)
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

        # CROSS-STEP MEMORY (方案 a, 2026-11-10). Upstream kept the last
        # ``agent.max_text_history`` MESSAGES in the LLM conversation; our step
        # child is a fresh process with no conversation, so the window is
        # re-rendered into each step's payload instead. Same name, same unit
        # (messages; one step = 2), same default (16 = 8 steps); 0 = off, which
        # keeps the prompt byte-identical to the pre-history behaviour.
        agent_cfg = config.get("agent") or {}
        try:
            self.history_messages = max(0, int(agent_cfg.get("max_text_history") or 0))
        except (TypeError, ValueError):
            self.history_messages = 0
        try:
            self.history_max_chars = max(0, int(agent_cfg.get("history_max_chars") or 0))
        except (TypeError, ValueError):
            self.history_max_chars = 0
        self.history_steps = self.history_messages // 2

        # NOTE: the upstream in-context-learning dataset was REMOVED (P2-14).
        # `InContextDataset` was constructed here but never used by
        # `run_episode`, its `records/` directory does not exist in this repo,
        # and enabling it divided by zero (`i % len([])`). Dead capability that
        # looks configurable is worse than none: `eval.icl_*` keys are gone from
        # `domains/balrog/config/config.yaml` too.

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
        # The env is OWNED here and closed in ``finally``: a parallel worker
        # reuses its process for many episodes, so an un-closed env leaks a
        # renderer per episode (and the sandboxed children behind it). The body
        # lives in ``_play_episode`` so the close is unconditional, including on
        # the exception path (P2-16).
        env = make_env(self.env_name, task, self.config)
        try:
            return self._play_episode(
                env, task, agent, process_num=process_num, position=position,
                episode_idx=episode_idx, episode_seed=episode_seed,
                episode_id=episode_id, is_anchor=is_anchor,
            )
        finally:
            close_env(env, self.env_name, task, episode_idx)

    def _play_episode(self, env, task, agent, process_num=None, position=0,
                      episode_idx=0, episode_seed=None, episode_id=None,
                      is_anchor=False):
        """One episode on an ALREADY-OPEN env (``run_episode`` owns closing)."""
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
            # Slot index: the REPORT needs it to locate this episode's per-call
            # LLM sidecar (agent_evals/chat_history_<qid>.llm_calls.jsonl).
            "episode_idx": episode_idx,
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

            # Rolling (observation, executed action) window, oldest first. Lives
            # in this function scope, so it CANNOT leak across episodes (a new
            # episode starts with an empty window by construction).
            history = []
            # Observable-state trace (L1 behaviour facts): one key per step, the
            # observation the agent SAW at that step. See _behaviour_summary.
            state_keys = []
            # Executed-action trace, index-aligned with ``state_keys``: the pair
            # (what it decided FROM, what it DID) is the unit the cycle detector
            # in _behaviour_summary counts (docs/20 §4.2).
            executed_actions = []
            action = None
            for step in range(max_steps_per_episode):
                inputs = {
                    "domain": f"balrog_{self.env_name}",
                    "instruction": instruction_prompt,
                    "observation": to_jsonable({k: v for k, v in obs.items() if k != "image"}),
                    "prev_action": action,
                }
                # What the model is ABOUT to see at this step, captured verbatim
                # (including the invalid-action feedback prefix) so the window
                # replays the episode as the agent experienced it.
                seen_text = ((inputs["observation"].get("text") or {})
                             .get("long_term_context", ""))
                # The state the agent is deciding FROM (feedback prefix already
                # merged into inputs["observation"], so _obs_state_key strips it).
                state_keys.append(_obs_state_key(inputs["observation"]))
                if self.history_steps > 0:
                    block = render_history(history, self.history_max_chars)
                    if block:
                        inputs["history"] = block
                response_str, _ = agent.forward(inputs)
                # ``executed`` is what the ENV will actually do (an unparseable
                # reply is replaced by ``default_action`` and counted in
                # ``failed_candidates``). The raw reply is kept separately: the
                # old code reassigned ``action = response_str`` after the step,
                # so the next turn advertised "your previous action was <prose>"
                # while the env had executed the default action, and the CSV
                # Action column disagreed with ``action_frequency``.
                executed = env.check_action_validity(response_str)

                episode_log["action_frequency"][executed] += 1
                executed_actions.append(executed)

                obs, reward, terminated, truncated, info = env.step(executed)
                done = terminated or truncated

                episode_return += reward  # pyright: ignore

                # Give feedback on the action (if not valid)
                obs["text"]["long_term_context"] = (
                    f"\n\nYour previous output did not contain a valid action. Defaulted to action: {executed}\n\nObservation:\n"
                    + obs["text"]["long_term_context"]
                    if (executed != response_str) and (self.config.eval.feedback_on_invalid_action)
                    else obs["text"]["long_term_context"]
                )
                # ``prev_action`` = the action the ENVIRONMENT EXECUTED.
                action = executed
                # Feed the window with what this step SAW and what it DID (the
                # executed action, never the raw reply -- same rule as
                # ``prev_action``). Reasoning is deliberately not carried: the
                # upstream ``remember_cot`` default is not part of this port and
                # it would triple the prompt growth.
                if self.history_steps > 0:
                    history.append((seen_text, executed))
                    if len(history) > self.history_steps:
                        del history[0]
                # Write the step data to the CSV file. Column semantics are now
                # honest: Action = executed, Reasoning = the model's raw reply
                # (the only record of an illegal attempt, since a defaulted
                # action overwrites the Action column with ``default_action``).
                csv_writer.writerow(
                    [
                        step,
                        executed,
                        response_str,
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
            # Memory-window facts (方案 a): the DECLARED depth and how much of it
            # this episode actually used. Reported per episode so report.json (and
            # the offline tripwire) can tell "history was off" from "history was
            # on but the prompt never carried it".
            episode_log["history_steps"] = self.history_steps
            episode_log["history_used"] = len(history)
            # L1 behaviour facts (see _behaviour_summary): per-episode, so the
            # report can aggregate them and the evaluator can quote them.
            episode_log.update(  # pyright: ignore
                _behaviour_summary(state_keys, executed_actions, step + 1))
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
