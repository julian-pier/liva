import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error, parse, request


_TIME_TAG_RULES = [
    ("#projekt", ("liva", "liva", "usememos", "api", "infra", "design", "code", "repo", "core")),
    ("#kurzfristig", ("krank", "hals", "schleim", "erkalt", "symptom", "tagesform", "schlaf", "hrv-alarm", "ruhepuls", "puls hoch", "planänder", "akut")),
    ("#diese_woche", ("woche", "wochenplan", "klausurwoche", "aktueller block", "diese woche", "block")),
    ("#temporär", ("experiment", "test", "übergang", "uebergang", "vorübergehend", "temporaer", "temporär")),
    ("#dauerhaft", ("präferenz", "praeferenz", "immer", "langfrist", "stabil", "identität", "identitaet", "muster", "systemregel", "regel")),
]
_CONTENT_TAG_RULES = [
    ("#krankheit", ("krank", "hals", "schleim", "erkält", "erkaelt", "symptom", "erkalt")),
    ("#hrv", ("hrv", "ans", "puls", "ruhepuls", "schlafdaten", "polar", "rmssd")),
    ("#liva", ("liva", "liva", "usememos", "api", "core")),
    ("#training", ("training", "rpe", "volumen", "übung", "uebung", "plan", "session")),
    ("#recovery", ("erholung", "müd", "mued", "deload", "recovery", "fatigue")),
    ("#nutrition", ("kcal", "makro", "essen", "cut", "protein", "carb", "fett", "nutrition")),
    ("#schule", ("schule", "klausur", "abi", "unterricht")),
    ("#gym", ("gym", "studio", "topfit", "mobile")),
    ("#auto", ("auto", "fahren", "parken", "führerschein", "fuehrerschein")),
    ("#privat", ("beziehung", "freundschaft", "familie", "gefühle", "gefuehle", "privat")),
]
SHORT_TERM_TTL_TAGS = {"kurzfristig", "diese_woche", "temporär", "temporaer"}


def _configured_base_url() -> str:
    return str(os.getenv("MEMOS_BASE_URL") or "http://127.0.0.1:5230").strip() or "http://127.0.0.1:5230"


def _api_base_url() -> str:
    return _configured_base_url().rstrip("/") + "/api/v1"


def _access_token(required: bool = False) -> str | None:
    token = str(os.getenv("MEMOS_ACCESS_TOKEN") or os.getenv("MEMOS_TOKEN") or "").strip()
    if required and not token:
        raise ValueError("MEMOS_ACCESS_TOKEN or MEMOS_TOKEN is required for memos write access")
    return token or None


def _default_visibility() -> str:
    return str(os.getenv("MEMOS_DEFAULT_VISIBILITY") or "PRIVATE").strip() or "PRIVATE"


