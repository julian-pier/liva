from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, timedelta
import logging
import re
from typing import Any, Callable

from .read_service import ReadService, berlin_today
from .redaction import redact, safe_error
from .schemas import DailyNoteInput, EmptyInput, FlagInput, LimitInput, MemoryArchiveInput, MemoryCaptureInput, MemoryInput, MemoryReadInput, MemorySearchInput, NutritionInput, TrainingDecisionPlanInput, TrainingDecisionWriteInput, TrainingRawlogInput, ValidationError, WeightInput, optional_text
from .write_service import WriteService
from .memory_v2_service import MemoryV2Service, MemoryV2ToolError
from .skill_engine import SkillEngineError

logger = logging.getLogger("liva_mcp.tool_registry")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[ReadService, dict[str, Any]], dict[str, Any]]
    required_scope: str = "liva.read"


EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
LIMIT_SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    "additionalProperties": False,
}

WRITE_META = {
    "date": {"type": ["string", "null"], "format": "date"},
    "dry_run": {"type": "boolean", "default": True},
    "reason": {"type": "string", "maxLength": 300},
    "idempotency_key": {"type": ["string", "null"], "maxLength": 160},
}
WEIGHT_SCHEMA = {
    "type": "object",
    "required": ["weight_kg"],
    "properties": {
        **WRITE_META,
        "weight_kg": {"type": ["number", "string"], "description": "Kilograms; decimal comma or point accepted."},
        "note": {"type": ["string", "null"], "maxLength": 500},
    },
    "additionalProperties": False,
}
TRAINING_DECISION_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": ["string", "null"], "format": "date"},
        "date_iso": {"type": ["string", "null"], "format": "date", "deprecated": True},
        "subjective_status": {"type": ["string", "null"], "maxLength": 500},
        "hrv_status_text": {"type": ["string", "null"], "maxLength": 500},
        "pain_notes": {"type": ["string", "null"], "maxLength": 1000},
        "time_available_min": {"type": ["integer", "null"], "minimum": 10, "maximum": 360},
        "constraints": {"type": ["string", "null"], "maxLength": 1500},
        "dry_run": {"type": "boolean", "const": True, "default": True},
    },
    "additionalProperties": False,
}
TRAINING_DECISION_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": ["string", "null"], "format": "date"},
        "date_iso": {"type": ["string", "null"], "format": "date", "deprecated": True},
        "subjective_status": {"type": ["string", "null"], "maxLength": 500},
        "recovery_note": {"type": ["string", "null"], "maxLength": 500},
        "pain_notes": {"type": ["string", "null"], "maxLength": 1000},
        "time_available_min": {"type": ["integer", "null"], "minimum": 10, "maximum": 360},
        "constraints": {"type": ["string", "null"], "maxLength": 1500},
        "force_session_key": {"type": ["string", "null"], "maxLength": 160},
        "dry_run": {"type": "boolean", "default": True},
        "reason": {"type": "string", "maxLength": 300},
        "idempotency_key": {"type": ["string", "null"], "maxLength": 160},
    },
    "additionalProperties": False,
}
LEGACY_MEMORY_SCHEMA = {
    "type": "object",
    "required": ["text"],
    "properties": {
        "text": {"type": "string", "maxLength": 2000},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 50}, "maxItems": 12},
        "scope": {"type": "string", "enum": ["permanent", "temporary", "project", "observation"], "default": "permanent"},
        "replaces_query": {"type": ["string", "null"], "maxLength": 300},
        "replaces_id": {"type": ["integer", "null"]},
        "dry_run": {"type": "boolean", "default": True},
        "reason": {"type": "string", "maxLength": 300},
        "idempotency_key": {"type": ["string", "null"], "maxLength": 160},
    },
    "additionalProperties": False,
}
DAILY_NOTE_SCHEMA = {"type":"object", "required":["category","text"], "properties": {**WRITE_META, "category":{"type":"string","enum":["training","recovery","nutrition","school","personal","system","general"]}, "text":{"type":"string","maxLength":2000}}, "additionalProperties":False}
FLAG_SCHEMA = {"type":"object", "required":["flag_type"], "properties": {**WRITE_META, "flag_type":{"type":"string","enum":["alcohol","sickness","pain","own_football","unusual_load","sleep_issue","stress","other"]}, "severity":{"type":["string","null"],"enum":["low","medium","high",None]}, "body_part":{"type":["string","null"],"maxLength":100}, "text":{"type":["string","null"],"maxLength":1000}}, "additionalProperties":False}
RAWLOG_SCHEMA = {"type":"object", "required":["raw_text"], "properties": {**WRITE_META, "raw_text":{"type":"string","maxLength":8000}, "session_name":{"type":["string","null"],"maxLength":200}, "parsed_exercises":{"type":"array","items":{"type":"object"},"maxItems":30}}, "additionalProperties":False}
NUTRITION_PROPERTIES = {**WRITE_META, "text":{"type":"string","maxLength":2000}, "precision_level":{"type":"string","enum":["exact","estimate","context"]}, "confidence":{"type":["string","null"],"enum":["low","medium","high",None]}}
for _metric in ("calories", "protein", "carbs", "fat"):
    for _kind in ("exact", "estimate", "range_min", "range_max"):
        NUTRITION_PROPERTIES[f"{_metric}_{_kind}{'_g' if _metric != 'calories' else ''}"] = {"type":["number","null"]}
NUTRITION_SCHEMA = {"type":"object", "required":["text","precision_level"], "properties":NUTRITION_PROPERTIES, "additionalProperties":False}
OPERATOR_SCHEMA = {"type":"object", "required":["operations"], "properties":{"operations":{"type":"array","maxItems":20,"items":{"type":"object","required":["operation_type","arguments"],"properties":{"operation_type":{"type":"string","enum":["weight.upsert","daily.flag.post"]},"arguments":{"type":"object"},"confidence":{"type":"string"}},"additionalProperties":False}},"dry_run":{"type":"boolean","default":True},"reason":{"type":"string","maxLength":300}},"additionalProperties":False}

