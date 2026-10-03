from __future__ import annotations

import importlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

import database.connections as db_conn


def _load_app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    db_conn.RUNS_DB = str(tmp_path / "runs.sqlite3")
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    db_conn.PLANS_DB = str(tmp_path / "plans.sqlite3")
    db_conn.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    db_conn.HRV_DB = str(tmp_path / "hrv.sqlite3")
    db_conn.POLAR_DB = str(tmp_path / "polar.sqlite3")

    import nutrition.nutrition_planning_db as npdb

    npdb._SCHEMA_READY = False
    importlib.reload(npdb)

    if "app" in os.sys.modules:
        appmod = importlib.reload(os.sys.modules["app"])
    else:
        appmod = importlib.import_module("app")

    appmod._NUTRITION_DAILY_VIEW_READY = False
    return appmod, npdb


def _seed_supporting_dbs():
    conn = sqlite3.connect(db_conn.HRV_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hrv_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_measurement TEXT,
            date_utc TEXT,
            hr REAL,
            rmssd REAL,
            sdnn REAL,
            avnn REAL,
            source_file TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_conn.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            raw_json TEXT,
            ans_status REAL,
            recovery_indicator REAL,
            recovery_indicator_sublevel REAL,
            ans_rate REAL,
            mean_recovery_rri REAL,
            mean_recovery_rmssd REAL,
            mean_recovery_respiration_interval REAL,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_sleep (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            raw_json TEXT,
            sleep_score REAL,
            sleep_start TEXT,
            sleep_end TEXT,
            sleep_minutes REAL,
            actual_sleep_minutes REAL,
            deep_sleep_minutes REAL,
            rem_sleep_minutes REAL,
            light_sleep_minutes REAL,
            awake_minutes REAL,
            interruptions REAL,
            interruption_minutes REAL,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_continuous_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            raw_json TEXT,
            sample_count REAL,
            min_hr REAL,
            max_hr REAL,
            avg_hr REAL,
            resting_candidate_hr REAL,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            raw_json TEXT,
            steps REAL,
            active_minutes REAL,
            inactivity_count REAL,
            calories REAL,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_conn.RUNS_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            distance REAL
        )
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_conn.TRAINING_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_iso TEXT
        );
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER,
            workout_id INTEGER,
            weight REAL,
            reps INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


