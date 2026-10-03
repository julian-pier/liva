from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from control_center.adapters import backup_snapshot, endurance_snapshot
from control_center.models import OperationalCheck, aggregate_status
from control_center.redaction import redact


def _create_queue(path, rows, sessions=()):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE endurance_sync_jobs (
        id TEXT PRIMARY KEY, provider TEXT, session_id TEXT, external_id TEXT,
        operation TEXT, status TEXT, attempt_count INTEGER, next_attempt_at TEXT,
        last_error TEXT, created_at TEXT, updated_at TEXT
        )"""
    )
    conn.execute("CREATE TABLE endurance_sessions (id TEXT PRIMARY KEY, sync_state TEXT, scheduled_date TEXT)")
    for index, row in enumerate(rows):
        if isinstance(row, tuple):
            status, updated_at = row
            row = {"status": status, "updated_at": updated_at}
        conn.execute(
            "INSERT INTO endurance_sync_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("id", f"job-{index}"),
                "intervals",
                row.get("session_id", f"session-{index}"),
                row.get("external_id", f"liva:endurance:session-{index}"),
                row.get("operation", "UPSERT"),
                row["status"],
                row.get("attempt_count", 1),
                row.get("next_attempt_at"),
                row.get("last_error"),
                row.get("created_at", row["updated_at"]),
                row["updated_at"],
            ),
        )
    conn.executemany("INSERT INTO endurance_sessions VALUES (?,?,?)", sessions)
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["healthy"], "healthy"),
        (["healthy", "degraded"], "degraded"),
        (["healthy", "failing"], "failing"),
        (["healthy", "unknown"], "unknown"),
        (["inactive"], "inactive"),
    ],
)
def test_operational_status_aggregation(statuses, expected):
    checks = [OperationalCheck(f"check.{index}", "component", "Check", status, "2026-09-23T00:00:00Z", "fixture") for index, status in enumerate(statuses)]
    assert aggregate_status(checks) == expected


def test_endurance_adapter_reports_healthy_empty_queue_without_mutation(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    _create_queue(db_path, [])

    before = db_path.read_bytes()
    checks, job = endurance_snapshot(db_path=str(db_path))

    assert checks[0].status == "healthy"
    assert job.pending_count == job.retry_count == job.failed_count == 0
    assert job.capabilities["retry_backoff"] is True
    assert job.capabilities["dedupe"] is True
    assert job.actions == ()
    assert db_path.read_bytes() == before


def test_endurance_adapter_keeps_old_failed_history_visible_without_calling_it_current_failure(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    rows = [
        {"status": "failed", "updated_at": "2026-09-14T10:00:00Z", "external_id": "object-1", "session_id": "session-1", "last_error": "missing_execution_pace: old"},
        {"status": "done", "updated_at": "2026-09-19T10:00:00Z", "external_id": "object-1", "session_id": "session-1"},
    ]
    _create_queue(db_path, rows, [("session-1", "synced", "2026-12-01")])

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "healthy"
    assert job.pending_count == 0
    assert job.retry_count == 0
    assert job.failed_count == 1
    assert job.details["historical_failed_rows"] == 1
    assert job.details["latest_failure_at"] == "2026-09-14T10:00:00Z"


def test_endurance_adapter_marks_current_repeated_failure_failing(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    _create_queue(
        db_path,
        [{"status": "failed", "updated_at": "2026-09-23T09:50:00Z", "attempt_count": 3, "last_error": "missing_execution_pace: current", "session_id": "session-1", "external_id": "object-1"}],
        [("session-1", "sync_error", "2026-12-01")],
    )

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "failing"
    assert job.failed_count == 1
    assert job.details["active_sync_error_sessions"] == 1


def test_endurance_adapter_does_not_fail_after_latest_failed_object_recovers(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    rows = [
        {"status": "failed", "updated_at": "2026-09-23T09:45:00Z", "attempt_count": 3, "last_error": "missing_execution_pace: current", "session_id": "session-1", "external_id": "object-1"},
        {"status": "done", "updated_at": "2026-09-23T09:50:00Z", "session_id": "session-1", "external_id": "object-1"},
    ]
    _create_queue(db_path, rows, [("session-1", "synced", "2026-12-01")])

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "healthy"
    assert job.details["latest_failure_recovered"] is True


def test_endurance_adapter_treats_fresh_pending_work_as_normal(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    _create_queue(db_path, [{"status": "pending", "created_at": "2026-09-23T09:55:00Z", "updated_at": "2026-09-23T09:55:00Z"}])

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "healthy"
    assert job.pending_count == 1


def test_endurance_adapter_marks_unusually_old_pending_queue_failing(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    _create_queue(db_path, [{"status": "pending", "created_at": "2026-09-23T06:00:00Z", "updated_at": "2026-09-23T06:00:00Z"}])

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "failing"
    assert job.details["queue_lag_seconds"] == 4 * 60 * 60


def test_endurance_adapter_marks_old_active_sync_errors_degraded(tmp_path):
    db_path = tmp_path / "plans.sqlite3"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    _create_queue(
        db_path,
        [{"status": "failed", "updated_at": "2026-09-14T10:00:00Z", "attempt_count": 8, "last_error": "missing_execution_pace: old", "session_id": "session-1", "external_id": "object-1"}],
        [("session-1", "sync_error", "2026-12-01")],
    )

    checks, job = endurance_snapshot(db_path=str(db_path), now=now)

    assert checks[0].status == "degraded"
    assert job.details["historical_failed_rows"] == 1
    assert job.details["active_sync_error_sessions"] == 1


def test_endurance_adapter_treats_missing_or_unreadable_queue_as_unknown_and_redacts(tmp_path):
    checks, job = endurance_snapshot(db_path=str(tmp_path / "missing-token=top-secret.sqlite3"))

    assert checks[0].status == "unknown"
    assert job.status == "unknown"
    assert "top-secret" not in json.dumps(checks[0].to_dict())


def _backup_metadata(
    *,
    updated_at: str,
    ok: bool,
    sources: dict | None = None,
    targets: dict | None = None,
    target_errors: dict | None = None,
    remote_ok: bool = True,
    legacy_tar: bool = False,
):
    return {
        "updated_at": updated_at,
        "payload": {
            "sqlite_backup": {
                "ok": ok,
                "sources": sources or {"training.sqlite3": "/safe/path/training.sqlite3"},
                "targets": targets if targets is not None else {"/safe/dropbox": {"training.sqlite3": "ok"}},
                "target_errors": target_errors or {},
                "remote_sync": {"ok": remote_ok, "status": "ok" if remote_ok else "error"},
            },
            "legacy_tar": {"ok": legacy_tar},
        },
    }


def _backup_checks(path, *, now, runtime_state=None):
    checks, job = backup_snapshot(metadata_path=path, now=now, runtime_state=runtime_state)
    return {check.check_id: check for check in checks}, job


def test_backup_adapter_separates_successful_execution_from_degraded_recovery_coverage(tmp_path):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    stamp = "2026-09-23T03:05:00Z"
    path.write_text(json.dumps(_backup_metadata(updated_at=stamp, ok=True, legacy_tar=True)), encoding="utf-8")

    checks, job = _backup_checks(path, now=now)

    assert checks["backup.daily.execution"].status == "healthy"
    assert checks["backup.freshness"].status == "healthy"
    assert checks["backup.recovery.coverage"].status == "degraded"
    assert checks["backup.restore.verification"].status == "unknown"
    assert checks["backup.recovery.coverage"].details["legacy_tar_counts_as_full_recovery"] is False
    assert "memory_vault" in checks["backup.recovery.coverage"].details["missing_categories"]
    assert "mcp_oauth_config" in checks["backup.recovery.coverage"].details["missing_categories"]
    assert job.status == "degraded"
    assert job.capabilities["sqlite_online_snapshot"] is True


def test_backup_adapter_marks_failed_expected_modern_run_failing(tmp_path):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    path.write_text(json.dumps(_backup_metadata(updated_at="2026-09-23T03:05:00Z", ok=False, remote_ok=False)), encoding="utf-8")

    checks, job = _backup_checks(
        path,
        now=now,
        runtime_state={"available": True, "timer_active": True, "last_run_at": "2026-09-23T03:02:00Z", "last_result": "failed", "last_exit_status": 1},
    )

    assert checks["backup.daily.execution"].status == "failing"
    assert job.status == "failing"


def test_backup_adapter_prefers_successful_scheduled_run_over_later_inconsistent_metadata(tmp_path, monkeypatch):
    path = tmp_path / "last_backup.json"
    usb_path = "/mnt/usb/backups/sqlite"
    payload = _backup_metadata(
        updated_at="2026-09-23T06:47:12Z",
        ok=False,
        targets={},
        target_errors={usb_path: {"_target": "error: synthetic test data"}},
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.delenv("LIVA_USB_BACKUP_REQUIRED", raising=False)
    monkeypatch.setattr("control_center.adapters._mounted_paths", lambda: {"/"})
    runtime = {
        "available": True,
        "timer_active": True,
        "last_run_at": "2026-09-23T01:02:42Z",
        "last_completed_at": "2026-09-23T01:05:08Z",
        "last_result": "success",
        "last_exit_status": 0,
        "last_payload": {
            "ok": True,
            "sqlite_backup": {
                "ok": True,
                "sources": {"training.sqlite3": "/safe/training.sqlite3"},
                "targets": {"/safe/dropbox": {"training.sqlite3": "ok"}, usb_path: {"training.sqlite3": "ok"}},
                "target_errors": {},
                "remote_sync": {"ok": True, "status": "ok"},
            },
        },
    }

    checks, job = _backup_checks(path, now=datetime(2026, 9, 23, 10, tzinfo=timezone.utc), runtime_state=runtime)

    assert checks["backup.daily.execution"].status == "healthy"
    assert checks["backup.daily.execution"].details["metadata_after_last_scheduled_run"] is True
    assert checks["backup.freshness"].status == "healthy"
    assert checks["backup.usb.target"].status == "degraded"
    assert job.status == "degraded"


def test_backup_adapter_exposes_optional_unmounted_usb_without_failing(tmp_path, monkeypatch):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    stamp = "2026-09-23T03:05:00Z"
    payload = _backup_metadata(updated_at=stamp, ok=True)
    payload["payload"]["sqlite_backup"]["targets"]["/mnt/usb/backups/sqlite"] = {"training.sqlite3": "ok"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.delenv("LIVA_USB_BACKUP_REQUIRED", raising=False)
    monkeypatch.setattr("control_center.adapters._mounted_paths", lambda: set())

    checks, job = _backup_checks(path, now=now)

    assert checks["backup.daily.execution"].status == "healthy"
    assert checks["backup.usb.target"].status == "degraded"
    assert checks["backup.usb.target"].details["required"] is False
    assert job.status == "degraded"


def test_backup_adapter_marks_required_usb_failed_expected_run_failing(tmp_path, monkeypatch):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    usb_path = "/mnt/usb/backups/sqlite"
    payload = _backup_metadata(updated_at="2026-09-23T03:05:00Z", ok=False, targets={}, target_errors={usb_path: {"_target": "error: not mounted"}})
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("LIVA_USB_BACKUP_DIRS", usb_path)
    monkeypatch.setenv("LIVA_USB_BACKUP_REQUIRED", "1")
    monkeypatch.setattr("control_center.adapters._mounted_paths", lambda: set())

    checks, job = _backup_checks(path, now=now)

    assert checks["backup.daily.execution"].status == "failing"
    assert checks["backup.usb.target"].status == "failing"
    assert job.status == "failing"


def test_backup_adapter_marks_stale_backup_freshness_failing(tmp_path):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    path.write_text(json.dumps(_backup_metadata(updated_at="2026-09-20T03:05:00Z", ok=True)), encoding="utf-8")

    checks, job = _backup_checks(path, now=now)

    assert checks["backup.daily.execution"].status == "healthy"
    assert checks["backup.freshness"].status == "failing"
    assert job.status == "failing"


def test_backup_adapter_does_not_let_fresh_legacy_tar_mask_modern_failure(tmp_path):
    path = tmp_path / "last_backup.json"
    now = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
    path.write_text(json.dumps(_backup_metadata(updated_at="2026-09-23T04:25:00Z", ok=False, legacy_tar=True)), encoding="utf-8")

    checks, job = _backup_checks(path, now=now)

    assert checks["backup.daily.execution"].status == "failing"
    assert checks["backup.recovery.coverage"].details["legacy_tar_counts_as_full_recovery"] is False
    assert job.status == "failing"


def test_backup_adapter_marks_missing_metadata_unknown_without_mutation(tmp_path):
    path = tmp_path / "no-metadata.json"
    checks, job = backup_snapshot(metadata_path=path)

    by_id = {check.check_id: check for check in checks}
    assert by_id["backup.daily.execution"].status == "unknown"
    assert by_id["backup.freshness"].status == "unknown"
    assert by_id["backup.restore.verification"].status == "unknown"
    assert job.status == "unknown"
    assert not path.exists()


def test_backup_adapter_redacts_secrets_and_preserves_metadata_bytes(tmp_path):
    path = tmp_path / "last_backup.json"
    payload = _backup_metadata(
        updated_at="2026-09-23T03:05:00Z",
        ok=False,
        target_errors={"/safe/dropbox": {"training.sqlite3": "error: token=top-secret"}},
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    checks, job = backup_snapshot(metadata_path=path, now=datetime(2026, 9, 23, 10, tzinfo=timezone.utc))

    serialized = json.dumps([check.to_dict() for check in checks] + [job.to_dict()])
    assert "top-secret" not in serialized
    assert path.read_bytes() == before


def test_redaction_removes_secret_values_from_nested_error_payloads():
    value = redact({"api_token": "secret-value", "error": "Authorization: Bearer abcdef token=still-secret", "nested": ["password=hunter2"]})

    serialized = json.dumps(value)
    assert "secret-value" not in serialized
    assert "abcdef" not in serialized
    assert "hunter2" not in serialized
    assert "[REDACTED]" in serialized


def test_control_center_api_requires_authenticated_key_or_master_and_returns_readonly_payload(monkeypatch):
    from app import app
    import control_center.api as api

    monkeypatch.setattr(api, "build_cockpit", lambda: {"ok": True, "generated_at": "2026-09-23T10:00:00Z", "status": "healthy", "checks": [], "jobs": [], "actions": []})
    monkeypatch.setenv("LIVA_AUTO_WHITELIST_TAILSCALE", "0")
    monkeypatch.setenv("LIVA_IP_WHITELIST", "203.0.113.1")
    app.config.update(TESTING=True, SECRET_KEY="test-control-center-secret")
    client = app.test_client()

    denied = client.get("/api/control-center/summary", headers={"Accept": "application/json"})
    assert denied.status_code == 401

    with client.session_transaction() as session:
        session["role"] = "master"
    allowed = client.get("/api/control-center/summary", headers={"Accept": "application/json"})
    assert allowed.status_code == 200
    assert allowed.get_json()["actions"] == []


def test_control_center_incident_apis_are_protected_and_bounded(monkeypatch):
    from app import app
    import control_center.api as api

    monkeypatch.setattr(api, "list_incidents", lambda **kwargs: [{"incident_id": "incident-1", "limit": kwargs["limit"]}])
    monkeypatch.setattr(api, "list_events", lambda **kwargs: [{"event_id": "event-1", "limit": kwargs["limit"]}])
    monkeypatch.setattr(api, "get_incident", lambda incident_id: {"incident_id": incident_id, "events": []} if incident_id == "incident-1" else None)
    monkeypatch.setattr(api, "read_logs", lambda **kwargs: {"ok": True, "logs": [], "limit": kwargs["limit"]})
    monkeypatch.setenv("LIVA_AUTO_WHITELIST_TAILSCALE", "0")
    monkeypatch.setenv("LIVA_IP_WHITELIST", "203.0.113.1")
    app.config.update(TESTING=True, SECRET_KEY="test-control-center-secret")
    client = app.test_client()

    assert client.get("/api/control-center/incidents").status_code == 401
    with client.session_transaction() as session:
        session["role"] = "master"
    assert client.get("/api/control-center/incidents?limit=999").get_json()["incidents"][0]["limit"] == 200
    assert client.get("/api/control-center/events?limit=999").get_json()["events"][0]["limit"] == 200
    assert client.get("/api/control-center/incidents/incident-1").status_code == 200
    assert client.get("/api/control-center/incidents/missing").status_code == 404
