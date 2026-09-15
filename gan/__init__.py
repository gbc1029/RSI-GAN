"""GAN-style three-role self-evolution framework on top of HyperAgents (DGM-H).

Layout:
    gan.framework  - frozen hyperparameters + domain registry + loaders
    gan.context    - all contextvars (plan/eval/design/access)
    gan.tools      - always-on callables: work/ + design/ (operators) + deep/ (gate)
    gan.components - opt-in component implementations (skills/memory/eval_points)
    gan.registries - per-role component catalog (json) + loader
    gan.design     - shallow evolvable design data (schema/store/seeds/composer)
    gan.roles      - planner / evaluator roles
    gan.tree       - version DAG
    gan.reward     - RewardPacket + text feedback digest
    gan.loop       - dual-loop scheduler
"""

__all__ = ["framework", "context", "tools", "components", "registries", "design", "roles", "tree", "reward"]
