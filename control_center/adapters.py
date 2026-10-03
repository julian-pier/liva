from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database import connections
from database.sqlite_backup import DEFAULT_DROPBOX_BACKUP_DIR
from jobs.daily_backup import BACKUP_META_PATH

from .models import ManagedJobSnapshot, OperationalCheck, utc_now_iso
from .redaction import redact
from . import store


ENDURANCE_COMPONENT = "endurance-intervals"
BACKUP_COMPONENT = "daily-sqlite-backup"
FULL_RECOVERY_COMPONENT = "full-recovery"
_STALE_BACKUP_SECONDS = 30 * 60 * 60
_ENDURANCE_RECENT_FAILURE_SECONDS = 30 * 60
_ENDURANCE_PENDING_DEGRADED_SECONDS = 45 * 60
_ENDURANCE_PENDING_FAILING_SECONDS = 2 * 60 * 60

# These are explicit recovery requirements for the future full-restore goal,
# not claims that today's SQLite backup already fulfils them.
RECOVERY_CATEGORIES: tuple[dict[str, str], ...] = (
    {"category_id": "sqlite_databases", "name": "SQLite databases", "role": "primary application data"},
    {"category_id": "memory_vault", "name": "Memory vault", "role": "living wiki and source files"},
    {"category_id": "memory_runtime_audit_state", "name": "Memory runtime and audit state", "role": "memory service runtime state"},
    {"category_id": "mcp_oauth_config", "name": "MCP OAuth and configuration", "role": "MCP authentication and configuration"},
    {"category_id": "runtime_environment", "name": "Runtime environment", "role": ".env and /etc/liva.env"},
    {"category_id": "secrets_tokens", "name": "Secrets and tokens", "role": "credentials required for recovery"},
    {"category_id": "private_uploads", "name": "Private uploads", "role": "physique and other private files"},
    {"category_id": "schoolsync_private_state", "name": "SchoolSync and private file state", "role": "WebUntis and private integration state"},
)


