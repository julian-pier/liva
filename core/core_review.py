from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any

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
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _window_metrics(decision_day: date) -> dict[str, Any]:
    before_start = (decision_day - timedelta(days=3)).isoformat()
    before_end = (decision_day - timedelta(days=1)).isoformat()
    after_start = (decision_day + timedelta(days=1)).isoformat()
    after_end = (decision_day + timedelta(days=3)).isoformat()

    training_conn = get_training_db()
    runs_conn = get_runs_db()
    nutri_conn = get_nutrition_db()
    hrv_conn = get_hrv_db()
    training_conn.row_factory = sqlite3.Row
    runs_conn.row_factory = sqlite3.Row
    nutri_conn.row_factory = sqlite3.Row
    hrv_conn.row_factory = sqlite3.Row

    try:
        tr_before = training_conn.execute(
            "SELECT COUNT(DISTINCT w.id) AS c, AVG(COALESCE(s.rpe, 0)) AS avg_rpe FROM workouts w LEFT JOIN sets s ON s.workout_id=w.id WHERE w.date_iso BETWEEN ? AND ?",
            (before_start, before_end),
        ).fetchone()
        tr_after = training_conn.execute(
            "SELECT COUNT(DISTINCT w.id) AS c, AVG(COALESCE(s.rpe, 0)) AS avg_rpe FROM workouts w LEFT JOIN sets s ON s.workout_id=w.id WHERE w.date_iso BETWEEN ? AND ?",
            (after_start, after_end),
        ).fetchone()

        run_before = runs_conn.execute(
            "SELECT COUNT(*) AS c, SUM(distance) AS km FROM runs WHERE substr(date,1,10) BETWEEN ? AND ?",
            (before_start, before_end),
        ).fetchone()
        run_after = runs_conn.execute(
            "SELECT COUNT(*) AS c, SUM(distance) AS km FROM runs WHERE substr(date,1,10) BETWEEN ? AND ?",
            (after_start, after_end),
        ).fetchone()

        nutri_before = nutri_conn.execute(
            "SELECT COUNT(*) AS c, SUM(CASE WHEN kcal IS NOT NULL THEN 1 ELSE 0 END) AS logged FROM nutrition_daily WHERE date_iso BETWEEN ? AND ?",
            (before_start, before_end),
        ).fetchone()
        nutri_after = nutri_conn.execute(
            "SELECT COUNT(*) AS c, SUM(CASE WHEN kcal IS NOT NULL THEN 1 ELSE 0 END) AS logged FROM nutrition_daily WHERE date_iso BETWEEN ? AND ?",
            (after_start, after_end),
        ).fetchone()

        hrv_before_rows = hrv_conn.execute(
            "SELECT rmssd, hr FROM hrv_measurements WHERE substr(date_utc,1,10) BETWEEN ? AND ?",
            (before_start, before_end),
        ).fetchall()
        hrv_after_rows = hrv_conn.execute(
            "SELECT rmssd, hr FROM hrv_measurements WHERE substr(date_utc,1,10) BETWEEN ? AND ?",
            (after_start, after_end),
        ).fetchall()
    finally:
        training_conn.close()
        runs_conn.close()
        nutri_conn.close()
        hrv_conn.close()

    rmssd_before = [float(r["rmssd"]) for r in hrv_before_rows if r["rmssd"] is not None]
    rmssd_after = [float(r["rmssd"]) for r in hrv_after_rows if r["rmssd"] is not None]
    hr_before = [float(r["hr"]) for r in hrv_before_rows if r["hr"] is not None]
    hr_after = [float(r["hr"]) for r in hrv_after_rows if r["hr"] is not None]

    return {
        "training_before": {
            "sessions": _safe_int(tr_before["c"] if tr_before else 0),
            "avg_rpe": _safe_float(tr_before["avg_rpe"] if tr_before else 0.0),
        },
        "training_after": {
            "sessions": _safe_int(tr_after["c"] if tr_after else 0),
            "avg_rpe": _safe_float(tr_after["avg_rpe"] if tr_after else 0.0),
        },
        "runs_before": {
            "sessions": _safe_int(run_before["c"] if run_before else 0),
            "km": round(_safe_float(run_before["km"] if run_before else 0.0), 2),
        },
        "runs_after": {
            "sessions": _safe_int(run_after["c"] if run_after else 0),
            "km": round(_safe_float(run_after["km"] if run_after else 0.0), 2),
        },
        "nutrition_before": {
            "days": _safe_int(nutri_before["c"] if nutri_before else 0),
            "logged_days": _safe_int(nutri_before["logged"] if nutri_before else 0),
        },
        "nutrition_after": {
            "days": _safe_int(nutri_after["c"] if nutri_after else 0),
            "logged_days": _safe_int(nutri_after["logged"] if nutri_after else 0),
        },
        "recovery_before": {
            "rmssd": round(mean(rmssd_before), 2) if rmssd_before else None,
            "hr": round(mean(hr_before), 2) if hr_before else None,
            "n": len(rmssd_before),
        },
        "recovery_after": {
            "rmssd": round(mean(rmssd_after), 2) if rmssd_after else None,
            "hr": round(mean(hr_after), 2) if hr_after else None,
            "n": len(rmssd_after),
        },
    }


