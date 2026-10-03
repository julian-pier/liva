from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clients.liva_activity_windows.browser_context import BrowserContextStore
from clients.liva_activity_windows.browser_server import start_browser_server
from clients.liva_activity_windows.config import AgentConfig
from clients.liva_activity_windows.models import BrowserContext, ForegroundSnapshot
from clients.liva_activity_windows.models import VisibleWindowSnapshot
from clients.liva_activity_windows.segmenter import SegmentAggregator
from clients.liva_activity_windows.spool import ActivitySpool
from clients.liva_activity_windows.uploader import BatchUploader
from clients.liva_activity_windows.visible_tracker import VisibleMonitorTracker

UTC = timezone.utc
BASE = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def snap(seconds: int, *, exe="code.exe", title="Editor", idle=0, locked=False, visible_windows=()):
    return ForegroundSnapshot(
        BASE + timedelta(seconds=seconds), 42, exe, exe, title, idle, locked,
        visible_windows,
    )


def test_identical_state_keeps_uuid_and_checkpoints():
    persisted = []
    aggregator = SegmentAggregator(persisted.append, BrowserContextStore(), checkpoint_seconds=10)
    first = aggregator.observe(snap(0))
    second = aggregator.observe(snap(5))
    third = aggregator.observe(snap(11))
    assert first.id == second.id == third.id
    assert len(persisted) == 2
    assert persisted[-1].ended_at == BASE + timedelta(seconds=11)


def test_window_state_change_closes_and_starts_segment():
    persisted = []
    aggregator = SegmentAggregator(persisted.append, BrowserContextStore())
    first = aggregator.observe(snap(0))
    second = aggregator.observe(snap(2, exe="other.exe", title="Other"))
    assert first.id != second.id
    assert first.ended_at == second.started_at == BASE + timedelta(seconds=2)


def test_idle_transition_removes_threshold_from_active_usage():
    persisted = []
    aggregator = SegmentAggregator(
        persisted.append, BrowserContextStore(), idle_threshold_seconds=180, sleep_gap_seconds=1000
    )
    active = aggregator.observe(snap(0))
    optimistic = aggregator.observe(snap(179, idle=179))
    assert optimistic.id == active.id
    assert optimistic.ended_at == BASE + timedelta(seconds=179)

    idle = aggregator.observe(snap(180, idle=180))
    assert active.ended_at == BASE
    assert idle.is_idle is True
    assert idle.started_at == BASE
    resumed = aggregator.observe(snap(200, idle=0))
    assert idle.ended_at == resumed.started_at == BASE + timedelta(seconds=200)


def test_audible_browser_media_remains_active_without_recent_input():
    store = BrowserContextStore()
    store.update(BrowserContext("opera", True, "youtube.com", "YouTube", BASE + timedelta(seconds=181), 1, True))
    aggregator = SegmentAggregator(lambda _: None, store, idle_threshold_seconds=180)
    media = aggregator.observe(snap(181, exe="opera.exe", title="YouTube", idle=181))
    assert media.is_idle is False
    assert media.domain == "youtube.com"


def test_stale_audible_signal_does_not_disable_idle_detection():
    store = BrowserContextStore()
    store.update(BrowserContext("opera", True, "youtube.com", "YouTube", BASE, 1, True))
    segment = SegmentAggregator(lambda _: None, store, idle_threshold_seconds=180).observe(
        snap(181, exe="opera.exe", title="YouTube", idle=181)
    )
    assert segment.is_idle is True


def test_media_becomes_idle_when_audible_signal_stops():
    persisted = []
    store = BrowserContextStore()
    store.update(BrowserContext("opera", True, "youtube.com", "YouTube", BASE + timedelta(seconds=181), 1, True))
    aggregator = SegmentAggregator(persisted.append, store, idle_threshold_seconds=180)
    media = aggregator.observe(snap(181, exe="opera.exe", title="YouTube", idle=181))
    store.update(BrowserContext("opera", True, "youtube.com", "YouTube", BASE + timedelta(seconds=182), 1, False))
    idle = aggregator.observe(snap(182, exe="opera.exe", title="YouTube", idle=182))
    assert media.ended_at == BASE + timedelta(seconds=182)
    assert idle.is_idle is True
    assert idle.started_at == BASE + timedelta(seconds=182)


def test_open_game_without_input_becomes_idle_like_any_other_app():
    game = SegmentAggregator(lambda _: None, BrowserContextStore(), idle_threshold_seconds=180).observe(
        snap(181, exe="FortniteClient-Win64-Shipping.exe", title="Fortnite", idle=181)
    )
    assert game.is_idle is True


