from __future__ import annotations

from scripts import cleanup_runtime_artifacts as cleanup


def test_find_empty_runtime_artifacts_only_returns_zero_byte_db_like_files(tmp_path):
    (tmp_path / "database").mkdir()
    (tmp_path / ".venv").mkdir()

    empty_db = tmp_path / "database" / "hrv.db"
    empty_db.write_bytes(b"")
    empty_wal = tmp_path / "database" / "polar.sqlite3-wal"
    empty_wal.write_bytes(b"")
    non_empty = tmp_path / "database" / "polar.sqlite3"
    non_empty.write_bytes(b"x")
    skipped = tmp_path / ".venv" / "ignored.db"
    skipped.write_bytes(b"")

    matches = cleanup.find_empty_runtime_artifacts(tmp_path)

    assert matches == [empty_db, empty_wal]


def test_cleanup_empty_runtime_artifacts_deletes_matches(tmp_path):
    target = tmp_path / "var" / "data.db"
    target.parent.mkdir()
    target.write_bytes(b"")

    removed = cleanup.cleanup_empty_runtime_artifacts(tmp_path)

    assert removed == [target]
    assert not target.exists()
