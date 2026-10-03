from __future__ import annotations

import sqlite3
from collections import defaultdict
from flask import Blueprint, jsonify

from security.write_guard import require_ai_read

ai_catalog_api = Blueprint("ai_catalog_api", __name__, url_prefix="/api/ai")

def get_training_db() -> sqlite3.Connection:
    conn = sqlite3.connect("./database/training.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn

@ai_catalog_api.get("/exercises_catalog")
@require_ai_read
def exercises_catalog():
    conn = get_training_db()
    rows = conn.execute("""
        SELECT name, device, COUNT(*) as n
        FROM exercises
        WHERE name IS NOT NULL AND TRIM(name) != ''
        GROUP BY name, device
        ORDER BY n DESC
    """).fetchall()
    conn.close()

    by_name = defaultdict(lambda: {"name": "", "devices": [], "popularity": 0})
    for r in rows:
        name = (r["name"] or "").strip()
        dev = (r["device"] or "").strip()
        n = int(r["n"] or 0)
        if not name:
            continue
        obj = by_name[name]
        obj["name"] = name
        obj["popularity"] += n
        if dev and dev not in obj["devices"]:
            obj["devices"].append(dev)

    # sort by popularity desc
    out = sorted(by_name.values(), key=lambda x: x["popularity"], reverse=True)

    return jsonify({"ok": True, "exercises": out})
