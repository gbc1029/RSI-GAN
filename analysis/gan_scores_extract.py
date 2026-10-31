#!/usr/bin/env python3
"""Offline score extraction + analysis for GAN run dirs (runs/<genid>/ layout).

Reuses the DGM-H analysis toolkit `analysis/analysis_utils.py`
(compute_bootstrap_ci / save_significance_tests) and adapts the
`analysis/plot_progress.py` progress-curve idea to the GAN layout.

Offline only: reads run artifacts + dataset ground truth; no network, no LLM.

Usage:
  venv_nat/bin/python analysis/gan_scores_extract.py RUN_DIR [RUN_DIR ...]

Outputs (per run, into <run>/score_analysis/):
  scores_extract.csv   per-sample rows: score + parse method
  significance.txt     pairwise tests (reused save_significance_tests)
  progress.png         per-gen curves + per-question correctness
  REPORT.md            the analysis report

One score per run, computed by the SAME rule the run's online scorer used
(report.json `score_mode` self-describes it): docker_resolved -> the broker's
container verdicts; lenient -> verdict regex extraction via
gan.framework.verdict.parse_verdict (paper_review; identical function, so
offline == online by construction); anything else -> exact lowercase label
match (search_arena and the other label domains, current online rule).
Verdicts that do not parse count as wrong; the denominator never shrinks.

Ground truth: auto-detected from each domain's dataset catalogue.  For
imo_proof, proofbench identifies the questions while report.json supplies the
proof score; proof text is never parsed as a verdict.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analysis.analysis_utils import compute_bootstrap_ci, save_significance_tests  # noqa: E402
from gan.framework import cost_ledger  # noqa: E402
from gan.framework.verdict import parse_verdict  # noqa: E402
from scipy import stats  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

csv.field_size_limit(10 ** 7)


# ---------------------------------------------------------------- loaders
def load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _dataset_spec(domain):
    """Return (directory, candidate files, id column, label columns)."""
    if domain == "imo_grading":
        return (
            "imo",
            ["gradingbench_filtered_100_train.csv",
             "gradingbench_filtered_100_test.csv",
             "gradingbench_filtered_100_val.csv", "gradingbench_filtered.csv",
             "gradingbench.csv"],
            "Grading ID",
            "Reward",
        )
    if domain == "imo_proof":
        return (
            "imo",
            ["proofbench_filtered_100_train.csv",
             "proofbench_filtered_100_test.csv",
             "proofbench_filtered_100_val.csv", "proofbench_filtered.csv",
             "proofbench.csv"],
            "Problem ID",
            None,
        )
    return (
        domain,
        ["dataset_filtered_100_train.csv", "dataset_filtered_100_test.csv",
         "dataset_filtered_100_val.csv", "dataset_filtered.csv", "dataset.csv"],
        "question_id",
        ("winner", "outcome"),
    )


def detect_dataset(run_dir, domain, question_ids):
    """Pick the domain dataset that contains every observed question id."""
    folder, cands, id_col, label_col = _dataset_spec(domain)
    ddir = os.path.join(REPO, "domains", folder)
    if not os.path.isdir(ddir):
        return None, None
    wanted = set(question_ids)
    for name in cands:
        path = os.path.join(ddir, name)
        if not os.path.exists(path):
            continue
        try:
            truth = {}
            for r in csv.DictReader(open(path)):
                qid = (r.get(id_col) or r.get("question_id") or "").strip()
                if not qid:
                    continue
                if label_col is None:
                    truth[qid] = None
                else:
                    label_cols = (label_col,) if isinstance(label_col, str) else label_col
                    lab = next(
                        ((r.get(col) or "").strip().lower() for col in label_cols
                         if (r.get(col) or "").strip()),
                        "",
                    )
                    if lab:
                        truth[qid] = lab
        except Exception:
            continue
        if (wanted and wanted.issubset(truth)) or (domain == "imo_proof" and not wanted):
            return path, truth
    return None, None


def extract_run(run_dir):
    """Pull everything score-relevant out of one GAN run dir."""
    name = os.path.basename(os.path.normpath(run_dir))
    scores = load_jsonl(os.path.join(run_dir, "scores", "scores.jsonl"))
    events = load_jsonl(os.path.join(run_dir, "logs", "events.jsonl"))
    gens = [str(s["genid"]) for s in scores]
    domain = (scores or [{}])[0].get("domain")
    _, _, native_id_col, _ = _dataset_spec(domain or "search_arena")

    # events
    inner_done, harness_done = {}, {}
    model_cfg, stagnation, brief_missing = None, [], 0
    llm_calls, self_receipts = [], 0
    for e in events:
        t = e.get("type")
        if t == "inner_done":
            inner_done[str(e.get("genid"))] = e
        elif t == "task_harness_done":
            harness_done[str(e.get("genid"))] = e
        elif t == "model_config" and model_cfg is None:
            model_cfg = e
        elif t == "stagnation_break":
            stagnation.append((e.get("outer"), e.get("inner")))
        elif t == "task_brief_missing":
            brief_missing += 1
        elif t == "llm_call":
            llm_calls.append(e)
        elif t == "self_receipt":
            self_receipts += 1

    # per-sample extraction
    rows, per_gen = [], {}
    sha_prev = {}
    for s in scores:
        g = str(s["genid"])
        rep = load_json(os.path.join(run_dir, "runs", g, "report.json")) or {}
        ev = load_json(os.path.join(run_dir, "runs", g, "eval.json")) or {}
        rc = load_json(os.path.join(run_dir, "runs", g, "patch_receipt.json")) or {}
        preds, pred_err = {}, None
        ppath = os.path.join(run_dir, "runs", g, "predictions.csv")
        if os.path.exists(ppath):
            try:
                for row in csv.DictReader(open(ppath)):
                    qid = (row.get("question_id") or row.get(native_id_col) or "").strip()
                    if qid:
                        preds[qid] = row.get("prediction") or ""
            except Exception as exc:
                pred_err = str(exc)
        issues = ev.get("issues") or []
        ops = ((rc.get("design") or {}).get("ops")) or []
        # cost ledger (docs/10 §9 A): runs/<genid>/cost.json — task cost from
        # the proxy audit, plan/evaluate session sums from the trajectories.
        # Absent file (pre-ledger runs) -> None columns, never 0.
        cost = load_json(os.path.join(run_dir, "runs", g, "cost.json")) or {}
        sha = s.get("report_sha")
        rec = dict(
            genid=g, score=s.get("score"), coverage=s.get("coverage"),
            score_status=s.get("score_status"), valid_parent=s.get("valid_parent"),
            report_sha=sha, same_report_as_prev=bool(sha and sha_prev.get("prev") == sha),
            predicted_score=ev.get("predicted_score"),
            n_issues=len(issues),
            sev=dict(Counter(i.get("severity") for i in issues)),
            n_ops=len(ops),
            ops=dict(Counter(o.get("op") for o in ops)),
            prompt_changed=bool((rc.get("design") or {}).get("prompt_changed")),
            harness_s=(harness_done.get(g) or {}).get("duration_s"),
            inner_s=(inner_done.get(g) or {}).get("duration_s"),
            cost_tokens=(cost.get("task") or {}).get("cost_tokens"),
            cost_wallclock_s=(cost.get("task") or {}).get("cost_wallclock_s"),
            plan_tokens=(cost.get("planner_plan") or {}).get("total_tokens"),
            eval_tokens=(cost.get("evaluator_evaluate") or {}).get("total_tokens"),
            preds=preds, report=rep, pred_err=pred_err,
        )
        sha_prev["prev"] = sha
        per_gen[g] = rec
        for qid, raw in sorted(preds.items()):
            rows.append(dict(run=name, genid=g, question_id=qid, raw_head=re.sub(r"\s+", " ", (raw or ""))[:120],
                             score=None, method=None))
    # the run's own scoring rule (report.json self-describes it): docker
    # runs say "docker_resolved", verdict-extraction runs say "lenient",
    # exact-match runs say "strict"; proof runs are re-labeled in main().
    score_mode = None
    for g in gens:
        smode = (per_gen[g].get("report") or {}).get("score_mode")
        if smode:
            score_mode = smode
            break
    return dict(name=name, dir=run_dir, gens=gens, per_gen=per_gen, rows=rows,
                model_cfg=model_cfg, stagnation=stagnation, brief_missing=brief_missing,
                llm_calls=llm_calls, self_receipts=self_receipts,
                sessions=cost_ledger.read_session_costs(run_dir),
                domain=domain, score_mode=score_mode,
                events=events)


_VERDICT_LABELS = ("accept", "reject")


def attach_truth(run, truth):
    """Score every row by the run's own online rule (report.json score_mode).

    ``lenient`` -> verdict regex extraction via the SAME parse_verdict the
    online scorer calls (labels accept/reject) — offline == online by
    construction. Anything else -> exact lowercase label match (the current
    online rule of search_arena and the other label domains). Undecided
    parses count as wrong; the denominator never shrinks."""
    mode = run.get("score_mode")
    for r in run["rows"]:
        raw = run["per_gen"][r["genid"]]["preds"].get(r["question_id"], "")
        tlab = truth.get(r["question_id"])
        r["truth"] = tlab
        if mode == "lenient":
            v = parse_verdict(str(raw), labels=_VERDICT_LABELS)
            r["method"] = v.method if v.label else (
                "empty" if not (raw or "").strip() else "unparsed")
            r["score"] = int(bool(v.label and tlab and v.label == tlab))
        else:
            candidate = (raw or "").strip().lower()
            r["method"] = "label_exact" if candidate else "empty"
            r["score"] = int(bool(candidate and tlab and candidate == tlab))


def attach_polyglot_truth(run):
    """Polyglot has no dataset*.csv: the broker-side docker eval IS the ground
    truth. Derive per-question correctness from runs/<genid>/report.json's
    resolved list (container-verified), which extract_run already loaded as
    per_gen[genid]["report"]. The prediction is a unified diff, so the score
    is the container's own verdict, never a text parse."""
    n = 0
    for r in run["rows"]:
        rep = (run["per_gen"].get(r["genid"]) or {}).get("report") or {}
        hit = r["question_id"] in set(rep.get("resolved") or [])
        r["truth"] = "resolved" if hit else "unresolved"
        r["score"] = int(hit)
        r["method"] = "docker_resolved"
        n += int(hit)
    return n


