# get all  envs
GENESIS_ENVS = []
GENESIS_ENVS += [
    "Genesis-Go2WalkingCommand/speed",
    "Genesis-Go2HoppingCommand/hop",
]
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))


def make_genesis_env(
    task,
    num_envs,
    lin_vel_x_range=[0.5, 0.5],
    update_reward_str=None,
    add_camera=False,
    video_filename=None,
    eval_dir=None,
    video_fps=30,
    rwd_func_path="",
    hop_height_range=None,
    fixed_camera=False,
):
    task_base, variation = task.split("/")

    if task.lower().startswith(("go2walkingcommand", "go2walkbackcommand")):
        from domains.genesis.environments.Go2WalkingCommand import Go2Env

        base_kwargs = {}
        if variation == "speed":
            base_kwargs["lin_vel_x_range"] = (
                lin_vel_x_range  #  forward speed instead of [0.5, 0.5]
            )

        # Add video recording parameters if provided
        if add_camera:
            base_kwargs["video_filename"] = video_filename
            base_kwargs["eval_dir"] = eval_dir
            base_kwargs["video_fps"] = video_fps
            base_kwargs["fixed_camera"] = fixed_camera

        env = Go2Env(
            num_envs=num_envs,
            add_camera=add_camera,
            show_viewer=False,
            update_reward_str=update_reward_str,
            rwd_func_path=rwd_func_path,
            **base_kwargs,
        )
        return env
    elif task.lower().startswith("go2hoppingcommand"):
        from domains.genesis.environments.Go2HoppingCommand import Go2HopEnv

        base_kwargs = {}
        if hop_height_range is not None:
            base_kwargs["hop_height_range"] = hop_height_range

        # Add video recording parameters if provided
        if add_camera:
            base_kwargs["video_filename"] = video_filename
            base_kwargs["eval_dir"] = eval_dir
            base_kwargs["video_fps"] = video_fps
            base_kwargs["fixed_camera"] = fixed_camera

        env = Go2HopEnv(
            num_envs=num_envs,
            add_camera=add_camera,
            show_viewer=False,
            rwd_func_path=rwd_func_path,
            **base_kwargs,
        )
        return env
    else:
        raise ValueError(f"Unsupported task: {task_base}")
