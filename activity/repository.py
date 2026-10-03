from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from database.connections import get_activity_db


BERLIN = ZoneInfo("Europe/Berlin")

# Browser executables are tracked as apps while domains remain metadata for the
# same segments. They are never added a second time to total active PC time.
BROWSER_APP_EXES = {
    "chrome.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "firefox.exe",
    "brave.exe", "vivaldi.exe", "browser.exe",
}

KNOWN_WEBSITES = {
    "chat.openai.com": "ChatGPT",
    "chatgpt.com": "ChatGPT",
    "twitch.tv": "Twitch",
    "www.twitch.tv": "Twitch",
    "youtube.com": "YouTube",
    "www.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "primevideo.com": "Prime Video",
    "amazon.de": "Amazon",
    "liva.example.com": "LIVA",
    "gmail.com": "Gmail",
    "mail.google.com": "Gmail",
    "calendar.google.com": "Google Kalender",
    "next.djk-coesfeld.de": "EintrachtNext",
}

WEBSITE_SUFFIXES = {
    "chatgpt.com": "ChatGPT",
    "openai.com": "OpenAI",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "twitch.tv": "Twitch",
    "primevideo.com": "Prime Video",
    "netflix.com": "Netflix",
    "disneyplus.com": "Disney+",
    "joyn.de": "Joyn",
    "rtlplus.de": "RTL+",
    "dazn.com": "DAZN",
    "spotify.com": "Spotify",
    "soundcloud.com": "SoundCloud",
    "music.apple.com": "Apple Music",
    "mail.google.com": "Gmail",
    "calendar.google.com": "Google Kalender",
    "drive.google.com": "Google Drive",
    "docs.google.com": "Google Docs",
    "sheets.google.com": "Google Sheets",
    "slides.google.com": "Google Präsentationen",
    "meet.google.com": "Google Meet",
    "maps.google.com": "Google Maps",
    "google.com": "Google",
    "outlook.live.com": "Outlook",
    "outlook.office.com": "Outlook",
    "teams.microsoft.com": "Microsoft Teams",
    "onedrive.live.com": "OneDrive",
    "office.com": "Microsoft 365",
    "github.com": "GitHub",
    "gitlab.com": "GitLab",
    "stackoverflow.com": "Stack Overflow",
    "reddit.com": "Reddit",
    "x.com": "X",
    "twitter.com": "X",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "facebook.com": "Facebook",
    "discord.com": "Discord",
    "web.whatsapp.com": "WhatsApp Web",
    "amazon.de": "Amazon",
    "amazon.com": "Amazon",
    "ebay.de": "eBay",
    "kleinanzeigen.de": "Kleinanzeigen",
    "wikipedia.org": "Wikipedia",
    "deepl.com": "DeepL",
    "canva.com": "Canva",
    "figma.com": "Figma",
    "notion.so": "Notion",
    "webuntis.com": "WebUntis",
    "djk-coesfeld.de": "EintrachtNext",
}

APP_LABELS = {
    "chrome.exe": "Chrome",
    "google chrome": "Chrome",
    "msedge.exe": "Edge",
    "microsoft edge": "Edge",
    "opera.exe": "Opera",
    "opera_gx.exe": "Opera GX",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "chatgpt.exe": "ChatGPT",
    "chatgpt": "ChatGPT",
    "windowsterminal.exe": "Terminal",
    "wt.exe": "Terminal",
    "cmd.exe": "Terminal",
    "powershell.exe": "PowerShell",
    "explorer.exe": "Explorer",
    "snippingtool.exe": "Snipping Tool",
    "searchhost.exe": "Windows Search",
    "whatsapp.root.exe": "WhatsApp",
    "whatsapp.exe": "WhatsApp",
    "code.exe": "Visual Studio Code",
    "devenv.exe": "Visual Studio",
    "steam.exe": "Steam",
    "epicgameslauncher.exe": "Epic Games",
    "eadesktop.exe": "EA App",
    "upc.exe": "Ubisoft Connect",
    "battle.net.exe": "Battle.net",
    "discord.exe": "Discord",
    "spotify.exe": "Spotify",
    "obs64.exe": "OBS Studio",
    "winword.exe": "Microsoft Word",
    "excel.exe": "Microsoft Excel",
    "powerpnt.exe": "Microsoft PowerPoint",
    "outlook.exe": "Microsoft Outlook",
    "onenote.exe": "Microsoft OneNote",
    "acrobat.exe": "Adobe Acrobat",
    "photoshop.exe": "Adobe Photoshop",
    "fortniteclient-win64-shipping.exe": "Fortnite",
    "gta5.exe": "Grand Theft Auto V",
    "gta5_enhanced.exe": "Grand Theft Auto V",
    "batmanak.exe": "Batman: Arkham Knight",
    "shootergame.exe": "ARK: Survival Evolved",
    "arkascended.exe": "ARK: Survival Ascended",
    "rocketleague.exe": "Rocket League",
    "cyberpunk2077.exe": "Cyberpunk 2077",
    "witcher3.exe": "The Witcher 3",
    "eldenring.exe": "Elden Ring",
    "rdr2.exe": "Red Dead Redemption 2",
    "valorant-win64-shipping.exe": "VALORANT",
    "cs2.exe": "Counter-Strike 2",
    "overwatch.exe": "Overwatch 2",
}

