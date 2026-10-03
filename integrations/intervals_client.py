from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class IntervalsClientError(RuntimeError):
    code = "intervals_error"
    retryable = False

    def __init__(self, message: str, *, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class IntervalsNotConfiguredError(IntervalsClientError):
    code = "intervals_not_configured"


class IntervalsAuthError(IntervalsClientError):
    code = "intervals_auth_failed"


class IntervalsRateLimitError(IntervalsClientError):
    code = "intervals_rate_limited"
    retryable = True


class IntervalsValidationError(IntervalsClientError):
    code = "intervals_validation_error"


class IntervalsNetworkError(IntervalsClientError):
    code = "intervals_network_error"
    retryable = True


class IntervalsServerError(IntervalsClientError):
    code = "intervals_server_error"
    retryable = True


class IntervalsClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, timeout_seconds: float = 12.0) -> None:
        self.api_key = (api_key or _read_env_or_dotenv("INTERVALS_API_KEY") or "").strip()
        self.base_url = (base_url or _read_env_or_dotenv("INTERVALS_BASE_URL") or "https://intervals.icu/api/v1").strip().rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _auth_header(self) -> str:
        if not self.configured:
            raise IntervalsNotConfiguredError("INTERVALS_API_KEY fehlt.")
        token = base64.b64encode(f"API_KEY:{self.api_key}".encode()).decode("ascii")
        return f"Basic {token}"

    def _request_json(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        if not self.configured:
            raise IntervalsNotConfiguredError("INTERVALS_API_KEY fehlt.")
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")}, doseq=True)
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": self._auth_header(), "User-Agent": "LIVA/EnduranceSync"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = str(exc)
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            cls = IntervalsAuthError if exc.code in {401, 403} else IntervalsRateLimitError if exc.code == 429 else IntervalsValidationError if 400 <= exc.code < 500 else IntervalsServerError
            raise cls(f"Intervals HTTP {exc.code}: {detail}", status=exc.code, retry_after=retry_after) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise IntervalsNetworkError(f"Intervals nicht erreichbar: {getattr(exc, 'reason', exc)}") from exc
        except Exception as exc:
            raise IntervalsNetworkError(f"Intervals Anfrage fehlgeschlagen: {exc}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            raise IntervalsClientError("Intervals Antwort ist kein gültiges JSON.") from exc

    def get_wellness(self, oldest: str, newest: str) -> Any:
        return self._request_json("GET", "athlete/0/wellness", {"oldest": oldest, "newest": newest})

    def get_activities(self, oldest: str, newest: str) -> Any:
        return self._request_json("GET", "athlete/0/activities", {"oldest": oldest, "newest": newest})

    def get_events(self, oldest: str, newest: str) -> Any:
        return self._request_json("GET", "athlete/0/events", {"oldest": oldest, "newest": newest})

    def get_event(self, event_id: int | str) -> Any:
        return self._request_json("GET", f"athlete/0/events/{event_id}")

    def upsert_events(self, events: list[dict[str, Any]]) -> Any:
        return self._request_json("POST", "athlete/0/events/bulk", {"upsert": "true"}, events)

    def delete_events(self, events: list[dict[str, Any]]) -> Any:
        return self._request_json("PUT", "athlete/0/events/bulk-delete", body=events)


def _retry_after_seconds(value: str | None) -> int | None:
    try:
        return max(1, int(value or ""))
    except ValueError:
        return None


def _read_env_or_dotenv(key: str) -> str | None:
    if key in os.environ:
        return str(os.environ[key]).strip() or None
    dotenv_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        raw = dotenv_path.read_text(encoding="utf-8")
    except Exception:
        return None
    prefix = f"{key}="
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip() or None
    return None
