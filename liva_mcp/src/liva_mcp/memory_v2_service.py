"""Thin, typed boundary between MCP and the LIVA Memory V2 core."""
from __future__ import annotations

import hashlib
import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .config import Config
from .intent_routing import LIVE_ROUTES, route_for_query

logger = logging.getLogger("liva_mcp.memory_v2")


def _strip_md(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


def _clean_event_title(title: str, event_date: str | None) -> str:
    title=title.strip()
    title=re.sub(r"^\d{4}-\d{2}-\d{2}\s*[–—-]\s*", "", title).strip()
    return title or (event_date or "Ereignis")


def _safe_filename(title: str) -> str:
    filename=re.sub(r'[<>:"/\\|?*\x00-\x1f]', " – ", title)
    filename=re.sub(r"\s+", " ", filename).strip(" .")
    if not filename: raise MemoryV2ToolError("INVALID_CHANGE", "A canonical filename could not be derived from the note title.")
    return filename


def _without_generated_heading(text: str, title: str) -> str:
    """The Memory core owns the page H1; accept a matching model-supplied one."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# ") and lines[0][2:].strip().casefold() == title.strip().casefold():
        return "\n".join(lines[1:]).strip()
    return text.strip()


def _parent_candidates(vault: Path, links: list[dict[str, Any]], note_type: str, event_date: str | None) -> list[Path]:
    candidates: list[Path]=[]
    for link in links:
        raw=link.get("path") if isinstance(link,dict) else None
        if not isinstance(raw,str) or not raw.startswith("wiki/") or not raw.endswith(".md"): continue
        target=Path(raw); parent=target.parent; stem=target.stem
        candidate=None
        if len(target.parts)>=4 and target.parts[:2]==("wiki","Menschen") and stem==target.parts[2]: candidate=Path(*target.parts[:3])
        elif note_type=="event" and len(target.parts)==3 and target.parts[:2]==("wiki","Mein Leben") and re.fullmatch(r"20\d{2}",stem): candidate=parent/stem/"Episoden"
        elif stem==parent.name: candidate=parent
        elif target.parts[:2]==("wiki","Ich") and len(target.parts)==3 and (vault/parent/stem).is_dir(): candidate=parent/stem
        if candidate is not None and candidate not in candidates: candidates.append(candidate)
    return candidates


def _canonicalize_create(vault: Path, change: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    item=deepcopy(change); note_type=item.get("note_type"); links=item.get("links")
    requested=item.get("path"); raw_title=item.get("title"); event_date=item.get("event_date")
    if not isinstance(requested,str) or not isinstance(raw_title,str): raise MemoryV2ToolError("INVALID_CHANGE", "create_page requires path and title.")
    requested_parts=Path(requested)
    if requested_parts.is_absolute() or ".." in requested_parts.parts or requested_parts.suffix!=".md" or not requested.startswith(("wiki/Ich/","wiki/Menschen/","wiki/Training & Körper/","wiki/Projekte/","wiki/Mein Leben/","wiki/Erinnerungsarchiv/")):
        raise MemoryV2ToolError("INVALID_PATH", "The requested knowledge path is outside the allowed Memory V2 area.")
    if note_type not in {"hub","event","insight","topic","archive"}: raise MemoryV2ToolError("INVALID_NOTE_TYPE", "note_type must be hub, event, insight, topic, or archive.")
    if note_type=="event" and event_date is not None and (not isinstance(event_date,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",event_date)): raise MemoryV2ToolError("INVALID_EVENT_DATE", "An event_date, if documented, requires YYYY-MM-DD.")
    if note_type!="event" and event_date is not None: raise MemoryV2ToolError("INVALID_EVENT_DATE", "Only an event may include event_date.")
    if not isinstance(links,list) or not 1<=len(links)<=6:
        raise MemoryV2ToolError("MISSING_PARENT_LINKS", "Every new Living Wiki page requires 1-6 explicit semantic links. Include its main hub or parent; unconnected pages are rejected.")
    if any(not isinstance(link,dict) or not isinstance(link.get("path"),str) or not link["path"].startswith("wiki/") or not link["path"].endswith(".md") for link in links):
        raise MemoryV2ToolError("INVALID_PARENT_LINK", "Every create_page link must be an explicit Living Wiki .md path.")
    title=raw_title.strip()
    if note_type=="event": title=_clean_event_title(title,event_date)
    item["title"]=title
    candidates=_parent_candidates(vault,links,note_type,event_date)
    requested_parent=Path(requested).parent
    if note_type=="hub": canonical=Path(requested)
    else:
        if not candidates:
            raise MemoryV2ToolError("INVALID_PARENT_LINK", "No canonical parent hub could be derived from the supplied links. Link the page to its real person, topic, project, training or year hub.")
        person=item.get("person")
        person_parent=Path("wiki/Menschen")/person if isinstance(person,str) and person.strip() else None
        matching=[candidate for candidate in candidates if requested_parent==candidate]
        if person_parent is not None:
            if person_parent not in candidates: raise MemoryV2ToolError("INVALID_PARENT_LINK", "A person event must link its matching person hub.")
            parent=person_parent
        elif matching: parent=matching[0]
        elif len(candidates)==1: parent=candidates[0]
        else:
            raise MemoryV2ToolError("AMBIGUOUS_PARENT", "Several possible parent branches were supplied. Mark the real main hub by placing the page in that hub's canonical folder or remove unrelated structural links from create_page.")
        filename=(f"{event_date} – {_safe_filename(title)}" if note_type=="event" and event_date else _safe_filename(title))+".md"
        canonical=parent/filename
    item["path"]=canonical.as_posix()
    if note_type=="event" and len(canonical.parts)>=4 and canonical.parts[:2]==("wiki","Menschen"):
        item["person"]=canonical.parts[2]
    return item,{"requested_path":requested,"canonical_path":item["path"],"changed":requested!=item["path"]}


def _prepare_living_changes(vault: Path, changes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    prepared=[]; routing=[]; path_map={}
    for change in changes:
        if change.get("action")=="create_page":
            item,decision=_canonicalize_create(vault,change); path_map[change["path"]]=item["path"]; routing.append(decision); prepared.append(item)
        else: prepared.append(deepcopy(change))
    for item in prepared:
        if isinstance(item.get("links"),list):
            for link in item["links"]:
                if link.get("path") in path_map: link["path"]=path_map[link["path"]]
    return prepared,routing

if TYPE_CHECKING:
    from liva_memory import MemoryStore


class MemoryV2ToolError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _error_code(message: str) -> str:
    normalized = message.casefold()
    if "source hash mismatch" in normalized or "content hash mismatch" in normalized:
        return "SOURCE_INTEGRITY_ERROR"
    if "stale_context" in normalized:
        return "STALE_CONTEXT"
    if "unknown source" in normalized:
        return "UNKNOWN_SOURCE"
    if "inbox file not found" in normalized:
        return "FILE_NOT_FOUND"
    if "invalid inbox filename" in normalized:
        return "INVALID_FILENAME"
    if "invalid import source_kind" in normalized:
        return "INVALID_FILE_TYPE"
    if "already imported" in normalized or "already ingested" in normalized:
        return "ALREADY_IMPORTED"
    if "claim evidence" in normalized or "evidence incompatible" in normalized:
        return "CLAIM_CONTRACT_ERROR"
    if "invalid knowledge path" in normalized or "outside allowed knowledge area" in normalized:
        return "INVALID_PATH"
    if "human note type" in normalized:
        return "INVALID_NOTE_TYPE"
    if "event date" in normalized:
        return "INVALID_EVENT_DATE"
    if "invalid modify" in normalized or "invalid create" in normalized or "knowledge operation" in normalized or "markdown h1" in normalized:
        return "INVALID_CHANGE"
    if "idempotency_conflict" in normalized:
        return "IDEMPOTENCY_CONFLICT"
    if "historical event" in normalized:
        return "HISTORICAL_EVENT_IMMUTABLE"
    if "duplicate_page" in normalized:
        return "DUPLICATE_PAGE"
    if "duplicate_content" in normalized:
        return "DUPLICATE_CONTENT"
    if "memory_preflight_failed" in normalized or "broken_link" in normalized:
        return "PREFLIGHT_FAILED"
    if "structural link" in normalized:
        return "INVALID_LINK"
    if "database is locked" in normalized or "disk i/o" in normalized or "read-only" in normalized:
        return "PERSISTENCE_ERROR"
    if "missing_parent_links" in normalized:
        return "MISSING_PARENT_LINKS"
    return "TRANSACTION_FAILED"


class MemoryV2Service:
    """No business logic: the installed liva_memory package remains authoritative."""

    def __init__(self, config: Config) -> None:
        try:
            from liva_memory import MemoryStore, StoreError
        except ImportError as exc:
            raise MemoryV2ToolError("MEMORY_CORE_UNAVAILABLE", "The Memory V2 core package is not installed for this MCP deployment.") from exc
        self.store: MemoryStore = MemoryStore(config.memory_vault_path(), config.memory_runtime_path())
        self._store_error = StoreError

    def _call(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except self._store_error as exc:
            code = getattr(exc, "code", None) or _error_code(str(exc))
            details = dict(getattr(exc, "details", {}) or {})
            if not details and code in {"SOURCE_INTEGRITY_ERROR", "PERSISTENCE_ERROR"}:
                details = {"phase": "source_read" if code == "SOURCE_INTEGRITY_ERROR" else "persistence", "retryable": False}
            if details:
                details.setdefault("operation", getattr(operation, "__name__", "unknown"))
                details.setdefault("retryable", bool(getattr(exc, "retryable", False)))
            logger.warning("memory_v2_core_rejected operation=%s exception_type=%s message=%s", getattr(operation, "__name__", "unknown"), exc.__class__.__name__, str(exc))
            messages = {
                "STALE_CONTEXT": "The selected section changed. Read context again and retry with its new target_ref.",
                "UNKNOWN_SOURCE": "The requested source does not exist in the Memory V2 source store.",
                "FILE_NOT_FOUND": "The requested file does not exist in the vault _inbox.",
                "INVALID_FILENAME": "filename must be a single .md or .txt basename inside _inbox.",
                "INVALID_FILE_TYPE": "source_kind is not supported for file import.",
                "ALREADY_IMPORTED": "This file was already imported.",
                "CLAIM_CONTRACT_ERROR": "Claims are incompatible with the source evidence contract.",
                "INVALID_PATH": "The requested knowledge path is outside the allowed Memory V2 area.",
                "INVALID_NOTE_TYPE": "note_type must be hub, event, insight, topic, or archive.",
                "INVALID_EVENT_DATE": "An event date must use YYYY-MM-DD; an undated event must explicitly use event_date_precision unknown or approximate.",
                "INVALID_CHANGE": "The requested change is incomplete or violates the Human Note structure.",
                "IDEMPOTENCY_CONFLICT": "This idempotency key was already used for a different commit payload.",
                "HISTORICAL_EVENT_IMMUTABLE": "Historical event prose is immutable. Use ensure_links when only verified outgoing links are missing; create a new event note only for a later factual development.",
                "DUPLICATE_PAGE": "A canonical page with the same identity already exists. Update or link the existing page instead of creating a duplicate.",
                "DUPLICATE_CONTENT": "The same correction or durable content is already present; no duplicate was written.",
                "PREFLIGHT_FAILED": "The Memory change failed structural validation before any knowledge file was written.",
                "MISSING_PARENT_LINKS": "Every new Human Note must be connected to 1-6 explicit Living Wiki parents or related nodes.",
                "SOURCE_INTEGRITY_ERROR": "The immutable source failed its integrity check. Do not retry the same finish until the source can be read successfully.",
                "INVALID_LINK": "A requested Living Wiki link is missing, unsafe, or not part of the same transaction.",
                "CONCURRENT_MODIFICATION": "The selected knowledge changed concurrently. Read fresh context and retry once with new target references.",
                "PERSISTENCE_ERROR": "The Memory V2 persistence layer failed. Retry only when the response marks the operation retryable.",
                "TRANSACTION_ROLLED_BACK": "The Memory V2 transaction failed and all staged changes were rolled back.",
                "ROLLBACK_FAILED": "The Memory V2 transaction failed and rollback could not be fully verified. Do not retry automatically.",
                "VALIDATION_ERROR": "The requested Memory V2 changes failed validation before a verified commit.",
            }
            diagnostic_codes = {"SOURCE_INTEGRITY_ERROR", "STALE_CONTEXT", "PERSISTENCE_ERROR", "TRANSACTION_ROLLED_BACK", "ROLLBACK_FAILED"}
            raise MemoryV2ToolError(code, messages.get(code, "The Memory V2 transaction failed safely."), details if code in diagnostic_codes else None) from None
        except OSError as exc:
            raise MemoryV2ToolError("MEMORY_RUNTIME_UNAVAILABLE", "The Memory V2 vault or runtime state is not writable by the MCP service.") from None

    def context(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.store.memory_context, raw["queries"], raw.get("layers", "auto"), raw.get("limit", 8), raw.get("max_chars", 24000))

    def source(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.store.memory_source, raw["source_id"], raw.get("offset", 0), raw.get("max_chars", 12000))

    def pending(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        return self._call(self.store.memory_pending, raw.get("kind"), raw.get("limit", 50))

    def ingest_file(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.store.memory_ingest_file, raw["filename"], raw.get("source_kind", "conversation_export"), raw.get("title"))

    def commit(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.store.memory_commit, raw)

    def recall(self, raw: dict[str, Any]) -> dict[str, Any]:
        route = route_for_query(raw["query"])
        if route and route["intent"] in LIVE_ROUTES:
            # Memory V2 deliberately never reads a live database.  Returning an
            # authority redirect prevents a historical number from becoming a
            # plausible-looking current answer when this tool is called anyway.
            return {
                "answer_context": [],
                "sources_available": False,
                "routing": {**route, "memory_called": False, "reason": "Current structured LIVA data is canonical; Memory V2 is non-canonical for this request."},
            }
        scope = raw.get("scope", "auto")
        layers = {"auto": "auto", "personal": "living_wiki", "procedures": "procedures", "historical": "living_wiki"}.get(scope)
        if layers is None:
            raise MemoryV2ToolError("INVALID_SCOPE", "scope must be auto, personal, procedures, or historical.")
        depth = raw.get("depth", "normal")
        if depth not in {"brief", "normal", "deep"}:
            raise MemoryV2ToolError("INVALID_DEPTH", "depth must be brief, normal, or deep.")
        limit, maximum = {"brief": (3, 6000), "normal": (8, 24000), "deep": (16, 60000)}[depth]
        result = self._call(self.store.memory_context, [raw["query"]], layers, limit, maximum)
        return {"answer_context": [{"title": item["title"], "path": item["path"], "section_ref": item["section_ref"], "text": item["exact_text"], "text_chars": item["text_chars"], "text_complete": item["text_complete"], "temporal_status": "documented"} for item in result["results"]], "sources_available": bool(result["results"]), "routing": route or {"intent": "MEMORY", "canonical_tool": "liva_memory_recall", "memory_called": True}}

    def audit(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded, read-only structural report for the canonical vault."""
        scope = raw.get("scope", "vault")
        paths = raw.get("paths")
        if scope not in {"vault", "paths"}:
            raise MemoryV2ToolError("INVALID_SCOPE", "scope must be vault or paths.")
        if scope == "vault" and paths is not None:
            raise MemoryV2ToolError("INVALID_PATHS", "paths are only allowed with scope=paths.")
        if scope == "paths" and (not isinstance(paths, list) or not paths):
            raise MemoryV2ToolError("INVALID_PATHS", "scope=paths requires one or more canonical Living Wiki paths.")

        selected: list[str] | None = None
        if paths is not None:
            selected = []
            vault = self.store.vault.resolve()
            for path in paths:
                if not isinstance(path, str) or not path.startswith("wiki/") or not path.endswith(".md"):
                    raise MemoryV2ToolError("INVALID_PATH", "Audit paths must be canonical wiki/*.md paths.")
                relative = Path(path)
                if relative.is_absolute() or ".." in relative.parts:
                    raise MemoryV2ToolError("INVALID_PATH", "Audit paths must not be absolute or traverse the vault.")
                candidate = (vault / relative).resolve()
                if not candidate.is_relative_to(vault) or not candidate.is_file():
                    raise MemoryV2ToolError("UNKNOWN_PATH", "Audit paths must identify existing files in the canonical vault.")
                selected.append(relative.as_posix())
            selected = list(dict.fromkeys(selected))

        report = self._call(self.store.memory_health, selected)
        maximum = raw.get("max_issues", 100)
        issues = report["issues"][:maximum]
        return {
            "status": report["status"],
            "scope": scope,
            "checked_paths": report["checked_paths"],
            "error_count": report["error_count"],
            "warning_count": report["warning_count"],
            "issues": issues,
            "issues_returned": len(issues),
            "issues_truncated": len(issues) < len(report["issues"]),
            "total_issues": len(report["issues"]),
        }

    @staticmethod
    def _begin_key(raw: dict[str, Any]) -> str:
        canonical = "\x1f".join(str(raw.get(key, "")) for key in ("text", "kind", "event_date", "temporal_scope", "context_queries"))
        return "facade-begin-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]

    @staticmethod
    def _layers_for(kind: str) -> tuple[str, str]:
        return {"auto": ("living_wiki", "until_changed"), "personal": ("living_wiki", "until_changed"), "procedure": ("procedures", "until_changed"), "historical": ("living_wiki", "historical"), "raw_only": ("living_wiki", "historical")}[kind]

    def begin(self, raw: dict[str, Any]) -> dict[str, Any]:
        kind = raw.get("kind", "auto")
        if kind not in {"auto", "personal", "procedure", "historical", "raw_only"}:
            raise MemoryV2ToolError("INVALID_KIND", "kind must be auto, personal, procedure, historical, or raw_only.")
        route, default_scope = self._layers_for(kind)
        temporal_scope = raw.get("temporal_scope") or default_scope
        if temporal_scope not in {"historical", "until_changed", "temporary"}:
            raise MemoryV2ToolError("INVALID_TEMPORAL_SCOPE", "temporal_scope is invalid.")
        text = raw["text"].strip()
        payload = {"idempotency_key": self._begin_key(raw), "source": {"title": "ChatGPT Memory Capture", "direct_user_context": text, "assistant_synthesis": "No assistant synthesis was provided; direct user context is authoritative.", "event_date": raw.get("event_date"), "temporal_scope": temporal_scope, "topics": [kind]}, "claims": [], "knowledge_operations": [], "compile_result": {"state": "raw_only"}}
        stored = self._call(self.store.memory_commit, payload)
        queries = raw.get("context_queries") or [text]
        context = self._call(self.store.memory_context, queries, route if kind != "raw_only" else "auto", 8, 24000)
        candidates=[{"title": item["title"], "path":item["path"], "heading":item["heading"], "section_ref": item["section_ref"], "text": item["exact_text"], "score":item["score"]} for item in context["results"]]
        # This is only typed retrieval structure. The assistant, not the server,
        # decides whether new information is an event, insight, topic, archive or hub.
        typed={"hub":[],"event":[],"insight":[]}
        for item in context["results"]:
            try: raw_note=(self.store.vault/item["path"]).read_text(encoding="utf-8")
            except OSError: continue
            match=re.search(r"(?m)^type:\s*(hub|event|insight)\s*$",raw_note)
            if match: typed[match.group(1)].append({"title":item["title"],"path":item["path"],"section_ref":item["section_ref"]})
        candidate_paths=list(dict.fromkeys(item["path"] for item in context["results"] if item["layer"]=="living_wiki"))
        health=self._call(self.store.memory_health,candidate_paths) if candidate_paths else {"status":"clean","checked_paths":[],"issues":[],"error_count":0,"warning_count":0}
        gardener={"suggested_note_type":None,"split_candidates":[],"relevant_hubs":typed["hub"],"relevant_events":typed["event"],"relevant_insights":typed["insight"],"folder_patterns":{"person_hub":"wiki/Menschen/<Person>/<Person>.md","person_event":"wiki/Menschen/<Person>/<YYYY-MM-DD> – <Titel>.md"}}
        workflow={"existing_knowledge_first":True,"create_only_for_new_semantic_unit":True,"historical_correction":"Use correct_page to append a source-linked correction without erasing the original event.","structural_repair":"Use ensure_links; targets must already exist or be created in the same finish transaction.","success_rule":"Only reporting.may_claim_knowledge_saved authorizes saying the Wiki was updated."}
        return {"source_id": stored["source_id"], "route_hint": "raw_only" if kind == "raw_only" else route, "evidence_type": "direct_user_statement", "candidates": candidates, "gardener":gardener,"workflow":workflow,"maintenance_context":health, "allowed_actions": ["no_change", "replace_section", "create_page", "move_page", "ensure_links", "correct_page"], "storage_status": stored.get("reporting"), "replayed": stored.get("replayed", False)}

    def finish(self, raw: dict[str, Any]) -> dict[str, Any]:
        route = raw["route"]
        if route not in {"living_wiki", "procedures", "raw_only", "current_state", "live_data"}:
            raise MemoryV2ToolError("INVALID_ROUTE", "route is invalid.")
        if route == "current_state":
            raise MemoryV2ToolError("UNSUPPORTED_ROUTE", "current_state has no controlled facade writer yet; retain the source as raw_only instead.")
        source = self._call(self.store.memory_source, raw["source_id"], 0, 1)
        sections = source["claim_contract"]["source_sections"]
        section = next((item for item in sections if item["name"] == "direct_user_context"), sections[0])
        allowed = set(section["allowed_evidence_types"])
        evidence = "direct_user_statement" if "direct_user_statement" in allowed else "retrospective_synthesis" if "retrospective_synthesis" in allowed else sorted(allowed)[0]
        claims = [{"claim_id": f"c{index}", "text": text, "source_section": section["name"], "evidence_type": evidence} for index, text in enumerate(raw["claims"], 1)]
        claim_ids = [claim["claim_id"] for claim in claims]
        operations: list[dict[str, Any]] = []
        verification: list[dict[str, Any]] = []
        changes=raw.get("changes") or []; path_routing=[]
        valid_actions = {"no_change", "move_page", "ensure_links", "correct_page", "replace_section", "create_page"}
        for index, change in enumerate(changes):
            action = change.get("action") if isinstance(change, dict) else None
            if action not in valid_actions:
                raise MemoryV2ToolError(
                    "INVALID_CHANGE",
                    f"changes[{index}].action must be one of: {', '.join(sorted(valid_actions))}.",
                )
        if route=="living_wiki": changes,path_routing=_prepare_living_changes(self.store.vault,changes)
        for change in changes:
            action = change["action"]
            if action == "no_change":
                continue
            if action == "move_page":
                if route != "living_wiki" or not isinstance(change.get("target_ref"), str) or not isinstance(change.get("path"), str):
                    raise MemoryV2ToolError("INVALID_CHANGE", "move_page requires a Living Wiki target_ref and destination path.")
                operations.append({"operation":"move_page", "target_ref":change["target_ref"], "path":change["path"]})
                continue
            if action == "ensure_links":
                if route != "living_wiki" or not isinstance(change.get("target_ref"), str) or not change["target_ref"]:
                    raise MemoryV2ToolError("INVALID_CHANGE", "ensure_links requires a Living Wiki target_ref.")
                links=change.get("links")
                if not isinstance(links,list) or not links:
                    raise MemoryV2ToolError("INVALID_CHANGE", "ensure_links requires at least one verified Living Wiki target.")
                operations.append({"operation":"ensure_links", "target_ref":change["target_ref"], "links":links})
                continue
            if action == "correct_page":
                text=change.get("text")
                if route!="living_wiki" or not isinstance(change.get("target_ref"),str) or not change["target_ref"] or not isinstance(text,str) or not text.strip():
                    raise MemoryV2ToolError("INVALID_CHANGE", "correct_page requires a Living Wiki target_ref and the factual correction text.")
                if not claim_ids: raise MemoryV2ToolError("INVALID_CLAIMS", "A correction requires at least one source-grounded claim.")
                operations.append({"operation":"append_correction","target_ref":change["target_ref"],"new_text":text.strip(),"used_claim_ids":claim_ids})
                verification.extend({"sentence":sentence,"supported_by":claim_ids} for sentence in re.split(r"(?<=[.!?])\s+",text.strip()) if sentence.strip())
                continue
            text = change.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MemoryV2ToolError("INVALID_CHANGE", "replace_section and create_page require non-empty text.")
            text = text.strip()
            if not claim_ids:
                raise MemoryV2ToolError("INVALID_CLAIMS", "A write requires at least one source-grounded claim.")
            if action == "replace_section":
                if not isinstance(change.get("target_ref"), str) or not change["target_ref"]:
                    raise MemoryV2ToolError("INVALID_CHANGE", "replace_section requires a target_ref returned by begin or recall.")
                operations.append({"operation": "modify_section", "target_ref": change["target_ref"], "classification": "EXPANDS", "new_text": text, "used_claim_ids": claim_ids})
            elif action == "create_page":
                layer = "living_wiki" if route == "living_wiki" else "procedures" if route == "procedures" else None
                if layer is None:
                    raise MemoryV2ToolError("INVALID_ROUTE", "raw_only and live_data cannot create a Markdown page.")
                if not isinstance(change.get("path"), str) or not isinstance(change.get("title"), str):
                    raise MemoryV2ToolError("INVALID_CHANGE", "create_page requires path and title.")
                text = _without_generated_heading(text, change["title"])
                if not text:
                    raise MemoryV2ToolError("INVALID_CHANGE", "create_page needs body text after its generated H1.")
                operation={"operation": "create_page", "target_layer": layer, "path": change["path"], "title": change["title"], "classification": "NEW_TOPIC", "new_text": text, "used_claim_ids": claim_ids}
                if layer=="living_wiki":
                    note_type=change.get("note_type")
                    if note_type not in {"hub", "event", "insight", "topic", "archive"}:
                        raise MemoryV2ToolError("INVALID_NOTE_TYPE", "note_type must be hub, event, insight, topic, or archive.")
                    event_date=change.get("event_date")
                    precision=change.get("event_date_precision")
                    if note_type == "event" and event_date is not None and (not isinstance(event_date,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",event_date)):
                        raise MemoryV2ToolError("INVALID_EVENT_DATE", "An event_date, if documented, requires YYYY-MM-DD.")
                    if note_type == "event" and event_date is None and precision not in {"unknown","approximate"}:
                        raise MemoryV2ToolError("INVALID_EVENT_DATE", "An undated event requires event_date_precision unknown or approximate.")
                    if note_type != "event" and event_date is not None:
                        raise MemoryV2ToolError("INVALID_EVENT_DATE", "Only an event may include event_date.")
                    operation["note_type"]=note_type
                    operation["links"]=change["links"]
                    if "event_date" in change: operation["event_date"]=change["event_date"]
                    if "event_date_precision" in change: operation["event_date_precision"]=change["event_date_precision"]
                    if "person" in change: operation["person"]=change["person"]
                operations.append(operation)
            else:
                raise MemoryV2ToolError("INVALID_ACTION", "action must be no_change, replace_section, create_page, move_page, ensure_links, or correct_page.")
            verification.extend({"sentence": sentence, "supported_by": claim_ids} for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip())
        state = "live_data" if route == "live_data" else "compiled" if operations else "raw_only"
        key_material = raw["source_id"] + "\x1f" + repr(raw["claims"]) + "\x1f" + repr(raw.get("changes") or []) + "\x1f" + raw.get("reason", "")
        payload = {"idempotency_key": "facade-finish-" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:40], "source_id": raw["source_id"], "claims": claims, "knowledge_operations": operations, "compile_result": {"state": state, "reason": raw.get("reason", "")}}
        if operations:
            for operation in operations:
                operation["verification"] = verification
            # The core expects top-level verification, never a model-supplied hash.
            payload["verification"] = verification
        result=self._call(self.store.memory_commit, payload)
        if path_routing: result["path_routing"]=path_routing
        return result
