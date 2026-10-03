from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from typing import Any

DAY_ORDER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DAY_SET = set(DAY_ORDER)
EM_DASH = "\u2014"

DAY_HEADER_RE = re.compile(r"^##\s+(Mo|Di|Mi|Do|Fr|Sa|So)\s*$")
BLOCK_HEADER_RE = re.compile(r"^##\s+(.+?)(?:\s*\((\d+)\s+Wochen\))?\s*$")
EVENT_HEADER_RE = re.compile(
    r"^-\s*\[(?P<time>[^\]]+)\]\s*(?P<kind>Gym|Run|Ergo|Bike|Row|Swim|Rest)(?:\s*[\-\u2013\u2014]\s*(?P<title>.*?))?\s*\((?P<freq>\d+)x\)\s*$",
    re.IGNORECASE,
)
BLOCK_DAY_RE = re.compile(r"^-\s*\[(Mo|Di|Mi|Do|Fr|Sa|So)\]\s+(.+)\s+\((\d+)x\)\s*$")
EXERCISE_ITEM_RE = re.compile(
    r"^\s*-\s*(?P<name>[^:]+?)\s*:\s*(?P<sets>\d+)x(?P<repmin>\d+)(?:-(?P<repmax>\d+))?(?:\s*@RPE\s*(?P<rpe>[\d\./\s]+))?\s*(?P<note>.*)$",
    re.IGNORECASE,
)
REF_BLOCK_RE = re.compile(
    r'^\s*-\s*(?P<display>[^:]+?)\s*:\s*siehe\s*"(?P<block_name>[^"]+)"(?:\s+unten)?\s*$',
    re.IGNORECASE,
)
RUN_REF_BLOCK_RE = re.compile(
    r'^\s*-\s*siehe\s*"(?P<block_name>[^"]+)"(?:\s+unten)?\s*$',
    re.IGNORECASE,
)
RUN_ITEM_RE = re.compile(r"^\s*-\s*(?P<text>.+)$")
RUN_PULSE_RE = re.compile(
    r"(?P<dur_min>\d+)(?:\s*[\-\u2013\u2014]\s*(?P<dur_max>\d+))?\s*min\s*@Puls\s*"
    r"(?P<low>\d+)\s*[\-\u2013\u2014]\s*(?P<high>\d+)"
    r"(?:\s*(?P<unit>%\s*HFmax|bpm))?(?:\s*\((?P<note>.*)\))?",
    re.IGNORECASE,
)
VARIATION_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<variation>[^()]+)\)\s*$")
RULES_HEADER_RE = re.compile(r"^##\s+Wochenregeln\b.*$", re.IGNORECASE)
RULE_ROW_RE = re.compile(r"^-\s*W(?P<week>\d+)\s*:\s*(?P<body>.+?)\s*$", re.IGNORECASE)
RUN_DURATION_RE = re.compile(r"(\d+)(?:\s*[\-\u2013\u2014]\s*(\d+))?\s*min", re.IGNORECASE)
WEEK_LOGIC_HEADER_RE = re.compile(r"^##\s+Wochenlogik.*$", re.IGNORECASE)
TITLE_HEADER_RE = re.compile(r"^#\s+(.+?)\s*$")

DEFAULT_MIN_RPE = 5.0
DEFAULT_RPE_ROUND_STEP = 0.5
DEFAULT_RUN_STEP_MIN = 5.0
DEFAULT_EXERCISE_PRIORITY = "secondary"
ENDURANCE_EVENT_KINDS = {"run", "ergo", "bike", "row", "swim"}
CARDIO_EVENT_KINDS = {"cardio", *ENDURANCE_EVENT_KINDS}
ALL_EVENT_KINDS = {"gym", "rest", "note", *CARDIO_EVENT_KINDS}
PLAN_MODES = {"fixed_week", "rolling_sequence"}
ROLLING_SECTION_RE = re.compile(r"^##\s+Rotation\s*$", re.IGNORECASE)
ROLLING_EVENT_RE = re.compile(
    r"^-\s*\[(?P<kind>Gym|Rest|Note|Cardio(?:\s*:\s*(?P<mode>Run|Bike|Ergo|Row|Swim|Cardio))?)\]\s*(?P<title>.*?)(?:\s*\((?P<freq>\d+)x\))?\s*$",
    re.IGNORECASE,
)

EXERCISE_CANONICAL_MAP = {
    "hammer curls": "Hammers",
    "hammers": "Hammers",
}

ROLLING_PATTERN_ALLOWED = {"train", "optional", "rest"}
DEFAULT_ROLLING_WEEK_PATTERN = {
    "Mo": "train",
    "Di": "optional",
    "Mi": "train",
    "Do": "rest",
    "Fr": "train",
    "Sa": "optional",
    "So": "rest",
}


def _extract_weeks_hint_from_rules_header(line: str) -> int | None:
    m = re.search(r"\((\d+)\s+Wochen\)", str(line or ""), flags=re.IGNORECASE)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except Exception:
        return None
    return max(1, min(52, val))


def _safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    return num if math.isfinite(num) else None


def _canonicalize_exercise_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return raw
    key = raw.lower()
    mapped = EXERCISE_CANONICAL_MAP.get(key)
    return mapped if mapped else raw


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def _round_int(value: float) -> int:
    return int(math.floor(value + 0.5))


def _cap_step_up_limit(priority: str, opts: dict[str, Any]) -> int:
    global_up = int(opts.get("max_step_up", 1))
    priority_up = {
        "assist": opts.get("assist_max_step_up", global_up),
        "secondary": opts.get("secondary_max_step_up", global_up),
        "main": opts.get("main_max_step_up", global_up),
    }
    return max(0, int(priority_up.get(priority, global_up)))


