from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from time import time

from flask import Blueprint, Response, g, jsonify, render_template, request, stream_with_context

from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_runs_db, get_training_db
from core.core_service import (
    extract_core_key_from_request,
    get_core_summary,
    get_core_intel_payload,
    get_core_node_details,
    get_core_observatory_snapshot,
    get_core_state,
    is_core_access_allowed,
    observatory_event_stream,
    record_heartbeat,
    sse_event_stream,
)
from core.core_control_room import (
    ensure_core_control_room_schema,
    get_core_control_room_payload,
    update_memory_state,
)
from core.core_daily_board import core_board_view_model
from core.core_memory_layers import (
    batch_control_consolidations,
    build_consolidation_drafts,
    control_consolidation,
    control_memory_event,
    get_memory_consolidations,
    get_memory_inbox,
)
from core.core_memory_file_patches import (
    PATCH_ERROR_MESSAGES,
    build_memory_file_patch_drafts,
    control_memory_file_patch,
    get_memory_file_patches,
)
from core.core_model import get_model_state_payload
from core.core_review import run_review_pass
from core.core_graph import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_NODES,
    core_stream_events,
    get_core_graph_bundle,
    get_recent_activity,
    rebuild_core_index,
    search_core_nodes,
)
from core.core_training_engine import (
    end_core_training_session,
    ensure_core_training_schema,
    get_core_profile,
    get_core_training_stats,
    next_core_training_card,
    start_core_training_session,
    submit_core_training_label,
    update_core_profile,
)
from core.core_v2_engine import (
    backfill_night_cycle_history,
    ensure_core_v2_ready,
    get_bootstrap_payload,
    get_clone_payload,
    get_decision_v2_payload,
    get_decision_explain_payload,
    get_explain_v2_payload,
    get_hypothesis_payload,
    get_learned_payload,
    get_night_cycle_payload,
    get_parameter_learning_payload,
    get_pattern_payload,
    get_reviews_payload,
    get_scenarios_payload,
    get_timeline_payload,
    get_today_payload,
    recompute_core_v2,
)
core_bp = Blueprint("core_bp", __name__)
_RL_LOCK = threading.Lock()
_RL_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def _to_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw or "").strip())
    except Exception:
        return int(default)


def _core_live_enabled() -> bool:
    return _to_bool(os.getenv("CORE_LIVE"), True)


def _rate_limits_enabled() -> bool:
    return _to_bool(os.getenv("CORE_RATE_LIMITS"), True)


def _cache_ttl_state(window: str) -> float:
    if (window or "").strip().lower() == "live":
        return float((os.getenv("CORE_CACHE_STATE_LIVE_TTL_S") or "3").strip())
    return float((os.getenv("CORE_CACHE_STATE_TTL_S") or "8").strip())


def _cache_ttl_summary(window: str) -> float:
    w = (window or "").strip().lower()
    if w == "live":
        return float((os.getenv("CORE_CACHE_SUMMARY_LIVE_TTL_S") or "6").strip())
    if w == "7d":
        return float((os.getenv("CORE_CACHE_SUMMARY_7D_TTL_S") or "30").strip())
    return float((os.getenv("CORE_CACHE_SUMMARY_TTL_S") or "15").strip())


def _cache_ttl_intel() -> float:
    return float((os.getenv("CORE_CACHE_INTEL_TTL_S") or "60").strip())


def _cache_ttl_snapshot(window: str) -> float:
    w = (window or "").strip().lower()
    if w == "live":
        return float((os.getenv("CORE_CACHE_SNAPSHOT_LIVE_TTL_S") or "1").strip())
    if w == "7d":
        return float((os.getenv("CORE_CACHE_SNAPSHOT_7D_TTL_S") or "20").strip())
    return float((os.getenv("CORE_CACHE_SNAPSHOT_TTL_S") or "5").strip())


def _cache_ttl_graph() -> float:
    return float((os.getenv("CORE_CACHE_GRAPH_TTL_S") or "20").strip())


