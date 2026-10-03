from __future__ import annotations

from liva_mcp.redaction import redact, redact_text, safe_error


def test_redacts_artificial_tokens_and_headers():
    samples = [
        "Authorization: Bearer sk-example-1234567890",
        "api_key=super-secret-example-token",
        "token: abcdefghijklmnopqrstuvwxyz",
        "password=hunter-example",
    ]
    for sample in samples:
        result = redact_text(sample)
        assert "[REDACTED]" in result
        assert "example-token" not in result


def test_redacts_sensitive_mapping_keys_and_paths():
    result = redact({"token": "abc", "nested": {"authorization": "Bearer abc"}, "error": "/private/user/file"})
    assert result["token"] == "[REDACTED]"
    assert result["nested"]["authorization"] == "[REDACTED]"
    assert "/private/" not in result["error"]


def test_safe_error_contains_no_traceback_or_secret():
    result = safe_error(RuntimeError("Authorization: Bearer artificial-secret-token"))
    assert "artificial-secret-token" not in result["message"]
    assert "traceback" not in result["message"].lower()


def test_unexpected_errors_are_not_reflected_to_client():
    result = safe_error(RuntimeError("private opaque value without a recognizable key"))
    assert result == {"code": "read_failed", "message": "Read failed"}
