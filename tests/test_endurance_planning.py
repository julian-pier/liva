from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from flask import Flask

from database import connections
from plans.endurance_planning import EnduranceError, create_plan, ensure_endurance_schema, get_next_open_cardio_session, get_plan, list_plans, patch_plan, preview_rebase, redo_plan, undo_plan
from plans.endurance_api import endurance_planning_api


@pytest.fixture()
def endurance_db(tmp_path, monkeypatch):
    path = tmp_path / "plans.sqlite3"
    monkeypatch.setattr(connections, "PLANS_DB", str(path))
    ensure_endurance_schema()
    return path


def sample_plan():
    return {
        "title": "5K Frühjahr",
        "event": {"distance_m": 5000, "target_time_s": 1260, "event_date": "2027-04-05", "priority": "A"},
        "phases": [
            {"id": "phase-base", "name": "Base", "phase_type": "base", "start_date": "2026-11-02", "end_date": "2026-12-27"},
            {"id": "phase-threshold", "name": "Threshold", "phase_type": "threshold", "start_date": "2026-12-28", "end_date": "2027-02-07"},
        ],
        "sessions": [{
            "id": "session-interval",
            "phase_id": "phase-threshold",
            "scheduled_date": "2027-01-05",
            "session_type": "interval",
            "title": "6 × 800 m",
            "distance_m": 10000,
            "duration_s": 3600,
            "steps": [
                {"id": "warmup", "kind": "warmup", "duration_s": 900, "target": {"metric": "pace", "basis": "fitness_anchor", "zone": "easy"}},
                {"id": "repeat", "kind": "repeat", "reps": 6, "steps": [
                    {"id": "work", "kind": "work", "distance_m": 800, "target": {"metric": "pace", "basis": "fitness_anchor", "zone": "interval"}},
                    {"id": "recovery", "kind": "recovery", "distance_m": 400, "target": {"metric": "pace", "basis": "fitness_anchor", "zone": "easy"}},
                ]},
            ],
        }],
    }


def test_model_creates_stable_hierarchy(endurance_db):
    created = create_plan(sample_plan())
    assert created["event"]["goal_pace_s_per_km"] == 252
    assert [phase["id"] for phase in created["phases"]] == ["phase-base", "phase-threshold"]
    session = created["sessions"][0]
    assert session["id"] == "session-interval"
    assert {step["id"] for step in session["steps"]} == {"warmup", "repeat", "work", "recovery"}
    assert next(step for step in session["steps"] if step["id"] == "work")["parent_step_id"] == "repeat"
    assert len(session["execution_steps"]) == 12
    assert sum(step["kind"] == "work" for step in session["execution_steps"]) == 6
    assert sum(step["kind"] == "recovery" for step in session["execution_steps"]) == 5
    assert session["execution_contract"]["step_count"] == 12
    assert len(session["execution_contract"]["fingerprint"]) == 64


def test_dashboard_next_run_is_local_and_uses_session_sync_state(endurance_db):
    plan = create_plan(sample_plan())
    payload = get_next_open_cardio_session("2026-11-01")
    assert payload["source"] == "local_endurance_plan"
    assert payload["plan"]["id"] == plan["id"]
    assert payload["session"]["id"] == "session-interval"
    assert payload["sync"]["state"] == "not_synced"
    assert payload["sync"]["garmin_state"] == "waiting"


def test_missing_anchor_keeps_explicit_rpe_without_inventing_pace(endurance_db):
    payload = sample_plan()
    payload["sessions"].append({
        "id": "easy-rpe", "scheduled_date": "2026-11-10", "session_type": "easy",
        "title": "Easy", "duration_s": 1800,
        "steps": [{"id": "easy-work", "kind": "work", "duration_s": 1800, "target": {"metric": "rpe", "basis": "absolute", "min": 3, "max": 4}}],
    })
    sessions = create_plan(payload)["sessions"]
    session = next(item for item in sessions if item["id"] == "easy-rpe")
    target = session["steps"][0]["target"]
    assert target["metric"] == "rpe"
    assert target["min"] == 3
    assert target["max"] == 4
    assert (target["hr_min_pct"], target["hr_max_pct"]) == (65, 76)
    assert session["load_value"] == 105

    explicit = next(item for item in sessions if item["id"] == "session-interval")
    assert explicit["distance_m"] == 10000


