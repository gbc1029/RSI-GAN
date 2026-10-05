"""GAN-style three-role self-evolution framework on top of HyperAgents (DGM-H).

Layout:
    gan.framework   - FROZEN runtime (trust anchor); the authoritative module
                        list is AGENTS.md ("gan/framework"): loader.py /
                        loop.yaml / domains.yaml / models.yaml + models.py /
                        frozen.py / paths.py / workspace.py / access.py /
                        context.py / task_execution.py / task_runner.py /
                        checkpoint.py / scores.py / trajectory.py /
                        preflight.py / code_repo.py / write_auth.py /
                        ast_checker.py / receipt.py / loop.py / tree/ / reward/
    gan.tools       - always-on callables: work/ + design/ (operators) + deep/ (gate)
    gan.components  - opt-in tool implementations, one tree per role
    gan.registries  - per-role tool catalog (one single-writer json per role) + loader
    gan.design      - shallow evolvable design data (schema/store/seeds)
    gan.roles       - planner / evaluator roles
    gan.build       - factory assembling a runnable GanLoop
    gan.summary     - sanitized diff summary + feedback schema
"""

__all__ = ["framework", "tools", "components", "registries", "design", "roles"]
