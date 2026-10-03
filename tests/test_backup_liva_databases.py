from __future__ import annotations

import importlib
import json
import tarfile
from datetime import datetime, timezone


def _reload_backup_module():
    import scripts.backup_liva_databases as backup_module

    importlib.reload(backup_module)
    return backup_module


def test_backup_archive_created_with_manifest(tmp_path):
    backup_module = _reload_backup_module()
    project_root = tmp_path / "project"
    database_dir = project_root / "database"
    database_dir.mkdir(parents=True)
    (database_dir / "training.sqlite3").write_bytes(b"training-db")
    (project_root / "core.sqlite3").write_bytes(b"core-db")
    backup_dir = tmp_path / "backups" / "liva-db"

    archive_path = backup_module.create_backup_archive(
        project_root=project_root,
        backup_dir=backup_dir,
        now=datetime(2026, 5, 23, 4, 20, 0, tzinfo=timezone.utc),
    )

    assert archive_path.exists()
    assert archive_path.suffixes[-2:] == [".tar", ".gz"]
    assert not any(path.suffix == ".tmp" for path in backup_dir.iterdir())

    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "database/training.sqlite3" in names
        assert "core.sqlite3" in names
        assert "manifest.json" in names
        manifest = json.load(tar.extractfile("manifest.json"))
    assert manifest["created_at"] == "2026-05-23T04:20:00+00:00"
    assert len(manifest["source_files"]) == 2


def test_no_databases_raises_error(tmp_path):
    backup_module = _reload_backup_module()
    project_root = tmp_path / "project"
    project_root.mkdir()
    backup_dir = tmp_path / "backups" / "liva-db"

    try:
        backup_module.create_backup_archive(project_root=project_root, backup_dir=backup_dir)
    except RuntimeError as exc:
        assert "Keine SQLite-Datenbanken gefunden" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError when no sqlite databases exist")
