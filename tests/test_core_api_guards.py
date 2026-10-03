from flask import Flask

import core.core_api as core_api


def _build_app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(core_api.core_bp)

    monkeypatch.setattr(core_api, "is_core_access_allowed", lambda req, role=None: True)
    core_api._RL_BUCKETS.clear()
    core_api._CACHE.clear()
    return app


def test_state_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "1")
    app = _build_app(monkeypatch)
    monkeypatch.setattr(core_api, "get_core_state", lambda window: {"ok": True, "window": window, "nodes": [], "edges": []})

    client = app.test_client()
    r1 = client.get("/api/core/state?window=live")
    r2 = client.get("/api/core/state?window=live")

    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.headers.get("X-Core-RateLimit") == "BLOCK"


def test_intel_cache_hit_within_ttl(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "0")
    app = _build_app(monkeypatch)

    calls = {"n": 0}

    def fake_payload(mode="today", day_iso=None, force=False):
        calls["n"] += 1
        return {"ok": True, "mode": mode, "day": day_iso, "cards": [], "resolved": [], "brief": {}, "focus_target": None}

    monkeypatch.setattr(core_api, "get_core_intel_payload", fake_payload)

    client = app.test_client()
    r1 = client.get("/api/core/intel?mode=today")
    r2 = client.get("/api/core/intel?mode=today")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls["n"] == 1
    assert r1.headers.get("X-Core-Cache") == "MISS"
    assert r2.headers.get("X-Core-Cache") == "HIT"


def test_summary_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "1")
    app = _build_app(monkeypatch)
    monkeypatch.setattr(core_api, "get_core_summary", lambda mode: {"ok": True, "mode": mode, "modules": [], "events": {}, "sources": {}})

    client = app.test_client()
    r1 = client.get("/api/core/summary?mode=live")
    r2 = client.get("/api/core/summary?mode=live")

    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.headers.get("X-Core-RateLimit") == "BLOCK"


def test_summary_cache_hit_within_ttl(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "0")
    app = _build_app(monkeypatch)

    calls = {"n": 0}

    def fake_summary(mode):
        calls["n"] += 1
        return {"ok": True, "mode": mode, "modules": [], "events": {"active": [], "resolved": []}, "sources": {}}

    monkeypatch.setattr(core_api, "get_core_summary", fake_summary)

    client = app.test_client()
    r1 = client.get("/api/core/summary?mode=today")
    r2 = client.get("/api/core/summary?mode=today")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls["n"] == 1
    assert r1.headers.get("X-Core-Cache") == "MISS"
    assert r2.headers.get("X-Core-Cache") == "HIT"


def test_snapshot_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "1")
    app = _build_app(monkeypatch)
    monkeypatch.setattr(core_api, "get_core_observatory_snapshot", lambda mode="live": {"ok": True, "mode": mode, "nodes": [], "edges": []})

    client = app.test_client()
    r1 = client.get("/api/core/snapshot?mode=live")
    r2 = client.get("/api/core/snapshot?mode=live")
    r3 = client.get("/api/core/snapshot?mode=live")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.headers.get("X-Core-RateLimit") == "BLOCK"


def test_snapshot_cache_hit_within_ttl(monkeypatch):
    monkeypatch.setenv("CORE_RATE_LIMITS", "0")
    app = _build_app(monkeypatch)

    calls = {"n": 0}

    def fake_snapshot(mode="live"):
        calls["n"] += 1
        return {
            "ok": True,
            "mode": mode,
            "t": 1.0,
            "nodes": [],
            "edges": [],
            "events": [],
            "logs": [],
            "system": {},
            "processes": [],
            "stats": {},
            "histogram_60s": [],
        }

    monkeypatch.setattr(core_api, "get_core_observatory_snapshot", fake_snapshot)

    client = app.test_client()
    r1 = client.get("/api/core/snapshot?mode=today")
    r2 = client.get("/api/core/snapshot?mode=today")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls["n"] == 1
    assert r1.headers.get("X-Core-Cache") == "MISS"
    assert r2.headers.get("X-Core-Cache") == "HIT"