def _cache_ttl_control_room(days: int) -> float:
    if int(days or 30) <= 7:
        return float((os.getenv("CORE_CACHE_CONTROL_ROOM_7D_TTL_S") or "20").strip())
    return float((os.getenv("CORE_CACHE_CONTROL_ROOM_TTL_S") or "30").strip())


def _get_control_room_payload_cached(days: int) -> tuple[dict, bool]:
    safe_days = max(7, min(90, int(days or 30)))
    cache_key = f"control-room:{safe_days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached, True
    payload = get_core_control_room_payload(days=safe_days)
    _cache_set(cache_key, payload, _cache_ttl_control_room(safe_days))
    return payload, False


def _rate_limit_for(endpoint: str) -> tuple[int, float]:
    if endpoint == "graph":
        return (12, 2.0)
    if endpoint == "search":
        return (6, 2.0)
    if endpoint == "recent":
        return (4, 2.0)
    if endpoint == "state":
        return (1, 2.0)
    if endpoint == "intel":
        return (1, 5.0)
    if endpoint == "heartbeat":
        return (2, 2.0)
    if endpoint == "summary":
        return (1, 3.0)
    if endpoint == "snapshot":
        return (2, 2.0)
    if endpoint == "stream":
        return (120, 60.0)
    return (5, 1.0)


def _rl_key(endpoint: str) -> str:
    sid = (request.cookies.get("th_core_sid") or "").strip()
    ip = getattr(g, "client_ip", "") or (request.remote_addr or "")
    return f"{endpoint}|{sid or ip or 'anon'}"


def _check_rate_limit(endpoint: str) -> bool:
    if not _rate_limits_enabled():
        return True
    limit, window_s = _rate_limit_for(endpoint)
    key = _rl_key(endpoint)
    now = time()
    with _RL_LOCK:
        dq = _RL_BUCKETS[key]
        cutoff = now - window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
    return True


def _rl_block_response():
    resp = jsonify({"ok": False, "error": "rate_limited"})
    resp.status_code = 429
    resp.headers["Retry-After"] = "1"
    resp.headers["X-Core-RateLimit"] = "BLOCK"
    resp.headers["X-Core-Cache"] = "MISS"
    return resp


def _cache_get(key: str) -> dict | None:
    now = time()
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if row is None:
            return None
        exp, payload = row
        if exp < now:
            _CACHE.pop(key, None)
            return None
        return payload


def _cache_set(key: str, payload: dict, ttl_s: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time() + max(0.1, float(ttl_s)), payload)


def _ensure_core_access():
    if not _core_live_enabled():
        resp = jsonify({"ok": False, "error": "core_disabled"})
        resp.status_code = 503
        resp.headers["X-Core-RateLimit"] = "OK"
        resp.headers["X-Core-Cache"] = "MISS"
        return resp
    role = getattr(g, "auth_level", "public")
    if not is_core_access_allowed(request, role):
        return jsonify({"ok": False, "error": "core_access_denied"}), 403
    return None


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)




def _parse_iso_dt(raw: str | None) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None






def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / float(len(values))


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = _avg(values)
    var = sum((v - mean) ** 2 for v in values) / float(n - 1)
    return var ** 0.5
















@core_bp.get("/core")
def core_page():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_v2_ready()
    debug_mode = (request.args.get("debug") or "").strip() == "1"
    bootstrap_payload = get_bootstrap_payload() if debug_mode else None
    core_board_bootstrap = core_board_view_model(request.args.get("date"))
    return render_template(
        "core_v2.html",
        core_key=extract_core_key_from_request(request),
        core_enabled=True,
        debug_mode=debug_mode,
        core_bootstrap=bootstrap_payload,
        core_board_bootstrap=core_board_bootstrap,
    )


@core_bp.get("/core/control-room")
def core_page_legacy():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_control_room_schema()
    bootstrap_payload, _ = _get_control_room_payload_cached(30)
    return render_template(
        "core.html",
        core_key=extract_core_key_from_request(request),
        core_enabled=True,
        core_bootstrap=bootstrap_payload,
    )


