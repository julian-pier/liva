print("SYNC START")

import requests, json, os
from datetime import datetime
import sqlite3

BASE_DIR = os.path.dirname(__file__)

CONFIG = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")


def get_config():
    with open(CONFIG, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=4)

def refresh_access_token(cfg):
    res = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token"
    }).json()

    cfg["refresh_token"] = res["refresh_token"]
    save_config(cfg)

    return res["access_token"]

def fetch_activities(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    res = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers
    ).json()
    return res

def save_activity(a):
    if a.get("type") != "Run":
        return False

    os.makedirs(DATA_DIR, exist_ok=True)
    path = f"{DATA_DIR}/{a['id']}.json"

    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(a, f, indent=4)
        return True

    return False


def sync_strava_runs():
    cfg = get_config()
    token = refresh_access_token(cfg)
    acts = fetch_activities(token)

    imported = 0
    for a in acts:
        if save_activity(a):
            imported += 1

    return imported

DB_PATH = os.path.join(BASE_DIR, "runs.sqlite3")

def import_json_to_sqlite():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for file in os.listdir(DATA_DIR):
        if not file.endswith(".json"):
            continue

        with open(os.path.join(DATA_DIR, file)) as f:
            a = json.load(f)

        cur.execute("""
            INSERT OR IGNORE INTO runs
            (id, date, distance, moving_time, pace, avg_hr, max_hr, elevation, load)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            a["id"],
            a["start_date"],
            a["distance"],
            a["moving_time"],
            a["distance"] / a["moving_time"] * 1000 if a["moving_time"] else None,
            a.get("average_heartrate"),
            a.get("max_heartrate"),
            a.get("total_elevation_gain"),
            a.get("suffer_score"),
        ))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    sync_strava_runs()
