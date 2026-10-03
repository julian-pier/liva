from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import subprocess

import pytest


def test_preflight_reports_not_initialized_without_recipient(tmp_path: Path) -> None:
    from recovery.full_recovery import preflight
    from recovery.policy import RecoveryPolicy

    config = tmp_path / "config.toml"
    config.write_text(
        f'''\
[recovery]
staging_root = "{tmp_path / 'staging'}"
[usb]
uuid = "TEST-UUID"
label = "LIVA_RECOVERY"
filesystem = "vfat"
min_capacity_bytes = 1
[crypto]
recipients_file = "{tmp_path / 'missing-recipients.txt'}"
''',
        encoding="utf-8",
    )

    result = preflight(config_path=config, policy=RecoveryPolicy.empty())

    assert result["ok"] is False
    assert result["error_code"] == "NOT_INITIALIZED"
    assert result["mode"] == "read_only"


def test_encrypted_roundtrip_keeps_hashes_modes_sqlite_and_manual_sources(tmp_path: Path) -> None:
    from recovery.full_recovery import create_backup
    from recovery.policy import RecoveryPolicy, RecoverySource
    from recovery.restore import apply_restore, plan_restore

    key = tmp_path / "test-identity.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True, text=True)
    recipient = tmp_path / "recipients.txt"
    recipient.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f'''\
[recovery]
staging_root = "{tmp_path / 'staging'}"
[usb]
uuid = "TEST-UUID"
label = "LIVA_RECOVERY"
filesystem = "vfat"
min_capacity_bytes = 1
[crypto]
recipients_file = "{recipient}"
''', encoding="utf-8")
    automatic = tmp_path / "automatic.txt"; automatic.write_text("ordinary\n", encoding="utf-8"); os.chmod(automatic, 0o640)
    secret = tmp_path / "secret.txt"; secret.write_text("secret-value-never-in-receipt", encoding="utf-8"); os.chmod(secret, 0o600)
    manual = tmp_path / "tailscale.state"; manual.write_text("host-identity", encoding="utf-8")
    database = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(database); conn.execute("PRAGMA journal_mode=WAL"); conn.execute("CREATE TABLE items(value TEXT)"); conn.execute("INSERT INTO items VALUES ('persisted')"); conn.commit(); conn.close()
    policy = RecoveryPolicy(
        sources=(RecoverySource("ordinary", automatic, "file"), RecoverySource("secrets_credentials", secret, "file", restore_policy="bootstrap"), RecoverySource("tailscale_identity", manual, "file", restore_policy="manual"), RecoverySource("liva_sqlite", database, "sqlite")),
        sqlite_scan_roots=(tmp_path,), known_sqlite=frozenset({database.resolve()}),
    )
    result = create_backup(config_path=config, target_dir=tmp_path / "usb", policy=policy, project_root=tmp_path)
    receipt_text = Path(result["bundle"] + ".receipt.json").read_text(encoding="utf-8")
    assert "secret-value" not in receipt_text
    plan = plan_restore(bundle=Path(result["bundle"]), identity=key, staging=tmp_path / "restore-staging")
    restored = apply_restore(plan, target_root=tmp_path / "target")
    assert not any("tailscale.state" in value for value in restored)
    restored_auto = tmp_path / "target" / str(automatic).lstrip("/")
    assert restored_auto.read_text(encoding="utf-8") == "ordinary\n"
    assert stat_mode(restored_auto) == 0o640
    restored_db = tmp_path / "target" / str(database).lstrip("/")
    assert sqlite3.connect(restored_db).execute("SELECT value FROM items").fetchone()[0] == "persisted"
    apply_restore(plan, target_root=tmp_path / "target-manual", include_manual={"tailscale_identity"})
    assert (tmp_path / "target-manual" / str(manual).lstrip("/")).read_text(encoding="utf-8") == "host-identity"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_restore_rejects_wrong_identity_corrupt_ciphertext_and_sidecar(tmp_path: Path) -> None:
    from recovery.full_recovery import create_backup
    from recovery.policy import RecoveryPolicy, RecoverySource
    from recovery.restore import RestoreError, plan_restore

    key = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    config = recovery_config(tmp_path, recipients)
    source = tmp_path / "source.txt"; source.write_text("content")
    result = create_backup(config_path=config, target_dir=tmp_path / "usb", policy=RecoveryPolicy((RecoverySource("ordinary", source, "file"),)), project_root=tmp_path)
    wrong = tmp_path / "wrong.txt"; subprocess.run(["age-keygen", "-o", str(wrong)], check=True, capture_output=True)
    with pytest.raises(RestoreError):
        plan_restore(bundle=Path(result["bundle"]), identity=wrong, staging=tmp_path / "wrong-stage")
    sidecar = Path(result["bundle"] + ".sha256")
    sidecar.write_text("0" * 64 + "  bundle\n")
    with pytest.raises(RestoreError, match="sidecar"):
        plan_restore(bundle=Path(result["bundle"]), identity=key, staging=tmp_path / "sidecar-stage")
    sidecar.unlink()
    bundle = Path(result["bundle"])
    bundle.write_bytes(bundle.read_bytes()[:-8] + b"corrupted")
    with pytest.raises(RestoreError):
        plan_restore(bundle=bundle, identity=key, staging=tmp_path / "corrupt-stage")


