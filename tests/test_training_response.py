from __future__ import annotations

import json
import sqlite3

import pytest

from analysis.training_response import (
    DEFAULT_PARAMS,
    ExerciseIdentity,
    SCOPE_ORDER,
    compute_training_response_payload,
    impulse_response,
    measured_performance_trend,
    muscle_shares_for_row,
    rpe_load_weight,
)


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY,
            date_iso TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY,
            workout_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            execution_mode TEXT DEFAULT '',
            canonical_exercise_id TEXT,
            variation_id TEXT
        );
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY,
            exercise_id INTEGER NOT NULL,
            workout_id INTEGER NOT NULL,
            set_number INTEGER,
            weight REAL,
            reps INTEGER,
            rpe REAL,
            is_warmup INTEGER,
            is_working_set INTEGER,
            intentional_deload INTEGER DEFAULT 0,
            technique_set INTEGER DEFAULT 0,
            progression_excluded INTEGER DEFAULT 0,
            set_slot TEXT,
            created_at TEXT
        );
        """
    )
    return conn


def add_session(
    conn: sqlite3.Connection,
    workout_id: int,
    day: str,
    exercises: list[dict],
) -> None:
    conn.execute(
        "INSERT INTO workouts(id,date_iso,name,created_at) VALUES(?,?,?,?)",
        (workout_id, day, f"Session {workout_id}", f"{day}T10:00:00Z"),
    )
    next_exercise = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM exercises").fetchone()[0])
    next_set = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM sets").fetchone()[0])
    for exercise in exercises:
        exercise_id = next_exercise
        next_exercise += 1
        conn.execute(
            """
            INSERT INTO exercises(
                id,workout_id,name,variation,device,laterality,execution_mode,variation_id
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                exercise_id,
                workout_id,
                exercise["name"],
                exercise.get("variation", ""),
                exercise.get("device", ""),
                exercise.get("laterality", "bilateral"),
                exercise.get("execution_mode", ""),
                exercise.get("variation_id", exercise.get("variation", "")),
            ),
        )
        for set_number, set_row in enumerate(exercise.get("sets", []), start=1):
            conn.execute(
                """
                INSERT INTO sets(
                    id,exercise_id,workout_id,set_number,weight,reps,rpe,is_warmup,
                    is_working_set,intentional_deload,technique_set,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    next_set,
                    exercise_id,
                    workout_id,
                    set_number,
                    set_row.get("weight"),
                    set_row.get("reps"),
                    set_row.get("rpe"),
                    set_row.get("is_warmup"),
                    set_row.get("is_working_set"),
                    int(bool(set_row.get("intentional_deload"))),
                    int(bool(set_row.get("technique_set"))),
                    f"{day}T10:{set_number:02d}:00Z",
                ),
            )
            next_set += 1
    conn.commit()


def bench(weight: float = 100.0, rpe: float | None = 8.0, **set_extra) -> dict:
    return {
        "name": "Bench",
        "variation": "LH",
        "device": "LH",
        "laterality": "bilateral",
        "sets": [{"weight": weight, "reps": 8, "rpe": rpe, **set_extra}],
    }


def test_exact_exercise_identity_distributes_muscle_shares():
    shares = muscle_shares_for_row(
        {"exercise_name": "Bench", "variation": "LH", "device": "LH", "laterality": "bilateral"}
    )
    assert shares == {"chest": 1.0, "triceps": 0.5, "shoulders": 0.3}
    assert muscle_shares_for_row(
        {"exercise_name": "Bench", "variation": "Smith", "device": "Smith", "laterality": "bilateral"}
    ) is None


def test_overall_counts_compound_set_once_while_scopes_use_shares():
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    payload = compute_training_response_payload(conn, end_iso="2026-01-01")
    assert payload["scopes"]["overall"]["load"] == [1.0]
    assert payload["scopes"]["chest"]["load"] == [1.0]
    assert payload["scopes"]["triceps"]["load"] == [0.5]
    assert payload["scopes"]["shoulders"]["load"] == [0.3]


def test_explicit_and_inferred_warmups_do_not_count():
    conn = make_db()
    exercise = bench()
    exercise["sets"] = [
        {"weight": 40, "reps": 15, "rpe": 5, "is_warmup": True},
        {"weight": 60, "reps": 15, "rpe": 6},
        {"weight": 100, "reps": 8, "rpe": 8},
    ]
    add_session(conn, 1, "2026-01-01", [exercise])
    payload = compute_training_response_payload(conn, end_iso="2026-01-01")
    assert payload["scopes"]["overall"]["load"] == [1.0]


def test_rpe_weighting_interpolates_and_missing_rpe_is_conservative():
    assert rpe_load_weight(6) == pytest.approx(0.35)
    assert rpe_load_weight(7) == pytest.approx(0.65)
    assert rpe_load_weight(8.5) == pytest.approx(1.075)
    assert rpe_load_weight(10) == pytest.approx(1.25)
    assert rpe_load_weight(None) == pytest.approx(0.65)


def test_missing_rpe_counts_as_load_and_reduces_data_quality():
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench(rpe=None)])
    scope = compute_training_response_payload(conn, end_iso="2026-01-01")["scopes"]["overall"]
    assert scope["load"] == [0.65]
    assert scope["data_quality"]["missing_rpe_sets"] == 1
    assert scope["performance"] == [None]


def test_unknown_exercise_is_reported_without_breaking_payload():
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [{"name": "Mystery Lift", "variation": "X", "device": "Y", "sets": [{"weight": 50, "reps": 10, "rpe": 8}]}])
    payload = compute_training_response_payload(conn, end_iso="2026-01-01")
    assert payload["scopes"]["overall"]["load"] == [0.0]
    assert payload["meta"]["unmapped_exercises"][0]["name"] == "mystery lift"


def test_performance_baseline_uses_only_previous_42_days():
    conn = make_db()
    for workout_id, (day, weight) in enumerate(
        [("2026-01-01", 100), ("2026-01-08", 100), ("2026-01-15", 100), ("2026-01-22", 110)],
        start=1,
    ):
        add_session(conn, workout_id, day, [bench(weight)])
    payload = compute_training_response_payload(conn, end_iso="2026-01-22")
    chest = payload["scopes"]["chest"]
    index = payload["dates"].index("2026-01-22")
    assert chest["performance"][index] == pytest.approx(110.0)
    detail = chest["performance_details"]["2026-01-22"][0]
    assert detail["baseline_score"] == pytest.approx(100 * (1 + 10 / 30), abs=0.001)


def test_exact_variants_are_never_mixed_into_one_baseline():
    conn = make_db()
    for workout_id, day in enumerate(("2026-01-01", "2026-01-08", "2026-01-15"), start=1):
        add_session(conn, workout_id, day, [bench(100)])
    smith = {
        "name": "Schrägbankdrücken",
        "variation": "Smith",
        "device": "Smith",
        "laterality": "bilateral",
        "sets": [{"weight": 100, "reps": 8, "rpe": 8}],
    }
    add_session(conn, 4, "2026-01-22", [smith])
    payload = compute_training_response_payload(conn, end_iso="2026-01-22")
    assert payload["scopes"]["chest"]["performance"][payload["dates"].index("2026-01-22")] is None


def test_confirmed_measurement_scale_change_starts_a_new_baseline():
    conn = make_db()
    values = [100, 100, 100, 25, 26, 27, 28]
    days = ["2026-01-01", "2026-01-05", "2026-01-09", "2026-01-13", "2026-01-17", "2026-01-21", "2026-01-25"]
    for workout_id, (day, weight) in enumerate(zip(days, values), start=1):
        add_session(conn, workout_id, day, [bench(weight)])

    payload = compute_training_response_payload(conn, end_iso=days[-1])
    chest = payload["scopes"]["chest"]
    assert all(value is None or value > 80 for value in chest["performance"])
    assert chest["performance"][payload["dates"].index(days[-1])] == pytest.approx(28 / 26 * 100, abs=0.001)
    assert payload["meta"]["measurement_regime_resets"][0]["confirmed_on"] == "2026-01-17"


def test_rest_days_have_no_invented_measured_performance():
    conn = make_db()
    for workout_id, day in enumerate(("2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22"), start=1):
        add_session(conn, workout_id, day, [bench(100 + workout_id)])
    payload = compute_training_response_payload(conn, end_iso="2026-01-24")
    chest = payload["scopes"]["chest"]
    assert chest["performance"][payload["dates"].index("2026-01-22")] is not None
    assert chest["performance"][payload["dates"].index("2026-01-23")] is None
    assert chest["performance"][payload["dates"].index("2026-01-24")] is None


def test_performance_trend_is_trailing_robust_and_only_exists_on_measurement_days():
    values = [100.0, None, 102.0, 160.0, None, 103.0, 104.0, 105.0]
    assert measured_performance_trend(values) == [100.0, None, 100.35, 100.927, None, 101.478, 102.011, 102.707]


def test_future_session_does_not_change_past_load_values():
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    before = compute_training_response_payload(conn, end_iso="2026-01-01")
    add_session(conn, 2, "2026-02-01", [bench(rpe=10)])
    after = compute_training_response_payload(conn, end_iso="2026-02-01")
    assert after["scopes"]["overall"]["load"][after["dates"].index("2026-01-01")] == before["scopes"]["overall"]["load"][0]


def test_impulse_model_drops_first_then_can_show_positive_window_and_returns():
    assert impulse_response(0, DEFAULT_PARAMS) < 0
    assert DEFAULT_PARAMS.fatigue_tau_days < DEFAULT_PARAMS.adaptation_tau_days
    later = [impulse_response(day, DEFAULT_PARAMS) for day in range(1, 20)]
    assert max(later) > 0
    assert abs(impulse_response(120, DEFAULT_PARAMS)) < abs(impulse_response(20, DEFAULT_PARAMS))


def test_short_history_uses_default_instead_of_calibration():
    conn = make_db()
    for workout_id, day in enumerate(("2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22"), start=1):
        add_session(conn, workout_id, day, [bench(100 + workout_id)])
    model = compute_training_response_payload(conn, end_iso="2026-01-22")["scopes"]["chest"]["model"]
    assert model["calibrated"] is False


def test_overall_performance_equal_weights_areas_not_exercise_frequency():
    conn = make_db()
    for workout_id, day in enumerate(("2026-01-01", "2026-01-08", "2026-01-15"), start=1):
        add_session(
            conn,
            workout_id,
            day,
            [bench(100), {"name": "Hammers", "variation": "SZ", "device": "SZ", "sets": [{"weight": 20, "reps": 8, "rpe": 8}]}],
        )
    add_session(
        conn,
        4,
        "2026-01-22",
        [
            bench(110),
            {"name": "Hammers", "variation": "SZ", "device": "SZ", "sets": [{"weight": 18, "reps": 8, "rpe": 8}]},
            {"name": "sup. Curls", "variation": "KH", "device": "KH", "sets": [{"weight": 10, "reps": 8, "rpe": 8}]},
        ],
    )
    payload = compute_training_response_payload(conn, end_iso="2026-01-22")
    idx = payload["dates"].index("2026-01-22")
    chest_value = payload["scopes"]["chest"]["performance"][idx]
    biceps_value = payload["scopes"]["biceps"]["performance"][idx]
    overall_value = payload["scopes"]["overall"]["performance"][idx]
    assert overall_value == pytest.approx((chest_value + biceps_value) / 2.0)
    assert {item["scope"] for item in payload["scopes"]["overall"]["performance_details"]["2026-01-22"]} == {"chest", "biceps"}


def test_all_scopes_and_json_numbers_are_stable():
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    payload = compute_training_response_payload(conn, end_iso="2026-01-03")
    assert tuple(payload["scopes"]) == SCOPE_ORDER
    json.dumps(payload, allow_nan=False)


def test_empty_history_returns_all_seven_clean_scopes():
    payload = compute_training_response_payload(make_db(), end_iso="2026-01-01")
    assert payload["dates"] == []
    assert tuple(payload["scopes"]) == SCOPE_ORDER
    assert all(scope["load"] == [] for scope in payload["scopes"].values())
