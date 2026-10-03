from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from datetime import date as Date
import unicodedata
import hashlib
import re

from .read_service import berlin_today
from .memory_policy import MemoryPolicy, parse_iso


class ValidationError(ValueError):
    pass


def has_disallowed_control(value: str, *, allow_newlines: bool = False) -> bool:
    allowed = "\n\r\t" if allow_newlines else ""
    return any(unicodedata.category(char) == "Cc" and char not in allowed for char in value)


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError("Expected an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Expected an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"Integer must be between {minimum} and {maximum}")
    return parsed


def optional_text(value: Any, *, maximum: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("Expected text")
    result = value.strip()
    if not result:
        return None
    if len(result) > maximum:
        raise ValidationError(f"Text exceeds {maximum} characters")
    return result


def date_value(value: Any) -> str:
    if value is None or value == "":
        return berlin_today().isoformat()
    if not isinstance(value, str):
        raise ValidationError("date must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValidationError("date must be YYYY-MM-DD") from exc


def write_fields(raw: Any, allowed: set[str]) -> tuple[str, bool, str, str | None]:
    if not isinstance(raw, dict):
        raise ValidationError("Arguments must be an object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(f"Unknown argument: {', '.join(unknown)}")
    dry_run = raw.get("dry_run", True)
    if not isinstance(dry_run, bool): raise ValidationError("dry_run must be boolean")
    reason = optional_text(raw.get("reason"), maximum=300)
    if not dry_run and not reason: raise ValidationError("reason is required for a live write")
    key = optional_text(raw.get("idempotency_key"), maximum=160)
    return date_value(raw.get("date")), dry_run, reason or "", key


@dataclass(frozen=True)
class DailyNoteInput:
    date: str; category: str; text: str; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "DailyNoteInput":
        date,dry,reason,key=write_fields(raw,{"date","category","text","dry_run","reason","idempotency_key"})
        category=optional_text(raw.get("category"),maximum=20); text=optional_text(raw.get("text"),maximum=2000)
        if category not in {"training","recovery","nutrition","school","personal","system","general"}: raise ValidationError("Invalid category; allowed: training, recovery, nutrition, school, personal, system, general")
        if not text: raise ValidationError("text is required")
        if has_disallowed_control(text, allow_newlines=True):
            raise ValidationError("text contains control characters")
        if key and has_disallowed_control(key):
            raise ValidationError("idempotency_key contains control characters")
        return cls(date,category,text,dry,reason,key)


def number(value: Any, name: str, minimum: float=0, maximum: float=10000) -> float | None:
    if value is None or value == "": return None
    if isinstance(value,bool): raise ValidationError(f"{name} must be numeric")
    try: result=float(str(value).replace(",","."))
    except (TypeError,ValueError) as exc: raise ValidationError(f"{name} must be numeric") from exc
    if not minimum <= result <= maximum: raise ValidationError(f"{name} is outside allowed range")
    return result


@dataclass(frozen=True)
class WeightInput:
    date: str; weight_kg: float; note: str | None; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "WeightInput":
        date,dry,reason,key=write_fields(raw,{"date","weight_kg","note","dry_run","reason","idempotency_key"})
        weight=number(raw.get("weight_kg"),"weight_kg",35,250)
        if weight is None: raise ValidationError("weight_kg is required")
        return cls(date,round(weight,3),optional_text(raw.get("note"),maximum=500),dry,reason,key)


@dataclass(frozen=True)
class NutritionInput:
    date: str; text: str; precision_level: str; values: dict[str, float | None]; confidence: str | None; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "NutritionInput":
        fields={"date","text","precision_level","confidence","dry_run","reason","idempotency_key"}
        metrics=("calories","protein","carbs","fat")
        for metric in metrics: fields.update({f"{metric}_exact"+("_g" if metric != "calories" else ""),f"{metric}_estimate"+("_g" if metric != "calories" else ""),f"{metric}_range_min"+("_g" if metric != "calories" else ""),f"{metric}_range_max"+("_g" if metric != "calories" else "")})
        date,dry,reason,key=write_fields(raw,fields); text=optional_text(raw.get("text"),maximum=2000); level=optional_text(raw.get("precision_level"),maximum=12)
        if not text or level not in {"exact","estimate","context"}: raise ValidationError("precision_level must be one of: exact, estimate, context")
        confidence=optional_text(raw.get("confidence"),maximum=10)
        if confidence not in {None,"low","medium","high"}: raise ValidationError("confidence must be one of: low, medium, high")
        values={}
        for metric in metrics:
            suffix="_g" if metric != "calories" else ""
            for kind in ("exact","estimate","range_min","range_max"):
                name=f"{metric}_{kind}{suffix}"; values[name]=number(raw.get(name),name,0,10000)
            lo,hi=values[f"{metric}_range_min{suffix}"],values[f"{metric}_range_max{suffix}"]
            if (lo is None) != (hi is None) or (lo is not None and lo > hi): raise ValidationError(f"{metric} range must include valid min and max")
        if level=="context" and any(v is not None for v in values.values()): raise ValidationError("context must not contain numeric macros")
        if level=="exact" and not any(values[k] is not None for k in values if "exact" in k): raise ValidationError("exact precision needs an exact value")
        if level=="estimate" and not any(v is not None for v in values.values()): raise ValidationError("estimate precision needs a numeric estimate or range")
        return cls(date,text,level,values,confidence,dry,reason,key)


@dataclass(frozen=True)
class MemoryInput:
    text: str; text_key: str; tags: list[str]; scope: str; replaces_query: str | None; replaces_id: int | None; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "MemoryInput":
        _date,dry,reason,key=write_fields(raw,{"text","tags","scope","replaces_query","replaces_id","dry_run","reason","idempotency_key"})
        text=optional_text(raw.get("text"),maximum=2000); scope=optional_text(raw.get("scope"),maximum=20) or "permanent"
        if not text or scope not in {"permanent","temporary","project","observation"}: raise ValidationError("text and valid scope are required")
        validate_memory_text(text)
        tags=raw.get("tags") or []
        if not isinstance(tags,list) or len(tags)>12 or any(not optional_text(tag,maximum=50) for tag in tags): raise ValidationError("tags must be a small list of text")
        if re.search(r"(?<!\w)#texte\b", text, re.IGNORECASE) or any(str(tag).strip().lstrip("#").casefold() == "texte" for tag in tags): raise ValidationError("texte memories are read-only")
        replaces_id=raw.get("replaces_id")
        if replaces_id is not None and (isinstance(replaces_id,bool) or not str(replaces_id).isdigit()): raise ValidationError("replaces_id must be an integer")
        normalized=" ".join(text.casefold().split())
        return cls(text,normalized,[str(tag).strip() for tag in tags],scope,optional_text(raw.get("replaces_query"),maximum=300),int(replaces_id) if replaces_id is not None else None,dry,reason,key)


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?key|password|passphrase|secret|private[_ -]?key|oauth[_ -]?secret)\s*[:=]"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)


def validate_memory_text(text: str) -> None:
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise ValidationError("memory text appears to contain credentials or a secret")


def normalize_memory_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValidationError("tags must be a small list of text")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 40:
            raise ValidationError("tags must contain short text values")
        tag = "#" + re.sub(r"[^\wäöüÄÖÜß-]", "_", item.strip().lstrip("#"))
        if tag == "#" or len(tag) > 50:
            raise ValidationError("invalid tag")
        if tag.casefold() not in {value.casefold() for value in result}:
            result.append(tag)
    return result


@dataclass(frozen=True)
class MemoryCaptureInput:
    text: str; tags: list[str]; scope: str; origin: str; domain: str | None; memory_type: str; temporal_scope: str; event_date: str | None; valid_until: str | None; operation: str; target_memo_name: str | None; replaces_query: str | None; replaces_memo_name: str | None; replaces_id: int | None; reason: str; idempotency_key: str
    @classmethod
    def parse(cls, raw: Any) -> "MemoryCaptureInput":
        allowed = {"text", "tags", "scope", "origin", "domain", "memory_type", "temporal_scope", "event_date", "valid_until", "operation", "target_memo_name", "replaces_query", "replaces_memo_name", "replaces_id", "reason", "idempotency_key"}
        if not isinstance(raw, dict) or set(raw) - allowed:
            unknown = sorted(set(raw) - allowed) if isinstance(raw, dict) else []
            raise ValidationError(f"Unknown argument: {', '.join(unknown)}" if unknown else "Arguments must be an object")
        text = optional_text(raw.get("text"), maximum=2000)
        if not text:
            raise ValidationError("text is required")
        validate_memory_text(text)
        scope = optional_text(raw.get("scope"), maximum=20) or "permanent"
        if scope not in {"permanent", "temporary", "project", "observation"}:
            raise ValidationError("invalid scope")
        policy = MemoryPolicy.default()
        origin = optional_text(raw.get("origin"), maximum=20) or "gpt"
        domain = optional_text(raw.get("domain"), maximum=30)
        memory_type = optional_text(raw.get("memory_type"), maximum=30) or "erkenntnis"
        temporal_scope = optional_text(raw.get("temporal_scope"), maximum=30) or ({"temporary":"temporary"}.get(scope, "until_changed"))
        if origin not in policy.origins or origin == "user": raise ValidationError("origin must be gpt or liva for direct MCP writes")
        if domain is not None and domain not in policy.domains: raise ValidationError("domain must be a canonical policy domain")
        if domain == "texte": raise ValidationError("domain texte is read-only and cannot be written by MCP")
        if domain not in {None, "privat", "system"}: raise ValidationError("new MCP memories use no domain, privat, or system; coach context is expressed by origin=liva")
        if memory_type not in policy.memory_types: raise ValidationError("memory_type must be a canonical policy type")
        if temporal_scope not in policy.temporal_scopes: raise ValidationError("temporal_scope must be historical, until_changed, or temporary")
        event_date = parse_iso(raw.get("event_date"), datetime_allowed=False)
        valid_until = parse_iso(raw.get("valid_until"))
        if temporal_scope == "temporary" and valid_until is None: raise ValidationError("temporary memories require valid_until")
        replaces_id = raw.get("replaces_id")
        if replaces_id is not None and (isinstance(replaces_id, bool) or not str(replaces_id).isdigit()):
            raise ValidationError("replaces_id must be an integer")
        key = optional_text(raw.get("idempotency_key"), maximum=160)
        if key is None:
            key = "capture-" + hashlib.sha256((scope + "\0" + " ".join(text.casefold().split())).encode()).hexdigest()[:40]
        memo_name = optional_text(raw.get("replaces_memo_name"), maximum=220)
        if memo_name is not None and not re.fullmatch(r"memos/[A-Za-z0-9_-]+", memo_name):
            raise ValidationError("replaces_memo_name must be a Memos resource name")
        operation = optional_text(raw.get("operation"), maximum=20) or ("supersede" if (memo_name or raw.get("replaces_query") or replaces_id is not None) else "create")
        if operation not in policy.operations: raise ValidationError("operation must be create, supersede, or append_edit")
        query = optional_text(raw.get("replaces_query"), maximum=300)
        target = optional_text(raw.get("target_memo_name"), maximum=220)
        if operation == "supersede" and not (memo_name or query or replaces_id is not None): raise ValidationError("supersede requires a replacement target")
        if operation == "append_edit" and (not target or not event_date): raise ValidationError("append_edit requires target_memo_name and event_date")
        if temporal_scope == "historical" and operation == "supersede": raise ValidationError("historical memories cannot be superseded by capture")
        tags = normalize_memory_tags(raw.get("tags"))
        if "#texte" in {tag.casefold() for tag in tags}: raise ValidationError("texte memories are read-only")
        return cls(text, tags, scope, origin, domain, memory_type, temporal_scope, event_date, valid_until, operation, target, query, memo_name, int(replaces_id) if replaces_id is not None else None, optional_text(raw.get("reason"), maximum=300) or "", key)


@dataclass(frozen=True)
class MemoryArchiveInput:
    memo_name: str; reason: str; idempotency_key: str

    @classmethod
    def parse(cls, raw: Any) -> "MemoryArchiveInput":
        if not isinstance(raw, dict) or set(raw) - {"memo_name", "reason", "idempotency_key"}:
            raise ValidationError("Unknown argument")
        name = optional_text(raw.get("memo_name"), maximum=220)
        if not name or not re.fullmatch(r"memos/[A-Za-z0-9_-]+", name):
            raise ValidationError("memo_name must be a Memos resource name")
        key = optional_text(raw.get("idempotency_key"), maximum=160)
        if not key:
            raise ValidationError("idempotency_key is required")
        return cls(name, optional_text(raw.get("reason"), maximum=300) or "", key)


@dataclass(frozen=True)
class FlagInput:
    date: str; flag_type: str; severity: str | None; body_part: str | None; text: str | None; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "FlagInput":
        date,dry,reason,key=write_fields(raw,{"date","flag_type","severity","body_part","text","dry_run","reason","idempotency_key"})
        typ=optional_text(raw.get("flag_type"),maximum=20); severity=optional_text(raw.get("severity"),maximum=10)
        if typ not in {"alcohol","sickness","pain","own_football","unusual_load","sleep_issue","stress","other"}: raise ValidationError("flag_type must be one of: alcohol, sickness, pain, own_football, unusual_load, sleep_issue, stress, other")
        if severity not in {None,"low","medium","high"}: raise ValidationError("severity must be one of: low, medium, high")
        return cls(date,typ,severity,optional_text(raw.get("body_part"),maximum=100),optional_text(raw.get("text"),maximum=1000),dry,reason,key)


@dataclass(frozen=True)
class TrainingRawlogInput:
    date: str; session_name: str | None; raw_text: str; parsed_exercises: list[dict[str, Any]]; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "TrainingRawlogInput":
        date,dry,reason,key=write_fields(raw,{"date","session_name","raw_text","parsed_exercises","dry_run","reason","idempotency_key"})
        text=optional_text(raw.get("raw_text"),maximum=8000); candidates=raw.get("parsed_exercises") or []
        if not text: raise ValidationError("raw_text is required")
        if not isinstance(candidates,list) or len(candidates)>30 or any(not isinstance(item,dict) for item in candidates): raise ValidationError("parsed_exercises must be a list of objects")
        # Candidates remain raw; no set-slot or inferred values are accepted here.
        return cls(date,optional_text(raw.get("session_name"),maximum=200),text,candidates,dry,reason,key)


@dataclass(frozen=True)
class TrainingDecisionPlanInput:
    date: str; subjective_status: str | None; hrv_status_text: str | None; pain_notes: str | None; time_available_min: int | None; constraints: str | None; dry_run: bool
    @classmethod
    def parse(cls, raw: Any) -> "TrainingDecisionPlanInput":
        if not isinstance(raw, dict) or set(raw) - {"date", "date_iso", "subjective_status", "hrv_status_text", "pain_notes", "time_available_min", "constraints", "dry_run"}:
            raise ValidationError("Unknown argument")
        if raw.get("date") not in (None, "") and raw.get("date_iso") not in (None, "") and raw.get("date") != raw.get("date_iso"):
            raise ValidationError("date and date_iso must match when both are set")
        date = date_value(raw.get("date") if raw.get("date") not in (None, "") else raw.get("date_iso"))
        dry_run = raw.get("dry_run", True)
        if dry_run is not True:
            raise ValidationError("liva_training_decision_plan is plan-only; dry_run must be true")
        time_available = raw.get("time_available_min")
        if time_available is not None:
            if isinstance(time_available, bool): raise ValidationError("time_available_min must be an integer")
            try: time_available = int(time_available)
            except (TypeError, ValueError) as exc: raise ValidationError("time_available_min must be an integer") from exc
            if not 10 <= time_available <= 360: raise ValidationError("time_available_min must be between 10 and 360")
        return cls(date, optional_text(raw.get("subjective_status"), maximum=500), optional_text(raw.get("hrv_status_text"), maximum=500), optional_text(raw.get("pain_notes"), maximum=1000), time_available, optional_text(raw.get("constraints"), maximum=1500), True)


@dataclass(frozen=True)
class TrainingDecisionWriteInput:
    date: str; subjective_status: str | None; recovery_note: str | None; pain_notes: str | None; time_available_min: int | None; constraints: str | None; force_session_key: str | None; dry_run: bool; reason: str; idempotency_key: str | None
    @classmethod
    def parse(cls, raw: Any) -> "TrainingDecisionWriteInput":
        allowed = {"date","date_iso","subjective_status","recovery_note","pain_notes","time_available_min","constraints","force_session_key","dry_run","reason","idempotency_key"}
        if not isinstance(raw, dict) or set(raw) - allowed: raise ValidationError("Unknown argument")
        if raw.get("date") not in (None, "") and raw.get("date_iso") not in (None, "") and raw.get("date") != raw.get("date_iso"): raise ValidationError("date and date_iso must match when both are set")
        normalized = {key: value for key, value in raw.items() if key != "date_iso"}
        normalized["date"] = raw.get("date") if raw.get("date") not in (None, "") else raw.get("date_iso")
        date,dry,reason,key=write_fields(normalized, allowed - {"date_iso"})
        time_available=raw.get("time_available_min")
        if time_available is not None:
            if isinstance(time_available,bool): raise ValidationError("time_available_min must be an integer")
            try: time_available=int(time_available)
            except (TypeError,ValueError) as exc: raise ValidationError("time_available_min must be an integer") from exc
            if not 10 <= time_available <= 360: raise ValidationError("time_available_min must be between 10 and 360")
        force=optional_text(raw.get("force_session_key"),maximum=160)
        return cls(date,optional_text(raw.get("subjective_status"),maximum=500),optional_text(raw.get("recovery_note"),maximum=500),optional_text(raw.get("pain_notes"),maximum=1000),time_available,optional_text(raw.get("constraints"),maximum=1500),force,dry,reason,key)


@dataclass(frozen=True)
class EmptyInput:
    @classmethod
    def parse(cls, raw: Any) -> "EmptyInput":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValidationError("Arguments must be an object")
        if raw:
            raise ValidationError("This tool accepts no arguments")
        return cls()


@dataclass(frozen=True)
class LimitInput:
    limit: int

    @classmethod
    def parse(cls, raw: Any, default: int = 10, maximum: int = 50) -> "LimitInput":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValidationError("Arguments must be an object")
        unknown = set(raw) - {"limit"}
        if unknown:
            raise ValidationError("Unknown argument")
        return cls(limit=bounded_int(raw.get("limit"), default=default, minimum=1, maximum=maximum))


@dataclass(frozen=True)
class MemorySearchInput:
    query: str | None; limit: int; include_overlay: bool; active_only: bool; include_archived: bool; include_superseded: bool; sort: str; origin: str | None; domain: str | None; memory_type: str | None; temporal_scope: str | None; valid_at: str | None; include_expired: bool

    @classmethod
    def parse(cls, raw: Any) -> "MemorySearchInput":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValidationError("Arguments must be an object")
        allowed = {"query", "limit", "include_overlay", "active_only", "include_archived", "include_superseded", "sort", "origin", "domain", "memory_type", "temporal_scope", "valid_at", "include_expired"}
        if set(raw) - allowed:
            raise ValidationError(f"Unknown argument: {', '.join(sorted(set(raw)-allowed))}")
        sort = raw.get("sort") or "relevance"
        if sort == "newest": sort = "newest_updated"
        if sort not in {"relevance", "newest_created", "newest_updated"}:
            raise ValidationError("sort must be one of: relevance, newest_created, newest_updated, newest")
        policy = MemoryPolicy.default()
        origin = optional_text(raw.get("origin"), maximum=20); domain = optional_text(raw.get("domain"), maximum=30); memory_type = optional_text(raw.get("memory_type"), maximum=30); temporal_scope = optional_text(raw.get("temporal_scope"), maximum=30)
        if origin and origin not in policy.origins: raise ValidationError("invalid origin")
        if domain and domain not in policy.domains: raise ValidationError("invalid domain")
        if memory_type and memory_type not in policy.memory_types: raise ValidationError("invalid memory_type")
        if temporal_scope and temporal_scope not in policy.temporal_scopes: raise ValidationError("invalid temporal_scope")
        valid_at = parse_iso(raw.get("valid_at"))
        return cls(
            query=optional_text(raw.get("query"), maximum=200),
            limit=bounded_int(raw.get("limit"), default=20, minimum=1, maximum=50),
            include_overlay=raw.get("include_overlay", False), active_only=raw.get("active_only", True),
            include_archived=raw.get("include_archived", False), include_superseded=raw.get("include_superseded", False), sort=sort, origin=origin, domain=domain, memory_type=memory_type, temporal_scope=temporal_scope, valid_at=valid_at, include_expired=raw.get("include_expired", False),
        )


@dataclass(frozen=True)
class MemoryReadInput:
    memo_name: str

    @classmethod
    def parse(cls, raw: Any) -> "MemoryReadInput":
        if not isinstance(raw, dict) or set(raw) - {"memo_name", "memo_id"} or not (raw.get("memo_name") or raw.get("memo_id")):
            raise ValidationError("memo_name is required")
        memo_id = optional_text(raw.get("memo_name") or raw.get("memo_id"), maximum=160)
        if memo_id is None:
            raise ValidationError("memo_id is required")
        import re

        normalized = memo_id.removeprefix("memos/")
        if normalized.startswith("liva-mcp-memory/"):
            if normalized.removeprefix("liva-mcp-memory/").isdigit():
                return cls(memo_name=normalized)
            raise ValidationError("Invalid memo_id")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValidationError("Invalid memo_id")
        return cls(memo_name="memos/" + normalized)
