from __future__ import annotations

import json

import pytest

from control_center import emergency, self_healing, store
from control_center import collector


def test_status_is_read_only(monkeypatch):
    monkeypatch.setattr(emergency, "_systemd", lambda unit: {"unit": unit, "available": True})
    monkeypatch.setattr(emergency, "db_status", lambda: {"available": True, "integrity": "ok"})
    monkeypatch.setattr(emergency, "_recovery_status", lambda: {"state": {}})
    monkeypatch.setattr(emergency, "storage_status", lambda: {"state": "healthy"})
    monkeypatch.setattr(emergency.store, "list_incidents", lambda **_: [])
    monkeypatch.setattr(emergency.store, "get_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(emergency.releases, "status", lambda: {"in_progress": False})
    value = emergency.status()
    assert value["ok"] is True
    assert value["root_storage"]["state"] == "healthy"


def test_restart_is_allowlisted_and_audited(monkeypatch):
    markers = []
    monkeypatch.setattr(emergency.store, "get_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(emergency.store, "set_state", lambda key, value, **_kwargs: markers.append((key, value)))
    captured = {}
    def action(*args, **kwargs):
        captured.update({"args": args, **kwargs})
        return {"audit": {"result": "success", "actor_type": kwargs["actor_type"]}}
    monkeypatch.setattr(emergency.jobs, "action", action)
    result = emergency.restart("liva")
    assert result["audit"]["result"] == "success"
    assert captured["args"] == ("core-liva", "restart")
    assert captured["actor_type"] == "emergency_admin"
    assert markers[0][1]["active"] is True and markers[-1][1]["active"] is False


def test_restart_rejects_unknown_target():
    with pytest.raises(ValueError):
        emergency.restart("anything; systemctl poweroff")


def test_active_emergency_operation_blocks_second_restart(monkeypatch):
    monkeypatch.setattr(emergency.store, "get_state", lambda *_args, **_kwargs: {"active": True, "started_at": emergency.store.utc_now()})
    with pytest.raises(RuntimeError, match="in_progress"):
        emergency.restart("mcp")


def test_self_healing_skips_an_active_emergency_operation(monkeypatch):
    monkeypatch.setattr(self_healing.store, "get_state", lambda key, **_kwargs: {"active": True} if key == "emergency:operation" else None)
    result = self_healing.run_once(path="/tmp/emergency-coordination-test.sqlite3")
    assert result["actions"] == []
    assert "emergency_operation_in_progress" in result["skipped"]


def test_self_healing_changes_canonical_state_and_audits(tmp_path):
    path = str(tmp_path / "control.sqlite3")
    monkeypatch = pytest.MonkeyPatch()
    original_global_state = self_healing.global_state
    original_set_enabled = self_healing.set_global_enabled
    original_config_audit = store.record_config_audit
    original_record_event = store.record_event
    try:
        monkeypatch.setattr(emergency.self_healing, "global_state", lambda: original_global_state(path=path))
        monkeypatch.setattr(emergency.self_healing, "set_global_enabled", lambda enabled: original_set_enabled(enabled, path=path))
        monkeypatch.setattr(emergency.store, "record_config_audit", lambda value: original_config_audit(value, path=path))
        monkeypatch.setattr(emergency.store, "record_event", lambda value: original_record_event(value, path=path))
        assert emergency.set_self_healing(False)["enabled"] is False
        assert original_global_state(path=path)["enabled"] is False
    finally:
        monkeypatch.undo()


def test_cli_rejects_arbitrary_command():
    with pytest.raises(SystemExit):
        emergency.main(["restart", "liva.service;id"])


@pytest.mark.parametrize(("free", "expected"), [(4 * 1024**3, "healthy"), (2 * 1024**3, "degraded"), (512 * 1024**2, "critical")])
def test_storage_thresholds(monkeypatch, free, expected):
    class Usage:
        def __init__(self, free): self.free = free
    monkeypatch.setattr(emergency.shutil, "disk_usage", lambda _path: Usage(free))
    assert emergency.storage_status()["state"] == expected


def test_critical_storage_creates_bounded_critical_event(monkeypatch, tmp_path):
    monkeypatch.setattr(emergency, "storage_status", lambda: {"state": "critical", "free_bytes": 1})
    events = collector.collect_storage(db_path=str(tmp_path / "control.sqlite3"))
    assert len(events) == 1
    assert events[0]["severity"] == "critical"
