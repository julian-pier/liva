from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
SQLITE_MAGIC = b"SQLite format 3\x00"
SESSION_CACHE_KEY = "api-sessions-repo-find-sessions-cache"
RESET_TIME_KEY = "daily-reset-time"
EXPECTED_RESET_MS = 4 * 60 * 60 * 1000
KEY_SEPARATOR = "—"
MAX_DAY_SECONDS = 25 * 60 * 60


class StayFreeCacheError(RuntimeError):
    pass


class StayFreeDayMissing(StayFreeCacheError):
    pass


@dataclass(frozen=True)
class DailyUsage:
    usage_day: str
    total_usage_seconds: int
    apps: tuple[dict[str, Any], ...]
    source_updated_at: str | None = None


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _is_stayfree_config(path: Path) -> bool:
    if not _is_sqlite(path):
        return False
    try:
        with open_readonly(path) as conn:
            row = conn.execute(
                "SELECT 1 FROM config WHERE key=? LIMIT 1", (SESSION_CACHE_KEY,)
            ).fetchone()
        return row is not None
    except (sqlite3.Error, OSError):
        return False


def locate_cache(explicit: Path | None = None) -> Path:
    if explicit:
        path = explicit.expanduser().resolve()
        if not path.is_file() or not _is_stayfree_config(path):
            raise StayFreeCacheError(f"Configured StayFree config.db is not readable or incompatible: {path}")
        return path

    roots = [Path(value) for name in ("LOCALAPPDATA", "APPDATA") if (value := os.environ.get(name))]
    patterns = (
        "Packages/*StayFree*/LocalCache/Roaming/StayFree/config.db",
        "Packages/*stayfree*/LocalCache/Roaming/StayFree/config.db",
        "StayFree/config.db",
        "stayfree/config.db",
    )
    candidates: set[Path] = set()
    for root in roots:
        for pattern in patterns:
            try:
                candidates.update(path.resolve() for path in root.glob(pattern) if path.is_file())
            except (OSError, PermissionError):
                continue
    compatible = [path for path in candidates if _is_stayfree_config(path)]
    if not compatible:
        raise StayFreeCacheError(
            "StayFree config.db with the synced-session cache was not found under the Windows app package"
        )
    compatible.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return compatible[0]


def _config_value(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    except sqlite3.Error as exc:
        raise StayFreeCacheError(f"StayFree config.db schema is incompatible: {exc}") from exc
    if row is None or row[0] is None:
        raise StayFreeCacheError(f"StayFree config key is missing: {key}")
    return str(row[0])


def _milliseconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 < number < 10**16 else None


def _utc_iso(milliseconds: Any) -> str | None:
    value = _milliseconds(milliseconds)
    if value is None:
        return None
    try:
        parsed = datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_app_name(value: Any) -> str | None:
    text = "".join(character for character in str(value or "") if unicodedata.category(character) != "Cf")
    text = " ".join(text.split()).strip()
    return text[:160] or None


def _cache_key_bounds(raw_key: Any) -> tuple[str, int, int, str] | None:
    parts = str(raw_key).split(KEY_SEPARATOR, 2)
    if len(parts) != 3 or not parts[2] or parts[2] == "undefined":
        return None
    start_ms = _milliseconds(parts[0])
    end_ms = _milliseconds(parts[1])
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    try:
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc).astimezone(BERLIN)
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc).astimezone(BERLIN)
    except (OSError, OverflowError, ValueError):
        return None
    expected_end_date = start.date() + timedelta(days=1)
    if start.timetz().replace(tzinfo=None) != time(hour=4):
        return None
    if end.date() != expected_end_date or end.timetz().replace(tzinfo=None) != time(hour=4):
        return None
    elapsed = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    if elapsed not in (23 * 60 * 60, 24 * 60 * 60, 25 * 60 * 60):
        return None
    return start.date().isoformat(), start_ms, end_ms, parts[2]


