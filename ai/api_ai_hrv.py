from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request

from database import connections
from security.write_guard import require_ai_read
from ai.api_ai_common import parse_limit, parse_time_range, add_pagination_meta, parse_cursor, parse_fields, apply_fields


HRV_BASE_COLUMNS = [
    "id",
    "ts_measurement",
    "date_utc",
    "hr",
    "rmssd",
    "sdnn",
    "avnn",
    "signal_quality",
    "source_file",
    "created_at",
]
HRV_OPTIONAL_COLUMNS = [
    "training_motivation",
    "fatigue",
    "sickness",
    "sleep_quality",
    "alcohol",
    "sickness_bool",
    "alcohol_bool",
    "sickness_yn",
    "alcohol_yn",
]
SERIES_FIELDS = [
    "date",
    "date_iso",
    "date_de",
    "rmssd_ms",
    "hr_bpm",
    "sdnn_ms",
    *HRV_BASE_COLUMNS,
    *HRV_OPTIONAL_COLUMNS,
]


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


def _normalize_row(row) -> Dict[str, Any]:
    data = dict(row)
    ts_dt = _parse_hrv_timestamp(data.get("ts_measurement"))
    date_dt = _parse_hrv_timestamp(data.get("date_utc"))
    base_dt = date_dt or ts_dt
    data["_ts_dt"] = ts_dt
    data["_date_dt"] = date_dt
    data["_date_only"] = base_dt.date() if base_dt else None
    return data


def _hrv_available_columns(conn) -> set[str]:
    cur = conn.cursor()
    rows = cur.execute("PRAGMA table_info(hrv_measurements)").fetchall()
    return {str(r["name"]) for r in rows}


def _hrv_select_columns(conn, include_series_aliases: bool = False) -> List[str]:
    available = _hrv_available_columns(conn)
    select_cols = [
        c
        for c in (HRV_BASE_COLUMNS + HRV_OPTIONAL_COLUMNS)
        if c in available and c not in {"sickness_bool", "sickness_yn", "alcohol_yn"}
    ]
    if "sickness" in available:
        select_cols.append("CASE WHEN COALESCE(sickness, 0) = 1 THEN 1 ELSE 0 END AS sickness_bool")
        select_cols.append("CASE WHEN COALESCE(sickness, 0) = 1 THEN 1 ELSE 0 END AS sickness_yn")
    elif "sickness_bool" in available:
        select_cols.append("CASE WHEN COALESCE(sickness_bool, 0) = 1 THEN 1 ELSE 0 END AS sickness_bool")
        select_cols.append("CASE WHEN COALESCE(sickness_bool, 0) = 1 THEN 1 ELSE 0 END AS sickness_yn")
    if "alcohol_bool" in available:
        select_cols.append("CASE WHEN COALESCE(alcohol_bool, 0) = 1 THEN 1 ELSE 0 END AS alcohol_yn")
    if include_series_aliases:
        date_iso_expr = (
            "COALESCE("
            "CASE WHEN date_utc IS NOT NULL AND length(date_utc) >= 10 THEN substr(date_utc,1,10) END, "
            "CASE WHEN ts_measurement IS NOT NULL AND length(ts_measurement) >= 10 THEN substr(ts_measurement,1,10) END"
            ")"
        )
        select_cols.append(f"{date_iso_expr} AS date_iso")
        select_cols.append(f"{date_iso_expr} AS date_de")
        select_cols.append(
            "CASE "
            "WHEN length(" + date_iso_expr + ") = 10 "
            "THEN substr(" + date_iso_expr + ",9,2) || '.' || substr(" + date_iso_expr + ",6,2) || '.' || substr(" + date_iso_expr + ",1,4) "
            "ELSE " + date_iso_expr + " END AS date"
        )
        if "rmssd" in available:
            select_cols.append("rmssd AS rmssd_ms")
        if "hr" in available:
            select_cols.append("hr AS hr_bpm")
        if "sdnn" in available:
            select_cols.append("sdnn AS sdnn_ms")
    return select_cols


def _fetch_hrv_rows(limit: int = 5000) -> List[Dict[str, Any]]:
    conn = connections.get_hrv_db()
    cur = conn.cursor()
    cols = _hrv_select_columns(conn, include_series_aliases=False)
    if not cols:
        conn.close()
        return []
    sql = "SELECT " + ", ".join(cols) + " FROM hrv_measurements ORDER BY ts_measurement ASC"
    rows = cur.execute(sql + " LIMIT ?", (limit,)).fetchall() if limit else cur.execute(sql).fetchall()
    conn.close()
    return [_normalize_row(r) for r in rows]