def apply_rpe_cap(set_rpes: list[float], cap: float, opts: dict[str, Any] | None = None) -> list[float]:
    values = list(set_rpes or [])
    if not values:
        return []

    cfg = opts or {}
    min_rpe = _safe_float(cfg.get("min_rpe"))
    round_step = _safe_float(cfg.get("round_step"))
    min_rpe = DEFAULT_MIN_RPE if min_rpe is None else min_rpe
    round_step = DEFAULT_RPE_ROUND_STEP if round_step is None else abs(round_step)

    cap_f = _safe_float(cap)
    if cap_f is None or cap_f <= 0:
        return values

    parsed: list[float] = []
    for val in values:
        num = _safe_float(val)
        if num is None or num < 0:
            return values
        parsed.append(num)

    top = max(parsed)
    if cap_f >= top:
        return values

    new_top = min(top, cap_f)
    out: list[float] = []
    for current in parsed:
        delta = top - current
        lowered = new_top - delta
        lowered = max(min_rpe, lowered)
        if round_step > 0:
            lowered = _round_to_step(lowered, round_step)
        lowered = max(min_rpe, lowered)
        out.append(float(lowered))
    return out


def scale_sets(
    baseline_sets: int,
    factor: float,
    exercise_priority: str = DEFAULT_EXERCISE_PRIORITY,
    opts: dict[str, Any] | None = None,
) -> int:
    cfg = opts or {}
    min_sets = max(1, int(cfg.get("min_sets", 1)))
    max_total_up = max(0, int(cfg.get("max_total_up", 2)))
    priority = str(exercise_priority or DEFAULT_EXERCISE_PRIORITY).strip().lower()

    base_raw = _safe_float(baseline_sets)
    factor_f = _safe_float(factor)
    if base_raw is None or base_raw < 0 or factor_f is None or factor_f <= 0:
        return max(min_sets, int(baseline_sets or 0))

    base = max(0, int(round(base_raw)))
    target = _round_int(base * factor_f)

    upper_limit = base + _cap_step_up_limit(priority, cfg)
    upper_limit = min(upper_limit, base + max_total_up)

    scaled = target
    if scaled > upper_limit:
        scaled = upper_limit
    if scaled < min_sets:
        scaled = min_sets
    return scaled


def scale_run_duration_or_reps(baseline: float, run_factor: float, opts: dict[str, Any] | None = None) -> float:
    cfg = opts or {}
    mode = str(cfg.get("mode") or "duration").strip().lower()
    baseline_f = _safe_float(baseline)
    factor_f = _safe_float(run_factor)
    if baseline_f is None or baseline_f < 0 or factor_f is None or factor_f <= 0:
        return baseline_f if baseline_f is not None else 0.0

    if mode == "interval_reps":
        min_value = max(1, int(cfg.get("min_value", 1)))
        max_total_up = max(0, int(cfg.get("max_total_up", 2)))
        max_step_up = max(0, int(cfg.get("max_step_up", 1)))
        base_i = max(0, int(round(baseline_f)))
        target_i = _round_int(base_i * factor_f)
        upper = min(base_i + max_total_up, base_i + max_step_up)
        return float(max(min_value, min(target_i, upper)))

    step = _safe_float(cfg.get("step"))
    step = DEFAULT_RUN_STEP_MIN if step is None or step <= 0 else step
    min_value = _safe_float(cfg.get("min_value"))
    min_value = step if min_value is None else min_value
    scaled = _round_to_step(baseline_f * factor_f, step)
    return float(max(min_value, scaled))


def _exercise_priority(name: str) -> str:
    text = str(name or "").lower()
    assist_tokens = (
        "fly",
        "seitheben",
        "lateral",
        "pushdown",
        "push-down",
        "curl",
        "trizeps",
        "waden",
        "calf",
        "rear delt",
        "face pull",
        "crunch",
    )
    main_tokens = (
        "bank",
        "bench",
        "ohp",
        "overhead press",
        "squat",
        "kniebeuge",
        "deadlift",
        "kreuzheben",
        "rudern",
        "row",
        "latzug",
        "pull up",
        "pullup",
    )
    if any(tok in text for tok in assist_tokens):
        return "assist"
    if any(tok in text for tok in main_tokens):
        return "main"
    return "secondary"


def _fit_rpe_list_to_sets(rpe_values: list[float], sets_target: int) -> list[float]:
    vals = [v for v in (rpe_values or []) if _safe_float(v) is not None]
    target = max(0, int(sets_target or 0))
    if target == 0 or not vals:
        return []
    if target == len(vals):
        return list(vals)

    top = max(vals)
    deltas = [top - v for v in vals]
    if target < len(vals):
        ranked = sorted(range(len(vals)), key=lambda idx: (deltas[idx], idx))
        keep = set(ranked[:target])
        return [vals[idx] for idx in range(len(vals)) if idx in keep]

    easiest = max(deltas) if deltas else 0.0
    out = list(vals)
    while len(out) < target:
        out.append(top - easiest)
    return out


def _scale_run_detail_item(item: dict[str, Any], run_factor: float) -> None:
    dur_min = _safe_float(item.get("duration_min"))
    dur_max = _safe_float(item.get("duration_max"))
    if dur_min is None:
        return

    new_min = scale_run_duration_or_reps(dur_min, run_factor, {"mode": "duration", "step": DEFAULT_RUN_STEP_MIN})
    new_max = new_min
    if dur_max is not None:
        new_max = scale_run_duration_or_reps(dur_max, run_factor, {"mode": "duration", "step": DEFAULT_RUN_STEP_MIN})
        new_max = max(new_min, new_max)

    item["duration_min"] = int(round(new_min))
    item["duration_max"] = int(round(new_max))

    text = str(item.get("text") or "")
    if text and RUN_DURATION_RE.search(text):
        if int(round(new_min)) == int(round(new_max)):
            repl = f"{int(round(new_min))} min"
        else:
            repl = f"{int(round(new_min))}-{int(round(new_max))} min"
        item["text"] = RUN_DURATION_RE.sub(repl, text, count=1)


