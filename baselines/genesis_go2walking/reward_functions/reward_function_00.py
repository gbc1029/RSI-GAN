# taken from ./outputs/generate_20251216_192315_534288/gen_79/genesis_go2walking_eval/reward_function_00.py
# average fitness of 0.7947 on go2walking

from typing import Dict, Tuple

import torch
from torch import Tensor


def compute_reward(
    env,
) -> Tuple[Tensor, Dict, Dict]:

    # 1. Forward velocity tracking (PRIMARY OBJECTIVE - HIGHEST SCALE)
    lin_vel_x_error = torch.square(env.commands[:, 0] - env.base_lin_vel[:, 0])
    lin_vel_x_reward = 3.2 * torch.exp(-lin_vel_x_error / 0.25)  # Sharp temperature for strong gradients
    
    # 2. Lateral velocity tracking (sharp temperature)
    lin_vel_y_error = torch.square(env.commands[:, 1] - env.base_lin_vel[:, 1])
    lin_vel_y_reward = 0.85 * torch.exp(-lin_vel_y_error / 0.25)
    
    # 3. Angular velocity tracking (sharp temperature)
    ang_vel_z_error = torch.square(env.commands[:, 2] - env.base_ang_vel[:, 2])
    ang_vel_z_reward = 0.75 * torch.exp(-ang_vel_z_error / 0.25)
    
    # 4. Upright stability reward (POSITIVE reward - encourages upright posture)
    # Projected gravity should be [0, 0, -1] when perfectly upright
    orientation_error = torch.square(env.projected_gravity[:, :2]).sum(dim=1)
    upright_reward = 0.85 * torch.exp(-3.0 * orientation_error)
    
    # 5. Base height stability reward (POSITIVE reward - maintains proper height)
    # Target height: 0.30m for Go2 walking stance
    height_error = torch.square(env.base_pos[:, 2] - 0.30)
    height_reward = 0.55 * torch.exp(-10.0 * height_error)
    
    # 6. Action smoothness penalty (SMALL - allows dynamic motion)
    action_rate_penalty = -0.015 * torch.sum(torch.square(env.actions - env.last_actions), dim=1)
    
    # 7. Vertical velocity penalty (moderate - discourages bouncing)
    z_vel_penalty = -0.35 * torch.square(env.base_lin_vel[:, 2])
    
    # Total reward: sum of all components
    total_reward = (
        lin_vel_x_reward + 
        lin_vel_y_reward + 
        ang_vel_z_reward +
        upright_reward + 
        height_reward +
        action_rate_penalty + 
        z_vel_penalty
    )
    
    reward_components = {
        "lin_vel_x": lin_vel_x_reward,
        "lin_vel_y": lin_vel_y_reward,
        "ang_vel_z": ang_vel_z_reward,
        "upright": upright_reward,
        "height": height_reward,
        "action_rate": action_rate_penalty,
        "z_vel": z_vel_penalty,
    }
    
    reward_scales = {
        "lin_vel_x": 3.2,
        "lin_vel_y": 0.85,
        "ang_vel_z": 0.75,
        "upright": 0.85,
        "height": 0.55,
        "action_rate": -0.015,
        "z_vel": -0.35,
    }
    
    return total_reward, reward_components, reward_scales