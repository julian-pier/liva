from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from app_support.weight_trend import build_weight_trend
from database.connections import (
    get_hrv_db,
    get_nutrition_db,
    get_plans_db,
    get_runs_db,
    get_training_db,
)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 60.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M")


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _date_only(value: str | None) -> date | None:
    dt = _from_iso(value)
    return dt.date() if dt else None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


@dataclass
class SafeDb:
    conn: sqlite3.Connection | None

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.close()
        except Exception:
            pass


def _open(getter) -> SafeDb:
    try:
        return SafeDb(getter())
    except Exception:
        return SafeDb(None)


def _fetchall(conn: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, params)
        return list(cur.fetchall())
    except Exception:
        return []


def _fetchone(conn: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    if conn is None:
        return None
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone()
    except Exception:
        return None




def _bucket_trend(delta: float | None, threshold: float) -> str:
    if delta is None or not math.isfinite(delta):
        return "missing"
    if delta <= -threshold:
        return "down"
    if delta >= threshold:
        return "up"
    return "flat"


def _latest_recovery(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "hrv_measurements"):
        return {
            "bucket": "missing",
            "summary": "Recovery unklar",
            "signal": "keine klare Recovery-Lage",
            "quality": "unbekannt",
        }
    rows = _fetchall(
        conn,
        """
        SELECT ts_measurement, date_utc, rmssd, hr, signal_quality
        FROM hrv_measurements
        ORDER BY COALESCE(date_utc, ts_measurement) DESC
        LIMIT 35
        """,
    )
    clean = []
    for row in rows:
        d = _date_only(row["date_utc"] or row["ts_measurement"])
        if not d:
            continue
        try:
            rmssd = float(row["rmssd"]) if row["rmssd"] is not None else None
        except Exception:
            rmssd = None
        try:
            hr = float(row["hr"]) if row["hr"] is not None else None
        except Exception:
            hr = None
        clean.append({
            "date": d,
            "rmssd": rmssd,
            "hr": hr,
            "quality": str(row["signal_quality"] or "").strip().lower() or "mittel",
        })
    if not clean:
        return {
            "bucket": "missing",
            "summary": "Recovery unklar",
            "signal": "keine klare Recovery-Lage",
            "quality": "unbekannt",
        }
    today = clean[0]
    refs = [r for r in clean[1:29] if r["rmssd"] is not None and r["hr"] is not None]
    if len(refs) < 5 or today["rmssd"] is None or today["hr"] is None:
        return {
            "bucket": "missing",
            "summary": "Recovery teils vorhanden",
            "signal": "zu wenig Vergleich",
            "quality": today["quality"],
        }
    rmssd_med = median(r["rmssd"] for r in refs)
    hr_med = median(r["hr"] for r in refs)
    delta_rmssd = today["rmssd"] - rmssd_med
    delta_hr = today["hr"] - hr_med
    score = 0
    if delta_rmssd <= -8:
        score -= 1
    elif delta_rmssd >= 8:
        score += 1
    if delta_hr >= 3:
        score -= 1
    elif delta_hr <= -2:
        score += 1
    bucket = "ok"
    if score <= -1:
        bucket = "low"
    elif score >= 1:
        bucket = "high"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Recovery wirkt gedrückt",
            "ok": "Recovery wirkt stabil",
            "high": "Recovery wirkt stark",
        }[bucket],
        "signal": f"RMSSD {int(round(today['rmssd']))} ms, Puls {int(round(today['hr']))}",
        "quality": today["quality"],
    }


def _strength_trend(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "workouts"):
        return {"bucket": "missing", "summary": "Krafttrend unklar", "signal": "keine Gym-Historie"}
    rows = _fetchall(
        conn,
        """
        SELECT date_iso, COUNT(*) AS workout_count
        FROM workouts
        WHERE date_iso IS NOT NULL
        GROUP BY date_iso
        ORDER BY date_iso DESC
        LIMIT 28
        """,
    )
    if not rows:
        return {"bucket": "missing", "summary": "Krafttrend unklar", "signal": "keine Gym-Historie"}
    recent = 0
    prev = 0
    for row in rows:
        d = _date_only(row["date_iso"])
        if not d:
            continue
        days = (now.date() - d).days
        if 0 <= days <= 13:
            recent += int(row["workout_count"] or 0)
        elif 14 <= days <= 27:
            prev += int(row["workout_count"] or 0)
    bucket = _bucket_trend(float(recent - prev), 1.0)
    return {
        "bucket": bucket,
        "summary": {
            "down": "Krafttrend eher rückläufig",
            "flat": "Krafttrend wirkt stabil",
            "up": "Krafttrend zieht an",
            "missing": "Krafttrend unklar",
        }[bucket],
        "signal": f"Gym: {recent} Einheiten / 14 Tage",
    }


