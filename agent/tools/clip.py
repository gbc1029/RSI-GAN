"""Shared output-clipping helpers — the SINGLE SOURCE of clip math + marker text.

Two layers cut tool output in this framework, and their markers are
deliberately DISTINCT so an audit (trajectory row / run log) can attribute
WHICH layer fired (the small_1031_110220 audit showed a double cut — render
120k then loop 16k — going unnoticed for a full round because both layers'
texts looked interchangeable):

* inner (tool-level, per-tool cap)      -> ``[clipped: ...]``
* outer (chat-loop budget, llm_withtools) -> ``[truncated by output budget: ...]``
  (built in ``agent.llm_withtools._outer_clip``, not here — it carries the
  loop-level advice line and the audit contract)

Every seat carries ``agent/`` (the task sandbox copies ``agent``; role seats
import it natively), so ``gan/tools`` wrappers (work/common, design, the task
components) import the SAME helpers instead of growing per-tool marker
dialects. STDLIB-ONLY: snapshot-safe for the task sandbox.
"""
from __future__ import annotations

# Marker tag tokens (single source; the guard test regexes on these).
INNER_TAG = "clipped"
OUTER_TAG = "truncated by output budget"


def clip_head(text, cap: int) -> str:
    """Keep the FIRST ``cap`` chars; append a counted inner marker when cut.

    Boundary-exact: ``len(text) == cap`` returns the text unchanged (no
    marker), so a caller may pass its own ceiling directly.
    """
    text = str(text or "")
    if len(text) <= cap:
        return text
    return (text[:cap]
            + f"\n...[{INNER_TAG}: showing first {cap} of {len(text)} chars "
              f"(limit {cap})]")


def clip_head_tail(text, head: int, tail: int) -> str:
    """Keep head+tail (composite cap); counted middle-omission marker when cut.

    Used where the END of the output carries the payload (bash: benchmark
    verdicts / build summaries print last). Boundary-exact: any total
    ``<= head + tail`` is returned unchanged.
    """
    text = str(text or "")
    total = len(text)
    if total <= head + tail:
        return text
    omitted = total - head - tail
    return (text[:head]
            + f"\n...[{INNER_TAG}: middle {omitted} of {total} chars omitted "
              f"(kept head {head} + tail {tail})]\n"
            + text[-tail:])
