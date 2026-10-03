from __future__ import annotations

import logging
import signal
import threading
import time

from .browser_context import BrowserContextStore
from .browser_server import start_browser_server
from .config import AgentConfig
from .segmenter import SegmentAggregator
from .spool import ActivitySpool
from .uploader import BatchUploader
from .visible_tracker import VisibleMonitorTracker
from .win32_capture import Win32Capture

LOG = logging.getLogger(__name__)


def run(config: AgentConfig) -> None:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    spool = ActivitySpool(config.spool_path)
    contexts = BrowserContextStore()
    bridge = start_browser_server(contexts, config.local_token, config.local_port)
    capture = Win32Capture()
    aggregator = SegmentAggregator(
        spool.upsert, contexts,
        idle_threshold_seconds=config.idle_threshold_seconds,
        checkpoint_seconds=config.checkpoint_seconds,
        browser_context_max_age_seconds=config.browser_context_max_age_seconds,
        sleep_gap_seconds=config.sleep_gap_seconds,
    )
    uploader = BatchUploader(config, spool)
    visible_tracker = VisibleMonitorTracker(
        spool.upsert, contexts,
        checkpoint_seconds=config.checkpoint_seconds,
        idle_threshold_seconds=config.idle_threshold_seconds,
        browser_context_max_age_seconds=config.browser_context_max_age_seconds,
        sleep_gap_seconds=config.sleep_gap_seconds,
    )
    last_upload = float("-inf")
    LOG.info("LIVA Activity läuft; Browser-Brücke auf 127.0.0.1:%s", config.local_port)
    try:
        while not stop.is_set():
            loop_started = time.monotonic()
            try:
                snapshot = capture.capture()
                aggregator.observe(snapshot)
                visible_tracker.observe(snapshot)
            except Exception:
                LOG.exception("Foreground-Erfassung fehlgeschlagen")
            interval = (
                config.fast_upload_interval_seconds
                if contexts.fast_sync_requested()
                else config.upload_interval_seconds
            )
            if loop_started - last_upload >= interval:
                uploader.upload_once()
                last_upload = loop_started
            stop.wait(max(0.05, config.poll_seconds - (time.monotonic() - loop_started)))
    finally:
        aggregator.close()
        visible_tracker.close()
        uploader.upload_once()
        bridge.shutdown()
        bridge.server_close()
