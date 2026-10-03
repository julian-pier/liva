from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def _extract_output_text(resp_json: dict[str, Any]) -> str:
    # Newer API: output_text field
    t = resp_json.get("output_text")
    if isinstance(t, str) and t.strip():
        return t.strip()

    # Fallback: walk output items and concatenate text parts
    out = resp_json.get("output")
    if not isinstance(out, list):
        return ""

    parts: list[str] = []
    for item in out:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("output_text", "text"):
                txt = c.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
    return "\n".join(parts).strip()


def responses_create(
    *,
    api_key: str,
    model: str,
    input_messages: list[dict[str, Any]],
    temperature: float = 0.7,
    top_p: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    max_output_tokens: int = 420,
    timeout_s: int = 45,
) -> str:
    """
    Minimal OpenAI Responses API client (no external dependency).
    Returns output_text as plain string.
    """
    if not api_key or not api_key.strip():
        raise RuntimeError("missing_openai_api_key")

    body = {
        "model": model,
        "input": input_messages,
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
    }
    if top_p is not None:
        body["top_p"] = float(top_p)
    if presence_penalty is not None:
        body["presence_penalty"] = float(presence_penalty)
    if frequency_penalty is not None:
        body["frequency_penalty"] = float(frequency_penalty)

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"openai_http_error:{e.code}:{raw[:300]}") from e
    except Exception as e:
        raise RuntimeError(f"openai_request_failed:{e}") from e

    try:
        resp_json = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"openai_bad_json:{raw[:300]}") from e

    return _extract_output_text(resp_json)
