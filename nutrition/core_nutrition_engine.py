from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from database.connections import get_nutrition_db, get_plans_db, get_training_db
from core.core_training_policy import get_core_policy_profile, get_core_question_policy_signal
from core.core_training_schedule import resolve_training_context
from nutrition.nutrition_planning_db import get_logging_day_payload
from nutrition.core_nutrition_daystate import append_day_event, build_active_day_plan, ensure_core_day_state_schema


BERLIN = ZoneInfo("Europe/Berlin")
REASON_CODES = {
    "EARLY_HOME",
    "LONG_SCHOOL_DAY",
    "TRAINING_LATE",
    "PROTEIN_BEHIND",
    "CARBS_BEHIND",
    "KCAL_BEHIND",
    "WEIGHT_TREND_LOW",
    "INVENTORY_LOW",
    "MISSED_MEAL",
    "LOW_TIME_AVAILABLE",
    "TRAINING_WINDOW",
    "TRAINING_MACRO_SHIFT",
}

DECISION_DOMAIN_NUTRITION = "nutrition"
DECISION_STATUS_LABELS = {
    "monitoring": "Beobachtet",
    "proposed": "Vorgeschlagen",
    "applied": "Aktiv angepasst",
    "guarded": "Schutz aktiv",
    "manual_override": "Manuell überschrieben",
    "recomputed": "Neu berechnet",
}


@dataclass(frozen=True)
class MealCandidate:
    slot_id: int
    slot_index: int
    title: str
    time_text: str
    status: str
    macros: dict[str, float]
    tags: str
    items_count: int
    servings: float


def _now_berlin() -> datetime:
    return datetime.now(BERLIN)


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _normalize_date_iso(raw: Optional[str]) -> str:
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10]).isoformat()
        except Exception:
            pass
    return _now_berlin().date().isoformat()


