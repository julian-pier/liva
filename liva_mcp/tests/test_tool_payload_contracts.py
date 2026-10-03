from __future__ import annotations

from liva_mcp.tool_registry import TOOL_BY_NAME
from liva_mcp.remote_server import REMOTE_MEMORY_TOOL_NAMES


def test_training_decision_write_publishes_its_validated_payload_contract():
    schema = TOOL_BY_NAME["liva_training_decision_write"].input_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["time_available_min"] == {
        "type": ["integer", "null"],
        "minimum": 10,
        "maximum": 360,
    }
    assert {"date", "dry_run", "reason", "idempotency_key", "constraints"}.issubset(schema["properties"])


def test_remote_memory_contract_has_no_legacy_compatibility_tool():
    legacy = {"liva_capture_memory", "liva_archive_memory", "liva_upsert_memory_entry", "liva_memory_search", "liva_memory_read"}
    assert not (legacy & set(TOOL_BY_NAME))
    assert not (legacy & set(REMOTE_MEMORY_TOOL_NAMES))
