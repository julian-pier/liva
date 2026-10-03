from __future__ import annotations

import json

from liva_mcp.server import MAX_TOOL_RESPONSE_BYTES, McpServer


class LargeService:
    def training_plan(self):
        return {"plan": ["x" * 400 for _ in range((MAX_TOOL_RESPONSE_BYTES // 400) + 10)]}


def test_mcp_replaces_oversized_tool_response_with_bounded_error():
    response = McpServer(LargeService()).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "liva_training_plan", "arguments": {}},
        }
    )
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    assert result["isError"] is True
    assert payload["error"]["code"] == "response_too_large"
    assert len(result["content"][0]["text"].encode()) < MAX_TOOL_RESPONSE_BYTES


def test_unknown_notification_gets_no_response(service):
    assert McpServer(service).handle({"jsonrpc": "2.0", "method": "notifications/unknown"}) is None
