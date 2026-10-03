from __future__ import annotations

import sqlite3

import database.connections as db_conn

from tests.test_autopilot_frontend_payload_regression import _load_app_module


def _mk_training_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY,
            date TEXT,
            date_iso TEXT,
            name TEXT,
            session_key TEXT
        );
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY,
            workout_id INTEGER NOT NULL,
            name TEXT,
            variation TEXT,
            device TEXT
        );
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY,
            exercise_id INTEGER NOT NULL,
            workout_id INTEGER NOT NULL,
            set_number INTEGER,
            reps INTEGER,
            weight REAL,
            rpe REAL,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _seed_history(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        INSERT INTO workouts (id, date, date_iso, name, session_key) VALUES
            (1, '21.02.26', '2026-02-21', 'Push A', 'd1-lower-a'),
            (2, '19.02.26', '2026-02-19', 'Lower A', 'legacy-lower-a'),
            (3, '26.02.26', '2026-02-26', 'Lower A', 'new-lower-a');

        INSERT INTO exercises (id, workout_id, name, variation, device) VALUES
            (11, 1, 'Squats', '', 'Barbell'),
            (12, 2, 'Squats', '', 'Barbell'),
            (13, 3, 'RDL', '', 'Barbell');

        INSERT INTO sets (id, exercise_id, workout_id, set_number, reps, weight, rpe, created_at) VALUES
            (101, 11, 1, 1, 5, 120.0, 8.0, '2026-02-21T10:00:00'),
            (102, 12, 2, 1, 5, 90.0, 7.0, '2026-02-19T10:00:00'),
            (103, 13, 3, 1, 8, 130.0, 8.0, '2026-02-26T10:00:00');
        """
    )
    conn.commit()


def _install_common_patches(monkeypatch, appmod) -> None:
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(appmod, "plan_row_to_dict", lambda row: {"id": 1})
    monkeypatch.setattr(
        appmod,
        "_plan_strength_sessions",
        lambda plan: [{"session_key": "d1-lower-a", "session_name": "Lower A"}],
    )
    monkeypatch.setattr(
        appmod,
        "expected_exercises_for_session",
        lambda *args, **kwargs: {
            "exercises": [
                {
                    "name": "Squats",
                    "variation": "",
                    "device": "Barbell",
                    "planned_sets": 3,
                    "planned_rpe_min": 6.0,
                    "planned_rpe_max": 6.0,
                    "expected_reps": "5",
                    "exercise_key": "squats",
                }
            ]
        },
    )
    monkeypatch.setattr(
        appmod,
        "load_plan_check_report",
        lambda workout_id: {"version": appmod.PLAN_CHECK_VERSION, "plan_id": 1, "exercises": []},
    )
    monkeypatch.setattr(appmod, "_load_plan_session_map", lambda cur, plan_id, session_key: {})
    monkeypatch.setattr(appmod, "_upsert_plan_session_map", lambda *args, **kwargs: None)

    def fake_exercise_report(plan_row, matches, coverage, history=None, **kwargs):
        return {
            "coverage": coverage,
            "planned": {"sets": plan_row.get("sets") or 0},
            "done": {
                "sets": 0,
                "reps": [],
                "avg_rpe": None,
                "weights": [],
                "rpes": [],
                "set_numbers": [],
                "rpe_target_hit": True,
            },
            "labels": {},
            "history": history or {},
        }

    def fake_progression_decision(exercise_report):
        history_sets = exercise_report.get("history_sets") or []
        history = exercise_report.get("history") or {}
        weight = None
        if history_sets:
            weight = history_sets[0].get("weight")
        elif history:
            weight = history.get("working_weight") or history.get("top_weight")
        return {
            "pill": {"text": "OK", "color": "neutral"},
            "suggestion_inline": str(exercise_report.get("history_source") or ""),
            "structured": {
                "working_weight": weight,
                "new_weights": [weight] if weight is not None else [],
            },
        }

    monkeypatch.setattr(appmod, "_exercise_report", fake_exercise_report)
    monkeypatch.setattr(appmod, "_progression_decision", fake_progression_decision)


def test_reference_workout_uses_matching_session_name_not_session_key(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    conn = sqlite3.connect(db_conn.TRAINING_DB)
    _seed_history(conn)
    conn.close()

    appmod = _load_app_module(monkeypatch)
    _install_common_patches(monkeypatch, appmod)

    with appmod.app.test_request_context("/api/dashboard/plan_check_for_session?session_key=d1-lower-a"):
        response = appmod.api_dashboard_plan_check_for_session()
    payload = response.get_json()

    assert payload["reference_workout"]["id"] == 3
    assert payload["reference_workout"]["name"] == "Lower A"


def test_missing_item_prefers_same_session_history_before_any_session(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    conn = sqlite3.connect(db_conn.TRAINING_DB)
    _seed_history(conn)
    conn.close()

    appmod = _load_app_module(monkeypatch)
    _install_common_patches(monkeypatch, appmod)

    with appmod.app.test_request_context("/api/dashboard/plan_check_for_session?session_key=d1-lower-a"):
        response = appmod.api_dashboard_plan_check_for_session()
    payload = response.get_json()
    item = payload["items"][0]

    assert payload["reference_workout"]["id"] == 3
    assert item["structured"]["working_weight"] == 90.0
    assert item["suggestion_inline"] == "session_name"
