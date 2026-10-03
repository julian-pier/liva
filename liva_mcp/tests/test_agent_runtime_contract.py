from __future__ import annotations

import json
import sqlite3
from datetime import date

import liva_mcp.read_service as read_module
from liva_mcp.instructions import server_instructions
from liva_mcp.tool_registry import TOOL_BY_NAME, call_tool


def test_consumed_policy_requires_a_liva_read_for_every_turn_and_failure_safety():
    policy = server_instructions()
    normalized = " ".join(policy.split())
    assert "Before every answer" in policy
    assert "liva_context_snapshot" in policy
    assert "greetings, general questions, feedback, and" in policy
    assert "If the mandatory context read fails" in normalized
    assert "cannot observe or block an external model answer" in policy


def test_universal_entry_and_training_contract_descriptions_are_explicit():
    context = TOOL_BY_NAME["liva_context_snapshot"]
    decision = TOOL_BY_NAME["liva_training_decision_plan"]
    assert context.required_scope == "liva.read"
    assert "every user turn" in context.description
    assert decision.required_scope == "liva.read"
    for field in ("today_status", "session_relation", "is_today_session", "is_rest_day", "resolution_status"):
        assert field in decision.description


def test_sunday_rest_keeps_push_b_as_next_not_today(service):
    plan = {
        "meta": {
            "mode": "rolling_sequence",
            "rolling_week_pattern": {"Mo": "train", "Di": "train", "Mi": "train", "Do": "train", "Fr": "train", "Sa": "train", "So": "rest"},
        },
        "sequence": [
            {"id": "pull-a", "kind": "gym", "title": "FB Pull"},
            {"id": "push-b", "kind": "gym", "title": "Push B · Schrägbank + OHP eGym + Beinpresse"},
        ],
    }
    with sqlite3.connect(service.config.database_path("plans.sqlite3")) as conn:
        conn.execute("UPDATE gym_plans SET plan_json=?", (json.dumps(plan),))
    with sqlite3.connect(service.config.database_path("training.sqlite3")) as conn:
        conn.execute("UPDATE workouts SET date_iso='2026-07-25', name='FB Pull' WHERE id=1")

    result = call_tool(service, "liva_training_decision_plan", {"date": "2026-07-26", "dry_run": True})

    assert result["ok"] is True
    data = result["data"]
    assert data["requested_date"] == "2026-07-26"
    assert data["timezone"] == "Europe/Berlin"
    assert data["today_status"] == "rest"
    assert data["session_relation"] == "next"
    assert data["is_today_session"] is False
    assert data["is_rest_day"] is True
    assert data["today_session"] is None
    assert data["next_session"]["name"].startswith("Push B")
    assert data["next_session_date"] == "2026-07-27"
    assert data["selected_session"]["session_relation"] == "next"
    assert data["next_step"] == "do_not_write_training_decision"


def test_old_sickness_and_alcohol_flags_are_historical_not_current(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 26))
    with sqlite3.connect(service.hrv.database_path) as conn:
        conn.execute("CREATE TABLE recovery_day_flags (date_iso TEXT PRIMARY KEY, sickness_bool INTEGER, alcohol_bool INTEGER, note TEXT, source TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO recovery_day_flags VALUES ('2026-07-16',1,1,'old','test','2026-07-16T08:00:00+02:00')")

    result = service.recovery_snapshot()

    assert result["flags"]["as_of_date"] == "2026-07-26"
    assert result["flags"]["current_flags"] == {"date": "2026-07-26", "sickness": False, "alcohol": False}
    assert result["flags"]["historical_day_flags"][0]["date_iso"] == "2026-07-16"


def test_answer_semantics_are_present_without_duplicating_domain_engine():
    policy = " ".join(server_instructions().split())
    required = (
        "Never say \"if training is scheduled today\"",
        "Never confirm storage without matching date/value readback",
        "otherwise do not refer to a photo",
        "Return one synthesis using the current Polar sources",
    )
    assert all(fragment in policy for fragment in required)


def test_memory_write_and_reporting_contract_is_delivered_to_gpt():
    policy = server_instructions()
    for fragment in (
        "Existing knowledge is the default target",
        "correct_page",
        "reporting.may_claim_knowledge_saved=true",
        "Quelle erfasst; die Living Wiki wurde nicht geändert.",
        "never work around that rejection with a renamed duplicate",
        "Every create_page must provide 1-6 explicit Living Wiki links",
        "rejects missing or ambiguous parents",
    ):
        assert fragment in policy


def test_grill_me_uses_persistent_skill_engine_and_verified_stop():
    policy = server_instructions()
    for fragment in (
        'After the required `liva_context_snapshot`',
        '`skill_id="grill-me"`',
        '`liva_coach_act(domain="skill", command="start")`',
        'After every user answer call `record_turn`',
        '`/checkpoint` calls `checkpoint`',
        'An explicit cross-chat\ncontinuation uses `resume`',
        '`/stop` calls `stop`',
        'Sessions with three or more answered turns are substantial',
        'standalone dated `event`',
        'Report the five `completion_status` stages separately',
        '`may_claim_completed=true`',
        'A session checkpoint alone is not a Wiki update',
        'liva_memory_recall(query="LIVA Vault Audit", scope="procedures", depth="deep")',
        'procedures/skills/liva-vault-audit/SKILL.md',
        'non-empty text',
        '`text_complete=true`',
    ):
        assert fragment in policy
    assert "Interview mich" not in policy


def test_nutrition_policy_requires_real_multi_day_food_history_before_plan_rewrite():
    policy = server_instructions()
    assert 'mode="nutrition_data"' in policy
    assert '"nutrition_mode":"meals"' in policy
    assert "real logged foods, quantities and meal macros" in policy
    assert 'command="patch_nutrition_plan"' in policy
    assert "Then read `nutrition_plan` again" in policy


def test_phase_analysis_policy_requires_recall_and_confirmed_wiki_write():
    policy = " ".join(server_instructions().split())
    for fragment in (
        "Historical phase analysis",
        "targeted `liva_memory_recall`",
        'mode="analysis_phase_overview"',
        "Never start a memory write from the analysis alone",
        "Only after explicit user",
        "exact date range, inspected metrics, proposed statement and exceptions",
        '`liva_memory_finish(route="living_wiki")`',
        "A one-off observation stays in the analysis",
        "failed memory write must not trigger any live-data write",
    ):
        assert fragment in policy
