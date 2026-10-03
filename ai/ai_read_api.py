from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import time
import uuid
from werkzeug.exceptions import HTTPException
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psutil
from flask import Blueprint, jsonify, request

from security.write_guard import require_ai_read

ai_api = Blueprint("ai_api", __name__, url_prefix="/api/ai")

@ai_api.before_request
def _ai_log_request():
    req_id = uuid.uuid4().hex[:12]
    request.environ["th_req_id"] = req_id
    qs = request.query_string.decode("utf-8", errors="replace")
    auth_raw = (request.headers.get("Authorization") or "").strip()
    auth_scheme = auth_raw.split()[0] if auth_raw else ""
    print(
        "[ai_api] req_id={req_id} path={path} qs={qs} remote={remote} xff={xff} "
        "ua={ua} accept={accept} auth_present={auth_present} auth_scheme={auth_scheme}".format(
            req_id=req_id,
            path=request.path,
            qs=qs,
            remote=request.remote_addr or "",
            xff=request.headers.get("X-Forwarded-For") or "",
            ua=request.headers.get("User-Agent") or "",
            accept=request.headers.get("Accept") or "",
            auth_present=bool(request.headers.get("Authorization")),
            auth_scheme=auth_scheme,
        )
    )

@ai_api.after_request
def _ai_log_response(response):
    req_id = request.environ.get("th_req_id", "-")
    print(
        "[ai_api] req_id={req_id} status={status} content_type={ct} content_length={length}".format(
            req_id=req_id,
            status=response.status_code,
            ct=response.content_type or "",
            length=response.content_length,
        )
    )
    return response

@ai_api.errorhandler(HTTPException)
def _ai_http_exception(e: HTTPException):
    # garantiert JSON statt HTML bei abort(401/403/...)
    payload = {
        "ok": False,
        "error": getattr(e, "name", "HTTPException"),
        "status": int(getattr(e, "code", 500) or 500),
        "detail": str(getattr(e, "description", ""))[:500],
    }
    return jsonify(payload), payload["status"]


@ai_api.errorhandler(Exception)
def _ai_unhandled_exception(e: Exception):
    payload = {
        "ok": False,
        "error": "internal_error",
        "status": 500,
        "detail": str(e)[:500],
    }
    return jsonify(payload), 500


BASE_DIR = "/opt/liva/database"
RUNS_DB = os.getenv("LIVA_RUNS_DB") or f"{BASE_DIR}/runs.sqlite3"
TRAINING_DB = os.getenv("LIVA_TRAINING_DB") or f"{BASE_DIR}/training.sqlite3"
PLANS_DB = os.getenv("LIVA_PLANS_DB") or f"{BASE_DIR}/plans.sqlite3"
ERNAEHRUNG_DB = os.getenv("LIVA_ERNAEHRUNG_DB") or f"{BASE_DIR}/ernaehrung.sqlite3"
HRV_DB = os.getenv("LIVA_HRV_DB") or f"{BASE_DIR}/hrv.sqlite3"


@ai_api.route("/health", methods=["GET"], strict_slashes=False)
@require_ai_read
def health():
    return jsonify({"ok": True, "service": "liva-ai-read", "ts": int(time.time())})


@ai_api.route("/meta", methods=["GET"], strict_slashes=False)
@require_ai_read
def meta():
    return jsonify({
        "ok": True,
        "service": "liva-ai-read",
        "ts": int(time.time()),
        "dbs": {
            "runs_db": RUNS_DB,
            "training_db": TRAINING_DB,
            "plans_db": PLANS_DB,
            "ernaehrung_db": ERNAEHRUNG_DB,
            "hrv_db": HRV_DB,

        },
        "datasets": [
            {
                "name": "runs",
                "fields": ["date_raw", "date_iso", "distance_m", "moving_time_s", "pace_s", "avg_hr", "max_hr", "elev_m"],
                "time_fields": ["date_iso"],
            },
            {
                "name": "hrv_series",
                "fields": ["date", "rmssd_ms", "hr_bpm", "sdnn_ms"],
                "time_fields": ["date"],
            },
            {
                "name": "HRVseries",
                "fields": ["date", "rmssd_ms", "hr_bpm", "sdnn_ms"],
                "time_fields": ["date"],
            },
            {
                "name": "training_sets",
                "fields": ["date_iso", "week", "workout_name", "exercise", "variation", "device", "laterality", "set_number", "reps", "weight", "rpe", "e1rm", "tonnage"],
                "time_fields": ["date_iso", "week"],
            },
            {
                "name": "weight_logs",
                "fields": ["date_iso", "weight_kg", "kcal", "protein", "carbs", "fat", "sugar"],
                "time_fields": ["date_iso"],
            },
            {
                "name": "sessions",
                "fields": ["workout_id", "date_iso", "week", "workout_name", "n_exercises", "n_sets", "total_reps", "total_tonnage", "avg_rpe", "avg_e1rm"],
                "time_fields": ["date_iso", "week"],
            },
            {
                "name": "plans_list",
                "fields": ["id", "name", "block_length", "is_active", "updated_at", "created_at"],
                "time_fields": ["updated_at", "created_at"],
            },
            {
                "name": "plans",
                "fields": ["id", "name", "block_length", "is_active", "updated_at", "created_at", "summary", "plan"],
                "time_fields": ["updated_at", "created_at"],
            },
        ],
        "ops": ["eq", "neq", "lt", "lte", "gt", "gte", "contains", "in"],
    })


@ai_api.get("/pi_stats")
@require_ai_read
def pi_stats():
    data = _get_pi_stats_cached()
    return jsonify({"ok": True, **data})


_PI_STATS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default

def _to_mb(x) -> Optional[float]:
    return round(x / 1024 / 1024, 1) if x is not None else None

def _to_gb(x) -> Optional[float]:
    return round(x / 1024 / 1024 / 1024, 1) if x is not None else None

def _read_temp_c() -> Optional[float]:
    temps = _safe(lambda: psutil.sensors_temperatures(fahrenheit=False), {})
    if not temps:
        return None
    for entries in temps.values():
        if not entries:
            continue
        for entry in entries:
            val = getattr(entry, "current", None)
            if val is not None:
                return round(float(val), 1)
    return None

