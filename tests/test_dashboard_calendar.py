import sqlite3
from datetime import date

from analysis.dashboard_calendar import build_calendar_month_payload, build_calendar_day_payload


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_calendar_month_payload_handles_missing_tables():
    tconn = _conn()
    rconn = _conn()
    hconn = _conn()
    try:
        payload = build_calendar_month_payload(
            training_conn=tconn,
            runs_conn=rconn,
            hrv_conn=hconn,
            plan=None,
            month_raw="2026-02",
            today=date(2026, 2, 11),
        )
    finally:
        tconn.close()
        rconn.close()
        hconn.close()

    assert payload["meta"]["month"] == "2026-02"
    weeks = payload["weeks"]
    assert 4 <= len(weeks) <= 6
    assert all(len(week) == 7 for week in weeks)


def test_calendar_day_payload_handles_missing_tables_and_bad_date():
    tconn = _conn()
    rconn = _conn()
    hconn = _conn()
    try:
        payload = build_calendar_day_payload(
            training_conn=tconn,
            runs_conn=rconn,
            hrv_conn=hconn,
            plan=None,
            day_raw="not-a-date",
            today=date(2026, 2, 11),
        )
    finally:
        tconn.close()
        rconn.close()
        hconn.close()

    assert payload["meta"]["date"] == "2026-02-11"
    assert payload["day"]["planned"]["has_plan"] is False
    assert payload["day"]["actual"]["did_train"] is False
