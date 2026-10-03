from __future__ import annotations

from typing import Any, Dict, List, Tuple

from flask import jsonify, request

from database import connections
from security.write_guard import require_ai_read
from ai.api_ai_common import (
    add_pagination_meta,
    apply_fields,
    build_date_where,
    parse_fields,
    parse_limit,
    parse_time_range,
    parse_cursor,
)


WORKOUT_FIELDS = [
    "id",
    "date_iso",
    "date",
    "name",
    "title",
    "n_exercises",
    "n_sets",
]

RAW_SET_FIELDS = [
    "set_id",
    "workout_id",
    "date_iso",
    "workout_name",
    "exercise",
    "variation",
    "device",
    "laterality",
    "set_number",
    "reps",
    "weight",
    "rpe",
]

PROGRESSION_OVERVIEW_FIELDS = [
    "exercise",
    "variation",
    "device",
    "laterality",
    "sessions",
    "progression_events",
    "latest_signal",
    "latest_signal_reason",
    "first_date",
    "latest_date",
    "best_date",
    "first_e1rm",
    "latest_e1rm",
    "best_e1rm",
    "delta_e1rm_vs_first",
    "delta_e1rm_vs_first_pct",
    "delta_e1rm_vs_previous",
    "delta_e1rm_vs_previous_pct",
    "delta_weight_vs_previous",
    "delta_reps_vs_previous",
    "delta_rpe_vs_previous",
]

PROGRESSION_SESSION_FIELDS = [
    "workout_id",
    "date",
    "date_iso",
    "workout_name",
    "exercise",
    "variation",
    "device",
    "laterality",
    "top_set_id",
    "top_set_number",
    "weight",
    "top_set_weight",
    "reps",
    "top_set_reps",
    "rpe",
    "top_set_rpe",
    "e1rm",
    "signal",
    "signal_reason",
    "delta_e1rm_vs_previous",
    "delta_e1rm_vs_previous_pct",
    "delta_weight_vs_previous",
    "delta_reps_vs_previous",
    "delta_rpe_vs_previous",
    "delta_e1rm_vs_best_prior",
    "is_all_time_pr",
]

PROGRESSION_MONTHLY_FIELDS = [
    "month",
    "exercise",
    "variation",
    "device",
    "laterality",
    "sessions",
    "progression_events",
    "regression_events",
    "stable_sessions",
    "first_date",
    "latest_date",
    "first_e1rm",
    "latest_e1rm",
    "best_e1rm",
    "delta_e1rm_in_month",
    "delta_e1rm_in_month_pct",
    "delta_e1rm_vs_prev_month",
    "delta_e1rm_vs_prev_month_pct",
]

_PROGRESSION_EPSILON = 0.0075


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _e1rm(weight: Any, reps: Any) -> float | None:
    w = _safe_float(weight)
    r = _safe_int(reps)
    if w is None or r is None or w <= 0 or r <= 0:
        return None
    return w * (1.0 + (r / 30.0))


