# taken from ./outputs/generate_20251219_105301_034575/gen_12/genesis_go2walking_eval/reward_function_02.py
# average fitness of 0.7688 on go2walking

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:

    # 1. Reward tracking of linear velocity command (forward walking - x axis)
    lin_vel_x_temperature = 4.0
    lin_vel_x_scale = 1.5
    lin_vel_x_error = torch.square(env.commands[:, 0] - env.base_lin_vel[:, 0])
    lin_vel_x_reward = torch.exp(-lin_vel_x_error * lin_vel_x_temperature)

    # 2. Reward tracking of linear velocity command (sideways - y axis)
    lin_vel_y_temperature = 4.0
    lin_vel_y_scale = 0.3
    lin_vel_y_error = torch.square(env.commands[:, 1] - env.base_lin_vel[:, 1])
    lin_vel_y_reward = torch.exp(-lin_vel_y_error * lin_vel_y_temperature)

    # 3. Reward tracking of angular velocity command (yaw)
    ang_vel_temperature = 1.0
    ang_vel_scale = 0.3
    ang_vel_error = torch.square(env.commands[:, 2] - env.base_ang_vel[:, 2])
    ang_vel_reward = torch.exp(-ang_vel_error * ang_vel_temperature)

    # 4. Penalize base height away from target
    base_height_target = 0.35
    base_height_scale = -0.5
    base_height_penalty = torch.square(env.base_pos[:, 2] - base_height_target)

    # 5. Penalize high joint velocities (energy efficiency)
    dof_vel_scale = -0.0002
    dof_vel_penalty = torch.sum(torch.square(env.dof_vel), dim=1)

    # 6. Penalize large actions (smooth control)
    action_scale = -0.01
    action_penalty = torch.sum(torch.square(env.actions), dim=1)

    # 7. Penalize action rate (encourage smooth transitions)
    action_rate_scale = -0.01
    action_rate_penalty = torch.sum(torch.square(env.actions - env.last_actions), dim=1)

    # 8. Penalize joint positions far from default (natural stance)
    dof_pos_scale = -0.05
    dof_pos_penalty = torch.sum(torch.square(env.dof_pos - env.default_dof_pos), dim=1)

    # Calculate total reward
    total_reward = (
        lin_vel_x_scale * lin_vel_x_reward +
        lin_vel_y_scale * lin_vel_y_reward +
        ang_vel_scale * ang_vel_reward +
        base_height_scale * base_height_penalty +
        dof_vel_scale * dof_vel_penalty +
        action_scale * action_penalty +
        action_rate_scale * action_rate_penalty +
        dof_pos_scale * dof_pos_penalty
    )

    reward_components = {
        "lin_vel_x_reward": lin_vel_x_scale * lin_vel_x_reward,
        "lin_vel_y_reward": lin_vel_y_scale * lin_vel_y_reward,
        "ang_vel_reward": ang_vel_scale * ang_vel_reward,
        "base_height_penalty": base_height_scale * base_height_penalty,
        "dof_vel_penalty": dof_vel_scale * dof_vel_penalty,
        "action_penalty": action_scale * action_penalty,
        "action_rate_penalty": action_rate_scale * action_rate_penalty,
        "dof_pos_penalty": dof_pos_scale * dof_pos_penalty,
    }
    
    reward_scales = {
        "lin_vel_x_reward": lin_vel_x_scale,
        "lin_vel_y_reward": lin_vel_y_scale,
        "ang_vel_reward": ang_vel_scale,
        "base_height_penalty": base_height_scale,
        "dof_vel_penalty": dof_vel_scale,
        "action_penalty": action_scale,
        "action_rate_penalty": action_rate_scale,
        "dof_pos_penalty": dof_pos_scale,
    }
    
    return total_reward, reward_components, reward_scales