from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

from security.private_access import get_current_role, require_role, verify_csrf

from .health import build_cockpit
from .logs import read_logs
from .store import get_incident, list_events, list_incidents
from . import jobs as job_center
from . import self_healing
from . import config as technical_config
from . import notifications
from . import releases
from . import store
from . import emergency


control_center_api = Blueprint("control_center_api", __name__, url_prefix="/api/control-center")


@control_center_api.get("/summary")
@require_role("key")
def summary():
    return jsonify(build_cockpit())


@control_center_api.get("/health")
@require_role("key")
def health_checks():
    cockpit = build_cockpit()
    return jsonify(ok=True, generated_at=cockpit["generated_at"], status=cockpit["status"], checks=cockpit["checks"], actions=[])


@control_center_api.get("/jobs")
@require_role("key")
def jobs():
    return jsonify(ok=True, generated_at=job_center.utc_now_iso(), jobs=job_center.snapshots())


@control_center_api.get("/jobs/<job_id>")
@require_role("key")
def job_detail(job_id: str):
    value = job_center.snapshot(job_id)
    if value is None:
        abort(404)
    return jsonify(ok=True, job=value)


@control_center_api.get("/jobs/<job_id>/history")
@require_role("key")
def job_history(job_id: str):
    try:
        return jsonify(ok=True, history=job_center.history(job_id, _limit(50)))
    except KeyError:
        abort(404)


@control_center_api.get("/job-actions")
@require_role("key")
def job_actions():
    return jsonify(ok=True, actions=job_center.store.list_job_actions(job_id=request.args.get("job_id"), limit=_limit(50)))


def _job_action(job_id: str, action_name: str):
    if not verify_csrf():
        return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    try:
        result = job_center.action(job_id, action_name, request_id=str(payload.get("request_id") or ""), confirmed=bool(payload.get("confirmed")), actor_role=get_current_role())
    except KeyError:
        abort(404)
    except ValueError as exc:
        return jsonify(ok=False, error="confirmation_required", detail=str(exc)), 400
    return jsonify(ok=True, **result)


@control_center_api.post("/jobs/<job_id>/actions/<action_name>")
@require_role("master")
def mutate_job(job_id: str, action_name: str):
    return _job_action(job_id, action_name)


@control_center_api.post("/services/restart-all")
@require_role("master")
def restart_all_services():
    """Restart the core LIVA service through the existing allowlisted broker."""
    if not verify_csrf():
        return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmed") is not True:
        return jsonify(ok=False, error="confirmation_required"), 400
    try:
        mcp = emergency.restart("mcp", actor_role=get_current_role(), actor_type="control_center_master")
        liva = emergency.restart("liva", actor_role=get_current_role(), actor_type="control_center_master")
    except RuntimeError as exc:
        return jsonify(ok=False, error="restart_unavailable", detail=str(exc)), 409
    return jsonify(ok=True, restarts={"mcp": mcp, "liva": liva}, message="Core services restart requested."), 202


@control_center_api.get("/self-healing")
@require_role("key")
def self_healing_status():
    state = self_healing.global_state()
    policies = self_healing.policy_views()
    recent = [item for item in job_center.store.list_job_actions(limit=100) if item.get("actor_type") == "self_healing"]
    return jsonify(ok=True, state=state, policies=policies, automatic_actions_24h=recent[:50])


@control_center_api.get("/self-healing/policies")
@require_role("key")
def self_healing_policies():
    return jsonify(ok=True, policies=self_healing.policy_views())


@control_center_api.get("/self-healing/history")
@require_role("key")
def self_healing_history():
    return jsonify(ok=True, actions=[item for item in job_center.store.list_job_actions(limit=_limit(100)) if item.get("actor_type") == "self_healing"])


def _set_self_healing(enabled: bool):
    if not verify_csrf():
        return jsonify(ok=False, error="csrf_invalid"), 403
    return jsonify(ok=True, state=self_healing.set_global_enabled(enabled))


@control_center_api.post("/self-healing/enable")
@require_role("master")
def enable_self_healing():
    return _set_self_healing(True)


@control_center_api.post("/self-healing/disable")
@require_role("master")
def disable_self_healing():
    return _set_self_healing(False)


@control_center_api.get("/config")
@require_role("key")
def config_list():
    return jsonify(ok=True, configs=technical_config.views())


@control_center_api.get("/config/<config_id>")
@require_role("key")
def config_detail(config_id: str):
    value = technical_config.view(config_id)
    if value is None: abort(404)
    return jsonify(ok=True, config=value)


@control_center_api.post("/config/<config_id>")
@require_role("master")
def config_mutate(config_id: str):
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    try: value = technical_config.mutate(config_id, payload.get("value"), actor_role=get_current_role())
    except KeyError: abort(404)
    except PermissionError: return jsonify(ok=False, error="config_read_only"), 403
    except ValueError as exc: return jsonify(ok=False, error="validation_failed", detail=str(exc)), 400
    return jsonify(ok=True, config=value)


