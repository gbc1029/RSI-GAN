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
  `issue_keys.py` + `issue_ledger.py` + `issue_checks.py` (cross-outer issue
  continuity: the closed ``<domain>.<facet>`` vocabulary is the ONLY stable
  identity a defect has — ``issue_id`` is a per-round handle the framework
  prefixes with the generation (``g<genid>/i-1``), and every outer boundary folds
  raises / verdicts / stances into ``ckpt/issues/ledger.json``; the next round
  receives the open keys as an id-free, prescription-free ``carried_over`` block,
  so history survives while the incumbent evaluator re-diagnoses instead of
  re-issuing a stale fix. ``issue_checks.py`` supplies the PER-ISSUE mechanical
  truth (three states from recorded node facts) that ``verdict_stability``
  consumes),
  `loop.py`, `tree/`, `reward/`, `cm/` (context-management frozen core —
  block model, primitives, the per-turn seam (``assemble_turn`` DISPATCHES
  only; the pipeline itself is the baseline assembly ``assembly.py`` — one
  named preset per ``cm_mode``, composed over the session's frozen primitive
  methods; invariants live in the primitives, never in the pipeline source;
  the SENT TEXT of every form and the saving number are single-sourced in
  the session: ``view_text`` (context-free forms delegate to ``bake``; dedup /
  derived_host are session-anchored) and the accounting funnel books `saved`
  as a SIGNED TRANSITION delta from the previous rendered form (`cm_window_k`
  counts LLM CALLS — one call = assistant reply + its tool-output entry, so
  `k` = 2k entries; the window, the compaction tier and dedup all share ONE
  membership rule, `_window_kept_ids`, docs/19 §12.56; a dedup form that can
  no longer apply is DEMOTED to full with an exact refund rather than left
  claiming a substitution, docs/19 §12.58; every `cm_view` carries `view_seq`
  / `view_in_turn`, and a view rolled back by the fail-open replay is
  RETRACTED by its own `cm_view_retracted` event rather than rewritten, so
  `sum(saved_chars) - sum(retracted)` is the authoritative turn delta and "the
  last view" is decided by the ordinals, never by stream order, docs/19 §12.62) — primitives
  carry no ``saved``, a repeated/no-op op books 0, a restore refunds, and
  ``pin`` on an already-elided block restores it to FULL (dedup the only
  exempt non-full pinned form; docs/19 §12.40 — the P1 ``pinned_form``
  invariant is therefore enforced at the entry, never left for the
  reconcile to misattribute to an innocent module); a DERIVED node may be
  consumed ONCE (depth-N derivation is legal) and the consumption pin —
  which keeps the lineage a tree, ≤1 consumer per node — is EXEMPT from
  the reconcile's fourth question via the primitive's own `pinned`
  evidence set (docs/19 §12.52, N1); a block's ``msg_index`` is its RENDER
  ADDRESS whose ONLY legal domain is the turn's CANONICAL history — exactly
  (same length, same ``(role, text)`` per index): the framework registers the
  canonical itself at the turn boundary (``_open_turn``), a mismatching list
  from agent code is sent but never registered
  (``cm_register_history_refused``), and ``render``/``finalize_turn`` refuse to
  render a non-canonical list at all (``cm_render_off_canonical`` +
  ``cm_view.stats.send_unrendered`` + ``summary().send_unrendered_turns`` ⇒
  cost key ``cm_send_unrendered``); the address map rides the
  snapshot/restore, and the sixth reconcile question (``msg_index_moved``)
  allows only same-text / own-baked-form / unbound-binds-to-own-text moves
  (docs/19 §12.53, §12.55 — the old "canonical prefix + appended entries"
  allowance was removed because an appended entry never enters the next turn's
  canonical, so its address was a misaddress one turn later, finding C),
  and ``resolve()`` also loads the P2 NAMED agent-owned assembly (``cost.cm_assembly``
  design key → ``gan/components/<role>/cm/<stem>.py``, delivered as
  ``cm_assembly.py`` / policy as ``cm_policy.py`` under FIXED names so the
  child env pins are static; load-once per session, and the per-turn exec
  budget covers LOAD+execute — module-level code is agent code, so a
  top-level hang is a load-phase timeout, never a session hung outside
  every budget; failure semantics (docs/19 §12.17, UNIFORM for both
  agent-owned module kinds): load-once per session with the failure cached
  (a mid-session edit must not change the seam; load-once caches the
  MODULE — apply/assemble still run every turn), ONE strike disables the
  module for the session (load crash, load timeout, or run failure — a
  crash on byte-identical retries is deterministic), plus a loop-level
  kind-keyed fingerprint latch (clamped keys + module bytes, released when
  either changes; descriptor flags ``assembly_disabled``/``policy_disabled``,
  both ADDITIVE), fail-open to the preset — every fallback leaves durable
  events (with ``phase``) + ``degraded``/``assembly_disabled`` cm_view
  stats; policy failures ride ``policy_report`` → ``recent["cm_policy"]``
  into both self-improve prompts (mirror of the assembly report channel);
  the agent-owned assembly receives the P2 ``SessionPortal``
  (narrowed facade) and a per-turn BOUNDARY COPY of msgs (deepcopy; the
  canonical list is unreachable by construction and the fallback preset
  replay always runs on the pristine real list — docs/19 §12.36; the P1
  texts layer survives as the escape-tier tripwire, so
  ``canonical_mutated`` can no longer fire for named turns), and the
  portal's ``register_outgoing`` returns a SNAPSHOT copy of the block —
  the live universe object is unreachable by construction too, with the
  reconcile's fifth question (registration identity) as the escape-tier
  backstop and the overrun counters riding the P1 counters tuple
  (docs/19 §12.38) — and agent-owned turns are reconciled at turn end
  (P1 snapshot/reconcile/restore on BOTH the named-assembly branch and the
  custom policy tail — also the backstop for attribute-level escapes: a
  ``_s``-escape on the policy path is caught, restored, contract-struck;
  the baseline stages' refs are folded before the policy snapshot so the
  reconcile does not false-positive on their form changes — docs/19 §12.18);
  in-process p/e seats resolve the SAME named slots
  workspace-first at session build (``cm_wiring.resolve_named`` reached from
  ``loop._build_cm_session``, no sort-order discovery — an unresolved claim
  is a loud ``cm_module_unresolved`` and custom degrades to the slice preset
  with its keys rewritten), while DRIVER-mode p/e seats build the session in
  the CHILD from the parent-computed descriptor (``role_state.cm``,
  protocol 2 → ``cm_wiring.build_from_descriptor``; stems resolve
  child-side, workspace ``<instance>__<seat>`` first then committed), with
  per-path sys.modules keys
  for the parallel self-improve threads and a preflight pool gate; named
  modules may export a ``METHODS`` dict of custom methods (harvested
  all-or-nothing with ``cm_methods_refused``, assembly > policy shadowing,
  invoked same-level/same-callee via ``handle.call``/``session.call_method``
  — the caller's own privilege, never an escalation), fail-open policy
  runner, and ONE shared plan-seat wiring (``loop._plan_seat_cm_open/_close``)
  for BOTH the main plan seat AND the fixed-root evaluation's throwaway
  planner (``outer_<N>_eval``): design-clamped cost keys, the cm_mode stamp,
  the CM session/descriptor, the latch, and ``cost_keys_applied`` all apply
  there too — while evaluation stays cost-isolated (no ``cost_sessions``
  row); the latch/report tables are keyed ``(role, seat, scope)`` (main vs
  eval) and the fingerprint watches the EVAL seat's own workspace bytes,
  so the two seats can never release each other's latch (docs/19 §12.45),
  trajectory index (``materialize_traj`` snapshots the seat's visible
  trajectories as pinned ``traj_seg`` blocks — one scan per session WHEN
  the seat's sandbox can open the files; the read tools and the ``ctx_``
  faces share the same genid-qualified ADDRESS space, but the single-scan
  SERVING tier exists only where a materialization happened (a DRIVER-mode
  p/e seat has no task-trajectory mount — docs/19 §12.48: decision = no
  mount — so its ctx faces stay empty and ``read_trajectory`` serves Tier 2
  from the same visible files); the seat layer bounds what the budgets
  cannot (CM-03
  residual): an INACTIVITY watchdog (``loop.role_inactivity_timeout_s``,
  no-frame based — its duration is DERIVED as
  ``retry_budget + max(llm_client_timeout_s, tool_call_timeout_s) + 300s`` and an
  explicit value below that bound REFUSES the build. The RETRY TERM was added
  2026-11-11: a retrying call is ONE frame-free stretch, because the child writes
  its ``llm_call`` row only after the whole retry sequence ends (measured:
  ``attempts: 3, duration_s: 198``), so the bound is one call INCLUDING its
  retries, never two operations. Leaving it out was harmless only while the
  budget was ``max_time=120`` against a 300 s margin; with 300 s it would have put
  a legitimate retrying call exactly ON the watchdog (docs/19 §12.57); the kill
  event names ``last_frame`` and converges as
  ``planner_failed``/``evaluator_failed`` and is deliberately NOT a CM latch
  signal) plus a post-session hygiene restart when the session summary reports
  leaked worker threads (``thread_leaks``/``thread_leak_delta`` — conditional
  fields, absence = clean; framework plumbing threads are excluded by NAME
  prefix ``rsi-gan-``/``f5-telemetry``/``role-inactivity-watchdog``/
  ``litellm-logging``, so the
  census only sees what an agent module actually spawned, docs/19 §12.59; the
  census also reports WHICH threads: ``thread_leak_names`` is the multiset
  difference against the build-time name baseline, so a thread already alive
  when the session opened is never blamed on it — a bare ``delta`` was
  unattributable and cost two rounds of "something leaked, unknown",
  docs/19 §12.67); the task seat latches a failed named assembly OR POLICY for the
  REST of the generation (harness-injected ``GAN_CM_ASSEMBLY_DISABLED`` /
  ``GAN_CM_POLICY_DISABLED`` — the agent child is per-question);
  STDLIB-ONLY import contract because it is
  snapshot-delivered into the task sandbox as top-level ``cm``).
- `gan/tools/` — **always-on** callables: `work/<role>/` (role behavior tools),
  `work/common/` (frozen plumbing: `edit_source`, `read_file`, `list_dir`, `grep`,
  `read_trajectory`, `read_session_trajectory`, `list_editable`, `list_components`),
  `design/` (shallow design
  operators incl. the read-only `get_config` design reader, TWO modes —
  no argument: the keys view (static/dynamic keys with type/enum/consumer,
  present vs unset) plus the framework-owned task output-contract field;
  keyed: that key's value — `prompt` adds the gen-0 seed and
  prompt_equals_seed, enum-typed dynamic keys add the allowed values), `deep/` (gated deep gate: `request_source_access` + `register_component` +
  `unregister_component` + `update_component`),
  `assembly.py`.
- `gan/components/` — **opt-in** tool implementations, one tree per role (batch 6):
  `task/` (tools + the curated `task/knowledge/*.md` base), `evaluator/`,
  `planner/` (self-authored knowledge/tools); each tree may carry a `cm/`
  pool of THIN context-management wrappers (``ctx_index``/`ctx_fetch`/
  `ctx_usage`/`ctx_pin`/`ctx_note`/`ctx_view` — task selects only
  `ctx_usage`/`ctx_note`; its trajectory access rides the
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
  the runtime self-description). **Episode domains (balrog) have their own
  branch**: there is no `predictions.csv` and no `dataset*.csv`, so
  `extract_run` builds one row per EPISODE straight from `report.json`'s
  `question_points` (the truth the harness already computed), `attach_episode_truth`
  scores it by PROGRESSION in [0,1] (binary accuracy is meaningless here), and
  `episode_crosscheck` replaces `report_crosscheck`'s `total_correct` comparison
  with "recomputed mean vs the mean the loop recorded in `scores.jsonl`". Output
  is `write_episode_report` / `write_episode_plot`: per-generation means, a
  per-episode progression grid (systematic-failure localization), a per-TASK
  breakdown and an explicit list of aborted episodes — an infrastructure drop is
  never reported as a play-quality regression.
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
- **Shallow first, then ESCALATE (2026-11-11, `gan/design/seeds/planner.md` rule 4).**
  The seed used to leave the choice implicit, and the first evolution round showed
  the cost: after the evaluator judged the prompt rule ineffective, the planner
  rewrote the prompt AGAIN, and only on the third generation did it request a
  source grant (a generation then lost to an upstream 502). The seed now names
  the escalation triggers — "the evaluator says the prompt/config rule is
  ineffective or was never followed", "the same `issue_key` is `judged_unfixed`
  two rounds in a row", "`report_summary.behavior` shows the config edits changed
  nothing" — after which the planner must go to depth 2 in that round instead of
  re-editing the prompt. It is explicitly an ESCALATION rule, not "always patch
  source": with no such evidence the shallow path stays first, otherwise the
  shallow-evolution axis (`B3` shallow_prompt_only / `B1` deny_deep) stops being
  meaningful.
- **Deep-write recovery (6 layers, so a deep modification can actually land):**
  1. *copy* — `AccessBroker._safe_copy` writes a temp sibling + `os.replace` and only tries `copystat` best-effort, so a refresh can never fail with `Operation not permitted` just because the workspace copy belongs to another uid (the old `shutil.copy2` did).
  2. *backup* — every overwrite (`refresh=true`, session reset, `clear_workspace` at an outer boundary) copies the actually-EDITED files to `workspaces/_backup/<role>/<instance>/<seq>/` **outside** the purged tree, logs `workspace_backup{files,count,reason}`, and names the discarded files in the tool's answer. Nothing a session wrote is deleted silently any more.
  3. *merge* — each grant snapshots the baseline it copied FROM into `workspace/.gan_base/<rel>`. When the baseline has moved by commit time, `build_patch_from_workspace` runs `git merge-file -p <workspace> <grant-time base> <current repo>`: clean → the merged text becomes the b-side (the edit lands on top of the moved baseline); an unedited stale copy emits **no** diff (it used to emit a silent REVERT); a true same-line conflict REFUSES the patch and is reported (`patch_baseline_drift` event + `broker.last_patch_report`), and the session receives the refusal as a retryable reason ("refresh=true and redo") on BOTH seats instead of a silent empty patch.
  4. *visibility* — one unified diff per file means a registry JSON written by both `update_component` and `edit_source` lands only ONE write. `gan/patch.py:multi_writer_paths` derives that from the existing op records (no new IPC surface) and both receipts gain `multi_writer{path: [tools]}` + a `same_field_overwrite` event, so a lossy session is auditable.
  5. *EOL* — `_read_lines` reads in TEXT mode (universal newlines), so every diff line ends with LF while the working-tree file may hold CRLF bytes; `git apply` compares context BYTE-WISE and refuses each hunk with "patch does not apply". 168 of 253 tracked text files here are CRLF — including every `gan/registries/*.json` and `gan/design/schema_ext/*.json`, i.e. exactly what `register_component` / `unregister_component` / `add_config_key` must write (2026-11-01 `verify_deep_v2`: task-side patches to LF files landed 4/4, both planner self-improve registry patches were refused 0/2). `_file_eol` reads the terminator back from the BYTES and `_align_eol` re-terminates both diff sides with it (deletion hunks included), so the patch is byte-faithful without normalising the tree; guard `scripts/local/test_crlf_patch.py` (18 checks, asserts the pre-fix diff is refused and the aligned one applies against real `git apply`).
  6. *ACL mask* — the workspace root carries a DEFAULT ACL for the role, but a default ACL is MASKED by the creating process's umask, and the driver's private umask is 0o077: `copyfile`'s 0666 then lands as 0600, the inherited `u:<role>:rwx` entry becomes `#effective:---`, and the role cannot even READ its own granted copy (measured 2026-11-02 `verify_deep_v3`: `_read_lines(b_file)` `PermissionError` killed BOTH planner self-improve seats before the patch was ever built — `sudo -u rsi_gan_planner head` on the live copy = Permission denied). `_safe_copy` re-asserts `0o660` AFTER `copystat` (chmod rewrites the ACL mask, never the ownership), so the inherited role entry stays effective; guard `scripts/local/test_ws_acl_mask.py` (11 checks: a mask-zeroed control is unreadable, the granted copy is readable AND writable, refresh over a role-owned file still works).
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

### Code base lineage (task commits + role commits share one tree)

- **Blood line = the selected node's own commit**, but the *working* base is
  that commit plus the role self-improvements stacked directly above it:
  `loop._carry_role_commits`. A self-improve commit belongs to the RUN, not to
  any task node, so returning the bare parent silently reverted every earlier
  self-upgrade at the next parent switch (measured 2026-11-02 `verify_deep_v4`:
  planner's `f5ad5a5` — a 125-line tool plus its registry entry — vanished in
  outer 2, which then produced an `empty_patch` and the exit heal reported the
  name as "not registered"). Only `checkpoint.code.commit` was carrying it, and
  only the RESUME path reads that.
- **The lift is conditional, and the condition is what makes it safe**: a
  candidate tip is accepted only when it is a descendant of the parent AND every
  commit in `parent..tip` is a role commit (`_role_commits` or the
  `self-improve <role>` subject). Using the raw outer-entry HEAD would be wrong:
  LCB routinely selects an EARLIER node, and the entry HEAD then also carries
  the later task generations — a base that lineage says belongs to someone else.
  Unqualified ranges leave the base untouched, so this can only ADD role
  commits, never move backwards or mix sibling lines. Audited as
  `code_base_with_roles{parent_commit,base,role_commits}`; guard
  `scripts/local/test_role_base_carry.py` (18 checks incl. the gen0-selected
  leak and the sibling-line case).
- Node `meta["code_commit"]` still records the TASK commit only, so the lineage
  graph and ref reconciliation are unchanged by the carry.

### Knowledge base (pull-based)
- `gan/components/<role>/knowledge/*.md` are **data** — not registry entries, no `name=stem` contract, never executed.
- The planner authors task-side notes via the deep patch channel; the whole committed base is materialized into the task sandbox each generation (`.gan_runtime/knowledge` + `GAN_TASK_KNOWLEDGE_DIR`; per-note/total caps, skips reported).
- The task agent reads on demand via the opt-in `knowledge` tool. Planner/evaluator read their own (and task-side) notes through the normal access channel (`request_source_access(view)` + `list_dir`/`read_file`) — never an un-audited side door.

### Issue continuity across rounds and outers

- **Two identifiers, two jobs.** `issue_id` is a *per-round handle* (`i-1`, renumbered whenever a round has no parent list to continue — a fresh outer, a branch off `initial`). The framework prefixes it with the generation (`g<genid>/i-1`) in `loop._qualify_issue_ids`, so handles are globally unique; a bare `i-N` echoed by either role is normalized back to the parent's qualified id (`_normalize_issue_refs`) instead of silently becoming `unjudged`. `issue_key` is the *cross-round identity*, drawn from the CLOSED vocabulary in `gan/framework/issue_keys.py` (18 `<domain>.<facet>` keys; `other.new` is the only escape hatch). Measured: A0 re-raised the same cost defect ten times under `i-2, i-4, i-5, i-4, i-1, i-3, i-6, i-1, i-1, i-1`; A6 re-raised its access bug three times under `i-2, i-4, i-2`. Nothing could join those, so every outer re-discovered them.
- **Cross-outer issues are stripped, and NOT for budget reasons.** The live round-trip list is dropped when the selected parent comes from a different outer because the causal chain is broken there (evaluator's own design, restored parent design and code branch all changed) and its `suggested_fix` was written against that older state. What crosses the boundary is the ledger projection instead — **病史, not 处方**.
- **`ckpt/issues/ledger.json`** (`gan/framework/issue_ledger.py`, `WriterToken`-guarded) accumulates four observations per key — `raised`, `judged_fixed`, `judged_unfixed`, `stance` — joined in `loop._update_issue_ledger` (the only place that owns both parent and child nodes: the raise lands on the node, the verdict/stance lands on its child). Derived `status` = `open` / `fixed` / `not_actionable`; a raise after a fix **reopens** it.
- **`carried_over`** (injected only when the live channel is empty, and suppressed by the C2/C4 text ablations) carries **no `issue_id`, no `description`, no `suggested_fix`**: the incumbent evaluator is told to re-detect and re-prescribe. `framework`-owned keys (`access.grant-broken`, `harness.infra`, `process.observability`) are never carried to the planner — it has no operator for them (A6's evaluator said so itself: "根源在文件访问/路径解析层，非 planner 的 prompt/config 算子可直接修复").
- **Stopping the loop.** `issues.not_actionable_stances` declined stances (`out_of_scope`/`disputed`) with zero confirmed fixes mark a key `not_actionable`, and it stops being injected. Config: `issues.{ledger_enabled,carried_limit,not_actionable_stances}` in `loop.yaml`.
- **`verdict_stability` = per-issue mechanical truth (2026-10 v2).** `hits / verified`, where each judged issue's truth comes from `gan/framework/issue_checks.py` — three states (`PRESENT`/`ABSENT`/`UNKNOWN`) computed from node facts the framework already records (`report_summary`, `toolset_report`, `harness_rc`, `cost_tokens`, plus the grant-time readability self-check), paired as "present in the parent → absent in the child". Bound by `issue_key`, never by a predicate the evaluator writes. Ten checkers ship: `cost.tokens`, `cost.truncation`, `harness.infra`, `design.contract-format`, `access.grant-broken`, `process.dead-end`, `policy.{accept,reject}-bias`, `policy.constant-output`, plus the PAIR checker `policy.overcorrection` (decision rule inverted AND accuracy worse — a single generation cannot decide it). `process.dead-end` was added 2026-11-11 because its ABSENCE was MEASURED: the evaluator raised it in 6/6 generations of the six-generation round and no generation ever carried a verdict, even after zero-displacement episodes fell from 9/10 to 1/10 — `verdict_stability` stayed `UNKNOWN`, so nothing caught the drift. It reads the L1 `behavior` block and fires on EITHER signal (`zero_displacement_share >= 0.5` = half the traced episodes never left their starting neighbourhood, OR `obs_repeat_step_share >= 0.35` = the agent moves but a third of its steps change nothing — the wall-pushing shape gen 5 landed in, 0.444). `design.prompt-ineffective` deliberately still has NO checker: it is a judgement about whether a specific edit helped, which the anchor/score pair already measures — a mechanical twin would only duplicate it and make "not fixed" trivially correct. `UNKNOWN` on either side is excluded (never a miss); a defect already `ABSENT` in the parent is `unsupported` and excluded too. `verified == 0` → `None`, neutral in `F_e`. The old definition — one outer's whole verdict vector against the single shared bit "did the mean score rise?" — is GONE: it scored an honest evaluator 0.0 on A1 (planner disabled, so 19/20 `false` claims were correct, but the score rose anyway). Old supernodes keep an unrecomputable old value, told apart offline by `evaluator_stats.verdict_stability_basis` (`"issue_key_checks"` = new). Diagnostics only, not consumed by fitness: `verdict_verified` / `verdict_hits` / `verdict_unsupported`.
- **Checker thresholds (`issues.check.*`)** are calibrated, not arbitrary: `bias_share=0.65` (the pilot's real leniency cases sat at 0.67–0.70 accept against a 0.50 truth share, so 0.75 called a real defect fabricated); `min_samples=10` for DISTRIBUTION claims only (at n=2 the shares are {0, 0.5, 1} — pure noise, so the honest answer is `UNKNOWN`); `constant_min_samples=6` for `policy.constant-output` (flatness is exact for the recorded rows, but "the policy always answers X" is still an inference — 25% chance at n=2, ~1.6% at n=6); `acc_tol=0.05`; `dead_end_share=0.5` / `dead_end_repeat_share=0.35` for `process.dead-end`, calibrated on the six measured generations (zero-displacement shares 0.60/0.70/0.90 while dead-ended vs 0.20/0.10/0.10 once moving; repeat shares ≤0.16 in every productive generation vs 0.444 in gen 5's wall-pushing regression), with `behavior_min_episodes=4` before a share is decidable.
- **Ordering invariant**: at an outer boundary `_update_issue_ledger` MUST run before `_record_supernode` — the supernode reads `verdict_stability` back from the ledger that call just wrote.

### Instances & scheduling

- *instance* = role + workspace + trajectory. The task agent refreshes every **inner** generation (per `genid`); planner/evaluator every **outer** generation (`loop.py:_refresh_roles`, `*_factory` args from `build.py`).
- Each instance gets `toolsets/<role>/<inst>/` and `trajectory/outer_<O>/...jsonl`; tooling is re-assembled with `clear=True`, so deselected components actually disappear.
- The source workspace is keyed by role instance (per outer): `workspaces/<role>/outer_<O>/src`, shared by that outer's plan/evaluate/self-improve passes, and cleared at each outer start.
- Single-chain: no accept/reject and no parent selection for planner/evaluator. Each outer saves a checkpoint; the next outer starts from the previous checkpoint's design. `outer_improved` is logged, not enforced.
- The two role self-improve sessions inside one outer run in **parallel threads**; their write surfaces are seat-disjoint by `frozen.SEAT_WRITE`. Only the fast patch apply+commit serializes on `code_repo._TREE_LOCK`, staging exactly the patch's own files (attribution-clean).
- Credentials travel as an explicit `api_key` argument and the toolset load-report sink as an explicit parameter — never process-global `os.environ` swaps; `AccessBroker.last_result` is thread-local.

### Episode sampling (episode-based domains: balrog)

- A balrog "question" is an **episode**, identified by `<task>#<seed>`.
  `utils/episode_seed.py` is the ONE definition (stdlib-only — `utils/` is copied
  into every sandboxed harness run, so the parent and the domain harness derive
  identical seeds). Measured 2026-11-04: `env.reset(seed=...)` alone reproduces
  an episode through `EnvWrapper -> domain wrapper -> gym/gymnasium` for BOTH
  wired balrog domains; seeding the global RNGs does nothing (the constructor's
  random draw only picks the BabyAI sub-task, which the harness's goal filter
  already pins).
- Three layers, mirroring the question-sampling design:
  **anchor** (frozen per run in `ckpt/sample_seed.json.anchor_ids` — paired
  child-vs-parent comparison, and the evidence the LCB parent selector reads),
  **rotate** (per generation, from `derive_sample_seed(base_seed, genid)`),
  **eval** (frozen, `EVAL_KIND`, for the fixed-root evaluation — see the
  fixed-root evaluation bullet below).
- `domains.yaml episode_tasks` + `anchor_episodes_per_task` (`auto` =
  `min(n, max(1, n//2))`, the paper_review rule with a floor of one) are drawn
  by `task_execution.ensure_sampling_seed`. `sampling.anchor_k` deliberately does
  NOT apply to episode domains (it is a QUESTION count, default 10). The balrog
  harness refuses a plan whose task is absent from
  `domains/balrog/config/config.yaml` (fail-closed drift check).
- The plan travels on the EXISTING transport — `--sample_seed` / `--anchor_ids`,
  already forwarded by the task broker for non-parent-scored domains — and the
  harness writes `<output>/episode_plan.json` BEFORE the first episode
  (`task_runner` folds counts into an `episode_plan_ready` event and archives
  the file into `runs/<genid>/`).
- `report.json` gains `question_points` / `question_ids_passed|failed` /
  `episodes[]` keyed by episode id, so the FROZEN `loop._read_per_question` ->
  `_anchor_stats` -> `_select_parents_lcb` work unchanged (the selector stops
  reporting `fallback_no_anchor`; a missing anchor episode is a miss, matching
  the per-question convention).
- Seeds/ids never reach an agent face: the archived transcript qid stays
  `env__task__epNN` and `report_summary` carries no episode ids.
- **Runnable-domain gate (P1-8)**: `task_execution.domain_runnable_error` is the
  ONE answer to "can this domain run in the secure driver". `balrog_babyai` /
  `balrog_babaisai` are wired (sandboxed per-step agent factory +
  `domains.yaml` section + installer); `balrog_minihack` / `balrog_nle` /
  `balrog_crafter` / `balrog_textworld` are named by the upstream config and by
  the harness argparse but wire NOTHING — without the gate they fell back to the
  IN-PROCESS `AgentFactory` (no bwrap nesting, no scoped proxy token), a silent
  downgrade that voids "capability = existence". The driver refuses them before
  any sandbox side effect, `domains/harness.py` refuses them again (it cannot
  import `gan`, so it keeps `_SANDBOXED_BALROG_MODULES`; the offline guard
  asserts the two lists agree), and the registry keeps `balrog_minihack` /
  `balrog_nle` family stubs so the error is "not wired", never "unknown". Those
  stubs SHADOW the generic `balrog` family (longest prefix wins), so every key
  the generic family carries (`score_key`, `score_divisor`) must be repeated.
- **Report scale is DECLARED (P1-7)**: `families.balrog.score_divisor: 100` and
  `DomainTaskRunner` passes it to `extract_score`. The old hard-coded
  `_SANDBOXED_BALROG_DOMAINS` set meant any balrog domain added later silently
  kept a 0-100 value where every other domain scores 0-1.
- **Episode output contract + objective facts (P1-9)**: both wired balrog
  domains declare `output_contract.kind: action` with the env's own
  `language_action_space` (the offline guard asserts the two agree), so the
  task child is told the exact action vocabulary and `compute_report_summary`
  derives balrog's analogues of the label facts: `coverage` =
  episodes_played / episodes_requested (the plan, NOT the runner's per-task
  `num_samples`), `contract_ok` = "every step produced a legal action" (from
  `failed_candidates`), `label_distribution.prediction` = the ACTION share (so
  `policy.constant-output` becomes decidable), `overall_accuracy` = solve rate.
  `design.contract-format` / `cost.truncation` / `policy.constant-output` stop
  being UNKNOWN; `policy.{accept,reject}-bias` stay question-domain only
  (their labels do not exist in an action vocabulary).
- **The report no longer parses the output dir (P2-14/P2-15)**: the dead
  in-context-learning path is GONE (`domains/balrog/dataset.py` deleted, its
  `icl_*` config keys removed — it was constructed but never used, its
  `records/` never existed, and enabling it divided by zero), and
  `collect_and_summarize_results` takes its coverage denominator from the run's
  own `episode_plan.json` instead of re-composing `config.yaml` with hydra. A
  missing plan falls back to what actually ran (previous behaviour), a missing
  `config.yaml` no longer breaks anything.
- **Fixed-root evaluation (the eval layer, wired 2026-11-05)**: an episode domain
  has no question dataset to hold out, so its evaluation set is a frozen
  `<task>#<seed>` list instead. `domains.yaml eval_episodes_per_task` (per-task
  count, `loop.yaml eval.episodes_per_task` three-state override: `null` =
  inherit / `0` = off / `>0` = override) is drawn by
  `task_execution.ensure_sampling_seed` with `EVAL_KIND` into
  `ckpt/sample_seed.json.eval_episode_ids` + `eval_episodes_per_task`, disjoint
  from both the anchor core and every generation's rotating half.
  `build.py` builds a second `DomainTaskRunner` with
  `fixed_episode_ids` + `fixed_episodes_per_task`, and the launch shape is
  decided in ONE place — `task_execution.fixed_eval_launch_plan` — which the
  root-side broker calls for `eval_outer*` scopes: episode mode feeds the frozen
  list as the harness **anchor** list (so `plan_episodes` fills EVERY slot and
  `sample_seed` stays `None`), passes the PER-TASK count as `num_samples`
  (NOT `len(ids)`, which is tasks × N), and requires NO `questions.csv`;
  question mode keeps `questions.csv` + `len(ids)` exactly as before. The loop's
  comparability guard accepts either shape (`fixed_question_ids` OR
  `fixed_episode_ids`) and records `val_anchor_ids` / `val_episodes_per_task`
  with the `val_mean` batch. **`--anchor_ids` is INDEPENDENT of `--sample_seed`**
  in `_run_harness_and_report_local`: it used to be emitted only inside the
  seeded branch, so the evaluation plan came out `episodes=5 anchors=0 id:null`
  — five time-seeded random episodes that still produced rc=0, a report and a
  `val_mean` (measured 2026-11-05, `balrog_feval_1105`). `DomainTaskRunner` now
  also refuses such a round: `fixed_eval_plan_mismatch` + a raise, so
  `run_eval_rounds` drops it (`n_valid=0` → `supernode_eval_degraded`, previous
  `val_mean` preserved) instead of stamping a different exam's score.
  Guards: `scripts/local/verify_fixed_eval_episodes.py` (51 checks) and
  `scripts/local/check_fixed_eval_run.py` (audits a finished run dir).
- **Episode-level failure isolation** (`domains/balrog/evaluator.py`): one broken
  episode (upstream 502, malformed step response, crashed step child) must not
  void the generation. Both execution paths (`_run_sequential`, `_worker`)
  catch per episode and `_record_aborted` persists a row with the SAME shape a
  finished episode writes (`episode_id`/`seed`/`anchor`/`progression: 0.0`,
  `num_steps: 0`) plus `aborted: true` + `error`, so the report counts it as a
  played-and-lost episode (the denominator never shrinks) and the anchor
  pairing reads "this anchor missed" — true — instead of the anchor vanishing.
  `report.json` adds `episodes_aborted[]` and flags those `episodes[]` rows.
  The one asymmetry is deliberate: if EVERY planned episode aborted, `run()`
  raises ("infrastructure failure rather than a zero score") so the framework
  marks the generation invalid instead of writing a fabricated 0.0 into the
  lineage.
- **Parallel episodes are a DECLARED budget (P2-16)**, not a literal. The harness
  used to compose `eval.num_workers = 1 if agent_factory is not None else ...`,
  so the evaluator's `_run_parallel` existed but no sandboxed deployment could
  reach it. The number now lives in `domains.yaml`
  (`harness_episode_workers`: babyai `2`, babaisai `1`), is resolved by
  `task_execution.episode_worker_limit` and crosses as an explicit
  `domains.harness --episode_workers`; the harness takes
  `min(that, --num_workers)`, and an undeclared cap still means ONE episode. The
  scheduler itself was made fail-safe first: the queue is PRELOADED (episodes +
  one `None` per worker) so a dead worker's item still reaches a LIVE worker,
  results are polled with a timeout and the wait ends only when no worker is
  alive (`_EPISODE_POLL_S`), an episode that never came back is recorded as
  ABORTED in the master (unless its JSON is already on disk), and workers are
  joined/terminated in `finally`. `run_episode` also owns and CLOSES the env
  (`finally`) — a parallel worker reuses its process, so an unclosed env leaked
  a renderer per episode. Guard `scripts/local/verify_parallel_episodes.py`
  (22 checks: 1-vs-3-worker parity on the same plan, dead-worker isolation,
  all-dead round raising, env close on both paths, declaration resolution).
- **Two gym lineages, one CONTRACT (P2-17).** `nle` / `minihack` / `textworld` /
  `crafter` / `babaisai` build a gym-0.23 env (`baba` is 0.23-only) and adapt it
  via `wrappers.gym_compatibility.GymV21CompatibilityV0`; `babyai` is Gymnasium
  native. `EnvWrapper` now normalizes 1/2-tuple `reset` and 4/5-tuple `step` and
  raises a NAMED error ("the env is not adapted to the Gymnasium API — wrap it in
  GymV21CompatibilityV0") instead of a bare unpack failure three frames away;
  the six-arm dead `_process_observation` chain is replaced by ONE observation
  contract check (`text.long_term_context` must be a str, error names the env),
  `check_action_validity` refuses a missing `language_action_space` /
  `default_action` by name, and `get_instruction_prompt`'s `self.env_namee` typo
  is fixed. `from gym import spaces` stays (the `Strings` space) — documented as
  the ONE deliberate legacy import, since migrating it would not remove the
  dependency. Guard `scripts/local/verify_env_contract.py` (33 checks incl. real
  babyai/babaisai: seeded reset reproducibility, 5-tuple shape, and
  `domains.yaml output_contract.allowed_labels` == the env's own vocabulary).
- **Per-episode cost ATTRIBUTION (P2-18).** `agent/llm_withtools.py` writes its
  per-call rows to `llm_calls_sidecar(<trajectory>)`, i.e. a SIBLING of the
  transcript; binding only the transcript meant every step child's rows died in
  the tmpfs. Two successive transports fixed it: first a staged sidecar
  bind+copy-back, and since the F5 telemetry rewrite (docs/19 §12.28) the rows
  simply STREAM to the trusted harness tagged `stream: "llm_calls"`
  (`_telemetry_stream_target` routes them to
  `trajectory_source + LLM_CALLS_SIDECAR_SUFFIX`) and are written hash-chained
  there — the child holds no write path to its own accounting. Either way
  `archive_episode_trajectory` mirrors `<transcript>.llm_calls.jsonl` into
  `agent_evals/`, so `trajectory._collect_llm_calls_sidecar` folds it into the
  archived sidecar with `qid=<env>__<task>__epNN` — no framework change was
  needed. The report adds `episodes[].llm_calls` (`None` when absent),
  `episodes_sidecar_missing[]` and `llm_calls_total`; `episode_idx` rides the
  episode log so the report can locate the sidecar. **Attribution only**: the
  child counts its own calls, the root-side proxy meters usage — money/tokens
  stay the proxy audit's numbers and the two are never summed. Guard
  `scripts/local/verify_episode_sidecar.py` (30 checks).
- **Every STEP of an episode reaches the archive (2026-11-08, transport reworked
  2026-11-09).** The task agent truncates its transcript at construction
  (`agent/base_agent.py`: `trajectory_log.reset`), so a multi-step balrog episode
  kept only its LAST step — measured on the first real round with the new
  pipeline (`balrog_verify_1108a`: 10 episodes / 98 step children → 10
  transcript rows, which silently blinded the evaluator). In the TELEMETRY
  design the same hazard reappears as a `traj_reset` message, so the parent (the
  trusted side) decides: `_telemetry_reset_allowed(msg, trajectory_reset)`
  DROPS a transcript reset from a child that is ONE STEP of an episode
  (`trajectory_reset=False`, selected by `_SandboxedBalrogAgent.forward`) while
  always honouring an `llm_calls` reset (that sidecar IS per step child). A
  child that changed its own logging behaviour therefore cannot re-truncate the
  archive. Re-measured after the fix (`balrog_verify_1108b`): 55 calls → 55 input
  + 55 output rows; and after the telemetry rewrite (`balrog_verify_1109a`):
  40 calls → 40 rows with chain fields `seq`/`prev_hash`.
- **The CM seam sidecar of a balrog round (2026-11-10).** `task_runner`
  ingests exactly ONE path per round (`paths.task_cm_events_path` =
  `<run_dir>/outputs/<run_id>/cm_events.jsonl`); question domains satisfy it
  incidentally through `run_agent`'s `cm_events_target`, but the per-step
  balrog path never passed one, so every balrog generation reported
  `cm_events_ingest_empty` ("sidecar file missing") and the seam events existed
  only in the harness's own `[f5] ... cm_events=N` print — the audit stream was
  empty on exactly the domain where the memory window's cost is decided.
  `_SandboxedBalrogAgentFactory.set_cm_events_target()` is called once by
  `harness_balrog` after `output_dir` is final (the path rule stays single-
  sourced there, so it cannot drift from `paths`; the in-process `AgentFactory`
  has no setter, so legacy/direct runs are unchanged). Appends are serialized
  on `harness._CM_SIDECAR_LOCK`: one harness run has N episode workers, each
  with its own telemetry thread, and an interleaved partial line would be
  skipped as a torn line by the ingest = silently lost audit events. Acceptance:
  the next balrog round shows `cm_events_ingested events=N`. Guard:
  `scripts/local/verify_balrog_cm_sidecar.py` (13 checks — the wiring, the
  `resume_from` branch, the in-process no-setter path, and that 120 concurrent
  appends land as 120 parseable lines). NOTE the first real round after the fix
  still read `cm_events_ingest_empty`: a generation runs from the STAGED copy
  (`work/<genid>/repo`, snapshotted at generation start), so a mid-round source
  edit cannot reach it — the proof must come from a run STARTED after the edit.
- **Behaviour facts in the report (L1, 2026-11-11).** The loop's improvement
  verdict reads the SOLVED RATE only, and on a 10-episode generation its quantum
  is 0.1 — measured: an IDENTICAL code+prompt+episode set scored 0.2 in one run
  and 0.3 in another, while a prompt patch moved the turn share 80.5% → 91.4%
  (a real regression) with solved unchanged at 0.3. So two process measurements
  now ride `report.json`: per episode `distinct_obs_states` (distinct observable
  states = facing + the two relative descriptor blocks, with the invalid-action
  feedback prefix STRIPPED so the same cell cannot look new) and
  `obs_repeat_steps` (steps whose action changed nothing), plus the derived
  `zero_displacement` (`distinct_obs_states <= 2` — the episode never went
  anywhere); `behavior` aggregates them with a denominator of TRACED episodes
  only (an aborted episode has no trace — absence, not zero).
  `compute_report_summary` forwards `behavior`, so the evaluator can quote a
  stable number instead of inferring "it is spinning" from an action histogram;
  it carries no ground truth, so it is blind-phase safe. **`zero_displacement` is
  deliberately the gameable-resistant one**: inaction cannot lower it (sitting
  still IS two states), so the only way to reduce it is to actually move — use
  it, never the go-forward share, if a behaviour term is ever added to the
  improvement gate. Guard `scripts/local/test_llm_retry_budget.py` (32 checks,
  including a scripted spin-vs-walk drive of the real `_play_episode`).
- **Cycle facts + action classes (docs/20, 2026-11-17).** `obs_repeat_steps`
  measured INFORMATION change, not progress: the state key carries the facing, so
  a turning step always changes it and the WORST generation scored LOWEST (A0
  gen8: 5.4% while turning in 92% of its steps) while a healthy agent that keeps
  trying and failing scored 36–53% (A1) — the sign is inverted, so it is no
  longer a verdict input. Two EVIDENCE-ONLY additions (measured over 31 archived
  generations; no checker threshold reads them): per episode `revisit_steps` /
  `revisit_share` — steps whose `(state, action)` pair already occurred earlier
  in the SAME episode, i.e. a CYCLE detector (spinning generation 90.9%, healthy
  arm 46.8%) — plus `no_progress_action_counts` (steps whose action changed
  nothing, keyed by the EXECUTED action); aggregated as `behavior.{revisit_
  steps_total, revisit_step_share, episodes_degenerate_revisit[_share]`
  (per-episode line `DEGENERATE_REVISIT_SHARE = 0.80`), `no_progress_action_
  counts}`, and both families are ABSENT (never 0) when the action trace is
  missing or length-mismatched. The framework then derives
  `behavior.action_class_shares` (where the STEPS went) and
  `behavior.no_progress_class_counts` (where the WASTED steps went) from the
  domain's declared `output_contract.action_classes`, which must be an exact
  PARTITION of `allowed_labels` (`task_execution.validate_action_classes`,
  refused at `TaskRunner` construction, asserted offline by the unit it enables);
  babyai declares `turn/forward/interact`, babaisai `move/idle` (it has no turn
  semantics — forcing a unification would make an always-zero `interact` share
  read as "never manipulates", which is false). Blocked forward vs failed
  interaction are DIFFERENT defects, and the aggregate wasted-step count cannot
  tell them apart because trying things is what produces failures (the healthy
  arm wastes MORE). A low `interact` share is deliberately NOT an issue key: one
  arm solved 5–6/10 at 7% interaction. `process.dead-end` still keys on
  `zero_displacement_share` alone until the generation-level revisit threshold is
  calibrated on data that did not produce it (docs/20 §8: metrics →
  calibration run → verdict, never the same batch twice). Guards
  `scripts/local/test_behaviour_facts.py` (34 checks: 4 constructed sequences +
  an archive replay that reproduces the deployed `obs_repeat_steps_total`
  exactly) and `scripts/local/test_action_classes.py` (26 checks: partition
  refusals, both real domains, shares/denominator/drift visibility).
- **The recorded action is the EXECUTED one.** `_play_episode` used to do
  `action = response_str` after the step, so the next turn's `prev_action` was
  the model's raw prose while the env had executed `default_action` (the
  feedback line even admitted the default), and the episode CSV's `Action`
  column contradicted `action_frequency`. Now: `executed =
  check_action_validity(reply)` drives `prev_action`, `env.step()`,
  `action_frequency` and the CSV `Action`; the raw reply is preserved in the CSV
  `Reasoning` column (the only record of an illegal attempt).
- **Aborted-round guard (P2-16b).** `episodes_aborted` was written into
  `report.json` and consumed by NOTHING: 2 of 5 episodes dying to an upstream
  502 still produced an ordinary, depressed score that entered the lineage.
  `domains.yaml max_aborted_ratio` (balrog family + both concrete domains, which
  SHADOW it) is resolved by `task_execution.aborted_round_reason` against
  `compute_report_summary`'s new `episodes_aborted` / `aborted_ratio` facts;
  above the limit `task_runner` VOIDS the score (`score: null`,
  `invalid_reason`, `task_round_degraded` event) while keeping the report on
  disk for forensics. Undeclared = off, so question domains are byte-identical.
  Guard `scripts/local/verify_score_integrity.py` (32 checks: the CSV/prev_action
  semantics on the real episode loop, the threshold table, and
  longest-prefix declaration resolution).
- **Cross-step memory window (方案 a, 2026-11-10).** Upstream kept the last
  `agent.max_text_history` MESSAGES in the LLM conversation (one step = 1
  observation + 1 action = 2 messages). A step child here is a FRESH process
  whose `msg_history` starts empty, so the episode loop RE-RENDERS the same
  window into each step's payload (`inputs["history"]`) —
  `domains/balrog/evaluator.py:render_history` emits a plain-text block behind a
  stable marker (`HISTORY_MARKER`), oldest-first, `(observation AS THE MODEL SAW
  IT, EXECUTED action)` pairs only — no reasoning (`remember_cot` is not part of
  this port) and never the raw reply. The window lives in `_play_episode`'s
  function scope, so it cannot leak across episodes. Depth is DECLARED:
  `gan/framework/domains.yaml history_messages` (upstream unit; both wired
  balrog domains 16 = 8 steps — babaisai was 0 until the babyai A/B verdict,
  then flipped) → `task_execution.history_window` →
  `domains.harness --history_messages` (explicit CLI, never env);
  `history_max_chars` drops the OLDEST steps first and never the newest. `0`
  means the key never appears at all, i.e. the prompt is byte-identical to the
  pre-window behaviour (the ablation switch). `report.json` states
  `history_steps` / `history_used_max` and each `episodes[].history_used`, and
  `scripts/local/check_history_delivery.py` greps the archived prompts for the
  marker — an evolved `task_agent.py` that stopped dumping `inputs` would
  otherwise lose the memory SILENTLY. **The A/B verdict (2026-11-10, full
  length, both arms on ONE frozen 5-episode eval plan)**: ON 1/5 solved vs OFF
  0/5, and the stronger evidence was the action census — ON used `toggle`/
  `pick up` (75 of 397 steps), OFF used **no interaction action at all** in 384
  steps (turn-left / turn-right / go-forward ≈ 1/3 each: pure rotation +
  blind forward), so the window is what makes "already facing / already
  toggled" representable. Harnesses: `scripts/local/run_full_arm.sh`
  (`<messages> <out_dir> [seed_from_dir]`, refuses a pinned step count),
  comparison `scripts/local/run_full_ab_chain.sh`. **Why not CM**: the task
  child's CM universe is ONE block / ONE turn by construction (`docs/19
  §12.69.3`, and 40 calls / 40 step children measured on 2026-11-09), so there
  is no in-session history for CM to manage; §12.69's pre-seed channel
  pre-seeds the task DESIGN config (`prompt`/`tools`/`cost`), not session
  content. Guards: `scripts/local/verify_episode_history.py` (30 checks,
  including an ON-vs-OFF behavioural difference that proves the window reaches
  the DECISION surface, not just the payload string).
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
- **Secure sandbox boundary (fix3-main-sync)**: the GAN loop runs ONLY through the sandboxed driver (`scripts/run_gan.py` / `gan.driver.run_gan_driver`, launched via `sudo` as a normal user with the three `*_SANDBOX_USER` env vars — see README "Environment deployment"); the legacy `--in-process` option is hard-disabled and only ONE `--domains` value is accepted. Every worker is dropped to a dedicated UID (`setpriv --bounding-set=-all --no-new-privs`) inside a bubblewrap namespace (`--unshare-all --unshare-net --cap-drop ALL`, a synthetic credential-free `/etc` from `gan/framework/sandbox_etc.py` — which also projects package-owned `/etc` subtrees (such as `/etc/java-*-openjdk` so OpenJDK's `conf/security` symlinks resolve, and the Debian `alternatives` registry so `/usr/bin/cc` — rustc's linker driver — resolves through the sandbox's read-only `/usr` view)), with role/seat-scoped write roots granted and reclaimed via POSIX ACL (root-side brokers reconcile stale ACLs after a crash). Provider credentials never leave the root-side `ParentLLMProxy`: children get scoped `sk-gan-` tokens delivered as sealed memfds (never argv/env), planner/evaluator run as separate seat processes hosted by `gan/framework/role_process.py` (the one sanctioned framework→roles import). Per-domain sandbox profiles in `domains.yaml` are fail-closed (missing/disabled profile or `gpu != none` refuses the run). Resource budgets (cgroup/CPU/mem/pid/disk, #7) remain deferred.
- **Cross-process contract (role seat ↔ broker, fix3)**: because a role session runs in a child and its tools call back over IPC, three things are NOT automatic — (a) `role_process._validated_result` validates each role method's return dict by **exact field set**, so a field added to `planner.plan()` / `base_role.self_improve()` / `EvalContext` must be mirrored there or *every* result is rejected (plan -> `planner_failed`/`inner_invalid` with a null score; self-improve -> `self_improve_error` degrading to `kept`, i.e. the role never evolves); (b) `_RemoteBroker` must mirror the broker's ablation flags (`deny_deep`/`deep_view_only`/`shallow_prompt_only`/`disable_shallow`, carried in `broker_info`) or the child's tool-layer checks see all-false and B3/B4 silently no-op; (c) an exception raised in the broker reaches the child only if `_RemoteBroker._rpc`'s name→type map knows it, else it degrades to a generic `RuntimeError` the catcher misses (this is how B1's `SourceAccessDisabled` broke).
- **Activation**: always-on → `gan/tools/`; opt-in → `gan/components/` (registered, selected by design config). Plumbing (`deep/`, `design/`, `work/common/`) is frozen; only `work/<role>/` and the task components/registry are evolvable.

### Model config

- **Model config**: single source is `gan/framework/models.yaml`, read only by `gan/framework/models.py` (`resolve`/`resolve_entry`/`resolve_section`/`describe`) — pure lookup, no env, no fallback, no precedence chain. Sections: `gan.{task,planner,evaluator}`, `dgmh.{meta,task}`, `domains.<role>` (non-task domain roles only). Entry = plain model string or mapping `{model, reasoning_effort}`; `resolve()` returns the model STRING; `resolve_entry()` returns the normalized entry and validates at startup (unknown keys / unknown `reasoning_effort` fail before any run). `reasoning_effort` contract: `low|high|max` (NOT the OpenAI enum; defined as `agent.llm.REASONING_EFFORTS`), travels EXPLICITLY as function/CLI argument — never env: roles threaded `build factories -> Role -> chat_with_agent -> get_response_from_llm`; task child `DomainTaskRunner -> run_harness_and_report -> domains.harness --model/--reasoning_effort -> stdin payload -> TaskAgent` (broker mode: fixed in root-side broker config, same models.yaml entry). **The task agent shares the driver's model** (explicit `domains.harness --model` arg). Effective config recorded at runtime (`events.jsonl: model_config` for GAN, `[model_config]` log line for DGM-H, `Node.meta["model"]` per generation; proxy audit adds caller's `reasoning_effort` per call). Do not read `models.yaml` anywhere else.

### Write path, ledger, and audit

- **Front-loaded write policy**: `edit_source` is the only official source-edit entry and uses the centralized frozen authorization helper before mutation. Dispatch validates declared `edit_source` inputs before invoking tool code. Commit and load/preflight layers also apply a conservative AST policy rejecting raw writes, subprocesses, and dynamic execution in agent-owned Python. Framework plumbing is explicitly exempt because it legitimately writes state; the AST policy is defense in depth, not a replacement for a real sandbox.
- **Ledger write gate (B46 step-1)**: decision-grade artifacts (`scores/scores.jsonl`, design configs, checkpoints) are written only through guarded framework entry points that require the per-run `WriterToken` (`gan/framework/ledger.py`, minted once in `build_gan_loop` and threaded explicitly — never env, never module-global). Unguarded writes raise `LedgerError`; agent-owned source importing a guarded writer module (`gan.framework.scores` / `gan.design.store` / `gan.framework.checkpoint`) is refused at the registry and commit gate via the same AST issues channel (`observed_ledger_writer`). The token is an identity, not a secret — file-level tamper DETECTION is deferred step 2 (hash chain, unchanged trigger); step 2's role (batch 4): the detection compensation for application-step BYPASS — the write side's existence control (grants / AST gate / `WriterToken`) is strong but heuristic, the read side's is absolute. Regression suite: `scripts/local/regress_ledger_gate.py`.

### Data flow and boundaries

- **Permissions = capability by existence (batch 4 axiom)**: reads are physical absence without a grant; writes are not applied without capability. Enforced by what the framework MOVES (`AccessContext` role+seat; REQUIRED no-default on `set_access_context` / `frozen.write_roots` / `frozen.is_allowed` / `AccessBroker.grant` / `code_repo.check_patch|apply_*`; no `legacy` seat; `frozen.any_seat_allows` is the only deliberate union query — classification, never authorization; `AccessBroker` refuses repo-root / self-containing / oversized grants; local check: `scripts/local/test_frozen.py`).
- **Reserved cost keys & context-management seam (docs/10 phases C/D)**: authority is `gan/framework/cost_keys.py` (`validate_cost_config` shared by schema/set, `effective_all` the ONLY clamp, `cost_keys_applied` events); task budget keys ride the harness stdin payload (never env). Phase-D seam: `agent/llm_withtools._counting_llm` is the single per-turn assembly point — `CMSession` (per-seat, snapshot-loaded) rewrites only what is SENT; per-entry copies keep canonical history + raw trajectory rows untouched; the canonical rebuild is gated by `seam_applied` (never the sent-view length), so a legally EMPTY sent view still preserves the prior canonical history (docs/19 §12.42); modes `passthrough` (kill switch) / `slice` / `compact_conservative` / `custom`; audit via `cm_violation` / `cm_*` events and the `cm_events.jsonl` sidecar; runtime `cm` savings are DERIVED, never billing. The runtime CM numbers are FOUR separate calibers (docs/19 §12.46, §12.54): the view STATE (`substituted`), the view-transition FLOW split by SOURCE (`saved_chars` per turn is seam-only; `est_chars_saved` is the total, with the agent tool-face half exported as the conditional `agent_saved_chars` / `est_saved_agent` — the two never mix), the SHAPER of the send (conditional `send_source`: `identity` = passthrough sent the raw history, `author` = a named assembly returned its own history, ABSENCE = the framework post segment rendered the send), and the SEND FACT — claim vs wire (`cm_send_fact`, one per LLM call: the widths the render PROMISED against the widths the client actually built, with a conditional signed `drift` + per-entry `mismatch` only on disagreement and `compare:false` where the framework produced no claim). It exists because a rendered view could be dropped between the seam and the provider while every other number stayed healthy — the silent no-op of docs/19 §12.54-C. History entries carry ONE text carrier, `content`; the rendered form of a send copy is the framework-private projection `_cm_sent` (written only by `render`, consumed and stripped only by the payload builder in `agent/llm.py`, deliberately absent from the assembly portal surface) — so "raw" and "rendered" are two keys with two meanings instead of one key with two. The OP-REFUSAL family is split by `cm_violation.source` (`policy_face` | `seam_record`), and the AGENT tool face rides its own UNCOUNTED `cm_face_violation` (`face="agent"`, docs/19 §12.47) — it is outside the `count_ops` universe, so it never moves `violations`/`ops_refused`; scanners must subscribe to both event types. One `_turn_stats` builder feeds both the normal and the synthetic `cm_view`, so their calibers cannot drift. The cost-record `cm` node keeps three facts distinct by construction (docs/19 §12.43): a failure is a STRIKE event, a disabled flag is a STATE (bool), an overrun is ADVISORY — never summed together; the refusal/breach families stay separate keys too (`cm_violations` = the counted op-refusal half, `cm_face_violations` = the agent-face half — audit-only, never degradation, docs/19 §12.50 — and `cm_*_violations` = contract breaches). The cost row's `cm` node is ONE pure transform (`loop._cm_cost_node`) gated on "a session exists" — never on `turns`, so a 0-turn session's `policy_disabled`/`assembly_disabled` facts are not mistaken for "CM was never wired", and the whitelist's two-way coverage (no dropped field, no dead entry) is tested against a session that can report everything (docs/19 §12.64). The evaluator's receipt `budget` (and its reduced `cm`) is a NAMED projection with explicit allowlists (docs/19 §12.44), and the task child's CM degradation reaches both roles once each (planner `budget.task_cm_degraded`, evaluator `run_summary.task_cm_degraded`) — covering RUNTIME strikes/disabled AND the BUILD-time `cm_session_degraded` fact (docs/19 §12.47.7).
- **Planner→evaluator isolation**: every evaluator-facing channel is a NAMED PROJECTION (explicit allowlist transform) — summary is ops-only (files exited; changed files are an outcome fact owned by `meta_view.task_patch_files`, grant facts ride `ops[].paths`), receipt exposes only outcomes (`receipt.design.ops`), run summary exposes only `run_summary.meta`/`run_summary.receipt` (no `rejected_reason` full text, which quotes the planner's artifacts), feedback digest exposes only `issue_id`/`accepted`/`stance`. A NEW meta key or receipt field is invisible to the evaluator until its projection is extended on purpose (default-deny, mirroring the role access allowlist). The planner responds to issues with a REQUIRED structured stance (`response_kind`: acted / acted_differently / out_of_scope / disputed / deferred) — the evaluator calibrates on decisions, never on persuasion.
- **Artifact vs rationale (B29, batch 16)**: the ARTIFACT under evolution gets structural facts; the planner's RATIONALE gets nothing. `set_prompt` / `set_config(key="prompt")` record `prompt_change_facts` (chars/lines/prev_*/added_lines/removed_lines/similarity/char_similarity/char_survival/changed_from_to/sha256_12/empty/equals_seed — numbers and hashes only, never text), and `DesignContext.record` stamps `target_role` for every design record (framework-owned, written after the caller's fields so it cannot be shadowed). Those facts are whitelisted into `build_diff_summary` and `receipt.design.ops`. The prompt TEXT is deliberately not projected: it is audit evidence the evaluator may fetch from the task trajectory (`read_trajectory` archives the child's input message verbatim, redacted only for benchmark/score strings), never an assertion pushed into its decision surface — the isolation is on the PROMPT surface, not on the read surface; the AUTHOR side reads its own draft via `get_config` (same object the R2 gate compares against). This does NOT relax the `respond_issue.feedback` rule: its derived statistics stay banned.
- Evaluator feedback is **text** (a digest), not a numeric reward; the evaluator must not fit the benchmark score (blind score first, then reveal).
- **Framework output contract (F4/C1)**: every domain declares the final-answer requirement either structurally (`domains.yaml` `output_contract`, e.g. label vocabulary) or by prediction source (polyglot: the workspace diff is the deliverable; imo_proof: the response IS the proof). `derive_response_contract` reduces that to ONE sentence with THREE consumers — an explicitly labeled "Output contract" section in the task child's instruction (assembled AFTER the role-authored design prompt; travels via `GAN_RESPONSE_CONTRACT`), the format-repair turn's requirement, and a framework-owned `output_contract` field in `get_config`'s keys view (stamped onto the design context by the loop, so the planner SEES the contract at the point of rewriting the task prompt). `GAN_RESPONSE_CONTRACT` must be listed in ALL THREE `_TASK_ENV_NAMES` whitelists it crosses (`gan/framework/task_execution.py` source + the `domains/harness.py` and `domains/task_worker.py` child hops) — an unmirrored key dies silently at that hop (observed live; guard `scripts/local/test_task_env_mirror.py`). The injection is immutable by construction — the contract is not a `set_config` key and lives in no writable surface; a role rewriting a DIFFERENT contract inside its own prompt is NOT blocked (soft constraint: the author has seen the contract, and online scoring always follows the framework rule, so a competing contract is self-defeating).

## Driver-mode field lessons (v5.1 real-API round)

Lessons from the first driver-mode rounds run end-to-end against a real
OpenAI-compatible gateway — each was invisible to the offline smokes:

- **A venv on a host-shared filesystem taxes every sandboxed child.** Each
  child re-imports site-packages from scratch and the prefix is bound read-only
  into every sandbox, so its read latency is multiplied by the child count —
  and `balrog` spawns one child per ENVIRONMENT STEP. Measured 2026-11-04 with
  a byte-identical venv (`cp -a`): `import agent.llm` = **3.0 s** on ext4 vs
  **67.5 s** under `/mnt/d` (WSL 9p/drvfs); through a repo symlink it is still
  20 s, because `sys.prefix` follows the symlink path and Python keeps
  resolving site-packages over 9p. This matched the live symptom exactly:
  alternating ~50 s / ~5 s `llm_call` gaps in a 10-episode round (51.9, 57.3,
  4.8, 5.0, 56.3, …) with `task_trajectory.wallclock_s = 537.7` for 10 calls.
  A per-call staging copy was measured at 0.47 s — the hypothesis "staging is
  the bottleneck" was WRONG, so measure before optimizing. `gan/driver.py`
  prints a `WARNING` and records `python_prefix_slow_fs` in `driver_start`
  whenever `sys.prefix` sits under `/mnt|/media|/net`; the fix is deployment
  (create the venv at a native path and invoke it by ABSOLUTE path), see README
  "2) Python environment".

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
  (and `docs/cost_tem.md`); `docs/19_CM问题与解决.md` (CM audit ledger +
  §12 batch records — the authority for CM done/open status; the `outer=2`
  real-round acceptance is PASSED (§12.22); still open there:
  T3 process isolation deferred (F5 — audit-stream write authority +
  parent-side hash chain — has since landed in §12.28, see below);
  CM-08 trajectory seat reads CLOSED in §12.24 (segment index/query RPC,
  offset/fetch/tail/head/grep, per-request 120k cap); CM-06 CLOSED in
  §12.25 — usage face renamed ctx_budget→ctx_usage (budget = runtime cap,
  usage = meter) + tokens split input/output/reasoning (absent when
  unreported), ⑥ ctx_view stats ruled won't-fix); the CM permission-surface
  batch CLOSED in §12.27 — make_face_tool param boundary (spec params
  enforced, keyword-only, no internal-name leaks), load_policy runs the
  guarded-cm import gate, POLICY_FACES narrowed to read+shrink (D8 ruled
  direction (b) thorough: no pin/unpin/note on the policy face; protection
  = agent ctx_pin/presets, creation = ctx_note; policy-turn pinned delta
  caught by the reconcile's fourth question); F5 audit-stream write
  authority moved to the parent (§12.28 — seat trajectory rows arrive as
  protocol messages, task child forwards over the telemetry socket,
  role_broker no longer grants the trajectory dir, parent-side hash chain
  in gan/framework/traj_chain.py, offline verifier verify_stream); the
  §12.40 F1+F2 batch (2026-11-05 deep-audit follow-up) closed the
  pin-semantics hole and the saving caliber: ctx_pin on an elided block
  restores FULL (dedup the only exempt form; entry-level convergence, so
  `pinned_form` no longer misattributes an innocent module), and
  `saved` is the SIGNED TRANSITION width delta from the single
  `CMSession.view_text` source (primitives carry no `saved`; dedup /
  derived_host anchored forms + stale-reference re-points are included;
  C.1's small-block widening is recorded honestly instead of clamped).
  The F4/F5 description-face items (agent-facing registry descriptions /
  params_schema, the `_cm_latch_scan` docstring) and the doc/comment
  errata are closed by the TEXT-ONLY batch (§12.41 — zero behavior change);
  the §12.42 F3 batch closed the seam empty-history hole: the canonical
  rebuild is gated by `seam_applied` (never by the sent-view LENGTH), so a
  named assembly that legally returns `[]` no longer replaces the prior
  conversation; `n_viewed` is only the offset into the client's rebuilt
  history, and canonical stays AS-SENT (user turns keep the per-turn
  tool-use header; no dedup marker ever enters it). F6 (cost aggregation
  caliber) is closed by §12.43: `cm_*_failures` counts STRIKE events only,
  `cm_*_disabled` is a BOOL state (disabled/latched/cm_view stats merged),
  `cm_*_overruns` is the ADVISORY counter (never a failure),
  `cm_*_violations` is its own family, and both transports share ONE
  mapping helper (`_cm_node`). F7 is closed by §12.44: the evaluator's
  receipt `budget` is now a NAMED projection (explicit allowlist +
  reduced cm node with `cm_degraded`, never the planner's module report /
  `last_error` / `where`), both roles get the task child's CM degradation
  once each (plan receipt `budget.task_cm_degraded` / evaluator
  `run_summary.task_cm_degraded`), the cost record's `cm` whitelist
  carries the violation/overrun families + `thread_leaks_live` +
  `substituted`, and `substituted` is the NON-FULL form count in BOTH
  `summary()` and `cm_view.stats` (`violations` is documented as the
  `ops_refused` alias). F5 (the fixed-root evaluation's plan seat ran without
  any CM/cost-key wiring) is closed by §12.45: the main plan seat's wiring was
  extracted into one helper used by BOTH seats, and the latch/report tables
  became `(role, seat, scope)`-keyed (closing the F4 instance blind spot) —
  evaluation still writes no `cost_sessions` row.
- Environment/deployment record: `docs/archive/1_DGMH部署记录.md` and `README.md`

## Base license

HyperAgents base code is under `LICENSE.md` (CC BY-NC-SA 4.0); keep attribution.
