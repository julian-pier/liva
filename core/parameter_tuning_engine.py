from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


DEFAULT_PARAMETERS: dict[str, float] = {
    "sleep": 0.14,
    "hrv": 0.16,
    "nutrition": 0.14,
    "run_interference": 0.12,
    "fatigue": 0.15,
    "back_to_back": 0.11,
    "stress": 0.09,
    "simulation": 0.09,
}

PARAMETER_MIN = 0.04
PARAMETER_MAX = 0.35
MAX_DELTA_PER_UPDATE = 0.03
MIN_REVIEWS_FOR_UPDATE = 8
MIN_SIM_FEEDBACK_FOR_UPDATE = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _normalize(params: dict[str, float]) -> dict[str, float]:
    base = {k: _clamp(_safe_float(v, 0.0), PARAMETER_MIN, PARAMETER_MAX) for k, v in params.items()}
    total = sum(base.values()) or 1.0
    return {k: round(v / total, 4) for k, v in base.items()}


def _extract_reason_codes(raw_json: Any) -> list[str]:
    raw = _parse_json(raw_json, {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    return [str(code or "").strip().upper() for code in (meta.get("reason_codes") or []) if str(code or "").strip()]


def _extract_mode(raw_json: Any) -> str:
    raw = _parse_json(raw_json, {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    mode = str(meta.get("autopilot_mode_effective") or meta.get("autopilot_mode_suggested") or "").strip().upper()
    if mode == "PUSH":
        return "HEAVY"
    if mode in {"HEAVY", "NORMAL", "LIGHT", "REST"}:
        return mode
    return "NORMAL"


def get_active_parameters(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, Any] | None]:
    row = conn.execute(
        """
        SELECT *
        FROM core_parameter_versions
        WHERE active=1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return (_normalize(dict(DEFAULT_PARAMETERS)), None)
    payload = _parse_json(row["parameter_json"], {})
    merged = dict(DEFAULT_PARAMETERS)
    for key, value in payload.items():
        merged[str(key)] = _safe_float(value, merged.get(str(key), 0.1))
    return (_normalize(merged), dict(row))


@dataclass(frozen=True)
class UpdateEvidence:
    reviews: int
    too_aggressive: int
    too_conservative: int
    bad: int
    good: int
    heavy_days: int
    sleep_related_bad: int
    hrv_related_bad: int
    nutrition_related_bad: int
    b2b_related_bad: int
    run_related_bad: int
    stress_related_bad: int
    hrv_related_conservative: int
    b2b_related_conservative: int
    run_related_conservative: int


@dataclass(frozen=True)
class SimulationEvidence:
    samples: int
    optimistic_count: int
    pessimistic_count: int
    high_error_count: int
    mean_readiness_bias: float
    mean_abs_readiness_error: float
    mean_abs_rmssd_error: float
    mean_abs_rhr_error: float
    mean_abs_fatigue_error: float
    driver_high_error_counts: dict[str, int]


def _collect_update_evidence(conn: sqlite3.Connection, *, day_iso: str, lookback_days: int = 45) -> UpdateEvidence:
    since = (date.fromisoformat(day_iso) - timedelta(days=max(14, int(lookback_days)))).isoformat()
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
        SELECT d.id, d.raw_json, lr.outcome_label, lr.outcome_score
        FROM core_decision_log d
        JOIN latest_reviews lr ON lr.decision_id = d.id
        WHERE d.day_iso >= ?
          AND d.source IN ('live_today', 'plateau_scan')
        ORDER BY d.day_iso DESC
        """,
        (since,),
    ).fetchall()

    reviews = 0
    too_aggressive = 0
    too_conservative = 0
    bad = 0
    good = 0
    heavy_days = 0
    sleep_related_bad = 0
    hrv_related_bad = 0
    nutrition_related_bad = 0
    b2b_related_bad = 0
    run_related_bad = 0
    stress_related_bad = 0
    hrv_related_conservative = 0
    b2b_related_conservative = 0
    run_related_conservative = 0

    for row in rows:
        raw_json = row["raw_json"]
        reason_codes = _extract_reason_codes(raw_json)
        mode = _extract_mode(raw_json)
        outcome = str(row["outcome_label"] or "inconclusive").strip().lower()
        reviews += 1

        if mode == "HEAVY":
            heavy_days += 1
        if outcome == "too_aggressive":
            too_aggressive += 1
        if outcome == "too_conservative":
            too_conservative += 1
        if outcome == "bad":
            bad += 1
        if outcome == "good":
            good += 1

        is_bad_like = outcome in {"bad", "too_aggressive"}
        is_conservative = outcome == "too_conservative"

        if is_bad_like and any(code in reason_codes for code in {"SLEEP_DEBT", "LATE_LOGGING"}):
            sleep_related_bad += 1
        if is_bad_like and any(code in reason_codes for code in {"HRV_LOW", "HRV_VERY_LOW", "RHR_HIGH"}):
            hrv_related_bad += 1
        if is_bad_like and any(code in reason_codes for code in {"NUTRITION_UNDERSUPPORTED", "RECOVERY_MISSING"}):
            nutrition_related_bad += 1
        if is_bad_like and any(code in reason_codes for code in {"HI_BACK_TO_BACK_GUARD"}):
            b2b_related_bad += 1
        if is_bad_like and any(code in reason_codes for code in {"CORE_TACTICAL_RUN", "CORE_PROTECT_RUN", "RUN_INTERFERENCE"}):
            run_related_bad += 1
        if is_bad_like and any(code in reason_codes for code in {"SCHOOL_STRESS", "CALENDAR_STRESS", "STRESS_LOAD"}):
            stress_related_bad += 1

        if is_conservative and any(code in reason_codes for code in {"HRV_LOW", "RHR_HIGH"}):
            hrv_related_conservative += 1
        if is_conservative and any(code in reason_codes for code in {"HI_BACK_TO_BACK_GUARD"}):
            b2b_related_conservative += 1
        if is_conservative and any(code in reason_codes for code in {"CORE_TACTICAL_RUN", "RUN_INTERFERENCE"}):
            run_related_conservative += 1

    return UpdateEvidence(
        reviews=reviews,
        too_aggressive=too_aggressive,
        too_conservative=too_conservative,
        bad=bad,
        good=good,
        heavy_days=heavy_days,
        sleep_related_bad=sleep_related_bad,
        hrv_related_bad=hrv_related_bad,
        nutrition_related_bad=nutrition_related_bad,
        b2b_related_bad=b2b_related_bad,
        run_related_bad=run_related_bad,
        stress_related_bad=stress_related_bad,
        hrv_related_conservative=hrv_related_conservative,
        b2b_related_conservative=b2b_related_conservative,
        run_related_conservative=run_related_conservative,
    )


def _driver_key_to_parameter(key_raw: Any) -> str | None:
    key = str(key_raw or "").strip().lower()
    if not key:
        return None
    if key in {"load_density", "back_to_back", "back_to_back_load"}:
        return "back_to_back"
    if key in {"recovery_state", "hrv_state", "hrv", "rhr"}:
        return "hrv"
    if key in {"sleep_state", "sleep"}:
        return "sleep"
    if key in {"calendar_stress", "stress", "school_stress"}:
        return "stress"
    if key in {"nutrition_state", "nutrition", "kcal"}:
        return "nutrition"
    if key in {"run_load", "run", "run_interference"}:
        return "run_interference"
    return None


def _collect_simulation_feedback_evidence(
    conn: sqlite3.Connection,
    *,
    day_iso: str,
    lookback_days: int = 60,
) -> SimulationEvidence:
    since = (date.fromisoformat(day_iso) - timedelta(days=max(14, int(lookback_days)))).isoformat()
    rows = conn.execute(
        """
        SELECT error_json, overall_error, quality_label, driver_impact_json
        FROM core_simulation_feedback
        WHERE actual_date BETWEEN ? AND ?
        ORDER BY actual_date DESC
        """,
        (since, day_iso),
    ).fetchall()

    if not rows:
        return SimulationEvidence(
            samples=0,
            optimistic_count=0,
            pessimistic_count=0,
            high_error_count=0,
            mean_readiness_bias=0.0,
            mean_abs_readiness_error=0.0,
            mean_abs_rmssd_error=0.0,
            mean_abs_rhr_error=0.0,
            mean_abs_fatigue_error=0.0,
            driver_high_error_counts={},
        )

    optimism = 0
    pessimism = 0
    high_error = 0
    readiness_bias_vals: list[float] = []
    abs_ready_vals: list[float] = []
    abs_rmssd_vals: list[float] = []
    abs_rhr_vals: list[float] = []
    abs_fatigue_vals: list[float] = []
    driver_counts: dict[str, int] = {}

    for row in rows:
        err = _parse_json(row["error_json"], {})
        overall = _safe_float(row["overall_error"], 0.0)
        quality = str(row["quality_label"] or "").strip().lower()
        signed_ready = _safe_float(err.get("readiness_delta_pct"), 0.0)
        abs_ready = _safe_float(err.get("readiness_abs_error_pct"), abs(signed_ready))
        abs_rmssd = _safe_float(err.get("rmssd_abs_error_ms"), abs(_safe_float(err.get("rmssd_delta_ms"), 0.0)))
        abs_rhr = _safe_float(err.get("rhr_abs_error_bpm"), abs(_safe_float(err.get("rhr_delta_bpm"), 0.0)))
        abs_fatigue = _safe_float(err.get("fatigue_abs_error_10"), abs(_safe_float(err.get("fatigue_delta_10"), 0.0)))

        readiness_bias_vals.append(signed_ready)
        abs_ready_vals.append(abs_ready)
        abs_rmssd_vals.append(abs_rmssd)
        abs_rhr_vals.append(abs_rhr)
        abs_fatigue_vals.append(abs_fatigue)

        if signed_ready >= 8.0:
            optimism += 1
        elif signed_ready <= -8.0:
            pessimism += 1

        is_high_error = overall >= 0.55 or quality in {"weak", "bad"}
        if is_high_error:
            high_error += 1
            drivers = _parse_json(row["driver_impact_json"], {})
            if isinstance(drivers, dict):
                for key in drivers.keys():
                    pname = _driver_key_to_parameter(key)
                    if not pname:
                        continue
                    driver_counts[pname] = _safe_int(driver_counts.get(pname), 0) + 1

    samples = len(rows)
    return SimulationEvidence(
        samples=samples,
        optimistic_count=optimism,
        pessimistic_count=pessimism,
        high_error_count=high_error,
        mean_readiness_bias=round(sum(readiness_bias_vals) / max(1, samples), 3),
        mean_abs_readiness_error=round(sum(abs_ready_vals) / max(1, samples), 3),
        mean_abs_rmssd_error=round(sum(abs_rmssd_vals) / max(1, samples), 3),
        mean_abs_rhr_error=round(sum(abs_rhr_vals) / max(1, samples), 3),
        mean_abs_fatigue_error=round(sum(abs_fatigue_vals) / max(1, samples), 3),
        driver_high_error_counts=driver_counts,
    )


def _suggest_parameter_deltas(e: UpdateEvidence, sim: SimulationEvidence) -> list[tuple[str, float, str, float]]:
    updates: list[tuple[str, float, str, float]] = []

    if e.reviews >= MIN_REVIEWS_FOR_UPDATE:
        if e.sleep_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.01 + (e.sleep_related_bad * 0.004))
            updates.append(("sleep", step, "Schlechter Schlaf korrelierte wiederholt mit negativen Outcomes.", 0.72))

        if e.hrv_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.008 + (e.hrv_related_bad * 0.003))
            updates.append(("hrv", step, "HRV/RHR-Warnungen waren bei negativen Outcomes häufig relevant.", 0.68))

        if e.hrv_related_conservative >= 2:
            step = -min(MAX_DELTA_PER_UPDATE, 0.008 + (e.hrv_related_conservative * 0.003))
            updates.append(("hrv", step, "HRV wurde zuletzt tendenziell überbewertet (zu konservative Entscheidungen).", 0.63))

        if e.nutrition_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.01 + (e.nutrition_related_bad * 0.004))
            updates.append(("nutrition", step, "Ernährungsdefizit drückte Performance/Recovery stärker als erwartet.", 0.74))

        if e.b2b_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.008 + (e.b2b_related_bad * 0.003))
            updates.append(("back_to_back", step, "Back-to-back Last erzeugte wiederholt negative Folgen.", 0.66))

        if e.b2b_related_conservative >= 2:
            step = -min(MAX_DELTA_PER_UPDATE, 0.007 + (e.b2b_related_conservative * 0.003))
            updates.append(("back_to_back", step, "Back-to-back Bremse war öfter zu streng.", 0.62))

        if e.run_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.008 + (e.run_related_bad * 0.003))
            updates.append(("run_interference", step, "Run/Gym-Interferenz wurde in negativen Outcomes bestätigt.", 0.65))

        if e.run_related_conservative >= 2:
            step = -min(MAX_DELTA_PER_UPDATE, 0.007 + (e.run_related_conservative * 0.003))
            updates.append(("run_interference", step, "Run-Interferenzbremse war zuletzt teils zu vorsichtig.", 0.61))

        if e.stress_related_bad >= 2:
            step = min(MAX_DELTA_PER_UPDATE, 0.007 + (e.stress_related_bad * 0.003))
            updates.append(("stress", step, "Alltagsstress war bei schlechter Session-Qualität häufiger sichtbar.", 0.6))

        if e.good >= max(3, e.too_aggressive + e.bad + 2):
            updates.append(("simulation", -0.006, "Simulation war zuletzt eher zu streng; kleine Entschärfung.", 0.58))
        elif (e.too_aggressive + e.bad) >= max(3, e.good):
            updates.append(("simulation", 0.008, "Simulation unterschätzte zuletzt Folgekosten; kleine Verschärfung.", 0.62))

        if e.heavy_days >= 6 and e.too_aggressive == 0 and e.good >= 3:
            updates.append(("back_to_back", -0.005, "Back-to-back Heavy lief stabil; Bremse leicht reduziert.", 0.57))

    if sim.samples >= MIN_SIM_FEEDBACK_FOR_UPDATE:
        optimistic_ratio = sim.optimistic_count / max(1, sim.samples)
        pessimistic_ratio = sim.pessimistic_count / max(1, sim.samples)
        if optimistic_ratio >= 0.45 or sim.mean_readiness_bias >= 6.0 or sim.mean_abs_readiness_error >= 10.0:
            step = min(MAX_DELTA_PER_UPDATE, 0.006 + (sim.mean_abs_readiness_error * 0.0008))
            updates.append(("simulation", step, "Prognose war zuletzt öfter zu optimistisch; Simulation wird vorsichtiger kalibriert.", 0.69))
        elif pessimistic_ratio >= 0.45 or sim.mean_readiness_bias <= -6.0:
            step = -min(MAX_DELTA_PER_UPDATE, 0.005 + (abs(sim.mean_readiness_bias) * 0.0007))
            updates.append(("simulation", step, "Prognose war zuletzt eher zu streng; Simulation wird leicht entschaerft.", 0.64))

        if sim.mean_abs_rmssd_error >= 6.0:
            step = min(MAX_DELTA_PER_UPDATE, 0.004 + (sim.mean_abs_rmssd_error * 0.00045))
            updates.append(("hrv", step, "RMSSD-Prognose lag öfter daneben; HRV-Gewichtung wird leicht erhöht.", 0.63))

        if sim.mean_abs_fatigue_error >= 1.3:
            step = min(MAX_DELTA_PER_UPDATE, 0.004 + (sim.mean_abs_fatigue_error * 0.0028))
            updates.append(("fatigue", step, "Müdigkeit morgen wurde unterschätzt; Fatigue-Gewichtung wird erhöht.", 0.65))

        for pname, count in (sim.driver_high_error_counts or {}).items():
            if count < 2:
                continue
            step = min(MAX_DELTA_PER_UPDATE, 0.003 + (count * 0.0015))
            updates.append((pname, step, f"Hohe Prognosefehler häuften sich bei Treiber '{pname}'.", 0.6))

    # Aggregate by parameter and cap in one pass.
    by_param: dict[str, tuple[float, list[str], list[float]]] = {}
    for name, delta, reason, conf in updates:
        prev = by_param.get(name)
        if prev is None:
            by_param[name] = (delta, [reason], [conf])
        else:
            by_param[name] = (prev[0] + delta, prev[1] + [reason], prev[2] + [conf])

    compact: list[tuple[str, float, str, float]] = []
    for name, (delta_sum, reasons, confs) in by_param.items():
        delta_cap = _clamp(delta_sum, -MAX_DELTA_PER_UPDATE, MAX_DELTA_PER_UPDATE)
        reason = reasons[0]
        confidence = round(_safe_float(sum(confs) / max(1, len(confs))), 3)
        compact.append((name, delta_cap, reason, confidence))
    return compact


