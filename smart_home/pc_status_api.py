from __future__ import annotations

import logging

from flask import Blueprint, jsonify

from smart_home.pc_monitor_automation import PCStatusSignalStore, read_automation_state

LOG = logging.getLogger(__name__)
LOG_PREFIX = "[pc-status-api]"

pc_status_api = Blueprint("pc_status_api", __name__, url_prefix="/api/pc-status")
_signal_store = PCStatusSignalStore()


def _payload(*, event: str | None = None) -> dict:
    state = read_automation_state()
    signal = _signal_store.read().to_dict()
    payload = {
        "ok": True,
        "state": state,
        "signal": signal,
    }
    if event:
        payload["event"] = event
    return payload


@pc_status_api.post("/online")
def pc_status_online():
    try:
        _signal_store.record_event("online")
        return jsonify(_payload(event="online"))
    except Exception as exc:
        LOG.exception("%s online signal failed: %s", LOG_PREFIX, exc)
        return jsonify({"ok": False, "error": "online_signal_failed", "detail": str(exc)}), 500


@pc_status_api.post("/offline")
def pc_status_offline():
    try:
        _signal_store.record_event("offline")
        return jsonify(_payload(event="offline"))
    except Exception as exc:
        LOG.exception("%s offline signal failed: %s", LOG_PREFIX, exc)
        return jsonify({"ok": False, "error": "offline_signal_failed", "detail": str(exc)}), 500


@pc_status_api.get("/state")
def pc_status_state():
    try:
        return jsonify(_payload())
    except Exception as exc:
        LOG.exception("%s state read failed: %s", LOG_PREFIX, exc)
        return jsonify({"ok": False, "error": "state_read_failed", "detail": str(exc)}), 500
