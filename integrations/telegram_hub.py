from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
import urllib.parse as urllib_parse
import urllib.request as urllib_request
from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from analysis.training_analysis import classify_status_from_history, compare_top_sets, compute_trend_from_history, get_exercise_history
from database.connections import get_core_db, get_nutrition_db, get_plans_db, get_training_db
from database.sqlite_backup import latest_backup_timestamp
from jobs.daily_backup import load_backup_metadata, run_daily_backup
from training.physique_gallery import create_physique_update, parse_physique_caption
from security.write_guard import get_pi_stats
from nutrition.nutrition_planning_db import (
    ensure_nutrition_planning_schema,
    get_logging_day_payload,
    log_planned_meal,
    search_logging_foods,
    create_free_logged_meal,
    set_planned_meal_status,
    list_foods,
    list_meal_templates,
    resolve_meal_template,
)
from nutrition.core_nutrition_engine import (
    evaluate_core_nutrition_day,
    get_core_nutrition_actions_for_day,
    mark_core_action_rejected,
    mark_core_action_telegram_confirmed,
    mark_last_core_action_telegram_confirmed,
    mark_last_core_action_rejected,
)
from nutrition.core_nutrition_daystate import append_day_event, build_active_day_plan
from core.core_daily_decision import record_daily_decision_feedback
from smart_home.intervention_lights import pulse_light
from integrations.ntfy_client import send_ntfy_notification

ENV_FILES = (
    "/var/lib/liva/.secrets/telegram.env",
    "/opt/liva/.env",
)

TELEGRAM_DOMAINS: dict[str, dict[str, tuple[str, ...]]] = {
    "system": {
        "token_keys": ("TG_SYSTEM_BOT_TOKEN", "TG_BOT_TOKEN"),
        "chat_keys": ("TG_SYSTEM_CHAT_ID", "TG_CHAT_ID"),
    },
    "training": {
        "token_keys": ("TG_TRAINING_BOT_TOKEN",),
        "chat_keys": ("TG_TRAINING_CHAT_ID",),
    },
    "nutrition": {
        "token_keys": ("TG_NUTRITION_BOT_TOKEN", "TG_BOT_TOKEN"),
        "chat_keys": ("TG_NUTRITION_CHAT_ID", "TG_CHAT_ID"),
    },
    "core": {
        "token_keys": ("TG_CORE_BOT_TOKEN",),
        "chat_keys": ("TG_CORE_CHAT_ID",),
    },
}

YES_TOKENS = {"ja", "j", "yes", "y"}
NO_TOKENS = {"nein", "n", "no"}
OPTION_TOKENS = {
    "1": "keep",
    "2": "rep_range",
    "3": "rpe_down",
    "4": "variation",
    "5": "core_decides",
}

QUERY_WEEKLY_TOKENS = {"wochencheck", "woche", "wochen-check", "weekly", "weeklycheck", "weekly-check", "checkin", "check-in"}
VARIATION_ALIASES = {
    "kh": "Kurzhantel",
    "kurzhantel": "Kurzhantel",
    "db": "Kurzhantel",
    "dumbbell": "Kurzhantel",
    "lh": "Langhantel",
    "langhantel": "Langhantel",
    "bb": "Langhantel",
    "barbell": "Langhantel",
    "smith": "Smith",
    "maschine": "Maschine",
    "machine": "Maschine",
    "cable": "Kabel",
    "kabel": "Kabel",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ParsedEvent:
    title: str
    date_iso: str
    start_time: str | None
    end_time: str | None
    all_day: bool = False


@dataclass(frozen=True)
class ParsedShiftDay:
    date_iso: str
    title: str
    start_time: str
    end_time: str
    employee: str
    raw_block: str
    slot_lines: list[str]


def _dt_for_calendar(date_iso: str, hhmm: str, tz_name: str) -> dict[str, str]:
    tz = ZoneInfo(tz_name)
    ts = datetime.fromisoformat(f"{date_iso}T{hhmm}:00").replace(tzinfo=tz).isoformat()
    return {"dateTime": ts, "timeZone": tz_name}


def _parse_date_token(token: str, *, today: date | None = None) -> date | None:
    raw = str(token or "").strip()
    token_norm = raw.lower()
    token_norm = token_norm.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    token_norm = re.sub(r"\s+", " ", token_norm).strip()
    token_norm = re.sub(r"^(am|an|naechsten|naechster|naechste|next)\s+", "", token_norm).strip()

    ref = today or datetime.now(ZoneInfo("Europe/Berlin")).date()
    relative_map = {
        "heute": 0,
        "today": 0,
        "morgen": 1,
        "tomorrow": 1,
        "uebermorgen": 2,
        "ubermorgen": 2,
        "overmorgen": 2,
    }
    if token_norm in relative_map:
        return ref + timedelta(days=int(relative_map[token_norm]))

    weekday_map = {
        "montag": 0,
        "mo": 0,
        "monday": 0,
        "dienstag": 1,
        "di": 1,
        "tuesday": 1,
        "mittwoch": 2,
        "mi": 2,
        "wednesday": 2,
        "donnerstag": 3,
        "do": 3,
        "thursday": 3,
        "freitag": 4,
        "fr": 4,
        "friday": 4,
        "samstag": 5,
        "sa": 5,
        "sonnabend": 5,
        "saturday": 5,
        "sonntag": 6,
        "so": 6,
        "sunday": 6,
    }
    if token_norm in weekday_map:
        target = int(weekday_map[token_norm])
        delta = (target - ref.weekday()) % 7
        return ref + timedelta(days=delta)

    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*", str(token or ""))
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year_raw = m.group(3)
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except Exception:
            return None
    try:
        candidate = date(ref.year, month, day)
    except Exception:
        return None
    if candidate < ref:
        try:
            return date(ref.year + 1, month, day)
        except Exception:
            return None
    return candidate


def _parse_hhmm(token: str) -> str | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(token or ""))
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def _hhmm_to_minutes(token: str) -> int | None:
    hhmm = _parse_hhmm(token)
    if not hhmm:
        return None
    try:
        hh, mm = hhmm.split(":", 1)
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _fmt_date_de(date_iso: str) -> str:
    try:
        d = date.fromisoformat(str(date_iso))
        return d.strftime("%d.%m.%Y")
    except Exception:
        return str(date_iso)


def parse_quick_calendar_text(text: str, *, today: date | None = None) -> ParsedEvent | None:
    raw = str(text or "").strip()
    parts = [p.strip() for p in raw.split(",", 2)]
    if len(parts) not in {2, 3}:
        return None
    title, date_tok = parts[0], parts[1]
    if not title:
        return None
    day_obj = _parse_date_token(date_tok, today=today)
    if day_obj is None:
        return None
    if len(parts) == 2:
        return ParsedEvent(
            title=title,
            date_iso=day_obj.isoformat(),
            start_time=None,
            end_time=None,
            all_day=True,
        )
    time_tok = parts[2]
    if "-" in time_tok:
        start_raw, end_raw = time_tok.split("-", 1)
        start_hhmm = _parse_hhmm(start_raw)
        end_hhmm = _parse_hhmm(end_raw)
        if not start_hhmm or not end_hhmm:
            return None
        start_dt = datetime.fromisoformat(f"{day_obj.isoformat()}T{start_hhmm}:00")
        end_dt = datetime.fromisoformat(f"{day_obj.isoformat()}T{end_hhmm}:00")
        if end_dt <= start_dt:
            return None
    else:
        start_hhmm = _parse_hhmm(time_tok)
        if not start_hhmm:
            return None
        start_dt = datetime.fromisoformat(f"{day_obj.isoformat()}T{start_hhmm}:00")
        end_dt = start_dt + timedelta(minutes=60)
        end_hhmm = end_dt.strftime("%H:%M")
    return ParsedEvent(
        title=title,
        date_iso=day_obj.isoformat(),
        start_time=start_hhmm,
        end_time=end_hhmm,
        all_day=False,
    )


def _looks_like_monthly_plan(text: str) -> bool:
    raw = str(text or "")
    if "plan für" not in raw.lower():
        return False
    day_lines = len(re.findall(r"(?m)^\s*\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s+\S+\s*:", raw))
    return day_lines >= 3


def _extract_month_year_hint(text: str) -> tuple[int | None, int | None]:
    m = re.search(r"plan\s+für\s+([a-zäöü]+)\s+(\d{4})", str(text or ""), flags=re.IGNORECASE)
    if not m:
        return None, None
    mon_name = str(m.group(1) or "").strip().lower()
    year = int(m.group(2))
    month_map = {
        "januar": 1,
        "februar": 2,
        "märz": 3,
        "maerz": 3,
        "april": 4,
        "mai": 5,
        "juni": 6,
        "juli": 7,
        "august": 8,
        "september": 9,
        "oktober": 10,
        "november": 11,
        "dezember": 12,
    }
    return month_map.get(mon_name), year


def _extract_slot_range(line: str) -> tuple[str, str] | None:
    m = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(?:uhr)?",
        str(line or ""),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    sh = int(m.group(1))
    sm = int(m.group(2) or 0)
    eh = int(m.group(3))
    em = int(m.group(4) or 0)
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return None
    start = f"{sh:02d}:{sm:02d}"
    end = f"{eh:02d}:{em:02d}"
    if end <= start:
        return None
    return start, end


def _extract_head_time(line: str) -> str | None:
    m = re.search(r"(\d{1,2}:\d{2})\s*$", str(line or ""))
    if m:
        return _parse_hhmm(m.group(1))
    m = re.search(r"\b(\d{1,2})\s*Uhr\b", str(line or ""), flags=re.IGNORECASE)
    if not m:
        return None
    hh = int(m.group(1))
    if hh < 0 or hh > 23:
        return None
    return f"{hh:02d}:00"


def parse_monthly_shift_plan(text: str, target_name: str = "") -> list[ParsedShiftDay]:
    raw = str(text or "")
    target_name = str(target_name or os.getenv("LIVA_SHIFT_TARGET_NAME", "")).strip()
    if not target_name:
        return []
    lines = raw.splitlines()
    header_re = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*[A-Za-zÄÖÜäöü]{0,4}\s*:\s*(.+?)\s*$")
    hint_month, hint_year = _extract_month_year_hint(raw)
    today_local = datetime.now(ZoneInfo("Europe/Berlin")).date()
    results: list[ParsedShiftDay] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = header_re.match(line)
        if not m:
            i += 1
            continue
        day = int(m.group(1))
        month = int(m.group(2))
        year_raw = m.group(3)
        head_tail = str(m.group(4) or "").strip()
        block_lines = [line.rstrip()]
        j = i + 1
        while j < len(lines) and not header_re.match(lines[j]):
            block_lines.append(lines[j].rstrip())
            j += 1
        i = j
        if not re.search(r"\b" + re.escape(target_name) + r"\b", head_tail, flags=re.IGNORECASE):
            continue
        year: int
        if year_raw:
            year = int(year_raw) + (2000 if int(year_raw) < 100 else 0)
        elif hint_year is not None and (hint_month is None or hint_month == month):
            year = int(hint_year)
        else:
            year = int(today_local.year)
        try:
            day_obj = date(year, month, day)
        except Exception:
            continue
        head_time = _extract_head_time(head_tail)
        slot_lines: list[str] = []
        slot_ranges: list[tuple[str, str]] = []
        for body_line in block_lines[1:]:
            body = str(body_line or "").strip()
            if not body:
                continue
            parsed = _extract_slot_range(body)
            if not parsed:
                continue
            slot_lines.append(body)
            slot_ranges.append(parsed)
        if head_time:
            start_time = head_time
        elif slot_ranges:
            start_time = min(x[0] for x in slot_ranges)
        else:
            continue
        if slot_ranges:
            end_time = max(x[1] for x in slot_ranges)
        else:
            start_dt = datetime.fromisoformat(f"{day_obj.isoformat()}T{start_time}:00")
            end_time = (start_dt + timedelta(hours=3)).strftime("%H:%M")
        results.append(
            ParsedShiftDay(
                date_iso=day_obj.isoformat(),
                title="SportWelt Schicht",
                start_time=start_time,
                end_time=end_time,
                employee=target_name,
                raw_block="\n".join(block_lines).strip(),
                slot_lines=slot_lines,
            )
        )
    return results


def _shift_day_description(day: ParsedShiftDay) -> str:
    slots = "\n".join(f"- {line}" for line in day.slot_lines) if day.slot_lines else "- (keine erkannt)"
    details = (
        f"Erkannter Mitarbeiter: {day.employee}\n"
        f"Erkannter Tag: {_fmt_date_de(day.date_iso)}\n"
        f"Erkannter Schichtstart: {day.start_time}\n"
        f"Erkannter Schichtende: {day.end_time}\n"
        f"Erkannte Einzeltermine:\n{slots}\n"
    )
    return f"{details}\nOriginal-Block:\n{day.raw_block}"


def create_or_update_calendar_event(
    *,
    service: Any,
    calendar_id: str,
    summary: str,
    date_iso: str,
    start_time: str | None,
    end_time: str | None,
    timezone_name: str,
    source: str,
    marker_id: str,
    description: str = "",
    all_day: bool = False,
) -> dict[str, Any]:
    from gcalsync.service import insert_event, list_events, update_event

    if all_day:
        next_day = (date.fromisoformat(date_iso) + timedelta(days=1)).isoformat()
        start_obj: dict[str, str] = {"date": date_iso}
        end_obj: dict[str, str] = {"date": next_day}
    else:
        if not start_time or not end_time:
            raise ValueError("timed_event_requires_start_end")
        start_obj = _dt_for_calendar(date_iso, start_time, timezone_name)
        end_obj = _dt_for_calendar(date_iso, end_time, timezone_name)
    body = {
        "summary": str(summary).strip(),
        "description": str(description or "").strip(),
        "start": start_obj,
        "end": end_obj,
        "extendedProperties": {
            "private": {
                "liva_source": str(source or "").strip(),
                "liva_event_id": str(marker_id or "").strip(),
            }
        },
    }
    if all_day:
        existing = list_events(
            service,
            calendar_id=calendar_id,
            private_extended_property=[f"liva_event_id={marker_id}"],
        )
    else:
        existing = list_events(
            service,
            calendar_id=calendar_id,
            time_min=_dt_for_calendar(date_iso, "00:00", timezone_name)["dateTime"],
            time_max=_dt_for_calendar(date_iso, "23:59", timezone_name)["dateTime"],
            private_extended_property=[f"liva_event_id={marker_id}"],
        )
    if existing:
        out = update_event(service, calendar_id=calendar_id, event_id=str(existing[0]["id"]), event=body)
        return {"ok": True, "action": "updated", "event": out}
    out = insert_event(service, calendar_id=calendar_id, event=body)
    return {"ok": True, "action": "created", "event": out}


def create_calendar_event_from_telegram(text: str) -> dict[str, Any]:
    parsed = parse_quick_calendar_text(text)
    if parsed is None:
        return {"ok": False, "error": "parse_failed"}
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service

        cfg = load_config()
        service = build_service(cfg.credentials_file, cfg.token_file)
        marker_tag = "allday" if parsed.all_day else str(parsed.start_time or "")
        marker_id = f"telegram_quickadd:{parsed.date_iso}:{marker_tag}:{_normalize_text(parsed.title)[:64]}"
        res = create_or_update_calendar_event(
            service=service,
            calendar_id=cfg.default_calendar_id,
            summary=parsed.title,
            date_iso=parsed.date_iso,
            start_time=parsed.start_time,
            end_time=parsed.end_time,
            timezone_name=cfg.timezone,
            source="telegram_quickadd",
            marker_id=marker_id,
            all_day=bool(parsed.all_day),
        )
        return {"ok": True, "parsed": parsed, "calendar": res}
    except Exception as exc:
        return {"ok": False, "error": "calendar_write_failed", "detail": str(exc)}


def import_shift_plan_to_calendar(text: str, *, target_name: str = "") -> dict[str, Any]:
    days = parse_monthly_shift_plan(text, target_name=target_name)
    if not days:
        return {"ok": True, "count": 0, "items": []}
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service

        cfg = load_config()
        service = build_service(cfg.credentials_file, cfg.token_file)
        created_items: list[dict[str, str]] = []
        for day in days:
            marker_id = f"shiftplan:{_normalize_text(day.employee)}:{day.date_iso}:{day.start_time}"
            create_or_update_calendar_event(
                service=service,
                calendar_id=cfg.default_calendar_id,
                summary=day.title,
                date_iso=day.date_iso,
                start_time=day.start_time,
                end_time=day.end_time,
                timezone_name=cfg.timezone,
                source="shiftplan",
                marker_id=marker_id,
                description=_shift_day_description(day),
            )
            created_items.append(
                {
                    "date_iso": day.date_iso,
                    "start_time": day.start_time,
                    "end_time": day.end_time,
                }
            )
        return {"ok": True, "count": len(created_items), "items": created_items}
    except Exception as exc:
        return {"ok": False, "error": "calendar_write_failed", "detail": str(exc)}


def _read_env_file_value(path: str, key: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                found_key, value = line.split("=", 1)
                if found_key.strip() != key:
                    continue
                return value.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def _resolve_env_key(keys: tuple[str, ...]) -> str:
    for key in keys:
        direct = (os.environ.get(key) or "").strip()
        if direct:
            return direct
        for path in ENV_FILES:
            value = _read_env_file_value(path, key)
            if value:
                return value
    return ""


def resolve_domain_config(domain: str) -> dict[str, str]:
    spec = TELEGRAM_DOMAINS.get(str(domain or "").strip().lower()) or {}
    token = _resolve_env_key(tuple(spec.get("token_keys") or ()))
    chat_id = _resolve_env_key(tuple(spec.get("chat_keys") or ()))
    return {
        "domain": str(domain or "").strip().lower(),
        "token": token,
        "chat_id": chat_id,
    }


def _telegram_push_muted() -> bool:
    """Read the global Telegram notification switch used by /settings."""
    try:
        conn = get_training_db()
        try:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key='telegram_push_muted_until_ts' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] in (None, ""):
            return False
        return int(str(row[0]).strip()) >= int(datetime.now(timezone.utc).timestamp())
    except (TypeError, ValueError, sqlite3.Error):
        return False


def _api_call(token: str, method: str, payload: dict[str, Any], *, timeout_s: int = 15) -> dict[str, Any]:
    body = urllib_parse.urlencode(payload).encode("utf-8")
    req = urllib_request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body, method="POST")
    with urllib_request.urlopen(req, timeout=max(5, int(timeout_s))) as response:
        raw = response.read().decode("utf-8")
    try:
        data = json.loads(raw)
    except Exception:
        data = {"ok": False, "description": "invalid_json", "raw": raw}
    return data if isinstance(data, dict) else {"ok": False, "description": "invalid_payload"}


def _api_get_file_bytes(token: str, file_id: str) -> tuple[bytes | None, str | None, str | None]:
    fid = str(file_id or "").strip()
    if not token or not fid:
        return None, None, None
    try:
        meta = _api_call(token, "getFile", {"file_id": fid})
        if not bool(meta.get("ok")):
            return None, None, None
        result = meta.get("result") if isinstance(meta.get("result"), dict) else {}
        path = str(result.get("file_path") or "").strip()
        if not path:
            return None, None, None
        url = f"https://api.telegram.org/file/bot{token}/{path}"
        with urllib_request.urlopen(url, timeout=20) as response:
            content = response.read()
            mime = str(response.headers.get_content_type() or "").strip() or None
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return content, mime, filename
    except Exception:
        return None, None, None


