import argparse
import os
import sys

# Add the project root to Python path FIRST so we can import gpu_selector
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))

# GPU Selection MUST happen before ANY CUDA imports (including torch and genesis)
from domains.genesis.gpu_selector import set_cuda_visible_devices

set_cuda_visible_devices(strategy="underutilized")

# NOW it's safe to import torch, genesis, and other CUDA libraries
import pickle
import shutil
import time
from ast import parse
from datetime import datetime
from importlib import metadata

import torch
import hydra
from domains.genesis.environments.genesis_env import make_genesis_env
from omegaconf import DictConfig

#  RL  Library: rsl-rl-lib (version 2.2.4 specifically required)
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

from rsl_rl.runners import OnPolicyRunner


def get_train_cfg(exp_name, max_iterations):

    #  RL Algorithm: PPO
    train_cfg_dict = {
        "algorithm": {
            "class_name": "PPO",
            "clip_param": 0.2,
            "desired_kl": 0.01,
            "entropy_coef": 0.01,
            "gamma": 0.99,
            "lam": 0.95,
            "learning_rate": 0.001,
            "max_grad_norm": 1.0,
            "num_learning_epochs": 5,
            "num_mini_batches": 4,
            "schedule": "adaptive",
            "use_clipped_value_loss": True,
            "value_loss_coef": 1.0,
        },
        "init_member_classes": {},
        "policy": {
            "activation": "elu",
            "actor_hidden_dims": [512, 256, 128],
            "critic_hidden_dims": [512, 256, 128],
            "init_noise_std": 1.0,
            "class_name": "ActorCritic",
        },
        "runner": {
            "checkpoint": -1,
            "experiment_name": exp_name,
            "load_run": -1,
            "log_interval": 1,
            "max_iterations": max_iterations,
            "record_interval": -1,
            "resume": False,
            "resume_path": None,
            "run_name": "",
        },
        "runner_class_name": "OnPolicyRunner",
        "num_steps_per_env": 24,  # Each simulated robot collects exactly 24 time steps ofexperience before the policy gets updated.
        "save_interval": 100,
        "empirical_normalization": None,
        "seed": 1,
    }

    return train_cfg_dict


def RLTrainer():
    """
    Run RL training
    Each iteration involves:

    1.  **Rollout phase**: Collect `num_steps_per_env` (24) steps from each environment
    2.  **Learning phase**: Update the policy using the collected data
    """

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-e", "--exp_name", type=str, default="Go2WalkingCommand-v0/speed_run_00"
    )
    parser.add_argument(
        "-B", "--num_envs", type=int, default=4096
    )  # 4096 identical Go2 robots are simulated simultaneously onthe GPU, each learning the same walking task independently.
    parser.add_argument("--max_iterations", type=int, default=101)
    parser.add_argument("--output_dir", type=str, default="outputs/genesis/train")
    parser.add_argument("--lin_vel_x_range", nargs=2, type=float, default=[0.5, 0.5])
    parser.add_argument("--hop_height_range", nargs=2, type=float, default=None)
    parser.add_argument("--rwd_func_path", type=str, default="")
    parser.add_argument("--episode_idx", type=str, default=None)
    args = parser.parse_args()

    task = args.exp_name.split("_")[0]

    # Initialize genesis (GPU already selected via CUDA_VISIBLE_DEVICES at module import)
    # explicitly use CUDA backend --> will bypass the Vulkan detection and use NVIDIA GPU directly via CUDA.
    try:
        gs.init(backend=gs.cuda, logging_level="warning")
        print(f"Genesis initialized with CUDA backend on selected GPU")
    except Exception as e:
        print(f"CUDA initialization failed: {e}")
        print("Falling back to CPU backend")
        gs.init(backend=gs.cpu, logging_level="warning")

    log_dir = f"{args.output_dir}/genesis_train" if args.episode_idx is None else f"{args.output_dir}/genesis_train_{args.episode_idx}"
    train_cfg = get_train_cfg(args.exp_name, args.max_iterations)

    if os.path.exists(log_dir):
        shutil.rmtree(log_dir)
    os.makedirs(log_dir, exist_ok=True)

    # Log training session start
    env = make_genesis_env(
        task,
        args.num_envs,
        args.lin_vel_x_range,
        add_camera=False,
        rwd_func_path=args.rwd_func_path,
        hop_height_range=args.hop_height_range,
    )
    env_cfg, obs_cfg, command_cfg = env.env_cfg, env.obs_cfg, env.command_cfg
    pickle.dump(
        [env_cfg, obs_cfg, command_cfg, train_cfg],
        open(f"{log_dir}/cfgs.pkl", "wb"),
    )
    runner = OnPolicyRunner(env, train_cfg, log_dir, device=gs.device)

    # Start timing the training
    train_start_time = time.time()
    print(f"Starting training at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Training {args.num_envs} environments for {args.max_iterations} iterations")

    runner.learn(
        num_learning_iterations=args.max_iterations, init_at_random_ep_len=True
    )

    # End timing and log results
    train_end_time = time.time()
    print(f"Training completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"Total training time: {train_end_time - train_start_time:.2f} seconds ({(train_end_time - train_start_time)/60:.2f} minutes)"
    )


if __name__ == "__main__":
    RLTrainer()
    """
    # Run RL training
    python -m domains.genesis.genesis_train.rl_trainer -e Go2WalkingCommand-v0/speed_run_00 --num_envs 4096 --max_iterations 101 --output_dir outputs/initial_genesis_go2walking_0/go2walking/Go2WalkingCommand-v0/speed --lin_vel_x_range 0.1 1
    """
