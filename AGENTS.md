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
  for the parallel self-improve threads and a preflight pool gate; named
  modules may export a ``METHODS`` dict of custom methods (harvested
  all-or-nothing with ``cm_methods_refused``, assembly > policy shadowing,
  invoked same-level/same-callee via ``handle.call``/``session.call_method``
  — the caller's own privilege, never an escalation), fail-open policy
  runner, trajectory index (``materialize_traj`` snapshots the seat's visible
  trajectories as pinned ``traj_seg`` blocks — one scan per session; the
  read tools and the ``ctx_`` faces share that single genid-qualified
  address space); STDLIB-ONLY import contract because it is
  snapshot-delivered into the task sandbox as top-level ``cm``).
- `gan/tools/` — **always-on** callables: `work/<role>/` (role behavior tools),
  `work/common/` (frozen plumbing: `edit_source`, `read_file`, `list_dir`, `grep`,
  `read_trajectory`, `read_session_trajectory`, `list_editable`, `list_components`),
  `design/` (shallow design
  operators incl. the read-only `get_config` design reader: config + gen-0
  seed + keys view), `deep/` (gated deep gate: `request_source_access` + `register_component` +
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
- `gan/design/` — **shallow, evolvable**: `schema.py`, `store.py`, `seeds/`. The
  seed files (`seeds/<role>.md`) are the framework-owned FACTORY DEFAULTS: in
  NO role's read or write roots (run-invariant, so the set_prompt seed-restore
  exemption cannot be moved from inside a session); the only agent-facing seed
  projection is `get_config`'s `seed` field. An existing design config is
  authoritative and never re-seeded.
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
# GAN dual-loop (paper_review minimal) -- sandboxed driver ONLY: launch as a
# normal user via `sudo` with the three *_SANDBOX_USER env vars (see README
# "Environment deployment"); `--in-process` is disabled; one --domains per run.
sudo GAN_TASK_SANDBOX_USER=rsi_gan_task \
  GAN_PLANNER_SANDBOX_USER=rsi_gan_planner \
  GAN_EVALUATOR_SANDBOX_USER=rsi_gan_evaluator \
  venv_nat/bin/python scripts/run_gan.py --repo_root "$PWD" \
  --domains paper_review --subset _filtered_100_train \
  --num_samples 2 --outer 1 --inner 2 --output_dir /opt/rsi-gan/runs/gan_paper_review

# original HyperAgents domain eval (model is explicit; resolved from models.yaml)
python -m domains.harness --domain paper_review --model openai/gpt-4o \
  --run_id demo --subset _filtered_100_train --num_samples 2
