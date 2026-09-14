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

- [`docs/plan.md`](docs/plan.md) — full design + implementation plan.
- [`docs/GAN实现记录.md`](docs/GAN实现记录.md) — implementation record (files, problems, fixes, minimal run).
- [`docs/部署记录.md`](docs/部署记录.md) — deployment / environment record.

> This repository also contains the original HyperAgents (DGM-H) codebase, which
> the GAN framework reuses (archive/version trees, container harness, domain
> evaluation). The upstream license (`LICENSE.md`, CC BY-NC-SA 4.0) applies to
> that base code. This repo drops the upstream git history and keeps a single,
> clean initial commit.

## Repository layout

```
gan/                     # the GAN framework (new)
  config/                #   yaml registries + prompts + loader
  reward/                #   RewardPacket + evaluator reward (2x2 matrix + calibration)
  tree/                  #   task/planner/evaluator version trees
  operators/             #   planner operators + evaluator eval points (+ source-access gate)
  roles/                 #   planner / evaluator roles
  access.py              #   on-demand source-code access gating
  loop.py build.py task_runner.py patch.py summary.py
scripts/run_gan.py       # GAN dual-loop CLI entry
docs/                    # design / implementation / deployment docs
agent/ domains/ utils/   # HyperAgents base code (reused)
task_agent.py meta_agent.py generate_loop.py   # HyperAgents base
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

The repo loads `.env` automatically (`agent/llm.py`), and `litellm` reads
`OPENAI_API_BASE` / `OPENAI_API_KEY`. Any OpenAI-compatible endpoint works:

```ini
# .env  (never commit this file)
OPENAI_API_BASE=https://<your-openai-compatible-endpoint>/v1
OPENAI_API_KEY=<your key>
```

Optional model overrides for the GAN roles:

```ini
GAN_MODEL_DEFAULT=<model>      # fallback default model
GAN_TASK_MODEL=<model>         # task agent model (also injected per node)
GAN_MODEL_PLANNER=<model>
GAN_MODEL_EVALUATOR=<model>
```

### 4) Docker (only needed for the original HyperAgents container flow)

```bash
docker build --network=host -t hyperagents .
# The original loop expects the image tag `hyperagents` (REPO_NAME)
```

## Running

### GAN dual-loop (new)

```bash
python scripts/run_gan.py \
  --task-domain paper_review \
  --subset _filtered_100_train \
  --num_samples 2 \
  --outer 1 --inner 2 \
  --output_dir outputs/gan_paper_review
```

Outputs: `task_tree.jsonl`, `planner_tree.jsonl`, `evaluator_tree.jsonl`,
`runs/<genid>/packet.json`, `runs/<genid>/evaluator_reward.json`, `events.jsonl`.

### Original HyperAgents flow (base)

```bash
# evaluate the task agent on a domain (host)
python -m domains.harness --domain paper_review --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo

# full self-improvement loop (requires Docker + `hyperagents` image)
python generate_loop.py --domains paper_review
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
