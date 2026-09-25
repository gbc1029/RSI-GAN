"""Tier-A unified fallback: report-and-continue, never silent.

The repository's failure taxonomy distinguishes failures that must ABORT a unit
(B tier: e.g. ``RepoIntegrityError``, ``PatchRejected``) from Tier-A
best-effort operations where continuing is correct -- as long as the failure is
VISIBLE. ``soft_fail`` is the single primitive for those sites: one stderr line
always, plus one structured ``events.jsonl`` record when the caller has an
events path (``gan.framework.paths.events_path(output_dir)`` -- passed as a
plain string so ``utils`` never imports ``gan``).

Event write failures are swallowed here (stderr already carried the message):
an audit-log outage must not escalate the very failure it is reporting.
"""
from __future__ import annotations

from utils import trajectory_log


def soft_fail(message: str, *, event_path: str = None, event_type: str = "soft_fail",
              **fields) -> None:
    """Report-and-continue for Tier-A degradations.

    ``message``   : human-readable one-liner (already includes where + error);
    ``event_path``: optional events.jsonl path (caller computes it via
                    ``gan.framework.paths.events_path(output_dir)`` when an
                    output dir is in scope);
    ``**fields``  : extra structured fields merged into the event record.
    """
    try:
        print(f"[WARN] {message}")
    except Exception:  # noqa: BLE001 -- printing must never be the failure
        pass
    if event_path:
        try:
            trajectory_log.append(event_path, {
                "type": event_type, "detail": str(message)[:300], **fields})
        except Exception:  # noqa: BLE001 -- audit write must not mask the cause
            pass
