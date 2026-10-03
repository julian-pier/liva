from __future__ import annotations

import sqlite3

from database.bootstrap import ensure_base_schemas


def test_empty_install_gets_required_base_tables(tmp_path):
    ensure_base_schemas(tmp_path)

    expected = {
        "training.sqlite3": {"workouts", "exercises", "sets"},
        "ernaehrung.sqlite3": {"weight_logs"},
        "hrv.sqlite3": {"hrv_measurements"},
        "runs.sqlite3": {"runs"},
    }
    for filename, tables in expected.items():
        with sqlite3.connect(tmp_path / filename) as connection:
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert tables <= actual


def test_bootstrap_is_idempotent_and_preserves_rows(tmp_path):
    ensure_base_schemas(tmp_path)
    with sqlite3.connect(tmp_path / "training.sqlite3") as connection:
        connection.execute(
            "INSERT INTO workouts(date_iso, name) VALUES (?, ?)",
            ("2026-01-01", "Example"),
        )

    ensure_base_schemas(tmp_path)
    with sqlite3.connect(tmp_path / "training.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
    assert count == 1
