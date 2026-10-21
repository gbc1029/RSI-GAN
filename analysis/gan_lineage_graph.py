#!/usr/bin/env python3
"""GAN run genealogy: inner (task-agent per-generation) + outer lineage graphs.

Reuses the DGM-H renderer `analysis/visualize_archive.py:visualize_graph`
UNCHANGED (colormap, valid_parent red/green borders, best-node diamond,
colorbar, graphviz dot layout). Adaptation is on the DATA side only:

  - nodes/edges replayed from `logs/events.jsonl` (code_lineage / code_branch
    / inner_done / stagnation_break) — the framework's own lineage record;
  - node id encodes generation + 代际 + marks: ``g4·o3i1·nopatch`` renders as
    label ``#g4·o3i1·nopatch`` (the renderer prepends '#');
  - score colors come from scores.jsonl (strict) or the lenient re-judgement
    of analysis/gan_scores_extract.py;
  - valid_parent borders work through a metadata shim dir (gen_<node>/metadata.json),
    which is exactly what the renderer's get_node_metadata_key reads;
  - outer graph: initial -> outer_1 -> ... nodes carry base commit (or ·same);
  - offline git audit (safe.directory override, read-only) cross-checks
    claimed parentage vs actual commit parents.

Outputs into <run>/score_analysis/:
  lineage_inner_{lenient,strict}.png   task-agent per-generation genealogy
  lineage_outer_{lenient,strict}.png   outer-boundary genealogy
  lineage_audit.md                     per-gen + per-outer integrity audit
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analysis.visualize_archive import visualize_graph  # renderer, unchanged

import gan_scores_extract as gse  # lenient re-judgement reuse


# ---------------------------------------------------------------- data replay
def load_jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def replay(run_dir):
    ev = load_jsonl(os.path.join(run_dir, "logs", "events.jsonl"))
    d = dict(outer_base={}, gen_branch={}, lineage={}, gen2oi={}, stag=[],
             outer_done={}, prev_bench={})
    for e in ev:
        t = e.get("type")
        if t == "code_branch" and e.get("kind") == "outer_base":
            d["outer_base"][e["outer"]] = dict(ref=e.get("ref"), commit=e.get("commit"))
        elif t == "code_branch":
            d["gen_branch"][str(e.get("genid"))] = dict(
                ref=e.get("ref"), commit=e.get("commit"), base=e.get("base"),
                applied=e.get("applied"), ref_ok=e.get("ref_ok"))
        elif t == "code_lineage":
            d["lineage"][str(e.get("genid"))] = dict(
                parent=e.get("parent_genid"), base=e.get("base"), checked=e.get("checked"))
        elif t == "inner_done":
            d["gen2oi"][str(e.get("genid"))] = (e.get("outer"), e.get("inner"))
        elif t == "stagnation_break":
            d["stag"].append((e.get("outer"), e.get("inner")))
        elif t == "outer_done":
            d["outer_done"][e.get("outer")] = e.get("duration_s")
    # outer boundary snapshots: prev_benchmark + boundary commit
    ckpt = os.path.join(run_dir, "ckpt")
    if os.path.isdir(ckpt):
        for f in sorted(os.listdir(ckpt)):
            if f.startswith("outer_") and f.endswith(".json"):
                try:
                    snap = json.load(open(os.path.join(ckpt, f)))
                except Exception:
                    continue
                o = snap.get("state", {}).get("outer")
                st = snap.get("state", {})
                d["prev_bench"][o] = dict(
                    benchmark=st.get("prev_benchmark"), predicted=st.get("prev_predicted"),
                    commit=(snap.get("code") or {}).get("commit"))
    return d


def git(code_root, *args):
    cmd = ["git", "-c", f"safe.directory={code_root}"] + list(args)
    try:
        r = subprocess.run(cmd, cwd=code_root, capture_output=True, text=True, timeout=30)
        return (r.stdout.strip(), r.returncode == 0)
    except Exception as exc:
        return (str(exc), False)


def git_audit(run_dir, replay_d):
    """Offline integrity checks: ref existence, claimed vs actual commit parents."""
    code_root = os.path.join(run_dir, "ckpt", "code")
    if not os.path.isdir(os.path.join(code_root, ".git")):
        return None
    audit = dict(refs={}, parents={})

    def ref_commit(ref):
        out, ok = git(code_root, "rev-parse", ref)
        return out if ok else None

    wanted = [b["ref"] for b in replay_d["gen_branch"].values() if b.get("ref")]
    wanted += [b["ref"] for b in replay_d["outer_base"].values() if b.get("ref")]
    for ref in wanted:
        audit["refs"][ref] = ref_commit(ref)

    def actual_parents(ref):
        out, ok = git(code_root, "log", "-1", "--format=%P", ref)
        return out.split() if ok and out else []

    for ref in wanted:
        audit["parents"][ref] = actual_parents(ref)
    audit["code_root"] = code_root
    return audit


# ---------------------------------------------------------------- metrics
def per_gen_metrics(run_dir):
    """strict from scores.jsonl; lenient via gan_scores_extract reuse."""
    scores = load_jsonl(os.path.join(run_dir, "scores", "scores.jsonl"))
    strict = {str(s["genid"]): s.get("score") for s in scores}
    valid_parent = {str(s["genid"]): bool(s.get("valid_parent", True)) for s in scores}
    lenient = {}
    try:
        rd = gse.extract_run(run_dir)
        qids = {r["question_id"] for r in rd["rows"]}
        _, truth = gse.detect_dataset(run_dir, rd["domain"] or "search_arena", qids)
        if truth:
            gse.attach_truth(rd, truth)
            acc = defaultdict(list)
            for r in rd["rows"]:
                acc[r["genid"]].append(r["lenient_correct"])
            lenient = {g: sum(v) / len(v) for g, v in acc.items()}
    except Exception as exc:
        print(f"[WARN] lenient metrics unavailable: {exc}")
    return strict, lenient, valid_parent


# ---------------------------------------------------------------- graphs
def node_id(genid, oi, marks):
    o, i = oi
    base = f"g{genid}·o{o}i{i}"
    return base + ("".join(f"·{m}" for m in marks) if marks else "")


def build_inner(replay_d, strict, lenient, valid_parent):
    """Return (G_edges, nodes_meta) — node id -> dict(parent_id, score_strict/lenient, valid)."""
    parent_node = {"initial": "initial"}
    nodes, edges = {}, []
    for genid in sorted(replay_d["lineage"], key=lambda x: int(x) if x.isdigit() else 0):
        lin = replay_d["lineage"][genid]
        br = replay_d["gen_branch"].get(genid, {})
        oi = replay_d["gen2oi"].get(genid, ("?", "?"))
        marks = []
        if br.get("applied") is False:
            marks.append("nopatch")
        if (oi in replay_d["stag"]):
            marks.append("stag")
        nid = node_id(genid, oi, marks)
        parent = str(lin.get("parent", "initial"))
        parent_id = parent_node.get(parent, parent)
        parent_node[genid] = nid
        nodes[nid] = dict(
            genid=genid, parent_genid=parent, parent_id=parent_id,
            commit=br.get("commit"), checked=lin.get("checked"), ref=br.get("ref"),
            applied=br.get("applied"), ref_ok=br.get("ref_ok"), oi=oi,
            strict=strict.get(genid), lenient=lenient.get(genid),
            valid=valid_parent.get(genid, True))
    # edges after all nodes known (gen5 may parent to a later-resolved id — none here)
    for nid, meta in nodes.items():
        edges.append((meta["parent_id"], nid))
    return nodes, edges


def build_outer(replay_d, strict, lenient):
    """outer chain: initial -> outer_1 -> ... ; metric = best of its inner gens."""
    outs = sorted({int(o) for o in list(replay_d["outer_base"]) + list(replay_d["outer_done"]) if o is not None})
    inner_by_outer = defaultdict(list)
    for genid, (o, _i) in replay_d["gen2oi"].items():
        inner_by_outer[o].append(genid)
    nodes, edges = {}, []
    prev_commit, prev_id = None, "initial"
    for o in outs:
        ob = replay_d["outer_base"].get(o) or {}
        commit = ob.get("commit") or (replay_d["prev_bench"].get(o) or {}).get("commit")
        gens = sorted(inner_by_outer.get(o, []), key=int)
        ls = [lenient[g] for g in gens if lenient.get(g) is not None]
        ss = [strict[g] for g in gens if strict.get(g) is not None]
        same = (prev_commit is not None and commit == prev_commit)
        nid = f"outer{o}·{'same' if same else ('base=' + str(commit or '?')[:8])}"
        if (o in replay_d["stag"]):
            nid += "·stag"
        nodes[nid] = dict(outer=o, commit=commit, same_as_prev=same, gens=gens,
                          lenient=max(ls) if ls else None, strict=max(ss) if ss else None,
                          valid=True, ref=ob.get("ref"))
        edges.append((prev_id, nid))
        prev_commit, prev_id = commit, nid
    return nodes, edges


# ---------------------------------------------------------------- render
def shim_meta(outdir, nodes):
    """Write gen_<node>/metadata.json so the renderer's border logic works as-is."""
    for nid, meta in nodes.items():
        p = os.path.join(outdir, f"gen_{nid}")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "metadata.json"), "w") as f:
            json.dump({"valid_parent": bool(meta.get("valid", True)),
                       "parent_genid": meta.get("parent_genid") or meta.get("parent_id")}, f)


