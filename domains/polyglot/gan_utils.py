"""GAN-side data adapter for the polyglot domain (Arch 2).

The GAN task child edits exercise files inside its sandbox workspace and
produces a unified diff (the prediction); the parent applies that patch inside
the eval-only container and scores `resolved/total`.

Named ``gan_utils`` (not ``utils``) because ``domains/polyglot/utils.py`` is
the pre-existing DGM docker-helper module; ``domains/harness.py`` resolves this
module explicitly for the polyglot domain.

`repo` in the metadata is a LOCAL exercise directory under
``domains/polyglot/polyglot-benchmark`` -- exercise-relative starter files are
read from that tree (local snapshot semantics; documented deviation from the
metadata's base_commit, which references the upstream exercism history).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

QUESTION_ID = "question_id"
# Polyglot has no label column: the objective outcome (resolved) is produced
# by the eval container on the parent side. Kept for interface parity.
GROUND_TRUTH_KEY = "resolved"

BENCH_ROOT = "domains/polyglot/polyglot-benchmark"
_REPO_PREFIX = BENCH_ROOT + "/"


def _repo_rel(exercise_dir: str) -> str:
    norm = exercise_dir.replace("\\", "/").rstrip("/")
    return norm[len(_REPO_PREFIX):] if norm.startswith(_REPO_PREFIX) else norm


def load_instances(
    dataset_root: str,
    task_list: Optional[List[str]] = None,
    num_samples: int = 0,
) -> List[Dict[str, Any]]:
    """Materialize GAN question rows from the polyglot metadata.

    Each row carries the exercise's starter files (solution skeletons +
    invalidator files such as go.mod) inline as a JSON string, so the sandboxed
    child can rebuild the exercise workspace from the questions.csv alone.

    Path convention: EXERCISE-RELATIVE (e.g. ``bottle_song.go``) -- the eval
    container checks the exercise out at ``/testbed`` root and applies the
    child's patch there with `git apply`; the instance's `repo`/`exercise_dir`
    stay informational.
    """
    meta_path = os.path.normpath(os.path.join(
        os.path.abspath(dataset_root), BENCH_ROOT, "..",
        "polyglot_benchmark_metadata.json"))
    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    rows: List[Dict[str, Any]] = []
    for entry in metadata:
        iid = entry.get("instance_id")
        if task_list and iid not in task_list:
            continue
        repo_dir = os.path.join(os.path.abspath(dataset_root), entry["repo"])
        starter: Dict[str, str] = {}
        for group in ("solution", "invalidator"):
            for rel in entry.get("files", {}).get(group, []):
                path = os.path.join(repo_dir, rel)
                if not os.path.isfile(path):
                    raise FileNotFoundError(
                        f"polyglot starter file missing: {path} (instance {iid})")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    starter[rel] = f.read()
        rows.append({
            QUESTION_ID: iid,
            "language": entry.get("language", ""),
            "problem_statement": entry.get("problem_statement", ""),
            "test_description": entry.get("blurb", ""),
            "exercise_dir": _repo_rel(entry["repo"]),
            "starter_files": json.dumps(starter, ensure_ascii=False),
            "solution_paths": json.dumps(
                list(entry.get("files", {}).get("solution", [])),
                ensure_ascii=False),
        })
    if num_samples and num_samples > 0:
        rows = rows[:num_samples]
    return rows


def format_input_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Sandbox inputs for one polyglot question (CSV round-trip safe)."""
    starter = row.get("starter_files", "{}")
    paths = row.get("solution_paths", "[]")
    return {
        "domain": "polyglot",
        "problem_statement": row.get("problem_statement", ""),
        "language": row.get("language", ""),
        "test_description": row.get("test_description", ""),
        "exercise_dir": row.get("exercise_dir", ""),
        "files": json.loads(starter) if isinstance(starter, str) else dict(starter),
        "solution_paths": json.loads(paths) if isinstance(paths, str) else list(paths),
    }
