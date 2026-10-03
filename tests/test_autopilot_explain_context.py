from __future__ import annotations

import importlib
import os


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        appmod = importlib.reload(os.sys.modules["app"])
    else:
        appmod = importlib.import_module("app")
    return appmod


def test_autopilot_plan_context_for_day_from_cycle(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(
        appmod,
        "_get_active_gym_plan_record",
        lambda: {"plan_json": {"meta": {}}, "rules_json": {"weeks": {}}},
    )
    monkeypatch.setattr(
        appmod,
        "_gym_plan_cycle_status",
        lambda plan_json, rules_json, *, today=None: {
            "week_current": 7,
            "week_total": 8,
            "phase_label": "OVERREACH",
        },
    )
    monkeypatch.setattr(
        appmod,
        "_block_status_days_until_deload",
        lambda cycle, plan_json, rules_json, *, today=None: 1,
    )

    ctx = appmod._autopilot_plan_context_for_day("2026-02-15")
    assert ctx["phase"] == "OVERREACH"
    assert ctx["week"] == "7/8"
    assert ctx["days_to_deload"] == 1
    assert ctx["plan_ctx_partial"] is False


def test_autopilot_plan_context_for_day_fallback_partial(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "_get_active_gym_plan_record", lambda: None)
    ctx = appmod._autopilot_plan_context_for_day("2026-02-15")
    assert ctx["phase"] == "unknown"
    assert ctx["week"] == "?/?"
    assert ctx["days_to_deload"] is None
    assert ctx["plan_ctx_partial"] is True


def test_core_overlay_does_not_light_push_for_legs_fatigue(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})

    decision = {
        "kind": "plan",
        "session_key": "push-a",
        "session_name": "Push",
        "minimal_gym": False,
        "reason_codes": ["LEGS_FATIGUE"],
        "decision_meta": {},
        "badges": [],
    }
    state_snapshot = {
        "flags": {
            "zns_fatigue": False,
            "sick": False,
            "alcohol": False,
            "local_fatigue": {"legs": True, "push": False, "pull": False},
        }
    }
    core_overlay = {
        "enabled": True,
        "available": True,
        "features": {},
        "actions": {"signal_guard": True},
        "why_lines": [],
    }
    planned_slot = {"kind": "plan", "category": "push", "session_name": "Push"}

    out = appmod._apply_core_overlay_to_decision(decision, state_snapshot, core_overlay, planned_slot=planned_slot)

    assert out.get("minimal_gym") is False
    assert "LEGS_FATIGUE" not in (out.get("reason_codes") or [])
    assert "CORE_LIGHT_GYM" not in (out.get("reason_codes") or [])


def test_core_overlay_can_light_lower_for_legs_fatigue(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})

    decision = {
        "kind": "plan",
        "session_key": "lower-a",
        "session_name": "Lower",
        "minimal_gym": False,
        "reason_codes": ["LEGS_FATIGUE"],
        "decision_meta": {},
        "badges": [],
    }
    state_snapshot = {
        "flags": {
            "zns_fatigue": False,
            "sick": False,
            "alcohol": False,
            "local_fatigue": {"legs": True, "push": False, "pull": False},
        }
    }
    core_overlay = {
        "enabled": True,
        "available": True,
        "features": {},
        "actions": {"signal_guard": True},
        "why_lines": [],
    }
    planned_slot = {"kind": "plan", "category": "legs", "session_name": "Lower"}

    out = appmod._apply_core_overlay_to_decision(decision, state_snapshot, core_overlay, planned_slot=planned_slot)

    assert out.get("minimal_gym") is True
    assert "LEGS_FATIGUE" in (out.get("reason_codes") or [])
    assert "CORE_LIGHT_GYM" in (out.get("reason_codes") or [])


def test_core_overlay_pushes_gym_when_recovery_and_week_factor_are_good(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})

    decision = {
        "kind": "plan",
        "session_key": "push-a",
        "session_name": "Push",
        "minimal_gym": True,
        "reason_codes": [],
        "decision_meta": {},
        "badges": [],
    }
    state_snapshot = {
        "flags": {
            "zns_fatigue": False,
            "sick": False,
            "alcohol": False,
            "local_fatigue": {"legs": False, "push": False, "pull": False},
        },
        "recovery": {"hrv_ratio": 1.06, "rhr_ratio": 0.98},
        "subjective": {
            "motivation_state": "low",
            "sleep_state": "ok",
            "fatigue_state": "ok",
            "readiness_state": "high",
        },
    }
    core_overlay = {
        "enabled": True,
        "available": True,
        "features": {"recovery_state": "high"},
        "actions": {"signal_guard": True, "nutrition_guard": True, "consistency_guard": True},
        "why_lines": [],
    }
    planned_slot = {"kind": "plan", "category": "push", "session_name": "Push"}

    out = appmod._apply_core_overlay_to_decision(
        decision,
        state_snapshot,
        core_overlay,
        planned_slot=planned_slot,
        week_layer={"week_type": "build", "strength_factor": 1.1, "run_factor": 1.0, "rpe_cap": None},
    )

    assert out.get("minimal_gym") is False
    assert "CORE_PUSH_GYM" in (out.get("reason_codes") or [])


