from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import (
    get_core_db,
    get_hrv_db,
    get_nutrition_db,
    get_runs_db,
    get_training_db,
)

VALID_MODES = {"mission", "priority", "replay", "sparring", "interview"}
TONE_STYLES = {"calm_direct", "ultra_short", "analytic", "hard_guard"}
REENTRY_STYLES = {"minimal", "structured", "social_trigger", "timeboxed"}
PRIMARY_GOALS = {"hypertrophy", "strength", "5k", "hybrid", "recovery"}
_ACTIONS = {"accept", "tighten", "ease", "reroute"}

_DEFAULT_GOAL_WEIGHTS = {
    "strength": 0.35,
    "hypertrophy": 0.30,
    "run": 0.20,
    "recovery": 0.15,
}

_WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

_DEFAULT_STRENGTH_UNITS = [
    "Upper A",
    "Lower (Squat Volumen)",
    "Push",
    "Pull",
]
_DEFAULT_RUN_UNITS = ["30 min Z2", "Easy Run 40 min", "Intervals 6x2 min"]
_EXERCISE_VARIANTS = {
    "Squats": ["Squats", "Front Squats", "Hack Squat"],
    "Beinstrecker": ["Beinstrecker", "Leg Press", "Bulgarian Split Squat"],
    "Beinbeuger": ["Beinbeuger", "RDL", "Nordic Curl"],
    "Wadenheben": ["Wadenheben", "Seated Calf Raise", "Donkey Calf Raise"],
    "Crunches": ["Crunches", "Hanging Knee Raise", "Cable Crunch"],
    "Bench": ["Bench", "Incline Bench", "Kurzhantel-Bankdrücken"],
    "Incline DB Press": ["Incline DB Press", "Incline Bench", "Maschinen-Press"],
    "Schulterdrücken": ["Schulterdrücken", "Arnold Press", "Maschinen-Schulterdrücken"],
    "Seitheben": ["Seitheben", "Cable Lateral Raise", "Maschinen-Seitheben"],
    "Trizeps Pushdown": ["Trizeps Pushdown", "Overhead Extension", "Dips"],
    "Row": ["Row", "Chest Supported Row", "Cable Row"],
    "Lat Pulldown": ["Lat Pulldown", "Pull-up", "Close Grip Pulldown"],
    "Chest Supported Row": ["Chest Supported Row", "Row", "Seal Row"],
    "Rear Delt Fly": ["Rear Delt Fly", "Face Pull", "Reverse Pec Deck"],
    "Bizeps Curl": ["Bizeps Curl", "Hammer Curl", "Preacher Curl"],
    "Incline Press": ["Incline Press", "Incline DB Press", "Maschinen-Incline"],
}


# -------------------------
# Basics
# -------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_text(raw: str | None) -> str:
    return " ".join(str(raw or "").split()).strip()


