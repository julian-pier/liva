"""Allowlisted technical Job Center registry and safe systemd actions."""

from __future__ import annotations

import subprocess
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from . import store
from .adapters import endurance_snapshot
from .models import ManagedJobSnapshot, utc_now_iso
from .redaction import redact


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    name: str
    component: str
    job_type: str
    source: str
    service: str | None = None
    timer: str | None = None
    run: bool = False
    pause: bool = False
    retry: bool = False
    retry_reason: str | None = None


def _job(job_id: str, name: str, component: str, service: str | None, timer: str | None, *, job_type: str = "systemd_timer", run: bool = True, pause: bool = True, retry: bool = False, retry_reason: str | None = None) -> JobDefinition:
    return JobDefinition(job_id, name, component, job_type, "systemd", service, timer, run, pause, retry, retry_reason)


REGISTRY: dict[str, JobDefinition] = {
    "full-recovery": _job("full-recovery", "Full Recovery", "full-recovery", "liva-full-recovery.service", "liva-full-recovery.timer"),
    "full-recovery-catchup": _job("full-recovery-catchup", "Full Recovery Catch-up", "full-recovery", "liva-full-recovery-catchup.service", "liva-full-recovery-catchup.timer"),
    "daily-sqlite-backup": _job("daily-sqlite-backup", "Daily SQLite Backup", "daily-sqlite-backup", "liva-daily-backup.service", "liva-daily-backup.timer"),
    "legacy-db-tar": _job("legacy-db-tar", "Legacy DB TAR", "legacy-db-tar", "liva-db-backup.service", "liva-db-backup.timer"),
    "endurance-intervals": _job("endurance-intervals", "Endurance → Intervals", "endurance-intervals", "liva-endurance-intervals-sync.service", "liva-endurance-intervals-sync.timer", job_type="persistent_queue", retry_reason="missing_execution_pace is a domain-blocked retry"),
    "garmin-sync": _job("garmin-sync", "Garmin Sync", "garmin-sync", "liva-garmin-sync.service", "liva-garmin-sync.timer"),
    "polar-sync": _job("polar-sync", "Polar Sync", "polar-sync", "liva-polar-smart-sync.service", "liva-polar-smart-sync.timer"),
    "hrv-dropbox-poll": _job("hrv-dropbox-poll", "HRV Dropbox Poll", "hrv-dropbox-poll", "liva-polar-smart-sync.service", "liva-polar-smart-sync.timer", run=False, pause=False),
    "telegram-inbox": _job("telegram-inbox", "Telegram Inbox", "telegram-inbox", "liva-telegram-inbox.service", "liva-telegram-inbox.timer"),
    "system-telegram-watch": _job("system-telegram-watch", "System Telegram Watch", "system-telegram-watch", "liva-system-telegram-watch.service", "liva-system-telegram-watch.timer"),
    "liva-watchdog": _job("liva-watchdog", "LIVA Watchdog", "liva-watchdog", "liva-watchdog.service", "liva-watchdog.timer"),
    "availability-watchdog": _job("availability-watchdog", "Availability Watchdog", "availability-watchdog", "liva-availability-watchdog.service", "liva-availability-watchdog.timer"),
    "webuntis": _job("webuntis", "WebUntis", "webuntis", "liva-webuntis-update-watch.service", "liva-webuntis-update-watch.timer"),
    "training-calendar": _job("training-calendar", "Training Calendar", "training-calendar", "liva-training-calendar-sync.service", "liva-training-calendar-sync.timer"),
    "control-center-collector": _job("control-center-collector", "Control Center Collector", "control-center-collector", "liva-control-center-collector.service", "liva-control-center-collector.timer"),
}

# Not exposed in the Jobs UI or public mutation API.  They exist solely for
# tightly scoped policy execution in ``self_healing``.
_INTERNAL_RESTARTS: dict[str, JobDefinition] = {
    "core-liva": _job("core-liva", "LIVA service", "liva", "liva.service", None, job_type="daemon", run=False, pause=False),
    "core-liva-mcp": _job("core-liva-mcp", "LIVA MCP remote service", "liva-mcp-remote", "liva-mcp-remote.service", None, job_type="daemon", run=False, pause=False),
}

_SELF_HEALING_POLICIES = {"garmin-sync": "garmin-transient-retry"}


def _execute(job_id: str, action_name: str) -> None:
    """Invoke the installed root-owned allowlist broker, never systemctl directly."""
    subprocess.run(
        ["sudo", "-n", "/usr/local/sbin/liva-job-action", job_id, action_name],
        timeout=180 if action_name == "run" else 20,
        check=True,
        capture_output=True,
        text=True,
    )


