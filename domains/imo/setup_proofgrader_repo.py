import argparse
import json
import math
import os
import shutil
import fnmatch
import re
from pathlib import Path

from utils.constants import REPO_NAME
from utils.git_utils import apply_patch, commit_repo
from utils.gl_utils import get_patch_files, get_saved_score, load_archive_data


def get_mae_score(domain, output_dir, genid, split="train"):
    # Get score from eval file
    eval_dirname = f"{domain}_eval" if split == "train" else f"{domain}_eval_{split}"
    eval_file = os.path.join(output_dir, f"gen_{genid}/{eval_dirname}/report.json")
    score_key = "normalized_mean_absolute_error"
    try:
        with open(eval_file, "r") as f:
            eval_results = json.load(f)
        score = eval_results[score_key]
        # Filter nan values
        if math.isnan(score):
            score = None
        return score
    except Exception:
        return None  # If score is missing or file not found


# ----------------------------
# Packaging helpers
# ----------------------------
def _ensure_init_py(dirpath: Path):
    dirpath.mkdir(parents=True, exist_ok=True)
    init_file = dirpath / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")


def _write_pyproject(dst_dir: Path, package_name: str = "proofgrader", version: str = "0.1.0"):
    pyproject = dst_dir / "pyproject.toml"
    if pyproject.exists():
        return

    pyproject.write_text(
        f"""\
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "{version}"
requires-python = ">=3.9"

[tool.setuptools.packages.find]
where = ["."]
include = ["{package_name}*"]
""",
        encoding="utf-8",
    )


def _rewrite_imports_under(root: Path, package_name: str = "proofgrader"):
    """
    Rewrite absolute imports that would collide (domains, utils, agent) to be under proofgrader.*.
    Handles:
      - from domains.x import y      -> from proofgrader.domains.x import y
      - import domains.x             -> import proofgrader.domains.x
      - from utils.x import y        -> from proofgrader.utils.x import y
      - import utils.x               -> import proofgrader.utils.x
      - from agent.x import y        -> from proofgrader.agent.x import y
      - import agent.x               -> import proofgrader.agent.x
    """
    patterns = [
        # from domains.xxx import yyy  -> from proofgrader.domains.xxx import yyy
        (
            re.compile(r"(^|\n)(\s*)from\s+domains(\.[\w\.]+)?\s+import\s+", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}from {package_name}.domains{m.group(3) or ''} import ",
        ),
        # import domains.xxx -> import proofgrader.domains.xxx
        (
            re.compile(r"(^|\n)(\s*)import\s+domains(\.[\w\.]+)?(\s|$)", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}import {package_name}.domains{m.group(3) or ''}{m.group(4)}",
        ),
        # from utils.xxx import yyy -> from proofgrader.utils.xxx import yyy
        (
            re.compile(r"(^|\n)(\s*)from\s+utils(\.[\w\.]+)?\s+import\s+", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}from {package_name}.utils{m.group(3) or ''} import ",
        ),
        # import utils.xxx -> import proofgrader.utils.xxx
        (
            re.compile(r"(^|\n)(\s*)import\s+utils(\.[\w\.]+)?(\s|$)", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}import {package_name}.utils{m.group(3) or ''}{m.group(4)}",
        ),
        # from agent.xxx import yyy -> from proofgrader.agent.xxx import yyy
        (
            re.compile(r"(^|\n)(\s*)from\s+agent(\.[\w\.]+)?\s+import\s+", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}from {package_name}.agent{m.group(3) or ''} import ",
        ),
        # import agent.xxx -> import proofgrader.agent.xxx
        (
            re.compile(r"(^|\n)(\s*)import\s+agent(\.[\w\.]+)?(\s|$)", re.M),
            lambda m: f"{m.group(1)}{m.group(2)}import {package_name}.agent{m.group(3) or ''}{m.group(4)}",
        ),
        # String literals with module paths: "agent.xxx" -> "proofgrader.agent.xxx"
        (
            re.compile(r'(["\'])agent\.([\w\.]+)'),
            lambda m: f'{m.group(1)}{package_name}.agent.{m.group(2)}',
        ),
    ]

    for py in root.rglob("*.py"):
        try:
            txt = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        new = txt
        for pat, repl in patterns:
            new = pat.sub(lambda m: repl(m), new)

        if new != txt:
            py.write_text(new, encoding="utf-8")


def _packageize_proofgrader_repo(dst_dir: str, package_name: str = "proofgrader"):
    """
    Convert dst_dir into an installable package:
      - create dst_dir/proofgrader/
      - move task_agent.py, domains/, utils/, agent/ under proofgrader/
      - add __init__.py files
      - write pyproject.toml
      - rewrite imports so proofgrader uses proofgrader.domains / proofgrader.utils / proofgrader.agent
      - keep a top-level task_agent.py shim for compatibility
    """
    dst = Path(dst_dir).resolve()
    if not dst.exists():
        raise FileNotFoundError(f"dst_dir does not exist: {dst}")

    pkg = dst / package_name
    _ensure_init_py(pkg)

    # Move common top-level modules/folders into the package namespace if they exist
    move_candidates = ["task_agent.py", "domains", "utils", "agent"]
    for name in move_candidates:
        src = dst / name
        if not src.exists():
            continue

        dst_path = pkg / name
        if dst_path.exists():
            continue

        shutil.move(str(src), str(dst_path))

    # Ensure __init__.py for subpackages if we moved them
    for subpkg in ["domains", "utils", "agent"]:
        p = pkg / subpkg
        if p.exists() and p.is_dir():
            _ensure_init_py(p)

    # Add a top-level shim so old code paths still work: proofgrader_repo/task_agent.py
    shim = dst / "task_agent.py"
    if not shim.exists() and (pkg / "task_agent.py").exists():
        shim.write_text(f"from {package_name}.task_agent import *\n", encoding="utf-8")

    # Write packaging config
    _write_pyproject(dst, package_name=package_name)

    # Rewrite imports inside the packaged code
    _rewrite_imports_under(pkg, package_name=package_name)


