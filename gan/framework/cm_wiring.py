"""CM seat wiring — the ONE implementation shared by the loop and the role child.

Why this module exists
----------------------
The per-turn CM seam lives inside the chat loop, which runs in the ROLE SEAT
CHILD under driver mode (``gan/framework/role_process.py``). The parent (loop)
therefore cannot build the ``CMSession`` object: it cannot cross the IPC
boundary (it is not JSON, and the seam is consumed mid-conversation in the
child). Until this module existed the loop assigned ``role.cm_session`` /
``role.session_cm_mode`` onto the ``RoleProcess`` PROXY — dead attributes that
no child ever saw, so every planner/evaluator seat ran with no session at all
(``ctx_*`` faces answered "no context-management session is active").

The split of responsibility is deliberate:

* **The parent owns the policy inputs.** ``cm_*`` keys come from the role's own
  design through ``cost_keys.effective_all`` — the single clamp authority
  (docs/10 §10). It sends the ALREADY-CLAMPED keys plus the module STEMS, and
  it also records them in ``cost_keys_applied``. Sending keys (instead of
  letting the child re-derive them) keeps the audit record and the executed
  session on ONE source of truth.
* **The child owns the namespace.** A stem is not a path: the child resolves it
  workspace-first, then against its own read-only code projection. That is why
  the descriptor carries stems, not paths — parent and child mount the same
  ``output_dir`` at the same absolute path, but their ``code_root`` differs by
  construction (the child sees a projection built from ``code_root``).
* **The parent owns visibility.** The descriptor carries the trajectory files
  the seat may read, already filtered to those the CHILD can actually open
  (``child_visible_traj``): the seat's write/read roots are decided by
  ``role_broker`` and are narrower than the parent's view.

NOT part of the snapshot package
--------------------------------
``gan/framework/cm/`` is copied verbatim into the task sandbox as top-level
``cm`` and is stdlib-only by contract (``_CM_CORE_FILES`` in
``gan/framework/task_execution.py``). This module imports the framework (paths,
design/config conventions) and would break that contract, so it lives OUTSIDE
``cm/`` and never enters ``_CM_CORE_FILES``. The child imports it from its own
projection (``gan/framework`` IS projected — see ``role_broker``).

Addressing contract (one rule, three writers)
---------------------------------------------
The seat workspace is ``workspaces/<role>/<instance>__<seat>`` and the seat
trajectory root is ``trajectory/outer_<instance-suffix>/roles/<role>/<seat>``.
Both shapes were independently re-derived in ``role_process._prepare_paths``,
``role_broker.launch`` and ``BaseRole.traj_outer``; this module is the shared
formula for the CM wiring, and ``tests`` assert it against those writers.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Module stems: a design names a bare stem, never a path. The pattern is the
# same shape the CM method-name rules use (``cm.assembly._METHOD_NAME_RE``):
# lowercase, digit, underscore. Anything else — a slash, a dot, an absolute
# path, ``..`` — is refused. Without this, the new transport would itself
# re-open a traversal surface: the stem crosses a process boundary and is then
# joined into a filesystem path on the other side.
_STEM_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The one seat with NO workspace (``frozen.SEAT_WRITE["evaluator"]["evaluate"]``
# is empty and ``role_broker.launch`` does not bind a workspace for it), so the
# workspace-first probe is skipped rather than creating an ungranted directory.
NO_WORKSPACE_SEATS = frozenset({("evaluator", "evaluate")})

# Descriptor field set — EXACT, both directions. The descriptor crosses IPC in
# role_state, so both ends validate it (a silent schema drift here would look
# like "CM built nothing this session" instead of an error).
DESCRIPTOR_FIELDS = frozenset({
    "keys", "seat", "genid", "session_cm_mode",
    "assembly_stem", "policy_stem", "traj_files", "assembly_disabled",
    "policy_disabled",
})


# ---------------------------------------------------------------- small helpers
def clean_stem(stem: Any) -> Optional[str]:
    """Validate and normalize a design-named CM module stem.

    Returns the stem, or ``None`` when it is not a usable name. ``None`` is
    never an error: the caller reports it as an unresolved CLAIM (loud), which
    is the same posture as a claim that names a module the tree does not have.
    """
    if not isinstance(stem, str):
        return None
    cand = stem.strip()
    if not cand or not _STEM_RE.match(cand):
        return None
    return cand


def stem_problem(stem: Any) -> Optional[str]:
    """Why ``stem`` is unusable — a human sentence for the audit event."""
    if not isinstance(stem, str) or not stem.strip():
        return "empty stem"
    if not _STEM_RE.match(stem.strip()):
        return (f"refused stem {stem.strip()!r}: CM module names must match "
                f"^[a-z][a-z0-9_]*$ (no path separators, dots or '..')")
    return None


def workspace_node(instance: Any, seat: str) -> str:
    """The role workspace node for ONE seat: ``<instance>__<seat>``."""
    return f"{instance}__{seat}"


def traj_root(instance: Any, outer: Any = None) -> str:
    """The outer trajectory root label for an instance tag.

    The ONE rule shared by ``BaseRole.traj_outer`` (the writer),
    ``role_process._prepare_paths`` (pre-creation) and ``role_broker.launch``
    (the ACL write root): a suffixed (eval-round) instance ``outer_N_eval``
    reads ONLY its own tree; a plain ``outer_N`` keeps the main tree.
    """
    tag = str(instance or "")
    if tag.startswith("outer_"):
        return f"outer_{tag.split('_', 1)[1]}"
    return f"outer_{outer}"


def seat_traj_dir(output_dir: str, role: str, instance: Any, seat: str,
                  outer: Any = None) -> str:
    """Host directory holding THIS seat's own session trajectories (the one
    trajectory subtree the seat child can both see and write)."""
    return os.path.join(output_dir, "trajectory", traj_root(instance, outer),
                        "roles", role, seat)


def candidate_paths(role: str, stem: Any, *, committed_root: Optional[str] = None,
                    workspace_root: Optional[str] = None) -> List[str]:
    """Every path a design-named module COULD live at (no existence filter).

    The loop's assembly LATCH fingerprints the resolved bytes: both the live
    workspace copy and the committed tree are agent-editable, so both are
    candidates (a change in either must release the latch)."""
    cand = clean_stem(stem)
    if cand is None:
        return []
    rel = os.path.join("gan", "components", role, "cm", cand + ".py")
    out: List[str] = []
    if workspace_root:
        out.append(os.path.join(workspace_root, rel))
    if committed_root:
        out.append(os.path.join(committed_root, rel))
    return out


def resolve_stem(role: str, stem: Any, *,
                 committed_root: Optional[str],
                 workspace_root: Optional[str] = None,
                 ) -> Tuple[Optional[str], List[str]]:
    """Resolve a design-named CM module to a loadable path.

    Order: the seat's LIVE workspace copy first (that is the role's own
    self-edit surface for this outer), then the committed tree. Returns
    ``(path_or_None, tried)`` where ``tried`` lists the candidates probed in
    order — the caller reports a miss with exactly those paths, so a
    "claims a module the tree does not have" gap names where it looked.
    """
    cand = clean_stem(stem)
    if cand is None:
        return None, []
    rel = os.path.join("gan", "components", role, "cm", cand + ".py")
    tried: List[str] = []
    if workspace_root:
        tried.append(os.path.join(workspace_root, rel))
    if committed_root:
        tried.append(os.path.join(committed_root, rel))
    for path in tried:
        if os.path.isfile(path):
            return path, tried
    return None, tried


def child_visible_traj(files: Iterable[Any], *, output_dir: str, role: str,
                       instance: Any, seat: str, outer: Any = None,
                       ) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Split the seat's visible trajectory set into (materializable, skipped).

    ``materialize_traj`` reads whole files. Under driver mode the seat child
    can only open the trajectory subtree ``role_broker`` binds for it, so any
    file outside it must be DROPPED BEFORE the transport — otherwise the child
    would report an unreadable file and the parent would report a good one for
    the same session.

    Returns ``(kept, skipped_labels)``. The reasons are the caller's: this
    function answers only "is this path inside the seat's bind".
    """
    root = os.path.realpath(seat_traj_dir(output_dir, role, instance, seat,
                                          outer=outer))
    # A SECOND read-only root is bound for the self seat: the PREVIOUS outer's
    # own-session dir (docs/19 §12.11 decision Q1). Materializing "your last
    # outer" needs exactly that mount, so the filter accepts the same subtree
    # under any outer_<O> — nothing else.
    outers_root = os.path.join(os.path.realpath(output_dir), "trajectory")
    kept: List[Tuple[str, str]] = []
    skipped: List[str] = []
    for item in files or []:
        try:
            label, path = item
        except (TypeError, ValueError):
            continue
        if not label or not path:
            continue
        real = os.path.realpath(str(path))
        inside = real == root or real.startswith(root + os.sep)
        if not inside and seat == "self_improve" and real.startswith(outers_root + os.sep):
            rest = real[len(outers_root) + 1:].split(os.sep)
            if len(rest) >= 2 and rest[0].startswith("outer_"):
                tail = os.sep.join(rest[1:])
                inside = tail.startswith(
                    os.sep.join(("roles", role, seat)) + os.sep)
        if inside:
            if os.path.isfile(real):
                kept.append((str(label), str(path)))
                continue
        skipped.append(str(label))
    return kept, skipped


