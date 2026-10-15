"""CLI entry for the GAN dual-loop.

Example (paper_review, minimal):
    python scripts/run_gan.py --repo_root . --output_dir ../runs/gan_paper_review \
        --domains paper_review --subset _filtered_100_train --num_samples 2 \
        --outer 1 --inner 2
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run_offline_analysis(output_dir: str, skip: bool = False) -> int:
    """Post-run offline data analysis into <output_dir>/score_analysis/.

    Runs the local (gitignored) analyzers on whatever artifacts landed:
    score extraction (scores_extract.csv / REPORT.md / significance.txt /
    progress.png), lineage graphs (lineage_{inner,outer}_{lenient,strict}.png
    + lineage_audit.md), and the cost report (cost_report.md /
    cost_breakdown.csv; token-only unless --prices/$GAN_PRICE_TABLE).
    NO network, NO LLM calls.

    Deliberately runs EVEN when the loop failed or was interrupted: the
    artifacts are the point (driver-mode lesson: judge a round by its events
    and artifacts, not by the exit code alone). A missing analyzer script
    degrades to a printed note, never masks the loop's own result.
    Returns the FIRST non-zero analyzer rc (0 when all ok), after running both.
    """
    if skip:
        return 0
    import shutil
    analyzers = [
        os.path.join(REPO_ROOT, "scripts", "local", "gan_scores_extract.py"),
        os.path.join(REPO_ROOT, "scripts", "local", "gan_lineage_graph.py"),
        os.path.join(REPO_ROOT, "scripts", "local", "gan_cost_report.py"),
    ]
    if not os.path.isdir(os.path.join(REPO_ROOT, "scripts", "local")):
        return 0  # upstream checkout without the local tools: nothing to run
    py = sys.executable or shutil.which("python3")
    rc_first = 0
    for path in analyzers:
        if not os.path.isfile(path):
            print(f"[analysis] missing {os.path.basename(path)}; skipped")
            continue
        proc = subprocess.run([py, path, output_dir],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT)
        status = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
        print(f"[analysis] {os.path.basename(path)}: {status} "
              f"-> {os.path.join(output_dir, 'score_analysis')}")
        if proc.returncode != 0 and rc_first == 0:
            rc_first = proc.returncode
    return rc_first


def _parse_ablation_value(raw: str):
    """Type-infer a single --ablation KEY=VALUE value.

    comma-separated -> list (elements type-inferred individually);
    true/false/yes/no/1/0 -> bool; int/float; else string.
    """
    raw = raw.strip()
    if "," in raw:
        return [_parse_ablation_value(p) for p in raw.split(",") if p.strip()]
    low = raw.lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def main():
    p = argparse.ArgumentParser(description="Run the GAN dual-loop (planner/evaluator/task).")
    p.add_argument("--repo_root", default=".")
    p.add_argument("--output_dir", default=None,
                   help="Run directory outside repo_root (default: ../runs/gan_run)")
    p.add_argument("--domains", default="paper_review",
                   help="Task domain. Currently exactly ONE domain is supported "
                        "(passing multiple values is rejected at this entry; "
                        "multi-domain evaluation is not implemented yet). This is "
                        "the ONLY place with a default; downstream layers take the "
                        "value as required input and validate it.")
    p.add_argument("--subset", default="_filtered_100_train")
    p.add_argument("--num_samples", type=int, default=2)
    p.add_argument("--outer", type=int, default=None)
    p.add_argument("--inner", type=int, default=None)
    p.add_argument("--resume", action="store_true",
                   help="Continue an interrupted run: skip completed outers and resume at "
                        "the next one (restores the last OUTER-boundary checkpoint; never "
                        "resumes from a crashed outer's partial state).")
    p.add_argument("--force", action="store_true",
                   help="Re-run from outer 1 on an output_dir that already has completed "
                        "outers (legacy duplicate-prone behaviour; explicit escape hatch).")
    p.add_argument("--preflight", action="store_true",
                   help="Probe configured models once before running (fail-fast).")
    p.add_argument("--in-process", action="store_true",
                   help="DISABLED legacy mode: bypasses the required process sandbox.")
    p.add_argument("--skip-analysis", action="store_true",
                   help="Skip the offline post-run analysis (scripts/local/gan_*.py -> "
                        "<output_dir>/score_analysis/). Default: run it after the loop "
                        "exits, even on a failed run (judge a round by its artifacts).")
    p.add_argument("--ablation", action="append", default=None, metavar="KEY=VALUE",
                   help="Ablation switch (repeatable). Maps into the loop.yaml "
                        "'ablation.*' section; 'deny_deep' is aliased to "
                        "source_access.deny_deep (B1). Values are type-inferred: "
                        "true/false, int/float, comma-separated -> list, else string. "
                        "Example: --ablation disable_planner=true "
                        "--ablation trajectory_visibility=all")
    args = p.parse_args()

    if args.in_process:
        p.error(
            "--in-process is disabled because it bypasses the required role/task "
            "sandbox; use the default driver mode"
        )

    repo_root = os.path.realpath(os.path.abspath(os.path.expanduser(args.repo_root)))
    if args.output_dir is None:
        args.output_dir = os.path.join(os.path.dirname(repo_root), "runs", "gan_run")
    from gan.driver import _validate_run_paths
    args.repo_root, args.output_dir = _validate_run_paths(repo_root, args.output_dir)
    env_file = os.path.join(args.repo_root, ".env")
    if os.path.isfile(env_file):
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_file, override=False)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    # Post-run analysis always targets THIS run's output_dir. The loop itself
    # may fail (raise) — analysis still runs first via the wrapper below.
    from gan.framework import paths as _paths  # noqa: F401  (early sanity)
    out_abs = os.path.abspath(args.output_dir)

    def _finish(loop_rc: int) -> int:
        ana_rc = _run_offline_analysis(out_abs, skip=args.skip_analysis)
        return loop_rc if loop_rc != 0 else ana_rc

    # litellm offline cost map: the deployment has no github egress, so the
    # remote fetch burns ~10s of retries in every process before falling back
    # to the bundled copy. Opt-out by exporting it explicitly.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    # --domains: the single domain entry. Multi-value / empty are rejected here,
    # BEFORE any task execution. (Multi-domain evaluation is not implemented yet;
    # this parse shape is kept so the future extension is additive.)
    domains = None
    if args.domains:
        domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        p.error("--domains 解析为空；请提供至少一个域名")
    if len(domains) != 1:
        p.error(f"--domains 目前仅支持单个域（收到 {len(domains)} 个：{domains}）；"
                f"多域评测尚未实现，本次不执行任务")

    overrides = {}
    if args.outer is not None:
        overrides.setdefault("loop", {})["outer_generations"] = args.outer
    if args.inner is not None:
        overrides.setdefault("loop", {})["inner_max"] = args.inner

    # --ablation KEY=VALUE (repeatable) -> loop.yaml 'ablation.*' section.
    # 'deny_deep' is a B1 alias resolved to source_access.deny_deep (its only
    # consumer); everything else lands under the ablation block.
    if args.ablation:
        ablation = {}
        for kv in args.ablation:
            if "=" not in kv:
                p.error(f"--ablation 需要 KEY=VALUE 形式（收到 {kv!r}）")
            key, raw = kv.split("=", 1)
            key = key.strip()
            value = _parse_ablation_value(raw.strip())
            if key == "deny_deep":
                overrides.setdefault("source_access", {})["deny_deep"] = value
            else:
                ablation[key] = value
        if ablation:
            overrides["ablation"] = {**overrides.get("ablation", {}), **ablation}

    if args.resume and args.force:
        p.error("--resume and --force are mutually exclusive")

    from gan.driver import run_gan_driver
    _exc_repr = None
    try:
        code_root = run_gan_driver(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            domains=domains,
            subset=args.subset,
            num_samples=args.num_samples,
            inner=args.inner,
            cfg_overrides=overrides or None,
            preflight=args.preflight,
            resume=args.resume,
            force=args.force,
        )
        print(f"GAN driver done. code_root={code_root}; "
              f"output_dir={os.path.abspath(args.output_dir)}")
    except BaseException as _exc:  # noqa: BLE001 — artifacts outlive the error
        _exc_repr = repr(_exc)
        print(f"GAN driver exited with {_exc_repr}; analyzing landed artifacts",
              file=sys.stderr)
    _finish(1 if _exc_repr else 0)
    if _exc_repr is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
