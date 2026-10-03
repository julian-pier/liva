from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import mean, pstdev
from zoneinfo import ZoneInfo
import os
import re
import sqlite3
from typing import Any


TZ_BERLIN = ZoneInfo("Europe/Berlin")

# Trend-Radar tuning constants.
# Adjust thresholds here to make flags stricter or looser.
THRESHOLDS = {
    "infekt": {"rmssd": -0.10, "rhr": 0.05},
    "cut_fatigue": {"weight": -0.005, "kcal": -0.08, "rmssd": -0.08, "rhr": 0.03},
    "water_stress": {"weight": 0.005, "kcal": -0.05},
    "load_spike": {"volume": 0.20},
    "underload": {"volume": -0.20},
    "cardio_plus_gym": {"cardio": 0.20, "volume": 0.15, "rmssd": -0.08},
}

# Per-metric safety config for baseline validity and default chart visibility.
METRIC_CONFIG: dict[str, dict[str, Any]] = {
    "weight": {"unit": "kg", "min_baseline": 30.0, "min_points": 7, "chart_default": True},
    "rmssd": {"unit": "ms", "min_baseline": 20.0, "min_points": 7, "chart_default": True},
    "rhr": {"unit": "bpm", "min_baseline": 30.0, "min_points": 7, "chart_default": True},
    "kcal": {"unit": "kcal", "min_baseline": 800.0, "min_points": 7, "chart_default": True},
    "volume": {"unit": "ton", "min_baseline": 200.0, "min_points": 5, "chart_default": True},
    "cardio": {"unit": "min", "min_baseline": 10.0, "min_points": 5, "chart_default": False},
}

# Metrics used by Trend Radar:
# - weight (kg), rmssd (ms), rhr (bpm), volume (tonnage/hard-set fallback), kcal, cardio minutes.
METRICS_ORDER = ["weight", "rmssd", "rhr", "volume", "kcal", "cardio"]
METRIC_META = {
    "weight": {"label": "Gewicht", "unit": "kg"},
    "rmssd": {"label": "RMSSD", "unit": "ms"},
    "rhr": {"label": "RHR", "unit": "bpm"},
    "volume": {"label": "Volumen", "unit": "Tonnage"},
    "kcal": {"label": "Kalorien", "unit": "kcal"},
    "cardio": {"label": "Cardio", "unit": "min"},
}


def _debug_enabled() -> bool:
    return (os.getenv("DEBUG_RADAR") or "0").strip().lower() in {"1", "true", "yes", "on"}


def _dbg(*parts: Any) -> None:
    if _debug_enabled():
        print("[trend_radar]", *parts)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        out = float(v)
    except Exception:
        return None
    if out != out:  # nan
        return None
    return out


def _round_or_none(v: float | None, digits: int = 2) -> float | None:
    if v is None:
        return None
    return round(float(v), digits)


def _safe_mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(mean(vals))


