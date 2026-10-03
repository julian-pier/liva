from __future__ import annotations

import sqlite3

import pytest

from liva_mcp.repository import ReadOnlyRepository


def test_connection_reports_query_only(repo_root):
    repository = ReadOnlyRepository(repo_root / "database" / "training.sqlite3")
    with repository.connect() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO workouts (date_iso, name) VALUES ('x', 'x')",
        "UPDATE workouts SET name='x'",
        "DELETE FROM workouts",
        "CREATE TABLE forbidden (id INTEGER)",
        "ALTER TABLE workouts ADD COLUMN forbidden TEXT",
    ],
)
def test_sql_writes_fail(repo_root, statement):
    repository = ReadOnlyRepository(repo_root / "database" / "training.sqlite3")
    with repository.connect() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)


def test_repository_rejects_non_select_before_sqlite(repo_root):
    repository = ReadOnlyRepository(repo_root / "database" / "training.sqlite3")
    with pytest.raises(PermissionError):
        repository.all("DELETE FROM workouts")
