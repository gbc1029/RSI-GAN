"""Convert the paper_review dataset into RQGM's tasks/anchor JSONL format.

RQGM's shipped ``llm_providers`` consume two JSONL files (see
``examples/llm_coevolution.py`` in the RQGM repo):

  tasks.jsonl   -> one JSON per line: {"task_id", "prompt_input", "answer"}
  anchor.jsonl  -> one JSON per line: {"artifact", "label"}  (label: Accept|Reject)

Mapping for paper_review:

  task_id      = question_id
  prompt_input = task brief + paper text  (the coder reads this and answers
                 "accept" / "reject")
  answer       = ground-truth outcome ("accept" / "reject")

KNOWN LIMITATION (also documented in rqgm_bridge/README.md): RQGM's reviewer
only ever sees the coder's *artifact text*, never the paper. On paper_review
the answer is context-dependent ("accept" for a *specific* paper), so the
reviewer role degrades to a well-formedness check on this domain, and the
anchor set below calibrates formatting only (not content correctness).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# paper_review 的 paper_text（论文全文）可超 csv 默认 128KB 字段上限
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Keep in sync with gan/framework/domains.yaml -> paper_review.task_brief.
PAPER_REVIEW_TASK_BRIEF = (
    "This is peer review: read the paper and decide whether it should be "
    "accepted or rejected. Your final answer must be exactly one word: "
    "accept or reject."
)

# Formatting-level anchors. Because RQGM's reviewer cannot see the paper, these
# calibrate only "is the answer a well-formed single-word verdict" — NOT "is
# the verdict content-correct for this paper". This is the documented
# degradation of the reviewer role on context-dependent tasks.
_FORMAT_ANCHORS = [
    ("accept", "Accept"),
    ("reject", "Accept"),
    (
        "this paper is clearly a strong accept because the methodology is "
        "rigorous and the empirical results are significant and compelling",
        "Reject",
    ),
    ("", "Reject"),
    ("I think maybe accept or perhaps reject, hard to say", "Reject"),
]


def build_prompt_input(task_brief: str, paper_text: str) -> str:
    return f"{task_brief}\n\nPaper:\n{paper_text}"


def csv_to_tasks(csv_path: Path, task_brief: str = PAPER_REVIEW_TASK_BRIEF) -> list[dict]:
    tasks = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            qid = row.get("question_id") or f"q{i}"
            paper = row.get("paper_text")
            outcome = row.get("outcome")
            if paper is None or outcome is None:
                raise ValueError(
                    "paper_review CSV is missing expected columns "
                    "(question_id / paper_text / outcome); got columns: "
                    f"{list(row.keys())}")
            tasks.append({
                "task_id": qid,
                "prompt_input": build_prompt_input(task_brief, paper),
                "answer": str(outcome).strip().lower(),
            })
    return tasks


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Convert paper_review dataset -> RQGM tasks/anchor JSONL")
    p.add_argument("--csv", required=True,
                   help="path to dataset_filtered_100_*.csv")
    p.add_argument("--out-dir", default="baselines/rqgm_bridge/data",
                   help="output dir (default: baselines/rqgm_bridge/data)")
    args = p.parse_args()

    out = Path(args.out_dir)
    tasks = csv_to_tasks(Path(args.csv))
    anchors = [{"artifact": a, "label": label} for a, label in _FORMAT_ANCHORS]
    write_jsonl(out / "tasks.jsonl", tasks)
    write_jsonl(out / "anchor.jsonl", anchors)
    print(f"wrote {len(tasks)} tasks  -> {out / 'tasks.jsonl'}")
    print(f"wrote {len(anchors)} anchors -> {out / 'anchor.jsonl'}")


if __name__ == "__main__":
    main()
