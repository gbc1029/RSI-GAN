import gymnasium as gym


# Every environment this wrapper fronts is adapted to the GYMNASIUM API by its
# own ``make_*_env``: ``nle`` / ``minihack`` / ``textworld`` / ``crafter`` /
# ``babaisai`` build a legacy gym-0.23 env (``baba`` is gym-0.23 only) and wrap
# it in ``wrappers.gym_compatibility.GymV21CompatibilityV0``; ``babyai`` is
# Gymnasium native (minigrid 2.x). The wrapper still NORMALIZES the tuple shape
# defensively -- a missed shim used to surface three frames away from the cause
# as a bare "not enough values to unpack" (P2-17).
_KNOWN_ENVS = ("nle", "minihack", "babyai", "textworld", "babaisai", "crafter")


def _as_gymnasium_reset(env_name, result):
    """Return ``(obs, info)`` from a 1-tuple (legacy gym) or 2-tuple reset."""
    if not isinstance(result, tuple):
        raise TypeError(
            f"{env_name} reset() returned {type(result).__name__}; expected the "
            f"Gymnasium (obs, info) tuple"
        )
    if len(result) == 2:
        return result
    if len(result) == 1:
        return result[0], {}
    raise TypeError(
        f"{env_name} reset() returned {len(result)} values; expected (obs, info). "
        f"The env is not adapted to the Gymnasium API -- wrap it in "
        f"GymV21CompatibilityV0 inside its make_*_env."
    )


def _as_gymnasium_step(env_name, result):
    """Return the 5-tuple step from a 5- or 4-tuple (legacy gym) result."""
    if not isinstance(result, tuple):
        raise TypeError(
            f"{env_name} step() returned {type(result).__name__}; expected the "
            f"Gymnasium (obs, reward, terminated, truncated, info) tuple"
        )
    if len(result) == 5:
        return result
    if len(result) == 4:
        obs, reward, done, info = result
        return obs, reward, bool(done), False, info
    raise TypeError(
        f"{env_name} step() returned {len(result)} values; expected 5 "
        f"(obs, reward, terminated, truncated, info). The env is not adapted to "
        f"the Gymnasium API -- wrap it in GymV21CompatibilityV0 inside its "
        f"make_*_env."
    )


class EnvWrapper(gym.Wrapper):
    """
    A wrapper class for gym environments to standardize interactions across different environments.
    It provides additional functionalities, such as handling specific observation processing,
    managing action validity, retrieving instruction prompts, and tracking failed action candidates.
    """

    def __init__(self, env, env_name, task_name):
        super().__init__(env)
        self.env_name = env_name
        self.task_name = task_name
        self.failed_candidates = []

    @property
    def max_steps(self):
        return self.env.max_steps

    def reset(self, **kwargs):
        obs, info = _as_gymnasium_reset(self.env_name, self.env.reset(**kwargs))
        return self._process_observation(obs), info

    def step(self, action):
        obs, reward, terminated, truncated, info = _as_gymnasium_step(
            self.env_name, self.env.step(action)
        )
        processed_obs = self._process_observation(obs)
        return processed_obs, reward, terminated, truncated, info

    def _process_observation(self, obs):
        """Validate the observation contract every consumer relies on.

        The previous implementation branched over the environment names and
        returned ``obs`` UNCHANGED in all six arms (dead code), so a broken env
        surfaced far away as ``KeyError: 'text'`` inside the episode loop. The
        contract is a single, checkable thing -- a ``text.long_term_context``
        STRING, the field the agent prompt and the CSV evidence are built from
        -- so it is asserted ONCE, naming the env that broke it (P2-17).
        """
        if self.env_name not in _KNOWN_ENVS:
            raise ValueError(f"Unknown environment: {self.env_name}")
        if not isinstance(obs, dict) or "text" not in obs:
            raise TypeError(
                f"{self.env_name}/{self.task_name}: observation has no 'text' "
                f"field (got {sorted(obs) if isinstance(obs, dict) else type(obs).__name__})"
            )
        text = obs["text"]
        if not isinstance(text, dict) or not isinstance(text.get("long_term_context"), str):
            raise TypeError(
                f"{self.env_name}/{self.task_name}: observation['text'] must carry a "
                f"string 'long_term_context' (got {type(text).__name__})"
            )
        return obs

    @property
    def actions(self):
        # This property should return the list of available actions
        return self.env.actions if hasattr(self.env, "actions") else list(range(len(self.env.action_space)))

    def get_text_action(self, action):
        return self.env.get_text_action(action)

    def get_instruction_prompt(self, instructions=None):
        if self.env_name == "nle":
            from domains.balrog.environments.nle import get_instruction_prompt

            return get_instruction_prompt()
        elif self.env_name == "minihack":
            from domains.balrog.environments.minihack import get_instruction_prompt

            return get_instruction_prompt(self.env, self.task_name)
        elif self.env_name == "babyai":
            from domains.balrog.environments.babyai_text import get_instruction_prompt

            return get_instruction_prompt(self.env, mission=instructions)
        elif self.env_name == "textworld":
            from domains.balrog.environments.textworld import get_instruction_prompt

            return get_instruction_prompt(self.env, self.task_name)
        elif self.env_name == "babaisai":
            from domains.balrog.environments.babaisai import get_instruction_prompt

            return get_instruction_prompt(self.env, self.task_name)
        elif self.env_name == "crafter":
            from domains.balrog.environments.crafter import get_instruction_prompt

            return get_instruction_prompt(self.task_name)
        else:
            # NOTE: this branch used to read ``self.env_namee`` -- so a wiring
            # mistake raised AttributeError instead of the intended ValueError
            # (P2-17).
            raise ValueError(f"Unknown environment: {self.env_name}")

    def check_action_validity(self, candidate_action):
        space = getattr(self.env, "language_action_space", None)
        default_action = getattr(self.env, "default_action", None)
        if space is None or default_action is None:
            # The action vocabulary IS the domain's output contract
            # (domains.yaml output_contract.kind=action): a missing one is a
            # wiring bug, reported by name instead of an AttributeError.
            raise AttributeError(
                f"{self.env_name}/{self.task_name}: the wrapped env exposes no "
                f"language_action_space / default_action, so the action contract "
                f"cannot be enforced"
            )
        if candidate_action in space:
            return candidate_action
        candidate_str = (
            candidate_action if type(candidate_action) is str else repr(candidate_action)
        )
        self.failed_candidates.append(candidate_str)
        return default_action

    def get_stats(self):
        return self.env.get_stats()
