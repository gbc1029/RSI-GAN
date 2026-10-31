"""Cleanup that never kills the process: best-effort sweep + explicit record.

Field audit small_1030_095410: ``shutil.rmtree(..., ignore_errors=True)`` left
root-owned polyglot eval payloads (``gan_polyglot_0/``) silently in
``work/<genid>/`` — the run could not clean them and nothing said so; the bare
``rmtree`` in ``prepare_run_dir`` could instead have killed the whole task run
on the same residue. Both shapes are wrong in opposite directions.

Policy: try one full ``rmtree``; on failure sweep whatever is still deletable
(deepest-first) and RECORD the survivors — a ``soft_fail`` event when an event
path is given, stderr otherwise. This function never raises.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Optional


def safe_rmtree(path: str, *, label: str,
                event_path: Optional[str] = None) -> bool:
    """Remove ``path`` best-effort; record failures, never raise.

    Returns True when ``path`` is fully gone. On partial/failed removal the
    survivors are named in the record (first 10 + count) so the operator can
    clean them and the run audit shows WHY the residue exists.
    """
    if not os.path.exists(path) and not os.path.islink(path):
        return True
    first_err_desc = ""
    try:
        shutil.rmtree(path)
        return True
    except Exception as exc:  # noqa: BLE001 -- recorded below, never re-raised
        # Do NOT keep the exception object past this block: Python unbinds the
        # `as` name on block exit, and the old `first_err` reference at the
        # `detail` line raised UnboundLocalError -- turning this never-raise
        # helper into a run killer (smoke_1030_195145, docs/17 §13).
        first_err_desc = f"{type(exc).__name__}: {exc}"
    survivors: List[str] = []
    for current, dirs, files in os.walk(path, topdown=False):
        for name in list(files) + list(dirs):
            target = os.path.join(current, name)
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    os.rmdir(target)
                else:
                    os.remove(target)
            except OSError:
                survivors.append(target)
    detail = (f"{first_err_desc}; "
              f"survivors: {sorted(survivors)[:10]}"
              + (f" (+{len(survivors) - 10} more)" if len(survivors) > 10 else ""))
    if event_path:
        try:
            from utils.soft_fail import soft_fail
            soft_fail(f"{label}: cleanup failed — {detail}",
                      event_path=event_path, event_type="rmtree_failed",
                      path=path)
        except Exception:
            print(f"[WARN] {label}: cleanup failed — {detail}", flush=True)
    else:
        print(f"[WARN] {label}: cleanup failed — {detail}", flush=True)
    return not os.path.exists(path)
