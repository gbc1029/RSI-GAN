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

> **Put the venv on a Linux-native filesystem when the repo is on a Windows
> drive (WSL).** Every sandboxed child process (`domains/harness.py` spawns one
> per task question — and one per ENVIRONMENT STEP for `balrog`) re-imports the
> interpreter's site-packages from scratch, so the prefix's read latency is
> multiplied by the number of children. Measured 2026-11-04 on this repo with a
> byte-identical venv (`cp -a`):
>
> | `import agent.llm` | prefix | wall |
> |---|---|---|
> | native (ext4) | `~/venv_nat` | **3.0 s** |
> | repo on `/mnt/d` (9p/drvfs) | `/mnt/d/.../venv_nat` | **67.5 s** |
>
> `python3 -m venv venv_nat` inside a repo that lives under `/mnt/...` therefore
> adds ~60 s of pure I/O to EVERY step (a 10-episode × 128-step balrog
> generation then spends hours before the model is called). Recommended:
>
> ```bash
> python3.12 -m venv ~/venv_nat                 # native path
> ~/venv_nat/bin/pip install -r requirements.txt -r requirements_dev.txt
> ~/venv_nat/bin/python scripts/run_gan.py ...   # use the ABSOLUTE native path
> ```
>
> A symlink `<repo>/venv_nat -> ~/venv_nat` keeps repo-relative tooling working,
> but `sys.prefix` then reports the `/mnt/...` path and Python keeps resolving
> site-packages through 9p (measured 20 s instead of 3 s): for long runs invoke
> the interpreter by its native absolute path. The driver emits a loud
> `WARNING` (and records `python_prefix_slow_fs` in the `driver_start` event)
> whenever `sys.prefix` sits under `/mnt|/media|/net`, so a slow run is
> diagnosable after the fact.

> `requirements.txt` does NOT carry the `balrog`/`genesis` extras: `balrog` has
> its own installer (see **3d** — the pins it needs cannot be expressed as
> requirements lines), and the `genesis` deps stay commented out (GPU / WSL CUDA
> is not configured). A core install needs nothing beyond this file.

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

### 3b) Secure sandbox dependencies (GAN driver)

The GAN security boundary (fix3-main-sync) additionally requires — the driver
fails fast without them:

```bash
sudo apt-get install -y bubblewrap acl util-linux tzdata ca-certificates
```

`bwrap` / `setpriv` / `setfacl` must be root-owned regular executables that are
NOT group/other-writable:

```bash
stat -Lc '%U %G %a %n' \
  "$(command -v bwrap)" "$(command -v setpriv)" "$(command -v setfacl)"
```

### 3b+) Polyglot child-sandbox toolchains (bake, 2026-10-29)

The task sandbox is a read-only projection of the host (`/usr`, `/bin`, ...),
so a language toolchain becomes available to the task agent simply by
installing it on the HOST — no extra sandbox config. The eval containers
bake their own toolchains (Go 1.21.5 et al., `domains/polyglot/dockerfiles.py`);
match those versions so the agent sees what the grader sees:

```bash
# Go (align with the eval env image version)
curl -fsSL --retry 2 -o /tmp/go.tgz   "https://mirrors.aliyun.com/golang/go1.21.5.linux-amd64.tar.gz"
tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz
ln -sf /usr/local/go/bin/go /usr/bin/go && ln -sf /usr/local/go/bin/gofmt /usr/bin/gofmt
# Java (javac for the java exercises)
apt-get install -y openjdk-17-jdk-headless
```

Offline behaviour is pinned per exercise by `domains/task_worker.py`
(`GOTOOLCHAIN=local`, `GOPROXY=off`, `GOTELEMETRY=off`); the go build cache
lands in the sandbox-private tmpfs (`HOME=/tmp`), never in the patch.

### 3c) Dedicated sandbox accounts (GAN driver)

The driver runs as root but drops every worker to a dedicated unprivileged
UID. Four UIDs must be pairwise distinct: the `sudo` caller, the task agent,
the planner and the evaluator. Create three no-login system accounts:

```bash
for u in rsi_gan_task rsi_gan_planner rsi_gan_evaluator; do
  getent passwd "$u" >/dev/null || \
    sudo useradd --system --user-group --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin "$u"
done
```

Env vars: `GAN_TASK_SANDBOX_USER` has NO default and must be set explicitly;
`GAN_PLANNER_SANDBOX_USER` / `GAN_EVALUATOR_SANDBOX_USER` default to
`rsi_gan_planner` / `rsi_gan_evaluator` — pass all three explicitly to avoid
deployment ambiguity.

### 3d) balrog domain (optional)

`balrog_babyai` / `balrog_babaisai` need an env stack that a plain
`pip install -r requirements.txt` cannot produce. Install it with the domain's
own script (it ends by verifying the stack and is idempotent):

