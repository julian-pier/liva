from __future__ import annotations

import os
import time
from typing import Any, Dict

from flask import jsonify, request

from database import connections
from nutrition.nutrition_planning_db import get_live_macro_targets
from security.write_guard import require_ai_read
from ai.api_ai_common import summarize_bounds


def register(ai_api):
    @ai_api.get("/index")
    @require_ai_read
    def index():
        endpoints = [
            {"path": "/api/ai/index", "method": "GET", "purpose": "discovery list"},
            {"path": "/api/ai/meta", "method": "GET", "purpose": "service metadata"},
            {"path": "/api/ai/hrv/latest", "method": "GET", "purpose": "latest HRV"},
            {"path": "/api/ai/hrv/series", "method": "GET", "purpose": "HRV series", "params": ["from", "to", "last", "limit", "cursor", "fields"]},
            {"path": "/api/ai/hrv/summary", "method": "GET", "purpose": "HRV summary", "params": ["last", "baseline_days", "trend_days"]},
            {"path": "/api/ai/hrv/events/latest", "method": "GET", "purpose": "latest sickness/alcohol events"},
            {"path": "/api/ai/gym/exercises_catalog", "method": "GET", "purpose": "exercise catalog", "params": ["limit"]},
            {"path": "/api/ai/gym/workouts", "method": "GET", "purpose": "workouts list", "params": ["from", "to", "last", "limit", "cursor", "fields"]},
            {"path": "/api/ai/gym/workout/{workout_id}", "method": "GET", "purpose": "workout detail"},
            {"path": "/api/ai/gym/raw_sets", "method": "GET", "purpose": "raw sets", "params": ["from", "to", "last", "limit", "cursor", "fields"]},
            {"path": "/api/ai/gym/exercise_frequency", "method": "GET", "purpose": "exercise frequency", "params": ["from", "to", "last", "limit"]},
            {"path": "/api/ai/gym/summary_weekly", "method": "GET", "purpose": "gym weekly summary", "params": ["from", "to", "last"]},
            {"path": "/api/ai/gym/prs", "method": "GET", "purpose": "gym prs", "params": ["from", "to", "last", "limit"]},
            {"path": "/api/ai/gym/progression/overview", "method": "GET", "purpose": "exercise progression overview", "params": ["from", "to", "last", "limit", "exercise", "variation", "device", "laterality", "fields"]},
            {"path": "/api/ai/gym/progression/exercise", "method": "GET", "purpose": "exercise progression timeline", "params": ["exercise", "from", "to", "last", "limit", "variation", "device", "laterality", "fields"]},
            {"path": "/api/ai/runs/list", "method": "GET", "purpose": "runs list", "params": ["from", "to", "last", "limit", "cursor", "fields"]},
            {"path": "/api/ai/runs/run/{run_id}", "method": "GET", "purpose": "run detail"},
            {"path": "/api/ai/runs/summary_weekly", "method": "GET", "purpose": "runs weekly summary", "params": ["from", "to", "last"]},
            {"path": "/api/ai/plans/list", "method": "GET", "purpose": "plans list", "params": ["limit"]},
            {"path": "/api/ai/plans/active", "method": "GET", "purpose": "active plan"},
            {"path": "/api/ai/nutrition/targets/live", "method": "GET", "purpose": "live nutrition targets"},
            {"path": "/api/ai/nutrition/actuals/daily", "method": "GET", "purpose": "daily actuals", "params": ["from", "to", "last", "limit", "cursor"]},
            {"path": "/api/ai/nutrition/meals/list", "method": "GET", "purpose": "meal templates list", "params": ["limit", "cursor", "fields"]},
            {"path": "/api/ai/nutrition/mealplan/week", "method": "GET", "purpose": "active mealplan week"},
        ]

        return jsonify({
            "ok": True,
            "max_operations": 30,
            "defaults": {
                "limit": 200,
                "cursor": "opaque",
                "last_tokens": ["7d", "14d", "30d", "12w", "1y"],
            },
            "endpoints": endpoints,
        })

    @ai_api.get("/meta")
    @require_ai_read
    def meta():
        info: Dict[str, Any] = {
            "ok": True,
            "service": "liva-ai",
            "ts": int(time.time()),
            "timezone": time.tzname[0] if time.tzname else "UTC",
            "db_bounds": {},
            "counts": {},
            "active_plan_id": None,
            "build": os.getenv("LIVA_BUILD") or None,
        }

        try:
            conn = connections.get_runs_db()
            info["db_bounds"]["runs"] = summarize_bounds(conn, "runs", "date")
            conn.close()
        except Exception:
            info["db_bounds"]["runs"] = {"min_date": None, "max_date": None, "count": 0}

        try:
            conn = connections.get_training_db()
            info["db_bounds"]["training"] = summarize_bounds(conn, "workouts", "date_iso")
            conn.close()
        except Exception:
            info["db_bounds"]["training"] = {"min_date": None, "max_date": None, "count": 0}

        try:
            conn = connections.get_hrv_db()
            info["db_bounds"]["hrv"] = summarize_bounds(conn, "hrv_measurements", "date_utc")
            conn.close()
        except Exception:
            info["db_bounds"]["hrv"] = {"min_date": None, "max_date": None, "count": 0}

        try:
            conn = connections.get_nutrition_db()
            info["db_bounds"]["nutrition_actuals"] = summarize_bounds(conn, "nutrition_day_actuals", "day")
            info["db_bounds"]["nutrition_meals"] = summarize_bounds(conn, "nutrition_meal_templates", "created_at")
            conn.close()
        except Exception:
            info["db_bounds"]["nutrition_actuals"] = {"min_date": None, "max_date": None, "count": 0}

        try:
            conn = connections.get_plans_db()
            cur = conn.cursor()
            row = cur.execute(
                "SELECT id FROM plans WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
            info["active_plan_id"] = row["id"] if row else None
            conn.close()
        except Exception:
            info["active_plan_id"] = None

        info["targets_live"] = get_live_macro_targets()
        return jsonify(info)
