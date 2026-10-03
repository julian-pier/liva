from __future__ import annotations

from scripts.google_calendar_sync_schoolsync import (
    SchoolEvent,
    _dedupe_school_events,
    _entry_looks_like_exam,
    _event_identity_key,
    _event_body,
    _merge_exam_entries,
    _normalize_subject,
    _pick_primary_and_duplicates,
    _to_school_event,
)


def test_normalize_subject_codes():
    assert _normalize_subject("KU_GK1") == "Kunst"
    assert _normalize_subject("KR_GK2") == "Religion"
    assert _normalize_subject("D_GK3") == "Deutsch"
    assert _normalize_subject("M_LK1") == "Mathe LK"
    assert _normalize_subject("SP_LK1") == "Sport LK"
    assert _normalize_subject("SW_GK1") == "SoWi"
    assert _normalize_subject("E_GK1") == "Englisch"
    assert _normalize_subject("GE_GK1") == "Geschichte"
    assert _normalize_subject("BI_GK1") == "Bio"
    assert _normalize_subject("IF_GK1") == "Info"


def test_to_school_event_skips_cancelled_and_eva():
    base = {
        "date": "2026-03-02",
        "start_time": "08:00",
        "end_time": "09:30",
        "subject": "KU_GK1",
        "teacher": "HOR",
        "room": "X0.06",
    }
    cancelled = dict(base, status_hint="cancelled")
    eva = dict(base, status_hint="eva")
    normal = dict(base, status_hint="unknown")

    assert _to_school_event(cancelled) is None
    assert _to_school_event(eva) is None
    assert _to_school_event(normal) is not None


def test_to_school_event_exam_title_has_klausur():
    entry = {
        "date": "2026-03-10",
        "start_time": "08:00",
        "end_time": "10:40",
        "subject": "M_LK1",
        "teacher": "BAU",
        "room": "X0.03",
        "status_hint": "exam",
        "raw_text": "Prüfung: Klausur",
    }
    ev = _to_school_event(entry)
    assert ev is not None
    assert ev.summary == "Mathe LK Klausur"


def test_entry_looks_like_exam_for_klassenarbeit_text():
    entry = {
        "date": "2026-03-25",
        "start_time": "11:45",
        "end_time": "13:15",
        "subject": "D_GK3",
        "status_hint": "unknown",
        "raw_text": "MEY\nD_GK3\nC1.14\nPrüfung: Klassenarbeit",
    }
    assert _entry_looks_like_exam(entry) is True


def test_to_school_event_promotes_unknown_to_exam_from_raw_text():
    entry = {
        "date": "2026-03-25",
        "start_time": "11:45",
        "end_time": "13:15",
        "subject": "D_GK3",
        "teacher": "MEY",
        "room": "C1.14",
        "status_hint": "unknown",
        "raw_text": "MEY\nD_GK3\nC1.14\nPrüfung: Klassenarbeit",
    }
    ev = _to_school_event(entry)
    assert ev is not None
    assert ev.status_hint == "exam"
    assert ev.summary == "Deutsch Klausur"


def test_event_body_exam_has_red_color():
    entry = {
        "date": "2026-03-10",
        "start_time": "08:00",
        "end_time": "10:40",
        "subject": "M_LK1",
        "teacher": "BAU",
        "room": "X0.03",
        "status_hint": "exam",
        "raw_text": "Prüfung: Klausur",
    }
    ev = _to_school_event(entry)
    assert ev is not None
    body = _event_body(ev, "Europe/Berlin")
    assert body.get("colorId") == "11"


def test_merge_exam_entries_contiguous_same_subject():
    entries = [
        {
            "date": "2026-03-10",
            "start_time": "08:00",
            "end_time": "09:55",
            "subject": "M_LK1",
            "teacher": "BAU",
            "room": "X0.03",
            "status_hint": "exam",
            "raw_text": "Prüfung: Klausur",
        },
        {
            "date": "2026-03-10",
            "start_time": "09:55",
            "end_time": "10:40",
            "subject": "M_LK1",
            "teacher": "BAU",
            "room": "X0.03",
            "status_hint": "exam",
            "raw_text": "Klausur Block LK B",
        },
    ]
    out = _merge_exam_entries(entries)
    assert len(out) == 1
    assert out[0]["start_time"] == "08:00"
    assert out[0]["end_time"] == "10:40"
    assert out[0]["subject"] == "M_LK1"