```bash
bash domains/balrog/scripts/setup_balrog.sh            # babyai only
bash domains/balrog/scripts/setup_balrog.sh --only all  # + babaisai
bash domains/balrog/scripts/setup_balrog.sh --check     # verify, install nothing
bash domains/balrog/scripts/setup_balrog.sh --uninstall # roll back
```

What it pins, and why (all measured 2026-11-03):

| piece | pin | reason |
|---|---|---|
| Minigrid **fork** | `cf73dd148e51276bd675a37e3bfb0bf2b42329b2` (branch `babyai_text`), tarball | PyPI `minigrid==2.3.1` has neither `BabyAI-MixedTrainLocal-v0` nor `info["descriptions"]` — balrog's BabyAI text wrapper needs both |
| `gymnasium` | `1.2.0` | minigrid's API base |
| `pygame` | `>=2.5` | minigrid rendering (`get_pov_render`) |
| `gym` | `0.23.0`, installed with **`--no-deps`** | `domains/balrog/environments` builds its `Strings` space on old gym; its own `pygame==2.1.0` pin has no cp312 wheel. Every env handed to `EnvWrapper` is adapted to the Gymnasium `reset/step` API in its own `make_*_env` (`GymV21CompatibilityV0`), and `scripts/local/verify_env_contract.py` asserts that contract for the wired domains |
| `setuptools` | latest | old gym imports `distutils`, removed from the py3.12 stdlib |
| `baba-is-ai` (`import baba`) | `40ae1b29dbc67f2ecea06cc161ed5ca51719e5e3`, tarball | babaisai env; git-only project (not on PyPI) |

Known constraints:

- GitHub `git+https` clones stall in restricted networks (`objects.githubusercontent.com`
  blocked) — the script uses `codeload.github.com/<repo>/tar.gz/<sha>` tarballs and
  checks their sha256. Point `BALROG_MINGRID_TARBALL` / `BALROG_BABA_TARBALL`
  at local copies to install fully offline.
- `pip check` stays clean; numpy/pandas are never touched (numpy 2.x is what the
  venv already ships and what these pins were verified against).
- Cost: one `balrog_babaisai` generation is 40 tasks x 100 steps = 4000 agent
  sessions (the budgets in `gan/framework/domains.yaml` say so out loud). Trim
  `domains/balrog/config/config.yaml` `babaisai_tasks` for cheap runs.
- **Parallel episodes are DECLARED, not guessed** (`harness_episode_workers` in
  `domains.yaml`): `balrog_babyai` runs 2 episodes at a time, `balrog_babaisai`
  stays at 1 (40 tasks x 100 steps per unit — raise it only after a measured
  round). The harness applies `min(--episode_workers, --num_workers)`, so a
  domain that declares nothing keeps one episode at a time. Every env STEP is
  its own sandboxed child, so this is a namespace-tree budget: measure with
  `scripts/local/run_balrog_real_ext4.sh` (same frozen plan, workers 1 vs N)
  before raising it. A worker that dies no longer stalls the round — the lost
  episode is recorded as ABORTED and the denominator never shrinks.
- **Per-episode cost attribution**: each episode's per-call audit rows
  (`<transcript>.llm_calls.jsonl`) now survive the sandbox (stage → bind →
  copy-back), are archived next to the transcript under
  `agent_evals/chat_history_<env>__<task>__epNN.jsonl.llm_calls.jsonl`, and
  `report.json` carries `episodes[].llm_calls` / `episodes_sidecar_missing` /
  `llm_calls_total`. These are CALL COUNTS for attribution only — money and
  tokens remain the root-side proxy audit's numbers; never sum the two.
- **Every step of an episode now reaches the archive.** The task agent truncates
  its transcript at construction, so a multi-step episode used to keep only its
  LAST step (measured 2026-11-08: 10 episodes / 98 step children produced 10
  transcript rows). The balrog step child now appends
  (`_copy_back_transcript(..., "append")`), which is what lets the evaluator
  read a whole episode instead of one step.
- **The CSV / `prev_action` record the EXECUTED action.** They used to carry the
  model's raw reply, so the agent was told "your previous action was <a
  sentence>" while the env had executed `default_action`; the raw reply now
  lives in the `Reasoning` column.
- **Aborted rounds cannot masquerade as scores.** `domains.yaml
  max_aborted_ratio: 0.25` (balrog): above that share of aborted episodes the
  round's score is voided (`score: null`, `invalid_reason`, and a
  `task_round_degraded` event) instead of letting infrastructure failures enter
  the lineage as a depressed score.