@control_center_api.post("/config/<config_id>/apply")
@require_role("master")
def config_apply(config_id: str):
    # 009 has only immediate, validated apply semantics; no covert restart path.
    return jsonify(ok=False, error="apply_not_required"), 409


@control_center_api.get("/notifications")
@require_role("key")
def notification_status():
    return jsonify(ok=True, **notifications.status())


@control_center_api.get("/notifications/history")
@require_role("key")
def notification_history():
    return jsonify(ok=True, deliveries=job_center.store.list_notification_deliveries(limit=_limit(50)))


@control_center_api.post("/notifications/test")
@require_role("master")
def notification_test():
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    try: return jsonify(ok=True, **notifications.send_test())
    except ValueError as exc: return jsonify(ok=False, error="test_unavailable", detail=str(exc)), 409


@control_center_api.post("/notifications/provider")
@require_role("master")
def notification_provider():
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    try: value = technical_config.mutate("notification-provider", payload.get("provider"), actor_role=get_current_role())
    except ValueError as exc: return jsonify(ok=False, error="provider_unavailable", detail=str(exc)), 409
    return jsonify(ok=True, config=value)


@control_center_api.get("/releases/status")
@require_role("key")
def release_status():
    return jsonify(ok=True, **releases.status())


@control_center_api.get("/releases")
@require_role("key")
def release_list():
    return jsonify(ok=True, deployments=job_center.store.list_deployments(limit=_limit(50)))


@control_center_api.get("/releases/<deployment_id>")
@require_role("key")
def release_detail(deployment_id: str):
    value = job_center.store.get_deployment(deployment_id)
    if value is None: abort(404)
    return jsonify(ok=True, deployment=value)


@control_center_api.post("/releases/plan")
@require_role("master")
def release_plan():
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    return jsonify(ok=True, deployment=releases.plan(actor_role=get_current_role()))


def _start_release() -> None:
    import subprocess
    subprocess.run(["sudo", "-n", "/usr/local/sbin/liva-release-action", "start"], check=True, capture_output=True, text=True, timeout=15)


@control_center_api.post("/releases/deploy")
@require_role("master")
def release_deploy():
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    deployment_id = str(payload.get("deployment_id") or "")
    record = job_center.store.get_deployment(deployment_id)
    if not record: abort(404)
    if record["status"] != "planned" or not bool(payload.get("confirmed")) or record.get("plan", {}).get("no_update_available"): return jsonify(ok=False, error="deployment_confirmation_required"), 409
    try: _start_release()
    except Exception: return jsonify(ok=False, error="release_executor_unavailable"), 503
    return jsonify(ok=True, deployment_id=deployment_id, status="started")


@control_center_api.post("/releases/<deployment_id>/rollback")
@require_role("master")
def release_rollback(deployment_id: str):
    if not verify_csrf(): return jsonify(ok=False, error="csrf_invalid"), 403
    payload = request.get_json(silent=True) or {}
    record = job_center.store.get_deployment(deployment_id)
    if not record: abort(404)
    if not record.get("rollback_available") or not bool(payload.get("confirmed")): return jsonify(ok=False, error="rollback_unavailable"), 409
    job_center.store.update_deployment(deployment_id, {"status": "rolling_back", "requested_at": store.utc_now()})
    try: _start_release()
    except Exception: return jsonify(ok=False, error="release_executor_unavailable"), 503
    return jsonify(ok=True, deployment_id=deployment_id, status="rolling_back")


def _limit(default: int = 100) -> int:
    try:
        return max(1, min(int(request.args.get("limit", default)), 200))
    except (TypeError, ValueError):
        return default


@control_center_api.get("/incidents")
@require_role("key")
def incidents():
    return jsonify(ok=True, incidents=list_incidents(status=request.args.get("status"), severity=request.args.get("severity"), component=request.args.get("component"), limit=_limit()))


@control_center_api.get("/incidents/<incident_id>")
@require_role("key")
def incident_detail(incident_id: str):
    incident = get_incident(incident_id)
    if incident is None:
        abort(404)
    return jsonify(ok=True, incident=incident)


@control_center_api.get("/events")
@require_role("key")
def events():
    return jsonify(ok=True, events=list_events(component=request.args.get("component"), from_at=request.args.get("from"), to_at=request.args.get("to"), limit=_limit()))


@control_center_api.get("/logs")
@require_role("key")
def logs():
    return jsonify(read_logs(component=request.args.get("component"), limit=_limit(), from_at=request.args.get("from"), to_at=request.args.get("to"), search=request.args.get("search")))
