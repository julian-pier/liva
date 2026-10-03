from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database import connections

from .redaction import redact


DB_PATH = Path(connections.CONTROL_CENTER_DB)
EVENT_RETENTION_DAYS = 30
RESOLVED_INCIDENT_RETENTION_DAYS = 180


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = str(path or DB_PATH)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialize(path: Path | str | None = None) -> None:
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_center_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collector_state (
                source_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                correlation_key TEXT NOT NULL,
                component_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','resolved')),
                severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT,
                occurrence_count INTEGER NOT NULL DEFAULT 0,
                affected_object_count INTEGER NOT NULL DEFAULT 0,
                last_event_id TEXT
            );
            CREATE TABLE IF NOT EXISTS operational_events (
                event_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                incident_id TEXT REFERENCES incidents(incident_id) ON DELETE SET NULL,
                component_id TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
                occurred_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                message TEXT NOT NULL,
                error_class TEXT,
                correlation_key TEXT NOT NULL,
                details_json TEXT NOT NULL,
                object_ref TEXT,
                is_recovery INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON operational_events(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_incident ON operational_events(incident_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_component ON operational_events(component_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_incidents_status_seen ON incidents(status, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_incidents_correlation ON incidents(correlation_key, status);
            INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (1, datetime('now'));
            CREATE TABLE IF NOT EXISTS full_recovery_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                last_attempt_at TEXT, last_created_at TEXT, last_transport_verified_at TEXT,
                last_restore_tested_at TEXT, backup_due INTEGER NOT NULL DEFAULT 0,
                due_since TEXT, next_retry_at TEXT, phase TEXT NOT NULL DEFAULT 'setup',
                status TEXT NOT NULL DEFAULT 'not_initialized', error_code TEXT,
                message TEXT, backup_id TEXT, expected_device_json TEXT NOT NULL DEFAULT '{}',
                observed_device_json TEXT NOT NULL DEFAULT '{}', source_bytes INTEGER,
                ciphertext_bytes INTEGER, runtime_seconds REAL, verification_state TEXT,
                recipient_fingerprint TEXT, file_count INTEGER, category_count INTEGER,
                free_space_before INTEGER, free_space_after INTEGER,
                retention_deletions_json TEXT NOT NULL DEFAULT '[]', source_commit TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (2, datetime('now'));
            CREATE TABLE IF NOT EXISTS job_action_audit (
                action_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL, action TEXT NOT NULL, requested_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, actor_role TEXT NOT NULL,
                result TEXT NOT NULL, error_code TEXT, message TEXT,
                correlation_key TEXT, resulting_state_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_job_action_audit_job_time ON job_action_audit(job_id, requested_at DESC);
            CREATE TABLE IF NOT EXISTS self_healing_policy_state (
                policy_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                attempts_json TEXT NOT NULL DEFAULT '[]', next_allowed_at TEXT,
                last_attempt_at TEXT, last_result TEXT, manual_intervention_required INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS technical_config_audit (
                audit_id TEXT PRIMARY KEY, config_id TEXT NOT NULL, previous_json TEXT NOT NULL,
                requested_json TEXT NOT NULL, actor_role TEXT NOT NULL, requested_at TEXT NOT NULL,
                validation TEXT NOT NULL, apply_result TEXT NOT NULL, resulting_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_technical_config_audit_time ON technical_config_audit(config_id, requested_at DESC);
            CREATE TABLE IF NOT EXISTS notification_delivery (
                notification_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                incident_id TEXT, event_id TEXT, provider TEXT NOT NULL, requested_at TEXT NOT NULL,
                sent_at TEXT, result TEXT NOT NULL, error_code TEXT, diagnostics_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_notification_delivery_time ON notification_delivery(requested_at DESC);
            CREATE TABLE IF NOT EXISTS deployments (
                deployment_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                actor_role TEXT NOT NULL, source TEXT NOT NULL, commit_before TEXT, commit_target TEXT,
                commit_after TEXT, branch TEXT, dirty_before INTEGER, status TEXT NOT NULL,
                preflight_status TEXT, migration_status TEXT, service_restart_status TEXT,
                post_deploy_health_status TEXT, rollback_available INTEGER NOT NULL DEFAULT 0,
                rollback_target TEXT, rollback_result TEXT, error_code TEXT, message TEXT,
                plan_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_deployments_time ON deployments(requested_at DESC);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(full_recovery_state)")}
        for name, definition in (
            ("last_restore_test_backup_id", "TEXT"),
            ("restore_test_type", "TEXT"),
            ("restore_test_result", "TEXT"),
            ("restore_test_duration_seconds", "REAL"),
            ("primary_media_uuid", "TEXT"),
            ("primary_media_status", "TEXT"),
            ("offline_mirror_uuid", "TEXT"),
            ("offline_mirror_status", "TEXT"),
            ("last_mirror_verified_at", "TEXT"),
            ("mirror_backup_id", "TEXT"),
            ("mirror_hash_status", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE full_recovery_state ADD COLUMN {name} {definition}")
        conn.execute("INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (4, datetime('now'))")
        conn.execute("INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (5, datetime('now'))")
        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_action_audit)")}
        for name, definition in (
            ("actor_type", "TEXT NOT NULL DEFAULT 'operator'"), ("policy_id", "TEXT"),
            ("trigger_event_id", "TEXT"), ("incident_id", "TEXT"), ("attempt", "INTEGER"),
            ("budget_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in audit_columns:
                conn.execute(f"ALTER TABLE job_action_audit ADD COLUMN {name} {definition}")
        conn.execute("INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (6, datetime('now'))")
        conn.execute("INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (7, datetime('now'))")
        conn.execute("INSERT OR IGNORE INTO control_center_schema(version, applied_at) VALUES (8, datetime('now'))")


def _json(value: Any) -> str:
    return json.dumps(redact(value if isinstance(value, (dict, list)) else {"value": value}), ensure_ascii=True, sort_keys=True)


def get_state(source_key: str, *, path: Path | str | None = None) -> Any:
    initialize(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT value_json FROM collector_state WHERE source_key=?", (source_key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None


def set_state(source_key: str, value: Any, *, path: Path | str | None = None, observed_at: str | None = None) -> None:
    initialize(path)
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO collector_state(source_key,value_json,updated_at) VALUES (?,?,?) ON CONFLICT(source_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (source_key, _json(value), observed_at or utc_now()),
        )


def record_config_audit(value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    initialize(path)
    record = redact(value)
    record.setdefault("audit_id", uuid.uuid4().hex); record.setdefault("requested_at", utc_now())
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO technical_config_audit(audit_id,config_id,previous_json,requested_json,actor_role,requested_at,validation,apply_result,resulting_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (record["audit_id"], record["config_id"], _json(record.get("previous") or {}), _json(record.get("requested") or {}), record["actor_role"], record["requested_at"], record["validation"], record["apply_result"], _json(record.get("resulting") or {})),
        )
    return record


def record_notification_delivery(value: dict[str, Any], *, path: Path | str | None = None) -> tuple[dict[str, Any], bool]:
    initialize(path)
    record = redact(value)
    record.setdefault("notification_id", uuid.uuid4().hex); record.setdefault("requested_at", utc_now())
    with _connect(path) as conn:
        existing = conn.execute("SELECT * FROM notification_delivery WHERE dedupe_key=?", (record["dedupe_key"],)).fetchone()
        if existing: return dict(existing), False
        conn.execute(
            "INSERT INTO notification_delivery(notification_id,dedupe_key,incident_id,event_id,provider,requested_at,sent_at,result,error_code,diagnostics_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record["notification_id"], record["dedupe_key"], record.get("incident_id"), record.get("event_id"), record["provider"], record["requested_at"], record.get("sent_at"), record["result"], record.get("error_code"), _json(record.get("diagnostics") or {})),
        )
    return record, True


def list_notification_deliveries(*, limit: int = 50, path: Path | str | None = None) -> list[dict[str, Any]]:
    initialize(path)
    with _connect(path) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM notification_delivery ORDER BY requested_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()]
    for row in rows:
        try: row["diagnostics"] = json.loads(row.pop("diagnostics_json") or "{}")
        except ValueError: row["diagnostics"] = {}
    return rows


def update_notification_delivery(value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    initialize(path)
    record = redact(value)
    with _connect(path) as conn:
        conn.execute("UPDATE notification_delivery SET sent_at=?,result=?,error_code=?,diagnostics_json=? WHERE notification_id=?", (record.get("sent_at"), record["result"], record.get("error_code"), _json(record.get("diagnostics") or {}), record["notification_id"]))
    return record


def create_deployment(value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    initialize(path); record = redact(value)
    with _connect(path) as conn:
        conn.execute("INSERT INTO deployments(deployment_id,requested_at,started_at,finished_at,actor_role,source,commit_before,commit_target,commit_after,branch,dirty_before,status,preflight_status,migration_status,service_restart_status,post_deploy_health_status,rollback_available,rollback_target,rollback_result,error_code,message,plan_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record["deployment_id"], record["requested_at"], record.get("started_at"), record.get("finished_at"), record["actor_role"], record["source"], record.get("commit_before"), record.get("commit_target"), record.get("commit_after"), record.get("branch"), int(bool(record.get("dirty_before"))), record["status"], record.get("preflight_status"), record.get("migration_status"), record.get("service_restart_status"), record.get("post_deploy_health_status"), int(bool(record.get("rollback_available"))), record.get("rollback_target"), record.get("rollback_result"), record.get("error_code"), record.get("message"), _json(record.get("plan") or {})))
    return record


def update_deployment(deployment_id: str, value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any] | None:
    current = get_deployment(deployment_id, path=path)
    if not current: return None
    merged = {**current, **redact(value)}
    with _connect(path) as conn:
        conn.execute("UPDATE deployments SET started_at=?,finished_at=?,commit_after=?,status=?,preflight_status=?,migration_status=?,service_restart_status=?,post_deploy_health_status=?,rollback_available=?,rollback_target=?,rollback_result=?,error_code=?,message=?,plan_json=? WHERE deployment_id=?", (merged.get("started_at"), merged.get("finished_at"), merged.get("commit_after"), merged["status"], merged.get("preflight_status"), merged.get("migration_status"), merged.get("service_restart_status"), merged.get("post_deploy_health_status"), int(bool(merged.get("rollback_available"))), merged.get("rollback_target"), merged.get("rollback_result"), merged.get("error_code"), merged.get("message"), _json(merged.get("plan") or {}), deployment_id))
    return get_deployment(deployment_id, path=path)


def _deployment_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row: return None
    value = dict(row); value["dirty_before"] = bool(value["dirty_before"]); value["rollback_available"] = bool(value["rollback_available"])
    try: value["plan"] = json.loads(value.pop("plan_json") or "{}")
    except ValueError: value["plan"] = {}
    return value


def get_deployment(deployment_id: str, *, path: Path | str | None = None) -> dict[str, Any] | None:
    initialize(path)
    with _connect(path) as conn: row = conn.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    return _deployment_row(row)


def list_deployments(*, limit: int = 50, path: Path | str | None = None) -> list[dict[str, Any]]:
    initialize(path)
    with _connect(path) as conn: rows = conn.execute("SELECT * FROM deployments ORDER BY requested_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
    return [_deployment_row(row) for row in rows if row]


def get_full_recovery_state(*, path: Path | str | None = None) -> dict[str, Any] | None:
    initialize(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM full_recovery_state WHERE singleton=1").fetchone()
    if not row:
        return None
    value = dict(row)
    for key in ("expected_device_json", "observed_device_json", "retention_deletions_json"):
        try:
            value[key.removesuffix("_json")] = json.loads(value.pop(key) or ("[]" if key.startswith("retention") else "{}"))
        except ValueError:
            value[key.removesuffix("_json")] = [] if key.startswith("retention") else {}
    return value


def set_full_recovery_state(value: dict[str, Any], *, path: Path | str | None = None) -> None:
    """Persist only the public, redacted recovery operational state."""
    initialize(path)
    current = get_full_recovery_state(path=path) or {}
    merged = {"phase": "setup", "status": "not_initialized", "backup_due": 0, **current, **redact(value), "updated_at": utc_now()}
    columns = ("last_attempt_at", "last_created_at", "last_transport_verified_at", "last_restore_tested_at", "last_restore_test_backup_id", "restore_test_type", "restore_test_result", "restore_test_duration_seconds", "last_mirror_verified_at", "backup_due", "due_since", "next_retry_at", "phase", "status", "error_code", "message", "backup_id", "mirror_backup_id", "expected_device", "observed_device", "source_bytes", "ciphertext_bytes", "runtime_seconds", "verification_state", "recipient_fingerprint", "file_count", "category_count", "free_space_before", "free_space_after", "primary_media_uuid", "primary_media_status", "offline_mirror_uuid", "offline_mirror_status", "mirror_hash_status", "retention_deletions", "source_commit", "updated_at")
    values = [merged.get(column) for column in columns]
    values[columns.index("expected_device")] = _json(merged.get("expected_device") or {})
    values[columns.index("observed_device")] = _json(merged.get("observed_device") or {})
    values[columns.index("retention_deletions")] = _json(merged.get("retention_deletions") or [])
    database_columns = [column + "_json" if column in {"expected_device", "observed_device", "retention_deletions"} else column for column in columns]
    assignments = ",".join(f"{column}=excluded.{column}" for column in database_columns)
    with _connect(path) as conn:
        conn.execute(f"INSERT INTO full_recovery_state(singleton,{','.join(database_columns)}) VALUES (1,{','.join('?' for _ in values)}) ON CONFLICT(singleton) DO UPDATE SET {assignments}", values)


def reserve_job_action(value: dict[str, Any], *, path: Path | str | None = None) -> tuple[dict[str, Any], bool]:
    """Atomically reserve one action request before it can affect a job."""
    initialize(path)
    record = redact(value)
    with _connect(path) as conn:
        existing = conn.execute("SELECT * FROM job_action_audit WHERE dedupe_key=?", (record["dedupe_key"],)).fetchone()
        if existing:
            return dict(existing), False
        conn.execute(
            "INSERT INTO job_action_audit(action_id,dedupe_key,job_id,action,requested_at,started_at,finished_at,actor_role,actor_type,policy_id,trigger_event_id,incident_id,attempt,budget_json,result,error_code,message,correlation_key,resulting_state_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["action_id"], record["dedupe_key"], record["job_id"], record["action"], record["requested_at"], record.get("started_at"), record.get("finished_at"), record["actor_role"], record.get("actor_type", "operator"), record.get("policy_id"), record.get("trigger_event_id"), record.get("incident_id"), record.get("attempt"), _json(record.get("budget") or {}), record["result"], record.get("error_code"), record.get("message"), record.get("correlation_key"), _json(record.get("resulting_state") or {})),
        )
    return record, True


def update_job_action(value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    """Finish an already reserved action without changing its dedupe identity."""
    initialize(path)
    record = redact(value)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE job_action_audit SET started_at=?,finished_at=?,result=?,error_code=?,message=?,correlation_key=?,resulting_state_json=? WHERE action_id=?",
            (record.get("started_at"), record.get("finished_at"), record["result"], record.get("error_code"), record.get("message"), record.get("correlation_key"), _json(record.get("resulting_state") or {}), record["action_id"]),
        )
    return record


def record_job_action(value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    """Compatibility helper for callers that only need to insert an audit row."""
    return reserve_job_action(value, path=path)[0]


def list_job_actions(*, job_id: str | None = None, limit: int = 50, path: Path | str | None = None) -> list[dict[str, Any]]:
    initialize(path)
    query = "SELECT * FROM job_action_audit"
    params: list[Any] = []
    if job_id:
        query += " WHERE job_id=?"; params.append(job_id)
    query += " ORDER BY requested_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 200)))
    with _connect(path) as conn:
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    for row in rows:
        try:
            row["resulting_state"] = json.loads(row.pop("resulting_state_json") or "{}")
        except ValueError:
            row["resulting_state"] = {}
        try:
            row["budget"] = json.loads(row.pop("budget_json") or "{}")
        except ValueError:
            row["budget"] = {}
    return rows


def self_healing_state(policy_id: str, *, path: Path | str | None = None) -> dict[str, Any]:
    initialize(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM self_healing_policy_state WHERE policy_id=?", (policy_id,)).fetchone()
    if not row:
        return {"policy_id": policy_id, "enabled": True, "attempts": [], "manual_intervention_required": False}
    value = dict(row)
    try: value["attempts"] = json.loads(value.pop("attempts_json") or "[]")
    except ValueError: value["attempts"] = []
    value["enabled"] = bool(value["enabled"]); value["manual_intervention_required"] = bool(value["manual_intervention_required"])
    return value


def set_self_healing_state(policy_id: str, value: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    current = self_healing_state(policy_id, path=path)
    merged = {**current, **redact(value), "policy_id": policy_id, "updated_at": utc_now()}
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO self_healing_policy_state(policy_id,enabled,attempts_json,next_allowed_at,last_attempt_at,last_result,manual_intervention_required,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(policy_id) DO UPDATE SET enabled=excluded.enabled,attempts_json=excluded.attempts_json,next_allowed_at=excluded.next_allowed_at,last_attempt_at=excluded.last_attempt_at,last_result=excluded.last_result,manual_intervention_required=excluded.manual_intervention_required,updated_at=excluded.updated_at",
            (policy_id, int(bool(merged.get("enabled", True))), _json(merged.get("attempts") or []), merged.get("next_allowed_at"), merged.get("last_attempt_at"), merged.get("last_result"), int(bool(merged.get("manual_intervention_required"))), merged["updated_at"]),
        )
    return self_healing_state(policy_id, path=path)


def _incident_title(component_id: str, error_class: str | None, message: str) -> str:
    component = {"endurance-intervals": "Endurance → Intervals", "daily-sqlite-backup": "Daily backup"}.get(component_id, component_id)
    return f"{component}: {error_class}" if error_class else f"{component}: {message}"


def record_event(event: dict[str, Any], *, path: Path | str | None = None) -> dict[str, Any]:
    """Persist one normalized fact and correlate it with an open incident.

    The caller supplies an idempotency key; identical source observations never
    create a second event. A recovery closes the currently open episode only.
    """
    initialize(path)
    observed_at = str(event.get("observed_at") or utc_now())
    occurred_at = str(event.get("occurred_at") or observed_at)
    key = str(event["idempotency_key"])
    correlation = str(event["correlation_key"])
    recovery = bool(event.get("is_recovery"))
    incident_eligible = bool(event.get("incident_eligible", True))
    severity = str(event.get("severity") or "warning")
    component = str(event["component_id"])
    error_class = str(event.get("error_class") or "") or None
    details = redact(event.get("details") or {})
    affected = max(0, int(event.get("affected_object_count") or 0))
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"liva-control-center:event:{key}"))
    with _connect(path) as conn:
        existing = conn.execute("SELECT event_id,incident_id FROM operational_events WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return {"inserted": False, "event_id": existing["event_id"], "incident_id": existing["incident_id"]}
        open_incident = conn.execute("SELECT * FROM incidents WHERE correlation_key=? AND status='open' ORDER BY first_seen DESC LIMIT 1", (correlation,)).fetchone()
        incident_id = open_incident["incident_id"] if open_incident else None
        if not incident_eligible:
            # Operator actions are observable facts, not incidents by themselves.
            incident_id = None
        elif recovery:
            if open_incident:
                conn.execute("UPDATE incidents SET status='resolved',last_seen=?,resolved_at=?,last_event_id=? WHERE incident_id=?", (occurred_at, occurred_at, event_id, incident_id))
        else:
            if not open_incident:
                incident_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"liva-control-center:incident:{correlation}:{event_id}"))
                conn.execute(
                    "INSERT INTO incidents(incident_id,correlation_key,component_id,title,status,severity,first_seen,last_seen,occurrence_count,affected_object_count,last_event_id) VALUES (?,?,?,?, 'open', ?,?,?,1,?,?)",
                    (incident_id, correlation, component, _incident_title(component, error_class, str(event.get("message") or "Operational event")), severity, occurred_at, occurred_at, affected, event_id),
                )
            else:
                highest = "critical" if "critical" in {open_incident["severity"], severity} else "warning" if "warning" in {open_incident["severity"], severity} else "info"
                conn.execute("UPDATE incidents SET severity=?,last_seen=?,occurrence_count=occurrence_count+1,affected_object_count=MAX(affected_object_count,?),last_event_id=? WHERE incident_id=?", (highest, occurred_at, affected, event_id, incident_id))
        conn.execute(
            "INSERT INTO operational_events(event_id,idempotency_key,incident_id,component_id,source,event_type,severity,occurred_at,observed_at,message,error_class,correlation_key,details_json,object_ref,is_recovery) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, key, incident_id, component, str(event.get("source") or "collector"), str(event.get("event_type") or "state_change"), severity, occurred_at, observed_at, str(redact(str(event.get("message") or "Operational event"))), error_class, correlation, _json(details), event.get("object_ref"), int(recovery)),
        )
    return {"inserted": True, "event_id": event_id, "incident_id": incident_id}


def apply_retention(*, path: Path | str | None = None, now: datetime | None = None) -> None:
    initialize(path)
    current = now or datetime.now(timezone.utc)
    events_before = (current - timedelta(days=EVENT_RETENTION_DAYS)).isoformat().replace("+00:00", "Z")
    incidents_before = (current - timedelta(days=RESOLVED_INCIDENT_RETENTION_DAYS)).isoformat().replace("+00:00", "Z")
    with _connect(path) as conn:
        conn.execute("DELETE FROM operational_events WHERE occurred_at < ? AND (incident_id IS NULL OR incident_id IN (SELECT incident_id FROM incidents WHERE status='resolved' AND resolved_at < ?))", (events_before, incidents_before))
        conn.execute("DELETE FROM incidents WHERE status='resolved' AND resolved_at < ?", (incidents_before,))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in ("details_json",):
        if key in value:
            try: value["details"] = json.loads(value.pop(key) or "{}")
            except ValueError: value["details"] = {}
    return value


def list_incidents(*, status: str | None = None, severity: str | None = None, component: str | None = None, limit: int = 100, path: Path | str | None = None) -> list[dict[str, Any]]:
    initialize(path)
    clauses, values = [], []
    for column, value in (("status", status), ("severity", severity), ("component_id", component)):
        if value:
            clauses.append(f"{column}=?"); values.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, min(int(limit), 200)))
    with _connect(path) as conn:
        rows = conn.execute(f"SELECT * FROM incidents {where} ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,last_seen DESC LIMIT ?", values).fetchall()
    return [dict(row) for row in rows]


def get_incident(incident_id: str, *, path: Path | str | None = None) -> dict[str, Any] | None:
    initialize(path)
    with _connect(path) as conn:
        incident = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if not incident: return None
        events = conn.execute("SELECT * FROM operational_events WHERE incident_id=? ORDER BY occurred_at ASC LIMIT 200", (incident_id,)).fetchall()
    return {**dict(incident), "events": [_row(row) for row in events]}


def list_events(*, component: str | None = None, from_at: str | None = None, to_at: str | None = None, limit: int = 100, path: Path | str | None = None) -> list[dict[str, Any]]:
    initialize(path)
    clauses, values = ["(? IS NULL OR component_id=?)"], [component, component]
    if from_at:
        clauses.append("occurred_at>=?"); values.append(from_at)
    if to_at:
        clauses.append("occurred_at<=?"); values.append(to_at)
    values.append(max(1, min(int(limit), 200)))
    with _connect(path) as conn:
        rows = conn.execute(f"SELECT * FROM operational_events WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC LIMIT ?", values).fetchall()
    return [_row(row) for row in rows]