def _safe_std(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return float(pstdev(vals))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _rolling_mean_nullsafe(values: list[float | None], window: int = 7, min_points: int = 3) -> list[float | None]:
    out: list[float | None] = []
    w = max(1, int(window))
    need = max(1, int(min_points))
    for i in range(len(values)):
        s = max(0, i - w + 1)
        chunk = [v for v in values[s : i + 1] if v is not None]
        if len(chunk) < need:
            out.append(None)
        else:
            out.append(_safe_mean(chunk))
    return out


def _coerce_iso_day(raw: Any) -> str | None:
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    if len(txt) >= 10:
        maybe = txt[:10]
        try:
            date.fromisoformat(maybe)
            return maybe
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(txt)
        return dt.date().isoformat()
    except Exception:
        return None


def _date_range(end_date: date, days: int) -> list[str]:
    days_n = max(1, int(days))
    start = end_date - timedelta(days=days_n - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(days_n)]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    out: set[str] = set()
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name = row[1] if isinstance(row, tuple) else row["name"]
        if name:
            out.add(str(name))
    return out


def _first_existing(cols: set[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def _parse_hrv_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    txt = txt.replace("Z", "+00:00")
    # Normalize offsets like +0000 to +00:00
    txt = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", txt)
    try:
        dt = datetime.fromisoformat(txt)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pick_best_hrv_rows(
    rows: list[sqlite3.Row],
) -> tuple[dict[str, float], dict[str, float]]:
    quality_rank = {
        "excellent": 5,
        "very good": 4,
        "good": 3,
        "ok": 2,
        "fair": 1,
    }
    per_day: dict[str, tuple[int, datetime, float | None, float | None]] = {}

    for r in rows:
        ts = _parse_hrv_ts(r["ts_measurement"]) or _parse_hrv_ts(r["date_utc"])
        if ts is None:
            continue
        day_iso = ts.astimezone(TZ_BERLIN).date().isoformat()
        q = str(r["signal_quality"] or "").strip().lower()
        rank = quality_rank.get(q, 0)
        rmssd = _to_float(r["rmssd"])
        hr = _to_float(r["hr"])
        prev = per_day.get(day_iso)
        if prev is None or (rank, ts) > (prev[0], prev[1]):
            per_day[day_iso] = (rank, ts, rmssd, hr)

    rmssd_by_day: dict[str, float] = {}
    hr_by_day: dict[str, float] = {}
    for day_iso, (_, _, rmssd, hr) in per_day.items():
        if rmssd is not None:
            rmssd_by_day[day_iso] = rmssd
        if hr is not None:
            hr_by_day[day_iso] = hr
    return rmssd_by_day, hr_by_day


def _load_weight_series(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, float]:
    for table in ["weight_logs", "bodyweight", "measurements", "weight_daily"]:
        cols = _table_columns(conn, table)
        if not cols:
            continue
        date_col = _first_existing(cols, ["date_iso", "date", "day"])
        value_col = _first_existing(cols, ["weight_kg", "weight", "bodyweight_kg"])
        if not date_col or not value_col:
            continue
        id_col = "id" if "id" in cols else None
        created_col = "created_at" if "created_at" in cols else None

        select_tail = []
        if created_col:
            select_tail.append(f"{created_col} AS created_at")
        if id_col:
            select_tail.append(f"{id_col} AS rid")
        tail = ", " + ", ".join(select_tail) if select_tail else ""

        rows = conn.execute(
            f"""
            SELECT {date_col} AS d, {value_col} AS v{tail}
            FROM {table}
            WHERE {date_col} IS NOT NULL
              AND SUBSTR({date_col}, 1, 10) >= ?
              AND SUBSTR({date_col}, 1, 10) <= ?
            ORDER BY SUBSTR({date_col}, 1, 10) ASC
            """,
            (start_iso, end_iso),
        ).fetchall()

        by_day: dict[str, tuple[str, int, float]] = {}
        for row in rows:
            day_iso = _coerce_iso_day(row["d"])
            if not day_iso:
                continue
            v = _to_float(row["v"])
            if v is None:
                continue
            created = str(row["created_at"]) if "created_at" in row.keys() and row["created_at"] is not None else ""
            rid = int(row["rid"]) if "rid" in row.keys() and row["rid"] is not None else 0
            prev = by_day.get(day_iso)
            key = (created, rid)
            if prev is None or key >= (prev[0], prev[1]):
                by_day[day_iso] = (created, rid, v)

        if by_day:
            return {k: v[2] for k, v in by_day.items()}
        return {}
    return {}


def _load_actual_kcal_series(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, float]:
    out: dict[str, float] = {}

    # Source 1: dedicated day actuals table
    cols = _table_columns(conn, "nutrition_day_actuals")
    if cols:
        date_col = _first_existing(cols, ["day", "date_iso", "date"])
        kcal_col = _first_existing(cols, ["kcal", "calories"])
        if date_col and kcal_col:
            rows = conn.execute(
                f"""
                SELECT {date_col} AS d, {kcal_col} AS kcal
                FROM nutrition_day_actuals
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) >= ?
                  AND SUBSTR({date_col}, 1, 10) <= ?
                ORDER BY SUBSTR({date_col}, 1, 10) ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            for r in rows:
                d = _coerce_iso_day(r["d"])
                kcal = _to_float(r["kcal"])
                if d and kcal is not None and d not in out:
                    out[d] = kcal

    # Source 2: weight logs (legacy but often primary in this repo)
    cols = _table_columns(conn, "weight_logs")
    if cols and "kcal" in cols:
        date_col = _first_existing(cols, ["date_iso", "date"])
        if date_col:
            rows = conn.execute(
                f"""
                SELECT {date_col} AS d, kcal
                FROM weight_logs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) >= ?
                  AND SUBSTR({date_col}, 1, 10) <= ?
                ORDER BY SUBSTR({date_col}, 1, 10) ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            for r in rows:
                d = _coerce_iso_day(r["d"])
                kcal = _to_float(r["kcal"])
                if d and kcal is not None and d not in out:
                    out[d] = kcal

    return out


def _mode_target(settings: dict[str, str], mode: str | None) -> float | None:
    if not mode:
        return None
    key_candidates = [
        f"mode_{mode}_target",
        f"target_{mode}",
    ]
    for key in key_candidates:
        raw = settings.get(key)
        v = _to_float(raw)
        if v is not None:
            return v
    return None


def _load_planned_kcal_series(conn: sqlite3.Connection, days: list[str]) -> dict[str, float | None]:
    settings_rows = conn.execute("SELECT key, value FROM nutrition_settings").fetchall() if _table_exists(conn, "nutrition_settings") else []
    settings: dict[str, str] = {}
    for r in settings_rows:
        settings[str(r["key"])] = "" if r["value"] is None else str(r["value"])

    active_mode = settings.get("active_mode") or settings.get("selected_mode")

    timeline: list[dict[str, Any]] = []
    if _table_exists(conn, "nutrition_mode_timeline"):
        rows = conn.execute(
            """
            SELECT start_date, end_date, mode, target_kcal
            FROM nutrition_mode_timeline
            ORDER BY start_date ASC
            """
        ).fetchall()
        for r in rows:
            s = _coerce_iso_day(r["start_date"])
            e = _coerce_iso_day(r["end_date"]) if r["end_date"] is not None else None
            if not s:
                continue
            timeline.append(
                {
                    "start": s,
                    "end": e,
                    "mode": (str(r["mode"]) if r["mode"] is not None else None),
                    "target": _to_float(r["target_kcal"]),
                }
            )

    planned: dict[str, float | None] = {}
    for d in days:
        match = None
        for entry in timeline:
            if d >= entry["start"] and (entry["end"] is None or d <= entry["end"]):
                match = entry
        if match is None:
            target = _mode_target(settings, active_mode)
            planned[d] = target
            continue

        target = match["target"]
        if target is None:
            target = _mode_target(settings, match.get("mode"))
        if target is None:
            target = _mode_target(settings, active_mode)
        planned[d] = target

    return planned


def _load_volume_series(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, float]:
    if not _table_exists(conn, "workouts"):
        return {}

    workout_cols = _table_columns(conn, "workouts")
    date_col = _first_existing(workout_cols, ["date_iso", "date"])
    if not date_col:
        return {}

    has_sets = _table_exists(conn, "sets")
    has_exercises = _table_exists(conn, "exercises")

    if has_sets and has_exercises:
        rows = conn.execute(
            f"""
            SELECT
                w.{date_col} AS date_iso,
                SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN (s.weight * s.reps) END) AS tonnage,
                SUM(CASE WHEN s.reps IS NOT NULL AND s.reps > 0 THEN 1 ELSE 0 END) AS hard_sets
            FROM workouts w
            LEFT JOIN exercises e ON e.workout_id = w.id
            LEFT JOIN sets s ON s.exercise_id = e.id
            WHERE w.{date_col} IS NOT NULL
              AND SUBSTR(w.{date_col}, 1, 10) >= ?
              AND SUBSTR(w.{date_col}, 1, 10) <= ?
            GROUP BY SUBSTR(w.{date_col}, 1, 10)
            ORDER BY SUBSTR(w.{date_col}, 1, 10) ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    elif has_sets and "workout_id" in _table_columns(conn, "sets"):
        rows = conn.execute(
            f"""
            SELECT
                w.{date_col} AS date_iso,
                SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN (s.weight * s.reps) END) AS tonnage,
                SUM(CASE WHEN s.reps IS NOT NULL AND s.reps > 0 THEN 1 ELSE 0 END) AS hard_sets
            FROM workouts w
            LEFT JOIN sets s ON s.workout_id = w.id
            WHERE w.{date_col} IS NOT NULL
              AND SUBSTR(w.{date_col}, 1, 10) >= ?
              AND SUBSTR(w.{date_col}, 1, 10) <= ?
            GROUP BY SUBSTR(w.{date_col}, 1, 10)
            ORDER BY SUBSTR(w.{date_col}, 1, 10) ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT SUBSTR({date_col}, 1, 10) AS date_iso, COUNT(*) AS hard_sets, NULL AS tonnage
            FROM workouts
            WHERE {date_col} IS NOT NULL
              AND SUBSTR({date_col}, 1, 10) >= ?
              AND SUBSTR({date_col}, 1, 10) <= ?
            GROUP BY SUBSTR({date_col}, 1, 10)
            ORDER BY SUBSTR({date_col}, 1, 10) ASC
            """,
            (start_iso, end_iso),
        ).fetchall()

    out: dict[str, float] = {}
    for r in rows:
        d = _coerce_iso_day(r["date_iso"])
        if not d:
            continue
        tonnage = _to_float(r["tonnage"])
        hard_sets = _to_float(r["hard_sets"])
        if tonnage is not None and tonnage > 0:
            out[d] = tonnage
        elif hard_sets is not None and hard_sets > 0:
            out[d] = hard_sets
        else:
            out[d] = 0.0
    return out


def _load_cardio_series(
    runs_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> dict[str, float]:
    out: dict[str, float] = {}

    # Strava/primary runs DB
    if _table_exists(runs_conn, "runs"):
        cols = _table_columns(runs_conn, "runs")
        date_col = _first_existing(cols, ["date", "date_iso", "day"])
        moving_col = _first_existing(cols, ["moving_time", "moving_time_s", "duration_s", "minutes"])
        if date_col and moving_col:
            rows = runs_conn.execute(
                f"""
                SELECT {date_col} AS d, {moving_col} AS moving
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) >= ?
                  AND SUBSTR({date_col}, 1, 10) <= ?
                ORDER BY {date_col} ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            for r in rows:
                d = _coerce_iso_day(r["d"])
                mv = _to_float(r["moving"])
                if not d or mv is None:
                    continue
                # If source is in seconds use /60. If already minutes, keep as-is.
                minutes = mv / 60.0 if moving_col in {"moving_time", "moving_time_s", "duration_s"} else mv
                out[d] = out.get(d, 0.0) + max(0.0, minutes)

    # Manual runs in training DB
    if _table_exists(training_conn, "runs"):
        cols = _table_columns(training_conn, "runs")
        date_col = _first_existing(cols, ["date_iso", "date", "day"])
        min_col = _first_existing(cols, ["minutes", "moving_time", "moving_time_s"])
        if date_col and min_col:
            rows = training_conn.execute(
                f"""
                SELECT {date_col} AS d, {min_col} AS m
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) >= ?
                  AND SUBSTR({date_col}, 1, 10) <= ?
                ORDER BY {date_col} ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            for r in rows:
                d = _coerce_iso_day(r["d"])
                mv = _to_float(r["m"])
                if not d or mv is None:
                    continue
                minutes = mv / 60.0 if min_col in {"moving_time", "moving_time_s"} else mv
                out[d] = out.get(d, 0.0) + max(0.0, minutes)

    return out


def load_daily_series(
    training_conn: sqlite3.Connection,
    nutrition_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    end_date: date,
    days: int,
) -> dict[str, dict[str, float]]:
    start_iso = (end_date - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end_iso = end_date.isoformat()
    full_days = _date_range(end_date, days)

    weight_map = _load_weight_series(nutrition_conn, start_iso, end_iso)

    hrv_rows = hrv_conn.execute(
        """
        SELECT ts_measurement, date_utc, hr, rmssd, signal_quality
        FROM hrv_measurements
        WHERE COALESCE(SUBSTR(ts_measurement, 1, 10), SUBSTR(date_utc, 1, 10)) >= ?
          AND COALESCE(SUBSTR(ts_measurement, 1, 10), SUBSTR(date_utc, 1, 10)) <= ?
        ORDER BY ts_measurement ASC
        """,
        (
            (end_date - timedelta(days=max(1, int(days)) + 2)).isoformat(),
            (end_date + timedelta(days=1)).isoformat(),
        ),
    ).fetchall()
    rmssd_map, rhr_map = _pick_best_hrv_rows(hrv_rows)

    volume_map = _load_volume_series(training_conn, start_iso, end_iso)
    cardio_map = _load_cardio_series(runs_conn, training_conn, start_iso, end_iso)

    kcal_actual = _load_actual_kcal_series(nutrition_conn, start_iso, end_iso)
    kcal_planned = _load_planned_kcal_series(nutrition_conn, full_days)
    kcal_map: dict[str, float] = {}
    for d in full_days:
        if d in kcal_actual:
            kcal_map[d] = kcal_actual[d]
        elif kcal_planned.get(d) is not None:
            kcal_map[d] = float(kcal_planned[d])

    return {
        "weight": weight_map,
        "rmssd": rmssd_map,
        "rhr": rhr_map,
        "volume": volume_map,
        "kcal": kcal_map,
        "cardio": cardio_map,
    }


def compute_windows(
    series: dict[str, dict[str, float | None]],
    end_date: date,
    baseline_days: int,
    trend_days: int,
) -> dict[str, dict[str, Any]]:
    baseline_dates = _date_range(end_date, baseline_days)
    trend_dates = _date_range(end_date, trend_days)

    out: dict[str, dict[str, Any]] = {}
    for metric, by_day in series.items():
        cfg = METRIC_CONFIG.get(metric, {})
        min_points = int(cfg.get("min_points", 5) or 5)
        min_baseline = float(cfg.get("min_baseline", 1.0) or 1.0)

        base_vals = [_to_float(by_day.get(d)) for d in baseline_dates if _to_float(by_day.get(d)) is not None]
        trend_vals = [_to_float(by_day.get(d)) for d in trend_dates if _to_float(by_day.get(d)) is not None]

        baseline_mean_raw = _safe_mean(base_vals)
        trend_mean = _safe_mean(trend_vals)
        baseline_ok = bool(
            baseline_mean_raw is not None
            and len(base_vals) >= min_points
            and baseline_mean_raw >= min_baseline
        )
        baseline_mean = baseline_mean_raw if baseline_ok else None
        baseline_std = _safe_std(base_vals) if baseline_ok and len(base_vals) >= 10 else None

        delta_pct = None
        if baseline_ok and baseline_mean is not None and baseline_mean != 0 and trend_mean is not None:
            delta_pct = (trend_mean - baseline_mean) / baseline_mean

        if not baseline_ok:
            direction = "na"
        elif delta_pct is None:
            direction = "flat"
        elif delta_pct > 0.01:
            direction = "up"
        elif delta_pct < -0.01:
            direction = "down"
        else:
            direction = "flat"

        out[metric] = {
            "value_7d": trend_mean,
            "value_baseline": baseline_mean,
            "value_baseline_raw": baseline_mean_raw,
            "delta_pct": delta_pct,
            "direction": direction,
            "baseline_std": baseline_std,
            "baseline_ok": baseline_ok,
            "baseline_count": len(base_vals),
            "trend_count": len(trend_vals),
            "min_points": min_points,
            "min_baseline": min_baseline,
        }
    return out


def _pct_label(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:+.0f}%"


def _confidence_from_values(values: list[float | None]) -> str:
    present = sum(1 for v in values if v is not None)
    if present >= len(values):
        return "high"
    if present >= max(1, len(values) - 1):
        return "medium"
    return "low"


def build_flags(
    chips: list[dict[str, Any]],
    thresholds: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    th = thresholds or THRESHOLDS
    by_key = {c.get("key"): c for c in chips}

    def d(key: str) -> float | None:
        val = by_key.get(key, {}).get("delta_pct")
        return _to_float(val)

    flags: list[dict[str, Any]] = []

    rmssd = d("rmssd")
    rhr = d("rhr")
    weight = d("weight")
    kcal = d("kcal")
    volume = d("volume")
    cardio = d("cardio")

    # A) infekt_overreach_pattern
    if rmssd is not None and rhr is not None and rmssd <= th["infekt"]["rmssd"] and rhr >= th["infekt"]["rhr"]:
        mag = abs(rmssd - th["infekt"]["rmssd"]) + abs(rhr - th["infekt"]["rhr"])
        flags.append(
            {
                "id": "infekt_overreach_pattern",
                "level": "danger",
                "title": "Hoher Puls, niedrige HRV -> Infekt oder Overreach?",
                "why": [
                    f"RMSSD 7d {_pct_label(rmssd)} vs Baseline",
                    f"RHR 7d {_pct_label(rhr)} vs Baseline",
                ],
                "confidence": _confidence_from_values([rmssd, rhr]),
                "actions": [
                    "Heute Light / weniger Volume",
                    "Schlaf priorisieren",
                    "Optional: Schritte easy",
                ],
                "debug": {
                    "rmssd_delta_pct": rmssd,
                    "rhr_delta_pct": rhr,
                    "thresholds": {"rmssd": th["infekt"]["rmssd"], "rhr": th["infekt"]["rhr"]},
                },
                "_score": 3.0 + min(2.0, mag * 5.0),
            }
        )

    # B) cut_fatigue
    if (
        weight is not None
        and kcal is not None
        and rmssd is not None
        and rhr is not None
        and weight <= th["cut_fatigue"]["weight"]
        and kcal <= th["cut_fatigue"]["kcal"]
        and rmssd <= th["cut_fatigue"]["rmssd"]
        and rhr >= th["cut_fatigue"]["rhr"]
    ):
        is_danger = (rmssd <= -0.12) and (rhr >= 0.05)
        lvl = "danger" if is_danger else "warn"
        base = 3.0 if lvl == "danger" else 2.0
        mag = abs(weight) + abs(kcal) + abs(rmssd) + abs(rhr)
        flags.append(
            {
                "id": "cut_fatigue",
                "level": lvl,
                "title": "Cut + Fatigue Muster",
                "why": [
                    f"Gewicht {_pct_label(weight)}, kcal {_pct_label(kcal)}",
                    f"RMSSD {_pct_label(rmssd)}, RHR {_pct_label(rhr)}",
                ],
                "confidence": _confidence_from_values([weight, kcal, rmssd, rhr]),
                "actions": [
                    "Defizit temporär reduzieren",
                    "Volume 1-2 Tage senken",
                    "Erholung prüfen (Schlaf/Stress)",
                ],
                "debug": {
                    "weight_delta_pct": weight,
                    "kcal_delta_pct": kcal,
                    "rmssd_delta_pct": rmssd,
                    "rhr_delta_pct": rhr,
                    "thresholds": th["cut_fatigue"],
                },
                "_score": base + min(2.0, mag * 2.0),
            }
        )

    # C) water_stress
    if weight is not None and kcal is not None and weight >= th["water_stress"]["weight"] and kcal <= th["water_stress"]["kcal"]:
        mag = abs(weight - th["water_stress"]["weight"]) + abs(kcal - th["water_stress"]["kcal"])
        flags.append(
            {
                "id": "water_stress",
                "level": "info",
                "title": "Gewicht hoch trotz weniger kcal -> Wasser/Stress?",
                "why": [
                    f"Gewicht 7d {_pct_label(weight)}",
                    f"kcal 7d {_pct_label(kcal)}",
                ],
                "confidence": _confidence_from_values([weight, kcal]),
                "actions": [
                    "Natrium/Schlaf konsistent halten",
                    "Trend 3-5 Tage weiter beobachten",
                    "Keine hektischen Kalorien-Sprünge",
                ],
                "debug": {
                    "weight_delta_pct": weight,
                    "kcal_delta_pct": kcal,
                    "thresholds": th["water_stress"],
                },
                "_score": 1.0 + min(2.0, mag * 3.0),
            }
        )

    # D) load_spike
    if volume is not None and volume >= th["load_spike"]["volume"]:
        mag = abs(volume - th["load_spike"]["volume"])
        flags.append(
            {
                "id": "load_spike",
                "level": "warn",
                "title": "Trainingsvolumen Spike",
                "why": [f"Volumen 7d {_pct_label(volume)} vs Baseline"],
                "confidence": _confidence_from_values([volume]),
                "actions": [
                    "Nächsten Block progressiv steuern",
                    "RPE im Blick behalten",
                    "Zusatzstress reduzieren",
                ],
                "debug": {"volume_delta_pct": volume, "thresholds": th["load_spike"]},
                "_score": 2.0 + min(2.0, mag * 3.0),
            }
        )

    # E) underload_drift
    if volume is not None and volume <= th["underload"]["volume"]:
        mag = abs(volume - th["underload"]["volume"])
        flags.append(
            {
                "id": "underload_drift",
                "level": "info",
                "title": "Volumen driftet unter Baseline",
                "why": [f"Volumen 7d {_pct_label(volume)} vs Baseline"],
                "confidence": _confidence_from_values([volume]),
                "actions": [
                    "Re-Entry für Hauptlifts planen",
                    "1-2 zusätzliche harte Sätze erwägen",
                    "Trainingsrhythmus stabilisieren",
                ],
                "debug": {"volume_delta_pct": volume, "thresholds": th["underload"]},
                "_score": 1.0 + min(2.0, mag * 3.0),
            }
        )

    # F) cardio_plus_gym_stress
    if (
        cardio is not None
        and volume is not None
        and rmssd is not None
        and cardio >= th["cardio_plus_gym"]["cardio"]
        and volume >= th["cardio_plus_gym"]["volume"]
        and rmssd <= th["cardio_plus_gym"]["rmssd"]
    ):
        mag = abs(cardio) + abs(volume) + abs(rmssd)
        flags.append(
            {
                "id": "cardio_plus_gym_stress",
                "level": "warn",
                "title": "Cardio + Gym Stresskombi",
                "why": [
                    f"Cardio {_pct_label(cardio)}, Volumen {_pct_label(volume)}",
                    f"RMSSD {_pct_label(rmssd)}",
                ],
                "confidence": _confidence_from_values([cardio, volume, rmssd]),
                "actions": [
                    "Cardio-Intensität temporär drosseln",
                    "Gym-Volume 1 Session reduzieren",
                    "Erholung aktiv absichern",
                ],
                "debug": {
                    "cardio_delta_pct": cardio,
                    "volume_delta_pct": volume,
                    "rmssd_delta_pct": rmssd,
                    "thresholds": th["cardio_plus_gym"],
                },
                "_score": 2.0 + min(2.0, mag * 1.5),
            }
        )

    flags.sort(key=lambda x: x.get("_score", 0), reverse=True)
    for f in flags:
        f.pop("_score", None)
    return flags


def _status_from_flags(flags: list[dict[str, Any]], chips: list[dict[str, Any]]) -> dict[str, Any]:
    has_danger = any(f.get("level") == "danger" for f in flags)
    has_warn = any(f.get("level") == "warn" for f in flags)
    top_codes = [str(f.get("id") or "") for f in flags[:2] if f.get("id")]
    reasons = top_codes[:]

    if has_danger:
        level = "red"
        top = flags[0].get("title") if flags else "Top Pattern"
        headline = f"Achtung: {top}, heute defensiv"
    elif has_warn:
        level = "yellow"
        codes = ", ".join(top_codes) if top_codes else "leichte Signale"
        headline = f"Hinweis: {codes}, leichte Divergenz"
    else:
        level = "green"
        headline = "System stabil: Erholung ok, Gewicht on track"

    return {"level": level, "headline": headline, "reasons": reasons}


def _build_chip(metric: str, stats: dict[str, Any], has_any_data: bool, trend_days: int, baseline_days: int) -> dict[str, Any]:
    meta = METRIC_META[metric]
    baseline_ok = bool(stats.get("baseline_ok"))
    return {
        "key": metric,
        "label": meta["label"],
        "unit": meta["unit"],
        "value_7d": _round_or_none(stats.get("value_7d"), 2),
        "value_baseline": _round_or_none(stats.get("value_baseline"), 2),
        "delta_pct": _round_or_none(stats.get("delta_pct"), 4) if baseline_ok else None,
        "direction": (stats.get("direction") or "flat") if baseline_ok else "na",
        "note": f"{trend_days}d vs {baseline_days}d" if baseline_ok else "zu wenig Baseline",
        "missing": not has_any_data,
        "baseline_ok": baseline_ok,
    }


def _freshness(series: dict[str, dict[str, float | None]]) -> dict[str, str | None]:
    def last_day(metric: str) -> str | None:
        days = [d for d, v in series.get(metric, {}).items() if _to_float(v) is not None]
        return max(days) if days else None

    return {
        "weight_last": last_day("weight"),
        "hrv_last": last_day("rmssd"),
        "training_last": last_day("volume"),
        "nutrition_last": last_day("kcal"),
        "cardio_last": last_day("cardio"),
    }


def build_trend_radar_payload(
    training_conn: sqlite3.Connection,
    nutrition_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    *,
    end_date: date,
    days: int,
    baseline_days: int,
    trend_days: int,
) -> dict[str, Any]:
    span = max(int(days), int(baseline_days), int(trend_days))
    series = load_daily_series(
        training_conn=training_conn,
        nutrition_conn=nutrition_conn,
        hrv_conn=hrv_conn,
        runs_conn=runs_conn,
        end_date=end_date,
        days=span,
    )

    metric_has_data: dict[str, bool] = {}
    for m in METRICS_ORDER:
        vals = [_to_float(v) for v in series.get(m, {}).values()]
        metric_has_data[m] = any(v is not None for v in vals)

    # For activity metrics with at least some data, missing dates inside range are treated as 0 (rest days).
    full_span_days = _date_range(end_date, span)
    for m in ("volume", "cardio"):
        if metric_has_data.get(m):
            for d in full_span_days:
                if d not in series[m]:
                    series[m][d] = 0.0

    stats = compute_windows(series, end_date=end_date, baseline_days=baseline_days, trend_days=trend_days)
    chips = [_build_chip(m, stats.get(m, {}), metric_has_data.get(m, False), trend_days, baseline_days) for m in METRICS_ORDER]

    flags = build_flags(chips, THRESHOLDS)
    status = _status_from_flags(flags, chips)

    chart_days = _date_range(end_date, days)
    series_payload: dict[str, Any] = {"dates": chart_days, "metrics": {}}
    for m in METRICS_ORDER:
        baseline = _to_float(stats.get(m, {}).get("value_baseline"))
        baseline_ok = bool(stats.get(m, {}).get("baseline_ok"))
        cfg = METRIC_CONFIG.get(m, {})

        if not metric_has_data.get(m):
            raw = [None for _ in chart_days]
            idx_daily = [None for _ in chart_days]
            idx_7d = [None for _ in chart_days]
            idx_unclamped = [None for _ in chart_days]
            band = {"low": 95.0, "high": 105.0}
        else:
            raw = []
            for d in chart_days:
                v = _to_float(series.get(m, {}).get(d))
                raw.append(_round_or_none(v, 3) if v is not None else None)

            idx_daily = []
            idx_unclamped = []
            for v in raw:
                if v is None or not baseline_ok or baseline is None or baseline == 0:
                    idx_daily.append(None)
                    idx_unclamped.append(None)
                else:
                    raw_idx = 100.0 * (v / baseline)
                    idx_unclamped.append(_round_or_none(raw_idx, 3))
                    idx_daily.append(_round_or_none(raw_idx, 3))

            idx_7d_raw = _rolling_mean_nullsafe(idx_daily, window=7, min_points=3)
            idx_7d = [_round_or_none(v, 3) if v is not None else None for v in idx_7d_raw]

            band = {"low": 95.0, "high": 105.0}

        metric_payload = {
            "raw": raw,
            "index": idx_daily,  # backward-compatible alias for daily index
            "index_daily": idx_daily,
            "index_7d": idx_7d,
            "band": band,
            "baseline_mean": _round_or_none(baseline, 3) if baseline_ok else None,
            "baseline_ok": baseline_ok,
            "chart_default": bool(cfg.get("chart_default", True)),
        }
        if _debug_enabled():
            metric_payload["index_unclamped"] = idx_unclamped
        series_payload["metrics"][m] = metric_payload

    payload = {
        "meta": {
            "end_date": end_date.isoformat(),
            "days": int(days),
            "baseline_days": int(baseline_days),
            "trend_days": int(trend_days),
            "timezone": "Europe/Berlin",
            "data_freshness": _freshness(series),
        },
        "status": status,
        "chips": chips,
        "series": series_payload,
        "flags": flags,
    }

    _dbg(
        "payload_ready",
        {
            "end": payload["meta"]["end_date"],
            "chips": len(payload["chips"]),
            "flags": len(payload["flags"]),
            "status": payload["status"]["level"],
        },
    )
    return payload
