from __future__ import annotations

import sqlite3

from nutrition import core_nutrition_daystate as daystate


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _base_payload() -> dict:
    return {
        "date": "2026-03-11",
        "targets": {"kcal": 2800, "p": 190, "c": 320, "f": 80},
        "logged_totals": {"kcal": 0, "p": 0, "c": 0, "f": 0},
        "planned_meals": [
            {
                "slot_id": 2,
                "slot_index": 1,
                "time_text": "09:30",
                "title": "Porridge Cookies&Cream & Banane",
                "status": "open",
                "status_source": "user",
                "items": [{"food_name": "Haferflocken", "amount": 150, "unit": "g"}],
                "macros": {"kcal": 936, "p": 54, "c": 134, "f": 16},
            }
        ],
    }


def test_switch_persists_after_rebuild(tmp_path, monkeypatch):
    ndb = tmp_path / "nutrition.sqlite3"
    sqlite3.connect(ndb).close()
    monkeypatch.setattr(daystate, "get_nutrition_db", lambda: _conn(ndb))

    daystate.ensure_core_day_state_schema()
    daystate.append_day_event(
        day_iso="2026-03-11",
        meal_slot_id=2,
        event_type="meal_switched",
        source="telegram",
        payload={
            "title": "Porridge Cinnamon Cereal & Marmelade",
            "items": [{"food_name": "Haferflocken", "amount": 200, "unit": "g"}],
            "macros": {"kcal": 1180, "p": 64, "c": 168, "f": 20},
            "time_text": "09:30",
        },
    )

    first = daystate.build_active_day_plan("2026-03-11", base_payload=_base_payload())
    second = daystate.build_active_day_plan("2026-03-11", base_payload=_base_payload())

    first_meal = first["planned_meals"][0]
    second_meal = second["planned_meals"][0]
    assert first_meal["title"] == "Porridge Cinnamon Cereal & Marmelade"
    assert second_meal["title"] == "Porridge Cinnamon Cereal & Marmelade"
    assert first_meal["stale_base_reference_blocked"] is True
    assert second_meal["stale_base_reference_blocked"] is True


def test_logged_reality_beats_switch_event(tmp_path, monkeypatch):
    ndb = tmp_path / "nutrition.sqlite3"
    sqlite3.connect(ndb).close()
    monkeypatch.setattr(daystate, "get_nutrition_db", lambda: _conn(ndb))

    daystate.ensure_core_day_state_schema()
    daystate.append_day_event(
        day_iso="2026-03-11",
        meal_slot_id=2,
        event_type="meal_switched",
        source="telegram",
        payload={"title": "Neue Variante"},
    )
    payload = _base_payload()
    payload["planned_meals"][0]["status"] = "logged"
    payload["planned_meals"][0]["logged_meal"] = {
        "title": "Geloggte Realitaet",
        "items": [{"food_name": "Haferflocken", "amount": 180, "unit": "g"}],
        "macros": {"kcal": 1000, "p": 60, "c": 140, "f": 18},
    }

    out = daystate.build_active_day_plan("2026-03-11", base_payload=payload)
    meal = out["planned_meals"][0]
    assert meal["state"] == "logged"
    assert meal["title"] == "Geloggte Realitaet"
    assert meal["source_of_truth"] == "logged_reality"


def test_only_one_active_suggestion_per_slot(tmp_path, monkeypatch):
    ndb = tmp_path / "nutrition.sqlite3"
    sqlite3.connect(ndb).close()
    monkeypatch.setattr(daystate, "get_nutrition_db", lambda: _conn(ndb))

    daystate.ensure_core_day_state_schema()
    daystate.append_day_event(
        day_iso="2026-03-11",
        meal_slot_id=2,
        event_type="suggestion_created",
        source="core",
        payload={"suggestion_key": "s1"},
        dedupe_key="s1",
    )
    daystate.append_day_event(
        day_iso="2026-03-11",
        meal_slot_id=2,
        event_type="suggestion_created",
        source="core",
        payload={"suggestion_key": "s2"},
        dedupe_key="s2",
    )

    events = daystate.list_day_events("2026-03-11")
    active = [e for e in events if e["event_type"] == "suggestion_created" and e["status"] == "active"]
    assert len(active) == 1
    assert active[0]["dedupe_key"] == "s2"

