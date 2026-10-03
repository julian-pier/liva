"""Filesystem-first direct-source store; SQLite contains only rebuildable metadata."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import difflib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class StoreError(ValueError):
    """Raised when a requested source operation is invalid."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.retryable = retryable


TEMPORAL_SCOPES = {"historical", "until_changed", "temporary"}
COMPILE_POLICIES = {"compile", "raw_only", "live_data"}
SOURCE_KINDS = {
    "legacy_memo", "chat_capture", "conversation_export", "user_authored", "structured_import"
}
IMPORT_SOURCE_KINDS = {"conversation_export", "user_authored", "structured_import"}
IMPORTED_CLAIM_CONTRACTS = {
    # A conversation export is always treated as a retrospective/assistant
    # artifact, even when it describes direct quotations.
    "conversation_export": ("imported_content", ["retrospective_synthesis", "assistant_summary", "assistant_interpretation"]),
    # A file authored by the user may support their own statement or a plainly
    # reported event, but is never silently promoted beyond that.
    "user_authored": ("imported_content", ["direct_user_statement", "reported_event"]),
    # Structured data is evidence only as a structured measurement.
    "structured_import": ("imported_content", ["structured_measurement"]),
}
EVIDENCE_TYPES = {
    "direct_user_statement", "reported_event", "structured_measurement", "assistant_summary",
    "assistant_interpretation", "retrospective_synthesis",
}
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ulid() -> str:
    """Generate a sortable ULID without a third-party dependency."""
    value = (int(datetime.now(UTC).timestamp() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10))
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "src_" + "".join(reversed(chars))


def quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def list_yaml(items: list[str]) -> str:
    return "\n".join(f"  - {quote_yaml(item)}" for item in items) or "  []"