def test_hill_intervals_keep_effort_target_and_add_hr_guidance(endurance_db):
    payload = sample_plan()
    session = payload["sessions"][0]
    session["title"] = "Bergintervalle · 6×60 s"
    session["steps"][1]["steps"][0]["target"] = {"metric": "rpe", "basis": "absolute", "min": 8, "max": 8.5}
    plan = create_plan(payload)
    work = next(step for step in plan["sessions"][0]["steps"] if step["id"] == "work")
    assert work["target"].get("pace_s_per_km") is None
    assert (work["target"]["hr_min_pct"], work["target"]["hr_max_pct"]) == (88, 95)


def test_new_plan_creation_can_be_undone(endurance_db):
    created = create_plan(sample_plan())
    assert created["undo_available"] is True
    result = undo_plan(created["id"], expected_revision=created["revision"])
    assert result["plan_removed"] is True
    assert result["plan"] is None
    assert list_plans() == []


def test_phase_resize_moves_neighbor_boundary_without_gap_or_overlap(endurance_db):
    plan = create_plan(sample_plan())
    original_end = plan["phases"][0]["end_date"]
    resized_end = (date.fromisoformat(original_end) + timedelta(days=7)).isoformat()
    result = patch_plan(plan["id"], [{"op": "resize_phase", "phase_id": "phase-base", "end_date": resized_end}], dry_run=True)
    assert result["dry_run"] is True
    phases = result["preview"]["phases"]
    assert phases[0]["end_date"] == resized_end
    assert phases[1]["start_date"] == (date.fromisoformat(resized_end) + timedelta(days=1)).isoformat()
    assert not ({"phase_gap", "phase_overlap"} & {item["code"] for item in result["preview"]["warnings"]})
    assert get_plan(plan["id"])["phases"][0]["end_date"] == original_end


def test_set_season_start_shifts_sessions_and_keeps_goal_boundary(endurance_db):
    plan = create_plan(sample_plan())
    result = patch_plan(plan["id"], [{"op": "set_season_start", "start_date": "2026-10-26"}], dry_run=True)
    preview = result["preview"]
    assert preview["phases"][0]["start_date"] == "2026-10-26"
    assert preview["phases"][-1]["end_date"] == preview["event"]["event_date"]
    assert preview["sessions"][0]["scheduled_date"] == "2026-12-29"
    assert get_plan(plan["id"])["phases"][0]["start_date"] == "2026-11-02"


def test_inner_phase_move_preserves_duration_and_adjusts_both_neighbors(endurance_db):
    payload = sample_plan()
    payload["phases"].insert(1, {
        "id": "phase-build", "name": "Build", "phase_type": "build",
        "start_date": "2026-12-01", "end_date": "2026-12-31",
    })
    plan = create_plan(payload)
    phases = plan["phases"]
    middle = phases[1]
    duration = (date.fromisoformat(middle["end_date"]) - date.fromisoformat(middle["start_date"])).days
    requested = (date.fromisoformat(middle["start_date"]) + timedelta(days=5)).isoformat()

    moved = patch_plan(plan["id"], [{"op": "move_phase", "phase_id": middle["id"], "start_date": requested}])["plan"]["phases"]

    assert moved[1]["start_date"] == requested
    assert (date.fromisoformat(moved[1]["end_date"]) - date.fromisoformat(moved[1]["start_date"])).days == duration
    assert date.fromisoformat(moved[1]["start_date"]) == date.fromisoformat(moved[0]["end_date"]) + timedelta(days=1)
    assert date.fromisoformat(moved[2]["start_date"]) == date.fromisoformat(moved[1]["end_date"]) + timedelta(days=1)


