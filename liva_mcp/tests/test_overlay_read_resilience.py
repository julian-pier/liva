from __future__ import annotations

import sqlite3

from liva_mcp.write_service import WriteService


def test_recent_treats_pre_schema_overlay_as_empty(tmp_path):
    path = tmp_path / "overlay.sqlite3"
    sqlite3.connect(path).close()

    overlay = WriteService(path)

    assert overlay.recent("daily_flags", "2026-07-27") == []


def test_initialize_makes_all_read_overlay_tables_available(tmp_path):
    overlay = WriteService(tmp_path / "overlay.sqlite3")

    overlay.initialize()

    for table in (
        "daily_notes",
        "daily_flags",
        "nutrition_context_logs",
        "training_rawlogs",
        "weight_entries",
    ):
        assert overlay.recent(table, "2026-07-27") == []