def _get_pi_stats_cached(ttl_s: float = 3.0) -> Dict[str, Any]:
    now = time.time()
    if _PI_STATS_CACHE["data"] and (now - _PI_STATS_CACHE["ts"]) < ttl_s:
        return _PI_STATS_CACHE["data"]

    boot_time = _safe(lambda: psutil.boot_time(), None)
    uptime_s = int(now - boot_time) if boot_time else None

    load = _safe(lambda: os.getloadavg(), None)
    load1 = load[0] if load else None
    load5 = load[1] if load else None
    load15 = load[2] if load else None

    cpu_pct = _safe(lambda: psutil.cpu_percent(interval=0.08), None)
    mem = _safe(lambda: psutil.virtual_memory(), None)
    disk = _safe(lambda: psutil.disk_usage("/"), None)

    data = {
        "ts": int(now),
        "host": socket.gethostname(),
        "uptime_s": uptime_s,
        "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
        "ram_used_mb": _to_mb(mem.used) if mem else None,
        "ram_total_mb": _to_mb(mem.total) if mem else None,
        "disk_used_gb": _to_gb(disk.used) if disk else None,
        "disk_total_gb": _to_gb(disk.total) if disk else None,
        "load1": round(load1, 2) if load1 is not None else None,
        "load5": round(load5, 2) if load5 is not None else None,
        "load15": round(load15, 2) if load15 is not None else None,
        "temp_c": _read_temp_c(),
    }

    _PI_STATS_CACHE["ts"] = now
    _PI_STATS_CACHE["data"] = data
    return data


