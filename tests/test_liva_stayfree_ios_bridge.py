from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clients.liva_stayfree_ios_bridge.bridge import (
    backfill,
    build_payload,
    import_usage,
    last_completed_usage_day,
)
from clients.liva_stayfree_ios_bridge.cache import (
    DailyUsage,
    StayFreeCacheError,
    StayFreeDayMissing,
    _cache_key_bounds,
    locate_cache,
    open_readonly,
    read_available_days,
    read_day,
)
from clients.liva_stayfree_ios_bridge.config import BridgeConfig
from clients.liva_stayfree_ios_bridge.state import ImportState
from clients.liva_stayfree_ios_bridge.uploader import UploadError, upload_daily
from clients.liva_stayfree_ios_bridge.scheduled import RETRY_INTERVAL_SECONDS, run_catchup_attempt, run_scheduled


def make_cache(path: Path) -> None:
    def sessions(apps, started_at, *, platform="ios", imported=True):
        return [
            {
                "id": f"{name}{started_at}",
                "appId": name,
                "createdBy": "iphone-source",
                "startedAt": started_at,
                "endedAt": started_at + seconds * 1000,
                "imported": imported,
                "platform": platform,
            }
            for name, seconds in apps
        ]

    actual_reference_apps = [
        ("TikTok", 4200),
        ("Instagram", 3120),
        ("YouTube", 420),
        ("Vinted", 300),
        ("Kurzbefehle", 180),
        ("StayFree", 180),
        ("\u200eWhatsApp", 120),
        ("App Store", 60),
        ("Polar Flow", 60),
        ("Safari", 60),
        ("Spotify", 60),
        ("Einstellungen", 35),
        ("Wetter", 28),
        ("Wallet", 25),
        ("ChatGPT", 16),
        ("Fotos", 14),
        ("Google Kalender", 13),
        ("Benutzerauthentifizierung", 9),
        ("FamilyControlsAuthenticationUI", 6),
    ]
    cache = {
        "1786759200000—1786845600000—iphone-device": {
            "value": sessions([("Old", 100)], 1_786_800_000_000),
            "createdAt": 1_786_845_000_000,
        },
        "1786845600000—1786932000000—iphone-device": {
            "value": sessions(actual_reference_apps, 1_786_917_600_000),
            "createdAt": 1_786_959_234_084,
        },
        "1786845600000—1786932000000—windows-device": {
            "value": sessions([("Opera GX", 50_000)], 1_786_917_600_000, platform="windows", imported=False),
            "createdAt": 1_786_959_234_084,
        },
        "1786845600000—1786932000000—undefined": {"value": [], "createdAt": 1_786_959_234_084},
    }
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO config VALUES (?, ?)",
            [
                ("daily-reset-time", "14400000"),
                ("api-sessions-repo-find-sessions-cache", json.dumps(cache)),
            ],
        )


def config(tmp_path: Path, cache_path: Path) -> BridgeConfig:
    return BridgeConfig(
        server_url="https://liva.example",
        api_key="secret-key",
        device_id="77ebf338-f307-5c9f-b956-f70ad6a4518e",
        stayfree_db_path=cache_path,
        state_path=tmp_path / "state.sqlite3",
    )


def test_config_accepts_windows_powershell_utf8_bom(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "server_url": "https://liva.example",
            "api_key": "secret-key",
            "device_id": "77ebf338-f307-5c9f-b956-f70ad6a4518e",
        }),
        encoding="utf-8-sig",
    )
    loaded = BridgeConfig.load(config_path)
    assert loaded.server_url == "https://liva.example"
    assert loaded.api_key == "secret-key"


