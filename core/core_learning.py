from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any

from core.core_training_deck import get_core_principle_scores
from core.core_training_features import load_latest_features
from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_runs_db, get_training_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _date_from_any(raw: Any) -> date | None:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    if " " in s:
        head = s.split(" ", 1)[0]
        try:
            return date.fromisoformat(head)
        except Exception:
            return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _hour_from_any(raw: Any) -> int | None:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return int(datetime.strptime(s, fmt).hour)
        except Exception:
            continue
    if " " in s:
        tail = s.split(" ", 1)[1]
        try:
            return int(tail.split(":", 1)[0])
        except Exception:
            return None
    if "T" in s:
        tail = s.split("T", 1)[1]
        try:
            return int(tail.split(":", 1)[0])
        except Exception:
            return None
    return None


def _iso(dt: date) -> str:
    return dt.isoformat()


def _days_between(start: date, end: date) -> int:
    return max(0, (end - start).days)


def _ratio(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return num / den




def _pattern_status(support: float, confidence: float) -> str:
    if support >= 0.68 and confidence >= 0.48:
        return "active"
    if support >= 0.45:
        return "monitoring"
    if support >= 0.2:
        return "weak"
    return "discarded"


def _observation(
    *,
    observed_at: str,
    domain: str,
    signal_key: str,
    value: dict[str, Any],
    source: str,
    quality: float,
    confidence: float,
) -> dict[str, Any]:
    return {
        "observed_at": observed_at,
        "domain": domain,
        "signal_key": signal_key,
        "signal_value_json": json.dumps(value, ensure_ascii=False),
        "source": source,
        "quality_score": round(_clamp(quality), 3),
        "confidence": round(_clamp(confidence), 3),
        "created_at": _utc_now(),
        "_value": value,
    }


def _load_training_signals(day: date) -> dict[str, Any]:
    since14 = _iso(day - timedelta(days=13))
    since28 = _iso(day - timedelta(days=27))
    since7 = _iso(day - timedelta(days=6))

    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        workouts14 = conn.execute(
            "SELECT date_iso, created_at FROM workouts WHERE date_iso >= ? ORDER BY date_iso",
            (since14,),
        ).fetchall()
        workouts28 = conn.execute(
            "SELECT date_iso FROM workouts WHERE date_iso >= ? ORDER BY date_iso",
            (since28,),
        ).fetchall()

        row_sets = conn.execute(
            """
            SELECT
                COUNT(*) AS sets_count,
                AVG(COALESCE(s.rpe, 0)) AS avg_rpe,
                SUM(CASE WHEN lower(e.name) LIKE '%lat%' OR lower(e.name) LIKE '%row%' OR lower(e.name) LIKE '%pull%' OR lower(e.name) LIKE '%rudern%' THEN 1 ELSE 0 END) AS pull_sets,
                SUM(CASE WHEN lower(e.name) LIKE '%bench%' OR lower(e.name) LIKE '%press%' OR lower(e.name) LIKE '%ohp%' OR lower(e.name) LIKE '%drücken%' THEN 1 ELSE 0 END) AS push_sets
            FROM sets s
            JOIN workouts w ON w.id = s.workout_id
            LEFT JOIN exercises e ON e.id = s.exercise_id
            WHERE w.date_iso >= ?
            """,
            (since14,),
        ).fetchone()

        override_rows = conn.execute(
            """
            SELECT ts, decision_type, user_override_bool
            FROM override_log
            WHERE ts >= strftime('%s', ?)
            ORDER BY ts DESC
            """,
            (since14,),
        ).fetchall()

    finally:
        conn.close()

    workout_days = {str(r["date_iso"]) for r in workouts14}
    workout_days_28 = {str(r["date_iso"]) for r in workouts28}
    late_hours = [
        h
        for h in (_hour_from_any(r["created_at"]) for r in workouts14)
        if h is not None
    ]
    late_count = sum(1 for h in late_hours if h >= 21)

    override_count = 0
    override_types: Counter[str] = Counter()
    override_days: set[str] = set()
    for row in override_rows:
        ts = _safe_int(row["ts"])
        if ts > 0:
            override_days.add(datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat())
        if bool(row["user_override_bool"]):
            override_count += 1
        t = str(row["decision_type"] or "").strip().lower()
        if t:
            override_types[t] += 1

    sets_count = _safe_int(row_sets["sets_count"] if row_sets else 0)
    avg_rpe = _safe_float(row_sets["avg_rpe"] if row_sets else 0.0)
    pull_sets = _safe_int(row_sets["pull_sets"] if row_sets else 0)
    push_sets = _safe_int(row_sets["push_sets"] if row_sets else 0)

    return {
        "workout_days_14": len(workout_days),
        "workout_days_28": len(workout_days_28),
        "sets_14": sets_count,
        "avg_rpe_14": avg_rpe,
        "pull_sets_14": pull_sets,
        "push_sets_14": push_sets,
        "late_workout_rate": _ratio(late_count, max(1, len(late_hours))),
        "override_rate_14": _ratio(override_count, 14.0),
        "override_days_14": len(override_days),
        "override_types_top": override_types.most_common(4),
        "workout_coverage_14": _ratio(len(workout_days), 14.0),
        "workout_coverage_28": _ratio(len(workout_days_28), 28.0),
        "since7": since7,
    }


def _load_run_signals(day: date) -> dict[str, Any]:
    since14 = _iso(day - timedelta(days=13))
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date, distance, moving_time, avg_hr, pace
            FROM runs
            WHERE substr(date,1,10) >= ?
            ORDER BY date
            """,
            (since14,),
        ).fetchall()
    finally:
        conn.close()

    run_days: set[str] = set()
    total_km = 0.0
    total_min = 0.0
    hrs: list[float] = []
    for row in rows:
        day_id = str(row["date"] or "")[:10]
        if day_id:
            run_days.add(day_id)
        total_km += _safe_float(row["distance"])
        total_min += _safe_float(row["moving_time"]) / 60.0
        h = _safe_float(row["avg_hr"], -1.0)
        if h > 0:
            hrs.append(h)

    return {
        "run_days_14": len(run_days),
        "run_coverage_14": _ratio(len(run_days), 14.0),
        "distance_km_14": round(total_km, 2),
        "duration_min_14": round(total_min, 1),
        "avg_hr_14": round(mean(hrs), 1) if hrs else None,
    }


def _load_hrv_signals(day: date) -> dict[str, Any]:
    since28 = _iso(day - timedelta(days=27))
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date_utc, hr, rmssd, sleep_quality, fatigue
            FROM hrv_measurements
            WHERE date_utc >= ?
            ORDER BY date_utc DESC
            """,
            (since28,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    if not rows:
        return {
            "hrv_days_14": 0,
            "hrv_coverage_14": 0.0,
            "rmssd_today": None,
            "hr_today": None,
            "rmssd_ratio_14": None,
            "hr_ratio_14": None,
            "sleep_quality_today": None,
            "fatigue_today": None,
            "avg_measurement_hour": None,
        }

    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_day = _date_from_any(row["date_utc"])
        if row_day is None:
            continue
        key = row_day.isoformat()
        if key not in by_date:
            by_date[key] = dict(row)

    days14 = [d for d in by_date if d >= _iso(day - timedelta(days=13))]
    rmssd_values = [_safe_float(v.get("rmssd"), -1.0) for v in by_date.values()]
    rmssd_values = [v for v in rmssd_values if v > 0]
    hr_values = [_safe_float(v.get("hr"), -1.0) for v in by_date.values()]
    hr_values = [v for v in hr_values if v > 0]

    latest_key = max(by_date.keys())
    latest = by_date[latest_key]
    rmssd_today = _safe_float(latest.get("rmssd"), -1.0)
    hr_today = _safe_float(latest.get("hr"), -1.0)

    rmssd_base = mean(rmssd_values[:14]) if len(rmssd_values) >= 2 else (mean(rmssd_values) if rmssd_values else 0.0)
    hr_base = mean(hr_values[:14]) if len(hr_values) >= 2 else (mean(hr_values) if hr_values else 0.0)

    hours = [
        h
        for h in (_hour_from_any(v.get("date_utc")) for v in by_date.values())
        if h is not None
    ]

    return {
        "hrv_days_14": len(days14),
        "hrv_coverage_14": _ratio(len(days14), 14.0),
        "rmssd_today": rmssd_today if rmssd_today > 0 else None,
        "hr_today": hr_today if hr_today > 0 else None,
        "rmssd_ratio_14": (rmssd_today / rmssd_base) if rmssd_today > 0 and rmssd_base > 0 else None,
        "hr_ratio_14": (hr_today / hr_base) if hr_today > 0 and hr_base > 0 else None,
        "sleep_quality_today": _safe_float(latest.get("sleep_quality"), 0.0) if latest.get("sleep_quality") is not None else None,
        "fatigue_today": _safe_float(latest.get("fatigue"), 0.0) if latest.get("fatigue") is not None else None,
        "avg_measurement_hour": round(mean(hours), 2) if hours else None,
    }


def _load_nutrition_signals(day: date) -> dict[str, Any]:
    since14 = _iso(day - timedelta(days=13))
    since30 = _iso(day - timedelta(days=29))

    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date_iso, kcal, protein, carbs, fat
            FROM nutrition_daily
            WHERE date_iso >= ?
            ORDER BY date_iso ASC
            """,
            (since30,),
        ).fetchall()
        weight_rows = conn.execute(
            """
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso >= ?
            ORDER BY date_iso ASC
            """,
            (since30,),
        ).fetchall()
        action_rows = conn.execute(
            """
            SELECT date_iso, action_type, status, reason_codes_json
            FROM core_nutrition_actions
            WHERE date_iso >= ?
            ORDER BY created_at DESC
            LIMIT 80
            """,
            (since14,),
        ).fetchall()
    finally:
        conn.close()

    recent = [r for r in rows if str(r["date_iso"] or "") >= since14]
    logged_days = sum(1 for r in recent if r["kcal"] is not None)
    cals = [_safe_float(r["kcal"], -1.0) for r in recent if r["kcal"] is not None]
    proteins = [_safe_float(r["protein"], -1.0) for r in recent if r["protein"] is not None]

    kcal_volatility = 0.0
    if len(cals) >= 3:
        mu = mean(cals)
        std = math.sqrt(sum((x - mu) ** 2 for x in cals) / len(cals))
        kcal_volatility = _ratio(std, max(1.0, mu))

    weights = [_safe_float(r["weight_kg"], -1.0) for r in weight_rows if r["weight_kg"] is not None]
    weight_recent_days = {
        str(r["date_iso"] or "")
        for r in weight_rows
        if str(r["date_iso"] or "") >= since14 and r["weight_kg"] is not None
    }
    weight_trend_14 = 0.0
    if len(weights) >= 2:
        tail = weights[-14:] if len(weights) > 14 else weights
        weight_trend_14 = _safe_float(tail[-1]) - _safe_float(tail[0])

    actions_by_status: Counter[str] = Counter()
    action_reason_codes: Counter[str] = Counter()
    for row in action_rows:
        actions_by_status[str(row["status"] or "unknown").strip().lower()] += 1
        for code in _parse_json(row["reason_codes_json"], []):
            c = str(code or "").strip().upper()
            if c:
                action_reason_codes[c] += 1

    return {
        "nutrition_days_14": len(recent),
        "nutrition_logged_days_14": logged_days,
        "nutrition_logging_coverage_14": _ratio(logged_days, 14.0),
        "kcal_avg_14": round(mean(cals), 1) if cals else None,
        "protein_avg_14": round(mean(proteins), 1) if proteins else None,
        "kcal_volatility_14": round(kcal_volatility, 3),
        "weight_days_14": len(weight_recent_days),
        "weight_coverage_14": _ratio(len(weight_recent_days), 14.0),
        "weight_trend_14": round(weight_trend_14, 2),
        "nutrition_action_status": dict(actions_by_status),
        "nutrition_action_reason_codes": dict(action_reason_codes),
    }


def _load_decision_signals(day: date) -> dict[str, Any]:
    since30 = _iso(day - timedelta(days=29))
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, day_iso, type, severity, source, decision_text, impact_text, why_json, raw_json
            FROM core_decision_log
            WHERE day_iso >= ?
            ORDER BY day_iso DESC, created_at DESC
            """,
            (since30,),
        ).fetchall()
    finally:
        conn.close()

    reason_codes: Counter[str] = Counter()
    live_rows = 0
    major_rows = 0
    plateau_rows = 0
    pull_hint_count = 0

    for row in rows:
        if str(row["source"] or "") == "live_today":
            live_rows += 1
        if str(row["severity"] or "").lower().startswith("major"):
            major_rows += 1
        if str(row["source"] or "") == "plateau_scan":
            plateau_rows += 1

        for line in _parse_json(row["why_json"], []):
            text = str(line or "").lower()
            if "pull" in text and "m\u00fcd" in text:
                pull_hint_count += 1

        raw = _parse_json(row["raw_json"], {})
        session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
        session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
        meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
        for code in meta.get("reason_codes") or []:
            c = str(code or "").strip().upper()
            if c:
                reason_codes[c] += 1

    return {
        "decision_count_30": len(rows),
        "live_decisions_30": live_rows,
        "major_decisions_30": major_rows,
        "plateau_decisions_30": plateau_rows,
        "reason_codes": dict(reason_codes),
        "pull_fatigue_mentions": pull_hint_count,
    }


def collect_raw_signals(day_iso: str | None = None) -> dict[str, Any]:
    target = _date_from_any(day_iso) if day_iso else date.today()
    if target is None:
        target = date.today()

    features_payload: dict[str, Any] = {}
    try:
        features_payload = load_latest_features()
    except Exception:
        features_payload = {"features": {}, "summaries": {}, "generated_at": _utc_now()}

    training = _load_training_signals(target)
    runs = _load_run_signals(target)
    hrv = _load_hrv_signals(target)
    nutrition = _load_nutrition_signals(target)
    decisions = _load_decision_signals(target)
    principles = get_core_principle_scores()

    coverage = {
        "training": round(training.get("workout_coverage_14", 0.0), 3),
        "run": round(runs.get("run_coverage_14", 0.0), 3),
        "recovery": round(hrv.get("hrv_coverage_14", 0.0), 3),
        "nutrition": round(nutrition.get("nutrition_logging_coverage_14", 0.0), 3),
        "weight": round(nutrition.get("weight_coverage_14", 0.0), 3),
        "decision_history": round(_clamp(_ratio(decisions.get("decision_count_30", 0), 20.0)), 3),
    }

    coverage_avg = round(mean(list(coverage.values())) if coverage else 0.0, 3)

    return {
        "date": target.isoformat(),
        "generated_at": _utc_now(),
        "features": features_payload.get("features") if isinstance(features_payload.get("features"), dict) else {},
        "feature_summaries": features_payload.get("summaries") if isinstance(features_payload.get("summaries"), dict) else {},
        "training": training,
        "runs": runs,
        "hrv": hrv,
        "nutrition": nutrition,
        "decisions": decisions,
        "principles": principles,
        "coverage": coverage,
        "coverage_avg": coverage_avg,
    }


def _build_observations(raw: dict[str, Any]) -> list[dict[str, Any]]:
    obs_at = f"{raw['date']}T12:00:00Z"
    coverage_avg = _safe_float(raw.get("coverage_avg"))
    items: list[dict[str, Any]] = []

    training = raw.get("training") if isinstance(raw.get("training"), dict) else {}
    runs = raw.get("runs") if isinstance(raw.get("runs"), dict) else {}
    hrv = raw.get("hrv") if isinstance(raw.get("hrv"), dict) else {}
    nutrition = raw.get("nutrition") if isinstance(raw.get("nutrition"), dict) else {}
    decisions = raw.get("decisions") if isinstance(raw.get("decisions"), dict) else {}

    items.append(
        _observation(
            observed_at=obs_at,
            domain="training",
            signal_key="training.load_14d",
            value={
                "workout_days": training.get("workout_days_14"),
                "sets": training.get("sets_14"),
                "avg_rpe": training.get("avg_rpe_14"),
            },
            source="training.sqlite3",
            quality=training.get("workout_coverage_14", 0.0),
            confidence=0.55 + 0.35 * _safe_float(training.get("workout_coverage_14")),
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="run",
            signal_key="run.load_14d",
            value={
                "run_days": runs.get("run_days_14"),
                "distance_km": runs.get("distance_km_14"),
                "duration_min": runs.get("duration_min_14"),
            },
            source="runs.sqlite3",
            quality=runs.get("run_coverage_14", 0.0),
            confidence=0.5 + 0.4 * _safe_float(runs.get("run_coverage_14")),
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="recovery",
            signal_key="recovery.hrv_today",
            value={
                "rmssd": hrv.get("rmssd_today"),
                "hr": hrv.get("hr_today"),
                "rmssd_ratio_14": hrv.get("rmssd_ratio_14"),
                "hr_ratio_14": hrv.get("hr_ratio_14"),
            },
            source="hrv.sqlite3",
            quality=hrv.get("hrv_coverage_14", 0.0),
            confidence=0.45 + 0.45 * _safe_float(hrv.get("hrv_coverage_14")),
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="nutrition",
            signal_key="nutrition.logging_14d",
            value={
                "logged_days": nutrition.get("nutrition_logged_days_14"),
                "coverage": nutrition.get("nutrition_logging_coverage_14"),
                "kcal_volatility": nutrition.get("kcal_volatility_14"),
            },
            source="ernaehrung.sqlite3",
            quality=nutrition.get("nutrition_logging_coverage_14", 0.0),
            confidence=0.4 + 0.5 * _safe_float(nutrition.get("nutrition_logging_coverage_14")),
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="schedule",
            signal_key="schedule.override_14d",
            value={
                "override_rate": training.get("override_rate_14"),
                "override_days": training.get("override_days_14"),
                "top_types": training.get("override_types_top"),
            },
            source="training.override_log",
            quality=0.75,
            confidence=0.65,
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="behavior",
            signal_key="behavior.late_signals",
            value={
                "late_workout_rate": training.get("late_workout_rate"),
                "avg_hrv_measurement_hour": hrv.get("avg_measurement_hour"),
            },
            source="training+hrv",
            quality=0.55,
            confidence=0.5,
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="system",
            signal_key="system.coverage",
            value={"coverage": raw.get("coverage"), "avg": coverage_avg},
            source="core_v2",
            quality=coverage_avg,
            confidence=0.8,
        )
    )
    items.append(
        _observation(
            observed_at=obs_at,
            domain="system",
            signal_key="system.decision_history",
            value={
                "decision_count_30": decisions.get("decision_count_30"),
                "major_decisions_30": decisions.get("major_decisions_30"),
                "reason_codes": decisions.get("reason_codes"),
            },
            source="core_decision_log",
            quality=_clamp(_ratio(_safe_float(decisions.get("decision_count_30")), 30.0)),
            confidence=0.72,
        )
    )

    return items


def _pattern(
    *,
    key: str,
    title: str,
    summary: str,
    domain: str,
    support: float,
    contradiction: float,
    confidence: float,
    evidence: list[dict[str, Any]],
    notes: str,
) -> dict[str, Any]:
    support_c = _clamp(support)
    contradiction_c = _clamp(contradiction)
    status = _pattern_status(support_c, confidence)
    stability = _clamp(0.5 + (support_c - contradiction_c) * 0.45)
    return {
        "pattern_key": key,
        "title": title,
        "summary": summary,
        "domain": domain,
        "status": status,
        "confidence": round(_clamp(confidence), 3),
        "stability_score": round(stability, 3),
        "support_score": round(support_c, 3),
        "contradiction_score": round(contradiction_c, 3),
        "evidence_count": len(evidence),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
        "notes": notes,
    }


def _build_patterns(raw: dict[str, Any]) -> list[dict[str, Any]]:
    features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    training = raw.get("training") if isinstance(raw.get("training"), dict) else {}
    runs = raw.get("runs") if isinstance(raw.get("runs"), dict) else {}
    hrv = raw.get("hrv") if isinstance(raw.get("hrv"), dict) else {}
    nutrition = raw.get("nutrition") if isinstance(raw.get("nutrition"), dict) else {}
    decisions = raw.get("decisions") if isinstance(raw.get("decisions"), dict) else {}
    coverage_avg = _safe_float(raw.get("coverage_avg"))
    reason_codes = decisions.get("reason_codes") if isinstance(decisions.get("reason_codes"), dict) else {}

    patterns: list[dict[str, Any]] = []

    late_hour = _safe_float(hrv.get("avg_measurement_hour"), 0.0)
    late_workout_rate = _safe_float(training.get("late_workout_rate"), 0.0)
    late_support = _clamp((max(0.0, late_hour - 22.0) / 3.0) * 0.6 + late_workout_rate * 0.5)
    patterns.append(
        _pattern(
            key="late_shutdown_probability",
            title="Schlaf vermutlich spät",
            summary="Mess- und Aktivitätszeiten liegen wiederholt spät, was die Recovery am Folgetag fragiler machen kann.",
            domain="behavior",
            support=late_support,
            contradiction=1.0 - late_support,
            confidence=0.35 + 0.45 * _clamp(_ratio(training.get("workout_days_14", 0), 10.0) + _ratio(hrv.get("hrv_days_14", 0), 10.0)),
            evidence=[
                {"label": "Ø HRV-Messzeit", "value": f"{late_hour:.2f} Uhr" if late_hour else "keine"},
                {"label": "Späte Workout-Rate", "value": f"{round(late_workout_rate * 100)}%"},
            ],
            notes="Bei diesem Muster priorisiert CORE früheres Runterfahren statt zusätzlichen Druck am Abend.",
        )
    )

    nutrition_cov = _safe_float(nutrition.get("nutrition_logging_coverage_14"), 0.0)
    kcal_vol = _safe_float(nutrition.get("kcal_volatility_14"), 0.0)
    nutrition_bucket = str(features.get("nutrition_adherence") or "").strip().lower()
    nutrition_support = _clamp((1.0 - nutrition_cov) * 0.65 + _clamp(kcal_vol / 0.35) * 0.35)
    if nutrition_bucket in {"missing", "low"}:
        nutrition_support = _clamp(nutrition_support + 0.18)
    patterns.append(
        _pattern(
            key="nutrition_instability",
            title="Ernährung aktuell labil",
            summary="Die Ernährungssignale sind uneinheitlich oder unvollständig; Entscheidungen brauchen mehr Vorsicht beim Push.",
            domain="nutrition",
            support=nutrition_support,
            contradiction=1.0 - nutrition_support,
            confidence=0.35 + 0.5 * nutrition_cov,
            evidence=[
                {"label": "Logging-Coverage 14d", "value": f"{round(nutrition_cov * 100)}%"},
                {"label": "Kcal-Volatilität", "value": f"{round(kcal_vol * 100)}%"},
                {"label": "Feature-Bucket", "value": nutrition_bucket or "unbekannt"},
            ],
            notes="Wenn dieses Muster aktiv ist, dämpft CORE aggressive Progressionsfenster bis die Energiezufuhr wieder stabil ist.",
        )
    )

    structure_support = _clamp(_safe_float(training.get("override_rate_14"), 0.0) * 3.4 + _ratio(_safe_float(decisions.get("major_decisions_30")), 18.0))
    patterns.append(
        _pattern(
            key="day_structure_drift",
            title="Tagesstruktur driftet",
            summary="Viele Overrides oder häufige größere Eingriffe deuten auf driftende Tagesstruktur statt stabile Routine.",
            domain="schedule",
            support=structure_support,
            contradiction=1.0 - structure_support,
            confidence=0.58,
            evidence=[
                {"label": "Override-Rate 14d", "value": f"{round(_safe_float(training.get('override_rate_14')) * 100)}%"},
                {"label": "Major Decisions 30d", "value": str(decisions.get("major_decisions_30") or 0)},
                {"label": "Top Override-Typen", "value": ", ".join(t for t, _ in training.get("override_types_top") or []) or "keine"},
            ],
            notes="Drift erhöht das Risiko für inkonsistente Reizsetzung und verschiebt die Güte der Tagesentscheidungen.",
        )
    )

    pull_ratio = _ratio(_safe_float(training.get("pull_sets_14")), max(1.0, _safe_float(training.get("push_sets_14"))))
    pull_mentions = _safe_int(decisions.get("pull_fatigue_mentions"))
    pull_support = _clamp(_clamp((pull_ratio - 0.9) / 0.8) * 0.45 + _clamp(_ratio(pull_mentions, 6.0)) * 0.55)
    patterns.append(
        _pattern(
            key="local_pull_fatigue",
            title="Lokale Pull-Fatigue wahrscheinlich",
            summary="Ziehende Strukturen zeigen wiederholt Ermüdungssignale und sollten nicht blind eskaliert werden.",
            domain="training",
            support=pull_support,
            contradiction=1.0 - pull_support,
            confidence=0.45 + 0.4 * _clamp(_ratio(_safe_float(training.get("sets_14")), 120.0)),
            evidence=[
                {"label": "Pull/Push-Set-Verhältnis", "value": f"{round(pull_ratio, 2)}"},
                {"label": "Pull-Fatigue-Hinweise", "value": str(pull_mentions)},
                {"label": "Sets 14d", "value": str(training.get("sets_14") or 0)},
            ],
            notes="Dieses Muster beeinflusst primär Übungsauswahl, lokale Intensität und das Timing von Pull-Schwerpunkten.",
        )
    )

    plateau_count = _safe_int(decisions.get("plateau_decisions_30"))
    progress_support = _clamp(_ratio(plateau_count, 4.0))
    patterns.append(
        _pattern(
            key="progress_stagnation_active_plan",
            title="Progress im aktiven Plan stockt",
            summary="Plateau-Scans wurden zuletzt mehrfach ausgelöst; Progression braucht ggf. Strukturwechsel statt Wiederholung.",
            domain="training",
            support=progress_support,
            contradiction=1.0 - progress_support,
            confidence=0.4 + 0.5 * _clamp(_ratio(_safe_float(decisions.get("decision_count_30")), 20.0)),
            evidence=[
                {"label": "Plateau-Entscheidungen 30d", "value": str(plateau_count)},
                {"label": "Live-Decisions 30d", "value": str(decisions.get("live_decisions_30") or 0)},
            ],
            notes="Bei aktivem Stagnationsmuster priorisiert CORE Wechselhebel (Variation, Rep-Range, Lastprofil) vor mehr Volumen.",
        )
    )

    tactical_run_codes = _safe_int(reason_codes.get("CORE_TACTICAL_RUN", 0)) + _safe_int(reason_codes.get("CORE_PROTECT_RUN", 0))
    run_support = _clamp(_ratio(tactical_run_codes, 6.0) * 0.6 + _clamp(_ratio(_safe_float(runs.get("run_days_14")), max(1.0, _safe_float(training.get("workout_days_14")))) ) * 0.4)
    patterns.append(
        _pattern(
            key="run_gym_interference",
            title="Run/Gym-Interferenz aktuell erhöht",
            summary="Run-Steuerung wird häufiger taktisch gebremst; Interferenz mit Kraftfenstern ist derzeit relevant.",
            domain="interference",
            support=run_support,
            contradiction=1.0 - run_support,
            confidence=0.52,
            evidence=[
                {"label": "Tactical/Protect Run Codes", "value": str(tactical_run_codes)},
                {"label": "Run-Tage 14d", "value": str(runs.get("run_days_14") or 0)},
                {"label": "Workout-Tage 14d", "value": str(training.get("workout_days_14") or 0)},
            ],
            notes="Dieses Muster steuert vor allem die Frage, ob Laufqualität heute den Kraftfokus stört oder ergänzt.",
        )
    )

    rmssd_ratio = _safe_float(hrv.get("rmssd_ratio_14"), 1.0)
    hr_ratio = _safe_float(hrv.get("hr_ratio_14"), 1.0)
    recovery_bucket = str(features.get("recovery_state") or "").strip().lower()
    fatigue_support = _clamp((_clamp((1.0 - rmssd_ratio) / 0.18) * 0.45) + (_clamp((hr_ratio - 1.0) / 0.08) * 0.35))
    if recovery_bucket == "low":
        fatigue_support = _clamp(fatigue_support + 0.2)
    patterns.append(
        _pattern(
            key="systemic_fatigue",
            title="Systemische Müdigkeit möglich",
            summary="Recovery-Signale liegen im unteren Bereich; hohe Belastungsentscheidungen bleiben heute angreifbar.",
            domain="recovery",
            support=fatigue_support,
            contradiction=1.0 - fatigue_support,
            confidence=0.4 + 0.5 * _safe_float(hrv.get("hrv_coverage_14"), 0.0),
            evidence=[
                {"label": "RMSSD-Ratio", "value": f"{round(rmssd_ratio, 2)}"},
                {"label": "RHR-Ratio", "value": f"{round(hr_ratio, 2)}"},
                {"label": "Recovery Bucket", "value": recovery_bucket or "unbekannt"},
            ],
            notes="Wenn dieses Muster aktiv ist, priorisiert CORE Wiederholbarkeit über Spitzenreize.",
        )
    )

    data_support = _clamp(coverage_avg)
    patterns.append(
        _pattern(
            key="logging_confidence",
            title="Datenlage heute nur mittel" if coverage_avg < 0.72 else "Datenlage heute belastbar",
            summary=(
                "Die Datenabdeckung ist nutzbar, aber nicht in allen Domänen stabil." if coverage_avg < 0.72 else "Die Datenabdeckung ist breit genug für klare Entscheidungen."
            ),
            domain="system",
            support=data_support,
            contradiction=1.0 - data_support,
            confidence=0.82,
            evidence=[
                {"label": "Coverage-Avg", "value": f"{round(coverage_avg * 100)}%"},
                {
                    "label": "Schwächste Domäne",
                    "value": min(raw.get("coverage", {}).items(), key=lambda item: item[1])[0] if raw.get("coverage") else "unbekannt",
                },
            ],
            notes="Dieses Muster steuert, wie aggressiv CORE heute Schlussfolgerungen zieht und wie stark Unsicherheiten gewichtet werden.",
        )
    )

    return patterns


def _store_observations(conn: sqlite3.Connection, day_iso: str, observations: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM core_observations WHERE substr(observed_at,1,10)=?", (day_iso,))
    conn.executemany(
        """
        INSERT INTO core_observations (
            observed_at, domain, signal_key, signal_value_json, source, quality_score, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["observed_at"],
                item["domain"],
                item["signal_key"],
                item["signal_value_json"],
                item["source"],
                item["quality_score"],
                item["confidence"],
                item["created_at"],
            )
            for item in observations
        ],
    )


def _store_patterns(conn: sqlite3.Connection, day_iso: str, patterns: list[dict[str, Any]]) -> None:
    now = _utc_now()
    for pattern in patterns:
        existing = conn.execute(
            "SELECT id, first_seen, confidence, status FROM core_patterns WHERE pattern_key=?",
            (pattern["pattern_key"],),
        ).fetchone()
        if existing:
            first_seen = str(existing["first_seen"] or day_iso)
            conn.execute(
                """
                UPDATE core_patterns
                SET title=?, summary=?, domain=?, status=?, last_seen=?, confidence=?, stability_score=?,
                    evidence_count=?, support_score=?, contradiction_score=?, evidence_json=?, notes=?, updated_at=?
                WHERE pattern_key=?
                """,
                (
                    pattern["title"],
                    pattern["summary"],
                    pattern["domain"],
                    pattern["status"],
                    day_iso,
                    pattern["confidence"],
                    pattern["stability_score"],
                    pattern["evidence_count"],
                    pattern["support_score"],
                    pattern["contradiction_score"],
                    pattern["evidence_json"],
                    pattern["notes"],
                    now,
                    pattern["pattern_key"],
                ),
            )
            conn.execute(
                "UPDATE core_patterns SET first_seen=? WHERE pattern_key=?",
                (first_seen, pattern["pattern_key"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO core_patterns (
                    pattern_key, title, summary, domain, status, first_seen, last_seen,
                    confidence, stability_score, evidence_count, support_score, contradiction_score,
                    evidence_json, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern["pattern_key"],
                    pattern["title"],
                    pattern["summary"],
                    pattern["domain"],
                    pattern["status"],
                    day_iso,
                    day_iso,
                    pattern["confidence"],
                    pattern["stability_score"],
                    pattern["evidence_count"],
                    pattern["support_score"],
                    pattern["contradiction_score"],
                    pattern["evidence_json"],
                    pattern["notes"],
                    now,
                    now,
                ),
            )


def run_daily_interpretation_pass(day_iso: str | None = None, *, persist: bool = True) -> dict[str, Any]:
    raw = collect_raw_signals(day_iso)
    observations = _build_observations(raw)
    patterns = _build_patterns(raw)

    if persist:
        conn = get_core_db()
        try:
            _store_observations(conn, raw["date"], observations)
            _store_patterns(conn, raw["date"], patterns)
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "date": raw["date"],
        "generated_at": raw["generated_at"],
        "raw": raw,
        "coverage": raw.get("coverage") or {},
        "coverage_avg": raw.get("coverage_avg") or 0.0,
        "observations": observations,
        "patterns": patterns,
    }