# ----------------------------------------------------------------- descriptor
def descriptor(role: str, keys: Optional[Dict[str, Any]], *,
               seat: str, instance: Any = None, genid: Any = None,
               session_cm_mode: Any = None,
               traj_files: Optional[List[Tuple[str, str]]] = None,
               assembly_disabled: bool = False,
               policy_disabled: bool = False,
               ) -> Optional[Dict[str, Any]]:
    """Build the JSON descriptor the seat child needs (parent side).

    ``keys`` already went through ``cost_keys.effective_all`` — this function
    NEVER clamps, it only filters to the ``cm_`` family and extracts the two
    named stems. ``None`` keys means "the parent has no keys for this seat":
    the child then runs without a seam, exactly like in-process.
    """
    if keys is None:
        return None
    cm_keys = {k: v for k, v in keys.items()
               if isinstance(k, str) and k.startswith("cm_")}
    asm = cm_keys.get("cm_assembly")
    pol = cm_keys.get("cm_policy")
    return {
        "keys": cm_keys,
        "seat": str(seat),
        "genid": None if genid is None else str(genid),
        "session_cm_mode": (session_cm_mode
                            if isinstance(session_cm_mode, str) else None),
        "assembly_stem": (clean_stem(asm)
                          if isinstance(asm, str) and asm.strip() else None),
        "policy_stem": (clean_stem(pol)
                        if isinstance(pol, str) and pol.strip() else None),
        "traj_files": [[str(label), str(path)]
                       for label, path in (traj_files or [])],
        # The loop's failure latch (docs/19 §12.14): the module stays
        # configured (so every cm_view still carries assembly_disabled) but it
        # is not loaded this session — it will be re-attempted as soon as its
        # configuration (keys or bytes) changes. ``policy_disabled`` is the
        # same contract for the custom-mode policy (docs/19 §12.17).
        "assembly_disabled": bool(assembly_disabled),
        "policy_disabled": bool(policy_disabled),
    }