def test_dashboard_signals_energy_uses_active_week_template_target(monkeypatch, tmp_path):
    appmod, npdb = _load_app_module(monkeypatch, tmp_path)
    _seed_supporting_dbs()

    npdb.ensure_nutrition_planning_schema()

    today_iso = date.today().isoformat()
    weekday_key = str(date.today().weekday())
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()

    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            weight_kg REAL,
            kcal REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            sugar REAL,
            created_at TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        DELETE FROM nutrition_settings;
        DELETE FROM weight_logs;
        DELETE FROM nutrition_week_template_timeline;
        DELETE FROM nutrition_week_template_days;
        DELETE FROM nutrition_week_templates;
        """
    )
    conn.execute(
        "INSERT INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (today_iso, today_iso, 80.0, 2200.0, 180.0, 220.0, 70.0, 40.0, now_iso, "{}"),
    )
    settings_rows = [
        ("active_mode", "lean_bulk"),
        ("selected_mode", "lean_bulk"),
        ("mode_lean_bulk_target", "2800"),
        ("mode_lean_bulk_green_low", "2700"),
        ("mode_lean_bulk_green_high", "2900"),
        ("mode_lean_bulk_yellow_low", "2550"),
        ("mode_lean_bulk_yellow_high", "3050"),
        ("mode_maintenance_target", "2200"),
        ("mode_maintenance_green_low", "2100"),
        ("mode_maintenance_green_high", "2300"),
        ("mode_maintenance_yellow_low", "2000"),
        ("mode_maintenance_yellow_high", "2400"),
    ]
    conn.executemany("INSERT INTO nutrition_settings (key, value) VALUES (?, ?)", settings_rows)
    conn.execute(
        """
        INSERT INTO nutrition_week_templates (id, title, is_active, created_at, updated_at)
        VALUES (1, 'Current Targets', 1, ?, ?)
        """,
        (now_iso, now_iso),
    )
    conn.execute(
        """
        INSERT INTO nutrition_week_template_days (
            week_template_id, weekday, day_type, target_kcal, target_p, target_c, target_f, created_at, updated_at
        ) VALUES (?, ?, 'standard', ?, ?, ?, ?, ?, ?)
        """,
        (1, int(weekday_key), 2200.0, 180.0, 220.0, 70.0, now_iso, now_iso),
    )
    snapshot = {
        "template_id": 1,
        "title": "Current Targets",
        "active_mode": "maintenance",
        "active_mode_settings": {
            "kcal_target": 2200,
            "green_low": 2100,
            "green_high": 2300,
            "yellow_low": 2000,
            "yellow_high": 2400,
        },
        "day_targets": {
            weekday_key: {
                "kcal": 2200,
                "p": 180,
                "c": 220,
                "f": 70,
            }
        },
    }
    conn.execute(
        """
        INSERT INTO nutrition_week_template_timeline (
            week_template_id, start_date, end_date, snapshot_json, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (1, today_iso, json.dumps(snapshot), now_iso, now_iso),
    )
    conn.commit()
    conn.close()

    with appmod.app.test_request_context("/api/dashboard/signals"):
        resp = appmod.api_dashboard_signals()

    payload = resp.get_json()
    assert payload["ok"] is True
    latest = payload["lanes"]["energy"][-1]
    assert latest["kcal_target"] == 2200
    assert latest["target_source"] == "active_week_template"


