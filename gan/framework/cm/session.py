"""CM session: per-chat-run Universe/View + per-turn assembly pipeline.

The seam (docs/10 §5.2 分一): every LLM call passes the session's message
history through :meth:`CMSession.assemble` — fixed pre-segment (window
defaults + conservative compaction) -> optional custom policy -> post-segment
apply (count + emit ``cm_view``) -> render. View forms PERSIST across turns
(a block compacted last turn stays compacted; ``fetch`` restores it).
``passthrough`` mode is the identity view (pre-D behaviour, byte identical).

The canonical history is never mutated: substitutions are applied to shallow
copies, so the raw trajectory rows stay the untouched audit face (双记).
Effective key values are injected ALREADY CLAMPED by the frozen
``gan.framework.cost_keys`` authority — this module never clamps, it only
type-guards (the harness pass-through posture). The ``cm.ops`` primitives
validate BEFORE mutating (拒绝即不动); the seam only counts and emits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ops
from .blocks import (Block, Universe, View, cut, placeholder, estimate_tokens,
                       EXCERPT_KEEP, HEAD_TAIL_KEEP)

# Framework floor (docs/10 §5.2.3: K 是带框架下限的键) — the clamping
# authority owns the ranges; this is the hard invariant this layer adds.
WINDOW_K_FLOOR = 1
_NOTE_PREFIX = "[gan:note]"
_WARN_RATIO = 0.8      # budget_checkpoint warns at 80% of an active budget
_POLICY_STRIKES = 2    # C1-class wedge breaker: 2 failures -> disabled
_KIND_FOR_ROLE = {"tool": "tool_output"}
# view artifacts carry this marker prefix (placeholder/template/excerpt
# suffix) — history entries showing it are re-bound to their block, never
# re-registered as new content (the LLM client rebuilds the history list on
# every call, so object identity does not survive a turn).
_VIEW_MARKER_PREFIX = "[block "
_DEDUP_MARKER = "[gan:system prompt deduplicated]"


def _parse_view_marker(content: str) -> Optional[str]:
    """Extract ``block_id`` from a baked view artifact, or None."""
    if _VIEW_MARKER_PREFIX not in content:
        return None
    idx = content.index(_VIEW_MARKER_PREFIX) + len(_VIEW_MARKER_PREFIX)
    rest = content[idx:]
    token = rest.split(" ", 1)[0] if " " in rest else ""
    token = token.rstrip("].,;")
    if token and (token.startswith("b") or token.startswith("derived:")):
        return token
    return None


def _fingerprint(content: str) -> str:
    import hashlib
    return hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()


class ViewHandle:
    """The primitive facade handed to BOTH faces (agent ctx_* tools and the
    assembly-time custom policy). Face membership decides what is callable
    (ops.AGENT_FACES / ops.POLICY_FACES); the policy face carries the
    per-turn op budget (``cm_max_ops_per_turn``). Every op returns a
    structured record that never carries block content (peek/note reads are
    the explicit content requests)."""

    def __init__(self, session: "CMSession", face: str):
        self._s = session
        self.face = face
        self._budget: Optional[int] = (
            session.max_ops_per_turn if face == "policy" else None)

    def _audit(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Policy-face refusals are violations wherever they happen (the
        wedge breaker counts them via the session, not only in the seam)."""
        if self.face == "policy" and not rec.get("ok"):
            self._s.note_violation(rec)
        return rec

    def _gate(self, op: str) -> Optional[Dict[str, Any]]:
        if not ops.face_allows(self.face, op):
            return self._audit({"op": op, "ok": False,
                                "error": f"op {op!r} not on face {self.face!r}"})
        if self._budget is not None:
            if self._budget <= 0:
                return self._audit({"op": op, "ok": False,
                                    "error": "op budget exceeded"})
            self._budget -= 1
        return None

    # -- read-only ---------------------------------------------------------
    def index(self, kinds: Optional[List[str]] = None) -> Dict[str, Any]:
        if err := self._gate("index"):
            return err
        return ops.index_blocks(self._s.universe, kinds)

    def peek(self, block_id: str) -> Dict[str, Any]:
        if err := self._gate("peek"):
            return err
        return ops.peek(self._s.universe, block_id)

    def view_state(self) -> Dict[str, Any]:
        if err := self._gate("view_state"):
            return err
        return ops.view_state(self._s.universe, self._s.view)

    def budget_query(self, tokens_used: int = 0,
                     budget: Optional[int] = None) -> Dict[str, Any]:
        if err := self._gate("budget_query"):
            return err
        return self._s.budget_snapshot(tokens_used, budget)

    # -- mutations (ops validate first; view mutates only on success) ---------
    def fetch(self, block_id: str, form: str = "full") -> Dict[str, Any]:
        if err := self._gate("fetch"):
            return err
        return self._audit(ops.fetch(self._s.universe, self._s.view, block_id, form))

    def pin(self, block_id: str, note: str = "") -> Dict[str, Any]:
        if err := self._gate("pin"):
            return err
        return self._audit(ops.pin(self._s.universe, self._s.view, block_id, True, note))

    def unpin(self, block_id: str) -> Dict[str, Any]:
        if err := self._gate("unpin"):
            return err
        return self._audit(ops.pin(self._s.universe, self._s.view, block_id, False))

    def drop(self, block_id: str, rule: str = "policy") -> Dict[str, Any]:
        if err := self._gate("drop"):
            return err
        return self._audit(ops.drop(self._s.universe, self._s.view, block_id, rule,
                        self._s.drop_floor_chars))

    def compact(self, block_ids: List[str],
                strategy: str = "first_last") -> Dict[str, Any]:
        if err := self._gate("compact"):
            return err
        return self._audit(ops.compact(self._s.universe, self._s.view, list(block_ids),
                           strategy, self._s.drop_floor_chars))

    def substitute(self, block_id: str, form: str) -> Dict[str, Any]:
        if err := self._gate("substitute"):
            return err
        return self._audit(ops.substitute(self._s.universe, self._s.view, block_id, form,
                              "policy", self._s.drop_floor_chars))

    def order(self, block_ids: List[str]) -> Dict[str, Any]:
        if err := self._gate("order"):
            return err
        return self._audit(ops.order(self._s.universe, self._s.view, list(block_ids)))

    # -- notes (scratchpad) -----------------------------------------------------
    def note_write(self, text: str) -> Dict[str, Any]:
        if err := self._gate("note"):
            return err
        return ops.note_write(self._s, text, self._s.note_cap_chars)

    def note_read(self, note_id: str) -> Dict[str, Any]:
        if err := self._gate("note"):
            return err
        return ops.note_read(self._s.notes, note_id)

    def note_list(self) -> Dict[str, Any]:
        if err := self._gate("note"):
            return err
        return ops.note_list(self._s.notes)


