from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:

    # 1. Reward tracking of angular velocity command (yaw)
    ang_vel_temperature = 0.25
    ang_vel_scale = 0.2
    ang_vel_error = torch.square(env.commands[:, 2] - env.base_ang_vel[:, 2])
    ang_vel_reward = torch.exp(-ang_vel_error * ang_vel_temperature)

    # 2. Penalize base height away from target
    base_height_target = 0.1
    base_height_scale = -0.1
    base_height_penalty = torch.square(env.base_pos[:, 2] - base_height_target)

    total_reward = (
        ang_vel_scale * ang_vel_reward + base_height_scale * base_height_penalty
    )

    reward_components = {
        "ang_vel_reward": ang_vel_scale * ang_vel_reward,
        "base_height_penalty": base_height_scale * base_height_penalty,
    }
    reward_scales = {
        "ang_vel_reward": ang_vel_scale,
        "base_height_penalty": base_height_scale,
    }
    return total_reward, reward_components, reward_scales