def _normalize_explicit_tags(explicit_tags: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in explicit_tags or []:
        value = str(raw).strip()
        if not value:
            continue
        cleaned = "#" + re.sub(r"\s+", "_", value.lstrip("#"))
        cleaned = re.sub(r"[^#\wäöüÄÖÜß-]", "_", cleaned)
        if cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def extract_terminal_tags(content: str) -> list[str]:
    lines = content.rstrip().splitlines()
    if not lines:
        return []
    last_line = lines[-1].strip()
    if not last_line:
        return []
    parts = [part for part in last_line.split() if part.startswith("#")]
    if parts and " ".join(parts) == last_line:
        unique: list[str] = []
        for part in parts:
            if part not in unique:
                unique.append(part)
        return unique
    return []


def normalize_tag(raw: str) -> str:
    return re.sub(r"\s+", "_", str(raw or "").strip().lower().lstrip("#"))


def memos_enabled() -> bool:
    backend = str(os.getenv("LIVA_MEMORY_BACKEND") or "").strip().lower()
    base_url_set = bool(str(os.getenv("MEMOS_BASE_URL") or "").strip())
    return backend == "memos" or base_url_set


def ensure_gpt_prefix(content: str) -> str:
    if content.startswith("#gpt"):
        return content
    return "#gpt\n\n" + content


def normalize_memo_name(name_or_id: str) -> str:
    value = str(name_or_id or "").strip()
    if not value:
        raise ValueError("memo name or id is required")
    return value if value.startswith("memos/") else f"memos/{value}"


def _memo_resource_url(name_or_id: str) -> str:
    memo_name = normalize_memo_name(name_or_id)
    memo_id = memo_name.split("/", 1)[1]
    return _api_base_url() + "/memos/" + parse.quote(memo_id, safe="")


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None, require_token: bool = False) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    token = _access_token(required=require_token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Memos API request failed with HTTP {exc.code}: {detail[:500]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Memos API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Memos API returned invalid JSON") from exc


def is_gpt_memo(memo: dict) -> bool:
    return str(memo.get("content") or "").startswith("#gpt")


def memo_tags(memo: dict) -> list[str]:
    normalized: list[str] = []
    for raw in memo.get("tags") or []:
        tag = normalize_tag(str(raw))
        if tag and tag not in normalized:
            normalized.append(tag)
    for raw in extract_terminal_tags(str(memo.get("content") or "")):
        tag = normalize_tag(raw)
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized


def memo_has_any_tag(memo: dict, tags: set[str]) -> bool:
    if not tags:
        return False
    memo_tag_set = set(memo_tags(memo))
    wanted = {normalize_tag(tag) for tag in tags if normalize_tag(tag)}
    return bool(memo_tag_set & wanted)


def is_short_term_gpt_memo(memo: dict) -> bool:
    return is_gpt_memo(memo) and memo_has_any_tag(memo, SHORT_TERM_TTL_TAGS)


def parse_memo_create_time(memo: dict) -> datetime | None:
    raw = str(memo.get("createTime") or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def build_memory_tags(content: str, topic: str | None = None, command: str | None = None, explicit_tags: list[str] | None = None) -> list[str]:
    text = " ".join(str(part or "") for part in (content, topic, command)).lower()
    explicit = _normalize_explicit_tags(explicit_tags)
    time_tag = next((tag for tag, needles in _TIME_TAG_RULES if any(needle in text for needle in needles)), "#beobachtung")
    content_tag = next((tag for tag, needles in _CONTENT_TAG_RULES if any(needle in text for needle in needles)), None)
    tags: list[str] = [time_tag]
    if explicit:
        for tag in explicit:
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= 2:
                break
    if len(tags) < 2 and content_tag and content_tag not in tags:
        tags.append(content_tag)
    if len(tags) < 2 and not content_tag and explicit:
        for tag in explicit:
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= 2:
                break
    if len(tags) < 2 and content_tag is None and not explicit and time_tag == "#beobachtung":
        tags.append("#notiz")
    unique: list[str] = []
    for tag in tags:
        if tag and tag.startswith("#") and tag not in unique:
            unique.append(tag)
    return unique[:2]


def append_terminal_tags(content: str, tags: list[str]) -> str:
    if not tags:
        return content
    normalized = []
    for tag in tags:
        value = str(tag).strip()
        if value and value.startswith("#") and value not in normalized:
            normalized.append(value)
    if not normalized:
        return content
    existing_terminal = extract_terminal_tags(content)
    if existing_terminal == normalized:
        return content
    return content.rstrip() + "\n\n" + " ".join(normalized) + "\n"


def prepare_gpt_memo_content(content: str, topic: str | None = None, command: str | None = None, tags: list[str] | None = None) -> str:
    prefixed = ensure_gpt_prefix(content)
    auto_tags = build_memory_tags(prefixed, topic=topic, command=command, explicit_tags=tags)
    return append_terminal_tags(prefixed, auto_tags)


def create_memo(content: str, visibility: str | None = None, state: str = "NORMAL") -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content is required")
    payload = {
        "state": str(state or "NORMAL").strip() or "NORMAL",
        "content": ensure_gpt_prefix(content),
        "visibility": str(visibility or _default_visibility()).strip() or _default_visibility(),
    }
    return _json_request("POST", _api_base_url() + "/memos", payload=payload, require_token=True)


def get_memo(name_or_id: str) -> dict[str, Any]:
    return _json_request("GET", _memo_resource_url(name_or_id), require_token=False)


def update_memo(name_or_id: str, content: str | None = None, state: str | None = None, visibility: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    update_mask: list[str] = []
    if content is not None:
        payload["content"] = ensure_gpt_prefix(content)
        update_mask.append("content")
    if state is not None:
        payload["state"] = str(state)
        update_mask.append("state")
    if visibility is not None:
        payload["visibility"] = str(visibility)
        update_mask.append("visibility")
    if not update_mask:
        raise ValueError("At least one field must be set for update_memo")
    url = _memo_resource_url(name_or_id) + "?" + parse.urlencode({"updateMask": ",".join(update_mask)})
    return _json_request("PATCH", url, payload=payload, require_token=True)


def delete_memo(name_or_id: str, force: bool = False) -> dict[str, Any]:
    name = normalize_memo_name(name_or_id)
    url = _memo_resource_url(name_or_id) + "?" + parse.urlencode({"force": str(bool(force)).lower()})
    response = _json_request("DELETE", url, require_token=True)
    return response or {"deleted": True, "name": name}


def archive_memo(name_or_id: str) -> dict[str, Any]:
    return update_memo(name_or_id, state="ARCHIVED")


def list_memos(
    page_size: int = 20,
    page_token: str | None = None,
    state: str = "NORMAL",
    filter: str | None = None,
    order_by: str | None = None,
) -> dict[str, Any]:
    query = {"pageSize": max(1, min(int(page_size or 20), 100))}
    if page_token:
        query["pageToken"] = str(page_token)
    if state:
        query["state"] = str(state)
    if filter:
        query["filter"] = str(filter)
    if order_by:
        query["orderBy"] = str(order_by)
    url = _api_base_url() + "/memos?" + parse.urlencode(query)
    return _json_request("GET", url, require_token=False)


def search_memos(
    query: str | None,
    tags: list[str] | None,
    limit: int = 20,
    include_gpt: bool | None = None,
    keywords: list[str] | None = None,
    any_tags: list[str] | None = None,
    all_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> dict[str, Any]:
    response = list_memos(page_size=max(1, min(int(limit or 20), 100)))
    memos = response.get("memos") if isinstance(response.get("memos"), list) else []
    query_text = str(query or "").strip().lower()
    keyword_filters = [str(keyword).strip().lower() for keyword in (keywords or []) if str(keyword).strip()]
    required_tags = [normalize_tag(tag) for tag in (tags or []) if normalize_tag(tag)]
    required_tags.extend(normalize_tag(tag) for tag in (all_tags or []) if normalize_tag(tag))
    any_tag_filters = [normalize_tag(tag) for tag in (any_tags or []) if normalize_tag(tag)]
    exclude_tag_filters = [normalize_tag(tag) for tag in (exclude_tags or []) if normalize_tag(tag)]
    filtered: list[dict[str, Any]] = []
    for memo in memos:
        if not isinstance(memo, dict):
            continue
        content = str(memo.get("content") or "")
        name = str(memo.get("name") or "")
        snippet = str(memo.get("snippet") or "")
        memo_tag_list = memo_tags(memo)
        memo_tag_set = set(memo_tag_list)
        is_gpt = content.startswith("#gpt")
        haystack = " ".join((content, snippet, name, str(memo.get("title") or ""))).lower()
        if query_text and query_text not in haystack:
            continue
        if keyword_filters and not all(keyword in haystack for keyword in keyword_filters):
            continue
        if required_tags and not all(tag in memo_tag_set for tag in required_tags):
            continue
        if any_tag_filters and not any(tag in memo_tag_set for tag in any_tag_filters):
            continue
        if exclude_tag_filters and any(tag in memo_tag_set for tag in exclude_tag_filters):
            continue
        if include_gpt is True and not is_gpt:
            continue
        if include_gpt is False and is_gpt:
            continue
        filtered.append(memo)
    filtered = filtered[: max(1, int(limit or 20))]
    return {"memos": filtered, "count": len(filtered), "backend": "usememos"}


def cleanup_expired_gpt_memos(max_age_days: int = 14, dry_run: bool = True, limit: int = 500) -> dict:
    max_age_days = max(1, min(int(max_age_days or 14), 90))
    response = list_memos(page_size=min(max(1, int(limit or 500)), 500), state="NORMAL")
    memos = response.get("memos") if isinstance(response.get("memos"), list) else []
    now = datetime.now(timezone.utc)
    expired: list[dict[str, Any]] = []
    deleted_count = 0
    skipped_non_gpt = 0
    for memo in memos:
        if not isinstance(memo, dict):
            continue
        if not is_gpt_memo(memo):
            skipped_non_gpt += 1
            continue
        if not memo_has_any_tag(memo, SHORT_TERM_TTL_TAGS):
            continue
        created_at = parse_memo_create_time(memo)
        if created_at is None:
            continue
        age_days = max(0, (now - created_at.astimezone(timezone.utc)).days)
        if age_days < max_age_days:
            continue
        expired_item = {
            "name": memo.get("name"),
            "createTime": memo.get("createTime"),
            "tags": memo_tags(memo),
            "age_days": age_days,
        }
        expired.append(expired_item)
        if not dry_run:
            delete_memo(str(memo.get("name") or ""), force=False)
            deleted_count += 1
    return {
        "backend": "usememos",
        "dry_run": bool(dry_run),
        "max_age_days": max_age_days,
        "scanned": len(memos),
        "expired_count": len(expired),
        "deleted_count": deleted_count,
        "skipped_non_gpt": skipped_non_gpt,
        "expired": expired,
    }
