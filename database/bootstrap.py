"""Create the empty, portable database foundation required on first start."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from database.connections import DB_DIR


def _apply(path: Path, schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(schema)


def ensure_base_schemas(database_dir: str | Path | None = None) -> None:
    """Initialize only stable base tables; feature modules own later migrations."""

    root = Path(database_dir or DB_DIR)
    root.mkdir(parents=True, exist_ok=True)

    _apply(
        root / "training.sqlite3",
        """
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            name TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            session_key TEXT,
            raw_import_text TEXT,
            plan_id INTEGER,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workouts_date_iso ON workouts(date_iso);

        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            canonical_exercise_id TEXT,
            variation TEXT,
            variation_id TEXT,
            device TEXT,
            laterality TEXT,
            execution_mode TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_exercises_workout_id ON exercises(workout_id);

        CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER NOT NULL,
            workout_id INTEGER NOT NULL,
            set_number INTEGER,
            set_slot TEXT,
            weight REAL,
            reps INTEGER,
            rpe REAL,
            is_warmup INTEGER,
            is_working_set INTEGER,
            intentional_deload INTEGER NOT NULL DEFAULT 0,
            technique_set INTEGER NOT NULL DEFAULT 0,
            progression_excluded INTEGER NOT NULL DEFAULT 0,
            progression_exclusion_reason TEXT,
            created_at TEXT,
            FOREIGN KEY (exercise_id) REFERENCES exercises(id) ON DELETE CASCADE,
            FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sets_workout_id ON sets(workout_id);
        CREATE INDEX IF NOT EXISTS idx_sets_exercise_id ON sets(exercise_id);
        """,
    )

    _apply(
        root / "ernaehrung.sqlite3",
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
            raw_json TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_weight_logs_date_iso ON weight_logs(date_iso);
        """,
    )

    _apply(
        root / "hrv.sqlite3",
        """
        CREATE TABLE IF NOT EXISTS hrv_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_measurement TEXT,
            date_utc TEXT,
            hr REAL,
            rmssd REAL,
            sdnn REAL,
            avnn REAL,
            signal_quality TEXT,
            source_file TEXT,
            training_motivation REAL,
            fatigue REAL,
            sickness TEXT,
            sleep_quality REAL,
            alcohol TEXT,
            sickness_bool INTEGER,
            alcohol_bool INTEGER,
            flag TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_hrv_measurements_date ON hrv_measurements(date_utc);
        """,
    )

    _apply(
        root / "runs.sqlite3",
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            name TEXT,
            distance REAL,
            moving_time INTEGER,
            avg_hr REAL,
            max_hr REAL,
            elevation_gain REAL,
            pace REAL,
            sport_type TEXT,
            run_type TEXT,
            avg_power REAL,
            stair_floors REAL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(date);
        """,
    )


__all__ = ["ensure_base_schemas"]
