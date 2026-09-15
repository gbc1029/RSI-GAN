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
`task_agent.py`, `Dockerfile`) is the HyperAgents base, reused with minimal
adaptations. DGM-H-only entry scripts (`generate_loop.py`, `meta_agent.py`,
`run_meta_agent.py`, `run_task_agent.py`, `select_next_parent.py`, `ensemble.py`)
live under `scripts/dgmh/`.

## `gan/` layout (nature × mutability)

- `gan/framework/` — **frozen**: `loop.yaml` (hyperparams), `domains.yaml`, `loader.py`.
- `gan/context.py` — **frozen**: all contextvars (plan/eval/design/access).
- `gan/tools/` — **always-on** callables: `work/<role>/` (work tools),
  `design/` (shallow design operators), `deep/` (gated deep gate), `assembly.py`.
- `gan/components/` — **opt-in** implementations: `shared/{skills,memory}/`,
  `task/skills/`, `evaluator/eval_points/`.
- `gan/registries/` — component catalog: `shared.json` + `<role>.json` + `loader.py`.
- `gan/design/` — **shallow, evolvable**: `schema.py`, `store.py`, `composer.py`, `seeds/`.
- `gan/roles/`, `gan/tree/`, `gan/reward/`, `gan/loop.py`, `gan/build.py`, `gan/task_runner.py`, `gan/access.py`.

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

- **Shallow vs deep**: each agent's *shallow design* is a single config JSON in
  `gan/design/` (selection from per-role registries). Shallow operators live in
  `gan/tools/design/` and may only set **existing** keys or select **registered**
  components. Adding a new key / new component / new implementation is a **deep**
  change via the gated `gan/tools/deep/request_source_access`.
- **Activation rule**: `always-on → gan/tools/`, `opt-in → gan/components/`
  (registered in `gan/registries/` and selected by the design config).
- **Frozen substrate**: the shared runtime (`agent/llm.py`, `agent/llm_withtools.py`,
  `agent/base_agent.py`, `gan/framework/*`, `gan/context.py`) must NOT be modified
  during evolution (trust anchor / anti-hacking). Enforced via
  `gan/framework/loop.yaml: source_access.deny_paths`.
- `gan/framework/loop.yaml` is framework hyperparameters — **not** evolvable.
- **Work tools vs design operators**: evaluator scoring and planner
  `respond_issue` are work tools (`gan/tools/work/`); design operators are in
  `gan/tools/design/`.
- Operators are tools: a module exposing `tool_info()` + `tool_function(**kwargs)`.
- Session state travels via contextvars (`gan/context.py`), not function arguments.
- Do not expose planner rationale/reason to the evaluator (use
  `gan/summary.py:build_diff_summary`).
- Evaluator feedback is **text** (a digest), not a numeric reward; the evaluator
  must not fit the benchmark score (blind score first, then reveal).
- The task agent has **no always-on tools**; its capabilities are opt-in skills.

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
