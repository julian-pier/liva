import sqlite3
import unittest

from analysis.training_analysis import compute_default_set_progress_series


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE workouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL,
          date_iso TEXT NOT NULL,
          name TEXT NOT NULL,
          notes TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE exercises (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workout_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          variation TEXT,
          device TEXT,
          laterality TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE sets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          exercise_id INTEGER NOT NULL,
          workout_id INTEGER NOT NULL,
          set_number INTEGER,
          weight REAL,
          reps INTEGER,
          rpe REAL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _add_set(conn: sqlite3.Connection, *, date_iso: str, exercise: str, variation: str = "", device: str = "", laterality: str = "", set_number: int = 1, weight: float = 0.0, reps: int = 0, rpe: float | None = 8.0) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO workouts(date, date_iso, name, notes, created_at) VALUES(?,?,?,?,?)",
        (date_iso, date_iso, "A", "", "2026-01-01T00:00:00"),
    )
    wid = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO exercises(workout_id, name, variation, device, laterality, created_at) VALUES(?,?,?,?,?,?)",
        (wid, exercise, variation, device, laterality, "2026-01-01T00:00:00"),
    )
    eid = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO sets(exercise_id, workout_id, set_number, weight, reps, rpe, created_at) VALUES(?,?,?,?,?,?,?)",
        (eid, wid, set_number, weight, reps, rpe, "2026-01-01T00:00:00"),
    )
    conn.commit()


def _add_session(conn: sqlite3.Connection, *, date_iso: str, name: str, sets: list[dict]) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO workouts(date, date_iso, name, notes, created_at) VALUES(?,?,?,?,?)",
        (date_iso, date_iso, name, "", "2026-01-01T00:00:00"),
    )
    wid = int(cur.lastrowid)
    for s in sets:
        cur.execute(
            "INSERT INTO exercises(workout_id, name, variation, device, laterality, created_at) VALUES(?,?,?,?,?,?)",
            (
                wid,
                s.get("exercise", ""),
                s.get("variation", ""),
                s.get("device", ""),
                s.get("laterality", ""),
                "2026-01-01T00:00:00",
            ),
        )
        eid = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO sets(exercise_id, workout_id, set_number, weight, reps, rpe, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                eid,
                wid,
                int(s.get("set_number", 1)),
                float(s.get("weight", 0.0)),
                int(s.get("reps", 0)),
                s.get("rpe", 8.0),
                "2026-01-01T00:00:00",
            ),
        )
    conn.commit()


def _totals(points: list[dict]) -> dict:
    out = {"improved": 0, "same": 0, "worse": 0, "new": 0, "comparable": 0, "total": 0}
    for p in points:
        out["improved"] += int(p.get("improved") or 0)
        out["same"] += int(p.get("same") or 0)
        out["worse"] += int(p.get("worse") or 0)
        out["new"] += int(p.get("new") or 0)
        out["comparable"] += int(p.get("comparable") or 0)
        out["total"] += int(p.get("total") or 0)
    return out


