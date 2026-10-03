from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from datetime import datetime
from zoneinfo import ZoneInfo

from nutrition import core_nutrition_engine as engine
from nutrition import nutrition_planning_db as npdb


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _mk_empty(path):
    conn = sqlite3.connect(path)
    conn.close()


def test_core_nutrition_generates_explainable_change_target(tmp_path, monkeypatch):
    ndb = tmp_path / "nutrition.sqlite3"
    pdb = tmp_path / "plans.sqlite3"
    tdb = tmp_path / "training.sqlite3"
    _mk_empty(ndb)
    _mk_empty(pdb)
    _mk_empty(tdb)

    monkeypatch.setattr(npdb, "get_nutrition_db", lambda: _conn(ndb))
    monkeypatch.setattr(engine, "get_nutrition_db", lambda: _conn(ndb))
    monkeypatch.setattr(engine, "get_plans_db", lambda: _conn(pdb))
    monkeypatch.setattr(engine, "get_training_db", lambda: _conn(tdb))

    npdb.ensure_nutrition_planning_schema()
    engine.ensure_core_nutrition_schema()

    conn = _conn(ndb)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS nutrition_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weight_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                date_iso TEXT NOT NULL UNIQUE,
                weight_kg REAL,
                kcal INTEGER,
                protein INTEGER,
                carbs INTEGER,
                fat INTEGER,
                sugar INTEGER,
                created_at TEXT NOT NULL,
                raw_json TEXT
            )
            """
        )
        conn.execute("INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES ('active_mode', 'lean_bulk')")
        conn.execute("INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES ('mode_lean_bulk_target', '2800')")
        conn.execute("INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES ('mode_lean_bulk_protein_target', '190')")
        conn.execute("INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES ('mode_lean_bulk_carbs_target', '320')")
        conn.execute("INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES ('mode_lean_bulk_fat_target', '80')")

        day = date.today()
        conn.execute(
            "INSERT OR REPLACE INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
            ((day - timedelta(days=7)).strftime("%d.%m.%y"), (day - timedelta(days=7)).isoformat(), 80.0, 2600, 180, 290, 75, 0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
            (day.strftime("%d.%m.%y"), day.isoformat(), 80.0, 2500, 175, 280, 70, 0),
        )
        conn.commit()
    finally:
        conn.close()

    out = engine.evaluate_core_nutrition_day(date.today().isoformat())
    assert out["ok"] is True
    assert out["status"] in {"CORE ADJUSTED", "CATCH-UP", "MANUELL", "CORE STEUERT"}
    assert isinstance(out.get("decisions"), list)
    assert isinstance(out.get("decision_state"), dict)
    assert str((out.get("decision_state") or {}).get("decision_id") or "").strip()
    assert isinstance(out.get("decision_narrative"), dict)
    assert isinstance((out.get("decision_narrative") or {}).get("header"), dict)
    assert isinstance(out.get("decision_history"), list)

    conn2 = _conn(ndb)
    try:
        day_state = conn2.execute("SELECT status, explain_json FROM core_nutrition_day_state WHERE date_iso=?", (date.today().isoformat(),)).fetchone()
        assert day_state is not None
        actions = conn2.execute("SELECT COUNT(*) AS c FROM core_nutrition_actions WHERE date_iso=?", (date.today().isoformat(),)).fetchone()
        assert int(actions["c"]) >= 0
    finally:
        conn2.close()


def test_core_nutrition_prioritizes_shift_to_now_after_skipped_meal(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_now_berlin",
        lambda: datetime(2026, 3, 10, 13, 40, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    payload = {
        "targets": {"mode": "lean_bulk", "kcal": 2800, "p": 190, "c": 320, "f": 80},
        "planned_totals": {"kcal": 2600, "p": 170, "c": 300, "f": 70},
        "logged_totals": {"kcal": 900, "p": 60, "c": 90, "f": 20},
        "remaining": {"kcal": 1900, "p": 130, "c": 230, "f": 60},
        "planned_meals": [
            {"slot_id": 1, "slot_index": 1, "title": "Meal 2", "time_text": "12:00", "status": "skipped", "macros": {}, "items": [], "servings": 1.0},
            {"slot_id": 2, "slot_index": 2, "title": "Meal 3", "time_text": "14:15", "status": "open", "macros": {}, "items": [], "servings": 1.0},
        ],
    }
    context = {"school": {"early_home": True, "school_end": "12:15", "largest_free_window_min": 90}}
    actions = engine._rule_actions("2026-03-10", payload, context, {"available": False})  # type: ignore[attr-defined]
    shift = next((a for a in actions if a.get("action_type") == "shift_meal"), None)
    assert shift is not None
    assert shift.get("reason_codes") == ["MISSED_MEAL"]
    assert (shift.get("payload") or {}).get("to_time") == "13:40"