def _parse_ios_entry(raw_entry: Any, start_ms: int, end_ms: int) -> tuple[dict[str, int], str | None]:
    if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("value"), list):
        return {}, None
    sessions: dict[str, tuple[str, int]] = {}
    for record in raw_entry["value"]:
        if not isinstance(record, dict):
            continue
        if str(record.get("platform") or "").strip().casefold() != "ios":
            continue
        if record.get("imported") is not True:
            continue
        app_name = _clean_app_name(record.get("appId"))
        started_at = _milliseconds(record.get("startedAt"))
        ended_at = _milliseconds(record.get("endedAt"))
        session_id = str(record.get("id") or "").strip()
        if not app_name or started_at is None or ended_at is None or not session_id:
            continue
        clipped_start = max(started_at, start_ms)
        clipped_end = min(ended_at, end_ms)
        if clipped_end <= clipped_start:
            continue
        duration = round((clipped_end - clipped_start) / 1000)
        if duration <= 0 or duration > MAX_DAY_SECONDS:
            continue
        existing = sessions.get(session_id)
        candidate = (app_name, duration)
        if existing is not None and existing != candidate:
            raise StayFreeCacheError(f"Conflicting duplicate StayFree session id: {session_id[:80]}")
        sessions[session_id] = candidate

    apps: dict[str, int] = {}
    display: dict[str, str] = {}
    for app_name, duration in sessions.values():
        identity = app_name.casefold()
        display.setdefault(identity, app_name)
        apps[identity] = apps.get(identity, 0) + duration
    normalized = {display[key]: seconds for key, seconds in apps.items()}
    return normalized, _utc_iso(raw_entry.get("createdAt"))


def read_available_days(path: Path) -> dict[str, DailyUsage]:
    try:
        with open_readonly(path) as conn:
            reset_ms = int(_config_value(conn, RESET_TIME_KEY))
            if reset_ms != EXPECTED_RESET_MS:
                raise StayFreeCacheError(
                    f"StayFree daily reset must be 04:00 ({EXPECTED_RESET_MS} ms), got {reset_ms} ms"
                )
            raw_cache = json.loads(_config_value(conn, SESSION_CACHE_KEY))
    except StayFreeCacheError:
        raise
    except (sqlite3.OperationalError, OSError) as exc:
        raise StayFreeCacheError(f"StayFree config.db unavailable or locked: {exc}") from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StayFreeCacheError(f"StayFree synced-session cache is malformed: {exc}") from exc
    if not isinstance(raw_cache, dict):
        raise StayFreeCacheError("StayFree synced-session cache is not an object")

    by_day: dict[str, list[tuple[str, dict[str, int], str | None]]] = {}
    for raw_key, raw_entry in raw_cache.items():
        bounds = _cache_key_bounds(raw_key)
        if bounds is None:
            continue
        usage_day, start_ms, end_ms, source_device = bounds
        apps, updated = _parse_ios_entry(raw_entry, start_ms, end_ms)
        if apps:
            by_day.setdefault(usage_day, []).append((source_device, apps, updated))

    output: dict[str, DailyUsage] = {}
    for usage_day, entries in by_day.items():
        device_ids = {entry[0] for entry in entries}
        if len(device_ids) != 1:
            raise StayFreeCacheError(
                f"Multiple iOS device cache entries exist for {usage_day}; refusing to merge devices"
            )
        apps = entries[0][1]
        updated = entries[0][2]
        total = sum(apps.values())
        if total <= 0 or total > MAX_DAY_SECONDS:
            continue
        normalized_apps = tuple(
            {"app_key": name.casefold(), "name": name, "duration_seconds": seconds}
            for name, seconds in sorted(apps.items(), key=lambda item: (-item[1], item[0].casefold()))
        )
        output[usage_day] = DailyUsage(usage_day, total, normalized_apps, updated)
    return dict(sorted(output.items()))


def read_day(path: Path, usage_day: str) -> DailyUsage:
    try:
        selected = date.fromisoformat(usage_day).isoformat()
    except ValueError as exc:
        raise StayFreeDayMissing(f"Invalid StayFree usage day: {usage_day}") from exc
    available = read_available_days(path)
    if selected not in available:
        raise StayFreeDayMissing(f"No complete StayFree iOS data for {selected}")
    return available[selected]
