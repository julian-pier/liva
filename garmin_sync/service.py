from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from garminconnect import Garmin

from database.connections import get_runs_db
from integrations.ntfy_client import send_ntfy_notification


REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENSTORE_PATH = Path(
    os.environ.get(
        "GARMIN_TOKENSTORE",
        "/var/lib/liva/.config/liva/garmin",
    )
).expanduser()
LOCK_PATH = REPO_ROOT / "var" / "garmin-sync.lock"
DEFAULT_LOOKBACK_DAYS = 14


class GarminNotConfiguredError(RuntimeError):
    pass


def _activity_id(raw_id: Any) -> int:
    text = str(raw_id or "").strip()
    if not text:
        raise ValueError("Garmin-Aktivitaet ohne ID")
    try:
        numeric = int(text)
    except ValueError:
        numeric = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:15], 16)
    return -abs(numeric)


def _first(activity: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = activity.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sport_type(activity: dict[str, Any]) -> str:
    raw_type = activity.get("activityType")
    if isinstance(raw_type, dict):
        raw_type = _first(raw_type, "typeKey", "typeNameKey", "typeId")
    text = str(raw_type or _first(activity, "activityTypeKey", "type") or "").lower()
    if any(token in text for token in ("run", "jog")):
        return "run"
    if any(token in text for token in ("stair", "stepper")):
        return "stair"
    if any(token in text for token in ("indoor_cycl", "indoor cycl", "spin", "ergometer")):
        return "ergo"
    if any(token in text for token in ("cycl", "bike", "biking")):
        return "bike"
    if any(token in text for token in ("walk", "hik")):
        return "walk"
    if any(token in text for token in ("row", "rowing")):
        return "row"
    if "swim" in text:
        return "swim"
    return "other"


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    external_id = _first(activity, "activityId", "id")
    distance = _number(_first(activity, "distance", "distanceMeters"))
    moving_time = _number(
        _first(activity, "movingDuration", "duration", "elapsedDuration", "moving_time")
    )
    pace = None
    if distance and distance > 0 and moving_time and moving_time > 0:
        pace = moving_time / (distance / 1000.0)
    activity_type = _sport_type(activity)
    return {
        "id": _activity_id(external_id),
        "external_id": f"garmin:{external_id}",
        "date": str(
            _first(activity, "startTimeLocal", "startTimeGMT", "start_date_local", "start_date")
            or ""
        ),
        "distance": distance,
        "moving_time": int(round(moving_time)) if moving_time is not None else None,
        "avg_speed": _number(_first(activity, "averageSpeed", "avgSpeed", "average_speed")),
        "avg_hr": _number(_first(activity, "averageHR", "averageHeartRate", "average_heartrate")),
        "max_hr": _number(_first(activity, "maxHR", "maxHeartRate", "max_heartrate")),
        "elevation_gain": _number(
            _first(activity, "elevationGain", "totalElevationGain", "total_elevation_gain")
        ),
        "pace": pace,
        "sport_type": activity_type,
        "note": str(_first(activity, "activityName", "name") or ""),
        "avg_power": _number(_first(activity, "avgPower", "averagePower", "averageWatts")),
        "calories": _number(_first(activity, "calories", "kilocalories")),
        "cadence": _number(_first(activity, "averageRunningCadenceInStepsPerMinute", "averageCadence")),
        "source": "garmin",
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    columns = {
        "source": "TEXT",
        "external_id": "TEXT",
        "calories": "REAL",
        "cadence": "REAL",
        "min_hr": "REAL",
        "elevation_loss": "REAL",
        "steps": "INTEGER",
        "stride_length": "REAL",
        "training_effect": "REAL",
        "location_name": "TEXT",
        "device_name": "TEXT",
    }
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_external_id ON runs(external_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS garmin_sync_notifications (
            external_id TEXT PRIMARY KEY,
            sent_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS garmin_activity_payloads (
            external_id TEXT PRIMARY KEY,
            garmin_activity_id TEXT NOT NULL,
            summary_json TEXT,
            details_json TEXT,
            splits_json TEXT,
            typed_splits_json TEXT,
            split_summaries_json TEXT,
            weather_json TEXT,
            hr_zones_json TEXT,
            power_zones_json TEXT,
            gear_json TEXT,
            errors_json TEXT,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS garmin_activity_files (
            external_id TEXT PRIMARY KEY,
            format TEXT NOT NULL,
            content BLOB NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS garmin_activity_laps (
            external_id TEXT NOT NULL,
            lap_index INTEGER NOT NULL,
            start_time TEXT,
            distance_m REAL,
            duration_sec REAL,
            moving_time_sec REAL,
            avg_hr REAL,
            max_hr REAL,
            avg_speed_mps REAL,
            max_speed_mps REAL,
            avg_cadence REAL,
            max_cadence REAL,
            calories REAL,
            elevation_gain_m REAL,
            elevation_loss_m REAL,
            min_elevation_m REAL,
            max_elevation_m REAL,
            stride_length_m REAL,
            start_latitude REAL,
            start_longitude REAL,
            end_latitude REAL,
            end_longitude REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (external_id, lap_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS garmin_activity_samples (
            external_id TEXT NOT NULL,
            sample_index INTEGER NOT NULL,
            timestamp_ms INTEGER,
            elapsed_sec REAL,
            moving_sec REAL,
            distance_m REAL,
            heart_rate_bpm REAL,
            speed_mps REAL,
            cadence_spm REAL,
            elevation_m REAL,
            latitude REAL,
            longitude REAL,
            vertical_speed_mps REAL,
            performance_condition REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (external_id, sample_index)
        )
        """
    )
    conn.commit()


def _duration_text(seconds: Any) -> str:
    total = max(0, int(round(_number(seconds) or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d} h"
    return f"{minutes}:{secs:02d} min"


def _run_notification_text(row: tuple[Any, ...]) -> str:
    _external_id, name, _started_at, distance, moving_time, avg_hr = row
    label = str(name or "Neuer Lauf").strip() or "Neuer Lauf"
    details = []
    distance_value = _number(distance)
    if distance_value is not None:
        details.append(f"{distance_value / 1000.0:.2f}".replace(".", ",") + " km")
    if _number(moving_time):
        details.append(_duration_text(moving_time))
    if _number(avg_hr):
        details.append(f"Ø {int(round(float(avg_hr)))} bpm")
    suffix = " · ".join(details)
    return f"Neuer Lauf: {label}" + (f" · {suffix}" if suffix else "")


def _notify_pending_runs(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT r.external_id, r.note, r.date, r.distance, r.moving_time, r.avg_hr
        FROM runs r
        LEFT JOIN garmin_sync_notifications n ON n.external_id = r.external_id
        WHERE r.source = 'garmin'
          AND r.sport_type = 'run'
          AND r.external_id IS NOT NULL
          AND n.sent_at IS NULL
        ORDER BY r.date ASC
        """
    ).fetchall()
    sent = 0
    failed = 0
    for row in rows:
        result = send_ntfy_notification(
            _run_notification_text(tuple(row)),
            title="LIVA · Garmin",
            priority=3,
            tags="runner,white_check_mark",
        )
        if result.get("sent"):
            conn.execute(
                """
                INSERT INTO garmin_sync_notifications (external_id, sent_at, attempts, last_error, updated_at)
                VALUES (?, CURRENT_TIMESTAMP, 1, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(external_id) DO UPDATE SET
                    sent_at=CURRENT_TIMESTAMP,
                    attempts=garmin_sync_notifications.attempts + 1,
                    last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (row[0],),
            )
            sent += 1
        else:
            conn.execute(
                """
                INSERT INTO garmin_sync_notifications (external_id, sent_at, attempts, last_error, updated_at)
                VALUES (?, NULL, 1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(external_id) DO UPDATE SET
                    attempts=garmin_sync_notifications.attempts + 1,
                    last_error=excluded.last_error,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (row[0], str(result.get("error") or "unknown_error")[:300]),
            )
            failed += 1
        conn.commit()
    return {"notifications_sent": sent, "notifications_failed": failed}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _optional_garmin_call(errors: dict[str, str], name: str, call) -> Any:
    try:
        return call()
    except Exception as exc:  # Optional Garmin detail endpoints vary by activity/device.
        errors[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return None


def _replace_laps(conn: sqlite3.Connection, external_id: str, splits: Any) -> int:
    payload = splits if isinstance(splits, dict) else {}
    laps = payload.get("lapDTOs") if isinstance(payload.get("lapDTOs"), list) else []
    conn.execute("DELETE FROM garmin_activity_laps WHERE external_id=?", (external_id,))
    for offset, lap in enumerate(laps, start=1):
        if not isinstance(lap, dict):
            continue
        lap_index = int(_number(lap.get("lapIndex")) or offset)
        conn.execute(
            """
            INSERT INTO garmin_activity_laps (
                external_id, lap_index, start_time, distance_m, duration_sec,
                moving_time_sec, avg_hr, max_hr, avg_speed_mps, max_speed_mps,
                avg_cadence, max_cadence, calories, elevation_gain_m,
                elevation_loss_m, min_elevation_m, max_elevation_m,
                stride_length_m, start_latitude, start_longitude,
                end_latitude, end_longitude, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                external_id,
                lap_index,
                _first(lap, "startTimeLocal", "startTimeGMT"),
                _number(lap.get("distance")),
                _number(lap.get("duration")),
                _number(lap.get("movingDuration")),
                _number(lap.get("averageHR")),
                _number(lap.get("maxHR")),
                _number(_first(lap, "averageMovingSpeed", "averageSpeed")),
                _number(lap.get("maxSpeed")),
                _number(_first(lap, "averageRunCadence", "averageCadence")),
                _number(_first(lap, "maxRunCadence", "maxCadence")),
                _number(lap.get("calories")),
                _number(lap.get("elevationGain")),
                _number(lap.get("elevationLoss")),
                _number(lap.get("minElevation")),
                _number(lap.get("maxElevation")),
                _number(lap.get("strideLength")),
                _number(lap.get("startLatitude")),
                _number(lap.get("startLongitude")),
                _number(lap.get("endLatitude")),
                _number(lap.get("endLongitude")),
                _json_text(lap),
            ),
        )
    return len(laps)


def _detail_metrics(details: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors = details.get("metricDescriptors") or []
    key_by_index = {
        int(item["metricsIndex"]): str(item["key"])
        for item in descriptors
        if isinstance(item, dict) and _number(item.get("metricsIndex")) is not None and item.get("key")
    }
    samples = []
    for raw in details.get("activityDetailMetrics") or []:
        values = raw.get("metrics") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            continue
        samples.append({key: values[index] for index, key in key_by_index.items() if index < len(values)})
    return samples


def _replace_samples(conn: sqlite3.Connection, external_id: str, details: Any) -> int:
    payload = details if isinstance(details, dict) else {}
    samples = _detail_metrics(payload)
    conn.execute("DELETE FROM garmin_activity_samples WHERE external_id=?", (external_id,))
    for index, sample in enumerate(samples):
        timestamp = _number(sample.get("directTimestamp"))
        conn.execute(
            """
            INSERT INTO garmin_activity_samples (
                external_id, sample_index, timestamp_ms, elapsed_sec, moving_sec,
                distance_m, heart_rate_bpm, speed_mps, cadence_spm, elevation_m,
                latitude, longitude, vertical_speed_mps, performance_condition, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                external_id,
                index,
                int(timestamp) if timestamp is not None else None,
                _number(_first(sample, "sumElapsedDuration", "sumDuration")),
                _number(sample.get("sumMovingDuration")),
                _number(sample.get("sumDistance")),
                _number(sample.get("directHeartRate")),
                _number(sample.get("directSpeed")),
                _number(_first(sample, "directDoubleCadence", "directRunCadence")),
                _number(_first(sample, "directCorrectedElevation", "directElevation", "directUncorrectedElevation")),
                _number(sample.get("directLatitude")),
                _number(sample.get("directLongitude")),
                _number(sample.get("directVerticalSpeed")),
                _number(sample.get("directPerformanceCondition")),
                _json_text(sample),
            ),
        )
    return len(samples)


def _update_run_from_summary(
    conn: sqlite3.Connection, external_id: str, summary_payload: Any
) -> None:
    payload = summary_payload if isinstance(summary_payload, dict) else {}
    summary = payload.get("summaryDTO") if isinstance(payload.get("summaryDTO"), dict) else {}
    metadata = payload.get("metadataDTO") if isinstance(payload.get("metadataDTO"), dict) else {}
    device = metadata.get("deviceMetaDataDTO") if isinstance(metadata.get("deviceMetaDataDTO"), dict) else {}
    conn.execute(
        """
        UPDATE runs SET
            min_hr=COALESCE(?, min_hr),
            elevation_loss=COALESCE(?, elevation_loss),
            steps=COALESCE(?, steps),
            stride_length=COALESCE(?, stride_length),
            training_effect=COALESCE(?, training_effect),
            location_name=COALESCE(?, location_name),
            device_name=COALESCE(?, device_name)
        WHERE external_id=?
        """,
        (
            _number(summary.get("minHR")),
            _number(summary.get("elevationLoss")),
            int(_number(summary.get("steps"))) if _number(summary.get("steps")) is not None else None,
            _number(summary.get("strideLength")),
            _number(summary.get("trainingEffect")),
            str(payload.get("locationName") or "") or None,
            str(_first(device, "deviceName", "manufacturer") or metadata.get("manufacturer") or "") or None,
            external_id,
        ),
    )


def _enrich_activity(client: Garmin, activity: dict[str, Any]) -> dict[str, int]:
    raw_id = str(_first(activity, "activityId", "id") or "").strip()
    if not raw_id or _sport_type(activity) != "run":
        return {"details_fetched": 0, "laps_stored": 0, "samples_stored": 0, "files_stored": 0}
    external_id = f"garmin:{raw_id}"
    conn = get_runs_db()
    try:
        _ensure_schema(conn)
        existing = conn.execute(
            "SELECT 1 FROM garmin_activity_payloads WHERE external_id=? AND details_json IS NOT NULL",
            (external_id,),
        ).fetchone()
        existing_file = conn.execute(
            "SELECT 1 FROM garmin_activity_files WHERE external_id=?", (external_id,)
        ).fetchone()
        if existing and existing_file:
            return {"details_fetched": 0, "laps_stored": 0, "samples_stored": 0, "files_stored": 0}

        errors: dict[str, str] = {}
        payloads = {
            "summary": _optional_garmin_call(errors, "summary", lambda: client.get_activity(raw_id)),
            "details": _optional_garmin_call(errors, "details", lambda: client.get_activity_details(raw_id)),
            "splits": _optional_garmin_call(errors, "splits", lambda: client.get_activity_splits(raw_id)),
            "typed_splits": _optional_garmin_call(errors, "typed_splits", lambda: client.get_activity_typed_splits(raw_id)),
            "split_summaries": _optional_garmin_call(errors, "split_summaries", lambda: client.get_activity_split_summaries(raw_id)),
            "weather": _optional_garmin_call(errors, "weather", lambda: client.get_activity_weather(raw_id)),
            "hr_zones": _optional_garmin_call(errors, "hr_zones", lambda: client.get_activity_hr_in_timezones(raw_id)),
            "power_zones": _optional_garmin_call(errors, "power_zones", lambda: client.get_activity_power_in_timezones(raw_id)),
            "gear": _optional_garmin_call(errors, "gear", lambda: client.get_activity_gear(raw_id)),
        }
        conn.execute(
            """
            INSERT INTO garmin_activity_payloads (
                external_id, garmin_activity_id, summary_json, details_json,
                splits_json, typed_splits_json, split_summaries_json, weather_json,
                hr_zones_json, power_zones_json, gear_json, errors_json, fetched_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(external_id) DO UPDATE SET
                summary_json=excluded.summary_json,
                details_json=excluded.details_json,
                splits_json=excluded.splits_json,
                typed_splits_json=excluded.typed_splits_json,
                split_summaries_json=excluded.split_summaries_json,
                weather_json=excluded.weather_json,
                hr_zones_json=excluded.hr_zones_json,
                power_zones_json=excluded.power_zones_json,
                gear_json=excluded.gear_json,
                errors_json=excluded.errors_json,
                fetched_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                external_id,
                raw_id,
                *(_json_text(payloads[name]) if payloads[name] is not None else None for name in (
                    "summary", "details", "splits", "typed_splits", "split_summaries",
                    "weather", "hr_zones", "power_zones", "gear"
                )),
                _json_text(errors),
            ),
        )
        laps = _replace_laps(conn, external_id, payloads["splits"])
        samples = _replace_samples(conn, external_id, payloads["details"])
        _update_run_from_summary(conn, external_id, payloads["summary"])
        files_stored = 0
        if not existing_file:
            original = _optional_garmin_call(
                errors,
                "original_file",
                lambda: client.download_activity(raw_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL),
            )
            if isinstance(original, bytes) and original:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO garmin_activity_files
                    (external_id, format, content, size_bytes, sha256, downloaded_at)
                    VALUES (?, 'original', ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (external_id, original, len(original), hashlib.sha256(original).hexdigest()),
                )
                files_stored = 1
        if errors:
            conn.execute(
                "UPDATE garmin_activity_payloads SET errors_json=?, updated_at=CURRENT_TIMESTAMP WHERE external_id=?",
                (_json_text(errors), external_id),
            )
        conn.commit()
        return {
            "details_fetched": 1,
            "laps_stored": laps,
            "samples_stored": samples,
            "files_stored": files_stored,
        }
    finally:
        conn.close()


def _enrich_activities(client: Garmin, activities: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"details_fetched": 0, "laps_stored": 0, "samples_stored": 0, "files_stored": 0}
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        result = _enrich_activity(client, activity)
        for key in totals:
            totals[key] += int(result.get(key) or 0)
    return totals


def get_garmin_activity_backend_payload(activity_id: str) -> dict[str, Any] | None:
    raw_id = str(activity_id or "").removeprefix("garmin:").strip()
    external_id = f"garmin:{raw_id}"
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        payload = conn.execute(
            "SELECT * FROM garmin_activity_payloads WHERE external_id=?", (external_id,)
        ).fetchone()
        if not payload:
            return None
        parsed = dict(payload)
        for key in tuple(parsed):
            if key.endswith("_json"):
                try:
                    parsed[key.removesuffix("_json")] = json.loads(parsed.pop(key)) if parsed[key] else None
                except (TypeError, ValueError):
                    parsed[key.removesuffix("_json")] = None
        parsed["laps"] = [dict(row) for row in conn.execute(
            "SELECT * FROM garmin_activity_laps WHERE external_id=? ORDER BY lap_index", (external_id,)
        ).fetchall()]
        parsed["samples"] = [dict(row) for row in conn.execute(
            "SELECT * FROM garmin_activity_samples WHERE external_id=? ORDER BY sample_index", (external_id,)
        ).fetchall()]
        file_row = conn.execute(
            "SELECT format, size_bytes, sha256, downloaded_at FROM garmin_activity_files WHERE external_id=?",
            (external_id,),
        ).fetchone()
        parsed["original_file"] = dict(file_row) if file_row else None
        return parsed
    finally:
        conn.close()


def _store_activities(activities: list[dict[str, Any]]) -> dict[str, int]:
    normalized = [normalize_activity(item) for item in activities if isinstance(item, dict)]
    normalized = [item for item in normalized if item["date"] and item["sport_type"] != "other"]
    conn = get_runs_db()
    try:
        _ensure_schema(conn)
        known = {
            row[0]
            for row in conn.execute(
                "SELECT external_id FROM runs WHERE external_id IS NOT NULL"
            ).fetchall()
        }
        for item in normalized:
            conn.execute(
                """
                INSERT INTO runs (
                    id, date, distance, moving_time, avg_speed, avg_hr, max_hr,
                    elevation_gain, pace, sport_type, note, avg_power, source,
                    external_id, calories, cadence
                ) VALUES (
                    :id, :date, :distance, :moving_time, :avg_speed, :avg_hr, :max_hr,
                    :elevation_gain, :pace, :sport_type, :note, :avg_power, :source,
                    :external_id, :calories, :cadence
                )
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    distance=excluded.distance,
                    moving_time=excluded.moving_time,
                    avg_speed=excluded.avg_speed,
                    avg_hr=excluded.avg_hr,
                    max_hr=excluded.max_hr,
                    elevation_gain=excluded.elevation_gain,
                    pace=excluded.pace,
                    sport_type=excluded.sport_type,
                    note=excluded.note,
                    avg_power=excluded.avg_power,
                    source=excluded.source,
                    external_id=excluded.external_id,
                    calories=excluded.calories,
                    cadence=excluded.cadence
                """,
                item,
            )
        conn.commit()
        notifications = _notify_pending_runs(conn)
    finally:
        conn.close()
    return {
        "received": len(activities),
        "stored": len(normalized),
        "imported": sum(item["external_id"] not in known for item in normalized),
        "updated": sum(item["external_id"] in known for item in normalized),
        **notifications,
    }


def _garmin_client() -> Garmin:
    token_file = TOKENSTORE_PATH / "garmin_tokens.json"
    if not token_file.is_file():
        raise GarminNotConfiguredError(
            "Garmin ist noch nicht verbunden. Einmalige Garmin-Anmeldung erforderlich."
        )
    client = Garmin()
    client.login(str(TOKENSTORE_PATH))
    return client


def sync_garmin_to_database(
    *, blocking: bool = True, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> dict[str, Any]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock_file.fileno(), flags)
        except BlockingIOError:
            return {"skipped": True, "reason": "already_running", "imported": 0}

        today = date.today()
        start = today - timedelta(days=max(1, int(lookback_days)))
        client = _garmin_client()
        activities = client.get_activities_by_date(
            start.isoformat(), today.isoformat()
        )
        activity_list = activities if isinstance(activities, list) else []
        summary = _store_activities(activity_list)
        enrichment = _enrich_activities(client, activity_list)
        return {"skipped": False, "reason": None, **summary, **enrichment}
