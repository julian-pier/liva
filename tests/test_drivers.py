import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.drivers import spearman_corr, _first_diff, _rankdata_average_ties


class DriversAnalysisTests(unittest.TestCase):
    def test_rank_ties_average(self):
        vals = [10.0, 20.0, 20.0, 30.0]
        ranks = _rankdata_average_ties(vals)
        self.assertEqual(ranks[0], 1.0)
        self.assertEqual(ranks[1], 2.5)
        self.assertEqual(ranks[2], 2.5)
        self.assertEqual(ranks[3], 4.0)

    def test_spearman_positive(self):
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        r = spearman_corr(x, y)
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.99)

    def test_first_diff_nullsafe(self):
        arr = [10.0, 11.0, None, 15.0, 14.5]
        out = _first_diff(arr)
        self.assertEqual(out[0], None)
        self.assertAlmostEqual(out[1], 1.0)
        self.assertEqual(out[2], None)
        self.assertEqual(out[3], None)
        self.assertAlmostEqual(out[4], -0.5)


if __name__ == "__main__":
    unittest.main()
