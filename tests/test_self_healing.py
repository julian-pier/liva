from __future__ import annotations

from datetime import datetime, timezone

from control_center import self_healing, store


def _incident(path, *, component="garmin-sync", error_class="network_timeout", correlation="garmin:sync"):
    created = store.record_event({
        "idempotency_key": f"fixture:{component}:{error_class}:{correlation}", "component_id": component,
        "source": "fixture", "event_type": "failed", "severity": "warning", "message": "fixture failure",
        "error_class": error_class, "correlation_key": correlation,
    }, path=path)
    return created["incident_id"]


def test_default_is_off_and_executor_is_read_only(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    _incident(path)
    monkeypatch.setattr(self_healing.jobs, "action", lambda **_: (_ for _ in ()).throw(AssertionError("must not act")))

    result = self_healing.run_once(path=str(path))

    assert result["enabled"] is False
    assert result["actions"] == []


def test_only_explicit_transient_incident_runs_same_action_engine_and_resolves(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"; incident_id = _incident(path)
    self_healing.set_global_enabled(True, path=str(path))
    calls = []
    monkeypatch.setattr(self_healing.jobs, "action", lambda *args, **kwargs: calls.append((args, kwargs)) or {"audit": {"result": "success"}})

    result = self_healing.run_once(path=str(path), now=datetime(2026, 9, 24, tzinfo=timezone.utc))

    assert len(result["actions"]) == 1
    assert calls[0][0] == ("garmin-sync", "run")
    assert calls[0][1]["actor_type"] == "self_healing"
    assert store.get_incident(incident_id, path=str(path))["status"] == "resolved"


def test_unknown_permanent_and_unsafe_errors_never_run(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"; self_healing.set_global_enabled(True, path=str(path))
    _incident(path, component="garmin-sync", error_class="unparseable")
    _incident(path, component="endurance-intervals", error_class="missing_execution_pace", correlation="endurance:pace")
    _incident(path, component="full-recovery", error_class="usb_missing", correlation="recovery:usb")
    monkeypatch.setattr(self_healing.jobs, "action", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not act")))

    result = self_healing.run_once(path=str(path))

    assert result["actions"] == []
    assert {item["reason"] for item in result["skipped"] if isinstance(item, dict)} >= {"unknown"}


def test_persistent_budget_exhaustion_requires_manual_intervention(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"; _incident(path); self_healing.set_global_enabled(True, path=str(path))
    self_healing.store.set_self_healing_state("garmin-transient-retry", {"attempts": ["2026-09-24T00:00:00Z", "2026-09-24T00:01:00Z"]}, path=str(path))
    monkeypatch.setattr(self_healing.jobs, "action", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not act")))

    result = self_healing.run_once(path=str(path), now=datetime(2026, 9, 24, 0, 2, tzinfo=timezone.utc))

    assert any(item.get("reason") == "budget_exhausted" for item in result["skipped"] if isinstance(item, dict))
    assert self_healing.store.self_healing_state("garmin-transient-retry", path=str(path))["manual_intervention_required"] is True


def test_healthy_service_never_restarts(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"; self_healing.set_global_enabled(True, path=str(path))
    _incident(path, component="liva", error_class="", correlation="systemd:liva.service")
    monkeypatch.setattr(self_healing, "_service_is_failed", lambda _: False)
    monkeypatch.setattr(self_healing.jobs, "action", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not restart")))

    assert self_healing.run_once(path=str(path))["actions"] == []
