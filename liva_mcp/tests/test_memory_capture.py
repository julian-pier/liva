"""Cutover regressions: the Remote MCP has no legacy-Memos memory surface."""
from __future__ import annotations

from pathlib import Path

from liva_mcp.config import Config
from liva_mcp.read_service import ReadService
from liva_mcp.remote_server import REMOTE_MEMORY_TOOL_NAMES
from liva_mcp.tool_registry import TOOL_BY_NAME, call_tool, list_tools


VISIBLE_MEMORY_TOOLS = [
    "liva_memory_context", "liva_memory_recall", "liva_memory_audit",
    "liva_memory_begin", "liva_memory_finish",
    "liva_memory_import_file", "liva_memory_source",
]
LEGACY_MEMORY_TOOLS = {
    "liva_capture_memory", "liva_archive_memory", "liva_upsert_memory_entry",
    "liva_memory_search", "liva_memory_read",
}


def test_remote_memory_discovery_is_the_v2_contract():
    """The published Remote registration is the only ChatGPT memory surface."""
    assert list(REMOTE_MEMORY_TOOL_NAMES) == VISIBLE_MEMORY_TOOLS


def test_legacy_memory_tools_are_not_registered_or_callable(tmp_path: Path):
    database = tmp_path / "database"; database.mkdir()
    for name in ("training", "plans", "hrv", "ernaehrung", "runs", "polar", "core"):
        (database / f"{name}.sqlite3").touch()
    service = ReadService(Config(repo_root=tmp_path, database_root=database, write_state_path=tmp_path / "writes.sqlite3"))
    listed = {item["name"] for item in list_tools()}
    assert not (listed & LEGACY_MEMORY_TOOLS)
    assert not (set(TOOL_BY_NAME) & LEGACY_MEMORY_TOOLS)
    for name in LEGACY_MEMORY_TOOLS:
        assert call_tool(service, name, {}, scopes={"liva.write"})["error"]["code"] == "unknown_tool"


def test_v2_tool_metadata_never_declares_memos_as_production_memory():
    for name in VISIBLE_MEMORY_TOOLS:
        if name in TOOL_BY_NAME:
            description = TOOL_BY_NAME[name].description.casefold()
            assert "canonical memory" in description or "memory v2" in description
            assert "usememos production" not in description