def report_crosscheck(run):
    """Compare recomputed score with the harness report.json.

    report.json's `total` excludes invalid predictions (coverage<1), so only the
    correctness COUNT is compared; a denominator difference is expected there.
    Because the offline score reuses the online rule (score_mode dispatch),
    this is a true offline==online probe: a mismatch means implementation
    drift, not a convention difference.
    """
    mism, denom = [], []
    for g in run["gens"]:
        rep = run["per_gen"][g]["report"]
        if not rep:
            continue
        mine = sum(r["score"] or 0 for r in run["rows"] if r["genid"] == g)
        theirs = rep.get("total_correct")
        if theirs is not None and mine != theirs:
            mism.append((g, mine, theirs))
        nrows = len([r for r in run["rows"] if r["genid"] == g])
        if rep.get("total") is not None and nrows != rep["total"]:
            denom.append((g, nrows, rep["total"]))
    return mism, denom


# ---------------------------------------------------------------- stats
def run_scores(run):
    if run.get("domain") == "imo_proof":
        values = [run["per_gen"][g].get("score") for g in run["gens"]]
        finite = [float(v) for v in values if v is not None]
        return np.asarray(finite, dtype=float)
    return np.array([r["score"] or 0 for r in run["rows"]], dtype=float)


def fmt_ci(ci):
    return f"{ci[0]:.3f} [{ci[1]:.3f}, {ci[2]:.3f}]"


