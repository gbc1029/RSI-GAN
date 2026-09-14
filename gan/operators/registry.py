"""Operator registry: discovery + session toolset assembly.

Operators are ordinary tools (``tool_info``/``tool_function``), so they reuse
``agent.tools.load_tools`` with a custom ``tools_dir``. This module adds:
  * ``default_dirs(role)`` - standard ops dirs for planner/evaluator;
  * ``assemble_tools_dir(...)`` - build a per-session dir (shared + role ops);
  * ``load_operators(...)`` - load from several dirs at once.
"""
from __future__ import annotations

import glob
import os
import shutil
from typing import List, Optional

from agent.tools import load_tools
from gan.config.loader import config_dir

_GAN_DIR = config_dir().parent


def default_dirs(role: str) -> List[str]:
    ops = os.path.join(_GAN_DIR, "operators", "planner_ops" if role == "planner" else "evaluator_ops")
    shared = os.path.join(_GAN_DIR, "operators", "common")
    return [ops, shared]


def assemble_tools_dir(
    role: str,
    dest_dir: str,
    ops_dirs: Optional[List[str]] = None,
    shared_dirs: Optional[List[str]] = None,
) -> str:
    """Copy shared + role op modules into a single session tools dir."""
    if ops_dirs is None or shared_dirs is None:
        d_ops, d_shared = default_dirs(role)
        ops_dirs = ops_dirs or [d_ops]
        shared_dirs = shared_dirs or [d_shared]
    os.makedirs(dest_dir, exist_ok=True)
    for src in list(shared_dirs) + list(ops_dirs):
        if not os.path.isdir(src):
            continue
        for f in glob.glob(os.path.join(src, "*.py")):
            if os.path.basename(f).startswith("__"):
                continue
            shutil.copy2(f, os.path.join(dest_dir, os.path.basename(f)))
    return dest_dir


def load_operators(
    names="all",
    ops_dirs: Optional[List[str]] = None,
    logging=print,
):
    tools = []
    for d in ops_dirs or []:
        tools.extend(load_tools(logging=logging, names=names, tools_dir=d))
    return tools
