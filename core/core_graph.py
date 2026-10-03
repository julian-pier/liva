from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from database.connections import (
    get_core_db,
    get_hrv_db,
    get_nutrition_db,
    get_plans_db,
    get_runs_db,
    get_training_db,
)

DEFAULT_DEPTH = 2
DEFAULT_MAX_NODES = 250
DEFAULT_MAX_EDGES = 600
GRAPH_CACHE_TTL_S = 25
GRAPH_CACHE_SIZE = 128

PANEL_KEYS = [
    "snapshot",
    "training",
    "recovery",
    "run",
    "ernaehrung",
    "plan",
    "autopilot",
    "insights",
]

TYPE_BASE_WEIGHT = {
    "core_root": 2.2,
    "domain_hub": 1.9,
    "domain_track": 1.65,
    "global_day": 1.5,
    "domain_day": 1.3,
    "day": 1.1,
    "gym_session": 1.4,
    "exercise": 1.7,
    "run": 1.2,
    "hrv_daily": 1.2,
    "hr_daily": 1.0,
    "meal_day": 1.1,
    "plan_block": 1.0,
    "autopilot_decision": 1.3,
    "insight": 1.2,
    "pattern": 1.45,
    "plan_session": 1.2,
}

DOMAIN_SPOKES = {
    "recovery": (-0.75, 0.7, -0.2),
    "nutrition": (0.75, 0.7, -0.2),
    "training": (-0.75, -0.72, -0.2),
    "run": (0.75, -0.72, -0.2),
    "plan": (-1.0, 0.0, -0.16),
    "autopilot": (1.0, 0.0, -0.16),
}

DOMAIN_TRACK_LABELS = {
    "training": ["Push", "Pull", "Legs"],
    "recovery": ["HRV", "Sleep", "Stress"],
    "nutrition": ["Protein", "Kcal", "Hydration"],
    "run": ["Zone2", "Intervals", "Long"],
    "plan": ["Blocks", "Week", "Session"],
    "autopilot": ["Load", "Readiness", "Decision"],
}

DOMAIN_ARM_ANGLES = {
    "training": (228.0, 18.0),
    "recovery": (142.0, 42.0),
    "nutrition": (24.0, 36.0),
    "run": (322.0, -14.0),
    "plan": (188.0, -42.0),
    "autopilot": (8.0, -34.0),
}


@dataclass
class CacheEntry:
    expires_at: float
    payload: dict[str, Any]


class TimedLRU:
    def __init__(self, maxsize: int = GRAPH_CACHE_SIZE):
        self.maxsize = max(8, int(maxsize))
        self._lock = threading.Lock()
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            row = self._store.get(key)
            if row is None:
                return None
            if row.expires_at < now:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return row.payload

    def set(self, key: str, payload: dict[str, Any], ttl_s: float) -> None:
        ttl = max(0.5, float(ttl_s))
        with self._lock:
            self._store[key] = CacheEntry(expires_at=time.time() + ttl, payload=payload)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_GRAPH_CACHE = TimedLRU()


def make_node_id(node_type: str, key: str | int) -> str:
    return f"{node_type}:{key}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _stable_unit(seed: str) -> float:
    raw = hashlib.sha1(seed.encode("utf-8")).digest()
    value = int.from_bytes(raw[:8], "big", signed=False)
    return value / float(2**64 - 1)


def _parse_ts(raw: Any) -> str:
    if raw is None:
        return ""
    txt = str(raw).strip()
    if not txt:
        return ""
    if len(txt) == 10 and txt[4] == "-" and txt[7] == "-":
        return f"{txt}T00:00:00"
    normalized = txt.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%d.%m.%y", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(txt, fmt)
            return dt.replace(tzinfo=None).isoformat(timespec="seconds")
        except Exception:
            continue
    return ""


