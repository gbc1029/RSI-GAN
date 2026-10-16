#!/usr/bin/env python3
"""Offline cost report for GAN run dirs (no network, no LLM calls).

Single source: the per-call proxy audit in ``logs/events.jsonl`` (llm_call
events carry role/scope/model/tokens/effort/latency/status). Wall clock comes
from ``inner_done`` / ``outer_done`` / the driver window. Task-side per-node
costs (NodeValue.cost_tokens / cost_wallclock_s) are cross-checked when
present (they exist only for runs whose loop recorded them).

Usage:
  venv_nat/bin/python analysis/gan_cost_report.py RUN_DIR [RUN_DIR ...]
      [--prices PRICES.json]

Outputs (per run, into <run>/score_analysis/):
  cost_report.md     per-role / per-outer / per-task-gen tables (+ $ estimate)
  cost_breakdown.csv one row per (scope, role)

Prices (optional): JSON mapping model (or "*") to
  {"prompt_per_1m": $/1Mtok, "completion_per_1m": $/1Mtok}.
  Read from --prices or $GAN_PRICE_TABLE; without prices the report is
  token-only (never guess currency).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _role_of(e: Dict[str, Any]) -> str:
    return "task" if e.get("scope_type") == "genid" else str(e.get("role") or "unknown")


def _agg(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    lat = [e.get("latency_ms") or 0 for e in calls]
    return {
        "calls": len(calls),
        "prompt_tokens": sum(e.get("prompt_tokens") or 0 for e in calls),
        "completion_tokens": sum(e.get("completion_tokens") or 0 for e in calls),
        "total_tokens": sum(e.get("total_tokens") or 0 for e in calls),
        "latency_s": round(sum(lat) / 1000.0, 1),
        "latency_median_ms": round(statistics.median(lat)) if lat else 0,
        "failures": sum(1 for e in calls if e.get("status") != 200),
        "effort": dict(Counter(str(e.get("reasoning_effort")) for e in calls)),
    }


def _cost_usd(calls: List[Dict[str, Any]], prices: Optional[dict]) -> Optional[float]:
    if not prices:
        return None
    total = 0.0
    for e in calls:
        p = prices.get(str(e.get("model"))) or prices.get("*")
        if not p:
            continue
        total += (e.get("prompt_tokens") or 0) / 1e6 * float(p.get("prompt_per_1m", 0))
        total += (e.get("completion_tokens") or 0) / 1e6 * float(p.get("completion_per_1m", 0))
    return round(total, 4)


def analyze(run_dir: str, prices: Optional[dict]) -> Dict[str, Any]:
    name = os.path.basename(os.path.normpath(run_dir))
    events = _load_jsonl(os.path.join(run_dir, "logs", "events.jsonl"))
    calls = [e for e in events if e.get("type") == "llm_call"]
    inner_done = {str(e.get("genid")): e for e in events if e.get("type") == "inner_done"}
    outer_done = {e.get("outer"): e for e in events if e.get("type") == "outer_done"}
    start_ev = next((e for e in events if e.get("type") == "driver_start"), None)
    done_ev = next((e for e in reversed(events) if e.get("type") == "driver_done"), None)
    # generic events stamp "ts"; the proxy stamps "timestamp" on llm_call rows
    t0 = (start_ev or {}).get("ts")
    t1 = (done_ev or {}).get("ts")
    scores = _load_jsonl(os.path.join(run_dir, "scores", "scores.jsonl"))
    score_by_gen = {str(s.get("genid")): s.get("score") for s in scores}
    model_cfg = next((e for e in events if e.get("type") == "model_config"), None)
    rollups = [e for e in events if e.get("type") == "cost_rollup"]

    # per-role totals
    by_role = defaultdict(list)
    for e in calls:
        by_role[_role_of(e)].append(e)
    roles = {r: _agg(by_role[r]) for r in sorted(by_role)}
    for r in roles:
        roles[r]["usd"] = _cost_usd(by_role[r], prices)

    # per-outer: outer-scoped by scope_id; task gens mapped via inner_done
    gen_outer = {g: e.get("outer") for g, e in inner_done.items()}
    per_outer: Dict[int, Dict[str, Any]] = defaultdict(dict)
    for e in calls:
        if e.get("scope_type") == "outer":
            per_outer[int(e.get("scope_id"))].setdefault(
                _role_of(e), []).append(e)
        elif e.get("scope_type") == "genid":
            o = gen_outer.get(str(e.get("scope_id")))
            if o is not None:
                per_outer[int(o)].setdefault("task", []).append(e)
    outer_table = []
    for o in sorted(per_outer):
        row = {"outer": o,
               "wallclock_s": (outer_done.get(o) or {}).get("duration_s"),
               "score_mean": None}
        gens_o = [g for g, oo in gen_outer.items() if oo == o]
        ss = [score_by_gen[g] for g in gens_o if score_by_gen.get(g) is not None]
        row["score_mean"] = round(sum(ss) / len(ss), 4) if ss else None
        row["gens"] = gens_o
        for r in ("planner", "evaluator", "task"):
            cl = per_outer[o].get(r, [])
            a = _agg(cl)
            row[r] = {"calls": a["calls"], "total_tokens": a["total_tokens"],
                      "latency_s": a["latency_s"]}
        row["usd"] = _cost_usd([e for r in ("planner", "evaluator", "task")
                                for e in per_outer[o].get(r, [])], prices)
        outer_table.append(row)

    # per-task-gen
    gen_table = []
    for g in sorted({str(e.get("scope_id")) for e in calls
                     if e.get("scope_type") == "genid"}, key=lambda x: int(x)):
        cl = [e for e in calls if e.get("scope_type") == "genid"
              and str(e.get("scope_id")) == g]
        a = _agg(cl)
        harness_s = (inner_done.get(g) or {}).get("duration_s")
        score = score_by_gen.get(g)
        gen_table.append({
            "genid": g, "outer": gen_outer.get(g),
            "calls": a["calls"], "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"], "latency_s": a["latency_s"],
            "harness_s": harness_s, "score": score,
            "tokens_per_point": (round(a["total_tokens"] / (score * 1000))
                                 if score not in (None, 0) else None),
            "usd": _cost_usd(cl, prices),
        })

    # wallclock bookkeeping
    t0 = (start_ev or {}).get("timestamp")
    t1 = (done_ev or {}).get("timestamp")
    run_wall = round(t1 - t0, 1) if (t0 and t1) else None
    llm_wall = round(sum(e.get("latency_ms") or 0 for e in calls) / 1000.0, 1)

    return {
        "name": name, "run_dir": run_dir,
        "model_cfg": {k: v for k, v in (model_cfg or {}).items()
                      if k.startswith("gan.")}, "prices": bool(prices),
        "roles": roles, "outer_table": outer_table, "gen_table": gen_table,
        "calls_total": len(calls),
        "tokens_total": sum(e.get("total_tokens") or 0 for e in calls),
        "failures_total": sum(1 for e in calls if e.get("status") != 200),
        "run_wall_s": run_wall,
        "sum_outer_wall_s": round(sum((v.get("duration_s") or 0)
                                      for v in outer_done.values()), 1),
        "llm_wall_s": llm_wall,
        "llm_wall_share": (round(llm_wall / run_wall, 3) if run_wall else None),
        "usd_total": _cost_usd(calls, prices),
        "rollup_events": len(rollups),
    }


def _fmt_tokens(n: Any) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def render_md(r: Dict[str, Any]) -> str:
    L = [f"# 成本报告：`{r['name']}`", ""]
    if r["model_cfg"]:
        L.append(f"- 模型配置：`{json.dumps(r['model_cfg'], ensure_ascii=False)}`")
    L.append(f"- 计价：{'按价表估算美元' if r['prices'] else '仅 token 口径（未提供价表）'}")
    L.append(f"- 总量：{r['calls_total']} 次调用 / {r['tokens_total']:,} tokens / "
             f"失败 {r['failures_total']}；运行墙钟 {r['run_wall_s']}s，"
             f"其中 LLM 延迟 {r['llm_wall_s']}s（占 {r['llm_wall_share'] or '—'}）"
             f"；outer 墙钟合计 {r['sum_outer_wall_s']}s")
    if r["rollup_events"]:
        L.append(f"- （本 run 由 driver 记录了 {r['rollup_events']} 个 cost_rollup 事件，"
                 f"本报告从 llm_call 明细独立重算，可互为校验）")
    L.append("")

    L += ["## 按角色", "",
          "| 角色 | 调用 | prompt tok | completion tok | 合计 tok | LLM 延迟 s | 延迟中位 ms | 失败 | effort |"
          + (" 估算 $ |" if r["prices"] else ""),
          "|---|---|---|---|---|---|---|---|---|" + ("---|" if r["prices"] else "")]
    for role, a in r["roles"].items():
        eff = ",".join(f"{k}×{v}" for k, v in sorted(a["effort"].items())) or "—"
        usd = f" {a['usd']} |" if r["prices"] else ""
        L.append(f"| {role} | {a['calls']} | {a['prompt_tokens']:,} | {a['completion_tokens']:,} "
                 f"| {a['total_tokens']:,} | {a['latency_s']} | {a['latency_median_ms']} "
                 f"| {a['failures']} | {eff} |{usd}")
    if r["prices"]:
        L.append(f"| **合计** | {r['calls_total']} | — | — | {r['tokens_total']:,} | "
                 f"{r['llm_wall_s']} | — | {r['failures_total']} | — | {r['usd_total']} |")
    L.append("")

    if r["outer_table"]:
        L += ["## 按 outer（角色调用 + 任务代合并到所属 outer）", "",
              "| outer | wallclock s | planner tok | evaluator tok | task tok | 分数均值 | gens |"
              + (" 估算 $ |" if r["prices"] else ""),
              "|---|---|---|---|---|---|---|" + ("---|" if r["prices"] else "")]
        for row in r["outer_table"]:
            usd = f" {row['usd']} |" if r["prices"] else ""
            L.append(f"| {row['outer']} | {row['wallclock_s']} "
                     f"| {row['planner']['total_tokens']:,} | {row['evaluator']['total_tokens']:,} "
                     f"| {row['task']['total_tokens']:,} | {row['score_mean']} "
                     f"| {','.join(map(str, row['gens']))} |{usd}")
        L.append("")

    if r["gen_table"]:
        L += ["## 按任务代（task child）", "",
              "| gen | outer | 调用 | prompt tok | completion tok | LLM 延迟 s | harness s | 分数 | tok/千分点 |"
              + (" 估算 $ |" if r["prices"] else ""),
              "|---|---|---|---|---|---|---|---|---|" + ("---|" if r["prices"] else "")]
        for g in r["gen_table"]:
            usd = f" {g['usd']} |" if r["prices"] else ""
            tpp = f"{g['tokens_per_point']:,}" if g["tokens_per_point"] else "—"
            L.append(f"| {g['genid']} | {g['outer']} | {g['calls']} | {g['prompt_tokens']:,} "
                     f"| {g['completion_tokens']:,} | {g['latency_s']} | {g['harness_s']} "
                     f"| {g['score']} | {tpp} |{usd}")
        L.append("")

    L.append("## 结论（自动摘要）")
    big = max(r["roles"].items(), key=lambda kv: kv[1]["total_tokens"], default=None)
    if big:
        share = big[1]["total_tokens"] / r["tokens_total"] if r["tokens_total"] else 0
        L.append(f"- token 大头：`{big[0]}`（{big[1]['total_tokens']:,}，占 {share:.0%}）——"
                 f"提示词预算优化应从这里下手。")
    L.append(f"- 失败调用 {r['failures_total']} 次；LLM 延迟占运行墙钟 "
             f"{r['llm_wall_share'] or '—'}（其余为沙箱/IO/子进程开销）。")
    if r["gen_table"]:
        avg_gen = statistics.mean(g["prompt_tokens"] + g["completion_tokens"]
                                  for g in r["gen_table"])
        L.append(f"- 任务代平均 {int(avg_gen):,} tokens/代（{len(r['gen_table'])} 代）")
    L.append(f"- 产物：cost_breakdown.csv / cost_report.md（本目录）。")
    return "\n".join(L) + "\n"


def write_csv(r: Dict[str, Any], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run", "scope", "role", "calls", "prompt_tokens",
                    "completion_tokens", "total_tokens", "latency_s",
                    "failures", "usd"])
        for role, a in r["roles"].items():
            w.writerow([r["name"], "run", role, a["calls"], a["prompt_tokens"],
                        a["completion_tokens"], a["total_tokens"], a["latency_s"],
                        a["failures"], a.get("usd") if r["prices"] else ""])
        for row in r["outer_table"]:
            for role in ("planner", "evaluator", "task"):
                c = row.get(role) or {}
                w.writerow([r["name"], f"outer_{row['outer']}", role,
                            c.get("calls", 0), "", "", c.get("total_tokens", 0),
                            c.get("latency_s", 0), "", row.get("usd") if r["prices"] else ""])
        for g in r["gen_table"]:
            w.writerow([r["name"], f"gen_{g['genid']}", "task", g["calls"],
                        g["prompt_tokens"], g["completion_tokens"],
                        g["prompt_tokens"] + g["completion_tokens"],
                        g["latency_s"], "", g.get("usd") if r["prices"] else ""])


def main() -> int:
    ap = argparse.ArgumentParser(description="offline cost report for GAN run dirs")
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--prices", help="JSON price table: {model or *: {prompt_per_1m, completion_per_1m}}")
    args = ap.parse_args()

    prices = None
    ppath = args.prices or os.environ.get("GAN_PRICE_TABLE")
    if ppath:
        with open(ppath, encoding="utf-8") as f:
            prices = json.load(f)
        print(f"[cost] price table: {ppath}")

    for run_dir in args.run_dirs:
        r = analyze(run_dir, prices)
        outdir = os.path.join(run_dir, "score_analysis")
        os.makedirs(outdir, exist_ok=True)
        md = render_md(r)
        with open(os.path.join(outdir, "cost_report.md"), "w", encoding="utf-8") as f:
            f.write(md)
        write_csv(r, os.path.join(outdir, "cost_breakdown.csv"))
        print(f"[OK] {r['name']}: {r['calls_total']} calls, {r['tokens_total']:,} tokens, "
              f"llm_wall={r['llm_wall_s']}s"
              + (f", est ${r['usd_total']}" if r["prices"] else ""))
        print(f"     {os.path.join(outdir, 'cost_report.md')}")
        print(f"     {os.path.join(outdir, 'cost_breakdown.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
