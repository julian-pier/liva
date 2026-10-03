from __future__ import annotations


def test_read_model_refresh_does_not_spawn_thread_under_pytest(monkeypatch):
    from app_support import site_read_models

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test-marker")
    monkeypatch.setattr(
        site_read_models.threading,
        "Thread",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("thread spawned")),
    )

    started = site_read_models._start_refresh_thread(
        "test-snapshot",
        builder=lambda: {"ok": True},
        ttl_seconds=30,
    )

    assert started is False


def test_snapshot_warmup_does_not_spawn_thread_under_pytest(monkeypatch):
    import app

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test-marker")
    monkeypatch.setattr(
        app.threading,
        "Thread",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("thread spawned")),
    )

    app._schedule_snapshot_warmup()
