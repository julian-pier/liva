from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any

from database import connections

from .health import build_cockpit
from .redaction import redact
from . import store


SYSTEMD_UNITS = {
    "liva.service": "liva",
    "liva-mcp-remote.service": "liva-mcp-remote",
    "liva-control-center-collector.service": "control-center-collector",
}


def _iso(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return store.utc_now()


def _changed(key: str, value: Any, *, db_path: str | None) -> tuple[bool, Any]:
    previous = store.get_state(key, path=db_path)
    if previous == value:
        return False, previous
    store.set_state(key, value, path=db_path)
    return True, previous


def _event(*, component: str, source: str, event_type: str, severity: str, correlation: str, message: str, error_class: str | None = None, details: dict[str, Any] | None = None, recovery: bool = False, affected: int = 0, occurrence: str) -> dict[str, Any]:
    observed = store.utc_now()
    return {
        "idempotency_key": f"{source}:{occurrence}", "component_id": component, "source": source,
        "event_type": event_type, "severity": severity, "occurred_at": observed, "observed_at": observed,
        "message": message, "error_class": error_class, "correlation_key": correlation,
        "details": redact(details or {}), "is_recovery": recovery, "affected_object_count": affected,
    }


def collect_health(*, db_path: str | None = None) -> list[dict[str, Any]]:
    payload = build_cockpit()
    output: list[dict[str, Any]] = []
    eligible = {
        "backup.daily.execution": ("daily-sqlite-backup", "backup.execution"),
        "backup.freshness": ("daily-sqlite-backup", "backup.freshness"),
        "backup.usb.target": ("daily-sqlite-backup", "backup.usb.required"),
    }
    for check in payload.get("checks", []):
        check_id = str(check.get("check_id") or "")
        if check_id not in eligible:
            continue
        component, event_type = eligible[check_id]
        status = str(check.get("status") or "unknown")
        is_eligible = status == "failing"
        state_key = f"health:{check_id}"
        changed, previous = _changed(state_key, {"status": status, "eligible": is_eligible}, db_path=db_path)
        if not changed:
            continue
        correlation = f"{component}:{event_type}"
        if is_eligible:
            output.append(_event(component=component, source="health", event_type=event_type, severity="critical", correlation=correlation, message=str(check.get("reason") or check_id), details=check.get("details") or {}, occurrence=f"{status}:{check.get('checked_at') or store.utc_now()}"))
        elif previous and previous.get("eligible"):
            output.append(_event(component=component, source="health", event_type="recovery", severity="info", correlation=correlation, message=f"Recovered: {check_id}", details={"current_status": status}, recovery=True, occurrence=f"recovered:{check.get('checked_at') or store.utc_now()}"))
    return output


def collect_full_recovery(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Persist a read-only preflight observation without erasing backup history."""
    from recovery.full_recovery import preflight

    result = preflight()
    error = result.get("error_code")
    previous_state = store.get_full_recovery_state(path=db_path) or {}
    observed = result.get("device") or {}
    expected = {key: observed.get(key) for key in ("uuid", "label", "filesystem")}
    state = {
        "recipient_fingerprint": result.get("recipient_fingerprint"),
        "expected_device": expected,
        "observed_device": observed,
    }
    if error:
        state.update({
            "phase": "setup" if error == "NOT_INITIALIZED" else "failed",
            "status": "not_initialized" if error == "NOT_INITIALIZED" else "failed",
            "error_code": error,
            "message": result.get("message"),
        })
    else:
        state.update({
            "phase": previous_state.get("phase") or "ready",
            "status": previous_state.get("status") or "ready",
            "error_code": None,
            "message": None,
        })
    store.set_full_recovery_state(state, path=db_path)
    if error in {None, "NOT_INITIALIZED"}:
        return []
    changed, previous = _changed("full-recovery:preflight", {"error": error, "message": result.get("message")}, db_path=db_path)
    if not changed:
        return []
    if previous and not previous.get("error"):
        return [_event(component="full-recovery", source="recovery_preflight", event_type="recovery", severity="info", correlation="full-recovery:preflight", message="Full Recovery preflight recovered.", recovery=True, occurrence=f"recovered:{store.utc_now()}")]
    return [_event(component="full-recovery", source="recovery_preflight", event_type="preflight_failed", severity="critical", correlation=f"full-recovery:{error}", message="Full Recovery preflight failed.", error_class=str(error), details={"message": result.get("message"), "device": result.get("device"), "inventory_drift": result.get("inventory_drift")}, occurrence=f"{error}:{result.get('message')}")]


def collect_endurance(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Group active Endurance sync errors by normalized error class, read-only."""
    try:
        conn = sqlite3.connect(f"file:{connections.PLANS_DB}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """SELECT CASE WHEN instr(COALESCE(j.last_error,''), ':') > 0 THEN substr(j.last_error,1,instr(j.last_error,':')-1) ELSE 'sync_error' END AS error_class,
                       COUNT(DISTINCT s.id) AS affected, COUNT(j.id) AS historical_rows, MAX(j.updated_at) AS latest_at
                 FROM endurance_sessions s LEFT JOIN endurance_sync_jobs j ON j.session_id=s.id AND j.status='failed'
                 WHERE s.sync_state='sync_error' AND s.scheduled_date>=date('now')
                 GROUP BY error_class"""
        ).fetchall()
        conn.close()
    except Exception as exc:
        return [_event(component="endurance-intervals", source="endurance_queue", event_type="source_error", severity="warning", correlation="endurance-intervals:collector.source", message="Endurance queue could not be read.", error_class=type(exc).__name__, details={"error": str(exc)}, occurrence=f"error:{type(exc).__name__}:{datetime.now(timezone.utc).strftime('%Y%m%d%H')}")]
    current = {str(row["error_class"]): {"affected": int(row["affected"] or 0), "historical_rows": int(row["historical_rows"] or 0), "latest_at": row["latest_at"]} for row in rows if int(row["affected"] or 0) > 0}
    changed, previous = _changed("endurance:active_errors", current, db_path=db_path)
    if not changed:
        return []
    output: list[dict[str, Any]] = []
    prior = previous if isinstance(previous, dict) else {}
    for error_class, details in current.items():
        if prior.get(error_class) == details:
            continue
        output.append(_event(component="endurance-intervals", source="endurance_queue", event_type="terminal_sync_error", severity="warning", correlation=f"endurance-intervals:terminal_sync_error:{error_class}", message=f"Active Endurance sync errors: {error_class}", error_class=error_class, details=details, affected=details["affected"], occurrence=f"{error_class}:{json.dumps(details, sort_keys=True)}"))
    for error_class in prior:
        if error_class not in current:
            output.append(_event(component="endurance-intervals", source="endurance_queue", event_type="recovery", severity="info", correlation=f"endurance-intervals:terminal_sync_error:{error_class}", message=f"Recovered Endurance sync errors: {error_class}", error_class=error_class, recovery=True, occurrence=f"recovery:{error_class}:{store.utc_now()}"))
    return output


def collect_systemd(*, db_path: str | None = None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    deployment = store.get_state("release:in_progress", path=db_path)
    if isinstance(deployment, dict) and deployment.get("deployment_id"):
        return output
    emergency = store.get_state("emergency:operation", path=db_path)
    if isinstance(emergency, dict) and emergency.get("active"):
        # A deliberately requested restart is not a fresh outage episode.
        return output
    for unit, component in SYSTEMD_UNITS.items():
        try:
            result = subprocess.run(["systemctl", "show", unit, "--no-pager", "--property=ActiveState", "--property=Result", "--property=ExecMainStatus"], capture_output=True, text=True, timeout=2, check=False)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            failed = result.returncode != 0 or values.get("ActiveState") == "failed" or values.get("Result") not in {None, "", "success"}
            state = {"failed": failed, "result": values.get("Result"), "active": values.get("ActiveState"), "exit": values.get("ExecMainStatus")}
        except Exception as exc:
            state = {"failed": True, "error": type(exc).__name__}
        changed, previous = _changed(f"unit:{unit}", state, db_path=db_path)
        if not changed:
            continue
        correlation = f"systemd:{unit}"
        if state["failed"]:
            output.append(_event(component=component, source="systemd", event_type="unit_failed", severity="critical", correlation=correlation, message=f"systemd unit failed: {unit}", details=state, occurrence=json.dumps(state, sort_keys=True)))
        elif previous and previous.get("failed"):
            output.append(_event(component=component, source="systemd", event_type="unit_recovered", severity="info", correlation=correlation, message=f"systemd unit recovered: {unit}", details=state, recovery=True, occurrence=f"recovered:{store.utc_now()}"))
    return output


def collect_storage(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Observe root capacity; retention remains the only deletion mechanism."""
    from .emergency import storage_status
    state = storage_status()
    changed, previous = _changed("storage:root", state, db_path=db_path)
    if not changed:
        return []
    if state["state"] == "critical":
        return [_event(component="control-center", source="storage", event_type="root_storage_critical", severity="critical", correlation="storage:root", message="Root storage is critical; releases are blocked.", error_class="storage_critical", details=state, occurrence=f"critical:{state['free_bytes']}")]
    if previous and previous.get("state") == "critical":
        return [_event(component="control-center", source="storage", event_type="root_storage_recovered", severity="info", correlation="storage:root", message="Root storage recovered from critical capacity.", details=state, recovery=True, occurrence=f"recovered:{state['free_bytes']}")]
    return []


def run_once(*, db_path: str | None = None) -> dict[str, Any]:
    store.initialize(db_path)
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in (collect_health, collect_full_recovery, collect_endurance, collect_systemd, collect_storage):
        try:
            events.extend(source(db_path=db_path))
        except Exception as exc:
            errors.append(type(exc).__name__)
    inserted = [store.record_event(event, path=db_path) for event in events]
    # Delivery failures are explicitly incident-ineligible in the adapter, so
    # this one-way dispatch cannot create notification recursion.
    try:
        from .notifications import dispatch_incident
        for event, result in zip(events, inserted):
            if result.get("inserted"):
                dispatch_incident(event, result)
    except Exception as exc:
        errors.append(type(exc).__name__)
    store.apply_retention(path=db_path)
    return {"ok": not errors, "events_observed": len(events), "events_inserted": sum(1 for item in inserted if item["inserted"]), "errors": errors}


if __name__ == "__main__":
    print(json.dumps(run_once(), ensure_ascii=True))