MEMORY_CONTEXT_SCHEMA = {"type": "object", "required": ["queries"], "properties": {"queries": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string", "minLength": 1, "maxLength": 300}}, "layers": {"type": "string", "enum": ["auto", "living_wiki", "procedures", "both"], "default": "auto"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 24000}}, "additionalProperties": False}
MEMORY_SOURCE_SCHEMA = {"type": "object", "required": ["source_id"], "properties": {"source_id": {"type": "string", "pattern": "^src_[A-Za-z0-9]+$", "maxLength": 80}, "offset": {"type": "integer", "minimum": 0, "default": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 12000}}, "additionalProperties": False}
MEMORY_PENDING_SCHEMA = {"type": "object", "properties": {"kind": {"type": ["string", "null"], "enum": ["chat_capture", "conversation_export", "memos_shadow", None]}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}, "additionalProperties": False}
MEMORY_INGEST_SCHEMA = {"type": "object", "required": ["filename"], "properties": {"filename": {"type": "string", "minLength": 1, "maxLength": 255, "description": "Basename of a .md or .txt file already placed in the vault's _inbox folder."}, "source_kind": {"type": "string", "enum": ["conversation_export", "user_authored", "structured_import"], "default": "conversation_export"}, "title": {"type": ["string", "null"], "minLength": 1, "maxLength": 500}}, "additionalProperties": False}
MEMORY_COMMIT_SCHEMA = {"type": "object", "required": ["idempotency_key", "claims", "knowledge_operations", "compile_result"], "properties": {"idempotency_key": {"type": "string", "minLength": 1, "maxLength": 160}, "source_id": {"type": "string", "pattern": "^src_[A-Za-z0-9]+$", "maxLength": 80}, "source": {"type": "object", "required": ["title", "direct_user_context", "assistant_synthesis", "temporal_scope", "topics"], "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 500}, "direct_user_context": {"type": "string", "maxLength": 20000}, "assistant_synthesis": {"type": "string", "maxLength": 20000}, "temporal_scope": {"type": "string", "enum": ["historical", "until_changed", "temporary"]}, "topics": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 100}}, "event_date": {"type": ["string", "null"]}}, "additionalProperties": False}, "claims": {"type": "array", "maxItems": 100, "items": {"type": "object", "required": ["claim_id", "text", "evidence_type", "source_section"], "properties": {"claim_id": {"type": "string", "minLength": 1, "maxLength": 120}, "text": {"type": "string", "minLength": 1, "maxLength": 4000}, "evidence_type": {"type": "string", "enum": ["direct_user_statement", "reported_event", "structured_measurement", "assistant_summary", "assistant_interpretation", "retrospective_synthesis"]}, "source_section": {"type": "string", "minLength": 1, "maxLength": 120}}, "additionalProperties": False}}, "knowledge_operations": {"type": "array", "maxItems": 20, "items": {"type": "object", "additionalProperties": True}}, "compile_result": {"type": "object", "required": ["state"], "properties": {"state": {"type": "string", "enum": ["compiled", "raw_only", "live_data"]}}, "additionalProperties": True}}, "additionalProperties": False}
MEMORY_RECALL_SCHEMA = {"type":"object","required":["query"],"properties":{"query":{"type":"string","minLength":1,"maxLength":1000},"scope":{"type":"string","enum":["auto","personal","procedures","historical"],"default":"auto"},"depth":{"type":"string","enum":["brief","normal","deep"],"default":"normal"}},"additionalProperties":False}
MEMORY_AUDIT_SCHEMA = {"type":"object","properties":{"scope":{"type":"string","enum":["vault","paths"],"default":"vault"},"paths":{"type":"array","minItems":1,"maxItems":100,"items":{"type":"string","minLength":8,"maxLength":500}},"max_issues":{"type":"integer","minimum":1,"maximum":200,"default":100}},"additionalProperties":False}
MEMORY_BEGIN_SCHEMA = {"type":"object","required":["text"],"properties":{"text":{"type":"string","minLength":1,"maxLength":20000},"kind":{"type":"string","enum":["auto","personal","procedure","historical","raw_only"],"default":"auto"},"event_date":{"type":["string","null"]},"temporal_scope":{"type":["string","null"],"enum":["historical","until_changed","temporary",None]},"context_queries":{"type":"array","maxItems":8,"items":{"type":"string","minLength":1,"maxLength":500}}},"additionalProperties":False}
MEMORY_FINISH_SCHEMA = {"type":"object","required":["source_id","claims","route"],"properties":{"source_id":{"type":"string","pattern":"^src_[A-Za-z0-9]+$","maxLength":80},"claims":{"type":"array","maxItems":50,"items":{"type":"string","minLength":1,"maxLength":4000}},"route":{"type":"string","enum":["living_wiki","procedures","raw_only","current_state","live_data"]},"changes":{"type":"array","maxItems":12,"items":{"type":"object","required":["action"],"properties":{"action":{"type":"string","enum":["no_change","replace_section","create_page","move_page","ensure_links","correct_page"]},"target_ref":{"type":"string","maxLength":100},"path":{"type":"string","maxLength":500},"title":{"type":"string","maxLength":500},"text":{"type":"string","maxLength":30000},"note_type":{"type":"string","enum":["hub","event","insight","topic","archive"]},"event_date":{"type":"string","pattern":"^\\d{4}-\\d{2}-\\d{2}$"},"event_date_precision":{"type":"string","enum":["exact","approximate","unknown"]},"person":{"type":"string","minLength":1,"maxLength":200},"links":{"type":"array","minItems":1,"maxItems":6,"description":"Required for every create_page: explicit existing or same-transaction Living Wiki parent/related targets. The backend derives the canonical folder from these links.","items":{"type":"object","required":["path"],"properties":{"path":{"type":"string","minLength":4,"maxLength":500},"label":{"type":"string","minLength":1,"maxLength":200}},"additionalProperties":False}}},"additionalProperties":False}},"reason":{"type":"string","maxLength":1000}},"additionalProperties":False}
_memory_change_schema = MEMORY_FINISH_SCHEMA["properties"]["changes"]["items"]
_memory_change_schema["description"] = (
    "Action contract: replace_section requires target_ref+text; create_page requires path+title+text and, "
    "for Living Wiki, note_type+1-6 canonical wiki/*.md links; move_page requires target_ref+path; "
    "ensure_links requires target_ref+links; correct_page requires target_ref+text; no_change has no payload. "
    "target_ref must come from context/begin/recall. Link targets must already exist or be created in the same finish call."
)
_memory_change_schema["allOf"] = [
    {"if":{"properties":{"action":{"const":"replace_section"}}},"then":{"required":["target_ref","text"]}},
    {"if":{"properties":{"action":{"const":"create_page"}}},"then":{"required":["path","title","text","note_type","links"]}},
    {"if":{"properties":{"action":{"const":"move_page"}}},"then":{"required":["target_ref","path"]}},
    {"if":{"properties":{"action":{"const":"ensure_links"}}},"then":{"required":["target_ref","links"]}},
    {"if":{"properties":{"action":{"const":"correct_page"}}},"then":{"required":["target_ref","text"]}},
]

def _memory_v2(service: ReadService) -> MemoryV2Service:
    return MemoryV2Service(service.config)


def _memory_arguments(raw: Any, allowed: set[str], required: set[str] = set()) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - allowed or not required <= set(raw):
        raise ValidationError("Unsupported or missing Memory V2 arguments")
    return raw


def _memory_context(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"queries", "layers", "limit", "max_chars"}, {"queries"})
    if not isinstance(raw["queries"], list) or not raw["queries"] or any(not isinstance(item, str) or not item.strip() or len(item) > 300 for item in raw["queries"]): raise ValidationError("queries must contain short non-empty strings")
    if raw.get("layers", "auto") not in {"auto", "living_wiki", "procedures", "both"}: raise ValidationError("invalid layers")
    for key, default, maximum in (("limit", 8, 20), ("max_chars", 24000, 100000)):
        if key in raw and (not isinstance(raw[key], int) or not 1 <= raw[key] <= maximum): raise ValidationError(f"invalid {key}")
    return _memory_v2(service).context(raw)


def _memory_source(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"source_id", "offset", "max_chars"}, {"source_id"})
    if not isinstance(raw["source_id"], str) or not re.fullmatch(r"src_[A-Za-z0-9]+", raw["source_id"]): raise ValidationError("invalid source_id")
    if "offset" in raw and (not isinstance(raw["offset"], int) or raw["offset"] < 0): raise ValidationError("invalid offset")
    if "max_chars" in raw and (not isinstance(raw["max_chars"], int) or not 1 <= raw["max_chars"] <= 50000): raise ValidationError("invalid max_chars")
    return _memory_v2(service).source(raw)


def _memory_pending(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"kind", "limit"})
    if raw.get("kind") not in {None, "chat_capture", "conversation_export", "memos_shadow"}: raise ValidationError("invalid kind")
    if "limit" in raw and (not isinstance(raw["limit"], int) or not 1 <= raw["limit"] <= 200): raise ValidationError("invalid limit")
    return {"sources": _memory_v2(service).pending(raw)}


def _memory_ingest_file(service: ReadService, raw: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"filename", "source_kind", "title"}, {"filename"})
    filename = raw["filename"]
    if not isinstance(filename, str) or not filename or len(filename) > 255: raise ValidationError("invalid inbox filename")
    if raw.get("source_kind", "conversation_export") not in {"conversation_export", "user_authored", "structured_import"}: raise ValidationError("invalid source_kind")
    if "title" in raw and raw["title"] is not None and (not isinstance(raw["title"], str) or not raw["title"].strip() or len(raw["title"]) > 500): raise ValidationError("invalid title")
    return _memory_v2(service).ingest_file(raw)


def _memory_commit(service: ReadService, raw: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"idempotency_key", "source", "source_id", "claims", "knowledge_operations", "compile_result"}, {"idempotency_key", "claims", "knowledge_operations", "compile_result"})
    if not isinstance(raw["idempotency_key"], str) or not raw["idempotency_key"].strip() or len(raw["idempotency_key"]) > 160: raise ValidationError("invalid idempotency_key")
    if bool(raw.get("source")) == bool(raw.get("source_id")): raise ValidationError("provide exactly one of source or source_id")
    if not isinstance(raw["claims"], list) or not isinstance(raw["knowledge_operations"], list) or not isinstance(raw["compile_result"], dict): raise ValidationError("invalid commit payload")
    return _memory_v2(service).commit(raw)

