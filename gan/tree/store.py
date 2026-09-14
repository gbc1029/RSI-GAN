"""Version trees for the three roles.

A :class:`TreeStore` persists an append-only event log (JSONL) per tree and
reconstructs node state by replay. It enforces the task-tree constraints
(branch limit / depth limit / stagnation) and offers a UCB-based parent
selection. It is intentionally self-contained but can bootstrap from a DGM-H
``archive.jsonl`` via :meth:`TreeStore.import_from_dgm_archive`.
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
    potential: Optional[float] = None   # evaluator judgement 0..10 (tie-break / context only)
    cost_tokens: Optional[float] = None
    cost_wallclock_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Node:
    genid: Any
    parent_genid: Any = None
    planner_genid: Any = None
    evaluator_genid: Any = None
    prev_patch_files: List[str] = field(default_factory=list)
    curr_patch_files: List[str] = field(default_factory=list)
    valid_parent: bool = True
    run_full_eval: bool = False
    depth: int = 0
    children: int = 0
    status: str = "alive"               # alive | stagnant | pruned
    value: NodeValue = field(default_factory=NodeValue)
    scores: Dict[str, Any] = field(default_factory=dict)
    modify_depth: int = 0               # 0=config 1=operator 2=code
    source_access_log: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

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


class TreeStore:
    def __init__(self, output_dir: str | os.PathLike, name: str):
        self.output_dir = os.path.abspath(str(output_dir))
        self.name = name
        self.path = os.path.join(self.output_dir, f"{name}_tree.jsonl")
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
        return self

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

    def _append(self, evt: Dict[str, Any]) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")

    # -- mutations ---------------------------------------------------------
    def add_node(self, node: Node) -> Node:
        # maintain parent child-count and depth
        if node.parent_genid is not None and _key(node.parent_genid) in self.nodes:
            parent = self.nodes[_key(node.parent_genid)]
            parent.children += 1
            node.depth = parent.depth + 1
            self._append(
                {"op": "update", "genid": parent.genid, "fields": {"children": parent.children}}
            )
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

    def mark_stagnant(self, genid: Any) -> None:
        self.update(genid, status="stagnant", valid_parent=False)

    def get(self, genid: Any) -> Optional[Node]:
        return self.nodes.get(_key(genid))

    def __len__(self) -> int:
        return len(self.nodes)

    # -- selection ---------------------------------------------------------
    def _candidates(self, branch_limit: int, depth_limit: int) -> List[Node]:
        out = []
        for k in self.order:
            n = self.nodes[k]
            if not n.valid_parent or n.status != "alive":
                continue
            if n.children >= branch_limit:
                continue
            if n.depth >= depth_limit:
                continue
            out.append(n)
        return out

    def select_parent(
        self,
        branch_limit: int = 2,
        depth_limit: int = 5,
        method: str = "ucb",
        ucb_c: float = 1.0,
        depth_penalty: float = 0.05,
        cost_penalty: float = 0.0,
    ) -> Optional[Node]:
        cands = self._candidates(branch_limit, depth_limit)
        if not cands:
            return None

        if method == "latest":
            return cands[-1]
        if method == "best":
            return max(cands, key=lambda n: (n.value.score if n.value.score is not None else -1e9))

        total_children = sum(n.children for n in cands) + len(cands)
        max_cost = max((n.value.cost_tokens or 0.0) for n in cands) or 1.0

        def metric(n: Node) -> float:
            score = n.value.score if n.value.score is not None else 0.0
            visits = n.children + 1
            explore = ucb_c * math.sqrt(math.log(total_children + 1) / visits)
            cost = (n.value.cost_tokens or 0.0) / max_cost
            return score + explore - depth_penalty * n.depth - cost_penalty * cost

        return max(cands, key=metric)

    # -- import from DGM-H -------------------------------------------------
    def import_from_dgm_archive(self, genids: List[Any]) -> None:
        """Bootstrap a task tree from a DGM-H archive list (genid ordering)."""
        for genid in genids:
            if _key(genid) in self.nodes:
                continue
            self.add_node(Node(genid=genid))
