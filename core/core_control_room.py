from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import current_app

from analysis.training_analysis import compare_top_sets, compute_trend_from_history, get_exercise_history
from core.core_training_deck import get_core_principle_scores
from core.core_daily_decision import get_daily_decision
from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_plans_db, get_runs_db, get_training_db
from integrations.telegram_hub import build_variation_proposal, propose_training_adjustment


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-") or "memory"


PLATEAU_RECENT_WINDOW_DAYS = 120
PLATEAU_MAX_LAST_SEEN_DAYS = 45
PLATEAU_MIN_RECENT_EXPOSURES = 6
PLATEAU_MIN_COMPARISONS = 4
PLATEAU_COMPARE_WINDOW = 5
PLATEAU_MIN_STALLED_COMPARISONS = 2


def ensure_core_control_room_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_state (
                memory_key TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                always_on INTEGER NOT NULL DEFAULT 0,
                test_until TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                day_iso TEXT NOT NULL,
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                decision_text TEXT NOT NULL,
                impact_text TEXT,
                why_json TEXT NOT NULL DEFAULT '[]',
                used_memory_json TEXT NOT NULL DEFAULT '[]',
                used_principles_json TEXT NOT NULL DEFAULT '[]',
                outcome_text TEXT,
                override_flag INTEGER NOT NULL DEFAULT 0,
                major_flag INTEGER NOT NULL DEFAULT 0,
                reevaluate_on TEXT,
                exit_criterion TEXT,
                source TEXT NOT NULL DEFAULT 'system',
                raw_json TEXT NOT NULL DEFAULT '{}',
                telegram_sent_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_decision_log_day ON core_decision_log(day_iso DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_decision_log_type ON core_decision_log(type, day_iso DESC)")
        conn.commit()
    finally:
        conn.close()


def _invoke_json(path: str, endpoint: str) -> dict[str, Any]:
    with current_app.test_request_context(path):
        resp = current_app.view_functions[endpoint]()
    if isinstance(resp, tuple):
        resp = resp[0]
    if hasattr(resp, "get_json"):
        payload = resp.get_json(silent=True)
        if isinstance(payload, dict):
            return payload
    return {}


def _load_memory_state_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM core_memory_state").fetchall()
    return {
        str(row["memory_key"]): {
            "status": str(row["status"] or "active"),
            "always_on": bool(row["always_on"]),
            "test_until": str(row["test_until"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    }


def update_memory_state(memory_key: str, action: str) -> dict[str, Any]:
    ensure_core_control_room_schema()
    key = str(memory_key or "").strip()
    if not key:
        raise ValueError("invalid_memory_key")
    action = str(action or "").strip().lower()
    status = "active"
    always_on = 0
    test_until = None
    if action == "testing":
        status = "testing"
        test_until = (date.today() + timedelta(days=14)).isoformat()
    elif action == "ignored":
        status = "ignored"
    elif action == "archived":
        status = "archived"
    elif action == "always":
        status = "active"
        always_on = 1
    elif action != "active":
        raise ValueError("invalid_action")

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO core_memory_state (memory_key, status, always_on, test_until, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_key) DO UPDATE SET
                status=excluded.status,
                always_on=excluded.always_on,
                test_until=excluded.test_until,
                updated_at=excluded.updated_at
            """,
            (key, status, always_on, test_until, _utc_now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM core_memory_state WHERE memory_key=?", (key,)).fetchone()
        return {
            "memory_key": key,
            "status": str(row["status"] or "active"),
            "always_on": bool(row["always_on"]),
            "test_until": str(row["test_until"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
    finally:
        conn.close()


def _coverage_ratio(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> float:
    try:
        row = conn.execute(sql, params).fetchone()
        count = int(row[0] or 0) if row else 0
    except Exception:
        count = 0
    return max(0.0, min(1.0, count / 14.0))


def _exercise_plateau_snapshot(history_rows: list[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    rows = [dict(row) for row in history_rows if isinstance(row, dict)]
    if not rows:
        return {
            "recent_history": [],
            "history_count_total": 0,
            "history_count_recent": 0,
            "comparison_count": 0,
            "stalled_count": 0,
            "progress_count": 0,
            "unknown_count": 0,
            "latest_stalled": False,
            "trend": "neutral",
            "progressed": None,
            "should_trigger": False,
            "plateau_signature": "",
        }

    today = today or date.today()
    recent_cutoff = today - timedelta(days=PLATEAU_RECENT_WINDOW_DAYS)
    ordered: list[dict[str, Any]] = []
    for row in rows:
        try:
            row_date = date.fromisoformat(str(row.get("date") or ""))
        except Exception:
            continue
        ordered.append({**row, "date_obj": row_date})
    ordered.sort(key=lambda item: item["date_obj"])
    recent_history = [item for item in ordered if item["date_obj"] >= recent_cutoff]
    if not recent_history:
        return {
            "recent_history": [],
            "history_count_total": len(ordered),
            "history_count_recent": 0,
            "comparison_count": 0,
            "stalled_count": 0,
            "progress_count": 0,
            "unknown_count": 0,
            "latest_stalled": False,
            "trend": "neutral",
            "progressed": None,
            "should_trigger": False,
            "plateau_signature": "",
        }

    latest = recent_history[-1]
    previous = recent_history[-2] if len(recent_history) >= 2 else None
    progressed = compare_top_sets(latest, previous)
    trend = compute_trend_from_history(recent_history[-8:], lookback=min(8, len(recent_history)))

    compare_window_rows = recent_history[-(PLATEAU_COMPARE_WINDOW + 1):]
    comparison_results: list[bool | None] = []
    for idx in range(1, len(compare_window_rows)):
        comparison_results.append(compare_top_sets(compare_window_rows[idx], compare_window_rows[idx - 1]))
    stalled_count = sum(result is False for result in comparison_results)
    progress_count = sum(result is True for result in comparison_results)
    unknown_count = sum(result is None for result in comparison_results)
    latest_stalled = bool(comparison_results and comparison_results[-1] is False)

    latest_date = latest["date_obj"]
    history_start = recent_history[0]["date_obj"]
    enough_recent_work = len(recent_history) >= PLATEAU_MIN_RECENT_EXPOSURES
    enough_comparisons = len(comparison_results) >= PLATEAU_MIN_COMPARISONS
    repeated_stall = stalled_count >= PLATEAU_MIN_STALLED_COMPARISONS
    currently_relevant = (today - latest_date).days <= PLATEAU_MAX_LAST_SEEN_DAYS
    stable_signal = latest_stalled or trend in {"flat", "down"}
    no_clear_progress = progress_count <= stalled_count
    should_trigger = all(
        [
            enough_recent_work,
            enough_comparisons,
            repeated_stall,
            currently_relevant,
            stable_signal,
            no_clear_progress,
        ]
    )

    return {
        "recent_history": recent_history,
        "history_count_total": len(ordered),
        "history_count_recent": len(recent_history),
        "history_start": history_start.isoformat(),
        "history_end": latest_date.isoformat(),
        "history_span_days": max(0, (latest_date - history_start).days),
        "latest_date": latest_date,
        "comparison_count": len(comparison_results),
        "stalled_count": stalled_count,
        "progress_count": progress_count,
        "unknown_count": unknown_count,
        "latest_stalled": latest_stalled,
        "trend": trend,
        "progressed": progressed,
        "should_trigger": should_trigger,
        "plateau_signature": f"{latest_date.isoformat()}:{stalled_count}:{progress_count}",
    }


def _data_coverage_payload() -> dict[str, Any]:
    since = (date.today() - timedelta(days=13)).isoformat()
    training = get_training_db()
    hrv = get_hrv_db()
    nutrition = get_nutrition_db()
    runs = get_runs_db()
    try:
        coverage = {
            "training": _coverage_ratio(training, "SELECT COUNT(DISTINCT date_iso) FROM workouts WHERE date_iso >= ?", (since,)),
            "gewicht": _coverage_ratio(nutrition, "SELECT COUNT(DISTINCT date_iso) FROM weight_logs WHERE date_iso >= ?", (since,)),
            "hrv": _coverage_ratio(hrv, "SELECT COUNT(DISTINCT date_utc) FROM hrv_measurements WHERE date_utc >= ?", (since,)),
            "runs": _coverage_ratio(runs, "SELECT COUNT(DISTINCT substr(date,1,10)) FROM runs WHERE substr(date,1,10) >= ?", (since,)),
            "ernährung": _coverage_ratio(nutrition, "SELECT COUNT(DISTINCT date_iso) FROM nutrition_daily WHERE date_iso >= ?", (since,)),
        }
    finally:
        training.close()
        hrv.close()
        nutrition.close()
        runs.close()
    avg = sum(coverage.values()) / max(1, len(coverage))
    if avg >= 0.72:
        quality = "Gut"
        note = "genug aktuelle Einträge für stabile Entscheidungen"
    elif avg >= 0.42:
        quality = "Mittel"
        note = "brauchbar, aber nicht überall sauber gefüttert"
    else:
        quality = "Schwach"
        note = "zu viele Lücken, CORE bleibt konservativ"
    return {"coverage": coverage, "avg": avg, "quality": quality, "note": note}


def _principle_impacts(principles: dict[str, Any]) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    def add(topic: str, effect: str) -> None:
        item = principles.get(topic)
        if not item:
            return
        impacts.append(
            {
                "memory_key": f"principle:{topic}",
                "title": item["label"],
                "topic": topic,
                "strength": int(item.get("strength") or 0),
                "risk": int(item.get("risk") or 0),
                "effect": effect,
            }
        )

    if int(principles.get("rpe_honesty", {}).get("risk") or 0) >= 56:
        add("rpe_honesty", "RPE vorsichtiger deuten")
    if int(principles.get("logging_quality", {}).get("risk") or 0) >= 56:
        add("logging_quality", "Datenlage konservativer lesen")
    if int(principles.get("nutrition_discipline", {}).get("risk") or 0) >= 56:
        add("nutrition_discipline", "Ernährung robuster und einfacher halten")
    if int(principles.get("regeneration_honesty", {}).get("risk") or 0) >= 56:
        add("regeneration_honesty", "Erholung ernster gewichten")
    if int(principles.get("run_quality", {}).get("risk") or 0) >= 56:
        add("run_quality", "Qualitätsläufe früher absichern")
    if int(principles.get("regression_response", {}).get("risk") or 0) >= 56:
        add("regression_response", "Stagnation früher sauber benennen")
    if int(principles.get("plan_first", {}).get("strength") or 0) >= 64:
        add("plan_first", "näher am Plan bleiben")
    return impacts


def _load_plateau_memories(state_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    plans = get_plans_db()
    plans.row_factory = sqlite3.Row
    active_plan = None
    try:
        active_plan = plans.execute(
            """
            SELECT id, title, plan_json
            FROM gym_plans
            WHERE is_active = 1 AND is_archived = 0
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        active_plan = None
    finally:
        plans.close()
    if not active_plan:
        return []

    try:
        plan_json = json.loads(active_plan["plan_json"]) if active_plan["plan_json"] else {}
    except Exception:
        plan_json = {}
    days = plan_json.get("days") if isinstance(plan_json.get("days"), list) else []
    active_items: list[dict[str, Any]] = []
    seen_plan_keys: set[tuple[str, str, str, str]] = set()
    for day in days:
        if not isinstance(day, dict):
            continue
        day_name = str(day.get("day") or "").strip()
        for event in (day.get("events") or []):
            if not isinstance(event, dict) or str(event.get("kind") or "").strip().lower() != "gym":
                continue
            session_title = str(event.get("title") or "").strip()
            for item in (event.get("items") or []):
                if not isinstance(item, dict) or str(item.get("kind") or "").strip().lower() != "exercise":
                    continue
                name = str(item.get("name") or "").strip()
                variation = str(item.get("variation") or "").strip()
                item_id = str(item.get("id") or "").strip()
                if not name:
                    continue
                plan_key = (day_name, session_title, name.lower(), variation.lower())
                if plan_key in seen_plan_keys:
                    continue
                seen_plan_keys.add(plan_key)
                active_items.append(
                    {
                        "day": day_name,
                        "session_title": session_title,
                        "name": name,
                        "variation": variation,
                        "item_id": item_id,
                        "reps": item.get("reps") if isinstance(item.get("reps"), dict) else {},
                        "rpe_list": list(item.get("rpe_list") or []) if isinstance(item.get("rpe_list"), list) else [],
                    }
                )

    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    memories: list[dict[str, Any]] = []
    try:
        today = date.today()
        for plan_item in active_items:
            name = plan_item["name"]
            variation = plan_item["variation"]
            history = get_exercise_history(conn, name, variation=variation if variation else None)
            snapshot = _exercise_plateau_snapshot(history, today=today)
            recent_history = snapshot["recent_history"]
            if not snapshot["should_trigger"]:
                continue
            key = f"exercise:{_slug(name)}:{_slug(variation or 'base')}"
            overlay = state_map.get(key, {})
            status = str(overlay.get("status") or "active")
            confidence = min(
                94,
                58
                + int(snapshot["history_count_recent"]) * 3
                + int(snapshot["stalled_count"]) * 5
                + (5 if snapshot["trend"] == "down" else 0),
            )
            title = f"{name}{f' ({variation})' if variation else ''}: wiederholt ohne Progress im aktiven Plan"
            action = "Option prüfen: nichts ändern, Rep-Range ändern, RPE senken oder Variation wechseln"
            if "row" in name.lower():
                action = "Alternative früh testen, bevor noch mehr Wiederholungen versanden"
            latest_txt = str(snapshot["history_end"] or "")
            first_txt = str(snapshot["history_start"] or "")
            delta_days = int(snapshot["history_span_days"] or 0)
            progress_label = (
                "letzter Vergleich ohne Fortschritt"
                if snapshot["latest_stalled"]
                else ("Trend fällt" if snapshot["trend"] == "down" else "Trend flach")
            )
            memories.append(
                {
                    "memory_key": key,
                    "title": title,
                    "type": "Exercise",
                    "confidence": confidence,
                    "n": len(recent_history),
                    "scope": f"aktiv im Plan: {plan_item['day']} · {plan_item['session_title'] or 'Gym'}",
                    "action": action,
                    "status": status,
                    "why": (
                        f"{snapshot['history_count_recent']} relevante Exposures in {delta_days} Tagen "
                        f"({first_txt} bis {latest_txt}); {snapshot['stalled_count']} von {snapshot['comparison_count']} "
                        f"letzten Vergleichen ohne Fortschritt, {progress_label}."
                    ),
                    "evidence": [{"label": str(item.get("date") or "-"), "ref": str(item.get("date") or "-")} for item in recent_history[-4:]],
                    "last_updated": latest_txt,
                    "impact_count": 0,
                    "always_on": bool(overlay.get("always_on")),
                    "test_until": str(overlay.get("test_until") or ""),
                    "raw": {
                        "exercise_name": name,
                        "current_variation": variation,
                        "day": plan_item["day"],
                        "session_title": plan_item["session_title"],
                        "item_id": plan_item["item_id"],
                        "reps": plan_item["reps"],
                        "rpe_list": plan_item["rpe_list"],
                        "history_count_recent": len(recent_history),
                        "history_count_total": int(snapshot["history_count_total"] or 0),
                        "history_start": first_txt,
                        "history_end": latest_txt,
                        "history_span_days": delta_days,
                        "trend": snapshot["trend"],
                        "progressed": snapshot["progressed"],
                        "comparison_count": int(snapshot["comparison_count"] or 0),
                        "stalled_count": int(snapshot["stalled_count"] or 0),
                        "progress_count": int(snapshot["progress_count"] or 0),
                        "unknown_count": int(snapshot["unknown_count"] or 0),
                        "latest_stalled": bool(snapshot["latest_stalled"]),
                        "plateau_signature": str(snapshot["plateau_signature"] or ""),
                    },
                }
            )
    except Exception:
        return []
    finally:
        conn.close()
    return memories


def _principle_memories(principles: dict[str, Any], state_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    action_map = {
        "plan_first": "lieber am Plan halten und nur nötige Abweichungen zulassen",
        "rpe_honesty": "RPE konservativer interpretieren",
        "logging_quality": "Datenlage vorsichtiger lesen",
        "nutrition_discipline": "Meal-Entscheidungen robuster und einfacher halten",
        "regeneration_honesty": "Erholung früher schützen",
        "run_quality": "Qualitätsläufe strenger absichern",
        "regression_response": "Plateaus früher als echten Ansatzwechsel markieren",
    }
    scope_map = {
        "plan_first": "vor allem an Tagen mit Alternativen",
        "rpe_honesty": "bei Progression und Satzhärte",
        "logging_quality": "wenn Logs auffällig glatt oder schön aussehen",
        "nutrition_discipline": "an stressigen Tagen oder im Defizit",
        "regeneration_honesty": "nach schlechter Nacht oder hohem Stress",
        "run_quality": "bei Qualitätsläufen und dichten Wochen",
        "regression_response": "wenn eine Übung über Wochen hängt",
    }
    memories: list[dict[str, Any]] = []
    for topic, item in principles.items():
        overlay = state_map.get(f"principle:{topic}", {})
        strength = int(item.get("strength") or 0)
        risk = int(item.get("risk") or 0)
        confidence = int(item.get("confidence") or 0)
        effect = strength if topic == "plan_first" else max(strength, risk)
        memories.append(
            {
                "memory_key": f"principle:{topic}",
                "title": f"{item['label']}: {'sitzt' if strength >= risk else 'gerade wacklig'}",
                "type": "Principle",
                "confidence": max(confidence, effect),
                "n": int(item.get("exposures") or 0),
                "scope": scope_map.get(topic, "im aktuellen Steuerfenster"),
                "action": action_map.get(topic, "vorsichtiger interpretieren"),
                "status": str(overlay.get("status") or "active"),
                "why": (
                    f"Strength {strength}% / Risk {risk}% aus dem aktuellen Antwortverlauf."
                ),
                "evidence": [
                    {"label": f"Strength {strength}%", "ref": f"strength:{strength}"},
                    {"label": f"Risk {risk}%", "ref": f"risk:{risk}"},
                ],
                "last_updated": _today_iso(),
                "impact_count": 0,
                "always_on": bool(overlay.get("always_on")),
                "test_until": str(overlay.get("test_until") or ""),
                "principle": {
                    "strength": strength,
                    "risk": risk,
                },
            }
        )
    return memories


def _memory_catalog() -> list[dict[str, Any]]:
    ensure_core_control_room_schema()
    principles = get_core_principle_scores()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        state_map = _load_memory_state_map(conn)
    finally:
        conn.close()
    memories = _principle_memories(principles, state_map) + _load_plateau_memories(state_map)
    return memories


def _upsert_decision_event(event: dict[str, Any]) -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO core_decision_log (
                source_key, created_at, day_iso, type, severity, decision_text, impact_text,
                why_json, used_memory_json, used_principles_json, outcome_text, override_flag,
                major_flag, reevaluate_on, exit_criterion, source, raw_json, telegram_sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                created_at=excluded.created_at,
                day_iso=excluded.day_iso,
                type=excluded.type,
                severity=excluded.severity,
                decision_text=excluded.decision_text,
                impact_text=excluded.impact_text,
                why_json=excluded.why_json,
                used_memory_json=excluded.used_memory_json,
                used_principles_json=excluded.used_principles_json,
                outcome_text=excluded.outcome_text,
                override_flag=excluded.override_flag,
                major_flag=excluded.major_flag,
                reevaluate_on=excluded.reevaluate_on,
                exit_criterion=excluded.exit_criterion,
                source=excluded.source,
                raw_json=excluded.raw_json
            """,
            (
                event["source_key"],
                event["created_at"],
                event["day_iso"],
                event["type"],
                event["severity"],
                event["decision_text"],
                event.get("impact_text"),
                json.dumps(event.get("why") or [], ensure_ascii=False),
                json.dumps(event.get("used_memory") or [], ensure_ascii=False),
                json.dumps(event.get("used_principles") or [], ensure_ascii=False),
                event.get("outcome_text"),
                1 if event.get("override_flag") else 0,
                1 if event.get("major_flag") else 0,
                event.get("reevaluate_on"),
                event.get("exit_criterion"),
                event.get("source") or "system",
                json.dumps(event.get("raw") or {}, ensure_ascii=False),
                event.get("telegram_sent_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_telegram_sent(source_key: str) -> None:
    conn = get_core_db()
    try:
        conn.execute("UPDATE core_decision_log SET telegram_sent_at=? WHERE source_key=?", (_utc_now(), source_key))
        conn.commit()
    finally:
        conn.close()


def _sync_override_events() -> None:
    training = get_training_db()
    training.row_factory = sqlite3.Row
    try:
        rows = training.execute(
            """
            SELECT ts, decision_type, proposed_json, applied_json, user_override_bool, context_json
            FROM override_log
            ORDER BY ts DESC
            LIMIT 80
            """
        ).fetchall()
    except Exception:
        training.close()
        return
    finally:
        try:
            training.close()
        except Exception:
            pass

    for row in rows:
        ts = int(row["ts"] or 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        decision_type = str(row["decision_type"] or "override").strip().lower()
        proposed = _parse_json(row["proposed_json"], {})
        applied = _parse_json(row["applied_json"], {})
        label_map = {
            "day_override": ("System", "Major", "Tag manuell verschoben", "anderer Tag gilt ab jetzt"),
            "manual_override": ("Training", "Major", "Plan manuell angepasst", "manueller Eingriff ersetzt die Standardsteuerung"),
            "autopilot_override": ("System", "Major", "CORE überstimmt", "Nutzerentscheidung geht vor"),
            "target_override": ("Training", "Daily micro", "Target angepasst", "Trainingsziel wurde manuell verändert"),
            "volume_intensity_override": ("Training", "Daily micro", "Volumen oder Intensität geändert", "Satz- oder Intensitätsziel angepasst"),
            "skip_rest_override": ("Recovery", "Major", "Rest bewusst überschrieben", "Erholungsvorschlag wurde verworfen"),
        }
        mapped = label_map.get(decision_type)
        if not mapped:
            continue
        event_type, severity, text, impact = mapped
        source_key = f"override:{ts}:{decision_type}"
        why = ["Manueller Eingriff hat Vorrang."]
        if decision_type == "day_override":
            why = ["Alter Link oder manuelle Auswahl hat den Tag geändert."]
        _upsert_decision_event(
            {
                "source_key": source_key,
                "created_at": dt.isoformat().replace("+00:00", "Z"),
                "day_iso": dt.date().isoformat(),
                "type": event_type,
                "severity": severity,
                "decision_text": text,
                "impact_text": impact,
                "why": why,
                "used_memory": [],
                "used_principles": [],
                "override_flag": bool(row["user_override_bool"]),
                "major_flag": severity == "Major",
                "source": "override_log",
                "raw": {"proposed": proposed, "applied": applied, "context": _parse_json(row["context_json"], {})},
            }
        )


def _build_today_event(memories: list[dict[str, Any]]) -> dict[str, Any]:
    session_payload = _invoke_json("/api/next_session?explain=1", "api_next_session")
    daily_decision = get_daily_decision(_today_iso()) or {}
    explain = session_payload.get("autopilot_explain") if isinstance(session_payload.get("autopilot_explain"), dict) else {}
    planned = session_payload.get("planned_day") if isinstance(session_payload.get("planned_day"), dict) else {}
    final_day = session_payload.get("final_day") if isinstance(session_payload.get("final_day"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    decision_meta = meta.get("decision_meta") if isinstance(meta.get("decision_meta"), dict) else {}
    core_overlay = decision_meta.get("core_overlay") if isinstance(decision_meta.get("core_overlay"), dict) else {}
    principles = get_core_principle_scores()
    principle_impacts = _principle_impacts(principles)
    memory_by_key = {m["memory_key"]: m for m in memories}
    used_memory = []
    for item in principle_impacts:
        memory = memory_by_key.get(item["memory_key"])
        if memory and memory.get("status") not in {"ignored", "archived"}:
            used_memory.append(
                {
                    "memory_key": memory["memory_key"],
                    "title": memory["title"],
                    "type": memory["type"],
                }
            )

    active_plateau = next(
        (
            {
                "memory_key": memory["memory_key"],
                "title": memory["title"],
                "type": memory["type"],
            }
            for memory in memories
            if memory.get("type") == "Exercise" and memory.get("status") not in {"ignored", "archived"}
        ),
        None,
    )
    if active_plateau:
        used_memory.append(active_plateau)

    planned_label = str(planned.get("label") or planned.get("session_name") or "Nichts geplant").strip()
    final_label = str(final_day.get("label") or session.get("session_name") or planned_label).strip() or planned_label
    change_line = str(explain.get("change_line") or "").strip()
    today_line = str(explain.get("today") or "").strip()
    if not today_line:
        today_line = f"Heute: {final_label}"
    why_lines = [str(line).strip() for line in (explain.get("why_lines") or []) if str(line).strip()]
    for item in principle_impacts:
        if len(why_lines) >= 3:
            break
        why_lines.append(item["effect"])
    reason_codes = meta.get("reason_codes") or []
    major = any(code in {"CORE_LIGHT_GYM", "CORE_REENTRY_RUN", "CORE_REENTRY_GYM"} for code in reason_codes) or planned_label != final_label
    if daily_decision:
        decision_text = str(daily_decision.get("hero") or "").strip() or (change_line or f"{planned_label} -> {final_label}")
        unit = daily_decision.get("planned_unit") if isinstance(daily_decision.get("planned_unit"), dict) else {}
        final_label = str(unit.get("plan_name") or final_label).strip() or final_label
        reasons = [
            str(r.get("text") or "").strip()
            for r in (daily_decision.get("reasons") or [])
            if isinstance(r, dict) and str(r.get("text") or "").strip()
        ]
        if reasons:
            why_lines = reasons[:3]
    else:
        decision_text = change_line or f"{planned_label} -> {final_label}"
    severity = "Major" if major else "Daily micro"
    return {
        "source_key": f"today:{_today_iso()}",
        "created_at": _utc_now(),
        "day_iso": _today_iso(),
        "type": _decision_type_from_kind(str(final_day.get("kind") or "")),
        "severity": severity,
        "decision_text": decision_text,
        "impact_text": today_line.replace("Heute: ", ""),
        "why": why_lines[:3],
        "used_memory": used_memory[:3],
        "used_principles": principle_impacts[:4],
        "override_flag": planned_label != final_label,
        "major_flag": major,
        "reevaluate_on": (date.today() + timedelta(days=1)).isoformat(),
        "exit_criterion": str(explain.get("hint") or "Morgen neu prüfen, ob die Einheit sauber gepasst hat.").strip(),
        "source": "live_today",
        "raw": {
            "session_payload": session_payload,
            "core_overlay": core_overlay,
            "daily_decision": daily_decision,
        },
    }


def _decision_type_from_kind(kind: str) -> str:
    kind = str(kind or "").strip().lower()
    if kind == "run":
        return "Run"
    if kind == "rest":
        return "Recovery"
    if kind == "plan":
        return "Training"
    return "System"


_PLATEAU_COMPOUND_ALLOW = (
    "bank",
    "bench",
    "schrägbank",
    "incline press",
    "ohp",
    "overhead press",
    "military press",
    "rudern",
    "row",
    "latzug",
    "pull up",
    "chin up",
    "kniebeuge",
    "squat",
    "deadlift",
    "kreuzheben",
    "rdl",
    "beinpresse",
    "leg press",
    "split squat",
    "lunge",
    "hip thrust",
)
_PLATEAU_COMPOUND_BLOCK = (
    "fly",
    "flys",
    "flies",
    "reverse fly",
    "rear delt",
    "seitheben",
    "lateral raise",
    "curl",
    "trizeps",
    "triceps",
    "pushdown",
    "kickback",
    "wadenheben",
    "calf",
    "shrug",
    "face pull",
    "rotator",
    "crunch",
    "plank",
)


def _is_plateau_compound_candidate(memory: dict[str, Any]) -> bool:
    raw = memory.get("raw") if isinstance(memory.get("raw"), dict) else {}
    exercise_name = str(raw.get("exercise_name") or "").strip().lower()
    if not exercise_name:
        exercise_name = str(memory.get("title") or "").split(":", 1)[0].strip().lower()
    if not exercise_name:
        return False
    if any(token in exercise_name for token in _PLATEAU_COMPOUND_BLOCK):
        return False
    return any(token in exercise_name for token in _PLATEAU_COMPOUND_ALLOW)


def _plateau_priority(memory: dict[str, Any]) -> tuple[float, int]:
    raw = memory.get("raw") if isinstance(memory.get("raw"), dict) else {}
    stalled = int(raw.get("stalled_count") or 0)
    comparisons = int(raw.get("comparison_count") or 0)
    ratio = (stalled / comparisons) if comparisons > 0 else 0.0
    confidence = int(memory.get("confidence") or 0)
    score = ratio * 100.0 + comparisons * 2.0 + confidence * 0.2
    return score, comparisons


def _plateau_message_sent_today() -> bool:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM core_decision_log
            WHERE source='plateau_scan'
              AND day_iso=?
              AND telegram_sent_at IS NOT NULL
            LIMIT 1
            """,
            (_today_iso(),),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _sync_plateau_events(memories: list[dict[str, Any]]) -> None:
    if _plateau_message_sent_today():
        return
    candidates = [
        memory
        for memory in memories
        if memory.get("type") == "Exercise"
        and memory.get("status") not in {"ignored", "archived"}
        and _is_plateau_compound_candidate(memory)
    ]
    if not candidates:
        return
    candidates = sorted(candidates, key=_plateau_priority, reverse=True)[:1]
    for memory in candidates:
        memory_raw = memory.get("raw") if isinstance(memory.get("raw"), dict) else {}
        plateau_signature = str(memory_raw.get("plateau_signature") or memory_raw.get("history_end") or "current").strip()
        source_key = f"major:{memory['memory_key']}:{_slug(plateau_signature)}"
        event = {
            "source_key": source_key,
            "created_at": _utc_now(),
            "day_iso": _today_iso(),
            "type": "Training",
            "severity": "Major",
            "decision_text": f"{memory['title']} -> Ansatzwechsel testen",
            "impact_text": "3-5 Wochen andere Variation, Rep-Range oder Struktur",
            "why": [
                "genug Exposures ohne klaren Vorwärtsschritt",
                "gleiches Muster nicht endlos wiederholen",
            ],
            "used_memory": [{"memory_key": memory["memory_key"], "title": memory["title"], "type": memory["type"]}],
            "used_principles": [],
            "override_flag": False,
            "major_flag": True,
            "reevaluate_on": (date.today() + timedelta(days=28)).isoformat(),
            "exit_criterion": "Neu bewerten nach 4-6 Exposures oder sobald Leistung wieder sauber anzieht.",
            "source": "plateau_scan",
            "raw": {"memory": memory},
        }
        _upsert_decision_event(event)
        conn = get_core_db()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT telegram_sent_at FROM core_decision_log WHERE source_key=?", (source_key,)).fetchone()
        finally:
            conn.close()
        if row and row["telegram_sent_at"]:
            continue
        raw_memory = event.get("raw", {}).get("memory") if isinstance(event.get("raw"), dict) else {}
        memory_raw = raw_memory.get("raw") if isinstance(raw_memory.get("raw"), dict) else {}
        title = str((raw_memory or {}).get("title") or memory["title"]).strip()
        exercise_name = str(memory_raw.get("exercise_name") or (title.split(":", 1)[0].strip() if ":" in title else title.replace("kaum Progress erkennbar", "").strip()))
        current_variation = str(memory_raw.get("current_variation") or "").strip()
        proposal = build_variation_proposal(
            exercise_name=exercise_name,
            current_variation=current_variation,
            reason_text=str(memory.get("why") or "Mehrere Exposures ohne sauberen Vorwärtsschritt."),
        )
        result = propose_training_adjustment(
            source_key=source_key,
            exercise_name=proposal["exercise_name"],
            current_variation=proposal["current_variation"],
            proposed_variation=proposal["proposed_variation"],
            reason_text=proposal["reason_text"],
            raw={"memory": memory, "event": event, "plan_context": memory_raw},
        )
        if result.get("ok"):
            _mark_telegram_sent(source_key)
        else:
            try:
                current_app.logger.warning("core training telegram proposal failed: %s", result)
            except Exception:
                pass


def sync_control_room_state() -> None:
    ensure_core_control_room_schema()
    memories = _memory_catalog()
    _sync_override_events()
    _upsert_decision_event(_build_today_event(memories))
    _sync_plateau_events(memories)


def _decision_rows(days: int = 30) -> list[dict[str, Any]]:
    sync_control_room_state()
    since = (date.today() - timedelta(days=max(1, int(days) - 1))).isoformat()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM core_decision_log
            WHERE day_iso >= ?
            ORDER BY day_iso DESC, created_at DESC
            """,
            (since,),
        ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "id": int(row["id"]),
                    "source_key": str(row["source_key"]),
                    "day_iso": str(row["day_iso"]),
                    "created_at": str(row["created_at"]),
                    "type": str(row["type"]),
                    "severity": str(row["severity"]),
                    "decision_text": str(row["decision_text"]),
                    "impact_text": str(row["impact_text"] or ""),
                    "why": _parse_json(row["why_json"], []),
                    "used_memory": _parse_json(row["used_memory_json"], []),
                    "used_principles": _parse_json(row["used_principles_json"], []),
                    "outcome_text": str(row["outcome_text"] or ""),
                    "override_flag": bool(row["override_flag"]),
                    "major_flag": bool(row["major_flag"]),
                    "reevaluate_on": str(row["reevaluate_on"] or ""),
                    "exit_criterion": str(row["exit_criterion"] or ""),
                }
            )
        return out
    finally:
        conn.close()


def _health_payload(memories: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = _data_coverage_payload()
    active_memories = [memory for memory in memories if memory.get("status") not in {"ignored", "archived"}]
    decision_rate = round(len([item for item in decisions if item["day_iso"] >= (date.today() - timedelta(days=13)).isoformat()]) / 14.0, 2)
    override_rate = 0.0
    recent_decisions = [item for item in decisions if item["day_iso"] >= (date.today() - timedelta(days=27)).isoformat()]
    if recent_decisions:
        override_rate = sum(1 for item in recent_decisions if item.get("override_flag")) / len(recent_decisions)
    insight_utilization = 0.0
    principle_utilization = 0.0
    if recent_decisions:
        insight_utilization = sum(
            1
            for item in recent_decisions
            if (item.get("used_memory") or []) or (item.get("used_principles") or [])
        ) / len(recent_decisions)
        principle_utilization = sum(1 for item in recent_decisions if item.get("used_principles")) / len(recent_decisions)
    avg_conf = round(sum(int(item.get("confidence") or 0) for item in active_memories) / max(1, len(active_memories)))
    strongest = sorted(active_memories, key=lambda item: (int(item.get("confidence") or 0), int(item.get("impact_count") or 0)), reverse=True)[:5]
    note = "CORE bleibt konservativ, bis wieder mehr saubere Daten reinkommen." if coverage["quality"] == "Schwach" else "Datenlage reicht für normale Steuerung."
    return {
        "coverage": coverage,
        "decision_rate": decision_rate,
        "override_rate": round(override_rate * 100),
        "memory_utilization": round(insight_utilization * 100),
        "principle_utilization": round(principle_utilization * 100),
        "confidence_overview": {
            "avg_active_confidence": avg_conf,
            "top_memories": [{"title": item["title"], "confidence": int(item.get("confidence") or 0)} for item in strongest],
        },
        "note": note,
    }


def _today_payload(memories: list[dict[str, Any]]) -> dict[str, Any]:
    event = _build_today_event(memories)
    coverage = _data_coverage_payload()
    used_memory = event.get("used_memory") or []
    why = list(event.get("why") or [])
    status = "STABIL"
    if any(memory.get("status") == "testing" for memory in memories if memory.get("memory_key") in {item["memory_key"] for item in used_memory}):
        status = "TESTMODUS"
    elif coverage["quality"] == "Schwach" or any("vorsichtiger" in line.lower() for line in why):
        status = "VORSICHT"
    if event.get("major_flag"):
        status = "EINGRIFF"
    return {
        "status": status,
        "planned": event["raw"]["session_payload"].get("planned_day", {}).get("label") or "Keine Vorgabe",
        "today": event["impact_text"],
        "intervening": bool(event.get("override_flag")),
        "why_short": why[:2],
        "data_quality": {
            "label": coverage["quality"],
            "note": coverage["note"],
        },
        "detail": {
            "title": "Heute",
            "plan": event["raw"]["session_payload"].get("planned_day", {}).get("label") or "Keine Vorgabe",
            "decision": event["impact_text"],
            "why": why[:3],
            "tomorrow_check": event.get("exit_criterion") or "Morgen kurz ehrlich gegenprüfen.",
            "used_memory": used_memory[:3],
        },
    }


def get_core_control_room_payload(days: int = 30) -> dict[str, Any]:
    memories = _memory_catalog()
    decisions = _decision_rows(days=days)
    impact_counter = Counter()
    impacting_keys = []
    for decision in decisions:
        for memory in decision.get("used_memory") or []:
            key = str(memory.get("memory_key") or "")
            if not key:
                continue
            impact_counter[key] += 1
            impacting_keys.append(key)
        for principle in decision.get("used_principles") or []:
            key = f"principle:{str(principle.get('topic') or '').strip()}"
            if key == "principle:":
                continue
            impact_counter[key] += 1
            impacting_keys.append(key)
    for memory in memories:
        memory["impact_count"] = int(impact_counter.get(memory["memory_key"], 0))
        memory["is_impacting"] = memory["memory_key"] in impacting_keys
    today = _today_payload(memories)
    health = _health_payload(memories, decisions)
    return {
        "ok": True,
        "generated_at": _utc_now(),
        "today": today,
        "decisions": decisions,
        "memories": memories,
        "health": health,
        "principles": get_core_principle_scores(),
    }