APP_EXE_PREFIX_LABELS = (
    ("fortniteclient-", "Fortnite"),
    ("gta5", "Grand Theft Auto V"),
    ("batmanak", "Batman: Arkham Knight"),
    ("shootergame", "ARK: Survival Evolved"),
    ("arkascended", "ARK: Survival Ascended"),
    ("minecraft", "Minecraft"),
    ("league of legends", "League of Legends"),
)


def app_label(app_name: str | None, app_exe: str | None) -> str:
    raw_name = str(app_name or "").strip()
    raw_exe = str(app_exe or "").strip()
    for candidate in (raw_exe, raw_name):
        key = candidate.lower()
        if key in APP_LABELS:
            return APP_LABELS[key]
        if key.endswith(".exe") and key[:-4] in APP_LABELS:
            return APP_LABELS[key[:-4]]
        normalized_exe = key[:-4] if key.endswith(".exe") else key
        for prefix, label in APP_EXE_PREFIX_LABELS:
            if normalized_exe.startswith(prefix):
                return label
    clean = raw_name or raw_exe or "Unbekannte App"
    if clean.lower().endswith(".exe"):
        clean = clean[:-4]
    return clean.replace("_", " ").strip() or "Unbekannte App"


def website_label(domain: str | None) -> str | None:
    clean = str(domain or "").strip().lower().rstrip(".")
    if not clean:
        return None
    if clean in KNOWN_WEBSITES:
        return KNOWN_WEBSITES[clean]
    for suffix, label in sorted(WEBSITE_SUFFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if clean == suffix or clean.endswith("." + suffix):
            return label
    if clean.startswith("www."):
        clean = clean[4:]
    parts = clean.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return clean.capitalize()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_activity_schema(conn: sqlite3.Connection | None = None) -> None:
    owns_connection = conn is None
    conn = conn or get_activity_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'windows',
                agent_version TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_events (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL CHECK(duration_seconds >= 0),
                app_exe TEXT,
                app_name TEXT,
                window_title TEXT,
                browser TEXT,
                domain TEXT,
                page_title TEXT,
                is_idle INTEGER NOT NULL DEFAULT 0 CHECK(is_idle IN (0, 1)),
                source TEXT NOT NULL DEFAULT 'windows_agent',
                track_kind TEXT NOT NULL DEFAULT 'foreground',
                monitor_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_activity_events_time
                ON activity_events(started_at, ended_at);
            CREATE INDEX IF NOT EXISTS idx_activity_events_device_time
                ON activity_events(device_id, started_at, ended_at);

            CREATE TABLE IF NOT EXISTS ingest_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                device_id TEXT,
                received_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                upserted_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)")}
        if "track_kind" not in columns:
            conn.execute("ALTER TABLE activity_events ADD COLUMN track_kind TEXT NOT NULL DEFAULT 'foreground'")
        if "monitor_id" not in columns:
            conn.execute("ALTER TABLE activity_events ADD COLUMN monitor_id TEXT")
        from .iphone_daily import ensure_iphone_daily_schema
        ensure_iphone_daily_schema(conn)
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _day_bounds(day_iso: str) -> tuple[datetime, datetime]:
    selected = date.fromisoformat(day_iso)
    start_local = datetime.combine(selected, time(hour=4), tzinfo=BERLIN)
    # A LIVA activity day follows the user's waking day and rolls over at
    # 04:00 local time. Zone-aware local construction keeps DST days correct.
    end_exclusive = datetime.combine(selected + timedelta(days=1), time(hour=4), tzinfo=BERLIN)
    return start_local.astimezone(timezone.utc), end_exclusive.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone missing")
    return parsed.astimezone(timezone.utc)


def upsert_batch(device: dict[str, Any], events: list[dict[str, Any]], batch_id: str) -> dict[str, int]:
    ensure_activity_schema()
    now = utc_now_iso()
    conn = get_activity_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_batch = conn.execute(
            "SELECT event_count, upserted_count FROM ingest_log WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing_batch:
            conn.rollback()
            return {"received": int(existing_batch["event_count"]), "upserted": int(existing_batch["upserted_count"])}

        conn.execute(
            """
            INSERT INTO devices(id, name, platform, agent_version, first_seen_at, last_seen_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, platform=excluded.platform, agent_version=excluded.agent_version,
                last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
            """,
            (
                device["id"], device["name"], device.get("platform") or "windows",
                device.get("agent_version"), now, now, now, now,
            ),
        )
        for event in events:
            conn.execute(
                """
                INSERT INTO activity_events(
                    id, device_id, started_at, ended_at, duration_seconds, app_exe, app_name,
                    window_title, browser, domain, page_title, is_idle, source, track_kind, monitor_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    device_id=excluded.device_id, started_at=excluded.started_at, ended_at=excluded.ended_at,
                    duration_seconds=excluded.duration_seconds, app_exe=excluded.app_exe,
                    app_name=excluded.app_name, window_title=excluded.window_title,
                    browser=excluded.browser, domain=excluded.domain, page_title=excluded.page_title,
                    is_idle=excluded.is_idle, source=excluded.source, track_kind=excluded.track_kind,
                    monitor_id=excluded.monitor_id, updated_at=excluded.updated_at
                """,
                (
                    event["id"], device["id"], event["started_at"], event["ended_at"],
                    event["duration_seconds"], event.get("app_exe"), event.get("app_name"),
                    event.get("window_title"), event.get("browser"), event.get("domain"),
                    event.get("page_title"), int(bool(event.get("is_idle"))),
                    event.get("source") or "windows_agent", event.get("track_kind") or "foreground",
                    event.get("monitor_id"), event.get("created_at") or now, now,
                ),
            )
        conn.execute(
            "INSERT INTO ingest_log(batch_id, device_id, received_at, event_count, upserted_count, status, detail) VALUES (?, ?, ?, ?, ?, 'ok', ?)",
            (batch_id, device["id"], now, len(events), len(events), json.dumps({"source": "activity_api"})),
        )
        conn.commit()
        return {"received": len(events), "upserted": len(events)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _events_for_day(day_iso: str) -> list[dict[str, Any]]:
    ensure_activity_schema()
    start_utc, end_utc = _day_bounds(day_iso)
    start_iso = start_utc.isoformat().replace("+00:00", "Z")
    end_iso = end_utc.isoformat().replace("+00:00", "Z")
    conn = get_activity_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM activity_events
            WHERE started_at < ? AND ended_at > ?
            ORDER BY started_at ASC, id ASC
            """,
            (end_iso, start_iso),
        ).fetchall()
    finally:
        conn.close()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        actual_start = max(_parse_utc(item["started_at"]), start_utc)
        actual_end = min(_parse_utc(item["ended_at"]), end_utc)
        item["duration_seconds"] = max(0.0, (actual_end - actual_start).total_seconds())
        item["started_at"] = actual_start.isoformat().replace("+00:00", "Z")
        item["ended_at"] = actual_end.isoformat().replace("+00:00", "Z")
        item["started_at_local"] = actual_start.astimezone(BERLIN).isoformat()
        item["ended_at_local"] = actual_end.astimezone(BERLIN).isoformat()
        item["is_idle"] = bool(item["is_idle"])
        output.append(item)
    return output


def _website_from_event(item: dict[str, Any]) -> str | None:
    if item.get("domain"):
        return website_label(item.get("domain")) or str(item["domain"])
    exe = str(item.get("app_exe") or "").lower()
    if exe not in BROWSER_APP_EXES:
        return None
    title = f"{item.get('page_title') or ''} {item.get('window_title') or ''}".casefold()
    title_labels = (
        ("chatgpt", "ChatGPT"), ("youtube", "YouTube"), ("twitch", "Twitch"),
        ("prime video", "Prime Video"), ("gmail", "Gmail"), ("liva", "LIVA"),
        ("eintrachtnext", "EintrachtNext"), ("djk-coesfeld", "EintrachtNext"),
    )
    for needle, label in title_labels:
        if needle in title:
            return label
    return "Browser"


def _is_noise(item: dict[str, Any]) -> bool:
    if item.get("is_idle"):
        return False
    duration = float(item.get("duration_seconds") or 0)
    exe = str(item.get("app_exe") or "").lower()
    title = str(item.get("window_title") or "").casefold()
    if exe == "explorer.exe" and (not title or title in {"program manager", "programmumschaltung"}):
        return True
    if duration < 5 and exe in {"explorer.exe", "searchhost.exe", "snippingtool.exe"}:
        return True
    if duration < 3 and ("programmumschaltung" in title or not title):
        return True
    return False


def _sessionize_lane(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in events:
        item = dict(item)
        item["app_display_name"] = app_label(item.get("app_name"), item.get("app_exe"))
        if item["is_idle"]:
            group_key = ("idle",)
            item["display_name"] = "Idle"
        elif _website_from_event(item):
            item["display_name"] = _website_from_event(item)
            group_key = ("website", item["display_name"].lower())
        else:
            item["display_name"] = app_label(item.get("app_name"), item.get("app_exe"))
            group_key = ("app", item["display_name"].lower())
        item["_group_key"] = group_key
        if _is_noise(item):
            continue
        prepared.append(item)

    grouped: list[dict[str, Any]] = []
    for item in prepared:
        gap = 0.0
        if grouped:
            gap = (_parse_utc(item["started_at"]) - _parse_utc(grouped[-1]["ended_at"])).total_seconds()
        if grouped and grouped[-1].get("_group_key") == item["_group_key"] and gap <= 15:
            previous = grouped[-1]
            previous["ended_at"] = item["ended_at"]
            previous["ended_at_local"] = item["ended_at_local"]
            previous["duration_seconds"] = round(float(previous["duration_seconds"]) + float(item["duration_seconds"]), 3)
            previous["page_title"] = item.get("page_title") or previous.get("page_title")
            previous["window_title"] = item.get("window_title") or previous.get("window_title")
            previous["display_name"] = item.get("display_name") or previous.get("display_name")
            previous["event_count"] = int(previous.get("event_count", 1)) + int(item.get("event_count", 1))
            continue
        item["event_count"] = 1
        grouped.append(item)

    meaningful: list[dict[str, Any]] = []
    for index, item in enumerate(grouped):
        if float(item["duration_seconds"]) < 5 and index != len(grouped) - 1:
            continue
        item.pop("_group_key", None)
        meaningful.append(item)
    return meaningful


def timeline_for_day(day_iso: str) -> list[dict[str, Any]]:
    raw = _events_for_day(day_iso)
    lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in raw:
        track_kind = str(event.get("track_kind") or "foreground")
        monitor_id = str(event.get("monitor_id") or "")
        lanes.setdefault((track_kind, monitor_id), []).append(event)
    output: list[dict[str, Any]] = []
    for lane_events in lanes.values():
        output.extend(_sessionize_lane(lane_events))
    return sorted(output, key=lambda item: (item["started_at"], item.get("track_kind") or "foreground", item.get("monitor_id") or ""))


def _active_foreground_intervals(events: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    intervals = sorted(
        (_parse_utc(event["started_at"]), _parse_utc(event["ended_at"]))
        for event in events
        if str(event.get("track_kind") or "foreground") != "visible" and not event.get("is_idle")
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def _overlap_with_intervals(event: dict[str, Any], intervals: list[tuple[datetime, datetime]]) -> float:
    start = _parse_utc(event["started_at"])
    end = _parse_utc(event["ended_at"])
    return sum(max(0.0, (min(end, right) - max(start, left)).total_seconds()) for left, right in intervals)


def _parallel_visible_seconds(events: list[dict[str, Any]], active_intervals: list[tuple[datetime, datetime]]) -> float:
    """Return wall-clock seconds with at least two visible monitor lanes.

    The result is deliberately not added to active PC time. It is a detail
    metric that describes parallel visibility inside already active time.
    """
    edges: list[tuple[datetime, int]] = []
    for event in events:
        if str(event.get("track_kind") or "foreground") != "visible" or _is_noise(event):
            continue
        start = _parse_utc(event["started_at"])
        end = _parse_utc(event["ended_at"])
        for left, right in active_intervals:
            clipped_start, clipped_end = max(start, left), min(end, right)
            if clipped_end > clipped_start:
                edges.extend(((clipped_start, 1), (clipped_end, -1)))
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    total = 0.0
    visible = 0
    previous: datetime | None = None
    for moment, delta in edges:
        if previous is not None and visible >= 2:
            total += max(0.0, (moment - previous).total_seconds())
        visible += delta
        previous = moment
    return total


def summary_for_day(day_iso: str) -> dict[str, Any]:
    events = _events_for_day(day_iso)
    active_intervals = _active_foreground_intervals(events)
    foreground_events = [event for event in events if str(event.get("track_kind") or "foreground") != "visible"]
    latest_foreground = max(foreground_events, key=lambda event: _parse_utc(event["ended_at"]), default=None)
    active = 0.0
    idle = 0.0
    apps: dict[str, float] = {}
    domains: dict[str, float] = {}
    websites: dict[str, float] = {}
    visible_apps: dict[str, float] = {}
    visible_websites: dict[str, float] = {}
    monitor_totals: dict[str, float] = {}
    for event in events:
        duration = float(event["duration_seconds"])
        track_kind = str(event.get("track_kind") or "foreground")
        if track_kind == "visible":
            # Visible monitor windows are metadata for active PC time. Clip
            # them to foreground-active intervals so the 180-second idle
            # lookback can never inflate apps or websites beyond PC usage.
            duration = _overlap_with_intervals(event, active_intervals)
            if duration <= 0:
                continue
            if _is_noise(event):
                continue
            monitor_id = str(event.get("monitor_id") or "Monitor")
            monitor_totals[monitor_id] = monitor_totals.get(monitor_id, 0.0) + duration
            app = app_label(event.get("app_name"), event.get("app_exe"))
            visible_apps[app] = visible_apps.get(app, 0.0) + duration
            label = _website_from_event(event)
            if label:
                visible_websites[label] = visible_websites.get(label, 0.0) + duration
            continue
        if event["is_idle"]:
            idle += duration
            continue
        active += duration
        if _is_noise(event):
            continue
        app = app_label(event.get("app_name"), event.get("app_exe"))
        apps[app] = apps.get(app, 0.0) + duration
        domain = event.get("domain")
        if domain:
            raw_domain = str(domain).lower().rstrip(".")
            domains[raw_domain] = domains.get(raw_domain, 0.0) + duration
            label = website_label(raw_domain) or raw_domain
            websites[label] = websites.get(label, 0.0) + duration
    sort_usage = lambda values: [
        {"name": name, "seconds": round(seconds, 3)}
        for name, seconds in sorted(values.items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    active_foreground = [event for event in foreground_events if not event.get("is_idle") and not _is_noise(event)]
    active_foreground.sort(key=lambda event: event["started_at"])
    switches = sum(
        1 for previous, current in zip(active_foreground, active_foreground[1:])
        if app_label(previous.get("app_name"), previous.get("app_exe"))
        != app_label(current.get("app_name"), current.get("app_exe"))
    )
    longest = max(active_foreground, key=lambda event: float(event.get("duration_seconds") or 0), default=None)
    first_active = min((event["started_at"] for event in active_foreground), default=None)
    last_active = max((event["ended_at"] for event in active_foreground), default=None)
    return {
        "date": day_iso,
        "timezone": "Europe/Berlin",
        "total_active_seconds": round(active, 3),
        "total_idle_seconds": round(idle, 3),
        "data_through": max((event["ended_at"] for event in events), default=None),
        "current_foreground_active": bool(latest_foreground and not latest_foreground.get("is_idle")),
        "current_foreground_ended_at": latest_foreground.get("ended_at") if latest_foreground else None,
        "usage_by_app": sort_usage(apps),
        "usage_by_domain": sort_usage(domains),
        "usage_by_website": sort_usage(websites),
        "usage_by_visible_app": sort_usage(visible_apps),
        "usage_by_visible_website": sort_usage(visible_websites),
        "visible_seconds_by_monitor": sort_usage(monitor_totals),
        "first_activity_at": first_active,
        "last_activity_at": last_active,
        "app_switches": switches,
        "longest_focus_block": ({
            "name": app_label(longest.get("app_name"), longest.get("app_exe")),
            "seconds": round(float(longest.get("duration_seconds") or 0), 3),
        } if longest else None),
        "idle_share": round(idle / (active + idle), 4) if active + idle else 0,
        "parallel_active_seconds": round(_parallel_visible_seconds(events, active_intervals), 3),
    }


def windows_period_summary(start_day: str, end_day: str) -> dict[str, Any]:
    """Aggregate existing trustworthy day summaries without mixing track semantics."""
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    if end < start:
        raise ValueError("end_date before start_date")
    count = (end - start).days + 1
    if count > 90:
        raise ValueError("maximum period is 90 days")

    def collect(first: date, days: int) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float]]:
        daily: list[dict[str, Any]] = []
        apps: dict[str, float] = {}
        websites: dict[str, float] = {}
        for offset in range(days):
            day = (first + timedelta(days=offset)).isoformat()
            summary = summary_for_day(day)
            app_rows = summary["usage_by_visible_app"] or summary["usage_by_app"]
            website_rows = summary["usage_by_visible_website"] or summary["usage_by_website"]
            daily.append({
                "date": day,
                "total_active_seconds": summary["total_active_seconds"],
                "total_idle_seconds": summary["total_idle_seconds"],
                "usage_by_app": app_rows,
                "usage_by_website": website_rows,
            })
            for row in app_rows:
                apps[row["name"]] = apps.get(row["name"], 0.0) + float(row["seconds"])
            for row in website_rows:
                websites[row["name"]] = websites.get(row["name"], 0.0) + float(row["seconds"])
        return daily, apps, websites

    daily, apps, websites = collect(start, count)
    previous, _, _ = collect(start - timedelta(days=count), count)
    heatmap: dict[tuple[int, int], float] = {}
    for offset in range(count):
        events = _events_for_day((start + timedelta(days=offset)).isoformat())
        for interval_start, interval_end in _active_foreground_intervals(events):
            cursor = interval_start
            while cursor < interval_end:
                local = cursor.astimezone(BERLIN)
                next_local_hour = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                boundary = min(interval_end, next_local_hour.astimezone(timezone.utc))
                if boundary <= cursor:
                    boundary = min(interval_end, cursor + timedelta(hours=1))
                key = (local.weekday(), local.hour)
                heatmap[key] = heatmap.get(key, 0.0) + (boundary - cursor).total_seconds()
                cursor = boundary
    sort_usage = lambda values: [
        {"name": name, "seconds": round(seconds, 3)}
        for name, seconds in sorted(values.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]
    return {
        "daily": daily,
        "average_active_seconds": round(sum(row["total_active_seconds"] for row in daily) / count, 3),
        "previous_average_active_seconds": round(sum(row["total_active_seconds"] for row in previous) / count, 3),
        "usage_by_app": sort_usage(apps),
        "usage_by_website": sort_usage(websites),
        "heatmap": [
            {"weekday": weekday, "hour": hour, "seconds": round(seconds, 3)}
            for (weekday, hour), seconds in sorted(heatmap.items())
        ],
    }
