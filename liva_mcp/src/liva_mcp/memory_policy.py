from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MemoryPolicy:
    version: int
    origins: dict[str, str | None]
    domains: tuple[str, ...]
    memory_types: tuple[str, ...]
    temporal_scopes: tuple[str, ...]
    operations: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "MemoryPolicy":
        # The checked-in file is JSON-compatible YAML, keeping the runtime dependency-free.
        if not path.is_file():
            return cls.default()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(int(data["version"]), dict(data["origins"]), tuple(data["domains"]), tuple(data["memory_types"]), tuple(data["temporal_scopes"]), tuple(data["operations"]))

    @classmethod
    def default(cls) -> "MemoryPolicy":
        return cls(2, {"gpt": "#gpt", "liva": "#liva"}, ("privat", "training", "recovery", "ernaehrung", "schule", "system", "kreativ", "texte"), ("ereignis", "person", "emotion", "erkenntnis", "praeferenz", "entscheidung", "status", "technik"), ("historical", "until_changed", "temporary"), ("create", "supersede", "append_edit"))

    def tags(self, origin: str, domain: str | None = None, memory_type: str | None = None) -> list[str]:
        """Visible Policy V2 tags: origin plus at most one optional domain."""
        return [tag for tag in (self.origins[origin], f"#{domain}" if domain else None) if tag]


def parse_iso(value: Any, *, datetime_allowed: bool = True) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("date values must be ISO strings")
    try:
        if "T" in value or " " in value:
            if not datetime_allowed: raise ValueError
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("date values must be ISO date or datetime") from exc
