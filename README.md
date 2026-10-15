# RSI-GAN

A **GAN-inspired three-role self-evolution framework** built on top of
[HyperAgents (DGM-H)](https://github.com/facebookresearch/HyperAgents):

- **Task agent** — executes the downstream task (the "generated" artifact).
- **Planner** — improves the task agent (the "generator"); receives the
  evaluator's feedback and the benchmark score; also self-improves.
- **Evaluator** — judges the task agent's *process* and *result* (finds problems
  benchmark-only scoring cannot see: process quality, reward hacking, detail
  defects, overfitting); also self-improves.

The design and rationale are documented in:

- [`docs/整合_框架简述和细节决策记录.md`](docs/整合_框架简述和细节决策记录.md) — current-state framework + per-topic decision record (start here).
- [`docs/整合_待办记录.md`](docs/整合_待办记录.md) — open-todo ledger.
- [`docs/整合_已办留档.md`](docs/整合_已办留档.md) — done archive + item→batch closure map.
- Historical records (archived under [`docs/archive/`](docs/archive/)): deep design `3_GAN深入设计说明.md`, plan `2_GAN_plan.md`, implementation `4_v1实现和v2改动.md`, v3/v4 decision record `5_v3改动.md`, deployment `1_DGMH部署记录.md`.
- [`AGENTS.md`](AGENTS.md) — contributor/agent guide.

> This repository also contains the original HyperAgents (DGM-H) codebase, which
> the GAN framework reuses (archive/version trees, container harness, domain
> evaluation). The upstream license (`LICENSE.md`, CC BY-NC-SA 4.0) applies to
> that base code. This repo drops the upstream git history and keeps a single,
> clean initial commit.

## Repository layout

```
gan/                     # the GAN framework (new)
  framework/             #   frozen trust anchor: loop.yaml, domains.yaml, models.yaml(+models.py),
                         #   loader/frozen/paths/workspace/access/context/loop,
                         #   task_execution/task_runner/checkpoint/scores/trajectory/preflight,
                         #   code_repo/receipt, tree/, reward/
  tools/                 #   always-on: work/{planner,evaluator,common} + design/ (operators)
                         #   + deep/ (gated source access) + assembly.py
  components/            #   opt-in: shared/skills, task/skills, evaluator/eval_points
  registries/            #   component catalog (json) + loader
  design/                #   shallow evolvable design (schema/store/seeds)
  roles/                 #   planner / evaluator (Role base in base_role.py)
  build.py driver.py outer_worker.py patch.py summary.py
scripts/run_gan.py       # GAN dual-loop CLI entry (sandboxed driver only)
scripts/dgmh/            # DGM-H-only entry scripts (generate_loop/meta_agent/run_meta_agent/...)
scripts/local/           # local-only verification scripts (gitignored)
docs/                    # integrated docs (current) + archive/ (historical records)
agent/ domains/ utils/   # HyperAgents base code (reused)
task_agent.py            # the (design-driven) task agent
Dockerfile               # HyperAgents container image
```

## Environment deployment

Target: Linux or WSL2 (Ubuntu). Python 3.12. GPU optional (only needed for the
`genesis` domain / GPU container runs).

### 1) System dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
  python3.12-dev python3.12-venv \
  graphviz libgraphviz-dev cmake ninja-build build-essential \
  libbz2-dev zlib1g-dev libncurses-dev libffi-dev \
  git curl
```

### 2) Python environment

```bash
python3.12 -m venv venv_nat
source venv_nat/bin/activate
pip install -r requirements.txt
pip install -r requirements_dev.txt
```

> `requirements.txt` contains a few `git+https://github.com/...` deps for the
> `balrog`/`genesis` domains. If GitHub is not reachable these lines can be
> dropped for a core install.

### 3) LLM credentials (create `.env`, NOT committed)

The secure GAN entry loads only `<repo_root>/.env` in the trusted parent;
sandboxed role/task processes disable dotenv discovery and receive no provider
credential. `litellm` reads `OPENAI_API_BASE` / `OPENAI_API_KEY` in the parent.
Any OpenAI-compatible endpoint works:

```ini
# .env  (never commit this file)
OPENAI_API_BASE=https://<your-openai-compatible-endpoint>/v1
OPENAI_API_KEY=<your key>
```

Model selection has **no environment variables and no fallback**: the single
source is `gan/framework/models.yaml`, read only by `gan/framework/models.py`
(sections `gan.{task,planner,evaluator}`, `dgmh.{meta,task}`, `domains.<role>`).
The domain task agent receives its model via an explicit
`domains.harness --model ...` argument, never via env. To change a model, edit
`gan/framework/models.yaml`.

### 4) Docker (only needed for the original HyperAgents container flow)

```bash
docker build --network=host -t hyperagents .
# The original loop expects the image tag `hyperagents` (REPO_NAME)
```

## Running

### GAN dual-loop (new)

```bash
python scripts/run_gan.py \
  --domains paper_review \
  --subset _filtered_100_train \
  --num_samples 2 \
  --outer 1 --inner 2 \
  --output_dir ../runs/gan_paper_review
```

Outputs (under `--output_dir`): `logs/events.jsonl`,
`logs/{task,planner,evaluator}_tree.jsonl`, `scores/scores.jsonl`,
`runs/<genid>/{packet.json,eval.json,feedback_digest.md,patch_receipt.json}`,
`trajectory/outer_<O>/...`, `ckpt/checkpoint.json` (+ immutable `ckpt/outer_<N>.json`).

### Original HyperAgents flow (base)

```bash
# evaluate the task agent on a domain (host)
python -m domains.harness --domain paper_review --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo

# full self-improvement loop (requires Docker + `hyperagents` image)
python scripts/dgmh/generate_loop.py --domains paper_review
```

## Data (excluded from this repo, fetch separately)

Large datasets are intentionally **not** committed (see `.gitignore`):

- `domains/paper_review/dataset.csv` — from the upstream
  [HyperAgents](https://github.com/facebookresearch/HyperAgents) repository.
  The split files `dataset_filtered_100_{train,val,test}.csv` are generated
  locally:
  ```bash
  python -m domains.paper_review.curate_subsets
  ```
- `domains/polyglot/polyglot-benchmark/` — from
  [Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark).
- `domains/polyglot/SWE-bench/` — from
  [princeton-nlp/SWE-bench](https://github.com/princeton-nlp/SWE-bench)
  (commit `dc4c087c2b9e4cefebf2e3d201d27e362d899e0f`).
- `domains/polyglot/polyglot_benchmark_metadata.json` — generated by
  `python -m domains.polyglot.prepare_polyglot_dataset`.

## Safety

> [!WARNING]
> This repository executes untrusted, model-generated code. Review the original
> HyperAgents safety notes before running self-improvement loops.

The supported GAN security boundary starts at `scripts/run_gan.py` (or the
programmatic `gan.driver.run_gan_driver` entry) and requires the driver-managed
role/task sandboxes. The legacy `--in-process` option is disabled because it
bypasses those sandboxes. Direct calls to `build_gan_loop`, `gan.outer_worker`,
or role classes are internal/test interfaces and are not sandboxed entry points.

## Attribution

Base code: **HyperAgents** by Meta (see `LICENSE.md`, CC BY-NC-SA 4.0). If you
use it, please cite:

```bibtex
@misc{zhang2026hyperagents,
      title={Hyperagents},
      author={Jenny Zhang and Bingchen Zhao and Wannan Yang and Jakob Foerster and Jeff Clune and Minqi Jiang and Sam Devlin and Tatiana Shavrina},
      year={2026},
      eprint={2603.19461},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2603.19461},
}
```