def check_descriptor(value: Any) -> List[str]:
    """Schema problems for a received descriptor (child side).

    Strict on purpose: the descriptor decides whether a peer executes agent
    source this session, so a malformed one must be an ERROR, not a shrug. An
    empty list means the descriptor is usable (``None`` is valid = no seam).
    """
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"cm descriptor must be an object or null (got {type(value).__name__})"]
    out: List[str] = []
    # ``assembly_disabled``/``policy_disabled`` are purely ADDITIVE flags
    # (the loop's failure latch): a descriptor built before either existed is
    # still valid, so both are optional while every other field stays
    # required (strict on purpose).
    missing = sorted((DESCRIPTOR_FIELDS - {"assembly_disabled",
                                           "policy_disabled"}) - set(value))
    extra = sorted(set(value) - DESCRIPTOR_FIELDS)
    for flag in ("assembly_disabled", "policy_disabled"):
        if flag in value and not isinstance(value.get(flag), bool):
            out.append(f"cm descriptor {flag} must be a bool")
    if missing:
        out.append(f"cm descriptor missing {missing}")
    if extra:
        out.append(f"cm descriptor has unknown keys {extra}")
    if not isinstance(value.get("keys"), dict):
        out.append("cm descriptor keys must be an object")
    else:
        for k in value["keys"]:
            if not (isinstance(k, str) and k.startswith("cm_")):
                out.append(f"cm descriptor keys carries a non-cm key {k!r}")
    if not isinstance(value.get("seat"), str) or not value["seat"]:
        out.append("cm descriptor seat must be a non-empty string")
    if value.get("genid") is not None and not isinstance(value["genid"], str):
        out.append("cm descriptor genid must be a string or null")
    mode = value.get("session_cm_mode")
    if mode is not None and not isinstance(mode, str):
        out.append("cm descriptor session_cm_mode must be a string or null")
    for field in ("assembly_stem", "policy_stem"):
        v = value.get(field)
        if v is not None and (not isinstance(v, str) or clean_stem(v) != v):
            out.append(f"cm descriptor {field} is not a valid stem: {v!r}")
    files = value.get("traj_files")
    if not isinstance(files, list):
        out.append("cm descriptor traj_files must be a list")
    else:
        for item in files:
            if (not isinstance(item, list) or len(item) != 2
                    or not all(isinstance(v, str) and v for v in item)):
                out.append("cm descriptor traj_files entries must be [label, path] strings")
                break
    return out


