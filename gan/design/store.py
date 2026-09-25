"""Design store: read/write the per-role design config JSON."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from gan.design.schema import default_config


class DesignStore:
    def __init__(self, root: str | os.PathLike):
        self.root = os.path.abspath(str(root))

    def path(self, role: str, node_id: Any = None) -> str:
        if role == "task" and node_id is not None:
            return os.path.join(self.root, "task", str(node_id), "config.json")
        return os.path.join(self.root, role, "config.json")

    def load(self, role: str, node_id: Any = None) -> Dict[str, Any]:
        p = self.path(role, node_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return default_config(role)

    def save(self, config: Dict[str, Any], role: str, node_id: Any = None) -> str:
        p = self.path(role, node_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # B5: atomic write. The design file is authoritative and every role
        # session writes through here; a crash mid-write previously left HALF a
        # JSON behind -- which _seed_self_designs would then treat as corrupt
        # and re-seed (erasing the role's evolved design). tmp + os.replace
        # keeps the previous version intact on any write failure.
        tmp = f"{p}.save-tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        return p
