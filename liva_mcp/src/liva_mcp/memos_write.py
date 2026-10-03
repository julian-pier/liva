from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request


class MemosWriteClient:
    """Small authenticated Memos client with no redirect following."""

    def __init__(self, base_url: str | None, read_token: str | None, write_token: str | None):
        self.base_url = (base_url or "").rstrip("/")
        self.read_token = read_token
        self.write_token = write_token

    def _request(self, method: str, suffix: str, payload: dict[str, Any] | None = None, *, write: bool = False) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("usememos is not configured")
        token = self.write_token if write else (self.read_token or self.write_token)
        if write and not token:
            raise RuntimeError("usememos capture write access is not configured")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(f"{self.base_url}/api/v1/memos{suffix}", data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=15) as response:
                raw = response.read(1_048_577)
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise RuntimeError("usememos request failed") from exc
        if len(raw) > 1_048_576:
            raise RuntimeError("usememos response exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("usememos returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("usememos returned an invalid response")
        return value

    def search(self, limit: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        for _page in range(5):
            query = {"pageSize": 100, "state": "NORMAL"}
            if page_token:
                query["pageToken"] = page_token
            result = self._request("GET", "?" + parse.urlencode(query))
            page_rows = result.get("memos")
            if isinstance(page_rows, list):
                rows.extend(row for row in page_rows if isinstance(row, dict))
            page_token = result.get("nextPageToken") or result.get("next_page_token")
            if not page_token or len(rows) >= 500:
                break
        return rows

    def get(self, name: str) -> dict[str, Any]:
        memo_id = name.removeprefix("memos/")
        return self._request("GET", "/" + parse.quote(memo_id, safe=""))

    def create(self, content: str, tags: list[str]) -> dict[str, Any]:
        return self._request("POST", "", {"state": "NORMAL", "visibility": "PRIVATE", "content": content}, write=True)

    def update_content(self, name: str, content: str, *, create_time: str | None = None) -> dict[str, Any]:
        memo_id = name.removeprefix("memos/")
        update_fields = ["content"]
        payload = {"content": content}
        if create_time:
            update_fields.append("create_time")
            payload["createTime"] = create_time
        suffix = "/" + parse.quote(memo_id, safe="") + "?" + parse.urlencode({"updateMask": ",".join(update_fields)})
        return self._request("PATCH", suffix, payload, write=True)

    def archive(self, name: str) -> dict[str, Any]:
        memo_id = name.removeprefix("memos/")
        suffix = "/" + parse.quote(memo_id, safe="") + "?" + parse.urlencode({"updateMask": "state"})
        return self._request("PATCH", suffix, {"state": "ARCHIVED"}, write=True)
