from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from .config import Config
from .repository import ReadOnlyRepository
from .write_service import WriteService
from .memos_write import MemosWriteClient
from .memory_policy import MemoryPolicy
from .intent_routing import routing_contract
from .actions_bridge import ActionsBridge
from .skill_engine import LOCAL_READ_MODES, SKILL_CONTRACT_REVISION, SkillEngine
from app_support.weight_trend import build_weight_trend
from app_support.training_decision_service import DecisionPaths, build_training_decision_context, plan_training_decision, read_training_decision_for_dashboard, write_training_decision


BERLIN = ZoneInfo("Europe/Berlin")
MAX_MEMOS_RESPONSE_BYTES = 1_048_576
MAX_PLAN_JSON_BYTES = 131_072
MAX_DB_JSON_BYTES = 131_072
MAX_CALENDAR_EVENTS = 25
MAX_PLANNED_MEALS = 25
MAX_RECOVERY_HISTORY_DAYS = 30
MEMOS_CATALOG_CACHE_SECONDS = 10.0


def berlin_today(now: datetime | None = None) -> date:
    current = now or datetime.now(BERLIN)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BERLIN).date()


def freshness_status(value: Any, *, fresh_days: int = 3) -> tuple[str, int | None]:
    if not value:
        return "missing", None
    try:
        age_days = (berlin_today() - date.fromisoformat(str(value)[:10])).days
    except ValueError:
        return "stale", None
    return ("fresh" if 0 <= age_days <= fresh_days else "stale"), age_days


def parse_json_object(value: Any) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(value, str) or not value:
        return None, False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, True
    return (parsed if isinstance(parsed, dict) else None), not isinstance(parsed, dict)


