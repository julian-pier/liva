from __future__ import annotations

import json
from urllib import error

import pytest

from liva_mcp.actions_bridge import ActionsBridge
from liva_mcp.config import read_actions_token_file


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.raw[:limit] if limit >= 0 else self.raw

    def close(self):
        return None


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_bridge_allows_only_loopback_http_origins():
    for url in ("https://127.0.0.1:5000", "http://example.com", "http://user:pass@localhost:5000", "http://localhost:5000/path"):
        with pytest.raises(ValueError):
            ActionsBridge(url, "secret")
    assert ActionsBridge("http://127.0.0.1:5000", "secret").configured is True


def test_bridge_token_file_must_be_private(tmp_path):
    token_file = tmp_path / "bridge-token"
    token_file.write_text("a" * 64, encoding="utf-8")
    token_file.chmod(0o600)
    assert read_actions_token_file(str(token_file)) == "a" * 64
    token_file.chmod(0o640)
    with pytest.raises(ValueError):
        read_actions_token_file(str(token_file))


def test_read_posts_to_fixed_canonical_endpoint(monkeypatch):
    opener = FakeOpener(FakeResponse({"ok": True, "mode": "training_deep_dive", "data": {"sets": 10}}))
    monkeypatch.setattr("liva_mcp.actions_bridge.request.build_opener", lambda *_args: opener)

    result = ActionsBridge("http://127.0.0.1:5000", "bridge-token").read(
        "training_deep_dive", {"date": "2026-08-04", "mode": "memory"}
    )

    req, timeout = opener.requests[0]
    assert req.full_url == "http://127.0.0.1:5000/api/v2/actions/liva/read"
    assert req.get_header("Authorization") == "Bearer bridge-token"
    assert req.get_header("X-liva-mcp-bridge") == "1"
    assert json.loads(req.data) == {
        "mode": "training_deep_dive",
        "date": "2026-08-04",
        "payload": {"mode": "memory"},
    }
    assert timeout == 45
    assert result["source"] == "canonical_actions" and result["production_read"] is True


def test_read_preserves_payload_mode_alias_on_outer_mode_collision(monkeypatch):
    opener = FakeOpener(FakeResponse({"ok": True, "mode": "nutrition_data", "data": {"days": []}}))
    monkeypatch.setattr("liva_mcp.actions_bridge.request.build_opener", lambda *_args: opener)

    ActionsBridge("http://127.0.0.1:5000", "bridge-token").read(
        "nutrition_data", {"mode": "daily_totals"}
    )

    req, _ = opener.requests[0]
    assert json.loads(req.data) == {
        "mode": "nutrition_data",
        "payload": {"mode": "daily_totals"},
    }


def test_act_defaults_are_forwarded_and_live_change_is_reported(monkeypatch):
    opener = FakeOpener(FakeResponse({
        "ok": True,
        "execution": {"live_state_changed": True, "affected_ids": [42]},
        "readback": {"workout_id": 42},
    }))
    monkeypatch.setattr("liva_mcp.actions_bridge.request.build_opener", lambda *_args: opener)

    result = ActionsBridge("http://localhost:5000", "bridge-token").act(
        "training", "log_gym_session", {"date": "2026-08-04"},
        dry_run=False, confirm=False, reason="user explicitly logged workout",
    )

    req, _ = opener.requests[0]
    body = json.loads(req.data)
    assert req.full_url == "http://localhost:5000/api/v2/actions/liva/act"
    assert body["dry_run"] is False and body["confirm"] is False
    assert body["response_profile"] == "mcp_compact"
    assert body["payload"]["reason"] == "user explicitly logged workout"
    assert result["production_write"] is True
    assert result["dry_run"] is False
    assert result["executed"] is True and result["changed"] is True
    assert result["readback_source"] == "liva.actions_v2"


def test_training_card_refresh_uses_fixed_private_endpoint(monkeypatch):
    opener = FakeOpener(FakeResponse({
        "ok": True,
        "date": "2026-08-05",
        "invalidated": True,
        "readback": {"decision_id": 98, "status": "fresh"},
    }))
    monkeypatch.setattr("liva_mcp.actions_bridge.request.build_opener", lambda *_args: opener)

    result = ActionsBridge("http://127.0.0.1:5000", "bridge-token").refresh_training_card("2026-08-05")

    req, _ = opener.requests[0]
    assert req.full_url == "http://127.0.0.1:5000/api/v2/actions/training/today/card/refresh"
    assert json.loads(req.data) == {"date": "2026-08-05"}
    assert result["invalidated"] is True
    assert result["readback_source"] == "liva.training_today_card"


def test_upstream_rejection_is_safe_and_does_not_follow_redirects(monkeypatch):
    rejected = error.HTTPError(
        "http://127.0.0.1:5000/api/v2/actions/liva/act",
        400,
        "Bad Request",
        {},
        FakeResponse({"ok": False, "error": {"code": "confirmation_required", "message": "confirm=true required"}}),
    )
    opener = FakeOpener(rejected)
    monkeypatch.setattr("liva_mcp.actions_bridge.request.build_opener", lambda *_args: opener)

    with pytest.raises(ValueError, match="confirmation_required"):
        ActionsBridge("http://127.0.0.1:5000", "bridge-token").act(
            "training", "delete_training_plan", {}, dry_run=False, confirm=False, reason=None
        )