def apply_week_rules_to_plan(
    plan_json: dict[str, Any],
    rules_json: dict[str, Any] | None,
    week_label: str,
    opts: dict[str, Any] | None = None,
    respect_periodization_enabled: bool = False,
) -> dict[str, Any]:
    plan = _ensure_plan_shape(plan_json)
    if respect_periodization_enabled and not bool((plan.get("meta") or {}).get("periodization_enabled")):
        return plan
    rules = _ensure_rules_shape(rules_json, int(plan.get("weeks") or 8))
    week_key = str(week_label or "").strip()
    week_row = rules.get(week_key) if isinstance(rules, dict) else None
    if not isinstance(week_row, dict):
        return plan

    cap = _safe_float(week_row.get("rpe_cap"))
    strength_factor = _safe_float(week_row.get("strength_factor"))
    run_factor = _safe_float(week_row.get("cardio_factor"))
    if run_factor is None:
        run_factor = _safe_float(week_row.get("run_factor"))
    strength_factor = 1.0 if strength_factor is None or strength_factor <= 0 else strength_factor
    run_factor = 1.0 if run_factor is None or run_factor <= 0 else run_factor

    cfg = opts or {}
    set_cfg = cfg.get("set_scaling") if isinstance(cfg.get("set_scaling"), dict) else {}
    rpe_cfg = cfg.get("rpe") if isinstance(cfg.get("rpe"), dict) else {}

    def _apply_to_event(event: dict[str, Any]) -> None:
            kind = str(event.get("kind") or "").lower()
            if kind == "gym":
                for item in event.get("items") or []:
                    if str(item.get("kind") or "") != "exercise":
                        continue
                    base_sets = int(item.get("sets") or 0)
                    name = str(item.get("name") or "")
                    prio = _exercise_priority(name)
                    sets_final = scale_sets(base_sets, strength_factor, prio, set_cfg)
                    item["sets"] = sets_final

                    rpe_list = item.get("rpe_list") if isinstance(item.get("rpe_list"), list) else []
                    if rpe_list and cap is not None:
                        rpe_final = apply_rpe_cap(rpe_list, cap, rpe_cfg)
                        item["rpe_list"] = _fit_rpe_list_to_sets(rpe_final, sets_final)
                    elif rpe_list and len(rpe_list) != sets_final:
                        item["rpe_list"] = _fit_rpe_list_to_sets(rpe_list, sets_final)
            elif kind in CARDIO_EVENT_KINDS:
                for item in event.get("items") or []:
                    item_kind = str(item.get("kind") or "")
                    if item_kind == "run_detail":
                        _scale_run_detail_item(item, run_factor)
                        continue
                    if item_kind != "cardio":
                        continue
                    if item.get("duration_min") not in (None, ""):
                        item["duration_min"] = scale_run_duration_or_reps(item.get("duration_min"), run_factor)
                    if item.get("duration_max") not in (None, ""):
                        item["duration_max"] = scale_run_duration_or_reps(item.get("duration_max"), run_factor)
                    if item.get("amount_value") not in (None, ""):
                        item["amount_value"] = scale_run_duration_or_reps(item.get("amount_value"), run_factor)

    for day in plan.get("days") or []:
        for event in day.get("events") or []:
            _apply_to_event(event)
    for event in plan.get("sequence") or []:
        _apply_to_event(event)

    plan["base_week"] = {d.get("day"): d.get("events") for d in (plan.get("days") or []) if isinstance(d, dict)}
    return plan


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _warning(kind: str, message: str, path: str) -> dict[str, str]:
    return {"kind": kind, "message": message, "path": path}


def default_rules_json(weeks: int = 8) -> dict[str, Any]:
    n = max(1, min(52, int(weeks or 8)))
    out: dict[str, Any] = {}
    for i in range(1, n + 1):
        label = f"W{i}"
        deload = (i % 4 == 0)
        out[label] = {
            "rpe_cap": 7 if deload else 9,
            "strength_factor": 1.0,
            "run_factor": 1.0,
            "cardio_factor": 1.0,
            "deload": deload,
            "tag": "Deload" if deload else "Build",
        }
    return out


def default_blocks_json() -> dict[str, Any]:
    return {}


def default_gym_plan_json() -> dict[str, Any]:
    return {
        "meta": {
            "title": "Neuer Gym-Plan",
            "focus": "",
            "status": "draft",
            "schema_version": 2,
            "mode": "fixed_week",
            "periodization_enabled": False,
            "rolling_week_pattern": deepcopy(DEFAULT_ROLLING_WEEK_PATTERN),
            "updated_at": None,
        },
        "weeks": 8,
        "sequence": [],
        "base_week": {d: [] for d in DAY_ORDER},
        "days": [{"day": d, "events": []} for d in DAY_ORDER],
    }


def _normalize_rolling_week_pattern(value: Any) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    out: dict[str, str] = {}
    for day in DAY_ORDER:
        raw = str(source.get(day) or DEFAULT_ROLLING_WEEK_PATTERN[day]).strip().lower()
        out[day] = raw if raw in ROLLING_PATTERN_ALLOWED else DEFAULT_ROLLING_WEEK_PATTERN[day]
    return out


