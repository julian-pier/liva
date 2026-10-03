from __future__ import annotations

import json
from typing import Any


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pct(value: float, digits: int = 0) -> str:
    v = max(0.0, min(1.0, _safe_float(value)))
    return f"{round(v * 100, digits)}%"


def _confidence_label(value: float) -> str:
    v = _safe_float(value)
    if v >= 0.78:
        return "hoch"
    if v >= 0.55:
        return "mittel"
    if v >= 0.35:
        return "vorsichtig"
    return "niedrig"


def _stability_label(value: float) -> str:
    v = _safe_float(value)
    if v >= 0.75:
        return "sitzt"
    if v >= 0.5:
        return "stabilisiert sich"
    if v >= 0.3:
        return "wacklig"
    return "instabil"


def explain_pattern_v2(pattern: dict[str, Any]) -> dict[str, Any]:
    evidence = _parse_json(pattern.get("evidence_json"), [])
    confidence = _safe_float(pattern.get("confidence"))
    stability = _safe_float(pattern.get("stability_score"))
    status = str(pattern.get("status") or "monitoring")
    summary = str(pattern.get("summary") or "").strip()
    title = str(pattern.get("title") or pattern.get("pattern_key") or "Pattern").strip()
    domain = str(pattern.get("domain") or "system")

    why_parts: list[str] = []
    for item in evidence[:3]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("signal") or "Signal").strip()
        value = str(item.get("value") or item.get("text") or "").strip()
        if value:
            why_parts.append(f"{label}: {value}")
        else:
            why_parts.append(label)

    if why_parts:
        why_text = " · ".join(why_parts)
    else:
        why_text = "Noch wenig direkte Evidenz; CORE führt das Pattern aktuell im Monitoring-Modus."

    explain_text = (
        f"{title} wird aktuell als {status} geführt. "
        f"Sicherheit {_confidence_label(confidence)} ({_pct(confidence)}), Stabilität {_stability_label(stability)}. "
        f"{summary or 'Es gibt ein erkennbares Muster, das derzeit weiter beobachtet wird.'} "
        f"Begründung: {why_text}"
    )

    return {
        "id": pattern.get("id"),
        "pattern_key": pattern.get("pattern_key"),
        "title": title,
        "summary": summary,
        "domain": domain,
        "status": status,
        "confidence": round(confidence, 3),
        "confidence_label": _confidence_label(confidence),
        "stability_score": round(stability, 3),
        "stability_label": _stability_label(stability),
        "evidence_count": int(pattern.get("evidence_count") or len(evidence)),
        "support_score": round(_safe_float(pattern.get("support_score")), 3),
        "contradiction_score": round(_safe_float(pattern.get("contradiction_score")), 3),
        "first_seen": str(pattern.get("first_seen") or ""),
        "last_seen": str(pattern.get("last_seen") or ""),
        "notes": str(pattern.get("notes") or ""),
        "why_it_matters": str(pattern.get("notes") or "").strip() or "Pattern beeinflusst die Tagessteuerung in seinem Domain-Bereich.",
        "evidence": evidence,
        "explain_text": explain_text,
    }


def explain_hypothesis_v2(hypothesis: dict[str, Any]) -> dict[str, Any]:
    evidence = _parse_json(hypothesis.get("evidence_json"), [])
    if isinstance(evidence, dict):
        evidence = [evidence]
    elif not isinstance(evidence, list):
        evidence = []
    confidence = _safe_float(hypothesis.get("confidence"))
    impact = _safe_float(hypothesis.get("impact_score"))
    support = int(hypothesis.get("support_count") or 0)
    contradiction = int(hypothesis.get("contradiction_count") or 0)
    total = max(1, support + contradiction)
    tested_ratio = support / total
    tested_label = "robust" if total >= 6 else ("früh" if total <= 2 else "im Aufbau")

    evidence_lines: list[str] = []
    for item in evidence[:3]:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("signal") or "Evidenz").strip()
            text = str(item.get("value") or item.get("text") or "").strip()
            if text:
                evidence_lines.append(f"{label}: {text}")
            else:
                evidence_lines.append(label)
    if not evidence_lines:
        evidence_lines.append("Keine harte Evidenz gespeichert; Hypothese bleibt Testkandidat.")

    statement = str(hypothesis.get("statement") or "").strip()
    explain_text = (
        f"{statement or hypothesis.get('title') or 'Hypothese'} "
        f"Status: {hypothesis.get('status') or 'testing'}. "
        f"Sicherheit {_confidence_label(confidence)} ({_pct(confidence)}), Wirkung {_pct(impact)}. "
        f"Bestätigungen {support}, Widersprüche {contradiction} ({tested_label}, Trefferquote {_pct(tested_ratio)})."
    )

    return {
        "id": hypothesis.get("id"),
        "hypothesis_key": hypothesis.get("hypothesis_key"),
        "title": str(hypothesis.get("title") or "Hypothese"),
        "statement": statement,
        "domain": str(hypothesis.get("domain") or "system"),
        "type": str(hypothesis.get("type") or "principle"),
        "status": str(hypothesis.get("status") or "testing"),
        "confidence": round(confidence, 3),
        "confidence_label": _confidence_label(confidence),
        "impact_score": round(impact, 3),
        "impact_label": _confidence_label(impact),
        "support_count": support,
        "contradiction_count": contradiction,
        "last_reviewed_at": str(hypothesis.get("last_reviewed_at") or ""),
        "relevance": str(hypothesis.get("relevance_text") or ""),
        "evidence": evidence,
        "evidence_preview": evidence_lines,
        "explain_text": explain_text,
    }


