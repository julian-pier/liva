from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import current_app

from core.core_control_room import ensure_core_control_room_schema, get_core_control_room_payload
from core.core_daily_decision import build_core_daily_ui_payload, ensure_core_daily_decision_schema, get_daily_decision
from core.core_explain import explain_decision_v2, explain_hypothesis_v2, explain_pattern_v2
from core.core_learning import run_daily_interpretation_pass
from core.core_model import get_model_state_payload, run_personal_learning_pass
from core.core_night_cycle import load_explain_payload, run_night_cycle
from core.core_review import run_review_pass
from core.core_v2_schema import ensure_core_v2_schema
from database.connections import get_core_db, get_hrv_db, get_nutrition_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_iso_datetime(raw: Any) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


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


def ensure_core_v2_ready() -> None:
    ensure_core_control_room_schema()
    ensure_core_v2_schema()
    ensure_core_daily_decision_schema()


def _latest_decision(day_iso: str | None = None) -> dict[str, Any] | None:
    day_iso = day_iso or _today_iso()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM core_decision_log
            WHERE source IN ('live_today', 'plateau_scan')
              AND day_iso <= ?
            ORDER BY
              day_iso DESC,
              CASE source WHEN 'live_today' THEN 0 ELSE 1 END ASC,
              created_at DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _latest_nutrition_action(day_iso: str) -> dict[str, Any] | None:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT date_iso, action_type, status, reason_text, human_text, reason_codes_json, created_at
            FROM core_nutrition_actions
            WHERE date_iso=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_patterns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM core_patterns ORDER BY confidence DESC, updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def _load_hypotheses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM core_hypotheses ORDER BY confidence DESC, updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def _load_blind_spots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM core_blind_spots ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def _load_observations_for_day(conn: sqlite3.Connection, day_iso: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM core_observations WHERE substr(observed_at,1,10)=? ORDER BY domain, signal_key",
        (day_iso,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["signal_value"] = _parse_json(item.get("signal_value_json"), {})
        out.append(item)
    return out


def _load_latest_review_for_decision(conn: sqlite3.Connection, decision_id: int | None) -> dict[str, Any] | None:
    if not decision_id:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM core_decision_reviews
        WHERE decision_id=?
        ORDER BY review_date DESC, id DESC
        LIMIT 1
        """,
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["expected_vs_actual"] = _parse_json(item.get("expected_vs_actual_json"), {})
    item["should_weaken_reason_codes"] = _parse_json(item.get("should_weaken_reason_codes_json"), [])
    item["should_strengthen_reason_codes"] = _parse_json(item.get("should_strengthen_reason_codes_json"), [])
    return item


def _store_snapshot(
    conn: sqlite3.Connection,
    *,
    day_iso: str,
    raw_context: dict[str, Any],
    patterns: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    decision_summary: dict[str, Any],
    uncertainty: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO core_daily_snapshot_v2 (
            date, raw_context_json, interpreted_patterns_json, active_hypotheses_json,
            decision_summary_json, uncertainty_json, next_review_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            raw_context_json=excluded.raw_context_json,
            interpreted_patterns_json=excluded.interpreted_patterns_json,
            active_hypotheses_json=excluded.active_hypotheses_json,
            decision_summary_json=excluded.decision_summary_json,
            uncertainty_json=excluded.uncertainty_json,
            next_review_at=excluded.next_review_at,
            created_at=excluded.created_at
        """,
        (
            day_iso,
            json.dumps(raw_context or {}, ensure_ascii=False),
            json.dumps(patterns or [], ensure_ascii=False),
            json.dumps(hypotheses or [], ensure_ascii=False),
            json.dumps(decision_summary or {}, ensure_ascii=False),
            json.dumps(uncertainty or {}, ensure_ascii=False),
            (date.fromisoformat(day_iso) + timedelta(days=1)).isoformat(),
            _utc_now(),
        ),
    )


def recompute_core_v2(
    day_iso: str | None = None,
    *,
    run_reviews: bool = True,
    review_days: int = 45,
    model_lookback_days: int = 60,
) -> dict[str, Any]:
    ensure_core_v2_ready()
    learning = run_daily_interpretation_pass(day_iso, persist=True)
    cycle_day = str(learning.get("date") or day_iso or _today_iso())
    try:
        as_of = date.fromisoformat(cycle_day)
    except Exception:
        as_of = date.today()
    coverage = learning.get("coverage") if isinstance(learning.get("coverage"), dict) else {}
    model_update = run_personal_learning_pass(
        coverage=coverage,
        as_of=as_of,
        lookback_days=model_lookback_days,
    )
    review_update = (
        run_review_pass(days=max(7, min(365, int(review_days or 45))), as_of=as_of)
        if run_reviews
        else {"ok": True, "reviewed": 0, "items": []}
    )

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        patterns = [dict(r) for r in conn.execute("SELECT * FROM core_patterns ORDER BY confidence DESC").fetchall()]
        hypotheses = [dict(r) for r in conn.execute("SELECT * FROM core_hypotheses WHERE status IN ('active', 'testing', 'weakened') ORDER BY confidence DESC").fetchall()]
        blind_spots = [dict(r) for r in conn.execute("SELECT * FROM core_blind_spots WHERE status IN ('open','monitoring') ORDER BY updated_at DESC").fetchall()]
        decision = _latest_decision(learning.get("date"))
        decision_summary = {
            "decision_id": _safe_int(decision.get("id") if decision else 0),
            "decision_text": str(decision.get("decision_text") if decision else ""),
            "impact_text": str(decision.get("impact_text") if decision else ""),
            "day_iso": str(decision.get("day_iso") if decision else learning.get("date") or _today_iso()),
        }
        _store_snapshot(
            conn,
            day_iso=str(learning.get("date") or _today_iso()),
            raw_context=learning.get("raw") if isinstance(learning.get("raw"), dict) else {},
            patterns=patterns,
            hypotheses=hypotheses,
            decision_summary=decision_summary,
            uncertainty={
                "blind_spot_count": len(blind_spots),
                "open_blind_spots": [str(x.get("blind_spot_key") or "") for x in blind_spots[:8]],
            },
        )
        conn.commit()
    finally:
        conn.close()

    night_cycle = run_night_cycle(day_iso=cycle_day, apply_parameter_updates=True)

    return {
        "ok": True,
        "date": cycle_day,
        "learning": {
            "observations": len(learning.get("observations") or []),
            "patterns": len(learning.get("patterns") or []),
            "coverage_avg": round(_safe_float(learning.get("coverage_avg")), 3),
        },
        "reviews": {
            "reviewed": _safe_int(review_update.get("reviewed")),
            "days": max(7, min(365, int(review_days or 45))),
        },
        "model": model_update,
        "night_cycle": {
            "ran": bool(night_cycle.get("ok")),
            "simulations": _safe_int((night_cycle.get("night_cycle") or {}).get("simulations_count"), 0),
            "mode": str((night_cycle.get("decision") or {}).get("mode") or ""),
        },
        "generated_at": _utc_now(),
    }


def backfill_night_cycle_history(
    *,
    days: int = 90,
    review_days: int = 180,
    model_lookback_days: int = 365,
    run_reviews: bool = True,
) -> dict[str, Any]:
    ensure_core_v2_ready()
    days = max(7, min(365, int(days or 90)))
    review_days = max(7, min(365, int(review_days or 180)))
    model_lookback_days = max(21, min(730, int(model_lookback_days or 365)))

    end_day = date.today()
    start_day = end_day - timedelta(days=days - 1)

    # Use HRV date span as anchor for retro feed-in.
    hrv_conn = get_hrv_db()
    hrv_conn.row_factory = sqlite3.Row
    try:
        row = hrv_conn.execute(
            """
            SELECT
                MIN(substr(COALESCE(date_utc, ts_measurement), 1, 10)) AS min_day,
                MAX(substr(COALESCE(date_utc, ts_measurement), 1, 10)) AS max_day
            FROM hrv_measurements
            """
        ).fetchone()
    finally:
        hrv_conn.close()

    min_day_raw = str(row["min_day"] if row and "min_day" in row.keys() else "").strip()
    max_day_raw = str(row["max_day"] if row and "max_day" in row.keys() else "").strip()
    if min_day_raw:
        try:
            min_day = date.fromisoformat(min_day_raw)
            if min_day > start_day:
                start_day = min_day
        except Exception:
            pass
    if max_day_raw:
        try:
            max_day = date.fromisoformat(max_day_raw)
            if max_day < end_day:
                end_day = max_day
        except Exception:
            pass

    if start_day > end_day:
        return {
            "ok": False,
            "error": "no_hrv_range",
            "window": {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": 0},
            "generated_at": _utc_now(),
        }

    total = (end_day - start_day).days + 1
    ok_days = 0
    err_days = 0
    reviewed_sum = 0
    last_result: dict[str, Any] | None = None
    first_error: str | None = None

    current = start_day
    while current <= end_day:
        day_iso = current.isoformat()
        try:
            result = recompute_core_v2(
                day_iso=day_iso,
                run_reviews=run_reviews,
                review_days=review_days,
                model_lookback_days=model_lookback_days,
            )
            if result.get("ok"):
                ok_days += 1
                reviewed_sum += _safe_int((result.get("reviews") or {}).get("reviewed"), 0)
                last_result = result
            else:
                err_days += 1
                if first_error is None:
                    first_error = str(result.get("error") or "recompute_failed")
        except Exception as exc:
            err_days += 1
            if first_error is None:
                first_error = str(exc)
        current = current + timedelta(days=1)

    return {
        "ok": err_days == 0,
        "generated_at": _utc_now(),
        "window": {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "days": total,
            "hrv_min_day": min_day_raw or None,
            "hrv_max_day": max_day_raw or None,
        },
        "recompute": {
            "ok_days": ok_days,
            "error_days": err_days,
            "reviews_total": reviewed_sum,
            "first_error": first_error,
        },
        "last": last_result or {},
    }


def _ensure_today_materialized() -> None:
    ensure_core_v2_ready()
    day_iso = _today_iso()
    conn = get_core_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM core_observations WHERE substr(observed_at,1,10)=? LIMIT 1",
            (day_iso,),
        ).fetchone()
        explain_row = conn.execute(
            "SELECT created_at FROM core_decision_explain_v2 WHERE date=? LIMIT 1",
            (day_iso,),
        ).fetchone()
        latest_observation_at = conn.execute(
            "SELECT MAX(observed_at) AS ts FROM core_observations WHERE substr(observed_at,1,10)=?",
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        recompute_core_v2(day_iso, run_reviews=True)
    elif not explain_row:
        run_night_cycle(day_iso=day_iso, apply_parameter_updates=True)
    else:
        explain_created_at = _parse_iso_datetime(explain_row["created_at"] if explain_row else None)
        latest_obs_raw = latest_observation_at["ts"] if latest_observation_at is not None else None
        latest_obs_at = _parse_iso_datetime(latest_obs_raw)
        now_utc = datetime.now(timezone.utc)
        stale_by_age = (
            explain_created_at is None
            or (now_utc - explain_created_at) > timedelta(minutes=90)
        )
        stale_by_new_inputs = bool(
            explain_created_at is not None
            and latest_obs_at is not None
            and latest_obs_at > (explain_created_at + timedelta(minutes=2))
        )
        if stale_by_age or stale_by_new_inputs:
            run_night_cycle(day_iso=day_iso, apply_parameter_updates=True)


def get_explain_v2_payload(day_iso: str | None = None, *, force_rebuild: bool = False) -> dict[str, Any]:
    _ensure_today_materialized()
    day_iso = day_iso or _today_iso()
    if force_rebuild:
        return run_night_cycle(day_iso=day_iso, apply_parameter_updates=True)
    payload = load_explain_payload(day_iso)
    if isinstance(payload, dict) and payload.get("ok"):
        daily_ui = build_core_daily_ui_payload(day_iso)
        payload["daily_ui"] = daily_ui
        payload["daily_decision"] = daily_ui.get("daily_decision") if isinstance(daily_ui, dict) else (get_daily_decision(day_iso) or {})
        return payload
    return run_night_cycle(day_iso=day_iso, apply_parameter_updates=True)


def get_today_payload(day_iso: str | None = None) -> dict[str, Any]:
    _ensure_today_materialized()
    day_iso = day_iso or _today_iso()
    explain_v2 = get_explain_v2_payload(day_iso)

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        observations = _load_observations_for_day(conn, day_iso)
        patterns = _load_patterns(conn)
        hypotheses = _load_hypotheses(conn)
        blind_spots = _load_blind_spots(conn)
        decision = _latest_decision(day_iso)
        review = _load_latest_review_for_decision(conn, _safe_int(decision.get("id") if decision else 0)) if decision else None
    finally:
        conn.close()

    explained_patterns = [explain_pattern_v2(p) for p in patterns]
    explained_hyp = [explain_hypothesis_v2(h) for h in hypotheses]

    coverage_obs = next((o for o in observations if o.get("signal_key") == "system.coverage"), None)
    coverage_payload = coverage_obs.get("signal_value") if coverage_obs else {}
    coverage_map = coverage_payload.get("coverage") if isinstance(coverage_payload.get("coverage"), dict) else {}
    coverage_avg = _safe_float(coverage_payload.get("avg"), 0.0)

    covered_good = [k for k, v in coverage_map.items() if _safe_float(v) >= 0.6]
    covered_weak = [k for k, v in coverage_map.items() if _safe_float(v) < 0.45]

    nutrition_action = _latest_nutrition_action(day_iso)

    actions: list[dict[str, Any]] = []
    if decision:
        decision_text = str(decision.get("decision_text") or "").strip()
        impact = str(decision.get("impact_text") or "").strip() or "Heute bleibt CORE bei kontrollierter Standardsteuerung."
        actions.append(
            {
                "domain": str(decision.get("type") or "System"),
                "action": decision_text or impact,
                "confidence": round(0.45 + coverage_avg * 0.45, 3),
                "expected_effect": impact,
                "later_check": "Im Review-Fenster prüfen, ob die Maßnahme Leistung/Recovery stabil gehalten hat.",
            }
        )

    if nutrition_action:
        actions.append(
            {
                "domain": "Nutrition",
                "action": str(nutrition_action.get("human_text") or nutrition_action.get("action_type") or "Nutrition-Anpassung"),
                "confidence": 0.56,
                "expected_effect": str(nutrition_action.get("reason_text") or "Ernährungsseite für den Tag stabilisieren."),
                "later_check": "Prüfen, ob die Aktion später bestätigt oder verworfen wurde.",
            }
        )

    active_patterns = [p for p in explained_patterns if p["status"] in {"active", "monitoring"}]
    active_hyp = [h for h in explained_hyp if h["status"] in {"active", "testing", "weakened"}]

    pattern_keys = {str(p.get("pattern_key") or "") for p in active_patterns}
    if {"systemic_fatigue", "nutrition_instability"}.issubset(pattern_keys):
        assessment_line = "Heute bremst CORE, weil Recovery und Ernährung gleichzeitig fragil wirken."
    elif "systemic_fatigue" in pattern_keys:
        assessment_line = "Heute bleibt CORE vorsichtig, weil das Recovery-Bild noch keine harte Freigabe trägt."
    elif "nutrition_instability" in pattern_keys:
        assessment_line = "Heute ist Push möglich, aber die Ernährungsseite macht die Entscheidung deutlich fragiler."
    elif "progress_stagnation_active_plan" in pattern_keys:
        assessment_line = "Heute priorisiert CORE Strukturwechsel statt noch mehr vom gleichen Reiz."
    elif actions:
        assessment_line = actions[0]["expected_effect"]
    elif active_patterns:
        assessment_line = active_patterns[0]["summary"]
    else:
        assessment_line = "CORE hat heute noch kein belastbares Signalbild und fährt konservativ."

    uncertainties = [
        {
            "title": str(spot.get("title") or "Unsicherheit"),
            "description": str(spot.get("description") or ""),
            "recommendation": str(spot.get("recommendation") or ""),
        }
        for spot in blind_spots
        if str(spot.get("status") or "") in {"open", "monitoring"}
    ]

    decision_explain = explain_decision_v2(
        decision or {"id": None, "day_iso": day_iso, "type": "System", "severity": "Daily micro", "decision_text": "", "impact_text": ""},
        patterns=patterns,
        hypotheses=hypotheses,
        blind_spots=blind_spots,
        review=review,
    )

    fallback = None
    if coverage_avg < 0.32:
        try:
            legacy = get_core_control_room_payload(days=14)
        except Exception:
            legacy = {"today": {"status": "VORSICHT", "today": "Zu wenig Daten für v2."}}
        fallback = {
            "active": True,
            "reason": "Zu wenig belastbare v2-Daten für klare Muster.",
            "legacy_today": legacy.get("today") if isinstance(legacy, dict) else {},
        }

    checks_later = [
        "Ob die heutige Maßnahme morgen weiter tragfähig wirkt (nicht nur heute).",
        "Ob aktive Muster bestätigt oder abgeschwächt werden müssen.",
    ]
    if decision:
        checks_later.append(f"Decision #{decision.get('id')} bekommt ein Outcome-Review mit Grundcode-Abgleich.")

    payload = {
        "ok": True,
        "generated_at": _utc_now(),
        "date": day_iso,
        "daily_ui": build_core_daily_ui_payload(day_iso),
        "daily_decision": get_daily_decision(day_iso) or {},
        "assessment": {
            "headline": assessment_line,
            "summary": decision_explain.get("explain_text"),
        },
        "actions": actions,
        "why": {
            "raw_signals": [
                {
                    "domain": str(o.get("domain") or "system"),
                    "signal_key": str(o.get("signal_key") or ""),
                    "value": o.get("signal_value") or {},
                    "source": str(o.get("source") or ""),
                    "confidence": _safe_float(o.get("confidence"), 0.0),
                }
                for o in observations
            ],
            "patterns": active_patterns[:8],
            "learned_rules": active_hyp[:8],
            "uncertainties": uncertainties,
        },
        "data_coverage": {
            "avg": round(coverage_avg, 3),
            "good_coverage": covered_good,
            "weak_coverage": covered_weak,
            "coverage": coverage_map,
            "summary": (
                "Datenlage ist breit genug für konkrete Steuerung." if coverage_avg >= 0.68 else "Datenlage ist mittel; Aussagen sind nutzbar, aber nicht in allen Domänen gleich belastbar."
            ),
        },
        "review_next": checks_later,
        "decision_explain": decision_explain,
        "fallback": fallback,
        "explain_v2": explain_v2,
    }
    if explain_v2.get("ok"):
        if isinstance(explain_v2.get("daily_decision"), dict):
            payload["daily_decision"] = explain_v2.get("daily_decision") or payload["daily_decision"]
        decision_block = explain_v2.get("decision") if isinstance(explain_v2.get("decision"), dict) else {}
        clone_block = explain_v2.get("clone") if isinstance(explain_v2.get("clone"), dict) else {}
        learning_block = explain_v2.get("learning") if isinstance(explain_v2.get("learning"), dict) else {}
        night_block = explain_v2.get("night_cycle") if isinstance(explain_v2.get("night_cycle"), dict) else {}
        payload["assessment"]["headline"] = str(decision_block.get("subtitle") or payload["assessment"]["headline"])
        payload["assessment"]["summary"] = str(clone_block.get("summary") or payload["assessment"]["summary"])
        payload["review_next"] = [
            str(night_block.get("summary") or ""),
            str(learning_block.get("quality_summary") or ""),
        ] + [line for line in payload["review_next"] if line][:1]
    return payload


def get_learned_payload() -> dict[str, Any]:
    _ensure_today_materialized()

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        hypotheses = [dict(r) for r in conn.execute("SELECT * FROM core_hypotheses ORDER BY updated_at DESC").fetchall()]
        patterns = [dict(r) for r in conn.execute("SELECT * FROM core_patterns ORDER BY updated_at DESC").fetchall()]
        changes = [dict(r) for r in conn.execute("SELECT * FROM core_model_updates ORDER BY created_at DESC LIMIT 120").fetchall()]
    finally:
        conn.close()

    hyp_items = [explain_hypothesis_v2(h) for h in hypotheses]
    pat_items = [explain_pattern_v2(p) for p in patterns]

    stable = [
        x
        for x in hyp_items
        if x["status"] == "active" and x["confidence"] >= 0.68
    ] + [
        x
        for x in pat_items
        if x["status"] == "active" and x["stability_score"] >= 0.68
    ]

    new_cutoff = (date.today() - timedelta(days=14)).isoformat()
    new_items = [
        x for x in hyp_items if str(x.get("last_reviewed_at") or "") >= new_cutoff
    ] + [
        x for x in pat_items if str(x.get("last_seen") or "") >= new_cutoff and str(x.get("first_seen") or "") >= new_cutoff
    ]

    changing = [x for x in hyp_items if x["status"] == "testing"] + [x for x in pat_items if x["status"] == "monitoring"]
    weakened = [x for x in hyp_items if x["status"] in {"weakened", "rejected"}] + [x for x in pat_items if x["status"] in {"weak", "discarded"}]
    open_hypotheses = [x for x in hyp_items if x["status"] == "testing" and x["confidence"] < 0.62]

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "stable": stable[:20],
        "new": new_items[:20],
        "changing": changing[:20],
        "weakened": weakened[:20],
        "open_hypotheses": open_hypotheses[:20],
        "recent_changes": [
            {
                "date": str(c.get("update_date") or ""),
                "type": str(c.get("update_type") or ""),
                "object": f"{c.get('object_type')}:{c.get('object_key')}",
                "reason": str(c.get("reason_summary") or ""),
            }
            for c in changes[:25]
        ],
    }


def get_reviews_payload(*, domain: str | None = None, outcome: str | None = None, days: int = 30) -> dict[str, Any]:
    _ensure_today_materialized()

    since = (date.today() - timedelta(days=max(7, min(365, days)))).isoformat()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH latest_reviews AS (
                SELECT r.*
                FROM core_decision_reviews r
                JOIN (
                    SELECT decision_id, MAX(review_date) AS review_date
                    FROM core_decision_reviews
                    GROUP BY decision_id
                ) x
                  ON x.decision_id = r.decision_id
                 AND x.review_date = r.review_date
            )
            SELECT d.id AS decision_id, d.day_iso, d.type, d.severity, d.decision_text, d.impact_text, d.why_json,
                   lr.review_date, lr.outcome_label, lr.outcome_score,
                   lr.expected_vs_actual_json, lr.what_worked, lr.what_failed,
                   lr.should_repeat, lr.should_weaken_reason_codes_json,
                   lr.should_strengthen_reason_codes_json, lr.generated_summary
            FROM core_decision_log d
            LEFT JOIN latest_reviews lr ON lr.decision_id = d.id
            WHERE d.day_iso >= ?
              AND d.source IN ('live_today', 'plateau_scan')
            ORDER BY d.day_iso DESC, d.created_at DESC
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["why"] = _parse_json(item.get("why_json"), [])
        item["expected_vs_actual"] = _parse_json(item.get("expected_vs_actual_json"), {})
        item["should_weaken_reason_codes"] = _parse_json(item.get("should_weaken_reason_codes_json"), [])
        item["should_strengthen_reason_codes"] = _parse_json(item.get("should_strengthen_reason_codes_json"), [])
        items.append(item)

    if domain:
        d = domain.strip().lower()
        items = [x for x in items if str(x.get("type") or "").strip().lower() == d]
    if outcome:
        o = outcome.strip().lower()
        items = [x for x in items if str(x.get("outcome_label") or "").strip().lower() == o]

    summary_counts: dict[str, int] = {}
    for item in items:
        label = str(item.get("outcome_label") or "inconclusive")
        summary_counts[label] = summary_counts.get(label, 0) + 1

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "filters": {
            "domain": domain or "all",
            "outcome": outcome or "all",
            "days": days,
        },
        "summary": summary_counts,
        "items": items,
    }


