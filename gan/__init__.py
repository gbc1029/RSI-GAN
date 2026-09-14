"""GAN-style three-role self-evolution framework on top of HyperAgents (DGM-H).

Package layout:
    gan.config    - config loader + yaml registries + prompts
    gan.reward    - RewardPacket + evaluator reward (2x2 matrix + calibration)
    gan.tree      - version trees (task / planner / evaluator)
    gan.access    - source-access gating (on-demand code transfer)
    gan.roles     - planner / evaluator roles
    gan.operators - planner operators & evaluator eval points
    gan.loop      - dual-loop scheduler
"""

__all__ = ["config", "reward", "tree"]