def summarize_model_change_v2(change: dict[str, Any]) -> dict[str, Any]:
    old_state = _parse_json(change.get("old_state_json"), {})
    new_state = _parse_json(change.get("new_state_json"), {})
    update_type = str(change.get("update_type") or "recalibrated")
    object_type = str(change.get("object_type") or "model")
    object_key = str(change.get("object_key") or "")

    old_status = str(old_state.get("status") or "")
    new_status = str(new_state.get("status") or "")
    old_conf = _safe_float(old_state.get("confidence"), -1.0)
    new_conf = _safe_float(new_state.get("confidence"), -1.0)

    delta_conf = None
    if old_conf >= 0 and new_conf >= 0:
        delta_conf = round(new_conf - old_conf, 3)

    parts = [f"{object_type}:{object_key}" if object_key else object_type]
    if old_status or new_status:
        parts.append(f"Status {old_status or 'n/a'} -> {new_status or 'n/a'}")
    if delta_conf is not None:
        sign = "+" if delta_conf >= 0 else ""
        parts.append(f"Confidence {sign}{round(delta_conf * 100)}pp")

    summary = str(change.get("reason_summary") or "").strip()
    if summary:
        parts.append(summary)

    return {
        "id": change.get("id"),
        "update_date": str(change.get("update_date") or ""),
        "update_type": update_type,
        "object_type": object_type,
        "object_key": object_key,
        "delta_confidence": delta_conf,
        "reason_summary": summary,
        "text": " · ".join([p for p in parts if p]),
        "old_state": old_state,
        "new_state": new_state,
    }


