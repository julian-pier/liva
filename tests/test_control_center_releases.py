from __future__ import annotations

from pathlib import Path

import pytest

from control_center import releases, store


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "control.sqlite3")


def _repo(*, dirty=False, branch="fix/unified-training-progress", target="b" * 40):
    return {"ok": True, "branch": branch, "commit": "a" * 40, "commit_short": "a" * 12, "dirty": dirty, "dirty_paths": ["safe/path.py"] if dirty else [], "dirty_count": int(dirty), "target_commit": target, "target_short": target[:12] if target else None, "allowed_branch": "fix/unified-training-progress", "target_available": bool(target)}


def _green(monkeypatch, *, dirty=False):
    monkeypatch.setattr(releases, "repo_status", lambda: _repo(dirty=dirty))
    monkeypatch.setattr(releases, "_db_gate", lambda: True)
    monkeypatch.setattr(releases, "_backup_gate", lambda: {"ok": True, "reason": "fresh_transport_verified"})
    monkeypatch.setattr(releases, "_critical_incident_gate", lambda: {"ok": True, "blocking_incidents": []})
    monkeypatch.setattr(releases.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 9 * 1024**3})())
    monkeypatch.setattr(releases.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(releases.Path, "exists", lambda self: False if str(self) == "/var/lib/liva-recovery" else True)


def test_dirty_tree_blocks_preflight_without_any_git_mutation(monkeypatch):
    _green(monkeypatch, dirty=True)
    result = releases.preflight()
    assert result["ok"] is False
    assert any(item["reason"] == "dirty_working_tree" for item in result["checks"])


def test_plan_is_deterministic_and_marks_migrations_rollback_unsafe(monkeypatch):
    _green(monkeypatch)
    monkeypatch.setattr(releases, "_git", lambda *args: "database/migrations/009.sql\nliva_mcp/x.py" if args[:2] == ("diff", "--name-only") else None)
    plan = releases.plan(actor_role="master")
    assert plan["status"] == "planned"
    assert plan["migration_status"] == "manual_required"
    assert plan["rollback_available"] is False
    assert plan["plan"]["services"] == ["liva.service", "liva-mcp-remote.service"]


def test_duplicate_deployment_lock_blocks_execution(monkeypatch):
    _green(monkeypatch)
    plan = releases.plan(actor_role="master")
    store.set_state(releases.LOCK_KEY, {"deployment_id": "other"})
    with pytest.raises(RuntimeError): releases.execute(plan["deployment_id"])


def test_execute_simulation_restarts_only_planned_services_and_verifies(monkeypatch):
    _green(monkeypatch)
    monkeypatch.setattr(releases, "_git", lambda *args: "" if args[:2] == ("diff", "--name-only") else "b" * 40 if args[:2] == ("rev-parse", "HEAD") else None)
    plan = releases.plan(actor_role="master")
    calls = []
    monkeypatch.setattr(releases, "_run", lambda args, **_: calls.append(args) or (True, ""))
    monkeypatch.setattr(releases, "_health", lambda: True)
    result = releases.execute(plan["deployment_id"])
    assert result["status"] == "succeeded"
    assert [args for args in calls if any(str(value).endswith("liva-release-action") for value in args)] == [["sudo", "-n", "/usr/local/sbin/liva-release-action", "restart", "liva.service"]]


def test_unsafe_rollback_is_never_available(monkeypatch):
    _green(monkeypatch)
    monkeypatch.setattr(releases, "_git", lambda *args: "database/migrations/009.sql" if args[:2] == ("diff", "--name-only") else None)
    plan = releases.plan(actor_role="master")
    with pytest.raises(RuntimeError): releases.execute(plan["deployment_id"], rollback=True)


def test_missing_remote_target_keeps_preflight_green_but_blocks_deploy(monkeypatch):
    _green(monkeypatch)
    monkeypatch.setattr(releases, "repo_status", lambda: {**_repo(target=None), "target_commit": None, "target_available": False, "target_short": None})
    plan = releases.plan(actor_role="master")
    assert plan["preflight_status"] == "passed"
    assert plan["plan"]["no_update_available"] is True
    with pytest.raises(RuntimeError): releases.execute(plan["deployment_id"])
