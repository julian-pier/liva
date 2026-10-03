from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import integrations.telegram_hub as telegram_hub


def _core_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _plans_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _training_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _mk_core_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE core_decision_log (
                source_key TEXT PRIMARY KEY,
                outcome_text TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _mk_plans_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE gym_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                focus TEXT,
                plan_json TEXT,
                rules_json TEXT,
                blocks_json TEXT,
                is_active INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO gym_plans (title, focus, plan_json, rules_json, blocks_json, is_active, is_archived, created_at, updated_at)
            VALUES (
                'Hybrid',
                '',
                ?,
                '{}',
                '{}',
                1,
                0,
                '2026-03-03T08:00:00',
                '2026-03-03T08:00:00'
            )
            """,
            (
                """
                {
                  "meta": {"title": "Hybrid"},
                  "weeks": 8,
                  "days": [
                    {"day": "Mo", "events": [
                      {"kind": "gym", "title": "Upper A", "items": [
                        {"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 4, "reps": {"min": 6, "max": 10}}
                      ]}
                    ]},
                    {"day": "Di", "events": []},
                    {"day": "Mi", "events": []},
                    {"day": "Do", "events": []},
                    {"day": "Fr", "events": []},
                    {"day": "Sa", "events": []},
                    {"day": "So", "events": []}
                  ]
                }
                """,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _mk_training_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, date_iso TEXT, name TEXT, notes TEXT)")
        conn.execute(
            """
            CREATE TABLE exercises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id INTEGER,
                name TEXT,
                variation TEXT,
                device TEXT,
                laterality TEXT,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise_id INTEGER,
                set_number INTEGER,
                reps INTEGER,
                weight REAL,
                rpe REAL
            )
            """
        )

        payload = [
            ("2026-02-03", "Pull", "Enges Rudern", "Kurzhantel", "", [(1, 10, 42.5, 8.0), (2, 9, 42.5, 8.5)]),
            ("2026-02-10", "Pull", "Enges Rudern", "Kurzhantel", "", [(1, 10, 45.0, 8.0), (2, 9, 45.0, 8.5)]),
            ("2026-02-17", "Pull", "Enges Rudern", "Kurzhantel", "", [(1, 8, 45.0, 9.0), (2, 8, 45.0, 9.0)]),
            ("2026-02-24", "Pull", "Enges Rudern", "Kurzhantel", "", [(1, 8, 45.0, 9.0), (2, 8, 45.0, 9.0)]),
        ]
        for date_iso, session_name, name, variation, device, sets in payload:
            cur = conn.execute("INSERT INTO workouts (date_iso, name, notes) VALUES (?, ?, '')", (date_iso, session_name))
            workout_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, ?, ?, ?, '', '')",
                (workout_id, name, variation, device),
            )
            exercise_id = cur.lastrowid
            for set_number, reps, weight, rpe in sets:
                conn.execute(
                    "INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, ?, ?, ?, ?)",
                    (exercise_id, set_number, reps, weight, rpe),
                )
        conn.commit()
    finally:
        conn.close()


def test_process_training_reply_accepts_and_updates_plan(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_core_db(core_db)
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))
    monkeypatch.setattr(telegram_hub.importlib, "import_module", lambda name: type("FakeApp", (), {})())

    sent_messages = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent_messages.append((domain, text, kwargs)) or {"ok": True, "message_id": 77, "chat_id": "1"},
    )

    telegram_hub.ensure_telegram_schema()
    telegram_hub.upsert_training_proposal(
        source_key="major:exercise:incline",
        exercise_name="Schrägbankdrücken",
        current_variation="Smith",
        proposed_variation="Kurzhantel",
        reason_text="Plateau.",
        raw={"plan_context": {"item_id": "", "day": "Mo", "session_title": "Upper A", "exercise_name": "Schrägbankdrücken", "current_variation": "Smith"}},
    )
    telegram_hub.mark_training_proposal_sent("major:exercise:incline", message_id=55, chat_id="1")

    result = telegram_hub.process_training_reply_message(
        {
            "message_id": 99,
            "text": "4",
            "reply_to_message": {"message_id": 55},
        }
    )

    assert result["ok"] is True
    assert result["status"] == "accepted"

    conn = sqlite3.connect(plans_db)
    try:
        raw = conn.execute("SELECT plan_json FROM gym_plans WHERE is_active = 1 LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert "Kurzhantel" in raw
    assert "Smith" not in raw
    assert "\"core_change\"" in raw

    conn = sqlite3.connect(core_db)
    try:
        row = conn.execute(
            "SELECT status, response_text FROM telegram_pending_actions WHERE source_key='major:exercise:incline'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("accepted", "4")
    assert sent_messages


def test_process_training_reply_rejects_without_plan_change(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_core_db(core_db)
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))
    monkeypatch.setattr(telegram_hub.importlib, "import_module", lambda name: type("FakeApp", (), {})())
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True, "message_id": 88, "chat_id": "1"})

    telegram_hub.ensure_telegram_schema()
    telegram_hub.upsert_training_proposal(
        source_key="major:exercise:incline",
        exercise_name="Schrägbankdrücken",
        current_variation="Smith",
        proposed_variation="Kurzhantel",
        reason_text="Plateau.",
        raw={"plan_context": {"item_id": "", "day": "Mo", "session_title": "Upper A", "exercise_name": "Schrägbankdrücken", "current_variation": "Smith"}},
    )
    telegram_hub.mark_training_proposal_sent("major:exercise:incline", message_id=55, chat_id="1")

    result = telegram_hub.process_training_reply_message(
        {
            "message_id": 100,
            "text": "1",
            "reply_to_message": {"message_id": 55},
        }
    )

    assert result["ok"] is True
    assert result["status"] == "rejected"

    conn = sqlite3.connect(plans_db)
    try:
        raw = conn.execute("SELECT plan_json FROM gym_plans WHERE is_active = 1 LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert "Smith" in raw


def test_send_stateful_system_message_only_on_state_change(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    _mk_core_db(core_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    calls = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: calls.append((domain, text)) or {"ok": True, "message_id": 1, "chat_id": "1"},
    )

    first = telegram_hub.send_stateful_system_message(notice_key="system:backup", status="ok", message="Backups erfolgreich.", min_repeat_minutes=999)
    second = telegram_hub.send_stateful_system_message(notice_key="system:backup", status="ok", message="Backups erfolgreich.", min_repeat_minutes=999)
    third = telegram_hub.send_stateful_system_message(notice_key="system:backup", status="error", message="Backup fehlgeschlagen.", min_repeat_minutes=999)

    assert first["sent"] is True
    assert second["sent"] is False
    assert third["sent"] is True
    assert len(calls) == 2


def test_process_training_reply_core_decides_falls_back_to_variation(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_core_db(core_db)
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))
    monkeypatch.setattr(telegram_hub.importlib, "import_module", lambda name: type("FakeApp", (), {})())
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True, "message_id": 90, "chat_id": "1"})

    telegram_hub.ensure_telegram_schema()
    telegram_hub.upsert_training_proposal(
        source_key="major:exercise:incline-core",
        exercise_name="Schrägbankdrücken",
        current_variation="Smith",
        proposed_variation="Kurzhantel",
        reason_text="Plateau.",
        raw={
            "plan_context": {
                "item_id": "",
                "day": "Mo",
                "session_title": "Upper A",
                "exercise_name": "Schrägbankdrücken",
                "current_variation": "Smith",
                "history_count_recent": 9,
                "stalled_count": 4,
                "progress_count": 1,
                "latest_stalled": True,
                "trend": "flat",
                "rpe_list": [8.0, 8.0, 9.0],
            }
        },
    )
    telegram_hub.mark_training_proposal_sent("major:exercise:incline-core", message_id=56, chat_id="1")

    result = telegram_hub.process_training_reply_message(
        {
            "message_id": 101,
            "text": "5",
            "reply_to_message": {"message_id": 56},
        }
    )

    assert result["ok"] is True
    assert result["status"] == "accepted"

    conn = sqlite3.connect(plans_db)
    try:
        raw = conn.execute("SELECT plan_json FROM gym_plans WHERE is_active = 1 LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert "Kurzhantel" in raw
    assert "\"core_change\"" in raw


def test_process_training_reply_uses_latest_pending_when_reply_target_is_not_proposal(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_core_db(core_db)
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))
    monkeypatch.setattr(telegram_hub.importlib, "import_module", lambda name: type("FakeApp", (), {})())
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True, "message_id": 90, "chat_id": "1"})

    telegram_hub.ensure_telegram_schema()
    telegram_hub.upsert_training_proposal(
        source_key="major:exercise:incline-latest",
        exercise_name="Schrägbankdrücken",
        current_variation="Smith",
        proposed_variation="Kurzhantel",
        reason_text="Plateau.",
        raw={"plan_context": {"item_id": "", "day": "Mo", "session_title": "Upper A", "exercise_name": "Schrägbankdrücken", "current_variation": "Smith"}},
    )
    telegram_hub.mark_training_proposal_sent("major:exercise:incline-latest", message_id=56, chat_id="1")

    result = telegram_hub.process_training_reply_message(
        {
            "message_id": 102,
            "text": "4",
            "reply_to_message": {"message_id": 99999},
            "chat": {"id": "1"},
        }
    )

    assert result["ok"] is True
    assert result["status"] == "accepted"

    conn = sqlite3.connect(plans_db)
    try:
        raw = conn.execute("SELECT plan_json FROM gym_plans WHERE is_active = 1 LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert "Kurzhantel" in raw

def test_choose_core_action_prefers_rpe_down_for_hard_downtrend():
    action = telegram_hub._choose_core_action(
        {
            "current_variation": "Smith",
            "proposed_variation": "Kurzhantel",
        },
        {
            "history_count_recent": 6,
            "stalled_count": 2,
            "progress_count": 1,
            "latest_stalled": True,
            "trend": "down",
            "rpe_list": [8.5, 8.5, 9.0],
        },
    )

    assert action == "rpe_down"


def test_choose_core_action_prefers_variation_for_long_repeated_plateau():
    action = telegram_hub._choose_core_action(
        {
            "current_variation": "Smith",
            "proposed_variation": "Kurzhantel",
        },
        {
            "history_count_recent": 9,
            "stalled_count": 4,
            "progress_count": 1,
            "latest_stalled": True,
            "trend": "flat",
            "rpe_list": [7.5, 8.0, 8.0],
        },
    )

    assert action == "variation"


def test_process_training_query_message_answers_exercise_summary(tmp_path, monkeypatch):
    core_db = tmp_path / "core.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_core_db(core_db)
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)

    monkeypatch.setattr(telegram_hub, "get_core_db", lambda: _core_conn(core_db))
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))

    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 111, "chat_id": "1"},
    )

    result = telegram_hub.process_training_query_message({"message_id": 500, "text": "Enges Rudern, KH"})

    assert result["ok"] is True
    assert sent
    assert sent[0][0] == "training"
    assert "Enges Rudern (Kurzhantel)" in sent[0][1]
    assert "Übungsreport" in sent[0][1]
    assert sent[0][2]["reply_to_message_id"] == 500


def test_process_training_query_message_help_layout(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 901, "chat_id": "1"},
    )

    result = telegram_hub.process_training_query_message({"message_id": 601, "text": "help"})

    assert result["ok"] is True
    assert sent
    assert "🧭 Training Chat · Commands" in sent[0][1]
    assert "Nicht fürs Loggen" in sent[0][1]


def test_process_training_query_message_status_routes_renderer(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_hub, "_training_status_text", lambda: "STATUS-RENDER")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 902, "chat_id": "1"},
    )

    result = telegram_hub.process_training_query_message({"message_id": 602, "text": "status"})

    assert result["ok"] is True
    assert sent[0][1] == "STATUS-RENDER"


def test_training_core_status_text_lists_reply_options(monkeypatch):
    monkeypatch.setattr(
        telegram_hub,
        "_pending_training_proposal",
        lambda: {
            "exercise_name": "Schrägbankdrücken",
            "current_variation": "Smith",
            "proposed_variation": "Kurzhantel",
            "reason_text": "Signalqualität zuletzt flach.",
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_next_plan_slot_context",
        lambda include_today=True: {"day": "Do", "session_title": "Upper B", "has_override": False, "items": []},
    )

    text = telegram_hub._training_core_status_text()

    assert "Antwortoptionen" in text
    assert "1 · Standard unverändert" in text
    assert "2 · Rep-Range anpassen" in text
    assert "3 · RPE-Cap konservativer" in text
    assert "4 · Variante wechseln (Kurzhantel)" in text
    assert "5 · CORE-Auswahl" in text


def test_weekly_training_check_text_avoids_coach_phrases(tmp_path, monkeypatch):
    plans_db = tmp_path / "plans.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    _mk_plans_db(plans_db)
    _mk_training_db(training_db)
    monkeypatch.setattr(telegram_hub, "get_plans_db", lambda: _plans_conn(plans_db))
    monkeypatch.setattr(telegram_hub, "get_training_db", lambda: _training_conn(training_db))

    text = telegram_hub._weekly_training_check_text()

    assert "📊 Wochencheck" in text
    assert "Ziel:" not in text
    assert "Falls müde" not in text


def test_stale_core_nutrition_action_blocks_skipped_meal():
    action = {
        "action_type": "adjust_servings",
        "payload": {"slot_id": 2, "from_servings": 1.0, "to_servings": 1.2},
    }
    day_payload = {
        "planned_meals": [
            {"slot_id": 2, "time_text": "09:30", "status": "skipped", "title": "Porridge"},
        ]
    }
    stale, reason = telegram_hub._is_stale_core_nutrition_action(action, day_payload, now_min=22 * 60)
    assert stale is True
    assert reason.startswith("meal_status_")


def test_stale_core_nutrition_action_allows_active_window():
    action = {
        "action_type": "adjust_servings",
        "payload": {"slot_id": 3, "from_servings": 1.0, "to_servings": 1.1},
    }
    day_payload = {
        "planned_meals": [
            {"slot_id": 3, "time_text": "18:00", "status": "open", "title": "Meal"},
        ]
    }
    stale, reason = telegram_hub._is_stale_core_nutrition_action(action, day_payload, now_min=18 * 60 + 20)
    assert stale is False
    assert reason == ""


def test_fresh_core_action_for_push_recent_true():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=8)).replace(microsecond=0).isoformat()
    action = {"updated_at": recent}
    assert telegram_hub._is_fresh_core_action_for_push(action, max_age_minutes=35) is True


def test_fresh_core_action_for_push_old_false():
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat()
    action = {"updated_at": old}
    assert telegram_hub._is_fresh_core_action_for_push(action, max_age_minutes=35) is False


def test_meal_reminder_dedupe_key_stable_for_same_slot_and_time():
    meal_a = {
        "slot_id": 2,
        "title": "Porridge",
        "time_text": "09:30",
        "status": "open",
        "items": [{"food_name": "Haferflocken", "amount": 120, "unit": "g"}],
    }
    meal_b = {
        "slot_id": 2,
        "title": "Porridge",
        "time_text": "09:30",
        "status": "manual_override",
        "items": [{"food_name": "Haferflocken", "amount": 150, "unit": "g"}],
    }
    key_a = telegram_hub._meal_reminder_dedupe_key("2026-03-11", meal_a)
    key_b = telegram_hub._meal_reminder_dedupe_key("2026-03-11", meal_b)
    assert key_a == key_b


def test_effective_day_payload_for_runtime_prefers_core_planned_meals(monkeypatch):
    base = {"planned_meals": [{"slot_id": 4, "time_text": "16:00", "title": "NicNac's"}]}
    evaluated = {"planned_meals": [{"slot_id": 4, "time_text": "14:45", "title": "NicNac's", "status": "adjusted_by_core"}]}
    monkeypatch.setattr(
        telegram_hub,
        "build_active_day_plan",
        lambda day_iso, base_payload=None: {"planned_meals": [{"slot_id": 4, "slot_index": 3, "time_text": "16:00", "title": "NicNac's", "status": "open"}]},
    )
    out = telegram_hub._effective_day_payload_for_runtime("2026-03-11", base_payload=base, evaluated=evaluated)
    assert out["planned_meals"][0]["time_text"] == "14:45"
    assert out["planned_meals"][0]["slot_id"] == 4


def test_meal_reminder_uses_switched_active_title(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_hub, "_claim_message_log_slot", lambda **kwargs: True)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 42, "chat_id": "c1"},
    )
    monkeypatch.setattr(telegram_hub, "_open_meal_context", lambda **kwargs: {"id": 99})
    monkeypatch.setattr(telegram_hub, "_finalize_message_log", lambda **kwargs: None)

    out = telegram_hub._maybe_send_meal_reminder(
        "2026-03-11",
        {
            "slot_id": 2,
            "slot_index": 1,
            "time_text": "09:30",
            "title": "Porridge Cinnamon Cereal & Marmelade",
            "state": "switched",
            "reminder_eligible": True,
            "items": [{"food_name": "Haferflocken", "amount": 200, "unit": "g"}],
            "macros": {"kcal": 1180, "p": 64, "c": 168, "f": 20},
        },
    )
    assert out["ok"] is True
    assert out["sent"] is True
    assert sent
    assert "Cinnamon Cereal" in sent[0]
    assert "Cookies&Cream" not in sent[0]


def test_meal_reminder_does_not_send_when_claim_already_exists(monkeypatch):
    calls = {"sent": 0}

    monkeypatch.setattr(telegram_hub, "_claim_message_log_slot", lambda **kwargs: False)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: calls.__setitem__("sent", calls["sent"] + 1) or {"ok": True, "message_id": 42, "chat_id": "c1"},
    )

    out = telegram_hub._maybe_send_meal_reminder(
        "2026-03-11",
        {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "items": [], "macros": {}},
    )

    assert out["ok"] is True
    assert out["sent"] is False
    assert out["reason"] == "already_sent"
    assert calls["sent"] == 0


def test_meal_reminder_releases_claim_when_send_fails(monkeypatch):
    released = {"keys": []}

    monkeypatch.setattr(telegram_hub, "_claim_message_log_slot", lambda **kwargs: True)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: {"ok": False, "error": "boom"},
    )
    monkeypatch.setattr(telegram_hub, "_release_message_log_claim", lambda dedupe_key: released["keys"].append(dedupe_key))

    out = telegram_hub._maybe_send_meal_reminder(
        "2026-03-11",
        {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "items": [], "macros": {}},
    )

    assert out["ok"] is False
    assert out["sent"] is False
    assert released["keys"] == ["nutrition:reminder:2026-03-11:2"]


def test_run_meal_reminder_tick_skips_when_minute_bucket_already_claimed(monkeypatch):
    monkeypatch.setattr(telegram_hub, "_claim_notice_minute_bucket", lambda **kwargs: False)
    called = {"count": 0}
    monkeypatch.setattr(telegram_hub, "_maybe_send_meal_reminder", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1) or {"ok": True, "sent": True})

    out = telegram_hub._run_meal_reminder_tick(
        "2026-03-11",
        {"planned_meals": [{"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open"}]},
    )

    assert out["ok"] is True
    assert out["sent"] == 0
    assert out["reason"] == "minute_bucket_already_processed"
    assert called["count"] == 0


def test_day_overview_hides_old_title_after_replace():
    text = telegram_hub._nutrition_day_meals_overview_text(
        {
            "planned_meals": [
                {
                    "slot_id": 2,
                    "slot_index": 1,
                    "time_text": "09:30",
                    "title": "Porridge Cinnamon Cereal & Marmelade",
                    "state": "replaced",
                    "state_label": "ersetzt",
                    "base_title": "Porridge Cookies&Cream & Banane",
                    "items": [],
                    "macros": {"kcal": 1180, "p": 64, "c": 168, "f": 20},
                }
            ],
            "remaining": {"kcal": 1200, "p": 50, "c": 160, "f": 35},
        }
    )
    assert "Cinnamon Cereal" in text
    assert "Cookies&Cream" not in text


def test_apply_confirmed_core_action_to_planned_status_shift(monkeypatch):
    calls = []

    def _fake_set_status(slot_id, **kwargs):
        calls.append((slot_id, kwargs))
        return {"ok": True}, None

    monkeypatch.setattr(telegram_hub, "set_planned_meal_status", _fake_set_status)
    telegram_hub._apply_confirmed_core_action_to_planned_status(
        {
            "id": 77,
            "action_type": "shift_meal",
            "payload": {"slot_id": 4, "to_time": "14:45"},
        },
        day_iso="2026-03-11",
    )
    assert calls
    slot_id, kwargs = calls[0]
    assert slot_id == 4
    assert kwargs["status"] == "shifted"
    assert kwargs["shifted_time_text"] == "14:45"
    assert kwargs["status_source"] == "core"


def test_run_nutrition_periodic_tick_uses_effective_payload(monkeypatch):
    captured = {"reminder_payload": None, "core_info_payload": None}

    monkeypatch.setattr(telegram_hub, "_expire_nutrition_contexts", lambda day_iso: 2)
    monkeypatch.setattr(telegram_hub, "_maybe_send_pending_nutrition_prompt", lambda day_iso: {"ok": True, "sent": True, "reason": "sent"})
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso: {"planned_meals": [{"slot_id": 4, "time_text": "14:45", "title": "NicNac's"}]},
    )

    def _fake_reminder(day_iso, day_payload):
        captured["reminder_payload"] = day_payload
        return {"ok": True, "sent": 1}

    def _fake_core_info(day_iso, day_payload=None):
        captured["core_info_payload"] = day_payload
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(telegram_hub, "_run_meal_reminder_tick", _fake_reminder)
    monkeypatch.setattr(telegram_hub, "_run_core_info_tick", _fake_core_info)

    out = telegram_hub._run_nutrition_periodic_tick("2026-03-11")
    assert out["ok"] is True
    assert out["expired_contexts"] == 2
    assert out["reminder"]["sent"] == 1
    assert out["core_prompt"]["sent"] is True
    assert out["core_info"]["sent"] == 1
    assert captured["reminder_payload"]["planned_meals"][0]["time_text"] == "14:45"
    assert captured["core_info_payload"]["planned_meals"][0]["time_text"] == "14:45"


def test_process_nutrition_query_ok_confirms_shift_and_updates_slot(monkeypatch):
    calls = {"set_status": [], "sent": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {"planned_meals": [{"slot_id": 4, "time_text": "16:00", "title": "NicNac's", "status": "open"}]},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_find_pending_nutrition_prompt_by_outbound",
        lambda message_id: {"source_key": "nutrition-core:2026-03-11:shift4", "raw_json": "{\"id\":77}"},
    )
    monkeypatch.setattr(telegram_hub, "_mark_nutrition_prompt_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        telegram_hub,
        "mark_core_action_telegram_confirmed",
        lambda action_id: {
            "id": 77,
            "action_type": "shift_meal",
            "human": "NicNac's (16:00) auf 14:45 verschieben",
            "payload": {"slot_id": 4, "to_time": "14:45"},
        },
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)

    def _fake_set_status(slot_id, **kwargs):
        calls["set_status"].append((slot_id, kwargs))
        return {"ok": True}, None

    monkeypatch.setattr(telegram_hub, "set_planned_meal_status", _fake_set_status)
    monkeypatch.setattr(telegram_hub, "_parse_meal_reference", lambda text, day: (0, None))

    def _fake_send(domain, text, **kwargs):
        calls["sent"].append((domain, text, kwargs))
        return {"ok": True, "message_id": 9001, "chat_id": kwargs.get("chat_id") or "c1"}

    monkeypatch.setattr(telegram_hub, "send_domain_message", _fake_send)

    out = telegram_hub.process_nutrition_query_message(
        {
            "message_id": 501,
            "text": "ok",
            "chat": {"id": "c1"},
            "reply_to_message": {"message_id": 777},
        }
    )

    assert out["ok"] is True
    assert calls["set_status"]
    slot_id, kwargs = calls["set_status"][0]
    assert slot_id == 4
    assert kwargs["status"] == "shifted"
    assert kwargs["shifted_time_text"] == "14:45"
    assert kwargs["status_source"] == "core"
    assert calls["sent"]
    assert "CORE angepasst" in calls["sent"][0][1]


def test_process_nutrition_query_help(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_effective_day_payload_for_runtime", lambda day_iso, base_payload=None, evaluated=None: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 9002, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 510, "text": "help", "chat": {"id": "c1"}})

    assert out["ok"] is True
    assert sent
    assert sent[0][0] == "nutrition"
    assert "Nutrition Chat · Commands" in sent[0][1]
    assert "switch meal 2 to porridge cinnamon" in sent[0][1]


def test_process_nutrition_inbox_flow_aggregates_tick_and_updates(monkeypatch):
    monkeypatch.setattr(
        telegram_hub,
        "_run_nutrition_periodic_tick",
        lambda day_iso: {"ok": True, "expired_contexts": 0, "reminder": {"ok": True, "sent": 1}, "core_prompt": {"ok": True, "sent": False}, "core_info": {"ok": True, "sent": 0}},
    )
    monkeypatch.setattr(
        telegram_hub,
        "fetch_updates",
        lambda domain, timeout=0: [
            {"message": {"message_id": 11, "text": "ok", "chat": {"id": "c1"}}},
            {"message": {"message_id": 12, "text": "", "chat": {"id": "c1"}}},
        ],
    )
    monkeypatch.setattr(
        telegram_hub,
        "process_nutrition_query_message",
        lambda message: {"ok": True, "status": "answered"} if str(message.get("text") or "").strip() else {"ok": False, "ignored": True, "error": "empty_message"},
    )

    out = telegram_hub.process_nutrition_inbox(timeout=0)
    assert out["ok"] is True
    assert out["updates"] == 2
    assert out["processed"] == 1
    assert out["ignored"] == 1
    assert out["errors"] == []


def test_process_nutrition_inbox_processes_updates_before_tick(monkeypatch):
    order = []
    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "fetch_updates",
        lambda domain, timeout=0: [{"message": {"message_id": 77, "text": "ok", "chat": {"id": "c1"}}}],
    )

    def _fake_process(message):
        order.append("process_message")
        return {"ok": True, "status": "answered"}

    def _fake_tick(day_iso):
        order.append("tick")
        return {"ok": True, "expired_contexts": 0, "reminder": {"ok": True, "sent": 0}, "core_prompt": {"ok": True, "sent": 0}, "core_info": {"ok": True, "sent": 0}}

    monkeypatch.setattr(telegram_hub, "process_nutrition_query_message", _fake_process)
    monkeypatch.setattr(telegram_hub, "_run_nutrition_periodic_tick", _fake_tick)

    out = telegram_hub.process_nutrition_inbox(timeout=0)
    assert out["ok"] is True
    assert order == ["process_message", "tick"]


def test_process_nutrition_query_ok_without_reply_confirms_unanswered_prompt(monkeypatch):
    calls = {"mark_core": [], "responses": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k1", "raw_json": "{\"id\":55}"},
    )

    def _fake_mark_core(action_id):
        calls["mark_core"].append(action_id)
        return {"id": 55, "action_type": "shift_meal", "human": "Shift passt", "payload": {"slot_id": 4, "to_time": "14:45"}}

    monkeypatch.setattr(telegram_hub, "mark_core_action_telegram_confirmed", _fake_mark_core)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(telegram_hub, "_parse_meal_reference", lambda text, day: (0, None))
    monkeypatch.setattr(telegram_hub, "_apply_confirmed_core_action_to_planned_status", lambda action, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_mark_nutrition_prompt_response",
        lambda source_key, **kwargs: calls["responses"].append((source_key, kwargs.get("status"))),
    )
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda domain, text, **kwargs: {"ok": True, "message_id": 1, "chat_id": "c1"})

    out = telegram_hub.process_nutrition_query_message({"message_id": 71, "text": "ok", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["mark_core"] == [55]
    assert calls["responses"] == [("nutrition-core:2026-03-11:k1", "confirmed")]


def test_process_nutrition_query_nein_without_reply_rejects_unanswered_prompt(monkeypatch):
    calls = {"reject_core": [], "responses": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k2", "raw_json": "{\"id\":66}"},
    )

    def _fake_reject_core(action_id):
        calls["reject_core"].append(action_id)
        return {"id": 66, "action_type": "adjust_servings", "human": "Reject passt", "payload": {"slot_id": 4}}

    monkeypatch.setattr(telegram_hub, "mark_core_action_rejected", _fake_reject_core)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(telegram_hub, "_parse_meal_reference", lambda text, day: (0, None))
    monkeypatch.setattr(
        telegram_hub,
        "_mark_nutrition_prompt_response",
        lambda source_key, **kwargs: calls["responses"].append((source_key, kwargs.get("status"))),
    )
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda domain, text, **kwargs: {"ok": True, "message_id": 1, "chat_id": "c1"})

    out = telegram_hub.process_nutrition_query_message({"message_id": 72, "text": "nein", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["reject_core"] == [66]
    assert calls["responses"] == [("nutrition-core:2026-03-11:k2", "rejected")]


def test_process_nutrition_query_ok_reply_to_meal_does_not_confirm_core(monkeypatch):
    calls = {"mark_core": 0, "log_meal": 0, "sent_texts": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k1", "raw_json": "{\"id\":55}"},
    )
    monkeypatch.setattr(
        telegram_hub,
        "mark_core_action_telegram_confirmed",
        lambda action_id: calls.__setitem__("mark_core", calls["mark_core"] + 1) or {"id": action_id},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {"id": 9, "meal_slot_id": 4, "meal_title": "Meal 4", "referenced_time": None, "raw": {"pending_macros": {}}},
    )
    monkeypatch.setattr(telegram_hub, "_parse_meal_reference", lambda text, day: (0, None))
    monkeypatch.setattr(
        telegram_hub,
        "_log_context_meal",
        lambda context, day_iso, chat_id, **kwargs: (calls.__setitem__("log_meal", calls["log_meal"] + 1) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_close_meal_context", lambda context_id, reason="": None)
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: calls["sent_texts"].append(text) or {"ok": True, "message_id": 1, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message(
        {
            "message_id": 80,
            "text": "ok",
            "chat": {"id": "c1"},
            "reply_to_message": {"message_id": 242, "text": "Denk dran: gleich Meal 1"},
        }
    )
    assert out["ok"] is True
    assert calls["mark_core"] == 0
    assert calls["log_meal"] == 0
    assert any("Zum Loggen bitte 'done' schreiben." in txt for txt in calls["sent_texts"])


def test_process_nutrition_query_done_reply_to_meal_logs_context(monkeypatch):
    calls = {"mark_core": 0, "log_meal": 0, "sent_texts": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k1", "raw_json": "{\"id\":55}"},
    )
    monkeypatch.setattr(
        telegram_hub,
        "mark_core_action_telegram_confirmed",
        lambda action_id: calls.__setitem__("mark_core", calls["mark_core"] + 1) or {"id": action_id},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 9,
            "meal_slot_id": 4,
            "meal_title": "Meal 4",
            "referenced_time": None,
            "raw": {"pending_macros": {}, "pending_items": [{"food_name": "Haferflocken", "amount": 150, "unit": "g"}]},
        },
    )
    monkeypatch.setattr(telegram_hub, "_parse_meal_reference", lambda text, day: (0, None))
    monkeypatch.setattr(
        telegram_hub,
        "_log_context_meal",
        lambda context, day_iso, chat_id, **kwargs: (calls.__setitem__("log_meal", calls["log_meal"] + 1) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_close_meal_context", lambda context_id, reason="": None)
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: calls["sent_texts"].append(text) or {"ok": True, "message_id": 1, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message(
        {
            "message_id": 81,
            "text": "done",
            "chat": {"id": "c1"},
            "reply_to_message": {"message_id": 247, "text": "Denk dran: gleich Meal 2"},
        }
    )
    assert out["ok"] is True
    assert calls["mark_core"] == 0
    assert calls["log_meal"] == 1
    assert any("- Haferflocken 150 g" in txt for txt in calls["sent_texts"])


def test_process_nutrition_query_meals_returns_db_meals_list(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "list_meal_templates",
        lambda query=None, limit=500: [
            {"title": "PBJ Brot", "kcal_per_serving": 349, "p_per_serving": 11, "c_per_serving": 52, "f_per_serving": 10},
            {"title": "Porridge", "kcal_per_serving": 880, "p_per_serving": 53, "c_per_serving": 119, "f_per_serving": 16},
        ],
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 301, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 91, "text": "meals", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert sent
    assert sent[0][0] == "nutrition"
    body = sent[0][1]
    assert "Meals (Datenbank)" in body
    assert "1. PBJ Brot" in body
    assert "349 kcal · P 11 / C 52 / F 10" in body
    assert "2. Porridge" in body
    assert "880 kcal · P 53 / C 119 / F 16" in body


def test_process_nutrition_query_foods_returns_db_foods_list(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "list_foods",
        lambda query=None, limit=500, favorites_only=False: [
            {"name": "Haferflocken (zart)", "unit_default": "g", "kcal_per_100": 370, "p_per_100": 13, "c_per_100": 58, "f_per_100": 7},
            {"name": "Milch", "unit_default": "ml", "kcal_per_100": 48, "p_per_100": 3.4, "c_per_100": 4.8, "f_per_100": 1.5},
        ],
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 311, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 100, "text": "foods", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert sent
    body = sent[0][1]
    assert "Foods (Datenbank)" in body
    assert "1. Haferflocken (zart) (g/100)" in body
    assert "370 kcal · P 13 / C 58 / F 7" in body
    assert "2. Milch (ml/100)" in body
    assert "48 kcal · P 3 / C 5 / F 2" in body


def test_process_nutrition_query_search_meal_returns_matching_templates(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "list_meal_templates",
        lambda query=None, limit=100: [
            {"title": "Porridge Chocolate & Marmelade", "kcal_per_serving": 880, "p_per_serving": 53, "c_per_serving": 119, "f_per_serving": 16},
            {"title": "Porridge Vanille & Apfelmus", "kcal_per_serving": 910, "p_per_serving": 55, "c_per_serving": 124, "f_per_serving": 16},
        ],
    )
    monkeypatch.setattr(telegram_hub, "list_foods", lambda query=None, limit=100, favorites_only=False: [])
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 312, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 111, "text": 'search meal "Porridge"', "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert 'Search Meals: "Porridge"' in body
    assert "1. Porridge Chocolate & Marmelade" in body
    assert "2. Porridge Vanille & Apfelmus" in body
    assert "880 kcal · P 53 / C 119 / F 16" in body


def test_process_nutrition_query_search_food_returns_matching_foods(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "list_meal_templates", lambda query=None, limit=100: [])
    monkeypatch.setattr(
        telegram_hub,
        "list_foods",
        lambda query=None, limit=100, favorites_only=False: [
            {"name": "Haferflocken (zart)", "unit_default": "g", "kcal_per_100": 370, "p_per_100": 13, "c_per_100": 58, "f_per_100": 7},
            {"name": "Haferflocken kernig", "unit_default": "g", "kcal_per_100": 365, "p_per_100": 12, "c_per_100": 57, "f_per_100": 7},
        ],
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 313, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 112, "text": 'search food "Haferflocken"', "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert 'Search Foods: "Haferflocken"' in body
    assert "1. Haferflocken (zart) (g/100)" in body
    assert "2. Haferflocken kernig (g/100)" in body
    assert "370 kcal · P 13 / C 58 / F 7" in body


def test_process_nutrition_query_search_generic_returns_meals_and_foods(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "list_meal_templates",
        lambda query=None, limit=60: [{"title": "Hähnchen mit Reis", "kcal_per_serving": 700, "p_per_serving": 60, "c_per_serving": 70, "f_per_serving": 18}],
    )
    monkeypatch.setattr(
        telegram_hub,
        "list_foods",
        lambda query=None, limit=60, favorites_only=False: [{"name": "Hähnchenbrust", "unit_default": "g", "kcal_per_100": 110, "p_per_100": 23, "c_per_100": 0, "f_per_100": 1}],
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 314, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 113, "text": 'search "Hähnchen"', "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert 'Search: "Hähnchen"' in body
    assert "Meals" in body
    assert "1. Hähnchen mit Reis" in body
    assert "Foods" in body
    assert "1. Hähnchenbrust (g/100)" in body


def test_process_nutrition_query_today_returns_day_overview(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {
                    "slot_id": 1,
                    "slot_index": 0,
                    "time_text": "06:40",
                    "title": "PBJ Brot",
                    "status": "open",
                    "macros": {"kcal": 349, "protein_g": 11, "carbs_g": 52, "fat_g": 10},
                    "items": [{"food_name": "Brot", "amount": 1, "unit": "pcs"}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: base_payload,
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 309, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 99, "text": "today", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert sent
    assert "Plan ab jetzt" in sent[0][1]
    assert "- 06:40 · PBJ Brot (offen)" in sent[0][1]


def test_process_nutrition_query_today_sorted_and_numbered_by_time(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 9, "slot_index": 5, "time_text": "17:17", "title": "Abendessen", "status": "open", "macros": {}, "items": []},
                {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "macros": {}, "items": []},
                {"slot_id": 1, "slot_index": 0, "time_text": "06:40", "title": "PBJ Brot", "status": "open", "macros": {}, "items": []},
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: base_payload,
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 302, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 92, "text": "today", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert body.find("- 06:40 · PBJ Brot (offen)") < body.find("- 09:30 · Porridge (offen)")
    assert body.find("- 09:30 · Porridge (offen)") < body.find("- 17:17 · Abendessen (offen)")


def test_process_nutrition_query_today_hides_skipped_entries(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 1, "slot_index": 0, "time_text": "06:40", "title": "PBJ Brot", "status": "open", "macros": {}, "items": []},
                {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "skipped", "macros": {}, "items": []},
                {"slot_id": 3, "slot_index": 2, "time_text": "14:15", "title": "Mittag", "status": "open", "macros": {}, "items": []},
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: base_payload,
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 308, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 98, "text": "today", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert "Porridge (ausgelassen)" in body


def test_process_nutrition_query_open_returns_only_open_meals(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 1, "slot_index": 0, "time_text": "06:40", "title": "PBJ Brot", "status": "logged", "macros": {}, "items": []},
                {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "macros": {}, "items": []},
                {"slot_id": 3, "slot_index": 2, "time_text": "14:15", "title": "Mittag", "status": "shifted", "macros": {}, "items": []},
                {"slot_id": 4, "slot_index": 3, "time_text": "16:00", "title": "Snack", "status": "skipped", "macros": {}, "items": []},
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: base_payload,
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 303, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 93, "text": "open", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert "Tagesstand ab jetzt" in body
    assert "- Meal 2 · 09:30 · Porridge" in body
    assert "- Meal 3 · 14:15 · Mittag" in body
    assert "PBJ Brot" not in body
    assert "Snack" not in body


def test_process_nutrition_query_skip_meal_command_sets_skipped(monkeypatch):
    sent = []
    calls = {"set_status": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 1, "slot_index": 0, "time_text": "06:40", "title": "PBJ Brot", "status": "logged", "macros": {}, "items": []},
                {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "macros": {}, "items": []},
            ]
        },
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)

    def _fake_set_status(slot_id, **kwargs):
        calls["set_status"].append((slot_id, kwargs))
        return {"ok": True}, None

    monkeypatch.setattr(telegram_hub, "set_planned_meal_status", _fake_set_status)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 305, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 95, "text": "skip meal 2", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["set_status"]
    slot_id, kwargs = calls["set_status"][0]
    assert slot_id == 2
    assert kwargs["status"] == "skipped"
    assert kwargs["status_source"] == "telegram"
    assert "ausgelassen" in sent[0].lower()


def test_process_nutrition_query_makros_returns_logged_and_delta(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [],
            "logged_totals": {"kcal": 1300, "p": 95, "c": 140, "f": 30},
            "targets": {"kcal": 2500, "p": 190, "c": 260, "f": 80},
        },
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 304, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 94, "text": "makros", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert "Bisher geloggt" in body
    assert "Kcal: 1300 / 2500 (-1200)" in body
    assert "P: 95 / 190 g (-95)" in body
    assert "C: 140 / 260 g (-120)" in body
    assert "F: 30 / 80 g (-50)" in body


def test_process_nutrition_query_today_uses_active_context_state(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {
                    "slot_id": 2,
                    "slot_index": 1,
                    "time_text": "09:30",
                    "title": "Porridge Chocolate & Marmelade",
                    "status": "open",
                    "macros": {"kcal": 880, "p": 53, "c": 119, "f": 16},
                    "items": [
                        {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                        {"food_name": "Milch", "amount": 375, "unit": "ml"},
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: base_payload,
    )
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 222,
            "meal_slot_id": 2,
            "meal_title": "Porridge Chocolate & Marmelade",
            "raw": {
                "pending_title": "Porridge Vanille & Apfelmus",
                "pending_items": [
                    {"food_name": "Haferflocken (zart)", "amount": 180, "unit": "g"},
                    {"food_name": "Milch", "amount": 430, "unit": "ml"},
                    {"food_name": "Apfelmus", "amount": 140, "unit": "g"},
                ],
                "pending_macros": {"kcal": 990, "p": 60, "c": 140, "f": 18},
            },
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 306, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 96, "text": "today", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert "Porridge Vanille & Apfelmus" in body
    assert "Porridge Chocolate & Marmelade" not in body


def test_process_nutrition_query_today_manual_override_uses_logged_meal_payload(monkeypatch):
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {
                    "slot_id": 2,
                    "slot_index": 1,
                    "time_text": "09:30",
                    "title": "Porridge Original",
                    "status": "manual_override",
                    "macros": {"kcal": 880, "p": 53, "c": 119, "f": 16},
                    "items": [
                        {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                        {"food_name": "Milch", "amount": 375, "unit": "ml"},
                    ],
                    "logged_meal": {
                        "title": "Porridge Cookies&Cream & Banane",
                        "macros": {"kcal": 936, "p": 54, "c": 134, "f": 16},
                        "items": [
                            {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                            {"food_name": "Milch", "amount": 375, "unit": "ml"},
                            {"food_name": "Whey Cookies&Cream", "amount": 30, "unit": "g"},
                            {"food_name": "Banane", "amount": 1, "unit": "pcs"},
                        ],
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: {
            "planned_meals": [
                {
                    "slot_id": 2,
                    "slot_index": 1,
                    "time_text": "09:30",
                    "title": "Porridge Cookies&Cream & Banane",
                    "state": "logged",
                }
            ],
            "remaining": {"kcal": 0, "p": 0, "c": 0, "f": 0},
        },
    )
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 307, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 97, "text": "today", "chat": {"id": "c1"}})
    assert out["ok"] is True
    body = sent[0]
    assert "Meal 1 ist bereits geloggt:" in body
    assert "09:30 · Porridge Cookies&Cream & Banane" in body
    assert "Porridge Original" not in body


def test_slot_id_from_meal_number_uses_time_order():
    payload = {
        "planned_meals": [
            {"slot_id": 30, "slot_index": 2, "time_text": "14:15"},
            {"slot_id": 10, "slot_index": 0, "time_text": "06:40"},
            {"slot_id": 20, "slot_index": 1, "time_text": "09:30"},
        ]
    }
    assert telegram_hub._slot_id_from_meal_number(payload, 1) == 10
    assert telegram_hub._slot_id_from_meal_number(payload, 2) == 20
    assert telegram_hub._slot_id_from_meal_number(payload, 3) == 30


def test_process_nutrition_query_inline_meal_done_logs_target(monkeypatch):
    sent = []
    calls = {"log": 0}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 1, "slot_index": 4, "time_text": "17:00", "title": "Late Meal", "status": "open", "items": [], "macros": {}},
                {"slot_id": 2, "slot_index": 0, "time_text": "06:40", "title": "PBJ Brot", "status": "open", "items": [], "macros": {}},
            ]
        },
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: {"id": 77, "meal_slot_id": 2, "meal_title": "PBJ Brot", "referenced_time": None, "raw": {"pending_macros": {}}},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_log_context_meal",
        lambda context, **kwargs: (calls.__setitem__("log", calls["log"] + 1) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_close_meal_context", lambda context_id, reason="": None)
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 401, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 101, "text": "meal 1 done", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["log"] == 1
    assert any("PBJ Brot geloggt" in txt for txt in sent)


def test_process_nutrition_query_inline_meal_quantity_edit_logs(monkeypatch):
    sent = []
    calls = {"save_raw": 0, "log": 0}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "items": [], "macros": {}},
            ]
        },
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: {
            "id": 88,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {
                "pending_items": [{"food_name": "Haferflocken", "amount": 150, "unit": "g"}],
                "pending_macros": {},
            },
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_save_context_raw",
        lambda context_id, raw: calls.__setitem__("save_raw", calls["save_raw"] + 1),
    )
    monkeypatch.setattr(
        telegram_hub,
        "_log_context_meal",
        lambda context, **kwargs: (calls.__setitem__("log", calls["log"] + 1) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_close_meal_context", lambda context_id, reason="": None)
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 402, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 102, "text": "meal 1 haferflocken 180g", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["save_raw"] == 1
    assert calls["log"] == 0
    assert any("Porridge angepasst · noch nicht geloggt" in txt for txt in sent)
    assert any("- Haferflocken 180 g" in txt for txt in sent)
    assert any("Zum Loggen: done" in txt for txt in sent)


def test_parse_context_action_supports_numeric_factor():
    action = telegram_hub._parse_context_action("1,3x")
    assert action["type"] == "scale_factor"
    assert float(action["factor"]) == pytest.approx(1.3)


def test_parse_context_actions_supports_comma_separated_multi_edit():
    actions = telegram_hub._parse_context_actions(
        "Haferflocken 180g, Milch 430ml, ohne Marmelade, Whey Chocolate -> Whey Cinnamon Cereal"
    )
    assert [a.get("type") for a in actions] == ["quantity", "quantity", "remove", "replace"]


def test_parse_context_action_quantity_supports_parentheses_name():
    action = telegram_hub._parse_context_action("Reis (parboiled) 160g")
    assert action["type"] == "quantity"
    assert action["name"] == "reis (parboiled)"
    assert float(action["amount"]) == pytest.approx(160.0)
    assert action["unit"] == "g"


def test_parse_context_actions_supports_multiple_quantities_without_comma():
    actions = telegram_hub._parse_context_actions("Hähnchenbrust 200g Reis 160g")
    assert len(actions) == 2
    assert actions[0]["type"] == "quantity"
    assert actions[0]["name"] == "hähnchenbrust"
    assert float(actions[0]["amount"]) == pytest.approx(200.0)
    assert actions[1]["type"] == "quantity"
    assert actions[1]["name"] == "reis"
    assert float(actions[1]["amount"]) == pytest.approx(160.0)


def test_process_nutrition_query_multi_edit_in_one_message(monkeypatch):
    sent = []
    calls = {"save_raw": 0, "log": 0}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 55,
            "meal_slot_id": 2,
            "meal_title": "Porridge Chocolate & Marmelade",
            "referenced_time": None,
            "raw": {
                "pending_items": [
                    {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                    {"food_name": "Milch", "amount": 375, "unit": "ml"},
                    {"food_name": "Whey Chocolate", "amount": 30, "unit": "g"},
                    {"food_name": "Marmelade", "amount": 15, "unit": "g"},
                ],
                "pending_macros": {"kcal": 880, "p": 53, "c": 119, "f": 16},
            },
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "search_logging_foods",
        lambda name, limit=4: [{"id": 999, "name": "Whey Cinnamon Cereal", "unit_default": "g"}] if "cinnamon cereal" in str(name).lower() else [],
    )
    monkeypatch.setattr(telegram_hub, "_compute_macros_for_items", lambda items: {"kcal": 906, "p": 55, "c": 122, "f": 16})
    monkeypatch.setattr(
        telegram_hub,
        "_save_context_raw",
        lambda context_id, raw: calls.__setitem__("save_raw", calls["save_raw"] + 1),
    )
    monkeypatch.setattr(
        telegram_hub,
        "_log_context_meal",
        lambda context, **kwargs: (calls.__setitem__("log", calls["log"] + 1) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_close_meal_context", lambda context_id, reason="": None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 501, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message(
        {
            "message_id": 103,
            "text": "Haferflocken 180g, Milch 430ml, ohne Marmelade, Whey Chocolate -> Whey Cinnamon Cereal",
            "chat": {"id": "c1"},
            "reply_to_message": {"message_id": 250},
        }
    )
    assert out["ok"] is True
    assert calls["save_raw"] == 1
    assert calls["log"] == 0
    body = sent[0]
    assert "Original-Item nicht gefunden" not in body
    assert "Porridge Chocolate & Marmelade angepasst · noch nicht geloggt" in body
    assert "Haferflocken (zart) 180.0g" in body
    assert "Milch 430.0ml" in body
    assert "ohne Marmelade" in body
    assert "Whey Chocolate -> Whey Cinnamon Cereal" in body
    assert "Zum Loggen: done" in body


def test_process_nutrition_query_quantity_adds_new_item_if_missing(monkeypatch):
    sent = []
    calls = {"save_raw": 0}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 60,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {
                "pending_items": [{"food_name": "Haferflocken", "amount": 150, "unit": "g"}],
                "pending_macros": {},
            },
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "search_logging_foods",
        lambda name, limit=8: [{"id": 123, "name": "Apfelmus", "unit_default": "g"}] if "apfelmus" in str(name).lower() else [],
    )
    monkeypatch.setattr(telegram_hub, "_compute_macros_for_items", lambda items: {"kcal": 100, "p": 1, "c": 22, "f": 0})
    monkeypatch.setattr(
        telegram_hub,
        "_save_context_raw",
        lambda context_id, raw: calls.__setitem__("save_raw", calls["save_raw"] + 1),
    )
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 510, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 104, "text": "Apfelmus 140g", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["save_raw"] == 1
    assert "Item nicht eindeutig gefunden" not in sent[0]
    assert "- Apfelmus 140 g" in sent[0]


def test_inline_meal_edit_reuses_active_context_and_keeps_prior_changes(monkeypatch):
    sent = []
    calls = {"opened": 0, "saved_raw": []}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {
            "planned_meals": [
                {
                    "slot_id": 2,
                    "slot_index": 1,
                    "time_text": "09:30",
                    "title": "Porridge",
                    "status": "open",
                    "items": [
                        {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                        {"food_name": "Milch", "amount": 375, "unit": "ml"},
                        {"food_name": "Marmelade", "amount": 15, "unit": "g"},
                    ],
                    "macros": {},
                }
            ]
        },
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 70,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {
                "pending_items": [
                    {"food_name": "Haferflocken (zart)", "amount": 180, "unit": "g"},
                    {"food_name": "Milch", "amount": 430, "unit": "ml"},
                    {"food_name": "Whey Cinnamon Cereal", "amount": 30, "unit": "g"},
                ],
                "pending_macros": {"kcal": 984, "p": 59, "c": 132, "f": 18},
            },
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: calls.__setitem__("opened", calls["opened"] + 1) or {},
    )
    monkeypatch.setattr(
        telegram_hub,
        "search_logging_foods",
        lambda name, limit=8: [{"id": 123, "name": "Apfelmus", "unit_default": "g"}],
    )
    monkeypatch.setattr(
        telegram_hub,
        "_compute_macros_for_items",
        lambda items: {"kcal": 1000, "p": 60, "c": 140, "f": 18},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_save_context_raw",
        lambda context_id, raw: calls["saved_raw"].append(raw),
    )
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 511, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 105, "text": "meal 1 Apfelmus 140g", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert calls["opened"] == 0
    assert calls["saved_raw"]
    saved_items = calls["saved_raw"][0].get("pending_items") or []
    assert any(str(i.get("food_name") or "") == "Haferflocken (zart)" and float(i.get("amount") or 0) == 180 for i in saved_items)
    assert any(str(i.get("food_name") or "") == "Milch" and float(i.get("amount") or 0) == 430 for i in saved_items)
    assert any(str(i.get("food_name") or "") == "Apfelmus" and float(i.get("amount") or 0) == 140 for i in saved_items)
    assert "noch nicht geloggt" in sent[0]


def test_process_nutrition_query_meal_title_swap_uses_meal_template(monkeypatch):
    sent = []
    saved_raw = {}

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_get_active_meal_context",
        lambda chat_id, day_iso: {
            "id": 90,
            "meal_slot_id": 2,
            "meal_title": "Porridge Chocolate & Marmelade",
            "referenced_time": None,
            "raw": {
                "pending_items": [
                    {"food_name": "Haferflocken (zart)", "amount": 150, "unit": "g"},
                    {"food_name": "Milch", "amount": 375, "unit": "ml"},
                ],
                "pending_macros": {"kcal": 880, "p": 53, "c": 119, "f": 16},
            },
        },
    )
    monkeypatch.setattr(telegram_hub, "list_meal_templates", lambda query, limit=25: [{"id": 44, "title": "Porridge Vanille & Apfelmus"}])
    monkeypatch.setattr(
        telegram_hub,
        "resolve_meal_template",
        lambda template_id: {
            "id": 44,
            "title": "Porridge Vanille & Apfelmus",
            "ingredients": [
                {"food_id": 1, "food_name": "Haferflocken (zart)", "amount": 180, "unit": "g"},
                {"food_id": 2, "food_name": "Milch", "amount": 430, "unit": "ml"},
                {"food_id": 3, "food_name": "Whey Vanille", "amount": 30, "unit": "g"},
                {"food_id": 4, "food_name": "Apfelmus", "amount": 140, "unit": "g"},
            ],
        },
    )
    monkeypatch.setattr(telegram_hub, "_compute_macros_for_items", lambda items: {"kcal": 990, "p": 60, "c": 140, "f": 18})
    monkeypatch.setattr(telegram_hub, "_save_context_raw", lambda context_id, raw: saved_raw.setdefault("raw", raw))
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 512, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message(
        {
            "message_id": 106,
            "text": "Porridge Chocolate & Marmelade -> Porridge Vanille & Apfelmus",
            "chat": {"id": "c1"},
            "reply_to_message": {"message_id": 250},
        }
    )
    assert out["ok"] is True
    assert saved_raw["raw"]["pending_title"] == "Porridge Vanille & Apfelmus"
    assert len(saved_raw["raw"]["pending_items"]) == 4
    assert "Original-Item nicht gefunden" not in sent[0]
    assert "Porridge Vanille & Apfelmus angepasst · noch nicht geloggt" in sent[0]
    assert "- Apfelmus 140 g" in sent[0]


def test_log_context_meal_uses_pending_title_for_done(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        telegram_hub,
        "log_planned_meal",
        lambda slot_id, **kwargs: (captured.update({"slot_id": slot_id, "title": kwargs.get("title")}) or {"ok": True}, None),
    )

    data, err = telegram_hub._log_context_meal(
        {
            "id": 1,
            "meal_slot_id": 2,
            "meal_title": "Porridge Chocolate & Marmelade",
            "referenced_time": None,
            "raw": {
                "pending_title": "Porridge Vanille & Apfelmus",
                "pending_items": [{"food_id": 1, "food_name": "Haferflocken", "amount": 180, "unit": "g"}],
            },
        },
        day_iso="2026-03-11",
        chat_id="c1",
    )
    assert err is None
    assert data == {"ok": True}
    assert captured["slot_id"] == 2
    assert captured["title"] == "Porridge Vanille & Apfelmus"


def test_inline_meal_done_with_time_logs_custom_time_done_first(monkeypatch):
    captured = {}
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {"planned_meals": [{"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "items": [], "macros": {}}]},
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: {
            "id": 91,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {"pending_items": [{"food_id": 1, "food_name": "Haferflocken", "amount": 150, "unit": "g"}], "pending_macros": {}},
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "log_planned_meal",
        lambda slot_id, **kwargs: (captured.update({"slot_id": slot_id, "time_mode": kwargs.get("time_mode"), "custom_logged_at": kwargs.get("custom_logged_at")}) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 520, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 107, "text": "meal 1 done 11:45", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert captured["slot_id"] == 2
    assert captured["time_mode"] == "custom"
    assert "T11:45:00" in str(captured["custom_logged_at"])
    assert "geloggt · 11:45" in sent[0]


def test_inline_meal_done_without_time_uses_planned_time(monkeypatch):
    captured = {}
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {"planned_meals": [{"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "items": [], "macros": {}}]},
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: {
            "id": 93,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {"pending_items": [{"food_id": 1, "food_name": "Haferflocken", "amount": 150, "unit": "g"}], "pending_macros": {}},
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "log_planned_meal",
        lambda slot_id, **kwargs: (captured.update({"slot_id": slot_id, "time_mode": kwargs.get("time_mode"), "custom_logged_at": kwargs.get("custom_logged_at")}) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 522, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 109, "text": "meal 1 done", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert captured["slot_id"] == 2
    assert captured["time_mode"] == "custom"
    assert "T09:30:00" in str(captured["custom_logged_at"])
    assert "geloggt · 09:30" in sent[0]


def test_inline_meal_done_with_time_logs_custom_time_time_first(monkeypatch):
    captured = {}
    sent = []

    monkeypatch.setattr(telegram_hub, "_today_iso_berlin", lambda: "2026-03-11")
    monkeypatch.setattr(
        telegram_hub,
        "get_logging_day_payload",
        lambda day_iso: {"planned_meals": [{"slot_id": 2, "slot_index": 1, "time_text": "09:30", "title": "Porridge", "status": "open", "items": [], "macros": {}}]},
    )
    monkeypatch.setattr(telegram_hub, "_find_pending_nutrition_prompt_by_outbound", lambda message_id: None)
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_get_active_meal_context", lambda chat_id, day_iso: None)
    monkeypatch.setattr(
        telegram_hub,
        "_open_meal_context",
        lambda **kwargs: {
            "id": 92,
            "meal_slot_id": 2,
            "meal_title": "Porridge",
            "referenced_time": None,
            "raw": {"pending_items": [{"food_id": 1, "food_name": "Haferflocken", "amount": 150, "unit": "g"}], "pending_macros": {}},
        },
    )
    monkeypatch.setattr(
        telegram_hub,
        "log_planned_meal",
        lambda slot_id, **kwargs: (captured.update({"slot_id": slot_id, "time_mode": kwargs.get("time_mode"), "custom_logged_at": kwargs.get("custom_logged_at")}) or {"ok": True}, None),
    )
    monkeypatch.setattr(telegram_hub, "_format_macros_short", lambda macros: "0 kcal · P 0 / C 0 / F 0")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 521, "chat_id": "c1"},
    )

    out = telegram_hub.process_nutrition_query_message({"message_id": 108, "text": "meal 1 11:45 done", "chat": {"id": "c1"}})
    assert out["ok"] is True
    assert captured["slot_id"] == 2
    assert captured["time_mode"] == "custom"
    assert "T11:45:00" in str(captured["custom_logged_at"])
    assert "geloggt · 11:45" in sent[0]


def test_pending_nutrition_prompt_not_auto_rejected_when_fresh(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    calls = {"rejected": 0, "response_marked": 0, "muted": 0}
    monkeypatch.setattr(telegram_hub, "_now_min_berlin", lambda: 12 * 60)

    monkeypatch.setattr(telegram_hub, "evaluate_core_nutrition_day", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "get_core_nutrition_actions_for_day", lambda day_iso: [])
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k3", "raw_json": "{\"id\":88}", "created_at": now},
    )
    monkeypatch.setattr(telegram_hub, "mark_core_action_rejected", lambda action_id: calls.__setitem__("rejected", calls["rejected"] + 1) or None)
    monkeypatch.setattr(
        telegram_hub,
        "_mark_nutrition_prompt_response",
        lambda *args, **kwargs: calls.__setitem__("response_marked", calls["response_marked"] + 1),
    )
    monkeypatch.setattr(telegram_hub, "_set_nutrition_prompt_muted", lambda day_iso: calls.__setitem__("muted", calls["muted"] + 1))
    monkeypatch.setattr(telegram_hub, "_is_nutrition_prompt_muted", lambda day_iso: False)

    out = telegram_hub._maybe_send_pending_nutrition_prompt("2026-03-11")
    assert out["ok"] is True
    assert out["sent"] is False
    assert out["reason"] == "awaiting_response"
    assert calls["rejected"] == 0
    assert calls["response_marked"] == 0
    assert calls["muted"] == 0


def test_nutrition_prompt_text_offers_only_ok(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_hub, "_now_min_berlin", lambda: 12 * 60)
    monkeypatch.setattr(telegram_hub, "evaluate_core_nutrition_day", lambda day_iso: {"planned_meals": [{"slot_id": 4, "time_text": "18:30", "title": "Carbs Pulver"}]})
    monkeypatch.setattr(
        telegram_hub,
        "get_core_nutrition_actions_for_day",
        lambda day_iso: [
            {
                "id": 7,
                "action_key": "abc",
                "status": "proposed",
                "source": "core",
                "action_type": "shift_meal",
                "reason_text": "Timing-Fix",
                "payload": {"slot_id": 4, "to_time": "17:10"},
            }
        ],
    )
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": [{"slot_id": 4, "time_text": "18:30", "title": "Carbs Pulver", "status": "open"}]})
    monkeypatch.setattr(
        telegram_hub,
        "_effective_day_payload_for_runtime",
        lambda day_iso, base_payload=None, evaluated=None: {"planned_meals": [{"slot_id": 4, "slot_index": 4, "time_text": "18:30", "title": "Carbs Pulver", "status": "open"}]},
    )
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_is_nutrition_prompt_muted", lambda day_iso: False)
    monkeypatch.setattr(telegram_hub, "_get_pending_nutrition_prompt", lambda source_key: None)
    monkeypatch.setattr(telegram_hub, "_upsert_nutrition_prompt", lambda **kwargs: {})
    monkeypatch.setattr(telegram_hub, "append_day_event", lambda **kwargs: {})
    monkeypatch.setattr(telegram_hub, "_mark_nutrition_prompt_sent", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 1, "chat_id": "c1"},
    )

    out = telegram_hub._maybe_send_pending_nutrition_prompt("2026-03-11")
    assert out["ok"] is True
    assert out["sent"] is True
    assert sent
    assert "- ok" in sent[0]
    assert "- spaeter" in sent[0]
    assert "nein = verwerfen" not in sent[0]


def test_pending_nutrition_prompt_suppressed_during_quiet_hours(monkeypatch):
    monkeypatch.setattr(telegram_hub, "_now_min_berlin", lambda: 0)
    monkeypatch.setattr(telegram_hub, "evaluate_core_nutrition_day", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "get_core_nutrition_actions_for_day",
        lambda day_iso: [
            {
                "id": 7,
                "action_key": "abc",
                "status": "proposed",
                "source": "core",
                "action_type": "shift_meal",
                "reason_text": "Timing-Fix",
                "payload": {"slot_id": 4, "to_time": "17:10"},
            }
        ],
    )
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": [{"slot_id": 4, "time_text": "18:30", "title": "Carbs Pulver", "status": "open"}]})
    monkeypatch.setattr(telegram_hub, "_find_unanswered_nutrition_prompt_for_day", lambda day_iso: None)
    monkeypatch.setattr(telegram_hub, "_is_nutrition_prompt_muted", lambda day_iso: False)

    out = telegram_hub._maybe_send_pending_nutrition_prompt("2026-03-11")
    assert out["ok"] is True
    assert out["sent"] is False
    assert out["reason"] == "quiet_hours"


def test_pending_nutrition_prompt_not_auto_rejected_when_old(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=180)).replace(microsecond=0).isoformat()
    calls = {"rejected": 0, "response_marked": 0, "muted": 0}
    monkeypatch.setattr(telegram_hub, "_now_min_berlin", lambda: 12 * 60)

    monkeypatch.setattr(telegram_hub, "evaluate_core_nutrition_day", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(telegram_hub, "get_core_nutrition_actions_for_day", lambda day_iso: [])
    monkeypatch.setattr(telegram_hub, "get_logging_day_payload", lambda day_iso: {"planned_meals": []})
    monkeypatch.setattr(
        telegram_hub,
        "_find_unanswered_nutrition_prompt_for_day",
        lambda day_iso: {"source_key": "nutrition-core:2026-03-11:k4", "raw_json": "{\"id\":99}", "created_at": old},
    )
    monkeypatch.setattr(telegram_hub, "_is_nutrition_prompt_muted", lambda day_iso: False)
    monkeypatch.setattr(
        telegram_hub,
        "mark_core_action_rejected",
        lambda action_id: calls.__setitem__("rejected", calls["rejected"] + 1) or {"id": action_id},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_mark_nutrition_prompt_response",
        lambda *args, **kwargs: calls.__setitem__("response_marked", calls["response_marked"] + 1),
    )
    monkeypatch.setattr(telegram_hub, "_set_nutrition_prompt_muted", lambda day_iso: calls.__setitem__("muted", calls["muted"] + 1))

    out = telegram_hub._maybe_send_pending_nutrition_prompt("2026-03-11")
    assert out["ok"] is True
    assert out["sent"] is False
    assert out["reason"] == "awaiting_response"
    assert calls["rejected"] == 0
    assert calls["response_marked"] == 0
    assert calls["muted"] == 0


def test_process_training_query_message_replies_to_origin_chat(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_hub, "_exercise_query_summary_text", lambda exercise, variation="": "Antwort")
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 211, "chat_id": kwargs.get("chat_id")},
    )

    result = telegram_hub.process_training_query_message(
        {
            "message_id": 777,
            "text": "Enges Rudern, KH",
            "chat": {"id": -10012345},
        }
    )

    assert result["ok"] is True
    assert sent[0][2]["chat_id"] == "-10012345"


def test_training_pr_alert_sent_once_for_latest_workout(tmp_path, monkeypatch):
    training_db = tmp_path / "training.sqlite3"
    core_db = tmp_path / "core.sqlite3"

    conn_t = sqlite3.connect(training_db)
    conn_t.row_factory = sqlite3.Row
    conn_t.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, date_iso TEXT, name TEXT, notes TEXT)")
    conn_t.execute(
        """
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            notes TEXT
        )
        """
    )
    conn_t.execute(
        """
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER,
            set_number INTEGER,
            reps INTEGER,
            weight REAL,
            rpe REAL
        )
        """
    )
    # Previous squat exposure
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-03', 'Lower A', '')")
    wid_prev = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'Smith', '', '', '')",
        (wid_prev,),
    )
    ex_prev = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 6, 80, 8.5)", (ex_prev,))
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Rows', 'eGym', 'eGym', 'bilateral', '')",
        (wid_prev,),
    )
    row_prev = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 8, 100, 8.5)", (row_prev,))
    # Latest lower A with better e1rm
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-10', 'Lower A', '')")
    wid_latest = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'Smith', '', '', '')",
        (wid_latest,),
    )
    ex_latest = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 8, 80, 8.5)", (ex_latest,))
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Rows', 'eGym', 'eGym', 'bilateral', '')",
        (wid_latest,),
    )
    row_latest = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 11, 94, 9)", (row_latest,))
    conn_t.commit()
    conn_t.close()

    conn_c = sqlite3.connect(core_db)
    conn_c.row_factory = sqlite3.Row
    conn_c.close()

    def _training_conn():
        c = sqlite3.connect(training_db)
        c.row_factory = sqlite3.Row
        return c

    def _core_conn():
        c = sqlite3.connect(core_db)
        c.row_factory = sqlite3.Row
        return c

    sent = []
    monkeypatch.setattr(telegram_hub, "get_training_db", _training_conn)
    monkeypatch.setattr(telegram_hub, "get_core_db", _core_conn)
    monkeypatch.setattr(
        telegram_hub,
        "send_ntfy_notification",
        lambda text, **kwargs: sent.append(("ntfy", text, kwargs)) or {"ok": True, "sent": True, "channel": "ntfy"},
    )

    out1 = telegram_hub._maybe_send_training_pr_alert()
    assert out1["ok"] is True
    assert out1["sent"] is True
    assert out1["workout_ids"] == [wid_latest]
    assert out1["count"] == 2
    assert len(sent) == 2
    assert all(item[0] == "ntfy" for item in sent)
    body = "\n\n".join(item[1] for item in sent)
    assert "PR 🚀" in body
    assert "Squats (Smith)" in body
    assert "Rows (eGym)" in body
    assert "Top-Set: 8 x 80,0kg" in body
    assert "Top-Set: 11 x 94,0kg" in body
    assert "Neues e1RM:" in body
    assert "(+" in body
    assert all(item[2]["title"] == "LIVA · Training PR" for item in sent)

    out2 = telegram_hub._maybe_send_training_pr_alert()
    assert out2["ok"] is True
    assert out2["sent"] is False
    assert out2["reason"] in {"no_new_logged_training", "no_new_pr"}


def test_training_pr_alert_can_send_for_previous_workout_if_latest_has_no_pr(tmp_path, monkeypatch):
    training_db = tmp_path / "training.sqlite3"
    core_db = tmp_path / "core.sqlite3"

    conn_t = sqlite3.connect(training_db)
    conn_t.row_factory = sqlite3.Row
    conn_t.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, date_iso TEXT, name TEXT, notes TEXT)")
    conn_t.execute(
        """
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            notes TEXT
        )
        """
    )
    conn_t.execute(
        """
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER,
            set_number INTEGER,
            reps INTEGER,
            weight REAL,
            rpe REAL
        )
        """
    )
    # Older lower with PR
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-10', 'Lower A', '')")
    wid_lower = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'Smith', '', '', '')",
        (wid_lower,),
    )
    ex_lower = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 8, 80, 8.5)", (ex_lower,))
    # Historical comparison
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-03', 'Lower A', '')")
    wid_hist = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'Smith', '', '', '')",
        (wid_hist,),
    )
    ex_hist = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 6, 80, 8.5)", (ex_hist,))
    # Latest workout (no PR / no comparable history)
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-11', 'Pull', '')")
    wid_pull = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Latzug', 'Kabel', '', '', '')",
        (wid_pull,),
    )
    ex_pull = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 10, 70, 8.0)", (ex_pull,))
    conn_t.commit()
    conn_t.close()

    conn_c = sqlite3.connect(core_db)
    conn_c.row_factory = sqlite3.Row
    conn_c.close()

    def _training_conn():
        c = sqlite3.connect(training_db)
        c.row_factory = sqlite3.Row
        return c

    def _core_conn():
        c = sqlite3.connect(core_db)
        c.row_factory = sqlite3.Row
        return c

    sent = []
    monkeypatch.setattr(telegram_hub, "get_training_db", _training_conn)
    monkeypatch.setattr(telegram_hub, "get_core_db", _core_conn)
    monkeypatch.setattr(
        telegram_hub,
        "send_ntfy_notification",
        lambda text, **kwargs: sent.append(("ntfy", text)) or {"ok": True, "sent": True, "channel": "ntfy"},
    )

    out = telegram_hub._maybe_send_training_pr_alert()
    assert out["ok"] is True
    assert out["sent"] is True
    assert out["workout_ids"] == [wid_lower]
    assert sent
    assert "PR 🚀" in sent[0][1]
    assert "Squats (Smith)" in sent[0][1]


def test_training_pr_alert_skips_overbidden_old_pr(tmp_path, monkeypatch):
    training_db = tmp_path / "training.sqlite3"
    core_db = tmp_path / "core.sqlite3"

    conn_t = sqlite3.connect(training_db)
    conn_t.row_factory = sqlite3.Row
    conn_t.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, date_iso TEXT, name TEXT, notes TEXT)")
    conn_t.execute(
        """
        CREATE TABLE exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER,
            name TEXT,
            variation TEXT,
            device TEXT,
            laterality TEXT,
            notes TEXT
        )
        """
    )
    conn_t.execute(
        """
        CREATE TABLE sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER,
            set_number INTEGER,
            reps INTEGER,
            weight REAL,
            rpe REAL
        )
        """
    )
    # Older PR candidate
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-09', 'Lower A', '')")
    wid_old = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'LH', '', '', '')",
        (wid_old,),
    )
    ex_old = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 5, 80, 8.5)", (ex_old,))
    # Newer better PR (active)
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-10', 'Lower A', '')")
    wid_new = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'LH', '', '', '')",
        (wid_new,),
    )
    ex_new = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 5, 85, 8.5)", (ex_new,))
    # Historical baseline before both
    cur = conn_t.execute("INSERT INTO workouts (date_iso, name, notes) VALUES ('2026-03-01', 'Lower A', '')")
    wid_hist = cur.lastrowid
    cur = conn_t.execute(
        "INSERT INTO exercises (workout_id, name, variation, device, laterality, notes) VALUES (?, 'Squats', 'LH', '', '', '')",
        (wid_hist,),
    )
    ex_hist = cur.lastrowid
    conn_t.execute("INSERT INTO sets (exercise_id, set_number, reps, weight, rpe) VALUES (?, 1, 5, 70, 8.5)", (ex_hist,))
    conn_t.commit()
    conn_t.close()

    conn_c = sqlite3.connect(core_db)
    conn_c.row_factory = sqlite3.Row
    conn_c.close()

    def _training_conn():
        c = sqlite3.connect(training_db)
        c.row_factory = sqlite3.Row
        return c

    def _core_conn():
        c = sqlite3.connect(core_db)
        c.row_factory = sqlite3.Row
        return c

    sent = []
    monkeypatch.setattr(telegram_hub, "get_training_db", _training_conn)
    monkeypatch.setattr(telegram_hub, "get_core_db", _core_conn)
    monkeypatch.setattr(
        telegram_hub,
        "send_ntfy_notification",
        lambda text, **kwargs: sent.append(text) or {"ok": True, "sent": True, "channel": "ntfy"},
    )

    out = telegram_hub._maybe_send_training_pr_alert()
    assert out["ok"] is True
    assert out["sent"] is True
    assert out["workout_ids"] == [wid_new]
    assert sent
    assert "PR 🚀" in sent[0]
    assert "Squats (LH)" in sent[0]


def test_process_training_inbox_runs_pr_alert_tick(monkeypatch):
    monkeypatch.setattr(telegram_hub, "fetch_updates", lambda domain, timeout=0: [])
    monkeypatch.setattr(
        telegram_hub,
        "_maybe_send_training_pr_alert",
        lambda: {"ok": True, "sent": True, "workout_id": 42},
    )

    out = telegram_hub.process_training_inbox(timeout=0)

    assert out["ok"] is True
    assert out["updates"] == 0
    assert out["pr_alert"]["sent"] is True
    assert out["pr_alert"]["workout_id"] == 42


def test_process_training_query_message_answers_weekly_check(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "_weekly_training_check_text",
        lambda weeks=8: "CORE Wochencheck\nDiese Woche: 3 Einheiten",
    )
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 112, "chat_id": "1"},
    )

    result = telegram_hub.process_training_query_message({"message_id": 501, "text": "wochencheck"})

    assert result["ok"] is True
    assert sent[0][1].startswith("CORE Wochencheck")


def test_process_training_media_message_imports_physique_and_replies(monkeypatch):
    monkeypatch.setattr(
        telegram_hub,
        "resolve_domain_config",
        lambda domain: {"domain": domain, "token": "token123", "chat_id": "-1001"},
    )
    monkeypatch.setattr(
        telegram_hub,
        "_api_get_file_bytes",
        lambda token, file_id: (b"fakeimgbytes", "image/jpeg", "tg.jpg"),
    )
    monkeypatch.setattr(
        telegram_hub,
        "parse_physique_caption",
        lambda text: {"note": "checkin", "weight_kg": None, "view_label": "", "pose_label": ""},
    )

    imported = {}

    def _fake_create(**kwargs):
        imported.update(kwargs)
        return {"id": 42, "view_label": "Front", "pose_label": "Front Relaxed"}

    monkeypatch.setattr(telegram_hub, "create_physique_update", _fake_create)
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 123, "chat_id": "-1001"},
    )

    result = telegram_hub.process_training_media_message(
        {
            "message_id": 555,
            "caption": "Physique front",
            "chat": {"id": -1001},
            "photo": [{"file_id": "a1", "width": 120, "height": 200}],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "physique_imported"
    assert imported["source"] == "telegram"
    assert imported["source_domain"] == "training"
    assert imported["source_ref"] == "tg-training:-1001:555"
    assert imported["assets"]
    assert sent
    assert "Physique-Flag gesetzt" in sent[0][1]


def test_process_telegram_inboxes_combines_domains(monkeypatch):
    monkeypatch.setattr(telegram_hub, "process_training_inbox", lambda timeout=0: {"ok": True, "updates": 2, "processed": 1, "ignored": 1, "errors": []})
    monkeypatch.setattr(telegram_hub, "process_nutrition_inbox", lambda timeout=0: {"ok": True, "updates": 4, "processed": 2, "ignored": 2, "errors": []})
    monkeypatch.setattr(telegram_hub, "process_system_inbox", lambda timeout=0: {"ok": True, "updates": 3, "processed": 0, "ignored": 3, "errors": []})
    result = telegram_hub.process_telegram_inboxes(timeout=0)
    assert result["ok"] is True
    assert result["updates"] == 9
    assert result["processed"] == 3


def test_process_system_query_message_webuntis_triggers_pipeline(monkeypatch):
    monkeypatch.setattr(
        telegram_hub.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    sent = []
    monkeypatch.setattr(
        telegram_hub,
        "send_domain_message",
        lambda domain, text, **kwargs: sent.append((domain, text, kwargs)) or {"ok": True, "message_id": 1, "chat_id": "1"},
    )
    out = telegram_hub.process_system_query_message({"message_id": 10, "chat": {"id": "1"}, "text": "webuntis"})
    assert out["ok"] is True
    assert sent
    assert "WebUntis-Sync gestartet" in sent[0][1]
