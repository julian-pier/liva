#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from pathlib import Path

from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gcalsync.config import load_config  # noqa: E402
from gcalsync.service import (  # noqa: E402
    GoogleCalendarAuthError,
    GoogleCalendarConfigError,
    build_service,
    delete_event,
    get_school_calendar_id_for_write,
    insert_event,
    is_rate_limit_error,
    list_events,
    make_datetime,
    update_event,
)


@dataclass(frozen=True)
class SchoolEvent:
    source_id: str
    summary: str
    description: str
    date: str
    start_time: str
    end_time: str
    status_hint: str


EXAM_EVENT_COLOR_ID = "11"


def _entry_looks_like_exam(item: dict) -> bool:
    status = str((item or {}).get("status_hint") or "").strip().lower()
    if status == "exam":
        return True
    raw = str((item or {}).get("raw_text") or "").strip().lower()
    exam_tokens = (
        "klausur",
        "prüfung",
        "pruefung",
        "examen",
        "klassenarbeit",
        "kursarbeit",
        "nachschreib",
    )
    return any(token in raw for token in exam_tokens)


def _parse_hhmm(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        hh, mm = raw.split(":", 1)
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _merge_exam_entries(entries: list[dict]) -> list[dict]:
    exam_rows: list[dict] = []
    other_rows: list[dict] = []
    for item in entries:
        if _entry_looks_like_exam(item):
            exam_rows.append(dict(item))
        else:
            other_rows.append(item)

    exam_rows.sort(
        key=lambda x: (
            str(x.get("date") or ""),
            str(x.get("subject") or ""),
            _parse_hhmm(str(x.get("start_time") or "")) or 9999,
            _parse_hhmm(str(x.get("end_time") or "")) or 9999,
        )
    )

    merged: list[dict] = []
    for row in exam_rows:
        if not merged:
            merged.append(row)
            continue
        prev = merged[-1]
        same_day = str(prev.get("date") or "") == str(row.get("date") or "")
        same_subject = str(prev.get("subject") or "") == str(row.get("subject") or "")
        prev_end = _parse_hhmm(str(prev.get("end_time") or ""))
        cur_start = _parse_hhmm(str(row.get("start_time") or ""))
        contiguous = prev_end is not None and cur_start is not None and prev_end == cur_start
        if not (same_day and same_subject and contiguous):
            merged.append(row)
            continue

        prev["end_time"] = row.get("end_time") or prev.get("end_time")

        prev_teacher = str(prev.get("teacher") or "").strip()
        cur_teacher = str(row.get("teacher") or "").strip()
        if not prev_teacher and cur_teacher:
            prev["teacher"] = cur_teacher

        prev_room = str(prev.get("room") or "").strip()
        cur_room = str(row.get("room") or "").strip()
        if not prev_room and cur_room:
            prev["room"] = cur_room

        prev_raw = str(prev.get("raw_text") or "").strip()
        cur_raw = str(row.get("raw_text") or "").strip()
        if cur_raw and cur_raw not in prev_raw:
            prev["raw_text"] = (prev_raw + "\n" + cur_raw).strip() if prev_raw else cur_raw

    return [*other_rows, *merged]


def _normalize_subject(subject_raw: str) -> str:
    raw = str(subject_raw or "").strip()
    if not raw:
        return ""
    prefix = raw.split("_", 1)[0].strip().upper()
    if prefix.startswith("M"):
        return "Mathe LK"
    mapping = {
        "KU": "Kunst",
        "KR": "Religion",
        "D": "Deutsch",
        "SP": "Sport LK",
        "SW": "SoWi",
        "E": "Englisch",
        "GE": "Geschichte",
        "BI": "Bio",
        "IF": "Info",
    }
    return mapping.get(prefix, prefix or raw)


def _hash_id(entry: dict) -> str:
    subject = _normalize_subject(str(entry.get("subject") or ""))
    key = "|".join(
        [
            str(entry.get("date") or ""),
            str(entry.get("start_time") or ""),
            str(entry.get("end_time") or ""),
            subject,
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def _to_school_event(entry: dict) -> SchoolEvent | None:
    date_iso = entry.get("date")
    start = entry.get("start_time")
    end = entry.get("end_time")
    if not (date_iso and start and end):
        return None
    subject = _normalize_subject(str(entry.get("subject") or ""))
    teacher = (entry.get("teacher") or "").strip()
    room = (entry.get("room") or "").strip()
    status = (entry.get("status_hint") or "unknown").strip()
    if status.lower() == "unknown" and _entry_looks_like_exam(entry):
        status = "exam"
    if status.lower() in {"cancelled", "eva"}:
        return None
    source_id = _hash_id(entry)
    summary = subject or "Unterricht"
    if status.lower() == "exam":
        summary = f"{subject} Klausur" if subject else "Klausur"
    if status == "cancelled":
        summary = f"{subject or 'Unterricht'} (Entfall)"
    description_parts = []
    if teacher:
        description_parts.append(f"Lehrer: {teacher}")
    if room:
        description_parts.append(f"Raum: {room}")
    if entry.get("raw_text"):
        description_parts.append(f"Raw: {entry['raw_text']}")
    description_parts.append(f"Quelle: SchoolSync / {source_id}")
    return SchoolEvent(
        source_id=source_id,
        summary=summary,
        description="\n".join(description_parts),
        date=date_iso,
        start_time=start,
        end_time=end,
        status_hint=status,
    )


def _dedupe_school_events(events: list[SchoolEvent]) -> list[SchoolEvent]:
    out: list[SchoolEvent] = []
    seen_ids: set[str] = set()
    for item in events:
        if item.source_id in seen_ids:
            continue
        seen_ids.add(item.source_id)
        out.append(item)
    return out


def _event_identity_key(summary: str, start: dict, end: dict) -> str:
    start_raw = str(start.get("dateTime") or start.get("date") or "").strip()
    end_raw = str(end.get("dateTime") or end.get("date") or "").strip()
    return "|".join([summary.strip(), start_raw, end_raw])


def _pick_primary_and_duplicates(events: list[dict]) -> tuple[dict | None, list[str]]:
    if not events:
        return None, []

    def _sort_key(e: dict) -> tuple[str, str, str]:
        start_raw = str((e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or "")
        end_raw = str((e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date") or "")
        event_id = str(e.get("id") or "")
        return (start_raw, end_raw, event_id)

    ordered = sorted(events, key=_sort_key)
    primary = ordered[0]
    duplicate_ids = [str(e.get("id") or "").strip() for e in ordered[1:] if str(e.get("id") or "").strip()]
    return primary, duplicate_ids


def _find_existing_by_source_id_for_day(
    service,
    *,
    calendar_id: str,
    source_id: str,
    date_iso: str,
    timezone: str,
) -> tuple[dict | None, list[str]]:
    day_min = make_datetime(date_iso, "00:00", timezone)["dateTime"]
    day_max = make_datetime(date_iso, "23:59", timezone)["dateTime"]
    matches = list_events(
        service,
        calendar_id=calendar_id,
        time_min=day_min,
        time_max=day_max,
        private_extended_property=[f"liva_event_id={source_id}"],
    )
    return _pick_primary_and_duplicates(matches)


def _event_body(item: SchoolEvent, timezone: str) -> dict:
    body = {
        "summary": item.summary,
        "description": (item.description + "\nsource=webuntis").strip(),
        "start": make_datetime(item.date, item.start_time, timezone),
        "end": make_datetime(item.date, item.end_time, timezone),
        "extendedProperties": {
            "private": {
                "liva_source": "webuntis",
                "liva_event_id": item.source_id,
                "liva_status": item.status_hint,
            }
        },
    }
    if str(item.status_hint or "").strip().lower() == "exam":
        body["colorId"] = EXAM_EVENT_COLOR_ID
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SchoolSync events to Google Calendar.")
    parser.add_argument(
        "--from-json",
        default=None,
        help="Optional path to SchoolSync JSON. Default: WEBUNTIS_LATEST_TODAY_FILE",
    )
    parser.add_argument("--days-back", type=int, default=3)
    parser.add_argument("--days-forward", type=int, default=31)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    source_file = Path(args.from_json) if args.from_json else cfg.latest_schoolsync_file
    if not source_file.exists():
        print(f"[GCal] SchoolSync JSON missing: {source_file}")
        return 2

    payload = json.loads(source_file.read_text(encoding="utf-8"))
    entries = payload.get("entries_visible") or payload.get("entries") or []
    entries = _merge_exam_entries(entries)
    school_events = [e for e in (_to_school_event(item) for item in entries) if e is not None]
    school_events = _dedupe_school_events(school_events)
    if not school_events:
        print("[GCal] No timed school events found in JSON.")
        return 0

    now = datetime.now().astimezone().replace(microsecond=0)
    base_min = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    time_min = (base_min - timedelta(days=max(0, args.days_back))).isoformat()
    time_max = (now + timedelta(days=max(1, args.days_forward))).isoformat()

    try:
        service = build_service(cfg.credentials_file, cfg.token_file)
    except GoogleCalendarAuthError as exc:
        print(f"[GCal] {exc}")
        return 2
    try:
        webuntis_calendar_id = get_school_calendar_id_for_write(cfg.school_calendar_id)
        print(f"[GCal] WebUntis calendar id: {webuntis_calendar_id}")

        existing = list_events(
            service,
            calendar_id=webuntis_calendar_id,
            time_min=time_min,
            time_max=time_max,
        )
        by_source_id: dict[str, dict] = {}
        by_identity: dict[str, dict] = {}
        duplicate_existing_event_ids: set[str] = set()
        for e in existing:
            private = ((e.get("extendedProperties") or {}).get("private") or {})
            source = str(private.get("liva_source") or "").strip().lower()
            if source not in {"webuntis", "schoolsync"}:
                continue
            identity_key = _event_identity_key(
                str(e.get("summary") or ""),
                e.get("start") or {},
                e.get("end") or {},
            )
            if identity_key in by_identity:
                dup_id = str(e.get("id") or "").strip()
                if dup_id:
                    duplicate_existing_event_ids.add(dup_id)
                continue
            by_identity[identity_key] = e
            src_id = str(private.get("liva_event_id") or "").strip()
            if src_id:
                if src_id in by_source_id:
                    dup_id = str(e.get("id") or "").strip()
                    if dup_id:
                        duplicate_existing_event_ids.add(dup_id)
                else:
                    by_source_id[src_id] = e

        wanted_ids = {e.source_id for e in school_events}
        created = 0
        updated = 0
        deleted = 0
        unchanged = 0

        if duplicate_existing_event_ids:
            for event_id in sorted(duplicate_existing_event_ids):
                if args.dry_run:
                    print(f"[dry-run] delete-duplicate {event_id}")
                else:
                    delete_event(service, calendar_id=webuntis_calendar_id, event_id=event_id)
                deleted += 1

        for item in school_events:
            body = _event_body(item, cfg.timezone)
            old = by_source_id.get(item.source_id)
            if old is None:
                found_old, found_duplicate_ids = _find_existing_by_source_id_for_day(
                    service,
                    calendar_id=webuntis_calendar_id,
                    source_id=item.source_id,
                    date_iso=item.date,
                    timezone=cfg.timezone,
                )
                if found_old is not None:
                    by_source_id[item.source_id] = found_old
                    old = found_old
                for event_id in found_duplicate_ids:
                    if args.dry_run:
                        print(f"[dry-run] delete-duplicate {event_id}")
                    else:
                        delete_event(service, calendar_id=webuntis_calendar_id, event_id=event_id)
                    deleted += 1
            if old is None:
                if args.dry_run:
                    print(f"[dry-run] create {item.source_id} {item.summary}")
                else:
                    created_event = insert_event(service, calendar_id=webuntis_calendar_id, event=body)
                    created_id = str(created_event.get("id") or "").strip()
                    if created_id:
                        by_source_id[item.source_id] = {"id": created_id, **body}
                created += 1
                continue

            old_cmp = {
                "summary": old.get("summary"),
                "description": old.get("description"),
                "start": old.get("start"),
                "end": old.get("end"),
                "extendedProperties": old.get("extendedProperties"),
            }
            new_cmp = {
                "summary": body.get("summary"),
                "description": body.get("description"),
                "start": body.get("start"),
                "end": body.get("end"),
                "extendedProperties": body.get("extendedProperties"),
            }
            if old_cmp == new_cmp:
                unchanged += 1
                continue
            if args.dry_run:
                print(f"[dry-run] update {item.source_id} {item.summary}")
            else:
                update_event(service, calendar_id=webuntis_calendar_id, event_id=old["id"], event=body)
            updated += 1

        cleanup_enabled = cfg.cleanup_missing_school_events and not args.no_cleanup
        if cleanup_enabled:
            for src_id, old in by_source_id.items():
                if src_id in wanted_ids:
                    continue
                if args.dry_run:
                    print(f"[dry-run] delete {src_id} {old.get('summary')}")
                else:
                    delete_event(service, calendar_id=webuntis_calendar_id, event_id=old["id"])
                deleted += 1

        print(
            "[GCal] School sync done: "
            f"create={created} update={updated} delete={deleted} unchanged={unchanged} total={len(school_events)}"
        )
        return 0
    except GoogleCalendarConfigError as exc:
        print(f"[GCal] {exc}")
        return 2
    except HttpError as exc:
        if is_rate_limit_error(exc):
            print("[GCal] Google Calendar Rate Limit erreicht. Calendar-Sync übersprungen.")
            return 0
        print(f"[GCal] Google Calendar Sync fehlgeschlagen: HTTP {getattr(exc.resp, 'status', '?')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
