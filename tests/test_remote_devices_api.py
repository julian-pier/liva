from __future__ import annotations

import importlib
import os

import smart_home.config as sh_config
import smart_home.service as sh_service


def _load_app_module():
    os.environ.setdefault("LIVA_FLASK_SECRET", "test-secret")
    return importlib.import_module("app")


def test_remote_devices_payload_prefers_live_tapo_state_over_stale_cache(tmp_path, monkeypatch):
    appmod = _load_app_module()
    monkeypatch.setattr(appmod, "_remote_devices_state_path", tmp_path / "remote_devices_state.json")
    appmod._remote_devices_state.clear()
    appmod._remote_device_set_cached_state("monitor_links", is_on=False, reachable=True)

    monkeypatch.setattr(
        sh_config,
        "TAPO_DEVICES",
        {"monitor_links": {"alias": "Monitor links", "ip": "192.0.2.71"}},
    )
    monkeypatch.setattr(sh_config, "GOVEE_DEVICES", {})

    seen = []

    def fake_get_state(device_name: str, **kwargs):
        seen.append((device_name, kwargs))
        return {"ok": True, "device": device_name, "state": True}

    monkeypatch.setattr(sh_service, "get_state", fake_get_state)

    payload = appmod._remote_devices_payload()

    assert payload["ok"] is True
    assert payload["devices"][0]["id"] == "monitor_links"
    assert payload["devices"][0]["is_on"] is True
    assert payload["devices"][0]["reachable"] is True
    assert appmod._remote_device_get_cached_state("monitor_links")["is_on"] is True
    assert seen == [("monitor_links", {"retries": 1, "delay": 0.0, "timeout": 1.6})]


def test_remote_devices_payload_keeps_cached_tapo_state_when_live_read_fails(tmp_path, monkeypatch):
    appmod = _load_app_module()
    monkeypatch.setattr(appmod, "_remote_devices_state_path", tmp_path / "remote_devices_state.json")
    appmod._remote_devices_state.clear()
    appmod._remote_device_set_cached_state("monitor_links", is_on=True, reachable=True)

    monkeypatch.setattr(
        sh_config,
        "TAPO_DEVICES",
        {"monitor_links": {"alias": "Monitor links", "ip": "192.0.2.71"}},
    )
    monkeypatch.setattr(sh_config, "GOVEE_DEVICES", {})

    def fake_get_state(device_name: str, **kwargs):
        raise RuntimeError(f"{device_name} offline")

    monkeypatch.setattr(sh_service, "get_state", fake_get_state)

    payload = appmod._remote_devices_payload()

    assert payload["ok"] is True
    assert payload["devices"][0]["id"] == "monitor_links"
    assert payload["devices"][0]["is_on"] is True
    assert payload["devices"][0]["reachable"] is True
