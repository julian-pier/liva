from __future__ import annotations

import importlib
import os
from datetime import date, timedelta

import pytest


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def _hours_to_minutes(hours_text: str | None) -> float | None:
    if not hours_text:
        return None
    hours_part, minutes_part = str(hours_text).split(":")
    return (int(hours_part) * 60) + int(minutes_part)


def _baseline_rows():
    today = date(2026, 5, 13)
    rows = []
    for offset in range(1, 15):
        day = today - timedelta(days=offset)
        rows.append(
            {
                "date": day.isoformat(),
                "time": f"{day.isoformat()}T06:00:00+00:00",
                "source": "polar",
                "usable": True,
                "rmssd": 138.0,
                "nightPulse": 42.0,
                "ansStatus": 0.4,
                "sleepScore": 80.0,
                "actualSleepMinutes": 8 * 60.0,
                "sleepMinutes": 8 * 60.0,
                "interruptionMinutes": 20.0,
                "remSleepMinutes": 100.0,
                "flag": None,
            }
        )
    return rows


def _score_case(appmod, **kwargs):
    today = date(2026, 5, 13)
    row = {
        "date": today.isoformat(),
        "time": f"{today.isoformat()}T06:00:00+00:00",
        "source": "polar",
        "usable": True,
        "rmssd": kwargs.get("rmssd"),
        "nightPulse": kwargs.get("pulse"),
        "ansStatus": kwargs.get("ans"),
        "sleepScore": kwargs.get("sleep_score"),
        "actualSleepMinutes": _hours_to_minutes(kwargs.get("sleep")),
        "sleepMinutes": _hours_to_minutes(kwargs.get("sleep")),
        "interruptionMinutes": kwargs.get("wake"),
        "remSleepMinutes": kwargs.get("rem"),
        "flag": kwargs.get("flag"),
    }
    rows = [row] + _baseline_rows()
    return appmod._recovery_score_for_row(row, rows)


def _score_case_with_history(appmod, row_day, history_rows, **kwargs):
    row = {
        "date": row_day.isoformat(),
        "time": f"{row_day.isoformat()}T06:00:00+00:00",
        "source": "polar",
        "usable": True,
        "rmssd": kwargs.get("rmssd"),
        "nightPulse": kwargs.get("pulse"),
        "ansStatus": kwargs.get("ans"),
        "sleepScore": kwargs.get("sleep_score"),
        "actualSleepMinutes": _hours_to_minutes(kwargs.get("sleep")),
        "sleepMinutes": _hours_to_minutes(kwargs.get("sleep")),
        "interruptionMinutes": kwargs.get("wake"),
        "remSleepMinutes": kwargs.get("rem"),
        "flag": kwargs.get("flag"),
    }
    rows = [row, *history_rows, *_baseline_rows()]
    return appmod._recovery_score_for_row(row, rows)