class MemoryStore:
    def __init__(self, vault: Path | str, runtime_dir: Path | str | None = None):
        self.vault = Path(vault).resolve()
        default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "liva-memory"
        self.runtime_dir = Path(runtime_dir or default).resolve()
        self.db_path = self.runtime_dir / "memory.sqlite"

    def initialize(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "locks").mkdir(exist_ok=True)
        (self.runtime_dir / "logs").mkdir(exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS sources (
                  source_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                  content_sha256 TEXT NOT NULL, captured_at TEXT NOT NULL, event_date TEXT,
                  temporal_scope TEXT NOT NULL, ingest_status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compile_queue (
                  job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
                  status TEXT NOT NULL CHECK(status IN ('pending','leased','awaiting_review','completed','failed','ignored')),
                  created_at TEXT NOT NULL, available_at TEXT NOT NULL, leased_at TEXT, leased_by TEXT,
                  lease_until TEXT, completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_job_per_source
                  ON compile_queue(source_id) WHERE status IN ('pending','leased');
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                  idempotency_key TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_hashes (
                  original_sha256 TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(source_id),
                  created_at TEXT NOT NULL, PRIMARY KEY(original_sha256, source_kind)
                );
                CREATE TABLE IF NOT EXISTS wiki_sections (
                  section_id INTEGER PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
                  heading TEXT NOT NULL, anchor TEXT NOT NULL, text TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                  UNIQUE(path, anchor)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(path, title, heading, text);
                CREATE TABLE IF NOT EXISTS procedure_sections (
                  section_id INTEGER PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
                  heading TEXT NOT NULL, anchor TEXT NOT NULL, text TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                  UNIQUE(path, anchor)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS procedure_fts USING fts5(path, title, heading, text);
                CREATE TABLE IF NOT EXISTS proposals (
                  proposal_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES compile_queue(job_id),
                  source_id TEXT NOT NULL REFERENCES sources(source_id), worker_id TEXT NOT NULL,
                  status TEXT NOT NULL, submitted_at TEXT NOT NULL, path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_compile_state (source_id TEXT PRIMARY KEY, state TEXT NOT NULL, transaction_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_commits (idempotency_key TEXT PRIMARY KEY, transaction_id TEXT NOT NULL, result_json TEXT NOT NULL, payload_sha256 TEXT);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(compile_queue)")}
            if "lease_until" not in columns:
                db.execute("ALTER TABLE compile_queue ADD COLUMN lease_until TEXT")
            commit_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_commits)")}
            if "payload_sha256" not in commit_columns:
                db.execute("ALTER TABLE chat_commits ADD COLUMN payload_sha256 TEXT")
            queue_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='compile_queue'").fetchone()[0]
            if "awaiting_review" not in queue_sql:
                db.executescript(
                    """
                    ALTER TABLE compile_queue RENAME TO compile_queue_old;
                    CREATE TABLE compile_queue (
                      job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
                      status TEXT NOT NULL CHECK(status IN ('pending','leased','awaiting_review','completed','failed','ignored')),
                      created_at TEXT NOT NULL, available_at TEXT NOT NULL, leased_at TEXT, leased_by TEXT,
                      lease_until TEXT, completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
                    );
                    INSERT INTO compile_queue(job_id,source_id,status,created_at,available_at,leased_at,leased_by,lease_until,completed_at,attempts,last_error)
                    SELECT job_id,source_id,status,created_at,available_at,leased_at,leased_by,lease_until,completed_at,attempts,last_error FROM compile_queue_old;
                    DROP TABLE compile_queue_old;
                    CREATE UNIQUE INDEX IF NOT EXISTS one_open_job_per_source
                      ON compile_queue(source_id) WHERE status IN ('pending','leased');
                    """
                )
            fts_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='wiki_fts'").fetchone()
            if fts_sql and "content=''" in fts_sql[0]:
                db.execute("DROP TABLE wiki_fts")
                db.execute("CREATE VIRTUAL TABLE wiki_fts USING fts5(path, title, heading, text)")

    def memory_context(self, queries: list[str], layers: str = "auto", limit: int = 8, max_chars: int = 24000) -> dict[str, Any]:
        from .interactive import memory_context
        return memory_context(self, queries, layers, limit, max_chars)

    def memory_source(self, source_id: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        from .interactive import memory_source
        return memory_source(self, source_id, offset, max_chars)

    def memory_pending(self, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from .interactive import memory_pending
        return memory_pending(self, kind, limit)

    def memory_health(self, paths: list[str] | None = None) -> dict[str, Any]:
        from .interactive import memory_health
        return memory_health(self, paths)

    def memory_ingest_file(self, filename: str, source_kind: str = "conversation_export", title: str | None = None) -> dict[str, Any]:
        return self.ingest_inbox_file(filename, kind=source_kind, title=title)

    @staticmethod
    def imported_claim_contract(kind: str) -> dict[str, Any]:
        section, evidence_types = IMPORTED_CLAIM_CONTRACTS[kind]
        return {"source_sections": [{"name": section, "allowed_evidence_types": list(evidence_types)}]}

    def memory_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .interactive import memory_commit
        return memory_commit(self, payload)

    def queue_retry_source(self, source_id: str, reason: str = "manual retry") -> dict[str, Any]:
        self.initialize(); now = utc_now()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone(): raise StoreError("unknown source")
            if db.execute("SELECT 1 FROM compile_queue WHERE source_id=? AND status IN ('pending','leased','awaiting_review')", (source_id,)).fetchone(): raise StoreError("source already has an open job")
            job_id = "job_" + ulid()[4:]
            db.execute("INSERT INTO compile_queue(job_id,source_id,status,created_at,available_at,last_error) VALUES (?,?,'pending',?,?,?)", (job_id, source_id, now, now, reason[:500]))
        return {"job_id": job_id, "source_id": source_id, "status": "pending"}

    def capture_chat_memory(
        self, *, direct_user_context: str, assistant_synthesis: str, event_date: str | None = None,
        temporal_scope: str = "historical", topics: list[str] | None = None,
        evidence_types: list[str] | None = None, idempotency_key: str,
        compile_policy: str = "compile", title: str = "Chat Capture",
    ) -> dict[str, Any]:
        self._validate(temporal_scope, compile_policy, topics or [], evidence_types or [], idempotency_key)
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT source_id FROM idempotency_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return self._source_result(db, row[0], duplicate=True)
        captured_at = utc_now()
        source_id = self._new_id()
        body = self._capture_body(title, direct_user_context, assistant_synthesis)
        digest = sha256_bytes(body.encode("utf-8"))
        relative = Path("sources/captures") / captured_at[:4] / captured_at[5:7] / f"{source_id}.md"
        content = self._capture_document(
            source_id, captured_at, event_date, temporal_scope, topics or [], evidence_types or [],
            compile_policy, digest, title, body,
        )
        self._atomic_write(self.vault / relative, content.encode("utf-8"))
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT source_id FROM idempotency_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row:  # A concurrent retry won; preserve its source and remove only ours.
                    (self.vault / relative).unlink(missing_ok=True)
                    db.rollback()
                    return self._source_result(db, row[0], duplicate=True)
                self._insert_source(db, source_id, "chat_capture", str(relative), digest, captured_at, event_date, temporal_scope)
                db.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?)", (idempotency_key, source_id, captured_at))
                queued = self._enqueue(db, source_id, compile_policy, captured_at)
                db.commit()
        except Exception:
            # A file without a successful metadata transaction must never look complete.
            (self.vault / relative).unlink(missing_ok=True)
            raise
        return {"source_id": source_id, "path": str(relative), "sha256": digest, "queued": queued, "duplicate": False}

    def ingest_file(
        self, original: Path | str, *, kind: str, title: str, temporal_scope: str = "historical",
        event_date: str | None = None, compile_policy: str = "compile",
    ) -> dict[str, Any]:
        if kind not in IMPORT_SOURCE_KINDS:
            raise StoreError("invalid import source_kind")
        self._validate(temporal_scope, compile_policy, [], [], "import")
        raw = Path(original).read_bytes()
        digest = sha256_bytes(raw)
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT source_id FROM import_hashes WHERE original_sha256=? AND source_kind=?", (digest, kind)).fetchone()
            if row:
                result = self._source_result(db, row[0], duplicate=True)
                result["already_ingested"] = True
                return result
        source_id, captured_at = self._new_id(), utc_now()
        relative_dir = Path("sources/imports") / source_id
        target_dir = self.vault / relative_dir
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._atomic_write(target_dir / "original.txt", raw)
            manifest = self._import_manifest(source_id, captured_at, title, kind, Path(original).name, len(raw), temporal_scope, event_date, digest, compile_policy)
            self._atomic_write(target_dir / "source.md", manifest.encode("utf-8"))
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT source_id FROM import_hashes WHERE original_sha256=? AND source_kind=?", (digest, kind)).fetchone()
                if row:
                    shutil.rmtree(target_dir)
                    db.rollback()
                    result = self._source_result(db, row[0], duplicate=True)
                    result["already_ingested"] = True
                    return result
                self._insert_source(db, source_id, kind, str(relative_dir / "source.md"), digest, captured_at, event_date, temporal_scope)
                db.execute("INSERT INTO import_hashes VALUES (?, ?, ?, ?)", (digest, kind, source_id, captured_at))
                queued = self._enqueue(db, source_id, compile_policy, captured_at)
                db.commit()
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        return {"source_id": source_id, "path": str(relative_dir / "source.md"), "sha256": digest, "queued": queued, "duplicate": False, "already_ingested": False}

    def ingest_inbox_file(self, filename: str, *, kind: str = "conversation_export", title: str | None = None) -> dict[str, Any]:
        """Ingest exactly one regular .md/.txt file from the vault-local inbox."""
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise StoreError("invalid inbox filename")
        candidate_name = Path(filename)
        if candidate_name.name != filename or candidate_name.suffix.casefold() not in {".md", ".txt"}:
            raise StoreError("invalid inbox filename")
        inbox = self.vault / "_inbox"
        candidate = inbox / candidate_name
        if candidate.is_symlink() or not candidate.is_file():
            raise StoreError("inbox file not found")
        if kind not in IMPORT_SOURCE_KINDS:
            raise StoreError("invalid import source_kind")
        raw = candidate.read_bytes()
        result = self.ingest_file(candidate, kind=kind, title=title or candidate.stem)
        # Only delete after the immutable canonical source and its metadata were
        # committed. On every error above, the inbox file remains untouched.
        candidate.unlink()
        state = "uncompiled"
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT state FROM chat_compile_state WHERE source_id=?", (result["source_id"],)).fetchone()
            if row:
                state = row["state"]
        source_id = result["source_id"]
        return {
            "source_id": source_id,
            "filename": filename,
            "source_kind": kind,
            "sha256": sha256_bytes(raw),
            "size": len(raw),
            "state": state,
            "duplicate": bool(result["duplicate"]),
            "replayed": bool(result.get("already_ingested", False)),
            "stored_path": str(Path("sources/imports") / source_id / "original.txt"),
            "claim_source_section": self.imported_claim_contract(kind)["source_sections"][0]["name"],
            "allowed_evidence_types": self.imported_claim_contract(kind)["source_sections"][0]["allowed_evidence_types"],
        }

    def queue_list(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM compile_queue ORDER BY created_at, job_id")]

    def queue_stats(self) -> dict[str, int]:
        self.initialize()
        with self._connect() as db:
            result = {status: 0 for status in ("pending", "leased", "awaiting_review", "completed", "failed", "ignored")}
            result.update(dict(db.execute("SELECT status, count(*) FROM compile_queue GROUP BY status")))
            return result

    def source_list(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sources ORDER BY captured_at, source_id")]

    def source_show(self, source_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            return self._source_result(db, source_id, duplicate=False)

    def reindex(self) -> dict[str, int]:
        """Rebuild only direct-source metadata from canonical Markdown/TXT files."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self.db_path.unlink()
        self.initialize()
        indexed = queued = 0
        for path in sorted((self.vault / "sources/captures").glob("*/*/*.md")) if (self.vault / "sources/captures").exists() else []:
            meta, body = self._read_frontmatter(path)
            digest = sha256_bytes(body.encode("utf-8"))
            if meta.get("content_sha256") != digest:
                raise StoreError(f"hash mismatch: {path}")
            self._reindex_one(meta, path.relative_to(self.vault), digest, "chat_capture")
            indexed += 1
            queued += meta.get("compile_policy", "compile") == "compile"
        imports = self.vault / "sources/imports"
        for manifest_path in sorted(imports.glob("*/source.md")) if imports.exists() else []:
            meta, _ = self._read_frontmatter(manifest_path)
            original = manifest_path.parent / str(meta.get("original_file", ""))
            digest = sha256_bytes(original.read_bytes()) if original.exists() else ""
            if not original.exists() or meta.get("original_sha256") != digest:
                raise StoreError(f"invalid import manifest: {manifest_path}")
            kind = meta.get("source_kind", "conversation_export")
            if kind not in IMPORT_SOURCE_KINDS:
                raise StoreError(f"invalid import source_kind: {manifest_path}")
            self._reindex_one(meta, manifest_path.relative_to(self.vault), digest, kind)
            indexed += 1
            queued += meta.get("compile_policy", "compile") == "compile"
        return {"indexed": indexed, "queued": queued}

    def verify(self) -> dict[str, Any]:
        self.initialize()
        errors: list[str] = []
        with self._connect() as db:
            rows = list(db.execute("SELECT * FROM sources"))
            ids = set()
            for row in rows:
                source_id, path = row["source_id"], self.vault / row["path"]
                if source_id in ids: errors.append(f"duplicate source_id: {source_id}")
                ids.add(source_id)
                if not path.exists(): errors.append(f"missing source: {row['path']}"); continue
                try:
                    if row["source_kind"] in IMPORT_SOURCE_KINDS:
                        meta, _ = self._read_frontmatter(path)
                        original = path.parent / str(meta.get("original_file", ""))
                        digest = sha256_bytes(original.read_bytes())
                        if meta.get("original_sha256") != digest: errors.append(f"import hash mismatch: {row['path']}")
                    else:
                        meta, body = self._read_frontmatter(path)
                        digest = sha256_bytes(body.encode("utf-8"))
                        if meta.get("content_sha256") != digest: errors.append(f"content hash mismatch: {row['path']}")
                    if digest != row["content_sha256"]: errors.append(f"database hash mismatch: {row['path']}")
                except Exception as exc: errors.append(f"invalid source {row['path']}: {exc}")
            for row in db.execute("SELECT q.job_id FROM compile_queue q LEFT JOIN sources s ON s.source_id=q.source_id WHERE s.source_id IS NULL"):
                errors.append(f"queue references missing source: {row['job_id']}")
            duplicates = db.execute("SELECT source_id, count(*) n FROM compile_queue WHERE status IN ('pending','leased') GROUP BY source_id HAVING n>1").fetchall()
            errors.extend(f"multiple open jobs: {r['source_id']}" for r in duplicates)
        return {"valid": not errors, "source_count": len(rows), "errors": errors}

    def queue_lease(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        if not worker_id or not 30 <= lease_seconds <= 7200: raise StoreError("invalid worker_id or lease_seconds")
        self.initialize(); now = datetime.now(UTC); now_s = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        until = (now + timedelta(seconds=lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE compile_queue SET status='pending', leased_at=NULL, leased_by=NULL, lease_until=NULL WHERE status='leased' AND lease_until < ?", (now_s,))
            row = db.execute("SELECT q.job_id, q.source_id FROM compile_queue q WHERE q.status='pending' AND q.available_at <= ? AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.job_id=q.job_id) ORDER BY q.created_at, q.job_id LIMIT 1", (now_s,)).fetchone()
            if not row: db.commit(); return None
            db.execute("UPDATE compile_queue SET status='leased', leased_at=?, leased_by=?, lease_until=?, attempts=attempts+1 WHERE job_id=?", (now_s, worker_id, until, row["job_id"]))
            db.commit()
            return {"job_id": row["job_id"], "source_id": row["source_id"], "lease_until": until}

    def queue_heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 7200: raise StoreError("invalid lease_seconds")
        self.initialize(); now = datetime.now(UTC); until = (now + timedelta(seconds=lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._connect() as db:
            changed = db.execute("UPDATE compile_queue SET lease_until=? WHERE job_id=? AND status='leased' AND leased_by=? AND lease_until >= ?", (until, job_id, worker_id, utc_now())).rowcount
            if not changed: raise StoreError("lease is not held by worker")
        return {"job_id": job_id, "lease_until": until}

    def queue_release(self, job_id: str, worker_id: str, reason: str = "released") -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            changed = db.execute("UPDATE compile_queue SET status='pending', available_at=?, leased_at=NULL, leased_by=NULL, lease_until=NULL, last_error=? WHERE job_id=? AND status='leased' AND leased_by=?", (utc_now(), reason[:500], job_id, worker_id)).rowcount
            if not changed: raise StoreError("lease is not held by worker")
        return {"job_id": job_id, "status": "pending"}

    def queue_fail(self, job_id: str, worker_id: str, error: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            changed = db.execute("UPDATE compile_queue SET status='failed', completed_at=?, lease_until=NULL, last_error=? WHERE job_id=? AND status='leased' AND leased_by=?", (utc_now(), error[:2000], job_id, worker_id)).rowcount
            if not changed: raise StoreError("lease is not held by worker")
        return {"job_id": job_id, "status": "failed"}

    def source_content(self, source_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not row: raise StoreError("unknown source")
        path = self.vault / row["path"]
        meta, body = self._read_frontmatter(path)
        if row["source_kind"] in IMPORT_SOURCE_KINDS:
            original = path.parent / meta["original_file"]
            data = original.read_bytes(); content = data.decode("utf-8", errors="replace")
            digest = sha256_bytes(data)
        else:
            content = body; digest = sha256_bytes(body.encode("utf-8"))
        if digest != row["content_sha256"]: raise StoreError("source hash mismatch")
        evidence = meta.get("evidence_types", [])
        sections: list[dict[str, Any]] = []
        if row["source_kind"] == "chat_capture":
            direct = re.search(r"^## Direkter Nutzerkontext\s*$([\s\S]*?)(?=^## |\Z)", content, re.M)
            synthesis = re.search(r"^## Verdichtete Erinnerung\s*$([\s\S]*?)(?=^## |\Z)", content, re.M)
            for name, match, allowed in (("direct_user_context", direct, ["direct_user_statement"]), ("assistant_synthesis", synthesis, [x for x in evidence if x in {"assistant_summary", "assistant_interpretation", "retrospective_synthesis"}])):
                if match:
                    first = content[:match.start(1)].count("\n") + 1; last = content[:match.end(1)].count("\n") + 1
                    sections.append({"name": name, "line_start": first, "line_end": last, "content": match.group(1).strip(), "allowed_evidence_types": allowed})
        if row["source_kind"] in IMPORT_SOURCE_KINDS:
            contract = self.imported_claim_contract(row["source_kind"])
            evidence = contract["source_sections"][0]["allowed_evidence_types"]
            sections = [{
                "name": contract["source_sections"][0]["name"],
                "line_start": 1,
                "line_end": content.count("\n") + 1,
                "content": content,
                "allowed_evidence_types": evidence,
            }]
        if not sections:
            sections = [{"name": "source_content", "line_start": 1, "line_end": content.count("\n") + 1, "content": content, "allowed_evidence_types": evidence}]
        return {"source_id": source_id, "source_kind": row["source_kind"], "event_date": row["event_date"], "temporal_scope": row["temporal_scope"], "evidence_types": evidence, "sections": sections, "content": content, "sha256": digest}

    def _knowledge_reindex(self, layer: str) -> dict[str, int]:
        self.initialize(); sections: list[tuple[str, str, str, str, str, str]] = []
        if layer == "living_wiki": root, table, fts = self.vault / "wiki", "wiki_sections", "wiki_fts"
        elif layer == "procedures": root, table, fts = self.vault / "procedures", "procedure_sections", "procedure_fts"
        else: raise StoreError("invalid knowledge layer")
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(self.vault)
            if layer == "procedures" and (relative.parts[:2] in {("procedures", "runtime"), ("procedures", "obsidian")} or "test" in path.name.casefold()): continue
            raw = path.read_text(encoding="utf-8"); title = next((line[2:].strip() for line in raw.splitlines() if line.startswith("# ")), path.stem)
            matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", raw, re.M))
            if not matches: matches = [re.match(r"^", raw)]
            for n, match in enumerate(matches):
                start = match.end() if match and match.group(0) else 0; end = matches[n + 1].start() if n + 1 < len(matches) else len(raw)
                heading = match.group(2).strip() if match and match.group(0) else title
                text = raw[start:end].strip(); anchor = f"h{n}"
                sections.append((str(path.relative_to(self.vault)), title, heading, anchor, text, sha256_bytes(text.encode())))
        with self._connect() as db:
            db.execute(f"DELETE FROM {table}"); db.execute(f"DELETE FROM {fts}")
            for item in sections:
                db.execute(f"INSERT INTO {table}(path,title,heading,anchor,text,content_sha256) VALUES (?,?,?,?,?,?)", item)
                db.execute(f"INSERT INTO {fts}(path,title,heading,text) VALUES (?,?,?,?)", (item[0], item[1], item[2], item[4]))
        return {"pages": len({s[0] for s in sections}), "sections": len(sections)}

    def wiki_reindex(self) -> dict[str, int]: return self._knowledge_reindex("living_wiki")
    def procedure_reindex(self) -> dict[str, int]: return self._knowledge_reindex("procedures")

    def wiki_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._knowledge_search("living_wiki", query, limit)

    def procedure_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._knowledge_search("procedures", query, limit)

    def _knowledge_search(self, layer: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not query or not 1 <= limit <= 20: raise StoreError("invalid search")
        # FTS5 query syntax treats punctuation (notably hyphens) as operators.
        # Worker input is natural language, so turn it into safely quoted terms.
        terms = re.findall(r"[\wÀ-ÿ]+", query, flags=re.UNICODE)
        if not terms: return []
        fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:32])
        self.initialize()
        with self._connect() as db:
            fts = "wiki_fts" if layer == "living_wiki" else "procedure_fts" if layer == "procedures" else None
            if not fts: raise StoreError("invalid knowledge layer")
            rows = db.execute(f"SELECT path,title,heading,highlight({fts},3,'','') excerpt,bm25({fts}) score FROM {fts} WHERE {fts} MATCH ? ORDER BY score LIMIT ?", (fts_query, limit)).fetchall()
            return [dict(r) for r in rows]

    def wiki_get(self, path: str, heading: str | None = None, content_sha256: str | None = None) -> dict[str, Any]:
        return self._knowledge_get("living_wiki", path, heading, content_sha256)

    def procedure_get(self, path: str, heading: str | None = None, content_sha256: str | None = None) -> dict[str, Any]:
        return self._knowledge_get("procedures", path, heading, content_sha256)

    def _knowledge_get(self, layer: str, path: str, heading: str | None = None, content_sha256: str | None = None) -> dict[str, Any]:
        prefix, table = ("wiki/", "wiki_sections") if layer == "living_wiki" else ("procedures/", "procedure_sections") if layer == "procedures" else (None, None)
        if not prefix or not path.startswith(prefix) or ".." in Path(path).parts: raise StoreError("invalid knowledge path")
        self.initialize()
        with self._connect() as db:
            if heading:
                if content_sha256:
                    row = db.execute(f"SELECT path,title,heading,anchor,text,content_sha256 FROM {table} WHERE path=? AND heading=? AND content_sha256=?", (path, heading, content_sha256)).fetchone()
                else:
                    row = db.execute(f"SELECT path,title,heading,anchor,text,content_sha256 FROM {table} WHERE path=? AND heading=?", (path, heading)).fetchone()
            else:
                row = db.execute(f"SELECT path,title,heading,anchor,text,content_sha256 FROM {table} WHERE path=? ORDER BY section_id LIMIT 1", (path,)).fetchone()
        if not row: raise StoreError("wiki section not found")
        result = dict(row); page = self.vault / path
        result["page_sha256"] = sha256_bytes(page.read_bytes())
        return result

    def proposal_submit(self, proposal: dict[str, Any], worker_id: str) -> dict[str, Any]:
        required = {"job_id", "source_id", "source_sha256", "operation", "claims", "verification", "model", "prompt_version"}
        if not required <= proposal.keys(): raise StoreError("proposal schema incomplete")
        job_id, source_id = proposal["job_id"], proposal["source_id"]
        if not isinstance(proposal["claims"], list) or not isinstance(proposal["verification"], list): raise StoreError("proposal claims/verification invalid")
        op = proposal["operation"]
        if op.get("operation") not in {"modify_section", "create_page", "no_change"}: raise StoreError("invalid proposal operation")
        target_layer = op.get("target_layer")
        if target_layer not in {"living_wiki", "procedures"}: raise StoreError("invalid target layer")
        claim_ids = {c.get("claim_id") for c in proposal["claims"] if isinstance(c, dict)}
        if None in claim_ids or (not claim_ids and op.get("operation") != "no_change"): raise StoreError("proposal claims invalid")
        sections = {s["name"]: set(s["allowed_evidence_types"]) for s in self.source_content(source_id)["sections"]}
        if any(c.get("source_section") not in sections or c.get("evidence_type") not in sections[c.get("source_section")] for c in proposal["claims"] if isinstance(c, dict)): raise StoreError("claim evidence type is incompatible with source section")
        used = set(op.get("used_claim_ids", []))
        if not used <= claim_ids: raise StoreError("unknown used claim")
        if op["operation"] == "no_change":
            if any(op.get(key) is not None for key in ("target_path", "target_heading", "before_sha256", "new_text")) or used or proposal["verification"]: raise StoreError("no_change must not contain target, hash, text, used claims, or verification")
            before = after = ""
            if op.get("classification") in {"DUPLICATE", "CONFIRMS"}:
                evidence = proposal.get("duplicate_evidence")
                if not isinstance(evidence, dict) or not isinstance(evidence.get("matched_existing_text"), str) or not evidence["matched_existing_text"].strip() or not set(evidence.get("matched_claim_ids", [])) <= claim_ids or not evidence.get("matched_claim_ids"): raise StoreError("duplicate/confirm needs concrete match evidence")
                candidates = proposal.get("retrieval_candidates", [])
                if not isinstance(candidates, list) or not any(evidence["matched_existing_text"] in str(candidate.get("excerpt", "")) for candidate in candidates if isinstance(candidate, dict)): raise StoreError("duplicate evidence is not present in retrieval candidates")
        else:
            target = op.get("target_path")
            after = op.get("new_text")
            if not isinstance(after, str) or not after.strip(): raise StoreError("write proposal needs new_text")
            if op["operation"] == "modify_section":
                prefix = "wiki/" if target_layer == "living_wiki" else "procedures/"
                if not isinstance(target, str) or not target.startswith(prefix) or ".." in Path(target).parts or Path(target).suffix != ".md": raise StoreError("invalid target path")
                file_path = self.vault / target
                if not file_path.exists() or not isinstance(op.get("before_sha256"), str) or not isinstance(op.get("target_heading"), str): raise StoreError("modify_section needs existing target, heading, and before_sha")
                section = self._knowledge_get(target_layer, target, op["target_heading"])
                before = section["text"]
                if section["content_sha256"] != op.get("before_sha256"): raise StoreError("before_sha mismatch")
            else:
                if target is not None:
                    prefix = "wiki/" if target_layer == "living_wiki" else "procedures/"
                    if not isinstance(target, str) or not target.startswith(prefix) or ".." in Path(target).parts or Path(target).suffix != ".md": raise StoreError("invalid target path")
                    if (self.vault / target).exists(): raise StoreError("create_page target already exists")
                if op.get("target_heading") is not None or op.get("before_sha256") is not None: raise StoreError("create_page must have no heading or before_sha")
                before = ""
            self._verify_sentences(after, proposal["verification"], claim_ids, before)
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT status,leased_by,source_id FROM compile_queue WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "leased" or row["leased_by"] != worker_id or row["source_id"] != source_id: raise StoreError("job lease invalid")
            source = db.execute("SELECT content_sha256 FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not source or source[0] != proposal["source_sha256"]: raise StoreError("source hash mismatch")
            proposal_id = "proposal_" + ulid()[4:]; directory = self.runtime_dir / "proposals" / job_id; directory.mkdir(parents=True, exist_ok=False)
            now = utc_now(); stored = dict(proposal, proposal_id=proposal_id, worker_id=worker_id, submitted_at=now)
            self._atomic_write(directory / "proposal.json", json.dumps(stored, ensure_ascii=False, indent=2).encode())
            self._atomic_write(directory / "claims.json", json.dumps(proposal["claims"], ensure_ascii=False, indent=2).encode())
            self._atomic_write(directory / "verification.json", json.dumps(proposal["verification"], ensure_ascii=False, indent=2).encode())
            self._atomic_write(directory / "worker-log.json", json.dumps(proposal.get("worker_log", {}), ensure_ascii=False, indent=2).encode())
            self._atomic_write(directory / "before.md", before.encode()); self._atomic_write(directory / "after.md", after.encode())
            patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="before.md", tofile="after.md")); self._atomic_write(directory / "diff.patch", patch.encode())
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?)", (proposal_id, job_id, source_id, worker_id, "awaiting_review", now, str(directory)))
            db.execute("UPDATE compile_queue SET status='awaiting_review', lease_until=NULL WHERE job_id=?", (job_id,))
        return {"proposal_id": proposal_id, "job_id": job_id, "status": "awaiting_review"}

    def proposal_list(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as db: return [dict(r) for r in db.execute("SELECT * FROM proposals ORDER BY submitted_at DESC")]

    def proposal_show(self, proposal_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db: row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row: raise StoreError("proposal not found")
        data = json.loads((Path(row["path"]) / "proposal.json").read_text(encoding="utf-8")); data["status"] = row["status"]; return data

    def proposal_reject(self, proposal_id: str, reason: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT job_id FROM proposals WHERE proposal_id=? AND status='awaiting_review'", (proposal_id,)).fetchone()
            if not row: raise StoreError("proposal not awaiting review")
            db.execute("UPDATE proposals SET status='rejected' WHERE proposal_id=?", (proposal_id,))
            db.execute("UPDATE compile_queue SET status='ignored', completed_at=?, last_error=? WHERE job_id=?", (utc_now(), reason[:1000], row["job_id"]))
        return {"proposal_id": proposal_id, "status": "rejected"}

    @staticmethod
    def _verify_sentences(text: str, mappings: list[dict[str, Any]], claim_ids: set[str], existing: str) -> None:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if len(s.strip()) > 2]
        if not sentences: raise StoreError("write proposal needs verifiable sentences")
        by_sentence = {m.get("sentence"): m for m in mappings if isinstance(m, dict)}
        for sentence in sentences:
            mapping = by_sentence.get(sentence, {})
            supported = set(mapping.get("supported_by_new_claims", mapping.get("supported_by", [])))
            preserved = mapping.get("preserved_from_existing_section", False) is True and sentence in existing
            if (not supported or not supported <= claim_ids) and not preserved: raise StoreError("unsupported sentence in proposal")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _new_id(self) -> str:
        source_id = ulid()
        while any((self.vault / "sources/captures").glob(f"*/*/{source_id}.md")):
            source_id = ulid()
        return source_id

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _insert_source(self, db: sqlite3.Connection, source_id: str, kind: str, path: str, digest: str, captured_at: str, event_date: str | None, temporal_scope: str) -> None:
        db.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)", (source_id, kind, path, digest, captured_at, event_date, temporal_scope, captured_at))

    def _enqueue(self, db: sqlite3.Connection, source_id: str, policy: str, now: str) -> bool:
        if policy != "compile": return False
        db.execute("INSERT INTO compile_queue(job_id, source_id, status, created_at, available_at) VALUES (?, ?, 'pending', ?, ?)", ("job_" + ulid()[4:], source_id, now, now))
        return True

    def _source_result(self, db: sqlite3.Connection, source_id: str, duplicate: bool) -> dict[str, Any]:
        row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not row: raise StoreError(f"unknown source: {source_id}")
        queued = bool(db.execute("SELECT 1 FROM compile_queue WHERE source_id=? AND status IN ('pending','leased')", (source_id,)).fetchone())
        return {"source_id": source_id, "path": row["path"], "sha256": row["content_sha256"], "queued": queued, "duplicate": duplicate}

    def _validate(self, temporal_scope: str, compile_policy: str, topics: list[str], evidence_types: list[str], idempotency_key: str) -> None:
        if temporal_scope not in TEMPORAL_SCOPES: raise StoreError("invalid temporal_scope")
        if compile_policy not in COMPILE_POLICIES: raise StoreError("invalid compile_policy")
        if not idempotency_key: raise StoreError("idempotency_key is required")
        if any(not isinstance(x, str) or not x for x in topics): raise StoreError("topics must be non-empty strings")
        if set(evidence_types) - EVIDENCE_TYPES: raise StoreError("invalid evidence type")

    @staticmethod
    def _capture_body(title: str, context: str, synthesis: str) -> str:
        if not title or not synthesis: raise StoreError("title and assistant_synthesis are required")
        # Canonicalise transport newlines before hashing.  Path.read_text uses
        # universal-newline decoding; without this boundary normalisation a
        # CRLF paste is written successfully but becomes unreadable by the
        # next source/finish call because its recomputed hash uses LF.
        context = context.replace("\r\n", "\n").replace("\r", "\n")
        synthesis = synthesis.replace("\r\n", "\n").replace("\r", "\n")
        return f"# {title}\n\n## Direkter Nutzerkontext\n\n{context or 'Nicht mitgeliefert.'}\n\n## Verdichtete Erinnerung\n\n{synthesis}\n"

    @staticmethod
    def _capture_document(source_id: str, captured_at: str, event_date: str | None, scope: str, topics: list[str], evidence: list[str], policy: str, digest: str, title: str, body: str) -> str:
        return f"---\nschema_version: 1\nsource_id: {quote_yaml(source_id)}\nsource_kind: chat_capture\ncaptured_at: {quote_yaml(captured_at)}\nevent_date: {quote_yaml(event_date or '')}\ntemporal_scope: {scope}\ntopics:\n{list_yaml(topics)}\nevidence_types:\n{list_yaml(evidence)}\ncompile_policy: {policy}\ncontent_sha256: {quote_yaml(digest)}\n---\n\n{body}"

    @staticmethod
    def _import_manifest(source_id: str, captured_at: str, title: str, kind: str, filename: str, size: int, scope: str, event_date: str | None, digest: str, policy: str) -> str:
        return f"---\nschema_version: 1\nsource_id: {quote_yaml(source_id)}\nsource_kind: {kind}\ntitle: {quote_yaml(title)}\nfilename: {quote_yaml(filename)}\ncaptured_at: {quote_yaml(captured_at)}\nimported_at: {quote_yaml(captured_at)}\nevent_date: {quote_yaml(event_date or '')}\ntemporal_scope: {scope}\nevidence_types:\n  - \"retrospective_synthesis\"\ncompile_policy: {policy}\ncompile_state: uncompiled\noriginal_file: original.txt\noriginal_sha256: {quote_yaml(digest)}\nsize: {size}\n---\n\n# Import manifest\n\nDer kanonische Inhalt liegt byte-identisch in `original.txt`.\n"

    @staticmethod
    def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
        # Decode bytes directly so legacy captures whose immutable body
        # contains CRLF retain the exact byte-derived text used for their
        # stored content hash.  New captures are normalised in _capture_body.
        text = path.read_bytes().decode("utf-8")
        if not text.startswith("---\n"): raise StoreError("frontmatter missing")
        _, raw, body = text.split("---\n", 2)
        meta: dict[str, Any] = {}
        current: str | None = None
        for line in raw.splitlines():
            if line.startswith("  - ") and current:
                meta.setdefault(current, []).append(json.loads(line[4:]))
            elif line.endswith(":"):
                current = line[:-1]
                meta[current] = []
            elif ": " in line:
                key, value = line.split(": ", 1); current = key
                if value: meta[key] = json.loads(value) if value.startswith('"') else value
                else: meta[key] = []
        return meta, body.lstrip("\n")

    def _reindex_one(self, meta: dict[str, Any], path: Path, digest: str, kind: str) -> None:
        required = ("source_id", "captured_at", "temporal_scope")
        if any(not meta.get(k) for k in required): raise StoreError(f"incomplete source metadata: {path}")
        if meta["temporal_scope"] not in TEMPORAL_SCOPES: raise StoreError(f"invalid temporal scope: {path}")
        with self._connect() as db:
            self._insert_source(db, meta["source_id"], kind, str(path), digest, meta["captured_at"], meta.get("event_date") or None, meta["temporal_scope"])
            self._enqueue(db, meta["source_id"], meta.get("compile_policy", "compile"), meta["captured_at"])
            if kind in IMPORT_SOURCE_KINDS: db.execute("INSERT INTO import_hashes VALUES (?, ?, ?, ?)", (digest, kind, meta["source_id"], meta["captured_at"]))
