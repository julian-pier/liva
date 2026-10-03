from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from activity.api import activity_bp
from activity.iphone_daily import current_usage_day, usage_day_bounds
from activity.repository import ensure_activity_schema


@pytest.fixture()
def daily_app(tmp_path, monkeypatch):
    db_path = tmp_path / "digital_activity.sqlite3"
    monkeypatch.setenv("LIVA_ACTIVITY_DB", str(db_path))
    monkeypatch.setenv("LIVA_ACTIVITY_INGEST_KEY", "activity-write-key")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(activity_bp)
    return app, db_path


def payload(*, usage_day="2026-08-16", total=8906, platform="ios", source="stayfree_ios"):
    start, end = usage_day_bounds(usage_day)
    return {
        "platform": platform,
        "source": source,
        "device": {
            "id": "77ebf338-f307-5c9f-b956-f70ad6a4518e",
            "name": "iPhone 13",
            "model": "iPhone14,5",
        },
        "usage_day": usage_day,
        "day_start": start,
        "day_end": end,
        "total_usage_seconds": total,
        "source_updated_at": "2026-08-17T02:12:00Z",
        "apps": [
            {"app_key": "net.whatsapp.WhatsApp", "name": "WhatsApp", "duration_seconds": 3120},
            {"app_key": "com.burbn.instagram", "name": "Instagram", "duration_seconds": 2280},
            {"app_key": "com.google.ios.youtube", "name": "YouTube", "duration_seconds": 1860},
            {"app_key": "com.apple.mobilesafari", "name": "Safari", "duration_seconds": 1000},
            {"app_key": "com.openai.chat", "name": "ChatGPT", "duration_seconds": 646},
        ],
    }


def post(client, body):
    return client.post(
        "/api/activity/iphone/daily",
        json=body,
        headers={"Authorization": "Bearer activity-write-key"},
    )


def test_daily_schema_initializes(daily_app):
    _, db_path = daily_app
    ensure_activity_schema()
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"iphone_daily_usage", "iphone_daily_apps", "iphone_import_runs"}.issubset(tables)


def test_legacy_live_cleanup_migration_keeps_daily_device(tmp_path):
    db_path = tmp_path / "cleanup.sqlite3"
    legacy_tables = {
        "iphone_app_sessions", "iphone_screen_sessions", "iphone_telemetry_gaps",
        "iphone_watcher_health", "iphone_current_state", "iphone_app_catalog",
        "iphone_state_events", "iphone_shortcut_events", "iphone_current_activity",
        "iphone_app_totals", "iphone_domain_totals", "iphone_sync_log",
        "iphone_activity_snapshots",
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE iphone_devices(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE iphone_daily_usage(device_id TEXT NOT NULL)")
        conn.executemany("INSERT INTO iphone_devices VALUES (?)", [("stayfree",), ("legacy",)])
        conn.execute("INSERT INTO iphone_daily_usage VALUES ('stayfree')")
        for table in legacy_tables:
            conn.execute(f"CREATE TABLE {table}(id TEXT)")
        migration = Path("database/migrations/2026_08_17_remove_legacy_iphone_live.sql").read_text()
        conn.executescript(migration)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        devices = [row[0] for row in conn.execute("SELECT id FROM iphone_devices ORDER BY id")]
    assert not legacy_tables.intersection(tables)
    assert devices == ["stayfree"]


def test_retired_live_iphone_endpoints_are_absent(daily_app):
    app, _ = daily_app
    client = app.test_client()
    assert client.get("/api/activity/iphone/current").status_code == 404
    assert client.get("/api/activity/iphone/health").status_code == 404
    assert client.get("/api/activity/iphone/experimental/current").status_code == 404
    assert client.post("/api/activity/iphone/snapshot", json={}).status_code == 404


def test_insert_read_and_exact_authoritative_total(daily_app):
    app, _ = daily_app
    client = app.test_client()
    response = post(client, payload())
    assert response.status_code == 200
    body = client.get("/api/activity/iphone/summary?date=2026-08-16").get_json()
    assert body["available"] is True
    assert body["status"] == "complete"
    assert body["total_usage_seconds"] == 8906
    assert [app["name"] for app in body["apps"]] == ["WhatsApp", "Instagram", "YouTube", "Safari", "ChatGPT"]


def test_daily_reimport_is_idempotent_and_can_update_day(daily_app):
    app, db_path = daily_app
    client = app.test_client()
    assert post(client, payload()).status_code == 200
    assert post(client, payload()).status_code == 200
    changed = payload(total=9000)
    changed["apps"][0]["duration_seconds"] = 3214
    result = post(client, changed)
    assert result.status_code == 200
    assert result.get_json()["created"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_daily_usage").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM iphone_daily_apps").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM iphone_import_runs").fetchone()[0] == 3
        assert conn.execute("SELECT total_usage_seconds FROM iphone_daily_usage").fetchone()[0] == 9000


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("platform", "windows", "only ios"),
        ("source", "stayfree_windows", "only stayfree_ios"),
        ("total_usage_seconds", -1, "out of range"),
    ],
)
def test_daily_rejects_non_ios_wrong_source_and_negative_times(daily_app, field, value, detail):
    app, _ = daily_app
    body = payload()
    body[field] = value
    response = post(app.test_client(), body)
    assert response.status_code == 400
    assert detail in response.get_json()["detail"]


