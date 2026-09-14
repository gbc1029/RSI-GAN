"""
GPU selection utility that doesn't initialize CUDA.

This module must NOT import torch, genesis, or any CUDA libraries.
It uses nvidia-smi to query GPU information without initializing CUDA.
"""
import os
import subprocess
import random


def select_gpu(strategy="underutilized"):
    """Select GPU before any CUDA initialization using nvidia-smi.

    Args:
        strategy (str): Either "random" for random selection or "underutilized"
                       for selecting the GPU with the most free memory.

    Returns:
        int: GPU device ID to use, or None if no GPUs available
    """
    try:
        # Use nvidia-smi to get GPU info without initializing CUDA
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.free,memory.total', '--format=csv,noheader,nounits'],
            capture_output=True,
            text=True,
            check=True
        )

        gpu_info = []
        for line in result.stdout.strip().split('\n'):
            idx, free, total = line.split(', ')
            gpu_info.append({
                'index': int(idx),
                'free': float(free),
                'total': float(total)
            })

        if len(gpu_info) == 0:
            print("No CUDA GPUs found, will use CPU")
            return None

        if len(gpu_info) == 1:
            print(f"Only 1 GPU available, using GPU 0")
            return 0

        print(f"Checking utilization of {len(gpu_info)} GPUs...")
        for gpu in gpu_info:
            used = gpu['total'] - gpu['free']
            utilization_pct = (used / gpu['total']) * 100
            print(f"  GPU {gpu['index']}: {gpu['free'] / 1024:.2f}GB free / {gpu['total'] / 1024:.2f}GB total ({utilization_pct:.1f}% used)")

        if strategy == "random":
            gpu_id = random.randint(0, len(gpu_info) - 1)
            print(f"Randomly selected GPU {gpu_id} out of {len(gpu_info)} available GPUs")
            return gpu_id

        elif strategy == "underutilized":
            # Find GPU with most free memory
            best_gpu = max(gpu_info, key=lambda x: x['free'])
            print(f"Selected GPU {best_gpu['index']} with {best_gpu['free'] / 1024:.2f}GB free memory")
            return best_gpu['index']

        else:
            raise ValueError(f"Unknown strategy '{strategy}'. Use 'random' or 'underutilized'")

    except (subprocess.CalledProcessError, FileNotFoundError):
        print("nvidia-smi not available, will use default GPU")
        return None


def set_cuda_visible_devices(strategy="underutilized"):
    """Select GPU and set CUDA_VISIBLE_DEVICES environment variable.

    MUST be called before importing any CUDA libraries (torch, genesis, etc).

    Args:
        strategy (str): GPU selection strategy ("random" or "underutilized")
    """
    selected_gpu = select_gpu(strategy=strategy)
    if selected_gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(selected_gpu)
        print(f"Set CUDA_VISIBLE_DEVICES={selected_gpu}")
    else:
        print("No GPU selected, using default")