def _run_consistency(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "runs"):
        return {"bucket": "missing", "summary": "Lauf-Rhythmus unklar", "signal": "keine Laufdaten"}
    rows = _fetchall(conn, "SELECT date, distance FROM runs ORDER BY date DESC LIMIT 40")
    if not rows:
        return {"bucket": "missing", "summary": "Lauf-Rhythmus unklar", "signal": "keine Laufdaten"}
    run_count = 0
    km = 0.0
    for row in rows:
        d = _date_only(row["date"])
        if not d:
            continue
        days = (now.date() - d).days
        if 0 <= days <= 13:
            run_count += 1
            try:
                km += float(row["distance"] or 0.0) / 1000.0
            except Exception:
                pass
    if run_count == 0:
        bucket = "low"
    elif run_count >= 3:
        bucket = "high"
    else:
        bucket = "ok"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Lauf-Rhythmus ist dünn",
            "ok": "Lauf-Rhythmus ist brauchbar",
            "high": "Lauf-Rhythmus ist stabil",
        }[bucket],
        "signal": f"Läufe: {run_count} in 14 Tagen · {km:.1f} km",
    }


def _nutrition_adherence(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"bucket": "missing", "summary": "Ernährung unklar", "signal": "keine Ernährungsdaten"}
    table = None
    for candidate in ("nutrition_day_actuals", "nutrition_daily"):
        if _table_exists(conn, candidate):
            table = candidate
            break
    if not table:
        return {"bucket": "missing", "summary": "Ernährung unklar", "signal": "keine Ernährungsdaten"}
    order_col = "day" if table == "nutrition_day_actuals" else "date_iso"
    rows = _fetchall(conn, f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 14")
    if not rows:
        return {"bucket": "missing", "summary": "Ernährung unklar", "signal": "keine Ernährungsdaten"}
    tracked = 0
    for row in rows:
        raw_day = row["day"] if "day" in row.keys() else row["date_iso"]
        d = _date_only(raw_day)
        if not d:
            continue
        if 0 <= (now.date() - d).days <= 6:
            tracked += 1
    if tracked <= 2:
        bucket = "low"
    elif tracked >= 6:
        bucket = "high"
    else:
        bucket = "ok"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Ernährung war lückenhaft",
            "ok": "Ernährung ist halbwegs stabil",
            "high": "Ernährung ist sauber erfasst",
        }[bucket],
        "signal": f"Tracking: {tracked}/7 Tage",
    }


def _weight_trend(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "weight_logs"):
        return {"bucket": "missing", "summary": "Gewichtstrend unklar", "signal": "keine Gewichtslogs"}
    rows = _fetchall(
        conn,
        "SELECT id, date_iso AS date, weight_kg AS weight FROM weight_logs WHERE date_iso IS NOT NULL ORDER BY date_iso DESC, id DESC LIMIT 1000",
    )
    trend = build_weight_trend(rows, now.date())
    calendar_week = trend["calendar_week"]
    delta = calendar_week["delta_kg"]
    if delta is None or not calendar_week["calorie_adjustment_eligible"]:
        return {"bucket": "missing", "summary": "Gewichtstrend vorläufig", "signal": f"KW {calendar_week['current']['iso_week']}: {calendar_week['current']['sample_count']}/7; keine Kalorienanpassung"}
    bucket = _bucket_trend(delta, 0.5)
    direction = "hoch" if delta > 0 else "runter" if delta < 0 else "neutral"
    return {
        "bucket": bucket,
        "summary": {
            "down": "Gewicht driftet runter",
            "flat": "Gewicht ist stabil",
            "up": "Gewicht driftet rauf",
            "missing": "Gewichtstrend unklar",
        }[bucket],
        "signal": f"KW {calendar_week['current']['iso_week']}: {delta:+.1f} kg vs. Vorwoche ({calendar_week['current']['sample_count']}/7, {direction})",
    }


def _session_gap(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "workouts"):
        return {"bucket": "missing", "summary": "Gym-Abstand unklar", "signal": "keine Gym-Historie"}
    row = _fetchone(conn, "SELECT date_iso FROM workouts WHERE date_iso IS NOT NULL ORDER BY date_iso DESC LIMIT 1")
    if not row:
        return {"bucket": "missing", "summary": "Gym-Abstand unklar", "signal": "keine Gym-Historie"}
    d = _date_only(row["date_iso"])
    if not d:
        return {"bucket": "missing", "summary": "Gym-Abstand unklar", "signal": "keine Gym-Historie"}
    gap = (now.date() - d).days
    if gap <= 2:
        bucket = "short"
    elif gap <= 5:
        bucket = "normal"
    else:
        bucket = "long"
    return {
        "bucket": bucket,
        "summary": {
            "short": "Gym zuletzt frisch",
            "normal": "Gym-Abstand normal",
            "long": "Gym-Pause länger",
            "missing": "Gym-Abstand unklar",
        }[bucket],
        "signal": f"Letztes Gym: vor {gap} Tagen",
    }


