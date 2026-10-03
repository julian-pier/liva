from __future__ import annotations

import base64
import hashlib
import html
import hmac
import json
import logging
import secrets
import time
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlencode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.provider import TokenError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__
from .auth_store import AuthStore
from .config import Config
from .dcr import ALLOWED_REGISTRATION_FIELDS, SAFE_LOG_VALUE, DcrValidationError, RequestAuditMiddleware, configure_audit_logging, register_dynamic_client
from .http_security import SecurityMiddleware, WindowLimiter
from .instructions import server_instructions
from .oauth_provider import PersonalOAuthProvider, SCOPE_OFFLINE, SCOPE_READ, SCOPE_WRITE, normalize_scopes, redirect_allowed
from .read_service import ReadService
from .remote_config import RemoteConfig
from .tool_registry import TOOLS, call_tool


VERSION = __version__
MAX_TOOL_RESPONSE_BYTES = 262_144
tool_audit_logger = logging.getLogger("liva_mcp.tool_audit")
SECURITY_META = {"securitySchemes": [{"type": "oauth2", "scopes": [SCOPE_READ]}]}
WRITE_SECURITY_META = {"securitySchemes": [{"type": "oauth2", "scopes": [SCOPE_WRITE]}]}
READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
REMOTE_MEMORY_TOOL_NAMES = (
    "liva_memory_context", "liva_memory_recall", "liva_memory_audit", "liva_memory_begin", "liva_memory_finish",
    "liva_memory_import_file", "liva_memory_source",
)


class CoachReadPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    date: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    start: str | None = None
    end: str | None = None
    days: int | None = None
    limit: int | None = None
    query: str | None = None
    q: str | None = None
    id: int | None = None
    workout_id: int | None = None
    session_id: int | None = None
    exercise_id: int | None = None
    plan_id: int | None = None
    template_id: int | None = None
    detail: str | None = None
    scope: str | None = None
    focus: str | None = None
    muscle_group: str | None = None
    canonical_id: str | None = None
    exercise: str | None = None
    exercise_names: list[str] | None = None
    nutrition_mode: Literal["daily_totals", "meals", "planned_meals"] | None = None
    progression_mode: Literal["by_exercise", "by_group"] | None = None
    metrics: list[str] | None = None
    compare_type: str | None = None
    range_a: dict[str, Any] | None = None
    range_b: dict[str, Any] | None = None
    sport_type: str | None = None
    day: str | None = None
    calendar_id: str | None = None
    include_archived: bool | None = None
    include_exercises: bool | None = None
    include_sets: bool | None = None
    include_history: bool | None = None
    include_items: bool | None = None
    include_sessions: bool | None = None


class CoachActPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    date: str | None = None
    date_iso: str | None = None
    id: int | None = None
    workout_id: int | None = None
    exercise_index: int | None = Field(default=None, ge=0)
    set_index: int | None = Field(default=None, ge=0)
    run_id: int | None = None
    plan_id: int | None = None
    template_id: int | None = None
    raw_text: str | None = None
    session_name: str | None = None
    exercises: list[dict[str, Any]] | None = None
    weight: float | None = None
    weight_kg: float | None = None
    reps: Any = None
    rpe: Any = None
    sets: Any = None
    day: str | None = None
    exercise: str | None = None
    old_exercise: str | None = None
    new_exercise: Any = None
    variation: str | None = None
    new_variation: str | None = None
    canonical_id: str | None = None
    position: int | None = Field(default=None, ge=0)
    workout_index: int | None = Field(default=None, ge=0)
    operations: list[dict[str, Any]] | None = None
    plan: dict[str, Any] | None = None
    replace_with: dict[str, Any] | None = None
    days: list[dict[str, Any]] | None = None
    sequence: list[dict[str, Any]] | None = None
    hard_delete: bool | None = None
    logged_at: str | None = None
    items: list[dict[str, Any]] | None = None
    meal_id: int | None = None
    logged_meal_id: int | None = None
    meal_number: int | None = None
    slot_id: int | None = None
    all_planned: bool | None = None
    append_items: bool | None = None
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    mode: str | None = None
    green_low: float | None = None
    green_high: float | None = None
    yellow_low: float | None = None
    yellow_high: float | None = None
    name: str | None = None
    brand: str | None = None
    unit: Literal["g", "ml", "pcs"] | None = None
    unit_default: Literal["g", "ml", "pcs"] | None = None
    serving_size: float | None = None
    portion_size: float | None = None
    serving_weight_g: float | None = None
    portion_g: float | None = None
    common_portion_size: float | None = None
    kcal_per_100: float | None = None
    p_per_100: float | None = None
    c_per_100: float | None = None
    f_per_100: float | None = None
    sugar_per_100: float | None = None
    salt_per_100: float | None = None
    category: str | None = None
    tags: str | None = None
    is_favorite: bool | None = None
    value: Any = None
    context_hash: str | None = None
    source_message: str | None = None
    trigger: str | None = None
    confidence: float | None = None
    note: str | None = None
    reason: str | None = None
    sport_type: str | None = None
    duration: Any = None
    duration_min: float | None = None
    duration_s: int | None = None
    distance_km: float | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power_w: float | None = None
    elevation_gain: float | None = None
    pace: float | None = None
    run_type: str | None = None
    stair_floors: int | None = None
    phase_type: Literal["cut", "bulk", "maintenance"] | None = None
    phase_start: str | None = None
    phase_start_weight: float | None = None
    execution_mode: Literal["RUN", "ERGO"] | None = None
    intervals_event_id: str | None = None


