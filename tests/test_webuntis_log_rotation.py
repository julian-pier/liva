from __future__ import annotations

import gzip

from jobs import webuntis_update_watch as watch


def test_append_log_rotates_and_compresses_bounded_backups(tmp_path, monkeypatch):
    log_file = tmp_path / "webuntis_update_watch.log"
    monkeypatch.setattr(watch, "LOG_FILE", log_file)
    monkeypatch.setenv("WEBUNTIS_LOG_MAX_BYTES", "1024")

    log_file.write_text("old-entry\n" * 200, encoding="utf-8")
    watch._append_log("new-entry")

    assert log_file.read_text(encoding="utf-8") == "new-entry\n"
    first = tmp_path / "webuntis_update_watch.log.1.gz"
    assert first.exists()
    with gzip.open(first, "rt", encoding="utf-8") as handle:
        assert "old-entry" in handle.read()

    for generation in range(2, 6):
        log_file.write_text(f"generation-{generation}\n" * 100, encoding="utf-8")
        watch._append_log(f"after-{generation}")

    assert (tmp_path / "webuntis_update_watch.log.3.gz").exists()
    assert not (tmp_path / "webuntis_update_watch.log.4.gz").exists()
