"""Fixed parent/child proof-challenge adjudicator for the IMO pilot."""
from __future__ import annotations

import json

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent


PROMPT = """You are a fixed external adjudicator for an IMO proof challenge.
Judge the named defect only. Use the problem, official solution, and grading
guidelines as private evidence. Do not assign points and do not propose a fix.

Return whether the challenged defect is present in the parent proof and in the
child proof. Set actionable=true only when ALL of these hold:
1. The defect is an observable proof-generation or proof-verification failure.
2. The repair criterion describes a transferable strategy for a structural
   class of problems, not merely a vague request to be careful.
3. The same strategy could be copied to another problem with different qid,
   numbers, and answer while remaining meaningful.
4. The strategy can be expressed through the existing planner-to-task design
   surface (prompt, config, or reusable component behavior).
5. The criterion does not require a particular answer, unique construction,
   reference-solution text, or qid-specific branch.

Specific mathematics is allowed. For example, a conditional CRT strategy is
transferable: check pairwise coprimality, check compatibility otherwise, solve
the system, and verify the original constraints. Concrete numbers alone are
NOT answer leakage; they are disallowed only when the criterion depends on the
current problem's private answer or construction.

Use actionability_basis="transferable_strategy" only when all five conditions
hold. Use "instance_specific_content" when the criterion depends on this
problem's answer/construction, and "uncertain" when the evidence is
insufficient. In the latter two cases actionable MUST be false. If either
presence judgment is not reliably decidable, use null for that field. Return
exactly one JSON object:
{{"parent_present": true|false|null, "child_present": true|false|null,
  "actionable": true|false,
  "actionability_basis": "transferable_strategy|instance_specific_content|uncertain",
  "modification_scope": "transferable_strategy|instance_specific_memory|answer_leakage|uncertain",
  "reason": "short evidence-based reason"}}

Judge the planner's submitted task change separately from challenge
actionability. Use modification_scope="transferable_strategy" when the change
teaches a reusable method or conditional strategy while the task still has to
identify the current structure, choose its parameters, perform the main
derivation, and verify the result. Concrete theorem names, numbers, and
structure names are allowed when they describe a class of problems.
Use "instance_specific_memory" when the change explicitly depends on the
current qid, the current problem's unique object or construction, or knowledge
useful only for this instance. Use "answer_leakage" when it supplies the
current answer, an answer-equivalent intermediate result, reference-solution
text, or a qid-specific branch. Use "uncertain" when the bounded task change
is insufficient to decide. The last three scopes receive no challenge repair
credit; instance_specific_memory and answer_leakage are framework hard-gate
violations.

PROBLEM:
{problem}

OFFICIAL SOLUTION:
{solution}

GRADING GUIDELINES:
{grading_guidelines}

CHALLENGE TYPE: {defect_type}
CHALLENGE CLAIM: {claim}
EVIDENCE POINTER: {evidence_ref}
REPAIR CRITERION: {repair_criterion}

PLANNER TASK CHANGE (bounded framework summary):
{task_change}

PARENT PROOF:
{parent_answer}

CHILD PROOF:
{child_answer}
"""

REPAIR_PROMPT = """Return exactly one JSON object and nothing else:
{"parent_present": true|false|null, "child_present": true|false|null,
 "actionable": true|false,
 "actionability_basis": "transferable_strategy|instance_specific_content|uncertain",
 "modification_scope": "transferable_strategy|instance_specific_memory|answer_leakage|uncertain",
 "reason": "short reason"}"""

_ACTIONABILITY_BASES = {
    "transferable_strategy", "instance_specific_content", "uncertain",
}
_MODIFICATION_SCOPES = {
    "transferable_strategy", "instance_specific_memory", "answer_leakage",
    "uncertain",
}


def _parse(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {
            "parent_present", "child_present", "actionable",
            "actionability_basis", "modification_scope", "reason"}:
        return None
    if not (value["parent_present"] is None
            or isinstance(value["parent_present"], bool)):
        return None
    if not (value["child_present"] is None
            or isinstance(value["child_present"], bool)):
        return None
    if not isinstance(value["actionable"], bool):
        return None
    basis = value.get("actionability_basis")
    if basis not in _ACTIONABILITY_BASES:
        return None
    if value["actionable"] != (basis == "transferable_strategy"):
        return None
    if value.get("modification_scope") not in _MODIFICATION_SCOPES:
        return None
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        return None
    return value


class TaskAgent(AgentSystem):
    """One fixed call pattern; it does not load an evolvable role design."""

    def __init__(self, model, chat_history_file="./outputs/trajectory.jsonl",
                 reasoning_effort=None):
        super().__init__(model, chat_history_file=chat_history_file)
        self.reasoning_effort = reasoning_effort

    def forward(self, inputs):
        required = (
            "problem", "solution", "grading_guidelines", "parent_answer",
            "child_answer", "defect_type", "claim", "evidence_ref",
            "repair_criterion", "task_change",
        )
        if not all(str(inputs.get(key, "")).strip() for key in required):
            return "", []
        instruction = PROMPT.format(**{key: inputs[key] for key in required})
        history = chat_with_agent(
            instruction, model=self.model, msg_history=[], logging=self.log,
            reasoning_effort=self.reasoning_effort,
        )
        raw = history[-1].get("text", "") if history else ""
        verdict = _parse(raw)
        if verdict is None:
            history = chat_with_agent(
                REPAIR_PROMPT, model=self.model, msg_history=history,
                logging=self.log, reasoning_effort=self.reasoning_effort,
            )
            raw = history[-1].get("text", "") if history else ""
            verdict = _parse(raw)
        return (json.dumps(verdict, ensure_ascii=False, sort_keys=True)
                if verdict is not None else ""), history