- **Cross-step memory** (`domains.yaml history_messages`, upstream
  `agent.max_text_history` unit = messages, one step = 2): a step child has no
  conversation, so the last N steps are re-rendered into every prompt
  (`inputs["history"]`, `(observation as seen, executed action)` pairs).
  BOTH wired balrog domains declare 16 (= the last 8 steps, the upstream
  default); `balrog_babaisai` was 0 while the A/B was pending and was flipped
  after it. Measured prompt cost on babyai: **+~1.1 KB / +275 tokens per step
  (+14 %)**. Setting it to `0` restores the exact pre-window prompt (the A/B
  switch). **A/B verdict (2026-11-10, real length, both arms on the same frozen
  5-episode eval plan)**: ON 1/5 solved vs OFF 0/5, and the qualitative
  difference was the decisive one — ON used `toggle`/`pick up` (75 of 397
  steps) while OFF used NO interaction action at all in 384 steps (only
  turn-left / turn-right / go-forward, ~1/3 each), i.e. without the window the
  agent cannot tell "I am already facing / have already toggled this" and can
  never solve a task that needs an interaction. Verify that the window really
  reached the model with `scripts/local/check_history_delivery.py <run_dir>`
  (greps the archived prompts for the marker; violations mean an evolved
  `task_agent.py` stopped dumping `inputs`).
- `balrog_minihack` / `balrog_nle` / `crafter` / `textworld` are **not** wired
  into the GAN path and are not covered by this script (`post_install.py`
  downloads their data from GitHub/Drive and needs network). This is now
  **fail-closed**: `task_execution.domain_runnable_error` refuses a driver run on
  any balrog domain other than the two above (and `domains/harness.py` refuses it
  again), because without the per-step sandboxed agent factory the harness would
  silently fall back to an in-process agent with no proxy token. Wiring one needs
  three things together — a `_SANDBOXED_BALROG_MODULES` entry, a `domains.yaml`
  section (`task_brief` / `episode_tasks` / `output_contract` / per-sample
  budgets), and an installer path (NLE adds a C extension plus ~2 GB of game
  data).

### 4) Docker (only needed for the original HyperAgents container flow)

```bash
docker build --network=host -t hyperagents .
# The original loop expects the image tag `hyperagents` (REPO_NAME)
```

## Running

### GAN dual-loop (new)

The GAN loop runs ONLY through the sandboxed driver: launch it as a normal
user via `sudo` (so the driver gets both root start privileges and the correct
`SUDO_UID`/`SUDO_GID`), with the three sandbox-account env vars. `--in-process`
is hard-disabled (it bypasses the sandboxes), and only ONE `--domains` value is
accepted per run:

```bash
sudo \
  GAN_TASK_SANDBOX_USER=rsi_gan_task \
  GAN_PLANNER_SANDBOX_USER=rsi_gan_planner \
  GAN_EVALUATOR_SANDBOX_USER=rsi_gan_evaluator \
  venv_nat/bin/python scripts/run_gan.py \
  --repo_root "$PWD" \
  --domains paper_review \
  --subset _filtered_100_train \
  --num_samples 2 \
  --outer 1 --inner 2 \
  --output_dir /opt/rsi-gan/runs/gan_paper_review
```

`output_dir` rules (validated fail-fast): NOT inside `repo_root`, not a parent
of it, not `/`, not your home (or a parent of it), not under `/tmp`, no symlink
component, no bind-mount alias of the repo/home, and an already-existing
`output_dir` must not contain source/credential markers (`.env`, `.git`,
`agent/`, `domains/`, `gan/`, ...). It must be owned by the `sudo` caller and
sit on a native Linux filesystem with POSIX ACL support (avoid `/mnt/c`
DrvFS); e.g. `sudo install -d -o "$USER" -g "$(id -gn)" -m 700
/opt/rsi-gan/runs`. Credentials come from `<repo_root>/.env`, loaded only in
the trusted parent; sandboxed workers receive no provider credential (scoped
short-lived tokens via a sealed memfd instead). GPU runs are rejected
(`sandbox.gpu` must be `none`); only the CPU path is verified.

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

### RQGM baseline (git submodule)

> This repository uses RQGM reproduction code as a git submodule.
> RQGM reproduction source: https://github.com/kingkillery/red-queen-godel-machine, License: MIT.
> The submodule code retains its original license and copyright.

```bash
# one-time: add the submodule (needs GitHub access; see baselines/rqgm_bridge/README.md)
git submodule add https://github.com/kingkillery/red-queen-godel-machine.git baselines/rqgm

# convert paper_review -> RQGM tasks/anchor, then run
python -m domains.paper_review.curate_subsets
python baselines/rqgm_bridge/convert.py \
  --csv domains/paper_review/dataset_filtered_100_train.csv
python baselines/rqgm_bridge/run.py \
  --tasks baselines/rqgm_bridge/data/tasks.jsonl \
  --anchor baselines/rqgm_bridge/data/anchor.jsonl
```

See `baselines/rqgm_bridge/README.md` for details and the documented
reviewer-degradation limitation on `paper_review`.

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
