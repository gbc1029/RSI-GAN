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


class Go2Env:

    def __init__(
        self,
        num_envs=1,
        show_viewer=False,
        add_camera=True,
        rwd_func_path=None,
        **kwargs,  # Accept additional arguments
    ):
        """
        The __init__ function sets up the simulation environment with the following steps:
        1. Reward Registration. Reward functions, defined in the configuration, are registered to guide the policy. These functions will be explained in the “Reward” section.

        2. Control Frequency. The simulation runs at 50 Hz, matching the real robot’s control frequency. To further bridge sim2real gap, we also manually simulate the action latecy (~20ms, one dt) shown on the real robot.

        3. Scene Creation. A simulation scene is created, including the robot and a static plane.

        4. PD Controller Setup. Motors are first identified based on their names. Stiffness and damping are then set for each motor.

        5. Buffer Initialization. Buffers are initialized to store environment states, observations, and rewards

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
            # Use the default reward function
            from domains.genesis.reward.default_function import compute_reward

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
            # If source is not available (e.g., dynamically generated), log the function object
            logger.info(
                f"Reward function loaded: {self.compute_reward} (source not available: {e})"
            )
            # Optionally, for dynamically loaded functions, log the file path
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
            # `list(range(1))` means only environment index `[0]` is visually rendered, even though 4096 environments are running.
            vis_options=gs.options.VisOptions(
                rendered_envs_idx=[self.rendered_envs_idx],
                background_color=(0, 0, 0),  # Black background
                ambient_light=(0.6, 0.6, 0.6),  # Higher ambient for robot visibility
                shadow=True,
                plane_reflection=False,
                lights=[
                    {"type": "directional", "dir": (-1, -0.5, -1), "color": (1.0, 1.0, 1.0), "intensity": 5.0},
                    {"type": "directional", "dir": (0.5, -1, -0.5), "color": (1.0, 1.0, 1.0), "intensity": 3.0},
                ],
            ),
            rigid_options=gs.options.RigidOptions(
                dt=self.dt,
                constraint_solver=gs.constraint_solver.Newton,
                enable_collision=True,
                enable_joint_limit=True,
                # for this locomotion policy there are usually no more than 30 collision pairs
                # set a low value can save memory
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
                "pos": (0.0, -3.5, 0.5),  # Camera position to robot's right, at eye level
                "lookat": (0, 0, 0.4),  # Look at the robot center
                "fov": 40,  # Field of view
                "GUI": False,  # No GUI window for headless mode
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
        # names to indices
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
        )  # 3 (linear velocity)
        self.base_ang_vel = torch.zeros(
            (self.num_envs, 3), device=gs.device, dtype=gs.tc_float
        )  # 3 (angular velocity)
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

        self.rew_buf = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.fitness_score = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.total_reward = torch.zeros(
            (self.num_envs,), device=gs.device, dtype=gs.tc_float
        )
        self.episode_sums = dict()

        self.extras = dict()  # extra information for logging
        self.extras["observations"] = dict()
        self.extras["component_scale"] = defaultdict(float)
        self.extras["all_episodes"] = {}
        self.extras["all_episodes"]["fitness_score"] = []
        self.extras["all_episodes"]["reward_component"] = defaultdict(list)
        self.extras["all_episodes"]["total_reward"] = []
        self.extras["all_episodes"]["speed_command"] = {}
        self.extras["all_episodes"]["speed_command"]["min_max_lin_vel_x"] = []
        self.extras["total_episodes_played"] = (
            0  # Track total episodes across all environments
        )
        self.extras["episodes_completed_this_step"] = (
            0  # Track episodes completed in current step
        )
        self.extras["cumulative_episode_count"] = 0  # Alternative cumulative counter
        self.extras["num_envs"] = self.num_envs
        self.extras["lin_vel_x_range"] = self.command_cfg["lin_vel_x_range"]
        # Validate reward function now that environment is fully initialized
        # self._validate_reward_function_output()

    def _validate_reward_function_output(self):
        """Validate that the reward function returns correct output format"""
        try:
            # Test the function with self to check output format
            test_result = self._validate_reward_function(self)

            # Check if function returns exactly 3 values
            if not isinstance(test_result, (tuple, list)) or len(test_result) != 3:
                raise ValueError(
                    f"compute_reward must return exactly 3 values (total_reward, reward_components, reward_scales), "
                    f"but got {len(test_result) if isinstance(test_result, (tuple, list)) else 'non-tuple/list'} values"
                )

            total_reward, reward_components, reward_scales = test_result

            # Check if total_reward is a tensor
            if not isinstance(total_reward, torch.Tensor):
                raise ValueError(
                    f"total_reward must be a torch.Tensor, but got {type(total_reward)}"
                )

            # Check if reward_components is a dictionary
            if not isinstance(reward_components, dict):
                raise ValueError(
                    f"reward_components must be a dictionary, but got {type(reward_components)}"
                )

            # Check if reward_scales is a dictionary
            if not isinstance(reward_scales, dict):
                raise ValueError(
                    f"reward_scales must be a dictionary, but got {type(reward_scales)}"
                )

            # Check if reward_components and reward_scales have matching keys
            if set(reward_components.keys()) != set(reward_scales.keys()):
                raise ValueError(
                    f"reward_components keys {set(reward_components.keys())} must match "
                    f"reward_scales keys {set(reward_scales.keys())}"
                )

            # Check if all reward components are tensors with correct shape
            for name, reward in reward_components.items():
                if not isinstance(reward, torch.Tensor):
                    raise ValueError(
                        f"reward_components['{name}'] must be a torch.Tensor, but got {type(reward)}"
                    )
                if reward.shape != total_reward.shape:
                    raise ValueError(
                        f"reward_components['{name}'] shape {reward.shape} must match "
                        f"total_reward shape {total_reward.shape}"
                    )

            # Check if all reward scales are numbers
            for name, scale in reward_scales.items():
                if not isinstance(scale, (int, float)):
                    raise ValueError(
                        f"reward_scales['{name}'] must be a number (int or float), but got {type(scale)}"
                    )

        except Exception as e:
            # Re-raise with more context
            raise ValueError(
                f"Error validating compute_reward function: {str(e)}"
            ) from e

    def _get_cfgs(self):
        """
        What is a Quaternion?
        ---------------------

        A quaternion is a 4-dimensional number system used to represent rotations in 3D space. It consists of:

        *   **1 scalar component** (w)
        *   **3 vector components** (x, y, z)

        Format: `[w, x, y, z]` or `[x, y, z, w]` (depending on convention)

        Why Use Quaternions Instead of Euler Angles?
        --------------------------------------------

        **Advantages of Quaternions:**

        1.  **No Gimbal Lock** - Euler angles can lose a degree of freedom in certain orientations
        2.  **Smooth Interpolation** - Better for animation and control
        3.  **Efficient Composition** - Combining rotations is faster
        4.  **Numerical Stability** - Less prone to accumulated errors

        **Disadvantages:**

        1.  **Less Intuitive** - Harder for humans to interpret
        2.  **4 Numbers** - Uses more memory than 3 Euler angles
        """
        # Default configuration
        env_cfg = {
            # "target_pos": [4.0, 0.0, 0.42],  # [m]
            "num_actions": 12,
            # joint/link names
            "default_joint_angles": {  # [rad]
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
            # PD
            "kp": 20.0,
            "kd": 0.5,
            # termination
            "termination_if_roll_greater_than": 10,  # degree
            "termination_if_pitch_greater_than": 10,
            # base pose
            "base_init_pos": [
                0.0,
                0.0,
                0.42,
            ],  # [forward/backward, left/right, height above ground]
            # quat = quaternion: [w, x, y, z]
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
        # Default command configuration
        command_cfg = {
            "num_commands": 3,
            # The robot is commanded to move at a **constant forward velocity of 0.5 m/s**
            "lin_vel_x_range": [0.5, 0.5],  # Default: Fixed forward velocity of 0.5 m/s
            "lin_vel_y_range": [0.0, 0.0],  # Default: No sideways movement
            "ang_vel_range": [0.0, 0.0],  # Default: No rotation
        }

        # Apply overrides from kwargs
        if self.config_overrides:
            # Override command_cfg parameters
            if "lin_vel_x_range" in self.config_overrides:
                command_cfg["lin_vel_x_range"] = self.config_overrides[
                    "lin_vel_x_range"
                ]

        return env_cfg, obs_cfg, command_cfg

    def _resample_commands(self, envs_idx):
        """
        * WHY resample commands?
            The robot must learn to **react to changing commands** during episodes, making it more robust
        *   **Only specific environments** (`envs_idx`) get new commands at each resampling time
        *   Each environment can have **different commands** if ranges allow variation
        *   Commands are sampled **uniformly** within the specified range
        """
        self.commands[envs_idx, 0] = gs_rand_float(
            self.command_cfg["lin_vel_x_range"][0],
            self.command_cfg["lin_vel_x_range"][1],
            (len(envs_idx),),
            gs.device,
        )
        self.commands[envs_idx, 1] = gs_rand_float(
            self.command_cfg["lin_vel_y_range"][0],
            self.command_cfg["lin_vel_y_range"][1],
            (len(envs_idx),),
            gs.device,
        )
        self.commands[envs_idx, 2] = gs_rand_float(
            self.command_cfg["ang_vel_range"][0],
            self.command_cfg["ang_vel_range"][1],
            (len(envs_idx),),
            gs.device,
        )

        ################################### RECORD FOR REPORT ########################################
        # fill extras
        # only record the extras if there are environments to resample

        if self.episode_sums and len(envs_idx) > 0:
            # record min/max command speed values for logging (only if there are environments to resample)
            min_max_lin_vel_x = (
                self.commands[envs_idx, 0].min().item(),
                self.commands[envs_idx, 0].max().item(),
            )
            self.extras["all_episodes"]["speed_command"]["min_max_lin_vel_x"].append(
                min_max_lin_vel_x
            )

            # Increment total episodes counter by number of completed episodes
            num_completed_episodes = len(envs_idx)
            self.extras["total_episodes_played"] += num_completed_episodes
            self.extras["episodes_completed_this_step"] = num_completed_episodes

            # fitness score
            # records the average fitness score per second for each completed episode
            self.extras["all_episodes"]["fitness_score"].append(
                torch.mean(self.fitness_score[envs_idx]).item()
                / self.env_cfg["resampling_time_s"]
            )
            self.fitness_score[envs_idx] = 0.0  # reset fitness score
            # total reward
            self.extras["all_episodes"]["total_reward"].append(
                torch.mean(self.total_reward[envs_idx]).item()
                / self.env_cfg["resampling_time_s"]
            )
            self.total_reward[envs_idx] = 0.0  # reset total reward

            for key in self.episode_sums.keys():
                # average reward across all envs, per second for each reward component for the current finished episode
                # self.extras["episode"]["rew_" + key] = (
                #     torch.mean(self.episode_sums[key][envs_idx]).item()
                #     / self.env_cfg["episode_length_s"]
                # )
                #  average reward across all envs, per second for each reward component for ALL the eposodes
                self.extras["all_episodes"]["reward_component"][f"{key}"].append(
                    torch.mean(self.episode_sums[key][envs_idx]).item()
                    / self.env_cfg["resampling_time_s"]
                )
                # reset episode sum
                self.episode_sums[key][envs_idx] = 0.0

            # Stop recording and save video
            # stop recording if the target environment is in the list of resample environments
            if self.camera and self.rendered_envs_idx in envs_idx:
                print(
                    f"Stopping recording and saving to {self.eval_dir}/{self.video_filename}_{self.resample_counter}.mp4"
                )
                self.camera.stop_recording(
                    save_to_filename=f"{self.eval_dir}/{self.video_filename}_{self.resample_counter}.mp4",
                    fps=self.video_fps,
                )
                # Increment counter after stopping recording
                self.resample_counter += 1

        # Start new recording if enabled
        # start recording if the target environment is in the list of resample environments
        if self.camera and self.rendered_envs_idx in envs_idx and len(envs_idx) > 0:
            print(
                f"Starting video recording for env {self.rendered_envs_idx}, episode {self.resample_counter}"
            )
            self.camera.start_recording()

    def step(self, actions, step_count=0):
        """
        The step function takes the action for execution and returns new observations and rewards.
        Here is how it works:
        1. Action Execution. The input action will be clipped, rescaled, and added on top of default motor positions. The transformed action, representing target joint positions, will then be sent to the robot controller for one-step execution.
        2. State Updates. Robot states, such as joint positions and velocities, are retrieved and stored in buffers.
        3. Resample commands. Commands are resampled every 4 seconds during episodes. This is to ensure that the robot The robot must learn to **react to changing commands** during episodes, making it more robust.
        4. Termination Checks. Environments are terminated if (1) Episode length exceeds the maximum allowed (2) The robot’s body orientation deviates significantly. Terminated environments are reset automatically.
        5. Reward Computation.
        6. Observation Computation. Observation used for training includes base angular velocity, projected gravity, commands, dof position, dof velocity, and previous actions.

        Reset happens when:
        1.  Time limit: Episode exceeds 1000 steps (20 seconds)
        2.  Robot falls: Roll or pitch > 10 degrees (robot tips over)
        """
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
        # Render camera if available (for recording or visualization)
        if self.camera is not None and step_count % (30 / self.video_fps) == 0:
            # Only update camera position if not using fixed camera
            if not self.fixed_camera:
                # Get the robot's current position (for the chosen environment)
                robot_pos = self.base_pos[self.rendered_envs_idx].cpu().numpy()  # [x, y, z]

                # Dynamic camera movement - circle around the robot's actual position
                camera_radius = 3.0
                camera_height_offset = 2.0  # Height above robot

                # Calculate camera position relative to robot
                # camera_x = robot_pos[0] + camera_radius * np.sin(step_count / 60)
                # camera_y = robot_pos[1] + camera_radius * np.cos(step_count / 60)
                camera_x = robot_pos[0] + camera_radius * np.sin(0)
                camera_y = robot_pos[1] + camera_radius * np.cos(0)
                camera_z = robot_pos[2] + camera_height_offset

                # Camera looks at robot's center (slightly above ground level)
                lookat_pos = (robot_pos[0], robot_pos[1], robot_pos[2] + 0.2)

                self.camera.set_pose(
                    pos=(camera_x, camera_y, camera_z),
                    lookat=lookat_pos,
                )
            self.camera.render()

        # 2. Update States (buffers)
        self.episode_length_buf += 1
        self.base_pos[:] = self.robot.get_pos()
        self.base_quat[:] = self.robot.get_quat()
        """
        Roll, Pitch, and Yaw (Euler Angles)
        -----------------------------------
        These are the three rotational axes that describe 3D orientation:

        **Roll** (rotation around X-axis):

        *   **Left/Right tilting** of the robot
        *   Like a motorcycle leaning into a turn
        *   If the robot tips over sideways, it has excessive roll

        **Pitch** (rotation around Y-axis):

        *   **Forward/Backward tilting** of the robot
        *   Like a car going uphill (nose up) or downhill (nose down)
        *   If the robot falls forward or backward, it has excessive pitch

        **Yaw** (rotation around Z-axis):

        *   **Left/Right turning** of the robot
        *   Rotation around the vertical axis
        *   Like a car steering left or right
        """
        self.base_euler = quat_to_xyz(
            transform_quat_by_quat(
                torch.ones_like(self.base_quat) * self.inv_base_init_quat,
                self.base_quat,
            ),
            rpy=True,
            degrees=True,
        )
        # Get inverse of current robot orientation
        inv_base_quat = inv_quat(
            self.base_quat
        )  # Quaternion inverse (opposite rotation)

        # Transform world-frame vectors to robot body frame
        """
        **World Frame**: Fixed global coordinate system (gravity always points down as [0,0,-1])
        **Body Frame**: Coordinate system attached to the robot (moves and rotates with robot)
        * Robot's sensors measure in body frame (IMU, joint encoders)
        * Control commands are more intuitive in body frame
        """
        self.base_lin_vel[:] = transform_by_quat(self.robot.get_vel(), inv_base_quat)
        self.base_ang_vel[:] = transform_by_quat(self.robot.get_ang(), inv_base_quat)
        self.projected_gravity = transform_by_quat(self.global_gravity, inv_base_quat)
        self.dof_pos[:] = self.robot.get_dofs_position(self.motors_dof_idx)
        self.dof_vel[:] = self.robot.get_dofs_velocity(self.motors_dof_idx)

        # 3. Resample commands
        # commands are resampled **every 4 seconds** (`resampling_time_s` = 4.0) during episodes
        # which equals **200 time steps** at 50Hz control frequency.
        # This creates a **staggered resampling pattern** because
        # **initially**: If all environments start together, they would resample simultaneously every 200 steps
        # **After terminations**: Environments reset at different times due to different failure conditions (roll/pitch limits, episode length)
        # **Desynchronization**: Once an environment resets, its "episode_length_buf" becomes 0, putting it out of sync with others
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
        # check if termination or reset conditions are met
        # Check for fall conditions (roll/pitch limits exceeded) are met
        self.reset_buf = self.episode_length_buf > self.max_episode_length
        self.reset_buf |= (
            torch.abs(self.base_euler[:, 1])
            > self.env_cfg["termination_if_pitch_greater_than"]
        )
        self.reset_buf |= (
            torch.abs(self.base_euler[:, 0])
            > self.env_cfg["termination_if_roll_greater_than"]
        )  # |= accumulates multiple boolean conditions.
        # check if time out (termination) conditions are met
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
        # accumulate total reward within an episode
        self.total_reward += total_reward * self.dt
        # accumulate fitness score within an episode
        # accumulated_fitness = Σ(instantaneous_fitness * dt) for X steps
        # accumulated_fitness = Σ(instantaneous_fitness * 0.02) for 1000 steps
        self.fitness_score += task_fitness_score * self.dt
        # accumulate reward by component within an episode
        for name, rew in reward_components.items():
            if name not in self.episode_sums.keys():
                self.episode_sums[name] = torch.zeros(
                    (self.num_envs,), device=gs.device, dtype=gs.tc_float
                )
            # Always accumulate rewards
            self.episode_sums[name] += rew * self.dt
            self.extras["component_scale"][name] = reward_scales[f"{name}"]

        # 5. Compute observations
        self.obs_buf = torch.cat(
            [
                self.base_ang_vel * self.obs_scales["ang_vel"],  # 3 (angular velocity)
                self.projected_gravity,  # 3 (gravity vector)
                self.commands * self.commands_scale,  # 3 (velocity commands)
                (self.dof_pos - self.default_dof_pos)
                * self.obs_scales["dof_pos"],  # 12 (joint positions)
                self.dof_vel * self.obs_scales["dof_vel"],  # 12 (joint velocities)
                self.actions,  # 12 (joint actions)
            ],
            axis=-1,
        )
        self.last_actions[:] = self.actions[:]
        self.last_dof_vel[:] = self.dof_vel[:]

        self.extras["observations"]["critic"] = self.obs_buf

        # Reset episodes_completed_this_step for next step (if no resets occurred)
        if not torch.any(self.reset_buf):
            self.extras["episodes_completed_this_step"] = 0

        return self.obs_buf, self.rew_buf, self.reset_buf, self.extras

    def get_observations(self):
        self.extras["observations"]["critic"] = self.obs_buf
        return self.obs_buf, self.extras

    def get_privileged_observations(self):
        return None

    def reset_idx(self, envs_idx):
        # only reset the specified environments (envs_idx)
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
        self.base_pos[envs_idx] = self.base_init_pos  # [0, 0, 0.42]
        self.base_quat[envs_idx] = self.base_init_quat.reshape(
            1, -1
        )  #  [1, 0, 0, 0] (upright)
        self.robot.set_pos(
            self.base_pos[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )
        self.robot.set_quat(
            self.base_quat[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )

        # Reset velocities to zero
        self.base_lin_vel[envs_idx] = 0
        self.base_ang_vel[envs_idx] = 0
        self.robot.zero_all_dofs_velocity(envs_idx)

        # Reset episode counters
        self.episode_length_buf[envs_idx] = 0
        self.reset_buf[envs_idx] = True
        self.last_actions[envs_idx] = 0.0
        self.last_dof_vel[envs_idx] = 0.0

        # Resample new velocity commands
        if len(envs_idx) > 0:
            self._resample_commands(envs_idx)

    def reset(self):
        """
        The reset_idx function resets the initial pose and state buffers of the specified environments.
        This ensures robots start from predefined configurations, crucial for consistent training.
        """
        self.reset_buf[:] = True
        self.reset_idx(torch.arange(self.num_envs, device=gs.device))
        return self.obs_buf, None

    def _task_fitness_function(self):
        """task_fitness_function
        for Go2WakingCommand task fitness is defined as matching the linear velocity commands in xy axes
        """
        # Tracking of linear velocity commands (x axes)
        lin_vel_temperature = 4  # temperature
        lin_vel_error = torch.sum(
            torch.square(self.commands[:, :1] - self.base_lin_vel[:, :1]), dim=1
        )
        fitness_score = torch.exp(-lin_vel_error * lin_vel_temperature)
        return fitness_score

    def extract_reward_function(self, rwd_func_path):
        """Extract and optionally save reward function from JSON response.

        Args:
            rwd_func_path: Optional path to save the extracted code

        Returns:
            The compute_reward function

        Raises:
            ValueError: If extraction fails or compute_reward function is not found
        """
        # Extract the Python code
        from domains.genesis.genesis_utils import file_to_string

        code_str = file_to_string(rwd_func_path)

        # Execute the code to get the function
        reward_ns = {}
        exec(code_str, reward_ns, reward_ns)

        # Validate and return the function
        func = reward_ns.get("compute_reward")
        if not callable(func):
            raise ValueError(
                "Extracted code must define a function named 'compute_reward'"
            )

        return func
