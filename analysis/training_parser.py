import re
from statistics import quantiles
from typing import Any, Dict, List, Optional, Tuple

import sqlite3
from datetime import datetime

from analysis.training_analysis import get_exercise_history
from plans.plans_api import _normalize_plan, _safe_load_plan, get_plans_db

DEFAULT_TOP_THRESHOLD_PCT = 0.05
DEFAULT_TOP_THRESHOLD_ABS = 5.0
LOAD_TOLERANCE_PCT = 0.08
LOAD_TOLERANCE_ABS = 5.0
HISTORY_LIMIT = 12

SET_PATTERN = re.compile(
    r"(?P<first>[\d.,]+)\s*[x×]\s*(?P<second>[+-]?[\d.,]+)(?:\s*[@;]\s*(?P<rpe>[\d.,]+))?",
    re.IGNORECASE,
)
COMMA_RPE_PATTERN = re.compile(
    r"(?P<first>\d+(?:[.,]\d+)?)\s*[x×]\s*(?P<weight>\d{2,3})(?P<sep>[,.])(?P<rpe>10|[6-9])\b(?!\s*[@;])",
    re.IGNORECASE,
)
OCR_WEIGHT_RPE_TOKEN_PATTERN = re.compile(
    r"^(?P<weight>\d+)[.,](?P<rpe>10|[6-9])$",
    re.IGNORECASE,
)
HEADER_WITH_VARIATION_PATTERN = re.compile(
    r"^(?P<name>.+?)\s*\((?P<variation>[^()]+)\)\s*$"
)
SESSION_HEADER_PATTERN = re.compile(
    r"^(?P<session>.+?)\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*$"
)


def _norm_text(val: Optional[str]) -> str:
    if not val:
        return ""
    return " ".join(str(val).strip().lower().split())