def test_core_overlay_hrv_beats_good_motivation_and_keeps_light(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})

    decision = {
        "kind": "plan",
        "session_key": "push-a",
        "session_name": "Push",
        "minimal_gym": False,
        "reason_codes": [],
        "decision_meta": {},
        "badges": [],
    }
    state_snapshot = {
        "flags": {
            "zns_fatigue": True,
            "sick": False,
            "alcohol": False,
            "local_fatigue": {"legs": False, "push": False, "pull": False},
        },
        "recovery": {"hrv_ratio": 0.85, "rhr_ratio": 1.07},
        "subjective": {
            "motivation_state": "high",
            "sleep_state": "high",
            "fatigue_state": "low",
            "readiness_state": "high",
        },
    }
    core_overlay = {
        "enabled": True,
        "available": True,
        "features": {"recovery_state": "low"},
        "actions": {"progression_push": True},
        "why_lines": [],
    }
    planned_slot = {"kind": "plan", "category": "push", "session_name": "Push"}

    out = appmod._apply_core_overlay_to_decision(
        decision,
        state_snapshot,
        core_overlay,
        planned_slot=planned_slot,
        week_layer={"week_type": "build", "strength_factor": 1.1, "run_factor": 1.0, "rpe_cap": None},
    )

    assert out.get("minimal_gym") is True
    assert "CORE_LIGHT_GYM" in (out.get("reason_codes") or [])
    assert "CORE_PUSH_GYM" not in (out.get("reason_codes") or [])


