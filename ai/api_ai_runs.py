from __future__ import annotations

from typing import Any, Dict, List, Tuple

from flask import jsonify, request

from database import connections
from security.write_guard import require_ai_read
from ai.api_ai_common import (
    add_pagination_meta,
    apply_fields,
    parse_fields,
    parse_limit,
    parse_time_range,
    parse_cursor,
    build_date_where,
)


RUN_FIELDS = [
    "id",
    "date",
    "title",
    "distance",
    "distance_m",
    "moving_time",
    "moving_time_s",
    "avg_speed",
    "avg_hr",
    "max_hr",
    "elevation_gain",
    "elevation_gain_m",
    "pace",
    "avg_pace_s_per_km",
]


def _with_run_aliases(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row)
    payload["title"] = payload.get("title") or f"Run {payload.get('date') or payload.get('id')}"
    payload["distance_m"] = payload.get("distance")
    payload["moving_time_s"] = payload.get("moving_time")
    payload["elevation_gain_m"] = payload.get("elevation_gain")
    payload["avg_pace_s_per_km"] = payload.get("pace")
    return payload


def register(ai_api):
    @ai_api.get("/runs/latest")
    @require_ai_read
    def runs_latest():
        conn = connections.get_runs_db()
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace
            FROM runs
            ORDER BY date DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
        return jsonify({"ok": True, "data": _with_run_aliases(dict(row)) if row else None})

    @ai_api.get("/runs/list")
    @require_ai_read
    def runs_list():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        offset = parse_cursor(request.args.get("cursor"))
        fields = parse_fields(request.args.get("fields"), RUN_FIELDS)

        conn = connections.get_runs_db()
        cur = conn.cursor()
        where, params = build_date_where("date", date_from, date_to)
        sql = """
            SELECT id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace
            FROM runs
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date DESC LIMIT ? OFFSET ?"
        params.extend([limit + 1, offset])
        rows = [_with_run_aliases(dict(r)) for r in cur.execute(sql, params).fetchall()]
        conn.close()

        trimmed = rows[:limit]
        data = apply_fields(trimmed, fields)
        meta = {"from": date_from, "to": date_to, "last": last_token}
        meta = add_pagination_meta(meta, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": data, "meta": meta})

    @ai_api.get("/runs/run/<int:run_id>")
    @require_ai_read
    def runs_run(run_id: int):
        conn = connections.get_runs_db()
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace
            FROM runs
            WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error_code": "not_found", "message": "run not found", "data": None})
        return jsonify({"ok": True, "data": _with_run_aliases(dict(row))})

    @ai_api.get("/runs/summary_weekly")
    @require_ai_read
    def runs_summary_weekly():
        date_from, date_to, last_token = parse_time_range(request.args)
        conn = connections.get_runs_db()
        cur = conn.cursor()
        where, params = build_date_where("date", date_from, date_to)
        sql = """
            SELECT
                strftime('%Y-%W', date(date)) AS week,
                COUNT(*) AS n_runs,
                SUM(distance) AS distance,
                SUM(moving_time) AS moving_time,
                AVG(avg_hr) AS avg_hr
            FROM runs
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY week ORDER BY week DESC"
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "meta": {"from": date_from, "to": date_to, "last": last_token}})
