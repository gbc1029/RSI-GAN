"""propose_challenge - the evaluator authors a challenge about a defect it FOUND.

WHAT THE MODEL MAY DECIDE, AND WHAT IT MAY NOT
----------------------------------------------
The evaluator names a ``(defect_class, family)`` pair plus a repair criterion. It
does NOT write a question, does NOT pick a seed and never sees the instance: the
framework materializes a same-family episode pair from the environment's own
generator (docs/21 §4, "L1"). That is what makes the resulting question
benchmark-consistent by construction and impossible to fit to.

WHY IT IS BLIND-ONLY
--------------------
The challenge is locked BEFORE the benchmark score is revealed: a proposal made
after the reveal could be chosen to explain the score instead of to describe the
defect. The ledger's blind lock is the second gate -- this tool refuses earlier,
so the model gets a clear reason instead of a silently dropped row.

WHY ADMISSION IS NOT JUDGED HERE
-------------------------------
The mechanical checks (closed vocabulary, family really showed the defect, K
budget, duplicates) run in ``gan/framework/challenge_questions.py`` when the loop
locks the proposal. Doing them here would need revealed evidence (the score) and
would blur admission with truth -- the mistake that made the IMO pilot's
``actionable`` flag vacuous.

SOFT BINDING TO AN ISSUE (docs/21 §19, 2026-11-22)
--------------------------------------------------
A challenge is *about a defect the evaluator FOUND*, so it should be traceable to
the issue that recorded that finding. It used to be traceable to nothing: the
proposal had no link, and the ledger had no field, so "did the challenge come
from this diagnosis" was unanswerable and the two channels could only be
compared as two unrelated sample sets.

``issue_id`` is therefore REQUIRED to be CONSIDERED but allowed to be EMPTY:

* given -> it must name an issue reported in this same round, and that issue's
  ``issue_key`` must equal ``defect_class`` (a challenge about a defect the
  author never reported, or reported under another key, is refused here so the
  author gets a clear reason instead of a silently unlinked row);
* empty  -> the proposal is still accepted and recorded as UNLINKED. Soft on
  purpose: measured 2026-11-22, a hard requirement would have refused the ONE
  proposal a 3-outer run produced (the citable family and the reported keys did
  not line up), and the join RATE is the number we actually want to watch.
"""
from gan.framework.context import challenge_config, get_eval_context


def _bare(issue_id: str) -> str:
    """Compare handles generation-agnostically (``g3/i-1`` and ``i-1`` are one)."""
    text = str(issue_id or "").strip()
    head, sep, tail = text.partition("/")
    return tail if (sep and head.startswith("g")) else text


def tool_info():
    return {
        "name": "propose_challenge",
        "description": (
            "Author ONE challenge about a defect you found: name the defect class and "
            "the task family, state what would count as repaired, and cite the issue "
            "you reported for it. The framework draws the actual instance from the "
            "environment's own generator, so do NOT write a question, a seed or an "
            "answer. Available only in the blind phase and only when the run enables "
            "challenge authoring."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "defect_class": {
                    "type": "string",
                    "description": "Which defect this tests, from the vocabulary your "
                                   "domain can decide mechanically.",
                },
                "family": {
                    "type": "string",
                    "description": "The task family where YOU observed this defect in "
                                   "the generation under review. It must be a family "
                                   "whose episodes actually showed it.",
                },
                "evidence_ref": {
                    "type": "string",
                    "description": "Pointer to where you saw it (episode id or report "
                                   "path). A pointer, not an argument.",
                },
                "repair_criterion": {
                    "type": "string",
                    "description": "Mechanical condition that would count as repaired "
                                   "(e.g. 'the episode stops cycling and does not lose "
                                   "progression').",
                },
                "issue_id": {
                    "type": "string",
                    "description": "The local handle of the issue you reported for "
                                   "THIS defect in this round (i-1, i-2, ...). Its "
                                   "issue_key must equal defect_class. Leave it EMPTY "
                                   "if you reported no issue for this defect -- the "
                                   "challenge is then recorded as unlinked.",
                },
            },
            "required": ["defect_class", "family", "evidence_ref", "repair_criterion"],
        },
    }


