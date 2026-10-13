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
  `loop.py`, `tree/`, `reward/`, `cm/` (context-management frozen core —
  block model, primitives, the per-turn seam (``assemble_turn`` DISPATCHES
  only; the pipeline itself is the baseline assembly ``assembly.py`` — one
  named preset per ``cm_mode``, composed over the session's frozen primitive
  methods; invariants live in the primitives, never in the pipeline source,
  and ``resolve()`` also loads the P2 NAMED agent-owned assembly (``cost.cm_assembly``
  design key → ``gan/components/<role>/cm/<stem>.py``, delivered as
  ``cm_assembly.py`` / policy as ``cm_policy.py`` under FIXED names so the
  child env pins are static; load-once per session, per-turn exec budget,
  2-strike wedge breaker, fail-open to the preset — every fallback leaves
  durable events + ``degraded``/``assembly_disabled`` cm_view stats; the P3
  in-process p/e seats resolve the SAME named slots workspace-first at
  session build (``loop._resolve_cm_module``, no sort-order discovery — an
  unresolved claim is a loud ``cm_module_unresolved`` and custom degrades to
  the slice preset with its keys rewritten), with per-path sys.modules keys
  for the parallel self-improve threads and a preflight pool gate), fail-open policy
  runner, trajectory index; STDLIB-ONLY import contract because it is
  snapshot-delivered into the task sandbox as top-level ``cm``).
- `gan/tools/` — **always-on** callables: `work/<role>/` (role behavior tools),
  `work/common/` (frozen plumbing: `edit_source`, `read_file`, `list_dir`, `grep`,
  `read_trajectory`, `read_session_trajectory`, `list_editable`, `list_components`),
  `design/` (shallow design
  operators), `deep/` (gated deep gate: `request_source_access` + `register_component` +
  `unregister_component` + `update_component`),
  `assembly.py`.
- `gan/components/` — **opt-in** tool implementations, one tree per role (batch 6):
  `task/` (tools + the curated `task/knowledge/*.md` base), `evaluator/`,
  `planner/` (self-authored knowledge/tools); each tree may carry a `cm/`
  pool of THIN context-management wrappers (``ctx_index``/`ctx_fetch`/
  `ctx_budget`/`ctx_pin`/`ctx_note`/`ctx_view` — task selects only
  `ctx_budget`/`ctx_note`; its trajectory access rides the
  `read_trajectory` slice front-end instead).
- `gan/registries/` — per-role tool catalog: **one single-writer `json` per role**
  (`task.json` / `planner.json` / `evaluator.json`; no shared file) + `loader.py`.
  Every entry is a TOOL (a callable `tool_info`/`tool_function` module); the legacy
  `kind` field (`skill`/`eval_point`) may still appear in content and is a
  descriptive tag only.
- `gan/design/` — **shallow, evolvable**: `schema.py`, `store.py`, `seeds/`.
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
python scripts/run_gan.py --domains paper_review --subset _filtered_100_train \
  --num_samples 2 --outer 1 --inner 2 --output_dir outputs/gan_paper_review

# original HyperAgents domain eval (model is explicit; resolved from models.yaml)
python -m domains.harness --domain paper_review --model openai/gpt-4o \
  --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo
```

Tests / verification scripts are kept locally under `scripts/local/` (gitignored).
The tool-management regression suites (per-role registries, deep gate, P→E
projections) live there too: run `venv_nat/bin/python
scripts/local/run_tool_regression.py` before and after touching `gan/tools/`,
`gan/registries/`, `gan/framework/{access,task_execution,receipt,loop}.py` or the
design operators — three suites (deep gate S1–S4 / batch-6 unification /
batch-8 isolation), one subprocess each.

## Offline run analysis (scripts/local/, gitignored)

Two local analyzers read finished GAN run dirs (`scores.jsonl`,
`runs/<genid>/`, `logs/events.jsonl`, `ckpt/`) — **offline only** (no network,
no LLM calls), run with `venv_nat/bin/python`, and write artifacts into
`<run_dir>/score_analysis/`. The base `analysis/*.py` scripts consume the DGM-H
`archive.jsonl` + `gen_<id>/metadata.json` layout and do NOT fit GAN run dirs
directly; the sanctioned bridge is to reuse their computation/rendering cores
with a GAN-native data layer.

- **`gan_scores_extract.py` — score extraction + analysis** (per run:
  `scores_extract.csv` / `REPORT.md` / `significance.txt` / `progress.png`).
  Reuses `analysis/analysis_utils.py` (`save_significance_tests`,
  `compute_bootstrap_ci`) verbatim. Conventions: **strict** = the harness's
  exact lowercase string match against the dataset `winner`
  (`report.json.total` excludes invalid samples when `coverage<1` — crosscheck
  counts, not denominators); **lenient** = offline verdict re-parse from the
  raw prediction text with a per-row parse-method audit trail
  (dict_verdict/verdict_line/response_better/bare/model_exact/…); ground truth
  is auto-disambiguated by matching every observed `question_id` against the
  `domains/<domain>/dataset*.csv` files. Per-generation AND per-question tables
  (systematic-error localization), evaluator `predicted_score` calibration, and
  the significance file only include runs with n≥2 samples (n=1 runs are noise).
- **`gan_lineage_graph.py` — genealogy graphs + offline lineage audit** (per
  run: `lineage_inner_{lenient,strict}.png`, `lineage_outer_{lenient,strict}.png`,
  `lineage_audit.md`). Imports `analysis/visualize_archive.py:visualize_graph`
  UNCHANGED (colormap, valid_parent red/green borders, best-node diamond,
  dot layout); the data layer replays `events.jsonl` (`code_lineage` /
  `code_branch` / `inner_done` / `stagnation_break`). Border metadata reaches
  the renderer through a shim dir (`gen_<node>/metadata.json`); node ids encode
  代际 + marks (`g4·o3i1·nopatch·stag`). Git audit is read-only via
  `git -c safe.directory=<code_root>`: a generation with `applied:false`
  aliases its parent's commit (lineage expressed by ref equality, no new git
  parentage — reported as ALIAS, not MISMATCH); all refs resolving to one
  commit is reported as a degenerate tree, not an error.

## Conventions

- **Shallow vs deep**: each agent's *shallow design* is a single config JSON in
  `gan/design/` (one component slot, `tools`, per role — batch 6; the legacy
  `skills`/`eval_points` slot names are aliases). Shallow operators live in
  `gan/tools/design/` and may set **existing** keys, **declared dynamic keys**
  (`gan/design/schema_ext/<role>.json`, declared via `add_config_key` with a
  registered `consumer` — declaration and consumer land in the SAME patch and
  roll back together; undeclared/consumerless keys are stripped by the key heal
  at persist), or select/deselect
  **registered** tools (`select_component` / `deselect_component` / the `tools`
  key of `set_config`). Tool add/delete/modify is classified by what it
  touches: **shallow** = edit the design config list only; **deep delete** =
  remove from the registry + source file (`unregister_component`); **deep
  add/modify** = new tool or logic, which
  **must** touch source via the gated `gan/tools/deep/request_source_access`.
  There is no `mint_operator`. After a `modify` grant, planner/evaluator edit the
  granted copies with `edit_source`
  (`gan/tools/work/common/`, confined to the instance workspace `src/`); roles are
  intentionally NOT given raw `bash` (it cannot be confined). A repeated
  `request_source_access` for a path that is already granted does **not** re-copy
  from `code_root` — in-session workspace edits survive; pass `refresh=true` to
  deliberately discard them and re-sync the workspace copy.
- **Knowledge base (batch 6, pull-based)**: markdown notes under
  `gan/components/<role>/knowledge/*.md` are **DATA** — not registry entries, no
  name=stem contract, never executed. The planner authors task-side notes with
  the deep patch channel; the whole COMMITTED base is materialized into the task
  sandbox each generation (`.gan_runtime/knowledge` + `GAN_TASK_KNOWLEDGE_DIR`,
  per-note/total caps, skips reported) and the task agent reads it on demand via
  the opt-in `knowledge` tool — the base is the base, and there is no knowledge
  config list. planner/evaluator read their own (and task-side) notes through the
  normal access channel (`request_source_access(view)` + `list_dir`/`read_file`),
  never through an un-audited side door.
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
  previous checkpoint's design. `outer_improved` is logged, not enforced. The two
  role SELF-IMPROVE sessions inside one outer run in PARALLEL threads (their write
  surfaces are seat-disjoint by `frozen.SEAT_WRITE`); only the fast patch
  apply+commit serializes on `code_repo._TREE_LOCK`, and commits stage only the
  patch's own files (attribution-clean). Role sessions carry their proxy
  credential as an explicit `api_key` argument and the toolset load-report sink as
  an explicit parameter — never process-global `os.environ` swaps;
  `AccessBroker.last_result` is thread-local.
- **Output layout** (`gan/framework/paths.py`, the single source): `ckpt/`
  (recovery + `code/` per-run git tree + `code.json` manifest + `design/`), `logs/`
  (`events.jsonl`, `*_tree.jsonl`, `*.log`),
  `trajectory/outer_<O>/{planner,evaluator,<genid>/{task,planner,evaluator}}.jsonl`
  (OUTER-level role files are attempt-keyed `<role>__<attempt>.jsonl` — one per
  loop instantiation, assigned at role CONSTRUCTION so content and stop records
  share one file; directly-constructed roles keep `<role>.jsonl`; readers merge
  canonical + attempt siblings, newest first), `scores/scores.jsonl`, `runs/<genid>/` (small evidence:
  `packet.json`, `eval.json`, `feedback_digest.md`, `patch_receipt.json`,
  `patch_proposed.diff`), `work/<genid>/` (ephemeral, pruned),
  `workspaces/<role>/outer_<O>/`. `ckpt/checkpoint.json` is the latest (overwritten atomically); outer boundaries
  also write an immutable `ckpt/outer_<N>.json` (atomic; archived as
  `outer_<N>_<ms>.json` when the same outer is re-run); `restore_trees` appends an
  `op=reset` event instead of truncating the log.
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
  labels are read by the harness from `repo_root`, passed **only** via the CLI
  `--dataset_root` argument to the parent harness (never written into the task
  child's env/payload — v5/fix1); `GAN_DATASET_ROOT` is scrubbed from every child env.
- **Activation rule**: `always-on → gan/tools/`, `opt-in → gan/components/`
  (registered in `gan/registries/` and selected by the design config). Plumbing
  tools (`deep/`, `design/`, `work/common/`) are frozen; only `work/<role>/` and
  the task components/registry are evolvable.
- **Session permissions (role + seat)**: `AccessContext` carries both the role and
  session seat. Reads remain role-wide; writes are seat-specific: planner `plan`
  may write the task surface, planner/evaluator `self_improve` may write its own
  role surface, and evaluator `evaluate`/task sessions are read-only. The frozen
  matrix is enforced at grants, source edits, patch checks, and commits.
  **The seat is a closed, validated set** (`frozen.SEAT_WRITE`: `planner/{plan,
  self_improve}`, `evaluator/{evaluate, self_improve}`, `task/{task}`): it is
  REQUIRED (no default) on `set_access_context` / `frozen.write_roots` /
  `frozen.is_allowed` / `AccessBroker.grant` / `code_repo.check_patch|apply_*`,
  and an unknown `(role, seat)` pair raises `ValueError` instead of falling back
  to the role's union — role/seat are supplied by framework code, never by the
  agent, so a missing or misspelled seat is a bug that must fail loudly. There is
  no `legacy` seat. The only deliberate union query is
  `frozen.any_seat_allows` (assembly path→role **classification**, never
  authorization); the tool layer returns an error string (plus an
  `illegal_session_seat` audit event) rather than raising, so a bad context
  cannot abort a session.
- **Front-loaded write policy**: `edit_source` is the only official source-edit
  entry and uses the centralized frozen authorization helper before mutation.
  Dispatch validates declared `edit_source` inputs before invoking tool code.
  Commit and load/preflight layers also apply a conservative AST policy rejecting
  raw writes, subprocesses, and dynamic execution in agent-owned Python. Framework
  plumbing is explicitly exempt because it legitimately writes state; this AST
  check is defense in depth, not a replacement for a real sandbox.
- **Access boundary**: the frozen role allowlist also defines the read surface
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
  from `repo_root`, passed only as the CLI `--dataset_root` argument (never env).
- **Code blood lineage (v5, branch-per-node)**: every task generation's code is
  a git commit in the per-run code tree whose git parent is its **selected
  parent's** code commit; each generation pins a persistent ref `task_<genid>`,
  and the outer's entry HEAD is pinned as `outer_<O>_base` (the base for
  `initial`-parent children and the per-outer ancestry anchor). The base for
  planning/patching is resolved from the parent's ref, never from the moving
  HEAD — cross-outer tree selection follows the same rule (方案 A: the task tree
  is inherited intact across outers; invalid children become auditable dead
  branches, and a rejected/missing patch aliases the parent's commit — no
  lineage gaps). The outer boundary snapshot's `code.commit` remains the HEAD
  at snapshot time.
- **Task brief**: each domain in `gan/framework/domains.yaml` has a `task_brief`
  (human-readable task + answer interface). It is **framework-injected** into the
  planner / task / evaluator prompts (via `GAN_TASK_BRIEF` for the task agent and
  the `task_brief=` argument for roles) and is **NOT** part of the evolvable design
  (marked *可解冻* for a future axis). It must never include grading mechanics
  (case-insensitive / exact-match / no partial credit) or ground-truth labels/scores.
  The evaluator's blind-phase `run_summary` deliberately excludes the objective
  score/accuracy (revealed only via `benchmark_score`).
- **Generation-0 prompts (seeds)**: every role's *initial* design prompt comes from
  `gan/design/seeds/<role>.md` via `gan.design.initial_config` (schema defaults +
  non-empty seed). The task agent's gen-0 config goes through the **same** helper
  (`loop._initial_task_config`), so all three roles start from their seed; a blank
  seed falls back to the schema default rather than an empty prompt. Editing a seed
  only affects **new** runs — an existing design file under `ckpt/design/**` is
  authoritative and never rewritten.
- **Sandbox / bash (deferred)**: planner/evaluator are NOT given raw `bash`; they
  use the frozen, workspace-confined `edit_source` / `read_file` / `list_dir` /
  `grep`. A real sandbox (bubblewrap / container) is deferred until roles execute
  self-written code or adversarial leakage must be ruled out (see v4.14).
- **Tool dispatch net (batch 27)**: the tool loop's single execution chokepoint
  `agent/llm_withtools.py:process_tool_call` packages every call with three
  dispatch-level guarantees — **C1** a per-call timeout (`loop.tool_call_timeout_s`,
  roles via `Role.run` → instance attribute; task child 240s under the harness
  QUESTION_TIMEOUT) with a per-tool wedge breaker (2 wedges → disabled for the
  session, events `tool_wedged`/`tool_disabled`; wedged threads are non-daemon, so
  interpreter exit joins them — bounded by the enclosing process timeouts);
  **C2** a central 16k-char output cap with an explicit in-band truncation marker
  + `tool_output_truncated` event (fine-grained caps like `read_file`'s 8000 stay);
  **C3** tool stdout/stderr captured OUT of the loop's log stream via
  `_CaptureProxy` (main-thread-owned, flipped back to pass-through when a call is
  abandoned — a wedged thread must never swallow main-thread output) and merged
  into the tool result as `-- tool stdout (captured) --` + audit events. Tool STATE
  travels via explicitly SEEDED contextvars (`contextvars.copy_context()`) — a bare
  executor thread would break every context-dependent tool. A FIRST-TURN response
  that announces intent in prose but emits NO tool-call block gets exactly one
  bounded feedback turn (first-turn no-op guard; reasoning models occasionally
  narrate "I'll start by..." and stop — 3/10 role self-improve sessions); sessions
  without a toolset and later prose endings are unaffected.
- **Model config**: the single source is `gan/framework/models.yaml`, read only by
  `gan/framework/models.py` (`resolve/resolve_entry/resolve_section/describe`) — a
  **pure lookup** with NO env, NO fallback and NO precedence chain. Sections:
  `gan.{task,planner,evaluator}`, `dgmh.{meta,task}`, `domains.<role>` (non-task
  domain roles only, e.g. the polyglot aider CLI). An entry is either a plain
  model string or a mapping `{model: ..., reasoning_effort: ...}` — `resolve()`
  always returns the model STRING (all existing consumers unaffected);
  `resolve_entry()` returns the normalized entry and validates it at startup
  (unknown mapping keys / unknown `reasoning_effort` values fail before any run).
  `reasoning_effort` is the gateway's thinking-intensity knob (contract:
  `low|high|max`, NOT the OpenAI enum; defined as `agent.llm.REASONING_EFFORTS`).
  It travels EXPLICITLY as a function/CLI argument — never an environment
  variable: roles get it threaded `build factories -> Role -> chat_with_agent ->
  get_response_from_llm`; the task child gets it `DomainTaskRunner ->
  run_harness_and_report -> domains.harness --reasoning_effort -> stdin payload ->
  TaskAgent` (broker mode: fixed in the root-side broker config from the same
  models.yaml entry). The **domain task agent shares the driver's task model**
  and receives it at runtime via an explicit `domains.harness --model ...`
  argument (never env). The effective model config is recorded at runtime
  (`events.jsonl: model_config` for GAN, via `describe()` incl. effort;
  `[model_config]` log line for DGM-H; `Node.meta["model"]` per generation; the
  proxy audit adds the caller's `reasoning_effort` per call). Do not read
  `models.yaml` anywhere else.
- **Reserved cost keys & context management (docs/10 phases C/D)**: the per-role
  design carries a RESERVED top-level `cost` dict slot — the authority is the frozen
  `gan/framework/cost_keys.py` (ranges/enums/defaults; `validate_cost_config` shared by
  `set_config`/schema; `effective_all` the ONLY clamp; `cost_keys_applied` events). Task
  budget keys ride the harness **stdin payload** (never env) into `chat_with_agent`
  (`max_tool_calls`/`max_total_tokens` soft stop). Phase D adds the `cm_*` subgroup and
  the seam: `agent/llm_withtools._counting_llm` is the SINGLE per-turn assembly point —
  the `CMSession` (`cm=` arg, built by the loop per seat / by `task_worker` from the
  `.gan_runtime/cm` SNAPSHOT — the run dir has no `gan/`, so the frozen core is copied
  per generation and imports as top-level `cm`; STDLIB-ONLY by contract) rewrites only
  what is SENT (per-ENTRY copies — the client's text->content conversion pops keys from
  the dicts it is handed, so the canonical history and the raw trajectory rows stay
  untouched — 双记; sticky view forms). Binding reads BOTH `content`- and `text`-keyed
  entries (`_msg_text`); the session's FIRST user turn (the instruction + cost
  statement) is pinned by default, whose only exemption is the `dedup` prefix-elision.
  Modes: `passthrough` (identity, pre-D behavior — an absolute kill switch:
  a named `cm_assembly` is never executed under it, spoken once)
  / `slice` / `compact_conservative` / `custom` (agent policy module, AST-gated no-LLM/
  no-network, exec budget + 2-strike wedge breaker, fail-open always; since P2
  the design NAMES the policy (`cost.cm_policy`) and may name a whole
  agent-owned assembly (`cost.cm_assembly`) replacing the preset composition —
  delivered under fixed names, loaded once per session, wedge-broken, and a
  degraded session marks receipt `budget.kind` = `cm` (policy OR assembly
  degradation); compact_conservative
  excerpts big old USER turns — tool outputs ride inside user turns in this protocol,
  so a separate tool_output kind never materializes). Invariants:
  pinned blocks survive, `cm_drop_floor_chars` guards tiny blocks, the view cannot
  fabricate content (face-enforced; the handle's `ctx._s` private reference is NOT a
  security boundary). Agent face = the `ctx_*` opt-in tools (`ctx_view` included —
  `view_state` is on the agent face; `ctx_budget` is a LIVE meter: `chat_with_agent`
  attaches a reader over the run's authoritative usage totals, 实付口径, completed
  tool calls only); `read_trajectory`/`read_session_trajectory` gain a slice front-end
  (structural index first, `fetch=` materializes one segment) whose mode rides
  `AccessContext.session_cm_mode` — stamped per session from the session OWNER's own
  design, presentation-only (trajectory_genids/redaction stay the sole permission
  surface). Audit: per-turn `cm_view` (aggregate stats + per-op brief with block
  refs/rules, no content) + `cm_violation`/`cm_policy_failed`/`cm_policy_disabled`
  events; the task child's seam events land in a `cm_events.jsonl` sidecar (rw-bound
  as `/workspace/cm_events.jsonl`) that `DomainTaskRunner._ingest_cm_events` folds
  into events.jsonl with role=task attribution (`cost.json` carries the `task_cm`
  summary); the session cost record carries a `cm`
  summary (estimated savings are a DERIVED indicator, never billing — the ledger keeps
  实付); receipt `budget.kind` gains `cm` when a session's policy degraded.
- `gan/framework/loop.yaml` is framework hyperparameters — **not** evolvable. Task
  trajectory visibility is NOT configured here: the loop passes an explicit
  `AccessContext.trajectory_genids` set per session (v4.20) — planner = direct
  parent; evaluator = current + parent; self-improvement = this outer's
  generations + the last generation's direct parent.
- **Work tools vs design operators**: evaluator scoring and planner
  `respond_issue` are work tools (`gan/tools/work/`); design operators are in
  `gan/tools/design/`.
- Operators are tools: a module exposing `tool_info()` + `tool_function(**kwargs)`.
- **Operators must self-record structurally**: any operator that changes design or
  source must call `ctx.record(op, **structured_fields)` — **never** free-text
  rationale. `records` is both the patch gate (`gan/patch.py:has_deep_write`) and
  the input to the evaluator-visible summary (`gan/summary.py:build_diff_summary`,
  which whitelists structured fields only), so an operator that records nothing is
  invisible to the loop even when it changed the workspace.
- **Identity contract (three names must agree)**: a tool's registered `name`
  (`gan/registries/*.json`), its module file **stem**, and its `tool_info()["name"]`
  must be the same string. The tool loop keys tools by file stem
  (`agent/tools/__init__.py`), so a mismatch assembles a tool that silently
  never loads. The FIRST TWO are statically enforced (`loader.entry_reason`:
  name == stem, plus the AST binding of `tool_info`/`tool_function`);
  the THIRD (`tool_info()["name"]`, which the runtime dispatch dict keys by)
  is enforced at LOAD time since batch 22 (`ca44ffb`): `agent/tools/__init__.py`
  import-checks the module and fail-closes a `name != stem` file with a
  skip-with-reason report entry (erratum vs batch 18: a STATIC gate can never
  check the name VALUE without an import, so the load-time runtime gate is
  the settled K2 form). `gan/registries/loader.py:entry_reason` enforces this at selection
  time (plus the **role-directory binding**: a registry entry may only reference
  its own role's component tree — one single-writer file per role since batch 6);
  `gan/framework/code_repo.py` (differential gate on commits touching
  `gan/registries/**` or `gan/components/**`) and
  `gan/framework/preflight.py:preflight_tools` (startup, fail-fast) enforce it too.
  Adding a tool file without a registry entry is reported as an **orphan**
  instead of failing silently; `list_components` lists such files as candidates and
  `register_component` (deep, same gate as `unregister_component`) registers them.
  A registry entry's `module` is **data, not code** (the scan can read a registry the
  agent edited this session), so it is sanitised before use: an absolute path or a
  `..` segment is rejected by `loader.safe_module_rel` (else `module_path` would hand
  `shutil.copy2` a file from outside `gan/components`, which `load_tools` would then
  import and expose).
  Both deep tools only reach the code tree through the **patch channel**: their
  changes are committed only in sessions that build a patch (`plan` / `self_improve`);
  an `evaluate` session cannot build a patch, so `modify` intents are refused
  outright there (batch 26) — nothing is silently dropped. `register_component`
  registers source that **already exists** (in
  `code_root` or in the workspace) and never creates, restores or deletes source
  files — restoring a deleted component means re-granting it
  (`request_source_access(..., refresh=true)`) and registering it again.
  An entry's catalog **metadata** (`description`/`params_schema`) is edited by
  `update_component` (deep); changing a tool's source is `edit_source` in place —
  the entry and its metadata survive untouched — not unregister+re-register.
  A component whose file stem equals an always-on tool's (or another registered
  component's) stem would make the assembly copy two files onto one toolset
  basename, silently shadowing one of them: `register_component` refuses such a
  stem up front, and the differential commit gate refuses a NEW collision for
  **every non-empty patch** (including patches that only add/modify
  `gan/tools/work/**`; pre-existing collisions never block).
  Patch visibility follows `granted_paths`: a file the agent created in the
  workspace reaches the patch only when a grant covers it (grant the parent
  **directory** to have new files inside it captured). `AccessBroker.covers` is the
  single definition of that predicate; `list_components` reports it per orphan as
  `patch_covered`.
  **Catalog-metadata drift (B30, batch 17)**: an entry carries an optional
  `source_sha` (sha256 of the module bytes, first 12 chars) meaning "the description
  was written against THIS version". It is stamped only when METADATA is written
  (`register_component` / `update_component` — refreshing the description is what
  clears the flag); a module edited on its own therefore drifts. Detection is
  ADVISORY and never gating: `list_components` reports per-entry `description_stale`
  (+ aggregate note) **without touching `valid`/`selectable`**, `edit_source` adds a
  one-line note when a mutating edit hits a registered component (`view` never
  does), `preflight_tools.catalog_stale` reports it at startup WITHOUT failing, and
  the planner's receipt carries `component_drift` (per-patch, computed from the same
  stamp so the two can never disagree). A missing stamp means unknown and is never
  reported. Deliberately NOT done: refusing to commit/select on drift (whether a
  change is user-visible is the agent's call, and a hand-edited registry could forge
  the stamp — it is not a security boundary) and auto-rewriting descriptions. Both
  the startup list and `component_drift` stay out of the evaluator's projection: they
  are catalog bookkeeping, not task facts (the evaluator already has
  `task_patch_files`, and `name == file stem` makes the component identity free).
- **`list_components` reads the design target, not the access role**: the catalog it
  lists is the one `select_component` consults (`DesignContext.role`), which differs
  from `AccessContext.role` in the planner's `plan` session (access `planner`,
  design target `task`). It resolves each registry file and component module
  **workspace-first, committed-tree second** (same rule the deep tools scan with),
  and reports per entry `origin` (`workspace`/`code`), `pending`
  (`added`/`removed`/`modified`/`none`) and `selectable` — the latter mirroring
  exactly what `select_component` will accept **now** (the effective,
  workspace-first registry, **for every role** since batch 13). The tool is
  read-only: it never grants and never writes.
- **Design authority and healing (batch 5; extended to all roles in batch 13)**:
  `select_component` and the component slots of `set_config` validate against the
  same registry — the effective (workspace-first) view, **for every role**, so a
  component registered this session is selectable in the same session whichever
  design is being edited (the batch-5 committed-only exception for role
  self-designs is retired; safety rests on the exit heal + the B7 assembly
  report + the next outer's ability to re-select). The task design is **healed**
  right before it is persisted (`gan/framework/task_execution.py:heal_design_slots`):
  slot names the committed tree cannot deliver are removed and recorded (event
  `design_dangling_stripped`, receipt `design_stripped`), so a rejected patch can
  never leave a dangling reference in the design inheritance chain — the child
  only ever runs with a design whose every component resolves. Since batch 13 the
  same heal runs for **role self-designs at the successful patch exit**
  (`loop._apply_self_patch`, after `self_improve_commit`, against that role's own
  committed registry: re-saved file + event + `design_stripped` on the self
  receipt); on the exhausted/rejected exit the persisted file is the SESSION
  DRAFT that `Role.save_self_config` saved unconditionally at session end
  (BEFORE the loop revealed the patch outcome), and it is left UN-healed there
  — it is NOT the pre-session file (K1, doc-erratum batch 18 per `docs/8` §1);
  the exhausted-exit heal plus a delayed
  save remain backlog (`docs/7` §6.1). Meanwhile `warn_dropped_workspace_edits` fires
  a loud `deep_edit_dropped` event (`phase="exhausted"`) when that path discards
  non-empty workspace edits. Healing removes names only; it never adds, restores
  or rewrites source. `unregister_component` never edits the design itself — when
  the removed name is still selected it merely *says so* in its return text.
- **Startup integrity and the load layer (batch 14)**: three checks close the gap
  between "the design says X" and "the session really has X".
  `preflight_tools` (startup, fatal unless `loop.toolset_preflight: false`) now also
  AST-checks the **always-on** files, split by OWNER via `gan/framework/frozen.py`:
  a broken FROZEN plumbing file (`work/common`, `design`, `deep` — nobody in the run
  may modify it, and it degrades every role) fails fast, while a broken **role-owned**
  tool (`work/<role>/**`, an agent artifact) is reported in `always_on.owned` and
  handed back to that role instead. `repair_dangling_designs` (B15) repairs a role
  design whose `tools` slot names something the committed tree cannot deliver: the
  unreachable names are stripped, the previous file is preserved as
  `config.json.dangling-<ts>`, an event records it, and the strip rides that role's
  next self receipt (the actionable `render_receipt` note) — deliberately NOT fatal,
  because the design is a losslessly repairable agent artifact and it outlives the
  session (aborting undoes nothing). `load_tools` writes a **load report**
  (`$GAN_TOOLS_LOAD_REPORT`; task child via `assemble_task_env`, roles via
  `Role.run`) listing what actually imported; it is merged into the task toolset
  report / the role `assembly_report` and classified by owner, so a component that
  failed to load becomes an agent-fixable item in the next instruction while a
  broken frozen tool is escalated (`framework_failed`) and never handed to an agent
  that cannot touch it. Since B47-R1 the assembly and preflight reports also carry per-tool
   **observed-capability profiles** (static, advisory-only: raw writes / subprocess /
   dynamic exec, keyed by the dispatch stem) — the observed counterpart to self-reported
   `tool_info()` descriptions; capability-flagged modules cannot ride the registry path
   at all, so the live positive-signal surface is always-on owned tools. General rule
   these implement: **abort only when nobody can
  repair the state OR continuing would produce a silently wrong result; otherwise
  strip/repair, record, and feed the owner.**
- **No silent failures/degradations (fail-open must SPEAK; 拒绝所有静默失败/降级).**
  Fail-open is the availability contract; audit persistence is the truth contract.
  BOTH are required — fail-open is NEVER fail-silent. Every advisory path that
  changes behavior — a swallowed exception, a degraded mode, a missing audit
  artifact, a `None` that means "absent" — must leave a DURABLE trace in the
  audit stream (events.jsonl / the cm sidecar / a `soft_fail` event), at the
  point where the degradation happens. A bare `print` is NOT durable: child
  stdout is PIPE-captured and log windows truncate, so prints vanish exactly
  when needed. Rule of thumb: **an `except` that restores a default without a
  same-path audit write is a bug**; `except: pass` needs a comment naming the
  auditor that observes the case, and an optional subsystem's ABSENCE must be
  as visible as its activity (e.g. emit the ingest event even at zero events).
  Rationale (2026-10-12 real-API round): the driver-mode task child ran EVERY
  generation with no CM seam at all while three fail-open layers stacked into
  total silence (broker env omission + cwd fallback miss + a swallowed
  tuple-unpack AttributeError at the self seat) — only the offline record
  cross-check caught it. When introducing or editing such a path, add the
  degradation event in the SAME patch.
- Session state travels via contextvars (`gan/framework/context.py`), not function arguments.
- **Planner→evaluator isolation (batch 8): facts flow, rhetoric does not.** Every
  evaluator-facing channel is a NAMED PROJECTION (explicit allowlist transform) —
  `build_diff_summary` (ops-only since B9: `files` exited — changed files are
  an outcome fact owned by `meta_view.task_patch_files`, grant facts ride
  `ops[].paths`), `receipt.design.ops` (`_ops_summary`),
  `run_summary.meta` (`_EVALUATOR_META_KEYS`), `run_summary.receipt`
  (`_receipt_for_evaluator`: outcomes only, no `rejected_reason` full text which
  quotes the planner's artifacts), and `feedback_digest.responses`
  (`project_responses_for_evaluator`: issue_id/accepted/stance only). The
  planner's free text (`respond_issue.feedback`, grant `reason`) exists solely in
  the planner's own audit surfaces (session trajectory, events.jsonl for human
  review) and never rides back into any decision context. A NEW meta key or
  receipt field is invisible to the evaluator until its projection is extended
  on purpose (default-deny, mirroring the per-role access allowlist). The
  planner responds to issues with a REQUIRED structured stance
  (`response_kind`: acted / acted_differently / out_of_scope / disputed /
  deferred) — the evaluator calibrates on decisions, never on persuasion.
  **Artifact vs rationale (B29, batch 16)**: the ARTIFACT under evolution gets
  structural facts; the planner's RATIONALE gets nothing. `set_prompt` /
  `set_config(key="prompt")` record `prompt_change_facts` (`gan/framework/receipt.py`:
  chars/lines/prev_*/added_lines/removed_lines/similarity/changed_from+to/sha256_12/
  empty/equals_seed — numbers and hashes only, never text) and `DesignContext.record`
  stamps `target_role` for every design record (framework-owned, written after the
  caller's fields so it cannot be shadowed). Those facts are whitelisted into
  `build_diff_summary` and `receipt.design.ops`. The prompt TEXT is deliberately not
  projected: it is audit evidence the evaluator may fetch from the task trajectory
  (`read_trajectory` archives the child's input message verbatim, redacted only for
  benchmark/score strings), never an assertion pushed into its decision surface —
  the isolation is on the PROMPT surface, not on the read surface. This does NOT
  relax the `respond_issue.feedback` rule: its derived statistics stay banned.
- Evaluator feedback is **text** (a digest), not a numeric reward; the evaluator
  must not fit the benchmark score (blind score first, then reveal).
- The task agent has **no always-on tools**; its capabilities are all opt-in tools.

## Driver-mode field lessons (v5.1 real-API round)

Lessons from the first driver-mode rounds run end-to-end against a real
OpenAI-compatible gateway — each was invisible to the offline smokes:

- **Latency-dependent transport bugs need slow fakes.** `LocalLLMRelay` held a
  10s socket timeout across its whole pump lifetime: every response slower than
  10s was silently dropped while the proxy still completed and audited it
  (`llm_call 200`), and the C1 dispatch timeout (600s) then abandoned and
  retried into the same wall — an infinite loop in which only <10s calls ever
  landed. Fixed by lifting the timeout after connect. Offline smokes passed
  because the fake upstream answers in ~0ms; smoke transports against a
  deliberately SLOW fake (e.g. 15s) too.
- **`driver_done` must be exercised, not assumed.** The first round ever to
  reach `outer_done` then died at the final teardown line (`relay.close()` did
  not exist; rc=1 AFTER every artifact — boundary snapshot, checkpoint, scores,
  receipts, lineage refs — was durably written). Judge a round by its events
  through `outer_done`, not by the exit code alone.
- **Trajectory attempt-keying is construction-time.** Role instances receive
  the loop's per-instantiation attempt id at construction (factory arg), so all
  outer-level writers (session content, stop records, redaction) resolve ONE
  `<role>__<attempt>.jsonl`; `read_session` merges canonical + attempt siblings
  newest-first. Before the fix, construction bound the canonical name while
  session content resolved the attempt name — one session's record split across
  two files.
- **Deployment constraints that fail fast:** `output_dir` must be owned by the
  post-drop caller uid, outside `/tmp` (tmpfs shadowing) and outside the repo
  tree; symlink-based venvs (python → /usr/bin/…) fail the trusted-interpreter
  realpath check (conda or `--copies` venvs pass).
- **`score: null` with an invalid prediction is a task-quality outcome**, not an
  infrastructure failure — the lineage machinery records an auditable dead
  branch and the next outer plans from the parent's commit.

## Field lessons for AI agents (tool-channel discipline)

Lessons from a coding round that briefly concluded "the tool channel is
untrustworthy" — the session log later proved every anomaly was
self-inflicted. Recorded so the failure pattern is recognizable and
non-repeatable:

- **An unexpected tool result is a hypothesis to test, never a verdict.** The
  correct reaction to output that contradicts expectations is a SECOND
  INDEPENDENT CHANNEL (write command output to a log file and `read` it;
  re-run; grep the session log), not escalating to "the environment
  fabricates". A wrong verdict built on one channel then poisons every
  downstream decision.
- **Relative and absolute paths are the same file.** Edit results show
  workspace-relative paths, reads show absolute ones (`gan/...` vs
  `/root/HyperAgents/gan/...`). A path-display difference is not evidence of
  "two different file states"; neither is seeing the same path twice.
- **Do not cite your own memory as evidence about files.** Several
  "contradictions" resolved the same way: the memory of what was just written
  was wrong, the file was right. The previous turn's recollection is not
  ground truth — re-read the file (or `git diff`) instead.
- **Own your typos and hallucinated APIs.** A mistyped command name, a wrong
  function signature, or an invented script path executed faithfully by the
  tool is still your bug; blaming the channel doubles the error and blocks
  the real diagnosis (which, in every checked case, was ordinary: an extra
  brace, a miscounted fixture, an assertion reading the wrong channel).
- **Before declaring an output "foreign", compare its fingerprint with what
  you wrote** — suite prefixes, assertion names, diff stats (`git status
  --short` + `git diff --stat` names exactly which files changed). The
  confabulation risk peaks exactly when tired reasoning meets a long tool
  transcript; the check costs one command.

## Do NOT

- Do not commit `.env`, secrets, `venv_nat/`, `outputs/`, or large datasets
  (see `.gitignore`).
- Do not upload local-only test scripts (`scripts/local/`).
- Do not commit model-generated code from runs without review.

## Where things are

- **Current entry point (integrated)**: `docs/整合_框架简述和细节决策记录.md`
  (current-state framework + per-topic decision record), `docs/整合_待办记录.md`
  (open-todo ledger), `docs/整合_已办留档.md` (done archive + item→batch closure map).
- Historical record docs (archived 2026-10 under `docs/archive/`, content unchanged
  apart from in-place erratum annotations): `1_DGMH部署记录.md`, `2_GAN_plan.md`,
  `3_GAN深入设计说明.md`, `4_v1实现和v2改动.md`, `5_v3改动.md`, `6_v5改动.md`,
  `7_遗留问题与待办.md`, `8_工具待办与已办.md`, plus the tool-line audit trail
  (`工具管理审查.md`, `工具管理复核报告.md`, `工具管理线复核报告_2.md`,
  `gan_tools_*.md`, `注释审查报告.md`). Line-number citations to "docs/N L…" in the
  integrated docs refer to these archived files.
- Active design docs outside that consolidation: `docs/9_多域评测设计.md`,
  `docs/10_成本反馈闭环设计.md` (and `docs/cost_tem.md`).
- Environment/deployment record: `docs/archive/1_DGMH部署记录.md` and `README.md`

## Base license

HyperAgents base code is under `LICENSE.md` (CC BY-NC-SA 4.0); keep attribution.