def tune_parameters(
    conn: sqlite3.Connection,
    *,
    day_iso: str,
    apply_updates: bool = True,
) -> dict[str, Any]:
    params, active_row = get_active_parameters(conn)
    evidence = _collect_update_evidence(conn, day_iso=day_iso, lookback_days=45)
    sim_evidence = _collect_simulation_feedback_evidence(conn, day_iso=day_iso, lookback_days=60)
    suggestions = _suggest_parameter_deltas(evidence, sim_evidence)

    if not suggestions:
        return {
            "changed_parameters": [],
            "delta": {},
            "reason": "Mindestdatenmenge oder Signalstaerke fuer Updates noch nicht erreicht.",
            "confidence_of_update": 0.0,
            "active_parameters": params,
            "trend_summary": "Keine neue Kalibrierung.",
            "evidence": {
                "reviews": evidence.reviews,
                "good": evidence.good,
                "bad": evidence.bad,
                "too_aggressive": evidence.too_aggressive,
                "too_conservative": evidence.too_conservative,
                "simulation_feedback_samples": sim_evidence.samples,
                "simulation_abs_readiness_error": sim_evidence.mean_abs_readiness_error,
                "simulation_abs_rmssd_error": sim_evidence.mean_abs_rmssd_error,
            },
        }

    new_params = dict(params)
    changed_rows: list[dict[str, Any]] = []
    for name, delta, reason, confidence in suggestions:
        old_value = _safe_float(new_params.get(name), DEFAULT_PARAMETERS.get(name, 0.1))
        updated = _clamp(old_value + delta, PARAMETER_MIN, PARAMETER_MAX)
        if abs(updated - old_value) < 0.0009:
            continue
        new_params[name] = updated
        changed_rows.append(
            {
                "parameter_name": name,
                "old_value": round(old_value, 4),
                "new_value": round(updated, 4),
                "delta": round(updated - old_value, 4),
                "trigger_reason": reason,
                "confidence": round(confidence, 3),
            }
        )

    if not changed_rows:
        return {
            "changed_parameters": [],
            "delta": {},
            "reason": "Keine wirksame Delta-Aenderung nach Caps/Clamps.",
            "confidence_of_update": 0.0,
            "active_parameters": params,
            "trend_summary": "Keine neue Kalibrierung.",
            "evidence": {
                "reviews": evidence.reviews,
                "simulation_feedback_samples": sim_evidence.samples,
            },
        }

    normalized = _normalize(new_params)
    avg_conf = round(sum(_safe_float(row.get("confidence"), 0.0) for row in changed_rows) / len(changed_rows), 3)
    delta_map = {row["parameter_name"]: row["delta"] for row in changed_rows}
    signed = sum(delta_map.values())
    trend_summary = "CORE wird leicht vorsichtiger." if signed > 0 else "CORE wird leicht aggressiver."
    if abs(signed) < 0.002:
        trend_summary = "CORE kalibriert seitwaerts."

    if apply_updates:
        if active_row:
            conn.execute("UPDATE core_parameter_versions SET active=0 WHERE id=?", (active_row["id"],))
        version_tag = f"pv-{day_iso}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        conn.execute(
            """
            INSERT INTO core_parameter_versions (
                created_at, version_tag, parameter_json,
                derived_from_range_start, derived_from_range_end,
                notes, active
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                _utc_now(),
                version_tag,
                json.dumps(normalized, ensure_ascii=False),
                (date.fromisoformat(day_iso) - timedelta(days=45)).isoformat(),
                day_iso,
                "Auto calibration from review + simulation feedback evidence.",
            ),
        )
        for row in changed_rows:
            conn.execute(
                """
                INSERT INTO core_parameter_updates (
                    created_at, parameter_name, old_value, new_value, delta,
                    trigger_reason, evidence_json, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    row["parameter_name"],
                    row["old_value"],
                    row["new_value"],
                    row["delta"],
                    row["trigger_reason"],
                    json.dumps(
                        {
                            "reviews": evidence.reviews,
                            "good": evidence.good,
                            "bad": evidence.bad,
                            "too_aggressive": evidence.too_aggressive,
                            "too_conservative": evidence.too_conservative,
                            "simulation_feedback_samples": sim_evidence.samples,
                            "simulation_mean_readiness_bias": sim_evidence.mean_readiness_bias,
                            "simulation_abs_readiness_error": sim_evidence.mean_abs_readiness_error,
                            "simulation_abs_rmssd_error_ms": sim_evidence.mean_abs_rmssd_error,
                            "simulation_abs_rhr_error_bpm": sim_evidence.mean_abs_rhr_error,
                            "simulation_abs_fatigue_error_10": sim_evidence.mean_abs_fatigue_error,
                        },
                        ensure_ascii=False,
                    ),
                    row["confidence"],
                ),
            )

    return {
        "changed_parameters": changed_rows,
        "delta": delta_map,
        "reason": "Parameter wurden langsam und begrenzt anhand Outcome-Reviews kalibriert.",
        "confidence_of_update": avg_conf,
        "active_parameters": normalized,
        "trend_summary": trend_summary,
        "evidence": {
            "reviews": evidence.reviews,
            "good": evidence.good,
            "bad": evidence.bad,
            "too_aggressive": evidence.too_aggressive,
            "too_conservative": evidence.too_conservative,
            "simulation_feedback_samples": sim_evidence.samples,
            "simulation_optimistic_count": sim_evidence.optimistic_count,
            "simulation_pessimistic_count": sim_evidence.pessimistic_count,
            "simulation_high_error_count": sim_evidence.high_error_count,
            "simulation_mean_readiness_bias": sim_evidence.mean_readiness_bias,
            "simulation_abs_readiness_error": sim_evidence.mean_abs_readiness_error,
            "simulation_abs_rmssd_error_ms": sim_evidence.mean_abs_rmssd_error,
            "simulation_abs_rhr_error_bpm": sim_evidence.mean_abs_rhr_error,
            "simulation_abs_fatigue_error_10": sim_evidence.mean_abs_fatigue_error,
        },
    }


def latest_parameter_updates(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM core_parameter_updates
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": _safe_int(row["id"]),
                "created_at": str(row["created_at"] or ""),
                "parameter_name": str(row["parameter_name"] or ""),
                "old_value": _safe_float(row["old_value"], 0.0),
                "new_value": _safe_float(row["new_value"], 0.0),
                "delta": _safe_float(row["delta"], 0.0),
                "trigger_reason": str(row["trigger_reason"] or ""),
                "evidence": _parse_json(row["evidence_json"], {}),
                "confidence": _safe_float(row["confidence"], 0.0),
            }
        )
    return out