def _model_payload(value: BaseModel | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    return dict(value or {})


def install_log_redaction(config: RemoteConfig) -> None:
    previous_factory = logging.getLogRecordFactory()
    secret_values = tuple(value for value in (config.client_secret, config.signing_secret, config.passphrase_hash) if value)
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        message = str(record.msg)
        for value in secret_values:
            message = message.replace(value, "[REDACTED]")
        record.msg = message
        if record.args:
            record.args = tuple("[REDACTED]" if str(value) in secret_values else value for value in record.args) if isinstance(record.args, tuple) else ()
        return record
    logging.setLogRecordFactory(factory)


def configure_tool_audit_logging() -> None:
    tool_audit_logger.setLevel(logging.INFO)
    if not tool_audit_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s liva_mcp.tool_audit %(message)s"))
        tool_audit_logger.addHandler(handler)
    tool_audit_logger.propagate = False


def _invoke(service: ReadService, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    token = get_access_token()
    request_id = (token.claims or {}).get("jti") if token else None
    started = time.monotonic()
    exception_class = "-"
    try:
        result = call_tool(service, name, arguments, scopes=set(token.scopes) if token else set(), client_id=token.client_id if token else None, request_id=request_id, smoke_test=bool(token and (token.claims or {}).get("liva_smoke_test") is True))
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_TOOL_RESPONSE_BYTES:
            result = {"ok": False, "status": "error", "error": {"code": "response_too_large", "message": "Tool response exceeds the size limit"}}
    except Exception as exc:  # defensive boundary around the MCP tool bridge
        exception_class = exc.__class__.__name__
        result = {"ok": False, "status": "error", "error": {"code": "tool_bridge_failed", "message": "Controlled tool execution failed"}}
    data = result.get("data") if isinstance(result, dict) else None
    error = result.get("error") if isinstance(result, dict) else None
    dry_run = arguments.get("dry_run", True) if isinstance(arguments, dict) else True
    write_tool = name in {"liva_coach_act", "liva_post_daily_note", "liva_upsert_weight", "liva_post_nutrition_context", "liva_post_daily_flag", "liva_post_training_rawlog", "liva_operator_execute", "liva_memory_begin", "liva_memory_finish", "liva_memory_import_file"}
    tool_audit_logger.info(
        "tool request_id=%s tool_name=%s operation_type=%s dry_run=%s live_write_attempted=%s write_executed=%s audit_written=%s readback_ok=%s error_code=%s exception_class=%s affected_domain=%s affected_ids_count=%s duration_ms=%s",
        request_id or "-", name, (data or {}).get("operation_type", "-") if isinstance(data, dict) else "-", "yes" if dry_run is True else "no", "yes" if (write_tool and dry_run is False) else "no", "yes" if isinstance(data, dict) and data.get("executed") else "no", "yes" if isinstance(data, dict) and data.get("audit_id") else "no", "yes" if isinstance(data, dict) and data.get("readback") else "no", error.get("code", "-") if isinstance(error, dict) else "-", exception_class, (data or {}).get("affected_domain", "-") if isinstance(data, dict) else "-", len((data or {}).get("affected_ids", [])) if isinstance(data, dict) and isinstance((data or {}).get("affected_ids"), list) else 0, int((time.monotonic() - started) * 1000),
    )
    return result


def _authorization_code(config: RemoteConfig, request_id: str) -> str:
    digest = hmac.new(config.signing_secret.encode(), f"liva-oauth-code:{request_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _public_locals(values: dict[str, Any]) -> dict[str, Any]:
    """Keep closure variables out of tool arguments passed to strict schemas."""
    return {key: value for key, value in values.items() if key != "service"}


def create_app(config: RemoteConfig) -> SecurityMiddleware:
    store = AuthStore(config.auth_db); provider = PersonalOAuthProvider(config, store)
    login_failure_limiter = WindowLimiter(10, 300)
    service = ReadService(Config(repo_root=config.repo_root, database_root=config.database_root, memos_base_url=config.memos_base_url, memos_read_token=config.memos_read_token, memos_write_token=config.memos_write_token, write_state_path=config.write_db, nutrition_write_path=config.nutrition_write_db or (config.database_root / "ernaehrung.sqlite3"), core_write_path=config.core_write_db or (config.database_root / "core.sqlite3"), actions_base_url=config.actions_base_url, actions_token=config.actions_token))
    try:
        service.overlay.initialize()
    except Exception as exc:  # live tools convert a later retry into a controlled error
        logging.getLogger("liva_mcp.tool_audit").error("overlay_initialize_failed exception_class=%s", exc.__class__.__name__)
    auth = AuthSettings(
        issuer_url=config.issuer, resource_server_url=config.resource, required_scopes=[SCOPE_READ],
        client_registration_options=ClientRegistrationOptions(enabled=False, valid_scopes=[SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE], default_scopes=[SCOPE_READ]),
        revocation_options=RevocationOptions(enabled=True),
    )
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[config.public_host, "127.0.0.1:8765", "localhost:8765"],
        allowed_origins=[config.base_url],
    )
    mcp = FastMCP("LIVA Connect", instructions=server_instructions(), auth_server_provider=provider, auth=auth, host=config.host, port=config.port, streamable_http_path="/mcp", stateless_http=True, json_response=True, debug=False, log_level="WARNING", transport_security=transport_security)
    descriptions = {tool.name: tool.description for tool in TOOLS}
    def register(name: str, function: Any) -> None:
        write = name in {"liva_coach_act","liva_post_daily_note","liva_upsert_weight","liva_post_nutrition_context","liva_post_daily_flag","liva_post_training_rawlog","liva_training_decision_write","liva_operator_plan","liva_operator_execute","liva_memory_begin","liva_memory_finish","liva_memory_import_file"}
        mcp.tool(name=name, description=descriptions.get(name, name), annotations=ToolAnnotations(readOnlyHint=not write, destructiveHint=False, idempotentHint=True, openWorldHint=False), meta=WRITE_SECURITY_META if write else SECURITY_META, structured_output=True)(function)

    def context_snapshot() -> dict[str, Any]: return _invoke(service, "liva_context_snapshot", {})
    def daily() -> dict[str, Any]: return _invoke(service, "liva_daily_snapshot", {})
    def training(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]: return _invoke(service, "liva_training_state", {"limit": limit})
    def plan() -> dict[str, Any]: return _invoke(service, "liva_training_plan", {})
    def recovery() -> dict[str, Any]: return _invoke(service, "liva_recovery_snapshot", {})
    def nutrition() -> dict[str, Any]: return _invoke(service, "liva_nutrition_snapshot", {})
    def weight(limit: Annotated[int, Field(ge=1, le=50)] = 14) -> dict[str, Any]: return _invoke(service, "liva_weight_state", {"limit": limit})
    def runs(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]: return _invoke(service, "liva_runs_state", {"limit": limit})
    def coach_read(mode: str, payload: CoachReadPayload | None = None) -> dict[str, Any]: return _invoke(service, "liva_coach_read", {"mode": mode, "payload": _model_payload(payload)})
    def coach_act(domain: str, command: str, payload: CoachActPayload | None = None, dry_run: bool = False, confirm: bool = False, reason: str | None = None) -> dict[str, Any]: return _invoke(service, "liva_coach_act", {"domain": domain, "command": command, "payload": _model_payload(payload), "dry_run": dry_run, "confirm": confirm, "reason": reason})
    def memory_context(queries: list[str], layers: Literal["auto", "living_wiki", "procedures", "both"] = "auto", limit: Annotated[int, Field(ge=1, le=20)] = 8, max_chars: Annotated[int, Field(ge=1, le=100000)] = 24000) -> dict[str, Any]: return _invoke(service, "liva_memory_context", _public_locals(locals()))
    def memory_source(source_id: str, offset: Annotated[int, Field(ge=0)] = 0, max_chars: Annotated[int, Field(ge=1, le=50000)] = 12000) -> dict[str, Any]: return _invoke(service, "liva_memory_source", _public_locals(locals()))
    def memory_recall(query: Annotated[str, Field(min_length=1, max_length=1000)], scope: Literal["auto", "personal", "procedures", "historical"] = "auto", depth: Literal["brief", "normal", "deep"] = "normal") -> dict[str, Any]: return _invoke(service, "liva_memory_recall", _public_locals(locals()))
    def memory_audit(scope: Literal["vault", "paths"] = "vault", paths: list[str] | None = None, max_issues: Annotated[int, Field(ge=1, le=200)] = 100) -> dict[str, Any]: return _invoke(service, "liva_memory_audit", _public_locals(locals()))
    def memory_begin(text: Annotated[str, Field(min_length=1, max_length=20000)], kind: Literal["auto", "personal", "procedure", "historical", "raw_only"] = "auto", event_date: str | None = None, temporal_scope: Literal["historical", "until_changed", "temporary"] | None = None, context_queries: list[str] | None = None) -> dict[str, Any]: return _invoke(service, "liva_memory_begin", _public_locals(locals()))
    def memory_finish(source_id: str, claims: list[str], route: Literal["living_wiki", "procedures", "raw_only", "current_state", "live_data"], changes: list[dict[str, Any]] | None = None, reason: str = "") -> dict[str, Any]: return _invoke(service, "liva_memory_finish", _public_locals(locals()))
    def memory_import_file(filename: Annotated[str, Field(min_length=1, max_length=255)], source_kind: Literal["conversation_export", "user_authored", "structured_import"] = "conversation_export", title: Annotated[str | None, Field(min_length=1, max_length=500)] = None) -> dict[str, Any]: return _invoke(service, "liva_memory_import_file", _public_locals(locals()))
    def memory_pending(kind: Literal["chat_capture", "conversation_export", "memos_shadow"] | None = None, limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict[str, Any]: return _invoke(service, "liva_memory_pending", _public_locals(locals()))
    def memory_ingest_file(filename: Annotated[str, Field(min_length=1, max_length=255)], source_kind: Literal["conversation_export", "user_authored", "structured_import"] = "conversation_export", title: Annotated[str | None, Field(min_length=1, max_length=500)] = None) -> dict[str, Any]: return _invoke(service, "liva_memory_ingest_file", _public_locals(locals()))
    def memory_commit(idempotency_key: str, claims: list[dict[str, Any]], knowledge_operations: list[dict[str, Any]], compile_result: dict[str, Any], source: dict[str, Any] | None = None, source_id: str | None = None) -> dict[str, Any]: return _invoke(service, "liva_memory_commit", _public_locals(locals()))
    def post_daily_note(category: Literal["training", "recovery", "nutrition", "school", "personal", "system", "general"], text: str, date: str | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_post_daily_note",{"date":date,"category":category,"text":text,"dry_run":dry_run,"reason":reason,"idempotency_key":idempotency_key})
    def upsert_weight(weight_kg: float | str, date: str | None = None, note: str | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_upsert_weight",{"date":date,"weight_kg":weight_kg,"note":note,"dry_run":dry_run,"reason":reason,"idempotency_key":idempotency_key})
    def post_nutrition_context(text: str, precision_level: Literal["exact", "estimate", "context"], date: str | None = None, calories_exact: float | None = None, calories_estimate: float | None = None, calories_range_min: float | None = None, calories_range_max: float | None = None, protein_exact_g: float | None = None, protein_estimate_g: float | None = None, protein_range_min_g: float | None = None, protein_range_max_g: float | None = None, carbs_exact_g: float | None = None, carbs_estimate_g: float | None = None, carbs_range_min_g: float | None = None, carbs_range_max_g: float | None = None, fat_exact_g: float | None = None, fat_estimate_g: float | None = None, fat_range_min_g: float | None = None, fat_range_max_g: float | None = None, confidence: Literal["low", "medium", "high"] | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_post_nutrition_context",_public_locals(locals()))
    def post_daily_flag(flag_type: Literal["alcohol", "sickness", "pain", "own_football", "unusual_load", "sleep_issue", "stress", "other"], date: str | None = None, severity: Literal["low", "medium", "high"] | None = None, body_part: str | None = None, text: str | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_post_daily_flag",_public_locals(locals()))
    def post_training_rawlog(raw_text: str, date: str | None = None, session_name: str | None = None, parsed_exercises: list[dict[str, Any]] | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_post_training_rawlog",_public_locals(locals()))
    def training_decision_plan(date: str | None = None, date_iso: str | None = None, subjective_status: str | None = None, hrv_status_text: str | None = None, pain_notes: str | None = None, time_available_min: int | None = None, constraints: str | None = None, dry_run: bool = True) -> dict[str, Any]: return _invoke(service,"liva_training_decision_plan",{"date":date,"date_iso":date_iso,"subjective_status":subjective_status,"hrv_status_text":hrv_status_text,"pain_notes":pain_notes,"time_available_min":time_available_min,"constraints":constraints,"dry_run":dry_run})
    def training_decision_write(date: str | None = None, date_iso: str | None = None, subjective_status: str | None = None, recovery_note: str | None = None, pain_notes: str | None = None, time_available_min: int | None = None, constraints: str | None = None, force_session_key: str | None = None, dry_run: bool = True, reason: str = "", idempotency_key: str | None = None) -> dict[str, Any]: return _invoke(service,"liva_training_decision_write",_public_locals(locals()))
    def operator_plan(text: str) -> dict[str, Any]: return _invoke(service,"liva_operator_plan",{"text":text})
    def operator_execute(operations: list[dict[str, Any]], dry_run: bool = True, reason: str = "") -> dict[str, Any]: return _invoke(service,"liva_operator_execute",_public_locals(locals()))
    for name, function in (("liva_context_snapshot", context_snapshot), ("liva_daily_snapshot", daily), ("liva_training_state", training), ("liva_training_plan", plan), ("liva_recovery_snapshot", recovery), ("liva_nutrition_snapshot", nutrition), ("liva_weight_state", weight), ("liva_runs_state", runs), ("liva_coach_read", coach_read), ("liva_memory_context", memory_context), ("liva_memory_recall", memory_recall), ("liva_memory_audit", memory_audit), ("liva_memory_source", memory_source)):
        register(name, function)
    for name, function in (("liva_coach_act",coach_act),("liva_post_daily_note",post_daily_note),("liva_upsert_weight",upsert_weight),("liva_post_nutrition_context",post_nutrition_context),("liva_post_daily_flag",post_daily_flag),("liva_post_training_rawlog",post_training_rawlog),("liva_training_decision_plan",training_decision_plan),("liva_training_decision_write",training_decision_write),("liva_memory_begin",memory_begin),("liva_memory_finish",memory_finish),("liva_memory_import_file",memory_import_file)):
        register(name,function)
    for name, function in (("liva_operator_plan",operator_plan),("liva_operator_execute",operator_execute)):
        register(name,function)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": VERSION})

    @mcp.custom_route("/oauth/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        async def login_error(error: str, status_code: int) -> Response:
            peer = request.client.host if request.client else ""
            if not await login_failure_limiter.allow(f"login-failure:{peer}"):
                return HTMLResponse("Authorization failed", status_code=429, headers={"x-liva-oauth-error": "login_rate_limited"})
            return HTMLResponse("Authorization failed", status_code=status_code, headers={"x-liva-oauth-error": error})

        def callback_url(row: dict[str, Any], code: str) -> str:
            query = {"code": code}
            if row.get("state") is not None:
                query["state"] = row["state"]
            return f"{row['redirect_uri']}?{urlencode(query)}"

        def handoff(row: dict[str, Any], code: str, audit_error: str) -> Response:
            callback = callback_url(row, code)
            callback_json = json.dumps(callback).replace("<", "\\u003c")
            callback_html = html.escape(callback, quote=True)
            page = f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"0;url={callback_html}\"><title>LIVA authorization</title></head><body><main><p>Continuing to ChatGPT…</p><p><a href=\"{callback_html}\">Continue to ChatGPT</a></p></main><script>window.location.replace({callback_json});</script></body></html>"""
            return HTMLResponse(page, status_code=200, headers={"x-liva-oauth-error": audit_error, "x-liva-oauth-handoff": "1"})

        if request.method == "GET":
            request_id = request.query_params.get("request", ""); row = store.get_request(request_id)
            if not row or row["expires_at"] < int(time.time()):
                return HTMLResponse("Authorization request is invalid or expired", status_code=400, headers={"x-liva-oauth-error": "invalid_login_state"})
            csrf = secrets.token_urlsafe(32)
            if not store.set_csrf(request_id, csrf):
                return HTMLResponse("Authorization request is invalid or expired", status_code=400, headers={"x-liva-oauth-error": "missing_pending_authorization"})
            page = f"""<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta name=\"color-scheme\" content=\"dark\">
  <title>LIVA mit ChatGPT verbinden</title>
  <style>
    :root {{ color-scheme: dark; --ink: #f4f7fb; --muted: #929cab; --line: #293342; --panel: #151b23; --field: #0d1218; --blue: #5c8dff; --mint: #54d6bd; }}
    * {{ box-sizing: border-box; }}
    html {{ min-height: 100%; background: #090d12; }}
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; padding: 24px; color: var(--ink); font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: radial-gradient(circle at 50% 14%, rgba(92,141,255,.12), transparent 31rem), #090d12; }}
    main {{ width: min(100%, 430px); }}
    .brand {{ display: flex; align-items: center; gap: 10px; margin: 0 0 22px 2px; font-size: 13px; font-weight: 750; letter-spacing: .12em; }}
    .brand-mark {{ width: 10px; height: 10px; border-radius: 50%; background: var(--mint); box-shadow: 0 0 0 5px rgba(84,214,189,.1); }}
    .card {{ overflow: hidden; border: 1px solid var(--line); border-radius: 22px; background: color-mix(in srgb, var(--panel) 94%, transparent); box-shadow: 0 28px 80px rgba(0,0,0,.38); }}
    .handoff {{ display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 12px; padding: 18px 22px; border-bottom: 1px solid var(--line); color: #c7ced8; font: 650 12px/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .04em; }}
    .rail {{ height: 1px; background: linear-gradient(90deg, var(--mint), var(--blue)); position: relative; }}
    .rail::after {{ content: \"\"; position: absolute; right: -1px; top: -3px; width: 7px; height: 7px; border-radius: 50%; background: var(--blue); box-shadow: 0 0 12px rgba(92,141,255,.8); }}
    .content {{ padding: 30px 28px 28px; }}
    h1 {{ margin: 0; max-width: 330px; font: 720 clamp(27px, 7vw, 34px)/1.08 ui-rounded, \"SF Pro Rounded\", system-ui, sans-serif; letter-spacing: -.035em; }}
    .intro {{ margin: 13px 0 27px; color: var(--muted); }}
    label {{ display: block; margin-bottom: 9px; color: #cbd2dc; font-size: 13px; font-weight: 650; }}
    .access-key {{ width: 100%; height: 52px; border: 1px solid #344052; border-radius: 13px; padding: 0 15px; outline: none; color: var(--ink); background: var(--field); font: 600 17px/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; transition: border-color .16s ease, box-shadow .16s ease, background .16s ease; }}
    .access-key:hover {{ border-color: #46566d; }}
    .access-key:focus-visible {{ border-color: var(--blue); background: #0f151d; box-shadow: 0 0 0 4px rgba(92,141,255,.16); }}
    button {{ width: 100%; height: 52px; margin-top: 14px; border: 0; border-radius: 13px; color: #071017; background: linear-gradient(135deg, #72e1ca, #77a0ff); font: 760 15px/1 ui-sans-serif, system-ui, sans-serif; cursor: pointer; box-shadow: 0 10px 28px rgba(92,141,255,.16); transition: transform .16s ease, filter .16s ease; }}
    button:hover {{ filter: brightness(1.06); transform: translateY(-1px); }}
    button:active {{ transform: translateY(0); }}
    button:focus-visible {{ outline: 3px solid rgba(119,160,255,.42); outline-offset: 3px; }}
    .note {{ display: flex; align-items: center; gap: 8px; margin: 17px 1px 0; color: #748092; font-size: 12px; }}
    .note::before {{ content: \"\"; width: 6px; height: 6px; border-radius: 50%; background: var(--mint); }}
    @media (max-width: 480px) {{ body {{ padding: 16px; }} .content {{ padding: 27px 21px 23px; }} .handoff {{ padding-inline: 18px; }} }}
    @media (prefers-reduced-motion: reduce) {{ input, button {{ transition: none !important; }} }}
  </style>
</head>
<body>
  <main>
    <div class=\"brand\"><span class=\"brand-mark\" aria-hidden=\"true\"></span>LIVA CONNECT</div>
    <section class=\"card\" aria-labelledby=\"login-title\">
      <div class=\"handoff\" aria-label=\"Sichere Verbindung von LIVA zu ChatGPT\"><span>LIVA</span><span class=\"rail\" aria-hidden=\"true\"></span><span>CHATGPT</span></div>
      <div class=\"content\">
        <h1 id=\"login-title\">Verbindung bestätigen</h1>
        <p class=\"intro\">Gib deinen persönlichen Zugangsschlüssel ein. Danach geht es direkt zurück zu ChatGPT.</p>
        <form method=\"post\" action=\"/oauth/login\">
          <input type=\"hidden\" name=\"request\" value=\"{html.escape(request_id, quote=True)}\">
          <input type=\"hidden\" name=\"csrf\" value=\"{html.escape(csrf, quote=True)}\">
          <label for=\"passphrase\">Zugangsschlüssel</label>
          <input class=\"access-key\" id=\"passphrase\" type=\"text\" inputmode=\"numeric\" name=\"passphrase\" required autofocus autocomplete=\"off\" autocapitalize=\"none\" spellcheck=\"false\">
          <button type=\"submit\">Sicher verbinden</button>
        </form>
        <p class=\"note\">Geschützte, private Verbindung</p>
      </div>
    </section>
  </main>
</body>
</html>"""
            response = HTMLResponse(page, headers={"x-liva-oauth-login": "1"})
            response.set_cookie("liva_oauth_csrf", csrf, secure=True, httponly=True, samesite="lax", max_age=config.authorization_request_seconds, path="/oauth/login")
            return response
        form = await request.form()
        request_id = str(form.get("request", "")); csrf = str(form.get("csrf", "")); cookie = request.cookies.get("liva_oauth_csrf", ""); passphrase = str(form.get("passphrase", ""))
        if not csrf or not cookie or not secrets.compare_digest(csrf, cookie):
            return await login_error("csrf_mismatch", 400)
        code = _authorization_code(config, request_id)
        approved = store.get_recently_approved_request(request_id, csrf, int(time.time()) - config.authorization_code_seconds)
        if approved and store.is_code_replayable(code):
            return handoff(approved, code, "duplicate_handoff_replayed")
        if approved:
            return HTMLResponse("Authorization already completed. You may close this window.", status_code=200, headers={"x-liva-oauth-error": "already_completed_consumed"})
        try:
            PasswordHasher().verify(config.passphrase_hash, passphrase)
        except (VerifyMismatchError, InvalidHashError):
            return await login_error("invalid_passphrase", 403)
        row = store.approve_request(request_id, csrf, code, int(time.time()) + config.authorization_code_seconds)
        if not row:
            return await login_error("invalid_login_state", 400)
        return handoff(row, code, "successful_handoff_page")

    inner = mcp.streamable_http_app()

    def upgrade_dynamic_client_scope(request: Request) -> str | None:
        """Normalize OAuth scope syntax before the SDK validates client metadata.

        The SDK validates a client's registered scope before calling our provider.
        This preflight is deliberately limited to an active dynamic ChatGPT client,
        its registered callback, and this exact protected resource.
        """
        raw_scope = request.query_params.get("scope")
        audit: dict[str, str] = {"requested_scope_raw": raw_scope or "-", "client_scope_upgraded": "no"}
        request.scope["_liva_scope_audit"] = audit
        if raw_scope is None:
            return None
        try:
            normalized = normalize_scopes(raw_scope)
        except ValueError:
            audit["requested_scopes_normalized"] = "invalid"
            return raw_scope
        canonical = " ".join(normalized)
        audit["requested_scopes_normalized"] = ",".join(normalized)
        client_id = request.query_params.get("client_id", "")
        redirect_uri = request.query_params.get("redirect_uri", "")
        resource = request.query_params.get("resource", "")
        dynamic = store.get_dynamic_client(client_id)
        if not dynamic:
            return canonical
        try:
            before = normalize_scopes(dynamic["scope"])
            registered_redirects = json.loads(dynamic["redirect_uris_json"])
        except (ValueError, json.JSONDecodeError):
            return canonical
        audit["client_allowed_scopes_before"] = ",".join(before)
        audit["client_allowed_scopes_after"] = ",".join(before)
        if resource != config.resource or not redirect_allowed(redirect_uri) or redirect_uri not in registered_redirects:
            return canonical
        if set(normalized) - set(before):
            # Scope upgrades are additive. Never remove offline_access from a
            # refresh-capable client just because ChatGPT omitted it here.
            upgraded = [scope for scope in (SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE) if scope in set(before) | set(normalized)]
            if store.update_dynamic_client_scope(client_id, " ".join(upgraded)):
                audit["client_scope_upgraded"] = "yes"
                audit["client_allowed_scopes_after"] = ",".join(upgraded)
        return canonical

    def normalized_authorize_endpoint(endpoint: Any) -> Any:
        async def handle(request: Request) -> Response:
            if request.method != "GET":
                return await endpoint(request)
            normalized_scope = upgrade_dynamic_client_scope(request)
            if normalized_scope is None:
                return await endpoint(request)
            query = [(key, value) for key, value in request.query_params.multi_items() if key != "scope"]
            query.append(("scope", normalized_scope))
            scope = dict(request.scope)
            scope["query_string"] = urlencode(query).encode("utf-8")
            return await endpoint(Request(scope, request.receive))
        return handle

    aliases = {"/authorize": "/oauth/authorize", "/revoke": "/oauth/revoke"}
    rewritten = []
    for route in inner.routes:
        path = getattr(route, "path", "")
        if path == "/.well-known/oauth-authorization-server":
            continue
        if path == "/revoke":
            continue
        if path == "/token":
            continue
        if path in aliases:
            endpoint = route.endpoint
            if path == "/authorize":
                endpoint = normalized_authorize_endpoint(endpoint)
            rewritten.append(Route(aliases[path], endpoint=endpoint, methods=sorted(route.methods or [])))
        else:
            rewritten.append(route)

    async def authorization_metadata(request: Request) -> Response:
        return JSONResponse({
            "issuer": config.issuer,
            "authorization_endpoint": f"{config.base_url}/oauth/authorize",
            "token_endpoint": f"{config.base_url}/oauth/token",
            "revocation_endpoint": f"{config.base_url}/oauth/revoke",
            "registration_endpoint": f"{config.base_url}/oauth/register",
            "scopes_supported": [SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
            "code_challenge_methods_supported": ["S256"],
        })

    async def dynamic_registration(request: Request) -> Response:
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            request.scope["_liva_dcr_audit"] = {"validation_failure_reason": "content_type_not_json"}
            return JSONResponse({"error": "invalid_client_metadata", "error_description": "JSON content is required"}, status_code=400)
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                redirects = payload.get("redirect_uris")
                auth_method = payload.get("token_endpoint_auth_method", "client_secret_basic")
                response_types = payload.get("response_types", ["code"])
                grant_types = payload.get("grant_types", ["authorization_code"])
                ignored_fields = sorted(
                    key if SAFE_LOG_VALUE.fullmatch(key) else "invalid"
                    for key in payload
                    if key not in ALLOWED_REGISTRATION_FIELDS
                )[:20]
                request.scope["_liva_dcr_audit"] = {
                    "redirect_count": str(len(redirects)) if isinstance(redirects, list) and len(redirects) <= 2 else "invalid",
                    "auth_method": auth_method if auth_method in {"client_secret_basic", "none"} else "unsupported",
                    "response_type": "code" if response_types == ["code"] else "unsupported",
                    "grant_types": "+".join(value for value in ("authorization_code", "refresh_token") if isinstance(grant_types, list) and value in grant_types) or "unsupported",
                    "ignored_fields": ",".join(ignored_fields) or "-",
                }
            registered = register_dynamic_client(provider, payload)
        except (json.JSONDecodeError, UnicodeError):
            request.scope["_liva_dcr_audit"] = {"validation_failure_reason": "payload_invalid"}
            return JSONResponse({"error": "invalid_client_metadata", "error_description": "Registration metadata is invalid"}, status_code=400)
        except DcrValidationError as exc:
            audit = request.scope.setdefault("_liva_dcr_audit", {})
            if isinstance(audit, dict):
                audit["validation_failure_reason"] = exc.reason
            status = 500 if exc.error == "server_error" else 400
            return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=status)
        return JSONResponse(registered, status_code=201)

    async def token_endpoint(request: Request) -> Response:
        form = await request.form()
        fields = {key: str(value) for key, value in form.items()}
        grant_type = fields.get("grant_type", "")
        client_id = fields.get("client_id", "")
        client_secret = ""
        auth_method = "none"
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                basic_id, client_secret = decoded.split(":", 1)
                basic_id, client_secret = unquote(basic_id), unquote(client_secret)
                auth_method = "basic"
                if not client_id:
                    client_id = basic_id
                if basic_id != client_id:
                    client_id = ""
            except (ValueError, UnicodeError):
                client_id = client_secret = ""
        elif fields.get("client_secret"):
            client_secret = fields["client_secret"]
            auth_method = "post"
        dynamic = store.get_dynamic_client(client_id) if client_id else None
        client = await provider.get_client(client_id) if client_id else None
        secret_valid = bool(client and provider.verify_client_secret(client_id, client_secret))
        request.scope["_liva_token_auth"] = {
            "method": auth_method, "expected": dynamic["token_endpoint_auth_method"] if dynamic else "client_secret_basic" if client else "-",
            "client_found": "yes" if client else "no", "client_active": "yes" if client else "no",
            "dynamic": "yes" if dynamic else "no", "client_id_match": "yes" if client_id else "no",
            "secret_valid": "yes" if secret_valid else "no", "sdk_auth": "not_used",
        }
        exchange = {
            "token_handler": "custom", "provider_stage": "client_auth", "final_failure_source": "-",
            "grant_allowed": "no", "redirect_allowed": "na", "code_found": "no", "code_used": "na",
            "code_expired": "na", "pkce_valid": "na", "token_issued": "no",
        }
        request.scope["_liva_token_exchange"] = exchange

        def error(name: str, description: str, status: int = 400, source: str = "custom_validation") -> Response:
            exchange["final_failure_source"] = source
            return JSONResponse({"error": name, "error_description": description}, status_code=status, headers={"x-liva-oauth-error": name})

        def success(tokens: Any) -> Response:
            exchange["provider_stage"] = "issue_token"
            exchange["token_issued"] = "yes"
            exchange["token_scope_issued"] = str(tokens.scope or "-")
            return JSONResponse(tokens.model_dump(mode="json", exclude_none=True), headers={"cache-control": "no-store", "pragma": "no-cache"})

        if not client or not secret_valid:
            return error("unauthorized_client", "Client authentication failed", 401, "client_auth")
        allowed_grants = list(client.grant_types or [])
        exchange["grant_allowed"] = "yes" if grant_type in allowed_grants else "no"
        if grant_type not in {"authorization_code", "refresh_token"} or grant_type not in allowed_grants:
            return error("unsupported_grant_type", "Grant type is not allowed", source="validate_grant")

        if grant_type == "authorization_code":
            exchange["provider_stage"] = "load_code"
            raw_code = fields.get("code", "")
            verifier = fields.get("code_verifier", "")
            inspected = store.inspect_code(raw_code) if raw_code else None
            exchange["code_found"] = "yes" if inspected else "no"
            exchange["code_used"] = "yes" if inspected and inspected["consumed_at"] is not None else "no" if inspected else "na"
            exchange["code_expired"] = "yes" if inspected and inspected["expires_at"] < int(time.time()) else "no" if inspected else "na"
            if not inspected or inspected["consumed_at"] is not None or inspected["expires_at"] < int(time.time()):
                return error("invalid_grant", "Authorization code is invalid or expired", source="load_code")
            if inspected["client_id"] != client_id:
                return error("invalid_grant", "Authorization code is not valid for this client", source="validate_code")
            redirect_uri = fields.get("redirect_uri") or inspected["redirect_uri"]
            exchange["redirect_allowed"] = "yes" if redirect_allowed(redirect_uri) else "no"
            if not redirect_allowed(redirect_uri) or redirect_uri != inspected["redirect_uri"]:
                return error("invalid_request", "Redirect URI does not match", source="validate_redirect")
            resource = fields.get("resource") or inspected["resource"]
            if resource != inspected["resource"] or resource != config.resource:
                return error("invalid_target", "OAuth resource does not match", source="validate_resource")
            try:
                scopes = normalize_scopes(json.loads(inspected["scopes_json"]))
            except ValueError:
                return error("invalid_scope", "Authorization code scope is not allowed", source="validate_scope")
            if not verifier:
                return error("invalid_request", "Code verifier is required", source="validate_pkce")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            exchange["pkce_valid"] = "yes" if secrets.compare_digest(challenge, inspected["code_challenge"]) else "no"
            if exchange["pkce_valid"] != "yes":
                return error("invalid_grant", "PKCE verification failed", source="validate_pkce")
            authorization_code = await provider.load_authorization_code(client, raw_code)
            if authorization_code is None:
                return error("invalid_grant", "Authorization code is invalid", source="load_code")
            try:
                return success(await provider.exchange_authorization_code(client, authorization_code))
            except TokenError:
                return error("invalid_grant", "Authorization code is invalid or already used", source="issue_token")

        exchange["provider_stage"] = "load_refresh"
        raw_refresh = fields.get("refresh_token", "")
        stored_refresh = store.get_refresh(raw_refresh) if raw_refresh else None
        if not stored_refresh or stored_refresh["client_id"] != client_id or stored_refresh["expires_at"] < int(time.time()):
            return error("invalid_grant", "Refresh token is invalid or expired", source="load_refresh")
        resource = fields.get("resource") or stored_refresh["resource"]
        if resource != stored_refresh["resource"] or resource != config.resource:
            return error("invalid_target", "OAuth resource does not match", source="validate_resource")
        refresh = await provider.load_refresh_token(client, raw_refresh)
        if refresh is None:
            return error("invalid_grant", "Refresh token is invalid", source="load_refresh")
        try:
            requested_scopes = normalize_scopes(fields.get("scope") if fields.get("scope") else refresh.scopes)
        except ValueError:
            return error("invalid_scope", "Refresh scope is not allowed", source="validate_scope")
        if any(value not in refresh.scopes for value in requested_scopes):
            return error("invalid_scope", "Refresh scope is not allowed", source="validate_scope")
        try:
            return success(await provider.exchange_refresh_token(client, refresh, requested_scopes))
        except TokenError:
            return error("invalid_grant", "Refresh token is invalid or already used", source="issue_token")

    async def protected_resource_metadata(request: Request) -> Response:
        return JSONResponse({
            "resource": config.resource,
            "authorization_servers": [config.issuer],
            "scopes_supported": [SCOPE_READ, SCOPE_WRITE],
            "bearer_methods_supported": ["header"],
        })

    async def revoke(request: Request) -> Response:
        form = await request.form()
        client_id = str(form.get("client_id", "")); client_secret = str(form.get("client_secret", ""))
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode()
                client_id, client_secret = decoded.split(":", 1)
            except (ValueError, UnicodeError):
                client_id = client_secret = ""
        if not provider.verify_client_secret(client_id, client_secret):
            return JSONResponse({"error": "unauthorized_client"}, status_code=401)
        raw = str(form.get("token", ""))
        token = await provider.load_access_token(raw)
        if token is None:
            registered = await provider.get_client(client_id)
            assert registered is not None
            token = await provider.load_refresh_token(registered, raw)
        if token is not None:
            await provider.revoke_token(token)
        return Response(status_code=200)

    rewritten.insert(0, Route("/.well-known/oauth-authorization-server", endpoint=authorization_metadata, methods=["GET"]))
    rewritten.insert(0, Route("/.well-known/oauth-protected-resource", endpoint=protected_resource_metadata, methods=["GET"]))
    rewritten.insert(0, Route("/.well-known/oauth-protected-resource/mcp", endpoint=protected_resource_metadata, methods=["GET"]))
    rewritten.insert(0, Route("/oauth/revoke", endpoint=revoke, methods=["POST"]))
    rewritten.insert(0, Route("/oauth/register", endpoint=dynamic_registration, methods=["POST"]))
    rewritten.insert(0, Route("/oauth/token", endpoint=token_endpoint, methods=["POST"]))
    inner.routes[:] = rewritten
    secured = SecurityMiddleware(inner, config.public_host, config.resource, f"{config.base_url}/.well-known/oauth-protected-resource")
    return RequestAuditMiddleware(secured, config.resource)


def main() -> None:
    import uvicorn
    config = RemoteConfig.from_env()
    install_log_redaction(config)
    configure_audit_logging()
    configure_tool_audit_logging()
    logging.getLogger("mcp.server.auth").setLevel(logging.ERROR)
    uvicorn.run(create_app(config), host="127.0.0.1", port=8765, proxy_headers=False, server_header=False, date_header=False, access_log=False, timeout_keep_alive=5, limit_concurrency=16)


if __name__ == "__main__":
    main()
