from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi


class UploadError(RuntimeError):
    pass


def upload_daily(server_url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}/api/activity/iphone/daily",
        data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "LIVA-StayFree-iOS-Bridge/1.0",
        },
        method="POST",
    )
    try:
        tls_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise UploadError(f"LIVA HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise UploadError(f"LIVA unavailable: {exc}") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise UploadError("LIVA did not acknowledge the import")
    return body
