from __future__ import annotations

import subprocess

from integrations import ntfy_client


def test_send_ntfy_notification_uses_configured_wrapper(monkeypatch, tmp_path):
    notify_bin = tmp_path / "liva-notify"
    notify_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    notify_bin.chmod(0o755)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("LIVA_NOTIFY_BIN", str(notify_bin))
    monkeypatch.setattr(ntfy_client.subprocess, "run", fake_run)

    result = ntfy_client.send_ntfy_notification(
        "PR 🚀 Squats",
        title="LIVA · Training PR",
        priority=4,
        tags="trophy,muscle",
    )

    assert result == {"ok": True, "sent": True, "channel": "ntfy"}
    assert calls[0][0] == [str(notify_bin), "PR 🚀 Squats"]
    assert calls[0][1]["env"]["LIVA_NOTIFY_TITLE"] == "LIVA · Training PR"
    assert calls[0][1]["env"]["LIVA_NOTIFY_PRIORITY"] == "4"
    assert calls[0][1]["env"]["LIVA_NOTIFY_TAGS"] == "trophy,muscle"


def test_send_ntfy_notification_does_not_report_failed_wrapper_as_sent(monkeypatch, tmp_path):
    notify_bin = tmp_path / "liva-notify"
    notify_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    notify_bin.chmod(0o755)
    monkeypatch.setenv("LIVA_NOTIFY_BIN", str(notify_bin))
    monkeypatch.setattr(
        ntfy_client.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 22, "", "failed"),
    )

    result = ntfy_client.send_ntfy_notification("PR")

    assert result["ok"] is False
    assert result["sent"] is False
    assert result["channel"] == "ntfy"
    assert result["error"] == "notify_failed"
