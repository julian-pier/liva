from __future__ import annotations

import sqlite3
from pathlib import Path

from app_support.liva_mcp_overlay import day_context, merge_weight_series


def test_overlay_context_and_weight_merge_are_read_only(tmp_path):
    path = tmp_path / "liva_mcp_writes.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE daily_notes (id INTEGER PRIMARY KEY, date_iso TEXT, category TEXT, text TEXT, created_at TEXT);
    CREATE TABLE daily_flags (id INTEGER PRIMARY KEY, date_iso TEXT, flag_type TEXT, severity TEXT, body_part TEXT, text TEXT, created_at TEXT);
    CREATE TABLE nutrition_context_logs (id INTEGER PRIMARY KEY, date_iso TEXT, text TEXT, precision_level TEXT, values_json TEXT, confidence TEXT, created_at TEXT);
    CREATE TABLE training_rawlogs (id INTEGER PRIMARY KEY, date_iso TEXT, session_name TEXT, raw_text TEXT, parsed_candidates_json TEXT, created_at TEXT);
    CREATE TABLE weight_entries (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL, note TEXT, updated_at TEXT);
    """)
    conn.execute("INSERT INTO daily_notes VALUES (1,'2026-07-11','system','visible','2026-07-11T10:00:00Z')")
    conn.execute("INSERT INTO daily_flags VALUES (1,'2026-07-11','pain','low','shoulder',NULL,'2026-07-11T10:00:00Z')")
    conn.execute("INSERT INTO nutrition_context_logs VALUES (1,'2026-07-11','Protein safe','context','{}',NULL,'2026-07-11T10:00:00Z')")
    conn.execute("INSERT INTO weight_entries VALUES (1,'2026-07-11',70.2,'correction','2026-07-11T10:00:00Z')")
    conn.commit(); conn.close()
    context = day_context("2026-07-11", path=path)
    merged = merge_weight_series([{"date":"11.07.26", "date_iso":"2026-07-11", "bodyweight_kg":70.4, "calories":None}], path=path)
    assert context["daily_notes"][0]["source"] == "mcp_overlay"
    assert context["daily_flags"][0]["body_part"] == "shoulder"
    assert context["nutrition_context"][0]["precision_level"] == "context"
    assert context["weight_entries"] == [{"id": 1, "date_iso": "2026-07-11", "weight_kg": 70.2, "note": "correction", "updated_at": "2026-07-11T10:00:00Z", "source": "mcp_overlay"}]
    assert merged[0]["bodyweight_kg"] == 70.2 and merged[0]["weight_source"] == "mcp_overlay"


def test_dashboard_context_api_exposes_explicit_overlay(monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "enforce_private_access", lambda: None)
    monkeypatch.setattr(appmod, "enforce_key_read_only", lambda: None)
    monkeypatch.setattr(appmod, "enforce_ip_whitelist", lambda: None)
    monkeypatch.setattr(appmod, "liva_mcp_day_context", lambda day: {"source":"mcp_overlay", "daily_notes":[{"text":"visible", "source":"mcp_overlay"}], "nutrition_context":[], "daily_flags":[], "training_rawlogs":[], "weight_entries":[{"weight_kg":69.8}]})
    response = appmod.app.test_client().get("/api/dashboard/mcp_context?date=2026-07-11")
    assert response.status_code == 200
    assert response.get_json()["daily_notes"][0]["text"] == "visible"
    assert response.get_json()["weight_entries"][0]["weight_kg"] == 69.8


def test_dashboard_does_not_render_an_mcp_overlay_card():
    template = (Path(__file__).parents[1] / "templates" / "dashboard.html").read_text()
    script = (Path(__file__).parents[1] / "static" / "js" / "dashboard_vnext.js").read_text()
    stylesheet = (Path(__file__).parents[1] / "static" / "css" / "dashboard_new.css").read_text()
    assert "ChatGPT / MCP Kontext" not in template
    assert "dashboard-mcp-context-card" not in template
    assert "mcp_overlay" not in template
    assert template.count("20260727_professional_v9") == 3
    assert "renderMcpContext" not in script
    assert "dashboard-mcp-context-card" not in script
    assert "mcp-context-card" not in stylesheet
