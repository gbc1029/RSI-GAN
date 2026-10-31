"""Version DAG for the three roles.

A :class:`TreeStore` persists an append-only event log (JSONL) per tree and
reconstructs node state by replay. It supports a **DAG** (a node may have
multiple parents, e.g. produced by a crossover operator) while staying
backward-compatible with the old single-parent ``parent_genid`` field.

It enforces the task constraints (branch limit / depth limit) and offers parent
selection over four strategies, optionally returning ``k`` parents for crossover:

- ``lcb``    -- DEFAULT. Confidence-set selection: rank by a Wilson LOWER bound on
  the run-persistent anchor core, keep every node within ``lcb_delta`` of the best
  one (a set that is *statistically indistinguishable* from the leader), then pick
  inside that set by "fewest children" (i.e. expand the candidate we know least
  about). Exploration can never leave the confidence set.
- ``ucb``    -- legacy additive bandit ``score + c*sqrt(ln N / visits) - penalties``.
- ``best``   -- plain ``score`` argmax.
- ``latest`` -- most recently added candidates.

WHY ``lcb`` is the default. The additive UCB metric is dominated by its own
exploration term at this sample size: a node's ``score`` lives in [0, 1] while the
bonus reaches ~1.4 at ``visits=2``, so the ranking degenerates into "pick the
least-visited node" -- which happily re-selects a node the data already shows to
be worse (a single lucky draw reads as 1.0, and the average is never shrunk).
Because every generation is evaluated exactly ONCE, "visit it again to tighten the
estimate" is not available either: the only honest fix is a pessimistic,
small-sample-corrected estimate plus an exploration budget confined to the nodes
that could still be the best one.

Can bootstrap from a DGM-H ``archive.jsonl`` via
:meth:`TreeStore.import_from_dgm_archive`.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NodeValue:
    score: Optional[float] = None       # report.json numeric score (from benchmark)
    potential: Optional[float] = None   # evaluator blind prediction (tie-break / context only)
    cost_tokens: Optional[float] = None
    cost_wallclock_s: Optional[float] = None
    score_status: str = "ok"            # ok | partial | imputed | failed | invalid
    coverage: Optional[float] = None    # valid_samples / requested_samples

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Node:
    genid: Any
    parent_genid: Any = None                 # primary parent (kept for compatibility)
    parents: List[Any] = field(default_factory=list)  # full parent list (DAG)
    curr_patch_files: List[str] = field(default_factory=list)
    valid_parent: bool = True
    depth: int = 0
    children: int = 0
    status: str = "alive"               # alive | stagnant | pruned
    value: NodeValue = field(default_factory=NodeValue)
    scores: Dict[str, Any] = field(default_factory=dict)
    modify_depth: int = 0               # 0=config 1=operator 2=code
    source_access_log: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # normalise the two representations so both work
        if self.parents and self.parent_genid is None:
            self.parent_genid = self.parents[0]
        elif self.parent_genid is not None and not self.parents:
            self.parents = [self.parent_genid]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["value"] = self.value.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        d = dict(d)
        value = d.pop("value", {}) or {}
        node = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        node.value = NodeValue(**{k: v for k, v in value.items() if k in NodeValue.__dataclass_fields__})
        return node


def _key(genid: Any) -> str:
    return str(genid)


# Score statuses that are REAL measurements. Mirrors ``loop._MEASURED`` (the tree
# store cannot import the loop -- the loop imports this module). An ``imputed``
# score is a fallback value, not evidence, so it must never enter a confidence
# bound: treating a sibling-median as if it had been measured is how a bound stops
# being a bound.
_MEASURED_STATUSES = ("ok", "partial")


def _wilson_lcb(passed: float, n: int, z: float) -> Optional[float]:
    """Wilson score interval LOWER bound for ``passed`` successes out of ``n``.

    Preferred over the raw ratio because it is defined (and finite) at the edges:
    1/1 is 0.21 at z=1.64, not 1.0, and 0/0 has no answer at all. ``z`` is the
    confidence knob (1.64 ~ one-sided 95%); ``z=0`` degenerates to the plain ratio
    (escape hatch / ablation).
    """
    if n <= 0:
        return None
    p = min(1.0, max(0.0, float(passed) / float(n)))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return max(0.0, centre - half)


def _genid_order(genid: Any) -> tuple:
    """Total, STABLE ordering over genids (ints first, numeric where possible).

    Tie-breaks must not fall back to dict/insertion order: a resume replays the
    same tree and must pick the same node, otherwise the lineage audit
    (``analysis/gan_lineage_graph.py``) can no longer be reproduced.
    """
    try:
        return (0, int(genid), "")
    except (TypeError, ValueError):
        return (1, 0, str(genid))


class TreeStore:
    def __init__(self, output_dir: str | os.PathLike, name: str):
        from gan.framework import paths
        self.output_dir = os.path.abspath(str(output_dir))
        self.name = name
        self.path = paths.tree_path(self.output_dir, name)
        self.nodes: Dict[str, Node] = {}
        self.order: List[str] = []

    # -- persistence -------------------------------------------------------
    def load(self) -> "TreeStore":
        if not os.path.exists(self.path):
            return self
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                evt = json.loads(line)
                self._apply(evt)
        self._recompute_children()
        return self

    @staticmethod
    def _counts_as_child(node: Node) -> bool:
        return bool(
            node.valid_parent
            and node.status != "invalid"
            and node.value.score_status != "invalid"
            and not node.meta.get("invalid")
        )

    def _recompute_children(self) -> None:
        for node in self.nodes.values():
            node.children = 0
        for child in self.nodes.values():
            if not self._counts_as_child(child):
                continue
            for parent_id in child.parents:
                parent = self.nodes.get(_key(parent_id))
                if parent is not None:
                    parent.children += 1

    def _apply(self, evt: Dict[str, Any]) -> None:
        op = evt.get("op")
        if op == "add":
            node = Node.from_dict(evt["node"])
            k = _key(node.genid)
            if k not in self.nodes:
                self.order.append(k)
            self.nodes[k] = node
        elif op == "update":
            k = _key(evt["genid"])
            node = self.nodes.get(k)
            if node is not None:
                for name, val in evt.get("fields", {}).items():
                    if name == "value":
                        node.value = NodeValue(**val)
                    else:
                        setattr(node, name, val)
        elif op == "reset":
            # Rollback marker: rebuild the node set without truncating the log.
            self.nodes = {}
            self.order = []
            for nd in evt.get("nodes", []) or []:
                node = Node.from_dict(nd)
                k = _key(node.genid)
                if k not in self.nodes:
                    self.order.append(k)
                self.nodes[k] = node

    def _append(self, evt: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")

    # -- mutations ---------------------------------------------------------
    def add_node(self, node: Node) -> Node:
        # register child with every parent; depth = max(parent depths)+1
        depths: List[int] = []
        for p in node.parents:
            if _key(p) in self.nodes:
                parent = self.nodes[_key(p)]
                depths.append(parent.depth)
                if self._counts_as_child(node):
                    parent.children += 1
                    self._append(
                        {"op": "update", "genid": parent.genid,
                         "fields": {"children": parent.children}}
                    )
        node.depth = (max(depths) + 1) if depths else 0
        self.nodes[_key(node.genid)] = node
        if _key(node.genid) not in self.order:
            self.order.append(_key(node.genid))
        self._append({"op": "add", "node": node.to_dict()})
        return node

    def update(self, genid: Any, **fields: Any) -> None:
        node = self.nodes.get(_key(genid))
        if node is None:
            return
        payload: Dict[str, Any] = {}
        for k, v in fields.items():
            if k == "value" and isinstance(v, NodeValue):
                node.value = v
                payload["value"] = v.to_dict()
            else:
                setattr(node, k, v)
                payload[k] = v
        self._append({"op": "update", "genid": genid, "fields": payload})

    def get(self, genid: Any) -> Optional[Node]:
        return self.nodes.get(_key(genid))

    def __len__(self) -> int:
        return len(self.nodes)

    # -- selection ---------------------------------------------------------
    def _candidates(self, branch_limit: int, depth_limit: int,
                    outer_id: Optional[int] = None,
                    exclude: Optional[set] = None) -> List[Node]:
        out = []
        for k in self.order:
            if exclude is not None and k in exclude:
                continue
            n = self.nodes[k]
            if not n.valid_parent or n.status != "alive":
                continue
            if n.children >= branch_limit and n.meta.get("outer_id") is not None:
                # The shared root / legacy anchor nodes (no outer_id) are the
                # only starting point for a new outer's first generation.
                # Their parent capacity must not be exhausted by the branch
                # limit, otherwise a new outer cannot begin once the root has
                # spawned branch_limit outer-seeds. Regular nodes that belong
                # to a specific outer still respect the limit.
                continue
            if n.depth >= depth_limit:
                continue
            if outer_id is not None:
                # Subtree selection (docs/13 §2.4): keep this outer's nodes plus
                # legacy/initial nodes that carry no outer_id (back-compat).
                n_outer = n.meta.get("outer_id")
                if n_outer is not None and n_outer != outer_id:
                    continue
            out.append(n)
        return out

    def _metric(self, n: Node, total_children: int, max_cost: float,
                method: str, ucb_c: float, depth_penalty: float, cost_penalty: float) -> float:
        """Legacy additive metric (``ucb`` / ``best`` only; NOT used by ``lcb``)."""
        score = n.value.score if n.value.score is not None else 0.0
        if method == "best":
            return score
        visits = n.children + 1
        explore = ucb_c * math.sqrt(math.log(total_children + 1) / visits)
        cost = (n.value.cost_tokens or 0.0) / max_cost
        return score + explore - depth_penalty * n.depth - cost_penalty * cost

    @staticmethod
    def _anchor_evidence(n: Node) -> tuple:
        """``(correct, answered)`` on the anchor core, stamped by the loop.

        ``anchor_n`` counts ANSWERED anchors only: a generation that crashed or
        was truncated must not be scored as a wall of misses (that is the right
        convention for the stagnation metric, which asks "did it move?", but the
        wrong one for selection, which asks "which node is best?").
        """
        correct = n.meta.get("anchor_correct")
        answered = n.meta.get("anchor_n")
        if (isinstance(correct, (int, float)) and not isinstance(correct, bool)
                and isinstance(answered, int) and not isinstance(answered, bool)
                and answered > 0):
            return float(correct), int(answered)
        return None, 0

    def _lcb_ordering(self, n: Node, max_cost: float,
                      depth_penalty: float, cost_penalty: float) -> tuple:
        """Stage-2 key INSIDE the confidence set: expand what we know least about.

        ``children`` is the number of times this node was already used as a parent
        -- the only "information already spent" proxy the tree has. Penalties are
        a SECONDARY key here on purpose: promoting them to a filter would let a
        non-statistical reason (deep / expensive) evict a node from the confidence
        set, and the set would stop meaning "could still be the best one".
        """
        cost = (n.value.cost_tokens or 0.0) / max_cost
        return (n.children,
                depth_penalty * n.depth + cost_penalty * cost,
                _genid_order(n.genid))

    def _select_parents_lcb(self, cands: List[Node], k: int,
                            depth_penalty: float, cost_penalty: float,
                            lcb_delta: float, lcb_z: float,
                            report: Optional[Dict[str, Any]]) -> List[Node]:
        """Confidence-set selection (see the module docstring).

        Stage 1 (statistics): keep nodes whose anchor LCB is within ``lcb_delta``
        of the best one. ``lcb_delta <= 0`` defaults to ``2/anchor_k`` -- about two
        anchor questions, i.e. roughly ONE standard error of a difference of two
        proportions at k=10. (One question would be the intuitive width, but at
        z=1.64 the LCB gap of a single anchor is ~0.13 > 0.1, so a 1/k set would
        collapse to a singleton and the stage-2 exploration would never fire.)
        Stage 2 (exploration): inside that set, take the node with the fewest
        children. Exploration therefore happens ONLY among nodes that could
        honestly be the best; a node the evidence already rejected is never
        re-selected just because it is unvisited.

        Falls back to the legacy ranking when NO candidate carries anchor evidence
        (the very first generation, anchors disabled, legacy/DGM-imported nodes).
        """
        max_cost = max((n.value.cost_tokens or 0.0) for n in cands) or 1.0
        evidence: Dict[str, tuple] = {}
        for n in cands:
            if n.value.score_status not in _MEASURED_STATUSES:
                continue          # imputed / failed / invalid is not evidence
            correct, answered = self._anchor_evidence(n)
            if correct is None:
                continue
            lcb = _wilson_lcb(correct, answered, lcb_z)
            if lcb is not None:
                evidence[_key(n.genid)] = (lcb, answered)

        if not evidence:
            # Nothing to be statistical about: keep the pre-LCB behaviour so the
            # root generation can still start (and a run with anchors disabled is
            # not silently gutted).
            ranked = sorted(
                cands,
                key=lambda n: (
                    -(float(n.value.score) if n.value.score is not None else 0.0),
                    *self._lcb_ordering(n, max_cost, depth_penalty, cost_penalty),
                ),
            )
            if report is not None:
                report.update({"mode": "fallback_no_anchor",
                               "candidates": len(cands),
                               "set_size": len(ranked),
                               "picked": str(ranked[0].genid) if ranked else None})
            return ranked[:k]

        best = max(lcb for lcb, _ in evidence.values())
        anchor_k = max(answered for _, answered in evidence.values())
        delta = (lcb_delta if lcb_delta > 0
                 else (2.0 / anchor_k if anchor_k else 0.0))
        pool = [n for n in cands
                if _key(n.genid) in evidence
                and evidence[_key(n.genid)][0] >= best - delta]
        if not pool:                      # defensive: ``best`` is always inside
            pool = [n for n in cands if _key(n.genid) in evidence]
        pool.sort(key=lambda n: self._lcb_ordering(n, max_cost, depth_penalty,
                                                   cost_penalty))
        if report is not None:
            picked = pool[0]
            report.update({
                "mode": "lcb",
                "candidates": len(cands),
                "measured": len(evidence),
                "anchor_k": anchor_k,
                "best_lcb": round(best, 4),
                "delta": round(delta, 4),
                "set_size": len(pool),
                "picked": str(picked.genid),
                "picked_lcb": round(evidence[_key(picked.genid)][0], 4),
                "picked_children": int(picked.children),
                # The anchor core stopped discriminating (e.g. every node answers
                # every anchor correctly): the statistical filter is inert and the
                # pick is decided by stage 2 alone. Offline this must be visible.
                "degenerate": bool(len(pool) == len(evidence) and len(evidence) > 1),
            })
        return pool[:k]

    def select_parents(
        self,
        k: int = 1,
        branch_limit: int = 2,
        depth_limit: int = 5,
        method: str = "lcb",
        ucb_c: float = 1.0,
        depth_penalty: float = 0.05,
        cost_penalty: float = 0.0,
        outer_id: Optional[int] = None,
        exclude: Optional[set] = None,
        lcb_delta: float = 0.0,
        lcb_z: float = 1.64,
        report: Optional[Dict[str, Any]] = None,
    ) -> List[Node]:
        """Return up to ``k`` parents (``k>1`` used by crossover).

        ``outer_id`` restricts the candidate pool to that outer's subtree
        (nodes with a different ``meta.outer_id`` are excluded; nodes without
        an ``outer_id`` — ``initial`` and pre-migration nodes — stay eligible).
        ``exclude`` drops genids from the pool (e.g. keeping ``initial``
        root-only for the first outer).

        ``lcb_delta`` / ``lcb_z`` only apply to ``method="lcb"``.
        ``report`` (optional dict) is filled with the decision's audit fields
        (mode / set_size / picked / ...): the selection is a decision-grade fact
        and must be replayable offline, not inferred from the outcome.
        """
        cands = self._candidates(branch_limit, depth_limit, outer_id=outer_id,
                                 exclude=exclude)
        if not cands:
            if report is not None:
                report.update({"mode": "empty_pool", "candidates": 0,
                               "set_size": 0, "picked": None})
            return []
        if method == "latest":
            return list(reversed(cands[-k:]))
        if method == "lcb":
            return self._select_parents_lcb(cands, k, depth_penalty, cost_penalty,
                                            lcb_delta, lcb_z, report)
        total_children = sum(n.children for n in cands) + len(cands)
        max_cost = max((n.value.cost_tokens or 0.0) for n in cands) or 1.0
        ranked = sorted(
            cands,
            key=lambda n: self._metric(n, total_children, max_cost, method, ucb_c, depth_penalty, cost_penalty),
            reverse=True,
        )
        if report is not None:
            report.update({"mode": method, "candidates": len(cands),
                           "set_size": len(ranked),
                           "picked": str(ranked[0].genid) if ranked else None})
        return ranked[:k]

    def select_parent(
        self,
        branch_limit: int = 2,
        depth_limit: int = 5,
        method: str = "lcb",
        ucb_c: float = 1.0,
        depth_penalty: float = 0.05,
        cost_penalty: float = 0.0,
        outer_id: Optional[int] = None,
        exclude: Optional[set] = None,
        lcb_delta: float = 0.0,
        lcb_z: float = 1.64,
        report: Optional[Dict[str, Any]] = None,
    ) -> Optional[Node]:
        ps = self.select_parents(1, branch_limit, depth_limit, method, ucb_c,
                                 depth_penalty, cost_penalty, outer_id=outer_id,
                                 exclude=exclude, lcb_delta=lcb_delta, lcb_z=lcb_z,
                                 report=report)
        return ps[0] if ps else None

    # -- import from DGM-H -------------------------------------------------
    def import_from_dgm_archive(self, genids: List[Any]) -> None:
        """Bootstrap a task tree from a DGM-H archive list (genid ordering)."""
        for genid in genids:
            if _key(genid) in self.nodes:
                continue
            self.add_node(Node(genid=genid))
