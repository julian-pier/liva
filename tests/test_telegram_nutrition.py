from __future__ import annotations

import sqlite3

from database import connections
import integrations.telegram_hub as telegram_hub


def test_nutrition_ok_confirms_last_core_action(monkeypatch):
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day: {
            "date": day,
            "planned_meals": [{"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "items": [], "macros": {}}],
            "logged_totals": {"kcal": 0, "p": 0},
            "targets": {"kcal": 2500, "p": 180, "c": 300, "f": 70},
        },
    )
    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:abc", "raw_json": "{\"id\":12,\"payload\":{\"slot_id\":2}}"},
    )
    monkeypatch.setattr(
        telegram_hub,
        "mark_core_action_telegram_confirmed",
        lambda action_id: {"id": 12, "action_type": "shift_meal", "payload": {"slot_id": 2, "to_time": "09:45"}, "human": "Meal 2 verschoben"},
    )
    monkeypatch.setattr(telegram_hub, "_apply_confirmed_core_action_to_planned_status", lambda action, day_iso: None)
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 1, "chat_id": "1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 77, "text": "ok", "chat": {"id": "1"}})

    assert out["ok"] is True
    assert sent
    assert sent[0][0] == "nutrition"
    assert "CORE angepasst" in sent[0][1]


def test_nutrition_inbox_initializes_a_fresh_database_before_reading(tmp_path, monkeypatch):
    """The non-Flask worker must prepare schema, then keep reads read-only."""
    import nutrition.nutrition_planning_db as nutrition_db

    fresh_db = tmp_path / "nutrition.sqlite3"
    monkeypatch.setattr(connections, "NUTRITION_DB", str(fresh_db))
    nutrition_db._SCHEMA_READY_FOR.clear()
    monkeypatch.setattr(telegram_hub, "fetch_updates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-09-22")
    monkeypatch.setattr(
        telegram_hub,
        "_run_nutrition_periodic_tick",
        lambda day_iso: {"ok": True, "day": nutrition_db.get_logging_day_payload(day_iso)["date"]},
    )

    first = telegram_hub.process_nutrition_inbox(timeout=0)

    assert first["ok"] is True
    assert first["tick"]["day"] == "2026-09-22"
    with sqlite3.connect(fresh_db) as observer:
        assert observer.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nutrition_week_templates'"
        ).fetchone() == (1,)
        before_read = observer.execute("PRAGMA data_version").fetchone()[0]
        nutrition_db.get_logging_day_payload("2026-09-22")
        after_read = observer.execute("PRAGMA data_version").fetchone()[0]
        assert before_read == after_read

    second = telegram_hub.process_nutrition_inbox(timeout=0)

    assert second["ok"] is True


def test_parser_accepts_natural_time_update_and_html_output(monkeypatch):
    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day: {
            "date": day,
            "planned_meals": [
                {"slot_id": 4, "slot_index": 3, "time_text": "16:00", "title": "Nougat Bits", "status": "open", "items": [], "macros": {}}
            ],
            "logged_totals": {"kcal": 0, "p": 0, "c": 0, "f": 0},
            "targets": {"kcal": 2500, "p": 180, "c": 300, "f": 70},
            "remaining": {"kcal": 2500, "p": 180, "c": 300, "f": 70},
        },
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "set_planned_meal_status", lambda *args, **kwargs: ({}, None))
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 1, "chat_id": "1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 88, "text": "nougat bits auf 14:45", "chat": {"id": "1"}})

    assert out["ok"] is True
    assert sent
    assert "⏰ Meal" in sent[0]
    assert "<b>14:45</b>" in sent[0]


def test_runtime_payload_does_not_override_user_shift_with_stale_core_overlay(monkeypatch):
    monkeypatch.setattr(
        telegram_hub,
        "build_active_day_plan",
        lambda day_iso, base_payload=None: {
            "planned_meals": [
                {
                    "slot_id": 4,
                    "slot_index": 3,
                    "title": "Nougat Bits",
                    "time_text": "14:45",
                    "state": "accepted_adjustment",
                    "status": "adjusted_by_core",
                    "status_source": "telegram",
                    "last_mutation_type": "meal_shifted",
                    "macros": {"kcal": 300, "p": 10, "c": 40, "f": 10},
                }
            ]
        },
    )
    out = telegram_hub._effective_day_payload_for_runtime(
        "2026-03-11",
        base_payload={"planned_meals": []},
        evaluated={
            "planned_meals": [
                {"slot_id": 4, "time_text": "16:00", "shifted_time_text": "16:00", "status": "adjusted_by_core"}
            ]
        },
    )
    meal = out["planned_meals"][0]
    assert meal["time_text"] == "14:45"


def test_periodic_tick_consolidates_notifications(monkeypatch):
    monkeypatch.setattr(telegram_hub, "_expire_stale_nutrition_prompts", lambda day_iso: 0)
    monkeypatch.setattr(telegram_hub, "_close_stale_nutrition_contexts", lambda day_iso: 0)
    monkeypatch.setattr(telegram_hub, "_expire_nutrition_contexts", lambda day_iso: 0)
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_effective_day_payload_for_runtime", lambda day_iso, base_payload=None: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_is_new_day_hard_reset_window", lambda minute_window=20: False)
    monkeypatch.setattr(telegram_hub, "_maybe_send_pending_nutrition_prompt", lambda day_iso: {"ok": True, "sent": False})
    monkeypatch.setattr(telegram_hub, "_run_core_info_tick", lambda day_iso, day_payload=None: {"ok": True, "sent": 0})
    monkeypatch.setattr(telegram_hub, "_run_meal_reminder_tick", lambda day_iso, runtime_payload: {"ok": True, "sent": 1})

    out = telegram_hub._run_nutrition_periodic_tick("2026-03-11")

    assert out["ok"] is True
    assert int(out["reminder"]["sent"] or 0) == 1
    assert out["core_prompt"]["sent"] is False
    assert int(out["core_info"]["sent"] or 0) == 0
