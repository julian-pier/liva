from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import get_polar_db
from integrations.polar_client import PolarClient, PolarClientError, ensure_polar_schema, log_polar_sync


def sync_polar_day(day_iso: str, client: PolarClient) -> dict[str, Any]:
    ensure_polar_schema()
    day = date.fromisoformat(day_iso)
    next_day = day + timedelta(days=1)
    from_date = day.isoformat()
    to_date = next_day.isoformat()
    from_dt = f"{from_date}T00:00:00"
    to_dt = f"{to_date}T00:00:00"
    summary = {
        "date": day_iso,
        "sleep": {"stored_rows": 0, "populated_days": 0},
        "nightly_recharge": {"stored_rows": 0, "populated_days": 0},
        "continuous_samples": {"stored_rows": 0, "populated_days": 0},
        "activity": {"stored_rows": 0, "populated_days": 0},
        "training_sessions": {"stored_sessions": 0},
        "errors": [],
    }
    fetchers = [
        ("sleep", lambda f, t: client.get_sleeps(f, t, features=["sleep-score", "sleep-result", "sleep-evaluation"]), _store_sleep),
        ("nightly_recharge", lambda f, t: client.get_nightly_recharge(f, t), _store_nightly_recharge),
        (
            "continuous_samples",
            lambda f, t: client.get_continuous_samples(f, t, features=["heart-rate-samples"]),
            _store_continuous_samples,
        ),
        ("activity", lambda f, t: client.get_activity(f, t), _store_activity),
        ("training_sessions", lambda _f, _t: client.get_training_sessions(from_dt, to_dt), _store_training_sessions),
    ]
    for sync_type, fetcher, store in fetchers:
        try:
            if sync_type == "training_sessions":
                log_polar_sync("training_sessions_request", "info", f"from={from_dt} to={to_dt}")
            payload = fetcher(from_date, to_date)
            store_result = store(day_iso, payload)
            if sync_type == "training_sessions":
                summary["training_sessions"]["stored_sessions"] += int(store_result.get("stored_sessions", 0))
                if store_result.get("stored_sessions", 0):
                    log_polar_sync(f"sync_day:{sync_type}", "ok", f"{day_iso}: {store_result.get('stored_sessions', 0)} Sessions gespeichert")
                else:
                    log_polar_sync(f"sync_day:{sync_type}", "empty", f"{day_iso}: keine Daten")
            else:
                summary[sync_type]["stored_rows"] += int(store_result.get("stored_rows", 0))
                summary[sync_type]["populated_days"] += int(store_result.get("populated_days", 0))
                if store_result.get("stored_rows", 0):
                    status = "ok" if store_result.get("populated_days", 0) else "empty"
                    detail = "gespeichert" if store_result.get("populated_days", 0) else "gespeichert, aber ohne echte Kernwerte"
                    log_polar_sync(f"sync_day:{sync_type}", status, f"{day_iso}: {detail}")
                else:
                    log_polar_sync(f"sync_day:{sync_type}", "empty", f"{day_iso}: keine Daten")
        except PolarClientError as exc:
            message = f"{day_iso} {sync_type}: {exc}"
            summary["errors"].append(message)
            log_polar_sync(f"sync_day:{sync_type}", "error", message)
        except Exception as exc:
            message = f"{day_iso} {sync_type}: {exc}"
            summary["errors"].append(message)
            log_polar_sync(f"sync_day:{sync_type}", "error", message)
    return summary


