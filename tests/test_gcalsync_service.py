from __future__ import annotations

from types import SimpleNamespace

from googleapiclient.errors import HttpError

from gcalsync import service


def _http_error(status: int, reason: str) -> HttpError:
    resp = SimpleNamespace(status=status, reason=reason)
    content = (
        '{"error":{"errors":[{"reason":"rateLimitExceeded","message":"%s"}]}}' % reason
    ).encode("utf-8")
    return HttpError(resp, content)


def test_should_retry_http_for_google_quota_exceeded_message():
    exc = _http_error(403, "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'.")

    assert service._should_retry_http(exc) is True


def test_should_retry_http_for_explicit_rate_limit_exceeded_message():
    exc = _http_error(403, "rateLimitExceeded")

    assert service._should_retry_http(exc) is True
