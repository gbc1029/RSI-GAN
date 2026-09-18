"""Single source of truth for the frozen trust anchor (FRAMEWORK, frozen).

The freeze rule is: everything under ``gan/framework/`` is frozen by
construction, plus an explicit short list of *external* shared
substrate/measurement modules that cannot be moved into the framework (they are
part of the wider DGM-H codebase / domain layer).

``gan.build`` builds the AccessBroker deny list from here, so the deny list can
never silently diverge from the physical layout.
"""
from __future__ import annotations

import fnmatch
from typing import List

# Everything under this glob is frozen by construction.
FRAMEWORK_GLOB = "gan/framework/*"

# External frozen substrate / measurement. Kept in place on purpose:
# - agent/*     : shared LLM/tool runtime used by the whole DGM-H codebase.
# - domains/*   : the domain measurement apparatus (harness + report).
EXTERNAL_FROZEN: List[str] = [
    "agent/llm.py",
    "agent/llm_withtools.py",
    "agent/base_agent.py",
    "domains/harness.py",
    "domains/report.py",
]


def deny_paths() -> List[str]:
    """Deny list for the source-access gate (framework root + external frozen)."""
    return [FRAMEWORK_GLOB, *EXTERNAL_FROZEN]


def is_frozen(rel_path: str) -> bool:
    rel = str(rel_path).replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatch(rel, pat) for pat in deny_paths())