def sync_polar_range(client: PolarClient, days: int = 14) -> dict[str, Any]:
    ensure_polar_schema()
    days = max(1, min(int(days or 14), 60))
    today = date.today()
    result = {
        "ok": True,
        "days_requested": days,
        "sleep": {"stored_rows": 0, "populated_days": 0},
        "nightly_recharge": {"stored_rows": 0, "populated_days": 0},
        "continuous_samples": {"stored_rows": 0, "populated_days": 0},
        "activity": {"stored_rows": 0, "populated_days": 0},
        "training_sessions": {"stored_sessions": 0},
        "errors": [],
    }
    for offset in range(days):
        day_iso = (today - timedelta(days=offset)).isoformat()
        day_result = sync_polar_day(day_iso, client)
        for section in ("sleep", "nightly_recharge", "continuous_samples", "activity"):
            result[section]["stored_rows"] += int(day_result[section]["stored_rows"])
            result[section]["populated_days"] += int(day_result[section]["populated_days"])
        result["training_sessions"]["stored_sessions"] += int(day_result["training_sessions"]["stored_sessions"])
        if day_result["errors"]:
            result["errors"].extend(day_result["errors"])
    result["ok"] = not bool(result["errors"])
    return result


def latest_polar_snapshot() -> dict[str, Any]:
    ensure_polar_schema()
    with closing(get_polar_db()) as conn:
        latest_sleep = _latest_row(conn, "polar_sleep", "date")
        latest_nightly = _latest_row(conn, "polar_nightly_recharge", "date")
        latest_activity = _latest_row(conn, "polar_activity", "date")
        latest_continuous = _latest_row(conn, "polar_continuous_samples", "date")
        latest_training = _latest_row(conn, "polar_training_sessions", "start_time")
        last_sync_any = conn.execute(
            "SELECT id, sync_type, status, message, created_at FROM polar_sync_log ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        last_successful_sync = conn.execute(
            "SELECT id, sync_type, status, message, created_at FROM polar_sync_log WHERE lower(status) IN ('ok', 'success') ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        last_error = conn.execute(
            "SELECT id, sync_type, status, message, created_at FROM polar_sync_log WHERE lower(status) = 'error' ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        token = conn.execute(
            "SELECT polar_user_id, expires_at, refresh_token FROM polar_tokens ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "connected": bool(token),
        "latest_sleep": _compact_sleep(latest_sleep),
        "latest_nightly_recharge": _compact_nightly(latest_nightly),
        "latest_activity": _compact_activity(latest_activity),
        "latest_continuous_samples": _compact_continuous(latest_continuous),
        "latest_training_session": _compact_training(latest_training),
        "last_sync_any": dict(last_sync_any) if last_sync_any else None,
        "last_successful_sync": dict(last_successful_sync) if last_successful_sync else None,
        "last_error": dict(last_error) if last_error else None,
    }


