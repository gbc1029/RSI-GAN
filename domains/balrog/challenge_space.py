"""L1 question space for balrog challenges -- generator sampling (docs/21 §4).

THE AUTHORITY MODEL
-------------------
The evaluator (the author) may NOT write a question. It names a
``(defect_class, family)`` pair; this module materializes the actual instance
from the environment's OWN generator, with a framework-chosen seed. That is what
``L1`` means, and it is why no revealed information can leak into a question:
the author never sees a level before the child plays it, and the level is drawn
from the same distribution the benchmark itself draws from.

TWO INSTANCES PER CHALLENGE (decision D2, scheme b)
---------------------------------------------------
* ``instances.train`` -- joins the TRAINING batch (it takes a rotating slot), so
  the planner sees it and must deal with it;
* ``instances.eval``  -- joins the evaluation batch (``challenge_val``), which the
  planner never sees, so "repaired" can be told apart from "repaired on the one
  episode I was shown".
They are the same FAMILY (same task id), so a cross-instance ``progression``
comparison is meaningful -- but only within a comparable difficulty band, which
is why the band is enforced here and re-checked by the adjudicator.

DIFFICULTY IS ``optimal_steps``, NOT ``max_steps``
--------------------------------------------------
Measured (docs/21 §11): ``max_steps`` is a FAMILY CONSTANT (goto/pickup = 64,
open/putnext/seq = 128) and ``time_limit`` is None, so neither encodes
difficulty. ``optimal_steps`` is the length of ``BabyAIBot``'s rollout (0.00-0.04
s per instance), which is what the band in ``domains.yaml`` pins.

WHY THE FINGERPRINT IS TAKEN AT RESET
-------------------------------------
An instance's identity is the level AT RESET. Fingerprinting after a rollout
makes every solved episode end with the agent standing on the goal, so distinct
instances collapse into one hash (measured: pickup looked like a single layout
over 24 seeds; the truth is 23). The mission TEXT is not an identity either
(measured: two different ``open`` levels both say "open the door"), so the
identity is ``sha1(grid.encode())`` -- the grid bytes, agent position included.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from omegaconf import OmegaConf

from utils.episode_seed import derive_episode_seed

#: seed-derivation kind for challenge instances. A separate kind from
#: ``ANCHOR_KIND`` / ``ROTATE_KIND`` / ``EVAL_KIND`` so a challenge seed can never
#: coincide with a planned episode seed (the A-layer intersection check then has
#: nothing to catch -- disjointness is by construction, not by luck).
CHALLENGE_KIND = "challenge-ep"

#: how many seeds a single ``materialize`` may inspect before giving up
DEFAULT_MAX_ATTEMPTS = 32

#: rollout cap (an instance that is not solved within this is not admitted)
OPTIMAL_STEP_CAP = 256

_CONFIG_REL = os.path.join("domains", "balrog", "config", "config.yaml")


class ChallengeSpaceError(RuntimeError):
    """An instance could not be materialized / admitted (A or B layer)."""


# ---------------------------------------------------------------------------
# domain config (the domain's OWN config.yaml -- no framework import)
# ---------------------------------------------------------------------------
def declared_num_dists(config_path: Optional[str] = None) -> Optional[int]:
    """``envs.babyai_kwargs.num_dists`` from the domain's own config.yaml.

    Exists so a difficulty band cannot silently be measured under a different
    generator configuration than the one the run will use: the band is only
    meaningful if the layout distribution is the same. ``None`` when the file or
    the key is absent (a caller may then skip the drift check).
    """
    path = config_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        _CONFIG_REL,
    )
    try:
        cfg = OmegaConf.load(path)
    except Exception:  # noqa: BLE001 -- absence is "unknown", never a crash
        return None
    try:
        value = cfg.envs.babyai_kwargs.num_dists
    except Exception:  # noqa: BLE001
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
def fingerprint(env_wrapped: Any) -> str:
    """Layout identity AT RESET: sha1 of the encoded grid (agent included)."""
    grid = env_wrapped.env.unwrapped.grid.encode()
    raw = bytes(grid.tobytes()) if hasattr(grid, "tobytes") else repr(grid.tolist()).encode()
    return hashlib.sha1(raw).hexdigest()


def _make_env(family: str, num_dists: int):
    from domains.balrog.environments import make_env

    cfg = OmegaConf.create({"envs": {"babyai_kwargs": {"num_dists": int(num_dists)}}})
    return make_env("babyai", family, cfg)


def optimal_steps(env_wrapped: Any, seed: int, cap: int = OPTIMAL_STEP_CAP) -> Tuple[int, bool]:
    """Replay the demonstrably optimal policy; return ``(steps, solved)``.

    ``BabyAIBot`` only exposes ``replan()`` (the NEXT action), so the optimal
    length is a rollout, not a lookup. The wrapped env's ``step`` takes the
    action LABEL (string), index-aligned with ``language_action_space``.
    """
    env_wrapped.reset(seed=int(seed))
    space = list(env_wrapped.env.language_action_space)
    from minigrid.utils.baby_ai_bot import BabyAIBot

    bot = BabyAIBot(env_wrapped.env.unwrapped)
    steps, solved = 0, False
    while steps < int(cap):
        act = bot.replan()
        if act is None:
            break
        _obs, reward, terminated, truncated, _ = env_wrapped.step(space[int(act)])
        steps += 1
        if terminated or truncated:
            solved = bool(reward > 0)
            break
    return steps, solved


def validate_episode(instance: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    """B layer: solvable AND inside the family's declared difficulty band."""
    steps = instance.get("optimal_steps")
    if not isinstance(steps, int) or isinstance(steps, bool):
        raise ChallengeSpaceError(
            f"instance {instance.get('episode_id')!r} carries no optimal_steps")
    if not instance.get("solved"):
        raise ChallengeSpaceError(
            f"instance {instance.get('episode_id')!r} is not solvable by the optimal "
            f"policy within {OPTIMAL_STEP_CAP} steps -- refusing an unsolvable question")
    floor = int(params.get("min_optimal_steps", 4))
    lo = max(floor, int(params.get("optimal_steps_p10", floor)))
    hi = int(params.get("optimal_steps_p90", lo))
    if steps < lo:
        raise ChallengeSpaceError(
            f"instance {instance.get('episode_id')!r} is too easy: optimal_steps={steps} "
            f"< band low={lo} (min_optimal_steps={floor}, p10={params.get('optimal_steps_p10')})")
    if steps > hi:
        raise ChallengeSpaceError(
            f"instance {instance.get('episode_id')!r} is out of band: optimal_steps={steps} "
            f"> band high={hi} (p90={params.get('optimal_steps_p90')})")
    if not instance.get("grid_sha1"):
        raise ChallengeSpaceError(
            f"instance {instance.get('episode_id')!r} carries no layout fingerprint")


