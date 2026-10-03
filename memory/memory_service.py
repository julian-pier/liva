from __future__ import annotations

import logging
import re
from typing import Any

from memory.errors import MemoryError
from memory.memory_markdown import apply_operation, extract_context_blocks
from memory.memory_models import (
    DEFAULT_CONTEXT_MAX_ITEMS,
    LOGICAL_FILE_SPECS,
    normalize_domain,
    resolve_logical_files_for_domains,
    resolve_section_name,
)
from memory.memory_repository import ensure_memory_schema, latest_successful_operation, list_registry_entries, record_operation, upsert_registry_entry, utc_now_iso
from memory.memory_rules import propose_memory_updates
from memory.memory_storage import MemoryStorage

LOGGER = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, *, storage: MemoryStorage | None = None):
        self.storage = storage or MemoryStorage()

    def get_status(self) -> dict[str, Any]:
        ensure_memory_schema()
        config = self.storage.get_config_status()
        known_entries = {row["logical_name"]: row for row in list_registry_entries()}
        init_status = {
            "root_path": config.get("root_path"),
            "root_exists": bool(config.get("root_exists")),
            "folders": [],
            "logical_files": [],
        }
        if config.get("configured") and config.get("memory_enabled"):
            try:
                init_status = self.initialize_storage(create_missing=False)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("memory status lookup failed")
                config.setdefault("warnings", []).append(str(exc))

        last_sync = latest_successful_operation("apply") or latest_successful_operation("initialize")
        last_archive = latest_successful_operation("archive")

        logical_files: list[dict[str, Any]] = []
        for logical_name, spec in LOGICAL_FILE_SPECS.items():
            init_row = next((row for row in init_status["logical_files"] if row["logical_name"] == logical_name), {})
            registry_row = known_entries.get(logical_name) or {}
            logical_files.append(
                {
                    "logical_name": logical_name,
                    "title": spec.title,
                    "folder_name": spec.folder_name,
                    "filename": spec.filename,
                    "found": bool(init_row.get("found")),
                    "path": init_row.get("path") or registry_row.get("relative_path"),
                    "last_updated": init_row.get("last_updated") or registry_row.get("last_known_modified_at"),
                    "last_synced_at": registry_row.get("last_synced_at"),
                }
            )
        return {
            "enabled": bool(config.get("memory_enabled")),
            "configured": bool(config.get("configured")),
            "storage_mode": config.get("storage_mode"),
            "root_path": init_status["root_path"],
            "root_exists": init_status["root_exists"],
            "folders": init_status["folders"],
            "logical_files": logical_files,
            "last_sync": last_sync["created_at"] if last_sync else None,
            "last_archive": last_archive["created_at"] if last_archive else None,
            "warnings": config.get("warnings") or [],
            "missing_runtime": config.get("missing_runtime") or [],
        }

    def get_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_memory_available()
        status = self.initialize_storage(create_missing=False)
        raw_domains = payload.get("domains") or []
        if isinstance(raw_domains, str):
            raw_domains = [raw_domains]
        domains = [normalize_domain(item) for item in raw_domains if isinstance(item, (str, int, float))]
        domains = [item for item in domains if item]
        logical_files = resolve_logical_files_for_domains(domains)
        if payload.get("include_recent", False) and "review_log" not in logical_files:
            logical_files.append("review_log")

        query = str(payload.get("query") or "").strip().lower()
        max_items = _clamp_int(payload.get("max_items"), default=DEFAULT_CONTEXT_MAX_ITEMS, minimum=1, maximum=20)
        blocks: list[dict[str, Any]] = []

        for logical_name in logical_files:
            file_row = next((item for item in status["logical_files"] if item["logical_name"] == logical_name), None)
            if not file_row or not file_row.get("found"):
                continue
            snapshot = self.storage.read_file(logical_name, create_missing=False)
            for block in extract_context_blocks(snapshot["raw_markdown"]):
                content = _clean_text(block.get("content") or "")
                if not content:
                    continue
                score = _context_score(content, query=query, logical_name=logical_name)
                blocks.append(
                    {
                        "logical_file": logical_name,
                        "file": snapshot["relative_path"],
                        "section": block.get("section") or None,
                        "heading": block.get("heading") or None,
                        "text": _truncate_text(content, 1400),
                        "confidence": score["confidence"],
                        "importance": score["importance"],
                        "last_updated": snapshot["last_updated"],
                        "_score": score["score"],
                    }
                )

        blocks.sort(key=lambda item: item["_score"], reverse=True)
        final_blocks = []
        for item in blocks[:max_items]:
            row = dict(item)
            row.pop("_score", None)
            final_blocks.append(row)
        record_operation("context", status="success", detail={"domains": domains, "count": len(final_blocks)})
        return {
            "domains": domains,
            "query": query or None,
            "include_recent": bool(payload.get("include_recent", False)),
            "memory_blocks": final_blocks,
            "known_files": [item["logical_name"] for item in status["logical_files"]],
        }

    def get_file(self, logical_name: str, *, create_missing: bool = True) -> dict[str, Any]:
        self._ensure_memory_available()
        if logical_name not in LOGICAL_FILE_SPECS:
            raise MemoryError("memory_file_unknown", f"Unknown logical file: {logical_name}", 404)
        snapshot = self.storage.read_file(logical_name, create_missing=create_missing)
        spec = LOGICAL_FILE_SPECS[logical_name]
        upsert_registry_entry(
            logical_name,
            title=spec.title,
            folder_name=spec.folder_name,
            relative_path=snapshot["relative_path"],
            last_known_modified_at=snapshot["last_updated"],
            metadata={"storage_kind": "local_markdown", "path": snapshot["path"]},
        )
        return {
            "logical_name": logical_name,
            "title": spec.title,
            "folder_name": spec.folder_name,
            "filename": spec.filename,
            "path": snapshot["path"],
            "relative_path": snapshot["relative_path"],
            "last_updated": snapshot["last_updated"],
            "raw_markdown": snapshot["raw_markdown"],
            "rendered_html": snapshot["rendered_html"],
        }

    def propose(self, payload: dict[str, Any]) -> dict[str, Any]:
        ensure_memory_schema()
        result = propose_memory_updates(
            conversation_summary=str(payload.get("conversation_summary") or "").strip(),
            extracted_facts=payload.get("extracted_facts") or [],
            candidates=payload.get("candidates") or [],
            dry_run=_to_bool(payload.get("dry_run"), True),
        )
        record_operation(
            "propose",
            status="success",
            detail={"accepted": result["stats"]["accepted_count"], "dry_run": result["dry_run"]},
        )
        return result

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_memory_available()
        ensure_memory_schema()
        self.initialize_storage(create_missing=True)
        write_plan = payload.get("write_plan") or []
        if not isinstance(write_plan, list) or not write_plan:
            raise MemoryError("memory_write_plan_missing", "apply requires a non-empty write_plan.", 400)

        requires_archive = _to_bool(payload.get("requires_archive"), False) or any(
            _to_bool(item.get("requires_archive"), False) for item in write_plan if isinstance(item, dict)
        )
        staged_updates: dict[str, str] = {}
        affected_files: list[str] = []

        for item in write_plan:
            if not isinstance(item, dict):
                raise MemoryError("invalid_write_plan_item", f"Invalid write plan item: {item}", 400)
            logical_name = str(item.get("logical_file") or "").strip()
            if logical_name not in LOGICAL_FILE_SPECS:
                raise MemoryError("unknown_logical_file", logical_name, 400)
            if logical_name not in staged_updates:
                current = self.storage.read_file(logical_name, create_missing=True)
                staged_updates[logical_name] = current["raw_markdown"]
                affected_files.append(logical_name)
            section = resolve_section_name(logical_name, item.get("section"))
            strategy = str(item.get("strategy") or LOGICAL_FILE_SPECS[logical_name].default_strategy).strip()
            content = sanitize_memory_content(item.get("content") or "")
            if not content:
                raise MemoryError("memory_content_missing", f"Missing content for {logical_name}/{section}", 400)
            if section not in LOGICAL_FILE_SPECS[logical_name].sections:
                allowed = ", ".join(LOGICAL_FILE_SPECS[logical_name].sections)
                raise MemoryError(
                    "memory_section_unknown",
                    f"Unknown section '{item.get('section')}' for {logical_name}. Allowed: {allowed}",
                    400,
                )
            heading_label = LOGICAL_FILE_SPECS[logical_name].section_labels.get(section)
            staged_updates[logical_name] = apply_operation(
                staged_updates[logical_name],
                strategy=strategy,
                section_name=section,
                content=content,
                heading_label=heading_label,
            )

        snapshots: list[dict[str, Any]] = []
        if requires_archive:
            archive_result = self.archive(
                {
                    "logical_files": sorted(set(affected_files)),
                    "reason": payload.get("reason") or "pre_apply_snapshot",
                }
            )
            snapshots = archive_result["snapshots"]

        updated_files: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for logical_name in affected_files:
            try:
                file_info = self.storage.write_file(logical_name, staged_updates[logical_name])
                now = utc_now_iso()
                upsert_registry_entry(
                    logical_name,
                    title=LOGICAL_FILE_SPECS[logical_name].title,
                    folder_name=LOGICAL_FILE_SPECS[logical_name].folder_name,
                    relative_path=file_info["relative_path"],
                    last_known_modified_at=file_info.get("last_updated"),
                    last_synced_at=now,
                    metadata={"storage_kind": "local_markdown", "path": file_info["path"]},
                )
                updated_files.append(
                    {
                        "logical_file": logical_name,
                        "path": file_info["path"],
                        "relative_path": file_info["relative_path"],
                        "updated_at": file_info["last_updated"] or now,
                    }
                )
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("memory apply failed for %s", logical_name)
                errors.append({"error": "memory_apply_failed", "logical_file": logical_name, "detail": str(exc)})
                break

        success = not errors
        record_operation(
            "apply",
            status="success" if success else "failed",
            detail={"updated_count": len(updated_files), "error_count": len(errors), "requires_archive": requires_archive},
        )
        return {
            "success": success,
            "updated_files": updated_files,
            "snapshots": snapshots,
            "errors": errors,
        }

    def archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_memory_available()
        ensure_memory_schema()
        self.initialize_storage(create_missing=True)
        if "logical_files" in payload:
            requested = payload.get("logical_files")
            if requested is None:
                requested = []
            if isinstance(requested, str):
                requested = [requested]
        else:
            requested = list(LOGICAL_FILE_SPECS.keys())
        logical_files = [name for name in requested if isinstance(name, str) and name in LOGICAL_FILE_SPECS]
        snapshots = self.storage.archive_files(logical_files, reason=str(payload.get("reason") or "").strip())
        record_operation(
            "archive",
            status="success",
            detail={"count": len(snapshots), "reason": str(payload.get("reason") or "").strip()},
        )
        return {"success": True, "snapshots": snapshots}

    def initialize_storage(self, *, create_missing: bool) -> dict[str, Any]:
        ensure_memory_schema()
        config = self.storage.get_config_status()
        if not config.get("memory_enabled"):
            raise MemoryError("memory_disabled", "Set MEMORY_ENABLED=1 to use the memory module.", 503)
        payload = self.storage.initialize_storage(create_missing=create_missing)
        for row in payload["logical_files"]:
            if row["found"]:
                upsert_registry_entry(
                    row["logical_name"],
                    title=row["title"],
                    folder_name=row["folder_name"],
                    relative_path=row["relative_path"],
                    last_known_modified_at=row["last_updated"],
                    metadata={"storage_kind": "local_markdown", "path": row["path"]},
                )
        if create_missing:
            record_operation("initialize", status="success", detail={"root_path": payload["root_path"]})
        return payload

    def _ensure_memory_available(self) -> None:
        config = self.storage.get_config_status()
        if not config.get("memory_enabled"):
            raise MemoryError("memory_disabled", "Set MEMORY_ENABLED=1 to use memory endpoints.", 503)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _clean_text(value: str) -> str:
    lines = [line.rstrip() for line in str(value or "").replace("\r\n", "\n").splitlines()]
    lines = [line for line in lines if line and line != "_Noch offen._"]
    return "\n".join(lines).strip()


