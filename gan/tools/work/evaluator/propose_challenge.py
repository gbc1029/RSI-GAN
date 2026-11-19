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
"""
from gan.framework.context import challenge_config, get_eval_context


def tool_info():
    return {
        "name": "propose_challenge",
        "description": (
            "Author ONE challenge about a defect you found: name the defect class and "
            "the task family, and state what would count as repaired. The framework "
            "draws the actual instance from the environment's own generator, so do NOT "
            "write a question, a seed or an answer. Available only in the blind phase "
            "and only when the run enables challenge authoring."
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
            },
            "required": ["defect_class", "family", "evidence_ref", "repair_criterion"],
        },
    }


def tool_function(defect_class, family, evidence_ref, repair_criterion, **kwargs):
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
    # Vocabulary check HERE (cheap, actionable) rather than only at admission:
    # the closed set is the domain's, so the agent must be told the exact values
    # it may use instead of discovering them through a rejected round.
    from gan.framework.challenge_adjudication import supported_classes_for
    allowed = supported_classes_for(config["domain"] or "")
    if allowed and str(defect_class) not in allowed:
        return (f"Error: unknown defect_class '{defect_class}'; allowed for this "
                f"domain: {', '.join(sorted(allowed))}")
    if getattr(ctx, "revealed", False):
        # Blind discipline: a post-reveal proposal would be fitted to the score.
        return ("Error: challenges may only be proposed in the BLIND phase; the "
                "benchmark score has already been revealed")
    used = len(getattr(ctx, "challenges", []) or [])
    if used >= config["max_k"]:
        return f"Error: challenge budget exhausted (max_k={config['max_k']})"
    ctx.add_challenge({
        "defect_class": str(defect_class),
        "family": str(family),
        "evidence_ref": str(evidence_ref),
        "repair_criterion": str(repair_criterion),
    })
    return (f"challenge recorded ({used + 1}/{config['max_k']}): "
            f"{defect_class} on {family}. The framework will draw the instance; "
            "you cannot choose or see it.")


op_info = tool_info
op_function = tool_function
