# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

**RSI-GAN** — a GAN-inspired three-role self-evolution framework built on top of
**HyperAgents (DGM-H)**:

- `task_agent.py` — the task agent (the artifact being improved).
- `gan/roles/planner.py` — plans improvements to the task agent; self-improves.
- `gan/roles/evaluator.py` — judges process/result (finds benchmark-invisible
  problems); self-improves.

New framework code lives in `gan/`. The rest (`agent/`, `domains/`, `utils/`,
`task_agent.py`, `meta_agent.py`, `generate_loop.py`, `Dockerfile`) is the
HyperAgents base, reused with minimal adaptations.

## Setup

```bash
python3.12 -m venv venv_nat && source venv_nat/bin/activate
pip install -r requirements.txt
pip install -r requirements_dev.txt
```

Create `.env` (never commit it) with an OpenAI-compatible endpoint:

```ini
OPENAI_API_BASE=https://<endpoint>/v1
OPENAI_API_KEY=<key>
```

## Run

```bash
# GAN dual-loop (paper_review minimal)
python scripts/run_gan.py --task-domain paper_review --subset _filtered_100_train \
  --num_samples 2 --outer 1 --inner 2 --output_dir outputs/gan_paper_review

# original HyperAgents domain eval
python -m domains.harness --domain paper_review --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo
```

Tests / verification scripts are kept locally under `scripts/local/` (gitignored).

## Conventions

- Config over code: prefer `gan/config/*.yaml` (+ `Config.add_custom`) and
  operators over editing source. Source edits require the gated path
  (`request_source_access` / `code_edit`).
- Operators are tools: a module under `gan/operators/{planner_ops,evaluator_ops}`
  exposing `tool_info()` + `tool_function(**kwargs)`.
- Session state travels via contextvars (`gan/operators/context.py`:
  `PlanContext` / `EvalContext`), not function arguments.
- Do not expose planner rationale/reason to the evaluator (use
  `gan/summary.py:build_diff_summary`).
- Keep the evaluator honest: it must not fit the benchmark score (blind score
  first, then reveal).

## Do NOT

- Do not commit `.env`, secrets, `venv_nat/`, `outputs/`, or large datasets
  (see `.gitignore`).
- Do not upload local-only test scripts (`scripts/local/`).
- Do not commit model-generated code from runs without review.

## Where things are

- Deep design + code locations: `docs/深入设计说明.md`
- Design/plan: `docs/plan.md`
- Implementation record (files/problems/fixes): `docs/GAN实现记录.md`
- Environment/deployment record: `docs/部署记录.md` and `README.md`

## Base license

HyperAgents base code is under `LICENSE.md` (CC BY-NC-SA 4.0); keep attribution.
