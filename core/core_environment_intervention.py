from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from database.connections import get_core_db, get_training_db
from integrations.telegram_hub import send_domain_message
from smart_home.intervention_lights import pulse_light

try:
    from core.core_night_cycle import load_explain_payload
except Exception:  # pragma: no cover
    load_explain_payload = None

LOG = logging.getLogger(__name__)
LOG_PREFIX = "[core-env-intervention]"
BERLIN_TZ = ZoneInfo("Europe/Berlin")

ROOT_DIR = Path(__file__).resolve().parents[1]
PC_STATE_PATH = ROOT_DIR / "smart_home" / "data" / "pc_state.json"
REMOTE_STATE_PATH = ROOT_DIR / "smart_home" / "data" / "remote_devices_state.json"
REMOTE_PC_HOST = (os.getenv("LIVA_REMOTE_PC_HOST") or "192.0.2.2").strip() or "192.0.2.2"

ENV_THREAD: threading.Thread | None = None
ENV_THREAD_LOCK = threading.Lock()
ENV_STOP_EVENT = threading.Event()
ENV_STARTED_AT = time.time()


@dataclass
class DriftScores:
    late_context_score: float
    tomorrow_cost_score: float
    pc_active_score: float
    drift_confidence: float
    state: str
    target_level: int
    details: dict[str, Any]


FALLBACK_GOVEE_STATE = {
    "is_on": True,
    "brightness": 35,
    "color": {"r": 255, "g": 180, "b": 120},
}

LEVEL_PATTERNS: dict[int, dict[str, Any]] = {
    1: {
        "pulses": 2,
        "color": {"r": 30, "g": 60, "b": 200},
        "brightness": 80,
        "on_s": 0.28,
        "off_s": 0.22,
        "allow_tapo_fallback": False,
        "fallback_restore_state": FALLBACK_GOVEE_STATE,
        "govee_state_timeout": 3.0,
        "govee_state_retries": 6,
        "unknown_state_restore": "dim_blue",
    },
    2: {
        "pulses": 2,
        "color": {"r": 20, "g": 50, "b": 180},
        "brightness": 95,
        "on_s": 0.32,
        "off_s": 0.22,
        "allow_tapo_fallback": False,
        "fallback_restore_state": FALLBACK_GOVEE_STATE,
        "govee_state_timeout": 3.0,
        "govee_state_retries": 6,
        "unknown_state_restore": "dim_blue",
    },
    3: {
        "pulses": 3,
        "color": {"r": 15, "g": 35, "b": 160},
        "brightness": 100,
        "on_s": 0.26,
        "off_s": 0.18,
        "allow_tapo_fallback": False,
        "fallback_restore_state": FALLBACK_GOVEE_STATE,
        "govee_state_timeout": 3.0,
        "govee_state_retries": 6,
        "unknown_state_restore": "dim_blue",
    },
}

LEVEL_MESSAGES: dict[int, list[str]] = {
    1: [
        "CORE: Es ist spät und du hängst noch fest. Das war dein erster Hinweis.",
        "CORE: Du driftest gerade in unnötige Wachzeit. Schlaf wird jetzt wertvoll.",
    ],
    2: [
        "CORE: Noch am PC. Morgen zahlt das drauf. Fahr langsam runter.",
        "CORE: Später Abend, Fokus kippt. Das ist dein zweiter Hinweis.",
    ],
    3: [
        "CORE: Klarer Hinweis: Schlaf-Drift ist jetzt deutlich. Bitte runterfahren.",
        "CORE: Noch wach, Risiko hoch. Das ist der letzte Hinweis heute.",
    ],
}


