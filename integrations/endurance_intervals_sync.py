from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import get_plans_db
from integrations.intervals_client import IntervalsClient, IntervalsClientError
from analysis.run_pace_model import execution_model_quality, execution_pace_for_hr_percent, load_current_hr_pace_model, rpe_to_hr_percent
from plans.endurance_semantics import expanded_leaf_steps, step_tree, summarize_steps

log = logging.getLogger(__name__)
PROVIDER = "intervals"
EXTERNAL_PREFIX = "liva:endurance:"
RETRY_DELAYS = (60, 300, 900, 3600)
SERIALIZER_VERSION = "intervals_dsl_v3"


class WorkoutSerializationError(ValueError):
    code = "workout_serialization_failed"


class MissingResolvedTargetError(WorkoutSerializationError):
    code = "missing_resolved_target"


class MissingExecutionPaceError(WorkoutSerializationError):
    code = "missing_execution_pace"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def external_id_for_session(session_id: str) -> str:
    return f"{EXTERNAL_PREFIX}{session_id}"


def _duration(step: dict[str, Any]) -> str:
    seconds = int(step.get("duration_s") or 0)
    distance = int(step.get("distance_m") or 0)
    if seconds > 0:
        minutes, remainder = divmod(seconds, 60)
        return (f"{minutes}m" if minutes else "") + (f"{remainder}s" if remainder else "")
    if distance > 0:
        return f"{distance // 1000}km" if distance % 1000 == 0 else f"{distance}mtr"
    raise WorkoutSerializationError("Workout-Schritt hat weder positive Dauer noch positive Distanz.")


def _pace(seconds: float) -> str:
    value = max(1, round(float(seconds)))
    return f"{value // 60}:{value % 60:02d}"


def _step_text(step: dict[str, Any], kind: str) -> str:
    base = {"warmup": "Einlaufen", "cooldown": "Auslaufen", "recovery": "Pause", "work": "Belastung", "stride": "Steigerung"}.get(kind, "Locker")
    note = " ".join(str(step.get("notes") or "").split())
    # Intervals interprets clock-shaped tokens inside otherwise free text as a
    # second duration/pace declaration and truncates the instruction there.
    # A mathematical ratio colon looks identical on the watch but remains text.
    note = re.sub(r"(?<=\d):(?=\d{2}\b)", "∶", note)
    return f"{base} – ({note})" if note else base


def _target(step: dict[str, Any], preferred_metric: str = "pace") -> tuple[str, bool]:
    canonical = step.get("target") if isinstance(step.get("target"), dict) else {}
    resolved = step.get("resolved") if isinstance(step.get("resolved"), dict) else None
    explicitly_open = not canonical or canonical.get("basis") == "open" or canonical.get("metric") == "open"
    target = resolved or (canonical if canonical.get("basis") == "absolute" else None)
    if target is None:
        if explicitly_open:
            return "", False
        raise MissingResolvedTargetError(f"Target für Schritt {step.get('id') or step.get('kind')} konnte nicht aufgelöst werden.")
    fast = target.get("pace_min_s_per_km") or target.get("pace_s_per_km")
    slow = target.get("pace_max_s_per_km") or target.get("pace_s_per_km")
    if fast and slow:
        token = _pace(fast)
        if round(float(fast)) != round(float(slow)):
            token += f"-{_pace(slow)}"
        return f"{token}/km Pace", True
    if target.get("hr_min_pct") is not None:
        raise MissingExecutionPaceError("Herzfrequenzziel besitzt keine aktuelle persönliche Ausführungs-Pace.")
    if target.get("hr_bpm") is not None:
        # Intervals' documented portable builder contract has no absolute-bpm target.
        raise WorkoutSerializationError("Absolute Herzfrequenzziele sind im Intervals-Workout-Text nicht portabel darstellbar.")
    rpe = target.get("rpe", target.get("rpe_min", target.get("min") if target.get("metric") == "rpe" else None))
    if rpe is not None:
        high = target.get("rpe_max", target.get("max") if target.get("metric") == "rpe" else None)
        return f"RPE={rpe}{f'-{high}' if high is not None and high != rpe else ''}", False
    if explicitly_open:
        return "", False
    raise MissingResolvedTargetError(f"Resolved Target für Schritt {step.get('id') or step.get('kind')} enthält kein unterstütztes Ziel.")