# ----------------------------
# Query helpers
# ----------------------------

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def _to_float(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return 0.0
        s = s.replace(",", ".")
        try:
            return float(s)
        except Exception:
            return 0.0
    return 0.0

def _parse_q() -> Dict[str, Any]:
    """
    Supports:
      - GET:  /api/ai/query?q={...json...}
      - POST: /api/ai/query   body: {...}
    """
    if request.method == "POST":
        return request.get_json(force=True)

    raw = (request.args.get("q") or "").strip()
    if not raw:
        return {}
    return json.loads(raw)

def _days_to_date_filter(days: int, column: str = "date_iso") -> Tuple[str, str]:
    """
    Returns SQL snippet + param like: ("date(<column>) >= date('now', ?)", f"-{days} day")
    """
    return f"date({column}) >= date('now', ?)", f"-{int(days)} day"

def _clamp_limit(raw: Any, default: int, max_limit: int) -> int:
    try:
        val = int(raw)
    except Exception:
        val = default
    if val <= 0:
        val = default
    return min(val, max_limit)

def _apply_time_filters(where: List[str], params: List[Any], time_obj: Dict[str, Any], column: str) -> None:
    days = int(time_obj.get("days") or 0)
    date_from = (time_obj.get("from") or "").strip()
    date_to = (time_obj.get("to") or "").strip()

    if days > 0:
        w, p = _days_to_date_filter(days, column)
        where.append(w)
        params.append(p)

    if date_from:
        where.append(f"date({column}) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append(f"date({column}) <= date(?)")
        params.append(date_to)

def _apply_filters(where: List[str], params: List[Any], filters: List[Dict[str, Any]], colmap: Dict[str, str]) -> None:
    op_map = {"eq": "=", "neq": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

    for f in (filters or []):
        field = (f.get("field") or "").strip()
        op = (f.get("op") or "").strip()
        val = f.get("value")
        if not field or field not in colmap or op not in (set(op_map.keys()) | {"contains", "in"}):
            continue

        col = colmap[field]

        if op == "contains":
            where.append(f"{col} LIKE ?")
            params.append(f"%{'' if val is None else str(val)}%")
            continue

        if op == "in":
            if not isinstance(val, list) or not val:
                continue
            where.append(f"{col} IN ({','.join(['?'] * len(val))})")
            params.extend(val)
            continue

        if val is None:
            if op == "eq":
                where.append(f"{col} IS NULL")
            elif op == "neq":
                where.append(f"{col} IS NOT NULL")
            continue

        where.append(f"{col} {op_map[op]} ?")
        params.append(val)

# ----------------------------
# HRV helpers
# ----------------------------

def _to_optional_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        s = s.replace(",", ".")
        try:
            return float(s)
        except Exception:
            return None
    return None

def _parse_hrv_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        pass
    if len(text) >= 5 and text[-5] in "+-" and text[-4:-2].isdigit() and text[-2:].isdigit():
        with_colon = f"{text[:-2]}:{text[-2:]}"
        try:
            return datetime.strptime(with_colon, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def _normalize_hrv_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    ts_dt = _parse_hrv_timestamp(data.get("ts_measurement"))
    date_dt = _parse_hrv_timestamp(data.get("date_utc"))
    base_dt = date_dt or ts_dt
    data["_ts_dt"] = ts_dt
    data["_date_dt"] = date_dt
    data["_date_only"] = base_dt.date() if base_dt else None
    data["_rmssd"] = _to_optional_float(data.get("rmssd"))
    data["_hr"] = _to_optional_float(data.get("hr"))
    data["_sdnn"] = _to_optional_float(data.get("sdnn"))
    data["_avnn"] = _to_optional_float(data.get("avnn"))
    return data

def _hrv_public_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "ts_measurement": row.get("ts_measurement"),
        "date_utc": row.get("date_utc"),
        "hr": row.get("_hr") if row.get("_hr") is not None else row.get("hr"),
        "rmssd": row.get("_rmssd") if row.get("_rmssd") is not None else row.get("rmssd"),
        "sdnn": row.get("_sdnn") if row.get("_sdnn") is not None else row.get("sdnn"),
        "avnn": row.get("_avnn") if row.get("_avnn") is not None else row.get("avnn"),
        "signal_quality": row.get("signal_quality"),
        "source_file": row.get("source_file"),
        "created_at": row.get("created_at"),
    }

def _hrv_series_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "ts_measurement": row.get("ts_measurement"),
        "date_utc": row.get("date_utc"),
        "hr": row.get("_hr") if row.get("_hr") is not None else row.get("hr"),
        "rmssd": row.get("_rmssd") if row.get("_rmssd") is not None else row.get("rmssd"),
        "sdnn": row.get("_sdnn") if row.get("_sdnn") is not None else row.get("sdnn"),
        "avnn": row.get("_avnn") if row.get("_avnn") is not None else row.get("avnn"),
        "signal_quality": row.get("signal_quality"),
    }

def _fetch_hrv_rows(limit: int = 5000) -> List[Dict[str, Any]]:
    conn = _connect(HRV_DB)
    cur = conn.cursor()
    sql = """
        SELECT ts_measurement, date_utc, hr, rmssd, sdnn, avnn, signal_quality, source_file, created_at
        FROM hrv_measurements
        ORDER BY ts_measurement ASC
    """
    if limit:
        rows = cur.execute(sql + " LIMIT ?", (limit,)).fetchall()
    else:
        rows = cur.execute(sql).fetchall()
    conn.close()
    return [_normalize_hrv_row(r) for r in rows]

def _fetch_hrv_latest() -> Optional[Dict[str, Any]]:
    conn = _connect(HRV_DB)
    cur = conn.cursor()
    row = cur.execute("""
        SELECT ts_measurement, date_utc, hr, rmssd, sdnn, avnn, signal_quality, source_file, created_at
        FROM hrv_measurements
        ORDER BY ts_measurement DESC
        LIMIT 1
    """).fetchone()
    conn.close()
    return _normalize_hrv_row(row) if row else None

def _latest_hrv_row(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    def _key(r: Dict[str, Any]) -> Tuple[int, float]:
        if r.get("_ts_dt"):
            return (2, r["_ts_dt"].timestamp())
        if r.get("_date_dt"):
            return (1, r["_date_dt"].timestamp())
        return (0, 0.0)
    return max(rows, key=_key)

def _compute_stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"mean": None, "sd": None}
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return {"mean": mean, "sd": math.sqrt(variance)}

def _compute_baseline(rows: List[Dict[str, Any]], latest_date: Optional[date], baseline_days: int) -> Dict[str, Any]:
    if not latest_date:
        return {"days": baseline_days, "count": 0, "rmssd": {"mean": None, "sd": None}, "hr": {"mean": None, "sd": None}, "sdnn": {"mean": None, "sd": None}}
    start = latest_date - timedelta(days=baseline_days)
    baseline_rows = [r for r in rows if r.get("_date_only") and start <= r["_date_only"] < latest_date]
    rmssd_vals = [r["_rmssd"] for r in baseline_rows if r.get("_rmssd") is not None]
    hr_vals = [r["_hr"] for r in baseline_rows if r.get("_hr") is not None]
    sdnn_vals = [r["_sdnn"] for r in baseline_rows if r.get("_sdnn") is not None]
    return {
        "days": baseline_days,
        "count": len(baseline_rows),
        "rmssd": _compute_stats(rmssd_vals),
        "hr": _compute_stats(hr_vals),
        "sdnn": _compute_stats(sdnn_vals),
    }

def _clamp_value(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))

def _compute_readiness(latest_row: Optional[Dict[str, Any]], baseline: Dict[str, Any]) -> Dict[str, Any]:
    if not latest_row:
        return {"score": 50, "status": "neutral", "warning": "Keine Messung vorhanden.", "z_rmssd": None, "z_hr": None, "z_total": None}
    if baseline.get("count", 0) < 3 or not baseline["rmssd"]["sd"] or not baseline["hr"]["sd"]:
        return {"score": 50, "status": "neutral", "warning": "Zu wenig Daten fuer eine stabile Baseline.", "z_rmssd": None, "z_hr": None, "z_total": None}
    if latest_row.get("_rmssd") is None or latest_row.get("_hr") is None:
        return {"score": 50, "status": "neutral", "warning": "Zu wenig Daten fuer Readiness.", "z_rmssd": None, "z_hr": None, "z_total": None}

    z_rmssd = (latest_row["_rmssd"] - baseline["rmssd"]["mean"]) / baseline["rmssd"]["sd"]
    z_hr = (baseline["hr"]["mean"] - latest_row["_hr"]) / baseline["hr"]["sd"]
    z_total = 0.7 * z_rmssd + 0.3 * z_hr
    score = _clamp_value(50 + 18 * z_total, 0, 100)

    status = "yellow"
    if score >= 65:
        status = "green"
    elif score < 45:
        status = "red"

    return {"score": score, "status": status, "warning": "", "z_rmssd": z_rmssd, "z_hr": z_hr, "z_total": z_total}

def _classify_delta(diff: Optional[float], pct: Optional[float], metric: str) -> Dict[str, str]:
    if diff is None or pct is None or not math.isfinite(pct):
        return {"direction": "flat", "status": "neutral"}
    if abs(pct) < 0.5:
        return {"direction": "flat", "status": "neutral"}
    up = diff > 0
    good = (metric == "hr" and not up) or (metric != "hr" and up)
    return {"direction": "up" if up else "down", "status": "good" if good else "bad"}

def _compute_trend(values: List[Optional[float]], window: int, metric: str) -> Dict[str, Any]:
    clean = [v for v in values if v is not None]
    if len(clean) < window * 2:
        return {"recent_mean": None, "previous_mean": None, "diff": None, "pct_change": None, "direction": "flat", "status": "neutral"}
    recent = clean[-window:]
    prev = clean[-window * 2:-window]
    recent_mean = sum(recent) / len(recent)
    prev_mean = sum(prev) / len(prev)
    if prev_mean == 0:
        return {"recent_mean": recent_mean, "previous_mean": prev_mean, "diff": None, "pct_change": None, "direction": "flat", "status": "neutral"}
    diff = recent_mean - prev_mean
    pct = (diff / prev_mean) * 100
    info = _classify_delta(diff, pct, metric)
    return {"recent_mean": recent_mean, "previous_mean": prev_mean, "diff": diff, "pct_change": pct, **info}

def _apply_hrv_range(rows: List[Dict[str, Any]], latest_date: Optional[date], range_days: Optional[int]) -> List[Dict[str, Any]]:
    if not latest_date or not range_days:
        return rows
    start = latest_date - timedelta(days=range_days - 1)
    return [r for r in rows if r.get("_date_only") and start <= r["_date_only"] <= latest_date]

def _build_hrv_summary_payload(range_raw: Any, baseline_raw: Any, trend_raw: Any) -> Dict[str, Any]:
    range_text = str(range_raw or "30").strip().lower()
    if range_text == "all":
        range_days = None
        range_out: Any = "all"
        limit = 0
    else:
        range_days = _clamp_limit(range_text, 30, 3650)
        range_out = range_days
        limit = 5000

    baseline_days = _clamp_limit(baseline_raw, 28, 365)
    trend_days = _clamp_limit(trend_raw, 7, 365)

    rows = _fetch_hrv_rows(limit=limit)
    latest = _latest_hrv_row(rows)
    latest_date = latest.get("_date_only") if latest else None
    baseline = _compute_baseline(rows, latest_date, baseline_days)
    readiness = _compute_readiness(latest, baseline)
    range_rows = _apply_hrv_range(rows, latest_date, range_days)
    range_rows = sorted(range_rows, key=lambda r: r.get("ts_measurement") or "")

    rmssd_values = [r.get("_rmssd") for r in range_rows]
    hr_values = [r.get("_hr") for r in range_rows]
    sdnn_values = [r.get("_sdnn") for r in range_rows]

    trends = {
        "window_days": trend_days,
        "rmssd": _compute_trend(rmssd_values, trend_days, "rmssd"),
        "hr": _compute_trend(hr_values, trend_days, "hr"),
        "sdnn": _compute_trend(sdnn_values, trend_days, "sdnn"),
    }

    return {
        "ok": True,
        "baseline_days": baseline_days,
        "range_days": range_out,
        "trend_days": trend_days,
        "counts": {"total": len(rows), "range": len(range_rows)},
        "latest": _hrv_public_row(latest),
        "baseline": baseline,
        "readiness": readiness,
        "trends": trends,
    }

def _select_runs(q: Dict[str, Any]) -> Dict[str, Any]:
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 100, 2000)

    conn = _connect(RUNS_DB)
    cur = conn.cursor()

    sql = "SELECT date AS date_raw, substr(date,1,10) AS date_iso, distance AS distance_m, moving_time AS moving_time_s, pace AS pace_s, avg_hr, max_hr, elevation_gain AS elev_m FROM runs"
    where = []
    params: List[Any] = []

    _apply_time_filters(where, params, time_obj, "date")

    _apply_filters(where, params, q.get("filters") or [], {
        "date_raw": "date",
        "date_iso": "substr(date,1,10)",
        "distance_m": "distance",
        "moving_time_s": "moving_time",
        "pace_s": "pace",
        "avg_hr": "avg_hr",
        "max_hr": "max_hr",
        "elev_m": "elevation_gain",
    })

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY date_iso DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "runs", "n": len(rows), "data": rows}

