from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from liva_mcp.tool_registry import COACH_ACT_SCHEMA, call_tool, list_tools


class MutableBackend:
    """Versioned backend fixture representing a deploy behind one MCP session."""

    def __init__(self):
        self.revision = "A"
        self.command = "record_v1"
        self.required_field = "value"

    def read(self, mode, payload):
        assert mode == "capabilities"
        return {
            "result": {
                "contract_revision": self.revision,
                "read_modes": ["capabilities", "future_read"],
                "commands": {"future": {self.command: {"required": [self.required_field]}}},
            }
        }

    def act(self, domain, command, payload, *, dry_run, confirm, reason):
        if domain != "future" or command != self.command or self.required_field not in payload:
            raise AssertionError("stale contract reached backend")
        return {"ok": True, "accepted": {"domain": domain, "command": command, "payload": payload}}


def test_same_mcp_session_dispatches_new_backend_action_and_payload_after_deploy(service):
    backend = MutableBackend()
    service.actions = backend
    tool_manifest_before = [tool for tool in list_tools() if tool["name"] in {"liva_coach_read", "liva_coach_act"}]

    version_a = call_tool(
        service,
        "liva_coach_act",
        {"domain": "future", "command": "record_v1", "payload": {"value": "A"}},
        scopes={"liva.read", "liva.write"},
    )
    assert version_a["ok"] is True
    assert version_a["data"]["contract_revision"] == "A"

    # Backend deployment B: a newly introduced action and a changed required
    # payload, while the ChatGPT-visible MCP tool manifest stays identical.
    backend.revision = "B"
    backend.command = "record_v2"
    backend.required_field = "payload_v2"
    version_b = call_tool(
        service,
        "liva_coach_act",
        {"domain": "future", "command": "record_v2", "payload": {"payload_v2": "B"}},
        scopes={"liva.read", "liva.write"},
    )

    assert version_b["ok"] is True
    assert version_b["data"]["contract_revision"] == "B"
    assert [tool for tool in list_tools() if tool["name"] in {"liva_coach_read", "liva_coach_act"}] == tool_manifest_before


def test_live_dispatcher_rejects_a_stale_payload_before_backend_execution(service):
    backend = MutableBackend()
    backend.revision = "B"
    backend.command = "record_v2"
    backend.required_field = "payload_v2"
    service.actions = backend

    result = call_tool(
        service,
        "liva_coach_act",
        {"domain": "future", "command": "record_v2", "payload": {"value": "stale"}},
        scopes={"liva.read", "liva.write"},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "missing_live_required_fields" in result["error"]["message"]


def test_pre_evergreen_chat_manifest_cannot_gain_a_new_domain_without_refresh():
    request = {"domain": "future", "command": "record_v2", "payload": {"payload_v2": "B"}}
    Draft202012Validator(COACH_ACT_SCHEMA).validate(request)

    legacy_snapshot = copy.deepcopy(COACH_ACT_SCHEMA)
    legacy_snapshot["properties"]["domain"] = {"type": "string", "enum": ["core", "training"]}
    with pytest.raises(ValidationError):
        Draft202012Validator(legacy_snapshot).validate(request)