def polar_state_summary() -> dict[str, Any]:
    ensure_polar_schema()
    today_iso = date.today().isoformat()
    with closing(get_polar_db()) as conn:
        token = conn.execute("SELECT 1 FROM polar_tokens ORDER BY id DESC LIMIT 1").fetchone()
        latest_sleep = _latest_row(conn, "polar_sleep", "date")
        latest_nightly = _latest_row(conn, "polar_nightly_recharge", "date")
        latest_activity = _latest_row(conn, "polar_activity", "date")
        latest_continuous = _latest_row(conn, "polar_continuous_samples", "date")
        today_sleep = _by_pk(conn, "polar_sleep", "date", today_iso)
        today_nightly = _by_pk(conn, "polar_nightly_recharge", "date", today_iso)
        today_activity = _by_pk(conn, "polar_activity", "date", today_iso)
        today_continuous = _by_pk(conn, "polar_continuous_samples", "date", today_iso)
        rows = {
            "sleep": _rows_since(conn, "polar_sleep", "date", 7),
            "nightly": _rows_since(conn, "polar_nightly_recharge", "date", 7),
            "activity": _rows_since(conn, "polar_activity", "date", 7),
            "continuous": _rows_since(conn, "polar_continuous_samples", "date", 7),
        }
    sleep_count = len(rows["sleep"])
    recovery_count = len(rows["nightly"])
    activity_count = len(rows["activity"])
    continuous_count = len(rows["continuous"])
    total_rows = sleep_count + recovery_count + activity_count + continuous_count
    sleep_populated = sum(1 for row in rows["sleep"] if _sleep_has_real_values(row))
    nightly_populated = sum(1 for row in rows["nightly"] if _nightly_has_real_values(row))
    activity_populated = sum(1 for row in rows["activity"] if _activity_has_real_values(row))
    continuous_populated = sum(1 for row in rows["continuous"] if _continuous_has_real_values(row))
    if not token:
        quality = "missing"
    elif total_rows == 0:
        quality = "missing"
    elif sleep_populated >= 3 and nightly_populated >= 3 and activity_populated >= 3:
        quality = "good"
    elif max([sleep_populated, nightly_populated, activity_populated, continuous_populated]) >= 1:
        quality = "ok"
    else:
        quality = "thin"
    notes: list[str] = []
    if sleep_count == 0:
        notes.append("sleep_missing")
    if continuous_count == 0:
        notes.append("continuous_samples_missing")
    if recovery_count == 0:
        notes.append("nightly_recharge_missing")
    if _activity_has_day_without_details(today_activity) or any(_activity_has_day_without_details(row) for row in rows["activity"]):
        notes.append("Polar Activity liefert aktuell noch keine Schritt-/Aktivitätsdetails, obwohl ein Aktivitätstag existiert.")
    if bool(token) and total_rows > 0 and (sleep_populated + nightly_populated + activity_populated + continuous_populated == 0):
        notes.append("Polar ist verbunden, aber es liegen noch kaum echte Messwerte vor. Das ist bei einem neuen Gerät normal.")
    return {
        "source": "polar",
        "connected": bool(token),
        "data_quality": quality,
        "latest_dates": {
            "sleep": latest_sleep["date"] if latest_sleep else None,
            "nightly_recharge": latest_nightly["date"] if latest_nightly else None,
            "activity": latest_activity["date"] if latest_activity else None,
            "continuous_samples": latest_continuous["date"] if latest_continuous else None,
        },
        "today": {
            "sleep": _compact_sleep(today_sleep),
            "nightly_recharge": _compact_nightly(today_nightly),
            "activity": _compact_activity(today_activity),
            "heart_rate": _compact_continuous(today_continuous),
        },
        "last_7_days": {
            "sleep_days": sleep_count,
            "avg_sleep_minutes": _avg(rows["sleep"], "sleep_minutes"),
            "avg_sleep_score": _avg(rows["sleep"], "sleep_score"),
            "avg_rmssd": _avg(rows["nightly"], "mean_recovery_rmssd"),
            "avg_resting_candidate_hr": _avg(rows["continuous"], "resting_candidate_hr"),
            "avg_steps": _avg(rows["activity"], "steps"),
        },
        "notes": notes,
    }


def _store_sleep(day_iso: str, payload: Any) -> int:
    entries = _as_items(payload)
    stored = 0
    populated = 0
    for entry in entries:
        row = _normalize_sleep(entry, day_iso)
        if not row:
            continue
        _upsert_sleep_row(row)
        stored += 1
        if _sleep_has_real_values(row):
            populated = 1
    return {"stored_rows": stored, "populated_days": populated}


def _store_nightly_recharge(day_iso: str, payload: Any) -> int:
    entries = _as_items(payload)
    stored = 0
    populated = 0
    for entry in entries:
        row = _normalize_nightly(entry, day_iso)
        if not row:
            continue
        _upsert("polar_nightly_recharge", "date", row)
        stored += 1
        if _nightly_has_real_values(row):
            populated = 1
    return {"stored_rows": stored, "populated_days": populated}


def _store_continuous_samples(day_iso: str, payload: Any) -> int:
    rows = _normalize_continuous_payload(payload, day_iso)
    if not rows:
        return {"stored_rows": 0, "populated_days": 0}
    stored_rows = 0
    populated_days = 0
    for row in rows:
        _upsert("polar_continuous_samples", "date", row)
        stored_rows += 1
        if _continuous_has_real_values(row):
            populated_days += 1
    return {"stored_rows": stored_rows, "populated_days": populated_days}


