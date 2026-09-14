import argparse
import json
import os
import sys

# Add the project root to Python path FIRST so we can import gpu_selector
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))

# GPU Selection MUST happen before ANY CUDA imports (including torch and genesis)
from domains.genesis.gpu_selector import set_cuda_visible_devices

set_cuda_visible_devices(strategy="underutilized")

# NOW it's safe to import torch, genesis, and other CUDA libraries
import pickle
import platform
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path

import numpy as np
import torch

from domains.genesis.environments.genesis_env import make_genesis_env

try:
    try:
        if metadata.version("rsl-rl"):
            raise ImportError
    except metadata.PackageNotFoundError:
        if metadata.version("rsl-rl-lib") != "2.2.4":
            raise ImportError
except (metadata.PackageNotFoundError, ImportError) as e:
    raise ImportError(
        "Please uninstall 'rsl_rl' and install 'rsl-rl-lib==2.2.4'."
    ) from e
import genesis as gs

from domains.genesis.environments.Go2WalkingCommand import Go2Env
from rsl_rl.runners import OnPolicyRunner


def track_evaluation_metrics(env, rewards, dones, infos, step_count, eval_stats):
    """Track detailed evaluation metrics during the episode"""

    # Initialize tracking on first call
    if "episode_total_rewards" not in eval_stats:
        eval_stats.update(
            {
                "fitness_score": [],  # average fitness score across all environments for completed episodes
                "total_reward": [],  # average total reward across all environments for completed episodes
                "reward_component": {},  # average reward across all environments for completed episodes for each reward component
                "speed_command": {},
                "num_envs": infos["num_envs"],
                "total_episodes_played": 0,  # total number of episodes played across all environments
            }
        )

    # Check if episode completion information is available in infos
    for key, value in infos["all_episodes"]["reward_component"].items():
        eval_stats["reward_component"][f"{key}"] = value

    eval_stats["fitness_score"] = infos["all_episodes"]["fitness_score"]
    eval_stats["total_reward"] = infos["all_episodes"]["total_reward"]
    eval_stats["total_episodes_played"] = infos["total_episodes_played"]
    eval_stats["speed_command"] = infos["all_episodes"]["speed_command"]

    # Handle episode completion (check every step)
    # Handle dones which could be a tensor or boolean
    episode_done = False
    if hasattr(dones, "__len__") and len(dones) > 0:
        episode_done = bool(dones[0])
    elif hasattr(dones, "item"):
        episode_done = bool(dones.item())
    elif isinstance(dones, bool):
        episode_done = dones
    eval_stats["episode_done"] = episode_done

    return eval_stats


def finalize_evaluation_stats(exp_name, eval_stats, eval_dir):
    """Calculate final statistics after evaluation"""
    episode_idx = exp_name.split("_")[-1]
    task = exp_name.split("-")[0]

    # Convert numpy types to Python native types for JSON serialization
    json_serializable_stats = {}
    for key, value in eval_stats.items():
        if isinstance(value, (np.integer, np.floating)):
            json_serializable_stats[key] = value.item()
        elif isinstance(value, np.ndarray):
            json_serializable_stats[key] = value.tolist()
        elif isinstance(value, list):
            # Handle lists that might contain numpy types
            json_serializable_stats[key] = [
                item.item() if isinstance(item, (np.integer, np.floating)) else item
                for item in value
            ]
        else:
            json_serializable_stats[key] = value

    # Save stats to JSON file with the specified naming format
    try:
        episode_idx_int = int(episode_idx)
        json_filename = f"{task}_run_{episode_idx_int:02d}.json"
    except (ValueError, TypeError):
        # Fallback if episode_idx is not a valid integer
        json_filename = f"{task}_run_00.json"

    json_filepath = os.path.join(eval_dir, json_filename)

    with open(json_filepath, "w") as f:
        json.dump(json_serializable_stats, f, indent=2)

    print(f"Evaluation stats saved to: {json_filepath}")