def get_timeline_payload(*, days: int = 60) -> dict[str, Any]:
    _ensure_today_materialized()
    since = (date.today() - timedelta(days=max(14, min(365, days)))).isoformat()

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        decisions = [dict(r) for r in conn.execute(
            "SELECT id, day_iso, type, severity, decision_text, impact_text, created_at FROM core_decision_log WHERE day_iso >= ? ORDER BY created_at DESC LIMIT 120",
            (since,),
        ).fetchall()]
        patterns = [dict(r) for r in conn.execute(
            "SELECT id, pattern_key, title, status, domain, confidence, updated_at FROM core_patterns WHERE last_seen >= ? ORDER BY updated_at DESC LIMIT 120",
            (since,),
        ).fetchall()]
        updates = [dict(r) for r in conn.execute(
            "SELECT id, update_date, update_type, object_type, object_key, reason_summary, created_at FROM core_model_updates WHERE update_date >= ? ORDER BY created_at DESC LIMIT 120",
            (since,),
        ).fetchall()]
        reviews = [dict(r) for r in conn.execute(
            "SELECT id, decision_id, review_date, outcome_label, generated_summary, created_at FROM core_decision_reviews WHERE review_date >= ? ORDER BY created_at DESC LIMIT 120",
            (since,),
        ).fetchall()]
    finally:
        conn.close()

    events: list[dict[str, Any]] = []

    for d in decisions:
        events.append(
            {
                "ts": str(d.get("created_at") or f"{d.get('day_iso')}T00:00:00Z"),
                "day": str(d.get("day_iso") or ""),
                "kind": "decision",
                "title": str(d.get("decision_text") or "CORE Entscheidung"),
                "summary": str(d.get("impact_text") or ""),
                "domain": str(d.get("type") or "System"),
                "ref": {"decision_id": d.get("id")},
            }
        )

    for p in patterns:
        events.append(
            {
                "ts": str(p.get("updated_at") or ""),
                "day": str(p.get("updated_at") or "")[:10],
                "kind": "pattern",
                "title": str(p.get("title") or p.get("pattern_key") or "Pattern"),
                "summary": f"Status {p.get('status')} · Confidence {round(_safe_float(p.get('confidence')) * 100)}%",
                "domain": str(p.get("domain") or "system"),
                "ref": {"pattern_id": p.get("id")},
            }
        )

    for u in updates:
        events.append(
            {
                "ts": str(u.get("created_at") or ""),
                "day": str(u.get("update_date") or ""),
                "kind": "model_update",
                "title": f"{u.get('object_type')} {u.get('object_key')}",
                "summary": f"{u.get('update_type')}: {u.get('reason_summary')}",
                "domain": str(u.get("object_type") or "system"),
                "ref": {"update_id": u.get("id")},
            }
        )

    for r in reviews:
        events.append(
            {
                "ts": str(r.get("created_at") or ""),
                "day": str(r.get("review_date") or ""),
                "kind": "review",
                "title": f"Review zu Decision #{r.get('decision_id')}",
                "summary": f"{r.get('outcome_label')}: {r.get('generated_summary')}",
                "domain": "review",
                "ref": {"review_id": r.get("id"), "decision_id": r.get("decision_id")},
            }
        )

    events.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "items": events[:220],
    }


