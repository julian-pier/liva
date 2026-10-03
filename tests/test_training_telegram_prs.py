from __future__ import annotations

import sqlite3

from tests.test_autopilot_frontend_payload_regression import _load_app_module


def _mk_training_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            name TEXT,
            notes TEXT
        );
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER NOT NULL,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            notes TEXT
        );
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def test_active_plan_exercise_name_keys_supports_liva_ref_blocks(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(
        appmod,
        "_get_active_gym_plan_record",
        lambda: {
            "plan_json": {
                "days": [
                    {
                        "day": "Di",
                        "events": [
                            {
                                "kind": "gym",
                                "items": [
                                    {"kind": "ref_block", "display_name": "Squats"},
                                    {"kind": "exercise", "name": "Leg Curls"},
                                ],
                            }
                        ],
                    }
                ],
                "base_week": [
                    {
                        "session_name": "Lower A",
                        "strength_exercises": [
                            {"exercise_name": "RDL"},
                        ],
                    }
                ],
            }
        },
    )

    keys = appmod._active_plan_exercise_name_keys()

    assert "squats" in keys
    assert "leg curls" in keys
    assert "rdl" in keys


def test_notify_plan_e1rm_prs_sends_for_liva_ref_block_exercise(monkeypatch, tmp_path):
    appmod = _load_app_module(monkeypatch)
    db_path = tmp_path / "training.sqlite3"
    _mk_training_db(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO workouts (date, date_iso, name, notes) VALUES ('20.03.26', '2026-03-20', 'Lower A', '')")
    prev_workout_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'LH', '', '', '')",
        (prev_workout_id,),
    )
    prev_exercise_id = cur.lastrowid
    conn.execute(
        "INSERT INTO sets (exercise_id, workout_id, set_number, reps, weight, rpe, created_at) VALUES (?, ?, 1, 6, 100.0, 8.0, '2026-03-20T10:00:00')",
        (prev_exercise_id, prev_workout_id),
    )

    cur = conn.execute("INSERT INTO workouts (date, date_iso, name, notes) VALUES ('26.03.26', '2026-03-26', 'Lower A', '')")
    current_workout_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'LH', '', '', '')",
        (current_workout_id,),
    )
    current_exercise_id = cur.lastrowid
    conn.execute(
        "INSERT INTO sets (exercise_id, workout_id, set_number, reps, weight, rpe, created_at) VALUES (?, ?, 1, 8, 100.0, 8.0, '2026-03-26T10:00:00')",
        (current_exercise_id, current_workout_id),
    )
    conn.commit()
    conn.close()

    def _training_conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(appmod, "get_training_db", _training_conn)
    monkeypatch.setattr(
        appmod,
        "_get_active_gym_plan_record",
        lambda: {
            "plan_json": {
                "days": [
                    {
                        "day": "Di",
                        "events": [
                            {
                                "kind": "gym",
                                "items": [
                                    {"kind": "ref_block", "display_name": "Squats"},
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(
        appmod,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text)) or {"ok": True, "message_id": 9001},
    )

    appmod._notify_plan_e1rm_prs(current_workout_id)

    assert sent
    assert sent[0][0] == "training"
    assert "PR" in sent[0][1]
    assert "Squats" in sent[0][1]