def explain_decision_v2(
    decision: dict[str, Any],
    *,
    patterns: list[dict[str, Any]] | None = None,
    hypotheses: list[dict[str, Any]] | None = None,
    blind_spots: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patterns = patterns or []
    hypotheses = hypotheses or []
    blind_spots = blind_spots or []

    why_lines = _parse_json(decision.get("why_json"), [])
    used_memory = _parse_json(decision.get("used_memory_json"), [])
    used_principles = _parse_json(decision.get("used_principles_json"), [])
    raw = _parse_json(decision.get("raw_json"), {})

    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    explain = session_payload.get("autopilot_explain") if isinstance(session_payload.get("autopilot_explain"), dict) else {}
    trace = explain.get("trace") if isinstance(explain.get("trace"), dict) else {}

    runtime_inputs = trace.get("runtime_inputs") if isinstance(trace.get("runtime_inputs"), dict) else {}
    recovery = runtime_inputs.get("recovery") if isinstance(runtime_inputs.get("recovery"), dict) else {}
    metrics = runtime_inputs.get("metrics") if isinstance(runtime_inputs.get("metrics"), dict) else {}
    classes = runtime_inputs.get("classes") if isinstance(runtime_inputs.get("classes"), dict) else {}
    flags = runtime_inputs.get("flags") if isinstance(runtime_inputs.get("flags"), dict) else {}

    feature_items: list[dict[str, Any]] = []
    if recovery:
        feature_items.append(
            {
                "label": "Recovery",
                "value": (
                    f"RMSSD-Verhältnis {round(_safe_float(recovery.get('hrv_ratio')), 2)} · "
                    f"RHR-Verhältnis {round(_safe_float(recovery.get('rhr_ratio')), 2)}"
                ),
                "source": "core_overlay.recovery",
            }
        )
    if metrics:
        feature_items.append(
            {
                "label": "Belastungsfenster",
                "value": (
                    f"Erwartet 7d: {int(metrics.get('expected_7d') or 0)}, "
                    f"geloggt: {int(metrics.get('logged_done_7d') or 0)}, "
                    f"Skips: {int(metrics.get('skipped_7d') or 0)}"
                ),
                "source": "core_overlay.metrics",
            }
        )
    if classes:
        feature_items.append(
            {
                "label": "Cluster",
                "value": ", ".join(f"{k}:{v}" for k, v in classes.items() if str(v).strip()),
                "source": "core_overlay.classes",
            }
        )
    active_flags = [k for k, v in flags.items() if bool(v)]
    if active_flags:
        feature_items.append(
            {
                "label": "Aktive Flags",
                "value": ", ".join(active_flags[:6]),
                "source": "core_overlay.flags",
            }
        )

    domain = str(decision.get("type") or "System").strip().lower()
    domain_map = {
        "training": "training",
        "run": "run",
        "recovery": "recovery",
        "nutrition": "nutrition",
        "system": "system",
    }
    domain_key = domain_map.get(domain, "system")

    relevant_patterns = [
        explain_pattern_v2(p)
        for p in patterns
        if str(p.get("domain") or "").strip().lower() in {domain_key, "system"}
    ]
    relevant_hypotheses = [
        explain_hypothesis_v2(h)
        for h in hypotheses
        if str(h.get("domain") or "").strip().lower() in {domain_key, "system"}
        and str(h.get("status") or "") in {"active", "testing", "weakened"}
    ]

    uncertainty = []
    for spot in blind_spots:
        if str(spot.get("status") or "") in {"open", "monitoring"} and str(spot.get("domain") or "") in {domain_key, "system"}:
            uncertainty.append(
                {
                    "title": str(spot.get("title") or "Unsicherheit"),
                    "description": str(spot.get("description") or ""),
                    "recommendation": str(spot.get("recommendation") or ""),
                }
            )

    expected_effect = str(decision.get("impact_text") or "").strip() or "Die Maßnahme soll den Tag robuster machen."
    falsify_checks = [
        "Wenn sich die Folgetage trotz Maßnahme nicht stabilisieren, wird diese Entscheidung abgeschwächt.",
        "Wenn ähnliche Lagebilder wiederholt ein besseres Ergebnis mit Gegenmaßnahme zeigen, wird die Logik rekalibriert.",
    ]

    if str(decision.get("severity") or "").lower().startswith("major"):
        falsify_checks.insert(0, "Bei Major-Eingriff: Wenn der Schutz keinen messbaren Nutzen bringt, gilt der Eingriff als zu hart.")

    summary_line = str(explain.get("today") or "").strip() or str(decision.get("decision_text") or "").strip()
    if not summary_line:
        summary_line = "CORE hat eine operative Entscheidung für heute getroffen."

    if review and isinstance(review, dict):
        review_text = str(review.get("generated_summary") or "").strip()
    else:
        review_text = "Noch kein Outcome-Review vorhanden."

    return {
        "decision_id": decision.get("id"),
        "day_iso": str(decision.get("day_iso") or ""),
        "type": str(decision.get("type") or "System"),
        "severity": str(decision.get("severity") or "Daily micro"),
        "decision_text": str(decision.get("decision_text") or ""),
        "summary": summary_line,
        "why": [str(x).strip() for x in why_lines if str(x).strip()],
        "top_contributing_signals": feature_items,
        "interpreted_patterns": relevant_patterns[:5],
        "personal_rules": relevant_hypotheses[:5],
        "used_memory": used_memory,
        "used_principles": used_principles,
        "uncertainty": uncertainty,
        "expected_effect": expected_effect,
        "falsify_checks": falsify_checks,
        "review": review,
        "review_summary": review_text,
        "explain_text": (
            f"{summary_line}. "
            f"Die Entscheidung stützt sich auf {len(feature_items)} Hauptsignale, "
            f"{len(relevant_patterns)} interpretierte Muster und {len(relevant_hypotheses)} gelernte Regeln. "
            f"Unsicherheiten: {len(uncertainty)}."
        ),
    }
