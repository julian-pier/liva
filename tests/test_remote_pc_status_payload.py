from __future__ import annotations

from tests.test_autopilot_frontend_payload_regression import _load_app_module


def test_remote_pc_status_payload_is_read_only(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    ready_markers = []

    monkeypatch.setattr(appmod, "_remote_mark_ready_seen", lambda checked_at: ready_markers.append(checked_at))
    monkeypatch.setattr(
        appmod,
        "read_automation_state",
        lambda: {
            "state": "PC_ON",
            "is_online": True,
            "last_evaluation": {
                "checks": [
                    {"name": "tcp:3389", "ok": True},
                    {"name": "listener_ping", "ok": False},
                ]
            },
        },
    )

    payload = appmod._remote_pc_status_payload()

    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["rdp_port_open"] is True
    assert payload["host_reachable"] is True
    assert payload["automation_state"]["state"] == "PC_ON"
    assert ready_markers
