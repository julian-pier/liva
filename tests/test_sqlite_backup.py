import sqlite3
from pathlib import Path

import pytest

from database import sqlite_backup as sb


@pytest.fixture(autouse=True)
def isolate_backup_runtime(monkeypatch, tmp_path):
    """SQLite backup tests use only temporary sources, targets, and transports."""

    calls: list[tuple[Path, str]] = []

    def fake_sync(local_dir, remote):
        calls.append((local_dir, remote))
        return {"ok": True, "status": "isolated"}

    monkeypatch.setattr(sb, "_sync_dir_to_rclone_remote", fake_sync)
    monkeypatch.setattr(sb, "DEFAULT_DROPBOX_BACKUP_DIR", tmp_path / "dropbox")
    monkeypatch.setattr(sb, "DEFAULT_RCLONE_REMOTE", "test:disabled")
    monkeypatch.setattr(sb, "DB_DIR", tmp_path / "database")
    monkeypatch.setattr(sb, "DEFAULT_MCP_WRITE_STATE", tmp_path / "liva_mcp_writes.sqlite3")
    monkeypatch.setattr(sb.os, "geteuid", lambda: 1000)
    monkeypatch.delenv("LIVA_USB_BACKUP_DIRS", raising=False)
    monkeypatch.delenv("LIVA_USB_MOUNT_ROOTS", raising=False)
    return calls


def _write_sample_db(path, values):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sample (value INTEGER NOT NULL)")
        conn.execute("DELETE FROM sample")
        conn.executemany("INSERT INTO sample (value) VALUES (?)", [(value,) for value in values])
        conn.commit()
    finally:
        conn.close()


