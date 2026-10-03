from __future__ import annotations

import importlib

import database.connections as connections


def _load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    connections.CORE_DB = str(tmp_path / "core.sqlite3")
    connections.HRV_DB = str(tmp_path / "hrv.sqlite3")
    connections.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    connections.TRAINING_DB = str(tmp_path / "training.sqlite3")
    connections.PLANS_DB = str(tmp_path / "plans.sqlite3")
    connections.RUNS_DB = str(tmp_path / "runs.sqlite3")
    if "app" in importlib.sys.modules:
        return importlib.reload(importlib.sys.modules["app"])
    return importlib.import_module("app")


def test_core_morning_context_contains_endurance(monkeypatch, tmp_path):
    appmod = _load_app(monkeypatch, tmp_path)
    import core.core_training_card as training_card

    monkeypatch.setattr(
        training_card,
        "build_endurance_core_context",
        lambda day_iso, refresh=True: {
            "available": True,
            "connected": True,
            "reachable": True,
            "has_workout": True,
            "original_label": "Aerobic Development · Run · 70 min",
            "execution_mode": "ERGO",
            "final_summary": "70 min · Ergo · aerobe Grundlage · locker halten",
            "status": "YELLOW",
            "reason": "CORE ist vorsichtig, deshalb Zielbereich nicht überschreiten.",
            "conflicts": ["CORE vorsichtig"],
            "gpt_instruction": "Originalplan nicht mit Durchführung verwechseln.",
        },
    )
    payload = training_card.build_core_morning_context("2026-05-03")
    morning = payload["morning_context"]

    assert morning["endurance_approval"]["has_workout"] is True
    assert morning["endurance_approval"]["execution_mode"] == "ERGO"
    assert morning["decision_contract"]["include_endurance_approval"] is True
    assert morning["decision_contract"]["do_not_confuse_original_run_with_ergo_execution"] is True


def test_actions_endurance_approval_and_control(monkeypatch, tmp_path):
    appmod = _load_app(monkeypatch, tmp_path)
    client = appmod.app.test_client()
    import ai.actions_v2 as actions_v2

    monkeypatch.setattr(
        actions_v2,
        "build_endurance_core_context",
        lambda day_iso, refresh=True: {
            "available": True,
            "connected": True,
            "reachable": True,
            "has_workout": True,
            "original": {"label": "Aerobic Development · Run · 70 min"},
            "liva": {"execution_mode": "ERGO", "final_summary": "70 min · Ergo · aerobe Grundlage · locker halten"},
            "execution_mode": "ERGO",
            "final_summary": "70 min · Ergo · aerobe Grundlage · locker halten",
            "status": "YELLOW",
        },
    )
    monkeypatch.setattr(actions_v2, "set_execution_mode", lambda day_iso, intervals_event_id, execution_mode, source="actions_v2": {"execution_mode": execution_mode})

    get_resp = client.get("/api/v2/actions/endurance/approval?date=2026-05-03", headers={"Authorization": "Bearer test"})
    assert get_resp.status_code == 200
    assert get_resp.get_json()["endurance_approval"]["execution_mode"] == "ERGO"

    post_resp = client.post(
        "/api/v2/actions/endurance/control",
        headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        json={"command": "set_execution_mode", "date": "2026-05-03", "execution_mode": "RUN", "intervals_event_id": 107773845},
    )
    assert post_resp.status_code == 200
    assert post_resp.get_json()["ok"] is True