def test_makros_series_uses_historical_plan_resolver_and_logged_target_override(monkeypatch, tmp_path):
    appmod, npdb = _load_app_module(monkeypatch, tmp_path)
    _seed_supporting_dbs()
    npdb.ensure_nutrition_planning_schema()

    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    conn.row_factory = sqlite3.Row
    now_iso = "2026-06-02T12:00:00"
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            weight_kg REAL,
            kcal REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            sugar REAL,
            created_at TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        DELETE FROM nutrition_settings;
        DELETE FROM weight_logs;
        DELETE FROM nutrition_week_template_timeline;
        DELETE FROM nutrition_week_template_days;
        DELETE FROM nutrition_week_templates;
        """
    )
    conn.executemany(
        "INSERT INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("15.01.24", "2024-01-15", 80.0, 2550.0, None, None, None, None, now_iso, "{}"),
            (
                "16.01.24",
                "2024-01-16",
                80.1,
                2480.0,
                None,
                None,
                None,
                None,
                now_iso,
                json.dumps(
                    {
                        "target": {
                            "kcal": 2480,
                            "green_low": 2400,
                            "green_high": 2550,
                            "yellow_low": 2300,
                            "yellow_high": 2650,
                            "mode": "custom",
                            "title": "Logged Override",
                        }
                    }
                ),
            ),
            ("22.07.25", "2025-07-22", 78.0, 2600.0, None, None, None, None, now_iso, "{}"),
            ("10.05.25", "2025-05-10", 78.0, 3020.0, None, None, None, None, now_iso, "{}"),
            ("05.04.26", "2026-04-05", 75.0, 2660.0, None, None, None, None, now_iso, "{}"),
            ("18.05.26", "2026-05-18", 74.5, 2490.0, None, None, None, None, now_iso, "{}"),
            ("26.05.26", "2026-05-26", 74.2, 2510.0, None, None, None, None, now_iso, "{}"),
            ("31.05.26", "2026-05-31", 74.0, 2910.0, None, None, None, None, now_iso, "{}"),
            ("02.02.21", "2021-02-02", 82.0, 2150.0, None, None, None, None, now_iso, "{}"),
        ],
    )
    conn.executemany(
        "INSERT INTO nutrition_settings (key, value) VALUES (?, ?)",
        [
            ("active_mode", "maintenance"),
            ("selected_mode", "maintenance"),
            ("target_kcal", "2200"),
            ("target_maintenance", "2200"),
            ("mode_maintenance_target", "2200"),
            ("mode_maintenance_green_low", "2100"),
            ("mode_maintenance_green_high", "2300"),
            ("mode_maintenance_yellow_low", "2000"),
            ("mode_maintenance_yellow_high", "2400"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO nutrition_week_templates
            (id, title, is_active, archived_at, last_used_at, created_at, updated_at, macro_active_mode, macro_modes_json)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        [
            (16, "Standardwoche", 0, "2026-04-05T18:59:02", "2026-02-08T22:15:58", "2026-04-05T18:59:02", "lean_bulk", "{}"),
            (17, "Mini-Cut", 0, "2026-05-27T19:57:52", "2026-04-05T14:06:34", "2026-05-27T19:57:52", "cut", "{}"),
            (31, "Reverse Erhalt · 2900 Standardwoche", 0, "2026-05-31T20:18:56", "2026-05-31T20:18:56", "2026-05-31T20:23:08", "custom", "{}"),
            (32, "Reverse Erhalt · 2900 Standardwoche V2", 0, "2026-05-31T20:21:58", "2026-05-31T20:21:58", "2026-05-31T20:45:41", "custom", "{}"),
            (33, "Reverse Erhalt · 2900 Standardwoche V3", 0, "2026-05-31T20:44:28", "2026-05-31T20:44:28", "2026-05-31T20:54:22", "custom", "{}"),
            (34, "Reverse Erhalt · 2900 Standardwoche V4", 0, "2026-05-31T20:53:06", "2026-05-31T20:53:06", "2026-05-31T21:01:23", "custom", "{}"),
            (35, "Reverse Erhalt · 2900 Standardwoche V5", 0, "2026-05-31T21:00:11", "2026-05-31T21:00:11", "2026-05-31T21:08:30", "custom", "{}"),
            (36, "Reverse Erhalt · 2900 Standardwoche V6", 0, "2026-05-31T21:07:25", "2026-05-31T21:07:25", "2026-05-31T21:14:38", "custom", "{}"),
            (38, "Reverse Erhalt · 2900 Standardwoche V8", 0, "2026-05-31T21:23:51", "2026-05-31T21:23:50", "2026-05-31T21:29:05", "custom", "{}"),
            (39, "Reverse Erhalt · 2900 Standardwoche V9", 1, "2026-05-31T21:28:03", "2026-05-31T21:28:03", "2026-05-31T21:28:03", "custom", "{}"),
        ],
    )
    timeline_rows = [
        (
            2,
            16,
            "2022-04-01",
            "2025-07-21",
            json.dumps(
                {
                    "template_id": 16,
                    "title": "Standardwoche",
                    "active_mode": "lean_bulk",
                    "active_mode_settings": {
                        "kcal_target": 3800,
                        "green_low": 3650,
                        "green_high": 4450,
                        "yellow_low": 3500,
                        "yellow_high": 4800,
                    },
                    "day_targets": {},
                }
            ),
            "2026-05-26T22:20:45",
            "2026-05-26T22:20:45",
        ),
        (
            4,
            17,
            "2025-07-22",
            "2025-08-24",
            json.dumps(
                {
                    "template_id": 17,
                    "title": "Mini-Cut",
                    "active_mode": "cut",
                    "active_mode_settings": {
                        "kcal_target": 2650,
                        "green_low": 2500,
                        "green_high": 2750,
                        "yellow_low": 2350,
                        "yellow_high": 2950,
                    },
                    "day_targets": {},
                }
            ),
            "2026-05-26T22:20:45",
            "2026-05-26T22:20:45",
        ),
        (
            5,
            16,
            "2025-08-25",
            "2026-04-04",
            json.dumps(
                {
                    "template_id": 16,
                    "title": "Standardwoche",
                    "active_mode": "lean_bulk",
                    "active_mode_settings": {
                        "kcal_target": 3800,
                        "green_low": 3650,
                        "green_high": 4450,
                        "yellow_low": 3500,
                        "yellow_high": 4800,
                    },
                    "day_targets": {},
                }
            ),
            "2026-05-26T22:20:45",
            "2026-05-26T22:20:45",
        ),
        (
            3,
            17,
            "2026-04-05",
            "2026-05-17",
            json.dumps(
                {
                    "template_id": 17,
                    "title": "Mini-Cut",
                    "active_mode": "cut",
                    "active_mode_settings": {
                        "kcal_target": 2650,
                        "green_low": 2500,
                        "green_high": 2750,
                        "yellow_low": 2350,
                        "yellow_high": 2950,
                    },
                    "day_targets": {},
                }
            ),
            "2026-05-26T22:20:45",
            "2026-05-26T22:20:45",
        ),
        (
            6,
            17,
            "2026-05-18",
            "2026-05-25",
            json.dumps(
                {
                    "template_id": 17,
                    "title": "Mini-Cut",
                    "active_mode": "cut",
                    "active_mode_settings": {
                        "kcal_target": 2500,
                        "green_low": 2350,
                        "green_high": 2650,
                        "yellow_low": 2200,
                        "yellow_high": 2800,
                    },
                    "day_targets": {},
                }
            ),
            "2026-05-26T22:20:45",
            "2026-05-26T22:20:45",
        ),
        (
            106,
            17,
            "2026-05-26",
            "2026-05-30",
            json.dumps(
                {
                    "template_id": 17,
                    "title": "Mini-Cut",
                    "active_mode": "cut",
                    "active_mode_settings": {
                        "kcal_target": 2500,
                        "green_low": 2350,
                        "green_high": 2650,
                        "yellow_low": 2200,
                        "yellow_high": 2800,
                    },
                    "day_targets": {},
                }
            ),
            "2026-06-02T09:42:37",
            "2026-06-02T09:42:37",
        ),
        (
            114,
            39,
            "2026-05-31",
            None,
            json.dumps(
                {
                    "template_id": 39,
                    "title": "Reverse Erhalt · 2900 Standardwoche V9",
                    "active_mode": "custom",
                    "active_mode_settings": {
                        "kcal_target": 2900,
                        "green_low": 2800,
                        "green_high": 3000,
                        "yellow_low": 2650,
                        "yellow_high": 3150,
                    },
                    "day_targets": {},
                }
            ),
            "2026-06-02T09:42:37",
            "2026-06-02T09:42:37",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO nutrition_week_template_timeline
            (id, week_template_id, start_date, end_date, snapshot_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        timeline_rows,
    )
    conn.commit()
    conn.close()

    # Re-running schema setup must not collapse a valid multi-phase timeline just
    # because newer templates exist without their own historical spans.
    npdb._SCHEMA_READY = False
    npdb.ensure_nutrition_planning_schema()

    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    conn.row_factory = sqlite3.Row
    timeline_after = conn.execute(
        """
        SELECT id, week_template_id, start_date, end_date
        FROM nutrition_week_template_timeline
        ORDER BY start_date ASC, id ASC
        """
    ).fetchall()
    conn.close()
    assert [(row["id"], row["week_template_id"], row["start_date"], row["end_date"]) for row in timeline_after] == [
        (2, 16, "2022-04-01", "2025-07-21"),
        (4, 17, "2025-07-22", "2025-08-24"),
        (5, 16, "2025-08-25", "2026-04-04"),
        (3, 17, "2026-04-05", "2026-05-17"),
        (6, 17, "2026-05-18", "2026-05-25"),
        (106, 17, "2026-05-26", "2026-05-30"),
        (114, 39, "2026-05-31", None),
    ]

    client = appmod.app.test_client()
    body = client.get("/api/makros/series?days=all").get_json()
    assert body["ok"] is True

    rows = {row["date_iso"]: row for row in body["data"]}

    debug_conn = sqlite3.connect(db_conn.NUTRITION_DB)
    debug_conn.row_factory = sqlite3.Row
    debug_rows = appmod._debug_week_template_matches(
        appmod._load_week_template_target_timeline(debug_conn),
        ["2025-07-22", "2026-04-05", "2026-05-18", "2026-05-26", "2026-05-31"],
    )
    debug_conn.close()
    assert [row["timeline_id"] for row in debug_rows] == [4, 3, 6, 106, 114]

    assert rows["2024-01-15"]["source"] == "nutrition_plan"
    assert rows["2024-01-15"]["mode"] == "lean_bulk"
    assert rows["2024-01-15"]["target_kcal"] == 3800
    assert rows["2024-01-15"]["template_title"] == "Standardwoche"

    assert rows["2024-01-16"]["source"] == "logged_target"
    assert rows["2024-01-16"]["mode"] == "custom"
    assert rows["2024-01-16"]["target_kcal"] == 2480
    assert rows["2024-01-16"]["template_title"] == "Logged Override"

    assert rows["2025-07-22"]["source"] == "nutrition_plan"
    assert rows["2025-07-22"]["mode"] == "cut"
    assert rows["2025-07-22"]["target_kcal"] == 2650
    assert rows["2025-07-22"]["template_title"] == "Mini-Cut"

    assert rows["2026-04-05"]["source"] == "nutrition_plan"
    assert rows["2026-04-05"]["mode"] == "cut"
    assert rows["2026-04-05"]["target_kcal"] == 2650
    assert rows["2026-04-05"]["template_title"] == "Mini-Cut"

    assert rows["2026-05-18"]["source"] == "nutrition_plan"
    assert rows["2026-05-18"]["mode"] == "cut"
    assert rows["2026-05-18"]["target_kcal"] == 2500
    assert rows["2026-05-18"]["template_title"] == "Mini-Cut"

    assert rows["2026-05-26"]["source"] == "nutrition_plan"
    assert rows["2026-05-26"]["mode"] == "cut"
    assert rows["2026-05-26"]["target_kcal"] == 2500
    assert rows["2026-05-26"]["template_title"] == "Mini-Cut"

    assert rows["2026-05-31"]["source"] == "nutrition_plan"
    assert rows["2026-05-31"]["mode"] == "custom"
    assert rows["2026-05-31"]["target_kcal"] == 2900
    assert rows["2026-05-31"]["template_title"] == "Reverse Erhalt · 2900 Standardwoche V9"

    assert rows["2021-02-02"]["source"] == "dated_mode"
    assert rows["2021-02-02"]["mode"] == "maintenance"
    assert rows["2021-02-02"]["target_kcal"] is None


def test_dashboard_signals_energy_accepts_delta_bounds(monkeypatch, tmp_path):
    appmod, _npdb = _load_app_module(monkeypatch, tmp_path)
    _seed_supporting_dbs()

    bounds = appmod._macro_bounds_for_target(
        {
            "kcal_target": 2800,
            "green_low": -150,
            "green_high": 300,
            "yellow_low": -300,
            "yellow_high": 450,
        },
        2650,
    )

    assert bounds == {
        "green_low": 2500.0,
        "green_high": 2950.0,
        "yellow_low": 2350.0,
        "yellow_high": 3100.0,
    }
    assert appmod._energy_state_and_reason(
        2484.0,
        2650.0,
        bounds["green_low"],
        bounds["green_high"],
        bounds["yellow_low"],
        bounds["yellow_high"],
    ) == ("neutral", "within_yellow")


def test_dashboard_signals_recovery_uses_current_recovery_source_instead_of_legacy_hrv_only(monkeypatch, tmp_path):
    appmod, npdb = _load_app_module(monkeypatch, tmp_path)
    _seed_supporting_dbs()
    npdb.ensure_nutrition_planning_schema()

    today = date.today()
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()

    nutrition_conn = sqlite3.connect(db_conn.NUTRITION_DB)
    nutrition_conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            weight_kg REAL,
            kcal REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            sugar REAL,
            created_at TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    nutrition_conn.commit()
    nutrition_conn.close()

    hrv_conn = sqlite3.connect(db_conn.HRV_DB)
    baseline_rows = []
    for offset in range(1, 29):
        day = today - timedelta(days=offset)
        day_iso = day.isoformat()
        baseline_rows.append(
            (
                f"{day_iso} 06:00:00+0000",
                f"{day_iso} 00:00:00+0000",
                50.0,
                62.0,
                38.0,
                1200.0,
                "HRV4Training",
                now_iso,
            )
        )
    baseline_rows.append(
        (
            f"{today.isoformat()} 06:00:00+0000",
            f"{today.isoformat()} 00:00:00+0000",
            61.0,
            31.0,
            28.0,
            980.0,
            "HRV4Training",
            now_iso,
        )
    )
    hrv_conn.executemany(
        """
        INSERT INTO hrv_measurements
        (ts_measurement, date_utc, hr, rmssd, sdnn, avnn, source_file, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        baseline_rows,
    )
    hrv_conn.commit()
    hrv_conn.close()

    polar_conn = sqlite3.connect(db_conn.POLAR_DB)
    polar_conn.execute(
        """
        INSERT INTO polar_nightly_recharge
        (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            today.isoformat(),
            "{}",
            0.8,
            3,
            78,
            1.2,
            1300.0,
            96.0,
            0.0,
            now_iso,
            now_iso,
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_sleep
        (date, raw_json, sleep_score, sleep_start, sleep_end, sleep_minutes, actual_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes, light_sleep_minutes, awake_minutes, interruptions, interruption_minutes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            today.isoformat(),
            "{}",
            86.0,
            f"{(today - timedelta(days=1)).isoformat()}T21:30:00Z",
            f"{today.isoformat()}T06:15:00Z",
            525.0,
            500.0,
            90.0,
            105.0,
            305.0,
            25.0,
            2.0,
            10.0,
            now_iso,
            now_iso,
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_continuous_samples
        (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            today.isoformat(),
            "{}",
            144.0,
            43.0,
            104.0,
            59.0,
            46.0,
            now_iso,
            now_iso,
        ),
    )
    polar_conn.commit()
    polar_conn.close()

    with appmod.app.test_request_context("/api/dashboard/signals"):
        resp = appmod.api_dashboard_signals()

    payload = resp.get_json()
    assert payload["ok"] is True

    latest = payload["lanes"]["recovery"][-1]
    assert latest["source"] == "polar"
    assert latest["rmssd"] == 96.0
    assert latest["state"] == "good"

    recovery_ctx = payload["context"]["recovery"]
    assert recovery_ctx["today"] != 96.0
    assert recovery_ctx["today"] is not None
    assert recovery_ctx["state"] == "good"


def test_dashboard_signals_debug_mode_still_returns_payload(monkeypatch, tmp_path):
    appmod, npdb = _load_app_module(monkeypatch, tmp_path)
    _seed_supporting_dbs()
    npdb.ensure_nutrition_planning_schema()

    nutrition_conn = sqlite3.connect(db_conn.NUTRITION_DB)
    nutrition_conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            weight_kg REAL,
            kcal REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            sugar REAL,
            created_at TEXT,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    nutrition_conn.commit()
    nutrition_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/dashboard/signals?debug=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert "debug" in payload
    assert "recovery_debug" in payload["debug"]
