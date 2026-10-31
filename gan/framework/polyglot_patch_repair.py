"""Parent-side patch pre-flight + repair for the polyglot eval bridge (Arch 2).

Field audit small_1028_0046 (2026-10-28): 31/90 predictions died at the eval
container's ``git apply`` — 19 python patches poisoned by ``__pycache__/*.pyc``
hunks (NUL bytes survive decode), 5 rust patches carrying the whole cargo
``target/`` tree, 6 cpp/java patches corrupted by no-trailing-newline glue
(the difflib generator emitted the bare last line, which physically joined the
next line and desynced the hunk line counts), 1 js new-file collision
(package.json already existed in the container). Every failure surfaced only
as one error line in an unread docker log.

The generator is now solution-scoped git-serialized (domains/task_worker.py);
this module is the parent-side gate that

1. reproduces the eval container's apply gate BEFORE spending a docker
   container (``git apply --check`` against a seed repo built from the
   exercise snapshot — the same starter bytes the container checks out);
2. repairs what is provably repairable and nothing else:
   - drop stanzas whose target is not an official solution file (the eval
     side discards them anyway via its stash/reset anti-tamper step);
   - for a solution file whose starter lacks the trailing newline, replay
     the patch's old side against the known starter bytes (byte-exact or
     abort), re-derive the agent's new-side content, and re-emit the stanza
     with per-line ``\\ No newline at end of file`` markers;
3. records the per-question outcome for report.json (ok / repaired /
   rejected / blank) — a rejected patch is NOT dispatched: the eval scores
   the pristine exercise exactly as before, but the report now says WHY.

Repair validates every candidate with the same ``git apply --check`` the
container runs; the dispatch set only ever contains patches that passed it.
Seed repos are read-only under ``--check`` and cached per exercise for the
whole run.

Run: stdlib-only; offline; no docker, no LLM.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

_STATUS_OK = "ok"
_STATUS_REPAIRED = "repaired"
_STATUS_REJECTED = "rejected"

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def split_file_stanzas(patch: str) -> List[str]:
    """Split a concatenated multi-file unified diff into per-file texts.

    A file header is a ``--- `` line immediately followed by a ``+++ `` line;
    a hunk-internal deleted line cannot produce that adjacent pair, so the
    split is deterministic for the generator's output shape.
    """
    lines = patch.split("\n")
    idx = [i for i, l in enumerate(lines)
           if l.startswith("--- ") and i + 1 < len(lines)
           and lines[i + 1].startswith("+++ ")]
    parts: List[str] = []
    for k, i in enumerate(idx):
        j = idx[k + 1] if k + 1 < len(idx) else len(lines)
        text = "\n".join(lines[i:j])
        if not text.endswith("\n"):
            text += "\n"
        parts.append(text)
    return parts


def stanza_target(stanza: str) -> Tuple[Optional[str], Optional[str]]:
    """(old_path, new_path) of a file stanza; /dev/null when absent."""
    old = new = None
    for line in stanza.split("\n"):
        if line.startswith("--- "):
            old = _strip_path(line[4:])
        elif line.startswith("+++ "):
            new = _strip_path(line[4:])
            break
    return old, new


def _strip_path(path: str) -> Optional[str]:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_hunks(stanza: str) -> List[Tuple[int, List[str]]]:
    """[(old_start_1based, body_lines), ...] of one file stanza."""
    lines = stanza.split("\n")
    hunks: List[Tuple[int, List[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@ "):
            match = _HUNK_RE.match(line)
            start = int(match.group(1)) if match else 1
            body: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@ ", "--- ")):
                if lines[i] != "":          # trailing '' from split("\n")
                    body.append(lines[i])
                i += 1
            hunks.append((start, body))
            continue
        i += 1
    return hunks


def _replay_reconstruct(old_text: str, hunks: List[Tuple[int, List[str]]]
                        ) -> Tuple[Optional[List[str]], Optional[str]]:
    """Re-derive the agent's new-side lines by replaying the hunks against
    the known starter bytes, repairing a glued no-trailing-newline line.

    Returns (new_lines, None) or (None, error_reason). The old-side replay
    must consume the starter EXACTLY (every context/deletion line matched,
    the hunk positions aligned) -- anything else aborts the repair: we never
    invent content.
    """
    old_lines = old_text.splitlines(keepends=True)
    out: List[str] = []
    old_pos = 1                                 # 1-based next starter line
    for start, body in hunks:
        if start > old_pos:
            out.extend(old_lines[old_pos - 1:start - 1])
            old_pos = start
        for bl in body:
            if bl.startswith("\\ No newline"):
                continue
            tag, rest = bl[:1], bl[1:]
            if tag not in (" ", "-", "+"):
                return None, f"unexpected body line {bl[:40]!r}"
            at_last = (old_pos == len(old_lines)
                       and not old_lines[old_pos - 1].endswith("\n"))
            if at_last:
                core = old_lines[old_pos - 1].rstrip("\n")
                if rest == core:                # bare old-last-line, ends body
                    old_pos += 1
                    if tag == " ":
                        out.append(core)        # bare context at EOF
                    continue
                if rest.startswith(core) and len(rest) > len(core):
                    # GLUED: the bare old line joined the next logical line
                    tail = rest[len(core):]
                    old_pos += 1
                    if tag == " ":
                        out.append(core + "\n")  # context, newline restored
                    t2, r2 = tail[:1], tail[1:]
                    if t2 == "+":
                        out.append(r2 + "\n")
                    elif t2 == " ":
                        out.append(r2 + "\n")
                    else:
                        return None, f"glued tail tag {t2!r}"
                    continue
            if tag in (" ", "-"):
                if old_pos > len(old_lines):
                    return None, "old-side overrun"
                old_pos += 1
                if tag == " ":
                    out.append(rest if rest.endswith("\n") else rest + "\n")
            else:
                out.append(rest if rest.endswith("\n") else rest + "\n")
    out.extend(old_lines[old_pos - 1:])
    if old_pos - 1 != len(old_lines):
        return None, f"old-side replay mismatch ({old_pos - 1}/{len(old_lines)})"
    return out, None


def _reemit(old_path: str, new_path: str, old_lines: List[str],
            new_lines: List[str]) -> str:
    """Re-emit one file stanza with per-line no-newline markers (what the
    difflib generator should have produced)."""
    delta = difflib.unified_diff(old_lines, new_lines,
                                 fromfile=f"a/{old_path}", tofile=f"b/{new_path}")
    out: List[str] = []
    for line in delta:
        if line.startswith(("--- ", "+++ ", "@@ ")):
            out.append(line if line.endswith("\n") else line + "\n")
        elif line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)


class PatchPreflight:
    """Per-run gate: seed-repo cache + preflight/repair per question."""

    def __init__(self, dataset_root: str):
        self.dataset_root = os.path.abspath(dataset_root)
        self._metadata = self._load_metadata(dataset_root)
        self._by_id = {str(e.get("instance_id")): e
                       for e in self._metadata if e.get("instance_id")}
        self._repos: Dict[str, str] = {}
        self._tmp_root = tempfile.mkdtemp(prefix="polyglot_preflight_")
        self._env = dict(os.environ,
                         GIT_CONFIG_GLOBAL="/dev/null",
                         GIT_CONFIG_SYSTEM="/dev/null",
                         GIT_TERMINAL_PROMPT="0")
        self.counts: Dict[str, int] = {}

    # -- metadata ----------------------------------------------------------
    @staticmethod
    def _load_metadata(dataset_root: str) -> List[Dict[str, Any]]:
        path = os.path.join(dataset_root, "domains", "polyglot",
                            "polyglot_benchmark_metadata.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def solution_paths(self, instance_id: str) -> List[str]:
        entry = self._by_id.get(instance_id) or {}
        return [str(p) for p in (entry.get("files", {}) or {}).get("solution", [])]

    # -- seed repo ---------------------------------------------------------
    def _git(self, cwd: str, *args: str, check: bool = False
             ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", "-C", cwd,
             "-c", "user.name=patch-base", "-c", "user.email=patch-base@invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
            env=self._env, capture_output=True, text=True, check=False)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: "
                               f"{(proc.stderr or proc.stdout).strip()[:200]}")
        return proc

    def _seed_repo(self, instance_id: str) -> str:
        repo = self._repos.get(instance_id)
        if repo:
            return repo
        entry = self._by_id.get(instance_id)
        if not entry:
            raise KeyError(f"unknown polyglot instance: {instance_id}")
        src = os.path.join(os.path.abspath(self.dataset_root), entry["repo"])
        repo = os.path.join(self._tmp_root, instance_id.replace("/", "__"))
        shutil.copytree(src, repo)
        shutil.rmtree(os.path.join(repo, ".git"), ignore_errors=True)
        self._git(repo, "init", "-q", check=True)
        self._git(repo, "add", "-A", check=True)
        self._git(repo, "commit", "-qm", "patch-base", check=True)
        self._repos[instance_id] = repo
        return repo

    # -- gate --------------------------------------------------------------
    def _check(self, repo: str, patch: str) -> Tuple[bool, str]:
        proc = subprocess.run(
            ["git", "-C", repo, "apply", "--check", "--whitespace=nowarn", "-"],
            input=patch, env=self._env, capture_output=True, text=True)
        err = (proc.stderr or proc.stdout).strip()
        return proc.returncode == 0, err.splitlines()[-1][:200] if err else ""

    def preflight(self, instance_id: str, patch: str
                  ) -> Tuple[str, str, Dict[str, Any]]:
        """Gate one prediction. Returns (status, dispatch_patch, info).

        status: ok (passes as-is) / repaired (passed after repair) /
        rejected (still fails; dispatch nothing). The caller dispatches
        dispatch_patch only for ok/repaired; '' otherwise (the eval then
        scores the pristine exercise, recorded as empty_patch, exactly the
        pre-gate behavior for a corrupt patch -- minus the silent mystery).
        """
        if not patch.strip():
            return "blank", "", {}
        repo = self._seed_repo(instance_id)
        ok, err = self._check(repo, patch)
        if ok:
            self._count(_STATUS_OK)
            return _STATUS_OK, patch, {}
        info: Dict[str, Any] = {"gate_error": err}
        fixed, repair_info = self._repair(instance_id, patch, repo)
        if fixed is None:
            info.update(repair_info or {})
            self._count(_STATUS_REJECTED)
            return _STATUS_REJECTED, "", info
        ok2, err2 = self._check(repo, fixed)
        if not ok2:
            info["residual_error"] = err2
            self._count(_STATUS_REJECTED)
            return _STATUS_REJECTED, "", info
        info.update(repair_info)
        self._count(_STATUS_REPAIRED)
        return _STATUS_REPAIRED, fixed, info

    def _repair(self, instance_id: str, patch: str, repo: str
                ) -> Tuple[Optional[str], Dict[str, Any]]:
        solution = set(self.solution_paths(instance_id))
        kept: List[str] = []
        dropped: List[str] = []
        rebuilt: List[str] = []
        rebuild_failed: Dict[str, str] = {}
        for stanza in split_file_stanzas(patch):
            old, new = stanza_target(stanza)
            target = new or old
            if target and target not in solution:
                dropped.append(target)
                continue
            old_text = None
            if old and os.path.isfile(os.path.join(repo, old)):
                with open(os.path.join(repo, old), "rb") as f:
                    raw = f.read()
                if b"\x00" not in raw[:8192]:       # never textualize binary
                    old_text = raw.decode("utf-8", errors="replace")
            if old_text is not None and not old_text.endswith("\n"):
                new_lines, err = _replay_reconstruct(
                    old_text, [(s, b) for s, b in parse_hunks(stanza)])
                if err is None:
                    rebuilt.append(old or "")
                    kept.append(_reemit(old, new, old_text.splitlines(keepends=True),
                                        new_lines))
                    continue
                kept.append(stanza)                 # unrepaired: the gate decides
                if old:
                    rebuild_failed[old] = err
                continue
            kept.append(stanza)
        info = {"dropped_files": dropped, "rebuilt_files": rebuilt,
                "rebuild_failed": rebuild_failed}
        return "".join(kept), info

    # -- bookkeeping -------------------------------------------------------
    def _count(self, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1

    def summary(self) -> Dict[str, Any]:
        data = dict(self.counts)
        data["total_gated"] = sum(self.counts.values())
        return data

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp_root, ignore_errors=True)