def get_decision_explain_payload(decision_id: int) -> dict[str, Any]:
    _ensure_today_materialized()

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        decision_row = conn.execute("SELECT * FROM core_decision_log WHERE id=?", (decision_id,)).fetchone()
        if not decision_row:
            return {"ok": False, "error": "decision_not_found"}
        decision = dict(decision_row)
        patterns = _load_patterns(conn)
        hypotheses = _load_hypotheses(conn)
        blind_spots = _load_blind_spots(conn)
        review = _load_latest_review_for_decision(conn, decision_id)
    finally:
        conn.close()

    explain = explain_decision_v2(decision, patterns=patterns, hypotheses=hypotheses, blind_spots=blind_spots, review=review)
    return {"ok": True, "decision": explain}


def get_hypothesis_payload(hypothesis_id: int) -> dict[str, Any]:
    _ensure_today_materialized()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM core_hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "hypothesis_not_found"}
        return {"ok": True, "hypothesis": explain_hypothesis_v2(dict(row))}
    finally:
        conn.close()


def get_pattern_payload(pattern_id: int) -> dict[str, Any]:
    _ensure_today_materialized()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM core_patterns WHERE id=?", (pattern_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "pattern_not_found"}
        return {"ok": True, "pattern": explain_pattern_v2(dict(row))}
    finally:
        conn.close()


