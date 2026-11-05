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
            parent_genid=(inner_done.get(g) or {}).get("parent_genid"),
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
        # EPISODE domains (balrog): no predictions.csv and no dataset CSV exist.
        # The harness report itself carries per-EPISODE truth (question_points),
        # so a row IS an episode and its score is its PROGRESSION in [0, 1] --
        # exactly the quantity the online scorer averages (score_key
        # average_progress / 100). Binary accuracy is meaningless here, which is
        # why the episode report has its own writer.
        qpoints = rep.get("question_points") or {}
        if not preds and qpoints:
            ep_meta = {e.get("id"): e for e in (rep.get("episodes") or [])}
            for eid, pts in sorted(qpoints.items()):
                m = ep_meta.get(str(eid)) or {}
                rows.append(dict(
                    run=name, genid=g, question_id=str(eid),
                    raw_head=f"episode {eid}", score=None,
                    method="episode_progression",
                    progression=float(pts or 0.0),
                    anchor=bool(m.get("anchor")),
                    task=m.get("task"), seed=m.get("seed"),
                    steps=m.get("steps"),
                    aborted=bool(m.get("aborted")), error=m.get("error"),
                ))
            continue
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
                is_episode=any(r.get("method") == "episode_progression" for r in rows),
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


def attach_episode_truth(run):
    """Balrog: the EPISODE is the question, PROGRESSION is the score.

    There is no winner/outcome dataset and no text prediction to re-parse, so
    the run's own report.json is the truth: every episode contributes its
    progression in [0, 1] (the online scorer averages exactly this for
    ``score_key: average_progress``). An ABORTED episode (infrastructure killed
    it) is scored 0.0 like a lost episode -- the framework's consensus is that a
    crashed episode is a played-and-lost episode -- but it carries ``aborted``
    so a run-level drop is not misread as a regression in play quality.
    Returns (n_episode_rows, n_aborted).
    """
    n = n_ab = 0
    for r in run["rows"]:
        prog = float(r.get("progression") or 0.0)
        r["truth"] = "progression_target=1.0"
        r["score"] = max(0.0, min(1.0, prog))
        r["passed"] = bool(r["score"] >= 1.0 - 1e-9)
        if r.get("aborted"):
            r["method"] = "aborted"
            n_ab += 1
        else:
            r["method"] = "episode_progression"
        n += 1
    return n, n_ab


