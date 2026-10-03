from __future__ import annotations

import json
from pathlib import Path

from liva_mcp.memory_audit import apply_plan, normalization_plan, report, rollback
from liva_mcp.memory_policy import MemoryPolicy


class AuditClient:
    def __init__(self):
        self.rows = [{"name": "memos/legacy", "content": "#gpt #gpt #dauerhaft\n\nlegacy", "state": "NORMAL"}]
        self.requests = 0

    def _request(self, method, suffix, payload=None, **kwargs):
        self.requests += 1
        return {"memos": list(self.rows) if "ARCHIVED" not in suffix else []}

    def get(self, name):
        return next(row for row in self.rows if row["name"] == name)

    def update_content(self, name, content):
        row = self.get(name)
        row["content"] = content
        return row


def test_policy_file_and_audit_modes_are_read_only(tmp_path: Path):
    policy = MemoryPolicy.load(Path(__file__).parents[2] / "config" / "liva_memory_policy.yaml")
    client = AuditClient()
    before = json.dumps(client.rows, sort_keys=True)
    result = report(client, policy, tmp_path / "missing.sqlite3")
    plan = normalization_plan(client, policy, tmp_path / "missing.sqlite3")
    assert result["policy_version"] == 2 and result["gpt_count"] == 2 and result["duplicate_tag_entries"] == 1
    assert plan["write_applied"] is False and plan["actions"]
    action = plan["actions"][0]
    assert {"memo_name", "old_content_sha256", "new_content_sha256", "old_visible_tags", "new_visible_tags", "origin_decision", "domain_decision", "memory_type_decision", "confidence", "created_at", "updated_at", "change_type"} <= set(action)
    assert json.dumps(client.rows, sort_keys=True) == before


def test_apply_and_rollback_are_hash_guarded_and_restore_content(tmp_path: Path):
    policy = MemoryPolicy.default()
    client = AuditClient()
    client.rows[0].update({"createTime": "2026-01-01T00:00:00Z", "updateTime": "2026-01-01T00:00:00Z"})
    plan = normalization_plan(client, policy, tmp_path / "state.sqlite3")
    backup = tmp_path / "private" / "backup.json"
    result = apply_plan(client, plan, tmp_path / "state.sqlite3", backup)
    assert result["applied"] == 1 and backup.exists() and backup.stat().st_mode & 0o777 == 0o600
    assert client.rows[0]["content"].startswith("#liva")
    # A V2 canonical row is reversible through the same API contract.
    client.rows[0]["content"] = "#gpt #system #technik #temporary\n\nprobe"
    plan = normalization_plan(client, policy, tmp_path / "state.sqlite3")
    result = apply_plan(client, plan, tmp_path / "state.sqlite3", backup)
    assert result["applied"] == 1 and client.rows[0]["content"].startswith("#liva #system")
    restored = rollback(client, backup, tmp_path / "state.sqlite3")
    assert restored["restored"] == 1 and "#temporary" in client.rows[0]["content"]


def test_v2_keeps_body_hashtags_and_leaves_user_memos_untouched(tmp_path: Path):
    policy = MemoryPolicy.default()
    client = AuditClient()
    client.rows = [
        {"name": "memos/auto", "content": "#gpt #training #status\n\nText with #poetry stays.", "state": "NORMAL"},
        {"name": "memos/user", "content": "A user body hashtag #poetry stays.", "state": "NORMAL"},
    ]
    plan = normalization_plan(client, policy, tmp_path / "state.sqlite3")
    auto = next(item for item in plan["actions"] if item["memo_name"] == "memos/auto")
    user = next(item for item in plan["actions"] if item["memo_name"] == "memos/user")
    assert auto["new_visible_tags"] == ["#liva", "#training"]
    assert user["confidence"] == "user_unchanged"
    result = apply_plan(client, plan, tmp_path / "state.sqlite3", tmp_path / "backup.json")
    assert result["applied"] == 1 and "#poetry stays" in client.rows[0]["content"] and client.rows[1]["content"] == "A user body hashtag #poetry stays."