# ---------------------------------------------------------------------------
# materialization
# ---------------------------------------------------------------------------
def _seed_candidates(base_seed: int, family: str, count: int) -> List[int]:
    return [derive_episode_seed(int(base_seed), CHALLENGE_KIND, family, idx)
            for idx in range(max(0, int(count)))]


def materialize(family: str, *, base_seed: int, params: Mapping[str, Any],
                exclude_seeds: Iterable[int] = (),
                exclude_fingerprints: Iterable[str] = (),
                source_id: Optional[str] = None,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Dict[str, Any]:
    """Draw ONE same-family instance PAIR (``train`` + ``eval``).

    Deterministic in ``base_seed``: re-running with the same inputs yields the
    same pair, so a materialization is reproducible evidence rather than a
    lottery. Every rejection is returned in ``rejected`` (an audit trail: "the
    band refused 6 of 32 seeds" is a fact an offline reader needs).

    Raises :class:`ChallengeSpaceError` when no admissible pair exists -- the
    caller turns that into ``challenge_rejected`` and does NOT spend the budget
    (A/B layer failure, not a truth failure).
    """
    num_dists = params.get("num_dists")
    if num_dists is None:
        num_dists = declared_num_dists()
    if num_dists is None:
        raise ChallengeSpaceError(
            "challenge_space.params.num_dists is undeclared and the domain config "
            "could not be read -- the difficulty band would be measured under a "
            "different generator than the run uses")
    num_dists = int(num_dists)
    declared = declared_num_dists()
    if declared is not None and declared != num_dists:
        raise ChallengeSpaceError(
            f"challenge_space.params.num_dists={num_dists} disagrees with the domain "
            f"config's envs.babyai_kwargs.num_dists={declared} -- the declared "
            f"difficulty band was measured under a different generator")

    used_seeds: Set[int] = {int(s) for s in (exclude_seeds or ())}
    used_prints: Set[str] = {str(f) for f in (exclude_fingerprints or ())}
    rejected: List[Dict[str, Any]] = []
    admitted: List[Dict[str, Any]] = []

    env_wrapped = _make_env(family, num_dists)
    source = None

    def describe(seed: int) -> Dict[str, Any]:
        env_wrapped.reset(seed=seed)
        print_sha = fingerprint(env_wrapped)
        steps, solved = optimal_steps(env_wrapped, seed)
        return {
            "family": family, "seed": seed, "episode_id": f"{family}#{seed}",
            "grid_sha1": print_sha, "optimal_steps": steps,
            "solved": solved, "num_dists": num_dists,
        }

    try:
        if source_id is not None:
            from utils.episode_seed import split_episode_id
            source_family, source_seed = split_episode_id(source_id)
            if source_family != family:
                raise ChallengeSpaceError("source episode belongs to a different family")
            source = describe(source_seed)
            validate_episode(source, params)
            used_seeds.add(source_seed)
            used_prints.add(source["grid_sha1"])
        for seed in _seed_candidates(base_seed, family, max_attempts):
            if seed in used_seeds:
                rejected.append({"seed": seed, "reason": "seed_already_planned"})
                continue
            try:
                instance = describe(seed)
            except Exception as exc:  # noqa: BLE001 -- a generator failure is a refusal
                rejected.append({"seed": seed, "reason": f"generator_error: {exc}"})
                continue
            print_sha = instance["grid_sha1"]
            if print_sha in used_prints:
                rejected.append({"seed": seed, "reason": "duplicate_layout",
                                 "grid_sha1": print_sha})
                continue
            try:
                validate_episode(instance, params)
            except ChallengeSpaceError as exc:
                rejected.append({"seed": seed, "reason": str(exc)})
                continue
            used_seeds.add(int(seed))
            used_prints.add(print_sha)
            admitted.append(instance)
            if len(admitted) >= 2:
                break
    finally:
        try:
            env_wrapped.close()
        except Exception:  # noqa: BLE001 -- closing a probe env is never fatal
            pass

    if len(admitted) < 2:
        raise ChallengeSpaceError(
            f"could not materialize a same-family pair for {family!r} in "
            f"{int(max_attempts)} attempts ({len(admitted)} admitted); rejections: "
            f"{[r.get('reason') for r in rejected][:4]}")
    return {
        "family": family,
        "train": admitted[0],
        "eval": admitted[1],
        **({"source": source} if source is not None else {}),
        "band": {"low": max(int(params.get("min_optimal_steps", 4)),
                            int(params.get("optimal_steps_p10", 4))),
                 "high": int(params.get("optimal_steps_p90", 0))},
        "attempts": len(rejected) + len(admitted),
        "rejected": rejected,
    }


__all__ = [
    "CHALLENGE_KIND",
    "ChallengeSpaceError",
    "declared_num_dists",
    "fingerprint",
    "materialize",
    "optimal_steps",
    "validate_episode",
]