def test_list_database_sources_includes_canonical_and_extra_sqlite_files(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    for name in ("training.sqlite3", "auth.sqlite3", "custom.db"):
        _write_sample_db(db_dir / name, [1])

    sources = sb.list_database_sources(db_dir)

    assert set(sources) == {"training.sqlite3", "auth.sqlite3"}


def test_canonical_sources_include_polar_database(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    _write_sample_db(db_dir / "polar.sqlite3", [1])

    sources = sb.list_database_sources(db_dir)

    assert sources == {"polar.sqlite3": db_dir / "polar.sqlite3"}


def test_canonical_sources_include_control_center_database(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    _write_sample_db(db_dir / "control_center.sqlite3", [1])

    assert sb.list_database_sources(db_dir) == {"control_center.sqlite3": db_dir / "control_center.sqlite3"}


def test_default_sources_include_external_mcp_write_state_when_present(monkeypatch, tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    mcp_state = tmp_path / "liva_mcp_writes.sqlite3"
    _write_sample_db(mcp_state, [1])
    monkeypatch.setattr(sb, "DB_DIR", db_dir)
    monkeypatch.setattr(sb, "DEFAULT_MCP_WRITE_STATE", mcp_state)

    sources = sb.list_database_sources()

    assert sources == {"liva_mcp_writes.sqlite3": mcp_state}


def test_run_backup_overwrites_same_target_file_instead_of_creating_new_one(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    src = db_dir / "training.sqlite3"
    target = tmp_path / "backup-target"
    _write_sample_db(src, [1])

    sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[target])
    _write_sample_db(src, [1, 2, 3])
    sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[target])

    files = list(target.glob("training.sqlite3*"))
    assert [path.name for path in files] == ["training.sqlite3"]

    conn = sqlite3.connect(target / "training.sqlite3")
    try:
        rows = conn.execute("SELECT value FROM sample ORDER BY value").fetchall()
    finally:
        conn.close()

    assert [row[0] for row in rows] == [1, 2, 3]


def test_run_backup_uses_isolated_remote_sync(tmp_path, isolate_backup_runtime):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    source = db_dir / "training.sqlite3"
    _write_sample_db(source, [1])

    result = sb.run_backup(source_files={"training.sqlite3": source}, target_dirs=[tmp_path / "backup-target"])

    assert result["remote_sync"] == {"ok": True, "status": "isolated"}
    assert len(isolate_backup_runtime) == 1


def test_run_backup_prunes_stale_duplicate_files_from_target(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    src = db_dir / "training.sqlite3"
    target = tmp_path / "backup-target"
    target.mkdir()
    _write_sample_db(src, [1])
    _write_sample_db(target / "training.db", [999])

    sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[target])

    assert not (target / "training.db").exists()
    assert (target / "training.sqlite3").exists()


def test_run_backup_reports_target_error_in_ok_flag(tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    src = db_dir / "training.sqlite3"
    _write_sample_db(src, [1])
    broken_target = tmp_path / "not-a-dir"
    broken_target.write_text("occupied", encoding="utf-8")

    result = sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[broken_target])

    assert result["ok"] is False
    assert str(broken_target) in result["target_errors"]
    assert result["targets"][str(broken_target)]["_target"].startswith("error:")


def test_run_backup_does_not_write_to_unmounted_usb_path(monkeypatch, tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    src = db_dir / "training.sqlite3"
    _write_sample_db(src, [1])
    usb_target = Path("/mnt/liva-usb/65CA-09BE/backups/sqlite")
    monkeypatch.setattr(sb, "_mount_info_for_path", lambda path: None)

    result = sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[usb_target])

    assert result["ok"] is False
    assert result["targets"][str(usb_target)]["_target"] == "error: USB target is not mounted"


def test_run_backup_reports_read_only_usb_path(monkeypatch, tmp_path):
    db_dir = tmp_path / "database"
    db_dir.mkdir()
    src = db_dir / "training.sqlite3"
    _write_sample_db(src, [1])
    usb_target = Path("/mnt/liva-usb/65CA-09BE/backups/sqlite")
    monkeypatch.setattr(sb, "_mount_info_for_path", lambda path: (Path("/mnt/liva-usb/65CA-09BE"), "ro,relatime"))

    result = sb.run_backup(source_files={"training.sqlite3": src}, target_dirs=[usb_target])

    assert result["ok"] is False
    assert result["targets"][str(usb_target)]["_target"] == "error: USB target is mounted read-only"


def test_list_backup_targets_automounts_unmounted_usb_partition_for_root(monkeypatch, tmp_path):
    automount_root = tmp_path / "automount"
    mounted_dirs: list[Path] = []

    monkeypatch.delenv("LIVA_USB_BACKUP_DIRS", raising=False)
    monkeypatch.delenv("LIVA_USB_MOUNT_ROOTS", raising=False)
    monkeypatch.setattr(sb, "DEFAULT_USB_AUTOMOUNT_ROOT", automount_root)
    monkeypatch.setattr(sb, "_mounted_usb_roots", lambda: [])
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        sb,
        "_lsblk_devices",
        lambda: [
            {
                "name": "sda",
                "path": "/dev/sda",
                "tran": "usb",
                "rm": True,
                "fstype": "",
                "label": "",
                "uuid": "",
                "mountpoints": [None],
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "tran": "",
                        "rm": True,
                        "fstype": "vfat",
                        "label": "65CA-09BE",
                        "uuid": "65CA-09BE",
                        "mountpoints": [None],
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        sb,
        "_automount_usb_partition",
        lambda device_path, label="", uuid="": mounted_dirs.append(automount_root / "65CA-09BE") or (automount_root / "65CA-09BE"),
    )

    targets = sb.list_backup_targets()

    assert automount_root / "65CA-09BE" in mounted_dirs
    assert automount_root / "65CA-09BE" / "backups" / "sqlite" in targets


def test_list_backup_targets_does_not_try_automount_without_root(monkeypatch):
    monkeypatch.setattr(sb, "_mounted_usb_roots", lambda: [])
    monkeypatch.setattr(sb.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        sb,
        "_lsblk_devices",
        lambda: [
            {
                "name": "sda",
                "path": "/dev/sda",
                "tran": "usb",
                "rm": True,
                "fstype": "",
                "label": "",
                "uuid": "",
                "mountpoints": [None],
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "tran": "",
                        "rm": True,
                        "fstype": "vfat",
                        "label": "65CA-09BE",
                        "uuid": "65CA-09BE",
                        "mountpoints": [None],
                    }
                ],
            }
        ],
    )

    called = {"count": 0}

    def _fake_automount(device_path, label="", uuid=""):
        called["count"] += 1
        return Path("/mnt/should-not-happen")

    monkeypatch.setattr(sb, "_automount_usb_partition", _fake_automount)

    targets = sb.list_backup_targets()

    assert called["count"] == 0
    assert all("/mnt/should-not-happen" not in str(path) for path in targets)