def test_core_execution_profile_reads_energy_memory_and_horizon(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    monkeypatch.setattr(
        appmod,
        "_core_memory_learning_map",
        lambda day_iso=None: {
            "lower_to_run_interference": {"id": "lower_to_run_interference"},
            "sleep_to_rpe_drift": {"id": "sleep_to_rpe_drift"},
        },
    )
    monkeypatch.setattr(
        appmod,
        "_core_energy_context",
        lambda day_iso=None, lookback_days=35: {
            "available": True,
            "mode": "cut",
            "daily_status": "red",
            "momentum": -260.0,
            "target_kcal": 2400.0,
            "daily_delta": -350.0,
        },
    )
    monkeypatch.setattr(
        appmod,
        "_autopilot_plan_context_for_day",
        lambda day_id, planned_slot=None: {"phase": "BUILD", "week": "2/6", "days_to_deload": 3, "plan_ctx_partial": False},
    )

    profile = appmod._build_core_execution_profile(
        selected_day_id="2026-03-03",
        decision={
            "kind": "plan",
            "session_name": "Lower A",
            "decision_meta": {"core_overlay": {"execution": {"band": "push"}}},
        },
        state_snapshot={
            "flags": {"local_fatigue": {"legs": False, "push": False, "pull": False}},
            "subjective": {"readiness_state": "ok", "sleep_state": "low", "fatigue_state": "ok"},
            "gym": {"hi_exposures_3d": 2},
        },
        core_overlay={"features": {"strength_trend": "up", "nutrition_adherence": "low"}, "actions": {}},
        week_layer={"week_type": "build", "strength_factor": 1.1, "run_factor": 1.0, "rpe_cap": 8.5},
        planned_slot={"kind": "plan", "category": "legs", "session_name": "Lower A"},
        week_days=[
            {"day_id": "2026-03-03", "planned_label": "Lower A", "final_label": "Lower A", "kind": "plan"},
            {"day_id": "2026-03-04", "planned_label": "Run THRESHOLD", "final_label": "Run THRESHOLD", "kind": "run"},
        ],
        items=[],
        pain_keys=set(),
    )

    assert profile["gym_mode"] != "push"
    assert profile["horizon"]["risk"] > 1.0
    assert "lower_to_run_interference" in (profile.get("learning_keys") or [])
    assert "Kalorien-Momentum negativ" in " | ".join(profile.get("reasons") or [])


def test_core_execution_profile_modifies_item_level_gym_suggestions(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    items = [
        {
            "title": "Bench Press",
            "planned": {"sets": 3, "rpe_values": [8.0, 8.0, 8.0], "rpe_min": 8.0, "rpe_max": 8.0, "device": "Langhantel", "weight_suggestion": 100.0},
            "structured": {"working_weight": 100.0, "new_weights": [100.0, 100.0, 100.0]},
        },
        {
            "title": "Seitheben",
            "planned": {"sets": 3, "rpe_values": [8.0, 8.0, 8.0], "rpe_min": 8.0, "rpe_max": 8.0, "device": "Cable", "weight_suggestion": 20.0},
            "structured": {"working_weight": 20.0, "new_weights": [20.0, 20.0, 20.0]},
        },
    ]
    profile = {
        "volume_factor": 1.08,
        "weight_factor": 1.025,
        "rpe_shift": 0.4,
        "item_profiles": [
            {"title": "Bench Press", "weight_factor": 1.05, "rpe_shift": 0.5, "set_delta": 1, "notes": [], "replacement_candidates": []},
            {"title": "Seitheben", "weight_factor": 0.92, "rpe_shift": -0.6, "set_delta": -1, "notes": ["Pain-Flag auf Bewegung"], "replacement_candidates": ["cable lateral raise"]},
        ],
    }

    out = appmod._apply_core_execution_profile_to_items(items, profile)

    bench = out[0]
    lateral = out[1]
    assert bench["planned"]["sets"] == 4
    assert float(bench["planned"]["weight_suggestion"]) > 100.0
    assert max(bench["planned"]["rpe_values"]) > 8.0
    assert lateral["planned"]["sets"] == 2
    assert float(lateral["planned"]["weight_suggestion"]) < 20.0
    assert "Pain-Flag" in str(lateral.get("core_note") or "")


def test_core_dashboard_explain_marks_normal_push_as_not_light(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})
    monkeypatch.setattr(appmod, "_core_data_coverage_payload", lambda: {"quality": "Mittel", "note": "brauchbar"})
    monkeypatch.setattr(appmod, "_core_memory_catalog", lambda: [])
    monkeypatch.setattr(appmod, "_core_principle_impacts", lambda principles: [])

    explain = appmod._build_core_dashboard_explain(
        state_snapshot={},
        week_days=[{"day_id": "2026-03-02", "planned_label": "Push", "final_label": "Push", "label": "Mo · Push"}],
        selected_day_id="2026-03-02",
        decision={"kind": "plan", "reason_codes": []},
        core_info={
            "enabled": True,
            "actions": {"signal_guard": True, "nutrition_guard": True},
            "execution": {"gym_mode": "normal"},
            "principles": [{"topic": "plan_first", "label": "Plan-First", "strength": 80, "risk": 20, "effect": "Plan bleibt stabil"}],
        },
        is_light_day=False,
    )

    assert explain["decision_line"] == "Entscheidung: Push · CORE bestätigt normal"
    assert explain["status_line"] == "Status: Vorsicht · Datenlage Mittel"
    assert explain["phase_line"] == ""
    assert any("nicht aus, um Push auf Light zu setzen" in line for line in (explain.get("why_lines") or []))
    assert explain["signal_lines"] == ["Datenlage: Mittel"]
    assert "Heute normal trainieren" in str(explain.get("hint") or "")


def test_core_dashboard_explain_marks_push_window_clearly(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_core_principle_scores", lambda: {})
    monkeypatch.setattr(appmod, "_core_data_coverage_payload", lambda: {"quality": "Mittel", "note": "brauchbar"})
    monkeypatch.setattr(appmod, "_core_memory_catalog", lambda: [])
    monkeypatch.setattr(appmod, "_core_principle_impacts", lambda principles: [])

    explain = appmod._build_core_dashboard_explain(
        state_snapshot={},
        week_days=[{"day_id": "2026-03-02", "planned_label": "Push", "final_label": "Push", "label": "Mo · Push"}],
        selected_day_id="2026-03-02",
        decision={"kind": "plan", "reason_codes": ["CORE_PUSH_GYM"]},
        core_info={
            "enabled": True,
            "actions": {},
            "execution": {"gym_mode": "push"},
            "principles": [],
        },
        is_light_day=False,
    )

    assert explain["headline"] == "CORE gibt heute grün für: Push"
    assert explain["decision_line"] == "Entscheidung: Push · CORE gibt Druck frei"
    assert explain["status_line"] == "Status: Freigabe · Datenlage Mittel"