def _clamp_int(v: Any, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def _clamp_float(v: Any, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def _parse_num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def _normalize_goal_weights(raw: Any) -> dict[str, float]:
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for key, default in _DEFAULT_GOAL_WEIGHTS.items():
        out[key] = _clamp_float(src.get(key, default), 0.05, 0.8)
    total = sum(out.values())
    if total <= 0:
        return dict(_DEFAULT_GOAL_WEIGHTS)
    return {k: v / total for k, v in out.items()}


def _open_db_rw(db_fn):
    conn = db_fn()
    conn.row_factory = sqlite3.Row
    return conn


def _safe_query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = (), default: float = 0.0) -> float:
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return default
        val = row[0]
        if val is None:
            return default
        return float(val)
    except Exception:
        return default


def _seeded_rng(session_id: int, step_no: int) -> random.Random:
    token = f"core-lab|{date.today().isoformat()}|{session_id}|{step_no}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


# -------------------------
# Schema
# -------------------------

def ensure_core_training_schema() -> None:
    conn = _open_db_rw(get_core_db)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_profile (
          id INTEGER PRIMARY KEY CHECK (id=1),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          tone_style TEXT NOT NULL DEFAULT 'calm_direct',
          autonomy_level INTEGER NOT NULL DEFAULT 2,
          strictness_level INTEGER NOT NULL DEFAULT 2,
          reentry_style TEXT NOT NULL DEFAULT 'minimal',
          primary_goal TEXT NOT NULL DEFAULT 'hybrid',
          goal_weights_json TEXT NOT NULL DEFAULT '{}',
          notes TEXT NOT NULL DEFAULT ''
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_lab_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mode TEXT NOT NULL,
          mission_json TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          step_index INTEGER NOT NULL DEFAULT 0,
          labels_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_lab_steps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL,
          step_no INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES core_lab_sessions(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_lab_feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL,
          step_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          confidence_user INTEGER,
          override_text TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES core_lab_sessions(id),
          FOREIGN KEY(step_id) REFERENCES core_lab_steps(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_lab_memory (
          key TEXT PRIMARY KEY,
          value REAL NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_core_lab_steps_session ON core_lab_steps(session_id, step_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_core_lab_feedback_session ON core_lab_feedback(session_id)")

    now = _now_iso()
    row = cur.execute("SELECT id FROM core_profile WHERE id=1").fetchone()
    if row is None:
        cur.execute(
            """
            INSERT INTO core_profile
            (id, created_at, updated_at, tone_style, autonomy_level, strictness_level, reentry_style, primary_goal, goal_weights_json, notes)
            VALUES (1, ?, ?, 'calm_direct', 2, 2, 'minimal', 'hybrid', ?, '')
            """,
            (now, now, _json_dumps(_DEFAULT_GOAL_WEIGHTS)),
        )

    for key in (
        "strictness_bias",
        "autonomy_bias",
        "tone_bias",
        "volume_bias",
        "run_bias",
        "fatigue_chain_bias",
        "recovery_guard_bias",
        "performance_push_bias",
        "quality_focus_bias",
    ):
        cur.execute(
            "INSERT OR IGNORE INTO core_lab_memory (key, value, updated_at) VALUES (?, 0, ?)",
            (key, now),
        )

    conn.commit()
    conn.close()


# -------------------------
# Profile
# -------------------------

def _profile_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM core_profile WHERE id=1").fetchone()
    if row is None:
        ensure_core_training_schema()
        row = conn.execute("SELECT * FROM core_profile WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("core_profile_missing")
    return row


def _serialize_profile(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "tone_style": str(row["tone_style"]),
        "autonomy_level": int(row["autonomy_level"]),
        "strictness_level": int(row["strictness_level"]),
        "reentry_style": str(row["reentry_style"]),
        "primary_goal": str(row["primary_goal"]),
        "goal_weights_json": _normalize_goal_weights(_safe_json_load(row["goal_weights_json"], _DEFAULT_GOAL_WEIGHTS)),
        "notes": str(row["notes"] or ""),
    }


def get_core_profile() -> dict[str, Any]:
    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)
    out = _serialize_profile(_profile_row(conn))
    conn.close()
    return out


def update_core_profile(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)
    current = _serialize_profile(_profile_row(conn))

    updates: dict[str, Any] = {}

    if "tone_style" in payload:
        v = str(payload.get("tone_style") or "").strip()
        if v not in TONE_STYLES:
            conn.close()
            raise ValueError("invalid_tone_style")
        updates["tone_style"] = v

    if "autonomy_level" in payload:
        v = _clamp_int(payload.get("autonomy_level"), 0, 4)
        updates["autonomy_level"] = v

    if "strictness_level" in payload:
        v = _clamp_int(payload.get("strictness_level"), 0, 4)
        updates["strictness_level"] = v

    if "reentry_style" in payload:
        v = str(payload.get("reentry_style") or "").strip()
        if v not in REENTRY_STYLES:
            conn.close()
            raise ValueError("invalid_reentry_style")
        updates["reentry_style"] = v

    if "primary_goal" in payload:
        v = str(payload.get("primary_goal") or "").strip()
        if v not in PRIMARY_GOALS:
            conn.close()
            raise ValueError("invalid_primary_goal")
        updates["primary_goal"] = v

    if "goal_weights_json" in payload:
        raw = payload.get("goal_weights_json")
        parsed = _safe_json_load(raw, raw) if isinstance(raw, str) else raw
        updates["goal_weights_json"] = _json_dumps(_normalize_goal_weights(parsed))

    if not updates:
        conn.close()
        return current

    updates["updated_at"] = _now_iso()
    assignments = ", ".join([f"{k}=?" for k in updates.keys()])
    conn.execute(f"UPDATE core_profile SET {assignments} WHERE id=1", tuple(updates.values()))
    conn.commit()

    out = _serialize_profile(_profile_row(conn))
    conn.close()
    return out


# -------------------------
# Signals
# -------------------------

def _compute_band(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    vals = sorted(values)
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    lo = vals[max(0, int(n * 0.2) - 1)]
    hi = vals[min(n - 1, int(n * 0.8))]
    return float(median), float(lo), float(hi)


def _fetch_signals() -> dict[str, Any]:
    today = date.today()
    d7 = (today - timedelta(days=6)).isoformat()
    d14 = (today - timedelta(days=13)).isoformat()
    d30 = (today - timedelta(days=29)).isoformat()

    out: dict[str, Any] = {
        "workouts_7d": 0,
        "runs_7d": 0,
        "runs_km_7d": 0.0,
        "days_since_workout": None,
        "nutrition_days_7d": 0,
        "kcal_avg_7d": None,
        "weight_series": [],
        "kcal_series": [],
        "rmssd_series": [],
        "rhr_series": [],
        "sleep_series": [],
        "rmssd_today": None,
        "rhr_today": None,
        "rmssd_med": None,
        "rmssd_low": None,
        "rmssd_high": None,
        "rhr_med": None,
        "rhr_low": None,
        "rhr_high": None,
        "signal_quality": "mittel",
        "weight_delta_14d": None,
    }

    # training
    tconn = _open_db_rw(get_training_db)
    try:
        out["workouts_7d"] = int(_safe_scalar(tconn, "SELECT COUNT(*) FROM workouts WHERE date_iso >= ?", (d7,), 0))
        last = _safe_query(tconn, "SELECT date_iso FROM workouts WHERE date_iso IS NOT NULL ORDER BY date_iso DESC LIMIT 1")
        if last and last[0]["date_iso"]:
            try:
                ld = date.fromisoformat(str(last[0]["date_iso"]))
                out["days_since_workout"] = max(0, (today - ld).days)
            except Exception:
                pass
    finally:
        tconn.close()

    # runs
    rconn = _open_db_rw(get_runs_db)
    try:
        out["runs_7d"] = int(_safe_scalar(rconn, "SELECT COUNT(*) FROM runs WHERE substr(date,1,10) >= ?", (d7,), 0))
        km = _safe_scalar(rconn, "SELECT SUM(distance) FROM runs WHERE substr(date,1,10) >= ?", (d7,), 0.0)
        out["runs_km_7d"] = round((km or 0.0) / 1000.0, 1)
    finally:
        rconn.close()

    # nutrition & weight
    nconn = _open_db_rw(get_nutrition_db)
    try:
        out["nutrition_days_7d"] = int(_safe_scalar(nconn, "SELECT COUNT(*) FROM nutrition_daily WHERE date_iso >= ?", (d7,), 0))
        kcal_avg = _safe_scalar(nconn, "SELECT AVG(kcal) FROM nutrition_daily WHERE date_iso >= ?", (d7,), 0)
        out["kcal_avg_7d"] = round(kcal_avg, 1) if kcal_avg > 0 else None

        kcal_rows = _safe_query(
            nconn,
            "SELECT date_iso, kcal FROM nutrition_daily WHERE date_iso >= ? ORDER BY date_iso ASC LIMIT 14",
            (d14,),
        )
        out["kcal_series"] = [float(r["kcal"]) for r in kcal_rows if r["kcal"] is not None]

        w_rows = _safe_query(
            nconn,
            "SELECT date_iso, weight_kg FROM weight_logs WHERE date_iso >= ? ORDER BY date_iso ASC LIMIT 14",
            (d14,),
        )
        weights = [float(r["weight_kg"]) for r in w_rows if r["weight_kg"] is not None]
        out["weight_series"] = weights
        if len(weights) >= 2:
            out["weight_delta_14d"] = round(weights[-1] - weights[0], 2)
    finally:
        nconn.close()

    # hrv/rhr
    hconn = _open_db_rw(get_hrv_db)
    try:
        rows = _safe_query(
            hconn,
            """
            SELECT COALESCE(date_utc, substr(ts_measurement,1,10)) AS d, rmssd, hr
            FROM hrv_measurements
            WHERE COALESCE(date_utc, substr(ts_measurement,1,10)) >= ?
            ORDER BY d ASC
            """,
            (d30,),
        )
        rmssd_vals = [float(r["rmssd"]) for r in rows if r["rmssd"] is not None]
        rhr_vals = [float(r["hr"]) for r in rows if r["hr"] is not None]
        out["rmssd_series"] = rmssd_vals[-14:]
        out["rhr_series"] = rhr_vals[-14:]

        if rmssd_vals:
            out["rmssd_today"] = rmssd_vals[-1]
        if rhr_vals:
            out["rhr_today"] = rhr_vals[-1]

        rm_m, rm_lo, rm_hi = _compute_band(rmssd_vals[-14:])
        rh_m, rh_lo, rh_hi = _compute_band(rhr_vals[-14:])
        out["rmssd_med"], out["rmssd_low"], out["rmssd_high"] = rm_m, rm_lo, rm_hi
        out["rhr_med"], out["rhr_low"], out["rhr_high"] = rh_m, rh_lo, rh_hi

        hrv_samples_7d = len([r for r in rows if str(r["d"]) >= d7])
        if hrv_samples_7d >= 5:
            out["signal_quality"] = "sauber"
        elif hrv_samples_7d >= 3:
            out["signal_quality"] = "mittel"
        else:
            out["signal_quality"] = "unklar"
    finally:
        hconn.close()

    return out


# -------------------------
# Session + Mission
# -------------------------

def _load_memory(conn: sqlite3.Connection) -> dict[str, float]:
    rows = _safe_query(conn, "SELECT key, value FROM core_lab_memory")
    mem = {str(r["key"]): float(r["value"] or 0.0) for r in rows}
    for k in (
        "strictness_bias",
        "autonomy_bias",
        "tone_bias",
        "volume_bias",
        "run_bias",
        "fatigue_chain_bias",
        "recovery_guard_bias",
        "performance_push_bias",
        "quality_focus_bias",
    ):
        mem.setdefault(k, 0.0)
    return mem


def _save_memory(conn: sqlite3.Connection, mem: dict[str, float]) -> None:
    now = _now_iso()
    for k, v in mem.items():
        conn.execute(
            "INSERT INTO core_lab_memory (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (k, float(v), now),
        )


def _mode_norm(mode: str) -> str:
    m = str(mode or "").strip().lower()
    if m == "sparring":
        return "mission"
    if m == "interview":
        return "priority"
    return m


def _mission_for_mode(mode: str, profile: dict[str, Any]) -> dict[str, Any]:
    m = _mode_norm(mode)
    if m == "priority":
        title = "Prioritäten-Drill"
        focus = "Konflikte zwischen Kraft, Lauf und Erholung sauber lösen"
    elif m == "replay":
        title = "Failure Replay"
        focus = "Schwache Woche neu durchspielen und bessere Eingriffe trainieren"
    else:
        title = "Mission Sprint"
        focus = "Präzise nächste Einheiten mit klaren Parametern planen"

    return {
        "title": title,
        "focus": focus,
        "goal": str(profile.get("primary_goal") or "hybrid"),
    }


def _load_recent_unit_names() -> list[str]:
    conn = _open_db_rw(get_training_db)
    try:
        rows = _safe_query(
            conn,
            "SELECT name FROM workouts WHERE name IS NOT NULL AND TRIM(name) != '' ORDER BY date_iso DESC LIMIT 30",
        )
    finally:
        conn.close()

    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        name = _normalize_text(r["name"])
        if not name:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)

    # Keep concrete common names for clarity in timeline.
    concrete: list[str] = []
    for n in out:
        low = n.lower()
        if any(k in low for k in ("upper", "lower", "push", "pull", "rest")):
            concrete.append(n)
    if not concrete:
        concrete = list(_DEFAULT_STRENGTH_UNITS)
    return concrete[:6]


def start_core_training_session(mode: str) -> dict[str, Any]:
    mode_norm = _mode_norm(mode)
    if mode_norm not in VALID_MODES:
        raise ValueError("invalid_mode")

    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)
    profile = _serialize_profile(_profile_row(conn))

    mission = _mission_for_mode(mode_norm, profile)
    mission["strength_units"] = _load_recent_unit_names()

    now = _now_iso()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO core_lab_sessions (mode, mission_json, started_at, ended_at, step_index, labels_count) VALUES (?, ?, ?, NULL, 0, 0)",
        (mode_norm, _json_dumps(mission), now),
    )
    sid = int(cur.lastrowid)
    conn.commit()
    conn.close()

    return {"session_id": sid, "mode": mode_norm, "started_at": now}


def end_core_training_session(session_id: int) -> dict[str, Any]:
    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)

    row = conn.execute(
        "SELECT id, mode, started_at, ended_at, step_index, labels_count FROM core_lab_sessions WHERE id=?",
        (int(session_id),),
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError("session_not_found")

    now = _now_iso()
    conn.execute("UPDATE core_lab_sessions SET ended_at=COALESCE(ended_at, ?) WHERE id=?", (now, int(session_id)))
    conn.commit()

    row2 = conn.execute(
        "SELECT id, mode, started_at, ended_at, step_index, labels_count FROM core_lab_sessions WHERE id=?",
        (int(session_id),),
    ).fetchone()
    conn.close()

    return {
        "session_id": int(row2["id"]),
        "mode": str(row2["mode"]),
        "started_at": str(row2["started_at"]),
        "ended_at": str(row2["ended_at"]),
        "cards_seen": int(row2["step_index"] or 0),
        "labels_given": int(row2["labels_count"] or 0),
    }


# -------------------------
# Plan + Payload
# -------------------------

def _weekday_label(d: date) -> str:
    return _WEEKDAY_DE[d.weekday()]


def _unit_kind(unit_name: str) -> str:
    low = unit_name.lower()
    if "z2" in low or "run" in low or "interval" in low:
        return "run"
    if "rest" in low:
        return "rest"
    if "meal" in low:
        return "meal"
    return "strength"


def _split_sets(raw: str) -> list[int]:
    out: list[int] = []
    for part in str(raw).split("/"):
        try:
            out.append(max(1, int(part.strip())))
        except Exception:
            continue
    return out or [8, 8]


def _rpe_series(base: float, n_sets: int) -> list[float]:
    vals: list[float] = []
    for i in range(n_sets):
        vals.append(round(_clamp_float(base + (0.2 if i > 0 else 0.0), 5.0, 10.0), 1))
    return vals


def _fmt_set_compact(sets: list[dict[str, Any]]) -> str:
    if not sets:
        return "—"
    reps = "/".join(str(int(_clamp_int(s.get("reps", 0), 1, 30))) for s in sets)
    weights: list[str] = []
    rpes: list[str] = []
    for s in sets:
        w = _clamp_float(s.get("weight_kg", 0), 0, 500)
        if abs(w - round(w)) < 1e-9:
            weights.append(str(int(round(w))))
        else:
            weights.append(str(round(w, 1)).replace(".", ","))
        rv = s.get("rpe")
        if rv is None:
            rpes.append("—")
        else:
            r = _clamp_float(rv, 0, 10)
            rpes.append(str(int(round(r))) if abs(r - round(r)) < 1e-9 else str(round(r, 1)).replace(".", ","))
    return f"{reps} x {'/'.join(weights)} kg @ {'/'.join(rpes)}"


def _apply_volume_delta(sets: list[dict[str, Any]], vol_delta: int) -> list[dict[str, Any]]:
    out = [dict(s) for s in sets]
    if vol_delta > 0 and out:
        for _ in range(vol_delta):
            out.append(dict(out[-1]))
    elif vol_delta < 0:
        for _ in range(abs(vol_delta)):
            if len(out) > 1:
                out.pop()
    return out


def _set_delta_text(proposed: list[dict[str, Any]], edited: list[dict[str, Any]]) -> tuple[str, str]:
    set_delta = len(edited) - len(proposed)
    p_w = [float(s.get("weight_kg", 0)) for s in proposed if s.get("weight_kg") is not None]
    e_w = [float(s.get("weight_kg", 0)) for s in edited if s.get("weight_kg") is not None]
    avg_w_delta = (sum(e_w) / max(1, len(e_w))) - (sum(p_w) / max(1, len(p_w))) if p_w and e_w else 0.0

    p_r = [float(s.get("rpe")) for s in proposed if s.get("rpe") is not None]
    e_r = [float(s.get("rpe")) for s in edited if s.get("rpe") is not None]
    avg_r_delta = (sum(e_r) / max(1, len(e_r))) - (sum(p_r) / max(1, len(p_r))) if p_r and e_r else 0.0

    line = f"({set_delta:+d} Satz; Ø {avg_w_delta:+.1f} kg"
    if p_r and e_r:
        line += f"; Ø {avg_r_delta:+.1f} RPE"
    line += ")"

    status = []
    if avg_w_delta > 0.15:
        status.append("Gewicht ↑")
    elif avg_w_delta < -0.15:
        status.append("Gewicht ↓")
    if set_delta > 0:
        status.append("Volumen ↑")
    elif set_delta < 0:
        status.append("Volumen ↓")
    if avg_r_delta > 0.15:
        status.append("RPE ↑")
    elif avg_r_delta < -0.15:
        status.append("RPE ↓")
    return line, (" | ".join(status) if status else "Stabil")


def _strength_template(unit_name: str, strictness_bias: float, vol_delta: int) -> list[dict[str, Any]]:
    if "lower" in unit_name.lower():
        base = [
            ("Squats", "6/6/6", 60.0),
            ("Beinstrecker", "10/10", 50.0),
            ("Beinbeuger", "10/10", 55.0),
            ("Wadenheben", "9/8", 110.0),
            ("Crunches", "8/9", 47.5),
        ]
    elif "push" in unit_name.lower():
        base = [
            ("Bench", "6/6/6", 95.0),
            ("Incline DB Press", "10/10", 30.0),
            ("Schulterdrücken", "8/8", 45.0),
            ("Seitheben", "14/12", 12.5),
            ("Trizeps Pushdown", "12/12", 45.0),
        ]
    elif "pull" in unit_name.lower():
        base = [
            ("Row", "8/8/8", 75.0),
            ("Lat Pulldown", "10/10", 65.0),
            ("Chest Supported Row", "10/10", 55.0),
            ("Rear Delt Fly", "14/12", 11.0),
            ("Bizeps Curl", "12/12", 30.0),
        ]
    else:  # upper
        base = [
            ("Bench", "6/6/6", 92.5),
            ("Row", "8/8/8", 72.5),
            ("Incline Press", "10/10", 65.0),
            ("Lat Pulldown", "10/10", 62.5),
            ("Seitheben", "14/12", 12.0),
        ]

    out: list[dict[str, Any]] = []
    for name, reps, weight in base:
        today_sets: list[dict[str, Any]] = []
        last_sets: list[dict[str, Any]] = []

        reps_list = _split_sets(reps)
        base_rpe = _clamp_float(7.8 + strictness_bias * 0.35, 6.0, 9.3)
        rpe_vals = _rpe_series(base_rpe, len(reps_list))

        for i, rep in enumerate(reps_list):
            today_sets.append(
                {
                    "reps": int(rep),
                    "weight_kg": round(max(5.0, weight + strictness_bias * 1.2 + i * 0.2), 1),
                    "rpe": rpe_vals[i],
                }
            )

        today_sets = _apply_volume_delta(today_sets, vol_delta)
        for i, s in enumerate(today_sets):
            last_sets.append(
                {
                    "reps": int(max(1, int(s["reps"]) - (1 if i % 2 else 0))),
                    "weight_kg": round(max(5.0, float(s["weight_kg"]) - (2.5 if i == 0 else 0.0)), 1),
                    "rpe": round(max(5.0, float(s["rpe"]) - 0.5), 1),
                }
            )

        delta_line, status = _set_delta_text(today_sets, today_sets)
        out.append(
            {
                "name": name,
                "last_days_ago": 2 + (len(name) % 5),
                "last_sets": last_sets,
                "proposed_sets": [dict(s) for s in today_sets],
                "today_sets": [dict(s) for s in today_sets],
                "last_compact": _fmt_set_compact(last_sets),
                "today_compact": _fmt_set_compact(today_sets),
                "delta_line": delta_line,
                "status_line": status,
                "alternatives": _EXERCISE_VARIANTS.get(name, [name]),
                "muted": False,
            }
        )
    return out


def _build_day_plan(
    *,
    d: date,
    unit_name: str,
    memory: dict[str, float],
    rng: random.Random,
) -> dict[str, Any]:
    kind = _unit_kind(unit_name)

    strictness_bias = _clamp_float(memory.get("strictness_bias", 0.0), -2.0, 2.0)
    chain_guard = _clamp_float(memory.get("fatigue_chain_bias", 0.0) + memory.get("recovery_guard_bias", 0.0), -2.0, 2.0)
    vol_delta = _clamp_int(round(memory.get("volume_bias", 0.0) - chain_guard * 0.4), -3, 3)

    run_duration = _clamp_int(round(30 + memory.get("run_bias", 0.0) * 6 + memory.get("performance_push_bias", 0.0) * 2), 20, 70)
    pulse_low = _clamp_int(135 + round(memory.get("run_bias", 0.0) * 2), 120, 165)
    pulse_high = _clamp_int(pulse_low + 10, 128, 178)

    if kind == "run":
        summary = f"{run_duration} min, {pulse_low}–{pulse_high} bpm"
    elif kind == "rest":
        summary = "Entlastung, Schlaf priorisieren, Schritte locker"
    else:
        summary = f"Sätze {12 + max(-2, vol_delta)}–{16 + max(0, vol_delta)}, Intensität kontrolliert"

    return {
        "date": d.isoformat(),
        "day": _weekday_label(d),
        "unit": unit_name,
        "kind": kind,
        "summary": summary,
        "defaults": {
            "rpe_cap": round(8.0 + strictness_bias * 0.2, 1),
            "volume_delta": int(vol_delta),
            "run_duration_min": int(run_duration),
            "pulse_low": int(pulse_low),
            "pulse_high": int(pulse_high),
        },
        "exercise_cards": _strength_template(unit_name, strictness_bias, vol_delta) if kind == "strength" else [],
    }


def _context_chain(horizon: list[dict[str, Any]], current_idx: int) -> dict[str, Any]:
    past = [h for h in horizon[:current_idx] if isinstance(h, dict)]
    prev1 = past[-1] if past else None
    prev2 = past[-2] if len(past) >= 2 else None
    prev3 = past[-3] if len(past) >= 3 else None

    def _is_long_run(item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        if str(item.get("kind") or "") != "run":
            return False
        unit = str(item.get("unit") or "").lower()
        dur = int(((item.get("defaults") or {}).get("run_duration_min") or 0))
        return "40" in unit or "interval" in unit or dur >= 40

    long_run_prev = _is_long_run(prev1)
    run_in_last2 = _is_long_run(prev1) or _is_long_run(prev2)
    lower_in_last2 = any("lower" in str((p or {}).get("unit") or "").lower() for p in (prev1, prev2) if p)
    hard_cluster = int(run_in_last2) + int(lower_in_last2)
    return {
        "past_days": [f"{p.get('day')} · {p.get('unit')}" for p in past[-3:]],
        "prev1": f"{prev1.get('day')} · {prev1.get('unit')}" if prev1 else "",
        "prev2": f"{prev2.get('day')} · {prev2.get('unit')}" if prev2 else "",
        "prev3": f"{prev3.get('day')} · {prev3.get('unit')}" if prev3 else "",
        "long_run_prev": bool(long_run_prev),
        "run_in_last2": bool(run_in_last2),
        "lower_in_last2": bool(lower_in_last2),
        "hard_cluster_score": int(hard_cluster),
    }


def _unit_load_score(plan: dict[str, Any]) -> float:
    kind = str(plan.get("kind") or "")
    unit = str(plan.get("unit") or "").lower()
    if kind == "rest":
        return -0.8
    if kind == "run":
        if "interval" in unit:
            return 1.8
        if "40" in unit:
            return 1.5
        return 1.2
    if kind == "strength":
        if "lower" in unit:
            return 1.7
        if "push" in unit or "pull" in unit or "upper" in unit:
            return 1.3
        return 1.1
    return 0.9


def _apply_day_hrv_chain(horizon: list[dict[str, Any]], signals: dict[str, Any], current_idx: int = 3) -> None:
    if not horizon:
        return
    rm_med = _parse_num(signals.get("rmssd_med"))
    rh_med = _parse_num(signals.get("rhr_med"))
    rm_today = _parse_num(signals.get("rmssd_today"))
    rh_today = _parse_num(signals.get("rhr_today"))
    rm_lo = _parse_num(signals.get("rmssd_low"))
    rm_hi = _parse_num(signals.get("rmssd_high"))
    rh_lo = _parse_num(signals.get("rhr_low"))
    rh_hi = _parse_num(signals.get("rhr_high"))

    rm_base = rm_med if rm_med is not None else (rm_today if rm_today is not None else 56.0)
    rh_base = rh_med if rh_med is not None else (rh_today if rh_today is not None else 54.0)

    fatigue: list[float] = [0.0] * len(horizon)
    for i in range(1, len(horizon)):
        prev_load = _unit_load_score(horizon[i - 1])
        fatigue[i] = _clamp_float(fatigue[i - 1] * 0.62 + prev_load, -2.0, 3.5)

    rm_seq: list[float] = []
    rh_seq: list[float] = []
    for i in range(len(horizon)):
        rm_seq.append(rm_base - fatigue[i] * 3.2)
        rh_seq.append(rh_base + fatigue[i] * 1.35)

    if 0 <= current_idx < len(horizon):
        if rm_today is not None:
            off = rm_today - rm_seq[current_idx]
            rm_seq = [v + off for v in rm_seq]
        if rh_today is not None:
            off = rh_today - rh_seq[current_idx]
            rh_seq = [v + off for v in rh_seq]

    if rm_lo is not None and rm_hi is not None:
        rm_seq = [_clamp_float(v, rm_lo - 8.0, rm_hi + 8.0) for v in rm_seq]
    if rh_lo is not None and rh_hi is not None:
        rh_seq = [_clamp_float(v, rh_lo - 5.0, rh_hi + 8.0) for v in rh_seq]

    for i, item in enumerate(horizon):
        item["hrv_rmssd"] = round(rm_seq[i], 1)
        item["hrv_rhr"] = round(rh_seq[i], 1)


def _build_horizon(
    session_mode: str,
    mission: dict[str, Any],
    step_no: int,
    memory: dict[str, float],
    signals: dict[str, Any],
    rng: random.Random,
) -> list[dict[str, Any]]:
    current_day = date.today() + timedelta(days=max(0, step_no - 1))

    strength_units = mission.get("strength_units") if isinstance(mission.get("strength_units"), list) else None
    if not strength_units:
        strength_units = list(_DEFAULT_STRENGTH_UNITS)

    if session_mode == "replay":
        pattern = [strength_units[1 % len(strength_units)], "30 min Z2", "Rest", strength_units[2 % len(strength_units)], "Easy Run 40 min"]
    elif session_mode == "priority":
        pattern = [strength_units[0], "30 min Z2", strength_units[1 % len(strength_units)], "Rest", strength_units[2 % len(strength_units)]]
    else:
        pattern = [strength_units[0], strength_units[1 % len(strength_units)], "30 min Z2", strength_units[2 % len(strength_units)], "Rest"]

    horizon: list[dict[str, Any]] = []
    # 7-day rolling queue: 3 past, current in middle, 3 future
    for rel in range(-3, 4):
        d = current_day + timedelta(days=rel)
        idx = (step_no - 1 + rel) % len(pattern)
        unit_name = pattern[idx]
        day_plan = _build_day_plan(d=d, unit_name=unit_name, memory=memory, rng=rng)
        day_plan["offset"] = int(rel)
        horizon.append(day_plan)
    _apply_day_hrv_chain(horizon, signals, current_idx=3)
    return horizon


def _hrv_reason_lines(signals: dict[str, Any]) -> tuple[str, str]:
    rm = signals.get("rmssd_today")
    rml = signals.get("rmssd_low")
    rmh = signals.get("rmssd_high")
    rh = signals.get("rhr_today")
    rhl = signals.get("rhr_low")
    rhh = signals.get("rhr_high")

    if rm is not None and rml is not None and rmh is not None and rh is not None and rhl is not None and rhh is not None:
        rm_ok = rml <= rm <= rmh
        rh_ok = rhl <= rh <= rhh
        if rm_ok and rh_ok:
            return (
                f"RMSSD {int(round(rm))} ms im Normalbereich ({int(round(rml))}–{int(round(rmh))}), RHR {int(round(rh))} bpm normal.",
                "Progression ist möglich, heute mit kontrollierten Satzwerten.",
            )
        if (not rm_ok) and rh > rhh:
            return (
                f"RMSSD {int(round(rm))} ms unter üblich ({int(round(rml))}–{int(round(rmh))}) und RHR {int(round(rh))} bpm höher als üblich.",
                "Heute konservativer planen, um Overreach zu vermeiden.",
            )
        return (
            f"RMSSD {int(round(rm))} ms und RHR {int(round(rh))} bpm sind gemischt gegenüber deinem Normalbereich.",
            "Heute kontrolliert steuern und nur moderate Progression zulassen.",
        )

    return (
        "Zu wenig belastbare HRV-Daten für eine harte Progression.",
        "CORE plant heute konservativer, bis das Signal wieder sauber ist.",
    )


def _build_payload(
    *,
    session_mode: str,
    mission: dict[str, Any],
    step_no: int,
    signals: dict[str, Any],
    horizon: list[dict[str, Any],],
) -> dict[str, Any]:
    current_idx = 3
    today_plan = horizon[current_idx]
    chain = _context_chain(horizon, current_idx)

    rmssd_today = today_plan.get("hrv_rmssd") if today_plan.get("hrv_rmssd") is not None else signals.get("rmssd_today")
    rmssd_low = signals.get("rmssd_low")
    rmssd_high = signals.get("rmssd_high")
    rhr_today = today_plan.get("hrv_rhr") if today_plan.get("hrv_rhr") is not None else signals.get("rhr_today")
    rhr_low = signals.get("rhr_low")
    rhr_high = signals.get("rhr_high")

    situation = []
    if rmssd_today is not None and rmssd_low is not None and rmssd_high is not None:
        situation.append(f"RMSSD: {int(round(rmssd_today))} ms (üblich {int(round(rmssd_low))}–{int(round(rmssd_high))})")
    else:
        situation.append("RMSSD: zu wenig Daten")

    if rhr_today is not None and rhr_low is not None and rhr_high is not None:
        situation.append(f"RHR: {int(round(rhr_today))} bpm (üblich {int(round(rhr_low))}–{int(round(rhr_high))})")
    else:
        situation.append("RHR: zu wenig Daten")

    chain_window = [h for h in horizon if int(h.get("offset") or 0) in (-3, -2, -1, 0)]
    chain_txt = []
    for day in chain_window:
        rm = day.get("hrv_rmssd")
        rh = day.get("hrv_rhr")
        if rm is None or rh is None:
            continue
        chain_txt.append(f"{day.get('day')} {int(round(float(rm)))}ms/{int(round(float(rh)))}bpm")
    if chain_txt:
        situation.append("4T-Chain: " + " · ".join(chain_txt))
    else:
        since = signals.get("days_since_workout")
        last_txt = f"vor {int(since)} Tagen" if since is not None else "unbekannt"
        situation.append(f"Letztes Krafttraining: {last_txt} | Runs: {int(signals.get('runs_7d') or 0)} in 7 Tagen")

    goal_line = "Ziel: Progression nutzen, ohne Risiko zu erhöhen"
    if session_mode == "replay":
        goal_line = "Ziel: Rückkehr stabilisieren, ohne Overreach"
    elif session_mode == "priority":
        goal_line = "Ziel: Konflikte klar priorisieren, Leistung stabil halten"

    kind = today_plan["kind"]
    dflt = today_plan["defaults"]

    prev_plan = horizon[current_idx - 1] if len(horizon) > current_idx else None
    prev_is_long_run = bool(
        isinstance(prev_plan, dict)
        and prev_plan.get("kind") == "run"
        and (
            "40" in str(prev_plan.get("unit") or "").lower()
            or "interval" in str(prev_plan.get("unit") or "").lower()
            or int((prev_plan.get("defaults") or {}).get("run_duration_min") or 0) >= 40
        )
    )
    legs_saver_pattern = kind == "strength" and "lower" in str(today_plan.get("unit") or "").lower() and (prev_is_long_run or chain.get("hard_cluster_score", 0) >= 2)

    if kind == "strength":
        proposal_lines = [
            f"Heute: {today_plan['unit']}",
            f"Volumen {dflt['volume_delta']:+d} Satz gegenüber Standard",
            "Sätze präzise steuern: Reps, Gewicht und RPE pro Satz.",
            "Technik stabil, Progression kontrolliert.",
        ]
        if legs_saver_pattern:
            proposal_lines[1] = "Pattern erkannt: langer Lauf + Lower → Beine heute schonen, Volumen leicht runter."
            proposal_lines[3] = "Backoffs sauber, keine harten Topsatz-Exzesse."
    elif kind == "run":
        proposal_lines = [
            f"Heute: {today_plan['unit']}",
            f"Dauer: {dflt['run_duration_min']} min | Puls: {dflt['pulse_low']}–{dflt['pulse_high']} bpm",
            "Pace ruhig halten, keine zusätzliche Härte heute",
            "Fokus: sauberer Rhythmus und kontrollierte Atmung.",
        ]
    else:
        proposal_lines = [
            f"Heute: {today_plan['unit']}",
            "Aktive Erholung + Schlaf priorisieren",
            "Morgen mit kontrolliertem Einstieg zurückkehren",
            "Bewegung locker, keine harte Zusatzlast.",
        ]

    r1, r2 = _hrv_reason_lines(signals)

    timeline_rows = [
        {
            "day": f"{item['day']} · {item['unit']}",
            "unit": item["unit"],
            "plan": f"{item['summary']} · RMSSD {int(round(float(item.get('hrv_rmssd') or rmssd_today or 0)))} ms · RHR {int(round(float(item.get('hrv_rhr') or rhr_today or 0)))} bpm",
            "day_id": item.get("date"),
            "position": "current" if int(item.get("offset") or 0) == 0 else ("past" if int(item.get("offset") or 0) < 0 else "future"),
        }
        for item in horizon
    ]

    return {
        "title": f"Heute: {today_plan['day']} · {today_plan['unit']}",
        "mode": session_mode,
        "card_type": "day_simulation",
        "step_no": step_no,
        "mission": mission,
        "timeline_title": "7-Tage-Fenster (laufend)",
        "timeline_hint": "3 vergangene · heute (Mitte) · 3 kommende. Bei jedem Schritt rückt das Fenster um 1 Tag.",
        "timeline_rows": timeline_rows,
        "situation_lines": situation[:3],
        "goal_line": goal_line,
        "proposal_lines": proposal_lines[:4],
        "reason_lines": [r1, r2],
        "exercise_cards": today_plan.get("exercise_cards") or [],
        "controls": {
            "type": kind,
            "defaults": dflt,
        },
        "context_chain": chain,
        "hrv": {
            "rmssd_today": rmssd_today,
            "rmssd_low": rmssd_low,
            "rmssd_high": rmssd_high,
            "rhr_today": rhr_today,
            "rhr_low": rhr_low,
            "rhr_high": rhr_high,
            "signal_quality": str(signals.get("signal_quality") or "mittel"),
        },
        "rmssd_series": signals.get("rmssd_series") or [],
        "rhr_series": signals.get("rhr_series") or [],
        "sleep_series": signals.get("sleep_series") or [],
        "weight_series": signals.get("weight_series") or [],
        "kcal_series": [],
        "confidence": int(64),
        "allowed_labels": ["accept", "tighten", "ease", "reroute"],
        "label_labels": {
            "accept": "Passt",
            "tighten": "Zu aggressiv",
            "ease": "Zu konservativ",
            "reroute": "Andere Struktur",
        },
        "query_prompt": "",  # intentionally unused in UI
        "ask_confidence": True,
        "allow_free_text": False,
        "signature": f"sim|{session_mode}|step:{step_no}|unit:{today_plan['unit']}",
    }


def next_core_training_card(session_id: int) -> dict[str, Any]:
    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)

    sess = conn.execute(
        "SELECT id, mode, mission_json, step_index, labels_count, ended_at FROM core_lab_sessions WHERE id=?",
        (int(session_id),),
    ).fetchone()
    if sess is None:
        conn.close()
        raise ValueError("session_not_found")
    if sess["ended_at"]:
        conn.close()
        raise ValueError("session_ended")

    step_no = int(sess["step_index"] or 0) + 1

    existing = conn.execute(
        "SELECT id, payload_json FROM core_lab_steps WHERE session_id=? AND step_no=?",
        (int(session_id), step_no),
    ).fetchone()

    if existing is None:
        mission = _safe_json_load(sess["mission_json"], {})
        memory = _load_memory(conn)
        signals = _fetch_signals()
        rng = _seeded_rng(int(session_id), step_no)
        horizon = _build_horizon(str(sess["mode"]), mission, step_no, memory, signals, rng)
        payload = _build_payload(
            session_mode=str(sess["mode"]),
            mission=mission,
            step_no=step_no,
            signals=signals,
            horizon=horizon,
        )

        now = _now_iso()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO core_lab_steps (session_id, step_no, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (int(session_id), step_no, _json_dumps(payload), now),
        )
        step_id = int(cur.lastrowid)
        conn.execute("UPDATE core_lab_sessions SET step_index=? WHERE id=?", (step_no, int(session_id)))
        conn.commit()
    else:
        step_id = int(existing["id"])
        payload = _safe_json_load(existing["payload_json"], {})

    labels = int(sess["labels_count"] or 0)
    conn.close()

    return {
        "card": {
            "id": step_id,
            "mode": str(sess["mode"]),
            "card_type": "day_simulation",
            "payload": payload,
        },
        "session": {
            "id": int(sess["id"]),
            "mode": str(sess["mode"]),
            "cards_seen": step_no,
            "labels_given": labels,
        },
    }


# -------------------------
# Feedback + Learning
# -------------------------

def _parse_set_line_compact(line: str) -> dict[str, Any] | None:
    raw = _normalize_text(line).replace(",", ".")
    if not raw or " x " not in raw:
        return None
    left, right = raw.split(" x ", 1)
    reps: list[int] = []
    for part in left.split("/"):
        try:
            reps.append(_clamp_int(part.strip(), 1, 40))
        except Exception:
            continue
    if not reps:
        return None

    weights_txt = right
    rpe_txt = ""
    if " @ " in right:
        weights_txt, rpe_txt = right.split(" @ ", 1)
    weights_txt = weights_txt.replace("kg", "").strip()

    weights: list[float] = []
    for part in weights_txt.split("/"):
        v = _parse_num(part.strip())
        if v is None:
            continue
        weights.append(round(_clamp_float(v, 0.0, 500.0), 1))
    if not weights:
        return None

    rpes: list[float] = []
    if rpe_txt:
        for part in rpe_txt.split("/"):
            v = _parse_num(part.strip())
            if v is None:
                continue
            rpes.append(round(_clamp_float(v, 0.0, 10.0), 1))

    return {"reps": reps, "weights": weights, "rpes": rpes}


def _parse_plan_text_blocks(text: str) -> dict[str, dict[str, Any]]:
    lines = [str(x).strip() for x in str(text or "").splitlines()]
    entries: dict[str, dict[str, Any]] = {}
    i = 0
    while i < len(lines):
        name = lines[i]
        if not name or name.startswith("Heute:") or name.startswith("Letzte Ausführung:") or name.startswith("Planbasis:"):
            i += 1
            continue
        j = i + 1
        today_line = None
        last_line = None
        while j < len(lines):
            cur = lines[j]
            if not cur:
                break
            if cur == "—":
                j += 1
                continue
            parsed = _parse_set_line_compact(cur)
            if parsed:
                if today_line is None:
                    today_line = parsed
                else:
                    last_line = parsed
                    break
            j += 1
        if today_line:
            key = _normalize_text(name)
            entries[key] = {"name": name, "today": today_line, "last": last_line}
        i = max(i + 1, j + 1)
    return entries


def _extract_text_override_deltas(original_text: str, edited_text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = context if isinstance(context, dict) else {}
    old_map = _parse_plan_text_blocks(original_text)
    new_map = _parse_plan_text_blocks(edited_text)
    if not old_map or not new_map:
        return {"has_changes": False}

    def _avg(vals: list[float]) -> float:
        return (sum(vals) / max(1, len(vals))) if vals else 0.0

    def _backoff_count(weights: list[float]) -> int:
        if not weights:
            return 0
        top = max(weights)
        return sum(1 for w in weights if w <= top - 2.5)

    set_delta = 0
    w_sum = 0.0
    w_n = 0
    r_sum = 0.0
    r_n = 0
    first_name = ""
    rep_sum = 0.0
    rep_n = 0
    backoff_delta_total = 0
    intents: list[dict[str, Any]] = []

    for key, old_ex in old_map.items():
        new_ex = new_map.get(key)
        if not new_ex:
            continue
        old_today = old_ex.get("today") if isinstance(old_ex.get("today"), dict) else {}
        new_today = new_ex.get("today") if isinstance(new_ex.get("today"), dict) else {}
        old_reps = old_today.get("reps") if isinstance(old_today.get("reps"), list) else []
        new_reps = new_today.get("reps") if isinstance(new_today.get("reps"), list) else []
        old_w = old_today.get("weights") if isinstance(old_today.get("weights"), list) else []
        new_w = new_today.get("weights") if isinstance(new_today.get("weights"), list) else []
        old_r = old_today.get("rpes") if isinstance(old_today.get("rpes"), list) else []
        new_r = new_today.get("rpes") if isinstance(new_today.get("rpes"), list) else []
        old_rep = old_today.get("reps") if isinstance(old_today.get("reps"), list) else []
        new_rep = new_today.get("reps") if isinstance(new_today.get("reps"), list) else []

        ex_set_delta = len(new_reps) - len(old_reps)
        set_delta += ex_set_delta
        pair_w = min(len(old_w), len(new_w))
        pair_r = min(len(old_r), len(new_r))
        pair_rep = min(len(old_rep), len(new_rep))
        ex_w = 0.0
        ex_w_n = 0
        ex_r = 0.0
        ex_r_n = 0
        ex_rep = 0.0
        ex_rep_n = 0
        for idx in range(pair_w):
            d = float(new_w[idx]) - float(old_w[idx])
            w_sum += d
            ex_w += d
            w_n += 1
            ex_w_n += 1
        for idx in range(pair_r):
            d = float(new_r[idx]) - float(old_r[idx])
            r_sum += d
            ex_r += d
            r_n += 1
            ex_r_n += 1
        for idx in range(pair_rep):
            d = float(new_rep[idx]) - float(old_rep[idx])
            rep_sum += d
            rep_n += 1
            ex_rep += d
            ex_rep_n += 1

        backoff_delta = _backoff_count(new_w) - _backoff_count(old_w)
        backoff_delta_total += backoff_delta
        ex_w_avg = ex_w / max(1, ex_w_n)
        ex_r_avg = ex_r / max(1, ex_r_n)
        ex_rep_avg = ex_rep / max(1, ex_rep_n) if pair_rep else 0.0

        if ex_set_delta or ex_w_n or ex_r_n or backoff_delta:
            if ex_set_delta > 0 and ex_w_avg >= 0.8:
                intent = "aggressiver_progress"
            elif ex_set_delta < 0 and ex_w_avg <= -0.8:
                intent = "konservativer_reset"
            elif backoff_delta > 0:
                intent = "extra_backoffs"
            elif ex_r_avg <= -0.5:
                intent = "rpe_entlastung"
            elif ex_r_avg >= 0.5:
                intent = "rpe_push"
            elif ex_w_avg >= 1.0:
                intent = "gewicht_push"
            elif ex_w_avg <= -1.0:
                intent = "gewicht_entlastung"
            else:
                intent = "feintuning"
            motive = "feintuning"
            if intent in {"konservativer_reset", "rpe_entlastung", "gewicht_entlastung"}:
                if bool(ctx.get("long_run_prev")) or int(ctx.get("hard_cluster_score") or 0) >= 2:
                    motive = "ermuedungsmanagement_nach_cluster"
                elif bool(ctx.get("rmssd_low_signal")):
                    motive = "recovery_guard_bei_signalstress"
                else:
                    motive = "risikoreduktion"
            elif intent in {"aggressiver_progress", "rpe_push", "gewicht_push"}:
                if bool(ctx.get("rmssd_good_signal")) and not bool(ctx.get("long_run_prev")):
                    motive = "leistungsfenster_nutzen"
                else:
                    motive = "progression_trotz_mischsignal"
            elif intent == "extra_backoffs":
                motive = "technik_und_qualitaet_absichern"

            intents.append({
                "name": str(new_ex.get("name") or old_ex.get("name") or "Übung"),
                "intent": intent,
                "motive": motive,
                "set_delta": ex_set_delta,
                "avg_weight_delta": ex_w_avg,
                "avg_rpe_delta": ex_r_avg,
                "avg_rep_delta": ex_rep_avg,
                "backoff_delta": backoff_delta,
            })

        if not first_name and (pair_w > 0 or pair_r > 0 or len(new_reps) != len(old_reps)):
            first_name = str(new_ex.get("name") or old_ex.get("name") or "")

    if w_n == 0 and r_n == 0 and set_delta == 0 and backoff_delta_total == 0:
        return {"has_changes": False}

    return {
        "has_changes": True,
        "first_name": first_name or "Übung",
        "set_delta": int(set_delta),
        "avg_weight_delta": float(w_sum / max(1, w_n)),
        "avg_rpe_delta": float(r_sum / max(1, r_n)),
        "avg_rep_delta": float(rep_sum / max(1, rep_n)),
        "backoff_delta": int(backoff_delta_total),
        "exercise_intents": intents[:6],
    }


def _apply_memory_from_feedback(mem: dict[str, float], action: str, depth: int, controls: dict[str, Any], adjustments: dict[str, Any]) -> tuple[dict[str, float], list[str], str]:
    scale = 0.06 * max(1, depth)
    deltas: list[str] = []
    hint = "Änderung übernommen"

    if action == "tighten":  # in UX this means "zu aggressiv"
        mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) - scale, -2.0, 2.0)
        mem["volume_bias"] = _clamp_float(mem.get("volume_bias", 0.0) - scale, -3.0, 3.0)
    elif action == "ease":  # in UX this means "zu konservativ"
        mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) + scale, -2.0, 2.0)
        mem["volume_bias"] = _clamp_float(mem.get("volume_bias", 0.0) + scale, -3.0, 3.0)
    elif action == "reroute":
        mem["autonomy_bias"] = _clamp_float(mem.get("autonomy_bias", 0.0) + scale, -2.0, 2.0)
    else:  # accept
        mem["autonomy_bias"] = _clamp_float(mem.get("autonomy_bias", 0.0) - 0.03 * max(1, depth), -2.0, 2.0)

    ex_changes = adjustments.get("exercise_overrides")
    if isinstance(ex_changes, list) and ex_changes:
        set_delta_total = 0
        weight_delta_sum = 0.0
        weight_delta_n = 0
        rpe_delta_sum = 0.0
        rpe_delta_n = 0
        first_name = None

        for ex in ex_changes:
            if not isinstance(ex, dict):
                continue
            name = _normalize_text(ex.get("name"))
            first_name = first_name or name or "Übung"
            proposed = ex.get("proposed_sets") if isinstance(ex.get("proposed_sets"), list) else []
            edited = ex.get("edited_sets") if isinstance(ex.get("edited_sets"), list) else []
            set_delta_total += len(edited) - len(proposed)
            pairs = min(len(proposed), len(edited))
            for i in range(pairs):
                ps = proposed[i] if isinstance(proposed[i], dict) else {}
                es = edited[i] if isinstance(edited[i], dict) else {}
                pw = _parse_num(ps.get("weight_kg"))
                ew = _parse_num(es.get("weight_kg"))
                if pw is not None and ew is not None:
                    weight_delta_sum += ew - pw
                    weight_delta_n += 1
                pr = _parse_num(ps.get("rpe"))
                er = _parse_num(es.get("rpe"))
                if pr is not None and er is not None:
                    rpe_delta_sum += er - pr
                    rpe_delta_n += 1

        avg_w = weight_delta_sum / max(1, weight_delta_n)
        avg_r = rpe_delta_sum / max(1, rpe_delta_n)

        mem["volume_bias"] = _clamp_float(mem.get("volume_bias", 0.0) + set_delta_total * 0.05, -3.0, 3.0)
        mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) + avg_r * 0.18 + avg_w * 0.02, -2.0, 2.0)

        w_txt = f"{avg_w:+.1f} kg"
        s_txt = f"{set_delta_total:+d} Satz"
        hint = f"Änderung übernommen: {first_name} {s_txt}, Ø {w_txt}."

    source = str(adjustments.get("source") or "").strip().lower()
    if source == "next_session_text":
        original = _normalize_text(adjustments.get("original_plan_text"))
        edited = _normalize_text(adjustments.get("edited_plan_text"))
        if edited and edited != original:
            mem["autonomy_bias"] = _clamp_float(mem.get("autonomy_bias", 0.0) + 0.04, -2.0, 2.0)
            ctx = adjustments.get("__context") if isinstance(adjustments.get("__context"), dict) else {}
            parsed = _extract_text_override_deltas(adjustments.get("original_plan_text") or "", adjustments.get("edited_plan_text") or "", ctx)
            if parsed.get("has_changes"):
                set_d = int(parsed.get("set_delta") or 0)
                w_d = float(parsed.get("avg_weight_delta") or 0.0)
                r_d = float(parsed.get("avg_rpe_delta") or 0.0)
                rep_d = float(parsed.get("avg_rep_delta") or 0.0)
                bo_d = int(parsed.get("backoff_delta") or 0)
                mem["volume_bias"] = _clamp_float(mem.get("volume_bias", 0.0) + set_d * 0.05 + bo_d * 0.05, -3.0, 3.0)
                mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) + r_d * 0.22 + w_d * 0.02 + rep_d * 0.015, -2.0, 2.0)
                intents = parsed.get("exercise_intents") if isinstance(parsed.get("exercise_intents"), list) else []
                for ex in intents:
                    if not isinstance(ex, dict):
                        continue
                    motive = str(ex.get("motive") or "")
                    if motive in {"ermuedungsmanagement_nach_cluster", "recovery_guard_bei_signalstress"}:
                        mem["fatigue_chain_bias"] = _clamp_float(mem.get("fatigue_chain_bias", 0.0) + 0.08, -3.0, 3.0)
                        mem["recovery_guard_bias"] = _clamp_float(mem.get("recovery_guard_bias", 0.0) + 0.08, -3.0, 3.0)
                    elif motive in {"leistungsfenster_nutzen", "progression_trotz_mischsignal"}:
                        mem["performance_push_bias"] = _clamp_float(mem.get("performance_push_bias", 0.0) + 0.06, -3.0, 3.0)
                    elif motive == "technik_und_qualitaet_absichern":
                        mem["quality_focus_bias"] = _clamp_float(mem.get("quality_focus_bias", 0.0) + 0.07, -3.0, 3.0)
                if bo_d > 0:
                    deltas.append("Backoff-Fokus: höher")
                elif bo_d < 0:
                    deltas.append("Backoff-Fokus: niedriger")
                if r_d >= 0.4:
                    deltas.append("Intensität: höher")
                elif r_d <= -0.4:
                    deltas.append("Intensität: niedriger")

                intents = parsed.get("exercise_intents") if isinstance(parsed.get("exercise_intents"), list) else []
                if intents:
                    bits: list[str] = []
                    for ex in intents[:2]:
                        if not isinstance(ex, dict):
                            continue
                        nm = str(ex.get("name") or "Übung")
                        kind = str(ex.get("intent") or "")
                        if kind == "aggressiver_progress":
                            bits.append(f"{nm}: aggressiver Progress")
                        elif kind == "konservativer_reset":
                            bits.append(f"{nm}: konservativer")
                        elif kind == "extra_backoffs":
                            bits.append(f"{nm}: extra Backoffs")
                        elif kind == "rpe_entlastung":
                            bits.append(f"{nm}: RPE runter")
                        elif kind == "rpe_push":
                            bits.append(f"{nm}: RPE hoch")
                        elif kind == "gewicht_push":
                            bits.append(f"{nm}: Gewicht hoch")
                        elif kind == "gewicht_entlastung":
                            bits.append(f"{nm}: Gewicht runter")
                    if bits:
                        hint = f"Änderung übernommen: {'; '.join(bits)}."
                    else:
                        hint = f"Änderung übernommen: {parsed.get('first_name') or 'Übung'} {set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE."
                else:
                    hint = f"Änderung übernommen: {parsed.get('first_name') or 'Übung'} {set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE."
            else:
                diff = abs(len(edited) - len(original))
                if action == "tighten":
                    mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) - 0.05, -2.0, 2.0)
                elif action == "ease":
                    mem["strictness_bias"] = _clamp_float(mem.get("strictness_bias", 0.0) + 0.05, -2.0, 2.0)
                hint = f"Änderung übernommen: Plantext angepasst ({max(1, round(diff / 16))} Edits)."

    pulse_low = _parse_num(adjustments.get("pulse_low"))
    pulse_high = _parse_num(adjustments.get("pulse_high"))
    if pulse_low is not None and pulse_high is not None:
        shift = (pulse_low + pulse_high) / 2.0 - 140.0
        mem["run_bias"] = _clamp_float(mem.get("run_bias", 0.0) + shift / 20.0, -3.0, 3.0)
        hint = f"Änderung übernommen: Z2-Bereich {int(pulse_low)}–{int(pulse_high)} bpm"

    duration = _parse_num(adjustments.get("run_duration_min"))
    if duration is not None:
        mem["run_bias"] = _clamp_float(mem.get("run_bias", 0.0) + (duration - 30.0) / 80.0, -3.0, 3.0)

    # profile delta snippets
    if mem.get("strictness_bias", 0.0) > 0.4:
        deltas.append("Eingriffstiefe: höher")
    elif mem.get("strictness_bias", 0.0) < -0.4:
        deltas.append("Eingriffstiefe: niedriger")

    if mem.get("autonomy_bias", 0.0) > 0.4:
        deltas.append("Autonomie: mehr Freiraum")

    if mem.get("run_bias", 0.0) < -0.5:
        deltas.append("Run-Steuerung: konservativer")

    return mem, deltas[:3], hint


