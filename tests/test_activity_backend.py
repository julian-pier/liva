from __future__ import annotations

import sqlite3
import uuid

import pytest
from flask import Flask

from activity.api import activity_bp
from activity.iphone_daily import usage_day_bounds
from activity.repository import app_label, ensure_activity_schema, website_label
from security.private_access import enforce_key_read_only, enforce_private_access
from security.write_guard import enforce_write_protection


@pytest.fixture()
def activity_app(tmp_path, monkeypatch):
    db_path = tmp_path / "digital_activity.sqlite3"
    monkeypatch.setenv("LIVA_ACTIVITY_DB", str(db_path))
    monkeypatch.setenv("LIVA_ACTIVITY_INGEST_KEY", "activity-write-key")
    app = Flask(__name__)
    app.register_blueprint(activity_bp)
    app.config["TESTING"] = True
    return app, db_path


def _event(event_id: str, start: str, end: str, **overrides):
    payload = {
        "id": event_id,
        "started_at": start,
        "ended_at": end,
        "app_exe": "chrome.exe",
        "app_name": "Google Chrome",
        "window_title": "LIVA",
        "browser": "chrome",
        "domain": "example.com",
        "page_title": "Example",
        "is_idle": False,
        "source": "windows_agent",
        "track_kind": "foreground",
    }
    payload.update(overrides)
    return payload


def _batch(events, *, batch_id=None):
    return {
        "batch_id": batch_id or str(uuid.uuid4()),
        "device": {"id": str(uuid.uuid4()), "name": "Windows-PC", "platform": "windows", "agent_version": "0.1.0"},
        "events": events,
    }


def test_activity_schema_initializes_separate_tables(activity_app):
    _, db_path = activity_app
    ensure_activity_schema()
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "activity_events", "ingest_log"}.issubset(tables)


@pytest.mark.parametrize(("domain", "label"), [
    ("music.youtube.com", "YouTube"),
    ("m.twitch.tv", "Twitch"),
    ("www.netflix.com", "Netflix"),
    ("drive.google.com", "Google Drive"),
    ("web.whatsapp.com", "WhatsApp Web"),
    ("next.djk-coesfeld.de", "EintrachtNext"),
])
def test_website_aliases_group_subdomains(domain, label):
    assert website_label(domain) == label


@pytest.mark.parametrize(("exe", "label"), [
    ("FortniteClient-Win64-Shipping.exe", "Fortnite"),
    ("GTA5_Enhanced.exe", "Grand Theft Auto V"),
    ("BatmanAK.exe", "Batman: Arkham Knight"),
    ("ShooterGame.exe", "ARK: Survival Evolved"),
    ("ArkAscended.exe", "ARK: Survival Ascended"),
    ("EpicGamesLauncher.exe", "Epic Games"),
])
def test_app_aliases_group_game_and_launcher_executables(exe, label):
    assert app_label(exe, exe) == label


def test_ingest_requires_existing_write_key_and_validates_payload(activity_app):
    app, _ = activity_app
    client = app.test_client()
    event = _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z")
    assert client.post("/api/activity/ingest", json=_batch([event])).status_code == 401
    response = client.post("/api/activity/ingest", json=_batch([event]), headers={"Authorization": "Bearer activity-write-key"})
    assert response.status_code == 200
    assert response.get_json()["upserted"] == 1


def test_event_uuid_upsert_is_idempotent_and_accepts_shorter_corrections(activity_app):
    app, db_path = activity_app
    client = app.test_client()
    headers = {"Authorization": "Bearer activity-write-key"}
    device_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    payload = _batch([_event(event_id, "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z")])
    payload["device"]["id"] = device_id
    first = client.post("/api/activity/ingest", json=payload, headers=headers)
    second = client.post("/api/activity/ingest", json=payload, headers=headers)
    updated = _batch([_event(event_id, "2026-08-13T08:00:00Z", "2026-08-13T08:20:00Z")])
    updated["device"]["id"] = device_id
    third = client.post("/api/activity/ingest", json=updated, headers=headers)
    corrected = _batch([_event(event_id, "2026-08-13T08:00:00Z", "2026-08-13T08:02:00Z")])
    corrected["device"]["id"] = device_id
    fourth = client.post("/api/activity/ingest", json=corrected, headers=headers)
    assert first.status_code == second.status_code == third.status_code == fourth.status_code == 200
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        duration = conn.execute("SELECT duration_seconds FROM activity_events WHERE id=?", (event_id,)).fetchone()[0]
    assert count == 1
    assert duration == 120


