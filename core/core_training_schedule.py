from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from flask import g, has_request_context
from database.connections import get_plans_db, get_training_db
from app_support.request_timing import record_timing, timed_block

BERLIN = ZoneInfo("Europe/Berlin")
CORE_TRAINING_EVENT_COLOR_ID = "7"


def _request_cache_bucket(name: str) -> dict[str, Any]:
    if not has_request_context():
        return {}
    cache = getattr(g, "_training_schedule_request_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        g._training_schedule_request_cache = cache
    bucket = cache.get(name)
    if not isinstance(bucket, dict):
        bucket = {}
        cache[name] = bucket
    return bucket


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        val = float(value)
    except Exception:
        return float(default)
    if val != val:
        return float(default)
    return val


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_hhmm(text: Any) -> Optional[int]:
    raw = str(text or "").strip()
    if ":" not in raw:
        return None
    try:
        hh_s, mm_s = raw.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
    except Exception:
        return None
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return hh * 60 + mm


def _to_hhmm(minutes: int) -> str:
    m = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{m // 60:02d}:{m % 60:02d}"


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def ensure_core_training_schedule_schema(conn=None) -> None:
    owns = conn is None
    db = conn or get_training_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_schedule_day (
                day_iso TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_training_schedule_day_updated ON core_training_schedule_day(updated_at DESC)"
        )
        db.commit()
    finally:
        if owns:
            db.close()


def _load_schedule(day_iso: str) -> dict[str, Any] | None:
    ensure_core_training_schedule_schema()
    conn = get_training_db()
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT signature, schedule_json FROM core_training_schedule_day WHERE day_iso=? LIMIT 1",
            (day_iso,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[1] or "{}") if row[1] else None
        except Exception:
            return None
    finally:
        conn.close()


def _write_schedule(day_iso: str, signature: str, schedule: dict[str, Any]) -> None:
    ensure_core_training_schedule_schema()
    conn = get_training_db()
    try:
        now = _utcnow_iso()
        payload = json.dumps(schedule or {}, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO core_training_schedule_day (day_iso, signature, schedule_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day_iso) DO UPDATE SET
                signature=excluded.signature,
                schedule_json=excluded.schedule_json,
                updated_at=excluded.updated_at
            """,
            (day_iso, signature, payload, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _load_autopilot_day_output(day_iso: str) -> dict[str, Any] | None:
    conn = get_training_db()
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT output_json FROM autopilot_day WHERE date=? LIMIT 1",
            (day_iso,),
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0]) if row[0] else None
        except Exception:
            return None
    finally:
        conn.close()


def _training_intent_from_plan(day_iso: str) -> dict[str, Any]:
    conn = get_plans_db()
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT plan_json, blocks_json, title FROM gym_plans WHERE is_active=1 AND is_archived=0 ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return {
            "available": False,
            "training_today": False,
            "training_type": None,
            "training_time": None,
            "run": False,
            "gym": False,
        }
    finally:
        conn.close()
    if not row:
        return {
            "available": False,
            "training_today": False,
            "training_type": None,
            "training_time": None,
            "run": False,
            "gym": False,
        }
    try:
        plan_json = json.loads(row[0] or "{}") if row[0] else {}
    except Exception:
        plan_json = {}
    try:
        target_day = date.fromisoformat(day_iso)
    except Exception:
        target_day = date.today()
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][target_day.weekday()]
    events: list[dict[str, Any]] = []

    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    if days:
        for d in days:
            if str(d.get("day") or "") == wd and isinstance(d.get("events"), list):
                events.extend([e for e in d.get("events") if isinstance(e, dict)])

    if not events:
        base_week = plan_json.get("base_week") if isinstance(plan_json.get("base_week"), dict) else {}
        events.extend([e for e in (base_week.get(wd) or []) if isinstance(e, dict)])

    if not events:
        return {
            "available": True,
            "training_today": False,
            "training_type": None,
            "training_time": None,
            "run": False,
            "gym": False,
        }

    gym_event = next((e for e in events if str(e.get("kind") or "").lower() == "gym"), None)
    run_event = next((e for e in events if str(e.get("kind") or "").lower() == "run"), None)
    event = gym_event or run_event or events[0]
    return {
        "available": True,
        "training_today": True,
        "training_type": str(event.get("kind") or "").lower() or ("run" if run_event else "gym"),
        "training_time": str(event.get("time") or "").strip() or None,
        "run": bool(run_event),
        "gym": bool(gym_event),
        "title": str(event.get("title") or "").strip() or None,
    }


def _load_meal_plan(day_iso: str) -> list[dict[str, Any]]:
    try:
        from nutrition.nutrition_planning_db import get_logging_day_payload
    except Exception:
        return []
    try:
        payload = get_logging_day_payload(day_iso)
    except Exception:
        return []
    meals = [m for m in (payload.get("planned_meals") or []) if isinstance(m, dict)]
    out = []
    for m in meals:
        status = str(m.get("status") or "open").strip().lower()
        if status in {"logged", "skipped", "missed"}:
            continue
        t = _parse_hhmm(m.get("shifted_time_text") or m.get("time_text"))
        if t is None:
            continue
        macros = m.get("macros") if isinstance(m.get("macros"), dict) else {}
        out.append(
            {
                "slot_id": m.get("slot_id"),
                "slot_index": m.get("slot_index"),
                "time_min": t,
                "macros": {
                    "kcal": _safe_float(macros.get("kcal"), 0.0),
                    "p": _safe_float(macros.get("p"), 0.0),
                    "c": _safe_float(macros.get("c"), 0.0),
                    "f": _safe_float(macros.get("f"), 0.0),
                },
            }
        )
    return out


def _meal_ratio(macros: dict[str, float]) -> dict[str, float]:
    kcal = _safe_float(macros.get("kcal"), 0.0)
    if kcal <= 1.0:
        return {"p": 0.0, "c": 0.0, "f": 0.0}
    return {
        "p": _clamp((_safe_float(macros.get("p"), 0.0) * 4.0) / kcal),
        "c": _clamp((_safe_float(macros.get("c"), 0.0) * 4.0) / kcal),
        "f": _clamp((_safe_float(macros.get("f"), 0.0) * 9.0) / kcal),
    }


def _meal_alignment_score(training_start: int, training_end: int, meals: list[dict[str, Any]]) -> float:
    if not meals:
        return 0.45
    pre_candidates = []
    post_candidates = []
    for m in meals:
        t = m.get("time_min")
        if t is None:
            continue
        if training_start - 240 <= t <= training_start - 60:
            pre_candidates.append(m)
        if training_end <= t <= training_end + 120:
            post_candidates.append(m)
    def _score_pre(m: dict[str, Any]) -> float:
        t = int(m.get("time_min") or 0)
        dist = abs((training_start - 180) - t)
        time_score = _clamp(1.0 - (dist / 140.0))
        ratios = _meal_ratio(m.get("macros") or {})
        carb_score = _clamp((ratios.get("c", 0.0) - 0.32) / 0.28)
        return 0.6 * time_score + 0.4 * carb_score

    def _score_post(m: dict[str, Any]) -> float:
        t = int(m.get("time_min") or 0)
        dist = abs((training_end + 60) - t)
        time_score = _clamp(1.0 - (dist / 120.0))
        ratios = _meal_ratio(m.get("macros") or {})
        prot_score = _clamp((ratios.get("p", 0.0) - 0.28) / 0.32)
        return 0.6 * time_score + 0.4 * prot_score

    pre_score = max((_score_pre(m) for m in pre_candidates), default=0.25)
    post_score = max((_score_post(m) for m in post_candidates), default=0.25)
    return _clamp((pre_score + post_score) / 2.0)


def _schedule_from_latest_file(day_iso: str) -> dict[str, Any]:
    path = "/opt/liva/var/schoolsync/latest_today.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {
            "available": False,
            "entries": [],
            "intervals": [],
            "school_start": None,
            "school_end": None,
        }
    rows = [
        e
        for e in (payload.get("entries_visible") or payload.get("entries") or [])
        if isinstance(e, dict) and str(e.get("date") or "").strip() == day_iso
    ]
    rows = [e for e in rows if str(e.get("status_hint") or "").lower() not in {"cancelled", "eva"}]
    ranges: list[tuple[int, int]] = []
    for row in rows:
        st = _parse_hhmm(row.get("start_time"))
        en = _parse_hhmm(row.get("end_time"))
        if st is None or en is None or en <= st:
            continue
        ranges.append((st, en))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for st, en in ranges:
        if not merged or st > merged[-1][1]:
            merged.append((st, en))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
    school_start = _to_hhmm(merged[0][0]) if merged else None
    school_end = _to_hhmm(merged[-1][1]) if merged else None
    return {
        "available": bool(rows),
        "entries": rows,
        "intervals": merged,
        "school_start": school_start,
        "school_end": school_end,
    }


def _parse_iso_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _collect_calendar_events(day_iso: str) -> dict[str, Any]:
    started_total = datetime.now(BERLIN)
    cache = _request_cache_bucket("calendar_events")
    cached = cache.get(day_iso)
    if isinstance(cached, dict):
        record_timing("calendar.collect.cache_hit", 0.0, day=day_iso)
        return cached
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service, list_calendars, list_events
    except Exception:
        return {"available": False, "events": [], "source": "google_calendar"}
    try:
        cfg = load_config()
        tz_name = str(getattr(cfg, "timezone", "Europe/Berlin") or "Europe/Berlin")
        tz = ZoneInfo(tz_name)
        service_cache = _request_cache_bucket("google_service")
        service = service_cache.get("service")
        if service is None:
            with timed_block("calendar.collect.build_service"):
                service = build_service(cfg.credentials_file, cfg.token_file)
            service_cache["service"] = service
        day = date.fromisoformat(day_iso)
        day_start = datetime.combine(day, dt_time(0, 0), tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        calendar_ids: list[str] = []
        for attr in [
            "default_calendar_id",
            "school_calendar_id",
            "work_calendar_id",
            "training_calendar_id",
            "football_calendar_id",
        ]:
            cid = str(getattr(cfg, attr, "") or "").strip()
            if cid and cid not in calendar_ids:
                calendar_ids.append(cid)
        extra_tokens: list[str] = []
        for env_key in [
            "GOOGLE_CALENDAR_FAMILY_ID",
            "GOOGLE_CALENDAR_FAMILY_IDS",
            "GOOGLE_CALENDAR_WORK_ID",
            "GOOGLE_CALENDAR_WORK_IDS",
            "GOOGLE_CALENDAR_EXTRA_IDS",
            "CORE_CALENDAR_STRESS_IDS",
            "CORE_CALENDAR_BLOCKER_IDS",
        ]:
            raw = str(os.getenv(env_key, "") or "").strip()
            if not raw:
                continue
            extra_tokens.extend([part.strip() for part in raw.split(",") if part.strip()])
        for cid in extra_tokens:
            if cid not in calendar_ids:
                calendar_ids.append(cid)
        selected_cache = _request_cache_bucket("calendar_ids")
        selected_ids = selected_cache.get("selected_ids")
        if not isinstance(selected_ids, list):
            selected_ids = []
            try:
                with timed_block("calendar.collect.calendar_list"):
                    entries = list_calendars(service)
                for entry in entries or []:
                    if not isinstance(entry, dict):
                        continue
                    cid = str(entry.get("id") or "").strip()
                    if not cid:
                        continue
                    selected = bool(entry.get("selected", True))
                    hidden = bool(entry.get("hidden", False))
                    if not selected or hidden:
                        continue
                    selected_ids.append(cid)
            except Exception:
                selected_ids = []
            selected_cache["selected_ids"] = list(selected_ids)
        for cid in selected_ids:
            if cid not in calendar_ids:
                calendar_ids.append(cid)
        if not calendar_ids:
            result = {"available": False, "events": [], "source": "google_calendar"}
            cache[day_iso] = result
            record_timing("calendar.collect.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, events=0)
            return result
        raw_events: list[dict[str, Any]] = []
        for calendar_id in calendar_ids:
            try:
                with timed_block("calendar.collect.events_list", calendar_id=calendar_id):
                    rows = list_events(
                        service,
                        calendar_id=calendar_id,
                        time_min=day_start.isoformat(),
                        time_max=day_end.isoformat(),
                        max_results=200,
                    )
            except Exception:
                continue
            for row in rows or []:
                if isinstance(row, dict):
                    row["_calendar_id"] = calendar_id
                    raw_events.append(row)
        result = {
            "available": True,
            "events": raw_events,
            "source": "google_calendar",
            "calendar_ids": calendar_ids,
            "timezone": tz_name,
        }
        cache[day_iso] = result
        record_timing("calendar.collect.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, events=len(raw_events), calendars=len(calendar_ids))
        return result
    except Exception:
        return {"available": False, "events": [], "source": "google_calendar"}


def _event_to_interval(event: dict[str, Any], *, day_start: datetime, day_end: datetime, tz: ZoneInfo) -> tuple[int, int] | None:
    if not isinstance(event, dict):
        return None
    if str(event.get("transparency") or "").strip().lower() == "transparent":
        return None
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    st_date = str(start.get("date") or "").strip()
    en_date = str(end.get("date") or "").strip()
    if st_date and en_date:
        try:
            sd = date.fromisoformat(st_date)
            ed = date.fromisoformat(en_date)
        except Exception:
            sd = None
            ed = None
        if sd and ed and sd <= day_start.date() < ed:
            return (0, 24 * 60)
    st_dt = _parse_iso_dt(start.get("dateTime"))
    en_dt = _parse_iso_dt(end.get("dateTime"))
    if not st_dt:
        return None
    if en_dt is None:
        en_dt = st_dt + timedelta(hours=1)
    st_local = st_dt.astimezone(tz)
    en_local = en_dt.astimezone(tz)
    clip_start = max(st_local, day_start)
    clip_end = min(en_local, day_end)
    if clip_end <= clip_start:
        return None
    start_min = int((clip_start - day_start).total_seconds() // 60)
    end_min = int((clip_end - day_start).total_seconds() // 60)
    return (start_min, end_min)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for st, en in intervals[1:]:
        last_st, last_en = merged[-1]
        if st <= last_en:
            merged[-1] = (last_st, max(last_en, en))
        else:
            merged.append((st, en))
    return merged


def _overlap_minutes(window: tuple[int, int], busy: list[tuple[int, int]]) -> int:
    if not busy:
        return 0
    total = 0
    ws, we = window
    for bs, be in busy:
        if be <= ws or bs >= we:
            continue
        total += max(0, min(we, be) - max(ws, bs))
    return total


def _preferred_time_score(start_min: int, preferred_min: int | None) -> float:
    if preferred_min is None:
        return 0.5
    dist = abs(start_min - preferred_min)
    if dist <= 30:
        return 1.0
    if dist <= 90:
        return 0.8 - (dist - 30) / 150.0
    if dist <= 180:
        return 0.45
    return 0.2


def _lateness_score(end_min: int, late_threshold: int) -> float:
    if end_min <= late_threshold:
        return 1.0
    over = end_min - late_threshold
    if over <= 30:
        return 0.7
    if over <= 60:
        return 0.45
    return 0.2


def _recovery_score(end_min: int, next_day_early: bool) -> float:
    if not next_day_early:
        return 1.0 if end_min <= 21 * 60 + 30 else 0.6
    if end_min <= 20 * 60 + 45:
        return 1.0
    if end_min <= 21 * 60 + 30:
        return 0.6
    return 0.25


def _window_payload(
    *,
    prep_start: int,
    training_start: int,
    training_end: int,
    post_buffer_end: int,
    scores: dict[str, float],
    conflict_minutes: int,
    total_minutes: int,
    note: str,
) -> dict[str, Any]:
    return {
        "prep_start": _to_hhmm(prep_start),
        "training_start": _to_hhmm(training_start),
        "training_end": _to_hhmm(training_end),
        "post_buffer_end": _to_hhmm(post_buffer_end),
        "conflict_minutes": int(conflict_minutes),
        "total_minutes": int(total_minutes),
        "scores": {k: round(_clamp(v), 3) for k, v in (scores or {}).items()},
        "score_total": round(_clamp(sum(scores.values()) / max(1, len(scores))), 3),
        "note": note,
    }


def _build_calendar_event_body(
    *,
    summary: str,
    date_iso: str,
    start_hhmm: str,
    end_hhmm: str,
    timezone_name: str,
    marker_id: str,
    description: str,
) -> dict[str, Any]:
    from gcalsync.service import make_datetime

    return {
        "summary": str(summary).strip() or "Training",
        "description": str(description or "").strip(),
        "colorId": CORE_TRAINING_EVENT_COLOR_ID,
        "start": make_datetime(date_iso, start_hhmm, timezone_name),
        "end": make_datetime(date_iso, end_hhmm, timezone_name),
        "extendedProperties": {
            "private": {
                "liva_source": "core_training",
                "liva_event_id": marker_id,
            }
        },
    }


def _sync_calendar_event(
    *,
    day_iso: str,
    summary: str,
    start_hhmm: str,
    end_hhmm: str,
    marker_id: str,
    description: str,
) -> dict[str, Any]:
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service, insert_event, list_events, update_event
    except Exception:
        return {"ok": False, "error": "calendar_module_unavailable"}
    try:
        cfg = load_config()
        service = build_service(cfg.credentials_file, cfg.token_file)
        calendar_id = str(getattr(cfg, "training_calendar_id", "") or cfg.default_calendar_id or "primary")
        body = _build_calendar_event_body(
            summary=summary,
            date_iso=day_iso,
            start_hhmm=start_hhmm,
            end_hhmm=end_hhmm,
            timezone_name=cfg.timezone,
            marker_id=marker_id,
            description=description,
        )
        tz = ZoneInfo(cfg.timezone)
        day_start = datetime.combine(date.fromisoformat(day_iso), dt_time(0, 0), tzinfo=tz)
        existing = list_events(
            service,
            calendar_id=calendar_id,
            time_min=day_start.isoformat(),
            time_max=(day_start + timedelta(days=1)).isoformat(),
            private_extended_property=[f"liva_event_id={marker_id}"],
            max_results=5,
        )
        if existing:
            ev = existing[0]
            ev_id = str(ev.get("id") or "")
            updated = update_event(service, calendar_id=calendar_id, event_id=ev_id, event=body)
            # Remove other CORE training events on the same day (legacy marker changes)
            try:
                from gcalsync.service import delete_event
                day_events = list_events(
                    service,
                    calendar_id=calendar_id,
                    time_min=day_start.isoformat(),
                    time_max=(day_start + timedelta(days=1)).isoformat(),
                    max_results=20,
                )
                for other in day_events or []:
                    if str(other.get("id") or "") == ev_id:
                        continue
                    props = (other.get("extendedProperties") or {}).get("private") if isinstance(other.get("extendedProperties"), dict) else {}
                    if isinstance(props, dict) and str(props.get("liva_source") or "").strip().lower() == "core_training":
                        oid = str(other.get("id") or "")
                        if oid:
                            delete_event(service, calendar_id=calendar_id, event_id=oid)
                    else:
                        # Legacy duplicates without marker: remove extra Training · entries.
                        summary = str(other.get("summary") or "").strip()
                        creator = (other.get("creator") or {}).get("email")
                        if summary.startswith("Training") and str(creator or "").strip().lower() == str(cfg.default_calendar_id).lower():
                            oid = str(other.get("id") or "")
                            if oid:
                                delete_event(service, calendar_id=calendar_id, event_id=oid)
            except Exception:
                pass
            return {"ok": True, "action": "updated", "event_id": ev_id, "event": updated}

        # Fallback: clean up duplicate CORE training events for same day if marker changed.
        day_events = list_events(
            service,
            calendar_id=calendar_id,
            time_min=day_start.isoformat(),
            time_max=(day_start + timedelta(days=1)).isoformat(),
            max_results=20,
        )
        core_events = []
        for ev in day_events or []:
            props = (ev.get("extendedProperties") or {}).get("private") if isinstance(ev.get("extendedProperties"), dict) else {}
            if isinstance(props, dict) and str(props.get("liva_source") or "").strip().lower() == "core_training":
                core_events.append(ev)
        if core_events:
            # Keep most recently updated, update it to new marker and details, delete others.
            core_events.sort(key=lambda e: str(e.get("updated") or ""), reverse=True)
            keep = core_events[0]
            keep_id = str(keep.get("id") or "")
            updated = update_event(service, calendar_id=calendar_id, event_id=keep_id, event=body)
            try:
                from gcalsync.service import delete_event
                for ev in core_events[1:]:
                    ev_id = str(ev.get("id") or "")
                    if ev_id:
                        delete_event(service, calendar_id=calendar_id, event_id=ev_id)
            except Exception:
                pass
            return {"ok": True, "action": "updated_deduped", "event_id": keep_id, "event": updated}

        # Legacy cleanup even if marker not present.
        try:
            from gcalsync.service import delete_event
            day_events = list_events(
                service,
                calendar_id=calendar_id,
                time_min=day_start.isoformat(),
                time_max=(day_start + timedelta(days=1)).isoformat(),
                max_results=20,
            )
            training_like = []
            for ev in day_events or []:
                summary = str(ev.get("summary") or "").strip()
                creator = (ev.get("creator") or {}).get("email")
                if summary.startswith("Training") and str(creator or "").strip().lower() == str(cfg.default_calendar_id).lower():
                    training_like.append(ev)
            if len(training_like) > 1:
                training_like.sort(key=lambda e: str(e.get("updated") or ""), reverse=True)
                for ev in training_like[1:]:
                    ev_id = str(ev.get("id") or "")
                    if ev_id:
                        delete_event(service, calendar_id=calendar_id, event_id=ev_id)
        except Exception:
            pass

        created = insert_event(service, calendar_id=calendar_id, event=body)
        ev_id = str(created.get("id") or "")
        return {"ok": True, "action": "created", "event_id": ev_id, "event": created}
    except Exception as exc:
        return {"ok": False, "error": "calendar_write_failed", "detail": str(exc)}


def _clear_training_calendar_events(day_iso: str) -> dict[str, Any]:
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service, delete_event, list_events
    except Exception:
        return {"ok": False, "error": "calendar_module_unavailable"}
    try:
        cfg = load_config()
        service = build_service(cfg.credentials_file, cfg.token_file)
        calendar_id = str(getattr(cfg, "training_calendar_id", "") or cfg.default_calendar_id or "primary")
        tz = ZoneInfo(cfg.timezone)
        day_start = datetime.combine(date.fromisoformat(day_iso), dt_time(0, 0), tzinfo=tz)
        day_events = list_events(
            service,
            calendar_id=calendar_id,
            time_min=day_start.isoformat(),
            time_max=(day_start + timedelta(days=1)).isoformat(),
            max_results=25,
        )
        removed = 0
        for event in day_events or []:
            props = (event.get("extendedProperties") or {}).get("private") if isinstance(event.get("extendedProperties"), dict) else {}
            if not (isinstance(props, dict) and str(props.get("liva_source") or "").strip().lower() == "core_training"):
                continue
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            delete_event(service, calendar_id=calendar_id, event_id=event_id)
            removed += 1
        return {"ok": True, "removed": removed}
    except Exception as exc:
        return {"ok": False, "error": "calendar_clear_failed", "detail": str(exc)}


def _training_summary(training_type: str, session_name: str | None) -> str:
    base = (session_name or "").strip()
    if base:
        return f"Training · {base}"
    if training_type == "run":
        return "Training · Run"
    return "Training · Gym"


def plan_training_window(
    day_iso: str,
    *,
    decision: dict[str, Any] | None = None,
    force_recompute: bool = False,
    write_calendar: bool = True,
    now: datetime | None = None,
    horizon_sync: bool = False,
) -> dict[str, Any]:
    started_total = datetime.now(BERLIN)
    now = now or datetime.now(BERLIN)
    try:
        day = date.fromisoformat(day_iso)
    except Exception:
        day = now.date()
        day_iso = day.isoformat()

    decision = decision if isinstance(decision, dict) else {}
    with timed_block("plan_training_window.load_autopilot", day=day_iso):
        autopilot = _load_autopilot_day_output(day_iso) or {}
    with timed_block("plan_training_window.plan_intent", day=day_iso):
        plan_intent = _training_intent_from_plan(day_iso)

    kind = str(decision.get("kind") or autopilot.get("type") or "").strip().lower()
    training_today = kind in {"run", "plan"}
    training_type = "run" if kind == "run" else ("gym" if kind == "plan" else None)
    if not training_today:
        training_today = bool(plan_intent.get("training_today"))
        training_type = plan_intent.get("training_type") if plan_intent.get("training_today") else None

    session_name = None
    if training_today:
        session_name = str(decision.get("session_name") or autopilot.get("title") or plan_intent.get("title") or "").strip() or None
    preferred_time = plan_intent.get("training_time")
    preferred_min = _parse_hhmm(preferred_time)

    if not training_today:
        schedule = {
            "day": day_iso,
            "training_today": False,
            "status": "rest_day",
            "reason": "no_training_intent",
            "training_type": None,
            "preferred_time": preferred_time,
        }
        if write_calendar and day >= now.date():
            schedule["calendar_sync"] = _clear_training_calendar_events(day_iso)
        with timed_block("plan_training_window.write_schedule_rest", day=day_iso):
            _write_schedule(day_iso, _signature({"day": day_iso, "training_today": False}), schedule)
        record_timing("plan_training_window.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, status="rest_day")
        return schedule

    run_kind = str(decision.get("run_kind") or "").strip().lower() or "z2"
    run_duration = decision.get("run_duration_min")
    if run_duration is None and isinstance((autopilot.get("blocks") or {}).get("run"), dict):
        run_duration = (autopilot.get("blocks") or {}).get("run", {}).get("duration_min")
    if training_type == "run":
        try:
            duration_min = int(run_duration or (45 if run_kind == "z2" else 30))
        except Exception:
            duration_min = 45 if run_kind == "z2" else 30
    else:
        base = 70 if decision.get("minimal_gym") else 80
        duration_min = int(base)

    prep_min = 15 if training_type == "gym" else 10
    post_buffer_min = 20 if training_type == "gym" else 15
    total_block_min = int(duration_min + prep_min + post_buffer_min)

    with timed_block("plan_training_window.calendar", day=day_iso):
        calendar_ctx = _collect_calendar_events(day_iso)
    with timed_block("plan_training_window.school", day=day_iso):
        school_ctx = _schedule_from_latest_file(day_iso)
    with timed_block("plan_training_window.meals", day=day_iso):
        meals = _load_meal_plan(day_iso)

    day_start = datetime.combine(day, dt_time(0, 0), tzinfo=BERLIN)
    day_end = day_start + timedelta(days=1)
    busy_intervals: list[tuple[int, int]] = []
    if calendar_ctx.get("available"):
        tz = ZoneInfo(calendar_ctx.get("timezone") or "Europe/Berlin")
        for event in calendar_ctx.get("events") or []:
            props = (event.get("extendedProperties") or {}).get("private") if isinstance(event.get("extendedProperties"), dict) else {}
            if isinstance(props, dict) and str(props.get("liva_source") or "").strip().lower() == "core_training":
                continue
            interval = _event_to_interval(event, day_start=day_start, day_end=day_end, tz=tz)
            if interval:
                busy_intervals.append(interval)
    if school_ctx.get("available"):
        for st, en in school_ctx.get("intervals") or []:
            busy_intervals.append((int(st), int(en)))
    busy_intervals = _merge_intervals(busy_intervals)

    meal_sig = [m.get("time_min") for m in meals]
    cal_sig = busy_intervals
    input_sig_payload = {
        "day": day_iso,
        "training_today": True,
        "training_type": training_type,
        "session_name": session_name,
        "duration": duration_min,
        "prep_min": prep_min,
        "post_buffer_min": post_buffer_min,
        "preferred": preferred_time,
        "calendar": cal_sig,
        "meals": meal_sig,
    }
    input_sig = _signature(input_sig_payload)

    with timed_block("plan_training_window.load_schedule", day=day_iso):
        existing = _load_schedule(day_iso)
    if existing and not force_recompute:
        if str(existing.get("signature") or "").strip() == input_sig:
            if not write_calendar:
                record_timing("plan_training_window.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, status="cache_hit")
                return existing
            cal_sync = existing.get("calendar_sync") if isinstance(existing.get("calendar_sync"), dict) else {}
            if cal_sync.get("ok") is True:
                record_timing("plan_training_window.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, status="calendar_synced")
                return existing
        if day == now.date():
            ex_start = _parse_hhmm((existing.get("window") or {}).get("training_start"))
            if ex_start is not None and ex_start + 10 >= (_parse_hhmm(now.strftime("%H:%M")) or 0):
                # allow stickiness if still feasible
                pass

    earliest_base = 6 * 60 + 30
    latest_end_base = 22 * 60
    now_min = _parse_hhmm(now.strftime("%H:%M")) or 0
    if day == now.date():
        earliest_base = max(earliest_base, now_min + 25)

    late_threshold = 21 * 60 + 15
    next_day_ctx = _schedule_from_latest_file((day + timedelta(days=1)).isoformat())
    next_day_early = False
    if next_day_ctx.get("school_start"):
        next_start = _parse_hhmm(next_day_ctx.get("school_start"))
        if next_start is not None and next_start <= 8 * 60:
            next_day_early = True
            late_threshold = 20 * 60 + 30

    candidates: list[dict[str, Any]] = []
    step = 15
    start_min = earliest_base
    latest_start = latest_end_base - total_block_min
    if latest_start < earliest_base:
        latest_start = earliest_base
    with timed_block("plan_training_window.candidate_loop", day=day_iso):
        while start_min <= latest_start:
            prep_start = start_min - prep_min
            training_end = start_min + duration_min
            post_end = training_end + post_buffer_min
            if prep_start < 0 or post_end > (24 * 60):
                start_min += step
                continue
            window = (prep_start, post_end)
            conflict = _overlap_minutes(window, busy_intervals)
            conflict_score = _clamp(1.0 - (conflict / max(1, total_block_min)))
            meal_score = _meal_alignment_score(start_min, training_end, meals)
            feasibility_score = 1.0 if conflict == 0 else 0.4
            lateness_score = _lateness_score(post_end, late_threshold)
            recovery_score = _recovery_score(post_end, next_day_early)
            pref_score = _preferred_time_score(start_min, preferred_min)
            weights = {
                "feasibility_score": feasibility_score,
                "calendar_conflict_score": conflict_score,
                "meal_alignment_score": meal_score,
                "lateness_cost_score": lateness_score,
                "recovery_cost_score": recovery_score,
                "preference_score": pref_score,
            }
            note = "" if conflict == 0 else "calendar_overlap"
            candidates.append(
                _window_payload(
                    prep_start=prep_start,
                    training_start=start_min,
                    training_end=training_end,
                    post_buffer_end=post_end,
                    scores=weights,
                    conflict_minutes=conflict,
                    total_minutes=total_block_min,
                    note=note,
                )
            )
            start_min += step

    if preferred_min is not None and all(w.get("training_start") != _to_hhmm(preferred_min) for w in candidates):
        pref_start = preferred_min
        prep_start = pref_start - prep_min
        training_end = pref_start + duration_min
        post_end = training_end + post_buffer_min
        if prep_start >= 0 and post_end <= (24 * 60):
            window = (prep_start, post_end)
            conflict = _overlap_minutes(window, busy_intervals)
            conflict_score = _clamp(1.0 - (conflict / max(1, total_block_min)))
            meal_score = _meal_alignment_score(pref_start, training_end, meals)
            feasibility_score = 1.0 if conflict == 0 else 0.4
            lateness_score = _lateness_score(post_end, late_threshold)
            recovery_score = _recovery_score(post_end, next_day_early)
            pref_score = _preferred_time_score(pref_start, preferred_min)
            weights = {
                "feasibility_score": feasibility_score,
                "calendar_conflict_score": conflict_score,
                "meal_alignment_score": meal_score,
                "lateness_cost_score": lateness_score,
                "recovery_cost_score": recovery_score,
                "preference_score": pref_score,
            }
            candidates.append(
                _window_payload(
                    prep_start=prep_start,
                    training_start=pref_start,
                    training_end=training_end,
                    post_buffer_end=post_end,
                    scores=weights,
                    conflict_minutes=conflict,
                    total_minutes=total_block_min,
                    note="preferred_time",
                )
            )

    if not candidates:
        fallback = None
        if day == now.date():
            now_min = _parse_hhmm(now.strftime("%H:%M")) or 0
            if now_min < 22 * 60:
                fb_start = min(22 * 60, now_min + 25)
                fb_end = min(23 * 60, fb_start + max(35, duration_min - 20))
                if fb_end > fb_start:
                    fallback = {
                        "training_start": _to_hhmm(fb_start),
                        "training_end": _to_hhmm(fb_end),
                        "note": "short_fallback_window",
                    }
        schedule = {
            "day": day_iso,
            "training_today": True,
            "training_type": training_type,
            "session_name": session_name,
            "status": "no_window_available",
            "reason": "no_candidate_window",
            "preferred_time": preferred_time,
            "duration_min": duration_min,
            "prep_min": prep_min,
            "post_buffer_min": post_buffer_min,
            "fallback": fallback,
            "signature": input_sig,
        }
        if write_calendar and day >= now.date():
            schedule["calendar_sync"] = _clear_training_calendar_events(day_iso)
        _write_schedule(day_iso, input_sig, schedule)
        record_timing("plan_training_window.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, status="no_window_available")
        return schedule

    # Prefer conflict-free windows when available.
    if any(int(w.get("conflict_minutes") or 0) == 0 for w in candidates):
        candidates = [w for w in candidates if int(w.get("conflict_minutes") or 0) == 0]

    candidates.sort(key=lambda w: (-_safe_float(w.get("score_total"), 0.0), w.get("training_start")))
    best = candidates[0]

    if existing and isinstance(existing.get("window"), dict) and not force_recompute:
        ex = existing.get("window") or {}
        ex_start = _parse_hhmm(ex.get("training_start"))
        ex_end = _parse_hhmm(ex.get("training_end"))
        if ex_start is not None and ex_end is not None:
            # Keep existing if close and still feasible
            best_start = _parse_hhmm(best.get("training_start"))
            if best_start is not None and abs(best_start - ex_start) <= 20:
                best = existing.get("window")

    window = best
    if isinstance(window, dict) and "scores" in window:
        score_total = window.get("score_total")
    else:
        score_total = best.get("score_total") if isinstance(best, dict) else None

    reasoning = {
        "best_window_reasoning": "Ausgewählt nach Kalender-Fit, Mahlzeiten-Alignment und Spät-Kosten.",
        "scores": best.get("scores") if isinstance(best, dict) else {},
        "score_total": score_total,
    }

    busy_minutes = sum(max(0, en - st) for st, en in busy_intervals)
    schedule = {
        "day": day_iso,
        "training_today": True,
        "training_type": training_type,
        "session_name": session_name,
        "status": "scheduled",
        "preferred_time": preferred_time,
        "duration_min": duration_min,
        "prep_min": prep_min,
        "post_buffer_min": post_buffer_min,
        "window": window,
        "candidates": candidates[:8],
        "reasoning": reasoning,
        "calendar": {
            "available": bool(calendar_ctx.get("available")),
            "events_count": len(calendar_ctx.get("events") or []),
            "calendar_ids": calendar_ctx.get("calendar_ids") or [],
            "busy_minutes": int(busy_minutes),
        },
        "busy_intervals": [{"start": _to_hhmm(st), "end": _to_hhmm(en)} for st, en in busy_intervals][:12],
        "signature": input_sig,
    }

    if write_calendar and day >= now.date() and isinstance(window, dict):
        summary = _training_summary(training_type or "gym", session_name)
        marker_id = f"core_training:{day_iso}:{training_type or 'training'}"
        description = f"CORE geplant: smartes Zeitfenster inkl. Kalender-Check. Session: {session_name or '—'}."
        start_hhmm = window.get("training_start")
        end_hhmm = window.get("training_end")
        if start_hhmm and end_hhmm:
            cal = _sync_calendar_event(
                day_iso=day_iso,
                summary=summary,
                start_hhmm=start_hhmm,
                end_hhmm=end_hhmm,
                marker_id=marker_id,
                description=description,
            )
            schedule["calendar_sync"] = cal
            schedule["calendar"]["marker_id"] = marker_id
            schedule["calendar"]["summary"] = summary

    _write_schedule(day_iso, input_sig, schedule)
    if horizon_sync and write_calendar:
        try:
            plan_training_horizon(day_iso, days=7, write_calendar=True, include_anchor=False, now=now)
        except Exception:
            pass
    record_timing("plan_training_window.total", (datetime.now(BERLIN) - started_total).total_seconds(), day=day_iso, status=str(schedule.get("status") or "scheduled"))
    return schedule


def resolve_training_context(day_iso: str, *, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    schedule = plan_training_window(day_iso, decision=decision, horizon_sync=False, write_calendar=False)
    window = schedule.get("window") if isinstance(schedule, dict) else None
    training_time = None
    if isinstance(window, dict):
        training_time = window.get("training_start")
    return {
        "available": bool(schedule),
        "training_today": bool(schedule.get("training_today")) if isinstance(schedule, dict) else False,
        "training_type": schedule.get("training_type") if isinstance(schedule, dict) else None,
        "training_time": training_time,
        "training_window": window,
        "schedule": schedule,
    }


def plan_training_horizon(
    anchor_day_iso: str,
    *,
    days: int = 7,
    write_calendar: bool = True,
    include_anchor: bool = True,
    force_recompute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(BERLIN)
    try:
        anchor = date.fromisoformat(anchor_day_iso)
    except Exception:
        anchor = now.date()
        anchor_day_iso = anchor.isoformat()
    total = max(1, min(21, int(days or 7)))
    items: list[dict[str, Any]] = []
    start_offset = 0 if include_anchor else 1
    for offset in range(start_offset, total):
        day = anchor + timedelta(days=offset)
        schedule = plan_training_window(
            day.isoformat(),
            decision=None,
            force_recompute=force_recompute,
            write_calendar=write_calendar,
            now=now,
            horizon_sync=False,
        )
        items.append(schedule)
    return {
        "anchor_day": anchor_day_iso,
        "days": total,
        "items": items,
    }


__all__ = [
    "ensure_core_training_schedule_schema",
    "plan_training_window",
    "plan_training_horizon",
    "resolve_training_context",
]