def _store_activity(day_iso: str, payload: Any) -> int:
    entries = _as_items(payload)
    stored = 0
    populated = 0
    for entry in entries:
        row = _normalize_activity(entry, day_iso)
        if not row:
            continue
        _upsert("polar_activity", "date", row)
        stored += 1
        if _activity_has_real_values(row):
            populated = 1
    return {"stored_rows": stored, "populated_days": populated}


def _store_training_sessions(day_iso: str, payload: Any) -> int:
    entries = _as_items(payload)
    stored = 0
    for entry in entries:
        row = _normalize_training_session(entry, day_iso)
        if not row:
            continue
        _upsert("polar_training_sessions", "session_id", row)
        stored += 1
    return {"stored_sessions": stored}


def _normalize_sleep(entry: Any, fallback_date: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    day_iso = _date_str(
        entry.get("sleepDate") or entry.get("date") or entry.get("night") or _nested_get(entry, "date")
    ) or fallback_date
    raw_json = _json_dump(entry)
    sleep_start = _str_or_none(
        entry.get("sleepStart")
        or entry.get("sleep_start")
        or _nested_get(entry, "sleepResult.hypnogram.sleepStart")
        or _nested_get(entry, "originalSleepResult.hypnogram.sleepStart")
    )
    sleep_end = _str_or_none(
        entry.get("sleepEnd")
        or entry.get("sleep_end")
        or _nested_get(entry, "sleepResult.hypnogram.sleepEnd")
        or _nested_get(entry, "originalSleepResult.hypnogram.sleepEnd")
    )
    duration_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.sleepSpan"),
        entry.get("sleep_minutes"),
        entry.get("duration"),
        _minutes_between(sleep_start, sleep_end),
    )
    actual_sleep_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.asleepDuration"),
        entry.get("actualSleep"),
        entry.get("actual_sleep_minutes"),
        duration_minutes,
    )
    deep_sleep_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.phaseDurations.deep"),
        entry.get("deepSleep"),
        entry.get("deep_sleep_minutes"),
    )
    rem_sleep_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.phaseDurations.rem"),
        entry.get("remSleep"),
        entry.get("rem_sleep_minutes"),
    )
    light_sleep_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.phaseDurations.light"),
    )
    awake_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.phaseDurations.wake"),
    )
    interruptions = _first_int(
        _nested_get(entry, "sleepEvaluation.interruptions.totalCount"),
        entry.get("interruptions"),
        entry.get("interruptionsCount"),
    )
    interruption_minutes = _first_minutes(
        _nested_get(entry, "sleepEvaluation.interruptions.totalDuration"),
    )
    # Polar v4 /sleeps exposes detail fields only via features, and sleepScore often arrives as an object.
    # If only sleep-score is requested/available, detail minutes can legitimately remain empty.
    sleep_score = _nested_get(entry, "sleepScore.sleepScore")
    if sleep_score in (None, ""):
        sleep_score = entry.get("sleepScore")
    if sleep_score in (None, ""):
        sleep_score = entry.get("sleep_score")
    row = {
        "date": day_iso,
        "raw_json": raw_json,
        "sleep_score": _num(sleep_score),
        "sleep_start": sleep_start,
        "sleep_end": sleep_end,
        "sleep_minutes": duration_minutes,
        "actual_sleep_minutes": actual_sleep_minutes,
        "deep_sleep_minutes": deep_sleep_minutes,
        "rem_sleep_minutes": rem_sleep_minutes,
        "light_sleep_minutes": light_sleep_minutes,
        "awake_minutes": awake_minutes,
        "interruptions": interruptions,
        "interruption_minutes": interruption_minutes,
    }
    return row


