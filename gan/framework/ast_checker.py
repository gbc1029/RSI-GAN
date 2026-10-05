"""Small static policy checker for agent-authored Python tools.

This is deliberately conservative and import-free: source is parsed with ``ast``
and suspicious capability calls are reported before an agent patch is committed
or a tool is loaded. Framework-owned plumbing is exempt because it is not an
agent-authored artifact (and is separately protected by the frozen allowlist).
"""
from __future__ import annotations

import ast
import os
from pathlib import PurePosixPath
from typing import List, Optional


_WRITE_MODES = {"w", "wb", "wt", "a", "ab", "at", "x", "xb", "xt", "w+", "w+b", "wb+", "a+", "a+b", "ab+", "x+", "x+b", "xb+", "r+", "r+b", "rb+"}


def framework_owned(relpath: str) -> bool:
    """Whether *relpath* is frozen framework plumbing rather than agent code."""
    p = PurePosixPath(str(relpath).replace(os.sep, "/")).as_posix().lstrip("./")
    if "/gan/framework/" in p:
        p = p[p.index("gan/framework/"):]
    elif "/gan/tools/" in p:
        p = p[p.index("gan/tools/"):]
    return (p.startswith("gan/framework/") or p.startswith("gan/tools/design/")
            or p.startswith("gan/tools/deep/") or p.startswith("gan/tools/work/common/")
            or p == "gan/tools/assembly.py")


def _const_string(node: ast.AST) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.issues: List[str] = []

    def _issue(self, node: ast.AST, text: str) -> None:
        self.issues.append(f"line {getattr(node, 'lineno', '?')}: {text}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "subprocess" or alias.name.startswith("subprocess."):
                self._issue(node, "subprocess import")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "subprocess" or (node.module or "").startswith("subprocess."):
            self._issue(node, "subprocess import")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else None
        attr = fn.attr if isinstance(fn, ast.Attribute) else None
        root = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else None
        if name in {"eval", "exec"}:
            self._issue(node, f"dynamic execution: {name}()")
        elif root == "subprocess" or name in {"Popen", "run", "call", "check_call", "check_output"}:
            self._issue(node, "subprocess execution")
        elif attr in {"write_text", "write_bytes"}:
            self._issue(node, f"raw file write: .{attr}()")
        elif name in {"open", "io_open"}:
            mode = _const_string(node.args[1]) if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = _const_string(kw.value)
            if mode is None or mode in _WRITE_MODES:
                if mode is None:
                    # open() defaults to read-only; this is an explicit exemption.
                    pass
                else:
                    self._issue(node, f"raw file write: open(mode={mode!r})")
        elif root in {"os", "pathlib", "Path"} and attr in {"open", "write_text", "write_bytes"}:
            self._issue(node, f"raw file write: {root}.{attr}()")
        self.generic_visit(node)


def check_source(text: str, relpath: str = "") -> List[str]:
    """Return policy violations in Python source; empty means allowed."""
    if framework_owned(relpath):
        return []
    try:
        tree = ast.parse(text, filename=relpath or "<source>")
    except SyntaxError as exc:
        return [f"unparseable: {exc}"]
    checker = _Checker()
    checker.visit(tree)
    return checker.issues


def check_file(path: str, relpath: str = "") -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return check_source(fh.read(), relpath or path)
    except OSError as exc:
        return [f"unreadable: {exc}"]


def policy_reason(path: str, relpath: str = "") -> Optional[str]:
    issues = check_file(path, relpath)
    return "; ".join(issues[:4]) if issues else None


def validate_files(code_root: str, rel_files) -> List[str]:
    """Check changed Python files, returning ``path: issue`` strings."""
    out: List[str] = []
    for rel in rel_files:
        rel = str(rel).replace("\\", "/")
        if not rel.endswith(".py"):
            continue
        issues = check_file(os.path.join(code_root, rel), rel)
        out.extend(f"{rel}: {issue}" for issue in issues)
    return out