def _pct_delta(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return ((current - baseline) / baseline) * 100.0


def _progress_signal(current: Dict[str, Any], previous: Dict[str, Any] | None) -> Tuple[str, str]:
    # Exercise-history compatibility signal, delegated to the shared V3 set
    # comparator. Session counts are supplied by analysis.training_progress.
    from analysis.progression_rules import SetPerformance, compare_set_progress, normalize_set_performance

    identity = {
        "exercise_name": current.get("name") or current.get("exercise_name") or "exercise",
        "canonical_exercise": current.get("canonical_exercise") or current.get("name") or current.get("exercise_name") or "exercise",
        "device": current.get("device") or "",
        "variation": current.get("variation") or "",
        "laterality": current.get("laterality") or "bilateral",
    }
    current_perf = normalize_set_performance({**current, **identity}) or SetPerformance(**identity)
    previous_perf = normalize_set_performance({**(previous or {}), **identity}) if previous else None
    comparison = compare_set_progress(current_perf, previous_perf, match_level="exercise_history_top_set")
    if not previous:
        return "seed", comparison.reason_code
    if comparison.status == "progress":
        return "progress", comparison.reason_code
    if comparison.status == "regress":
        return "regress", comparison.reason_code
    if comparison.status == "stable":
        return "stable", comparison.reason_code
    return "stable", comparison.reason_code


def _progression_where(
    date_from: str | None,
    date_to: str | None,
    exercise: str | None = None,
    variation: str | None = None,
    device: str | None = None,
    laterality: str | None = None,
) -> Tuple[List[str], List[Any]]:
    where, params = build_date_where("w.date_iso", date_from, date_to)
    if exercise:
        where.append("LOWER(TRIM(e.name)) = LOWER(TRIM(?))")
        params.append(exercise)
    if variation:
        where.append("LOWER(TRIM(COALESCE(e.variation, ''))) = LOWER(TRIM(?))")
        params.append(variation)
    if device:
        where.append("LOWER(TRIM(COALESCE(e.device, ''))) = LOWER(TRIM(?))")
        params.append(device)
    if laterality:
        where.append("LOWER(TRIM(COALESCE(e.laterality, ''))) = LOWER(TRIM(?))")
        params.append(laterality)
    return where, params


def _fetch_progression_rows(
    date_from: str | None,
    date_to: str | None,
    exercise: str | None = None,
    variation: str | None = None,
    device: str | None = None,
    laterality: str | None = None,
) -> List[Dict[str, Any]]:
    conn = connections.get_training_db()
    cur = conn.cursor()
    where, params = _progression_where(date_from, date_to, exercise, variation, device, laterality)
    sql = """
        SELECT
            s.id AS set_id,
            s.set_number AS set_number,
            s.weight AS weight,
            s.reps AS reps,
            s.rpe AS rpe,
            w.id AS workout_id,
            w.date_iso AS date_iso,
            w.name AS workout_name,
            e.name AS exercise,
            e.variation AS variation,
            e.device AS device,
            e.laterality AS laterality
        FROM sets s
        JOIN workouts w ON w.id = s.workout_id
        JOIN exercises e ON e.id = s.exercise_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY w.date_iso ASC, w.id ASC, s.id ASC"
    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _session_group_key(row: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(row.get("exercise") or ""),
        str(row.get("variation") or ""),
        str(row.get("device") or ""),
        str(row.get("laterality") or ""),
    )


def _session_sort_key(row: Dict[str, Any]) -> Tuple[float, int, float, int]:
    return (
        _safe_float(row.get("weight")) or -1.0,
        _safe_int(row.get("reps")) or -1,
        -(_safe_float(row.get("rpe")) or 99.0),
        -(_safe_int(row.get("set_number")) or 999),
    )


def _build_progression_sessions(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str, str], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str, str, str], Dict[Tuple[str, int], Dict[str, Any]]] = {}
    for row in rows:
        row = dict(row)
        row["e1rm"] = _e1rm(row.get("weight"), row.get("reps"))
        exercise_key = _session_group_key(row)
        session_key = (str(row.get("date_iso") or ""), int(row.get("workout_id") or 0))
        session = grouped.setdefault(exercise_key, {}).get(session_key)
        if session is None:
            session = {
                "workout_id": row.get("workout_id"),
                "date_iso": row.get("date_iso"),
                "date": row.get("date_iso"),
                "workout_name": row.get("workout_name"),
                "exercise": row.get("exercise"),
                "variation": row.get("variation"),
                "device": row.get("device"),
                "laterality": row.get("laterality"),
                "top_set_id": row.get("set_id"),
                "top_set_number": row.get("set_number"),
                "weight": row.get("weight"),
                "top_set_weight": row.get("weight"),
                "reps": row.get("reps"),
                "top_set_reps": row.get("reps"),
                "rpe": row.get("rpe"),
                "top_set_rpe": row.get("rpe"),
                "e1rm": row.get("e1rm"),
                "n_sets": 1,
            }
            grouped[exercise_key][session_key] = session
            continue

        session["n_sets"] = int(session.get("n_sets") or 0) + 1
        if _session_sort_key(row) > _session_sort_key(session):
            session.update(
                {
                    "top_set_id": row.get("set_id"),
                    "top_set_number": row.get("set_number"),
                    "weight": row.get("weight"),
                    "top_set_weight": row.get("weight"),
                    "reps": row.get("reps"),
                    "top_set_reps": row.get("reps"),
                    "rpe": row.get("rpe"),
                    "top_set_rpe": row.get("rpe"),
                    "e1rm": row.get("e1rm"),
                }
            )

    out: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for key, by_session in grouped.items():
        sessions = list(by_session.values())
        sessions.sort(key=lambda item: (str(item.get("date_iso") or ""), int(item.get("workout_id") or 0)))
        best_so_far: float | None = None
        previous: Dict[str, Any] | None = None
        progression_events = 0
        for session in sessions:
            curr_e1 = _safe_float(session.get("e1rm"))
            prev_e1 = _safe_float(previous.get("e1rm")) if previous else None
            prev_w = _safe_float(previous.get("weight")) if previous else None
            prev_r = _safe_int(previous.get("reps")) if previous else None
            prev_rpe = _safe_float(previous.get("rpe")) if previous else None

            signal, reason = _progress_signal(session, previous)
            session["signal"] = signal
            session["signal_reason"] = reason
            session["delta_e1rm_vs_previous"] = None if curr_e1 is None or prev_e1 is None else curr_e1 - prev_e1
            session["delta_e1rm_vs_previous_pct"] = _pct_delta(curr_e1, prev_e1)
            session["delta_weight_vs_previous"] = None if _safe_float(session.get("weight")) is None or prev_w is None else _safe_float(session.get("weight")) - prev_w
            session["delta_reps_vs_previous"] = None if _safe_int(session.get("reps")) is None or prev_r is None else _safe_int(session.get("reps")) - prev_r
            session["delta_rpe_vs_previous"] = None if _safe_float(session.get("rpe")) is None or prev_rpe is None else _safe_float(session.get("rpe")) - prev_rpe
            session["delta_e1rm_vs_best_prior"] = None if curr_e1 is None or best_so_far is None else curr_e1 - best_so_far
            session["is_all_time_pr"] = bool(curr_e1 is not None and (best_so_far is None or curr_e1 > best_so_far * (1.0 + _PROGRESSION_EPSILON)))

            if session["is_all_time_pr"] and previous is not None:
                progression_events += 1
            if curr_e1 is not None and (best_so_far is None or curr_e1 > best_so_far):
                best_so_far = curr_e1
            previous = session

        for session in sessions:
            session["progression_events_so_far"] = progression_events
        out[key] = sessions
    return out


def _fetch_workouts(date_from: str | None, date_to: str | None, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    conn = connections.get_training_db()
    cur = conn.cursor()

    where, params = build_date_where("w.date_iso", date_from, date_to)
    sql = """
        SELECT
            w.id AS id,
            w.date_iso AS date_iso,
            w.name AS name,
            COUNT(DISTINCT e.id) AS n_exercises,
            COUNT(s.id) AS n_sets
        FROM workouts w
        LEFT JOIN exercises e ON e.workout_id = w.id
        LEFT JOIN sets s ON s.workout_id = w.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY w.id ORDER BY w.date_iso DESC, w.id DESC LIMIT ? OFFSET ?"
    params.extend([limit + 1, offset])

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return rows, len(rows)


def _build_progression_monthly(sessions_by_exercise: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    monthly: List[Dict[str, Any]] = []
    for key, sessions in sessions_by_exercise.items():
        exercise, variation, device, laterality = key
        by_month: Dict[str, List[Dict[str, Any]]] = {}
        for session in sessions:
            month = str(session.get("date_iso") or "")[:7]
            if len(month) != 7:
                continue
            by_month.setdefault(month, []).append(session)

        prev_latest_e1rm: float | None = None
        for month in sorted(by_month):
            items = by_month[month]
            items.sort(key=lambda item: (str(item.get("date_iso") or ""), int(item.get("workout_id") or 0)))
            first = items[0]
            latest = items[-1]
            first_e1rm = _safe_float(first.get("e1rm"))
            latest_e1rm = _safe_float(latest.get("e1rm"))
            valid_e1rms = [_safe_float(item.get("e1rm")) for item in items]
            valid_e1rms = [value for value in valid_e1rms if value is not None]
            best_e1rm = max(valid_e1rms) if valid_e1rms else None
            monthly.append(
                {
                    "month": month,
                    "exercise": exercise,
                    "variation": variation,
                    "device": device,
                    "laterality": laterality,
                    "sessions": len(items),
                    "progression_events": sum(1 for item in items if item.get("signal") == "progress"),
                    "regression_events": sum(1 for item in items if item.get("signal") == "regress"),
                    "stable_sessions": sum(1 for item in items if item.get("signal") == "stable"),
                    "first_date": first.get("date_iso"),
                    "latest_date": latest.get("date_iso"),
                    "first_e1rm": first_e1rm,
                    "latest_e1rm": latest_e1rm,
                    "best_e1rm": best_e1rm,
                    "delta_e1rm_in_month": None if latest_e1rm is None or first_e1rm is None else latest_e1rm - first_e1rm,
                    "delta_e1rm_in_month_pct": _pct_delta(latest_e1rm, first_e1rm),
                    "delta_e1rm_vs_prev_month": None if latest_e1rm is None or prev_latest_e1rm is None else latest_e1rm - prev_latest_e1rm,
                    "delta_e1rm_vs_prev_month_pct": _pct_delta(latest_e1rm, prev_latest_e1rm),
                }
            )
            prev_latest_e1rm = latest_e1rm
    monthly.sort(key=lambda item: (str(item.get("month") or ""), str(item.get("exercise") or ""), str(item.get("variation") or ""), str(item.get("device") or "")))
    return monthly


def _fetch_raw_sets(date_from: str | None, date_to: str | None, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    conn = connections.get_training_db()
    cur = conn.cursor()

    where, params = build_date_where("w.date_iso", date_from, date_to)
    sql = """
        SELECT
            s.id AS set_id,
            w.id AS workout_id,
            w.date_iso AS date_iso,
            w.name AS workout_name,
            e.name AS exercise,
            e.variation AS variation,
            e.device AS device,
            e.laterality AS laterality,
            s.set_number AS set_number,
            s.reps AS reps,
            s.weight AS weight,
            s.rpe AS rpe
        FROM sets s
        JOIN workouts w ON w.id = s.workout_id
        JOIN exercises e ON e.id = s.exercise_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY w.date_iso DESC, s.id DESC LIMIT ? OFFSET ?"
    params.extend([limit + 1, offset])

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()
    return rows, len(rows)


def register(ai_api):
    @ai_api.get("/gym/exercises_catalog")
    @require_ai_read
    def gym_exercises_catalog():
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=1000)
        conn = connections.get_training_db()
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT
                name,
                variation,
                device,
                laterality,
                COUNT(*) AS n
            FROM exercises
            GROUP BY name, variation, device, laterality
            ORDER BY n DESC, name ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        data = [dict(r) for r in rows]
        return jsonify({"ok": True, "data": data, "meta": {"limit": limit}})

    @ai_api.get("/gym/workouts")
    @require_ai_read
    def gym_workouts():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        offset = parse_cursor(request.args.get("cursor"))
        fields = parse_fields(request.args.get("fields"), WORKOUT_FIELDS)

        rows, _ = _fetch_workouts(date_from, date_to, limit, offset)
        trimmed = rows[:limit]
        for row in trimmed:
            # Backward-compatible aliases expected by some GPT actions.
            row["date"] = row.get("date_iso")
            row["title"] = row.get("name")
        data = apply_fields(trimmed, fields)

        meta = {
            "from": date_from,
            "to": date_to,
            "last": last_token,
        }
        meta = add_pagination_meta(meta, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": data, "meta": meta})

    @ai_api.get("/gym/workout/<int:workout_id>")
    @require_ai_read
    def gym_workout(workout_id: int):
        conn = connections.get_training_db()
        cur = conn.cursor()
        workout = cur.execute(
            "SELECT id, date_iso, name, notes, created_at FROM workouts WHERE id=?",
            (workout_id,),
        ).fetchone()
        if not workout:
            conn.close()
            return jsonify({"ok": False, "error_code": "not_found", "message": "workout not found", "data": None})

        exercises = cur.execute(
            """
            SELECT id, workout_id, name, variation, device, laterality, created_at
            FROM exercises
            WHERE workout_id=?
            ORDER BY id ASC
            """,
            (workout_id,),
        ).fetchall()

        sets = cur.execute(
            """
            SELECT id, exercise_id, workout_id, set_number, reps, weight, rpe, created_at
            FROM sets
            WHERE workout_id=?
            ORDER BY exercise_id ASC, set_number ASC, id ASC
            """,
            (workout_id,),
        ).fetchall()
        conn.close()

        exercise_map: Dict[int, Dict[str, Any]] = {}
        for row in exercises:
            payload = dict(row)
            payload["sets"] = []
            exercise_map[row["id"]] = payload

        for row in sets:
            ex = exercise_map.get(row["exercise_id"])
            if ex is None:
                continue
            ex["sets"].append(dict(row))

        return jsonify({
            "ok": True,
            "data": {
                "workout": dict(workout),
                "exercises": list(exercise_map.values()),
            },
        })

    @ai_api.get("/gym/raw_sets")
    @require_ai_read
    def gym_raw_sets():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        offset = parse_cursor(request.args.get("cursor"))
        fields = parse_fields(request.args.get("fields"), RAW_SET_FIELDS)

        rows, _ = _fetch_raw_sets(date_from, date_to, limit, offset)
        trimmed = rows[:limit]
        data = apply_fields(trimmed, fields)
        meta = {
            "from": date_from,
            "to": date_to,
            "last": last_token,
        }
        meta = add_pagination_meta(meta, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": data, "meta": meta})

    @ai_api.get("/gym/top_sets")
    @require_ai_read
    def gym_top_sets():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=1000)

        conn = connections.get_training_db()
        cur = conn.cursor()
        where, params = build_date_where("w.date_iso", date_from, date_to)
        sql = """
            SELECT
                e.name AS exercise,
                e.variation AS variation,
                MAX(s.weight) AS max_weight,
                MAX(CASE
                    WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL AND s.reps > 0
                    THEN (s.weight * (1.0 + (s.reps / 30.0)))
                    ELSE NULL
                END) AS max_e1rm,
                COUNT(*) AS n_sets
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = s.workout_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY e.name, e.variation ORDER BY max_e1rm DESC, max_weight DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "meta": {"from": date_from, "to": date_to, "last": last_token, "limit": limit}})

    @ai_api.get("/gym/exercise_frequency")
    @require_ai_read
    def gym_exercise_frequency():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=1000)
        conn = connections.get_training_db()
        cur = conn.cursor()
        where, params = build_date_where("w.date_iso", date_from, date_to)
        sql = """
            SELECT
                e.name AS exercise,
                e.variation AS variation,
                COUNT(*) AS n_sets
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = s.workout_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY e.name, e.variation ORDER BY n_sets DESC, exercise ASC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "meta": {"from": date_from, "to": date_to, "last": last_token, "limit": limit}})

    @ai_api.get("/gym/summary_weekly")
    @require_ai_read
    def gym_summary_weekly():
        date_from, date_to, last_token = parse_time_range(request.args)
        conn = connections.get_training_db()
        cur = conn.cursor()
        where, params = build_date_where("w.date_iso", date_from, date_to)
        sql = """
            SELECT
                strftime('%Y-%W', date(w.date_iso)) AS week,
                COUNT(DISTINCT w.id) AS n_workouts,
                COUNT(s.id) AS n_sets,
                SUM(COALESCE(s.weight, 0) * COALESCE(s.reps, 0)) AS tonnage
            FROM workouts w
            LEFT JOIN sets s ON s.workout_id = w.id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY week ORDER BY week DESC"
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "meta": {"from": date_from, "to": date_to, "last": last_token}})

    @ai_api.get("/gym/prs")
    @require_ai_read
    def gym_prs():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=1000)
        conn = connections.get_training_db()
        cur = conn.cursor()
        where, params = build_date_where("w.date_iso", date_from, date_to)
        sql = """
            SELECT
                e.name AS exercise,
                e.variation AS variation,
                MAX(s.weight) AS max_weight,
                MAX(CASE
                    WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL AND s.reps > 0
                    THEN (s.weight * (1.0 + (s.reps / 30.0)))
                    ELSE NULL
                END) AS max_e1rm
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = s.workout_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY e.name, e.variation ORDER BY max_e1rm DESC, max_weight DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()
        return jsonify({"ok": True, "data": rows, "meta": {"from": date_from, "to": date_to, "last": last_token, "limit": limit}})

    @ai_api.get("/gym/progression/overview")
    @require_ai_read
    def gym_progression_overview():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=100, max_limit=500)
        fields = parse_fields(request.args.get("fields"), PROGRESSION_OVERVIEW_FIELDS)

        rows = _fetch_progression_rows(
            date_from,
            date_to,
            exercise=(request.args.get("exercise") or "").strip() or None,
            variation=(request.args.get("variation") or "").strip() or None,
            device=(request.args.get("device") or "").strip() or None,
            laterality=(request.args.get("laterality") or "").strip() or None,
        )
        sessions_by_exercise = _build_progression_sessions(rows)
        data: List[Dict[str, Any]] = []
        for sessions in sessions_by_exercise.values():
            if not sessions:
                continue
            first = sessions[0]
            latest = sessions[-1]
            previous = sessions[-2] if len(sessions) > 1 else None
            best = max(sessions, key=lambda item: _safe_float(item.get("e1rm")) or -1.0)
            progression_events = sum(1 for item in sessions[1:] if item.get("is_all_time_pr"))

            latest_e1 = _safe_float(latest.get("e1rm"))
            first_e1 = _safe_float(first.get("e1rm"))
            prev_e1 = _safe_float(previous.get("e1rm")) if previous else None
            item = {
                "exercise": latest.get("exercise"),
                "variation": latest.get("variation"),
                "device": latest.get("device"),
                "laterality": latest.get("laterality"),
                "sessions": len(sessions),
                "progression_events": progression_events,
                "latest_signal": latest.get("signal"),
                "latest_signal_reason": latest.get("signal_reason"),
                "first_date": first.get("date_iso"),
                "latest_date": latest.get("date_iso"),
                "best_date": best.get("date_iso"),
                "first_e1rm": first_e1,
                "latest_e1rm": latest_e1,
                "best_e1rm": _safe_float(best.get("e1rm")),
                "delta_e1rm_vs_first": None if latest_e1 is None or first_e1 is None else latest_e1 - first_e1,
                "delta_e1rm_vs_first_pct": _pct_delta(latest_e1, first_e1),
                "delta_e1rm_vs_previous": None if latest_e1 is None or prev_e1 is None else latest_e1 - prev_e1,
                "delta_e1rm_vs_previous_pct": _pct_delta(latest_e1, prev_e1),
                "delta_weight_vs_previous": latest.get("delta_weight_vs_previous"),
                "delta_reps_vs_previous": latest.get("delta_reps_vs_previous"),
                "delta_rpe_vs_previous": latest.get("delta_rpe_vs_previous"),
            }
            data.append(item)

        data.sort(
            key=lambda item: (
                -(int(item.get("progression_events") or 0)),
                -(_safe_float(item.get("delta_e1rm_vs_first_pct")) if _safe_float(item.get("delta_e1rm_vs_first_pct")) is not None else -9999.0),
                str(item.get("exercise") or ""),
            )
        )
        trimmed = data[:limit]
        return jsonify({"ok": True, "data": apply_fields(trimmed, fields), "meta": {"from": date_from, "to": date_to, "last": last_token, "limit": limit}})

    @ai_api.get("/gym/progression/exercise")
    @require_ai_read
    def gym_progression_exercise():
        exercise = (request.args.get("exercise") or "").strip()
        if not exercise:
            return jsonify({"ok": False, "error_code": "missing_params", "message": "exercise is required", "data": [], "meta": {"exercise": None}})

        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=100, max_limit=500)
        fields = parse_fields(request.args.get("fields"), PROGRESSION_SESSION_FIELDS)
        variation = (request.args.get("variation") or "").strip() or None
        device = (request.args.get("device") or "").strip() or None
        laterality = (request.args.get("laterality") or "").strip() or None

        rows = _fetch_progression_rows(date_from, date_to, exercise, variation, device, laterality)
        sessions_by_exercise = _build_progression_sessions(rows)
        target_key = None
        for key in sessions_by_exercise:
            if key[0] != exercise:
                continue
            if variation is not None and key[1] != variation:
                continue
            if device is not None and key[2] != device:
                continue
            if laterality is not None and key[3] != laterality:
                continue
            target_key = key
            break

        sessions = sessions_by_exercise.get(target_key or ("", "", "", ""), [])
        sessions = sessions[-limit:]
        payload = apply_fields(sessions, fields)
        meta = {
            "from": date_from,
            "to": date_to,
            "last": last_token,
            "limit": limit,
            "exercise": exercise,
            "variation": variation,
            "device": device,
            "laterality": laterality,
            "sessions": len(sessions),
        }
        return jsonify({"ok": True, "data": payload, "meta": meta})

    @ai_api.get("/gym/progression/monthly")
    @require_ai_read
    def gym_progression_monthly():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        fields = parse_fields(request.args.get("fields"), PROGRESSION_MONTHLY_FIELDS)

        rows = _fetch_progression_rows(
            date_from,
            date_to,
            exercise=(request.args.get("exercise") or "").strip() or None,
            variation=(request.args.get("variation") or "").strip() or None,
            device=(request.args.get("device") or "").strip() or None,
            laterality=(request.args.get("laterality") or "").strip() or None,
        )
        sessions_by_exercise = _build_progression_sessions(rows)
        data = _build_progression_monthly(sessions_by_exercise)
        data.sort(
            key=lambda item: (
                str(item.get("month") or ""),
                -(int(item.get("progression_events") or 0)),
                str(item.get("exercise") or ""),
            ),
            reverse=True,
        )
        trimmed = data[:limit]
        return jsonify({"ok": True, "data": apply_fields(trimmed, fields), "meta": {"from": date_from, "to": date_to, "last": last_token, "limit": limit}})
