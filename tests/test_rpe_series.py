import unittest

from app import _rpe_series_for_sets


class RpeSeriesTests(unittest.TestCase):
    def test_progresses_up_across_four_sets(self):
        self.assertEqual(_rpe_series_for_sets(7.5, 9.0, 4), [7.5, 8.0, 8.5, 9.0])

    def test_two_sets_keep_heavier_rpe_last(self):
        self.assertEqual(_rpe_series_for_sets(8.5, 9.0, 2), [8.5, 9.0])

    def test_single_value_stays_constant(self):
        self.assertEqual(_rpe_series_for_sets(8.0, 8.0, 4), [8.0, 8.0, 8.0, 8.0])


if __name__ == "__main__":
    unittest.main()
