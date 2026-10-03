from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .cache import DailyUsage, StayFreeDayMissing, locate_cache, read_available_days, read_day
from .config import BridgeConfig
from .state import ImportState
from .uploader import upload_daily

BERLIN = ZoneInfo("Europe/Berlin")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def current_usage_day(now: datetime | None = None) -> str:
    local_now = (now or datetime.now(timezone.utc)).astimezone(BERLIN)
    if local_now.timetz().replace(tzinfo=None) < time(hour=4):
        local_now -= timedelta(days=1)
    return local_now.date().isoformat()


def usage_day_bounds(usage_day: str) -> tuple[str, str]:
    selected = date.fromisoformat(usage_day)
    start = datetime.combine(selected, time(hour=4), tzinfo=BERLIN)
    end = datetime.combine(selected + timedelta(days=1), time(hour=4), tzinfo=BERLIN)
    return start.isoformat(), end.isoformat()


def last_completed_usage_day(now: datetime | None = None) -> str:
    return (date.fromisoformat(current_usage_day(now)) - timedelta(days=1)).isoformat()


def build_payload(config: BridgeConfig, usage: DailyUsage) -> dict[str, Any]:
    day_start, day_end = usage_day_bounds(usage.usage_day)
    return {
        "platform": "ios",
        "source": "stayfree_ios",
        "device": {
            "id": config.device_id,
            "name": config.device_name,
            "model": "iPhone14,5" if config.device_name.casefold() == "iphone 13" else None,
            "app_version": "stayfree-ios-bridge-1.0",
        },
        "usage_day": usage.usage_day,
        "day_start": day_start,
        "day_end": day_end,
        "total_usage_seconds": usage.total_usage_seconds,
        "source_updated_at": usage.source_updated_at,
        "apps": list(usage.apps),
    }


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_usage(
    config: BridgeConfig,
    usage: DailyUsage,
    state: ImportState,
    uploader: Callable[[str, str, dict[str, Any], float], dict[str, Any]] = upload_daily,
) -> dict[str, Any]:
    payload = build_payload(config, usage)
    digest = payload_hash(payload)
    if state.is_current(usage.usage_day, digest):
        return {"usage_day": usage.usage_day, "status": "already_imported", "total_usage_seconds": usage.total_usage_seconds}
    attempted_at = now_iso()
    state.record_attempt(usage.usage_day, digest, attempted_at)
    try:
        response = uploader(config.server_url, config.api_key, payload, config.request_timeout_seconds)
    except Exception as exc:
        state.record_error(usage.usage_day, digest, attempted_at, str(exc))
        raise
    state.record_success(usage.usage_day, digest, now_iso())
    return {
        "usage_day": usage.usage_day,
        "status": "imported",
        "total_usage_seconds": usage.total_usage_seconds,
        "apps": len(usage.apps),
        "daily_usage_id": response.get("daily_usage_id"),
    }


def run_daily(config: BridgeConfig, usage_day: str | None = None) -> dict[str, Any]:
    target = usage_day or last_completed_usage_day()
    cache_path = locate_cache(config.stayfree_db_path)
    state = ImportState(config.state_path)
    try:
        usage = read_day(cache_path, target)
    except StayFreeDayMissing:
        state.record_waiting(target, now_iso())
        return {"usage_day": target, "status": "waiting_for_stayfree"}
    return import_usage(config, usage, state)


def backfill(config: BridgeConfig, *, dry_run: bool = False) -> dict[str, Any]:
    cache_path = locate_cache(config.stayfree_db_path)
    available = read_available_days(cache_path)
    last_closed = date.fromisoformat(last_completed_usage_day())
    earliest = last_closed - timedelta(days=config.history_days - 1)
    selected = [
        usage for day, usage in sorted(available.items())
        if earliest <= date.fromisoformat(day) <= last_closed
    ]
    if config.verification_day and config.verification_total_seconds is not None:
        verification = available.get(config.verification_day)
        if verification is None:
            raise RuntimeError(f"StayFree verification day {config.verification_day} is missing")
        if verification.total_usage_seconds != config.verification_total_seconds:
            raise RuntimeError(
                f"StayFree verification failed for {config.verification_day}: "
                f"expected {config.verification_total_seconds}, got {verification.total_usage_seconds}"
            )
    report: dict[str, Any] = {
        "status": "dry_run" if dry_run else "complete",
        "available_days": len(selected),
        "earliest_day": selected[0].usage_day if selected else None,
        "latest_day": selected[-1].usage_day if selected else None,
        "app_records": sum(len(usage.apps) for usage in selected),
        "verification": {
            "usage_day": config.verification_day,
            "total_usage_seconds": config.verification_total_seconds,
            "status": "passed",
        },
        "imports": [],
    }
    if dry_run:
        return report
    state = ImportState(config.state_path)
    for usage in selected:
        try:
            report["imports"].append(import_usage(config, usage, state))
        except Exception as exc:
            report["imports"].append({
                "usage_day": usage.usage_day,
                "status": "retry",
                "error": type(exc).__name__,
                "detail": str(exc),
            })
    if any(item.get("status") == "retry" for item in report["imports"]):
        report["status"] = "partial"
    return report
