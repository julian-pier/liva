from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _kind = winreg.QueryValueEx(key, name)
                return str(registry_value)
        except (OSError, ImportError):
            pass
    return default


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int((_env(name) or str(default)).strip()))
    except ValueError:
        return default


@dataclass(frozen=True)
class AgentConfig:
    server_url: str
    api_key: str
    local_token: str
    device_id: str
    device_name: str
    spool_path: Path
    idle_threshold_seconds: int = 180
    poll_seconds: float = 1.0
    checkpoint_seconds: int = 2
    upload_interval_seconds: int = 10
    fast_upload_interval_seconds: int = 1
    batch_size: int = 100
    local_port: int = 8765
    # Event-driven extensions do not emit heartbeats for an unchanged tab.
    # Keep the latest focused context across long sessions; focus events clear it.
    browser_context_max_age_seconds: int = 7 * 86400
    sleep_gap_seconds: int = 15

    @classmethod
    def from_env(cls) -> "AgentConfig":
        state_dir = Path(_env("LIVA_ACTIVITY_STATE_DIR") or (Path.home() / ".liva-activity"))
        server_url = (_env("LIVA_ACTIVITY_SERVER_URL") or "").strip().rstrip("/")
        api_key = (_env("LIVA_ACTIVITY_API_KEY") or "").strip()
        local_token = (_env("LIVA_ACTIVITY_LOCAL_TOKEN") or "").strip()
        if not server_url:
            raise RuntimeError("LIVA_ACTIVITY_SERVER_URL fehlt")
        parsed = urlparse(server_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise RuntimeError("LIVA_ACTIVITY_SERVER_URL muss HTTPS verwenden (HTTP nur für localhost)")
        if not api_key:
            raise RuntimeError("LIVA_ACTIVITY_API_KEY fehlt")
        if len(local_token) < 24:
            raise RuntimeError("LIVA_ACTIVITY_LOCAL_TOKEN fehlt oder ist zu kurz (mindestens 24 Zeichen)")
        state_dir.mkdir(parents=True, exist_ok=True)
        device_file = state_dir / "device-id"
        configured_id = (_env("LIVA_ACTIVITY_DEVICE_ID") or "").strip()
        if configured_id:
            device_id = str(uuid.UUID(configured_id))
        elif device_file.exists():
            device_id = str(uuid.UUID(device_file.read_text(encoding="utf-8").strip()))
        else:
            device_id = str(uuid.uuid4())
            device_file.write_text(device_id + "\n", encoding="utf-8")
        return cls(
            server_url=server_url,
            api_key=api_key,
            local_token=local_token,
            device_id=device_id,
            device_name=(_env("LIVA_ACTIVITY_DEVICE_NAME") or socket.gethostname()).strip(),
            spool_path=Path(_env("LIVA_ACTIVITY_SPOOL_PATH") or (state_dir / "activity-spool.sqlite3")),
            idle_threshold_seconds=_int_env("LIVA_ACTIVITY_IDLE_THRESHOLD_SECONDS", 180),
            checkpoint_seconds=_int_env("LIVA_ACTIVITY_CHECKPOINT_SECONDS", 2),
            upload_interval_seconds=_int_env("LIVA_ACTIVITY_UPLOAD_INTERVAL_SECONDS", 10),
            fast_upload_interval_seconds=_int_env("LIVA_ACTIVITY_FAST_UPLOAD_INTERVAL_SECONDS", 1),
            batch_size=min(500, _int_env("LIVA_ACTIVITY_BATCH_SIZE", 100)),
            local_port=_int_env("LIVA_ACTIVITY_LOCAL_PORT", 8765),
        )
