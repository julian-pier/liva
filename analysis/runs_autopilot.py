import sqlite3
from datetime import date, timedelta

from database.connections import get_runs_db
from analysis.runs_analysis import classify_run


QUALITY_LABELS = {"Tempo", "Threshold", "Interval"}


def _to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).split("T")[0])
    except Exception:
        return None


def _run_kind_from_label(label: str) -> str:
    lab = (label or "").strip().lower()
    if lab in {"interval", "intervals"}:
        return "threshold"
    if lab in {"threshold", "tempo"}:
        return "threshold"
    return "z2"


def load_recent_runs(days: int = 180):
    try:
        conn = get_runs_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT date, distance, moving_time, avg_hr, pace
            FROM runs
            WHERE date IS NOT NULL
              AND date >= date('now', ?)
            ORDER BY date DESC
            """,
            (f"-{int(days)} days",),
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []


def latest_run_by_kind(kind: str, days: int = 180):
    rows = load_recent_runs(days)
    for row in rows:
        dist_m = row["distance"] or 0.0
        km = dist_m / 1000.0 if dist_m else None
        pace_s = row["pace"]
        pace_min = (float(pace_s) / 60.0) if pace_s else None
        label = classify_run(km, pace_min, row["avg_hr"]) if km and pace_min else "Unknown"
        rk = _run_kind_from_label(label)
        if rk != kind:
            continue
        return {
            "date": row["date"],
            "km": km,
            "pace_s": pace_s,
            "avg_hr": row["avg_hr"],
            "moving_time": row["moving_time"],
        }
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def compute_run_targets(kind: str, days: int = 180, n: int = 8):
    rows = load_recent_runs(days)
    if not rows:
        return None
    samples = []
    for row in rows:
        dist_m = row["distance"] or 0.0
        km = dist_m / 1000.0 if dist_m else None
        pace_s = row["pace"]
        pace_min = (float(pace_s) / 60.0) if pace_s else None
        label = classify_run(km, pace_min, row["avg_hr"]) if km and pace_min else "Unknown"
        rk = _run_kind_from_label(label)
        if rk != kind:
            continue
        samples.append(row)
        if len(samples) >= n:
            break

    if not samples and kind == "z2":
        # fallback to all easy runs
        for row in rows:
            dist_m = row["distance"] or 0.0
            km = dist_m / 1000.0 if dist_m else None
            pace_s = row["pace"]
            pace_min = (float(pace_s) / 60.0) if pace_s else None
            label = classify_run(km, pace_min, row["avg_hr"]) if km and pace_min else "Unknown"
            rk = _run_kind_from_label(label)
            if rk != "z2":
                continue
            samples.append(row)
            if len(samples) >= n:
                break

    if not samples:
        return None

    durations = []
    hrs = []
    paces = []
    for row in samples:
        if row["moving_time"]:
            durations.append(float(row["moving_time"]) / 60.0)
        if row["avg_hr"]:
            hrs.append(float(row["avg_hr"]))
        if row["pace"]:
            paces.append(float(row["pace"]))

    duration_min = _median(durations)
    target_hr = _median(hrs)
    target_pace_s = _median(paces)

    hr_ok = True
    if len(hrs) < max(3, len(samples) // 2):
        hr_ok = False
    if target_hr is not None:
        if target_hr < 90 or target_hr > 195:
            hr_ok = False
        if kind == "z2" and target_pace_s is not None and target_hr >= 165:
            hr_ok = False
    if not hr_ok:
        target_hr = None

    return {
        "duration_min": duration_min,
        "target_hr": target_hr,
        "target_pace_s": target_pace_s,
        "basis": f"median_last_{min(len(samples), n)}_{kind}",
    }


def compute_run_stats(days: int = 60, today: date | None = None):
    today = today or date.today()
    rows = load_recent_runs(days)
    last_run = None
    last_quality = None
    daily_km = {}
    quality_count_7d = 0
    total_km_7d = 0.0

    for row in rows:
        d = _to_date(row["date"])
        if not d:
            continue
        if not last_run:
            last_run = d
        dist_m = row["distance"] or 0.0
        km = dist_m / 1000.0 if dist_m else 0.0
        pace_s = row["pace"]
        pace_min = (float(pace_s) / 60.0) if pace_s else None
        label = classify_run(km, pace_min, row["avg_hr"]) if km and pace_min else "Unknown"
        if label in QUALITY_LABELS and not last_quality:
            last_quality = d

        if d >= today - timedelta(days=6):
            total_km_7d += km
            if label in QUALITY_LABELS:
                quality_count_7d += 1

        daily_km.setdefault(d, 0.0)
        daily_km[d] += km

    km_vals_7d = [v for d, v in daily_km.items() if d >= today - timedelta(days=6)]
    mean_km = sum(km_vals_7d) / len(km_vals_7d) if km_vals_7d else 0.0
    if km_vals_7d:
        variance = sum((v - mean_km) ** 2 for v in km_vals_7d) / max(1, len(km_vals_7d))
        std = variance ** 0.5
    else:
        std = 0.0
    monotony = mean_km / max(1.0, std) if mean_km else 0.0

    return {
        "last_run_date": last_run.isoformat() if last_run else None,
        "last_quality_date": last_quality.isoformat() if last_quality else None,
        "weekly_km": total_km_7d,
        "quality_count_7d": quality_count_7d,
        "monotony": monotony,
    }
