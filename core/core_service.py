from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from flask import Request, Response, g

from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_plans_db, get_runs_db, get_training_db

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

SSE_LOCK = threading.Lock()
SSE_SUBS: set[queue.Queue] = set()
CORE_EVENT_BUFFER: list[tuple[int, str, str, str, str, int | None, int | None, str]] = []
CORE_EVENT_BUFFER_LOCK = threading.Lock()
CORE_SCHEMA_READY = False
CORE_SCHEMA_LOCK = threading.Lock()

OBS_LOCK = threading.Lock()
OBS_EVENTS: deque[dict[str, Any]] = deque(maxlen=2400)
OBS_LOGS: deque[dict[str, Any]] = deque(maxlen=200)
OBS_METRIC_BUCKETS: deque[dict[str, Any]] = deque(maxlen=120)
OBS_SUBS: set[queue.Queue] = set()
OBS_DROPPED_EVENTS = 0
OBS_SYS_STATE: dict[str, Any] = {}
OBS_PROC_STATE: list[dict[str, Any]] = []
OBS_THREAD: threading.Thread | None = None
OBS_THREAD_STARTED = False
OBS_ACTIVE_UNTIL = 0.0
OBS_EMIT_SEC = 0
OBS_EMIT_COUNT = 0

SYSTEM_NODES = [
    {"id": "API", "x": 0.50, "y": 0.46},
    {"id": "DB_TRAINING", "x": 0.30, "y": 0.24},
    {"id": "DB_HRV", "x": 0.70, "y": 0.24},
    {"id": "DB_NUTRITION", "x": 0.82, "y": 0.42},
    {"id": "AUTOPILOT", "x": 0.68, "y": 0.68},
    {"id": "AI", "x": 0.36, "y": 0.72},
    {"id": "INGEST", "x": 0.18, "y": 0.56},
    {"id": "CRON", "x": 0.22, "y": 0.34},
]

CORE_MODULE_LAYOUT = [
    {"id": "recovery", "label": "RECOVERY", "ring": "inner", "angle": 292},
    {"id": "training", "label": "TRAINING", "ring": "inner", "angle": 340},
    {"id": "plan", "label": "PLAN", "ring": "inner", "angle": 28},
    {"id": "load", "label": "LOAD", "ring": "inner", "angle": 72},
    {"id": "nutrition", "label": "NUTRITION", "ring": "outer", "angle": 124},
    {"id": "sleep", "label": "SLEEP", "ring": "outer", "angle": 172},
    {"id": "runs", "label": "RUNS", "ring": "outer", "angle": 214},
    {"id": "bodyweight", "label": "BODYWEIGHT", "ring": "outer", "angle": 254},
    {"id": "stress", "label": "STRESS", "ring": "outer", "angle": 18},
]


@dataclass
class IntelGateDecision:
    emit: bool
    direction: str


def _to_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _now_ts() -> int:
    return int(time.time())


def _today_iso() -> str:
    return date.today().isoformat()


def _ip_salt() -> str:
    return (os.getenv("LIVA_CORE_IP_SALT") or os.getenv("LIVA_FLASK_SECRET") or "core-ip-salt").strip()


def _core_log_events_enabled() -> bool:
    return _to_bool(os.getenv("CORE_LOG_EVENTS"), False)




def _core_observatory_rate_cap() -> int:
    try:
        return max(5, int((os.getenv("CORE_OBS_EVENT_RATE_CAP") or "12").strip()))
    except Exception:
        return 12


def _core_observatory_active_ttl_s() -> int:
    try:
        return max(20, int((os.getenv("CORE_OBS_ACTIVE_TTL_S") or "120").strip()))
    except Exception:
        return 120


def _hash_ip(raw: str) -> str:
    return hashlib.sha256(f"{_ip_salt()}|{(raw or '').strip()}".encode("utf-8")).hexdigest()


