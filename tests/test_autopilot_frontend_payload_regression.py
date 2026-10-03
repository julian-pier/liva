from __future__ import annotations

import importlib
import os
from datetime import date, timedelta


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def test_stale_shifted_rest_does_not_stick_for_today(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [
                {"strength_exercises": [{"exercise_name": "A"}], "session_name": "Upper A", "run_sessions": []},
                {"strength_exercises": [], "run_sessions": [{"run_type": "threshold"}]},
                {"strength_exercises": [{"exercise_name": "B"}], "session_name": "Upper B", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "L"}], "session_name": "Legs", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "C"}], "session_name": "Upper C", "run_sessions": []},
                {"strength_exercises": [], "run_sessions": [{"run_type": "z2"}]},
                {"strength_exercises": [], "run_sessions": []},
            ],
        },
    )

    today = date.today()
    week_start = appmod._autopilot_week_start(today)
    today_iso = today.isoformat()

    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": True, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {
                "hrv_ratio": 0.80,
                "rhr_ratio": 1.07,
                "rmssd_today": 80.0,
                "rmssd_28d": 120.0,
                "rhr_today": 52.0,
                "rhr_28d": 49.0,
            },
            "metrics": {"expected_7d": 6, "actual_7d": 3, "skipped_7d": 3, "override_count_7d": 0},
        },
    )

    # Simulate stale persisted week where today's slot was rest.
    def fake_apply_week_override(week_plan, week_start_arg, planned_json, plan_id=None):
        out = [dict(d) for d in week_plan]
        for d in out:
            if d.get("day_id") == today_iso:
                d.update({"kind": "rest", "label": "Rest", "session_key": None, "run_kind": None})
        return out

    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", fake_apply_week_override)

    with appmod.app.test_request_context("/api/next_session"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    # Must not remain sticky rest; on run-threshold day with fatigue it should downshift to Z2.
    assert (payload.get("final_day") or {}).get("kind") != "rest"
    assert (payload.get("session") or {}).get("session_name") in {"Run Z2", "Run THRESHOLD", "Upper A", "Upper B", "Legs", "Upper C"}


def test_week_rpe_cap_is_enforced_in_gym_payload(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today_iso = date.today().isoformat()

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [{"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Upper A", "run_sessions": []}] + [{"strength_exercises": [], "run_sessions": []} for _ in range(6)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {"hrv_ratio": 1.0, "rhr_ratio": 1.0, "rmssd_today": 100.0, "rmssd_28d": 100.0, "rhr_today": 50.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 1, "actual_7d": 1, "skipped_7d": 0, "override_count_7d": 0},
            "gym": {"hi_exposures_3d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_get_autopilot_rpe_hardcap", lambda: 9.5)
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])

    monkeypatch.setattr(
        appmod,
        "determine_next_session",
        lambda *args, **kwargs: {
            "kind": "plan",
            "session_key": "d1-upper-a",
            "session_name": "Upper A",
            "minimal_gym": False,
            "reason_codes": [],
            "decision_meta": {"outputs": {"choice": "plan:d1-upper-a"}},
            "badges": ["GYM"],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_week_overview",
        lambda: appmod.jsonify({"ok": True, "week": {"week_type": "build"}, "layer": {"week_type": "build", "rpe_cap": 6.5}}),
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_for_session",
        lambda: appmod.jsonify(
            {
                "ok": True,
                "session": {"session_key": "d1-upper-a", "session_name": "Upper A"},
                "reference_workout": None,
                "items": [
                    {
                        "title": "Bench",
                        "planned": {"rpe_min": 8.0, "rpe_max": 9.0, "rpe_values": [8.0, 8.0, 8.0], "sets": 3, "display_rpe": "8", "weight_suggestion": 100.0},
                        "structured": {"working_weight": 100.0, "new_weights": [100.0, 100.0, 100.0]},
                        "done": {"sets": 0, "reps": [], "avg_rpe": None, "weights": [], "rpes": [], "set_numbers": []},
                    }
                ],
                "summary": {"status": "green", "flags": []},
            }
        ),
    )

    with appmod.app.test_request_context(f"/api/next_session?day={today_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    item = (payload.get("items") or [])[0]
    planned = item.get("planned") or {}
    values = [float(v) for v in (planned.get("rpe_values") or [])]
    assert values
    assert values[0] == 6.5
    assert all(5.5 <= value <= 6.5 for value in values)
    assert float(planned.get("rpe_max")) == 6.5
    assert float(planned.get("rpe_min")) == 5.5
    assert float(planned.get("weight_suggestion")) == 100.0
    structured = item.get("structured") or {}
    assert float(structured.get("working_weight")) == 100.0


def test_tomorrow_lower_preview_no_longer_blindly_swaps_to_pull(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [
                {"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Push", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "Squat"}], "session_name": "Lower A", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "Row"}], "session_name": "Pull", "run_sessions": []},
            ] + [{"strength_exercises": [], "run_sessions": []} for _ in range(4)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {
                "sick": False,
                "alcohol": False,
                "zns_fatigue": False,
                "local_fatigue": {"legs": True, "push": False, "pull": False},
                "hi_yesterday": False,
            },
            "subjective": {
                "motivation_state": "ok",
                "sleep_state": "ok",
                "fatigue_state": "ok",
                "readiness_state": "ok",
            },
            "recovery": {"hrv_ratio": 1.04, "rhr_ratio": 0.99, "rmssd_today": 120.0, "rmssd_28d": 110.0, "rhr_today": 49.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 3, "actual_7d": 3, "skipped_7d": 0, "override_count_7d": 0},
            "gym": {"hi_exposures_3d": 0},
            "learning_profile": {"learning_ready": False},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])

    with appmod.app.test_request_context(f"/api/next_session?day={tomorrow_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    tomorrow = next((d for d in payload.get("week_days") or [] if d.get("day_id") == tomorrow_iso), {})
    assert "Lower A" in str(tomorrow.get("label") or "")
    assert "Pull" not in str(tomorrow.get("label") or "")


def test_deload_does_not_force_light_mode(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today_iso = date.today().isoformat()
    seen = {"mode": None}

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [{"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Upper A", "run_sessions": []}] + [{"strength_exercises": [], "run_sessions": []} for _ in range(6)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {"hrv_ratio": 0.95, "rhr_ratio": 1.03, "rmssd_today": 95.0, "rmssd_28d": 100.0, "rhr_today": 52.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 1, "actual_7d": 1, "skipped_7d": 0, "override_count_7d": 0},
            "gym": {"hi_exposures_3d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])
    monkeypatch.setattr(
        appmod,
        "determine_next_session",
        lambda *args, **kwargs: {
            "kind": "plan",
            "session_key": "d1-upper-a",
            "session_name": "Upper A",
            "minimal_gym": True,
            "reason_codes": [],
            "decision_meta": {"outputs": {"choice": "plan:d1-upper-a"}},
            "badges": ["RPE ↓", "Volumen ↓"],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_week_overview",
        lambda: appmod.jsonify({"ok": True, "week": {"week_type": "deload"}, "layer": {"week_type": "deload", "rpe_cap": 6.5}}),
    )

    def _fake_plan_check_for_session():
        seen["mode"] = appmod.request.args.get("mode")
        return appmod.jsonify(
            {
                "ok": True,
                "session": {"session_key": "d1-upper-a", "session_name": "Upper A"},
                "reference_workout": None,
                "items": [],
                "summary": {"status": "green", "flags": []},
            }
        )

    monkeypatch.setattr(appmod, "api_dashboard_plan_check_for_session", _fake_plan_check_for_session)

    with appmod.app.test_request_context(f"/api/next_session?day={today_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    assert seen["mode"] == "normal"
    assert (payload.get("final_day") or {}).get("is_light") is False
    badges = (((payload.get("session") or {}).get("meta") or {}).get("badges") or [])
    assert "Light" not in badges


def test_deload_week_day_override_does_not_mark_minimal_gym(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today = date.today()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [
                {"strength_exercises": [{"exercise_name": "A"}], "session_name": "Upper A", "run_sessions": []},
                {"strength_exercises": [], "run_sessions": [{"run_type": "threshold"}]},
                {"strength_exercises": [{"exercise_name": "B"}], "session_name": "Upper B", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "L"}], "session_name": "Legs", "run_sessions": []},
                {"strength_exercises": [{"exercise_name": "C"}], "session_name": "Upper C", "run_sessions": []},
                {"strength_exercises": [], "run_sessions": [{"run_type": "z2"}]},
                {"strength_exercises": [], "run_sessions": []},
            ],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": True, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {"hrv_ratio": 0.80, "rhr_ratio": 1.07, "rmssd_today": 80.0, "rmssd_28d": 120.0, "rhr_today": 52.0, "rhr_28d": 49.0},
            "metrics": {"expected_7d": 6, "actual_7d": 3, "skipped_7d": 3, "override_count_7d": 0},
            "gym": {"hi_exposures_3d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])
    monkeypatch.setattr(
        appmod,
        "_autopilot_plan_context_for_day",
        lambda day_id, planned_slot=None: {"phase": "DELOAD", "week": "8/8", "days_to_deload": 0, "plan_ctx_partial": False},
    )

    with appmod.app.test_request_context(f"/api/next_session?day={tomorrow_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    entry = next((d for d in (payload.get("week_days") or []) if d.get("day_id") == tomorrow_iso), {})
    assert entry.get("minimal_gym") in {False, None}
    assert "Light" not in (entry.get("badges") or [])


def test_next_session_payload_contains_core_execution_profile_and_item_overrides(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today_iso = date.today().isoformat()

    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [{"strength_exercises": [{"exercise_name": "Bench Press"}], "session_name": "Push", "run_sessions": []}] + [{"strength_exercises": [], "run_sessions": []} for _ in range(6)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "zns_fatigue": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
            "subjective": {"motivation_state": "high", "sleep_state": "ok", "fatigue_state": "low", "readiness_state": "high"},
            "recovery": {"hrv_ratio": 1.05, "rhr_ratio": 0.98, "rmssd_today": 105.0, "rmssd_28d": 100.0, "rhr_today": 49.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 1, "actual_7d": 1, "skipped_7d": 0, "override_count_7d": 0},
            "gym": {"hi_exposures_3d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])
    monkeypatch.setattr(
        appmod,
        "determine_next_session",
        lambda *args, **kwargs: {
            "kind": "plan",
            "session_key": "d1-push",
            "session_name": "Push",
            "minimal_gym": False,
            "reason_codes": ["CORE_PUSH_GYM"],
            "decision_meta": {"core_overlay": {"execution": {"band": "push", "gym_mode": "push"}}},
            "badges": ["CORE"],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_week_overview",
        lambda: appmod.jsonify({"ok": True, "week": {"week_type": "build"}, "layer": {"week_type": "build", "rpe_cap": 9.0, "strength_factor": 1.1, "run_factor": 1.0}}),
    )
    monkeypatch.setattr(
        appmod,
        "_build_core_execution_profile",
        lambda **kwargs: {
            "gym_mode": "push",
            "run_mode": "normal",
            "volume_factor": 1.08,
            "weight_factor": 1.03,
            "rpe_shift": 0.4,
            "energy": {"daily_status": "green"},
            "item_profiles": [
                {"title": "Bench Press", "weight_factor": 1.05, "rpe_shift": 0.5, "set_delta": 1, "notes": [], "replacement_candidates": []}
            ],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_for_session",
        lambda: appmod.jsonify(
            {
                "ok": True,
                "session": {"session_key": "d1-push", "session_name": "Push"},
                "reference_workout": None,
                "items": [
                    {
                        "title": "Bench Press",
                        "planned": {"sets": 3, "rpe_min": 8.0, "rpe_max": 8.0, "rpe_values": [8.0, 8.0, 8.0], "display_rpe": "8", "weight_suggestion": 100.0, "device": "Langhantel"},
                        "structured": {"working_weight": 100.0, "new_weights": [100.0, 100.0, 100.0]},
                        "done": {"sets": 0, "reps": [], "avg_rpe": None, "weights": [], "rpes": [], "set_numbers": []},
                    }
                ],
                "summary": {"status": "green", "flags": []},
            }
        ),
    )

    with appmod.app.test_request_context(f"/api/next_session?day={today_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    decision_meta = (((payload.get("session") or {}).get("meta") or {}).get("decision_meta") or {})
    profile = (((decision_meta.get("core_overlay") or {}).get("execution_profile")) or {})
    assert profile.get("gym_mode") == "push"
    assert profile.get("energy", {}).get("daily_status") == "green"
    item = (payload.get("items") or [])[0]
    assert item["planned"]["sets"] >= 3
    assert float(item["planned"]["weight_suggestion"]) > 100.0


def test_next_session_payload_exposes_core_mode_enabled_when_on(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today_iso = date.today().isoformat()

    monkeypatch.setattr(appmod, "_get_core_mode_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [{"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Push", "run_sessions": []}] + [{"strength_exercises": [], "run_sessions": []} for _ in range(6)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {"hrv_ratio": 1.0, "rhr_ratio": 1.0, "rmssd_today": 100.0, "rmssd_28d": 100.0, "rhr_today": 50.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 1, "actual_7d": 1, "skipped_7d": 0, "override_count_7d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])
    monkeypatch.setattr(
        appmod,
        "determine_next_session",
        lambda *args, **kwargs: {
            "kind": "plan",
            "session_key": "d1-push",
            "session_name": "Push",
            "minimal_gym": False,
            "reason_codes": [],
            "decision_meta": {},
            "badges": [],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_for_session",
        lambda: appmod.jsonify(
            {
                "ok": True,
                "session": {"session_key": "d1-push", "session_name": "Push"},
                "reference_workout": None,
                "items": [],
                "summary": {"status": "green", "flags": []},
            }
        ),
    )

    with appmod.app.test_request_context(f"/api/next_session?day={today_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    assert payload.get("core_mode_enabled") is True
    assert (((payload.get("session") or {}).get("meta") or {}).get("core_mode_enabled")) is True


def test_next_session_payload_exposes_core_mode_enabled_when_off(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    today_iso = date.today().isoformat()

    monkeypatch.setattr(appmod, "_get_core_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(
        appmod,
        "plan_row_to_dict",
        lambda row: {
            "id": 1,
            "base_week": [{"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Push", "run_sessions": []}] + [{"strength_exercises": [], "run_sessions": []} for _ in range(6)],
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_state_snapshot",
        lambda: {
            "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
            "recovery": {"hrv_ratio": 1.0, "rhr_ratio": 1.0, "rmssd_today": 100.0, "rmssd_28d": 100.0, "rhr_today": 50.0, "rhr_28d": 50.0},
            "metrics": {"expected_7d": 1, "actual_7d": 1, "skipped_7d": 0, "override_count_7d": 0},
        },
    )
    monkeypatch.setattr(appmod, "_autopilot_apply_week_override", lambda week_plan, week_start, planned_json, plan_id=None: [dict(d) for d in week_plan])
    monkeypatch.setattr(
        appmod,
        "determine_next_session",
        lambda *args, **kwargs: {
            "kind": "plan",
            "session_key": "d1-push",
            "session_name": "Push",
            "minimal_gym": False,
            "reason_codes": [],
            "decision_meta": {},
            "badges": [],
        },
    )
    monkeypatch.setattr(
        appmod,
        "api_dashboard_plan_check_for_session",
        lambda: appmod.jsonify(
            {
                "ok": True,
                "session": {"session_key": "d1-push", "session_name": "Push"},
                "reference_workout": None,
                "items": [],
                "summary": {"status": "green", "flags": []},
            }
        ),
    )

    with appmod.app.test_request_context(f"/api/next_session?day={today_iso}"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    assert payload.get("core_mode_enabled") is False
    assert (((payload.get("session") or {}).get("meta") or {}).get("core_mode_enabled")) is False