def test_timeline_and_summary_clip_day_and_keep_domain_as_metadata(activity_app):
    app, _ = activity_app
    client = app.test_client()
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z"),
        _event(str(uuid.uuid4()), "2026-08-13T08:10:00Z", "2026-08-13T08:15:00Z", is_idle=True, app_exe=None, app_name=None, browser=None, domain=None, page_title=None),
        _event(str(uuid.uuid4()), "2026-08-13T08:15:00Z", "2026-08-13T08:25:00Z", app_exe="code.exe", app_name="Visual Studio Code", browser=None, domain=None, page_title=None),
    ]
    assert client.post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    timeline = client.get("/api/activity/timeline?date=2026-08-13").get_json()
    summary = client.get("/api/activity/summary?date=2026-08-13").get_json()
    assert [item["is_idle"] for item in timeline["events"]] == [False, True, False]
    assert summary["total_active_seconds"] == 1200
    assert summary["total_idle_seconds"] == 300
    assert summary["data_through"] == "2026-08-13T08:25:00Z"
    assert summary["current_foreground_active"] is True
    assert summary["current_foreground_ended_at"] == "2026-08-13T08:25:00Z"
    assert summary["usage_by_app"] == [
        {"name": "Chrome", "seconds": 600.0},
        {"name": "Visual Studio Code", "seconds": 600.0},
    ]
    assert summary["usage_by_domain"] == [{"name": "example.com", "seconds": 600.0}]
    assert summary["first_activity_at"] == "2026-08-13T08:00:00Z"
    assert summary["last_activity_at"] == "2026-08-13T08:25:00Z"
    assert summary["app_switches"] == 1
    assert summary["longest_focus_block"]["seconds"] == 600
    assert summary["idle_share"] == 0.2


