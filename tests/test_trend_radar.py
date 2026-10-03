import unittest
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.radar import compute_windows, build_flags


class TrendRadarTests(unittest.TestCase):
    def test_compute_windows_delta_and_direction(self):
        series = {
            "weight": {
                "2026-01-01": 80.0,
                "2026-01-02": 80.0,
                "2026-01-03": 80.0,
                "2026-01-04": 80.0,
                "2026-01-05": 80.0,
                "2026-01-06": 84.0,
                "2026-01-07": 84.0,
            }
        }
        out = compute_windows(series, end_date=date(2026, 1, 7), baseline_days=7, trend_days=3)
        w = out["weight"]
        self.assertEqual(w["direction"], "up")
        self.assertIsNotNone(w["delta_pct"])
        self.assertGreater(w["delta_pct"], 0.01)

    def test_compute_windows_handles_missing(self):
        series = {"rmssd": {"2026-01-01": None, "2026-01-02": None}}
        out = compute_windows(series, end_date=date(2026, 1, 2), baseline_days=2, trend_days=2)
        r = out["rmssd"]
        self.assertIsNone(r["value_7d"])
        self.assertIsNone(r["value_baseline"])
        self.assertIsNone(r["delta_pct"])
        self.assertFalse(r["baseline_ok"])
        self.assertEqual(r["direction"], "na")

    def test_build_flags_infekt_pattern(self):
        chips = [
            {"key": "rmssd", "delta_pct": -0.18},
            {"key": "rhr", "delta_pct": 0.09},
            {"key": "kcal", "delta_pct": -0.02},
            {"key": "weight", "delta_pct": 0.00},
            {"key": "volume", "delta_pct": 0.00},
            {"key": "cardio", "delta_pct": 0.00},
        ]
        flags = build_flags(chips)
        self.assertTrue(any(f["id"] == "infekt_overreach_pattern" for f in flags))
        top = flags[0]
        self.assertIn(top["level"], {"danger", "warn", "info"})


if __name__ == "__main__":
    unittest.main()
