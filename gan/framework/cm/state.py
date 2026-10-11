"""Session-state contextvar: how ctx_* wrapper tools reach the LIVE session.

The chat loop (agent/llm_withtools) holds the CMSession object it was given
(``cm=`` parameter) — the object's own methods set/refresh this contextvar
around each turn, so the wrapper components (which run inside
``process_tool_call`` in the same thread) can resolve the session without
importing anything from ``agent/``. stdlib-only (snapshot import contract).
"""
from __future__ import annotations

import contextvars
from typing import Any, Optional

from .session import CMSession

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("gan_cm_session",
                                                          default=None)


def activate(session: CMSession) -> Any:
    """Bind a session to the current context (called by the chat loop)."""
    return _CURRENT.set(session)


def deactivate(token: Any) -> None:
    if token is not None:
        _CURRENT.reset(token)


def current_session() -> Optional[CMSession]:
    session = _CURRENT.get()
    return session if isinstance(session, CMSession) else None


def current_handle(face: str = "agent"):
    """A ViewHandle for the active session, or None (tools degrade loudly)."""
    session = current_session()
    if session is None:
        return None
    from .session import ViewHandle
    return ViewHandle(session, face)
