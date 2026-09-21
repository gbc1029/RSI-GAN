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

- `gan/framework/` — **frozen** (trust anchor): `loop.yaml` (hyperparams),
  `domains.yaml` (incl. per-domain `output_contract`), `models.yaml` + `models.py`
  (unified model config/resolution), `loader.py`, `frozen.py` (single source of
  the **per-role access allowlist**), `paths.py` (canonical output layout),
  `workspace.py` (edit/read confinement), `task_execution.py` (design persistence
  + harness glue + minimal run-dir copy), `task_runner.py`, `checkpoint.py`,
  `scores.py`, `trajectory.py`, `preflight.py`, `context.py`, `access.py`,
  `loop.py`, `tree/`, `reward/`.
- `gan/tools/` — **always-on** callables: `work/<role>/` (role behavior tools),
  `work/common/` (frozen plumbing: `edit_source`, `read_file`, `list_dir`, `grep`,
  `read_trajectory`, `read_session_trajectory`, `list_editable`), `design/` (shallow design
  operators), `deep/` (gated deep gate: `request_source_access` + `unregister_component`),
  `assembly.py`.
- `gan/components/` — **opt-in** implementations: `shared/{skills,memory}/`,
  `task/skills/`, `evaluator/eval_points/`.
- `gan/registries/` — component catalog: `shared.json` + `<role>.json` + `loader.py`.
- `gan/design/` — **shallow, evolvable**: `schema.py`, `store.py`, `composer.py`, `seeds/`.
- `gan/roles/`, `gan/build.py`, `gan/summary.py` — evolvable orchestration/glue.

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

# original HyperAgents domain eval (model is explicit; resolved from models.yaml)
python -m domains.harness --domain paper_review --model openai/gpt-4o \
  --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo
```

Tests / verification scripts are kept locally under `scripts/local/` (gitignored).

## Conventions

- **Shallow vs deep**: each agent's *shallow design* is a single config JSON in
  `gan/design/` (selection from per-role registries). Shallow operators live in
  `gan/tools/design/` and may only set **existing** keys or select/deselect
  **registered** components. Tool add/delete/modify is classified by what it
  touches: **shallow** = edit the design config list only (`select_component` /
  `deselect_component`); **deep delete** = remove from the registry + source file;
  **deep add/modify** = new component or logic, which **must** touch source via the
  gated `gan/tools/deep/request_source_access`. There is no `mint_operator`. After a
  `modify` grant, planner/evaluator edit the granted copies with `edit_source`
  (`gan/tools/work/common/`, confined to the instance workspace `src/`); roles are
  intentionally NOT given raw `bash` (it cannot be confined).
- **Instances**: an *instance* = role + workspace + its trajectory. The task agent
  is refreshed every inner generation (per `genid`); planner/evaluator are refreshed
  every **outer** generation (`gan/framework/loop.py:_refresh_roles` + the
  `*_factory` args from `gan/build.py`). Each instance gets `toolsets/<role>/<inst>/`
  and `trajectory/outer_<O>/...jsonl`; tooling is re-assembled **with `clear=True`**
  so deselected components actually disappear. The source workspace is keyed by the
  role **instance** (per outer): `workspaces/<role>/outer_<O>/src`, shared by that
  outer's plan/evaluate/self_improve, and cleared at each outer start.
- **Scheduling is single-chain**: no accept/reject and no parent selection for
  planner/evaluator. Each outer saves a checkpoint; the next outer starts from the
  previous checkpoint's design. `outer_improved` is logged, not enforced.
- **Output layout** (`gan/framework/paths.py`, the single source): `ckpt/`
  (recovery + `design/`), `logs/` (`events.jsonl`, `*_tree.jsonl`, `*.log`),
  `trajectory/outer_<O>/{planner,evaluator,<genid>/{task,planner,evaluator}}.jsonl`,
  `scores/scores.jsonl`, `runs/<genid>/` (small evidence), `work/<genid>/`
  (ephemeral, pruned), `workspaces/<role>/outer_<O>/`. `checkpoint.json` is the
  latest; outer boundaries also write an immutable `ckpt/outer_<N>.json` (atomic);
  `restore_trees` appends an `op=reset` event instead of truncating the log.
- **Trajectory access**: every agent writes structured **JSONL** (one file per
  instance) via the shared `utils/trajectory_log.py`; GAN's
  `gan/framework/trajectory.py` (frozen; redaction is part of the trust anchor)
  archives + redacts task trajectories and also redacts role sessions
  (`Role.run`). The visible set is explicit: `AccessContext.trajectory_genids`
  (evaluator = current + parent; planner = parent; self-improvement = this
  outer's generations + the direct parent). `read_trajectory` reads visible task
  generations; `read_session_trajectory` reads the role's own sessions this outer.
  The current generation's task trajectory is archived **before** evaluation.
- **Task runs are isolated from `repo_root`**: each generation runs in a
  **minimal allowlist copy** `work/<genid>/repo` (agent runtime + domain package,
  datasets excluded; patched inside the copy when a deep patch exists). Benchmark
  labels are read by the harness from `GAN_DATASET_ROOT` (the real repo), outside
  the copy.
- **Activation rule**: `always-on → gan/tools/`, `opt-in → gan/components/`
  (registered in `gan/registries/` and selected by the design config). Plumbing
  tools (`deep/`, `design/`, `work/common/`) are frozen; only `work/<role>/` and
  the task components/registry are evolvable.
- **Access boundary**: `gan/framework/frozen.py` declares a **per-role allowlist**
  (read/write) — `task`: none; `planner`: read+write t-set ∪ p-set; `evaluator`:
  read t-set (read-only) + read/write e-set. Everything else is frozen by default;
  there is no persisted deny list. `AccessBroker` enforces it and refuses the repo
  root / self-containing / oversized grants. A local-only layout check lives at
  `scripts/local/test_frozen.py` (gitignored).
- **Code repo / driver (default)**: `scripts/run_gan.py` materializes a per-run git
  tree `ckpt/code` (`gan/framework/code_repo.py`) and runs one `gan.outer_worker`
  subprocess per outer (`gan/driver.py`; cwd + `PYTHONPATH = code_root`), so role
  self-edits take effect for the **next outer**. Task (t) deep patches apply to the
  code tree (validated + committed); role self-patches are likewise validated +
  committed **by the worker itself** (option B, v4.22: `loop._apply_self_patch` →
  `code_repo.apply_self_patch`, allowlist + compile, rolled back on failure; the
  durable record is the code commit, events `self_improve_commit` /
  `self_improve_apply_failed`). Use `--in-process` for the legacy single-process
  loop. Access grants/diffs are against `code_root`; benchmark labels still come
  from `repo_root` via `GAN_DATASET_ROOT`.
- **Task brief**: each domain in `gan/framework/domains.yaml` has a `task_brief`
  (human-readable task + answer interface). It is **framework-injected** into the
  planner / task / evaluator prompts (via `GAN_TASK_BRIEF` for the task agent and
  the `task_brief=` argument for roles) and is **NOT** part of the evolvable design
  (marked *可解冻* for a future axis). It must never include grading mechanics
  (case-insensitive / exact-match / no partial credit) or ground-truth labels/scores.
  The evaluator's blind-phase `run_summary` deliberately excludes the objective
  score/accuracy (revealed only via `benchmark_score`).
- **Sandbox / bash (deferred)**: planner/evaluator are NOT given raw `bash`; they
  use the frozen, workspace-confined `edit_source` / `read_file` / `list_dir` /
  `grep`. A real sandbox (bubblewrap / container) is deferred until roles execute
  self-written code or adversarial leakage must be ruled out (see v4.14).
- **Model config**: the single source is `gan/framework/models.yaml`, read only by
  `gan/framework/models.py` (`resolve/resolve_section/describe`) — a **pure lookup**
  with NO env, NO fallback and NO precedence chain. Sections: `gan.{task,planner,evaluator}`,
  `dgmh.{meta,task}`, `domains.<role>` (non-task domain roles only, e.g. the polyglot
  aider CLI). The **domain task agent shares the driver's task model** and receives it
  at runtime via an explicit `domains.harness --model ...` argument (never env). The
  effective model is recorded at runtime (`events.jsonl: model_config` for GAN;
  `[model_config]` log line for DGM-H; `Node.meta["model"]` per generation). Do not
  read `models.yaml` anywhere else.
- `gan/framework/loop.yaml` is framework hyperparameters — **not** evolvable. Task
  trajectory visibility is NOT configured here: the loop passes an explicit
  `AccessContext.trajectory_genids` set per session (v4.20) — planner = direct
  parent; evaluator = current + parent; self-improvement = this outer's
  generations + the last generation's direct parent.
- **Work tools vs design operators**: evaluator scoring and planner
  `respond_issue` are work tools (`gan/tools/work/`); design operators are in
  `gan/tools/design/`.
- Operators are tools: a module exposing `tool_info()` + `tool_function(**kwargs)`.
- Session state travels via contextvars (`gan/framework/context.py`), not function arguments.
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

- Deep design + code locations: `docs/3_GAN深入设计说明.md`
- Design/plan: `docs/2_GAN_plan.md`
- Implementation record: `docs/4_v1实现和v2改动.md`
- Modification decision record (v3/v4): `docs/5_v3改动.md`
- Environment/deployment record: `docs/1_DGMH部署记录.md` and `README.md`

## Base license

HyperAgents base code is under `LICENSE.md` (CC BY-NC-SA 4.0); keep attribution.