def _tree(flat_steps: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    try:
        return step_tree(flat_steps)
    except ValueError as exc:
        raise WorkoutSerializationError(str(exc)) from exc


def serialize_endurance_session_to_intervals(session: dict[str, Any]) -> str:
    steps = list(session.get("steps") or [])
    if not steps:
        raise WorkoutSerializationError("Session besitzt keine Workout-Schritte.")
    children = _tree(steps)
    work_targets = []
    for step in steps:
        if str(step.get("kind")) in {"work", "interval"}:
            work_targets.append(step.get("resolved") or step.get("target") or {})
    preferred_metric = "pace"

    def render_step(step: dict[str, Any], ancestors: set[str]) -> list[str]:
            step_id = str(step.get("id") or id(step)); kind = str(step.get("kind") or "open").lower()
            if step_id in ancestors:
                raise WorkoutSerializationError("Zyklische Workout-Step-Struktur.")
            nested = children.get(step_id, []) if step.get("id") else []
            if kind == "repeat" or nested:
                reps = int(step.get("reps") or 0)
                if reps < 1 or not nested:
                    raise WorkoutSerializationError("Repeat-Gruppe braucht Wiederholungen und mindestens einen Schritt.")
                lines = [str(step.get("notes") or "Main Set").strip().splitlines()[0]]
                for repetition in range(reps):
                    for child in nested:
                        if repetition == reps - 1 and str(child.get("kind")) == "recovery":
                            continue
                        lines.extend(render_step(child, ancestors | {step_id}))
                lines.append("")
                return lines
            duration = _duration(step)
            target, _structured = _target(step, preferred_metric)
            label = _step_text(step, kind)
            suffix = f" {target}" if target else ""
            intensity_kind = "interval" if kind in {"work", "stride"} else kind
            intensity = f" intensity={intensity_kind}" if intensity_kind in {"warmup", "cooldown", "recovery", "interval"} else ""
            return [f"- {label} {duration}{suffix}{intensity}".rstrip()]

    def render(parent: str | None) -> list[str]:
        return [line for step in children.get(parent, []) for line in render_step(step, set())]

    output = "\n".join(render(None)).strip()
    if not output or not any(line.lstrip().startswith("-") for line in output.splitlines()):
        raise WorkoutSerializationError("Intervals Workout Description ist leer.")
    return output


def build_intervals_payload(session: dict[str, Any]) -> dict[str, Any]:
    session_id = str(session.get("id") or "").strip()
    name = str(session.get("title") or "").strip()
    scheduled = str(session.get("scheduled_date") or "")
    if not session_id or not name:
        raise WorkoutSerializationError("Session-ID und Name sind erforderlich.")
    try:
        date.fromisoformat(scheduled)
    except ValueError as exc:
        raise WorkoutSerializationError("Session-Datum ist ungültig.") from exc
    return {"category": "WORKOUT", "type": "Run", "start_date_local": f"{scheduled}T00:00:00", "name": name, "description": serialize_endurance_session_to_intervals(session), "external_id": external_id_for_session(session_id)}


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json({"serializer_version": SERIALIZER_VERSION, "payload": payload}).encode("utf-8")).hexdigest()