def test_cache_locator_and_reader_are_read_only_and_ios_only(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    before = hashlib.sha256(cache.read_bytes()).hexdigest()
    assert locate_cache(cache) == cache.resolve()
    days = read_available_days(cache)
    assert list(days) == ["2026-08-15", "2026-08-16"]
    assert days["2026-08-16"].total_usage_seconds == 8906
    assert [app["name"] for app in days["2026-08-16"].apps[:5]] == [
        "TikTok", "Instagram", "YouTube", "Vinted", "Kurzbefehle"
    ]
    assert all(app["name"] != "Opera GX" for usage in days.values() for app in usage.apps)
    assert hashlib.sha256(cache.read_bytes()).hexdigest() == before
    with open_readonly(cache) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO config VALUES ('forbidden', '{}')")


def test_real_synced_cache_schema_filters_windows_and_cleans_app_names(tmp_path):
    cache = tmp_path / "config.db"
    make_cache(cache)
    usage = read_day(cache, "2026-08-16")
    assert usage.total_usage_seconds == 8906
    assert usage.source_updated_at == "2026-08-17T09:33:54Z"
    assert any(app == {"app_key": "whatsapp", "name": "WhatsApp", "duration_seconds": 120} for app in usage.apps)
    assert all(app["name"] != "Opera GX" for app in usage.apps)


def test_cache_rejects_wrong_reset_and_ambiguous_ios_devices(tmp_path):
    cache = tmp_path / "config.db"
    make_cache(cache)
    with sqlite3.connect(cache) as conn:
        conn.execute("UPDATE config SET value='0' WHERE key='daily-reset-time'")
    with pytest.raises(StayFreeCacheError, match="04:00"):
        read_available_days(cache)

    cache.unlink()
    make_cache(cache)
    with sqlite3.connect(cache) as conn:
        raw = json.loads(conn.execute(
            "SELECT value FROM config WHERE key='api-sessions-repo-find-sessions-cache'"
        ).fetchone()[0])
        original = raw["1786845600000—1786932000000—iphone-device"]
        raw["1786845600000—1786932000000—second-iphone"] = original
        conn.execute(
            "UPDATE config SET value=? WHERE key='api-sessions-repo-find-sessions-cache'", (json.dumps(raw),)
        )
    with pytest.raises(StayFreeCacheError, match="Multiple iOS device"):
        read_available_days(cache)


def test_cache_deduplicates_session_ids_and_accepts_dst_day_bounds(tmp_path):
    cache = tmp_path / "config.db"
    make_cache(cache)
    with sqlite3.connect(cache) as conn:
        raw = json.loads(conn.execute(
            "SELECT value FROM config WHERE key='api-sessions-repo-find-sessions-cache'"
        ).fetchone()[0])
        sessions = raw["1786845600000—1786932000000—iphone-device"]["value"]
        sessions.append(dict(sessions[0]))
        conn.execute(
            "UPDATE config SET value=? WHERE key='api-sessions-repo-find-sessions-cache'", (json.dumps(raw),)
        )
    assert read_day(cache, "2026-08-16").total_usage_seconds == 8906

    # Construct bounds through explicit Berlin offsets to avoid depending on the test host timezone.
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    spring_start = round(datetime(2026, 3, 29, 4, 0, tzinfo=berlin).timestamp() * 1000)
    spring_end = round(datetime(2026, 3, 30, 4, 0, tzinfo=berlin).timestamp() * 1000)
    assert _cache_key_bounds(f"{spring_start}—{spring_end}—iphone") is not None


def test_missing_and_incomplete_days_never_become_zero(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    with pytest.raises(StayFreeDayMissing):
        read_day(cache, "2026-08-14")
    with sqlite3.connect(cache) as conn:
        raw = json.loads(conn.execute(
            "SELECT value FROM config WHERE key='api-sessions-repo-find-sessions-cache'"
        ).fetchone()[0])
        raw["1786932000000—1787018400000—iphone-device"] = {
            "value": [{"platform": "ios", "imported": True, "appId": "WhatsApp", "startedAt": 1786932000000}],
            "createdAt": 1787018400000,
        }
        conn.execute(
            "UPDATE config SET value=? WHERE key='api-sessions-repo-find-sessions-cache'", (json.dumps(raw),)
        )
    assert "2026-08-17" not in read_available_days(cache)


def test_payload_uses_exact_total_and_four_am_bounds(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    body = build_payload(config(tmp_path, cache), read_day(cache, "2026-08-16"))
    assert body["platform"] == "ios" and body["source"] == "stayfree_ios"
    assert body["total_usage_seconds"] == 8906
    assert body["day_start"] == "2026-08-16T04:00:00+02:00"
    assert body["day_end"] == "2026-08-17T04:00:00+02:00"


def test_backfill_dry_run_requires_known_reference_total(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = BridgeConfig(**{
        **config(tmp_path, cache).__dict__,
        "verification_day": "2026-08-16",
        "verification_total_seconds": 8906,
    })
    report = backfill(cfg, dry_run=True)
    assert report["verification"] == {
        "usage_day": "2026-08-16", "total_usage_seconds": 8906, "status": "passed"
    }
    wrong = cfg
    wrong = BridgeConfig(**{**wrong.__dict__, "verification_total_seconds": 8905})
    with pytest.raises(RuntimeError, match="verification failed"):
        backfill(wrong, dry_run=True)


def test_backfill_allows_a_pruned_historical_probe_day(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    report = backfill(cfg, dry_run=True)
    assert report["available_days"] == 2
    assert report["verification"]["status"] == "passed"


def test_duplicate_retry_and_successful_import_state(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    state = ImportState(cfg.state_path)
    calls = []

    def uploader(server, key, body, timeout):
        calls.append((server, key, body["usage_day"], timeout))
        return {"ok": True, "daily_usage_id": "daily-id"}

    usage = read_day(cache, "2026-08-16")
    assert import_usage(cfg, usage, state, uploader)["status"] == "imported"
    assert import_usage(cfg, usage, state, uploader)["status"] == "already_imported"
    assert len(calls) == 1
    with state.connect() as conn:
        row = conn.execute("SELECT status, attempts, last_success_at FROM import_state").fetchone()
    assert row["status"] == "imported" and row["attempts"] == 1 and row["last_success_at"]


def test_offline_upload_is_retried_on_next_run(tmp_path):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    state = ImportState(cfg.state_path)
    usage = read_day(cache, "2026-08-16")
    with pytest.raises(ConnectionError):
        import_usage(cfg, usage, state, lambda *_: (_ for _ in ()).throw(ConnectionError("offline")))
    assert import_usage(cfg, usage, state, lambda *_: {"ok": True})["status"] == "imported"
    with state.connect() as conn:
        row = conn.execute("SELECT status, attempts, last_error FROM import_state").fetchone()
    assert row["status"] == "imported" and row["attempts"] == 2 and row["last_error"] is None


def test_hidden_scheduler_retries_until_stayfree_day_arrives(tmp_path, monkeypatch):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.scheduled.last_completed_usage_day", lambda: "2026-08-16")
    results = iter([
        {"usage_day": "2026-08-16", "status": "waiting_for_stayfree"},
        {"usage_day": "2026-08-16", "status": "waiting_for_stayfree"},
        {"usage_day": "2026-08-16", "status": "imported"},
    ])
    targets = []
    sleeps = []

    def run_once(_config, target):
        targets.append(target)
        return next(results)

    assert run_scheduled(cfg, run_once=run_once, sleeper=sleeps.append, attempts=5) == 0
    assert targets == ["2026-08-16"] * 3
    assert sleeps == [RETRY_INTERVAL_SECONDS, RETRY_INTERVAL_SECONDS]


def test_hidden_scheduler_stops_immediately_for_imported_hash(tmp_path, monkeypatch):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.scheduled.last_completed_usage_day", lambda: "2026-08-16")
    sleeps = []
    assert run_scheduled(
        cfg,
        run_once=lambda *_: {"usage_day": "2026-08-16", "status": "already_imported"},
        sleeper=sleeps.append,
    ) == 0
    assert sleeps == []


def test_scheduled_attempt_backfills_days_missed_while_windows_was_off(tmp_path, monkeypatch):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    cfg = config(tmp_path, cache)
    calls = []

    def fake_backfill(scheduled_config):
        calls.append(("backfill", scheduled_config.verification_day, scheduled_config.verification_total_seconds))
        return {"status": "complete"}

    def fake_daily(scheduled_config, target):
        calls.append(("daily", target, scheduled_config.verification_day))
        return {"usage_day": target, "status": "imported"}

    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.scheduled.backfill", fake_backfill)
    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.scheduled.run_daily", fake_daily)
    assert run_catchup_attempt(cfg, "2026-08-16")["status"] == "imported"
    assert calls == [("backfill", None, None), ("daily", "2026-08-16", None)]


def test_uploader_uses_certifi_tls_context_and_preserves_network_detail(monkeypatch):
    captured = {}

    def fake_context(*, cafile):
        captured["cafile"] = cafile
        return object()

    def fail(_request, *, timeout, context):
        captured.update(timeout=timeout, context=context)
        raise urllib.error.URLError("certificate verify failed")

    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.uploader.ssl.create_default_context", fake_context)
    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.uploader.certifi.where", lambda: "trusted-ca.pem")
    monkeypatch.setattr("clients.liva_stayfree_ios_bridge.uploader.urllib.request.urlopen", fail)

    with pytest.raises(UploadError, match="certificate verify failed"):
        upload_daily("https://liva.example", "secret", {"ok": True}, 12)
    assert captured["cafile"] == "trusted-ca.pem"
    assert captured["timeout"] == 12
    assert captured["context"] is not None


def test_backfill_reports_retry_detail(tmp_path, monkeypatch):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    monkeypatch.setattr(
        "clients.liva_stayfree_ios_bridge.bridge.import_usage",
        lambda *_: (_ for _ in ()).throw(UploadError("LIVA unavailable: TLS failed")),
    )
    report = backfill(config(tmp_path, cache))
    assert report["status"] == "partial"
    assert report["imports"][0]["detail"] == "LIVA unavailable: TLS failed"


def test_cache_database_errors_are_reported_as_retryable(tmp_path, monkeypatch):
    cache = tmp_path / "stayfree.sqlite3"
    make_cache(cache)
    monkeypatch.setattr(
        "clients.liva_stayfree_ios_bridge.cache.open_readonly",
        lambda _path: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(StayFreeCacheError, match="locked"):
        read_available_days(cache)


def test_last_completed_day_respects_four_am_boundary():
    assert last_completed_usage_day(datetime(2026, 8, 17, 1, 59, tzinfo=timezone.utc)) == "2026-08-15"
    assert last_completed_usage_day(datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)) == "2026-08-16"
