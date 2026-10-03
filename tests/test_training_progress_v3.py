import sqlite3

import pytest

import database.connections as connections
from ai.actions_v2 import compare_workout_to_last
from analysis.dashboard_calendar import _load_strength_day_counters
from analysis.progression_rules import RULE_VERSION
from analysis.training_analysis import compute_default_set_progress_series
from analysis.training_progress import (
    compute_progress_payloads,
    compute_session_progress,
    progress_payload_for_gpt,
)


def db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE workouts (id INTEGER PRIMARY KEY, date TEXT, date_iso TEXT, name TEXT, raw_import_text TEXT);
        CREATE TABLE exercises (id INTEGER PRIMARY KEY, workout_id INTEGER, name TEXT, canonical_exercise_id TEXT, variation TEXT, variation_id TEXT, device TEXT, laterality TEXT, execution_mode TEXT);
        CREATE TABLE sets (id INTEGER PRIMARY KEY, exercise_id INTEGER, workout_id INTEGER, set_number INTEGER, set_slot TEXT, weight REAL, reps INTEGER, rpe REAL, is_warmup INTEGER, is_working_set INTEGER, intentional_deload INTEGER NOT NULL DEFAULT 0, technique_set INTEGER NOT NULL DEFAULT 0, progression_excluded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE exercise_aliases (id INTEGER PRIMARY KEY, alias TEXT UNIQUE, exercise_id INTEGER);
        """
    )
    return conn


def add_session(conn, wid, day, sets, name="Session"):
    conn.execute("INSERT INTO workouts(id,date,date_iso,name) VALUES(?,?,?,?)", (wid, day, day, name))
    exercise_ids = {}
    next_eid = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM exercises").fetchone()[0])
    next_sid = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM sets").fetchone()[0])
    for row in sets:
        key = (row.get("exercise", "Bench"), row.get("variation", "flat"), row.get("device", "barbell"), row.get("laterality", "bilateral"))
        if key not in exercise_ids:
            exercise_ids[key] = next_eid
            conn.execute(
                "INSERT INTO exercises(id,workout_id,name,variation,device,laterality,canonical_exercise_id,variation_id,execution_mode) VALUES(?,?,?,?,?,?,?,?,?)",
                (next_eid, wid, *key, row.get("canonical_exercise_id"), row.get("variation_id"), row.get("execution_mode")),
            )
            next_eid += 1
        conn.execute(
            "INSERT INTO sets(id,exercise_id,workout_id,set_number,set_slot,weight,reps,rpe,is_warmup,is_working_set,intentional_deload,technique_set) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_sid, exercise_ids[key], wid, row.get("set_number", 1), row.get("set_slot"), row.get("weight", 100), row.get("reps", 8), row["rpe"] if "rpe" in row else 8, row.get("is_warmup"), row.get("is_working_set"), int(bool(row.get("intentional_deload"))), int(bool(row.get("technique_set")))),
        )
        next_sid += 1
    conn.commit()


def counts(payload):
    return {key: payload[key] for key in ("improved", "same", "worse", "comparable_sets", "total_eligible_sets", "net_progress", "rule_version")}


def test_rep_gain_with_one_point_higher_rpe_counts_in_progression_quote():
    conn = db()
    add_session(conn, 1, "2026-07-20", [{"weight": 100, "reps": 10, "rpe": 8}])
    add_session(conn, 2, "2026-07-27", [{"weight": 100, "reps": 11, "rpe": 9}])

    payload = compute_session_progress(conn, 2)

    assert payload["improved"] == 1
    assert payload["same"] == 0
    assert payload["progress_rate"] == 1.0
    assert payload["set_comparisons"][0]["reason_code"] == "successful_rep_progression"


def test_pull_b_progress_uses_only_exercise_and_variation_and_counts_balanced_rpe_gain():
    conn = db()
    previous = [
        {"exercise": "PullUps", "variation": "weighted", "variation_id": "weighted", "weight": 15, "reps": 9, "rpe": 8, "set_number": 1},
        {"exercise": "PullUps", "variation": "weighted", "variation_id": "weighted", "weight": 12.5, "reps": 9, "rpe": 9, "set_number": 2},
        {"exercise": "HighRows", "variation": "unilat", "variation_id": "unilat", "execution_mode": "Schulter unten, Halbkreis ziehen", "weight": 62.5, "reps": 10, "rpe": 8, "set_number": 1},
        {"exercise": "HighRows", "variation": "unilat", "variation_id": "unilat", "execution_mode": "Schulter unten, Halbkreis ziehen", "weight": 62.5, "reps": 8, "rpe": 8, "set_number": 2},
        {"exercise": "sup. Curls", "variation": "KH", "variation_id": "KH", "weight": 17.5, "reps": 8, "rpe": 9, "set_number": 1},
        {"exercise": "sup. Curls", "variation": "KH", "variation_id": "KH", "weight": 15, "reps": 11, "rpe": 9, "set_number": 2},
        {"exercise": "Cable Crunches", "variation": "SZ", "variation_id": "SZ", "weight": 40, "reps": 11, "rpe": 8, "set_number": 1},
        {"exercise": "Cable Crunches", "variation": "SZ", "variation_id": "SZ", "weight": 40, "reps": 9, "rpe": 8, "set_number": 2},
        {"exercise": "Beinbeuger", "variation": "eGym", "variation_id": "eGym", "weight": 70, "reps": 7, "rpe": 7, "set_number": 1},
        {"exercise": "Beinbeuger", "variation": "eGym", "variation_id": "eGym", "weight": 68, "reps": 8, "rpe": 9, "set_number": 2},
    ]
    current = [
        {"exercise": "PullUps", "variation": "weighted", "variation_id": "weighted", "weight": 15, "reps": 10, "rpe": 9, "set_number": 1},
        {"exercise": "PullUps", "variation": "weighted", "variation_id": "weighted", "weight": 12.5, "reps": 9, "rpe": 9, "set_number": 2},
        {"exercise": "HighRows", "variation": "unilat", "variation_id": "unilat", "execution_mode": "", "weight": 62.5, "reps": 11, "rpe": 8, "set_number": 1},
        {"exercise": "HighRows", "variation": "unilat", "variation_id": "unilat", "execution_mode": "", "weight": 62.5, "reps": 9, "rpe": 8, "set_number": 2},
        {"exercise": "sup. Curls", "variation": "KH", "variation_id": "KH", "weight": 17.5, "reps": 8, "rpe": 9, "set_number": 1},
        {"exercise": "sup. Curls", "variation": "KH", "variation_id": "KH", "weight": 15, "reps": 11, "rpe": 8, "set_number": 2},
        {"exercise": "Cable Crunches", "variation": "SZ", "variation_id": "SZ", "weight": 42.5, "reps": 6, "rpe": 8, "set_number": 1},
        {"exercise": "Cable Crunches", "variation": "SZ", "variation_id": "SZ", "weight": 40, "reps": 10, "rpe": 8, "set_number": 2},
        {"exercise": "Beinbeuger", "variation": "eGym", "variation_id": "eGym", "weight": 70, "reps": 9, "rpe": 9, "set_number": 1},
        {"exercise": "Beinbeuger", "variation": "eGym", "variation_id": "eGym", "weight": 68, "reps": 8, "rpe": 8, "set_number": 2},
    ]
    add_session(conn, 356, "2026-08-25", previous, name="FB Pull B")
    add_session(conn, 360, "2026-08-31", current, name="FB Pull B")

    payload = compute_session_progress(conn, 360)

    assert payload["comparable_sets"] == 10
    assert payload["excluded_sets"] == 0
    assert payload["improved"] == 7
    assert payload["same"] == 2
    assert payload["worse"] == 1
    assert payload["progress_rate"] == pytest.approx(0.7)
    highrows = [item for item in payload["set_comparisons"] if item["exercise_name"] == "highrows"]
    assert len(highrows) == 2
    assert all(item["status"] == "progress" for item in highrows)
    beinbeuger_top = next(
        item
        for item in payload["set_comparisons"]
        if item["exercise_name"] == "beinbeuger" and item["set_slot"] == "set_number:1"
    )
    assert beinbeuger_top["status"] == "progress"
    assert beinbeuger_top["reason_code"] == "successful_rep_progression"


def test_full_history_finds_exercise_more_than_eight_general_workouts_back():
    conn = db()
    add_session(conn, 1, "2026-01-01", [{"exercise": "Squat", "weight": 100, "reps": 5}])
    for wid in range(2, 12):
        add_session(conn, wid, f"2026-01-{wid:02d}", [{"exercise": f"Other {wid}", "weight": 20, "reps": 10}])
    add_session(conn, 12, "2026-01-20", [{"exercise": "Squat", "weight": 102.5, "reps": 5}])
    payload = compute_session_progress(conn, 12)
    assert payload["comparable_sets"] == 1
    assert payload["improved"] == 1


def test_cosmetic_punctuation_change_keeps_same_exercise_history():
    conn = db()
    add_session(conn, 332, "2026-07-28", [
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup. curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 15, "reps": 8, "rpe": 9, "set_number": 1},
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup. curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 12.5, "reps": 8, "rpe": 8, "set_number": 2},
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup. curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 10, "reps": 9, "rpe": 8, "set_number": 3},
    ], name="FB Pull B")
    add_session(conn, 336, "2026-08-03", [
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 17.5, "reps": 5, "rpe": 9, "set_number": 1},
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 15, "reps": 8, "rpe": 9, "set_number": 2},
        {"exercise": "sup. Curls", "canonical_exercise_id": "sup curls", "variation": "KH", "variation_id": "KH", "device": "KH", "weight": 12.5, "reps": 10, "rpe": 8, "set_number": 3},
    ], name="FB Pull B")

    payload = compute_session_progress(conn, 336)

    assert payload["comparable_sets"] == 3
    assert payload["improved"] == 3
    assert payload["new_exercise"] == 0
    assert payload["excluded_sets"] == 0
    assert {item["reference_session_id"] for item in payload["set_comparisons"]} == {332}


def test_one_time_progression_exclusion_keeps_eligible_denominator_and_becomes_baseline():
    conn = db()
    rows = [
        {"exercise": f"Exercise {idx}", "variation": "same", "device": "machine", "weight": 50, "reps": 8}
        for idx in range(7)
    ] + [
        {"exercise": "sup. Curls", "variation": "KH", "device": "KH", "weight": 12.5, "reps": 10},
        {"exercise": "sup. Curls", "variation": "KH", "device": "KH", "weight": 12.5, "reps": 8},
    ]
    add_session(conn, 1, "2026-01-01", rows)
    add_session(conn, 2, "2026-01-08", rows)
    conn.execute(
        "UPDATE sets SET progression_excluded=1 WHERE workout_id=2 AND exercise_id=(SELECT id FROM exercises WHERE workout_id=2 AND name='sup. Curls')"
    )
    conn.commit()

    current = compute_session_progress(conn, 2)
    assert current["total_eligible_sets"] == 9
    assert current["comparable_sets"] == 7
    assert sum(item["category"] == "progression_excluded" for item in current["set_comparisons"]) == 2

    conn.execute("UPDATE workouts SET id=3, date='2026-01-15', date_iso='2026-01-15' WHERE id=2")
    conn.execute("UPDATE exercises SET workout_id=3 WHERE workout_id=2")
    conn.execute("UPDATE sets SET workout_id=3 WHERE workout_id=2")
    conn.execute("UPDATE sets SET progression_excluded=0")
    conn.commit()
    next_session = compute_session_progress(conn, 3)
    assert next_session["comparable_sets"] == 9


def test_latest_execution_beats_older_exact_slot():
    conn = db()
    add_session(conn, 1, "2026-01-01", [
        {"set_number": 1, "weight": 100, "reps": 5},
        {"set_number": 2, "weight": 80, "reps": 10},
    ])
    add_session(conn, 2, "2026-01-08", [{"set_number": 3, "weight": 85, "reps": 8}])
    add_session(conn, 3, "2026-01-15", [{"set_number": 1, "weight": 102.5, "reps": 5}])
    payload = compute_session_progress(conn, 3)
    comparison = payload["set_comparisons"][0]
    assert comparison["match_level"].startswith("same_rank_position")
    assert comparison["reference_set_id"] == 3
    assert comparison["fallback_to_older_execution"] is False


def test_latest_execution_set_number_beats_older_exact_slot():
    conn = db()
    add_session(conn, 1, "2026-01-01", [{"set_number": 1, "weight": 100, "reps": 5}])
    add_session(conn, 2, "2026-01-08", [{"set_number": 2, "weight": 90, "reps": 8}])
    add_session(conn, 3, "2026-01-15", [{"set_number": 1, "weight": 102.5, "reps": 5}])
    comparison = compute_session_progress(conn, 3)["set_comparisons"][0]
    assert comparison["reference_set_id"] == 2
    assert comparison["match_level"].startswith("same_rank_position")
    assert comparison["fallback_to_older_execution"] is False


def test_three_current_sets_against_two_previous_are_one_to_one():
    conn = db()
    add_session(conn, 1, "2026-02-01", [
        {"set_number": 1, "weight": 100, "reps": 5},
        {"set_number": 2, "weight": 90, "reps": 8},
    ])
    add_session(conn, 2, "2026-02-08", [
        {"set_number": 1, "weight": 102.5, "reps": 5},
        {"set_number": 2, "weight": 92.5, "reps": 8},
        {"set_number": 3, "weight": 85, "reps": 10},
    ])
    payload = compute_session_progress(conn, 2)
    assert payload["comparable_sets"] == 2
    assert payload["unmatched_known_exercise"] == 1
    refs = [item["reference_set_id"] for item in payload["set_comparisons"] if item["reference_set_id"]]
    assert len(refs) == len(set(refs)) == 2
    matched = [item for item in payload["set_comparisons"] if item["reference_set_id"]]
    assert all(item["fallback_to_older_execution"] is False for item in matched)
    assert payload["set_comparisons"][-1]["fallback_reason"] == "no_matching_fixed_slot"


def test_older_fallback_is_one_to_one_across_multiple_executions():
    conn = db()
    add_session(conn, 1, "2026-02-01", [{"set_number": 1, "weight": 100, "reps": 5}])
    add_session(conn, 2, "2026-02-08", [{"set_number": 1, "weight": 101, "reps": 5}])
    add_session(conn, 3, "2026-02-15", [
        {"set_number": 1, "weight": 102, "reps": 5},
        {"set_number": 2, "weight": 90, "reps": 8},
        {"set_number": 3, "weight": 80, "reps": 10},
    ])
    payload = compute_session_progress(conn, 3)
    refs = [item["reference_set_id"] for item in payload["set_comparisons"] if item["reference_set_id"]]
    assert len(refs) == len(set(refs)) == 1
    assert sum(item["fallback_to_older_execution"] for item in payload["set_comparisons"]) == 0


def test_coverage_controls_confidence_without_hiding_result():
    conn = db()
    add_session(conn, 1, "2026-03-01", [{"set_number": i, "weight": 100, "reps": 8} for i in range(1, 5)])
    add_session(conn, 2, "2026-03-08", [{"set_number": i, "weight": 102.5, "reps": 8} for i in range(1, 5)])
    four_of_four = compute_session_progress(conn, 2)
    assert four_of_four["comparable_sets"] == 4
    assert four_of_four["progress_rate"] == 1.0
    assert four_of_four["comparison_confidence"] == "high"

    add_session(conn, 3, "2026-03-15", [
        *[{"set_number": i, "weight": 105, "reps": 8} for i in range(1, 5)],
        *[{"exercise": f"New {i}", "set_number": 1, "weight": 20, "reps": 10} for i in range(6)],
    ])
    four_of_ten = compute_session_progress(conn, 3)
    assert four_of_ten["comparable_sets"] == 4
    assert four_of_ten["total_eligible_sets"] == 10
    assert four_of_ten["progress_rate"] is not None
    assert four_of_ten["comparison_confidence"] in {"low", "very_low"}


def test_alias_matches_and_non_variation_metadata_does_not_split_history():
    conn = db()
    add_session(conn, 1, "2026-04-01", [{"exercise": "Bench Press", "device": "barbell", "weight": 100}])
    conn.execute("INSERT INTO exercise_aliases(id,alias,exercise_id) VALUES(1,'Bankdrücken',1)")
    add_session(conn, 2, "2026-04-08", [{"exercise": "Bankdrücken", "device": "barbell", "weight": 102.5}])
    add_session(conn, 3, "2026-04-15", [{"exercise": "Bankdrücken", "device": "smith", "laterality": "unilateral", "execution_mode": "paused", "weight": 110}])
    assert compute_session_progress(conn, 2)["comparable_sets"] == 1
    smith = compute_session_progress(conn, 3)
    assert smith["comparable_sets"] == 1
    assert smith["improved"] == 1
    assert smith["new_exercise"] == 0


def test_inferred_leading_warmup_is_excluded_from_progression_quote():
    conn = db()
    add_session(conn, 1, "2026-04-01", [
        {"set_number": 1, "weight": 40, "reps": 10},
        {"set_number": 2, "weight": 100, "reps": 8},
    ])
    add_session(conn, 2, "2026-04-08", [
        {"set_number": 1, "weight": 40, "reps": 10},
        {"set_number": 2, "weight": 102.5, "reps": 8},
    ])

    payload = compute_session_progress(conn, 2)

    assert payload["total_sets"] == 2
    assert payload["total_eligible_sets"] == 1
    assert payload["comparable_sets"] == 1
    assert payload["excluded_sets"] == 1
    excluded = next(item for item in payload["set_comparisons"] if not item["included"])
    assert excluded["exclusion_reason"] == "inferred_warmup"


def test_leading_set_with_out_of_scheme_reps_is_inferred_warmup():
    conn = db()
    add_session(conn, 1, "2026-04-01", [
        {"set_number": 1, "weight": 90, "reps": 15},
        {"set_number": 2, "weight": 100, "reps": 8},
    ])
    add_session(conn, 2, "2026-04-08", [
        {"set_number": 1, "weight": 90, "reps": 15},
        {"set_number": 2, "weight": 102.5, "reps": 8},
    ])

    payload = compute_session_progress(conn, 2)

    assert payload["total_eligible_sets"] == 1
    assert payload["comparable_sets"] == 1
    assert any(item["exclusion_reason"] == "inferred_warmup" for item in payload["set_comparisons"])


def test_explicit_stable_slots_override_changed_set_numbers():
    conn = db()
    add_session(conn, 1, "2026-04-01", [
        {"set_number": 1, "set_slot": "top_1", "weight": 100, "reps": 8},
        {"set_number": 2, "set_slot": "backoff_1", "weight": 90, "reps": 10},
    ])
    add_session(conn, 2, "2026-04-08", [
        {"set_number": 1, "set_slot": "backoff_1", "weight": 92.5, "reps": 10},
        {"set_number": 2, "set_slot": "top_1", "weight": 102.5, "reps": 8},
    ])

    comparisons = compute_session_progress(conn, 2)["set_comparisons"]

    assert all(item["match_level"].startswith("same_stable_slot") for item in comparisons)
    by_slot = {item["set_slot"]: item for item in comparisons}
    assert by_slot["top_1"]["reference_set_slot"] == "top_1"
    assert by_slot["backoff_1"]["reference_set_slot"] == "backoff_1"


def test_legacy_missing_rpe_is_comparable_and_new_missing_rpe_is_excluded():
    conn = db()
    add_session(conn, 1, "2026-06-01", [{"weight": 100, "reps": 8, "rpe": None}])
    add_session(conn, 2, "2026-06-08", [{"weight": 102.5, "reps": 8, "rpe": None}])
    legacy = compute_session_progress(conn, 2)
    comparison = legacy["set_comparisons"][0]
    assert comparison["comparison_mode"] == "legacy_raw_e1rm"
    assert comparison["status"] == "progress"
    assert legacy["coverage_all_work_sets"] == 1.0
    assert legacy["comparison_success_rate"] == 1.0

    add_session(conn, 3, "2026-08-01", [{"weight": 105, "reps": 8, "rpe": None}])
    new_missing = compute_session_progress(conn, 3)
    assert new_missing["comparable_sets"] == 0
    assert new_missing["exclusion_reasons"] == {"missing_current_rpe": 1}


def test_reference_distance_counts_same_identity_executions_not_global_workouts():
    conn = db()
    add_session(conn, 1, "2026-06-01", [{"exercise": "Bench", "weight": 100, "reps": 8}])
    add_session(conn, 2, "2026-06-02", [{"exercise": "Row", "weight": 80, "reps": 8}])
    add_session(conn, 3, "2026-06-03", [{"exercise": "Squat", "weight": 120, "reps": 8}])
    add_session(conn, 4, "2026-06-08", [{"exercise": "Bench", "weight": 102.5, "reps": 8}])
    comparison = compute_session_progress(conn, 4)["set_comparisons"][0]
    assert comparison["reference_distance_executions"] == 1
    assert comparison["reference_distance_days"] == 7


def test_intentional_deload_and_technique_sets_are_excluded():
    conn = db()
    add_session(conn, 1, "2026-04-01", [
        {"set_number": 1, "weight": 100, "reps": 8},
        {"set_number": 2, "weight": 90, "reps": 10},
    ])
    add_session(conn, 2, "2026-04-08", [
        {"set_number": 1, "weight": 80, "reps": 8, "intentional_deload": True},
        {"set_number": 2, "weight": 70, "reps": 10, "technique_set": True},
    ])

    payload = compute_session_progress(conn, 2)

    assert payload["comparable_sets"] == 0
    assert {item["exclusion_reason"] for item in payload["set_comparisons"]} == {"intentional_deload", "technique_set"}


def test_all_session_consumers_expose_identical_canonical_counts():
    conn = db()
    add_session(conn, 1, "2026-05-01", [{"set_number": i, "weight": 100, "reps": 8} for i in range(1, 5)], name="Pull A")
    add_session(conn, 2, "2026-05-08", [
        {"set_number": 1, "weight": 102.5, "reps": 8},
        {"set_number": 2, "weight": 100, "reps": 8},
        {"set_number": 3, "weight": 97.5, "reps": 8},
        {"set_number": 4, "weight": 100, "reps": 8, "rpe": 7},
    ], name="Pull A")
    detail = compute_session_progress(conn, 2)
    analysis = compute_default_set_progress_series(conn, start_iso="2026-05-08", end_iso="2026-05-08")["points"][0]
    calendar = _load_strength_day_counters(conn, compare_start_iso="2026-05-01", end_iso="2026-05-08", visible_days={"2026-05-08"}, deload_days=set())["2026-05-08"]["sessions"][0]
    gpt = progress_payload_for_gpt(detail)
    assert counts(detail) == counts(analysis) == counts(calendar)
    count_keys = ("improved", "same", "worse", "comparable_sets", "total_eligible_sets", "rule_version")
    assert {key: detail[key] for key in count_keys} == {key: gpt[key] for key in count_keys}
    assert detail["rule_version"] == RULE_VERSION == "progression_rules_v6"
    assert gpt["progression_quote"] == detail["progress_rate"]
    assert gpt["progression_quote_definition"] == "sets_improved / sets_compared"
    assert "Progressionsquote:" in gpt["summary_text"]
    assert "Net progress" not in gpt["summary_text"]
    assert "success_quote" not in gpt
    for competing_rate in (
        "progress_rate",
        "stable_rate",
        "regression_rate",
        "net_progress",
        "coverage",
        "coverage_all_work_sets",
        "comparison_success_rate",
        "comparison_confidence_score",
        "average_match_quality",
    ):
        assert competing_rate not in gpt


def test_gpt_coach_builder_matches_the_other_real_consumer_builders(tmp_path, monkeypatch):
    """Exercise the action-layer coach payload, not another engine invocation."""
    database_path = tmp_path / "training.sqlite3"
    conn = db(str(database_path))
    add_session(conn, 1, "2026-05-01", [{"set_number": 1, "weight": 100, "reps": 8}], name="Pull A")
    add_session(conn, 2, "2026-05-08", [{"set_number": 1, "weight": 102.5, "reps": 8}], name="Pull A")
    conn.close()
    monkeypatch.setattr(connections, "TRAINING_DB", str(database_path))

    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    detail = compute_session_progress(conn, 2)
    analysis = compute_default_set_progress_series(conn, start_iso="2026-05-08", end_iso="2026-05-08")["points"][0]
    calendar = _load_strength_day_counters(
        conn,
        compare_start_iso="2026-05-01",
        end_iso="2026-05-08",
        visible_days={"2026-05-08"},
        deload_days=set(),
    )["2026-05-08"]["sessions"][0]
    conn.close()

    coach = compare_workout_to_last(2)
    assert counts(detail) == counts(analysis) == counts(calendar)
    count_keys = ("improved", "same", "worse", "comparable_sets", "total_eligible_sets", "rule_version")
    assert {key: detail[key] for key in count_keys} == {key: coach[key] for key in count_keys}
    assert coach["available"] is True
    assert coach["rule_version"] == RULE_VERSION


def test_deleted_session_disappears_and_future_payload_recomputes():
    conn = db()
    add_session(conn, 1, "2026-06-01", [{"weight": 100}])
    add_session(conn, 2, "2026-06-08", [{"weight": 105}])
    add_session(conn, 3, "2026-06-15", [{"weight": 102.5}])
    before = compute_session_progress(conn, 3)
    assert before["same"] == 1
    conn.execute("DELETE FROM sets WHERE workout_id=2")
    conn.execute("DELETE FROM exercises WHERE workout_id=2")
    conn.execute("DELETE FROM workouts WHERE id=2")
    conn.commit()
    payloads = compute_progress_payloads(conn)
    assert {item["session_id"] for item in payloads} == {1, 3}
    after = compute_session_progress(conn, 3)
    assert after["improved"] == 1


def test_edit_recomputes_without_stale_derived_values():
    conn = db()
    add_session(conn, 1, "2026-07-01", [{"weight": 100, "reps": 8}])
    add_session(conn, 2, "2026-07-08", [{"weight": 102.5, "reps": 8}])
    assert compute_session_progress(conn, 2)["improved"] == 1
    conn.execute("UPDATE sets SET weight=95 WHERE workout_id=2")
    conn.commit()
    assert compute_session_progress(conn, 2)["worse"] == 1


def test_session_322_uses_latest_execution_and_expected_regressions():
    conn = sqlite3.connect("file:database/training.sqlite3?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    payload = compute_session_progress(conn, 322)
    conn.close()
    assert {key: payload[key] for key in ("total_eligible_sets", "comparable_sets", "improved", "same", "worse")} == {
        "total_eligible_sets": 8,
        "comparable_sets": 8,
        "improved": 6,
        "same": 0,
        "worse": 2,
    }
    worse = {item["exercise_name"] for item in payload["set_comparisons"] if item["status"] == "regress"}
    assert worse == {"breite rows", "hammers"}