def _expanded_leaf_steps(session: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return expanded_leaf_steps(list(session.get("steps") or []))
    except ValueError as exc:
        raise WorkoutSerializationError(str(exc)) from exc


def _remote_leaf_steps(workout_doc: dict[str, Any]) -> list[dict[str, Any]]:
    def expand(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items:
            nested = item.get("steps") if isinstance(item.get("steps"), list) else []
            if nested:
                for _ in range(max(1, int(item.get("reps") or 1))): out.extend(expand(nested))
            else: out.append(item)
        return out
    return expand(list(workout_doc.get("steps") or []))


def validate_remote_semantics(session: dict[str, Any], remote: dict[str, Any]) -> None:
    expected = _expanded_leaf_steps(session)
    doc = remote.get("workout_doc") if isinstance(remote.get("workout_doc"), dict) else {}
    actual = _remote_leaf_steps(doc)
    if len(actual) != len(expected):
        raise WorkoutSerializationError(f"Remote Semantik: {len(actual)} statt {len(expected)} Workout-Schritte.")
    work_targets = [step.get("resolved") or step.get("target") or {} for step in expected if step.get("kind") in {"work", "stride"}]
    preferred_metric = "pace"
    for index, (source, compiled) in enumerate(zip(expected, actual), start=1):
        if source.get("duration_s") and abs(float(compiled.get("duration") or 0) - float(source["duration_s"])) > 1:
            raise WorkoutSerializationError(f"Remote Semantik: Dauer in Schritt {index} verändert.")
        if source.get("distance_m") and abs(float(compiled.get("distance") or 0) - float(source["distance_m"])) > 2:
            raise WorkoutSerializationError(f"Remote Semantik: Distanz in Schritt {index} verändert.")
        kind = str(source.get("kind") or "")
        expected_intensity = "interval" if kind in {"work", "stride"} else kind if kind in {"warmup", "cooldown", "recovery"} else None
        if expected_intensity and compiled.get("intensity") != expected_intensity:
            raise WorkoutSerializationError(f"Remote Semantik: Zieltyp in Schritt {index} verändert.")
        token, structured = _target(source, preferred_metric)
        if structured:
            field = "hr" if token.endswith(" HR") else "pace"
            if not isinstance(compiled.get(field), dict):
                raise WorkoutSerializationError(f"Remote Semantik: {field.upper()}-Ziel in Schritt {index} fehlt.")
            target = source.get("resolved") or source.get("target") or {}
            if field == "pace":
                fast = round(float(target.get("pace_min_s_per_km") or target.get("pace_s_per_km") or 0))
                slow = round(float(target.get("pace_max_s_per_km") or target.get("pace_s_per_km") or 0))
                remote_fast = compiled[field].get("start", compiled[field].get("value"))
                remote_slow = compiled[field].get("end", compiled[field].get("value"))
                if round(float(remote_fast or 0)) != fast or round(float(remote_slow or 0)) != slow:
                    raise WorkoutSerializationError(f"Remote Semantik: Pace in Schritt {index} verändert.")
        expected_text = _step_text(source, kind)
        if str(compiled.get("text") or "") != expected_text:
            raise WorkoutSerializationError(f"Remote Semantik: Anweisung in Schritt {index} verändert.")
    expected_duration = sum(float(step.get("duration_s") or 0) for step in expected)
    if expected_duration and all(step.get("duration_s") for step in expected) and abs(float(doc.get("duration") or 0) - expected_duration) > 1:
        raise WorkoutSerializationError("Remote Semantik: Gesamtdauer stimmt nicht überein.")


def ensure_sync_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS endurance_sync_jobs (
      id TEXT PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT, external_id TEXT NOT NULL,
      operation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
      next_attempt_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_endurance_sync_jobs_due ON endurance_sync_jobs(provider,status,next_attempt_at);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(endurance_external_map)")}
    for name, ddl in {
        "external_event_id": "TEXT", "last_synced_at": "TEXT", "last_attempt_at": "TEXT",
        "payload_hash": "TEXT", "synced_revision": "INTEGER"
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE endurance_external_map ADD COLUMN {name} {ddl}")


def _enqueue(conn: sqlite3.Connection, session_id: str | None, operation: str, external_id: str) -> None:
    existing = conn.execute("SELECT id FROM endurance_sync_jobs WHERE provider=? AND external_id=? AND operation=? AND status IN ('pending','retry') ORDER BY created_at DESC LIMIT 1", (PROVIDER, external_id, operation)).fetchone()
    now = _now()
    if existing:
        conn.execute("UPDATE endurance_sync_jobs SET session_id=?,status='pending',next_attempt_at=?,last_error=NULL,updated_at=? WHERE id=?", (session_id, now, now, existing[0]))
    else:
        conn.execute("INSERT INTO endurance_sync_jobs VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)", (f"esj_{uuid.uuid4().hex[:16]}", PROVIDER, session_id, external_id, operation, now, now, now))


def _snapshot_signatures(snapshot: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    snapshot = snapshot or {}
    sessions = {str(row["id"]): dict(row) for row in snapshot.get("endurance_sessions") or []}
    children: dict[str, list[dict[str, Any]]] = {}
    for table in ("endurance_steps", "endurance_resolutions"):
        for row in snapshot.get(table) or []:
            children.setdefault(str(row["session_id"]), []).append({"table": table, **dict(row)})
    signatures = {sid: hashlib.sha256(_json({"session": row, "children": sorted(children.get(sid, []), key=lambda x: (x["table"], str(x.get("id"))))}).encode()).hexdigest() for sid, row in sessions.items()}
    return signatures, sessions


def _current_signatures(conn: sqlite3.Connection, plan_id: str) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    snapshot = {"endurance_sessions": [dict(r) for r in conn.execute("SELECT * FROM endurance_sessions WHERE plan_id=?", (plan_id,))]}
    snapshot["endurance_steps"] = [dict(r) for r in conn.execute("SELECT st.* FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id WHERE s.plan_id=?", (plan_id,))]
    snapshot["endurance_resolutions"] = [dict(r) for r in conn.execute("SELECT rr.* FROM endurance_resolutions rr JOIN endurance_sessions s ON s.id=rr.session_id WHERE s.plan_id=?", (plan_id,))]
    return _snapshot_signatures(snapshot)


def queue_plan_sync_changes(conn: sqlite3.Connection, plan_id: str, before_snapshot: dict[str, Any] | None = None) -> dict[str, int]:
    before_sig, before_sessions = _snapshot_signatures(before_snapshot)
    current_sig, current_sessions = _current_signatures(conn, plan_id)
    today = date.today().isoformat()
    queued = deleted = 0
    for session_id, row in current_sessions.items():
        if row["scheduled_date"] < today:
            continue
        current_map = conn.execute("SELECT * FROM endurance_external_map WHERE session_id=? AND provider=?", (session_id, PROVIDER)).fetchone()
        if row.get("status") not in {"planned", "scheduled", None}:
            if current_map and current_map["external_id"]:
                _enqueue(conn, session_id, "DELETE", current_map["external_id"])
                conn.execute("UPDATE endurance_sessions SET sync_state='dirty' WHERE id=?", (session_id,))
                conn.execute("UPDATE endurance_external_map SET sync_state='dirty' WHERE id=?", (current_map["id"],))
                deleted += 1
            continue
        if before_snapshot is None and row.get("sync_state") == "synced" and current_map:
            session = load_session(conn, session_id)
            current_payload_hash = payload_hash(build_intervals_payload(session)) if session else None
            if current_map["payload_hash"] == current_payload_hash:
                continue
        elif before_snapshot is None and row.get("sync_state") == "sync_error":
            continue
        if before_sig.get(session_id) == current_sig[session_id] and row.get("sync_state") == "synced":
            continue
        external_id = external_id_for_session(session_id)
        mapping = current_map
        state = "dirty" if mapping and mapping["sync_state"] == "synced" else "not_synced"
        conn.execute("UPDATE endurance_sessions SET sync_state=? WHERE id=?", (state, session_id))
        if mapping:
            conn.execute("UPDATE endurance_external_map SET external_id=?,sync_state=?,last_error=NULL WHERE id=?", (external_id, state, mapping["id"]))
        else:
            conn.execute("INSERT INTO endurance_external_map(id,session_id,provider,external_id,sync_state) VALUES(?,?,?,?,?)", (f"eem_{uuid.uuid4().hex[:16]}", session_id, PROVIDER, external_id, state))
        _enqueue(conn, session_id, "UPSERT", external_id)
        queued += 1
    old_maps = {str(r["session_id"]): dict(r) for r in (before_snapshot or {}).get("endurance_external_map") or [] if r.get("provider") == PROVIDER}
    for session_id, row in before_sessions.items():
        if session_id in current_sessions or row.get("scheduled_date", "") < today or session_id not in old_maps:
            continue
        _enqueue(conn, None, "DELETE", str(old_maps[session_id].get("external_id") or external_id_for_session(session_id)))
        deleted += 1
    return {"upsert": queued, "delete": deleted}


def retry_plan_sync(plan_id: str) -> dict[str, int]:
    conn = get_plans_db(); ensure_sync_schema(conn)
    retried = conn.execute("UPDATE endurance_sync_jobs SET status='pending',next_attempt_at=?,last_error=NULL,updated_at=? WHERE provider=? AND session_id IN (SELECT id FROM endurance_sessions WHERE plan_id=?) AND status IN ('failed','retry')", (_now(), _now(), PROVIDER, plan_id)).rowcount
    queued = queue_plan_sync_changes(conn, plan_id)
    conn.commit(); conn.close()
    return {**queued, "retried": int(retried or 0)}


def load_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM endurance_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    session = dict(row)
    resolutions = {r["step_id"]: json.loads(r["resolved_target_json"]) for r in conn.execute("SELECT * FROM endurance_resolutions WHERE session_id=?", (session_id,))}
    session["steps"] = []
    for step in conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (session_id,)):
        item = dict(step); item["target"] = json.loads(item.pop("target_json") or "{}"); item["resolved"] = resolutions.get(item["id"]); session["steps"].append(item)
    return session


def _valid_readback(remote: Any, payload: dict[str, Any]) -> bool:
    if not isinstance(remote, dict):
        return False
    if not remote.get("id") or remote.get("external_id") != payload["external_id"] or remote.get("category") != "WORKOUT" or remote.get("type") != "Run":
        return False
    if str(remote.get("start_date_local") or "")[:10] != payload["start_date_local"][:10] or not remote.get("description"):
        return False
    if "Pace" in payload["description"]:
        doc = remote.get("workout_doc")
        if isinstance(doc, dict) and not doc.get("steps"):
            return False
    return True


def _finish_error(conn: sqlite3.Connection, job: sqlite3.Row, error: Exception) -> None:
    attempts = int(job["attempt_count"] or 0) + 1
    retryable = bool(getattr(error, "retryable", False))
    status = "retry" if retryable else "failed"
    delay = getattr(error, "retry_after", None) or RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
    next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds") if retryable else None
    code = getattr(error, "code", "workout_serialization_failed")
    message = f"{code}: {str(error)[:400]}"
    conn.execute("UPDATE endurance_sync_jobs SET status=?,attempt_count=?,next_attempt_at=?,last_error=?,updated_at=? WHERE id=?", (status, attempts, next_at, message, _now(), job["id"]))
    if job["session_id"]:
        conn.execute("UPDATE endurance_sessions SET sync_state='sync_error' WHERE id=?", (job["session_id"],))
        conn.execute("UPDATE endurance_external_map SET sync_state='sync_error',last_attempt_at=?,last_error=? WHERE session_id=? AND provider=?", (_now(), message, job["session_id"], PROVIDER))


def run_sync_jobs(*, client: IntervalsClient | None = None, limit: int = 100, session_id: str | None = None, plan_id: str | None = None) -> dict[str, int]:
    client = client or IntervalsClient()
    conn = get_plans_db(); ensure_sync_schema(conn)
    plan_query = "SELECT DISTINCT s.plan_id FROM endurance_sync_jobs j JOIN endurance_sessions s ON s.id=j.session_id WHERE j.provider=? AND j.status IN ('pending','retry')"
    plan_args: list[Any] = [PROVIDER]
    if session_id:
        plan_query += " AND j.session_id=?"; plan_args.append(session_id)
    if plan_id:
        plan_query += " AND s.plan_id=?"; plan_args.append(plan_id)
    for plan_row in conn.execute(plan_query, plan_args):
        refresh_execution_paces(conn, plan_row["plan_id"])
    conn.commit()
    query = "SELECT * FROM endurance_sync_jobs WHERE provider=? AND status IN ('pending','retry') AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
    args: list[Any] = [PROVIDER, _now()]
    if session_id:
        query += " AND session_id=?"; args.append(session_id)
    if plan_id:
        query += " AND session_id IN (SELECT id FROM endurance_sessions WHERE plan_id=?)"; args.append(plan_id)
    query += " ORDER BY created_at LIMIT ?"; args.append(limit)
    jobs = conn.execute(query, args).fetchall()
    result = {"processed": 0, "synced": 0, "deleted": 0, "skipped": 0, "errors": 0}
    upserts: list[tuple[sqlite3.Row, dict[str, Any], str, dict[str, Any]]] = []
    deletes: list[sqlite3.Row] = []
    for job in jobs:
        if job["operation"] == "DELETE":
            deletes.append(job); continue
        try:
            session = load_session(conn, str(job["session_id"]))
            if not session:
                raise WorkoutSerializationError("Session existiert nicht mehr.")
            payload = build_intervals_payload(session); digest = payload_hash(payload)
            mapping = conn.execute("SELECT payload_hash,sync_state FROM endurance_external_map WHERE session_id=? AND provider=?", (job["session_id"], PROVIDER)).fetchone()
            if mapping and mapping["payload_hash"] == digest and mapping["sync_state"] == "synced":
                conn.execute("UPDATE endurance_sync_jobs SET status='done',updated_at=? WHERE id=?", (_now(), job["id"])); result["skipped"] += 1
            else:
                upserts.append((job, payload, digest, session))
        except Exception as exc:
            _finish_error(conn, job, exc); result["errors"] += 1
    if upserts:
        started = time.monotonic()
        try:
            response = client.upsert_events([item[1] for item in upserts])
            remote_by_external = {str(item.get("external_id")): item for item in response or [] if isinstance(item, dict)}
        except Exception as exc:
            for job, _, _, _ in upserts:
                _finish_error(conn, job, exc); result["errors"] += 1
        else:
            for job, payload, digest, session in upserts:
                try:
                    remote = remote_by_external.get(payload["external_id"])
                    if not _valid_readback(remote, payload):
                        raise WorkoutSerializationError(f"Remote Readback für {payload['external_id']} ist unvollständig.")
                    validate_remote_semantics(session, remote)
                    now = _now(); event_id = str(remote["id"])
                    conn.execute("UPDATE endurance_external_map SET external_event_id=?,sync_state='synced',last_error=NULL,synced_at=?,last_synced_at=?,last_attempt_at=?,payload_hash=?,synced_revision=(SELECT revision FROM endurance_plans p JOIN endurance_sessions s ON s.plan_id=p.id WHERE s.id=?) WHERE session_id=? AND provider=?", (event_id, now, now, now, digest, job["session_id"], job["session_id"], PROVIDER))
                    conn.execute("UPDATE endurance_sessions SET sync_state='synced' WHERE id=?", (job["session_id"],))
                    conn.execute("UPDATE endurance_sync_jobs SET status='done',attempt_count=attempt_count+1,last_error=NULL,updated_at=? WHERE id=?", (now, job["id"])); result["synced"] += 1
                except Exception as exc:
                    _finish_error(conn, job, exc); result["errors"] += 1
            log.info("endurance_intervals_sync operation=UPSERT count=%s synced=%s errors=%s duration_ms=%s", len(upserts), result["synced"], result["errors"], round((time.monotonic()-started)*1000))
    if deletes:
        try:
            client.delete_events([{"external_id": row["external_id"]} for row in deletes])
            for job in deletes:
                if job["session_id"]:
                    conn.execute("DELETE FROM endurance_external_map WHERE session_id=? AND provider=?", (job["session_id"], PROVIDER))
                    conn.execute("UPDATE endurance_sessions SET sync_state='not_synced' WHERE id=?", (job["session_id"],))
                conn.execute("UPDATE endurance_sync_jobs SET status='done',attempt_count=attempt_count+1,last_error=NULL,updated_at=? WHERE id=?", (_now(), job["id"])); result["deleted"] += 1
        except Exception as exc:
            for job in deletes:
                _finish_error(conn, job, exc); result["errors"] += 1
    result["processed"] = len(jobs)
    conn.commit(); conn.close()
    return result


def reconcile_sync_queue() -> dict[str, int]:
    conn = get_plans_db(); ensure_sync_schema(conn)
    totals = {"upsert": 0, "delete": 0}
    for row in conn.execute("SELECT id FROM endurance_plans WHERE status='active'"):
        refresh_execution_paces(conn, row["id"])
        queued = queue_plan_sync_changes(conn, row["id"])
        totals = {key: totals[key] + queued[key] for key in totals}
    conn.commit(); conn.close()
    return totals


def refresh_execution_paces(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    """Materialize current athlete-specific pace guidance without replacing internal HR/RPE intent."""
    model = load_current_hr_pace_model()
    if model is None:
        return {"available": False, "changed_sessions": 0, "sample_count": 0}
    quality = execution_model_quality(model)
    changed_sessions: set[str] = set()
    rows = conn.execute(
        """
        SELECT st.*,s.session_type,s.scheduled_date
        FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id
        WHERE s.plan_id=? AND s.scheduled_date>=?
        ORDER BY s.scheduled_date,st.sort_order,st.id
        """,
        (plan_id, date.today().isoformat()),
    ).fetchall()
    for row in rows:
        if row["kind"] == "repeat":
            continue
        target = json.loads(row["target_json"] or "{}")
        # HR lags too much to turn short efforts into an authoritative pace.
        # The model remains available for analysis, but the authored RPE intent
        # stays primary until a benchmark/anchor or robust execution model exists.
        short_effort = row["kind"] in {"work", "stride"} and int(row["duration_s"] or 0) <= 180
        if not quality["execution_ready"] or short_effort:
            desired = None
        elif target.get("pace_s_per_km") or target.get("pace_min_s_per_km"):
            desired = None
        elif target.get("hr_min_pct") is not None:
            desired = execution_pace_for_hr_percent(model, target["hr_min_pct"], target.get("hr_max_pct", target["hr_min_pct"]))
        else:
            rpe_low = target.get("rpe_min", target.get("min") if target.get("metric") == "rpe" else None)
            rpe_high = target.get("rpe_max", target.get("max") if target.get("metric") == "rpe" else rpe_low)
            if rpe_low is not None:
                low_pct, high_pct = rpe_to_hr_percent(float(rpe_low), float(rpe_high if rpe_high is not None else rpe_low))
            elif row["kind"] == "recovery":
                low_pct, high_pct = 60, 72
            else:
                low_pct, high_pct = 62, 75
            desired = execution_pace_for_hr_percent(model, low_pct, high_pct)
        current_row = conn.execute("SELECT * FROM endurance_resolutions WHERE step_id=?", (row["id"],)).fetchone()
        current = json.loads(current_row["resolved_target_json"]) if current_row else None
        current_is_dynamic = bool(current and current.get("source") == "current_hr_pace_model")
        if desired is None:
            if current_is_dynamic:
                conn.execute("DELETE FROM endurance_resolutions WHERE step_id=?", (row["id"],))
                changed_sessions.add(row["session_id"])
            continue
        if current == desired:
            continue
        conn.execute("DELETE FROM endurance_resolutions WHERE step_id=?", (row["id"],))
        conn.execute(
            "INSERT INTO endurance_resolutions VALUES(?,?,?,?,?,?)",
            (f"ers_{uuid.uuid4().hex[:16]}", row["session_id"], row["id"], None, _now(), _json(desired)),
        )
        changed_sessions.add(row["session_id"])
    session_ids = [row["id"] for row in conn.execute("SELECT id FROM endurance_sessions WHERE plan_id=? AND scheduled_date>=?", (plan_id, date.today().isoformat()))]
    for session_id in session_ids:
        session = load_session(conn, session_id)
        if not session:
            continue
        summary = summarize_steps(session["steps"])
        if not summary["complete"]:
            continue
        duration_s = round(float(summary["duration_s"]))
        distance_m = round(float(summary["distance_m"]) / 10) * 10
        current = conn.execute("SELECT duration_s,distance_m FROM endurance_sessions WHERE id=?", (session_id,)).fetchone()
        if current and (current["duration_s"] != duration_s or current["distance_m"] != distance_m):
            conn.execute("UPDATE endurance_sessions SET duration_s=?,distance_m=?,sync_state='dirty',updated_at=? WHERE id=?", (duration_s, distance_m, _now(), session_id))
            conn.execute("UPDATE endurance_external_map SET sync_state='dirty' WHERE session_id=? AND provider=?", (session_id, PROVIDER))
            changed_sessions.add(session_id)
    for session_id in changed_sessions:
        conn.execute("UPDATE endurance_sessions SET sync_state='dirty',updated_at=? WHERE id=?", (_now(), session_id))
        conn.execute("UPDATE endurance_external_map SET sync_state='dirty' WHERE session_id=? AND provider=?", (session_id, PROVIDER))
    return {"available": quality["execution_ready"], "analysis_available": True, "changed_sessions": len(changed_sessions), "model_signature": model.signature, **quality}


def sync_summary(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    ensure_sync_schema(conn)
    counts = {row["sync_state"]: row["n"] for row in conn.execute("SELECT sync_state,COUNT(*) n FROM endurance_sessions WHERE plan_id=? AND scheduled_date>=? GROUP BY sync_state", (plan_id, date.today().isoformat()))}
    return {"provider": "intervals.icu", "write_available": IntervalsClient().configured, "mode": "automatic_mirror", "counts": counts, "pending": counts.get("not_synced", 0) + counts.get("dirty", 0) + counts.get("syncing", 0), "errors": counts.get("sync_error", 0)}