def parse_json_value(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def snapshot_envelope(
    *,
    data_status: str,
    freshness: dict[str, Any],
    sources: list[str],
    missing_components: list[str],
    parity_level: str,
    parity_missing: list[str],
    notes: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(BERLIN).isoformat(),
        "local_date": berlin_today().isoformat(),
        "timezone": "Europe/Berlin",
        "data_status": data_status,
        "freshness": freshness,
        "sources": sources,
        "missing_components": sorted(set(missing_components)),
        "canonical_parity": {
            "level": parity_level,
            "canonical_equivalent": False,
            "missing": sorted(set(parity_missing)),
            "notes": notes,
        },
        **payload,
    }


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadService:
    def __init__(self, config: Config):
        self.config = config
        self.training = ReadOnlyRepository(config.database_path("training.sqlite3"))
        self.plans = ReadOnlyRepository(config.database_path("plans.sqlite3"))
        self.hrv = ReadOnlyRepository(config.database_path("hrv.sqlite3"))
        self.nutrition = ReadOnlyRepository(config.database_path("ernaehrung.sqlite3"))
        self.runs = ReadOnlyRepository(config.database_path("runs.sqlite3"))
        self.polar = ReadOnlyRepository(config.database_path("polar.sqlite3"))
        self.core = ReadOnlyRepository(config.database_path("core.sqlite3"))
        self.overlay = WriteService(config.write_database_path())
        self.memos = MemosWriteClient(config.memos_base_url, config.memos_read_token, config.memos_write_token)
        self.actions = ActionsBridge(config.actions_base_url, config.actions_token)
        from .memory_service import MemoryProductionService
        self.memory_policy = MemoryPolicy.load(config.memory_policy_path())
        self.memory_production = MemoryProductionService(self.overlay, self.memos, self.memory_policy)
        self._memos_catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._skills: SkillEngine | None = None

    @property
    def skills(self) -> SkillEngine:
        if self._skills is None:
            self._skills = SkillEngine(self.config)
        return self._skills

    @staticmethod
    def _merge_skill_contract(contract: dict[str, Any]) -> dict[str, Any]:
        local = SkillEngine.capability_contract()
        merged = dict(contract)
        merged["read_modes"] = sorted(set(contract.get("read_modes") or []) | set(local["read_modes"]))
        commands = {key: dict(value) for key, value in (contract.get("commands") or {}).items() if isinstance(value, dict)}
        commands.update(local["commands"])
        merged["commands"] = commands
        backend_revision = contract.get("contract_revision") or contract.get("contract_version")
        merged["backend_contract_revision"] = backend_revision
        merged["contract_revision"] = backend_revision
        merged["skill_engine"] = {"status": "active", "contract_revision": SKILL_CONTRACT_REVISION, "registry_read_mode": "skill_registry", "load_read_mode": "skill_load", "session_read_mode": "skill_session", "action_domain": "skill"}
        return merged

    def coach_read(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        if mode in LOCAL_READ_MODES:
            return {
                **self.skills.read(mode, payload),
                "contract_revision": SKILL_CONTRACT_REVISION,
                "contract_source": "liva_skill_engine",
                "source": "liva_skill_engine",
                "production_read": True,
            }
        contract = self._live_coach_contract()
        read_modes = contract.get("read_modes")
        if isinstance(read_modes, list) and mode not in read_modes:
            raise ValueError("unknown_live_read_mode: reload capabilities and use a published mode")
        result = self.actions.read(mode, payload)
        if mode == "capabilities" and isinstance(result.get("result"), dict):
            result["result"] = contract
        return self._with_contract_revision(result, contract)

    def coach_act(self, domain: str, command: str, payload: dict[str, Any], *, dry_run: bool, confirm: bool, reason: str | None, client_id: str | None = None) -> dict[str, Any]:
        if domain == "skill":
            if dry_run:
                raise ValueError("skill sessions do not support dry_run; use isolated test configuration for tests")
            result = self.skills.act(command, payload, client_id=client_id)
            return {
                **result,
                "contract_revision": SKILL_CONTRACT_REVISION,
                "contract_source": "liva_skill_engine",
                "source": "liva_skill_engine",
                "production_write": True,
                "visible_in_frontend": False,
                "visible_in_daily_snapshot": False,
                "readback_source": "liva_skill_engine",
            }
        contract = self._live_coach_contract()
        commands = contract.get("commands")
        domain_contract = commands.get(domain) if isinstance(commands, dict) else None
        command_contract = domain_contract.get(command) if isinstance(domain_contract, dict) else None
        if not isinstance(command_contract, dict):
            raise ValueError("unknown_live_action: reload capabilities and use a published domain and command")
        self._validate_live_action_payload(payload, command_contract)
        result = self.actions.act(domain, command, payload, dry_run=dry_run, confirm=confirm, reason=reason)
        return self._with_contract_revision(result, contract)

    def _live_coach_contract(self) -> dict[str, Any]:
        """Fetch the backend contract per dispatched call, never from startup state."""
        response = self.actions.read("capabilities", {})
        contract = response.get("result") if isinstance(response, dict) else None
        if not isinstance(contract, dict):
            raise RuntimeError("canonical_actions_invalid_capabilities")
        if not isinstance(contract.get("contract_revision") or contract.get("contract_version"), str):
            raise RuntimeError("canonical_actions_missing_contract_revision")
        return self._merge_skill_contract(contract)

    @staticmethod
    def _validate_live_action_payload(payload: dict[str, Any], contract: dict[str, Any]) -> None:
        required = contract.get("required")
        if isinstance(required, list):
            missing = [field for field in required if isinstance(field, str) and payload.get(field) in (None, "")]
            if missing:
                raise ValueError("missing_live_required_fields: " + ", ".join(missing))
        alternatives = contract.get("required_any")
        if isinstance(alternatives, list) and alternatives:
            valid = any(
                isinstance(group, list)
                and all(isinstance(field, str) and payload.get(field) not in (None, "") for field in group)
                for group in alternatives
            )
            if not valid:
                raise ValueError("missing_live_required_any")

    @staticmethod
    def _with_contract_revision(result: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
        return {
            **result,
            "contract_revision": contract.get("contract_revision") or contract.get("contract_version"),
            "contract_source": "live_capabilities",
        }

    def capability_state(self) -> dict[str, Any]:
        return {
            "canonical_actions_live": self.actions.configured,
            "contract_discovery": {"tool": "liva_coach_read", "mode": "capabilities"},
            "forward_compatible_facade": True,
            "stable_tool_pair": ["liva_coach_read", "liva_coach_act"],
            "coach_domains": ["core", "training", "nutrition", "weight", "cardio", "recovery", "endurance", "skill"],
            "skill_engine": {"contract_revision": SKILL_CONTRACT_REVISION, "read_modes": sorted(LOCAL_READ_MODES), "action_domain": "skill"},
            "memory": {
                "canonical_backend": "liva_memory_v2",
                "recall_tool": "liva_memory_recall",
                "write_flow": {"begin": "liva_memory_begin", "finish": "liva_memory_finish"},
                "import_tool": "liva_memory_import_file",
                "source_tool": "liva_memory_source",
                "legacy_memos": {"status": "read_only_archive"},
            },
        }

    def memory_capture(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        self.memory_production.client = self.memos
        result = self.memory_production.capture(value, context)
        self._memos_catalog_cache.clear()
        return result

    def _memos_catalog(self, state: str) -> list[dict[str, Any]]:
        cached = self._memos_catalog_cache.get(state)
        now = monotonic()
        if cached and cached[0] > now:
            return cached[1]

        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        for _page in range(5):
            query = {"pageSize": 100, "state": state}
            if page_token:
                query["pageToken"] = page_token
            payload = self._memos_get("/api/v1/memos", query)
            page_rows = payload.get("memos") if isinstance(payload, dict) else []
            if isinstance(page_rows, list):
                rows.extend(row for row in page_rows if isinstance(row, dict))
            page_token = (payload.get("nextPageToken") or payload.get("next_page_token")) if isinstance(payload, dict) else None
            if not page_token or len(rows) >= 500:
                break
        self._memos_catalog_cache[state] = (now + MEMOS_CATALOG_CACHE_SECONDS, rows)
        return rows

    def memory_archive(self, value: Any, context: dict[str, Any]) -> dict[str, Any]:
        self.memory_production.client = self.memos
        result = self.memory_production.archive(value, context)
        self._memos_catalog_cache.clear()
        return result

    def training_state(self, limit: int = 10) -> dict[str, Any]:
        today = berlin_today()
        recent = self.training.all(
            "SELECT id, date_iso AS date, name, created_at FROM workouts "
            "WHERE date(date_iso) <= date(?) ORDER BY date(date_iso) DESC, id DESC LIMIT ?",
            (today.isoformat(), limit),
        )
        cutoff = (today - timedelta(days=13)).isoformat()
        count = self.training.one(
            "SELECT COUNT(*) AS value FROM workouts WHERE date(date_iso) BETWEEN date(?) AND date(?)",
            (cutoff, today.isoformat()),
        )
        plan = self._active_plan(include_plan=False)
        status, age_days = freshness_status((recent[0] if recent else {}).get("date"))
        return {
            "active_plan": plan,
            "recent_sessions": recent,
            "sessions_last_14_days": int((count or {}).get("value") or 0),
            "latest_session_age_days": age_days,
            "data_status": status,
        }

    def training_plan(self) -> dict[str, Any]:
        plan = self._active_plan(include_plan=True)
        status = "missing" if not plan else "invalid" if plan.get("plan_parse_error") or plan.get("plan_oversized") else "ok"
        return {"active_plan": plan, "data_status": status}

    def training_decision_plan(self, value: Any) -> dict[str, Any]:
        """Return production decision inputs without inventing a rolling session or writing a card."""
        canonical_context = self._canonical_training_context(value.date)
        if canonical_context is not None:
            canonical_plan = self._canonical_training_plan(canonical_context) or {}
            is_today_session = bool(canonical_plan.get("is_today_session"))
            temporal = {key: canonical_plan.get(key) for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")}
            return {
                "status": "dry_run", "changed": False, "replayed": False, "executed": False, "dry_run": True,
                "operation_type": "training.decision.plan", "affected_domain": "training_decision", "affected_ids": [],
                "date": value.date, "source": "canonical_actions", "production_write": False,
                "visible_in_frontend": False, "visible_in_daily_snapshot": False, "visible_in_training_card": False,
                "readback_source": "liva.actions_v2", "context_hash": canonical_context["context_hash"],
                **temporal,
                "selected_session": canonical_plan.get("selected_session"), "dashboard_card_payload": None, "warnings": canonical_plan.get("warnings") or [],
                "error_code": None, "next_step": "ready_for_training_decision_write" if is_today_session else "do_not_write_training_decision",
            }
        paths = DecisionPaths(self.config.database_path("training.sqlite3"), self.config.database_path("plans.sqlite3"), self.config.database_path("hrv.sqlite3"), self.config.database_path("ernaehrung.sqlite3"), self.config.database_path("core.sqlite3"), self.config.core_production_write_path(), self.config.write_database_path())
        supplied = {key: item for key, item in {"subjective_status": value.subjective_status, "hrv_status_text": value.hrv_status_text, "pain_notes": value.pain_notes, "time_available_min": value.time_available_min}.items() if item is not None}
        context = build_training_decision_context(paths, value.date, supplied, value.constraints)
        canonical_hash = self._canonical_training_context_hash(value.date)
        if canonical_hash:
            context["context_hash"] = canonical_hash
        plan = plan_training_decision(context)
        temporal = {key: plan[key] for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")}
        next_step = "ready_for_training_decision_write" if plan["is_today_session"] and plan["resolution_status"] == "resolved" else "do_not_write_training_decision"
        return {"status":"dry_run","changed":False,"replayed":False,"executed":False,"dry_run":True,"operation_type":"training.decision.plan","affected_domain":"training_decision","affected_ids":[],"date":value.date,"source":"production","production_write":False,"visible_in_frontend":False,"visible_in_daily_snapshot":False,"visible_in_training_card":False,"readback_source":"core.training_ai_decisions","context_hash":context["context_hash"],**temporal,"selected_session":plan["selected_session"],"dashboard_card_payload":plan["dashboard_card_payload"],"warnings":plan["warnings"],"error_code":None,"next_step":next_step}
    def training_decision_write(self, value: Any) -> dict[str, Any]:
        canonical_context = self._canonical_training_context(value.date)
        canonical_plan = self._canonical_training_plan(canonical_context) if canonical_context else None
        if canonical_plan is not None:
            canonical_key = str(((canonical_plan.get("selected_session") or {}).get("session_key") or ""))
            if value.force_session_key and str(value.force_session_key) != canonical_key:
                raise ValueError("force_session_key_conflicts_with_canonical_session")
            if value.dry_run:
                temporal = {key: canonical_plan[key] for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")}
                return {"status":"dry_run","changed":False,"replayed":False,"executed":False,"dry_run":True,"operation_type":"training.decision.write","affected_domain":"training_decision","affected_ids":[],"source":"canonical_actions","production_write":False,"visible_in_frontend":False,"visible_in_daily_snapshot":False,"visible_in_training_card":False,"readback_source":"liva.actions_v2","context_hash":canonical_context["context_hash"],**temporal,"selected_session":canonical_plan.get("selected_session"),"dashboard_status":"missing","readback_summary":None,"warnings":canonical_plan.get("warnings") or [],"error_code":None,"next_step":"ready_for_training_decision_write" if canonical_plan.get("is_today_session") else "do_not_write_training_decision"}
            if not canonical_plan.get("is_today_session") or canonical_plan.get("resolution_status") != "resolved":
                raise ValueError("requested_date_has_no_resolved_training_session")
        paths = DecisionPaths(self.config.database_path("training.sqlite3"), self.config.database_path("plans.sqlite3"), self.config.database_path("hrv.sqlite3"), self.config.database_path("ernaehrung.sqlite3"), self.config.database_path("core.sqlite3"), self.config.core_production_write_path(), self.config.write_database_path())
        supplied = {key: item for key, item in {"subjective_status": value.subjective_status, "recovery_note": value.recovery_note, "pain_notes": value.pain_notes, "time_available_min": value.time_available_min}.items() if item is not None}
        forced_key = ((canonical_plan or {}).get("selected_session") or {}).get("session_key") or value.force_session_key
        context = build_training_decision_context(paths, value.date, supplied, value.constraints, forced_key)
        if canonical_context and canonical_plan:
            selected = canonical_plan["selected_session"]
            context["context"]["selected_session"] = selected
            context["context"]["temporal_resolution"] = {key: canonical_plan[key] for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")}
            context["context_hash"] = canonical_context["context_hash"]
        else:
            canonical_hash = self._canonical_training_context_hash(value.date)
            if canonical_hash:
                context["context_hash"] = canonical_hash
        plan = plan_training_decision(context)
        if canonical_plan:
            plan.update({key: canonical_plan[key] for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")})
            plan["selected_session"] = canonical_plan["selected_session"]
        if value.dry_run:
            temporal = {key: plan[key] for key in ("requested_date", "timezone", "today_status", "decision_scope", "session_relation", "is_today_session", "is_rest_day", "today_session", "next_session", "next_session_date", "calendar_source", "rolling_sequence_position", "resolution_status", "resolution_reason")}
            return {"status":"dry_run","changed":False,"replayed":False,"executed":False,"dry_run":True,"operation_type":"training.decision.write","affected_domain":"training_decision","affected_ids":[],"source":"production","production_write":False,"visible_in_frontend":False,"visible_in_daily_snapshot":False,"visible_in_training_card":False,"readback_source":"core.training_ai_decisions","context_hash":context["context_hash"],**temporal,"selected_session":plan["selected_session"],"dashboard_status":"missing","readback_summary":None,"warnings":plan["warnings"],"error_code":None}
        written = write_training_decision(paths, context, plan, value.reason, value.idempotency_key)
        readback = written["readback"]
        direct_complete = readback.get("dashboard_status") == "fresh" and readback.get("context_hash") == context["context_hash"] and readback.get("session_name") == plan["selected_session"]["name"]
        frontend_refresh = None
        frontend_refresh_error = None
        if self.actions.configured:
            try:
                frontend_refresh = self.actions.refresh_training_card(context["context"]["date_iso"])
            except Exception:
                # The canonical decision is already committed. Report a
                # partial result instead of hiding a successful write behind a
                # generic write_failed response.
                frontend_refresh_error = "frontend_snapshot_refresh_failed"
        frontend_complete = direct_complete and (not self.actions.configured or bool((frontend_refresh or {}).get("invalidated")))
        warnings = plan["warnings"] + ([] if direct_complete else ["dashboard_readback_mismatch"])
        if frontend_refresh_error:
            warnings.append(frontend_refresh_error)
        return {"status": written["status"] if frontend_complete else "partial", "changed": written["changed"], "replayed": written["replayed"], "executed": written["changed"], "operation_type":"training.decision.write","affected_domain":"training_decision","affected_ids":written["affected_ids"],"source":"production","production_write":True,"visible_in_frontend":frontend_complete,"visible_in_daily_snapshot":frontend_complete,"visible_in_training_card":frontend_complete,"decision_id":written["decision_id"],"context_hash":context["context_hash"],"selected_session":plan["selected_session"],"dashboard_status":readback.get("dashboard_status"),"readback_summary":readback,"frontend_readback":frontend_refresh,"warnings":warnings,"error_code":None if frontend_complete else (frontend_refresh_error or "dashboard_readback_mismatch"),"audit_id":written.get("audit_id")}

    def _canonical_training_context_hash(self, date_value: Any) -> str | None:
        """Actions V2 owns the rolling-resolver context hash for every MCP view."""
        context = self._canonical_training_context(date_value)
        value = context.get("context_hash") if isinstance(context, dict) else None
        return str(value) if isinstance(value, str) and value else None

    @staticmethod
    def _canonical_training_plan(context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(context, dict):
            return None
        planned = context.get("planned_session") if isinstance(context.get("planned_session"), dict) else {}
        available = bool(planned.get("available"))
        selected = None
        if available:
            exercises = []
            for item in planned.get("exercises") if isinstance(planned.get("exercises"), list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("exercise") or item.get("name") or "").strip()
                if not name:
                    continue
                exercises.append({
                    "exercise": name,
                    "planned_sets": item.get("planned_sets") if item.get("planned_sets") is not None else item.get("sets"),
                    "target_rep_range": item.get("target_rep_range") if item.get("target_rep_range") is not None else item.get("reps"),
                    "variation": item.get("variation") or item.get("device"),
                })
            selected = {
                "name": planned.get("name") or planned.get("label"),
                "session_key": planned.get("session_key"),
                "session_type": planned.get("session_type") or planned.get("type") or "gym",
                "plan_id": planned.get("plan_id"),
                "exercises": exercises,
                "session_relation": "today",
                "is_today_session": True,
            }
        return {
            "requested_date": context.get("date"),
            "timezone": "Europe/Berlin",
            "today_status": "training" if available else "rest",
            "decision_scope": "today",
            "session_relation": "today" if available else "rest",
            "is_today_session": available,
            "is_rest_day": not available,
            "today_session": selected,
            "next_session": None,
            "next_session_date": None,
            "calendar_source": "canonical_actions",
            "rolling_sequence_position": planned.get("rotation_index"),
            "resolution_status": "resolved",
            "resolution_reason": "canonical_training_today_decision_context",
            "selected_session": selected,
            "warnings": [],
        }

    def _canonical_training_context(self, date_value: Any) -> dict[str, Any] | None:
        """Read the one Actions V2 builder shared by cards, snapshots and MCP."""
        if not self.actions.configured:
            return None
        try:
            # The compact GPT view intentionally omits ``planned_session.available``.
            # Decision plan/write need the full canonical contract or they can
            # misclassify a resolved rolling session as a rest day.
            response = self.actions.read("training_today_decision_context_full", {"date": str(date_value or berlin_today())})
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            return result if isinstance(result.get("context_hash"), str) and result.get("context_hash") else None
        except Exception:
            return None

    def _active_plan(self, *, include_plan: bool) -> dict[str, Any] | None:
        row = self.plans.one(
            "SELECT id, title, focus, substr(plan_json, 1, ?) AS plan_json, length(plan_json) AS plan_size_bytes, "
            "is_active, is_archived, updated_at "
            "FROM gym_plans WHERE is_active=1 AND is_archived=0 ORDER BY updated_at DESC, id DESC LIMIT 1",
            (MAX_PLAN_JSON_BYTES + 1,),
        )
        source = "gym_plans"
        if not row:
            row = self.plans.one(
                "SELECT id, name AS title, NULL AS focus, substr(data, 1, ?) AS plan_json, "
                "length(data) AS plan_size_bytes, is_active, 0 AS is_archived, updated_at "
                "FROM plans WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1",
                (MAX_PLAN_JSON_BYTES + 1,),
            )
            source = "plans"
        if not row:
            return None
        raw = row.pop("plan_json", None)
        plan_size = int(row.get("plan_size_bytes") or 0)
        oversized = plan_size > MAX_PLAN_JSON_BYTES
        parsed: Any = None
        parse_error = False
        if raw and not oversized:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parse_error = True
        result = row
        result["source"] = source
        result["plan_oversized"] = oversized
        result["plan_parse_error"] = parse_error
        if include_plan:
            result["plan"] = parsed
        else:
            result["plan_mode"] = ((parsed or {}).get("meta") or {}).get("mode") if isinstance(parsed, dict) else None
        return result

    def recovery_snapshot(self) -> dict[str, Any]:
        latest = self.hrv.one(
            "SELECT id, ts_measurement, date_utc, hr, rmssd, sdnn, signal_quality, "
            "training_motivation, fatigue, sickness, sleep_quality, alcohol, sickness_bool, alcohol_bool, flag "
            "FROM hrv_measurements ORDER BY COALESCE(date_utc, ts_measurement) DESC, id DESC LIMIT 1"
        ) if self.hrv.table_exists("hrv_measurements") else None
        legacy_date = str((latest or {}).get("date_utc") or (latest or {}).get("ts_measurement") or "")[:10] or None
        today = berlin_today().isoformat()

        sleep = self.polar.one(
            "SELECT date, sleep_score, sleep_start, sleep_end, sleep_minutes, actual_sleep_minutes, "
            "deep_sleep_minutes, rem_sleep_minutes, light_sleep_minutes, awake_minutes, interruption_minutes, "
            "interruptions, updated_at FROM polar_sleep WHERE date<=? ORDER BY date DESC LIMIT 1", (today,)
        ) if self.polar.table_exists("polar_sleep") else None
        nightly = self.polar.one(
            "SELECT date, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, "
            "mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, updated_at "
            "FROM polar_nightly_recharge WHERE date<=? ORDER BY date DESC LIMIT 1", (today,)
        ) if self.polar.table_exists("polar_nightly_recharge") else None
        continuous = self.polar.one(
            "SELECT date, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, updated_at "
            "FROM polar_continuous_samples WHERE date<=? ORDER BY date DESC LIMIT 1", (today,)
        ) if self.polar.table_exists("polar_continuous_samples") else None

        cutoff = (berlin_today() - timedelta(days=MAX_RECOVERY_HISTORY_DAYS - 1)).isoformat()
        day_flags = self.hrv.all(
            "SELECT date_iso, sickness_bool, alcohol_bool, substr(note,1,500) AS note, source, updated_at "
            "FROM recovery_day_flags WHERE date_iso BETWEEN ? AND ? ORDER BY date_iso DESC LIMIT 30", (cutoff, today)
        ) if self.hrv.table_exists("recovery_day_flags") else []
        hrv_flags = self.hrv.all(
            "SELECT date, flag, updated_at FROM hrv_day_flags WHERE date BETWEEN ? AND ? ORDER BY date DESC LIMIT 30",
            (cutoff, today),
        ) if self.hrv.table_exists("hrv_day_flags") else []
        annotations = self.hrv.all(
            "SELECT date_iso, annotation_type, substr(value,1,300) AS value, substr(note,1,500) AS note, source, updated_at "
            "FROM recovery_day_annotations WHERE date_iso BETWEEN ? AND ? ORDER BY date_iso DESC, id DESC LIMIT 30",
            (cutoff, today),
        ) if self.hrv.table_exists("recovery_day_annotations") else []

        source_rows = {
            "polar_sleep": (sleep, (sleep or {}).get("date")),
            "polar_nightly_recharge": (nightly, (nightly or {}).get("date")),
            "polar_continuous_hr": (continuous, (continuous or {}).get("date")),
        }
        freshness: dict[str, Any] = {}
        for name, (row, source_date) in source_rows.items():
            status, age = freshness_status(source_date, fresh_days=2)
            freshness[name] = {"status": status, "date": source_date, "age_days": age, "updated_at": (row or {}).get("updated_at")}
        missing = [name for name, (row, _source_date) in source_rows.items() if not row]
        legacy_is_today = legacy_date == today
        today_day_flags = [row for row in day_flags if row.get("date_iso") == today]
        today_hrv_flags = [row for row in hrv_flags if row.get("date") == today]
        today_overlay_flags = [row for row in self.overlay.recent("daily_flags", today) if row.get("date_iso") == today]
        sickness = (legacy_is_today and bool((latest or {}).get("sickness_bool"))) or any(bool(row.get("sickness_bool")) for row in today_day_flags) or any(row.get("flag") == "sick" for row in today_hrv_flags) or any(row.get("flag_type") == "sickness" for row in today_overlay_flags)
        alcohol = (legacy_is_today and bool((latest or {}).get("alcohol_bool"))) or any(bool(row.get("alcohol_bool")) for row in today_day_flags) or any(row.get("flag") == "alcohol" for row in today_hrv_flags) or any(row.get("flag_type") == "alcohol" for row in today_overlay_flags)
        available_statuses = [item["status"] for item in freshness.values() if item["status"] != "missing"]
        overall = "fresh" if "fresh" in available_statuses else "stale" if available_statuses else "missing"
        canonical_fresh = [freshness[name]["status"] == "fresh" for name in ("polar_sleep", "polar_nightly_recharge", "polar_continuous_hr")]
        recovery_state = "current_complete" if all(canonical_fresh) else "current_partial" if any(canonical_fresh) else "stale" if available_statuses else "missing"
        overlay_flags = self.overlay.recent("daily_flags", today)
        for row in overlay_flags:
            row["source"] = "mcp_overlay"
        return snapshot_envelope(
            data_status=overall,
            freshness=freshness,
            sources=[name for name, (row, _date) in source_rows.items() if row] + (["recovery_day_flags"] if day_flags else []) + (["hrv_day_flags"] if hrv_flags else []) + (["recovery_day_annotations"] if annotations else []),
            missing_components=missing,
            parity_level="partial",
            parity_missing=["canonical_readiness_calculation", "28_day_baselines", "recent_flag_resolution"],
            notes="Raw read-only recovery components; no new readiness formula is calculated.",
            payload={
                "canonical_readiness_available": False,
                "recovery_state": recovery_state,
                "current_recovery_available": recovery_state in {"current_complete", "current_partial"},
                "polar": {"sleep": sleep, "nightly_recharge": nightly, "continuous_hr": continuous},
                "flags": {
                    "as_of_date": today,
                    "sickness": sickness,
                    "alcohol": alcohol,
                    "current_flags": {"date": today, "sickness": sickness, "alcohol": alcohol},
                    "historical_day_flags": [row for row in day_flags if row.get("date_iso") != today],
                    "historical_hrv_day_flags": [row for row in hrv_flags if row.get("date") != today],
                    "day_flags": day_flags,
                    "hrv_day_flags": hrv_flags,
                    "mcp_overlay_flags": overlay_flags,
                },
                "annotations": annotations,
            },
        )

    def nutrition_snapshot(self) -> dict[str, Any]:
        settings_rows = self.nutrition.all("SELECT key, value FROM nutrition_settings ORDER BY key")
        settings = {str(row["key"]): row.get("value") for row in settings_rows}
        mode = settings.get("active_mode") or settings.get("selected_mode")
        prefix = f"mode_{mode}_" if mode else ""

        def number(key: str) -> float | None:
            try:
                return float(settings.get(prefix + key))
            except (TypeError, ValueError):
                return None

        today_date = berlin_today()
        today = today_date.isoformat()
        legacy_daily = self.nutrition.one(
            "SELECT date, kcal, protein, carbs, fat FROM nutrition_daily WHERE date=? LIMIT 1", (today,)
        ) if self.nutrition.table_exists("nutrition_daily") else None
        day_actual = self.nutrition.one(
            "SELECT day AS date, kcal, p AS protein, c AS carbs, f AS fat, source, updated_at "
            "FROM nutrition_day_actuals WHERE day=? LIMIT 1", (today,)
        ) if self.nutrition.table_exists("nutrition_day_actuals") else None
        weight_actual = self.nutrition.one(
            "SELECT date_iso AS date, kcal, protein, carbs, fat, created_at FROM weight_logs WHERE date_iso=? "
            "AND (kcal IS NOT NULL OR protein IS NOT NULL OR carbs IS NOT NULL OR fat IS NOT NULL) ORDER BY id DESC LIMIT 1",
            (today,),
        ) if self.nutrition.table_exists("weight_logs") else None

        active_template = self.nutrition.one(
            "SELECT id, title, macro_active_mode, updated_at FROM nutrition_week_templates "
            "WHERE is_active=1 AND archived_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT 1"
        ) if self.nutrition.table_exists("nutrition_week_templates") else None
        template_day = self.nutrition.one(
            "SELECT id, week_template_id, weekday, day_type, target_kcal AS kcal, target_p AS protein, "
            "target_c AS carbs, target_f AS fat, updated_at FROM nutrition_week_template_days "
            "WHERE week_template_id=? AND weekday=? LIMIT 1", (active_template["id"], today_date.weekday())
        ) if active_template and self.nutrition.table_exists("nutrition_week_template_days") else None
        active_plan = self.nutrition.one(
            "SELECT id, title, start_monday, updated_at FROM nutrition_week_plans WHERE is_active=1 "
            "ORDER BY updated_at DESC, id DESC LIMIT 1"
        ) if self.nutrition.table_exists("nutrition_week_plans") else None

        override_row = self.nutrition.one(
            "SELECT substr(value_json,1,?) AS value_json, length(value_json) AS json_bytes, updated_at "
            "FROM actions_v2_state WHERE key='active_targets_override' LIMIT 1", (MAX_DB_JSON_BYTES + 1,)
        ) if self.nutrition.table_exists("actions_v2_state") else None
        override, override_invalid = parse_json_object((override_row or {}).get("value_json"))
        if (override_row or {}).get("json_bytes", 0) > MAX_DB_JSON_BYTES:
            override, override_invalid = None, True

        settings_targets = {"kcal": number("target"), "protein": number("protein_target"), "carbs": number("carbs_target"), "fat": number("fat_target")}
        template_targets = {key: (template_day or {}).get(key) for key in ("kcal", "protein", "carbs", "fat")}
        override_targets = {key: (override or {}).get(key) for key in ("kcal", "protein", "carbs", "fat")}
        if any(value is not None for value in override_targets.values()):
            effective_targets, target_source = override_targets, "active_targets_override"
        elif any(value is not None for value in template_targets.values()):
            effective_targets, target_source = template_targets, "active_week_template_day"
        else:
            effective_targets, target_source = settings_targets, "nutrition_settings"

        actual = weight_actual or day_actual or legacy_daily
        actual_values = {key: (actual or {}).get(key) for key in ("kcal", "protein", "carbs", "fat")}
        difference = {
            key: round(float(effective_targets[key]) - float(actual_values[key]), 1)
            if effective_targets.get(key) is not None and actual_values.get(key) is not None else None
            for key in effective_targets
        }

        day_plan_row = self.nutrition.one(
            "SELECT substr(payload_json,1,?) AS payload_json, length(payload_json) AS json_bytes, rebuilt_at "
            "FROM nutrition_active_day_plan WHERE day_iso=? LIMIT 1", (MAX_DB_JSON_BYTES + 1, today)
        ) if self.nutrition.table_exists("nutrition_active_day_plan") else None
        day_plan, day_plan_invalid = parse_json_object((day_plan_row or {}).get("payload_json"))
        if (day_plan_row or {}).get("json_bytes", 0) > MAX_DB_JSON_BYTES:
            day_plan, day_plan_invalid = None, True
        planned_meals = []
        for meal in ((day_plan or {}).get("planned_meals") or [])[:MAX_PLANNED_MEALS]:
            if isinstance(meal, dict):
                planned_meals.append({key: meal.get(key) for key in ("slot_id", "source_of_truth", "state", "time_text", "title", "status", "logged_meal_id") if key in meal})
        timing = [{"slot_id": meal.get("slot_id"), "time_text": meal.get("time_text"), "title": meal.get("title")} for meal in planned_meals if meal.get("time_text")]
        actual_timestamp = (weight_actual or {}).get("created_at") or (day_actual or {}).get("updated_at") or None
        actual_status, actual_age = freshness_status(str((actual or {}).get("date") or ""), fresh_days=2)
        plan_status, plan_age = freshness_status(today if day_plan else None, fresh_days=1)
        missing = []
        if not actual: missing.append("today_actuals")
        if not planned_meals: missing.append("planned_meals")
        if not active_template: missing.append("active_week_template")
        if override_invalid: missing.append("valid_active_targets_override")
        if day_plan_invalid: missing.append("valid_active_day_plan")
        data_status = "ok" if effective_targets and actual else "partial" if effective_targets or actual else "missing"
        return snapshot_envelope(
            data_status=data_status,
            freshness={"actuals": {"status": actual_status, "date": (actual or {}).get("date"), "age_days": actual_age, "updated_at": actual_timestamp}, "day_plan": {"status": plan_status, "date": today if day_plan else None, "age_days": plan_age, "rebuilt_at": (day_plan_row or {}).get("rebuilt_at")}},
            sources=["nutrition_settings"] + (["active_targets_override"] if override else []) + (["active_week_template_day"] if template_day else []) + (["nutrition_day_actuals"] if day_actual else []) + (["weight_logs"] if weight_actual else []) + (["nutrition_daily"] if legacy_daily else []) + (["nutrition_active_day_plan"] if day_plan else []),
            missing_components=missing,
            parity_level="partial",
            parity_missing=["canonical_adherence_summary", "logging_streak", "full_meal_items", "canonical_timing_analysis"],
            notes="Target priority mirrors actions_v2: active override, then active template day, then settings; planning remains compact.",
            payload={"mode": mode, "effective_targets": effective_targets, "target_source": target_source, "target_candidates": {"active_targets_override": override_targets, "active_week_template_day": template_targets, "nutrition_settings": settings_targets}, "active_week_template": active_template, "active_plan": active_plan, "today_actuals": actual_values if actual else None, "actual_source": "weight_logs" if weight_actual else "nutrition_day_actuals" if day_actual else "nutrition_daily" if legacy_daily else None, "nutrition_daily": legacy_daily, "difference_target_minus_actual": difference, "planned_meals": planned_meals, "timing": timing, "mcp_context_logs": self._overlay_nutrition(today)},
        )

    def weight_state(self, limit: int = 14) -> dict[str, Any]:
        today = berlin_today().isoformat()
        rows = self.nutrition.all(
            "SELECT id, date_iso AS date, weight_kg, created_at FROM weight_logs "
            "WHERE weight_kg IS NOT NULL AND date(date_iso) <= date(?) "
            "ORDER BY date(date_iso) DESC, id DESC LIMIT ?",
            (today, limit),
        )
        trend_rows = self.nutrition.all(
            "SELECT id, date_iso AS date, weight_kg AS weight, created_at FROM weight_logs "
            "WHERE date(date_iso) <= date(?) ORDER BY date(date_iso) DESC, id DESC LIMIT 1000",
            (today,),
        )
        weight_trend = build_weight_trend(trend_rows, today)
        status, age_days = freshness_status((rows[0] if rows else {}).get("date"))
        return {
            "latest": rows[0] if rows else None,
            "recent": rows,
            "average": weight_trend["calendar_week"]["current"]["mean_kg"],
            "units": {"weight_kg": "kg"},
            "latest_weight_age_days": age_days,
            "data_status": status,
            "weight_trend": weight_trend,
        }

    def _garmin_activity_details(self, external_id: str) -> dict[str, Any] | None:
        if not self.runs.table_exists("garmin_activity_payloads"):
            return None
        payload = self.runs.one(
            "SELECT garmin_activity_id, summary_json, details_json, splits_json, typed_splits_json, "
            "split_summaries_json, weather_json, hr_zones_json, power_zones_json, gear_json, "
            "errors_json, fetched_at, updated_at FROM garmin_activity_payloads WHERE external_id=?",
            (external_id,),
        )
        if not payload:
            return None

        details = parse_json_value(payload.pop("details_json")) or {}
        compact_details = {
            "measurement_count": details.get("measurementCount"),
            "metrics_count": details.get("metricsCount"),
            "total_metrics_count": details.get("totalMetricsCount"),
            "metric_descriptors": details.get("metricDescriptors"),
            "details_available": details.get("detailsAvailable"),
            "pending_data": details.get("pendingData"),
        }
        result: dict[str, Any] = {
            "activity_id": payload["garmin_activity_id"],
            "summary": parse_json_value(payload["summary_json"]),
            "detail_metadata": compact_details,
            "splits": parse_json_value(payload["splits_json"]),
            "typed_splits": parse_json_value(payload["typed_splits_json"]),
            "split_summaries": parse_json_value(payload["split_summaries_json"]),
            "weather": parse_json_value(payload["weather_json"]),
            "heart_rate_zones": parse_json_value(payload["hr_zones_json"]),
            "power_zones": parse_json_value(payload["power_zones_json"]),
            "gear": parse_json_value(payload["gear_json"]),
            "fetch_errors": parse_json_value(payload["errors_json"]) or {},
            "fetched_at": payload["fetched_at"],
            "updated_at": payload["updated_at"],
        }

        if self.runs.table_exists("garmin_activity_laps"):
            result["laps"] = self.runs.all(
                "SELECT lap_index, start_time, distance_m, duration_sec, moving_time_sec, avg_hr, max_hr, "
                "avg_speed_mps, max_speed_mps, avg_cadence, max_cadence, calories, elevation_gain_m, "
                "elevation_loss_m, min_elevation_m, max_elevation_m, stride_length_m, start_latitude, "
                "start_longitude, end_latitude, end_longitude FROM garmin_activity_laps "
                "WHERE external_id=? ORDER BY lap_index",
                (external_id,),
            )

        if self.runs.table_exists("garmin_activity_samples"):
            count_row = self.runs.one(
                "SELECT COUNT(*) AS sample_count FROM garmin_activity_samples WHERE external_id=?",
                (external_id,),
            ) or {"sample_count": 0}
            source_count = int(count_row["sample_count"])
            max_samples = 2000
            stride = max(1, (source_count + max_samples - 1) // max_samples)
            samples = self.runs.all(
                "SELECT sample_index, timestamp_ms, elapsed_sec, moving_sec, distance_m, heart_rate_bpm, "
                "speed_mps, cadence_spm, elevation_m, latitude, longitude, vertical_speed_mps, "
                "performance_condition FROM garmin_activity_samples WHERE external_id=? "
                "AND sample_index % ? = 0 ORDER BY sample_index",
                (external_id, stride),
            )
            result["sample_series"] = {
                "source_count": source_count,
                "returned_count": len(samples),
                "sampling_stride": stride,
                "is_downsampled": stride > 1,
                "samples": samples,
            }

        if self.runs.table_exists("garmin_activity_files"):
            result["original_file"] = self.runs.one(
                "SELECT format, size_bytes, sha256, downloaded_at FROM garmin_activity_files WHERE external_id=?",
                (external_id,),
            )
        return result

    def runs_state(self, limit: int = 10, *, include_garmin_details: bool = True) -> dict[str, Any]:
        today = berlin_today().isoformat()
        rows = self.runs.all(
            "SELECT * FROM runs "
            "WHERE date(date) <= date(?) ORDER BY date(date) DESC, id DESC LIMIT ?",
            (today, limit),
        )
        if include_garmin_details:
            for row in rows:
                external_id = row.get("external_id")
                if row.get("source") == "garmin" and isinstance(external_id, str):
                    row["garmin_details"] = self._garmin_activity_details(external_id)
        status, age_days = freshness_status((rows[0] if rows else {}).get("date"))
        return {
            "recent": rows,
            "units": {"distance": "m", "moving_time": "s", "pace": "s_per_km", "heart_rate": "bpm", "avg_power": "W", "speed": "m/s", "cadence": "steps/min", "elevation": "m"},
            "latest_activity_age_days": age_days,
            "data_status": status,
        }

    def context_snapshot(self) -> dict[str, Any]:
        """Compact, read-only context assembled from established snapshot readers."""
        generated_at = datetime.now(BERLIN).isoformat()
        missing: list[str] = []; stale: list[str] = []; warnings: list[str] = []
        def read(name: str, call: Any) -> dict[str, Any]:
            try:
                value = call()
            except Exception:
                missing.append(name); warnings.append(f"{name}:unavailable")
                return {}
            status = value.get("data_status", "missing")
            if status in {"missing", "invalid"}: missing.append(name)
            elif status in {"stale"}: stale.append(name)
            return value
        training = read("training", lambda: self.training_state(1))
        recovery = read("recovery", self.recovery_snapshot)
        nutrition = read("nutrition", self.nutrition_snapshot)
        weight = read("weight", lambda: self.weight_state(14))
        runs = read("running", lambda: self.runs_state(1, include_garmin_details=False))
        today = berlin_today().isoformat()
        calendar = self._calendar_today(today)
        if not calendar.get("available"): missing.append("calendar")
        decision = self._canonical_snapshot_decision(today) or self._persisted_ai_decision(today)
        latest_run = (runs.get("recent") or [None])[0]
        latest_training = (training.get("recent_sessions") or [None])[0]
        latest_weight = weight.get("latest")
        polar = recovery.get("polar") or {}
        actual = nutrition.get("today_actuals")
        status = "attention" if stale or warnings else "insufficient_data" if len(missing) >= 3 else "ok"
        return {"generated_at": generated_at, "timezone": "Europe/Berlin", "period": {"date": today},
            "summary": {"status": status, "headline": "Current LIVA context snapshot", "attention_items": sorted(set(stale + warnings))},
            "weight": {"latest": latest_weight, "average_kg": weight.get("average"), "age_days": weight.get("latest_weight_age_days")},
            "training": {"last_completed": latest_training, "sessions_last_14_days": training.get("sessions_last_14_days"), "active_plan": training.get("active_plan"), "current_decision": decision},
            "recovery": {"sleep": polar.get("sleep"), "nightly_recharge": polar.get("nightly_recharge"), "freshness": recovery.get("freshness"), "flags": (recovery.get("flags") or {})},
            "nutrition": {"actuals": actual, "targets": nutrition.get("effective_targets"), "difference_target_minus_actual": nutrition.get("difference_target_minus_actual"), "actual_source": nutrition.get("actual_source"), "freshness": nutrition.get("freshness")},
            "running": {"last_run": latest_run, "age_days": runs.get("latest_activity_age_days")},
            "tasks": {"status": "not_configured"},
            "calendar": {"next_event": calendar.get("next_event"), "freshness": calendar.get("freshness")},
            "system": {"status": "not_included"},
            "memory": {"canonical_backend": "liva_memory_v2", "canonical_write_tool": "liva_memory_begin", "canonical_recall_tool": "liva_memory_recall", "legacy_memos": "read_only_archive", "normal_flow": "recall/begin -> finish"},
            "routing": routing_contract(),
            "capabilities": self.capability_state(),
            "data_quality": {"missing": sorted(set(missing)), "stale": sorted(set(stale)), "warnings": sorted(set(warnings))}}

    def daily_snapshot(self) -> dict[str, Any]:
        training = self.training_state(limit=3)
        recovery = self.recovery_snapshot()
        nutrition = self.nutrition_snapshot()
        weight = self.weight_state(limit=7)
        runs = self.runs_state(limit=3)
        today = berlin_today().isoformat()
        calendar = self._calendar_today(today)
        decision = self._canonical_snapshot_decision(today) or self._persisted_ai_decision(today)
        training_card = self._persisted_training_card(today)
        core_context = self._core_context(today)
        planned_session = None
        if decision:
            planned_session = {key: decision.get(key) for key in ("plan_id", "planned_session_id", "session_name", "session_type", "status")}
            planned_session["source"] = "persisted_training_ai_decision"
        missing = []
        for name, value in (("calendar", calendar.get("available")), ("planned_session", planned_session), ("training_card", training_card), ("ai_decision", decision), ("core_phase", core_context.get("phase")), ("core_mode", core_context.get("mode"))):
            if not value: missing.append(name)
        freshness = {"training": training.get("data_status"), "recovery": recovery.get("data_status"), "nutrition": nutrition.get("data_status"), "weight": weight.get("data_status"), "runs": runs.get("data_status"), "calendar": calendar.get("freshness", {}).get("status"), "ai_decision": (decision or {}).get("status") or "missing", "training_card": "available" if training_card else "missing"}
        return snapshot_envelope(
            data_status="partial" if missing else "ok",
            freshness=freshness,
            sources=["snapshot_aggregates", "school_schedule_snapshots"] + (["training_ai_decisions"] if decision else []) + (["core_training_cards"] if training_card else []) + (["core.actions_v2_state"] if core_context else []),
            missing_components=missing,
            parity_level="partial",
            parity_missing=["google_calendar_cache", "canonical_training_resolver", "context_hash_recalculation", "core_live_decision", "endurance_approval"],
            notes="Aggregates persisted and directly readable state only; it does not build boards, cards, decisions, or coaching output.",
            payload={"calendar": calendar, "training": {**training, "planned_session": planned_session, "persisted_training_card": training_card, "persisted_ai_decision": decision}, "core": core_context, "recovery": recovery, "nutrition": nutrition, "weight": weight, "runs": runs, "mcp_write_context": self._overlay_context(today)},
        )

    def _calendar_today(self, today: str) -> dict[str, Any]:
        if not self.training.table_exists("school_schedule_snapshots"):
            return {"available": False, "events": [], "next_event": None, "freshness": {"status": "missing"}}
        snapshot = self.training.one(
            "SELECT id, fetched_at, source, substr(derived_summary_json,1,?) AS summary_json, "
            "length(derived_summary_json) AS summary_bytes FROM school_schedule_snapshots "
            "WHERE date=? ORDER BY fetched_at DESC, id DESC LIMIT 1", (MAX_DB_JSON_BYTES + 1, today)
        )
        events = []
        if snapshot and self.training.table_exists("school_schedule_entries"):
            events = self.training.all(
                "SELECT id, subject AS title, substr(teacher,1,120) AS teacher, substr(room,1,120) AS location, "
                "start_time, end_time, status_hint AS status FROM school_schedule_entries "
                "WHERE snapshot_id=? AND date=? ORDER BY start_time, id LIMIT ?",
                (snapshot["id"], today, MAX_CALENDAR_EVENTS),
            )
        summary, invalid = parse_json_object((snapshot or {}).get("summary_json"))
        status = "missing"
        age_hours = None
        fetched_at = (snapshot or {}).get("fetched_at")
        if fetched_at:
            try:
                parsed = datetime.fromisoformat(str(fetched_at))
                if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=BERLIN)
                age_hours = round((datetime.now(BERLIN) - parsed.astimezone(BERLIN)).total_seconds() / 3600, 1)
                status = "fresh" if 0 <= age_hours <= 36 else "stale"
            except ValueError:
                status = "stale"
        now_time = datetime.now(BERLIN).strftime("%H:%M")
        next_event = next((event for event in events if str(event.get("start_time") or "") >= now_time), None)
        return {"available": bool(snapshot), "events": events, "event_count": len(events), "next_event": next_event, "summary": None if invalid else summary, "freshness": {"status": status, "fetched_at": fetched_at, "age_hours": age_hours}, "limit": MAX_CALENDAR_EVENTS}

    def _persisted_ai_decision(self, today: str) -> dict[str, Any] | None:
        if not self.core.table_exists("training_ai_decisions"):
            return None
        row = self.core.one(
            "SELECT id, day_iso, planned_session_id, plan_id, session_name, session_type, status, source, "
            "context_hash, trigger_reason, invalidated_by, created_at, updated_at, "
            "substr(decision_json,1,?) AS decision_json, length(decision_json) AS decision_bytes "
            "FROM training_ai_decisions WHERE day_iso=? AND status!='superseded' ORDER BY updated_at DESC,id DESC LIMIT 1",
            (MAX_DB_JSON_BYTES + 1, today),
        )
        if not row: return None
        raw = row.pop("decision_json", None)
        oversized = int(row.get("decision_bytes") or 0) > MAX_DB_JSON_BYTES
        parsed, invalid = parse_json_object(raw if not oversized else None)
        row["decision"] = parsed
        row["decision_oversized"] = oversized
        row["decision_parse_error"] = invalid
        current_hash = self._canonical_training_context_hash(today)
        saved_hash = str(row.get("context_hash") or "") or None
        row["current_context_hash"] = current_hash
        if current_hash and saved_hash != current_hash:
            row["status"] = "stale"
            row["stale_reason"] = "context_hash_changed"
        else:
            row["stale_reason"] = None
        return row

    def _canonical_snapshot_decision(self, today: str) -> dict[str, Any] | None:
        """Use the same live freshness calculation that powers the training card."""
        if not self.actions.configured:
            return None
        try:
            response = self.actions.read("training_today_card", {"date": today})
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            decision = result.get("ai_decision") if isinstance(result.get("ai_decision"), dict) else None
            if not decision:
                return None
            return {**decision, "source": "canonical_actions.training_today_card"}
        except Exception:
            return None

    def _persisted_training_card(self, today: str) -> dict[str, Any] | None:
        if not self.core.table_exists("core_training_cards"):
            return None
        row = self.core.one(
            "SELECT id, day_iso, board_id, updated_at, source, status, session_type, session_label, "
            "decision_intent, substr(title,1,200) AS title, substr(summary,1,500) AS summary, "
            "substr(card_json,1,?) AS card_json, length(card_json) AS card_bytes FROM core_training_cards "
            "WHERE day_iso=? AND status='active' ORDER BY updated_at DESC,id DESC LIMIT 1",
            (MAX_DB_JSON_BYTES + 1, today),
        )
        if not row: return None
        raw = row.pop("card_json", None)
        oversized = int(row.get("card_bytes") or 0) > MAX_DB_JSON_BYTES
        parsed, invalid = parse_json_object(raw if not oversized else None)
        row["card"] = parsed
        row["card_oversized"] = oversized
        row["card_parse_error"] = invalid
        return row

    def _core_context(self, today: str) -> dict[str, Any]:
        phase = None
        sidecar = None
        if self.core.table_exists("actions_v2_state"):
            for key in ("phase", "core_override"):
                row = self.core.one("SELECT substr(value_json,1,?) AS value_json, updated_at FROM actions_v2_state WHERE key=?", (MAX_DB_JSON_BYTES + 1, key))
                parsed, _invalid = parse_json_object((row or {}).get("value_json"))
                if key == "phase": phase = parsed
                else: sidecar = parsed
        live_choice = self.training.one("SELECT choice, created_at FROM core_override_choices WHERE date=? LIMIT 1", (today,)) if self.training.table_exists("core_override_choices") else None
        autopilot = self.training.one("SELECT mode, created_ts FROM autopilot_day WHERE date=? LIMIT 1", (today,)) if self.training.table_exists("autopilot_day") else None
        mode = (live_choice or {}).get("choice") or ((sidecar or {}).get("mode") if (sidecar or {}).get("override_active") else None) or (autopilot or {}).get("mode")
        source = "core_override_choices" if live_choice else "actions_v2_state.core_override" if sidecar and (sidecar or {}).get("override_active") else "autopilot_day" if autopilot else None
        return {"phase": phase, "mode": mode, "mode_source": source, "canonical_live_decision_available": False}

    def memory_search(self, query_text: str | None, limit: int, include_overlay: bool = False, active_only: bool = True, include_archived: bool = False, include_superseded: bool = False, sort: str = "relevance", origin: str | None = None, domain: str | None = None, memory_type: str | None = None, temporal_scope: str | None = None, valid_at: str | None = None, include_expired: bool = False) -> dict[str, Any]:
        if not self.config.memos_base_url:
            return self._overlay_memory_search(query_text, limit) if include_overlay else {"memos": [], "count": 0, "backend": "usememos"}
        rows: list[dict[str, Any]] = []
        states = ["NORMAL", "ARCHIVED"] if include_archived else ["NORMAL"]
        for state in states:
            rows.extend(self._memos_catalog(state))
        needle = (query_text or "").casefold()
        superseded_names: set[str] = set()
        try:
            import sqlite3
            if self.config.write_database_path().exists():
                with sqlite3.connect(self.config.write_database_path()) as conn:
                    superseded_names = {str(row[0]) for row in conn.execute("SELECT old_memo_name FROM memory_relations")}
        except sqlite3.Error:
            superseded_names = set()
        metadata: dict[str, dict[str, Any]] = {}
        try:
            import sqlite3
            if self.config.write_database_path().exists():
                with sqlite3.connect(self.config.write_database_path()) as conn:
                    conn.row_factory = sqlite3.Row
                    metadata = {str(row["memo_name"]): dict(row) for row in conn.execute("SELECT * FROM memory_metadata")}
        except sqlite3.Error:
            metadata = {}
        now = datetime.now(BERLIN).isoformat()
        check_at = valid_at or now
        results = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            haystack = " ".join(str(row.get(key) or "") for key in ("content", "snippet", "name")).casefold()
            if needle and needle not in haystack:
                continue
            state = str(row.get("state") or "NORMAL").upper()
            memo_name = str(row.get("name") or "")
            is_superseded = memo_name in superseded_names
            if is_superseded and not include_superseded: continue
            if active_only and not include_archived and state != "NORMAL": continue
            if not include_archived and state != "NORMAL": continue
            content = str(row.get("content") or row.get("snippet") or "")
            tags = ["#" + match.group(1).lower() for match in re.finditer(r"(?<!\w)#([\wäöüÄÖÜß-]+)", content)]
            meta = metadata.get(memo_name)
            if meta:
                if meta.get("valid_until") and str(meta["valid_until"]) < check_at and not include_expired: continue
                if origin and meta.get("origin") != origin: continue
                if domain and meta.get("domain") != domain: continue
                if memory_type and meta.get("memory_type") != memory_type: continue
                if temporal_scope and meta.get("temporal_scope") != temporal_scope: continue
            elif domain and f"#{domain.casefold()}" not in tags:
                # Direct user-authored Memos intentionally have no MCP metadata.
                # Their visible tag is the canonical read-only collection boundary.
                continue
            item = {"memo_name": memo_name, "snippet": content[:500], "tags": list(dict.fromkeys(tags)), "created_at": row.get("createTime"), "updated_at": row.get("updateTime"), "status": "superseded" if is_superseded else ("active" if state == "NORMAL" else "archived"), "source": "memos", "legacy_metadata": meta is None}
            if meta:
                item.update({key: meta.get(key) for key in ("origin", "domain", "memory_type", "temporal_scope", "event_date", "valid_from", "valid_until", "policy_version")})
            results.append(item)
        sort_applied = sort
        if sort == "newest_created": results.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        elif sort == "newest_updated": results.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        results = results[:limit]
        if include_overlay:
            overlay = self._overlay_memory_search(query_text, limit)
            results = ((overlay.get("memos") or []) + results)[:limit]
        return {"memos": results, "count": len(results), "backend": "usememos" if not include_overlay else "usememos+overlay", "sort_applied": sort_applied}

    def _overlay_nutrition(self, today: str) -> list[dict[str, Any]]:
        rows = self.overlay.recent("nutrition_context_logs", today)
        for row in rows:
            try: row["values"] = json.loads(row.pop("values_json"))
            except (json.JSONDecodeError, TypeError): row["values"] = {}
            row["source"] = "mcp_overlay"
        return rows

    def _overlay_context(self, today: str) -> dict[str, Any]:
        def rows(table: str) -> list[dict[str, Any]]:
            value = self.overlay.recent(table, today)
            for item in value:
                item["source"] = "mcp_overlay"
            return value
        return {"daily_notes": rows("daily_notes"), "daily_flags": rows("daily_flags"), "nutrition_context": self._overlay_nutrition(today), "training_rawlogs": rows("training_rawlogs")}

    def _overlay_memory_search(self, query_text: str | None, limit: int) -> dict[str, Any]:
        path = self.config.write_database_path()
        if not path.exists(): return {"memos": [], "count": 0, "backend": "liva_mcp_overlay"}
        import sqlite3
        needle = (query_text or "").casefold()
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute("SELECT id,text,tags_json,scope,status,updated_at FROM memory_entries WHERE status='active' ORDER BY updated_at DESC")]
        result=[]
        for row in rows:
            if needle and needle not in (row["text"] + " " + row["tags_json"]).casefold(): continue
            result.append({"name": f"liva-mcp-memory/{row['id']}", "snippet": row["text"][:500], "tags": json.loads(row["tags_json"]), "scope": row["scope"], "state": row["status"], "updateTime": row["updated_at"]})
            if len(result)>=limit: break
        return {"memos": result, "count": len(result), "backend": "liva_mcp_overlay"}

    def _overlay_memory_read(self, memo_id: str) -> dict[str, Any]:
        if not memo_id.isdigit() or not self.config.write_database_path().exists():
            raise RuntimeError("Overlay memory entry is unavailable")
        import sqlite3
        with sqlite3.connect(self.config.write_database_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT id,text,tags_json,scope,status,created_at,updated_at,supersedes_id FROM memory_entries WHERE id=?", (int(memo_id),)).fetchone()
        if not row:
            raise RuntimeError("Overlay memory entry is unavailable")
        value = dict(row)
        return {"memo": {"name": f"liva-mcp-memory/{value['id']}", "content": value["text"], "tags": json.loads(value["tags_json"]), "scope": value["scope"], "state": value["status"], "createTime": value["created_at"], "updateTime": value["updated_at"], "supersedes_id": value["supersedes_id"]}, "backend": "liva_mcp_overlay"}

    def memory_read(self, memo_id: str) -> dict[str, Any]:
        if memo_id.startswith("liva-mcp-memory/"):
            return self._overlay_memory_read(memo_id.removeprefix("liva-mcp-memory/"))
        resource_name = memo_id if memo_id.startswith("memos/") else "memos/" + memo_id
        payload = self._memos_get(f"/api/v1/memos/{parse.quote(resource_name.removeprefix('memos/'), safe='')}")
        if not isinstance(payload, dict):
            raise RuntimeError("Memos returned an invalid response")
        value = self._safe_memo(payload, include_content=True)
        content = str(payload.get("content") or "")
        tags = ["#" + match.group(1).lower() for match in re.finditer(r"(?<!\w)#([\wäöüÄÖÜß-]+)", content)]
        value.update({"memo_name": payload.get("name", resource_name), "status": "active" if str(payload.get("state") or "NORMAL").upper() == "NORMAL" else "archived", "source": "memos", "content": content, "tags": list(dict.fromkeys(tags))})
        try:
            import sqlite3
            if self.config.write_database_path().exists():
                with sqlite3.connect(self.config.write_database_path()) as conn:
                    conn.row_factory = sqlite3.Row
                    meta = conn.execute("SELECT * FROM memory_metadata WHERE memo_name=?", (value["memo_name"],)).fetchone()
                    if meta:
                        value.update({key: meta[key] for key in ("origin", "domain", "memory_type", "temporal_scope", "event_date", "valid_from", "valid_until", "policy_version", "supersedes", "superseded_by", "edited_at")})
                        value["legacy_metadata"] = False
                    else:
                        value["legacy_metadata"] = True
        except sqlite3.Error:
            value["legacy_metadata"] = True
        return {"memo": value, "backend": "usememos"}

    def _memos_get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.memos_base_url:
            raise RuntimeError("usememos read access is not configured")
        base = self.config.memos_base_url.rstrip("/")
        url = base + path
        if query:
            url += "?" + parse.urlencode(query)
        headers = {"Accept": "application/json"}
        memos_token = self.config.memos_read_token or self.config.memos_write_token
        if memos_token:
            headers["Authorization"] = "Bearer " + memos_token
        req = request.Request(url, headers=headers, method="GET")
        opener = request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=10) as response:
                content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
                if content_length and int(content_length) > MAX_MEMOS_RESPONSE_BYTES:
                    raise RuntimeError("usememos response exceeds the size limit")
                raw_bytes = response.read(MAX_MEMOS_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_MEMOS_RESPONSE_BYTES:
                    raise RuntimeError("usememos response exceeds the size limit")
                raw = raw_bytes.decode("utf-8")
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise RuntimeError("usememos read request failed") from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("usememos returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("usememos returned an invalid response")
        return parsed

    @staticmethod
    def _safe_memo(row: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
        allowed = ("name", "createTime", "updateTime", "state", "visibility", "tags", "snippet")
        result = {key: row.get(key) for key in allowed if key in row}
        if include_content:
            result["content"] = row.get("content")
        return result