MEMORY_ENTRY_PREFIX_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[^\]]*(?:mem_[0-9A-Za-z_-]+|memory_[0-9A-Za-z_-]+)[^\]]*\]\s*)?"
    r"(?:\((?:low|normal|medium|high|unknown|confidence\s*[:=]?\s*[0-9.]+|conf(?:idence)?\s*[:=]?\s*[0-9.]+|[0-9.]+)\)\s*)+",
    re.IGNORECASE,
)
MEMORY_ID_ONLY_PREFIX_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[[^\]]*(?:mem_[0-9A-Za-z_-]+|memory_[0-9A-Za-z_-]+)[^\]]*\]\s*",
    re.IGNORECASE,
)


def sanitize_memory_content(value: Any) -> str:
    """Strip GPT/tool metadata prefixes from memory text before persistence."""
    lines = str(value or "").replace("\r\n", "\n").splitlines()
    cleaned: list[str] = []
    for line in lines:
        text = MEMORY_ENTRY_PREFIX_RE.sub("", line).strip()
        text = MEMORY_ID_ONLY_PREFIX_RE.sub("", text).strip()
        cleaned.append(text)
    return "\n".join(cleaned).strip()


def _context_score(content: str, *, query: str, logical_name: str) -> dict[str, Any]:
    score = float(LOGICAL_FILE_SPECS[logical_name].read_priority)
    if query:
        terms = [token for token in query.split() if token]
        hits = sum(1 for token in terms if token in content.lower())
        score += hits * 20.0
    importance = "medium"
    if logical_name in {"master_profile", "athlete_dossier", "health_notes"}:
        importance = "high"
        score += 8.0
    confidence = round(min(0.99, max(0.45, score / 120.0)), 2)
    return {"score": score, "importance": importance, "confidence": confidence}


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
