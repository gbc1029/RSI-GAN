"""Fixed parent/child proof-challenge adjudicator for the IMO pilot."""
from __future__ import annotations

import json

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent


PROMPT = """You are a fixed external adjudicator for an IMO proof challenge.
Judge the named defect only. Use the problem, official solution, and grading
guidelines as private evidence. Do not assign points and do not propose a fix.

Return whether the challenged defect is present in the parent proof and in the
child proof. Set actionable=true only when the challenge is specific, relevant
to the task behavior, and can be addressed by a general proof-generation
strategy rather than a one-question answer. If either presence judgment is not
reliably decidable, use null for that field. Return exactly one JSON object:
{{"parent_present": true|false|null, "child_present": true|false|null,
  "actionable": true|false, "reason": "short evidence-based reason"}}

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

PARENT PROOF:
{parent_answer}

CHILD PROOF:
{child_answer}
"""

REPAIR_PROMPT = """Return exactly one JSON object and nothing else:
{"parent_present": true|false|null, "child_present": true|false|null,
 "actionable": true|false, "reason": "short reason"}"""


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
            "parent_present", "child_present", "actionable", "reason"}:
        return None
    if not (value["parent_present"] is None
            or isinstance(value["parent_present"], bool)):
        return None
    if not (value["child_present"] is None
            or isinstance(value["child_present"], bool)):
        return None
    if not isinstance(value["actionable"], bool):
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
            "repair_criterion",
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