def _update_profile_from_memory(conn: sqlite3.Connection, mem: dict[str, float]) -> None:
    row = _profile_row(conn)
    profile = _serialize_profile(row)

    strictness = _clamp_int(round(2 + mem.get("strictness_bias", 0.0)), 0, 4)
    autonomy = _clamp_int(round(2 + mem.get("autonomy_bias", 0.0)), 0, 4)

    tone = str(profile["tone_style"])
    if mem.get("tone_bias", 0.0) > 0.8:
        tone = "ultra_short"
    elif mem.get("tone_bias", 0.0) < -0.8:
        tone = "analytic"
    else:
        tone = "calm_direct"

    weights = dict(profile["goal_weights_json"])
    weights["run"] = _clamp_float(weights.get("run", 0.20) + mem.get("run_bias", 0.0) * 0.01, 0.08, 0.45)
    weights = _normalize_goal_weights(weights)

    conn.execute(
        "UPDATE core_profile SET updated_at=?, strictness_level=?, autonomy_level=?, tone_style=?, goal_weights_json=? WHERE id=1",
        (_now_iso(), strictness, autonomy, tone, _json_dumps(weights)),
    )


def submit_core_training_label(
    *,
    session_id: int,
    card_id: int,
    label: str,
    confidence_user: Any = None,
    free_text: str | None = None,
    adjustments: Any = None,
) -> dict[str, Any]:
    ensure_core_training_schema()

    action = str(label or "").strip().lower()
    if action not in _ACTIONS:
        raise ValueError("invalid_label")

    conn = _open_db_rw(get_core_db)

    sess = conn.execute(
        "SELECT id, mode, labels_count, ended_at FROM core_lab_sessions WHERE id=?",
        (int(session_id),),
    ).fetchone()
    if sess is None:
        conn.close()
        raise ValueError("session_not_found")
    if sess["ended_at"]:
        conn.close()
        raise ValueError("session_ended")

    step = conn.execute(
        "SELECT id, payload_json FROM core_lab_steps WHERE id=? AND session_id=?",
        (int(card_id), int(session_id)),
    ).fetchone()
    if step is None:
        conn.close()
        raise ValueError("card_not_found")

    payload = _safe_json_load(step["payload_json"], {})
    controls = payload.get("controls") if isinstance(payload, dict) else {}
    if not isinstance(controls, dict):
        controls = {}
    defaults = controls.get("defaults") if isinstance(controls.get("defaults"), dict) else {}

    adj = adjustments if isinstance(adjustments, dict) else {}

    # Merge incoming adjustments over defaults.
    effective_adj = dict(defaults)
    for k, v in adj.items():
        effective_adj[k] = v

    hrv_ctx = payload.get("hrv") if isinstance(payload.get("hrv"), dict) else {}
    chain_ctx = payload.get("context_chain") if isinstance(payload.get("context_chain"), dict) else {}
    unit_txt = str(payload.get("title") or "")
    rm_today = _parse_num(hrv_ctx.get("rmssd_today"))
    rm_lo = _parse_num(hrv_ctx.get("rmssd_low"))
    rm_hi = _parse_num(hrv_ctx.get("rmssd_high"))
    rm_good = False
    rm_low = False
    if rm_today is not None and rm_lo is not None and rm_hi is not None:
        rm_good = rm_lo <= rm_today <= rm_hi
        rm_low = rm_today < rm_lo
    effective_adj["__context"] = {
        "unit": unit_txt,
        "long_run_prev": bool(chain_ctx.get("long_run_prev")),
        "hard_cluster_score": int(chain_ctx.get("hard_cluster_score") or 0),
        "rmssd_good_signal": bool(rm_good),
        "rmssd_low_signal": bool(rm_low),
    }

    depth = _clamp_int(confidence_user if confidence_user is not None else 3, 1, 5)

    mem = _load_memory(conn)
    mem, profile_delta, next_hint = _apply_memory_from_feedback(mem, action, depth, controls, effective_adj)
    hrv = payload.get("hrv") if isinstance(payload, dict) and isinstance(payload.get("hrv"), dict) else {}
    rmssd_txt = hrv.get("rmssd_today")
    rmssd_info = "RMSSD ohne klare Daten"
    if rmssd_txt is not None:
        try:
            rmssd_info = f"RMSSD {int(round(float(rmssd_txt)))} ms"
        except Exception:
            rmssd_info = "RMSSD ohne klare Daten"

    ex_over = effective_adj.get("exercise_overrides")
    learning_lines: list[str] = []
    learning_lines.append(f"Kontext: {rmssd_info}.")
    source = str(effective_adj.get("source") or "").strip().lower()
    if source == "next_session_text":
        parsed = _extract_text_override_deltas(
            effective_adj.get("original_plan_text") or "",
            effective_adj.get("edited_plan_text") or "",
            effective_adj.get("__context") if isinstance(effective_adj.get("__context"), dict) else {},
        )
        if parsed.get("has_changes"):
            intents = parsed.get("exercise_intents") if isinstance(parsed.get("exercise_intents"), list) else []
            if intents:
                for ex in intents[:2]:
                    if not isinstance(ex, dict):
                        continue
                    nm = str(ex.get("name") or "Übung")
                    kind = str(ex.get("intent") or "")
                    motive = str(ex.get("motive") or "")
                    sd = int(ex.get("set_delta") or 0)
                    wd = float(ex.get("avg_weight_delta") or 0.0)
                    rd = float(ex.get("avg_rpe_delta") or 0.0)
                    bd = int(ex.get("backoff_delta") or 0)
                    if kind == "aggressiver_progress":
                        learning_lines.append(f"{nm}: aggressiver Progress ({sd:+d} Satz, Ø {wd:+.1f} kg, Ø {rd:+.1f} RPE).")
                    elif kind == "konservativer_reset":
                        learning_lines.append(f"{nm}: konservativer Reset ({sd:+d} Satz, Ø {wd:+.1f} kg, Ø {rd:+.1f} RPE).")
                    elif kind == "extra_backoffs":
                        learning_lines.append(f"{nm}: extra Backoffs ({bd:+d}), Laststeuerung feiner.")
                    elif kind == "rpe_entlastung":
                        learning_lines.append(f"{nm}: du planst niedrigere RPE ({rd:+.1f}) bei ähnlicher Last.")
                    elif kind == "rpe_push":
                        learning_lines.append(f"{nm}: du pushst RPE höher ({rd:+.1f}).")
                    elif kind == "gewicht_push":
                        learning_lines.append(f"{nm}: du erhöhst Gewicht ({wd:+.1f} kg).")
                    elif kind == "gewicht_entlastung":
                        learning_lines.append(f"{nm}: du reduzierst Gewicht ({wd:+.1f} kg).")
                    if motive:
                        learning_lines.append(f"Motiv erkannt: {motive.replace('_', ' ')}.")
                name = str(parsed.get("first_name") or "Übung")
                set_d = int(parsed.get("set_delta") or 0)
                w_d = float(parsed.get("avg_weight_delta") or 0.0)
                r_d = float(parsed.get("avg_rpe_delta") or 0.0)
                learning_lines.append(f"Gesamtmuster: {name} {set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE.")
                next_hint = f"Gelernt: {name} {set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE."
            else:
                name = str(parsed.get("first_name") or "Übung")
                set_d = int(parsed.get("set_delta") or 0)
                w_d = float(parsed.get("avg_weight_delta") or 0.0)
                r_d = float(parsed.get("avg_rpe_delta") or 0.0)
                learning_lines.append(f"{name}: Textänderung erkannt ({set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE).")
                next_hint = f"Gelernt: bei {rmssd_info} willst du {name} {set_d:+d} Satz, Ø {w_d:+.1f} kg, Ø {r_d:+.1f} RPE."

    if isinstance(ex_over, list) and ex_over:
        first = ex_over[0] if isinstance(ex_over[0], dict) else {}
        n = _normalize_text(first.get("name")) or "Übung"
        proposed = first.get("proposed_sets") if isinstance(first.get("proposed_sets"), list) else []
        edited = first.get("edited_sets") if isinstance(first.get("edited_sets"), list) else []
        set_delta = len(edited) - len(proposed)
        w_delta = 0.0
        n_pairs = 0
        for i in range(min(len(proposed), len(edited))):
            ps = proposed[i] if isinstance(proposed[i], dict) else {}
            es = edited[i] if isinstance(edited[i], dict) else {}
            pw = _parse_num(ps.get("weight_kg"))
            ew = _parse_num(es.get("weight_kg"))
            if pw is not None and ew is not None:
                w_delta += ew - pw
                n_pairs += 1
        w_avg = w_delta / max(1, n_pairs)
        learning_lines.append(f"Direktes Override: {n} {set_delta:+d} Satz und Ø {w_avg:+.1f} kg gegenüber CORE-Vorschlag.")
        next_hint = f"Gelernt: bei {rmssd_info} willst du {n} {set_delta:+d} Satz & Ø {w_avg:+.1f} kg."

    learning_lines.append(
        f"System-Update: Strictness {mem.get('strictness_bias', 0.0):+.2f}, "
        f"Volumen {mem.get('volume_bias', 0.0):+.2f}, "
        f"Autonomie {mem.get('autonomy_bias', 0.0):+.2f}, "
        f"Run {mem.get('run_bias', 0.0):+.2f}, "
        f"Fatigue-Chain {mem.get('fatigue_chain_bias', 0.0):+.2f}, "
        f"Recovery-Guard {mem.get('recovery_guard_bias', 0.0):+.2f}, "
        f"Performance-Push {mem.get('performance_push_bias', 0.0):+.2f}, "
        f"Quality-Focus {mem.get('quality_focus_bias', 0.0):+.2f}."
    )

    detailed_profile_delta = [ln for ln in learning_lines if ln][:8]
    _save_memory(conn, mem)
    _update_profile_from_memory(conn, mem)
    conn.execute("UPDATE core_profile SET notes=?, updated_at=? WHERE id=1", ("\n".join(detailed_profile_delta)[:2000], _now_iso()))

    now = _now_iso()
    conn.execute(
        "INSERT INTO core_lab_feedback (session_id, step_id, action, confidence_user, override_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (int(session_id), int(card_id), action, depth, _json_dumps(effective_adj), now),
    )
    conn.execute("UPDATE core_lab_sessions SET labels_count = labels_count + 1 WHERE id=?", (int(session_id),))

    labels_count = int(
        conn.execute("SELECT labels_count FROM core_lab_sessions WHERE id=?", (int(session_id),)).fetchone()["labels_count"]
    )
    accept_count = int(
        _safe_scalar(conn, "SELECT COUNT(*) FROM core_lab_feedback WHERE session_id=? AND action='accept'", (int(session_id),), 0)
    )
    agreement = int(round((accept_count / max(1, labels_count)) * 100))

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "profile_delta": detailed_profile_delta or profile_delta,
        "next_hint": next_hint,
        "session_stats": {
            "labels_this_session": labels_count,
            "agreement_rate_session": agreement,
        },
    }


