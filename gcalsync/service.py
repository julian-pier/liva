from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httplib2
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_httplib2 import AuthorizedHttp

from app_support.request_timing import record_timing, timed_block

SCOPES = ("https://www.googleapis.com/auth/calendar",)
_CALENDAR_ID_CACHE: dict[str, str] = {}
_HTTP_TIMEOUT_SECONDS = 2.0
_MAX_ATTEMPTS = max(1, int((os.getenv("LIVA_GCAL_MAX_ATTEMPTS") or "4").strip() or "4"))


class GoogleCalendarAuthError(RuntimeError):
    pass


class GoogleCalendarConfigError(RuntimeError):
    pass


def is_rate_limit_error(exc: HttpError) -> bool:
    status = int(getattr(exc.resp, "status", 0) or 0)
    if status not in {403, 429}:
        return False
    text = str(exc).lower()
    tokens = (
        "ratelimitexceeded",
        "rate limit exceeded",
        "rate limit",
        "ratelimit",
        "quota exceeded",
        "quotaexceeded",
        "userratelimitexceeded",
        "userratelimit",
        "queries per minute per user",
    )
    return any(token in text for token in tokens)


def _should_retry_http(exc: HttpError) -> bool:
    status = int(getattr(exc.resp, "status", 0) or 0)
    if status in {429, 500, 502, 503, 504}:
        return True
    if status == 403:
        return is_rate_limit_error(exc) or ("usagelimits" in str(exc).lower())
    return False


class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", _HTTP_TIMEOUT_SECONDS)
        return super().request(method, url, **kwargs)


def _execute_with_backoff(request, *, max_attempts: int = _MAX_ATTEMPTS, label: str = "google.execute"):
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            with timed_block(label, attempt=attempt):
                return request.execute(num_retries=0)
        except HttpError as exc:
            if attempt >= max_attempts or not _should_retry_http(exc):
                raise
            sleep_for = delay + random.uniform(0.0, 0.35)
            record_timing("google.backoff.sleep", sleep_for, attempt=attempt)
            time.sleep(sleep_for)
            delay = min(delay * 2.0, 20.0)


def bootstrap_token(
    credentials_file: Path,
    token_file: Path,
    *,
    local_server: bool = True,
    local_port: int = 8080,
) -> Path:
    if not credentials_file.exists():
        raise GoogleCalendarAuthError(
            f"Google credentials file missing: {credentials_file}. "
            "Create OAuth client in Google Cloud and download JSON."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    if local_server:
        creds = flow.run_local_server(host="localhost", port=local_port, open_browser=False)
    else:
        redirect_uris = (
            (flow.client_config.get("installed") or {}).get("redirect_uris")
            or (flow.client_config.get("web") or {}).get("redirect_uris")
            or []
        )
        loopback = next((u for u in redirect_uris if u.startswith("http://localhost")), None)
        flow.redirect_uri = loopback or "http://localhost"
        auth_url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        print("[GCal] Open this URL in your browser:")
        print(auth_url)
        print(
            "[GCal] After login Google redirects to localhost. "
            "If the page cannot be reached, copy the 'code=' value from the URL."
        )
        code = input("[GCal] Paste authorization code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return token_file


def build_service(credentials_file: Path, token_file: Path):
    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        with timed_block("google.credentials.refresh"):
            creds.refresh(Request(session=_TimeoutSession()))
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise GoogleCalendarAuthError(
            "Google token invalid/missing. Run scripts/google_calendar_auth.py first."
        )
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS))
    with timed_block("google.build_service"):
        return build("calendar", "v3", http=http, cache_discovery=False)


def list_calendars(service) -> list[dict[str, Any]]:
    page_token = None
    out: list[dict[str, Any]] = []
    while True:
        resp = _execute_with_backoff(service.calendarList().list(pageToken=page_token), label="google.calendar_list")
        out.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def list_readable_calendars(service, *, include_hidden: bool = False, include_free_busy_reader: bool = False) -> list[dict[str, Any]]:
    allowed_roles = {"owner", "writer", "reader"}
    if include_free_busy_reader:
        allowed_roles.add("freeBusyReader")
    out: list[dict[str, Any]] = []
    for item in list_calendars(service):
        if not isinstance(item, dict):
            continue
        if not include_hidden and bool(item.get("hidden")):
            continue
        access_role = str(item.get("accessRole") or "").strip()
        if access_role not in allowed_roles:
            continue
        calendar_id = str(item.get("id") or "").strip()
        if not calendar_id:
            continue
        out.append(item)
    return out


def find_calendar_by_summary(service, summary: str) -> dict[str, Any] | None:
    want = (summary or "").strip().lower()
    if not want:
        return None
    for item in list_calendars(service):
        if str(item.get("summary") or "").strip().lower() == want:
            return item
    return None


def create_calendar(service, *, summary: str, timezone: str) -> dict[str, Any]:
    body = {"summary": summary, "timeZone": timezone}
    return _execute_with_backoff(service.calendars().insert(body=body), label="google.calendar_create")


def get_or_create_calendar(service, *, name: str, timezone: str) -> str:
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("calendar_name_required")
    cached = _CALENDAR_ID_CACHE.get(key)
    if cached:
        return cached
    existing = find_calendar_by_summary(service, name)
    if existing and str(existing.get("id") or "").strip():
        cid = str(existing["id"]).strip()
        _CALENDAR_ID_CACHE[key] = cid
        return cid
    created = create_calendar(service, summary=name, timezone=timezone)
    cid = str(created.get("id") or "").strip()
    if not cid:
        raise RuntimeError(f"calendar_create_failed:{name}")
    _CALENDAR_ID_CACHE[key] = cid
    return cid


