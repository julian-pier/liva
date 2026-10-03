from __future__ import annotations

import secrets
from datetime import date, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, make_response, redirect, request, session

from integrations.polar_client import (
    PolarClient,
    PolarClientError,
    PolarNotConfiguredError,
    PolarNotConnectedError,
    ensure_polar_schema,
    log_polar_sync,
)
from integrations.polar_sync import latest_polar_snapshot, sync_polar_range

polar_bp = Blueprint("polar_api", __name__, url_prefix="/api/polar")

POLAR_AUTH_URL = "https://auth.polar.com/oauth/authorize"
POLAR_SCOPES = [
    "sleep:read",
    "nightly_recharge:read",
    "continuous_samples:read",
    "activity:read",
    "training_sessions:read",
    "profile:read",
]


def _payload_debug_summary(payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "payload_type": type(payload).__name__,
        "root_keys": [],
        "item_count": None,
        "first_item_keys": [],
        "has_sleepScore": False,
        "has_sleepResult": False,
        "has_sleepEvaluation": False,
    }
    first_item = None
    if isinstance(payload, dict):
        summary["root_keys"] = list(payload.keys())
        if isinstance(payload.get("nightSleeps"), list):
            items = payload.get("nightSleeps") or []
            summary["item_count"] = len(items)
            first_item = items[0] if items else None
    elif isinstance(payload, list):
        summary["item_count"] = len(payload)
        first_item = payload[0] if payload else None
    if isinstance(first_item, dict):
        summary["first_item_keys"] = list(first_item.keys())
        summary["has_sleepScore"] = "sleepScore" in first_item and first_item.get("sleepScore") not in (None, "")
        summary["has_sleepResult"] = "sleepResult" in first_item and first_item.get("sleepResult") not in (None, "")
        summary["has_sleepEvaluation"] = "sleepEvaluation" in first_item and first_item.get("sleepEvaluation") not in (None, "")
    return summary


def create_polar_client() -> PolarClient:
    return PolarClient(
        client_id=current_app.config.get("POLAR_CLIENT_ID"),
        client_secret=current_app.config.get("POLAR_CLIENT_SECRET"),
        redirect_uri=current_app.config.get("POLAR_REDIRECT_URI"),
    )


def init_polar_app(app) -> None:
    app.config["POLAR_CLIENT_ID"] = (app.config.get("POLAR_CLIENT_ID") or "").strip()
    app.config["POLAR_CLIENT_SECRET"] = (app.config.get("POLAR_CLIENT_SECRET") or "").strip()
    app.config["POLAR_REDIRECT_URI"] = (app.config.get("POLAR_REDIRECT_URI") or "").strip()
    ensure_polar_schema()


def _config_error_response(exc: PolarNotConfiguredError):
    return jsonify({"ok": False, "message": str(exc)}), HTTPStatus.INTERNAL_SERVER_ERROR


@polar_bp.get("/connect")
def polar_connect():
    client = create_polar_client()
    try:
        client.require_configured()
    except PolarNotConfiguredError as exc:
        return _config_error_response(exc)
    state = secrets.token_urlsafe(24)
    session["polar_oauth_state"] = state
    params = {
        "client_id": current_app.config["POLAR_CLIENT_ID"],
        "response_type": "code",
        "scope": " ".join(POLAR_SCOPES),
        "redirect_uri": current_app.config["POLAR_REDIRECT_URI"],
        "state": state,
    }
    return redirect(f"{POLAR_AUTH_URL}?{urlencode(params)}", code=302)


