
# Set up

## Install Pytorch
First, check CUDA version and GPU information using nvidia-smi:

```bash
nvidia-smi
```
Then following the official guide [here](https://pytorch.org/get-started/previous-versions/) to install the correct Pytorch version


## Install Genesis
```bash
with-proxy pip install --upgrade pip
with-proxy pip install git+https://github.com/Genesis-Embodied-AI/Genesis.git
with-proxy pip install rsl-rl-lib==2.2.4 tensorboard==2.20.0
```


# Evaluate the initial agent:
```bash
# harness
python -m domains.harness --domain genesis_go2walking --run_id initial_genesis_go2walking_0 --num_samples 1 --num_workers 1
# report
python -m domains.report --domain genesis_go2walking --dname ./outputs/initial_genesis_go2walking_0
```

By default, outputs will be saved in outputs/ directory. The structure of the output structure is as follows:


*   **Root level**: `eval.log` - evaluation log file
*   **go2walking/Go2WalkingCommand-v0/**: Main experiment directory containing:
    *   **Chat history**: Markdown file with task agent conversation logs
    *   **rl_eval_{episode_idx}/**: RL Evaluation results including:
        *   JSON log file
        *   MP4 video file (`eval_100.mp4`) - video of the agent executing the task after training for 100 steps
    *   **rl_train_{episode_idx}/**: RL Training artifacts including:
        *   Configuration files (pickle)
        *   TensorFlow events file for logging
        *   Model checkpoints (`model_0.pt`, `model_100.pt`)
        *   JSON training log

# Report

## Fitness Score Evaluation

The fitness scores are reported after evaluation with the following configuration:

### Evaluation Setup

- **Total simulation time:** 20 seconds
- **Episode duration:** 4.0 seconds (200 steps at dt = 0.02 seconds)
- **Speed command:** Randomly sampled for each episode
- **Fitness score range:** 0 to 1 (1 = best, 0 = worst)
- **Parallel environments:** 4096 environments evaluated simultaneously and asynchronously

### Expected Behavior

Under normal conditions, each environment completes exactly 5 episodes in 20 seconds (20s ÷ 4s = 5 episodes), resulting in 5 data points per environment.

### Early Episode Termination

Episodes can end prematurely if the robot falls (roll or pitch > 10 degrees). When this occurs, a new episode starts immediately. This is why you may observe more than 5 data points per environment.