def _safe_int(x: Any, fallback: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return fallback


def _safe_float(x: Any, fallback: float = 0.0) -> float:
    try:
        val = float(x)
    except Exception:
        return fallback
    if not math.isfinite(val):
        return fallback
    return val


def _parse_hhmm(text: Any) -> Optional[int]:
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
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


def _schedule_from_latest_file(day_iso: str) -> dict[str, Any]:
    path = Path("/opt/liva/var/schoolsync/latest_today.json")
    if not path.exists():
        return {
            "source": "schoolsync_latest_json",
            "available": False,
            "entries": [],
            "free_windows": [],
            "school_start": None,
            "school_end": None,
            "early_home": False,
            "long_school_day": False,
            "largest_free_window_min": 0,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "source": "schoolsync_latest_json",
            "available": False,
            "entries": [],
            "free_windows": [],
            "school_start": None,
            "school_end": None,
            "early_home": False,
            "long_school_day": False,
            "largest_free_window_min": 0,
        }
    rows = [
        e for e in (payload.get("entries_visible") or payload.get("entries") or [])
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
    merged: list[list[int]] = []
    for st, en in ranges:
        if not merged or st > merged[-1][1]:
            merged.append([st, en])
        else:
            merged[-1][1] = max(merged[-1][1], en)
    free_windows: list[dict[str, Any]] = []
    for idx in range(len(merged) - 1):
        a = merged[idx][1]
        b = merged[idx + 1][0]
        if b > a:
            free_windows.append({"start": _to_hhmm(a), "end": _to_hhmm(b), "minutes": b - a})
    school_start = _to_hhmm(merged[0][0]) if merged else None
    school_end = _to_hhmm(merged[-1][1]) if merged else None
    school_end_min = merged[-1][1] if merged else None
    return {
        "source": "schoolsync_latest_json",
        "available": bool(rows),
        "entries": rows,
        "free_windows": free_windows,
        "school_start": school_start,
        "school_end": school_end,
        "early_home": bool(school_end_min is not None and school_end_min <= (13 * 60 + 30)),
        "long_school_day": bool(school_end_min is not None and school_end_min >= (15 * 60 + 15)),
        "largest_free_window_min": max((int(w["minutes"]) for w in free_windows), default=0),
    }


def _training_context_from_active_plan(day_iso: str) -> dict[str, Any]:
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
    target_day = date.fromisoformat(day_iso)
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][target_day.weekday()]
    events = []

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


def _calendar_context_from_google(day_iso: str) -> dict[str, Any]:
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service, list_events
    except Exception:
        return {"available": False, "events": [], "source": "google_calendar"}

    try:
        cfg = load_config()
        service = build_service(cfg.credentials_file, cfg.token_file)
        time_min = f"{day_iso}T00:00:00+01:00"
        time_max = f"{day_iso}T23:59:59+01:00"
        events = list_events(
            service,
            calendar_id=cfg.default_calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=80,
        )
        compact = []
        for e in events:
            start = ((e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or "")
            end = ((e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date") or "")
            compact.append(
                {
                    "id": e.get("id"),
                    "title": str(e.get("summary") or "").strip() or "Termin",
                    "start": str(start),
                    "end": str(end),
                }
            )
        return {"available": True, "events": compact, "source": "google_calendar"}
    except Exception:
        return {"available": False, "events": [], "source": "google_calendar"}


def _weight_trend(day_iso: str, lookback_days: int = 21) -> dict[str, Any]:
    conn = get_nutrition_db()
    try:
        rows = conn.execute(
            """
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso <= ? AND date_iso >= ? AND weight_kg IS NOT NULL
            ORDER BY date_iso ASC
            """,
            (day_iso, (date.fromisoformat(day_iso) - timedelta(days=max(1, lookback_days))).isoformat()),
        ).fetchall()
    finally:
        conn.close()
    pts = [(str(r[0]), _safe_float(r[1], float("nan"))) for r in rows]
    pts = [(d, w) for d, w in pts if math.isfinite(w)]
    if len(pts) < 2:
        return {"available": False, "trend_kg_per_week": 0.0, "samples": len(pts)}
    d0 = date.fromisoformat(pts[0][0])
    d1 = date.fromisoformat(pts[-1][0])
    days = (d1 - d0).days
    if days <= 0:
        return {"available": False, "trend_kg_per_week": 0.0, "samples": len(pts)}
    trend = (pts[-1][1] - pts[0][1]) / (days / 7.0)
    return {"available": True, "trend_kg_per_week": round(trend, 3), "samples": len(pts), "first": pts[0][1], "last": pts[-1][1]}


def _macro_totals_from_logging(logging_payload: dict[str, Any]) -> dict[str, float]:
    logged = logging_payload.get("logged_totals") if isinstance(logging_payload.get("logged_totals"), dict) else {}
    planned = logging_payload.get("planned_totals") if isinstance(logging_payload.get("planned_totals"), dict) else {}
    remaining = logging_payload.get("remaining") if isinstance(logging_payload.get("remaining"), dict) else {}
    return {
        "logged_kcal": _safe_float(logged.get("kcal"), 0.0),
        "logged_p": _safe_float(logged.get("p"), 0.0),
        "logged_c": _safe_float(logged.get("c"), 0.0),
        "logged_f": _safe_float(logged.get("f"), 0.0),
        "planned_kcal": _safe_float(planned.get("kcal"), 0.0),
        "planned_p": _safe_float(planned.get("p"), 0.0),
        "planned_c": _safe_float(planned.get("c"), 0.0),
        "planned_f": _safe_float(planned.get("f"), 0.0),
        "remaining_kcal": _safe_float(remaining.get("kcal"), 0.0),
        "remaining_p": _safe_float(remaining.get("p"), 0.0),
        "remaining_c": _safe_float(remaining.get("c"), 0.0),
        "remaining_f": _safe_float(remaining.get("f"), 0.0),
    }


def _extract_meal_candidates(logging_payload: dict[str, Any]) -> list[MealCandidate]:
    out: list[MealCandidate] = []
    for row in (logging_payload.get("planned_meals") or []):
        if not isinstance(row, dict):
            continue
        out.append(
            MealCandidate(
                slot_id=_safe_int(row.get("slot_id"), 0),
                slot_index=_safe_int(row.get("slot_index"), 0),
                title=str(row.get("title") or "Meal").strip() or "Meal",
                time_text=str(row.get("shifted_time_text") or row.get("time_text") or "").strip(),
                status=str(row.get("status") or "open").strip().lower() or "open",
                macros={
                    "kcal": _safe_float((row.get("macros") or {}).get("kcal"), 0.0),
                    "p": _safe_float((row.get("macros") or {}).get("p"), 0.0),
                    "c": _safe_float((row.get("macros") or {}).get("c"), 0.0),
                    "f": _safe_float((row.get("macros") or {}).get("f"), 0.0),
                },
                tags=str((row.get("meal_template") or {}).get("tags") or ""),
                items_count=len(row.get("items") or []),
                servings=_safe_float(row.get("servings"), 1.0) or 1.0,
            )
        )
    out.sort(key=lambda x: (x.slot_index, x.slot_id))
    return out


def _human_reason(reason_codes: list[str], trace: dict[str, Any]) -> str:
    if "EARLY_HOME" in reason_codes:
        return "Meal wurde vorgezogen, weil heute früher Schluss bzw. nutzbare Freistunden vorhanden sind."
    if "LONG_SCHOOL_DAY" in reason_codes:
        return "Meal wurde auf portable Variante umgestellt, weil der Schultag lang ist."
    if "TRAINING_LATE" in reason_codes:
        return "Timing wurde ans späte Training angepasst, damit Pre/Post besser passt."
    if "TRAINING_WINDOW" in reason_codes:
        return "Meal-Timing wurde um das finale Trainingsfenster ausgerichtet."
    if "TRAINING_MACRO_SHIFT" in reason_codes:
        return "Makro-Verteilung wurde rund ums Training leicht verschoben (Pre mehr Carbs, Post mehr Protein)."
    if "MISSED_MEAL" in reason_codes:
        return "Meal wurde auf jetzt gezogen, weil ein vorheriges Meal ausgefallen ist und der Tag sonst hinterherhinkt."
    if "PROTEIN_BEHIND" in reason_codes and "MISSED_MEAL" in reason_codes:
        return "Catch-up aktiv, weil ein Meal ausgefallen ist und Protein deutlich zurückliegt."
    if "WEIGHT_TREND_LOW" in reason_codes:
        return "Tagesziel wurde moderat erhöht, weil der Gewichtstrend unter dem Bulk-Ziel liegt."
    if "LOW_TIME_AVAILABLE" in reason_codes:
        return "Meal wurde vereinfacht, weil heute wenig verfügbare Zeitfenster vorhanden sind."
    if "INVENTORY_LOW" in reason_codes:
        return "Meal wurde wegen fehlender Zutaten markiert und alternative Lösung vorgeschlagen."
    if "PROTEIN_BEHIND" in reason_codes or "KCAL_BEHIND" in reason_codes:
        return "Portion wurde angepasst, weil Makros heute klar hinter dem Ziel liegen."
    return str(trace.get("explain") or "CORE hat eine Kontext-Anpassung vorgenommen.")


def _is_porridge_like(meal: MealCandidate) -> bool:
    hay = f"{meal.title} {meal.tags}".lower()
    return any(tok in hay for tok in ("porridge", "hafer", "oats", "oatmeal"))


def _macro_ratios(macros: dict[str, float]) -> dict[str, float]:
    kcal = _safe_float(macros.get("kcal"), 0.0)
    if kcal <= 1.0:
        return {"p": 0.0, "c": 0.0, "f": 0.0}
    return {
        "p": min(1.0, max(0.0, (_safe_float(macros.get("p"), 0.0) * 4.0) / kcal)),
        "c": min(1.0, max(0.0, (_safe_float(macros.get("c"), 0.0) * 4.0) / kcal)),
        "f": min(1.0, max(0.0, (_safe_float(macros.get("f"), 0.0) * 9.0) / kcal)),
    }


def _pre_meal_score(meal: MealCandidate, *, target_min: int) -> float:
    tmin = _parse_hhmm(meal.time_text) or 0
    dist = abs(target_min - tmin)
    time_score = max(0.0, 1.0 - (dist / 150.0))
    ratios = _macro_ratios(meal.macros or {})
    carb_score = max(0.0, min(1.0, (ratios.get("c", 0.0) - 0.30) / 0.30))
    fat_penalty = max(0.0, min(1.0, (ratios.get("f", 0.0) - 0.35) / 0.35))
    return max(0.0, (0.6 * time_score + 0.4 * carb_score) - (fat_penalty * 0.2))


def _post_meal_score(meal: MealCandidate, *, target_min: int) -> float:
    tmin = _parse_hhmm(meal.time_text) or 0
    dist = abs(target_min - tmin)
    time_score = max(0.0, 1.0 - (dist / 120.0))
    ratios = _macro_ratios(meal.macros or {})
    prot_score = max(0.0, min(1.0, (ratios.get("p", 0.0) - 0.26) / 0.34))
    return max(0.0, 0.6 * time_score + 0.4 * prot_score)


def _pick_best_meal(
    meals: list[MealCandidate],
    *,
    target_min: int,
    window_min: int,
    window_max: int,
    scorer,
) -> MealCandidate | None:
    best = None
    best_score = -1.0
    for meal in meals:
        tmin = _parse_hhmm(meal.time_text)
        if tmin is None:
            continue
        if tmin < window_min - 120 or tmin > window_max + 180:
            continue
        score = scorer(meal, target_min=target_min)
        if window_min <= tmin <= window_max:
            score += 0.15
        if score > best_score:
            best = meal
            best_score = score
    return best


def _policy_yes_strength(signal: dict[str, Any] | None) -> float:
    if not isinstance(signal, dict) or not bool(signal.get("ok")):
        return 0.0
    conf = max(0.0, min(1.0, _safe_float(signal.get("confidence"), 0.0)))
    prob = max(0.0, min(1.0, _safe_float(signal.get("prob_yes"), 0.5)))
    return max(-1.0, min(1.0, (prob - 0.5) * 2.0)) * conf


def _load_policy_signals() -> dict[str, Any]:
    try:
        signals = {
            "calendar_timing_logic": get_core_question_policy_signal(
                topic="calendar_timing_logic",
                query_text="Meal timing mit Freistunden, früher Schluss und Terminfenstern",
            ),
            "telegram_ops_discipline": get_core_question_policy_signal(
                topic="telegram_ops_discipline",
                query_text="Telegram Vorschläge einmalig, dedupe, default verwerfen ohne ok",
            ),
            "macro_scaling_control": get_core_question_policy_signal(
                topic="macro_scaling_control",
                query_text="Mengenanpassung und Portion-Scaling für Catch-up statt neue Meals",
            ),
            "inventory_prep_shopping": get_core_question_policy_signal(
                topic="inventory_prep_shopping",
                query_text="Vorrat, fehlende Zutaten, Prep- und Shopping-Hinweise",
            ),
            "explainability_governance": get_core_question_policy_signal(
                topic="explainability_governance",
                query_text="Reason-Codes, Trace, klare Explainability statt Blackbox",
            ),
            "data_freshness_integrity": get_core_question_policy_signal(
                topic="data_freshness_integrity",
                query_text="Früher Tag ohne vorschnelle Makro-Wertung, Datenfrische und rückwirkende Korrektheit",
            ),
        }
        return {
            "ok": True,
            "signals": signals,
            "profile": get_core_policy_profile(),
        }
    except Exception:
        return {"ok": False, "signals": {}, "profile": {"ok": False}}


def _rule_actions(
    day_iso: str,
    logging_payload: dict[str, Any],
    context: dict[str, Any],
    weight_trend: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    now_min = _parse_hhmm(_now_berlin().strftime("%H:%M")) or 0
    policy_signals = (policy or {}).get("signals") if isinstance(policy, dict) else {}
    calendar_signal = (policy_signals.get("calendar_timing_logic") or {}) if isinstance(policy_signals, dict) else {}
    macro_signal = (policy_signals.get("macro_scaling_control") or {}) if isinstance(policy_signals, dict) else {}
    data_signal = (policy_signals.get("data_freshness_integrity") or {}) if isinstance(policy_signals, dict) else {}
    explain_signal = (policy_signals.get("explainability_governance") or {}) if isinstance(policy_signals, dict) else {}
    cal_strength = _policy_yes_strength(calendar_signal)
    macro_strength = _policy_yes_strength(macro_signal)
    data_strength = _policy_yes_strength(data_signal)
    explain_strength = _policy_yes_strength(explain_signal)

    targets = logging_payload.get("targets") if isinstance(logging_payload.get("targets"), dict) else {}
    mode = str(targets.get("mode") or "maintenance").lower()
    meals = _extract_meal_candidates(logging_payload)
    open_meals = [m for m in meals if m.status in {"open", "planned", "shifted", "adjusted_by_core"}]
    missed_open = [m for m in open_meals if (tm := _parse_hhmm(m.time_text)) is not None and tm + 45 < now_min]
    skipped_or_missed = [
        m for m in meals
        if m.status in {"skipped", "missed"} and (tm := _parse_hhmm(m.time_text)) is not None and tm <= now_min
    ]
    missed_signals = [*missed_open, *skipped_or_missed]
    macro = _macro_totals_from_logging(logging_payload)

    if missed_signals:
        next_open = next((m for m in open_meals if (_parse_hhmm(m.time_text) or 0) >= now_min), None)
        if next_open:
            from_min = _parse_hhmm(next_open.time_text) or now_min
            if from_min > now_min:
                to_txt = _to_hhmm(now_min)
                trace = {
                    "meal_slot_id": next_open.slot_id,
                    "from_time": next_open.time_text,
                    "to_time": to_txt,
                    "missed_slots": sorted({m.slot_id for m in missed_signals}),
                    "now": _to_hhmm(now_min),
                }
                actions.append(
                    {
                        "action_type": "shift_meal",
                        "priority": 93,
                        "status": "proposed",
                        "reason_codes": ["MISSED_MEAL"],
                        "reason_text": _human_reason(["MISSED_MEAL"], trace),
                        "trace": trace,
                        "payload": {"slot_id": next_open.slot_id, "to_time": to_txt, "from_time": next_open.time_text},
                        "human": f"{next_open.title} auf JETZT ({to_txt}) gezogen, weil ein vorheriges Meal ausgefallen ist.",
                    }
                )

    if weight_trend.get("available") and "bulk" in mode and _safe_float(weight_trend.get("trend_kg_per_week"), 0.0) < 0.12:
        trace = {
            "mode": mode,
            "trend_kg_per_week": weight_trend.get("trend_kg_per_week"),
            "threshold": 0.12,
            "delta_kcal": 150,
        }
        actions.append(
            {
                "action_type": "change_target",
                "priority": 95,
                "status": "proposed",
                "reason_codes": ["WEIGHT_TREND_LOW"],
                "reason_text": _human_reason(["WEIGHT_TREND_LOW"], trace),
                "trace": trace,
                "payload": {"delta_kcal": 150, "delta_p": 0.0, "delta_c": 20.0, "delta_f": 3.0},
                "human": "+150 kcal heute, weil Gewichtstrend unter Bulk-Ziel.",
            }
        )

    school = context.get("school") if isinstance(context.get("school"), dict) else {}
    if school.get("early_home") and open_meals and not missed_signals:
        school_end_min = _parse_hhmm(school.get("school_end"))
        candidate = None
        min_gap = 110 if cal_strength >= 0 else 130
        for m in open_meals:
            meal_min = _parse_hhmm(m.time_text)
            if meal_min is None:
                continue
            if school_end_min is not None and meal_min - school_end_min >= min_gap:
                candidate = m
                break
        if candidate:
            from_min = _parse_hhmm(candidate.time_text) or 0
            shift_delta = 75 if cal_strength >= -0.15 else 55
            to_min = max((school_end_min or (from_min - shift_delta)) + 35, from_min - shift_delta, now_min)
            to_txt = _to_hhmm(to_min)
            trace = {
                "meal_slot_id": candidate.slot_id,
                "from_time": candidate.time_text,
                "to_time": to_txt,
                "school_end": school.get("school_end"),
                "largest_free_window_min": school.get("largest_free_window_min"),
            }
            if to_min < from_min:
                actions.append(
                    {
                        "action_type": "shift_meal",
                        "priority": 90,
                        "status": "proposed",
                        "reason_codes": ["EARLY_HOME"],
                        "reason_text": _human_reason(["EARLY_HOME"], trace),
                        "trace": trace,
                        "payload": {"slot_id": candidate.slot_id, "to_time": to_txt, "from_time": candidate.time_text},
                        "human": f"{candidate.title} um {candidate.time_text or '—'} -> {to_txt} verschoben (früher zuhause).",
                    }
                )

    if school.get("long_school_day") and open_meals:
        candidate = next((m for m in open_meals if "portable" not in m.tags.lower() and m.items_count >= 4), None)
        if candidate:
            trace = {
                "meal_slot_id": candidate.slot_id,
                "long_school_day": True,
                "items_count": candidate.items_count,
                "tags": candidate.tags,
            }
            actions.append(
                {
                    "action_type": "replace_meal",
                    "priority": 75,
                    "status": "proposed",
                    "reason_codes": ["LONG_SCHOOL_DAY"],
                    "reason_text": _human_reason(["LONG_SCHOOL_DAY"], trace),
                    "trace": trace,
                    "payload": {
                        "slot_id": candidate.slot_id,
                        "replacement_title": f"{candidate.title} (portable)",
                        "replacement_tags": "portable,quick",
                    },
                    "human": f"{candidate.title} als portable Variante markiert (langer Schultag).",
                }
            )

    training = context.get("training") if isinstance(context.get("training"), dict) else {}
    window = training.get("training_window") if isinstance(training.get("training_window"), dict) else None
    if not window and isinstance(training.get("schedule"), dict):
        window = (training.get("schedule") or {}).get("window") if isinstance((training.get("schedule") or {}).get("window"), dict) else None
    t_start = _parse_hhmm((window or {}).get("training_start") or training.get("training_time"))
    t_end = _parse_hhmm((window or {}).get("training_end"))
    if t_start is not None and t_end is None:
        t_end = t_start + 75

    if training.get("training_today") and t_start is not None and open_meals:
        pre_target = t_start - 180
        pre_window_min = t_start - 240
        pre_window_max = t_start - 120
        post_target = (t_end or t_start) + 60
        post_window_min = (t_end or t_start)
        post_window_max = (t_end or t_start) + 120

        future_meals = [m for m in open_meals if (_parse_hhmm(m.time_text) or 0) >= now_min]
        pre_meal = _pick_best_meal(
            future_meals,
            target_min=pre_target,
            window_min=pre_window_min,
            window_max=pre_window_max,
            scorer=_pre_meal_score,
        )
        post_candidates = [m for m in future_meals if not pre_meal or m.slot_id != pre_meal.slot_id]
        post_meal = _pick_best_meal(
            post_candidates,
            target_min=post_target,
            window_min=post_window_min,
            window_max=post_window_max,
            scorer=_post_meal_score,
        )

        def _shift_if_needed(meal: MealCandidate | None, *, target: int, wmin: int, wmax: int, label: str):
            if not meal:
                return
            meal_min = _parse_hhmm(meal.time_text)
            if meal_min is None:
                return
            if wmin <= meal_min <= wmax:
                return
            to_min = max(now_min + 15, min(max(wmin, target), wmax))
            if abs(to_min - meal_min) < 20:
                return
            if abs(to_min - meal_min) > 190:
                return
            to_txt = _to_hhmm(to_min)
            trace = {
                "meal_slot_id": meal.slot_id,
                "from_time": meal.time_text,
                "to_time": to_txt,
                "training_start": _to_hhmm(t_start),
                "training_end": _to_hhmm(t_end or t_start),
                "training_type": training.get("training_type"),
                "target_window": label,
            }
            actions.append(
                {
                    "action_type": "shift_meal",
                    "priority": 84 if label == "pre" else 83,
                    "status": "applied",
                    "reason_codes": ["TRAINING_WINDOW"],
                    "reason_text": _human_reason(["TRAINING_WINDOW"], trace),
                    "trace": trace,
                    "payload": {"slot_id": meal.slot_id, "to_time": to_txt, "from_time": meal.time_text},
                    "human": f"{meal.title} auf {to_txt} gelegt (Training-Fenster {label}).",
                }
            )

        _shift_if_needed(pre_meal, target=pre_target, wmin=pre_window_min, wmax=pre_window_max, label="pre")
        _shift_if_needed(post_meal, target=post_target, wmin=post_window_min, wmax=post_window_max, label="post")

        if pre_meal and macro.get("remaining_c", 0.0) >= 30.0 and macro.get("remaining_kcal", 0.0) >= 220.0:
            ratios = _macro_ratios(pre_meal.macros or {})
            if ratios.get("c", 0.0) >= 0.38:
                base = max(0.1, _safe_float(pre_meal.servings, 1.0))
                factor = 1.08
                to_serv = round(base * factor, 2)
                trace = {
                    "meal_slot_id": pre_meal.slot_id,
                    "from_servings": base,
                    "to_servings": to_serv,
                    "training_start": _to_hhmm(t_start),
                    "macro_shift": "pre_carb",
                }
                actions.append(
                    {
                        "action_type": "adjust_servings",
                        "priority": 78,
                        "status": "applied",
                        "reason_codes": ["TRAINING_MACRO_SHIFT"],
                        "reason_text": _human_reason(["TRAINING_MACRO_SHIFT"], trace),
                        "trace": trace,
                        "payload": {
                            "slot_id": pre_meal.slot_id,
                            "from_servings": base,
                            "to_servings": to_serv,
                            "factor": round(to_serv / base, 3) if base else 1.0,
                        },
                        "human": f"{pre_meal.title}: Pre-Workout-Portion leicht hoch (Carb-Fokus).",
                    }
                )

        if post_meal and macro.get("remaining_p", 0.0) >= 25.0 and macro.get("remaining_kcal", 0.0) >= 220.0:
            ratios = _macro_ratios(post_meal.macros or {})
            if ratios.get("p", 0.0) >= 0.30:
                base = max(0.1, _safe_float(post_meal.servings, 1.0))
                factor = 1.08
                to_serv = round(base * factor, 2)
                trace = {
                    "meal_slot_id": post_meal.slot_id,
                    "from_servings": base,
                    "to_servings": to_serv,
                    "training_end": _to_hhmm(t_end or t_start),
                    "macro_shift": "post_protein",
                }
                actions.append(
                    {
                        "action_type": "adjust_servings",
                        "priority": 77,
                        "status": "applied",
                        "reason_codes": ["TRAINING_MACRO_SHIFT"],
                        "reason_text": _human_reason(["TRAINING_MACRO_SHIFT"], trace),
                        "trace": trace,
                        "payload": {
                            "slot_id": post_meal.slot_id,
                            "from_servings": base,
                            "to_servings": to_serv,
                            "factor": round(to_serv / base, 3) if base else 1.0,
                        },
                        "human": f"{post_meal.title}: Post-Workout-Portion leicht hoch (Protein-Fokus).",
                    }
                )

    if missed_signals and macro.get("remaining_p", 0.0) >= 75.0 and len(open_meals) <= 2:
        trace = {
            "missed_slots": sorted({m.slot_id for m in missed_signals}),
            "remaining_p": macro.get("remaining_p"),
            "open_meals": len(open_meals),
        }
        actions.append(
            {
                "action_type": "add_catchup_meal",
                "priority": 92,
                "status": "proposed",
                "reason_codes": ["MISSED_MEAL", "PROTEIN_BEHIND"],
                "reason_text": _human_reason(["MISSED_MEAL", "PROTEIN_BEHIND"], trace),
                "trace": trace,
                "payload": {
                    "title": "Catch-up Protein",
                    "time": _to_hhmm(max(now_min + 25, 17 * 60)),
                    "macros": {"kcal": 320, "p": 38, "c": 22, "f": 7},
                },
                "human": "Catch-up Meal ergänzt, weil Meal verpasst und Protein-Rest hoch.",
            }
        )

    early_guard_until = 13 * 60 if data_strength <= 0.35 else 14 * 60
    allow_macro_reaction = bool(now_min >= early_guard_until or missed_signals)
    p_threshold = max(28.0, 35.0 - max(0.0, macro_strength) * 9.0)
    kcal_threshold = max(220.0, 300.0 - max(0.0, macro_strength) * 80.0)
    if allow_macro_reaction and open_meals and (macro.get("remaining_p", 0.0) >= p_threshold or macro.get("remaining_kcal", 0.0) >= kcal_threshold):
        future_open = [m for m in open_meals if (_parse_hhmm(m.time_text) or 0) >= now_min]
        pool = future_open or open_meals
        porridge_candidate = next((m for m in pool if _is_porridge_like(m)), None)
        candidate = porridge_candidate or pool[0]
        base_servings = max(0.1, _safe_float(candidate.servings, 1.0))
        p_gap = max(0.0, _safe_float(macro.get("remaining_p"), 0.0))
        kcal_gap = max(0.0, _safe_float(macro.get("remaining_kcal"), 0.0))
        delta_from_p = min(0.25 + max(0.0, macro_strength) * 0.15, p_gap / 240.0)
        delta_from_kcal = min(0.2 + max(0.0, macro_strength) * 0.12, kcal_gap / 2200.0)
        factor = 1.0 + max(0.1, delta_from_p, delta_from_kcal)
        factor_cap = 1.3 + max(0.0, macro_strength) * 0.15
        factor = min(factor_cap, max(0.8, factor))
        to_servings = round(base_servings * factor, 2)
        real_factor = round(to_servings / base_servings, 3) if base_servings > 0 else 1.0
        auto_apply = bool(_is_porridge_like(candidate))
        trace = {
            "meal_slot_id": candidate.slot_id,
            "meal_title": candidate.title,
            "from_servings": base_servings,
            "to_servings": to_servings,
            "factor": real_factor,
            "remaining_p": p_gap,
            "remaining_kcal": kcal_gap,
            "auto_applied": auto_apply,
            "policy": "telegram_confirm_required_except_porridge",
            "policy_profile": {
                "macro_scaling_control": {
                    "answer": macro_signal.get("answer"),
                    "confidence": macro_signal.get("confidence"),
                },
                "data_freshness_integrity": {
                    "answer": data_signal.get("answer"),
                    "confidence": data_signal.get("confidence"),
                },
            },
        }
        reason_codes = []
        if p_gap >= 35.0:
            reason_codes.append("PROTEIN_BEHIND")
        if kcal_gap >= 300.0:
            reason_codes.append("KCAL_BEHIND")
        actions.append(
            {
                "action_type": "adjust_servings",
                "priority": 89,
                "status": "applied" if auto_apply else "proposed",
                "reason_codes": reason_codes or ["KCAL_BEHIND"],
                "reason_text": _human_reason(reason_codes or ["KCAL_BEHIND"], trace),
                "trace": trace,
                "payload": {
                    "slot_id": candidate.slot_id,
                    "from_servings": base_servings,
                    "to_servings": to_servings,
                    "factor": real_factor,
                },
                "human": (
                    f"{candidate.title}: Menge {base_servings:.2f}x -> {to_servings:.2f}x angepasst."
                    if auto_apply
                    else f"Vorschlag: {candidate.title} Menge {base_servings:.2f}x -> {to_servings:.2f}x (Telegram-Bestätigung offen)."
                ),
            }
        )

    # Placeholder for inventory integration (architecture hook)
    conn = get_nutrition_db()
    try:
        inv = conn.execute(
            "SELECT ingredient_name, status, note FROM nutrition_inventory_flags WHERE date_iso=? ORDER BY id DESC LIMIT 2",
            (day_iso,),
        ).fetchall()
    except Exception:
        inv = []
    finally:
        conn.close()

    if inv:
        note = ", ".join(str(r[0]) for r in inv if r and r[0])
        trace = {"count": len(inv), "items": [dict(r) for r in inv]}
        actions.append(
            {
                "action_type": "mark_inventory_issue",
                "priority": 88,
                "status": "proposed",
                "reason_codes": ["INVENTORY_LOW"],
                "reason_text": _human_reason(["INVENTORY_LOW"], trace),
                "trace": trace,
                "payload": {"note": note},
                "human": f"Vorratshinweis aktiv ({note or 'fehlende Zutaten'}).",
            }
        )

    if explain_strength >= 0.3:
        for action in actions:
            txt = str(action.get("reason_text") or "").strip()
            if txt:
                continue
            reasons = [str(x) for x in (action.get("reason_codes") or [])]
            action["reason_text"] = _human_reason(reasons, action.get("trace") or {})

    actions.sort(key=lambda a: (-_safe_int(a.get("priority"), 0), str(a.get("action_type") or "")))
    return actions


def _action_key(day_iso: str, action: dict[str, Any]) -> str:
    base = {
        "day": day_iso,
        "type": action.get("action_type"),
        "payload": action.get("payload") or {},
        "reasons": action.get("reason_codes") or [],
    }
    txt = json.dumps(base, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(txt.encode("utf-8")).hexdigest()[:20]


def _parse_iso_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    return None


def _fmt_local_time(raw_ts: Any) -> str:
    dt = _parse_iso_ts(raw_ts)
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    return dt.astimezone(BERLIN).strftime("%H:%M")


def _fmt_local_datetime(raw_ts: Any) -> str:
    dt = _parse_iso_ts(raw_ts)
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    local = dt.astimezone(BERLIN)
    return local.strftime("%d.%m · %H:%M")


def _status_to_level(status: str) -> str:
    s = str(status or "").strip().lower()
    if s in {"applied", "telegram_confirmed"}:
        return "angewendet"
    if s in {"rejected"}:
        return "verworfen"
    if s in {"accepted", "proposed"}:
        return "vorgeschlagen"
    return "beobachtet"


def _action_label(action_type: str) -> str:
    key = str(action_type or "").strip().lower()
    mapping = {
        "shift_meal": "Timing verschoben",
        "replace_meal": "Meal-Form gewechselt",
        "adjust_servings": "Portion skaliert",
        "change_target": "Tagesziel angehoben",
        "add_catchup_meal": "Catch-up ergänzt",
        "mark_inventory_issue": "Bestandsrisiko markiert",
    }
    return mapping.get(key, "Anpassung vorbereitet")


def _compute_day_logic(
    now_min: int,
    logging_payload: dict[str, Any],
    context: dict[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, str]:
    remaining = (logging_payload.get("remaining") or {}) if isinstance(logging_payload, dict) else {}
    p_gap = _safe_float(remaining.get("p"), 0.0)
    kcal_gap = _safe_float(remaining.get("kcal"), 0.0)
    training = context.get("training") if isinstance(context.get("training"), dict) else {}
    school = context.get("school") if isinstance(context.get("school"), dict) else {}
    has_training = bool(training.get("training_today"))
    training_time = str(training.get("training_time") or "").strip()
    early_day = now_min < 13 * 60
    has_timing_move = any(str(a.get("action_type") or "") == "shift_meal" for a in actions)
    has_scaling = any(str(a.get("action_type") or "") == "adjust_servings" for a in actions)
    if early_day and (kcal_gap > 0 or p_gap > 0):
        return {
            "mode": "basis_halten",
            "headline": "Heute hält CORE die Ernährung bewusst ruhig, weil der Tag noch früh ist und die Lücke noch nicht belastbar ist.",
            "priority": "Stabil versorgen statt früh überreagieren",
        }
    if school.get("early_home") and has_timing_move:
        return {
            "mode": "timing_vorziehen",
            "headline": "Heute priorisiert CORE eine frühe, leichte Vorverlagerung statt späteres Aufholen unter Zeitdruck.",
            "priority": "Frühes Zeitfenster sauber nutzen",
        }
    if has_training and training_time:
        return {
            "mode": "performance_stuetzen",
            "headline": f"Heute priorisiert CORE Timing-Sauberkeit rund um das Training um {training_time}, bevor Mengen aggressiv angehoben werden.",
            "priority": "Performance sichern vor blindem Catch-up",
        }
    if has_scaling and (p_gap >= 35.0 or kcal_gap >= 300.0):
        return {
            "mode": "moderater_catch_up",
            "headline": "Heute priorisiert CORE moderate Skalierung über bestehende Meals statt eine große Zusatzmahlzeit.",
            "priority": "Lücken schließen ohne Verdichtung",
        }
    return {
        "mode": "reibung_minimieren",
        "headline": "Heute priorisiert CORE eine reibungsarme Versorgung mit kleinen, rücknehmbaren Schritten.",
        "priority": "Reibung minimieren",
    }


def _build_guardrails(
    now_min: int,
    logging_payload: dict[str, Any],
    context: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    remaining = (logging_payload.get("remaining") or {}) if isinstance(logging_payload, dict) else {}
    school = context.get("school") if isinstance(context.get("school"), dict) else {}
    macro_kcal = _safe_float(remaining.get("kcal"), 0.0)
    macro_p = _safe_float(remaining.get("p"), 0.0)
    action_types = {str(a.get("action_type") or "") for a in actions}
    out = [
        {
            "code": "EARLY_DAY_CONSERVATIVE",
            "active": bool(now_min < 13 * 60),
            "effect": "Kein aggressives Early-Day-Catch-up",
            "why": "Der Tag ist noch jung; frühe Makrolücken sind noch nicht stabil genug.",
        },
        {
            "code": "NO_MEGA_CATCHUP",
            "active": bool("add_catchup_meal" not in action_types and (macro_p > 70 or macro_kcal > 1000)),
            "effect": "Keine große Zusatzmahlzeit erzeugt",
            "why": "CORE verteilt Aufholen zuerst auf bestehende Meals.",
        },
        {
            "code": "UNSTABLE_CALENDAR_GUARD",
            "active": bool(not school.get("available")),
            "effect": "Kein harter Kalendershift auf unsicherer Datenlage",
            "why": "Heute ist der Schultag nicht stabil genug aus den Quellen ableitbar.",
        },
    ]
    return out


def _build_learning_signals(policy: dict[str, Any]) -> list[dict[str, Any]]:
    signals = (policy or {}).get("signals") if isinstance(policy, dict) else {}
    if not isinstance(signals, dict):
        return []
    out: list[dict[str, Any]] = []
    entries = [
        ("macro_scaling_control", "Du bevorzugst in ähnlichen Situationen eher moderate Skalierung als harte Catch-up-Moves."),
        ("calendar_timing_logic", "Bei Kalenderfenstern bevorzugst du meist frühe, saubere Verschiebungen statt spätes Komprimieren."),
        ("data_freshness_integrity", "Bei unsicherer Tageslage soll CORE erst klein entscheiden und später nachschärfen."),
        ("telegram_ops_discipline", "Größere Schritte sollen als klarer Vorschlag laufen, kleine reversible Dinge direkt."),
    ]
    for key, sentence in entries:
        raw = signals.get(key) if isinstance(signals, dict) else None
        strength = _policy_yes_strength(raw if isinstance(raw, dict) else {})
        if abs(strength) < 0.15:
            continue
        direction = "stützt" if strength > 0 else "bremst"
        out.append(
            {
                "topic": key,
                "direction": direction,
                "strength": round(strength, 3),
                "text": sentence if strength > 0 else f"{sentence} Diese Tendenz war heute bewusst abgeschwächt.",
            }
        )
    return out


def _compute_data_quality(now_min: int, context: dict[str, Any], logging_payload: dict[str, Any]) -> dict[str, Any]:
    school = context.get("school") if isinstance(context.get("school"), dict) else {}
    cal = context.get("calendar") if isinstance(context.get("calendar"), dict) else {}
    planned_meals = logging_payload.get("planned_meals") if isinstance(logging_payload, dict) else []
    logged_meals = logging_payload.get("logged_meals") if isinstance(logging_payload, dict) else []
    planned_n = len(planned_meals) if isinstance(planned_meals, list) else 0
    logged_n = len(logged_meals) if isinstance(logged_meals, list) else 0

    score = 0.48
    notes: list[str] = []
    if school.get("available"):
        score += 0.16
        notes.append("Schulkalender ist vorhanden")
    else:
        notes.append("Schulkalender fehlt")
    if cal.get("available"):
        score += 0.12
        notes.append("Google-Kalender ist lesbar")
    else:
        notes.append("Google-Kalender fehlt")
    if planned_n > 0:
        score += 0.12
        notes.append("Meal-Slots sind geplant")
    if logged_n > 0:
        score += 0.08
        notes.append("Tageslogging hat frische Einträge")
    if now_min < 11 * 60:
        score -= 0.12
        notes.append("Frühes Tagesfenster: eingeschränkte Aussagekraft")
    if now_min > 17 * 60:
        score += 0.06

    score = max(0.05, min(0.97, score))
    band = "hoch" if score >= 0.78 else ("mittel" if score >= 0.58 else "niedrig")
    explanation = (
        "Datenlage ist stabil genug für klare operative Schritte."
        if band == "hoch"
        else ("Datenlage ist brauchbar, daher moderater Entscheidungsstil." if band == "mittel" else "Datenlage ist noch unsicher, daher konservative Schritte.")
    )
    return {"score": round(score, 3), "band": band, "explanation": explanation, "signals": notes}


def _decide_live_status(manual_override: bool, actions: list[dict[str, Any]], is_recompute: bool, active_guardrails: list[dict[str, Any]]) -> tuple[str, str]:
    if manual_override:
        return "manual_override", DECISION_STATUS_LABELS["manual_override"]
    if any(str(a.get("status") or "") in {"applied", "telegram_confirmed"} for a in actions):
        return "applied", DECISION_STATUS_LABELS["applied"]
    if any(str(a.get("status") or "") in {"proposed", "accepted"} for a in actions):
        return "proposed", DECISION_STATUS_LABELS["proposed"]
    if active_guardrails:
        return "guarded", DECISION_STATUS_LABELS["guarded"]
    if is_recompute:
        return "recomputed", DECISION_STATUS_LABELS["recomputed"]
    return "monitoring", DECISION_STATUS_LABELS["monitoring"]


def _delta_modules(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in actions:
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        kind = str(action.get("action_type") or "")
        level = _status_to_level(str(action.get("status") or ""))
        if kind == "shift_meal":
            out.append(
                {
                    "axis": "Timing",
                    "label": _action_label(kind),
                    "before": str(payload.get("from_time") or "—"),
                    "after": str(payload.get("to_time") or "—"),
                    "level": level,
                }
            )
        elif kind == "adjust_servings":
            out.append(
                {
                    "axis": "Menge",
                    "label": _action_label(kind),
                    "before": f"{_safe_float(payload.get('from_servings'), 1.0):.2f}x",
                    "after": f"{_safe_float(payload.get('to_servings'), 1.0):.2f}x",
                    "level": level,
                }
            )
        elif kind == "replace_meal":
            out.append(
                {
                    "axis": "Form",
                    "label": _action_label(kind),
                    "before": str(payload.get("slot_title") or "Standard"),
                    "after": str(payload.get("replacement_title") or "Portable Variante"),
                    "level": level,
                }
            )
        elif kind == "change_target":
            out.append(
                {
                    "axis": "Ziel",
                    "label": _action_label(kind),
                    "before": "Basisziel",
                    "after": f"+{round(_safe_float(payload.get('delta_kcal'), 0.0))} kcal",
                    "level": level,
                }
            )
    return out[:5]


def _build_decision_narrative(
    *,
    day_iso: str,
    now_iso: str,
    day_logic: dict[str, str],
    context: dict[str, Any],
    logging_payload: dict[str, Any],
    actions: list[dict[str, Any]],
    guardrails: list[dict[str, Any]],
    learning_signals: list[dict[str, Any]],
    confidence: dict[str, Any],
    manual_override: bool,
    previous_active: dict[str, Any] | None,
    is_recompute: bool,
) -> dict[str, Any]:
    active_guardrails = [g for g in guardrails if g.get("active")]
    live_code, live_label = _decide_live_status(manual_override, actions, is_recompute, active_guardrails)
    training = context.get("training") if isinstance(context.get("training"), dict) else {}
    school = context.get("school") if isinstance(context.get("school"), dict) else {}
    remaining = (logging_payload.get("remaining") or {}) if isinstance(logging_payload, dict) else {}
    open_meals = [m for m in (logging_payload.get("planned_meals") or []) if str((m or {}).get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}]
    known_facts = {
        "left": {
            "Zeitfenster": _fmt_local_time(now_iso),
            "Offene Meals": str(len(open_meals)),
            "Offene kcal": str(int(round(_safe_float(remaining.get("kcal"), 0.0)))),
            "Offenes Protein": f"{int(round(_safe_float(remaining.get('p'), 0.0)))} g",
        },
        "right": {
            "Training": (f"{training.get('training_type') or 'ja'} um {training.get('training_time')}" if training.get("training_today") else "Kein Training erkannt"),
            "Schule": (f"Bis {school.get('school_end')}" if school.get("school_end") else "Kein stabiler Schultag"),
            "Freistundenfenster": (f"{int(school.get('largest_free_window_min') or 0)} min" if school.get("largest_free_window_min") else "Kein klares Fenster"),
            "Datenlage": confidence.get("band") or "mittel",
        },
    }

    chosen = actions[0] if actions else None
    chosen_desc = "Heute wurde keine operative Stellschraube ausgelöst; CORE beobachtet weiter."
    if chosen:
        chosen_desc = str(chosen.get("reason_text") or chosen.get("human") or "").strip() or "Operative Anpassung vorbereitet."
    path_text = chosen_desc
    if active_guardrails:
        first = active_guardrails[0]
        path_text = f"{path_text} Vorher griff die Leitplanke „{first.get('effect')}“."
    if learning_signals:
        path_text = f"{path_text} Verhaltenstendenz: {learning_signals[0].get('text')}"

    rejected = []
    for action in actions[1:]:
        rejected.append(
            {
                "label": _action_label(str(action.get("action_type") or "")),
                "reason": str(action.get("reason_text") or "Wurde nicht priorisiert.").strip(),
            }
        )
    if not any(r.get("label") == "Aggressives Catch-up" for r in rejected):
        rejected.append(
            {
                "label": "Aggressives Catch-up",
                "reason": "Wurde heute bewusst nicht gewählt, um Reibung und Verdichtung zu vermeiden.",
            }
        )

    current_truth = {
        "state": "active",
        "label": "Aktueller Stand ist aktiv",
        "text": "Diese Entscheidung ist derzeit die führende Wahrheit für heute.",
    }
    if manual_override:
        current_truth = {
            "state": "manual_override",
            "label": "Nutzer-Eingriff hat Vorrang",
            "text": "Seit dem manuellen Eingriff ist die letzte CORE-Entscheidung nur noch Historie.",
        }
    elif is_recompute:
        current_truth = {
            "state": "recomputed",
            "label": "Neu berechnet",
            "text": "Neue Daten haben die vorherige Entscheidungsstufe ersetzt.",
        }
    elif previous_active and str(previous_active.get("decision_id") or "").strip():
        current_truth = {
            "state": "supersedes_previous",
            "label": "Frühere Stufe ersetzt",
            "text": "Eine ältere CORE-Stufe bleibt sichtbar, gilt aber nicht mehr als aktive Wahrheit.",
        }

    return {
        "header": {
            "domain_label": "CORE NUTRITION",
            "live_status_code": live_code,
            "live_status_label": live_label,
            "decision_at": _fmt_local_datetime(now_iso),
            "truth_badge": "Aktuelle Wahrheit" if current_truth["state"] == "active" else "Letzte Aktion (nicht führend)",
        },
        "day_logic": day_logic,
        "baseline": known_facts,
        "decision_path": path_text,
        "delta_modules": _delta_modules(actions),
        "current_truth": current_truth,
        "meaning": (
            "Die heutige Steuerung bleibt praktisch umsetzbar, vermeidet unnötige Härte und hält trotzdem die Tagesziele im Blick."
            if live_code in {"applied", "proposed"}
            else "CORE hält den Tag stabil und vermeidet Aktionismus, bis die Lage klarer ist."
        ),
        "rejected_alternatives": rejected[:4],
        "confidence": confidence,
        "guardrails": guardrails,
        "learning_signals": learning_signals,
    }


def ensure_core_nutrition_schema() -> None:
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS core_nutrition_day_state (
                date_iso TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'PLAN',
                target_kcal REAL NULL,
                target_p REAL NULL,
                target_c REAL NULL,
                target_f REAL NULL,
                base_target_kcal REAL NULL,
                base_target_p REAL NULL,
                base_target_c REAL NULL,
                base_target_f REAL NULL,
                planned_kcal REAL NULL,
                planned_p REAL NULL,
                planned_c REAL NULL,
                planned_f REAL NULL,
                logged_kcal REAL NULL,
                logged_p REAL NULL,
                logged_c REAL NULL,
                logged_f REAL NULL,
                remaining_kcal REAL NULL,
                remaining_p REAL NULL,
                remaining_c REAL NULL,
                remaining_f REAL NULL,
                open_meals INTEGER NOT NULL DEFAULT 0,
                last_logged_meal_id INTEGER NULL,
                next_open_slot_id INTEGER NULL,
                drift_flags_json TEXT NULL,
                context_json TEXT NULL,
                explain_json TEXT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS core_nutrition_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_iso TEXT NOT NULL,
                action_key TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                priority INTEGER NOT NULL DEFAULT 0,
                meal_slot_id INTEGER NULL,
                meal_slot_index INTEGER NULL,
                from_time TEXT NULL,
                to_time TEXT NULL,
                target_delta_kcal REAL NULL,
                target_delta_p REAL NULL,
                target_delta_c REAL NULL,
                target_delta_f REAL NULL,
                reason_codes_json TEXT NOT NULL,
                reason_text TEXT NULL,
                human_text TEXT NULL,
                payload_json TEXT NULL,
                trace_json TEXT NULL,
                source TEXT NOT NULL DEFAULT 'core',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(date_iso, action_key)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_nutrition_actions_date ON core_nutrition_actions(date_iso, created_at DESC)")
        cols_actions = {r[1] for r in cur.execute("PRAGMA table_info(core_nutrition_actions)").fetchall()}
        if "decision_id" not in cols_actions:
            cur.execute("ALTER TABLE core_nutrition_actions ADD COLUMN decision_id TEXT NULL")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS core_nutrition_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                date_iso TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_recomputed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                priority_mode TEXT NULL,
                day_logic TEXT NULL,
                context_snapshot_json TEXT NULL,
                hard_guards_checked_json TEXT NULL,
                learned_policy_signals_json TEXT NULL,
                considered_actions_json TEXT NULL,
                chosen_action_json TEXT NULL,
                chosen_action_reasoning TEXT NULL,
                rejected_actions_json TEXT NULL,
                superseded_by_decision_id TEXT NULL,
                supersedes_decision_id TEXT NULL,
                user_override_state TEXT NULL,
                confidence_json TEXT NULL,
                current_truth_state TEXT NOT NULL DEFAULT 'active',
                narrative_json TEXT NULL,
                signature TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_nutrition_decisions_day ON core_nutrition_decisions(date_iso, created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_nutrition_decisions_truth ON core_nutrition_decisions(date_iso, current_truth_state)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nutrition_inventory_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_iso TEXT NOT NULL,
                ingredient_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'low',
                note TEXT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_inventory_flags_date ON nutrition_inventory_flags(date_iso)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS core_nutrition_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source TEXT NOT NULL DEFAULT 'derived',
                updated_at TEXT NOT NULL,
                UNIQUE(memory_key)
            )
            """
        )

        # Add explainability metadata columns to existing status table when missing.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(nutrition_planned_meal_status)").fetchall()}
        if "status_source" not in cols:
            cur.execute("ALTER TABLE nutrition_planned_meal_status ADD COLUMN status_source TEXT NOT NULL DEFAULT 'user'")
        if "reason_code" not in cols:
            cur.execute("ALTER TABLE nutrition_planned_meal_status ADD COLUMN reason_code TEXT NULL")
        if "trace_json" not in cols:
            cur.execute("ALTER TABLE nutrition_planned_meal_status ADD COLUMN trace_json TEXT NULL")

        cols2 = {r[1] for r in cur.execute("PRAGMA table_info(nutrition_logged_meals)").fetchall()}
        if "adjusted_from_meal_id" not in cols2:
            cur.execute("ALTER TABLE nutrition_logged_meals ADD COLUMN adjusted_from_meal_id INTEGER NULL")
        if "adjustment_reason" not in cols2:
            cur.execute("ALTER TABLE nutrition_logged_meals ADD COLUMN adjustment_reason TEXT NULL")
        if "action_source" not in cols2:
            cur.execute("ALTER TABLE nutrition_logged_meals ADD COLUMN action_source TEXT NULL")
        if "trace_json" not in cols2:
            cur.execute("ALTER TABLE nutrition_logged_meals ADD COLUMN trace_json TEXT NULL")

        conn.commit()
    finally:
        conn.close()


def _load_latest_decision_for_day(day_iso: str) -> Optional[dict[str, Any]]:
    conn = get_nutrition_db()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM core_nutrition_decisions
            WHERE date_iso=?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in (
            "context_snapshot_json",
            "hard_guards_checked_json",
            "learned_policy_signals_json",
            "considered_actions_json",
            "chosen_action_json",
            "rejected_actions_json",
            "confidence_json",
            "narrative_json",
        ):
            raw = data.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    data[key[:-5]] = json.loads(raw)
                except Exception:
                    data[key[:-5]] = {}
            else:
                data[key[:-5]] = {}
        return data
    except Exception:
        return None
    finally:
        conn.close()


def _load_decision_history_for_day(day_iso: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = get_nutrition_db()
    try:
        rows = conn.execute(
            """
            SELECT decision_id, created_at, status, current_truth_state, supersedes_decision_id, superseded_by_decision_id, user_override_state
            FROM core_nutrition_decisions
            WHERE date_iso=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (day_iso, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _persist_decision_state(day_iso: str, decision: dict[str, Any], signature: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        now = _utcnow_iso()
        prev = cur.execute(
            """
            SELECT decision_id, signature, created_at, current_truth_state
            FROM core_nutrition_decisions
            WHERE date_iso=?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        prev_id = str(prev[0]) if prev else None
        prev_signature = str(prev[1]) if prev else None
        decision_id = str(decision.get("decision_id") or f"cn-{uuid4().hex[:14]}")

        if prev and prev_signature == signature:
            decision_id = prev_id or decision_id
            cur.execute(
                """
                UPDATE core_nutrition_decisions
                SET
                    last_recomputed_at=?,
                    status=?,
                    priority_mode=?,
                    day_logic=?,
                    context_snapshot_json=?,
                    hard_guards_checked_json=?,
                    learned_policy_signals_json=?,
                    considered_actions_json=?,
                    chosen_action_json=?,
                    chosen_action_reasoning=?,
                    rejected_actions_json=?,
                    user_override_state=?,
                    confidence_json=?,
                    current_truth_state=?,
                    narrative_json=?
                WHERE decision_id=?
                """,
                (
                    now,
                    str(decision.get("status") or "monitoring"),
                    str(decision.get("priority_mode") or ""),
                    str(decision.get("day_logic") or ""),
                    json.dumps(decision.get("context_snapshot") or {}, ensure_ascii=False),
                    json.dumps(decision.get("hard_guards_checked") or [], ensure_ascii=False),
                    json.dumps(decision.get("learned_policy_signals") or [], ensure_ascii=False),
                    json.dumps(decision.get("considered_actions") or [], ensure_ascii=False),
                    json.dumps(decision.get("chosen_action") or {}, ensure_ascii=False),
                    str(decision.get("chosen_action_reasoning") or ""),
                    json.dumps(decision.get("rejected_actions") or [], ensure_ascii=False),
                    str(decision.get("user_override_state") or "none"),
                    json.dumps(decision.get("confidence") or {}, ensure_ascii=False),
                    str(decision.get("current_truth_state") or "active"),
                    json.dumps(decision.get("narrative") or {}, ensure_ascii=False),
                    decision_id,
                ),
            )
        else:
            # New signature means a new decision stage; never reuse the previous decision_id.
            if not decision_id or (prev_id and decision_id == prev_id):
                decision_id = f"cn-{uuid4().hex[:14]}"
            supersedes = prev_id if prev_id else None
            if supersedes:
                cur.execute(
                    """
                    UPDATE core_nutrition_decisions
                    SET current_truth_state='superseded', superseded_by_decision_id=?
                    WHERE decision_id=?
                    """,
                    (decision_id, supersedes),
                )
            cur.execute(
                """
                INSERT INTO core_nutrition_decisions (
                    decision_id, domain, date_iso, created_at, last_recomputed_at, status, priority_mode, day_logic,
                    context_snapshot_json, hard_guards_checked_json, learned_policy_signals_json, considered_actions_json,
                    chosen_action_json, chosen_action_reasoning, rejected_actions_json, superseded_by_decision_id,
                    supersedes_decision_id, user_override_state, confidence_json, current_truth_state, narrative_json, signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    DECISION_DOMAIN_NUTRITION,
                    day_iso,
                    now,
                    now,
                    str(decision.get("status") or "monitoring"),
                    str(decision.get("priority_mode") or ""),
                    str(decision.get("day_logic") or ""),
                    json.dumps(decision.get("context_snapshot") or {}, ensure_ascii=False),
                    json.dumps(decision.get("hard_guards_checked") or [], ensure_ascii=False),
                    json.dumps(decision.get("learned_policy_signals") or [], ensure_ascii=False),
                    json.dumps(decision.get("considered_actions") or [], ensure_ascii=False),
                    json.dumps(decision.get("chosen_action") or {}, ensure_ascii=False),
                    str(decision.get("chosen_action_reasoning") or ""),
                    json.dumps(decision.get("rejected_actions") or [], ensure_ascii=False),
                    None,
                    supersedes,
                    str(decision.get("user_override_state") or "none"),
                    json.dumps(decision.get("confidence") or {}, ensure_ascii=False),
                    str(decision.get("current_truth_state") or "active"),
                    json.dumps(decision.get("narrative") or {}, ensure_ascii=False),
                    signature,
                ),
            )
        conn.commit()
        decision["decision_id"] = decision_id
        decision["last_recomputed_at"] = now
        decision["is_recompute"] = bool(prev and prev_signature == signature)
        decision["supersedes"] = prev_id if (prev and prev_signature != signature and prev_id) else decision.get("supersedes")
        return decision
    finally:
        conn.close()


def _persist_day_and_actions(
    day_iso: str,
    logging_payload: dict[str, Any],
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    targets_after: dict[str, Any],
    status: str,
    decision_id: Optional[str] = None,
) -> None:
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        now = _utcnow_iso()

        planned = logging_payload.get("planned_totals") if isinstance(logging_payload.get("planned_totals"), dict) else {}
        logged = logging_payload.get("logged_totals") if isinstance(logging_payload.get("logged_totals"), dict) else {}
        remaining = logging_payload.get("remaining") if isinstance(logging_payload.get("remaining"), dict) else {}
        base_targets = logging_payload.get("targets") if isinstance(logging_payload.get("targets"), dict) else {}

        planned_meals = logging_payload.get("planned_meals") or []
        logged_meals = logging_payload.get("logged_meals") or []
        open_meals = [m for m in planned_meals if str((m or {}).get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}]
        next_open_slot_id = None
        if open_meals:
            next_open_slot_id = _safe_int(sorted(open_meals, key=lambda m: _safe_int((m or {}).get("slot_index"), 999))[0].get("slot_id"), 0) or None

        drift_flags = {
            "kcal_behind": _safe_float(remaining.get("kcal"), 0.0) > 250.0,
            "protein_behind": _safe_float(remaining.get("p"), 0.0) > 45.0,
            "has_missed": any(str((m or {}).get("status") or "").lower() in {"skipped", "missed"} for m in planned_meals),
        }

        explain = {
            "status": status,
            "actions_count": len(actions),
            "reason_codes": sorted({code for a in actions for code in (a.get("reason_codes") or [])}),
            "data_used": {
                "weight_trend": bool(context.get("weight_trend", {}).get("available")),
                "logging_today": True,
                "calendar_webuntis": bool((context.get("school") or {}).get("available")),
                "calendar_google": bool((context.get("calendar") or {}).get("available")),
                "training": bool((context.get("training") or {}).get("available")),
                "open_macros": True,
                "inventory": bool(context.get("inventory_used")),
            },
        }

        cur.execute(
            """
            INSERT INTO core_nutrition_day_state (
                date_iso, status,
                target_kcal, target_p, target_c, target_f,
                base_target_kcal, base_target_p, base_target_c, base_target_f,
                planned_kcal, planned_p, planned_c, planned_f,
                logged_kcal, logged_p, logged_c, logged_f,
                remaining_kcal, remaining_p, remaining_c, remaining_f,
                open_meals, last_logged_meal_id, next_open_slot_id,
                drift_flags_json, context_json, explain_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_iso) DO UPDATE SET
                status=excluded.status,
                target_kcal=excluded.target_kcal,
                target_p=excluded.target_p,
                target_c=excluded.target_c,
                target_f=excluded.target_f,
                base_target_kcal=excluded.base_target_kcal,
                base_target_p=excluded.base_target_p,
                base_target_c=excluded.base_target_c,
                base_target_f=excluded.base_target_f,
                planned_kcal=excluded.planned_kcal,
                planned_p=excluded.planned_p,
                planned_c=excluded.planned_c,
                planned_f=excluded.planned_f,
                logged_kcal=excluded.logged_kcal,
                logged_p=excluded.logged_p,
                logged_c=excluded.logged_c,
                logged_f=excluded.logged_f,
                remaining_kcal=excluded.remaining_kcal,
                remaining_p=excluded.remaining_p,
                remaining_c=excluded.remaining_c,
                remaining_f=excluded.remaining_f,
                open_meals=excluded.open_meals,
                last_logged_meal_id=excluded.last_logged_meal_id,
                next_open_slot_id=excluded.next_open_slot_id,
                drift_flags_json=excluded.drift_flags_json,
                context_json=excluded.context_json,
                explain_json=excluded.explain_json,
                updated_at=excluded.updated_at
            """,
            (
                day_iso,
                status,
                targets_after.get("kcal"),
                targets_after.get("p"),
                targets_after.get("c"),
                targets_after.get("f"),
                base_targets.get("kcal"),
                base_targets.get("p"),
                base_targets.get("c"),
                base_targets.get("f"),
                planned.get("kcal"),
                planned.get("p"),
                planned.get("c"),
                planned.get("f"),
                logged.get("kcal"),
                logged.get("p"),
                logged.get("c"),
                logged.get("f"),
                remaining.get("kcal"),
                remaining.get("p"),
                remaining.get("c"),
                remaining.get("f"),
                len(open_meals),
                (_safe_int((logged_meals[-1] or {}).get("id"), 0) if logged_meals else None),
                next_open_slot_id,
                json.dumps(drift_flags, ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
                json.dumps(explain, ensure_ascii=False),
                now,
            ),
        )

        current_keys = {_action_key(day_iso, a) for a in actions}
        if current_keys:
            placeholders = ",".join("?" for _ in current_keys)
            cur.execute(
                f"""
                DELETE FROM core_nutrition_actions
                WHERE date_iso=?
                  AND source='core'
                  AND action_key NOT IN ({placeholders})
                """,
                (day_iso, *sorted(current_keys)),
            )
        else:
            cur.execute(
                """
                DELETE FROM core_nutrition_actions
                WHERE date_iso=?
                  AND source='core'
                """,
                (day_iso,),
            )

        for action in actions:
            payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
            trace = action.get("trace") if isinstance(action.get("trace"), dict) else {}
            key = _action_key(day_iso, action)
            cur.execute(
                """
                INSERT INTO core_nutrition_actions (
                    date_iso, action_key, action_type, status, priority,
                    meal_slot_id, meal_slot_index, from_time, to_time,
                    target_delta_kcal, target_delta_p, target_delta_c, target_delta_f,
                    reason_codes_json, reason_text, human_text,
                    payload_json, trace_json, source, created_at, updated_at, decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_iso, action_key) DO UPDATE SET
                    status=CASE
                        WHEN core_nutrition_actions.status IN ('telegram_confirmed', 'applied', 'rejected')
                            THEN core_nutrition_actions.status
                        ELSE excluded.status
                    END,
                    priority=excluded.priority,
                    meal_slot_id=excluded.meal_slot_id,
                    meal_slot_index=excluded.meal_slot_index,
                    from_time=excluded.from_time,
                    to_time=excluded.to_time,
                    target_delta_kcal=excluded.target_delta_kcal,
                    target_delta_p=excluded.target_delta_p,
                    target_delta_c=excluded.target_delta_c,
                    target_delta_f=excluded.target_delta_f,
                    reason_codes_json=excluded.reason_codes_json,
                    reason_text=excluded.reason_text,
                    human_text=excluded.human_text,
                    payload_json=excluded.payload_json,
                    trace_json=excluded.trace_json,
                    decision_id=excluded.decision_id,
                    updated_at=excluded.updated_at
                """,
                (
                    day_iso,
                    key,
                    str(action.get("action_type") or ""),
                    str(action.get("status") or "proposed"),
                    _safe_int(action.get("priority"), 0),
                    _safe_int(payload.get("slot_id"), 0) or None,
                    _safe_int(payload.get("slot_index"), 0) or None,
                    str(payload.get("from_time") or "").strip() or None,
                    str(payload.get("to_time") or payload.get("time") or "").strip() or None,
                    _safe_float(payload.get("delta_kcal"), 0.0) if "delta_kcal" in payload else None,
                    _safe_float(payload.get("delta_p"), 0.0) if "delta_p" in payload else None,
                    _safe_float(payload.get("delta_c"), 0.0) if "delta_c" in payload else None,
                    _safe_float(payload.get("delta_f"), 0.0) if "delta_f" in payload else None,
                    json.dumps(action.get("reason_codes") or [], ensure_ascii=False),
                    str(action.get("reason_text") or "").strip() or None,
                    str(action.get("human") or "").strip() or None,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(trace, ensure_ascii=False),
                    "core",
                    now,
                    now,
                    str(decision_id or "").strip() or None,
                ),
            )

        conn.commit()
    finally:
        conn.close()


def _manual_override_present(day_iso: str) -> bool:
    conn = get_nutrition_db()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM nutrition_planned_meal_status
            WHERE log_date=?
              AND LOWER(COALESCE(status_source, 'user')) IN ('user', 'manual', 'telegram')
              AND LOWER(COALESCE(status, 'open')) IN ('manual_override', 'shifted', 'skipped', 'changed')
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        conn.close()


def _load_existing_action_statuses(day_iso: str) -> dict[str, str]:
    conn = get_nutrition_db()
    try:
        rows = conn.execute(
            """
            SELECT action_key, status
            FROM core_nutrition_actions
            WHERE date_iso=? AND source='core'
            """,
            (day_iso,),
        ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            key = str(row[0] or "").strip()
            status = str(row[1] or "").strip().lower()
            if key:
                out[key] = status
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def _apply_actions_to_meals(planned_meals: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_slot: dict[int, dict[str, Any]] = {}
    for action in actions:
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        slot_id = _safe_int(payload.get("slot_id"), 0)
        if slot_id <= 0:
            continue
        by_slot.setdefault(slot_id, {})[str(action.get("action_type") or "")] = action

    out: list[dict[str, Any]] = []
    for meal in planned_meals:
        row = dict(meal)
        state = str(row.get("state") or "").strip().lower()
        if state in {"logged", "skipped", "replaced", "cancelled_by_replan", "merged_into_other_meal", "fulfilled_by_alternative"}:
            out.append(row)
            continue
        slot_id = _safe_int(row.get("slot_id"), 0)
        entry = by_slot.get(slot_id) or {}
        marker = None
        if "shift_meal" in entry:
            payload = (entry["shift_meal"].get("payload") or {})
            row["core_adjusted_time"] = payload.get("to_time")
            row["planned_time"] = row.get("time_text")
            row["time_text"] = payload.get("to_time") or row.get("time_text")
            row["status"] = "adjusted_by_core"
            marker = "shifted"
        if "replace_meal" in entry and not bool(row.get("stale_base_reference_blocked")):
            payload = (entry["replace_meal"].get("payload") or {})
            row["core_adjusted_name"] = payload.get("replacement_title") or row.get("title")
            row["planned_name"] = row.get("title")
            row["title"] = payload.get("replacement_title") or row.get("title")
            row["status"] = "adjusted_by_core"
            marker = "adjusted"
        if "adjust_servings" in entry:
            payload = (entry["adjust_servings"].get("payload") or {})
            factor = _safe_float(payload.get("factor"), 1.0) or 1.0
            from_servings = _safe_float(payload.get("from_servings"), _safe_float(row.get("servings"), 1.0)) or 1.0
            to_servings = _safe_float(payload.get("to_servings"), from_servings * factor) or from_servings
            row["planned_servings"] = row.get("servings")
            row["core_adjusted_servings"] = to_servings
            row["servings"] = to_servings
            if isinstance(row.get("macros"), dict):
                row["macros"] = {
                    "kcal": round(_safe_float((row["macros"] or {}).get("kcal"), 0.0) * factor, 1),
                    "p": round(_safe_float((row["macros"] or {}).get("p"), 0.0) * factor, 1),
                    "c": round(_safe_float((row["macros"] or {}).get("c"), 0.0) * factor, 1),
                    "f": round(_safe_float((row["macros"] or {}).get("f"), 0.0) * factor, 1),
                }
            new_items: list[dict[str, Any]] = []
            for item in (row.get("items") or []):
                item_row = dict(item) if isinstance(item, dict) else {}
                amount = item_row.get("amount")
                if isinstance(amount, (int, float)):
                    item_row["amount"] = round(float(amount) * factor, 2)
                new_items.append(item_row)
            if new_items:
                row["items"] = new_items
            row["status"] = "adjusted_by_core"
            marker = "adjusted"
        if marker:
            row["core_marker"] = marker
            row["core_reason"] = "; ".join(entry[k].get("reason_text") or "" for k in entry.keys() if entry.get(k))
        out.append(row)
    return out


def _sync_applied_actions_to_day_events(
    day_iso: str,
    actions: list[dict[str, Any]],
    planned_meals: list[dict[str, Any]],
) -> None:
    applied = {"applied", "telegram_confirmed"}
    for action in actions:
        status = str(action.get("status") or "").strip().lower()
        if status not in applied:
            continue
        action_type = str(action.get("action_type") or "").strip().lower()
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        slot_id = _safe_int(payload.get("slot_id"), 0) or None
        if not slot_id:
            continue
        reason_codes = action.get("reason_codes") or []
        reason_text = action.get("reason_text") or ""
        dedupe_key = f"core_action:{_action_key(day_iso, action)}"

        if action_type == "shift_meal":
            append_day_event(
                day_iso=day_iso,
                event_type="core_shifted_time",
                meal_slot_id=slot_id,
                payload={"to_time": payload.get("to_time"), "from_time": payload.get("from_time")},
                reason_code="TRAINING_WINDOW" if "TRAINING_WINDOW" in reason_codes else None,
                reason_text=reason_text,
                dedupe_key=dedupe_key,
            )
            continue

        if action_type == "adjust_servings":
            factor = _safe_float(payload.get("factor"), 1.0) or 1.0
            meal = next((m for m in planned_meals if _safe_int((m or {}).get("slot_id"), 0) == slot_id), None)
            macros = (meal.get("macros") if isinstance(meal, dict) else None) or {}
            if isinstance(macros, dict):
                macros = {
                    "kcal": round(_safe_float(macros.get("kcal"), 0.0) * factor, 1),
                    "p": round(_safe_float(macros.get("p"), 0.0) * factor, 1),
                    "c": round(_safe_float(macros.get("c"), 0.0) * factor, 1),
                    "f": round(_safe_float(macros.get("f"), 0.0) * factor, 1),
                }
            append_day_event(
                day_iso=day_iso,
                event_type="core_adjusted_portion",
                meal_slot_id=slot_id,
                payload={
                    "from_servings": payload.get("from_servings"),
                    "to_servings": payload.get("to_servings"),
                    "factor": factor,
                    "macros": macros,
                },
                reason_code="TRAINING_MACRO_SHIFT" if "TRAINING_MACRO_SHIFT" in reason_codes else None,
                reason_text=reason_text,
                dedupe_key=dedupe_key,
            )
            continue

        if action_type == "replace_meal":
            append_day_event(
                day_iso=day_iso,
                event_type="meal_replaced",
                meal_slot_id=slot_id,
                payload={
                    "title": payload.get("replacement_title"),
                    "time_text": payload.get("to_time") or payload.get("time_text"),
                },
                reason_code=(reason_codes[0] if reason_codes else None),
                reason_text=reason_text,
                dedupe_key=dedupe_key,
            )


def evaluate_core_nutrition_day(date_iso: Optional[str] = None) -> dict[str, Any]:
    ensure_core_nutrition_schema()
    ensure_core_day_state_schema()
    day_iso = _normalize_date_iso(date_iso)
    now_iso = _utcnow_iso()
    now_min = _parse_hhmm(_now_berlin().strftime("%H:%M")) or 0

    base_payload = get_logging_day_payload(day_iso)
    try:
        logging_payload = build_active_day_plan(day_iso, base_payload=base_payload)
    except Exception:
        logging_payload = base_payload
    school_ctx = _schedule_from_latest_file(day_iso)
    training_ctx = resolve_training_context(day_iso)
    calendar_ctx = _calendar_context_from_google(day_iso)
    trend = _weight_trend(day_iso)
    policy = _load_policy_signals()

    context = {
        "day": day_iso,
        "school": school_ctx,
        "training": training_ctx,
        "calendar": calendar_ctx,
        "weight_trend": trend,
        "core_policy": policy,
        "inventory_used": False,
    }

    actions = _rule_actions(day_iso, logging_payload, context, trend, policy=policy)
    applied_statuses = {"applied", "telegram_confirmed"}
    existing_statuses = _load_existing_action_statuses(day_iso)
    for action in actions:
        key = _action_key(day_iso, action)
        prev = existing_statuses.get(key)
        if prev in {"telegram_confirmed", "applied", "rejected"}:
            action["status"] = prev

    base_targets = dict(logging_payload.get("targets") or {})
    adjusted_targets = dict(base_targets)
    for action in actions:
        if str(action.get("action_type") or "") != "change_target":
            continue
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        adjusted_targets["kcal"] = round(_safe_float(adjusted_targets.get("kcal"), 0.0) + _safe_float(payload.get("delta_kcal"), 0.0), 1)
        adjusted_targets["p"] = round(_safe_float(adjusted_targets.get("p"), 0.0) + _safe_float(payload.get("delta_p"), 0.0), 1)
        adjusted_targets["c"] = round(_safe_float(adjusted_targets.get("c"), 0.0) + _safe_float(payload.get("delta_c"), 0.0), 1)
        adjusted_targets["f"] = round(_safe_float(adjusted_targets.get("f"), 0.0) + _safe_float(payload.get("delta_f"), 0.0), 1)

    manual = _manual_override_present(day_iso)
    previous_state = _load_latest_decision_for_day(day_iso)
    day_logic = _compute_day_logic(now_min, logging_payload, context, actions)
    guardrails = _build_guardrails(now_min, logging_payload, context, actions)
    learning_signals = _build_learning_signals(policy)
    confidence = _compute_data_quality(now_min, context, logging_payload)

    considered_actions = [
        {
            "type": str(a.get("action_type") or ""),
            "label": _action_label(str(a.get("action_type") or "")),
            "level": _status_to_level(str(a.get("status") or "")),
            "priority": _safe_int(a.get("priority"), 0),
            "why": str(a.get("reason_text") or "").strip(),
            "payload": a.get("payload") or {},
        }
        for a in actions
    ]
    chosen_action = considered_actions[0] if considered_actions else {}
    rejected_actions = considered_actions[1:] if len(considered_actions) > 1 else []
    user_override_state = "user_changed_state" if manual else "none"

    signature_payload = {
        "day": day_iso,
        "remaining": logging_payload.get("remaining") or {},
        "planned_statuses": [
            {
                "slot_id": (m or {}).get("slot_id"),
                "status": (m or {}).get("status"),
                "time_text": (m or {}).get("time_text"),
            }
            for m in (logging_payload.get("planned_meals") or [])
            if isinstance(m, dict)
        ],
        "actions": [
            {
                "type": a.get("action_type"),
                "status": a.get("status"),
                "reason_codes": a.get("reason_codes") or [],
                "payload": a.get("payload") or {},
            }
            for a in actions
        ],
        "manual": manual,
        "school_flags": {
            "available": school_ctx.get("available"),
            "early_home": school_ctx.get("early_home"),
            "long_school_day": school_ctx.get("long_school_day"),
        },
        "training": {
            "today": training_ctx.get("training_today"),
            "time": training_ctx.get("training_time"),
            "type": training_ctx.get("training_type"),
        },
    }
    signature = hashlib.sha1(
        json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    is_recompute = bool(previous_state and str(previous_state.get("signature") or "") == signature)

    narrative = _build_decision_narrative(
        day_iso=day_iso,
        now_iso=now_iso,
        day_logic=day_logic,
        context=context,
        logging_payload=logging_payload,
        actions=actions,
        guardrails=guardrails,
        learning_signals=learning_signals,
        confidence=confidence,
        manual_override=manual,
        previous_active=previous_state,
        is_recompute=is_recompute,
    )
    live_status_code = str((narrative.get("header") or {}).get("live_status_code") or "monitoring")

    decision_state = {
        "decision_id": str((previous_state or {}).get("decision_id") or f"cn-{uuid4().hex[:14]}"),
        "domain": DECISION_DOMAIN_NUTRITION,
        "created_at": now_iso,
        "last_recomputed_at": now_iso,
        "status": live_status_code,
        "priority_mode": day_logic.get("mode"),
        "day_logic": day_logic.get("headline"),
        "context_snapshot": {
            "time_now": _to_hhmm(now_min),
            "remaining": logging_payload.get("remaining") or {},
            "logged_totals": logging_payload.get("logged_totals") or {},
            "open_meals": len([m for m in (logging_payload.get("planned_meals") or []) if str((m or {}).get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}]),
            "school": {
                "available": bool(school_ctx.get("available")),
                "school_end": school_ctx.get("school_end"),
                "early_home": bool(school_ctx.get("early_home")),
                "long_school_day": bool(school_ctx.get("long_school_day")),
                "largest_free_window_min": int(school_ctx.get("largest_free_window_min") or 0),
            },
            "training": {
                "training_today": bool(training_ctx.get("training_today")),
                "training_time": training_ctx.get("training_time"),
                "training_type": training_ctx.get("training_type"),
            },
            "calendar": {
                "available": bool(calendar_ctx.get("available")),
                "events_count": len(calendar_ctx.get("events") or []),
            },
            "data_freshness": confidence.get("band"),
            "manual_override_present": manual,
        },
        "hard_guards_checked": guardrails,
        "learned_policy_signals": learning_signals,
        "considered_actions": considered_actions,
        "chosen_action": chosen_action,
        "chosen_action_reasoning": str(chosen_action.get("why") or "Keine operative Aktion priorisiert."),
        "rejected_actions": rejected_actions,
        "superseded_by": None,
        "supersedes": None,
        "user_override_state": user_override_state,
        "confidence": confidence,
        "current_truth_state": "manual_override" if manual else "active",
        "narrative": narrative,
    }
    decision_state = _persist_decision_state(day_iso, decision_state, signature)

    has_catch = any(str(a.get("action_type") or "") == "add_catchup_meal" and str(a.get("status") or "") in applied_statuses for a in actions)
    has_adj = any(str(a.get("status") or "") in applied_statuses for a in actions)
    status = "PLAN"
    if manual:
        status = "MANUELL"
    elif has_catch:
        status = "CATCH-UP"
    elif has_adj:
        status = "CORE ADJUSTED"
    else:
        status = "CORE STEUERT"

    _persist_day_and_actions(
        day_iso,
        logging_payload,
        context,
        actions,
        adjusted_targets,
        status,
        decision_id=str(decision_state.get("decision_id") or ""),
    )

    applied_actions = [a for a in actions if str(a.get("status") or "") in applied_statuses]
    _sync_applied_actions_to_day_events(day_iso, applied_actions, logging_payload.get("planned_meals") or [])
    planned_with_overrides = _apply_actions_to_meals(logging_payload.get("planned_meals") or [], applied_actions)
    history = _load_decision_history_for_day(day_iso, limit=6)

    data_used = {
        "weight_trend": trend if trend.get("available") else None,
        "logging_today": {
            "logged_totals": logging_payload.get("logged_totals") or {},
            "remaining": logging_payload.get("remaining") or {},
            "open_meals": len([m for m in planned_with_overrides if str(m.get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}]),
        },
        "calendar_webuntis": {
            "available": bool(school_ctx.get("available")),
            "school_end": school_ctx.get("school_end"),
            "free_windows": school_ctx.get("free_windows") or [],
            "early_home": bool(school_ctx.get("early_home")),
            "long_school_day": bool(school_ctx.get("long_school_day")),
        },
        "calendar_google": {
            "available": bool(calendar_ctx.get("available")),
            "events_count": len(calendar_ctx.get("events") or []),
        },
        "training": training_ctx,
        "open_macros": logging_payload.get("remaining") or {},
        "core_policy": policy,
    }

    decisions = [
        {
            "type": action.get("action_type"),
            "status": action.get("status") or "proposed",
            "reason_codes": action.get("reason_codes") or [],
            "why": action.get("reason_text") or "",
            "human": action.get("human") or "",
            "trace": action.get("trace") or {},
            "payload": action.get("payload") or {},
        }
        for action in actions
    ]

    catchups = [
        {
            "title": (a.get("payload") or {}).get("title") or "Catch-up",
            "time": (a.get("payload") or {}).get("time"),
            "macros": (a.get("payload") or {}).get("macros") or {},
            "source": "core",
        }
        for a in actions
        if str(a.get("action_type") or "") == "add_catchup_meal"
    ]

    return {
        "ok": True,
        "date": day_iso,
        "status": status,
        "pill_label": str((narrative.get("header") or {}).get("live_status_label") or status),
        "active": True,
        "adjusted": has_adj,
        "manual_override": manual,
        "catch_up": has_catch,
        "decision_state": decision_state,
        "decision_narrative": narrative,
        "decision_history": history,
        "targets": {
            "base": {
                "kcal": base_targets.get("kcal"),
                "p": base_targets.get("p"),
                "c": base_targets.get("c"),
                "f": base_targets.get("f"),
                "mode": base_targets.get("mode"),
            },
            "effective": {
                "kcal": adjusted_targets.get("kcal"),
                "p": adjusted_targets.get("p"),
                "c": adjusted_targets.get("c"),
                "f": adjusted_targets.get("f"),
                "mode": base_targets.get("mode"),
            },
        },
        "nutrition_state": {
            "planned_totals": logging_payload.get("planned_totals") or {},
            "logged_totals": logging_payload.get("logged_totals") or {},
            "remaining": logging_payload.get("remaining") or {},
            "open_meals": len([m for m in planned_with_overrides if str(m.get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}]),
            "last_logged_meal": (logging_payload.get("logged_meals") or [])[-1] if (logging_payload.get("logged_meals") or []) else None,
            "next_open_meal": next((m for m in planned_with_overrides if str(m.get("status") or "open").lower() in {"open", "planned", "shifted", "adjusted_by_core"}), None),
        },
        "data_used": data_used,
        "decisions": decisions,
        "catch_up_suggestions": catchups,
        "next_step": (
            next((d.get("human") for d in decisions if str(d.get("status") or "") == "proposed"), None)
            or (decisions[0].get("human") if decisions else "Plan normal weiterführen.")
        ),
        "planned_meals": planned_with_overrides,
        "reason_codes": sorted({code for d in decisions for code in (d.get("reason_codes") or []) if code in REASON_CODES}),
        "trace_updated_at": now_iso,
    }


def get_core_nutrition_actions_for_day(date_iso: Optional[str] = None) -> list[dict[str, Any]]:
    ensure_core_nutrition_schema()
    day_iso = _normalize_date_iso(date_iso)
    conn = get_nutrition_db()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM core_nutrition_actions
            WHERE date_iso=?
            ORDER BY priority DESC, id ASC
            """,
            (day_iso,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            payload = json.loads(data.get("payload_json") or "{}") if data.get("payload_json") else {}
            trace = json.loads(data.get("trace_json") or "{}") if data.get("trace_json") else {}
            reasons = json.loads(data.get("reason_codes_json") or "[]") if data.get("reason_codes_json") else []
            out.append(
                {
                    "id": int(data.get("id") or 0),
                    "action_key": str(data.get("action_key") or ""),
                    "decision_id": str(data.get("decision_id") or ""),
                    "date": data.get("date_iso"),
                    "action_type": data.get("action_type"),
                    "status": data.get("status"),
                    "priority": data.get("priority"),
                    "reason_codes": reasons,
                    "reason_text": data.get("reason_text"),
                    "human": data.get("human_text"),
                    "payload": payload,
                    "trace": trace,
                    "source": data.get("source"),
                    "updated_at": data.get("updated_at"),
                }
            )
        return out
    finally:
        conn.close()


def mark_last_core_action_telegram_confirmed(date_iso: Optional[str] = None) -> Optional[dict[str, Any]]:
    ensure_core_nutrition_schema()
    day_iso = _normalize_date_iso(date_iso)
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, action_type, payload_json, human_text
            FROM core_nutrition_actions
            WHERE date_iso=? AND status IN ('proposed', 'accepted')
            ORDER BY priority DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        if not row:
            return None
        now = _utcnow_iso()
        cur.execute(
            "UPDATE core_nutrition_actions SET status='telegram_confirmed', source='telegram', updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
        conn.commit()
        payload = json.loads(row[2] or "{}") if row[2] else {}
        return {
            "id": int(row[0]),
            "action_type": str(row[1] or ""),
            "payload": payload,
            "human": str(row[3] or ""),
        }
    finally:
        conn.close()


def mark_core_action_telegram_confirmed(action_id: int) -> Optional[dict[str, Any]]:
    ensure_core_nutrition_schema()
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, action_type, payload_json, human_text
            FROM core_nutrition_actions
            WHERE id=? AND status IN ('proposed', 'accepted')
            LIMIT 1
            """,
            (int(action_id),),
        ).fetchone()
        if not row:
            return None
        now = _utcnow_iso()
        cur.execute(
            "UPDATE core_nutrition_actions SET status='telegram_confirmed', source='telegram', updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
        conn.commit()
        payload = json.loads(row[2] or "{}") if row[2] else {}
        return {
            "id": int(row[0]),
            "action_type": str(row[1] or ""),
            "payload": payload,
            "human": str(row[3] or ""),
        }
    finally:
        conn.close()


def mark_last_core_action_rejected(date_iso: Optional[str] = None) -> Optional[dict[str, Any]]:
    ensure_core_nutrition_schema()
    day_iso = _normalize_date_iso(date_iso)
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, action_type, payload_json, human_text
            FROM core_nutrition_actions
            WHERE date_iso=? AND status IN ('proposed', 'accepted')
            ORDER BY priority DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        if not row:
            return None
        now = _utcnow_iso()
        cur.execute(
            "UPDATE core_nutrition_actions SET status='rejected', source='telegram', updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
        conn.commit()
        payload = json.loads(row[2] or "{}") if row[2] else {}
        return {
            "id": int(row[0]),
            "action_type": str(row[1] or ""),
            "payload": payload,
            "human": str(row[3] or ""),
        }
    finally:
        conn.close()


def mark_core_action_rejected(action_id: int) -> Optional[dict[str, Any]]:
    ensure_core_nutrition_schema()
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, action_type, payload_json, human_text
            FROM core_nutrition_actions
            WHERE id=? AND status IN ('proposed', 'accepted')
            LIMIT 1
            """,
            (int(action_id),),
        ).fetchone()
        if not row:
            return None
        now = _utcnow_iso()
        cur.execute(
            "UPDATE core_nutrition_actions SET status='rejected', source='telegram', updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
        conn.commit()
        payload = json.loads(row[2] or "{}") if row[2] else {}
        return {
            "id": int(row[0]),
            "action_type": str(row[1] or ""),
            "payload": payload,
            "human": str(row[3] or ""),
        }
    finally:
        conn.close()


__all__ = [
    "ensure_core_nutrition_schema",
    "evaluate_core_nutrition_day",
    "get_core_nutrition_actions_for_day",
    "mark_last_core_action_telegram_confirmed",
    "mark_core_action_telegram_confirmed",
    "mark_last_core_action_rejected",
    "mark_core_action_rejected",
    "REASON_CODES",
]