def _normalize_nightly(entry: Any, fallback_date: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    day_iso = _date_str(
        entry.get("sleepResultDate") or entry.get("date") or entry.get("created")
    ) or fallback_date
    raw_json = _json_dump(entry)
    return {
        "date": day_iso,
        "raw_json": raw_json,
        "ans_status": _num(entry.get("ansStatus")),
        "recovery_indicator": _num(entry.get("recoveryIndicator")),
        "recovery_indicator_sublevel": _num(entry.get("recoveryIndicatorSubLevel")),
        "ans_rate": _num(entry.get("ansRate")),
        "mean_recovery_rri": _num(entry.get("meanNightlyRecoveryRri")),
        "mean_recovery_rmssd": _num(entry.get("meanNightlyRecoveryRmssd")),
        "mean_recovery_respiration_interval": _num(entry.get("meanNightlyRecoveryRespirationInterval")),
    }


def _normalize_continuous_payload(payload: Any, fallback_date: str) -> list[dict[str, Any]]:
    day_blocks = _continuous_day_blocks(payload, fallback_date)
    rows: list[dict[str, Any]] = []
    for block in day_blocks:
        row = _normalize_continuous_day(block, fallback_date)
        if row:
            rows.append(row)
    if not rows:
        log_polar_sync("sync_day:continuous_samples", "empty", f"{fallback_date}: keine HR-Samples")
    return rows


def _normalize_continuous_day(entry: Any, fallback_date: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    day_iso = _date_str(entry.get("date")) or fallback_date
    samples = entry.get("samples")
    if not isinstance(samples, list) or not samples:
        return None
    hr_values: list[float] = []
    low_trigger_values: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        heart_rate = _num(sample.get("heartRate"))
        if heart_rate is None:
            continue
        value = float(heart_rate)
        hr_values.append(value)
        if str(sample.get("triggerType") or "").strip() == "TRIGGER_LOW_247":
            low_trigger_values.append(value)
    if not hr_values:
        return None
    resting_pool = sorted(low_trigger_values) if low_trigger_values else sorted(hr_values)
    idx = max(0, int(math.floor((len(resting_pool) - 1) * 0.1)))
    return {
        "date": day_iso,
        "raw_json": _json_dump(entry),
        "sample_count": len(hr_values),
        "min_hr": round(min(hr_values), 2),
        "max_hr": round(max(hr_values), 2),
        "avg_hr": round(sum(hr_values) / len(hr_values), 2),
        "resting_candidate_hr": round(resting_pool[idx], 2),
    }


def _normalize_activity(entry: Any, fallback_date: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    day_iso = _date_str(entry.get("date") or entry.get("startTime") or entry.get("start_time")) or fallback_date
    device_activity = _pick_activity_device(entry.get("activitiesPerDevice"))
    active_minutes = _minutes_from_any(
        entry.get("activeDuration")
        or entry.get("active_minutes")
        or _nested_get(device_activity, "activeDuration")
        or _nested_get(device_activity, "activity.activeDuration")
    )
    if active_minutes is None:
        active_minutes = _minutes_from_any(_nested_get(entry, "activity.activeDuration"))
    return {
        "date": day_iso,
        "raw_json": _json_dump(entry),
        "steps": _first_int(
            entry.get("steps"),
            _nested_get(entry, "samples.steps.totalSteps"),
            _nested_get(device_activity, "steps"),
            _nested_get(device_activity, "samples.steps.totalSteps"),
            _nested_get(device_activity, "activity.steps"),
        ),
        "active_minutes": active_minutes,
        "inactivity_count": _first_int(
            entry.get("inactivityAlertCount"),
            entry.get("inactivity_count"),
            _nested_get(device_activity, "inactivityAlertCount"),
        ),
        "calories": _first_int(
            entry.get("calories"),
            _nested_get(device_activity, "calories"),
            _nested_get(device_activity, "activity.calories"),
            _nested_get(device_activity, "samples.calories"),
        ),
    }


def _normalize_training_session(entry: Any, fallback_date: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    session_id = _str_or_none(_nested_get(entry, "identifier.id") or entry.get("id"))
    if not session_id:
        log_polar_sync("sync_day:training_sessions", "unknown", f"{fallback_date}: Trainingseintrag ohne session_id")
        return None
    return {
        "session_id": session_id,
        "raw_json": _json_dump(entry),
        "start_time": _str_or_none(entry.get("startTime") or entry.get("start_time")),
        "stop_time": _str_or_none(entry.get("stopTime") or entry.get("stop_time")),
        "duration_seconds": _seconds_from_any(entry.get("durationMillis") or entry.get("duration")),
        "sport": _str_or_none(entry.get("sport")),
        "name": _str_or_none(entry.get("name")),
        "avg_hr": _num(entry.get("avgHeartRate") or entry.get("avg_hr")),
        "max_hr": _num(entry.get("maxHeartRate") or entry.get("max_hr")),
        "calories": _int(entry.get("calories")),
    }


def _upsert(table: str, pk: str, data: dict[str, Any]) -> None:
    now_iso = _utc_now_iso()
    with closing(get_polar_db()) as conn:
        existing = conn.execute(f"SELECT created_at FROM {table} WHERE {pk} = ?", (data[pk],)).fetchone()
        payload = dict(data)
        payload["created_at"] = existing["created_at"] if existing and existing["created_at"] else now_iso
        payload["updated_at"] = now_iso
        cols = list(payload.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != "created_at")
        conn.execute(
            f"""
            INSERT INTO {table} ({", ".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT({pk}) DO UPDATE SET {updates}
            """,
            tuple(payload[col] for col in cols),
        )
        conn.commit()


def _sleep_detail_keys() -> tuple[str, ...]:
    return (
        "sleep_start",
        "sleep_end",
        "sleep_minutes",
        "actual_sleep_minutes",
        "deep_sleep_minutes",
        "rem_sleep_minutes",
        "light_sleep_minutes",
        "awake_minutes",
        "interruptions",
        "interruption_minutes",
    )


def _sleep_payload_has_content(row: dict[str, Any]) -> bool:
    if _num(row.get("sleep_score")) is not None:
        return True
    return any(
        (_num(row.get(key)) is not None) or (key in ("sleep_start", "sleep_end") and bool(row.get(key)))
        for key in _sleep_detail_keys()
    )


def _sleep_payload_has_details(row: dict[str, Any]) -> bool:
    return any(
        (_num(row.get(key)) is not None) or (key in ("sleep_start", "sleep_end") and bool(row.get(key)))
        for key in _sleep_detail_keys()
    )


def _sleep_merge_rows(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    existing_dict = {key: existing[key] for key in existing.keys()} if existing else None
    if not existing:
        return dict(incoming)
    merged = dict(existing_dict or {})
    for key, value in incoming.items():
        if key == "date":
            merged[key] = value
            continue
        if key == "raw_json":
            existing_has_content = _sleep_payload_has_content(existing_dict or {})
            incoming_has_content = _sleep_payload_has_content(incoming)
            if incoming_has_content or not existing_has_content:
                merged[key] = value
            continue
        if value is not None and value != "":
            merged[key] = value
    return merged


def _upsert_sleep_row(data: dict[str, Any]) -> None:
    now_iso = _utc_now_iso()
    with closing(get_polar_db()) as conn:
        existing = conn.execute("SELECT * FROM polar_sleep WHERE date = ?", (data["date"],)).fetchone()
        payload = _sleep_merge_rows(existing, data)
        created_at = existing["created_at"] if existing and existing["created_at"] else now_iso
        payload["created_at"] = created_at
        payload["updated_at"] = now_iso
        cols = list(payload.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != "created_at")
        conn.execute(
            f"""
            INSERT INTO polar_sleep ({", ".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(date) DO UPDATE SET {updates}
            """,
            tuple(payload[col] for col in cols),
        )
        conn.commit()


def _latest_row(conn, table: str, order_col: str):
    return conn.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 1").fetchone()


def _by_pk(conn, table: str, pk: str, value: str):
    return conn.execute(f"SELECT * FROM {table} WHERE {pk} = ? LIMIT 1", (value,)).fetchone()


def _rows_since(conn, table: str, date_col: str, days: int) -> list[dict[str, Any]]:
    threshold = (date.today() - timedelta(days=max(0, days - 1))).isoformat()
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {date_col} >= ? ORDER BY {date_col} DESC", (threshold,)).fetchall()]


def _compact_sleep(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    data = {
        k: row[k]
        for k in (
            "date",
            "sleep_score",
            "sleep_start",
            "sleep_end",
            "sleep_minutes",
            "actual_sleep_minutes",
            "deep_sleep_minutes",
            "rem_sleep_minutes",
            "light_sleep_minutes",
            "awake_minutes",
            "interruptions",
            "interruption_minutes",
        )
        if k in keys
    }
    data["has_real_values"] = _sleep_has_real_values(row)
    return data


def _compact_nightly(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    data = {k: row[k] for k in ("date", "ans_status", "recovery_indicator", "recovery_indicator_sublevel", "ans_rate", "mean_recovery_rri", "mean_recovery_rmssd") if k in keys}
    data["has_real_values"] = _nightly_has_real_values(row)
    return data


def _compact_activity(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    data = {k: row[k] for k in ("date", "steps", "active_minutes", "inactivity_count", "calories") if k in keys}
    data["has_real_values"] = _activity_has_real_values(row)
    return data


def _compact_continuous(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else ()
    data = {k: row[k] for k in ("date", "sample_count", "min_hr", "max_hr", "avg_hr", "resting_candidate_hr") if k in keys}
    data["has_real_values"] = _continuous_has_real_values(row)
    return data


def _compact_training(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    return {k: row[k] for k in ("session_id", "start_time", "stop_time", "duration_seconds", "sport", "name", "avg_hr", "max_hr", "calories")}


def _sleep_has_real_values(row: Any) -> bool:
    return any(
        _num(_value_get(row, key)) is not None
        for key in (
            "sleep_score",
            "sleep_minutes",
            "actual_sleep_minutes",
            "deep_sleep_minutes",
            "rem_sleep_minutes",
            "light_sleep_minutes",
            "awake_minutes",
            "interruptions",
            "interruption_minutes",
        )
    ) or bool(_value_get(row, "sleep_start")) or bool(_value_get(row, "sleep_end"))


def _nightly_has_real_values(row: Any) -> bool:
    return any(
        _num(_value_get(row, key)) is not None
        for key in (
            "ans_status",
            "recovery_indicator",
            "recovery_indicator_sublevel",
            "ans_rate",
            "mean_recovery_rri",
            "mean_recovery_rmssd",
            "mean_recovery_respiration_interval",
        )
    )


def _activity_has_real_values(row: Any) -> bool:
    return any(_num(_value_get(row, key)) is not None for key in ("steps", "active_minutes", "inactivity_count", "calories"))


def _continuous_has_real_values(row: Any) -> bool:
    sample_count = _num(_value_get(row, "sample_count"))
    return bool((sample_count is not None and sample_count > 0) or _num(_value_get(row, "avg_hr")) is not None)


def _activity_has_day_without_details(row: Any) -> bool:
    if not row or _activity_has_real_values(row):
        return False
    raw = _json_loads(_value_get(row, "raw_json"), {})
    if not isinstance(raw, dict):
        return False
    activities = raw.get("activitiesPerDevice")
    return isinstance(activities, list) and len(activities) == 0


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [_num(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _value_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _as_items(payload: Any) -> list[Any]:
    if payload in (None, "", []):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (
            "data",
            "items",
            "samples",
            "sessions",
            "activities",
            "activityDays",
            "continuousSamples",
            "nightSleeps",
            "sleeps",
            "nightlyRechargeResults",
            "results",
        ):
            value = payload.get(key)
            items = _as_items(value) if isinstance(value, dict) else (value if isinstance(value, list) else None)
            if isinstance(items, list) and items:
                return items
            if isinstance(value, list):
                return value
        return [payload]
    return []


def _pick_activity_device(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict):
            return item
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _int(value)
        if parsed is not None:
            return parsed
    return None


def _json_loads(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return default


def _continuous_day_blocks(payload: Any, fallback_date: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        per_day = payload.get("heartRateSamplesPerDay")
        if isinstance(per_day, list):
            return [item for item in per_day if isinstance(item, dict)]
    entries = _as_items(payload)
    blocks: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("samples"), list):
            blocks.append(entry)
            continue
        nested = entry.get("heartRateSamplesPerDay")
        if isinstance(nested, list):
            blocks.extend(item for item in nested if isinstance(item, dict))
    if not blocks and isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict) and isinstance(entry.get("samples"), list):
                blocks.append(entry)
    return blocks


def _extract_hr_values(entry: Any) -> list[float]:
    values: list[float] = []
    if isinstance(entry, dict):
        for key in ("heartRate", "heart_rate", "hr", "bpm", "value"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                values.append(float(value))
        for child_key in ("samples", "heartRateSamples", "heart_rate_samples", "continuousSamples", "data"):
            child = entry.get(child_key)
            if isinstance(child, list):
                for item in child:
                    values.extend(_extract_hr_values(item))
            elif isinstance(child, dict):
                for nested_value in child.values():
                    values.extend(_extract_hr_values(nested_value))
    elif isinstance(entry, list):
        for item in entry:
            values.extend(_extract_hr_values(item))
    elif isinstance(entry, (int, float)):
        values.append(float(entry))
    return [value for value in values if 20 <= float(value) <= 260]


def _nested_get(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    if number is None:
        return None
    try:
        return int(round(number))
    except Exception:
        return None


def _date_str(value: Any) -> str | None:
    text = _str_or_none(value)
    if not text:
        return None
    if len(text) >= 10:
        return text[:10]
    return text


def _minutes_from_any(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if float(value) > 100000:
            return int(round(float(value) / 60000.0))
        return int(round(float(value)))
    text = str(value).strip()
    if text.endswith("s"):
        try:
            return int(round(float(text[:-1]) / 60.0))
        except Exception:
            return None
    if text.startswith("PT"):
        seconds = _parse_iso_duration_seconds(text)
        return int(round(seconds / 60.0)) if seconds is not None else None
    try:
        return int(round(float(text)))
    except Exception:
        return None


def _seconds_from_any(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        return int(round(raw / 1000.0)) if raw > 100000 else int(round(raw))
    text = str(value).strip()
    if text.endswith("s"):
        try:
            return int(round(float(text[:-1])))
        except Exception:
            return None
    if text.startswith("PT"):
        return _parse_iso_duration_seconds(text)
    try:
        raw = float(text)
        return int(round(raw / 1000.0)) if raw > 100000 else int(round(raw))
    except Exception:
        return None


def _parse_iso_duration_seconds(text: str) -> int | None:
    try:
        body = text.strip().removeprefix("PT")
        hours = minutes = seconds = 0.0
        current = ""
        for char in body:
            if char.isdigit() or char == ".":
                current += char
                continue
            if not current:
                continue
            if char == "H":
                hours = float(current)
            elif char == "M":
                minutes = float(current)
            elif char == "S":
                seconds = float(current)
            current = ""
        return int(round(hours * 3600 + minutes * 60 + seconds))
    except Exception:
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_minutes(*values: Any) -> int | None:
    for value in values:
        parsed = _minutes_from_any(value)
        if parsed is not None:
            return parsed
    return None


def _minutes_between(start_value: Any, end_value: Any) -> int | None:
    start = _parse_dt_any(start_value)
    end = _parse_dt_any(end_value)
    if not start or not end:
        return None
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    return int(round(seconds / 60.0))


def _parse_dt_any(value: Any) -> datetime | None:
    text = _str_or_none(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None