def get_decision_v2_payload(day_iso: str | None = None) -> dict[str, Any]:
    payload = get_explain_v2_payload(day_iso)
    daily_ui = build_core_daily_ui_payload(day_iso)
    return {
        "ok": bool(payload.get("ok")),
        "generated_at": payload.get("generated_at") or _utc_now(),
        "date": payload.get("date") or (day_iso or _today_iso()),
        "decision": payload.get("decision") if isinstance(payload.get("decision"), dict) else {},
        "daily_decision": daily_ui.get("daily_decision") if isinstance(daily_ui, dict) else (get_daily_decision(day_iso) or {}),
        "daily_ui": daily_ui if isinstance(daily_ui, dict) else {},
    }


def get_night_cycle_payload(day_iso: str | None = None) -> dict[str, Any]:
    payload = get_explain_v2_payload(day_iso)
    return {
        "ok": bool(payload.get("ok")),
        "generated_at": payload.get("generated_at") or _utc_now(),
        "date": payload.get("date") or (day_iso or _today_iso()),
        "night_cycle": payload.get("night_cycle") if isinstance(payload.get("night_cycle"), dict) else {},
    }


def get_clone_payload(day_iso: str | None = None) -> dict[str, Any]:
    payload = get_explain_v2_payload(day_iso)
    return {
        "ok": bool(payload.get("ok")),
        "generated_at": payload.get("generated_at") or _utc_now(),
        "date": payload.get("date") or (day_iso or _today_iso()),
        "clone": payload.get("clone") if isinstance(payload.get("clone"), dict) else {},
    }


