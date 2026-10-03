from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class VisibleWindowSnapshot:
    pid: int | None
    app_exe: str | None
    app_name: str | None
    window_title: str | None
    monitor_id: str
    coverage: float


@dataclass(frozen=True)
class ForegroundSnapshot:
    timestamp: datetime
    pid: int | None
    app_exe: str | None
    app_name: str | None
    window_title: str | None
    idle_seconds: float
    session_locked: bool = False
    visible_windows: tuple[VisibleWindowSnapshot, ...] = ()


@dataclass(frozen=True)
class BrowserContext:
    browser: str
    focused: bool
    domain: str | None
    page_title: str | None
    timestamp: datetime
    window_id: int | None = None
    media_active: bool = False


@dataclass
class ActivitySegment:
    id: str
    started_at: datetime
    ended_at: datetime
    app_exe: str | None
    app_name: str | None
    window_title: str | None
    browser: str | None
    domain: str | None
    page_title: str | None
    is_idle: bool
    source: str = "windows_agent"
    track_kind: str = "foreground"
    monitor_id: str | None = None
    idle_exempt: bool = False

    def state_key(self) -> tuple[Any, ...]:
        if self.is_idle:
            return (True,)
        return (
            False, (self.app_exe or "").lower(), self.window_title or "",
            (self.browser or "").lower(), self.domain or "", self.page_title or "",
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": iso_utc(self.started_at),
            "ended_at": iso_utc(self.ended_at),
            "duration_seconds": max(0.0, (self.ended_at - self.started_at).total_seconds()),
            "app_exe": self.app_exe,
            "app_name": self.app_name,
            "window_title": self.window_title,
            "browser": self.browser,
            "domain": self.domain,
            "page_title": self.page_title,
            "is_idle": self.is_idle,
            "source": self.source,
            "track_kind": self.track_kind,
            "monitor_id": self.monitor_id,
            "created_at": iso_utc(self.started_at),
        }
