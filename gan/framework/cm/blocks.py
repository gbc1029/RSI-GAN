"""Block model + view forms (docs/10 §5.2.3, 冻结).

The UNIVERSE is this session's set of materialized blocks, add-only. The VIEW
is the per-block form selection (sticky across turns) that decides what is
actually SENT. A block's full text lives only in the universe (and in the raw
trajectory rows, which are the untouched audit record — 双记); the view
references it.

Kinds (closed vocabulary — audit texts and policies may only use these;
RETAINED names nothing materializes yet are marked, so an agent-facing kind
list must not advertise them):
``turn_output``  one LLM turn's text (assistant/user message content)
``tool_output``  one tool result carried into the conversation. In the chat
                 protocol this seam serves, tool results usually ride inside
                 the next user turn, so this kind materializes only when the
                 client returns a genuine tool-role entry (the session's
                 role→kind map; docs/10 §11.5) — vocabulary retained either
                 way.
``traj_seg``     one segment of a prebuilt trajectory index (fetch materializes)
``kb_note``      RETAINED vocabulary, never materialized (knowledge notes are
                 read via the knowledge tool, not as blocks — docs/10 §12.3 T2)
``note``         scratchpad note (ctx_note; auto-pinned — never dropped)
``boundary``     RETAINED vocabulary: question-boundary marker, no materializing
                 code path in v1 (multi-question sessions only, docs/10 §11.4)
``derived``      compaction product; id ``derived:<op>:<n>``, refs its sources
                 (merge/template materialize it — docs/10 §15; first_last
                 stays form-level. register_derived is NOT on any
                 face, so no caller can fabricate derived text — no-
                 fabrication by construction. A derived block becomes PINNED
                 the moment it is consumed as a source of a higher derived
                 block: consumed nodes never re-compact, keeping the lineage
                 a tree, not a DAG)

View forms: ``full`` | ``head_tail`` | ``placeholder`` | ``template`` (plus the
two session-anchored forms ``dedup`` and ``derived_host``, whose text depends
on session state). Rendering is PURE: every form derives from the block's own
text or is a content-free placeholder — no new content can enter a view
(§5.2 不变量 1). ``bake()`` is the single source of the CONTEXT-FREE forms'
SENT TEXT; the session-anchored ones are resolved by ``CMSession.view_text``,
which is the single source for ALL forms, and the accounting funnel there
books the SAVING as a signed transition width — ``form_saving()`` is only a
gate helper ("how much would this form save vs full"), never an op's saved
(docs/19 §12.40).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

FORMS = ("full", "head_tail", "placeholder", "template", "dedup",
         "derived_host")
KINDS = ("turn_output", "tool_output", "traj_seg", "kb_note", "note",
         "boundary", "derived")

HEAD_TAIL_KEEP = 600    # default per-side keep for the head_tail form (the
# session key cm_head_tail_keep overrides it; excerpt form retired — docs/19
# §12.21)
HEAD_TAIL_KEEP_MAX = 2000  # clamp ceiling for cm_head_tail_keep (cost_keys)
MIN_COMPACT_SAVE = 200  # refuse compaction that saves less than this
PLACEHOLDER_OVERHEAD = 128  # ~ chars a placeholder line costs (declared
# approximation, drop-floor guard; recalibrated for the enriched line, no
# runtime consumer — docs/19 §12.20)
DERIVED_PIECE_KEEP = 300  # per-source head+tail keep inside a merge derived text


def estimate_tokens(text: str) -> int:
    """Coarse frozen token estimate (chars/4); metadata only, never billing."""
    return len(text or "") // 4


def cut(text: str, keep: int) -> str:
    """Head+tail cut with an explicit in-band omission marker."""
    if len(text) <= 2 * keep:
        return text
    omitted = len(text) - 2 * keep
    return (text[:keep] + f"\n...[omitted {omitted} chars]...\n" + text[-keep:])


def placeholder(block: "Block", rule: str) -> str:
    """Eviction EVENT (docs/10 form 语义声明): the block's content is gone
    from the view and the line says WHY (reason = the op's rule tag) and
    WHERE the block came from (source, unclipped — address semantics, same
    as to_meta().source). Recoverable via fetch(block_id, form="full").
    No content."""
    rule = str(rule)[:60]
    return (f"[block {block.block_id} omitted: {block.chars} chars, "
            f"kind={block.kind}, source={block.source}, reason={rule}]")


def bake(block: "Block", form: str, rule: str = "", keep: Optional[int] = None,
         host_text: Optional[str] = None) -> str:
    """The sent text of one block in a CONTEXT-FREE view form.

    No second copy of these formulas may exist (docs/19 §12.21). Two forms
    are SESSION-ANCHORED and cannot be baked here without session state:
    ``dedup`` (needs the current system prefix) and ``derived_host`` (needs
    the resolved derived text). ``CMSession.view_text`` is the single source
    for a block's sent text across ALL forms — render, the marker re-bind
    and the saving accounting all consume it; this function supplies the
    context-free half only. ``derived_host`` therefore REQUIRES
    ``host_text``: a missing host is a programming error, never "render the
    marker alone" (that silently produced a ~40-char lie for the saved
    accounting — docs/19 §12.40, F2).
    """
    rule = str(rule)[:60]
    if form == "full":
        return block.text
    if form == "head_tail":
        return (cut(block.text, keep if keep is not None else HEAD_TAIL_KEEP)
                + f"\n[block {block.block_id} head_tail: {rule or 'view'}]")
    if form == "placeholder":
        return placeholder(block, rule)
    if form == "template":
        meta = block.to_meta()
        return (f"[block {block.block_id} template: kind={meta['kind']}, "
                f"chars={meta['chars']}, source={meta['source']}, "
                f"reason={rule or 'view'}]")
    if form == "derived_host":
        if host_text is None:
            raise ValueError(
                "bake(derived_host) needs the resolved host text; use "
                "CMSession.view_text (the single source for anchored forms)")
        return (host_text
                + f"\n[block {block.block_id} derived_host: {rule}]")
    raise ValueError(f"invalid form {form!r}")


def form_saving(block: "Block", form: str, rule: str = "",
                keep: Optional[int] = None,
                host_text: Optional[str] = None) -> int:
    """GATE helper: how much a form would save **relative to full** (clamped
    at 0). Used by the pipeline's "does this elision save at least
    MIN_COMPACT_SAVE" gates only.

    It is NOT an op's saving: an op's saved is a TRANSITION from the block's
    CURRENT rendered form, which needs session state (dedup prefix / derived
    host) and the previous form — ``CMSession`` owns that single accounting
    funnel (docs/19 §12.40, F2). Per-form copies are still forbidden.
    """
    return max(0, block.chars - len(bake(block, form, rule, keep=keep,
                                         host_text=host_text)))


def render_form(block: "Block", form: str, rule: str = "",
                keep: Optional[int] = None) -> str:
    """Render one block in a view form — thin delegate to bake() (the
    single source). Exported for tests/audit only, no runtime caller."""
    return bake(block, form, rule, keep=keep)


@dataclass
class Block:
    block_id: str
    kind: str
    text: str            # FULL content; never sent unless the view form says so
    turn: int            # creation turn
    source: str = ""     # origin descriptor (tool name / genid / ...)
    pinned: bool = False
    msg_index: Optional[int] = None   # where this text lives in the history
    sources: List[str] = field(default_factory=list)  # derived lineage (parents)
    depth: int = 0       # 0 = canonical block, N = Nth derivation generation

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_meta(self) -> Dict[str, Any]:
        """Metadata projection (ctx_index / cm_view): sizes + flags, NO text.
        Lineage fields materialize only on derived blocks (conditional audit
        fields — the canonical block projection stays byte-identical).
        ``source`` is NOT clipped (docs/19 §12.19): for ``traj_seg`` blocks
        it is the seg_id address shared with the read tools — a clipped
        address would be a silently broken one; the framework-generated
        labels are bounded by construction."""
        meta = {"block_id": self.block_id, "kind": self.kind, "turn": self.turn,
                "chars": self.chars, "token_est": estimate_tokens(self.text),
                "pinned": self.pinned, "source": self.source}
        if self.kind == "derived":
            meta["sources"] = list(self.sources)
            meta["depth"] = self.depth
        return meta


@dataclass
class View:
    """Per-block form selection (sticky across turns; ``fetch`` restores)."""
    forms: Dict[str, str] = field(default_factory=dict)  # block_id -> form
    rules: Dict[str, str] = field(default_factory=dict)  # block_id -> rule tag

    def form_of(self, block_id: str) -> str:
        return self.forms.get(block_id, "full")

    def set(self, block_id: str, form: str, rule: str) -> None:
        """Sticky form write. ``rule`` is clipped to 60 (docs/19 §12.19):
        it is a rule TAG, never content — the cap bounds the surfaces that
        echo it (the render-suffix line, the cm_view brief at the same 60)."""
        if form not in FORMS:
            raise ValueError(f"invalid form {form!r}")
        self.forms[block_id] = form
        self.rules[block_id] = str(rule)[:60]


@dataclass
class Universe:
    """Add-only registry of this session's materialized blocks."""
    blocks: Dict[str, Block] = field(default_factory=dict)
    _counter: int = 0

    def register(self, kind: str, text: str, turn: int, source: str = "",
                 pinned: bool = False) -> Block:
        if kind not in KINDS:
            raise ValueError(f"invalid block kind {kind!r}")
        self._counter += 1
        block = Block(block_id=f"b{self._counter}", kind=kind, text=text or "",
                      turn=turn, source=source, pinned=pinned)
        self.blocks[block.block_id] = block
        return block

    def register_derived(self, op: str, text: str, turn: int,
                         sources: List[str]) -> Block:
        """Compaction product: refs its source blocks (溯源). NOT on any
        face — only frozen ops code may mint derived blocks, and the
        primitive builds ``text`` from the sources only, so no-fabrication
        holds by construction (docs/10 §15). Sources must already exist
        (the caller validates); the shared counter keeps ids unique, so the
        lineage is acyclic by construction. Depth = 1 + max(parent depths)."""
        self._counter += 1
        srcs = [str(s) for s in sources]
        parent_depth = max((self.blocks[s].depth for s in srcs
                            if s in self.blocks), default=0)
        block = Block(block_id=f"derived:{op}:{self._counter}", kind="derived",
                      text=text or "", turn=turn,
                      source=",".join(srcs)[:120],
                      sources=srcs, depth=parent_depth + 1)
        self.blocks[block.block_id] = block
        return block

    def get(self, block_id: str) -> Optional[Block]:
        return self.blocks.get(str(block_id))

    def ordered(self, kinds: Optional[List[str]] = None) -> List[Block]:
        return [b for b in self.blocks.values()
                if kinds is None or b.kind in kinds]
