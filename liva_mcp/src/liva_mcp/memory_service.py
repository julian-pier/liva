from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .memos_write import MemosWriteClient
from .memory_policy import MemoryPolicy
from .schemas import MemoryArchiveInput, MemoryCaptureInput
from .write_service import WriteService, digest, utc_now

_TAG = re.compile(r"(?<!\w)#([\wäöüÄÖÜß-]+)")


def _tags(content: str, supplied: list[str] | None = None) -> list[str]:
    values = list(supplied or []) + ["#" + m.group(1) for m in _TAG.finditer(content)]
    result: list[str] = []
    for value in values:
        value = "#" + re.sub(r"[^\wäöüÄÖÜß-]", "_", value.lstrip("#")).lower()
        if value != "#" and value not in result:
            result.append(value)
    return result


def _row(row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    name = str(row.get("name") or "")
    content = str(row.get("content") or row.get("snippet") or "")
    state = str(row.get("state") or "NORMAL").lower()
    result = {"memo_name": name, "snippet": content[:500], "tags": _tags(content, row.get("tags") if isinstance(row.get("tags"), list) else None),
              "created_at": row.get("createTime"), "updated_at": row.get("updateTime"), "status": "active" if state == "normal" else "archived", "source": "memos"}
    if full: result["content"] = content
    return result


class MemoryProductionService:
    """Canonical memory operations. The MCP overlay stores only audit/idempotency metadata."""
    def __init__(self, overlay: WriteService, client: MemosWriteClient, policy: MemoryPolicy | None = None):
        self.overlay, self.client, self.policy = overlay, client, policy or MemoryPolicy.default()

    def _record_relation(self, old_name: str, new_name: str) -> None:
        with self.overlay._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (memo_name TEXT PRIMARY KEY, origin TEXT, domain TEXT, memory_type TEXT, temporal_scope TEXT, event_date TEXT, valid_from TEXT, valid_until TEXT, status TEXT, supersedes TEXT, superseded_by TEXT, edited_at TEXT, policy_version INTEGER, legacy_metadata INTEGER NOT NULL DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS memory_relations (old_memo_name TEXT PRIMARY KEY, new_memo_name TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO memory_relations VALUES (?,?,?)", (old_name, new_name, utc_now()))
            conn.commit()

    def _record_metadata(self, name: str, value: MemoryCaptureInput, *, status: str = "active", supersedes: str | None = None, edited_at: str | None = None) -> None:
        with self.overlay._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (memo_name TEXT PRIMARY KEY, origin TEXT, domain TEXT, memory_type TEXT, temporal_scope TEXT, event_date TEXT, valid_from TEXT, valid_until TEXT, status TEXT, supersedes TEXT, superseded_by TEXT, edited_at TEXT, policy_version INTEGER, legacy_metadata INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT OR REPLACE INTO memory_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)", (name, value.origin, value.domain, value.memory_type, value.temporal_scope, value.event_date, utc_now(), value.valid_until, status, supersedes, None, edited_at, self.policy.version))
            conn.commit()

    def _metadata(self, name: str) -> dict[str, Any] | None:
        if not self.overlay.path.exists(): return None
        with self.overlay._connect() as conn:
            try:
                row = conn.execute("SELECT * FROM memory_metadata WHERE memo_name=?", (name,)).fetchone()
            except sqlite3.OperationalError:
                return None
            return dict(row) if row else None

    def _idem(self, key: str, operation: str, context: Any) -> dict[str, Any] | None:
        with self.overlay._connect(busy_timeout=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.overlay._existing_idempotency(conn, key, operation, context)

    def _store(self, key: str, operation: str, context: Any, result: dict[str, Any], *, tool: str, reason: str, ids: list[str], readback: dict[str, Any]) -> dict[str, Any]:
        with self.overlay._connect(busy_timeout=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.overlay._finish(conn, tool=tool, operation=operation, reason=reason, domain="memory.usememos", ids=ids, before=None, after=readback, key=key, client_id=None, request_id=None, idempotency_context=context, result_fields={**result, "readback": readback})

    @staticmethod
    def _readback(row: dict[str, Any], *, name: str, status: str, supersedes: str | None = None, superseded_by: str | None = None) -> dict[str, Any]:
        content = str(row.get("content") or "")
        result = {"memo_name": name, "content": content, "tags": _tags(content, row.get("tags") if isinstance(row.get("tags"), list) else None), "status": status, "source": "memos", "created_at": row.get("createTime"), "updated_at": row.get("updateTime"), "supersedes": supersedes, "superseded_by": superseded_by}
        return result

    def _get_or_created(self, name: str, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.client.get(name)
        except Exception:
            return fallback

    def _append_edit(self, value: MemoryCaptureInput, context: dict[str, Any]) -> dict[str, Any]:
        target = value.target_memo_name or ""
        existing = self.client.get(target)
        original = str(existing.get("content") or "")
        if "#texte" in _tags(original, existing.get("tags") if isinstance(existing.get("tags"), list) else None):
            raise ValueError("texte_memories_are_read_only")
        edit_date = value.event_date or utc_now()[:10]
        from datetime import date as Date
        stamp = Date.fromisoformat(edit_date[:10]).strftime("%d.%m.%Y")
        marker = f"EDIT — {stamp}\n\n{value.text}"
        if marker not in original:
            edited_now = datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds")
            self.client.update_content(
                target,
                original.rstrip() + "\n\n" + marker,
                create_time=edited_now,
            )
        updated = self._get_or_created(target, {**existing, "content": original.rstrip() + "\n\n" + marker})
        readback = self._readback(updated, name=target, status="active")
        result = {"stored": True, "source": "memos", "production_write": True, "readback_source": "memos", "memo_name": target, "status": "active", "changed": marker not in original, "duplicate": marker in original, "idempotency_replayed": False}
        stored = self._store(value.idempotency_key, "memory.append_edit", {"target_memo_name": target, "text": value.text, "event_date": value.event_date}, result, tool="liva_capture_memory", reason=value.reason, ids=[target], readback=readback)
        self._record_metadata(target, value, edited_at=utc_now())
        return stored

    def _content(self, value: MemoryCaptureInput) -> str:
        body = _TAG.sub("", value.text).strip()
        return " ".join(self.policy.tags(value.origin, value.domain, value.memory_type)) + "\n\n" + body

    def capture(self, value: MemoryCaptureInput, context: dict[str, Any]) -> dict[str, Any]:
        ctx = {"text": value.text, "tags": value.tags, "scope": value.scope, "replaces_memo_name": value.replaces_memo_name, "replaces_query": value.replaces_query}
        replay = self._idem(value.idempotency_key, "memory.capture", ctx)
        if replay:
            replay.update({"idempotency_replayed": True, "replayed": True})
            return replay
        if value.operation == "append_edit":
            return self._append_edit(value, context)
        rows = self.client.search(500)
        normalized = " ".join(_TAG.sub("", value.text).casefold().split())
        duplicate = next((r for r in rows if " ".join(_TAG.sub("", str(r.get("content") or "")).casefold().split()).split(" supersedes:", 1)[0] == normalized and str(r.get("state") or "NORMAL") == "NORMAL"), None)
        if duplicate:
            return {"stored": True, "source": "memos", "production_write": True, "readback_source": "memos", "memo_name": duplicate.get("name"), "status": "active", "changed": False, "duplicate": True, "idempotency_replayed": False}
        replacement = None
        replacement_name = value.replaces_memo_name or (f"memos/{value.replaces_id}" if value.replaces_id is not None else None)
        if replacement_name:
            replacement = next((r for r in rows if r.get("name") == replacement_name and str(r.get("state") or "NORMAL") == "NORMAL"), None)
            if replacement is None:
                raise ValueError("replacement_memo_not_found")
        elif value.replaces_query:
            matches = [r for r in rows if value.replaces_query.casefold() in str(r.get("content") or "").casefold() and str(r.get("state") or "NORMAL") == "NORMAL"]
            if len(matches) > 1:
                return {"stored": False, "source": "memos", "status": "conflict", "error": {"code": "replacement_query_ambiguous", "matches": [r.get("name") for r in matches]}}
            replacement = matches[0] if matches else None
        if replacement and "#texte" in _tags(str(replacement.get("content") or ""), replacement.get("tags") if isinstance(replacement.get("tags"), list) else None):
            raise ValueError("texte_memories_are_read_only")
        created = self.client.create(self._content(value), value.tags)
        new_name = created.get("name")
        if not new_name:
            raise RuntimeError("memos_create_missing_name")
        if replacement:
            existing_metadata = self._metadata(str(replacement["name"]))
            if existing_metadata and existing_metadata.get("temporal_scope") == "historical":
                raise ValueError("historical_memories_cannot_be_superseded")
            try:
                self.client.archive(str(replacement["name"]))
            except Exception as exc:
                try: self.client.archive(str(new_name))
                except Exception: pass
                raise RuntimeError("supersede_archive_failed") from exc
        result = {"stored": True, "source": "memos", "production_write": True, "readback_source": "memos", "memo_name": new_name, "status": "active", "changed": True, "duplicate": False, "idempotency_replayed": False}
        if replacement: result["superseded_memo_name"] = replacement["name"]
        memo = self._get_or_created(str(new_name), created)
        readback = self._readback(memo, name=str(new_name), status="active", supersedes=str(replacement["name"]) if replacement else None)
        stored = self._store(value.idempotency_key, "memory.capture", ctx, result, tool="liva_capture_memory", reason=value.reason, ids=[str(new_name)] + ([str(replacement["name"])] if replacement else []), readback=readback)
        if replacement: self._record_relation(str(replacement["name"]), str(new_name))
        self._record_metadata(str(new_name), value, supersedes=str(replacement["name"]) if replacement else None)
        if replacement: self._record_metadata(str(replacement["name"]), value, status="superseded", supersedes=None)
        return stored

    def archive(self, value: MemoryArchiveInput, context: dict[str, Any]) -> dict[str, Any]:
        replay = self._idem(value.idempotency_key, "memory.archive", {"memo_name": value.memo_name})
        if replay:
            replay.update({"idempotency_replayed": True})
            return replay
        already_archived = False
        try:
            current = self.client.get(value.memo_name)
            already_archived = str(current.get("state") or "").upper() == "ARCHIVED"
        except Exception:
            current = None
        if current and "#texte" in _tags(str(current.get("content") or ""), current.get("tags") if isinstance(current.get("tags"), list) else None):
            raise ValueError("texte_memories_are_read_only")
        if not already_archived:
            self.client.archive(value.memo_name)
        memo = self._get_or_created(value.memo_name, current or {"name": value.memo_name, "state": "ARCHIVED"})
        result = {"archived": True, "source": "memos", "production_write": True, "readback_source": "memos", "memo_name": value.memo_name, "status": "archived", "dry_run": False, "changed": not already_archived, "already_archived": already_archived, "idempotency_replayed": False}
        return self._store(value.idempotency_key, "memory.archive", {"memo_name": value.memo_name}, result, tool="liva_archive_memory", reason=value.reason, ids=[value.memo_name], readback=self._readback(memo, name=value.memo_name, status="archived"))