def _ts_to_epoch(raw_ts: str) -> int:
    if not raw_ts:
        return 0
    try:
        return int(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _normalize_day(raw: Any) -> str | None:
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    if len(txt) >= 10 and txt[4] == "-" and txt[7] == "-":
        return txt[:10]
    if "T" in txt and len(txt) >= 10:
        head = txt[:10]
        if len(head) == 10 and head[4] == "-" and head[7] == "-":
            return head
    for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(txt[:10], fmt).date().isoformat()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def _empty_panel(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "summary": "Keine Daten.",
        "metrics": [],
        "items": [],
        "details": [],
        "empty": True,
    }


def _default_panels() -> dict[str, Any]:
    return {
        "snapshot": _empty_panel("Snapshot"),
        "training": _empty_panel("Training"),
        "recovery": _empty_panel("Recovery"),
        "run": _empty_panel("Run"),
        "ernaehrung": _empty_panel("Ernaehrung"),
        "plan": _empty_panel("Plan"),
        "autopilot": _empty_panel("Autopilot"),
        "insights": _empty_panel("Insights"),
    }


def ensure_core_graph_schema() -> None:
    conn = get_core_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_edge_index (
          source TEXT NOT NULL,
          target TEXT NOT NULL,
          type TEXT NOT NULL,
          weight REAL,
          ts TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_node_index (
          id TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          title TEXT,
          subtitle TEXT,
          ts TEXT,
          weight REAL,
          json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_index_meta (
          key TEXT PRIMARY KEY,
          value TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_edge_source ON core_edge_index(source)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_edge_target ON core_edge_index(target)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_edge_type ON core_edge_index(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_node_type ON core_node_index(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_node_ts ON core_node_index(ts)")
    conn.commit()
    conn.close()


def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM core_index_meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return str(row["value"] if isinstance(row, sqlite3.Row) else row[0] or default)


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO core_index_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _index_version(conn: sqlite3.Connection) -> str:
    return _meta_get(conn, "index_built_ts", "0")


def _index_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM core_node_index").fetchone()
    return _safe_int(row[0] if row else 0)


def ensure_core_index_ready(auto_rebuild_if_empty: bool = True) -> dict[str, Any]:
    ensure_core_graph_schema()
    conn = get_core_db()
    count = _index_count(conn)
    built_ts = _index_version(conn)
    conn.close()
    if count == 0 and auto_rebuild_if_empty:
        return rebuild_core_index()
    return {"ok": True, "nodes": count, "version": built_ts, "rebuilt": False}


def rebuild_core_index() -> dict[str, Any]:
    ensure_core_graph_schema()
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    workout_day: dict[int, str] = {}
    workout_name: dict[int, str] = {}
    workout_exercises: defaultdict[int, set[int]] = defaultdict(set)
    exercise_name: dict[int, str] = {}
    exercise_variation: dict[int, str] = {}
    day_sessions: defaultdict[str, list[int]] = defaultdict(list)
    nutrition_days: set[str] = set()
    run_days: list[str] = []
    insight_by_day: defaultdict[str, list[int]] = defaultdict(list)
    domain_days: dict[str, set[str]] = {
        "recovery": set(),
        "nutrition": set(),
        "training": set(),
        "run": set(),
        "plan": set(),
        "autopilot": set(),
    }

    def upsert_node(
        node_id: str,
        node_type: str,
        title: str,
        subtitle: str = "",
        ts: str = "",
        metrics: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        base_weight: float | None = None,
    ) -> None:
        row = nodes.get(node_id)
        w = TYPE_BASE_WEIGHT.get(node_type, 1.0) if base_weight is None else float(base_weight)
        payload = {
            "id": node_id,
            "type": node_type,
            "title": title,
            "subtitle": subtitle,
            "ts": ts,
            "metrics": metrics or {},
            "tags": tags or [],
            "weight": w,
        }
        if row is None:
            nodes[node_id] = payload
            return
        if ts and (not row.get("ts") or ts > row.get("ts", "")):
            row["ts"] = ts
        if subtitle and not row.get("subtitle"):
            row["subtitle"] = subtitle
        if title and row.get("title", "") == node_id:
            row["title"] = title
        merged = dict(row.get("metrics") or {})
        merged.update(payload["metrics"])
        row["metrics"] = merged
        tags_set = set(row.get("tags") or [])
        tags_set.update(payload["tags"])
        row["tags"] = sorted(tags_set)
        row["weight"] = max(float(row.get("weight") or 0.0), w)

    def add_day_node(day_iso: str) -> str:
        node_id = make_node_id("day", day_iso)
        upsert_node(node_id, "day", f"Tag {day_iso}", ts=f"{day_iso}T00:00:00", metrics={"day": day_iso})
        return node_id

    def add_edge(source: str, target: str, edge_type: str, weight: float = 1.0, ts: str = "") -> None:
        if not source or not target or source == target:
            return
        edges.append(
            {
                "source": source,
                "target": target,
                "type": edge_type,
                "weight": float(weight),
                "ts": ts,
            }
        )

    # Training domain: workouts + exercises + sets + autopilot + insights.
    try:
        conn = get_training_db()
        workouts = conn.execute(
            """
            SELECT id, COALESCE(NULLIF(date_iso,''), date) AS d, name, created_at, session_key, plan_id
            FROM workouts
            ORDER BY id DESC
            LIMIT 5000
            """
        ).fetchall()
        for row in workouts:
            wid = _safe_int(row["id"])
            day = _normalize_day(row["d"]) or _normalize_day(row["created_at"]) or date.today().isoformat()
            w_ts = _parse_ts(row["created_at"]) or f"{day}T00:00:00"
            day_id = add_day_node(day)
            session_id = make_node_id("gym_session", wid)
            workout_day[wid] = day
            workout_name[wid] = str(row["name"] or f"Session {wid}")
            day_sessions[day].append(wid)
            domain_days["training"].add(day)
            upsert_node(
                session_id,
                "gym_session",
                str(row["name"] or f"Session {wid}"),
                subtitle=day,
                ts=w_ts,
                metrics={"day": day, "workout_id": wid, "session_key": row["session_key"], "plan_id": row["plan_id"]},
            )
            add_edge(day_id, session_id, "has_gym", 1.0, w_ts)
            session_key = str(row["session_key"] or "").strip()
            if session_key:
                plan_unit_id = make_node_id("planunit", session_key)
                upsert_node(
                    plan_unit_id,
                    "plan_session",
                    f"Plan-Einheit {session_key}",
                    subtitle=day,
                    ts=w_ts,
                    metrics={"session_key": session_key, "day": day, "plan_id": row["plan_id"]},
                )
                add_edge(plan_unit_id, day_id, "planned_for_day", 0.9, w_ts)
                add_edge(plan_unit_id, session_id, "matches_session", 1.25, w_ts)
                domain_days["plan"].add(day)

        exercises = conn.execute(
            """
            SELECT e.id, e.workout_id, e.name, e.variation, e.created_at
            FROM exercises e
            ORDER BY e.id DESC
            LIMIT 10000
            """
        ).fetchall()
        for row in exercises:
            ex_id = _safe_int(row["id"])
            wid = _safe_int(row["workout_id"])
            day = workout_day.get(wid)
            ex_ts = _parse_ts(row["created_at"]) or (f"{day}T00:00:00" if day else "")
            title = str(row["name"] or f"Exercise {ex_id}")
            var = str(row["variation"] or "").strip()
            subtitle = var if var else (day or "")
            node_id = make_node_id("exercise", ex_id)
            exercise_name[ex_id] = title
            exercise_variation[ex_id] = var
            if wid > 0:
                workout_exercises[wid].add(ex_id)
            upsert_node(
                node_id,
                "exercise",
                title,
                subtitle=subtitle,
                ts=ex_ts,
                metrics={"exercise_id": ex_id, "day": day, "workout_id": wid, "variation": var},
            )
            if wid:
                add_edge(make_node_id("gym_session", wid), node_id, "contains_exercise", 1.0, ex_ts)

        set_rows = conn.execute(
            """
            SELECT s.exercise_id, w.id AS workout_id, COALESCE(NULLIF(w.date_iso,''), w.date) AS d,
                   s.weight, s.reps, s.rpe
            FROM sets s
            JOIN workouts w ON w.id = s.workout_id
            ORDER BY s.id DESC
            LIMIT 20000
            """
        ).fetchall()
        ex_last_score: dict[int, float] = {}
        for row in set_rows:
            ex_id = _safe_int(row["exercise_id"])
            if ex_id <= 0:
                continue
            score = _safe_float(row["weight"]) * max(1.0, _safe_float(row["reps"]))
            prev = ex_last_score.get(ex_id)
            if prev is None or score > prev:
                ex_last_score[ex_id] = score
        for ex_id, score in ex_last_score.items():
            node_id = make_node_id("exercise", ex_id)
            if node_id in nodes:
                nodes[node_id]["metrics"]["top_score"] = round(score, 1)

        # Exercise highways: top co-occurrence pairs per exercise (max 3) + variation links (max 2).
        pair_count: defaultdict[tuple[int, int], int] = defaultdict(int)
        for ex_set in workout_exercises.values():
            seq = sorted(ex_set)
            for i in range(len(seq)):
                for j in range(i + 1, len(seq)):
                    pair_count[(seq[i], seq[j])] += 1

        per_ex_links: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
        for (a, b), count in pair_count.items():
            if count < 2:
                continue
            per_ex_links[a].append((b, count))
            per_ex_links[b].append((a, count))
        for ex_id, rels in per_ex_links.items():
            for other_id, count in sorted(rels, key=lambda x: x[1], reverse=True)[:3]:
                add_edge(
                    make_node_id("exercise", ex_id),
                    make_node_id("exercise", other_id),
                    "co_occurs",
                    min(1.8, 0.55 + count * 0.12),
                    "",
                )

        variation_groups: defaultdict[str, list[int]] = defaultdict(list)
        for ex_id, name in exercise_name.items():
            key = "".join(ch.lower() for ch in name if ch.isalnum())
            if key:
                variation_groups[key].append(ex_id)
        for ids in variation_groups.values():
            if len(ids) < 2:
                continue
            ids = sorted(ids, reverse=True)[:5]
            for ex_id in ids:
                c = 0
                for other in ids:
                    if other == ex_id:
                        continue
                    add_edge(
                        make_node_id("exercise", ex_id),
                        make_node_id("exercise", other),
                        "variant_of",
                        1.0,
                        "",
                    )
                    c += 1
                    if c >= 2:
                        break

        ap_rows = conn.execute(
            """
            SELECT date, mode, output_json, created_ts
            FROM autopilot_day
            ORDER BY date DESC
            LIMIT 730
            """
        ).fetchall()
        for row in ap_rows:
            day = _normalize_day(row["date"])
            if not day:
                continue
            ap_id = make_node_id("autopilot", day)
            ts = _parse_ts(row["created_ts"]) or f"{day}T00:00:00"
            title = f"Autopilot {day}"
            mode = str(row["mode"] or "").strip()
            upsert_node(
                ap_id,
                "autopilot_decision",
                title,
                subtitle=mode,
                ts=ts,
                metrics={"day": day, "mode": mode, "output_json": str(row["output_json"] or "")[:1200]},
            )
            day_id = add_day_node(day)
            add_edge(ap_id, day_id, "decision_for_day", 1.0, ts)
            domain_days["autopilot"].add(day)
            for wid, w_day in workout_day.items():
                if w_day == day:
                    add_edge(ap_id, make_node_id("gym_session", wid), "modified_session", 0.8, ts)

        conn.close()
    except Exception:
        pass

    # Runs domain.
    try:
        conn = get_runs_db()
        run_rows = conn.execute(
            """
            SELECT id, date, distance, moving_time, avg_hr, max_hr
            FROM runs
            ORDER BY date DESC
            LIMIT 5000
            """
        ).fetchall()
        for row in run_rows:
            rid = str(row["id"])
            day = _normalize_day(row["date"])
            if not day:
                continue
            ts = _parse_ts(row["date"]) or f"{day}T00:00:00"
            km = round(_safe_float(row["distance"]) / 1000.0, 2)
            run_id = make_node_id("run", rid)
            run_days.append(day)
            domain_days["run"].add(day)
            upsert_node(
                run_id,
                "run",
                f"Run {km} km",
                subtitle=day,
                ts=ts,
                metrics={
                    "day": day,
                    "run_id": rid,
                    "distance_m": _safe_float(row["distance"]),
                    "moving_time": _safe_int(row["moving_time"]),
                    "avg_hr": _safe_float(row["avg_hr"]),
                    "max_hr": _safe_float(row["max_hr"]),
                },
            )
            add_edge(add_day_node(day), run_id, "has_run", 1.0, ts)
            freq_node = make_node_id("pattern", "run_frequency_weekly")
            gap_node = make_node_id("pattern", "run_gap")
            upsert_node(freq_node, "pattern", "Run-Rhythmus Frequenz", subtitle="weekly frequency", ts=ts)
            upsert_node(gap_node, "pattern", "Run-Rhythmus Gap", subtitle="tage seit letztem run", ts=ts)
            add_edge(run_id, freq_node, "run_frequency", 0.7, ts)
            add_edge(run_id, gap_node, "run_gap", 0.7, ts)
            ap_id = make_node_id("autopilot", day)
            if ap_id in nodes:
                add_edge(ap_id, run_id, "modified_run", 0.85, ts)
        conn.close()
    except Exception:
        pass

    # Recovery domain.
    try:
        conn = get_hrv_db()
        day_rows = conn.execute(
            """
            SELECT substr(date_utc, 1, 10) AS d,
                   AVG(rmssd) AS rmssd,
                   AVG(hr) AS rhr,
                   COUNT(*) AS n,
                   MAX(ts_measurement) AS ts
            FROM hrv_measurements
            GROUP BY substr(date_utc, 1, 10)
            ORDER BY d DESC
            LIMIT 1500
            """
        ).fetchall()
        rmssd_vals = [_safe_float(r["rmssd"]) for r in day_rows if _safe_float(r["rmssd"]) > 0]
        rmssd_base = (sum(rmssd_vals) / len(rmssd_vals)) if rmssd_vals else 0.0
        baseline_node = make_node_id("pattern", "recovery_baseline")
        drop_node = make_node_id("pattern", "recovery_drop")
        peak_node = make_node_id("pattern", "recovery_peak")
        upsert_node(baseline_node, "pattern", "Recovery Baseline", subtitle="rmssd normalwert")
        upsert_node(drop_node, "pattern", "Recovery Drop", subtitle="deutlich unter baseline")
        upsert_node(peak_node, "pattern", "Recovery Peak", subtitle="deutlich ueber baseline")

        for row in day_rows:
            day = _normalize_day(row["d"])
            if not day:
                continue
            ts = _parse_ts(row["ts"]) or f"{day}T00:00:00"
            hrv_id = make_node_id("hrv", day)
            hr_id = make_node_id("hr", day)
            rmssd = round(_safe_float(row["rmssd"]), 1)
            rhr = round(_safe_float(row["rhr"]), 1)
            upsert_node(
                hrv_id,
                "hrv_daily",
                f"HRV {day}",
                subtitle=f"RMSSD {rmssd}",
                ts=ts,
                metrics={"day": day, "rmssd": rmssd, "samples": _safe_int(row["n"])},
            )
            upsert_node(
                hr_id,
                "hr_daily",
                f"RHR {day}",
                subtitle=f"HR {rhr}",
                ts=ts,
                metrics={"day": day, "rhr": rhr, "samples": _safe_int(row["n"])},
            )
            day_id = add_day_node(day)
            add_edge(day_id, hrv_id, "has_hrv", 1.0, ts)
            add_edge(day_id, hr_id, "has_hr", 1.0, ts)
            domain_days["recovery"].add(day)
            if rmssd_base > 0:
                ratio = rmssd / rmssd_base if rmssd > 0 else 1.0
                add_edge(hrv_id, baseline_node, "recovery_pattern", 0.72, ts)
                if ratio < 0.82:
                    add_edge(hrv_id, drop_node, "recovery_drop", 0.92, ts)
                elif ratio > 1.16:
                    add_edge(hrv_id, peak_node, "recovery_peak", 0.88, ts)
            ap_id = make_node_id("autopilot", day)
            if ap_id in nodes:
                add_edge(ap_id, hrv_id, "triggered_by_recovery", 0.9, ts)
        conn.close()
    except Exception:
        pass

    # Nutrition domain.
    try:
        conn = get_nutrition_db()
        goal_node = make_node_id("pattern", "nutrition_goal")
        protein_low_node = make_node_id("pattern", "nutrition_protein_low")
        kcal_high_node = make_node_id("pattern", "nutrition_kcal_high")
        kcal_low_node = make_node_id("pattern", "nutrition_kcal_low")
        upsert_node(goal_node, "pattern", "Ernaehrung Ziel", subtitle="kcal/protein ziel")
        upsert_node(protein_low_node, "pattern", "Protein Low", subtitle="unter ziel")
        upsert_node(kcal_high_node, "pattern", "Kcal High", subtitle="ueber ziel")
        upsert_node(kcal_low_node, "pattern", "Kcal Low", subtitle="unter ziel")

        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(date_iso,''), date) AS d, kcal, protein, carbs, fat, created_at
            FROM nutrition_daily
            ORDER BY COALESCE(date_iso, date) DESC
            LIMIT 2500
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["d"])
            if not day:
                continue
            nutrition_days.add(day)
            domain_days["nutrition"].add(day)
            ts = _parse_ts(row["created_at"]) or f"{day}T00:00:00"
            meal_id = make_node_id("meal", day)
            upsert_node(
                meal_id,
                "meal_day",
                f"Meal {day}",
                subtitle=f"{_safe_int(row['kcal'])} kcal",
                ts=ts,
                metrics={
                    "day": day,
                    "kcal": _safe_float(row["kcal"]),
                    "protein": _safe_float(row["protein"]),
                    "carbs": _safe_float(row["carbs"]),
                    "fat": _safe_float(row["fat"]),
                },
            )
            add_edge(add_day_node(day), meal_id, "has_meal", 1.0, ts)
            add_edge(meal_id, goal_node, "nutrition_goal", 0.72, ts)
            p = _safe_float(row["protein"])
            kcal = _safe_float(row["kcal"])
            if p > 0 and p < 120:
                add_edge(meal_id, protein_low_node, "protein_low", 0.88, ts)
            if kcal > 3200:
                add_edge(meal_id, kcal_high_node, "kcal_high", 0.84, ts)
            elif kcal > 0 and kcal < 2200:
                add_edge(meal_id, kcal_low_node, "kcal_low", 0.84, ts)
            ap_id = make_node_id("autopilot", day)
            if ap_id in nodes:
                add_edge(ap_id, meal_id, "modified_mealplan", 0.86, ts)

        rows2 = conn.execute(
            """
            SELECT day, kcal, p, c, f, created_at
            FROM nutrition_day_actuals
            ORDER BY day DESC
            LIMIT 2500
            """
        ).fetchall()
        for row in rows2:
            day = _normalize_day(row["day"])
            if not day:
                continue
            nutrition_days.add(day)
            domain_days["nutrition"].add(day)
            meal_id = make_node_id("meal", day)
            ts = _parse_ts(row["created_at"]) or f"{day}T00:00:00"
            upsert_node(
                meal_id,
                "meal_day",
                f"Meal {day}",
                subtitle=f"{_safe_int(row['kcal'])} kcal",
                ts=ts,
                metrics={
                    "day": day,
                    "kcal_actual": _safe_float(row["kcal"]),
                    "protein_actual": _safe_float(row["p"]),
                    "carbs_actual": _safe_float(row["c"]),
                    "fat_actual": _safe_float(row["f"]),
                },
            )
            add_edge(add_day_node(day), meal_id, "has_meal", 1.0, ts)
            add_edge(meal_id, goal_node, "nutrition_goal", 0.72, ts)
            p = _safe_float(row["p"])
            kcal = _safe_float(row["kcal"])
            if p > 0 and p < 120:
                add_edge(meal_id, protein_low_node, "protein_low", 0.88, ts)
            if kcal > 3200:
                add_edge(meal_id, kcal_high_node, "kcal_high", 0.84, ts)
            elif kcal > 0 and kcal < 2200:
                add_edge(meal_id, kcal_low_node, "kcal_low", 0.84, ts)
            ap_id = make_node_id("autopilot", day)
            if ap_id in nodes:
                add_edge(ap_id, meal_id, "modified_mealplan", 0.86, ts)
        conn.close()
    except Exception:
        pass

    # Plan domain.
    try:
        conn = get_plans_db()
        rows = conn.execute(
            """
            SELECT id, name, start_date, total_weeks, is_active, updated_at
            FROM plans
            ORDER BY id DESC
            LIMIT 400
            """
        ).fetchall()
        today = date.today()
        for row in rows:
            pid = _safe_int(row["id"])
            weeks = max(1, _safe_int(row["total_weeks"], 1))
            start_day = _normalize_day(row["start_date"])
            ts = _parse_ts(row["updated_at"]) or (f"{start_day}T00:00:00" if start_day else "")
            node_id = make_node_id("planblock", pid)
            upsert_node(
                node_id,
                "plan_block",
                str(row["name"] or f"Plan {pid}"),
                subtitle=("Aktiv" if _safe_int(row["is_active"]) else "Inaktiv"),
                ts=ts,
                metrics={
                    "plan_id": pid,
                    "start_day": start_day,
                    "total_weeks": weeks,
                    "is_active": _safe_int(row["is_active"]),
                },
            )
            if start_day:
                try:
                    start_dt = datetime.strptime(start_day, "%Y-%m-%d").date()
                except Exception:
                    start_dt = today
                end_dt = start_dt + timedelta(days=min(weeks * 7, 365))
                lower = today - timedelta(days=180)
                upper = today + timedelta(days=180)
                cursor = max(start_dt, lower)
                stop = min(end_dt, upper)
                while cursor <= stop:
                    day_iso = cursor.isoformat()
                    domain_days["plan"].add(day_iso)
                    day_id = add_day_node(day_iso)
                    add_edge(node_id, day_id, "covers_day", 0.6, f"{day_iso}T00:00:00")
                    plan_unit_id = make_node_id("planunit", f"plan{pid}:{day_iso}")
                    upsert_node(
                        plan_unit_id,
                        "plan_session",
                        f"Plan-Einheit {day_iso}",
                        subtitle=str(row["name"] or f"Plan {pid}"),
                        ts=f"{day_iso}T00:00:00",
                        metrics={"plan_id": pid, "day": day_iso},
                    )
                    add_edge(node_id, plan_unit_id, "has_planned_unit", 0.92, f"{day_iso}T00:00:00")
                    add_edge(plan_unit_id, day_id, "planned_for_day", 0.82, f"{day_iso}T00:00:00")
                    for wid in day_sessions.get(day_iso, [])[:2]:
                        add_edge(plan_unit_id, make_node_id("gym_session", wid), "planned_session", 0.95, f"{day_iso}T00:00:00")
                        for ex_id in list(workout_exercises.get(wid, set()))[:3]:
                            add_edge(node_id, make_node_id("exercise", ex_id), "plan_contains_exercise", 0.7, f"{day_iso}T00:00:00")
                    ap_id = make_node_id("autopilot", day_iso)
                    if ap_id in nodes:
                        add_edge(node_id, ap_id, "plan_autopilot_context", 0.7, f"{day_iso}T00:00:00")
                    cursor += timedelta(days=1)
        conn.close()
    except Exception:
        pass

    # Autopilot links to meal where present.
    for day in nutrition_days:
        ap_id = make_node_id("autopilot", day)
        meal_id = make_node_id("meal", day)
        if ap_id in nodes and meal_id in nodes:
            add_edge(ap_id, meal_id, "modified_mealplan", 0.7, f"{day}T00:00:00")
        for iid in insight_by_day.get(day, [])[:3]:
            add_edge(ap_id, make_node_id("insight", iid), "based_on_insight", 0.78, f"{day}T00:00:00")

    # Core tree scaffold: core root + domain hubs + global day + per-domain day timelines.
    runs_by_day: defaultdict[str, list[str]] = defaultdict(list)
    plan_sessions_by_day: defaultdict[str, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        d = _normalize_day((n.get("metrics") or {}).get("day"))
        if not d:
            continue
        if n.get("type") == "run":
            runs_by_day[d].append(nid)
        elif n.get("type") == "plan_session":
            plan_sessions_by_day[d].append(nid)

    core_root = "core:root"
    upsert_node(
        core_root,
        "core_root",
        "CORE",
        subtitle="Universe",
        ts=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        metrics={"role": "root", "layout": {"x": 0.0, "y": 0.0, "z": 0.0}},
    )
    def _basis_from_direction(vx: float, vy: float, vz: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        ref = (0.0, 0.0, 1.0) if abs(vz) < 0.86 else (0.0, 1.0, 0.0)
        b1x = vy * ref[2] - vz * ref[1]
        b1y = vz * ref[0] - vx * ref[2]
        b1z = vx * ref[1] - vy * ref[0]
        b1n = max(1e-6, math.sqrt(b1x * b1x + b1y * b1y + b1z * b1z))
        b1x, b1y, b1z = b1x / b1n, b1y / b1n, b1z / b1n
        b2x = vy * b1z - vz * b1y
        b2y = vz * b1x - vx * b1z
        b2z = vx * b1y - vy * b1x
        b2n = max(1e-6, math.sqrt(b2x * b2x + b2y * b2y + b2z * b2z))
        return (b1x, b1y, b1z), (b2x / b2n, b2y / b2n, b2z / b2n)

    domain_hub_pos: dict[str, tuple[float, float, float]] = {}
    domain_dirs: dict[str, tuple[float, float, float]] = {}
    domain_basis: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
    domain_track_nodes: dict[str, list[tuple[str, tuple[float, float, float]]]] = {}
    for domain in DOMAIN_SPOKES.keys():
        az_deg, el_deg = DOMAIN_ARM_ANGLES.get(domain, (0.0, 0.0))
        az = math.radians(az_deg)
        el = math.radians(el_deg)
        dx = math.cos(el) * math.cos(az)
        dy = math.cos(el) * math.sin(az)
        dz = math.sin(el)
        hx = dx * 290.0
        hy = dy * 290.0
        hz = dz * 290.0
        hub_id = f"domain:{domain}"
        upsert_node(
            hub_id,
            "domain_hub",
            domain.capitalize(),
            subtitle="Hub",
            metrics={"role": "hub", "domain": domain, "layout": {"x": hx, "y": hy, "z": hz}},
        )
        add_edge(core_root, hub_id, "domain_branch", 1.65, "")
        domain_hub_pos[domain] = (hx, hy, hz)
        domain_dirs[domain] = (dx, dy, dz)
        b1, b2 = _basis_from_direction(dx, dy, dz)
        domain_basis[domain] = (b1, b2)

        tracks: list[tuple[str, tuple[float, float, float]]] = []
        labels = DOMAIN_TRACK_LABELS.get(domain, [])
        for idx, label in enumerate(labels):
            spread = idx - (len(labels) - 1) / 2.0
            tx = hx + dx * 190.0 + b1[0] * spread * 88.0 + b2[0] * spread * 24.0
            ty = hy + dy * 190.0 + b1[1] * spread * 88.0 + b2[1] * spread * 24.0
            tz = hz + dz * 190.0 + b1[2] * spread * 88.0 + b2[2] * spread * 24.0
            track_id = make_node_id("track", f"{domain}:{label.lower()}")
            upsert_node(
                track_id,
                "domain_track",
                label,
                subtitle=f"{domain.capitalize()} Branch",
                metrics={"role": "track", "domain": domain, "track": label.lower(), "layout": {"x": tx, "y": ty, "z": tz}},
            )
            add_edge(hub_id, track_id, "domain_split", 1.42, "")
            tracks.append((track_id, (tx, ty, tz)))
        domain_track_nodes[domain] = tracks

    all_days = sorted({d for s in domain_days.values() for d in s if d}, reverse=True)
    day_domain_count: dict[str, int] = {}
    for d in all_days:
        day_domain_count[d] = sum(1 for ds in domain_days.values() if d in ds)
    day_rank = {d: idx for idx, d in enumerate(all_days)}
    domain_day_seq: defaultdict[str, list[str]] = defaultdict(list)
    global_day_seq: list[str] = []
    for day in all_days[:730]:
        rank = day_rank.get(day, 0)
        g_r = min(940.0, 180.0 + rank * 2.4)
        g_theta = rank * 0.31 + _stable_unit(f"global:{day}:theta") * (math.pi * 2)
        g_phi = (_stable_unit(f"global:{day}:phi") - 0.5) * 1.55
        gx = math.cos(g_theta) * math.cos(g_phi) * g_r
        gy = math.sin(g_theta) * math.cos(g_phi) * g_r
        gz = math.sin(g_phi) * g_r * 0.9 + (day_domain_count.get(day, 1) - 3) * 22.0
        global_day_id = f"global_day:{day}"
        global_day_seq.append(global_day_id)
        upsert_node(
            global_day_id,
            "global_day",
            day,
            subtitle="Global Day",
            ts=f"{day}T00:00:00",
            metrics={"role": "global_day", "day": day, "layout": {"x": gx, "y": gy, "z": gz}},
        )
        day_node = make_node_id("day", day)
        if day_node in nodes:
            add_edge(global_day_id, day_node, "global_day_bridge", 0.92, f"{day}T00:00:00")

        for domain, days in domain_days.items():
            if day not in days:
                continue
            dd_id = f"domain_day:{domain}:{day}"
            hub_id = f"domain:{domain}"
            dir_v = domain_dirs.get(domain, (0.0, 0.0, 1.0))
            b1, b2 = domain_basis.get(domain, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))
            tracks = domain_track_nodes.get(domain, [])
            anchor_id = ""
            tx, ty, tz = domain_hub_pos.get(domain, (0.0, 0.0, 0.0))
            if tracks:
                track_idx = int(_stable_unit(f"{domain}:{day}:track") * len(tracks))
                track_idx = max(0, min(len(tracks) - 1, track_idx))
                anchor_id, (tx, ty, tz) = tracks[track_idx]
            arm_depth = min(560.0, 90.0 + rank * 2.15)
            swirl = max(22.0, 84.0 - rank * 0.16)
            angle = rank * 0.39 + _stable_unit(f"{domain}:{day}:angle") * (math.pi * 2)
            jitter = (_stable_unit(f"{domain}:{day}:jitter") - 0.5) * 18.0
            base_x = tx + dir_v[0] * arm_depth
            base_y = ty + dir_v[1] * arm_depth
            base_z = tz + dir_v[2] * arm_depth
            dd_x = base_x + b1[0] * math.cos(angle) * swirl + b2[0] * math.sin(angle) * (swirl * 0.86) + jitter
            dd_y = base_y + b1[1] * math.cos(angle) * swirl + b2[1] * math.sin(angle) * (swirl * 0.86) - jitter * 0.45
            dd_z = base_z + b1[2] * math.cos(angle) * swirl + b2[2] * math.sin(angle) * (swirl * 0.86) + jitter * 0.62
            upsert_node(
                dd_id,
                "domain_day",
                day,
                subtitle=f"{domain.capitalize()} Day",
                ts=f"{day}T00:00:00",
                metrics={"role": "domain_day", "domain": domain, "day": day, "layout": {"x": dd_x, "y": dd_y, "z": dd_z}},
            )
            domain_day_seq[domain].append(dd_id)
            add_edge(hub_id, dd_id, "domain_timeline", 1.05, f"{day}T00:00:00")
            if anchor_id:
                add_edge(anchor_id, dd_id, "track_timeline", 1.16, f"{day}T00:00:00")
            add_edge(global_day_id, dd_id, "global_domain_day", 1.18, f"{day}T00:00:00")
            if day_node in nodes:
                add_edge(dd_id, day_node, "same_day", 0.75, f"{day}T00:00:00")

            if domain == "training":
                for wid in day_sessions.get(day, [])[:4]:
                    add_edge(dd_id, make_node_id("gym_session", wid), "day_session", 1.22, f"{day}T00:00:00")
            elif domain == "recovery":
                hrv_id = make_node_id("hrv", day)
                hr_id = make_node_id("hr", day)
                if hrv_id in nodes:
                    add_edge(dd_id, hrv_id, "day_recovery", 1.1, f"{day}T00:00:00")
                if hr_id in nodes:
                    add_edge(dd_id, hr_id, "day_recovery", 1.0, f"{day}T00:00:00")
            elif domain == "nutrition":
                meal_id = make_node_id("meal", day)
                if meal_id in nodes:
                    add_edge(dd_id, meal_id, "day_nutrition", 1.1, f"{day}T00:00:00")
            elif domain == "run":
                for nid in runs_by_day.get(day, [])[:4]:
                    add_edge(dd_id, nid, "day_run", 1.14, f"{day}T00:00:00")
            elif domain == "plan":
                for nid in plan_sessions_by_day.get(day, [])[:4]:
                    add_edge(dd_id, nid, "day_plan", 1.05, f"{day}T00:00:00")
            elif domain == "autopilot":
                ap_id = make_node_id("autopilot", day)
                if ap_id in nodes:
                    add_edge(dd_id, ap_id, "day_autopilot", 1.2, f"{day}T00:00:00")
                for iid in insight_by_day.get(day, [])[:4]:
                    add_edge(dd_id, make_node_id("insight", iid), "day_insight", 0.9, f"{day}T00:00:00")

    # Timeline rails: chain day nodes per domain and globally (new -> old).
    for ids in domain_day_seq.values():
        for i in range(len(ids) - 1):
            add_edge(ids[i], ids[i + 1], "timeline_next", 0.92, "")
    for i in range(len(global_day_seq) - 1):
        add_edge(global_day_seq[i], global_day_seq[i + 1], "timeline_next", 0.82, "")

    # Pattern hub keeps temporal pattern nodes connected without full-mesh noise.
    pattern_hub = make_node_id("pattern", "bridge_hub")
    upsert_node(pattern_hub, "pattern", "Muster-Bruecke", subtitle="recovery/nutrition/run")
    for nid, n in list(nodes.items()):
        if n.get("type") == "pattern" and nid != pattern_hub:
            add_edge(pattern_hub, nid, "pattern_bridge", 0.62, n.get("ts", ""))

    # Dedupe edges (keep strongest/newest) and derive node weights.
    edge_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in edges:
        key = (e["source"], e["target"], e["type"])
        prev = edge_map.get(key)
        if prev is None:
            edge_map[key] = e
            continue
        prev_w = _safe_float(prev.get("weight"), 0.0)
        cur_w = _safe_float(e.get("weight"), 0.0)
        if cur_w > prev_w or (cur_w == prev_w and str(e.get("ts", "")) > str(prev.get("ts", ""))):
            edge_map[key] = e
    edges = list(edge_map.values())

    degree: defaultdict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1

    now = int(time.time())
    for node in nodes.values():
        ts_epoch = _ts_to_epoch(str(node.get("ts") or ""))
        age_days = max(0.0, (now - ts_epoch) / 86400.0) if ts_epoch else 365.0
        recency = max(0.0, 1.2 - min(age_days / 120.0, 1.2))
        node["weight"] = round(float(node.get("weight") or 1.0) + degree[node["id"]] * 0.08 + recency, 3)

    # Persist atomically.
    conn = get_core_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM core_edge_index")
    cur.execute("DELETE FROM core_node_index")
    cur.executemany(
        "INSERT INTO core_node_index(id,type,title,subtitle,ts,weight,json) VALUES(?,?,?,?,?,?,?)",
        [
            (
                n["id"],
                n["type"],
                n.get("title", ""),
                n.get("subtitle", ""),
                n.get("ts", ""),
                float(n.get("weight") or 0.0),
                json.dumps(
                    {
                        "metrics": n.get("metrics") or {},
                        "tags": n.get("tags") or [],
                    },
                    ensure_ascii=True,
                ),
            )
            for n in nodes.values()
        ],
    )
    cur.executemany(
        "INSERT INTO core_edge_index(source,target,type,weight,ts) VALUES(?,?,?,?,?)",
        [
            (
                e["source"],
                e["target"],
                e["type"],
                float(e.get("weight") or 0.0),
                e.get("ts", ""),
            )
            for e in edges
        ],
    )
    built_ts = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    _meta_set(conn, "index_built_ts", built_ts)
    _meta_set(conn, "node_count", str(len(nodes)))
    _meta_set(conn, "edge_count", str(len(edges)))
    conn.commit()
    conn.close()
    _GRAPH_CACHE.clear()

    return {
        "ok": True,
        "rebuilt": True,
        "nodes": len(nodes),
        "edges": len(edges),
        "version": built_ts,
    }


def _load_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,type,title,subtitle,ts,weight,json FROM core_node_index WHERE id=?",
        (node_id,),
    ).fetchone()
    if not row:
        return None
    data = {}
    try:
        data = json.loads(row["json"] or "{}")
    except Exception:
        data = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"] or row["id"],
        "subtitle": row["subtitle"] or "",
        "ts": row["ts"] or "",
        "weight": _safe_float(row["weight"], 1.0),
        "metrics": data.get("metrics") or {},
        "tags": data.get("tags") or [],
    }


def _fetch_nodes_by_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id,type,title,subtitle,ts,weight,json FROM core_node_index WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {}
        try:
            data = json.loads(row["json"] or "{}")
        except Exception:
            data = {}
        out.append(
            {
                "id": row["id"],
                "type": row["type"],
                "title": row["title"] or row["id"],
                "subtitle": row["subtitle"] or "",
                "ts": row["ts"] or "",
                "weight": _safe_float(row["weight"], 1.0),
                "metrics": data.get("metrics") or {},
                "tags": data.get("tags") or [],
            }
        )
    out.sort(key=lambda n: (n["id"] != ids[0], -_safe_float(n.get("weight"), 0), n.get("id", "")))
    return out


def _bfs_collect(
    conn: sqlite3.Connection,
    focus: str,
    depth: int,
    max_nodes: int,
) -> set[str]:
    visited: set[str] = set([focus])
    dq: deque[tuple[str, int]] = deque([(focus, 0)])

    while dq and len(visited) < max_nodes:
        node_id, d = dq.popleft()
        if d >= depth:
            continue
        rows = conn.execute(
            """
            SELECT source, target
            FROM core_edge_index
            WHERE source=? OR target=?
            ORDER BY ts DESC
            LIMIT 300
            """,
            (node_id, node_id),
        ).fetchall()
        for row in rows:
            other = row["target"] if row["source"] == node_id else row["source"]
            if other in visited:
                continue
            visited.add(other)
            dq.append((other, d + 1))
            if len(visited) >= max_nodes:
                break
    return visited


def _edges_for_nodes(conn: sqlite3.Connection, node_ids: set[str], max_edges: int) -> list[dict[str, Any]]:
    if not node_ids:
        return []
    ids = list(node_ids)
    placeholders = ",".join("?" for _ in ids)
    query = f"""
        SELECT source,target,type,weight,ts
        FROM core_edge_index
        WHERE source IN ({placeholders}) AND target IN ({placeholders})
        ORDER BY ts DESC, weight DESC
        LIMIT ?
    """
    rows = conn.execute(query, ids + ids + [max_edges]).fetchall()
    return [
        {
            "source": row["source"],
            "target": row["target"],
            "type": row["type"],
            "weight": _safe_float(row["weight"], 1.0),
            "ts": row["ts"] or "",
        }
        for row in rows
    ]


def _link_rows(conn: sqlite3.Connection, focus_id: str, incoming: bool) -> list[dict[str, Any]]:
    if incoming:
        rows = conn.execute(
            """
            SELECT e.source AS other_id, e.type, e.weight, e.ts, n.type AS other_type, n.title AS other_title
            FROM core_edge_index e
            LEFT JOIN core_node_index n ON n.id = e.source
            WHERE e.target=?
            ORDER BY e.ts DESC, e.weight DESC
            LIMIT 120
            """,
            (focus_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.target AS other_id, e.type, e.weight, e.ts, n.type AS other_type, n.title AS other_title
            FROM core_edge_index e
            LEFT JOIN core_node_index n ON n.id = e.target
            WHERE e.source=?
            ORDER BY e.ts DESC, e.weight DESC
            LIMIT 120
            """,
            (focus_id,),
        ).fetchall()

    return [
        {
            "node_id": row["other_id"],
            "type": row["other_type"] or "unknown",
            "title": row["other_title"] or row["other_id"],
            "edge_type": row["type"],
            "weight": _safe_float(row["weight"], 1.0),
            "ts": row["ts"] or "",
        }
        for row in rows
    ]


def _day_from_focus(focus_node: dict[str, Any]) -> str:
    t = str(focus_node.get("type") or "")
    node_id = str(focus_node.get("id") or "")
    metrics = focus_node.get("metrics") or {}
    if t == "day" and node_id.startswith("day:"):
        return node_id.split(":", 1)[1]
    m_day = _normalize_day(metrics.get("day"))
    if m_day:
        return m_day
    if node_id.startswith(("hrv:", "hr:", "meal:", "autopilot:")):
        d = _normalize_day(node_id.split(":", 1)[1])
        if d:
            return d
    ts = _normalize_day(focus_node.get("ts"))
    if ts:
        return ts
    return date.today().isoformat()


def _build_snapshot_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Snapshot")
    try:
        tconn = get_training_db()
        r = tconn.execute("SELECT COUNT(*) AS n FROM workouts WHERE COALESCE(NULLIF(date_iso,''), date)=?", (day,)).fetchone()
        sessions = _safe_int(r["n"] if r else 0)
        tconn.close()
    except Exception:
        sessions = 0
    try:
        hconn = get_hrv_db()
        r = hconn.execute("SELECT AVG(rmssd) AS rmssd, AVG(hr) AS rhr FROM hrv_measurements WHERE substr(date_utc, 1, 10)=?", (day,)).fetchone()
        rmssd = _safe_float(r["rmssd"] if r else 0.0)
        rhr = _safe_float(r["rhr"] if r else 0.0)
        hconn.close()
    except Exception:
        rmssd = 0.0
        rhr = 0.0
    try:
        nconn = get_nutrition_db()
        r = nconn.execute(
            """
            SELECT COALESCE(kcal, 0) AS kcal
            FROM nutrition_daily
            WHERE COALESCE(NULLIF(date_iso,''), date)=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        kcal = _safe_int(r["kcal"] if r else 0)
        nconn.close()
    except Exception:
        kcal = 0

    meaning = "Stabiler Tag."
    if sessions >= 1 and rmssd > 0 and rmssd < 40:
        meaning = "Belastung hoch bei niedriger Recovery."
    elif sessions == 0 and kcal < 2200 and kcal > 0:
        meaning = "Ruhetag mit eher niedriger Energiezufuhr."
    elif sessions >= 1 and kcal > 2800:
        meaning = "Training + Energiezufuhr passen zusammen."

    panel.update(
        {
            "summary": meaning,
            "metrics": [
                {"label": "Sessions", "value": sessions},
                {"label": "RMSSD", "value": round(rmssd, 1) if rmssd else "--"},
                {"label": "kcal", "value": kcal if kcal else "--"},
            ],
            "items": [
                {"key": f"snap:{day}:sessions", "title": f"{sessions} Gym-Session(s)", "meta": day},
                {"key": f"snap:{day}:hrv", "title": f"RMSSD {round(rmssd, 1) if rmssd else '--'} / RHR {round(rhr, 1) if rhr else '--'}", "meta": "Recovery"},
                {"key": f"snap:{day}:kcal", "title": f"Kalorien {kcal if kcal else '--'}", "meta": "Ernaehrung"},
            ],
            "empty": False,
        }
    )
    return panel


def _build_training_panel(day: str, focus: dict[str, Any]) -> dict[str, Any]:
    panel = _empty_panel("Training")
    items: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    try:
        tconn = get_training_db()
        last = tconn.execute(
            """
            SELECT id, name, COALESCE(NULLIF(date_iso,''), date) AS d
            FROM workouts
            WHERE COALESCE(NULLIF(date_iso,''), date) <= ?
            ORDER BY d DESC, id DESC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        nxt = tconn.execute(
            """
            SELECT id, name, COALESCE(NULLIF(date_iso,''), date) AS d
            FROM workouts
            WHERE COALESCE(NULLIF(date_iso,''), date) > ?
            ORDER BY d ASC, id ASC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        if last:
            items.append(
                {
                    "key": f"training:last:{last['id']}",
                    "title": f"Letzte Session: {last['name']}",
                    "meta": str(last["d"]),
                    "node_id": make_node_id("gym_session", last["id"]),
                }
            )
        if nxt:
            items.append(
                {
                    "key": f"training:next:{nxt['id']}",
                    "title": f"Naechste Session: {nxt['name']}",
                    "meta": str(nxt["d"]),
                    "node_id": make_node_id("gym_session", nxt["id"]),
                }
            )

        if focus.get("type") == "exercise":
            ex_id = _safe_int(str(focus.get("id", "")).split(":", 1)[1], 0)
            rows = tconn.execute(
                """
                SELECT COALESCE(NULLIF(w.date_iso,''), w.date) AS d, s.weight, s.reps, s.rpe
                FROM sets s
                JOIN workouts w ON w.id = s.workout_id
                WHERE s.exercise_id=?
                ORDER BY d DESC, s.id DESC
                LIMIT 12
                """,
                (ex_id,),
            ).fetchall()
            for idx, row in enumerate(rows[:6]):
                items.append(
                    {
                        "key": f"exercise:{ex_id}:{idx}",
                        "title": f"{row['d']}: {row['weight']} kg x {row['reps']}",
                        "meta": f"RPE {row['rpe'] if row['rpe'] is not None else '--'}",
                    }
                )
        ex_trend = tconn.execute(
            """
            SELECT e.id AS ex_id, e.name AS name, MAX(s.weight * COALESCE(s.reps,1)) AS score
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = s.workout_id
            WHERE COALESCE(NULLIF(w.date_iso,''), w.date) >= date(?, '-21 day')
            GROUP BY e.id, e.name
            ORDER BY score DESC
            LIMIT 3
            """,
            (day,),
        ).fetchall()
        for row in ex_trend:
            items.append(
                {
                    "key": f"extrend:{row['ex_id']}",
                    "title": f"{row['name']} Score {round(_safe_float(row['score']), 1)}",
                    "meta": "21d",
                    "node_id": make_node_id("exercise", row["ex_id"]),
                }
            )
        metrics.append({"label": "Uebungen aktiv", "value": len(ex_trend)})
        tconn.close()
    except Exception:
        pass

    panel.update(
        {
            "summary": "Letzte Session, naechster Block und Uebungsstatus.",
            "items": items[:8],
            "metrics": metrics,
            "empty": len(items) == 0,
        }
    )
    return panel


def _build_recovery_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Recovery")
    try:
        conn = get_hrv_db()
        d7 = conn.execute(
            "SELECT AVG(rmssd) AS rmssd, AVG(hr) AS rhr FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-6 day') AND date(?)",
            (day, day),
        ).fetchone()
        d14 = conn.execute(
            "SELECT AVG(rmssd) AS rmssd, AVG(hr) AS rhr FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-13 day') AND date(?)",
            (day, day),
        ).fetchone()
        base = conn.execute(
            "SELECT AVG(rmssd) AS rmssd, AVG(hr) AS rhr FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-56 day') AND date(?, '-14 day')",
            (day, day),
        ).fetchone()
        conn.close()

        rmssd7 = _safe_float(d7["rmssd"] if d7 else 0)
        rhr7 = _safe_float(d7["rhr"] if d7 else 0)
        rmssd14 = _safe_float(d14["rmssd"] if d14 else 0)
        rmssd_base = _safe_float(base["rmssd"] if base else 0)
        rhr_base = _safe_float(base["rhr"] if base else 0)
        dev = rmssd7 - rmssd_base if rmssd_base else 0.0
        dev_hr = rhr7 - rhr_base if rhr_base else 0.0

        panel.update(
            {
                "summary": "RMSSD/RHR gegen Baseline.",
                "metrics": [
                    {"label": "RMSSD 7d", "value": round(rmssd7, 1) if rmssd7 else "--"},
                    {"label": "RMSSD 14d", "value": round(rmssd14, 1) if rmssd14 else "--"},
                    {"label": "Delta", "value": round(dev, 1) if rmssd_base else "--"},
                ],
                "items": [
                    {"key": f"rec:{day}:rmssd", "title": f"RMSSD Delta {round(dev, 1) if rmssd_base else '--'}", "meta": "vs Baseline"},
                    {"key": f"rec:{day}:rhr", "title": f"RHR Delta {round(dev_hr, 1) if rhr_base else '--'}", "meta": "vs Baseline"},
                ],
                "empty": False,
            }
        )
    except Exception:
        pass
    return panel


def _build_run_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Run")
    try:
        conn = get_runs_db()
        last = conn.execute("SELECT id, date, distance FROM runs WHERE date(date) <= date(?) ORDER BY date DESC LIMIT 1", (day,)).fetchone()
        n14 = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE date(date) BETWEEN date(?, '-13 day') AND date(?)", (day, day)).fetchone()
        gap = None
        if last:
            last_day = _normalize_day(last["date"])
            if last_day:
                gap = (datetime.strptime(day, "%Y-%m-%d").date() - datetime.strptime(last_day, "%Y-%m-%d").date()).days
        conn.close()

        panel.update(
            {
                "summary": "Frequenz, letzter Run, Pausenlaenge.",
                "metrics": [
                    {"label": "Runs 14d", "value": _safe_int(n14["n"] if n14 else 0)},
                    {"label": "Gap", "value": (f"{gap} Tage" if gap is not None else "--")},
                    {"label": "Last", "value": (f"{round(_safe_float(last['distance'])/1000.0, 2)} km" if last else "--")},
                ],
                "items": [
                    {
                        "key": f"run:last:{last['id']}" if last else f"run:none:{day}",
                        "title": (
                            f"Letzter Run {round(_safe_float(last['distance'])/1000.0, 2)} km" if last else "Kein Run gefunden"
                        ),
                        "meta": (_normalize_day(last["date"]) if last else day),
                        "node_id": (make_node_id("run", last["id"]) if last else None),
                    }
                ],
                "empty": False,
            }
        )
    except Exception:
        pass
    return panel


def _build_nutrition_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Ernaehrung")
    try:
        conn = get_nutrition_db()
        day_row = conn.execute(
            """
            SELECT kcal, protein, carbs, fat
            FROM nutrition_daily
            WHERE COALESCE(NULLIF(date_iso,''), date)=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        if not day_row:
            day_row = conn.execute(
                """
                SELECT kcal, p AS protein, c AS carbs, f AS fat
                FROM nutrition_day_actuals
                WHERE day=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (day,),
            ).fetchone()

        target = conn.execute(
            """
            SELECT ROUND(AVG(kcal),0) AS kcal_t, ROUND(AVG(protein),1) AS p_t
            FROM nutrition_daily
            WHERE COALESCE(NULLIF(date_iso,''), date) BETWEEN date(?, '-28 day') AND date(?, '-1 day')
            """,
            (day, day),
        ).fetchone()
        conn.close()

        kcal = _safe_float(day_row["kcal"] if day_row else 0.0)
        p = _safe_float(day_row["protein"] if day_row else 0.0)
        kcal_t = _safe_float(target["kcal_t"] if target else 0.0)
        p_t = _safe_float(target["p_t"] if target else 0.0)
        panel.update(
            {
                "summary": "Tagesplan und Ist-Werte gegen Ziel.",
                "metrics": [
                    {"label": "kcal", "value": (round(kcal, 0) if kcal else "--")},
                    {"label": "Protein", "value": (round(p, 1) if p else "--")},
                    {"label": "Delta kcal", "value": (round(kcal - kcal_t, 0) if kcal_t else "--")},
                ],
                "items": [
                    {"key": f"nutri:{day}:kcal", "title": f"kcal {round(kcal,0) if kcal else '--'}", "meta": f"Ziel {round(kcal_t,0) if kcal_t else '--'}"},
                    {"key": f"nutri:{day}:p", "title": f"Protein {round(p,1) if p else '--'} g", "meta": f"Ziel {round(p_t,1) if p_t else '--'}"},
                ],
                "empty": False,
            }
        )
    except Exception:
        pass
    return panel


def _build_plan_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Plan")
    try:
        conn = get_plans_db()
        rows = conn.execute(
            """
            SELECT id, name, start_date, total_weeks, is_active
            FROM plans
            ORDER BY is_active DESC, id DESC
            LIMIT 4
            """
        ).fetchall()
        conn.close()
        items = [
            {
                "key": f"plan:{r['id']}",
                "title": f"{r['name']}",
                "meta": f"{('aktiv' if _safe_int(r['is_active']) else 'inaktiv')} | {r['total_weeks']} Wochen",
                "node_id": make_node_id("planblock", r["id"]),
            }
            for r in rows
        ]
        panel.update(
            {
                "summary": "Aktive Planbloecke und Dauer.",
                "metrics": [{"label": "Plaene", "value": len(rows)}],
                "items": items,
                "empty": len(rows) == 0,
            }
        )
    except Exception:
        pass
    return panel


def _build_autopilot_panel(day: str, focus: dict[str, Any]) -> dict[str, Any]:
    panel = _empty_panel("Autopilot")
    try:
        conn = get_training_db()
        row = conn.execute(
            """
            SELECT date, mode, output_json, reason_codes_json
            FROM autopilot_day
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        conn.close()
        if row:
            out = str(row["output_json"] or "")
            reason = str(row["reason_codes_json"] or "[]")
            panel.update(
                {
                    "summary": "Letzte Entscheidung + Trigger + Caps.",
                    "metrics": [
                        {"label": "Modus", "value": str(row["mode"] or "--")},
                        {"label": "Tag", "value": str(row["date"] or "--")},
                    ],
                    "items": [
                        {
                            "key": f"ap:{row['date']}",
                            "title": f"Decision {row['mode']}",
                            "meta": str(row["date"]),
                            "node_id": make_node_id("autopilot", row["date"]),
                        },
                        {"key": f"ap:{row['date']}:trigger", "title": "Trigger", "meta": reason[:120]},
                    ],
                    "details": [out[:450]],
                    "empty": False,
                }
            )
    except Exception:
        pass
    return panel


def _build_insights_panel(day: str) -> dict[str, Any]:
    panel = _empty_panel("Insights")
    panel.update(
        {
            "summary": "",
            "metrics": [],
            "items": [],
            "empty": True,
        }
    )
    return panel


def _build_panels(focus_node: dict[str, Any]) -> dict[str, Any]:
    day = _day_from_focus(focus_node)
    panels = _default_panels()
    panels["snapshot"] = _build_snapshot_panel(day)
    panels["training"] = _build_training_panel(day, focus_node)
    panels["recovery"] = _build_recovery_panel(day)
    panels["run"] = _build_run_panel(day)
    panels["ernaehrung"] = _build_nutrition_panel(day)
    panels["plan"] = _build_plan_panel(day)
    panels["autopilot"] = _build_autopilot_panel(day, focus_node)
    panels["insights"] = _build_insights_panel(day)
    return panels


def get_core_graph_bundle(
    focus: str | None,
    depth: int = DEFAULT_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    global_graph: bool = False,
) -> dict[str, Any]:
    ensure_core_index_ready(auto_rebuild_if_empty=True)
    depth = max(1, min(2 if not global_graph else 3, _safe_int(depth, DEFAULT_DEPTH)))
    max_nodes = max(1, min(500, _safe_int(max_nodes, DEFAULT_MAX_NODES)))
    max_edges = max(1, min(1200, _safe_int(max_edges, DEFAULT_MAX_EDGES)))

    conn = get_core_db()
    version = _index_version(conn)
    raw_focus = (focus or "").strip() or make_node_id("day", date.today().isoformat())
    cache_key = json.dumps(
        {
            "f": raw_focus,
            "d": depth,
            "n": max_nodes,
            "e": max_edges,
            "g": int(bool(global_graph)),
            "v": version,
        },
        sort_keys=True,
    )
    cached = _GRAPH_CACHE.get(cache_key)
    if cached is not None:
        conn.close()
        return cached

    focus_node = _load_node(conn, raw_focus)
    if focus_node is None:
        fallback = make_node_id("day", date.today().isoformat())
        focus_node = _load_node(conn, fallback)
        if focus_node is None:
            row = conn.execute("SELECT id FROM core_node_index ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                focus_node = _load_node(conn, row["id"])
    if focus_node is None:
        conn.close()
        return {
            "ok": True,
            "focus": {"id": raw_focus, "type": "unknown", "title": raw_focus},
            "nodes": [],
            "edges": [],
            "incoming": [],
            "outgoing": [],
            "panels": _default_panels(),
            "meta": {"depth": depth, "max_nodes": max_nodes, "max_edges": max_edges, "global_graph": global_graph},
        }

    focus_id = str(focus_node["id"])
    if global_graph:
        fixed_rows = conn.execute(
            """
            SELECT id
            FROM core_node_index
            WHERE type IN ('core_root', 'domain_hub', 'domain_track')
            ORDER BY type, id
            """
        ).fetchall()
        day_rows = conn.execute(
            """
            SELECT id
            FROM core_node_index
            WHERE type IN ('global_day', 'domain_day')
            ORDER BY ts DESC
            LIMIT 360
            """
        ).fetchall()
        data_rows = conn.execute(
            """
            SELECT id
            FROM core_node_index
            WHERE type NOT IN ('core_root', 'domain_hub', 'domain_track', 'global_day', 'domain_day')
            ORDER BY weight DESC, ts DESC
            LIMIT 1200
            """
        ).fetchall()
        ordered_ids: list[str] = [str(r["id"]) for r in fixed_rows]
        ordered_ids.extend(str(r["id"]) for r in day_rows)
        ordered_ids.extend(str(r["id"]) for r in data_rows)
        ordered_ids.append(focus_id)
        node_ids = set()
        for nid in ordered_ids:
            node_ids.add(nid)
            if len(node_ids) >= max_nodes:
                break
    else:
        node_ids = _bfs_collect(conn, focus_id, depth, max_nodes)

    edges = _edges_for_nodes(conn, node_ids, max_edges)
    nodes = _fetch_nodes_by_ids(conn, list(node_ids))

    degree: defaultdict[str, int] = defaultdict(int)
    now = int(time.time())
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    for n in nodes:
        age_days = 365.0
        ts_epoch = _ts_to_epoch(str(n.get("ts") or ""))
        if ts_epoch:
            age_days = max(0.0, (now - ts_epoch) / 86400.0)
        recency = max(0.0, 1.0 - min(age_days / 120.0, 1.0))
        n["weight"] = round(_safe_float(n.get("weight"), 1.0) + degree[n["id"]] * 0.12 + recency, 3)

    incoming = _link_rows(conn, focus_id, incoming=True)
    outgoing = _link_rows(conn, focus_id, incoming=False)
    panels = _build_panels(focus_node)
    conn.close()

    payload = {
        "ok": True,
        "focus": focus_node,
        "nodes": nodes,
        "edges": edges,
        "incoming": incoming,
        "outgoing": outgoing,
        "panels": panels,
        "meta": {
            "depth": depth,
            "max_nodes": max_nodes,
            "max_edges": max_edges,
            "global_graph": bool(global_graph),
            "version": version,
        },
    }
    _GRAPH_CACHE.set(cache_key, payload, GRAPH_CACHE_TTL_S)
    return payload


def search_core_nodes(q: str, limit: int = 40) -> dict[str, Any]:
    ensure_core_index_ready(auto_rebuild_if_empty=True)
    query = (q or "").strip().lower()
    if not query:
        return {"ok": True, "q": "", "groups": {}, "items": []}

    conn = get_core_db()
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id,type,title,subtitle,ts,weight
        FROM core_node_index
        WHERE lower(title) LIKE ? OR lower(subtitle) LIKE ? OR lower(id) LIKE ?
        ORDER BY weight DESC, ts DESC
        LIMIT ?
        """,
        (like, like, like, max(10, min(120, int(limit)))),
    ).fetchall()
    conn.close()

    items = [
        {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"] or row["id"],
            "subtitle": row["subtitle"] or "",
            "ts": row["ts"] or "",
            "weight": _safe_float(row["weight"], 1.0),
        }
        for row in rows
    ]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        arr = groups[item["type"]]
        if len(arr) < 8:
            arr.append(item)

    return {"ok": True, "q": q, "groups": dict(groups), "items": items}


def get_recent_activity(limit: int = 24) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    try:
        tconn = get_training_db()
        rows = tconn.execute(
            """
            SELECT id, name, COALESCE(NULLIF(date_iso,''), date) AS d, created_at
            FROM workouts
            ORDER BY created_at DESC
            LIMIT 8
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["d"]) or date.today().isoformat()
            ts = _parse_ts(row["created_at"]) or f"{day}T00:00:00"
            items.append(
                {
                    "type": "gym_session",
                    "title": f"Neue Session: {row['name']}",
                    "subtitle": day,
                    "node_id": make_node_id("gym_session", row["id"]),
                    "ts": ts,
                }
            )

        rows = tconn.execute(
            """
            SELECT date, mode, created_ts
            FROM autopilot_day
            ORDER BY date DESC
            LIMIT 5
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["date"])
            if not day:
                continue
            items.append(
                {
                    "type": "autopilot_decision",
                    "title": f"Autopilot: {row['mode']}",
                    "subtitle": day,
                    "node_id": make_node_id("autopilot", day),
                    "ts": _parse_ts(row["created_ts"]) or f"{day}T00:00:00",
                }
            )

        tconn.close()
    except Exception:
        pass

    try:
        hconn = get_hrv_db()
        rows = hconn.execute(
            """
            SELECT substr(date_utc, 1, 10) AS d, AVG(rmssd) AS rmssd, MAX(ts_measurement) AS ts
            FROM hrv_measurements
            GROUP BY substr(date_utc, 1, 10)
            ORDER BY d DESC
            LIMIT 4
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["d"])
            if not day:
                continue
            items.append(
                {
                    "type": "hrv_daily",
                    "title": f"HRV: RMSSD {round(_safe_float(row['rmssd']), 1)}",
                    "subtitle": day,
                    "node_id": make_node_id("hrv", day),
                    "ts": _parse_ts(row["ts"]) or f"{day}T00:00:00",
                }
            )
        hconn.close()
    except Exception:
        pass

    try:
        nconn = get_nutrition_db()
        rows = nconn.execute(
            """
            SELECT COALESCE(NULLIF(date_iso,''), date) AS d, kcal, protein, created_at
            FROM nutrition_daily
            ORDER BY COALESCE(date_iso, date) DESC
            LIMIT 4
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["d"])
            if not day:
                continue
            items.append(
                {
                    "type": "meal_day",
                    "title": f"Meal: {row['kcal']} kcal / P {row['protein']}",
                    "subtitle": day,
                    "node_id": make_node_id("meal", day),
                    "ts": _parse_ts(row["created_at"]) or f"{day}T00:00:00",
                }
            )
        nconn.close()
    except Exception:
        pass

    try:
        rconn = get_runs_db()
        rows = rconn.execute(
            """
            SELECT id, date, distance
            FROM runs
            ORDER BY date DESC
            LIMIT 4
            """
        ).fetchall()
        for row in rows:
            day = _normalize_day(row["date"])
            if not day:
                continue
            km = round(_safe_float(row["distance"]) / 1000.0, 2)
            items.append(
                {
                    "type": "run",
                    "title": f"Run: {km} km",
                    "subtitle": day,
                    "node_id": make_node_id("run", row["id"]),
                    "ts": _parse_ts(row["date"]) or f"{day}T00:00:00",
                }
            )
        rconn.close()
    except Exception:
        pass

    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return {"ok": True, "items": items[: max(1, min(int(limit), 80))]}


def core_stream_events(sleep_s: float = 5.0):
    ensure_core_index_ready(auto_rebuild_if_empty=True)
    last_ts = ""
    while True:
        try:
            conn = get_core_db()
            row = conn.execute("SELECT id, ts FROM core_node_index ORDER BY ts DESC LIMIT 1").fetchone()
            conn.close()
            node_id = row["id"] if row else ""
            ts = row["ts"] if row else ""
            if ts and ts != last_ts:
                last_ts = ts
                payload = json.dumps({"type": "new_data", "node_id": node_id, "ts": ts}, ensure_ascii=True)
                yield f"event: new_data\ndata: {payload}\n\n"
            else:
                yield "event: ping\ndata: {}\n\n"
        except Exception:
            yield "event: ping\ndata: {}\n\n"
        time.sleep(max(1.0, float(sleep_s)))


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_MAX_NODES",
    "DEFAULT_MAX_EDGES",
    "make_node_id",
    "ensure_core_graph_schema",
    "ensure_core_index_ready",
    "rebuild_core_index",
    "get_core_graph_bundle",
    "search_core_nodes",
    "get_recent_activity",
    "core_stream_events",
]
