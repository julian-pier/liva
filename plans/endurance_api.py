from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request

from .endurance_planning import EnduranceError, create_plan, get_plan, list_plans, patch_plan, preview_rebase, redo_plan, undo_plan
from integrations.endurance_intervals_sync import retry_plan_sync, run_sync_jobs


endurance_planning_api = Blueprint("endurance_planning_api", __name__, url_prefix="/api/endurance")


def _error(exc: Exception, status: int = 400):
    code = str(exc)
    if code == "endurance plan not found":
        status, code = 404, "not_found"
    elif code == "revision_conflict":
        status = 409
    elif code == "confirm_required":
        status = 409
    return jsonify({"ok": False, "error": {"code": code, "message": str(exc)}}), status


@endurance_planning_api.get("/plans")
def endurance_plans_list():
    return jsonify({"ok": True, "plans": list_plans()})


@endurance_planning_api.post("/plans")
def endurance_plan_create():
    try:
        return jsonify({"ok": True, "plan": create_plan(request.get_json(silent=True) or {})}), 201
    except (EnduranceError, TypeError, ValueError) as exc:
        return _error(exc)


@endurance_planning_api.get("/plans/<plan_id>")
def endurance_plan_detail(plan_id: str):
    try:
        return jsonify({"ok": True, "plan": get_plan(plan_id, request.args.get("from"), request.args.get("to"))})
    except EnduranceError as exc:
        return _error(exc)


@endurance_planning_api.patch("/plans/<plan_id>")
def endurance_plan_patch(plan_id: str):
    body = request.get_json(silent=True) or {}
    try:
        result = patch_plan(
            plan_id,
            list(body.get("operations") or []),
            dry_run=bool(body.get("dry_run")),
            confirm=bool(body.get("confirm")),
            expected_revision=body.get("expected_revision"),
        )
        return jsonify(result)
    except (EnduranceError, TypeError, ValueError, KeyError) as exc:
        return _error(exc)


@endurance_planning_api.post("/plans/<plan_id>/undo")
def endurance_plan_undo(plan_id: str):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(undo_plan(plan_id, expected_revision=body.get("expected_revision")))
    except (EnduranceError, TypeError, ValueError, KeyError) as exc:
        return _error(exc)


@endurance_planning_api.post("/plans/<plan_id>/redo")
def endurance_plan_redo(plan_id: str):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(redo_plan(plan_id, expected_revision=body.get("expected_revision")))
    except (EnduranceError, TypeError, ValueError, KeyError) as exc:
        return _error(exc)


@endurance_planning_api.post("/plans/<plan_id>/rebase-preview")
def endurance_plan_rebase_preview(plan_id: str):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "preview": preview_rebase(plan_id, body.get("from_date") or date.today().isoformat())})
    except (EnduranceError, ValueError) as exc:
        return _error(exc)


@endurance_planning_api.post("/plans/<plan_id>/sync/retry")
def endurance_plan_sync_retry(plan_id: str):
    try:
        get_plan(plan_id)
        queued = retry_plan_sync(plan_id)
        # A manual click is an execution request, not merely a queue mutation.
        # Process a complete large plan immediately instead of waiting up to the
        # next timer tick.
        result = run_sync_jobs(limit=500, plan_id=plan_id)
        current = get_plan(plan_id)["sync"]
        return jsonify({"ok": True, "started": True, "queued": queued, "result": result, "sync": current})
    except EnduranceError as exc:
        return _error(exc)