@core_bp.get("/api/core/control-room")
def api_core_control_room():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_control_room_schema()
    try:
        days = max(7, min(90, int(request.args.get("days", "30"))))
    except Exception:
        days = 30
    payload, from_cache = _get_control_room_payload_cached(days)
    if from_cache:
        resp = jsonify(payload)
        resp.headers["X-Core-Cache"] = "HIT"
        return resp
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    return resp


@core_bp.post("/api/core/memory/<memory_key>/state")
def api_core_memory_state(memory_key: str):
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_control_room_schema()
    payload = request.get_json(silent=True) or {}
    try:
        state = update_memory_state(memory_key, str(payload.get("action") or "active"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "state": state})


@core_bp.get("/api/core/memory/inbox")
def api_core_memory_inbox():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("date") or "").strip() or None
    raw_limit = (request.args.get("limit") or "80").strip()
    try:
        limit = max(1, min(200, int(raw_limit)))
    except Exception:
        limit = 80
    return jsonify(get_memory_inbox(day, limit=limit))


@core_bp.post("/api/core/memory/control")
def api_core_memory_control():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        event_id = int(body.get("event_id"))
        event = control_memory_event(
            event_id,
            str(body.get("action") or ""),
            edits=body.get("edits") if isinstance(body.get("edits"), dict) else None,
            note=str(body.get("note") or "").strip() or None,
            actor="ui",
        )
        return jsonify(
            {
                "ok": True,
                "event": event,
                "execution": {
                    "mode": "applied_live",
                    "live_state_changed": True,
                    "affected_resources": ["core_memory_events"],
                },
            }
        )
    except ValueError as exc:
        code = str(exc)
        return jsonify({"ok": False, "error": code}), 400 if code != "event_not_found" else 404


@core_bp.get("/api/core/memory/consolidations")
def api_core_memory_consolidations():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    status = (request.args.get("status") or "").strip().lower() or None
    timeframe = (request.args.get("timeframe") or "").strip().lower() or None
    conflict_status = (request.args.get("conflict_status") or "").strip().lower() or None
    raw_limit = (request.args.get("limit") or "80").strip()
    try:
        limit = max(1, min(200, int(raw_limit)))
    except Exception:
        limit = 80
    return jsonify({"ok": True, "consolidations": get_memory_consolidations(status=status, timeframe=timeframe, limit=limit, conflict_status=conflict_status)})


