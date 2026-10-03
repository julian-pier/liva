from __future__ import annotations

import re
from typing import Any


_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)(?:/home|/root|/tmp|/private|/Users)/[^\s\"']+"),
)


def redact_text(value: object, *, maximum: int = 500) -> str:
    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    return text[:maximum]


def redact(value: Any, *, text_maximum: int = 500) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if re.search(r"(?i)(authorization|api[_-]?key|token|password|secret)", str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact(item, text_maximum=text_maximum)
        return out
    if isinstance(value, list):
        return [redact(item, text_maximum=text_maximum) for item in value]
    if isinstance(value, tuple):
        return [redact(item, text_maximum=text_maximum) for item in value]
    if isinstance(value, str):
        return redact_text(value, maximum=text_maximum)
    return value


def safe_error(exc: BaseException, code: str = "read_failed") -> dict[str, str]:
    if code == "read_failed":
        return {"code": code, "message": "Read failed"}
    if code == "parse_error":
        return {"code": code, "message": "Invalid JSON-RPC message"}
    return {"code": code, "message": redact_text(exc) or "Read failed"}
