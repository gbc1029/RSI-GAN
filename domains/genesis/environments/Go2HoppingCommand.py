import importlib
import inspect
import logging
import math
import os
import sys
from collections import defaultdict

import numpy as np

# Add the project root to Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))

import torch
from pandas.core._numba.kernels import min_max_

# Import the external Genesis physics simulation library
# We need to explicitly avoid importing our local domains.genesis package
# Import the external genesis package
gs = importlib.import_module("genesis")

# Import reward functions
from genesis.utils.geom import (
    inv_quat,  # Quaternion inverse (opposite rotation)
    quat_to_xyz,  # Convert quaternion to Euler angles (roll, pitch, yaw)
    transform_by_quat,  # Compose two rotations
    transform_quat_by_quat,  # Rotate a vector by a quaternion
)

logger = logging.getLogger(__name__)


def gs_rand_float(lower, upper, shape, device):
    return (upper - lower) * torch.rand(size=shape, device=device) + lower


class Go2HopEnv:
    """
    Go2 Jumping Environment

    This environment trains the Unitree Go2 quadruped robot to jump as high
    as possible. The robot should maximize its vertical height while maintaining
    stability.
    """

    def __init__(
        self,
        num_envs=1,
        show_viewer=False,
        add_camera=True,
        rwd_func_path=None,
        **kwargs,  # Accept additional arguments
    ):
        """
        Initialize the Go2 jumping environment.

        The task is for the quadruped to jump as high as possible.
        """
        self.rwd_func_path = rwd_func_path
        self.num_envs = num_envs
        self.add_camera = add_camera
        if self.add_camera:
            self.video_filename = kwargs.get("video_filename", "video.mp4")
            self.eval_dir = kwargs.get("eval_dir", "rl_eval")
            self.video_fps = kwargs.get("video_fps", 30)
            self.fixed_camera = kwargs.get("fixed_camera", False)
        self.rendered_envs_idx = 0
        self.resample_counter = 0
        # Store override parameters from kwargs
        self.config_overrides = kwargs

        # Get default configs and apply overrides
        self.env_cfg, self.obs_cfg, self.command_cfg = self._get_cfgs()

        self.num_obs = self.obs_cfg["num_obs"]
        self.num_privileged_obs = None
        self.num_actions = self.env_cfg["num_actions"]
        self.num_commands = self.command_cfg["num_commands"]
        self.device = gs.device
        print(f"device: {self.device}")

        self.simulate_action_latency = True  # there is a 1 step latency on real robot
        self.dt = 0.02  # control frequency on real robot is 50hz
        self.max_episode_length = math.ceil(self.env_cfg["episode_length_s"] / self.dt)

        self.obs_scales = self.obs_cfg["obs_scales"]
        self.reward_scales = {}

        # 1. Register reward function
        if not self.rwd_func_path:
            # Use the default reward function for jumping
            from domains.genesis.reward.default_hop_function import compute_reward

            self.compute_reward = compute_reward

        else:
            func = self.extract_reward_function(rwd_func_path=self.rwd_func_path)

            # Store function for later validation after environment is fully initialized
            self._validate_reward_function = func

            self.compute_reward = func

        # Log the reward function source code
        try:
            reward_source = inspect.getsource(self.compute_reward)
            logger.info(f"Reward function loaded:\n{reward_source}")
        except (OSError, TypeError) as e:
            logger.info(
                f"Reward function loaded: {self.compute_reward} (source not available: {e})"
            )
            if self.rwd_func_path:
                logger.info(f"Reward function loaded from: {self.rwd_func_path}")

        # 2. create scene
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=2),
            viewer_options=gs.options.ViewerOptions(
                max_FPS=int(0.5 / self.dt),
                camera_pos=(2.0, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=40,
            ),
            vis_options=gs.options.VisOptions(
                rendered_envs_idx=[self.rendered_envs_idx],
                background_color=(0, 0, 0),  # Black background
                ambient_light=(0.6, 0.6, 0.6),  # Higher ambient for robot visibility
                shadow=True,
                plane_reflection=False,
                lights=[
                    {"type": "directional", "dir": (-5, -0.5, -20), "color": (1.0, 1.0, 1.0), "intensity": 10.0},
                ],
            ),
            rigid_options=gs.options.RigidOptions(
                dt=self.dt,
                constraint_solver=gs.constraint_solver.Newton,
                enable_collision=True,
                enable_joint_limit=True,
                max_collision_pairs=30,
            ),
            show_viewer=show_viewer,
        )

        # add ground plane with black tile pattern
        # Create a grid texture (black tiles with white lines)
        tile_size = 128  # pixels per tile (bigger tiles)
        num_tiles = 2  # grid
        line_width = 2  # white line width in pixels
        grid_size = tile_size * num_tiles
        grid_texture = np.zeros((grid_size, grid_size, 3), dtype=np.uint8)
        grid_texture[:, :] = 10  # Fill with black

        # Draw white grid lines
        for i in range(num_tiles + 1):
            pos = i * tile_size
            # Horizontal lines
            if pos < grid_size:
                grid_texture[max(0, pos - line_width // 2):min(grid_size, pos + line_width // 2), :] = 255
            # Vertical lines
            if pos < grid_size:
                grid_texture[:, max(0, pos - line_width // 2):min(grid_size, pos + line_width // 2)] = 255

        self.scene.add_entity(
            gs.morphs.Plane(
                pos=(0, 0, 0),
                normal=(0, 0, 1),
                visualization=True,
                collision=True,
                fixed=True,
            ),
            surface=gs.surfaces.Plastic(
                roughness=0.8,  # Higher roughness for more diffused reflections
                diffuse_texture=gs.textures.ImageTexture(
                    image_array=grid_texture,
                ),
            ),
        )

        # add robot
        self.base_init_pos = torch.tensor(
            self.env_cfg["base_init_pos"], device=gs.device
        )
        self.base_init_quat = torch.tensor(
            self.env_cfg["base_init_quat"], device=gs.device
        )
        # Store inverse of initial robot orientation
        self.inv_base_init_quat = inv_quat(self.base_init_quat)
        self.robot = self.scene.add_entity(
            gs.morphs.URDF(
                file="urdf/go2/urdf/go2.urdf",
                pos=self.base_init_pos.cpu().numpy(),
                quat=self.base_init_quat.cpu().numpy(),
            ),
        )

        # add camera if requested (must be before scene.build())
        if add_camera:
            # Use higher resolution for fixed camera (high quality mode)
            if self.fixed_camera:
                camera_res = (1920, 1080)  # Full HD for fixed camera
            else:
                camera_res = (1280, 720)  # Standard HD for following camera

            # Camera positioned to robot's right side (negative Y)
            camera_config = {
                "res": camera_res,
                "pos": (0.0, -3.5, 0.5),
                "lookat": (0, 0, 0.4),
                "fov": 40,
                "GUI": False,
            }

            self.camera = self.scene.add_camera(
                res=camera_config.get("res", (1280, 720)),
                pos=camera_config.get("pos", (3.5, 0.0, 2.5)),
                lookat=camera_config.get("lookat", (0, 0, 0.5)),
                fov=camera_config.get("fov", 40),
                GUI=camera_config.get("GUI", False),
            )
        else:
            self.camera = None

        # build scene
        self.scene.build(n_envs=num_envs)

        # 3. PD control parameters
        self.motors_dof_idx = [
            self.robot.get_joint(name).dof_start for name in self.env_cfg["joint_names"]
        ]

        self.robot.set_dofs_kp(
            [self.env_cfg["kp"]] * self.num_actions, self.motors_dof_idx
        )
        self.robot.set_dofs_kv(
            [self.env_cfg["kd"]] * self.num_actions, self.motors_dof_idx
        )

        # 5. Initialize buffers
        self.base_lin_vel = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )
        self.base_ang_vel = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )
        self.projected_gravity = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )
        self.global_gravity = torch.tensor(
            [0.0, 0.0, -1.0], device=gs.device, dtype=gs.tc_float
        ).repeat(self.num_envs, 1)
        self.obs_buf = torch.zeros(
            (self.num_envs, self.num_obs), device=gs.device, dtype=gs.tc_float
        )

        self.reset_buf = torch.ones((self.num_envs,), device=gs.device, dtype=gs.tc_int)
        self.episode_length_buf = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_int
        )
        self.commands = torch.zeros(
            (self.num_envs, self.num_commands), device=gs.device, dtype=gs.tc_float
        )
        self.commands_scale = torch.tensor(
            [
                self.obs_scales["lin_vel"],
                self.obs_scales["lin_vel"],
                self.obs_scales["ang_vel"],
            ],
            device=gs.device,
            dtype=gs.tc_float,
        )
        self.actions = torch.zeros(
            (self.num_envs, self.num_actions), device=gs.device, dtype=gs.tc_float
        )
        self.last_actions = torch.zeros_like(self.actions)
        self.dof_pos = torch.zeros_like(self.actions)
        self.dof_vel = torch.zeros_like(self.actions)
        self.last_dof_vel = torch.zeros_like(self.actions)
        self.base_pos = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )
        self.base_quat = torch.zeros(
            (self.num_envs, 4), device=gs.device, dtype=gs.tc_float
        )
        self.default_dof_pos = torch.tensor(
            [
                self.env_cfg["default_joint_angles"][name]
                for name in self.env_cfg["joint_names"]
            ],
            device=gs.device,
            dtype=gs.tc_float,
        )

        # Store initial position for tracking displacement
        self.init_base_pos = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )

        self.rew_buf = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.fitness_score = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.total_reward = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.max_height = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.episode_sums = dict()

        self.extras = dict()
        self.extras["observations"] = dict()
        self.extras["component_scale"] = defaultdict(float)
        self.extras["all_episodes"] = {}
        self.extras["all_episodes"]["fitness_score"] = []
        self.extras["all_episodes"]["reward_component"] = defaultdict(list)
        self.extras["all_episodes"]["total_reward"] = []
        self.extras["all_episodes"]["speed_command"] = {}
        self.extras["all_episodes"]["speed_command"]["min_max_lin_vel_x"] = []
        self.extras["total_episodes_played"] = 0
        self.extras["episodes_completed_this_step"] = 0
        self.extras["cumulative_episode_count"] = 0
        self.extras["num_envs"] = self.num_envs

    def _get_cfgs(self):
        """Get configuration for the jumping task."""
        env_cfg = {
            "num_actions": 12,
            "default_joint_angles": {
                "FL_hip_joint": 0.0,
                "FR_hip_joint": 0.0,
                "RL_hip_joint": 0.0,
                "RR_hip_joint": 0.0,
                "FL_thigh_joint": 0.8,
                "FR_thigh_joint": 0.8,
                "RL_thigh_joint": 1.0,
                "RR_thigh_joint": 1.0,
                "FL_calf_joint": -1.5,
                "FR_calf_joint": -1.5,
                "RL_calf_joint": -1.5,
                "RR_calf_joint": -1.5,
            },
            "joint_names": [
                "FR_hip_joint",
                "FR_thigh_joint",
                "FR_calf_joint",
                "FL_hip_joint",
                "FL_thigh_joint",
                "FL_calf_joint",
                "RR_hip_joint",
                "RR_thigh_joint",
                "RR_calf_joint",
                "RL_hip_joint",
                "RL_thigh_joint",
                "RL_calf_joint",
            ],
            "kp": 20.0,
            "kd": 0.5,
            # More lenient termination for jumping (robot will be more dynamic)
            "termination_if_roll_greater_than": 30,  # degree (more lenient for jumping)
            "termination_if_pitch_greater_than": 30,  # degree (more lenient for jumping)
            "base_init_pos": [0.0, 0.0, 0.42],
            "base_init_quat": [1.0, 0.0, 0.0, 0.0],
            "episode_length_s": 20.0,
            "resampling_time_s": 4.0,
            "action_scale": 0.25,
            "simulate_action_latency": True,
            "clip_actions": 100.0,
        }
        obs_cfg = {
            "num_obs": 45,
            "obs_scales": {
                "lin_vel": 2.0,
                "ang_vel": 0.25,
                "dof_pos": 1.0,
                "dof_vel": 0.05,
            },
        }
        # Command configuration for jumping
        command_cfg = {
            "num_commands": 3,
            "lin_vel_x_range": [0.0, 0.0],  # No forward movement
            "lin_vel_y_range": [0.0, 0.0],  # No sideways movement
            "ang_vel_range": [0.0, 0.0],    # No rotation
        }

        return env_cfg, obs_cfg, command_cfg

    def _resample_commands(self, envs_idx):
        """Resample commands for jumping task.

        Commands are set to zero as the robot should focus on jumping high.
        """
        self.commands[envs_idx, 0] = 0.0  # No forward movement
        self.commands[envs_idx, 1] = 0.0  # No sideways movement
        self.commands[envs_idx, 2] = 0.0  # No rotation

        # Record for report
        if self.episode_sums and len(envs_idx) > 0:
            min_max_lin_vel_x = (0.0, 0.0)
            self.extras["all_episodes"]["speed_command"]["min_max_lin_vel_x"].append(
                min_max_lin_vel_x
            )

            num_completed_episodes = len(envs_idx)
            self.extras["total_episodes_played"] += num_completed_episodes
            self.extras["episodes_completed_this_step"] = num_completed_episodes

            # fitness score
            self.extras["all_episodes"]["fitness_score"].append(
                torch.mean(self.fitness_score[envs_idx]).item()
                / self.env_cfg["resampling_time_s"]
            )
            self.fitness_score[envs_idx] = 0.0

            # total reward
            self.extras["all_episodes"]["total_reward"].append(
                torch.mean(self.total_reward[envs_idx]).item()
                / self.env_cfg["resampling_time_s"]
            )
            self.total_reward[envs_idx] = 0.0

            for key in self.episode_sums.keys():
                self.extras["all_episodes"]["reward_component"][f"{key}"].append(
                    torch.mean(self.episode_sums[key][envs_idx]).item()
                    / self.env_cfg["resampling_time_s"]
                )
                self.episode_sums[key][envs_idx] = 0.0

            if self.camera and self.rendered_envs_idx in envs_idx:
                print(
                    f"Stopping recording and saving to {self.eval_dir}/{self.video_filename}_{self.resample_counter}.mp4"
                )
                self.camera.stop_recording(
                    save_to_filename=f"{self.eval_dir}/{self.video_filename}_{self.resample_counter}.mp4",
                    fps=self.video_fps,
                )
                self.resample_counter += 1

        if self.camera and self.rendered_envs_idx in envs_idx and len(envs_idx) > 0:
            print(
                f"Starting video recording for env {self.rendered_envs_idx}, episode {self.resample_counter}"
            )
            self.camera.start_recording()

    def step(self, actions, step_count=0):
        """Execute one step in the environment."""
        # 1. Action Execution
        self.actions = torch.clip(
            actions, -self.env_cfg["clip_actions"], self.env_cfg["clip_actions"]
        )
        exec_actions = (
            self.last_actions if self.simulate_action_latency else self.actions
        )
        target_dof_pos = (
            exec_actions * self.env_cfg["action_scale"] + self.default_dof_pos
        )
        self.robot.control_dofs_position(target_dof_pos, self.motors_dof_idx)
        self.scene.step()

        # Render camera if available
        if self.camera is not None and step_count % (30 / self.video_fps) == 0:
            # Only update camera position if not using fixed camera
            if not self.fixed_camera:
                robot_pos = self.base_pos[self.rendered_envs_idx].cpu().numpy()
                camera_radius = 3.0
                camera_height_offset = 2.0
                camera_x = robot_pos[0] + camera_radius * np.sin(0)
                camera_y = robot_pos[1] + camera_radius * np.cos(0)
                camera_z = robot_pos[2] + camera_height_offset
                lookat_pos = (robot_pos[0], robot_pos[1], robot_pos[2] + 0.2)
                self.camera.set_pose(
                    pos=(camera_x, camera_y, camera_z),
                    lookat=lookat_pos,
                )
            self.camera.render()

        # 2. Update States
        self.episode_length_buf += 1
        self.base_pos[:] = self.robot.get_pos()
        self.base_quat[:] = self.robot.get_quat()

        self.base_euler = quat_to_xyz(
            transform_quat_by_quat(
                torch.ones_like(self.base_quat) * self.inv_base_init_quat,
                self.base_quat,
            ),
            rpy=True,
            degrees=True,
        )

        inv_base_quat = inv_quat(self.base_quat)
        self.base_lin_vel[:] = transform_by_quat(self.robot.get_vel(), inv_base_quat)
        self.base_ang_vel[:] = transform_by_quat(self.robot.get_ang(), inv_base_quat)
        self.projected_gravity = transform_by_quat(self.global_gravity, inv_base_quat)
        self.dof_pos[:] = self.robot.get_dofs_position(self.motors_dof_idx)
        self.dof_vel[:] = self.robot.get_dofs_velocity(self.motors_dof_idx)

        # Track maximum height reached during episode
        self.max_height = torch.maximum(self.max_height, self.base_pos[:, 2])

        # 3. Resample commands
        envs_idx = (
            (
                self.episode_length_buf
                % int(self.env_cfg["resampling_time_s"] / self.dt)
                == 0
            )
            .nonzero(as_tuple=False)
            .reshape((-1,))
        )
        if len(envs_idx) > 0:
            self._resample_commands(envs_idx)

        # 4. Reset
        self.reset_buf = self.episode_length_buf > self.max_episode_length
        self.reset_buf |= (
            torch.abs(self.base_euler[:, 1])
            > self.env_cfg["termination_if_pitch_greater_than"]
        )
        self.reset_buf |= (
            torch.abs(self.base_euler[:, 0])
            > self.env_cfg["termination_if_roll_greater_than"]
        )

        time_out_idx = (
            (self.episode_length_buf > self.max_episode_length)
            .nonzero(as_tuple=False)
            .reshape((-1,))
        )
        self.extras["time_outs"] = torch.zeros_like(
            self.reset_buf, device=gs.device, dtype=gs.tc_float
        )
        self.extras["time_outs"][time_out_idx] = 1.0
        self.reset_idx(self.reset_buf.nonzero(as_tuple=False).reshape((-1,)))

        # 5. Compute rewards
        total_reward, reward_components, reward_scales = self.compute_reward(self)
        task_fitness_score = self._task_fitness_function()
        self.rew_buf = total_reward * self.dt
        self.total_reward += total_reward * self.dt
        self.fitness_score += task_fitness_score * self.dt

        for name, rew in reward_components.items():
            if name not in self.episode_sums.keys():
                self.episode_sums[name] = torch.zeros(
                    (self.num_envs,), device=gs.device, dtype=gs.tc_float
                )
            self.episode_sums[name] += rew * self.dt
            self.extras["component_scale"][name] = reward_scales[f"{name}"]

        # 6. Compute observations
        self.obs_buf = torch.cat(
            [
                self.base_ang_vel * self.obs_scales["ang_vel"],
                self.projected_gravity,
                self.commands * self.commands_scale,
                (self.dof_pos - self.default_dof_pos) * self.obs_scales["dof_pos"],
                self.dof_vel * self.obs_scales["dof_vel"],
                self.actions,
            ],
            axis=-1,
        )
        self.last_actions[:] = self.actions[:]
        self.last_dof_vel[:] = self.dof_vel[:]

        self.extras["observations"]["critic"] = self.obs_buf

        if not torch.any(self.reset_buf):
            self.extras["episodes_completed_this_step"] = 0

        return self.obs_buf, self.rew_buf, self.reset_buf, self.extras

    def get_observations(self):
        self.extras["observations"]["critic"] = self.obs_buf
        return self.obs_buf, self.extras

    def get_privileged_observations(self):
        return None

    def reset_idx(self, envs_idx):
        if len(envs_idx) == 0:
            return

        # Reset joint positions to default stance
        self.dof_pos[envs_idx] = self.default_dof_pos
        self.dof_vel[envs_idx] = 0.0
        self.robot.set_dofs_position(
            position=self.dof_pos[envs_idx],
            dofs_idx_local=self.motors_dof_idx,
            zero_velocity=True,
            envs_idx=envs_idx,
        )

        # Reset robot pose to initial position
        self.base_pos[envs_idx] = self.base_init_pos
        self.base_quat[envs_idx] = self.base_init_quat.reshape(1, -1)
        self.robot.set_pos(
            self.base_pos[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )
        self.robot.set_quat(
            self.base_quat[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )

        # Store initial position for displacement tracking
        self.init_base_pos[envs_idx] = self.base_pos[envs_idx].clone()

        # Reset velocities to zero
        self.base_lin_vel[envs_idx] = 0
        self.base_ang_vel[envs_idx] = 0
        self.robot.zero_all_dofs_velocity(envs_idx)

        # Reset episode counters
        self.episode_length_buf[envs_idx] = 0
        self.reset_buf[envs_idx] = True
        self.last_actions[envs_idx] = 0.0
        self.last_dof_vel[envs_idx] = 0.0
        self.max_height[envs_idx] = 0.0

        # Resample commands
        if len(envs_idx) > 0:
            self._resample_commands(envs_idx)

    def reset(self):
        self.reset_buf[:] = True
        self.reset_idx(torch.arange(self.num_envs, device=gs.device))
        return self.obs_buf, None

    def _task_fitness_function(self):
        """Task fitness function for jumping as high as possible.

        Fitness is defined as the maximum height reached during the episode.
        The robot should maximize its vertical position.
        """
        # Return the maximum height reached during this episode
        fitness_score = self.max_height

        return fitness_score

    def extract_reward_function(self, rwd_func_path):
        """Extract and load reward function from file."""
        from domains.genesis.genesis_utils import file_to_string

        code_str = file_to_string(rwd_func_path)
        reward_ns = {}
        exec(code_str, reward_ns, reward_ns)
        func = reward_ns.get("compute_reward")
        if not callable(func):
            raise ValueError(
                "Extracted code must define a function named 'compute_reward'"
            )
        return func