def _proximity_to_failure(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "sets") or not _table_exists(conn, "workouts"):
        return {"bucket": "missing", "summary": "RPE-Nähe unklar", "signal": "keine Satzdaten"}
    rows = _fetchall(
        conn,
        """
        SELECT s.rpe, w.date_iso
        FROM sets s
        JOIN workouts w ON w.id = s.workout_id
        WHERE s.rpe IS NOT NULL AND w.date_iso IS NOT NULL
        ORDER BY w.date_iso DESC, s.id DESC
        LIMIT 80
        """,
    )
    recent_rpes = []
    for row in rows:
        d = _date_only(row["date_iso"])
        if not d or not (0 <= (now.date() - d).days <= 28):
            continue
        try:
            recent_rpes.append(float(row["rpe"]))
        except Exception:
            continue
    if len(recent_rpes) < 4:
        return {"bucket": "missing", "summary": "RPE-Nähe unklar", "signal": "zu wenig RPE-Daten"}
    top = sorted(recent_rpes, reverse=True)[: min(8, len(recent_rpes))]
    avg_top = sum(top) / len(top)
    if avg_top >= 9.0:
        bucket = "high"
    elif avg_top >= 7.5:
        bucket = "ok"
    else:
        bucket = "low"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Top-Sätze zuletzt mit viel Puffer",
            "ok": "Top-Sätze zuletzt moderat",
            "high": "Top-Sätze zuletzt nah am Limit",
            "missing": "RPE-Nähe unklar",
        }[bucket],
        "signal": f"Ø hohe RPE: {avg_top:.1f}",
    }


def _gym_frequency(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "workouts"):
        return {"bucket": "missing", "summary": "Gym-Frequenz unklar", "signal": "keine Gym-Daten"}
    rows = _fetchall(
        conn,
        """
        SELECT date_iso, COUNT(*) AS c
        FROM workouts
        WHERE date_iso IS NOT NULL
        GROUP BY date_iso
        ORDER BY date_iso DESC
        LIMIT 28
        """,
    )
    count = 0
    for row in rows:
        d = _date_only(row["date_iso"])
        if not d:
            continue
        if 0 <= (now.date() - d).days <= 13:
            count += int(row["c"] or 0)
    if count <= 2:
        bucket = "low"
    elif count >= 5:
        bucket = "high"
    else:
        bucket = "ok"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Gym-Frequenz zuletzt niedrig",
            "ok": "Gym-Frequenz zuletzt okay",
            "high": "Gym-Frequenz zuletzt hoch",
            "missing": "Gym-Frequenz unklar",
        }[bucket],
        "signal": f"Gym: {count} Einheiten in 14 Tagen",
    }


def _run_load(now: datetime, conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None or not _table_exists(conn, "runs"):
        return {"bucket": "missing", "summary": "Laufbelastung unklar", "signal": "keine Laufdaten"}
    rows = _fetchall(conn, "SELECT date, date_iso, km, distance, minutes FROM runs ORDER BY COALESCE(date_iso, date) DESC LIMIT 40")
    km_7d = 0.0
    min_7d = 0.0
    count = 0
    for row in rows:
        raw_date = row["date_iso"] if "date_iso" in row.keys() and row["date_iso"] else row["date"]
        d = _date_only(raw_date)
        if not d or not (0 <= (now.date() - d).days <= 6):
            continue
        count += 1
        try:
            if "km" in row.keys() and row["km"] is not None:
                km_7d += float(row["km"])
            elif "distance" in row.keys() and row["distance"] is not None:
                km_7d += float(row["distance"]) / 1000.0
        except Exception:
            pass
        try:
            if "minutes" in row.keys() and row["minutes"] is not None:
                min_7d += float(row["minutes"])
        except Exception:
            pass
    if count == 0:
        bucket = "low"
    elif km_7d >= 25 or min_7d >= 150:
        bucket = "high"
    else:
        bucket = "ok"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Laufbelastung zuletzt niedrig",
            "ok": "Laufbelastung zuletzt okay",
            "high": "Laufbelastung zuletzt hoch",
            "missing": "Laufbelastung unklar",
        }[bucket],
        "signal": f"Lauf: {count} Einheiten · {km_7d:.1f} km · {min_7d:.0f} min",
    }


