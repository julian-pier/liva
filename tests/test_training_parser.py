import sqlite3
import unittest
from unittest.mock import patch

from analysis import training_parser as parser


def create_training_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY,
            date_iso TEXT,
            name TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY,
            workout_id INTEGER,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY,
            exercise_id INTEGER,
            weight REAL,
            reps INTEGER,
            rpe REAL
        )
    """)
    conn.commit()
    return conn


def populate_history(conn):
    cur = conn.cursor()
    cur.execute("INSERT INTO workouts (id, date_iso, name) VALUES (1, '2026-01-01', 'Upper A')")
    cur.execute("INSERT INTO exercises (id, workout_id, name, variation) VALUES (1, 1, 'Bench', 'LH')")
    cur.executemany(
        "INSERT INTO sets (exercise_id, weight, reps, rpe) VALUES (1, ?, ?, ?)",
        [(80, 5, 8), (82.5, 5, 8), (77.5, 6, 8)],
    )
    cur.execute("INSERT INTO workouts (id, date_iso, name) VALUES (2, '2026-01-08', 'Upper A')")
    cur.execute("INSERT INTO exercises (id, workout_id, name, variation) VALUES (2, 2, 'Bench', 'Smith')")
    cur.executemany(
        "INSERT INTO sets (exercise_id, weight, reps, rpe) VALUES (2, ?, ?, ?)",
        [(60, 6, 8), (62.5, 5, 8)],
    )
    conn.commit()


def build_draft_payload(variation, weight):
    return {
        'draft': {
            'exercises': [
                {
                    'name': 'Bench',
                    'variation': variation,
                    'notes': '',
                    'sets': [
                        {'reps': 5, 'weight': weight, 'rpe': 8},
                        {'reps': 5, 'weight': 77.5, 'rpe': 8},
                    ],
                }
            ]
        },
        'session': {
            'plan_id': 1,
            'session_name': 'Upper A',
            'date_iso': '2026-02-01'
        }
    }


class TrainingParserTests(unittest.TestCase):

    def test_normalized_training_text_round_trips_structured_session(self):
        structured = [
            {
                "name": "Bench",
                "variation": "Smith",
                "sets": [
                    {"reps": 8, "weight": 80, "rpe": 8},
                    {"reps": 10, "weight": 72.5, "rpe": None},
                ],
            }
        ]
        text = parser.normalized_training_text(structured)
        exercises, _, warnings = parser.parse_training_text(text)
        self.assertTrue(text)
        self.assertEqual(exercises[0]["name"], "Bench")
        self.assertEqual(exercises[0]["variation"], "Smith")
        self.assertEqual(exercises[0]["sets"][1]["weight"], 72.5)
        self.assertEqual(warnings, [])

    def test_parser_handles_decimals_and_notes(self):
        text = 'Bench\n6x80@8 4x80@9 8x72,5@8\n"Schulter tut weh"\n\nLatzug\n9x105@8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(len(exercises), 2)
        self.assertEqual(exercises[0]['sets'][2]['weight'], 72.5)
        self.assertEqual(exercises[0]['notes'], 'Schulter tut weh')
        self.assertEqual(warnings, [])

    def test_parser_keeps_reps_before_x_and_exact_decimal_weight(self):
        text = 'Seitheben\n10x22,5@8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['sets'][0]['reps'], 10)
        self.assertEqual(exercises[0]['sets'][0]['weight'], 22.5)
        self.assertEqual(exercises[0]['sets'][0]['rpe'], 8)
        self.assertEqual(warnings, [])

    def test_parser_rejects_decimal_value_before_x(self):
        text = 'Seitheben\n22,5x10@8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['sets'], [])

    def test_parser_handles_raw_ocr_colon_and_comma_rpe(self):
        text = (
            'Schrägbank: reverse grip versucht, Handgelenk tut weh, Brustgefühl gut ~80\n'
            'Flys: 1. 9x72,8 2. 9x65,8\n'
            'OHP: 1. 7x45,8 2. 6x40,8\n'
            'Seitheben: 1. 5x47,8 2. 6x43,9\n'
            'Pushdowns: 1. 7x50,8'
        )
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['name'], 'Schrägbank')
        self.assertIn('reverse grip', exercises[0]['notes'])
        self.assertEqual(exercises[1]['name'], 'Flys')
        self.assertEqual(exercises[1]['sets'][0]['reps'], 9)
        self.assertEqual(exercises[1]['sets'][0]['weight'], 72)
        self.assertEqual(exercises[1]['sets'][0]['rpe'], 8)
        self.assertEqual(exercises[1]['sets'][1]['weight'], 65)
        self.assertEqual(exercises[3]['sets'][1]['weight'], 43)
        self.assertEqual(exercises[3]['sets'][1]['rpe'], 9)
        self.assertEqual(exercises[4]['sets'][0]['weight'], 50)
        self.assertEqual(warnings, [])

    def test_parser_keeps_gpt_repeated_dot_rpe_lines_as_real_sets(self):
        text = (
            'Flys: 1. 9x42,8 2. 9x65,8\n'
            '9x42.8 9x65.8\n\n'
            'OHP: 1. 7x45,8 2. 6x40,8\n'
            '7x45.8 6x40.8\n\n'
            'Seitheben: 1. 5x47,8 2. 6x43,9\n'
            '5x47.8 6x43.9\n\n'
            'Pushdowns: 1. 7x50,8\n'
            '7x50.8'
        )
        exercises, notes, warnings = parser.parse_training_text(text)
        by_name = {ex['name']: ex for ex in exercises}
        self.assertEqual(len(by_name['Flys']['sets']), 4)
        self.assertEqual(by_name['Flys']['sets'][0]['weight'], 42)
        self.assertEqual(by_name['Flys']['sets'][0]['rpe'], 8)
        self.assertEqual(by_name['Flys']['sets'][2]['weight'], 42)
        self.assertEqual(by_name['OHP']['sets'][3]['weight'], 40)
        self.assertEqual(by_name['OHP']['sets'][3]['rpe'], 8)
        self.assertEqual(len(by_name['Seitheben']['sets']), 4)
        self.assertEqual(by_name['Seitheben']['sets'][3]['weight'], 43)
        self.assertEqual(by_name['Seitheben']['sets'][3]['rpe'], 9)
        self.assertEqual(len(by_name['Pushdowns']['sets']), 2)
        self.assertEqual(by_name['Pushdowns']['sets'][0]['weight'], 50)
        self.assertEqual(by_name['Pushdowns']['sets'][0]['rpe'], 8)
        self.assertEqual(warnings, [])

    def test_parser_strips_trailing_colon_from_standalone_headers(self):
        text = 'Cable Crunches:\n8x12,5\n7x12,5\n\nFlys:\n9x42,8 9x65,8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['name'], 'Cable Crunches')
        self.assertEqual(exercises[0]['sets'][0]['reps'], 8)
        self.assertEqual(exercises[0]['sets'][0]['weight'], 12.5)
        self.assertEqual(exercises[1]['name'], 'Flys')
        self.assertEqual(exercises[1]['sets'][0]['weight'], 42)
        self.assertEqual(exercises[1]['sets'][0]['rpe'], 8)
        self.assertEqual(warnings, [])

    def test_parser_splits_ocr_weight_dot_rpe_notation(self):
        text = (
            'Wadenheben\n'
            '8x125.9 7x120.9\n\n'
            'Squats\n'
            '7x80.8\n\n'
            'Adduktoren\n'
            '10x68.9 7x65.9\n\n'
            'Cable Crunches\n'
            '10x17.5'
        )
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['name'], 'Wadenheben')
        self.assertEqual(exercises[0]['sets'][0]['weight'], 125)
        self.assertEqual(exercises[0]['sets'][0]['rpe'], 9)
        self.assertEqual(exercises[0]['sets'][1]['weight'], 120)
        self.assertEqual(exercises[0]['sets'][1]['rpe'], 9)
        self.assertEqual(exercises[1]['sets'][0]['weight'], 80)
        self.assertEqual(exercises[1]['sets'][0]['rpe'], 8)
        self.assertEqual(exercises[2]['sets'][0]['weight'], 68)
        self.assertEqual(exercises[2]['sets'][0]['rpe'], 9)
        self.assertEqual(exercises[3]['sets'][0]['weight'], 17.5)
        self.assertIsNone(exercises[3]['sets'][0]['rpe'])
        self.assertEqual(warnings, [])

    def test_parser_handles_numbered_semicolon_shortcuts_and_positive_loads(self):
        text = (
            'Schrägbank:\n'
            '1. 10x35;8 2. 7x35;9\n\n'
            'Seitheben:\n'
            '1. 7x22,5;8 2. 11x17,5;8\n\n'
            'PullUps:\n'
            '1. 7x+15;8 2. 7x+12,5;9'
        )
        exercises, notes, warnings = parser.parse_training_text(text)

        self.assertEqual(
            [(s['reps'], s['weight'], s['rpe']) for s in exercises[0]['sets']],
            [(10, 35, 8), (7, 35, 9)],
        )
        self.assertEqual(
            [(s['reps'], s['weight'], s['rpe']) for s in exercises[1]['sets']],
            [(7, 22.5, 8), (11, 17.5, 8)],
        )
        self.assertEqual(
            [(s['reps'], s['weight'], s['rpe']) for s in exercises[2]['sets']],
            [(7, 15, 8), (7, 12.5, 9)],
        )
        self.assertEqual(notes, '')
        self.assertEqual(warnings, [])

    def test_parser_warns_unknown_lines(self):
        text = 'Bench\n6x80@8 leftover\n9x90@8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertTrue(any('leftover' in w for w in warnings))

    def test_parser_keeps_explicit_variation_in_header_text_for_resolver(self):
        text = 'Curls KH\n10x14@8'
        exercises, notes, warnings = parser.parse_training_text(text)
        self.assertEqual(exercises[0]['name'], 'Curls KH')
        self.assertEqual(exercises[0]['variation'], '')
        self.assertEqual(exercises[0]['sets'][0]['weight'], 14)
        self.assertEqual(warnings, [])

    def test_classify_top_backoff_decisions(self):
        sets = [{'weight': 40}, {'weight': 42}, {'weight': 38}]
        classified = parser.classify_top_backoff(sets, pct_threshold=0.05, abs_threshold=2)
        self.assertTrue(classified[0]['is_top'])
        self.assertTrue(classified[1]['is_top'])
        self.assertFalse(classified[2]['is_top'])
        self.assertTrue(classified[2]['is_backoff'])

    def test_classify_single_weight_all_top(self):
        sets = [{'weight': 50}]
        classified = parser.classify_top_backoff(sets, pct_threshold=0.05, abs_threshold=5)
        self.assertTrue(classified[0]['is_top'])
        self.assertFalse(classified[0]['is_backoff'])

    def test_validate_draft_plan_load_match(self):
        conn = create_training_db()
        populate_history(conn)
        plan = {
            'plan_id': 1,
            'plan_name': 'Test',
            'session_name': 'Upper A',
            'exercises': [
                {'exercise_name': 'Bench', 'variation': 'LH', 'sets': 3}
            ]
        }
        with patch.object(parser, 'load_plan_context', return_value=plan):
            result = parser.validate_draft(
                conn,
                session_info={'plan_id': 1, 'session_name': 'Upper A'},
                structured=build_draft_payload('LH', 82.5)['draft'],
                raw_text='',
                thresholds={}
            )
        meta = result['draft']['exercises'][0]['meta']
        self.assertEqual(meta['confidence'], 'high')
        self.assertTrue(meta['plan_match'])
        conn.close()

    def test_validate_draft_plan_conflict(self):
        conn = create_training_db()
        populate_history(conn)
        plan = {
            'plan_id': 1,
            'plan_name': 'Test',
            'session_name': 'Upper A',
            'exercises': [
                {'exercise_name': 'Bench', 'variation': 'Smith', 'sets': 3}
            ]
        }
        with patch.object(parser, 'load_plan_context', return_value=plan):
            result = parser.validate_draft(
                conn,
                session_info={'plan_id': 1, 'session_name': 'Upper A'},
                structured=build_draft_payload('LH', 82.5)['draft'],
                raw_text='',
                thresholds={}
            )
        meta = result['draft']['exercises'][0]['meta']
        self.assertEqual(meta['confidence'], 'warn')
        self.assertTrue(meta['requires_confirmation'])
        conn.close()

    def test_validate_draft_no_plan_load_detected(self):
        conn = create_training_db()
        populate_history(conn)
        with patch.object(parser, 'load_plan_context', return_value=None):
            result = parser.validate_draft(
                conn,
                session_info={'plan_id': None, 'session_name': 'Upper A'},
                structured=build_draft_payload('LH', 82.5)['draft'],
                raw_text='',
                thresholds={}
            )
        meta = result['draft']['exercises'][0]['meta']
        self.assertEqual(meta['confidence'], 'medium')
        self.assertTrue(meta['load_match'])
        conn.close()


if __name__ == '__main__':
    unittest.main()