def _memory_recall(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"query", "scope", "depth"}, {"query"})
    if not isinstance(raw["query"], str) or not raw["query"].strip() or len(raw["query"]) > 1000: raise ValidationError("invalid query")
    return _memory_v2(service).recall(raw)

def _memory_audit(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"scope", "paths", "max_issues"})
    if raw.get("scope", "vault") not in {"vault", "paths"}:
        raise ValidationError("invalid scope")
    paths = raw.get("paths")
    if paths is not None and (not isinstance(paths, list) or not 1 <= len(paths) <= 100):
        raise ValidationError("invalid paths")
    if "max_issues" in raw and (not isinstance(raw["max_issues"], int) or not 1 <= raw["max_issues"] <= 200):
        raise ValidationError("invalid max_issues")
    return _memory_v2(service).audit(raw)

def _memory_begin(service: ReadService, raw: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"text", "kind", "event_date", "temporal_scope", "context_queries"}, {"text"})
    if not isinstance(raw["text"], str) or not raw["text"].strip() or len(raw["text"]) > 20000: raise ValidationError("invalid text")
    if raw.get("context_queries") is not None and (not isinstance(raw["context_queries"], list) or len(raw["context_queries"]) > 8 or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in raw["context_queries"])): raise ValidationError("invalid context_queries")
    return _memory_v2(service).begin(raw)

def _memory_finish(service: ReadService, raw: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    raw = _memory_arguments(raw, {"source_id", "claims", "route", "changes", "reason"}, {"source_id", "claims", "route"})
    if not isinstance(raw["source_id"], str) or not re.fullmatch(r"src_[A-Za-z0-9]+", raw["source_id"]): raise ValidationError("invalid source_id")
    if not isinstance(raw["claims"], list) or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in raw["claims"]): raise ValidationError("invalid claims")
    if raw.get("changes") is not None and (not isinstance(raw["changes"], list) or any(not isinstance(item, dict) for item in raw["changes"])): raise ValidationError("invalid changes")
    return _memory_v2(service).finish(raw)