def build_device_fingerprint(*, ip_hash: str, user_agent: str, accept_language: str, timezone_hint: str) -> str:
    payload = "|".join([user_agent.strip().lower(), ip_hash.strip().lower(), accept_language.strip().lower(), timezone_hint.strip().lower()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _core_secret() -> str:
    return (os.getenv("LIVA_CORE_SECRET") or "").strip()


def core_enabled() -> bool:
    return _to_bool(os.getenv("CORE_ENABLED") or os.getenv("LIVA_CORE_ENABLED"), False)


def extract_core_key_from_request(req: Request) -> str:
    token = (req.args.get("core_key") or "").strip()
    if token:
        return token
    token = (req.headers.get("X-Core-Key") or "").strip()
    if token:
        return token
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def is_core_access_allowed(req: Request, role: str | None = None) -> bool:
    role = (role or "").strip().lower()
    if role in {"key", "master", "trusted"}:
        return True
    if core_enabled():
        return True
    secret = _core_secret()
    token = extract_core_key_from_request(req)
    return bool(secret and token and token == secret)


def ensure_core_schema() -> None:
    conn = get_core_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_session (
          id TEXT PRIMARY KEY,
          device_id TEXT,
          start_ts INTEGER,
          last_seen_ts INTEGER,
          is_active INTEGER,
          current_route TEXT,
          req_count INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_event (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER,
          session_id TEXT,
          kind TEXT,
          route TEXT,
          subsystem TEXT,
          latency_ms INTEGER,
          status_code INTEGER,
          meta_json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_intel_state (
          fingerprint_hash TEXT PRIMARY KEY,
          last_shown_day TEXT,
          last_signal_value REAL,
          last_direction TEXT,
          cooldown_days INTEGER DEFAULT 2
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_core_environment_interventions() -> None:
    from core.core_environment_intervention import ensure_env_schema, ensure_env_thread

    ensure_env_schema()
    ensure_env_thread()


def _ensure_core_schema_once() -> None:
    global CORE_SCHEMA_READY
    if CORE_SCHEMA_READY:
        return
    with CORE_SCHEMA_LOCK:
        if CORE_SCHEMA_READY:
            return
        ensure_core_schema()
        CORE_SCHEMA_READY = True


def _flush_core_event_buffer_once() -> None:
    with CORE_EVENT_BUFFER_LOCK:
        if not CORE_EVENT_BUFFER:
            return
        batch = CORE_EVENT_BUFFER[:800]
        del CORE_EVENT_BUFFER[: len(batch)]
    conn = get_core_db()
    conn.executemany(
        """
        INSERT INTO core_event (ts, session_id, kind, route, subsystem, latency_ms, status_code, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
    conn.commit()
    conn.close()


def _window_start(window: str) -> int:
    now = _now_ts()
    w = (window or "today").strip().lower()
    if w == "live":
        return now - 20 * 60
    if w == "7d":
        return now - 7 * 24 * 60 * 60
    return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _to_ts(raw: str | None) -> int | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            return int(datetime.strptime(s, "%Y-%m-%d").timestamp())
        s2 = s.replace("Z", "+00:00")
        if " " in s2 and "+" in s2 and "T" not in s2:
            left, right = s2.rsplit(" ", 1)
            if ":" in right:
                s2 = f"{left}T{right}"
        return int(datetime.fromisoformat(s2).timestamp())
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except Exception:
            continue
    return None


def _state_from_metrics(severity: float, confidence: float) -> str:
    if confidence < 0.45:
        return "uncertain"
    if severity >= 80:
        return "critical"
    if severity >= 55:
        return "warning"
    if severity >= 30:
        return "active"
    return "ok"


def _clamp_severity(v: float) -> int:
    try:
        return max(0, min(100, int(round(v))))
    except Exception:
        return 0


def _module_payload(
    *,
    module_id: str,
    summary_line: str,
    severity: float,
    confidence: float,
    headline: str,
    drivers: list[str],
    chart_points: list[float],
    chart_labels: list[str],
    action: dict[str, Any] | None,
    updated_ts: int | None = None,
) -> dict[str, Any]:
    meta = next((m for m in CORE_MODULE_LAYOUT if m["id"] == module_id), None)
    if meta is None:
        meta = {"id": module_id, "label": module_id.upper(), "ring": "outer", "angle": 0}
    sev = _clamp_severity(severity)
    conf = max(0.0, min(1.0, float(confidence)))
    return {
        "id": meta["id"],
        "labelShort": meta["label"],
        "ring": meta["ring"],
        "angle": meta["angle"],
        "state": _state_from_metrics(sev, conf),
        "severity": sev,
        "confidence": conf,
        "summaryLine": (summary_line or "no signal").strip()[:120],
        "lastUpdatedTs": int(updated_ts or 0),
        "detail": {
            "headline": (headline or "No strong signal available.").strip()[:140],
            "drivers": [str(d).strip()[:90] for d in (drivers or []) if str(d).strip()][:3],
            "action": action or None,
            "chart": {"points": chart_points[:14], "labels": chart_labels[:14], "kind": "spark"},
        },
    }


def _source_row(name: str, last_seen_ts: int | None, stale_after_s: int) -> dict[str, Any]:
    now = _now_ts()
    stale = True
    age_s = None
    if last_seen_ts:
        age_s = max(0, now - int(last_seen_ts))
        stale = age_s > stale_after_s
    return {"name": name, "lastSeenTs": last_seen_ts, "ageS": age_s, "stale": stale}


def _subsystem_for_route(route: str) -> str:
    p = (route or "").lower()
    if p.startswith("/api/hrv"):
        return "DB_HRV"
    if p.startswith("/api/makros") or p.startswith("/api/nutrition"):
        return "DB_NUTRITION"
    if p.startswith("/api/training") or p.startswith("/api/workout") or p.startswith("/api/analysis"):
        return "DB_TRAINING"
    if p.startswith("/api/autopilot"):
        return "AUTOPILOT"
    if p.startswith("/api/ai") or p.startswith("/jit/ai"):
        return "AI"
    if p.startswith("/api/runs"):
        return "INGEST"
    return "API"


def capture_http_event(req: Request, resp: Response, latency_ms: int) -> Response:
    try:
        route = (req.path or "/")[:220]
        if route.startswith("/static/") or route.startswith("/favicon"):
            return resp
        obs_active = _observatory_is_active()
        core_logging = _core_log_events_enabled()
        core_route = route == "/core" or route.startswith("/api/core")
        if not (obs_active or core_logging or core_route):
            return resp

        _ensure_core_schema_once()
        now = _now_ts()
        sid = (req.cookies.get("th_core_sid") or "").strip()[:64] or uuid.uuid4().hex
        setattr(g, "core_session_id", sid)
        resp.set_cookie(
            "th_core_sid",
            sid,
            max_age=60 * 60 * 24 * 90,
            httponly=True,
            samesite="Lax",
            secure=_to_bool(os.getenv("LIVA_SESSION_COOKIE_SECURE") or "1", True),
        )
        subsystem = _subsystem_for_route(route)
        status = int(resp.status_code or 0)
        meta = {"method": req.method, "remote_addr": "hashed", "content_length": int(resp.calculate_content_length() or 0)}
        if core_logging:
            ip_source = getattr(g, "client_ip", "") or (req.remote_addr or "")
            ip_hash = _hash_ip(ip_source)
            ua = (req.headers.get("User-Agent") or "").strip()
            _ = build_device_fingerprint(
                ip_hash=ip_hash,
                user_agent=ua,
                accept_language=(req.headers.get("Accept-Language") or "").strip(),
                timezone_hint=(req.headers.get("X-Timezone") or "").strip(),
            )
            with CORE_EVENT_BUFFER_LOCK:
                CORE_EVENT_BUFFER.append(
                    (now, sid, "http", route, subsystem, max(0, int(latency_ms)), status, json.dumps(meta, separators=(",", ":")))
                )
                if len(CORE_EVENT_BUFFER) > 4000:
                    del CORE_EVENT_BUFFER[: len(CORE_EVENT_BUFFER) - 4000]

        req_id = (req.headers.get("X-Request-ID") or req.headers.get("X-Trace-ID") or "").strip()[:12] or uuid.uuid4().hex[:8]
        if obs_active:
            _emit_observatory_flow(
                route=route,
                method=req.method,
                status=status,
                latency_ms=max(0, int(latency_ms)),
                subsystem=subsystem,
                request_id=req_id,
            )
            if status >= 500:
                _emit_observatory_log("ERR", f"HTTP {status} auf {route}", request_id=req_id)
            elif latency_ms >= 1200:
                _emit_observatory_log("WARN", f"Langsame Anfrage {route} ({latency_ms} ms)", request_id=req_id)
        return resp
    except Exception:
        return resp


def record_heartbeat(session_id: str, route: str) -> dict[str, Any]:
    ensure_core_schema()
    now = _now_ts()
    sid = (session_id or "").strip()[:64] or uuid.uuid4().hex
    route = (route or "/core")[:220]
    conn = get_core_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM core_session WHERE id=?", (sid,)).fetchone()
    if row is None:
        cur.execute(
            """
            INSERT INTO core_session (id, device_id, start_ts, last_seen_ts, is_active, current_route, req_count)
            VALUES (?, NULL, ?, ?, 1, ?, 0)
            """,
            (sid, now, now, route),
        )
    else:
        cur.execute(
            "UPDATE core_session SET last_seen_ts=?, is_active=1, current_route=? WHERE id=?",
            (now, route, sid),
        )
    cur.execute("UPDATE core_session SET is_active=0 WHERE last_seen_ts < ?", (now - 35,))
    conn.commit()
    conn.close()
    return {"type": "heartbeat", "session_id": sid, "route": route, "ts": now}


def _fetch_training_data(days: int) -> dict[str, Any]:
    out = {"last_ts": None, "sessions": 0, "tonnage": 0.0, "series": []}
    start_iso = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    last = cur.execute("SELECT date_iso FROM workouts WHERE date_iso IS NOT NULL ORDER BY date_iso DESC, id DESC LIMIT 1").fetchone()
    rows = cur.execute(
        """
        SELECT w.date_iso AS date_iso,
               COUNT(DISTINCT w.id) AS sessions,
               SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN s.weight*s.reps ELSE 0 END) AS tonnage
        FROM workouts w
        LEFT JOIN exercises e ON e.workout_id = w.id
        LEFT JOIN sets s ON s.exercise_id = e.id
        WHERE w.date_iso >= ?
        GROUP BY w.date_iso
        ORDER BY w.date_iso ASC
        """,
        (start_iso,),
    ).fetchall()
    conn.close()
    if last:
        out["last_ts"] = _to_ts(last["date_iso"])
    for row in rows:
        ton = _safe_float(row["tonnage"]) or 0.0
        ses = int(row["sessions"] or 0)
        out["sessions"] += ses
        out["tonnage"] += ton
        out["series"].append({"date": row["date_iso"], "sessions": ses, "tonnage": ton})
    return out


def _fetch_runs_data(days: int) -> dict[str, Any]:
    out = {"last_ts": None, "runs": 0, "km": 0.0, "series": []}
    start_iso = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    last = cur.execute("SELECT date FROM runs ORDER BY date DESC LIMIT 1").fetchone()
    rows = cur.execute(
        """
        SELECT substr(date, 1, 10) AS date_iso, COUNT(*) AS run_count, SUM(distance) AS dist_m
        FROM runs
        WHERE substr(date, 1, 10) >= ?
        GROUP BY substr(date, 1, 10)
        ORDER BY date_iso ASC
        """,
        (start_iso,),
    ).fetchall()
    conn.close()
    if last:
        out["last_ts"] = _to_ts(last["date"])
    for row in rows:
        c = int(row["run_count"] or 0)
        km = (_safe_float(row["dist_m"]) or 0.0) / 1000.0
        out["runs"] += c
        out["km"] += km
        out["series"].append({"date": row["date_iso"], "runs": c, "km": km})
    return out


def _fetch_hrv_data(days: int) -> dict[str, Any]:
    out = {"last_ts": None, "rmssd_latest": None, "hr_latest": None, "sleep_latest": None, "series": []}
    start_iso = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    last = cur.execute(
        """
        SELECT ts_measurement, date_utc, rmssd, hr, sleep_quality
        FROM hrv_measurements
        WHERE rmssd IS NOT NULL OR hr IS NOT NULL
        ORDER BY ts_measurement DESC, date_utc DESC
        LIMIT 1
        """
    ).fetchone()
    rows = cur.execute(
        """
        SELECT COALESCE(substr(date_utc,1,10), substr(ts_measurement,1,10)) AS date_iso,
               AVG(rmssd) AS rmssd_avg,
               AVG(hr) AS hr_avg,
               AVG(sleep_quality) AS sleep_avg
        FROM hrv_measurements
        WHERE COALESCE(substr(date_utc,1,10), substr(ts_measurement,1,10)) >= ?
        GROUP BY COALESCE(substr(date_utc,1,10), substr(ts_measurement,1,10))
        ORDER BY date_iso ASC
        """,
        (start_iso,),
    ).fetchall()
    conn.close()
    if last:
        out["last_ts"] = _to_ts(last["ts_measurement"] or last["date_utc"])
        out["rmssd_latest"] = _safe_float(last["rmssd"])
        out["hr_latest"] = _safe_float(last["hr"])
        out["sleep_latest"] = _safe_float(last["sleep_quality"])
    for row in rows:
        out["series"].append(
            {
                "date": row["date_iso"],
                "rmssd": _safe_float(row["rmssd_avg"]),
                "hr": _safe_float(row["hr_avg"]),
                "sleep": _safe_float(row["sleep_avg"]),
            }
        )
    return out


def _fetch_nutrition_data(days: int) -> dict[str, Any]:
    out = {"last_ts": None, "series": [], "kcal_mean": None, "protein_mean": None, "weight_latest": None}
    start_iso = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    last = cur.execute(
        "SELECT date_iso FROM nutrition_daily WHERE (kcal IS NOT NULL OR protein IS NOT NULL OR weight_kg IS NOT NULL) ORDER BY date_iso DESC LIMIT 1"
    ).fetchone()
    rows = cur.execute(
        """
        SELECT date_iso, kcal, protein, weight_kg
        FROM nutrition_daily
        WHERE date_iso >= ?
        ORDER BY date_iso ASC
        """,
        (start_iso,),
    ).fetchall()
    conn.close()
    if last:
        out["last_ts"] = _to_ts(last["date_iso"])
    kcal_vals: list[float] = []
    prot_vals: list[float] = []
    for row in rows:
        kcal = _safe_float(row["kcal"])
        prot = _safe_float(row["protein"])
        bw = _safe_float(row["weight_kg"])
        if kcal is not None:
            kcal_vals.append(kcal)
        if prot is not None:
            prot_vals.append(prot)
        if bw is not None:
            out["weight_latest"] = bw
        out["series"].append({"date": row["date_iso"], "kcal": kcal, "protein": prot, "weight": bw})
    out["kcal_mean"] = _mean(kcal_vals[-7:])
    out["protein_mean"] = _mean(prot_vals[-7:])
    return out


def _fetch_plan_data() -> dict[str, Any]:
    out = {"last_ts": None, "active_name": None}
    conn = get_plans_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT title, updated_at
        FROM gym_plans
        WHERE is_active = 1 AND is_archived = 0
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    if row:
        out["active_name"] = (row["title"] or "").strip() or "active plan"
        out["last_ts"] = _to_ts(row["updated_at"])
    return out


def _build_core_modules(mode: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    days = 14 if mode == "live" else (14 if mode == "today" else 28)
    training_data = _fetch_training_data(days)
    runs_data = _fetch_runs_data(days)
    hrv_data = _fetch_hrv_data(days)
    nutrition_data = _fetch_nutrition_data(days)
    plan_data = _fetch_plan_data()
    now = _now_ts()

    hrv_rmssd_series = [x["rmssd"] for x in hrv_data["series"] if x.get("rmssd") is not None]
    hrv_recent = hrv_rmssd_series[-3:]
    hrv_prev = hrv_rmssd_series[-10:-3]
    rmssd_recent = _mean(hrv_recent)
    rmssd_prev = _mean(hrv_prev)
    rec_delta = ((rmssd_recent - rmssd_prev) / rmssd_prev) * 100.0 if rmssd_recent is not None and rmssd_prev not in (None, 0) else 0.0
    recovery_sev = 60.0 if rec_delta <= -10 else 20.0 if rec_delta >= 5 else 38.0
    recovery_conf = min(1.0, len(hrv_rmssd_series) / 10.0)
    recovery = _module_payload(
        module_id="recovery",
        summary_line=f"rmssd {rec_delta:+.0f}% vs prev",
        severity=recovery_sev,
        confidence=recovery_conf,
        headline=f"Readiness trend {('down' if rec_delta < 0 else 'up')} vs baseline window.",
        drivers=[
            f"rmssd recent {_safe_float(rmssd_recent) or 0:.1f}",
            f"rmssd prev {_safe_float(rmssd_prev) or 0:.1f}",
            f"hr latest {_safe_float(hrv_data.get('hr_latest')) or 0:.1f}",
        ],
        chart_points=[float(v) for v in hrv_rmssd_series[-10:]],
        chart_labels=[str(x.get("date") or "")[5:] for x in hrv_data["series"][-10:]],
        action={"label": "Open HRV", "href": "/hrv", "kind": "link"},
        updated_ts=hrv_data.get("last_ts"),
    )

    sessions = int(training_data["sessions"] or 0)
    tonnage = float(training_data["tonnage"] or 0.0)
    training_conf = min(1.0, sessions / 6.0)
    training_sev = 62.0 if sessions <= 1 else 35.0 if sessions <= 3 else 18.0
    training_mod = _module_payload(
        module_id="training",
        summary_line=f"{sessions} sessions • tonnage {tonnage:.0f}",
        severity=training_sev,
        confidence=training_conf,
        headline="Session output in current window.",
        drivers=[
            f"sessions {sessions}",
            f"tonnage {tonnage:.0f}",
            f"last update {datetime.fromtimestamp(training_data.get('last_ts') or now).date().isoformat() if training_data.get('last_ts') else 'n/a'}",
        ],
        chart_points=[float(x.get("tonnage") or 0.0) for x in (training_data["series"][-10:] if training_data["series"] else [])],
        chart_labels=[str(x.get("date") or "")[5:] for x in (training_data["series"][-10:] if training_data["series"] else [])],
        action={"label": "Open Training Session", "href": "/training", "kind": "link"},
        updated_ts=training_data.get("last_ts"),
    )

    days_since_training = (now - int(training_data.get("last_ts") or now)) / 86400.0 if training_data.get("last_ts") else 9.0
    plan_conf = 0.8 if plan_data.get("active_name") else 0.35
    plan_sev = 72.0 if days_since_training > 3 else 34.0
    plan_mod = _module_payload(
        module_id="plan",
        summary_line=f"{plan_data.get('active_name') or 'source missing'}",
        severity=plan_sev,
        confidence=plan_conf,
        headline="Plan freshness and execution continuity.",
        drivers=[
            f"active {plan_data.get('active_name') or 'none'}",
            f"days since training {days_since_training:.1f}",
            "skip risk elevated" if days_since_training > 3 else "execution cadence normal",
        ],
        chart_points=[days_since_training],
        chart_labels=["now"],
        action={"label": "Open Planning", "href": "/planung", "kind": "link"},
        updated_ts=plan_data.get("last_ts"),
    )

    load_points: list[float] = []
    load_labels: list[str] = []
    by_date: dict[str, float] = {}
    for row in training_data["series"]:
        by_date[row["date"]] = by_date.get(row["date"], 0.0) + float(row.get("tonnage") or 0.0) / 100.0
    for row in runs_data["series"]:
        by_date[row["date"]] = by_date.get(row["date"], 0.0) + float(row.get("km") or 0.0) * 12.0
    for day_iso in sorted(by_date.keys()):
        load_points.append(float(by_date[day_iso]))
        load_labels.append(day_iso[5:])
    recent_load = _mean(load_points[-7:]) or 0.0
    prev_load = _mean(load_points[-14:-7]) or 0.0
    load_delta = ((recent_load - prev_load) / prev_load) * 100.0 if prev_load > 0 else 0.0
    load_conf = min(1.0, len(load_points) / 10.0)
    load_sev = 74.0 if load_delta > 45 else 58.0 if load_delta > 25 else 30.0 if load_delta > 10 else 20.0
    load_mod = _module_payload(
        module_id="load",
        summary_line=f"7d load {load_delta:+.0f}%",
        severity=load_sev,
        confidence=load_conf,
        headline="Combined gym + run load trend.",
        drivers=[f"recent {recent_load:.1f}", f"prev {prev_load:.1f}", f"delta {load_delta:+.1f}%"],
        chart_points=load_points[-10:],
        chart_labels=load_labels[-10:],
        action={"label": "Open Planning", "href": "/planung", "kind": "link"},
        updated_ts=max(training_data.get("last_ts") or 0, runs_data.get("last_ts") or 0) or None,
    )

    kcal_vals = [x["kcal"] for x in nutrition_data["series"] if x.get("kcal") is not None]
    protein_vals = [x["protein"] for x in nutrition_data["series"] if x.get("protein") is not None]
    kcal_recent = _mean(kcal_vals[-3:]) or 0.0
    kcal_prev = _mean(kcal_vals[-10:-3]) or 0.0
    protein_recent = _mean(protein_vals[-3:]) or 0.0
    nutrition_conf = min(1.0, (len(kcal_vals) + len(protein_vals)) / 14.0)
    nutrition_sev = 66.0 if protein_recent < 120 else 45.0 if kcal_prev and abs(kcal_recent - kcal_prev) / max(1.0, kcal_prev) > 0.28 else 22.0
    nutrition_mod = _module_payload(
        module_id="nutrition",
        summary_line=f"protein {protein_recent:.0f}g • kcal {kcal_recent:.0f}",
        severity=nutrition_sev,
        confidence=nutrition_conf,
        headline="Intake consistency over the recent window.",
        drivers=[f"protein 3d {protein_recent:.0f}g", f"kcal 3d {kcal_recent:.0f}", f"kcal prev {kcal_prev:.0f}"],
        chart_points=[float(v) for v in kcal_vals[-10:]],
        chart_labels=[str(x.get("date") or "")[5:] for x in nutrition_data["series"][-10:]],
        action={"label": "Open Mealplan", "href": "/ernaehrung/plan", "kind": "link"},
        updated_ts=nutrition_data.get("last_ts"),
    )

    sleep_vals = [x["sleep"] for x in hrv_data["series"] if x.get("sleep") is not None]
    sleep_recent = _mean(sleep_vals[-3:]) or 0.0
    sleep_conf = min(1.0, len(sleep_vals) / 8.0)
    sleep_sev = 68.0 if sleep_recent and sleep_recent < 5.0 else 40.0 if sleep_recent and sleep_recent < 6.5 else 18.0
    sleep_mod = _module_payload(
        module_id="sleep",
        summary_line=f"sleep quality {sleep_recent:.1f}",
        severity=sleep_sev,
        confidence=sleep_conf,
        headline="Sleep consistency inferred from HRV entries.",
        drivers=[f"recent avg {sleep_recent:.1f}", f"samples {len(sleep_vals)}", f"source {'hrv' if sleep_vals else 'missing'}"],
        chart_points=[float(v) for v in sleep_vals[-10:]],
        chart_labels=[str(x.get("date") or "")[5:] for x in hrv_data["series"][-10:]],
        action={"label": "Open HRV", "href": "/hrv", "kind": "link"},
        updated_ts=hrv_data.get("last_ts"),
    )

    run_points = [float(x.get("km") or 0.0) for x in runs_data["series"]]
    run_recent = _mean(run_points[-7:]) or 0.0
    runs_conf = min(1.0, len(run_points) / 8.0)
    runs_sev = 55.0 if int(runs_data["runs"] or 0) == 0 else 33.0 if run_recent < 2.5 else 20.0
    runs_mod = _module_payload(
        module_id="runs",
        summary_line=f"{int(runs_data['runs'] or 0)} runs • {float(runs_data['km'] or 0.0):.1f}km",
        severity=runs_sev,
        confidence=runs_conf,
        headline="Run frequency and volume in selected window.",
        drivers=[f"runs {int(runs_data['runs'] or 0)}", f"total {float(runs_data['km'] or 0.0):.1f}km", f"7d avg {run_recent:.1f}km"],
        chart_points=run_points[-10:],
        chart_labels=[str(x.get("date") or "")[5:] for x in runs_data["series"][-10:]],
        action={"label": "Open Runs", "href": "/runs", "kind": "link"},
        updated_ts=runs_data.get("last_ts"),
    )

    bw_vals = [x["weight"] for x in nutrition_data["series"] if x.get("weight") is not None]
    bw_recent = _mean(bw_vals[-3:]) or 0.0
    bw_prev = _mean(bw_vals[-10:-3]) or 0.0
    bw_delta = bw_recent - bw_prev if bw_prev else 0.0
    bw_conf = min(1.0, len(bw_vals) / 8.0)
    bw_sev = 62.0 if abs(bw_delta) >= 1.2 else 38.0 if abs(bw_delta) >= 0.6 else 18.0
    bw_mod = _module_payload(
        module_id="bodyweight",
        summary_line=f"{bw_recent:.1f}kg ({bw_delta:+.1f})",
        severity=bw_sev,
        confidence=bw_conf,
        headline="Bodyweight trajectory vs recent baseline.",
        drivers=[f"recent {bw_recent:.1f}kg", f"prev {bw_prev:.1f}kg", f"delta {bw_delta:+.2f}kg"],
        chart_points=[float(v) for v in bw_vals[-10:]],
        chart_labels=[str(x.get("date") or "")[5:] for x in nutrition_data["series"][-10:]],
        action={"label": "Open Mealplan", "href": "/ernaehrung/plan", "kind": "link"},
        updated_ts=nutrition_data.get("last_ts"),
    )

    stress_score = 0.0
    if rec_delta < -8:
        stress_score += 30.0
    if sleep_recent and sleep_recent < 6.0:
        stress_score += 30.0
    if load_delta > 20:
        stress_score += 24.0
    stress_score += min(16.0, max(0.0, float(_safe_float(hrv_data.get("hr_latest")) or 0.0) - 72.0))
    stress_conf = min(1.0, (recovery_conf + sleep_conf + load_conf) / 3.0)
    stress_mod = _module_payload(
        module_id="stress",
        summary_line=f"proxy {stress_score:.0f}/100",
        severity=stress_score,
        confidence=stress_conf,
        headline="Composite stress proxy from recovery, sleep and load.",
        drivers=[f"recovery {rec_delta:+.1f}%", f"sleep {sleep_recent:.1f}", f"load {load_delta:+.1f}%"],
        chart_points=[recovery["severity"], sleep_mod["severity"], load_mod["severity"], _clamp_severity(stress_score)],
        chart_labels=["recovery", "sleep", "load", "stress"],
        action={"label": "Open HRV", "href": "/hrv", "kind": "link"},
        updated_ts=hrv_data.get("last_ts"),
    )

    modules = [
        recovery,
        training_mod,
        plan_mod,
        load_mod,
        nutrition_mod,
        sleep_mod,
        runs_mod,
        bw_mod,
        stress_mod,
    ]
    ctx = {
        "training": training_data,
        "runs": runs_data,
        "hrv": hrv_data,
        "nutrition": nutrition_data,
        "plan": plan_data,
    }
    return modules, ctx


def _brief_from_modules(modules: list[dict[str, Any]]) -> dict[str, Any]:
    if not modules:
        return {"athlete": "STABLE", "top": "No active signal", "system": "STABLE", "lines": ["ATHLETE: STABLE", "TOP: no signal", "SYSTEM: STABLE"]}
    athlete_ids = {"recovery", "training", "load", "sleep", "stress", "runs", "bodyweight", "nutrition", "plan"}
    ath_vals = [m["severity"] for m in modules if m["id"] in athlete_ids and m["state"] != "uncertain"]
    ath_score = _mean([float(v) for v in ath_vals]) or 0.0
    if ath_score >= 76:
        ath_status = "AT RISK"
    elif ath_score >= 58:
        ath_status = "STRAINED"
    elif ath_score >= 36:
        ath_status = "VOLATILE"
    else:
        ath_status = "STABLE"

    sys_candidates = [m for m in modules if m["id"] in {"plan", "nutrition"}]
    sys_score = _mean([float(m["severity"]) for m in sys_candidates if m["state"] != "uncertain"]) or 0.0
    stale_count = sum(1 for m in modules if m["state"] == "uncertain")
    if stale_count >= 3:
        sys_status = "STALE"
    elif sys_score >= 62:
        sys_status = "DEGRADED"
    else:
        sys_status = "STABLE"

    top = sorted(modules, key=lambda m: (m["severity"] * m["confidence"]), reverse=True)[0]
    top_signal = top["summaryLine"]
    return {
        "athlete": ath_status,
        "top": top_signal,
        "system": sys_status,
        "lines": [f"ATHLETE: {ath_status}", f"TOP: {top_signal}", f"SYSTEM: {sys_status}"],
    }


def _events_from_modules(modules: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    now = _now_ts()
    for m in sorted(modules, key=lambda x: (x["severity"] * x["confidence"]), reverse=True):
        if m["state"] in {"critical", "warning"}:
            active.append(
                {
                    "id": f"evt_{m['id']}",
                    "ts": now,
                    "moduleId": m["id"],
                    "title": m["labelShort"],
                    "detailLine": m["summaryLine"],
                    "severity": m["severity"],
                    "confidence": m["confidence"],
                    "status": "active",
                }
            )
        elif m["state"] == "ok" and m["confidence"] >= 0.65:
            resolved.append(
                {
                    "id": f"rsv_{m['id']}",
                    "ts": now,
                    "moduleId": m["id"],
                    "title": m["labelShort"],
                    "detailLine": "back in range",
                    "severity": m["severity"],
                    "confidence": m["confidence"],
                    "status": "resolved",
                }
            )
    return {"active": active[:5], "resolved": resolved[:2]}


def get_core_summary(mode: str = "live") -> dict[str, Any]:
    mode = (mode or "live").strip().lower()
    if mode not in {"live", "today", "7d"}:
        mode = "today"
    modules, ctx = _build_core_modules(mode)
    sources = {
        "hrv": _source_row("hrv", ctx["hrv"].get("last_ts"), stale_after_s=36 * 3600),
        "training": _source_row("training", ctx["training"].get("last_ts"), stale_after_s=72 * 3600),
        "runs": _source_row("runs", ctx["runs"].get("last_ts"), stale_after_s=96 * 3600),
        "nutrition": _source_row("nutrition", ctx["nutrition"].get("last_ts"), stale_after_s=48 * 3600),
        "plan": _source_row("plan", ctx["plan"].get("last_ts"), stale_after_s=7 * 24 * 3600),
    }
    for m in modules:
        src_key = "plan" if m["id"] == "plan" else ("nutrition" if m["id"] in {"nutrition", "bodyweight"} else ("runs" if m["id"] == "runs" else "hrv" if m["id"] in {"recovery", "sleep", "stress"} else "training"))
        src = sources.get(src_key)
        if src and src["stale"]:
            m["confidence"] = min(float(m["confidence"]), 0.42)
            m["state"] = "uncertain"
            m["summaryLine"] = "source missing" if not src.get("lastSeenTs") else "source stale"
    brief = _brief_from_modules(modules)
    events = _events_from_modules(modules)
    ranked = sorted(modules, key=lambda x: (x["severity"] * x["confidence"]), reverse=True)
    return {
        "ok": True,
        "mode": mode,
        "lastSyncTs": _now_ts(),
        "sources": sources,
        "brief": brief,
        "modules": modules,
        "events": events,
        "autofocusTargets": [m["id"] for m in ranked[:2]],
    }


def get_core_state(window: str = "today") -> dict[str, Any]:
    ensure_core_schema()
    cutoff = _window_start(window)
    now = _now_ts()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sess = cur.execute(
        """
        SELECT id, last_seen_ts, is_active, current_route, COALESCE(req_count,0) AS req_count
        FROM core_session
        WHERE last_seen_ts >= ?
        ORDER BY is_active DESC, last_seen_ts DESC
        LIMIT 30
        """,
        (cutoff,),
    ).fetchall()
    err_row = cur.execute(
        "SELECT SUM(CASE WHEN status_code>=500 THEN 1 ELSE 0 END) AS err, COUNT(*) AS total FROM core_event WHERE ts>=?",
        (cutoff,),
    ).fetchone()
    conn.close()

    nodes = [dict(n, kind="system", label=n["id"], size=18, active=True, color="#30363d", health=0.85) for n in SYSTEM_NODES]
    edges = []
    for i, r in enumerate(sess):
        nid = r["id"]
        nodes.append(
            {
                "id": nid,
                "kind": "user",
                "label": nid[:8],
                "x": 0.45 + ((i % 5) * 0.04),
                "y": 0.55 + ((i // 5) * 0.04),
                "size": 8,
                "active": int(r["is_active"] or 0) == 1 and int(r["last_seen_ts"] or 0) >= now - 20,
                "req_count": int(r["req_count"] or 0),
                "route": r["current_route"] or "/",
                "device_type": "desktop",
                "os": "unknown",
                "browser": "unknown",
                "color": "#39a0ed",
                "last_seen_ts": int(r["last_seen_ts"] or 0),
            }
        )
        edges.append({"source": nid, "target": "API", "weight": 1, "latency_ms": 0, "errors": 0})

    return {
        "ok": True,
        "window": (window or "today").strip().lower(),
        "ts": now,
        "nodes": nodes[:40],
        "edges": edges[:70],
        "counters": {
            "active_sessions": sum(1 for s in sess if int(s["is_active"] or 0) == 1),
            "visible_user_nodes": len(sess),
            "archive_nodes": 0,
            "events": int(err_row["total"] or 0) if err_row else 0,
            "errors": int(err_row["err"] or 0) if err_row else 0,
            "latency_p95": 0.0,
        },
        "health": {
            "overall": "stable",
            "latency_status": "stable",
            "error_status": "stable",
            "freshness_status": "stable",
            "job_status": "stable",
            "node_health": {n["id"]: 0.85 for n in SYSTEM_NODES},
        },
    }


def get_core_node_details(node_id: str, window: str = "today") -> dict[str, Any]:
    if any(n["id"] == node_id for n in SYSTEM_NODES):
        return {"ok": True, "node_id": node_id, "kind": "system", "top_routes": []}
    return {
        "ok": True,
        "node_id": node_id,
        "kind": "user",
        "session": {"id": node_id, "last_seen_ts": _now_ts(), "start_ts": _now_ts(), "req_count": 0, "route": "/core"},
        "top_routes": [],
    }


def _intel_direction(signal: float, prev_signal: float | None) -> str:
    if prev_signal is None:
        return "flat"
    if signal > prev_signal:
        return "up"
    if signal < prev_signal:
        return "down"
    return "flat"


def should_emit_intel(
    *,
    last_signal_value: float | None,
    last_direction: str | None,
    signal_value: float,
    days_since_last_shown: int | None,
    cooldown_days: int,
    z_threshold: float = 0.7,
    pct_threshold: float = 0.2,
) -> IntelGateDecision:
    if last_signal_value is None:
        return IntelGateDecision(True, "flat")
    direction = _intel_direction(signal_value, last_signal_value)
    diff = abs(signal_value - last_signal_value)
    rel = diff / abs(last_signal_value) if abs(last_signal_value) > 1e-9 else (1.0 if diff > 0 else 0.0)
    direction_flip = direction in {"up", "down"} and (last_direction or "") in {"up", "down"} and direction != (last_direction or "")
    cooldown_ok = days_since_last_shown is not None and days_since_last_shown >= max(0, int(cooldown_days))
    emit = diff >= z_threshold or rel >= pct_threshold or direction_flip or (cooldown_ok and diff >= 0.2)
    return IntelGateDecision(emit, direction)


def _candidate_base(
    *,
    category: str,
    entity_type: str,
    entity_id: str,
    metric: str,
    window: str,
    title: str,
    body: str,
    severity: float,
    confidence: float,
    signal_value: float,
    baseline_value: float | None,
    direction: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    fp = hashlib.sha256(f"{category}|{entity_id}|{metric}|{window}".encode("utf-8")).hexdigest()
    return {
        "category": category,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metric": metric,
        "window": window,
        "title": title[:120],
        "body": body[:220],
        "severity": max(0.0, min(1.0, float(severity))),
        "confidence": max(0.0, min(1.0, float(confidence))),
        "signal_value": float(signal_value),
        "baseline_value": baseline_value,
        "direction": direction,
        "evidence_json": evidence,
        "fingerprint_hash": fp,
    }


def _athlete_candidates() -> list[dict[str, Any]]:
    return []


def _system_candidates() -> list[dict[str, Any]]:
    return []


def _days_between(day_a: str | None, day_b: str | None) -> int | None:
    if not day_a or not day_b:
        return None
    try:
        a = datetime.strptime(day_a, "%Y-%m-%d").date()
        b = datetime.strptime(day_b, "%Y-%m-%d").date()
        return abs((a - b).days)
    except Exception:
        return None


def get_core_intel_payload(mode: str = "today", day_iso: str | None = None, force: bool = False) -> dict[str, Any]:
    ensure_core_schema()
    day = (day_iso or _today_iso()).strip()
    candidates = (_athlete_candidates() or []) + (_system_candidates() or [])
    active = candidates[:5]
    resolved = []

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    for c in candidates:
        fp = c.get("fingerprint_hash")
        if not fp:
            continue
        row = cur.execute(
            "SELECT last_signal_value, last_direction, last_shown_day, cooldown_days FROM core_intel_state WHERE fingerprint_hash=?",
            (fp,),
        ).fetchone()
        if row is None:
            continue
        last_signal = float(row["last_signal_value"] or 0.0)
        curr_signal = float(c.get("signal_value") or 0.0)
        if abs(last_signal) >= 0.9 and abs(curr_signal) <= 0.35:
            rc = dict(c)
            rc["category"] = f"RESOLVED_{c['category']}"
            rc["title"] = f"Resolved: {c['entity_id']} back in range"
            rc["direction"] = "flat"
            resolved.append(rc)
    resolved = resolved[:2]

    for c in active:
        cur.execute(
            """
            INSERT INTO core_intel_state (fingerprint_hash, last_shown_day, last_signal_value, last_direction, cooldown_days)
            VALUES (?, ?, ?, ?, 2)
            ON CONFLICT(fingerprint_hash)
            DO UPDATE SET last_shown_day=excluded.last_shown_day,
                          last_signal_value=excluded.last_signal_value,
                          last_direction=excluded.last_direction
            """,
            (
                c["fingerprint_hash"],
                day,
                float(c.get("signal_value") or 0.0),
                c.get("direction") or "flat",
            ),
        )
    conn.commit()
    conn.close()

    if not active:
        active = [
            _candidate_base(
                category="SYS_BASELINE",
                entity_type="subsystem",
                entity_id="API",
                metric="state",
                window="today",
                title="CORE baseline stable",
                body="no high-novelty deltas • conf 0.80",
                severity=0.2,
                confidence=0.8,
                signal_value=0.0,
                baseline_value=0.0,
                direction="flat",
                evidence={"probe": "observe next update cycle"},
            )
        ]

    brief = {
        "athlete_status": "STABLE",
        "top_signal": active[0]["title"],
        "system_status": "STABLE",
        "lines": [f"ATHLETE: STABLE", f"TOP: {active[0]['title']}", "SYSTEM: STABLE"],
    }

    focus = {"entity_type": active[0]["entity_type"], "entity_id": active[0]["entity_id"], "nodes": ["API"], "severity": active[0]["severity"]}

    def _serialize(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "category": card.get("category"),
            "entity_type": card.get("entity_type"),
            "entity_id": card.get("entity_id"),
            "metric": card.get("metric"),
            "window": card.get("window"),
            "title": card.get("title"),
            "body": card.get("body"),
            "severity": float(card.get("severity") or 0.0),
            "confidence": float(card.get("confidence") or 0.0),
            "signal_value": float(card.get("signal_value") or 0.0),
            "baseline_value": card.get("baseline_value"),
            "direction": card.get("direction") or "flat",
            "fingerprint_hash": card.get("fingerprint_hash"),
            "evidence_json": card.get("evidence_json") or {},
            "subline": f"{card.get('window') or 'today'} | conf {float(card.get('confidence') or 0.0):.2f}",
        }

    return {
        "ok": True,
        "mode": (mode or "today").strip().lower(),
        "day": day,
        "generated_at": _now_ts(),
        "brief": brief,
        "cards": [_serialize(c) for c in active[:5]],
        "resolved": [_serialize(c) for c in resolved[:2]],
        "focus_target": focus,
        "limits": {"active_max": 5, "resolved_max": 2},
    }


def _bucket_second(ts: float) -> int:
    return int(ts)


def _mark_observatory_active(ttl_s: int | None = None) -> None:
    global OBS_ACTIVE_UNTIL
    ttl = int(ttl_s or _core_observatory_active_ttl_s())
    until = time.time() + max(20, ttl)
    with OBS_LOCK:
        if until > OBS_ACTIVE_UNTIL:
            OBS_ACTIVE_UNTIL = until


def _observatory_is_active() -> bool:
    now = time.time()
    with OBS_LOCK:
        return bool(OBS_SUBS) or OBS_ACTIVE_UNTIL >= now


def _record_ob_metric(ts: float, *, count: int = 0, err: int = 0, latency_ms: float = 0.0) -> None:
    sec = _bucket_second(ts)
    with OBS_LOCK:
        row = OBS_METRIC_BUCKETS[-1] if OBS_METRIC_BUCKETS else None
        if row is None or int(row["sec"]) != sec:
            row = {"sec": sec, "count": 0, "err": 0, "lat_sum": 0.0}
            OBS_METRIC_BUCKETS.append(row)
        row["count"] = int(row["count"]) + int(count)
        row["err"] = int(row["err"]) + int(err)
        row["lat_sum"] = float(row["lat_sum"]) + float(latency_ms)


def _emit_observatory_event(payload: dict[str, Any], *, broadcast: bool = True) -> None:
    global OBS_DROPPED_EVENTS, OBS_EMIT_SEC, OBS_EMIT_COUNT
    payload = dict(payload)
    payload.setdefault("t", round(time.time(), 3))
    kind = str(payload.get("k") or "")
    # harte Schutzbremse: Flow-Events pro Sekunde deckeln, um Pi und Netzwerk zu schützen
    sec = int(float(payload.get("t") or time.time()))
    cap = _core_observatory_rate_cap()
    with OBS_LOCK:
        if kind == "flow":
            if OBS_EMIT_SEC != sec:
                OBS_EMIT_SEC = sec
                OBS_EMIT_COUNT = 0
            if OBS_EMIT_COUNT >= cap:
                OBS_DROPPED_EVENTS += 1
                return
            OBS_EMIT_COUNT += 1
        OBS_EVENTS.append(payload)
        if payload.get("k") == "log":
            OBS_LOGS.append(payload)
        if broadcast:
            stale_q: list[queue.Queue] = []
            for q in OBS_SUBS:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    OBS_DROPPED_EVENTS += 1
                    stale_q.append(q)
            for q in stale_q:
                OBS_SUBS.discard(q)


def _emit_observatory_log(level: str, message: str, request_id: str | None = None) -> None:
    _emit_observatory_event(
        {
            "k": "log",
            "lvl": "ERR" if str(level).upper().startswith("E") else "WARN",
            "msg": str(message)[:180],
            "id": (request_id or "")[:12] or None,
        }
    )


def _emit_observatory_flow(*, route: str, method: str, status: int, latency_ms: int, subsystem: str, request_id: str) -> None:
    ts = time.time()
    ok = int(status) < 500
    _record_ob_metric(ts, count=1, err=0 if ok else 1, latency_ms=max(0, int(latency_ms)))
    label = f"{method.upper()} {route}"
    _emit_observatory_event(
        {
            "k": "flow",
            "id": request_id[:8],
            "src": "UI",
            "dst": "API",
            "label": label[:120],
            "ms": max(0, int(latency_ms)),
            "ok": bool(ok),
            "meta": {"status": int(status)},
        }
    )
    if subsystem and subsystem != "API":
        db_ms = max(1, int(latency_ms * 0.35))
        _emit_observatory_event(
            {
                "k": "flow",
                "id": request_id[:8],
                "src": "API",
                "dst": subsystem,
                "label": f"{subsystem} Zugriff",
                "ms": db_ms,
                "ok": bool(ok),
                "meta": {"status": int(status), "route": route[:120]},
            }
        )


def _safe_system_metrics(*, collect_processes: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = time.time()
    cpu = 0.0
    ram = 0.0
    disk = 0.0
    load = [0.0, 0.0]
    net = {"up": 0.0, "down": 0.0}
    uptime_s = 0
    procs: list[dict[str, Any]] = []
    if psutil is None:
        return ({"cpu": cpu, "ram": ram, "disk": disk, "load": load, "net": net, "uptime_s": uptime_s, "t": round(now, 3)}, procs)
    try:
        cpu = float(psutil.cpu_percent(interval=None))
        ram = float(psutil.virtual_memory().percent)
        disk = float(psutil.disk_usage("/").percent)
        la = os.getloadavg()
        load = [float(la[0]), float(la[1])]
        uptime_s = int(max(0.0, now - float(psutil.boot_time())))
        nio = psutil.net_io_counters()
        last = getattr(_safe_system_metrics, "_last_net", None)
        if last is None:
            net = {"up": 0.0, "down": 0.0}
        else:
            dt = max(1.0, now - float(last["t"]))
            net = {
                "up": max(0.0, float(nio.bytes_sent) - float(last["sent"])) / dt,
                "down": max(0.0, float(nio.bytes_recv) - float(last["recv"])) / dt,
            }
        setattr(_safe_system_metrics, "_last_net", {"t": now, "sent": float(nio.bytes_sent), "recv": float(nio.bytes_recv)})

        if collect_processes:
            plist: list[dict[str, Any]] = []
            for p in psutil.process_iter(attrs=["name", "cpu_percent", "memory_percent"]):
                info = p.info
                name = str(info.get("name") or "")[:24]
                if not name:
                    continue
                plist.append(
                    {
                        "name": name,
                        "cpu": float(info.get("cpu_percent") or 0.0),
                        "mem": float(info.get("memory_percent") or 0.0),
                    }
                )
            procs = sorted(plist, key=lambda x: (x["cpu"], x["mem"]), reverse=True)[:6]
    except Exception:
        pass
    return (
        {
            "k": "sys",
            "t": round(now, 3),
            "cpu": round(cpu, 1),
            "ram": round(ram, 1),
            "disk": round(disk, 1),
            "load": [round(load[0], 2), round(load[1], 2)],
            "net": {"up": round(float(net["up"]), 1), "down": round(float(net["down"]), 1)},
            "uptime_s": int(uptime_s),
        },
        procs,
    )


def _observatory_sampler_loop() -> None:
    global OBS_SYS_STATE, OBS_PROC_STATE
    cycle = 0
    while True:
        try:
            if not _observatory_is_active():
                time.sleep(2.0)
                continue
            collect_processes = cycle % 5 == 0
            sys_evt, procs = _safe_system_metrics(collect_processes=collect_processes)
            with OBS_LOCK:
                OBS_SYS_STATE = sys_evt
                if procs:
                    OBS_PROC_STATE = procs
            _emit_observatory_event(sys_evt)
            if procs:
                _emit_observatory_event({"k": "proc", "t": round(time.time(), 3), "top": procs})
            # automatische Drosselhinweise bei Last
            if float(sys_evt.get("cpu") or 0.0) >= 85.0 or float((sys_evt.get("load") or [0.0])[0]) >= 3.0:
                _emit_observatory_log("WARN", "Systemlast hoch: Stream wird gedrosselt")
            cycle += 1
        except Exception:
            pass
        time.sleep(1.0)


def _ensure_observatory_thread() -> None:
    global OBS_THREAD_STARTED, OBS_THREAD
    with OBS_LOCK:
        if OBS_THREAD_STARTED:
            return
        OBS_THREAD_STARTED = True
        OBS_THREAD = threading.Thread(target=_observatory_sampler_loop, name="core-observatory", daemon=True)
        OBS_THREAD.start()


def _observatory_nodes() -> list[dict[str, Any]]:
    return [
        {"id": "UI", "label": "UI", "group": "core", "x": 0.16, "y": 0.48},
        {"id": "API", "label": "API", "group": "core", "x": 0.50, "y": 0.48},
        {"id": "TRAINING_DB", "label": "TRAINING_DB", "group": "db", "x": 0.34, "y": 0.68},
        {"id": "PLANS_DB", "label": "PLANS_DB", "group": "db", "x": 0.44, "y": 0.70},
        {"id": "HRV_DB", "label": "HRV_DB", "group": "db", "x": 0.56, "y": 0.70},
        {"id": "RUNS_DB", "label": "RUNS_DB", "group": "db", "x": 0.66, "y": 0.68},
        {"id": "NUTRITION_DB", "label": "NUTRITION_DB", "group": "db", "x": 0.50, "y": 0.80},
        {"id": "STRAVA", "label": "STRAVA", "group": "ext", "x": 0.83, "y": 0.24},
        {"id": "APPLE_HEALTH", "label": "APPLE_HEALTH", "group": "ext", "x": 0.85, "y": 0.42},
        {"id": "TELEGRAM", "label": "TELEGRAM", "group": "ext", "x": 0.82, "y": 0.60},
        {"id": "AI", "label": "AI", "group": "ext", "x": 0.74, "y": 0.14},
        {"id": "CRON", "label": "CRON", "group": "job", "x": 0.28, "y": 0.18},
    ]


def _observatory_edges() -> list[dict[str, Any]]:
    return [
        {"src": "UI", "dst": "API"},
        {"src": "API", "dst": "TRAINING_DB"},
        {"src": "API", "dst": "PLANS_DB"},
        {"src": "API", "dst": "HRV_DB"},
        {"src": "API", "dst": "RUNS_DB"},
        {"src": "API", "dst": "NUTRITION_DB"},
        {"src": "API", "dst": "STRAVA"},
        {"src": "API", "dst": "APPLE_HEALTH"},
        {"src": "API", "dst": "TELEGRAM"},
        {"src": "API", "dst": "AI"},
        {"src": "CRON", "dst": "API"},
    ]


def get_core_observatory_snapshot(mode: str = "live") -> dict[str, Any]:
    _ensure_observatory_thread()
    _mark_observatory_active(120)
    now = time.time()
    m = (mode or "live").strip().lower()
    if m not in {"live", "today", "7d"}:
        m = "today"
    window_s = 60 if m == "live" else (20 * 60 if m == "today" else 60 * 60)
    cutoff = now - window_s
    with OBS_LOCK:
        events = [e for e in list(OBS_EVENTS) if float(e.get("t") or 0.0) >= cutoff]
        logs = list(OBS_LOGS)[-8:]
        sys_state = dict(OBS_SYS_STATE)
        procs = list(OBS_PROC_STATE)
        dropped = int(OBS_DROPPED_EVENTS)
        buckets = list(OBS_METRIC_BUCKETS)[-60:]

    flow_events = [e for e in events if e.get("k") == "flow"]
    req_count = len(flow_events)
    err_count = sum(1 for e in flow_events if not bool(e.get("ok")))
    lat_vals = [float(e.get("ms") or 0.0) for e in flow_events]
    avg_latency = (sum(lat_vals) / len(lat_vals)) if lat_vals else 0.0
    secs = max(1.0, min(float(window_s), now - cutoff))
    req_s = req_count / secs
    histogram = [int(b.get("count") or 0) for b in buckets][-60:]

    return {
        "ok": True,
        "mode": m,
        "t": round(now, 3),
        "nodes": _observatory_nodes(),
        "edges": _observatory_edges(),
        "events": events[-140:],
        "logs": logs[-8:],
        "system": sys_state or {"k": "sys", "t": round(now, 3), "cpu": 0.0, "ram": 0.0, "disk": 0.0, "load": [0.0, 0.0], "net": {"up": 0.0, "down": 0.0}, "uptime_s": 0},
        "processes": procs[:6],
        "stats": {
            "flows_active": sum(1 for e in flow_events if float(e.get("t") or 0.0) >= now - 4.0),
            "events_s": round(req_s, 2),
            "req_s": round(req_s, 2),
            "err_rate": round((err_count / max(1, req_count)) * 100.0, 1),
            "avg_ms": round(avg_latency, 1),
            "dropped": dropped,
        },
        "histogram_60s": histogram,
    }


def observatory_event_stream(mode: str = "live", categories: str = ""):
    _ensure_observatory_thread()
    _mark_observatory_active(300)
    q: queue.Queue = queue.Queue(maxsize=96)
    with OBS_LOCK:
        OBS_SUBS.add(q)
    try:
        snapshot = get_core_observatory_snapshot(mode=mode)
        yield "retry: 3000\n\n"
        yield f"event: core\ndata: {json.dumps({'k': 'snapshot', 'payload': snapshot}, separators=(',', ':'))}\n\n"
        while True:
            try:
                payload = q.get(timeout=10)
                _mark_observatory_active(300)
                yield f"event: core\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except queue.Empty:
                _mark_observatory_active(300)
                yield "event: ping\ndata: {}\n\n"
    finally:
        with OBS_LOCK:
            OBS_SUBS.discard(q)


def sse_event_stream():
    q: queue.Queue = queue.Queue(maxsize=16)
    with SSE_LOCK:
        SSE_SUBS.add(q)
    try:
        yield "retry: 10000\n\n"
        while True:
            try:
                payload = q.get(timeout=15)
                yield f"event: delta\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
    finally:
        with SSE_LOCK:
            SSE_SUBS.discard(q)