# ---------------------------------------------------------------- outputs
def write_extract_csv(run, outdir):
    path = os.path.join(outdir, "scores_extract.csv")
    cols = ["run", "genid", "question_id", "truth", "score", "method", "raw_head"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(run["rows"])
    return path


def write_report(run, truth_path, outdir, cross=None):
    gens = run["gens"]
    score = run_scores(run)
    L = []  # markdown lines

    L.append(f"# 成绩分析：`{run['name']}`\n")
    cfg = (run["model_cfg"] or {})
    body = {k: v for k, v in cfg.items() if k not in ("type", "ts", "time")}
    if body:
        L.append(f"- 模型配置：`{json.dumps(body, ensure_ascii=False)}`")
    outer_done = [e for e in run["events"] if e.get("type") == "outer_done"]
    L.append(f"- 规模：{len(gens)} generations（outer_done ×{len(outer_done)}），"
             f"domain=`{run['domain']}`")
    L.append(f"- ground truth：`{os.path.relpath(truth_path, REPO) if truth_path else 'N/A'}`")
    if run.get("score_mode") == "docker_resolved":
        L.append("- 打分：broker docker 评测（score = 容器验证的 resolved）")
    elif run.get("score_mode") == "lenient":
        L.append("- 打分：verdict 正则提取（与在线一致，同一 parse_verdict）")
    else:
        L.append("- 打分：逐字硬匹配（与在线一致）")
    if run["brief_missing"]:
        L.append(f"- ⚠ `task_brief_missing` ×{run['brief_missing']}（task_brief 注入缺失）")
    if run["stagnation"]:
        L.append(f"- `stagnation_break`：{run['stagnation']}")
    L.append("")

    # per-gen table
    L.append("## 每 generation 一览\n")
    L.append("| gen | score | cov | evaluator预测 | 正确 | issues | patch ops | 无效子代 | harness_s | inner_s | 时长(s) | cost_tokens | plan_tok | eval_tok |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for g in gens:
        r = run["per_gen"][g]
        sk = sum((x["score"] or 0) for x in run["rows"] if x["genid"] == g)
        st = sum(1 for x in run["rows"] if x["genid"] == g)
        nokey = "同report_sha" if r["same_report_as_prev"] else ""
        L.append(f"| {g} | {r['score']} | {r['coverage']} | {r['predicted_score']} "
                 f"| {sk}/{st} | {r['n_issues']} {r['sev'] or ''} | {r['n_ops']} "
                 f"| {nokey} | {r['harness_s']} | {r['inner_s']} "
                 f"| {r['cost_wallclock_s'] if r['cost_wallclock_s'] is not None else '—'} "
                 f"| {r['cost_tokens']} | {r['plan_tokens']} | {r['eval_tokens']} |")
    L.append("")

    # per-session cost ledger (docs/10 §9 B / A7): runs that predate the
    # ledger (no logs/cost_sessions.jsonl) or with a garbled file yield no rows.
    sessions = run.get("sessions") or []
    if sessions:
        L.append("## 会话成本（cost_sessions.jsonl）\n")
        L.append("| role | seat | outer | genid | attempt | llm_calls | tool_calls | prompt_tok | complet_tok | reason_tok | total_tok | 时长(s) |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for s in sessions:
            L.append(f"| {s.get('role') or '—'} | {s.get('seat') or '—'} "
                     f"| {s.get('outer') if s.get('outer') is not None else '—'} "
                     f"| {s.get('genid') if s.get('genid') is not None else '—'} "
                     f"| {s.get('attempt') or '—'} | {s.get('llm_calls') or '—'} "
                     f"| {s.get('tool_calls') or '—'} | {s.get('prompt_tokens') or '—'} "
                     f"| {s.get('completion_tokens') or '—'} | {s.get('reasoning_tokens') or '—'} "
                     f"| {s.get('total_tokens') or '—'} | {s.get('wallclock_s') or '—'} |")
        L.append("")

    # overall (one score; the rule was declared in the header)
    L.append("## 总体\n")
    ns = len(score)
    k = int(score.sum())
    bt = stats.binomtest(k, ns, 0.5, alternative="greater") if ns else None
    p = f"{bt.pvalue:.4f}" if bt else "—"
    acc = f"{k / ns:.3f}" if ns else "—"
    L.append("| 口径 | 正确 | n | 准确率 | 二项检验 vs 0.5（greater） |")
    L.append("|---|---|---|---|---|")
    L.append(f"| score | {k}/{ns} | {ns} | {acc} | {p} |")
    # bootstrap CI on per-generation accuracies (reused compute_bootstrap_ci)
    gen_accs = np.array([np.mean([(x["score"] or 0) for x in run["rows"] if x["genid"] == g])
                         for g in gens], dtype=float)
    if len(gen_accs) >= 2:
        L.append(f"- 每 gen 准确率：{', '.join(f'{g}:{a:.2f}' for g, a in zip(gens, gen_accs))}；"
                 f"跨 gen 分布 median/CI = {fmt_ci(compute_bootstrap_ci(gen_accs))}")
    L.append("")

    # per-question
    L.append("## 逐题（系统性错误定位）\n")
    qids = sorted({r["question_id"] for r in run["rows"]})
    L.append("| question_id | truth | 各 gen 判定（方法） | 正确 | 判定 |")
    L.append("|---|---|---|---|---|")
    for q in qids:
        rs = [r for r in run["rows"] if r["question_id"] == q]
        seq = "/".join((r["method"] or "∅") for r in rs)
        k = sum((r["score"] or 0) for r in rs)
        verdict = "全对" if k == len(rs) else ("全错（系统性反相关）" if k == 0 else "混合")
        L.append(f"| {q} | {rs[0]['truth']} | {seq} | {k}/{len(rs)} | {verdict} |")
    L.append("")

    # evaluator calibration
    L.append("## evaluator 校准与 issue 走向\n")
    L.append("| gen | benchmark | evaluator预测 | 高severity issues |")
    L.append("|---|---|---|---|")
    for g in gens:
        r = run["per_gen"][g]
        L.append(f"| {g} | {r['score']} | {r['predicted_score']} | {r['sev'].get('high', 0)} |")
    L.append("")

    # evolution activity
    L.append("## 进化活动\n")
    noop = [g for g in gens if run["per_gen"][g]["same_report_as_prev"]]
    L.append(f"- 无效子代（report_sha 与前代相同）：{noop or '无'}")
    applied = [g for g in gens if run["per_gen"][g]["n_ops"]]
    L.append(f"- 有 design ops 的 gen：{applied or '无'}；"
             f"prompt_changed 的 gen：{[g for g in gens if run['per_gen'][g]['prompt_changed']] or '无'}")
    L.append(f"- self_receipt（双角色 self-improve 收据）事件：{run['self_receipts']}")
    calls = run["llm_calls"]
    if calls:
        eff = Counter(c.get("reasoning_effort") for c in calls)
        tok = (sum(c.get("prompt_tokens") or 0 for c in calls),
               sum(c.get("completion_tokens") or 0 for c in calls))
        lat = [c.get("latency_ms") or 0 for c in calls]
        L.append(f"- llm_call ×{len(calls)}，effort 分布 {dict(eff)}，tokens P/C {tok}，"
                 f"latency 中位 {int(np.median(lat))}ms")
    L.append("")

    if cross:
        L.append("## 跨运行对比\n")
        L.append("| run | n | 正确 | acc |")
        L.append("|---|---|---|---|")
        for other, s2 in cross:
            L.append(f"| {other['name']} | {len(s2)} | {int(s2.sum())} | {s2.mean():.3f} |")
        L.append("")

    # auto conclusion
    L.append("## 结论（自动摘要）\n")
    docker_mode = run.get("score_mode") == "docker_resolved"
    qs = {}
    for r in run["rows"]:
        qs.setdefault(r["question_id"], []).append(r["score"] or 0)
    all_wrong = sorted(q for q, v in qs.items() if sum(v) == 0)
    all_right = sorted(q for q, v in qs.items() if sum(v) == len(v))
    k, n = int(score.sum()), len(score)
    bt = stats.binomtest(k, n, 0.5, alternative="greater") if n else None
    pct = f"{k / n:.1%}" if n else "n/a"
    if docker_mode:
        L.append(f"- score = 容器 resolved：{k}/{n}（{pct}）—— broker 评测结论，无文本重判。")
    else:
        L.append(f"- score {k}/{n}（{pct}）—— 口径见报告头部（与在线同规则重算）。")
    if all_wrong:
        L.append(f"- 系统性错误（全部 gen 判错）：question {all_wrong} —— 单题集中，非均匀噪声。")
    if all_right:
        L.append(f"- 全对题：{all_right}。")
    if bt is not None:
        L.append(f"- vs 随机(0.5) 二项检验（greater）：p={bt.pvalue:.4f}。")
    else:
        L.append("- 无有效样本，跳过二项检验。")
    noop = [g for g in gens if run["per_gen"][g]["same_report_as_prev"]]
    if noop:
        L.append(f"- 无效子代：gen {noop}（与前代同 report_sha，patch 未产生有效变化）。")
    pcorr = [run["per_gen"][g]["predicted_score"] for g in gens]
    if pcorr and all(v is not None for v in pcorr) and len(set(pcorr)) > 1:
        rho = stats.spearmanr(pcorr, gen_accs).statistic if len(gen_accs) >= 3 else float("nan")
        L.append(f"- evaluator 预测分 {pcorr} 与每 gen acc 的 Spearman ρ={rho:.2f}。")
    L.append(f"- 产物：scores_extract.csv / significance.txt / progress.png（本目录）。")
    L.append("")

    path = os.path.join(outdir, "REPORT.md")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    return path


def write_proof_report(run, truth_path, outdir):
    """Write a proof-domain report without verdict re-parsing."""
    lines = [f"# 成绩分析：`{run['name']}`\n",
             f"- domain=`{run['domain']}`；评分字段=`points_percentage`",
             "- proof 文本不具备 winner/outcome 标签；曲线直接使用框架分数和 coverage。",
             "",
             "| gen | score | coverage | score_status | harness_s | inner_s |",
             "|---|---|---|---|---|---|"]
    for g in run["gens"]:
        rec = run["per_gen"][g]
        lines.append(
            f"| {g} | {rec['score']} | {rec['coverage']} | {rec['score_status']} "
            f"| {rec['harness_s']} | {rec['inner_s']} |"
        )
    if truth_path:
        lines.append(f"\n- 题目目录：`{os.path.relpath(truth_path, REPO)}`")
    lines.append("\n- 产物：scores_extract.csv / significance.txt / progress.png（本目录）。")
    path = os.path.join(outdir, "REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_plot(run, outdir):
    gens = run["gens"]
    x = np.arange(1, len(gens) + 1)
    score_g = [np.mean([(r["score"] or 0) for r in run["rows"] if r["genid"] == g])
               if any(r["genid"] == g for r in run["rows"]) else np.nan
               for g in gens]
    pred_g = [run["per_gen"][g]["predicted_score"] if run["per_gen"][g]["predicted_score"] is not None else np.nan
              for g in gens]
    vals = [0.0 if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
            for v in score_g]
    best = np.maximum.accumulate([0.0] + vals)[1:]

    qids = sorted({r["question_id"] for r in run["rows"]})
    q_ok = [sum((r["score"] or 0) for r in run["rows"] if r["question_id"] == q) for q in qids]
    q_truth = [next(r["truth"] for r in run["rows"] if r["question_id"] == q) for q in qids]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    ax1.plot(x, score_g, "o-", ms=5, label="score", zorder=3)
    ax1.plot(x, pred_g, "^--", label="evaluator predicted")
    ax1.plot(x, best, ":", color="gray", label="best-so-far (score)")
    ax1.axhline(0.5, color="r", lw=0.8, alpha=0.6, label="random guess")
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xticks(x)
    ax1.set_xlabel("generation")
    ax1.set_title(f"{run['name']} progress")
    ax1.legend(fontsize=7)
    ax1.grid(alpha=0.3)

    ax2.bar(range(len(qids)), q_ok, color="#4C72B0")
    ax2.set_xticks(range(len(qids)))
    ax2.set_xticklabels([f"{q}\ntruth={t.split('_')[-1]}" for q, t in zip(qids, q_truth)], fontsize=8)
    ax2.set_ylim(0, len(gens))
    ax2.set_ylabel("correct (of all gens)")
    ax2.set_title("per-question correctness")
    ax2.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    path = os.path.join(outdir, "progress.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def write_proof_plot(run, outdir):
    """Plot proof score and coverage; never interpret proof prose as verdicts."""
    x = np.arange(1, len(run["gens"]) + 1)
    scores = [run["per_gen"][g]["score"] for g in run["gens"]]
    coverage = [run["per_gen"][g]["coverage"] for g in run["gens"]]
    scores = [float(v) if v is not None else np.nan for v in scores]
    coverage = [float(v) if v is not None else np.nan for v in coverage]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    ax1.plot(x, scores, "o-", label="points score")
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xlabel("generation")
    ax1.set_title(f"{run['name']} proof score")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8)
    ax2.plot(x, coverage, "s-", color="#4C72B0", label="coverage")
    ax2.set_ylim(-0.05, 1.05)
    ax2.set_xlabel("generation")
    ax2.set_title("graded proof coverage")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=8)
    fig.tight_layout()
    path = os.path.join(outdir, "progress.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--skip-plots", action="store_true")
    args = ap.parse_args()

    runs = [extract_run(d) for d in args.run_dirs]
    for run in runs:
        qids = {r["question_id"] for r in run["rows"]}
        tp, truth = detect_dataset(run["dir"], run["domain"] or "search_arena", qids)
        if run["domain"] == "imo_proof":
            # proofbench is a question catalogue, not a winner/outcome truth
            # table.  Proof correctness is already summarized by report.json.
            run["truth_path"] = tp
            run["score_mode"] = "proof_points"
            continue
        if truth is None and run["domain"] == "polyglot":
            # No dataset CSV exists for polyglot by design; the broker docker
            # eval is the authority. Skip the crosscheck — both sides
            # would derive from the same resolved list.
            n = attach_polyglot_truth(run)
            run["truth_path"] = None
            run["score_mode"] = "docker_resolved"
            print(f"[NOTE] {run['name']}: polyglot — ground truth from broker "
                  f"report.json ({n} resolved)")
            continue
        if truth is None:
            print(f"[ERROR] {run['name']}: no dataset file covers question ids {sorted(qids)}")
            return 2
        attach_truth(run, truth)
        run["truth_path"] = tp
        mism, denom = report_crosscheck(run)
        if mism:
            print(f"[WARN] {run['name']} score cross-check mismatch "
                  f"(offline != online, investigate): {mism}")
        if denom:
            print(f"[NOTE] {run['name']} report total excludes invalid samples: {denom}")

    # significance file (reused util): only runs with n>=2 give meaningful tests;
    # n=1 smoke runs would just add nan/-inf noise to every pairwise cell.
    methods = {}
    meta = {}
    for run in runs:
        s = run_scores(run)
        if len(s) >= 2:
            methods[f"{run['name']}:score"] = s
            meta.setdefault("n_samples", []).append(f"{run['name']}={len(s)}")
    sig_path = os.path.join(runs[0]["dir"], "score_analysis")
    os.makedirs(sig_path, exist_ok=True)
    save_significance_tests({k: v for k, v in methods.items()},
                            os.path.join(sig_path, "significance.txt"),
                            metadata={"domain": runs[0]["domain"],
                                      "samples": "; ".join(meta["n_samples"])})

    cross = None
    if len(runs) > 1:
        base = runs[0]
        base_q = {r["question_id"] for r in base["rows"]}
        cross = []
        for other in runs[1:]:
            oq = {r["question_id"] for r in other["rows"]}
            shared = base_q & oq
            s2 = np.array([r["score"] for r in other["rows"] if r["question_id"] in shared], dtype=float)
            cross.append((other, s2))

    for run in runs:
        outdir = os.path.join(run["dir"], "score_analysis")
        os.makedirs(outdir, exist_ok=True)
        csv_path = write_extract_csv(run, outdir)
        if run.get("score_mode") == "proof_points":
            rep_path = write_proof_report(run, run["truth_path"], outdir)
            plot_path = None if args.skip_plots else write_proof_plot(run, outdir)
        else:
            rep_path = write_report(run, run["truth_path"], outdir, cross=cross if run is runs[0] else None)
            plot_path = None if args.skip_plots else write_plot(run, outdir)
        s = run_scores(run)
        if run.get("score_mode") == "proof_points":
            print(f"[OK] {run['name']}: proof score mean "
                  f"{(float(s.mean()) if len(s) else float('nan')):.3f} ({len(s)} scored generations)")
        else:
            print(f"[OK] {run['name']}: score {int(s.sum())}/{len(s)} "
                  f"({s.mean():.1%})" if len(s) else f"[OK] {run['name']}: score —/0")
        for p in (csv_path, rep_path, plot_path):
            if p:
                print(f"     {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
