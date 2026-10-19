"""Run RQGM (Red Queen Gödel Machine) as a baseline on paper_review.

Reuses RQGM's shipped ``llm_providers`` (coder/reviewer prompt co-evolution)
UNCHANGED — this bridge only feeds it paper_review data in RQGM's own
tasks/anchor format and invokes ``run_rqgm("llm", ...)``. No code inside the
RQGM submodule is modified.

Requires the RQGM package to be importable, either:
  (1) via the git submodule at baselines/rqgm (src-layout: baselines/rqgm/src
      is prepended to sys.path automatically), or
  (2) via ``pip install 'red-queen-godel-machine[llm]'``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Prefer the submodule checkout (baselines/rqgm/src, src-layout); fall back to
# a pip-installed ``red-queen-godel-machine``.
_SUBMODULE_SRC = Path(__file__).resolve().parents[1] / "rqgm" / "src"
if _SUBMODULE_SRC.is_dir():
    sys.path.insert(0, str(_SUBMODULE_SRC))

try:
    from rqgm import AnchorItem, OpenAIChatModel, RQGMConfig, Sample, run_rqgm
except ImportError as e:  # pragma: no cover - environment issue, not logic
    raise SystemExit(
        "rqgm is not importable. Either initialize the submodule "
        "(`git submodule update --init baselines/rqgm`) or run "
        "`pip install 'red-queen-godel-machine[llm]'`.\n"
        f"Original error: {e}"
    ) from e


def load_samples(path: Path) -> list[Sample]:
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        samples.append(Sample(row["task_id"], row["prompt_input"], row.get("answer")))
    return samples


def load_anchor(path: Path) -> list[AnchorItem]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        items.append(AnchorItem(row["artifact"], row["label"]))
    return items


def main() -> None:
    p = argparse.ArgumentParser(description="Run RQGM baseline on paper_review")
    p.add_argument("--tasks", required=True)
    p.add_argument("--anchor", required=True)
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--base-url", default=None,
                   help="OpenAI-compatible base URL (default: OpenAI endpoint)")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--budget", type=int, default=64)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    model = OpenAIChatModel(
        args.model, base_url=args.base_url, api_key_env=args.api_key_env)
    result = run_rqgm(
        "llm",
        config=RQGMConfig(budget=args.budget, seed=args.seed),
        model=model,
        tasks=load_samples(Path(args.tasks)),
        anchor=load_anchor(Path(args.anchor)),
    )
    print("best node    :", result.best_node_id)
    print("best belief  :", round(result.best_belief, 4))
    print("epochs       :", result.epochs)
    print("replacements :", [(r.from_id, r.to_id) for r in result.replacements])


if __name__ == "__main__":
    main()
