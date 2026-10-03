from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from database.connections import get_core_db
from memory import get_memory_service
from memory.memory_markdown import apply_operation, parse_sections
from memory.memory_models import LOGICAL_FILE_SPECS
from memory.memory_service import sanitize_memory_content

from core.core_memory_layers import (
    CONSOLIDATION_STRATEGIES,
    TARGET_SECTIONS,
    _json_dumps,
    _json_loads,
    _similarity_ratio,
    _text,
    _token_set,
    _tokenize,
    _utc_iso,
    _validate_target,
    append_memory_review_log,
    ensure_core_memory_consolidations_schema,
    ensure_core_memory_review_log_schema,
    get_memory_review_history,
)

PATCH_ERROR_MESSAGES = {
    "memory_file_patch_not_found": "Dieser Memory-Datei-Patch wurde nicht gefunden.",
    "patch_not_draft": "Nur Draft-Patches können angewendet werden.",
    "patch_blocked": "Dieser Patch ist blockiert und kann nicht angewendet werden.",
    "confirm_required": "Dieser Patch braucht Review-Bestätigung.",
    "invalid_strategy": "Diese Patch-Strategie ist nicht applybar.",
    "invalid_target": "Ziel-Datei oder Ziel-Section ist ungültig.",
    "empty_patch_text": "Patch-Text darf nicht leer sein.",
    "patch_apply_failed": "Der Patch konnte nicht auf die Memory-Datei angewendet werden.",
    "invalid_action": "Diese Aktion ist für File-Patches nicht erlaubt.",
    "invalid_update_field": "Dieses Feld darf im Patch nicht bearbeitet werden.",
}


PATCH_STATUSES = {"draft", "applied", "dismissed", "superseded"}
PATCH_STRATEGIES = {"append_section_note", "append_log_entry", "manual_review_only"}
PATCH_SAFETY_STATUSES = {"ready", "needs_review", "blocked"}
SENSITIVE_TOKENS = {
    "medizin",
    "medikament",
    "blut",
    "schmerz",
    "verletzung",
    "diagnose",
    "arzt",
    "therapie",
    "depression",
    "panic",
    "panik",
}