def _full_recovery_automation() -> dict[str, Any]:
    """Read live timer state without making the Control Center an automation owner."""
    units = ("liva-full-recovery.timer", "liva-full-recovery-catchup.timer")
    try:
        result = subprocess.run(
            ["systemctl", "show", *units, "--no-pager", "-p", "Id", "-p", "ActiveState", "-p", "UnitFileState", "-p", "NextElapseUSecRealtime"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("systemctl unavailable")
        groups: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line:
                if current:
                    groups.append(current); current = {}
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                current[key] = value
        if current:
            groups.append(current)
        values = {item.get("Id"): item for item in groups if item.get("Id")}
        main = values.get(units[0], {})
        catchup = values.get(units[1], {})
        active = main.get("ActiveState") == "active" and main.get("UnitFileState") == "enabled"
        catchup_active = catchup.get("ActiveState") == "active" and catchup.get("UnitFileState") == "enabled"
        return {
            "active": active,
            "catchup_enabled": catchup_active,
            "next_scheduled_run": main.get("NextElapseUSecRealtime") or None,
            "next_catchup_run": catchup.get("NextElapseUSecRealtime") or None,
        }
    except Exception:
        return {"active": False, "catchup_enabled": False, "next_scheduled_run": None, "next_catchup_run": None}


def full_recovery_snapshot() -> tuple[list[OperationalCheck], ManagedJobSnapshot]:
    """Read-only projection of persisted full-recovery state; never runs a backup."""
    checked_at = utc_now_iso()
    state = store.get_full_recovery_state() or {"phase": "setup", "status": "not_initialized", "backup_due": 0, "expected_device": {"uuid": "65CA-09BE", "label": "LIVA_RECOVERY", "filesystem": "vfat"}}
    raw = str(state.get("status") or "unknown")
    automation = _full_recovery_automation()
    status = {"ready": "healthy", "verified": "healthy", "restore_tested": "healthy", "running": "degraded", "due": "degraded", "not_initialized": "inactive", "failed": "failing"}.get(raw, "unknown")
    encryption = "configured" if state.get("recipient_fingerprint") else "not_configured"
    reason = {"not_initialized": "Not initialized: a public age recipient is not configured.", "ready": "Ready for the explicitly activated backup service.", "verified": "Latest backup is transport-verified; this is not a complete restore proof.", "restore_tested": "Latest recovery bundle passed an isolated full-content restore test.", "failed": str(state.get("message") or "Full recovery failed."), "running": "Full recovery is running.", "due": "A full recovery backup is due."}.get(raw, "Full recovery state is unavailable.")
    details = redact({**{key: state.get(key) for key in ("phase", "status", "last_attempt_at", "last_created_at", "last_transport_verified_at", "last_restore_tested_at", "last_restore_test_backup_id", "restore_test_type", "restore_test_result", "restore_test_duration_seconds", "last_mirror_verified_at", "backup_due", "due_since", "next_retry_at", "error_code", "message", "backup_id", "mirror_backup_id", "expected_device", "observed_device", "source_bytes", "ciphertext_bytes", "runtime_seconds", "verification_state", "recipient_fingerprint", "file_count", "category_count", "free_space_before", "free_space_after", "primary_media_uuid", "primary_media_status", "offline_mirror_uuid", "offline_mirror_status", "mirror_hash_status", "retention_deletions", "source_commit")}, "automation": automation})
    checks = [
        OperationalCheck("full_recovery.engine", FULL_RECOVERY_COMPONENT, "Full Recovery backup engine", status, checked_at, reason, last_success_at=state.get("last_transport_verified_at"), details=details, error_class=state.get("error_code")),
        OperationalCheck("full_recovery.encryption", FULL_RECOVERY_COMPONENT, "Full Recovery encryption", "healthy" if encryption == "configured" else "inactive", checked_at, "Public age recipient configured." if encryption == "configured" else "No public age recipient configured.", details={"encryption": encryption, "recipient_fingerprint": state.get("recipient_fingerprint")}),
        OperationalCheck("full_recovery.coverage", FULL_RECOVERY_COMPONENT, "Full Recovery coverage", "degraded" if not state.get("last_transport_verified_at") else "healthy", checked_at, "Full allowlist is prepared; no verified full backup exists yet." if not state.get("last_transport_verified_at") else "Allowlist coverage recorded in the verified recovery bundle.", details={"categories": RECOVERY_CATEGORIES, "file_count": state.get("file_count"), "category_count": state.get("category_count")}),
    ]
    schedule = "daily at 05:30 (active)" if automation["active"] else "daily at 05:30 (not activated)"
    job = ManagedJobSnapshot("full-recovery", FULL_RECOVERY_COMPONENT, "Encrypted Full Recovery", schedule, status, "control_center.full_recovery_state", checked_at, last_run_at=state.get("last_attempt_at"), last_success_at=state.get("last_transport_verified_at"), failed_count=1 if raw == "failed" else 0, capabilities={"encrypted": True, "restore_plan": True, "activated": automation["active"], "catchup_enabled": automation["catchup_enabled"], "read_only": True}, details=details)
    return checks, job


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z") if parsed else None


def _freshness_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    parsed = _parse_iso(value)
    if not parsed:
        return None
    return max(0, int(((now or datetime.now(timezone.utc)) - parsed).total_seconds()))


def _readonly_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=1, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _systemd_timestamp(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw == "n/a":
        return None
    try:
        head, abbreviation = raw.rsplit(" ", 1)
        offset = {"UTC": 0, "CET": 1, "CEST": 2}.get(abbreviation)
        if offset is None:
            return None
        parsed = datetime.strptime(head, "%a %Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=offset)))
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _systemd_properties(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", unit, "--no-pager", *(f"--property={name}" for name in properties)],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemctl_show_failed")
    return {key: value for line in result.stdout.splitlines() if "=" in line for key, value in [line.split("=", 1)]}


def _endurance_runtime_state() -> dict[str, Any]:
    try:
        timer = _systemd_properties("liva-endurance-intervals-sync.timer", ("ActiveState", "LastTriggerUSec"))
        service = _systemd_properties("liva-endurance-intervals-sync.service", ("Result", "ExecMainStatus", "ExecMainExitTimestamp"))
        return {
            "available": True,
            "timer_active": timer.get("ActiveState") == "active",
            "last_run_at": _systemd_timestamp(timer.get("LastTriggerUSec", "")),
            "last_result": service.get("Result") or "unknown",
            "last_exit_status": int(service.get("ExecMainStatus") or 0),
        }
    except Exception as exc:
        return {"available": False, "error": redact(str(exc))}


def endurance_snapshot(
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    runtime_state: dict[str, Any] | None = None,
) -> tuple[list[OperationalCheck], ManagedJobSnapshot]:
    checked_at = utc_now_iso()
    source_path = db_path or connections.PLANS_DB
    now_ref = now or datetime.now(timezone.utc)
    try:
        with _readonly_connection(source_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest_created, MAX(updated_at) AS last_updated FROM endurance_sync_jobs GROUP BY status"
            ).fetchall()
            done = conn.execute("SELECT MAX(updated_at) AS last_success_at FROM endurance_sync_jobs WHERE status='done'").fetchone()
            failed_summary = conn.execute(
                """SELECT COUNT(*) AS failed_rows, COUNT(DISTINCT external_id) AS failed_objects,
                MIN(updated_at) AS oldest_failure_at, MAX(updated_at) AS latest_failure_at
                FROM endurance_sync_jobs WHERE status='failed'"""
            ).fetchone()
            latest_failure = conn.execute(
                """SELECT f.updated_at, f.attempt_count,
                CASE WHEN instr(COALESCE(last_error,''), ':') > 0
                     THEN substr(last_error, 1, instr(last_error, ':') - 1)
                     WHEN COALESCE(last_error,'') = '' THEN NULL ELSE 'unclassified' END AS error_class,
                EXISTS(SELECT 1 FROM endurance_sync_jobs d
                    WHERE d.external_id=f.external_id AND d.operation=f.operation
                      AND d.status='done' AND d.updated_at>f.updated_at) AS later_succeeded
                FROM endurance_sync_jobs f WHERE f.status='failed' ORDER BY f.updated_at DESC LIMIT 1"""
            ).fetchone()
            recovered = conn.execute(
                """WITH failures AS (
                    SELECT external_id, operation, MAX(updated_at) AS latest_failure
                    FROM endurance_sync_jobs WHERE status='failed' GROUP BY external_id, operation
                )
                SELECT COUNT(*) AS failed_objects,
                       SUM(EXISTS(SELECT 1 FROM endurance_sync_jobs d
                           WHERE d.external_id=f.external_id AND d.operation=f.operation
                             AND d.status='done' AND d.updated_at>f.latest_failure)) AS later_succeeded
                FROM failures f"""
            ).fetchone()
            active_errors = conn.execute(
                """SELECT COUNT(*) AS count, MIN(scheduled_date) AS earliest_date
                FROM endurance_sessions
                WHERE sync_state='sync_error' AND scheduled_date>=?""",
                (now_ref.date().isoformat(),),
            ).fetchone()
    except Exception as exc:
        details = redact({"database_path": source_path, "error": str(exc)})
        check = OperationalCheck("endurance.queue.reachable", ENDURANCE_COMPONENT, "Intervals sync queue", "unknown", checked_at, "Queue state could not be read.", details=details, error_class=type(exc).__name__)
        job = ManagedJobSnapshot("endurance-intervals-sync", ENDURANCE_COMPONENT, "Endurance → Intervals sync", "every 15 minutes", "unknown", "plans.sqlite3:endurance_sync_jobs", checked_at, capabilities={"retry_backoff": True, "dedupe": True, "read_only": True}, details=details)
        return [check], job

    counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
    last_run_at = max((_iso(row["last_updated"]) for row in rows if _iso(row["last_updated"])), default=None)
    last_success_at = _iso(done["last_success_at"] if done else None)
    pending = counts.get("pending", 0)
    retry = counts.get("retry", 0)
    failed = counts.get("failed", 0)
    active_rows = [row for row in rows if str(row["status"]) in {"pending", "retry"}]
    oldest_active = min((_iso(row["oldest_created"]) for row in active_rows if _iso(row["oldest_created"])), default=None)
    queue_lag = _freshness_seconds(oldest_active, now=now_ref) if oldest_active else 0
    latest_failure_at = _iso(failed_summary["latest_failure_at"] if failed_summary else None)
    latest_failure_age = _freshness_seconds(latest_failure_at, now=now_ref)
    latest_failure_attempts = int(latest_failure["attempt_count"] or 0) if latest_failure else 0
    latest_failure_recovered = bool(latest_failure["later_succeeded"]) if latest_failure else False
    active_sync_errors = int(active_errors["count"] or 0) if active_errors else 0
    historical_failed_objects = int(failed_summary["failed_objects"] or 0) if failed_summary else 0
    later_succeeded = int(recovered["later_succeeded"] or 0) if recovered else 0
    runtime = runtime_state if runtime_state is not None else (_endurance_runtime_state() if db_path is None else {"available": False})

    if runtime.get("available") and (runtime.get("last_result") != "success" or int(runtime.get("last_exit_status") or 0) != 0):
        status, reason = "failing", "The last scheduled sync service run failed."
    elif runtime.get("available") and not runtime.get("timer_active"):
        status, reason = "degraded", "The sync timer is not active."
    elif (pending or retry) and queue_lag is not None and queue_lag >= _ENDURANCE_PENDING_FAILING_SECONDS:
        status, reason = "failing", f"{pending + retry} queued job(s) have not been processed within the expected schedule."
    elif latest_failure_age is not None and latest_failure_age <= _ENDURANCE_RECENT_FAILURE_SECONDS and latest_failure_attempts >= 2 and not latest_failure_recovered:
        status, reason = "failing", "The most recent job failed repeatedly within the current operating window."
    elif (pending or retry) and queue_lag is not None and queue_lag >= _ENDURANCE_PENDING_DEGRADED_SECONDS:
        status, reason = "degraded", f"{pending + retry} queued job(s) are older than the normal processing window."
    elif retry:
        status, reason = "degraded", f"{retry} job(s) are currently waiting for retry."
    elif active_sync_errors:
        status, reason = "degraded", f"Queue processing is idle, but {active_sync_errors} future session(s) remain in sync_error."
    else:
        status = "healthy"
        if pending:
            reason = f"Queue is reachable; {pending} new job(s) are within the normal processing window."
        elif failed:
            reason = "Current queue is clear; historical failed jobs remain visible as history."
        else:
            reason = "Queue is reachable and has no current work or errors."
    details = {
        "counts": counts,
        "current_queue": {"pending": pending, "retry": retry, "queue_lag_seconds": queue_lag or 0},
        "historical_failed_rows": failed,
        "historical_failed_objects": historical_failed_objects,
        "failed_objects_later_succeeded": later_succeeded,
        "oldest_failure_at": _iso(failed_summary["oldest_failure_at"] if failed_summary else None),
        "latest_failure_at": latest_failure_at,
        "latest_failure_class": redact(str(latest_failure["error_class"] or "unknown")) if latest_failure else None,
        "latest_failure_recovered": latest_failure_recovered,
        "active_sync_error_sessions": active_sync_errors,
        "earliest_active_sync_error_date": str(active_errors["earliest_date"] or "") or None if active_errors else None,
        "queue_lag_seconds": queue_lag or 0,
        "last_queue_activity_at": last_run_at,
        "runtime": runtime,
        "database_path": source_path,
        "query_mode": "read_only",
    }
    check = OperationalCheck("endurance.queue.state", ENDURANCE_COMPONENT, "Intervals sync queue", status, checked_at, reason, last_success_at=last_success_at, details=details, freshness_seconds=_freshness_seconds(last_run_at, now=now))
    job = ManagedJobSnapshot("endurance-intervals-sync", ENDURANCE_COMPONENT, "Endurance → Intervals sync", "every 15 minutes", status, "plans.sqlite3:endurance_sync_jobs + systemd", checked_at, last_run_at=runtime.get("last_run_at") or last_run_at, last_success_at=last_success_at, pending_count=pending, failed_count=failed, retry_count=retry, capabilities={"retry_backoff": True, "dedupe": True, "read_only": True}, details=details)
    return [check], job


def _load_backup_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None, "metadata_is_not_an_object"
        return raw, None
    except FileNotFoundError:
        return None, "metadata_missing"
    except Exception as exc:
        return None, f"metadata_unreadable: {exc}"


def _configured_backup_targets(metadata: dict[str, Any] | None) -> list[str]:
    targets = [str(DEFAULT_DROPBOX_BACKUP_DIR)]
    payload = ((metadata or {}).get("payload") or {}).get("sqlite_backup") or {}
    historical_targets = payload.get("targets") if isinstance(payload, dict) else {}
    if isinstance(historical_targets, dict):
        targets.extend(str(item) for item in historical_targets)
    configured = (os.getenv("LIVA_USB_BACKUP_DIRS") or "").strip()
    if configured:
        targets.extend(item.strip() for item in configured.split(",") if item.strip())
    roots = (os.getenv("LIVA_USB_MOUNT_ROOTS") or "").strip()
    if roots:
        targets.extend(str(Path(item.strip()) / "backups" / "sqlite") for item in roots.split(",") if item.strip())
    return list(dict.fromkeys(targets))


def _mounted_paths() -> set[str]:
    try:
        rows = Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return set()
    return {parts[1].replace("\\040", " ") for line in rows if len(parts := line.split()) >= 2}


def _target_is_mounted(target: str, mounts: set[str]) -> bool:
    return any(mount != "/" and (target == mount or target.startswith(mount.rstrip("/") + "/")) for mount in mounts)


def _truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _latest_backup_journal_result() -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["journalctl", "--unit=liva-daily-backup.service", "--output=json", "--no-pager", "--lines=200"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        for line in reversed(result.stdout.splitlines()):
            try:
                entry = json.loads(line)
                message = json.loads(str(entry.get("MESSAGE") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or not isinstance(message.get("sqlite_backup"), dict):
                continue
            stamp = entry.get("__REALTIME_TIMESTAMP")
            completed_at = None
            if str(stamp or "").isdigit():
                completed_at = datetime.fromtimestamp(int(stamp) / 1_000_000, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            return {"completed_at": completed_at, "payload": message}
    except Exception:
        return None
    return None


def _backup_runtime_state() -> dict[str, Any]:
    try:
        timer = _systemd_properties("liva-daily-backup.timer", ("ActiveState", "LastTriggerUSec"))
        service = _systemd_properties("liva-daily-backup.service", ("Result", "ExecMainStatus"))
        journal_result = _latest_backup_journal_result()
        return {
            "available": True,
            "timer_active": timer.get("ActiveState") == "active",
            "last_run_at": _systemd_timestamp(timer.get("LastTriggerUSec", "")),
            "last_result": service.get("Result") or "unknown",
            "last_exit_status": int(service.get("ExecMainStatus") or 0),
            "last_completed_at": (journal_result or {}).get("completed_at"),
            "last_payload": (journal_result or {}).get("payload"),
        }
    except Exception as exc:
        return {"available": False, "error": redact(str(exc))}


def _latest_backup_file_evidence(targets: list[str], mounts: set[str]) -> tuple[str | None, list[str]]:
    latest: tuple[float, str] | None = None
    names: set[str] = set()
    for target in targets:
        is_usb = target.startswith(("/mnt/", "/media/"))
        mounted = _target_is_mounted(target, mounts)
        if is_usb and not mounted:
            continue
        try:
            for path in Path(target).iterdir():
                if not path.is_file() or path.suffix.lower() not in {".sqlite3", ".db"}:
                    continue
                modified = path.stat().st_mtime
                names.add(path.name)
                if latest is None or modified > latest[0]:
                    latest = (modified, path.name)
        except OSError:
            continue
    if latest is None:
        return None, sorted(names)
    stamp = datetime.fromtimestamp(latest[0], tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return stamp, sorted(names)


def _backup_overall_status(checks: list[OperationalCheck]) -> str:
    if any(check.status == "failing" for check in checks):
        return "failing"
    critical = {check.check_id: check.status for check in checks}
    if critical.get("backup.daily.execution") == "unknown" or critical.get("backup.freshness") == "unknown":
        return "unknown"
    if any(check.status in {"degraded", "unknown"} for check in checks):
        return "degraded"
    return "healthy"


def backup_snapshot(
    *,
    metadata_path: Path | None = None,
    now: datetime | None = None,
    runtime_state: dict[str, Any] | None = None,
) -> tuple[list[OperationalCheck], ManagedJobSnapshot]:
    checked_at = utc_now_iso()
    metadata_path = metadata_path or BACKUP_META_PATH
    is_production_source = metadata_path == BACKUP_META_PATH
    metadata, error = _load_backup_metadata(metadata_path)
    if error:
        details = redact({"metadata_path": str(metadata_path), "error": error})
        execution = OperationalCheck("backup.daily.execution", BACKUP_COMPONENT, "Daily SQLite backup execution", "unknown", checked_at, "Modern backup metadata is unavailable.", details=details, error_class="BackupMetadataUnavailable")
        freshness = OperationalCheck("backup.freshness", BACKUP_COMPONENT, "Backup freshness", "unknown", checked_at, "Backup freshness cannot be determined without the modern status source.", details=details)
        usb = OperationalCheck("backup.usb.target", BACKUP_COMPONENT, "USB backup target", "unknown", checked_at, "USB target state cannot be tied to a recorded modern backup run.", details=details)
        coverage = OperationalCheck("backup.recovery.coverage", BACKUP_COMPONENT, "Disaster-recovery coverage", "degraded", checked_at, "Full recovery coverage is not implemented.", details={"requirements": RECOVERY_CATEGORIES, "missing_categories": [item["category_id"] for item in RECOVERY_CATEGORIES]})
        restore = OperationalCheck("backup.restore.verification", BACKUP_COMPONENT, "Restore verification", "unknown", checked_at, "No complete restore verification is recorded.", details={"restore_verification": "not_recorded"})
        job = ManagedJobSnapshot("daily-sqlite-backup", BACKUP_COMPONENT, "Daily SQLite backup", "daily at 03:00", "unknown", str(metadata_path), checked_at, capabilities={"sqlite_online_snapshot": True, "read_only": True}, details=details)
        return [execution, freshness, usb, coverage, restore], job

    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    metadata_backup = payload.get("sqlite_backup") if isinstance(payload.get("sqlite_backup"), dict) else {}
    updated_at = _iso(metadata.get("updated_at"))
    runtime = runtime_state if runtime_state is not None else (_backup_runtime_state() if is_production_source else {"available": False})
    journal_payload = runtime.get("last_payload") if isinstance(runtime.get("last_payload"), dict) else {}
    journal_backup = journal_payload.get("sqlite_backup") if isinstance(journal_payload.get("sqlite_backup"), dict) else {}
    effective_backup = journal_backup or metadata_backup
    metadata_after_scheduled_run = bool(updated_at and runtime.get("last_completed_at") and updated_at > str(runtime.get("last_completed_at")))

    if runtime.get("available") and (runtime.get("last_result") != "success" or int(runtime.get("last_exit_status") or 0) != 0):
        execution_status, reason = "failing", "The last scheduled modern backup service run failed."
    elif runtime.get("available") and not runtime.get("timer_active"):
        execution_status, reason = "degraded", "The modern backup timer is not active."
    elif runtime.get("available"):
        execution_status, reason = "healthy", "The last scheduled modern backup service run completed successfully."
    elif metadata_backup.get("ok") is True:
        execution_status, reason = "healthy", "The latest recorded modern SQLite backup completed successfully."
    elif metadata_backup:
        execution_status, reason = "failing", "The latest recorded modern SQLite backup failed."
    else:
        execution_status, reason = "unknown", "Modern backup metadata has no SQLite execution result."

    if is_production_source:
        targets = _configured_backup_targets(metadata)
    else:
        fixture_targets = list((metadata_backup.get("targets") or {}).keys()) + list((metadata_backup.get("target_errors") or {}).keys())
        configured_usb = (os.getenv("LIVA_USB_BACKUP_DIRS") or "").strip()
        if configured_usb:
            fixture_targets.extend(item.strip() for item in configured_usb.split(",") if item.strip())
        targets = list(dict.fromkeys(str(item) for item in fixture_targets))
    effective_target_paths = list((effective_backup.get("targets") or {}).keys()) + list((effective_backup.get("target_errors") or {}).keys())
    targets = list(dict.fromkeys([*targets, *(str(item) for item in effective_target_paths)]))
    mounts = _mounted_paths()
    usb_targets = [target for target in targets if target.startswith(("/mnt/", "/media/"))]
    usb_required = _truthy_env("LIVA_USB_BACKUP_REQUIRED")
    target_results = effective_backup.get("targets") if isinstance(effective_backup.get("targets"), dict) else {}
    target_errors = effective_backup.get("target_errors") if isinstance(effective_backup.get("target_errors"), dict) else {}
    usb_state = []
    for target in usb_targets:
        mounted = _target_is_mounted(target, mounts)
        result = target_results.get(target) if isinstance(target_results.get(target), dict) else {}
        error = target_errors.get(target) if isinstance(target_errors.get(target), dict) else {}
        usb_state.append({"path": target, "mounted": mounted, "last_recorded_success": bool(result) and all(value == "ok" for value in result.values()), "last_recorded_error": bool(error)})

    any_usb_error = any(item["last_recorded_error"] for item in usb_state)
    any_usb_mounted = any(item["mounted"] for item in usb_state)
    if not usb_targets:
        usb_status, usb_reason = "inactive", "No USB backup target is configured."
    elif usb_required and execution_status == "failing" and any_usb_error:
        usb_status, usb_reason = "failing", "A required USB target prevented the expected backup run from succeeding."
    elif any_usb_mounted and not any_usb_error:
        usb_status, usb_reason = "healthy", "The configured USB backup target is mounted without a recorded target error."
    elif usb_required:
        usb_status, usb_reason = "degraded", "The required USB target is not currently mounted, but no expected run is presently recorded as missed because of it."
    else:
        usb_status, usb_reason = "degraded", "The optional USB target is not currently mounted; the successful primary backup remains valid."

    latest_file_at, backup_files = _latest_backup_file_evidence(targets, mounts)
    recorded_success_at = _iso(runtime.get("last_completed_at")) if execution_status == "healthy" and runtime.get("last_completed_at") else None
    if not recorded_success_at and metadata_backup.get("ok") is True:
        recorded_success_at = updated_at
    latest_success_at = max((stamp for stamp in (latest_file_at, recorded_success_at) if stamp), default=None)
    freshness_seconds = _freshness_seconds(latest_success_at, now=now)
    if freshness_seconds is None:
        freshness_status, freshness_reason = "unknown", "No successful modern backup timestamp could be established."
    elif freshness_seconds > _STALE_BACKUP_SECONDS:
        freshness_status, freshness_reason = "failing", "No sufficiently fresh successful modern backup is available."
    else:
        freshness_status, freshness_reason = "healthy", "A successful modern backup exists within the expected freshness window."

    sources = effective_backup.get("sources") if isinstance(effective_backup.get("sources"), dict) else {}
    source_names = sorted(set(sources) | set(backup_files))
    runtime_public = {key: runtime.get(key) for key in ("available", "timer_active", "last_run_at", "last_completed_at", "last_result", "last_exit_status") if key in runtime}
    execution_details = redact({
        "metadata_path": str(metadata_path),
        "metadata_updated_at": updated_at,
        "metadata_after_last_scheduled_run": metadata_after_scheduled_run,
        "evidence_source": "systemd+journal" if journal_backup else ("systemd+target_files" if runtime.get("available") else "metadata"),
        "runtime": runtime_public,
        "sources": source_names,
        "targets": sorted(target_results),
        "remote_sync_status": (effective_backup.get("remote_sync") or {}).get("status") if isinstance(effective_backup.get("remote_sync"), dict) else None,
        "sqlite_snapshot_mechanism": "sqlite_online_backup_api",
    })
    execution = OperationalCheck("backup.daily.execution", BACKUP_COMPONENT, "Daily SQLite backup execution", execution_status, checked_at, reason, last_success_at=latest_success_at if execution_status == "healthy" else None, details=execution_details)
    freshness_check = OperationalCheck("backup.freshness", BACKUP_COMPONENT, "Backup freshness", freshness_status, checked_at, freshness_reason, last_success_at=latest_success_at, details=redact({"latest_successful_backup_at": latest_success_at, "backup_files": backup_files, "freshness_threshold_seconds": _STALE_BACKUP_SECONDS}), freshness_seconds=freshness_seconds)
    usb_check = OperationalCheck("backup.usb.target", BACKUP_COMPONENT, "USB backup target", usb_status, checked_at, usb_reason, details=redact({"required": usb_required, "targets": usb_state}))

    categories = []
    for requirement in RECOVERY_CATEGORIES:
        covered = requirement["category_id"] == "sqlite_databases" and bool(sources)
        categories.append({**requirement, "covered": covered})
    missing = [item["category_id"] for item in categories if not item["covered"]]
    coverage_details = {"categories": categories, "missing_categories": missing, "legacy_tar_counts_as_full_recovery": False}
    coverage = OperationalCheck("backup.recovery.coverage", BACKUP_COMPONENT, "Disaster-recovery coverage", "degraded", checked_at, "Current backup covers SQLite data only; it is not a tested full LIVA restore.", details=coverage_details)
    restore = OperationalCheck("backup.restore.verification", BACKUP_COMPONENT, "Restore verification", "unknown", checked_at, "No complete restore verification is recorded.", details={"restore_verification": "not_recorded", "legacy_tar_counts_as_restore_test": False})
    checks = [execution, freshness_check, usb_check, coverage, restore]
    job_status = _backup_overall_status(checks)
    job_details = {**execution_details, "freshness": freshness_check.details, "usb": usb_check.details, "recovery_coverage": coverage_details, "restore_verification": "not_recorded"}
    job = ManagedJobSnapshot("daily-sqlite-backup", BACKUP_COMPONENT, "Daily SQLite backup", "daily at 03:00", job_status, str(metadata_path), checked_at, last_run_at=_iso(runtime.get("last_run_at")) or updated_at, last_success_at=latest_success_at, capabilities={"sqlite_online_snapshot": True, "configured_targets": True, "full_restore_tested": False, "read_only": True}, details=job_details)
    return checks, job
