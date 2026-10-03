from __future__ import annotations

import json
from datetime import date, timedelta

import app as appmod
from ai import actions_v2
from liva_mcp.tool_registry import COACH_READ_MODES


def _synthetic_rows(days: int = 56):
    start = date(2026, 4, 5)
    nutrition = []
    recovery = []
    training = []
    for index in range(days):
        day = (start + timedelta(days=index)).isoformat()
        nutrition.append({
            "date_iso": day,
            "bodyweight_kg": 75 - index * 0.04,
            "calories": 2400 + (index % 4) * 100,
            "protein_g": 180,
            "carbs_g": 300,
            "fat_g": 65,
            "target_kcal": 2500,
            "mode": "cut",
            "source": "nutrition_plan",
            "context_ambiguous": False,
        })
        recovery.append({"date": day, "rmssd": 90 + index % 5, "nightPulse": 43, "actualSleepMinutes": 450, "flag": None})
        if index % 2 == 0:
            training.append({
                "id": index + 1,
                "date": day,
                "day_type": "Pull",
                "exercises": [{"name": "HighRows", "display_name": "HighRows", "sets": [{"set_number": 1, "weight": 70, "reps": 8, "rpe": 8}]}],
            })
    return {"nutrition": nutrition, "training": training, "recovery": recovery, "training_truncated": False}


def test_phase_modes_are_published_by_backend_and_mcp():
    assert {"analysis_phase_overview", "analysis_phase_detail"} <= actions_v2.LIVA_READ_MODES
    assert {"analysis_phase_overview", "analysis_phase_detail"} <= COACH_READ_MODES


def test_phase_overview_is_cross_domain_and_bounded(monkeypatch):
    monkeypatch.setattr(actions_v2, "_analysis_phase_source_rows", lambda *_args: _synthetic_rows())
    monkeypatch.setattr(
        appmod,
        "_analysis_phase_payload",
        lambda *_args, **_kwargs: {"selected": {"label": "Mini-Cut", "target_min": 2500, "target_max": 2500, "segments": []}},
    )

    payload = actions_v2._analysis_phase_overview_payload({"date_from": "2026-04-05", "date_to": "2026-05-30"})

    assert payload["phase_context"]["label"] == "Mini-Cut"
    assert payload["coverage"] == {
        "nutrition_days": 56,
        "weight_days": 56,
        "training_sessions": 28,
        "recovery_days": 56,
        "training_truncated": False,
    }
    assert len(payload["weeks"]) == 9  # partial opening week plus eight calendar weeks
    assert payload["zoom"]["mode"] == "analysis_phase_detail"
    assert payload["memory_workflow"]["recall_before_interpretation"] is True
    assert payload["memory_workflow"]["save_requires_explicit_user_confirmation"] is True
    assert payload["memory_workflow"]["save_flow"] == ["liva_memory_begin", "liva_memory_finish"]
    assert payload["payload_bytes"] < 64_000


def test_phase_detail_paginates_days_and_caps_exercise_points(monkeypatch):
    rows = _synthetic_rows(140)
    rows["training"] = rows["training"] * 5
    monkeypatch.setattr(actions_v2, "_analysis_phase_source_rows", lambda *_args: rows)

    days = actions_v2._analysis_phase_detail_payload({"date_from": "2026-04-05", "date_to": "2026-08-22", "focus": "days", "limit": 12, "offset": 12})
    exercises = actions_v2._analysis_phase_detail_payload({"date_from": "2026-04-05", "date_to": "2026-08-22", "focus": "exercises", "limit": 5})

    assert days["page"]["returned"] == 12
    assert days["page"]["has_more"] is True
    assert days["page"]["next_offset"] == 24
    assert len(exercises["exercises"][0]["set_points"]) == 120
    assert exercises["exercises"][0]["points_truncated"] is True
    assert len(json.dumps(exercises, ensure_ascii=False).encode("utf-8")) < 96_000


def test_phase_dispatch_forwards_overview_and_detail(monkeypatch):
    monkeypatch.setattr(actions_v2, "_analysis_phase_overview_payload", lambda payload: {"kind": "overview", "payload": payload})
    monkeypatch.setattr(actions_v2, "_analysis_phase_detail_payload", lambda payload: {"kind": "detail", "payload": payload})

    overview, overview_status = actions_v2._dispatch_liva_read({"mode": "analysis_phase_overview", "payload": {"date_from": "2026-04-05", "date_to": "2026-05-30"}})
    detail, detail_status = actions_v2._dispatch_liva_read({"mode": "analysis_phase_detail", "payload": {"date_from": "2026-04-05", "date_to": "2026-05-30", "focus": "training"}})

    assert overview_status == detail_status == 200
    assert overview["result"]["kind"] == "overview"
    assert detail["result"]["payload"]["focus"] == "training"