def _select_hrv_series(q: Dict[str, Any]) -> Dict[str, Any]:
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 500, 5000)

    conn = _connect(HRV_DB)
    cur = conn.cursor()

    date_expr = "COALESCE(date(date_utc), date(ts_measurement))"
    sql = f"""
        SELECT {date_expr} AS date, rmssd AS rmssd_ms, hr AS hr_bpm, sdnn AS sdnn_ms
        FROM hrv_measurements
    """
    where = []
    params: List[Any] = []

    _apply_time_filters(where, params, time_obj, date_expr)

    _apply_filters(where, params, q.get("filters") or [], {
        "date": date_expr,
        "rmssd_ms": "rmssd",
        "hr_bpm": "hr",
        "sdnn_ms": "sdnn",
    })

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "hrv_series", "n": len(rows), "data": rows}

def _select_training_sets(q: Dict[str, Any]) -> Dict[str, Any]:
    """
    Uses training.sqlite3 tables:
      - sets (assumed)
      - workouts (assumed)
      - exercises (assumed)
    If your schema differs, adjust the SQL here.
    """
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 500, 2000)

    group_by = q.get("group_by") or []
    metrics = q.get("metrics") or []
    select = q.get("select") or []

    conn = _connect(TRAINING_DB)
    cur = conn.cursor()

    # Base view: join sets -> exercises -> workouts to get date/workout_name and exercise meta
    base_sql = """
    SELECT
      w.date_iso AS date_iso,
      strftime('%Y-%W', date(w.date_iso)) AS week,
      w.name AS workout_name,

      e.name AS exercise,
      e.variation AS variation,
      e.device AS device,
      e.laterality AS laterality,

      s.set_number AS set_number,
      s.reps AS reps,
      s.weight AS weight,
      s.rpe AS rpe,

      CASE
        WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL AND s.reps > 0
        THEN (s.weight * (1.0 + (s.reps / 30.0)))
        ELSE NULL
      END AS e1rm,

      (COALESCE(s.weight, 0) * COALESCE(s.reps, 0)) AS tonnage
    FROM sets s
    JOIN exercises e ON e.id = s.exercise_id
    JOIN workouts w ON w.id = e.workout_id
    """

    where = []
    params: List[Any] = []
    _apply_time_filters(where, params, time_obj, "w.date_iso")

    _apply_filters(where, params, q.get("filters") or [], {
        "date_iso": "w.date_iso",
        "week": "strftime('%Y-%W', date(w.date_iso))",
        "workout_name": "w.name",
        "exercise": "e.name",
        "variation": "e.variation",
        "device": "e.device",
        "laterality": "e.laterality",
    })

    sql = base_sql
    if where:
        sql += " WHERE " + " AND ".join(where)

    # Aggregation
    if group_by and metrics:
        # Build SELECT: group_by cols + metrics
        gb_cols = []
        for g in group_by:
            if g in ("week","exercise","device","variation","laterality","workout_name","date_iso"):
                gb_cols.append(g)

        if not gb_cols:
            return jsonify({"ok": False, "error": "Invalid group_by"}), 400

        metric_sql = []
        for m in metrics:
            name = (m.get("name") or "").strip()
            op = (m.get("op") or "").strip()
            field = (m.get("field") or "").strip()

            if op == "count":
                metric_sql.append(f"COUNT(*) AS {name or 'count'}")
            elif op in ("sum","avg") and field in ("tonnage","e1rm","rpe","weight","reps"):
                func = "SUM" if op == "sum" else "AVG"
                metric_sql.append(f"{func}({field}) AS {name or (op+'_'+field)}")

        if not metric_sql:
            return jsonify({"ok": False, "error": "Invalid metrics"}), 400

        agg_sql = "SELECT " + ", ".join(gb_cols + metric_sql) + " FROM (" + sql + ") t"
        agg_sql += " GROUP BY " + ", ".join(gb_cols)

        # default order
        if "week" in gb_cols:
            agg_sql += " ORDER BY week ASC"
        else:
            agg_sql += " ORDER BY 1 ASC"

        agg_sql += " LIMIT ?"
        rows = [dict(r) for r in cur.execute(agg_sql, params + [limit]).fetchall()]
        conn.close()
        return {"ok": True, "dataset": "training_sets", "n": len(rows), "data": rows}

    # Non-aggregated select
    if not select:
        select = ["date_iso","week","workout_name","exercise","variation","device","laterality","set_number","reps","weight","rpe","e1rm","tonnage"]

    allowed_cols = {"date_iso","week","workout_name","exercise","variation","device","laterality","set_number","reps","weight","rpe","e1rm","tonnage"}
    select_cols = [c for c in select if c in allowed_cols]
    if not select_cols:
        select_cols = ["date_iso","week","workout_name","exercise","device","reps","weight","rpe","e1rm","tonnage"]

    final_sql = "SELECT " + ", ".join(select_cols) + " FROM (" + sql + ") t ORDER BY date_iso DESC LIMIT ?"
    rows = [dict(r) for r in cur.execute(final_sql, params + [limit]).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "training_sets", "n": len(rows), "data": rows}

