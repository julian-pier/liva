from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import kienzl

from analysis.kienzl_weekly import refresh_kienzl_weekly_analyse
from security.private_access import require_master

kienzl_api = Blueprint("kienzl_api", __name__, url_prefix="/api/kienzl")


@kienzl_api.get("/daily")
@require_master
def api_kienzl_daily():
    day = (request.args.get("day") or "").strip() or None
    force = (request.args.get("force") or "0").strip()
    force_bool = force not in ("0", "", "false", "False", "no", "No")
    refresh_weekly = (request.args.get("refresh_weekly") or "0").strip()
    refresh_weekly_bool = refresh_weekly not in ("0", "", "false", "False", "no", "No")

    payload = kienzl.build_daily(day or kienzl._day_iso(None), force=force_bool)

    if refresh_weekly_bool:
        try:
            row = refresh_kienzl_weekly_analyse(days=90, tz_name="Europe/Berlin")
            payload["weekly_refreshed"] = bool(row.get("openai_ok"))
            payload["weekly_error"] = row.get("openai_error")
            payload["weekly_key_source"] = row.get("openai_key_source")
            payload["weekly_key_fingerprint"] = row.get("openai_key_fingerprint")
        except Exception as e:
            payload["weekly_refreshed"] = False
            payload["weekly_error"] = str(e)[:200]

    return jsonify(payload)


@kienzl_api.post("/usage")
def api_kienzl_usage():
    body = request.get_json(silent=True) or {}
    day = (body.get("day") or "").strip()
    phrase_id = body.get("phrase_id")
    action_id = (body.get("action_id") or "").strip()
    context = (body.get("context") or "").strip() or None

    if not day or not action_id:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    try:
        phrase_id_int = int(phrase_id)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_phrase_id"}), 400

    ok = kienzl.record_usage(day=day, phrase_id=phrase_id_int, action_id=action_id, context=context)
    if not ok:
        return jsonify({"ok": False, "error": "write_failed"}), 500
    return jsonify({"ok": True})