@pytest.mark.parametrize(
    ("name", "payload", "score_range", "statuses", "confidence"),
    [
        ("Kranker Crash", {"sleep": "6:14", "sleep_score": 58, "rmssd": 103, "pulse": 51, "ans": -9.56, "flag": "sick"}, (20, 35), {"rot"}, None),
        ("Kranker Beginn", {"sleep": "5:58", "sleep_score": 52, "rmssd": 124, "pulse": 46, "ans": -5.95, "flag": "sick"}, (35, 45), {"rot", "reduziert"}, None),
        ("Solider normaler Tag", {"sleep": "6:08", "sleep_score": 74, "rmssd": 101, "pulse": 39, "ans": 0}, (68, 78), {"vorsichtig", "bereit"}, None),
        ("Langer Schlaf niedriges RMSSD", {"sleep": "10:26", "sleep_score": 87, "rmssd": 94, "pulse": 41, "ans": 0}, (68, 78), {"vorsichtig", "bereit"}, None),
        ("Top Tag", {"sleep": "8:06", "sleep_score": 88, "rmssd": 150, "pulse": 39, "ans": 2.6}, (86, 95), {"stark"}, None),
        ("Sehr gut nur 6h50", {"sleep": "6:50", "sleep_score": 81, "rmssd": 145, "pulse": 40, "ans": 1.7}, (78, 86), {"bereit", "stark"}, None),
        ("8h HRV mies Puls hoch", {"sleep": "8:00", "sleep_score": 80, "rmssd": 95, "pulse": 49, "ans": -4.8}, (42, 55), {"reduziert"}, None),
        ("12h stabil", {"sleep": "12:00", "sleep_score": 90, "rmssd": 145, "pulse": 40, "ans": 1.0, "wake": 20}, (78, 86), {"bereit"}, None),
        ("12h krank wirkend", {"sleep": "12:00", "sleep_score": 63, "rmssd": 102, "pulse": 49, "ans": -4.0, "wake": 110}, (40, 55), {"reduziert"}, None),
        ("Alkohol gleicher Tag neutral", {"sleep": "7:10", "sleep_score": 65, "rmssd": 98, "pulse": 50, "ans": -6.5, "flag": "alcohol"}, (42, 55), {"reduziert"}, None),
        ("Alkohol gleicher Tag okayish", {"sleep": "7:40", "sleep_score": 78, "rmssd": 126, "pulse": 45, "ans": -2.0, "flag": "alcohol"}, (64, 74), {"vorsichtig", "bereit"}, None),
        ("Kurzschlaf 4h30", {"sleep": "4:30", "sleep_score": 45, "rmssd": 132, "pulse": 43, "ans": -1.0}, (45, 55), {"reduziert"}, None),
        ("5h42 aber ANS top", {"sleep": "5:42", "sleep_score": 72, "rmssd": 150, "pulse": 39, "ans": 2.0}, (65, 72), {"vorsichtig", "bereit"}, None),
        ("Schlaf gut Stress", {"sleep": "7:55", "sleep_score": 86, "rmssd": 112, "pulse": 46, "ans": -3.5}, (60, 68), {"vorsichtig"}, None),
        ("Schlaf gut Puls extrem hoch", {"sleep": "8:00", "sleep_score": 84, "rmssd": 130, "pulse": 52, "ans": -4.2}, (45, 55), {"reduziert"}, None),
        ("HRV hoch Puls niedrig Schlaf mies", {"sleep": "7:45", "sleep_score": 56, "rmssd": 158, "pulse": 39, "ans": 1.8, "wake": 100}, (62, 70), {"vorsichtig"}, None),
        ("RMSSD hoch Puls hoch", {"sleep": "7:45", "sleep_score": 76, "rmssd": 155, "pulse": 49, "ans": -1.5}, (58, 66), {"vorsichtig"}, None),
        ("RMSSD niedrig Puls niedrig", {"sleep": "7:50", "sleep_score": 78, "rmssd": 90, "pulse": 38, "ans": 0.5}, (68, 78), {"vorsichtig", "bereit"}, None),
        ("Viel Wachzeit", {"sleep": "7:50", "sleep_score": 66, "rmssd": 135, "pulse": 42, "ans": 0.2, "wake": 110}, (62, 70), {"vorsichtig"}, None),
        ("REM niedrig Rest gut", {"sleep": "8:00", "sleep_score": 82, "rmssd": 142, "pulse": 41, "ans": 1.0, "rem": 25}, (76, 84), {"bereit"}, None),
        ("Schlafscore top ANS schwach", {"sleep": "8:05", "sleep_score": 91, "rmssd": 105, "pulse": 48, "ans": -5.8}, (43, 52), {"reduziert"}, None),
        ("Krank Schlafscore gut", {"sleep": "8:10", "sleep_score": 86, "rmssd": 112, "pulse": 50, "ans": -6.0, "flag": "sick"}, (35, 45), {"rot", "reduziert"}, None),
        ("Krank fast normal", {"sleep": "8:00", "sleep_score": 82, "rmssd": 132, "pulse": 43, "ans": -0.5, "flag": "sick"}, (52, 58), {"vorsichtig", "reduziert"}, None),
        ("Ultra fit", {"sleep": "8:10", "sleep_score": 93, "rmssd": 170, "pulse": 37, "ans": 4.0}, (92, 100), {"stark"}, None),
        ("Normaler Schlaf HRV faellt", {"sleep": "7:55", "sleep_score": 79, "rmssd": 108, "pulse": 44, "ans": -1.8}, (63, 72), {"vorsichtig", "bereit"}, None),
    ],
)
def test_recovery_score_ranges(monkeypatch, name, payload, score_range, statuses, confidence):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, **payload)
    score = result["recovery_score"]
    assert score is not None, name
    assert score_range[0] <= score <= score_range[1], (name, score, result)
    assert result["recovery_status"] in statuses, (name, result)
    if confidence is not None:
        assert result["recovery_confidence"] in confidence, (name, result)


def test_recovery_score_handles_sparse_data(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, sleep="7:30")
    score = result["recovery_score"]
    assert score is None
    assert result["recovery_status"] is None
    assert result["recovery_confidence"] == "niedrig"
    assert result["recovery_reasons"] == []


