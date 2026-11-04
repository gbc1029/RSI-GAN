"""Balrog environment wrappers.

``NLETimeLimit`` is exported LAZILY on purpose: it needs the NetHack Learning
Environment (``import nle``), a heavy dependency with its own data package that
only the ``nle`` / ``minihack`` environments use. Importing it at module level
made ``from domains.balrog.environments.wrappers import GymV21CompatibilityV0``
-- the babaisai path -- drag ``nle`` in and die with ``ModuleNotFoundError``,
so babaisai was un-importable even with its own dependencies installed
(measured 2026-11-03, P0-4). PEP 562 keeps ``from ...wrappers import
NLETimeLimit`` working for the NLE/minihack envs, which import it explicitly.
"""

from domains.balrog.environments.wrappers.gym_compatibility import GymV21CompatibilityV0

__all__ = ["NLETimeLimit", "GymV21CompatibilityV0"]


def __getattr__(name):
    if name == "NLETimeLimit":
        from domains.balrog.environments.wrappers.nle_timelimit import NLETimeLimit

        return NLETimeLimit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
