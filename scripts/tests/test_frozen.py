"""Frozen-layout invariants (run: python scripts/tests/test_frozen.py).

Guards the trust anchor after moving GAN's frozen machinery into gan/framework/:
  1. old top-level paths no longer exist;
  2. the deny list is derived from a single source (gan.framework.frozen) and
     equals [FRAMEWORK_GLOB, *EXTERNAL_FROZEN];
  3. every deny entry is either under gan/framework or an explicit external;
  4. frozen framework modules do not import the evolvable role layer (roles are
     injected into the loop, not imported).
"""
from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

VALID_EXTERNAL = {
    "agent/llm.py",
    "agent/llm_withtools.py",
    "agent/base_agent.py",
    "domains/harness.py",
    "domains/report.py",
}


def test_old_paths_gone():
    for rel in ["gan/context.py", "gan/access.py", "gan/loop.py",
                "gan/tree", "gan/reward", "gan/task_runner.py"]:
        assert not os.path.exists(os.path.join(REPO_ROOT, rel)), f"old path still exists: {rel}"


def test_single_source_deny():
    from gan.framework import frozen
    assert frozen.FRAMEWORK_GLOB == "gan/framework/*"
    assert set(frozen.EXTERNAL_FROZEN) == VALID_EXTERNAL
    assert frozen.deny_paths() == [frozen.FRAMEWORK_GLOB, *frozen.EXTERNAL_FROZEN]


def test_deny_entries_are_framework_or_external():
    from gan.framework.frozen import deny_paths
    for p in deny_paths():
        ok = p == "gan/framework/*" or p in VALID_EXTERNAL
        assert ok, f"unexpected deny entry (should be framework or explicit external): {p}"


def test_framework_does_not_import_roles():
    fw = os.path.join(REPO_ROOT, "gan", "framework")
    bad = []
    for dirpath, _dirs, files in os.walk(fw):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            if re.search(r"^\s*(from|import)\s+gan\.roles\b", text, re.M):
                bad.append(os.path.relpath(path, REPO_ROOT))
    assert not bad, f"framework must not import gan.roles (inject instead): {bad}"


def test_moved_modules_importable():
    import importlib
    for m in ["gan.framework.context", "gan.framework.access", "gan.framework.loop",
              "gan.framework.tree.store", "gan.framework.task_runner",
              "gan.framework.task_execution", "gan.framework.checkpoint",
              "gan.framework.reward.packet", "gan.framework.reward.evaluator_reward"]:
        importlib.import_module(m)


def main():
    for fn in [test_old_paths_gone, test_single_source_deny,
               test_deny_entries_are_framework_or_external,
               test_framework_does_not_import_roles, test_moved_modules_importable]:
        fn()
        print(f"[OK ] {fn.__name__}")
    print("\nFROZEN-LAYOUT INVARIANTS PASSED")


if __name__ == "__main__":
    main()