def test_recovery_score_is_null_for_hrv_only_day_without_ans_or_sleep_data(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, rmssd=140, pulse=42)
    assert result["recovery_score"] is None
    assert result["recovery_status"] is None
    assert result["recovery_reasons"] == []


def test_recovery_score_exposes_reasons_and_components(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, sleep="6:14", sleep_score=58, rmssd=103, pulse=51, ans=-9.56, flag="sick", wake=85)
    assert result["recovery_status"] == "rot"
    assert result["recovery_reasons"]
    assert any("Krank-Flag" in reason for reason in result["recovery_reasons"])
    assert result["recovery_components"]["ans"] is not None
    assert result["recovery_components"]["sleep_pulse"] is not None
    assert result["recovery_baselines"]["rmssd"] == 138.0
    assert result["recovery_baselines"]["nightPulse"] == 42.0


def test_recovery_score_low_confidence_day_is_capped_and_never_stark(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, ans=0.2, sleep="7:40")
    assert result["recovery_confidence"] == "niedrig"
    assert result["recovery_score"] <= 79
    assert result["recovery_status"] != "stark"
    assert "Aussagekraft eingeschränkt wegen fehlender Daten" in result["recovery_reasons"]


def test_recovery_score_hrv_only_day_with_ans_still_has_no_global_score(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, ans=0.2, rmssd=140, pulse=42)
    assert result["recovery_score"] is None
    assert result["recovery_status"] is None
    assert result["recovery_reasons"] == []


def test_recovery_score_full_top_day_can_still_be_stark(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    result = _score_case(appmod, sleep="8:06", sleep_score=88, rmssd=150, pulse=39, ans=2.6, wake=18, rem=105)
    assert result["recovery_confidence"] == "hoch"
    assert result["recovery_score"] >= 85
    assert result["recovery_status"] == "stark"


def test_recovery_score_krank_days_stay_plausible(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    crash = _score_case(appmod, sleep="6:14", sleep_score=58, rmssd=103, pulse=51, ans=-9.56, flag="sick", wake=85)
    start = _score_case(appmod, sleep="5:58", sleep_score=52, rmssd=124, pulse=46, ans=-5.95, flag="sick", wake=50)
    assert 20 <= crash["recovery_score"] <= 35
    assert crash["recovery_status"] == "rot"
    assert 35 <= start["recovery_score"] <= 45
    assert start["recovery_status"] in {"rot", "reduziert"}


def test_recovery_score_alcohol_carryover_hits_next_two_days_not_same_day(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    alcohol_day = date(2026, 5, 13)
    base_kwargs = {"sleep": "7:40", "sleep_score": 78, "rmssd": 126, "pulse": 45, "ans": -2.0}

    same_day = _score_case(appmod, **base_kwargs, flag="alcohol")
    next_day = _score_case_with_history(
        appmod,
        alcohol_day + timedelta(days=1),
        [{
            "date": alcohol_day.isoformat(),
            "time": f"{alcohol_day.isoformat()}T06:00:00+00:00",
            "source": "polar",
            "usable": True,
            "flag": "alcohol",
            "rmssd": None,
            "nightPulse": None,
            "ansStatus": None,
            "sleepScore": None,
            "actualSleepMinutes": None,
            "sleepMinutes": None,
            "interruptionMinutes": None,
            "remSleepMinutes": None,
        }],
        **base_kwargs,
    )
    two_days_later = _score_case_with_history(
        appmod,
        alcohol_day + timedelta(days=2),
        [{
            "date": alcohol_day.isoformat(),
            "time": f"{alcohol_day.isoformat()}T06:00:00+00:00",
            "source": "polar",
            "usable": True,
            "flag": "alcohol",
            "rmssd": None,
            "nightPulse": None,
            "ansStatus": None,
            "sleepScore": None,
            "actualSleepMinutes": None,
            "sleepMinutes": None,
            "interruptionMinutes": None,
            "remSleepMinutes": None,
        }],
        **base_kwargs,
    )

    assert same_day["recovery_score"] is not None
    assert next_day["recovery_score"] is not None
    assert two_days_later["recovery_score"] is not None
    assert same_day["recovery_score"] > next_day["recovery_score"]
    assert same_day["recovery_score"] > two_days_later["recovery_score"] > next_day["recovery_score"]
    assert any("Vortag" in reason for reason in next_day["recovery_reasons"])
    assert any("Leichte Nachwirkung" in reason for reason in two_days_later["recovery_reasons"])
