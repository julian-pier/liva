from __future__ import annotations

import subprocess

import pytest


def test_registry_is_explicit_and_endurance_retry_is_not_allowlisted() -> None:
    from control_center.jobs import REGISTRY

    assert "full-recovery" in REGISTRY
    assert "endurance-intervals" in REGISTRY
    assert REGISTRY["endurance-intervals"].retry is False
    assert "missing_execution_pace" in str(REGISTRY["endurance-intervals"].retry_reason)


def test_unknown_job_and_action_are_never_mutable() -> None:
    from control_center import jobs

    with pytest.raises(KeyError):
        jobs.action("not-a-job", "run", request_id="x", confirmed=True, actor_role="master")
    with pytest.raises(KeyError):
        jobs.action("full-recovery", "restart", request_id="x", confirmed=True, actor_role="master")


def test_run_does_not_start_an_already_active_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from control_center import jobs

    monkeypatch.setattr(jobs, "snapshot", lambda _: {"job_id": "full-recovery", "active": True})
    saved = []
    monkeypatch.setattr(jobs.store, "reserve_job_action", lambda value: (value, True))
    monkeypatch.setattr(jobs.store, "update_job_action", lambda value: saved.append(value) or value)
    monkeypatch.setattr(jobs.store, "record_event", lambda value: None)
    result = jobs.action("full-recovery", "run", request_id="double-click", confirmed=True, actor_role="master")

    assert result["audit"]["result"] == "failed"
    assert "already running" in result["audit"]["message"]
    assert len(saved) == 1


def test_duplicate_request_is_reserved_before_any_job_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from control_center import jobs

    existing = {"action_id": "prior", "result": "requested"}
    monkeypatch.setattr(jobs.store, "reserve_job_action", lambda value: (existing, False))
    monkeypatch.setattr(jobs, "_execute", lambda *_: pytest.fail("must not run twice"))
    result = jobs.action("control-center-collector", "run", request_id="same", confirmed=True, actor_role="master")

    assert result["deduplicated"] is True


def test_core_restart_records_dispatched_when_systemd_terminates_its_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd may terminate the caller before the new service is active."""
    from control_center import jobs

    events = []
    monkeypatch.setattr(jobs.store, "reserve_job_action", lambda value: (value, True))
    monkeypatch.setattr(jobs.store, "update_job_action", lambda value: value)
    monkeypatch.setattr(jobs.store, "record_event", lambda value: events.append(value))
    monkeypatch.setattr(jobs, "snapshot", lambda _job_id: {})
    monkeypatch.setattr(jobs, "_execute", lambda *_args: (_ for _ in ()).throw(subprocess.CalledProcessError(-15, ["liva-job-action"])))
    monkeypatch.setattr(jobs, "_restart_target_is_active", lambda _job: False, raising=False)

    result = jobs.action("core-liva", "restart", request_id="restart-own-service", confirmed=True, actor_role="master", actor_type="control_center_master")

    assert result["audit"]["result"] == "dispatched"
    assert events[0]["severity"] == "info"


def test_core_restart_handoff_is_confirmed_after_the_new_service_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    from control_center import jobs

    audit = {"job_id": "core-liva", "action": "restart", "result": "dispatched", "error_code": None, "message": "Restart handed to systemd; confirmation follows after service startup."}
    saved = []
    monkeypatch.setattr(jobs.store, "list_job_actions", lambda **_kwargs: [audit])
    monkeypatch.setattr(jobs.store, "update_job_action", lambda value: saved.append(value) or value)
    monkeypatch.setattr(jobs, "_restart_target_is_active", lambda _job: True)

    jobs._reconcile_core_restart_audits()

    assert saved[0]["result"] == "success"
    assert saved[0]["error_code"] is None


def test_job_mutation_requires_master_csrf_and_never_accepts_client_risk_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import app
    import control_center.api as api

    monkeypatch.setattr(api.job_center, "action", lambda *args, **kwargs: {"audit": {"result": "success"}, "job": {}})
    app.config.update(TESTING=True, SECRET_KEY="test-control-center-jobs")
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = "203.0.113.2"

    assert client.post("/api/control-center/jobs/control-center-collector/actions/run", json={"confirmed": True}).status_code in {401, 403}
    with client.session_transaction() as session:
        session["role"] = "key"; session["csrf_token"] = "csrf"
    assert client.post("/api/control-center/jobs/control-center-collector/actions/run", json={"confirmed": True, "request_id": "one", "risk": "low"}, headers={"X-CSRF-Token": "csrf"}).status_code in {401, 403}
    with client.session_transaction() as session:
        session["role"] = "master"; session["csrf_token"] = "csrf"
    assert client.post("/api/control-center/jobs/control-center-collector/actions/run", json={"confirmed": True, "request_id": "one", "risk": "low"}, headers={"X-CSRF-Token": "csrf"}).status_code == 200
