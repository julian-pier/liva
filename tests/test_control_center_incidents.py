from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from control_center import collector, store
from control_center import logs


def event(**overrides):
    value = {
        "idempotency_key": "source:one",
        "component_id": "endurance-intervals",
        "source": "fixture",
        "event_type": "terminal_sync_error",
        "severity": "warning",
        "occurred_at": "2026-09-23T10:00:00Z",
        "observed_at": "2026-09-23T10:00:00Z",
        "message": "missing execution pace",
        "error_class": "missing_execution_pace",
        "correlation_key": "endurance-intervals:terminal_sync_error:missing_execution_pace",
        "details": {"token": "secret-value", "affected": 12},
        "affected_object_count": 12,
    }
    value.update(overrides)
    return value


def test_event_store_is_idempotent_and_redacts_details(tmp_path):
    path = tmp_path / "control_center.sqlite3"
    first = store.record_event(event(), path=path)
    second = store.record_event(event(), path=path)

    assert first["inserted"] is True
    assert second["inserted"] is False
    incident = store.get_incident(first["incident_id"], path=path)
    assert incident["occurrence_count"] == 1
    assert "secret-value" not in json.dumps(incident)


def test_non_incident_eligible_operator_event_is_still_visible(tmp_path):
    path = tmp_path / "control_center.sqlite3"
    result = store.record_event(event(idempotency_key="job-action:one", incident_eligible=False), path=path)

    assert result["incident_id"] is None
    assert store.list_incidents(path=path) == []
    assert len(store.list_events(path=path)) == 1


def test_same_correlation_groups_many_occurrences_and_severity_escalates(tmp_path):
    path = tmp_path / "control_center.sqlite3"
    for number in range(100):
        store.record_event(event(idempotency_key=f"source:{number}", severity="critical" if number == 99 else "warning"), path=path)

    incidents = store.list_incidents(path=path)
    assert len(incidents) == 1
    assert incidents[0]["occurrence_count"] == 100
    assert incidents[0]["severity"] == "critical"


def test_error_classes_and_resolved_episode_are_separate(tmp_path):
    path = tmp_path / "control_center.sqlite3"
    first = store.record_event(event(), path=path)
    store.record_event(event(idempotency_key="source:other", error_class="network", correlation_key="endurance-intervals:terminal_sync_error:network"), path=path)
    store.record_event(event(idempotency_key="source:recovery", event_type="recovery", severity="info", is_recovery=True), path=path)
    reopened = store.record_event(event(idempotency_key="source:again", occurred_at="2026-09-27T10:00:00Z"), path=path)

    assert store.get_incident(first["incident_id"], path=path)["status"] == "resolved"
    assert reopened["incident_id"] != first["incident_id"]
    assert len(store.list_incidents(path=path)) == 3


def test_retention_keeps_open_incidents(tmp_path):
    path = tmp_path / "control_center.sqlite3"
    old = (datetime.now(timezone.utc) - timedelta(days=365)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    inserted = store.record_event(event(occurred_at=old), path=path)
    store.apply_retention(path=path)

    assert store.get_incident(inserted["incident_id"], path=path) is not None


def test_endurance_bootstrap_groups_active_objects_without_queue_mutation(tmp_path, monkeypatch):
    db = tmp_path / "plans.sqlite3"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE endurance_sessions(id TEXT, sync_state TEXT, scheduled_date TEXT); CREATE TABLE endurance_sync_jobs(id TEXT, session_id TEXT, status TEXT, last_error TEXT, updated_at TEXT);")
    for index in range(12):
        conn.execute("INSERT INTO endurance_sessions VALUES (?,?,?)", (f"s{index}", "sync_error", "2026-12-01"))
        for attempt in range(9 if index else 3): conn.execute("INSERT INTO endurance_sync_jobs VALUES (?,?,?,?,?)", (f"j{index}-{attempt}", f"s{index}", "failed", "missing_execution_pace: no pace", "2026-09-19T15:01:15Z"))
    conn.commit(); conn.close()
    before = db.read_bytes()
    monkeypatch.setattr(collector.connections, "PLANS_DB", str(db))
    store_db = tmp_path / "control_center.sqlite3"

    events = collector.collect_endurance(db_path=str(store_db))
    [store.record_event(item, path=store_db) for item in events]
    incidents = store.list_incidents(path=store_db)

    assert len(incidents) == 1
    assert incidents[0]["affected_object_count"] == 12
    assert incidents[0]["occurrence_count"] == 1
    assert events[0]["details"]["historical_rows"] == 102
    assert db.read_bytes() == before


def test_collector_is_idempotent_and_noneligible_backup_checks_create_no_events(tmp_path, monkeypatch):
    store_db = tmp_path / "control_center.sqlite3"
    monkeypatch.setattr(collector, "build_cockpit", lambda: {"checks": [
        {"check_id": "backup.recovery.coverage", "status": "degraded", "checked_at": "2026-09-23T10:00:00Z", "reason": "coverage"},
        {"check_id": "backup.restore.verification", "status": "unknown", "checked_at": "2026-09-23T10:00:00Z", "reason": "restore"},
        {"check_id": "backup.usb.target", "status": "degraded", "checked_at": "2026-09-23T10:00:00Z", "reason": "optional"},
    ]})
    monkeypatch.setattr(collector, "collect_endurance", lambda **_kwargs: [])
    monkeypatch.setattr(collector, "collect_systemd", lambda **_kwargs: [])

    assert collector.run_once(db_path=str(store_db))["events_inserted"] == 0
    assert collector.run_once(db_path=str(store_db))["events_inserted"] == 0
    assert store.list_incidents(path=store_db) == []


def test_systemd_failure_and_recovery(monkeypatch, tmp_path):
    class Result:
        def __init__(self, text): self.stdout, self.returncode = text, 0
    states = iter(["ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n", "ActiveState=active\nResult=success\nExecMainStatus=0\n"])
    monkeypatch.setattr(collector.subprocess, "run", lambda *_args, **_kwargs: Result(next(states)))
    monkeypatch.setattr(collector, "SYSTEMD_UNITS", {"liva.service": "liva"})
    path = tmp_path / "control_center.sqlite3"
    first = collector.collect_systemd(db_path=str(path)); [store.record_event(item, path=path) for item in first]
    second = collector.collect_systemd(db_path=str(path)); [store.record_event(item, path=path) for item in second]
    assert store.list_incidents(path=path)[0]["status"] == "resolved"


def test_logs_are_allowlisted_bounded_and_redacted(monkeypatch):
    seen = []
    class Result:
        returncode = 0
        stdout = "2026-09-23 token=top-secret normal line\n"
    monkeypatch.setattr(logs.subprocess, "run", lambda args, **_kwargs: seen.append(args) or Result())

    result = logs.read_logs(component="liva", limit=999, search="line")

    assert result["limit"] == 200
    assert "top-secret" not in json.dumps(result)
    assert seen[0][0] == "journalctl"
    assert "line" not in seen[0]
