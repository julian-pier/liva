from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _reload_watchdog(tmp_path):
    import scripts.liva_watchdog as watchdog

    importlib.reload(watchdog)
    watchdog.WATCHDOG_DB = tmp_path / "watchdog.sqlite3"
    watchdog.WATCHDOG_DIR = tmp_path
    return watchdog


def _event(watchdog, ip, line, *, source=None, now=None):
    return watchdog.parse_access_event_from_line(line, source=source or watchdog.JOURNAL_SOURCE, now=now)


def _classify(watchdog, ip, lines, *, now):
    events = [_event(watchdog, ip, line, now=now) for line in lines]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)[ip]
    return activity, watchdog.classify_security_event(activity)


def test_recovery_stale_alert(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        watchdog,
        "fetch_recovery_rows_from_endpoint",
        lambda **kwargs: ([{"time": (now - timedelta(hours=40)).isoformat(), "rmssd": 40, "rri": 1000, "sleepScore": 80}], None),
    )
    result = watchdog.evaluate_recovery(now=now)
    assert result.status == "alert"
    assert "veraltet" in result.message


def test_recovery_stale_alert_is_sent_only_once_for_same_night(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    stale_dt = datetime(2026, 5, 21, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        watchdog,
        "fetch_recovery_rows_from_endpoint",
        lambda **kwargs: ([{"time": stale_dt.isoformat(), "rmssd": 40, "rri": 1000, "sleepScore": 80}], None),
    )

    first = watchdog.evaluate_recovery(now=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc))
    second = watchdog.evaluate_recovery(now=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc))

    assert watchdog.should_send_alert(conn, first) is True
    assert watchdog.should_send_alert(conn, second) is False
    conn.close()