def ensure_env_schema() -> None:
    conn = get_core_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS core_env_intervention_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            night_key TEXT,
            current_level INTEGER DEFAULT 0,
            last_state TEXT,
            last_trigger_at TEXT,
            cooldown_until TEXT,
            last_pc_active_at TEXT,
            total_interventions INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS core_env_intervention_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            night_key TEXT,
            state TEXT,
            level INTEGER,
            drift_confidence REAL,
            late_context_score REAL,
            tomorrow_cost_score REAL,
            pc_active_score REAL,
            reason_json TEXT,
            light_ok INTEGER,
            telegram_ok INTEGER,
            telegram_message TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _env_enabled() -> bool:
    raw = (os.getenv("CORE_ENV_INTERVENTIONS_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(BERLIN_TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BERLIN_TZ)
        return dt.astimezone(BERLIN_TZ)
    except Exception:
        return None


def _night_key(now: datetime) -> str:
    hour = now.hour
    if hour < 5:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _pc_state_snapshot() -> dict[str, Any]:
    payload = _read_json(PC_STATE_PATH) or {}
    return {
        "is_online": bool(payload.get("is_online")),
        "last_online_at": str(payload.get("last_online_at") or ""),
        "last_offline_at": str(payload.get("last_offline_at") or ""),
    }


def _is_host_reachable(host: str, *, timeout_s: int = 1) -> bool:
    host = str(host or "").strip()
    if not host:
        return False
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pc_is_confirmed_online(now: datetime, pc_state: dict[str, Any]) -> bool:
    if not bool(pc_state.get("is_online")):
        return False

    last_offline = _parse_iso(pc_state.get("last_offline_at"))
    if last_offline and (now - last_offline).total_seconds() <= 15 * 60:
        return False

    return _is_host_reachable(REMOTE_PC_HOST, timeout_s=1)


def _monitor_state_snapshot() -> dict[str, Any]:
    payload = _read_json(REMOTE_STATE_PATH) or {}
    data = {}
    for name in ("monitor_links", "monitor_rechts"):
        entry = payload.get(name) if isinstance(payload, dict) else None
        if not isinstance(entry, dict):
            continue
        data[name] = {
            "is_on": bool(entry.get("is_on")) if entry.get("is_on") is not None else None,
            "updated_at": str(entry.get("updated_at") or ""),
        }
    return data


def _school_schedule_tomorrow(now: datetime) -> dict[str, Any]:
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    conn = get_training_db()
    try:
        rows = conn.execute(
            """
            SELECT start_time, status_hint
            FROM school_schedule_entries
            WHERE date = ?
            """,
            (tomorrow,),
        ).fetchall()
    finally:
        conn.close()

    earliest: str | None = None
    for row in rows:
        if str(row[1] or "").strip().lower() in {"cancelled", "canceled"}:
            continue
        start = str(row[0] or "").strip()
        if not start:
            continue
        if earliest is None or start < earliest:
            earliest = start

    return {
        "has_school": bool(earliest),
        "earliest_start": earliest,
    }


def _autopilot_tomorrow(now: datetime) -> dict[str, Any]:
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    conn = get_training_db()
    try:
        row = conn.execute(
            "SELECT mode, created_ts FROM autopilot_day WHERE date = ?",
            (tomorrow,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"mode": None}
    return {"mode": str(row[0] or "").strip().upper()}


def _night_cycle_outlook() -> dict[str, Any]:
    if load_explain_payload is None:
        return {}
    payload = load_explain_payload()
    if not isinstance(payload, dict):
        return {}
    night = payload.get("night_cycle") if isinstance(payload.get("night_cycle"), dict) else {}
    outlook = night.get("tomorrow_outlook") if isinstance(night.get("tomorrow_outlook"), dict) else {}
    future = night.get("future_lab") if isinstance(night.get("future_lab"), dict) else {}
    morgen = future.get("morgen_status") if isinstance(future.get("morgen_status"), dict) else {}
    return {
        "expected_readiness_delta": outlook.get("expected_readiness_delta"),
        "expected_fatigue_risk": outlook.get("expected_fatigue_risk"),
        "readiness_tomorrow_pct": morgen.get("readiness_tomorrow_pct"),
        "fatigue_tomorrow_10": morgen.get("fatigue_tomorrow_10"),
    }


def _score_late_context(now: datetime) -> float:
    hour_val = now.hour + (now.minute / 60.0)
    if hour_val < 6.0:
        hour_val += 24.0
    late_minutes = max(0.0, (hour_val - 21.5) * 60.0)
    return min(1.0, late_minutes / 210.0)


def _score_tomorrow_cost(now: datetime, school: dict[str, Any], autopilot: dict[str, Any], outlook: dict[str, Any]) -> float:
    score = 0.0

    tomorrow = now.date() + timedelta(days=1)
    weekday = tomorrow.weekday()  # 0=Mon
    if weekday <= 4:
        score += 0.25
    elif weekday == 5:
        score += 0.12
    else:
        score += 0.06

    earliest = school.get("earliest_start")
    if earliest:
        try:
            hh, mm = earliest.split(":", 1)
            minutes = int(hh) * 60 + int(mm[:2])
            if minutes <= 8 * 60:
                score += 0.40
            elif minutes <= 9 * 60:
                score += 0.25
            else:
                score += 0.15
        except Exception:
            score += 0.15

    mode = str(autopilot.get("mode") or "").upper()
    if mode in {"HEAVY", "NORMAL"}:
        score += 0.22
    elif mode == "LIGHT":
        score += 0.12
    elif mode == "REST":
        score += 0.05

    readiness = outlook.get("readiness_tomorrow_pct")
    try:
        readiness_val = float(readiness)
        if readiness_val <= 45:
            score += 0.20
        elif readiness_val <= 55:
            score += 0.10
    except Exception:
        pass

    delta = outlook.get("expected_readiness_delta")
    try:
        delta_val = float(delta)
        if delta_val <= -0.2:
            score += 0.20
        elif delta_val <= -0.1:
            score += 0.10
    except Exception:
        pass

    return min(1.0, score)


def _score_pc_activity(now: datetime, pc_state: dict[str, Any], monitors: dict[str, Any]) -> float:
    if not _pc_is_confirmed_online(now, pc_state):
        return 0.0

    score = 0.0
    score += 0.8

    last_online = _parse_iso(pc_state.get("last_online_at"))
    if last_online and (now - last_online).total_seconds() <= 30 * 60:
        score += 0.05

    for entry in monitors.values():
        if entry.get("is_on") is True:
            updated = _parse_iso(entry.get("updated_at"))
            if updated and (now - updated).total_seconds() <= 30 * 60:
                score += 0.15
            break

    return min(1.0, score)


def _evaluate_sleep_drift(now: datetime) -> DriftScores:
    pc_state = _pc_state_snapshot()
    monitors = _monitor_state_snapshot()
    school = _school_schedule_tomorrow(now)
    autopilot = _autopilot_tomorrow(now)
    outlook = _night_cycle_outlook()
    pc_confirmed_online = _pc_is_confirmed_online(now, pc_state)

    late_context_score = _score_late_context(now)
    tomorrow_cost_score = _score_tomorrow_cost(now, school, autopilot, outlook)
    pc_active_score = _score_pc_activity(now, pc_state, monitors) if pc_confirmed_online else 0.0

    drift_conf = (late_context_score * 0.4) + (pc_active_score * 0.3) + (tomorrow_cost_score * 0.3)
    if pc_active_score < 0.45:
        drift_conf *= 0.6

    if late_context_score < 0.25 or pc_active_score < 0.4:
        state = "normal"
        target_level = 0
    elif drift_conf < 0.35:
        state = "normal"
        target_level = 0
    elif drift_conf < 0.55:
        state = "late_pc_watch"
        target_level = 1
    elif drift_conf < 0.72:
        state = "sleep_drift"
        target_level = 2
    else:
        state = "high_sleep_risk"
        target_level = 3

    details = {
        "pc_state": pc_state,
        "pc_confirmed_online": pc_confirmed_online,
        "monitors": monitors,
        "school": school,
        "autopilot": autopilot,
        "outlook": outlook,
    }

    return DriftScores(
        late_context_score=round(late_context_score, 3),
        tomorrow_cost_score=round(tomorrow_cost_score, 3),
        pc_active_score=round(pc_active_score, 3),
        drift_confidence=round(min(1.0, drift_conf), 3),
        state=state,
        target_level=target_level,
        details=details,
    )


def _load_state(conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM core_env_intervention_state WHERE id=1").fetchone()
    if not row:
        return {
            "id": 1,
            "night_key": None,
            "current_level": 0,
            "last_state": "normal",
            "last_trigger_at": None,
            "cooldown_until": None,
            "last_pc_active_at": None,
            "total_interventions": 0,
        }
    return dict(row)


def _save_state(conn, state: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO core_env_intervention_state (
            id, night_key, current_level, last_state, last_trigger_at, cooldown_until,
            last_pc_active_at, total_interventions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            night_key=excluded.night_key,
            current_level=excluded.current_level,
            last_state=excluded.last_state,
            last_trigger_at=excluded.last_trigger_at,
            cooldown_until=excluded.cooldown_until,
            last_pc_active_at=excluded.last_pc_active_at,
            total_interventions=excluded.total_interventions
        """,
        (
            1,
            state.get("night_key"),
            int(state.get("current_level") or 0),
            state.get("last_state"),
            state.get("last_trigger_at"),
            state.get("cooldown_until"),
            state.get("last_pc_active_at"),
            int(state.get("total_interventions") or 0),
        ),
    )


def _cooldown_for_level(level: int) -> int:
    if level == 1:
        return 25 * 60
    if level == 2:
        return 35 * 60
    return 45 * 60


def _escalation_gap_for_level(level: int) -> int:
    if level == 1:
        return 18 * 60
    if level == 2:
        return 26 * 60
    return 35 * 60


def _same_level_gap_s() -> int:
    return 90 * 60


def _select_message(level: int, drift_state: str) -> str:
    base = LEVEL_MESSAGES.get(level) or ["CORE: Hinweis."]
    idx = 0 if drift_state in {"late_pc_watch", "sleep_drift"} else 1
    return base[min(idx, len(base) - 1)]


def _should_trigger(
    now: datetime,
    scores: DriftScores,
    state: dict[str, Any],
) -> tuple[bool, int, str]:
    if not bool(scores.details.get("pc_confirmed_online")):
        return False, 0, "pc_not_confirmed_online"

    if scores.target_level <= 0:
        return False, 0, "state_normal"

    if scores.late_context_score < 0.3 or scores.pc_active_score < 0.5:
        return False, 0, "weak_context"

    last_trigger = _parse_iso(state.get("last_trigger_at"))
    cooldown_until = _parse_iso(state.get("cooldown_until"))
    current_level = int(state.get("current_level") or 0)
    total_interventions = int(state.get("total_interventions") or 0)

    if total_interventions >= 3:
        return False, 0, "nightly_cap"

    if cooldown_until and now < cooldown_until:
        return False, 0, "cooldown"

    if last_trigger:
        elapsed = (now - last_trigger).total_seconds()
        if scores.target_level <= current_level and elapsed < _same_level_gap_s():
            return False, 0, "same_level_gap"
        if scores.target_level > current_level:
            if elapsed < _escalation_gap_for_level(current_level or 1):
                return False, 0, "escalation_gap"

    return True, scores.target_level, "ok"


def _maybe_reset_state(now: datetime, state: dict[str, Any]) -> None:
    night_key = _night_key(now)
    if state.get("night_key") != night_key:
        state.update(
            {
                "night_key": night_key,
                "current_level": 0,
                "last_state": "normal",
                "last_trigger_at": None,
                "cooldown_until": None,
                "last_pc_active_at": None,
                "total_interventions": 0,
            }
        )


def _update_pc_presence(now: datetime, scores: DriftScores, state: dict[str, Any]) -> None:
    if scores.pc_active_score >= 0.5:
        state["last_pc_active_at"] = _iso(now)
        return

    last_pc = _parse_iso(state.get("last_pc_active_at"))
    if last_pc and (now - last_pc).total_seconds() >= 20 * 60:
        state["current_level"] = 0
        state["last_state"] = "normal"


def _record_intervention(
    *,
    conn,
    now: datetime,
    scores: DriftScores,
    level: int,
    reason: str,
    light_ok: bool,
    telegram_ok: bool,
    telegram_message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO core_env_intervention_log (
            created_at, night_key, state, level, drift_confidence,
            late_context_score, tomorrow_cost_score, pc_active_score,
            reason_json, light_ok, telegram_ok, telegram_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _iso(now),
            _night_key(now),
            scores.state,
            int(level),
            float(scores.drift_confidence),
            float(scores.late_context_score),
            float(scores.tomorrow_cost_score),
            float(scores.pc_active_score),
            json.dumps({"reason": reason, "details": scores.details}, ensure_ascii=False),
            1 if light_ok else 0,
            1 if telegram_ok else 0,
            telegram_message,
        ),
    )


def _run_cycle() -> None:
    if time.time() - ENV_STARTED_AT < 5 * 60:
        return

    now = _now()
    scores = _evaluate_sleep_drift(now)

    conn = get_core_db()
    try:
        state = _load_state(conn)
        _maybe_reset_state(now, state)
        _update_pc_presence(now, scores, state)

        should_trigger, level, reason = _should_trigger(now, scores, state)
        state["last_state"] = scores.state

        if not should_trigger:
            _save_state(conn, state)
            conn.commit()
            return

        pattern = LEVEL_PATTERNS.get(level) or LEVEL_PATTERNS[1]
        light_result = pulse_light(**pattern)
        light_ok = bool(light_result.get("ok"))

        message = _select_message(level, scores.state)
        telegram_result = send_domain_message("system", message)
        telegram_ok = bool(telegram_result.get("ok"))

        now_iso = _iso(now)
        state["current_level"] = max(int(state.get("current_level") or 0), level)
        state["last_trigger_at"] = now_iso
        state["cooldown_until"] = _iso(now + timedelta(seconds=_cooldown_for_level(level)))
        state["total_interventions"] = int(state.get("total_interventions") or 0) + 1

        _record_intervention(
            conn=conn,
            now=now,
            scores=scores,
            level=level,
            reason=reason,
            light_ok=light_ok,
            telegram_ok=telegram_ok,
            telegram_message=message,
        )
        _save_state(conn, state)
        conn.commit()
    finally:
        conn.close()


def _loop() -> None:
    while not ENV_STOP_EVENT.is_set():
        try:
            if _env_enabled():
                _run_cycle()
        except Exception as exc:
            LOG.warning("%s run failed: %s", LOG_PREFIX, exc)
        ENV_STOP_EVENT.wait(timeout=90.0)


def ensure_env_thread() -> None:
    if not _env_enabled():
        return
    with ENV_THREAD_LOCK:
        global ENV_THREAD
        if ENV_THREAD and ENV_THREAD.is_alive():
            return
        ENV_STOP_EVENT.clear()
        ENV_THREAD = threading.Thread(target=_loop, name="core-env-intervention", daemon=True)
        ENV_THREAD.start()
        LOG.info("%s thread started", LOG_PREFIX)


def stop_env_thread(timeout: float = 2.0) -> None:
    ENV_STOP_EVENT.set()
    thread = ENV_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=max(0.2, float(timeout)))
