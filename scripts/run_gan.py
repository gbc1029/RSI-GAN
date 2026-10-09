"""CLI entry for the GAN dual-loop.

Example (paper_review, minimal):
    python scripts/run_gan.py --repo_root . --output_dir outputs/gan_paper_review \
        --domains paper_review --subset _filtered_100_train --num_samples 2 \
        --outer 1 --inner 2
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def main():
    p = argparse.ArgumentParser(description="Run the GAN dual-loop (planner/evaluator/task).")
    p.add_argument("--repo_root", default=".")
    p.add_argument("--output_dir", default="./outputs/gan_run")
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
                   help="Run all outers in one process WITHOUT the per-run code repo (legacy).")
    args = p.parse_args()

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

    if args.resume and args.force:
        p.error("--resume and --force are mutually exclusive")

    if args.in_process:
        # The in-process debug mode shares the driver's LLM supply model:
        # credentials stay inside a local ParentLLMProxy and the task chain
        # receives only a scoped token (GAN_LLM_PROXY_UNIX + control token).
        # Without this wiring the task harness would have NO LLM endpoint at
        # all (the task child env allowlist strips real credentials), so every
        # question would fail silently. In-process still runs WITHOUT the
        # outer sandbox/broker by design -- it is the developer-facing loop.
        import shutil as _shutil
        import tempfile as _tempfile

        from gan.build import build_gan_loop
        from gan.framework import models as _models
        from gan.framework.llm_proxy import ParentLLMProxy

        models = {
            "task": _models.resolve("gan.task"),
            "planner": _models.resolve("gan.planner"),
            "evaluator": _models.resolve("gan.evaluator"),
        }
        proxy_dir = _tempfile.mkdtemp(prefix="rsi-gan-inproc-proxy-")
        proxy_socket = os.path.join(proxy_dir, "p.sock")
        proxy = ParentLLMProxy(args.output_dir, proxy_socket, models=models)
        proxy.start()
        try:
            os.environ["GAN_LLM_PROXY_UNIX"] = proxy_socket
            os.environ["GAN_PROXY_CONTROL_TOKEN"] = proxy.issue_framework_scope(0)
            # Sampling scheme parity with driver mode (loop.yaml sampling.*):
            # same run-persistent seed file, so an in-process debug run of the
            # same output_dir stays comparable with a driver run.
            from gan.framework import task_execution as _tx
            from gan.framework.loader import load_gan_loop_config as _load_cfg
            _sampling = (_load_cfg().get("sampling") or {})
            _seed_base, _anchor = None, None
            if str(_sampling.get("mode", "seeded")) == "seeded":
                _info = _tx.ensure_sampling_seed(
                    os.path.abspath(args.output_dir), domains[0], args.subset,
                    os.path.abspath(args.repo_root), args.num_samples,
                    anchor_k=int(_sampling.get("anchor_k", 2)),
                )
                _seed_base = int(_info["base_seed"])
                _anchor = list(_info.get("anchor_ids") or [])
            loop = build_gan_loop(
                repo_root=args.repo_root,
                output_dir=args.output_dir,
                domains=domains,
                subset=args.subset,
                num_samples=args.num_samples,
                cfg_overrides=overrides or None,
                preflight=args.preflight,
                code_repo=False,
                sample_seed_base=_seed_base,
                anchor_ids=_anchor,
            )
            tree = loop.run()
        finally:
            proxy.close()
            _shutil.rmtree(proxy_dir, ignore_errors=True)
            os.environ.pop("GAN_LLM_PROXY_UNIX", None)
            os.environ.pop("GAN_PROXY_CONTROL_TOKEN", None)
        print(f"GAN loop done (in-process). task tree size={len(tree)}; "
              f"output_dir={os.path.abspath(args.output_dir)}")
    else:
        from gan.driver import run_gan_driver
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


if __name__ == "__main__":
    main()
