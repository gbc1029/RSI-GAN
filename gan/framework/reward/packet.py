"""RewardPacket - the unified "loss function" serialized for all roles.

All three roles exchange feedback through this structure.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict

SCHEMA_VERSION = "v1"


@dataclass
class RewardPacket:
    # benchmark score / delta / cost / fail rate
    numeric: Dict[str, Any] = field(default_factory=dict)
    # weaknesses / other qualitative audit surface
    textual: Dict[str, Any] = field(default_factory=dict)
    # cannot_run / reward_hacking_suspect / rule_violation
    penalties: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


    # -- convenience -------------------------------------------------------
    @property
    def benchmark(self) -> Optional[float]:
        return self.numeric.get("benchmark")

    @property
    def delta_vs_parent(self) -> Optional[float]:
        return self.numeric.get("delta_vs_parent")

    def add_penalty(self, name: str, value: Any = 1) -> None:
        self.penalties[name] = value

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RewardPacket":
        return cls(
            numeric=dict(data.get("numeric", {}) or {}),
            textual=dict(data.get("textual", {}) or {}),
            penalties=dict(data.get("penalties", {}) or {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )

    def save(self, path: str | os.PathLike) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "RewardPacket":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
