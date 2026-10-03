from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from liva_mcp.config import Config
from liva_mcp.read_service import ReadService


def _create(path: Path, statements: list[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    database = tmp_path / "database"
    database.mkdir()
    _create(
        database / "training.sqlite3",
        [
            "CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT, created_at TEXT)",
            "INSERT INTO workouts VALUES (1, date('now'), 'FB Pull', '2026-01-01T12:00:00Z')",
            "CREATE TABLE school_schedule_snapshots (id INTEGER PRIMARY KEY, fetched_at TEXT, source TEXT, date TEXT, raw_json TEXT, derived_summary_json TEXT, created_at TEXT)",
            "CREATE TABLE school_schedule_entries (id INTEGER PRIMARY KEY, snapshot_id INTEGER, date TEXT, subject TEXT, teacher TEXT, room TEXT, start_time TEXT, end_time TEXT, raw_text TEXT, status_hint TEXT)",
            "CREATE TABLE core_override_choices (date TEXT PRIMARY KEY, choice TEXT, created_at TEXT)",
            "CREATE TABLE autopilot_day (date TEXT PRIMARY KEY, mode TEXT, output_json TEXT, reason_codes_json TEXT, created_ts INTEGER)",
        ],
    )
    plan = json.dumps({"meta": {"mode": "rolling_sequence"}, "sequence": [{"title": "A"}]})
    _create(
        database / "plans.sqlite3",
        [
            "CREATE TABLE gym_plans (id INTEGER PRIMARY KEY, title TEXT, focus TEXT, plan_json TEXT, is_active INTEGER, is_archived INTEGER, updated_at TEXT)",
            "INSERT INTO gym_plans VALUES (1, 'Plan A', 'Strength', " + repr(plan) + ", 1, 0, '2026-01-01')",
        ],
    )
    _create(
        database / "hrv.sqlite3",
        [
            "CREATE TABLE hrv_measurements (id INTEGER PRIMARY KEY, ts_measurement TEXT, date_utc TEXT, hr REAL, rmssd REAL, sdnn REAL, signal_quality TEXT, training_motivation REAL, fatigue REAL, sickness TEXT, sleep_quality REAL, alcohol TEXT, sickness_bool INTEGER, alcohol_bool INTEGER, flag TEXT)",
            "INSERT INTO hrv_measurements VALUES (1, '2026-01-01', '2026-01-01', 42, 100, 80, 'good', 8, 2, 'no', 8, 'no', 0, 0, NULL)",
        ],
    )
    _create(
        database / "ernaehrung.sqlite3",
        [
            "CREATE TABLE nutrition_settings (key TEXT PRIMARY KEY, value TEXT)",
            "INSERT INTO nutrition_settings VALUES ('active_mode', 'lean_bulk')",
            "INSERT INTO nutrition_settings VALUES ('mode_lean_bulk_target', '2650')",
            "INSERT INTO nutrition_settings VALUES ('mode_lean_bulk_protein_target', '180')",
            "INSERT INTO nutrition_settings VALUES ('mode_lean_bulk_carbs_target', '300')",
            "INSERT INTO nutrition_settings VALUES ('mode_lean_bulk_fat_target', '65')",
            "CREATE TABLE nutrition_daily (date TEXT, kcal REAL, protein REAL, carbs REAL, fat REAL)",
            "INSERT INTO nutrition_daily VALUES (date('now'), 2500, 175, 280, 70)",
            "CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL, kcal REAL, protein REAL, carbs REAL, fat REAL, created_at TEXT)",
            "INSERT INTO weight_logs (id,date_iso,weight_kg,created_at) VALUES (1, date('now'), 70.5, '2026-01-01')",
            "CREATE TABLE actions_v2_state (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)",
            "CREATE TABLE nutrition_week_templates (id INTEGER PRIMARY KEY, title TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT, archived_at TEXT, last_used_at TEXT, macro_active_mode TEXT, macro_modes_json TEXT)",
            "CREATE TABLE nutrition_week_template_days (id INTEGER PRIMARY KEY, week_template_id INTEGER, weekday INTEGER, day_type TEXT, target_kcal REAL, target_p REAL, target_c REAL, target_f REAL, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE nutrition_week_plans (id INTEGER PRIMARY KEY, title TEXT, is_active INTEGER, start_monday TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE nutrition_day_actuals (id INTEGER PRIMARY KEY, day TEXT, kcal REAL, p REAL, c REAL, f REAL, source TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE nutrition_active_day_plan (day_iso TEXT PRIMARY KEY, signature TEXT, payload_json TEXT, rebuilt_at TEXT)",
        ],
    )
    _create(
        database / "runs.sqlite3",
        [
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time INTEGER, avg_hr REAL, max_hr REAL, elevation_gain REAL, pace REAL, sport_type TEXT, run_type TEXT, avg_power REAL, stair_floors REAL)",
            "INSERT INTO runs VALUES (1, '2026-01-01', 5000, 1500, 140, 160, 20, 5, 'run', 'easy', NULL, NULL)",
        ],
    )
    _create(
        database / "polar.sqlite3",
        [
            "CREATE TABLE polar_sleep (date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT, sleep_minutes INTEGER, actual_sleep_minutes INTEGER, deep_sleep_minutes INTEGER, rem_sleep_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT, light_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER)",
            "CREATE TABLE polar_nightly_recharge (date TEXT PRIMARY KEY, raw_json TEXT, ans_status REAL, recovery_indicator REAL, recovery_indicator_sublevel REAL, ans_rate REAL, mean_recovery_rri REAL, mean_recovery_rmssd REAL, mean_recovery_respiration_interval REAL, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE polar_continuous_samples (date TEXT PRIMARY KEY, raw_json TEXT, sample_count INTEGER, min_hr REAL, max_hr REAL, avg_hr REAL, resting_candidate_hr REAL, created_at TEXT, updated_at TEXT)",
        ],
    )
    _create(
        database / "core.sqlite3",
        [
            "CREATE TABLE actions_v2_state (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)",
            "CREATE TABLE core_training_cards (id INTEGER PRIMARY KEY, mode TEXT, card_type TEXT, payload_json TEXT, created_at TEXT, day_iso TEXT, board_id INTEGER, updated_at TEXT, source TEXT, status TEXT, session_type TEXT, session_label TEXT, decision_intent TEXT, title TEXT, summary TEXT, card_json TEXT, data_quality_json TEXT, source_context_json TEXT, user_feedback TEXT, user_feedback_at TEXT)",
            "CREATE TABLE training_ai_decisions (id INTEGER PRIMARY KEY, day_iso TEXT, checkin_id INTEGER, bodyweight_entry_id INTEGER, planned_session_id INTEGER, plan_id INTEGER, session_name TEXT, session_type TEXT, status TEXT, source TEXT, model TEXT, context_hash TEXT, decision_json TEXT, trigger_reason TEXT, invalidated_by TEXT, created_at TEXT, updated_at TEXT, superseded_at TEXT, error_message TEXT)",
        ],
    )
    return tmp_path


@pytest.fixture
def service(repo_root: Path) -> ReadService:
    return ReadService(Config(repo_root=repo_root))
