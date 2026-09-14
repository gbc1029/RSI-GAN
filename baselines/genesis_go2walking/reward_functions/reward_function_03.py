# taken from ./outputs/generate_20260106_094059_776217/gen_39/genesis_go2walking_eval/reward_function_03.py
# average fitness of 0.8586 on go2walking

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:

    # 1. Primary Goal: Track forward velocity command
    lin_vel_x_error = torch.square(env.base_lin_vel[:, 0] - env.commands[:, 0])
    lin_vel_x_reward = torch.exp(-3.0 * lin_vel_x_error)
    lin_vel_x_scale = 1.5

    # 2. Track angular velocity command (yaw) for turning
    ang_vel_z_error = torch.square(env.base_ang_vel[:, 2] - env.commands[:, 2])
    ang_vel_z_reward = torch.exp(-2.0 * ang_vel_z_error)
    ang_vel_z_scale = 0.5

    # 3. Penalize lateral velocity (drift)
    lateral_vel_penalty = torch.square(env.base_lin_vel[:, 1])
    lateral_vel_scale = -0.3

    # 4. Maintain target base height
    base_height_target = 0.30
    base_height_penalty = torch.square(env.base_pos[:, 2] - base_height_target)
    base_height_scale = -0.5

    # 5. Penalize excessive roll and pitch angular velocities (orientation stability)
    orientation_penalty = torch.sum(torch.square(env.base_ang_vel[:, :2]), dim=-1)
    orientation_scale = -0.2

    # 6. Penalize large action changes (smoothness)
    action_rate_penalty = torch.sum(torch.square(env.actions - env.last_actions), dim=-1)
    action_rate_scale = -0.005

    # 7. Penalize high joint velocities (energy efficiency)
    joint_vel_penalty = torch.sum(torch.square(env.dof_vel), dim=-1)
    joint_vel_scale = -0.001

    # 8. Penalize vertical velocity (discourage jumping)
    vertical_vel_penalty = torch.square(env.base_lin_vel[:, 2])
    vertical_vel_scale = -0.15

    # Total reward
    total_reward = (
        lin_vel_x_scale * lin_vel_x_reward
        + ang_vel_z_scale * ang_vel_z_reward
        + lateral_vel_scale * lateral_vel_penalty
        + base_height_scale * base_height_penalty
        + orientation_scale * orientation_penalty
        + action_rate_scale * action_rate_penalty
        + joint_vel_scale * joint_vel_penalty
        + vertical_vel_scale * vertical_vel_penalty
    )

    reward_components = {
        "lin_vel_x_reward": lin_vel_x_scale * lin_vel_x_reward,
        "ang_vel_z_reward": ang_vel_z_scale * ang_vel_z_reward,
        "lateral_vel_penalty": lateral_vel_scale * lateral_vel_penalty,
        "base_height_penalty": base_height_scale * base_height_penalty,
        "orientation_penalty": orientation_scale * orientation_penalty,
        "action_rate_penalty": action_rate_scale * action_rate_penalty,
        "joint_vel_penalty": joint_vel_scale * joint_vel_penalty,
        "vertical_vel_penalty": vertical_vel_scale * vertical_vel_penalty,
    }
    
    reward_scales = {
        "lin_vel_x_reward": lin_vel_x_scale,
        "ang_vel_z_reward": ang_vel_z_scale,
        "lateral_vel_penalty": lateral_vel_scale,
        "base_height_penalty": base_height_scale,
        "orientation_penalty": orientation_scale,
        "action_rate_penalty": action_rate_scale,
        "joint_vel_penalty": joint_vel_scale,
        "vertical_vel_penalty": vertical_vel_scale,
    }

    return total_reward, reward_components, reward_scales