def resolve_named(role: str, cm_keys: Dict[str, Any], *,
                  committed_root: Optional[str],
                  workspace_root: Optional[str],
                  ) -> Dict[str, Any]:
    """Resolve BOTH named slots for a set of already-clamped cm keys.

    Returns ``{"assembly_path", "policy_path", "assembly_stem", "policy_stem",
    "problems"}`` where ``problems`` is a list of
    ``{kind, stem, reason, tried}`` for every claim that could not be resolved
    — the caller turns each one into its own audit event (the loop emits
    ``cm_module_unresolved{kind}``; a miss is a claim-vs-tree gap and must
    SPEAK, never degrade silently).
    """
    out: Dict[str, Any] = {"assembly_path": None, "policy_path": None,
                           "assembly_stem": None, "policy_stem": None,
                           "problems": []}
    asm = cm_keys.get("cm_assembly")
    if isinstance(asm, str) and asm.strip():
        stem = clean_stem(asm)
        problem = stem_problem(asm)
        path, tried = ((None, []) if stem is None else
                       resolve_stem(role, stem, committed_root=committed_root,
                                    workspace_root=workspace_root))
        if stem is None:
            out["problems"].append({"kind": "assembly", "stem": asm.strip(),
                                    "reason": problem, "tried": []})
        elif path is None:
            out["problems"].append({"kind": "assembly", "stem": stem,
                                    "reason": "no workspace/committed module",
                                    "tried": tried})
        else:
            out["assembly_path"] = path
            out["assembly_stem"] = stem
    if cm_keys.get("cm_mode") == "custom":
        pol = cm_keys.get("cm_policy")
        if isinstance(pol, str) and pol.strip():
            stem = clean_stem(pol)
            path, tried = ((None, []) if stem is None else
                           resolve_stem(role, stem, committed_root=committed_root,
                                        workspace_root=workspace_root))
            if stem is None:
                out["problems"].append({"kind": "policy",
                                        "stem": pol.strip(),
                                        "reason": stem_problem(pol), "tried": []})
            elif path is None:
                out["problems"].append({"kind": "policy", "stem": stem,
                                        "reason": "no workspace/committed module",
                                        "tried": tried})
            else:
                out["policy_path"] = path
                out["policy_stem"] = stem
        else:
            out["problems"].append({
                "kind": "policy", "stem": None,
                "reason": ("cm_mode=custom without a policy stem "
                           "(cost.cm_policy is unset)"),
                "tried": [],
            })
    return out


# --------------------------------------------------------------------- builds
def build_from_descriptor(value: Dict[str, Any], *, role: str, output_dir: str,
                          code_root: Optional[str], emit: Optional[Any] = None,
                          ) -> Tuple[Any, Dict[str, Any]]:
    """Build the seat's session from a received descriptor (child side).

    Mirrors the loop's resolution semantics exactly, but resolves stems in the
    CHILD's own namespace: workspace-first (the role's live self-edit copy),
    then ``code_root`` (its read-only projection). A named module that cannot
    be resolved here is a loud CLAIM-vs-PROJECTION gap: ``cm_module_unresolved``
    for the assembly, mode rewritten to ``slice`` for a missing policy — the
    same degrade the loop performs, and the keys SAY it, so the session does
    not spend every turn failing on a missing policy.

    Returns ``(session_or_None, report)``. ``report`` carries what was resolved
    (paths, stems, problems, mode) — the caller emits the audit events; this
    function never writes to a sink it does not own.
    """
    from gan.framework.cm import build_session

    report: Dict[str, Any] = {"problems": [], "assembly_path": None,
                              "policy_path": None, "mode": None,
                              "traj_kept": 0, "traj_skipped": []}
    if value is None:
        return None, report
    problems = check_descriptor(value)
    if problems:
        report["problems"] = [{"kind": "descriptor", "stem": None,
                               "reason": "; ".join(problems), "tried": []}]
        return None, report
    keys = dict(value["keys"])
    seat = value["seat"]
    instance = os.environ.get("GAN_ROLE_INSTANCE") or None
    committed = code_root
    workspace_root = None
    if (role, seat) not in NO_WORKSPACE_SEATS:
        if instance:
            workspace_root = os.path.join(output_dir, "workspaces", role,
                                          workspace_node(instance, seat), "src")
        else:
            workspace_root = os.path.join(output_dir, "workspaces", role,
                                          f"__{seat}", "src")
    resolved = resolve_named(role, keys, committed_root=committed,
                             workspace_root=workspace_root)
    report["problems"] = resolved["problems"]
    report["assembly_path"] = resolved["assembly_path"]
    report["policy_path"] = resolved["policy_path"]
    report["assembly_stem"] = resolved["assembly_stem"]
    report["policy_stem"] = resolved["policy_stem"]
    # custom without a deliverable policy: the KEYS must say what actually
    # runs, or apply_keys would keep building a custom session whose every
    # turn fails on the missing policy.
    if keys.get("cm_mode") == "custom" and resolved["policy_path"] is None:
        keys["cm_mode"] = "slice"
    report["mode"] = keys.get("cm_mode")
    session = build_session(keys, policy_path=resolved["policy_path"],
                            assembly_path=resolved["assembly_path"],
                            emit=emit)
    if session is not None and value.get("assembly_disabled"):
        # Latched by the parent: keep the claim (``assembly_disabled`` still
        # shows on every cm_view) but do not load/re-run the module.
        session.assembly_disabled = True
    if session is not None and value.get("policy_disabled"):
        # Same latch contract for the custom-mode policy (docs/19 §12.17):
        # the claim stays (policy_path set), the module never loads.
        session.policy_disabled = True
    if session is not None:
        files = [(label, path) for label, path in (value.get("traj_files") or [])]
        kept, skipped = child_visible_traj(
            files, output_dir=output_dir, role=role,
            instance=instance, seat=seat,
            outer=_outer_from_instance(instance))
        report["traj_kept"] = len(kept)
        report["traj_skipped"] = skipped
        if kept:
            try:
                session.materialize_traj(kept)
            except Exception as exc:  # noqa: BLE001 -- session survives, speaks
                report["problems"].append({
                    "kind": "materialize", "stem": None,
                    "reason": f"traj materialization failed: {exc}"[:200],
                    "tried": [],
                })
    return session, report


