import os
import json
import sqlite3

BASE_DIR = os.path.dirname(__file__)

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(
    os.path.dirname(BASE_DIR),
    "database",
    "runs.sqlite3"
)



def compute_pace(avg_speed):
    if not avg_speed or avg_speed == 0:
        return None
    return 1000 / avg_speed  # seconds per km

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".json"):
            continue

        path = os.path.join(DATA_DIR, fname)
        data = json.load(open(path))

        run_id = data["id"]
        date = data["start_date_local"]
        distance = data.get("distance")
        moving_time = data.get("moving_time")
        avg_speed = data.get("average_speed")
        avg_hr = data.get("average_heartrate")
        max_hr = data.get("max_heartrate")
        elevation = data.get("total_elevation_gain")
        pace = compute_pace(avg_speed)

        cur.execute("""
            INSERT OR IGNORE INTO runs
            (id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation, pace))

    conn.commit()
    conn.close()
    print("✔ Import completed.")


def import_runs_to_db():
    main()


if __name__ == "__main__":
    main()
