# taken from ./outputs/generate_20260107_131422_514432/gen_89/genesis_go2walking_eval/reward_function_04.py
# average fitness of 0.7942 on go2walking

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:

    # 1. Primary reward: Track forward velocity command
    lin_vel_x_temperature = 1.5
    lin_vel_x_scale = 1.0
    lin_vel_x_error = torch.square(env.commands[:, 0] - env.base_lin_vel[:, 0])
    lin_vel_x_reward = torch.exp(-lin_vel_x_error * lin_vel_x_temperature)

    # 2. Track angular velocity command (yaw)
    ang_vel_z_temperature = 0.5
    ang_vel_z_scale = 0.3
    ang_vel_z_error = torch.square(env.commands[:, 2] - env.base_ang_vel[:, 2])
    ang_vel_z_reward = torch.exp(-ang_vel_z_error * ang_vel_z_temperature)

    # 3. Maintain proper base height (0.32m for Go2)
    base_height_target = 0.32
    base_height_scale = -0.3
    base_height_penalty = torch.square(env.base_pos[:, 2] - base_height_target)

    # 4. Maintain upright orientation using projected gravity
    orientation_scale = -0.2
    orientation_penalty = torch.sum(torch.square(env.projected_gravity[:, :2]), dim=1)

    # 5. Penalize lateral velocity (y-direction)
    lateral_vel_scale = -0.1
    lateral_vel_penalty = torch.square(env.base_lin_vel[:, 1])

    # 6. Action smoothness penalty
    action_smoothness_scale = -0.01
    action_smoothness_penalty = torch.sum(torch.square(env.actions - env.last_actions), dim=1)

    # 7. Joint velocity smoothness penalty
    dof_vel_smoothness_scale = -0.001
    dof_vel_smoothness_penalty = torch.sum(torch.square(env.dof_vel - env.last_dof_vel), dim=1)

    # 8. Excessive joint velocity penalty
    dof_vel_limit_scale = -0.0005
    dof_vel_limit_penalty = torch.sum(torch.square(env.dof_vel), dim=1)

    total_reward = (
        lin_vel_x_scale * lin_vel_x_reward
        + ang_vel_z_scale * ang_vel_z_reward
        + base_height_scale * base_height_penalty
        + orientation_scale * orientation_penalty
        + lateral_vel_scale * lateral_vel_penalty
        + action_smoothness_scale * action_smoothness_penalty
        + dof_vel_smoothness_scale * dof_vel_smoothness_penalty
        + dof_vel_limit_scale * dof_vel_limit_penalty
    )

    reward_components = {
        "lin_vel_x_reward": lin_vel_x_scale * lin_vel_x_reward,
        "ang_vel_z_reward": ang_vel_z_scale * ang_vel_z_reward,
        "base_height_penalty": base_height_scale * base_height_penalty,
        "orientation_penalty": orientation_scale * orientation_penalty,
        "lateral_vel_penalty": lateral_vel_scale * lateral_vel_penalty,
        "action_smoothness_penalty": action_smoothness_scale * action_smoothness_penalty,
        "dof_vel_smoothness_penalty": dof_vel_smoothness_scale * dof_vel_smoothness_penalty,
        "dof_vel_limit_penalty": dof_vel_limit_scale * dof_vel_limit_penalty,
    }
    reward_scales = {
        "lin_vel_x_reward": lin_vel_x_scale,
        "ang_vel_z_reward": ang_vel_z_scale,
        "base_height_penalty": base_height_scale,
        "orientation_penalty": orientation_scale,
        "lateral_vel_penalty": lateral_vel_scale,
        "action_smoothness_penalty": action_smoothness_scale,
        "dof_vel_smoothness_penalty": dof_vel_smoothness_scale,
        "dof_vel_limit_penalty": dof_vel_limit_scale,
    }
    return total_reward, reward_components, reward_scales