# -------------------------
# Stats
# -------------------------

def get_core_training_stats() -> dict[str, Any]:
    ensure_core_training_schema()
    conn = _open_db_rw(get_core_db)

    total_sessions = int(_safe_scalar(conn, "SELECT COUNT(*) FROM core_lab_sessions", default=0))
    total_labels = int(_safe_scalar(conn, "SELECT COUNT(*) FROM core_lab_feedback", default=0))
    since = (date.today() - timedelta(days=6)).isoformat()
    labels_7d = int(_safe_scalar(conn, "SELECT COUNT(*) FROM core_lab_feedback WHERE substr(created_at,1,10) >= ?", (since,), 0))

    accept = int(_safe_scalar(conn, "SELECT COUNT(*) FROM core_lab_feedback WHERE action='accept'", default=0))
    agreement = int(round((accept / max(1, total_labels)) * 100))

    avg_depth = _safe_scalar(conn, "SELECT AVG(confidence_user) FROM core_lab_feedback WHERE confidence_user IS NOT NULL", default=3.0)
    confidence_alignment = int(_clamp_float(32 + avg_depth * 14, 20, 95))

    mem = _load_memory(conn)
    top = sorted(mem.items(), key=lambda kv: abs(float(kv[1])), reverse=True)
    top_prefs = [{"key": k, "value": round(float(v), 3)} for k, v in top[:5]]

    conn.close()

    return {
        "total_sessions": total_sessions,
        "total_labels": total_labels,
        "labels_last_7d": labels_7d,
        "agreement_rate": agreement,
        "confidence_alignment": confidence_alignment,
        "top_preferences": top_prefs,
    }
