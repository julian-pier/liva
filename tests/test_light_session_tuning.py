from __future__ import annotations

import sqlite3

import database.connections as db_conn
from training.expected_exercises import expected_exercises_for_session

from tests.test_autopilot_frontend_payload_regression import _load_app_module


def test_expected_exercises_light_keeps_intensity_and_only_trims_some_sets():
    plan = {
        "id": 1,
        "base_week": [
            {
                "session_name": "Lower A",
                "strength_exercises": [
                    {"exercise_name": "Squat", "sets": 3, "reps_min": 5, "reps_max": 5, "rpe_min": 8.0, "rpe_max": 9.0},
                    {"exercise_name": "RDL", "sets": 4, "reps_min": 6, "reps_max": 6, "rpe_min": 8.0, "rpe_max": 9.0},
                    {"exercise_name": "Leg Curl", "sets": 3, "reps_min": 10, "reps_max": 12, "rpe_min": 8.0, "rpe_max": 9.0},
                ],
            }
        ],
    }

    payload = expected_exercises_for_session(
        {"session_key": "d1-lower-a", "session_name": "Lower A"},
        day_id="2026-03-03",
        plan_ctx=plan,
        mode="light",
    )

    exercises = payload["exercises"]
    assert [ex["final_sets"] for ex in exercises] == [3, 3, 2]
    assert exercises[0]["final_rpe_min"] == 7.5
    assert exercises[0]["final_rpe_max"] == 8.5
    assert exercises[1]["final_rpe_min"] == 7.5
    assert exercises[1]["final_rpe_max"] == 8.5


def test_dashboard_light_does_not_lower_weight_suggestion(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    conn = sqlite3.connect(db_conn.TRAINING_DB)
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

    appmod = _load_app_module(monkeypatch)
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
                    "name": "Squat",
                    "variation": "",
                    "device": "Barbell",
                    "planned_sets": 3,
                    "final_sets": 3,
                    "planned_rpe_min": 8.0,
                    "planned_rpe_max": 9.0,
                    "final_rpe_min": 7.5,
                    "final_rpe_max": 8.5,
                    "expected_reps": "5",
                    "exercise_key": "squat",
                    "slot": 0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        appmod,
        "_exercise_report",
        lambda *args, **kwargs: {
            "coverage": "planned_missing",
            "planned": {"sets": 3},
            "done": {"sets": 0, "reps": [], "avg_rpe": None, "weights": [], "rpes": [], "set_numbers": [], "rpe_target_hit": True},
            "labels": {},
        },
    )
    monkeypatch.setattr(
        appmod,
        "_progression_decision",
        lambda ex: {
            "pill": {"text": "OK", "color": "neutral"},
            "suggestion_inline": "",
            "structured": {
                "working_weight": 100.0,
                "new_weights": [100.0, 100.0, 100.0],
            },
        },
    )

    with appmod.app.test_request_context("/api/dashboard/plan_check_for_session?session_key=d1-lower-a&mode=light"):
        response = appmod.api_dashboard_plan_check_for_session()
    payload = response.get_json()
    item = payload["items"][0]

    assert item["planned"]["weight_suggestion"] == 100.0
    assert item["structured"]["working_weight"] == 100.0


def test_plan_check_reanchors_non_local_weight_drop_to_reference_floor(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    conn = sqlite3.connect(db_conn.TRAINING_DB)
    conn.executescript(
        """
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY,
            date TEXT,
            date_iso TEXT,
            name TEXT,
            session_key TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [
                {
                    "session_name": "Lower B",
                    "strength_exercises": [
                        {"exercise_name": "RDL", "sets": 1, "reps_min": 6, "reps_max": 8, "device": "Barbell"},
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_plan_strength_sessions",
        lambda plan: [{"session_key": "d1-lower-b", "session_name": "Lower B"}],
    )
    monkeypatch.setattr(
        appmod,
        "_latest_exercise_history",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        appmod,
        "_history_entry_for_target",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        appmod,
        "_previous_exercise_occurrences_with_fallback",
        lambda *args, **kwargs: ([], "latest_any"),
    )
    monkeypatch.setattr(
        appmod,
        "_progression_decision",
        lambda ex: {
            "pill": {"text": "Gewicht ↓", "color": "red"},
            "suggestion_inline": "1 x 6 x 123kg",
            "structured": {
                "working_weight": 123.0,
                "new_weights": [123.0],
                "new_reps": [6],
                "history_profile": {"current_standard_weight": 130.0},
                "weight_guard": {"local_reason_present": False},
            },
        },
    )

    with appmod.app.test_request_context("/api/dashboard/plan_check_for_session?session_key=d1-lower-b&mode=normal&day=2026-05-07"):
        response = appmod.api_dashboard_plan_check_for_session()
    payload = response.get_json()
    item = payload["items"][0]

    assert item["planned"]["weight_suggestion"] == 127.5
    assert item["structured"]["working_weight"] == 127.5
    assert item["structured"]["new_weights"][0] == 127.5
    assert item["structured"]["weight_guard"]["global_reduction_blocked"] is True
