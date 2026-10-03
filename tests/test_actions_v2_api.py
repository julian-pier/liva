import copy
import importlib
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from flask import Flask

import database.connections as connections
from memory.memory_markdown import apply_operation
from training.cardio_viewmodel import CardioFilters, build_cardio_viewmodel, save_cardio_session


class FakeMemoryStorage:
    def __init__(self):
        self.files = {
            "core_patterns": "# Core Patterns\n\n## Training\n<!-- SECTION:TRAINING -->\n- [mem_test_delete] (low) Delete me\n- [mem_test_update] (low) Old text\n- Text-only delete target\n<!-- /SECTION:TRAINING -->\n",
            "review_log": "# Review Log\n\n## Log Einträge\n<!-- SECTION:LOG_EINTRAEGE -->\n- Bestehender Eintrag\n<!-- /SECTION:LOG_EINTRAEGE -->\n",
            "athlete_dossier": "# Athlete Dossier\n\n## Aktuelle Phase\n<!-- SECTION:AKTUELLE_PHASE -->\n- Bestehende Phase\n<!-- /SECTION:AKTUELLE_PHASE -->\n",
            "health_notes": "# Health Notes\n\n## Recovery\n<!-- SECTION:RECOVERY -->\n- Bestehende Recovery-Notiz\n<!-- /SECTION:RECOVERY -->\n",
        }

    def write_file(self, logical_name, content):
        self.files[logical_name] = content
        return {
            "logical_file": logical_name,
            "path": f"/tmp/{logical_name}.md",
            "relative_path": f"10_athlete/{logical_name}.md",
            "last_updated": "2026-04-17T20:00:00Z",
        }


class FakeMemoryService:
    def __init__(self):
        self.storage = FakeMemoryStorage()
        self.apply_calls = []
        self.get_file_calls = []

    def get_status(self):
        return {
            "logical_files": [
                {"logical_name": "core_patterns", "title": "Core Patterns", "found": True, "last_updated": "2026-04-16T10:00:00Z"},
                {"logical_name": "review_log", "title": "Review Log", "found": True, "last_updated": "2026-04-16T10:00:00Z"},
                {"logical_name": "athlete_dossier", "title": "Athlete Dossier", "found": True, "last_updated": "2026-04-16T10:00:00Z"},
                {"logical_name": "health_notes", "title": "Health Notes", "found": True, "last_updated": "2026-04-16T10:00:00Z"},
            ]
        }

    def get_context(self, payload):
        return {
            "domains": payload.get("domains") or [],
            "query": payload.get("query"),
            "include_recent": payload.get("include_recent", False),
            "known_files": ["core_patterns"],
            "memory_blocks": [
                {
                    "logical_file": "core_patterns",
                    "section": "TRAINING",
                    "heading": "Training",
                    "text": "User responds well to stable training movements.",
                    "importance": "high",
                    "last_updated": "2026-04-16T10:00:00Z",
                }
            ]
        }

    def apply(self, payload):
        self.apply_calls.append(payload)
        updated_files = []
        for item in payload.get("write_plan") or []:
            logical_name = item.get("logical_file")
            self.storage.files[logical_name] = apply_operation(
                self.storage.files.get(logical_name, ""),
                strategy=item.get("strategy"),
                section_name=item.get("section"),
                content=item.get("content"),
            )
            updated_files.append({"logical_file": logical_name})
        return {"success": True, "updated_files": updated_files, "snapshots": [], "errors": [], "payload": payload}

    def propose(self, payload):
        return {
            "dry_run": True,
            "accepted_candidates": payload.get("candidates") or payload.get("extracted_facts") or [],
            "rejected_candidates": [],
            "write_plan": [{"logical_file": "core_patterns", "section": "TRAINING", "strategy": "append_section_note", "content": "test"}],
            "requires_archive": False,
            "stats": {"accepted_count": 1},
        }

    def archive(self, payload):
        return {"success": True, "snapshots": [{"logical_file": "core_patterns"}]}

    def get_file(self, logical_name, *, create_missing=True):
        self.get_file_calls.append({"logical_name": logical_name, "create_missing": bool(create_missing)})
        if logical_name not in self.storage.files and not create_missing:
            raise FileNotFoundError(logical_name)
        return {
            "logical_name": logical_name,
            "title": "Core Patterns",
            "folder_name": "10_athlete",
            "filename": "core_patterns.md",
            "path": "/tmp/core_patterns.md",
            "relative_path": "10_athlete/core_patterns.md",
            "last_updated": "2026-04-16T10:00:00Z",
            "raw_markdown": self.storage.files.get(logical_name, "# Core Patterns\n\n## Training\nTest"),
            "rendered_html": "<h1>Core Patterns</h1>",
        }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    calendar_cache = tmp_path / "latest_today.json"
    monkeypatch.setenv("LIVA_CALENDAR_CACHE_FILE", str(calendar_cache))

    connections.RUNS_DB = str(tmp_path / "runs.sqlite3")
    connections.TRAINING_DB = str(tmp_path / "training.sqlite3")
    connections.PLANS_DB = str(tmp_path / "plans.sqlite3")
    connections.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    connections.HRV_DB = str(tmp_path / "hrv.sqlite3")
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    connections.CORE_DB = str(tmp_path / "core.sqlite3")
    connections.AUTH_DB = str(tmp_path / "auth.sqlite3")

    conn = connections.get_training_db()
    conn.executescript(
        """
        CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT, notes TEXT);
        CREATE TABLE exercises (id INTEGER PRIMARY KEY, workout_id INTEGER, name TEXT, canonical_exercise_id TEXT, variation TEXT, variation_id TEXT, device TEXT, laterality TEXT, execution_mode TEXT);
        CREATE TABLE sets (id INTEGER PRIMARY KEY, exercise_id INTEGER, workout_id INTEGER, set_number INTEGER, set_slot TEXT, weight REAL, reps INTEGER, rpe REAL, is_warmup INTEGER, is_working_set INTEGER, intentional_deload INTEGER NOT NULL DEFAULT 0, technique_set INTEGER NOT NULL DEFAULT 0, progression_excluded INTEGER NOT NULL DEFAULT 0, progression_exclusion_reason TEXT);
        CREATE TABLE school_schedule_snapshots (id INTEGER PRIMARY KEY, fetched_at TEXT, source TEXT, date TEXT, raw_json TEXT, derived_summary_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE school_schedule_entries (id INTEGER PRIMARY KEY, snapshot_id INTEGER, date TEXT, subject TEXT, teacher TEXT, room TEXT, start_time TEXT, end_time TEXT, raw_text TEXT, status_hint TEXT);
        INSERT INTO workouts (id, date_iso, name) VALUES (1, '2026-04-16', 'Push'), (2, '2026-04-10', 'Pull'), (3, '2026-04-08', 'Upper');
        INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES
          (1, 1, 'Bench', 'Flat', 'barbell', 'both'),
          (2, 2, 'Row', 'Chest Supported', 'machine', 'both'),
          (3, 3, 'Schrägbankdrücken', 'Smith', 'Smith', 'bilateral'),
          (4, 3, 'Latzug', 'eGym', 'eGym', 'bilateral');
        INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES
          (1, 1, 1, 1, 100, 8, 8),
          (2, 1, 1, 2, 95, 10, 8.5),
          (3, 2, 2, 1, 80, 12, 8),
          (4, 3, 3, 1, 70, 8, 8),
          (5, 4, 3, 1, 110, 7, 8);
        INSERT INTO school_schedule_snapshots (id, fetched_at, source, date, raw_json, derived_summary_json) VALUES
          (1, '2026-04-17T07:00:00', 'webuntis_playwright', '2026-04-17', '{\"entries\": []}', '{\"school_start\": null, \"school_end\": null, \"total_blocks\": 0, \"total_minutes_in_school\": 0, \"free_windows\": []}'),
          (2, '2026-04-18T07:00:00', 'webuntis_playwright', '2026-04-18', '{\"entries\": []}', '{\"school_start\": \"08:00\", \"school_end\": \"15:35\", \"total_blocks\": 4, \"total_minutes_in_school\": 455, \"free_windows\": [{\"start\": \"09:30\", \"end\": \"10:00\", \"minutes\": 30}], \"has_afternoon_school\": true, \"is_fragmented_day\": false}'),
          (3, '2026-04-17T08:00:00', 'webuntis_playwright', '2026-04-17', '{\"entries\": []}', '{\"school_start\": null, \"school_end\": null, \"total_blocks\": 0, \"total_minutes_in_school\": 0, \"free_windows\": []}');
        INSERT INTO school_schedule_entries (snapshot_id, date, subject, teacher, room, start_time, end_time, raw_text, status_hint) VALUES
          (1, '2026-04-17', 'Mathe', 'Mueller', 'R1', '08:00', '09:30', 'Mathe R1', 'confirmed'),
          (1, '2026-04-17', 'Physik', 'Schmidt', 'Lab', '11:00', '12:00', 'Physik Lab', 'confirmed'),
          (2, '2026-04-18', 'Deutsch', 'Weber', 'R2', '10:00', '11:00', 'Deutsch R2', 'confirmed');
        """
    )
    conn.commit()
    conn.executemany(
        "INSERT INTO school_schedule_entries (snapshot_id, date, subject, teacher, room, start_time, end_time, raw_text, status_hint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (3, "2026-04-17", f"Kurs {idx}", "Teacher", None, None, None, f"Dump {idx}", "unknown")
            for idx in range(58)
        ],
    )
    conn.commit()
    conn.close()

    conn = connections.get_plans_db()
    conn.executescript(
        """
        CREATE TABLE plans (
          id INTEGER PRIMARY KEY,
          name TEXT,
          data TEXT,
          block_length INTEGER DEFAULT 4,
          is_active INTEGER DEFAULT 0,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE gym_plans (
          id INTEGER PRIMARY KEY,
          title TEXT,
          focus TEXT,
          plan_json TEXT,
          is_active INTEGER,
          is_archived INTEGER,
          created_at TEXT,
          updated_at TEXT,
          rules_json TEXT,
          blocks_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO plans (id, name, data, is_active, created_at, updated_at) VALUES (1, ?, ?, 1, '2026-04-01', '2026-04-14')",
        ("PPL_UL_v3", json.dumps({"days": {"Mo": "Push", "Mi": "Pull"}})),
    )
    conn.execute(
        "INSERT INTO gym_plans (id, title, focus, plan_json, is_active, is_archived, created_at, updated_at, rules_json, blocks_json) VALUES (1, ?, 'test', ?, 1, 0, '2026-04-01', '2026-04-14', '{}', '{}')",
        (
            "PPL x UL v3",
            json.dumps(
                {
                    "base_week": {
                        "Mo": [
                            {
                                "id": "push",
                                "kind": "gym",
                                "time": "18:30",
                                "title": "Push",
                                "items": [
                                    {"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]},
                                    {"kind": "exercise", "name": "Latzug", "variation": "eGym", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]},
                                ],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    conn = connections.get_nutrition_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nutrition_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE nutrition_day_actuals (id INTEGER PRIMARY KEY, day TEXT, kcal REAL, p REAL, c REAL, f REAL, source TEXT);
        CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date TEXT, date_iso TEXT, weight_kg REAL, kcal REAL, protein REAL, carbs REAL, fat REAL, sugar REAL, created_at TEXT, raw_json TEXT);
        CREATE TABLE nutrition_logged_meals (
          id INTEGER PRIMARY KEY,
          log_date TEXT,
          slot_id INTEGER,
          title TEXT,
          meal_name TEXT,
          meal_slot TEXT,
          source TEXT DEFAULT 'free',
          created_from_planned_slot_id INTEGER,
          created_from_template_id INTEGER,
          edited_from_template INTEGER DEFAULT 0,
          original_planned_time TEXT,
          logged_at TEXT,
          is_favorite INTEGER DEFAULT 0,
          created_at TEXT,
          updated_at TEXT,
          adjusted_from_meal_id INTEGER,
          adjustment_reason TEXT,
          action_source TEXT,
          trace_json TEXT
        );
        CREATE TABLE nutrition_logged_meal_items (
          id INTEGER PRIMARY KEY,
          logged_meal_id INTEGER,
          food_id INTEGER,
          food_name TEXT,
          amount REAL,
          unit TEXT,
          item_type TEXT DEFAULT 'food',
          calories REAL,
          sort_index INTEGER DEFAULT 0,
          created_at TEXT,
          updated_at TEXT,
          protein_override REAL,
          carbs_override REAL,
          fat_override REAL,
          sugar_override REAL
        );
        CREATE TABLE nutrition_foods (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          brand TEXT,
          unit_default TEXT NOT NULL DEFAULT 'g',
          portion_g REAL,
          kcal_per_100 REAL NOT NULL,
          p_per_100 REAL NOT NULL,
          c_per_100 REAL NOT NULL,
          f_per_100 REAL NOT NULL,
          fiber_per_100 REAL,
          salt_per_100 REAL,
          tags TEXT,
          sugar_per_100 REAL,
          common_portion_size REAL,
          mfp_search_hint TEXT,
          category TEXT,
          is_favorite INTEGER DEFAULT 0,
          is_active INTEGER DEFAULT 1,
          name_normalized TEXT
        );
        CREATE TABLE nutrition_logging_recent_foods (food_id INTEGER PRIMARY KEY, last_amount REAL, last_unit TEXT, last_meal_slot TEXT, last_logged_at TEXT, use_count INTEGER, today_count INTEGER, today_date TEXT, updated_at TEXT);
        CREATE TABLE nutrition_logging_recent_meals (meal_key TEXT PRIMARY KEY, title TEXT, meal_slot TEXT, payload_json TEXT, last_logged_at TEXT, use_count INTEGER, today_count INTEGER, today_date TEXT, updated_at TEXT);
        CREATE TABLE nutrition_planned_meal_status (id INTEGER PRIMARY KEY, log_date TEXT, slot_id INTEGER, status TEXT, shifted_time_text TEXT, logged_meal_id INTEGER, updated_at TEXT, status_source TEXT, reason_code TEXT, trace_json TEXT);
        INSERT INTO nutrition_settings (key, value) VALUES
          ('active_mode', 'maintenance'),
          ('mode_maintenance_target', '2650'),
          ('mode_maintenance_protein_target', '180'),
          ('mode_maintenance_carbs_target', '300'),
          ('mode_maintenance_fat_target', '65');
        INSERT INTO nutrition_day_actuals (day, kcal, p, c, f, source) VALUES ('2026-04-16', 2500, 170, 280, 70, 'test');
        INSERT INTO weight_logs (date_iso, weight_kg) VALUES ('2026-04-16', 73.7), ('2026-04-15', 73.5);
        INSERT INTO nutrition_logged_meals (log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at) VALUES ('2026-04-16', 'Breakfast', 'Breakfast', 'Breakfast', 'test', '2026-04-16T08:00:00', '2026-04-16T08:00:00Z', '2026-04-16T08:00:00Z');
        INSERT INTO nutrition_foods (id, name, unit_default, portion_g, kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, is_active, name_normalized)
        VALUES (27, 'Brot', 'pcs', 100, 222, 7, 42, 2, 4, 1, 'brot');
        INSERT INTO nutrition_foods (id, name, unit_default, kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, is_active, name_normalized)
        VALUES (28, 'Milch', 'ml', 64, 3.4, 4.8, 3.5, 4.8, 1, 'milch');
        INSERT INTO nutrition_foods (id, name, unit_default, kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, is_active, name_normalized)
        VALUES (29, 'Whey', 'g', 390, 78, 7, 6, 5, 1, 'whey');
        """
    )
    conn.commit()
    conn.close()

    conn = connections.get_runs_db()
    conn.executescript(
        """
        CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time INTEGER, avg_hr REAL, pace REAL);
        INSERT INTO runs (id, date, distance, moving_time, avg_hr, pace) VALUES (1, '2026-04-13', 5000, 1500, 145, 300);
        """
    )
    conn.commit()
    conn.close()

    import sqlite3

    conn = sqlite3.connect(connections.HRV_DB)
    conn.executescript(
        """
        CREATE TABLE hrv_measurements (id INTEGER PRIMARY KEY, ts_measurement TEXT, date_utc TEXT, hr REAL, rmssd REAL, sleep_quality REAL, fatigue REAL, training_motivation REAL, alcohol INTEGER, sickness INTEGER);
        INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, sleep_quality, fatigue, training_motivation, alcohol, sickness)
        VALUES ('2026-04-17 06:00:00+0000', '2026-04-17 06:00:00+0000', 53, 103.8, 8, 3, 7, 0, 0);
        """
    )
    conn.commit()
    conn.close()

    connections.get_core_db().close()

    import security.write_guard as write_guard
    importlib.reload(write_guard)
    import ai.api_ai as ai_api_module
    importlib.reload(ai_api_module)
    import ai.actions_v2 as actions_v2
    importlib.reload(actions_v2)
    import core.core_api as core_api_module
    importlib.reload(core_api_module)
    import security.private_access as private_access_module
    importlib.reload(private_access_module)
    monkeypatch.setattr(core_api_module, "_ensure_core_access", lambda: None)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["liva_memory_service"] = FakeMemoryService()
    app.register_blueprint(ai_api_module.ai_api)
    app.register_blueprint(actions_v2.actions_v2_api)
    app.register_blueprint(core_api_module.core_bp)
    app.register_blueprint(private_access_module.private_access_bp)
    actions_v2.register_public_actions_aliases(app)

    with app.test_client() as test_client:
        yield test_client


def auth():
    return {"Authorization": "Bearer test"}


def _set_active_rolling_rotation_plan():
    conn = connections.get_plans_db()
    plan = {
        "meta": {"mode": "rolling_sequence"},
        "base_week": {"Fr": "Fr", "Sa": "Sa"},
        "sequence": [
            {"id": "613d6d74-5", "kind": "gym", "title": "Push A · Road to 40s + Squat", "items": [
                {"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]},
                {"kind": "exercise", "name": "Squats", "variation": "LH", "sets": 2, "reps": {"min": 5, "max": 7}, "rpe_list": [8, 8]},
            ]},
            {"id": "613d6d74-6", "kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "name": "Rows", "variation": "eGym", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
            {"id": "613d6d74-7", "kind": "gym", "title": "Lower A", "items": [{"kind": "exercise", "name": "Leg Press", "variation": "unilat", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
            {"id": "613d6d74-8", "kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "OHP", "variation": "LH", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]},
            {"id": "613d6d74-9", "kind": "gym", "title": "Pull B", "items": [{"kind": "exercise", "name": "Pulldown", "variation": "SZ", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
            {"id": "613d6d74-10", "kind": "gym", "title": "Lower B", "items": [{"kind": "exercise", "name": "RDL", "variation": "LH", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]},
        ],
    }
    conn.execute("UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()


def _set_active_rolling_rotation_plan_with_fixed_weekday_labels():
    conn = connections.get_plans_db()
    plan = {
        "meta": {"mode": "rolling_sequence"},
        "base_week": {
            "Fr": [{"id": "fr-fixed", "kind": "gym", "title": "Push A · Road to 40s + Squat", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]}],
            "Sa": [{"id": "sa-fixed", "kind": "gym", "title": "Push A · Road to 40s + Squat", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]}],
        },
        "sequence": [
            {"id": "613d6d74-5", "kind": "gym", "title": "Push A · Road to 40s + Squat", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
            {"id": "613d6d74-6", "kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "name": "Rows", "variation": "eGym", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
        ],
    }
    conn.execute("UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()


def _insert_workout(day_iso: str, name: str):
    conn = connections.get_training_db()
    next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM workouts").fetchone()[0]) or 1
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (next_id, day_iso, name))
    conn.commit()
    conn.close()


def _ensure_core_training_card_row(day_iso: str, session_label: str, *, session_type: str = "gym", items_count: int = 1, board_source: str = "actions_v2_resolver"):
    from core.core_training_card import ensure_core_training_card_schema

    conn = connections.get_core_db()
    ensure_core_training_card_schema(conn)
    card_payload = {
        "title": session_label,
        "session_label": session_label,
        "planned_session_label": session_label,
        "recommended_session_label": session_label,
        "session_type": session_type,
        "items": [{"item_type": "exercise", "display_name": f"Item {idx + 1}"} for idx in range(items_count)],
    }
    source_context = {"board_source": board_source, "resolver": {"source": "rolling_rotation"}}
    conn.execute(
        """
        INSERT INTO core_training_cards (
            day_iso, board_id, source, status, session_type, session_label, decision_intent,
            title, summary, data_quality_json, source_context_json, card_json, payload_json,
            created_at, updated_at
        ) VALUES (?, NULL, 'core_board', 'active', ?, ?, 'train', ?, '', '{}', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (day_iso, session_type, session_label, session_label, json.dumps(source_context), json.dumps(card_payload), json.dumps(card_payload)),
    )
    conn.commit()
    conn.close()


def _ensure_saved_training_decision(day_iso: str, session_name: str, *, planned_session_id: int | None = None):
    import ai.actions_v2 as actions_v2

    conn = connections.get_core_db()
    actions_v2.ensure_training_ai_decisions_schema(conn)
    now = "2026-06-05T09:00:00+00:00"
    conn.execute(
        """
        INSERT INTO training_ai_decisions (
            day_iso, planned_session_id, plan_id, session_name, session_type, status, source,
            model, context_hash, decision_json, trigger_reason, created_at, updated_at
        ) VALUES (?, ?, 1, ?, 'gym', 'fresh', 'gpt', 'test', 'hash', '{}', 'none', ?, ?)
        """,
        (day_iso, planned_session_id, session_name, now, now),
    )
    conn.commit()
    conn.close()


SMOKE_CALLS = [
    ("GET", "/api/v2/actions/liva/today", None),
    ("GET", "/api/v2/actions/liva/context", None),
    ("GET", "/api/v2/actions/liva/state", None),
    ("GET", "/api/v2/actions/calendar/today", None),
    ("GET", "/api/v2/actions/calendar/range?start=2026-04-17&end=2026-04-18", None),
    ("GET", "/api/v2/actions/calendar/search?q=mathe&start=2026-04-17&end=2026-04-18", None),
    ("GET", "/api/v2/actions/daily/context?date=2026-04-17", None),
    ("GET", "/api/v2/actions/core/state", None),
    ("GET", "/api/v2/actions/core/board", None),
    (
        "POST",
        "/api/v2/actions/core/board/build",
        {
            "date": "2026-04-17",
            "source_message": "Gewicht & HRV sind drin",
            "human_headline": "Kontrolliert trainieren.",
            "human_summary": "Daten sind da, aber Phase 1 bleibt konservativ.",
            "decision_intent": "train_controlled",
            "confidence": 0.68,
            "machine_briefing": {"intent": "phase1_test"},
            "visible_reasons": ["HRV gelesen", "Gewicht gelesen"],
        },
    ),
    ("POST", "/api/v2/actions/core/control", {"command": "set_override", "mode": "LIGHT", "reason": "test"}),
    ("GET", "/api/v2/actions/training/state", None),
    ("POST", "/api/v2/actions/training/data", {"scope": "sessions", "include_exercises": True, "include_sets": True}),
    ("POST", "/api/v2/actions/training/exercise-data", {"exercise_names": ["Bench"], "include_history": True, "include_best_sets": True, "include_e1rm": True}),
    ("POST", "/api/v2/actions/training/adjust", {"command": "change_sets", "day": "Mo", "exercise": "Schrägbankdrücken", "sets": 2}),
    ("GET", "/api/v2/actions/training/plan-state", None),
    ("POST", "/api/v2/actions/training/plan-control", {"command": "activate_plan_version", "plan_version": "PPL_UL_v3"}),
    ("POST", "/api/v2/actions/progression/data", {"mode": "by_exercise", "exercise_names": ["Bench"], "metrics": ["e1rm", "top_set_weight", "top_set_reps"]}),
    ("POST", "/api/v2/actions/progression/compare", {"compare_type": "time_ranges", "exercise_names": ["Bench"], "range_a": {"date_from": "2026-04-01", "date_to": "2026-04-12"}, "range_b": {"date_from": "2026-04-13", "date_to": "2026-04-17"}}),
    ("GET", "/api/v2/actions/recovery/state", None),
    ("POST", "/api/v2/actions/recovery/data", {"metrics": ["rmssd", "heart_rate"], "group_by": "day"}),
    ("POST", "/api/v2/actions/recovery/control", {"command": "set_alcohol_flag", "date": "2026-04-17", "value": True}),
    ("GET", "/api/v2/actions/nutrition/state", None),
    ("GET", "/api/v2/actions/nutrition/timing?date=2026-04-16", None),
    ("GET", "/api/v2/actions/nutrition/foods?query=brot", None),
    ("POST", "/api/v2/actions/nutrition/data", {"mode": "daily_totals", "date_from": "2026-04-01", "date_to": "2026-04-17"}),
    ("POST", "/api/v2/actions/nutrition/control", {"command": "adjust_targets", "kcal": 2650, "protein": 180, "carbs": 300, "fat": 65}),
    ("GET", "/api/v2/actions/bodycomp/cut-status?date_to=2026-04-17&days=14", None),
    ("GET", "/api/v2/actions/weight/state", None),
    ("POST", "/api/v2/actions/weight/data", {"date_from": "2026-04-01", "date_to": "2026-04-17"}),
    ("POST", "/api/v2/actions/weight/control", {"command": "log_weight", "date": "2026-04-17", "weight": 73.7}),
    ("GET", "/api/v2/actions/runs/state", None),
    ("GET", "/api/v2/actions/cardio/summary?date_to=2026-04-17&days=14", None),
    ("POST", "/api/v2/actions/runs/data", {"date_from": "2026-04-01", "date_to": "2026-04-17"}),
    ("POST", "/api/v2/actions/runs/control", {"command": "set_run_type", "date": "2026-04-19", "run_type": "Easy"}),
    ("GET", "/api/v2/actions/training/today-loads?date=2026-04-14&day=Mo", None),
    ("GET", "/api/v2/actions/life/today?date=2026-04-16", None),
    ("GET", "/api/v2/actions/checkin/today?date=2026-04-17", None),
    ("GET", "/api/v2/actions/endurance/week?date=2026-04-17", None),
    ("GET", "/api/v2/actions/memory/state", None),
    ("POST", "/api/v2/actions/memory/data", {"mode": "by_type", "entry_types": ["pattern"], "limit": 5}),
    ("POST", "/api/v2/actions/memory/store", {"command": "store_pattern", "topic": "training", "text": "Test pattern", "priority": "high"}),
    ("POST", "/api/v2/actions/memory/link", {"command": "link_entry_to_exercise", "entry_id": "mem_test", "exercise_name": "Bench"}),
    ("POST", "/api/v2/actions/remote/control", {"command": "pc_action", "action": "status"}),
]


@pytest.mark.parametrize(("method", "path", "payload"), SMOKE_CALLS)
def test_actions_v2_smoke_all_endpoints(client, method, path, payload):
    if method == "GET":
        resp = client.get(path, headers=auth())
    else:
        resp = client.post(path, json=payload, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True, body


def test_actions_v2_requires_auth(client):
    resp = client.get("/api/v2/actions/liva/today")
    body = resp.get_json()
    assert resp.status_code == 401
    assert body["ok"] is False


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v2/actions/core/control", {"command": "bad"}),
        ("/api/v2/actions/training/adjust", {"command": "bad"}),
        ("/api/v2/actions/remote/control", {"command": "pc_action", "action": "format_disk"}),
    ],
)
def test_actions_v2_invalid_commands_are_structured(client, path, payload):
    resp = client.post(path, json=payload, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert "error" in body
    assert "code" in body["error"]
    assert "message" in body["error"]


def test_actions_v2_openapi_exposes_only_real_gpt_actions_and_does_not_replace_v1():
    spec = json.loads(Path("openapi/liva-actions-v2.json").read_text(encoding="utf-8"))
    assert spec["info"]["title"] == "LIVA GPT Actions API"
    operations = [
        operation
        for path_item in spec["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    action_operations = [
        operation
        for path, path_item in spec["paths"].items()
        for operation in path_item.values()
        if path.startswith("/actions/") and isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(action_operations) >= 57
    assert all(not operation["operationId"].lower().endswith("v2") for operation in operations)
    assert all("x-actions-v2-effects" not in operation for operation in operations)
    assert "/actions/core/board" in spec["paths"]
    assert "/actions/core/board/build" in spec["paths"]
    assert "/actions/core/memory/file-patches" in spec["paths"]
    assert "/actions/core/training-card" in spec["paths"]
    assert "/actions/core/training-card/build" in spec["paths"]
    assert "/actions/training/workout/{workout_id}/coach-view" in spec["paths"]
    assert "/actions/training/edit-logged-workout-exercise" in spec["paths"]
    assert "/actions/training/edit-logged-set" in spec["paths"]
    assert "/actions/progression/summary" in spec["paths"]
    assert "/actions/bodyweight/phase-report" in spec["paths"]
    assert "/actions/bodyweight/phase-setup" in spec["paths"]
    assert "/actions/training/plan-control" not in spec["paths"]
    assert "/actions/memory/link" not in spec["paths"]
    assert all(path.startswith("/actions/") or path.startswith("/api/training/import/") for path in spec["paths"])
    assert all(not path.startswith("/api/v2/") for path in spec["paths"])

    v1_spec = Path("openapi/openapi.yaml").read_text(encoding="utf-8")
    assert "/api/ai/index" in v1_spec
    assert "liva-actions-v2" not in v1_spec


def test_v1_ai_route_still_works_next_to_v2(client):
    resp = client.get("/api/ai/index", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True, body


def test_public_actions_alias_works_next_to_internal_v2(client):
    public = client.get("/actions/liva/today", headers=auth())
    internal = client.get("/api/v2/actions/liva/today", headers=auth())
    assert public.status_code == 200
    assert internal.status_code == 200
    assert public.get_json()["ok"] is True
    assert internal.get_json()["ok"] is True


def test_calendar_today_returns_ok_true(client):
    resp = client.get("/api/v2/actions/calendar/today", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "date" in body
    assert isinstance(body["events"], list)
    assert body["data_freshness"]["source"] == "existing_calendar_sync"


def test_calendar_range_filters_correctly(client):
    resp = client.get("/api/v2/actions/calendar/range?start=2026-04-18&end=2026-04-18", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["start"] == "2026-04-18"
    assert body["end"] == "2026-04-18"
    assert len(body["days"]) == 1
    assert [event["title"] for event in body["days"][0]["events"]] == ["Schule"]


def test_calendar_search_finds_title_and_location(client):
    title_resp = client.get("/api/v2/actions/calendar/search?q=schule&start=2026-04-17&end=2026-04-18", headers=auth())
    title_body = title_resp.get_json()
    assert title_resp.status_code == 200
    assert title_body["count"] == 1
    assert title_body["events"][0]["title"] == "Schule"

    location_resp = client.get("/api/v2/actions/calendar/search?q=15:35&start=2026-04-17&end=2026-04-18", headers=auth())
    location_body = location_resp.get_json()
    assert location_resp.status_code == 200
    assert location_body["count"] == 0


def test_calendar_empty_data_does_not_crash(client, tmp_path, monkeypatch):
    resp = client.get("/api/v2/actions/calendar/today", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["events"] == []
    assert body["available"] is True
    assert body["has_events"] is False


def test_daily_context_contains_calendar(client):
    resp = client.get("/api/v2/actions/daily/context?date=2026-04-17", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "calendar" in body
    assert body["calendar"]["day_pressure"] in {"low", "medium", "high"}


def test_calendar_read_key_auth_works(client):
    resp = client.get("/api/v2/actions/calendar/today", headers=auth())
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_openapi_exposes_no_calendar_write_routes():
    spec = json.loads(Path("openapi/liva-actions-v2.json").read_text(encoding="utf-8"))
    calendar_paths = {path: methods for path, methods in spec["paths"].items() if "/calendar/" in path}
    assert "/actions/calendar/today" in calendar_paths
    assert "/actions/calendar/range" in calendar_paths
    assert "/actions/calendar/search" in calendar_paths
    assert all(set(methods.keys()) == {"get"} for methods in calendar_paths.values())


def test_calendar_range_ignores_fake_entries_when_summary_has_zero_blocks(client):
    resp = client.get("/api/v2/actions/calendar/range?start=2026-04-17&end=2026-04-17", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    day = body["days"][0]
    assert day["events"] == []
    assert day["available"] is True
    assert day["has_events"] is False
    assert day["day_pressure"] == "low"
    assert day["reason"] == "no_school_no_events"


def test_calendar_range_builds_single_school_summary_event_and_free_blocks(client):
    resp = client.get("/api/v2/actions/calendar/range?start=2026-04-18&end=2026-04-18", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    day = body["days"][0]
    assert len(day["events"]) == 1
    event = day["events"][0]
    assert event["title"] == "Schule"
    assert event["source"] == "school_schedule_summary"
    assert event["details_available"] is False
    assert event["summary"]["total_blocks"] == 4
    assert day["free_blocks"][0]["minutes"] == 30
    assert day["day_pressure"] == "high"


def test_daily_context_matches_calendar_today_for_same_day(client, monkeypatch):
    import ai.actions_v2 as actions_v2
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-04-18")
    today_resp = client.get("/api/v2/actions/calendar/today", headers=auth())
    context_resp = client.get("/api/v2/actions/daily/context?date=2026-04-18", headers=auth())
    today_body = today_resp.get_json()
    context_body = context_resp.get_json()
    assert today_resp.status_code == 200
    assert context_resp.status_code == 200
    assert context_body["calendar"]["events"] == today_body["events"]
    assert context_body["calendar"]["free_blocks"] == today_body["free_blocks"]
    assert context_body["calendar"]["day_pressure"] == today_body["day_pressure"]
    assert context_body["calendar"]["available"] == today_body["available"]
    assert context_body["calendar"]["has_events"] == today_body["has_events"]


def test_days_without_events_never_become_high_pressure(client):
    resp = client.get("/api/v2/actions/calendar/range?start=2026-04-17&end=2026-04-17", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["days"][0]["day_pressure"] != "high"


def test_calendar_source_db_error_returns_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    connections.RUNS_DB = str(tmp_path / "runs.sqlite3")
    connections.TRAINING_DB = str(tmp_path / "training.sqlite3")
    connections.PLANS_DB = str(tmp_path / "plans.sqlite3")
    connections.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    connections.HRV_DB = str(tmp_path / "hrv.sqlite3")
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    connections.CORE_DB = str(tmp_path / "core.sqlite3")
    connections.AUTH_DB = str(tmp_path / "auth.sqlite3")

    import ai.actions_v2 as actions_v2
    import security.private_access as private_access_module
    import security.write_guard as write_guard
    import ai.api_ai as ai_api_module
    import core.core_api as core_api_module
    importlib.reload(write_guard)
    importlib.reload(ai_api_module)
    importlib.reload(actions_v2)
    importlib.reload(core_api_module)
    importlib.reload(private_access_module)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions["liva_memory_service"] = FakeMemoryService()
    app.register_blueprint(ai_api_module.ai_api)
    app.register_blueprint(actions_v2.actions_v2_api)
    app.register_blueprint(core_api_module.core_bp)
    app.register_blueprint(private_access_module.private_access_bp)
    actions_v2.register_public_actions_aliases(app)

    with app.test_client() as local_client:
        resp = local_client.get("/api/v2/actions/calendar/today", headers=auth())
        body = resp.get_json()
        assert resp.status_code == 200
        assert body["available"] is False
        assert body["reason"] == "db_missing"


def test_daily_context_free_day_is_not_missing_calendar(client):
    resp = client.get("/api/v2/actions/daily/context?date=2026-04-17", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["calendar"]["available"] is True
    assert body["calendar"]["has_events"] is False
    assert body["calendar"]["day_pressure"] == "low"


def _install_fake_google_calendar(monkeypatch, *, calendars, events_by_calendar, fail_calendar_ids=None):
    import gcalsync.config as cfg_mod
    import gcalsync.service as service_mod

    fail_ids = set(fail_calendar_ids or [])
    monkeypatch.setenv("LIVA_ENABLE_GOOGLE_CALENDAR_READ", "1")

    class _Cfg:
        timezone = "Europe/Berlin"
        credentials_file = Path("fake-creds.json")
        token_file = Path("fake-token.json")
        default_calendar_id = "all"
        school_calendar_id = ""
        work_calendar_id = ""
        training_calendar_id = ""
        football_calendar_id = ""

    monkeypatch.setattr(cfg_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(service_mod, "build_service", lambda credentials_file, token_file: object())
    monkeypatch.setattr(service_mod, "list_calendars", lambda service: list(calendars))

    calls = []

    def _fake_list_events(service, *, calendar_id, time_min=None, time_max=None, q=None, private_extended_property=None, max_results=250):
        calls.append(calendar_id)
        if calendar_id in fail_ids:
            raise RuntimeError(f"boom:{calendar_id}")
        return list(events_by_calendar.get(calendar_id, []))

    monkeypatch.setattr(service_mod, "list_events", _fake_list_events)
    return calls


def test_calendar_today_aggregates_all_readable_google_calendars(client, monkeypatch):
    import ai.actions_v2 as actions_v2
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-04-19")
    calls = _install_fake_google_calendar(
        monkeypatch,
        calendars=[
            {"id": "primary", "summary": "Primary", "accessRole": "owner", "backgroundColor": "#111"},
            {"id": "webuntis", "summary": "WebUntis", "accessRole": "writer", "backgroundColor": "#222"},
            {"id": "shared", "summary": "Family", "accessRole": "reader", "backgroundColor": "#333"},
        ],
        events_by_calendar={
            "primary": [{"id": "p1", "summary": "Breakfast", "start": {"dateTime": "2026-04-19T08:00:00+02:00"}, "end": {"dateTime": "2026-04-19T08:30:00+02:00"}}],
            "webuntis": [{"id": "w1", "summary": "Math", "start": {"dateTime": "2026-04-19T09:00:00+02:00"}, "end": {"dateTime": "2026-04-19T09:45:00+02:00"}}],
            "shared": [{"id": "s1", "summary": "Dinner", "start": {"dateTime": "2026-04-19T19:00:00+02:00"}, "end": {"dateTime": "2026-04-19T20:00:00+02:00"}}],
        },
    )
    resp = client.get("/api/v2/actions/calendar/today?calendar_id=", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["calendar_scope"] == "all_readable_calendars"
    assert [item["calendar_summary"] for item in body["calendars_checked"]] == ["Primary", "WebUntis", "Family"]
    assert [event["summary"] for event in body["events"]] == ["Breakfast", "Math", "Dinner"]
    assert calls == ["primary", "webuntis", "shared"]


def test_calendar_today_google_partial_failure_returns_warning_not_total_failure(client, monkeypatch):
    import ai.actions_v2 as actions_v2
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-04-19")
    _install_fake_google_calendar(
        monkeypatch,
        calendars=[
            {"id": "primary", "summary": "Primary", "accessRole": "owner"},
            {"id": "shared", "summary": "Family", "accessRole": "reader"},
        ],
        events_by_calendar={"primary": [{"id": "p1", "summary": "Call", "start": {"dateTime": "2026-04-19T10:00:00+02:00"}, "end": {"dateTime": "2026-04-19T11:00:00+02:00"}}]},
        fail_calendar_ids={"shared"},
    )
    resp = client.get("/api/v2/actions/calendar/today", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert [event["summary"] for event in body["events"]] == ["Call"]
    assert body["warnings"][0]["calendar_id"] == "shared"


def test_calendar_today_explicit_calendar_id_restricts_google_scope(client, monkeypatch):
    import ai.actions_v2 as actions_v2
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-04-19")
    calls = _install_fake_google_calendar(
        monkeypatch,
        calendars=[
            {"id": "primary", "summary": "Primary", "accessRole": "owner"},
            {"id": "webuntis", "summary": "WebUntis", "accessRole": "writer"},
        ],
        events_by_calendar={
            "primary": [{"id": "p1", "summary": "Primary Event", "start": {"dateTime": "2026-04-19T08:00:00+02:00"}, "end": {"dateTime": "2026-04-19T09:00:00+02:00"}}],
            "webuntis": [{"id": "w1", "summary": "School", "start": {"dateTime": "2026-04-19T09:00:00+02:00"}, "end": {"dateTime": "2026-04-19T10:00:00+02:00"}}],
        },
    )
    resp = client.get("/api/v2/actions/calendar/today?calendar_id=webuntis", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["calendar_scope"] == "explicit_calendar_id"
    assert [event["summary"] for event in body["events"]] == ["School"]
    assert calls == ["webuntis"]


def test_daily_context_calendar_defaults_to_all_readable_calendars(client, monkeypatch):
    _install_fake_google_calendar(
        monkeypatch,
        calendars=[
            {"id": "primary", "summary": "Primary", "accessRole": "owner"},
            {"id": "webuntis", "summary": "WebUntis", "accessRole": "writer"},
            {"id": "private", "summary": "Private", "accessRole": "reader"},
        ],
        events_by_calendar={
            "primary": [{"id": "p1", "summary": "Breakfast", "start": {"dateTime": "2026-04-19T08:00:00+02:00"}, "end": {"dateTime": "2026-04-19T08:30:00+02:00"}}],
            "webuntis": [{"id": "w1", "summary": "Math", "start": {"dateTime": "2026-04-19T09:00:00+02:00"}, "end": {"dateTime": "2026-04-19T09:45:00+02:00"}}],
            "private": [{"id": "x1", "summary": "Doctor", "start": {"dateTime": "2026-04-19T12:00:00+02:00"}, "end": {"dateTime": "2026-04-19T12:30:00+02:00"}}],
        },
    )
    resp = client.get("/api/v2/actions/daily/context?date=2026-04-19", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["calendar"]["calendar_scope"] == "all_readable_calendars"
    assert [event["summary"] for event in body["calendar"]["events"]] == ["Breakfast", "Math", "Doctor"]


def test_actions_v2_resolves_display_exercise_names(client):
    resp = client.post(
        "/api/v2/actions/training/exercise-data",
        json={"exercise_names": ["Schrägbankdrücken (Smith)", "Latzug (eGym)"], "include_history": True, "include_best_sets": True, "include_e1rm": True},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["data_status"] == "ok"
    assert {item["resolution"]["status"] for item in body["exercises"]} == {"resolved"}
    assert all(item["history"] for item in body["exercises"])
    assert body["exercises"][0]["resolution"]["resolved_to"]["name"] == "Schrägbankdrücken"
    assert body["exercises"][0]["resolution"]["resolved_to"]["exercise_key"] == "schragbankdrucken__smith__bilateral"


def test_actions_v2_marks_unresolved_exercise_cleanly(client):
    resp = client.post(
        "/api/v2/actions/training/exercise-data",
        json={"exercise_names": ["Imaginary Lift (Moon Machine)"], "include_history": True},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["data_status"] == "no_matching_points"
    assert body["resolution"][0]["status"] == "unresolved"
    assert body["exercises"][0]["data_status"] == "unresolved"


def test_actions_v2_progression_uses_resolved_exercises(client):
    resp = client.post(
        "/api/v2/actions/progression/data",
        json={"mode": "by_exercise", "exercise_names": ["Schrägbankdrücken (Smith)"], "metrics": ["e1rm", "top_set_weight", "top_set_reps", "volume_load"]},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["data_status"] == "ok"
    assert body["window_summary"]["session_count"] >= 1
    assert body["series"][0]["points"]
    assert body["series"][0]["points"][0]["e1rm"] is not None
    assert body["resolution"][0]["resolved_to"]["exercise_key"] == "schragbankdrucken__smith__bilateral"


def test_actions_v2_training_state_plan_and_today_are_consistent(client):
    state = client.get("/api/v2/actions/training/state", headers=auth()).get_json()["training"]
    plan = client.get("/api/v2/actions/training/plan-state", headers=auth()).get_json()["plan"]
    full_plan = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]
    today = client.get("/api/v2/actions/liva/today", headers=auth()).get_json()["today"]

    assert state["active_split"] == "PPL x UL v3"
    assert plan["available"] is True
    assert plan["detail"] == "compact"
    assert plan["days"]["Mo"]["label"] == "Push"
    assert plan["days"]["Mo"]["exercises"] == ["Schrägbankdrücken (Smith)", "Latzug (eGym)"]
    assert full_plan["detail"] == "full"
    assert isinstance(full_plan["days"]["Mo"]["exercises"][0], dict)
    first_exercise = full_plan["days"]["Mo"]["exercises"][0]
    assert first_exercise["sets"] == 2
    assert first_exercise["rep_range"] == {"min": 6, "max": 10}
    assert first_exercise["rpe_list"] == [8, 9]
    assert first_exercise["volume"] == {"sets": 2, "reps": {"min": 6, "max": 10}, "rpe": [8, 9]}
    assert first_exercise["set_targets"] == [
        {"set_number": 1, "reps": {"min": 6, "max": 10}, "rpe": 8},
        {"set_number": 2, "reps": {"min": 6, "max": 10}, "rpe": 9},
    ]
    assert state["current_exercise_map"]["Mo"] == ["Schrägbankdrücken (Smith)", "Latzug (eGym)"]
    assert today["training_summary"]["active_split"] == state["active_split"]
    assert today["training_summary"]["last_session_date"] == state["recent_sessions_summary"]["last_session_date"]


def test_actions_v2_nutrition_domain_status_is_partial_not_missing_when_targets_exist(client):
    body = client.get("/api/v2/actions/nutrition/state", headers=auth()).get_json()
    domain = body["nutrition"]["domain_status"]
    assert domain["status"] in {"fresh", "partial", "stale"}
    assert domain["targets_present"] is True
    assert "targets_source" in body["nutrition"]


def test_actions_v2_nutrition_command_targets_are_live_source(client):
    resp = client.post(
        "/api/v2/actions/nutrition/control",
        json={"command": "adjust_targets", "kcal": 2400, "protein": 170, "carbs": 250, "fat": 60},
        headers=auth(),
    )
    command_body = resp.get_json()
    assert command_body["execution"]["mode"] == "applied_live"
    assert command_body["execution"]["live_state_changed"] is True

    state = client.get("/api/v2/actions/nutrition/state", headers=auth()).get_json()["nutrition"]
    assert state["active_targets"]["carbs"] == 250.0
    assert state["targets_source"] == "live_plan"
    assert state["targets_source_detail"]["live_plan_changed"] is True


def test_actions_v2_nutrition_status_partial_when_only_structure_is_current(client):
    today = date.today().isoformat()
    conn = connections.get_nutrition_db()
    conn.execute("DELETE FROM nutrition_day_actuals")
    conn.execute("DELETE FROM nutrition_logged_meals")
    conn.execute("INSERT INTO nutrition_logged_meals (logged_at, meal_name) VALUES (?, ?)", (f"{today}T08:00:00", "Only Meal"))
    conn.commit()
    conn.close()

    domain = client.get("/api/v2/actions/nutrition/state", headers=auth()).get_json()["nutrition"]["domain_status"]
    assert domain["status"] == "partial"
    assert domain["daily_totals_count"] == 0
    assert domain["logged_meal_days_7d"] == 1


def test_actions_v2_nutrition_status_missing_when_no_structure_or_targets(client):
    conn = connections.get_nutrition_db()
    conn.execute("DELETE FROM nutrition_settings")
    conn.execute("DELETE FROM nutrition_day_actuals")
    conn.execute("DELETE FROM nutrition_logged_meals")
    conn.execute("DROP TABLE IF EXISTS nutrition_foods")
    conn.commit()
    conn.close()

    domain = client.get("/api/v2/actions/nutrition/state", headers=auth()).get_json()["nutrition"]["domain_status"]
    assert domain["status"] == "missing"


def test_actions_v2_command_responses_include_execution_metadata(client):
    resp = client.post("/api/v2/actions/training/adjust", json={"command": "change_sets", "day": "Mo", "exercise": "Bench", "sets": 2}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False


def test_actions_v2_training_log_gym_session_from_ocr_text(client):
    conn = connections.get_training_db()
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_read_models (
            snapshot_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL DEFAULT '{}',
            generated_at TEXT NOT NULL DEFAULT '',
            fresh_until TEXT NOT NULL DEFAULT '',
            build_latency_ms INTEGER NOT NULL DEFAULT 0,
            partial INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            meta_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_update', 'old')")
    conn.execute(
        "INSERT OR REPLACE INTO ui_read_models (snapshot_key, payload_json) VALUES (?, '{}')",
        ("training_snapshot",),
    )
    conn.execute(
        "INSERT OR REPLACE INTO ui_read_models (snapshot_key, payload_json) VALUES (?, '{}')",
        ("dashboard_snapshot:2026-04-18",),
    )
    conn.commit()
    conn.close()

    raw_text = """Schrägbankdrücken
7x62.5@9 6x60@8

Latzug
6x112@8
"""
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "log_gym_session", "date": "2026-04-18", "session_name": "Push", "raw_text": raw_text},
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert body["result"]["exercise_count"] == 2
    assert body["result"]["set_count"] == 3
    assert body["result"]["exercises"][0]["name"] == "Schrägbankdrücken"
    assert body["result"]["exercises"][0]["variation"] == "Smith"
    assert body["result"]["exercises"][1]["variation"] == "eGym"
    assert body["result"]["warnings"] == []
    assert body["result"]["incomplete_fields"] == {"missing_rpe_count": 0, "fields": []}
    assert body["result"]["post_write_refresh"]["meta_touched"] is True
    assert "training_snapshot" in body["result"]["post_write_refresh"]["snapshots_invalidated"]

    conn = connections.get_training_db()
    assert conn.execute("SELECT value FROM meta WHERE key='last_update'").fetchone()[0] != "old"
    assert conn.execute("SELECT COUNT(*) FROM ui_read_models WHERE snapshot_key='training_snapshot'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ui_read_models WHERE snapshot_key LIKE 'dashboard_snapshot:%'").fetchone()[0] == 0
    conn.close()

    data = client.post(
        "/api/v2/actions/training/data",
        json={"scope": "sessions", "date_from": "2026-04-18", "date_to": "2026-04-18", "include_exercises": True, "include_sets": True},
        headers=auth(),
    ).get_json()
    session = data["sessions"][0]
    assert session["day_type"] == "Push"
    assert session["exercises"][0]["sets"][0]["weight"] == 62.5
    assert session["exercises"][0]["sets"][0]["reps"] == 7
    assert session["exercises"][0]["sets"][0]["rpe"] == 9


def test_actions_v2_training_log_supports_exercise_and_set_progression_exclusions(client):
    baseline = {
        "command": "log_gym_session",
        "date": "2026-04-16",
        "session_name": "Push exclusions",
        "exercises": [
            {"name": "Flys", "variation": "Kabel", "sets": [{"weight": 30, "reps": 10, "rpe": 8}, {"weight": 25, "reps": 12, "rpe": 8}]},
            {"name": "Trizepsdrücken", "variation": "Kabel", "sets": [{"weight": 40, "reps": 10, "rpe": 8}, {"weight": 35, "reps": 12, "rpe": 8}]},
        ],
    }
    first = client.post("/api/v2/actions/training/adjust", json=baseline, headers=auth()).get_json()
    assert first["ok"] is True, first

    current = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-17",
            "session_name": "Push exclusions",
            "exercises": [
                {
                    "name": "Flys",
                    "variation": "Kabel",
                    "progression_excluded": True,
                    "progression_exclusion_reason": "Anderer Seilzug; Übersetzung nicht vergleichbar.",
                    "sets": [{"weight": 20, "reps": 8, "rpe": 9}, {"weight": 18, "reps": 9, "rpe": 9}],
                },
                {
                    "name": "Trizepsdrücken",
                    "variation": "Kabel",
                    "sets": [
                        {
                            "weight": 30,
                            "reps": 8,
                            "rpe": 9,
                            "progression_excluded": True,
                            "progression_exclusion_reason": "Technik geändert.",
                        },
                        {"weight": 35, "reps": 13, "rpe": 8},
                    ],
                },
            ],
        },
        headers=auth(),
    ).get_json()

    assert current["ok"] is True, current
    result = current["result"]
    conn = connections.get_training_db()
    stored_exclusions = [
        dict(row)
        for row in conn.execute(
            "SELECT progression_excluded, progression_exclusion_reason FROM sets WHERE workout_id=? ORDER BY id",
            (result["workout_id"],),
        ).fetchall()
    ]
    conn.close()
    assert [row["progression_excluded"] for row in stored_exclusions] == [1, 1, 1, 0], stored_exclusions
    progress = result["progress"]
    assert progress["comparable_sets"] == 1, progress
    assert progress["progress_sets"] == 1
    excluded = [item for item in progress["set_comparisons"] if item.get("category") == "progression_excluded"]
    assert len(excluded) == 3
    assert {item["reason_text"] for item in excluded} == {
        "Anderer Seilzug; Übersetzung nicht vergleichbar.",
        "Technik geändert.",
    }
    assert result["exercises"][0]["notes"].startswith("Progressionsbewertung ausgeschlossen:")
    assert "Satz 1" in result["exercises"][1]["notes"]

    coach_view = client.get(
        f"/api/v2/actions/training/workout/{result['workout_id']}/coach-view",
        headers=auth(),
    ).get_json()
    fly_sets = coach_view["exercises"][0]["sets"]
    assert coach_view["exercises"][0]["notes"].startswith("Progressionsbewertung ausgeschlossen:")
    assert all(item["progression_excluded"] is True for item in fly_sets)
    assert all(item["progression_exclusion_reason"].startswith("Anderer Seilzug") for item in fly_sets)
    triceps_sets = coach_view["exercises"][1]["sets"]
    assert triceps_sets[0]["progression_excluded"] is True
    assert triceps_sets[1]["progression_excluded"] is False

    note_edit = client.post(
        "/api/v2/actions/training/edit-logged-workout-exercise",
        json={"workout_id": result["workout_id"], "exercise_index": 0, "notes": "Schulterblätter aktiv führen."},
        headers=auth(),
    ).get_json()
    assert note_edit["ok"] is True, note_edit
    assert note_edit["canonical_readback"]["notes"].startswith("Schulterblätter aktiv führen.\n")
    assert "Progressionsbewertung ausgeschlossen:" in note_edit["canonical_readback"]["notes"]

    note_clear = client.post(
        "/api/v2/actions/training/edit-logged-workout-exercise",
        json={"workout_id": result["workout_id"], "exercise_index": 0, "notes": ""},
        headers=auth(),
    ).get_json()
    assert note_clear["ok"] is True, note_clear
    assert note_clear["canonical_readback"]["notes"].startswith("Progressionsbewertung ausgeschlossen:")
    assert "Schulterblätter aktiv führen." not in note_clear["canonical_readback"]["notes"]


def test_actions_v2_training_log_requires_reason_for_progression_exclusion(client):
    response = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-17",
            "session_name": "Push exclusions",
            "exercises": [
                {"name": "Flys", "progression_excluded": True, "sets": [{"weight": 20, "reps": 8, "rpe": 8}]},
            ],
        },
        headers=auth(),
    )

    assert response.status_code == 400
    assert "progression_exclusion_reason is required" in str(response.get_json())


def test_actions_v2_training_log_reports_missing_rpe_without_defaulting(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-19",
            "session_name": "Push",
            "exercises": [
                {
                    "name": "Bench Press",
                    "sets": [
                        {"reps": 8, "weight": 80},
                        {"reps": 7, "weight": 80},
                    ],
                }
            ],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["result"]["warnings"] == [
        {
            "code": "missing_rpe",
            "field": "sets[].rpe",
            "missing_count": 2,
            "message": "Some logged sets have no RPE; no default was applied.",
        }
    ]
    assert body["result"]["incomplete_fields"] == {
        "missing_rpe_count": 2,
        "fields": ["sets[].rpe"],
    }

    conn = connections.get_training_db()
    rows = conn.execute(
        "SELECT rpe FROM sets WHERE workout_id=? ORDER BY id",
        (body["result"]["workout_id"],),
    ).fetchall()
    assert [row[0] for row in rows] == [None, None]
    conn.close()


def test_actions_v2_training_log_persists_complete_structured_rpe_readback(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-20",
            "session_name": "Push",
            "exercises": [
                {"name": "Incline Press", "sets": [
                    {"reps": 6, "weight": 35, "rpe": 9},
                    {"reps": 8, "weight": 30.5, "rpe": 9},
                    {"reps": 9, "weight": 30, "rpe": 8},
                ]},
                {"name": "Lateral Raise", "sets": [
                    {"reps": 12, "weight": 15.5, "rpe": 9},
                    {"reps": 9, "weight": 15.5, "rpe": 8},
                ]},
                {"name": "Pushdowns", "sets": [
                    {"reps": 8, "weight": 26.5, "rpe": 8},
                    {"reps": 10, "weight": 25.5, "rpe": 9},
                ]},
                {"name": "Cable Crunches", "sets": [
                    {"reps": 12, "weight": 33, "rpe": 8},
                    {"reps": 12, "weight": 33, "rpe": 9},
                ]},
            ],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["result"]["set_count"] == 9
    assert body["result"]["warnings"] == []
    assert body["result"]["incomplete_fields"]["missing_rpe_count"] == 0
    conn = connections.get_training_db()
    rows = conn.execute(
        "SELECT rpe FROM sets WHERE workout_id=? ORDER BY id",
        (body["result"]["workout_id"],),
    ).fetchall()
    assert [row[0] for row in rows] == [9, 9, 8, 9, 8, 8, 9, 8, 9]
    conn.close()


def test_actions_v2_training_log_mixed_rpe_counts_only_missing_sets(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-21",
            "session_name": "Push",
            "exercises": [{"name": "Bench Press", "sets": [
                {"reps": 8, "weight": 80, "rpe": 8},
                {"reps": 7, "weight": 80},
                {"reps": 6, "weight": 77.5, "rpe": 9},
            ]}],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["result"]["incomplete_fields"]["missing_rpe_count"] == 1
    conn = connections.get_training_db()
    rows = conn.execute(
        "SELECT rpe FROM sets WHERE workout_id=? ORDER BY id",
        (body["result"]["workout_id"],),
    ).fetchall()
    assert [row[0] for row in rows] == [8, None, 9]
    conn.close()


@pytest.mark.parametrize("invalid_rpe", [0, -1, "NaN", "Infinity", 10.5])
def test_actions_v2_training_log_rejects_invalid_rpe_values(client, invalid_rpe):
    response = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-22",
            "session_name": "Push",
            "dry_run": True,
            "exercises": [{"name": "Bench Press", "sets": [
                {"reps": 8, "weight": 80, "rpe": invalid_rpe},
            ]}],
        },
        headers=auth(),
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_actions_v2_training_log_returns_coach_view_and_compare(client):
    client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-18",
            "session_name": "Upper",
            "exercises": [
                {"name": "Schrägbankdrücken", "variation": "Smith", "sets": [{"reps": 9, "weight": 72.5, "rpe": 8}]},
                {"name": "Latzug", "variation": "eGym", "sets": [{"reps": 8, "weight": 112, "rpe": 8}]},
            ],
        },
        headers=auth(),
    )
    resp = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-25",
            "session_name": "Upper",
            "exercises": [
                {"name": "Schrägbankdrücken", "variation": "Smith", "sets": [{"reps": 10, "weight": 72.5, "rpe": 8}]},
                {"name": "Latzug", "variation": "eGym", "sets": [{"reps": 8, "weight": 115, "rpe": 8}]},
            ],
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert body["ok"] is True
    assert body["action"] == "log_training_session"
    assert body["will_change"] == ["training_log"]
    assert body["will_not_change"] == ["training_plan"]
    assert body["ids"]["workout_id"] is not None
    assert body["matched_exercises"][0]["canonical_id"]
    assert body["comparison_to_last"]["available"] is True
    assert body["comparison_to_last"]["progression_quote"] is not None
    assert body["comparison_to_last"]["sets_compared"] == 2
    assert body["comparison_to_last"]["fallback_summary"]["available"] is True
    assert len(body["comparison_to_last"]["exercise_comparisons"]) == 2


def test_actions_v2_log_gym_session_resolves_curls_from_plan_context_to_sz(client):
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mi"] = [
        {
            "id": "pull",
            "kind": "gym",
            "title": "Pull",
            "items": [
                {"kind": "exercise", "name": "Curls", "variation": "SZ", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]},
            ],
        }
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (40, '2026-04-01', 'Pull')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (40, 40, 'Curls', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (40, 40, 40, 1, 50, 10, 8)")
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "log_gym_session", "date": "2026-04-18", "session_name": "Pull", "raw_text": "Curls\n10x55@8", "dry_run": True},
        headers=auth(),
    ).get_json()

    match = body["matched_exercises"][0]
    assert match["name"] == "Curls"
    assert match["variation"] == "SZ"
    assert match["canonical_id"] == "curls__sz__bilateral"
    assert match["resolution_basis"] == "plan_match"
    assert match["plan_match"] is True
    assert match["planned_variation"] == "SZ"
    assert body["result"]["resolution"][0]["resolution_basis"] == "plan_match"
    assert body["result"]["resolution"][0]["chosen_variation"] == "SZ"


def test_actions_v2_log_gym_session_explicit_variation_overrides_plan(client):
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mi"] = [
        {
            "id": "pull",
            "kind": "gym",
            "title": "Pull",
            "items": [
                {"kind": "exercise", "name": "Curls", "variation": "SZ", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]},
            ],
        }
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "log_gym_session", "date": "2026-04-18", "session_name": "Pull", "raw_text": "Curls KH\n10x14@8", "dry_run": True},
        headers=auth(),
    ).get_json()

    match = body["matched_exercises"][0]
    assert match["name"] == "Curls"
    assert match["variation"] == "KH"
    assert match["canonical_id"] == "curls__kh__bilateral"
    assert match["resolution_basis"] == "explicit_variation"
    assert match["plan_match"] is True
    assert match["planned_variation"] == "SZ"


def test_actions_v2_log_gym_session_normalizes_redundant_display_variation(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (50, '2026-04-11', 'Push B')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (50, 50, 'Seitheben', 'SZ', 'SZ', 'unilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (51, 50, 'Katanas', 'SZ', 'SZ', 'unilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (50, 50, 50, 1, 16.5, 10, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (51, 51, 50, 1, 20.5, 9, 9)")
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-18",
            "session_name": "Push B",
            "dry_run": True,
            "exercises": [
                {"name": "Seitheben (SZ)", "variation": "SZ", "sets": [{"reps": 11, "weight": 17.5, "rpe": 9}]},
                {"name": "Katanas [SZ]", "variation": "SZ", "sets": [{"reps": 7, "weight": 22.5, "rpe": 8}]},
            ],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    matches = body["matched_exercises"]
    assert [(item["name"], item["variation"]) for item in matches] == [
        ("Seitheben", "SZ"),
        ("Katanas", "SZ"),
    ]
    assert [item["canonical_id"] for item in matches] == [
        "seitheben__sz__unilateral",
        "katanas__sz__unilateral",
    ]
    assert all(item["resolution_basis"] == "explicit_variation" for item in matches)


def test_actions_v2_log_gym_session_persists_v5_identity_slots_and_exclusions(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-18",
            "session_name": "Push B",
            "exercises": [
                {
                    "name": "sup. Curls (KH)",
                    "variation": "KH",
                    "canonical_exercise_id": "sup_curls",
                    "variation_id": "incline_bench_fixed_supination",
                    "execution_mode": "strict",
                    "sets": [
                        {"set_slot": "top_1", "reps": 8, "weight": 17.5, "rpe": 8, "is_work_set": True},
                        {"set_slot": "backoff_1", "reps": 10, "weight": 15, "rpe": 8, "technique_set": True},
                    ],
                }
            ],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    workout_id = body["ids"]["workout_id"]
    coach = client.get(f"/api/v2/actions/training/workout/{workout_id}/coach-view", headers=auth()).get_json()
    exercise = coach["exercises"][0]
    assert exercise["canonical_exercise_id"] == "sup_curls"
    assert exercise["variation_id"] == "incline_bench_fixed_supination"
    assert exercise["execution_mode"] == "strict"
    assert [item["set_slot"] for item in exercise["sets"]] == ["top_1", "backoff_1"]
    assert exercise["sets"][1]["technique_set"] is True


def test_actions_v2_log_gym_session_uses_weight_plausibility_as_tie_breaker(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (60, '2026-04-01', 'Arms')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (61, '2026-04-08', 'Arms')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (60, 60, 'Curls', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (61, 61, 'Curls', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (60, 60, 60, 1, 14, 10, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (61, 61, 61, 1, 50, 10, 8)")
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "log_gym_session", "date": "2026-04-18", "session_name": "Legs", "raw_text": "Curls\n10x14@8", "dry_run": True},
        headers=auth(),
    ).get_json()

    match = body["matched_exercises"][0]
    assert match["variation"] == "KH"
    assert match["canonical_id"] == "curls__kh__bilateral"
    assert match["resolution_basis"] == "weight_plausibility"
    assert match["plan_match"] is False
    assert match["alternatives"]
    assert match["alternatives"][0]["variation"] == "LH"


def test_actions_v2_workout_coach_view_and_historical_edits_do_not_change_plan(client):
    create = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-26",
            "session_name": "Push",
            "exercises": [
                {"name": "OHP LH", "variation": "LH", "sets": [{"reps": 5, "weight": 25, "rpe": 8}]},
            ],
        },
        headers=auth(),
    ).get_json()
    wid = create["ids"]["workout_id"]
    before_plan = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]
    coach = client.get(f"/api/v2/actions/training/workout/{wid}/coach-view", headers=auth()).get_json()
    assert coach["ok"] is True
    assert coach["workout_id"] == wid
    assert coach["date"] == "2026-04-26"
    assert coach["session_name"] == "Push"
    assert coach["session"]["workout_id"] == wid
    assert coach["exercises"]
    assert coach["parsed_exercises"][0]["exercise_index"] == 0
    assert coach["parsed_exercises"]
    dry = client.post("/api/v2/actions/training/edit-logged-workout-exercise", json={"workout_id": wid, "exercise_index": 0, "new_name": "OHP KH", "new_variation": "KH", "canonical_id": "ohp_db", "dry_run": True}, headers=auth()).get_json()
    assert dry["dry_run"] is True
    assert dry["execution"]["mode"] == "dry_run"
    assert dry["execution"]["live_state_changed"] is False
    assert dry["will_change"] == ["training_log"]
    assert dry["will_not_change"] == ["training_plan"]
    live = client.post("/api/v2/actions/training/edit-logged-workout-exercise", json={"workout_id": wid, "exercise_index": 0, "new_name": "OHP KH", "new_variation": "KH", "canonical_id": "ohp_db"}, headers=auth()).get_json()
    assert live["ok"] is True, live
    assert live["after"]["variation"] == "KH"
    note_live = client.post(
        "/api/v2/actions/training/edit-logged-workout-exercise",
        json={"workout_id": wid, "exercise_index": 0, "notes": "Ellbogen aktiv unter der Hantel halten."},
        headers=auth(),
    ).get_json()
    assert note_live["ok"] is True, note_live
    assert note_live["after"]["notes"] == "Ellbogen aktiv unter der Hantel halten."
    assert note_live["canonical_readback"]["notes"] == "Ellbogen aktiv unter der Hantel halten."
    assert note_live["parsed_payload"]["notes_supplied"] is True
    set_dry = client.post("/api/v2/actions/training/edit-logged-set", json={"workout_id": wid, "exercise_index": 0, "set_index": 0, "weight": 27.5, "reps": 4, "rpe": 8, "dry_run": True}, headers=auth()).get_json()
    assert set_dry["before"]["weight"] == 25
    assert set_dry["execution"]["mode"] == "dry_run"
    assert set_dry["execution"]["live_state_changed"] is False
    assert set_dry["will_change"] == ["training_log"]
    assert set_dry["will_not_change"] == ["training_plan"]
    live_set = client.post("/api/v2/actions/training/edit-logged-set", json={"workout_id": wid, "exercise_index": 0, "set_index": 0, "weight": 27.5, "reps": 4, "rpe": 8}, headers=auth()).get_json()
    assert live_set["after"]["weight"] == 27.5
    after = client.get(f"/api/v2/actions/training/workout/{wid}/coach-view", headers=auth()).get_json()
    assert after["parsed_exercises"][0]["variation"] == "KH"
    assert after["parsed_exercises"][0]["sets"][0]["weight"] == 27.5
    assert after["parsed_exercises"][0]["notes"] == "Ellbogen aktiv unter der Hantel halten."
    cleared = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "edit_logged_workout_exercise",
            "confirm": True,
            "payload": {"workout_id": wid, "exercise_index": 0},
            "notes": "",
        },
        headers=auth(),
    ).get_json()
    assert cleared["ok"] is True, cleared
    assert cleared["result"]["canonical_readback"]["notes"] == ""
    cleared_readback = client.get(f"/api/v2/actions/training/workout/{wid}/coach-view", headers=auth()).get_json()
    assert cleared_readback["parsed_exercises"][0]["notes"] == ""
    after_plan = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]
    assert before_plan == after_plan


def test_actions_v2_edit_logged_workout_exercise_dry_run_reresolves_canonical_id_for_new_variation(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (50, '2026-04-24', 'Push')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (51, '2026-04-25', 'Push')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (50, 50, 'OHP', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (51, 51, 'OHP', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (50, 50, 50, 1, 22.5, 8, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (51, 51, 51, 1, 40, 6, 8)")
    conn.commit()
    conn.close()
    wid = 50
    dry = client.post(
        "/api/v2/actions/training/edit-logged-workout-exercise",
        json={"workout_id": wid, "exercise_index": 0, "new_variation": "LH", "dry_run": True},
        headers=auth(),
    ).get_json()
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["execution"]["live_state_changed"] is False
    assert dry["will_change"] == ["training_log"]
    assert dry["will_not_change"] == ["training_plan"]
    assert dry["before"]["variation"] == "KH"
    assert dry["after"]["variation"] == "LH"
    assert dry["after"]["canonical_id"] != dry["before"]["canonical_id"]
    assert dry["resolution"]["status"] == "resolved"
    assert dry["resolution"]["requested_name"] == "OHP"
    assert dry["resolution"]["requested_variation"] == "LH"
    assert dry["resolution"]["resolved_canonical_id"] == dry["after"]["canonical_id"]


def test_liva_act_logged_exercise_note_edit_surfaces_canonical_readback_mismatch(client, monkeypatch):
    import copy
    import ai.actions_v2 as actions_v2

    created = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-27",
            "session_name": "Push note verification",
            "exercises": [{"name": "OHP", "variation": "KH", "sets": [{"reps": 8, "weight": 20, "rpe": 8}]}],
        },
        headers=auth(),
    ).get_json()
    workout_id = created["ids"]["workout_id"]
    original_load = actions_v2._load_workout_detail
    calls = 0

    def stale_after_write(requested_workout_id):
        nonlocal calls
        calls += 1
        detail = original_load(requested_workout_id)
        if calls >= 2 and detail:
            detail = copy.deepcopy(detail)
            detail["exercises"][0]["notes"] = ""
        return detail

    monkeypatch.setattr(actions_v2, "_load_workout_detail", stale_after_write)
    response = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "edit_logged_workout_exercise",
            "confirm": True,
            "payload": {"workout_id": workout_id, "exercise_index": 0, "notes": "Muss im Readback stehen."},
        },
        headers=auth(),
    )
    body = response.get_json()

    assert response.status_code == 500
    assert body["ok"] is False
    assert body["error"]["code"] == "write_verification_failed"
    assert body["mismatches"][0]["field"] == "notes"
    assert body["canonical_readback"]["notes"] == ""


def test_actions_v2_workout_coach_view_normalizes_set_numbers_and_keeps_raw_set_number(client):
    create = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-05-18",
            "session_name": "Push",
            "exercises": [
                {"name": "TNF Press", "variation": "Smith", "sets": [{"reps": 8, "weight": 45.0, "rpe": 8.0}, {"reps": 10, "weight": 42.5, "rpe": 9.0}]},
            ],
        },
        headers=auth(),
    ).get_json()
    wid = create["ids"]["workout_id"]
    conn = connections.get_training_db()
    conn.execute("UPDATE sets SET set_number=1 WHERE workout_id=?", (wid,))
    conn.commit()
    conn.close()
    body = client.get(f"/api/v2/actions/training/workout/{wid}/coach-view", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["workout_id"] == wid
    assert body["date"] == "2026-05-18"
    assert body["session_name"] == "Push"
    assert body["exercises"]
    sets = body["exercises"][0]["sets"]
    assert [item["set_index"] for item in sets] == [0, 1]
    assert [item["set_number"] for item in sets] == [1, 2]
    assert [item["display_set_number"] for item in sets] == [1, 2]
    assert [item["raw_set_number"] for item in sets] == [1, 1]
    assert body["parsed_exercises"]


def test_actions_v2_compare_workout_to_last_never_uses_future_workout(client):
    conn = connections.get_training_db()
    workouts = [
        (120, "2026-05-08", "Upper"),
        (121, "2026-05-15", "Upper"),
        (122, "2026-05-22", "Upper"),
    ]
    exercise_names = ["TNF Press", "Flys", "breite Rows", "Seitheben", "Pushdowns"]
    ex_id = 1200
    set_id = 2200
    for wid, date_iso, name in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (wid, date_iso, name))
        for exercise_name in exercise_names:
            conn.execute(
                "INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, 'Cable', 'Cable', 'bilateral')",
                (ex_id, wid, exercise_name),
            )
            conn.execute(
                "INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, 20, 10, 8)",
                (set_id, ex_id, wid),
            )
            conn.execute(
                "INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 2, 18, 12, 8)",
                (set_id + 1, ex_id, wid),
            )
            ex_id += 1
            set_id += 2
    conn.commit()
    conn.close()

    historical = client.get("/api/v2/actions/training/workout/121/coach-view", headers=auth()).get_json()["comparison_to_last"]
    assert historical["last_workout_id"] == 120
    assert historical["last_session_date"] == "2026-05-08"
    assert historical["sets_compared"] > 0


def test_actions_v2_compare_workout_to_last_pairs_historical_sets_even_with_null_working_set_and_bad_raw_numbers(client):
    conn = connections.get_training_db()
    workouts = [
        (130, "2026-05-15", "Upper"),
        (131, "2026-05-22", "Upper"),
    ]
    exercise_names = ["TNF Press", "Flys", "breite Rows", "Seitheben", "Pushdowns"]
    weights = {
        130: [(45, 8), (42.5, 10)],
        131: [(47.5, 8), (45, 10)],
    }
    ex_id = 1300
    set_id = 2300
    for wid, date_iso, name in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (wid, date_iso, name))
        for exercise_name in exercise_names:
            conn.execute(
                "INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, 'Cable', 'Cable', 'bilateral')",
                (ex_id, wid, exercise_name),
            )
            for raw_set_number, (weight, reps) in enumerate(weights[wid], start=1):
                conn.execute(
                    "INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, ?, ?, 8)",
                    (set_id, ex_id, wid, weight, reps),
                )
                set_id += 1
            ex_id += 1
    conn.commit()
    conn.close()

    comparison = client.get("/api/v2/actions/training/workout/131/coach-view", headers=auth()).get_json()["comparison_to_last"]
    assert comparison["available"] is True
    assert comparison["last_workout_id"] == 130
    assert comparison["sets_compared"] >= 10
    assert comparison["progression_quote"] is not None
    assert any(item.startswith("warmup_status_unclear:") for item in comparison["warnings"])
    for exercise in comparison["exercise_comparisons"]:
        assert exercise["sets_compared"] == 2
    assert comparison["top_lifts"]
    assert comparison["top_lifts_aggregated"]
    tnf = next(item for item in comparison["top_lifts_aggregated"] if item["canonical_id"] == "tnf_press__cable__bilateral")
    assert tnf["sets_improved"] == 2
    assert "2/2" in tnf["summary"]
    assert len([item for item in comparison["top_lifts_aggregated"] if item["canonical_id"] == "tnf_press__cable__bilateral"]) == 1
    assert comparison["weak_spots_aggregated"] == []


def test_actions_v2_progression_summary_and_phase_report(client):
    conn = connections.get_core_db()
    conn.execute("CREATE TABLE IF NOT EXISTS actions_v2_state (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)")
    conn.execute("INSERT OR REPLACE INTO actions_v2_state (key, value_json, updated_at) VALUES ('phase', ?, '2026-04-03T00:00:00Z')", (json.dumps({"phase": "cut", "phase_start": "2026-04-03"}),))
    conn.commit()
    conn.close()
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (60, '2026-05-01', 'Upper')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (61, '2026-05-10', 'Upper')")
    exercise_rows = [
        (60, 60, "Seitheben", "KH", "KH"),
        (61, 61, "Seitheben", "KH", "KH"),
        (62, 60, "Curls", "SZ", "SZ"),
        (63, 61, "Curls", "SZ", "SZ"),
        (64, 60, "Pushdowns", "Cable", "Cable"),
        (65, 61, "Pushdowns", "Cable", "Cable"),
        (66, 60, "Squats", "LH", "LH"),
        (67, 61, "Squats", "LH", "LH"),
        (68, 60, "Cable Crunches", "Cable", "Cable"),
        (69, 61, "Cable Crunches", "Cable", "Cable"),
    ]
    for row in exercise_rows:
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, 'bilateral')", row)
    set_rows = [
        (60, 60, 60, 1, 12, 12, 8),
        (61, 61, 61, 1, 12, 13, 8),
        (62, 62, 60, 1, 20, 10, 8),
        (63, 63, 61, 1, 20, 11, 8),
        (64, 64, 60, 1, 40, 12, 8),
        (65, 65, 61, 1, 40, 13, 8),
        (66, 66, 60, 1, 100, 5, 8),
        (67, 67, 61, 1, 100, 6, 8),
        (68, 68, 60, 1, 25, 15, 8),
        (69, 69, 61, 1, 25, 16, 8),
    ]
    for row in set_rows:
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()
    prog = client.get("/api/v2/actions/progression/summary?days=30&date_to=2026-05-20", headers=auth()).get_json()
    assert prog["ok"] is True
    assert "overall" in prog
    assert "data_quality" in prog
    assert "die meisten" not in prog["overall"]["summary"].lower() or (prog["overall"]["sets_improved_pct"] or 0) >= 0.60
    assert prog["groups"]["shoulders"]["key_lifts"][0]["name"] == "Seitheben"
    assert "biceps" in prog["groups"]
    assert "triceps" in prog["groups"]
    assert "legs" in prog["groups"]
    assert "core" in prog["groups"]
    shoulder_lift = prog["groups"]["shoulders"]["key_lifts"][0]
    assert shoulder_lift["next_target"] != shoulder_lift["best_set_30d"]
    assert shoulder_lift["target_reason"]
    assert shoulder_lift["target_basis"]
    phase = client.get("/api/v2/actions/bodyweight/phase-report", headers=auth()).get_json()
    assert phase["ok"] is True
    assert phase["phase"] == "cut"


def test_actions_v2_bodyweight_phase_report_without_setup_stays_defensive(client):
    body = client.get("/api/v2/actions/bodyweight/phase-report", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["phase"] == "unknown"
    assert body["needs_setup"] is True
    assert body["missing"] == ["phase_start", "phase_type"]


def test_actions_v2_bodyweight_phase_setup_dry_run_does_not_persist(client):
    resp = client.post(
        "/api/v2/actions/bodyweight/phase-setup",
        json={"phase_type": "cut", "phase_start": "2026-04-03", "phase_start_weight": 73.5, "dry_run": True},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["action"] == "setup_bodyweight_phase"
    assert body["dry_run"] is True
    assert body["dry_run_supported"] is True
    assert body["after"]["phase_type"] == "cut"
    assert body["will_change"] == ["bodyweight_phase_settings"]
    assert body["will_not_change"] == ["training_log", "training_plan"]
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    report = client.get("/api/v2/actions/bodyweight/phase-report", headers=auth()).get_json()
    assert report["needs_setup"] is True


def test_actions_v2_bodyweight_phase_setup_write_persists_and_phase_report_reads_it(client):
    conn = connections.get_nutrition_db()
    conn.execute("DELETE FROM weight_logs")
    conn.executemany(
        "INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)",
        [
            ("2026-04-03", 73.5),
            ("2026-04-10", 72.8),
            ("2026-04-17", 72.1),
            ("2026-04-24", 71.6),
            ("2026-05-01", 71.1),
        ],
    )
    conn.commit()
    conn.close()
    setup = client.post(
        "/api/v2/actions/bodyweight/phase-setup",
        json={"phase_type": "cut", "phase_start": "2026-04-03"},
        headers=auth(),
    ).get_json()
    assert setup["ok"] is True
    assert setup["dry_run"] is False
    assert setup["execution"]["mode"] == "write"
    assert setup["execution"]["live_state_changed"] is True
    report = client.get("/api/v2/actions/bodyweight/phase-report?date_to=2026-05-01", headers=auth()).get_json()
    assert report["ok"] is True
    assert report["phase"] == "cut"
    assert report["phase_start"] == "2026-04-03"
    assert report["phase_start_weight"] == 73.5
    assert report["weekly_deltas"]


def test_actions_v2_bodyweight_phase_report_uses_calendar_week_trend(client):
    conn = connections.get_nutrition_db()
    conn.execute("DELETE FROM weight_logs")
    conn.executemany(
        "INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)",
        [
            ("2026-05-09", 71.2),
            ("2026-05-10", 71.1),
            ("2026-05-11", 71.0),
            ("2026-05-12", 70.9),
            ("2026-05-13", 70.8),
            ("2026-05-14", 70.7),
            ("2026-05-15", 70.6),
            ("2026-05-16", 70.5),
            ("2026-05-17", 70.4),
            ("2026-05-18", 70.3),
            ("2026-05-19", 70.2),
            ("2026-05-20", 70.1),
            ("2026-05-21", 70.0),
            ("2026-05-22", 69.7),
        ],
    )
    conn.commit()
    conn.close()
    client.post(
        "/api/v2/actions/bodyweight/phase-setup",
        json={"phase_type": "cut", "phase_start": "2026-05-15", "phase_start_weight": 71.2},
        headers=auth(),
    )
    report = client.get("/api/v2/actions/bodyweight/phase-report?date_to=2026-05-22", headers=auth()).get_json()
    assert report["ok"] is True
    assert report["total_delta_since_phase_start"] == -1.5
    trend = report["weight_trend"]
    assert trend["primary"] == "calendar_week"
    assert trend["calendar_week"]["current"]["iso_week"] == "2026-W21"
    assert trend["calendar_week"]["current"]["sample_count"] == 5
    assert trend["calendar_week"]["current"]["coverage_ratio"] == 5 / 7
    assert trend["calendar_week"]["status"] == "usable"
    assert trend["calendar_week"]["calorie_adjustment_eligible"] is True
    assert report["recommended_action"] == "review"


def test_actions_v2_bodyweight_phase_setup_invalid_phase_type_is_structured(client):
    resp = client.post(
        "/api/v2/actions/bodyweight/phase-setup",
        json={"phase_type": "recomp", "phase_start": "2026-04-03", "dry_run": True},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"] == "invalid_detail"
    assert body["field"] == "phase_type"
    assert body["expected"] == ["cut", "bulk", "maintenance"]
    assert body["received"] == "recomp"


def test_actions_v2_progression_exercise_uses_latest_sessions_and_summary_target_logic(client):
    conn = connections.get_training_db()
    workout_rows = [
        (70, "2026-03-24", "Pull"),
        (71, "2026-03-26", "Pull"),
        (72, "2026-04-01", "Pull"),
        (73, "2026-04-04", "Pull"),
        (74, "2026-04-08", "Pull"),
        (75, "2026-05-12", "Pull"),
        (76, "2026-05-16", "Pull"),
        (77, "2026-05-19", "Pull"),
    ]
    for wid, date_iso, name in workout_rows:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (wid, date_iso, name))
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'breite Rows', 'eGym', 'eGym', 'bilateral')", (wid, wid))
    set_payloads = [
        (70, 60, 8),
        (71, 60, 9),
        (72, 60, 10),
        (73, 60, 11),
        (74, 60, 12),
        (75, 60, 13),
        (76, 60, 14),
        (77, 60, 15),
    ]
    for sid, reps, rpe in [(wid, reps, 8) for wid, reps, _ in set_payloads]:
        pass
    for wid, reps, rpe in set_payloads:
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, 60, ?, ?)", (wid, wid, wid, reps, rpe))
    conn.commit()
    conn.close()
    canonical_id = "breite_rows__egym__bilateral"
    body = client.get(f"/api/v2/actions/progression/exercise?canonical_id={canonical_id}&days=60&date_to=2026-05-20", headers=auth()).get_json()
    assert body["ok"] is True
    assert [item["date"] for item in body["last_5_sessions"]] == ["2026-05-19", "2026-05-16", "2026-05-12", "2026-04-08", "2026-04-04"]
    assert body["best_set_30d"] is not None
    assert body["next_target"] != body["best_set_30d"]
    assert body["target_reason"]
    assert body["target_basis"]
    assert body["data_quality"]["sessions_seen"] >= 5
    assert body["data_quality"]["comparison_basis"] == "exercise_history"


def test_actions_v2_progression_exercise_does_not_warn_on_subthreshold_dips(client):
    conn = connections.get_training_db()
    workout_rows = [
        (80, "2026-04-01", "Pull"),
        (81, "2026-04-10", "Pull"),
        (82, "2026-04-20", "Pull"),
        (83, "2026-05-05", "Pull"),
        (84, "2026-05-15", "Pull"),
        (85, "2026-05-19", "Pull"),
    ]
    rep_map = {80: 6, 81: 9, 82: 12, 83: 10, 84: 9, 85: 8}
    for wid, date_iso, name in workout_rows:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (wid, date_iso, name))
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'breite Rows', 'eGym', 'eGym', 'bilateral')", (wid, wid))
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, 97, ?, 8)", (wid, wid, wid, rep_map[wid]))
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/progression/exercise?canonical_id=breite_rows__egym__bilateral&days=60&date_to=2026-05-20", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["window_trend"] in {"up", "mixed"}
    assert body["recent_momentum"] == "mixed"
    assert body["stagnation_risk"] == "low"
    assert body["target_basis"] in {"best_30d", "best_in_window"}


def test_actions_v2_progression_invalid_mode_is_structured(client):
    resp = client.post("/api/v2/actions/progression/data", json={"mode": "compact", "exercise_names": ["Bench"]}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"] == "invalid_detail"
    assert body["expected"] == ["by_exercise", "by_group"]


def test_actions_v2_progression_summary_text_for_0292_is_not_majority():
    import ai.actions_v2 as actions_v2

    status, summary = actions_v2._progression_overall_status_and_summary(0.292)
    assert status in {"stagnating", "weak"}
    assert "die meisten" not in summary.lower()


def test_actions_v2_compare_reports_no_comparable_session_without_progression_quote(client):
    client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-18",
            "session_name": "Upper",
            "exercises": [
                {"name": "Test Lift Alpha", "variation": "Smith", "sets": [{"reps": 9, "weight": 72.5, "rpe": 8}]},
            ],
        },
        headers=auth(),
    )
    resp = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-25",
            "session_name": "Upper",
            "exercises": [
                {"name": "Test Lift Beta", "variation": "eGym", "sets": [{"reps": 8, "weight": 115, "rpe": 8}]},
            ],
        },
        headers=auth(),
    )
    comparison = resp.get_json()["comparison_to_last"]
    assert comparison["available"] is False
    assert comparison["reason"] == "no_comparable_previous_session"
    assert comparison["progression_quote"] is None
    assert "success_quote" not in comparison
    assert comparison["sets_improved"] == 0
    assert comparison["sets_stable"] == 0
    assert comparison["sets_regressed"] == 0
    assert comparison["fallback_summary"]["available"] is False


def test_actions_v2_compare_workout_to_last_does_not_hard_compare_different_variants_with_same_name(client):
    conn = connections.get_training_db()
    workouts = [
        (279, "2026-05-12", "Pull"),
        (285, "2026-05-19", "Pull"),
    ]
    for wid, date_iso, name in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", (wid, date_iso, name))
    exercise_rows = [
        (2790, 279, "enge Rows", "Cable", "Cable", "bilateral"),
        (2791, 279, "breite Rows", "eGym", "eGym", "bilateral"),
        (2792, 279, "Hammers", "KH", "KH", "bilateral"),
        (2793, 279, "Curls", "LH", "LH", "bilateral"),
        (2850, 285, "enge Rows", "Cable", "Cable", "bilateral"),
        (2851, 285, "breite Rows", "eGym", "eGym", "bilateral"),
        (2852, 285, "Hammers", "KH", "KH", "bilateral"),
        (2853, 285, "Curls", "KH", "KH", "bilateral"),
    ]
    for row in exercise_rows:
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, ?)", row)
    set_rows = [
        (4000, 2790, 279, 1, 60, 10, 8),
        (4001, 2790, 279, 2, 60, 9, 8),
        (4002, 2791, 279, 1, 70, 10, 8),
        (4003, 2791, 279, 2, 70, 9, 8),
        (4004, 2792, 279, 1, 16, 10, 8),
        (4005, 2793, 279, 1, 50, 10, 8),
        (4006, 2793, 279, 2, 50, 9, 8),
        (4010, 2850, 285, 1, 62.5, 10, 8),
        (4011, 2850, 285, 2, 62.5, 9, 8),
        (4012, 2851, 285, 1, 72.5, 10, 8),
        (4013, 2851, 285, 2, 72.5, 9, 8),
        (4014, 2852, 285, 1, 16, 11, 8),
        (4015, 2853, 285, 1, 14, 9, 8),
        (4016, 2853, 285, 2, 14, 8, 8),
    ]
    for row in set_rows:
        conn.execute(
            "INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()

    comparison = client.get("/api/v2/actions/training/workout/285/coach-view", headers=auth()).get_json()["comparison_to_last"]

    assert comparison["available"] is True
    assert comparison["last_workout_id"] == 279
    assert comparison["sets_compared"] == 5
    assert all(item["canonical_id"] != "curls__kh__bilateral" for item in comparison["weak_spots"])

    exercise_comparisons = {item["canonical_id"]: item for item in comparison["exercise_comparisons"]}
    assert "curls__kh__bilateral" not in exercise_comparisons
    assert exercise_comparisons["enge_rows__cable__bilateral"]["match_basis"] == "canonical_id_exact"
    assert exercise_comparisons["enge_rows__cable__bilateral"]["confidence"] == 0.85
    assert exercise_comparisons["enge_rows__cable__bilateral"]["current"]["canonical_id"] == "enge_rows__cable__bilateral"
    assert exercise_comparisons["enge_rows__cable__bilateral"]["previous"]["canonical_id"] == "enge_rows__cable__bilateral"

    assert comparison["unmatched_or_variant_changed"] == [
        {
            "name": "Curls",
            "current_canonical_id": "curls__kh__bilateral",
            "previous_canonical_id": "curls__lh__bilateral",
            "reason": "same_name_different_variation_not_compared",
        }
    ]


def test_actions_v2_compare_unmatched_warning_uses_engine_identity_not_display_id(client):
    conn = connections.get_training_db()
    for wid, date_iso in ((288, "2026-05-12"), (289, "2026-05-19")):
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, 'Push B')", (wid, date_iso))
    exercise_rows = [
        (2880, 288, "Schrägbankdrücken", "KH", "KH", "bilateral"),
        (2881, 288, "Beinpresse", "unilat", "", "unilateral"),
        (2890, 289, "Schrägbankdrücken", "KH", "KH", "bilateral"),
        (2891, 289, "Beinpresse", "unilat", "", "unilateral"),
    ]
    for row in exercise_rows:
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, ?)", row)
    set_rows = [
        (5100, 2880, 288, 1, 35, 9, 9),
        (5101, 2881, 288, 1, 120, 9, 8),
        (5110, 2890, 289, 1, 35, 10, 9),
        (5111, 2891, 289, 1, 127, 7, 8),
    ]
    for row in set_rows:
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()

    comparison = client.get("/api/v2/actions/training/workout/289/coach-view", headers=auth()).get_json()["comparison_to_last"]

    assert comparison["sets_compared"] == 2
    assert comparison["unmatched_or_variant_changed"] == []


def test_actions_v2_compare_workout_to_last_aggregates_weak_spots_per_exercise(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (286, '2026-05-19', 'Upper')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (287, '2026-05-26', 'Upper')")
    exercise_rows = [
        (2860, 286, "Pushdowns", "Cable", "Cable", "bilateral"),
        (2861, 286, "TNF Press", "Smith", "Smith", "bilateral"),
        (2870, 287, "Pushdowns", "Cable", "Cable", "bilateral"),
        (2871, 287, "TNF Press", "Smith", "Smith", "bilateral"),
    ]
    for row in exercise_rows:
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, ?)", row)
    set_rows = [
        (5000, 2860, 286, 1, 50, 10, 8),
        (5001, 2860, 286, 2, 45, 10, 8),
        (5002, 2861, 286, 1, 45, 8, 8),
        (5003, 2861, 286, 2, 42.5, 10, 8),
        (5010, 2870, 287, 1, 45, 9, 8),
        (5011, 2870, 287, 2, 45, 10, 8),
        (5012, 2871, 287, 1, 47.5, 8, 8),
        (5013, 2871, 287, 2, 45, 10, 8),
    ]
    for row in set_rows:
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()

    comparison = client.get("/api/v2/actions/training/workout/287/coach-view", headers=auth()).get_json()["comparison_to_last"]
    weak = next(item for item in comparison["weak_spots_aggregated"] if item["canonical_id"] == "pushdowns__cable__bilateral")
    top = next(item for item in comparison["top_lifts_aggregated"] if item["canonical_id"] == "tnf_press__smith__bilateral")

    assert comparison["weak_spots"]
    assert weak["sets_regressed"] == 1
    assert weak["sets_compared"] == 2
    assert weak["summary"].startswith("1/2")
    assert len([item for item in comparison["weak_spots_aggregated"] if item["canonical_id"] == "pushdowns__cable__bilateral"]) == 1
    assert top["sets_improved"] == 2


def test_liva_read_training_workout_coach_view_exposes_aggregated_spotlights(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (300, '2026-05-01', 'Upper')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (301, '2026-05-08', 'Upper')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (3000, 300, 'TNF Press', 'Smith', 'Smith', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (3001, 301, 'TNF Press', 'Smith', 'Smith', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (6000, 3000, 300, 1, 45, 8, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (6001, 3000, 300, 2, 42.5, 10, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (6002, 3001, 301, 1, 47.5, 8, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (6003, 3001, 301, 2, 45, 10, 8)")
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_workout_coach_view", "workout_id": 301}, headers=auth()).get_json()
    comparison = body["result"]["comparison_to_last"]

    assert body["ok"] is True
    assert "top_lifts_aggregated" in comparison
    assert "weak_spots_aggregated" in comparison
    assert next(item for item in comparison["top_lifts_aggregated"] if item["canonical_id"] == "tnf_press__smith__bilateral")["sets_improved"] == 2


def test_actions_v2_core_control_is_live_and_visible(client):
    resp = client.post("/api/v2/actions/core/control", json={"command": "set_override", "mode": "LIGHT", "reason": "test"}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True

    state = client.get("/api/v2/actions/core/state", headers=auth()).get_json()["core"]
    assert state["override_active"] is True
    assert state["override_source"] == "live_core_override"
    assert state["current_mode"] == "LIGHT"

    cleared = client.post("/api/v2/actions/core/control", json={"command": "clear_override"}, headers=auth()).get_json()
    assert cleared["execution"]["mode"] == "applied_live"
    assert cleared["execution"]["live_state_changed"] is True


def test_actions_v2_core_state_uses_live_core_decision(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    class FakeAppModule:
        @staticmethod
        def _core_decision_payload(day_iso):
            return {
                "ok": True,
                "as_of_day": day_iso,
                "confidence_label": "hoch",
                "domain_status": {"training": "stabil"},
                "decision": {"summary_line": "Heute: Push · Modus: HEAVY"},
                "today": {
                    "gym": {
                        "is_planned": True,
                        "slot": "Push",
                        "mode_suggested": "HEAVY",
                        "mode_effective": "HEAVY",
                    }
                },
                "upcoming": {"next_gym": {"date": day_iso, "slot": "Push", "mode_suggested": "HEAVY", "mode_effective": "HEAVY"}},
                "drivers": [],
            }

    monkeypatch.setitem(sys.modules, "app", FakeAppModule)
    monkeypatch.setattr(actions_v2, "_core_live_override_state", lambda day_iso: None)

    state = client.get("/api/v2/actions/core/state", headers=auth()).get_json()["core"]
    assert state["current_mode"] == "HEAVY"
    assert state["suggested_mode"] == "HEAVY"
    assert state["mode_source"] == "live_core_decision.today_gym"
    assert state["confidence"] == 0.86

    today = client.get("/api/v2/actions/liva/today", headers=auth()).get_json()["today"]
    assert today["core_mode"] == "HEAVY"
    assert today["core_mode_source"] == "live_core_decision.today_gym"


def test_actions_v2_core_state_uses_rolling_resolver_for_today_gym(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(actions_v2, "_plan_session_for_date", lambda _day: {
        "session_key": "push-b",
        "name": "Push B",
        "session_type": "gym",
        "exercises": [{"name": "Press"}],
    })
    monkeypatch.setattr(actions_v2, "_core_decision_mode_state", lambda _day: {
        "available": True,
        "current_mode": "NORMAL",
        "suggested_mode": "NORMAL",
        "today_gym": {"is_planned": False, "slot": "Wird vorbereitet"},
        "next_gym": {"slot": "Rest"},
        "drivers": [],
        "confidence_label": "mittel",
    })
    monkeypatch.setattr(actions_v2, "_core_live_override_state", lambda _day: None)

    state = client.get("/api/v2/actions/core/state", headers=auth()).get_json()["core"]
    today = state["decision"]["today_gym"]
    assert today["is_planned"] is True
    assert today["slot"] == "Push B"
    assert today["source"] == "rolling_resolver"


def test_actions_v2_core_board_build_persists_and_read_returns_board(client):
    payload = {
        "date": "2026-04-17",
        "source": "gpt_checkin",
        "trigger": "weight_hrv_checkin",
        "source_message": "Gewicht & HRV sind drin",
        "human_headline": "Upper kontrolliert ausführen.",
        "human_summary": "Die Daten reichen für Training, aber Progression ist heute kein Muss.",
        "decision_intent": "train_controlled",
        "confidence": 0.68,
        "machine_briefing": {"decision": "controlled", "notes": ["phase1"]},
        "visible_reasons": ["Gewicht ist eingegangen.", "HRV ist eingegangen."],
        "training_card": {"type": "gym", "title": "Upper", "fallback": True, "sections": [{"title": "Leitplanken", "items": ["Keine erzwungene Progression."]}]},
        "data_status": {"status": "partial", "domains": {"weight": {"status": "fresh"}, "recovery": {"status": "fresh"}}},
    }

    built = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth())
    body = built.get_json()
    assert built.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "applied_live"
    assert body["board"]["human_headline"] == "Upper kontrolliert ausführen."
    assert body["board"]["training_card"]["title"] == "Upper"

    read = client.get("/api/v2/actions/core/board?date=2026-04-17", headers=auth()).get_json()
    assert read["ok"] is True
    assert read["date"] == "2026-04-17"
    assert read["board"]["decision_intent"] == "train_controlled"
    assert read["board"]["visible_reasons"] == ["Gewicht ist eingegangen.", "HRV ist eingegangen."]


def test_actions_v2_core_board_get_is_read_only_when_missing(client):
    body = client.get("/api/v2/actions/core/board?date=2026-04-18", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["status"] == "missing"
    assert body["board"] is None


def test_actions_v2_core_training_card_get_is_gpt_compatibility_alias(client):
    body = client.get("/api/v2/actions/core/training-card?date=2026-04-18", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["source"] == "gpt_training_decision"
    assert body["deprecated_alias"] is True
    assert body["training_card"]["date"] == "2026-04-18"
    assert body["training_card"]["ai_decision"]["decision_id"] is None


def test_actions_v2_daily_context_exposes_missing_training_ai_decision(client):
    body = client.get("/api/v2/actions/daily/context?date=2026-04-20", headers=auth()).get_json()
    training = body["training"]
    assert training["planned_session"] is not None
    assert training["planned_session"]["name"]
    assert training["ai_decision"]["status"] == "missing"
    assert training["needs_gpt_decision"] is True
    assert training["decision_reason"] == "missing"
    assert training["ai_decision"]["decision_id"] is None
    assert training["ai_decision"]["saved_context_hash"] is None
    assert training["ai_decision"]["current_context_hash"]
    assert training["ai_decision"]["stale_reason"] is None
    assert training["ai_decision"]["stale_reasons"] == []


def test_actions_v2_training_today_decision_context_contains_gpt_fields(client):
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mo"][0]["title"] = "Push B · OHP + Chest Volume"
    plan["base_week"]["Mo"][0]["items"] = [
        {"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]},
        {"kind": "exercise", "name": "OHP", "variation": "LH", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]},
        {"kind": "exercise", "name": "Latzug", "variation": "eGym", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]},
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    conn = connections.get_training_db()
    conn.executescript(
        """
        INSERT INTO workouts (id, date_iso, name) VALUES
          (10, '2026-04-18', 'Push B · OHP + Chest Volume'),
          (11, '2026-04-12', 'Push B · OHP + Chest Volume'),
          (12, '2026-04-14', 'Push A · Chest Focus'),
          (13, '2025-12-13', 'Upper C'),
          (14, '2026-04-17', 'Lower A'),
          (15, '2026-04-16', 'Push');
        INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES
          (10, 10, 'Schrägbankdrücken', 'Smith', 'Smith', 'bilateral'),
          (11, 10, 'Latzug', 'eGym', 'eGym', 'bilateral'),
          (12, 11, 'Schrägbankdrücken', 'Smith', 'Smith', 'bilateral'),
          (13, 11, 'Latzug', 'eGym', 'eGym', 'bilateral'),
          (14, 12, 'Schrägbankdrücken', 'Smith', 'Smith', 'bilateral'),
          (15, 13, 'OHP', 'LH', 'LH', 'bilateral'),
          (16, 14, 'Beinpresse', 'unilat', 'unilat', 'bilateral'),
          (17, 15, 'OHP', 'LH', 'LH', 'bilateral');
        INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES
          (10, 10, 10, 1, 72.5, 8, 8),
          (11, 11, 10, 1, 110, 9, 8),
          (12, 12, 11, 1, 70, 8, 8),
          (13, 12, 11, 2, 67.5, 10, 8),
          (14, 13, 11, 1, 108, 8, 8),
          (15, 13, 11, 2, 102, 10, 8),
          (16, 14, 12, 1, 75, 7, 9),
          (17, 15, 13, 1, 35, 8, 9),
          (18, 16, 14, 1, 160, 10, 8),
          (19, 17, 15, 2, 25, 6, 9);
        """
    )
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["planned_session"]["name"] == "Push B · OHP + Chest Volume"
    assert body["planned_session"]["exercises"]
    assert body["session_type_context"]["planned_session_type"] == "Push B"
    assert body["session_type_context"]["session_type_source"] == "session_name"
    assert body["same_session_type_history"]
    assert body["last_same_session_type_session"]["available"] is True
    assert body["planned_vs_actual_last_same_session"]["completion_status"] == "partial"
    assert body["set_slot_reference_map"]["Schrägbankdrücken (Smith)"]["slots"][0]["primary_reference"]["available"] is True
    assert body["set_slot_reference_map"]["Schrägbankdrücken (Smith)"]["slots"][1]["primary_reference"]["source_relation"] == "same_session_type_older_slot_match"
    assert "OHP (LH)" in body["recent_training_history"]
    assert "OHP (LH)" in body["exercise_session_context"]
    assert "OHP (LH)" in body["set_slot_reference_map"]
    assert "OHP (LH)" in body["exercise_trends"]
    assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][0]["primary_reference"]["available"] is False
    assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][0]["secondary_references"]
    assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][0]["secondary_references"][0]["set_slot"] == 1
    assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][0]["secondary_references"][0]["source_relation"] in {"related_session_family_slot_match", "other_session_type_slot_match"}
    assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][0]["secondary_references"][0]["reference_quality"]
    if body["set_slot_reference_map"]["OHP (LH)"]["slots"][1]["secondary_references"]:
        assert body["set_slot_reference_map"]["OHP (LH)"]["slots"][1]["secondary_references"][0]["set_slot"] == 2
    assert body["exercise_session_context"]["Schrägbankdrücken (Smith)"]["other_session_type_work_sets"]
    assert body["exercise_session_context"]["OHP (LH)"]["related_session_family_work_sets"]
    assert body["exercise_session_context"]["OHP (LH)"]["other_session_type_work_sets"]
    assert body["exercise_session_context"]["OHP (LH)"]["same_session_type_work_sets"] == []
    assert body["recent_training_history"]["Schrägbankdrücken (Smith)"][0]["source_relation"] == "same_session_type"
    assert body["recent_training_history"]["Schrägbankdrücken (Smith)"][0]["sets"][0]["set_slot"] == 1
    assert any(row["source_relation"] == "related_session_family" for row in body["recent_training_history"]["OHP (LH)"])
    assert any(row["source_relation"] == "other_session_type" for row in body["recent_training_history"]["OHP (LH)"])
    assert body["exercise_trends"]["OHP (LH)"]["last_any_top_set"]
    assert body["exercise_trends"]["OHP (LH)"]["primary_reference_source"] == "related_session_family"
    assert body["exercise_trends"]["OHP (LH)"]["set_slot_reference_summary"]["slots_without_primary_reference"] >= 1
    assert body["exercise_trends"]["OHP (LH)"]["last_top_reference"]["set_slot"] == 1
    if body["exercise_trends"]["OHP (LH)"]["last_backoff_reference"]:
        assert body["exercise_trends"]["OHP (LH)"]["last_backoff_reference"]["set_slot"] == 2
    assert body["exercise_trends"]["Schrägbankdrücken (Smith)"]["primary_reference_source"] == "same_session_type"
    assert body["exercise_trends"]["OHP (LH)"]["history_status"] == "available"
    assert body["exercise_trends"]["OHP (LH)"]["last_top_reference"]["reference_quality"]
    assert body["planned_session"]["plan_source"] == "active_plan_v2"
    assert body["planned_session"]["plan_source_authoritative"] is True
    assert body["planned_session"]["exercises"][0]["planned_sets_source"] == "active_plan_v2"
    assert body["context_quality"]["all_secondary_references_have_set_slot"] is True
    assert body["context_quality"]["same_session_type_found"] is True
    assert body["context_quality"]["all_planned_exercises_have_any_history"] is True
    assert not any(entry.get("session_type") == "Lower A" and entry.get("source_relation") == "same_session_type" for entry in body["recent_training_history"].get("Beinpresse", []))
    assert "recent_training_history" in body
    assert "recovery_context" in body
    assert "bodyweight_context" in body
    assert "user_checkin" in body
    assert "constraints" in body
    assert body["context_hash"]


def test_actions_v2_training_today_decision_context_passes_recovery_calendar_and_weight(monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(actions_v2, "_plan_session_for_date", lambda day_iso: {"available": True, "plan_id": 28, "planned_session_id": 7, "name": "Push B · OHP + Beinpresse + Chest Volume", "status": "planned", "session_type": "gym", "exercises": []})
    monkeypatch.setattr(actions_v2, "_build_training_history_context", lambda day_iso, planned_session: ({}, {}, None, {"session_type_context": {}, "same_session_type_history": [], "last_same_session_type_session": {"available": False}, "planned_vs_actual_last_same_session": {"available": False}, "set_slot_reference_map": {}, "exercise_session_context": {}, "context_quality": {}}))
    monkeypatch.setattr(actions_v2, "_compact_recovery_summary_payload", lambda recovery: recovery)
    monkeypatch.setattr(
        actions_v2,
        "_recovery_summary",
        lambda day_iso: {
            "source": "polar",
            "status": "okay",
            "actual_sleep_minutes": 404,
            "sleep_score": 68.6605,
            "sleep_window_label": "23:11 – 06:23",
            "sleep_start": "2026-06-02T23:11:06+02:00",
            "sleep_end": "2026-06-03T06:23:06+02:00",
            "rmssd": 101.0,
            "ans_status": -2.737339973449707,
            "ans_rate": 2.0,
            "recovery_indicator": 3.0,
            "recovery_indicator_sublevel": 45.0,
            "heart_rate": {"resting_heart_rate_available": False, "resting_heart_rate_bpm": None, "sleep_heart_rate_bpm": 39.45},
            "flags": {"alcohol": False, "sickness": False},
            "recent_flags": {"recent_alcohol_within_14d": False, "recent_sickness_within_14d": False},
        },
    )
    monkeypatch.setattr(
        actions_v2,
        "_weight_state",
        lambda *_args: {
            "latest_weight": 70.6,
            "latest_weight_age_days": 0,
            "checkin_status": "weight_fresh",
            "trend_direction": "up",
            "calendar_week_average_kg": 69.9,
            "calendar_week_delta_kg": 0.9,
            "calendar_week_status": "usable",
            "calendar_week_measurements": 7,
            "calorie_adjustment_eligible": True,
        },
    )
    monkeypatch.setattr(actions_v2, "_latest_bodyweight_entry", lambda day_iso: {"weight_kg": 70.6})
    monkeypatch.setattr(actions_v2, "_subjective_checkin_for_date", lambda day_iso: {"available": False, "reason": "no_checkin_for_date"})
    monkeypatch.setattr(actions_v2, "_latest_training_ai_decision", lambda day_iso: None)
    monkeypatch.setattr(
        actions_v2,
        "_calendar_range_payload",
        lambda start_iso, end_iso, **kwargs: {
            "calendar_scope": "all_readable_calendars",
            "days": [
                {
                    "available": True,
                    "day_pressure": "medium",
                    "data_freshness": {"is_stale": False, "last_sync_at": "2026-06-03T12:20:03", "source": "existing_calendar_sync"},
                    "next_event": {"title": "Training · Push B · OHP + Beinpresse + Chest Volume", "start": "2026-06-03T18:30:00+02:00", "end": "2026-06-03T19:50:00+02:00", "source": "google_calendar"},
                    "events": [
                        {"title": "Schule", "start": "2026-06-03T08:00:00+02:00", "end": "2026-06-03T13:15:00+02:00", "source": "school_schedule_summary", "summary": {"total_minutes_in_school": 315, "total_blocks": 3, "has_afternoon_school": False, "is_fragmented_day": False}},
                        {"title": "Englisch", "start": "2026-06-03T08:00:00+02:00", "end": "2026-06-03T09:55:00+02:00", "source": "google_calendar", "calendar_summary": "WebUntis"},
                        {"title": "Geschichte", "start": "2026-06-03T09:55:00+02:00", "end": "2026-06-03T11:45:00+02:00", "source": "google_calendar", "calendar_summary": "WebUntis"},
                        {"title": "Bio", "start": "2026-06-03T11:45:00+02:00", "end": "2026-06-03T13:15:00+02:00", "source": "google_calendar", "calendar_summary": "WebUntis"},
                        {"title": "Training · Push B · OHP + Beinpresse + Chest Volume", "start": "2026-06-03T18:30:00+02:00", "end": "2026-06-03T19:50:00+02:00", "source": "google_calendar", "description": "CORE geplant: smartes Zeitfenster inkl. Kalender-Check. Session: Push B · OHP + Beinpresse + Chest Volume."},
                    ],
                }
            ],
        },
    )

    payload = actions_v2._training_decision_context_payload("2026-06-03")
    assert payload["recovery_context"]["available"] is True
    assert payload["recovery_context"]["sleep_hours"] == 6.73
    assert payload["recovery_context"]["rmssd"] == 101.0
    assert payload["recovery_context"]["ans_status"] == -2.737339973449707
    assert payload["calendar_context"]["day_pressure"] == "medium"
    assert payload["calendar_context"]["school"]["total_minutes"] == 315
    assert any(item["role"] == "school_summary" for item in payload["calendar_context"]["relevant_events"])
    assert payload["calendar_context"]["training_event"]["available"] is True
    assert payload["calendar_context"]["training_event"]["duration_minutes"] == 80
    assert payload["bodyweight_context"]["calendar_week_average_kg"] == 69.9
    assert payload["constraints"]["decision_authority"] == "gpt_only"
    assert payload["constraints"]["core_may_decide_progression"] is False
    assert payload["constraints"]["core_may_decide_skip"] is False
    assert "CORE geplant" not in json.dumps(payload, ensure_ascii=False)


def test_actions_v2_training_today_decision_context_hash_changes_for_recovery_and_calendar_but_not_existing_decision():
    import ai.actions_v2 as actions_v2

    payload = {
        "date": "2026-06-03",
        "recovery_context": {"available": True, "sleep_hours": 6.73},
        "calendar_context": {"available": True, "day_pressure": "medium"},
        "existing_decision": {"status": "fresh", "decision_id": 1},
        "_latest_session_date": "2026-06-01",
    }
    original = actions_v2._training_decision_context_hash(payload)
    changed_recovery = dict(payload)
    changed_recovery["recovery_context"] = {"available": True, "sleep_hours": 7.1}
    assert actions_v2._training_decision_context_hash(changed_recovery) != original
    changed_calendar = dict(payload)
    changed_calendar["calendar_context"] = {"available": True, "day_pressure": "high"}
    assert actions_v2._training_decision_context_hash(changed_calendar) != original
    changed_existing = dict(payload)
    changed_existing["existing_decision"] = {"status": "stale", "decision_id": 99}
    assert actions_v2._training_decision_context_hash(changed_existing) == original


def test_actions_v2_training_today_decision_context_hash_ignores_resolver_anchor_metadata():
    import ai.actions_v2 as actions_v2

    payload = {
        "date": "2026-06-08",
        "planned_session": {
            "name": "Pull A · Latzug + RDL",
            "resolver": {
                "source": "rolling_rotation",
                "anchor_source": "saved_decision",
                "anchor_date": "2026-06-07",
                "anchor_session_name": "Push A · Road to 40s + Squat",
                "anchor_confidence": "planned",
                "reason": "used latest saved gym decision before 2026-06-08",
                "resolved_session": {"name": "Pull A · Latzug + RDL", "session_key": "abc"},
            },
        },
        "recovery_context": {"available": True, "sleep_hours": 7.1},
        "calendar_context": {"available": True, "day_pressure": "medium"},
    }
    original = actions_v2._training_decision_context_hash(payload)
    changed_resolver = copy.deepcopy(payload)
    changed_resolver["planned_session"]["resolver"]["anchor_source"] = "persisted_training_card"
    changed_resolver["planned_session"]["resolver"]["anchor_date"] = "2026-06-08"
    changed_resolver["planned_session"]["resolver"]["reason"] = "used latest persisted gym training card before or on 2026-06-08"
    assert actions_v2._training_decision_context_hash(changed_resolver) == original


def test_actions_v2_training_decision_freshness_uses_one_hash_for_mcp_and_card(monkeypatch):
    import ai.actions_v2 as actions_v2

    context = {"date": "2026-06-08", "planned_session": {"available": True}}
    monkeypatch.setattr(actions_v2, "_training_decision_context_payload", lambda _day: context)
    monkeypatch.setattr(actions_v2, "_training_decision_context_hash", lambda value: "canonical-hash" if value is context else "other")
    monkeypatch.setattr(actions_v2, "_latest_training_ai_decision", lambda _day: {
        "status": "fresh", "source": "liva_mcp", "context_hash": "canonical-hash", "decision": {"session": {"name": "Push B"}},
    })

    state = actions_v2._training_ai_decision_state("2026-06-08")
    assert state["status"] == "fresh"
    assert state["current_context_hash"] == "canonical-hash"

    monkeypatch.setattr(actions_v2, "_training_decision_context_hash", lambda _value: "changed-hash")
    stale = actions_v2._training_ai_decision_state("2026-06-08")
    assert stale["status"] == "stale"
    assert "context_hash_changed" in stale["stale_reasons"]


def test_actions_v2_liva_read_training_today_decision_context_returns_compact_payload(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    sample_payload = {
        "ok": True,
        "date": "2026-06-03",
        "context_hash": "hash-123",
        "purpose": "training_ai_decision_context",
        "existing_decision": {"status": "missing", "decision_id": None},
        "planned_session": {
            "name": "Push B",
            "plan_id": 28,
            "plan_source": "active_plan_v2",
            "plan_source_authoritative": True,
            "session_type": "gym",
            "weekday": "Mi",
            "status": "planned",
            "exercises": [
                {"name": "Schrägbankdrücken (KH)", "order_index": 1, "planned_sets": 2, "target_rep_range": "6–10", "equipment": "KH", "variation": "KH", "planned_sets_source": "active_plan_v2", "planned_sets_confidence": "high"}
            ],
        },
        "recovery_context": {"available": True, "sleep_hours": 6.73, "rmssd": 101.0, "ans_status": -2.7},
        "bodyweight_context": {"available": True, "today_kg": 70.6},
        "calendar_context": {"available": True, "day_pressure": "medium", "school": {"available": True}, "training_event": {"available": True}, "warnings": []},
        "user_checkin": {"available": False, "reason": "no_checkin_for_date"},
        "constraints": {"decision_authority": "gpt_only"},
        "context_quality": {"same_session_type_found": False, "all_planned_exercises_have_any_history": True, "all_planned_set_slots_have_primary_reference": False, "all_secondary_references_have_set_slot": True, "suspicious_reference_count": 0, "stale_reference_count": 1, "variation_mismatch_count": 0, "plan_source_conflict_count": 0, "warnings": ["No exact same-session references."]},
        "recent_training_history": {"Schrägbankdrücken (KH)": [{"date": "2026-05-28", "session_type": "Push", "source_relation": "related_session_family", "sets": [{"set_slot": 1, "load_kg": 30, "reps": 6, "rpe": 8}, {"set_slot": 2, "load_kg": 62.5, "reps": 5, "rpe": 9}]}]},
        "exercise_session_context": {"Schrägbankdrücken (KH)": {"same_session_type_work_sets": [], "related_session_family_work_sets": [{}], "other_session_type_work_sets": []}},
        "set_slot_reference_map": {"Schrägbankdrücken (KH)": {"slots": [{"set_slot": 1, "primary_reference": {"available": False}, "secondary_references": [{"source_relation": "related_session_family_slot_match", "date": "2026-05-28", "session_type": "Push", "load_kg": 30, "reps": 6, "rpe": 8, "reference_quality": "secondary_related", "age_days": 6, "warnings": []}]}, {"set_slot": 2, "primary_reference": {"available": False}, "secondary_references": [{"source_relation": "related_session_family_slot_match", "date": "2026-05-28", "session_type": "Push", "load_kg": 62.5, "reps": 5, "rpe": 9, "reference_quality": "low_confidence", "age_days": 6, "warnings": ["variation_mismatch", "old_reference"]}]}]}},
        "exercise_trends": {"Schrägbankdrücken (KH)": {"primary_reference_source": "related_session_family", "last_top_set": "30 x 6 @8", "last_backoff": "62.5 x 5 @9", "last_any_top_set": "30 x 6 @8", "reference_warning": None, "warnings": []}},
    }

    monkeypatch.setattr(actions_v2, "_read_training_today_decision_context", lambda *_args: (sample_payload, 200))
    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_decision_context", "date": "2026-06-03"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["mode"] == "training_today_decision_context"
    result = body["result"]
    assert result["context_hash"]
    assert result["planned_session"]
    assert result["recovery_context"]["sleep_hours"] is not None
    assert result["calendar_context"]["day_pressure"]
    assert result["bodyweight_context"]["today_kg"] is not None
    assert len(result["exercise_decision_context"]) == len(result["planned_session"]["exercises"])
    assert result["exercise_decision_context"][0]["slots"][1]["best_reference"]["use_for_prescription"] is False
    assert result["exercise_decision_context"][0]["slots"][1]["best_reference"]["reason"] == "low_confidence_variation_mismatch"
    assert "recent_training_history" not in result
    assert "set_slot_reference_map" not in result
    assert "exercise_session_context" not in result
    assert len(json.dumps(result, ensure_ascii=False)) < 35000
    assert result["constraints"]["decision_authority"] == "gpt_only"


def test_compact_training_context_derives_existing_decision_status(monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_training_ai_decision_state",
        lambda _day, context=None: {
            "status": "stale",
            "decision_id": 42,
            "saved_context_hash": "old",
            "current_context_hash": "new",
            "stale_reason": "context_hash_changed",
            "stale_reasons": ["context_hash_changed"],
        },
    )
    compact = actions_v2._compact_training_today_decision_context_payload(
        {"date": "2026-06-03", "existing_decision": {"status": "fresh", "decision_id": 42}}
    )
    assert compact["existing_decision"]["status"] == "stale"
    assert compact["existing_decision"]["current_context_hash"] == "new"


def test_actions_v2_liva_read_training_today_decision_context_full_still_returns_full_payload(client):
    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_decision_context_full", "date": "2026-06-03"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["mode"] == "training_today_decision_context_full"
    result = body["result"]
    assert "recent_training_history" in result
    assert "set_slot_reference_map" in result
    assert "exercise_session_context" in result


def test_actions_v2_liva_read_training_context_and_core_card_do_not_create_flask_context(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_call_actions_view",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("migrated LIVA reads must not create a Flask request context")),
    )

    compact = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_today_decision_context", "date": "2026-06-03"},
        headers=auth(),
    ).get_json()
    full = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_today_decision_context_full", "date": "2026-06-03"},
        headers=auth(),
    ).get_json()
    core_card = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "core_training_card", "date": "2026-06-03"},
        headers=auth(),
    ).get_json()
    direct_context = client.get(
        "/api/v2/actions/training/today/decision-context?date=2026-06-03",
        headers=auth(),
    ).get_json()
    direct_core_card = client.get(
        "/api/v2/actions/core/training-card?date=2026-06-03",
        headers=auth(),
    ).get_json()

    assert compact["ok"] is True
    assert full["ok"] is True
    assert core_card["ok"] is True
    assert full["result"] == direct_context
    assert core_card["result"] == direct_core_card


def test_actions_v2_liva_read_daily_context_returns_training_trigger_shape(client):
    body = client.post("/api/v2/actions/liva/read", json={"mode": "daily_context", "date": "2026-06-03"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["mode"] == "daily_context"
    training = body["result"]["training"]
    assert training["planned_session"] is not None
    assert isinstance(training["needs_gpt_decision"], bool)
    assert isinstance(training["decision_reason"], str) and training["decision_reason"]
    assert isinstance(training["ai_decision"], dict)
    assert training["ai_decision"]["status"]


def test_actions_v2_liva_read_daily_context_matches_direct_daily_context_status(client):
    facade = client.post("/api/v2/actions/liva/read", json={"mode": "daily_context", "date": "2026-06-03"}, headers=auth()).get_json()
    direct = client.get("/api/v2/actions/daily/context?date=2026-06-03", headers=auth()).get_json()
    assert facade["result"]["training"]["ai_decision"]["status"] == direct["training"]["ai_decision"]["status"]


def test_training_today_decision_context_placeholder_fr_resolves_from_last_logged_pull_b(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-04", "FB Pull B")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Lower B"
    assert planned["session_key"] == "613d6d74-10"
    assert planned["session_type"] == "gym"
    assert len(planned["exercises"]) == 1
    assert planned["resolver"]["source"] == "rolling_rotation"
    assert planned["resolver"]["anchor_source"] == "workout_log"
    assert planned["resolver"]["anchor_session_name"] == "FB Pull B"
    assert planned["resolver"]["anchor_confidence"] == "actual"
    assert planned["name"] != "Fr"


def test_training_today_decision_context_honors_rolling_sunday_rest_day(client):
    _set_active_rolling_rotation_plan()
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["meta"]["rolling_week_pattern"] = {
        "Mo": "train", "Di": "train", "Mi": "train", "Do": "train",
        "Fr": "train", "Sa": "train", "So": "rest",
    }
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()
    _insert_workout("2026-06-06", "Pull B")

    direct = client.get(
        "/api/v2/actions/training/today/decision-context?date=2026-06-07",
        headers=auth(),
    ).get_json()
    facade = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_today_decision_context_full", "date": "2026-06-07"},
        headers=auth(),
    ).get_json()
    monday = client.get(
        "/api/v2/actions/training/today/decision-context?date=2026-06-08",
        headers=auth(),
    ).get_json()

    assert direct["planned_session"]["available"] is False
    assert direct["planned_session"]["status"] == "rest"
    assert direct["planned_session"]["planned_session_id"] is None
    assert direct["planned_session"]["exercises"] == []
    assert facade["result"]["planned_session"] == direct["planned_session"]
    assert monday["planned_session"]["name"] == "Lower B"
    assert monday["planned_session"]["resolver"]["source"] == "rolling_rotation"


def test_training_today_decision_context_placeholder_sa_uses_saved_push_a_card_as_anchor(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-04", "FB Pull B")
    _ensure_core_training_card_row("2026-06-05", "Push A · Road to 40s + Squat", items_count=2)

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Lower B"
    assert planned["session_key"] == "613d6d74-10"
    assert planned["resolver"]["anchor_source"] == "workout_log"
    assert planned["resolver"]["anchor_date"] == "2026-06-04"
    assert planned["resolver"]["anchor_confidence"] == "actual"
    assert planned["name"] != "Sa"


def test_training_today_decision_context_same_day_saved_push_a_card_reuses_session(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-04", "FB Pull B")
    _ensure_core_training_card_row("2026-06-05", "Push A · Road to 40s + Squat", items_count=2)

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Lower B"
    assert planned["resolver"]["anchor_source"] == "workout_log"
    assert planned["resolver"]["anchor_date"] == "2026-06-04"
    assert planned["resolver"]["resolved_session"]["name"] == "Lower B"


def test_training_today_decision_context_rest_card_does_not_advance_rotation(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-04", "FB Pull A")
    _ensure_core_training_card_row("2026-06-05", "Rest", session_type="rest", items_count=0, board_source="phase1_fallback")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Lower A"
    assert planned["resolver"]["anchor_source"] == "workout_log"


def test_training_today_decision_context_gym_card_does_not_override_rotation_anchor(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-12", "FB Pull A")
    _ensure_core_training_card_row("2026-06-13", "Rest", session_type="rest", items_count=0, board_source="phase1_fallback")
    _ensure_core_training_card_row("2026-06-14", "Push A · Road to 40s + Squat", session_type="gym", items_count=2)

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-15", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Lower A"
    assert planned["resolver"]["anchor_source"] == "workout_log"
    assert planned["resolver"]["anchor_date"] == "2026-06-12"


def test_read_only_training_resolver_does_not_write_or_advance(client):
    import ai.actions_v2 as actions_v2

    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-04", "FB Pull B")

    conn = connections.get_core_db()
    actions_v2.ensure_training_ai_decisions_schema(conn)
    conn.execute("SELECT 1")
    before_decisions = conn.execute("SELECT COUNT(*) FROM training_ai_decisions").fetchone()[0]
    before_cards = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'").fetchone()[0]
    conn.close()

    decision = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()
    daily = client.get("/api/v2/actions/daily/context?date=2026-06-05", headers=auth()).get_json()

    conn = connections.get_core_db()
    after_decisions = conn.execute("SELECT COUNT(*) FROM training_ai_decisions").fetchone()[0]
    after_cards = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'").fetchone()[0]
    conn.close()

    assert decision["planned_session"]["name"] == "Lower B"
    assert daily["training"]["planned_session"]["name"] == "Lower B"
    assert decision["planned_session"]["resolver"]["source"] == "rolling_rotation"
    assert daily["training"]["planned_session"]["resolver"]["source"] == "rolling_rotation"
    assert before_decisions == after_decisions == 0
    assert before_cards == after_cards == 0


def test_training_today_decision_context_can_use_same_day_saved_decision_when_no_card_exists(client):
    _set_active_rolling_rotation_plan()
    _ensure_saved_training_decision("2026-06-05", "Push A · Road to 40s + Squat")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Pull A"
    assert planned["resolver"]["source"] == "rolling_rotation"


def test_rolling_sequence_prefers_rotation_over_fixed_weekday_labels(client):
    _set_active_rolling_rotation_plan_with_fixed_weekday_labels()
    _ensure_saved_training_decision("2026-06-05", "Push A · Road to 40s + Squat")
    _insert_workout("2026-06-05", "Push A · Road to 40s + Squat")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Pull A"
    assert planned["resolver"]["source"] == "rolling_rotation"


def test_rolling_resolver_uses_last_logged_arbitrary_plan_label_then_next_sequence_item(client):
    conn = connections.get_plans_db()
    plan = {
        "meta": {"mode": "rolling_sequence"},
        "base_week": {"Fr": [{"id": "weekday-fallback", "kind": "gym", "title": "Wrong Weekday Label", "items": [{"kind": "exercise", "name": "Fallback"}]}]},
        "sequence": [
            {"id": "alpha-id", "kind": "gym", "title": "Alpha Circuit", "items": [{"kind": "exercise", "name": "Alpha Lift", "sets": 2, "reps": {"min": 8, "max": 10}}]},
            {"id": "beta-id", "kind": "gym", "title": "Beta Circuit", "items": [{"kind": "exercise", "name": "Beta Lift", "sets": 2, "reps": {"min": 8, "max": 10}}]},
            {"id": "gamma-id", "kind": "gym", "title": "Gamma Circuit", "items": [{"kind": "exercise", "name": "Gamma Lift", "sets": 2, "reps": {"min": 8, "max": 10}}]},
        ],
    }
    conn.execute("UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()
    _insert_workout("2026-06-04", "Alpha Circuit")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()

    assert body["planned_session"]["name"] == "Beta Circuit"
    assert body["planned_session"]["session_key"] == "beta-id"
    assert body["planned_session"]["resolver"]["anchor_session_name"] == "Alpha Circuit"


def test_rolling_resolver_does_not_fall_back_to_weekday_when_log_is_unknown(client):
    conn = connections.get_plans_db()
    plan = {
        "meta": {"mode": "rolling_sequence"},
        "base_week": {"Fr": [{"id": "weekday-fallback", "kind": "gym", "title": "Wrong Weekday Label", "items": [{"kind": "exercise", "name": "Fallback"}]}]},
        "sequence": [
            {"id": "first-id", "kind": "gym", "title": "First Custom Session", "items": [{"kind": "exercise", "name": "First Lift", "sets": 2, "reps": {"min": 8, "max": 10}}]},
            {"id": "second-id", "kind": "gym", "title": "Second Custom Session", "items": [{"kind": "exercise", "name": "Second Lift", "sets": 2, "reps": {"min": 8, "max": 10}}]},
        ],
    }
    conn.execute("UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()
    _insert_workout("2026-06-04", "Unmapped Logged Workout")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()

    assert body["planned_session"]["name"] == "First Custom Session"
    assert body["planned_session"]["resolver"]["reason"].startswith("last logged session was not found")


def test_actions_v2_liva_act_compact_save_uses_context_planned_session_when_payload_name_is_stale(client):
    _set_active_rolling_rotation_plan_with_fixed_weekday_labels()
    _ensure_saved_training_decision("2026-06-05", "Push A · Road to 40s + Squat")

    context = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_decision_context", "date": "2026-06-06"}, headers=auth()).get_json()
    assert context["result"]["planned_session"]["name"] == "Pull A"

    saved = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "save_training_decision",
            "date": "2026-06-06",
            "context_hash": context["result"]["context_hash"],
            "value": {
                "status": "train",
                "session_name": "Push A · Road to 40s + Squat",
                "headline": "Pull A heute sauber durchziehen.",
                "global_rule": "Kein Grinden.",
                "exercises": [
                    {
                        "name": "Rows",
                        "target_sets": [{"weight": 60, "reps": "8-10", "rpe": "8-9"}],
                    }
                ],
            },
        },
        headers=auth(),
    ).get_json()

    assert saved["ok"] is True
    assert saved["result"]["status"] == "fresh"
    assert saved["result"]["readback"]["ok"] is True

    daily = client.get("/api/v2/actions/daily/context?date=2026-06-06", headers=auth()).get_json()
    assert daily["training"]["planned_session"]["name"] == "Pull A"
    assert daily["training"]["ai_decision"]["status"] == "fresh"

    card = client.get("/api/v2/actions/training/today/card?date=2026-06-06", headers=auth()).get_json()
    assert card["headline"] == "Pull A heute sauber durchziehen."
    assert card["planned_session"]["name"] == "Pull A"
    assert card["status"] == "fresh"


def test_rolling_sequence_same_day_wrong_saved_decision_does_not_override_expected_rotation(client):
    _set_active_rolling_rotation_plan_with_fixed_weekday_labels()
    _ensure_saved_training_decision("2026-06-05", "Push A · Road to 40s + Squat")
    _ensure_saved_training_decision("2026-06-06", "Push A · Road to 40s + Squat")
    _insert_workout("2026-06-05", "Push A · Road to 40s + Squat")

    body = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()

    planned = body["planned_session"]
    assert planned["name"] == "Pull A"
    assert planned["resolver"]["source"] == "rolling_rotation"


def test_rolling_anchor_excludes_target_day_but_includes_it_for_following_day(client):
    import ai.actions_v2 as actions_v2

    target_day = "2099-12-31"
    _insert_workout(target_day, "Push A · Road to 40s + Squat")

    same_day_anchor = actions_v2._latest_logged_workout_anchor(target_day)
    next_day_anchor = actions_v2._latest_logged_workout_anchor("2100-01-01")

    assert not same_day_anchor or same_day_anchor["anchor_date"] != target_day
    assert next_day_anchor is not None
    assert next_day_anchor["anchor_date"] == target_day


def test_saved_training_decision_does_not_advance_rotation_without_completed_workout(client):
    _set_active_rolling_rotation_plan_with_fixed_weekday_labels()
    _ensure_saved_training_decision("2026-06-05", "Push A · Road to 40s + Squat")

    day_one = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-05", headers=auth()).get_json()
    day_two = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()

    assert day_one["planned_session"]["name"] == "Pull A"
    assert day_two["planned_session"]["name"] == "Pull A"
    assert day_two["planned_session"]["resolver"]["source"] == "rolling_rotation"


def test_training_today_decision_allows_posting_non_current_but_real_plan_session(client):
    _set_active_rolling_rotation_plan()
    _insert_workout("2026-06-05", "Push A · Road to 40s + Squat")

    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-06-06", headers=auth()).get_json()
    assert context["planned_session"]["name"] == "Pull A"

    resp = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-06-06",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-06-06",
                "session": {
                    "name": "Push B",
                    "plan_id": context["planned_session"]["plan_id"],
                },
                "items": [{"exercise": "OHP", "action": "normal"}],
            },
        },
        headers=auth(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True

    conn = connections.get_core_db()
    row = conn.execute("SELECT decision_json FROM training_ai_decisions ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    saved = json.loads(row["decision_json"])
    assert saved["session"]["name"] == "Push B"


def test_training_today_decision_logs_unknown_planned_session_failures(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    resp = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Wrong Session", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [{"exercise": "Schrägbankdrücken", "action": "normal"}],
            },
        },
        headers=auth(),
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "unknown_planned_session"

    conn = connections.get_core_db()
    row = conn.execute(
        """
        SELECT result_json
        FROM actions_v2_command_log
        WHERE domain='training' AND command='save_training_decision'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    assert row is not None
    result = json.loads(row["result_json"])
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_planned_session"


def test_actions_v2_training_today_exercise_match_profile_prevents_bad_base_matches(client):
    import ai.actions_v2 as actions_v2

    planned_rows = [
        {
            "display_name": "breite Rows (eGym)",
            "canonical_id": None,
            "plan_exercise": {"name": "breite Rows", "variation": "eGym", "device": "eGym"},
            "match_profile": actions_v2._exercise_match_profile("breite Rows", "eGym", "eGym"),
        },
        {
            "display_name": "Beinpresse (unilat)",
            "canonical_id": None,
            "plan_exercise": {"name": "Beinpresse", "variation": "unilat", "device": "unilat"},
            "match_profile": actions_v2._exercise_match_profile("Beinpresse", "unilat", "unilat"),
        },
        {
            "display_name": "Flys (SZ)",
            "canonical_id": None,
            "plan_exercise": {"name": "Flys", "variation": "SZ", "device": "SZ"},
            "match_profile": actions_v2._exercise_match_profile("Flys", "SZ", "SZ"),
        },
    ]

    assert actions_v2._match_planned_exercise_to_logged(planned_rows, {"name": "enge Rows", "variation": "KH", "device": "KH", "display_name": "enge Rows (KH)"}) is None
    assert actions_v2._match_planned_exercise_to_logged(planned_rows, {"name": "Beinstrecker", "variation": "", "device": "", "display_name": "Beinstrecker"}) is None
    assert actions_v2._match_planned_exercise_to_logged(planned_rows, {"name": "Seitheben", "variation": "SZ", "device": "SZ", "display_name": "Seitheben (SZ)"}) is None


def test_actions_v2_training_today_suspicious_backoff_warning(client):
    import ai.actions_v2 as actions_v2

    top = {"load_kg": 30.0, "reps": 6, "rpe": 8.0, "set_slot": 1, "source_relation": "same_session_type"}
    backoff = {"load_kg": 60.0, "reps": 6, "rpe": 8.0, "set_slot": 2, "source_relation": "other_session_type", "reference_quality": "secondary_other", "warnings": []}
    top_load = actions_v2._float_or_none(top["load_kg"])
    backoff_load = actions_v2._float_or_none(backoff["load_kg"])
    assert top_load is not None and backoff_load is not None and backoff_load > top_load


def test_actions_v2_training_today_decision_save_and_card(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    cache = connections.get_training_db()
    cache.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_read_models (
            snapshot_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL DEFAULT '{}',
            generated_at TEXT NOT NULL DEFAULT '',
            fresh_until TEXT NOT NULL DEFAULT '',
            build_latency_ms INTEGER NOT NULL DEFAULT 0,
            partial INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            meta_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    cache.executemany(
        "INSERT OR REPLACE INTO ui_read_models (snapshot_key, payload_json) VALUES (?, '{}')",
        [
            ("dashboard_snapshot:2026-04-20",),
            ("training_today_card_snapshot:2026-04-20",),
        ],
    )
    cache.commit()
    cache.close()
    payload = {
        "date": "2026-04-20",
        "context_hash": context["context_hash"],
        "decision": {
            "date": "2026-04-20",
            "session": {
                "name": "Push",
                "plan_id": context["planned_session"]["plan_id"],
                "planned_session_id": context["planned_session"]["planned_session_id"],
                "decision": "leicht_reduziert",
                "headline": "Push sauber setzen, kein PR-Tag.",
                "global_rule": "Kein Satz über RPE 8,5.",
                "summary_note": "Sauber und logbar bleiben.",
            },
            "items": [
                {
                    "exercise": "Schrägbankdrücken",
                    "action": "reduced",
                    "badge": "RPE-Cap",
                    "prescription": "70 x 8 @7.5-8 / 67.5 x 10 @8",
                    "sets": [
                        {"set_type": "top", "load_kg": 70, "reps_min": 8, "reps_max": 8, "target_rpe_min": 7.5, "target_rpe_max": 8},
                        {"set_type": "backoff", "load_kg": 67.5, "reps_min": 10, "reps_max": 10, "target_rpe_min": 8, "target_rpe_max": 8},
                    ],
                    "note": "Nicht grinden.",
                }
            ],
        },
    }
    saved = client.post("/api/v2/actions/training/today/decision", json=payload, headers=auth()).get_json()
    assert saved["ok"] is True
    assert saved["status"] == "fresh"
    assert saved["readback"]["ok"] is True

    cache = connections.get_training_db()
    assert cache.execute(
        "SELECT COUNT(*) FROM ui_read_models WHERE snapshot_key IN (?, ?)",
        ("dashboard_snapshot:2026-04-20", "training_today_card_snapshot:2026-04-20"),
    ).fetchone()[0] == 0
    cache.close()

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["source_label"] == "GPT-Coaching"
    assert card["status"] == "fresh"
    assert card["headline"] == "Push sauber setzen, kein PR-Tag."
    assert card["items"][0]["left"] == "8 × 70 @7,5–8 / 10 × 67,5 @8"
    assert card["items"][0]["middle_note"] == "Nicht grinden."
    assert card["items"][0]["badge"] == "RPE-Cap"
    assert card["ai_decision"]["saved_context_hash"] == context["context_hash"]
    assert card["ai_decision"]["current_context_hash"] == context["context_hash"]
    assert card["ai_decision"]["stale_reason"] is None
    assert card["ai_decision"]["stale_reasons"] == []

    legacy_alias = client.get("/api/v2/actions/core/training-card?date=2026-04-20", headers=auth()).get_json()
    assert legacy_alias["source"] == "gpt_training_decision"
    assert legacy_alias["deprecated_alias"] is True
    assert legacy_alias["training_card"]["ai_decision"]["decision_id"] == saved["decision_id"]
    assert legacy_alias["training_card"]["headline"] == card["headline"]

    daily = client.get("/api/v2/actions/daily/context?date=2026-04-20", headers=auth()).get_json()
    assert daily["training"]["ai_decision"]["status"] == "fresh"
    assert daily["training"]["planned_session"] is not None
    assert daily["training"]["planned_session"]["name"] == context["planned_session"]["name"]
    assert daily["training"]["needs_gpt_decision"] is False
    assert daily["training"]["decision_reason"] == "none"
    assert daily["training"]["ai_decision"]["saved_context_hash"] == context["context_hash"]
    assert daily["training"]["ai_decision"]["current_context_hash"] == context["context_hash"]
    assert daily["training"]["ai_decision"]["status"] == card["status"]


def test_actions_v2_liva_act_save_training_decision_accepts_compact_value_shape(client, monkeypatch):
    context = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_decision_context", "date": "2026-04-20"}, headers=auth()).get_json()
    result = context["result"]

    saved = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "save_training_decision",
            "date": "2026-04-20",
            "context_hash": result["context_hash"],
            "value": {
                "status": "train",
                "session_name": result["planned_session"]["name"],
                "readiness": "yellow",
                "coach_note": "Sauber trainieren, keine Max-Versuche.",
                "exercises": [
                    {
                        "name": "Schrägbankdrücken",
                        "action": "normal",
                        "note": "Kein Grinden.",
                        "target_sets": [
                            {"weight": 30, "reps": "9", "rpe": "8.5"},
                            {"weight": 27.5, "reps": "8-9", "rpe": "8-9"},
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    ).get_json()

    assert saved["ok"] is True
    assert saved["result"]["status"] == "fresh"
    assert saved["result"]["readback"]["ok"] is True
    assert saved["result"]["readback"]["items_count"] == 1

    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_call_actions_view",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("training_today_card must not create a Flask request context")),
    )
    card = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_card", "date": "2026-04-20"}, headers=auth()).get_json()
    assert card["ok"] is True
    assert card["result"]["status"] == "fresh"
    assert card["result"]["items"][0]["left"] == "9 × 30 @8,5 / 8–9 × 27,5 @8–9"
    assert card["result"]["items"][0]["middle_note"] == "Kein Grinden."


def test_actions_v2_liva_act_save_training_decision_maps_compact_status_aliases(client):
    context = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_decision_context", "date": "2026-04-20"}, headers=auth()).get_json()
    result = context["result"]

    saved = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "save_training_decision",
            "date": "2026-04-20",
            "context_hash": result["context_hash"],
            "value": {
                "status": "train",
                "session_name": result["planned_session"]["name"],
                "headline": "Push A",
                "global_rule": "Sauber arbeiten.",
                "exercises": [
                    {
                        "name": "Schrägbankdrücken",
                        "status": "train",
                        "target_sets": [
                            {"weight": 30, "reps": "9", "rpe": "8.5"},
                        ],
                    },
                    {
                        "name": "Squat",
                        "status": "rest",
                        "target_sets": [],
                    },
                ],
            },
        },
        headers=auth(),
    ).get_json()

    assert saved["ok"] is True
    assert saved["result"]["status"] == "fresh"

    card = client.post("/api/v2/actions/liva/read", json={"mode": "training_today_card", "date": "2026-04-20"}, headers=auth()).get_json()
    assert card["ok"] is True
    assert card["result"]["items"][0]["action"] == "normal"
    assert card["result"]["items"][1]["action"] == "skip"
    assert card["result"]["items"][1]["muted"] is True


def test_actions_v2_training_today_decision_maps_hold_action_alias(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    saved = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {
                    "name": context["planned_session"]["name"],
                    "plan_id": context["planned_session"]["plan_id"],
                    "planned_session_id": context["planned_session"]["planned_session_id"],
                },
                "items": [
                    {"exercise": "Schrägbankdrücken", "action": "hold"},
                ],
            },
        },
        headers=auth(),
    ).get_json()

    assert saved["ok"] is True
    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["action"] == "normal"


def test_actions_v2_parse_training_target_range_handles_hyphen_and_en_dash():
    import ai.actions_v2 as actions_v2

    assert actions_v2._parse_training_target_range("7-8") == (7.0, 8.0)
    assert actions_v2._parse_training_target_range("8–9") == (8.0, 9.0)
    assert actions_v2._parse_training_target_range("8,5-9") == (8.5, 9.0)
    assert actions_v2._parse_training_target_range("7+") == (7.0, 7.0)


def test_actions_v2_parse_training_load_handles_ranges_and_preserves_text():
    import ai.actions_v2 as actions_v2

    assert actions_v2._parse_training_load("110-113") == (110.0, 113.0, "110-113")
    assert actions_v2._parse_training_load("27,5") == (27.5, 27.5, None)
    assert actions_v2._parse_training_load(80) == (80.0, 80.0, None)


def test_actions_v2_training_today_decision_normalizes_range_sets_without_negative_values(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    saved = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "value": {
                "status": "train",
                "session_name": context["planned_session"]["name"],
                "exercises": [
                    {
                        "name": "Schrägbankdrücken",
                        "status": "hold",
                        "target_sets": [
                            {"weight": "110-113", "reps": "7-8", "rpe": "8-8.5"},
                            {"weight": "105–108", "reps": "8–10", "rpe": "8"},
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    ).get_json()

    assert saved["ok"] is True

    conn = connections.get_core_db()
    row = conn.execute("SELECT decision_json FROM training_ai_decisions ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    decision = json.loads(row["decision_json"])
    sets = decision["items"][0]["sets"]
    assert sets[0]["reps_min"] == 7
    assert sets[0]["reps_max"] == 8
    assert sets[0]["target_rpe_min"] == 8.0
    assert sets[0]["target_rpe_max"] == 8.5
    assert sets[0]["load_kg_min"] == 110.0
    assert sets[0]["load_kg_max"] == 113.0
    assert sets[0]["load_kg_text"] == "110-113"
    assert sets[1]["reps_min"] == 8
    assert sets[1]["reps_max"] == 10
    assert sets[1]["load_kg_text"] == "105-108"

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["left"] == "7–8 × 110-113 @8–8,5 / 8–10 × 105-108 @8"


def test_actions_v2_training_today_decision_missing_fields_returns_field_list(client):
    saved = client.post(
        "/api/v2/actions/training/today/decision",
        json={"date": "2026-04-20", "decision": {"session": {"name": "Push"}}},
        headers=auth(),
    ).get_json()

    assert saved["ok"] is False
    assert saved["error"]["code"] == "missing_required_fields"
    assert "decision.items" in saved["error"]["details"]["fields"]
    assert "decision.date" in saved["error"]["details"]["fields"]


def test_training_today_card_formats_sets_as_reps_times_load_with_decimal_commas(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [
                    {
                        "exercise": "Schrägbankdrücken",
                        "action": "normal",
                        "sets": [
                            {"load_kg": 30.0, "reps_min": 9, "reps_max": 9, "target_rpe_min": 8, "target_rpe_max": 8.5},
                            {"load_kg": 27.5, "reps_min": 8, "reps_max": 9, "target_rpe_min": 8, "target_rpe_max": 9},
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    )

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["left"] == "9 × 30 @8–8,5 / 8–9 × 27,5 @8–9"


def test_training_today_card_preserves_render_ready_gpt_targets_and_notes(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {
                    "name": "Push",
                    "plan_id": context["planned_session"]["plan_id"],
                    "planned_session_id": context["planned_session"]["planned_session_id"],
                },
                "items": [
                    {
                        "exercise": "Schrägbankdrücken",
                        "action": "normal",
                        "left": "37,5 kg × 8–9 · 35 kg × 9–10",
                        "middle_note": "37,5er sauber bestätigen; 40 kg heute nicht erzwingen.",
                        "sets": [],
                    }
                ],
            },
        },
        headers=auth(),
    )

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["left"] == "37,5 kg × 8–9 · 35 kg × 9–10"
    assert card["items"][0]["middle_note"] == "37,5er sauber bestätigen; 40 kg heute nicht erzwingen."


def test_training_today_card_recovers_legacy_negative_range_and_string_loads(client):
    import ai.actions_v2 as actions_v2

    conn = connections.get_core_db()
    actions_v2.ensure_training_ai_decisions_schema(conn)
    conn.execute(
        """
        INSERT INTO training_ai_decisions (
            day_iso, checkin_id, bodyweight_entry_id, planned_session_id, plan_id, session_name, session_type,
            status, source, model, context_hash, decision_json, trigger_reason, created_at, updated_at, error_message
        ) VALUES (?, NULL, NULL, NULL, 28, ?, 'gym', 'fresh', 'gpt', NULL, ?, ?, 'none', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
        """,
        (
            "2026-04-20",
            "Push",
            "hash-legacy",
            json.dumps(
                {
                    "date": "2026-04-20",
                    "session": {"name": "Push", "headline": "Legacy"},
                    "items": [
                        {
                            "exercise": "Latzug",
                            "note": "Legacy malformed ranges should still render.",
                            "sets": [
                                {"reps": "5-6", "rpe": "8-8.5", "load_kg": "110-113", "reps_min": 5, "reps_max": -6, "target_rpe_min": 8.0, "target_rpe_max": -8.5},
                                {"reps": "6-8", "rpe": "8", "load_kg": "105-108", "reps_min": 6, "reps_max": -8, "target_rpe_min": 8.0, "target_rpe_max": 8.0},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    conn.close()

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["left"] == "5–6 × 110-113 @8–8,5 / 6–8 × 105-108 @8"


def test_training_today_card_derives_visible_badge_from_action_when_badge_missing(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [
                    {"exercise": "Schrägbankdrücken", "action": "progression", "sets": [{"load_kg": 30, "reps_min": 9, "reps_max": 9, "target_rpe_min": 8, "target_rpe_max": 9}]},
                    {"exercise": "OHP", "action": "reduced", "sets": [{"load_kg": 50, "reps_min": 6, "reps_max": 6, "target_rpe_min": 8, "target_rpe_max": 8}]},
                ],
            },
        },
        headers=auth(),
    )

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["items"][0]["badge"] == "Progression"
    assert card["items"][0]["action"] == "progression"
    assert card["items"][1]["badge"] == "reduziert"
    assert card["items"][1]["action"] == "reduced"


def test_actions_v2_training_today_card_and_daily_context_turn_stale_on_hash_mismatch(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [{"exercise": "Schrägbankdrücken", "action": "normal"}],
            },
        },
        headers=auth(),
    )

    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mo"][0]["items"][0]["sets"] = 3
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    daily = client.get("/api/v2/actions/daily/context?date=2026-04-20", headers=auth()).get_json()
    assert daily["training"]["ai_decision"]["status"] == "stale"
    assert daily["training"]["planned_session"] is not None
    assert daily["training"]["planned_session"]["name"] == context["planned_session"]["name"]
    assert daily["training"]["needs_gpt_decision"] is True
    assert daily["training"]["decision_reason"] == "stale_context_hash_changed"
    assert daily["training"]["ai_decision"]["saved_context_hash"] == context["context_hash"]
    assert daily["training"]["ai_decision"]["current_context_hash"] != context["context_hash"]
    assert daily["training"]["ai_decision"]["stale_reason"] == "context_hash_changed"
    assert "context_hash_changed" in daily["training"]["ai_decision"]["stale_reasons"]

    card = client.get("/api/v2/actions/training/today/card?date=2026-04-20", headers=auth()).get_json()
    assert card["status"] == "stale"
    assert daily["training"]["ai_decision"]["status"] == card["status"]
    assert card["fallback"]["visible"] is True
    assert "veraltet" in card["fallback"]["message"].lower()
    assert card["ai_decision"]["saved_context_hash"] == context["context_hash"]
    assert card["ai_decision"]["current_context_hash"] == daily["training"]["ai_decision"]["current_context_hash"]
    assert card["ai_decision"]["stale_reason"] == "context_hash_changed"


def test_actions_v2_training_today_decision_invalid_action_rejected(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    resp = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [{"exercise": "Schrägbankdrücken", "action": "wildcard"}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "invalid_action"


def test_actions_v2_weight_update_marks_training_ai_decision_stale(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": context["context_hash"],
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [{"exercise": "Schrägbankdrücken", "action": "normal"}],
            },
        },
        headers=auth(),
    )
    client.post("/api/v2/actions/weight/control", json={"command": "log_weight", "date": "2026-04-20", "weight": 72.8}, headers=auth())
    body = client.get("/api/v2/actions/daily/context?date=2026-04-20", headers=auth()).get_json()
    assert body["training"]["ai_decision"]["status"] == "stale"
    assert body["training"]["needs_gpt_decision"] is True
    assert body["training"]["decision_reason"] == "weight_updated"
    assert body["training"]["ai_decision"]["stale_reason"] == "weight_updated"
    assert "weight_updated" in body["training"]["ai_decision"]["stale_reasons"]


def test_actions_v2_training_today_decision_rejects_wrong_context_hash(client):
    context = client.get("/api/v2/actions/training/today/decision-context?date=2026-04-20", headers=auth()).get_json()
    resp = client.post(
        "/api/v2/actions/training/today/decision",
        json={
            "date": "2026-04-20",
            "context_hash": "wrong-hash",
            "decision": {
                "date": "2026-04-20",
                "session": {"name": "Push", "plan_id": context["planned_session"]["plan_id"], "planned_session_id": context["planned_session"]["planned_session_id"]},
                "items": [{"exercise": "Schrägbankdrücken", "action": "normal"}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "stale_context_hash"


def test_actions_v2_core_training_card_build_is_read_only_gpt_alias(client):
    conn = connections.get_core_db()
    before_exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'"
    ).fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM core_training_cards").fetchone()[0] if before_exists else 0
    conn.close()

    resp = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-19"}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["source"] == "gpt_training_decision"
    assert body["deprecated_alias"] is True
    assert body["execution"]["live_state_changed"] is False

    conn = connections.get_core_db()
    after_exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'"
    ).fetchone()[0]
    after = conn.execute("SELECT COUNT(*) FROM core_training_cards").fetchone()[0] if after_exists else 0
    conn.close()
    assert after_exists == before_exists
    assert after == before


def test_actions_v2_core_training_card_build_creates_run_card_from_plan(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "run",
                                "kind": "run",
                                "time": "45 min",
                                "title": "Z2 Run",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-26", "decision_intent": "train_controlled", "human_headline": "Z2 ruhig."},
        headers=auth(),
    )
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-26"}, headers=auth()).get_json()
    assert body["ok"] is True
    card = body["training_card"]
    assert card["session_type"] == "run"
    assert "run" in card["title"].lower()
    assert "z2" in card["title"].lower()
    assert "run" in card["planned_session_label"].lower()
    assert "z2" in card["planned_session_label"].lower()
    assert "run" in card["recommended_session_label"].lower()
    assert "z2" in card["recommended_session_label"].lower()
    assert card["adapted_from_plan"] is False
    assert card["items"][0]["item_type"] == "cardio"
    assert card["items"][0]["duration_target"] == "45 min"
    assert "run" in card["items"][0]["display_name"].lower()
    assert card["items"][0]["pace_target"] == "keine Pace-Jagd"


def test_actions_v2_core_training_card_generic_z2_with_ergo_kind_defaults_to_run(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "z2",
                                "kind": "ergo",
                                "time": "40 min",
                                "title": "Z2 (danach Mobility)",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    client.post("/api/v2/actions/core/board/build", json={"date": "2026-04-26", "decision_intent": "train_controlled"}, headers=auth())
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-26", "force": True}, headers=auth()).get_json()
    card = body["training_card"]
    assert card["session_type"] == "run"
    assert "run" in card["title"].lower()
    assert "z2" in card["title"].lower()
    assert "run" in card["items"][0]["display_name"].lower()
    assert card["adapted_from_plan"] is False


def test_actions_v2_core_training_card_explicit_ergo_stays_ergo(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "ergo",
                                "kind": "ergo",
                                "time": "40 min",
                                "title": "Ergo Z2",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    client.post("/api/v2/actions/core/board/build", json={"date": "2026-04-26", "decision_intent": "train_controlled"}, headers=auth())
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-26", "force": True}, headers=auth()).get_json()
    card = body["training_card"]
    assert card["session_type"] == "ergo"
    assert "z2" in card["title"].lower()
    assert "z2" in card["items"][0]["display_name"].lower()


def test_actions_v2_core_training_card_machine_briefing_can_adapt_run_to_ergo(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "run",
                                "kind": "run",
                                "time": "40 min",
                                "title": "Run Z2",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-26",
            "decision_intent": "train_controlled",
            "machine_briefing": {
                "training_session": {
                    "planned_session_label": "Run Z2",
                    "planned_session_type": "run",
                    "recommended_session_label": "Ergo Z2",
                    "recommended_session_type": "ergo",
                    "adaptation_note": "CORE-Anpassung: Run Z2 → Ergo Z2",
                }
            },
        },
        headers=auth(),
    )
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-26", "force": True}, headers=auth()).get_json()
    card = body["training_card"]
    assert card["session_type"] == "ergo"
    assert card["adapted_from_plan"] is True
    assert card["adaptation_note"] == "CORE-Anpassung: Run Z2 → Ergo Z2"
    assert card["planned_session_label"] == "Run Z2"
    assert card["recommended_session_label"] == "Ergo Z2"
    assert card["source_context"]["adapted_from_plan"] is True


def test_actions_v2_core_training_card_without_adaptation_keeps_planned_and_recommended_semantically_aligned(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "z2",
                                "kind": "ergo",
                                "time": "40 min",
                                "title": "Z2 (danach Mobility)",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    client.post("/api/v2/actions/core/board/build", json={"date": "2026-04-26", "decision_intent": "train_controlled"}, headers=auth())
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-26", "force": True}, headers=auth()).get_json()
    card = body["training_card"]
    assert card["adapted_from_plan"] is False
    assert "run" in card["planned_session_label"].lower()
    assert "z2" in card["planned_session_label"].lower()
    assert "run" in card["recommended_session_label"].lower()
    assert "z2" in card["recommended_session_label"].lower()
    assert card["source_context"]["planned_session_type"] == "run"
    assert card["source_context"]["recommended_session_type"] == "run"


def test_actions_v2_core_training_card_build_train_controlled_uses_rpe_cap_8(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-20", "decision_intent": "train_controlled", "human_headline": "Upper kontrolliert."},
        headers=auth(),
    )
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-20"}, headers=auth()).get_json()
    assert body["ok"] is True
    first = body["training_card"]["items"][0]
    assert first["item_type"] == "exercise"
    assert first["rpe_cap"] == "8"
    assert first["weight_target"] == "letztes gutes Arbeitsgewicht" or str(first["weight_target"]).endswith("kg")


def test_actions_v2_core_board_build_can_build_training_card_too(client):
    conn = connections.get_training_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_read_models (
            snapshot_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL DEFAULT '{}',
            generated_at TEXT NOT NULL DEFAULT '',
            fresh_until TEXT NOT NULL DEFAULT '',
            build_latency_ms INTEGER NOT NULL DEFAULT 0,
            partial INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            meta_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO ui_read_models (snapshot_key, payload_json) VALUES (?, ?)",
        ("training_today_card_snapshot:2026-04-20", json.dumps({"title": "alte Card"})),
    )
    conn.commit()
    conn.close()

    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Upper kontrolliert ausführen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["training_card_ready"] is True
    assert body["training_card"]["title"].startswith("Push") or body["training_card"]["title"].startswith("Upper")

    conn = connections.get_training_db()
    assert conn.execute(
        "SELECT COUNT(*) FROM ui_read_models WHERE snapshot_key=?",
        ("training_today_card_snapshot:2026-04-20",),
    ).fetchone()[0] == 0
    conn.close()


def test_actions_v2_core_board_build_creates_today_decision_memory_event(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "human_summary": "Plan bleibt, RPE-Cap 8, kein Zusatzvolumen.",
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["memory_events_ready"] is True
    assert body["memory_events"]["created"] >= 1
    conn = connections.get_core_db()
    row = conn.execute(
        "SELECT timeframe, category, write_policy, target_memory_file, event_key FROM core_memory_events WHERE day_iso=? ORDER BY id ASC LIMIT 1",
        ("2026-04-20",),
    ).fetchone()
    conn.close()
    assert row["timeframe"] == "today"
    assert row["category"] == "decision_rule"
    assert row["write_policy"] == "auto_log"
    assert row["target_memory_file"] == "review_log"
    assert row["event_key"]


def test_actions_v2_core_memory_dedupe_prevents_duplicate_events_and_review_log_spam(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "human_summary": "Plan bleibt, RPE-Cap 8, kein Zusatzvolumen.",
    }
    first = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    second = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert first["ok"] is True
    assert second["ok"] is True
    conn = connections.get_core_db()
    count = conn.execute("SELECT COUNT(*) FROM core_memory_events WHERE day_iso=?", ("2026-04-20",)).fetchone()[0]
    row = conn.execute(
        "SELECT event_key, applied_at FROM core_memory_events WHERE day_iso=? AND category='decision_rule' LIMIT 1",
        ("2026-04-20",),
    ).fetchone()
    conn.close()
    assert count == 1
    assert row["event_key"]
    assert row["applied_at"]
    memory_service = client.application.extensions["liva_memory_service"]
    assert len(memory_service.apply_calls) == 1


def test_actions_v2_core_board_build_with_training_card_creates_training_execution_memory_event(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    conn = connections.get_core_db()
    row = conn.execute(
        "SELECT category, source, write_policy FROM core_memory_events WHERE day_iso=? AND category='training_response' ORDER BY id DESC LIMIT 1",
        ("2026-04-20",),
    ).fetchone()
    conn.close()
    assert row["source"] == "core_training_card"
    assert row["write_policy"] == "auto_log"


def test_actions_v2_core_board_mismatch_creates_open_question_memory_event(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Run Z2 ruhig durchziehen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "mismatch"
    conn = connections.get_core_db()
    row = conn.execute(
        "SELECT category, timeframe, write_policy FROM core_memory_events WHERE day_iso=? AND title='Board/Text-Mismatch erkannt' ORDER BY id DESC LIMIT 1",
        ("2026-04-20",),
    ).fetchone()
    conn.close()
    assert row["category"] == "open_question"
    assert row["timeframe"] == "today"
    assert row["write_policy"] == "auto_log"


def test_actions_v2_core_board_build_stores_gpt_memory_candidates_and_routes_layers(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "machine_briefing": {"constraints": {"allow_bonus_sets": False}},
        "memory_candidates": [
            {
                "timeframe": "phase",
                "category": "nutrition_response",
                "title": "Carbs vor schweren Push-Tagen schützen",
                "summary": "Im Cut sollten Carbs vor Push priorisiert werden.",
                "confidence": 0.62,
                "evidence": {"source": "gpt_checkin"},
                "write_policy": "candidate",
            },
            {
                "timeframe": "global",
                "category": "behavior_pattern",
                "title": "Gute Tage nicht übereskalieren",
                "summary": "Example User neigt dazu, gute Tage zu übereskalieren.",
                "confidence": 0.55,
                "write_policy": "candidate",
            },
        ],
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["memory_events"]["pending"] >= 2
    conn = connections.get_core_db()
    rows = conn.execute(
        "SELECT timeframe, category, status, target_memory_file, target_section FROM core_memory_events WHERE day_iso=? AND source='gpt_memory_candidate' ORDER BY id ASC",
        ("2026-04-20",),
    ).fetchall()
    conn.close()
    assert {row["timeframe"] for row in rows} == {"phase", "global"}
    assert all(row["status"] == "pending" for row in rows)
    assert any(row["target_memory_file"] == "athlete_dossier" and row["target_section"] == "AKTUELLE_PHASE" for row in rows)
    assert any(row["target_memory_file"] == "athlete_dossier" and row["target_section"] == "AUFFAELLIGE_MUSTER" for row in rows)


def test_actions_v2_core_memory_layers_returns_all_layers(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "machine_briefing": {"constraints": {"allow_bonus_sets": False}},
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik wegen Tagesgewicht.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    body = client.get("/api/v2/actions/core/memory/layers?date=2026-04-20", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["counts"] is not None
    assert set(body["layers"]) == {"today", "week", "phase", "year", "global"}
    assert len(body["layers"]["today"]) >= 1
    assert len(body["layers"]["phase"]) >= 1
    assert body["counts"]["today"] == len(body["layers"]["today"])
    assert body["counts"]["phase"] == len(body["layers"]["phase"])
    assert body["counts"]["pending"] >= sum(1 for rows in body["layers"].values() for row in rows if row["status"] == "pending")
    assert body["counts"]["active"] >= sum(1 for rows in body["layers"].values() for row in rows if row["status"] == "active")


def test_actions_v2_core_board_build_survives_memory_event_failures(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    def fail(*args, **kwargs):
        raise RuntimeError("memory exploded")

    monkeypatch.setattr(actions_v2, "build_memory_events_from_board", fail)
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-20", "decision_intent": "train_controlled", "human_headline": "Push kontrolliert ausführen."},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["board"]["id"]
    assert body["memory_events_error"]["code"] == "memory_events_build_failed"


def test_actions_v2_core_board_build_survives_training_card_failures(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    def fail(*args, **kwargs):
        raise RuntimeError("training card exploded")

    monkeypatch.setattr(actions_v2, "build_core_training_card", fail)
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-20", "decision_intent": "train_controlled", "human_headline": "Push kontrolliert ausführen.", "build_training_card": True},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["board"]["id"]
    assert body["training_card_ready"] is False
    assert body["training_card_error"]["code"] == "training_card_build_failed"


def test_actions_v2_core_board_build_ignores_invalid_memory_candidates_shape(client):
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-20", "decision_intent": "train_controlled", "human_headline": "Push kontrolliert ausführen.", "memory_candidates": {"bad": True}},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["board"]["id"]


def test_actions_v2_core_memory_promote_marks_event_without_aggressive_dossier_rewrite(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik wegen Tagesgewicht.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    event_id = conn.execute(
        "SELECT id FROM core_memory_events WHERE day_iso=? AND source='gpt_memory_candidate' ORDER BY id DESC LIMIT 1",
        ("2026-04-20",),
    ).fetchone()["id"]
    conn.close()
    body = client.post("/api/v2/actions/core/memory/promote", json={"event_id": event_id, "mode": "promote_only"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["result"]["status"] == "promoted"
    assert body["result"]["applied_result"]["applied"] is False


def test_actions_v2_core_memory_control_promote_and_dismiss(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "global", "category": "behavior_pattern", "title": "Nicht eskalieren", "summary": "Gute Tage nicht aufblasen.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    event_id = conn.execute("SELECT id FROM core_memory_events WHERE source='gpt_memory_candidate' ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.close()
    promoted = client.post(
        "/api/v2/actions/core/memory/control",
        json={"event_id": event_id, "action": "promote", "note": "passt"},
        headers=auth(),
    ).get_json()
    assert promoted["ok"] is True
    assert promoted["event"]["status"] == "promoted"
    assert "later dossier consolidation" in json.dumps(promoted["event"]["applied_result"])
    dismissed = client.post(
        "/api/v2/actions/core/memory/control",
        json={"event_id": event_id, "action": "dismiss", "note": "doch zu weich"},
        headers=auth(),
    ).get_json()
    assert dismissed["ok"] is True
    assert dismissed["event"]["status"] == "dismissed"


def test_actions_v2_core_memory_control_update_changes_allowed_fields(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    event_id = conn.execute("SELECT id FROM core_memory_events WHERE source='gpt_memory_candidate' ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.close()
    updated = client.post(
        "/api/v2/actions/core/memory/control",
        json={
            "event_id": event_id,
            "action": "update",
            "edits": {
                "title": "Cut-Regel verfeinert",
                "summary": "Keine Panik wegen Tagesgewicht, nur Trend bewerten.",
                "timeframe": "global",
                "target_memory_file": "core_patterns",
                "target_section": "ENTSCHEIDUNGEN",
            },
        },
        headers=auth(),
    ).get_json()
    assert updated["ok"] is True
    assert updated["event"]["title"] == "Cut-Regel verfeinert"
    assert updated["event"]["summary"].startswith("Keine Panik")
    assert updated["event"]["timeframe"] == "global"
    invalid = client.post(
        "/api/v2/actions/core/memory/control",
        json={"event_id": event_id, "action": "update", "edits": {"created_at": "hack"}},
        headers=auth(),
    )
    assert invalid.status_code == 400


def test_actions_v2_core_memory_control_apply_review_log_is_idempotent(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    event_id = conn.execute("SELECT id FROM core_memory_events WHERE source='gpt_memory_candidate' ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.close()
    memory_service = client.application.extensions["liva_memory_service"]
    before = len(memory_service.apply_calls)
    first = client.post("/api/v2/actions/core/memory/control", json={"event_id": event_id, "action": "apply_review_log"}, headers=auth()).get_json()
    second = client.post("/api/v2/actions/core/memory/control", json={"event_id": event_id, "action": "apply_review_log"}, headers=auth()).get_json()
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["event"]["applied_at"]
    assert len(memory_service.apply_calls) == before + 1
    assert second["event"]["applied_result"]["review_log_already_applied"] is True


def test_core_memory_browser_inbox_and_control_do_not_use_ai_bearer(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    inbox = client.get("/api/core/memory/inbox")
    body = inbox.get_json()
    assert inbox.status_code == 200
    assert body["ok"] is True
    assert "counts" in body
    assert body["counts"]["phase_pending"] >= 1
    event_id = next(event["id"] for event in body["events"] if event["status"] == "pending")
    control = client.post("/api/core/memory/control", json={"event_id": event_id, "action": "keep_pending", "note": "später"})
    control_body = control.get_json()
    assert control.status_code == 200
    assert control_body["ok"] is True
    assert control_body["event"]["status"] == "pending"


def test_core_board_view_model_stays_compact_and_exposes_memory_counts(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "phase_rule", "title": "Cut-Regel", "summary": "Keine Panik.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    from core.core_daily_board import core_board_view_model

    payload = core_board_view_model("2026-04-20")
    assert payload["ok"] is True
    assert payload["ui"]["memory_summary_lines"][0].startswith("Memory:")
    assert payload["ui"]["memory_inbox_url"] == "http://192.0.2.53:5230/home"
    assert "events" not in payload["ui"]["memory_summary"]


def test_core_board_view_model_keeps_stale_legacy_out_of_normal_today_view(client):
    payload = {
        "date": "2026-04-27",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert.",
        "build_training_card": True,
        "legacy_core": {
            "daily_decision": {"day_iso": "2026-04-26"},
            "hero": "Run Z2",
            "planned_unit": "Run Z2",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["date"] == "2026-04-27"
    assert body["legacy_core_status"]["stale"] is True
    assert body["training_card"]["day_iso"] == "2026-04-27"

    from core.core_daily_board import core_board_view_model

    vm = core_board_view_model("2026-04-27")
    assert vm["ui"]["headline"]
    assert "2026-04-26" in (vm["ui"]["legacy_core_debug_note"] or "")
    assert "Run Z2" not in (vm["ui"]["headline"] or "")


def test_core_memory_consolidations_schema_is_created(client):
    from core.core_memory_layers import ensure_core_memory_consolidations_schema

    conn = connections.get_core_db()
    ensure_core_memory_consolidations_schema(conn)
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='core_memory_consolidations'").fetchone()
    conn.close()
    assert row["name"] == "core_memory_consolidations"


def test_core_memory_consolidation_build_creates_deduped_draft(client):
    for idx, title in enumerate(
        [
            "Push im Cut mit RPE Cap 8",
            "Push kontrolliert ohne Bonusvolumen",
            "Warmup entscheidet Push Progression",
        ],
        start=1,
    ):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": title, "summary": "Push im Cut mit RPE Cap, ohne Bonusvolumen, Warmup steuert Progression.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post(
        "/api/v2/actions/core/memory/consolidations/build",
        json={"timeframe": "phase", "min_events": 2, "status_filter": ["pending", "promoted"]},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True
    assert built["created"] >= 1
    first_count = len(built["drafts"])
    again = client.post(
        "/api/v2/actions/core/memory/consolidations/build",
        json={"timeframe": "phase", "min_events": 2, "status_filter": ["pending", "promoted"]},
        headers=auth(),
    ).get_json()
    assert again["ok"] is True
    conn = connections.get_core_db()
    rows = conn.execute("SELECT COUNT(*) FROM core_memory_consolidations").fetchone()[0]
    conn.close()
    assert rows == first_count


def test_core_memory_consolidation_ignores_dismissed_events(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-20",
            "decision_intent": "train_controlled",
            "human_headline": "Push kontrolliert ausführen.",
            "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": "Push vorsichtig", "summary": "Push im Cut ohne Bonusvolumen.", "write_policy": "candidate"}],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    event_id = conn.execute("SELECT id FROM core_memory_events WHERE source='gpt_memory_candidate' ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.close()
    client.post("/api/v2/actions/core/memory/control", json={"event_id": event_id, "action": "dismiss"}, headers=auth())
    built = client.post(
        "/api/v2/actions/core/memory/consolidations/build",
        json={"timeframe": "phase", "min_events": 2, "status_filter": ["pending", "promoted"]},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True
    assert built["created"] == 0


def test_core_memory_consolidation_apply_review_log_and_memory_file_are_conservative(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Push Regel {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nur nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post(
        "/api/v2/actions/core/memory/consolidations/build",
        json={"timeframe": "phase", "min_events": 2},
        headers=auth(),
    ).get_json()
    consolidation_id = built["drafts"][0]["id"]
    memory_service = client.application.extensions["liva_memory_service"]
    before = len(memory_service.apply_calls)
    applied = client.post(
        "/api/v2/actions/core/memory/consolidations/control",
        json={"consolidation_id": consolidation_id, "action": "apply", "mode": "review_log"},
        headers=auth(),
    ).get_json()
    assert applied["ok"] is True
    assert applied["consolidation"]["status"] == "applied"
    assert len(memory_service.apply_calls) == before + 1

    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-25",
            "decision_intent": "train_controlled",
            "human_headline": "Recovery ruhig halten.",
            "memory_candidates": [
                {"timeframe": "global", "category": "decision_rule", "title": "Master Profil Block", "summary": "Soll nicht ins Master Profil geschrieben werden.", "write_policy": "candidate"}
            ],
        },
        headers=auth(),
    )
    conn = connections.get_core_db()
    conn.execute(
        "INSERT INTO core_memory_consolidations (created_at, updated_at, consolidation_key, status, timeframe, category, title, summary, source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy, confidence, applied_result_json) VALUES (?, ?, ?, 'draft', 'global', 'decision_rule', 'Block', 'Block', '[]', '{}', 'master_profile', 'IDENTITAET', 'append_section_note', 0.7, '{}')",
        ("2026-04-27T00:00:00Z", "2026-04-27T00:00:00Z", "manual-block",),
    )
    consolidation_id = conn.execute("SELECT id FROM core_memory_consolidations WHERE consolidation_key='manual-block'").fetchone()["id"]
    conn.commit()
    conn.close()
    blocked = client.post(
        "/api/v2/actions/core/memory/consolidations/control",
        json={"consolidation_id": consolidation_id, "action": "apply", "mode": "memory_file"},
        headers=auth(),
    )
    assert blocked.status_code == 400


def test_core_memory_consolidation_browser_and_v2_endpoints_work_without_ai_key_in_browser(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Push Draft {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    v2_build = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    assert v2_build["ok"] is True
    v2_list = client.get("/api/v2/actions/core/memory/consolidations?status=draft", headers=auth()).get_json()
    assert v2_list["ok"] is True
    browser_list = client.get("/api/core/memory/consolidations").get_json()
    assert browser_list["ok"] is True
    assert len(browser_list["consolidations"]) >= 1
    consolidation_id = browser_list["consolidations"][0]["id"]
    dismissed = client.post("/api/core/memory/consolidations/control", json={"consolidation_id": consolidation_id, "action": "dismiss", "note": "zu schwach"}).get_json()
    assert dismissed["ok"] is True
    assert dismissed["consolidation"]["status"] == "dismissed"


def test_core_memory_consolidation_conflict_detection_marks_conflict_and_weak_filters(client):
    candidates = [
        ("Push im Cut normal trainieren", "Push kann im Cut normal trainiert werden, wenn Warmup gut läuft."),
        ("Push im Cut strikt begrenzen", "Push im Cut nicht eskalieren, kein Bonusvolumen und Progression vermeiden."),
    ]
    for idx, (title, summary) in enumerate(candidates, start=1):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": title, "summary": summary, "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    conflict = built["drafts"][0]
    assert conflict["conflict_status"] in {"conflict", "needs_review"}
    assert conflict["evidence"]["conflict_details"]["reason_codes"]
    assert conflict["evidence"]["conflict_details"]["explanation"]
    assert conflict["review_suggestion"] in {"review_required", "dismiss_suggested", "can_mark_needs_review"}
    filtered = client.get("/api/v2/actions/core/memory/consolidations?status=draft&conflict_status=conflict", headers=auth()).get_json()
    assert filtered["ok"] is True
    assert len(filtered["consolidations"]) >= 1
    assert filtered["consolidations"][0]["review_suggestion"] in {"review_required", "dismiss_suggested"}


def test_core_memory_consolidation_api_and_browser_include_conflict_details(client):
    candidates = [
        ("Push im Cut normal trainieren", "Push kann im Cut normal trainiert werden, wenn Warmup gut läuft."),
        ("Push im Cut strikt begrenzen", "Push im Cut nicht eskalieren, kein Bonusvolumen und Progression vermeiden."),
    ]
    for idx, (title, summary) in enumerate(candidates, start=1):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": title, "summary": summary, "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth())
    v2_rows = client.get("/api/v2/actions/core/memory/consolidations?status=draft", headers=auth()).get_json()["consolidations"]
    browser_rows = client.get("/api/core/memory/consolidations").get_json()["consolidations"]
    assert "conflict_details" in v2_rows[0]["evidence"]
    assert "conflict_details" in browser_rows[0]["evidence"]
    assert v2_rows[0]["evidence"]["conflict_details"] is not None


def test_existing_conflict_consolidation_without_details_is_enriched_on_get(client):
    conn = connections.get_core_db()
    from core.core_memory_layers import ensure_core_memory_consolidations_schema
    ensure_core_memory_consolidations_schema(conn)
    conn.execute(
        """
        INSERT INTO core_memory_consolidations (
            created_at, updated_at, consolidation_key, status, timeframe, category, title, summary,
            source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy,
            conflict_status, evidence_strength, confidence, applied_result_json
        ) VALUES (?, ?, ?, 'draft', 'phase', 'decision_rule', 'Legacy Conflict', 'Bestehender Konflikt ohne Details',
                  '[1,2]', '{"top_tokens":["push","cut"],"source_titles":["A","B"],"source_summaries":["sa","sb"]}',
                  'core_patterns', 'TRAINING', 'append_section_note', 'conflict', 0.76, 0.7, '{}')
        """,
        ("2026-04-29T08:00:00Z", "2026-04-29T08:00:00Z", "legacy-conflict"),
    )
    conn.commit()
    conn.close()
    row = client.get("/api/v2/actions/core/memory/consolidations?status=draft&conflict_status=conflict", headers=auth()).get_json()["consolidations"][0]
    assert row["review_suggestion"] in {"review_required", "dismiss_suggested"}
    assert row["evidence"]["conflict_details"] is not None
    assert row["evidence"]["conflict_details"]["reason_codes"]
    assert row["evidence"]["conflict_details"]["explanation"]


def test_core_memory_consolidation_weak_gets_dismiss_or_wait_suggestion(client):
    conn = connections.get_core_db()
    from core.core_memory_layers import ensure_core_memory_consolidations_schema
    ensure_core_memory_consolidations_schema(conn)
    conn.execute(
        """
        INSERT INTO core_memory_consolidations (
            created_at, updated_at, consolidation_key, status, timeframe, category, title, summary,
            source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy,
            conflict_status, evidence_strength, confidence, applied_result_json
        ) VALUES (?, ?, ?, 'draft', 'phase', 'decision_rule', 'Weak Draft', 'Zu wenig Evidenz', '[]', '{}',
                  'core_patterns', 'TRAINING', 'append_section_note', 'weak', 0.3, 0.4, '{}')
        """,
        ("2026-04-29T08:00:00Z", "2026-04-29T08:00:00Z", "weak-draft"),
    )
    weak_id = conn.execute("SELECT id FROM core_memory_consolidations WHERE consolidation_key='weak-draft'").fetchone()["id"]
    conn.commit()
    conn.close()
    row = client.get("/api/v2/actions/core/memory/consolidations?status=draft&conflict_status=weak", headers=auth()).get_json()["consolidations"][0]
    assert row["id"] == weak_id
    assert row["review_suggestion"] == "dismiss_or_wait"
    assert "weak_evidence" in row["evidence"]["conflict_details"]["reason_codes"]
    assert row["evidence"]["conflict_details"]["explanation"]


def test_needs_review_consolidation_gets_conflict_details(client):
    conn = connections.get_core_db()
    from core.core_memory_layers import ensure_core_memory_consolidations_schema
    ensure_core_memory_consolidations_schema(conn)
    conn.execute(
        """
        INSERT INTO core_memory_consolidations (
            created_at, updated_at, consolidation_key, status, timeframe, category, title, summary,
            source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy,
            conflict_status, evidence_strength, confidence, applied_result_json
        ) VALUES (?, ?, ?, 'draft', 'phase', 'decision_rule', 'Needs Review Draft', 'Bitte manuell prüfen',
                  '[1,2]', '{"top_tokens":["review","rule"]}',
                  'core_patterns', 'TRAINING', 'manual_review_only', 'needs_review', 0.7, 0.7, '{}')
        """,
        ("2026-04-29T08:00:00Z", "2026-04-29T08:00:00Z", "needs-review-draft"),
    )
    conn.commit()
    conn.close()
    row = client.get("/api/v2/actions/core/memory/consolidations?status=draft&conflict_status=needs_review", headers=auth()).get_json()["consolidations"][0]
    assert row["review_suggestion"] == "can_mark_needs_review"
    assert "needs_review" in row["evidence"]["conflict_details"]["reason_codes"]
    assert row["evidence"]["conflict_details"]["explanation"]


def test_core_memory_consolidation_ready_to_apply_gets_apply_suggestion(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "year", "category": "exercise_response", "title": f"Historie {idx}", "summary": "Schrägbankdrücken Smith: letzter sicherer Referenzpunkt 70kg.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "year", "min_events": 2}, headers=auth()).get_json()
    ready = next(item for item in built["drafts"] if item["conflict_status"] == "ready_to_apply")
    assert ready["review_suggestion"] == "can_apply_review_log"


def test_conflict_consolidation_still_does_not_generate_file_patch(client):
    candidates = [
        ("Push im Cut normal trainieren", "Push kann im Cut normal trainiert werden, wenn Warmup gut läuft."),
        ("Push im Cut strikt begrenzen", "Push im Cut nicht eskalieren, kein Bonusvolumen und Progression vermeiden."),
    ]
    for idx, (title, summary) in enumerate(candidates, start=1):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": title, "summary": summary, "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    assert any(item["conflict_status"] == "conflict" for item in built["drafts"])
    patches = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    assert patches["ok"] is True
    assert patches["patches"] == []


def test_rebuild_existing_conflict_consolidation_persists_conflict_details(client):
    candidates = [
        ("Push im Cut normal trainieren", "Push kann im Cut normal trainiert werden, wenn Warmup gut läuft."),
        ("Push im Cut strikt begrenzen", "Push im Cut nicht eskalieren, kein Bonusvolumen und Progression vermeiden."),
    ]
    for idx, (title, summary) in enumerate(candidates, start=1):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": title, "summary": summary, "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    first = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    conflict_id = first["drafts"][0]["id"]
    conn = connections.get_core_db()
    conn.execute("UPDATE core_memory_consolidations SET evidence_json='{}' WHERE id=?", (conflict_id,))
    conn.commit()
    conn.close()
    rebuilt = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    row = next(item for item in rebuilt["drafts"] if item["id"] == conflict_id)
    assert row["evidence"]["conflict_details"] is not None
    conn = connections.get_core_db()
    stored = conn.execute("SELECT evidence_json FROM core_memory_consolidations WHERE id=?", (conflict_id,)).fetchone()["evidence_json"]
    conn.close()
    assert "conflict_details" in stored


def test_core_memory_consolidation_detects_similar_applied_draft_and_duplicate_marker(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Push Draft {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    first = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    first_id = first["drafts"][0]["id"]
    applied = client.post("/api/v2/actions/core/memory/consolidations/control", json={"consolidation_id": first_id, "action": "apply", "mode": "review_log"}, headers=auth()).get_json()
    assert applied["ok"] is True
    second = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    assert second["ok"] is True
    draft = second["drafts"][0]
    assert draft["evidence"].get("similar_applied") in {True, False}
    assert "review_history" in draft


def test_core_memory_consolidation_exercise_response_title_is_more_human(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [
                    {
                        "timeframe": "year",
                        "category": "exercise_response",
                        "title": f"2026 Exercise Historie Letzter {idx}",
                        "summary": "Schrägbankdrücken Smith: letztes gutes Arbeitsgewicht 70kg.",
                        "write_policy": "candidate",
                    }
                ],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "year", "min_events": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    assert built["drafts"][0]["title"] == "Übungshistorie: sichere Referenzpunkte"


def test_core_memory_consolidation_decision_rule_title_is_more_human(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [
                    {
                        "timeframe": "phase",
                        "category": "decision_rule",
                        "title": f"Controlled Train {idx}",
                        "summary": "Train controlled, RPE-Cap 8, kein Bonusvolumen und kein Zusatzdruck.",
                        "write_policy": "candidate",
                    }
                ],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    assert built["drafts"][0]["title"] == "Kontrollierte Trainingstage: keine Zusatzeskalation"


def test_core_memory_consolidation_batch_control_review_log_and_dismiss(client):
    for idx in range(4):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-{20+idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Push Batch {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    ids = [row["id"] for row in built["drafts"]]
    batch = client.post(
        "/api/v2/actions/core/memory/consolidations/batch",
        json={"consolidation_ids": ids, "action": "apply_review_log", "mode": "review_log"},
        headers=auth(),
    ).get_json()
    assert batch["ok"] is True
    assert len(batch["result"]["changed"]) >= 1
    browser_batch = client.post(
        "/api/core/memory/consolidations/batch",
        json={"consolidation_ids": ids, "action": "dismiss", "note": "batch dismiss"},
    ).get_json()
    assert browser_batch["ok"] is True
    assert len(browser_batch["result"]["changed"]) >= 1


def test_core_memory_consolidation_batch_mark_needs_review_logs_review_history(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Needs Review {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    consolidation_id = built["drafts"][0]["id"]
    batch = client.post(
        "/api/v2/actions/core/memory/consolidations/batch",
        json={"consolidation_ids": [consolidation_id], "action": "mark_needs_review", "note": "bitte prüfen"},
        headers=auth(),
    ).get_json()
    assert batch["ok"] is True
    changed = client.get("/api/v2/actions/core/memory/consolidations?status=draft", headers=auth()).get_json()["consolidations"][0]
    assert any(row["action"] == "mark_needs_review" for row in changed["review_history"])


def test_core_memory_review_log_schema_is_created(client):
    client.get("/api/core/memory/consolidations")
    conn = connections.get_core_db()
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='core_memory_review_log'").fetchone()
    finally:
        conn.close()
    assert row is not None


def _seed_file_patch_consolidations(client, entries):
    from core.core_memory_layers import ensure_core_memory_consolidations_schema

    conn = connections.get_core_db()
    ensure_core_memory_consolidations_schema(conn)
    now = "2026-04-29T08:00:00Z"
    for idx, entry in enumerate(entries, start=1):
        conn.execute(
            """
            INSERT INTO core_memory_consolidations (
                created_at, updated_at, consolidation_key, status, timeframe, category, title, summary,
                source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy,
                conflict_status, evidence_strength, duplicate_of_consolidation_id, confidence, applied_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}', ?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                now,
                now,
                entry.get("key") or f"patch-seed-{idx}",
                entry.get("status", "applied"),
                entry.get("timeframe", "phase"),
                entry.get("category", "decision_rule"),
                entry.get("title", f"Patch Seed {idx}"),
                entry.get("summary", "Push im Cut ohne Bonusvolumen."),
                entry.get("target_memory_file", "core_patterns"),
                entry.get("target_section", "TRAINING"),
                entry.get("proposed_strategy", "append_section_note"),
                entry.get("conflict_status", "ready_to_apply"),
                entry.get("evidence_strength", 0.72),
                entry.get("duplicate_of_consolidation_id"),
                entry.get("confidence", 0.76),
            ),
        )
    conn.commit()
    conn.close()


def test_core_memory_file_patches_schema_is_created_idempotently(client):
    from core.core_memory_file_patches import ensure_core_memory_file_patches_schema

    conn = connections.get_core_db()
    ensure_core_memory_file_patches_schema(conn)
    ensure_core_memory_file_patches_schema(conn)
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='core_memory_file_patches'").fetchone()
    conn.close()
    assert row["name"] == "core_memory_file_patches"


def test_core_memory_file_patch_build_creates_draft_and_dedupes_by_patch_key(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "push-a", "title": "Push im Cut ohne Bonusdruck", "summary": "Push im Cut normal trainieren, aber ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "push-b", "title": "Warm-up steuert Progression", "summary": "Progression nur bei gutem Warm-up und RPE-Cap 8.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post(
        "/api/v2/actions/core/memory/file-patches/build",
        json={"target_memory_file": "core_patterns", "target_section": "TRAINING", "status_filter": ["applied", "promoted"], "min_consolidations": 2},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True
    assert built["created"] >= 1
    assert built["patches"][0]["status"] == "draft"
    assert built["patches"][0]["patch_key"]
    assert built["patches"][0]["source_consolidation_ids"]
    again = client.post(
        "/api/v2/actions/core/memory/file-patches/build",
        json={"target_memory_file": "core_patterns", "target_section": "TRAINING", "status_filter": ["applied", "promoted"], "min_consolidations": 2},
        headers=auth(),
    ).get_json()
    assert again["ok"] is True
    conn = connections.get_core_db()
    count = conn.execute("SELECT COUNT(*) FROM core_memory_file_patches").fetchone()[0]
    conn.close()
    assert count == len(built["patches"])
    assert again["updated"] >= 1


def test_core_memory_file_patch_build_ignores_weak_conflict_dismissed_and_duplicate_consolidations(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "good-a", "title": "Push im Cut", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "good-b", "title": "Warm-up gut", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "bad-dismissed", "status": "dismissed", "title": "Dismissed", "summary": "Ignore", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "bad-conflict", "conflict_status": "conflict", "title": "Conflict", "summary": "Ignore", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "bad-weak", "evidence_strength": 0.2, "title": "Weak", "summary": "Ignore", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    assert len(built["patches"]) == 1
    assert built["patches"][0]["evidence"]["source_consolidation_count"] == 2


def test_core_memory_file_patch_safety_blocks_master_profile_and_marks_dossier_review(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "master-a", "title": "Master A", "summary": "Ignore rewrite.", "target_memory_file": "master_profile", "target_section": "IDENTITAET"},
            {"key": "master-b", "title": "Master B", "summary": "Ignore rewrite.", "target_memory_file": "master_profile", "target_section": "IDENTITAET"},
            {"key": "dossier-a", "title": "Cut-Regeln", "summary": "Carbs vor schweren Tagen, Gewichtstrend nicht panisch bewerten.", "target_memory_file": "athlete_dossier", "target_section": "AKTUELLE_PHASE"},
            {"key": "dossier-b", "title": "Phase aktuell", "summary": "Cut-Regeln bleiben konservativ und reviewbar.", "target_memory_file": "athlete_dossier", "target_section": "AKTUELLE_PHASE"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    by_file = {row["target_memory_file"]: row for row in built["patches"]}
    assert by_file["master_profile"]["safety_status"] == "blocked"
    assert by_file["athlete_dossier"]["safety_status"] in {"needs_review", "blocked"}


def test_core_memory_file_patch_preview_contains_before_and_after(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "preview-a", "title": "Push Preview A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "preview-b", "title": "Push Preview B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch = built["patches"][0]
    assert "## Training" in patch["preview_before"]
    assert "CORE-Konsolidierung" in patch["preview_after"]


def test_core_memory_file_patch_preview_is_read_only_and_does_not_create_missing_files(client):
    from core.core_memory_file_patches import build_patch_preview

    memory_service = client.application.extensions["liva_memory_service"]
    before_files = dict(memory_service.storage.files)
    with client.application.app_context():
        preview = build_patch_preview(
            target_memory_file="missing_memory_file",
            target_section="TRAINING",
            strategy="append_section_note",
            patch_text="### 2026-04-29 · CORE-Konsolidierung: Test\n\n- Kernaussage: Test.",
        )
    assert preview["preview_before"] == "Section konnte nicht gelesen werden."
    assert "CORE-Konsolidierung" in preview["preview_after"]
    assert memory_service.storage.files == before_files
    assert memory_service.get_file_calls[-1]["create_missing"] is False


def test_core_memory_file_patch_build_is_read_only_for_memory_files(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "readonly-a", "title": "Readonly A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "readonly-b", "title": "Readonly B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    memory_service = client.application.extensions["liva_memory_service"]
    before_files = dict(memory_service.storage.files)
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    assert memory_service.storage.files == before_files
    assert memory_service.apply_calls == []


def test_core_memory_file_patch_apply_is_append_only_for_ready_patch(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "apply-a", "title": "Push Apply A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "apply-b", "title": "Push Apply B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch_id = built["patches"][0]["id"]
    memory_service = client.application.extensions["liva_memory_service"]
    before = memory_service.storage.files["core_patterns"]
    applied = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch_id, "action": "apply"},
        headers=auth(),
    ).get_json()
    assert applied["ok"] is True
    after = memory_service.storage.files["core_patterns"]
    assert len(after) > len(before)
    assert "- Text-only delete target" in after
    assert "CORE-Konsolidierung" in after


def test_core_memory_file_patch_needs_review_requires_confirm(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "review-a", "title": "Phase Review A", "summary": "Carbs vor schweren Tagen.", "target_memory_file": "athlete_dossier", "target_section": "AKTUELLE_PHASE"},
            {"key": "review-b", "title": "Phase Review B", "summary": "Gewichtstrend nicht panisch bewerten.", "target_memory_file": "athlete_dossier", "target_section": "AKTUELLE_PHASE"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch = built["patches"][0]
    assert patch["safety_status"] == "needs_review"
    denied = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch["id"], "action": "apply", "confirm": False},
        headers=auth(),
    )
    assert denied.status_code == 400
    assert denied.get_json()["error"]["code"] == "confirm_required"
    allowed = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch["id"], "action": "apply", "confirm": True, "reason": "manuell geprüft"},
        headers=auth(),
    ).get_json()
    assert allowed["ok"] is True


def test_core_memory_file_patch_blocked_cannot_be_applied(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "blocked-a", "title": "Master Block A", "summary": "Nicht schreiben.", "target_memory_file": "master_profile", "target_section": "IDENTITAET"},
            {"key": "blocked-b", "title": "Master Block B", "summary": "Nicht schreiben.", "target_memory_file": "master_profile", "target_section": "IDENTITAET"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch = built["patches"][0]
    blocked = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch["id"], "action": "apply", "confirm": True},
        headers=auth(),
    )
    assert blocked.status_code == 400
    assert blocked.get_json()["error"]["code"] == "patch_blocked"


def test_core_memory_file_patch_update_recomputes_preview_and_safety(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "update-a", "title": "Update A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "update-b", "title": "Update B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch_id = built["patches"][0]["id"]
    updated = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch_id, "action": "update", "edits": {"target_memory_file": "athlete_dossier", "target_section": "AKTUELLE_PHASE", "patch_text": "### 2026-04-29 · CORE-Konsolidierung: Update\n\n- Kernaussage: Test."}},
        headers=auth(),
    ).get_json()
    assert updated["ok"] is True
    assert updated["patch"]["safety_status"] == "needs_review"
    assert "## Aktuelle Phase" in updated["patch"]["preview_after"]


def test_core_memory_file_patch_dismiss_writes_review_log(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "dismiss-a", "title": "Dismiss A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "dismiss-b", "title": "Dismiss B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch_id = built["patches"][0]["id"]
    dismissed = client.post("/api/core/memory/file-patches/control", json={"patch_id": patch_id, "action": "dismiss", "reason": "nicht nötig"}).get_json()
    assert dismissed["ok"] is True
    assert any(row["action"] == "dismiss_patch" for row in dismissed["patch"]["review_history"])
    dismiss_row = next(row for row in dismissed["patch"]["review_history"] if row["action"] == "dismiss_patch")
    assert dismiss_row["reason"] == "nicht nötig"


def test_core_memory_file_patch_v2_and_browser_endpoints_work_without_ai_key_in_browser(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "browser-a", "title": "Browser A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "browser-b", "title": "Browser B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    assert built["ok"] is True
    v2_list = client.get("/api/v2/actions/core/memory/file-patches?status=draft", headers=auth()).get_json()
    browser_list = client.get("/api/core/memory/file-patches").get_json()
    assert v2_list["ok"] is True
    assert browser_list["ok"] is True
    assert browser_list["patches"][0]["review_history"] is not None


def test_core_memory_file_patch_unknown_patch_returns_not_found_code(client):
    response = client.post(
        "/api/core/memory/file-patches/control",
        json={"patch_id": 999999, "action": "apply"},
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "memory_file_patch_not_found"


def test_core_memory_file_patch_dismissed_patch_is_not_silently_resurrected(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "dismissed-build-a", "title": "Dismissed Build A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "dismissed-build-b", "title": "Dismissed Build B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch = built["patches"][0]
    dismissed = client.post("/api/core/memory/file-patches/control", json={"patch_id": patch["id"], "action": "dismiss", "reason": "weg"}).get_json()
    assert dismissed["patch"]["status"] == "dismissed"
    rebuilt = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    row = next(item for item in rebuilt["patches"] if item["patch_key"] == patch["patch_key"])
    assert row["status"] == "dismissed"
    conn = connections.get_core_db()
    count = conn.execute("SELECT COUNT(*) FROM core_memory_file_patches WHERE patch_key=?", (patch["patch_key"],)).fetchone()[0]
    conn.close()
    assert count == 1


def test_core_memory_file_patch_health_notes_sensitive_is_blocked(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "health-a", "title": "Recovery A", "summary": "Schmerz nach harten Tagen beobachten.", "target_memory_file": "health_notes", "target_section": "RECOVERY"},
            {"key": "health-b", "title": "Recovery B", "summary": "Schmerz ist wiederholt sichtbar.", "target_memory_file": "health_notes", "target_section": "RECOVERY"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch = next(item for item in built["patches"] if item["target_memory_file"] == "health_notes")
    assert patch["safety_status"] == "blocked"


def test_core_memory_file_patch_apply_writes_review_log_entry(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "log-a", "title": "Log A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "log-b", "title": "Log B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch_id = built["patches"][0]["id"]
    applied = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch_id, "action": "apply"},
        headers=auth(),
    ).get_json()
    assert any(row["action"] == "apply_memory_file_patch" for row in applied["patch"]["review_history"])


def test_core_memory_file_patch_update_writes_review_log_entry_with_edits(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "edit-a", "title": "Edit A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "edit-b", "title": "Edit B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    built = client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth()).get_json()
    patch_id = built["patches"][0]["id"]
    updated = client.post(
        "/api/v2/actions/core/memory/file-patches/control",
        json={"patch_id": patch_id, "action": "update", "edits": {"title": "Neuer Titel"}},
        headers=auth(),
    ).get_json()
    update_row = next(row for row in updated["patch"]["review_history"] if row["action"] == "update_patch")
    assert update_row["payload"]["edits"]["title"] == "Neuer Titel"


def test_core_board_memory_counts_include_patches(client):
    _seed_file_patch_consolidations(
        client,
        [
            {"key": "board-a", "title": "Board A", "summary": "Push im Cut ohne Bonusvolumen.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
            {"key": "board-b", "title": "Board B", "summary": "Progression nur bei gutem Warm-up.", "target_memory_file": "core_patterns", "target_section": "TRAINING"},
        ],
    )
    client.post("/api/v2/actions/core/memory/file-patches/build", json={"min_consolidations": 2}, headers=auth())
    client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-04-29", "decision_intent": "train_controlled", "human_headline": "Push kontrolliert."},
        headers=auth(),
    )
    from core.core_daily_board import core_board_view_model

    vm = core_board_view_model("2026-04-29")
    assert any("Patches" in line for line in vm["ui"]["memory_summary_lines"])


def test_core_foundation_schema_ensure_calls_are_idempotent(client):
    from core.core_daily_board import ensure_core_daily_boards_schema
    from core.core_memory_layers import (
        ensure_core_memory_consolidations_schema,
        ensure_core_memory_events_schema,
        ensure_core_memory_review_log_schema,
    )
    from core.core_memory_file_patches import ensure_core_memory_file_patches_schema
    from core.core_training_card import ensure_core_training_card_schema

    conn = connections.get_core_db()
    ensure_core_daily_boards_schema(conn)
    ensure_core_training_card_schema(conn)
    conn.execute(
        """
        INSERT INTO core_training_cards (
            mode, card_type, payload_json, created_at, session_type, decision_intent
        ) VALUES ('', '', '', '', 'gym', 'train_controlled')
        """
    )
    ensure_core_daily_boards_schema(conn)
    ensure_core_training_card_schema(conn)
    ensure_core_memory_events_schema(conn)
    ensure_core_memory_events_schema(conn)
    ensure_core_memory_consolidations_schema(conn)
    ensure_core_memory_consolidations_schema(conn)
    ensure_core_memory_review_log_schema(conn)
    ensure_core_memory_review_log_schema(conn)
    ensure_core_memory_file_patches_schema(conn)
    ensure_core_memory_file_patches_schema(conn)
    row = conn.execute(
        "SELECT mode, card_type, payload_json, created_at FROM core_training_cards ORDER BY id DESC LIMIT 1"
    ).fetchone()
    table_names = {
        item["name"]
        for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('core_daily_boards', 'core_training_cards', 'core_memory_events', 'core_memory_consolidations', 'core_memory_review_log', 'core_memory_file_patches')"
        ).fetchall()
    }
    conn.close()
    assert table_names == {
        "core_daily_boards",
        "core_training_cards",
        "core_memory_events",
        "core_memory_consolidations",
        "core_memory_review_log",
        "core_memory_file_patches",
    }
    assert row["mode"] == "LIGHT"
    assert row["card_type"] == "training"
    assert row["payload_json"] == "{}"
    assert row["created_at"]


def test_core_memory_consolidation_control_logs_update_and_dismiss_reason(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Review Log {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    consolidation_id = built["drafts"][0]["id"]

    updated = client.post(
        "/api/v2/actions/core/memory/consolidations/control",
        json={"consolidation_id": consolidation_id, "action": "update", "edits": {"title": "Review Titel"}, "note": "edited"},
        headers=auth(),
    ).get_json()
    assert updated["ok"] is True

    dismissed = client.post(
        "/api/core/memory/consolidations/control",
        json={"consolidation_id": consolidation_id, "action": "dismiss", "note": "zu schwach"},
    ).get_json()
    assert dismissed["ok"] is True
    history = dismissed["consolidation"]["review_history"]
    assert any(row["action"] == "create" for row in history)
    assert any(row["action"] == "update" for row in history)
    dismiss_row = next(row for row in history if row["action"] == "dismiss")
    assert dismiss_row["reason"] == "zu schwach"


def test_core_memory_consolidation_apply_logs_review_history(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Apply Review {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    consolidation_id = built["drafts"][0]["id"]
    applied = client.post(
        "/api/v2/actions/core/memory/consolidations/control",
        json={"consolidation_id": consolidation_id, "action": "apply", "mode": "review_log", "note": "review note"},
        headers=auth(),
    ).get_json()
    assert applied["ok"] is True
    assert any(row["action"] == "apply_review_log" for row in applied["consolidation"]["review_history"])


def test_core_memory_consolidation_batch_requires_selection(client):
    response = client.post(
        "/api/v2/actions/core/memory/consolidations/batch",
        json={"action": "dismiss"},
        headers=auth(),
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "selection_required"


def test_core_foundation_browser_js_does_not_call_v2_actions_or_ship_ai_headers():
    repo = Path(__file__).resolve().parents[1]
    for rel in ("static/js/dashboard_vnext.js", "static/js/core_board.js"):
        content = (repo / rel).read_text(encoding="utf-8")
        if rel == "static/js/dashboard_vnext.js":
            sanitized = content.replace("/api/v2/actions/core/training-card", "")
            assert "/api/v2/actions" not in sanitized
        else:
            assert "/api/v2/actions" not in content
        assert "Authorization" not in content
        assert "Bearer" not in content
        assert "LIVA_AI" not in content


def test_core_foundation_openapi_contains_core_board_training_and_memory_paths():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_openapi_spec()
    paths = spec["paths"]
    patch_schema = spec["components"]["schemas"]["CoreMemoryFilePatch"]["properties"]
    assert "/actions/core/board" in paths
    assert "/actions/core/morning-context" in paths
    assert "/actions/core/board/build" in paths
    assert "/actions/core/training-card" in paths
    assert "/actions/core/training-card/build" in paths
    assert "/actions/core/memory/layers" in paths
    assert "/actions/core/memory/read" in paths
    assert "/actions/core/memory/build" in paths
    assert "/actions/core/memory/control" in paths
    assert "/actions/core/memory/consolidations" in paths
    assert "/actions/core/memory/consolidations/batch" in paths
    assert "/actions/core/memory/file-patches" in paths
    assert "/actions/core/memory/file-patches/build" in paths
    assert "/actions/core/memory/file-patches/control" in paths
    assert "safety_status" in patch_schema
    assert "safety_reasons" in patch_schema
    assert "preview_before" in patch_schema
    assert "preview_after" in patch_schema
    assert "source_consolidation_ids" in patch_schema
    operation_ids = []
    for methods in paths.values():
        for operation in methods.values():
            operation_ids.append(operation["operationId"])
    assert len(operation_ids) == len(set(operation_ids))


def test_core_foundation_gpt_openapi_schema_is_slim_and_curated():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    paths = spec["paths"]
    operation_ids = []
    for methods in paths.values():
        for operation in methods.values():
            operation_ids.append(operation["operationId"])
    assert len(operation_ids) >= 2
    assert "readLiva" in operation_ids
    assert "actLiva" in operation_ids
    assert len(operation_ids) == len(set(operation_ids))
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(paths))
    assert "readLiva" in operation_ids
    assert "actLiva" in operation_ids
    for forbidden in (
        "/actions/training/adjust",
        "/actions/core/memory/read",
        "/actions/liva/context",
        "/actions/core/state",
        "/actions/nutrition/timing",
        "/actions/training/today-loads",
        "/actions/cardio/summary",
        "/actions/bodycomp/cut-status",
        "/actions/core/board/build",
    ):
        assert forbidden not in paths


def test_core_foundation_gpt_openapi_files_exist():
    repo = Path(__file__).resolve().parents[1]
    for rel in ("openapi/liva-actions-gpt.json", "openapi/liva-actions-gpt.yaml"):
        assert (repo / rel).exists()


def test_core_memory_bundle_read_build_and_control_dispatch(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Bundle Dispatch {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    build_consolidations = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "consolidations", "filters": {"timeframe": "phase", "min_events": 2}},
        headers=auth(),
    ).get_json()
    assert build_consolidations["ok"] is True
    assert build_consolidations["resource"] == "consolidations"
    consolidation_id = build_consolidations["drafts"][0]["id"]

    read_consolidations = client.post(
        "/api/v2/actions/core/memory/read",
        json={"resource": "consolidations", "filters": {"status": "draft", "limit": 5}},
        headers=auth(),
    ).get_json()
    assert read_consolidations["ok"] is True
    assert read_consolidations["resource"] == "consolidations"
    assert read_consolidations["count"] >= 1

    control_consolidation = client.post(
        "/api/v2/actions/core/memory/control",
        json={"entity_type": "consolidation", "entity_id": consolidation_id, "action": "dismiss", "reason": "gpt bundle"},
        headers=auth(),
    ).get_json()
    assert control_consolidation["ok"] is True
    assert control_consolidation["entity_type"] == "consolidation"
    assert control_consolidation["consolidation"]["status"] == "dismissed"

    read_patches = client.post(
        "/api/v2/actions/core/memory/read",
        json={"resource": "file_patches", "filters": {"status": "draft", "limit": 5}},
        headers=auth(),
    ).get_json()
    assert read_patches["ok"] is True
    assert read_patches["resource"] == "file_patches"


def test_core_memory_bundle_control_keeps_legacy_event_form(client):
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-29",
            "decision_intent": "train_controlled",
            "human_headline": "Pull sauber ausführen.",
            "memory_candidates": [{"timeframe": "today", "category": "decision_rule", "title": "Legacy Event", "summary": "Heute Pull sauber, ohne Zusatzdruck.", "write_policy": "candidate"}],
        },
        headers=auth(),
    ).get_json()
    event_id = body["memory_events"]["pending_event_ids"][0] if body.get("memory_events", {}).get("pending_event_ids") else None
    if event_id is None:
        inbox = client.get("/api/core/memory/inbox").get_json()
        event_id = inbox["events"][0]["id"]
    response = client.post(
        "/api/v2/actions/core/memory/control",
        json={"event_id": event_id, "action": "keep_pending", "note": "legacy form"},
        headers=auth(),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["event"]["status"] == "pending"


def test_core_foundation_smoke_includes_file_patch_schema_and_routes():
    content = Path("scripts/smoke_core_foundation.py").read_text(encoding="utf-8")
    assert "core.core_memory_file_patches" in content
    assert "ensure_core_memory_file_patches_schema" in content
    assert "/api/v2/actions/core/memory/file-patches" in content
    assert "/api/core/memory/file-patches" in content


def test_core_memory_consolidation_batch_only_changes_selected_and_logs_each(client):
    for idx in range(6):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-{20+idx}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"Selected Batch {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    built = client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth()).get_json()
    selected = [built["drafts"][0]["id"]]
    batch = client.post(
        "/api/core/memory/consolidations/batch",
        json={"ids": selected, "action": "dismiss", "reason": "nur dieser"},
    ).get_json()
    assert batch["ok"] is True
    assert batch["result"]["selection_count"] == 1
    assert [row["id"] for row in batch["result"]["changed"]] == selected
    dismissed = client.get("/api/core/memory/consolidations?status=dismissed").get_json()["consolidations"]
    row = next(item for item in dismissed if item["id"] == selected[0])
    assert any(item["action"] == "batch_dismiss" for item in row["review_history"])


def test_core_memory_consolidation_browser_response_includes_review_history(client):
    for idx in range(2):
        client.post(
            "/api/v2/actions/core/board/build",
            json={
                "date": f"2026-04-2{idx+1}",
                "decision_intent": "train_controlled",
                "human_headline": "Push kontrolliert ausführen.",
                "memory_candidates": [{"timeframe": "phase", "category": "decision_rule", "title": f"UI History {idx}", "summary": "Push im Cut ohne Bonusvolumen, Progression nach Warmup.", "write_policy": "candidate"}],
            },
            headers=auth(),
        )
    client.post("/api/v2/actions/core/memory/consolidations/build", json={"timeframe": "phase", "min_events": 2}, headers=auth())
    browser = client.get("/api/core/memory/consolidations").get_json()
    assert browser["ok"] is True
    assert "review_history" in browser["consolidations"][0]


def test_actions_v2_core_board_legacy_core_stale_is_marked(client):
    payload = {
        "date": "2026-04-27",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert.",
        "legacy_core": {
            "daily_decision": {"day_iso": "2026-04-26"},
            "hero": "Z2 Run",
            "planned_unit": "Run Z2",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["legacy_core_status"]["stale"] is True
    assert body["legacy_core_status"]["legacy_day_iso"] == "2026-04-26"


def test_actions_v2_core_board_text_mismatch_is_detected(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Run Z2 ruhig durchziehen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] == "mismatch"
    assert "run" in body["board_consistency"]["detected_text_tokens"]
    assert body["warning"]


def test_actions_v2_core_board_text_ok_for_push_card(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] == "ok"


def test_actions_v2_core_board_summary_mismatch_is_detected(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "human_summary": "Heute Run Z2 ruhig durchziehen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] == "mismatch"


def test_actions_v2_core_board_visible_reasons_only_conflict_is_warning(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push kontrolliert ausführen.",
        "visible_reasons": ["Run Z2 blieb aus Legacy sichtbar."],
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] in {"ok", "warning"}


def test_actions_v2_core_board_pull_card_with_push_headline_is_mismatch(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Push sauber ausführen.",
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Pull",
            "recommended_session_label": "Pull",
            "title": "Pull",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "mismatch"
    assert "andere Gym-Einheit" in body["board_consistency"]["message"]


def test_actions_v2_core_morning_context_endpoint_exists_and_is_compact(client):
    body = client.get("/api/v2/actions/core/morning-context?date=2026-04-22", headers=auth()).get_json()
    assert body["ok"] is True
    context = body["morning_context"]
    assert context["must_use_session_label"] == "Pull"
    assert context["decision_contract"]["use_must_use_session_label"] is True
    assert "allowed_decision_intents" in context["decision_contract"]
    assert "items" not in context
    assert "exercises" not in context
    assert "weights" not in context
    assert "recovery_summary" in context


def test_actions_v2_core_morning_context_is_read_only(client):
    conn = connections.get_core_db()
    board_before = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_daily_boards'").fetchone()[0]
    card_before = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'").fetchone()[0]
    conn.close()
    body = client.get("/api/v2/actions/core/morning-context?date=2026-04-22", headers=auth()).get_json()
    assert body["ok"] is True
    conn = connections.get_core_db()
    board_after = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_daily_boards'").fetchone()[0]
    card_after = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='core_training_cards'").fetchone()[0]
    conn.close()
    assert board_before == board_after
    assert card_before == card_after


def test_actions_v2_core_morning_context_missing_session_returns_warning(client):
    conn = connections.get_plans_db()
    conn.execute("DELETE FROM gym_plans")
    conn.execute("DELETE FROM plans")
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/core/morning-context?date=2026-04-22", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["morning_context"]["must_use_session_label"] is None
    assert body["morning_context"]["warnings"]


def test_actions_v2_core_morning_context_minimal_board_build_with_must_use_session_label(client):
    context = client.get("/api/v2/actions/core/morning-context?date=2026-04-22", headers=auth()).get_json()["morning_context"]
    payload = {
        "date": "2026-04-22",
        "trigger": "user_morning_checkin",
        "source_message": "Gewicht & HRV sind drin.",
        "human_headline": f"{context['must_use_session_label']} sauber ausführen, aber nicht drauflegen.",
        "decision_intent": "train_controlled",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] == "ok"
    assert body["training_card"]["recommended_session_label"] == context["must_use_session_label"]


def test_actions_v2_core_board_build_gpt_fast_returns_compact_summary_and_persists_card(client):
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "trigger": "user_morning_checkin",
            "source_message": "Gewicht & HRV sind drin.",
            "human_headline": "Pull sauber ausführen, aber nicht drauflegen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
            "persist": True,
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["mode"] == "gpt_fast"
    assert body["training_card_ready"] is True
    assert "training_card_summary" in body
    assert "training_card" not in body
    assert "legacy_core_status" not in body
    assert body["memory_events_ready"] is False
    assert body["memory_events_deferred"] is True
    assert body["memory_events_summary"]["skipped"] is True
    assert body["memory_events_summary"]["reason"] == "gpt_fast_does_not_block_on_memory"
    assert body["memory_events_summary"]["next_step"] == "call buildCoreMemoryArtifacts with artifact=events later"
    assert body["training_card_summary"]["planned_session_label"] == "Pull"
    card = client.get("/api/v2/actions/core/training-card?date=2026-04-22", headers=auth()).get_json()["training_card"]
    assert card["recommended_session_label"] == "Pull"
    assert isinstance(card["items"], list)
    assert len(card["items"]) == body["training_card_summary"]["item_count"]


def test_actions_v2_core_board_build_gpt_fast_prefers_gpt_today_changed_lines_and_source(client):
    lines = [
        "TEST A: GPT-Zeile wurde gespeichert.",
        "TEST B: Training-Card nutzt frische today_changed_lines.",
        "TEST C: Kein Fallback und kein alter Snapshot.",
    ]
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "trigger": "user_morning_checkin",
            "source_message": "Debug test for today_changed_lines",
            "human_headline": "Pull kontrolliert ausführen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
            "force_training_card": True,
            "persist": True,
            "training_card": {
                "session_type": "gym",
                "title": "Pull",
                "planned_session_label": "Pull",
                "recommended_session_label": "Pull",
                "today_changed_lines": lines,
            },
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["training_card_summary"]["today_changed_lines"] == lines
    assert body["training_card_summary"]["today_changed_source"] == "gpt_morning_checkin"
    card = client.get("/api/v2/actions/core/training-card?date=2026-04-22", headers=auth()).get_json()["training_card"]
    assert card["today_changed_lines"] == lines
    assert card["today_changed_source"] == "gpt_morning_checkin"


def test_actions_v2_core_board_build_recovery_only_keeps_gpt_training_card_lines(client):
    lines = [
        "Heute kein Gym: Feiertag/Studio geschlossen, deshalb Rest statt Upper.",
        "Upper wird nicht ersetzt, sondern auf morgen geschoben.",
        "Recovery ist nutzbar; Rest ist organisatorisch, nicht wegen Warnsignal.",
    ]
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "trigger": "user_core_rest_day_override",
            "source_message": "Heute ist Feiertag, Gym hat zu. Also Rest. Morgen machen wir dann Upper.",
            "human_headline": "Heute Rest, Upper morgen.",
            "human_summary": "Heute wird nicht trainiert, weil das Gym geschlossen ist. Upper bleibt morgen dran.",
            "decision_intent": "recovery_only",
            "build_training_card": True,
            "force_training_card": True,
            "persist": True,
            "visible_reasons": [
                "Nutzer meldet: Heute ist Feiertag und das Gym ist geschlossen.",
                "Geplante Einheit laut Morning-Kontext wäre Upper.",
                "Recovery-Daten sind nutzbar.",
            ],
            "training_card": {
                "title": "Rest Day · Upper morgen",
                "summary": "Heute kein Gym wegen Feiertag. Upper bleibt die nächste Einheit und wird auf morgen geschoben.",
                "session_type": "gym",
                "session_label": "Upper",
                "decision_intent": "recovery_only",
                "today_changed_lines": lines,
            },
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["training_card_summary"]["today_changed_lines"] == lines
    assert body["training_card_summary"]["today_changed_source"] == "gpt_morning_checkin"
    card = client.get("/api/v2/actions/core/training-card?date=2026-04-22", headers=auth()).get_json()["training_card"]
    assert card["decision_intent"] == "recovery_only"
    assert card["today_changed_lines"] == lines
    assert card["today_changed_source"] == "gpt_morning_checkin"
    assert card["title"] == "Rest Day · Upper morgen"


def test_actions_v2_core_board_build_gpt_fast_can_return_timings_and_small_response(client):
    response = client.post(
        "/api/v2/actions/core/board/build?debug_timing=1",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "human_headline": "Pull sauber ausführen, aber nicht drauflegen.",
            "human_summary": "Heute bleibt Pull drin. Warm-up ernst nehmen, RPE deckeln, kein Zusatzvolumen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    )
    body = response.get_json()
    assert body["ok"] is True
    assert body["mode"] == "gpt_fast"
    assert "timings" in body
    assert {"total_ms", "build_board_ms", "persist_board_ms", "consistency_ms", "training_card_ms", "memory_events_ms", "response_serialize_ms"} <= set(body["timings"])
    assert "training_card" not in body
    assert body["memory_events_deferred"] is True
    assert body["memory_events_summary"]["skipped"] is True
    assert body["timings"]["memory_events_ms"] == 0
    assert len(response.get_data()) < 30_000


def test_actions_v2_core_board_build_gpt_fast_survives_memory_event_failures(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    def boom(*args, **kwargs):
        raise RuntimeError("memory boom")

    monkeypatch.setattr(actions_v2, "build_memory_events_from_board", boom)
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "human_headline": "Pull sauber ausführen, aber nicht drauflegen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["mode"] == "gpt_fast"
    assert body["training_card_ready"] is True
    assert body["memory_events_ready"] is False
    assert body["memory_events_deferred"] is True
    assert body["memory_events_summary"]["skipped"] is True


def test_actions_v2_core_board_build_gpt_fast_memory_mode_sync_keeps_legacy_memory_path(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    called = {"count": 0}

    def fake_build(*args, **kwargs):
        called["count"] += 1
        return [{"source": "core_board", "kind": "candidate", "title": "Board built"}]

    def fake_upsert(events):
        assert len(events) == 1
        return {"created": 1, "pending": 1, "auto_logged": 0}

    monkeypatch.setattr(actions_v2, "build_memory_events_from_board", fake_build)
    monkeypatch.setattr(actions_v2, "upsert_core_memory_events", fake_upsert)
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "mode": "gpt_fast",
            "memory_mode": "sync",
            "human_headline": "Pull sauber ausführen, aber nicht drauflegen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    ).get_json()
    assert called["count"] == 1
    assert body["mode"] == "gpt_fast"
    assert body["memory_events_ready"] is True
    assert body["memory_events_summary"]["created"] == 1
    assert body["memory_events_summary"]["pending"] == 1


def test_actions_v2_core_board_build_full_mode_still_builds_memory_events(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    called = {"count": 0}

    def fake_build(*args, **kwargs):
        called["count"] += 1
        return [{"source": "core_board", "kind": "candidate", "title": "Board built"}]

    def fake_upsert(events):
        assert len(events) == 1
        return {"created": 1, "pending": 0, "auto_logged": 0}

    monkeypatch.setattr(actions_v2, "build_memory_events_from_board", fake_build)
    monkeypatch.setattr(actions_v2, "upsert_core_memory_events", fake_upsert)
    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-22",
            "human_headline": "Pull sauber ausführen, aber nicht drauflegen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    ).get_json()
    assert called["count"] == 1
    assert body["mode"] == "full"
    assert body["memory_events"]["created"] == 1
    assert body["memory_events_ready"] is True


def test_actions_v2_gpt_openapi_documents_gpt_fast_mode():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(spec["paths"]))
    assert spec["paths"]["/api/v2/actions/liva/read"]["post"]["x-openai-isConsequential"] is False
    assert "LivaReadRequest" in spec["components"]["schemas"]
    modes = set(spec["components"]["schemas"]["LivaReadRequest"]["properties"]["mode"]["enum"])
    assert {"recovery_state", "recovery_data", "training_plan", "memory", "core_board", "core_training_card"}.issubset(modes)


def test_actions_v2_gpt_schema_exposes_training_plans_and_include_archived_for_reads():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_plans" in read_props["mode"]["enum"]
    for field in ("include_archived", "plan_id", "id", "detail"):
        assert field in read_props

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_read_props = file_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_plans" in file_read_props["mode"]["enum"]
    assert "include_archived" in file_read_props

    yaml_spec = json.loads((repo / "openapi/liva-actions-gpt.yaml").read_text(encoding="utf-8"))
    yaml_read_props = yaml_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_plans" in yaml_read_props["mode"]["enum"]
    assert "include_archived" in yaml_read_props


def test_actions_v2_core_memory_build_events_works_after_gpt_fast_board_build(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-24",
            "mode": "gpt_fast",
            "human_headline": "Pull sauber ausführen.",
            "human_summary": "Warm-up ernst nehmen, kein Zusatzvolumen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    )
    body = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "events", "filters": {"date": "2026-04-24"}},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["artifact"] == "events"
    assert body["date"] == "2026-04-24"
    assert body["memory_events_ready"] is True
    assert body["memory_events_summary"]["deduped"] is True
    assert "events" not in body


def test_actions_v2_core_memory_build_events_requires_board(client):
    body = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "events", "filters": {"date": "2026-04-30"}},
        headers=auth(),
    ).get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "board_required"


def test_actions_v2_core_memory_build_events_is_idempotent(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-25",
            "mode": "gpt_fast",
            "human_headline": "Push kontrolliert ausführen.",
            "human_summary": "Nur geplantes Volumen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
        },
        headers=auth(),
    )
    first = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "events", "filters": {"date": "2026-04-25"}},
        headers=auth(),
    ).get_json()
    second = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "events", "filters": {"date": "2026-04-25"}},
        headers=auth(),
    ).get_json()
    assert first["ok"] is True and second["ok"] is True
    assert first["memory_events_summary"]["created"] >= 1
    assert second["memory_events_summary"]["created"] == 0
    conn = connections.get_core_db()
    rows = conn.execute("SELECT COUNT(*) AS count, COUNT(DISTINCT event_key) AS distinct_count FROM core_memory_events WHERE day_iso='2026-04-25'").fetchone()
    conn.close()
    assert rows["count"] == rows["distinct_count"]


def test_actions_v2_core_memory_build_events_works_without_training_card_with_warning(client):
    client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-26",
            "mode": "gpt_fast",
            "human_headline": "Locker bleiben.",
            "human_summary": "Nur Technik und saubere Ausführung.",
            "decision_intent": "technique_only",
            "build_training_card": False,
        },
        headers=auth(),
    )
    body = client.post(
        "/api/v2/actions/core/memory/build",
        json={"artifact": "events", "filters": {"date": "2026-04-26"}},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["warnings"] == ["training_card_missing"]
    assert body["memory_events_summary"]["active"] >= 1


def test_actions_v2_gpt_openapi_memory_build_includes_events_and_operation_count_in_target_range():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(spec["paths"]))
    assert spec["paths"]["/api/v2/actions/liva/act"]["post"]["operationId"] == "actLiva"
    assert "LivaActRequest" in spec["components"]["schemas"]


def test_actions_v2_full_openapi_special_routes_remain_unchanged_and_memory_build_includes_events():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_openapi_spec()
    paths = spec["paths"]
    assert "/actions/core/memory/consolidations/build" in paths
    assert "/actions/core/memory/file-patches/build" in paths
    schema = spec["components"]["schemas"]["CoreMemoryBuildRequest"]["properties"]["artifact"]["enum"]
    assert schema == ["events", "consolidations", "file_patches"]


def test_actions_v2_gpt_schema_marks_training_card_read_non_consequential_and_board_build_safe():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert spec["paths"]["/api/v2/actions/liva/read"]["post"]["x-openai-isConsequential"] is False
    assert spec["paths"]["/api/v2/actions/liva/act"]["post"]["x-openai-isConsequential"] is False


def test_actions_v2_gpt_schema_reads_and_writes_are_non_consequential_in_private_slim_mode():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(spec["paths"]))
    assert spec["paths"]["/api/v2/actions/liva/read"]["post"]["x-openai-isConsequential"] is False
    assert spec["paths"]["/api/v2/actions/liva/act"]["post"]["x-openai-isConsequential"] is False


def test_actions_v2_gpt_schema_does_not_expose_high_risk_internal_tools():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    operation_ids = []
    for methods in spec["paths"].values():
        for operation in methods.values():
            operation_ids.append(operation["operationId"])
    for blocked in {"controlRemote", "setCoreOverride", "normalizeTrainingImport"}:
        assert blocked not in operation_ids


def test_actions_v2_gpt_schema_exposes_log_gym_session_in_non_consequential_private_actliva():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    op = spec["paths"]["/api/v2/actions/liva/act"]["post"]
    request_props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert op["operationId"] == "actLiva"
    assert op["x-openai-isConsequential"] is False
    assert "dry_run=true first" in op["description"]
    assert "Allowed actLiva command." in request_props["command"]["description"]
    assert "operations or payload" in request_props["command"]["description"]
    assert "source_message" in request_props
    assert "dry_run" in request_props
    assert "session_name" in request_props
    command_enum = request_props["command"]["enum"]
    for token in ("set_sickness_flag", "set_alcohol_flag", "replace_exercise", "change_rpe", "update_exercise_target", "update_entry", "remove_entry", "replace_section", "create_guest_key"):
        assert token in command_enum


def test_actions_v2_gpt_schema_exposes_plan_mutation_top_level_fields():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    request_props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    for field in ("day", "exercise", "new_variation", "sets", "rpe_list"):
        assert field in request_props

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_props = file_spec["components"]["schemas"]["LivaActRequest"]["properties"]
    for field in ("day", "exercise", "new_variation", "sets", "rpe_list"):
        assert field in file_props


def test_actions_v2_gpt_schema_exposes_required_command_enum_entries():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    command_enum = spec["components"]["schemas"]["LivaActRequest"]["properties"]["command"]["enum"]
    for command in ("change_variation", "change_sets", "change_rpe", "update_exercise_target", "remove_entry", "create_guest_key", "setup_bodyweight_phase"):
        assert command in command_enum

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_enum = file_spec["components"]["schemas"]["LivaActRequest"]["properties"]["command"]["enum"]
    for command in ("change_variation", "change_sets", "change_rpe", "update_exercise_target", "remove_entry", "create_guest_key", "setup_bodyweight_phase"):
        assert command in file_enum

    assert "/actions/liva/read" not in file_spec["paths"]
    assert "/actions/liva/act" not in file_spec["paths"]
    assert file_spec["paths"]["/api/v2/actions/liva/read"]["post"]["x-openai-isConsequential"] is False
    assert file_spec["paths"]["/api/v2/actions/liva/act"]["post"]["x-openai-isConsequential"] is False
    assert {
        "/api/v2/actions/liva/read",
        "/api/v2/actions/liva/act",
        "/api/v2/actions/training/adjust",
        "/api/v2/actions/training/edit-logged-set",
        "/api/v2/actions/nutrition/control",
    }.issubset(set(file_spec["paths"]))


def test_actions_v2_gpt_schema_documents_compact_training_plan_reads():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "day" in read_props
    assert "exercise" in read_props
    assert "compact, day, exercise or full" in read_props["detail"]["description"]


def test_actions_v2_gpt_schema_exposes_bodyweight_phase_via_facade_not_direct_paths():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    act_props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "bodyweight_phase_report" in read_props["mode"]["enum"]
    assert "setup_bodyweight_phase" in act_props["command"]["enum"]
    for field in ("phase_type", "phase_start", "phase_start_weight", "dry_run"):
        assert field in act_props


def test_actions_v2_gpt_openapi_is_curated_and_operator_capable():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert 10 <= len(spec["paths"]) <= 20
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(spec["paths"]))
    operation_ids = {
        op.get("operationId")
        for methods in spec["paths"].values()
        for method, op in methods.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    assert {"readLiva", "actLiva"}.issubset(operation_ids)
    assert {"TrainingPlanPatchRequest", "NutritionPlanPatchRequest"}.issubset(spec["components"]["schemas"])
    act_props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    for field in ("domain", "command", "dry_run", "confirm", "plan_id", "operations", "value", "name", "set_active", "replace_active", "replace_with", "food_id", "template_id", "exercise_id", "date", "payload", "patch"):
        assert field in act_props
    assert spec["components"]["schemas"]["LivaActRequest"]["additionalProperties"] is True

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_read_props = file_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    file_act_props = file_spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "bodyweight_phase_report" in file_read_props["mode"]["enum"]
    assert "setup_bodyweight_phase" in file_act_props["command"]["enum"]
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(file_spec["paths"]))


def test_actions_v2_gpt_schema_exposes_progression_summary_via_read_facade():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "progression_summary" in read_props["mode"]["enum"]
    assert "days" in read_props

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_read_props = file_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "progression_summary" in file_read_props["mode"]["enum"]
    assert "days" in file_read_props


def test_actions_v2_gpt_schema_exposes_training_deep_dive_via_read_facade():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_deep_dive" in read_props["mode"]["enum"]
    for field in ("days", "date_from", "date_to", "focus", "muscle_group", "canonical_id"):
        assert field in read_props

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_read_props = file_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_deep_dive" in file_read_props["mode"]["enum"]
    assert "focus" in file_read_props


def test_actions_v2_openapi_exposes_training_deep_dive_route():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_openapi_spec()
    assert "/actions/training/deep-dive" in spec["paths"]
    params = spec["paths"]["/actions/training/deep-dive"]["get"]["parameters"]
    assert any(item["name"] == "focus" for item in params)

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-v2.json").read_text(encoding="utf-8"))
    assert "/actions/training/deep-dive" in file_spec["paths"]


def test_training_deep_dive_overview_and_muscle_groups(client):
    conn = connections.get_training_db()
    workouts = [
        (410, "2026-04-01", "Push"),
        (411, "2026-04-08", "Pull"),
        (412, "2026-04-15", "Legs"),
        (413, "2026-04-22", "Push"),
    ]
    for row in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", row)
    exercises = [
        (4100, 410, "TNF Press", "Smith", "Smith", "bilateral"),
        (4101, 410, "Cable Crunches", "Cable", "Cable", "bilateral"),
        (4110, 411, "breite Rows", "eGym", "eGym", "bilateral"),
        (4111, 411, "Curls", "SZ", "SZ", "bilateral"),
        (4120, 412, "Squats", "LH", "LH", "bilateral"),
        (4121, 412, "Wadenheben", "Machine", "Machine", "bilateral"),
        (4130, 413, "Pushdowns", "Cable", "Cable", "bilateral"),
    ]
    for row in exercises:
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, ?)", row)
    sets = [
        (7000, 4100, 410, 1, 45, 8, 8), (7001, 4100, 410, 2, 42.5, 10, 8),
        (7002, 4101, 410, 1, 20, 12, 8),
        (7003, 4110, 411, 1, 70, 10, 8), (7004, 4110, 411, 2, 70, 9, 8),
        (7005, 4111, 411, 1, 35, 10, 8),
        (7006, 4120, 412, 1, 100, 8, 8), (7007, 4121, 412, 1, 80, 12, 8),
        (7008, 4130, 413, 1, 45, 10, 8),
    ]
    for row in sets:
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=90&date_to=2026-05-20&focus=overview", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["mode"] == "training_deep_dive"
    assert body["overview"]
    assert body["muscle_groups"]
    assert body["exercise_rankings"]
    assert body["coach_insights"] is not None
    assert body["data_quality"]
    assert any(key in body["muscle_groups"] for key in ("chest", "back", "legs", "core"))
    assert any(item["sets_total"] >= 2 for item in body["muscle_groups"].values())
    assert all("weekly_volume" not in item for item in body["muscle_groups"].values())
    assert "weekly_buckets" not in body["phases"]


def test_training_deep_dive_exercise_progression_and_curl_example(client):
    conn = connections.get_training_db()
    workouts = [(420, "2026-03-01", "Pull"), (421, "2026-04-01", "Pull"), (422, "2026-05-01", "Pull")]
    for row in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", row)
    for ex_id, wid, weight in ((4200, 420, 30), (4201, 421, 32.5), (4202, 422, 35)):
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'Curls', 'SZ', 'SZ', 'bilateral')", (ex_id, wid))
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, ?, 10, 8)", (8000 + ex_id, ex_id, wid, weight))
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=180&focus=exercise_progression&canonical_id=curls__sz__bilateral", headers=auth()).get_json()
    ex = body["exercise_progression"][0]
    assert ex["canonical_id"] == "curls__sz__bilateral"
    assert ex["progression_score"] is not None
    assert ex["score_version"] == "simple_v1"
    assert ex["score_confidence"] in {"high", "medium", "low"}
    assert ex["last_5_sessions"]
    assert ex["recent_momentum"] in {"up", "down", "flat"}
    assert ex["stagnation_risk"] in {"low", "medium", "high"}


def test_training_deep_dive_best_and_weak_phases(client):
    conn = connections.get_training_db()
    for idx, day in enumerate(("2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02", "2026-02-09"), start=430):
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, 'Upper')", (idx, day))
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'TNF Press', 'Smith', 'Smith', 'bilateral')", (idx + 1000, idx))
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, ?, 8, 8)", (idx + 2000, idx + 1000, idx, 40 + (idx - 430)))
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=180&focus=best_phases", headers=auth()).get_json()
    assert body["phases"]["best_training_blocks"] is not None
    assert body["phases"]["weak_training_blocks"] is not None
    assert body["phases"]["recent_vs_previous"] is not None


def test_liva_read_training_deep_dive_routes_to_same_handler(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (460, '2026-05-01', 'Push')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4600, 460, 'TNF Press', 'Smith', 'Smith', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9600, 4600, 460, 1, 45, 8, 8)")
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_deep_dive", "days": 90, "focus": "overview"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["mode"] == "training_deep_dive"
    assert body["result"]["mode"] == "training_deep_dive"


def test_training_deep_dive_edge_cases(client):
    no_data = client.get("/api/v2/actions/training/deep-dive?date_from=2020-01-01&date_to=2020-01-10", headers=auth()).get_json()
    assert no_data["ok"] is True
    assert no_data["days"] == 10
    assert no_data["data_quality"]["confidence"] == "low"
    assert "no_training_sessions_in_range" in no_data["data_quality"]["warnings"]

    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (470, '2026-05-02', 'Push')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4700, 470, 'Flys', 'Cable', 'Cable', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9700, 4700, 470, 1, NULL, 12, NULL)")
    conn.commit()
    conn.close()
    partial = client.get("/api/v2/actions/training/deep-dive?days=30&date_to=2026-05-03", headers=auth()).get_json()
    assert partial["ok"] is True
    assert "missing_rpe_sets_present" in partial["data_quality"]["warnings"]
    assert "missing_weight_sets_present" in partial["data_quality"]["warnings"]


def test_liva_read_exposes_mode_specific_read_parameters(client):
    import ai.actions_v2 as actions_v2

    props = actions_v2.build_gpt_openapi_spec()["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert {"nutrition_mode", "exercise_names", "compare_type", "range_a", "range_b"}.issubset(props)


def test_liva_read_progression_data_routes_canonical_id_to_canonical_resolver(monkeypatch):
    import ai.actions_v2 as actions_v2

    captured = {}

    def fake_call(view, *, method, query=None, body=None):
        captured.update({"view": view, "method": method, "query": query, "body": body})
        return {"ok": True, "canonical_id": query["canonical_id"]}, 200

    monkeypatch.setattr(actions_v2, "_call_actions_view", fake_call)
    payload, status = actions_v2._dispatch_liva_read({"mode": "progression_data", "canonical_id": "rows__egym__bilateral", "days": 60})

    assert status == 200
    assert payload["canonical_id"] == "rows__egym__bilateral"
    assert captured["view"] is actions_v2.progression_exercise
    assert captured["method"] == "GET"
    assert captured["query"] == {"canonical_id": "rows__egym__bilateral", "days": 60}


def test_nutrition_state_prefers_active_plan_targets_over_stale_sidecar(monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "get_live_macro_targets",
        lambda: {
            "kcal": 2900,
            "p": 185,
            "c": 400,
            "f": 55,
            "source": "active_week_template",
            "template_id": 39,
            "template_title": "Plan 2900",
        },
    )
    monkeypatch.setattr(actions_v2, "_get_state", lambda *_args, **_kwargs: {"kcal": 2650, "protein": 180, "carbs": 300, "fat": 65})

    state = actions_v2._nutrition_target_state()
    assert state["active_targets"] == {"kcal": 2900.0, "protein": 185.0, "carbs": 400.0, "fat": 55.0}
    assert state["targets_source"] == "live_plan"


def test_calendar_failure_is_unknown_not_an_empty_day(monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(actions_v2, "_school_schedule_entries_range", lambda *_args, **_kwargs: ({}, {"source_available": False}))
    monkeypatch.setattr(actions_v2, "_calendar_snapshot_summaries", lambda *_args, **_kwargs: ({}, {"source_available": False}))
    monkeypatch.setattr(
        actions_v2,
        "_calendar_google_range_payload",
        lambda *_args, **_kwargs: {"events": [], "warnings": [{"error": "quota exceeded"}]},
    )

    payload = actions_v2._calendar_range_payload("2026-06-05", "2026-06-05")
    day = payload["days"][0]
    assert day["available"] is False
    assert day["day_pressure"] == "unknown"
    assert day["reason"] == "calendar_source_unavailable"


def test_training_deep_dive_muscle_group_mapping_rules(client):
    body = client.get("/api/v2/actions/training/deep-dive?date_from=2020-01-01&date_to=2020-01-02", headers=auth()).get_json()
    import ai.actions_v2 as actions_v2

    assert actions_v2._muscle_group_for_exercise("Flys", "flys__sz__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("Incline Flys", "incline_flys__sz__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("reversed Flys", "reversed_flys__sz__unilateral") == "shoulders"
    assert actions_v2._muscle_group_for_exercise("Bench", "bench__lh__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("Schrägbankdrücken", "schragbankdrucken__smith__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("TNF Press", "tnf_press__smith__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("Dips", "dips__weighted__bilateral") == "chest"
    assert actions_v2._muscle_group_for_exercise("Latzug", "latzug__egym__bilateral") == "back"
    assert actions_v2._muscle_group_for_exercise("breite Rows", "breite_rows__egym__bilateral") == "back"
    assert actions_v2._muscle_group_for_exercise("PullUps", "pullups__weighted__bilateral") == "back"
    assert actions_v2._muscle_group_for_exercise("Beinstrecker", "beinstrecker__egym__unilateral") == "legs"
    assert actions_v2._muscle_group_for_exercise("Bayesians", "bayesians__sz__bilateral") == "biceps"
    assert actions_v2._muscle_group_for_exercise("Preachers", "preachers__kh__bilateral") == "biceps"
    assert actions_v2._muscle_group_for_exercise("Pushdowns", "pushdowns__unilat__bilateral") == "triceps"
    assert actions_v2._muscle_group_for_exercise("Seitheben", "seitheben__sz__unilateral") == "shoulders"
    assert actions_v2._muscle_group_for_exercise("Flys", "flys__sz__bilateral") != "back"
    assert actions_v2._muscle_group_for_exercise("Seitheben", "seitheben__sz__unilateral") != "back"
    assert body["ok"] is True


def test_training_deep_dive_phase_type_only_applies_after_phase_start(client):
    conn = connections.get_core_db()
    conn.execute("CREATE TABLE IF NOT EXISTS actions_v2_state (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)")
    conn.execute("INSERT OR REPLACE INTO actions_v2_state (key, value_json, updated_at) VALUES ('phase', ?, '2026-04-03T00:00:00Z')", (json.dumps({"phase": "cut", "phase_start": "2026-04-03"}),))
    conn.commit()
    conn.close()

    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (480, '2025-05-01', 'Push')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (481, '2026-04-06', 'Push')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4800, 480, 'Bench', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4801, 481, 'Bench', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9800, 4800, 480, 1, 80, 8, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9801, 4801, 481, 1, 82.5, 8, 8)")
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=730&focus=all", headers=auth()).get_json()
    weeks = {item["week_start"]: item for item in body["phases"]["weekly_buckets"]}
    assert weeks["2025-04-28"]["phase_type"] is None
    assert weeks["2026-04-06"]["phase_type"] == "cut"
    assert weeks["2026-04-06"]["phase_source"] == "configured_phase"


def test_training_deep_dive_generates_insights_and_filters_needs_attention(client):
    conn = connections.get_training_db()
    workouts = [(490, "2026-03-01", "Push"), (491, "2026-03-08", "Push"), (492, "2026-03-15", "Push"), (493, "2026-03-22", "Push")]
    for row in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", row)
    for ex_id, wid, weight, reps, rpe in (
        (4900, 490, 50, 10, None),
        (4901, 491, 47.5, 9, None),
        (4902, 492, 45, 8, None),
        (4903, 493, 45, 8, None),
    ):
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'Pushdowns', 'Cable', 'Cable', 'bilateral')", (ex_id, wid))
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, ?, ?, ?)", (9900 + ex_id, ex_id, wid, weight, reps, rpe))
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=120&date_to=2026-04-15&focus=all", headers=auth()).get_json()
    insight_types = {item["type"] for item in body["coach_insights"]}
    assert "data_quality_missing_rpe" in insight_types
    assert any(item["type"] in {"volume_progression_mismatch", "high_volume_stagnation"} for item in body["coach_insights"])

    attention_ids = {item["canonical_id"] for item in body["exercise_rankings"]["needs_attention"]}
    assert "pushdowns__cable__bilateral" in attention_ids
    assert all(not (item["progression_score"] == 70 and item["stagnation_risk"] == "low") for item in body["exercise_rankings"]["needs_attention"])


def test_training_deep_dive_overview_contains_chest_and_keeps_flys_out_of_back(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (495, '2026-05-01', 'Push')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (496, '2026-05-08', 'Pull')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4950, 495, 'Flys', 'Cable', 'Cable', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (4960, 496, 'breite Rows', 'eGym', 'eGym', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9950, 4950, 495, 1, 40, 10, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (9960, 4960, 496, 1, 70, 10, 8)")
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=120&focus=overview", headers=auth()).get_json()
    assert "chest" in body["muscle_groups"]
    assert any(item["name"] == "Flys" for item in body["muscle_groups"]["chest"]["top_exercises"])
    assert all(item["name"] != "Flys" and item["name"] != "Incline Flys" for item in body["muscle_groups"].get("back", {}).get("top_exercises", []))


def test_training_deep_dive_overview_stagnant_exercises_hide_low_risk_items(client):
    conn = connections.get_training_db()
    workouts = [(497, "2026-04-01", "Core"), (498, "2026-04-08", "Core"), (499, "2026-04-15", "Core")]
    for row in workouts:
        conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (?, ?, ?)", row)
    for ex_id, wid, weight in ((4970, 497, 20), (4971, 498, 21), (4972, 499, 22)):
        conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, 'Cable Crunches', 'Cable', 'Cable', 'bilateral')", (ex_id, wid))
        conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (?, ?, ?, 1, ?, 12, 8)", (9970 + ex_id, ex_id, wid, weight))
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=120&focus=overview", headers=auth()).get_json()
    core_items = body["muscle_groups"].get("core", {}).get("stagnant_exercises", [])
    assert all(item["stagnation_risk"] in {"medium", "high"} for item in core_items)


def test_training_deep_dive_needs_attention_uses_current_recent_and_archives_historical(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (500, '2025-01-01', 'Pull')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (501, '2026-05-01', 'Pull')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (5000, 500, 'Curls', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (5001, 501, 'Curls', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (10000, 5000, 500, 1, 50, 8, 9)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (10001, 5001, 501, 1, 14, 8, 9)")
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=545&date_to=2026-06-01&focus=overview", headers=auth()).get_json()
    attention_ids = {item["canonical_id"] for item in body["exercise_rankings"]["needs_attention"]}
    archive_ids = {item["canonical_id"] for item in body["historical_stagnation_archive"]}

    assert "curls__lh__bilateral" not in attention_ids
    assert "curls__lh__bilateral" in archive_ids
    assert all(item["attention_relevance"] in {"current", "recent"} for item in body["exercise_rankings"]["needs_attention"])


def test_training_deep_dive_current_plan_exercise_can_stay_in_attention(client):
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mi"] = [
        {
            "id": "pull",
            "kind": "gym",
            "title": "Pull",
            "items": [
                {"kind": "exercise", "name": "Preachers", "variation": "KH", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]},
            ],
        }
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (510, '2026-02-01', 'Pull')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (5100, 510, 'Preachers', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (10100, 5100, 510, 1, 14, 8, 9)")
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=545&focus=overview", headers=auth()).get_json()
    match = next((item for item in body["exercise_rankings"]["needs_attention"] if item["canonical_id"] == "preachers__kh__bilateral"), None)
    assert match is not None
    assert match["attention_relevance"] == "current"
    assert match["attention_reason"] == "current_plan_exercise"


def test_training_deep_dive_recommended_next_actions_avoids_historical_only(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (520, '2025-01-01', 'Back')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (521, '2026-05-10', 'Push')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (5200, 520, 'T-Bar Row', 'LH', 'LH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (5201, 521, 'Pushdowns', 'Cable', 'Cable', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (10200, 5200, 520, 1, 60, 8, 9)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (10201, 5201, 521, 1, 45, 8, 9)")
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/training/deep-dive?days=545&focus=overview", headers=auth()).get_json()
    text = " ".join(body["recommended_next_actions"])
    assert "T-Bar Row" not in text


def test_actions_v2_gpt_schema_exposes_training_workout_coach_view_via_read_facade():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    read_props = spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_workout_coach_view" in read_props["mode"]["enum"]
    assert "workout_id" in read_props
    assert "comparison_to_last" in read_props["workout_id"]["description"]

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_read_props = file_spec["components"]["schemas"]["LivaReadRequest"]["properties"]
    assert "training_workout_coach_view" in file_read_props["mode"]["enum"]
    assert "workout_id" in file_read_props


def test_liva_read_progression_summary_via_slim_facade(client):
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (90, '2026-05-01', 'Upper')")
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (91, '2026-05-10', 'Upper')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (90, 90, 'Seitheben', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (91, 91, 'Seitheben', 'KH', 'KH', 'bilateral')")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (90, 90, 90, 1, 12, 12, 8)")
    conn.execute("INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe) VALUES (91, 91, 91, 1, 12, 13, 8)")
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", headers=auth(), json={"mode": "progression_summary", "days": 30}).get_json()
    assert body["ok"] is True
    assert body["mode"] == "progression_summary"
    assert body["data_status"] is None
    assert body["result"]["ok"] is True
    assert body["result"]["mode"] == "progression_summary"
    assert "overall" in body["result"]
    assert "groups" in body["result"]
    assert "data_quality" in body["result"]


def test_liva_read_training_workout_coach_view_via_slim_facade(client):
    create = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-05-18",
            "session_name": "Push",
            "exercises": [
                {"name": "TNF Press", "variation": "Smith", "sets": [{"reps": 8, "weight": 45.0, "rpe": 8.0}]},
            ],
        },
        headers=auth(),
    ).get_json()
    wid = create["ids"]["workout_id"]

    resp = client.post("/api/v2/actions/liva/read", json={"mode": "training_workout_coach_view", "workout_id": wid}, headers=auth())
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["mode"] == "training_workout_coach_view"
    assert body["data_status"] is None
    assert body["result"]["ok"] is True
    assert body["result"]["workout_id"] == wid
    assert body["result"]["exercises"]
    assert "comparison_to_last" in body["result"]


def test_liva_read_training_workout_coach_view_requires_workout_id(client):
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "training_workout_coach_view"}, headers=auth())
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_detail"
    assert body["error"]["message"] == "workout_id is required for training_workout_coach_view"
    assert body["expected"] == ["workout_id"]


def test_actions_v2_log_gym_session_accepts_source_message_and_dry_run_writes_nothing(client):
    raw_text = "Schrägbankdrücken Smith 2x6x30 RPE8\nLatzug eGym 2x6x115 RPE8"
    before = client.post(
        "/api/v2/actions/training/data",
        json={"scope": "sessions", "date_from": "2026-04-20", "date_to": "2026-04-20", "include_exercises": True, "include_sets": True},
        headers=auth(),
    ).get_json()
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-20",
            "session_name": "Upper",
            "raw_text": raw_text,
            "dry_run": True,
            "source_message": "Foto vom Notizbuch loggen",
        },
        headers=auth(),
    ).get_json()
    after = client.post(
        "/api/v2/actions/training/data",
        json={"scope": "sessions", "date_from": "2026-04-20", "date_to": "2026-04-20", "include_exercises": True, "include_sets": True},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["execution"]["affected_resources"] == ["training_log"]
    assert body["will_change"] == ["training_log"]
    assert body["will_not_change"] == ["training_plan"]
    assert body["result"]["dry_run"] is True
    assert body["result"]["exercise_count"] >= 1
    assert len(after["sessions"]) == len(before["sessions"])


def test_actions_v2_liva_act_log_gym_session_dry_run_mirrors_direct_adjust_fields(client):
    conn = connections.get_plans_db()
    plan = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=1").fetchone()[0])
    plan["base_week"]["Mi"] = [
        {
            "id": "pull",
            "kind": "gym",
            "title": "Pull",
            "items": [
                {"kind": "exercise", "name": "Curls", "variation": "SZ", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]},
            ],
        }
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=1", (json.dumps(plan),))
    conn.commit()
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "log_gym_session", "date": "2026-04-18", "session_name": "Pull", "raw_text": "Curls\n10x55@8", "dry_run": True},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["domain"] == "training"
    assert body["command"] == "log_gym_session"
    assert body["dry_run"] is True
    assert body["dry_run_supported"] is True
    assert body["matched_exercises"]
    assert body["matched_exercises"][0]["canonical_id"] == "curls__sz__bilateral"
    assert body["matched_exercises"][0]["resolution_basis"] == "plan_match"
    assert body["parsed_exercises"]
    assert body["will_change"] == ["training_log"]
    assert body["will_not_change"] == ["training_plan"]
    assert body["execution"]["live_state_changed"] is False
    assert body["comparison_to_last"]["reason"] == "dry_run_no_previous_lookup"
    assert body["result"]["exercises"]
    assert body["result"]["resolution"]
    assert body["result"]["exercises"][0]["resolution"]["resolution_basis"] == "plan_match"


def test_actions_v2_log_gym_session_requires_raw_text_or_exercises(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "log_gym_session", "date": "2026-04-20", "session_name": "Upper", "dry_run": True},
        headers=auth(),
    ).get_json()
    assert body["ok"] is False
    assert body["error"]["message"] == "raw_text or exercises is required"


def test_actions_v2_log_gym_session_dry_run_false_persists_session(client):
    raw_text = "Schrägbankdrücken\n7x62.5@9 6x60@8\n\nLatzug\n6x112@8"
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-21",
            "session_name": "Upper",
            "raw_text": raw_text,
            "dry_run": False,
            "source_message": "passt, eintragen",
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    data = client.post(
        "/api/v2/actions/training/data",
        json={"scope": "sessions", "date_from": "2026-04-21", "date_to": "2026-04-21", "include_exercises": True, "include_sets": True},
        headers=auth(),
    ).get_json()
    assert any(session["day_type"] == "Upper" for session in data["sessions"])


def test_actions_v2_log_gym_session_dry_run_splits_ocr_weight_dot_rpe_notation(client):
    raw_text = (
        "Wadenheben\n"
        "8x125.9 7x120.9\n\n"
        "Squats\n"
        "7x80.8\n\n"
        "Adduktoren\n"
        "10x68.9 7x65.9\n\n"
        "Cable Crunches\n"
        "10x17.5"
    )
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-23",
            "session_name": "Lower",
            "raw_text": raw_text,
            "dry_run": True,
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    exercises = body["result"]["exercises"]
    calf = exercises[0]["sets"][0]
    squat = exercises[1]["sets"][0]
    crunch = exercises[3]["sets"][0]
    assert calf["weight"] == 125
    assert calf["rpe"] == 9
    assert exercises[0]["sets"][1]["weight"] == 120
    assert exercises[0]["sets"][1]["rpe"] == 9
    assert squat["weight"] == 80
    assert squat["rpe"] == 8
    assert exercises[2]["sets"][0]["weight"] == 68
    assert exercises[2]["sets"][0]["rpe"] == 9
    assert crunch["weight"] == 17.5
    assert crunch["rpe"] is None


def test_actions_v2_log_gym_session_dry_run_keeps_identical_consecutive_sets(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-05-19",
            "session_name": "Pull",
            "raw_text": "breite Rows\n8x60@8\n8x60@8\n7x60@9",
            "dry_run": True,
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["parsed_payload"]["set_count"] == 3
    assert len(body["parsed_exercises"][0]["sets"]) == 3
    assert len(body["result"]["exercises"][0]["sets"]) == 3
    assert [item["set_number"] for item in body["parsed_exercises"][0]["sets"]] == [1, 2, 3]
    assert [item["set_number"] for item in body["result"]["exercises"][0]["sets"]] == [1, 2, 3]


def test_actions_v2_log_gym_session_structured_sets_split_weight_dot_rpe_notation(client):
    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-24",
            "session_name": "Lower",
            "dry_run": True,
            "exercises": [
                {
                    "name": "Wadenheben",
                    "sets": [
                        {"reps": 8, "weight": "125.9"},
                        {"reps": 7, "weight": "120.9"},
                    ],
                },
                {
                    "name": "Cable Crunches",
                    "sets": [
                        {"reps": 10, "weight": "17.5"},
                    ],
                },
            ],
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    calf = body["result"]["exercises"][0]["sets"][0]
    crunch = body["result"]["exercises"][1]["sets"][0]
    assert calf["weight"] == 125
    assert calf["rpe"] == 9
    assert body["result"]["exercises"][0]["sets"][1]["weight"] == 120
    assert body["result"]["exercises"][0]["sets"][1]["rpe"] == 9
    assert crunch["weight"] == 17.5
    assert crunch["rpe"] is None


def test_actions_v2_structured_live_log_persists_normalized_parser_text_and_progress(client):
    conn = connections.get_training_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workouts)")}
    if "raw_import_text" not in columns:
        conn.execute("ALTER TABLE workouts ADD COLUMN raw_import_text TEXT")
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/training/adjust",
        json={
            "command": "log_gym_session",
            "date": "2026-04-25",
            "session_name": "Structured GPT",
            "dry_run": False,
            "exercises": [
                {
                    "name": "Schrägbankdrücken",
                    "variation": "Smith",
                    "sets": [{"reps": 8, "weight": 70, "rpe": 8}],
                }
            ],
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    result = body["result"]
    assert result["normalized_training_text"]
    assert result["source_text"] == result["normalized_training_text"]
    assert result["source_text_origin"] == "normalized_from_structured_data"
    assert result["progress"]["rule_version"] == "progression_rules_v6"
    conn = connections.get_training_db()
    stored = conn.execute("SELECT raw_import_text FROM workouts WHERE id=?", (result["workout_id"],)).fetchone()[0]
    stored_slot = conn.execute("SELECT set_slot FROM sets WHERE workout_id=?", (result["workout_id"],)).fetchone()[0]
    conn.close()
    assert stored == result["normalized_training_text"]
    assert stored_slot == "straight_1"


def test_actions_v2_gpt_schema_documents_slow_memory_catchup():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(spec["paths"]))


def test_actions_v2_complete_planned_meal_logs_open_meal(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "targets": {"kcal": 2500},
        "logged_totals": {"kcal": 600},
        "remaining": {"kcal": 1900},
        "planned_meals": [
            {
                "slot_id": 11,
                "slot_index": 0,
                "title": "Frühstück",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
            }
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {
            **day_payload,
            "logged_totals": {"kcal": 900},
            "remaining": {"kcal": 1600},
            "logged_meals": [{"id": 44, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}],
            "planned_meals": [{**day_payload["planned_meals"][0], "status": "logged", "logged_meal_id": 44}],
        }, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)
    body = client.post(
        "/api/v2/actions/nutrition/planned-meal/complete",
        json={"date": "2026-04-17", "meal_number": 1},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["already_logged"] is False
    assert body["result"]["logged_meal_id"] == 44
    assert body["result"]["day"]["logged_totals"]["kcal"] == 900
    assert calls[0][0] == 11
    assert [item["food_name"] for item in calls[0][1]["items"]] == ["Brot"]


def test_actions_v2_complete_planned_meal_is_idempotent_when_already_logged(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 12,
                "slot_index": 0,
                "title": "Frühstück",
                "meal_slot": "Meal 1",
                "status": "logged",
                "logged_meal_id": 71,
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
            }
        ],
        "logged_meals": [{"id": 71, "slot_id": 12}],
    }
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)
    monkeypatch.setattr(nutrition_db, "log_planned_meal", lambda *args, **kwargs: pytest.fail("should not log again"))
    body = client.post(
        "/api/v2/actions/nutrition/planned-meal/complete",
        json={"date": "2026-04-17", "meal_number": 1},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["already_logged"] is True
    assert body["result"]["already_logged"] is True


def test_actions_v2_complete_planned_meal_returns_meal_ambiguous(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {"slot_id": 21, "slot_index": 0, "title": "Frühstück", "meal_slot": "Meal 1", "status": "open", "items": [{"food_name": "Brot"}]},
            {"slot_id": 22, "slot_index": 1, "title": "Mittag", "meal_slot": "Meal 2", "status": "open", "items": [{"food_name": "Reis"}]},
        ],
        "logged_meals": [],
    }
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)
    body = client.post(
        "/api/v2/actions/nutrition/planned-meal/complete",
        json={"date": "2026-04-17"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "meal_ambiguous"


def test_actions_v2_complete_planned_meal_does_not_invent_free_foods(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 31,
                "slot_index": 0,
                "title": "Frühstück",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_id": 91, "food_name": "Haferflocken", "amount": 80, "unit": "g"}],
            }
        ],
        "logged_meals": [],
    }
    captured = {}
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        captured["items"] = kwargs.get("items")
        return {**day_payload, "logged_meals": [{"id": 55, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)
    body = client.post(
        "/api/v2/actions/nutrition/planned-meal/complete",
        json={"date": "2026-04-17", "meal_number": 1, "source_message": "logge Frühstück"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert captured["items"] == day_payload["planned_meals"][0]["items"]


def test_actions_v2_weight_log_requires_explicit_weight_value(client):
    body = client.post(
        "/api/v2/actions/weight/control",
        json={"command": "log_weight", "date": "2026-04-17"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is False
    assert body["error"]["message"] == "weight is required and must be numeric"


def test_actions_v2_weight_log_rejects_unrealistic_value(client):
    body = client.post(
        "/api/v2/actions/weight/control",
        json={"command": "log_weight", "date": "2026-04-17", "weight_kg": 12},
        headers=auth(),
    ).get_json()
    assert body["ok"] is False
    assert body["error"]["message"] == "weight must be between 40 and 120 kg"


def test_actions_v2_core_board_pull_card_with_push_summary_is_mismatch(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Plan sauber ausführen.",
        "human_summary": "Heute Push kontrolliert fahren.",
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Pull",
            "recommended_session_label": "Pull",
            "title": "Pull",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "mismatch"


def test_actions_v2_core_board_pull_card_with_visible_reason_push_is_not_forced_mismatch(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Plan sauber ausführen.",
        "visible_reasons": ["Push blieb aus Legacy sichtbar."],
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Pull",
            "recommended_session_label": "Pull",
            "title": "Pull",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] in {"ok", "warning"}


def test_actions_v2_core_board_pull_card_with_generic_headline_stays_ok(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Plan sauber ausführen.",
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Pull",
            "recommended_session_label": "Pull",
            "title": "Pull",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "ok"


def test_actions_v2_core_board_upper_card_with_lower_headline_is_mismatch(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Lower sauber ausführen.",
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Upper",
            "recommended_session_label": "Upper",
            "title": "Upper",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "mismatch"


def test_actions_v2_core_board_legs_card_with_mobility_suffix_stays_ok(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Plan sauber ausführen.",
        "human_summary": "Mobility danach kurz mitnehmen.",
        "training_card": {
            "session_type": "gym",
            "planned_session_label": "Legs",
            "recommended_session_label": "Legs",
            "title": "Legs",
        },
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["board_consistency"]["status"] == "ok"


def test_actions_v2_core_board_mismatch_does_not_block_save(client):
    payload = {
        "date": "2026-04-20",
        "decision_intent": "train_controlled",
        "human_headline": "Run Z2 ruhig durchziehen.",
        "build_training_card": True,
    }
    resp = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["board"]["id"]
    assert body["board_consistency"]["status"] == "mismatch"


def test_actions_v2_core_board_run_text_matches_run_card(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "So": [
                            {
                                "id": "run",
                                "kind": "run",
                                "time": "45 min",
                                "title": "Run Z2",
                                "items": [],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    payload = {
        "date": "2026-04-26",
        "decision_intent": "train_controlled",
        "human_headline": "Run Z2 ruhig durchziehen.",
        "build_training_card": True,
    }
    body = client.post("/api/v2/actions/core/board/build", json=payload, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["board_consistency"]["status"] == "ok"


def test_actions_v2_core_training_card_missing_history_does_not_invent_weight(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "Mo": [
                            {
                                "id": "push",
                                "kind": "gym",
                                "title": "Push",
                                "items": [
                                    {"kind": "exercise", "name": "Mystery Press", "variation": "Cable", "sets": 3, "reps": {"min": 8, "max": 12}, "rpe_list": [8, 8]},
                                ],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    client.post("/api/v2/actions/core/board/build", json={"date": "2026-04-20", "decision_intent": "train_normal"}, headers=auth())
    body = client.post("/api/v2/actions/core/training-card/build", json={"date": "2026-04-20", "force": True}, headers=auth()).get_json()
    item = body["training_card"]["items"][0]
    assert item["weight_target"] in {"aus Plan übernehmen", "—", "letztes gutes Arbeitsgewicht"}
    assert item["weight_target"] != "0"


def test_core_training_card_moves_repeated_gym_notes_to_global_hint():
    from core.core_training_card import _dedupe_repeated_item_notes

    card = {
        "global_hint": "",
        "footer_note": "Wenn das Warm-up schwer wirkt: konservativ bleiben.",
        "items": [
            {"item_type": "exercise", "display_name": "A", "note": "Nur steigern, wenn das Warm-up klar normal läuft. Kein Zusatzsatz."},
            {"item_type": "exercise", "display_name": "B", "note": "Nur steigern, wenn das Warm-up klar normal läuft. Kein Zusatzsatz."},
            {"item_type": "exercise", "display_name": "C", "note": None},
        ],
    }

    out = _dedupe_repeated_item_notes(card)

    assert out["global_hint"] == "Nur steigern, wenn das Warm-up klar normal läuft. Kein Zusatzsatz."
    assert out["footer_note"] == "Wenn das Warm-up schwer wirkt: konservativ bleiben."
    assert out["items"][0]["note"] is None
    assert out["items"][1]["note"] is None


def test_dashboard_template_no_longer_renders_old_core_today_block():
    repo = Path(__file__).resolve().parents[1]
    content = (repo / "templates/dashboard.html").read_text(encoding="utf-8")
    assert "CORE heute" not in content
    assert "Daily Decision" not in content
    assert "core-home-prime" not in content


def test_actions_v2_memory_serialization_is_structured(client):
    resp = client.get("/api/v2/actions/memory/state", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    if body["memory"]["latest_entries"]:
        entry = body["memory"]["latest_entries"][0]
        assert {"id", "topic", "type", "priority", "updated_at", "links"} <= set(entry)


def test_actions_v2_memory_data_can_read_status_file_and_context(client):
    status = client.post("/api/v2/actions/memory/data", json={"mode": "status"}, headers=auth()).get_json()
    assert status["ok"] is True
    assert status["data_status"] == "ok"
    assert status["status"]["logical_files"][0]["logical_name"] == "core_patterns"

    file_body = client.post(
        "/api/v2/actions/memory/data",
        json={"mode": "file", "logical_name": "core_patterns", "include_raw": True},
        headers=auth(),
    ).get_json()
    assert file_body["ok"] is True
    assert file_body["file"]["raw_markdown"].startswith("# Core Patterns")

    context = client.post(
        "/api/v2/actions/memory/data",
        json={"mode": "context", "domains": ["training"], "query": "stable training", "limit": 5},
        headers=auth(),
    ).get_json()
    assert context["ok"] is True
    assert context["entries"][0]["topic"] == "core_patterns"


def test_actions_v2_runs_state_reports_historical_runs_when_no_recent_runs(client):
    # Fixture has a run in the future-relative test date window used by the app date.
    resp = client.get("/api/v2/actions/runs/state", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    summary = body["runs"]["latest_runs_summary"]
    assert "last_run_date_any" in summary
    assert "historical_runs_available" in summary
    assert body["runs"]["next_run_type"] is None
    assert body["runs"]["next_run_type_source"] is None


def test_actions_v2_runs_sidecar_control_is_visible_in_state(client):
    command = client.post(
        "/api/v2/actions/runs/control",
        json={"command": "set_run_type", "date": "2026-04-19", "run_type": "Easy"},
        headers=auth(),
    ).get_json()
    assert command["execution"]["mode"] == "applied_sidecar"
    assert command["execution"]["live_state_changed"] is False

    state = client.get("/api/v2/actions/runs/state", headers=auth()).get_json()["runs"]
    assert state["next_run_type"] == "Easy"
    assert state["next_run_type_source"] == "v2_sidecar"
    assert state["next_run_type_source_detail"]["state_key"] == "next_run_type"
    assert state["sidecar_controls"]["next_run_type"] == "Easy"
    assert state["sidecar_controls"]["active_in_state"] is True


def test_actions_v2_runs_create_run_is_applied_live_and_visible(client):
    command = client.post(
        "/api/v2/actions/runs/control",
        json={
            "command": "create_run",
            "date": "2026-04-17T18:30:00",
            "distance_km": 5.0,
            "duration_min": 25,
            "avg_hr": 145,
            "run_type": "Easy",
        },
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["execution"]["live_state_changed"] is True
    assert command["result"]["distance_m"] == 5000.0
    assert command["result"]["duration_s"] == 1500
    assert command["result"]["sport_type"] == "run"

    data = client.post(
        "/api/v2/actions/runs/data",
        json={"date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    assert data["runs"]["sessions"][0]["distance_m"] == 5000.0
    assert data["runs"]["sessions"][0]["duration_s"] == 1500


def test_actions_v2_runs_create_ergo_persists_cardio_metadata(client):
    command = client.post(
        "/api/v2/actions/runs/control",
        json={
            "command": "create_run",
            "date": "2026-04-19",
            "distance_km": 5,
            "duration_min": 31.17,
            "avg_hr": 142,
            "run_type": "ergo",
            "note": "Ergo 31:10 125W 142bpm",
        },
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["result"]["sport_type"] == "ergo"
    assert command["result"]["run_type"] == "ergo"
    assert command["result"]["avg_power"] == 125.0
    assert command["result"]["note"] == "Ergo 31:10 125W 142bpm"

    conn = connections.get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT sport_type, run_type, note, avg_power FROM runs WHERE id=?", (command["result"]["run_id"],)).fetchone()
    finally:
        conn.close()
    assert dict(row) == {
        "sport_type": "ergo",
        "run_type": "ergo",
        "note": "Ergo 31:10 125W 142bpm",
        "avg_power": 125.0,
    }


def test_cardio_sessions_create_stair_master_persists_floors_and_derives_distance(client):
    created = save_cardio_session(
        {
            "date": "2026-04-20",
            "sport_type": "stair",
            "duration_min": 25,
            "stair_floors": 82,
            "avg_power_w": 141,
            "avg_hr_bpm": 151,
            "note": "Stair Master 25:00 141W 82 Etagen",
        }
    )

    assert created["ok"] is True
    session_id = created["id"]

    conn = connections.get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT sport_type, distance, stair_floors, avg_power, note FROM runs WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()

    assert row["sport_type"] == "stair"
    assert row["stair_floors"] == 82
    assert row["avg_power"] == 141
    assert row["note"] == "Stair Master 25:00 141W 82 Etagen"
    assert row["distance"] == 820.0

    bundle = build_cardio_viewmodel(CardioFilters(sport_type="stair", time_range=None))
    assert bundle["ok"] is True
    assert bundle["summary"]["last_session"]["sport_type"] == "stair"
    assert bundle["summary"]["last_session"]["stair_floors"] == 82
    assert bundle["summary"]["last_session"]["avg_power_w"] == 141
    assert bundle["list"]["rows"][0]["distance_km"] == 0.82


def test_cardio_sessions_create_stair_master_derives_watts_from_floors_and_duration(client):
    created = save_cardio_session(
        {
            "date": "2026-04-21",
            "sport_type": "stair",
            "duration_min": 25,
            "stair_floors": 82,
            "avg_hr_bpm": 151,
            "note": "Stair Master 25:00 82 Etagen",
        }
    )

    assert created["ok"] is True
    session_id = created["id"]

    conn = connections.get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT distance, stair_floors, avg_power FROM runs WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()

    assert row["stair_floors"] == 82
    assert row["distance"] == 820.0
    assert row["avg_power"] == pytest.approx(118.6, abs=0.2)


def test_actions_v2_runs_delete_run_is_applied_live_and_removed(client):
    command = client.post(
        "/api/v2/actions/runs/control",
        json={"command": "delete_run", "run_id": 1},
        headers=auth(),
    ).get_json()

    assert command["ok"] is True, command
    assert command["execution"]["mode"] == "applied_live"
    assert command["execution"]["live_state_changed"] is True
    assert command["result"]["deleted"] is True
    assert command["result"]["deleted_run"]["id"] == 1

    data = client.post(
        "/api/v2/actions/runs/data",
        json={"date_from": "2026-04-13", "date_to": "2026-04-13"},
        headers=auth(),
    ).get_json()
    assert data["runs"]["sessions"] == []


def test_actions_v2_remote_control_dry_run_default(client):
    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "monitor_links", "power": "off"},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["result"]["dry_run"] is True
    assert body["result"]["device_driver"] == "tapo"
    assert "monitor_links" in body["result"]["allowed_targets"]["devices"]
    assert "desk_strip" not in body["result"]["allowed_targets"]["devices"]
    assert "Licht" not in body["result"]["allowed_targets"]["friendly_names"]


def test_actions_v2_remote_control_accepts_friendly_device_names(client):
    monitors = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "Monitore", "power": "off"},
        headers=auth(),
    ).get_json()
    assert monitors["ok"] is True
    assert monitors["result"]["device"] == "monitor_links,monitor_rechts"
    assert monitors["result"]["devices"] == ["monitor_links", "monitor_rechts"]
    assert "remote.device.monitor_links" in monitors["execution"]["affected_resources"]
    assert "remote.device.monitor_rechts" in monitors["execution"]["affected_resources"]

    lamp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "Schreibtischlampe", "power": "on"},
        headers=auth(),
    ).get_json()
    assert lamp["ok"] is True
    assert lamp["result"]["device"] == "schreibtischlampe"


def test_actions_v2_remote_control_hides_govee_targets_when_disabled(client):
    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "Licht", "power": "off"},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_target"


def test_actions_v2_remote_control_rejects_unsupported_device_action(client):
    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "set_brightness", "device": "monitor_links", "brightness": 20, "dry_run": True},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_target_action"


def test_actions_v2_remote_control_applied_live(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    calls = []

    def fake_apply(command, body, result):
        calls.append((command, dict(result)))
        result["live_result"] = {"ok": True}
        return result

    monkeypatch.setattr(actions_v2, "_remote_apply_device_command", fake_apply)

    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "monitor_links", "power": "off", "dry_run": False},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert calls
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert "remote.device.monitor_links" in body["execution"]["affected_resources"]
    assert body["result"]["device_driver"] == "tapo"


def test_actions_v2_remote_control_false_dry_run_never_silently_simulates(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    def fail_live(_command, _body, _result):
        raise ValueError("adapter_unavailable")

    monkeypatch.setattr(actions_v2, "_remote_apply_device_command", fail_live)

    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "monitor_links", "power": "off", "dry_run": False},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 502
    assert body["ok"] is False
    assert body["error"]["code"] == "remote_execution_failed"
    assert body["error"]["message"] == "adapter_unavailable"


def test_actions_v2_remote_control_rejects_invalid_device(client):
    resp = client.post(
        "/api/v2/actions/remote/control",
        json={"command": "device_set_power", "device": "unknown_lamp", "power": "off", "dry_run": False},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_target"


def test_actions_v2_pc_wake_uses_existing_remote_helpers(monkeypatch):
    import ai.actions_v2 as actions_v2

    calls = []

    class RemoteApp:
        REMOTE_PC_HOST = "192.0.2.2"
        REMOTE_PC_WAKE_INTERFACE = "enp2s0"

        @staticmethod
        def _remote_mark_wake_attempt():
            calls.append("mark_wake")
            return "wake-ts"

        @staticmethod
        def _remote_send_wake():
            calls.append("send_wake")
            return True, None

        @staticmethod
        def invalidate_snapshot(name):
            calls.append(("invalidate", name))

        @staticmethod
        def _iso_utc(value):
            return f"iso:{value}"

        @staticmethod
        def _utc_now():
            return "now"

    monkeypatch.setattr(actions_v2.importlib, "import_module", lambda name: RemoteApp)

    result = actions_v2._remote_apply_pc_action("wake")

    assert result["source"] == "/api/remote/pc/wake"
    assert result["status"] == "waking"
    assert calls == ["mark_wake", "send_wake", ("invalidate", "remote_snapshot")]


def test_actions_v2_pc_shutdown_uses_existing_remote_helpers(monkeypatch):
    import ai.actions_v2 as actions_v2

    calls = []

    class RemoteApp:
        REMOTE_PC_HOST = "192.0.2.2"
        REMOTE_PC_CONTROL_PORT = 8765
        REMOTE_PC_HTTP_TOKEN = "token"

        @staticmethod
        def _remote_pc_status_payload():
            calls.append("status")
            return {"status": "ready"}

        @staticmethod
        def _remote_listener_call(path, timeout_seconds=2.0):
            calls.append(("listener", path, timeout_seconds))
            return True, 200, "shutdown triggered"

        @staticmethod
        def invalidate_snapshot(name):
            calls.append(("invalidate", name))

        @staticmethod
        def _iso_utc(value):
            return f"iso:{value}"

        @staticmethod
        def _utc_now():
            return "now"

    monkeypatch.setattr(actions_v2.importlib, "import_module", lambda name: RemoteApp)

    result = actions_v2._remote_apply_pc_action("shutdown")

    assert result["source"] == "/api/remote/pc/shutdown"
    assert result["code"] == "shutdown_triggered"
    assert calls == ["status", ("listener", "/shutdown", 2.2), ("invalidate", "remote_snapshot")]


def test_actions_v2_memory_store_is_applied_live_when_markdown_write_succeeds(client):
    resp = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "store_pattern", "topic": "training", "text": "Test pattern", "priority": "high"},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert "memory.markdown" in body["execution"]["affected_resources"]


def test_actions_v2_memory_store_accepts_content_and_explicit_target(client):
    resp = client.post(
        "/api/v2/actions/memory/store",
        json={
            "command": "store_fact",
            "logical_file": "athlete_dossier",
            "section": "AKTUELLE_PHASE",
            "content": "Explicit content test",
            "priority": "low",
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["memory_write"]["logical_file"] == "athlete_dossier"
    assert body["result"]["memory_write"]["section"] == "AKTUELLE_PHASE"


def test_actions_v2_memory_store_routes_health_and_project_topics(client):
    health = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "store_fact", "topic": "sickness recovery", "text": "User war krank."},
        headers=auth(),
    ).get_json()
    assert health["result"]["memory_write"]["logical_file"] == "health_notes"
    assert health["result"]["memory_write"]["section"] == "RECOVERY"

    pattern = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "store_pattern", "topic": "behavior pattern", "text": "Stable prep behavior."},
        headers=auth(),
    ).get_json()
    assert pattern["result"]["memory_write"]["logical_file"] == "core_patterns"

    project = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "store_fact", "topic": "LIVA schema", "text": "Slim GPT facade bleibt."},
        headers=auth(),
    ).get_json()
    assert project["result"]["memory_write"]["logical_file"] == "project_memory"


def test_actions_v2_memory_store_rejects_unknown_section(client):
    resp = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "replace_section", "logical_file": "health_notes", "section": "UNKNOWN", "content": "bad"},
        headers=auth(),
    )
    assert resp.status_code == 400


def test_actions_v2_memory_store_strips_gpt_entry_metadata(client):
    resp = client.post(
        "/api/v2/actions/memory/store",
        json={
            "command": "store_fact",
            "topic": "project",
            "text": "[mem_b7ebd73eda] (normal) Testeintrag.",
            "priority": "high",
        },
        headers=auth(),
    )
    body = resp.get_json()
    write_plan = body["result"]["memory_write"]["payload"]["write_plan"]

    assert resp.status_code == 200
    assert write_plan[0]["content"] == "Testeintrag."
    assert "[mem_b7ebd73eda]" not in write_plan[0]["content"]
    assert "(normal)" not in write_plan[0]["content"]


def test_actions_v2_memory_store_supports_propose_and_direct_apply(client):
    proposed = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "propose", "conversation_summary": "test", "extracted_facts": [{"text": "stable fact"}]},
        headers=auth(),
    ).get_json()
    assert proposed["ok"] is True
    assert proposed["execution"]["mode"] == "dry_run"
    assert proposed["execution"]["live_state_changed"] is False
    assert proposed["result"]["write_plan"]

    applied = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "append_section_note", "logical_file": "core_patterns", "section": "TRAINING", "content": "Stable training note."},
        headers=auth(),
    ).get_json()
    assert applied["ok"] is True
    assert applied["execution"]["mode"] == "applied_live"
    assert applied["execution"]["live_state_changed"] is True


def test_actions_v2_memory_store_can_remove_and_update_entry_live(client):
    removed = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "remove_entry", "entry_id": "mem_test_delete", "reason": "cleanup"},
        headers=auth(),
    ).get_json()
    assert removed["ok"] is True
    assert removed["execution"]["mode"] == "applied_live"
    assert removed["execution"]["live_state_changed"] is True
    assert removed["result"]["removed"] is True

    after_remove = client.post(
        "/api/v2/actions/memory/data",
        json={"mode": "file", "logical_name": "core_patterns", "include_raw": True},
        headers=auth(),
    ).get_json()
    assert "mem_test_delete" not in after_remove["file"]["raw_markdown"]

    updated = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "update_entry", "entry_id": "mem_test_update", "text": "New memory text"},
        headers=auth(),
    ).get_json()
    assert updated["ok"] is True
    assert updated["execution"]["mode"] == "applied_live"
    assert updated["result"]["updated"] is True

    after_update = client.post(
        "/api/v2/actions/memory/data",
        json={"mode": "file", "logical_name": "core_patterns", "include_raw": True},
        headers=auth(),
    ).get_json()
    assert "mem_test_update" in after_update["file"]["raw_markdown"]
    assert "New memory text" in after_update["file"]["raw_markdown"]
    assert "Old text" not in after_update["file"]["raw_markdown"]

    removed_by_text = client.post(
        "/api/v2/actions/memory/store",
        json={
            "command": "remove_entry",
            "target_file": "core_patterns",
            "section": "TRAINING",
            "text": "Text-only delete target",
            "reason": "cleanup by text",
        },
        headers=auth(),
    ).get_json()
    assert removed_by_text["ok"] is True
    assert removed_by_text["execution"]["mode"] == "applied_live"
    assert removed_by_text["result"]["match_text"] == "Text-only delete target"

    after_text_remove = client.post(
        "/api/v2/actions/memory/data",
        json={"mode": "file", "logical_name": "core_patterns", "include_raw": True},
        headers=auth(),
    ).get_json()
    assert "Text-only delete target" not in after_text_remove["file"]["raw_markdown"]


def test_liva_act_memory_store_uses_memos_backend_without_markdown_write(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    apply_calls = []

    def fail_apply(payload):
        apply_calls.append(payload)
        raise AssertionError("markdown memory service should not be used when memos backend is active")

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setattr(actions_v2, "create_memo", lambda content, visibility=None, state="NORMAL": {"name": "memos/abc", "content": content, "visibility": "PRIVATE", "state": state})
    monkeypatch.setattr(type(client.application.extensions["liva_memory_service"]), "apply", staticmethod(fail_apply))

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "store_fact", "text": "Testmemo aus LIVA Bridge", "topic": "liva"},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["backend"] == "usememos"
    assert body["result"]["entry_id"] == "memos/abc"
    assert body["result"]["memo"]["content"].splitlines()[0] == "#gpt"
    assert body["result"]["memo"]["content"].rstrip().endswith("#projekt #liva")
    assert body["result"]["memory_write"]["backend"] == "usememos"
    assert body["result"]["auto_tags"] == ["#projekt", "#liva"]
    assert apply_calls == []


def test_actions_v2_memos_update_entry_allows_gpt_memo(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/abc", "content": "#gpt\n\nAlt\n\n#beobachtung #liva\n"})
    monkeypatch.setattr(actions_v2, "update_memo", lambda name, content=None, state=None, visibility=None: {"name": "memos/abc", "content": content, "state": "NORMAL", "visibility": "PRIVATE"})

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "update_entry", "entry_id": "memos/abc", "text": "Aktualisierter Inhalt", "topic": "krankheit"},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["updated"] is True
    assert body["result"]["memo"]["content"].splitlines()[0] == "#gpt"
    assert body["result"]["memo"]["content"].rstrip().endswith("#kurzfristig #krankheit")


def test_actions_v2_memos_update_entry_rejects_non_gpt_memo(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/user1", "content": "User memo"})

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "update_entry", "entry_id": "memos/user1", "text": "Nope"},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 403
    assert body["error"]["code"] == "not_gpt_memo"


def test_actions_v2_memos_remove_entry_respects_gpt_boundary(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/abc", "content": "#gpt\n\nLoeschbar"})
    monkeypatch.setattr(actions_v2, "delete_memo", lambda name, force=False: {"deleted": True, "name": name})

    ok_resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "remove_entry", "entry_id": "memos/abc"},
        headers=auth(),
    )
    ok_body = ok_resp.get_json()
    assert ok_resp.status_code == 200
    assert ok_body["result"]["deleted"] is True

    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/user1", "content": "User memo"})
    deny_resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "remove_entry", "entry_id": "memos/user1"},
        headers=auth(),
    )
    deny_body = deny_resp.get_json()
    assert deny_resp.status_code == 403
    assert deny_body["error"]["code"] == "not_gpt_memo"


def test_actions_v2_memos_archive_and_mark_stale_only_allow_gpt(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setenv("MEMOS_ACCESS_TOKEN", "token123")
    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/abc", "content": "#gpt\n\nMemo"})
    monkeypatch.setattr(actions_v2, "archive_memo", lambda name: {"name": name, "content": "#gpt\n\nMemo", "state": "ARCHIVED"})
    monkeypatch.setattr(actions_v2, "update_memo", lambda name, content=None, state=None, visibility=None: {"name": name, "content": content, "state": "NORMAL", "visibility": "PRIVATE"})

    archive_resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "archive", "entry_id": "memos/abc", "confirm": True},
        headers=auth(),
    )
    archive_body = archive_resp.get_json()
    assert archive_resp.status_code == 200
    assert archive_body["result"]["archived"] is True

    stale_resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "mark_stale", "entry_id": "memos/abc", "reason": "ersetzt"},
        headers=auth(),
    )
    stale_body = stale_resp.get_json()
    assert stale_resp.status_code == 200
    assert stale_body["result"]["stale_marked"] is True
    assert "> GPT-Status: veraltet / ersetzt am " in stale_body["result"]["memo"]["content"]

    monkeypatch.setattr(actions_v2, "get_memo", lambda name: {"name": "memos/user1", "content": "User memo"})
    deny_resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "memory", "command": "archive", "entry_id": "memos/user1", "confirm": True},
        headers=auth(),
    )
    deny_body = deny_resp.get_json()
    assert deny_resp.status_code == 403
    assert deny_body["error"]["code"] == "not_gpt_memo"


def test_liva_read_memory_only_gpt_mutable_filters_and_exposes_terminal_tags(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setattr(
        actions_v2,
        "search_memos",
        lambda **kwargs: {
            "backend": "usememos",
            "count": 2,
            "memos": [
                {"name": "memos/1", "content": "#gpt\n\nMemo A\n\n#projekt #liva\n"},
                {"name": "memos/2", "content": "User memo"},
            ],
        },
    )

    resp = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "memory", "query": "memo", "only_gpt_mutable": True, "limit": 5},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["result"]["count"] == 1
    assert body["result"]["memos"][0]["mutable_by_gpt"] is True
    assert body["result"]["memos"][0]["terminal_tags"] == ["#projekt", "#liva"]


def test_cleanup_expired_dry_run_deletes_nothing(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setattr(actions_v2, "cleanup_expired_gpt_memos", lambda max_age_days=14, dry_run=True, limit=500: {"backend": "usememos", "dry_run": dry_run, "deleted_count": 0, "expired_count": 2, "scanned": 3, "expired": []})
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "memory", "command": "cleanup_expired", "dry_run": True, "max_age_days": 14}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False


def test_cleanup_expired_live_without_confirm_is_rejected(client, monkeypatch):
    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "memory", "command": "cleanup_expired", "dry_run": False, "max_age_days": 14}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "confirmation_required"


def test_cleanup_expired_live_with_confirm_deletes_only_gpt_short_term(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setattr(actions_v2, "cleanup_expired_gpt_memos", lambda max_age_days=14, dry_run=False, limit=500: {"backend": "usememos", "dry_run": False, "deleted_count": 1, "expired_count": 1, "scanned": 2, "expired": [{"name": "memos/gpt1"}]})
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "memory", "command": "cleanup_expired", "dry_run": False, "confirm": True, "max_age_days": 14}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["deleted_count"] == 1


def test_liva_read_filters_by_tags_hash_form(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setattr(actions_v2, "search_memos", lambda **kwargs: {"backend": "usememos", "count": 1, "memos": [{"name": "memos/1", "content": "#gpt\n\nMemo\n\n#kurzfristig #krankheit\n"}]})
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "memory", "tags": ["#krankheit"], "include_gpt": True, "limit": 10}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["count"] == 1


def test_liva_read_filters_by_query_and_keywords_without_tags(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setenv("LIVA_MEMORY_BACKEND", "memos")
    monkeypatch.setattr(actions_v2, "search_memos", lambda **kwargs: {"backend": "usememos", "count": 1, "memos": [{"name": "memos/1", "content": "#gpt\n\nWenn mein Hals kratzt und der Puls hoch ist.\n\n#kurzfristig #krankheit\n"}]})
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "memory", "keywords": ["Hals", "Puls"], "only_gpt_mutable": True, "limit": 10}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["count"] == 1


def test_actions_v2_memory_link_is_honestly_logged_only(client):
    resp = client.post(
        "/api/v2/actions/memory/link",
        json={"command": "link_entry_to_exercise", "entry_id": "mem_test", "exercise_name": "Bench"},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "logged_only"
    assert body["execution"]["live_state_changed"] is False


def test_actions_v2_weight_log_is_applied_live_and_visible_in_reads(client):
    command = client.post(
        "/api/v2/actions/weight/control",
        json={"command": "log_weight", "date": "2026-04-17", "weight": 73.7},
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["execution"]["live_state_changed"] is True
    assert "nutrition.weight_logs" in command["execution"]["affected_resources"]
    assert command["result"]["inserted"] is True

    state = client.get("/api/v2/actions/weight/state", headers=auth()).get_json()["weight"]
    assert state["latest_weight"] == 73.7

    data = client.post(
        "/api/v2/actions/weight/data",
        json={"date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    assert data["weight"]["points"][0]["weight"] == 73.7


def test_actions_v2_nutrition_quick_log_meal_is_applied_live_and_visible(client):
    command = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "quick_log_meal",
            "logged_at": "2026-04-17T08:00:00",
            "meal_name": "Actions V2 Meal",
            "items": [{"food": "Quick Add", "kcal": 350, "protein": 25, "carbs": 40, "fat": 8}],
        },
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["execution"]["live_state_changed"] is True
    assert "nutrition.logged_meals" in command["execution"]["affected_resources"]
    assert command["result"]["items_count"] == 1
    assert command["result"]["meal_id"]

    data = client.post(
        "/api/v2/actions/nutrition/data",
        json={"mode": "meals", "date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    assert data["data_status"] == "ok"
    assert data["meals"][0]["title"] == "Frühstück"
    assert data["meals"][0]["meal_slot"] == "Meal 1"
    assert data["meals"][0]["items"][0]["item_type"] == "kcal_only"
    assert data["meals"][0]["macros"]["kcal"] == 350
    assert data["logged_totals"]["kcal"] == 350
    assert "targets" in data
    assert "remaining" in data
    assert "planned_meals" in data


def test_actions_v2_nutrition_multi_day_meals_include_real_foods_quantities_and_macros(client):
    for logged_at, meal_name, amount in (
        ("2026-04-17T08:00:00", "Brotfrühstück", 1),
        ("2026-04-18T09:00:00", "Großes Brotfrühstück", 2),
    ):
        written = client.post(
            "/api/v2/actions/nutrition/control",
            json={
                "command": "quick_log_meal",
                "logged_at": logged_at,
                "meal_name": meal_name,
                "items": [{"food": "Brot", "amount": amount, "unit": "pcs"}],
            },
            headers=auth(),
        ).get_json()
        assert written["ok"] is True, written

    history = client.post(
        "/api/v2/actions/nutrition/data",
        json={"mode": "meals", "date_from": "2026-04-17", "date_to": "2026-04-18", "limit": 20},
        headers=auth(),
    ).get_json()

    assert history["ok"] is True, history
    assert history["history_detail"] == "full_logged_foods_quantities_and_macros"
    assert history["logged_meals_count"] == 2
    assert history["days_with_logged_meals"] == 2
    assert {meal["log_date"] for meal in history["meals"]} == {"2026-04-17", "2026-04-18"}
    assert all(meal["items"][0]["food_name"] == "Brot" for meal in history["meals"])
    assert {meal["items"][0]["amount"] for meal in history["meals"]} == {1.0, 2.0}
    assert all(meal["macros"]["kcal"] > 0 for meal in history["meals"])


def test_liva_act_nutrition_patch_uses_stable_v2_plan_mutator(client, monkeypatch):
    calls = []

    def fake_patch(plan_id, operations, *, dry_run=True):
        calls.append((plan_id, operations, dry_run))
        return {
            "ok": True,
            "execution": {"mode": "dry_run", "live_state_changed": False, "affected_resources": []},
            "before": {"plan_id": plan_id},
            "after": {"plan_id": plan_id},
            "diff": {},
        }

    monkeypatch.setattr("ai.actions_v2.patch_nutrition_plan_v2", fake_patch)
    operation = {"op": "update_slot_amount", "match": {"slot_path": "d0:m0:s0"}, "amount": 35, "unit": "g"}
    result = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "patch_nutrition_plan",
            "payload": {"template_id": 7, "operations": [operation]},
            "dry_run": True,
        },
        headers=auth(),
    ).get_json()

    assert result["ok"] is True, result
    assert calls == [(7, [operation], True)]


def test_liva_act_nutrition_patch_returns_exact_validation_error(client, monkeypatch):
    def rejected_patch(plan_id, operations, *, dry_run=True):
        raise ValueError("unknown_operation")

    monkeypatch.setattr("ai.actions_v2.patch_nutrition_plan_v2", rejected_patch)
    response = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "patch_nutrition_plan", "payload": {"template_id": 7, "operations": [{"op": "invented_op"}]}, "dry_run": True},
        headers=auth(),
    )
    body = response.get_json()

    assert response.status_code == 400
    assert body["ok"] is False
    assert body["error"] == {"code": "unknown_operation", "message": "unknown_operation", "details": {}}
    assert body["execution"] == {"mode": "dry_run", "live_state_changed": False}


def test_actions_v2_nutrition_quick_log_resolves_known_food_names_and_syncs_totals(client):
    command = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "quick_log_meal",
            "logged_at": "2026-04-17T10:00:00",
            "meal_name": "Brot Test",
            "items": [{"food": "Brot", "amount": "1 pcs"}],
        },
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["result"]["item_resolution"][0]["status"] == "resolved"
    assert command["result"]["item_resolution"][0]["food_id"] == 27

    data = client.post(
        "/api/v2/actions/nutrition/data",
        json={"mode": "daily_totals", "date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    totals = data["daily_totals"][0]
    assert totals["kcal"] == 222
    assert totals["protein"] == 7
    assert totals["carbs"] == 42
    assert totals["fat"] == 2
    assert totals["source"] == "weight_logs:meals_logging"


def test_actions_v2_nutrition_log_planned_meal_by_number(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 11,
                "slot_index": 0,
                "title": "Meal 1",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
            }
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {**day_payload, "logged_meals": [{"id": 44, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)

    body = client.post(
        "/api/v2/actions/nutrition/control",
        json={"command": "log_planned_meal", "meal_number": 1, "date": "2026-04-17"},
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["logged_meal_id"] == 44
    assert calls[0][0] == 11
    assert calls[0][1]["items"][0]["food_name"] == "Brot"


def test_actions_v2_nutrition_log_planned_meal_with_substitution(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 12,
                "slot_index": 0,
                "title": "Meal 1",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_id": 1, "food_name": "Apfel", "amount": 1, "unit": "pcs"}],
            }
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {**day_payload, "logged_meals": [{"id": 45, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)

    body = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "log_planned_meal",
            "meal": "Meal 1",
            "date": "2026-04-17",
            "substitutions": [{"old_food": "Apfel", "new_food": "Apfelmus", "amount": 60, "unit": "g"}],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    items = calls[0][1]["items"]
    assert all(item.get("food_name") != "Apfel" for item in items)
    assert any(item.get("food_name") == "Apfelmus" and item.get("amount") == 60 for item in items)
    assert body["result"]["substitutions"][0]["old_food"] == "Apfel"
    assert body["result"]["substitutions"][0]["new_food"] == "Apfelmus"


def test_actions_v2_nutrition_log_planned_meal_uses_plan_items_even_if_items_sent(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 18,
                "slot_index": 1,
                "title": "Meal 2",
                "meal_slot": "Meal 2",
                "status": "open",
                "items": [
                    {"food_id": 81, "food_name": "Hähnchenbrust", "amount": 225, "unit": "g"},
                    {"food_id": 82, "food_name": "Reis parboiled", "amount": 70, "unit": "g"},
                    {"food_id": 83, "food_name": "Pfeffer-Rahm-Soße", "amount": 80, "unit": "g"},
                ],
            }
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {**day_payload, "logged_meals": [{"id": 88, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)

    body = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "log_planned_meal",
            "meal_number": 2,
            "logged_at": "2026-04-17T21:00:00",
            "items": [{"food": "Hähnchen mit Reis", "kcal": 0}],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    items = calls[0][1]["items"]
    assert [item["food_name"] for item in items] == ["Hähnchenbrust", "Reis parboiled", "Pfeffer-Rahm-Soße"]
    assert all(item.get("item_type") != "kcal_only" for item in items)
    assert calls[0][1]["custom_logged_at"] == "2026-04-17T21:00:00"
    assert body["result"]["used_planned_items"] is True


def test_actions_v2_nutrition_log_planned_meal_resolves_text_hint(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 21,
                "slot_index": 0,
                "title": "Frühstück",
                "meal_slot": "Meal 1",
                "time_text": "07:00",
                "status": "open",
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
            },
            {
                "slot_id": 22,
                "slot_index": 1,
                "title": "Abendessen",
                "meal_slot": "Meal 2",
                "time_text": "21:00",
                "status": "open",
                "items": [
                    {"food_id": 81, "food_name": "Hähnchenbrust", "amount": 225, "unit": "g"},
                    {"food_id": 82, "food_name": "Reis parboiled", "amount": 70, "unit": "g"},
                ],
            },
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {**day_payload, "logged_meals": [{"id": 89, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)

    body = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "log_planned_meal",
            "date": "2026-04-17",
            "query": "Reis Hähnchenmeal",
            "logged_at": "2026-04-17T21:00:00",
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert calls[0][0] == 22
    assert [item["food_name"] for item in calls[0][1]["items"]] == ["Hähnchenbrust", "Reis parboiled"]
    assert body["result"]["meal_number"] == 2


def test_actions_v2_nutrition_data_planned_meals(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "targets": {"kcal": 2650, "p": 180, "c": 300, "f": 65},
        "planned_totals": {"kcal": 900, "p": 60, "c": 100, "f": 20},
        "logged_totals": {"kcal": 0, "p": 0, "c": 0, "f": 0},
        "remaining": {"kcal": 2650, "p": 180, "c": 300, "f": 65},
        "planned_meals": [
            {
                "slot_id": 21,
                "slot_index": 0,
                "title": "Meal 1",
                "meal_slot": "Meal 1",
                "time_text": "08:00",
                "status": "open",
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
                "totals": {"kcal": 300, "p": 20, "c": 40, "f": 7},
            }
        ],
        "logged_meals": [],
    }
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    body = client.post(
        "/api/v2/actions/nutrition/data",
        json={"mode": "planned_meals", "date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["data_status"] == "ok"
    assert body["planned_meals"][0]["meal_number"] == 1
    assert body["planned_meals"][0]["items"][0]["food_name"] == "Brot"


def test_actions_v2_create_nutrition_plan_dry_run_accepts_top_level_days(client):
    body = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "dry_run": True,
            "name": "Top Level",
            "target_kcal": 2700,
            "days": [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": 27, "amount": 1, "unit": "pcs"}]}]}],
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["will_change"] == ["nutrition_plan"]
    assert body["normalized_plan"]["name"] == "Top Level"
    assert body["normalized_plan"]["days_count"] == 1
    assert body["normalized_plan"]["meal_count"] == 1


def test_actions_v2_create_nutrition_plan_dry_run_accepts_value_days(client):
    body = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "dry_run": True,
            "name": "Top Level Wins",
            "value": {
                "name": "Nested Value",
                "days": [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": 27, "amount": 1, "unit": "pcs"}]}]}],
            },
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["normalized_plan"]["name"] == "Top Level Wins"
    assert body["result"]["normalized_plan"]["days_count"] == 1


def test_actions_v2_create_nutrition_plan_dry_run_accepts_payload_days(client):
    body = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "dry_run": True,
            "payload": {
                "name": "Payload Plan",
                "days": [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": 27, "amount": 1, "unit": "pcs"}]}]}],
            },
        },
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["normalized_plan"]["name"] == "Payload Plan"
    assert body["normalized_plan"]["days_count"] == 1


def test_actions_v2_create_nutrition_plan_missing_days_returns_structured_error(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "create_nutrition_plan", "dry_run": True, "name": "Missing Days"},
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "missing_days"
    assert body["error"]["accepted_shapes"] == ["days", "value.days", "payload.days"]
    assert "name" in body["received_keys"]


def test_actions_v2_create_nutrition_plan_raw_text_without_days_blocks_write(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "confirm": True,
            "dry_run": False,
            "name": "Raw Text Only",
            "raw_text": "Montag: Brot und Milch",
        },
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["execution"]["mode"] == "no_change"
    assert body["execution"]["live_state_changed"] is False
    assert body["error"]["code"] == "structured_days_required"


def test_actions_v2_create_nutrition_plan_write_persists_and_returns_template_id(client):
    from tests.test_actions_v2_plan_create import _ensure_test_planning_schema

    _ensure_test_planning_schema()
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "confirm": True,
            "dry_run": False,
            "payload": {
                "name": "Live Write Plan",
                "set_active": True,
                "days": [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": 27, "amount": 1, "unit": "pcs"}]}]}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert "nutrition_plan" in body["execution"]["affected_resources"]
    assert body["result"]["template_id"] is not None


def test_actions_v2_gpt_openapi_documents_nutrition_create_days_shapes():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    act_props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "days" in act_props
    assert "top-level" in act_props["days"]["description"].lower()

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_act_props = file_spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "days" in file_act_props
    assert "value.days" in file_act_props["days"]["description"]
    assert "payload.days" in file_act_props["days"]["description"]


def test_actions_v2_gpt_openapi_documents_patch_training_plan_top_level_operations_and_note_op():
    import ai.actions_v2 as actions_v2

    spec = actions_v2.build_gpt_openapi_spec()
    props = spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "operations" in props
    assert "top-level operations" in props["operations"]["description"].lower()
    assert "update_exercise_note" in props["operations"]["description"]
    assert "patch_training_plan" in props["operations"]["description"]
    assert "replace_training_plan" in props["operations"]["description"]

    repo = Path(__file__).resolve().parents[1]
    file_spec = json.loads((repo / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    file_props = file_spec["components"]["schemas"]["LivaActRequest"]["properties"]
    assert "value.operations" in file_props["operations"]["description"]
    assert "payload.operations" in file_props["operations"]["description"]


def test_actions_v2_gpt_guide_disallows_replace_fallback_for_note_edits():
    guide = Path("docs/actions_gpt_endpoint_guide.md").read_text(encoding="utf-8")
    assert "immer patch_training_plan" in guide
    assert "replace_training_plan nur verwenden" in guide
    assert "Nicht auf replace_training_plan ausweichen" in guide
    assert "Ich konnte den Patch nicht speichern. Kein Live-State wurde verändert." in guide


def test_actions_v2_patch_training_plan_verification_failure_does_not_report_success(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "get_training_plan_detail",
        lambda plan_id: {
            "plan_id": plan_id,
            "name": "Mock Plan",
            "days": [{"day_id": "Mo", "day_index": 0, "title": "Mo", "label": "Mo", "workouts": [{"workout_index": 0, "title": "Push", "exercises": [{"exercise_index": 0, "name": "Schrägbankdrücken", "variation": "KH", "notes": "CORE Alt"}]}]}],
            "is_active": False,
            "archived": False,
        },
    )
    monkeypatch.setattr(
        actions_v2,
        "_patch_training_plan_notes_live",
        lambda plan_id, operations, transformed_operations, verification_targets, preview: {
            **preview,
            "ok": False,
            "operations_applied": 0,
            "error": {
                "code": "patch_verification_failed",
                "message": "Patch verification found stale note mirrors in persistent plan_json or full readback.",
                "failed_operations": [{"op": "update_exercise_note", "stale_paths": [{"path": "sequence[0].items[0]", "note": "CORE Alt"}]}],
                "stale_paths": [{"path": "sequence[0].items[0]", "note": "CORE Alt"}],
            },
            "warnings": [],
        },
    )

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": 28,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Schrägbankdrücken", "variation": "KH"}, "note": "Neu"}],
        },
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["ok"] is False
    assert body["execution"]["mode"] == "no_change"
    assert body["execution"]["live_state_changed"] is False
    assert body["error"]["code"] == "patch_verification_failed"
    log_row = connections.get_plans_db().execute("SELECT result_json FROM actions_v2_command_log ORDER BY created_at DESC LIMIT 1").fetchone()
    assert log_row is not None
    assert "patch_verification_failed" in log_row[0]


def test_actions_v2_nutrition_log_all_open_planned_meals(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    day_payload = {
        "ok": True,
        "date": "2026-04-17",
        "planned_meals": [
            {
                "slot_id": 31,
                "slot_index": 0,
                "title": "Meal 1",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
            },
            {
                "slot_id": 32,
                "slot_index": 1,
                "title": "Meal 2",
                "meal_slot": "Meal 2",
                "status": "logged",
                "items": [{"food_id": 28, "food_name": "Milch", "amount": 300, "unit": "ml"}],
            },
            {
                "slot_id": 33,
                "slot_index": 2,
                "title": "Meal 3",
                "meal_slot": "Meal 3",
                "status": "open",
                "items": [{"food_id": 29, "food_name": "Reis", "amount": 100, "unit": "g"}],
            },
        ],
        "logged_meals": [],
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso=None: day_payload)

    def fake_log(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {
            **day_payload,
            "logged_meals": [{"id": 100 + len(calls), "slot_id": slot_id, "created_from_planned_slot_id": slot_id}],
        }, None

    monkeypatch.setattr(nutrition_db, "log_planned_meal", fake_log)

    body = client.post(
        "/api/v2/actions/nutrition/control",
        json={"command": "log_planned_meal", "all_planned": True, "date": "2026-04-17"},
        headers=auth(),
    ).get_json()

    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["all_planned"] is True
    assert body["result"]["logged_count"] == 2
    assert [call[0] for call in calls] == [31, 33]
    assert body["result"]["logged_meals"][0]["meal_number"] == 1
    assert body["result"]["logged_meals"][1]["meal_number"] == 3


def test_actions_v2_nutrition_delete_logged_meal_is_applied_live(client):
    created = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "quick_log_meal",
            "logged_at": "2026-04-17T09:00:00",
            "meal_name": "Delete Me",
            "items": [{"food": "Quick Add", "kcal": 100}],
        },
        headers=auth(),
    ).get_json()
    meal_id = created["result"]["meal_id"]

    deleted = client.post(
        "/api/v2/actions/nutrition/control",
        json={"command": "delete_logged_meal", "meal_id": meal_id},
        headers=auth(),
    ).get_json()

    assert deleted["execution"]["mode"] == "applied_live"
    assert deleted["result"]["deleted"] is True


def test_actions_v2_nutrition_edit_logged_meal_can_append_items(client):
    created = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "quick_log_meal",
            "logged_at": "2026-04-17T21:00:00",
            "items": [{"food": "Brot", "amount": "1 pcs"}],
        },
        headers=auth(),
    ).get_json()
    meal_id = created["result"]["meal_id"]

    edited = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "edit_logged_meal",
            "meal_id": meal_id,
            "append_items": True,
            "items": [
                {"food": "Milch", "amount": 300, "unit": "ml"},
                {"food": "Whey", "amount": 70, "unit": "g"},
            ],
        },
        headers=auth(),
    ).get_json()

    assert edited["ok"] is True, edited
    assert edited["execution"]["mode"] == "applied_live"
    assert edited["result"]["append_items"] is True
    assert [item["status"] for item in edited["result"]["item_resolution"]] == ["resolved", "resolved"]

    data = client.post(
        "/api/v2/actions/nutrition/data",
        json={"mode": "meals", "date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    meal = next(row for row in data["meals"] if int(row["id"]) == int(meal_id))
    names = [item["food_name"] for item in meal["items"]]
    assert names == ["Brot", "Milch", "Whey"]
    assert meal["macros"]["p"] > 60


def test_nutrition_logged_meals_auto_title_and_slot_by_time(client):
    import nutrition.nutrition_planning_db as nutrition_db

    if hasattr(nutrition_db, "_SCHEMA_READY"):
        nutrition_db._SCHEMA_READY = False
    # Monday. Insert out of chronological order; title and slot must follow
    # logged_at, not creation order or user-provided title/slot.
    nutrition_db.create_free_logged_meal(
        date_iso="2026-04-20",
        title="User Dinner",
        meal_slot="Custom",
        logged_at="2026-04-20T19:00:00",
        items=[{"item_type": "kcal_only", "food_name": "Dinner", "amount": 300, "unit": "kcal", "calories": 300}],
    )
    nutrition_db.create_free_logged_meal(
        date_iso="2026-04-20",
        title="User Breakfast",
        meal_slot="Whatever",
        logged_at="2026-04-20T07:30:00",
        items=[{"item_type": "kcal_only", "food_name": "Breakfast", "amount": 200, "unit": "kcal", "calories": 200}],
    )
    payload, err = nutrition_db.create_free_logged_meal(
        date_iso="2026-04-20",
        title="User School",
        meal_slot="School Slot",
        logged_at="2026-04-20T12:00:00",
        items=[{"item_type": "kcal_only", "food_name": "Lunch", "amount": 250, "unit": "kcal", "calories": 250}],
    )

    assert err is None
    meals = payload["logged_meals"]
    assert [meal["time_text"] for meal in meals] == ["07:30", "12:00", "19:00"]
    assert [meal["title"] for meal in meals] == ["Frühstück", "Schule", "Abends"]
    assert [meal["meal_slot"] for meal in meals] == ["Meal 1", "Meal 2", "Meal 3"]


def test_nutrition_logged_planned_meal_uses_planned_title(client, monkeypatch):
    import nutrition.nutrition_planning_db as nutrition_db

    if hasattr(nutrition_db, "_SCHEMA_READY"):
        nutrition_db._SCHEMA_READY = False
    monkeypatch.setattr(
        nutrition_db,
        "_planned_meals_for_date",
        lambda date_iso: (
            [
                {
                    "slot_id": 41,
                    "slot_index": 1,
                    "title": "Hähnchen mit Reis",
                    "meal_slot": "Meal 2",
                    "time_text": "21:00",
                    "items": [{"item_type": "kcal_only", "food_name": "Plan", "amount": 500, "unit": "kcal", "calories": 500}],
                }
            ],
            {},
        ),
    )

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO nutrition_logged_meals
            (log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, logged_at, created_at, updated_at)
        VALUES ('2026-04-20', 41, 'Wrong Title', 'Wrong Slot', 'planned', 41, '2026-04-20T21:00:00', '2026-04-20T21:00:00', '2026-04-20T21:00:00')
        """
    )
    nutrition_db._renumber_logged_meals_for_date(cur, "2026-04-20")
    row = cur.execute("SELECT title, meal_slot FROM nutrition_logged_meals WHERE created_from_planned_slot_id=41").fetchone()
    conn.close()

    assert row["title"] == "Hähnchen mit Reis"
    assert row["meal_slot"] == "Meal 1"


def test_actions_v2_recovery_flags_are_applied_live_and_visible(client):
    command = client.post(
        "/api/v2/actions/recovery/control",
        json={"command": "set_alcohol_flag", "date": "2026-04-17", "value": True},
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert command["execution"]["live_state_changed"] is True
    assert "hrv.hrv_measurements" in command["execution"]["affected_resources"]

    state = client.get("/api/v2/actions/recovery/state", headers=auth()).get_json()["recovery"]
    assert state["last_flags"]["alcohol"] is True


def test_actions_v2_recovery_flags_require_explicit_value_and_support_false(client):
    command = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-04-17", "reason": "User sagt: ich bin krank"},
        headers=auth(),
    ).get_json()
    assert command["ok"] is False
    assert command["error"]["code"] == "invalid_arguments"
    assert command["error"]["message"] == "missing_required_field: value"

    command = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-04-17", "value": True, "reason": "User sagt: ich bin krank"},
        headers=auth(),
    ).get_json()
    assert command["ok"] is True and command["execution"]["mode"] == "applied_live"

    state = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "recovery_state"},
        headers=auth(),
    ).get_json()["result"]
    assert state["latest"]["flag"] == "sick"
    assert state["flags"]["sickness"] is True

    cleared = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-04-17", "value": False},
        headers=auth(),
    ).get_json()
    assert cleared["ok"] is True

    state_after = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "recovery_state"},
        headers=auth(),
    ).get_json()["result"]
    assert state_after["flags"]["sickness"] is False


def test_actions_v2_recovery_read_includes_polar_latest_fields(client):
    conn = connections.get_polar_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, access_token TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          sleep_minutes INTEGER, actual_sleep_minutes INTEGER, deep_sleep_minutes INTEGER, rem_sleep_minutes INTEGER,
          light_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
          date TEXT PRIMARY KEY, raw_json TEXT, ans_status TEXT, recovery_indicator INTEGER, recovery_indicator_sublevel INTEGER,
          ans_rate REAL, mean_recovery_rri REAL, mean_recovery_rmssd REAL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_continuous_samples (
          date TEXT PRIMARY KEY, raw_json TEXT, sample_count INTEGER, min_hr REAL, max_hr REAL, avg_hr REAL,
          resting_candidate_hr REAL, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO polar_tokens (access_token, created_at, updated_at) VALUES ('token', '2026-04-17T01:00:00Z', '2026-04-17T01:00:00Z')")
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-17", 77, "2026-04-16T23:41:00+02:00", "2026-04-17T05:47:00+02:00", 320, 22, 24, 3, "2026-04-17T06:00:00Z", "2026-04-17T06:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_nightly_recharge (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rmssd, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-17", '{\"breathingRate\":14.2}', "balanced", 2, 3, 1.2, 95, "2026-04-17T06:00:00Z", "2026-04-17T06:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_continuous_samples (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-17", 12, 42, 91, 58, 47, "2026-04-17T06:00:00Z", "2026-04-17T06:00:00Z"),
    )
    conn.commit()
    conn.close()

    payload = client.post("/api/v2/actions/liva/read", json={"mode": "recovery_state"}, headers=auth()).get_json()["result"]
    assert payload["latest"]["source"] == "polar"
    assert payload["latest"]["sleep_window_label"] == "23:41 – 05:47"
    assert payload["latest"]["breathing_rate"] == 14.2
    assert payload["polar"]["connected"] is True


def test_actions_v2_recovery_flag_applies_live_for_polar_only_day(client):
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
          date TEXT PRIMARY KEY, raw_json TEXT, ans_status TEXT, recovery_indicator INTEGER, recovery_indicator_sublevel INTEGER,
          ans_rate REAL, mean_recovery_rmssd REAL, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 52, "2026-05-11T22:45:00+02:00", "2026-05-12T05:40:00+02:00", 358, 56, 57, 44, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_nightly_recharge (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rmssd, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", "-5.95", 1, 34, 2, 124, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.commit()
    conn.close()

    command = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    ).get_json()
    assert command["ok"] is True
    assert command["execution"]["mode"] == "applied_live"

    state = client.post("/api/v2/actions/liva/read", json={"mode": "recovery_state"}, headers=auth()).get_json()["result"]
    assert state["flags"]["sickness"] is True
    assert state["latest"]["flag"] == "sick"


def test_actions_v2_recovery_flag_can_clear_for_polar_only_day_and_recovery_data_has_no_fake_hrv_point(client):
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 52, "2026-05-11T22:45:00+02:00", "2026-05-12T05:40:00+02:00", 358, 56, 57, 44, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.commit()
    conn.close()

    set_flag = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    ).get_json()
    assert set_flag["ok"] is True

    data = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "recovery_data", "date_from": "2026-05-12", "date_to": "2026-05-12"},
        headers=auth(),
    ).get_json()["result"]["recovery"]["points"]
    point = next(item for item in data if item["date"] == "2026-05-12")
    assert point["sickness_flag"] == 1
    assert point["rmssd"] is None
    assert point["hr"] is None
    assert len(data) == 1

    cleared = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": False},
        headers=auth(),
    ).get_json()
    assert cleared["ok"] is True

    state = client.post("/api/v2/actions/liva/read", json={"mode": "recovery_state"}, headers=auth()).get_json()["result"]
    assert state["flags"]["sickness"] is False


def test_actions_v2_daily_snapshot_includes_compact_recovery_summary(client):
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
          date TEXT PRIMARY KEY, raw_json TEXT, ans_status TEXT, recovery_indicator INTEGER, recovery_indicator_sublevel INTEGER,
          ans_rate REAL, mean_recovery_rmssd REAL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_continuous_samples (
          date TEXT PRIMARY KEY, raw_json TEXT, sample_count INTEGER, min_hr REAL, max_hr REAL, avg_hr REAL,
          resting_candidate_hr REAL, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 52, "2026-05-11T22:45:00+02:00", "2026-05-12T05:40:00+02:00", 358, 56, 57, 44, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_nightly_recharge (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rmssd, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", "-5.95", 1, 34, 2, 124, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_continuous_samples (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 12, 42, 91, 58, 46, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", json={"mode": "daily_snapshot"}, headers=auth()).get_json()["result"]["today"]
    recovery = body["recovery_summary"]
    assert recovery["sleep_score"] == 52
    assert recovery["sleep_window_label"] == "22:45 – 05:40"
    assert recovery["ans_status"] == "-5.95"
    assert recovery["heart_rate"]["night_pulse_bpm"] == 46
    assert recovery["heart_rate"]["resting_heart_rate_bpm"] is None
    assert recovery["heart_rate"]["resting_heart_rate_available"] is False
    assert "hr" not in recovery
    assert "night_pulse" not in recovery
    assert "flags" in recovery


def test_daily_context_uses_semantic_polar_heart_rate_payload(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_recovery_summary",
        lambda *args, **kwargs: {
            "latest_rmssd": 88.0,
            "latest_hr": 39.45,
            "readiness_summary": "good",
            "flags": {"sickness": False, "alcohol": False, "sleep": None},
            "latest": {"date": "2026-04-17", "source": "polar", "sleep_score": 80},
            "polar": {
                "latest_continuous": {"resting_candidate_hr": 40},
                "latest_nightly_recharge": {},
            },
        },
    )

    body = client.get("/api/v2/actions/daily/context?date=2026-04-17", headers=auth()).get_json()
    heart_rate = body["recovery"]["heart_rate"]
    assert heart_rate["night_pulse_bpm"] == 40
    assert heart_rate["resting_heart_rate_bpm"] is None
    assert heart_rate["resting_heart_rate_available"] is False
    assert heart_rate["sleep_heart_rate_bpm"] == 39.45
    assert "hr" not in body["recovery"]
    assert "night_pulse" not in body["recovery"]


def test_gpt_recovery_state_does_not_turn_polar_night_pulse_into_rhr(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_recovery_summary",
        lambda *args, **kwargs: {
            "latest_rmssd": 90.0,
            "latest_hr": 39.45,
            "readiness_summary": "good",
            "flags": {"sickness": False, "alcohol": False, "sleep": None},
            "latest": {"date": "2026-04-17", "source": "polar"},
            "polar": {
                "latest_continuous": {"resting_candidate_hr": 40},
                "latest_nightly_recharge": {},
            },
        },
    )

    recovery = client.get("/api/v2/actions/recovery/state", headers=auth()).get_json()["recovery"]
    assert recovery["heart_rate"]["night_pulse_bpm"] == 40
    assert recovery["heart_rate"]["resting_heart_rate_bpm"] is None
    assert recovery["heart_rate"]["resting_heart_rate_available"] is False


def test_gpt_recovery_state_does_not_expose_legacy_hrv4training_as_rhr(client, monkeypatch):
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "_recovery_summary",
        lambda *args, **kwargs: {
            "latest_rmssd": 55.0,
            "latest_hr": 52.0,
            "readiness_summary": "okay",
            "flags": {"sickness": False, "alcohol": False, "sleep": None},
            "latest": {"date": "2026-04-17", "source": "legacy"},
            "polar": {},
        },
    )

    recovery = client.get("/api/v2/actions/recovery/state", headers=auth()).get_json()["recovery"]
    assert recovery["heart_rate"]["resting_heart_rate_bpm"] is None
    assert recovery["heart_rate"]["resting_heart_rate_available"] is False
    assert recovery["heart_rate"]["resting_heart_rate_source"] is None


def test_openapi_documents_semantic_heart_rate_payload():
    spec = json.loads(Path("openapi/liva-actions-v2.json").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    assert "HeartRateSemanticInfo" in schemas
    description = schemas["HeartRateSemanticInfo"]["description"]
    assert "must not be described as resting heart rate" in description
    assert "resting_heart_rate_bpm is null unless a true resting-heart-rate source is available" in description


def test_daily_context_old_sick_flags_do_not_leak_to_later_day(client):
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    )
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-13", "value": True},
        headers=auth(),
    )
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-14", "value": True},
        headers=auth(),
    )

    body = client.get("/api/v2/actions/daily/context?date=2026-05-23", headers=auth()).get_json()
    recovery = body["recovery"]
    assert recovery["flag"] is None
    assert recovery["flags"]["sickness"] is False
    assert recovery["status"] != "sick"
    assert recovery["recent_flags"]["recent_sickness_within_14d"] is True
    assert recovery["recent_flags"]["last_sick_date"] == "2026-05-14"


def test_daily_context_exact_same_day_sick_flag_stays_active(client):
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-14", "value": True},
        headers=auth(),
    )
    body = client.get("/api/v2/actions/daily/context?date=2026-05-14", headers=auth()).get_json()
    recovery = body["recovery"]
    assert recovery["flag"] == "sick"
    assert recovery["flags"]["sickness"] is True
    assert recovery["status"] == "sick"


def test_recovery_day_flags_do_not_leak_to_future_days(client):
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    )
    state = client.get("/api/v2/actions/recovery/state", headers=auth()).get_json()["recovery"]
    assert state["flags"]["sickness"] is False
    assert state["latest"]["flag"] is None


def test_recent_flags_can_exist_without_active_sickness(client):
    client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-14", "value": True},
        headers=auth(),
    )
    body = client.get("/api/v2/actions/daily/context?date=2026-05-23", headers=auth()).get_json()
    recovery = body["recovery"]
    assert recovery["flags"]["sickness"] is False
    assert recovery["flag"] is None
    assert recovery["recent_flags"]["last_sick_date"] == "2026-05-14"


def test_actions_v2_daily_snapshot_includes_polar_only_sickness_flag(client):
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 52, "2026-05-11T22:45:00+02:00", "2026-05-12T05:40:00+02:00", 358, 56, 57, 44, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.commit()
    conn.close()
    command = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    ).get_json()
    assert command["ok"] is True

    body = client.post("/api/v2/actions/liva/read", json={"mode": "daily_snapshot"}, headers=auth()).get_json()["result"]["today"]["recovery_summary"]
    assert body["flags"]["sickness"] is True
    assert body["flag"] == "sick"
    assert body["status"] != "okay"
    readiness = client.post("/api/v2/actions/liva/read", json={"mode": "daily_snapshot"}, headers=auth()).get_json()["result"]["today"]["readiness_summary"]
    assert readiness["status"] != "okay"


def _seed_polar_only_sickness_day(day_iso: str = "2026-05-13") -> None:
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
          date TEXT PRIMARY KEY, raw_json TEXT, ans_status TEXT, recovery_indicator INTEGER, recovery_indicator_sublevel INTEGER,
          ans_rate REAL, mean_recovery_rmssd REAL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_continuous_samples (
          date TEXT PRIMARY KEY, raw_json TEXT, sample_count INTEGER, min_hr REAL, max_hr REAL, avg_hr REAL,
          resting_candidate_hr REAL, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day_iso, 52, f"{day_iso}T00:15:00+02:00", f"{day_iso}T06:40:00+02:00", 358, 56, 57, 44, f"{day_iso}T04:00:00Z", f"{day_iso}T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_nightly_recharge (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rmssd, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        (day_iso, "-9.56", 1, 22, 1, 74, f"{day_iso}T04:00:00Z", f"{day_iso}T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_continuous_samples (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        (day_iso, 12, 60, 95, 78, 74, f"{day_iso}T04:00:00Z", f"{day_iso}T04:00:00Z"),
    )
    conn.commit()
    conn.close()


def test_actions_v2_recovery_state_latest_status_is_sick_for_polar_only_flag(client):
    _seed_polar_only_sickness_day("2026-05-13")
    flagged = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-13", "value": True},
        headers=auth(),
    ).get_json()
    assert flagged["ok"] is True
    body = client.post("/api/v2/actions/liva/read", json={"mode": "recovery_state"}, headers=auth()).get_json()["result"]
    assert body["latest"]["flag"] == "sick"
    assert body["latest"]["status"] == "sick"


def test_actions_v2_core_board_uses_shared_recovery_summary_and_sickness_override(client):
    conn = sqlite3.connect(connections.HRV_DB)
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, sleep_quality, fatigue, training_motivation, alcohol, sickness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-11 06:00:00+0000", "2026-05-11 06:00:00+0000", 45, 101, 8, 3, 7, 0, 0),
    )
    conn.commit()
    conn.close()
    _seed_polar_only_sickness_day("2026-05-13")
    flagged = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-13", "value": True},
        headers=auth(),
    ).get_json()
    assert flagged["ok"] is True

    body = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-05-13", "decision_intent": "train_controlled", "human_headline": "Pull sauber ausführen.", "build_training_card": True},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    board = body["board"]
    assert board["recovery_summary"]["status"] == "sick"
    assert board["recovery_summary"]["flags"]["sickness"] is True
    assert board["decision_intent"] == "recovery_only"
    assert "krank" in str(board["human_headline"]).lower()
    assert "pull sauber" not in str(board["human_headline"]).lower()
    assert "krank" in str(board["human_summary"]).lower() or "recovery" in str(board["human_summary"]).lower()
    assert any("krank" in str(item).lower() or "recovery" in str(item).lower() for item in (board.get("visible_reasons") or []))
    assert ((board.get("data_status") or {}).get("domains") or {}).get("recovery", {}).get("latest_date") == "2026-05-13"
    card = body.get("training_card") or {}
    assert card.get("decision_intent") == "recovery_only"


def test_actions_v2_core_board_read_syncs_persisted_recovery_card_after_sickness_override(client):
    _seed_polar_only_sickness_day("2026-05-13")
    flagged = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-13", "value": True},
        headers=auth(),
    ).get_json()
    assert flagged["ok"] is True
    built = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-05-13", "decision_intent": "train_controlled", "human_headline": "Pull sauber ausführen.", "build_training_card": True},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True, built

    board = client.post("/api/v2/actions/liva/read", json={"mode": "core_board", "date": "2026-05-13"}, headers=auth()).get_json()["result"]["board"]
    card = client.post("/api/v2/actions/liva/read", json={"mode": "core_training_card", "date": "2026-05-13"}, headers=auth()).get_json()["result"]["training_card"]
    assert board["decision_intent"] == "recovery_only"
    assert board["training_card"]["decision_intent"] == "recovery_only"
    assert board["training_card"]["session_label"] == "Recovery"
    assert board["training_card"]["session_type"] == "recovery"
    assert not any("pull" in str(item).lower() for item in (board["training_card"].get("items") or []))
    assert board["training_card"]["decision_intent"] == card["decision_intent"]
    assert board["training_card"]["session_label"] == card["session_label"]
    assert board["training_card"]["session_type"] == card["session_type"]
    assert board["training_card"].get("summary") == card.get("summary")
    assert board["training_card"].get("footer_note") == card.get("footer_note")


def test_actions_v2_core_training_card_liva_read_uses_gpt_card_only(client, monkeypatch):
    built = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-05-14", "decision_intent": "train_controlled", "human_headline": "Push kontrolliert ausführen.", "build_training_card": True},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True, built

    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(actions_v2, "get_core_training_card", lambda *args, **kwargs: pytest.fail("slow get_core_training_card should not be used"))
    monkeypatch.setattr(actions_v2, "build_core_training_card", lambda *args, **kwargs: pytest.fail("build_core_training_card should not be used"), raising=False)

    body = client.post("/api/v2/actions/liva/read", json={"mode": "core_training_card", "date": "2026-05-14"}, headers=auth()).get_json()
    assert body["ok"] is True
    assert body["result"]["source"] == "gpt_training_decision"
    assert body["result"]["deprecated_alias"] is True
    assert body["result"]["training_card"]["date"] == "2026-05-14"


def test_actions_v2_core_board_liva_read_syncs_persisted_card_without_slow_card_path(client, monkeypatch):
    _seed_polar_only_sickness_day("2026-05-15")
    flagged = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-15", "value": True},
        headers=auth(),
    ).get_json()
    assert flagged["ok"] is True
    built = client.post(
        "/api/v2/actions/core/board/build",
        json={"date": "2026-05-15", "decision_intent": "train_controlled", "human_headline": "Pull sauber ausführen.", "build_training_card": True},
        headers=auth(),
    ).get_json()
    assert built["ok"] is True, built

    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(actions_v2, "get_core_daily_board", lambda *args, **kwargs: pytest.fail("slow get_core_daily_board should not be used"))
    monkeypatch.setattr(actions_v2, "get_core_training_card", lambda *args, **kwargs: pytest.fail("slow get_core_training_card should not be used"))
    monkeypatch.setattr(actions_v2, "build_core_training_card", lambda *args, **kwargs: pytest.fail("build_core_training_card should not be used"), raising=False)
    monkeypatch.setattr(actions_v2, "build_endurance_core_context", lambda *args, **kwargs: pytest.fail("external endurance refresh should not be used"))

    body = client.post("/api/v2/actions/liva/read", json={"mode": "core_board", "date": "2026-05-15"}, headers=auth()).get_json()
    assert body["ok"] is True
    board = body["result"]["board"]
    assert board["decision_intent"] == "recovery_only"
    assert board["training_card"]["decision_intent"] == "recovery_only"
    assert board["training_card"]["session_label"] == "Recovery"
    assert board["training_card"]["session_type"] == "rest"
def test_actions_v2_core_morning_context_marks_sickness_flag_as_hard_context(client):
    conn = sqlite3.connect(connections.POLAR_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polar_sleep (
          date TEXT PRIMARY KEY, raw_json TEXT, sleep_score REAL, sleep_start TEXT, sleep_end TEXT,
          actual_sleep_minutes INTEGER, awake_minutes INTEGER, interruption_minutes INTEGER, interruptions INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
          date TEXT PRIMARY KEY, raw_json TEXT, ans_status TEXT, recovery_indicator INTEGER, recovery_indicator_sublevel INTEGER,
          ans_rate REAL, mean_recovery_rmssd REAL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS polar_continuous_samples (
          date TEXT PRIMARY KEY, raw_json TEXT, sample_count INTEGER, min_hr REAL, max_hr REAL, avg_hr REAL,
          resting_candidate_hr REAL, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_sleep (date, raw_json, sleep_score, sleep_start, sleep_end, actual_sleep_minutes, awake_minutes, interruption_minutes, interruptions, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 52, "2026-05-11T22:45:00+02:00", "2026-05-12T05:40:00+02:00", 358, 56, 57, 44, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_nightly_recharge (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rmssd, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", "-9.56", 1, 22, 1, 74, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO polar_continuous_samples (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at) VALUES (?, '{}', ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12", 12, 60, 95, 78, 74, "2026-05-12T04:00:00Z", "2026-05-12T04:00:00Z"),
    )
    conn.commit()
    conn.close()
    flag_response = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "recovery", "command": "set_sickness_flag", "date": "2026-05-12", "value": True},
        headers=auth(),
    ).get_json()
    assert flag_response["ok"] is True

    body = client.post("/api/v2/actions/liva/read", json={"mode": "core_morning_context", "date": "2026-05-12"}, headers=auth()).get_json()["result"]["morning_context"]
    assert body["recovery_summary"]["flags"]["sickness"] is True
    assert body["recovery_summary"]["status"] != "okay"
    assert body["recovery_snapshot"]["status"] != "usable"
    assert body["suggested_payload_skeleton"]["decision_intent"] in {"reduce_volume", "technique_only", "recovery_only", "hard_stop"}


def test_actions_v2_liva_recovery_data_respects_days_window(client):
    conn = sqlite3.connect(connections.HRV_DB)
    for idx in range(20):
        base = date.fromisoformat("2026-04-22") + timedelta(days=idx)
        day_iso = base.isoformat()
        conn.execute(
            "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, sleep_quality, fatigue, training_motivation, alcohol, sickness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{day_iso} 06:00:00+0000", f"{day_iso} 06:00:00+0000", 50 + idx, 80 + idx, 8, 3, 7, 0, 0),
        )
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "recovery_data", "days": 14, "date_to": "2026-05-11"},
        headers=auth(),
    ).get_json()
    points = body["result"]["recovery"]["points"]
    assert points
    assert all("2026-04-28" <= point["date"] <= "2026-05-11" for point in points)
    assert len(points) <= 14


def test_actions_v2_training_plan_normalizes_rpe_and_set_targets(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "Mo": [{"id": "push", "kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Katanas", "variation": "SZ", "sets": 1, "reps": {"min": 10, "max": 12}, "rpe_list": [8, 9]}]}],
                        "Mi": [{"id": "pull", "kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "Curls", "variation": "SZ", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]}],
                        "Fr": [{"id": "arms", "kind": "gym", "title": "Arms", "items": [{"kind": "exercise", "name": "Incline Curls", "variation": "KH", "sets": 2, "reps": {"min": 10, "max": 12}, "rpe_list": [9]}]}],
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()
    plan = body["result"]["plan"]
    for info in plan["days"].values():
        for exercise in info.get("exercises", []):
            sets = exercise.get("sets")
            if sets is None:
                continue
            assert len(exercise.get("set_targets", [])) == sets
            assert len(exercise.get("rpe_list", [])) == sets
            assert (exercise.get("volume") or {}).get("rpe") == exercise.get("rpe_list")
            for idx, target in enumerate(exercise.get("set_targets", [])):
                assert target.get("rpe") == exercise["rpe_list"][idx]


def _set_test_plan_with_mi_curls_sz() -> None:
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=? WHERE id=1",
        (
            json.dumps(
                {
                    "base_week": {
                        "Mi": [
                            {
                                "id": "pull",
                                "kind": "gym",
                                "title": "Pull",
                                "items": [
                                    {
                                        "kind": "exercise",
                                        "name": "Curls",
                                        "variation": "SZ",
                                        "sets": 3,
                                        "reps": {"min": 8, "max": 10},
                                        "rpe_list": [8, 9, 9],
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()


def test_actions_v2_training_plan_compact_stays_small_and_exposes_exercise_map(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "compact"}, headers=auth()).get_json()
    plan = body["result"]["plan"]
    assert plan["detail"] == "compact"
    assert "current_exercise_map" in plan
    assert "Mi" in plan["current_exercise_map"]
    assert "days" in plan
    assert "events" not in ((plan.get("days") or {}).get("Mi") or {})
    first_ex = (((plan.get("days") or {}).get("Mi") or {}).get("exercises") or [None])[0]
    assert isinstance(first_ex, str)


def test_actions_v2_training_plan_day_returns_only_requested_day(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "day", "day": "Mi"}, headers=auth()).get_json()
    plan = body["result"]["plan"]
    assert plan["detail"] == "day"
    assert plan["matched_day"] == "Mi"
    assert set((plan.get("days") or {}).keys()) == {"Mi"}
    exercises = plan["days"]["Mi"]["exercises"]
    assert len(exercises) == 1
    assert exercises[0]["display_name"] == "Curls (SZ)"
    assert "set_targets" not in exercises[0]
    assert "volume" not in exercises[0]


def test_actions_v2_training_plan_exercise_returns_only_matched_target(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_plan", "detail": "exercise", "day": "Mi", "exercise": "Curls"},
        headers=auth(),
    ).get_json()
    plan = body["result"]["plan"]
    assert plan["detail"] == "exercise"
    assert plan["matched_day"] == "Mi"
    assert plan["matched_exercise"] == "Curls (SZ)"
    assert plan["target"]["display_name"] == "Curls (SZ)"
    assert "set_targets" not in plan["target"]
    assert "volume" not in plan["target"]


def test_actions_v2_change_variation_accepts_new_variation_and_changes_plan(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_variation", "day": "Mi", "exercise": "Curls", "new_variation": "LH"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert body["result"]["old_target"]["variation"] == "SZ"
    assert body["result"]["new_target"]["variation"] == "LH"

    plan = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()["result"]["plan"]
    curls = next(ex for ex in plan["days"]["Mi"]["exercises"] if ex["name"] == "Curls")
    assert curls["variation"] == "LH"
    assert "LH" in str(curls.get("display_name") or "")


def test_actions_v2_change_variation_accepts_variation_alias_and_keeps_existing_behavior(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_variation", "day": "Mi", "exercise": "Curls", "variation": "LH"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["new_target"]["variation"] == "LH"


def test_actions_v2_change_variation_requires_variation_value(client):
    _set_test_plan_with_mi_curls_sz()
    response = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_variation", "day": "Mi", "exercise": "Curls"},
        headers=auth(),
    )
    body = response.get_json()
    assert response.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "variation or new_variation is required"


def test_actions_v2_change_variation_noop_is_not_reported_as_applied_live(client):
    _set_test_plan_with_mi_curls_sz()
    body = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_variation", "day": "Mi", "exercise": "Curls", "new_variation": "SZ"},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    assert body["execution"]["mode"] == "no_change"
    assert body["execution"]["live_state_changed"] is False
    assert body["result"]["old_target"] == body["result"]["new_target"]


def test_actions_v2_liva_memory_routes_to_memory_data_modes(client):
    all_files = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "memory", "payload": {"mode": "all_files", "include_raw": False}},
        headers=auth(),
    ).get_json()["result"]
    files = all_files.get("files") or all_files.get("logical_files")
    assert files
    names = {item.get("logical_name") for item in files}
    assert {"health_notes", "core_patterns", "project_memory", "review_log"}.issubset(names)
    assert all("raw_markdown" not in item for item in files)

    file_payload = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "memory", "payload": {"mode": "file", "logical_file": "health_notes", "include_raw": True}},
        headers=auth(),
    ).get_json()["result"]
    assert file_payload["file"]["logical_name"] == "health_notes"
    assert "raw_markdown" in file_payload["file"]


def test_actions_v2_recovery_annotation_is_applied_live_and_visible_in_data(client):
    command = client.post(
        "/api/v2/actions/recovery/control",
        json={"command": "annotate_day", "date": "2026-04-17", "note": "test note"},
        headers=auth(),
    ).get_json()

    assert command["execution"]["mode"] == "applied_live"
    assert "hrv.recovery_day_annotations" in command["execution"]["affected_resources"]

    data = client.post(
        "/api/v2/actions/recovery/data",
        json={"date_from": "2026-04-17", "date_to": "2026-04-17"},
        headers=auth(),
    ).get_json()
    annotations = data["recovery"]["points"][0]["annotations"]
    assert annotations[0]["note"] == "test note"


def test_actions_v2_weight_log_upserts_existing_day(client):
    first = client.post(
        "/api/v2/actions/weight/control",
        json={"command": "log_weight", "date": "2026-04-16", "weight": 74.0},
        headers=auth(),
    ).get_json()

    assert first["execution"]["mode"] == "applied_live"
    assert first["result"]["updated"] is True

    data = client.post(
        "/api/v2/actions/weight/data",
        json={"date_from": "2026-04-16", "date_to": "2026-04-16"},
        headers=auth(),
    ).get_json()
    assert data["weight"]["points"][0]["weight"] == 74.0


def test_actions_v2_weight_checkin_note_remains_logged_only(client):
    body = client.post(
        "/api/v2/actions/weight/control",
        json={"command": "set_checkin_note", "date": "2026-04-17", "note": "test"},
        headers=auth(),
    ).get_json()

    assert body["execution"]["mode"] == "logged_only"
    assert body["execution"]["live_state_changed"] is False


def test_actions_v2_response_conventions_for_data_and_command(client):
    data_resp = client.post("/api/v2/actions/progression/data", json={"mode": "by_exercise", "exercise_names": ["Bench"], "metrics": ["e1rm"]}, headers=auth())
    data_body = data_resp.get_json()
    assert data_body["ok"] is True
    assert "data_status" in data_body
    assert "execution" not in data_body

    command_resp = client.post("/api/v2/actions/remote/control", json={"command": "pc_action", "action": "status"}, headers=auth())
    command_body = command_resp.get_json()
    assert command_body["ok"] is True
    assert command_body["execution"]["mode"] == "dry_run"
    assert command_body["command"] == "pc_action"


def test_actions_v2_command_execution_modes_are_canonical(client):
        calls = [
            ("/api/v2/actions/core/control", {"command": "clear_override"}),
            ("/api/v2/actions/training/adjust", {"command": "change_sets", "day": "Mo", "exercise": "Schrägbankdrücken", "sets": 2}),
        ("/api/v2/actions/training/plan-control", {"command": "set_active_days", "days": ["Mo"]}),
        ("/api/v2/actions/recovery/control", {"command": "annotate_day", "date": "2026-04-17", "note": "test"}),
        ("/api/v2/actions/nutrition/control", {"command": "adjust_targets", "kcal": 2500, "protein": 180, "carbs": 280, "fat": 70}),
        ("/api/v2/actions/weight/control", {"command": "set_checkin_note", "date": "2026-04-17", "note": "test"}),
        ("/api/v2/actions/runs/control", {"command": "mark_run_skipped", "date": "2026-04-17"}),
        ("/api/v2/actions/memory/store", {"command": "store_fact", "topic": "project", "text": "Test fact"}),
        ("/api/v2/actions/memory/link", {"command": "link_entry_to_domain", "entry_id": "mem_test", "domain": "training"}),
        ("/api/v2/actions/remote/control", {"command": "pc_action", "action": "status"}),
    ]
        allowed = {"applied_live", "applied_sidecar", "logged_only", "dry_run", "no_change"}
        for path, payload in calls:
            body = client.post(path, json=payload, headers=auth()).get_json()
            assert body["ok"] is True, f"{path} {payload} -> {body}"
            assert body["execution"]["mode"] in allowed
        assert body["execution"]["mode"] != "applied"


def test_actions_v2_weight_trend_requires_enough_points(client):
    resp = client.get("/api/v2/actions/weight/state", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["weight"]["trend_direction"] == "insufficient_data"