def tool_function(defect_class, family, evidence_ref, repair_criterion,
                  issue_id="", **kwargs):
    ctx = get_eval_context()
    if ctx is None:
        return "Error: no eval context"
    config = challenge_config()
    if not config["enabled"] or not config["max_k"]:
        return ("Error: challenge authoring is disabled for this run "
                f"({config['reason'] or 'challenge.enabled is false'})")
    if not config["authorable"]:
        return (f"Error: this domain cannot host challenges ({config['reason']}); "
                "use report_issue instead")
    # Closed vocabularies from the MIRRORED config (computed worker-side, where
    # the domain package is importable). FAIL CLOSED: an empty list means the
    # values were not wired through, NOT "anything goes" -- the first real round
    # (2026-11-19) fail-opened exactly that way and the author's plausible-but-
    # wrong `defect_class` reached admission instead of being corrected here.
    allowed = list(config.get("defect_classes") or [])
    families = list(config.get("families") or [])
    if not allowed or not families:
        return ("Error: challenge authoring is not fully wired for this run "
                f"(allowed defect classes={allowed or 'none'}, "
                f"families={families or 'none'}); use report_issue instead")
    if str(defect_class) not in allowed:
        return (f"Error: unknown defect_class '{defect_class}'; allowed for this "
                f"domain: {', '.join(allowed)}")
    # Citable families for THIS generation (the mechanical per-episode primitive
    # already ran on its report). Measured 2026-11-19: the author cited a family
    # whose defect it saw in the trajectory but which the primitive did not
    # confirm, and admission refused it -- nobody had told it which family was
    # supportable. An empty table means "nothing to challenge this round".
    citable = (config.get("citable") or {}).get(str(defect_class)) or {}
    if not citable:
        return (f"Error: no family in this generation shows '{defect_class}' "
                "mechanically; do not file a challenge this round (use report_issue)")
    if str(family) not in citable:
        listing = ", ".join(f"{fam} ({n} episode(s))"
                            for fam, n in sorted(citable.items()))
        return (f"Error: family '{family}' does not show {defect_class} in this "
                f"generation; citable families are: {listing}")
    if str(family) not in families:
        return (f"Error: unknown family '{family}'; allowed families (task ids): "
                f"{', '.join(families)}")
    if getattr(ctx, "revealed", False):
        # Blind discipline: a post-reveal proposal would be fitted to the score.
        return ("Error: challenges may only be proposed in the BLIND phase; the "
                "benchmark score has already been revealed")
    used = len(getattr(ctx, "challenges", []) or [])
    if used >= config["max_k"]:
        return f"Error: challenge budget exhausted (max_k={config['max_k']})"
    # SOFT BINDING (see the module docstring): resolve the cited issue against the
    # issues reported in THIS round. A dangling handle is refused (it would look
    # like a link that does not exist); a key mismatch is refused with both values
    # named; an EMPTY handle is accepted and recorded as unlinked.
    links: list = []
    raw_issue = str(issue_id or "").strip()
    wanted = _bare(raw_issue)
    if wanted:
        for it in (getattr(ctx, "issues", []) or []):
            if isinstance(it, dict) and _bare(it.get("issue_id")) == wanted:
                links.append(it)
    if wanted and not links:
        reported = [str(it.get("issue_id")) for it in (getattr(ctx, "issues", []) or [])
                    if isinstance(it, dict) and it.get("issue_id")]
        return (f"Error: issue_id '{raw_issue}' names no issue reported in this round "
                f"(reported: {', '.join(reported) or 'none'}). Omit issue_id to file "
                "an unlinked challenge.")
    if links:
        keys = sorted({str(it.get("issue_key") or "") for it in links})
        if str(defect_class) not in keys:
            return (f"Error: issue '{raw_issue}' was reported as "
                    f"{', '.join(k or '(none)' for k in keys)}, not '{defect_class}'. "
                    "A challenge must test the defect its issue actually claims "
                    "(omit issue_id to file it unlinked).")
    ctx.add_challenge({
        "defect_class": str(defect_class),
        "family": str(family),
        "evidence_ref": str(evidence_ref),
        "repair_criterion": str(repair_criterion),
        # The LOCAL handle as the author wrote it; the loop qualifies it and the
        # ledger stores both it and the resolved issue_key.
        "issue_id": raw_issue,
        "issue_key": (str(defect_class) if links else ""),
    })
    state = f"linked to issue '{raw_issue}'" if links else "UNLINKED (no issue cited)"
    return (f"challenge recorded ({used + 1}/{config['max_k']}): "
            f"{defect_class} on {family}, {state}. The framework will draw the "
            "instance; you cannot choose or see it.")


op_info = tool_info
op_function = tool_function