def test_audible_media_on_visible_second_monitor_keeps_pc_active():
    store = BrowserContextStore()
    store.update(BrowserContext("opera", False, "youtube.com", "YouTube", BASE + timedelta(seconds=181), 12, True))
    visible = (VisibleWindowSnapshot(2, "opera.exe", "Opera", "YouTube – Opera", "monitor-2", 0.8),)
    segment = SegmentAggregator(lambda _: None, store, idle_threshold_seconds=180).observe(
        snap(181, exe="chatgpt.exe", title="ChatGPT", idle=181, visible_windows=visible)
    )
    assert segment.is_idle is False


def test_browser_context_only_merges_into_matching_foreground_browser():
    store = BrowserContextStore()
    store.update(BrowserContext("chrome", True, "example.com", "Example", BASE))
    aggregator = SegmentAggregator(lambda _: None, store)
    chrome = aggregator.observe(snap(0, exe="chrome.exe", title="Example - Chrome"))
    other = aggregator.observe(snap(1, exe="code.exe", title="Editor"))
    assert (chrome.browser, chrome.domain) == ("chrome", "example.com")
    assert other.browser is None and other.domain is None


def test_dashboard_presence_enables_fast_sync_mode():
    store = BrowserContextStore()
    assert store.fast_sync_requested() is False
    store.set_dashboard_open(True)
    assert store.fast_sync_requested() is True
    store.set_dashboard_open(False)
    assert store.fast_sync_requested() is False


def test_opera_context_merges_but_is_not_an_app_category():
    store = BrowserContextStore()
    store.update(BrowserContext("opera", True, "youtube.com", "YouTube", BASE))
    aggregator = SegmentAggregator(lambda _: None, store)
    segment = aggregator.observe(snap(0, exe="opera.exe", title="YouTube"))
    assert segment.browser == "opera"
    assert segment.domain == "youtube.com"