COACH_READ_MODES = {
    "capabilities", "daily_snapshot", "daily_context", "state", "context", "core_state", "core_board",
    "core_morning_context", "core_training_card", "training_state", "training_deep_dive",
    "training_workout_coach_view", "training_exercises", "training_exercise_detail", "training_plan", "training_data", "training_today_loads",
    "training_today_decision_context", "training_today_decision_context_full", "training_today_card",
    "training_plans", "exercise_history", "progression_data", "progression_summary",
    "progression_compare", "recovery_state", "recovery_data", "nutrition_state", "nutrition_data",
    "nutrition_timing", "nutrition_plans", "nutrition_plan", "nutrition_foods", "nutrition_meal_templates", "nutrition_meal_template_detail", "weight_state", "weight_data",
    "cardio_state", "cardio_data", "cardio_summary", "life_today", "bodycomp_cut_status",
    "bodyweight_phase_report", "checkin_today", "endurance_approval", "endurance_week",
    "endurance_plans", "endurance_plan", "endurance_calendar", "endurance_session",
    "endurance_fitness", "endurance_plan_diff", "endurance_sync_state",
    "calendar_today", "calendar_range", "calendar_search",
    "analysis_phase_overview", "analysis_phase_detail",
    "weekly_feedback",
}
COACH_ACT_COMMANDS = {
    "core": {"build_board", "set_override", "clear_override"},
    "training": {"log_gym_session", "edit_logged_set", "edit_logged_workout_exercise", "delete_logged_workout", "replace_exercise", "add_exercise", "remove_exercise", "change_sets", "change_rep_range", "change_rpe", "change_variation", "reorder_exercise", "update_exercise_target", "create_training_plan", "replace_training_plan", "patch_training_plan", "activate_training_plan", "archive_training_plan", "delete_training_plan", "save_training_decision"},
    "nutrition": {"adjust_targets", "create_food", "quick_log_meal", "log_planned_meal", "edit_logged_meal", "delete_logged_meal", "create_nutrition_plan", "replace_nutrition_plan", "patch_nutrition_plan", "activate_nutrition_plan", "archive_nutrition_plan", "delete_nutrition_plan"},
    "weight": {"log_weight", "edit_weight", "delete_weight", "setup_bodyweight_phase"},
    "cardio": {"create_cardio_session", "edit_cardio_session", "delete_cardio_session", "create_run", "edit_run", "delete_run"},
    "recovery": {"annotate_day", "set_sleep_flag", "set_sickness_flag", "set_alcohol_flag"},
    "endurance": {"set_execution_mode", "create_endurance_plan", "patch_endurance_plan"},
}
ENDURANCE_TARGET_SCHEMA = {"type": "object", "properties": {"metric": {"type": "string", "enum": ["pace", "hr", "rpe", "open"]}, "basis": {"type": "string", "enum": ["fitness_anchor", "absolute", "open"]}, "zone": {"type": "string", "enum": ["easy", "steady", "long", "threshold", "interval", "vo2", "5k_specific", "goal_5k_pace"]}, "pace_s_per_km": {"type": "number", "minimum": 1}, "pace_min_s_per_km": {"type": "number", "minimum": 1}, "pace_max_s_per_km": {"type": "number", "minimum": 1}, "hr_bpm": {"type": "number", "minimum": 1}, "rpe": {"type": "number", "minimum": 1, "maximum": 10}, "rpe_min": {"type": "number", "minimum": 1, "maximum": 10}, "rpe_max": {"type": "number", "minimum": 1, "maximum": 10}, "min": {"type": "number"}, "max": {"type": "number"}, "source": {"type": "string"}}, "additionalProperties": False}
ENDURANCE_STEP_CHILD_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}, "kind": {"type": "string"}, "step_type": {"type": "string", "description": "Supported alias for kind."}, "sort_order": {"type": "integer"}, "duration_s": {"type": "integer", "minimum": 0}, "duration_min": {"type": "number", "minimum": 0}, "distance_m": {"type": "number", "minimum": 0}, "reps": {"type": "integer", "minimum": 1}, "repetitions": {"type": "integer", "minimum": 1, "description": "Supported alias for reps."}, "target": ENDURANCE_TARGET_SCHEMA, "notes": {"type": ["string", "null"]}}, "additionalProperties": False}
ENDURANCE_STEP_SCHEMA = {"type": "object", "properties": {**ENDURANCE_STEP_CHILD_SCHEMA["properties"], "parent_step_id": {"type": ["string", "null"]}, "steps": {"type": "array", "items": ENDURANCE_STEP_CHILD_SCHEMA}, "children": {"type": "array", "items": ENDURANCE_STEP_CHILD_SCHEMA}}, "additionalProperties": False}
ENDURANCE_SESSION_SCHEMA = {"type": "object", "required": ["title"], "properties": {"id": {"type": "string"}, "phase_id": {"type": ["string", "null"]}, "scheduled_date": {"type": "string", "format": "date"}, "date": {"type": "string", "format": "date", "description": "Supported alias for scheduled_date."}, "session_type": {"type": "string", "enum": ["easy", "steady", "long", "threshold", "interval", "intervals", "vo2", "5k_specific", "race", "rest", "other"]}, "sport_type": {"type": "string", "enum": ["easy", "steady", "long", "threshold", "interval", "intervals", "vo2", "5k_specific", "race", "rest", "other"], "description": "Supported alias for session_type."}, "title": {"type": "string"}, "status": {"type": "string"}, "duration_s": {"type": "integer", "minimum": 0}, "duration_min": {"type": "number", "minimum": 0}, "distance_m": {"type": "number", "minimum": 0}, "distance_km": {"type": "number", "minimum": 0}, "load_value": {"type": "number"}, "tags": {"type": "array", "items": {"type": "string"}}, "notes": {"type": ["string", "null"]}, "steps": {"type": "array", "items": ENDURANCE_STEP_SCHEMA}}, "additionalProperties": False, "anyOf": [{"required": ["scheduled_date"]}, {"required": ["date"]}], "allOf": [{"anyOf": [{"required": ["session_type"]}, {"required": ["sport_type"]}]}]}
ENDURANCE_PHASE_SCHEMA = {"type": "object", "required": ["name", "start_date", "end_date"], "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "phase_type": {"type": "string", "enum": ["base", "build", "threshold", "vo2", "specific", "5k_specific", "peak", "taper", "peak_taper", "sharpen", "recovery", "custom"]}, "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"}, "sort_order": {"type": "integer", "minimum": 0}, "notes": {"type": ["string", "null"]}}, "additionalProperties": False}
ENDURANCE_EVENT_SCHEMA = {"type": "object", "required": ["event_date"], "properties": {"id": {"type": "string"}, "title": {"type": ["string", "null"]}, "distance_m": {"type": "number", "minimum": 1}, "target_time_s": {"type": "integer", "minimum": 1}, "event_date": {"type": "string", "format": "date"}, "priority": {"type": "string"}}, "additionalProperties": False}
ENDURANCE_ANCHOR_SCHEMA = {"type": "object", "properties": {"date": {"type": "string", "format": "date"}, "anchor_date": {"type": "string", "format": "date"}, "source": {"type": "string"}, "five_k_time_s": {"type": "integer", "minimum": 1}, "threshold_pace_s_per_km": {"type": "number", "minimum": 1}, "zones": {"type": "object"}, "notes": {"type": ["string", "null"]}}, "additionalProperties": False, "anyOf": [{"required": ["date"]}, {"required": ["anchor_date"]}]}

def _endurance_operation(op: str, properties: dict, required: list[str]) -> dict:
    return {"type": "object", "required": ["op", *required], "properties": {"op": {"const": op}, **properties}, "additionalProperties": False}

ENDURANCE_OPERATION_SCHEMA = {"oneOf": [
    _endurance_operation("set_season_start", {"start_date": {"type": "string", "format": "date"}, "shift_sessions": {"type": "boolean", "default": True}}, ["start_date"]),
    _endurance_operation("set_goal_event", {"event": ENDURANCE_EVENT_SCHEMA}, ["event"]),
    _endurance_operation("add_phase", {"phase": ENDURANCE_PHASE_SCHEMA}, ["phase"]),
    _endurance_operation("update_phase", {"phase_id": {"type": "string"}, **{key: schema for key, schema in ENDURANCE_PHASE_SCHEMA["properties"].items() if key not in {"id", "sort_order"}}}, ["phase_id"]),
    _endurance_operation("move_phase", {"phase_id": {"type": "string"}, "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"}}, ["phase_id", "start_date"]),
    _endurance_operation("resize_phase", {"phase_id": {"type": "string"}, "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"}}, ["phase_id"]),
    _endurance_operation("delete_phase", {"phase_id": {"type": "string"}}, ["phase_id"]),
    _endurance_operation("add_session", {"session": ENDURANCE_SESSION_SCHEMA}, ["session"]),
    _endurance_operation("update_session", {"session_id": {"type": "string"}, **{key: schema for key, schema in ENDURANCE_SESSION_SCHEMA["properties"].items() if key not in {"id", "steps"}}}, ["session_id"]),
    _endurance_operation("move_session", {"session_id": {"type": "string"}, "scheduled_date": {"type": "string", "format": "date"}, "date": {"type": "string", "format": "date"}}, ["session_id"]),
    _endurance_operation("swap_sessions", {"session_id": {"type": "string"}, "other_session_id": {"type": "string"}}, ["session_id", "other_session_id"]),
    _endurance_operation("duplicate_session", {"session_id": {"type": "string"}, "date": {"type": "string", "format": "date"}}, ["session_id", "date"]),
    _endurance_operation("delete_session", {"session_id": {"type": "string"}}, ["session_id"]),
    _endurance_operation("replace_steps", {"session_id": {"type": "string"}, "steps": {"type": "array", "items": ENDURANCE_STEP_SCHEMA}}, ["session_id", "steps"]),
    _endurance_operation("move_step", {"step_id": {"type": "string"}, "parent_step_id": {"type": ["string", "null"]}, "sort_order": {"type": "integer", "minimum": 0}}, ["step_id", "sort_order"]),
    _endurance_operation("set_session_target", {"step_id": {"type": "string"}, "target": ENDURANCE_TARGET_SCHEMA}, ["step_id", "target"]),
    _endurance_operation("set_fitness_anchor", {"anchor": ENDURANCE_ANCHOR_SCHEMA}, ["anchor"]),
    _endurance_operation("clear_fitness_anchor", {}, []),
    _endurance_operation("rebase_future_targets", {"from_date": {"type": "string", "format": "date"}}, ["from_date"]),
    _endurance_operation("normalize_phases", {}, []),
    _endurance_operation("derive_metrics", {}, []),
    _endurance_operation("repair_workout_semantics", {}, []),
]}
COACH_READ_SCHEMA = {
    "type": "object",
    "required": ["mode"],
    "properties": {
        "mode": {"type": "string", "minLength": 1, "maxLength": 80, "examples": sorted(COACH_READ_MODES), "description": "Stable facade selector. Use capabilities to discover the current backend modes; future modes do not require a new MCP tool or OAuth connection."},
        "payload": {"type": "object", "additionalProperties": True, "properties": {
            "date": {"type": "string", "format": "date"}, "date_from": {"type": "string", "format": "date"}, "date_to": {"type": "string", "format": "date"},
            "week_start": {"type": "string", "format": "date"}, "week_end": {"type": "string", "format": "date"},
            "start": {"type": "string", "format": "date"}, "end": {"type": "string", "format": "date"}, "days": {"type": "integer", "minimum": 1, "maximum": 730}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}, "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
            "query": {"type": "string", "maxLength": 300}, "q": {"type": "string", "maxLength": 300}, "id": {"type": "integer"}, "workout_id": {"type": "integer"}, "exercise_id": {"type": "integer"}, "plan_id": {"type": ["integer", "string"], "description": "Integer for legacy plans; ep_... string for Endurance."}, "template_id": {"type": "integer"},
            "detail": {"type": "string"}, "scope": {"type": "string"}, "focus": {"type": "string"}, "muscle_group": {"type": "string"}, "canonical_id": {"type": "string"}, "exercise": {"type": "string"}, "exercise_names": {"type": "array", "items": {"type": "string"}},
            "nutrition_mode": {"type": "string", "enum": ["daily_totals", "meals", "planned_meals"]}, "progression_mode": {"type": "string", "enum": ["by_exercise", "by_group"]}, "metrics": {"type": "array", "items": {"type": "string"}},
            "compare_type": {"type": "string"}, "range_a": {"type": "object"}, "range_b": {"type": "object"}, "sport_type": {"type": "string"}, "day": {"type": "string"}, "calendar_id": {"type": "string"}, "endurance_plan_id": {"type": "string"}, "session_id": {"type": "string"}, "from_date": {"type": "string", "format": "date"},
            "include_archived": {"type": "boolean"}, "include_exercises": {"type": "boolean"}, "include_sets": {"type": "boolean"}, "include_history": {"type": "boolean"}, "include_items": {"type": "boolean"}, "include_sessions": {"type": "boolean"},
            "day_group_id": {"type": "string", "description": "Stable ID from nutrition_plan.day_groups."}, "expected_revision": {"type": "integer", "description": "Revision from nutrition_plan; use it to avoid writing against stale plan data."}, "weekdays": {"type": "array", "items": {"type": "string", "enum": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}}, "source_weekday": {"type": "string", "enum": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}
        }},
    },
    "additionalProperties": False,
}
COACH_ACT_SCHEMA = {
    "type": "object",
    "required": ["domain", "command"],
    "properties": {
        "domain": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,29}$", "description": "Live capability domain. The dispatcher validates it against the current backend contract."},
        "command": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,79}$", "examples": sorted({command for commands in COACH_ACT_COMMANDS.values() for command in commands}), "description": "Live capability command. Read mode=capabilities for the current domain/action matrix and exact payload hints; future domains and commands require no new MCP tool or OAuth connection."},
        "payload": {"type": "object", "additionalProperties": True, "properties": {
            "date": {"type": "string", "format": "date"}, "date_iso": {"type": "string", "format": "date"}, "id": {"type": "integer"}, "workout_id": {"type": "integer"}, "session_id": {"type": ["integer", "string"]}, "exercise_index": {"type": "integer", "minimum": 0}, "set_index": {"type": "integer", "minimum": 0}, "run_id": {"type": "integer"}, "plan_id": {"type": ["integer", "string"], "description": "Use the ep_... string returned by endurance_plans for Endurance."}, "template_id": {"type": "integer"},
            "raw_text": {"type": "string"}, "session_name": {"type": "string"}, "exercises": {"type": "array", "items": {"type": "object"}}, "weight": {"type": "number"}, "weight_kg": {"type": "number"}, "reps": {}, "rpe": {}, "sets": {},
            "day": {"type": "string"}, "exercise": {"type": "string"}, "old_exercise": {"type": "string"}, "new_exercise": {}, "variation": {"type": "string"}, "new_variation": {"type": "string"}, "canonical_id": {"type": "string"}, "position": {"type": "integer", "minimum": 0}, "workout_index": {"type": "integer", "minimum": 0},
            "operations": {"type": "array", "items": {"type": "object"}}, "plan": {"type": "object"}, "endurance_plan_id": {"type": "string"}, "replace_with": {"type": "object"}, "days": {"type": "array", "items": {"type": "object"}}, "sequence": {"type": "array", "items": {"type": "object"}}, "hard_delete": {"type": "boolean"}, "day_group_id": {"type": "string", "description": "Stable linked-day group ID returned by nutrition_plan."}, "expected_revision": {"type": "integer"}, "weekdays": {"type": "array", "items": {"type": "string", "enum": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}}, "source_weekday": {"type": "string", "enum": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]}, "amount": {"type": "number"}, "unit": {"type": "string"}, "slot_index": {"type": "integer", "minimum": 0}, "food_id": {"type": "integer"},
            "logged_at": {"type": "string"}, "items": {"type": "array", "items": {"type": "object"}}, "meal_id": {"type": "integer"}, "logged_meal_id": {"type": "integer"}, "meal_number": {"type": "integer"}, "slot_id": {"type": "integer"}, "all_planned": {"type": "boolean"}, "append_items": {"type": "boolean"},
            "kcal": {"type": "number"}, "protein": {"type": "number"}, "carbs": {"type": "number"}, "fat": {"type": "number"}, "mode": {"type": "string"}, "green_low": {"type": "number"}, "green_high": {"type": "number"}, "yellow_low": {"type": "number"}, "yellow_high": {"type": "number"},
            "name": {"type": "string"}, "brand": {"type": "string"}, "unit": {"type": "string", "enum": ["g", "ml", "pcs"]}, "unit_default": {"type": "string", "enum": ["g", "ml", "pcs"]}, "serving_size": {"type": "number"}, "portion_size": {"type": "number"}, "serving_weight_g": {"type": "number"}, "portion_g": {"type": "number"}, "common_portion_size": {"type": "number"}, "kcal_per_100": {"type": "number"}, "p_per_100": {"type": "number"}, "c_per_100": {"type": "number"}, "f_per_100": {"type": "number"}, "sugar_per_100": {"type": "number"}, "salt_per_100": {"type": "number"}, "category": {"type": "string"}, "tags": {"type": "string"}, "is_favorite": {"type": "boolean"},
            "value": {}, "context_hash": {"type": "string"}, "source_message": {"type": "string"}, "trigger": {"type": "string"}, "confidence": {"type": "number"}, "note": {"type": "string"}, "notes": {"type": "string", "description": "For training.edit_logged_workout_exercise: replace authored exercise notes; empty clears authored text."}, "new_notes": {"type": "string"}, "reason": {"type": "string"},
            "sport_type": {"type": "string"}, "run_type": {"type": "string"}, "duration": {}, "duration_min": {"type": "number"}, "duration_s": {"type": "integer"}, "distance_km": {"type": "number"}, "distance_m": {"type": "number"}, "avg_hr": {"type": "number"}, "max_hr": {"type": "number"}, "avg_power_w": {"type": "number"}, "elevation_gain": {"type": "number"}, "pace": {"type": "number"}, "stair_floors": {"type": "integer"},
            "phase_type": {"type": "string", "enum": ["cut", "bulk", "maintenance"]}, "phase_start": {"type": "string", "format": "date"}, "phase_start_weight": {"type": "number"}, "execution_mode": {"type": "string", "enum": ["RUN", "ERGO"]}, "intervals_event_id": {"type": "string"}
        }},
        "dry_run": {"type": "boolean", "default": False},
        "confirm": {"type": "boolean", "default": False},
        "reason": {"type": ["string", "null"], "maxLength": 300},
    },
    "additionalProperties": False,
    "allOf": [
        {"if": {"properties": {"domain": {"const": "endurance"}, "command": {"const": "patch_endurance_plan"}}, "required": ["domain", "command"]}, "then": {"properties": {"payload": {"type": "object", "required": ["operations"], "properties": {"endurance_plan_id": {"type": "string", "pattern": "^ep_", "description": "Canonical Endurance plan ID; always a string."}, "plan_id": {"type": "string", "pattern": "^ep_", "description": "Deprecated string alias; never an integer for Endurance."}, "expected_revision": {"type": "integer", "minimum": 1}, "operations": {"type": "array", "minItems": 1, "items": ENDURANCE_OPERATION_SCHEMA}, "reason": {"type": "string"}}, "anyOf": [{"required": ["endurance_plan_id"]}, {"required": ["plan_id"]}], "additionalProperties": False}}}},
        {"if": {"properties": {"domain": {"const": "endurance"}, "command": {"const": "create_endurance_plan"}}, "required": ["domain", "command"]}, "then": {"properties": {"payload": {"type": "object", "required": ["plan"], "properties": {"plan": {"type": "object", "required": ["event"], "properties": {"title": {"type": "string"}, "event": ENDURANCE_EVENT_SCHEMA, "phases": {"type": "array", "items": ENDURANCE_PHASE_SCHEMA}, "sessions": {"type": "array", "items": ENDURANCE_SESSION_SCHEMA}}, "additionalProperties": False}, "reason": {"type": "string"}}, "additionalProperties": False}}}}
    ],
}


