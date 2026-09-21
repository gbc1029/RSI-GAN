"""GAN-style three-role self-evolution framework on top of HyperAgents (DGM-H).

Layout:
    gan.framework   - FROZEN runtime (trust anchor):
                        loader.py / loop.yaml / domains.yaml / frozen.py
                        task_execution.py   (design persistence + harness glue)
                        task_runner.py      (thin domain task runner)
                        checkpoint.py       (outer-loop checkpointing)
                        context.py          (contextvars)
                        access.py           (source-access gate)
                        loop.py             (dual-loop scheduler)
                        tree/               (version DAG)
                        reward/             (RewardPacket + text feedback digest)
    gan.tools       - always-on callables: work/ + design/ (operators) + deep/ (gate)
    gan.components  - opt-in component implementations (skills/eval_points)
    gan.registries  - per-role component catalog (json) + loader
    gan.design      - shallow evolvable design data (schema/store/seeds)
    gan.roles       - planner / evaluator roles
    gan.build       - factory assembling a runnable GanLoop
    gan.summary     - sanitized diff summary + feedback schema
"""

__all__ = ["framework", "tools", "components", "registries", "design", "roles"]
