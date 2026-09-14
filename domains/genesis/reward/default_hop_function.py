# Default reward function for Go2 hopping task
# Naive reward that directly optimizes for the task fitness function

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:
    """
    Compute reward for the hopping task.

    Fitness is defined as the current height of the robot's base.
    The robot should maximize its vertical position.
    """

    # Reward for height - the robot should jump as high as possible
    height = env.base_pos[:, 2]
    total_reward = height

    reward_components = {
        "height": height,
    }

    reward_scales = {
        "height": 1.0,
    }

    return total_reward, reward_components, reward_scales