def test_send_alert_uses_canonical_notification_adapter(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    from control_center import notifications
    calls = []
    monkeypatch.setattr(notifications, "status", lambda: {"active_provider": "telegram"})
    monkeypatch.setattr(notifications, "deliver", lambda **kwargs: calls.append(kwargs) or {"delivery": {"result": "sent"}})

    watchdog.send_alert("watchdog:recovery", "test message")

    assert calls == [{"provider": "telegram", "dedupe_key": "watchdog:recovery", "text": "test message"}]


def test_recovery_fresh_ok(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        watchdog,
        "fetch_recovery_rows_from_endpoint",
        lambda **kwargs: ([{"time": (now - timedelta(hours=12)).isoformat(), "rmssd": 40, "rri": 1000, "sleepScore": 80}], None),
    )
    result = watchdog.evaluate_recovery(now=now)
    assert result.status == "ok"


def test_incomplete_recovery_without_sleep_score_is_not_complete(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    assert watchdog.is_complete_recovery_row({"rmssd": 45, "rri": 1020, "sleepScore": None}) is False


def test_backup_no_locations_skips_without_alert(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    monkeypatch.setattr(watchdog, "BACKUP_SEARCH_PATHS", (tmp_path / "missing-a", tmp_path / "missing-b"))
    monkeypatch.setattr(watchdog, "get_failed_backup_units", lambda: [])
    result = watchdog.evaluate_backup(now=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc))
    assert result.status == "skip"
    assert "no backup evidence found" in result.message


def test_backup_old_file_alert(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_file = backup_dir / "daily.sqlite3"
    backup_file.write_bytes(b"x")
    old_ts = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(backup_file, (old_ts, old_ts))
    monkeypatch.setattr(watchdog, "BACKUP_SEARCH_PATHS", (backup_dir,))
    monkeypatch.setattr(watchdog, "get_failed_backup_units", lambda: [])
    result = watchdog.evaluate_backup(now=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc))
    assert result.status == "alert"
    assert "Backup veraltet" in result.message


def test_backup_failed_unit_is_ignored_when_metadata_is_healthy(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_file = backup_dir / "daily.sqlite3"
    backup_file.write_bytes(b"x")
    fresh_ts = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(backup_file, (fresh_ts, fresh_ts))
    monkeypatch.setattr(watchdog, "BACKUP_SEARCH_PATHS", (backup_dir,))
    monkeypatch.setattr(watchdog, "get_failed_backup_units", lambda: ["liva-daily-backup.service"])
    monkeypatch.setattr(
        watchdog,
        "load_backup_metadata",
        lambda: {"payload": {"sqlite_backup": {"ok": True, "remote_sync": {"ok": True, "status": "ok"}}}},
    )
    result = watchdog.evaluate_backup(now=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc))
    assert result.status == "ok"


def test_system_thresholds_report_correct_status(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    monkeypatch.setattr(watchdog, "get_mount_usage", lambda path: (88.0, path) if path == "/" else None)
    monkeypatch.setattr(watchdog, "get_ram_used_pct", lambda: 91.5)
    monkeypatch.setattr(watchdog, "get_load15", lambda: 1.0)
    result = watchdog.evaluate_system()
    assert result.status == "alert"
    assert "RAM hoch" in result.message


def test_normal_root_gets_are_ignored(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(watchdog, "203.0.113.10", '203.0.113.10 - - [24/May/2026:11:40:00 +0000] "GET / HTTP/1.1" 200', now=now),
        _event(watchdog, "203.0.113.10", '203.0.113.10 - - [24/May/2026:11:41:00 +0000] "GET /favicon.ico HTTP/1.1" 200', now=now),
    ]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["203.0.113.10"]
    classification = watchdog.classify_security_event(activity)
    assert classification["level"] == "ignore"


def test_new_ip_alone_does_not_alert(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        watchdog,
        "scan_access_events",
        lambda **kwargs: [
            _event(watchdog, "203.0.113.10", '203.0.113.10 - - [24/May/2026:11:40:00 +0000] "GET / HTTP/1.1" 200', now=now)
        ],
    )
    first = watchdog.evaluate_access(conn, now=now)
    second = watchdog.evaluate_access(conn, now=now + timedelta(minutes=1))
    assert first.status == "ok"
    assert second.status == "ok"
    assert "keine sicherheitsrelevanten" in second.message.lower()
    conn.close()


def test_dotenv_path_is_high(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    activity, classification = _classify(watchdog, "198.51.100.7", ['198.51.100.7 - - [24/May/2026:11:40:00 +0000] "GET /.env HTTP/1.1" 404'], now=now)
    assert classification["level"] == "high"
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    watchdog.learn_access_ips(conn, [("198.51.100.7", watchdog.JOURNAL_SOURCE)], now=now - timedelta(minutes=5))
    assert watchdog.should_send_security_alert(conn, "198.51.100.7", classification, now=now) is False
    conn.close()


def test_legit_backup_status_get_does_not_trigger_high(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    activity, classification = _classify(watchdog, "100.64.0.42", ['100.64.0.42 - - [24/May/2026:11:40:00 +0000] "GET /api/settings/backup_status HTTP/1.1" 200'], now=now)
    assert classification["level"] in {"info", "low"}
    assert classification["level"] != "high"
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    watchdog.learn_access_ips(conn, [("100.64.0.42", watchdog.JOURNAL_SOURCE)], now=now - timedelta(minutes=5))
    assert watchdog.should_send_security_alert(conn, "100.64.0.42", classification, now=now) is False
    conn.close()


def test_many_legit_read_gets_stay_below_high(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = [
        '100.64.0.42 - - [24/May/2026:11:40:00 +0000] "GET /api/settings/backup_status HTTP/1.1" 200',
        '100.64.0.42 - - [24/May/2026:11:41:00 +0000] "GET /api/settings/profile HTTP/1.1" 200',
        '100.64.0.42 - - [24/May/2026:11:42:00 +0000] "GET /api/hrv/recovery HTTP/1.1" 200',
        '100.64.0.42 - - [24/May/2026:11:43:00 +0000] "GET /api/polar/status HTTP/1.1" 200',
    ]
    events = [_event(watchdog, "100.64.0.42", line, now=now) for line in lines]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["100.64.0.42"]
    classification = watchdog.classify_security_event(activity)
    assert classification["level"] in {"low", "info", "medium"}
    assert classification["level"] != "high"


def test_post_to_legit_read_endpoint_is_not_benign(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    event = _event(watchdog, "100.64.0.42", '100.64.0.42 - - [24/May/2026:11:40:00 +0000] "POST /api/settings/backup_status HTTP/1.1" 302', now=now)
    activity = watchdog.aggregate_ip_activities([event], now=now)["100.64.0.42"]
    classification = watchdog.classify_security_event(activity)
    assert classification["level"] in {"low", "medium", "high", "critical"}
    assert classification["level"] != "info"


def test_wp_admin_and_phpmyadmin_with_404s_is_high(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = [
        '198.51.100.8 - - [24/May/2026:11:40:00 +0000] "GET /wp-admin HTTP/1.1" 404',
        '198.51.100.8 - - [24/May/2026:11:41:00 +0000] "GET /phpmyadmin HTTP/1.1" 404',
        '198.51.100.8 - - [24/May/2026:11:42:00 +0000] "GET /missing.js HTTP/1.1" 404',
    ]
    events = [_event(watchdog, "198.51.100.8", line, now=now) for line in lines]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["198.51.100.8"]
    classification = watchdog.classify_security_event(activity)
    assert classification["level"] == "high"


def test_unknown_paths_with_login_redirects_can_be_medium(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = [
        "May 24 11:40:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-1 client_ip=198.51.100.9 status=302",
        "May 24 11:41:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-2 client_ip=198.51.100.9 status=302",
        "May 24 11:42:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-3 client_ip=198.51.100.9 status=302",
        "May 24 11:43:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-4 client_ip=198.51.100.9 status=302",
        "May 24 11:44:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-5 client_ip=198.51.100.9 status=302",
        "May 24 11:45:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fscan-6 client_ip=198.51.100.9 status=302",
    ]
    events = [_event(watchdog, "198.51.100.9", line, now=now) for line in lines]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["198.51.100.9"]
    classification = watchdog.classify_security_event(activity)
    assert classification["level"] in {"medium", "high"}


def test_post_to_sensitive_write_api_with_401_is_critical(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    line = "May 24 11:45:00 host [private_access] method=POST path=/api/v2/actions/run client_ip=198.51.100.20 status=401"
    activity, classification = _classify(watchdog, "198.51.100.20", [line], now=now)
    assert classification["level"] == "critical"
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    watchdog.learn_access_ips(conn, [("198.51.100.20", watchdog.JOURNAL_SOURCE)], now=now - timedelta(minutes=5))
    assert watchdog.should_send_security_alert(conn, "198.51.100.20", classification, now=now) is True
    conn.close()


def test_many_auth_failures_are_critical(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = [
        f"May 24 11:{40 + idx:02d}:00 host [private_access] method=GET path=/api/settings/secure client_ip=198.51.100.21 status=401"
        for idx in range(6)
    ]
    _, classification = _classify(watchdog, "198.51.100.21", lines, now=now)
    assert classification["level"] == "critical"


def test_successful_dotenv_response_is_critical(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    _, classification = _classify(watchdog, "198.51.100.22", ['198.51.100.22 - - [24/May/2026:11:40:00 +0000] "GET /.env HTTP/1.1" 200'], now=now)
    assert classification["level"] == "critical"


def test_dedupe_blocks_repeated_same_alert(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = ['198.51.100.30 - - [24/May/2026:11:40:00 +0000] "GET /.env HTTP/1.1" 404']
    watchdog.learn_access_ips(conn, [("198.51.100.30", watchdog.JOURNAL_SOURCE)], now=now - timedelta(hours=1))
    _, classification = _classify(watchdog, "198.51.100.30", lines, now=now)
    assert classification["level"] == "high"
    assert watchdog.should_send_security_alert(conn, "198.51.100.30", classification, now=now) is False
    conn.close()


def test_escalation_from_medium_to_high_bypasses_dedupe(tmp_path, monkeypatch):
    watchdog = _reload_watchdog(tmp_path)
    conn = watchdog.ensure_state_db(watchdog.WATCHDOG_DB)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    medium_lines = [
        f'198.51.100.40 - - [24/May/2026:11:{40 + idx:02d}:00 +0000] "GET /scan-{idx} HTTP/1.1" 200'
        for idx in range(8)
    ]
    high_lines = medium_lines + ['198.51.100.40 - - [24/May/2026:11:55:00 +0000] "GET /.env HTTP/1.1" 404']
    watchdog.learn_access_ips(conn, [("198.51.100.40", watchdog.JOURNAL_SOURCE)], now=now - timedelta(hours=1))
    _, first = _classify(watchdog, "198.51.100.40", medium_lines, now=now)
    _, second = _classify(watchdog, "198.51.100.40", high_lines, now=now)
    assert first["level"] == "medium"
    assert second["level"] == "high"
    assert watchdog.should_send_security_alert(conn, "198.51.100.40", first, now=now) is False
    assert watchdog.should_send_security_alert(conn, "198.51.100.40", second, now=now + timedelta(hours=1)) is False
    conn.close()


def test_internal_ips_stay_ignored(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    assert watchdog.extract_access_ip_from_journal_line("client_ip=127.0.0.1 client_ip_source=proxyfix") is None
    assert watchdog.extract_access_ip_from_journal_line("client_ip=172.19.0.2 Uptime-Kuma") is None


def test_tailscale_cgnat_ip_is_not_ignored(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    assert watchdog.is_ignored_client_ip("100.64.0.42") is False


def test_extract_access_event_supports_private_access_format(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    line = (
        "May 24 11:45:00 host [private_access] decision=deny path=/admin method=GET "
        "client_ip=198.51.100.50 x_forwarded_for=198.51.100.50 status=403 user_agent=curl/8.0"
    )
    event = watchdog.parse_access_event_from_line(line, now=now)
    assert event is not None
    assert event.ip == "198.51.100.50"
    assert event.method == "GET"
    assert event.path == "/admin"
    assert event.status_code == 403


def test_method_from_gunicorn_is_not_overwritten_by_private_access_line(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(watchdog, "100.64.0.42", '100.64.0.42 - - [24/May/2026:11:45:00 +0000] "GET /api/settings/backup_status HTTP/1.1" 200', now=now),
        _event(watchdog, "100.64.0.42", "May 24 11:45:01 host [private_access] decision=allow path=/api/settings/backup_status client_ip=100.64.0.42 status=200", now=now),
    ]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["100.64.0.42"]
    alert = watchdog.format_security_alert("100.64.0.42", activity, watchdog.classify_security_event(activity))
    assert "Methode: GET" in alert


def test_private_access_path_does_not_dominate_top_paths(tmp_path):
    watchdog = _reload_watchdog(tmp_path)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lines = [
        "May 24 11:40:00 host [private_access] decision=redirect_login path=/_private_access?next=%2F.env client_ip=198.51.100.70 status=302",
        "May 24 11:41:00 host [private_access] decision=redirect_login path=/_private_access?next=%2Fwp-admin client_ip=198.51.100.70 status=302",
        "May 24 11:42:00 host [private_access] decision=redirect_login path=/_private_access?next=%2F_private_access client_ip=198.51.100.70 status=302",
    ]
    events = [_event(watchdog, "198.51.100.70", line, now=now) for line in lines]
    activity = watchdog.aggregate_ip_activities([event for event in events if event], now=now)["198.51.100.70"]
    classification = watchdog.classify_security_event(activity)
    assert "/.env" in classification["top_paths"]
    assert "/wp-admin" in classification["top_paths"]
    assert "/_private_access" not in classification["top_paths"][:2]
