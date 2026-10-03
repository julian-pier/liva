#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "database" / "training.sqlite3"


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def run_query(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return list(conn.execute(sql))


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"DB: {DB_PATH}")

    print_section("Mixed Laterality Within Same Name/Device/Variation")
    rows = run_query(
        conn,
        """
        WITH base AS (
          SELECT
            LOWER(TRIM(name)) AS name,
            COALESCE(TRIM(device), '') AS device,
            COALESCE(TRIM(variation), '') AS variation,
            COALESCE(TRIM(laterality), '') AS laterality,
            COUNT(DISTINCT workout_id) AS sessions
          FROM exercises
          GROUP BY 1,2,3,4
        )
        SELECT
          name,
          device,
          variation,
          GROUP_CONCAT(laterality || ':' || sessions, ' | ') AS laterality_sessions,
          SUM(sessions) AS total_sessions
        FROM base
        GROUP BY name, device, variation
        HAVING COUNT(*) > 1
        ORDER BY total_sessions DESC, name ASC;
        """,
    )
    for row in rows:
        print(dict(row))

    print_section("Single-Row Label Typos / Unknown Short Codes")
    rows = run_query(
        conn,
        """
        SELECT
          id,
          workout_id,
          name,
          COALESCE(TRIM(device), '') AS device,
          COALESCE(TRIM(variation), '') AS variation,
          COALESCE(TRIM(laterality), '') AS laterality
        FROM exercises
        WHERE COALESCE(TRIM(device), '') IN ('K')
           OR COALESCE(TRIM(variation), '') IN ('K')
           OR (COALESCE(TRIM(device), '') = '' AND COALESCE(TRIM(variation), '') = '')
        ORDER BY LOWER(TRIM(name)), workout_id;
        """,
    )
    for row in rows:
        print(dict(row))

    print_section("Weight Profile Breaks Within Same Name/Device/Variation")
    rows = run_query(
        conn,
        """
        SELECT
          e.id AS exercise_id,
          w.date_iso AS date_iso,
          e.name AS name,
          COALESCE(TRIM(e.device), '') AS device,
          COALESCE(TRIM(e.variation), '') AS variation,
          COALESCE(TRIM(e.laterality), '') AS laterality,
          MAX(s.weight) AS max_weight
        FROM exercises e
        JOIN workouts w ON w.id = e.workout_id
        LEFT JOIN sets s ON s.exercise_id = e.id
        GROUP BY e.id, w.date_iso, e.name, device, variation, laterality
        ORDER BY LOWER(TRIM(e.name)), variation, device, w.date_iso
        """,
    )
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (row["name"], row["device"], row["variation"], row["laterality"]),
            [],
        ).append(row)
    for key, items in sorted(grouped.items()):
        weights = [float(item["max_weight"]) for item in items if item["max_weight"] is not None]
        if len(weights) < 4:
            continue
        med = statistics.median(weights)
        if med <= 0:
            continue
        for item in items:
            weight = item["max_weight"]
            if weight is None:
                continue
            weight = float(weight)
            if weight >= med * 1.6 or weight <= med * 0.6:
                print(
                    {
                        "name": key[0],
                        "device": key[1],
                        "variation": key[2],
                        "laterality": key[3],
                        "date_iso": item["date_iso"],
                        "exercise_id": item["exercise_id"],
                        "max_weight": round(weight, 1),
                        "median_weight": round(med, 1),
                    }
                )

    print_section("Mode-First Exercises Still Using Other Labels")
    rows = run_query(
        conn,
        """
        SELECT
          LOWER(TRIM(name)) AS name,
          COALESCE(TRIM(device), '') AS device,
          COALESCE(TRIM(variation), '') AS variation,
          COALESCE(TRIM(laterality), '') AS laterality,
          COUNT(DISTINCT workout_id) AS sessions,
          MIN((SELECT date_iso FROM workouts w WHERE w.id = exercises.workout_id)) AS first_date,
          MAX((SELECT date_iso FROM workouts w WHERE w.id = exercises.workout_id)) AS last_date
        FROM exercises
        WHERE LOWER(TRIM(name)) IN ('beinpresse', 'beinstrecker', 'wadenheben', 'pushdowns', 'pushdown')
        GROUP BY 1,2,3,4
        ORDER BY name, sessions DESC;
        """,
    )
    for row in rows:
        print(dict(row))

    conn.close()


if __name__ == "__main__":
    main()