def _select_sessions(q: Dict[str, Any]) -> Dict[str, Any]:
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 200, 2000)

    group_by = q.get("group_by") or []
    metrics = q.get("metrics") or []
    select = q.get("select") or []

    conn = _connect(TRAINING_DB)
    cur = conn.cursor()

    base_sql = """
    SELECT
      w.id AS workout_id,
      w.date_iso AS date_iso,
      strftime('%Y-%W', date(w.date_iso)) AS week,
      w.name AS workout_name,
      COUNT(DISTINCT e.id) AS n_exercises,
      COUNT(s.id) AS n_sets,
      SUM(CASE WHEN s.reps IS NOT NULL THEN s.reps ELSE 0 END) AS total_reps,
      SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN (s.weight * s.reps) END) AS total_tonnage,
      AVG(s.rpe) AS avg_rpe,
      AVG(CASE
        WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL AND s.reps > 0
        THEN (s.weight * (1.0 + (s.reps / 30.0)))
        ELSE NULL
      END) AS avg_e1rm
    FROM workouts w
    LEFT JOIN exercises e ON e.workout_id = w.id
    LEFT JOIN sets s ON s.exercise_id = e.id
    """

    where = []
    params: List[Any] = []
    _apply_time_filters(where, params, time_obj, "w.date_iso")

    _apply_filters(where, params, q.get("filters") or [], {
        "workout_id": "w.id",
        "date_iso": "w.date_iso",
        "week": "strftime('%Y-%W', date(w.date_iso))",
        "workout_name": "w.name",
    })

    sql = base_sql
    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " GROUP BY w.id"

    if group_by and metrics:
        gb_cols = []
        for g in group_by:
            if g in ("week", "workout_name", "date_iso"):
                gb_cols.append(g)

        if not gb_cols:
            return jsonify({"ok": False, "error": "Invalid group_by"}), 400

        metric_sql = []
        for m in metrics:
            name = (m.get("name") or "").strip()
            op = (m.get("op") or "").strip()
            field = (m.get("field") or "").strip()

            if op == "count":
                metric_sql.append(f"COUNT(*) AS {name or 'count_sessions'}")
            elif op in ("sum", "avg") and field in ("total_tonnage", "avg_rpe", "n_sets", "total_reps", "avg_e1rm"):
                func = "SUM" if op == "sum" else "AVG"
                metric_sql.append(f"{func}({field}) AS {name or (op + '_' + field)}")

        if not metric_sql:
            return jsonify({"ok": False, "error": "Invalid metrics"}), 400

        agg_sql = "SELECT " + ", ".join(gb_cols + metric_sql) + " FROM (" + sql + ") t"
        agg_sql += " GROUP BY " + ", ".join(gb_cols)

        if "week" in gb_cols:
            agg_sql += " ORDER BY week ASC"
        else:
            agg_sql += " ORDER BY 1 ASC"
        agg_sql += " LIMIT ?"

        rows = [dict(r) for r in cur.execute(agg_sql, params + [limit]).fetchall()]
        conn.close()
        return {"ok": True, "dataset": "sessions", "n": len(rows), "data": rows}

    allowed_cols = {
        "workout_id", "date_iso", "week", "workout_name",
        "n_exercises", "n_sets", "total_reps", "total_tonnage",
        "avg_rpe", "avg_e1rm",
    }
    if select:
        select_cols = [c for c in select if c in allowed_cols]
        if not select_cols:
            select_cols = ["workout_id", "date_iso", "week", "workout_name", "n_exercises", "n_sets", "total_reps", "total_tonnage", "avg_rpe", "avg_e1rm"]
    else:
        select_cols = ["workout_id", "date_iso", "week", "workout_name", "n_exercises", "n_sets", "total_reps", "total_tonnage", "avg_rpe", "avg_e1rm"]

    final_sql = "SELECT " + ", ".join(select_cols) + " FROM (" + sql + ") t ORDER BY date_iso DESC LIMIT ?"
    rows = [dict(r) for r in cur.execute(final_sql, params + [limit]).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "sessions", "n": len(rows), "data": rows}

