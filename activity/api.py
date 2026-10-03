from __future__ import annotations

import hmac
import os
import re
import uuid
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from .iphone_daily import (
    SOURCE as STAYFREE_IOS_SOURCE,
    current_usage_day,
    iphone_daily_for_day,
    iphone_period_summary,
    upsert_iphone_daily,
    usage_day_bounds,
)
from .repository import summary_for_day, timeline_for_day, upsert_batch, windows_period_summary

activity_bp = Blueprint("activity", __name__)
MAX_BATCH_SIZE = 500
MAX_TEXT = 1024
MAX_IPHONE_TOTALS = 2000
MAX_IPHONE_DAILY_SECONDS = 25 * 60 * 60


def _token_from_request() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token", "apikey", "api-key"}:
            return parts[1].strip()
    return (request.headers.get("X-API-Key") or request.headers.get("X-LIVA-Api-Key") or "").strip()


def require_activity_write_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = (os.getenv("LIVA_ACTIVITY_INGEST_KEY") or "").strip()
        if not expected:
            return jsonify({"ok": False, "error": "activity_ingest_key_missing"}), 503
        if not hmac.compare_digest(_token_from_request(), expected):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _utc_iso(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}: invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}: timezone required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field}: invalid UUID") from exc


def _text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field}: required")
        return None
    output = str(value).strip()
    if required and not output:
        raise ValueError(f"{field}: required")
    if len(output) > limit:
        raise ValueError(f"{field}: too long")
    return output or None


def _domain(value: Any, field: str) -> str | None:
    output = _text(value, field, limit=253)
    if output is None:
        return None
    output = output.lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", output) or ".." in output:
        raise ValueError(f"{field}: invalid domain")
    return output


def _validate_payload(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    device_raw = payload.get("device")
    events_raw = payload.get("events")
    if not isinstance(device_raw, dict):
        raise ValueError("device: object required")
    if not isinstance(events_raw, list) or not events_raw:
        raise ValueError("events: non-empty array required")
    if len(events_raw) > MAX_BATCH_SIZE:
        raise ValueError(f"events: maximum {MAX_BATCH_SIZE}")
    device = {
        "id": _uuid(device_raw.get("id"), "device.id"),
        "name": _text(device_raw.get("name"), "device.name", required=True, limit=200),
        "platform": _text(device_raw.get("platform") or "windows", "device.platform", limit=40),
        "agent_version": _text(device_raw.get("agent_version"), "device.agent_version", limit=80),
    }
    batch_id = _uuid(payload.get("batch_id"), "batch_id")
    events: list[dict[str, Any]] = []
    for index, raw in enumerate(events_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"events[{index}]: object required")
        started = _utc_iso(raw.get("started_at"), f"events[{index}].started_at")
        ended = _utc_iso(raw.get("ended_at"), f"events[{index}].ended_at")
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        if end_dt < start_dt:
            raise ValueError(f"events[{index}]: ended_at before started_at")
        duration = (end_dt - start_dt).total_seconds()
        if duration > 7 * 86400:
            raise ValueError(f"events[{index}]: duration too large")
        events.append({
            "id": _uuid(raw.get("id"), f"events[{index}].id"),
            "started_at": started,
            "ended_at": ended,
            "duration_seconds": duration,
            "app_exe": _text(raw.get("app_exe"), "app_exe", limit=500),
            "app_name": _text(raw.get("app_name"), "app_name", limit=300),
            "window_title": _text(raw.get("window_title"), "window_title"),
            "browser": _text(raw.get("browser"), "browser", limit=50),
            "domain": _domain(raw.get("domain"), f"events[{index}].domain"),
            "page_title": _text(raw.get("page_title"), "page_title"),
            "is_idle": bool(raw.get("is_idle", False)),
            "source": _text(raw.get("source") or "windows_agent", "source", limit=80),
            "track_kind": _text(raw.get("track_kind") or "foreground", "track_kind", limit=20),
            "monitor_id": _text(raw.get("monitor_id"), "monitor_id", limit=100),
            "created_at": _utc_iso(raw.get("created_at") or started, "created_at"),
        })
    return device, events, batch_id


def _date_arg() -> str:
    raw = (request.args.get("date") or date.today().isoformat()).strip()
    date.fromisoformat(raw)
    return raw


def _nonnegative_number(value: Any, field: str, *, maximum: float = 172800) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field}: number required")
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: number required") from exc
    if output < 0 or output > maximum:
        raise ValueError(f"{field}: out of range")
    return output


def _nonnegative_int(value: Any, field: str, *, maximum: int = 1_000_000) -> int:
    number = _nonnegative_number(value, field, maximum=maximum)
    if not number.is_integer():
        raise ValueError(f"{field}: integer required")
    return int(number)