def _normalize_cardio_mode(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw == "ergometer":
        raw = "ergo"
    return raw if raw in CARDIO_EVENT_KINDS else None


def _normalize_event_kind(kind: Any, mode: Any = None) -> str:
    cardio_mode = _normalize_cardio_mode(mode or kind)
    if cardio_mode:
        return "cardio" if cardio_mode == "cardio" else cardio_mode
    raw = str(kind or "").strip().lower()
    return raw if raw in ALL_EVENT_KINDS else "gym"


def _normalize_event(event: dict[str, Any] | None) -> dict[str, Any]:
    src = dict(event) if isinstance(event, dict) else {}
    kind = _normalize_event_kind(src.get("kind"), src.get("mode"))
    mode = _normalize_cardio_mode(src.get("mode") or src.get("kind"))
    # Prefer the first non-empty payload source. Some GPT/LIVA-created plans
    # persist compatibility placeholders like items: [] alongside real exercises.
    item_sources = []
    if isinstance(src.get("items"), list):
        item_sources.append(src.get("items"))
    if isinstance(src.get("exercises"), list):
        item_sources.append(src.get("exercises"))
    if isinstance(src.get("strength_exercises"), list):
        item_sources.append(src.get("strength_exercises"))
    items_src = next((candidate for candidate in item_sources if candidate), item_sources[0] if item_sources else [])
    items = []
    for item in items_src:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["id"] = row.get("id") or _new_id()
        if kind == "gym":
            row["kind"] = str(row.get("kind") or "exercise").strip().lower() or "exercise"
            rep_range = row.get("rep_range") if isinstance(row.get("rep_range"), dict) else {}
            reps = row.get("reps") if isinstance(row.get("reps"), dict) else {}
            if rep_range or not reps:
                row["reps"] = {
                    "min": int(rep_range.get("min") or reps.get("min") or 0),
                    "max": int(rep_range.get("max") or reps.get("max") or rep_range.get("min") or reps.get("min") or 0),
                }
            if row.get("variation") in (None, "") and row.get("device") not in (None, ""):
                row["variation"] = row.get("device")
        elif kind in CARDIO_EVENT_KINDS and row.get("kind") in (None, ""):
            row["kind"] = "cardio"
        items.append(row)
    return {
        "id": src.get("id") or _new_id(),
        "kind": kind,
        "mode": mode if kind in CARDIO_EVENT_KINDS else None,
        "time": src.get("time"),
        "title": str(src.get("title") or "").strip(),
        "frequency": max(1, int(src.get("frequency") or 1)),
        "items": items,
        "notes": str(src.get("notes") or src.get("note") or "").strip(),
    }


def _ensure_plan_shape(data: dict[str, Any] | None) -> dict[str, Any]:
    base = default_gym_plan_json()
    if not isinstance(data, dict):
        return base
    out = deepcopy(base)
    if isinstance(data.get("meta"), dict):
        out["meta"].update(data["meta"])
    mode = str(out["meta"].get("mode") or "fixed_week").strip().lower()
    out["meta"]["mode"] = mode if mode in PLAN_MODES else "fixed_week"
    out["meta"]["periodization_enabled"] = bool(out["meta"].get("periodization_enabled"))
    out["meta"]["rolling_week_pattern"] = _normalize_rolling_week_pattern(out["meta"].get("rolling_week_pattern"))

    try:
        weeks = int(data.get("weeks") or out["weeks"])
    except Exception:
        weeks = out["weeks"]
    out["weeks"] = max(1, min(52, weeks))

    day_map = {d["day"]: {"day": d["day"], "events": []} for d in out["days"]}

    # Prefer explicit day list when present; only fall back to base_week when it
    # actually carries event content (or no day list is available).
    days_list = data.get("days") if isinstance(data.get("days"), list) else []
    for day in days_list:
        if not isinstance(day, dict):
            continue
        day_name = day.get("day")
        if day_name in day_map and isinstance(day.get("events"), list):
            day_map[day_name]["events"] = [_normalize_event(ev) for ev in day["events"] if isinstance(ev, dict)]

    base_week = data.get("base_week") if isinstance(data.get("base_week"), dict) else {}
    base_has_events = any(isinstance(v, list) and bool(v) for v in base_week.values()) if isinstance(base_week, dict) else False
    if isinstance(base_week, dict) and (base_has_events or not days_list):
        for day in DAY_ORDER:
            events = base_week.get(day)
            if isinstance(events, list):
                day_map[day]["events"] = [_normalize_event(ev) for ev in events if isinstance(ev, dict)]
    out["days"] = [day_map[d] for d in DAY_ORDER]
    out["base_week"] = {d: day_map[d]["events"] for d in DAY_ORDER}
    sequence_src = data.get("sequence") if isinstance(data.get("sequence"), list) else []
    sequence = []
    for ev in sequence_src:
        if not isinstance(ev, dict):
            continue
        normalized = _normalize_event(ev)
        if normalized.get("kind") in {"run", "ergo", "bike", "row", "swim"}:
            normalized["mode"] = normalized["kind"]
            normalized["kind"] = "cardio"
        sequence.append(normalized)
    out["sequence"] = sequence
    return out


def _ensure_blocks_shape(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for name, entry in data.items():
        if not isinstance(name, str) or not name.strip():
            continue
        rows_src = entry if isinstance(entry, list) else (entry.get("entries") if isinstance(entry, dict) else [])
        if isinstance(entry, dict) and not isinstance(rows_src, list):
            legacy_days = entry.get("days") if isinstance(entry.get("days"), dict) else {}
            rows_src = []
            for day in DAY_ORDER:
                for r in legacy_days.get(day) or []:
                    if isinstance(r, dict):
                        rows_src.append(
                            {
                                "day": day,
                                "title": r.get("title"),
                                "frequency": r.get("frequency"),
                                "lines": r.get("lines"),
                            }
                        )
        if not isinstance(rows_src, list):
            rows_src = []
        rows = []
        for r in rows_src:
            if not isinstance(r, dict):
                continue
            day = str(r.get("day") or "").strip()
            if day not in DAY_SET:
                continue
            rows.append(
                {
                    "day": day,
                    "title": str(r.get("title") or "").strip(),
                    "frequency": int(r.get("frequency") or 1),
                    "lines": [str(x).strip() for x in (r.get("lines") or []) if str(x).strip()],
                }
            )
        out[name.strip()] = rows
    return out


def _ensure_rules_shape(data: dict[str, Any] | None, weeks: int) -> dict[str, Any]:
    defaults = default_rules_json(weeks)
    if not isinstance(data, dict):
        return defaults
    out = deepcopy(defaults)
    for label, row in data.items():
        if label not in out or not isinstance(row, dict):
            continue
        for key in ("rpe_cap", "strength_factor", "run_factor", "cardio_factor", "deload", "tag"):
            if key in row:
                out[label][key] = row[key]
        if "cardio_factor" not in row and "run_factor" in row:
            out[label]["cardio_factor"] = row["run_factor"]
        if "run_factor" not in row and "cardio_factor" in row:
            out[label]["run_factor"] = row["cardio_factor"]
        if "cardio_factor" not in out[label]:
            out[label]["cardio_factor"] = out[label].get("run_factor", 1.0)
    return out


def _auto_link_block_refs(plan: dict[str, Any], blocks_json: dict[str, Any], warnings: list[dict[str, str]]) -> None:
    for day in plan.get("days") or []:
        if not isinstance(day, dict):
            continue
        day_name = str(day.get("day") or "")
        if day_name not in DAY_SET:
            continue
        for event in day.get("events") or []:
            if not isinstance(event, dict) or str(event.get("kind") or "").lower() != "gym":
                continue
            items = event.get("items") or []
            for idx, item in enumerate(items):
                if not isinstance(item, dict) or item.get("kind") != "exercise":
                    continue
                ex_name = str(item.get("name") or "").strip()
                if not ex_name:
                    continue
                ex_var = str(item.get("variation") or "").strip()
                ex_title = f"{ex_name} ({ex_var})" if ex_var else ex_name
                rep = item.get("reps") if isinstance(item.get("reps"), dict) else {}
                rmin = int(rep.get("min") or 0)
                rmax = int(rep.get("max") or 0)
                sets = int(item.get("sets") or 0)
                scheme_a = f"{sets}x{rmin}-{rmax}" if sets > 0 and rmin > 0 and rmax > 0 and rmax != rmin else ""
                scheme_b = f"{sets}x{rmin}" if sets > 0 and rmin > 0 else ""
                candidates: list[tuple[int, str, dict[str, Any]]] = []
                for block_name, rows in (blocks_json or {}).items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if str(row.get("day") or "") != day_name:
                            continue
                        title = str(row.get("title") or "").strip().lower()
                        if ex_name.lower() not in title:
                            continue
                        if ex_var and ex_var.lower() not in title:
                            continue
                        score = 1
                        lines = [str(x).lower() for x in (row.get("lines") or [])]
                        if scheme_a and any(scheme_a.lower() in l for l in lines):
                            score += 2
                        elif scheme_b and any(scheme_b.lower() in l for l in lines):
                            score += 1
                        if ex_title.lower() in title:
                            score += 1
                        candidates.append((score, str(block_name), row))
                if not candidates:
                    continue
                candidates.sort(key=lambda x: x[0], reverse=True)
                top = candidates[0]
                if top[0] <= 0:
                    continue
                if len(candidates) > 1 and candidates[0][0] == candidates[1][0] and candidates[0][1] != candidates[1][1]:
                    warnings.append(
                        _warning(
                            "ambiguous_block_ref",
                            f"Block-Referenz für {ex_title} ist mehrdeutig und wurde nicht auto-verlinkt.",
                            f"day:{day_name}",
                        )
                    )
                    continue
                items[idx] = {
                    "id": item.get("id") or _new_id(),
                    "kind": "ref_block",
                    "display_name": ex_name,
                    "variation": ex_var,
                    "block_name": top[1],
                    "block_day": day_name,
                }
                warnings.append(
                    _warning(
                        "auto_block_ref",
                        f"{ex_title} wurde automatisch mit Block \"{top[1]}\" verknüpft.",
                        f"day:{day_name}",
                    )
                )


def parse_gym_plan_dsl(text: str, *, known_exercises: set[str] | None = None) -> dict[str, Any]:
    known = {k.strip().lower() for k in (known_exercises or set()) if str(k).strip()}
    warnings: list[dict[str, str]] = []
    plan = default_gym_plan_json()
    day_lookup = {d["day"]: d for d in plan["days"]}
    blocks_json: dict[str, Any] = {}
    parsed_rules: dict[str, Any] = {}

    current_day: dict[str, Any] | None = None
    current_event: dict[str, Any] | None = None
    current_block_name: str | None = None
    current_block_day: str | None = None
    current_block_entry: dict[str, Any] | None = None
    in_rotation_section = False
    in_rules_section = False
    rules_header_weeks: int | None = None
    max_rule_week = 0

    for idx, raw in enumerate((text or "").splitlines(), start=1):
        line = (raw or "").rstrip()
        if not line.strip():
            continue

        m_title = TITLE_HEADER_RE.match(line)
        if m_title and not line.startswith("##"):
            title_text = m_title.group(1).strip()
            if title_text:
                plan["meta"]["title"] = title_text
            continue

        m_day = DAY_HEADER_RE.match(line)
        if m_day:
            current_day = day_lookup[m_day.group(1)]
            current_event = None
            current_block_name = None
            current_block_day = None
            current_block_entry = None
            in_rotation_section = False
            in_rules_section = False
            continue

        if ROLLING_SECTION_RE.match(line):
            current_day = None
            current_event = None
            current_block_name = None
            current_block_day = None
            current_block_entry = None
            in_rotation_section = True
            in_rules_section = False
            plan["meta"]["mode"] = "rolling_sequence"
            continue

        if RULES_HEADER_RE.match(line):
            current_day = None
            current_event = None
            current_block_name = None
            current_block_day = None
            current_block_entry = None
            in_rotation_section = False
            in_rules_section = True
            hinted = _extract_weeks_hint_from_rules_header(line)
            if hinted is not None:
                rules_header_weeks = hinted
            continue

        if WEEK_LOGIC_HEADER_RE.match(line):
            current_day = None
            current_event = None
            current_block_name = None
            current_block_day = None
            current_block_entry = None
            in_rotation_section = False
            in_rules_section = False
            continue

        m_block = BLOCK_HEADER_RE.match(line)
        if m_block and m_block.group(1) not in DAY_SET:
            block_name = m_block.group(1).strip()
            duration = int(m_block.group(2)) if m_block.group(2) else 8
            if m_block.group(2):
                block_name = f"{block_name} ({duration} Wochen)"
            blocks_json[block_name] = []
            current_block_name = block_name
            current_block_day = None
            current_block_entry = None
            current_day = None
            current_event = None
            in_rotation_section = False
            in_rules_section = False
            plan["weeks"] = max(plan["weeks"], duration)
            continue

        if in_rules_section:
            m_rule = RULE_ROW_RE.match(line)
            if m_rule:
                week_num = int(m_rule.group("week"))
                week_label = f"W{week_num}"
                parsed = _parse_rule_row(m_rule.group("body"))
                if parsed:
                    parsed_rules[week_label] = parsed
                max_rule_week = max(max_rule_week, week_num)
                continue
            warnings.append(_warning("unparsed_rule_line", "Wochenregel-Zeile konnte nicht geparst werden.", f"line:{idx}"))
            continue

        if current_block_name:
            m_block_day = BLOCK_DAY_RE.match(line)
            if m_block_day:
                day = m_block_day.group(1)
                title = m_block_day.group(2).strip()
                freq = int(m_block_day.group(3))
                current_block_day = day
                current_block_entry = {"day": day, "title": title, "frequency": freq, "lines": []}
                blocks_json[current_block_name].append(current_block_entry)
                continue
            m_line = RUN_ITEM_RE.match(line)
            if m_line and current_block_entry is not None:
                current_block_entry["lines"].append(m_line.group("text").strip())
                continue
            warnings.append(_warning("unparsed_block_line", "Block-Zeile konnte nicht geparst werden.", f"line:{idx}"))
            continue

        m_rotation_event = ROLLING_EVENT_RE.match(line) if in_rotation_section else None
        if m_rotation_event:
            raw_kind = str(m_rotation_event.group("kind") or "").strip()
            raw_mode = str(m_rotation_event.group("mode") or "").strip()
            freq = int(m_rotation_event.group("freq") or 1)
            title = str(m_rotation_event.group("title") or "").strip()
            raw_kind_l = raw_kind.lower()
            raw_mode_l = raw_mode.lower() if raw_mode else None
            if raw_kind_l.startswith("cardio"):
                kind = "cardio"
                mode = raw_mode_l or "cardio"
            elif raw_kind_l in {"run", "ergo", "bike", "row", "swim"}:
                kind = "cardio"
                mode = raw_kind_l
            else:
                kind = raw_kind_l
                mode = raw_mode_l
            current_event = _normalize_event(
                {
                    "kind": kind,
                    "mode": mode,
                    "title": title if title else ("Rest" if kind == "rest" else ""),
                    "time": None,
                    "frequency": freq,
                    "items": [],
                }
            )
            plan["sequence"].append(current_event)
            continue

        m_event = EVENT_HEADER_RE.match(line)
        if m_event:
            if current_day is None:
                warnings.append(_warning("event_without_day", "Event ohne Day-Header ignoriert.", f"line:{idx}"))
                continue
            kind = (m_event.group("kind") or "").strip().lower()
            time_raw = (m_event.group("time") or "").strip()
            time_text = None if time_raw in {"", EM_DASH, "-", "--"} else time_raw
            title = (m_event.group("title") or "").strip()
            if kind == "rest" and not title:
                title = "Rest"
            current_event = {
                "id": _new_id(),
                "kind": kind,
                "time": time_text,
                "title": title,
                "frequency": int(m_event.group("freq")),
                "items": [],
            }
            current_day["events"].append(current_event)
            continue

        if current_event is None:
            warnings.append(_warning("line_without_context", "Zeile ohne Event-Kontext ignoriert.", f"line:{idx}"))
            continue

        if current_event["kind"] == "gym":
            m_ref = REF_BLOCK_RE.match(line)
            if m_ref:
                display_raw = m_ref.group("display").strip()
                variation = ""
                display_name = display_raw
                vm = VARIATION_RE.match(display_raw)
                if vm:
                    display_name = vm.group("name").strip()
                    variation = vm.group("variation").strip()
                current_event["items"].append(
                    {
                        "id": _new_id(),
                        "kind": "ref_block",
                        "display_name": display_name,
                        "variation": variation,
                        "block_name": m_ref.group("block_name").strip(),
                        "block_day": current_day.get("day") if isinstance(current_day, dict) else "",
                    }
                )
                continue

            m_ex = EXERCISE_ITEM_RE.match(line)
            if not m_ex:
                warnings.append(_warning("invalid_exercise_item", "Gym-Item hat ungültiges Format.", f"line:{idx}"))
                continue

            ex_name_raw = m_ex.group("name").strip()
            variation = ""
            vm = VARIATION_RE.match(ex_name_raw)
            if vm:
                ex_name = _canonicalize_exercise_name(vm.group("name").strip())
                variation = vm.group("variation").strip()
            else:
                ex_name = _canonicalize_exercise_name(ex_name_raw)

            sets = int(m_ex.group("sets"))
            rep_min = int(m_ex.group("repmin"))
            rep_max = int(m_ex.group("repmax") or rep_min)
            raw_rpe = m_ex.group("rpe") or ""
            rpe_list: list[float] = []
            if raw_rpe.strip():
                for part in [p.strip() for p in raw_rpe.split("/") if p.strip()]:
                    try:
                        rpe_list.append(float(part))
                    except Exception:
                        warnings.append(_warning("invalid_rpe_value", f"Ungültiger RPE-Wert: {part}", f"line:{idx}"))
            if rpe_list and len(rpe_list) == 1 and sets > 1:
                rpe_list = rpe_list * sets
            elif rpe_list and len(rpe_list) != sets:
                warnings.append(_warning("rpe_mismatch", f"RPE count {len(rpe_list)} passt nicht zu Sets {sets}.", f"line:{idx}"))
            if known and ex_name.lower() not in known:
                warnings.append(_warning("unknown_exercise", f"Unbekannte Übung: {ex_name}", f"line:{idx}"))

            current_event["items"].append(
                {
                    "id": _new_id(),
                    "kind": "exercise",
                    "name": ex_name,
                    "variation": variation,
                    "sets": sets,
                    "reps": {"min": rep_min, "max": rep_max},
                    "rpe_list": rpe_list,
                    "note": (m_ex.group("note") or "").strip(),
                }
            )
            continue

        if current_event["kind"] in CARDIO_EVENT_KINDS:
            m_run_ref = RUN_REF_BLOCK_RE.match(line)
            if m_run_ref:
                current_event["items"].append(
                    {
                        "id": _new_id(),
                        "kind": "ref_block",
                        "display_name": "",
                        "variation": "",
                        "block_name": m_run_ref.group("block_name").strip(),
                        "block_day": current_day.get("day") if isinstance(current_day, dict) else "",
                    }
                )
                continue
            m = RUN_ITEM_RE.match(line)
            if not m:
                warnings.append(_warning("invalid_run_item", "Run-Item konnte nicht geparst werden.", f"line:{idx}"))
                continue
            text_line = m.group("text").strip()
            pulse = RUN_PULSE_RE.search(text_line)
            if pulse:
                dur_min = int(pulse.group("dur_min"))
                dur_max_raw = pulse.group("dur_max")
                dur_max = int(dur_max_raw) if dur_max_raw else dur_min
                current_event["items"].append(
                    {
                        "id": _new_id(),
                        "kind": "run_detail",
                        "text": text_line,
                        "duration_min": dur_min,
                        "duration_max": dur_max,
                        "hr_low": int(pulse.group("low")),
                        "hr_high": int(pulse.group("high")),
                        "hr_unit": (pulse.group("unit") or "").replace(" ", "") or None,
                        "note": (pulse.group("note") or "").strip(),
                    }
                )
            else:
                duration = RUN_DURATION_RE.search(text_line)
                if duration:
                    dur_min = int(duration.group(1))
                    dur_max = int(duration.group(2) or dur_min)
                    zone_match = re.search(r"@([A-Za-z0-9]+)", text_line)
                    current_event["items"].append(
                        {
                            "id": _new_id(),
                            "kind": "cardio",
                            "mode": _normalize_cardio_mode(current_event.get("mode") or current_event.get("kind")) or "cardio",
                            "name": str(current_event.get("title") or "Cardio").strip() or "Cardio",
                            "duration_min": dur_min,
                            "duration_max": dur_max,
                            "amount_value": dur_min,
                            "amount_unit": "min",
                            "intensity": zone_match.group(1) if zone_match else "",
                            "display": text_line,
                            "display_text": text_line,
                        }
                    )
                else:
                    current_event["items"].append({"id": _new_id(), "kind": "run_detail", "text": text_line})
            continue

        if current_event["kind"] == "rest":
            m = RUN_ITEM_RE.match(line)
            if m:
                current_event["items"].append({"id": _new_id(), "kind": "note", "text": m.group("text").strip()})
            else:
                warnings.append(_warning("invalid_rest_item", "Rest-Item konnte nicht geparst werden.", f"line:{idx}"))
            continue

        if current_event["kind"] == "note":
            m = RUN_ITEM_RE.match(line)
            if m:
                current_event["items"].append({"id": _new_id(), "kind": "note", "text": m.group("text").strip()})
            else:
                current_event["notes"] = line.strip()

    # If week rules are present, they are source-of-truth for cycle length.
    if rules_header_weeks is not None or max_rule_week > 0:
        rule_weeks = max(max_rule_week, rules_header_weeks or 0)
        plan["weeks"] = max(1, min(52, int(rule_weeks)))

    plan = _ensure_plan_shape(plan)
    blocks_json = _ensure_blocks_shape(blocks_json)
    rules_json = _ensure_rules_shape(parsed_rules, int(plan.get("weeks") or 8))
    _auto_link_block_refs(plan, blocks_json, warnings)
    return {"plan_json": plan, "blocks_json": blocks_json, "rules_json": rules_json, "warnings": warnings}


def _fmt_rpe(values: list[Any]) -> str:
    out = []
    for v in values or []:
        try:
            n = float(v)
            out.append(str(int(n)) if n.is_integer() else str(n).rstrip("0").rstrip("."))
        except Exception:
            continue
    return "/".join(out)


def _parse_rule_bool(value: str) -> bool | None:
    v = str(value or "").strip().lower()
    if v in {"1", "true", "yes", "ja"}:
        return True
    if v in {"0", "false", "no", "nein"}:
        return False
    return None


def _parse_rule_row(body: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for chunk in str(body or "").split(";"):
        token = chunk.strip()
        if not token or "=" not in token:
            continue
        key_raw, val_raw = token.split("=", 1)
        key = key_raw.strip().lower()
        val = val_raw.strip()
        if not key:
            continue
        if key in {"phase", "tag"}:
            out["tag"] = val
            continue
        if key in {"rpe", "rpe_cap"}:
            try:
                out["rpe_cap"] = float(val)
            except Exception:
                pass
            continue
        if key in {"strength", "strength_factor"}:
            try:
                out["strength_factor"] = float(val)
            except Exception:
                pass
            continue
        if key in {"run", "run_factor"}:
            try:
                out["run_factor"] = float(val)
                out["cardio_factor"] = float(val)
            except Exception:
                pass
            continue
        if key in {"cardio", "cardio_factor"}:
            try:
                out["cardio_factor"] = float(val)
            except Exception:
                pass
            continue
        if key == "deload":
            parsed = _parse_rule_bool(val)
            if parsed is not None:
                out["deload"] = parsed
            continue
    return out


def _fmt_num(value: Any) -> str:
    try:
        n = float(value)
    except Exception:
        return str(value)
    if n.is_integer():
        return str(int(n))
    return str(n).rstrip("0").rstrip(".")


def format_cardio_item_for_export(item: dict[str, Any]) -> str:
    duration = item.get("duration_min") or item.get("amount_value")
    amount_unit = str(item.get("amount_unit") or "min").strip() or "min"
    intensity = str(item.get("intensity") or item.get("zone") or item.get("target_hr") or "").strip()
    distance = str(item.get("distance") or item.get("distance_km") or "").strip()
    intervals = str(item.get("intervals") or "").strip()
    notes = str(item.get("notes") or item.get("note") or "").strip()
    bits: list[str] = []
    if duration not in (None, ""):
        bits.append(f"{_fmt_num(duration)} {amount_unit}")
    if intensity:
        bits.append(f"@{intensity}")
    if distance:
        bits.append(distance)
    if intervals:
        bits.append(intervals)
    if notes:
        bits.append(notes)
    return " ".join(bits).strip() or str(item.get("display") or item.get("display_text") or item.get("name") or "Cardio").strip()


def export_gym_plan_dsl(
    plan_json: dict[str, Any],
    blocks_json: dict[str, Any] | None = None,
    rules_json: dict[str, Any] | None = None,
    *,
    week_label: str | None = None,
    apply_week_rules: bool = False,
) -> str:
    plan = _ensure_plan_shape(plan_json)
    blocks = _ensure_blocks_shape(blocks_json)
    rules = _ensure_rules_shape(rules_json, int(plan.get("weeks") or 8))
    if apply_week_rules and isinstance(week_label, str) and week_label.strip():
        plan = apply_week_rules_to_plan(plan, rules, week_label.strip())

    lines: list[str] = []
    if str((plan.get("meta") or {}).get("mode") or "fixed_week") == "rolling_sequence":
        lines.append("## Rotation")
        event_groups = [("sequence", plan.get("sequence") or [])]
    else:
        event_groups = [(day.get("day"), day.get("events") or []) for day in (plan.get("days") or []) if day.get("day") in DAY_SET]

    for group_name, group_events in event_groups:
        if group_name != "sequence":
            lines.append(f"## {group_name}")
        for event in group_events:
            kind = str(event.get("kind") or "").lower()
            if kind not in ALL_EVENT_KINDS:
                continue
            freq = int(event.get("frequency") or 1)
            title = (event.get("title") or "").strip()
            if group_name == "sequence":
                if kind == "cardio":
                    mode = (_normalize_cardio_mode(event.get("mode")) or "cardio").capitalize()
                    lines.append(f"- [Cardio: {mode}] {title} ({freq}x)")
                elif kind == "rest":
                    lines.append(f"- [Rest] {title or 'Rest'} ({freq}x)")
                elif kind == "note":
                    lines.append(f"- [Note] {title or 'Notiz'} ({freq}x)")
                else:
                    lines.append(f"- [{kind.capitalize()}] {title} ({freq}x)")
            else:
                t = event.get("time") or EM_DASH
                if kind == "rest":
                    lines.append(f"- [{t}] Rest ({freq}x)")
                else:
                    kind_label = kind.capitalize()
                    lines.append(f"- [{t}] {kind_label} \u2013 {title} ({freq}x)")

            for item in event.get("items") or []:
                ik = str(item.get("kind") or "")
                if kind == "gym" and ik == "exercise":
                    name = (item.get("name") or "").strip()
                    variation = (item.get("variation") or "").strip()
                    display = f"{name} ({variation})" if variation else name
                    sets = int(item.get("sets") or 0)
                    reps = item.get("reps") if isinstance(item.get("reps"), dict) else {}
                    rmin = int(reps.get("min") or 0)
                    rmax = int(reps.get("max") or rmin)
                    rep_text = f"{rmin}-{rmax}" if rmax != rmin else str(rmin)
                    rpe = _fmt_rpe(item.get("rpe_list") or [])
                    note = (item.get("note") or "").strip()
                    rpe_part = f" @RPE {rpe}" if rpe else ""
                    tail = f" {note}" if note else ""
                    lines.append(f"  - {display}: {sets}x{rep_text}{rpe_part}{tail}".rstrip())
                elif kind == "gym" and ik == "ref_block":
                    display_name = (item.get("display_name") or "Block").strip()
                    variation = (item.get("variation") or "").strip()
                    display = f"{display_name} ({variation})" if variation else display_name
                    block_name = (item.get("block_name") or "").strip()
                    lines.append(f'  - {display}: siehe "{block_name}" unten')
                elif kind in CARDIO_EVENT_KINDS and ik == "run_detail":
                    lines.append(f"  - {(item.get('text') or '').strip()}")
                elif kind in CARDIO_EVENT_KINDS and ik == "ref_block":
                    block_name = (item.get("block_name") or "").strip()
                    lines.append(f'  - siehe "{block_name}" unten')
                elif kind in CARDIO_EVENT_KINDS and ik == "cardio":
                    lines.append(f"  - {format_cardio_item_for_export(item)}")
                elif kind == "rest" and ik == "note":
                    lines.append(f"  - {(item.get('text') or '').strip()}")
                elif kind == "note" and ik == "note":
                    lines.append(f"  - {(item.get('text') or '').strip()}")
        lines.append("")

    for block_name, rows in blocks.items():
        lines.append(f"## {block_name}")
        for row in rows:
            day = str(row.get("day") or "").strip()
            if day not in DAY_SET:
                continue
            title = str(row.get("title") or "").strip()
            freq = int(row.get("frequency") or 1)
            lines.append(f"- [{day}] {title} ({freq}x)")
            for text_line in row.get("lines") or []:
                lines.append(f"  - {str(text_line).strip()}")
        lines.append("")

    lines.append("## Wochenregeln")
    week_labels = sorted(
        [k for k in rules.keys() if isinstance(k, str) and re.match(r"^W\d+$", k)],
        key=lambda k: int(k[1:]),
    )
    for label in week_labels:
        row = rules.get(label) if isinstance(rules.get(label), dict) else {}
        tag = str(row.get("tag") or "Build")
        rpe_cap = _fmt_num(row.get("rpe_cap"))
        strength_factor = _fmt_num(row.get("strength_factor"))
        cardio_factor = _fmt_num(row.get("cardio_factor", row.get("run_factor")))
        deload = "true" if bool(row.get("deload")) else "false"
        lines.append(
            f"- {label}: phase={tag}; rpe_cap={rpe_cap}; strength_factor={strength_factor}; run_factor={cardio_factor}; deload={deload}"
        )
    lines.append("")

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def validate_plan_against_rules(
    plan_json: dict[str, Any],
    rules_json: dict[str, Any] | None,
    week_label: str,
) -> list[dict[str, str]]:
    plan = _ensure_plan_shape(plan_json)
    rules = _ensure_rules_shape(rules_json, int(plan.get("weeks") or 8))
    rule = rules.get(week_label) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return []

    cap = rule.get("rpe_cap")
    try:
        cap_f = float(cap)
    except Exception:
        return []

    warnings: list[dict[str, str]] = []
    for day in plan.get("days") or []:
        day_name = day.get("day")
        for event in day.get("events") or []:
            if str(event.get("kind") or "").lower() != "gym":
                continue
            for item in event.get("items") or []:
                if str(item.get("kind") or "") != "exercise":
                    continue
                vals = item.get("rpe_list") if isinstance(item.get("rpe_list"), list) else []
                if not vals:
                    continue
                try:
                    max_rpe = max(float(v) for v in vals)
                except Exception:
                    continue
                adjusted = apply_rpe_cap(vals, cap_f)
                try:
                    max_adjusted = max(float(v) for v in adjusted) if adjusted else None
                except Exception:
                    max_adjusted = None
                if max_adjusted is not None and max_adjusted > cap_f:
                    warnings.append(
                        _warning(
                            "rpe_over_cap",
                            f"{item.get('name')}: RPE {max_rpe:g} > Cap {cap_f:g} ({week_label})",
                            f"day:{day_name}",
                        )
                    )
    return warnings
