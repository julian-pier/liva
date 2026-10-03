import sqlite3
from pathlib import Path

from flask import Flask

import core.core_api as core_api
import core.core_graph as core_graph
import database.connections as db_conn


def _set_core_db(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core_graph_test.sqlite3")
    core_graph._GRAPH_CACHE.clear()


def _seed_basic_graph(conn: sqlite3.Connection):
    conn.execute(
        "INSERT INTO core_node_index(id,type,title,subtitle,ts,weight,json) VALUES(?,?,?,?,?,?,?)",
        ("day:2026-02-24", "day", "Tag", "", "2026-02-24T00:00:00", 1.0, "{}"),
    )
    conn.execute(
        "INSERT INTO core_node_index(id,type,title,subtitle,ts,weight,json) VALUES(?,?,?,?,?,?,?)",
        ("gym_session:1", "gym_session", "Session 1", "", "2026-02-24T10:00:00", 1.0, "{}"),
    )
    conn.execute(
        "INSERT INTO core_node_index(id,type,title,subtitle,ts,weight,json) VALUES(?,?,?,?,?,?,?)",
        ("exercise:1", "exercise", "Bench", "", "2026-02-24T10:00:00", 1.0, "{}"),
    )
    conn.execute(
        "INSERT INTO core_node_index(id,type,title,subtitle,ts,weight,json) VALUES(?,?,?,?,?,?,?)",
        ("insight:1", "insight", "Insight", "", "2026-02-24T11:00:00", 1.0, "{}"),
    )
    conn.execute(
        "INSERT INTO core_edge_index(source,target,type,weight,ts) VALUES(?,?,?,?,?)",
        ("day:2026-02-24", "gym_session:1", "has_gym", 1.0, "2026-02-24T10:00:00"),
    )
    conn.execute(
        "INSERT INTO core_edge_index(source,target,type,weight,ts) VALUES(?,?,?,?,?)",
        ("gym_session:1", "exercise:1", "contains_exercise", 1.0, "2026-02-24T10:10:00"),
    )
    conn.execute(
        "INSERT INTO core_edge_index(source,target,type,weight,ts) VALUES(?,?,?,?,?)",
        ("insight:1", "day:2026-02-24", "supported_by", 1.0, "2026-02-24T11:00:00"),
    )
    conn.execute(
        "INSERT INTO core_index_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("index_built_ts", "2026-02-24T12:00:00Z"),
    )
    conn.commit()


def test_make_node_id_deterministic():
    assert core_graph.make_node_id("day", "2026-02-24") == "day:2026-02-24"
    assert core_graph.make_node_id("run", 123) == "run:123"


def test_depth_expansion_and_limits(tmp_path):
    _set_core_db(tmp_path)
    core_graph.ensure_core_graph_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    _seed_basic_graph(conn)
    conn.close()

    d1 = core_graph.get_core_graph_bundle("day:2026-02-24", depth=1, max_nodes=10, max_edges=10)
    d2 = core_graph.get_core_graph_bundle("day:2026-02-24", depth=2, max_nodes=10, max_edges=10)

    ids1 = {n["id"] for n in d1["nodes"]}
    ids2 = {n["id"] for n in d2["nodes"]}

    assert "day:2026-02-24" in ids1
    assert "gym_session:1" in ids1
    assert "exercise:1" not in ids1
    assert "exercise:1" in ids2

    limited = core_graph.get_core_graph_bundle("day:2026-02-24", depth=2, max_nodes=2, max_edges=1)
    assert len(limited["nodes"]) <= 2
    assert len(limited["edges"]) <= 1


def test_graph_endpoint_returns_panels(tmp_path, monkeypatch):
    _set_core_db(tmp_path)
    core_graph.ensure_core_graph_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    _seed_basic_graph(conn)
    conn.close()

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(core_api.core_bp)

    monkeypatch.setattr(core_api, "is_core_access_allowed", lambda req, role=None: True)
    core_api._RL_BUCKETS.clear()
    core_api._CACHE.clear()

    client = app.test_client()
    resp = client.get("/api/core/graph?focus=day:2026-02-24&depth=2&max_nodes=10")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    assert isinstance(payload["nodes"], list)
    assert isinstance(payload["edges"], list)
    assert set(core_graph.PANEL_KEYS).issubset(set(payload["panels"].keys()))