@core_bp.post("/api/core/memory/consolidations/build")
def api_core_memory_consolidations_build():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        result = build_consolidation_drafts(
            timeframe=(str(body.get("timeframe") or "").strip().lower() or None),
            category=(str(body.get("category") or "").strip() or None),
            min_events=max(2, min(8, int(body.get("min_events") or 2))),
            status_filter=body.get("status_filter") if isinstance(body.get("status_filter"), list) else None,
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@core_bp.post("/api/core/memory/consolidations/control")
def api_core_memory_consolidations_control():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        consolidation_id = int(body.get("consolidation_id"))
        consolidation = control_consolidation(
            consolidation_id,
            str(body.get("action") or ""),
            mode=str(body.get("mode") or "").strip().lower() or None,
            edits=body.get("edits") if isinstance(body.get("edits"), dict) else None,
            note=str(body.get("note") or "").strip() or None,
            actor="ui",
        )
        return jsonify(
            {
                "ok": True,
                "consolidation": consolidation,
                "execution": {
                    "mode": "applied_live",
                    "live_state_changed": True,
                    "affected_resources": ["core_memory_consolidations"],
                },
            }
        )
    except ValueError as exc:
        code = str(exc)
        return jsonify({"ok": False, "error": code}), 400 if code != "consolidation_not_found" else 404


@core_bp.post("/api/core/memory/consolidations/batch")
def api_core_memory_consolidations_batch():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        consolidation_ids = body.get("ids")
        if not isinstance(consolidation_ids, list):
            consolidation_ids = body.get("consolidation_ids") if isinstance(body.get("consolidation_ids"), list) else None
        result = batch_control_consolidations(
            consolidation_ids,
            str(body.get("action") or ""),
            mode=str(body.get("mode") or "").strip().lower() or None,
            note=str(body.get("note") or "").strip() or None,
            reason=str(body.get("reason") or "").strip() or None,
            explicit_confirm_all=bool(body.get("explicit_confirm_all")),
            status=str(body.get("status") or "").strip().lower() or None,
            timeframe=str(body.get("timeframe") or "").strip().lower() or None,
            conflict_status=str(body.get("conflict_status") or "").strip().lower() or None,
            actor="ui",
        )
        return jsonify(
            {
                "ok": True,
                "result": result,
                "execution": {
                    "mode": "applied_live",
                    "live_state_changed": bool(result.get("changed")),
                    "affected_resources": ["core_memory_consolidations"],
                },
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@core_bp.get("/api/core/memory/file-patches")
def api_core_memory_file_patches():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    raw_limit = (request.args.get("limit") or "80").strip()
    try:
        limit = max(1, min(200, int(raw_limit)))
    except Exception:
        limit = 80
    return jsonify(
        {
            "ok": True,
            "patches": get_memory_file_patches(
                status=(request.args.get("status") or "").strip().lower() or None,
                target_memory_file=(request.args.get("target_memory_file") or "").strip() or None,
                target_section=(request.args.get("target_section") or "").strip() or None,
                safety_status=(request.args.get("safety_status") or "").strip().lower() or None,
                limit=limit,
            ),
        }
    )


@core_bp.post("/api/core/memory/file-patches/build")
def api_core_memory_file_patches_build():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        result = build_memory_file_patch_drafts(
            target_memory_file=(str(body.get("target_memory_file") or "").strip() or None),
            target_section=(str(body.get("target_section") or "").strip() or None),
            status_filter=body.get("status_filter") if isinstance(body.get("status_filter"), list) else None,
            min_consolidations=max(1, min(8, int(body.get("min_consolidations") or 2))),
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        code = str(exc)
        return jsonify({"ok": False, "error": {"code": code, "message": PATCH_ERROR_MESSAGES.get(code, code.replace("_", " ") )}}), 400


@core_bp.post("/api/core/memory/file-patches/control")
def api_core_memory_file_patches_control():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        patch_id = int(body.get("patch_id"))
        patch = control_memory_file_patch(
            patch_id,
            str(body.get("action") or ""),
            confirm=bool(body.get("confirm")),
            edits=body.get("edits") if isinstance(body.get("edits"), dict) else None,
            reason=str(body.get("reason") or "").strip() or None,
            actor="ui",
        )
        return jsonify(
            {
                "ok": True,
                "patch": patch,
                "execution": {
                    "mode": "applied_live",
                    "live_state_changed": True,
                    "affected_resources": ["core_memory_file_patches"],
                },
            }
        )
    except ValueError as exc:
        code = str(exc)
        return (
            jsonify({"ok": False, "error": {"code": code, "message": PATCH_ERROR_MESSAGES.get(code, code.replace("_", " ") )}}),
            400 if code != "memory_file_patch_not_found" else 404,
        )


def _route_int_id(raw: str) -> int | None:
    try:
        return int(str(raw or "").strip())
    except Exception:
        return None


@core_bp.get("/api/core/v2/today")
def api_core_v2_today():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    return jsonify(get_today_payload(day_iso=day))


@core_bp.get("/api/core/v2/explain")
def api_core_v2_explain():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    force = _to_bool(request.args.get("force"), False)
    return jsonify(get_explain_v2_payload(day_iso=day, force_rebuild=force))


@core_bp.get("/api/core/v2/decision")
def api_core_v2_decision():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    return jsonify(get_decision_v2_payload(day_iso=day))


@core_bp.get("/api/core/v2/night-cycle")
def api_core_v2_night_cycle():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    force = _to_bool(request.args.get("force"), False)
    if force:
        return jsonify(get_explain_v2_payload(day_iso=day, force_rebuild=True))
    return jsonify(get_night_cycle_payload(day_iso=day))


@core_bp.get("/api/core/v2/clone")
def api_core_v2_clone():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    return jsonify(get_clone_payload(day_iso=day))


@core_bp.get("/api/core/v2/scenarios")
def api_core_v2_scenarios():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    return jsonify(get_scenarios_payload(day_iso=day))


@core_bp.get("/api/core/v2/parameter-learning")
def api_core_v2_parameter_learning():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    day = (request.args.get("day") or "").strip() or None
    return jsonify(get_parameter_learning_payload(day_iso=day))


@core_bp.get("/api/core/v2/learned")
def api_core_v2_learned():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    return jsonify(get_learned_payload())


@core_bp.get("/api/core/v2/reviews")
def api_core_v2_reviews():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    domain = (request.args.get("domain") or "").strip() or None
    outcome = (request.args.get("outcome") or "").strip() or None
    days = _to_int(request.args.get("days"), 30)
    days = max(7, min(365, days))
    return jsonify(get_reviews_payload(domain=domain, outcome=outcome, days=days))


@core_bp.get("/api/core/v2/model")
def api_core_v2_model():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_v2_ready()
    return jsonify(get_model_state_payload())


@core_bp.get("/api/core/v2/timeline")
def api_core_v2_timeline():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    days = _to_int(request.args.get("days"), 60)
    days = max(14, min(365, days))
    return jsonify(get_timeline_payload(days=days))


@core_bp.get("/api/core/v2/decision/<decision_id>/explain")
def api_core_v2_decision_explain(decision_id: str):
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    did = _route_int_id(decision_id)
    if not did:
        return jsonify({"ok": False, "error": "invalid_decision_id"}), 400
    payload = get_decision_explain_payload(did)
    if not payload.get("ok"):
        return jsonify(payload), 404
    return jsonify(payload)


@core_bp.get("/api/core/board")
def api_core_board():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    try:
        return jsonify(core_board_view_model(request.args.get("date")))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@core_bp.get("/api/core/v2/hypothesis/<hypothesis_id>")
def api_core_v2_hypothesis(hypothesis_id: str):
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    hid = _route_int_id(hypothesis_id)
    if not hid:
        return jsonify({"ok": False, "error": "invalid_hypothesis_id"}), 400
    payload = get_hypothesis_payload(hid)
    if not payload.get("ok"):
        return jsonify(payload), 404
    return jsonify(payload)


@core_bp.get("/api/core/v2/pattern/<pattern_id>")
def api_core_v2_pattern(pattern_id: str):
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    pid = _route_int_id(pattern_id)
    if not pid:
        return jsonify({"ok": False, "error": "invalid_pattern_id"}), 400
    payload = get_pattern_payload(pid)
    if not payload.get("ok"):
        return jsonify(payload), 404
    return jsonify(payload)


@core_bp.post("/api/core/v2/recompute")
def api_core_v2_recompute():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    day = (payload.get("day") if isinstance(payload, dict) else "") or None
    run_reviews = _to_bool(str(payload.get("run_reviews")) if isinstance(payload, dict) and payload.get("run_reviews") is not None else None, True)
    review_days = max(7, min(365, _to_int(str(payload.get("review_days")) if isinstance(payload, dict) and payload.get("review_days") is not None else None, 45)))
    model_lookback_days = max(21, min(730, _to_int(str(payload.get("model_lookback_days")) if isinstance(payload, dict) and payload.get("model_lookback_days") is not None else None, 60)))
    out = recompute_core_v2(
        day_iso=day,
        run_reviews=run_reviews,
        review_days=review_days,
        model_lookback_days=model_lookback_days,
    )
    return jsonify(out)


@core_bp.post("/api/core/v2/review/run")
def api_core_v2_review_run():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    days = max(7, min(180, _to_int(str(payload.get("days")) if isinstance(payload, dict) else None, 45)))
    out = run_review_pass(days=days)
    return jsonify(out)


@core_bp.post("/api/core/v2/night-cycle/backfill")
def api_core_v2_night_cycle_backfill():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    days = max(7, min(365, _to_int(str(payload.get("days")) if isinstance(payload, dict) and payload.get("days") is not None else None, 90)))
    review_days = max(7, min(365, _to_int(str(payload.get("review_days")) if isinstance(payload, dict) and payload.get("review_days") is not None else None, 180)))
    model_lookback_days = max(21, min(730, _to_int(str(payload.get("model_lookback_days")) if isinstance(payload, dict) and payload.get("model_lookback_days") is not None else None, 365)))
    run_reviews = _to_bool(str(payload.get("run_reviews")) if isinstance(payload, dict) and payload.get("run_reviews") is not None else None, True)
    out = backfill_night_cycle_history(
        days=days,
        review_days=review_days,
        model_lookback_days=model_lookback_days,
        run_reviews=run_reviews,
    )
    code = 200 if out.get("ok") else 207
    return jsonify(out), code


@core_bp.get("/api/core/graph")
def api_core_graph():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("graph"):
        return _rl_block_response()

    focus = (request.args.get("focus") or "").strip() or None
    depth = request.args.get("depth", str(DEFAULT_DEPTH))
    max_nodes = request.args.get("max_nodes", str(DEFAULT_MAX_NODES))
    max_edges = request.args.get("max_edges", "600")
    global_graph = (request.args.get("global") or "").strip().lower() in {"1", "true", "yes", "on"}
    rebuild = (request.args.get("rebuild") or "").strip().lower() in {"1", "true", "yes", "on"}

    if rebuild:
        rebuild_core_index()

    cache_key = f"graph:{focus or ''}:{depth}:{max_nodes}:{max_edges}:{1 if global_graph else 0}"
    cached = _cache_get(cache_key)
    if cached is not None and not rebuild:
        resp = jsonify(cached)
        resp.headers["X-Core-Cache"] = "HIT"
        resp.headers["X-Core-RateLimit"] = "OK"
        return resp

    payload = get_core_graph_bundle(
        focus=focus,
        depth=_to_int(depth, DEFAULT_DEPTH),
        max_nodes=_to_int(max_nodes, DEFAULT_MAX_NODES),
        max_edges=_to_int(max_edges, 600),
        global_graph=global_graph,
    )
    _cache_set(cache_key, payload, _cache_ttl_graph())
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/search")
def api_core_search():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("search"):
        return _rl_block_response()
    q = (request.args.get("q") or "").strip()
    payload = search_core_nodes(q, limit=40)
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/recent")
def api_core_recent():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("recent"):
        return _rl_block_response()
    payload = get_recent_activity(limit=24)
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/rebuild")
def api_core_rebuild():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = rebuild_core_index()
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/state")
def api_core_state():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("state"):
        return _rl_block_response()
    window = (request.args.get("window") or "today").strip().lower()
    if window not in {"live", "today", "7d"}:
        window = "today"
    cache_key = f"state:{window}"
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["X-Core-Cache"] = "HIT"
        resp.headers["X-Core-RateLimit"] = "OK"
        return resp
    payload = get_core_state(window)
    _cache_set(cache_key, payload, _cache_ttl_state(window))
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/summary")
def api_core_summary():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("summary"):
        return _rl_block_response()
    mode = (request.args.get("mode") or "live").strip().lower()
    if mode not in {"live", "today", "7d"}:
        mode = "today"
    cache_key = f"summary:{mode}"
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["X-Core-Cache"] = "HIT"
        resp.headers["X-Core-RateLimit"] = "OK"
        return resp
    payload = get_core_summary(mode)
    _cache_set(cache_key, payload, _cache_ttl_summary(mode))
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/intel")
def api_core_intel():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("intel"):
        return _rl_block_response()
    mode = (request.args.get("mode") or "today").strip().lower()
    day = (request.args.get("day") or "").strip() or None
    force = (request.args.get("force") or "").strip().lower() in {"1", "true", "yes", "on"}
    cache_key = f"intel:{mode}:{day or ''}"
    if not force:
        cached = _cache_get(cache_key)
        if cached is not None:
            resp = jsonify(cached)
            resp.headers["X-Core-Cache"] = "HIT"
            resp.headers["X-Core-RateLimit"] = "OK"
            return resp
    payload = get_core_intel_payload(mode=mode, day_iso=day, force=force)
    if not force:
        _cache_set(cache_key, payload, _cache_ttl_intel())
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/heartbeat")
def api_core_heartbeat():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("heartbeat"):
        return _rl_block_response()
    session_id = (request.args.get("session_id") or request.cookies.get("th_core_sid") or "").strip()
    route = (request.args.get("route") or "/core").strip()
    payload = record_heartbeat(session_id, route)
    resp = jsonify({"ok": True, "heartbeat": payload})
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/node/<node_id>")
def api_core_node_details(node_id: str):
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    window = (request.args.get("window") or "today").strip().lower()
    return jsonify(get_core_node_details(node_id, window))


@core_bp.get("/api/core/snapshot")
def api_core_snapshot():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("snapshot"):
        return _rl_block_response()
    mode = (request.args.get("mode") or "live").strip().lower()
    if mode not in {"live", "today", "7d"}:
        mode = "today"
    cache_key = f"snapshot:{mode}"
    cached = _cache_get(cache_key)
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["X-Core-Cache"] = "HIT"
        resp.headers["X-Core-RateLimit"] = "OK"
        return resp
    payload = get_core_observatory_snapshot(mode=mode)
    _cache_set(cache_key, payload, _cache_ttl_snapshot(mode))
    resp = jsonify(payload)
    resp.headers["X-Core-Cache"] = "MISS"
    resp.headers["X-Core-RateLimit"] = "OK"
    return resp


@core_bp.get("/api/core/stream")
def core_observatory_stream():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    if not _check_rate_limit("stream"):
        return _rl_block_response()

    @stream_with_context
    def _stream():
        for chunk in core_stream_events():
            yield chunk

    resp = Response(_stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["X-Core-RateLimit"] = "OK"
    resp.headers["X-Core-Cache"] = "MISS"
    return resp


@core_bp.get("/ws/core")
def core_stream():
    denied = _ensure_core_access()
    if denied is not None:
        return denied

    @stream_with_context
    def _stream():
        for chunk in sse_event_stream():
            yield chunk

    resp = Response(_stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@core_bp.get("/api/core/profile")
def api_core_profile():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_training_schema()
    return jsonify({"ok": True, "profile": get_core_profile()})


@core_bp.post("/api/core/profile")
def api_core_profile_update():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    try:
        ensure_core_training_schema()
        profile = update_core_profile(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "profile": profile})


@core_bp.post("/api/core/training/session/start")
def api_core_training_session_start():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "").strip().lower()
    try:
        ensure_core_training_schema()
        out = start_core_training_session(mode)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **out})


@core_bp.post("/api/core/training/session/end")
def api_core_training_session_end():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        session_id = int(payload.get("session_id"))
    except Exception:
        return jsonify({"ok": False, "error": "invalid_session_id"}), 400
    try:
        ensure_core_training_schema()
        out = end_core_training_session(session_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "session": out})


@core_bp.get("/api/core/training/next_card")
def api_core_training_next_card():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    raw = (request.args.get("session_id") or "").strip()
    try:
        session_id = int(raw)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_session_id"}), 400
    try:
        ensure_core_training_schema()
        payload = next_core_training_card(session_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **payload})


@core_bp.post("/api/core/training/label")
def api_core_training_label():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    try:
        session_id = int(payload.get("session_id"))
        card_id = int(payload.get("card_id"))
    except Exception:
        return jsonify({"ok": False, "error": "invalid_ids"}), 400
    try:
        out = submit_core_training_label(
            session_id=session_id,
            card_id=card_id,
            label=str(payload.get("label") or ""),
            confidence_user=payload.get("confidence_user"),
            free_text=payload.get("free_text"),
            adjustments=payload.get("adjustments"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(out)


@core_bp.get("/api/core/training/stats")
def api_core_training_stats():
    denied = _ensure_core_access()
    if denied is not None:
        return denied
    ensure_core_training_schema()
    return jsonify({"ok": True, "stats": get_core_training_stats()})