def test_preflight_blocks_drift_and_device_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from recovery.full_recovery import preflight
    from recovery.policy import RecoveryPolicy
    from recovery.storage import DeviceObservation, validate_device

    unknown = tmp_path / "unknown.sqlite3"; unknown.write_bytes(b"not-empty")
    key = tmp_path / "identity.txt"; subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"; recipients.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    policy = RecoveryPolicy(sqlite_scan_roots=(tmp_path,), known_sqlite=frozenset())
    assert preflight(config_path=recovery_config(tmp_path, recipients), policy=policy)["error_code"] == "INVENTORY_DRIFT"
    expected = dict(uuid="ID", label="LIVA_RECOVERY", filesystem="vfat", min_capacity_bytes=100)
    assert validate_device(DeviceObservation(False), **expected) == "USB_MISSING"
    assert validate_device(DeviceObservation(True, uuid="ID", label="OTHER", filesystem="vfat", removable=True, size=100), **expected) == "WRONG_DEVICE"
    assert validate_device(DeviceObservation(True, uuid="ID", label="LIVA_RECOVERY", filesystem="vfat", removable=True, size=100, read_only=True), **expected) == "USB_READ_ONLY"
    assert validate_device(DeviceObservation(True, uuid="ID", label="LIVA_RECOVERY", filesystem="vfat", removable=True, size=10), **expected) == "SPACE_LOW"


def test_backup_blocks_fat_limit_and_removes_failed_partial(tmp_path: Path) -> None:
    from recovery.full_recovery import RecoveryError, create_backup
    from recovery.policy import RecoveryPolicy, RecoverySource

    key = tmp_path / "identity.txt"; subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"; recipients.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    source = tmp_path / "source.txt"; source.write_bytes(b"too-large-for-test")
    config = recovery_config(tmp_path, recipients)
    config.write_text(config.read_text().replace("min_capacity_bytes = 1", "min_capacity_bytes = 1\nfat_max_file_bytes = 1"))
    with pytest.raises(RecoveryError, match="FAT32") as error:
        create_backup(config_path=config, target_dir=tmp_path / "usb", policy=RecoveryPolicy((RecoverySource("ordinary", source, "file"),)), project_root=tmp_path)
    assert error.value.code == "FAT_FILE_LIMIT"
    assert not list((tmp_path / "usb").glob("*.partial")) if (tmp_path / "usb").exists() else True


def test_backup_reports_failed_sqlite_snapshot(tmp_path: Path) -> None:
    from recovery.full_recovery import RecoveryError, create_backup
    from recovery.policy import RecoveryPolicy, RecoverySource

    key = tmp_path / "identity.txt"; subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"; recipients.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    bad_database = tmp_path / "missing-snapshot.sqlite3"; bad_database.write_text("not a sqlite database")
    with pytest.raises(RecoveryError) as error:
        create_backup(config_path=recovery_config(tmp_path, recipients), target_dir=tmp_path / "usb", policy=RecoveryPolicy((RecoverySource("liva_sqlite", bad_database, "sqlite"),)), project_root=tmp_path)
    assert error.value.code == "SQLITE_CHECK_FAILED"


def test_backup_rejects_duplicate_archive_paths(tmp_path: Path) -> None:
    from recovery.full_recovery import RecoveryError, create_backup
    from recovery.policy import RecoveryPolicy, RecoverySource

    key = tmp_path / "identity.txt"; subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"; recipients.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    source = tmp_path / "same.txt"; source.write_text("one source, two declarations")
    policy = RecoveryPolicy((RecoverySource("first", source, "file"), RecoverySource("second", source, "file")))
    with pytest.raises(RecoveryError) as error:
        create_backup(config_path=recovery_config(tmp_path, recipients), target_dir=tmp_path / "usb", policy=policy, project_root=tmp_path)
    assert error.value.code == "INVENTORY_DRIFT"


def test_catchup_is_a_noop_when_backup_is_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    from recovery import automation

    monkeypatch.setattr(automation, "preflight", lambda **_: {"ok": True, "device": {}})
    monkeypatch.setattr(automation.store, "get_full_recovery_state", lambda: {"backup_due": False, "backup_id": "known"})
    monkeypatch.setattr(automation, "run_backup", lambda **_: pytest.fail("catch-up must not create a backup when not due"))

    assert automation.run_catchup() == {"ok": True, "mode": "noop", "reason": "backup_not_due", "backup_id": "known"}


def test_retention_discovers_and_protects_restore_tested_bundle(tmp_path: Path) -> None:
    from recovery.retention import discover, protected

    bundle = tmp_path / "LIVA-FR-v1_20260923T000000Z_test.tar.zst.age"
    bundle.write_bytes(b"ciphertext")
    receipt = bundle.with_name(bundle.name + ".receipt.json")
    receipt.write_text(
        '{"format_version":"LIVA-FR-v1","backup_id":"test","finished_at":"2026-09-23T00:00:00Z","status":"restore_tested","restore_tested":true}',
        encoding="utf-8",
    )

    records = discover(tmp_path)
    assert len(records) == 1
    assert records[0].restore_tested is True
    assert bundle in protected(records)


def recovery_config(tmp_path: Path, recipients: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(f'''[recovery]\nstaging_root = "{tmp_path / 'staging'}"\n[usb]\nuuid = "TEST-UUID"\nlabel = "LIVA_RECOVERY"\nfilesystem = "vfat"\nmin_capacity_bytes = 1\n[crypto]\nrecipients_file = "{recipients}"\n''')
    return config
