from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database.connections import get_nutrition_db, get_runs_db


TZ = ZoneInfo("Europe/Berlin")
ERGO_DISTANCE_KM_PER_MIN_WATT = 0.003259893048128342
STAIR_FLOORS_PER_KM = 100.0
STAIR_VERTICAL_METERS_PER_FLOOR = 3.0
GRAVITY_M_S2 = 9.81
HR_ZONE_ORDER = ["Z1", "Z2", "Z3", "Z4", "Z5"]
HR_ZONE_THRESHOLDS = [
    ("Z1", 0, 129),
    ("Z2", 130, 149),
    ("Z3", 150, 164),
    ("Z4", 165, 179),
    ("Z5", 180, 260),
]


@dataclass(frozen=True)
class CardioFilters:
    time_range: int | None = None
    sport_type: str = "all"
    data_mode: str = "all"
    primary_metric: str = "minutes"
    indoor_mode: str = "all"
    source: str = "all"
    limit: int = 250
    offset: int = 0


def parse_days(value, default=None):
    text = str(value if value is not None else default).strip().lower()
    if text in {"all", "alle", "*"}:
        return None
    try:
        days = int(float(text))
    except Exception:
        return default
    return days if days > 0 else None


def clamp_int(value, default, lo, hi):
    try:
        n = int(value)
    except Exception:
        n = default
    return max(lo, min(hi, n))


def _finite(value):
    try:
        n = float(value)
    except Exception:
        return None
    return n if math.isfinite(n) else None