def test_daily_rejects_duplicate_apps_and_sum_above_total(daily_app):
    app, _ = daily_app
    duplicate = payload()
    duplicate["apps"].append(dict(duplicate["apps"][0]))
    assert "duplicate app" in post(app.test_client(), duplicate).get_json()["detail"]
    too_small = payload(total=100)
    assert "exceeds" in post(app.test_client(), too_small).get_json()["detail"]


def test_usage_day_bounds_are_four_am_and_dst_safe():
    winter_start, winter_end = usage_day_bounds("2026-01-15")
    spring_start, spring_end = usage_day_bounds("2026-03-28")
    autumn_start, autumn_end = usage_day_bounds("2026-10-24")
    assert winter_start.endswith("04:00:00+01:00") and winter_end.endswith("04:00:00+01:00")
    assert spring_start.endswith("04:00:00+01:00") and spring_end.endswith("04:00:00+02:00")
    assert autumn_start.endswith("04:00:00+02:00") and autumn_end.endswith("04:00:00+01:00")
    spring_seconds = (
        datetime.fromisoformat(spring_end).astimezone(timezone.utc)
        - datetime.fromisoformat(spring_start).astimezone(timezone.utc)
    ).total_seconds()
    autumn_seconds = (
        datetime.fromisoformat(autumn_end).astimezone(timezone.utc)
        - datetime.fromisoformat(autumn_start).astimezone(timezone.utc)
    ).total_seconds()
    assert spring_seconds == 23 * 3600
    assert autumn_seconds == 25 * 3600


def test_current_usage_day_rolls_at_four_am():
    assert current_usage_day(datetime(2026, 8, 17, 1, 59, tzinfo=timezone.utc)) == "2026-08-16"
    assert current_usage_day(datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)) == "2026-08-17"


def test_missing_day_is_not_reported_as_zero(daily_app, monkeypatch):
    app, _ = daily_app
    monkeypatch.setattr("activity.iphone_daily.current_usage_day", lambda now=None: "2026-08-17")
    current = app.test_client().get("/api/activity/iphone/summary?date=2026-08-17").get_json()
    historic = app.test_client().get("/api/activity/iphone/summary?date=2026-08-15").get_json()
    assert current["available"] is False and current["status"] == "not_completed"
    assert current["total_usage_seconds"] is None
    assert historic["available"] is False and historic["status"] == "not_imported"


def test_windows_phase_one_rows_are_untouched_by_daily_import(daily_app):
    app, db_path = daily_app
    ensure_activity_schema()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "Windows-PC", "windows", "test", "a", "a", "a", "a"),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM devices WHERE platform='windows'").fetchone()[0]
    assert post(app.test_client(), payload()).status_code == 200
    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM devices WHERE platform='windows'").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 0
    assert after == before
