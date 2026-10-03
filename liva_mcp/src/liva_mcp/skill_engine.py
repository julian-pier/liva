"""Persistent vertical slice for versioned LIVA conversational skills."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import secrets
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .memory_v2_service import MemoryV2Service, MemoryV2ToolError


SKILL_CONTRACT_REVISION = "skill-engine-2"
LOCAL_READ_MODES = {"skill_registry", "skill_load", "skill_session"}
LOCAL_COMMANDS: dict[str, dict[str, Any]] = {
    "start": {"required": ["skill_id", "topic", "idempotency_key"]},
    "record_turn": {"required": ["skill_session_id", "expected_revision", "question", "answer", "idempotency_key"]},
    "checkpoint": {"required": ["skill_session_id", "expected_revision", "idempotency_key"]},
    "pause": {"required": ["skill_session_id", "expected_revision", "idempotency_key"]},
    "resume": {"required": ["idempotency_key"], "required_any": [["skill_session_id"], ["query"]]},
    "stop": {"required": ["skill_session_id", "expected_revision", "handoff", "idempotency_key"], "conditional": {"completion": "required for sessions with three or more turns"}},
}


class SkillEngineError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    original_path: str
    overlay_path: str
    adapters: tuple[str, ...]
    triggers: tuple[str, ...]


SKILLS = {
    "grill-me": SkillDefinition(
        skill_id="grill-me",
        name="LIVA Grill Me",
        original_path="procedures/skills/liva-grill-me/SKILL.md",
        overlay_path="procedures/skills/liva-grill-me/LIVA-OVERLAY.md",
        adapters=("chatgpt_mcp", "codex"),
        triggers=("Grill Me", "#GrillMe", "Grill meine Idee"),
    )
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _session_id() -> str:
    stamp = int(datetime.now(UTC).timestamp() * 1000)
    return f"ses_{stamp:013x}{secrets.token_hex(8)}"


def _bounded_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
        raise SkillEngineError("INVALID_SKILL_ARGUMENTS", f"{field} must be a non-empty string up to {maximum} characters.")
    return value.strip()


def _string_list(value: Any, field: str, maximum: int = 30) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise SkillEngineError("INVALID_SKILL_ARGUMENTS", f"{field} must be a bounded string list.")
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, field, 2000, required=True)
        if text not in result:
            result.append(text)
    return result


class SkillEngine:
    """Loads canonical skills and persists resumable session state."""

    def __init__(self, config: Config):
        self.config = config
        self.vault = config.memory_vault_path().resolve()
        self.runtime = config.memory_runtime_path().resolve()
        self.db_path = self.runtime / "skill-engine.sqlite"

    @staticmethod
    def capability_contract() -> dict[str, Any]:
        return {
            "contract_revision": SKILL_CONTRACT_REVISION,
            "read_modes": sorted(LOCAL_READ_MODES),
            "commands": {"skill": LOCAL_COMMANDS},
        }

    def _connect(self) -> sqlite3.Connection:
        self.runtime.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_sessions (
                  session_id TEXT PRIMARY KEY,
                  skill_id TEXT NOT NULL,
                  skill_version TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','paused','completed')),
                  topic TEXT NOT NULL,
                  topic_type TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  turn_count INTEGER NOT NULL,
                  checkpoint_count INTEGER NOT NULL,
                  last_checkpoint_turn INTEGER NOT NULL,
                  pending_checkpoint INTEGER NOT NULL DEFAULT 0,
                  state_json TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  handoff_json TEXT,
                  archive_source_id TEXT,
                  origin_client_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS skill_sessions_lookup
                  ON skill_sessions(skill_id,status,updated_at DESC);
                CREATE TABLE IF NOT EXISTS skill_checkpoints (
                  checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL REFERENCES skill_sessions(session_id),
                  revision INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  source_path TEXT NOT NULL,
                  source_sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(session_id,revision,kind)
                );
                CREATE TABLE IF NOT EXISTS skill_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL REFERENCES skill_sessions(session_id),
                  revision INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  turn_number INTEGER,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(session_id,revision,event_type)
                );
                CREATE TABLE IF NOT EXISTS skill_idempotency (
                  idempotency_key TEXT PRIMARY KEY,
                  operation TEXT NOT NULL,
                  request_sha256 TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )

    def _definition(self, skill_id: Any) -> SkillDefinition:
        if not isinstance(skill_id, str) or skill_id not in SKILLS:
            raise SkillEngineError("UNKNOWN_SKILL", "The requested skill is not in the active LIVA registry.")
        return SKILLS[skill_id]

    def load(self, skill_id: str) -> dict[str, Any]:
        definition = self._definition(skill_id)
        original = self.vault / definition.original_path
        overlay = self.vault / definition.overlay_path
        try:
            methodology = original.read_text(encoding="utf-8")
            governance = overlay.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillEngineError("SKILL_DEFINITION_UNAVAILABLE", "The canonical skill definition or LIVA overlay is unavailable.") from exc
        if not methodology.strip() or not governance.strip():
            raise SkillEngineError("SKILL_DEFINITION_INVALID", "The canonical skill definition or LIVA overlay is empty.")
        version = hashlib.sha256(methodology.encode("utf-8")).hexdigest()
        overlay_version = hashlib.sha256(governance.encode("utf-8")).hexdigest()
        return {
            "skill_id": definition.skill_id,
            "name": definition.name,
            "version": version,
            "overlay_version": overlay_version,
            "original_path": definition.original_path,
            "overlay_path": definition.overlay_path,
            "adapters": list(definition.adapters),
            "triggers": list(definition.triggers),
            "methodology": methodology,
            "liva_overlay": governance,
            "authority": ["user_request", "liva_governance", "original_methodology"],
        }

    def registry(self) -> dict[str, Any]:
        entries = []
        for skill_id in SKILLS:
            loaded = self.load(skill_id)
            entries.append({key: loaded[key] for key in ("skill_id", "name", "version", "overlay_version", "original_path", "overlay_path", "adapters", "triggers")})
        return {"registry_revision": _hash(entries), "skills": entries, "manual_updates_only": True}

    @staticmethod
    def _state(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["state_json"])

    @staticmethod
    def _public_session(row: sqlite3.Row, *, include_turns: bool = True) -> dict[str, Any]:
        state = json.loads(row["state_json"])
        result = {
            "skill_session_id": row["session_id"],
            "skill_id": row["skill_id"],
            "skill_version": row["skill_version"],
            "status": row["status"],
            "topic": row["topic"],
            "topic_type": row["topic_type"],
            "revision": row["revision"],
            "turn_count": row["turn_count"],
            "checkpoint_count": row["checkpoint_count"],
            "pending_checkpoint": bool(row["pending_checkpoint"]),
            "decisions": state.get("decisions", []),
            "open_questions": state.get("open_questions", []),
            "progress": state.get("progress", ""),
            "updated_at": row["updated_at"],
        }
        if include_turns:
            result["turns"] = state.get("turns", [])
        if row["handoff_json"]:
            result["handoff"] = json.loads(row["handoff_json"])
        if row["archive_source_id"]:
            result["archive_source_id"] = row["archive_source_id"]
        return result

    def _row(self, session_id: str, db: sqlite3.Connection | None = None) -> sqlite3.Row:
        own = db is None
        connection = db or self._connect()
        try:
            row = connection.execute("SELECT * FROM skill_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                raise SkillEngineError("SESSION_NOT_FOUND", "No matching skill session exists.")
            return row
        finally:
            if own:
                connection.close()

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, path)
        finally:
            Path(pending).unlink(missing_ok=True)
        readback = path.read_bytes()
        if readback != raw:
            raise SkillEngineError("SESSION_PERSISTENCE_FAILED", "The session file could not be verified after writing.", details={"retryable": True})
        return hashlib.sha256(raw).hexdigest()

    def _active_path(self, session_id: str) -> Path:
        return self.vault / "sessions" / "active" / f"{session_id}.json"

    def _archive_path(self, session_id: str) -> Path:
        return self.vault / "sessions" / "archive" / f"{session_id}.json"

    def _write_active(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._public_session(row)
        sha = self._atomic_json(self._active_path(row["session_id"]), payload)
        return {"path": str(self._active_path(row["session_id"]).relative_to(self.vault)), "sha256": sha, "verified": True}

    @staticmethod
    def _idem_replay(db: sqlite3.Connection, key: str, operation: str, request_hash: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM skill_idempotency WHERE idempotency_key=?", (key,)).fetchone()
        if not row:
            return None
        if row["operation"] != operation or row["request_sha256"] != request_hash:
            raise SkillEngineError("IDEMPOTENCY_CONFLICT", "The idempotency key was already used for a different skill operation.")
        result = json.loads(row["response_json"])
        result["replayed"] = True
        result["changed"] = False
        return result

    @staticmethod
    def _store_idem(db: sqlite3.Connection, key: str, operation: str, request_hash: str, response: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO skill_idempotency VALUES (?,?,?,?,?)",
            (key, operation, request_hash, json.dumps(response, ensure_ascii=False, sort_keys=True), _now()),
        )

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        return _bounded_text(payload.get("idempotency_key"), "idempotency_key", 160, required=True)

    @staticmethod
    def _revision(payload: dict[str, Any]) -> int:
        revision = payload.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "expected_revision must be a positive integer.")
        return revision

    @staticmethod
    def _assert_revision(row: sqlite3.Row, expected: int) -> None:
        if row["revision"] != expected:
            raise SkillEngineError(
                "STALE_SKILL_SESSION",
                "The skill session changed; read its current status before retrying.",
                details={"current_revision": row["revision"], "retryable": True},
            )

    def _memory_context(self, topic: str, queries: list[str]) -> dict[str, Any]:
        selected = list(dict.fromkeys([topic, *queries]))[:4]
        try:
            context = MemoryV2Service(self.config).context({"queries": selected, "layers": "both", "limit": 6, "max_chars": 9000})
            return {"status": "available", "queries": selected, "context": context}
        except Exception:
            return {"status": "unavailable", "queries": selected, "context": None, "warning": "Memory V2 context was unavailable; continue without claiming it was loaded."}

    def start(self, payload: dict[str, Any], *, client_id: str | None = None) -> dict[str, Any]:
        allowed = {"skill_id", "topic", "topic_type", "context_queries", "idempotency_key"}
        if set(payload) - allowed:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported start fields were provided.")
        key = self._key(payload)
        request_hash = _hash(payload)
        self.initialize()
        with self._connect() as db:
            replay = self._idem_replay(db, key, "start", request_hash)
            if replay:
                return replay
        loaded = self.load(payload.get("skill_id"))
        topic = _bounded_text(payload.get("topic"), "topic", 2000, required=True)
        topic_type = payload.get("topic_type", "general")
        if topic_type not in {"technical", "personal", "sport", "general"}:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "topic_type must be technical, personal, sport, or general.")
        queries = _string_list(payload.get("context_queries"), "context_queries", 4)
        memory_context = self._memory_context(topic, queries)
        now = _now()
        session_id = _session_id()
        state = {"turns": [], "decisions": [], "open_questions": [], "progress": "Session started", "skill_snapshot": loaded}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._idem_replay(db, key, "start", request_hash)
            if replay:
                db.rollback()
                return replay
            db.execute(
                "INSERT INTO skill_sessions(session_id,skill_id,skill_version,status,topic,topic_type,revision,turn_count,checkpoint_count,last_checkpoint_turn,pending_checkpoint,state_json,context_json,handoff_json,archive_source_id,origin_client_id,created_at,updated_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, loaded["skill_id"], loaded["version"], "active", topic, topic_type, 1, 0, 0, 0, 0,
                 json.dumps(state, ensure_ascii=False, sort_keys=True), json.dumps(memory_context, ensure_ascii=False, sort_keys=True),
                None, None, client_id, now, now, None),
            )
            db.execute(
                "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (session_id, 1, "started", None, json.dumps({"topic": topic, "topic_type": topic_type}, ensure_ascii=False, sort_keys=True), now),
            )
            row = self._row(session_id, db)
            persistence = self._write_active(row)
            response = {
                "skill_session_id": session_id,
                "status": "active",
                "revision": 1,
                "skill": loaded,
                "memory_context": memory_context,
                "persistence": persistence,
                "turn_contract": self._turn_contract(topic_type),
                "replayed": False,
                "changed": True,
                "production_write": True,
            }
            self._store_idem(db, key, "start", request_hash, response)
            db.commit()
        return response

    @staticmethod
    def _turn_contract(topic_type: str) -> dict[str, Any]:
        return {
            "next_action": "ask_one_question",
            "question_count": 1,
            "style": "structured decisions are allowed" if topic_type == "technical" else "natural open question",
            "derive_from_latest_answer": True,
            "commands": {"checkpoint": "/checkpoint", "stop": "/stop", "return": "Zurück zum Grilling"},
        }

    def _latest_checkpoint(self, session_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM skill_checkpoints WHERE session_id=? ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()

    def _checkpoint_content(
        self,
        row: sqlite3.Row,
        kind: str,
        handoff: dict[str, Any] | None = None,
        *,
        full_snapshot: bool = False,
    ) -> tuple[str, str, int, int]:
        state = self._state(row)
        from_turn = 1 if full_snapshot else row["last_checkpoint_turn"] + 1
        to_turn = row["turn_count"]
        turns = [turn for turn in state.get("turns", []) if from_turn <= turn["turn"] <= to_turn]
        new_decisions = list(dict.fromkeys(decision for turn in turns for decision in turn.get("decisions", [])))
        user_lines = [f"Topic: {row['topic']}"]
        for turn in turns:
            user_lines.append(f"Turn {turn['turn']} question: {turn['question']}\nUser answer {turn['turn']}: {turn['answer']}")
        direct = "\n\n".join(user_lines)
        previous = self._latest_checkpoint(row["session_id"])
        synthesis = json.dumps({
            "session_id": row["session_id"], "skill_id": row["skill_id"], "skill_version": row["skill_version"],
            "revision": row["revision"], "kind": kind, "new_decisions": new_decisions,
            "turn_range": {"from": from_turn if turns else None, "to": to_turn if turns else None, "full_snapshot": full_snapshot},
            "previous_checkpoint_source_id": previous["source_id"] if previous else None,
            "open_questions": state.get("open_questions", []), "progress": state.get("progress", ""), "handoff": handoff,
        }, ensure_ascii=False, sort_keys=True, indent=2)
        return direct, synthesis, from_turn if turns else 0, to_turn if turns else 0

    def _persist_checkpoint(self, row: sqlite3.Row, kind: str, *, handoff: dict[str, Any] | None = None, full_snapshot: bool = False) -> dict[str, Any]:
        direct, synthesis, from_turn, to_turn = self._checkpoint_content(row, kind, handoff, full_snapshot=full_snapshot)
        service = MemoryV2Service(self.config)
        key = f"skill-checkpoint-{row['session_id']}-{row['revision']}-{kind}"
        captured = service.store.capture_chat_memory(
            direct_user_context=direct,
            assistant_synthesis=synthesis,
            temporal_scope="historical",
            topics=["skill-session", row["skill_id"]],
            evidence_types=["direct_user_statement", "assistant_summary"],
            idempotency_key=key,
            compile_policy="raw_only",
            title=f"{row['skill_id']} session checkpoint",
        )
        readback = service.store.memory_source(captured["source_id"], 0, 1)
        if readback.get("sha256") != captured.get("sha256"):
            raise SkillEngineError("CHECKPOINT_VERIFICATION_FAILED", "The checkpoint source failed readback verification.", details={"retryable": True})
        return {"kind": kind, "source_id": captured["source_id"], "path": captured["path"], "sha256": captured["sha256"], "verified": True,
                "turn_range": {"from": from_turn or None, "to": to_turn or None, "full_snapshot": full_snapshot},
                "replayed": bool(captured.get("duplicate"))}

    def record_turn(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "expected_revision", "question", "answer", "decisions", "open_questions", "progress", "milestone", "idempotency_key"}
        if set(payload) - allowed:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported record_turn fields were provided.")
        session_id = _bounded_text(payload.get("skill_session_id"), "skill_session_id", 80, required=True)
        expected = self._revision(payload)
        question = _bounded_text(payload.get("question"), "question", 4000, required=True)
        answer = _bounded_text(payload.get("answer"), "answer", 12000, required=True)
        decisions = _string_list(payload.get("decisions"), "decisions")
        open_questions = _string_list(payload.get("open_questions"), "open_questions")
        progress = _bounded_text(payload.get("progress"), "progress", 4000)
        milestone = payload.get("milestone", False)
        if not isinstance(milestone, bool):
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "milestone must be boolean.")
        key = self._key(payload); request_hash = _hash(payload); self.initialize()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._idem_replay(db, key, "record_turn", request_hash)
            if replay:
                db.rollback(); return replay
            row = self._row(session_id, db); self._assert_revision(row, expected)
            if row["status"] != "active":
                raise SkillEngineError("SESSION_NOT_ACTIVE", "Resume the skill session before recording another answer.")
            state = self._state(row); now = _now(); turn = row["turn_count"] + 1; revision = row["revision"] + 1
            state.setdefault("turns", []).append({"turn": turn, "question": question, "answer": answer, "decisions": decisions, "at": now})
            state["decisions"] = list(dict.fromkeys([*state.get("decisions", []), *decisions]))
            state["open_questions"] = open_questions
            if progress: state["progress"] = progress
            auto = milestone or turn - row["last_checkpoint_turn"] >= 3
            db.execute("UPDATE skill_sessions SET revision=?,turn_count=?,state_json=?,updated_at=?,pending_checkpoint=? WHERE session_id=?",
                       (revision, turn, json.dumps(state, ensure_ascii=False, sort_keys=True), now, int(auto), session_id))
            db.execute(
                "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (session_id, revision, "turn_recorded", turn, json.dumps({"question": question, "answer": answer, "decisions": decisions, "open_questions": open_questions, "progress": progress}, ensure_ascii=False, sort_keys=True), now),
            )
            if decisions:
                db.execute(
                    "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (session_id, revision, "decisions_recorded", turn, json.dumps({"decisions": decisions}, ensure_ascii=False, sort_keys=True), now),
                )
            updated = self._row(session_id, db)
            checkpoint = None; warning = None
            try:
                persistence = self._write_active(updated)
            except Exception:
                persistence = {"path": str(self._active_path(session_id).relative_to(self.vault)), "verified": False, "retryable": True}
                warning = {"code": "SESSION_MIRROR_PENDING", "message": "The answer is durable in the session database, but its Vault mirror is pending.", "retryable": True}
            if auto:
                try:
                    checkpoint = self._persist_checkpoint(updated, "automatic")
                    db.execute("INSERT OR IGNORE INTO skill_checkpoints(session_id,revision,kind,source_id,source_path,source_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                               (session_id, revision, "automatic", checkpoint["source_id"], checkpoint["path"], checkpoint["sha256"], now))
                    db.execute("UPDATE skill_sessions SET checkpoint_count=checkpoint_count+1,last_checkpoint_turn=?,pending_checkpoint=0 WHERE session_id=?", (turn, session_id))
                except Exception:
                    warning = {"code": "AUTO_CHECKPOINT_PENDING", "message": "The answer is stored in the persistent session, but its Memory V2 checkpoint is pending.", "retryable": True}
            updated = self._row(session_id, db)
            response = {**self._public_session(updated, include_turns=False), "persistence": persistence, "checkpoint": checkpoint,
                        "warning": warning, "turn_contract": self._turn_contract(row["topic_type"]), "replayed": False, "changed": True, "production_write": True}
            self._store_idem(db, key, "record_turn", request_hash, response); db.commit()
        return response

    def checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "expected_revision", "reason", "idempotency_key"}
        if set(payload) - allowed:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported checkpoint fields were provided.")
        session_id = _bounded_text(payload.get("skill_session_id"), "skill_session_id", 80, required=True)
        expected = self._revision(payload); _bounded_text(payload.get("reason"), "reason", 1000)
        key = self._key(payload); request_hash = _hash(payload); self.initialize()
        with self._connect() as db:
            replay = self._idem_replay(db, key, "checkpoint", request_hash)
            if replay: return replay
            row = self._row(session_id, db); self._assert_revision(row, expected)
            if row["status"] == "completed": raise SkillEngineError("SESSION_COMPLETED", "A completed session cannot receive another checkpoint.")
            latest = db.execute(
                "SELECT * FROM skill_checkpoints WHERE session_id=? ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if latest and row["last_checkpoint_turn"] >= row["turn_count"]:
                response = {**self._public_session(row, include_turns=False), "checkpoint": {
                    "kind": latest["kind"], "source_id": latest["source_id"], "path": latest["source_path"],
                    "sha256": latest["source_sha256"], "verified": True, "replayed": True,
                    "turn_range": {"from": None, "to": None, "full_snapshot": False},
                }, "persistence": self._write_active(row), "no_new_information": True,
                    "replayed": False, "changed": False, "production_write": False}
                self._store_idem(db, key, "checkpoint", request_hash, response)
                db.commit()
                return response
        try:
            checkpoint = self._persist_checkpoint(row, "manual")
        except Exception as exc:
            raise SkillEngineError("CHECKPOINT_FAILED", "The checkpoint was not verified and must not be reported as saved.", details={"retryable": True}) from exc
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE"); replay = self._idem_replay(db, key, "checkpoint", request_hash)
            if replay: db.rollback(); return replay
            current = self._row(session_id, db); self._assert_revision(current, expected)
            revision = current["revision"] + 1
            db.execute("INSERT OR IGNORE INTO skill_checkpoints(session_id,revision,kind,source_id,source_path,source_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                       (session_id, revision, "manual", checkpoint["source_id"], checkpoint["path"], checkpoint["sha256"], now))
            db.execute("UPDATE skill_sessions SET revision=?,checkpoint_count=checkpoint_count+1,last_checkpoint_turn=turn_count,pending_checkpoint=0,updated_at=? WHERE session_id=?", (revision, now, session_id))
            updated = self._row(session_id, db); persistence = self._write_active(updated)
            response = {**self._public_session(updated, include_turns=False), "checkpoint": checkpoint, "persistence": persistence,
                        "replayed": False, "changed": True, "production_write": True}
            self._store_idem(db, key, "checkpoint", request_hash, response); db.commit(); return response

    def pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "expected_revision", "reason", "subskill", "idempotency_key"}
        if set(payload) - allowed: raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported pause fields were provided.")
        session_id = _bounded_text(payload.get("skill_session_id"), "skill_session_id", 80, required=True)
        expected = self._revision(payload); reason = _bounded_text(payload.get("reason"), "reason", 1000)
        subskill = _bounded_text(payload.get("subskill"), "subskill", 100)
        key = self._key(payload); request_hash = _hash(payload); self.initialize(); now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE"); replay = self._idem_replay(db, key, "pause", request_hash)
            if replay: db.rollback(); return replay
            row = self._row(session_id, db); self._assert_revision(row, expected)
            if row["status"] != "active": raise SkillEngineError("SESSION_NOT_ACTIVE", "Only an active session can be paused.")
            state = self._state(row)
            revision = row["revision"] + 1
            db.execute("UPDATE skill_sessions SET status='paused',revision=?,state_json=?,updated_at=? WHERE session_id=?", (revision, json.dumps(state, ensure_ascii=False, sort_keys=True), now, session_id))
            db.execute(
                "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (session_id, revision, "paused", None, json.dumps({"reason": reason, "subskill": subskill}, ensure_ascii=False, sort_keys=True), now),
            )
            updated = self._row(session_id, db); warning = None
            try:
                persistence = self._write_active(updated)
            except Exception:
                persistence = {"path": str(self._active_path(session_id).relative_to(self.vault)), "verified": False, "retryable": True}
                warning = {"code": "SESSION_MIRROR_PENDING", "message": "The paused state is durable in the session database, but its Vault mirror is pending.", "retryable": True}
            checkpoint = None
            try:
                checkpoint = self._persist_checkpoint(updated, "pause")
                db.execute("INSERT OR IGNORE INTO skill_checkpoints(session_id,revision,kind,source_id,source_path,source_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                           (session_id, revision, "pause", checkpoint["source_id"], checkpoint["path"], checkpoint["sha256"], now))
                db.execute("UPDATE skill_sessions SET checkpoint_count=checkpoint_count+1,last_checkpoint_turn=turn_count,pending_checkpoint=0 WHERE session_id=?", (session_id,))
            except Exception:
                db.execute("UPDATE skill_sessions SET pending_checkpoint=1 WHERE session_id=?", (session_id,))
                warning = {"code": "PAUSE_CHECKPOINT_PENDING", "message": "The pause is durable, but its Memory V2 checkpoint is pending.", "retryable": True}
            updated = self._row(session_id, db)
            response = {**self._public_session(updated, include_turns=False), "persistence": persistence, "resume_phrase": "Zurück zum Grilling",
                        "checkpoint": checkpoint, "warning": warning, "replayed": False, "changed": True, "production_write": True}
            self._store_idem(db, key, "pause", request_hash, response); db.commit(); return response

    def _resolve(self, payload: dict[str, Any]) -> sqlite3.Row:
        session_id = payload.get("skill_session_id")
        if session_id:
            return self._row(_bounded_text(session_id, "skill_session_id", 80, required=True))
        query = _bounded_text(payload.get("query"), "query", 1000, required=True).casefold()
        self.initialize()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM skill_sessions WHERE status IN ('active','paused') AND (lower(topic) LIKE ? OR lower(skill_id) LIKE ?) ORDER BY updated_at DESC LIMIT 10", (f"%{query}%", f"%{query}%")).fetchall()
        if not rows: raise SkillEngineError("SESSION_NOT_FOUND", "No active or paused skill session matches the query.")
        if len(rows) > 1:
            raise SkillEngineError("AMBIGUOUS_SKILL_SESSION", "Several sessions match; choose one explicit skill_session_id.", details={"candidates": [self._public_session(row, include_turns=False) for row in rows]})
        return rows[0]

    def resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "query", "idempotency_key"}
        if set(payload) - allowed: raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported resume fields were provided.")
        key = self._key(payload); request_hash = _hash(payload); self.initialize()
        with self._connect() as db:
            replay = self._idem_replay(db, key, "resume", request_hash)
            if replay: return replay
        resolved = self._resolve(payload); session_id = resolved["session_id"]
        if resolved["status"] == "completed": raise SkillEngineError("SESSION_COMPLETED", "Completed sessions are read-only and cannot be resumed.")
        resolved_state = self._state(resolved)
        loaded = resolved_state.get("skill_snapshot")
        if not isinstance(loaded, dict) or loaded.get("version") != resolved["skill_version"]:
            raise SkillEngineError("PINNED_SKILL_UNAVAILABLE", "The version-pinned skill snapshot is unavailable; the session was not resumed.")
        current = self.load(resolved["skill_id"])
        if current["version"] != resolved["skill_version"]:
            loaded = dict(loaded)
            loaded["version_notice"] = "This session remains pinned to its original skill version."
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE"); replay = self._idem_replay(db, key, "resume", request_hash)
            if replay: db.rollback(); return replay
            row = self._row(session_id, db); state = self._state(row)
            revision = row["revision"] + 1
            db.execute("UPDATE skill_sessions SET status='active',revision=?,state_json=?,updated_at=? WHERE session_id=?", (revision, json.dumps(state, ensure_ascii=False, sort_keys=True), now, session_id))
            db.execute(
                "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (session_id, revision, "resumed", None, "{}", now),
            )
            updated = self._row(session_id, db); persistence = self._write_active(updated)
            response = {**self._public_session(updated), "skill": loaded, "persistence": persistence,
                        "turn_contract": self._turn_contract(row["topic_type"]), "replayed": False, "changed": True, "production_write": True}
            self._store_idem(db, key, "resume", request_hash, response); db.commit(); return response

    def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "expected_revision", "handoff", "completion", "durable_memory", "idempotency_key"}
        if set(payload) - allowed: raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported stop fields were provided.")
        session_id = _bounded_text(payload.get("skill_session_id"), "skill_session_id", 80, required=True)
        expected = self._revision(payload); key = self._key(payload); request_hash = _hash(payload)
        handoff = payload.get("handoff")
        if not isinstance(handoff, dict) or set(handoff) - {"main_idea", "possible_implementations", "open_points", "current_status"}:
            raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "handoff must contain only the documented summary fields.")
        main_idea = _bounded_text(handoff.get("main_idea"), "handoff.main_idea", 6000, required=True)
        normalized_handoff = {"main_idea": main_idea, "possible_implementations": _string_list(handoff.get("possible_implementations"), "possible_implementations"),
                              "open_points": _string_list(handoff.get("open_points"), "open_points"), "current_status": _bounded_text(handoff.get("current_status"), "current_status", 3000, required=True)}
        self.initialize()
        with self._connect() as db:
            replay = self._idem_replay(db, key, "stop", request_hash)
            if replay: return replay
            row = self._row(session_id, db); self._assert_revision(row, expected)
            if row["status"] == "completed": raise SkillEngineError("SESSION_COMPLETED", "The session is already completed.")
        completion = payload.get("completion")
        event_required = row["turn_count"] >= 3
        if completion is None and event_required:
            raise SkillEngineError(
                "SEMANTIC_COMPLETION_REQUIRED",
                "A substantial skill session requires an explicit semantic completion and a standalone Wiki event.",
                details={"retryable": True, "completion_status": {
                    "session_saved": True, "final_checkpoint_confirmed": False, "wiki_gardening": "not_started",
                    "event_file": {"required": True, "written": False, "read_back": False, "path": None},
                    "archived": False, "complete": False,
                }},
            )
        if completion is not None:
            if not isinstance(completion, dict) or set(completion) - {"classification", "rationale"}:
                raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "completion must contain only classification and rationale.")
            classification = completion.get("classification")
            if classification not in {"substantial", "minor"}:
                raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "completion.classification must be substantial or minor.")
            _bounded_text(completion.get("rationale"), "completion.rationale", 2000, required=True)
            event_required = row["turn_count"] >= 3 or classification == "substantial"
        try:
            final_checkpoint = self._persist_checkpoint(row, "final", handoff=normalized_handoff)
        except Exception as exc:
            raise SkillEngineError("FINAL_PERSISTENCE_FAILED", "The final checkpoint was not verified; the session remains unarchived.", details={"retryable": True, "completion_status": {
                "session_saved": True, "final_checkpoint_confirmed": False, "wiki_gardening": "not_started",
                "event_file": {"required": event_required, "written": False, "read_back": False, "path": None},
                "archived": False, "complete": False,
            }}) from exc
        memory_result = None
        durable = payload.get("durable_memory")
        event_change = None
        if event_required:
            if isinstance(durable, dict):
                event_change = next((change for change in durable.get("changes") or [] if isinstance(change, dict) and change.get("action") == "create_page" and change.get("note_type") == "event"), None)
            if event_change is None:
                raise SkillEngineError("WIKI_EVENT_REQUIRED", "A substantial skill session requires a standalone event create_page change; the session remains resumable.", details={"retryable": True, "completion_status": {
                    "session_saved": True, "final_checkpoint_confirmed": True, "wiki_gardening": "not_started",
                    "event_file": {"required": True, "written": False, "read_back": False, "path": None},
                    "archived": False, "complete": False,
                }})
        if durable is not None:
            if not isinstance(durable, dict) or durable.get("confirmed") is not True:
                raise SkillEngineError("UNCONFIRMED_DURABLE_MEMORY", "Durable Wiki integration requires confirmed=true and source-grounded changes.")
            try:
                memory_result = MemoryV2Service(self.config).finish({
                    "source_id": final_checkpoint["source_id"], "claims": durable.get("claims") or [],
                    "route": durable.get("route", "living_wiki"), "changes": durable.get("changes") or [],
                    "reason": durable.get("reason", "Confirmed result of a completed skill session"),
                })
            except MemoryV2ToolError as exc:
                raise SkillEngineError("DURABLE_MEMORY_FAILED", "The durable Memory V2 integration failed; the session remains unarchived.", details={"memory_code": exc.code, "retryable": bool(exc.details.get("retryable")), "completion_status": {
                    "session_saved": True, "final_checkpoint_confirmed": True, "wiki_gardening": "failed",
                    "event_file": {"required": event_required, "written": False, "read_back": False, "path": event_change.get("path") if event_change else None},
                    "archived": False, "complete": False,
                }}) from exc
            if not memory_result.get("reporting", {}).get("may_claim_knowledge_saved") and durable.get("changes"):
                raise SkillEngineError("DURABLE_MEMORY_UNVERIFIED", "Memory V2 did not authorize claiming the Wiki update; the session remains unarchived.", details={"retryable": True, "completion_status": {
                    "session_saved": True, "final_checkpoint_confirmed": True, "wiki_gardening": "failed",
                    "event_file": {"required": event_required, "written": False, "read_back": False, "path": event_change.get("path") if event_change else None},
                    "archived": False, "complete": False,
                }})
        event_path = event_change.get("path") if event_change else None
        event_read_back = bool(event_path and (self.vault / event_path).is_file() and memory_result and memory_result.get("verification", {}).get("knowledge_readback"))
        if event_required and not event_read_back:
            raise SkillEngineError("WIKI_EVENT_UNVERIFIED", "The required Wiki event could not be read back; the session remains unarchived.", details={"retryable": True, "completion_status": {
                "session_saved": True, "final_checkpoint_confirmed": True, "wiki_gardening": "failed",
                "event_file": {"required": True, "written": bool(event_path and (self.vault / event_path).is_file()), "read_back": False, "path": event_path},
                "archived": False, "complete": False,
            }})
        wiki_status = "succeeded" if memory_result and memory_result.get("reporting", {}).get("may_claim_knowledge_saved") else "skipped_minor"
        completion_status = {
            "session_saved": True, "final_checkpoint_confirmed": True, "wiki_gardening": wiki_status,
            "event_file": {"required": event_required, "written": bool(event_path), "read_back": event_read_back, "path": event_path},
            "archived": True, "complete": True,
        }
        now = _now(); archive_payload = {**self._public_session(row), "status": "completed", "completed_at": now,
                                         "handoff": normalized_handoff, "final_checkpoint": final_checkpoint, "durable_memory": memory_result,
                                         "completion_status": completion_status}
        archive_path = self._archive_path(session_id)
        archive_sha = self._atomic_json(archive_path, archive_payload)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE"); replay = self._idem_replay(db, key, "stop", request_hash)
                if replay: db.rollback(); return replay
                current = self._row(session_id, db); self._assert_revision(current, expected)
                revision = current["revision"] + 1
                db.execute("INSERT OR IGNORE INTO skill_checkpoints(session_id,revision,kind,source_id,source_path,source_sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                           (session_id, revision, "final", final_checkpoint["source_id"], final_checkpoint["path"], final_checkpoint["sha256"], now))
                db.execute("UPDATE skill_sessions SET status='completed',revision=?,checkpoint_count=checkpoint_count+1,last_checkpoint_turn=turn_count,pending_checkpoint=0,handoff_json=?,archive_source_id=?,updated_at=?,completed_at=? WHERE session_id=?",
                           (revision, json.dumps(normalized_handoff, ensure_ascii=False, sort_keys=True), final_checkpoint["source_id"], now, now, session_id))
                db.execute(
                    "INSERT INTO skill_events(session_id,revision,event_type,turn_number,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (session_id, revision, "completed", None, json.dumps({"handoff": normalized_handoff, "completion_status": completion_status}, ensure_ascii=False, sort_keys=True), now),
                )
                updated = self._row(session_id, db)
                response = {**self._public_session(updated, include_turns=False), "final_checkpoint": final_checkpoint,
                            "archive": {"path": str(archive_path.relative_to(self.vault)), "sha256": archive_sha, "verified": True},
                            "durable_memory": memory_result, "completion_status": completion_status,
                            "may_claim_completed": completion_status["complete"], "replayed": False, "changed": True, "production_write": True}
                self._store_idem(db, key, "stop", request_hash, response); db.commit()
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        self._active_path(session_id).unlink(missing_ok=True)
        return response

    def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"skill_session_id", "query", "include_completed"}
        if set(payload) - allowed: raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "Unsupported session query fields were provided.")
        if payload.get("skill_session_id") or payload.get("query"):
            row = self._resolve(payload)
            return {"session": self._public_session(row), "read_only": True}
        include_completed = payload.get("include_completed", False)
        if not isinstance(include_completed, bool): raise SkillEngineError("INVALID_SKILL_ARGUMENTS", "include_completed must be boolean.")
        self.initialize()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM skill_sessions WHERE status!='completed' OR ? ORDER BY updated_at DESC LIMIT 20", (int(include_completed),)).fetchall()
        return {"sessions": [self._public_session(row, include_turns=False) for row in rows], "read_only": True}

    def active_for_client(self, client_id: str | None) -> list[dict[str, Any]]:
        if not client_id:
            return []
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM skill_sessions WHERE origin_client_id=? AND status='active' ORDER BY updated_at DESC",
                (client_id,),
            ).fetchall()
        return [self._public_session(row, include_turns=False) for row in rows]

    def read(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        if mode == "skill_registry": return self.registry()
        if mode == "skill_load": return self.load(_bounded_text(payload.get("skill_id"), "skill_id", 80, required=True))
        if mode == "skill_session": return self.status(payload)
        raise SkillEngineError("UNKNOWN_SKILL_READ", "The requested skill read mode is not available.")

    def act(self, command: str, payload: dict[str, Any], *, client_id: str | None = None) -> dict[str, Any]:
        if command not in LOCAL_COMMANDS:
            raise SkillEngineError("UNKNOWN_SKILL_ACTION", "The requested skill action is not available.")
        self.runtime.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime / "skill-engine.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if command == "start": return self.start(payload, client_id=client_id)
                if command == "record_turn": return self.record_turn(payload)
                if command == "checkpoint": return self.checkpoint(payload)
                if command == "pause": return self.pause(payload)
                if command == "resume": return self.resume(payload)
                return self.stop(payload)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