def _parse_number(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = str(value).replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_set_notation(line: str) -> str:
    # Handwritten logs often use "9x72,8" to mean 9 reps, 72 kg, RPE 8.
    # OCR sometimes turns the comma into a dot ("9x72.8"). Decimal loads
    # like "72,5@8" stay untouched because they already have @.
    return COMMA_RPE_PATTERN.sub(r"\g<first>x\g<weight>@\g<rpe>", line or "")


def _split_ocr_weight_rpe_token(raw_value: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    text = (raw_value or "").strip()
    if not text:
        return None, None
    match = OCR_WEIGHT_RPE_TOKEN_PATTERN.match(text)
    if not match:
        return None, None
    weight = _parse_number(match.group("weight"))
    rpe = _parse_number(match.group("rpe"))
    if weight is None or rpe is None:
        return None, None
    return weight, rpe


def _is_note_line(text: str) -> bool:
    stripped = text.strip()
    return bool(
        stripped.startswith("#")
        or stripped.startswith('"')
        or (
            stripped.startswith("(")
            and stripped.endswith(")")
            and len(stripped) > 2
        )
    )


def _strip_note_markers(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("#"):
        return stripped[1:].strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        return stripped[1:-1].strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return stripped[1:-1].strip()
    return stripped


def _parse_set_tokens(line: str) -> List[Dict[str, Any]]:
    line = _normalize_set_notation(line)
    tokens = []
    for match in SET_PATTERN.finditer(line):
        raw_first = match.group("first")
        raw_second = match.group("second")
        first = _parse_number(match.group("first"))
        second = _parse_number(match.group("second"))
        rpe = _parse_number(match.group("rpe"))

        if first is None or second is None:
            continue

        # OCR often collapses "125 kg @ 9" into "125.9". If no explicit RPE is
        # present, recover that notation here without touching true decimal
        # loads like 17.5 or 72.5.
        if rpe is None and first <= 30:
            split_weight, split_rpe = _split_ocr_weight_rpe_token(raw_second)
            if split_weight is not None and split_rpe is not None:
                second = split_weight
                rpe = split_rpe

        # Reps always come before x, weight always after x.
        reps = first
        weight = second

        if reps is None or weight is None:
            continue
        if abs(float(reps) - round(float(reps))) > 1e-9:
            continue

        tokens.append(
            {
                "reps": int(round(reps)),
                "weight": weight,
                "rpe": float(rpe) if rpe is not None else None,
                "raw": match.group(0).strip(),
            }
        )
    return tokens


def _append_tokens_if_new(current_ex: Dict[str, Any], tokens: List[Dict[str, Any]]) -> bool:
    existing = current_ex.setdefault("sets", [])
    for token in tokens:
        existing.append(
            {
                "reps": token["reps"],
                "weight": token["weight"],
                "rpe": token["rpe"],
                "raw_token": token["raw"],
            }
        )
    return True


def _line_is_set_line(line: str) -> bool:
    line = _normalize_set_notation(line)
    cleaned = re.sub(r"\bkg\b", "", line, flags=re.IGNORECASE)
    cleaned = SET_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[;,.\s]+", "", cleaned)
    return not bool(re.search(r"[A-Za-zÄäÖöÜüß]", cleaned))


def _split_header_name_variation(line: str) -> Tuple[str, str]:
    text = (line or "").strip().rstrip(":：").strip()
    if not text:
        return "", ""
    match = HEADER_WITH_VARIATION_PATTERN.match(text)
    if not match:
        return text, ""
    name = (match.group("name") or "").strip()
    variation = (match.group("variation") or "").strip()
    if not name or not variation:
        return text, ""
    return name, variation


def _parse_session_header_date(raw_date: str) -> Optional[str]:
    text = (raw_date or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_session_header(raw_text: str) -> Tuple[str, Optional[Dict[str, str]]]:
    lines = (raw_text or "").splitlines()
    first_content_idx = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_content_idx = idx
            break
    if first_content_idx is None:
        return raw_text, None

    header_line = lines[first_content_idx].strip()
    match = SESSION_HEADER_PATTERN.match(header_line)
    if not match:
        return raw_text, None

    session_name = (match.group("session") or "").strip()
    date_iso = _parse_session_header_date(match.group("date") or "")
    if not session_name or not date_iso:
        return raw_text, None

    body_lines = lines[:first_content_idx] + lines[first_content_idx + 1 :]
    body_text = "\n".join(body_lines)
    return body_text, {"session_name": session_name, "date_iso": date_iso}


def parse_training_text(raw_text: str) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    exercises: List[Dict[str, Any]] = []
    session_notes: List[str] = []
    warnings: List[str] = []
    current_ex = None
    for idx, raw_line in enumerate(raw_text.splitlines()):
        line = raw_line.strip()
        if not line:
            current_ex = None
            continue

        if _is_note_line(line):
            note = _strip_note_markers(line)
            if current_ex:
                existing = current_ex.get("notes") or ""
                current_ex["notes"] = "\n".join(
                    [existing, note] if existing else [note]
                )
            else:
                session_notes.append(note)
            continue

        colon_header = re.match(r"^(?P<header>[^:：]{2,60})\s*[:：]\s*(?P<body>.*)$", line)
        if colon_header and not _parse_set_tokens(colon_header.group("header")):
            ex_name, ex_variation = _split_header_name_variation(colon_header.group("header").strip())
            current_ex = {
                "name": ex_name,
                "variation": ex_variation,
                "notes": "",
                "sets": [],
            }
            exercises.append(current_ex)
            body_part = colon_header.group("body").strip()
            tokens = _parse_set_tokens(body_part)
            if tokens:
                _append_tokens_if_new(current_ex, tokens)
                if not _line_is_set_line(body_part):
                    leftover = SET_PATTERN.sub("", _normalize_set_notation(body_part))
                    leftover = re.sub(r"[\d\s.,;:-]+", "", leftover).strip()
                    if leftover:
                        warnings.append(f"Line {idx+1}: Unbekannte Tokens in '{line}'")
            else:
                current_ex["notes"] = body_part
            continue

        tokens = _parse_set_tokens(line)
        if tokens and not _line_is_set_line(line):
            warnings.append(f"Line {idx+1}: Unbekannte Tokens in '{line}'")
        if _line_is_set_line(line) and tokens:
            if current_ex is None:
                current_ex = {
                    "name": "Unbekannte Übung",
                    "variation": "",
                    "notes": "",
                    "sets": [],
                }
                exercises.append(current_ex)
                warnings.append(
                    f"Line {idx+1}: Satz ohne Übungsüberschrift wurde angenommen."
                )

            _append_tokens_if_new(current_ex, tokens)
            continue

        # Header line (may still contain tokens)
        ex_name, ex_variation = _split_header_name_variation(line)
        current_ex = {
            "name": ex_name,
            "variation": ex_variation,
            "notes": "",
            "sets": [],
        }
        exercises.append(current_ex)

        if tokens:
            _append_tokens_if_new(current_ex, tokens)
        elif not line:
            warnings.append(f"Line {idx+1}: Leere Übungszeile wurde ignoriert.")

    session_notes_str = "\n".join(session_notes).strip()
    return exercises, session_notes_str, warnings


def normalized_training_text(exercises: List[Dict[str, Any]]) -> str:
    """Render the canonical structured workout as deterministic parser text."""

    blocks: List[str] = []
    for exercise in exercises or []:
        if not isinstance(exercise, dict):
            continue
        name = str(exercise.get("name") or exercise.get("exercise_name") or "").strip()
        if not name:
            continue
        variation = str(exercise.get("variation") or "").strip()
        header = f"{name} ({variation})" if variation else name
        set_tokens: List[str] = []
        for logged_set in exercise.get("sets") or []:
            if not isinstance(logged_set, dict):
                continue
            reps = logged_set.get("reps")
            weight = logged_set.get("weight")
            if reps in (None, "") or weight in (None, ""):
                continue
            try:
                reps_text = str(int(float(reps)))
                weight_num = float(weight)
                weight_text = f"{weight_num:g}".replace(".", ",")
            except (TypeError, ValueError):
                continue
            token = f"{reps_text}x{weight_text}"
            rpe = logged_set.get("rpe")
            if rpe not in (None, ""):
                try:
                    token += f"@{float(rpe):g}".replace(".", ",")
                except (TypeError, ValueError):
                    pass
            set_tokens.append(token)
        notes = str(exercise.get("notes") or "").strip()
        lines = [header]
        if set_tokens:
            lines.append(" ".join(set_tokens))
        if notes:
            lines.append(f'"{notes}"')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def classify_top_backoff(
    sets: List[Dict[str, Any]],
    pct_threshold: float,
    abs_threshold: float,
) -> List[Dict[str, Any]]:
    weights = [s.get("weight") for s in sets if isinstance(s.get("weight"), (int, float))]
    if not weights:
        for s in sets:
            s["is_top"] = False
            s["is_backoff"] = False
        return sets

    top_weight = max(weights)
    threshold = max(top_weight * (1 - pct_threshold), top_weight - abs_threshold)

    if len(set(weights)) == 1:
        for s in sets:
            s["is_top"] = True
            s["is_backoff"] = False
        return sets

    for s in sets:
        weight = s.get("weight")
        if weight is None:
            s["is_top"] = False
            s["is_backoff"] = False
            continue
        if weight >= threshold:
            s["is_top"] = True
            s["is_backoff"] = False
        else:
            s["is_top"] = False
            s["is_backoff"] = True
    return sets


def _format_history_entry(row: Dict[str, Any]) -> Optional[str]:
    if not row:
        return None
    parts = []
    reps = row.get("reps")
    weight = row.get("weight")
    rpe = row.get("rpe")
    if reps is not None:
        parts.append(f"{int(reps)}x")
    else:
        parts.append("x")
    if weight is not None:
        parts.append(f"{float(weight):g}")
    else:
        parts.append("kg")

    s = "".join(parts)
    if rpe is not None:
        s += f"@{rpe:g}"
    return s


def _summarize_history(
    history: List[Dict[str, Any]]
) -> Dict[str, Any]:
    weights = [row.get("weight") for row in history if isinstance(row.get("weight"), (int, float))]
    stats: Dict[str, Any] = {"sample_count": len(weights)}
    if not weights:
        return stats
    stats["min"] = min(weights)
    stats["max"] = max(weights)
    try:
        qs = quantiles(weights, n=10)
        stats["p10"] = qs[0]
        stats["p90"] = qs[-2]
    except ValueError:
        stats["p10"] = stats["min"]
        stats["p90"] = stats["max"]
    last = history[-1] if history else None
    stats["last_exposure"] = _format_history_entry(last)
    return stats


def _normalize_structured_exercises(raw_exercises: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for raw in raw_exercises or []:
        name = (raw.get("name") or raw.get("exercise_name") or "").strip()
        variation = (raw.get("variation") or "").strip()
        notes = (
            (raw.get("notes") or raw.get("exercise_notes") or raw.get("note") or "")
            .strip()
        )
        sets_list: List[Dict[str, Any]] = []
        for set_entry in raw.get("sets") or []:
            reps = _parse_number(set_entry.get("reps"))
            weight = _parse_number(set_entry.get("weight"))
            rpe = _parse_number(set_entry.get("rpe"))
            if reps is not None:
                reps = int(round(reps))
            sets_list.append(
                {
                    "reps": reps,
                    "weight": weight,
                    "rpe": rpe,
                    "set_number": set_entry.get("set_number") or set_entry.get("order_index"),
                    "raw_token": set_entry.get("raw_token") or set_entry.get("token") or "",
                }
            )
        normalized.append(
            {
                "name": name,
                "variation": variation,
                "notes": notes,
                "sets": sets_list,
                "meta": raw.get("meta", {}) or {},
            }
        )
    return normalized


def _serialize_exercises_to_text(exercises: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for ex in exercises:
        if not ex.get("name"):
            continue
        lines.append(ex["name"])
        tokens: List[str] = []
        for set_entry in ex.get("sets") or []:
            reps = set_entry.get("reps")
            weight = set_entry.get("weight")
            rpe = set_entry.get("rpe")
            if reps is None or weight is None:
                continue
            token = f"{int(reps)}x{float(weight):g}"
            if rpe is not None:
                token += f"@{float(rpe):g}"
            tokens.append(token)
        if tokens:
            lines.append(" ".join(tokens))
        if ex.get("notes"):
            for note_line in str(ex["notes"]).splitlines():
                note_line = note_line.strip()
                if note_line:
                    lines.append(f'"{note_line}"')
        lines.append("")
    return "\n".join(lines).strip()


def _recent_variations(conn: sqlite3.Connection, name: str, limit: int = 3) -> List[str]:
    if not name:
        return []
    name_norm = _norm_text(name)
    if not name_norm:
        return []
    cursor = conn.execute(
        """
        SELECT
            LOWER(TRIM(COALESCE(e.variation, ''))) AS variation
        FROM exercises e
        JOIN workouts w ON w.id = e.workout_id
        WHERE LOWER(TRIM(e.name)) = ?
        GROUP BY variation
        ORDER BY MAX(w.date_iso) DESC
        LIMIT ?
        """,
        (name_norm, limit),
    )
    return [row["variation"] for row in cursor.fetchall() if row["variation"] is not None]


def _history_for_variation(
    conn: sqlite3.Connection, name: str, variation: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    variation_norm = variation or ""
    history_rows = get_exercise_history(
        conn, exercise=name, variation=variation_norm
    )
    history = [dict(r) for r in (history_rows or [])]
    if len(history) > HISTORY_LIMIT:
        history = history[-HISTORY_LIMIT:]
    stats = _summarize_history(history)
    stats["variation"] = variation_norm
    return history, stats


def _weight_in_range(weight: Optional[float], stats: Dict[str, Any]) -> bool:
    if weight is None:
        return False
    lower = stats.get("p10") or stats.get("min")
    upper = stats.get("p90") or stats.get("max")
    if lower is None or upper is None:
        return False
    lower -= LOAD_TOLERANCE_ABS
    upper += LOAD_TOLERANCE_ABS
    return lower <= weight <= upper


def _build_plan_variation_map(plan_context: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    if not plan_context:
        return mapping
    for entry in plan_context.get("exercises", []):
        name_norm = _norm_text(entry.get("exercise_name"))
        if name_norm:
            mapping[name_norm] = entry
    return mapping


def _format_range_label(stats: Dict[str, Any]) -> str:
    if not stats:
        return ""
    sample = int(stats.get("sample_count") or 0)
    if sample == 0:
        return ""
    lower = stats.get("p10") or stats.get("min")
    upper = stats.get("p90") or stats.get("max")
    if lower is None or upper is None:
        return ""
    return f"{lower:.0f}–{upper:.0f}kg (n={sample})"


def load_plan_context(plan_id: Optional[int], session_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not plan_id:
        return None

    conn = get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if not row:
        return None

    plan = _normalize_plan(
        _safe_load_plan(row),
        fallback_name=row["name"],
        fallback_block=int(row["block_length"] or 4),
    )
    session_entry = None
    target = _norm_text(session_name)
    for day in plan.get("base_week", []):
        if _norm_text(day.get("session_name")) == target:
            session_entry = day
            break
    if session_entry is None and plan.get("base_week"):
        session_entry = plan["base_week"][0]

    exercises = []
    if session_entry:
        for entry in session_entry.get("strength_exercises", []):
            exercises.append(
                {
                    "exercise_name": entry.get("exercise_name", ""),
                    "variation": entry.get("variation", ""),
                    "sets": entry.get("sets") or 0,
                    "device": entry.get("device", ""),
                    "reps_min": entry.get("reps_min"),
                    "reps_max": entry.get("reps_max"),
                    "rpe_min": entry.get("rpe_min"),
                    "rpe_max": entry.get("rpe_max"),
                    "notes": entry.get("notes", ""),
                }
            )

    return {
        "plan_id": plan_id,
        "plan_name": plan.get("name") or row["name"],
        "session_name": session_entry.get("session_name") if session_entry else "",
        "exercises": exercises,
    }


def variation_history_snapshot(
    conn: sqlite3.Connection, exercise: str, variation: Optional[str] = None
) -> Dict[str, Any]:
    history, stats = _history_for_variation(conn, exercise, variation or "")
    return {
        "history": history,
        "stats": stats,
        "typical_range": _format_range_label(stats),
        "last_exposure": stats.get("last_exposure"),
    }


def validate_draft(
    conn: sqlite3.Connection,
    *,
    session_info: Optional[Dict[str, Any]] = None,
    structured: Optional[Dict[str, Any]] = None,
    raw_text: Optional[str] = None,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    session_info = session_info or {}
    thresholds = thresholds or {}
    raw_input = (
        (raw_text or "")
        or (structured or {}).get("raw_import_text") or ""
    ).strip()
    structured_exercises = (structured or {}).get("exercises") or []
    parse_warnings: List[str] = []
    parsed_notes = ""

    parsed_header: Optional[Dict[str, str]] = None
    parse_source_text = raw_input
    if raw_input:
        parse_source_text, parsed_header = _extract_session_header(raw_input)

    if structured_exercises:
        exercise_candidates = _normalize_structured_exercises(structured_exercises)
    else:
        parsed_exercises, parsed_notes, parse_warnings = parse_training_text(parse_source_text)
        exercise_candidates = [
            {
                "name": ex.get("name") or "",
                "variation": ex.get("variation") or "",
                "notes": ex.get("notes") or "",
                "sets": ex.get("sets") or [],
                "meta": {},
            }
            for ex in parsed_exercises
        ]

    effective_session_name = (
        (parsed_header or {}).get("session_name")
        or session_info.get("session_name")
        or session_info.get("session_type")
        or ""
    )
    effective_date_iso = (
        (parsed_header or {}).get("date_iso")
        or session_info.get("date_iso")
    )

    session_notes = session_info.get("notes") or parsed_notes or ""
    raw_import_text = raw_input or _serialize_exercises_to_text(exercise_candidates)

    plan_id = session_info.get("plan_id") or session_info.get("template_id")
    plan_context = load_plan_context(
        plan_id, effective_session_name
    )
    plan_map = _build_plan_variation_map(plan_context)
    plan_names = set(plan_map.keys())
    plan_matches = set()

    pct_threshold = float(thresholds.get("top_threshold_pct") or DEFAULT_TOP_THRESHOLD_PCT)
    abs_threshold = float(thresholds.get("top_threshold_abs") or DEFAULT_TOP_THRESHOLD_ABS)

    total_sets = 0
    total_load = 0.0
    validation_errors: List[Dict[str, Any]] = []
    validation_warnings: List[Dict[str, Any]] = []
    prepared_exercises: List[Dict[str, Any]] = []

    for idx, candidate in enumerate(exercise_candidates):
        name = (candidate.get("name") or "").strip()
        variation = (candidate.get("variation") or "").strip()
        notes = (candidate.get("notes") or "").strip()
        meta_input = candidate.get("meta") or {}
        user_confirmed = bool(meta_input.get("confirmed_variation"))

        sets_list: List[Dict[str, Any]] = []
        for set_idx, raw_set in enumerate(candidate.get("sets") or []):
            reps_raw = raw_set.get("reps")
            weight_raw = raw_set.get("weight")
            rpe_raw = raw_set.get("rpe")

            reps = (
                _parse_number(reps_raw) if isinstance(reps_raw, str) else reps_raw
            )
            weight = (
                _parse_number(weight_raw) if isinstance(weight_raw, str) else weight_raw
            )
            rpe = (
                _parse_number(rpe_raw) if isinstance(rpe_raw, str) else rpe_raw
            )

            if reps is not None:
                reps = int(round(reps))

            set_entry = {
                "reps": reps,
                "weight": float(weight) if weight is not None else None,
                "rpe": float(rpe) if rpe is not None else None,
                "raw_token": raw_set.get("raw_token") or raw_set.get("token") or "",
                "set_number": raw_set.get("set_number") or raw_set.get("order_index") or set_idx + 1,
                "order_index": set_idx + 1,
            }
            sets_list.append(set_entry)

            if reps is None:
                validation_errors.append({
                    "type": "missing_reps",
                    "message": f"Satz {set_idx + 1} von '{name or 'Übung'}' braucht Reps.",
                    "exercise_index": idx,
                    "set_index": set_idx,
                    "blocking": True,
                })
            if weight is None:
                validation_errors.append({
                    "type": "missing_weight",
                    "message": f"Satz {set_idx + 1} von '{name or 'Übung'}' braucht Gewicht.",
                    "exercise_index": idx,
                    "set_index": set_idx,
                    "blocking": True,
                })

        classify_top_backoff(sets_list, pct_threshold, abs_threshold)

        weights = [s["weight"] for s in sets_list if s["weight"] is not None]
        top_weight = max(weights) if weights else None

        total_sets += len(sets_list)
        for s in sets_list:
            if s["weight"] is not None and s["reps"] is not None:
                total_load += s["weight"] * s["reps"]

        if not name:
            validation_errors.append({
                "type": "missing_exercise_name",
                "message": "Übung ohne Namen wird nicht gespeichert.",
                "exercise_index": idx,
                "blocking": True,
            })
        if name and not variation:
            validation_warnings.append({
                "type": "missing_variation",
                "message": f"Variation fehlt bei '{name}'.",
                "exercise_index": idx,
                "blocking": False,
            })

        norm_name = _norm_text(name)
        if norm_name and norm_name in plan_names:
            plan_matches.add(norm_name)

        plan_entry = plan_map.get(norm_name)
        plan_variation = (plan_entry.get("variation") or "").strip() if plan_entry else ""
        plan_variation_norm = _norm_text(plan_variation)
        stats_plan: Dict[str, Any] = {}
        stats_user: Dict[str, Any] = {}
        if plan_variation:
            _, stats_plan = _history_for_variation(conn, name, plan_variation)
        if variation:
            _, stats_user = _history_for_variation(conn, name, variation)

        candidate_variation = ""
        candidate_variation_label = ""
        candidate_stats: Dict[str, Any] = {}
        if plan_variation and stats_plan.get("sample_count") and _weight_in_range(top_weight, stats_plan):
            candidate_variation = plan_variation_norm
            candidate_variation_label = plan_variation
            candidate_stats = stats_plan
        elif variation and stats_user.get("sample_count") and _weight_in_range(top_weight, stats_user):
            candidate_variation = _norm_text(variation)
            candidate_variation_label = variation
            candidate_stats = stats_user
        else:
            for recent in _recent_variations(conn, name):
                if not recent:
                    continue
                _, recent_stats = _history_for_variation(conn, name, recent)
                if recent_stats.get("sample_count") and _weight_in_range(top_weight, recent_stats):
                    candidate_variation = _norm_text(recent)
                    candidate_stats = recent_stats
                    candidate_variation_label = recent
                    break

        history_stats = candidate_stats or stats_user or stats_plan or {}
        typical_range = _format_range_label(history_stats)
        last_exposure = history_stats.get("last_exposure")

        plan_match = False
        load_match = bool(candidate_variation)
        confidence = "low"
        requires_confirmation = False

        if candidate_variation:
            if plan_variation and candidate_variation == plan_variation_norm:
                plan_match = True
                confidence = "high"
            elif plan_variation:
                confidence = "warn"
                requires_confirmation = True
            else:
                confidence = "medium"
        else:
            if plan_variation:
                requires_confirmation = True
            confidence = "low"

        if requires_confirmation and user_confirmed:
            requires_confirmation = False

        if requires_confirmation:
            validation_errors.append({
                "type": "variation_conflict",
                "message": f"Plan-Variation für '{name or 'Übung'}' wurde nicht bestätigt.",
                "exercise_index": idx,
                "blocking": True,
            })

        outlier_detected = False
        if candidate_stats and top_weight is not None:
            max_weight = candidate_stats.get("max") or top_weight
            min_weight = candidate_stats.get("min") or top_weight
            if top_weight > max_weight + LOAD_TOLERANCE_ABS or top_weight < min_weight - LOAD_TOLERANCE_ABS:
                outlier_detected = True
                validation_warnings.append({
                    "type": "outlier",
                    "message": f"Top-Gewicht {top_weight:.1f}kg für '{name or 'Übung'}' liegt außerhalb des üblichen Bereichs.",
                    "exercise_index": idx,
                    "blocking": False,
                })
        if outlier_detected:
            for s in sets_list:
                if s.get("is_top"):
                    s["warning"] = True

        prepared_exercises.append({
            "name": name,
            "variation": variation,
            "notes": notes,
            "sets": sets_list,
            "meta": {
                "plan_variation": plan_variation,
                "user_variation": variation,
                "resolved_variation": candidate_variation_label or variation,
                "plan_match": plan_match,
                "load_match": load_match,
                "confidence": confidence,
                "requires_confirmation": requires_confirmation,
                "confirmed_variation": user_confirmed,
                "history": {
                    "samples": int(history_stats.get("sample_count") or 0),
                    "typical_range": typical_range,
                    "last_exposure": last_exposure,
                    "range_min": history_stats.get("min"),
                    "range_max": history_stats.get("max"),
                },
                "warnings": list(meta_input.get("warnings") or []),
            },
        })

    for warning in parse_warnings:
        validation_warnings.append({
            "type": "parse",
            "message": warning,
            "blocking": False,
        })

    plan_coverage = {
        "logged": len(plan_matches),
        "planned": len(plan_names),
    }

    validation_summary = {
        "exercise_count": len(prepared_exercises),
        "set_count": total_sets,
        "warnings": len(validation_warnings),
    }

    return {
        "ok": True,
        "draft": {
            "session": {
                "date_iso": effective_date_iso,
                "session_name": effective_session_name,
                "session_type": session_info.get("session_type"),
                "plan_id": plan_id,
                "notes": session_notes,
                "raw_import_text": raw_import_text,
            },
            "exercises": prepared_exercises,
            "summary": {
                "exercise_count": len(prepared_exercises),
                "set_count": total_sets,
                "total_load": round(total_load, 2),
            },
            "thresholds": {
                "top_threshold_pct": pct_threshold,
                "top_threshold_abs": abs_threshold,
            },
        },
        "plan_context": plan_context,
        "plan_coverage": plan_coverage,
        "validation": {
            "errors": validation_errors,
            "warnings": validation_warnings,
            "summary": validation_summary,
            "blocking": len(validation_errors) > 0,
        },
    }