def _parse_ts(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def _table_columns(conn, table):
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _table_exists(conn, table):
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _sport_meta_from_row(row: dict, cols: set[str]) -> tuple[str, str, bool]:
    raw_sport = str(row.get("sport_type") or row.get("activity_type") or "").strip().lower() if cols else ""
    raw_type = str(row.get("run_type") or row.get("type") or row.get("classification") or "").strip().lower()
    sport = raw_sport or (raw_type if raw_type in {"ergo", "bike", "walk", "row", "swim", "other"} else "run")
    labels = {
        "run": ("Laufen", False),
        "ergo": ("Ergo", True),
        "stair": ("Stair Master", True),
        "bike": ("Bike", False),
        "walk": ("Walk", False),
        "row": ("Rudern", True),
        "swim": ("Schwimmen", False),
        "other": ("Cardio", False),
    }
    label, indoor = labels.get(sport, ("Cardio", False))
    return sport, label, indoor


def _ensure_cardio_columns(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "runs")
    wanted = {
        "sport_type": "TEXT",
        "run_type": "TEXT",
        "note": "TEXT",
        "avg_power": "REAL",
        "stair_floors": "REAL",
    }
    for name, decl in wanted.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    conn.commit()


def _hr_zone(avg_hr) -> dict:
    hr = _finite(avg_hr)
    if hr is None or hr < 40:
        return {"hr_zone": None, "hr_zone_source": None, "hr_zone_minutes": None}
    hr = round(hr)
    for zone, lo, hi in HR_ZONE_THRESHOLDS:
        if lo <= hr <= hi:
            return {"hr_zone": zone, "hr_zone_source": "avg_hr", "hr_zone_minutes": None}
    return {"hr_zone": None, "hr_zone_source": None, "hr_zone_minutes": None}


def _median(values):
    vals = sorted(v for v in (_finite(x) for x in values) if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _pace_context(rows: list[dict]) -> dict:
    run_paces = [
        p
        for p in (_finite(r.get("avg_pace_sec_per_km")) for r in rows if r.get("sport_type") == "run")
        if p is not None and 150 <= p <= 900
    ]
    anchors = {}
    for zone in HR_ZONE_ORDER:
        zone_paces = [
            _finite(r.get("avg_pace_sec_per_km"))
            for r in rows
            if r.get("sport_type") == "run"
            and r.get("avg_pace_sec_per_km") is not None
            and _hr_zone(r.get("avg_hr_bpm")).get("hr_zone") == zone
        ]
        median = _median([p for p in zone_paces if p is not None and 150 <= p <= 900])
        if median is not None:
            anchors[zone] = median
    return {"paces": sorted(run_paces), "anchors": anchors}


def _pace_fallback_zone(pace_sec, context: dict | None) -> dict:
    pace = _finite(pace_sec)
    if pace is None or pace < 150 or pace > 900:
        return {"hr_zone": None, "hr_zone_source": None, "hr_zone_minutes": None}
    anchors = (context or {}).get("anchors") or {}
    if len(anchors) >= 2:
        zone = min(anchors, key=lambda z: abs(float(anchors[z]) - pace))
        return {"hr_zone": zone, "hr_zone_source": "pace_fallback", "hr_zone_minutes": None}
    paces = (context or {}).get("paces") or []
    if len(paces) < 3:
        return {"hr_zone": None, "hr_zone_source": None, "hr_zone_minutes": None}
    faster_or_equal = sum(1 for p in paces if p <= pace)
    quantile = faster_or_equal / len(paces)
    if quantile <= 0.10:
        zone = "Z5"
    elif quantile <= 0.25:
        zone = "Z4"
    elif quantile <= 0.45:
        zone = "Z3"
    elif quantile <= 0.90:
        zone = "Z2"
    else:
        zone = "Z1"
    return {"hr_zone": zone, "hr_zone_source": "pace_fallback", "hr_zone_minutes": None}


def _session_zone(row: dict, context: dict | None) -> dict:
    zone = _hr_zone(row.get("avg_hr_bpm"))
    if zone.get("hr_zone"):
        return zone
    if row.get("sport_type") == "run":
        return _pace_fallback_zone(row.get("avg_pace_sec_per_km"), context)
    return zone


def _zone_seconds(duration_sec, zone):
    z = {f"z{i}_sec": None for i in range(1, 6)}
    if not zone or not duration_sec:
        return z
    z[f"{zone.lower()}_sec"] = duration_sec
    return z


def _load_run_rows(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "runs"):
        return []
    _ensure_cardio_columns(conn)
    cols = _table_columns(conn, "runs")
    rows = conn.execute("SELECT * FROM runs").fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        started = _parse_ts(row.get("date") or row.get("start_date") or row.get("started_at"))
        if not started:
            continue
        distance_m = _finite(row.get("distance"))
        duration_sec = _finite(row.get("moving_time"))
        if duration_sec is None:
            duration_sec = _finite(row.get("duration_sec"))
        if duration_sec is None:
            duration_sec = _finite(row.get("elapsed_time"))
        pace_sec = _finite(row.get("pace"))
        if pace_sec is None:
            pace_sec = _finite(row.get("pace_sec_per_km"))
        distance_km = distance_m / 1000.0 if distance_m is not None else None
        if duration_sec is None and distance_km and pace_sec:
            duration_sec = pace_sec * distance_km
        if pace_sec is None and duration_sec and distance_km:
            pace_sec = duration_sec / distance_km
        source = str(row.get("source") or "strava").strip() or "strava"
        sport_type, activity_label, indoor = _sport_meta_from_row(row, cols)
        out.append(
            {
                "id": f"run:{row.get('id') or started.isoformat()}",
                "raw_id": row.get("id"),
                "started_at": started.isoformat(),
                "_dt": started,
                "_utc": started.astimezone(timezone.utc),
                "source": source,
                "sport_type": sport_type,
                "sport_family": "endurance",
                "activity_label": activity_label,
                "indoor": indoor,
                "duration_sec": int(round(duration_sec)) if duration_sec else None,
                "distance_m": round(distance_m, 1) if distance_m is not None else None,
                "avg_pace_sec_per_km": round(pace_sec, 1) if pace_sec and sport_type == "run" else None,
                "avg_hr_bpm": _finite(row.get("avg_hr")) if "avg_hr" in cols else None,
                "max_hr_bpm": _finite(row.get("max_hr")) if "max_hr" in cols else None,
                "avg_power_w": _finite(row.get("avg_power")) if "avg_power" in cols else (_finite(row.get("power")) if "power" in cols else None),
                "cadence_rpm": _finite(row.get("cadence_rpm")) if "cadence_rpm" in cols else (_finite(row.get("cadence")) if "cadence" in cols else None),
                "calories_kcal": _finite(row.get("kcal")) if "kcal" in cols else (_finite(row.get("calories")) if "calories" in cols else None),
                "elevation_gain_m": _finite(row.get("elevation_gain")) if "elevation_gain" in cols else None,
                "stair_floors": _finite(row.get("stair_floors")) if "stair_floors" in cols else None,
                "note": row.get("note") if "note" in cols else None,
            }
        )
    return out


def _normalize_sessions(rows: list[dict]) -> list[dict]:
    context = _pace_context(rows)
    out = []
    for row in rows:
        zone_meta = _session_zone(row, context)
        session = {
            "id": row.get("id"),
            "started_at": row.get("started_at"),
            "_dt": row.get("_dt"),
            "_utc": row.get("_utc"),
            "source": row.get("source") or "unknown",
            "sport_type": row.get("sport_type") or "other",
            "sport_family": row.get("sport_family") or "endurance",
            "activity_label": row.get("activity_label") or "Cardio",
            "sport_label": row.get("activity_label") or "Cardio",
            "hr_zone": zone_meta["hr_zone"],
            "hr_zone_source": zone_meta["hr_zone_source"],
            "zone_type": zone_meta["hr_zone"],
            "indoor": bool(row.get("indoor")) if row.get("indoor") is not None else None,
            "duration_sec": row.get("duration_sec"),
            "active_minutes": round((row.get("duration_sec") or 0) / 60.0, 1) if row.get("duration_sec") else None,
            "distance_m": row.get("distance_m"),
            "distance_km": round((row.get("distance_m") or 0) / 1000.0, 2) if row.get("distance_m") is not None else None,
            "avg_pace_sec_per_km": row.get("avg_pace_sec_per_km"),
            "pace_sec_per_km": row.get("avg_pace_sec_per_km"),
            "avg_hr_bpm": row.get("avg_hr_bpm"),
            "avg_hr": row.get("avg_hr_bpm"),
            "max_hr_bpm": row.get("max_hr_bpm"),
            "max_hr": row.get("max_hr_bpm"),
            "avg_power_w": row.get("avg_power_w"),
            "avg_power": row.get("avg_power_w"),
            "cadence_rpm": row.get("cadence_rpm"),
            "calories_kcal": row.get("calories_kcal"),
            "calories": row.get("calories_kcal"),
            "stair_floors": row.get("stair_floors"),
            "note": row.get("note") or "",
            "is_editable": str(row.get("source") or "").lower() != "strava" or True,
            "available_metrics": [],
        }
        session.update(_zone_seconds(session["duration_sec"], session.get("hr_zone")))
        if session.get("sport_type") == "run":
            session["display_value"] = session.get("avg_pace_sec_per_km")
            session["display_unit"] = "pace"
        elif session.get("sport_type") == "ergo":
            session["display_value"] = session.get("avg_power_w")
            session["display_unit"] = "watt"
        elif session.get("sport_type") == "stair":
            session["display_value"] = session.get("avg_power_w")
            session["display_unit"] = "watt"
        else:
            session["display_value"] = session.get("active_minutes")
            session["display_unit"] = "min"
        session["available_metrics"] = [
            key
            for key, value in {
                "hr": session.get("avg_hr_bpm") is not None or session.get("max_hr_bpm") is not None,
                "pace": session.get("avg_pace_sec_per_km") is not None,
                "watt": session.get("avg_power_w") is not None,
                "zones": any(session.get(f"z{i}_sec") for i in range(1, 6)),
                "distance": session.get("distance_m") is not None,
            }.items()
            if value
        ]
        out.append(session)
    return out


def _load_all_sessions() -> list[dict]:
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = _load_run_rows(conn)
    finally:
        conn.close()
    return _normalize_sessions(rows)


def _apply_filters(sessions, filters: CardioFilters):
    out = list(sessions)
    if filters.time_range is not None:
        start = datetime.now(timezone.utc) - timedelta(days=int(filters.time_range))
        out = [s for s in out if s.get("_utc") and s["_utc"] >= start]
    sport = {"laufen": "run", "running": "run", "alle": "all", "sonstiges": "other"}.get(filters.sport_type, filters.sport_type)
    if sport != "all":
        out = [s for s in out if s.get("sport_type") == sport]
    if filters.data_mode == "hr":
        out = [s for s in out if "hr" in (s.get("available_metrics") or [])]
    elif filters.data_mode == "pace":
        out = [s for s in out if "pace" in (s.get("available_metrics") or [])]
    elif filters.data_mode in {"watt", "power"}:
        out = [s for s in out if "watt" in (s.get("available_metrics") or [])]
    elif filters.data_mode == "zones":
        out = [s for s in out if "zones" in (s.get("available_metrics") or [])]
    if filters.source != "all":
        out = [s for s in out if str(s.get("source") or "").lower() == filters.source]
    if filters.indoor_mode == "indoor":
        out = [s for s in out if s.get("indoor") is True]
    elif filters.indoor_mode == "outdoor":
        out = [s for s in out if s.get("indoor") is False]
    return sorted(out, key=lambda s: s.get("_utc") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def _sum(values):
    return round(sum(float(v) for v in values if v is not None and math.isfinite(float(v))), 1)


def _mean(values):
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return round(sum(vals) / len(vals), 1) if vals else None


def _weighted_mean(pairs):
    total_w = 0.0
    total = 0.0
    for value, weight in pairs:
        if value is None or weight is None:
            continue
        v = _finite(value)
        w = _finite(weight)
        if v is None or w is None or w <= 0:
            continue
        total += v * w
        total_w += w
    return round(total / total_w, 1) if total_w > 0 else None


def _compare(all_sessions, filters, current_load):
    if filters.time_range is None:
        return {"available": False, "reason": "open_range"}
    now = datetime.now(timezone.utc)
    cur_start = now - timedelta(days=int(filters.time_range))
    prev_start = cur_start - timedelta(days=int(filters.time_range))
    prev_filters = CardioFilters(**{**filters.__dict__, "time_range": None, "limit": 500, "offset": 0})
    prev = [s for s in _apply_filters(all_sessions, prev_filters) if s.get("_utc") and prev_start <= s["_utc"] < cur_start]
    prev_load = _sum(s.get("active_minutes") for s in prev)
    delta = round(current_load - prev_load, 1)
    return {
        "available": bool(prev or current_load),
        "current_active_minutes": current_load,
        "previous_active_minutes": prev_load,
        "delta_active_minutes": delta,
        "delta_percent": round((delta / prev_load) * 100.0, 1) if prev_load else None,
    }


def _weekly(sessions):
    weekly = {}
    for s in sessions:
        dt = s.get("_dt")
        if not dt:
            continue
        day = dt.astimezone(TZ).date()
        week_start = day - timedelta(days=day.weekday())
        iso = week_start.isocalendar()
        key = f"{iso.year}-{iso.week:02d}"
        row = weekly.setdefault(key, {"week": key, "active_minutes": 0.0, "run_minutes": 0.0, "ergo_minutes": 0.0, "other_minutes": 0.0, "distance_km": 0.0, "sessions": 0})
        minutes = float(s.get("active_minutes") or 0)
        row["active_minutes"] += minutes
        if s.get("sport_type") == "run":
            row["run_minutes"] += minutes
        elif s.get("sport_type") in {"ergo", "stair"}:
            row["ergo_minutes"] += minutes
        else:
            row["other_minutes"] += minutes
        row["distance_km"] += float(s.get("distance_km") or 0)
        row["sessions"] += 1
    return [{**v, "active_minutes": round(v["active_minutes"], 1), "run_minutes": round(v["run_minutes"], 1), "ergo_minutes": round(v["ergo_minutes"], 1), "other_minutes": round(v["other_minutes"], 1), "distance_km": round(v["distance_km"], 2)} for _, v in sorted(weekly.items())]


def _hr_zone_distribution(sessions):
    buckets = {k: {"hr_zone": k, "zone_type": k, "active_minutes": 0.0, "sessions": 0} for k in HR_ZONE_ORDER}
    for s in sessions:
        key = s.get("hr_zone")
        if not key:
            continue
        row = buckets.setdefault(key, {"hr_zone": key, "zone_type": key, "active_minutes": 0.0, "sessions": 0})
        row["active_minutes"] += float(s.get("active_minutes") or 0)
        row["sessions"] += 1
    total = sum(b["active_minutes"] for b in buckets.values())
    return [{**b, "active_minutes": round(b["active_minutes"], 1), "share": round(b["active_minutes"] / total, 3) if total else None} for b in buckets.values()]


def _sport_cards(sessions):
    by_sport = defaultdict(list)
    for s in sessions:
        by_sport[s.get("sport_type") or "other"].append(s)
    cards = []
    for sport, rows in by_sport.items():
        distance = _sum(s.get("distance_km") for s in rows)
        card = {
            "sport_type": sport,
            "sport_label": rows[0].get("sport_label") or sport.title(),
            "sessions": len(rows),
            "active_minutes": _sum(s.get("active_minutes") for s in rows),
            "distance_km": round(distance, 2),
            "avg_duration_min": _mean(s.get("active_minutes") for s in rows),
            "avg_hr": _mean(s.get("avg_hr_bpm") for s in rows),
            "avg_power": _mean(s.get("avg_power_w") for s in rows),
            "avg_stair_floors": _mean(s.get("stair_floors") for s in rows),
            "z2_minutes": _sum(s.get("active_minutes") for s in rows if s.get("hr_zone") == "Z2"),
            "avg_pace_sec": _weighted_mean((s.get("avg_pace_sec_per_km"), s.get("distance_km")) for s in rows),
            "best_pace_sec": min([s["avg_pace_sec_per_km"] for s in rows if s.get("avg_pace_sec_per_km")], default=None),
            "best_avg_power": max([s["avg_power_w"] for s in rows if s.get("avg_power_w")], default=None),
            "best_stair_floors": max([s["stair_floors"] for s in rows if s.get("stair_floors")], default=None),
        }
        cards.append(card)
    return sorted(cards, key=lambda c: c["active_minutes"], reverse=True)


def _performance(sessions):
    series = defaultdict(list)
    for s in sorted(sessions, key=lambda r: r.get("_utc") or datetime.min.replace(tzinfo=timezone.utc)):
        sport = s.get("sport_type") or "other"
        if sport not in {"run", "ergo", "stair", "bike", "walk"}:
            continue
        bucket = "ergo" if sport == "bike" else ("stair" if sport == "stair" else sport)
        series[bucket].append(
            {
                "id": s.get("id"),
                "started_at": s.get("started_at"),
                "sport_type": sport,
                "pace_sec_per_km": s.get("avg_pace_sec_per_km"),
                "avg_hr": s.get("avg_hr_bpm"),
                "avg_power": s.get("avg_power_w"),
                "stair_floors": s.get("stair_floors"),
                "distance_km": s.get("distance_km"),
                "active_minutes": s.get("active_minutes"),
            }
        )
    return {"mode": "mixed" if len(series) != 1 else next(iter(series), "mixed"), "series": dict(series)}


def _mode_summary(sessions, sport_type: str) -> dict:
    runs = [s for s in sessions if s.get("sport_type") == "run"]
    ergos = [s for s in sessions if s.get("sport_type") in {"ergo", "stair"}]
    return {
        "run_count": len(runs),
        "ergo_count": len(ergos),
        "avg_pace_sec": _weighted_mean((s.get("avg_pace_sec_per_km"), s.get("distance_km")) for s in runs),
        "best_pace_sec": min([s["avg_pace_sec_per_km"] for s in runs if s.get("avg_pace_sec_per_km")], default=None),
        "run_avg_hr": _mean(s.get("avg_hr_bpm") for s in runs),
        "avg_power": _mean(s.get("avg_power_w") for s in ergos),
        "best_avg_power": max([s["avg_power_w"] for s in ergos if s.get("avg_power_w")], default=None),
        "ergo_avg_hr": _mean(s.get("avg_hr_bpm") for s in ergos),
    }


def _public_session(session):
    return {k: v for k, v in session.items() if not k.startswith("_")}


def build_cardio_viewmodel(filters: CardioFilters) -> dict:
    all_sessions = _load_all_sessions()
    sessions = _apply_filters(all_sessions, filters)
    range_days = filters.time_range if filters.time_range is not None else _range_days(sessions)
    weeks = max(1.0, float(range_days or 1) / 7.0)
    active_total = _sum(s.get("active_minutes") for s in sessions)
    distance_total = _sum(s.get("distance_km") for s in sessions)
    longest_sec = max([s.get("duration_sec") or 0 for s in sessions], default=0) or None
    z2_minutes = _sum(s.get("active_minutes") for s in sessions if s.get("hr_zone") == "Z2")
    active_days = len({s["_dt"].astimezone(TZ).date() for s in sessions if s.get("_dt")})
    summary = {
        "total_active_minutes": active_total,
        "run_minutes": _sum(s.get("active_minutes") for s in sessions if s.get("sport_type") == "run"),
        "ergo_minutes": _sum(s.get("active_minutes") for s in sessions if s.get("sport_type") in {"ergo", "stair"}),
        "total_distance_km": round(distance_total, 2),
        "total_sessions": len(sessions),
        "sessions": len(sessions),
        "sessions_per_week": round(len(sessions) / weeks, 1),
        "minutes_per_week": round(active_total / weeks, 1),
        "avg_duration_sec": round(_mean(s.get("duration_sec") for s in sessions) or 0, 1) if sessions else None,
        "avg_duration_min": _mean(s.get("active_minutes") for s in sessions),
        "longest_session_sec": longest_sec,
        "longest_session_min": round(longest_sec / 60.0, 1) if longest_sec else None,
        "z2_total_minutes": z2_minutes,
        "z2_share": round((z2_minutes / float(active_total)), 3) if active_total else None,
        "active_days": active_days,
        "last_session": _public_session(sessions[0]) if sessions else None,
        "previous_period_delta": _compare(all_sessions, filters, active_total),
        "compare_previous": _compare(all_sessions, filters, active_total),
        "data_sources": sorted({s.get("source") for s in all_sessions if s.get("source")}),
        "missing_domains": [s for s in ["ergo", "stair", "walk", "bike"] if not any(x.get("sport_type") == s for x in all_sessions)],
        "hr_zone_policy": "avg_hr thresholds first; run pace fallback when pulse is missing",
    }
    summary.update(_mode_summary(sessions, filters.sport_type))
    table_rows = [_public_session(s) for s in sessions[filters.offset : filters.offset + filters.limit]]
    return {
        "ok": True,
        "filters": filters.__dict__,
        "summary": summary,
        "latest_session": summary["last_session"],
        "weekly_series": _weekly(sessions),
        "weekly": _weekly(sessions),
        "hr_zone_distribution": _hr_zone_distribution(sessions),
        "zones": _hr_zone_distribution(sessions),
        "sport_mix": _sport_cards(sessions),
        "sport_detail_cards": _sport_cards(sessions),
        "sport_summaries": _sport_cards(sessions),
        "performance_tabs": _performance(sessions),
        "performance": _performance(sessions),
        "table_rows": table_rows,
        "list": {"total": len(sessions), "rows": table_rows, "limit": filters.limit, "offset": filters.offset},
        "data_note": "Cardio view is optimized for Laufen and Ergo; legacy sports are only included in Alle.",
    }


def _range_days(sessions):
    if not sessions:
        return 0
    dates = [s["_utc"].date() for s in sessions if s.get("_utc")]
    if not dates:
        return 0
    return max(1, (max(dates) - min(dates)).days + 1)


def _parse_positive_float(value):
    n = _finite(str(value).replace(",", ".") if value is not None else value)
    return n if n is not None and n >= 0 else None


def _pace_to_seconds(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            return int(parts[0]) * 60 + float(parts[1])
        except Exception:
            return None
    n = _finite(text)
    if n is None:
        return None
    return n * 60 if n < 30 else n


def _duration_to_seconds(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [float(p.replace(",", ".")) for p in parts]
        except Exception:
            return None
        if len(nums) == 2:
            return int(round(nums[0] * 60 + nums[1]))
        if len(nums) == 3:
            return int(round(nums[0] * 3600 + nums[1] * 60 + nums[2]))
        return None
    n = _finite(text.replace(",", "."))
    return int(round(n * 60)) if n is not None else None


def _bodyweight_kg_for_date(started: datetime) -> float | None:
    day_iso = started.astimezone(TZ).date().isoformat()
    conn = get_nutrition_db()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT weight_kg
            FROM weight_logs
            WHERE date(date_iso) <= date(?)
              AND weight_kg IS NOT NULL
            ORDER BY date(date_iso) DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    return _finite(row["weight_kg"]) if row else None


def save_cardio_session(payload: dict, session_id: int | None = None) -> dict:
    sport = str(payload.get("sport_type") or payload.get("sport") or "run").strip().lower()
    if sport not in {"run", "ergo", "stair"}:
        sport = "run"
    date_text = str(payload.get("date") or payload.get("started_at") or "").strip()
    if len(date_text) == 10:
        date_text = f"{date_text}T12:00:00+02:00"
    started = _parse_ts(date_text) or datetime.now(TZ)
    moving_time = _duration_to_seconds(payload.get("duration_min") or payload.get("duration"))
    distance_km = _parse_positive_float(payload.get("distance_km"))
    pace_sec = _pace_to_seconds(payload.get("pace") or payload.get("avg_pace_sec_per_km"))
    avg_power = _parse_positive_float(payload.get("avg_power_w") or payload.get("avg_power"))
    stair_floors = _parse_positive_float(payload.get("stair_floors") or payload.get("floors"))
    if sport not in {"ergo", "stair"}:
        avg_power = None
    if distance_km is None and sport == "run" and moving_time and pace_sec:
        distance_km = moving_time / pace_sec
    if distance_km is None and sport == "ergo" and moving_time and avg_power is not None:
        distance_km = (moving_time / 60.0) * avg_power * ERGO_DISTANCE_KM_PER_MIN_WATT
    if distance_km is None and sport == "stair" and stair_floors is not None:
        distance_km = stair_floors / STAIR_FLOORS_PER_KM
    if stair_floors is None and sport == "stair" and distance_km is not None:
        stair_floors = distance_km * STAIR_FLOORS_PER_KM
    if avg_power is None and sport == "stair" and moving_time and stair_floors is not None and moving_time > 0:
        bodyweight_kg = _bodyweight_kg_for_date(started)
        if bodyweight_kg is not None and bodyweight_kg > 0:
            vertical_m = stair_floors * STAIR_VERTICAL_METERS_PER_FLOOR
            avg_power = (bodyweight_kg * GRAVITY_M_S2 * vertical_m) / moving_time
    distance_m = distance_km * 1000.0 if distance_km is not None else None
    if sport == "run" and pace_sec is None and moving_time and distance_km:
        pace_sec = moving_time / distance_km
    avg_hr = _parse_positive_float(payload.get("avg_hr_bpm") or payload.get("avg_hr"))
    note = str(payload.get("note") or "").strip()
    conn = get_runs_db()
    try:
        conn.row_factory = sqlite3.Row
        _ensure_cardio_columns(conn)
        if session_id:
            conn.execute(
                """
                UPDATE runs
                SET date=?, distance=?, moving_time=?, pace=?, avg_hr=?, sport_type=?, avg_power=?, stair_floors=?, note=?
                WHERE id=?
                """,
                (started.isoformat(), distance_m, moving_time, pace_sec, avg_hr, sport, avg_power, stair_floors, note, session_id),
            )
            row_id = session_id
        else:
            cur = conn.execute(
                """
                INSERT INTO runs (date, distance, moving_time, pace, avg_hr, sport_type, avg_power, stair_floors, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (started.isoformat(), distance_m, moving_time, pace_sec, avg_hr, sport, avg_power, stair_floors, note),
            )
            row_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": row_id}


def delete_cardio_session(session_id: int) -> dict:
    conn = get_runs_db()
    try:
        conn.execute("DELETE FROM runs WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
