"""Bounded, policy-only technical recovery executor.

This module never invents actions: it delegates to the audited 007 job-action
engine and deliberately does nothing for anything it cannot classify exactly.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import jobs, store
from .health import build_cockpit

GLOBAL_STATE_KEY = "self_healing:global"
MAX_ACTIONS_PER_RUN = 2


@dataclass(frozen=True)
class Policy:
    policy_id: str
    job_id: str
    action: str
    component: str
    transient_error_classes: frozenset[str] = frozenset()
    service: str | None = None
    max_attempts: int = 2
    window_seconds: int = 3600
    base_backoff_seconds: int = 300
    max_backoff_seconds: int = 900
    cooldown_seconds: int = 300
    requires_incident: bool = True


POLICIES: dict[str, Policy] = {
    "garmin-transient-retry": Policy("garmin-transient-retry", "garmin-sync", "run", "garmin-sync", frozenset({"network_timeout", "provider_unavailable", "http_502", "http_503", "http_504"})),
    "liva-service-restart": Policy("liva-service-restart", "core-liva", "restart", "liva", service="liva.service"),
    "liva-mcp-service-restart": Policy("liva-mcp-service-restart", "core-liva-mcp", "restart", "liva-mcp-remote", service="liva-mcp-remote.service"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def global_state(*, path: str | None = None) -> dict[str, Any]:
    value = store.get_state(GLOBAL_STATE_KEY, path=path)
    return {"enabled": False, "updated_at": None, **(value if isinstance(value, dict) else {})}


def set_global_enabled(enabled: bool, *, path: str | None = None) -> dict[str, Any]:
    value = {"enabled": bool(enabled), "updated_at": store.utc_now()}
    store.set_state(GLOBAL_STATE_KEY, value, path=path)
    return value


def policy_views(*, path: str | None = None) -> list[dict[str, Any]]:
    return [{**asdict(policy), "transient_error_classes": sorted(policy.transient_error_classes), "state": store.self_healing_state(policy.policy_id, path=path)} for policy in POLICIES.values()]


def _latest_error_class(incident: dict[str, Any]) -> str | None:
    for event in reversed(incident.get("events") or []):
        value = event.get("error_class")
        if value:
            return str(value)
    return None


def classify(policy: Policy, incident: dict[str, Any]) -> str:
    """Return one of the fixed classifications; never infer from message text."""
    error_class = _latest_error_class(incident)
    if error_class in {"missing_execution_pace", "auth_error", "credential_error", "invalid_config", "inventory_drift", "hash_mismatch", "sqlite_corrupt", "usb_missing", "restore_failed"}:
        return "permanent" if error_class == "missing_execution_pace" else "unsafe"
    if policy.transient_error_classes and error_class in policy.transient_error_classes:
        return "transient"
    if policy.service and incident.get("correlation_key") == f"systemd:{policy.service}":
        return "transient" if _service_is_failed(policy.service) else "unknown"
    return "unknown"


def _service_is_failed(service: str) -> bool:
    result = subprocess.run(["systemctl", "show", service, "--no-pager", "-p", "ActiveState", "-p", "Result"], capture_output=True, text=True, timeout=3, check=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    systemd_failed = result.returncode != 0 or values.get("ActiveState") == "failed" or values.get("Result") not in {None, "", "success"}
    try:
        health_failed = any(check.get("component_id") == ("liva-mcp-remote" if service == "liva-mcp-remote.service" else "liva") and check.get("status") == "failing" for check in build_cockpit().get("checks", []))
    except Exception:
        health_failed = False
    return systemd_failed and health_failed


def _eligible_state(policy: Policy, now: datetime, *, path: str | None) -> tuple[dict[str, Any], list[str], str | None]:
    state = store.self_healing_state(policy.policy_id, path=path)
    attempts = [stamp for stamp in state.get("attempts", []) if (parsed := _parse(stamp)) and parsed >= now - timedelta(seconds=policy.window_seconds)]
    state["attempts"] = attempts
    if not state.get("enabled", True): return state, attempts, "policy_disabled"
    if state.get("manual_intervention_required"): return state, attempts, "manual_intervention_required"
    if len(attempts) >= policy.max_attempts: return state, attempts, "budget_exhausted"
    next_allowed = _parse(state.get("next_allowed_at"))
    if next_allowed and now < next_allowed: return state, attempts, "backoff"
    return state, attempts, None


def _record_recovery(policy: Policy, incident: dict[str, Any], *, path: str | None) -> None:
    store.record_event({
        "idempotency_key": f"self-healing:recovered:{policy.policy_id}:{incident['incident_id']}",
        "component_id": policy.component, "source": "self-healing", "event_type": "automatic_recovery",
        "severity": "info", "message": f"Self-healing recovered {policy.policy_id}.",
        "correlation_key": incident["correlation_key"], "is_recovery": True,
        "details": {"policy_id": policy.policy_id, "incident_id": incident["incident_id"]},
    }, path=path)


def run_once(*, path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    state = global_state(path=path)
    summary: dict[str, Any] = {"ok": True, "enabled": bool(state["enabled"]), "actions": [], "skipped": []}
    deployment = store.get_state("release:in_progress", path=path)
    if isinstance(deployment, dict) and deployment.get("deployment_id"):
        summary["skipped"].append("deployment_in_progress")
        return summary
    emergency = store.get_state("emergency:operation", path=path)
    if isinstance(emergency, dict) and emergency.get("active"):
        summary["skipped"].append("emergency_operation_in_progress")
        return summary
    incidents = store.list_incidents(status="open", limit=100, path=path)
    if not state["enabled"]:
        summary["skipped"].append("global_disabled")
        return summary
    for incident_row in incidents:
        if len(summary["actions"]) >= MAX_ACTIONS_PER_RUN:
            summary["skipped"].append("run_budget")
            break
        incident = store.get_incident(incident_row["incident_id"], path=path) or incident_row
        for policy in POLICIES.values():
            if policy.component != incident.get("component_id"):
                continue
            classification = classify(policy, incident)
            if classification != "transient":
                summary["skipped"].append({"policy_id": policy.policy_id, "incident_id": incident["incident_id"], "reason": classification})
                continue
            policy_state, attempts, reason = _eligible_state(policy, current, path=path)
            if reason:
                if reason == "budget_exhausted":
                    store.set_self_healing_state(policy.policy_id, {**policy_state, "manual_intervention_required": True, "last_result": reason}, path=path)
                summary["skipped"].append({"policy_id": policy.policy_id, "incident_id": incident["incident_id"], "reason": reason})
                continue
            attempt = len(attempts) + 1
            request_id = f"auto:{policy.policy_id}:{incident['incident_id']}:{attempt}"
            result = jobs.action(policy.job_id, policy.action, request_id=request_id, confirmed=True, actor_role="system", actor_type="self_healing", policy_id=policy.policy_id, trigger_event_id=(incident.get("events") or [{}])[-1].get("event_id"), incident_id=incident["incident_id"], attempt=attempt, budget={"attempt": attempt, "max_attempts": policy.max_attempts, "window_seconds": policy.window_seconds})
            success = result.get("audit", {}).get("result") == "success"
            new_attempts = attempts + [_iso(current)]
            next_allowed = None if success else _iso(current + timedelta(seconds=min(policy.max_backoff_seconds, policy.base_backoff_seconds * (2 ** (attempt - 1)))))
            saved_state = store.set_self_healing_state(policy.policy_id, {**policy_state, "attempts": new_attempts, "last_attempt_at": _iso(current), "last_result": "success" if success else "failed", "next_allowed_at": next_allowed, "manual_intervention_required": len(new_attempts) >= policy.max_attempts and not success}, path=path)
            if success:
                _record_recovery(policy, incident, path=path)
            summary["actions"].append({"policy_id": policy.policy_id, "incident_id": incident["incident_id"], "result": result.get("audit", {}).get("result"), "attempt": attempt, "state": saved_state})
    return summary


if __name__ == "__main__":
    print(json.dumps(run_once(), ensure_ascii=True))
