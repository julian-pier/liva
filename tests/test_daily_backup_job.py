from pathlib import Path

import pytest

from jobs import daily_backup


_REAL_PUSH_CODE_SNAPSHOT = daily_backup.push_code_snapshot


@pytest.fixture(autouse=True)
def isolate_daily_backup_metadata(monkeypatch, tmp_path):
    """Every daily-backup test writes status only inside its pytest sandbox."""

    monkeypatch.setattr(daily_backup, "BACKUP_META_PATH", tmp_path / ".backup" / "last_backup.json")

    def blocked_side_effect(*_args, **_kwargs):
        raise AssertionError("daily-backup tests must explicitly mock external work")

    monkeypatch.setattr(daily_backup, "run_backup", blocked_side_effect)
    monkeypatch.setattr(daily_backup, "push_code_snapshot", blocked_side_effect)


def test_push_code_snapshot_skips_when_project_is_not_a_git_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_backup, "PROJECT_ROOT", Path(tmp_path))
    monkeypatch.setattr(daily_backup, "push_code_snapshot", _REAL_PUSH_CODE_SNAPSHOT)

    result = daily_backup.push_code_snapshot()

    assert result["status"] == "skipped"
    assert result["reason"] == "not_a_git_repo"


def test_source_snapshot_excludes_runtime_and_personal_data():
    excludes = set(daily_backup.CODE_SNAPSHOT_EXCLUDES)

    assert "data" in excludes
    assert "import" in excludes
    assert "logs" in excludes
    assert "var" in excludes
    assert "strava_sync/data" in excludes
    assert "smart_home/data" in excludes
    assert ":(glob)**/*.sqlite3" in excludes
    assert ":(glob)**/*.db" in excludes


def test_run_daily_backup_reports_failure_when_remote_sync_fails(monkeypatch):
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": True, "status": "ok"})
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {"ok": True, "targets": {}, "remote_sync": {"ok": False, "status": "error"}},
    )

    result = daily_backup.run_daily_backup()

    assert result["ok"] is False
    assert result["sqlite_backup"]["remote_sync"]["status"] == "error"


def test_run_daily_backup_treats_code_push_as_best_effort_by_default(monkeypatch):
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")
    monkeypatch.delenv("LIVA_CODE_PUSH_REQUIRED", raising=False)
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": False, "status": "error", "step": "git_push"})
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {"ok": True, "targets": {}, "remote_sync": {"ok": True, "status": "ok"}},
    )

    result = daily_backup.run_daily_backup()

    assert result["ok"] is True
    assert result["code_push"]["status"] == "error"


def test_run_daily_backup_can_require_code_push(monkeypatch):
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("LIVA_CODE_PUSH_REQUIRED", "1")
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": False, "status": "error", "step": "git_push"})
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {"ok": True, "targets": {}, "remote_sync": {"ok": True, "status": "ok"}},
    )

    result = daily_backup.run_daily_backup()

    assert result["ok"] is False


def test_run_daily_backup_treats_usb_target_errors_as_best_effort_by_default(monkeypatch):
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")
    monkeypatch.delenv("LIVA_USB_BACKUP_REQUIRED", raising=False)
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": True, "status": "ok"})
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {
            "ok": False,
            "targets": {},
            "target_errors": {"/mnt/liva-usb/65CA-09BE/backups/sqlite": {"_target": "error: read-only"}},
            "remote_sync": {"ok": True, "status": "ok"},
        },
    )

    result = daily_backup.run_daily_backup()

    assert result["ok"] is True


def test_run_daily_backup_can_require_usb_target_success(monkeypatch):
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")
    monkeypatch.setenv("LIVA_USB_BACKUP_REQUIRED", "1")
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": True, "status": "ok"})
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {
            "ok": False,
            "targets": {},
            "target_errors": {"/mnt/liva-usb/65CA-09BE/backups/sqlite": {"_target": "error: read-only"}},
            "remote_sync": {"ok": True, "status": "ok"},
        },
    )

    result = daily_backup.run_daily_backup()

    assert result["ok"] is False


def test_exit_code_reflects_backup_result():
    assert daily_backup.exit_code_for_result({"ok": True}) == 0
    assert daily_backup.exit_code_for_result({"ok": False}) == 1


def test_run_daily_backup_writes_only_isolated_metadata(monkeypatch):
    production_path = daily_backup.PROJECT_ROOT / ".backup" / "last_backup.json"
    production_bytes = production_path.read_bytes() if production_path.exists() else None
    writes: list[Path] = []
    original_write = daily_backup._write_backup_metadata

    def safe_write(payload):
        writes.append(daily_backup.BACKUP_META_PATH)
        if daily_backup.BACKUP_META_PATH != production_path:
            original_write(payload)

    monkeypatch.setattr(daily_backup, "_write_backup_metadata", safe_write)
    monkeypatch.setattr(daily_backup, "run_backup", lambda: {"ok": True, "targets": {}, "remote_sync": {"ok": True, "status": "ok"}})
    monkeypatch.setattr(daily_backup, "push_code_snapshot", lambda *args, **kwargs: {"ok": True, "status": "ok"})
    monkeypatch.setenv("LIVA_CODE_SNAPSHOT_ENABLED", "1")

    daily_backup.run_daily_backup()

    assert writes == [daily_backup.BACKUP_META_PATH]
    assert writes[0] != production_path
    assert daily_backup.BACKUP_META_PATH.exists()
    assert (production_path.read_bytes() if production_path.exists() else None) == production_bytes


def test_write_backup_metadata_creates_tracked_snapshot_file(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_backup, "BACKUP_META_PATH", tmp_path / ".backup" / "last_backup.json")

    daily_backup._write_backup_metadata({"sqlite_backup": {"ok": True}})

    content = daily_backup.BACKUP_META_PATH.read_text(encoding="utf-8")
    assert '"updated_at":' in content
    assert '"sqlite_backup"' in content


def test_run_daily_backup_does_not_push_code_unless_explicitly_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVA_CODE_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.setattr(daily_backup, "BACKUP_META_PATH", tmp_path / "last_backup.json")
    monkeypatch.setattr(
        daily_backup,
        "run_backup",
        lambda: {"ok": True, "targets": {}, "remote_sync": {"ok": True, "status": "ok"}},
    )

    def unexpected_push(*args, **kwargs):
        raise AssertionError("unattended backup must not push code by default")

    monkeypatch.setattr(daily_backup, "push_code_snapshot", unexpected_push)

    result = daily_backup.run_daily_backup()

    assert result["ok"] is True
    assert result["code_push"] == {"ok": True, "status": "skipped", "reason": "disabled"}
    assert daily_backup.BACKUP_META_PATH.exists()