def send_domain_message(
    domain: str,
    text: str,
    *,
    chat_id: str | int | None = None,
    reply_to_message_id: int | None = None,
    disable_notification: bool = False,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | list[Any] | str | None = None,
    respect_push_settings: bool = True,
) -> dict[str, Any]:
    if respect_push_settings and _telegram_push_muted():
        return {
            "ok": True,
            "sent": False,
            "suppressed": True,
            "reason": "telegram_push_muted",
            "domain": str(domain or "").strip().lower(),
        }
    config = resolve_domain_config(domain)
    target_chat_id = str(chat_id or "").strip() or config.get("chat_id") or ""
    if not config.get("token") or not target_chat_id or not str(text or "").strip():
        return {"ok": False, "error": "missing_config"}
    payload: dict[str, Any] = {
        "chat_id": target_chat_id,
        "text": str(text).strip(),
        "disable_notification": "true" if disable_notification else "false",
    }
    if parse_mode:
        payload["parse_mode"] = str(parse_mode).strip()
    if reply_markup:
        payload["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup, ensure_ascii=False)
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    try:
        data = _api_call(config["token"], "sendMessage", payload)
    except Exception as exc:
        return {"ok": False, "error": "request_failed", "detail": str(exc)}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return {
        "ok": bool(data.get("ok")),
        "data": data,
        "message_id": int(result.get("message_id") or 0) if result.get("message_id") is not None else None,
        "chat_id": str((result.get("chat") or {}).get("id") or target_chat_id),
    }


def answer_callback_query(
    domain: str,
    callback_query_id: str,
    *,
    text: str = "",
    show_alert: bool = False,
) -> dict[str, Any]:
    config = resolve_domain_config(domain)
    cb_id = str(callback_query_id or "").strip()
    if not config.get("token") or not cb_id:
        return {"ok": False, "error": "missing_config"}
    payload: dict[str, Any] = {
        "callback_query_id": cb_id,
        "show_alert": "true" if show_alert else "false",
    }
    if text:
        payload["text"] = str(text).strip()[:180]
    try:
        data = _api_call(config["token"], "answerCallbackQuery", payload)
    except Exception as exc:
        return {"ok": False, "error": "request_failed", "detail": str(exc)}
    return {"ok": bool(data.get("ok")), "data": data}


def ensure_telegram_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_bot_offsets (
                domain TEXT PRIMARY KEY,
                update_offset INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                chat_id TEXT,
                outbound_message_id INTEGER,
                proposal_type TEXT NOT NULL,
                exercise_name TEXT,
                current_variation TEXT,
                proposed_variation TEXT,
                reason_text TEXT,
                response_text TEXT,
                response_message_id INTEGER,
                created_at TEXT NOT NULL,
                responded_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_notice_state (
                notice_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_sent_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_meal_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                day_iso TEXT NOT NULL,
                meal_slot_id INTEGER NULL,
                meal_slot_index INTEGER NULL,
                meal_title TEXT NULL,
                referenced_time TEXT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                source_message_type TEXT NOT NULL DEFAULT 'unknown',
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                closed_at TEXT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_meal_context_lookup
            ON telegram_meal_context(domain, chat_id, state, day_iso, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_meal_message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                chat_id TEXT NULL,
                message_type TEXT NOT NULL,
                day_iso TEXT NOT NULL,
                meal_slot_id INTEGER NULL,
                core_action_id INTEGER NULL,
                context_id INTEGER NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                sent_message_id INTEGER NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_meal_message_log_day
            ON telegram_meal_message_log(domain, day_iso, message_type, id DESC)
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_domain_offset(domain: str) -> int:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT update_offset FROM telegram_bot_offsets WHERE domain=? LIMIT 1",
            (str(domain or "").strip().lower(),),
        ).fetchone()
        return int(row["update_offset"] or 0) if row else 0
    finally:
        conn.close()


def set_domain_offset(domain: str, offset: int) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO telegram_bot_offsets (domain, update_offset, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                update_offset=excluded.update_offset,
                updated_at=excluded.updated_at
            """,
            (str(domain or "").strip().lower(), int(offset or 0), _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_updates(domain: str, *, timeout: int = 0) -> list[dict[str, Any]]:
    config = resolve_domain_config(domain)
    if not config.get("token"):
        return []
    offset = get_domain_offset(domain)
    payload: dict[str, Any] = {"timeout": max(0, int(timeout or 0))}
    if offset > 0:
        payload["offset"] = offset
    try:
        req_timeout = max(10, int(payload.get("timeout") or 0) + 8)
        data = _api_call(config["token"], "getUpdates", payload, timeout_s=req_timeout)
    except Exception:
        return []
    results = data.get("result") if isinstance(data.get("result"), list) else []
    highest = offset
    updates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            update_id = int(item.get("update_id") or 0)
        except Exception:
            update_id = 0
        if update_id > 0:
            highest = max(highest, update_id + 1)
        updates.append(item)
    if highest > offset:
        set_domain_offset(domain, highest)
    return updates


def normalize_reply_token(text: str | None) -> str:
    raw = str(text or "").strip().lower()
    raw = re.sub(r"[^0-9a-zA-ZäöüÄÖÜß]+", "", raw).lower()
    return raw


def _normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _format_number(value: Any) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.1f}".rstrip("0").rstrip(".")


def _format_decimal_de(value: Any, *, decimals: int = 1, signed: bool = False) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    if abs(num) < (0.5 * (10 ** (-decimals))):
        signed = False
        num = 0.0
    fmt = f"{{:{'+' if signed else ''},.{decimals}f}}".format(num)
    return fmt.replace(",", "X").replace(".", ",").replace("X", ".")


def _format_int_de(value: Any) -> str:
    try:
        num = int(round(float(value or 0)))
    except Exception:
        num = 0
    return f"{num:,}".replace(",", ".")


def _format_kg(value: Any, *, decimals: int = 0) -> str:
    num = _safe_float(value)
    if num is None:
        return "- kg"
    if decimals <= 0:
        return f"{_format_int_de(num)} kg"
    return f"{_format_decimal_de(num, decimals=decimals)} kg"


def _h(value: Any) -> str:
    return html_escape(str(value or ""))


def _join_blocks(blocks: list[str]) -> str:
    return "\n".join(block for block in blocks if str(block or "").strip())


def _e1rm(weight: Any, reps: Any) -> float | None:
    w = _safe_float(weight)
    r = _safe_float(reps)
    if w is None or r is None or w <= 0 or r <= 0:
        return None
    return w * (1.0 + (r / 30.0))


def _normalize_variation_query(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    low = _normalize_text(raw)
    return VARIATION_ALIASES.get(low, raw)


def _query_tokens_from_text(text: str) -> tuple[str, str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return "", ""
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    if len(parts) >= 2:
        return parts[0], _normalize_variation_query(parts[1])
    return cleaned, ""


def _current_week_bounds(today: datetime | None = None) -> tuple[datetime, datetime]:
    now = today or datetime.now(timezone.utc)
    today_date = now.date()
    week_start = today_date - timedelta(days=today_date.weekday())
    week_end = week_start + timedelta(days=6)
    return datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc), datetime.combine(week_end, datetime.min.time(), tzinfo=timezone.utc)


def _current_week_label(today: datetime | None = None) -> str:
    now = today or datetime.now(timezone.utc)
    iso = now.date().isocalendar()
    return f"KW {iso.week}"


def _load_active_plan_days() -> list[dict[str, Any]]:
    conn, row = _load_active_gym_plan_row()
    try:
        if not row:
            return []
        plan_json = _load_json_obj(row["plan_json"])
        return plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    finally:
        conn.close()


def _weekday_index(day_name: str) -> int:
    order = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3, "Fr": 4, "Sa": 5, "So": 6}
    return order.get(str(day_name or "").strip(), 99)


def _find_active_plan_item(name: str, variation: str = "") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | tuple[None, None, None]:
    target_name = _normalize_text(name)
    target_variation = _normalize_text(variation)
    for day in _load_active_plan_days():
        if not isinstance(day, dict):
            continue
        for event in (day.get("events") or []):
            if not isinstance(event, dict) or str(event.get("kind") or "").strip().lower() != "gym":
                continue
            for item in (event.get("items") or []):
                if not isinstance(item, dict) or str(item.get("kind") or "").strip().lower() != "exercise":
                    continue
                if _normalize_text(item.get("name")) != target_name:
                    continue
                if target_variation and _normalize_text(item.get("variation")) != target_variation:
                    continue
                return day, event, item
    return None, None, None


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"Fortschritt": 0, "stabil": 0, "regress": 0}
    for idx in range(1, len(rows)):
        prev = rows[idx - 1]
        cur = rows[idx]
        progressed = compare_top_sets(cur, prev)
        if progressed is True:
            counts["Fortschritt"] += 1
            continue
        if progressed is False:
            pw = _safe_float(prev.get("weight"))
            cw = _safe_float(cur.get("weight"))
            pr = _safe_float(prev.get("reps"))
            cr = _safe_float(cur.get("reps"))
            if pw is not None and cw is not None and abs(cw - pw) <= 1e-9 and pr is not None and cr is not None and abs(cr - pr) <= 1e-9:
                counts["stabil"] += 1
            else:
                counts["regress"] += 1
            continue
        counts["stabil"] += 1
    return counts


def _exercise_report_text(candidate: dict[str, str]) -> str:
    name = str(candidate.get("name") or "").strip()
    variation = str(candidate.get("variation") or "").strip()
    conn = get_training_db()
    try:
        history = get_exercise_history(conn, name, variation=variation if variation else None)
    finally:
        conn.close()
    if not history:
        return f"Übungsreport · {name}{f' ({variation})' if variation else ''}\n\nKeine Log-Historie gefunden."

    latest = history[-1]
    previous = history[-2] if len(history) >= 2 else None
    last_five = history[-6:]
    counts = _status_counts(last_five)
    total_compares = max(1, sum(counts.values()))
    latest_e1rm = _e1rm(latest.get("weight"), latest.get("reps"))
    previous_e1rm = _e1rm(previous.get("weight"), previous.get("reps")) if previous else None
    best_row = max(history, key=lambda row: _e1rm(row.get("weight"), row.get("reps")) or 0.0)
    best_e1rm = _e1rm(best_row.get("weight"), best_row.get("reps")) or 0.0
    current_vs_best = (latest_e1rm - best_e1rm) if latest_e1rm is not None else None
    trend = compute_trend_from_history(history[-4:], lookback=min(4, len(history)))
    trend_text = {"up": "zieht an", "flat": "flat", "down": "leicht wackelig"}.get(trend, "unklar")

    recent_same_weight = [row for row in history[-8:] if _safe_float(row.get("weight")) == _safe_float(latest.get("weight"))][-4:]
    reps_line = " / ".join(_format_number(row.get("reps")) for row in recent_same_weight) if recent_same_weight else "-"
    rpe_line = " / ".join(_format_decimal_de(row.get("rpe"), decimals=1) for row in recent_same_weight) if recent_same_weight else "-"

    day, event, item = _find_active_plan_item(name, variation)
    plan_lines: list[str] = []
    if item:
        rep_min = int(((item.get("reps") or {}).get("min") or latest.get("reps") or 0))
        rep_max = int(((item.get("reps") or {}).get("max") or rep_min))
        rpe_list = list(item.get("rpe_list") or []) if isinstance(item.get("rpe_list"), list) else []
        target_reps = min(rep_max, int((_safe_float(latest.get("reps")) or rep_min)) + 1)
        cap = max((_safe_float(v) or 0.0) for v in rpe_list) if rpe_list else (_safe_float(latest.get("rpe")) or 8.0)
        low_cap = max(6.0, cap - 0.5)
        plan_lines = [
            "Plan (nächste Einheit)",
            f"Ziel: {_format_number(latest.get('weight'))} kg × {target_reps}",
            f"Cap: RPE {_format_decimal_de(cap, decimals=1)}",
            f"Falls müde: {_format_number(latest.get('weight'))} kg × {rep_min}–{max(rep_min, target_reps - 1)} @RPE {_format_decimal_de(low_cap, decimals=1)}",
        ]
    else:
        plan_lines = [
            "Plan",
            "Kein aktiver Plan-Slot gefunden.",
        ]

    delta_rep = ""
    delta_e1rm = ""
    if previous:
        rep_delta = (_safe_float(latest.get("reps")) or 0.0) - (_safe_float(previous.get("reps")) or 0.0)
        rpe_delta = (_safe_float(latest.get("rpe")) or 0.0) - (_safe_float(previous.get("rpe")) or 0.0)
        if _safe_float(latest.get("weight")) == _safe_float(previous.get("weight")):
            delta_rep = f"Δ: {rep_delta:+.0f} Wdh"
        else:
            weight_delta = (_safe_float(latest.get("weight")) or 0.0) - (_safe_float(previous.get("weight")) or 0.0)
            delta_rep = f"Δ: {_format_decimal_de(weight_delta, decimals=1, signed=True)} kg"
        if latest_e1rm is not None and previous_e1rm is not None:
            delta_e1rm = f"Δe1RM: {_format_decimal_de(latest_e1rm - previous_e1rm, decimals=1, signed=True)} kg"
        else:
            delta_e1rm = f"RPE {_format_decimal_de(rpe_delta, decimals=1, signed=True)}"

    title = f"Übungsreport · {name}{f' ({variation})' if variation else ''}"
    blocks = [
        f"<b>{_h(title)}</b>",
        _join_blocks(
            [
                "<b>Letzter Log</b>",
                f"{_h(latest.get('date'))}: <b>{_h(_format_number(latest.get('weight')))} kg × {_h(_format_number(latest.get('reps')))}</b> @RPE {_h(_format_decimal_de(latest.get('rpe'), decimals=1))}",
            ]
        ),
        _join_blocks(
            [
                "<b>Vorher</b>",
                (
                    f"{_h(previous.get('date'))}: {_h(_format_number(previous.get('weight')))} kg × {_h(_format_number(previous.get('reps')))} "
                    f"@RPE {_h(_format_decimal_de(previous.get('rpe'), decimals=1))}"
                    if previous else "-"
                ),
                f"{_h(delta_rep)} | {_h(delta_e1rm)}" if previous else "-",
            ]
        ),
        _join_blocks(
            [
                "<b>Letzte 5 Vergleiche</b>",
                f"<b>Fortschritt:</b> {_h(counts['Fortschritt'])}",
                f"<b>stabil:</b> {_h(counts['stabil'])}",
                f"<b>regress:</b> {_h(counts['regress'])}",
                f"<b>Quote:</b> {_h(_format_decimal_de((counts['Fortschritt'] / total_compares) * 100.0, decimals=0))}% positiv",
            ]
        ),
        _join_blocks(
            [
                "<b>Historie</b>",
                f"<b>Exposures:</b> {_h(len(history))}",
                (
                    f"<b>Best:</b> {_h(_format_number(best_row.get('weight')))} kg × {_h(_format_number(best_row.get('reps')))} "
                    f"(e1RM ≈ {_h(_format_decimal_de(best_e1rm, decimals=1))} kg)"
                ),
                (
                    f"<b>Aktuell vs Best-e1RM:</b> {_h(_format_decimal_de(current_vs_best, decimals=1, signed=True))} kg "
                    f"(≈ {_h(_format_decimal_de(latest_e1rm, decimals=1))} vs {_h(_format_decimal_de(best_e1rm, decimals=1))})"
                    if current_vs_best is not None and latest_e1rm is not None else "<b>Aktuell vs Best-e1RM:</b> -"
                ),
            ]
        ),
        _join_blocks(
            [
                "<b>Trend (letzte 4 Exposures)</b>",
                f"<b>Reps @{_h(_format_number(latest.get('weight')))} kg:</b> {_h(reps_line)}",
                f"<b>ØRPE:</b> {_h(rpe_line)}",
                f"<b>Richtung:</b> {_h(trend_text)}",
            ]
        ),
        _join_blocks(
            [
                f"<b>{_h(plan_lines[0])}</b>",
                *[_h(line) for line in plan_lines[1:]],
            ]
        ),
    ]
    return "\n\n".join(block for block in blocks if block)


def _training_pr_notice_key(workout_id: int, candidate: dict[str, Any] | None = None) -> str:
    if not candidate:
        return f"training:pr_alert:workout:{int(workout_id)}"
    parts = [
        str(candidate.get("name") or "").strip().lower(),
        str(candidate.get("variation") or "").strip().lower(),
        str(candidate.get("device") or "").strip().lower(),
        str(candidate.get("laterality") or "").strip().lower(),
    ]
    slug = re.sub(r"[^a-z0-9äöüß]+", "-", "|".join(parts)).strip("-") or "exercise"
    return f"training:pr_alert:workout:{int(workout_id)}:{slug}"


def _training_pr_last_seen_key() -> str:
    return "training:pr_alert:last_seen_workout_id"


def _training_pr_last_seen_workout_id() -> int:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT raw_json FROM telegram_notice_state WHERE notice_key=? LIMIT 1",
            (_training_pr_last_seen_key(),),
        ).fetchone()
        if not row:
            return 0
        raw = _load_json_obj(row["raw_json"])
        return int(raw.get("last_seen_workout_id") or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def _record_training_pr_last_seen(workout_id: int) -> None:
    _record_notice_status(
        _training_pr_last_seen_key(),
        "ok",
        {"last_seen_workout_id": int(workout_id or 0)},
    )


def _get_notice_status(notice_key: str) -> str:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM telegram_notice_state WHERE notice_key=? LIMIT 1",
            (notice_key,),
        ).fetchone()
        return str(row["status"] or "").strip() if row else ""
    finally:
        conn.close()


def _record_notice_status(notice_key: str, status: str, payload: dict[str, Any] | None = None) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO telegram_notice_state (notice_key, status, last_sent_at, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(notice_key) DO UPDATE SET
                status=excluded.status,
                last_sent_at=excluded.last_sent_at,
                raw_json=excluded.raw_json
            """,
            (
                str(notice_key or "").strip(),
                str(status or "").strip(),
                _utc_now(),
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _recent_logged_workouts(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT w.id, w.date_iso, COALESCE(w.name, '') AS name
        FROM workouts w
        WHERE EXISTS (
            SELECT 1
            FROM exercises e
            JOIN sets s ON s.exercise_id=e.id
            WHERE e.workout_id=w.id
              AND s.weight IS NOT NULL
              AND s.reps IS NOT NULL
        )
        ORDER BY w.date_iso DESC, w.id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def _workout_e1rm_pr_candidates(conn: sqlite3.Connection, workout: dict[str, Any]) -> list[dict[str, Any]]:
    workout_id = int(workout.get("id") or 0)
    workout_date = str(workout.get("date_iso") or "").strip()
    if workout_id <= 0 or not workout_date:
        return []
    exercise_rows = conn.execute(
        """
        SELECT
            e.id AS exercise_id,
            TRIM(COALESCE(e.name, '')) AS name,
            TRIM(COALESCE(e.variation, '')) AS variation,
            TRIM(COALESCE(e.device, '')) AS device,
            TRIM(COALESCE(e.laterality, '')) AS laterality,
            s.weight,
            s.reps,
            s.rpe
        FROM exercises e
        JOIN sets s ON s.exercise_id=e.id
        WHERE e.workout_id=?
          AND s.weight IS NOT NULL
          AND s.reps IS NOT NULL
        ORDER BY e.id ASC, s.weight DESC, COALESCE(s.reps, -1) DESC, s.id ASC
        """,
        (workout_id,),
    ).fetchall()

    top_by_exercise: dict[int, dict[str, Any]] = {}
    for row in exercise_rows:
        data = dict(row)
        ex_id = int(data.get("exercise_id") or 0)
        if ex_id > 0 and ex_id not in top_by_exercise:
            top_by_exercise[ex_id] = data

    candidates: list[dict[str, Any]] = []
    for row in top_by_exercise.values():
        name = str(row.get("name") or "").strip()
        variation = str(row.get("variation") or "").strip()
        device = str(row.get("device") or "").strip()
        laterality = str(row.get("laterality") or "").strip()
        current_e1rm = _e1rm(row.get("weight"), row.get("reps"))
        if not name or current_e1rm is None:
            continue
        previous_rows = conn.execute(
            """
            WITH top_sets AS (
                SELECT
                    w.date_iso AS date,
                    w.id AS workout_id,
                    s.weight,
                    s.reps,
                    s.rpe,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.id
                        ORDER BY s.weight DESC, COALESCE(s.reps, -1) DESC, s.id ASC
                    ) AS rn
                FROM exercises e
                JOIN workouts w ON w.id=e.workout_id
                JOIN sets s ON s.exercise_id=e.id
                WHERE LOWER(TRIM(COALESCE(e.name, '')))=LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(e.variation, '')))=LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(e.device, '')))=LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(e.laterality, '')))=LOWER(TRIM(?))
                  AND w.id <> ?
                  AND s.weight IS NOT NULL
                  AND s.reps IS NOT NULL
            )
            SELECT date, workout_id, weight, reps, rpe
            FROM top_sets
            WHERE rn=1
            """,
            (name, variation, device, laterality, workout_id),
        ).fetchall()
        previous_best: dict[str, Any] | None = None
        previous_best_e1rm: float | None = None
        for prev in previous_rows:
            prev_data = dict(prev)
            prev_e1rm = _e1rm(prev_data.get("weight"), prev_data.get("reps"))
            if prev_e1rm is None:
                continue
            if previous_best_e1rm is None or prev_e1rm > previous_best_e1rm:
                previous_best_e1rm = prev_e1rm
                previous_best = prev_data
        if previous_best_e1rm is None or previous_best is None:
            continue
        delta = current_e1rm - previous_best_e1rm
        if delta <= 0.05:
            continue
        candidates.append(
            {
                "name": name,
                "variation": variation,
                "device": device,
                "laterality": laterality,
                "weight": row.get("weight"),
                "reps": row.get("reps"),
                "rpe": row.get("rpe"),
                "e1rm": current_e1rm,
                "previous": previous_best,
                "previous_e1rm": previous_best_e1rm,
                "delta": delta,
            }
        )
    candidates.sort(key=lambda item: float(item.get("delta") or 0.0), reverse=True)
    return candidates


def _training_pr_alert_text(candidate: dict[str, Any]) -> str:
    variation = str(candidate.get("variation") or "").strip()
    label = f"{candidate.get('name')}{f' ({variation})' if variation else ''}"
    return "\n".join(
        [
            f"PR 🚀 {_h(label)}",
            f"Neues e1RM: {_h(_format_decimal_de(candidate.get('e1rm'), decimals=1))} kg ({_h(_format_decimal_de(candidate.get('delta'), decimals=1, signed=True))})",
            f"Top-Set: {_h(_format_number(candidate.get('reps')))} x {_h(_format_decimal_de(candidate.get('weight'), decimals=1))}kg",
        ]
    )


def _maybe_send_training_pr_alert() -> dict[str, Any]:
    ensure_telegram_schema()
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        workouts = _recent_logged_workouts(conn)
        if not workouts:
            return {"ok": True, "sent": False, "reason": "no_logged_training"}
        latest_seen_id = max(int(workout.get("id") or 0) for workout in workouts)
        previous_seen_id = _training_pr_last_seen_workout_id()
        new_workouts = [workout for workout in workouts if int(workout.get("id") or 0) > previous_seen_id]
        if previous_seen_id > 0 and not new_workouts:
            return {"ok": True, "sent": False, "reason": "no_new_logged_training"}
        scan_workouts = new_workouts or workouts
        sent_count = 0
        sent_workout_ids: list[int] = []
        for workout in scan_workouts:
            workout_id = int(workout.get("id") or 0)
            candidates = _workout_e1rm_pr_candidates(conn, workout)
            if not candidates:
                _record_notice_status(_training_pr_notice_key(workout_id), "no_pr", {"workout_id": workout_id})
                continue
            for candidate in candidates:
                notice_key = _training_pr_notice_key(workout_id, candidate)
                if _get_notice_status(notice_key) == "sent":
                    continue
                text = _training_pr_alert_text(candidate)
                sent = send_ntfy_notification(
                    text,
                    title="LIVA · Training PR",
                    priority=4,
                    tags="trophy,muscle",
                )
                if not sent.get("ok") or not sent.get("sent"):
                    return {"ok": False, "sent": False, "error": sent.get("error") or "send_failed", "workout_id": workout_id}
                _record_notice_status(
                    notice_key,
                    "sent",
                    {
                        "workout_id": workout_id,
                        "channel": "ntfy",
                        "candidate": candidate,
                    },
                )
                sent_count += 1
                if workout_id not in sent_workout_ids:
                    sent_workout_ids.append(workout_id)
        if sent_count > 0:
            _record_training_pr_last_seen(latest_seen_id)
            return {"ok": True, "sent": True, "workout_ids": sent_workout_ids, "count": sent_count}
        if scan_workouts:
            _record_training_pr_last_seen(latest_seen_id)
        return {"ok": True, "sent": False, "reason": "no_new_logged_training"}
    finally:
        conn.close()


def _active_plan_exercise_candidates() -> list[dict[str, str]]:
    conn, row = _load_active_gym_plan_row()
    try:
        if not row:
            return []
        plan_json = _load_json_obj(row["plan_json"])
    finally:
        conn.close()
    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for day in days:
        if not isinstance(day, dict):
            continue
        day_name = str(day.get("day") or "").strip()
        for event in (day.get("events") or []):
            if not isinstance(event, dict) or str(event.get("kind") or "").strip().lower() != "gym":
                continue
            session_title = str(event.get("title") or "").strip()
            for item in (event.get("items") or []):
                if not isinstance(item, dict) or str(item.get("kind") or "").strip().lower() != "exercise":
                    continue
                name = str(item.get("name") or "").strip()
                variation = str(item.get("variation") or "").strip()
                if not name:
                    continue
                key = (name.lower(), variation.lower(), day_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "name": name,
                        "variation": variation,
                        "day": day_name,
                        "session_title": session_title,
                    }
                )
    return candidates


def _find_query_candidate(exercise_query: str, variation_query: str = "") -> dict[str, str] | None:
    name_query = _normalize_text(exercise_query)
    variation_query_norm = _normalize_text(variation_query)
    if not name_query:
        return None
    plan_candidates = _active_plan_exercise_candidates()
    exact_plan = [
        item for item in plan_candidates
        if name_query in _normalize_text(item.get("name"))
        and (
            not variation_query_norm
            or variation_query_norm in _normalize_text(item.get("variation"))
        )
    ]
    if exact_plan:
        return exact_plan[0]

    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              TRIM(e.name) AS name,
              TRIM(COALESCE(e.variation,'')) AS variation,
              TRIM(COALESCE(e.device,'')) AS device,
              MAX(w.date_iso) AS last_date,
              COUNT(DISTINCT w.id) AS workouts
            FROM exercises e
            JOIN workouts w ON w.id = e.workout_id
            WHERE LOWER(TRIM(e.name)) LIKE '%' || ? || '%'
            GROUP BY TRIM(e.name), TRIM(COALESCE(e.variation,'')), TRIM(COALESCE(e.device,''))
            ORDER BY workouts DESC, last_date DESC
            LIMIT 25
            """,
            (name_query,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    scored: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        name = str(row["name"] or "").strip()
        variation = str(row["variation"] or "").strip()
        device = str(row["device"] or "").strip()
        haystack = " ".join(part for part in [name, variation, device] if part).lower()
        score = 0
        if _normalize_text(name) == name_query:
            score += 10
        elif name_query in _normalize_text(name):
            score += 6
        if variation_query_norm:
            if variation_query_norm == _normalize_text(variation):
                score += 6
            elif variation_query_norm == _normalize_text(device):
                score += 5
            elif variation_query_norm in haystack:
                score += 3
        score += min(5, int(row["workouts"] or 0))
        scored.append(
            (
                score,
                {
                    "name": name,
                    "variation": variation or device,
                    "day": "",
                    "session_title": "",
                },
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


def _exercise_query_summary_text(exercise_query: str, variation_query: str = "") -> str:
    candidate = _find_query_candidate(exercise_query, variation_query)
    if not candidate:
        detail = f" ({variation_query})" if variation_query else ""
        return f"Kein Match für {exercise_query}{detail} gefunden."
    return _exercise_report_text(candidate)


def _week_summary_row(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          COUNT(DISTINCT w.id) AS workouts,
          COUNT(s.id) AS sets_count,
          SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN s.weight * s.reps ELSE 0 END) AS tonnage_sum,
          AVG(CASE WHEN s.rpe IS NOT NULL THEN s.rpe END) AS rpe_avg
        FROM workouts w
        JOIN exercises e ON e.workout_id = w.id
        JOIN sets s ON s.exercise_id = e.id
        WHERE w.date_iso >= ? AND w.date_iso <= ?
        """,
        (start_iso, end_iso),
    ).fetchone()
    workouts = int(row["workouts"] or 0) if row else 0
    sets_count = int(row["sets_count"] or 0) if row else 0
    tonnage_sum = float(row["tonnage_sum"] or 0.0) if row else 0.0
    rpe_avg = _safe_float(row["rpe_avg"]) if row else None
    return {
        "workouts": workouts,
        "sets_count": sets_count,
        "tonnage_sum": tonnage_sum,
        "rpe_avg": rpe_avg,
        "avg_sets_per_workout": (sets_count / workouts) if workouts > 0 else 0.0,
    }


def _weekly_focus(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> str:
    row = conn.execute(
        """
        SELECT w.name, COUNT(*) AS c
        FROM workouts w
        WHERE w.date_iso >= ? AND w.date_iso <= ?
        GROUP BY w.name
        ORDER BY c DESC, MAX(w.date_iso) DESC
        LIMIT 1
        """,
        (start_iso, end_iso),
    ).fetchone()
    return str(row["name"] or "-").strip() if row else "-"


def _best_movers_this_week(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> list[str]:
    active_keys = {
        (_normalize_text(item.get("name")), _normalize_text(item.get("variation")))
        for item in _active_plan_exercise_candidates()
    }
    rows = conn.execute(
        """
        SELECT DISTINCT TRIM(e.name) AS name, TRIM(COALESCE(e.variation,'')) AS variation
        FROM exercises e
        JOIN workouts w ON w.id = e.workout_id
        WHERE w.date_iso >= ? AND w.date_iso <= ?
        """,
        (start_iso, end_iso),
    ).fetchall()
    movers: list[tuple[float, str]] = []
    for row in rows:
        name = str(row["name"] or "").strip()
        variation = str(row["variation"] or "").strip()
        if active_keys and (_normalize_text(name), _normalize_text(variation)) not in active_keys:
            continue
        history = get_exercise_history(conn, name, variation=variation if variation else None)
        if len(history) < 2:
            continue
        latest = history[-1]
        previous = history[-2]
        latest_date = str(latest.get("date") or "")
        if latest_date < start_iso or latest_date > end_iso:
            continue
        latest_e1rm = _e1rm(latest.get("weight"), latest.get("reps")) or 0.0
        previous_e1rm = _e1rm(previous.get("weight"), previous.get("reps")) or 0.0
        delta = latest_e1rm - previous_e1rm
        rep_delta = (_safe_float(latest.get("reps")) or 0.0) - (_safe_float(previous.get("reps")) or 0.0)
        weight_delta = (_safe_float(latest.get("weight")) or 0.0) - (_safe_float(previous.get("weight")) or 0.0)
        if delta <= 0 and rep_delta <= 0 and weight_delta <= 0:
            continue
        if rep_delta > 0 and weight_delta >= 0:
            label = f"{name}{f' ({variation})' if variation else ''}: {rep_delta:+.0f} Wdh @ {_format_number(latest.get('weight'))} kg"
        elif weight_delta > 0:
            label = f"{name}{f' ({variation})' if variation else ''}: {_format_decimal_de(weight_delta, decimals=1, signed=True)} kg"
        else:
            continue
        movers.append((max(delta, 0.0) + max(0.0, rep_delta) + max(0.0, weight_delta), label))
    movers.sort(key=lambda item: item[0], reverse=True)
    return [label for _, label in movers[:3]]


def _training_gap_days(conn: sqlite3.Connection) -> int | None:
    rows = conn.execute(
        "SELECT date_iso FROM workouts ORDER BY date_iso DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return None
    try:
        latest = datetime.fromisoformat(str(rows[0]["date_iso"])).date()
        previous = datetime.fromisoformat(str(rows[1]["date_iso"])).date()
    except Exception:
        return None
    return max(0, (latest - previous).days)


def _next_plan_slot() -> tuple[str, str]:
    days = _load_active_plan_days()
    if not days:
        return "-", "-"
    today_idx = datetime.now(timezone.utc).date().weekday()
    ordered = sorted(
        [day for day in days if any(str(ev.get("kind") or "").strip().lower() == "gym" for ev in (day.get("events") or []))],
        key=lambda day: ((_weekday_index(day.get("day")) - today_idx) % 7, _weekday_index(day.get("day"))),
    )
    if not ordered:
        return "-", "-"
    day = ordered[0]
    event = next(
        (ev for ev in (day.get("events") or []) if str(ev.get("kind") or "").strip().lower() == "gym"),
        {},
    )
    return str(day.get("day") or "-").strip(), str(event.get("title") or "Gym").strip() or "Gym"


def _next_plan_slot_context(*, include_today: bool = True) -> dict[str, Any]:
    day, session = _next_plan_slot()
    return {"day": day, "session_title": session, "has_override": False, "items": []}


def _weekly_training_check_text(*, weeks: int = 8) -> str:
    now = datetime.now(timezone.utc)
    week_start_dt, week_end_dt = _current_week_bounds(now)
    prev_start_dt = week_start_dt - timedelta(days=7)
    prev_end_dt = week_end_dt - timedelta(days=7)
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        current = _week_summary_row(conn, week_start_dt.date().isoformat(), week_end_dt.date().isoformat())
        prev = _week_summary_row(conn, prev_start_dt.date().isoformat(), prev_end_dt.date().isoformat())
        focus = _weekly_focus(conn, week_start_dt.date().isoformat(), week_end_dt.date().isoformat())
        best_movers = _best_movers_this_week(conn, week_start_dt.date().isoformat(), week_end_dt.date().isoformat())
        gap_days = _training_gap_days(conn)
    except Exception:
        current = {"workouts": 0, "sets_count": 0, "tonnage_sum": 0.0, "rpe_avg": None, "avg_sets_per_workout": 0.0}
        prev = {"workouts": 0, "sets_count": 0, "tonnage_sum": 0.0, "rpe_avg": None, "avg_sets_per_workout": 0.0}
        focus = "-"
        best_movers = []
        gap_days = None
    finally:
        conn.close()

    try:
        from core.core_control_room import _memory_catalog

        plateau_items = [
            item for item in _memory_catalog()
            if item.get("type") == "Exercise" and item.get("status") not in {"ignored", "archived"}
        ][:3]
    except Exception:
        plateau_items = []

    next_day, next_session = _next_plan_slot()
    plateau_focus = [
        item for item in plateau_items
        if str((item.get("raw") or {}).get("session_title") or "") == next_session
    ]
    goal_targets = plateau_focus[:2] if plateau_focus else plateau_items[:2]
    goal_text = ", ".join(
        f"1× {str((item.get('raw') or {}).get('exercise_name') or item.get('title') or '').split(':', 1)[0]}"
        for item in goal_targets
    ) or "1 sauberen PR-Trigger setzen"

    workouts_delta = current["workouts"] - prev["workouts"]
    sets_delta = current["sets_count"] - prev["sets_count"]
    rpe_delta = (current["rpe_avg"] or 0.0) - (prev["rpe_avg"] or 0.0) if current["rpe_avg"] is not None and prev["rpe_avg"] is not None else None
    tonnage_delta = current["tonnage_sum"] - prev["tonnage_sum"]
    tonnage_pct = ((tonnage_delta / prev["tonnage_sum"]) * 100.0) if prev["tonnage_sum"] else None
    if rpe_delta is not None and abs(rpe_delta) < 0.05:
        rpe_delta = 0.0
    blocks = [
        f"📊 Wochencheck · {_h(_current_week_label(now))}",
        _join_blocks(
            [
                "<b>Diese Woche</b>",
                f"<b>Einheiten:</b> {_h(current['workouts'])}",
                f"<b>Sätze:</b> {_h(current['sets_count'])}",
                f"<b>Volumen:</b> {_h(_format_kg(current['tonnage_sum']))}",
                f"<b>ØRPE:</b> {_h(_format_decimal_de(current['rpe_avg'], decimals=1))}" if current["rpe_avg"] is not None else "<b>ØRPE:</b> -",
                f"<b>ØSätze/Einheit:</b> {_h(_format_decimal_de(current['avg_sets_per_workout'], decimals=0))}",
                f"<b>Fokus:</b> {_h(focus)}",
            ]
        ),
        _join_blocks(
            [
                "<b>vs letzte Woche</b>",
                f"<b>Einheiten:</b> {_h(prev['workouts'])} → <b>{_h(current['workouts'])}</b> ({_h(f'{workouts_delta:+d}')})",
                f"<b>Sätze:</b> {_h(prev['sets_count'])} → <b>{_h(current['sets_count'])}</b> ({_h(f'{sets_delta:+d}')})",
                (
                    f"<b>Volumen:</b> {_h(_format_kg(prev['tonnage_sum']))} → <b>{_h(_format_kg(current['tonnage_sum']))}</b> "
                    f"({_h(_format_int_de(tonnage_delta))} kg / {_h(_format_decimal_de(tonnage_pct, decimals=0, signed=True))}%)"
                    if tonnage_pct is not None else
                    f"<b>Volumen:</b> {_h(_format_kg(prev['tonnage_sum']))} → <b>{_h(_format_kg(current['tonnage_sum']))}</b>"
                ),
                (
                    f"<b>ØRPE:</b> {_h(_format_decimal_de(prev['rpe_avg'], decimals=1))} → <b>{_h(_format_decimal_de(current['rpe_avg'], decimals=1))}</b> "
                    f"({_h(_format_decimal_de(rpe_delta, decimals=1, signed=True))})"
                    if rpe_delta is not None else "<b>ØRPE:</b> -"
                ),
            ]
        ),
    ]

    if plateau_items:
        plateau_lines = ["<b>Plateau-Kandidaten (letzte 5 Vergleiche)</b>"]
        for item in plateau_items:
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            name = raw.get("exercise_name") or str(item.get("title") or "").split(":", 1)[0]
            variation = str(raw.get("current_variation") or "").strip()
            display = f"{name} ({variation})" if variation else str(name)
            plateau_lines.append(
                f"• <b>{_h(display)}:</b> {_h(int(raw.get('stalled_count') or 0))}/{_h(int(raw.get('comparison_count') or 0))} = kein Fortschritt"
            )
        blocks.append("\n".join(plateau_lines))
    if best_movers:
        mover_lines = ["<b>Best Movers (diese Woche)</b>"]
        for mover in best_movers:
            mover_lines.append(f"• {_h(mover)}")
        blocks.append("\n".join(mover_lines))
    confidence = "mittel" if current["workouts"] <= 2 else ("hoch" if current["workouts"] >= 4 else "okay")
    confidence_detail = "nur 1 Einheit" if current["workouts"] == 1 else f"{current['workouts']} Einheiten"
    blocks.append(
        _join_blocks(
            [
                "<b>Risiko / Hinweis</b>",
                f"<b>Trainingsrhythmus:</b> {_h(f'Gap {gap_days} Tage' if gap_days is not None else 'Gap unklar')}",
                f"<b>Aussagekraft:</b> {_h(confidence)} ({_h(confidence_detail)})",
            ]
        )
    )
    blocks.append(
        _join_blocks(
            [
                "<b>Nächster Slot</b>",
                f"<b>{_h(next_day)} · {_h(next_session)}</b>",
                f"<b>Fokus:</b> {_h(goal_text)}",
            ]
        )
    )
    return "\n\n".join(block for block in blocks if block)


def send_weekly_training_checkin() -> dict[str, Any]:
    text = _weekly_training_check_text()
    sent = send_domain_message("training", text, disable_notification=False, parse_mode="HTML")
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed"}
    return {"ok": True, "sent": True, "message_id": sent.get("message_id")}


def _proposal_message_text(exercise_name: str, reason_text: str, current_variation: str, proposed_variation: str, raw: dict[str, Any] | None = None) -> str:
    raw = raw if isinstance(raw, dict) else {}
    plan_context = raw.get("plan_context") if isinstance(raw.get("plan_context"), dict) else {}
    current_line = current_variation or "ohne Variation"
    scope = "Aktiver Plan"
    if plan_context:
        scope = " · ".join(
            [
                str(plan_context.get("day") or "").strip() or "-",
                str(plan_context.get("session_title") or "").strip() or "Gym",
            ]
        )
    history_line = ""
    count = plan_context.get("history_count_recent")
    start = str(plan_context.get("history_start") or "").strip()
    end = str(plan_context.get("history_end") or "").strip()
    trend = str(plan_context.get("trend") or "").strip()
    progressed = plan_context.get("progressed")
    stalled_count = int(plan_context.get("stalled_count") or 0)
    comparison_count = int(plan_context.get("comparison_count") or 0)
    if count:
        status_line = "letzter Vergleich ohne Fortschritt" if progressed is False else (f"Trend {trend}" if trend else "Trend unklar")
        if stalled_count > 0 and comparison_count > 0:
            history_line = (
                f"Logs: {count} relevante Exposures ({start} bis {end}), "
                f"{stalled_count}/{comparison_count} letzte Vergleiche ohne Fortschritt, {status_line}."
            )
        else:
            history_line = f"Logs: {count} relevante Exposures ({start} bis {end}), {status_line}."
    return "\n".join(
        [
            "CORE Training",
            f"{scope}",
            f"{exercise_name} ({current_line}) zeigt im aktuellen Plan keinen sauberen Fortschritt.",
            history_line or reason_text,
            "",
            "Optionen:",
            "1 - nichts ändern",
            "2 - andere Rep-Range",
            "3 - Ziel-RPE senken",
            f"4 - Variation wechseln ({proposed_variation})",
            "5 - CORE entscheidet",
            "",
            "Antworte als Reply mit 1, 2, 3, 4 oder 5.",
        ]
    )


def _training_core_status_text() -> str:
    pending = _pending_training_proposal() or {}
    exercise = str(pending.get("exercise_name") or "offener Vorschlag").strip()
    proposed = str(pending.get("proposed_variation") or "Alternative").strip() or "Alternative"
    reason = str(pending.get("reason_text") or "").strip()
    next_slot = _next_plan_slot_context(include_today=True) or {}
    slot_line = " · ".join(
        part for part in [
            str(next_slot.get("day") or "").strip(),
            str(next_slot.get("session_title") or "").strip(),
        ] if part
    ) or "Nächster Slot offen"
    lines = [
        "CORE Training Status",
        slot_line,
    ]
    if pending:
        lines.extend(
            [
                "",
                f"Offen: {exercise}",
                reason,
                "",
                "Antwortoptionen",
                "1 · Standard unverändert",
                "2 · Rep-Range anpassen",
                "3 · RPE-Cap konservativer",
                f"4 · Variante wechseln ({proposed})",
                "5 · CORE-Auswahl",
            ]
        )
    else:
        lines.append("Keine offene Trainingsanpassung.")
    return "\n".join(line for line in lines if line is not None)


def _training_status_text() -> str:
    return _training_core_status_text()


def upsert_training_proposal(
    *,
    source_key: str,
    exercise_name: str,
    current_variation: str,
    proposed_variation: str,
    reason_text: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO telegram_pending_actions (
                source_key, domain, status, proposal_type, exercise_name, current_variation,
                proposed_variation, reason_text, created_at, raw_json
            ) VALUES (?, 'training', 'pending', 'exercise_variation_swap', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                exercise_name=excluded.exercise_name,
                current_variation=excluded.current_variation,
                proposed_variation=excluded.proposed_variation,
                reason_text=excluded.reason_text,
                raw_json=excluded.raw_json
            """,
            (
                source_key,
                str(exercise_name or "").strip(),
                str(current_variation or "").strip(),
                str(proposed_variation or "").strip(),
                str(reason_text or "").strip(),
                _utc_now(),
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM telegram_pending_actions WHERE source_key=? LIMIT 1",
            (source_key,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_pending_training_proposal(source_key: str) -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT * FROM telegram_pending_actions
            WHERE source_key=? AND domain='training'
            LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _pending_training_proposal() -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM telegram_pending_actions
            WHERE domain='training' AND status='pending'
            ORDER BY COALESCE(outbound_message_id, 0) DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_training_proposal_sent(source_key: str, *, message_id: int | None, chat_id: str | None) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            UPDATE telegram_pending_actions
            SET outbound_message_id=COALESCE(?, outbound_message_id),
                chat_id=COALESCE(?, chat_id)
            WHERE source_key=?
            """,
            (int(message_id) if message_id else None, str(chat_id or "").strip() or None, source_key),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_proposal_response(
    source_key: str,
    *,
    status: str,
    response_text: str,
    response_message_id: int | None,
) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            UPDATE telegram_pending_actions
            SET status=?, response_text=?, response_message_id=?, responded_at=?
            WHERE source_key=?
            """,
            (
                status,
                str(response_text or "").strip(),
                int(response_message_id) if response_message_id else None,
                _utc_now(),
                source_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def propose_training_adjustment(
    *,
    source_key: str,
    exercise_name: str,
    current_variation: str,
    proposed_variation: str,
    reason_text: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = get_pending_training_proposal(source_key)
    if existing and str(existing.get("outbound_message_id") or "").strip():
        return {"ok": True, "sent": False, "message_id": int(existing["outbound_message_id"]), "source_key": source_key}
    row = upsert_training_proposal(
        source_key=source_key,
        exercise_name=exercise_name,
        current_variation=current_variation,
        proposed_variation=proposed_variation,
        reason_text=reason_text,
        raw=raw,
    )
    text = _proposal_message_text(exercise_name, reason_text, current_variation, proposed_variation, raw)
    sent = send_domain_message("training", text)
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed", "source_key": source_key}
    mark_training_proposal_sent(source_key, message_id=sent.get("message_id"), chat_id=sent.get("chat_id"))
    return {"ok": True, "sent": True, "message_id": sent.get("message_id"), "source_key": source_key, "row": row}


def _pick_variation_suggestion(exercise_name: str, current_variation: str) -> str:
    ex = _normalize_text(exercise_name)
    cur = _normalize_text(current_variation)
    if "schräg" in ex or "incline" in ex:
        if "smith" in cur:
            return "Kurzhantel"
        if "kurzhantel" in cur or "db" in cur:
            return "Maschine"
        if "maschine" in cur:
            return "Langhantel"
        return "Kurzhantel"
    if "bank" in ex or "bench" in ex:
        if "langhantel" in cur or cur == "lh":
            return "Kurzhantel"
        if "kurzhantel" in cur or "db" in cur:
            return "Maschine"
        return "Kurzhantel"
    if "rudern" in ex or "row" in ex:
        if "kabel" in cur:
            return "Chest Supported"
        return "Kabel"
    if "kniebeuge" in ex or "squat" in ex:
        if "high bar" in cur:
            return "Pause"
        return "High Bar"
    return "Maschine" if cur and "maschine" not in cur else "Kurzhantel"


def build_variation_proposal(exercise_name: str, current_variation: str, reason_text: str) -> dict[str, str]:
    proposed = _pick_variation_suggestion(exercise_name, current_variation)
    return {
        "exercise_name": str(exercise_name or "").strip(),
        "current_variation": str(current_variation or "").strip(),
        "proposed_variation": proposed,
        "reason_text": str(reason_text or "").strip() or "Mehrere Exposures ohne sauberen Vorwärtsschritt.",
    }


def _load_active_gym_plan_row() -> tuple[sqlite3.Connection, sqlite3.Row | None]:
    conn = get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, title, focus, plan_json, rules_json, blocks_json, is_active, is_archived, created_at, updated_at
        FROM gym_plans
        WHERE is_active = 1 AND is_archived = 0
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return conn, row


def _load_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw) if raw else {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _locate_active_plan_item(days: list[dict[str, Any]], target: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | tuple[None, None, None]:
    wanted_item_id = str(target.get("item_id") or "").strip()
    wanted_day = _normalize_text(target.get("day"))
    wanted_session = _normalize_text(target.get("session_title"))
    target_name = _normalize_text(target.get("exercise_name"))
    target_variation = _normalize_text(target.get("current_variation"))
    for day in days:
        if not isinstance(day, dict):
            continue
        day_name = _normalize_text(day.get("day"))
        for event in (day.get("events") or []):
            if not isinstance(event, dict):
                continue
            session_title = _normalize_text(event.get("title"))
            for item in (event.get("items") or []):
                if not isinstance(item, dict) or str(item.get("kind") or "").strip().lower() != "exercise":
                    continue
                item_id = str(item.get("id") or "").strip()
                if wanted_item_id and item_id == wanted_item_id:
                    return day, event, item
                if target_name and _normalize_text(item.get("name")) != target_name:
                    continue
                if target_variation and _normalize_text(item.get("variation")) != target_variation:
                    continue
                if wanted_day and day_name != wanted_day:
                    continue
                if wanted_session and session_title != wanted_session:
                    continue
                return day, event, item
    return None, None, None


def _persist_active_plan(days: list[dict[str, Any]], row: sqlite3.Row, plan_json: dict[str, Any]) -> None:
    plan_json["days"] = days
    plan_json["base_week"] = {
        str(day.get("day") or ""): list(day.get("events") or [])
        for day in days
        if isinstance(day, dict)
    }
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = get_plans_db()
    try:
        conn.execute(
            "UPDATE gym_plans SET plan_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(plan_json, ensure_ascii=False), now, int(row["id"])),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        appmod = importlib.import_module("app")
        if hasattr(appmod, "_row_to_gym_plan") and hasattr(appmod, "_sync_active_legacy_plan_from_gym_record"):
            conn2, row2 = _load_active_gym_plan_row()
            try:
                if row2 is not None:
                    record = appmod._row_to_gym_plan(row2)
                    appmod._sync_active_legacy_plan_from_gym_record(record)
                    if hasattr(appmod, "_autopilot_clear_week_cache"):
                        appmod._autopilot_clear_week_cache()
            finally:
                conn2.close()
    except Exception:
        pass


def _set_core_change_meta(item: dict[str, Any], *, fields: list[str], label: str, value: dict[str, Any] | None = None) -> None:
    item["core_change"] = {
        "id": _utc_now(),
        "source": "core",
        "fields": [str(field).strip() for field in (fields or []) if str(field).strip()],
        "label": str(label or "").strip(),
        "value": value if isinstance(value, dict) else {},
        "seen": False,
        "changed_at": _utc_now(),
        "seen_at": None,
    }


def _replace_variation_in_active_plan(target: dict[str, Any], proposed_variation: str) -> dict[str, Any]:
    conn, row = _load_active_gym_plan_row()
    if not row:
        conn.close()
        return {"ok": False, "error": "no_active_gym_plan"}
    plan_json = _load_json_obj(row["plan_json"])
    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    day, event, item = _locate_active_plan_item(days, target)
    if not item:
        conn.close()
        return {"ok": False, "error": "exercise_not_found"}
    conn.close()
    item["variation"] = str(proposed_variation or "").strip()
    note = str(item.get("note") or "").strip()
    marker = f"CORE: Variation gewechselt auf {proposed_variation}"
    if marker not in note:
        item["note"] = f"{note} | {marker}".strip(" |")
    _set_core_change_meta(
        item,
        fields=["variation"],
        label=f"Variation auf {proposed_variation} geändert",
        value={"variation": str(proposed_variation or "").strip()},
    )
    _persist_active_plan(days, row, plan_json)
    return {
        "ok": True,
        "plan_id": int(row["id"]),
        "day": str(day.get("day") or "").strip(),
        "session_title": str(event.get("title") or "").strip(),
        "exercise_name": str(item.get("name") or "").strip(),
        "current_variation": str(target.get("current_variation") or "").strip(),
        "proposed_variation": proposed_variation,
    }


def _suggest_rep_range(reps: dict[str, Any]) -> tuple[int, int]:
    try:
        rep_min = int((reps or {}).get("min") or 6)
    except Exception:
        rep_min = 6
    try:
        rep_max = int((reps or {}).get("max") or rep_min)
    except Exception:
        rep_max = rep_min
    if rep_max <= 10:
        return (max(6, rep_min + 2), max(rep_min + 2, rep_max + 2))
    return (max(5, rep_min - 2), max(max(5, rep_min - 2), rep_max - 2))


def _apply_rep_range_in_active_plan(target: dict[str, Any]) -> dict[str, Any]:
    conn, row = _load_active_gym_plan_row()
    if not row:
        conn.close()
        return {"ok": False, "error": "no_active_gym_plan"}
    plan_json = _load_json_obj(row["plan_json"])
    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    day, event, item = _locate_active_plan_item(days, target)
    if not item:
        conn.close()
        return {"ok": False, "error": "exercise_not_found"}
    conn.close()
    rep_min_new, rep_max_new = _suggest_rep_range(item.get("reps") if isinstance(item.get("reps"), dict) else {})
    item["reps"] = {"min": rep_min_new, "max": rep_max_new}
    note = str(item.get("note") or "").strip()
    marker = f"CORE: Rep-Range auf {rep_min_new}-{rep_max_new} angepasst"
    if marker not in note:
        item["note"] = f"{note} | {marker}".strip(" |")
    _set_core_change_meta(
        item,
        fields=["reps"],
        label=f"Rep-Range auf {rep_min_new}-{rep_max_new} geändert",
        value={"reps": {"min": rep_min_new, "max": rep_max_new}},
    )
    _persist_active_plan(days, row, plan_json)
    return {
        "ok": True,
        "plan_id": int(row["id"]),
        "day": str(day.get("day") or "").strip(),
        "session_title": str(event.get("title") or "").strip(),
        "exercise_name": str(item.get("name") or "").strip(),
        "rep_range": f"{rep_min_new}-{rep_max_new}",
    }


def _apply_rpe_down_in_active_plan(target: dict[str, Any]) -> dict[str, Any]:
    conn, row = _load_active_gym_plan_row()
    if not row:
        conn.close()
        return {"ok": False, "error": "no_active_gym_plan"}
    plan_json = _load_json_obj(row["plan_json"])
    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    day, event, item = _locate_active_plan_item(days, target)
    if not item:
        conn.close()
        return {"ok": False, "error": "exercise_not_found"}
    conn.close()
    rpe_list = list(item.get("rpe_list") or []) if isinstance(item.get("rpe_list"), list) else []
    if rpe_list:
        item["rpe_list"] = [max(6.0, round(float(v) - 1.0, 1)) for v in rpe_list]
        rpe_text = "/".join(f"{v:g}" for v in item["rpe_list"])
    else:
        rpe_text = "-1.0"
    note = str(item.get("note") or "").strip()
    marker = f"CORE: Ziel-RPE gesenkt ({rpe_text})"
    if marker not in note:
        item["note"] = f"{note} | {marker}".strip(" |")
    _set_core_change_meta(
        item,
        fields=["rpe_list"],
        label=f"Ziel-RPE gesenkt ({rpe_text})",
        value={"rpe_list": list(item.get("rpe_list") or [])},
    )
    _persist_active_plan(days, row, plan_json)
    return {
        "ok": True,
        "plan_id": int(row["id"]),
        "day": str(day.get("day") or "").strip(),
        "session_title": str(event.get("title") or "").strip(),
        "exercise_name": str(item.get("name") or "").strip(),
        "rpe_text": rpe_text,
    }


def _choose_core_action(pending: dict[str, Any], plan_context: dict[str, Any]) -> str:
    current_variation = str(pending.get("current_variation") or "").strip()
    proposed_variation = str(pending.get("proposed_variation") or "").strip()
    rpe_list = plan_context.get("rpe_list") if isinstance(plan_context.get("rpe_list"), list) else []
    stalled_count = int(plan_context.get("stalled_count") or 0)
    progress_count = int(plan_context.get("progress_count") or 0)
    history_count_recent = int(plan_context.get("history_count_recent") or 0)
    trend = str(plan_context.get("trend") or "").strip().lower()
    latest_stalled = bool(plan_context.get("latest_stalled"))
    max_rpe = max((float(v) for v in rpe_list), default=0.0) if rpe_list else 0.0
    avg_rpe = (sum(float(v) for v in rpe_list) / len(rpe_list)) if rpe_list else 0.0

    if current_variation and proposed_variation and history_count_recent >= 8 and stalled_count >= 3 and progress_count <= 1:
        return "variation"
    if rpe_list and latest_stalled and (max_rpe >= 8.5 or avg_rpe >= 8.25 or trend == "down"):
        return "rpe_down"
    if current_variation and proposed_variation and stalled_count >= 4 and trend in {"flat", "down"}:
        return "variation"
    return "rep_range"


def _update_control_room_outcome(source_key: str, outcome_text: str) -> None:
    conn = get_core_db()
    try:
        conn.execute(
            "UPDATE core_decision_log SET outcome_text=? WHERE source_key=?",
            (str(outcome_text or "").strip(), source_key),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def process_training_reply_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"ok": False, "error": "invalid_message"}
    text = str(message.get("text") or "").strip()
    token = normalize_reply_token(text)
    choice = OPTION_TOKENS.get(token)
    if token in YES_TOKENS:
        choice = "core_decides"
    elif token in NO_TOKENS:
        choice = "keep"
    if choice:
        decision = choice
    else:
        return {"ok": False, "ignored": True, "error": "unsupported_reply"}

    reply_to = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    try:
        reply_message_id = int(reply_to.get("message_id") or 0)
    except Exception:
        reply_message_id = 0
    if reply_message_id <= 0:
        return {"ok": False, "ignored": True, "error": "reply_required"}

    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT * FROM telegram_pending_actions
            WHERE domain='training' AND outbound_message_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (reply_message_id,),
        ).fetchone()
    finally:
        conn.close()
    pending = dict(row) if row else (_pending_training_proposal() or {})
    if not pending:
        return {"ok": False, "ignored": True, "error": "proposal_not_found"}
    if str(pending.get("status") or "").strip().lower() != "pending":
        return {"ok": False, "ignored": True, "error": "already_processed"}
    try:
        pending_raw = json.loads(pending.get("raw_json") or "{}")
    except Exception:
        pending_raw = {}
    if not isinstance(pending_raw, dict):
        pending_raw = {}
    plan_context = pending_raw.get("plan_context") if isinstance(pending_raw.get("plan_context"), dict) else {}

    inbound_message_id = int(message.get("message_id") or 0) if message.get("message_id") is not None else None
    inbound_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    inbound_chat_id = str(inbound_chat.get("id") or "").strip() or None
    if decision == "keep":
        _mark_proposal_response(
            str(pending["source_key"]),
            status="rejected",
            response_text=text,
            response_message_id=inbound_message_id,
        )
        _update_control_room_outcome(str(pending["source_key"]), "Vorschlag via Telegram abgelehnt.")
        send_domain_message(
            "training",
            f"Kein Plan-Change. {pending.get('exercise_name') or 'Übung'} bleibt wie geplant.",
            chat_id=inbound_chat_id,
            reply_to_message_id=reply_message_id,
        )
        return {"ok": True, "status": "rejected", "source_key": pending["source_key"]}

    action = decision
    if action == "core_decides":
        action = _choose_core_action(pending, plan_context)

    target = {
        "item_id": str(plan_context.get("item_id") or "").strip(),
        "day": str(plan_context.get("day") or "").strip(),
        "session_title": str(plan_context.get("session_title") or "").strip(),
        "exercise_name": str(pending.get("exercise_name") or ""),
        "current_variation": str(pending.get("current_variation") or ""),
    }
    if action == "variation":
        applied = _replace_variation_in_active_plan(target, str(pending.get("proposed_variation") or ""))
    elif action == "rep_range":
        applied = _apply_rep_range_in_active_plan(target)
    else:
        applied = _apply_rpe_down_in_active_plan(target)
    if not applied.get("ok"):
        return {"ok": False, "error": applied.get("error") or "apply_failed", "source_key": pending["source_key"]}
    _mark_proposal_response(
        str(pending["source_key"]),
        status="accepted",
        response_text=text,
        response_message_id=inbound_message_id,
    )
    _update_control_room_outcome(
        str(pending["source_key"]),
        f"Telegram-Reply {text} -> {pending.get('exercise_name')} Aktion {action}.",
    )
    if action == "variation":
        ack_lines = [
            "Plan angepasst.",
            f"{pending.get('exercise_name')} -> {pending.get('proposed_variation')}",
            f"{applied.get('day') or '-'} · {applied.get('session_title') or '-'}",
        ]
    elif action == "rep_range":
        ack_lines = [
            "Plan angepasst.",
            f"{pending.get('exercise_name')} -> Rep-Range {applied.get('rep_range')}",
            f"{applied.get('day') or '-'} · {applied.get('session_title') or '-'}",
        ]
    else:
        ack_lines = [
            "Plan angepasst.",
            f"{pending.get('exercise_name')} -> Ziel-RPE gesenkt ({applied.get('rpe_text')})",
            f"{applied.get('day') or '-'} · {applied.get('session_title') or '-'}",
        ]
    send_domain_message(
        "training",
        "\n".join(ack_lines),
        chat_id=inbound_chat_id,
        reply_to_message_id=reply_message_id,
    )
    return {"ok": True, "status": "accepted", "source_key": pending["source_key"], "applied": applied}


def process_training_query_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"ok": False, "error": "invalid_message"}
    text = str(message.get("text") or "").strip()
    if not text:
        return {"ok": False, "ignored": True, "error": "empty_message"}
    token = normalize_reply_token(text)
    low = _normalize_text(text)
    cmd = token
    if low.startswith("/"):
        cmd = normalize_reply_token(low.split()[0].lstrip("/"))
    if cmd in {"help"}:
        answer = (
            "🧭 Training Chat · Commands\n"
            "- wochencheck\n"
            "- [Übung] [Variation] (z. B. bankdrücken kurzhantel)\n"
            "- foto senden (physique-flag)\n"
            "- 1/2/3/4/5 als reply auf CORE-vorschlag\n"
            "- status\n"
            "\n"
            "Nicht fürs Loggen: Trainingslogs bleiben im Dashboard."
        )
    elif cmd in {"status", "stand"}:
        answer = _training_status_text()
    elif cmd in {"core", "vorschlag"}:
        answer = _training_core_status_text()
    elif cmd in QUERY_WEEKLY_TOKENS or token in QUERY_WEEKLY_TOKENS:
        answer = _weekly_training_check_text()
    else:
        exercise_query, variation_query = _query_tokens_from_text(text)
        if not exercise_query:
            return {"ok": False, "ignored": True, "error": "empty_query"}
        answer = _exercise_query_summary_text(exercise_query, variation_query)
    inbound_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    inbound_chat_id = str(inbound_chat.get("id") or "").strip() or None
    sent = send_domain_message(
        "training",
        answer,
        chat_id=inbound_chat_id,
        reply_to_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
        parse_mode="HTML",
    )
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed"}
    return {"ok": True, "status": "answered"}


def process_training_media_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"ok": False, "error": "invalid_message"}
    photo_list = message.get("photo") if isinstance(message.get("photo"), list) else []
    document = message.get("document") if isinstance(message.get("document"), dict) else {}
    has_photo = bool(photo_list)
    document_name = str(document.get("file_name") or "").strip().lower()
    has_image_doc = bool(
        document
        and (
            str(document.get("mime_type") or "").lower().startswith("image/")
            or Path(document_name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
        )
    )
    if not has_photo and not has_image_doc:
        return {"ok": False, "ignored": True, "error": "no_media"}

    config = resolve_domain_config("training")
    token = str(config.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "missing_config"}

    assets: list[dict[str, Any]] = []
    if has_photo:
        # Telegram normally sends PhotoSize entries ascending by resolution,
        # but that ordering is not part of the contract. Pick the original
        # largest photo explicitly so an arbitrary list order cannot select a
        # thumbnail with missing metadata.
        photo_sizes = [item for item in photo_list if isinstance(item, dict) and str(item.get("file_id") or "").strip()]
        def _photo_size_rank(item: dict[str, Any]) -> tuple[int, int]:
            try:
                area = int(item.get("width") or 0) * int(item.get("height") or 0)
            except (TypeError, ValueError, OverflowError):
                area = 0
            try:
                file_size = int(item.get("file_size") or 0)
            except (TypeError, ValueError, OverflowError):
                file_size = 0
            return area, file_size

        best = max(photo_sizes, key=_photo_size_rank, default={})
        file_id = str(best.get("file_id") or "").strip()
        content, mime, filename = _api_get_file_bytes(token, file_id)
        if content:
            assets.append({
                "content": content,
                "filename": filename or "telegram-photo.jpg",
                "mime_type": mime or "image/jpeg",
                "telegram_file_id": file_id,
                "width": best.get("width"),
                "height": best.get("height"),
                "_telegram_media_type": "photo",
            })
    if has_image_doc:
        file_id = str(document.get("file_id") or "").strip()
        content, mime, filename = _api_get_file_bytes(token, file_id)
        if content:
            assets.append({
                "content": content,
                "filename": str(document.get("file_name") or filename or "telegram-image"),
                "mime_type": str(document.get("mime_type") or mime or "application/octet-stream").strip().lower(),
                "telegram_file_id": file_id,
                "_telegram_media_type": "document",
            })
    if not assets:
        return {"ok": False, "error": "download_failed"}

    caption = str(message.get("caption") or "").strip()
    parsed = parse_physique_caption(caption)
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip() or None
    msg_id = int(message.get("message_id") or 0) if message.get("message_id") is not None else None
    source_ref = f"tg-training:{chat_id or 'na'}:{msg_id or 0}"
    try:
        row = create_physique_update(
            source="telegram",
            assets=assets,
            note=str(parsed.get("note") or ""),
            weight_kg=parsed.get("weight_kg"),
            view_label=str(parsed.get("view_label") or ""),
            pose_label=str(parsed.get("pose_label") or ""),
            source_ref=source_ref,
            source_domain="training",
            source_chat_id=chat_id,
            source_message_id=msg_id,
            telegram_message_date=message.get("date"),
        )
    except Exception as exc:
        return {"ok": False, "error": f"physique_import_failed:{exc}"}

    view = str(row.get("view_label") or "").strip()
    pose = str(row.get("pose_label") or "").strip()
    flag_line = "🚩 Physique-Flag gesetzt und einsortiert."
    details = []
    if view:
        details.append(f"View: {view}")
    if pose:
        details.append(f"Pose: {pose}")
    ack = flag_line if not details else f"{flag_line}\n" + " · ".join(details)
    sent = send_domain_message(
        "training",
        ack,
        chat_id=chat_id,
        reply_to_message_id=msg_id,
    )
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed"}
    return {"ok": True, "status": "physique_imported", "id": row.get("id")}


def _day_from_core_callback(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("core:"):
        parts = token.split(":")
        day_raw = parts[2] if len(parts) >= 3 else ""
        if re.fullmatch(r"\d{8}", day_raw):
            return f"{day_raw[:4]}-{day_raw[4:6]}-{day_raw[6:8]}"
        return date.today().isoformat()
    parts = token.split(".")
    day_raw = parts[2] if len(parts) >= 3 else ""
    if re.fullmatch(r"\d{8}", day_raw):
        return f"{day_raw[:4]}-{day_raw[4:6]}-{day_raw[6:8]}"
    return date.today().isoformat()


def process_core_callback_query(callback_query: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(callback_query, dict):
        return {"ok": False, "error": "invalid_callback"}
    cb_id = str(callback_query.get("id") or "").strip()
    data = str(callback_query.get("data") or "").strip()
    if not (data.startswith("core:") or data.startswith("c.")):
        answer_callback_query("core", cb_id, text="Nicht von CORE verarbeitet.")
        return {"ok": False, "ignored": True, "error": "unsupported_callback"}

    if data.startswith("core:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
    else:
        action = data.split(".")[1] if len(data.split(".")) > 1 else ""
    day_iso = _day_from_core_callback(data)
    feedback_map = {
        "ok": ("accepted", "Passt. Ich halte die Entscheidung so fest."),
        "lighter": ("lighter", "Verstanden. CORE führt die Constraints konservativer."),
        "lt": ("lighter", "Verstanden. CORE führt die Constraints konservativer."),
        "harder": ("harder", "Verstanden. CORE prüft mehr Freigabe, aber nicht blind aggressiv."),
        "hi": ("harder", "Verstanden. CORE prüft mehr Freigabe, aber nicht blind aggressiv."),
        "later": ("review_later", "Ich prüfe später neu, ohne jetzt zu spammen."),
        "re": ("review_later", "Ich prüfe später neu, ohne jetzt zu spammen."),
        "plan": ("plan_change", "Planänderung ist markiert. Auf /core ist die Override-Box sichtbar."),
        "pl": ("plan_change", "Planänderung ist markiert. Auf /core ist die Override-Box sichtbar."),
    }
    mapped = feedback_map.get(action)
    if not mapped:
        answer_callback_query("core", cb_id, text="Unbekannte CORE-Aktion.")
        return {"ok": False, "ignored": True, "error": "unknown_core_action"}

    message = callback_query.get("message") if isinstance(callback_query.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip() or None
    msg_id = int(message.get("message_id") or 0) if message.get("message_id") is not None else None
    feedback, ack = mapped
    stored = record_daily_decision_feedback(
        day_iso=day_iso,
        feedback=feedback,
        source="telegram",
        chat_id=chat_id,
        message_id=msg_id,
        callback_id=cb_id,
        raw={"callback_data": data},
    )
    answer_callback_query("core", cb_id, text=ack)

    if feedback in {"lighter", "harder"}:
        try:
            from core.core_night_cycle import run_night_cycle

            run_night_cycle(day_iso=day_iso, apply_parameter_updates=False)
        except Exception:
            pass
    return {"ok": bool(stored.get("ok")), "status": feedback, "day_iso": day_iso}


def process_core_inbox(*, timeout: int = 0) -> dict[str, Any]:
    updates = fetch_updates("core", timeout=timeout)
    processed = 0
    ignored = 0
    errors: list[str] = []
    for update in updates:
        callback_query = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else None
        if callback_query:
            result = process_core_callback_query(callback_query)
        else:
            ignored += 1
            continue
        if result.get("ok"):
            processed += 1
        elif result.get("ignored"):
            ignored += 1
        else:
            ignored += 1
            errors.append(str(result.get("error") or "unknown"))
    return {"ok": not errors, "updates": len(updates), "processed": processed, "ignored": ignored, "errors": errors[:10]}


def process_training_inbox(*, timeout: int = 0) -> dict[str, Any]:
    updates = fetch_updates("training", timeout=timeout)
    processed = 0
    ignored = 0
    errors: list[str] = []
    for update in updates:
        message = update.get("message") if isinstance(update.get("message"), dict) else None
        if not message:
            ignored += 1
            continue
        reply_to = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        if int(reply_to.get("message_id") or 0) > 0:
            result = process_training_reply_message(message)
        elif bool(message.get("photo")) or (
            isinstance(message.get("document"), dict)
            and str((message.get("document") or {}).get("mime_type") or "").startswith("image/")
        ):
            result = process_training_media_message(message)
        else:
            result = process_training_query_message(message)
        if result.get("ok"):
            processed += 1
        elif result.get("ignored"):
            ignored += 1
        else:
            ignored += 1
            errors.append(str(result.get("error") or "unknown"))
    pr_alert = _maybe_send_training_pr_alert()
    if not pr_alert.get("ok"):
        errors.append(f"pr_alert:{pr_alert.get('error') or 'unknown'}")
    return {
        "ok": not errors,
        "updates": len(updates),
        "processed": processed,
        "ignored": ignored,
        "errors": errors[:10],
        "pr_alert": pr_alert,
    }


def _system_health_status_line() -> str:
    url = (os.environ.get("LIVA_HEALTH_URL") or "http://127.0.0.1:5000/healthz").strip()
    try:
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=6) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="ignore").strip()
        ok = (status_code == 200) and (body.lower() == "ok" or body.startswith("{"))
        return f"Health: {'OK' if ok else 'ERROR'} ({status_code})"
    except Exception as exc:
        return f"Health: ERROR ({exc})"


def _system_backup_status_line() -> str:
    try:
        last_ts = latest_backup_timestamp()
    except Exception:
        last_ts = None
    if not last_ts:
        return "Backup: ERROR (kein letzter Backup-Timestamp)"
    try:
        last_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc).astimezone()
        age_h = (datetime.now(timezone.utc) - datetime.fromtimestamp(float(last_ts), tz=timezone.utc)).total_seconds() / 3600.0
        age_txt = f"{age_h:.1f}h"
        base = f"Backup: OK (letztes DB-Backup {last_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}, Alter {age_txt})"
    except Exception:
        base = "Backup: OK (Timestamp vorhanden)"
    try:
        meta = load_backup_metadata() or {}
        payload = meta.get("payload") if isinstance(meta, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        code_push = payload.get("code_push") if isinstance(payload.get("code_push"), dict) else {}
        if code_push and not bool(code_push.get("ok")):
            return base + f" | Code-Push: ERROR ({code_push.get('reason') or code_push.get('step') or 'Fehler'})"
    except Exception:
        pass
    return base


def _system_status_text() -> str:
    pi: dict[str, Any] = {}
    try:
        pi = get_pi_stats() or {}
    except Exception:
        pi = {}
    host = str(pi.get("host") or "-")
    os_label = str(pi.get("os") or "-")
    cpu_label = "-"
    if pi.get("cpu_percent") is not None or pi.get("cpu_cores") is not None:
        cpu_pct = _format_decimal_de(pi.get("cpu_percent"), decimals=1)
        cores = _format_number(pi.get("cpu_cores"))
        cpu_label = f"{cpu_pct}% ({cores} Cores)"
    ram_label = "-"
    if pi.get("ram_used_mb") is not None and pi.get("ram_total_mb") is not None:
        ram_label = (
            f"{_format_decimal_de(pi.get('ram_used_mb'), decimals=1)} / "
            f"{_format_decimal_de(pi.get('ram_total_mb'), decimals=1)} MB"
        )
    disk_label = "-"
    if pi.get("disk_used_gb") is not None and pi.get("disk_total_gb") is not None:
        disk_label = (
            f"{_format_decimal_de(pi.get('disk_used_gb'), decimals=1)} / "
            f"{_format_decimal_de(pi.get('disk_total_gb'), decimals=1)} GB"
        )
        disk_pct = _safe_float(pi.get("disk_percent"))
        if disk_pct is not None:
            disk_label = f"{disk_label} ({_format_decimal_de(100.0 - disk_pct, decimals=0)}% frei)"
    load_label = str(pi.get("load") or "-")
    uptime_label = str(pi.get("uptime") or "-")
    return "\n".join(
        [
            "System-Status",
            _system_health_status_line(),
            _system_backup_status_line(),
            "",
            "Pi-Stats",
            f"Host: {host}",
            f"OS: {os_label}",
            f"CPU: {cpu_label}",
            f"RAM: {ram_label}",
            f"Disk: {disk_label}",
            f"Load: {load_label}",
            f"Uptime: {uptime_label}",
        ]
    )


def _backup_result_text(result: dict[str, Any]) -> str:
    ok = bool(result.get("ok"))
    lines: list[str] = ["Backup: OK" if ok else "Backup: ERROR"]

    sqlite_backup = result.get("sqlite_backup") if isinstance(result.get("sqlite_backup"), dict) else {}
    remote_sync = sqlite_backup.get("remote_sync") if isinstance(sqlite_backup.get("remote_sync"), dict) else {}
    code_push = result.get("code_push") if isinstance(result.get("code_push"), dict) else {}

    if sqlite_backup:
        lines.append("DB-Backup: OK")

    if remote_sync:
        if bool(remote_sync.get("ok")):
            lines.append("Dropbox-Sync: OK")
        else:
            reason = str(remote_sync.get("reason") or remote_sync.get("status") or "Fehler").strip()
            lines.append(f"Dropbox-Sync: ERROR ({reason})")

    if code_push:
        status = str(code_push.get("status") or "").strip().lower()
        reason = str(code_push.get("reason") or "").strip()
        if bool(code_push.get("ok")) and status == "ok":
            lines.append("Code-Push: OK")
        elif bool(code_push.get("ok")) and status == "skipped":
            if reason == "missing_origin":
                lines.append("Code-Push: übersprungen (kein Git-Remote 'origin').")
            else:
                lines.append(f"Code-Push: übersprungen ({reason or 'n/a'}).")
            stderr = str(code_push.get("stderr") or "").strip()
            if "safe.directory" in stderr and "/opt/liva" in stderr:
                lines.append("Hinweis: git safe.directory fehlt.")
                lines.append("Fix: git config --global --add safe.directory /opt/liva")
        else:
            detail = str(code_push.get("reason") or code_push.get("step") or "Fehler").strip()
            lines.append(f"Code-Push: ERROR ({detail})")

    return "\n".join(lines)


def process_system_query_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"ok": False, "error": "invalid_message"}
    text = str(message.get("text") or "").strip()
    if not text:
        return {"ok": False, "ignored": True, "error": "empty_message"}
    token = normalize_reply_token(text)
    low = _normalize_text(text)
    cmd = token
    if low.startswith("/"):
        cmd = normalize_reply_token(low.split()[0].lstrip("/"))

    if cmd in {"status", "statis", "state", "zustand"}:
        answer = _system_status_text()
    elif cmd in {"backup", "backupnow", "runbackup"}:
        result = run_daily_backup()
        answer = _backup_result_text(result)
    elif cmd in {"webuntis", "untis", "schule"}:
        root = "/opt/liva"
        try:
            env = os.environ.copy()
            env["HOME"] = "/var/lib/liva"
            env["PLAYWRIGHT_BROWSERS_PATH"] = "/var/lib/liva/.cache/ms-playwright"
            env["XDG_CACHE_HOME"] = "/var/lib/liva/.cache"
            subprocess.Popen(
                [os.path.join(root, "venv", "bin", "python"), "-m", "jobs.webuntis_update_watch"],
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            answer = "WebUntis-Sync gestartet (läuft im Hintergrund)."
        except Exception as exc:
            answer = f"WebUntis-Sync konnte nicht gestartet werden: {exc}"
    elif cmd in {"nochwach", "noch-wach", "wach", "awake"}:
        try:
            pulse_light(
                pulses=2,
                color={"r": 30, "g": 60, "b": 200},
                brightness=80,
                on_s=0.28,
                off_s=0.22,
                allow_tapo_fallback=False,
                fallback_restore_state={"is_on": True, "brightness": 35, "color": {"r": 255, "g": 180, "b": 120}},
            )
            answer = "Noch‑wach Signal gesendet."
        except Exception as exc:
            answer = f"Noch‑wach Signal fehlgeschlagen: {exc}"
    elif cmd in {"help", "hilfe", "commands", "start"}:
        answer = (
            "System-Bot Commands\n"
            "- status / statis\n"
            "- backup\n"
            "- webuntis\n"
            "- nochwach\n"
            "- help"
        )
    else:
        if _looks_like_monthly_plan(text):
            imported = import_shift_plan_to_calendar(text)
            if imported.get("ok") and int(imported.get("count") or 0) > 0:
                lines = [f"{_fmt_date_de(str(x.get('date_iso') or ''))} {x.get('start_time')}–{x.get('end_time')}" for x in (imported.get("items") or [])]
                answer = (
                    f"{int(imported.get('count') or 0)} Schichten erkannt und eingetragen:\n"
                    + "\n".join(lines[:20])
                )
            elif imported.get("ok"):
                answer = "No matching shifts found. Set LIVA_SHIFT_TARGET_NAME to enable this import."
            else:
                answer = "Monatsplan konnte nicht importiert werden."
        else:
            created = create_calendar_event_from_telegram(text)
            if created.get("ok"):
                parsed = created.get("parsed")
                if isinstance(parsed, ParsedEvent):
                    if parsed.all_day:
                        answer = f"Kalendereintrag erstellt: {parsed.title} – {_fmt_date_de(parsed.date_iso)} (ganztägig)"
                    else:
                        answer = (
                            f"Kalendereintrag erstellt: {parsed.title} – "
                            f"{_fmt_date_de(parsed.date_iso)}, {parsed.start_time}–{parsed.end_time}"
                        )
                else:
                    answer = "Kalendereintrag erstellt."
            else:
                answer = "Termin nicht erkannt. Bitte nutze z. B.: Zahnarzt, 27.02, 13:30"

    inbound_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    inbound_chat_id = str(inbound_chat.get("id") or "").strip() or None
    sent = send_domain_message(
        "system",
        answer,
        chat_id=inbound_chat_id,
        reply_to_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
    )
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed"}
    return {"ok": True, "status": "answered", "command": cmd}


def process_system_inbox(*, timeout: int = 0) -> dict[str, Any]:
    updates = fetch_updates("system", timeout=timeout)
    processed = 0
    ignored = 0
    errors: list[str] = []
    for update in updates:
        message = update.get("message") if isinstance(update.get("message"), dict) else None
        if not message:
            ignored += 1
            continue
        result = process_system_query_message(message)
        if result.get("ok"):
            processed += 1
        elif result.get("ignored"):
            ignored += 1
        else:
            ignored += 1
            errors.append(str(result.get("error") or "unknown"))
    return {
        "ok": True,
        "updates": len(updates),
        "processed": processed,
        "ignored": ignored,
        "errors": errors[:10],
    }


def _today_iso_berlin() -> str:
    return datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()


def _effective_day_payload_for_runtime(day_iso: str, base_payload: dict[str, Any] | None = None, evaluated: dict[str, Any] | None = None) -> dict[str, Any]:
    active = build_active_day_plan(day_iso, base_payload=base_payload or get_logging_day_payload(day_iso))
    if not isinstance(evaluated, dict):
        return active

    # CORE evaluations are normally the freshest runtime view.  A persisted,
    # explicitly accepted user mutation is stronger, though: an evaluation that
    # started before a Telegram shift must not move the meal back afterwards.
    result = dict(evaluated)
    evaluated_meals = [dict(row) for row in (evaluated.get("planned_meals") or []) if isinstance(row, dict)]
    active_by_slot = {
        int(row.get("slot_id") or 0): row
        for row in (active.get("planned_meals") or [])
        if isinstance(row, dict) and int(row.get("slot_id") or 0) > 0
    }
    user_states = {"accepted_adjustment", "manual_override", "telegram_confirmed"}
    user_sources = {"telegram", "user", "manual"}
    for idx, evaluated_meal in enumerate(evaluated_meals):
        active_meal = active_by_slot.get(int(evaluated_meal.get("slot_id") or 0))
        if not active_meal:
            continue
        is_user_mutation = (
            str(active_meal.get("state") or active_meal.get("status") or "").lower() in user_states
            or str(active_meal.get("status_source") or "").lower() in user_sources
            or str(active_meal.get("last_mutation_type") or "").lower() == "meal_shifted"
        )
        if is_user_mutation:
            evaluated_meals[idx] = dict(active_meal)
    result["planned_meals"] = evaluated_meals
    return result


def _slot_id_from_meal_number(day_payload: dict[str, Any], meal_no: int) -> int | None:
    rows = [r for r in (day_payload.get("planned_meals") or []) if isinstance(r, dict)]
    if not rows:
        return None
    direct = next((r for r in rows if int(r.get("slot_index") or -1) == int(meal_no) - 1), None)
    if direct:
        return int(direct.get("slot_id") or 0) or None
    rows_sorted = _meal_sort_rows(day_payload)
    idx = int(meal_no) - 1
    if 0 <= idx < len(rows_sorted):
        return int(rows_sorted[idx].get("slot_id") or 0) or None
    return None


def _parse_meal_number(text: str) -> int | None:
    m = re.search(r"\b(?:meal|m)\s*([1-9])\b", str(text or ""), flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _nutrition_parse_food_amount(text: str) -> tuple[float | None, str | None, str | None]:
    # very small, explicit parser: "150g haferflocken", "1 banane"
    raw = str(text or "").strip().lower()
    m = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(g|ml|pcs|stück|stk|x)\s+([a-z0-9äöüß -]{2,})$", raw)
    if m:
        amount = float(str(m.group(1)).replace(",", "."))
        unit = m.group(2).replace("stück", "pcs").replace("stk", "pcs")
        name = m.group(3).strip()
        return amount, unit, name
    m2 = re.search(r"\b(\d+(?:[.,]\d+)?)\s+([a-z0-9äöüß -]{2,})$", raw)
    if m2:
        amount = float(str(m2.group(1)).replace(",", "."))
        name = m2.group(2).strip()
        return amount, "g", name
    return None, None, None


def _normalize_food_label(text: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", str(text or "").strip().lower()).strip()


def _parse_meal_reference(text: str, day_payload: dict[str, Any]) -> tuple[int | None, str | None]:
    raw = str(text or "").strip()
    low = _normalize_text(raw)
    mtime = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", raw)
    explicit_time = f"{int(mtime.group(1)):02d}:{int(mtime.group(2)):02d}" if mtime else None

    meal_no = _parse_meal_number(raw)
    if meal_no:
        return _slot_id_from_meal_number(day_payload, meal_no), explicit_time

    alias_map = {
        "breakfast": 0,
        "frühstück": 0,
        "lunch": 1,
        "mittag": 1,
        "dinner": 4,
        "abendessen": 4,
    }
    target_index = None
    for alias, idx in alias_map.items():
        if alias in low:
            target_index = idx
            break
    if target_index is None:
        # Natural references such as "Nougat Bits auf 14:45" should resolve to
        # the planned meal title without requiring a synthetic "meal 4" prefix.
        for row in (day_payload.get("planned_meals") or []):
            if not isinstance(row, dict):
                continue
            title = _normalize_text(str(row.get("title") or ""))
            if title and title in low:
                return int(row.get("slot_id") or 0) or None, explicit_time
    if target_index is None:
        return None, explicit_time
    rows = sorted(
        [r for r in (day_payload.get("planned_meals") or []) if isinstance(r, dict)],
        key=lambda r: int(r.get("slot_index") or 999),
    )
    hit = next((r for r in rows if int(r.get("slot_index") or -1) == target_index), None)
    return (int(hit.get("slot_id") or 0) if hit else None), explicit_time


def _context_expiry_iso(minutes: int = 180) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(5, int(minutes)))).replace(microsecond=0).isoformat()


def _get_active_meal_context(chat_id: str, day_iso: str) -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM telegram_meal_context
            WHERE domain='nutrition' AND chat_id=? AND day_iso=? AND state='open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(chat_id or "").strip(), day_iso),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        expires = str(out.get("expires_at") or "").strip()
        if expires:
            try:
                if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    return None
            except Exception:
                pass
        try:
            out["raw"] = json.loads(out.get("raw_json") or "{}")
        except Exception:
            out["raw"] = {}
        return out
    finally:
        conn.close()


def _close_meal_context(context_id: int, *, reason: str = "closed") -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            "UPDATE telegram_meal_context SET state='closed', closed_at=?, raw_json=COALESCE(raw_json,'{}') WHERE id=?",
            (_utc_now(), int(context_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _open_meal_context(
    *,
    chat_id: str,
    day_iso: str,
    meal: dict[str, Any],
    referenced_time: str | None,
    source_message_type: str,
) -> dict[str, Any]:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            UPDATE telegram_meal_context
            SET state='closed', closed_at=?
            WHERE domain='nutrition' AND chat_id=? AND day_iso=? AND state='open'
            """,
            (_utc_now(), str(chat_id or "").strip(), day_iso),
        )
        raw = {
            "base_items": meal.get("items") or [],
            "pending_items": meal.get("items") or [],
            "base_macros": meal.get("macros") or {},
            "pending_macros": meal.get("macros") or {},
            "last_edit": None,
        }
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO telegram_meal_context
                (domain, chat_id, day_iso, meal_slot_id, meal_slot_index, meal_title, referenced_time,
                 state, source_message_type, started_at, expires_at, raw_json)
            VALUES ('nutrition', ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                str(chat_id or "").strip(),
                day_iso,
                int(meal.get("slot_id") or 0) or None,
                int(meal.get("slot_index") or 0),
                str(meal.get("title") or "").strip() or "Meal",
                referenced_time,
                source_message_type,
                now,
                _context_expiry_iso(180),
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM telegram_meal_context WHERE id=last_insert_rowid()").fetchone()
        out = dict(row) if row else {}
        out["raw"] = raw
        return out
    finally:
        conn.close()


def _message_log_exists(dedupe_key: str) -> bool:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM telegram_meal_message_log WHERE dedupe_key=? LIMIT 1",
            (str(dedupe_key or "").strip(),),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _append_message_log(
    *,
    chat_id: str | None,
    message_type: str,
    day_iso: str,
    dedupe_key: str,
    meal_slot_id: int | None = None,
    core_action_id: int | None = None,
    context_id: int | None = None,
    sent_message_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_meal_message_log
                (domain, chat_id, message_type, day_iso, meal_slot_id, core_action_id, context_id,
                 dedupe_key, sent_message_id, created_at, payload_json)
            VALUES ('nutrition', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(chat_id or "").strip() or None,
                message_type,
                day_iso,
                int(meal_slot_id) if meal_slot_id else None,
                int(core_action_id) if core_action_id else None,
                int(context_id) if context_id else None,
                str(dedupe_key or "").strip(),
                int(sent_message_id) if sent_message_id else None,
                _utc_now(),
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_message_log_slot(
    *,
    chat_id: str | None,
    message_type: str,
    day_iso: str,
    dedupe_key: str,
    meal_slot_id: int | None = None,
    core_action_id: int | None = None,
    context_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO telegram_meal_message_log
                (domain, chat_id, message_type, day_iso, meal_slot_id, core_action_id, context_id,
                 dedupe_key, sent_message_id, created_at, payload_json)
            VALUES ('nutrition', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                str(chat_id or "").strip() or None,
                message_type,
                day_iso,
                int(meal_slot_id) if meal_slot_id else None,
                int(core_action_id) if core_action_id else None,
                int(context_id) if context_id else None,
                str(dedupe_key or "").strip(),
                _utc_now(),
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        conn.close()


def _finalize_message_log(
    *,
    dedupe_key: str,
    chat_id: str | None = None,
    sent_message_id: int | None = None,
    context_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            UPDATE telegram_meal_message_log
            SET
                chat_id=COALESCE(?, chat_id),
                context_id=COALESCE(?, context_id),
                sent_message_id=COALESCE(?, sent_message_id),
                payload_json=?
            WHERE dedupe_key=?
            """,
            (
                str(chat_id or "").strip() or None,
                int(context_id) if context_id else None,
                int(sent_message_id) if sent_message_id else None,
                json.dumps(payload or {}, ensure_ascii=False),
                str(dedupe_key or "").strip(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _release_message_log_claim(dedupe_key: str) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute("DELETE FROM telegram_meal_message_log WHERE dedupe_key=?", (str(dedupe_key or "").strip(),))
        conn.commit()
    finally:
        conn.close()


def _claim_notice_minute_bucket(*, notice_key: str, bucket: str, payload: dict[str, Any] | None = None) -> bool:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO telegram_notice_state (notice_key, status, last_sent_at, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(notice_key) DO UPDATE SET
                status=excluded.status,
                last_sent_at=excluded.last_sent_at,
                raw_json=excluded.raw_json
            WHERE COALESCE(telegram_notice_state.status, '') <> excluded.status
            """,
            (
                str(notice_key or "").strip(),
                str(bucket or "").strip(),
                _utc_now(),
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        conn.close()

def _get_pending_nutrition_prompt(source_key: str) -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT * FROM telegram_pending_actions
            WHERE source_key=? AND domain='nutrition'
            LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_context_raw(context_id: int, raw: dict[str, Any], *, extend_minutes: int = 180) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            UPDATE telegram_meal_context
            SET raw_json=?, expires_at=?
            WHERE id=?
            """,
            (json.dumps(raw or {}, ensure_ascii=False), _context_expiry_iso(extend_minutes), int(context_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _item_name(item: dict[str, Any]) -> str:
    return str(item.get("food_name") or item.get("name") or "").strip()


def _meal_time_text(row: dict[str, Any]) -> str:
    return str(row.get("shifted_time_text") or row.get("time_text") or "").strip() or "--:--"


def _format_macros_short(macros: dict[str, Any]) -> str:
    kcal = int(round(float(macros.get("kcal") or 0)))
    p = int(round(float(macros.get("p") or 0)))
    c = int(round(float(macros.get("c") or 0)))
    f = int(round(float(macros.get("f") or 0)))
    return f"{kcal} kcal · P {p} / C {c} / F {f}"


def _macro_value(macros: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = macros.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _meal_status_label(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "logged":
        return "geloggt"
    if value == "skipped":
        return "ausgelassen"
    if value == "shifted":
        return "verschoben"
    if value == "manual_override":
        return "geloggt"
    return "offen"


def _meal_sort_rows(day_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in (day_payload.get("planned_meals") or []) if isinstance(r, dict)]
    return sorted(rows, key=lambda r: (_meal_time_text(r), int(r.get("slot_index") or 999), int(r.get("slot_id") or 0)))


def _meal_runtime_overrides(active_ctx: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not isinstance(active_ctx, dict):
        return {}
    slot_id = int(active_ctx.get("meal_slot_id") or 0)
    if slot_id <= 0:
        return {}
    raw = active_ctx.get("raw") if isinstance(active_ctx.get("raw"), dict) else {}
    return {
        slot_id: {
            "title": str(raw.get("pending_title") or active_ctx.get("meal_title") or "").strip() or None,
            "items": raw.get("pending_items") if isinstance(raw.get("pending_items"), list) else None,
            "macros": raw.get("pending_macros") if isinstance(raw.get("pending_macros"), dict) else None,
        }
    }


def _runtime_rows_by_slot(runtime_payload: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(runtime_payload, dict):
        return out
    for row in (runtime_payload.get("planned_meals") or []):
        if not isinstance(row, dict):
            continue
        slot_id = int(row.get("slot_id") or 0)
        if slot_id > 0:
            out[slot_id] = row
    return out


def _format_today_text(day_payload: dict[str, Any], runtime_payload: dict[str, Any] | None = None, active_ctx: dict[str, Any] | None = None) -> str:
    overrides = _meal_runtime_overrides(active_ctx)
    runtime_rows = _runtime_rows_by_slot(runtime_payload)
    rows = _meal_sort_rows(day_payload)
    lines = ["Plan ab jetzt"]
    for idx, row in enumerate(rows, start=1):
        slot_id = int(row.get("slot_id") or 0)
        override = overrides.get(slot_id) or {}
        status = str(row.get("status") or "open")
        runtime_row = runtime_rows.get(slot_id) or {}
        title = str(override.get("title") or runtime_row.get("title") or row.get("title") or "Meal").strip()
        lines.append(f"- {_meal_time_text(row)} · {title} ({_meal_status_label(status)})")
        if status == "manual_override":
            lines.append(f"Meal {idx} ist bereits geloggt:")
            lines.append(f"{_meal_time_text(row)} · {title}")
    return "\n".join(lines)


def _nutrition_day_meals_overview_text(day_payload: dict[str, Any]) -> str:
    rows = _meal_sort_rows(day_payload)
    lines = ["Plan ab jetzt"]
    for idx, row in enumerate(rows, start=1):
        title = str(row.get("title") or "Meal").strip() or "Meal"
        state = str(row.get("state_label") or _meal_status_label(str(row.get("state") or row.get("status") or "open"))).strip()
        lines.append(f"- Meal {idx} · {_meal_time_text(row)} · {title} ({state})")
        macros = row.get("macros") if isinstance(row.get("macros"), dict) else {}
        if macros:
            lines.append(f"  {_format_macros_short(macros)}")
    remaining = day_payload.get("remaining") if isinstance(day_payload.get("remaining"), dict) else {}
    if remaining:
        lines.extend(["", f"Rest: {_format_macros_short(remaining)}"])
    return "\n".join(lines)


def _format_open_text(day_payload: dict[str, Any]) -> str:
    rows = [
        r for r in _meal_sort_rows(day_payload)
        if str(r.get("status") or "open").strip().lower() in {"open", "shifted"}
    ]
    lines = ["Tagesstand ab jetzt"]
    for row in rows:
        lines.append(f"- Meal {int(row.get('slot_index') or 0) + 1} · {_meal_time_text(row)} · {str(row.get('title') or 'Meal').strip()}")
    if len(lines) == 1:
        lines.append("Keine offenen Meals.")
    return "\n".join(lines)


def _format_macro_overview(day_payload: dict[str, Any]) -> str:
    logged = day_payload.get("logged_totals") if isinstance(day_payload.get("logged_totals"), dict) else {}
    targets = day_payload.get("targets") if isinstance(day_payload.get("targets"), dict) else {}
    kcal = int(round(_macro_value(logged, "kcal")))
    kcal_target = int(round(_macro_value(targets, "kcal")))
    p = int(round(_macro_value(logged, "p", "protein_g")))
    p_target = int(round(_macro_value(targets, "p", "protein_g")))
    c = int(round(_macro_value(logged, "c", "carbs_g")))
    c_target = int(round(_macro_value(targets, "c", "carbs_g")))
    f = int(round(_macro_value(logged, "f", "fat_g")))
    f_target = int(round(_macro_value(targets, "f", "fat_g")))
    return "\n".join(
        [
            "Bisher geloggt",
            f"Kcal: {kcal} / {kcal_target} ({kcal - kcal_target:+d})",
            f"P: {p} / {p_target} g ({p - p_target:+d})",
            f"C: {c} / {c_target} g ({c - c_target:+d})",
            f"F: {f} / {f_target} g ({f - f_target:+d})",
        ]
    )


def _format_food_rows(rows: list[dict[str, Any]], *, title: str) -> str:
    lines = [title]
    for idx, row in enumerate(rows, start=1):
        kcal = int(round(_macro_value(row, "kcal_per_100")))
        p = int(round(_macro_value(row, "p_per_100")))
        c = int(round(_macro_value(row, "c_per_100")))
        f = int(round(_macro_value(row, "f_per_100")))
        unit = str(row.get("unit_default") or "g").strip()
        lines.append(f"{idx}. {str(row.get('name') or row.get('title') or '').strip()} ({unit}/100)")
        lines.append(f"{kcal} kcal · P {p} / C {c} / F {f}")
    return "\n".join(lines)


def _format_meal_rows(rows: list[dict[str, Any]], *, title: str) -> str:
    lines = [title]
    for idx, row in enumerate(rows, start=1):
        kcal = int(round(_macro_value(row, "kcal_per_serving")))
        p = int(round(_macro_value(row, "p_per_serving")))
        c = int(round(_macro_value(row, "c_per_serving")))
        f = int(round(_macro_value(row, "f_per_serving")))
        lines.append(f"{idx}. {str(row.get('title') or '').strip()}")
        lines.append(f"{kcal} kcal · P {p} / C {c} / F {f}")
    return "\n".join(lines)


def _nutrition_help_text() -> str:
    return "\n".join(
        [
            "Nutrition Chat · Commands",
            "- today",
            "- open",
            "- makros",
            "- meals",
            "- foods",
            '- search meal "Porridge"',
            '- search food "Haferflocken"',
            "- skip meal 2",
            "- meal 2, 17:16",
            "- meal 1 done",
            "- meal 1 haferflocken 180g",
            "- switch meal 2 to porridge cinnamon",
        ]
    )


def _search_quoted_term(text: str) -> str:
    raw = str(text or "").strip()
    m = re.search(r'"([^"]+)"', raw)
    if m:
        return str(m.group(1)).strip()
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _format_search_overview(term: str, meal_rows: list[dict[str, Any]], food_rows: list[dict[str, Any]]) -> str:
    lines = [f'Search: "{term}"']
    lines.append("")
    lines.append("Meals")
    if meal_rows:
        lines.extend(_format_meal_rows(meal_rows, title="").splitlines()[1:])
    else:
        lines.append("Keine Treffer.")
    lines.append("")
    lines.append("Foods")
    if food_rows:
        lines.extend(_format_food_rows(food_rows, title="").splitlines()[1:])
    else:
        lines.append("Keine Treffer.")
    return "\n".join(lines)



def _format_items_lines(items: list[dict[str, Any]], *, max_items: int = 8) -> list[str]:
    out: list[str] = []
    for item in (items or [])[:max_items]:
        name = _item_name(item)
        if not name:
            continue
        amount = item.get("amount")
        unit = str(item.get("unit") or "").strip()
        if isinstance(amount, (int, float)):
            amount_text = _format_number(amount)
            line = f"- {name} {amount_text}{(' ' + unit) if unit else ''}".strip()
        else:
            line = f"- {name}"
        out.append(line)
    return out


def _compute_macros_for_items(items: list[dict[str, Any]]) -> dict[str, float]:
    total = {"kcal": 0.0, "p": 0.0, "c": 0.0, "f": 0.0}
    food_ids = [int(i.get("food_id") or 0) for i in (items or []) if int(i.get("food_id") or 0) > 0]
    by_id: dict[int, dict[str, Any]] = {}
    if food_ids:
        uniq = sorted(set(food_ids))
        placeholders = ",".join("?" for _ in uniq)
        conn = get_nutrition_db()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                SELECT id, kcal_per_100, p_per_100, c_per_100, f_per_100, common_portion_size, portion_g
                FROM nutrition_foods
                WHERE id IN ({placeholders})
                """,
                tuple(uniq),
            ).fetchall()
            by_id = {int(r["id"]): dict(r) for r in rows}
        finally:
            conn.close()
    for item in (items or []):
        if str(item.get("item_type") or "").lower() == "kcal_only":
            kcal = float(item.get("calories") or item.get("amount") or 0.0)
            total["kcal"] += kcal
            continue
        fid = int(item.get("food_id") or 0)
        food = by_id.get(fid)
        if not food:
            continue
        unit = str(item.get("unit") or "g").strip().lower()
        amount = float(item.get("amount") or 0.0)
        grams = 0.0
        if unit in {"g", "gram", "grams", "ml"}:
            grams = amount
        elif unit in {"pcs", "stück", "stk", "x"}:
            cp = float(food.get("common_portion_size") or food.get("portion_g") or 0.0)
            grams = amount * cp if cp > 0 else 0.0
        factor = grams / 100.0
        total["kcal"] += factor * float(food.get("kcal_per_100") or 0.0)
        total["p"] += factor * float(food.get("p_per_100") or 0.0)
        total["c"] += factor * float(food.get("c_per_100") or 0.0)
        total["f"] += factor * float(food.get("f_per_100") or 0.0)
    return {k: round(v, 1) for k, v in total.items()}


def _find_item_index(items: list[dict[str, Any]], needle: str) -> int | None:
    nn = _normalize_food_label(needle)
    if not nn:
        return None
    for idx, item in enumerate(items or []):
        if nn in _normalize_food_label(_item_name(item)):
            return idx
    return None


def _parse_context_action(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    low = _normalize_text(raw)
    if low in {"done", "gegessen", "geloggt", "passt", "genau so", "ok"}:
        return {"type": "done"}
    if any(tok in low for tok in ("skip", "nicht gegessen", "ausgefallen", "übersprungen")):
        return {"type": "skip"}
    m_done_time = re.search(r"\bdone\b.*?\b([01]?\d|2[0-3]):([0-5]\d)\b", low)
    if m_done_time:
        return {"type": "done", "time": f"{int(m_done_time.group(1)):02d}:{int(m_done_time.group(2)):02d}"}
    m_time_done = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b.*?\bdone\b", low)
    if m_time_done:
        return {"type": "done", "time": f"{int(m_time_done.group(1)):02d}:{int(m_time_done.group(2)):02d}"}
    if low in {"warum", "wieso", "trace"}:
        return {"type": "why"}
    if low in {"zurück", "original", "wie geplant", "doch original"}:
        return {"type": "undo"}
    if low in {"alt", "andere option", "was anderes", "anderes"}:
        return {"type": "alt"}
    if low in {"größer", "kleiner", "halbe portion", "doppelt", "mehr carbs", "mehr protein", "leichter", "portable"}:
        return {"type": "size", "value": low}
    m_factor = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*x", low)
    if m_factor:
        return {"type": "scale_factor", "factor": float(m_factor.group(1).replace(",", "."))}
    m_rep = re.search(r"(.+?)\s*->\s*(.+)", raw)
    if m_rep:
        return {"type": "replace", "from": m_rep.group(1).strip(), "to": m_rep.group(2).strip()}
    m_statt = re.search(r"(.+?)\s+statt\s+(.+)", low)
    if m_statt:
        return {"type": "replace", "from": m_statt.group(2).strip(), "to": m_statt.group(1).strip()}
    m_ohne = re.search(r"\bohne\s+([a-z0-9äöüß -]{2,})$", low)
    if m_ohne:
        return {"type": "remove", "name": m_ohne.group(1).strip()}
    m_raus = re.search(r"([a-z0-9äöüß -]{2,})\s+raus$", low)
    if m_raus:
        return {"type": "remove", "name": m_raus.group(1).strip()}
    m_qty = re.search(r"\b([a-z0-9äöüß() -]{2,})\s+(\d+(?:[.,]\d+)?)\s*(g|gr|gramm|ml|l|x|stück|stk|pcs)\b", low)
    if m_qty:
        return {
            "type": "quantity",
            "name": m_qty.group(1).strip(),
            "amount": float(m_qty.group(2).replace(",", ".")),
            "unit": m_qty.group(3).strip(),
        }
    m_qty2 = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(g|gr|gramm|ml|l|x|stück|stk|pcs)\s+([a-z0-9äöüß() -]{2,})$", low)
    if m_qty2:
        return {
            "type": "quantity",
            "name": m_qty2.group(3).strip(),
            "amount": float(m_qty2.group(1).replace(",", ".")),
            "unit": m_qty2.group(2).strip(),
        }
    return {"type": "unknown"}


def _parse_context_actions(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"\s*,\s*", raw) if p.strip()]
    if len(parts) == 1:
        pattern = re.compile(r"([a-z0-9äöüß()&/+.' -]{2,}?\s+\d+(?:[.,]\d+)?\s*(?:g|gr|gramm|ml|l|x|stück|stk|pcs))", re.IGNORECASE)
        hits = [m.group(1).strip() for m in pattern.finditer(raw)]
        if len(hits) > 1 and "".join(hits).replace(" ", "") != raw.replace(" ", ""):
            parts = [raw]
        elif len(hits) > 1:
            parts = hits
    actions: list[dict[str, Any]] = []
    for part in parts:
        action = _parse_context_action(part)
        if str(action.get("type") or "") != "unknown":
            actions.append(action)
    return actions


def _extract_inline_meal_command(text: str) -> tuple[int | None, str | None, str]:
    raw = str(text or "").strip()
    m = re.match(r"^\s*(?:meal|m)\s*([1-9])\b(.*)$", raw, flags=re.IGNORECASE)
    if not m:
        return None, None, ""
    try:
        meal_no = int(m.group(1))
    except Exception:
        return None, None, ""
    rest = str(m.group(2) or "").strip()
    rest = rest.lstrip(",").strip()
    mtime = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", rest)
    explicit_time = f"{int(mtime.group(1)):02d}:{int(mtime.group(2)):02d}" if mtime else None
    return meal_no, explicit_time, rest


def _context_summary_text(context: dict[str, Any], *, suffix: str = "noch nicht geloggt") -> str:
    raw = dict(context.get("raw") or {})
    title = str(raw.get("pending_title") or context.get("meal_title") or "Meal").strip() or "Meal"
    pm = raw.get("pending_macros") if isinstance(raw.get("pending_macros"), dict) else {}
    lines = [f"{title} angepasst · {suffix}", _format_macros_short(pm), ""]
    lines.extend(_format_items_lines(raw.get("pending_items") or []))
    edits = raw.get("applied_edits") if isinstance(raw.get("applied_edits"), list) else []
    if edits:
        lines.extend(["", *[str(edit) for edit in edits if str(edit or "").strip()]])
    lines.extend(["", "Zum Loggen: done"])
    return "\n".join(line for line in lines if line is not None).strip()


def _apply_size_modifier(items: list[dict[str, Any]], value: str) -> tuple[list[dict[str, Any]], str]:
    factor_map = {
        "größer": 1.2,
        "kleiner": 0.85,
        "halbe portion": 0.5,
        "doppelt": 2.0,
        "mehr carbs": 1.15,
        "mehr protein": 1.15,
    }
    if value in {"leichter", "portable"}:
        return items, value
    factor = float(factor_map.get(value, 1.0))
    out: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        if isinstance(row.get("amount"), (int, float)):
            row["amount"] = round(float(row["amount"]) * factor, 2)
        out.append(row)
    return out, f"{value} ({factor:.2f}x)"

def _upsert_nutrition_prompt(*, source_key: str, action: dict[str, Any]) -> dict[str, Any]:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    now = _utc_now()
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    current = payload.get("from_servings")
    proposed = payload.get("to_servings")
    try:
        conn.execute(
            """
            INSERT INTO telegram_pending_actions (
                source_key, domain, status, proposal_type, exercise_name, current_variation,
                proposed_variation, reason_text, created_at, raw_json
            ) VALUES (?, 'nutrition', 'pending', 'core_nutrition_action', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                status='pending',
                exercise_name=excluded.exercise_name,
                current_variation=excluded.current_variation,
                proposed_variation=excluded.proposed_variation,
                reason_text=excluded.reason_text,
                raw_json=excluded.raw_json
            """,
            (
                source_key,
                str(action.get("action_type") or "core_action"),
                "" if current is None else str(current),
                "" if proposed is None else str(proposed),
                str(action.get("human") or "").strip(),
                now,
                json.dumps(action, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM telegram_pending_actions WHERE source_key=? LIMIT 1", (source_key,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _mark_nutrition_prompt_sent(source_key: str, *, message_id: int | None, chat_id: str | None) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            UPDATE telegram_pending_actions
            SET outbound_message_id=COALESCE(?, outbound_message_id),
                chat_id=COALESCE(?, chat_id)
            WHERE source_key=?
            """,
            (int(message_id) if message_id else None, str(chat_id or "").strip() or None, source_key),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_nutrition_prompt_response(source_key: str, *, status: str, response_text: str, response_message_id: int | None = None) -> None:
    _mark_proposal_response(
        source_key,
        status=status,
        response_text=response_text,
        response_message_id=response_message_id,
    )


def _find_pending_nutrition_prompt_by_outbound(message_id: int) -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM telegram_pending_actions
            WHERE domain='nutrition'
              AND outbound_message_id=?
              AND status='pending'
            LIMIT 1
            """,
            (int(message_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _find_unanswered_nutrition_prompt_for_day(day_iso: str) -> dict[str, Any] | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM telegram_pending_actions
            WHERE domain='nutrition'
              AND status='pending'
              AND COALESCE(outbound_message_id, 0) > 0
              AND source_key LIKE ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"nutrition-core:{day_iso}:%",),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _nutrition_prompt_mute_key(day_iso: str) -> str:
    return f"nutrition_prompt_muted:{day_iso}"


def _is_nutrition_prompt_muted(day_iso: str) -> bool:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM telegram_notice_state WHERE notice_key=? LIMIT 1",
            (_nutrition_prompt_mute_key(day_iso),),
        ).fetchone()
        return bool(row and str(row["status"] or "").lower() == "muted")
    finally:
        conn.close()


def _set_nutrition_prompt_muted(day_iso: str) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO telegram_notice_state (notice_key, status, last_sent_at, raw_json)
            VALUES (?, 'muted', ?, '{}')
            ON CONFLICT(notice_key) DO UPDATE SET
                status='muted',
                last_sent_at=excluded.last_sent_at
            """,
            (_nutrition_prompt_mute_key(day_iso), _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def _now_min_berlin() -> int:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    return now.hour * 60 + now.minute


def _apply_confirmed_core_action_to_planned_status(action: dict[str, Any], *, day_iso: str) -> None:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    slot_id = int(payload.get("slot_id") or 0)
    if slot_id <= 0:
        return
    action_type = str(action.get("action_type") or "").strip().lower()
    if action_type == "shift_meal":
        set_planned_meal_status(
            slot_id,
            date_iso=day_iso,
            status="shifted",
            shifted_time_text=str(payload.get("to_time") or "").strip() or None,
            status_source="core",
            reason_code="CORE_TELEGRAM_CONFIRMED",
            trace={"core_action_id": action.get("id"), "action_type": action_type},
        )
    elif action_type in {"adjust_servings", "replace_meal"}:
        set_planned_meal_status(
            slot_id,
            date_iso=day_iso,
            status="changed",
            status_source="core",
            reason_code="CORE_TELEGRAM_CONFIRMED",
            trace={"core_action_id": action.get("id"), "action_type": action_type},
        )


def _action_rich_text(action: dict[str, Any], day_payload: dict[str, Any]) -> str:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    slot_id = int(payload.get("slot_id") or 0)
    meal = next((m for m in (day_payload.get("planned_meals") or []) if int(m.get("slot_id") or 0) == slot_id), None)
    meal_title = str((meal or {}).get("title") or "Meal").strip() or "Meal"
    meal_time = str((meal or {}).get("time_text") or "—").strip() or "—"
    action_type = str(action.get("action_type") or "")
    if action_type == "shift_meal":
        to_time = str(payload.get("to_time") or "—").strip() or "—"
        return f"{meal_title} ({meal_time}) auf {to_time} verschieben"
    if action_type == "adjust_servings":
        from_sv = payload.get("from_servings")
        to_sv = payload.get("to_servings")
        return f"{meal_title}: Menge {from_sv}x -> {to_sv}x anpassen"
    return str(action.get("human") or action_type or "CORE Aktion")


def _is_stale_core_nutrition_action(action: dict[str, Any], day_payload: dict[str, Any], now_min: int) -> tuple[bool, str]:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    slot_id = int(payload.get("slot_id") or 0)
    action_type = str(action.get("action_type") or "").strip().lower()
    if slot_id <= 0:
        # Target-level actions without slot are still valid intraday.
        return False, ""
    meal = next(
        (m for m in (day_payload.get("planned_meals") or []) if int((m or {}).get("slot_id") or 0) == slot_id),
        None,
    )
    if not isinstance(meal, dict):
        return True, "meal_not_found"
    status = str(meal.get("status") or "open").strip().lower()
    if status in {"logged", "telegram_confirmed", "skipped", "missed", "manual_override", "changed"}:
        return True, f"meal_status_{status}"
    tmin = _hhmm_to_minutes(_meal_time_text(meal))
    if tmin is None:
        return False, ""
    # Do not talk about old slots long after their window has passed.
    past_window_min = 90 if action_type in {"adjust_servings", "replace_meal"} else 60
    if now_min > (tmin + past_window_min):
        return True, "time_window_passed"
    return False, ""


def _is_fresh_core_action_for_push(action: dict[str, Any], *, max_age_minutes: int = 35) -> bool:
    raw_ts = str(action.get("updated_at") or action.get("created_at") or "").strip()
    if not raw_ts:
        return False
    try:
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age <= timedelta(minutes=max(1, int(max_age_minutes)))
    except Exception:
        return False


def _maybe_send_pending_nutrition_prompt(day_iso: str) -> dict[str, Any]:
    now_min = _now_min_berlin()
    if now_min < 6 * 60 or now_min >= 23 * 60:
        return {"ok": True, "sent": False, "reason": "quiet_hours"}
    if _is_nutrition_prompt_muted(day_iso):
        return {"ok": True, "sent": False, "reason": "muted"}
    waiting = _find_unanswered_nutrition_prompt_for_day(day_iso)
    if waiting:
        return {"ok": True, "sent": False, "reason": "awaiting_response"}

    evaluated = evaluate_core_nutrition_day(day_iso)
    base_payload = get_logging_day_payload(day_iso)
    day_payload = _effective_day_payload_for_runtime(day_iso, base_payload=base_payload, evaluated=evaluated)
    actions = get_core_nutrition_actions_for_day(day_iso) or []
    action = None
    for candidate in actions:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("status") or "proposed").strip().lower() not in {"", "proposed", "pending"}:
            continue
        stale, _ = _is_stale_core_nutrition_action(candidate, day_payload, now_min)
        if stale:
            continue
        action = candidate
        break
    if not action:
        return {"ok": True, "sent": False, "reason": "no_action"}
    source_key = f"nutrition-core:{day_iso}:{action.get('action_key') or action.get('id') or 'action'}"
    row = _upsert_nutrition_prompt(source_key=source_key, action=action)
    rich = _action_rich_text(action, day_payload)
    text = "\n".join(
        [
            "CORE Nutrition",
            rich,
            str(action.get("reason_text") or "").strip(),
            "",
            "Antworten:",
            "- ok",
            "- spaeter",
        ]
    ).strip()
    sent = send_domain_message("nutrition", text)
    if not sent.get("ok"):
        return {"ok": False, "sent": False, "error": sent.get("error") or "send_failed"}
    _mark_nutrition_prompt_sent(
        str(row.get("source_key") or source_key),
        message_id=int(sent.get("message_id") or 0) if sent.get("message_id") else None,
        chat_id=str(sent.get("chat_id") or "") or None,
    )
    return {"ok": True, "sent": True, "message_id": sent.get("message_id"), "source_key": source_key}


def _expire_nutrition_contexts(day_iso: str) -> int:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        cur = conn.execute(
            """
            UPDATE telegram_meal_context
            SET state='closed', closed_at=?
            WHERE domain='nutrition' AND day_iso=? AND state='open' AND expires_at <= ?
            """,
            (_utc_now(), day_iso, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _expire_stale_nutrition_prompts(day_iso: str) -> int:
    """Close open nutrition prompts from previous days at day boundary."""
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        cur = conn.execute(
            """
            UPDATE telegram_pending_actions
            SET
                status='expired_day_boundary',
                response_text=COALESCE(response_text, 'auto_expired_new_day'),
                responded_at=COALESCE(responded_at, ?)
            WHERE domain='nutrition'
              AND status='pending'
              AND source_key LIKE 'nutrition-core:%'
              AND source_key NOT LIKE ?
            """,
            (_utc_now(), f"nutrition-core:{day_iso}:%"),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _close_stale_nutrition_contexts(day_iso: str) -> int:
    """Close lingering open meal contexts from previous days."""
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        cur = conn.execute(
            """
            UPDATE telegram_meal_context
            SET state='closed', closed_at=?
            WHERE domain='nutrition' AND state='open' AND day_iso<>?
            """,
            (_utc_now(), day_iso),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _is_new_day_hard_reset_window(*, minute_window: int = 20) -> bool:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    now_min = now.hour * 60 + now.minute
    return 0 <= now_min < max(1, int(minute_window))


def _meal_reminder_dedupe_key(day_iso: str, meal: dict[str, Any]) -> str:
    slot_id = int(meal.get("slot_id") or 0)
    return f"nutrition:reminder:{day_iso}:{slot_id}"


MEAL_REMINDER_LIGHT = {
    "pulses": 1,
    "color": {"r": 40, "g": 190, "b": 90},
    "brightness": 100,
    "on_s": 3.0,
    "off_s": 0.25,
    "allow_tapo_fallback": False,
    "fallback_restore_state": {"is_on": True, "brightness": 35, "color": {"r": 255, "g": 180, "b": 120}},
    "govee_state_timeout": 3.0,
    "govee_state_retries": 6,
    "unknown_state_restore": "off",
}


def _trigger_meal_reminder_light() -> None:
    try:
        pulse_light(**MEAL_REMINDER_LIGHT)
    except Exception:
        pass


def _maybe_send_meal_reminder(day_iso: str, meal: dict[str, Any]) -> dict[str, Any]:
    dedupe_key = _meal_reminder_dedupe_key(day_iso, meal)
    if not _claim_message_log_slot(
        chat_id=None,
        message_type="meal_reminder",
        day_iso=day_iso,
        dedupe_key=dedupe_key,
        meal_slot_id=int(meal.get("slot_id") or 0),
        payload={"meal": meal, "state": "reserved"},
    ):
        return {"ok": True, "sent": False, "reason": "already_sent"}
    time_txt = _meal_time_text(meal)
    macros = meal.get("macros") if isinstance(meal.get("macros"), dict) else {}
    lines = [
        f"Denk dran: Meal {int(meal.get('slot_index') or 0) + 1}",
        f"{time_txt} · {str(meal.get('title') or 'Meal').strip() or 'Meal'}",
        _format_macros_short(macros),
        "",
        *_format_items_lines(meal.get("items") or []),
    ]
    text = "\n".join(line for line in lines if line is not None).strip()
    sent = send_domain_message("nutrition", text)
    if not sent.get("ok"):
        _release_message_log_claim(dedupe_key)
        return {"ok": False, "sent": False, "error": sent.get("error") or "send_failed"}
    _trigger_meal_reminder_light()
    _finalize_message_log(
        dedupe_key=dedupe_key,
        chat_id=str(sent.get("chat_id") or ""),
        context_id=None,
        sent_message_id=int(sent.get("message_id") or 0) if sent.get("message_id") else None,
        payload={"meal": meal},
    )
    return {"ok": True, "sent": True, "message_id": sent.get("message_id")}


def _run_meal_reminder_tick(day_iso: str, day_payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    minute_bucket = now.strftime("%Y-%m-%d %H:%M")
    if not _claim_notice_minute_bucket(
        notice_key="nutrition:meal_reminder_tick",
        bucket=minute_bucket,
        payload={"day_iso": day_iso},
    ):
        return {"ok": True, "sent": 0, "reason": "minute_bucket_already_processed"}
    now_min = now.hour * 60 + now.minute
    sent_count = 0
    for meal in (day_payload.get("planned_meals") or []):
        if not isinstance(meal, dict):
            continue
        status = str(meal.get("status") or "open").lower()
        if status in {"logged", "telegram_confirmed", "skipped", "missed"}:
            continue
        tmin = _hhmm_to_minutes(_meal_time_text(meal))
        if tmin is None:
            continue
        # reminder: exactly 15 minutes before the current final meal time
        if now_min != (tmin - 15):
            continue
        out = _maybe_send_meal_reminder(day_iso, meal)
        if out.get("sent"):
            sent_count += 1
    return {"ok": True, "sent": sent_count}


def _run_core_info_tick(day_iso: str) -> dict[str, Any]:
    return {"ok": True, "sent": 0, "reason": "disabled_meal_reminder_only"}


def _run_nutrition_periodic_tick(day_iso: str) -> dict[str, Any]:
    stale_prompts = _expire_stale_nutrition_prompts(day_iso)
    stale_contexts = _close_stale_nutrition_contexts(day_iso)
    base_payload = get_logging_day_payload(day_iso)
    try:
        day_payload = _effective_day_payload_for_runtime(day_iso, base_payload=base_payload)
    except TypeError:
        day_payload = _effective_day_payload_for_runtime(day_iso)
    expired = _expire_nutrition_contexts(day_iso)
    if _is_new_day_hard_reset_window(minute_window=20):
        return {
            "ok": True,
            "expired_contexts": expired,
            "stale_contexts_closed": stale_contexts,
            "stale_prompts_closed": stale_prompts,
            "hard_day_reset": True,
            "reminder": {"ok": True, "sent": 0, "reason": "new_day_hard_reset_window"},
            "core_prompt": {"ok": True, "sent": False, "reason": "disabled_meal_reminder_only"},
            "core_info": {"ok": True, "sent": 0, "reason": "disabled_meal_reminder_only"},
        }
    reminder = _run_meal_reminder_tick(day_iso, day_payload)
    core_prompt = _maybe_send_pending_nutrition_prompt(day_iso)
    try:
        core_info = _run_core_info_tick(day_iso, day_payload=day_payload)
    except TypeError:
        core_info = _run_core_info_tick(day_iso)
    return {
        "ok": True,
        "expired_contexts": expired,
        "stale_contexts_closed": stale_contexts,
        "stale_prompts_closed": stale_prompts,
        "hard_day_reset": False,
        "reminder": reminder,
        "core_prompt": core_prompt,
        "core_info": core_info,
    }


def _resolve_meal(day_payload: dict[str, Any], slot_id: int) -> dict[str, Any] | None:
    return next((m for m in (day_payload.get("planned_meals") or []) if int(m.get("slot_id") or 0) == int(slot_id)), None)


def _context_prompt_text(meal: dict[str, Any], *, referenced_time: str | None = None) -> str:
    lines = [
        f"Meal {int(meal.get('slot_index') or 0) + 1} erkannt · {referenced_time or 'jetzt'}",
        f"Geplant: {str(meal.get('title') or 'Meal').strip() or 'Meal'}",
        _format_macros_short(meal.get("macros") or {}),
        "",
        *(_format_items_lines(meal.get("items") or [])),
        "",
        "Antworte mit done oder einer Änderung (z. B. 'Banane -> Apfel', '150g Haferflocken', 'ohne Whey').",
    ]
    return "\n".join(lines)


def _apply_action_to_context_items(context: dict[str, Any], action: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw = dict(context.get("raw") or {})
    items = [dict(i) for i in (raw.get("pending_items") or []) if isinstance(i, dict)]
    edits = [str(e) for e in (raw.get("applied_edits") or []) if str(e or "").strip()]
    atype = str(action.get("type") or "")
    if atype == "undo":
        items = [dict(i) for i in (raw.get("base_items") or []) if isinstance(i, dict)]
        raw["pending_items"] = items
        raw["pending_macros"] = _compute_macros_for_items(items)
        raw["last_edit"] = {"type": "undo"}
        return raw, "Änderungen zurückgesetzt."
    if atype == "quantity":
        idx = _find_item_index(items, str(action.get("name") or ""))
        if idx is None:
            foods = search_logging_foods(str(action.get("name") or ""), limit=8)
            food = foods[0] if foods else None
            if not food or not food.get("id"):
                return raw, "Item nicht eindeutig gefunden."
            unit = str(action.get("unit") or food.get("unit_default") or "g").lower().replace("gr", "g").replace("gramm", "g").replace("stück", "pcs").replace("stk", "pcs")
            items.append(
                {
                    "item_type": "food",
                    "food_id": int(food.get("id") or 0),
                    "food_name": str(food.get("name") or action.get("name") or "").strip(),
                    "amount": float(action.get("amount") or 0.0),
                    "unit": unit,
                }
            )
            edit_name = str(food.get("name") or action.get("name") or "").strip()
            edit_amount = float(action.get("amount") or 0.0)
        else:
            unit = str(action.get("unit") or items[idx].get("unit") or "g").lower().replace("gr", "g").replace("gramm", "g").replace("stück", "pcs").replace("stk", "pcs")
            items[idx]["amount"] = float(action.get("amount") or items[idx].get("amount") or 0.0)
            items[idx]["unit"] = unit
            edit_name = _item_name(items[idx])
            edit_amount = float(items[idx].get("amount") or 0.0)
        raw["pending_items"] = items
        raw["pending_macros"] = _compute_macros_for_items(items)
        raw["last_edit"] = {"type": "quantity", "name": edit_name, "amount": edit_amount, "unit": unit}
        edits.append(f"{edit_name} {edit_amount:.1f}{unit}")
        raw["applied_edits"] = edits
        return raw, None
    if atype == "remove":
        idx = _find_item_index(items, str(action.get("name") or ""))
        if idx is None:
            return raw, "Item nicht eindeutig gefunden."
        removed = _item_name(items[idx])
        items.pop(idx)
        raw["pending_items"] = items
        raw["pending_macros"] = _compute_macros_for_items(items)
        raw["last_edit"] = {"type": "remove", "name": removed}
        edits.append(f"ohne {removed}")
        raw["applied_edits"] = edits
        return raw, None
    if atype == "replace":
        from_name = str(action.get("from") or "").strip()
        to_name = str(action.get("to") or "").strip()
        pending_title = str(raw.get("pending_title") or context.get("meal_title") or "").strip()
        if from_name and to_name and _normalize_food_label(from_name) == _normalize_food_label(pending_title):
            templates = list_meal_templates(query=to_name, limit=25)
            tpl = templates[0] if templates else None
            if not tpl or not tpl.get("id"):
                return raw, f"Ersatz-Meal nicht gefunden: {to_name}."
            resolved = resolve_meal_template(int(tpl.get("id") or 0)) or {}
            ingredients = [dict(i) for i in (resolved.get("ingredients") or []) if isinstance(i, dict)]
            raw["pending_title"] = str(resolved.get("title") or tpl.get("title") or to_name).strip()
            raw["pending_items"] = ingredients
            raw["pending_macros"] = _compute_macros_for_items(ingredients)
            raw["last_edit"] = {"type": "replace_meal", "from": pending_title, "to": raw["pending_title"]}
            return raw, None
        idx = _find_item_index(items, str(action.get("from") or ""))
        if idx is None:
            return raw, "Original-Item nicht gefunden."
        foods = search_logging_foods(to_name, limit=4)
        food = foods[0] if foods else None
        if not food or not food.get("id"):
            return raw, f"Ersatz-Food nicht gefunden: {to_name}."
        old = _item_name(items[idx])
        items[idx]["food_id"] = int(food["id"])
        items[idx]["food_name"] = str(food.get("name") or to_name)
        items[idx]["unit"] = str(food.get("unit_default") or items[idx].get("unit") or "g")
        raw["pending_items"] = items
        raw["pending_macros"] = _compute_macros_for_items(items)
        raw["last_edit"] = {"type": "replace", "from": old, "to": items[idx]["food_name"]}
        edits.append(f"{old} -> {items[idx]['food_name']}")
        raw["applied_edits"] = edits
        return raw, None
    if atype == "size":
        updated, marker = _apply_size_modifier(items, str(action.get("value") or ""))
        raw["pending_items"] = updated
        raw["pending_macros"] = _compute_macros_for_items(updated)
        raw["last_edit"] = {"type": "size", "value": marker}
        edits.append(marker)
        raw["applied_edits"] = edits
        return raw, None
    if atype == "scale_factor":
        factor = float(action.get("factor") or 1.0)
        updated = []
        for item in items:
            row = dict(item)
            if isinstance(row.get("amount"), (int, float)):
                row["amount"] = round(float(row["amount"]) * factor, 2)
            updated.append(row)
        raw["pending_items"] = updated
        raw["pending_macros"] = _compute_macros_for_items(updated)
        raw["last_edit"] = {"type": "scale_factor", "factor": factor}
        edits.append(f"{factor:.2f}x")
        raw["applied_edits"] = edits
        return raw, None
    return raw, "Änderung nicht verstanden."


def _ensure_context_for_slot(
    *,
    active_ctx: dict[str, Any] | None,
    day: dict[str, Any],
    day_iso: str,
    chat_id: str,
    slot_id: int,
    referenced_time: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if active_ctx and int(active_ctx.get("meal_slot_id") or 0) == int(slot_id):
        if referenced_time and str(active_ctx.get("referenced_time") or "").strip() != referenced_time:
            active_ctx = dict(active_ctx)
            active_ctx["referenced_time"] = referenced_time
        return active_ctx, _resolve_meal(day, slot_id)
    meal = _resolve_meal(day, slot_id)
    if not meal:
        return None, None
    ctx = _open_meal_context(
        chat_id=chat_id,
        day_iso=day_iso,
        meal=meal,
        referenced_time=referenced_time,
        source_message_type="user_meal_reference",
    )
    if referenced_time and ctx is not None:
        ctx = dict(ctx)
        ctx["referenced_time"] = referenced_time
    return ctx, meal


def _handle_context_actions(
    context: dict[str, Any],
    *,
    action_text: str,
    day_iso: str,
    chat_id: str,
) -> str:
    actions = _parse_context_actions(action_text)
    if not actions:
        action = _parse_context_action(action_text)
        actions = [action] if str(action.get("type") or "") != "unknown" else []
    if not actions:
        return "Änderung nicht verstanden."
    working = dict(context)
    working["raw"] = dict(context.get("raw") or {})
    for action in actions:
        updated_raw, err_msg = _apply_action_to_context_items(working, action)
        if err_msg:
            return err_msg
        working["raw"] = updated_raw
    _save_context_raw(int(context.get("id") or 0), working.get("raw") or {})
    return _context_summary_text(working)


def _log_context_meal(context: dict[str, Any], *, day_iso: str, chat_id: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = dict(context.get("raw") or {})
    slot_id = int(context.get("meal_slot_id") or 0)
    if slot_id <= 0:
        return None, "meal_context_invalid"
    referenced_time = str(context.get("referenced_time") or "").strip() or None
    pending_title = str(raw.get("pending_title") or context.get("meal_title") or "").strip() or None
    if not referenced_time:
        day = get_logging_day_payload(day_iso)
        meal = _resolve_meal(day, slot_id)
        referenced_time = _meal_time_text(meal or {}) if isinstance(meal, dict) else None
    if referenced_time:
        logged_at = f"{day_iso}T{referenced_time}:00+01:00"
        data, err = log_planned_meal(
            slot_id,
            date_iso=day_iso,
            time_mode="custom",
            custom_logged_at=logged_at,
            items=raw.get("pending_items") or [],
            title=pending_title,
            status_source="telegram",
            reason_code="TELEGRAM_CONTEXT_DONE",
            trace={"context_id": int(context.get("id") or 0), "chat_id": chat_id},
        )
    else:
        data, err = log_planned_meal(
            slot_id,
            date_iso=day_iso,
            time_mode="now",
            items=raw.get("pending_items") or [],
            title=pending_title,
            status_source="telegram",
            reason_code="TELEGRAM_CONTEXT_DONE",
            trace={"context_id": int(context.get("id") or 0), "chat_id": chat_id},
        )
    return data, err


def process_nutrition_query_message(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"ok": False, "error": "invalid_message"}
    text = str(message.get("text") or "").strip()
    if not text:
        return {"ok": False, "ignored": True, "error": "empty_message"}
    low = _normalize_text(text)
    day_iso = _today_iso_berlin()
    day = get_logging_day_payload(day_iso)
    runtime_day = _effective_day_payload_for_runtime(day_iso, base_payload=day)
    answer = None
    reply_to = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    reply_to_message_id = int(reply_to.get("message_id") or 0) if reply_to.get("message_id") is not None else 0
    pending_from_reply = _find_pending_nutrition_prompt_by_outbound(reply_to_message_id) if reply_to_message_id > 0 else None
    inbound_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    inbound_chat_id = str(inbound_chat.get("id") or "").strip() or ""
    active_ctx = _get_active_meal_context(inbound_chat_id, day_iso) if inbound_chat_id else None

    if low in {"help", "hilfe", "commands", "command", "cmd"}:
        answer = _nutrition_help_text()
    elif low == "today":
        answer = _format_today_text(day, runtime_day, active_ctx)
    elif low == "open":
        answer = _format_open_text(day)
    elif low in {"makros", "macros", "macro"}:
        answer = _format_macro_overview(day)
    elif low == "meals":
        answer = _format_meal_rows(list_meal_templates(limit=500), title="Meals (Datenbank)")
    elif low == "foods":
        answer = _format_food_rows(list_foods(limit=500), title="Foods (Datenbank)")
    elif low.startswith("search meal"):
        term = _search_quoted_term(text.replace("search meal", "", 1).strip())
        rows = list_meal_templates(query=term, limit=100)
        answer = _format_meal_rows(rows, title=f'Search Meals: "{term}"')
    elif low.startswith("search food"):
        term = _search_quoted_term(text.replace("search food", "", 1).strip())
        rows = list_foods(query=term, limit=100, favorites_only=False)
        answer = _format_food_rows(rows, title=f'Search Foods: "{term}"')
    elif low.startswith("search "):
        term = _search_quoted_term(text)
        meal_rows = list_meal_templates(query=term, limit=100)
        food_rows = list_foods(query=term, limit=100, favorites_only=False)
        answer = _format_search_overview(term, meal_rows, food_rows)
    else:
        skip_match = re.search(r"\bskip\s+meal\s+([1-9])\b", low)
        if skip_match:
            meal_no = int(skip_match.group(1))
            slot_id = _slot_id_from_meal_number(day, meal_no)
            if slot_id:
                _, err = set_planned_meal_status(
                    slot_id,
                    date_iso=day_iso,
                    status="skipped",
                    status_source="telegram",
                )
                if not err:
                    answer = f"Meal {meal_no} als ausgelassen markiert."
                else:
                    answer = f"Konnte Meal {meal_no} nicht auslassen ({err})."

    if answer is None and low in {"ok", "passt", "bestätigt", "confirm"} and active_ctx and not pending_from_reply:
        answer = "Zum Loggen bitte 'done' schreiben."
    if answer is None and low in {"ok", "passt", "bestätigt", "confirm"}:
        confirmed = None
        prompt_for_response = pending_from_reply
        if pending_from_reply:
            raw = {}
            try:
                raw = json.loads(pending_from_reply.get("raw_json") or "{}")
            except Exception:
                raw = {}
            action_id = int(raw.get("id") or 0)
            if action_id > 0:
                confirmed = mark_core_action_telegram_confirmed(action_id)
                _mark_nutrition_prompt_response(
                    str(pending_from_reply.get("source_key") or ""),
                    status="confirmed",
                    response_text=text,
                    response_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
                )
        elif not active_ctx:
            prompt_for_response = _find_unanswered_nutrition_prompt_for_day(day_iso)
            if prompt_for_response:
                raw = {}
                try:
                    raw = json.loads(prompt_for_response.get("raw_json") or "{}")
                except Exception:
                    raw = {}
                action_id = int(raw.get("id") or 0)
                if action_id > 0:
                    confirmed = mark_core_action_telegram_confirmed(action_id)
                    _mark_nutrition_prompt_response(
                        str(prompt_for_response.get("source_key") or ""),
                        status="confirmed",
                        response_text=text,
                        response_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
                    )
        if confirmed is None:
            confirmed = mark_last_core_action_telegram_confirmed(day_iso)
        if confirmed:
            _apply_confirmed_core_action_to_planned_status(confirmed, day_iso=day_iso)
            answer = f"CORE angepasst: {confirmed.get('human') or confirmed.get('action_type')}"
        else:
            answer = "Keine offene CORE-Nutrition-Aktion zum Bestätigen gefunden."
    elif answer is None and low in {"nein", "n", "no", "verwerfen", "ablehnen"}:
        rejected = None
        prompt_for_response = pending_from_reply
        if pending_from_reply:
            raw = {}
            try:
                raw = json.loads(pending_from_reply.get("raw_json") or "{}")
            except Exception:
                raw = {}
            action_id = int(raw.get("id") or 0)
            if action_id > 0:
                rejected = mark_core_action_rejected(action_id)
                _mark_nutrition_prompt_response(
                    str(pending_from_reply.get("source_key") or ""),
                    status="rejected",
                    response_text=text,
                    response_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
                )
        elif not active_ctx:
            prompt_for_response = _find_unanswered_nutrition_prompt_for_day(day_iso)
            if prompt_for_response:
                raw = {}
                try:
                    raw = json.loads(prompt_for_response.get("raw_json") or "{}")
                except Exception:
                    raw = {}
                action_id = int(raw.get("id") or 0)
                if action_id > 0:
                    rejected = mark_core_action_rejected(action_id)
                    _mark_nutrition_prompt_response(
                        str(prompt_for_response.get("source_key") or ""),
                        status="rejected",
                        response_text=text,
                        response_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
                    )
        if rejected is None:
            rejected = mark_last_core_action_rejected(day_iso)
        if rejected:
            answer = f"CORE-Aktion verworfen: {rejected.get('human') or rejected.get('action_type')}"
        else:
            answer = "Keine offene CORE-Nutrition-Aktion zum Verwerfen gefunden."

    meal_no_from_inline, explicit_time_inline, inline_action_text = _extract_inline_meal_command(text)
    if answer is None and meal_no_from_inline:
        slot_id_inline = _slot_id_from_meal_number(day, meal_no_from_inline)
        if slot_id_inline:
            ctx, meal = _ensure_context_for_slot(
                active_ctx=active_ctx,
                day=day,
                day_iso=day_iso,
                chat_id=inbound_chat_id,
                slot_id=slot_id_inline,
                referenced_time=explicit_time_inline,
            )
            if ctx and meal:
                stripped_action = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", "", inline_action_text).strip()
                if stripped_action in {"done", "done,", ",done"} or ("done" in _normalize_text(stripped_action) and len(_normalize_text(stripped_action).replace("done", "").strip()) == 0):
                    data, err = _log_context_meal(ctx, day_iso=day_iso, chat_id=inbound_chat_id)
                    if err:
                        answer = f"Meal konnte nicht geloggt werden ({err})."
                    else:
                        _close_meal_context(int(ctx.get("id") or 0), reason="logged")
                        raw = dict(ctx.get("raw") or {})
                        pm = raw.get("pending_macros") if isinstance(raw.get("pending_macros"), dict) else {}
                        tline = str(ctx.get("referenced_time") or _meal_time_text(meal) or "jetzt")
                        title = str(raw.get("pending_title") or ctx.get("meal_title") or "Meal").strip() or "Meal"
                        lines = [f"{title} geloggt · {tline}", _format_macros_short(pm)]
                        item_lines = _format_items_lines(raw.get("pending_items") or [])
                        if item_lines:
                            lines.extend(["", *item_lines])
                        answer = "\n".join(lines)
                elif stripped_action:
                    answer = _handle_context_actions(ctx, action_text=stripped_action, day_iso=day_iso, chat_id=inbound_chat_id)
                else:
                    answer = _context_prompt_text(meal, referenced_time=explicit_time_inline)

    slot_id_from_ref, explicit_time = _parse_meal_reference(text, day)
    if answer is None and slot_id_from_ref:
        meal = _resolve_meal(day, slot_id_from_ref)
        if meal:
            is_time_shift = bool(
                explicit_time
                and re.search(r"\b(?:auf|um|to)\s+([01]?\d|2[0-3]):[0-5]\d\b", low)
            )
            if is_time_shift:
                _, err = set_planned_meal_status(
                    slot_id_from_ref,
                    date_iso=day_iso,
                    status="shifted",
                    shifted_time_text=explicit_time,
                    status_source="telegram",
                    reason_code="TELEGRAM_SHIFT",
                    trace={"raw": text, "message_id": message.get("message_id")},
                )
                if err:
                    answer = f"Meal konnte nicht verschoben werden ({err})."
                else:
                    meal_no = int(meal.get("slot_index") or 0) + 1
                    answer = f"⏰ Meal {meal_no} auf <b>{explicit_time}</b> verschoben."
            else:
                ctx = _open_meal_context(
                    chat_id=inbound_chat_id,
                    day_iso=day_iso,
                    meal=meal,
                    referenced_time=explicit_time,
                    source_message_type="user_meal_reference",
                )
                answer = _context_prompt_text(meal, referenced_time=explicit_time)
                _append_message_log(
                    chat_id=inbound_chat_id,
                    message_type="meal_context_open",
                    day_iso=day_iso,
                    dedupe_key=f"nutrition:ctxopen:{day_iso}:{int(ctx.get('id') or 0)}",
                    meal_slot_id=int(meal.get("slot_id") or 0),
                    context_id=int(ctx.get("id") or 0) if ctx else None,
                    payload={"source": "user_meal_reference", "time": explicit_time},
                )

    if answer is None and active_ctx and int(active_ctx.get("meal_slot_id") or 0) > 0:
        action = _parse_context_action(text)
        atype = str(action.get("type") or "")
        if atype == "why":
            last_edit = (active_ctx.get("raw") or {}).get("last_edit")
            why = "Aktiver Meal-Kontext aus letzter Erinnerung/Referenz."
            if isinstance(last_edit, dict) and last_edit.get("type"):
                why = f"Letzte Änderung: {last_edit.get('type')}."
            answer = why
        elif atype == "alt":
            answer = "Alternative: Schreib z. B. 'portable' oder 'Banane -> Apfel'."
        elif atype == "skip":
            _, err = set_planned_meal_status(
                int(active_ctx.get("meal_slot_id") or 0),
                date_iso=day_iso,
                status="skipped",
                status_source="telegram",
                reason_code="TELEGRAM_SKIP",
                trace={"raw": text, "context_id": int(active_ctx.get("id") or 0)},
            )
            if not err:
                _close_meal_context(int(active_ctx.get("id") or 0), reason="skipped")
                answer = f"{str(active_ctx.get('meal_title') or 'Meal')} als ausgelassen markiert."
            else:
                answer = f"Konnte Meal nicht auslassen ({err})."
        elif atype == "done":
            data, err = _log_context_meal(active_ctx, day_iso=day_iso, chat_id=inbound_chat_id)
            if err:
                answer = f"Meal konnte nicht geloggt werden ({err})."
            else:
                _close_meal_context(int(active_ctx.get("id") or 0), reason="logged")
                raw = dict(active_ctx.get("raw") or {})
                pm = raw.get("pending_macros") if isinstance(raw.get("pending_macros"), dict) else {}
                tline = str(action.get("time") or active_ctx.get("referenced_time") or "jetzt")
                title = str(raw.get("pending_title") or active_ctx.get("meal_title") or "Meal").strip() or "Meal"
                lines = [f"{title} geloggt · {tline}", _format_macros_short(pm or {})]
                item_lines = _format_items_lines(raw.get("pending_items") or [])
                if item_lines:
                    lines.extend(["", *item_lines])
                answer = "\n".join(lines)
        else:
            answer = _handle_context_actions(active_ctx, action_text=text, day_iso=day_iso, chat_id=inbound_chat_id)

    if answer is None:
        amount, unit, name = _nutrition_parse_food_amount(text)
        if amount is not None and name:
            foods = search_logging_foods(name, limit=8)
            food = foods[0] if foods else None
            if food and food.get("id"):
                payload = {
                    "item_type": "food",
                    "food_id": int(food["id"]),
                    "food_name": str(food.get("name") or name),
                    "amount": amount,
                    "unit": unit or str(food.get("unit_default") or "g"),
                }
                data, err = create_free_logged_meal(
                    date_iso=day_iso,
                    title=f"Telegram: {payload['food_name']}",
                    meal_slot="telegram",
                    items=[payload],
                    source="telegram",
                )
                if err:
                    answer = f"Konnte Food nicht loggen ({err})."
                else:
                    logged = data.get("logged_totals") or {}
                    answer = f"{payload['food_name']} geloggt ({amount:g}{payload['unit']}). Heute: {int(logged.get('kcal') or 0)} kcal."
            else:
                answer = f"Food nicht gefunden: {name}."

    if answer is None:
        answer = "Nicht eindeutig. Schreib z. B. 'meal 2, 17:16', 'done', 'ohne Whey', 'Banane -> Apfel' oder 'skip'."

    inbound_chat_id = inbound_chat_id or None
    sent = send_domain_message(
        "nutrition",
        answer,
        chat_id=inbound_chat_id,
        reply_to_message_id=int(message.get("message_id") or 0) if message.get("message_id") is not None else None,
    )
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error") or "send_failed"}
    return {"ok": True, "status": "answered"}


def process_nutrition_inbox(*, timeout: int = 0) -> dict[str, Any]:
    # This worker runs outside Flask's before_app_request lifecycle. Prepare
    # the schema explicitly before its first read so the read projection itself
    # remains free of migration side effects.
    ensure_nutrition_planning_schema()
    day_iso = _today_iso_berlin()
    updates = fetch_updates("nutrition", timeout=timeout)
    processed = 0
    ignored = 0
    errors: list[str] = []
    for update in updates:
        message = update.get("message") if isinstance(update.get("message"), dict) else None
        if not message:
            ignored += 1
            continue
        result = process_nutrition_query_message(message)
        if result.get("ok"):
            processed += 1
        elif result.get("ignored"):
            ignored += 1
        else:
            ignored += 1
            errors.append(str(result.get("error") or "unknown"))
    tick_info = _run_nutrition_periodic_tick(day_iso)
    if not tick_info.get("ok"):
        errors.append(f"nutrition_tick_failed:{tick_info.get('error') or 'unknown'}")
    return {
        "ok": True,
        "updates": len(updates),
        "processed": processed,
        "ignored": ignored,
        "errors": errors[:10],
        "tick": tick_info,
    }


def process_telegram_inboxes(*, timeout: int = 0) -> dict[str, Any]:
    core = process_core_inbox(timeout=timeout)
    training = process_training_inbox(timeout=timeout)
    nutrition = process_nutrition_inbox(timeout=timeout)
    system = process_system_inbox(timeout=timeout)
    return {
        "ok": bool(core.get("ok")) and bool(training.get("ok")) and bool(nutrition.get("ok")) and bool(system.get("ok")),
        "core": core,
        "training": training,
        "nutrition": nutrition,
        "system": system,
        "processed": int(core.get("processed") or 0) + int(training.get("processed") or 0) + int(nutrition.get("processed") or 0) + int(system.get("processed") or 0),
        "updates": int(core.get("updates") or 0) + int(training.get("updates") or 0) + int(nutrition.get("updates") or 0) + int(system.get("updates") or 0),
        "ignored": int(core.get("ignored") or 0) + int(training.get("ignored") or 0) + int(nutrition.get("ignored") or 0) + int(system.get("ignored") or 0),
        "errors": [*list(core.get("errors") or []), *list(training.get("errors") or []), *list(nutrition.get("errors") or []), *list(system.get("errors") or [])][:20],
    }


def send_stateful_system_message(
    *,
    notice_key: str,
    status: str,
    message: str,
    min_repeat_minutes: int = 120,
    force: bool = False,
) -> dict[str, Any]:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, last_sent_at FROM telegram_notice_state WHERE notice_key=? LIMIT 1",
            (str(notice_key or "").strip(),),
        ).fetchone()
    finally:
        conn.close()
    should_send = force or row is None or str(row["status"] or "") != str(status or "")
    if (not should_send) and row and min_repeat_minutes > 0:
        try:
            last_dt = datetime.fromisoformat(str(row["last_sent_at"]).replace("Z", "+00:00"))
            should_send = last_dt <= (datetime.now(timezone.utc) - timedelta(minutes=int(min_repeat_minutes)))
        except Exception:
            should_send = True
    if not should_send:
        return {"ok": True, "sent": False, "status": status}
    sent = send_domain_message("system", message)
    if not sent.get("ok"):
        return {"ok": False, "sent": False, "error": sent.get("error") or "send_failed"}
    conn2 = get_core_db()
    try:
        conn2.execute(
            """
            INSERT INTO telegram_notice_state (notice_key, status, last_sent_at, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(notice_key) DO UPDATE SET
                status=excluded.status,
                last_sent_at=excluded.last_sent_at,
                raw_json=excluded.raw_json
            """,
            (
                str(notice_key or "").strip(),
                str(status or "").strip(),
                _utc_now(),
                json.dumps({"message": message}, ensure_ascii=False),
            ),
        )
        conn2.commit()
    finally:
        conn2.close()
    return {"ok": True, "sent": True, "status": status}