def test_merge_exam_entries_not_contiguous_stays_split():
    entries = [
        {
            "date": "2026-03-10",
            "start_time": "08:00",
            "end_time": "09:55",
            "subject": "M_LK1",
            "status_hint": "exam",
            "raw_text": "Prüfung: Klausur",
        },
        {
            "date": "2026-03-10",
            "start_time": "10:40",
            "end_time": "11:45",
            "subject": "M_LK1",
            "status_hint": "exam",
            "raw_text": "Prüfung: Klausur",
        },
    ]
    out = _merge_exam_entries(entries)
    assert len(out) == 2


def test_merge_exam_entries_from_raw_even_when_status_unknown():
    entries = [
        {
            "date": "2026-03-10",
            "start_time": "08:00",
            "end_time": "09:55",
            "subject": "M_LK1",
            "status_hint": "unknown",
            "raw_text": "Prüfung: Klausur",
        },
        {
            "date": "2026-03-10",
            "start_time": "09:55",
            "end_time": "10:40",
            "subject": "M_LK1",
            "status_hint": "unknown",
            "raw_text": "Klausur Block LK B",
        },
    ]
    out = _merge_exam_entries(entries)
    assert len(out) == 1
    assert out[0]["start_time"] == "08:00"
    assert out[0]["end_time"] == "10:40"


def test_dedupe_school_events_by_source_id():
    first = SchoolEvent(
        source_id="abc123",
        summary="Mathe LK Klausur",
        description="A",
        date="2026-03-10",
        start_time="08:00",
        end_time="10:40",
        status_hint="exam",
    )
    duplicate = SchoolEvent(
        source_id="abc123",
        summary="Mathe LK Klausur",
        description="B",
        date="2026-03-10",
        start_time="08:00",
        end_time="10:40",
        status_hint="exam",
    )
    other = SchoolEvent(
        source_id="def456",
        summary="Deutsch",
        description="C",
        date="2026-03-10",
        start_time="11:00",
        end_time="11:45",
        status_hint="unknown",
    )

    out = _dedupe_school_events([first, duplicate, other])
    assert out == [first, other]


def test_event_identity_key_uses_summary_and_exact_start_end():
    same_a = _event_identity_key(
        "Mathe LK Klausur",
        {"dateTime": "2026-03-10T08:00:00+01:00"},
        {"dateTime": "2026-03-10T10:40:00+01:00"},
    )
    same_b = _event_identity_key(
        "Mathe LK Klausur",
        {"dateTime": "2026-03-10T08:00:00+01:00"},
        {"dateTime": "2026-03-10T10:40:00+01:00"},
    )
    other = _event_identity_key(
        "Mathe LK Klausur",
        {"dateTime": "2026-03-10T08:00:00+01:00"},
        {"dateTime": "2026-03-10T11:25:00+01:00"},
    )

    assert same_a == same_b
    assert same_a != other


def test_pick_primary_and_duplicates_keeps_one_event_id():
    events = [
        {
            "id": "z3",
            "start": {"dateTime": "2026-03-10T08:00:00+01:00"},
            "end": {"dateTime": "2026-03-10T10:40:00+01:00"},
        },
        {
            "id": "a1",
            "start": {"dateTime": "2026-03-10T08:00:00+01:00"},
            "end": {"dateTime": "2026-03-10T10:40:00+01:00"},
        },
        {
            "id": "b2",
            "start": {"dateTime": "2026-03-10T08:00:00+01:00"},
            "end": {"dateTime": "2026-03-10T10:40:00+01:00"},
        },
    ]
    primary, duplicate_ids = _pick_primary_and_duplicates(events)
    assert primary is not None
    assert primary["id"] == "a1"
    assert duplicate_ids == ["b2", "z3"]