def _fetch_latest() -> Optional[Dict[str, Any]]:
    conn = connections.get_hrv_db()
    cur = conn.cursor()
    cols = _hrv_select_columns(conn, include_series_aliases=False)
    if not cols:
        conn.close()
        return None
    row = cur.execute(
        "SELECT " + ", ".join(cols) + " FROM hrv_measurements ORDER BY ts_measurement DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return _normalize_row(row) if row else None


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
    return {"mean": mean, "sd": variance ** 0.5}


def _compute_baseline(rows: List[Dict[str, Any]], latest_date: Optional[date], baseline_days: int) -> Dict[str, Any]:
    if not latest_date:
        return {"days": baseline_days, "count": 0, "rmssd": {"mean": None, "sd": None}, "hr": {"mean": None, "sd": None}, "sdnn": {"mean": None, "sd": None}}
    start = latest_date - timedelta(days=baseline_days)
    baseline_rows = [r for r in rows if r.get("_date_only") and start <= r["_date_only"] < latest_date]
    rmssd_vals = [r.get("rmssd") for r in baseline_rows if r.get("rmssd") is not None]
    hr_vals = [r.get("hr") for r in baseline_rows if r.get("hr") is not None]
    sdnn_vals = [r.get("sdnn") for r in baseline_rows if r.get("sdnn") is not None]
    return {
        "days": baseline_days,
        "count": len(baseline_rows),
        "rmssd": _compute_stats(rmssd_vals),
        "hr": _compute_stats(hr_vals),
        "sdnn": _compute_stats(sdnn_vals),
    }


def _compute_readiness(latest_row: Optional[Dict[str, Any]], baseline: Dict[str, Any]) -> Dict[str, Any]:
    if not latest_row:
        return {"score": 50, "status": "neutral", "warning": "Keine Messung vorhanden."}
    if baseline.get("count", 0) < 3 or not baseline["rmssd"]["sd"] or not baseline["hr"]["sd"]:
        return {"score": 50, "status": "neutral", "warning": "Zu wenig Daten fuer eine stabile Baseline."}
    if latest_row.get("rmssd") is None or latest_row.get("hr") is None:
        return {"score": 50, "status": "neutral", "warning": "Zu wenig Daten fuer Readiness."}

    z_rmssd = (latest_row["rmssd"] - baseline["rmssd"]["mean"]) / baseline["rmssd"]["sd"]
    z_hr = (baseline["hr"]["mean"] - latest_row["hr"]) / baseline["hr"]["sd"]
    z_total = 0.7 * z_rmssd + 0.3 * z_hr
    score = max(0, min(100, 50 + 18 * z_total))

    status = "yellow"
    if score >= 65:
        status = "green"
    elif score < 45:
        status = "red"

    return {"score": score, "status": status, "warning": "", "z_rmssd": z_rmssd, "z_hr": z_hr, "z_total": z_total}


def register(ai_api):
    @ai_api.get("/hrv/latest")
    @require_ai_read
    def hrv_latest():
        row = _fetch_latest()
        if not row:
            return jsonify({"ok": True, "data": None})
        payload = {k: row.get(k) for k in (HRV_BASE_COLUMNS + HRV_OPTIONAL_COLUMNS)}
        return jsonify({"ok": True, "data": payload})

    @ai_api.get("/hrv/series")
    @require_ai_read
    def hrv_series():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=500, max_limit=5000)
        offset = parse_cursor(request.args.get("cursor"))
        fields = parse_fields(request.args.get("fields"), SERIES_FIELDS)

        conn = connections.get_hrv_db()
        cur = conn.cursor()
        date_expr = (
            "COALESCE("
            "CASE WHEN date_utc IS NOT NULL AND length(date_utc) >= 10 THEN substr(date_utc,1,10) END, "
            "CASE WHEN ts_measurement IS NOT NULL AND length(ts_measurement) >= 10 THEN substr(ts_measurement,1,10) END"
            ")"
        )
        cols = _hrv_select_columns(conn, include_series_aliases=True)
        if not cols:
            conn.close()
            meta = {"from": date_from, "to": date_to, "last": last_token}
            meta = add_pagination_meta(meta, offset, limit, 0)
            return jsonify({"ok": True, "data": [], "meta": meta})
        sql = "SELECT " + ", ".join(cols) + " FROM hrv_measurements"
        where = []
        params: List[Any] = []
        if date_from:
            where.append(f"{date_expr} >= ?")
            params.append(date_from)
        if date_to:
            where.append(f"{date_expr} <= ?")
            params.append(date_to)
        if where:
            sql += " WHERE " + " AND ".join(where)
        order_cols = ["date_iso DESC"]
        if "ts_measurement" in cols:
            order_cols.append("ts_measurement DESC")
        if "id" in cols:
            order_cols.append("id DESC")
        sql += " ORDER BY " + ", ".join(order_cols) + " LIMIT ? OFFSET ?"
        params.extend([limit + 1, offset])
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()

        trimmed = rows[:limit]
        data = apply_fields(trimmed, fields)
        meta = {"from": date_from, "to": date_to, "last": last_token}
        meta = add_pagination_meta(meta, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": data, "meta": meta})

    @ai_api.get("/hrv/summary")
    @require_ai_read
    def hrv_summary():
        range_raw = request.args.get("last") or request.args.get("range_days") or "30"
        baseline_raw = request.args.get("baseline_days") or "28"
        trend_raw = request.args.get("trend_days") or "7"

        range_text = str(range_raw or "30").strip().lower()
        if range_text == "all":
            range_days = None
            limit = 0
            range_out: Any = "all"
        else:
            try:
                range_days = max(1, min(3650, int(range_text)))
            except Exception:
                range_days = 30
            range_out = range_days
            limit = 5000

        baseline_days = max(1, min(365, int(baseline_raw)))
        trend_days = max(2, min(365, int(trend_raw)))

        rows = _fetch_hrv_rows(limit=limit)
        latest = _latest_hrv_row(rows)
        latest_date = latest.get("_date_only") if latest else None
        baseline = _compute_baseline(rows, latest_date, baseline_days)
        readiness = _compute_readiness(latest, baseline)
        range_rows = rows
        if latest_date and range_days:
            start = latest_date - timedelta(days=range_days - 1)
            range_rows = [r for r in rows if r.get("_date_only") and start <= r["_date_only"] <= latest_date]

        rmssd_values = [r.get("rmssd") for r in range_rows if r.get("rmssd") is not None]
        hr_values = [r.get("hr") for r in range_rows if r.get("hr") is not None]
        sdnn_values = [r.get("sdnn") for r in range_rows if r.get("sdnn") is not None]

        def _trend(values: List[float], window: int) -> Dict[str, Any]:
            if len(values) < window * 2:
                return {"recent_mean": None, "previous_mean": None, "diff": None, "pct_change": None}
            recent = values[-window:]
            prev = values[-window * 2:-window]
            recent_mean = sum(recent) / len(recent)
            prev_mean = sum(prev) / len(prev)
            diff = recent_mean - prev_mean
            pct = (diff / prev_mean * 100) if prev_mean else None
            return {"recent_mean": recent_mean, "previous_mean": prev_mean, "diff": diff, "pct_change": pct}

        trends = {
            "window_days": trend_days,
            "rmssd": _trend(rmssd_values, trend_days),
            "hr": _trend(hr_values, trend_days),
            "sdnn": _trend(sdnn_values, trend_days),
        }

        payload = {
            "ok": True,
            "baseline_days": baseline_days,
            "range_days": range_out,
            "trend_days": trend_days,
            "counts": {"total": len(rows), "range": len(range_rows)},
            "latest": {
                **({k: latest.get(k) for k in (HRV_BASE_COLUMNS + HRV_OPTIONAL_COLUMNS)} if latest else {k: None for k in (HRV_BASE_COLUMNS + HRV_OPTIONAL_COLUMNS)}),
            },
            "baseline": baseline,
            "readiness": readiness,
            "trends": trends,
        }
        return jsonify(payload)

    @ai_api.get("/hrv/diagnostics")
    @require_ai_read
    def hrv_diagnostics():
        conn = connections.get_hrv_db()
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) AS n FROM hrv_measurements").fetchone()["n"]
        last = cur.execute("SELECT MAX(ts_measurement) AS max_ts FROM hrv_measurements").fetchone()["max_ts"]
        conn.close()
        return jsonify({"ok": True, "data": {"total": total, "latest_ts": last}})

    @ai_api.get("/hrv/events/latest")
    @require_ai_read
    def hrv_events_latest():
        conn = connections.get_hrv_db()
        cur = conn.cursor()

        date_iso_expr = (
            "COALESCE("
            "CASE WHEN date_utc IS NOT NULL AND length(date_utc) >= 10 THEN substr(date_utc,1,10) END, "
            "CASE WHEN ts_measurement IS NOT NULL AND length(ts_measurement) >= 10 THEN substr(ts_measurement,1,10) END"
            ")"
        )
        date_de_expr = (
            "CASE "
            "WHEN length(" + date_iso_expr + ") = 10 "
            "THEN substr(" + date_iso_expr + ",9,2) || '.' || substr(" + date_iso_expr + ",6,2) || '.' || substr(" + date_iso_expr + ",1,4) "
            "ELSE " + date_iso_expr + " END"
        )

        sick = cur.execute(
            f"""
            SELECT
                {date_de_expr} AS date,
                {date_iso_expr} AS date_iso,
                ts_measurement,
                CASE WHEN COALESCE(sickness, 0) = 1 THEN 1 ELSE 0 END AS sickness_bool,
                sickness
            FROM hrv_measurements
            WHERE COALESCE(sickness, 0) = 1
            ORDER BY date_iso DESC, ts_measurement DESC
            LIMIT 1
            """
        ).fetchone()
        alcohol = cur.execute(
            f"""
            SELECT {date_de_expr} AS date, {date_iso_expr} AS date_iso, ts_measurement, alcohol_bool, alcohol
            FROM hrv_measurements
            WHERE COALESCE(alcohol_bool, 0) = 1
            ORDER BY date_iso DESC, ts_measurement DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()

        return jsonify(
            {
                "ok": True,
                "data": {
                    "latest_sickness": dict(sick) if sick else None,
                    "latest_alcohol": dict(alcohol) if alcohol else None,
                    "rule": "sickness_raw_alcohol_bool",
                },
            }
        )
