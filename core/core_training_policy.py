from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from database.connections import get_core_db


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_text(text: Any) -> str:
    compact = " ".join(str(text or "").strip().lower().split())
    return re.sub(r"[^a-z0-9äöüß\s]+", "", compact).strip()


def _tokens(text: Any) -> set[str]:
    stop = {
        "und", "oder", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "einem", "zu",
        "mit", "für", "von", "bei", "im", "in", "am", "an", "auf", "wenn", "soll", "core", "heute",
    }
    return {
        tok for tok in re.findall(r"[a-z0-9äöüß]+", _normalize_text(text))
        if len(tok) >= 3 and tok not in stop
    }


def _question_key(text: Any) -> str:
    norm = _normalize_text(text)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:24] if norm else ""


def ensure_core_question_policy_schema(conn: sqlite3.Connection | None = None, *, bootstrap: bool = True) -> None:
    owns_conn = conn is None
    db = conn or get_core_db()
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_training_question_policy (
            question_key TEXT PRIMARY KEY,
            question_norm TEXT NOT NULL,
            question_text TEXT NOT NULL,
            topic TEXT NOT NULL,
            dimension TEXT NOT NULL,
            tokens_json TEXT NOT NULL DEFAULT '[]',
            yes_count INTEGER NOT NULL DEFAULT 0,
            no_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            yes_rate REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.0,
            last_answer TEXT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_core_training_question_policy_topic_dim
        ON core_training_question_policy(topic, dimension, updated_at DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS core_training_policy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            t TEXT NOT NULL,
            signature TEXT NULL,
            question_key TEXT NOT NULL,
            topic TEXT NOT NULL,
            dimension TEXT NOT NULL,
            answer TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            payload_json TEXT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_core_training_policy_events_qk ON core_training_policy_events(question_key, id DESC)"
    )
    if bootstrap:
        try:
            policy_count = int(cur.execute("SELECT COUNT(*) FROM core_training_question_policy").fetchone()[0] or 0)
            answers_count = int(cur.execute("SELECT COUNT(*) FROM core_training_answers").fetchone()[0] or 0)
        except Exception:
            policy_count = 0
            answers_count = 0
        if policy_count == 0 and answers_count > 0:
            rebuild_core_question_policy(db)
    if owns_conn:
        db.commit()
    if owns_conn:
        db.close()


def _compute_confidence(total: int) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, total / 10.0)