# ----------------------------
# Original setup functions
# ----------------------------
def setup_default(dst_dir, proofautograder=False):
    """
    Create ./proofgrader_repo by copying from current repo, excluding some folders.
    Then package-ize it into an installable package named 'proofgrader'.
    """
    src = "."
    excluded_dirs = {
        ".claude",
        "outputs",
        "analysis",
        "misc",
        "baselines",
        # IMPORTANT:
        # If your proofgrader task_agent imports 'domains.*', 'utils.*', or 'agent.*',
        # you likely need to copy those folders into proofgrader_repo.
        # So do NOT exclude them here unless you're sure.
        "domains",
        # "utils",
        # "agent",
        "proofgrader_repo",
    }
    excluded_files = {
        "Dockerfile",
        ".dockerignore",
        "LICENSE.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "run_task_agent.py",
    }
    excluded_patterns = [
        "venv*",
        "__pycache__*",
        "*.png",
        "*ensemble*",
        "*select_next_parent*",
    ]

    def ignore(path, names):
        ignored = set()

        for name in names:
            full = os.path.join(path, name)

            # Excluded directories
            if name in excluded_dirs and os.path.isdir(full):
                ignored.add(name)
                continue

            # Excluded files
            if name in excluded_files and os.path.isfile(full):
                ignored.add(name)
                continue

            # Pattern matches
            for pattern in excluded_patterns:
                if fnmatch.fnmatch(name, pattern):
                    ignored.add(name)
                    break

        return ignored

    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir)

    shutil.copytree(src, dst_dir, ignore=ignore)

    # Use proofautograder instead of default task_agent
    if proofautograder:
        src_file = os.path.join(src, "baselines/imo_grading/proofautograder.py")
        dst_file = os.path.join(dst_dir, "task_agent.py")
        if os.path.exists(dst_file):
            os.remove(dst_file)
        shutil.copyfile(src_file, dst_file)
        print(f"Copied {src_file} → {dst_file}")

    # Package-ize the copied repo so it can be installed/imported cleanly
    _packageize_proofgrader_repo(dst_dir, package_name="proofgrader")


def setup_from_run(dst_dir, generate_dir):
    """
    Create ./proofgrader_repo by copying best node from an optimization run,
    applying patches, committing, then package-ize into 'proofgrader'.
    """
    N = 3
    domain = "imo_grading"
    archive = load_archive_data(os.path.join(generate_dir, "archive.jsonl"), last_only=True)["archive"]

    # Using mae
    scores = [get_mae_score(domain, generate_dir, genid, split="val") for genid in archive]
    valid = [i for i, s in enumerate(scores) if s is not None]
    top_indices = sorted(valid, key=lambda i: scores[i])[:N]
    top_genids = [archive[i] for i in top_indices]
    print("Genids with lowest mae:", top_genids)

    # Using label accuracy
    saved_scores = [get_saved_score(domain, generate_dir, genid, split="val") for genid in archive]
    valid_saved = [i for i, s in enumerate(saved_scores) if s is not None]
    top_saved_indices = sorted(valid_saved, key=lambda i: saved_scores[i], reverse=True)[:N]
    top_saved_genids = [archive[i] for i in top_saved_indices]
    print("Genids with highest label accuracy:", top_saved_genids)

    # Select genid to use
    chosen_genid = top_genids[0]
    print(f"Chosen genid: {chosen_genid}")
    patch_files = get_patch_files(generate_dir, chosen_genid)
    root_dir = os.path.join(generate_dir, f"gen_initial/{REPO_NAME}")

    # Create proofgrader_repo
    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir)
    shutil.copytree(root_dir, dst_dir)

    # Apply patches
    for patch_file in patch_files:
        with open(patch_file, "r") as f:
            patch_str = f.read()
        apply_patch(dst_dir, patch_str)

    # Commit it just in case
    _ = commit_repo(dst_dir)

    # Package-ize the copied repo so it can be installed/imported cleanly
    _packageize_proofgrader_repo(dst_dir, package_name="proofgrader")


def main():
    parser = argparse.ArgumentParser(description="Setup proof grader to be used for imo_proof domain.")
    parser.add_argument(
        "--proofautograder",
        default=False,
        action="store_true",
        help="Whether to use proofautograder instead of default task agent",
    )
    parser.add_argument(
        "--generate_dir",
        type=str,
        default=None,
        help="Path to the directory containing an optimization run to get the best proof grader.",
    )
    args = parser.parse_args()

    proofautograder = args.proofautograder
    generate_dir = args.generate_dir
    dst_dir = "./proofgrader_repo"
    print(f"Setting up {dst_dir}...")

    if generate_dir:
        setup_from_run(dst_dir, generate_dir)
    else:
        setup_default(dst_dir, proofautograder=proofautograder)

    print("\nDone.")
    print("Next steps:")
    print("pip install -e ./proofgrader_repo")


if __name__ == "__main__":
    main()
