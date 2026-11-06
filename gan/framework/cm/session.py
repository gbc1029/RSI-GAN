"""CM session: per-chat-run Universe/View + the frozen primitive surface.

The seam: every LLM call passes the session's message history through
:meth:`CMSession.assemble_turn` — a DISPATCHER that resolves the session's
assembly (``cm/assembly.py``: the baseline presets, one per ``cm_mode``)
and hands the turn to it. The assembly is pure orchestration; the rules
live in the PRIMITIVES below, each of which validates before it mutates
(拒绝即不动) and owns its own invariant:

    register_history / register_outgoing   block registration (fingerprint
        uniqueness, view-marker re-bind, first-user pin; the outgoing turn
        is registered but NEVER substituted except the dedup elision)
    materialize_traj                       the seat's visible trajectory
        files as pinned traj_seg blocks (one scan per session; the read
        tools' index face and the ctx faces share the same address space)
    budget_checkpoint                      advisory >=80% warning (emit-only)
    conservative_compaction / window_defaults / dedup_prefix   the stage ops
        (validate via the ``cm.ops`` vocabulary; pre-check pinned / floor /
        sticky so a stage's predictable refusal never becomes violation noise)
    run_policy                             the custom policy hook (fail-open,
        execution budget, one-strike wedge breaker — docs/19 §12.17)
    finalize_turn                          the post segment: count_ops +
        emit_view (audit completeness) + render (no-fabrication) + the
        outgoing dedup contract
    render / count_ops / emit_view / note_violation                   audit
        and no-fabrication primitives the post segment composes
    call_method                            the assembly-face entry to the
        session's registered custom methods — same-level, same-callee: the
        method runs with a policy-face handle whose budget resets each turn

This surface is FROZEN: the baseline presets are named compositions
over exactly these methods, and the named agent assemblies resolve
through the same contract — an assembly can reorder or extend stages, but every
mutation still passes a validating primitive, so pipeline freedom cannot
buy rule evasion. View forms PERSIST across turns (a block compacted last
turn stays compacted; ``fetch`` restores it).

The canonical history is never mutated: substitutions are applied to shallow
copies, so the raw trajectory rows stay the untouched audit face (双记).
Effective key values are injected ALREADY CLAMPED by the frozen
``gan.framework.cost_keys`` authority — this module never clamps, it only
type-guards (the harness pass-through posture).
"""
from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import assembly
from . import ops
from .blocks import (Block, Universe, View, bake, form_saving,
                       HEAD_TAIL_KEEP, HEAD_TAIL_KEEP_MAX, MIN_COMPACT_SAVE)

# Framework floor (docs/10 §5.2.3: K 是带框架下限的键) — the clamping
# authority owns the ranges; this is the hard invariant this layer adds.
WINDOW_K_FLOOR = 1
_NOTE_PREFIX = "[gan:note]"
_WARN_RATIO = 0.8      # budget_checkpoint warns at 80% of an active budget
# Agent-owned CM modules (assembly / policy) share ONE strike rule (docs/19
# §12.17): the module is load-once per session, so a retry of the same bytes
# is deterministic waste — and a timed-out retry leaks ANOTHER worker thread.
# One failure (load crash, load timeout, or a run-phase failure) latches the
# module for the session; the CROSS-session fingerprint latch owns the
# "a fixed module deserves one fresh try" decision. The success-resets-
# strikes rule died with the per-turn policy reload.
_AGENT_MODULE_STRIKES = 1
_KIND_FOR_ROLE = {"tool": "tool_output"}
# view artifacts carry this marker prefix ("[block <id> " — the match is
# form-agnostic; legacy excerpt lines from pre-§12.21 histories still
# re-bind) — history entries showing it are re-bound to their block, never
# re-registered as new content (the LLM client rebuilds the history list on
# every call, so object identity does not survive a turn).
_VIEW_MARKER_PREFIX = "[block "
_DEDUP_MARKER = "[gan:system prompt deduplicated]"

# Framework-owned thread names in a seat child: the tool executor keeps the
# stdlib default (``ThreadPoolExecutor-*``) and the CM exec pools are
# explicitly named ``cm_exec`` (assembly/failopen runners). Anything else
# alive is either a module-SPAWNED thread (agent code) or another framework
# subsystem — the session-build-baselined census of this set is the leak
# signal for self-spawned threads; exact exec-pool leaks are counted
# separately (``thread_leaks``, timeout-driven). Advisory in in-process
# mode (parallel sessions share one process census), authoritative in the
# seat child (one session's census per worker process).
_FOREIGN_THREAD_EXCLUDE = ("ThreadPoolExecutor", "cm_exec")


def _foreign_thread_count() -> int:
    n = 0
    for t in threading.enumerate():
        if not t.is_alive() or t is threading.main_thread():
            continue
        if t.name.startswith(_FOREIGN_THREAD_EXCLUDE):
            continue
        n += 1
    return n


class _ModuleLoad:
    """Load-once state for ONE agent-owned CM module (assembly / policy).

    Shared machinery (docs/19 §12.17): the module is loaded ONCE per
    session, under the SAME exec budget as its execution — module-level
    code is agent code, so a top-level hang is a load-phase timeout and a
    top-level crash is a load-phase failure; BOTH are strike-counted
    (byte-identical retries are deterministic waste). The cache holds
    FAILURE too: a mid-session edit must not change the seam. The cache
    holds the MODULE, never a run result — apply/assemble still execute
    every turn.

    ``loader(path, exec_ms)`` is the side's timed loader; it raises the
    side's ``timeout_exc`` on budget overrun and lets load crashes
    propagate — the classification (crash vs timeout) is what the session
    strikes on, uniformly for both kinds."""

    __slots__ = ("kind", "path", "loader", "timeout_exc",
                 "result", "failed", "error", "timed_out", "exc")

    def __init__(self, kind: str, path: Optional[str], loader: Callable,
                 timeout_exc: type) -> None:
        self.kind = kind
        self.path = path
        self.loader = loader
        self.timeout_exc = timeout_exc
        self.result: Optional[Tuple[Any, Dict[str, Callable], List[str]]] = None
        self.failed = False
        self.error = ""
        self.timed_out = False
        self.exc: Optional[BaseException] = None

    @property
    def armed(self) -> bool:
        return self.path is not None

    def ensure(self, exec_ms: int
               ) -> Optional[Tuple[Any, Dict[str, Callable], List[str]]]:
        """Load once (timed). Returns the loader result, or None with
        ``failed``/``error``/``timed_out`` set (the failure is cached)."""
        if self.failed:
            return None
        if self.result is not None:
            return self.result
        try:
            self.result = self.loader(self.path, exec_ms)
        except self.timeout_exc as e:
            self.failed = True
            self.timed_out = True
            self.error = ops.clip_diag(str(e), 160)
            self.exc = e   # carries candidate_thread (docs/19 §12.31)
        except Exception as e:  # noqa: BLE001 -- classified by the caller
            self.failed = True
            self.error = ops.clip_diag(str(e), 160)
        return self.result


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


def _msg_text(m: Any) -> Optional[str]:
    """The text of one history entry, in EITHER key convention.

    This repo's canonical history is ``text``-keyed (``agent/llm.py`` converts
    content->text on return; ``task_agent`` reads ``['text']``) while the
    seam's user-turn rebuild in the chat loop writes ``content`` — the binding
    must not depend on which form a given entry happens to carry (docs/10
    §11.5 erratum: the seam previously read ``content`` only and silently
    failed to bind the canonical history)."""
    if not isinstance(m, dict):
        return None
    v = m.get("content")
    if isinstance(v, str) and v:
        return v
    v = m.get("text")
    if isinstance(v, str) and v:
        return v
    return None


_LEGAL_ROLES = ("system", "user", "assistant", "tool")


def _validate_sent_history(history: Any) -> Optional[str]:
    """P0 (docs/19 §12.14): the SENT history must be a list of message dicts.

    The shape check alone (list) let ``[None]`` reach the LLM payload while
    ``assembly_failures`` stayed 0 — a silent contract break that surfaces as
    a confusing "model/task quality" failure. Element-level validation turns
    it into the same loud fallback a crash gets."""
    if not isinstance(history, list):
        return f"sent history is {type(history).__name__}, not a list"
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            return f"history[{i}] is {type(entry).__name__}, not a message dict"
        role = entry.get("role")
        if not isinstance(role, str) or not role:
            return f"history[{i}] carries no role"
        if role not in _LEGAL_ROLES:
            return f"history[{i}] role {role!r} is not a chat role"
        for key in ("text", "content"):
            if key in entry and entry[key] is not None \
                    and not isinstance(entry[key], str):
                return (f"history[{i}][{key!r}] is {type(entry[key]).__name__}, "
                        f"not text")
    return None


class _ViewRead:
    """READ-ONLY projection of the persistent view (P2 portal surface).

    Authors may inspect the view; they may not write it. ``set`` simply does
    not exist here (the presets keep the real object)."""

    __slots__ = ("_v",)

    def __init__(self, view: Any):
        object.__setattr__(self, "_v", view)

    def form_of(self, block_id: str) -> str:
        return self._v.form_of(block_id)

    @property
    def forms(self) -> Dict[str, str]:
        return dict(self._v.forms)

    @property
    def rules(self) -> Dict[str, str]:
        return dict(self._v.rules)

    def __getattr__(self, name: str) -> Any:
        from .assembly import AssemblyError
        raise AssemblyError(
            f"view.{name} is not available on the assembly surface: view is a "
            f"READ-ONLY projection (use the primitives to change it)")

    def __setattr__(self, name: str, value: Any) -> None:
        from .assembly import AssemblyError
        raise AssemblyError("the view is READ-ONLY for assemblies")


class _UniverseRead:
    """READ-ONLY projection of the block universe (P2 portal surface).

    ``get``/``ordered``/iteration return DEEP copies: a block object carries
    mutable fields (``pinned``, ``msg_index``) AND a mutable container
    (``sources`` — derived lineage); a shallow copy shares that list, so
    ``get(...).sources.append(...)`` would write straight through the
    read-only facade (N5c, docs/19 §12.32)."""

    __slots__ = ("_u",)

    def __init__(self, universe: Any):
        object.__setattr__(self, "_u", universe)

    def get(self, block_id: str) -> Any:
        block = self._u.blocks.get(str(block_id))
        return copy.deepcopy(block) if block is not None else None

    def ordered(self) -> List[Any]:
        return [copy.deepcopy(b) for b in self._u.ordered()]

    def __iter__(self):
        return iter(self.ordered())

    def __len__(self) -> int:
        return len(self._u.blocks)

    def __getattr__(self, name: str) -> Any:
        from .assembly import AssemblyError
        raise AssemblyError(
            f"universe.{name} is not available on the assembly surface: "
            f"universe is a READ-ONLY projection (blocks are framework-owned)")

    def __setattr__(self, name: str, value: Any) -> None:
        from .assembly import AssemblyError
        raise AssemblyError("the block universe is READ-ONLY for assemblies")


class SessionPortal:
    """P2 (docs/19 §12.14): the object an AGENT-OWNED assembly receives.

    The published authoring surface is the session's PRIMITIVE METHODS
    (``cm/assembly.py`` composes exactly those) plus the refs fold
    (``record_refs`` — N2 batch, docs/19 §12.33: the baseline pipeline
    performs the same early fold before ``run_policy``, so the authoring
    surface must be able to express it too) plus clamped parameters as
    READ-ONLY values. Everything else — the mutable ``view``/``universe``,
    the audit post segment (``emit_view``/``count_ops``), the internal
    bookkeeping — is refused here, so "it cannot bypass the rules" becomes
    true by construction instead of by convention. The parameter NAME stays
    ``session`` (signature unchanged); only the object narrows.

    Escape (``type(portal)``, a captured closure) is still possible — that is
    the malicious tier, and the P1 turn reconciliation backstops it.
    """

    __slots__ = ("_s",)

    _METHODS = frozenset({
        "register_history", "register_outgoing", "budget_checkpoint",
        "window_defaults", "conservative_compaction", "dedup_prefix",
        "run_policy", "call_method", "finalize_turn", "render",
        "record_refs",
    })
    _PARAMS = frozenset({
        "mode", "system_prefix", "window_k", "head_tail_keep",
        "drop_floor_chars",
        "min_compact_save", "note_cap_chars", "max_ops_per_turn",
        "assembly_exec_ms", "policy_exec_ms", "turn",
    })

    def __init__(self, session: Any):
        object.__setattr__(self, "_s", session)

    def __getattr__(self, name: str) -> Any:
        from .assembly import AssemblyError
        if name in self._METHODS:
            return getattr(self._s, name)
        if name in self._PARAMS:
            return getattr(self._s, name)
        if name == "view":
            return _ViewRead(self._s.view)
        if name == "universe":
            return _UniverseRead(self._s.universe)
        raise AssemblyError(
            f"{name!r} is not part of the assembly surface "
            f"(allowed methods: {sorted(self._METHODS)}; "
            f"read-only parameters: {sorted(self._PARAMS)}; "
            f"view/universe are read-only projections)")

    def __setattr__(self, name: str, value: Any) -> None:
        from .assembly import AssemblyError
        raise AssemblyError(
            f"{name!r} is read-only on the assembly surface (clamping is "
            f"framework-owned)")


