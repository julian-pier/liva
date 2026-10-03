"""Small, local-only emergency administration surface.

It deliberately has no Flask dependency and accepts no free-form unit,
command, repository, or path input.  Mutations use the existing Job Center
broker so their audit and deduplication rules stay canonical.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database import connections

from . import jobs, releases, self_healing, store

OPERATION_KEY = "emergency:operation"
STALE_OPERATION_SECONDS = 600
STORAGE_DEGRADED_BYTES = 3 * 1024**3
STORAGE_CRITICAL_BYTES = 1 * 1024**3


def _systemd(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", unit, "--no-pager", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "UnitFileState"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    values = {key: value for line in result.stdout.splitlines() if "=" in line for key, value in [line.split("=", 1)]}
    return {"unit": unit, "available": result.returncode == 0, **values}


def storage_status() -> dict[str, Any]:
    free = shutil.disk_usage("/").free
    state = "critical" if free < STORAGE_CRITICAL_BYTES else "degraded" if free < STORAGE_DEGRADED_BYTES else "healthy"
    return {"state": state, "free_bytes": free, "degraded_threshold_bytes": STORAGE_DEGRADED_BYTES, "critical_threshold_bytes": STORAGE_CRITICAL_BYTES}


def db_status() -> dict[str, Any]:
    path = Path(connections.CONTROL_CENTER_DB)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        schema = [row[0] for row in conn.execute("SELECT version FROM control_center_schema ORDER BY version")]
        conn.close()
        return {"available": True, "integrity": integrity, "foreign_key_violations": len(foreign_keys), "journal_mode": journal_mode, "schema_versions": schema, "wal_present": path.with_name(path.name + "-wal").exists()}
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__}


def _recovery_status() -> dict[str, Any]:
    state = store.get_full_recovery_state() or {}
    try:
        from recovery.full_recovery import preflight
        media = preflight()
    except Exception as exc:
        media = {"error_code": type(exc).__name__}
    return {"state": {key: state.get(key) for key in ("status", "backup_id", "verification_state", "last_transport_verified_at", "last_restore_tested_at", "primary_media_uuid", "primary_media_status", "offline_mirror_uuid", "offline_mirror_status", "mirror_backup_id", "mirror_hash_status")}, "primary_preflight": {key: media.get(key) for key in ("ok", "error_code", "message", "device")}}


def status() -> dict[str, Any]:
    critical = store.list_incidents(status="open", severity="critical", limit=20)
    return {
        "ok": True,
        "services": {"liva": _systemd("liva.service"), "mcp": _systemd("liva-mcp-remote.service"), "collector": _systemd("liva-control-center-collector.timer"), "self_healing_timer": _systemd("liva-self-healing.timer"), "full_recovery_timer": _systemd("liva-full-recovery.timer"), "full_recovery_catchup_timer": _systemd("liva-full-recovery-catchup.timer")},
        "control_center_db": db_status(), "full_recovery": _recovery_status(),
        "self_healing": self_healing.global_state(), "deployment": releases.status(),
        "critical_open_incidents": [{key: item.get(key) for key in ("incident_id", "component_id", "severity", "title", "last_seen")} for item in critical],
        "root_storage": storage_status(), "operation": store.get_state(OPERATION_KEY) or {},
    }


def health_matrix() -> list[dict[str, Any]]:
    snapshot = status()
    components = {
        "LIVA": snapshot["services"]["liva"], "MCP": snapshot["services"]["mcp"],
        "Control Center": snapshot["control_center_db"], "Recovery": snapshot["full_recovery"]["state"],
        "Jobs": {"registered": len(jobs.REGISTRY)}, "Self-Healing": snapshot["self_healing"],
        "Notifications": __import__("control_center.notifications", fromlist=["status"]).status(),
        "Releases": snapshot["deployment"], "Garmin": jobs.snapshot("garmin-sync") or {},
        "Polar": jobs.snapshot("polar-sync") or {}, "WebUntis": jobs.snapshot("webuntis") or {},
        "Endurance": jobs.snapshot("endurance-intervals") or {},
    }
    return [{"component": name, "evidence": value, "action": "liva-admin status" if name in {"LIVA", "MCP", "Control Center", "Recovery", "Self-Healing", "Releases"} else "Control Center Jobs"} for name, value in components.items()]


def _operation_is_stale(value: Any) -> bool:
    if not isinstance(value, dict) or not value.get("active"):
        return False
    try:
        when = datetime.fromisoformat(str(value.get("started_at")).replace("Z", "+00:00"))
        return when < datetime.now(timezone.utc) - timedelta(seconds=STALE_OPERATION_SECONDS)
    except ValueError:
        return True


def restart(target: str, *, actor_role: str = "emergency_admin", actor_type: str = "emergency_admin") -> dict[str, Any]:
    mapping = {"liva": "core-liva", "mcp": "core-liva-mcp"}
    if target not in mapping:
        raise ValueError("target is not allowlisted")
    existing = store.get_state(OPERATION_KEY)
    if isinstance(existing, dict) and existing.get("active") and not _operation_is_stale(existing):
        raise RuntimeError("emergency_operation_in_progress")
    request_id = f"emergency:{target}:{uuid.uuid4().hex}"
    marker = {"active": True, "target": target, "started_at": store.utc_now(), "request_id": request_id}
    store.set_state(OPERATION_KEY, marker)
    try:
        result = jobs.action(mapping[target], "restart", request_id=request_id, confirmed=True, actor_role=actor_role, actor_type=actor_type)
        return result
    finally:
        store.set_state(OPERATION_KEY, {"active": False, "target": target, "finished_at": store.utc_now(), "request_id": request_id})


def set_self_healing(enabled: bool) -> dict[str, Any]:
    previous = self_healing.global_state()
    state = self_healing.set_global_enabled(enabled)
    store.record_config_audit({"config_id": "self-healing-enabled", "previous": {"effective": previous.get("enabled")}, "requested": {"value": enabled}, "actor_role": "emergency_admin", "validation": "valid", "apply_result": "applied", "resulting": {"effective": state.get("enabled")}})
    store.record_event({"idempotency_key": f"emergency:self-healing:{enabled}:{state['updated_at']}", "component_id": "self-healing", "source": "emergency-admin", "event_type": "emergency_self_healing_toggle", "severity": "info", "message": "Emergency admin changed self-healing state.", "correlation_key": "emergency:self-healing", "details": {"enabled": enabled}, "incident_eligible": False})
    return state


def release_status() -> dict[str, Any]:
    return releases.status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="liva-admin", description="LIVA emergency administration (fixed local commands only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    restart_parser = sub.add_parser("restart"); restart_parser.add_argument("target", choices=("liva", "mcp"))
    healing_parser = sub.add_parser("self-healing"); healing_parser.add_argument("state", choices=("on", "off"))
    release_parser = sub.add_parser("release"); release_parser.add_argument("action", choices=("status",))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("liva-admin must be invoked through sudo")
    if args.command == "status": value = status()
    elif args.command == "restart": value = restart(args.target)
    elif args.command == "self-healing": value = set_self_healing(args.state == "on")
    else: value = release_status()
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
