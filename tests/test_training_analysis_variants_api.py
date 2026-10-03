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
            raw_import_text TEXT
        );
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY,
            workout_id INTEGER NOT NULL,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            notes TEXT
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
    conn.executescript(
        """
        INSERT INTO workouts (id, date, date_iso, name) VALUES
            (1, '2025-12-23', '2025-12-23', 'Legs'),
            (2, '2026-06-09', '2026-06-09', 'FB Push B'),
            (3, '2026-06-25', '2026-06-25', 'FB Push B'),
            (4, '2026-06-20', '2026-06-20', 'Pull');

        INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES
            (11, 1, 'Beinpresse', 'unilat', 'eGym', 'unilateral'),
            (12, 2, 'Beinpresse', 'unilat', '', 'unilateral'),
            (13, 3, 'Beinpresse', 'unilat', '', 'unilateral'),
            (14, 4, 'Latzug', 'eGym', 'eGym', 'bilateral');

        INSERT INTO sets (id, exercise_id, workout_id, set_number, reps, weight, rpe, created_at) VALUES
            (101, 11, 1, 1, 9, 105.0, 8.0, '2025-12-23T10:00:00'),
            (102, 12, 2, 1, 7, 107.0, 8.0, '2026-06-09T10:00:00'),
            (103, 13, 3, 1, 10, 108.0, 8.0, '2026-06-25T10:00:00'),
            (104, 14, 4, 1, 10, 70.0, 8.0, '2026-06-20T10:00:00');
        """
    )
    conn.commit()
    conn.close()


def test_exercise_variants_uses_variation_laterality_when_device_missing(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()

    response = client.get("/api/analysis/exercise_variants?exercise=Beinpresse")
    data = response.get_json()

    assert response.status_code == 200
    assert data[0]["device"] == "unilat"
    assert data[0]["last_date"] == "2026-06-25"
    assert len(data) == 1


def test_exercise_variants_keeps_regular_device_labels_for_non_mode_exercises(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()

    response = client.get("/api/analysis/exercise_variants?exercise=Latzug")
    data = response.get_json()

    assert response.status_code == 200
    assert data == [{"device": "eGym", "last_date": "2026-06-20"}]


def test_exercise_progress_maps_unilat_device_to_laterality_history(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()

    response = client.get("/api/analysis/exercise_progress?exercise=Beinpresse&device=unilat")
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "progress"
    assert data["trend"] in {"up", "flat"}
    assert data["latest"]["date"] == "2026-06-25"
    assert data["previous"]["date"] == "2026-06-09"


def test_workout_payload_embeds_set_progress_for_first_card_render(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    conn = sqlite3.connect(db_conn.TRAINING_DB)
    conn.execute(
        "INSERT INTO workouts (id, date, date_iso, name) VALUES (?, ?, ?, ?)",
        (5, "2026-07-02", "2026-07-02", "FB Push B"),
    )
    conn.execute(
        "INSERT INTO exercises (id, workout_id, name, variation, device, laterality) VALUES (?, ?, ?, ?, ?, ?)",
        (15, 5, "Beinpresse", "unilat", "", "unilateral"),
    )
    conn.execute(
        "INSERT INTO sets (id, exercise_id, workout_id, set_number, reps, weight, rpe, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (105, 15, 5, 1, 10, 110.0, 8.0, "2026-07-02T10:00:00"),
    )
    conn.commit()
    conn.close()

    appmod = _load_app_module(monkeypatch)
    response = appmod.app.test_client().get("/api/workout/5")
    data = response.get_json()
    first_set = data["exercises"][0]["sets"][0]

    assert response.status_code == 200
    assert first_set["last_status"] == "progress"
    assert first_set["last_ref"]["weight"] == 108.0
    assert first_set["last_reason_code"]
    assert first_set["last_reason_text"]


def test_workout_payload_keeps_excluded_sets_neutral_and_explains_them_in_notes(monkeypatch, tmp_path):
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    _mk_training_db(db_conn.TRAINING_DB)
    appmod = _load_app_module(monkeypatch)
    conn = sqlite3.connect(db_conn.TRAINING_DB)
    conn.execute(
        "UPDATE sets SET progression_excluded=1, progression_exclusion_reason=? WHERE id=104",
        ("Neue Ausführung mit Endpunktpause; nicht direkt vergleichbar.",),
    )
    conn.commit()
    conn.close()

    response = appmod.app.test_client().get("/api/workout/4")
    data = response.get_json()
    exercise = data["exercises"][0]
    logged_set = exercise["sets"][0]

    assert response.status_code == 200
    assert logged_set["progression_excluded"] == 1
    assert logged_set["last_ref"] is None
    assert logged_set["last_status"] is None
    assert logged_set["last_reason_code"] == "one_time_progression_exclusion"
    assert exercise["notes"] == (
        "Progressionsbewertung ausgeschlossen: "
        "Neue Ausführung mit Endpunktpause; nicht direkt vergleichbar."
    )