def _warning_load(recovery: dict[str, Any], strength: dict[str, Any], failure: dict[str, Any], nutrition: dict[str, Any]) -> dict[str, Any]:
    score = 0
    if recovery.get("bucket") == "low":
        score += 1
    if strength.get("bucket") == "down":
        score += 1
    if failure.get("bucket") == "high":
        score += 1
    if nutrition.get("bucket") == "low":
        score += 1
    if score >= 3:
        bucket = "high"
    elif score >= 2:
        bucket = "ok"
    else:
        bucket = "low"
    return {
        "bucket": bucket,
        "summary": {
            "low": "Warnlage eher ruhig",
            "ok": "Mehrere Warnsignale sichtbar",
            "high": "Warnlage deutlich verdichtet",
            "missing": "Warnlage unklar",
        }[bucket],
        "signal": f"Warnsignale: {score}",
    }


def _plan_shape(conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"days": 0, "titles": [], "source": "missing"}
    row = _fetchone(conn, "SELECT plan_json, title, focus FROM gym_plans WHERE is_active=1 ORDER BY updated_at DESC LIMIT 1")
    if row and row["plan_json"]:
        try:
            plan = json.loads(row["plan_json"])
        except Exception:
            plan = {}
        base_week = plan.get("base_week") if isinstance(plan, dict) else {}
        titles = []
        if isinstance(base_week, dict):
            for day in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"):
                for event in base_week.get(day) or []:
                    title = str(event.get("title") or "").strip()
                    if title:
                        titles.append(title)
        return {
            "days": len(titles),
            "titles": titles[:8],
            "source": str(row["title"] or "Plan"),
            "focus": str(row["focus"] or ""),
        }
    row = _fetchone(conn, "SELECT data, name FROM plans WHERE is_active=1 ORDER BY updated_at DESC LIMIT 1")
    if row and row["data"]:
        try:
            plan = json.loads(row["data"])
        except Exception:
            plan = {}
        titles = []
        for day in plan.get("base_week") or []:
            title = str(day.get("session_name") or "").strip()
            if title:
                titles.append(title)
        return {
            "days": len(titles),
            "titles": titles[:8],
            "source": str(row["name"] or "Plan"),
            "focus": "",
        }
    return {"days": 0, "titles": [], "source": "missing"}


def _build_features(now: datetime) -> dict[str, Any]:
    training = _open(get_training_db)
    hrv = _open(get_hrv_db)
    runs = _open(get_runs_db)
    nutrition = _open(get_nutrition_db)
    plans = _open(get_plans_db)
    try:
        recovery = _latest_recovery(now, hrv.conn)
        strength = _strength_trend(now, training.conn)
        run_consistency = _run_consistency(now, runs.conn)
        nutrition_adherence = _nutrition_adherence(now, nutrition.conn)
        weight = _weight_trend(now, nutrition.conn)
        session_gap = _session_gap(now, training.conn)
        failure = _proximity_to_failure(now, training.conn)
        gym_frequency = _gym_frequency(now, training.conn)
        run_load = _run_load(now, runs.conn)
        warnings = _warning_load(recovery, strength, failure, nutrition_adherence)
        plan = _plan_shape(plans.conn)
        features = {
            "recovery_state": recovery["bucket"],
            "strength_trend": strength["bucket"],
            "run_consistency": run_consistency["bucket"],
            "nutrition_adherence": nutrition_adherence["bucket"],
            "weight_trend": weight["bucket"],
            "session_gap": session_gap["bucket"],
            "proximity_to_failure": failure["bucket"],
            "gym_frequency": gym_frequency["bucket"],
            "run_load": run_load["bucket"],
            "warning_load": warnings["bucket"],
        }
        summaries = {
            "recovery": recovery,
            "strength": strength,
            "runs": run_consistency,
            "nutrition": nutrition_adherence,
            "weight": weight,
            "gap": session_gap,
            "failure": failure,
            "gym_frequency": gym_frequency,
            "run_load": run_load,
            "warnings": warnings,
            "plan": plan,
        }
        return {"features": features, "summaries": summaries, "generated_at": now.isoformat()}
    finally:
        training.close()
        hrv.close()
        runs.close()
        nutrition.close()
        plans.close()


def load_latest_features(now: datetime | None = None) -> dict[str, Any]:
    now = now or _utc_now()
    key = _cache_key(now)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] >= datetime.now(timezone.utc).timestamp():
            return cached[1]
    payload = _build_features(now)
    with _CACHE_LOCK:
        _CACHE[key] = (datetime.now(timezone.utc).timestamp() + _CACHE_TTL_S, payload)
    return payload


__all__ = ["load_latest_features"]
