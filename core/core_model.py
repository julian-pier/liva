from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.core_explain import explain_hypothesis_v2, summarize_model_change_v2
from core.core_training_deck import get_core_principle_scores
from database.connections import get_core_db, get_hrv_db


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


def _median(values: list[float]) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def _extract_feature(decision_raw_json: str | dict[str, Any], key: str) -> str:
    raw = _parse_json(decision_raw_json, {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    decision_meta = meta.get("decision_meta") if isinstance(meta.get("decision_meta"), dict) else {}
    core_overlay = decision_meta.get("core_overlay") if isinstance(decision_meta.get("core_overlay"), dict) else {}
    features = core_overlay.get("features") if isinstance(core_overlay.get("features"), dict) else {}
    return str(features.get(key) or "").strip().lower()


def _extract_reason_codes(decision_raw_json: str | dict[str, Any]) -> list[str]:
    raw = _parse_json(decision_raw_json, {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    return [str(x or "").strip().upper() for x in (meta.get("reason_codes") or []) if str(x or "").strip()]


def _status_from_stats(total: int, confidence: float, contradiction: int) -> str:
    if total < 3:
        return "testing"
    if confidence >= 0.72 and contradiction <= max(1, total // 3):
        return "active"
    if confidence <= 0.3 and total >= 5:
        return "rejected"
    if confidence < 0.5 and total >= 4:
        return "weakened"
    return "testing"


def _build_hypothesis_candidates(
    conn: sqlite3.Connection,
    *,
    as_of: date | None = None,
    lookback_days: int = 60,
) -> list[dict[str, Any]]:
    as_of = as_of or date.today()
    lookback_days = max(21, min(730, int(lookback_days or 60)))
    since = (as_of - timedelta(days=lookback_days)).isoformat()
    as_of_iso = as_of.isoformat()
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
        SELECT d.id, d.day_iso, d.type, d.why_json, d.raw_json,
               lr.outcome_label, lr.outcome_score
        FROM core_decision_log d
        LEFT JOIN latest_reviews lr ON lr.decision_id = d.id
        WHERE d.day_iso >= ?
          AND d.day_iso <= ?
          AND d.source IN ('live_today', 'plateau_scan')
        ORDER BY d.day_iso DESC
        """,
        (since, as_of_iso),
    ).fetchall()

    reviewed = [dict(r) for r in rows if str(r["outcome_label"] or "") not in {"", "inconclusive"}]

    low_recovery = [r for r in reviewed if _extract_feature(r.get("raw_json"), "recovery_state") == "low"]
    low_recovery_support = sum(1 for r in low_recovery if str(r.get("outcome_label") or "") not in {"bad", "too_aggressive"})
    low_recovery_contra = max(0, len(low_recovery) - low_recovery_support)
    low_recovery_conf = (low_recovery_support / len(low_recovery)) if low_recovery else 0.0

    poor_nutri = [r for r in reviewed if _extract_feature(r.get("raw_json"), "nutrition_adherence") in {"low", "missing"}]
    poor_nutri_bad = sum(1 for r in poor_nutri if str(r.get("outcome_label") or "") in {"bad", "too_aggressive", "too_conservative"})
    poor_nutri_rate = (poor_nutri_bad / len(poor_nutri)) if poor_nutri else 0.0

    recovery_only = [
        r
        for r in reviewed
        if _extract_feature(r.get("raw_json"), "recovery_state") == "low"
        and _extract_feature(r.get("raw_json"), "nutrition_adherence") not in {"low", "missing"}
    ]
    recovery_only_bad = sum(1 for r in recovery_only if str(r.get("outcome_label") or "") in {"bad", "too_aggressive", "too_conservative"})
    recovery_only_rate = (recovery_only_bad / len(recovery_only)) if recovery_only else 0.0
    nutrition_diff = poor_nutri_rate - recovery_only_rate
    nutrition_support = max(0, _safe_int(round(len(poor_nutri) * max(0.0, nutrition_diff))))
    nutrition_contra = max(0, _safe_int(round(len(poor_nutri) * max(0.0, -nutrition_diff))))

    pull_rows = [r for r in reviewed if "pull" in str(r.get("why_json") or "").lower() and "m\u00fcd" in str(r.get("why_json") or "").lower()]
    pull_support = sum(1 for r in pull_rows if str(r.get("outcome_label") or "") in {"good", "mixed"})
    pull_contra = max(0, len(pull_rows) - pull_support)
    pull_conf = (pull_support / len(pull_rows)) if pull_rows else 0.0

    plateau_rows = [r for r in reviewed if str(r.get("type") or "").lower() == "training" and "plateau" in str(r.get("raw_json") or "").lower()]
    plateau_support = sum(1 for r in plateau_rows if str(r.get("outcome_label") or "") in {"good", "mixed"})
    plateau_contra = max(0, len(plateau_rows) - plateau_support)
    plateau_conf = (plateau_support / len(plateau_rows)) if plateau_rows else 0.0

    late_rows = conn.execute(
        """
        SELECT o1.observed_at AS day_a, o1.signal_value_json AS late_json,
               o2.signal_value_json AS rec_json
        FROM core_observations o1
        LEFT JOIN core_observations o2
          ON substr(o2.observed_at,1,10) = date(substr(o1.observed_at,1,10), '+1 day')
         AND o2.signal_key='recovery.hrv_today'
        WHERE o1.signal_key='behavior.late_signals'
          AND substr(o1.observed_at,1,10) <= ?
        ORDER BY o1.observed_at DESC
        LIMIT 45
        """
        ,
        (as_of_iso,),
    ).fetchall()

    late_support = 0
    late_contra = 0
    for row in late_rows:
        late = _parse_json(row["late_json"], {})
        rec = _parse_json(row["rec_json"], {})
        late_rate = _safe_float(late.get("late_workout_rate"), 0.0)
        late_hour = _safe_float(late.get("avg_hrv_measurement_hour"), 0.0)
        rmssd_ratio = _safe_float(rec.get("rmssd_ratio_14"), 1.0)
        is_late = late_rate >= 0.25 or late_hour >= 23.0
        if not is_late:
            continue
        if rmssd_ratio > 0 and rmssd_ratio < 0.98:
            late_support += 1
        else:
            late_contra += 1

    late_total = late_support + late_contra
    late_conf = (late_support / late_total) if late_total else 0.0

    candidates = [
        {
            "hypothesis_key": "hrv_low_alone_weak_signal",
            "title": "Niedrige HRV allein ist nicht immer ein hartes Bremssignal",
            "statement": "Wenn nur HRV niedrig ist, ohne weitere rote Marker, trägt bei dir oft eine moderate statt harte Bremse.",
            "domain": "recovery",
            "type": "recovery",
            "support_count": low_recovery_support,
            "contradiction_count": low_recovery_contra,
            "confidence": round(low_recovery_conf, 3),
            "impact_score": round(_clamp(0.35 + 0.45 * low_recovery_conf), 3),
            "evidence": [
                {"label": "Reviews mit Recovery=low", "value": str(len(low_recovery))},
                {"label": "Nicht-negativer Verlauf", "value": str(low_recovery_support)},
                {"label": "Widersprüche", "value": str(low_recovery_contra)},
            ],
            "relevance": "Beeinflusst, ob CORE bei Recovery-Signalen sofort auf LIGHT geht oder erst Kontext prüft.",
            "reason": "Aus Reviews mit Recovery=low zeigt sich, wie oft harte Bremse wirklich nötig war.",
        },
        {
            "hypothesis_key": "nutrition_often_stronger_than_isolated_recovery",
            "title": "Ernährung limitiert dich oft stärker als isolierte Recovery-Dips",
            "statement": "Schwache Ernährungssignale gehen bei dir häufiger mit schlechten Outcomes einher als isolierte Recovery-Schwankungen.",
            "domain": "nutrition",
            "type": "nutrition",
            "support_count": nutrition_support,
            "contradiction_count": nutrition_contra,
            "confidence": round(_clamp(0.5 + nutrition_diff), 3),
            "impact_score": round(_clamp(0.42 + abs(nutrition_diff) * 0.55), 3),
            "evidence": [
                {"label": "Bad-Rate bei Nutrition low/missing", "value": f"{round(poor_nutri_rate * 100)}%"},
                {"label": "Bad-Rate bei Recovery low (ohne Nutrition-low)", "value": f"{round(recovery_only_rate * 100)}%"},
                {"label": "Differenz", "value": f"{round(nutrition_diff * 100)}pp"},
            ],
            "relevance": "Steuert, ob CORE Ernährung zuerst stabilisiert bevor ein Push freigegeben wird.",
            "reason": "Vergleich der Outcome-Raten zwischen Nutrition-Low und isoliertem Recovery-Low.",
        },
        {
            "hypothesis_key": "late_shutdown_hurts_recovery_next_day",
            "title": "Spätes Runterfahren verschlechtert wahrscheinlich deine Recovery",
            "statement": "Späte Aktivität am Abend korreliert bei dir oft mit schwächerer Recovery am Folgetag.",
            "domain": "behavior",
            "type": "behavior",
            "support_count": late_support,
            "contradiction_count": late_contra,
            "confidence": round(late_conf, 3),
            "impact_score": round(_clamp(0.3 + 0.5 * late_conf), 3),
            "evidence": [
                {"label": "Late->Recovery Fälle", "value": str(late_total)},
                {"label": "Bestätigt", "value": str(late_support)},
                {"label": "Widersprochen", "value": str(late_contra)},
            ],
            "relevance": "Wirkt direkt auf Timing-Empfehlungen und Abendroutine-Hinweise in CORE.",
            "reason": "Verknüpfung von Late-Signal-Observation und Recovery-Ratio am Folgetag.",
        },
        {
            "hypothesis_key": "local_pull_fatigue_more_reliable_than_global_fatigue",
            "title": "Lokale Pull-Fatigue ist bei dir oft der verlässlichere Grenzmarker",
            "statement": "Lokale Pull-Fatigue-Signale liefern bei dir häufiger brauchbare Bremsindikationen als diffuse Gesamtmüdigkeit.",
            "domain": "training",
            "type": "interference",
            "support_count": pull_support,
            "contradiction_count": pull_contra,
            "confidence": round(pull_conf, 3),
            "impact_score": round(_clamp(0.3 + 0.45 * pull_conf), 3),
            "evidence": [
                {"label": "Pull-Fatigue Reviews", "value": str(len(pull_rows))},
                {"label": "Nicht-negativ", "value": str(pull_support)},
                {"label": "Negativ", "value": str(pull_contra)},
            ],
            "relevance": "Steuert lokale Übungsauswahl und Intensitätsbegrenzung bei Pull-lastigen Tagen.",
            "reason": "Review-Ergebnis für Entscheidungen mit Pull-Fatigue-Hinweisen.",
        },
        {
            "hypothesis_key": "plateau_trigger_should_switch_structure_early",
            "title": "Bei Plateau lohnt früher Strukturwechsel statt mehr vom Gleichen",
            "statement": "Wenn Plateau-Signaturen mehrfach auftreten, ist ein früher Strukturwechsel oft tragfähiger als Wiederholung.",
            "domain": "training",
            "type": "planning",
            "support_count": plateau_support,
            "contradiction_count": plateau_contra,
            "confidence": round(plateau_conf, 3),
            "impact_score": round(_clamp(0.35 + 0.5 * plateau_conf), 3),
            "evidence": [
                {"label": "Plateau Reviews", "value": str(len(plateau_rows))},
                {"label": "Bestätigt", "value": str(plateau_support)},
                {"label": "Widersprochen", "value": str(plateau_contra)},
            ],
            "relevance": "Beeinflusst CORE-Wechselentscheidungen bei stagnierenden Übungen.",
            "reason": "Outcome-Verlauf nach Plateau-Entscheidungen.",
        },
    ]
    candidates.extend(_build_hrv_hypothesis_candidates(as_of=as_of, lookback_days=lookback_days))
    return candidates


def _build_hrv_hypothesis_candidates(*, as_of: date, lookback_days: int) -> list[dict[str, Any]]:
    since = (as_of - timedelta(days=lookback_days)).isoformat()
    as_of_iso = as_of.isoformat()
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                substr(COALESCE(date_utc, ts_measurement), 1, 10) AS day_iso,
                AVG(rmssd) AS rmssd,
                AVG(hr) AS hr,
                AVG(sleep_quality) AS sleep_quality
            FROM hrv_measurements
            WHERE substr(COALESCE(date_utc, ts_measurement), 1, 10) >= ?
              AND substr(COALESCE(date_utc, ts_measurement), 1, 10) <= ?
            GROUP BY day_iso
            ORDER BY day_iso
            """,
            (since, as_of_iso),
        ).fetchall()
    finally:
        conn.close()

    days: list[dict[str, Any]] = []
    for row in rows:
        day_iso = str(row["day_iso"] or "").strip()
        if not day_iso:
            continue
        rmssd = _safe_float(row["rmssd"], 0.0)
        hr = _safe_float(row["hr"], 0.0)
        sq = _safe_float(row["sleep_quality"], 0.0)
        days.append(
            {
                "day": day_iso,
                "rmssd": rmssd if rmssd > 0 else None,
                "hr": hr if hr > 0 else None,
                "sleep_quality": sq if sq > 0 else None,
            }
        )

    rmssd_vals = [float(d["rmssd"]) for d in days if d.get("rmssd") is not None]
    hr_vals = [float(d["hr"]) for d in days if d.get("hr") is not None]
    rmssd_base = _median(rmssd_vals)
    hr_base = _median(hr_vals)
    if rmssd_base is None or rmssd_base <= 0:
        return []

    # Candidate A: single-day low RMSSD often rebounds quickly -> avoid overreacting to one noisy dip.
    low_idxs = [i for i, d in enumerate(days) if d.get("rmssd") is not None and float(d["rmssd"]) <= float(rmssd_base) * 0.90]
    rebound_support = 0
    rebound_contra = 0
    for idx in low_idxs:
        next1 = days[idx + 1] if idx + 1 < len(days) else None
        if not next1 or next1.get("rmssd") is None:
            continue
        low_rmssd = float(days[idx]["rmssd"] or 0.0)
        nxt_rmssd = float(next1["rmssd"] or 0.0)
        if nxt_rmssd >= low_rmssd * 1.06:
            rebound_support += 1
        else:
            rebound_contra += 1
    rebound_total = rebound_support + rebound_contra
    rebound_conf = (rebound_support / rebound_total) if rebound_total else 0.0

    # Candidate B: multi-day RMSSD slump + elevated HR indicates systemic fatigue signal.
    slump_support = 0
    slump_contra = 0
    for i in range(2, len(days)):
        d0, d1, d2 = days[i - 2], days[i - 1], days[i]
        if any(d.get("rmssd") is None for d in (d0, d1, d2)):
            continue
        thr = float(rmssd_base) * 0.93
        if float(d0["rmssd"]) > thr or float(d1["rmssd"]) > thr or float(d2["rmssd"]) > thr:
            continue
        hr_slice = [d.get("hr") for d in (d0, d1, d2) if d.get("hr") is not None]
        sq_slice = [d.get("sleep_quality") for d in (d0, d1, d2) if d.get("sleep_quality") is not None]
        hr_high = bool(hr_slice and hr_base and _median([float(v) for v in hr_slice]) is not None and float(_median([float(v) for v in hr_slice]) or 0.0) >= float(hr_base) * 1.03)
        sq_low = bool(sq_slice and _median([float(v) for v in sq_slice]) is not None and float(_median([float(v) for v in sq_slice]) or 0.0) <= 3.0)
        if hr_high or sq_low:
            slump_support += 1
        else:
            slump_contra += 1
    slump_total = slump_support + slump_contra
    slump_conf = (slump_support / slump_total) if slump_total else 0.0

    out: list[dict[str, Any]] = []
    if rebound_total >= 4:
        out.append(
            {
                "hypothesis_key": "hrv_single_day_dip_often_noise",
                "title": "Einzelner HRV-Dip ist bei dir oft nur Tagesrauschen",
                "statement": "Ein einzelner niedriger RMSSD-Tag stabilisiert sich bei dir häufig direkt wieder am Folgetag.",
                "domain": "recovery",
                "type": "recovery",
                "support_count": rebound_support,
                "contradiction_count": rebound_contra,
                "confidence": round(_clamp(rebound_conf), 3),
                "impact_score": round(_clamp(0.28 + 0.42 * rebound_conf), 3),
                "evidence": [
                    {"label": "Single-dip Fälle", "value": str(rebound_total)},
                    {"label": "Schneller Rebound", "value": str(rebound_support)},
                    {"label": "Kein Rebound", "value": str(rebound_contra)},
                    {"label": "RMSSD-Baseline", "value": f"{round(float(rmssd_base), 1)}"},
                ],
                "relevance": "Verhindert Überbremsung bei isolierten Tagesdips ohne Folgesignal.",
                "reason": "Direkte HRV-Zeitreihe: Anzahl schneller Rebounds nach Low-RMSSD-Tag.",
            }
        )
    if slump_total >= 3:
        out.append(
            {
                "hypothesis_key": "hrv_multi_day_slump_is_actionable",
                "title": "Mehrtägiger HRV-Slump ist ein belastbares Warnsignal",
                "statement": "Wenn RMSSD mehrere Tage hintereinander gedrückt bleibt, ist das bei dir oft mit systemischem Stress gekoppelt.",
                "domain": "recovery",
                "type": "recovery",
                "support_count": slump_support,
                "contradiction_count": slump_contra,
                "confidence": round(_clamp(slump_conf), 3),
                "impact_score": round(_clamp(0.32 + 0.5 * slump_conf), 3),
                "evidence": [
                    {"label": "3-Tage-Slump Fälle", "value": str(slump_total)},
                    {"label": "Mit Stress-Marker (HR/Sleep)", "value": str(slump_support)},
                    {"label": "Ohne Stress-Marker", "value": str(slump_contra)},
                    {"label": "HR-Baseline", "value": f"{round(float(hr_base), 1) if hr_base else '-'}"},
                ],
                "relevance": "Erhöht die Sicherheit, wenn CORE Schutzentscheidungen bei anhaltendem Recovery-Druck trifft.",
                "reason": "RMSSD-Cluster + begleitende HR/Sleep-Indikatoren über den historischen Verlauf.",
            }
        )
    return out


def _record_model_update(
    conn: sqlite3.Connection,
    *,
    update_type: str,
    object_type: str,
    object_key: str,
    old_state: dict[str, Any],
    new_state: dict[str, Any],
    reason_summary: str,
    trigger_decision_id: int | None = None,
) -> None:
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO core_model_updates (
            update_date, update_type, object_type, object_key,
            old_state_json, new_state_json, reason_summary, trigger_decision_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _today_iso(),
            update_type,
            object_type,
            object_key,
            json.dumps(old_state or {}, ensure_ascii=False),
            json.dumps(new_state or {}, ensure_ascii=False),
            reason_summary,
            trigger_decision_id,
            now,
        ),
    )


def _upsert_hypothesis(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    support = _safe_int(item.get("support_count"))
    contradiction = _safe_int(item.get("contradiction_count"))
    total = support + contradiction
    confidence = _clamp(_safe_float(item.get("confidence")))
    status = _status_from_stats(total, confidence, contradiction)

    row = conn.execute(
        "SELECT * FROM core_hypotheses WHERE hypothesis_key=?",
        (item["hypothesis_key"],),
    ).fetchone()

    new_state = {
        "status": status,
        "confidence": round(confidence, 3),
        "impact_score": round(_clamp(_safe_float(item.get("impact_score"))), 3),
        "support_count": support,
        "contradiction_count": contradiction,
    }

    if row is None:
        conn.execute(
            """
            INSERT INTO core_hypotheses (
                hypothesis_key, title, statement, domain, type, status,
                confidence, impact_score, support_count, contradiction_count,
                evidence_json, relevance_text, last_reviewed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["hypothesis_key"],
                item["title"],
                item["statement"],
                item["domain"],
                item["type"],
                status,
                new_state["confidence"],
                new_state["impact_score"],
                support,
                contradiction,
                json.dumps(item.get("evidence") or [], ensure_ascii=False),
                item.get("relevance") or "",
                _today_iso(),
                now,
                now,
            ),
        )
        _record_model_update(
            conn,
            update_type="new_hypothesis",
            object_type="hypothesis",
            object_key=item["hypothesis_key"],
            old_state={},
            new_state=new_state,
            reason_summary=str(item.get("reason") or "Neue Hypothese aus Reviews/Patterns erzeugt."),
        )
        return {"changed": True, "state": new_state, "status": status}

    old_state = {
        "status": str(row["status"] or "testing"),
        "confidence": _safe_float(row["confidence"]),
        "impact_score": _safe_float(row["impact_score"]),
        "support_count": _safe_int(row["support_count"]),
        "contradiction_count": _safe_int(row["contradiction_count"]),
    }

    conn.execute(
        """
        UPDATE core_hypotheses
        SET title=?, statement=?, domain=?, type=?, status=?, confidence=?, impact_score=?,
            support_count=?, contradiction_count=?, evidence_json=?, relevance_text=?,
            last_reviewed_at=?, updated_at=?
        WHERE hypothesis_key=?
        """,
        (
            item["title"],
            item["statement"],
            item["domain"],
            item["type"],
            status,
            new_state["confidence"],
            new_state["impact_score"],
            support,
            contradiction,
            json.dumps(item.get("evidence") or [], ensure_ascii=False),
            item.get("relevance") or "",
            _today_iso(),
            now,
            item["hypothesis_key"],
        ),
    )

    changed = old_state["status"] != new_state["status"] or abs(old_state["confidence"] - new_state["confidence"]) >= 0.08
    if changed:
        if new_state["status"] == "rejected" and old_state["status"] != "rejected":
            update_type = "rejected"
        elif new_state["confidence"] >= old_state["confidence"] + 0.08:
            update_type = "strengthened"
        elif new_state["confidence"] <= old_state["confidence"] - 0.08:
            update_type = "weakened"
        else:
            update_type = "recalibrated"

        _record_model_update(
            conn,
            update_type=update_type,
            object_type="hypothesis",
            object_key=item["hypothesis_key"],
            old_state=old_state,
            new_state=new_state,
            reason_summary=str(item.get("reason") or "Hypothese wurde neu bewertet."),
        )

    return {"changed": changed, "state": new_state, "status": status}


def _upsert_blind_spot(
    conn: sqlite3.Connection,
    *,
    key: str,
    title: str,
    description: str,
    severity: str,
    domain: str,
    status: str,
    recommendation: str,
) -> None:
    now = _utc_now()
    row = conn.execute("SELECT * FROM core_blind_spots WHERE blind_spot_key=?", (key,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO core_blind_spots (
                blind_spot_key, title, description, severity, domain, status,
                recommendation, updated_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, title, description, severity, domain, status, recommendation, now, now),
        )
        return

    old_status = str(row["status"] or "open")
    conn.execute(
        """
        UPDATE core_blind_spots
        SET title=?, description=?, severity=?, domain=?, status=?, recommendation=?, updated_at=?
        WHERE blind_spot_key=?
        """,
        (title, description, severity, domain, status, recommendation, now, key),
    )
    if old_status != status:
        _record_model_update(
            conn,
            update_type="recalibrated",
            object_type="blind_spot",
            object_key=key,
            old_state={"status": old_status},
            new_state={"status": status},
            reason_summary=f"Blind Spot '{title}' wurde auf Status '{status}' gesetzt.",
        )


def _refresh_blind_spots(conn: sqlite3.Connection, coverage: dict[str, Any], hypothesis_stats: dict[str, int]) -> None:
    for domain in ("training", "run", "recovery", "nutrition"):
        cov = _safe_float(coverage.get(domain), 0.0)
        if cov < 0.35:
            status = "open"
            severity = "high"
        elif cov < 0.55:
            status = "monitoring"
            severity = "medium"
        else:
            status = "reduced"
            severity = "low"

        _upsert_blind_spot(
            conn,
            key=f"coverage_{domain}",
            title=f"Datenlücke: {domain}",
            description=f"Die Datenabdeckung in {domain} liegt bei {round(cov * 100)}%.",
            severity=severity,
            domain=domain,
            status=status,
            recommendation=f"Für {domain} möglichst an 5+ von 7 Tagen sauber loggen, damit CORE sicherer lernt.",
        )

    testing_count = _safe_int(hypothesis_stats.get("testing"))
    if testing_count >= 4:
        _upsert_blind_spot(
            conn,
            key="model_many_testing_hypotheses",
            title="Viele offene Hypothesen",
            description="Mehrere Kernannahmen stehen noch im Testing, Modellstabilität ist begrenzt.",
            severity="medium",
            domain="system",
            status="open",
            recommendation="Outcome-Reviews der nächsten 7-14 Tage priorisieren, um Hypothesen zu stabilisieren.",
        )
    else:
        _upsert_blind_spot(
            conn,
            key="model_many_testing_hypotheses",
            title="Viele offene Hypothesen",
            description="Die Zahl offener Modelltests ist aktuell reduziert.",
            severity="low",
            domain="system",
            status="reduced",
            recommendation="Tests weiter laufen lassen, bis die verbleibenden Hypothesen klare Evidenz haben.",
        )


def run_personal_learning_pass(
    *,
    coverage: dict[str, Any] | None = None,
    as_of: date | None = None,
    lookback_days: int = 60,
) -> dict[str, Any]:
    coverage = coverage or {}
    as_of = as_of or date.today()
    lookback_days = max(21, min(730, int(lookback_days or 60)))
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        candidates = _build_hypothesis_candidates(conn, as_of=as_of, lookback_days=lookback_days)
        changed = 0
        statuses: dict[str, int] = {"testing": 0, "active": 0, "weakened": 0, "rejected": 0}
        for item in candidates:
            result = _upsert_hypothesis(conn, item)
            statuses[result["status"]] = statuses.get(result["status"], 0) + 1
            if result["changed"]:
                changed += 1

        _refresh_blind_spots(conn, coverage, statuses)
        conn.commit()

        return {
            "ok": True,
            "updated_hypotheses": len(candidates),
            "changed": changed,
            "status_counts": statuses,
        }
    finally:
        conn.close()


def _factor_weights(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    hypo_rows = conn.execute(
        "SELECT hypothesis_key, title, domain, status, confidence, impact_score FROM core_hypotheses ORDER BY impact_score DESC, confidence DESC"
    ).fetchall()

    principles = get_core_principle_scores()

    factors: dict[str, float] = {
        "nutrition": 0.18,
        "sleep_late_shutdown": 0.12,
        "hrv_rhr": 0.14,
        "local_fatigue": 0.14,
        "progress_stagnation": 0.16,
        "run_interference": 0.1,
        "logging_confidence": 0.08,
        "plan_adherence": 0.08,
    }

    for row in hypo_rows:
        status = str(row["status"] or "testing")
        mult = 1.0 if status == "active" else (0.55 if status == "testing" else (0.35 if status == "weakened" else 0.15))
        weight = _safe_float(row["confidence"]) * _safe_float(row["impact_score"]) * mult
        key = str(row["hypothesis_key"] or "")
        if "nutrition" in key:
            factors["nutrition"] += weight * 0.42
        if "late_shutdown" in key:
            factors["sleep_late_shutdown"] += weight * 0.45
        if "hrv" in key:
            factors["hrv_rhr"] += weight * 0.35
        if "pull_fatigue" in key:
            factors["local_fatigue"] += weight * 0.38
        if "plateau" in key:
            factors["progress_stagnation"] += weight * 0.45

    plan_first = principles.get("plan_first") if isinstance(principles, dict) else None
    if isinstance(plan_first, dict):
        factors["plan_adherence"] += _safe_float(plan_first.get("strength"), 0.0) / 320.0

    logging_quality = principles.get("logging_quality") if isinstance(principles, dict) else None
    if isinstance(logging_quality, dict):
        factors["logging_confidence"] += _safe_float(logging_quality.get("strength"), 0.0) / 420.0

    total = sum(max(0.01, v) for v in factors.values())
    out = []
    label_map = {
        "nutrition": "Ernährung",
        "sleep_late_shutdown": "Spät runterfahren / Schlaf",
        "hrv_rhr": "HRV / RHR",
        "local_fatigue": "Lokale Fatigue",
        "progress_stagnation": "Progress-Stagnation",
        "run_interference": "Run/Gym-Interferenz",
        "logging_confidence": "Logging-Vertrauen",
        "plan_adherence": "Plan-Adherence",
    }
    for key, value in factors.items():
        score = max(0.01, value) / total
        out.append(
            {
                "key": key,
                "label": label_map.get(key, key),
                "weight": round(score, 3),
                "weight_pct": round(score * 100, 1),
            }
        )
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


def get_model_state_payload() -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        hypotheses = [dict(row) for row in conn.execute("SELECT * FROM core_hypotheses ORDER BY updated_at DESC").fetchall()]
        blind_spots = [dict(row) for row in conn.execute("SELECT * FROM core_blind_spots ORDER BY updated_at DESC").fetchall()]
        changes = [dict(row) for row in conn.execute("SELECT * FROM core_model_updates ORDER BY created_at DESC LIMIT 60").fetchall()]

        explained_hyp = [explain_hypothesis_v2(h) for h in hypotheses]
        profile_traits: list[str] = []

        for h in explained_hyp:
            if h["status"] == "active" and h["confidence"] >= 0.62:
                profile_traits.append(h["statement"])
        if not profile_traits:
            profile_traits.append("Das Modell ist noch in einer frühen Lernphase und priorisiert konservative Entscheidungen.")

        factors = _factor_weights(conn)

        return {
            "ok": True,
            "generated_at": _utc_now(),
            "profile": {
                "title": "Athlete Profile",
                "traits": profile_traits[:6],
                "learning_stage": "stabil" if len([h for h in explained_hyp if h["status"] == "active"]) >= 3 else "aufbau",
            },
            "factor_weights": factors,
            "blind_spots": blind_spots,
            "active_tests": [h for h in explained_hyp if h["status"] == "testing"],
            "hypotheses": explained_hyp,
            "model_changes": [summarize_model_change_v2(c) for c in changes],
        }
    finally:
        conn.close()