def test_normalize_phases_repairs_existing_structure_and_covers_goal(endurance_db):
    plan = create_plan(sample_plan())
    conn = sqlite3.connect(endurance_db)
    conn.execute("UPDATE endurance_phases SET end_date='2026-12-01' WHERE id='phase-base'")
    conn.execute("UPDATE endurance_phases SET start_date='2026-11-15' WHERE id='phase-threshold'")
    conn.commit()
    conn.close()

    normalized = patch_plan(plan["id"], [{"op": "normalize_phases"}])["plan"]
    phases = normalized["phases"]
    assert phases[0]["start_date"] == "2026-11-02"
    assert phases[-1]["end_date"] == normalized["event"]["event_date"]
    assert all(
        date.fromisoformat(right["start_date"]) == date.fromisoformat(left["end_date"]) + timedelta(days=1)
        for left, right in zip(phases, phases[1:])
    )
    assert not ({"phase_gap", "phase_overlap"} & {item["code"] for item in normalized["warnings"]})


def test_session_move_swap_duplicate_delete_and_revision(endurance_db):
    plan = create_plan(sample_plan())
    plan = patch_plan(plan["id"], [{"op": "add_session", "session": {"id": "session-easy", "scheduled_date": "2027-01-06", "session_type": "easy", "title": "Easy"}}])["plan"]
    plan = patch_plan(plan["id"], [{"op": "swap_sessions", "session_id": "session-interval", "other_session_id": "session-easy"}], expected_revision=plan["revision"])["plan"]
    assert next(x for x in plan["sessions"] if x["id"] == "session-interval")["scheduled_date"] == "2027-01-06"
    plan = patch_plan(plan["id"], [{"op": "duplicate_session", "session_id": "session-easy", "date": "2027-01-09"}])["plan"]
    assert len(plan["sessions"]) == 3
    plan = patch_plan(plan["id"], [{"op": "delete_session", "session_id": "session-easy"}], confirm=True)["plan"]
    assert len(plan["sessions"]) == 2
    assert plan["recent_changes"][0]["revision"] == plan["revision"]


