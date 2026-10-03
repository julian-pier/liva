#!/usr/bin/env python3
from __future__ import annotations

"""Read-only before/after diagnosis for a copied training database."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.progression_rules import compare_set_progress, normalize_set_performance
from analysis.training_progress import compute_progress_payloads


PRODUCTION_DB = (REPO_ROOT / "database" / "training.sqlite3").resolve()


def _norm(value):
    return " ".join(str(value or "").strip().lower().split())


def _rows(conn):
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT w.id session_id, SUBSTR(w.date_iso,1,10) session_date,
                   w.name session_name, e.name exercise_name,
                   COALESCE(e.device,'') device, COALESCE(e.variation,'') variation,
                   COALESCE(e.laterality,'bilateral') laterality,
                   s.id set_id, COALESCE(s.set_number,0) set_number,
                   s.weight, s.reps, s.rpe
            FROM workouts w
            JOIN exercises e ON e.workout_id=w.id
            JOIN sets s ON s.exercise_id=e.id
            WHERE s.weight > 0 AND s.reps > 0
            ORDER BY SUBSTR(w.date_iso,1,10),w.id,e.id,COALESCE(s.set_number,0),s.id
            """
        )
    ]


def _identity(row):
    return (_norm(row["exercise_name"]), _norm(row["device"]), _norm(row["variation"]), _norm(row["laterality"]) or "bilateral")


def _legacy_metrics(conn):
    sessions = []
    by_id = {}
    for row in _rows(conn):
        session = by_id.get(row["session_id"])
        if session is None:
            session = {"session_id": row["session_id"], "session_date": row["session_date"], "session_name": row["session_name"], "sets": []}
            by_id[row["session_id"]] = session
            sessions.append(session)
        session["sets"].append(row)
    output = []
    for idx, session in enumerate(sessions):
        previous = sessions[max(0, idx - 8):idx]
        counts = Counter()
        total = len(session["sets"])
        grouped = defaultdict(list)
        for row in session["sets"]:
            grouped[_identity(row)].append(row)
        for identity, current in grouped.items():
            reference = None
            for prior in reversed(previous):
                found = [row for row in prior["sets"] if _identity(row) == identity]
                if found:
                    reference = found
                    break
            for pos, row in enumerate(current):
                if reference is None or pos >= len(reference):
                    counts["new"] += 1
                    continue
                shared_identity = {
                    "canonical_exercise": identity[0], "exercise_name": identity[0],
                    "device": identity[1], "variation": identity[2], "laterality": identity[3],
                }
                result = compare_set_progress(
                    normalize_set_performance({**row, **shared_identity}),
                    normalize_set_performance({**reference[pos], **shared_identity}),
                )
                counts[result.status] += 1
        comparable = counts["improved"] + counts["same"] + counts["worse"]
        output.append({
            **{k: session[k] for k in ("session_id", "session_date", "session_name")},
            "total": total, "comparable": comparable,
            "coverage": comparable / total if total else None,
            "improved": counts["improved"], "same": counts["same"], "worse": counts["worse"],
            "has_result": comparable >= 6,
        })
    return output


def _calendar_legacy_counts(conn):
    last = {}
    by_session = defaultdict(Counter)
    for row in _rows(conn):
        key = (_norm(row["exercise_name"]), _norm(row["variation"]), int(row["set_number"] or 0))
        previous = last.get(key)
        if previous and previous["session_id"] != row["session_id"]:
            identity = {"canonical_exercise": key[0], "exercise_name": key[0], "device": "", "variation": key[1], "laterality": "bilateral"}
            result = compare_set_progress(normalize_set_performance({**row, **identity}), normalize_set_performance({**previous, **identity}))
            by_session[row["session_id"]][result.status if result.status in {"improved", "same", "worse"} else "same"] += 1
        last[key] = row
    return by_session


def _mean(values):
    clean = [float(value) for value in values if value is not None]
    return statistics.mean(clean) if clean else None


def _orphan_cache_references(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ui_read_models'").fetchone() is None:
        return []
    workout_ids = {int(row[0]) for row in conn.execute("SELECT id FROM workouts")}
    orphans = []
    for key, payload in conn.execute("SELECT snapshot_key,payload_json FROM ui_read_models"):
        ids = {int(match) for match in re.findall(r'\"(?:session_id|workout_id)\"\s*:\s*(\d+)', str(payload or ""))}
        missing = sorted(ids - workout_ids)
        if missing:
            orphans.append({"snapshot_key": key, "missing_session_ids": missing})
    return orphans


def diagnose(conn):
    old = _legacy_metrics(conn)
    new = compute_progress_payloads(conn)
    old_by_id = {row["session_id"]: row for row in old}
    new_by_id = {row["session_id"]: row for row in new}
    calendar = _calendar_legacy_counts(conn)
    contradictions = []
    changes = []
    for session_id, old_row in old_by_id.items():
        calendar_row = calendar.get(session_id, Counter())
        if any(old_row[key] != calendar_row[key] for key in ("improved", "same", "worse")):
            contradictions.append({
                "session_id": session_id, "date": old_row["session_date"], "name": old_row["session_name"],
                "old_analysis": {key: old_row[key] for key in ("improved", "same", "worse")},
                "old_calendar": {key: calendar_row[key] for key in ("improved", "same", "worse")},
            })
        new_row = new_by_id.get(session_id)
        if new_row:
            delta = sum(abs(int(new_row[key]) - int(old_row[key])) for key in ("improved", "same", "worse"))
            changes.append({"session_id": session_id, "date": old_row["session_date"], "name": old_row["session_name"], "count_delta": delta, "old": {key: old_row[key] for key in ("improved", "same", "worse", "comparable")}, "new": {key: new_row[key] for key in ("improved", "same", "worse", "comparable_sets")}})
    distribution = Counter(row["status"] for row in new)
    pull_a = [
        {
            "session_id": row["session_id"], "date": row["session_date"], "name": row["session_name"],
            "old_analysis": next((item for item in old if item["session_id"] == row["session_id"]), None),
            "old_calendar": dict(calendar.get(row["session_id"], {})), "new": row,
        }
        for row in new
        if row["session_date"] == "2026-07-14" and "pull a" in _norm(row["session_name"])
    ]
    return {
        "database_mode": "read_only_copy",
        "old_average_coverage": _mean(row["coverage"] for row in old),
        "new_average_coverage": _mean(row["coverage"] for row in new),
        "old_average_comparable_sets": _mean(row["comparable"] for row in old),
        "new_average_comparable_sets": _mean(row["comparable_sets"] for row in new),
        "sessions_without_result_before": sum(not row["has_result"] for row in old),
        "sessions_without_result_after": sum(row["comparable_sets"] == 0 for row in new),
        "sessions_with_contradictory_old_consumer_values": len(contradictions),
        "contradictory_sessions": contradictions[:50],
        "distribution": dict(distribution),
        "largest_changes": sorted(changes, key=lambda row: row["count_delta"], reverse=True)[:20],
        "low_confidence_sessions": [row for row in new if row["comparison_confidence"] in {"low", "very_low", "none"}][:50],
        "orphan_cached_deleted_session_references": _orphan_cache_references(conn),
        "pull_a_2026_07_14": pull_a,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-copy", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.database_copy.expanduser().resolve()
    if path == PRODUCTION_DB:
        parser.error("Refusing production database; pass a copied database file.")
    if not path.is_file():
        parser.error(f"Database copy does not exist: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = diagnose(conn)
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