@dataclass
class CMSession:
    """One chat run's CM state (universe + view + config + audit counters)."""
    mode: str = "passthrough"
    window_k: int = 4
    drop_floor_chars: int = 400
    max_ops_per_turn: int = 32
    policy_exec_ms: int = 200
    note_cap_chars: int = 8_000
    emit: Optional[Callable[[Dict[str, Any]], None]] = None
    budget_reader: Optional[Callable[[], Dict[str, Any]]] = None
    policy_path: Optional[str] = None  # custom mode: agent policy module path
    system_prefix: str = ""  # per-turn tool-use header (dedup op target)
    turn: int = 0
    universe: Universe = field(default_factory=Universe)
    view: View = field(default_factory=View)
    notes: Dict[str, str] = field(default_factory=dict)
    note_seq: int = 0
    # per-session audit (session cost summary / receipt ``cm`` node)
    ops_total: int = 0
    ops_applied: int = 0
    ops_refused: int = 0
    policy_failures: int = 0
    policy_disabled: bool = False
    violations: int = 0
    est_chars_saved: int = 0
    _policy_strikes: int = 0
    # identity-keyed bookkeeping: rewritten shallow copies are not re-registered
    _by_fp: Dict[str, str] = field(default_factory=dict)  # content fp -> bid

    # ------------------------------------------------------------------- config
    def apply_keys(self, keys: Dict[str, Any]) -> None:
        """Type-guarded application of ALREADY-CLAMPED effective values."""
        mode = keys.get("cm_mode")
        if isinstance(mode, str) and mode in ("passthrough", "slice",
                                              "compact_conservative", "custom"):
            self.mode = mode
        k = keys.get("cm_window_k")
        if type(k) is int and k >= WINDOW_K_FLOOR:
            self.window_k = k
        v = keys.get("cm_drop_floor_chars")
        if type(v) is int and v >= 0:
            self.drop_floor_chars = v
        v = keys.get("cm_max_ops_per_turn")
        if type(v) is int and v >= 1:
            self.max_ops_per_turn = v
        v = keys.get("cm_policy_exec_ms")
        if type(v) is int and v >= 1:
            self.policy_exec_ms = v
        v = keys.get("cm_note_cap_chars")
        if type(v) is int and v >= 1:
            self.note_cap_chars = v

    def summary(self) -> Dict[str, Any]:
        """Session-level CM summary (cost record / receipt ``cm`` node)."""
        return {"mode": self.mode, "turns": self.turn,
                "blocks": len(self.universe.blocks),
                "ops_total": self.ops_total, "ops_applied": self.ops_applied,
                "ops_refused": self.ops_refused,
                "policy_failures": self.policy_failures,
                "policy_disabled": self.policy_disabled,
                "violations": self.violations,
                "est_chars_saved": self.est_chars_saved}

    # ------------------------------------------------------------------- events
    def _emit(self, event: Dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event)
        except Exception:
            pass  # the audit sink must never break the session

    # ---------------------------------------------------- block registration
    def register_history(self, msgs: List[Dict[str, Any]]) -> None:
        """Register NEW message-history entries as blocks (universe add-only).

        The LLM client rebuilds the history list every call (litellm composes
        a fresh list), so object identity does NOT survive a turn — blocks are
        matched by CONTENT FINGERPRINT (uniqueness guard: identical text maps
        to one block). An entry showing a baked view artifact (``[block bN
        ...`` marker) is RE-BOUND to its block instead of re-registered: the
        rendered copy must not grow a second block or the view would fight
        its own output. Blocks carry their latest msg_index for rendering."""
        for i, m in enumerate(msgs):
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, str) or not content:
                continue  # tool-call carriers etc. — no text to manage
            bid = _parse_view_marker(content)
            if bid is not None and bid in self.universe.blocks:
                self.universe.get(bid).msg_index = i  # re-bind, not re-register
                continue
            fp = _fingerprint(content)
            known = self._by_fp.get(fp)
            if known is not None and known in self.universe.blocks:
                self.universe.get(known).msg_index = i
                continue
            role = str(m.get("role", "user"))
            kind = _KIND_FOR_ROLE.get(role, "turn_output")
            block = self.universe.register(kind, content, self.turn, source=role)
            block.msg_index = i
            self._by_fp[fp] = block.block_id

    def register_note(self, text: str) -> Tuple[Optional[str], str]:
        """Scratchpad note write: the note block is auto-pinned (never dropped)."""
        self.note_seq += 1
        note_id = f"n{self.note_seq}"
        text = str(text or "")
        if len(text) > self.note_cap_chars:
            return None, f"note exceeds cap ({self.note_cap_chars})"
        self.notes[note_id] = text
        block = self.universe.register(
            "note", f"{_NOTE_PREFIX} {note_id}\n{text}", self.turn,
            source=f"note:{note_id}", pinned=True)
        return block.block_id, note_id

    def register_note(self, text: str) -> Tuple[Optional[str], str]:
        """Scratchpad note write: the note block is auto-pinned (never dropped)."""
        self.note_seq += 1
        note_id = f"n{self.note_seq}"
        text = str(text or "")
        if len(text) > self.note_cap_chars:
            return None, f"note exceeds cap ({self.note_cap_chars})"
        self.notes[note_id] = text
        block = self.universe.register(
            "note", f"{_NOTE_PREFIX} {note_id}\n{text}", self.turn,
            source=f"note:{note_id}", pinned=True)
        return block.block_id, note_id

    # -------------------------------------------------------------- the seam
    def assemble_turn(self, msgs: List[Dict[str, Any]],
                      outgoing: str,
                      tokens_used: int = 0,
                      budget: Optional[int] = None
                      ) -> Tuple[List[Dict[str, Any]], str]:
        """The chat-loop contract: assemble ONE LLM call.

        ``msgs`` is the canonical history (raw dicts), ``outgoing`` the raw
        user text for this call. Returns ``(history_to_send, msg_to_send)``.
        The outgoing text is registered as a block (the current turn) but is
        NEVER substituted (the active instruction must not be mangled) except
        the dedup form, which elides the repeated per-turn header.
        ``passthrough`` returns ``(msgs, outgoing)`` unchanged."""
        self.turn += 1
        self.register_history(msgs)
        out_block = self._register_outgoing(outgoing)
        if self.mode == "passthrough":
            self._emit_view(0)
            return msgs, outgoing

        records: List[Dict[str, Any]] = []
        bc = self._budget_checkpoint(tokens_used, budget)
        if bc is not None:
            records.append(bc)
        records += self._window_defaults()
        if self.system_prefix:
            records += self._dedup_prefix(msgs)
        if self.mode == "compact_conservative":
            records += self._conservative_compaction()
        if self.mode == "custom":
            records += self._run_policy()

        saved = self._count(records)
        self._emit_view(saved)
        send_history = self._render(msgs)
        send_msg = outgoing
        if out_block is not None \
                and self.view.form_of(out_block.block_id) == "dedup":
            # the outgoing turn carries the per-turn header; elide it exactly
            # like an old turn (same frozen form, anchored at the head)
            if self.system_prefix and outgoing.startswith(self.system_prefix):
                send_msg = _DEDUP_MARKER + outgoing[len(self.system_prefix):]
        return send_history, send_msg

    def _register_outgoing(self, outgoing: str) -> Optional["Block"]:
        if not isinstance(outgoing, str) or not outgoing:
            return None
        return self.universe.register("turn_output", outgoing, self.turn,
                                      source="user")

    def assemble(self, msgs: List[Dict[str, Any]],
                 tokens_used: int = 0,
                 budget: Optional[int] = None) -> List[Dict[str, Any]]:
        """Direct-form seam (tests / non-chat callers): render the history
        only; passthrough returns ``msgs`` unchanged."""
        if self.mode == "passthrough":
            self.turn += 1
            self.register_history(msgs)
            self._emit_view(0)
            return msgs
        send_history, _msg = self.assemble_turn(msgs, "", tokens_used=tokens_used,
                                                budget=budget)
        return send_history

    def set_system_prefix(self, prefix: str) -> None:
        """The per-turn system prompt (tool-use header) — the dedup op's
        target. Set once by the chat loop when the session starts."""
        self.system_prefix = str(prefix or "")

    def activate(self) -> Any:
        """Bind this session for ctx_* tools (contextvar; thread-local)."""
        from .state import activate
        return activate(self)

    @staticmethod
    def deactivate(token: Any) -> None:
        from .state import deactivate
        deactivate(token)

    # -- fixed pre-segment -------------------------------------------------------
    def _budget_checkpoint(self, tokens_used: int,
                           budget: Optional[int]) -> Optional[Dict[str, Any]]:
        """Advisory warning at >=80% of an active token budget (the C8 soft
        stop stays the enforcement point; this only informs)."""
        used = tokens_used
        if not used and self.budget_reader is not None:
            used = self.budget_snapshot().get("tokens_used") or 0
        if not isinstance(budget, int) or budget <= 0 \
                or not isinstance(used, int) or used <= 0:
            return None
        if used < _WARN_RATIO * budget:
            return None
        self._emit({"type": "cm_budget_checkpoint", "turn": self.turn,
                    "used": used, "budget": budget})
        return {"op": "budget_checkpoint", "ok": True, "source": "pre"}

    def _window_defaults(self) -> List[Dict[str, Any]]:
        """slice/compact modes: keep the newest ``window_k`` assistant turns
        FULL (plus pinned blocks + notes — pin/note protection lives in the
        primitives), older turn/tool blocks -> placeholder. Drop-floor
        guarded: small blocks stay (a placeholder would cost more)."""
        out: List[Dict[str, Any]] = []
        blocks = [b for b in self.universe.ordered()
                  if b.kind in ("turn_output", "tool_output")]
        rounds = 0
        for block in reversed(blocks):
            if block.kind == "turn_output":
                rounds += 1
            if rounds <= max(self.window_k, WINDOW_K_FLOOR):
                continue
            if self.view.form_of(block.block_id) != "full":
                continue  # already substituted in an earlier turn (sticky)
            if block.chars < self.drop_floor_chars:
                continue
            out.append(ops.drop(self.universe, self.view, block.block_id,
                                f"window_k={self.window_k}",
                                self.drop_floor_chars))
        return out

    def _conservative_compaction(self) -> List[Dict[str, Any]]:
        """compact_conservative adds: tool_outputs older than the pin window
        whose excerpt actually saves >= MIN_COMPACT_SAVE chars."""
        out: List[Dict[str, Any]] = []
        rounds = 0
        for block in reversed([b for b in self.universe.ordered()
                               if b.kind in ("turn_output", "tool_output")]):
            if block.kind == "turn_output":
                rounds += 1
                continue
            if rounds <= max(self.window_k, WINDOW_K_FLOOR):
                continue
            if self.view.form_of(block.block_id) != "full":
                continue
            out.append(ops.compact(self.universe, self.view,
                                   [block.block_id], "first_last",
                                   self.drop_floor_chars))
        return out

    def budget_snapshot(self, tokens_used: int = 0,
                        budget: Optional[int] = None) -> Dict[str, Any]:
        return ops.budget_query(self.budget_reader, tokens_used, budget)

    # -- policy segment -----------------------------------------------------------
    def _run_policy(self) -> List[Dict[str, Any]]:
        """Run the custom policy module (fail-open; wedge breaker)."""
        if self.policy_disabled:
            return [{"op": "policy", "ok": False,
                     "error": "policy disabled for this session"}]
        from .failopen import run_policy
        handle = ViewHandle(self, "policy")
        try:
            records, over_ms = run_policy(self.policy_path, handle,
                                          self.policy_exec_ms)
        except Exception:  # noqa: BLE001 -- fail-open, never break the session
            self.policy_failures += 1
            self._policy_strikes += 1
            if self._policy_strikes >= _POLICY_STRIKES:
                self.policy_disabled = True
                self._emit({"type": "cm_policy_disabled", "turn": self.turn,
                            "strikes": self._policy_strikes})
            return [{"op": "policy", "ok": False, "error": "policy failed"}]
        if over_ms:
            self._emit({"type": "cm_policy_overrun", "turn": self.turn,
                        "over_ms": over_ms})
        if not isinstance(records, list):
            return [{"op": "policy", "ok": False,
                     "error": "policy returned a non-list"}]
        self._policy_strikes = 0
        return records

    # -- post segment: audit the applied records -----------------------------------
    def _count(self, records: List[Dict[str, Any]]) -> int:
        """The primitives already validated + mutated the view; this counts,
        emits violations, and sums the estimated savings (audit only — the
        ledger keeps 实付; savings are a derived indicator, never billing)."""
        saved_total = 0
        capped = records[:max(1, self.max_ops_per_turn)]
        for rec in capped:
            self.ops_total += 1
            if rec.get("op") == "budget_checkpoint":
                self.ops_applied += 1
                continue
            if not rec.get("ok"):
                if not rec.get("_audited"):
                    self.ops_refused += 1
                    self.violations += 1
                    self._emit({"type": "cm_violation", "turn": self.turn,
                                "op": rec.get("op"), "refs": rec.get("refs"),
                                "error": rec.get("error")})
                # already counted + emitted by the handle audit hook
                continue
            saved = int(rec.get("saved") or 0)
            self.ops_applied += 1
            self.est_chars_saved += saved
            saved_total += saved
        self.ops_refused += max(0, len(records) - len(capped))
        return saved_total

    def _dedup_prefix(self, msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fixed pre-segment: fixed-overhead governance. The tool-use header
        is re-sent in EVERY user message; for messages older than the pin
        window it is elided (one in-band marker replaces it) — the newest
        window keeps the full header so tool behavior is unaffected. This is
        a form, not a drop: no content is lost except the repeated prefix."""
        if not self.system_prefix:
            return []
        out: List[Dict[str, Any]] = []
        blocks = [b for b in self.universe.ordered()
                  if b.kind == "turn_output"]
        rounds = 0
        for block in reversed(blocks):
            if block.kind == "turn_output":
                rounds += 1
            if rounds <= max(self.window_k, WINDOW_K_FLOOR):
                continue
            if self.view.form_of(block.block_id) != "full":
                continue
            if not block.text.startswith(self.system_prefix):
                continue
            if len(block.text) - len(self.system_prefix) < self.drop_floor_chars:
                continue
            self.view.set(block.block_id, "dedup", "system_prefix")
            out.append({"op": "dedup", "refs": [block.block_id], "ok": True,
                        "rule": "system_prefix",
                        "saved": len(self.system_prefix)})
        return out

    def _render(self, msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Materialize the CURRENT persistent view into shallow copies.

        A block's text is only ever replaced by its own excerpt/head_tail cut
        or the frozen placeholder (or the metadata template) — the view
        cannot fabricate content (§5.2 不变量 1). Idempotent: an entry whose
        content already carries the block's baked artifact is left alone."""
        by_msg: Dict[int, List[Block]] = {}
        for block in self.universe.ordered():
            form = self.view.form_of(block.block_id)
            if form == "full" or block.msg_index is None:
                continue
            by_msg.setdefault(block.msg_index, []).append(block)
        if not by_msg:
            return msgs
        out = list(msgs)
        for idx, blocks in by_msg.items():
            if idx < 0 or idx >= len(out):
                continue
            m = out[idx]
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, str):
                continue
            new_content = content
            for block in blocks:
                form = self.view.form_of(block.block_id)
                rule = self.view.rules.get(block.block_id, "")
                if f"[block {block.block_id} " in content:
                    continue  # already baked in an earlier turn (idempotent)
                if form == "placeholder":
                    new_text = placeholder(block, rule or "view")
                elif form in ("excerpt", "head_tail"):
                    keep = EXCERPT_KEEP if form == "excerpt" else HEAD_TAIL_KEEP
                    new_text = (cut(block.text, keep)
                                + f"\n[block {block.block_id} {form}: {rule or 'view'}]")
                elif form == "dedup":
                    if self.system_prefix and new_content.startswith(self.system_prefix):
                        new_text = _DEDUP_MARKER + new_content[len(self.system_prefix):]
                    else:
                        continue
                else:  # template
                    meta = block.to_meta()
                    new_text = (f"[block {block.block_id} template: "
                                f"kind={meta['kind']}, chars={meta['chars']}, "
                                f"source={meta['source']}]")
                if form == "dedup":
                    new_content = new_text  # prefix elision (anchored at head)
                elif block.text and block.text in new_content:
                    new_content = new_content.replace(block.text, new_text, 1)
                else:
                    # the span drifted (result embedded with decorations): a
                    # whole-message placeholder keeps the view honest without
                    # fabricating anything
                    new_content = placeholder(block, f"span-drift:{rule or form}")
                    break
            if new_content != content:
                shallow = dict(m)
                shallow["content"] = new_content
                out[idx] = shallow
        return out

    def note_violation(self, rec: Dict[str, Any]) -> None:
        """Count + emit one op refusal (idempotent with the seam path: the
        seam's ``_count`` only emits for records that did NOT come from a
        handle audit — tracked via the record marker below)."""
        if rec.get("_audited"):
            return
        rec["_audited"] = True
        self.violations += 1
        self.ops_refused += 1
        self._emit({"type": "cm_violation", "turn": self.turn,
                    "op": rec.get("op"), "refs": rec.get("refs"),
                    "error": rec.get("error")})

    def _emit_view(self, saved: int) -> None:
        self._emit({"type": "cm_view", "turn": self.turn, "mode": self.mode,
                    "stats": {"blocks": len(self.universe.blocks),
                              "substituted": len(self.view.forms),
                              "saved_chars": saved,
                              "est_saved_session": self.est_chars_saved,
                              "policy_disabled": self.policy_disabled}})


def build_session(keys: Dict[str, Any], *, emit: Optional[Callable] = None,
                  budget_reader: Optional[Callable] = None,
                  policy_path: Optional[str] = None) -> CMSession:
    """Factory: already-clamped effective keys -> a ready session. Unknown or
    out-of-range values are IGNORED here (the clamp authority reported them
    parent-side); the safe defaults are the passthrough posture."""
    session = CMSession(emit=emit, budget_reader=budget_reader,
                        policy_path=policy_path)
    session.apply_keys(keys or {})
    return session
