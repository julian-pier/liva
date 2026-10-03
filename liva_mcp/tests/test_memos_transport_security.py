from __future__ import annotations

import json
from pathlib import Path
from urllib import parse, request

import pytest

from liva_mcp.config import Config
from liva_mcp.memos_write import MemosWriteClient
from liva_mcp.read_service import MAX_MEMOS_RESPONSE_BYTES, ReadService, _NoRedirectHandler


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/memos",
        "http://user:password@memos.invalid",
        "http://memos.invalid/path",
        "http://memos.invalid?target=other",
    ],
)
def test_rejects_unexpected_base_urls(repo_root: Path, url: str):
    with pytest.raises(ValueError):
        Config(repo_root=repo_root, memos_base_url=url)


def test_redirects_are_not_followed_and_cannot_forward_authorization():
    original = request.Request(
        "http://memos.invalid/api/v1/memos",
        headers={"Authorization": "Bearer artificial-read-token"},
        method="GET",
    )
    assert _NoRedirectHandler().redirect_request(
        original, None, 302, "Found", {}, "http://attacker.invalid/collect"
    ) is None


def test_oversized_memos_response_is_rejected(monkeypatch, repo_root: Path):
    body = json.dumps({"memos": [{"name": "memos/1", "content": "x" * MAX_MEMOS_RESPONSE_BYTES}]}).encode()

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return body[:limit]

    class Opener:
        def open(self, req, timeout):
            return Response()

    monkeypatch.setattr("liva_mcp.read_service.request.build_opener", lambda *handlers: Opener())
    service = ReadService(Config(repo_root=repo_root, memos_base_url="http://memos.invalid"))
    with pytest.raises(RuntimeError, match="size limit"):
        service.memory_search(None, 1)


def test_append_update_can_move_memo_timeline_date_in_same_patch(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return b'{"name":"memos/abc","content":"updated"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr("liva_mcp.memos_write.request.urlopen", fake_urlopen)
    client = MemosWriteClient("http://memos.invalid", "read-token", "write-token")
    timestamp = "2026-07-31T01:23:45+02:00"

    client.update_content("memos/abc", "updated", create_time=timestamp)

    query = parse.parse_qs(parse.urlsplit(captured["url"]).query)
    assert captured["method"] == "PATCH"
    assert query["updateMask"] == ["content,create_time"]
    assert captured["body"] == {"content": "updated", "createTime": timestamp}