def _outer_from_instance(instance: Any) -> Any:
    tag = str(instance or "")
    if tag.startswith("outer_"):
        try:
            return int(tag.split("_")[1])
        except (IndexError, ValueError):
            return None
    return None


def emit_summary(session: Any, *, role: str, seat: str, genid: Any,
                 event_path: str) -> Optional[Dict[str, Any]]:
    """Publish ONE session summary onto the events channel (child side).

    The role seat child CANNOT hand the summary back as a return value without
    changing three role result contracts (and ``role_process._validated_result``
    checks those by EXACT field set — a missing mirror rejects EVERY result).
    The event channel already exists for exactly this: the child's
    ``trajectory_log.append`` onto the framework events path is intercepted and
    forwarded to the parent as an IPC event, which the parent re-appends to its
    own ``logs/events.jsonl``.

    The payload is ``session.summary()`` VERBATIM (not a count-derived shape
    like ``cost_ledger.cm_summary_from_events``, which belongs to the task
    child's sidecar): the in-process and driver paths must produce the SAME
    ``cm`` node in ``cost_sessions.jsonl`` / the receipt, or the audit would
    depend on which transport ran.

    Returns the emitted record (the caller may reuse it), ``None`` when there
    is no session. A failed append must not break the session — but it is not
    silent either: the failure rides ``emit_failures`` on the session itself
    (``CMSession._emit`` counts them) and the caller reports the exception.
    """
    if session is None:
        return None
    try:
        summary = session.summary()
    except Exception:  # noqa: BLE001 -- audit-only enrichment
        return None
    record = {"type": "cm_session_summary", "role": role, "seat": seat,
              "genid": (None if genid is None else str(genid)),
              "summary": summary}
    from utils import trajectory_log
    trajectory_log.append(event_path, record)
    return record


def summary_of(role_obj: Any) -> Optional[Dict[str, Any]]:
    """The seat's settled CM summary, transport-agnostic.

    in-process: the session object is on the role instance, so read it live.
    driver: the seat child published ``cm_session_summary`` and the proxy
    stashed the last one (``RoleProcess.last_cm_summary``). Preferring the
    object keeps the in-process path byte-identical to pre-wiring behaviour.
    """
    session = getattr(role_obj, "cm_session", None)
    if session is not None:
        try:
            return session.summary()
        except Exception:  # noqa: BLE001 -- audit-only
            return None
    cached = getattr(role_obj, "last_cm_summary", None)
    return dict(cached) if isinstance(cached, dict) else None


def dumps(value: Any) -> str:
    """Stable JSON text for audit events (never raises on odd payloads)."""
    return json.dumps(value, ensure_ascii=False, default=str)
