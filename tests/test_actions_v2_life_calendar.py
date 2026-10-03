import sqlite3
import tempfile
import os
import importlib
from datetime import datetime, timedelta

import pytest

from database import connections

TEST_SCHEMA = {
    'snapshots': '''
    CREATE TABLE school_schedule_snapshots (
      id INTEGER PRIMARY KEY,
      fetched_at TEXT,
      source TEXT,
      date TEXT,
      raw_json TEXT,
      derived_summary_json TEXT,
      created_at TEXT
    );
    ''',
    'entries': '''
    CREATE TABLE school_schedule_entries (
      id INTEGER PRIMARY KEY,
      snapshot_id INTEGER,
      date TEXT,
      subject TEXT,
      teacher TEXT,
      room TEXT,
      start_time TEXT,
      end_time TEXT,
      raw_text TEXT,
      status_hint TEXT
    );
    ''',
}


def make_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(TEST_SCHEMA['snapshots'])
    cur.executescript(TEST_SCHEMA['entries'])
    conn.commit()
    return conn


def insert_snapshot(conn, snapshot_id, fetched_at, source, date_iso, created_at=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO school_schedule_snapshots (id,fetched_at,source,date,created_at) VALUES (?,?,?,?,?)",
        (snapshot_id, fetched_at, source, date_iso, created_at or fetched_at),
    )
    conn.commit()


def insert_entry(conn, snapshot_id, date_iso, start, end, subject, teacher=None, room=None, raw_text=None, status_hint=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO school_schedule_entries (snapshot_id,date,start_time,end_time,subject,teacher,room,raw_text,status_hint) VALUES (?,?,?,?,?,?,?,?,?)",
        (snapshot_id, date_iso, start, end, subject, teacher, room, raw_text, status_hint),
    )
    conn.commit()


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    fd = tmp_path / "training.sqlite3"
    conn = make_db(str(fd))
    # monkeypatch connections.get_training_db to return this conn
    monkeypatch.setattr(connections, 'get_training_db', lambda: conn)
    yield conn
    try:
        conn.close()
    except Exception:
        pass


def test_school_entries_and_range(temp_db):
    conn = temp_db
    # latest snapshot
    now = datetime.now().isoformat()
    insert_snapshot(conn, 10, now, 'webuntis', '2026-05-24')
    # future entries in latest snapshot
    insert_entry(conn, 10, '2026-05-27', '08:00', '09:55', 'E_GK1', 'T1', 'R1', None, None)
    insert_entry(conn, 10, '2026-05-27', '09:55', '11:45', 'GE_GK1', 'T2', 'R2', None, None)
    insert_entry(conn, 10, '2026-05-28', '08:00', '09:55', 'E_GK1', 'T1', 'R1', 'Prüfung', 'exam')
    insert_entry(conn, 10, '2026-05-28', '09:55', '11:45', 'D_GK3', None, None, None, 'cancelled')

    # older snapshot with same dates (should be ignored)
    old_time = (datetime.now() - timedelta(days=10)).isoformat()
    insert_snapshot(conn, 5, old_time, 'webuntis', '2026-05-14')
    insert_entry(conn, 5, '2026-05-27', '08:00', '09:55', 'E_GK1', 'OLD', 'ROLD', None, None)

    import ai.actions_v2 as m
    # use helper
    events_by_day, freshness = m._school_schedule_entries_range('2026-05-27', '2026-05-28')
    assert freshness.get('source') == 'webuntis_latest_snapshot'
    assert '2026-05-27' in events_by_day
    assert '2026-05-28' in events_by_day
    assert len(events_by_day['2026-05-27']) == 2
    assert any(e['title'] == 'E_GK1' for e in events_by_day['2026-05-27'])
    # cancelled present
    cancelled = [e for e in events_by_day['2026-05-28'] if e['title'] == 'D_GK3']
    assert cancelled and cancelled[0]['status'] == 'cancelled'
    # exam flagged
    exams = [e for e in events_by_day['2026-05-28'] if e['title'] == 'E_GK1']
    assert exams and exams[0]['is_exam']


def test_dedup_within_snapshot(temp_db):
    conn = temp_db
    now = datetime.now().isoformat()
    insert_snapshot(conn, 20, now, 'webuntis', '2026-05-24')
    insert_entry(conn, 20, '2026-06-01', '08:00', '09:55', 'KU_GK1')
    # duplicate entry
    insert_entry(conn, 20, '2026-06-01', '08:00', '09:55', 'KU_GK1')

    import ai.actions_v2 as m
    events_by_day, freshness = m._school_schedule_entries_range('2026-06-01', '2026-06-01')
    assert len(events_by_day.get('2026-06-01', [])) == 1


def test_calendar_range_payload_uses_entries(temp_db):
    conn = temp_db
    now = datetime.now().isoformat()
    insert_snapshot(conn, 30, now, 'webuntis', '2026-05-24')
    insert_entry(conn, 30, '2026-05-27', '08:00', '09:55', 'E_GK1')
    insert_entry(conn, 30, '2026-05-29', '08:00', '13:15', 'BI/SW/M')

    import ai.actions_v2 as m
    payload = m._calendar_range_payload('2026-05-27', '2026-05-29')
    days = {d['date']: d for d in payload['days']}
    assert '2026-05-27' in days and days['2026-05-27']['has_events']
    assert '2026-05-28' in days
    assert '2026-05-29' in days
    # total events count across all days
    total = sum(len(d.get('events') or []) for d in payload['days'])
    assert total == 2
    # flat events should include date/day_iso
    flat = []
    for d in payload['days']:
        for e in d.get('events') or []:
            flat.append(e)
    assert all(e.get('date') for e in flat), "Every flat event must include a date"
    assert all(e.get('day_iso') for e in flat), "Every flat event must include day_iso"


def test_school_schedule_exam_event_has_date(temp_db):
    conn = temp_db
    now = datetime.now().isoformat()
    insert_snapshot(conn, 40, now, 'webuntis', '2026-05-24')
    insert_entry(conn, 40, '2026-05-28', '08:00', '09:55', 'E_GK1', raw_text='Prüfung', status_hint='exam')

    import ai.actions_v2 as m
    payload = m._calendar_range_payload('2026-05-28', '2026-05-28')
    days = {d['date']: d for d in payload['days']}
    assert '2026-05-28' in days
    events = days['2026-05-28'].get('events') or []
    assert len(events) == 1
    exam = events[0]
    assert exam.get('is_exam') is True
    assert exam.get('date') == '2026-05-28'
    assert exam.get('day_iso') == '2026-05-28'
    assert exam.get('weekday_label') == 'Do'
    assert exam.get('title') == 'E_GK1'