class AnalyseDefaultProgressTests(unittest.TestCase):
    def test_normal_progress_case(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-01-01", exercise="Squat", weight=100, reps=5, rpe=8.0)
        _add_set(conn, date_iso="2026-01-08", exercise="Squat", weight=102.5, reps=5, rpe=8.0)
        _add_set(conn, date_iso="2026-01-15", exercise="Squat", weight=102.5, reps=6, rpe=8.0)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-01-08",
            end_iso="2026-01-21",
            trend_window_sessions=6,
        )
        t = _totals(payload["points"])
        self.assertEqual(t["improved"], 2)
        self.assertEqual(t["comparable"], 2)
        self.assertEqual(t["new"], 0)
        self.assertEqual(len(payload["points"]), 2)
        self.assertFalse(payload["meta"]["plan_change_flag"])
        self.assertAlmostEqual(float(payload["meta"]["coverage"]), 1.0, places=6)
        self.assertTrue(all(isinstance(p.get("t"), int) for p in payload["points"]))
        self.assertTrue(all(isinstance(p.get("t_iso"), str) for p in payload["points"]))
        self.assertGreater(payload["points"][-1]["t"], payload["points"][0]["t"])

    def test_full_plan_change_comparable_zero(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-02-01", exercise="A", weight=50, reps=8)
        _add_set(conn, date_iso="2026-02-02", exercise="B", weight=55, reps=8)
        _add_set(conn, date_iso="2026-02-03", exercise="C", weight=60, reps=8)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-02-01",
            end_iso="2026-02-03",
            trend_window_sessions=6,
        )
        t = _totals(payload["points"])
        self.assertEqual(t["comparable"], 0)
        self.assertEqual(t["new"], 3)
        self.assertEqual(len(payload["points"]), 3)
        self.assertTrue(payload["meta"]["plan_change_flag"])
        self.assertEqual(payload["meta"]["coverage"], 0.0)
        self.assertTrue(all(p["progress_rate"] is None for p in payload["points"]))
        self.assertTrue(all(p["trend"] is None for p in payload["points"]))

    def test_mixed_high_new_share(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-01-01", exercise="Squat", weight=100, reps=5)
        _add_set(conn, date_iso="2026-01-10", exercise="Squat", weight=101, reps=5)  # improved
        _add_set(conn, date_iso="2026-01-11", exercise="Squat", weight=101, reps=5)  # same
        _add_set(conn, date_iso="2026-01-11", exercise="New1", weight=40, reps=10)
        _add_set(conn, date_iso="2026-01-11", exercise="New2", weight=45, reps=10)
        _add_set(conn, date_iso="2026-01-11", exercise="New3", weight=50, reps=10)
        _add_set(conn, date_iso="2026-01-11", exercise="New4", weight=55, reps=10)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-01-10",
            end_iso="2026-01-12",
            trend_window_sessions=6,
        )
        t = _totals(payload["points"])
        self.assertEqual(t["improved"], 1)
        self.assertEqual(t["same"], 1)
        self.assertEqual(t["comparable"], 2)
        self.assertEqual(t["new"], 4)
        self.assertEqual(t["total"], 6)
        self.assertTrue(payload["meta"]["plan_change_flag"])
        self.assertAlmostEqual(float(payload["meta"]["coverage"]), 2.0 / 6.0, places=6)

    def test_matching_fallback_excludes_variation_change(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-03-01", exercise="Bench", variation="flat", device="barbell", laterality="bilateral", set_number=1, weight=80, reps=6)
        # variation/device differ -> not comparable.
        _add_set(conn, date_iso="2026-03-08", exercise="Bench", variation="pause", device="machine", laterality="bilateral", set_number=1, weight=82.5, reps=6)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-03-08",
            end_iso="2026-03-08",
            trend_window_sessions=6,
        )
        self.assertEqual(len(payload["points"]), 1)
        p = payload["points"][0]
        self.assertEqual(int(p["comparable"]), 0)
        self.assertEqual(int(p["improved"]), 0)
        self.assertTrue(bool(p.get("no_baseline")))

    def test_lookback_finds_baseline_three_sessions_back(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-06-01", exercise="Squat", set_number=1, weight=100, reps=5, laterality="bilateral")
        _add_set(conn, date_iso="2026-06-02", exercise="Bench", set_number=1, weight=80, reps=6, laterality="bilateral")
        _add_set(conn, date_iso="2026-06-03", exercise="Row", set_number=1, weight=70, reps=8, laterality="bilateral")
        _add_set(conn, date_iso="2026-06-04", exercise="Squat", set_number=1, weight=102.5, reps=5, laterality="bilateral")

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-06-04",
            end_iso="2026-06-04",
            baseline_lookback_sessions=3,
            trend_window_sessions=6,
            debug=True,
        )
        self.assertEqual(len(payload["points"]), 1)
        p = payload["points"][0]
        self.assertEqual(int(p["comparable"]), 1)
        self.assertEqual(int(p["improved"]), 1)
        self.assertFalse(bool(p.get("no_baseline")))
        dbg = payload["meta"]["debug"]
        self.assertIn("0", {str(k) for k in (dbg.get("lookback_histogram") or {}).keys()})

    def test_lookback_never_seen_exercise_stays_new(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-07-01", exercise="Bench", set_number=1, weight=80, reps=6)
        _add_set(conn, date_iso="2026-07-02", exercise="Row", set_number=1, weight=70, reps=8)
        _add_set(conn, date_iso="2026-07-03", exercise="Deadlift", set_number=1, weight=140, reps=4)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-07-03",
            end_iso="2026-07-03",
            baseline_lookback_sessions=8,
            trend_window_sessions=6,
        )
        self.assertEqual(len(payload["points"]), 1)
        p = payload["points"][0]
        self.assertEqual(int(p["comparable"]), 0)
        self.assertEqual(int(p["new"]), 1)
        self.assertTrue(bool(p.get("no_baseline")))

    def test_exercise_history_matches_across_different_workout_names(self):
        conn = _mk_conn()
        _add_session(conn, date_iso="2026-10-01", name="Lower A", sets=[
            {"exercise": "RDL", "set_number": 1, "weight": 100, "reps": 8},
            {"exercise": "RDL", "set_number": 2, "weight": 100, "reps": 8},
        ])
        _add_session(conn, date_iso="2026-10-05", name="Lower B", sets=[
            {"exercise": "Leg Curl", "set_number": 1, "weight": 40, "reps": 12},
        ])
        _add_session(conn, date_iso="2026-10-08", name="Lower B", sets=[
            {"exercise": "RDL", "set_number": 1, "weight": 102.5, "reps": 8},
            {"exercise": "RDL", "set_number": 2, "weight": 102.5, "reps": 8},
        ])

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-10-08",
            end_iso="2026-10-08",
            baseline_lookback_sessions=8,
            min_comparable_per_session=1,
        )
        p = payload["points"][0]
        self.assertEqual(int(p["sets_eligible_current"]), 2)
        self.assertEqual(int(p["sets_compared"]), 2)
        self.assertEqual(int(p["improved"]), 2)
        self.assertEqual(int(p["sets_unmatched"]), 0)
        self.assertEqual(int(p["comparison_coverage_pct"]), 100)

    def test_extra_current_sets_show_unmatched_reason_and_coverage(self):
        conn = _mk_conn()
        _add_session(conn, date_iso="2026-11-01", name="Pull", sets=[
            {"exercise": "Row", "set_number": 1, "weight": 70, "reps": 8},
            {"exercise": "Row", "set_number": 2, "weight": 70, "reps": 8},
        ])
        _add_session(conn, date_iso="2026-11-08", name="Pull", sets=[
            {"exercise": "Row", "set_number": 1, "weight": 72.5, "reps": 8},
            {"exercise": "Row", "set_number": 2, "weight": 72.5, "reps": 8},
            {"exercise": "Row", "set_number": 3, "weight": 70, "reps": 8},
        ])

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-11-08",
            end_iso="2026-11-08",
            baseline_lookback_sessions=8,
            min_comparable_per_session=1,
        )
        p = payload["points"][0]
        self.assertEqual(int(p["sets_total_current"]), 3)
        self.assertEqual(int(p["sets_eligible_current"]), 3)
        self.assertEqual(int(p["sets_compared"]), 2)
        self.assertEqual(int(p["sets_unmatched"]), 1)
        self.assertEqual(int(p["comparison_coverage_pct"]), 67)
        self.assertTrue(any(item["reason"] == "missing_reference" for item in p["unmatched_reasons"]))

    def test_low_sample_session_rate_remains_visible_with_confidence(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-08-01", exercise="Bench", set_number=1, weight=80, reps=6)
        _add_set(conn, date_iso="2026-08-01", exercise="Row", set_number=1, weight=70, reps=8)
        _add_set(conn, date_iso="2026-08-02", exercise="Bench", set_number=1, weight=82.5, reps=6)
        _add_set(conn, date_iso="2026-08-02", exercise="Row", set_number=1, weight=70, reps=8)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-08-02",
            end_iso="2026-08-02",
            min_comparable_per_session=8,
        )
        self.assertEqual(len(payload["points"]), 2)
        p = payload["points"][-1]
        self.assertEqual(int(p["comparable"]), 1)
        self.assertIsNotNone(p["progress_rate"])
        self.assertIsNotNone(p["net_progress"])
        self.assertTrue(bool(p.get("low_comparable")))
        self.assertEqual(p["comparison_confidence"], "low")
        self.assertNotEqual(p["status"], "not_enough_data")
        self.assertEqual(int(payload["meta"]["low_comparable_sessions_count"]), 2)

    def test_weighted_trend_uses_sqrt_comparable_weight(self):
        conn = _mk_conn()
        # Base session establishes references for set_number 1..9
        _add_session(conn, date_iso="2026-09-01", name="Base", sets=[
            {"exercise": "Bench", "set_number": i, "weight": 80, "reps": 6}
            for i in range(1, 10)
        ])
        # Session A: only 1 comparable, improved => progress=1.0
        _add_session(conn, date_iso="2026-09-02", name="A", sets=[
            {"exercise": "Bench", "set_number": 1, "weight": 82.5, "reps": 6}
        ])
        # Session B: 9 comparables, no improvement => progress=0.0
        _add_session(conn, date_iso="2026-09-03", name="B", sets=[
            {"exercise": "Bench", "set_number": i, "weight": 80, "reps": 6}
            for i in range(1, 10)
        ])

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-09-02",
            end_iso="2026-09-03",
            trend_window_sessions=2,
            min_comparable_per_session=1,
            baseline_lookback_sessions=8,
        )
        self.assertEqual(len(payload["points"]), 2)
        p_last = payload["points"][-1]
        # Surplus current sets may use older executions only as one-to-one fallbacks.
        self.assertEqual(int(p_last["sets_compared"]), 9)
        self.assertEqual(int(p_last["sets_unmatched"]), 0)
        self.assertAlmostEqual(float(p_last["trend"]), 0.25, places=6)
        self.assertEqual(payload["meta"].get("trend_weight"), "sqrt(comparable)")

    def test_debug_meta_exposes_empty_comparable_state(self):
        conn = _mk_conn()
        _add_set(conn, date_iso="2026-04-01", exercise="X1", weight=40, reps=10)
        _add_set(conn, date_iso="2026-04-02", exercise="X2", weight=42, reps=10)
        _add_set(conn, date_iso="2026-04-03", exercise="X3", weight=44, reps=10)

        payload = compute_default_set_progress_series(
            conn,
            start_iso="2026-04-01",
            end_iso="2026-04-03",
            trend_window_sessions=6,
            debug=True,
        )
        dbg = payload["meta"]["debug"]
        self.assertEqual(int(dbg["total_sessions_found"]), 3)
        self.assertEqual(int(dbg["points_count"]), 3)
        self.assertEqual(int(dbg["comparable_sessions_count"]), 0)
        self.assertEqual(int(dbg["new_only_sessions_count"]), 3)
        self.assertIsNotNone(dbg["first_session_ts"])
        self.assertIsNotNone(dbg["last_session_ts"])
        self.assertTrue(all(bool(p.get("no_baseline")) for p in payload["points"]))

if __name__ == "__main__":
    unittest.main()
