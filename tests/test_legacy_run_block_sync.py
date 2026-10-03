import unittest

from app import _legacy_plan_from_gym_record


def _base_record(active_week: str):
    return {
        "id": 123,
        "title": "Testplan",
        "focus": "",
        "rules_json": {"active_week": active_week, "weeks": {}},
        "blocks_json": {
            "Run Block (6 Wochen)": [
                {
                    "day": "So",
                    "title": "Run - Quality",
                    "frequency": 1,
                    "lines": [
                        "Woche 1: Easy 25 min @Puls 150–165 bpm",
                        "Woche 2: Threshold 2x8 min @Puls 170–178 bpm (Pause: 3 min Easy @Puls 150–165)",
                    ],
                }
            ]
        },
        "plan_json": {
            "meta": {"total_weeks": 6, "block_length": 4, "start_date": "2026-02-16"},
            "weeks": 6,
            "days": [
                {"day": "Mo", "events": []},
                {"day": "Di", "events": []},
                {"day": "Mi", "events": []},
                {"day": "Do", "events": []},
                {"day": "Fr", "events": []},
                {"day": "Sa", "events": []},
                {
                    "day": "So",
                    "events": [
                        {
                            "kind": "run",
                            "title": "Quality",
                            "items": [
                                {
                                    "kind": "ref_block",
                                    "block_name": "Run Block (6 Wochen)",
                                    "block_day": "So",
                                    "display_name": "",
                                    "variation": "",
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    }


class LegacyRunBlockSyncTests(unittest.TestCase):
    def test_active_week_one_quality_is_easy_z2(self):
        legacy = _legacy_plan_from_gym_record(_base_record("W1"))
        sunday = legacy["base_week"][6]
        run = sunday["run_sessions"][0]
        self.assertEqual(run["run_type"], "z2")
        self.assertEqual(run["amount_value"], "25")
        self.assertIn("Easy 25 min", run["notes"])

    def test_active_week_two_quality_is_threshold(self):
        legacy = _legacy_plan_from_gym_record(_base_record("W2"))
        sunday = legacy["base_week"][6]
        run = sunday["run_sessions"][0]
        self.assertEqual(run["run_type"], "threshold")
        self.assertIn("Threshold 2x8 min", run["notes"])


if __name__ == "__main__":
    unittest.main()
