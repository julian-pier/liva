from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WriteService:
    """Small, explicit overlay store for controlled MCP writes.

    It deliberately does not open any of the application SQLite files for writing.
    """
    def __init__(self, path: Path, nutrition_write_path: Path | None = None):
        self.path = Path(path)
        self.nutrition_write_path = Path(nutrition_write_path) if nutrition_write_path else None

    def _connect(self, *, busy_timeout: bool = False) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if busy_timeout:
            conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_notes (id INTEGER PRIMARY KEY, date_iso TEXT NOT NULL, category TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(date_iso,category,text));
        CREATE TABLE IF NOT EXISTS weight_entries (id INTEGER PRIMARY KEY, date_iso TEXT NOT NULL UNIQUE, weight_kg REAL NOT NULL, note TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS nutrition_context_logs (id INTEGER PRIMARY KEY, date_iso TEXT NOT NULL, text TEXT NOT NULL, precision_level TEXT NOT NULL, values_json TEXT NOT NULL, confidence TEXT, created_at TEXT NOT NULL, UNIQUE(date_iso,text,precision_level,values_json));
        CREATE TABLE IF NOT EXISTS memory_entries (id INTEGER PRIMARY KEY, text TEXT NOT NULL, text_key TEXT NOT NULL UNIQUE, tags_json TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', supersedes_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_flags (id INTEGER PRIMARY KEY, date_iso TEXT NOT NULL, flag_type TEXT NOT NULL, severity TEXT, body_part TEXT, text TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS training_rawlogs (id INTEGER PRIMARY KEY, date_iso TEXT NOT NULL, session_name TEXT, raw_text TEXT NOT NULL, parsed_candidates_json TEXT, created_at TEXT NOT NULL, UNIQUE(date_iso,raw_text));
        CREATE TABLE IF NOT EXISTS idempotency_records (idempotency_key TEXT PRIMARY KEY, operation_type TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS write_audit (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, tool_name TEXT NOT NULL, operation_type TEXT NOT NULL, dry_run INTEGER NOT NULL, executed INTEGER NOT NULL, reason TEXT NOT NULL, affected_domain TEXT NOT NULL, affected_ids_json TEXT NOT NULL, before_summary_json TEXT NOT NULL, after_summary_json TEXT NOT NULL, oauth_client_hash TEXT, source TEXT NOT NULL, idempotency_key TEXT, request_id TEXT);
        CREATE TRIGGER IF NOT EXISTS write_audit_append_only_delete BEFORE DELETE ON write_audit BEGIN SELECT RAISE(ABORT, 'write_audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS write_audit_append_only_update BEFORE UPDATE ON write_audit BEGIN SELECT RAISE(ABORT, 'write_audit is append-only'); END;
        """)
        return conn

    def initialize(self) -> None:
        """Create the MCP-owned overlay and its small schema before the first write."""
        with self._connect():
            pass

    def _existing_idempotency(self, conn: sqlite3.Connection, key: str | None, operation: str, expected_context: Any = None, legacy_match: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
        if not key:
            return None
        row = conn.execute("SELECT operation_type,response_json FROM idempotency_records WHERE idempotency_key=?", (key,)).fetchone()
        if not row:
            return None
        if row["operation_type"] != operation:
            raise ValueError("idempotency_key was already used for a different operation")
        result = json.loads(row["response_json"])
        stored_context = result.pop("_idempotency_context", None)
        if expected_context is not None:
            expected_digest = digest(expected_context)
            if stored_context is not None and stored_context != expected_digest:
                raise ValueError("idempotency_key_context_conflict")
            if stored_context is None and legacy_match is not None:
                if not legacy_match(result.get("readback") or {}):
                    raise ValueError("idempotency_key_context_conflict")
            elif stored_context is None:
                readback = result.get("readback") or {}
                if readback.get("date_iso") != expected_context.get("date") or float(readback.get("weight_kg", readback.get("weight", -1))) != expected_context.get("weight_kg"):
                    raise ValueError("idempotency_key_context_conflict")
        result["idempotent_replay"] = True
        return result

    def _finish(self, conn: sqlite3.Connection, *, tool: str, operation: str, reason: str, domain: str, ids: list[Any], before: Any, after: Any, key: str | None, client_id: str | None, request_id: str | None, idempotency_context: Any = None, audit_before: Any = None, audit_after: Any = None, idempotency_serializer: Callable[[dict[str, Any]], dict[str, Any]] | None = None, result_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        if not after:
            raise RuntimeError("readback_failed")
        result = {"status": "success", "executed": True, "operation_id": f"{operation}:{ids[0] if ids else 'none'}", "operation_type": operation, "affected_domain": domain, "affected_ids": ids, "before": before, "readback": after, "warnings": [], "error_code": None}
        if result_fields:
            result.update(result_fields)
        client_hash = hashlib.sha256(client_id.encode()).hexdigest()[:16] if client_id else None
        audit = conn.execute("INSERT INTO write_audit (timestamp,tool_name,operation_type,dry_run,executed,reason,affected_domain,affected_ids_json,before_summary_json,after_summary_json,oauth_client_hash,source,idempotency_key,request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (utc_now(), tool, operation, 0, 1, reason, domain, json.dumps(ids), json.dumps(audit_before if audit_before is not None else before, ensure_ascii=False), json.dumps(audit_after if audit_after is not None else after, ensure_ascii=False), client_hash, "chatgpt", key, request_id))
        result["audit_id"] = audit.lastrowid
        if key:
            stored = dict(result)
            if idempotency_serializer:
                stored = idempotency_serializer(stored)
            if idempotency_context is not None:
                stored["_idempotency_context"] = digest(idempotency_context)
            conn.execute("INSERT INTO idempotency_records VALUES (?,?,?,?)", (key, operation, json.dumps(stored, ensure_ascii=False), utc_now()))
        conn.commit()
        return result

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def _production_nutrition_connect(self) -> sqlite3.Connection:
        if self.nutrition_write_path is None or not self.nutrition_write_path.is_file():
            raise RuntimeError("production_nutrition_database_unavailable")
        conn = sqlite3.connect(self.nutrition_write_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        # The isolated file bind intentionally exposes no sibling databases or WAL sidecars.
        conn.execute("PRAGMA journal_mode=DELETE")
        return conn

    def daily_note(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        date, category, text = value.date, value.category, value.text
        proposed = {"date": date, "category": category, "text": text}
        if value.dry_run: return {"status": "dry_run", "changed": False, "replayed": False, "executed": False, "operation_type": "daily.note.post", "affected_domain": "daily_note", "proposed_write": proposed, "warnings": [], "error_code": None}
        request_context = {"date": date, "category": category, "text": text}
        with self._connect(busy_timeout=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._existing_idempotency(conn, value.idempotency_key, "daily.note.post", request_context, legacy_match=lambda _readback: False)
            if replay:
                replay.update({"changed": False, "replayed": True, "executed": False})
                return replay
            before = self._row(conn.execute("SELECT * FROM daily_notes WHERE date_iso=? AND category=? AND text=?", (date,category,text)).fetchone())
            if before: return {"status": "duplicate", "changed": False, "replayed": False, "executed": False, "operation_type": "daily.note.post", "affected_domain": "daily_note", "affected_ids": [before["id"]], "readback": before, "warnings": ["An identical note already exists."], "error_code": None}
            cur = conn.execute("INSERT INTO daily_notes (date_iso,category,text,created_at) VALUES (?,?,?,?)", (date,category,text,utc_now()))
            after = self._row(conn.execute("SELECT * FROM daily_notes WHERE id=?", (cur.lastrowid,)).fetchone())
            if not after or after["date_iso"] != date or after["category"] != category or after["text"] != text:
                raise RuntimeError("readback_failed")
            summary = {"id": after["id"], "date_iso": after["date_iso"], "category": after["category"], "text_length": len(after["text"]), "created_at": after["created_at"]}
            def sanitize(result: dict[str, Any]) -> dict[str, Any]:
                stored = dict(result)
                stored["before"] = None
                stored["readback"] = summary
                return stored
            return self._finish(conn, tool="liva_post_daily_note", operation="daily.note.post", reason=value.reason, domain="daily_note", ids=[cur.lastrowid], before=before, after=after, key=value.idempotency_key, idempotency_context=request_context, audit_before=None, audit_after={**summary, "changed": True, "replayed": False}, idempotency_serializer=sanitize, result_fields={"changed": True, "replayed": False}, **context)

    def weight(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        proposed = {"date": value.date, "weight_kg": value.weight_kg, "note": value.note}
        if value.dry_run:
            return {"status": "dry_run", "changed": False, "replayed": False, "executed": False, "dry_run": True, "operation_type": "weight.upsert", "affected_domain": "weight", "proposed_write": proposed, "warnings": [], "error_code": None, "source": "production", "production_write": False, "visible_in_frontend": False, "visible_in_daily_snapshot": False, "readback_source": "weight_logs"}
        with self._connect(busy_timeout=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            request_context = {"date": value.date, "weight_kg": value.weight_kg, "note": value.note}
            replay = self._existing_idempotency(conn, value.idempotency_key, "weight.upsert", request_context)
            if replay:
                replay.update({"changed": False, "replayed": True, "executed": False})
                return replay
            with self._production_nutrition_connect() as production:
                columns = {str(row["name"]) for row in production.execute("PRAGMA table_info(weight_logs)")}
                if not {"id", "date_iso"}.issubset(columns) or not ({"weight_kg", "weight"} & columns):
                    raise RuntimeError("production_weight_schema_unavailable")
                weight_column = "weight_kg" if "weight_kg" in columns else "weight"
                before = self._row(production.execute("SELECT * FROM weight_logs WHERE date_iso=? ORDER BY id DESC LIMIT 1", (value.date,)).fetchone())
                if before and float(before.get(weight_column)) == value.weight_kg:
                    after = before
                    duplicate = True
                elif before:
                    assignments = [f"{weight_column}=?"]
                    params: list[Any] = [value.weight_kg]
                    if "raw_json" in columns:
                        try:
                            raw = json.loads(before.get("raw_json") or "{}") if before.get("raw_json") else {}
                        except (TypeError, json.JSONDecodeError):
                            raw = {}
                        if not isinstance(raw, dict): raw = {}
                        raw.update({"source": "chatgpt", "note": value.note})
                        assignments.append("raw_json=?"); params.append(json.dumps(raw, ensure_ascii=False, sort_keys=True))
                    params.append(before["id"])
                    production.execute(f"UPDATE weight_logs SET {', '.join(assignments)} WHERE id=?", tuple(params))
                    after = self._row(production.execute("SELECT * FROM weight_logs WHERE id=?", (before["id"],)).fetchone())
                    duplicate = False
                else:
                    values: dict[str, Any] = {"date_iso": value.date, weight_column: value.weight_kg}
                    if "date" in columns: values["date"] = datetime.fromisoformat(value.date).strftime("%d.%m.%y")
                    if "created_at" in columns: values["created_at"] = utc_now().replace("T", " ").removesuffix("Z")
                    if "raw_json" in columns: values["raw_json"] = json.dumps({"source": "chatgpt", "note": value.note}, ensure_ascii=False, sort_keys=True)
                    keys = list(values)
                    cur = production.execute(f"INSERT INTO weight_logs ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})", tuple(values[key] for key in keys))
                    after = self._row(production.execute("SELECT * FROM weight_logs WHERE id=?", (cur.lastrowid,)).fetchone())
                    duplicate = False
            if not after or float(after.get(weight_column)) != value.weight_kg:
                raise RuntimeError("production_weight_readback_failed")
            result = self._finish(conn, tool="liva_upsert_weight", operation="weight.upsert", reason=value.reason, domain="weight", ids=[after["id"]], before=before, after=after, key=value.idempotency_key, idempotency_context=request_context, **context)
            result.update({"changed": not duplicate, "replayed": False, "executed": not duplicate, "source": "production", "production_write": True, "visible_in_frontend": True, "visible_in_daily_snapshot": True, "readback_source": "weight_logs", "duplicate": duplicate})
            return result

    def nutrition(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        proposed = {"date":value.date,"text":value.text,"precision_level":value.precision_level,"values":value.values,"confidence":value.confidence}
        if value.dry_run: return {"executed":False,"dry_run":True,"proposed_write":proposed}
        with self._connect() as conn:
            replay=self._existing_idempotency(conn,value.idempotency_key,"nutrition.context.post")
            if replay:return replay
            encoded=json.dumps(value.values,ensure_ascii=False,sort_keys=True)
            before=self._row(conn.execute("SELECT * FROM nutrition_context_logs WHERE date_iso=? AND text=? AND precision_level=? AND values_json=?",(value.date,value.text,value.precision_level,encoded)).fetchone())
            if before:return {"executed":False,"duplicate":True,"readback":before}
            cur=conn.execute("INSERT INTO nutrition_context_logs (date_iso,text,precision_level,values_json,confidence,created_at) VALUES (?,?,?,?,?,?)",(value.date,value.text,value.precision_level,encoded,value.confidence,utc_now()))
            after=self._row(conn.execute("SELECT * FROM nutrition_context_logs WHERE id=?",(cur.lastrowid,)).fetchone()); after["values"]=json.loads(after.pop("values_json"))
            return self._finish(conn,tool="liva_post_nutrition_context",operation="nutrition.context.post",reason=value.reason,domain="nutrition_context",ids=[cur.lastrowid],before=before,after=after,key=value.idempotency_key,**context)

    def memory(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        proposed={"text":value.text,"tags":value.tags,"scope":value.scope,"replaces_id":value.replaces_id,"replaces_query":value.replaces_query}
        if value.dry_run:return {"executed":False,"dry_run":True,"proposed_write":proposed}
        with self._connect() as conn:
            replay=self._existing_idempotency(conn,value.idempotency_key,"memory.upsert")
            if replay:return replay
            before=self._row(conn.execute("SELECT * FROM memory_entries WHERE text_key=? AND status='active'",(value.text_key,)).fetchone())
            if before:return {"executed":False,"duplicate":True,"readback":before}
            replacement=None
            if value.replaces_id: replacement=self._row(conn.execute("SELECT * FROM memory_entries WHERE id=? AND status='active'",(value.replaces_id,)).fetchone())
            elif value.replaces_query: replacement=self._row(conn.execute("SELECT * FROM memory_entries WHERE text LIKE ? AND status='active' ORDER BY updated_at DESC LIMIT 1",("%"+value.replaces_query+"%",)).fetchone())
            now=utc_now(); cur=conn.execute("INSERT INTO memory_entries (text,text_key,tags_json,scope,status,supersedes_id,created_at,updated_at) VALUES (?,?,?,?, 'active',?,?,?)",(value.text,value.text_key,json.dumps(value.tags,ensure_ascii=False),value.scope,replacement["id"] if replacement else None,now,now))
            if replacement: conn.execute("UPDATE memory_entries SET status='superseded',updated_at=? WHERE id=?",(now,replacement["id"]))
            after=self._row(conn.execute("SELECT * FROM memory_entries WHERE id=?",(cur.lastrowid,)).fetchone()); after["tags"]=json.loads(after.pop("tags_json"))
            return self._finish(conn,tool="liva_upsert_memory_entry",operation="memory.upsert",reason=value.reason,domain="memory",ids=[cur.lastrowid]+([replacement["id"]] if replacement else []),before=replacement or before,after=after,key=value.idempotency_key,**context)

    def capture_memory(self, value: Any, context: dict[str, Any], client: Any) -> dict[str, Any]:
        with self._connect(busy_timeout=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._existing_idempotency(conn, value.idempotency_key, "memory.capture")
            if replay:
                replay.update({"replayed": True, "executed": False})
                return replay
            rows = client.search(100)
            normalized = " ".join(value.text.casefold().split())
            def content_of(row: dict[str, Any]) -> str:
                content = str(row.get("content") or "").replace("#gpt", "", 1)
                content = content.split("\n\nSupersedes:", 1)[0]
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                if lines and all(part.startswith("#") for part in lines[-1].split()):
                    lines.pop()
                return " ".join(" ".join(lines).split()).casefold()
            duplicate = next((row for row in rows if content_of(row) == normalized), None)
            if duplicate:
                return {"status": "duplicate", "executed": False, "replayed": False, "duplicate": True, "affected_ids": [duplicate.get("name")], "readback": {"name": duplicate.get("name"), "text_length": len(value.text)}}
            replacement = None
            if value.replaces_id is not None:
                replacement = next((row for row in rows if str(row.get("name", "")).removeprefix("memos/") == str(value.replaces_id)), None)
            elif value.replaces_query:
                needle = value.replaces_query.casefold()
                replacement = next((row for row in rows if needle in str(row.get("content") or "").casefold()), None)
            tag_text = " ".join(value.tags + [f"#scope_{value.scope}"])
            suffix = f"\n\nSupersedes: {replacement.get('name')}" if replacement else ""
            content = f"#gpt\n\n{value.text}{suffix}\n\n{tag_text}" if tag_text else f"#gpt\n\n{value.text}{suffix}"
            created = client.create(content, value.tags)
            if replacement and replacement.get("name"):
                client.archive(str(replacement["name"]))
            after = {"name": created.get("name"), "text_length": len(value.text), "scope": value.scope, "supersedes": replacement.get("name") if replacement else None}
            before = {"name": replacement.get("name")} if replacement else None
            return self._finish(conn, tool="liva_capture_memory", operation="memory.capture", reason=value.reason, domain="memory.usememos", ids=[created.get("name")], before=before, after=after, key=value.idempotency_key, audit_before=before, audit_after=after, **context)

    def flag(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        proposed={"date":value.date,"flag_type":value.flag_type,"severity":value.severity,"body_part":value.body_part,"text":value.text}
        if value.dry_run:return {"executed":False,"dry_run":True,"proposed_write":proposed}
        with self._connect() as conn:
            replay=self._existing_idempotency(conn,value.idempotency_key,"daily.flag.post")
            if replay:return replay
            before=self._row(conn.execute("SELECT * FROM daily_flags WHERE date_iso=? AND flag_type=? AND COALESCE(body_part,'')=COALESCE(?, '') AND COALESCE(text,'')=COALESCE(?, '')",(value.date,value.flag_type,value.body_part,value.text)).fetchone())
            if before:return {"executed":False,"duplicate":True,"readback":before}
            cur=conn.execute("INSERT INTO daily_flags (date_iso,flag_type,severity,body_part,text,created_at) VALUES (?,?,?,?,?,?)",(value.date,value.flag_type,value.severity,value.body_part,value.text,utc_now()))
            after=self._row(conn.execute("SELECT * FROM daily_flags WHERE id=?",(cur.lastrowid,)).fetchone())
            return self._finish(conn,tool="liva_post_daily_flag",operation="daily.flag.post",reason=value.reason,domain="daily_flag",ids=[cur.lastrowid],before=before,after=after,key=value.idempotency_key,**context)

    def rawlog(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        proposed={"date":value.date,"session_name":value.session_name,"raw_text":value.raw_text,"parsed_candidates":value.parsed_exercises}
        if value.dry_run:return {"executed":False,"dry_run":True,"proposed_write":proposed}
        with self._connect() as conn:
            replay=self._existing_idempotency(conn,value.idempotency_key,"training.rawlog.post")
            if replay:return replay
            before=self._row(conn.execute("SELECT * FROM training_rawlogs WHERE date_iso=? AND raw_text=?",(value.date,value.raw_text)).fetchone())
            if before:return {"executed":False,"duplicate":True,"readback":before}
            cur=conn.execute("INSERT INTO training_rawlogs (date_iso,session_name,raw_text,parsed_candidates_json,created_at) VALUES (?,?,?,?,?)",(value.date,value.session_name,value.raw_text,json.dumps(value.parsed_exercises,ensure_ascii=False) if value.parsed_exercises else None,utc_now()))
            after=self._row(conn.execute("SELECT * FROM training_rawlogs WHERE id=?",(cur.lastrowid,)).fetchone()); after["parsed_candidates"]=json.loads(after.pop("parsed_candidates_json")) if after.get("parsed_candidates_json") else []
            return self._finish(conn,tool="liva_post_training_rawlog",operation="training.rawlog.post",reason=value.reason,domain="training_rawlog",ids=[cur.lastrowid],before=before,after=after,key=value.idempotency_key,**context)

    def recent(self, table: str, date: str, limit: int=20) -> list[dict[str, Any]]:
        allowed={"daily_notes","daily_flags","nutrition_context_logs","training_rawlogs","weight_entries"}
        if table not in allowed or not self.path.exists(): return []
        try:
            with sqlite3.connect(self.path) as conn:
                conn.row_factory=sqlite3.Row
                column="date_iso"; rows=conn.execute(f"SELECT * FROM {table} WHERE {column}=? ORDER BY id DESC LIMIT ?",(date,limit)).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.OperationalError as exc:
            # A pre-schema or partially restored overlay must behave like an
            # empty optional overlay for reads. Server startup initializes it.
            if "no such table" in str(exc).lower():
                return []
            raise

    def audit_count(self) -> int:
        if not self.path.exists(): return 0
        with sqlite3.connect(self.path) as conn:return int(conn.execute("SELECT COUNT(*) FROM write_audit").fetchone()[0])
