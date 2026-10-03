"""Deterministic, local-only release planning and execution for LIVA."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database import connections

from . import jobs, notifications, self_healing, store

REPO = Path("/opt/liva")
ALLOWED_BRANCH = (os.getenv("LIVA_RELEASE_BRANCH") or "fix/unified-training-progress").strip()
LOCK_KEY = "release:in_progress"
MAX_BACKUP_AGE = timedelta(hours=36)
CORE_SERVICES = {"liva.service", "liva-mcp-remote.service"}


def _run(args: list[str], *, timeout: int = 20) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode == 0, (result.stdout or result.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return False, "command_unavailable"


def _git(*args: str) -> str | None:
    ok, output = _run(["git", *args])
    return output if ok else None


def _short(value: str | None) -> str | None:
    return value[:12] if value else None


def repo_status() -> dict[str, Any]:
    if not (REPO / ".git").exists(): return {"ok": False, "error": "repo_missing"}
    branch = _git("branch", "--show-current")
    commit = _git("rev-parse", "HEAD")
    dirty_output = _git("status", "--porcelain=v1")
    remote_target = _git("rev-parse", f"origin/{ALLOWED_BRANCH}")
    return {"ok": bool(branch and commit), "branch": branch, "commit": commit, "commit_short": _short(commit), "dirty": bool(dirty_output), "dirty_paths": [line[3:] for line in (dirty_output or "").splitlines()[:30]], "dirty_count": len((dirty_output or "").splitlines()), "target_commit": remote_target, "target_short": _short(remote_target), "allowed_branch": ALLOWED_BRANCH, "target_available": bool(remote_target)}


def _backup_gate() -> dict[str, Any]:
    state = store.get_full_recovery_state() or {}
    stamp = state.get("last_transport_verified_at")
    try: fresh = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= datetime.now(timezone.utc) - MAX_BACKUP_AGE
    except ValueError: fresh = False
    return {"ok": bool(fresh and state.get("verification_state") == "transport_verified"), "last_transport_verified_at": stamp, "backup_id": state.get("backup_id"), "reason": "fresh_transport_verified" if fresh else "fresh_transport_verified_backup_required"}


def _critical_incident_gate() -> dict[str, Any]:
    blocked = [item for item in store.list_incidents(status="open", severity="critical", limit=100) if item.get("component_id") in {"full-recovery", "daily-sqlite-backup", "liva", "liva-mcp-remote"}]
    return {"ok": not blocked, "blocking_incidents": [item["incident_id"] for item in blocked]}


def _db_gate() -> bool:
    for name in (connections.TRAINING_DB, connections.CORE_DB, connections.CONTROL_CENTER_DB):
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{name}?mode=ro", uri=True, timeout=2); conn.execute("SELECT 1"); conn.close()
        except Exception: return False
    return True


def deployment_in_progress() -> bool:
    state = store.get_state(LOCK_KEY)
    return bool(isinstance(state, dict) and state.get("deployment_id"))


def preflight(*, target: str | None = None) -> dict[str, Any]:
    repo = repo_status(); checks: list[dict[str, Any]] = []
    def check(name: str, ok: bool, reason: str) -> None: checks.append({"name": name, "ok": bool(ok), "reason": reason})
    check("deployment_lock", not deployment_in_progress(), "no_active_deployment")
    check("repository", bool(repo.get("ok")), repo.get("error") or "available")
    check("branch", repo.get("branch") == ALLOWED_BRANCH, "expected_branch" if repo.get("branch") == ALLOWED_BRANCH else "unexpected_branch")
    check("working_tree", not repo.get("dirty"), "clean" if not repo.get("dirty") else "dirty_working_tree")
    selected = target or repo.get("target_commit")
    # A missing local remote-ref means there is no release candidate, not that
    # the currently running product is unhealthy. Deploy remains impossible.
    check("target", selected is None or selected == repo.get("target_commit"), "no_target_available" if selected is None else "allowed_target" if selected == repo.get("target_commit") else "target_not_allowed")
    from .emergency import storage_status
    root_storage = storage_status()
    check("disk", root_storage["state"] != "critical", root_storage["state"])
    check("databases", _db_gate(), "sqlite_reachable")
    check("backup_gate", _backup_gate()["ok"], _backup_gate()["reason"])
    check("restore", not Path("/var/lib/liva-recovery").exists() or not any(Path("/var/lib/liva-recovery").glob("restore-*")), "no_active_restore")
    check("critical_incidents", _critical_incident_gate()["ok"], "no_blocking_incidents")
    check("python", (REPO / ".venv/bin/python").is_file(), "venv_available")
    check("systemd", shutil.which("systemctl") is not None, "systemd_available")
    return {"ok": all(item["ok"] for item in checks), "checks": checks, "repo": repo, "backup_gate": _backup_gate(), "critical_incidents": _critical_incident_gate()}


def plan(*, actor_role: str) -> dict[str, Any]:
    repo = repo_status(); target = repo.get("target_commit")
    pre = preflight(target=target)
    changed = []
    if target and repo.get("commit"):
        diff = _git("diff", "--name-only", f"{repo['commit']}..{target}") or ""
        changed = diff.splitlines()[:500]
    migrations = [path for path in changed if path.startswith("database/migrations/")]
    dependency_change = any(Path(path).name in {"pyproject.toml", "requirements.txt", "package-lock.json", "poetry.lock"} for path in changed)
    services = ["liva.service"] if changed else []
    if any(path.startswith("liva_mcp/") for path in changed): services.append("liva-mcp-remote.service")
    rollback_available = not migrations and not dependency_change and bool(repo.get("commit"))
    value = {"deployment_id": uuid.uuid4().hex, "requested_at": store.utc_now(), "actor_role": actor_role, "source": "origin", "commit_before": repo.get("commit"), "commit_target": target, "branch": repo.get("branch"), "dirty_before": bool(repo.get("dirty")), "status": "planned" if pre["ok"] else "preflight_failed", "preflight_status": "passed" if pre["ok"] else "failed", "migration_status": "none" if not migrations else "manual_required", "rollback_available": rollback_available, "rollback_target": repo.get("commit") if rollback_available else None, "plan": {"no_update_available": target is None, "changed_paths": changed, "migrations": migrations, "dependency_change": dependency_change, "services": services, "health_probes": ["liva_http", "mcp_health", "control_center", "sqlite", "jobs", "self_healing", "notifications"], "recovery": pre["backup_gate"]}}
    store.create_deployment(value)
    return store.get_deployment(value["deployment_id"]) or value


def _set_lock(deployment_id: str | None) -> None:
    store.set_state(LOCK_KEY, {"deployment_id": deployment_id, "updated_at": store.utc_now()} if deployment_id else {})


def _release_event(record: dict[str, Any], event_type: str, severity: str, message: str) -> None:
    store.record_event({"idempotency_key": f"release:{record['deployment_id']}:{event_type}", "component_id": "releases", "source": "release-executor", "event_type": event_type, "severity": severity, "message": message, "correlation_key": f"release:{record['deployment_id']}", "incident_eligible": severity == "critical", "details": {"deployment_id": record["deployment_id"], "status": record.get("status")}})


def _release_notification(record: dict[str, Any], text: str) -> None:
    notifications.deliver(provider=notifications.status()["active_provider"], dedupe_key=f"release:{record['deployment_id']}:{record['status']}", text=text)


def _restart(service: str) -> bool:
    return _run(["sudo", "-n", "/usr/local/sbin/liva-release-action", "restart", service], timeout=60)[0]


def _health() -> bool:
    try:
        from .health import build_cockpit
        from . import notifications
        return bool(_db_gate() and jobs.snapshots() and self_healing.global_state() and notifications.status() and build_cockpit())
    except Exception: return False


def execute(deployment_id: str, *, rollback: bool = False) -> dict[str, Any]:
    record = store.get_deployment(deployment_id)
    if not record: raise KeyError(deployment_id)
    if deployment_in_progress(): raise RuntimeError("deployment_in_progress")
    if rollback and not record.get("rollback_available"): raise RuntimeError("rollback_unavailable")
    target = record.get("rollback_target") if rollback else record.get("commit_target")
    if not target: raise RuntimeError("no_update_available")
    pre = preflight(target=record.get("commit_target")) if not rollback else {"ok": True}
    if not pre["ok"]:
        result = store.update_deployment(deployment_id, {"status": "preflight_failed", "preflight_status": "failed", "finished_at": store.utc_now(), "error_code": "preflight_failed", "message": "Release preflight blocked deployment."}) or record
        _release_event(result, "preflight_failed", "warning", "Release preflight blocked deployment.")
        return result
    _set_lock(deployment_id)
    try:
        store.update_deployment(deployment_id, {"status": "rolling_back" if rollback else "deploying", "started_at": store.utc_now(), "preflight_status": "passed"})
        ok, _ = _run(["git", "fetch", "--quiet", "origin", ALLOWED_BRANCH], timeout=90)
        if not ok: raise RuntimeError("git_fetch_failed")
        ok, _ = _run(["git", "checkout", "--detach", str(target)], timeout=60)
        if not ok: raise RuntimeError("git_checkout_failed")
        services = list((record.get("plan") or {}).get("services") or ["liva.service"])
        if any(service not in CORE_SERVICES for service in services): raise RuntimeError("service_not_allowlisted")
        store.update_deployment(deployment_id, {"status": "verifying", "service_restart_status": "restarting"})
        if not all(_restart(service) for service in services): raise RuntimeError("service_restart_failed")
        if not _health(): raise RuntimeError("post_deploy_health_failed")
        result = store.update_deployment(deployment_id, {"status": "rolled_back" if rollback else "succeeded", "finished_at": store.utc_now(), "commit_after": _git("rev-parse", "HEAD"), "service_restart_status": "succeeded", "post_deploy_health_status": "passed", "rollback_result": "succeeded" if rollback else None})
        final = result or record
        _release_event(final, "rollback_succeeded" if rollback else "deployment_succeeded", "info", "Release completed successfully.")
        return final
    except Exception as exc:
        final = store.update_deployment(deployment_id, {"status": "rollback_failed" if rollback else "failed", "finished_at": store.utc_now(), "error_code": type(exc).__name__, "message": str(exc), "post_deploy_health_status": "failed"}) or record
        _release_event(final, "rollback_failed" if rollback else "deployment_failed", "critical", "Release failed; inspect Control Center.")
        _release_notification(final, "LIVA Control Center: Release fehlgeschlagen. Details im Control Center.")
        return final
    finally:
        _set_lock(None)


def status() -> dict[str, Any]:
    history = store.list_deployments(limit=50)
    return {"repo": repo_status(), "in_progress": deployment_in_progress(), "last_success": next((item for item in history if item["status"] == "succeeded"), None), "last_failure": next((item for item in history if item["status"] in {"failed", "preflight_failed", "rollback_failed"}), None), "last_rollback": next((item for item in history if item["status"] in {"rolled_back", "rollback_failed"}), None), "backup_gate": _backup_gate()}