def _validate_iphone_daily_payload(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    if str(payload.get("platform") or "").strip().lower() != "ios":
        raise ValueError("platform: only ios is accepted")
    if str(payload.get("source") or "").strip() != STAYFREE_IOS_SOURCE:
        raise ValueError("source: only stayfree_ios is accepted")
    device_raw = payload.get("device")
    apps_raw = payload.get("apps")
    if not isinstance(device_raw, dict):
        raise ValueError("device: object required")
    if not isinstance(apps_raw, list):
        raise ValueError("apps: array required")
    if len(apps_raw) > MAX_IPHONE_TOTALS:
        raise ValueError(f"apps: maximum {MAX_IPHONE_TOTALS}")

    usage_day = str(payload.get("usage_day") or "").strip()
    date.fromisoformat(usage_day)
    canonical_start, canonical_end = usage_day_bounds(usage_day)
    for field, supplied, canonical in (
        ("day_start", payload.get("day_start"), canonical_start),
        ("day_end", payload.get("day_end"), canonical_end),
    ):
        text = str(supplied or "").strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field}: invalid timestamp") from exc
        expected = datetime.fromisoformat(canonical)
        if parsed.tzinfo is None or parsed != expected or parsed.utcoffset() != expected.utcoffset():
            raise ValueError(f"{field}: must match Europe/Berlin 04:00 usage-day boundary")

    total = _nonnegative_int(
        payload.get("total_usage_seconds"),
        "total_usage_seconds",
        maximum=MAX_IPHONE_DAILY_SECONDS,
    )
    apps: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw in enumerate(apps_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"apps[{index}]: object required")
        name = _text(raw.get("name"), f"apps[{index}].name", required=True, limit=300)
        app_key = _text(raw.get("app_key"), f"apps[{index}].app_key", limit=300)
        identity = f"key:{app_key.casefold()}" if app_key else f"name:{name.casefold()}"
        if identity in identities:
            raise ValueError(f"apps[{index}]: duplicate app")
        identities.add(identity)
        apps.append({
            "app_key": app_key,
            "name": name,
            "duration_seconds": _nonnegative_int(
                raw.get("duration_seconds"),
                f"apps[{index}].duration_seconds",
                maximum=MAX_IPHONE_DAILY_SECONDS,
            ),
            "category": _text(raw.get("category"), f"apps[{index}].category", limit=200),
        })
    if sum(app["duration_seconds"] for app in apps) > total:
        raise ValueError("apps: summed duration exceeds total_usage_seconds")

    source_updated_at = payload.get("source_updated_at")
    if source_updated_at is not None:
        source_updated_at = _utc_iso(source_updated_at, "source_updated_at")
    device = {
        "id": _uuid(device_raw.get("id"), "device.id"),
        "name": _text(device_raw.get("name"), "device.name", required=True, limit=200),
        "model": _text(device_raw.get("model"), "device.model", limit=100),
        "system_version": _text(device_raw.get("system_version"), "device.system_version", limit=80),
        "app_version": _text(device_raw.get("app_version"), "device.app_version", limit=80),
    }
    daily = {
        "usage_day": usage_day,
        "day_start": canonical_start,
        "day_end": canonical_end,
        "total_usage_seconds": total,
        "source_updated_at": source_updated_at,
        "apps": apps,
    }
    return device, daily


@activity_bp.post("/api/activity/ingest")
@require_activity_write_key
def ingest_activity():
    try:
        device, events, batch_id = _validate_payload(request.get_json(silent=True))
        result = upsert_batch(device, events, batch_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": "invalid_payload", "detail": str(exc)}), 400
    return jsonify({"ok": True, "batch_id": batch_id, **result})


@activity_bp.post("/api/activity/iphone/daily")
@require_activity_write_key
def ingest_iphone_daily_activity():
    try:
        device, daily = _validate_iphone_daily_payload(request.get_json(silent=True))
        result = upsert_iphone_daily(device, daily)
    except ValueError as exc:
        return jsonify({"ok": False, "error": "invalid_payload", "detail": str(exc)}), 400
    return jsonify({"ok": True, "source": STAYFREE_IOS_SOURCE, **result})


def _iphone_device_arg() -> str | None:
    device_id = (request.args.get("device_id") or "").strip() or None
    return _uuid(device_id, "device_id") if device_id else None


@activity_bp.get("/api/activity/iphone/summary")
def summary_iphone_activity():
    try:
        device_id = _iphone_device_arg()
        day = (request.args.get("date") or "").strip() or None
        if day:
            date.fromisoformat(day)
    except ValueError as exc:
        return jsonify({"ok": False, "error": "invalid_query", "detail": str(exc)}), 400
    return jsonify({"ok": True, **iphone_daily_for_day(day or current_usage_day(), device_id)})


@activity_bp.get("/api/activity/timeline")
def activity_timeline():
    try:
        day_iso = _date_arg()
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_date"}), 400
    return jsonify({"ok": True, "date": day_iso, "timezone": "Europe/Berlin", "events": timeline_for_day(day_iso)})


@activity_bp.get("/api/activity/summary")
def activity_summary():
    try:
        day_iso = _date_arg()
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_date"}), 400
    return jsonify({"ok": True, **summary_for_day(day_iso)})


@activity_bp.get("/api/activity/period")
def activity_period():
    try:
        end = date.fromisoformat((request.args.get("end_date") or current_usage_day()).strip())
        start_raw = (request.args.get("start_date") or "").strip()
        if start_raw:
            start = date.fromisoformat(start_raw)
        else:
            days = int((request.args.get("days") or "7").strip())
            if days not in {7, 30}:
                raise ValueError("days must be 7 or 30")
            start = end.fromordinal(end.toordinal() - days + 1)
        if (end - start).days + 1 > 90 or end < start:
            raise ValueError("invalid period")
        device_id = _iphone_device_arg()
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": "invalid_query", "detail": str(exc)}), 400
    return jsonify({
        "ok": True,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": (end - start).days + 1,
        "windows": windows_period_summary(start.isoformat(), end.isoformat()),
        "iphone": iphone_period_summary(start.isoformat(), end.isoformat(), device_id),
    })


@activity_bp.get("/activity")
def activity_page():
    return render_template("activity.html")
