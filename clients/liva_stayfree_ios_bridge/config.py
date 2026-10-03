from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def default_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA") or Path.home() / ".config")
    return base / "LIVA" / "StayFreeIOSBridge" / "config.json"


@dataclass(frozen=True)
class BridgeConfig:
    server_url: str
    api_key: str
    device_id: str
    device_name: str = "iPhone 13"
    stayfree_db_path: Path | None = None
    state_path: Path = Path("bridge-state.sqlite3")
    history_days: int = 90
    request_timeout_seconds: float = 20.0
    # StayFree can prune historical cache entries, so no fixed probe day may
    # be required for a production import.
    verification_day: str | None = None
    verification_total_seconds: int | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "BridgeConfig":
        config_path = path or default_config_path()
        # Windows PowerShell 5.1 writes UTF-8 files with a BOM. Accept both
        # variants because the installer must work on stock Windows systems.
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        server_url = str(raw.get("server_url") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        device_id = str(raw.get("device_id") or "").strip()
        if not server_url or not api_key or not device_id:
            raise RuntimeError("server_url, api_key and device_id are required")
        state_raw = raw.get("state_path")
        state_path = Path(state_raw).expanduser() if state_raw else config_path.parent / "bridge-state.sqlite3"
        cache_raw = raw.get("stayfree_db_path")
        return cls(
            server_url=server_url,
            api_key=api_key,
            device_id=device_id,
            device_name=str(raw.get("device_name") or "iPhone 13").strip(),
            stayfree_db_path=Path(cache_raw).expanduser() if cache_raw else None,
            state_path=state_path,
            history_days=max(1, min(int(raw.get("history_days", 90)), 730)),
            request_timeout_seconds=max(2.0, min(float(raw.get("request_timeout_seconds", 20)), 120.0)),
            verification_day=str(raw.get("verification_day") or "").strip() or None,
            verification_total_seconds=(
                int(raw["verification_total_seconds"])
                if raw.get("verification_total_seconds") is not None else None
            ),
        )
