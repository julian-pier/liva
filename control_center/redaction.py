from __future__ import annotations

import re
from typing import Any


_KEY_RE = re.compile(r"(?i)(token|password|passphrase|secret|api[_-]?key|authorization|cookie|credential)")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:token|password|passphrase|secret|api[_-]?key|authorization|cookie)[a-z0-9_.-]*)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")
_AUTH_HEADER_RE = re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+[^\s,;]+")


def redact_text(value: object) -> str:
    text = str(value or "")
    text = _AUTH_HEADER_RE.sub("Authorization: [REDACTED]", text)
    text = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return _BEARER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", text)


def redact(value: Any, *, key: str = "") -> Any:
    """Redact nested payloads, including exception text, before an API response."""
    if _KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