def _show(*units: str) -> dict[str, dict[str, str]]:
    if not units:
        return {}
    result = subprocess.run(["systemctl", "show", *units, "--no-pager", "-p", "Id", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp", "-p", "UnitFileState", "-p", "NextElapseUSecRealtime"], capture_output=True, text=True, timeout=3, check=False)
    values: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line:
            if current.get("Id"):
                values[current["Id"]] = current
            current = {}
        elif "=" in line:
            key, value = line.split("=", 1); current[key] = value
    return values


def _restart_target_is_active(job: JobDefinition) -> bool:
    """Confirm a service is back after its restart handoff."""
    if not job.service:
        return False
    return _show(job.service).get(job.service, {}).get("ActiveState") == "active"


def _reconcile_core_restart_audits() -> None:
    """Confirm restarts once the new service process is available to inspect."""
    for audit in store.list_job_actions(limit=200):
        job = _INTERNAL_RESTARTS.get(str(audit.get("job_id") or ""))
        expected_signal = audit.get("error_code") == "CalledProcessError" and "Signals.SIGTERM" in str(audit.get("message") or "")
        if not job or audit.get("action") != "restart" or (audit.get("result") != "dispatched" and not expected_signal):
            continue
        if not _restart_target_is_active(job):
            continue
        confirmed = {**audit, "result": "success", "error_code": None, "message": "Restart confirmed after systemd handoff.", "finished_at": utc_now_iso()}
        store.update_job_action(confirmed)


def _actions(job: JobDefinition, active: bool) -> list[dict[str, Any]]:
    items = []
    if job.run:
        items.append({"action": "run", "risk": "high", "requires_master": True, "confirmation": True, "available": not active})
    if job.pause and job.timer:
        items.append({"action": "pause", "risk": "high", "requires_master": True, "confirmation": True, "available": True})
        items.append({"action": "resume", "risk": "high", "requires_master": True, "confirmation": True, "available": True})
    if job.retry:
        items.append({"action": "retry", "risk": "high", "requires_master": True, "confirmation": True, "available": True})
    return items


def snapshots() -> list[dict[str, Any]]:
    _reconcile_core_restart_audits()
    units = tuple(unit for job in REGISTRY.values() for unit in (job.service, job.timer) if unit)
    live = _show(*units)
    audits: dict[str, dict[str, Any]] = {}
    for item in store.list_job_actions(limit=200):
        audits.setdefault(item["job_id"], item)
    output = []
    for job in REGISTRY.values():
        service = live.get(job.service or "", {})
        timer = live.get(job.timer or "", {})
        active = service.get("ActiveState") == "active"
        enabled = timer.get("UnitFileState") == "enabled" if job.timer else None
        failed = service.get("Result") not in {None, "", "success"}
        status = "failing" if failed else "degraded" if active else "healthy" if enabled is not False else "inactive"
        domain: ManagedJobSnapshot | None = None
        if job.job_id == "endurance-intervals":
            # Queue status is authoritative for this persistent job; systemd only
            # tells us whether its worker is currently executing.
            _, domain = endurance_snapshot()
            status = domain.status
        audit = audits.get(job.job_id, {})
        policy_id = _SELF_HEALING_POLICIES.get(job.job_id)
        policy_state = store.self_healing_state(policy_id) if policy_id else None
        snapshot = ManagedJobSnapshot(job.job_id, job.component, job.name, job.timer or "manual", status, domain.source if domain else job.source, utc_now_iso(), last_run_at=(domain.last_run_at if domain else None) or service.get("ExecMainStartTimestamp") or None, last_success_at=(domain.last_success_at if domain else None) or (service.get("ExecMainExitTimestamp") if service.get("Result") == "success" else None), pending_count=domain.pending_count if domain else 0, failed_count=domain.failed_count if domain else (1 if failed else 0), retry_count=domain.retry_count if domain else 0, job_type=job.job_type, enabled=enabled, active=active, next_run_at=timer.get("NextElapseUSecRealtime") or None, last_failure_at=domain.last_failure_at if domain else (service.get("ExecMainExitTimestamp") if failed else None), capabilities={"run": job.run, "pause": bool(job.pause and job.timer), "resume": bool(job.pause and job.timer), "retry": job.retry, "self_healing": bool(policy_id)}, details={"service": job.service, "timer": job.timer, "retry_reason": job.retry_reason, "self_healing_policy": policy_id, "self_healing_state": policy_state, **(domain.details if domain else {})}, last_manual_action_at=audit.get("requested_at"), last_automatic_action_at=policy_state.get("last_attempt_at") if policy_state else None, actions=())
        value = snapshot.to_dict(); value["actions"] = _actions(job, active); output.append(value)
    return output


def snapshot(job_id: str) -> dict[str, Any] | None:
    return next((item for item in snapshots() if item["job_id"] == job_id), None)


def history(job_id: str, limit: int) -> list[dict[str, Any]]:
    job = REGISTRY.get(job_id)
    if not job:
        raise KeyError(job_id)
    bounded = max(1, min(limit, 100))
    entries: list[dict[str, Any]] = [
        {"kind": "action_audit", **item} for item in store.list_job_actions(job_id=job_id, limit=bounded)
    ]
    if job.service:
        result = subprocess.run(
            ["journalctl", "--unit", job.service, "--no-pager", "--output=short-iso", f"--lines={bounded}"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        if result.returncode == 0:
            entries.extend({"kind": "systemd_journal", "message": redact(line)} for line in result.stdout.splitlines() if line.strip())
    return entries[: bounded * 2]


def action(job_id: str, action_name: str, *, request_id: str, confirmed: bool, actor_role: str, actor_type: str = "operator", policy_id: str | None = None, trigger_event_id: str | None = None, incident_id: str | None = None, attempt: int | None = None, budget: dict[str, Any] | None = None) -> dict[str, Any]:
    # Core service restarts stay outside the general Jobs table.  The explicit
    # master emergency action uses this same deduplicated, audited broker.
    trusted_restart_actors = {"self_healing", "emergency_admin", "control_center_master"}
    job = REGISTRY.get(job_id) or (_INTERNAL_RESTARTS.get(job_id) if actor_type in trusted_restart_actors else None)
    if not job or action_name not in {"run", "retry", "pause", "resume", "restart"}:
        raise KeyError(job_id)
    if action_name == "restart" and actor_type not in trusted_restart_actors:
        raise KeyError(job_id)
    if not request_id or len(request_id) > 120 or not confirmed:
        raise ValueError("confirmed request_id is required")
    dedupe_key = f"job-action:{job_id}:{action_name}:{request_id}"
    requested = utc_now_iso()
    audit: dict[str, Any] = {"action_id": uuid.uuid4().hex, "dedupe_key": dedupe_key, "job_id": job_id, "action": action_name, "requested_at": requested, "started_at": requested, "actor_role": actor_role, "actor_type": actor_type, "policy_id": policy_id, "trigger_event_id": trigger_event_id, "incident_id": incident_id, "attempt": attempt, "budget": budget or {}, "result": "requested", "correlation_key": f"job-action:{job_id}:{action_name}"}
    audit, reserved = store.reserve_job_action(audit)
    if not reserved:
        return {"deduplicated": True, "audit": audit, "job": snapshot(job_id)}
    current = snapshot(job_id) or {}
    restart_dispatched = False
    try:
        if action_name == "run":
            if not job.run or not job.service:
                raise RuntimeError("run is not allowlisted")
            if current.get("active"):
                raise RuntimeError("job is already running")
            _execute(job_id, action_name)
        elif action_name == "restart":
            try:
                _execute(job_id, action_name)
            except subprocess.CalledProcessError as exc:
                # Restarting LIVA can kill the broker in the old service
                # cgroup before the replacement process is inspectable. Record
                # the handoff and let the new service confirm it on startup.
                if exc.returncode != -signal.SIGTERM:
                    raise
                restart_dispatched = True
                audit["message"] = "Restart handed to systemd; confirmation follows after service startup."
        elif action_name in {"pause", "resume"}:
            if not job.pause or not job.timer:
                raise RuntimeError("timer control is not allowlisted")
            _execute(job_id, action_name)
        else:
            raise RuntimeError(job.retry_reason or "retry is not allowlisted")
        audit["result"] = "dispatched" if restart_dispatched else "success"
    except Exception as exc:
        audit.update({"result": "failed" if action_name != "retry" else "rejected", "error_code": type(exc).__name__, "message": str(exc)})
    audit["finished_at"] = utc_now_iso(); audit["resulting_state"] = snapshot(job_id) or {"service": job.service}
    saved = store.update_job_action(audit)
    store.record_event({
        "idempotency_key": f"{dedupe_key}:{saved['result']}", "component_id": job.component,
        "source": "self-healing" if actor_type == "self_healing" else "job-center", "event_type": f"job_{action_name}",
        "severity": "info" if saved["result"] in {"success", "dispatched"} else "warning",
        "message": f"Job action {action_name}: {saved['result']}",
        "correlation_key": audit["correlation_key"], "details": {"job_id": job_id, "action_id": audit["action_id"], "result": saved["result"], "policy_id": policy_id, "attempt": attempt},
        "incident_eligible": False,
    })
    return {"deduplicated": False, "audit": saved, "job": snapshot(job_id)}