def RLEval():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-e", "--exp_name", type=str, default="Go2WalkingCommand-v0/speed_run_00"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./outputs/initial_genesis_go2walking_0/go2walking/Go2WalkingCommand-v0/speed",
    )
    parser.add_argument("--ckpt", type=int, default=100)
    parser.add_argument(
        "--headless",
        default=True,
        help="Run in headless mode with camera recording",
    )
    parser.add_argument(
        "--record_video", action=argparse.BooleanOptionalAction, help="Record video during evaluation"  # able to use --no-record_video
    )
    parser.add_argument(
        "--video_filename",
        type=str,
        default="eval",
        help="Output video filename",
    )

    parser.add_argument(
        "--num_envs",
        type=int,
        default=4096,
        help="Number of environments to create",
    )

    parser.add_argument(
        "--max_steps",
        type=int,
        default=2000,
        help="Maximum number of steps to run the environment",
    )
    parser.add_argument("--video_fps", type=int, default=30, help="Video FPS")
    parser.add_argument("--rwd_func_path", type=str, default="")
    parser.add_argument("--fixed_camera", action="store_true", help="Keep camera fixed instead of following the robot")
    parser.add_argument("--lin_vel_x_range", nargs=2, type=float, default=[0.5, 0.5])
    parser.add_argument("--hop_height_range", nargs=2, type=float, default=None)
    parser.add_argument("--episode_idx", type=str, default=None)
    args = parser.parse_args()

    # ---------------------------------------------------------------
    # 1. Environment Setup
    # Initializes Genesis engine (GPU already selected via CUDA_VISIBLE_DEVICES at module import)
    # explicitly use CUDA backend --> will bypass the Vulkan detection and use NVIDIA GPU directly via CUDA.
    try:
        gs.init(backend=gs.cuda, logging_level="warning")
        print(f"Genesis initialized with CUDA backend on selected GPU")
    except Exception as e:
        print(f"CUDA initialization failed: {e}")
        print("Falling back to CPU backend")
        gs.init(backend=gs.cpu, logging_level="warning")

    eval_dir = f"{args.output_dir}/genesis_eval" if args.episode_idx is None else f"{args.output_dir}/genesis_eval_{args.episode_idx}"
    train_dir = f"{args.output_dir}/genesis_train" if args.episode_idx is None else f"{args.output_dir}/genesis_train_{args.episode_idx}"
    config_path = f"{train_dir}/cfgs.pkl"
    exp_name = args.exp_name
    task = exp_name.split("_")[0]

    if not os.path.exists(eval_dir):
        os.makedirs(eval_dir, exist_ok=False)
    env_cfg, obs_cfg, command_cfg, train_cfg = pickle.load(open(config_path, "rb"))

    # Load reward configuration separately since it's not saved in the pickle file

    # Creates Go2 environment with a single environment instance
    # Use headless mode if specified, otherwise show viewer
    if args.record_video:
        add_camera = True
    else:
        add_camera = False

    env = make_genesis_env(
        task,
        args.num_envs,
        lin_vel_x_range=args.lin_vel_x_range,
        add_camera=add_camera,
        video_filename=f"{args.video_filename}_{args.ckpt}",
        eval_dir=eval_dir,
        video_fps=args.video_fps,
        rwd_func_path=args.rwd_func_path,
        hop_height_range=args.hop_height_range,
        fixed_camera=args.fixed_camera,
    )

    # ---------------------------------------------------------------

    # 2.  Model Loading
    # create an OnPolicyRunner instance

    runner = OnPolicyRunner(env, train_cfg, eval_dir, device=gs.device)
    # load the trained policy from the specified checkpoint file.
    resume_path = os.path.join(train_dir, f"model_{args.ckpt}.pt")
    runner.load(resume_path)
    #  Gets inference policy
    policy = runner.get_inference_policy(device=gs.device)

    # ---------------------------------------------------------------

    # 3. Evaluation Loop
    # Collect system and model information for comprehensive logging

    # Initialize evaluation statistics tracking
    eval_stats = {}

    # Start timing the evaluation
    eval_start_time = time.time()
    print(f"Starting evaluation at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Resets environment
    obs, _ = env.reset()

    step_count = 0
    max_steps = args.max_steps

    with torch.no_grad():
        while step_count <= max_steps:
            actions = policy(obs)
            obs, rews, dones, infos = env.step(actions, step_count=step_count)
            step_count += 1

            # Print progress every 100 steps
            if step_count % 100 == 0:
                print(f"Evaluation step: {step_count}/{max_steps}")
                if eval_stats.get("total_episodes_played", 0) > 0:
                    print(
                        f"  Episodes completed: {eval_stats['total_episodes_played']}"
                    )
                if len(eval_stats.get("fitness_score", [])) > 0:
                    avg_fitness = sum(eval_stats["fitness_score"]) / len(
                        eval_stats["fitness_score"]
                    )
                    print(f"  Average fitness score: {avg_fitness:.3f}")
                if len(eval_stats.get("total_reward", [])) > 0:
                    avg_reward = sum(eval_stats["total_reward"]) / len(
                        eval_stats["total_reward"]
                    )
                    print(f"  Average total reward: {avg_reward:.3f}")

    eval_stats = track_evaluation_metrics(env, rews, dones, infos, step_count, eval_stats)
    # Finalize and save evaluation statistics
    finalize_evaluation_stats(exp_name, eval_stats, eval_dir)

    # End timing and log results
    eval_end_time = time.time()
    print(f"Evaluation completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total evaluation time: {eval_end_time - eval_start_time:.2f} seconds")


if __name__ == "__main__":
    RLEval()
