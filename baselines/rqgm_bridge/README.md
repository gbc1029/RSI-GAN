# RQGM baseline bridge (paper_review)

This directory is the **adapter layer** that wires the
[RQGM reproduction](https://github.com/kingkillery/red-queen-godel-machine)
(git submodule at `baselines/rqgm`) into the `paper_review` domain. It reuses
RQGM's shipped `llm_providers` (coder/reviewer prompt co-evolution) **unchanged**
— no code inside the RQGM submodule is modified.

## Files

- `convert.py` — converts `domains/paper_review/dataset_filtered_100_*.csv`
  into RQGM's own `tasks.jsonl` + `anchor.jsonl` format.
- `run.py` — loads the converted data and calls `run_rqgm("llm", ...)`.

## Usage

```bash
# 1. one-time: initialize the submodule (see main README for the git command)
# 2. generate the paper_review splits (if not already present)
python -m domains.paper_review.curate_subsets

# 3. convert paper_review -> RQGM tasks/anchor
python baselines/rqgm_bridge/convert.py \
  --csv domains/paper_review/dataset_filtered_100_train.csv \
  --out-dir baselines/rqgm_bridge/data

# 4. run RQGM (needs OPENAI_API_KEY; pass --base-url for a non-OpenAI endpoint)
python baselines/rqgm_bridge/run.py \
  --tasks baselines/rqgm_bridge/data/tasks.jsonl \
  --anchor baselines/rqgm_bridge/data/anchor.jsonl \
  --model gpt-4o-mini --budget 64
```

## Known limitation (reviewer degradation on paper_review)

RQGM's reviewer only ever sees the **coder's artifact text** (e.g. `"accept"`),
never the paper itself. On `paper_review` the answer is context-dependent
(`"accept"` is a judgment about *one specific paper*), so:

- the **coder** role is fully meaningful: `prompt_input = paper`, `answer =
  ground-truth outcome`, scored by exact (normalized) match;
- the **reviewer** role degrades to a **well-formedness check** (it can only
  learn "is the answer a single-word verdict", not "is the verdict correct for
  this paper"), and the anchor set in `convert.py` therefore calibrates
  *formatting* only.

This is the accepted trade-off of "reuse `llm_providers` unchanged". If a
content-aware reviewer is later required, the fallback is to **fork RQGM** and
adapt the reviewer evaluation to receive the paper context (see the main
README's RQGM section).
