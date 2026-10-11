"""Context-management core (docs/10 §5.2, §11; phase D).

The frozen pool-A implementation of the seam 三分架构: block model, the
primitive set, the per-turn assembly pipeline (fixed pre-segment -> optional
policy -> post-segment audits), the trajectory index builder and the
fail-open policy runner.

IMPORT CONTRACT (load-bearing): this package is copied VERBATIM into the task
sandbox each generation (``.gan_runtime/cm/``, the knowledge-base mechanism)
and must there import as the top-level package ``cm`` — so it depends on
**stdlib only**. No ``gan.*`` import anywhere in here; effective key values
are passed in (already clamped by the frozen ``gan.framework.cost_keys``
authority parent-side); events are emitted through an injected sink callable.
"""
from .blocks import (Block, Universe, View, PLACEHOLDER_OVERHEAD,
                       MIN_COMPACT_SAVE, EXCERPT_KEEP, HEAD_TAIL_KEEP,
                       estimate_tokens, cut, placeholder, render_form)
from .session import CMSession, ViewHandle, build_session, WINDOW_K_FLOOR
from .state import activate, deactivate, current_session, current_handle
from .traj_index import build_traj_index, render_index_text, segment_text

__all__ = [
    "Block", "Universe", "View", "PLACEHOLDER_OVERHEAD", "MIN_COMPACT_SAVE",
    "EXCERPT_KEEP", "HEAD_TAIL_KEEP", "estimate_tokens", "cut", "placeholder",
    "render_form",
    "CMSession", "ViewHandle", "build_session", "WINDOW_K_FLOOR",
    "activate", "deactivate", "current_session", "current_handle",
    "build_traj_index", "render_index_text", "segment_text",
]
