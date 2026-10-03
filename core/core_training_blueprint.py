from __future__ import annotations

from flask import Blueprint, g, jsonify, render_template, request

from core.core_service import extract_core_key_from_request, is_core_access_allowed
from core.core_training_deck import answer_card, delete_card, ensure_core_training_deck_schema, get_core_training_live_stats, next_card, reset_deck, undo_last_answer

core_training_bp = Blueprint("core_training_bp", __name__)


def _denied(write: bool = False):
    role = getattr(g, "auth_level", "public")
    if not is_core_access_allowed(request, role):
        return jsonify({"ok": False, "error": "core_access_denied"}), 403
    if write and request.method != "GET":
        token = request.headers.get("X-CSRF-Token") or ""
        if role == "key" and not token:
            return jsonify({"ok": False, "error": "csrf_missing"}), 403
    return None


@core_training_bp.get("/core-training")
def core_training_page():
    denied = _denied(write=False)
    if denied is not None:
        return denied
    return render_template(
        "core_training_gate.html",
        core_key=extract_core_key_from_request(request),
        core_enabled=True,
    )


@core_training_bp.get("/core-training/cards")
def core_training_cards_page():
    denied = _denied(write=False)
    if denied is not None:
        return denied
    ensure_core_training_deck_schema()
    return render_template(
        "core_training.html",
        core_key=extract_core_key_from_request(request),
        core_enabled=True,
        core_training_stats=get_core_training_live_stats(),
    )


@core_training_bp.get("/api/core_training/next")
def api_core_training_next():
    denied = _denied(write=False)
    if denied is not None:
        return denied
    ensure_core_training_deck_schema()
    raw_recent = (request.args.get("recent_signatures") or "").strip()
    recent = [part for part in raw_recent.split(",") if part][:24]
    payload = next_card(recent_signatures=recent)
    return jsonify(payload)


@core_training_bp.post("/api/core_training/answer")
def api_core_training_answer():
    denied = _denied(write=True)
    if denied is not None:
        return denied
    ensure_core_training_deck_schema()
    payload = request.get_json(silent=True) or {}
    raw_recent = payload.get("recent_signatures") or []
    if isinstance(raw_recent, str):
        recent = [part for part in raw_recent.split(",") if part][:24]
    elif isinstance(raw_recent, list):
        recent = [str(part) for part in raw_recent[:24] if str(part).strip()]
    else:
        recent = []
    result = answer_card(
        card_id=str(payload.get("card_id") or ""),
        signature=str(payload.get("signature") or ""),
        answer=str(payload.get("answer") or "no"),
        client_ms=int(payload.get("client_ms") or 0),
        recent_signatures=recent,
    )
    return jsonify(result)


@core_training_bp.post("/api/core_training/reset")
def api_core_training_reset():
    denied = _denied(write=True)
    if denied is not None:
        return denied
    reset_deck()
    return jsonify({"ok": True})


@core_training_bp.post("/api/core_training/undo")
def api_core_training_undo():
    denied = _denied(write=True)
    if denied is not None:
        return denied
    ensure_core_training_deck_schema()
    result = undo_last_answer()
    status = 200 if result.get("ok") else 409
    return jsonify(result), status


@core_training_bp.post("/api/core_training/delete")
def api_core_training_delete():
    denied = _denied(write=True)
    if denied is not None:
        return denied
    ensure_core_training_deck_schema()
    payload = request.get_json(silent=True) or {}
    result = delete_card(str(payload.get("signature") or ""))
    status = 200 if result.get("ok") else 404
    return jsonify(result), status