@polar_bp.get("/callback")
def polar_callback():
    client = create_polar_client()
    try:
        client.require_configured()
    except PolarNotConfiguredError as exc:
        return make_response(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    error = (request.args.get("error") or "").strip()
    if error:
        message = f"Polar-Verbindung abgebrochen oder fehlgeschlagen: {error}"
        log_polar_sync("oauth_callback", "error", message)
        return make_response(message, HTTPStatus.BAD_REQUEST)
    state = (request.args.get("state") or "").strip()
    expected_state = str(session.pop("polar_oauth_state", "") or "").strip()
    if expected_state and state != expected_state:
        log_polar_sync("oauth_callback", "error", "Polar OAuth state ungültig.")
        return make_response("Ungültiger Polar-OAuth-Status.", HTTPStatus.BAD_REQUEST)
    code = (request.args.get("code") or "").strip()
    if not code:
        log_polar_sync("oauth_callback", "error", "Polar OAuth callback ohne Code empfangen.")
        return make_response("Kein OAuth-Code von Polar erhalten.", HTTPStatus.BAD_REQUEST)
    try:
        token = client.exchange_code(code)
    except PolarClientError as exc:
        log_polar_sync("oauth_callback", "error", str(exc))
        return make_response(f"Polar-Verbindung fehlgeschlagen: {exc}", HTTPStatus.BAD_GATEWAY)
    log_polar_sync(
        "oauth_callback",
        "ok",
        f"Polar erfolgreich verbunden. Scope: {token.scope or 'unbekannt'}",
    )
    return make_response("Polar verbunden. Du kannst dieses Fenster schließen.", HTTPStatus.OK)


@polar_bp.post("/disconnect")
def polar_disconnect():
    client = create_polar_client()
    deleted = client.disconnect()
    return jsonify({"ok": True, "message": "Polar-Verbindung lokal entfernt.", "deleted_tokens": deleted})


@polar_bp.get("/status")
def polar_status():
    client = create_polar_client()
    token = client.get_saved_token()
    needs_reconnect = client.needs_reconnect()
    return jsonify(
        {
            "connected": bool(token),
            "polar_user_id": token.polar_user_id if token else None,
            "expires_at": token.expires_at if token else None,
            "has_refresh_token": bool(token.refresh_token) if token else False,
            "needs_reconnect": bool(needs_reconnect),
        }
    )


@polar_bp.post("/test-sync")
def polar_test_sync():
    from app import _breathing_debug_from_raw

    client = create_polar_client()
    try:
        client.require_configured()
    except PolarNotConfiguredError as exc:
        return _config_error_response(exc)
    token = client.get_saved_token()
    if not token:
        return jsonify({"ok": False, "message": "Polar ist noch nicht verbunden."})
    today = date.today()
    sample_params = {
        "from": (today - timedelta(days=7)).isoformat(),
        "to": today.isoformat(),
    }
    debug_day = today - timedelta(days=1)
    debug_from = debug_day.isoformat()
    debug_to = (debug_day + timedelta(days=1)).isoformat()

    def _run_sleep_variant(label: str, *, features: list[str] | None = None) -> dict[str, Any]:
        try:
            payload = client.get_sleeps(debug_from, debug_to, features=features)
            return {"ok": True, "payload": payload, **_payload_debug_summary(payload)}
        except PolarClientError as exc:
            return {"ok": False, "error": str(exc)}

    def _run_nightly_variant() -> dict[str, Any]:
        try:
            if not hasattr(client, "get_nightly_recharge"):
                return {"ok": False, "error": "Nightly-Recharge-Debug im Stub nicht verfügbar."}
            payload = client.get_nightly_recharge(debug_from, debug_to)
            item = None
            if isinstance(payload, dict):
                if isinstance(payload.get("nightlyRechargeResults"), list) and payload.get("nightlyRechargeResults"):
                    item = payload.get("nightlyRechargeResults")[0]
                elif isinstance(payload.get("nightlyRechargeResults"), dict):
                    nested = payload.get("nightlyRechargeResults", {}).get("nightlyRechargeResults")
                    if isinstance(nested, list) and nested:
                        item = nested[0]
            elif isinstance(payload, list) and payload:
                item = payload[0]
            debug = _breathing_debug_from_raw(item or payload)
            debug["ok"] = True
            return debug
        except PolarClientError as exc:
            return {"ok": False, "error": str(exc)}

    try:
        sample = client.get_json("/sleeps", params=sample_params)
    except PolarNotConnectedError:
        return jsonify({"ok": False, "message": "Polar ist noch nicht verbunden."})
    except PolarClientError as exc:
        log_polar_sync("test_sync", "error", str(exc))
        return jsonify({"ok": False, "message": f"Polar API nicht erreichbar: {exc}"})
    sleep_without = _run_sleep_variant("without_features")
    sleep_score = _run_sleep_variant("sleep_score", features=["sleep-score"])
    sleep_multi = _run_sleep_variant(
        "sleep_multi_feature",
        features=["sleep-score", "sleep-result", "sleep-evaluation"],
    )
    sleep_payload = sleep_multi.get("payload") if sleep_multi.get("ok") else sleep_score.get("payload") if sleep_score.get("ok") else sleep_without.get("payload")
    if isinstance(sleep_without, dict):
        sleep_without.pop("payload", None)
    if isinstance(sleep_score, dict):
        sleep_score.pop("payload", None)
    if isinstance(sleep_multi, dict):
        sleep_multi.pop("payload", None)
    log_polar_sync("test_sync", "ok", "Polar-Test-Sync erfolgreich.")
    return jsonify(
        {
            "ok": True,
            "message": "Polar API erreichbar",
            "sample": sample,
            "sleep_debug": {
                "debug_day": debug_day.isoformat(),
                "without_features": sleep_without,
                "sleep_score": sleep_score,
                "sleep_multi_feature": sleep_multi,
            },
            "breathing_debug": {
                "debug_day": debug_day.isoformat(),
                "nightly": _run_nightly_variant(),
                "sleep": {
                    "ok": True,
                    "found_paths": _breathing_debug_from_raw(sleep_payload).get("found_paths", []) if sleep_payload is not None else [],
                },
            },
        }
    )


@polar_bp.post("/sync")
def polar_sync():
    client = create_polar_client()
    try:
        client.require_configured()
    except PolarNotConfiguredError as exc:
        return _config_error_response(exc)
    token = client.get_saved_token()
    if not token:
        return jsonify({"ok": False, "message": "Polar ist noch nicht verbunden."})
    body = request.get_json(silent=True)
    payload = body if isinstance(body, dict) else {}
    try:
        days = max(1, min(int(payload.get("days") or 14), 60))
    except Exception:
        return jsonify({"ok": False, "message": "days muss eine Zahl zwischen 1 und 60 sein."}), HTTPStatus.BAD_REQUEST
    try:
        summary = sync_polar_range(client, days=days)
    except PolarClientError as exc:
        log_polar_sync("sync_range", "error", str(exc))
        return jsonify({"ok": False, "message": f"Polar-Sync fehlgeschlagen: {exc}"})
    return jsonify(summary)


@polar_bp.get("/latest")
def polar_latest():
    return jsonify(latest_polar_snapshot())
