# Default reward function for Go2 walking task
# Naive reward that directly optimizes for the task fitness function

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:
    """
    Compute reward for the walking task.

    Fitness is defined as:
    1. Tracking of linear velocity commands (x axis)
    """

    # Tracking of linear velocity commands (x axis)
    lin_vel_scale = 1.0
    lin_vel_temperature = 4.0
    lin_vel_error = torch.sum(
        torch.square(env.commands[:, :1] - env.base_lin_vel[:, :1]), dim=1
    )
    lin_vel_reward = torch.exp(-lin_vel_error * lin_vel_temperature)

    total_reward = lin_vel_scale * lin_vel_reward

    reward_components = {
        "lin_vel": lin_vel_scale * lin_vel_reward,
    }

    reward_scales = {
        "lin_vel": lin_vel_scale,
    }

    return total_reward, reward_components, reward_scales
