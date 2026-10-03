from __future__ import annotations

from liva_mcp.tool_registry import TOOL_BY_NAME, call_tool, list_tools


EXPECTED = {
    "liva_context_snapshot",
    "liva_daily_snapshot",
    "liva_training_state",
    "liva_training_plan",
    "liva_recovery_snapshot",
    "liva_nutrition_snapshot",
    "liva_weight_state",
    "liva_runs_state",
    "liva_coach_read",
    "liva_coach_act",
    "liva_memory_recall",
    "liva_memory_audit",
    "liva_memory_begin",
    "liva_memory_finish",
    "liva_memory_import_file",
    "liva_memory_context",
    "liva_memory_source",
    "liva_memory_pending",
    "liva_memory_ingest_file",
    "liva_memory_commit",
    "liva_post_daily_note",
    "liva_upsert_weight",
    "liva_post_nutrition_context",
    "liva_post_daily_flag",
    "liva_post_training_rawlog",
    "liva_training_decision_plan",
    "liva_training_decision_write",
    "liva_operator_plan",
    "liva_operator_execute",
}


def test_registry_contains_exact_allowlist():
    assert set(TOOL_BY_NAME) == EXPECTED
    assert {"liva_daily_context", "liva_recovery_state", "liva_nutrition_state"}.isdisjoint(TOOL_BY_NAME)


def test_unknown_tool_cannot_be_called(service):
    result = call_tool(service, "execute_command", {"command": "DELETE FROM workouts"})
    assert result == {"ok": False, "error": {"code": "unknown_tool", "message": "Tool is not allowlisted"}}


def test_unknown_arguments_are_rejected(service):
    result = call_tool(service, "liva_training_state", {"endpoint": "/api/write"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_public_write_schemas_publish_real_enums_and_contract_fields():
    tools = {item["name"]: item for item in list_tools()}
    assert tools["liva_post_daily_note"]["inputSchema"]["properties"]["category"]["enum"] == ["training", "recovery", "nutrition", "school", "personal", "system", "general"]
    assert tools["liva_post_daily_flag"]["inputSchema"]["properties"]["flag_type"]["enum"]
    assert tools["liva_post_nutrition_context"]["inputSchema"]["properties"]["precision_level"]["enum"] == ["exact", "estimate", "context"]
    assert "operations" in tools["liva_operator_execute"]["inputSchema"]["properties"]
    assert tools["liva_upsert_weight"]["inputSchema"]["required"] == ["weight_kg"]
    assert tools["liva_training_decision_plan"]["inputSchema"]["additionalProperties"] is False
    assert tools["liva_coach_read"]["inputSchema"]["properties"]["mode"]["examples"]
    assert tools["liva_coach_act"]["inputSchema"]["properties"]["dry_run"]["default"] is False
    assert "workout_id" in tools["liva_coach_act"]["inputSchema"]["properties"]["payload"]["properties"]
    coach_payload = tools["liva_coach_act"]["inputSchema"]["properties"]["payload"]["properties"]
    assert "serving_weight_g" in coach_payload
    assert "green_low" in coach_payload
    assert "enum" not in tools["liva_coach_act"]["inputSchema"]["properties"]["command"]
    assert tools["liva_memory_begin"]["inputSchema"]["properties"]["kind"]["enum"] == ["auto", "personal", "procedure", "historical", "raw_only"]
    assert tools["liva_memory_finish"]["inputSchema"]["properties"]["route"]["enum"] == ["living_wiki", "procedures", "raw_only", "current_state", "live_data"]
    finish_change = tools["liva_memory_finish"]["inputSchema"]["properties"]["changes"]["items"]
    assert "ensure_links" in finish_change["properties"]["action"]["enum"]
    assert "correct_page" in finish_change["properties"]["action"]["enum"]
    assert finish_change["properties"]["links"]["items"]["additionalProperties"] is False
    assert finish_change["properties"]["links"]["maxItems"] == 6


def test_operator_execute_empty_operations_is_noop(service):
    result = call_tool(service, "liva_operator_execute", {"operations": [], "dry_run": True}, scopes={"liva.write"})
    assert result["ok"] and result["data"]["executed"] is False and result["data"]["succeeded"] == 0


def test_write_tools_require_write_scope(service):
    result = call_tool(service, "liva_post_daily_note", {"category": "general", "text": "x"})
    assert result["error"]["code"] == "insufficient_scope"
    plan = call_tool(service, "liva_training_decision_plan", {})
    assert plan["ok"] is False  # fixture plan has no valid rolling session id, but read scope is accepted
    assert plan["error"]["code"] != "insufficient_scope"
    coach = call_tool(service, "liva_coach_act", {"domain": "training", "command": "log_gym_session"})
    assert coach["error"]["code"] == "insufficient_scope"


def test_coach_action_is_live_by_default_and_pair_is_validated(service):
    seen = {}

    class FakeActions:
        def read(self, mode, payload):
            assert mode == "capabilities"
            return {"result": {"contract_revision": "test", "read_modes": ["capabilities"], "commands": {"training": {"log_gym_session": {}, "future_backend_command": {}}}}}

        def act(self, domain, command, payload, *, dry_run, confirm, reason):
            seen.update(domain=domain, command=command, payload=payload, dry_run=dry_run, confirm=confirm, reason=reason)
            return {"source": "canonical_actions", "production_write": True, "executed": True, "changed": True}

    service.actions = FakeActions()
    result = call_tool(
        service,
        "liva_coach_act",
        {"domain": "training", "command": "log_gym_session", "payload": {"date": "2026-08-04"}},
        scopes={"liva.write"},
    )
    assert result["ok"] and result["data"]["production_write"] is True
    assert seen["dry_run"] is False

    future = call_tool(
        service,
        "liva_coach_act",
        {"domain": "training", "command": "future_backend_command"},
        scopes={"liva.write"},
    )
    assert future["ok"] and seen["command"] == "future_backend_command"

    invalid = call_tool(service, "liva_coach_act", {"domain": "remote", "command": "pc_action"}, scopes={"liva.write"})
    assert invalid["error"]["code"] == "invalid_arguments"


def test_smoke_token_blocks_default_live_coach_action(service):
    result = call_tool(
        service,
        "liva_coach_act",
        {"domain": "weight", "command": "log_weight", "payload": {"weight_kg": 80}},
        scopes={"liva.write"},
        smoke_test=True,
    )
    assert result["error"]["code"] == "smoke_test_live_write_blocked"
