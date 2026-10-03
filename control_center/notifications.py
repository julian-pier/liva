"""Small, fixed provider abstraction for Control Center technical alerts."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from integrations.ntfy_client import DEFAULT_NOTIFY_BIN, send_ntfy_notification
from integrations.telegram_hub import _api_call, resolve_domain_config, send_domain_message

from . import store

POLICIES = {"critical-incident": {"severity": "critical", "cooldown_seconds": 4 * 60 * 60, "enabled": True}}


def _active_provider() -> str:
    state = store.get_state("technical-config:notification-provider")
    if isinstance(state, dict) and state.get("effective") in {"telegram", "ntfy"}: return str(state["effective"])
    return "telegram" if provider_status("telegram")["configured"] else "ntfy"


def _ntfy_healthcheck() -> tuple[bool, str | None]:
    """Probe the configured ntfy server without publishing a notification."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False, "health_check_disabled_in_test"
    configured_url = str(os.environ.get("LIVA_NOTIFY_URL") or "http://localhost:8081/liva-alerts")
    parts = urlsplit(configured_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False, "health_check_url_invalid"
    health_url = urlunsplit((parts.scheme, parts.netloc, "/v1/health", "", ""))
    try:
        with urlopen(Request(health_url, method="GET"), timeout=3) as response:
            return response.status == 200, None if response.status == 200 else "health_check_failed"
    except Exception:
        return False, "health_check_failed"


def provider_status(provider: str, *, verify: bool = False) -> dict[str, Any]:
    if provider == "telegram":
        config = resolve_domain_config("system")
        configured = bool(config.get("token") and config.get("chat_id"))
        healthy = configured
        code = None
        if configured and verify:
            try:
                healthy = bool(_api_call(config["token"], "getMe", {}).get("ok"))
            except Exception:
                healthy = False; code = "health_check_failed"
        return {"provider": "telegram", "configured": configured, "healthy": healthy, "status": "healthy" if healthy else "missing" if not configured else "unhealthy", "capabilities": ["send", "health"], "diagnostics": {"source": "existing_system_telegram_config", "error_code": code}}
    if provider == "ntfy":
        binary = Path(str(os.environ.get("LIVA_NOTIFY_BIN") or DEFAULT_NOTIFY_BIN))
        configured = binary.is_file() and os.access(binary, os.X_OK)
        healthy, code = (configured, None) if not verify else _ntfy_healthcheck()
        healthy = configured and healthy
        return {"provider": "ntfy", "configured": configured, "healthy": healthy, "status": "healthy" if healthy else "not_configured" if not configured else "unhealthy", "capabilities": ["send", "health"], "diagnostics": {"source": "existing_liva_notify_wrapper", "error_code": code}}
    return {"provider": provider, "configured": False, "healthy": False, "status": "unknown", "capabilities": [], "diagnostics": {}}


def status() -> dict[str, Any]:
    active = _active_provider()
    providers = [provider_status(name) for name in ("telegram", "ntfy")]
    history = store.list_notification_deliveries(limit=50)
    current = next(item for item in providers if item["provider"] == active)
    latest_success = next((item.get("sent_at") for item in history if item["provider"] == active and item["result"] == "sent"), None)
    latest_failure = next((item.get("requested_at") for item in history if item["provider"] == active and item["result"] == "failed"), None)
    return {"active_provider": active, "provider": current, "providers": providers, "last_success_at": latest_success, "last_failure_at": latest_failure}


def _send(provider: str, text: str, *, test: bool = False) -> dict[str, Any]:
    # Test discovery must be fail-closed even if a fixture accidentally carries
    # real credentials or a real notification wrapper path.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("LIVA_TEST_MODE") == "1":
        return {"ok": False, "error": "test_transport_blocked", "diagnostics": {"test": test, "skip_reason": "test_event"}}
    if provider == "telegram":
        outcome = send_domain_message("system", text, respect_push_settings=False)
        return {"ok": bool(outcome.get("ok")), "error": outcome.get("error"), "diagnostics": {"test": test, "transport": "telegram"}}
    if provider == "ntfy":
        outcome = send_ntfy_notification(text, title="LIVA Control Center", priority=4, tags="warning")
        return {"ok": bool(outcome.get("ok") and outcome.get("sent")), "error": outcome.get("error"), "diagnostics": {"test": test, "transport": "ntfy"}}
    return {"ok": False, "error": "unknown_provider", "diagnostics": {}}


def deliver(*, provider: str, dedupe_key: str, text: str, incident_id: str | None = None, event_id: str | None = None, test: bool = False) -> dict[str, Any]:
    active = _active_provider()
    skip_reason = "inactive_provider" if provider != active else "test_event" if test and os.environ.get("LIVA_TEST_MODE") == "1" else None
    record, reserved = store.record_notification_delivery({"dedupe_key": dedupe_key, "incident_id": incident_id, "event_id": event_id, "provider": provider, "result": "skipped" if skip_reason else "requested", "diagnostics": {"test": test, "skip_reason": skip_reason}})
    if not reserved: return {"deduplicated": True, "delivery": record}
    if skip_reason:
        return {"deduplicated": False, "skipped": True, "skip_reason": skip_reason, "delivery": record}
    outcome = _send(provider, text, test=test)
    record.update({"sent_at": store.utc_now() if outcome["ok"] else None, "result": "sent" if outcome["ok"] else "failed", "error_code": outcome.get("error"), "diagnostics": outcome.get("diagnostics") or {}})
    store.update_notification_delivery(record)
    if not outcome["ok"]:
        # Visible operational fact, deliberately incident-ineligible to prevent recursion.
        store.record_event({"idempotency_key": f"notification-failure:{record['notification_id']}", "component_id": "notifications", "source": "notification-adapter", "event_type": "delivery_failed", "severity": "warning", "message": "Technical notification delivery failed.", "error_class": outcome.get("error"), "correlation_key": f"notification:{provider}", "incident_eligible": False, "details": {"provider": provider, "notification_id": record["notification_id"]}})
    return {"deduplicated": False, "delivery": record}


def dispatch_incident(event: dict[str, Any], result: dict[str, Any]) -> None:
    """Notify only a newly observed critical incident, with stable per-episode dedupe."""
    policy = POLICIES["critical-incident"]
    if not policy["enabled"] or event.get("notification_eligible") is False or event.get("severity") != policy["severity"] or not result.get("incident_id") or event.get("is_recovery"):
        return
    incident_id = str(result["incident_id"])
    provider = _active_provider()
    dedupe = f"critical-incident:{incident_id}:{event.get('severity')}"
    deliver(provider=provider, dedupe_key=dedupe, incident_id=incident_id, event_id=result.get("event_id"), text=f"LIVA Control Center: kritischer technischer Vorfall in {event.get('component_id')}. Details im Control Center.")


def send_test() -> dict[str, Any]:
    provider = _active_provider()
    status_value = provider_status(provider, verify=(provider == "telegram"))
    if not (status_value["configured"] and status_value["healthy"]): raise ValueError("active provider is not healthy")
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return deliver(provider=provider, dedupe_key=f"control-center-test:{provider}:{day}", text="LIVA Control Center: technische Testbenachrichtigung.", test=True)