python -m domains.report  --domain paper_review --dname ./outputs/demo
```

Tests / verification scripts are kept locally under `scripts/local/` (gitignored).
The tool-management regression suites (per-role registries, deep gate, P→E
projections, ledger write gate) live there too: run `venv_nat/bin/python
scripts/local/run_tool_regression.py` before and after touching `gan/tools/`,
`gan/registries/`, `gan/framework/{access,task_execution,receipt,loop,
scores,checkpoint,ledger}.py`, `gan/design/store.py` or the
design operators — seven suites (deep gate / batch-6 unification / batch-8
isolation / c-dispatch / dynamic-keys / ledger-gate / session-universe), one subprocess each.

## Offline analysis tools (analysis/)

Two offline analyzers under `analysis/` read finished GAN run dirs (`scores.jsonl`,
`runs/<genid>/`, `logs/events.jsonl`, `ckpt/`) — **offline only** (no network,
no LLM calls), run with `venv_nat/bin/python`, and write artifacts into
`<run_dir>/score_analysis/`. The base `analysis/*.py` scripts consume the DGM-H
`archive.jsonl` + `gen_<id>/metadata.json` layout and do NOT fit GAN run dirs
directly; the sanctioned bridge is to reuse their computation/rendering cores
with a GAN-native data layer.

- **`gan_scores_extract.py` — score extraction + analysis** (per run:
  `scores_extract.csv` / `REPORT.md` / `significance.txt` / `progress.png`).
  Reuses `analysis/analysis_utils.py` (`save_significance_tests`,
  `compute_bootstrap_ci`) verbatim. ONE score per run (named exactly `score` —
  per-row 0/1 + a `method` provenance column), computed by the run's OWN
  online rule as self-described by `report.json.score_mode`:
  `docker_resolved` → the broker's container verdicts; `lenient` → verdict
  regex extraction via the SAME `gan.framework.verdict.parse_verdict`
  (paper_review; offline == online by construction); anything else
  (`strict`) → exact lowercase label match (search_arena and the other label
  domains, the current online rule). Undecided parses count as wrong; the
  denominator never shrinks. Because the offline score reuses the online
  rule, `report_crosscheck` (recomputed vs report.json `total_correct`) is a
  true offline==online probe — a mismatch means implementation drift, not a
  convention difference. Ground truth is auto-disambiguated by matching every
  observed `question_id` against the `domains/<domain>/dataset*.csv` files.
  Per-generation AND per-question tables (systematic-error localization),
  evaluator `predicted_score` calibration, and the significance file only
  include runs with n≥2 samples (n=1 runs are noise). The search_arena online
  rule change (regex extraction, paper-style) is DEFERRED — the draft cascade
  and landing steps are archived in `docs/17_polyglot小规模实验轮修复.md` §10.5;
  the offline tool needs NO change when it lands (score_mode dispatch follows
  the runtime self-description).
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

### Design depth: shallow vs deep

- **Shallow design** = each agent's single config JSON in `gan/design/` (one `tools` slot per role; legacy aliases: `skills`, `eval_points`). Shallow operators may set **existing** keys, **declared dynamic keys**, or select/deselect **registered** tools.
- **Dynamic keys** are declared via `add_config_key` in `gan/design/schema_ext/<role>.json`, with the consumer registered in the SAME patch — declaration and consumer roll back together; undeclared/consumerless keys are stripped by the key-heal at persist. There is no `mint_operator`.
- **Depth classification** is by what a change touches:
  - *shallow* — edits the design config list only;
  - *deep delete* — removes the registry entry + source file via `unregister_component`;
  - *deep add/modify* — new tool or logic; must touch source via the gated `gan/tools/deep/request_source_access`.
- After a `modify` grant, planner/evaluator edit their granted copies with `edit_source`, confined to the instance workspace `src/`. Roles are never given raw `bash` (it cannot be confined). A repeated `request_source_access` on an already-granted path does NOT re-copy from `code_root` — in-scope edits survive; pass `refresh=true` to discard them and re-sync.
- **Deep-write recovery (4 layers, so a deep modification can actually land):**
  1. *copy* — `AccessBroker._safe_copy` writes a temp sibling + `os.replace` and only tries `copystat` best-effort, so a refresh can never fail with `Operation not permitted` just because the workspace copy belongs to another uid (the old `shutil.copy2` did).
  2. *backup* — every overwrite (`refresh=true`, session reset, `clear_workspace` at an outer boundary) copies the actually-EDITED files to `workspaces/_backup/<role>/<instance>/<seq>/` **outside** the purged tree, logs `workspace_backup{files,count,reason}`, and names the discarded files in the tool's answer. Nothing a session wrote is deleted silently any more.
  3. *merge* — each grant snapshots the baseline it copied FROM into `workspace/.gan_base/<rel>`. When the baseline has moved by commit time, `build_patch_from_workspace` runs `git merge-file -p <workspace> <grant-time base> <current repo>`: clean → the merged text becomes the b-side (the edit lands on top of the moved baseline); an unedited stale copy emits **no** diff (it used to emit a silent REVERT); a true same-line conflict keeps the plain diff and is recorded (`patch_baseline_drift` event + `broker.last_patch_report`).
  4. *visibility* — one unified diff per file means a registry JSON written by both `update_component` and `edit_source` lands only ONE write. `gan/patch.py:multi_writer_paths` derives that from the existing op records (no new IPC surface) and both receipts gain `multi_writer{path: [tools]}` + a `same_field_overwrite` event, so a lossy session is auditable.
- The task agent has **no always-on tools**; its capabilities are all opt-in tools.

### Ablation switches (`docs/11_消融实验设计.md` is the authority)

- Keys live in `gan/framework/loop.yaml`: the `ablation.*` block (A/B/C/D/E groups) plus `source_access.deny_deep` (B1). CLI: `--ablation KEY=VALUE` (repeatable; `deny_deep` is aliased to `source_access.deny_deep`). The driver forwards the merged section to `gan.outer_worker` as an explicit `--cfg-overrides` JSON argv arg (never env).
- Enforcement is **split across the process boundary** under the fix3 sandbox (see "Sandboxing & activation"): A1–A6 / C1–C4 / D / E are projections computed in `loop.py` before the role call and cross as plain args; B1 / B2 / B3(deep) are enforced by the root-side `AccessBroker` the child reaches via RPC; B3(shallow) / B4 are enforced by the design tools' `shallow_op_error()` **inside the child**, reading the flags mirrored onto `_RemoteBroker`. Moving a check from one side to the other without a cross-process test silently voids that arm.
- Guards: `scripts/local/verify_deny_deep.py` (B1 in-process, 19 assertions) and `scripts/local/test_b1_cross_process.py` (B-group over real IPC, 14 assertions).

### Design healing (self-improve exit) — batch 35, B26 phase 2

- The role design file is written by the SESSION (`Role.save_self_config`, at
  session end, BEFORE the loop learns the patch outcome), so EVERY self-improve
  exit heals it against the **committed** tree: `loop._heal_self_design` runs on
  the success commit, on the exhausted/rejected exit, on the empty-patch exit and
  after a failed apply. Both directions are legality-only (never an effect
  review, so a deliberate shallow edit is never touched):
  - **forward** — `heal_design_keys` + `heal_design_slots` strip names/keys the
    committed tree cannot deliver (`design_dangling_stripped`,
    receipt `design_stripped`);
  - **reverse** — `heal_design_removals` puts a selection BACK when the session
    scheduled a deep removal (`unregister_component`), dropped the name, and the
    component is *still* committed (the removal never landed:
    `design_selection_restored`, receipt `design_restored`). A forward-only heal
    can never see this state — nothing is *dangling* when the design merely stops
    declaring a live component.
  - Evidence is structured only: the session `records` + the role design as it
    was BEFORE the sessions ran (`loop._self_design_pre`; without it a this-session
    `deselect_component` record is the only admissible evidence — absence alone
    never licenses adding a name back).
- The stage makes the two stories distinguishable: `design_dangling_stripped`
  carries `stage` = `self_improve_success` / `self_improve_exhausted` plus
  `outcome` = applied / exhausted / rejected / empty_patch / apply_failed.
- **Convergent & idempotent** (nested / chained heal): both directions share ONE
  predicate ("does the committed tree resolve this name"), so forward pruning
  leaves `tools subset-of resolvable` and the reverse direction may only re-add a
  name it already resolved — no pass can manufacture a new dangling reference, a
  second pass is a no-op, and chaining into the next outer's startup
  `repair_dangling_designs` cannot oscillate. `loop._self_design_residue`
  re-runs all three stages on the persisted result as a tripwire: any second-pass
  change is reported (`design_heal_not_converged` + `design_heal.converged=false`).
- The DRAFT the session proposed is snapshotted before the heal overwrites the
  file (`ckpt/design_drafts/<role>/outer_<N>_draft.json`, audit / replay only,
  never a lineage parent): **draft = what the attempt proposed; `design/<role>/
  config.json` = the aligned state that becomes the next outer's parent**. The
  facts ride the self receipt (`design_stripped` / `design_restored` /
  `design_heal{stage,outcome,converged,draft_path,ckpt_path}`). `save_self_config`
  stays where it is — `DesignStore.save` is atomic (tmp + `os.replace`), so the
  heal's re-save opens no new torn-file window.

### Knowledge base (pull-based)

- `gan/components/<role>/knowledge/*.md` are **data** — not registry entries, no `name=stem` contract, never executed.
- The planner authors task-side notes via the deep patch channel; the whole committed base is materialized into the task sandbox each generation (`.gan_runtime/knowledge` + `GAN_TASK_KNOWLEDGE_DIR`; per-note/total caps, skips reported).
- The task agent reads on demand via the opt-in `knowledge` tool. Planner/evaluator read their own (and task-side) notes through the normal access channel (`request_source_access(view)` + `list_dir`/`read_file`) — never an un-audited side door.

### Instances & scheduling

- *instance* = role + workspace + trajectory. The task agent refreshes every **inner** generation (per `genid`); planner/evaluator every **outer** generation (`loop.py:_refresh_roles`, `*_factory` args from `build.py`).
- Each instance gets `toolsets/<role>/<inst>/` and `trajectory/outer_<O>/...jsonl`; tooling is re-assembled with `clear=True`, so deselected components actually disappear.
- The source workspace is keyed by role instance (per outer): `workspaces/<role>/outer_<O>/src`, shared by that outer's plan/evaluate/self-improve passes, and cleared at each outer start.
- Single-chain: no accept/reject and no parent selection for planner/evaluator. Each outer saves a checkpoint; the next outer starts from the previous checkpoint's design. `outer_improved` is logged, not enforced.
- The two role self-improve sessions inside one outer run in **parallel threads**; their write surfaces are seat-disjoint by `frozen.SEAT_WRITE`. Only the fast patch apply+commit serializes on `code_repo._TREE_LOCK`, staging exactly the patch's own files (attribution-clean).
- Credentials travel as an explicit `api_key` argument and the toolset load-report sink as an explicit parameter — never process-global `os.environ` swaps; `AccessBroker.last_result` is thread-local.

### Output layout — `gan/framework/paths.py` is the single source

| path | contents |
|---|---|
| `ckpt/` | recovery; per-run git tree `code/`; `code.json` manifest; `design/`; `design_drafts/<role>/outer_<N>_draft.json` (audit-only pre-heal self-design snapshots) |
| `logs/` | `events.log`, `*_tree.log`, `*.log` |
| `trajectory/outer_<O>/…` | role trajectories; the outer root label is INSTANCE-DERIVED (`outer_` + instance suffix: main `outer_N` → `outer_N`, outer-select eval instances `outer_N_eval` → their own tree — one rule shared by `BaseRole.traj_outer`, `role_process._prepare_paths` and the role broker's ACL write root); outer-level files are attempt-keyed `<role>__<attempt>.log` (one per loop instance, assigned at role construction so content + stop records share one file; directly-constructed roles use `<role>.log`; readers merge canonical + attempt siblings, newest first; outer-select eval rounds archive the evaluated task child through the same `collect` pipeline as `trajectory/outer_<N>_eval/eval_outer<N>_s<M>/task.jsonl` (+ sidecar), advisory on failure) |
| `scores/scores.jsonl` | decision records (JSONL, one per row) |
| `runs/<genid>/` | small evidence: `packet.json`, `eval.json`, `feedback_digest.md`, `patch_receipt.json`, `patch_proposed.diff` |
| `work/<genid>/` | ephemeral, pruned (`utils/prune.safe_rmtree`: best-effort sweep, failures RECORDED — `rmtree_failed` event with the survivor list — never raised; root-phase payload extraction normalizes ownership to the run owner so the tree stays cleanable) |
| `workspaces/<role>/outer_<O>/` | per-role workspaces |

`ckpt/checkpoint.json` = the latest checkpoint, overwritten atomically; outer boundaries also write an immutable `ckpt/outer_<N>.json` (archived as `ckpt/outer_<N>_<ms>.json` when the same outer re-runs). `restore_trees` appends an `op=reset` event instead of truncating the log.

### Trajectory access

- Every agent writes structured **JSONL** (one file per instance) via shared `utils/trajectory_log.py`; `gan/framework/trajectory.py` (frozen — redaction is part of the trust anchor) archives + redacts task trajectories and also redacts role sessions (`Role.run`).
- The visible set is explicit: `AccessContext.trajectory_genids` (evaluator = current + parent; planner = parent; self-improvement = this outer's generations + the direct parent). `read_trajectory` reads visible task generations; `read_session_trajectory` reads the role's own sessions over the declared `AccessContext.session_outers` (self seat: this outer + the previous; other seats never reach it — labels: genid-qualified for the current, `outer_<O>/`-prefixed for extras, ONE rule shared by materialization and the tool fallback).
- The current generation's task trajectory is archived **before** evaluation; the fixed-root eval round's task trajectory is archived through the same `collect` pipeline when the round completes, under the eval round's OWN tree (`trajectory/outer_<N>_eval/` — planner sessions included), advisory on failure — it is not a decision generation.
- Rendered decision views mark their cap explicitly (`...[render truncated at max_chars=N]`): a capped VIEW is distinguishable from a short archive (field audit small_1030_095410 — a default-cap read was misread as truncated files). `ts` is authoritative; `time` is UTC for every writer (driver/loop/sandbox TZ views differ by design).

### Sandboxing & activation

- Each generation runs in a **minimal allowlist copy** `work/<genid>/repo` (agent runtime + domain package, datasets excluded; patched inside the copy when a deep patch exists). Benchmark labels are read by the harness from `repo_root`, passed only via the CLI `--dataset_root` argument to the parent harness — never written into the task child's env/payload; `GAN_DATASET_ROOT` is scrubbed from every child env.
- **Secure sandbox boundary (fix3-main-sync)**: the GAN loop runs ONLY through the sandboxed driver (`scripts/run_gan.py` / `gan.driver.run_gan_driver`, launched via `sudo` as a normal user with the three `*_SANDBOX_USER` env vars — see README "Environment deployment"); the legacy `--in-process` option is hard-disabled and only ONE `--domains` value is accepted. Every worker is dropped to a dedicated UID (`setpriv --bounding-set=-all --no-new-privs`) inside a bubblewrap namespace (`--unshare-all --unshare-net --cap-drop ALL`, a synthetic credential-free `/etc` from `gan/framework/sandbox_etc.py` — which also projects package-owned `/etc` subtrees such as `/etc/java-*-openjdk` so OpenJDK's `conf/security` symlinks resolve), with role/seat-scoped write roots granted and reclaimed via POSIX ACL (root-side brokers reconcile stale ACLs after a crash). Provider credentials never leave the root-side `ParentLLMProxy`: children get scoped `sk-gan-` tokens delivered as sealed memfds (never argv/env), planner/evaluator run as separate seat processes hosted by `gan/framework/role_process.py` (the one sanctioned framework→roles import). Per-domain sandbox profiles in `domains.yaml` are fail-closed (missing/disabled profile or `gpu != none` refuses the run). Resource budgets (cgroup/CPU/mem/pid/disk, #7) remain deferred.
- **Cross-process contract (role seat ↔ broker, fix3)**: because a role session runs in a child and its tools call back over IPC, three things are NOT automatic — (a) `role_process._validated_result` validates each role method's return dict by **exact field set**, so a field added to `planner.plan()` / `base_role.self_improve()` / `EvalContext` must be mirrored there or *every* result is rejected (plan -> `planner_failed`/`inner_invalid` with a null score; self-improve -> `self_improve_error` degrading to `kept`, i.e. the role never evolves); (b) `_RemoteBroker` must mirror the broker's ablation flags (`deny_deep`/`deep_view_only`/`shallow_prompt_only`/`disable_shallow`, carried in `broker_info`) or the child's tool-layer checks see all-false and B3/B4 silently no-op; (c) an exception raised in the broker reaches the child only if `_RemoteBroker._rpc`'s name→type map knows it, else it degrades to a generic `RuntimeError` the catcher misses (this is how B1's `SourceAccessDisabled` broke).
- **Activation**: always-on → `gan/tools/`; opt-in → `gan/components/` (registered, selected by design config). Plumbing (`deep/`, `design/`, `work/common/`) is frozen; only `work/<role>/` and the task components/registry are evolvable.

### Model config

- **Model config**: single source is `gan/framework/models.yaml`, read only by `gan/framework/models.py` (`resolve`/`resolve_entry`/`resolve_section`/`describe`) — pure lookup, no env, no fallback, no precedence chain. Sections: `gan.{task,planner,evaluator}`, `dgmh.{meta,task}`, `domains.<role>` (non-task domain roles only). Entry = plain model string or mapping `{model, reasoning_effort}`; `resolve()` returns the model STRING; `resolve_entry()` returns the normalized entry and validates at startup (unknown keys / unknown `reasoning_effort` fail before any run). `reasoning_effort` contract: `low|high|max` (NOT the OpenAI enum; defined as `agent.llm.REASONING_EFFORTS`), travels EXPLICITLY as function/CLI argument — never env: roles threaded `build factories -> Role -> chat_with_agent -> get_response_from_llm`; task child `DomainTaskRunner -> run_harness_and_report -> domains.harness --model/--reasoning_effort -> stdin payload -> TaskAgent` (broker mode: fixed in root-side broker config, same models.yaml entry). **The task agent shares the driver's model** (explicit `domains.harness --model` arg). Effective config recorded at runtime (`events.jsonl: model_config` for GAN, `[model_config]` log line for DGM-H, `Node.meta["model"]` per generation; proxy audit adds caller's `reasoning_effort` per call). Do not read `models.yaml` anywhere else.

### Write path, ledger, and audit

- **Front-loaded write policy**: `edit_source` is the only official source-edit entry and uses the centralized frozen authorization helper before mutation. Dispatch validates declared `edit_source` inputs before invoking tool code. Commit and load/preflight layers also apply a conservative AST policy rejecting raw writes, subprocesses, and dynamic execution in agent-owned Python. Framework plumbing is explicitly exempt because it legitimately writes state; the AST policy is defense in depth, not a replacement for a real sandbox.
- **Ledger write gate (B46 step-1)**: decision-grade artifacts (`scores/scores.jsonl`, design configs, checkpoints) are written only through guarded framework entry points that require the per-run `WriterToken` (`gan/framework/ledger.py`, minted once in `build_gan_loop` and threaded explicitly — never env, never module-global). Unguarded writes raise `LedgerError`; agent-owned source importing a guarded writer module (`gan.framework.scores` / `gan.design.store` / `gan.framework.checkpoint`) is refused at the registry and commit gate via the same AST issues channel (`observed_ledger_writer`). The token is an identity, not a secret — file-level tamper DETECTION is deferred step 2 (hash chain, unchanged trigger); step 2's role (batch 4): the detection compensation for application-step BYPASS — the write side's existence control (grants / AST gate / `WriterToken`) is strong but heuristic, the read side's is absolute. Regression suite: `scripts/local/regress_ledger_gate.py`.

### Data flow and boundaries

- **Permissions = capability by existence (batch 4 axiom)**: reads are physical absence without a grant; writes are not applied without capability. Enforced by what the framework MOVES (`AccessContext` role+seat; REQUIRED no-default on `set_access_context` / `frozen.write_roots` / `frozen.is_allowed` / `AccessBroker.grant` / `code_repo.check_patch|apply_*`; no `legacy` seat; `frozen.any_seat_allows` is the only deliberate union query — classification, never authorization; `AccessBroker` refuses repo-root / self-containing / oversized grants; local check: `scripts/local/test_frozen.py`).
- **Reserved cost keys & context-management seam (docs/10 phases C/D)**: authority is `gan/framework/cost_keys.py` (`validate_cost_config` shared by schema/set, `effective_all` the ONLY clamp, `cost_keys_applied` events); task budget keys ride the harness stdin payload (never env). Phase-D seam: `agent/llm_withouts._counting_llm` is the single per-turn assembly point — `CMSession` (per-seat, snapshot-loaded) rewrites only what is SENT; per-entry copies keep canonical history + raw trajectory rows untouched; modes `passthrough` (kill switch) / `slice` / `compact_conservative` / `custom`; audit via `cm_violation` / `cm_*` events and the `cm_events.jsonl` sidecar; runtime `cm` savings are DERIVED, never billing.
- **Planner→evaluator isolation**: every evaluator-facing channel is a NAMED PROJECTION (explicit allowlist transform) — summary is ops-only (files exited; changed files are an outcome fact owned by `meta_view.task_patch_files`, grant facts ride `ops[].paths`), receipt exposes only outcomes (`receipt.design.ops`), run summary exposes only `run_summary.meta`/`run_summary.receipt` (no `rejected_reason` full text, which quotes the planner's artifacts), feedback digest exposes only `issue_id`/`accepted`/`stance`. A NEW meta key or receipt field is invisible to the evaluator until its projection is extended on purpose (default-deny, mirroring the role access allowlist). The planner responds to issues with a REQUIRED structured stance (`response_kind`: acted / acted_differently / out_of_scope / disputed / deferred) — the evaluator calibrates on decisions, never on persuasion.
- **Artifact vs rationale (B29, batch 16)**: the ARTIFACT under evolution gets structural facts; the planner's RATIONALE gets nothing. `set_prompt` / `set_config(key="prompt")` record `prompt_change_facts` (chars/lines/prev_*/added_lines/removed_lines/similarity/char_similarity/char_survival/changed_from_to/sha256_12/empty/equals_seed — numbers and hashes only, never text), and `DesignContext.record` stamps `target_role` for every design record (framework-owned, written after the caller's fields so it cannot be shadowed). Those facts are whitelisted into `build_diff_summary` and `receipt.design.ops`. The prompt TEXT is deliberately not projected: it is audit evidence the evaluator may fetch from the task trajectory (`read_trajectory` archives the child's input message verbatim, redacted only for benchmark/score strings), never an assertion pushed into its decision surface — the isolation is on the PROMPT surface, not on the read surface; the AUTHOR side reads its own draft via `get_config` (same object the R2 gate compares against). This does NOT relax the `respond_issue.feedback` rule: its derived statistics stay banned.
- Evaluator feedback is **text** (a digest), not a numeric reward; the evaluator must not fit the benchmark score (blind score first, then reveal).

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
  `docs/10_成本反馈闭环设计.md`, `docs/11_消融实验设计.md` (ablation matrix +
  switch→consumer authority, incl. §3.1 new-sandbox execution-position map)
  (and `docs/cost_tem.md`).
- Environment/deployment record: `docs/archive/1_DGMH部署记录.md` and `README.md`

## Base license

HyperAgents base code is under `LICENSE.md` (CC BY-NC-SA 4.0); keep attribution.