def ensure_core_memory_file_patches_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_file_patches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                patch_key TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                target_memory_file TEXT NOT NULL,
                target_section TEXT NOT NULL,
                strategy TEXT NOT NULL,
                title TEXT NOT NULL,
                patch_text TEXT NOT NULL,
                preview_before TEXT,
                preview_after TEXT,
                source_consolidation_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                safety_status TEXT NOT NULL DEFAULT 'needs_review',
                safety_reasons_json TEXT NOT NULL DEFAULT '[]',
                applied_at TEXT,
                applied_result_json TEXT NOT NULL DEFAULT '{}',
                dismissed_at TEXT,
                dismissed_reason TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_file_patches_status ON core_memory_file_patches(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_file_patches_target_file ON core_memory_file_patches(target_memory_file)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_file_patches_target_section ON core_memory_file_patches(target_section)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_file_patches_safety_status ON core_memory_file_patches(safety_status)")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_memory_file_patches_key
            ON core_memory_file_patches(patch_key)
            WHERE patch_key IS NOT NULL AND patch_key != ''
            """
        )
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def _row_to_patch(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {}
    item = dict(row)
    item["source_consolidation_ids"] = _json_loads(item.pop("source_consolidation_ids_json", "[]"), [])
    item["evidence"] = _json_loads(item.pop("evidence_json", "{}"), {})
    item["safety_reasons"] = _json_loads(item.pop("safety_reasons_json", "[]"), [])
    item["applied_result"] = _json_loads(item.pop("applied_result_json", "{}"), {})
    item["review_history"] = get_memory_review_history("memory_file_patch", item["id"])
    return item




def _timeframe_label(value: str) -> str:
    return {
        "today": "Heute",
        "week": "diese Woche",
        "phase": "die aktuelle Phase",
        "year": "das aktuelle Jahr",
        "global": "langfristige Muster",
    }.get(str(value or "").strip().lower(), "den aktuellen Kontext")


def _normalize_patch_strategy(value: Any) -> str:
    strategy = str(value or "").strip().lower()
    if strategy in {"append_section_note", "append_log_entry", "manual_review_only"}:
        return strategy
    if strategy in CONSOLIDATION_STRATEGIES:
        return strategy
    return strategy


def _known_memory_file(value: str) -> bool:
    file_key = str(value or "").strip()
    return file_key in TARGET_SECTIONS or file_key == "master_profile"


def _section_heading(logical_name: str, section_name: str) -> str:
    spec = LOGICAL_FILE_SPECS.get(logical_name)
    if spec and section_name in spec.section_labels:
        return spec.section_labels[section_name]
    return section_name.replace("_", " ").title()


def _format_section_preview(heading: str, content: str) -> str:
    cleaned = str(content or "").strip() or "_Noch offen._"
    return f"## {heading}\n{cleaned}"


def _build_patch_title(consolidations: list[dict[str, Any]], top_tokens: list[str], target_section: str) -> str:
    lead = next((_text(item.get("title"), 160) for item in consolidations if _text(item.get("title"), 160)), None)
    if target_section == "TRAINING" and {"push", "cut"} <= set(top_tokens):
        return "Push im Cut: Progression ja, Zusatzdruck nein"
    if lead:
        return lead
    if top_tokens:
        return " / ".join(token.capitalize() for token in top_tokens[:4])
    return "CORE-Konsolidierung"


def _build_patch_text(
    *,
    patch_date: str,
    title: str,
    timeframe: str,
    consolidations: list[dict[str, Any]],
) -> str:
    summaries: list[str] = []
    for item in consolidations:
        summary = _text(item.get("summary"), 240)
        if summary and summary not in summaries:
            summaries.append(summary)
    if not summaries:
        summaries.append("Mehrere reviewte Konsolidierungen zeigen ein wiederkehrendes Muster.")
    kernaussage = summaries[0]
    hints = summaries[:3]
    lines = [
        f"### {patch_date} · CORE-Konsolidierung: {title}",
        "",
        f"- Kernaussage: {kernaussage}",
        f"- Gilt für: {_timeframe_label(timeframe)}",
        f"- Evidenz: {len(consolidations)} Consolidations",
        "- Hinweise:",
    ]
    for hint in hints:
        lines.append(f"  - {hint}")
    text = "\n".join(lines).strip()
    if len(text) > 1800:
        text = text[:1800].rstrip()
    return sanitize_memory_content(text)


def _build_patch_key(
    *,
    target_memory_file: str,
    target_section: str,
    timeframe: str,
    category: str,
    top_tokens: list[str],
    source_ids: list[int],
) -> str:
    lead = "-".join(top_tokens[:4]) if top_tokens else "-".join(str(item) for item in source_ids[:3])
    return f"{target_memory_file}:{target_section}:{timeframe}:{category}:{lead}"


def _preview_for_content(target_memory_file: str, target_section: str, strategy: str, patch_text: str) -> tuple[str, str]:
    service = get_memory_service()
    heading = _section_heading(target_memory_file, target_section)
    try:
        file_payload = service.get_file(target_memory_file, create_missing=False)
        raw_markdown = str(file_payload.get("raw_markdown") or "")
    except Exception:
        neutral_after = _format_section_preview(heading, str(patch_text or "").strip() or "_Noch offen._")
        return ("Section konnte nicht gelesen werden.", neutral_after)
    before_sections = parse_sections(raw_markdown)
    section_before = before_sections.get(str(target_section or "").strip().upper())
    before_content = (section_before or {}).get("content")
    before_text = _format_section_preview(heading, before_content or "_Noch offen._")
    after_markdown = apply_operation(
        raw_markdown,
        strategy="append_log_entry" if strategy == "append_log_entry" else "append_section_note",
        section_name=target_section,
        content=patch_text,
        heading_label=heading,
    )
    after_sections = parse_sections(after_markdown)
    section_after = after_sections.get(str(target_section or "").strip().upper())
    after_text = _format_section_preview(heading, (section_after or {}).get("content") or patch_text)
    return before_text, after_text


def build_patch_preview(*, target_memory_file: str, target_section: str, strategy: str, patch_text: str) -> dict[str, str]:
    before_text, after_text = _preview_for_content(target_memory_file, target_section, strategy, patch_text)
    return {"preview_before": before_text, "preview_after": after_text}


def _is_sensitive_health_patch(target_memory_file: str, patch_text: str) -> bool:
    if target_memory_file != "health_notes":
        return False
    tokens = _token_set(patch_text)
    return any(token in tokens for token in SENSITIVE_TOKENS)


def _compute_patch_safety(patch: dict[str, Any]) -> tuple[str, list[str]]:
    target_memory_file = str(patch.get("target_memory_file") or "").strip()
    target_section = str(patch.get("target_section") or "").strip()
    strategy = _normalize_patch_strategy(patch.get("strategy"))
    patch_text = str(patch.get("patch_text") or "").strip()
    evidence = patch.get("evidence") if isinstance(patch.get("evidence"), dict) else {}
    timeframe = str(patch.get("timeframe") or evidence.get("timeframe") or "").strip().lower()
    conflict_status = str(patch.get("conflict_status") or evidence.get("conflict_status") or "").strip().lower()
    evidence_strength = float(evidence.get("evidence_strength") or patch.get("evidence_strength") or 0.0)
    proposed_strategy = str(evidence.get("proposed_strategy") or "").strip().lower()
    reasons: list[str] = []
    if target_memory_file == "master_profile":
        reasons.append("master_profile bleibt grundsätzlich blockiert.")
    if not _known_memory_file(target_memory_file):
        reasons.append("Ziel-Memory-Datei ist unbekannt.")
    if strategy not in PATCH_STRATEGIES:
        reasons.append("Strategie ist nicht für append-only Patches freigegeben.")
    if strategy == "manual_review_only":
        reasons.append("manual_review_only bleibt nicht applybar.")
    elif strategy not in {"append_section_note", "append_log_entry"}:
        reasons.append("Nur append_section_note oder append_log_entry sind applybar.")
    if not target_section:
        reasons.append("Ziel-Section fehlt.")
    elif target_memory_file in TARGET_SECTIONS:
        try:
            _validate_target(target_memory_file, target_section)
        except Exception:
            reasons.append("Ziel-Section ist unbekannt.")
    if not patch_text:
        reasons.append("Patch-Text fehlt.")
    if len(patch_text) > 4000:
        reasons.append("Patch-Text ist zu lang.")
    if proposed_strategy == "replace_section_candidate":
        reasons.append("Replace-Kandidaten bleiben blockiert.")
    if _is_sensitive_health_patch(target_memory_file, patch_text):
        reasons.append("Health-Notizen mit sensibler Sprache bleiben blockiert.")
    if reasons:
        return "blocked", reasons
    review_reasons: list[str] = []
    if target_memory_file in {"athlete_dossier", "health_notes"}:
        review_reasons.append(f"{target_memory_file} braucht manuelles Review.")
    if timeframe in {"year", "global"}:
        review_reasons.append("Langfristige Zeiträume brauchen Review.")
    if evidence_strength < 0.6:
        review_reasons.append("Evidenz ist nicht stark genug für Auto-Ready.")
    if conflict_status in {"needs_review", "weak"}:
        review_reasons.append("Konfliktstatus verlangt Review.")
    if review_reasons:
        return "needs_review", review_reasons
    if (
        target_memory_file in {"core_patterns", "training_history", "review_log"}
        and strategy in {"append_section_note", "append_log_entry"}
        and conflict_status == "ready_to_apply"
        and evidence_strength >= 0.6
    ):
        return "ready", []
    return "needs_review", ["Patch braucht eine manuelle Sichtprüfung."]


def _load_eligible_consolidations(
    *,
    target_memory_file: str | None = None,
    target_section: str | None = None,
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    ensure_core_memory_consolidations_schema()
    allowed_statuses = {str(item or "").strip().lower() for item in (status_filter or ["applied", "promoted"]) if str(item or "").strip()}
    if not allowed_statuses:
        allowed_statuses = {"applied", "promoted"}
    conn = get_core_db()
    try:
        originals = {
            int(row["id"])
            for row in conn.execute("SELECT id FROM core_memory_consolidations").fetchall()
        }
        rows = conn.execute("SELECT * FROM core_memory_consolidations ORDER BY updated_at DESC, id DESC").fetchall()
    finally:
        conn.close()
    eligible: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        status = str(item.get("status") or "").strip().lower()
        conflict_status = str(item.get("conflict_status") or "").strip().lower()
        evidence_strength = float(item.get("evidence_strength") or 0.0)
        duplicate_of = item.get("duplicate_of_consolidation_id")
        if status not in allowed_statuses:
            continue
        if status == "dismissed":
            continue
        if conflict_status not in {"ready_to_apply", "needs_review"}:
            continue
        if evidence_strength < 0.45:
            continue
        if duplicate_of and int(duplicate_of) in originals:
            continue
        if str(item.get("target_memory_file") or "").strip() == "":
            continue
        if str(item.get("target_section") or "").strip() == "":
            continue
        if target_memory_file and str(item.get("target_memory_file") or "").strip() != str(target_memory_file).strip():
            continue
        if target_section and str(item.get("target_section") or "").strip() != str(target_section).strip():
            continue
        eligible.append(item)
    return eligible


def _group_consolidations(consolidations: list[dict[str, Any]], min_consolidations: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for item in consolidations:
        key = (
            str(item.get("target_memory_file") or "").strip(),
            str(item.get("target_section") or "").strip(),
            str(item.get("timeframe") or "").strip().lower(),
            str(item.get("category") or "").strip().lower(),
        )
        buckets.setdefault(key, []).append(item)
    return [bucket for bucket in buckets.values() if len(bucket) >= min_consolidations]


def _create_or_update_patch(conn: sqlite3.Connection, draft: dict[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    now = _utc_iso()
    patch_key = str(draft.get("patch_key") or "").strip() or None
    row = conn.execute("SELECT * FROM core_memory_file_patches WHERE patch_key=? LIMIT 1", (patch_key,)).fetchone() if patch_key else None
    created = row is None
    if row:
        current = _row_to_patch(row)
        current_status = str(current.get("status") or "").strip().lower()
        if current_status == "dismissed":
            return current, False, False
        conn.execute(
            """
            UPDATE core_memory_file_patches
            SET updated_at=?, target_memory_file=?, target_section=?, strategy=?, title=?, patch_text=?,
                preview_before=?, preview_after=?, source_consolidation_ids_json=?, evidence_json=?,
                safety_status=?, safety_reasons_json=?
            WHERE id=?
            """,
            (
                now,
                draft.get("target_memory_file"),
                draft.get("target_section"),
                draft.get("strategy"),
                draft.get("title"),
                draft.get("patch_text"),
                draft.get("preview_before"),
                draft.get("preview_after"),
                _json_dumps(draft.get("source_consolidation_ids"), []),
                _json_dumps(draft.get("evidence"), {}),
                draft.get("safety_status"),
                _json_dumps(draft.get("safety_reasons"), []),
                int(row["id"]),
            ),
        )
        patch_id = int(row["id"])
        append_memory_review_log(
            "memory_file_patch",
            patch_id,
            "update_patch",
            previous_status=current.get("status"),
            new_status=current.get("status"),
            actor="system",
            payload={"patch_key": patch_key, "source_consolidation_ids": draft.get("source_consolidation_ids", [])},
            conn=conn,
        )
        return _row_to_patch(conn.execute("SELECT * FROM core_memory_file_patches WHERE id=?", (patch_id,)).fetchone()), False, True
    else:
        cur = conn.execute(
            """
            INSERT INTO core_memory_file_patches (
                created_at, updated_at, patch_key, status, target_memory_file, target_section, strategy, title, patch_text,
                preview_before, preview_after, source_consolidation_ids_json, evidence_json, safety_status, safety_reasons_json,
                applied_at, applied_result_json, dismissed_at, dismissed_reason
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', NULL, NULL)
            """,
            (
                now,
                now,
                patch_key,
                draft.get("target_memory_file"),
                draft.get("target_section"),
                draft.get("strategy"),
                draft.get("title"),
                draft.get("patch_text"),
                draft.get("preview_before"),
                draft.get("preview_after"),
                _json_dumps(draft.get("source_consolidation_ids"), []),
                _json_dumps(draft.get("evidence"), {}),
                draft.get("safety_status"),
                _json_dumps(draft.get("safety_reasons"), []),
            ),
        )
        patch_id = int(cur.lastrowid)
        append_memory_review_log(
            "memory_file_patch",
            patch_id,
            "create_patch",
            previous_status=None,
            new_status="draft",
            actor="system",
            payload={"patch_key": patch_key, "source_consolidation_ids": draft.get("source_consolidation_ids", [])},
            conn=conn,
        )
        return _row_to_patch(conn.execute("SELECT * FROM core_memory_file_patches WHERE id=?", (patch_id,)).fetchone()), True, True


def build_memory_file_patch_drafts(
    *,
    target_memory_file: str | None = None,
    target_section: str | None = None,
    status_filter: list[str] | None = None,
    min_consolidations: int = 2,
) -> dict[str, Any]:
    ensure_core_memory_file_patches_schema()
    ensure_core_memory_review_log_schema()
    patch_date = datetime.now(timezone.utc).date().isoformat()
    consolidations = _load_eligible_consolidations(
        target_memory_file=target_memory_file,
        target_section=target_section,
        status_filter=status_filter,
    )
    groups = _group_consolidations(consolidations, max(1, min(8, int(min_consolidations or 2))))
    created = 0
    updated = 0
    rows: list[dict[str, Any]] = []
    conn = get_core_db()
    try:
        for group in groups:
            token_counter: Counter[str] = Counter()
            for item in group:
                token_counter.update(set(_tokenize(f"{item.get('title') or ''} {item.get('summary') or ''}")))
            top_tokens = [token for token, count in token_counter.most_common(6) if count >= 1]
            source_ids = sorted({int(item["id"]) for item in group if item.get("id") is not None})
            target_file = str(group[0].get("target_memory_file") or "").strip()
            target_sec = str(group[0].get("target_section") or "").strip()
            timeframe = str(group[0].get("timeframe") or "").strip().lower()
            category = str(group[0].get("category") or "").strip().lower()
            title = _build_patch_title(group, top_tokens, target_sec)
            strategy = _normalize_patch_strategy(group[0].get("proposed_strategy") or "append_section_note")
            if strategy == "replace_section_candidate":
                strategy = "manual_review_only"
            patch_text = _build_patch_text(
                patch_date=patch_date,
                title=title,
                timeframe=timeframe,
                consolidations=group,
            )
            preview = build_patch_preview(
                target_memory_file=target_file,
                target_section=target_sec,
                strategy="append_log_entry" if strategy == "append_log_entry" else "append_section_note",
                patch_text=patch_text,
            )
            evidence = {
                "timeframe": timeframe,
                "category": category,
                "conflict_status": str(group[0].get("conflict_status") or "").strip().lower(),
                "evidence_strength": max(float(item.get("evidence_strength") or 0.0) for item in group),
                "proposed_strategy": str(group[0].get("proposed_strategy") or "").strip().lower(),
                "source_titles": [_text(item.get("title"), 120) for item in group if _text(item.get("title"), 120)],
                "top_tokens": top_tokens[:6],
                "source_consolidation_count": len(source_ids),
            }
            draft = {
                "patch_key": _build_patch_key(
                    target_memory_file=target_file,
                    target_section=target_sec,
                    timeframe=timeframe,
                    category=category,
                    top_tokens=top_tokens,
                    source_ids=source_ids,
                ),
                "target_memory_file": target_file,
                "target_section": target_sec,
                "strategy": strategy if strategy in PATCH_STRATEGIES else "manual_review_only",
                "title": title,
                "patch_text": patch_text,
                "preview_before": preview["preview_before"],
                "preview_after": preview["preview_after"],
                "source_consolidation_ids": source_ids,
                "evidence": evidence,
                "timeframe": timeframe,
                "conflict_status": evidence["conflict_status"],
                "evidence_strength": evidence["evidence_strength"],
            }
            safety_status, safety_reasons = _compute_patch_safety(draft)
            draft["safety_status"] = safety_status
            draft["safety_reasons"] = safety_reasons
            row, was_created, was_touched = _create_or_update_patch(conn, draft)
            if was_created:
                created += 1
            elif was_touched:
                updated += 1
            rows.append(row)
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "updated": updated, "patches": rows}


def get_memory_file_patches(
    *,
    status: str | None = None,
    target_memory_file: str | None = None,
    target_section: str | None = None,
    safety_status: str | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    ensure_core_memory_file_patches_schema()
    ensure_core_memory_review_log_schema()
    safe_limit = max(1, min(200, int(limit)))
    params: list[Any] = []
    clauses: list[str] = []
    if status:
        clauses.append("status=?")
        params.append(str(status).strip().lower())
    if target_memory_file:
        clauses.append("target_memory_file=?")
        params.append(str(target_memory_file).strip())
    if target_section:
        clauses.append("target_section=?")
        params.append(str(target_section).strip())
    if safety_status:
        clauses.append("safety_status=?")
        params.append(str(safety_status).strip().lower())
    sql = "SELECT * FROM core_memory_file_patches"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(safe_limit)
    conn = get_core_db()
    try:
        return [_row_to_patch(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def get_memory_file_patch(patch_id: Any) -> dict[str, Any]:
    ensure_core_memory_file_patches_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_file_patches WHERE id=? LIMIT 1", (int(patch_id),)).fetchone()
        if not row:
            raise ValueError("memory_file_patch_not_found")
        return _row_to_patch(row)
    finally:
        conn.close()


def _patch_apply_result(patch: dict[str, Any]) -> dict[str, Any]:
    strategy = str(patch.get("strategy") or "").strip().lower()
    write_strategy = "append_log_entry" if strategy == "append_log_entry" else "append_section_note"
    result = get_memory_service().apply(
        {
            "write_plan": [
                {
                    "logical_file": patch.get("target_memory_file"),
                    "section": patch.get("target_section"),
                    "strategy": write_strategy,
                    "content": patch.get("patch_text"),
                }
            ],
            "reason": f"core_memory_file_patch:{patch.get('patch_key') or patch.get('id')}",
        }
    )
    return {
        "ok": bool(result.get("success")),
        "target_memory_file": patch.get("target_memory_file"),
        "target_section": patch.get("target_section"),
        "strategy": write_strategy,
        "memory_apply": result,
    }


def _validate_patch_apply_allowed(patch: dict[str, Any], confirm: bool = False) -> bool:
    if str(patch.get("status") or "").strip().lower() != "draft":
        raise ValueError("patch_not_draft")
    if str(patch.get("patch_text") or "").strip() == "":
        raise ValueError("empty_patch_text")
    if str(patch.get("target_memory_file") or "").strip() == "master_profile":
        raise ValueError("patch_blocked")
    safety_status = str(patch.get("safety_status") or "").strip().lower()
    if safety_status == "blocked":
        raise ValueError("patch_blocked")
    if safety_status == "needs_review" and not bool(confirm):
        raise ValueError("confirm_required")
    strategy = str(patch.get("strategy") or "").strip().lower()
    if strategy not in {"append_section_note", "append_log_entry"}:
        raise ValueError("invalid_strategy")
    try:
        _validate_target(patch.get("target_memory_file"), patch.get("target_section"))
    except Exception as exc:
        raise ValueError("invalid_target") from exc
    return True


def apply_memory_file_patch(patch_id: Any, *, confirm: bool = False, actor: str = "system", note: str | None = None) -> dict[str, Any]:
    ensure_core_memory_file_patches_schema()
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_file_patches WHERE id=? LIMIT 1", (int(patch_id),)).fetchone()
        if not row:
            raise ValueError("memory_file_patch_not_found")
        patch = _row_to_patch(row)
        _validate_patch_apply_allowed(patch, confirm=confirm)
        applied_result = _patch_apply_result(patch)
        if not applied_result.get("ok"):
            raise ValueError("patch_apply_failed")
        now = _utc_iso()
        conn.execute(
            """
            UPDATE core_memory_file_patches
            SET status='applied', updated_at=?, applied_at=?, applied_result_json=?
            WHERE id=?
            """,
            (now, now, _json_dumps(applied_result, {}), int(patch_id)),
        )
        append_memory_review_log(
            "memory_file_patch",
            patch_id,
            "apply_memory_file_patch",
            previous_status=patch.get("status"),
            new_status="applied",
            note=note,
            actor=actor,
            payload={"confirm": bool(confirm), **applied_result},
            conn=conn,
        )
        conn.commit()
        return _row_to_patch(conn.execute("SELECT * FROM core_memory_file_patches WHERE id=?", (int(patch_id),)).fetchone())
    finally:
        conn.close()


def dismiss_memory_file_patch(patch_id: Any, *, reason: str | None = None, actor: str = "system") -> dict[str, Any]:
    ensure_core_memory_file_patches_schema()
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_file_patches WHERE id=? LIMIT 1", (int(patch_id),)).fetchone()
        if not row:
            raise ValueError("memory_file_patch_not_found")
        patch = _row_to_patch(row)
        now = _utc_iso()
        conn.execute(
            """
            UPDATE core_memory_file_patches
            SET status='dismissed', updated_at=?, dismissed_at=?, dismissed_reason=?
            WHERE id=?
            """,
            (now, now, _text(reason, 400), int(patch_id)),
        )
        append_memory_review_log(
            "memory_file_patch",
            patch_id,
            "dismiss_patch",
            previous_status=patch.get("status"),
            new_status="dismissed",
            reason=reason,
            actor=actor,
            payload={"dismissed_reason": _text(reason, 400)},
            conn=conn,
        )
        conn.commit()
        return _row_to_patch(conn.execute("SELECT * FROM core_memory_file_patches WHERE id=?", (int(patch_id),)).fetchone())
    finally:
        conn.close()


def update_memory_file_patch(patch_id: Any, *, edits: dict[str, Any] | None = None, actor: str = "system", note: str | None = None) -> dict[str, Any]:
    ensure_core_memory_file_patches_schema()
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_file_patches WHERE id=? LIMIT 1", (int(patch_id),)).fetchone()
        if not row:
            raise ValueError("memory_file_patch_not_found")
        patch = _row_to_patch(row)
        updates: dict[str, Any] = {}
        allowed = {"title", "patch_text", "target_memory_file", "target_section", "strategy"}
        for key, value in (edits or {}).items():
            if key not in allowed:
                raise ValueError("invalid_update_field")
            if key in {"title", "target_memory_file", "target_section"}:
                updates[key] = _text(value, 200 if key == "title" else 160) or ""
            elif key == "patch_text":
                updates[key] = sanitize_memory_content(str(value or "").strip())
            elif key == "strategy":
                updates[key] = _normalize_patch_strategy(value)
        merged = {**patch, **updates}
        preview = build_patch_preview(
            target_memory_file=str(merged.get("target_memory_file") or ""),
            target_section=str(merged.get("target_section") or ""),
            strategy=str(merged.get("strategy") or ""),
            patch_text=str(merged.get("patch_text") or ""),
        )
        merged["preview_before"] = preview["preview_before"]
        merged["preview_after"] = preview["preview_after"]
        safety_status, safety_reasons = _compute_patch_safety(merged)
        now = _utc_iso()
        conn.execute(
            """
            UPDATE core_memory_file_patches
            SET updated_at=?, target_memory_file=?, target_section=?, strategy=?, title=?, patch_text=?,
                preview_before=?, preview_after=?, safety_status=?, safety_reasons_json=?
            WHERE id=?
            """,
            (
                now,
                merged.get("target_memory_file"),
                merged.get("target_section"),
                merged.get("strategy"),
                merged.get("title"),
                merged.get("patch_text"),
                merged.get("preview_before"),
                merged.get("preview_after"),
                safety_status,
                _json_dumps(safety_reasons, []),
                int(patch_id),
            ),
        )
        append_memory_review_log(
            "memory_file_patch",
            patch_id,
            "update_patch",
            previous_status=patch.get("status"),
            new_status=patch.get("status"),
            note=note,
            actor=actor,
            payload={"edits": updates, "safety_status": safety_status},
            conn=conn,
        )
        conn.commit()
        return _row_to_patch(conn.execute("SELECT * FROM core_memory_file_patches WHERE id=?", (int(patch_id),)).fetchone())
    finally:
        conn.close()


def control_memory_file_patch(
    patch_id: Any,
    action: str,
    *,
    confirm: bool = False,
    edits: dict[str, Any] | None = None,
    reason: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    action_text = str(action or "").strip().lower()
    if action_text == "apply":
        return apply_memory_file_patch(patch_id, confirm=confirm, actor=actor, note=reason)
    if action_text == "dismiss":
        return dismiss_memory_file_patch(patch_id, reason=reason, actor=actor)
    if action_text == "update":
        return update_memory_file_patch(patch_id, edits=edits, actor=actor, note=reason)
    raise ValueError("invalid_action")