def episode_crosscheck(run):
    """offline mean progression per generation vs the ONLINE score.

    The balrog report has no ``total_correct``/``total`` (the score is a mean),
    so the offline==online probe compares the recomputed MEAN with the number
    the loop actually recorded in ``scores.jsonl``. A mismatch means the report
    rows and the online aggregation disagree -- drift, not convention.
    """
    mism = []
    for g in run["gens"]:
        vals = [float(r["score"] or 0.0) for r in run["rows"] if r["genid"] == g]
        if not vals:
            continue
        mine = float(np.mean(vals))
        theirs = (run["per_gen"].get(g) or {}).get("score")
        if theirs is None:
            continue
        try:
            theirs_f = float(theirs)
        except (TypeError, ValueError):
            continue
        if abs(mine - theirs_f) > 1e-3:
            mism.append((g, round(mine, 6), theirs_f))
    return mism


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
    cols = ["run", "genid", "question_id", "truth", "score", "method",
            "task", "seed", "anchor", "aborted", "raw_head"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(run["rows"])
    return path


def write_episode_report(run, outdir):
    """Episode-domain report (balrog): progression means, not binary accuracy.

    Binary "correct/wrong" is the wrong lens here -- an episode is a graded
    rollout (progression in [0,1]) and the online score is its MEAN. The
    per-episode table is what localizes a systematic failure (one task family
    stuck at 0.0 across every generation), and the aborted list keeps an
    infrastructure outage distinguishable from bad play.
    """
    gens = run["gens"]
    L = [f"# 成绩分析：`{run['name']}`\n"]
    cfg = (run["model_cfg"] or {})
    body = {k: v for k, v in cfg.items() if k not in ("type", "ts", "time")}
    if body:
        L.append(f"- 模型配置：`{json.dumps(body, ensure_ascii=False)}`")
    outer_done = [e for e in run["events"] if e.get("type") == "outer_done"]
    L.append(f"- 规模：{len(gens)} generations（outer_done ×{len(outer_done)}），"
             f"domain=`{run['domain']}`")
    L.append("- 口径：episode 域。一个 question = 一个 EPISODE，分数 = 该 episode 的 "
             "`progression` ∈ [0,1]；在线分数 = 每代所有 episode 的 MEAN"
             "（`score_key: average_progress` / 100）。没有 dataset 标签，"
             "truth 不是二元对错。")
    if run["brief_missing"]:
        L.append(f"- ⚠ `task_brief_missing` ×{run['brief_missing']}")
    if run["stagnation"]:
        L.append(f"- `stagnation_break`：{run['stagnation']}")
    L.append("")

    L.append("## 每 generation 一览\n")
    L.append("| gen | 在线 score | 离线 mean progression | episodes | anchors | aborted | "
             "harness_s | inner_s | cost_tokens | plan_tok | eval_tok |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for g in gens:
        r = run["per_gen"][g]
        rs = [x for x in run["rows"] if x["genid"] == g]
        mean = float(np.mean([float(x["score"] or 0.0) for x in rs])) if rs else None
        n_anchor = sum(1 for x in rs if x.get("anchor"))
        n_ab = sum(1 for x in rs if x.get("aborted"))
        online = "—" if r["score"] is None else str(r["score"])
        L.append(f"| {g} | {online} | "
                 f"{'—' if mean is None else f'{mean:.4f}'} | {len(rs)} | {n_anchor} | {n_ab} "
                 f"| {r['harness_s']} | {r['inner_s']} | {r['cost_tokens']} "
                 f"| {r['plan_tokens']} | {r['eval_tokens']} |")
    L.append("")

    L.append("## 逐 episode（系统性失败定位）\n")
    L.append("| episode_id | task | seed | anchor | 各 gen progression | 均值 | 备注 |")
    L.append("|---|---|---|---|---|---|---|")
    for eid in sorted({r["question_id"] for r in run["rows"]}):
        rs = [r for r in run["rows"] if r["question_id"] == eid]
        seq = "/".join(f"{float(r['score'] or 0.0):.2f}" for r in rs)
        mean = float(np.mean([float(r["score"] or 0.0) for r in rs]))
        note = []
        if rs[0].get("anchor"):
            note.append("anchor")
        if any(r.get("aborted") for r in rs):
            note.append("ABORTED")
        steps = [r.get("steps") for r in rs if r.get("steps")]
        if steps:
            note.append(f"steps≤{max(steps)}")
        L.append(f"| {eid} | {rs[0].get('task')} | {rs[0].get('seed')} "
                 f"| {'Y' if rs[0].get('anchor') else ''} | {seq} | {mean:.3f} "
                 f"| {' '.join(note)} |")
    L.append("")

    aborted = [r for r in run["rows"] if r.get("aborted")]
    if aborted:
        L.append("## 中断的 episode（基础设施，非对局质量）\n")
        for r in aborted:
            L.append(f"- `{r['question_id']}` gen {r['genid']}: {r.get('error')}")
        L.append("")

    # per-task aggregation (the balrog analogue of a per-topic breakdown)
    tasks = {}
    for r in run["rows"]:
        tasks.setdefault(str(r.get("task")), []).append(float(r["score"] or 0.0))
    if tasks:
        L.append("## 逐 task 均值\n")
        L.append("| task | episodes | mean progression | 全 0 的比例 |")
        L.append("|---|---|---|---|")
        for t in sorted(tasks):
            v = tasks[t]
            zeros = sum(1 for x in v if x <= 1e-9)
            L.append(f"| {t} | {len(v)} | {float(np.mean(v)):.3f} | {zeros}/{len(v)} |")
        L.append("")

    L.append("## 结论（自动摘要）\n")
    score = run_scores(run)
    if len(score):
        L.append(f"- episode 级 mean progression = {float(np.mean(score)):.4f}"
                 f"（n={len(score)} 个 episode × gen）")
    gen_means = np.array([
        float(np.mean([float(x["score"] or 0.0) for x in run["rows"] if x["genid"] == g]))
        for g in gens if any(x["genid"] == g for x in run["rows"])
    ], dtype=float)
    if len(gen_means) >= 2:
        L.append(f"- 每代 mean 的 median/CI = {fmt_ci(compute_bootstrap_ci(gen_means))}")
    flat = [t for t, v in tasks.items() if v and all(x <= 1e-9 for x in v)]
    if flat:
        L.append(f"- 全 0 的 task（结构性失败，值得优先看）：{sorted(flat)}")
    if aborted:
        L.append(f"- ⚠ {len(aborted)} 个 episode 因基础设施中断记 0 分，"
                 "这些不是对局质量的证据。")
    noop = [g for g in gens if run["per_gen"][g]["same_report_as_prev"]]
    if noop:
        L.append(f"- 无效子代（与前代同 report_sha）：gen {noop}。")
    L.append("- 产物：scores_extract.csv / significance.txt / progress.png（本目录）。")
    L.append("")

    path = os.path.join(outdir, "REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


def write_episode_plot(run, outdir):
    """Per-generation mean progression + per-episode progression heat strip."""
    gens = run["gens"]
    x = np.arange(1, len(gens) + 1)
    means = [float(np.mean([float(r["score"] or 0.0) for r in run["rows"]
                            if r["genid"] == g]))
             if any(r["genid"] == g for r in run["rows"]) else np.nan for g in gens]
    pred = [run["per_gen"][g]["predicted_score"]
            if run["per_gen"][g].get("predicted_score") is not None else np.nan
            for g in gens]
    vals = [0.0 if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
            for v in means]
    best = np.maximum.accumulate([0.0] + vals)[1:]

    eids = sorted({r["question_id"] for r in run["rows"]})
    grid = np.full((len(eids), len(gens)), np.nan)
    for i, eid in enumerate(eids):
        for j, g in enumerate(gens):
            hit = [float(r["score"] or 0.0) for r in run["rows"]
                   if r["question_id"] == eid and r["genid"] == g]
            if hit:
                grid[i, j] = hit[0]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4),
                                   gridspec_kw={"width_ratios": [1, 1.1]})
    ax1.plot(x, means, "o-", ms=5, label="mean progression", zorder=3)
    ax1.plot(x, pred, "^--", label="evaluator predicted")
    ax1.plot(x, best, ":", color="gray", label="best-so-far")
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xticks(x)
    ax1.set_xticklabels([str(g) for g in gens], fontsize=8)
    ax1.set_xlabel("generation")
    ax1.set_title(f"{run['name']} episode progression")
    ax1.legend(fontsize=7)
    ax1.grid(alpha=0.3)

    im = ax2.imshow(grid, aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
    ax2.set_yticks(range(len(eids)))
    ax2.set_yticklabels(eids, fontsize=6)
    ax2.set_xticks(range(len(gens)))
    ax2.set_xticklabels([str(g) for g in gens], fontsize=8)
    ax2.set_xlabel("generation")
    ax2.set_title("per-episode progression")
    fig.colorbar(im, ax=ax2, fraction=0.046, pad=0.02)
    fig.tight_layout()
    path = os.path.join(outdir, "progress.png")
    fig.savefig(path, dpi=140)
    plt.close(fig)
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
    """Plot proof score and coverage using the recorded generation DAG.

    A sequential line would imply that generation ``n`` is always the parent
    of ``n+1``. Outer selection can branch, so only recorded parent edges are
    connected; node markers carry the measurement status.
    """
    def gen_order(value):
        try:
            return (0, int(value))
        except (TypeError, ValueError):
            return (1, str(value))

    gens = sorted({str(g) for g in run["gens"]}, key=gen_order)
    x = np.asarray([int(g) if str(g).isdigit() else i for i, g in enumerate(gens)],
                   dtype=float)
    recs = run["per_gen"]
    scores = {g: (float(recs[g]["score"]) if recs[g].get("score") is not None
                  else np.nan) for g in gens}
    coverage = {g: (float(recs[g]["coverage"]) if recs[g].get("coverage") is not None
                    else np.nan) for g in gens}
    status_colors = {
        "ok": "#2E8B57",
        "partial": "#D98C00",
        "imputed": "#7A7A7A",
        "failed": "#C23B22",
        "invalid": "#8B1E3F",
    }
    statuses = {g: str(recs[g].get("score_status") or "unknown") for g in gens}
    xpos = dict(zip(gens, x))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))

    # Connect only actual parent -> child edges, never adjacent genids.
    for child in gens:
        parent = recs[child].get("parent_genid")
        if parent is None:
            continue
        parent = str(parent)
        if (parent not in recs or parent not in scores
                or not np.isfinite(scores[child])
                or not np.isfinite(scores.get(parent, np.nan))):
            continue
        ax1.plot([xpos[parent], xpos[child]], [scores[parent], scores[child]],
                 color="#B0B0B0", lw=0.9, alpha=0.8, zorder=1)
        if (np.isfinite(coverage[child]) and np.isfinite(coverage.get(parent, np.nan))):
            ax2.plot([xpos[parent], xpos[child]],
                     [coverage[parent], coverage[child]],
                     color="#D0D0D0", lw=0.8, alpha=0.8, zorder=1)

    for status in sorted(set(statuses.values())):
        members = [g for g in gens if statuses[g] == status]
        color = status_colors.get(status, "#4C72B0")
        non_parents = [g for g in members if recs[g].get("valid_parent") is False]
        ax1.scatter([xpos[g] for g in members], [scores[g] for g in members],
                    color=color,
                    marker="x" if status in {"failed", "invalid"} else "o",
                    label=f"{status} score", zorder=3)
        if non_parents and status not in {"failed", "invalid"}:
            ax1.scatter([xpos[g] for g in non_parents], [scores[g] for g in non_parents],
                        color="#6A3D9A", marker="x", label=f"{status} (not parent)",
                        zorder=4)
        ax2.scatter([xpos[g] for g in members], [coverage[g] for g in members],
                    color=color, marker="x" if status in {"failed", "invalid"} else "s",
                    label=f"{status} coverage", zorder=3)
        if non_parents and status not in {"failed", "invalid"}:
            ax2.scatter([xpos[g] for g in non_parents], [coverage[g] for g in non_parents],
                        color="#6A3D9A", marker="x", label=f"{status} (not parent)",
                        zorder=4)
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xlabel("generation id")
    ax1.set_title(f"{run['name']} proof score")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8)
    ax2.set_ylim(-0.05, 1.05)
    ax2.set_xlabel("generation id")
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
        if run.get("is_episode"):
            # balrog & friends: no dataset CSV, no predictions.csv. Ground truth
            # is the report's own per-episode progression (see
            # attach_episode_truth); the crosscheck replaces "total_correct"
            # with the recorded mean score, because the online value IS a mean.
            n_rows, n_ab = attach_episode_truth(run)
            run["truth_path"] = None
            run["score_mode"] = "episode_progression"
            mism = episode_crosscheck(run)
            if mism:
                print(f"[WARN] {run['name']} episode score cross-check mismatch "
                      f"(offline != online, investigate): {mism}")
            print(f"[NOTE] {run['name']}: episode domain — {n_rows} episode rows "
                  f"from report.json, {n_ab} aborted (scored 0.0, flagged)")
            continue
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
        elif run.get("score_mode") == "episode_progression":
            rep_path = write_episode_report(run, outdir)
            plot_path = None if args.skip_plots else write_episode_plot(run, outdir)
        else:
            rep_path = write_report(run, run["truth_path"], outdir, cross=cross if run is runs[0] else None)
            plot_path = None if args.skip_plots else write_plot(run, outdir)
        s = run_scores(run)
        if run.get("score_mode") == "proof_points":
            print(f"[OK] {run['name']}: proof score mean "
                  f"{(float(s.mean()) if len(s) else float('nan')):.3f} ({len(s)} scored generations)")
        elif run.get("score_mode") == "episode_progression":
            print(f"[OK] {run['name']}: episode mean progression "
                  f"{(float(s.mean()) if len(s) else float('nan')):.4f} ({len(s)} episode rows)")
        else:
            print(f"[OK] {run['name']}: score {int(s.sum())}/{len(s)} "
                  f"({s.mean():.1%})" if len(s) else f"[OK] {run['name']}: score —/0")
        for p in (csv_path, rep_path, plot_path):
            if p:
                print(f"     {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
