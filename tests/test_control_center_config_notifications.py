from __future__ import annotations

from pathlib import Path

import pytest

from control_center import config, notifications, store


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.sqlite3")


def test_config_registry_is_explicit_and_secrets_never_expose_values(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "provider_status", lambda provider, **_: {"provider": provider, "configured": provider == "telegram", "healthy": True, "status": "healthy", "capabilities": [], "diagnostics": {}})
    values = config.views()
    secret = next(item for item in values if item["config_id"] == "telegram-system-secret")

    assert config.view("nope") is None
    assert secret["secret"] is True
    assert secret["effective"] == "configured"
    assert "token" not in str(secret).lower()


def test_config_validation_read_only_and_effective_update(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "provider_status", lambda provider, **_: {"provider": provider, "configured": provider == "telegram", "healthy": provider == "telegram", "status": "healthy", "capabilities": [], "diagnostics": {}})
    with pytest.raises(KeyError): config.mutate("unknown", True, actor_role="master")
    with pytest.raises(PermissionError): config.mutate("recovery-primary-media", "x", actor_role="master")
    with pytest.raises(ValueError): config.mutate("notification-provider", "invalid", actor_role="master")

    value = config.mutate("notification-provider", "telegram", actor_role="master")
    assert value["effective"] == "telegram" and value["pending"] is None


def test_provider_switch_blocks_unhealthy_target_and_never_has_two_active(monkeypatch):
    monkeypatch.setattr(notifications, "provider_status", lambda provider, **_: {"provider": provider, "configured": provider == "telegram", "healthy": provider == "telegram", "status": "healthy", "capabilities": [], "diagnostics": {}})
    with pytest.raises(ValueError): config.mutate("notification-provider", "ntfy", actor_role="master")
    assert config.mutate("notification-provider", "telegram", actor_role="master")["effective"] == "telegram"


def test_provider_switch_allows_reachable_ntfy_without_prior_delivery(monkeypatch):
    """Ntfy must be selectable after a non-sending health check, before its first delivery."""
    monkeypatch.setattr(notifications, "DEFAULT_NOTIFY_BIN", Path("/bin/true"))
    monkeypatch.setattr(notifications, "_ntfy_healthcheck", lambda: (True, None), raising=False)

    value = config.mutate("notification-provider", "ntfy", actor_role="master")

    assert value["effective"] == "ntfy"


def test_notification_delivery_dedupes_and_failure_does_not_create_incident(monkeypatch, tmp_path):
    path = str(tmp_path / "control.sqlite3")
    monkeypatch.setattr(notifications, "_send", lambda *_args, **_kwargs: {"ok": False, "error": "timeout", "diagnostics": {}})
    first = notifications.deliver(provider="telegram", dedupe_key="same", text="safe", test=True)
    second = notifications.deliver(provider="telegram", dedupe_key="same", text="safe", test=True)

    assert first["delivery"]["result"] == "failed"
    assert second["deduplicated"] is True
    assert store.list_incidents(path=path) == []


def test_inactive_provider_is_audited_and_never_sent(monkeypatch):
    monkeypatch.setattr(notifications, "_active_provider", lambda: "telegram")
    sent = []
    monkeypatch.setattr(notifications, "_send", lambda *args, **kwargs: sent.append(args) or {"ok": True, "diagnostics": {}})
    result = notifications.deliver(provider="ntfy", dedupe_key="inactive", text="must not send")
    assert result["skipped"] is True and result["skip_reason"] == "inactive_provider"
    assert sent == []


def test_synthetic_event_never_dispatches(monkeypatch):
    seen = []
    monkeypatch.setattr(notifications, "deliver", lambda **kwargs: seen.append(kwargs))
    notifications.dispatch_incident({"severity": "critical", "notification_eligible": False}, {"incident_id": "i", "event_id": "e"})
    assert seen == []


def test_critical_incident_dispatches_but_normal_job_does_not(monkeypatch):
    seen = []
    monkeypatch.setattr(notifications, "deliver", lambda **kwargs: seen.append(kwargs) or {})
    notifications.dispatch_incident({"severity": "info", "component_id": "job"}, {"incident_id": "i", "event_id": "e"})
    notifications.dispatch_incident({"severity": "critical", "component_id": "job"}, {"incident_id": "i", "event_id": "e"})
    assert len(seen) == 1


def test_test_notification_is_rate_limited(monkeypatch):
    monkeypatch.setattr(notifications, "_active_provider", lambda: "telegram")
    monkeypatch.setattr(notifications, "provider_status", lambda *_args, **_kwargs: {"configured": True, "healthy": True})
    calls = []
    monkeypatch.setattr(notifications, "deliver", lambda **kwargs: calls.append(kwargs) or {"delivery": {"result": "sent"}})
    notifications.send_test()
    assert calls[0]["dedupe_key"].startswith("control-center-test:telegram:")
