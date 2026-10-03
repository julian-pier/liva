from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import database.connections as db_conn
from core import core_daily_board as board


def _mk(path: str, sql: str = "") -> None:
    conn = sqlite3.connect(path)
    try:
        if sql:
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def _prep(tmp_path: Path) -> str:
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    db_conn.HRV_DB = str(tmp_path / "hrv.sqlite3")
    db_conn.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    db_conn.PLANS_DB = str(tmp_path / "plans.sqlite3")
    db_conn.RUNS_DB = str(tmp_path / "runs.sqlite3")
    db_conn.POLAR_DB = str(tmp_path / "polar.sqlite3")
    day = date.today().isoformat()
    _mk(
        db_conn.HRV_DB,
        """
        CREATE TABLE hrv_measurements (
            id INTEGER PRIMARY KEY,
            date_utc TEXT,
            ts_measurement TEXT,
            rmssd REAL,
            hr REAL,
            sleep_quality REAL,
            fatigue REAL,
            training_motivation REAL,
            sickness_bool INTEGER
        );
        """,
    )
    _mk(db_conn.NUTRITION_DB, "CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL);")
    _mk(db_conn.TRAINING_DB, "CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT, notes TEXT);")
    _mk(db_conn.RUNS_DB, "CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time REAL, name TEXT);")
    _mk(db_conn.PLANS_DB, "CREATE TABLE gym_plans (id INTEGER PRIMARY KEY, title TEXT, plan_json TEXT, updated_at TEXT, is_active INTEGER, is_archived INTEGER DEFAULT 0);")
    _mk(db_conn.CORE_DB)
    return day


def test_core_board_is_precheck_without_morning_checkin(tmp_path: Path):
    day = _prep(tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db_conn.HRV_DB)
    conn.execute(
        "INSERT INTO hrv_measurements (date_utc, ts_measurement, rmssd, hr) VALUES (?, ?, 62, 52)",
        (yesterday, f"{yesterday}T07:10:00Z"),
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    conn.execute("INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)", (yesterday, 80.4))
    conn.commit()
    conn.close()

    payload = board.core_board_view_model(day)
    ui = payload["ui"]

    assert ui["board_state"] in {"precheck", "stale"}
    assert ui["board_is_final"] is False
    assert ui["headline"] == "Vorläufiges CORE Board"
    assert "Morning Check-in fehlt" in ui["board_status"]
    freshness = {item["key"]: item["freshness"] for item in ui["data_status_items"]}
    details = {item["key"]: item["detail"] for item in ui["data_status_items"]}
    assert freshness["weight"] == "gestern"
    assert freshness["hrv"] == "gestern"
    assert freshness["morning_checkin"] == "gestern"
    assert details["morning_checkin"] == "gestern · nicht verwendet"
    visible_labels = {item["label"] for item in ui["today_signal_items"]}
    assert "Motivation-Lage" not in visible_labels
    assert "Energie-Lage" not in visible_labels
    assert "Schmerz-/Warnsignal" not in visible_labels


def test_core_board_is_final_with_today_subjective_signal(tmp_path: Path):
    day = _prep(tmp_path)
    conn = sqlite3.connect(db_conn.HRV_DB)
    conn.execute(
        """
        INSERT INTO hrv_measurements (date_utc, ts_measurement, rmssd, hr, sleep_quality, fatigue, training_motivation)
        VALUES (?, ?, 71, 49, 7.5, 3.0, 7.0)
        """,
        (day, f"{day}T06:55:00Z"),
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    conn.execute("INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)", (day, 79.8))
    conn.commit()
    conn.close()

    payload = board.core_board_view_model(day)
    ui = payload["ui"]

    assert ui["board_is_final"] is True
    assert ui["board_state"] == "final"
    assert "Final" in ui["board_status"]
    assert ui["morning_checkin_done_today"] is True
    visible_labels = {item["label"] for item in ui["today_signal_items"]}
    assert "Motivation-Lage" in visible_labels
    balance = ui["signal_balance"]
    assert balance["title"] == "Signalbilanz"
    assert len(balance["zones"]) == 4
    assert len(ui["today_signal_top"]) <= 5
    assert len(ui["today_signal_primary"]) <= 12


def test_yesterday_morning_checkin_subjective_values_are_not_used(tmp_path: Path):
    day = _prep(tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db_conn.HRV_DB)
    conn.execute(
        """
        INSERT INTO hrv_measurements (date_utc, ts_measurement, rmssd, hr, sleep_quality, fatigue, training_motivation)
        VALUES (?, ?, 68, 50, 7.1, 3.8, 6.2)
        """,
        (yesterday, f"{yesterday}T06:55:00Z"),
    )
    conn.commit()
    conn.close()

    payload = board.core_board_view_model(day)
    ui = payload["ui"]

    labels = {item["label"] for item in ui["today_signal_items"]}
    assert "Motivation-Lage" not in labels
    assert "Energie-Lage" not in labels
    assert "Schmerz-/Warnsignal" not in labels
    data_items = {item["key"]: item for item in ui["data_status_items"]}
    assert data_items["morning_checkin"]["freshness"] == "gestern"
    assert data_items["morning_checkin"]["detail"] == "gestern · nicht verwendet"


def test_polar_sleep_score_today_without_duration_is_used_but_sleep_debt_is_not(tmp_path: Path):
    day = _prep(tmp_path)
    conn = sqlite3.connect(db_conn.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE polar_sleep (
            date TEXT PRIMARY KEY,
            raw_json TEXT,
            sleep_score REAL,
            sleep_start TEXT,
            sleep_end TEXT,
            sleep_minutes INTEGER,
            actual_sleep_minutes INTEGER,
            deep_sleep_minutes INTEGER,
            rem_sleep_minutes INTEGER,
            interruptions INTEGER,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO polar_sleep (date, raw_json, sleep_score, sleep_minutes, actual_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes, created_at, updated_at)
        VALUES (?, '{}', 87, NULL, NULL, NULL, NULL, '2026-05-10T00:00:00Z', '2026-05-10T00:00:00Z')
        """,
        (day,),
    )
    conn.commit()
    conn.close()

    payload = board.core_board_view_model(day)
    ui = payload["ui"]
    signals = {item["label"]: item for item in ui["today_signal_items"]}
    data_items = {item["key"]: item for item in ui["data_status_items"]}

    assert "Schlafqualität" in signals
    assert signals["Schlafqualität"]["headline"] == "Schlafscore gut"
    assert "Polar Sleep Score 87/100" in signals["Schlafqualität"]["evidence"]
    assert "Schlafschuld" not in signals
    assert data_items["sleep_duration"]["detail"] == "Schlafscore vorhanden, Dauer fehlt"


def test_optional_unknown_signals_are_hidden_from_main_board(tmp_path: Path):
    day = _prep(tmp_path)
    payload = board.core_board_view_model(day)
    ui = payload["ui"]

    labels = {item["label"] for item in ui["today_signal_items"]}
    assert "Performance-Trend" not in labels
    assert "Motivation-Lage" not in labels
    assert "Energie-Lage" not in labels
    assert len(ui["today_signal_top"]) <= 5
    assert len(ui["today_signal_primary"]) <= 12