def test_visible_monitor_tracker_creates_parallel_monitor_segments():
    persisted = []
    store = BrowserContextStore()
    store.update(BrowserContext("opera", False, "youtube.com", "YouTube", BASE, 12))
    tracker = VisibleMonitorTracker(persisted.append, store)
    snapshot = ForegroundSnapshot(
        BASE, 1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", 0, False,
        (
            VisibleWindowSnapshot(1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", "monitor-1", 0.8),
            VisibleWindowSnapshot(2, "opera.exe", "opera.exe", "YouTube – Opera", "monitor-2", 0.8),
        ),
    )
    tracker.observe(snapshot)
    assert {(item.monitor_id, item.track_kind, item.domain) for item in persisted} == {
        ("monitor-1", "visible", None),
        ("monitor-2", "visible", "youtube.com"),
    }


def test_visible_monitor_tracker_removes_idle_lookback():
    persisted = []
    tracker = VisibleMonitorTracker(
        persisted.append,
        BrowserContextStore(),
        idle_threshold_seconds=180,
        sleep_gap_seconds=1000,
    )
    visible = (VisibleWindowSnapshot(1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", "monitor-1", 0.8),)
    tracker.observe(ForegroundSnapshot(BASE, 1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", 0, False, visible))
    tracker.observe(ForegroundSnapshot(BASE + timedelta(seconds=179), 1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", 179, False, visible))
    assert tracker.trackers["monitor-1"].current.ended_at == BASE + timedelta(seconds=179)

    tracker.observe(ForegroundSnapshot(BASE + timedelta(seconds=180), 1, "chatgpt.exe", "ChatGPT.exe", "ChatGPT", 180, False, visible))
    assert persisted[-1].ended_at == BASE


def test_visible_monitor_tracker_keeps_only_audible_media_during_idle():
    persisted = []
    store = BrowserContextStore()
    store.update(BrowserContext("opera", False, "youtube.com", "YouTube", BASE, 12, True))
    tracker = VisibleMonitorTracker(persisted.append, store, idle_threshold_seconds=180)
    windows = (
        VisibleWindowSnapshot(1, "chatgpt.exe", "ChatGPT", "ChatGPT", "monitor-1", 0.8),
        VisibleWindowSnapshot(2, "opera.exe", "Opera", "YouTube – Opera", "monitor-2", 0.8),
    )
    tracker.observe(ForegroundSnapshot(BASE, 1, "chatgpt.exe", "ChatGPT", "ChatGPT", 181, False, windows))
    assert set(tracker.trackers) == {"monitor-2"}
    assert tracker.trackers["monitor-2"].current.is_idle is False


def test_unfocused_or_stale_browser_context_is_ignored():
    store = BrowserContextStore()
    store.update(BrowserContext("edge", False, "background.example", "Hidden", BASE))
    aggregator = SegmentAggregator(lambda _: None, store)
    segment = aggregator.observe(snap(0, exe="msedge.exe", title="Edge"))
    assert segment.domain is None


def test_unchanged_browser_context_survives_long_event_free_session():
    store = BrowserContextStore()
    store.update(BrowserContext("chrome", True, "example.com", "Long read", BASE))
    aggregator = SegmentAggregator(
        lambda _: None, store, browser_context_max_age_seconds=7 * 86400, sleep_gap_seconds=8 * 86400
    )
    first = aggregator.observe(snap(0, exe="chrome.exe", title="Long read"))
    later = aggregator.observe(snap(6 * 3600, exe="chrome.exe", title="Long read"))
    assert later.id == first.id
    assert later.domain == "example.com"


def test_no_foreground_window_is_not_counted_as_active_usage():
    aggregator = SegmentAggregator(lambda _: None, BrowserContextStore())
    snapshot = ForegroundSnapshot(BASE, None, None, None, None, 0, False)
    assert aggregator.observe(snapshot).is_idle is True


def test_sleep_gap_does_not_create_phantom_duration():
    persisted = []
    aggregator = SegmentAggregator(persisted.append, BrowserContextStore(), sleep_gap_seconds=15)
    old = aggregator.observe(snap(0))
    new = aggregator.observe(snap(3600))
    assert old.ended_at == BASE
    assert new.started_at == BASE + timedelta(hours=1)


def test_spool_upsert_and_retry_queue(tmp_path):
    spool = ActivitySpool(tmp_path / "spool.sqlite3")
    persisted = SegmentAggregator(spool.upsert, BrowserContextStore(), checkpoint_seconds=1)
    segment = persisted.observe(snap(0))
    persisted.observe(snap(2))
    assert spool.pending_count() == 1
    assert spool.pending(10)[0]["id"] == segment.id
    spool.mark_synced([segment.id])
    assert spool.pending_count() == 0
    persisted.observe(snap(4))
    assert spool.pending_count() == 1


def test_spool_does_not_ack_newer_checkpoint_revision(tmp_path):
    spool = ActivitySpool(tmp_path / "spool.sqlite3")
    aggregator = SegmentAggregator(spool.upsert, BrowserContextStore(), checkpoint_seconds=1)
    segment = aggregator.observe(snap(0))
    pending_event, pending_revision = spool.pending_with_revisions(1)[0]
    aggregator.observe(snap(2))
    spool.mark_synced_revisions([(pending_event["id"], pending_revision)])
    assert spool.pending_count() == 1
    assert spool.pending(1)[0]["ended_at"].endswith("08:00:02Z")


def test_uploader_marks_only_confirmed_batch_synced(tmp_path, monkeypatch):
    spool = ActivitySpool(tmp_path / "spool.sqlite3")
    segment = SegmentAggregator(spool.upsert, BrowserContextStore()).observe(snap(0))
    config = AgentConfig("https://liva.test", "secret", "x" * 24, str(uuid.uuid4()), "PC", tmp_path / "spool.sqlite3")
    uploader = BatchUploader(config, spool)

    def fail(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr("clients.liva_activity_windows.uploader.urlopen", fail)
    assert uploader.upload_once() is False
    assert spool.pending_count() == 1

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return json.dumps({"ok": True}).encode()

    monkeypatch.setattr("clients.liva_activity_windows.uploader.urlopen", lambda *_args, **_kwargs: Response())
    assert uploader.upload_once() is True
    assert spool.pending_count() == 0


def test_browser_bridge_binds_loopback_and_requires_pairing_token():
    store = BrowserContextStore()
    server = start_browser_server(store, "p" * 24, 0)
    try:
        host, port = server.server_address
        assert host == "127.0.0.1"
        body = json.dumps({
            "browser": "chrome", "focused": True, "domain": "example.com",
            "page_title": "Example", "timestamp": BASE.isoformat(), "dashboard_open": True,
            "audible": True,
        }).encode()
        unauthorized = urllib.request.Request(
            f"http://127.0.0.1:{port}/browser-context", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(unauthorized, timeout=2)
            assert False, "request without token must fail"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        authorized = urllib.request.Request(
            f"http://127.0.0.1:{port}/browser-context", data=body,
            headers={"Content-Type": "application/json", "X-LIVA-Activity-Token": "p" * 24}, method="POST",
        )
        with urllib.request.urlopen(authorized, timeout=2) as response:
            assert response.status == 200
        assert store.for_foreground("chrome.exe", BASE, 10).domain == "example.com"
        assert store.for_foreground("chrome.exe", BASE, 10).media_active is True
        assert store.fast_sync_requested() is True
    finally:
        server.shutdown()
        server.server_close()