def _select_weight_logs(q: Dict[str, Any]) -> Dict[str, Any]:
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 200, 2000)

    group_by = q.get("group_by") or []
    metrics = q.get("metrics") or []
    select = q.get("select") or []

    conn = _connect(ERNAEHRUNG_DB)
    cur = conn.cursor()

    # Tabelle existiert?
    t = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='weight_logs'"
    ).fetchone()
    if not t:
        conn.close()
        return {"ok": True, "dataset": "weight_logs", "n": 0, "data": []}

    where = []
    params: List[Any] = []

    _apply_time_filters(where, params, time_obj, "date_iso")

    allowed_fields = {"date_iso","weight_kg","kcal","protein","carbs","fat","sugar"}
    _apply_filters(where, params, q.get("filters") or [], {field: field for field in allowed_fields})
    def gb_expr(field: str) -> tuple[str, str]:
        if field == "week":
            return "strftime('%Y-%W', date(date_iso))", "week"
        if field == "month":
            return "substr(date_iso,1,7)", "month"
        if field in allowed_fields:
            return field, field
        return "", ""

    # Aggregation mode (group_by + metrics)
    if group_by and metrics:
        gb_sql = []
        gb_select = []
        for g in group_by:
            expr, alias = gb_expr(str(g))
            if not expr:
                continue
            gb_sql.append(expr)
            gb_select.append(f"{expr} AS {alias}")

        # metrics whitelist
        metric_sql = []
        for m in metrics:
            op = str(m.get("op") or "").lower()
            field = str(m.get("field") or "")
            name = str(m.get("name") or f"{op}_{field}").strip()

            if op not in {"avg","sum","min","max","count"}:
                continue
            if op != "count" and field not in allowed_fields:
                continue

            if op == "count":
                metric_sql.append(f"COUNT(*) AS {name}")
            else:
                metric_sql.append(f"{op.upper()}({field}) AS {name}")

        if not gb_select or not metric_sql:
            # fallback raw if request is invalid
            group_by = []
        else:
            sql = "SELECT " + ", ".join(gb_select + metric_sql) + " FROM weight_logs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " GROUP BY " + ", ".join(gb_sql)
            sql += " ORDER BY " + gb_sql[0] + " DESC"
            sql += " LIMIT ?"
            params2 = params[:] + [_clamp_limit(q.get("limit"), 2000, 2000)]
            rows = [dict(r) for r in cur.execute(sql, params2).fetchall()]
            conn.close()
            return {"ok": True, "dataset": "weight_logs", "n": len(rows), "data": rows}

    # Raw mode
    if select:
        cols = [c for c in select if c in allowed_fields]
        if not cols:
            cols = ["date_iso","weight_kg","kcal","protein","carbs","fat","sugar"]
    else:
        cols = ["date_iso","weight_kg","kcal","protein","carbs","fat","sugar"]

    sql = "SELECT " + ", ".join(cols) + " FROM weight_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date_iso DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "weight_logs", "n": len(rows), "data": rows}

def _safe_load_plan(raw: Optional[str]) -> Dict[str, Any]:
    try:
        plan = json.loads(raw) if raw else {}
    except Exception:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    return plan

def _normalize_plan_data(plan: Dict[str, Any], *, fallback_name: str, fallback_block: int) -> Dict[str, Any]:
    plan = dict(plan) if isinstance(plan, dict) else {}
    plan.setdefault("name", fallback_name)
    plan.setdefault("block_length", fallback_block)
    plan.setdefault("meta", {"goal": "", "priorities": []})
    plan.setdefault("base_week", [])
    plan.setdefault("weeks", [])

    if not isinstance(plan["base_week"], list):
        plan["base_week"] = []
    if not isinstance(plan["weeks"], list):
        plan["weeks"] = []

    for d in plan["base_week"]:
        if not isinstance(d, dict):
            continue
        d.setdefault("day", "")
        d.setdefault("session_name", "")
        d.setdefault("strength_exercises", [])
        d.setdefault("run_sessions", [])

        if not isinstance(d["strength_exercises"], list):
            d["strength_exercises"] = []
        if not isinstance(d["run_sessions"], list):
            d["run_sessions"] = []

        for ex in d["strength_exercises"]:
            if not isinstance(ex, dict):
                continue
            ex.setdefault("exercise_name", "")
            ex.setdefault("device", "")
            ex.setdefault("variation", "")
            ex.setdefault("sets", 0)
            ex.setdefault("reps_min", 0)
            ex.setdefault("reps_max", 0)
            ex.setdefault("rpe_min", 0)
            ex.setdefault("rpe_max", 0)
            ex.setdefault("notes", "")

        for run in d["run_sessions"]:
            if not isinstance(run, dict):
                continue
            run.setdefault("run_type", "")
            run.setdefault("amount_value", "")
            run.setdefault("amount_unit", "")
            run.setdefault("pace", "")
            run.setdefault("interval_reps", "")
            run.setdefault("interval_on", "")
            run.setdefault("interval_on_unit", "min")
            run.setdefault("interval_off", "")
            run.setdefault("interval_off_unit", "min")
            run.setdefault("notes", "")

    if len(plan["weeks"]) == 0:
        for i in range(1, fallback_block + 1):
            plan["weeks"].append({
                "week": i,
                "strength_volume_factor": 1.0,
                "run_volume_factor": 1.0,
                "deload": False,
                "rpe_cap": None,
                "week_note": "",
            })
    else:
        for i, w in enumerate(plan["weeks"], start=1):
            if not isinstance(w, dict):
                continue
            w.setdefault("week", i)
            w.setdefault("strength_volume_factor", 1.0)
            w.setdefault("run_volume_factor", 1.0)
            w.setdefault("deload", False)
            w.setdefault("rpe_cap", None)
            w.setdefault("week_note", "")

    return plan

def _summarize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    base_week = plan.get("base_week") or []
    weeks = plan.get("weeks") or []
    if not isinstance(base_week, list):
        base_week = []
    if not isinstance(weeks, list):
        weeks = []

    strength_days = 0
    run_days = 0
    for d in base_week:
        if not isinstance(d, dict):
            continue
        se = d.get("strength_exercises") or []
        rs = d.get("run_sessions") or []
        if isinstance(se, list) and se:
            strength_days += 1
        if isinstance(rs, list) and rs:
            run_days += 1

    return {
        "days": len(base_week),
        "weeks": len(weeks),
        "strength_days": strength_days,
        "run_days": run_days,
    }