class ViewHandle:
    """The primitive facade handed to BOTH faces (agent ctx_* tools and the
    assembly-time custom policy). Face membership decides what is callable
    (ops.AGENT_FACES / ops.POLICY_FACES); the policy face carries the
    per-turn op budget (``cm_max_ops_per_turn``). Every op returns a
    structured record that never carries block content (peek/note reads are
    the explicit content requests).

    The policy budget is an ATTEMPTS quota (ruled, docs/19 §12.39.4 R7b):
    READ ops draw from the same pool as writes; a refused attempt counts
    and is NOT refunded (the decrement precedes the primitive's
    validation); exhaustion and parameter errors record as violation
    events — DISPLAY-ONLY (the session counter and the cost line's
    aggregate): no strike, latch or disable reads them (those consume the
    reconcile's return and the ``cm_*_failed`` counters). The agent face
    carries no budget."""

    def __init__(self, session: "CMSession", face: str):
        self._s = session
        self.face = face
        self._budget: Optional[int] = (
            session.max_ops_per_turn if face == "policy" else None)

    def _audit(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Policy-face refusals are violations wherever they happen (the
        wedge breaker counts them via the session, not only in the seam).

        Event vocabulary (docs/19 §12.39.4, R7b): THIS path emits the
        generic ``cm_violation`` — the OP-REFUSAL family (an attempt that
        did NOT take effect). The reconcile's detections are a different
        family — contract breaches by an effect that DID happen — emitted
        per-subsystem as ``cm_assembly_violation``/``cm_policy_violation``
        (§12.17.1, N5a) by ``_note_violations``. Two concepts, not two
        encodings: a scanner counting a session's refusals must subscribe
        to both, and the cost line's ``cm_violations`` aggregate counts
        ONLY this op-refusal family (declared half-count; widening it is
        a recorded pending option, not done).

        Successful MUTATING ops also publish their refs to the session's
        recorded-refs set: the P1 turn reconciliation must be able to explain
        a non-full form even when the caller forgot to thread the record
        through ``finalize_turn`` (or when the op happened inside a custom
        METHOD, whose own record carries no refs).

        Agent-face ok records ALSO land their ``saved`` delta in the
        session's net accounting (docs/19 §12.37, R4): agent-face records
        never flow through ``finalize_turn``/``count_ops`` (the only other
        accumulator), so the handle is where an agent trim credits and a
        full restore refunds. Policy/assembly records are counted by
        ``count_ops`` — accumulating here too would double-count.

        ``saved`` is a TRANSITION number (docs/19 §12.40, F2): the handle
        snapshots the block's previous form BEFORE calling the primitive and
        fills the record through the session's single accounting funnel
        (``_book``); primitives no longer carry a block-local "vs full"
        number, so a repeated / no-op / cross-form op can never double-book.
        """
        if rec.get("ok"):
            refs = rec.get("refs") or []
            if refs:
                self._s._recorded_refs.update(str(r) for r in refs)
            if self.face == "agent" and "saved" in rec:
                self._s.est_chars_saved += int(rec.get("saved") or 0)
            return rec
        if self.face == "policy":
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
    def index(self, kinds: Optional[List[str]] = None,
              offset: int = 0, cap: Optional[int] = None) -> Dict[str, Any]:
        if err := self._gate("index"):
            return err
        return ops.index_blocks(self._s.universe, kinds,
                                cap=ops.INDEX_CAP_DEFAULT if cap is None else cap,
                                offset=offset)

    def peek(self, block_id: str) -> Dict[str, Any]:
        if err := self._gate("peek"):
            return err
        return ops.peek(self._s.universe, block_id)

    def view_state(self) -> Dict[str, Any]:
        if err := self._gate("view_state"):
            return err
        return ops.view_state(self._s.universe, self._s.view,
                              head_tail_keep=self._s.head_tail_keep)

    def usage_query(self, tokens_used: Optional[int] = None,
                     budget: Optional[int] = None) -> Dict[str, Any]:
        if err := self._gate("usage_query"):
            return err
        return self._s.usage_snapshot(tokens_used, budget)

    # -- mutations (ops validate first; view mutates only on success) ---------
    def fetch(self, block_id: str, form: str = "full") -> Dict[str, Any]:
        if err := self._gate("fetch"):
            return err
        # Agent-face fetch verb contract (ops.AGENT_FETCH_FORMS — the same
        # constant the ctx_fetch schema enum is built from): materialize
        # (full) or trim (head_tail). The eviction forms are the
        # assembly/policy layer's, and the agent tool face has NO turn-end
        # reconcile — an out-of-contract form is refused HERE rather than
        # left to a backstop. The policy face's fetch(non-full) shrink verb
        # is deliberately untouched.
        if self.face == "agent" and form not in ops.AGENT_FETCH_FORMS:
            return self._audit({
                "op": "fetch", "refs": [block_id], "ok": False,
                "error": (f"invalid form {form!r} on the agent face "
                          f"(allowed: {', '.join(ops.AGENT_FETCH_FORMS)})")})
        prev = self._s._pre_state(block_id)
        rec = ops.fetch(self._s.universe, self._s.view, block_id,
                        form, keep=self._s.head_tail_keep)
        # saved is the session's transition delta — the SAME funnel every
        # mutation uses (docs/19 §12.40, F2). The former dedup-only fixup
        # (C.3, §12.39.5) is subsumed: ``_book`` knows every anchored form.
        self._s._book(rec, block_id, prev)
        return self._audit(rec)

    def pin(self, block_id: str, note: str = "") -> Dict[str, Any]:
        """Pin/unpin. PINNING A NOT-YET-FULL BLOCK RESTORES ITS CONTENT.

        pinned's declared meaning is "content must not be lost" — the ONE
        exemption is ``dedup`` (it only elides the repeated system header;
        docs/10 "pin 的唯一豁免是 dedup"). A pin on an already-elided block
        used to set the flag and buy nothing: the block kept shipping as a
        placeholder AND the state violated the P1 ``pinned_form`` contract,
        which struck the NEXT healthy policy/assembly turn as a false
        contract break (docs/19 §12.40, F1). The pin now converges the
        block: a form outside {full, dedup} is restored to full (the
        transition's refund books through the accounting funnel, and the
        record self-reports it: ``restored`` / ``restored_from`` / ``chars``
        / ``saved``). ``dedup`` and ``full`` are left untouched.
        """
        if err := self._gate("pin"):
            return err
        prev = self._s._pre_state(block_id)
        rec = ops.pin(self._s.universe, self._s.view, block_id, True, note)
        blk = self._s.universe.get(block_id)
        if (rec.get("ok") and blk is not None
                and prev[0] not in ("full", "dedup")):
            saving = self._s._transition_saving(
                blk, prev[0], prev[1], "full", prev[1])
            ops.fetch(self._s.universe, self._s.view, block_id, "full",
                      keep=self._s.head_tail_keep)
            if rec.get("note_stored"):
                # the restore wrote its own rule tag; the pin annotation is
                # the durable read-back, so it wins on a restored block
                self._s.view.rules[block_id] = "pin:" + str(rec["note_stored"])
            else:
                # no annotation: a FULL block needs no rule tag (the eviction
                # reason that applied before the restore lives in the earlier
                # cm_view event; leaving "fetch" on a full block is noise)
                self._s.view.rules.pop(block_id, None)
            rec["restored"] = True
            rec["restored_from"] = prev[0]
            rec["chars"] = blk.chars
            rec["saved"] = saving
        return self._audit(rec)

    def unpin(self, block_id: str) -> Dict[str, Any]:
        if err := self._gate("unpin"):
            return err
        return self._audit(ops.pin(self._s.universe, self._s.view, block_id, False))

    def drop(self, block_id: str, rule: str = "policy") -> Dict[str, Any]:
        if err := self._gate("drop"):
            return err
        prev = self._s._pre_state(block_id)
        rec = ops.drop(self._s.universe, self._s.view, block_id, rule,
                       self._s.drop_floor_chars)
        self._s._book(rec, block_id, prev)
        return self._audit(rec)

    def compact(self, block_ids: List[str],
                strategy: str = "first_last") -> Dict[str, Any]:
        if err := self._gate("compact"):
            return err
        # snapshot EVERY block's (form, rule): the materializing strategies
        # change host/other sources AND re-point stale references, so the
        # exact delta is computed over all changed blocks after the call.
        prev_all = self._s._pre_state_all()
        rec = ops.compact(self._s.universe, self._s.view, list(block_ids),
                          strategy, self._s.drop_floor_chars, self._s.turn,
                          keep=self._s.head_tail_keep)
        if rec.get("ok"):
            rec["saved"] = self._s._delta_since(prev_all)
        return self._audit(rec)

    def substitute(self, block_id: str, form: str) -> Dict[str, Any]:
        if err := self._gate("substitute"):
            return err
        prev = self._s._pre_state(block_id)
        rec = ops.substitute(self._s.universe, self._s.view, block_id, form,
                             "policy", self._s.drop_floor_chars,
                             keep=self._s.head_tail_keep)
        self._s._book(rec, block_id, prev)
        return self._audit(rec)

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

    def call(self, name: str, **kwargs) -> Dict[str, Any]:
        """Invoke a registered custom method at the
        CALLER's privilege — the method receives THIS handle (policy face:
        the same op budget, continuous, with ``call`` itself costing one
        unit; agent face: agent-level ops only). Never raises: unknown
        name, crash, or a non-record return comes back as a refusal
        record; a policy-face failure also counts a violation."""
        if err := self._gate("call"):
            return err
        rec = self._s._invoke_method(name, kwargs, callee=self)
        if not rec.get("ok"):
            self._audit(rec)
        return rec


@dataclass
class CMSession:
    """One chat run's CM state (universe + view + config + audit counters).

    The per-turn pipeline lives in the ASSEMBLY (``cm/assembly.py``), not
    here: :meth:`assemble_turn` dispatches to the resolved assembly, which
    composes the frozen primitive methods above. The dispatch itself owns
    exactly two invariants — the turn counter advances once per call, and
    the resolution is cached (an unknown mode fails open to the slice
    preset with a loud ``cm_assembly_fallback`` event, never silently)."""
    mode: str = "passthrough"
    window_k: int = 4
    head_tail_keep: int = HEAD_TAIL_KEEP  # session-wide head_tail notch
    # (cost key cm_head_tail_keep; excerpt form retired — docs/19 §12.21)
    drop_floor_chars: int = 400
    max_ops_per_turn: int = 32
    policy_exec_ms: int = 200
    note_cap_chars: int = 8_000
    emit: Optional[Callable[[Dict[str, Any]], None]] = None
    usage_reader: Optional[Callable[[], Dict[str, Any]]] = None
    policy_path: Optional[str] = None  # custom mode: agent policy module path
    assembly_path: Optional[str] = None  # named agent-owned assembly module
    assembly_exec_ms: int = 200          # per-turn budget for that assembly
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
    # Cumulative SIGNED flow of booked management-op deltas: every mutation
    # books the TRANSITION width (prev rendered form → new rendered form,
    # docs/19 §12.40 F2), render-visible blocks only (⑤). It can decrease
    # (refunds: a restore widens the view again) and it CAN be negative
    # transiently — a shrink that widens a tiny block (C.1) or a cross-form
    # move books the true signed delta instead of a clamped 0. The STATE
    # quantity (current view net saving) remains independently observable
    # from the per-turn render deltas; consumers treat this as a flow, never
    # as billing.
    est_chars_saved: int = 0
    emit_failures: int = 0
    # CM-03 leak observability: an exec-budget timeout leaks its worker
    # thread (Python cannot kill threads). ``thread_leaks`` counts the
    # timed-out load/apply/assemble jobs this session produced; the
    # ``_thread_baseline`` census catches module-SPAWNED threads, which
    # never pass through an exec budget at all. Both surface in
    # ``summary()`` as CONDITIONAL fields (absence = clean) and drive the
    # seat proxy's post-session hygiene restart (docs/19 CM-03 residual).
    thread_leaks: int = 0
    # Band-① advisory counters (docs/19 §12.30.3/N1): a run that completed
    # INSIDE the wait deadline but over the wall budget. Advisory only —
    # the result is honored, never struck.
    assembly_overruns: int = 0
    policy_overruns: int = 0
    # the workers abandoned at T (candidate threads attached to the raised
    # timeout exceptions) — ``thread_leaks_live`` measures how many are
    # STILL alive at summary time (docs/19 §12.31)
    _timeout_workers: List[Any] = field(default_factory=list, repr=False,
                                        compare=False)
    _thread_baseline: int = field(default=0, repr=False, compare=False)
    _policy_strikes: int = 0
    _seen_user: bool = False   # first-user-turn default pin (docs/10 §5.2.3)
    # Named-assembly bookkeeping (audit fields materialize WITH the
    # subsystem they describe: a session without an assembly configured has
    # no claim to expose; one with a claim shows activity AND absence)
    assembly_active: bool = False    # the named assembly produced >= 1 turn
    assembly_failures: int = 0
    assembly_disabled: bool = False  # wedge-broken for the session
    assembly_violations: int = 0     # seam-contract violations this session
    policy_violations: int = 0       # policy-tail reconcile violations (N5a)
    _assembly_strikes: int = 0
    # P1 turn reconciliation (docs/19 §12.14): turn-start snapshot + the op
    # refs the post segment recorded. Named-assembly turns only (presets never
    # populate them: they cannot touch these surfaces).
    _turn_snapshot: Optional[Dict[str, Any]] = field(default=None, repr=False,
                                                     compare=False)
    # Forms are STICKY across turns, so a block substituted on turn 2 is still
    # non-full on turn 3 without a new record. The reconciliation therefore
    # needs the CUMULATIVE set of recorded refs, not just this turn's.
    _recorded_refs: set = field(default_factory=set, repr=False, compare=False)
    _assembly_last_error: Dict[str, Any] = field(default_factory=dict,
                                                 repr=False, compare=False)
    _portal_cache: Any = field(default=None, repr=False, compare=False)
    # load-once state, one instance per agent-owned CM module kind (docs/19
    # §12.17): caches the MODULE (success AND failure) — apply/assemble still
    # run every turn.
    _assembly_load: Optional["_ModuleLoad"] = field(default=None, repr=False,
                                                    compare=False)
    _policy_load: Optional["_ModuleLoad"] = field(default=None, repr=False,
                                                  compare=False)
    _policy_last_error: Dict[str, Any] = field(default_factory=dict,
                                               repr=False, compare=False)
    _assembly_ignored: bool = field(default=False, repr=False, compare=False)
    _turn_degraded: bool = field(default=False, repr=False, compare=False)
    _view_emitted_turn: Optional[int] = field(default=None, repr=False,
                                              compare=False)
    # identity-keyed bookkeeping: rewritten shallow copies are not re-registered
    _by_fp: Dict[str, str] = field(default_factory=dict)  # content fp -> bid
    # R5 backstop (docs/19 §12.38): each turn_output block's REGISTRATION
    # fingerprint, keyed by block_id — the reconcile's identity question
    # compares it against the block's CURRENT text (a live-object leak that
    # mutates block.text in place desynchronizes the two and is caught).
    _reg_fp: Dict[str, str] = field(default_factory=dict)
    # Custom methods: registered per session from the
    # named modules' METHODS export — same-level, same-callee surface. The
    # assembly's harvest registers first; the policy's merges at its first
    # successful load (shadowed names refuse, assembly > policy).
    methods_registry: Dict[str, Callable] = field(default_factory=dict)
    _methods_policy_done: bool = field(default=False, repr=False,
                                       compare=False)
    _method_face: Optional["ViewHandle"] = field(default=None, repr=False,
                                                 compare=False)
    # Trajectory materialization: the seat's visible trajectory files,
    # snapshotted at build time as pinned traj_seg blocks. ``traj_index`` is
    # the metadata payload the read tools render; ``traj_segments`` maps each
    # seg_id to its block so a fetch resolves through the SAME scan the index
    # came from (one address space for both CM faces).
    traj_index: Optional[Dict[str, Any]] = None
    traj_segments: Dict[str, str] = field(default_factory=dict)
    # resolved assembly (cache: mode is clamped upstream and never mutates)
    _assembly_fn: Optional[Callable] = field(default=None, repr=False,
                                             compare=False)

    # ------------------------------------------------------------------- config
    def __post_init__(self) -> None:
        """Anchor the foreign-thread census at session build (CM-03 leak
        observability; see ``_foreign_thread_count``)."""
        self._thread_baseline = _foreign_thread_count()

    def apply_keys(self, keys: Dict[str, Any]) -> None:
        """Type-guarded application of ALREADY-CLAMPED effective values."""
        mode = keys.get("cm_mode")
        if isinstance(mode, str) and mode in ("passthrough", "slice",
                                              "compact_conservative", "custom"):
            self.mode = mode
        k = keys.get("cm_window_k")
        if type(k) is int and k >= WINDOW_K_FLOOR:
            self.window_k = k
        v = keys.get("cm_head_tail_keep")
        if type(v) is int and 1 <= v <= HEAD_TAIL_KEEP_MAX:
            self.head_tail_keep = v
        v = keys.get("cm_drop_floor_chars")
        if type(v) is int and v >= 0:
            self.drop_floor_chars = v
        v = keys.get("cm_max_ops_per_turn")
        if type(v) is int and v >= 1:
            self.max_ops_per_turn = v
        v = keys.get("cm_policy_exec_ms")
        if type(v) is int and v >= 1:
            self.policy_exec_ms = v
        v = keys.get("cm_assembly_exec_ms")
        if type(v) is int and v >= 1:
            self.assembly_exec_ms = v
        v = keys.get("cm_note_cap_chars")
        if type(v) is int and v >= 1:
            self.note_cap_chars = v

    # ------------------------------------------------ sent-text + accounting
    # ONE source for a block's sent text (all forms, incl. the session-
    # anchored dedup / derived_host), and ONE source for an op's saving
    # (the TRANSITION from the block's previous rendered form). Render, the
    # marker re-bind and the audit funnel all consume these (docs/19 §12.40):
    # the pre-§12.40 split (bake() + block-local form_saving() + a dedup-only
    # session fixup) booked wrong deltas for repeated/no-op/cross-form ops
    # and for the derived_host restore.
    @staticmethod
    def _renders(block: "Block") -> bool:
        """⑤ RENDER-VISIBILITY caliber, single-sourced: a block books saving
        when it renders NOW (bound to the canonical history) or necessarily
        NEXT turn (the unbound outgoing turn_output — the fingerprint bind is
        guaranteed by the chat protocol). Notes / derived / traj_seg never
        render → never book."""
        return block.msg_index is not None or block.kind == "turn_output"

    def view_text(self, block: "Block", form: str, rule: str = "") -> str:
        """The text this block contributes to the SENT history in ``form``.

        context-free forms delegate to ``bake`` (the frozen formula source);
        the two SESSION-ANCHORED forms are resolved here:
        ``dedup`` — exactly the render's rewrite (head prefix → marker;
        a non-matching prefix is a no-effect, i.e. the full text);
        ``derived_host`` — the resolved derived text + the host suffix
        (the rule carries ``derived=<id>``; a missing host stays raw, the
        same defensive behavior render has).
        """
        rule = str(rule or "")
        if form == "full":
            return block.text
        if form == "dedup":
            if self.system_prefix and block.text.startswith(self.system_prefix):
                return _DEDUP_MARKER + block.text[len(self.system_prefix):]
            return block.text
        if form == "derived_host":
            bid = rule[len("derived="):] if rule.startswith("derived=") else None
            dblock = self.universe.get(bid) if bid else None
            if dblock is None:
                return block.text
            return bake(block, "derived_host", rule, host_text=dblock.text)
        return bake(block, form, rule, keep=self.head_tail_keep)

    def _transition_saving(self, block: "Block", prev_form: str, prev_rule: str,
                           new_form: str, new_rule: str) -> int:
        """Signed sent-width delta of one form transition (SIGNED: a move to
        a wider form refunds; small-block evictions may legitimately widen —
        the C.1 phenomenon is now visible instead of clamped, docs/19
        §12.40). 0 for never-rendering blocks (⑤)."""
        if not self._renders(block):
            return 0
        prev_form = str(prev_form or "full")
        new_form = str(new_form or "full")
        if prev_form == new_form and str(prev_rule or "") == str(new_rule or ""):
            return 0
        return (len(self.view_text(block, prev_form, prev_rule))
                - len(self.view_text(block, new_form, new_rule)))

    def _pre_state(self, block_id: str) -> Tuple[str, str]:
        """The (form, rule) snapshot taken BEFORE a mutation."""
        return (self.view.form_of(block_id),
                str(self.view.rules.get(block_id, "") or ""))

    def _pre_state_all(self) -> Dict[str, Tuple[str, str]]:
        return {b.block_id: self._pre_state(b.block_id)
                for b in self.universe.ordered()}

    def _book(self, rec: Dict[str, Any], block_id: str,
              prev: Tuple[str, str]) -> Dict[str, Any]:
        """THE accounting funnel: fill ``rec["saved"]`` from the transition.

        Only ok mutation records are booked (refusals carry no saving);
        the record keeps its other fields untouched. Every caller that
        mutates the view goes through this (ViewHandle + the stage methods),
        so there is exactly ONE place a saving number is produced.
        """
        if rec.get("ok"):
            block = self.universe.get(block_id)
            if block is not None:
                rec["saved"] = self._transition_saving(
                    block, prev[0], prev[1],
                    *self._pre_state(block_id))
        return rec

    def _delta_since(self, prev_all: Dict[str, Tuple[str, str]]) -> int:
        """Exact summed delta for a multi-block mutation (``compact``): every
        block whose (form, rule) changed since the snapshot contributes its
        transition width difference — this includes the stale-reference
        re-points the materializing strategies perform."""
        total = 0
        for block in self.universe.ordered():
            before = prev_all.get(block.block_id)
            if before is None:
                continue   # blocks the op registered have no prior view state
            now = self._pre_state(block.block_id)
            if before[0] == now[0] and before[1] == now[1]:
                continue
            total += self._transition_saving(block, before[0], before[1],
                                             now[0], now[1])
        return total

    def _substituted_blocks(self) -> int:
        """Blocks currently carrying a NON-FULL view form (docs/19 §12.44 D5).

        ``len(view.forms)`` counted FULL entries too: ``fetch(full)``/unpin/
        a restore writes an explicit ``"full"`` form, so the old number mixed
        "blocks addressed by the view" with "blocks compacted". One name, one
        caliber: the count of forms that actually differ from ``full``."""
        return sum(1 for form in self.view.forms.values() if form != "full")

    def summary(self) -> Dict[str, Any]:
        """Session-level CM summary (cost record / receipt ``cm`` node).

        The assembly fields materialize only when an assembly was CONFIGURED
        (the design claim creates the observability duty: a configured-but-
        never-active assembly must read as exactly that, None ≠ 0 style).
        Conditional audit fields keep the same rule: ``policy_violations`` /
        ``*_overruns`` / ``thread_leaks`` / ``thread_leaks_live`` /
        ``thread_leak_delta`` appear only when non-zero (absence = zero/clean),
        while ``assembly_*`` appear whenever an assembly was configured (they
        may legitimately read 0)."""
        out = {"mode": self.mode, "turns": self.turn,
               "blocks": len(self.universe.blocks),
               "ops_total": self.ops_total, "ops_applied": self.ops_applied,
               "ops_refused": self.ops_refused,
               "policy_failures": self.policy_failures,
               "policy_disabled": self.policy_disabled,
               # ``violations`` is the OP-REFUSAL export of the SAME counter
               # as ``ops_refused`` (both are incremented together at exactly
               # two sites: count_ops' unaudited branch and note_violation;
               # docs/19 §12.44 D4). Kept as an alias -- no third writer.
               "violations": self.violations,
               "substituted": self._substituted_blocks(),
               "est_chars_saved": self.est_chars_saved}
        if self.policy_violations:
            out["policy_violations"] = self.policy_violations
        if self.emit_failures:
            # Conditional audit field (absence = a healthy sink): a session
            # whose event sink dropped lines must not read as fully audited.
            out["emit_failures"] = self.emit_failures
        # CM-03 leak observability: conditional audit fields (absence =
        # clean). ``thread_leaks`` counts exec-budget timeouts whose worker
        # thread leaked (load/apply/assemble); ``thread_leak_delta`` catches
        # module-SPAWNED threads via the foreign-thread census (never passes
        # through an exec budget). Advisory in in-process mode, authoritative
        # in the seat child; both drive the seat proxy's post-session hygiene
        # restart.
        if self.thread_leaks:
            out["thread_leaks"] = self.thread_leaks
        delta = _foreign_thread_count() - self._thread_baseline
        if delta > 0:
            out["thread_leak_delta"] = delta
        if self.assembly_overruns:
            out["assembly_overruns"] = self.assembly_overruns
        if self.policy_overruns:
            out["policy_overruns"] = self.policy_overruns
        # The MEASURED leak signal (docs/19 §12.31): how many of the
        # workers abandoned at T are STILL alive. A timed-out worker may
        # finish normally after the wait was abandoned (band ② — the wait
        # deadline and the work's completion are different clocks); only
        # the candidates' liveness distinguishes that from a real hang.
        # ``thread_leaks`` above stays the historical prediction count and
        # drives nothing.
        live = sum(1 for t in self._timeout_workers
                   if t is not None and t.is_alive())
        if live:
            out["thread_leaks_live"] = live
        if self.assembly_path:
            out["assembly_active"] = self.assembly_active
            out["assembly_failures"] = self.assembly_failures
            out["assembly_disabled"] = self.assembly_disabled
            out["assembly_violations"] = self.assembly_violations
            # Actionable "fix this module" facts for the NEXT session (rl:
            # stem + where + reason). Absence is meaningful: the module has
            # never failed, so there is nothing to act on.
            if self._assembly_last_error or self.assembly_violations:
                out["assembly_report"] = {
                    "stem": os.path.basename(str(self.assembly_path)).split(".")[0],
                    "failures": self.assembly_failures,
                    "violations": self.assembly_violations,
                    "disabled": self.assembly_disabled,
                    "last_error": ops.clip_diag(str(self._assembly_last_error.get("error") or ""), 160),
                    "phase": self._assembly_last_error.get("phase"),
                    "where": str(self._assembly_last_error.get("where") or ""),
                }
        # policy_report (docs/19 §12.17): same contract as assembly_report —
        # actionable "fix this module" facts for the NEXT self-improve
        # session; absence = the policy has never failed.
        if self.policy_path and self._policy_last_error:
            out["policy_report"] = {
                "stem": os.path.basename(str(self.policy_path)).split(".")[0],
                "failures": self.policy_failures,
                "violations": self.policy_violations,
                "disabled": self.policy_disabled,
                "last_error": ops.clip_diag(str(self._policy_last_error.get("error") or ""), 160),
                "phase": self._policy_last_error.get("phase"),
                "where": str(self._policy_last_error.get("where") or ""),
            }
        if self.traj_index is not None:
            out["traj_segments"] = len(self.traj_segments)
        return out

    # ------------------------------------------------------------------- events
    def _emit(self, event: Dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event)
        except Exception:
            # The audit sink must never break the session — but a sink that
            # FAILS is a degradation the session must carry, not hide: the
            # counter surfaces in summary() (the cost record ``cm`` node and
            # the receipt), which is this module's durable channel exactly
            # when the event sink itself is the broken part. Recursing into
            # the broken sink to report the breakage is not an option.
            self.emit_failures += 1

    # ---------------------------------------------------- block registration
    def _require_msg_dicts(self, msgs: Any, where: str) -> None:
        """The msgs contract (docs/19 §12.32.4): every canonical-history
        entry is a framework-constructed dict — agent/llm.py wraps strings
        into dicts at the boundary (user + assistant entries), tool rounds
        ride user-turn text, and no producer ships a bare entry. The old
        tolerance (skip unknown shapes silently) was a booby trap: the P1
        snapshot's texts layer is dict-only COMPRESSED while the restore
        indexed it by list position, so ONE non-dict entry would shift
        every later restoration (silent canonical corruption). A violation
        is now LOUD: the offenders are emitted on the CM event stream and
        a TypeError aborts the call BEFORE any mutation — no blocks, no
        view changes, no counters, and deliberately NO strike/latch: a
        harness-side contract break is not a module failure. (A violation
        raised from ``finalize_turn`` inside an agent-owned ``assemble`` IS
        attributed to the module by the P1 catch — the module tampered
        the list it was given.)"""
        if not isinstance(msgs, list):
            self._emit({"type": "cm_msg_contract_violation",
                        "turn": self.turn, "where": where, "offenders": [],
                        "detail": f"expected a list, got {type(msgs).__name__}"})
            raise TypeError(f"cm msgs contract violated at {where}: "
                            "expected a list, got "
                            f"{type(msgs).__name__}")
        bad = [(i, type(m).__name__) for i, m in enumerate(msgs)
               if not isinstance(m, dict)]
        if not bad:
            return
        detail = ", ".join(f"msgs[{i}] is {t}" for i, t in bad[:5])
        self._emit({"type": "cm_msg_contract_violation", "turn": self.turn,
                    "where": where, "offenders": bad[:10],
                    "detail": ops.clip_diag(detail, 160)})
        raise TypeError(f"cm msgs contract violated at {where}: {detail}")

    def register_history(self, msgs: List[Dict[str, Any]]) -> None:
        """Register NEW message-history entries as blocks (universe add-only).

        The LLM client rebuilds the history list every call (litellm composes
        a fresh list), so object identity does NOT survive a turn — blocks are
        matched by CONTENT FINGERPRINT (uniqueness guard: identical text maps
        to one block). An entry showing a baked view artifact (``[block bN
        ...`` marker) is RE-BOUND to its block instead of re-registered: the
        rendered copy must not grow a second block or the view would fight
        its own output. Blocks carry their latest msg_index for rendering."""
        self._require_msg_dicts(msgs, "register_history")
        for i, m in enumerate(msgs):
            content = _msg_text(m)
            if not content:
                continue  # tool-call carriers etc. — no text to manage
            bid = _parse_view_marker(content)
            if bid is not None and bid in self.universe.blocks:
                block = self.universe.get(bid)
                # Re-bind only a GENUINE rendered copy: an entry whose whole
                # text is the block's current baked form (render replaces the
                # block's span with the bake output; when that span WAS the
                # whole entry, the copy is byte-identical — the case this
                # branch exists for: the rendered copy must not grow a second
                # block). A marker QUOTED inside agent/tool text — discussing
                # one's own context, or a fetch trim's returned line riding
                # inside the tool-result json wrapper — must NOT steal the
                # binding: that silently un-applied the block's form on its
                # real entry (the eviction/trim shipped raw again) and left
                # the quoting entry unmanaged (docs/19 batch record).
                # Non-matching entries fall through to the fingerprint path
                # and register as the fresh content they are.
                if content.strip() == self._baked_entry_text(block).strip():
                    block.msg_index = i  # re-bind, not re-register
                    continue
            fp = _fingerprint(content)
            known = self._by_fp.get(fp)
            if known is not None and known in self.universe.blocks:
                self.universe.get(known).msg_index = i
                continue
            role = str(m.get("role", "user"))
            kind = _KIND_FOR_ROLE.get(role, "turn_output")
            # The session's FIRST user turn (the instruction, carrying the cost
            # statement) is pinned by default (docs/10 §5.2.3: 当前问题默认
            # pin) — whichever way it arrives (history entry or outgoing).
            first_user = role == "user" and not self._seen_user
            block = self.universe.register(kind, content, self.turn,
                                           source=role, pinned=first_user)
            if role == "user":
                self._seen_user = True
            block.msg_index = i
            self._by_fp[fp] = block.block_id
            self._reg_fp[block.block_id] = fp

    def _baked_entry_text(self, block: Block) -> str:
        """What a WHOLE-ENTRY rendered copy of this block looks like right
        now — the marker re-bind guard's expectation. Mirrors render's bake
        switch (span replacement makes an echoed copy byte-identical only
        when the baked span WAS the whole entry); ``full``/``dedup`` have no
        baked whole-entry form to match (identical text re-binds via the
        fingerprint path anyway; the dedup rewrite carries no ``[block``
        marker)."""
        form = self.view.form_of(block.block_id)
        rule = str(self.view.rules.get(block.block_id) or "")
        if form == "head_tail":
            return self.view_text(block, "head_tail", rule)
        if form == "placeholder":
            return self.view_text(block, "placeholder", rule)
        if form == "template":
            return self.view_text(block, "template", rule)
        if form == "derived_host":
            dblock = (self.universe.get(rule[len("derived="):])
                      if rule.startswith("derived=") else None)
            if dblock is None:
                return ""  # unreachable (universe add-only): nothing to match
            return self.view_text(block, "derived_host", rule)
        return ""

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

    def register_outgoing(self, outgoing: str) -> Optional["Block"]:
        """Register the outgoing user text as the current turn's block.

        The outgoing text is registered NOW and re-bound to its canonical
        history entry next turn (fingerprint below; without it the same
        content was registered TWICE — an orphan that inflated savings).
        Invariant: the ACTIVE instruction is registered for the audit face
        but is never substituted by the assembly — its only form change is
        the dedup prefix-elision, applied by ``finalize_turn``."""
        if not isinstance(outgoing, str) or not outgoing:
            return None
        first_user = not self._seen_user
        block = self.universe.register("turn_output", outgoing, self.turn,
                                       source="user", pinned=first_user)
        self._seen_user = True
        fp_out = _fingerprint(outgoing)
        self._by_fp[fp_out] = block.block_id
        self._reg_fp[block.block_id] = fp_out
        # R5 (docs/19 §12.38): return a SNAPSHOT, never the live Block — the
        # universe object anchors render span-matching, drop floors and the
        # reconcile's identity question, and a live hand-out let an
        # assembly's in-place field write corrupt all of that invisibly
        # (none of the reconcile questions covered block fields). The copy
        # makes the live object unreachable BY CONSTRUCTION (the P2 posture,
        # same as the msgs boundary copy and the ctx faces' to_meta reads):
        # every legitimate read (block_id/chars/pinned/text) sees identical
        # values, and a write lands on the assembly's own scratch copy.
        return copy.deepcopy(block)

    def materialize_traj(self, files: List[Tuple[str, str]]) -> int:
        """Snapshot the seat's visible trajectory files into the universe.

        ``files`` is a list of ``(label, path)`` pairs with genid-qualified
        labels — VISIBILITY is the caller's decision (the same set the read
        tools serve); this primitive only snapshots it, one scan per session.
        Every segment becomes a pinned ``traj_seg`` block holding its full
        text, with the seg_id as ``source``: the index the read tools render
        and the blocks ``ctx_index``/``ctx_fetch`` address derive from the
        SAME scan, so a fetch can never drift from the index the agent saw
        (the material is invariant: trajectories are archived before they
        become readable). Pinned so the window/compaction passes — which walk
        the RENDERED history's turns — never produce phantom ops on blocks
        that exist only as fetchable addressable content.

        ``passthrough`` is an absolute no-op (identity view: nothing
        materializes, nothing emits). Per-file scan failures skip that file
        and SPEAK (``cm_traj_materialize_failed``); the read tools then fall
        back to building their own index for uncovered genids. Returns the
        number of segments registered."""
        if self.mode == "passthrough" or not files:
            return 0
        from .traj_index import build_traj_materialization
        clean: List[Tuple[str, str]] = []
        seen: set = set()
        for label, path in files:
            if not label or not path or label in seen:
                continue
            if not os.path.isfile(path):
                continue
            seen.add(label)
            clean.append((str(label), str(path)))
        if not clean:
            return 0
        try:
            payload, texts = build_traj_materialization(clean)
        except Exception as e:  # noqa: BLE001 -- degrade, but speak
            self._emit({"type": "cm_traj_materialize_failed",
                        "error": ops.clip_diag(str(e), 200)})
            return 0
        registered = 0
        for seg in payload.get("segments", []):
            text = texts.get(seg["seg_id"])
            if text is None:
                continue
            if seg["seg_id"] in self.traj_segments:
                continue  # idempotent: a re-materialize never duplicates blocks
            block = self.universe.register("traj_seg", text, 0,
                                           source=seg["seg_id"], pinned=True)
            self.traj_segments[seg["seg_id"]] = block.block_id
            registered += 1
        self.traj_index = payload
        self._emit({"type": "cm_traj_materialized", "files": len(payload["files"]),
                    "segments": registered})
        return registered

    # -------------------------------------------------------------- the seam
    def _boundary_copy(self, msgs):
        """The per-turn defensive copy handed to the agent-owned assembly
        (docs/19 §12.36, R1): the seam's ``msgs`` is a list-local shell whose
        ENTRY DICTS are shared with the loop's ``raw_history`` — handing the
        live objects to agent code would let an in-place field write corrupt
        the cross-turn canonical (the P1 restore covers the text carriers
        only), and a list grow/shrink survives the restore into the preset
        replay. The copy makes the canonical unreachable BY CONSTRUCTION;
        the P1 texts layer stays as the escape-tier tripwire (§12.27
        pattern: backstop, never the primary guard), and the fallback
        preset replay at the end of :meth:`assemble_turn` runs on the
        PRISTINE real list.

        deepcopy (not ``dict(m)``): under the flat str-valued msgs contract
        the two are equivalent (strings are atomic — deepcopy returns them
        unchanged, so long texts are never copied), and deepcopy also
        isolates a future nested-mutable value instead of reopening the
        write-through channel (N5c posture). A non-copyable entry is OUT of
        contract: loud ``cm_msg_contract_violation`` envelope + TypeError,
        raised OUTSIDE the P1 catch (a framework/contract break is never a
        module strike)."""
        try:
            return [copy.deepcopy(m) for m in msgs]
        except Exception as e:  # noqa: BLE001 -- out-of-contract shape: loud, no strike
            self._emit({"type": "cm_msg_contract_violation", "turn": self.turn,
                        "where": "boundary_copy",
                        "detail": ops.clip_diag(str(e), 160)})
            raise TypeError(
                f"cm msgs contract violated at boundary_copy: {e}") from e

    def assemble_turn(self, msgs: List[Dict[str, Any]],
                      outgoing: str,
                      tokens_used: int = 0,
                      budget: Optional[int] = None
                      ) -> Tuple[List[Dict[str, Any]], str]:
        """The chat-loop contract: assemble ONE LLM call (dispatch only).

        ``msgs`` is the canonical history (raw dicts), ``outgoing`` the raw
        user text for this call. Returns ``(history_to_send, msg_to_send)``.
        The PIPELINE itself is the resolved assembly (``cm/assembly.py``):
        a named preset composition over this session's primitive methods —
        ``passthrough`` being the identity view. Resolution is cached; an unknown mode fails open to the
        slice preset with a loud ``cm_assembly_fallback`` event (the clamp
        authority makes that unreachable in production; a direct
        construction must not degrade silently).

        When the design names an assembly (``assembly_path``), it
        REPLACES the preset composition for every non-passthrough turn —
        loaded once per session, executed under ``cm_assembly_exec_ms``,
        wedge-broken after ONE failure (docs/19 §12.17: load-once makes
        byte-identical retries deterministic waste). ``passthrough`` is the ABSOLUTE
        identity posture: a named assembly is never executed under it (the
        kill switch must stay absolute; the mismatch is spoken once). ANY
        assembly failure re-runs the baseline preset on the current state
        for that turn (sticky forms make the replay safe) — the preset is
        the SESSION'S OWN mode preset: the fail-open removes exactly the
        failed module and keeps the rest (docs/19 §12.30 rule 1) — and
        leaves a durable trace: ``cm_assembly_failed`` per failing turn,
        ``degraded`` on that turn's ``cm_view``, ``assembly_disabled`` in
        every subsequent ``cm_view`` — a fallback is never silent.

        The assembly never sees the live ``msgs``: it receives a per-turn
        BOUNDARY COPY (``_boundary_copy``, docs/19 §12.36) whose entries
        share nothing with the loop's ``raw_history``, so the cross-turn
        canonical is unreachable by construction and the fallback replay
        always runs on the pristine real list. The P1 snapshot/reconcile
        stays as the escape-tier tripwire (view forms, pins, counters,
        refs — and the canonical texts layer, which can now only fire for
        direct callers)."""
        self._require_msg_dicts(msgs, "assemble_turn")
        self.turn += 1
        self._turn_degraded = False
        self._method_face = None  # per-turn method-face budget reset
        fn = self._assembly_fn
        if fn is None:
            fn, fallback = assembly.resolve(self.mode)
            self._assembly_fn = fn
            if fallback is not None:
                self._emit({"type": "cm_assembly_fallback", "turn": self.turn,
                            "mode": self.mode, "to": fallback})
        if self.assembly_path and self.mode == "passthrough":
            if not self._assembly_ignored:
                self._assembly_ignored = True
                self._emit({"type": "cm_assembly_ignored", "turn": self.turn,
                            "reason": "passthrough is the identity posture; "
                                      "the named assembly is never executed "
                                      "under it"})
            return fn(self, msgs, outgoing, tokens_used, budget)
        # after the wedge breaker disables the assembly, the preset runs
        # directly: no more per-turn attempts, failures, or disabled events —
        # the standing degradation stays visible via cm_view stats alone
        named = None if self.assembly_disabled else self._load_named()
        if named is not None:
            self._view_emitted_turn = None
            # P1: the LAST LEGAL STATE (end of the previous turn) is the
            # rollback target — never a blank view (that would silently undo
            # earlier turns' compaction, changing the pin window and the
            # dedup accounting). Everything the failed attempt does is undone
            # except the block universe, which is add-only by contract (its
            # extra blocks are unreferenced and never sent).
            snap = self._snapshot_turn(msgs)
            # R1 (docs/19 §12.36): the agent-owned assembly receives a
            # BOUNDARY COPY, never the live list — the canonical is
            # unreachable by construction, and the fallback preset replay at
            # the end of this method runs on the PRISTINE real list. The
            # copy failure raises OUTSIDE the P1 try on purpose (a contract
            # break is never a module strike).
            assembly_msgs = self._boundary_copy(msgs)
            try:
                # P2: the agent-owned assembly gets the PORTAL, not the live
                # session (the published primitive surface + read-only
                # parameters; ``emit_view``/``count_ops``/state fields are
                # refused). The parameter name stays ``session``.
                result, over_ms = assembly.run_assembly(
                    named, self._portal(), assembly_msgs, outgoing,
                    tokens_used, budget, self.assembly_exec_ms)
                if not (isinstance(result, tuple) and len(result) == 2
                        and isinstance(result[0], list)
                        and isinstance(result[1], str)):
                    raise assembly.AssemblyError(
                        "assembly returned a non-(history, msg) result")
                problem = _validate_sent_history(result[0])
                if problem:
                    raise assembly.AssemblyError(problem)
                if over_ms:
                    # Band-① advisory, owned by the LIVE session: the emit
                    # used to fire inside run_assembly through the portal,
                    # whose _emit raises — a completed-over-budget run died
                    # as a false contract failure (docs/19 §12.30.3/N1).
                    self.assembly_overruns += 1
                    self._emit({"type": "cm_assembly_overrun",
                                "turn": self.turn, "over_ms": over_ms})
                violations = self._reconcile_turn(msgs, snap)
                if violations:
                    self._note_violations(violations)
                    self._restore_turn(snap, msgs, outgoing_text=outgoing)
                    raise assembly.AssemblyError(
                        "assembly violated the seam contract: "
                        + ",".join(kind for kind, _refs in violations))
                self.assembly_active = True
                self._turn_snapshot = None
                if self._view_emitted_turn != self.turn:
                    # audit completeness is framework-owned: an assembly
                    # that skips the finalize_turn post segment still gets
                    # its per-turn cm_view (minimal honest brief)
                    self._emit_synthetic_view()
                return result
            except Exception as e:  # noqa: BLE001 -- fail-open, never break the seam
                phase = ("contract" if isinstance(e, assembly.AssemblyError)
                         else "run")
                # A crashed or violating attempt may have mutated state before
                # it raised: go back to the turn-start state BEFORE the preset
                # replays (idempotent — the violation path already restored).
                self._restore_turn(snap, msgs, outgoing_text=outgoing)
                self._note_assembly_failure(ops.clip_diag(str(e), 160), phase=phase,
                                            exc=e,
                                            timed_out=isinstance(
                                                e, assembly.AssemblyTimeout))
                # fall through: the preset re-runs on the CLEAN state (sticky
                # forms make the replay safe); this turn's cm_view carries
                # degraded=true
        return fn(self, msgs, outgoing, tokens_used, budget)

    def _load_named(self) -> Optional[Callable]:
        """Load the named assembly ONCE per session (docs/19 §12.17): the
        load runs under the SAME exec budget as the execution, the cache
        holds success AND failure, and a mid-session edit must not change
        the seam. A load failure — CRASH or timeout, both strike-counted
        (byte-identical retries are deterministic waste; a crash used to
        speak once and never strike, so the feedback loop could not tell
        the author) — fails the session open to the baseline preset;
        ``summary()`` shows ``assembly_active: false`` and the failure
        facts ride ``assembly_report``."""
        if self.assembly_path is None:
            return None
        if self._assembly_load is None:
            self._assembly_load = _ModuleLoad(
                "assembly", self.assembly_path,
                assembly.timed_load_assembly, assembly.AssemblyTimeout)
        st = self._assembly_load
        first = st.result is None and not st.failed
        result = st.ensure(self.assembly_exec_ms)
        if st.failed and result is None:
            # one strike, one event pair — crash and timeout alike
            self._note_assembly_failure(st.error, phase="load",
                                        timed_out=st.timed_out, exc=st.exc)
            self._emit({"type": "cm_assembly_fallback", "turn": self.turn,
                        "mode": self.mode, "to": self.mode,
                        "reason": st.error})
            return None
        fn, methods, method_problems = result
        if first:
            self._merge_methods(methods, method_problems, source="assembly")
        return fn

    def _merge_methods(self, methods: Dict[str, Callable],
                       problems: List[str], source: str) -> None:
        """Register one named module's custom methods. Refusals speak
        exactly once per source (``cm_methods_refused``, reasons included);
        a policy method whose name an assembly already registered refuses as
        shadowed (assembly > policy, deterministic precedence). All-or-
        nothing per module: the caller passes methods only when the whole
        group validated."""
        reasons = list(problems)
        for name, fn in (methods or {}).items():
            if name in self.methods_registry:
                reasons.append(f"{name!r}: shadowed by an earlier "
                               "registration (assembly > policy)")
            else:
                self.methods_registry[name] = fn
        if reasons:
            self._emit({"type": "cm_methods_refused", "source": source,
                        "reasons": reasons[:12]})

    def _invoke_method(self, name: str, kwargs: Dict[str, Any],
                       callee: "ViewHandle") -> Dict[str, Any]:
        """Run one custom method; NEVER raises. ``callee`` is the reach the
        method receives — the caller's own handle (handle.call) or the
        session's per-turn method-face handle (call_method): privilege =
        the caller's privilege, no escalation. Unknown name, crash, or a
        non-record return comes back as a refusal record."""
        fn = self.methods_registry.get(str(name))
        if fn is None:
            return {"op": str(name), "ok": False, "error": "unknown method"}
        try:
            rec = fn(callee, **kwargs)
        except Exception as e:  # noqa: BLE001 -- refusal, never a seam crash
            return {"op": str(name), "ok": False, "error": ops.clip_diag(str(e), 160)}
        if not isinstance(rec, dict) or "ok" not in rec:
            return {"op": str(name), "ok": False,
                    "error": "method returned a non-record"}
        rec = dict(rec)
        rec.setdefault("op", str(name))
        return rec

    def call_method(self, name: str, **kwargs) -> Dict[str, Any]:
        """Assembly-face entry: run a registered custom method with the
        session's method-face handle (policy face; budget =
        ``cm_max_ops_per_turn`` reset each turn, so an assembly looping
        calls stays bounded — the call itself costs one unit, exactly like
        ``handle.call``). Never raises — the refusal record is the
        assembly's to embed in its records (and be counted) or drop."""
        if self._method_face is None:
            self._method_face = ViewHandle(self, "policy")
        if err := self._method_face._gate("call"):
            return err
        return self._invoke_method(name, kwargs, callee=self._method_face)

    # -- P1: turn snapshots, reconciliation, rollback ---------------------
    def _portal(self) -> "SessionPortal":
        """The P2 façade handed to the named assembly (cached per session)."""
        if self._portal_cache is None:
            self._portal_cache = SessionPortal(self)
        return self._portal_cache

    def record_refs(self, records: List[Dict[str, Any]]) -> None:
        """Fold the refs of already-produced stage records into the
        recorded-refs set (docs/19 §12.18; portal surface since the N2
        batch, docs/19 §12.33).

        The policy-path P1 reconcile runs BEFORE ``finalize_turn``
        (``count_ops``), so form changes made by the baseline stages
        (``window_defaults`` drop placeholders, ``dedup_prefix``) would read
        as unrecorded at snapshot time — count_ops only records their refs
        at closure. Mirrors count_ops's condition exactly (ok + not already
        handle-audited); the later re-recording is an idempotent set
        update.

        SEMANTICS — explanation, NOT counting (docs/19 §12.33): this only
        widens the recorded-refs set, the P1 reconcile's explanation
        channel. The audit counters (``ops_total``/``ops_applied``/
        ``est_chars_saved``) move ONLY through ``finalize_turn``; an op
        whose record is folded here but never threaded is explained yet
        uncounted — exactly the mirror of the handle audit hook, which
        auto-publishes refs for the same reason. On the SessionPortal
        because the authoring surface must express the composition the
        baseline pipeline performs (a shrink stage before ``run_policy``
        was unexpressible and struck the POLICY with ``unrecorded_view``
        for the assembly's missing fold — measured repro in §12.33).
        Residual, accepted and documented: a caller may fold refs it
        fabricated, lowering the cost of faking a question-1 explanation;
        the malicious tier's backstop is the reconcile's other three
        questions (and T3/F5, both user-deferred)."""
        for rec in records:
            if (isinstance(rec, dict) and rec.get("ok")
                    and not rec.get("_audited")):
                refs = rec.get("refs") or []
                if refs:
                    self._recorded_refs.update(str(r) for r in refs)

    def _snapshot_turn(self, msgs: Optional[List[Dict[str, Any]]] = None
                       ) -> Dict[str, Any]:
        """Snapshot the turn-mutable state (rollback target for P1).

        ``msgs=None`` (the policy path without a canonical reference) skips
        the canonical-texts layer: the view/counters/refs protection still
        applies — only the msgs-rewrite question is unanswerable without the
        list."""
        return {
            "forms": dict(self.view.forms),
            "rules": dict(self.view.rules),
            # (value, carrier_key, had_text, had_content): restoration must
            # also undo a key the attempt ADDED (``content`` grafted onto an
            # entry that carries ``text`` would otherwise survive, because
            # ``_msg_text`` prefers ``content``).
            "texts": None if msgs is None else [
                (_msg_text(m),
                 ("content" if isinstance(m.get("content"), str)
                  else "text" if isinstance(m.get("text"), str) else None),
                 isinstance(m.get("text"), str),
                 isinstance(m.get("content"), str))
                for m in (msgs or [])],
            # R7a (docs/19 §12.38): the overrun counters ride the snapshot —
            # their band-① increment happens AFTER this snapshot and BEFORE
            # the reconcile, so a contract-rejected turn must roll them back
            # too (they used to survive the restore: a rejected turn kept
            # its +1).
            "counters": (self.ops_total, self.ops_applied, self.ops_refused,
                         self.violations, self.est_chars_saved,
                         self.assembly_overruns, self.policy_overruns),
            "refs": set(self._recorded_refs),
            # pinned FLAG of every block existing at snapshot time (docs/19
            # §12.18: the restore surface covers the pre-existing pins — a
            # crashed attempt's pin/unpin of an EXISTING block is undone).
            # Blocks the attempt REGISTERED (note blocks are auto-pinned)
            # stay as-is: the universe is add-only, and a note's block and
            # its ``notes`` entry must stay consistent (notes persist by
            # design — scratchpad semantics).
            "pinned_state": {bid: block.pinned
                             for bid, block in self.universe.blocks.items()},
        }

    def _restore_turn(self, snap: Optional[Dict[str, Any]],
                      msgs: List[Dict[str, Any]],
                      outgoing_text: Optional[str] = None) -> None:
        """Undo everything the failed attempt did, back to the last legal state.

        ``outgoing_text`` (docs/19 §12.38, R5 backstop) is the seam's
        outgoing payload — the pristine source for restoring an UNBOUND
        turn_output block whose fields were mutated in place; bound blocks
        restore from their canonical msg carrier."""
        if not snap:
            return
        self.view.forms.clear()
        self.view.forms.update(snap.get("forms") or {})
        # legacy normalization (docs/19 §12.21): the excerpt form retired —
        # an old snapshot may still carry it; head_tail at the session keep
        # is the closest surviving semantic.
        for _bid, _f in list(self.view.forms.items()):
            if _f == "excerpt":
                self.view.forms[_bid] = "head_tail"
        self.view.rules.clear()
        self.view.rules.update(snap.get("rules") or {})
        cnt = list(snap.get("counters") or ())
        defaults = (self.ops_total, self.ops_applied, self.ops_refused,
                    self.violations, self.est_chars_saved,
                    self.assembly_overruns, self.policy_overruns)
        cnt += defaults[len(cnt):]   # tolerate an older 5-field snapshot
        (self.ops_total, self.ops_applied, self.ops_refused, self.violations,
         self.est_chars_saved, self.assembly_overruns,
         self.policy_overruns) = cnt
        self._recorded_refs.clear()
        self._recorded_refs.update(snap.get("refs") or ())
        # pinned flags of blocks that EXISTED at snapshot time (docs/19
        # §12.18): the attempt's pin/unpin of a pre-existing block is undone;
        # blocks it registered (note blocks) stay as-is — the universe is
        # add-only and note blocks must keep their auto-pin.
        pinned_state = snap.get("pinned_state") or {}
        for bid, was_pinned in pinned_state.items():
            block = self.universe.blocks.get(bid)
            if block is not None:
                block.pinned = bool(was_pinned)
        # R5 backstop (docs/19 §12.38): a turn_output block whose text no
        # longer fingerprints to its registration value (or whose chars
        # desynced) was mutated in place — restore from the pristine
        # source: the canonical msg carrier for bound blocks, the seam's
        # outgoing payload for the unbound one. Pure backstop: the portal
        # hands out snapshots, so agent code cannot reach here.
        for block in self.universe.blocks.values():
            if block.kind != "turn_output":
                continue
            intact = (self._reg_fp.get(block.block_id)
                      == _fingerprint(block.text))
            if intact:
                continue
            if block.msg_index is not None and block.msg_index < len(msgs or []):
                entry = (msgs or [])[block.msg_index]
                if isinstance(entry, dict):
                    restored = _msg_text(entry)
                    if restored:
                        block.text = restored   # chars derives from text
            elif outgoing_text and block.block_id == self._by_fp.get(
                    _fingerprint(outgoing_text)):
                block.text = outgoing_text  # chars derives from text
        # canonical: put the original text back where the attempt rewrote it
        # (``texts is None`` = the snapshot carried no canonical reference —
        # the policy path without ``msgs``; the layer is simply skipped)
        texts = snap.get("texts")
        if texts is None:
            return
        for i, entry in enumerate(msgs or []):
            # entries are contract-validated upstream (§12.32.4): texts is
            # POSITION-ALIGNED with msgs by construction, and a non-dict
            # here is a bypass that must crash loudly, never skip silently.
            if i >= len(texts):
                continue
            value, key, had_text, had_content = texts[i]
            for carrier, had in (("text", had_text), ("content", had_content)):
                if not had and carrier in entry:
                    entry.pop(carrier, None)   # the attempt ADDED this carrier
            if key is not None and entry.get(key) != value:
                entry[key] = value

    def _reconcile_turn(self, msgs: List[Dict[str, Any]],
                        snap: Optional[Dict[str, Any]],
                        check_closure: bool = True
                        ) -> List[Tuple[str, List[str]]]:
        """The five seam-contract questions (docs/19 §12.14, §12.27, §12.38).

        1. every non-full form must be explained by a recorded op;
        2. a pinned block may only be ``full`` (or ``dedup``: the prefix-elision
           form, which loses only the repeated header);
        3. the canonical history must be untouched;
        4. the pinned set of PRE-EXISTING blocks is policy-immutable (D8
           direction (b): the policy face has no pin/unpin — a pinned delta
           on a block that existed at snapshot time is a ``_s``-escape:
           detected here, restored by the caller, strike-counted). Blocks the
           attempt REGISTERED keep their add-only auto-pin (the universe is
           never rolled back).

        ``check_closure=False`` (the POLICY path) skips the turn-closure
        question: a policy's ops are closed by ``assemble_custom``'s
        ``finalize_turn`` AFTER ``run_policy`` returns, so asking "did this
        turn close through the post segment" inside the policy would
        false-positive on every legitimate op (docs/19 §12.18).
        """
        out: List[Tuple[str, List[str]]] = []
        recorded = set(self._recorded_refs)
        unexplained = sorted(
            bid for bid, form in self.view.forms.items()
            if form != "full" and bid not in recorded)
        if unexplained:
            out.append(("unrecorded_view", unexplained))
        # Turn closure: a turn that CHANGED the view must have closed through
        # the post segment (``finalize_turn``) — otherwise the ops happened and
        # the audit never counted them (docs/19 §12.14, S2: the documented
        # handle API invites this). SKIPPED for the policy path (see above).
        if check_closure:
            before_forms = (snap or {}).get("forms") or {}
            changed_now = {bid for bid, form in self.view.forms.items()
                           if before_forms.get(bid, "full") != form}
            if changed_now and self._view_emitted_turn != self.turn:
                out.append(("unclosed_turn", sorted(changed_now)))
        pinned_broken = sorted(
            block.block_id for block in self.universe.blocks.values()
            if block.pinned
            and self.view.form_of(block.block_id) not in ("full", "dedup"))
        if pinned_broken:
            out.append(("pinned_form", pinned_broken))
        # 4. pinned delta on pre-existing blocks (docs/19 §12.27): BOTH
        # directions — an escape pin (protection the policy must not grant)
        # and an escape unpin (protection the policy must not remove).
        pinned_before = (snap or {}).get("pinned_state") or {}
        pinned_delta = sorted(
            bid for bid, block in self.universe.blocks.items()
            if bid in pinned_before
            and bool(pinned_before[bid]) != bool(block.pinned))
        if pinned_delta:
            out.append(("unrecorded_pin", pinned_delta))
        snap_texts = (snap or {}).get("texts")
        if snap_texts is not None:
            before = [t[0] for t in snap_texts]
            after = [_msg_text(m) for m in (msgs or []) if isinstance(m, dict)]
            if before != after:
                moved = [f"msgs[{i}]" for i, (a, b) in enumerate(zip(before, after))
                         if a != b]
                out.append(("canonical_mutated", moved[:8]))
        # 5. registration identity (docs/19 §12.38, R5 backstop): a
        # turn_output block's text must still fingerprint to its
        # registration value — the portal hands
        # out SNAPSHOT copies, so this can only fire through a future
        # live-object leak (a new portal method handing out state) or a
        # direct caller. Restored by _restore_turn from the pristine
        # source (canonical msg carrier / the seam's outgoing payload).
        tampered = sorted(
            block.block_id for block in self.universe.blocks.values()
            if block.kind == "turn_output"
            and self._reg_fp.get(block.block_id) != _fingerprint(block.text))
        if tampered:
            out.append(("block_tampered", tampered))
        return out

    def _note_violations(self, violations: List[Tuple[str, List[str]]],
                         source: str = "assembly") -> None:
        """Count + emit each seam-contract violation (never silent). The
        event vocabulary is per-subsystem (docs/19 §12.17.1): the named
        assembly's reconcile reports ``cm_assembly_violation``, the custom
        policy tail's reports ``cm_policy_violation`` — a policy escape
        used to be filed against the assembly (N5a)."""
        for kind, refs in violations:
            if source == "policy":
                self.policy_violations += 1
            else:
                self.assembly_violations += 1
            self._emit({"type": f"cm_{source}_violation", "turn": self.turn,
                        "kind": kind, "refs": list(refs)[:20]})

    def _note_assembly_failure(self, error: str, phase: str = "run",
                               exc: Any = None,
                               timed_out: bool = False) -> None:
        """One assembly turn failure: count + emit + latch.

        ONE failure is enough to stop re-attempting IN THIS SESSION (the
        module is deterministic; a retry can leak a worker thread). The
        preset then runs directly and every subsequent ``cm_view`` carries
        ``assembly_disabled: true`` until the session ends. The framework's
        loop-level LATCH (fingerprint-keyed, docs/19 §12.14) additionally
        stops the NEXT sessions from re-attempting a module that has not
        changed. ``phase`` distinguishes load-stage (module top level) from
        run-stage (``assemble``) from contract refusals; ``timed_out``
        marks the leaked worker thread in the session's leak counters
        (CM-03)."""
        self.assembly_failures += 1
        self._assembly_strikes += 1
        # A strike turn IS a degraded turn — its governance came from the
        # fail-open preset (docs/19 §12.30 rule 1; this also covers the
        # load-failure turn, which has no other degraded marker).
        self._turn_degraded = True
        if timed_out:
            self.thread_leaks += 1
            cand = getattr(exc, "candidate_thread", None)
            if cand is not None:
                self._timeout_workers.append(cand)
        where = ""
        if exc is not None and phase == "run":
            try:
                import traceback
                frames = traceback.extract_tb(exc.__traceback__)
                if frames:
                    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
            except Exception:  # noqa: BLE001 -- diagnostics only
                where = ""
        self._assembly_last_error = {"error": str(error), "phase": phase,
                                     "where": where}
        self._emit({"type": "cm_assembly_failed", "turn": self.turn,
                    "phase": phase, "error": error,
                    "struck": self._assembly_strikes,
                    "where": where,
                    "fallback": self.mode})
        if self._assembly_strikes >= _AGENT_MODULE_STRIKES:
            self.assembly_disabled = True
            self._emit({"type": "cm_assembly_disabled", "turn": self.turn,
                        "strikes": self._assembly_strikes})

    def _emit_synthetic_view(self) -> None:
        """Per-turn audit completion for an assembly that returned without
        closing through ``finalize_turn``: one ``cm_view`` brief per
        assembled turn is the framework's contract, so the seam emits the
        minimal honest brief itself (ops empty; stats marked)."""
        self._emit({"type": "cm_view", "turn": self.turn, "mode": self.mode,
                    "ops": [],
                    "stats": {"blocks": len(self.universe.blocks),
                              "substituted": len(self.view.forms),
                              "saved_chars": 0,
                              "est_saved_session": self.est_chars_saved,
                              "policy_disabled": self.policy_disabled,
                              "assembled_by": "agent_assembly"}})
        self._view_emitted_turn = self.turn

    def assemble(self, msgs: List[Dict[str, Any]],
                 tokens_used: int = 0,
                 budget: Optional[int] = None) -> List[Dict[str, Any]]:
        """Direct-form seam (tests / non-chat callers): render the history
        only; passthrough returns ``msgs`` unchanged."""
        if self.mode == "passthrough":
            self.turn += 1
            self.register_history(msgs)
            self.emit_view(0)
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

    # -- stage primitives (the frozen composition surface; see assembly.py) --
    def budget_checkpoint(self, tokens_used: int,
                          budget: Optional[int]) -> Optional[Dict[str, Any]]:
        """Advisory warning at >=80% of an active token budget (the C8 soft
        stop stays the enforcement point; this only informs)."""
        used = tokens_used
        if not used and self.usage_reader is not None:
            used = self.usage_snapshot().get("tokens_used") or 0
        if not isinstance(budget, int) or budget <= 0 \
                or not isinstance(used, int) or used <= 0:
            return None
        if used < _WARN_RATIO * budget:
            return None
        self._emit({"type": "cm_budget_checkpoint", "turn": self.turn,
                    "used": used, "budget": budget})
        return {"op": "budget_checkpoint", "ok": True, "source": "pre"}

    def window_defaults(self) -> List[Dict[str, Any]]:
        """slice/compact/custom modes: keep the newest ``window_k`` assistant
        turns FULL (plus pinned blocks + notes — pin/note protection lives in
        the primitives), older turn/tool blocks -> placeholder. Drop-floor
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
            if block.pinned:
                continue  # pre-checked so the primitive's refusal (a pinned
                # block must never drop) does not become violation noise
            if block.chars < self.drop_floor_chars:
                continue
            prev = self._pre_state(block.block_id)
            rec = ops.drop(self.universe, self.view, block.block_id,
                           f"window_k={self.window_k}",
                           self.drop_floor_chars)
            out.append(self._book(rec, block.block_id, prev))
        return out

    def conservative_compaction(self) -> List[Dict[str, Any]]:
        """compact_conservative adds: history turns older than the pin window
        whose mechanical head_tail trim actually saves >= MIN_COMPACT_SAVE
        chars become head_tail-trimmed (head+tail kept at the session
        ``cm_head_tail_keep``) instead of the slice mode's whole-turn
        placeholder.

        Erratum (docs/10 §11.5): the compaction target is the old USER turn —
        this chat protocol embeds tool outputs in the next user message, so a
        separate ``tool_output`` kind never materializes and the original
        tool_output-only targeting made this tier identical to ``slice``."""
        out: List[Dict[str, Any]] = []
        rounds = 0
        for block in reversed([b for b in self.universe.ordered()
                               if b.kind in ("turn_output", "tool_output")]):
            if block.kind == "turn_output":
                rounds += 1
            if rounds <= max(self.window_k, WINDOW_K_FLOOR):
                continue
            if block.pinned:
                continue
            if self.view.form_of(block.block_id) != "full":
                continue  # already substituted in an earlier turn (sticky)
            if form_saving(block, "head_tail", rule="compacted:first_last",
                           keep=self.head_tail_keep) < MIN_COMPACT_SAVE:
                continue  # the trim would save nothing: leave to the window
            prev = self._pre_state(block.block_id)
            rec = ops.compact(self.universe, self.view,
                              [block.block_id], "first_last",
                              self.drop_floor_chars, self.turn,
                              keep=self.head_tail_keep)
            out.append(self._book(rec, block.block_id, prev))
        return out

    def usage_snapshot(self, tokens_used: Optional[int] = None,
                       budget: Optional[int] = None) -> Dict[str, Any]:
        return ops.usage_query(self.usage_reader, tokens_used, budget)

    # -- policy segment -----------------------------------------------------------
    def run_policy(self, msgs: Optional[List[Dict[str, Any]]] = None
                   ) -> List[Dict[str, Any]]:
        """Run the custom policy module (fail-open; ONE-strike latch; P1
        snapshot/reconcile/restore — docs/19 §12.17/§12.18).

        The module is loaded ONCE per session (``_ModuleLoad``; cached with
        its failure — a mid-session edit must not change the seam) and
        ``apply`` executes EVERY turn under the per-turn budget: load-once
        caches the MODULE, never the run result. One failure — load crash,
        load timeout, or a run-phase failure — disables the policy for the
        session; the failure facts ride ``policy_report`` for the next
        self-improve session.

        P1 parity with the named-assembly branch: the turn-mutable state is
        snapshotted BEFORE ``apply`` and RESTORED when the attempt fails or
        violates the seam contract (a crashed policy's half-applied
        session-level ops must not persist; a ``_s``-escape must be caught).
        ``msgs`` (the canonical history, passed by ``assemble_custom``)
        enables the canonical-texts layer; without it the snapshot covers
        view/counters/refs only. The closure question is structurally
        delegated to ``finalize_turn`` (the caller invokes it right after
        this returns) — see ``_reconcile_turn(check_closure=False)``."""
        if msgs is not None:
            self._require_msg_dicts(msgs, "run_policy")
        if self.policy_disabled:
            return [{"op": "policy", "ok": False,
                     "error": "policy disabled for this session",
                     "_lifecycle": True}]
        if self.policy_path is None:
            # configuration signal, NOT a module failure: the build rewrites
            # custom when nothing is deliverable, so this is only reachable
            # by direct construction. Nothing struck, nothing latched.
            return [{"op": "policy", "ok": False,
                     "error": "no policy configured for this session",
                     "_lifecycle": True}]
        from .failopen import (PolicyTimeout, apply_policy as _apply_policy,
                               timed_load_policy as _timed_load_policy)
        if self._policy_load is None:
            self._policy_load = _ModuleLoad(
                "policy", self.policy_path,
                _timed_load_policy, PolicyTimeout)
        st = self._policy_load
        first = st.result is None and not st.failed
        result = st.ensure(self.policy_exec_ms)
        if st.failed and result is None:
            self._note_policy_failure(st.error, phase="load",
                                      timed_out=st.timed_out, exc=st.exc)
            return [{"op": "policy", "ok": False, "error": "policy failed"}]
        apply_fn, methods, method_problems = result
        if first and not self._methods_policy_done:
            # assembly registrations take precedence (shadowed policy names
            # refuse with a recorded reason)
            self._methods_policy_done = True
            self._merge_methods(methods, method_problems, source="policy")
        handle = ViewHandle(self, "policy")
        # P1: snapshot BEFORE apply; restore on any failure/violation. A
        # LOCAL variable — the turn-level P1 snapshot of the named branch
        # must stay untouched (nested portal.run_policy restores to
        # pre-policy, which is exactly the right granularity there).
        snap = self._snapshot_turn(msgs)
        try:
            records, over_ms = _apply_policy(apply_fn, handle,
                                             self.policy_exec_ms)
        except PolicyTimeout as e:
            # known shared race: the leaked worker still holds the handle
            # (T2 hygiene restart owns the residual; T3 closes it)
            self._restore_turn(snap, msgs)
            self._note_policy_failure(ops.clip_diag(str(e), 160), phase="timeout",
                                      timed_out=True, exc=e, restored=True)
            return [{"op": "policy", "ok": False, "error": "policy failed"}]
        except Exception as e:  # noqa: BLE001 -- fail-open, never break the session
            self._restore_turn(snap, msgs)
            self._note_policy_failure(ops.clip_diag(str(e), 160), phase="run", exc=e,
                                      restored=True)
            return [{"op": "policy", "ok": False, "error": "policy failed"}]
        if over_ms:
            self.policy_overruns += 1
            self._emit({"type": "cm_policy_overrun", "turn": self.turn,
                        "over_ms": over_ms})
        if not isinstance(records, list):
            # A non-list return is a policy failure like any other (failopen
            # contract): strike-counted, emitted, fail-open.
            self._restore_turn(snap, msgs)
            self._note_policy_failure("policy returned a non-list",
                                      phase="contract", restored=True)
            return [{"op": "policy", "ok": False,
                     "error": "policy returned a non-list"}]
        for rec in records:
            if not isinstance(rec, dict):
                # A non-dict record would crash the post-segment count PAST
                # the wedge breaker: one policy failure, strike-counted,
                # emitted, fail-open — never a seam crash with only a log
                # line.
                self._restore_turn(snap, msgs)
                self._note_policy_failure("policy returned a non-dict record",
                                          phase="contract", restored=True)
                return [{"op": "policy", "ok": False,
                         "error": "policy returned a non-dict record"}]
        # P1 reconcile on the SUCCESS route: the three questions answerable
        # before closure (unrecorded_view / pinned_form / canonical_mutated).
        # Closure itself is finalize_turn's job (check_closure=False).
        violations = self._reconcile_turn(msgs, snap, check_closure=False)
        if violations:
            # mirror the named branch's order: the violation EVENTS land
            # first (durable), then the counters roll back with the restore
            self._note_violations(violations, source="policy")
            self._restore_turn(snap, msgs)
            self._note_policy_failure(
                "policy violated the seam contract: "
                + ",".join(kind for kind, _refs in violations),
                phase="contract", restored=True)
            return [{"op": "policy", "ok": False, "error": "policy failed"}]
        return records

    def _note_policy_failure(self, error: str, phase: str = "run",
                             exc: Any = None, timed_out: bool = False,
                             restored: bool = False) -> None:
        """One policy failure: count + emit + ONE-strike latch (docs/19
        §12.17 — the module is load-once, so byte-identical retries are
        deterministic waste; the success-resets-strikes rule died with the
        per-turn reload). ``phase`` distinguishes load/run/timeout/contract;
        ``timed_out`` marks the leaked worker thread; ``restored`` marks a
        P1 rollback (docs/19 §12.18). The failure facts ride
        ``policy_report`` for the next self-improve session."""
        self.policy_failures += 1
        self._policy_strikes += 1
        # A strike turn IS a degraded turn (docs/19 §12.30 rule 1 — same
        # rule as the assembly side): the failing turn's governance came
        # from the pre-segment fail-open.
        self._turn_degraded = True
        if timed_out:
            self.thread_leaks += 1
            cand = getattr(exc, "candidate_thread", None)
            if cand is not None:
                self._timeout_workers.append(cand)
        where = ""
        if exc is not None and phase == "run":
            try:
                import traceback
                frames = traceback.extract_tb(exc.__traceback__)
                if frames:
                    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
            except Exception:  # noqa: BLE001 -- diagnostics only
                where = ""
        self._policy_last_error = {"error": str(error), "phase": phase,
                                   "where": where}
        self._emit({"type": "cm_policy_failed", "turn": self.turn,
                    "phase": phase, "error": error, "where": where,
                    "struck": self._policy_strikes,
                    "state_restored": bool(restored)})
        if self._policy_strikes >= _AGENT_MODULE_STRIKES:
            self.policy_disabled = True
            self._emit({"type": "cm_policy_disabled", "turn": self.turn,
                        "strikes": self._policy_strikes})

    # -- post segment: the audit + render + outgoing contract ----------------------
    def count_ops(self, records: List[Dict[str, Any]]) -> int:
        """The primitives already validated + mutated the view; this counts,
        emits violations, and sums the estimated savings (audit only — the
        ledger keeps 实付; savings are a derived indicator, never billing).

        N2 (docs/19 §12.33): FULL-fidelity accounting — every record is
        classified exactly once, wherever it sits. The old
        ``max_ops_per_turn`` slice + overflow blanket re-labeled tail
        records as refused AFTER they had already happened at exec time
        (the cap's exec authority is the handle gate, not the audit): the
        counters' identity broke (``ops_total`` missed the tail), overflow
        ``ok`` records lost their saved/refs (P1 false-positive risk for
        baseline-overflow refs), and the blanket silently absorbed real
        refusals. Now ``ops_total == ops_applied + ops_refused +
        lifecycle + audited`` holds for every turn, saved/refs recording
        is position-independent, and the cap survives ONLY where it can
        actually enforce (the policy-face handle gate) or present (the
        ``emit_view`` brief)."""
        saved_total = 0
        for rec in records or []:
            self.ops_total += 1
            if rec.get("op") == "budget_checkpoint":
                self.ops_applied += 1
                continue
            if not rec.get("ok"):
                if rec.get("_lifecycle"):
                    # A lifecycle marker (the module is retired/absent) is
                    # not a misbehavior: the standing degradation is spoken
                    # ONCE by cm_policy_disabled + the stats; re-filing it
                    # per turn is the N7 phantom (the assembly side's
                    # standing degradation is already silent — docs/19
                    # §12.30 rule 1 visibility symmetry). The strike
                    # turn's REAL failure record still counts below.
                    continue
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
            # P1: the reconciliation compares the view against EXACTLY what the
            # post segment recorded (cumulative: forms are sticky).
            self._recorded_refs.update(str(r) for r in (rec.get("refs") or []))
        return saved_total

    def finalize_turn(self, msgs: List[Dict[str, Any]],
                      outgoing: str,
                      out_block: Optional["Block"],
                      records: List[Dict[str, Any]]
                      ) -> Tuple[List[Dict[str, Any]], str]:
        """The post segment every non-passthrough turn closes with.

        Invariants owned here (so an assembly cannot route around them):
        the applied records are counted + emitted exactly once per turn
        (``cm_view`` brief, no content); the sent history is materialized
        from the persistent view by the no-fabrication render (copies only
        — 双记); the outgoing instruction is never substituted except the
        dedup prefix-elision (same frozen form, anchored at the head)."""
        self._require_msg_dicts(msgs, "finalize_turn")
        saved = self.count_ops(records)
        self.emit_view(saved, records)
        send_history = self.render(msgs)
        send_msg = outgoing
        if out_block is not None \
                and self.view.form_of(out_block.block_id) == "dedup":
            # the outgoing turn carries the per-turn header; elide it exactly
            # like an old turn (same frozen form, anchored at the head)
            if self.system_prefix and outgoing.startswith(self.system_prefix):
                send_msg = _DEDUP_MARKER + outgoing[len(self.system_prefix):]
        return send_history, send_msg

    def dedup_prefix(self, msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fixed pre-segment: fixed-overhead governance. The tool-use header
        is re-sent in EVERY user message; one in-band marker replaces it on
        an OLD block, keeping the newest window's full header so tool
        behavior is unaffected. A form, not a drop: no content is lost
        except the repeated prefix.

        Population in the PRESET pipelines is narrow by construction
        (docs/19 §12.32.3): ``window_defaults`` runs FIRST and placeholder's
        every un-pinned old block (the ``form != "full"`` skip below), so
        this stage's live surface is the PINNED old blocks — in practice
        the auto-pinned first user turn (one dedup op per session; probed
        in §12.32.3). The order is value-correct: the placeholder saves
        the whole body where dedup saves only the header. As a POLICY-
        composable primitive the stage is fully alive (a custom policy may
        run dedup without the window)."""
        if not self.system_prefix:
            return []
        # R4/F2 (docs/19 §12.37, §12.40): the ACTUAL sent delta is the
        # transition width (prefix minus marker), measured by the session
        # accounting funnel — never the bare prefix length, and never a
        # second copy of the formula. A prefix shorter than the marker would
        # GROW the old turn: skip the block (the "saves nothing" semantics
        # of compact's MIN_COMPACT_SAVE gate; realistic tool-use headers are
        # far longer than the marker).
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
            prev = self._pre_state(block.block_id)
            saving = self._transition_saving(block, prev[0], prev[1],
                                             "dedup", "system_prefix")
            if saving <= 0:
                continue
            self.view.set(block.block_id, "dedup", "system_prefix")
            out.append({"op": "dedup", "refs": [block.block_id], "ok": True,
                        "rule": "system_prefix", "saved": saving})
        return out

    def render(self, msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Materialize the CURRENT persistent view into shallow copies.

        A block's text is only ever replaced by its own head_tail cut (at
        the session keep), the frozen placeholder line, or the catalog-card
        line — the view cannot fabricate content (§5.2 不变量 1). Idempotent:
        an entry whose content already carries the block's baked artifact is
        left alone."""
        self._require_msg_dicts(msgs, "render")
        by_msg: Dict[int, List[Block]] = {}
        for block in self.universe.ordered():
            form = self.view.form_of(block.block_id)
            if form == "full" or block.msg_index is None:
                continue
            by_msg.setdefault(block.msg_index, []).append(block)
        if not by_msg:
            # still copy every entry: the client pops keys from what it gets
            # (see the passthrough preset) — canonical must stay raw
            return [dict(m) if isinstance(m, dict) else m for m in msgs]
        out = [dict(m) if isinstance(m, dict) else m for m in msgs]
        for idx, blocks in by_msg.items():
            if idx < 0 or idx >= len(out):
                continue
            m = out[idx]
            content = _msg_text(m)
            if content is None:
                continue
            new_content = content
            for block in blocks:
                form = self.view.form_of(block.block_id)
                rule = self.view.rules.get(block.block_id, "")
                if f"[block {block.block_id} " in content:
                    continue  # already baked in an earlier turn (idempotent)
                if form == "placeholder":
                    new_text = self.view_text(block, "placeholder",
                                              rule or "view")
                elif form == "head_tail":
                    new_text = self.view_text(block, "head_tail",
                                              rule or "view")
                elif form == "derived_host":
                    # the host span renders the DERIVED text IN FULL (the
                    # merge/template already did the compressing). The baked
                    # marker carries the HOST id, so the idempotency check
                    # and the view-marker re-bind above keep working on the
                    # next turn (docs/10 §15).
                    new_text = self.view_text(block, "derived_host", rule)
                elif form == "dedup":
                    if self.system_prefix and new_content.startswith(self.system_prefix):
                        new_text = _DEDUP_MARKER + new_content[len(self.system_prefix):]
                    else:
                        continue
                else:  # template — catalog CARD, not an eviction event (the
                    # line never claims a loss); restorable via fetch(full)
                    # like every non-full form (docs/10 form 语义声明,
                    # docs/19 §12.20/§12.21)
                    new_text = self.view_text(block, "template", rule or "view")
                if form == "dedup":
                    new_content = new_text  # prefix elision (anchored at head)
                elif block.text and block.text in new_content:
                    new_content = new_content.replace(block.text, new_text, 1)
                else:
                    # the span drifted (result embedded with decorations): a
                    # whole-message placeholder keeps the view honest without
                    # fabricating anything
                    new_content = bake(block, "placeholder",
                                       f"span-drift:{rule or form}")
                    break
            if new_content != content:
                shallow = dict(m)
                # write back under the entry's OWN key: the canonical history
                # mixes ``text``-form and ``content``-form entries, and the
                # client's text->content conversion keys off exactly these
                shallow["content" if isinstance(m.get("content"), str)
                        else "text"] = new_content
                out[idx] = shallow
        return out

    def note_violation(self, rec: Dict[str, Any]) -> None:
        """Count + emit one op refusal (idempotent with the seam path: the
        seam's ``count_ops`` only emits for records that did NOT come from a
        handle audit — tracked via the record marker below).

        This is the OP-REFUSAL family (``cm_violation``): an attempt that
        did not take effect. The reconcile's contract-breach family is
        ``_note_violations`` (per-subsystem event types, §12.17.1) — see
        the vocabulary map on :meth:`_audit`."""
        if rec.get("_audited"):
            return
        rec["_audited"] = True
        self.violations += 1
        self.ops_refused += 1
        self._emit({"type": "cm_violation", "turn": self.turn,
                    "op": rec.get("op"), "refs": rec.get("refs"),
                    "error": rec.get("error")})

    def emit_view(self, saved: int,
                  records: Optional[List[Dict[str, Any]]] = None) -> None:
        """Per-turn assembly event. ``ops`` is the applied-mutation brief
        (op name + block refs + rule, NO content) — the 对账 half of the 双记
        audit: what was kept/dropped, by which rule, addressable by block id
        (docs/10 §5.2.3; the aggregate ``stats`` stay alongside)."""
        ops_brief: List[Dict[str, Any]] = []
        # N2 (docs/19 §12.33): filter BEFORE the cap. Refusals ride
        # cm_violation and the policy run itself is not a view op, but the
        # old slice-first order let them waste brief slots and pushed real
        # applied mutations out of the 对账 half. The brief is the first
        # ``max_ops_per_turn`` APPLIED mutations; the accounting (count_ops)
        # stays full-fidelity regardless of this presentation cap.
        applied = [rec for rec in (records or [])
                   if rec.get("ok") and rec.get("op") != "policy"]
        for rec in applied[:max(1, self.max_ops_per_turn)]:
            brief = {"op": rec.get("op"),
                     "refs": list(rec.get("refs") or []),
                     "rule": str(rec.get("rule")
                                 or rec.get("form") or "")[:60]}
            if rec.get("derived"):
                # conditional audit field: materializes only on ops that
                # minted a derived block (the baseline event shape stays
                # byte-identical — same pattern as degraded/assembly_disabled)
                brief["derived"] = str(rec.get("derived"))
            ops_brief.append(brief)
        stats: Dict[str, Any] = {"blocks": len(self.universe.blocks),
                                 # docs/19 §12.44 D5: NON-FULL forms only —
                                 # same caliber as summary()'s substituted.
                                 "substituted": self._substituted_blocks(),
                                 "saved_chars": saved,
                                 "est_saved_session": self.est_chars_saved,
                                 "policy_disabled": self.policy_disabled}
        # conditional audit fields: they materialize only in the states they
        # describe, so the baseline event shape stays byte-identical while a
        # degraded turn / a wedge-broken assembly stays visible EVERY turn
        if self._turn_degraded:
            stats["degraded"] = True
        if self.assembly_disabled:
            stats["assembly_disabled"] = True
        self._emit({"type": "cm_view", "turn": self.turn, "mode": self.mode,
                    "ops": ops_brief, "stats": stats})
        self._view_emitted_turn = self.turn


def build_session(keys: Dict[str, Any], *, emit: Optional[Callable] = None,
                  usage_reader: Optional[Callable] = None,
                  policy_path: Optional[str] = None,
                  assembly_path: Optional[str] = None) -> CMSession:
    """Factory: already-clamped effective keys -> a ready session. Unknown or
    out-of-range values are IGNORED here (the clamp authority reported them
    parent-side); the safe defaults are the passthrough posture. The named
    assembly path is FRAMEWORK-RESOLVED (the snapshot/component tree), never
    a design value: keys carry only the name."""
    session = CMSession(emit=emit, usage_reader=usage_reader,
                        policy_path=policy_path, assembly_path=assembly_path)
    session.apply_keys(keys or {})
    return session