def ingest_training_answer(
    *,
    question_text: str,
    topic: str,
    dimension: str,
    answer: str,
    signature: str | None = None,
    answered_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    norm = _normalize_text(question_text)
    key = _question_key(norm)
    if not norm or not key:
        return {"ok": False, "error": "invalid_question_text"}
    owns_conn = conn is None
    db = conn or get_core_db()
    db.row_factory = sqlite3.Row
    ensure_core_question_policy_schema(db, bootstrap=False)
    cur = db.cursor()
    row = cur.execute(
        "SELECT yes_count, no_count, total_count FROM core_training_question_policy WHERE question_key=? LIMIT 1",
        (key,),
    ).fetchone()
    yes = int(row["yes_count"] or 0) if row else 0
    no = int(row["no_count"] or 0) if row else 0
    total = int(row["total_count"] or 0) if row else 0
    a = "yes" if str(answer or "").strip().lower() == "yes" else "no"
    if a == "yes":
        yes += 1
    else:
        no += 1
    total += 1
    yes_rate = yes / max(1, total)
    confidence = _compute_confidence(total)
    now = answered_at or _ts()
    cur.execute(
        """
        INSERT INTO core_training_question_policy (
            question_key, question_norm, question_text, topic, dimension, tokens_json,
            yes_count, no_count, total_count, yes_rate, confidence, last_answer, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(question_key) DO UPDATE SET
            question_norm=excluded.question_norm,
            question_text=excluded.question_text,
            topic=excluded.topic,
            dimension=excluded.dimension,
            tokens_json=excluded.tokens_json,
            yes_count=excluded.yes_count,
            no_count=excluded.no_count,
            total_count=excluded.total_count,
            yes_rate=excluded.yes_rate,
            confidence=excluded.confidence,
            last_answer=excluded.last_answer,
            updated_at=excluded.updated_at
        """,
        (
            key,
            norm,
            str(question_text or "").strip(),
            str(topic or "").strip() or "unknown",
            str(dimension or "").strip() or "unknown",
            json.dumps(sorted(_tokens(norm)), ensure_ascii=False),
            yes,
            no,
            total,
            yes_rate,
            confidence,
            a,
            now,
        ),
    )
    cur.execute(
        """
        INSERT INTO core_training_policy_events (t, signature, question_key, topic, dimension, answer, confidence, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            str(signature or "").strip() or None,
            key,
            str(topic or "").strip() or "unknown",
            str(dimension or "").strip() or "unknown",
            a,
            confidence,
            json.dumps({"question_text": str(question_text or "").strip(), "yes_rate": yes_rate}, ensure_ascii=False),
        ),
    )
    if owns_conn:
        db.commit()
    if owns_conn:
        db.close()
    return {
        "ok": True,
        "question_key": key,
        "topic": topic,
        "dimension": dimension,
        "yes_rate": round(yes_rate, 4),
        "confidence": round(confidence, 4),
        "samples": total,
        "answer": a,
    }


def rebuild_core_question_policy(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    owns_conn = conn is None
    db = conn or get_core_db()
    db.row_factory = sqlite3.Row
    ensure_core_question_policy_schema(db, bootstrap=False)
    cur = db.cursor()
    cur.execute("DELETE FROM core_training_question_policy")
    cur.execute("DELETE FROM core_training_policy_events")
    rows = cur.execute(
        """
        SELECT a.t, a.signature, a.answer, a.topic, a.kind, a.card_text_norm, s.card_json
        FROM core_training_answers a
        LEFT JOIN core_training_seen s ON s.signature = a.signature
        ORDER BY a.id ASC
        """
    ).fetchall()
    count = 0
    for row in rows:
        topic = str(row["topic"] or "unknown").strip() or "unknown"
        kind = str(row["kind"] or "").strip()
        parts = kind.split(":")
        dimension = parts[2] if len(parts) > 2 and parts[2] else (parts[1] if len(parts) > 1 and parts[1] else topic)
        question_text = ""
        raw = str(row["card_json"] or "").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            question_text = str(payload.get("text") or "").strip()
        if not question_text:
            question_text = str(row["card_text_norm"] or "").strip()
        if not question_text:
            continue
        ingest_training_answer(
            question_text=question_text,
            topic=topic,
            dimension=dimension,
            answer=str(row["answer"] or ""),
            signature=str(row["signature"] or "").strip() or None,
            answered_at=str(row["t"] or "").strip() or None,
            conn=db,
        )
        count += 1
    db.commit()
    if owns_conn:
        db.close()
    return {"ok": True, "processed_answers": count}


def _load_policy_rows(conn: sqlite3.Connection, topic: str | None = None, dimension: str | None = None) -> list[sqlite3.Row]:
    where = []
    params: list[Any] = []
    if topic:
        where.append("topic=?")
        params.append(str(topic))
    if dimension:
        where.append("dimension=?")
        params.append(str(dimension))
    sql = """
        SELECT question_key, question_text, topic, dimension, tokens_json, yes_rate, confidence, total_count, updated_at
        FROM core_training_question_policy
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC"
    return conn.execute(sql, tuple(params)).fetchall()


def get_core_question_policy_signal(
    *,
    topic: str | None = None,
    dimension: str | None = None,
    query_text: str | None = None,
    min_samples: int = 1,
) -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_core_question_policy_schema(conn)
        rows = _load_policy_rows(conn, topic=topic, dimension=dimension)
        if not rows:
            return {"ok": True, "answer": "neutral", "prob_yes": 0.5, "confidence": 0.0, "matched": []}
        q_tokens = _tokens(query_text or "")
        yes_weight = 0.0
        no_weight = 0.0
        matched: list[dict[str, Any]] = []
        for row in rows:
            samples = int(row["total_count"] or 0)
            if samples < max(1, int(min_samples)):
                continue
            try:
                row_tokens = set(json.loads(str(row["tokens_json"] or "[]")))
            except Exception:
                row_tokens = _tokens(row["question_text"])
            sim = 1.0
            if q_tokens:
                if not row_tokens:
                    continue
                inter = len(q_tokens & row_tokens)
                if inter <= 0:
                    continue
                sim = inter / max(1, len(q_tokens | row_tokens))
                if sim < 0.2:
                    continue
            conf = float(row["confidence"] or 0.0)
            rate = float(row["yes_rate"] or 0.5)
            weight = (0.25 + 0.75 * sim) * (0.35 + conf) * (0.55 + min(1.0, samples / 6.0))
            if weight <= 0.0:
                continue
            yes_weight += rate * weight
            no_weight += (1.0 - rate) * weight
            matched.append(
                {
                    "question_key": str(row["question_key"]),
                    "question_text": str(row["question_text"]),
                    "topic": str(row["topic"]),
                    "dimension": str(row["dimension"]),
                    "yes_rate": round(rate, 4),
                    "confidence": round(conf, 4),
                    "samples": samples,
                    "similarity": round(sim, 4),
                    "weight": round(weight, 4),
                }
            )
        total = yes_weight + no_weight
        if total <= 0:
            return {"ok": True, "answer": "neutral", "prob_yes": 0.5, "confidence": 0.0, "matched": []}
        prob = yes_weight / total
        certainty = min(1.0, total / 6.5)
        if prob >= 0.56:
            answer = "yes"
        elif prob <= 0.44:
            answer = "no"
        else:
            answer = "neutral"
        matched.sort(key=lambda x: float(x.get("weight") or 0.0), reverse=True)
        return {
            "ok": True,
            "answer": answer,
            "prob_yes": round(prob, 4),
            "confidence": round(certainty, 4),
            "topic": topic,
            "dimension": dimension,
            "query_text": str(query_text or "").strip(),
            "matched": matched[:8],
        }
    finally:
        conn.close()


def get_core_policy_profile() -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_core_question_policy_schema(conn)
        rows = conn.execute(
            """
            SELECT topic, dimension, yes_rate, confidence, total_count
            FROM core_training_question_policy
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"ok": True, "topics": {}, "dimensions": {}, "questions": 0}
    topics: dict[str, dict[str, float]] = {}
    dims: dict[str, dict[str, float]] = {}
    for row in rows:
        topic = str(row["topic"] or "unknown")
        dim = str(row["dimension"] or "unknown")
        rate = float(row["yes_rate"] or 0.5)
        conf = float(row["confidence"] or 0.0)
        samples = int(row["total_count"] or 0)
        wt = (0.4 + conf) * (0.5 + min(1.0, samples / 8.0))
        for bucket, key in ((topics, topic), (dims, dim)):
            agg = bucket.setdefault(key, {"yes_w": 0.0, "all_w": 0.0, "samples": 0.0, "n": 0.0})
            agg["yes_w"] += rate * wt
            agg["all_w"] += wt
            agg["samples"] += samples
            agg["n"] += 1

    def finalize(src: dict[str, dict[str, float]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, agg in src.items():
            prob = (agg["yes_w"] / agg["all_w"]) if agg["all_w"] > 0 else 0.5
            conf = min(1.0, agg["all_w"] / 4.5)
            out[key] = {
                "prob_yes": round(prob, 4),
                "confidence": round(conf, 4),
                "samples": int(agg["samples"]),
                "questions": int(agg["n"]),
                "answer": "yes" if prob >= 0.56 else "no" if prob <= 0.44 else "neutral",
            }
        return out

    return {
        "ok": True,
        "topics": finalize(topics),
        "dimensions": finalize(dims),
        "questions": len(rows),
    }


__all__ = [
    "ensure_core_question_policy_schema",
    "ingest_training_answer",
    "rebuild_core_question_policy",
    "get_core_question_policy_signal",
    "get_core_policy_profile",
]
