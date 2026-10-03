import json
import sqlite3
import unittest
from datetime import date

from plans import plans_api


def create_plans_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE plans (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            data TEXT NOT NULL,
            block_length INTEGER NOT NULL DEFAULT 4,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            start_date TEXT NULL,
            end_date TEXT NULL,
            total_weeks INTEGER NULL
        )
    """)
    conn.commit()
    return conn


def insert_plan(conn, *, plan_id, name, start_date=None, end_date=None, total_weeks=None, is_active=0, updated_at='2026-01-01'):
    plan = {
        "name": name,
        "block_length": 4,
        "meta": {
            "start_date": start_date,
            "end_date": end_date,
            "total_weeks": total_weeks,
        },
        "base_week": [
            {
                "session_name": "Upper A",
                "strength_exercises": [
                    {"exercise_name": "Bench", "variation": "LH", "sets": 3}
                ],
            }
        ],
        "weeks": [],
    }
    conn.execute(
        """
        INSERT INTO plans (id, name, data, block_length, is_active, created_at, updated_at, start_date, end_date, total_weeks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            name,
            json.dumps(plan),
            4,
            is_active,
            updated_at,
            updated_at,
            start_date,
            end_date,
            total_weeks,
        ),
    )
    conn.commit()


class PlansActiveTests(unittest.TestCase):
    def test_selects_plan_by_date_range(self):
        conn = create_plans_db()
        insert_plan(conn, plan_id=1, name='Plan A', start_date='2026-01-01', end_date='2026-01-31', is_active=0)
        payload = plans_api.resolve_active_plan_payload(conn, date(2026, 1, 15), 'Upper A')
        self.assertEqual(payload.get('plan_id'), 1)
        conn.close()

    def test_selects_latest_start_date(self):
        conn = create_plans_db()
        insert_plan(conn, plan_id=1, name='Plan A', start_date='2026-01-01', end_date='2026-03-01', is_active=0)
        insert_plan(conn, plan_id=2, name='Plan B', start_date='2026-01-15', end_date='2026-03-01', is_active=0, updated_at='2026-01-20')
        payload = plans_api.resolve_active_plan_payload(conn, date(2026, 1, 20), 'Upper A')
        self.assertEqual(payload.get('plan_id'), 2)
        conn.close()

    def test_returns_null_when_outside_range(self):
        conn = create_plans_db()
        insert_plan(conn, plan_id=1, name='Plan A', start_date='2025-12-27', end_date='2026-01-15', is_active=1)
        payload = plans_api.resolve_active_plan_payload(conn, date(2025, 5, 1), 'Upper A')
        self.assertIsNone(payload.get('plan_id'))
        conn.close()


if __name__ == '__main__':
    unittest.main()
