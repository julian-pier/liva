import unittest

from analysis.training_set_logic import (
    classify_tb_slots,
    find_last_reference,
    evaluate_plan_status,
    evaluate_simple_status,
)


class TrainingSetLogicTests(unittest.TestCase):
    def test_tb_slot_classification(self):
        sets = [
            {"weight": 100, "reps": 5},
            {"weight": 100, "reps": 4},
            {"weight": 90, "reps": 6},
            {"weight": 85, "reps": 8},
        ]
        slots = classify_tb_slots(sets, pct=0.05, abs_threshold=5)
        self.assertEqual(slots[0].tb_type, "T")
        self.assertEqual(slots[0].tb_slot, 1)
        self.assertEqual(slots[1].tb_type, "T")
        self.assertEqual(slots[1].tb_slot, 2)
        self.assertEqual(slots[2].tb_type, "B")
        self.assertEqual(slots[2].tb_slot, 1)

    def test_plan_corridor_logic(self):
        target = {"reps_min": 5, "reps_max": 8, "rpe_min": 7, "rpe_max": 8}
        status, _ = evaluate_plan_status({"reps": 6, "weight": 80, "rpe": 7.5}, target)
        self.assertEqual(status, "ok")
        status, _ = evaluate_plan_status({"reps": 9, "weight": 80, "rpe": 8}, target)
        self.assertEqual(status, "good")
        status, _ = evaluate_plan_status({"reps": 4, "weight": 80, "rpe": 8}, target)
        self.assertEqual(status, "bad")

    def test_simple_slot_comparison(self):
        last_slots = classify_tb_slots([
            {"weight": 80, "reps": 6, "rpe": 8},
        ])
        last_ref = find_last_reference(last_slots, "T", 1)
        status, _ = evaluate_simple_status({"weight": 82.5, "reps": 6, "rpe": 8}, last_ref, "T")
        self.assertEqual(status, "good")
        status, _ = evaluate_simple_status({"weight": 77.5, "reps": 6, "rpe": 8.5}, last_ref, "T")
        self.assertEqual(status, "bad")

    def test_last_reference_fallback(self):
        last_slots = classify_tb_slots([
            {"weight": 100, "reps": 5},
            {"weight": 90, "reps": 6},
        ])
        # T1 exists, T2 missing -> fallback to T1
        ref = find_last_reference(last_slots, "T", 2)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.tb_slot, 1)

    def test_backoff_fallback_prefers_lower_slot(self):
        last_slots = classify_tb_slots([
            {"weight": 100, "reps": 5},
            {"weight": 90, "reps": 6},
            {"weight": 85, "reps": 8},
        ])
        # Backoffs are B1 (90) and B2 (85); ask for B3 -> fallback to B2
        ref = find_last_reference(last_slots, "B", 3)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.tb_slot, 2)


if __name__ == '__main__':
    unittest.main()