def test_undo_restores_complete_actions_in_reverse_order(endurance_db):
    original = create_plan(sample_plan())
    moved = patch_plan(original["id"], [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-09"}])["plan"]
    deleted = patch_plan(moved["id"], [{"op": "delete_session", "session_id": "session-interval"}], confirm=True)["plan"]
    assert deleted["sessions"] == []
    assert deleted["undo_available"] is True

    restored_delete = undo_plan(deleted["id"], expected_revision=deleted["revision"])["plan"]
    assert restored_delete["sessions"][0]["scheduled_date"] == "2027-01-09"
    assert {step["id"] for step in restored_delete["sessions"][0]["steps"]} == {"warmup", "repeat", "work", "recovery"}

    restored_move = undo_plan(restored_delete["id"], expected_revision=restored_delete["revision"])["plan"]
    assert restored_move["sessions"][0]["scheduled_date"] == "2027-01-05"
    assert restored_move["undo_available"] is True
    removed = undo_plan(restored_move["id"], expected_revision=restored_move["revision"])
    assert removed["plan_removed"] is True
    with pytest.raises(EnduranceError, match="nothing_to_undo"):
        undo_plan(restored_move["id"], expected_revision=removed["revision"])


def test_redo_reapplies_undone_actions_and_new_changes_clear_redo(endurance_db):
    original = create_plan(sample_plan())
    moved = patch_plan(original["id"], [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-09"}])["plan"]
    restored = undo_plan(moved["id"], expected_revision=moved["revision"])["plan"]

    assert restored["sessions"][0]["scheduled_date"] == "2027-01-05"
    assert restored["redo_available"] is True
    repeated = redo_plan(restored["id"], expected_revision=restored["revision"])["plan"]
    assert repeated["sessions"][0]["scheduled_date"] == "2027-01-09"
    assert repeated["redo_available"] is False

    restored_again = undo_plan(repeated["id"], expected_revision=repeated["revision"])["plan"]
    changed = patch_plan(restored_again["id"], [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-12"}])["plan"]
    assert changed["redo_available"] is False
    with pytest.raises(EnduranceError, match="nothing_to_redo"):
        redo_plan(changed["id"], expected_revision=changed["revision"])


def test_undo_restores_anchor_and_rebase_state(endurance_db):
    plan = create_plan(sample_plan())
    anchored = patch_plan(plan["id"], [{"op": "set_fitness_anchor", "anchor": {"date": "2026-12-12", "source": "test", "five_k_time_s": 1260}}])["plan"]
    assert anchored["fitness_anchor"] is not None
    restored = undo_plan(plan["id"], expected_revision=anchored["revision"])["plan"]
    assert restored["fitness_anchor"] is None
    assert all(step["resolved"] is None for step in restored["sessions"][0]["steps"])


def test_clear_anchor_requires_confirmation_and_can_be_undone(endurance_db):
    plan = create_plan(sample_plan())
    anchored = patch_plan(plan["id"], [{"op": "set_fitness_anchor", "anchor": {"date": "2026-12-12", "source": "5k_benchmark", "five_k_time_s": 1260}}])["plan"]

    with pytest.raises(EnduranceError, match="confirm_required"):
        patch_plan(plan["id"], [{"op": "clear_fitness_anchor"}], expected_revision=anchored["revision"])

    cleared = patch_plan(plan["id"], [{"op": "clear_fitness_anchor"}], confirm=True, expected_revision=anchored["revision"])["plan"]
    assert cleared["fitness_anchor"] is None

    restored = undo_plan(plan["id"], expected_revision=cleared["revision"])["plan"]
    assert restored["fitness_anchor"]["five_k_time_s"] == 1260


def test_anchor_rebase_changes_future_only_and_preserves_absolute(endurance_db):
    payload = sample_plan()
    payload["sessions"].append({
        "id": "session-past", "scheduled_date": "2026-12-01", "session_type": "threshold", "title": "Past",
        "steps": [{"id": "past-step", "kind": "work", "distance_m": 1000, "target": {"metric": "pace", "basis": "fitness_anchor", "zone": "threshold"}}],
    })
    payload["sessions"][0]["steps"].append({"id": "absolute", "kind": "cooldown", "duration_s": 600, "target": {"metric": "pace", "basis": "absolute", "pace_s_per_km": 330}})
    plan = create_plan(payload)
    plan = patch_plan(plan["id"], [{"op": "set_fitness_anchor", "anchor": {"date": "2026-12-12", "source": "5k_benchmark", "five_k_time_s": 1275, "threshold_pace_s_per_km": 260, "zones": {"easy": {"pace_s_per_km": 345}}}}])["plan"]
    preview = preview_rebase(plan["id"], "2026-12-13")
    assert preview["past_changed"] == 0
    assert any(change["step_id"] == "work" and change["after"]["pace_s_per_km"] == 255 for change in preview["changes"])
    applied = patch_plan(plan["id"], [{"op": "rebase_future_targets", "from_date": "2026-12-13"}])["plan"]
    steps = {step["id"]: step for step in next(s for s in applied["sessions"] if s["id"] == "session-interval")["steps"]}
    assert steps["absolute"]["resolved"]["pace_s_per_km"] == 330
    past_steps = {step["id"]: step for step in next(s for s in applied["sessions"] if s["id"] == "session-past")["steps"]}
    assert past_steps["past-step"]["resolved"] is None


def test_missing_anchor_degrades_without_inventing_values(endurance_db):
    plan = create_plan(sample_plan())
    preview = preview_rebase(plan["id"], "2026-01-01")
    assert preview["changed"] == 0
    assert all(change.get("after") is None for change in preview["changes"])
    updated = patch_plan(plan["id"], [{"op": "set_session_target", "step_id": "work", "target": {"metric": "pace", "basis": "absolute", "pace_s_per_km": 250}}])["plan"]
    work = next(step for step in updated["sessions"][0]["steps"] if step["id"] == "work")
    assert work["target"]["pace_s_per_km"] == 250


def test_http_calendar_detail_patch_and_validation(endurance_db):
    app = Flask(__name__)
    app.register_blueprint(endurance_planning_api)
    client = app.test_client()
    created = client.post("/api/endurance/plans", json=sample_plan())
    assert created.status_code == 201
    plan = created.get_json()["plan"]
    detail = client.get(f"/api/endurance/plans/{plan['id']}?from=2027-01-01&to=2027-01-31")
    assert detail.status_code == 200
    assert len(detail.get_json()["plan"]["sessions"]) == 1
    moved = client.patch(f"/api/endurance/plans/{plan['id']}", json={
        "expected_revision": plan["revision"],
        "operations": [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-07"}],
    })
    assert moved.status_code == 200
    assert moved.get_json()["plan"]["sessions"][0]["scheduled_date"] == "2027-01-07"
    undone = client.post(f"/api/endurance/plans/{plan['id']}/undo", json={"expected_revision": moved.get_json()["plan"]["revision"]})
    assert undone.status_code == 200
    assert undone.get_json()["plan"]["sessions"][0]["scheduled_date"] == "2027-01-05"
    redone = client.post(f"/api/endurance/plans/{plan['id']}/redo", json={"expected_revision": undone.get_json()["plan"]["revision"]})
    assert redone.status_code == 200
    assert redone.get_json()["plan"]["sessions"][0]["scheduled_date"] == "2027-01-07"
    invalid = client.patch(f"/api/endurance/plans/{plan['id']}", json={"operations": [{"op": "unknown"}]})
    assert invalid.status_code == 400


def test_manual_sync_click_queues_and_executes_immediately(endurance_db, monkeypatch):
    plan = create_plan(sample_plan())
    calls = []
    monkeypatch.setattr("plans.endurance_api.retry_plan_sync", lambda plan_id: calls.append(("queue", plan_id)) or {"upsert": 1, "delete": 0, "retried": 0})
    monkeypatch.setattr("plans.endurance_api.run_sync_jobs", lambda limit, plan_id: calls.append(("run", limit, plan_id)) or {"processed": 1, "synced": 1, "deleted": 0, "skipped": 0, "errors": 0})
    app = Flask(__name__)
    app.register_blueprint(endurance_planning_api)

    response = app.test_client().post(f"/api/endurance/plans/{plan['id']}/sync/retry", json={})

    assert response.status_code == 200
    assert response.get_json()["started"] is True
    assert response.get_json()["result"]["synced"] == 1
    assert calls == [("queue", plan["id"]), ("run", 500, plan["id"])]


def test_actions_v2_endurance_capabilities_dry_run_and_readback(endurance_db):
    from ai import actions_v2

    plan = create_plan(sample_plan())
    capabilities, status = actions_v2._dispatch_liva_read({"mode": "capabilities"})
    assert status == 200
    assert "patch_endurance_plan" in capabilities["result"]["commands"]["endurance"]
    readback, status = actions_v2._dispatch_liva_read({"mode": "endurance_plan", "payload": {"endurance_plan_id": plan["id"]}})
    assert status == 200
    assert readback["plan"]["id"] == plan["id"]
    preview, status = actions_v2._dispatch_liva_act({
        "domain": "endurance", "command": "patch_endurance_plan", "dry_run": True,
        "payload": {"endurance_plan_id": plan["id"], "operations": [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-08"}]},
    })
    assert status == 200
    assert preview["dry_run"] is True
    denied, status = actions_v2._dispatch_liva_act({
        "domain": "endurance", "command": "patch_endurance_plan",
        "payload": {"endurance_plan_id": plan["id"], "operations": [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-08"}]},
    })
    assert status == 400
    assert denied["error"]["code"] == "confirmation_required"


def test_planning_template_contains_real_cardio_pane():
    source = open("templates/planning.html", encoding="utf-8").read()
    assert 'data-planning-tab="cardio"' in source
    assert 'id="endurance-planner"' in source
    assert "planning_endurance.js" in source
    script = open("static/js/planning_endurance.js", encoding="utf-8").read()
    assert 'deleteAnchor.textContent="Anchor löschen"' in script
    assert 'op:"clear_fitness_anchor"' in script
    assert "execution_steps" in script
    assert "repeatSpec" not in script
    assert "ep-workout-route" in script
    assert "ep-workout-metrics" in script
    assert "estimated_max_hr_bpm" in script
    assert "% HFmax" not in script
    assert 'data-action="undo"' in script
    assert 'data-action="redo"' in script
    assert 'action(key==="z"?"undo":"redo")' in script
    styles = open("static/css/planning_endurance.css", encoding="utf-8").read()
    assert "@media (min-width:768px) and (max-width:1250px)" in styles
    assert "grid-template-columns:repeat(4,minmax(0,1fr))" in styles
    assert "min-width:0;overflow:visible" in styles
    mobile = open("static/js/planning_mobile.js", encoding="utf-8").read()
    assert "execution_steps" in mobile
    assert "cardioRepeatSpec" not in mobile
    assert "mpv4-cardio-workout-chart" in mobile
    assert "% HFmax" not in mobile


def test_invalid_operation_and_revision_conflict(endurance_db):
    plan = create_plan(sample_plan())
    with pytest.raises(EnduranceError, match="invalid operation"):
        patch_plan(plan["id"], [{"op": "invent_training"}])
    with pytest.raises(EnduranceError, match="revision_conflict"):
        patch_plan(plan["id"], [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-08"}], expected_revision=999)


def test_session_aliases_are_explicitly_normalized_and_unknown_fields_rejected(endurance_db):
    payload = sample_plan()
    payload["sessions"] = [{
        "id": "alias-session", "date": "2027-01-10", "sport_type": "intervals",
        "title": "Intervals", "duration_min": 40, "distance_km": 8,
        "steps": [{"step_type": "repeat", "repetitions": 4, "steps": [
            {"step_type": "work", "duration_min": 3, "target": {"metric": "pace", "basis": "fitness_anchor", "zone": "interval"}}
        ]}],
    }]
    plan = create_plan(payload)
    session = plan["sessions"][0]
    assert session["scheduled_date"] == "2027-01-10"
    assert session["session_type"] == "interval"
    assert session["duration_s"] == 2400
    assert session["distance_m"] == 8000
    assert next(step for step in session["steps"] if step["kind"] == "repeat")["reps"] == 4

    bad = {"id": "bad-session", "scheduled_date": "2027-01-11", "session_type": "easy", "title": "Bad", "recovery_s": 90}
    with pytest.raises(EnduranceError, match="unsupported fields: recovery_s"):
        patch_plan(plan["id"], [{"op": "add_session", "session": bad}])

    bad_step = {"id": "bad-step-session", "scheduled_date": "2027-01-11", "session_type": "easy", "title": "Bad", "steps": [{"kind": "work", "recovery_s": 90}]}
    with pytest.raises(EnduranceError, match="unsupported fields: recovery_s"):
        patch_plan(plan["id"], [{"op": "add_session", "session": bad_step}])


def test_invalid_phase_type_rejected_and_duration_mismatch_warned(endurance_db):
    bad = sample_plan()
    bad["phases"][0]["phase_type"] = "nonsense"
    with pytest.raises(EnduranceError, match="invalid phase_type"):
        create_plan(bad)
    payload = sample_plan()
    payload["sessions"][0]["duration_s"] = 2400
    payload["sessions"][0]["steps"] = [
        {"id": "warmup", "kind": "warmup", "duration_s": 600},
        {"id": "work", "kind": "work", "duration_s": 600},
    ]
    with pytest.raises(EnduranceError, match="canonical repeat"):
        create_plan(payload)


def test_distance_steps_use_target_pace_for_duration_validation(endurance_db):
    payload = sample_plan()
    session = payload["sessions"][0]
    # 600 warmup + 2x300 work + one 100s recovery + 600 cooldown.
    # Canonical repeats deliberately omit recovery after the final rep.
    session["duration_s"] = 1900
    session["steps"] = [
        {"id": "warmup", "kind": "warmup", "duration_s": 600},
        {"id": "repeat", "kind": "repeat", "reps": 2, "steps": [
            {"id": "work", "kind": "work", "distance_m": 1000, "target": {"metric": "pace", "basis": "absolute", "pace_s_per_km": 300}},
            {"id": "recovery", "kind": "recovery", "duration_s": 100},
        ]},
        {"id": "cooldown", "kind": "cooldown", "duration_s": 600},
    ]
    plan = create_plan(payload)
    assert not any(warning["code"] == "duration_mismatch" for warning in plan["warnings"])


def test_repeat_duration_validation_does_not_count_recovery_after_final_rep(endurance_db):
    payload = sample_plan()
    session = payload["sessions"][0]
    session["duration_s"] = 2000
    session["steps"] = [
        {"id": "warmup", "kind": "warmup", "duration_s": 600},
        {"id": "repeat", "kind": "repeat", "reps": 2, "steps": [
            {"id": "work", "kind": "work", "distance_m": 1000, "target": {"metric": "pace", "basis": "absolute", "pace_s_per_km": 300}},
            {"id": "recovery", "kind": "recovery", "duration_s": 100},
        ]},
        {"id": "cooldown", "kind": "cooldown", "duration_s": 600},
    ]
    plan = create_plan(payload)
    assert any(warning["code"] == "duration_mismatch" for warning in plan["warnings"])


def test_unpaced_distance_step_does_not_create_false_duration_warning(endurance_db):
    payload = sample_plan()
    session = payload["sessions"][0]
    session["duration_s"] = 3105
    session["session_type"] = "race"
    session["title"] = "5K Standorttest"
    session["steps"] = [
        {"id": "warmup", "kind": "warmup", "duration_s": 900},
        {"id": "time-trial", "kind": "work", "distance_m": 5000, "target": {"metric": "rpe", "basis": "absolute", "min": 9, "max": 10}},
        {"id": "cooldown", "kind": "cooldown", "duration_s": 600},
    ]
    plan = create_plan(payload)
    assert not any(warning["code"] == "duration_mismatch" for warning in plan["warnings"])


def test_anchor_is_visible_in_dry_run_preview(endurance_db):
    plan = create_plan(sample_plan())
    result = patch_plan(plan["id"], [{"op": "set_fitness_anchor", "anchor": {
        "date": "2026-12-12", "source": "manual", "five_k_time_s": 1320,
        "threshold_pace_s_per_km": 270, "zones": {},
    }}], dry_run=True)
    assert result["preview"]["fitness_anchor"]["five_k_time_s"] == 1320
    assert get_plan(plan["id"])["fitness_anchor"] is None


def test_duplicate_phase_sort_orders_are_normalized_on_read(endurance_db):
    plan = create_plan(sample_plan())
    conn = sqlite3.connect(endurance_db)
    conn.execute("UPDATE endurance_phases SET sort_order=0 WHERE plan_id=?", (plan["id"],))
    conn.commit()
    conn.close()
    phases = get_plan(plan["id"])["phases"]
    assert [phase["sort_order"] for phase in phases] == [0, 1]
    assert phases[1]["sort_order_source"] == "normalized_from_dates"


def test_endurance_dry_run_response_never_claims_live_write(endurance_db):
    from ai import actions_v2

    plan = create_plan(sample_plan())
    with Flask(__name__).test_request_context():
        payload, status = actions_v2._dispatch_liva_act({
            "domain": "endurance", "command": "patch_endurance_plan", "dry_run": True,
            "payload": {"plan_id": plan["id"], "operations": [{"op": "move_session", "session_id": "session-interval", "date": "2027-01-08"}]},
        })
        assert status == 200
        response = actions_v2._liva_act_response("endurance", "patch_endurance_plan", payload, status)
        body = response.get_json()
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["result"]["dry_run"] is True


def test_endurance_response_uses_backend_execution_metadata(endurance_db):
    from ai import actions_v2

    with Flask(__name__).test_request_context():
        response = actions_v2._liva_act_response(
            "endurance", "set_execution_mode", {"ok": True, "result": {"mode": "RUN"}}, 200
        )
        body = response.get_json()
    assert body["execution"]["mode"] == "no_change"
    assert body["execution"]["live_state_changed"] is False


def test_pace_formatter_accepts_legacy_seconds_per_km():
    from ai.actions_v2 import _fmt_pace_from_min_per_km

    assert _fmt_pace_from_min_per_km(342.7) == "5:43"
    assert _fmt_pace_from_min_per_km(5.711) == "5:43"


def test_cardio_data_forwards_sport_filter(monkeypatch):
    from ai import actions_v2

    captured = {}
    def fake_call(_view, *, method, body):
        captured.update(body)
        return {"ok": True}, 200
    monkeypatch.setattr(actions_v2, "_call_actions_view", fake_call)
    _, status = actions_v2._dispatch_liva_read(
        {"mode": "cardio_data", "payload": {"sport_type": "run", "date_from": "2026-01-01"}}
    )
    assert status == 200
    assert captured["sport_type"] == "run"


def test_cardio_data_run_filter_returns_canonical_run(endurance_db, monkeypatch, tmp_path):
    from ai import actions_v2

    runs_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(actions_v2.connections, "RUNS_DB", str(runs_path))
    conn = actions_v2.connections.get_runs_db()
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time INTEGER, pace REAL, avg_hr REAL, sport_type TEXT, run_type TEXT, note TEXT)"
    )
    conn.execute(
        "INSERT INTO runs (id,date,distance,moving_time,pace,sport_type,note) VALUES (?,?,?,?,?,?,?)",
        (-9090701, "2026-09-07 05:57:29", 5246.9, 1798, 342.7, "run", "Laufen"),
    )
    conn.commit()
    conn.close()

    with Flask(__name__).test_request_context(json={"sport_type": "run", "date_from": "2026-09-07", "date_to": "2026-09-07"}):
        response = actions_v2.runs_data.__wrapped__()
    body = response.get_json()
    assert body["runs"]["sessions"][0]["sport_type"] == "run"


def test_endurance_warning_objects_and_write_metadata_survive_outer_facade(endurance_db):
    from ai import actions_v2

    warning = {"code": "phase_gap", "severity": "warning", "message": "Gap", "ids": ["a", "b"]}
    with Flask(__name__).test_request_context():
        read_response = actions_v2._liva_read_response(
            "endurance_calendar", {"ok": True, "data_status": "fresh", "warnings": [warning]}, 200
        )
        act_response = actions_v2._liva_act_response(
            "endurance",
            "patch_endurance_plan",
            {
                "ok": True,
                "dry_run": True,
                "preview": {"warnings": [warning]},
                "affected_ids": ["ep_123"],
                "visible_in_frontend": False,
                "execution": {"mode": "dry_run", "live_state_changed": False},
            },
            200,
        )
    assert read_response.get_json()["warnings"] == [warning]
    assert act_response.get_json()["warnings"] == [warning]
    assert act_response.get_json()["affected_ids"] == ["ep_123"]


def test_external_week_session_id_can_be_used_for_detail_read(endurance_db, monkeypatch):
    from ai import actions_v2

    plan = create_plan(sample_plan())
    monkeypatch.setattr(actions_v2, "build_endurance_core_context", lambda day, refresh=False: {
        "has_workout": True,
        "intervals_event_id": "evt-42",
        "original_label": "External Run",
        "original": {"id": "evt-42", "label": "External Run", "type": "Run"},
    })
    result, status = actions_v2._dispatch_liva_read({
        "mode": "endurance_session",
        "payload": {"plan_id": plan["id"], "session_id": "intervals:2027-01-07:evt-42"},
    })
    assert status == 200
    assert result["session"]["id"] == "intervals:2027-01-07:evt-42"
    assert result["session"]["external_event_id"] == "evt-42"
    assert result["session"]["provenance"] == "external_intervals"