def _format_range(min_val: Any, max_val: Any) -> str:
    if min_val in (None, "") and max_val in (None, ""):
        return ""
    if min_val in (None, ""):
        return str(max_val)
    if max_val in (None, ""):
        return str(min_val)
    return f"{min_val}-{max_val}"

def _build_plan_detail(plan: Dict[str, Any], row: sqlite3.Row) -> Dict[str, Any]:
    block_length = int(row["block_length"] or plan.get("block_length") or 0)
    meta = plan.get("meta") or {}
    base_week = plan.get("base_week") or []
    weeks_meta = {int(w.get("week") or 0): w for w in (plan.get("weeks") or []) if isinstance(w, dict)}

    weeks_out = []
    for i in range(1, block_length + 1):
        wmeta = weeks_meta.get(i, {})
        days = []
        for d_idx, day in enumerate(base_week, start=1):
            if not isinstance(day, dict):
                continue
            items = []
            for ex in (day.get("strength_exercises") or []):
                if not isinstance(ex, dict):
                    continue
                items.append({
                    "exercise": ex.get("exercise_name") or "",
                    "variation": ex.get("variation") or "",
                    "device": ex.get("device") or "",
                    "sets": ex.get("sets") or 0,
                    "reps_range": _format_range(ex.get("reps_min"), ex.get("reps_max")),
                    "rpe_target": _format_range(ex.get("rpe_min"), ex.get("rpe_max")),
                    "notes": ex.get("notes") or "",
                    "progression_rules": ex.get("progression_rules") or "",
                })
            days.append({
                "day_index": d_idx,
                "day_name": day.get("day") or "",
                "session_name": day.get("session_name") or "",
                "items": items,
                "run_sessions": day.get("run_sessions") or [],
            })
        weeks_out.append({
            "week_index": i,
            "strength_volume_factor": wmeta.get("strength_volume_factor"),
            "run_volume_factor": wmeta.get("run_volume_factor"),
            "deload": wmeta.get("deload"),
            "rpe_cap": wmeta.get("rpe_cap"),
            "week_note": wmeta.get("week_note") or "",
            "days": days,
        })

    return {
        "id": row["id"],
        "name": row["name"],
        "goal": (meta.get("goal") or ""),
        "block_length": block_length,
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "weeks": weeks_out,
    }

def _select_plans(q: Dict[str, Any]) -> Dict[str, Any]:
    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 20, 50)

    conn = _connect(PLANS_DB)
    cur = conn.cursor()

    where = []
    params: List[Any] = []

    _apply_time_filters(where, params, time_obj, "updated_at")
    _apply_filters(where, params, q.get("filters") or [], {
        "id": "id",
        "name": "name",
        "block_length": "block_length",
        "is_active": "is_active",
        "updated_at": "updated_at",
        "created_at": "created_at",
    })

    sql = "SELECT id, name, block_length, is_active, data, updated_at, created_at FROM plans"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    rows = []
    for r in cur.execute(sql, params).fetchall():
        plan = _safe_load_plan(r["data"])
        plan = _normalize_plan_data(plan, fallback_name=r["name"], fallback_block=int(r["block_length"] or 4))
        rows.append({
            "id": r["id"],
            "name": r["name"],
            "block_length": int(r["block_length"] or plan.get("block_length") or 4),
            "is_active": int(r["is_active"] or 0),
            "summary": _summarize_plan(plan),
            "updated_at": r["updated_at"],
            "created_at": r["created_at"],
            "plan": plan,
        })

    conn.close()
    return {"ok": True, "dataset": "plans", "n": len(rows), "data": rows}


def _select_plans_list(q: Dict[str, Any]) -> Dict[str, Any]:
    conn = _connect(PLANS_DB)
    cur = conn.cursor()

    time_obj = q.get("time") or {}
    limit = _clamp_limit(q.get("limit"), 200, 1000)

    where = []
    params: List[Any] = []
    _apply_time_filters(where, params, time_obj, "updated_at")
    _apply_filters(where, params, q.get("filters") or [], {
        "id": "id",
        "name": "name",
        "block_length": "block_length",
        "is_active": "is_active",
        "updated_at": "updated_at",
        "created_at": "created_at",
    })

    sql = "SELECT id, name, block_length, is_active, updated_at, created_at FROM plans"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return {"ok": True, "dataset": "plans_list", "n": len(rows), "data": rows}


@ai_api.get("/hrv/latest")
@require_ai_read
def ai_hrv_latest():
    try:
        latest = _fetch_hrv_latest()
    except Exception as e:
        return jsonify({"ok": False, "error": "hrv_read_failed", "detail": str(e)}), 500
    return jsonify({"ok": True, "data": _hrv_public_row(latest)})

@ai_api.route("/hrv/series", methods=["GET"], strict_slashes=False)
@require_ai_read
def ai_hrv_series():
    days_raw = (request.args.get("days") or "").strip().lower()
    limit_raw = (request.args.get("limit") or "").strip()

    if days_raw in {"all", "0"}:
        range_days = None
        days_out: Any = 0
    else:
        range_days = _clamp_limit(days_raw or 30, 30, 3650)
        days_out = range_days

    limit = 0
    if limit_raw:
        limit = _clamp_limit(limit_raw, 1, 5000)

    try:
        rows = _fetch_hrv_rows(limit=0)
    except Exception as e:
        return jsonify({"ok": False, "error": "hrv_read_failed", "detail": str(e)}), 500

    if not rows:
        return jsonify({"ok": True, "days": days_out, "count": 0, "series": []})

    latest = _latest_hrv_row(rows)
    range_rows = _apply_hrv_range(rows, latest.get("_date_only") if latest else None, range_days)
    range_rows = sorted(range_rows, key=lambda r: r.get("ts_measurement") or "")
    data = [_hrv_series_row(r) for r in range_rows]
    if range_days is None:
        unique_days = {r.get("_date_only") for r in range_rows if r.get("_date_only")}
        days_out = len(unique_days)
    if limit and len(data) > limit:
        data = data[-limit:]

    return jsonify({"ok": True, "days": days_out, "n": len(data), "data": data})