def _empty(call: Callable[[ReadService], dict[str, Any]]) -> Callable[[ReadService, dict[str, Any]], dict[str, Any]]:
    def handler(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
        EmptyInput.parse(raw)
        return call(service)

    return handler


def _limited(call: Callable[[ReadService, int], dict[str, Any]], default: int = 10) -> Callable[[ReadService, dict[str, Any]], dict[str, Any]]:
    def handler(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
        parsed = LimitInput.parse(raw, default=default)
        return call(service, parsed.limit)

    return handler


def _memory_search(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    parsed = MemorySearchInput.parse(raw)
    return service.memory_search(parsed.query, parsed.limit, parsed.include_overlay, parsed.active_only, parsed.include_archived, parsed.include_superseded, parsed.sort, parsed.origin, parsed.domain, parsed.memory_type, parsed.temporal_scope, parsed.valid_at, parsed.include_expired)


def _memory_read(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    parsed = MemoryReadInput.parse(raw)
    return service.memory_read(parsed.memo_name)


def _memory_capture(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return service.memory_capture(MemoryCaptureInput.parse(raw), context)

def _memory_archive(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return service.memory_archive(MemoryArchiveInput.parse(raw), context)


def _writer(service: ReadService) -> WriteService:
    return WriteService(service.config.write_database_path(), service.config.nutrition_production_write_path())


def _write(handler: str, parsed: Any) -> Callable[[ReadService, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    def call(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return getattr(_writer(service), handler)(parsed.parse(raw), context)
    return call


def _training_decision_plan(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    return service.training_decision_plan(TrainingDecisionPlanInput.parse(raw))


def _training_decision_write(service: ReadService, raw: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    return service.training_decision_write(TrainingDecisionWriteInput.parse(raw))


def _coach_read(service: ReadService, raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"mode", "payload"}:
        raise ValidationError("mode and optional payload are the only allowed arguments")
    mode = optional_text(raw.get("mode"), maximum=80)
    payload = raw.get("payload", {})
    if not mode or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", mode) or mode == "memory":
        raise ValidationError("mode must be a safe canonical coach read; use dedicated memory tools for memory")
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    return service.coach_read(mode, payload)


def _coach_act(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"domain", "command", "payload", "dry_run", "confirm", "reason"}:
        raise ValidationError("Unsupported coach action argument")
    domain = optional_text(raw.get("domain"), maximum=30)
    command = optional_text(raw.get("command"), maximum=80)
    payload = raw.get("payload", {})
    dry_run = raw.get("dry_run", False)
    confirm = raw.get("confirm", False)
    reason = optional_text(raw.get("reason"), maximum=300)
    if not domain or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", domain) or not command or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", command):
        raise ValidationError("domain and command must be safe identifiers")
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    if not isinstance(dry_run, bool) or not isinstance(confirm, bool):
        raise ValidationError("dry_run and confirm must be boolean")
    return service.coach_act(domain, command, payload, dry_run=dry_run, confirm=confirm, reason=reason, client_id=context.get("client_id"))


_OPERATION_TO_TOOL = {
    "daily.note.post": "liva_post_daily_note", "weight.upsert": "liva_upsert_weight",
    "nutrition.context.post": "liva_post_nutrition_context",
    "daily.flag.post": "liva_post_daily_flag", "training.rawlog.post": "liva_post_training_rawlog",
}


def _operator_plan(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"text"}: raise ValidationError("text is required")
    text = raw["text"] if isinstance(raw.get("text"), str) else ""
    if not text.strip() or len(text) > 4000: raise ValidationError("text is required")
    operations=[]; lowered=text.casefold()
    note_intent = any(term in lowered for term in ("tagesnotiz", "tagesnotiz", "notiere", "persönliche notiz", "persoenliche notiz"))
    if note_intent:
        category = "personal" if ("personal" in lowered or "persönlich" in lowered or "persoenlich" in lowered) else "general"
        target_date = berlin_today()
        if "morgen" in lowered: target_date += timedelta(days=1)
        month_names = {"januar":1,"februar":2,"märz":3,"maerz":3,"april":4,"mai":5,"juni":6,"juli":7,"august":8,"september":9,"oktober":10,"november":11,"dezember":12}
        explicit = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})\b", text)
        if explicit and explicit.group(2).casefold() in month_names:
            try: target_date = Date(int(explicit.group(3)), month_names[explicit.group(2).casefold()], int(explicit.group(1)))
            except ValueError: return {"proposed_operations": [], "confidence": "low", "needs_input": True, "unresolved": ["The note date is invalid."], "executed": False}
        note_text = ""
        if ":" in text:
            note_text = text.rsplit(":", 1)[1].strip()
        if note_text:
            note_text = re.sub(r"\s*(?:Führe|führe|Ändere|ändere)\s+(?:aber\s+)?(?:keine echte Änderung|nichts)\s*aus\.?\s*$", "", note_text, flags=re.IGNORECASE).strip()
        if not note_text:
            return {"proposed_operations": [], "confidence": "low", "needs_input": True, "unresolved": ["The daily-note content is missing; no text was invented."], "required_confirmations": ["Provide the exact note text."], "executed": False}
        operations.append({"operation_type":"daily.note.post", "arguments":{"date":target_date.isoformat(), "category":category, "text":note_text}, "confidence":"high"})
        return {"proposed_operations": operations, "confidence":"high", "needs_input":False, "unresolved":[], "required_confirmations":["Execute the typed operation explicitly."], "executed":False}
    match=re.search(r"\b(\d{2,3}[,.]\d{1,2})\b", text)
    if "gewicht" in lowered and match: operations.append({"operation_type":"weight.upsert","arguments":{"weight_kg":match.group(1),"dry_run":True},"confidence":"medium"})
    if "alkohol" in lowered: operations.append({"operation_type":"daily.flag.post","arguments":{"flag_type":"alcohol","dry_run":True},"confidence":"high"})
    if not operations: return {"proposed_operations":[],"confidence":"low","uncertainties":["No unambiguous Phase 4.1 operation could be derived."],"required_confirmations":["Choose a typed posting tool or provide structured details."],"executed":False}
    return {"proposed_operations":operations,"confidence":"medium","uncertainties":["The plan is dry-run only and never writes."],"required_confirmations":["Execute only after reviewing the typed operation."],"executed":False}


def _operator_execute(service: ReadService, raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw,dict): raise ValidationError("Arguments must be an object")
    unknown = sorted(set(raw)-{"operations","dry_run","reason"})
    if unknown: raise ValidationError(f"Unknown argument: {', '.join(unknown)}")
    operations=raw.get("operations")
    if not isinstance(operations,list) or len(operations) > 20 or any(not isinstance(item,dict) for item in operations): raise ValidationError("operations must be a list of 0 to 20 objects")
    dry=raw.get("dry_run",True)
    if not isinstance(dry,bool): raise ValidationError("dry_run must be boolean")
    reason=optional_text(raw.get("reason"),maximum=300)
    if not dry and not reason: raise ValidationError("reason is required for a live write")
    results=[]
    for index,item in enumerate(operations):
        operation=item.get("operation_type"); arguments=item.get("arguments")
        tool=_OPERATION_TO_TOOL.get(operation)
        if not tool or not isinstance(arguments,dict):
            results.append({"index":index,"ok":False,"error":{"code":"unsupported_operation","message":"Operation is not allowlisted"}}); continue
        values=dict(arguments); values["dry_run"]=dry
        if reason: values["reason"]=reason
        result=call_tool(service,tool,values,scopes={"liva.write"},client_id=context.get("client_id"),request_id=context.get("request_id"))
        results.append({"index":index,**result})
    return {"dry_run":dry,"executed":not dry,"results":results,"succeeded":sum(1 for result in results if result.get("ok")),"failed":sum(1 for result in results if not result.get("ok"))}


TOOLS: tuple[Tool, ...] = (
    Tool("liva_memory_recall", "Recall durable personal knowledge, project knowledge, history or procedures from LIVA Memory V2. It is not canonical for today's training, current/next plan, current weight, current nutrition or current recovery: read the respective structured LIVA tool first. Live reads are not substitutes for long-term personal memory. Do not use legacy Memos tools.", MEMORY_RECALL_SCHEMA, _memory_recall),
    Tool("liva_memory_audit", "Run a bounded, read-only structural audit of the canonical LIVA Memory V2 Living Wiki. It reports only technical integrity findings; it never reads arbitrary files, changes the vault, resolves semantic conflicts, or replaces a targeted source review.", MEMORY_AUDIT_SCHEMA, _memory_audit),
    Tool("liva_memory_begin", "Start the LIVA Memory V2 write flow. It captures an immutable source, searches canonical Wiki/Procedure candidates, and returns duplicate-avoidance, correction and maintenance guidance. Source capture alone is not a Wiki update. Always inspect candidates and maintenance_context; update existing knowledge before creating a genuinely new semantic unit.", MEMORY_BEGIN_SCHEMA, _memory_begin, "liva.write"),
    Tool("liva_memory_finish", "Finish the Memory V2 flow atomically with source-grounded changes. Action requirements are encoded in the schema. Every create_page requires 1-6 canonical wiki/*.md links, including its main hub/parent; targets may already exist or be created in the same call. The backend rejects duplicate identities, unsafe paths, invalid links, conflicting operations and title/H1 mismatches before commit, then returns path_routing, readback, provenance and maintenance results. Use target_ref values only from context/begin/recall. Retry only when error.details.retryable is true; identical successful requests replay their recorded result, while changed payloads conflict. Use replace_section for Living knowledge, correct_page for a source-linked historical correction, and ensure_links for structural repair. Never call source-only storage a Wiki update; only reporting.may_claim_knowledge_saved authorizes that claim.", MEMORY_FINISH_SCHEMA, _memory_finish, "liva.write"),
    Tool("liva_memory_import_file", "Import one file from the private vault _inbox as an immutable Memory V2 source. Then read it with liva_memory_source and finish it through the canonical V2 flow. Never uses Qwen or legacy Memos.", MEMORY_INGEST_SCHEMA, _memory_ingest_file, "liva.write"),
    Tool("liva_memory_context", "Before integrating long-term knowledge into canonical LIVA Memory V2, search a few relevant Living Wiki and/or Procedure sections; do not load the whole vault. Use living_wiki for durable knowledge about Example User, people, developments, projects, decisions and preferences; procedures for rules governing ChatGPT, LIVA, agents or systems. Read context before any modify_section and retain temporal development rather than overwriting the past. This is not Qwen and not a queue.", MEMORY_CONTEXT_SCHEMA, _memory_context),
    Tool("liva_memory_source", "Read an existing or imported Memory V2 source in bounded chunks, especially after liva_memory_import_file. Use it to understand a large source before the semantic liva_memory_finish step. This tool never writes.", MEMORY_SOURCE_SCHEMA, _memory_source),
    Tool("liva_memory_pending", "List existing sources not yet compiled by the ChatGPT-native Memory V2 flow, optionally narrowed by source kind. Use it for imported Sean, Cliffe, training or DJK material. It is not the Qwen queue and does not create normal-chat work.", MEMORY_PENDING_SCHEMA, _memory_pending),
    Tool("liva_memory_ingest_file", "Import exactly one .md or .txt file already placed in the private vault _inbox. The server accepts no paths: filename is a basename only. It stores immutable original bytes with SHA-256, removes the inbox copy only after success, and leaves the source uncompiled for ChatGPT. Flow: inbox → ingest → pending/source (chunked) → context → one memory_commit. Never use Qwen or the local compile queue for this normal flow.", MEMORY_INGEST_SCHEMA, _memory_ingest_file, "liva.write"),
    Tool("liva_memory_commit", "Atomically store one Memory V2 integration. Normal chat flow: identify durable information; call liva_memory_context first; form source-grounded claims; choose living_wiki, procedures, raw_only, current_state or live_data; formulate the final merge; then make exactly one commit. Living Wiki is durable human knowledge; Procedures are system/agent rules. raw_only preserves an immutable source without changing knowledge and MUST NOT be called a Wiki save, repair, or readback. A compiled result is accepted only after an actual knowledge-file change and byte-for-byte readback. Use ensure_links for verified outgoing-link repairs without rewriting historical event prose. Report success only when reporting.may_claim_knowledge_saved is true and use reporting.required_summary. Before modifying, read context and use its target_ref only—never invent paths or hashes. Claims contain only supported information with compatible evidence_type. No Qwen, queue or second capture step. Existing source_id or one new source is required.", MEMORY_COMMIT_SCHEMA, _memory_commit, "liva.write"),
    Tool("liva_context_snapshot", "Universal read-only start for every user turn. Resolve Europe/Berlin date, freshness and the published canonical routing contract from this compact context, then load intent-specific tools before answering. Current training, weight, nutrition and recovery route to live tools; durable/history/procedure knowledge routes to Memory V2. Never writes.", EMPTY_SCHEMA, _empty(lambda s: s.context_snapshot())),
    Tool("liva_daily_snapshot", "Read a selected-source, read-only daily snapshot with explicit freshness; not the full canonical LIVA daily context.", EMPTY_SCHEMA, _empty(lambda s: s.daily_snapshot())),
    Tool("liva_training_state", "Read recent training state.", LIMIT_SCHEMA, _limited(lambda s, n: s.training_state(n))),
    Tool("liva_training_plan", "Read the active training plan.", EMPTY_SCHEMA, _empty(lambda s: s.training_plan())),
    Tool("liva_recovery_snapshot", "Read selected Polar recovery sources with per-source date, age, update time and freshness. Only current_flags describe today. Read-only; not a canonical readiness calculation.", EMPTY_SCHEMA, _empty(lambda s: s.recovery_snapshot())),
    Tool("liva_nutrition_snapshot", "Read a selected-source, read-only nutrition snapshot with explicit freshness; not the full canonical LIVA nutrition state.", EMPTY_SCHEMA, _empty(lambda s: s.nutrition_snapshot())),
    Tool("liva_weight_state", "Read recent bodyweight state.", LIMIT_SCHEMA, _limited(lambda s, n: s.weight_state(n), 14)),
    Tool("liva_runs_state", "Read recent run and cardio state. Garmin runs include analysis-ready laps, splits, heart-rate zones, weather, device metadata, route/time-series samples and original-file metadata when available.", LIMIT_SCHEMA, _limited(lambda s, n: s.runs_state(n))),
    Tool("liva_coach_read", "Evergreen canonical read facade. Use mode=capabilities when current behavior matters; every dispatch validates the requested mode against the live backend contract revision, so future reads require no new MCP tool or OAuth connection.", COACH_READ_SCHEMA, _coach_read),
    Tool("liva_coach_act", "Evergreen canonical action facade. Its public tool definition is stable; every call obtains and server-validates the live backend capability revision, domain, command and required payload before dispatch. New backend domains/actions require no new MCP tool or OAuth connection.", COACH_ACT_SCHEMA, _coach_act, "liva.write"),
    Tool("liva_post_daily_note", "Post one controlled daily annotation; category is a canonical LIVA context category; dry_run defaults to true.", DAILY_NOTE_SCHEMA, _write("daily_note", DailyNoteInput), "liva.write"),
    Tool("liva_upsert_weight", "Create or correct one canonical bodyweight value for an explicit Europe/Berlin date. dry_run defaults to true; a live write requires reason and should use a stable idempotency_key. Claim success only after matching readback.", WEIGHT_SCHEMA, _write("weight", WeightInput), "liva.write"),
    Tool("liva_post_nutrition_context", "Store coarse nutrition context without inventing foods or macros; precision_level controls exact, estimate, or context values.", NUTRITION_SCHEMA, _write("nutrition", NutritionInput), "liva.write"),
    Tool("liva_post_daily_flag", "Store a day-specific context flag; flag_type and severity use the published canonical enums.", FLAG_SCHEMA, _write("flag", FlagInput), "liva.write"),
    Tool("liva_post_training_rawlog", "Store raw training text and only explicit structured candidates; dry_run defaults to true.", RAWLOG_SCHEMA, _write("rawlog", TrainingRawlogInput), "liva.write"),
    Tool("liva_training_decision_plan", "Resolve the requested date's training status. A returned next_session or legacy selected_session is not necessarily today's session: inspect today_status, session_relation, is_today_session, is_rest_day and resolution_status before describing what is scheduled today. Read-only; never writes.", TRAINING_DECISION_PLAN_SCHEMA, _training_decision_plan),
    Tool("liva_training_decision_write", "Create a validated production training decision from the rolling-plan context; dry_run defaults to true. A live write requires reason and a stable idempotency_key.", TRAINING_DECISION_WRITE_SCHEMA, _training_decision_write, "liva.write"),
    Tool("liva_operator_plan", "Derive an explicit Phase 4.1 write plan from free text; never writes.", {"type":"object","required":["text"],"properties":{"text":{"type":"string","maxLength":4000}},"additionalProperties":False}, _operator_plan, "liva.write"),
    Tool("liva_operator_execute", "Execute only typed, allowlisted Phase 4.1 operations with per-operation results; an empty list is a valid no-op.", OPERATOR_SCHEMA, _operator_execute, "liva.write"),
)

TOOL_BY_NAME = {tool.name: tool for tool in TOOLS}


def list_tools() -> list[dict[str, Any]]:
    return [{"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema, "required_scope": tool.required_scope} for tool in TOOLS]


def call_tool(service: ReadService, name: str, arguments: Any, *, scopes: set[str] | None = None, client_id: str | None = None, request_id: str | None = None, smoke_test: bool = False) -> dict[str, Any]:
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "error": {"code": "unknown_tool", "message": "Tool is not allowlisted"}}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": {"code": "invalid_arguments", "message": "Arguments must be an object"}}
    granted = scopes or {"liva.read"}
    if tool.required_scope not in granted:
        return {"ok": False, "error": {"code": "insufficient_scope", "message": "Tool requires liva.write"}}
    default_dry_run = False if name == "liva_coach_act" else True
    requested_dry_run = arguments.get("dry_run", default_dry_run)
    if smoke_test and (name in {"liva_memory_begin", "liva_memory_import_file"} or (name in {item.name for item in TOOLS if item.required_scope == "liva.write"} and requested_dry_run is not True)):
        return {"ok": False, "status": "error", "error": {"code": "smoke_test_live_write_blocked", "message": "Smoke-test tokens can only execute dry-run writes"}}
    try:
        context = {"client_id": client_id, "request_id": request_id}
        try:
            data = tool.handler(service, arguments, context) if tool.required_scope == "liva.write" else tool.handler(service, arguments)
        except (MemoryV2ToolError, SkillEngineError):
            raise
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if tool.required_scope == "liva.write" and isinstance(data, dict):
            memory_v2_write = name in {"liva_memory_begin", "liva_memory_finish", "liva_memory_import_file", "liva_memory_ingest_file", "liva_memory_commit"}
            production_memory_tool = False
            if memory_v2_write:
                data.setdefault("production_write", True)
                data.setdefault("source", "liva_memory_v2")
                data.setdefault("changed", not bool(data.get("replayed")))
            elif not production_memory_tool and requested_dry_run is True:
                data.update({"dry_run": True, "changed": False, "replayed": False, "executed": False, "production_write": False})
            elif not production_memory_tool:
                data.setdefault("dry_run", False)
                data.setdefault("changed", not bool(data.get("replayed") or data.get("idempotent_replay")))
            if production_memory_tool:
                data.setdefault("dry_run", False)
                if data.get("idempotency_replayed") or data.get("replayed"):
                    data["changed"] = False
                else:
                    data.setdefault("changed", True)
                data.setdefault("production_write", True)
                data.setdefault("readback_source", "memos")
            visibility = {
                "liva_memory_begin": ("liva_memory_v2", False, False, "liva_memory_v2"),
                "liva_memory_finish": ("liva_memory_v2", False, False, "liva_memory_v2"),
                "liva_memory_import_file": ("liva_memory_v2", False, False, "liva_memory_v2"),
                "liva_post_daily_note": ("staging", False, False, "mcp_overlay_staging"),
                "liva_post_nutrition_context": ("staging", False, False, "mcp_overlay_staging"),
                "liva_post_daily_flag": ("staging", False, False, "mcp_overlay_staging"),
                "liva_post_training_rawlog": ("staging", False, False, "mcp_overlay_staging"),
            }.get(name, ("staging", False, False, "mcp_overlay_staging"))
            data.setdefault("source", visibility[0])
            data.setdefault("production_write", False)
            data.setdefault("visible_in_frontend", visibility[1])
            data.setdefault("visible_in_daily_snapshot", visibility[2])
            data.setdefault("readback_source", visibility[3])
            data.setdefault("affected_ids", [])
            data.setdefault("warnings", [])
            data.setdefault("error_code", None)
            if data.get("idempotent_replay") is True:
                data["idempotency_replayed"] = True
                data["replayed"] = True
        # Memory V2 already bounds retrieval by depth/max_chars and the remote
        # transport enforces a total response limit. Procedure skills need their
        # complete instruction body, not the generic 500-character preview.
        skill_definition = (
            (name == "liva_coach_read" and arguments.get("mode") == "skill_load")
            or (name == "liva_coach_act" and arguments.get("domain") == "skill" and arguments.get("command") in {"start", "resume"})
        )
        text_maximum = 60_000 if name in {"liva_memory_recall", "liva_memory_context"} or skill_definition else 500
        return {"ok": True, "data": redact(data, text_maximum=text_maximum)}
    except MemoryV2ToolError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = redact(exc.details, text_maximum=500)
        return {"ok": False, "error": error}
    except SkillEngineError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = redact(exc.details, text_maximum=4000)
        return {"ok": False, "error": error}
    except ValidationError as exc:
        return {"ok": False, "error": safe_error(exc, "invalid_arguments")}
    except Exception as exc:
        logger.exception("controlled_tool_unexpected_failure tool=%s request_id=%s exception_type=%s", name, request_id or "-", exc.__class__.__name__)
        if name.startswith("liva_memory_"):
            return {"ok": False, "status": "error", "error": {
                "code": "MEMORY_FACADE_FAILURE",
                "message": "Memory V2 rejected an unexpected facade input or runtime condition before a verified commit. No storage success is implied.",
                "details": {"phase": "facade", "retryable": False, "request_id": request_id or None},
            }}
        if tool.required_scope == "liva.write":
            return {"ok": False, "status": "error", "error": {"code": "write_failed", "message": "Controlled write failed"}}
        return {"ok": False, "error": safe_error(exc)}
