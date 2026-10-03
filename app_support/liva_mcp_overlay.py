"""Read-only bridge from the MCP-owned Phase 4.1 overlay into LIVA views."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("/var/lib/liva/.local/share/liva-mcp-remote/liva_mcp_writes.sqlite3")


def overlay_path() -> Path:
    return Path(os.environ.get("LIVA_MCP_OVERLAY_DB", str(DEFAULT_PATH))).expanduser()


def _connect(path: Path | None = None) -> sqlite3.Connection | None:
    target = path or overlay_path()
    if not target.is_file():
        return None
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn: sqlite3.Connection, query: str, values: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, values).fetchall()]
    except sqlite3.DatabaseError:
        return []


def day_context(day_iso: str, *, path: Path | None = None) -> dict[str, Any]:
    """Return only explicit MCP context; never infer values or alter app records."""
    conn = _connect(path)
    empty = {"source": "mcp_overlay", "daily_notes": [], "nutrition_context": [], "daily_flags": [], "training_rawlogs": [], "weight_entries": []}
    if conn is None:
        return empty
    try:
        notes = _rows(conn, "SELECT id,date_iso,category,text,created_at FROM daily_notes WHERE date_iso=? ORDER BY created_at ASC,id ASC", (day_iso,))
        flags = _rows(conn, "SELECT id,date_iso,flag_type,severity,body_part,text,created_at FROM daily_flags WHERE date_iso=? ORDER BY created_at ASC,id ASC", (day_iso,))
        nutrition = _rows(conn, "SELECT id,date_iso,text,precision_level,values_json,confidence,created_at FROM nutrition_context_logs WHERE date_iso=? ORDER BY created_at ASC,id ASC", (day_iso,))
        rawlogs = _rows(conn, "SELECT id,date_iso,session_name,raw_text,parsed_candidates_json,created_at FROM training_rawlogs WHERE date_iso=? ORDER BY created_at ASC,id ASC", (day_iso,))
    finally:
        conn.close()
    for row in nutrition:
        try:
            row["values"] = json.loads(row.pop("values_json"))
        except (TypeError, json.JSONDecodeError):
            row["values"] = {}
        row["source"] = "mcp_overlay"
    for group in (notes, flags, rawlogs):
        for row in group:
            row["source"] = "mcp_overlay"
    weights = weight_entries(start_iso=day_iso, end_iso=day_iso, path=path)
    return {"source": "mcp_overlay", "daily_notes": notes, "nutrition_context": nutrition, "daily_flags": flags, "training_rawlogs": rawlogs, "weight_entries": weights}


def weight_entries(*, start_iso: str | None = None, end_iso: str | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(path)
    if conn is None:
        return []
    clauses: list[str] = []
    values: list[Any] = []
    if start_iso:
        clauses.append("date_iso>=?")
        values.append(start_iso)
    if end_iso:
        clauses.append("date_iso<=?")
        values.append(end_iso)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        rows = _rows(conn, f"SELECT id,date_iso,weight_kg,note,updated_at FROM weight_entries{where} ORDER BY date_iso ASC,id ASC", tuple(values))
    finally:
        conn.close()
    for row in rows:
        row["source"] = "mcp_overlay"
    return rows


def merge_weight_series(rows: list[dict[str, Any]], *, start_iso: str | None = None, end_iso: str | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Overlay weight wins for a day while app nutrition data stays untouched."""
    merged = {str(row.get("date_iso")): dict(row) for row in rows if row.get("date_iso")}
    for overlay in weight_entries(start_iso=start_iso, end_iso=end_iso, path=path):
        date_iso = overlay["date_iso"]
        base = merged.get(date_iso, {"date": date_iso, "date_iso": date_iso, "calories": None, "protein_g": None, "carbs_g": None, "fat_g": None, "sugar_g": None, "source": None, "note": None})
        base["bodyweight_kg"] = float(overlay["weight_kg"])
        base["weight_source"] = "mcp_overlay"
        base["weight_note"] = overlay.get("note")
        merged[date_iso] = base
    return [merged[key] for key in sorted(merged)]
