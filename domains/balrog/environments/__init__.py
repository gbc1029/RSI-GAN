# Here we should have an environment manager function that can be used to instantiate
# environments with the correct wrappers.
#
# NOTE (P2-17): ``gym`` (0.23) is still imported HERE for the custom ``Strings``
# space used by the NLE/textworld observation schemas. It is a module-level
# legacy dependency, kept deliberately: ``baba`` and ``minihack`` only expose
# gym-0.23 APIs, so migrating ``Strings`` to ``gymnasium.spaces`` would not
# remove the dependency, it would only split the two lineages in a file whose
# whole job is to bridge them. Every env handed to ``EnvWrapper`` is adapted to
# the Gymnasium step/reset API in its own ``make_*_env`` instead.
from gym import spaces

from domains.balrog.environments.env_wrapper import EnvWrapper


def make_env(env_name, task, config, render_mode=None):
    """Create an environment instance with the appropriate wrapper based on the environment name.

    Args:
        env_name (str): The name of the environment to create.
        task (str): The specific task within the environment.
        config (dict): Configuration settings for the environment.
        render_mode (str, optional): Rendering mode for the environment. Defaults to None.

    Returns:
        EnvWrapper: A wrapped environment instance.

    Raises:
        ValueError: If the environment name is not recognized.
    """
    if env_name == "nle":
        from domains.balrog.environments.nle.nle_env import make_nle_env

        base_env = make_nle_env(env_name, task, config, render_mode=render_mode)
    elif env_name == "minihack":
        from domains.balrog.environments.minihack.minihack_env import make_minihack_env

        base_env = make_minihack_env(env_name, task, config, render_mode=render_mode)
    elif env_name == "babyai":
        from domains.balrog.environments.babyai_text.babyai_env import make_babyai_env

        base_env = make_babyai_env(env_name, task, config, render_mode=render_mode)
    elif env_name == "crafter":
        from domains.balrog.environments.crafter.crafter_env import make_crafter_env

        base_env = make_crafter_env(env_name, task, config, render_mode=render_mode)
    elif env_name == "textworld":
        from domains.balrog.environments.textworld.textworld_env import make_textworld_env

        base_env = make_textworld_env(env_name, task, config, render_mode=render_mode)
    elif env_name == "babaisai":
        from domains.balrog.environments.babaisai.babaisai_env import make_babaisai_env

        base_env = make_babaisai_env(env_name, task, config, render_mode=render_mode)
    else:
        raise ValueError(f"Unknown environment: {env_name}")
    return EnvWrapper(base_env, env_name, task)


class Strings(spaces.Space):
    """A custom Gym space for managing discrete string-based actions."""

    def __init__(self, values, seed=None):
        super().__init__((len(values),), str, seed)
        self._dict = {value: i for i, value in enumerate(values)}
        self._values = values

    def sample(self):
        return self.np_random.choice(self._values)

    def map(self, action):
        return self._dict[action]

    def contains(self, value):
        return value in self._dict

    def __iter__(self):
        return self._values.__iter__()