@ai_api.get("/hrv/series_compact")
@require_ai_read
def ai_hrv_series_compact():
    days_raw = (request.args.get("days") or "30").strip().lower()
    limit_raw = (request.args.get("limit") or "").strip()
    if days_raw in {"all", "0"}:
        range_days = None
        days_out: Any = 0
        limit = 0
    else:
        range_days = _clamp_limit(days_raw, 30, 3650)
        days_out = range_days
        limit = 5000
    if limit_raw:
        limit = _clamp_limit(limit_raw, 10, 5000)

    try:
        rows = _fetch_hrv_rows(limit=limit)
    except Exception as e:
        return jsonify({"ok": False, "error": "hrv_read_failed", "detail": str(e)}), 500

    if not rows:
        return jsonify({"ok": True, "days": days_out, "n": 0, "data": []})

    latest = _latest_hrv_row(rows)
    range_rows = _apply_hrv_range(rows, latest.get("_date_only") if latest else None, range_days)
    range_rows = sorted(range_rows, key=lambda r: r.get("ts_measurement") or "")
    data = [
        {
            "ts_measurement": r.get("ts_measurement"),
            "date_utc": r.get("date_utc"),
            "rmssd": r.get("_rmssd") if r.get("_rmssd") is not None else r.get("rmssd"),
            "hr": r.get("_hr") if r.get("_hr") is not None else r.get("hr"),
        }
        for r in range_rows
    ]
    if days_raw == "all":
        unique_days = {r.get("_date_only") for r in range_rows if r.get("_date_only")}
        days_out = len(unique_days)

    payload = {"ok": True, "days": days_out, "n": len(data), "data": data}
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
    max_bytes = 1200
    while size > max_bytes and len(data) > 1:
        data.pop(0)
        payload = {"ok": True, "days": days_out, "n": len(data), "data": data}
        size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))

    return jsonify(payload)

@ai_api.route("/hrv/summary", methods=["GET"], strict_slashes=False)
@require_ai_read
def ai_hrv_summary():
    try:
        payload = _build_hrv_summary_payload(
            request.args.get("range_days"),
            request.args.get("baseline_days"),
            request.args.get("trend_days"),
        )
        return jsonify(payload)
    except Exception as exc:
        range_raw = (request.args.get("range_days") or "30").strip().lower()
        if range_raw == "all":
            range_out = "all"
        else:
            try:
                range_out = max(1, min(3650, int(range_raw)))
            except Exception:
                range_out = 30

        try:
            baseline_days = max(1, min(365, int(request.args.get("baseline_days") or 28)))
        except Exception:
            baseline_days = 28

        try:
            trend_days = max(2, min(365, int(request.args.get("trend_days") or 7)))
        except Exception:
            trend_days = 7

        fallback = {
            "ok": False,
            "error": "hrv_read_failed",
            "detail": str(exc),
            "baseline_days": baseline_days,
            "range_days": range_out,
            "trend_days": trend_days,
            "counts": {"total": 0, "range": 0},
            "latest": None,
            "baseline": {
                "days": baseline_days,
                "count": 0,
                "rmssd": {"mean": None, "sd": None},
                "hr": {"mean": None, "sd": None},
                "sdnn": {"mean": None, "sd": None},
            },
            "readiness": {
                "score": 50,
                "status": "neutral",
                "warning": "Zu wenig Daten fuer Readiness.",
                "z_rmssd": None,
                "z_hr": None,
                "z_total": None,
            },
            "trends": {
                "window_days": trend_days,
                "rmssd": {"recent_mean": None, "previous_mean": None, "diff": None, "pct_change": None, "direction": "flat", "status": "neutral"},
                "hr": {"recent_mean": None, "previous_mean": None, "diff": None, "pct_change": None, "direction": "flat", "status": "neutral"},
                "sdnn": {"recent_mean": None, "previous_mean": None, "diff": None, "pct_change": None, "direction": "flat", "status": "neutral"},
            },
        }
        return jsonify(fallback)


@ai_api.get("/plans/<int:plan_id>")
@require_ai_read
def ai_get_plan(plan_id: int):
    conn = _connect(PLANS_DB)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, name, block_length, is_active, data, updated_at, created_at FROM plans WHERE id=?",
        (plan_id,),
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    plan = _safe_load_plan(row["data"])
    plan = _normalize_plan_data(plan, fallback_name=row["name"], fallback_block=int(row["block_length"] or 4))
    detail = _build_plan_detail(plan, row)
    return jsonify({"ok": True, "plan": detail})

@ai_api.route("/query", methods=["GET", "POST"])
@require_ai_read
def query():

    try:
        q = _parse_q()
    except Exception as e:
        return jsonify({"ok": False, "error": "Invalid query JSON", "detail": str(e)}), 400

    dataset_raw = (q.get("dataset") or "").strip()
    dataset = dataset_raw.lower()
    if not dataset:
        return jsonify({"ok": False, "error": "Missing dataset"}), 400

    def _as_response(result):
        if isinstance(result, tuple):
            return result
        return jsonify(result)

    try:
        if dataset == "runs":
            return _as_response(_select_runs(q))
        if dataset in ("hrv_series", "hrvseries"):
            return _as_response(_select_hrv_series(q))
        if dataset == "training_sets":
            return _as_response(_select_training_sets(q))
        if dataset == "weight_logs":
            return _as_response(_select_weight_logs(q))
        if dataset == "sessions":
            return _as_response(_select_sessions(q))
        if dataset == "plans_list":
            return _as_response(_select_plans_list(q))
        if dataset == "plans":
            return _as_response(_select_plans(q))
        return jsonify({"ok": False, "error": f"Unknown dataset: {dataset}"}), 400
    except Exception as e:
        # IMPORTANT: never throw HTML back to Actions, always JSON
        return jsonify({"ok": False, "error": "Query failed", "detail": str(e)}), 500