def test_activity_day_rolls_over_at_four_am_berlin(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    crossing = _event(
        str(uuid.uuid4()),
        "2026-08-14T01:30:00Z",  # 03:30 Europe/Berlin
        "2026-08-14T02:30:00Z",  # 04:30 Europe/Berlin
        app_exe="code.exe", app_name="Visual Studio Code", browser=None, domain=None, page_title=None,
    )
    assert app.test_client().post("/api/activity/ingest", json=_batch([crossing]), headers=headers).status_code == 200
    previous_day = app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()
    next_day = app.test_client().get("/api/activity/summary?date=2026-08-14").get_json()
    assert previous_day["total_active_seconds"] == 1800
    assert next_day["total_active_seconds"] == 1800


def test_timeline_groups_consecutive_subpages_of_same_app_or_website(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:01:00Z", page_title="Inbox"),
        _event(str(uuid.uuid4()), "2026-08-13T08:01:00Z", "2026-08-13T08:03:00Z", page_title="Sent"),
        _event(str(uuid.uuid4()), "2026-08-13T08:03:00Z", "2026-08-13T08:04:00Z", app_exe="code.exe", app_name="Visual Studio Code", browser=None, domain=None, page_title=None),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    timeline = app.test_client().get("/api/activity/timeline?date=2026-08-13").get_json()["events"]
    assert len(timeline) == 2
    assert timeline[0]["duration_seconds"] == 180
    assert timeline[0]["event_count"] == 2


def test_summary_uses_human_app_labels(activity_app):
    app, _ = activity_app
    event = _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:01:00Z", app_exe="WindowsTerminal.exe", app_name="WindowsTerminal.exe", browser=None, domain=None, page_title=None)
    headers = {"Authorization": "Bearer activity-write-key"}
    assert app.test_client().post("/api/activity/ingest", json=_batch([event]), headers=headers).status_code == 200
    assert app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()["usage_by_app"] == [{"name": "Terminal", "seconds": 60.0}]


def test_visible_monitor_tracks_overlap_without_inflating_active_total(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z", app_exe="chatgpt.exe", app_name="ChatGPT.exe", browser=None, domain=None, page_title=None),
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z", app_exe="chatgpt.exe", app_name="ChatGPT.exe", browser=None, domain=None, page_title=None, source="windows_visible", track_kind="visible", monitor_id="monitor-1"),
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z", app_exe="opera.exe", app_name="opera.exe", browser="opera", domain="youtube.com", source="windows_visible", track_kind="visible", monitor_id="monitor-2"),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    summary = app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()
    assert summary["total_active_seconds"] == 600
    assert summary["usage_by_visible_app"] == [
        {"name": "ChatGPT", "seconds": 600.0},
        {"name": "Opera", "seconds": 600.0},
    ]
    assert summary["usage_by_visible_website"] == [{"name": "YouTube", "seconds": 600.0}]
    assert sum(item["seconds"] for item in summary["visible_seconds_by_monitor"]) == 1200
    assert summary["parallel_active_seconds"] == 600


def test_period_keeps_windows_and_iphone_separate_and_returns_empty_phone_days(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-12T08:00:00Z", "2026-08-12T08:10:00Z"),
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:20:00Z"),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    response = app.test_client().get("/api/activity/period?start_date=2026-08-12&end_date=2026-08-13")
    assert response.status_code == 200
    body = response.get_json()
    assert body["days"] == 2
    assert body["windows"]["average_active_seconds"] == 900
    assert body["windows"]["usage_by_app"] == [{"name": "Chrome", "seconds": 1800.0}]
    assert body["windows"]["daily"][0]["usage_by_app"] == [{"name": "Chrome", "seconds": 600.0}]
    heat = {(cell["weekday"], cell["hour"]): cell["seconds"] for cell in body["windows"]["heatmap"]}
    assert heat[(2, 10)] == 600
    assert heat[(3, 10)] == 1200
    assert body["iphone"]["completed_days"] == 0
    assert body["iphone"]["average_usage_seconds"] is None
    assert all(day["available"] is False for day in body["iphone"]["daily"])


def test_period_rejects_more_than_ninety_days(activity_app):
    app, _ = activity_app
    response = app.test_client().get("/api/activity/period?start_date=2026-01-01&end_date=2026-08-13")
    assert response.status_code == 400


def test_visible_usage_is_clipped_to_active_foreground_time(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:02:00Z", app_exe="chatgpt.exe", app_name="ChatGPT.exe", browser=None, domain=None, page_title=None),
        _event(str(uuid.uuid4()), "2026-08-13T08:02:00Z", "2026-08-13T08:10:00Z", is_idle=True, app_exe=None, app_name=None, browser=None, domain=None, page_title=None),
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z", app_exe="opera.exe", app_name="Opera", browser="opera", domain="youtube.com", source="windows_visible", track_kind="visible", monitor_id="monitor-1"),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    summary = app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()
    assert summary["total_active_seconds"] == 120
    assert summary["current_foreground_active"] is False
    assert summary["usage_by_visible_website"] == [{"name": "YouTube", "seconds": 120.0}]
    assert summary["visible_seconds_by_monitor"] == [{"name": "monitor-1", "seconds": 120.0}]


def test_windows_desktop_shell_is_not_reported_as_explorer_activity(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:02:00Z", app_exe="explorer.exe", app_name="explorer.exe", window_title="Program Manager", browser=None, domain=None, page_title=None),
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:02:00Z", app_exe="explorer.exe", app_name="explorer.exe", window_title="Program Manager", browser=None, domain=None, page_title=None, source="windows_visible", track_kind="visible", monitor_id="monitor-1"),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    summary = app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()
    assert summary["total_active_seconds"] == 120
    assert summary["usage_by_app"] == []
    assert summary["usage_by_visible_app"] == []
    assert summary["visible_seconds_by_monitor"] == []


def test_summary_counts_browser_as_app_and_keeps_website_as_same_segment_metadata(activity_app):
    app, _ = activity_app
    headers = {"Authorization": "Bearer activity-write-key"}
    events = [
        _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:10:00Z", app_exe="opera.exe", app_name="Opera", browser="opera", domain="www.youtube.com"),
        _event(str(uuid.uuid4()), "2026-08-13T08:10:00Z", "2026-08-13T08:20:00Z", app_exe="code.exe", app_name="Visual Studio Code", browser=None, domain=None, page_title=None),
    ]
    assert app.test_client().post("/api/activity/ingest", json=_batch(events), headers=headers).status_code == 200
    summary = app.test_client().get("/api/activity/summary?date=2026-08-13").get_json()
    assert summary["total_active_seconds"] == 1200
    assert summary["usage_by_app"] == [
        {"name": "Opera", "seconds": 600.0},
        {"name": "Visual Studio Code", "seconds": 600.0},
    ]
    assert summary["usage_by_website"] == [{"name": "YouTube", "seconds": 600.0}]


def test_invalid_timestamps_are_rejected(activity_app):
    app, _ = activity_app
    payload = _batch([_event(str(uuid.uuid4()), "2026-08-13T08:00:00", "2026-08-13T08:10:00Z")])
    response = app.test_client().post("/api/activity/ingest", json=payload, headers={"Authorization": "Bearer activity-write-key"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_payload"


def test_full_url_cannot_be_persisted_as_domain(activity_app):
    app, _ = activity_app
    event = _event(
        str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:01:00Z",
        domain="example.com/private?token=secret",
    )
    response = app.test_client().post(
        "/api/activity/ingest", json=_batch([event]),
        headers={"Authorization": "Bearer activity-write-key"},
    )
    assert response.status_code == 400


def test_dedicated_activity_key_passes_global_guards_without_ai_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_ACTIVITY_DB", str(tmp_path / "guarded.sqlite3"))
    monkeypatch.setenv("LIVA_ACTIVITY_INGEST_KEY", "dedicated-activity-key")
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(activity_bp)

    @app.before_request
    def guards():
        return enforce_private_access() or enforce_key_read_only() or enforce_write_protection()

    event = _event(str(uuid.uuid4()), "2026-08-13T08:00:00Z", "2026-08-13T08:01:00Z")
    client = app.test_client()
    bad = client.post(
        "/api/activity/ingest", json=_batch([event]),
        headers={"Authorization": "Bearer unrelated-ai-key"},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    good = client.post(
        "/api/activity/ingest", json=_batch([event]),
        headers={"Authorization": "Bearer dedicated-activity-key"},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert bad.status_code == 401
    assert good.status_code == 200


def test_dedicated_activity_key_allows_daily_iphone_import_through_global_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_ACTIVITY_DB", str(tmp_path / "guarded-iphone.sqlite3"))
    monkeypatch.setenv("LIVA_ACTIVITY_INGEST_KEY", "dedicated-activity-key")
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(activity_bp)

    @app.before_request
    def guards():
        return enforce_private_access() or enforce_key_read_only() or enforce_write_protection()

    day_start, day_end = usage_day_bounds("2026-08-16")
    payload = {
        "platform": "ios",
        "source": "stayfree_ios",
        "device": {"id": str(uuid.uuid4()), "name": "iPhone"},
        "usage_day": "2026-08-16",
        "day_start": day_start,
        "day_end": day_end,
        "total_usage_seconds": 60,
        "apps": [{"name": "Safari", "duration_seconds": 60}],
    }
    response = app.test_client().post(
        "/api/activity/iphone/daily",
        json=payload,
        headers={"Authorization": "Bearer dedicated-activity-key"},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert response.status_code == 200
