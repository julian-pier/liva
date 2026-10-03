import sqlite3

from analysis.training_analysis import get_exercise_history


def test_get_exercise_history_prefers_heaviest_set_over_higher_e1rm():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE workouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL,
          date_iso TEXT NOT NULL,
          name TEXT NOT NULL,
          notes TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE exercises (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workout_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          variation TEXT,
          device TEXT,
          laterality TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE sets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          exercise_id INTEGER NOT NULL,
          workout_id INTEGER NOT NULL,
          set_number INTEGER,
          weight REAL,
          reps INTEGER,
          rpe REAL,
          created_at TEXT NOT NULL
        );
        INSERT INTO workouts(date, date_iso, name, notes, created_at)
        VALUES ('2026-06-29', '2026-06-29', 'FB Pull B', '', '2026-06-29T10:00:00');
        INSERT INTO exercises(workout_id, name, variation, device, laterality, created_at)
        VALUES (1, 'enge Rows', 'KH', 'KH', 'bilateral', '2026-06-29T10:00:00');
        INSERT INTO sets(exercise_id, workout_id, set_number, weight, reps, rpe, created_at)
        VALUES
          (1, 1, 1, 40.0, 8, 9.0, '2026-06-29T10:01:00'),
          (1, 1, 2, 37.5, 11, 8.0, '2026-06-29T10:02:00');
        """
    )

    rows = get_exercise_history(conn, "enge Rows", "KH", "bilateral")

    assert len(rows) == 1
    assert rows[0]["weight"] == 40.0
    assert rows[0]["reps"] == 8

    assert get_exercise_history(
        conn,
        "enge Rows",
        "KH",
        "bilateral",
        start_iso="2026-06-30",
    ) == []