def get_webuntis_calendar_id(
    service,
    *,
    timezone: str = "Europe/Berlin",
    configured_calendar_id: str | None = None,
    configured_calendar_name: str = "WebUntis",
) -> str:
    fixed_id = str(configured_calendar_id or "").strip()
    if fixed_id and fixed_id.lower() != "primary":
        return fixed_id
    return get_or_create_calendar(service, name=configured_calendar_name, timezone=timezone)


def get_school_calendar_id_for_write(configured_calendar_id: str | None) -> str:
    fixed_id = str(configured_calendar_id or "").strip()
    if not fixed_id or fixed_id.lower() == "primary":
        raise GoogleCalendarConfigError("GOOGLE_CALENDAR_SCHOOL_ID fehlt. SchoolSync schreibt nicht in primary.")
    return fixed_id


def list_events(
    service,
    *,
    calendar_id: str,
    time_min: str | None = None,
    time_max: str | None = None,
    q: str | None = None,
    private_extended_property: list[str] | None = None,
    max_results: int = 250,
) -> list[dict[str, Any]]:
    page_token = None
    out: list[dict[str, Any]] = []
    while True:
        req = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_results,
            "pageToken": page_token,
        }
        if time_min:
            req["timeMin"] = time_min
        if time_max:
            req["timeMax"] = time_max
        if q:
            req["q"] = q
        if private_extended_property:
            req["privateExtendedProperty"] = private_extended_property
        resp = _execute_with_backoff(service.events().list(**req), label="google.events_list")
        out.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def list_events_across_calendars(
    service,
    *,
    time_min: str,
    time_max: str,
    q: str | None = None,
    include_hidden: bool = False,
    calendar_id: str | None = None,
    exclude_calendar_ids: list[str] | None = None,
) -> dict[str, Any]:
    excluded = {str(item).strip() for item in (exclude_calendar_ids or []) if str(item).strip()}
    if calendar_id not in (None, ""):
        calendars = [item for item in list_calendars(service) if str(item.get("id") or "").strip() == str(calendar_id).strip()]
    else:
        calendars = list_readable_calendars(service, include_hidden=include_hidden, include_free_busy_reader=False)
    calendars = [item for item in calendars if str(item.get("id") or "").strip() not in excluded]
    merged: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    checked: list[dict[str, Any]] = []
    for cal in calendars:
        cal_id = str(cal.get("id") or "").strip()
        cal_summary = str(cal.get("summary") or cal_id).strip() or cal_id
        checked.append(
            {
                "calendar_id": cal_id,
                "calendar_summary": cal_summary,
                "access_role": cal.get("accessRole"),
                "primary": bool(cal.get("primary")),
            }
        )
        try:
            events = list_events(service, calendar_id=cal_id, time_min=time_min, time_max=time_max, q=q)
        except Exception as exc:
            warnings.append({"calendar_id": cal_id, "calendar_summary": cal_summary, "error": str(exc)})
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            dedupe_key = (cal_id, event_id)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            start_obj = event.get("start") if isinstance(event.get("start"), dict) else {}
            end_obj = event.get("end") if isinstance(event.get("end"), dict) else {}
            start = start_obj.get("dateTime") or start_obj.get("date")
            end = end_obj.get("dateTime") or end_obj.get("date")
            description = str(event.get("description") or "").strip() or None
            if description and len(description) > 500:
                description = description[:497] + "..."
            merged.append(
                {
                    "id": event_id,
                    "calendar_id": cal_id,
                    "calendar_summary": cal_summary,
                    "calendar_color": cal.get("backgroundColor") or cal.get("foregroundColor"),
                    "source": "google_calendar",
                    "summary": str(event.get("summary") or "Termin").strip() or "Termin",
                    "title": str(event.get("summary") or "Termin").strip() or "Termin",
                    "start": start,
                    "end": end,
                    "all_day": bool(start_obj.get("date") and not start_obj.get("dateTime")),
                    "is_all_day": bool(start_obj.get("date") and not start_obj.get("dateTime")),
                    "location": str(event.get("location") or "").strip() or None,
                    "description": description,
                    "status": str(event.get("status") or "confirmed").strip() or "confirmed",
                    "htmlLink": event.get("htmlLink"),
                }
            )
    merged.sort(key=lambda item: (str(item.get("start") or ""), str(item.get("end") or ""), str(item.get("calendar_summary") or ""), str(item.get("summary") or "")))
    return {
        "calendar_scope": "all_readable_calendars" if calendar_id in (None, "") else "explicit_calendar_id",
        "calendars_checked": checked,
        "events": merged,
        "warnings": warnings,
    }


def insert_event(service, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return _execute_with_backoff(service.events().insert(calendarId=calendar_id, body=event), label="google.event_insert")


def update_event(service, *, calendar_id: str, event_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return _execute_with_backoff(
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event),
        label="google.event_update",
    )


def patch_event(service, *, calendar_id: str, event_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return _execute_with_backoff(
        service.events().patch(calendarId=calendar_id, eventId=event_id, body=patch),
        label="google.event_patch",
    )


def delete_event(service, *, calendar_id: str, event_id: str) -> None:
    _execute_with_backoff(service.events().delete(calendarId=calendar_id, eventId=event_id), label="google.event_delete")


def make_datetime(date_iso: str, time_hhmm: str, timezone: str) -> dict[str, str]:
    tz = ZoneInfo(timezone)
    ts = datetime.fromisoformat(f"{date_iso}T{time_hhmm}:00").replace(tzinfo=tz).isoformat()
    return {"dateTime": ts, "timeZone": timezone}


def compact_event(e: dict[str, Any]) -> str:
    start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
    end = e.get("end", {}).get("dateTime") or e.get("end", {}).get("date")
    return json.dumps(
        {
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": start,
            "end": end,
        },
        ensure_ascii=False,
    )
