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
``tool_output``  one tool result carried into the conversation (in THIS chat
                 protocol tool results ride inside user turns, so this kind
                 never materializes here — vocabulary retained, docs/10 §11.5)
``traj_seg``     one segment of a prebuilt trajectory index (fetch materializes)
``kb_note``      RETAINED vocabulary, never materialized (knowledge notes are
                 read via the knowledge tool, not as blocks — docs/10 §12.3 T2)
``note``         scratchpad note (ctx_note; auto-pinned — never dropped)
``boundary``     RETAINED vocabulary: question-boundary marker, no materializing
                 code path in v1 (multi-question sessions only, docs/10 §11.4)
``derived``      compaction product; id ``derived:<op>:<n>``, refs its sources
                 (v1: ``compact`` marks the source blocks instead — register_derived
                 is unused; enabled with L4, docs/10 §11.5/§11.7)

View forms: ``full`` | ``head_tail`` | ``excerpt`` | ``placeholder`` | ``template``.
Rendering is PURE: every form derives from the block's own text or is a
content-free placeholder — no new content can enter a view (§5.2 不变量 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

FORMS = ("full", "head_tail", "excerpt", "placeholder", "template", "dedup")
KINDS = ("turn_output", "tool_output", "traj_seg", "kb_note", "note",
         "boundary", "derived")

HEAD_TAIL_KEEP = 400    # chars kept on each side for head_tail
EXCERPT_KEEP = 600      # chars kept on each side for excerpt
MIN_COMPACT_SAVE = 200  # refuse compaction that saves less than this
PLACEHOLDER_OVERHEAD = 96  # ~ chars a placeholder costs (drop-floor guard)


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
    """The frozen placeholder rendering for one block (no content)."""
    rule = str(rule)[:60]
    return (f"[block {block.block_id} omitted: {block.chars} chars, "
            f"kind={block.kind}, reason={rule}]")


def excerpt_saving(block: "Block") -> int:
    return max(0, block.chars - len(cut(block.text, EXCERPT_KEEP)))


def render_form(block: "Block", form: str) -> str:
    """Render one block in a view form (pure; no new content)."""
    if form == "full":
        return block.text
    if form == "head_tail":
        return cut(block.text, HEAD_TAIL_KEEP)
    if form == "excerpt":
        return cut(block.text, EXCERPT_KEEP)
    if form == "placeholder":
        return placeholder(block, "view form")
    if form == "template":
        meta = block.to_meta()
        return (f"[block {block.block_id} template: kind={meta['kind']}, "
                f"chars={meta['chars']}, source={meta['source']}]")
    raise ValueError(f"invalid form {form!r}")


@dataclass
class Block:
    block_id: str
    kind: str
    text: str            # FULL content; never sent unless the view form says so
    turn: int            # creation turn
    source: str = ""     # origin descriptor (tool name / genid / ...)
    pinned: bool = False
    msg_index: Optional[int] = None   # where this text lives in the history

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_meta(self) -> Dict[str, Any]:
        """Metadata projection (ctx_index / cm_view): sizes + flags, NO text."""
        return {"block_id": self.block_id, "kind": self.kind, "turn": self.turn,
                "chars": self.chars, "token_est": estimate_tokens(self.text),
                "pinned": self.pinned, "source": self.source[:60]}


@dataclass
class View:
    """Per-block form selection (sticky across turns; ``fetch`` restores)."""
    forms: Dict[str, str] = field(default_factory=dict)  # block_id -> form
    rules: Dict[str, str] = field(default_factory=dict)  # block_id -> rule tag

    def form_of(self, block_id: str) -> str:
        return self.forms.get(block_id, "full")

    def set(self, block_id: str, form: str, rule: str) -> None:
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
        """Compaction product: refs its source blocks (溯源). The caller must
        build ``text`` from the sources only — the post-segment verifies it."""
        self._counter += 1
        block = Block(block_id=f"derived:{op}:{self._counter}", kind="derived",
                      text=text or "", turn=turn,
                      source=",".join(sources)[:120])
        self.blocks[block.block_id] = block
        return block

    def get(self, block_id: str) -> Optional[Block]:
        return self.blocks.get(str(block_id))

    def ordered(self, kinds: Optional[List[str]] = None) -> List[Block]:
        return [b for b in self.blocks.values()
                if kinds is None or b.kind in kinds]