def render(nodes, edges, score_of, outdir, tag, run_name):
    import networkx as nx
    G = nx.DiGraph()
    G.add_edges_from(edges)
    for n in G.nodes():
        G.add_node(n)  # isolated roots too
    score_map = {n: score_of(meta) for n, meta in nodes.items() if n in G.nodes}
    shim_meta(outdir, nodes)
    suffix = f"{run_name}_{tag}"
    visualize_graph(G, score_map, outdir, name_suffix=suffix, plot_borders=True)
    src = os.path.join(outdir, f"archive_graph_{suffix}_train_agent.png")
    dst = os.path.join(os.path.dirname(outdir), f"lineage_{tag}.png")
    shutil.move(src, dst)
    return dst


# ---------------------------------------------------------------- audit md
def short(c):
    return str(c)[:8] if c else "—"


def write_audit(run_dir, replay_d, inner_nodes, outer_nodes, ga, path):
    L = [f"# 系谱审计：`{os.path.basename(os.path.normpath(run_dir))}`\n"]

    L.append("## inner（task agent 每代）\n")
    L.append("| gen | 代际 | 声称父 | base | ref | commit | git实际父 | checked==commit | applied | strict | lenient | valid | 审计 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for nid, m in sorted(inner_nodes.items(), key=lambda kv: int(kv[1]["genid"])):
        ref = m.get("ref")
        actual = ",".join(short(p) for p in ga["parents"].get(ref, [])) if ga else "n/a"
        checked_ok = (m.get("checked") == m.get("commit"))
        aliased = (m.get("applied") is False)
        verdict = []
        if aliased:
            verdict.append("ALIAS(与父同commit,git无新父子)")
        elif ga and ref and str(m["parent_genid"]).isdigit():
            parent_ref = replay_d["gen_branch"].get(str(m["parent_genid"]), {}).get("ref")
            claimed = ga["refs"].get(parent_ref) if parent_ref else None
            actual_first = (ga["parents"].get(ref) or [None])[0]
            verdict.append("OK" if claimed and actual_first == claimed else "MISMATCH")
        if checked_ok:
            verdict.append("checked_ok")
        len_s = f"{m['lenient']:.3f}" if m.get("lenient") is not None else "—"
        L.append(f"| {m['genid']} | o{m['oi'][0]}i{m['oi'][1]} | {m['parent_genid']} "
                 f"| {short(m.get('checked'))} | {ref} | {short(m.get('commit'))} | {actual or '(root)'} "
                 f"| {'✓' if checked_ok else '✗'} | {m.get('applied')} | {m['strict']} | {len_s} "
                 f"| {'✓' if m['valid'] else '✗'} | {';'.join(verdict) or '—'} |")
    L.append("")

    L.append("## outer（代际底座）\n")
    L.append("| outer | base ref | base commit | 边界 commit | 边界 benchmark/predicted | 内部 gens | best lenient | same-as-prev |")
    L.append("|---|---|---|---|---|---|---|---|")
    for nid, m in sorted(outer_nodes.items(), key=lambda kv: kv[1]["outer"]):
        pb = replay_d["prev_bench"].get(m["outer"]) or {}
        len_s = f"{m['lenient']:.3f}" if m.get("lenient") is not None else "—"
        L.append(f"| {m['outer']} | {m.get('ref')} | {short(m.get('commit'))} | {short(pb.get('commit'))} "
                 f"| {pb.get('benchmark')}/{pb.get('predicted')} | {m['gens'] or '—'} | {len_s} "
                 f"| {'是' if m['same_as_prev'] else '否'} |")
    L.append("")

    commits = {m.get("commit") for m in inner_nodes.values()}
    if ga:
        L.append(f"- git code root：`{ga['code_root']}`；树内 commit 总数："
                 f"{len(set(ga['refs'].values()))}（全部 ref 指向 {len(commits)} 个 commit）")
        if len(commits) == 1:
            L.append("- ⚠ **退化树**：所有代共享同一 commit（每代 `applied:false`，patch 从未落地）——"
                     "系谱由 ref 等价表达，git 层无真实分叉。")
    L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    args = ap.parse_args()

    for run_dir in args.run_dirs:
        run_dir = os.path.normpath(run_dir)
        run_name = os.path.basename(run_dir)
        outdir = os.path.join(run_dir, "score_analysis", "lineage_meta")
        os.makedirs(outdir, exist_ok=True)
        rd = replay(run_dir)
        strict, lenient, valid_parent = per_gen_metrics(run_dir)
        ga = git_audit(run_dir, rd)

        inner_nodes, inner_edges = build_inner(rd, strict, lenient, valid_parent)
        outer_nodes, outer_edges = build_outer(rd, strict, lenient)

        made = []
        for metric, score_inner, score_outer in (
                ("lenient", lambda m: m.get("lenient"), lambda m: m.get("lenient")),
                ("strict", lambda m: m.get("strict"), lambda m: m.get("strict"))):
            made.append(render(inner_nodes, inner_edges,
                               score_inner, outdir, f"inner_{metric}", run_name))
            made.append(render(outer_nodes, outer_edges,
                               score_outer, outdir, f"outer_{metric}", run_name))

        audit_path = os.path.join(run_dir, "score_analysis", "lineage_audit.md")
        write_audit(run_dir, rd, inner_nodes, outer_nodes, ga, audit_path)
        shutil.rmtree(outdir, ignore_errors=True)  # shim was render-time only
        print(f"[OK] {run_name}:")
        for p in made + [audit_path]:
            print(f"     {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
