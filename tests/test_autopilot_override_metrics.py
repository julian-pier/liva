from __future__ import annotations

from datetime import date, datetime
import importlib
import os


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def test_override_count_ignores_reorder_noise(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    plan = {"base_week": [{"strength_exercises": [], "run_sessions": []} for _ in range(7)]}
    noise = ["session_reordered"] * 1000
    out = appmod._compute_adherence_from_context(
        plan,
        today=date(2026, 2, 15),
        now_dt=datetime(2026, 2, 15, 12, 0, 0),
        logged_gym_dates=set(),
        performed_runs_7d=0,
        override_types_7d=noise,
        days_since_last_run=None,
    )
    assert out["override_count_7d"] == 0
    assert out["override_count_7d_whitelisted"] == 0
    assert out["override_types_7d"] == []


def test_override_count_counts_only_whitelisted_types(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    plan = {"base_week": [{"strength_exercises": [], "run_sessions": []} for _ in range(7)]}
    data = [
        "manual_override",
        "autopilot_override",
        "target_override",
        "session_reordered",
        None,
        "",
        "unknown_type",
    ]
    out = appmod._compute_adherence_from_context(
        plan,
        today=date(2026, 2, 15),
        now_dt=datetime(2026, 2, 15, 12, 0, 0),
        logged_gym_dates=set(),
        performed_runs_7d=0,
        override_types_7d=data,
        days_since_last_run=None,
    )
    assert out["override_count_7d"] == 3
    assert out["override_count_7d_whitelisted"] == 3
    assert set(out["override_types_7d"]) == {"manual_override", "autopilot_override", "target_override"}