def get_scenarios_payload(day_iso: str | None = None) -> dict[str, Any]:
    payload = get_explain_v2_payload(day_iso)
    return {
        "ok": bool(payload.get("ok")),
        "generated_at": payload.get("generated_at") or _utc_now(),
        "date": payload.get("date") or (day_iso or _today_iso()),
        "scenarios": payload.get("scenarios") if isinstance(payload.get("scenarios"), dict) else {"list": []},
    }


def get_parameter_learning_payload(day_iso: str | None = None) -> dict[str, Any]:
    payload = get_explain_v2_payload(day_iso)
    return {
        "ok": bool(payload.get("ok")),
        "generated_at": payload.get("generated_at") or _utc_now(),
        "date": payload.get("date") or (day_iso or _today_iso()),
        "learning": payload.get("learning") if isinstance(payload.get("learning"), dict) else {},
    }


def get_bootstrap_payload() -> dict[str, Any]:
    try:
        today = get_today_payload()
        explain_v2 = get_explain_v2_payload(today.get("date"))
        learned = get_learned_payload()
        model = get_model_state_payload()
        reviews = get_reviews_payload(days=30)
        timeline = get_timeline_payload(days=45)
        return {
            "ok": True,
            "generated_at": _utc_now(),
            "today": today,
            "explain_v2": explain_v2,
            "learned": learned,
            "reviews": reviews,
            "model": model,
            "timeline": timeline,
        }
    except Exception as exc:
        try:
            current_app.logger.exception("core v2 bootstrap failed")
        except Exception:
            pass
        return {
            "ok": False,
            "generated_at": _utc_now(),
            "error": str(exc),
        }