def _decision_reason_codes(decision: dict[str, Any]) -> list[str]:
    raw = _parse_json(decision.get("raw_json"), {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    return [str(code or "").strip().upper() for code in (meta.get("reason_codes") or []) if str(code or "").strip()]


def review_decision_v2(decision: dict[str, Any], *, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    decision_day = _date_from_any(decision.get("day_iso"))
    if decision_day is None:
        decision_day = as_of

    reason_codes = _decision_reason_codes(decision)
    is_push = any(code in {"CORE_PUSH_GYM", "CORE_PUSH_RUN", "CORE_PROGRESS_WINDOW"} for code in reason_codes)
    is_protect = any(
        code in {"CORE_EMERGENCY_LIGHT", "CORE_TACTICAL_NORMAL", "CORE_TACTICAL_RUN", "CORE_PROTECT_RUN", "CORE_REENTRY_GYM", "CORE_REENTRY_RUN"}
        for code in reason_codes
    ) or "rest" in str(decision.get("decision_text") or "").lower()

    if (as_of - decision_day).days < 2:
        return {
            "decision_id": _safe_int(decision.get("id")),
            "review_date": as_of.isoformat(),
            "outcome_label": "inconclusive",
            "outcome_score": 0.45,
            "expected_vs_actual": {
                "expected": "Folgedaten noch nicht stabil genug für fairen Review.",
                "actual": "Zeitfenster < 48h",
            },
            "what_worked": "Noch offen",
            "what_failed": "Noch offen",
            "should_repeat": False,
            "should_weaken_reason_codes": [],
            "should_strengthen_reason_codes": [],
            "generated_summary": "Für diese Entscheidung ist das Outcome-Fenster noch zu kurz; CORE verschiebt die Bewertung.",
        }

    metrics = _window_metrics(decision_day)

    rmssd_before = _safe_float(metrics["recovery_before"].get("rmssd"), 0.0)
    rmssd_after = _safe_float(metrics["recovery_after"].get("rmssd"), 0.0)
    hr_before = _safe_float(metrics["recovery_before"].get("hr"), 0.0)
    hr_after = _safe_float(metrics["recovery_after"].get("hr"), 0.0)

    rmssd_delta = ((rmssd_after - rmssd_before) / rmssd_before) if rmssd_before > 0 else 0.0
    hr_delta = ((hr_after - hr_before) / hr_before) if hr_before > 0 else 0.0

    train_after = _safe_int(metrics["training_after"].get("sessions"))
    run_after = _safe_int(metrics["runs_after"].get("sessions"))

    nutri_after_days = _safe_int(metrics["nutrition_after"].get("days"))
    nutri_after_logged = _safe_int(metrics["nutrition_after"].get("logged_days"))
    nutri_after_cov = (nutri_after_logged / nutri_after_days) if nutri_after_days > 0 else 0.0

    outcome_score = 0.5
    worked: list[str] = []
    failed: list[str] = []

    if rmssd_delta >= 0.03:
        outcome_score += 0.12
        worked.append("Recovery (RMSSD) hat sich im Review-Fenster verbessert")
    elif rmssd_delta <= -0.08:
        outcome_score -= 0.16
        failed.append("Recovery (RMSSD) ist im Review-Fenster deutlich gefallen")

    if hr_before > 0 and hr_after > 0:
        if hr_delta <= -0.02:
            outcome_score += 0.08
            worked.append("Ruhiger Pulsverlauf nach der Maßnahme")
        elif hr_delta >= 0.05:
            outcome_score -= 0.09
            failed.append("Pulsverlauf wurde nach der Maßnahme schlechter")

    if train_after + run_after >= 1:
        outcome_score += 0.08
        worked.append("Belastung blieb umsetzbar (mindestens eine Session im Review-Fenster)")
    else:
        outcome_score -= 0.08
        failed.append("Nach der Entscheidung war kaum Folge-Umsetzung sichtbar")

    if nutri_after_cov >= 0.66:
        outcome_score += 0.05
        worked.append("Ernährungslogging blieb tragfähig")
    elif nutri_after_days > 0 and nutri_after_cov < 0.34:
        outcome_score -= 0.05
        failed.append("Ernährungsseite blieb im Outcome-Fenster labil")

    outcome_score = max(0.0, min(1.0, round(outcome_score, 3)))

    if is_push:
        if rmssd_delta <= -0.08 or hr_delta >= 0.06:
            label = "too_aggressive"
        elif outcome_score >= 0.64:
            label = "good"
        elif outcome_score <= 0.42:
            label = "bad"
        else:
            label = "mixed"
    elif is_protect:
        if outcome_score >= 0.68:
            label = "good"
        elif outcome_score <= 0.36:
            label = "bad"
        elif outcome_score <= 0.48 and train_after + run_after == 0 and rmssd_delta >= 0.0:
            label = "too_conservative"
        else:
            label = "mixed"
    else:
        if outcome_score >= 0.64:
            label = "good"
        elif outcome_score <= 0.38:
            label = "bad"
        else:
            label = "mixed"

    should_repeat = label in {"good"}
    strengthen = reason_codes if label in {"good"} else []
    weaken = reason_codes if label in {"bad", "too_aggressive", "too_conservative"} else []

    expected = ""
    if is_push:
        expected = "Push sollte Leistung ermöglichen, ohne Recovery unnötig zu kippen."
    elif is_protect:
        expected = "Schutzentscheidung sollte Recovery stabilisieren und Folgetag planbar machen."
    else:
        expected = "Entscheidung sollte Tagessteuerung stabilisieren."

    actual = (
        f"RMSSD-Delta {round(rmssd_delta * 100)}%, "
        f"HR-Delta {round(hr_delta * 100)}%, "
        f"Sessions danach {train_after + run_after}, Nutrition-Coverage {round(nutri_after_cov * 100)}%"
    )

    if not worked:
        worked.append("Kein klarer positiver Hebel nachweisbar")
    if not failed:
        failed.append("Kein dominanter Ausfallfaktor sichtbar")

    summary = (
        f"Review {decision_day.isoformat()}: Ergebnis {label}. "
        f"{worked[0]}. "
        f"{failed[0]}."
    )

    return {
        "decision_id": _safe_int(decision.get("id")),
        "review_date": as_of.isoformat(),
        "outcome_label": label,
        "outcome_score": outcome_score,
        "expected_vs_actual": {
            "expected": expected,
            "actual": actual,
            "metrics": metrics,
        },
        "what_worked": "; ".join(worked),
        "what_failed": "; ".join(failed),
        "should_repeat": should_repeat,
        "should_weaken_reason_codes": weaken,
        "should_strengthen_reason_codes": strengthen,
        "generated_summary": summary,
    }


def _store_review(conn: sqlite3.Connection, review: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO core_decision_reviews (
            decision_id, review_date, outcome_label, outcome_score, expected_vs_actual_json,
            what_worked, what_failed, should_repeat,
            should_weaken_reason_codes_json, should_strengthen_reason_codes_json,
            generated_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id, review_date) DO UPDATE SET
            outcome_label=excluded.outcome_label,
            outcome_score=excluded.outcome_score,
            expected_vs_actual_json=excluded.expected_vs_actual_json,
            what_worked=excluded.what_worked,
            what_failed=excluded.what_failed,
            should_repeat=excluded.should_repeat,
            should_weaken_reason_codes_json=excluded.should_weaken_reason_codes_json,
            should_strengthen_reason_codes_json=excluded.should_strengthen_reason_codes_json,
            generated_summary=excluded.generated_summary
        """,
        (
            review["decision_id"],
            review["review_date"],
            review["outcome_label"],
            review["outcome_score"],
            json.dumps(review.get("expected_vs_actual") or {}, ensure_ascii=False),
            review.get("what_worked") or "",
            review.get("what_failed") or "",
            1 if review.get("should_repeat") else 0,
            json.dumps(review.get("should_weaken_reason_codes") or [], ensure_ascii=False),
            json.dumps(review.get("should_strengthen_reason_codes") or [], ensure_ascii=False),
            review.get("generated_summary") or "",
            _utc_now(),
        ),
    )


def run_review_pass(*, days: int = 45, as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    since = (as_of - timedelta(days=max(7, days))).isoformat()

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        decision_rows = conn.execute(
            """
            SELECT *
            FROM core_decision_log
            WHERE day_iso >= ?
              AND source IN ('live_today', 'plateau_scan')
            ORDER BY day_iso DESC, created_at DESC
            """,
            (since,),
        ).fetchall()

        reviews: list[dict[str, Any]] = []
        for row in decision_rows:
            review = review_decision_v2(dict(row), as_of=as_of)
            reviews.append(review)
            _store_review(conn, review)

        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "review_date": as_of.isoformat(),
        "reviewed": len(reviews),
        "items": reviews,
    }
