from __future__ import annotations

"""Canonical database-backed training progress engine.

Every session consumer must display the payload produced here.  The module is
deliberately independent from Flask and does not persist derived values.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, time
import math
import re
import sqlite3
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from analysis.progression_rules import (
    DEFAULT_CONFIG,
    RULE_VERSION,
    RPE_REQUIRED_SINCE,
    SetComparisonResult,
    SetPerformance,
    compare_set_progress,
    is_legacy_logged_at,
    normalize_set_performance,
    summarize_session_progress,
)


MATCH_QUALITY = {
    "same_stable_slot": 1.0,
    "same_set_number": 0.85,
    "same_rank_position": 0.70,
}



def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _norm_exercise_identity(value: Any) -> str:
    """Normalize cosmetic punctuation/separators out of exercise identities.

    Historical rows can contain display-name-derived IDs (``sup. curls``)
    while newer rows contain canonical slugs/names (``sup curls`` or
    ``sup_curls``). Those are the same exercise and must share one history
    bucket. Only the variation remains a separate identity dimension.
    """

    return " ".join(re.sub(r"[\W_]+", " ", _norm(value)).split())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _canonical_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """Resolve the existing alias table to canonical exercise names.

    The current schema points aliases at historical exercise rows.  V5 treats
    that target row's normalized name as the stable canonical name while
    device, variation and laterality remain explicit identity dimensions.
    """

    if not _table_exists(conn, "exercise_aliases"):
        return {}
    cols = _columns(conn, "exercise_aliases")
    if not {"alias", "exercise_id"} <= cols:
        return {}
    rows = conn.execute(
        """
        SELECT a.alias, e.name
        FROM exercise_aliases a
        LEFT JOIN exercises e ON e.id = a.exercise_id
        WHERE a.alias IS NOT NULL
        """
    ).fetchall()
    return {
        _norm_exercise_identity(row[0]): _norm_exercise_identity(row[1])
        for row in rows
        if _norm_exercise_identity(row[0]) and _norm_exercise_identity(row[1])
    }


def canonical_exercise_name(name: Any, aliases: dict[str, str] | None = None) -> str:
    normalized = _norm_exercise_identity(name)
    alias_map = aliases or {}
    seen: set[str] = set()
    while normalized in alias_map and normalized not in seen:
        seen.add(normalized)
        normalized = alias_map[normalized]
    return normalized


def _identity(row: dict[str, Any], aliases: dict[str, str]) -> tuple[str, str]:
    canonical = canonical_exercise_name(
        row.get("exercise_name") or row.get("canonical_exercise_id"), aliases
    )
    return (
        canonical,
        _norm(row.get("variation_id") or row.get("variation")),
    )


def _eligible(row: dict[str, Any], *, require_current_rpe: bool = True) -> tuple[bool, str | None]:
    try:
        weight = float(row.get("weight"))
        reps = float(row.get("reps"))
    except (TypeError, ValueError):
        return False, "missing_performance_data"
    if not math.isfinite(weight) or weight <= 0:
        return False, "missing_weight"
    if not math.isfinite(reps) or reps <= 0:
        return False, "missing_reps"
    if row.get("is_warmup") in (True, 1, "1"):
        return False, "flagged_warmup"
    if row.get("inferred_warmup") is True:
        return False, "inferred_warmup"
    if row.get("is_working_set") in (False, 0, "0"):
        return False, "flagged_non_working_set"
    if row.get("intentional_deload") is True:
        return False, "intentional_deload"
    if row.get("technique_set") is True:
        return False, "technique_set"
    if require_current_rpe and row.get("rpe") is None and not is_legacy_logged_at(row.get("logged_at")):
        return False, "missing_current_rpe"
    return True, None


def _load_sessions(conn: sqlite3.Connection, end_iso: str | None = None) -> list[dict[str, Any]]:
    exercise_cols = _columns(conn, "exercises")
    set_cols = _columns(conn, "sets")
    workout_cols = _columns(conn, "workouts")
    canonical_col = next(
        (c for c in ("canonical_exercise_id", "canonical_id", "exercise_key") if c in exercise_cols),
        None,
    )
    canonical_expr = f"e.{canonical_col}" if canonical_col else "NULL"
    variation_id_col = next((c for c in ("variation_id", "canonical_variation_id") if c in exercise_cols), None)
    variation_id_expr = f"COALESCE(e.{variation_id_col}, e.variation)" if variation_id_col else "e.variation"
    execution_mode_col = next((c for c in ("execution_mode", "movement_execution_mode") if c in exercise_cols), None)
    execution_mode_expr = f"e.{execution_mode_col}" if execution_mode_col else "''"
    session_key_expr = "w.session_key" if "session_key" in workout_cols else "w.name"
    warmup_col = next((c for c in ("is_warmup", "warmup") if c in set_cols), None)
    working_col = next((c for c in ("is_work_set", "is_working_set", "working_set") if c in set_cols), None)
    progression_excluded_col = "progression_excluded" if "progression_excluded" in set_cols else None
    progression_exclusion_reason_col = "progression_exclusion_reason" if "progression_exclusion_reason" in set_cols else None
    slot_col = next((c for c in ("set_slot", "slot_name", "slot") if c in set_cols), None)
    intentional_deload_col = next((c for c in ("intentional_deload", "is_deload") if c in set_cols), None)
    technique_col = next((c for c in ("technique_set", "is_technique_set") if c in set_cols), None)
    warmup_expr = f"s.{warmup_col}" if warmup_col else "NULL"
    working_expr = f"s.{working_col}" if working_col else "NULL"
    progression_excluded_expr = f"COALESCE(s.{progression_excluded_col}, 0)" if progression_excluded_col else "0"
    progression_exclusion_reason_expr = f"s.{progression_exclusion_reason_col}" if progression_exclusion_reason_col else "NULL"
    slot_expr = f"s.{slot_col}" if slot_col else "NULL"
    intentional_deload_expr = f"COALESCE(s.{intentional_deload_col}, 0)" if intentional_deload_col else "0"
    technique_expr = f"COALESCE(s.{technique_col}, 0)" if technique_col else "0"
    set_created_expr = "s.created_at" if "created_at" in set_cols else "NULL"
    workout_created_expr = "w.created_at" if "created_at" in workout_cols else "NULL"
    where = "WHERE SUBSTR(w.date_iso,1,10) <= ?" if end_iso else ""
    params: tuple[Any, ...] = (end_iso,) if end_iso else ()
    rows = conn.execute(
        f"""
        SELECT w.id workout_id, w.date_iso, w.name session_name, {session_key_expr} session_key,
               e.id exercise_id, e.name exercise_name,
               COALESCE(e.device,'') device, COALESCE(e.variation,'') variation,
               COALESCE(e.laterality,'') laterality,
               {canonical_expr} canonical_exercise_id,
               COALESCE({variation_id_expr}, '') variation_id,
               COALESCE({execution_mode_expr}, '') execution_mode,
               s.id source_set_id, COALESCE(s.set_number,0) set_number,
               {slot_expr} set_slot, s.weight, s.reps, s.rpe,
               {warmup_expr} is_warmup, {working_expr} is_working_set,
               {progression_excluded_expr} progression_excluded,
               {progression_exclusion_reason_expr} progression_exclusion_reason,
               {intentional_deload_expr} intentional_deload,
               {technique_expr} technique_set
               ,COALESCE({set_created_expr}, {workout_created_expr}, w.date_iso || 'T00:00:00Z') logged_at
        FROM workouts w
        LEFT JOIN exercises e ON e.workout_id=w.id
        LEFT JOIN sets s ON s.exercise_id=e.id
        {where}
        ORDER BY SUBSTR(w.date_iso,1,10), w.id, e.id,
                 COALESCE(s.set_number,0), s.id
        """,
        params,
    ).fetchall()
    sessions: list[dict[str, Any]] = []
    by_id: dict[int, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw) if isinstance(raw, sqlite3.Row) else {
            key: raw[idx] for idx, key in enumerate(
                ("workout_id", "date_iso", "session_name", "session_key", "exercise_id", "exercise_name",
                 "device", "variation", "laterality", "canonical_exercise_id", "variation_id", "execution_mode", "source_set_id",
                 "set_number", "set_slot", "weight", "reps", "rpe", "is_warmup", "is_working_set", "progression_excluded", "progression_exclusion_reason",
                 "intentional_deload", "technique_set", "logged_at")
            )
        }
        workout_id = int(row["workout_id"])
        session = by_id.get(workout_id)
        if session is None:
            session = {
                "session_id": workout_id,
                "session_date": str(row.get("date_iso") or "")[:10],
                "session_name": str(row.get("session_name") or ""),
                "session_key": str(row.get("session_key") or row.get("session_name") or ""),
                "sets": [],
            }
            by_id[workout_id] = session
            sessions.append(session)
        if row.get("source_set_id") is None:
            continue
        row["order_idx"] = len(session["sets"])
        session["sets"].append(row)
    return sessions


def _slot_sets(rows: list[dict[str, Any]], aliases: dict[str, str]) -> list[dict[str, Any]]:
    by_exercise: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_exercise[int(row.get("exercise_id") or 0)].append(row)
    output: list[dict[str, Any]] = []
    for exercise_rows in by_exercise.values():
        exercise_rows = sorted(
            exercise_rows,
            key=lambda row: (int(row.get("set_number") or 0), int(row.get("source_set_id") or 0)),
        )
        inferred_warmup_ids: set[int] = set()
        weighted = [
            (idx, float(row["weight"]))
            for idx, row in enumerate(exercise_rows)
            if row.get("weight") is not None and float(row.get("weight") or 0) > 0
        ]
        if weighted:
            first_max_idx, max_weight = max(weighted, key=lambda item: item[1])
            safe_reps = [
                float(row.get("reps") or 0)
                for idx, row in enumerate(exercise_rows)
                if idx >= first_max_idx and row.get("reps") is not None and float(row.get("reps") or 0) > 0
            ]
            typical_work_reps = sorted(safe_reps)[len(safe_reps) // 2] if safe_reps else None
            for idx, weight in weighted:
                if idx >= first_max_idx:
                    continue
                row = exercise_rows[idx]
                reps = float(row.get("reps") or 0)
                outside_work_scheme = bool(
                    typical_work_reps is not None
                    and reps > 0
                    and abs(reps - typical_work_reps) >= 4
                    and weight < max_weight
                )
                clearly_lighter = weight <= max_weight * 0.85
                if (
                    row.get("is_warmup") is None
                    and row.get("is_working_set") not in (True, 1, "1")
                    and (clearly_lighter or outside_work_scheme)
                ):
                    inferred_warmup_ids.add(int(row.get("source_set_id") or 0))

        perf_rows: list[tuple[SetPerformance, dict[str, Any], int]] = []
        for rank, row in enumerate(exercise_rows, start=1):
            identity = _identity(row, aliases)
            perf = normalize_set_performance(
                {
                    **row,
                    "canonical_exercise": identity[0],
                    "canonical_exercise_id": identity[0],
                    "variation_id": identity[1],
                }
            )
            if perf is not None:
                perf_rows.append((perf, row, rank))
        for perf, raw_row, rank in perf_rows:
            explicit_slot = str(raw_row.get("set_slot") or "").strip()
            if explicit_slot:
                stable_slot = explicit_slot
                slot_source = "explicit_set_slot"
            elif perf.set_number is not None and perf.set_number > 0:
                stable_slot = f"set_number:{perf.set_number}"
                slot_source = "legacy_set_number"
            else:
                stable_slot = f"rank:{rank}"
                slot_source = "legacy_rank_position"
            perf = replace(perf, set_slot=stable_slot, slot_source=slot_source)
            output.append(
                {
                    **asdict(perf),
                    "set_slot": stable_slot,
                    "slot_source": slot_source,
                    "rank_position": rank,
                    "inferred_warmup": int(raw_row.get("source_set_id") or 0) in inferred_warmup_ids,
                    "is_warmup": raw_row.get("is_warmup"),
                    "is_working_set": raw_row.get("is_working_set"),
                    "intentional_deload": bool(raw_row.get("intentional_deload")),
                    "technique_set": bool(raw_row.get("technique_set")),
                    "progression_exclusion_reason": raw_row.get("progression_exclusion_reason"),
                    "identity": (
                        perf.canonical_exercise or perf.exercise_name,
                        perf.variation_id,
                    ),
                }
            )
    return sorted(output, key=lambda row: (int(row.get("order_idx") or 0), int(row.get("source_set_id") or 0)))


def _fixed_slot_match(current: dict[str, Any], reference: dict[str, Any]) -> tuple[str, float] | None:
    current_slot = str(current.get("set_slot") or "")
    reference_slot = str(reference.get("set_slot") or "")
    current_source = str(current.get("slot_source") or "")
    reference_source = str(reference.get("slot_source") or "")
    if current_slot and reference_slot and current_slot == reference_slot and "explicit_set_slot" in {current_source, reference_source}:
        return "same_stable_slot", MATCH_QUALITY["same_stable_slot"]
    current_number = current.get("set_number")
    reference_number = reference.get("set_number")
    if current_number is not None and reference_number is not None and int(current_number) == int(reference_number):
        return "same_set_number", MATCH_QUALITY["same_set_number"]
    current_rank = current.get("rank_position")
    reference_rank = reference.get("rank_position")
    if current_rank is not None and reference_rank is not None and int(current_rank) == int(reference_rank):
        return "same_rank_position", MATCH_QUALITY["same_rank_position"]
    return None


def _comparison_dict(
    result: SetComparisonResult,
    *,
    category: str,
    match_quality: float,
    reference_distance_executions: int | None = None,
    reference_distance_days: int | None = None,
    fallback_to_older_execution: bool = False,
    fallback_reason: str | None = None,
    reference_session_id: int | None = None,
    reference_session_date: str | None = None,
) -> dict[str, Any]:
    return {
        "current_set_id": result.current.source_set_id if result.current else None,
        "reference_set_id": result.reference.source_set_id if result.reference else None,
        "reference_session_id": result.reference.source_workout_id if result.reference else None,
        "reference_session_date": result.reference.source_session_date if result.reference else None,
        "status": result.status,
        "result": result.status,
        "included": result.comparable,
        "exclusion_reason": result.exclusion_reason,
        "comparison_mode": result.comparison_mode,
        "category": category,
        "comparable": result.comparable,
        "match_level": result.match_level,
        "match_quality": match_quality,
        "reason_code": result.reason_code,
        "reason_text": result.reason_text,
        "deltas": result.deltas,
        "exercise_name": result.current.exercise_name if result.current else None,
        "canonical_exercise": result.current.canonical_exercise if result.current else None,
        "device": result.current.device if result.current else None,
        "variation": result.current.variation if result.current else None,
        "variation_id": result.current.variation_id if result.current else None,
        "laterality": result.current.laterality if result.current else None,
        "execution_mode": result.current.execution_mode if result.current else None,
        "set_slot": result.current.set_slot if result.current else None,
        "reference_set_slot": result.reference.set_slot if result.reference else None,
        "reference_distance_executions": reference_distance_executions,
        "reference_distance_days": reference_distance_days,
        "fallback_to_older_execution": fallback_to_older_execution,
        "fallback_reason": fallback_reason,
        "reference_session_id": reference_session_id,
        "reference_session_date": reference_session_date,
    }


def _confidence(comparable: int, total: int, average_quality: float) -> tuple[str, float]:
    if total <= 0 or comparable <= 0:
        return "none", 0.0
    coverage = comparable / total
    sample_factor = min(1.0, comparable / 4.0)
    score = max(0.0, min(1.0, coverage * average_quality * (0.5 + 0.5 * sample_factor)))
    if score >= 0.75:
        return "high", score
    if score >= 0.65:
        return "medium", score
    if score >= 0.20:
        return "low", score
    return "very_low", score


def _session_payload(
    session: dict[str, Any],
    history: dict[tuple[str, str], list[dict[str, Any]]],
    aliases: dict[str, str],
) -> dict[str, Any]:
    slotted = session.get("_progression_slotted_sets")
    if slotted is None:
        slotted = _slot_sets(session["sets"], aliases)
        session["_progression_slotted_sets"] = slotted
    eligible: list[dict[str, Any]] = []
    unknown_items: list[dict[str, Any]] = []
    for row in slotted:
        # An explicit coach exclusion is authoritative and must retain its
        # human reason even when a warm-up/technique heuristic would otherwise
        # classify the set first.
        if row.get("progression_excluded"):
            eligible.append(row)
            continue
        ok, reason = _eligible(row)
        if ok:
            eligible.append(row)
        else:
            unknown_items.append({"row": row, "reason": reason})

    low_level: list[SetComparisonResult] = []
    details: list[dict[str, Any]] = []
    match_qualities: list[float] = []
    new_exercise = 0
    unmatched_known = 0

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        if row.get("progression_excluded"):
            exclusion_reason = str(row.get("progression_exclusion_reason") or "").strip() or "Vom Coach einmalig als nicht vergleichbar markiert."
            result = compare_set_progress(
                normalize_set_performance(row) or SetPerformance(),
                None,
                match_level="one_time_progression_exclusion",
            )
            low_level.append(result)
            details.append({
                **_comparison_dict(result, category="progression_excluded", match_quality=0.0),
                "reason_code": "one_time_progression_exclusion",
                "reason_text": exclusion_reason,
                "exclusion_reason": exclusion_reason,
            })
            continue
        grouped[tuple(row["identity"])].append(row)

    for identity, current_rows in grouped.items():
        all_exposures = history.get(identity, [])
        context = _norm(session.get("session_key") or session.get("session_name"))
        contextual = [exposure for exposure in all_exposures if _norm(exposure.get("session_key") or exposure.get("session_name")) == context]
        exposures = contextual or all_exposures
        if not exposures:
            related_identities = [known for known in history if known[0] == identity[0]]
            exclusion_reason = "new_baseline"
            if related_identities:
                if not any(known[1] == identity[1] for known in related_identities):
                    exclusion_reason = "different_variation"
            for current in current_rows:
                result = compare_set_progress(normalize_set_performance(current) or SetPerformance(), None, match_level="no_identity_history")
                low_level.append(result)
                details.append({
                    **_comparison_dict(result, category="excluded", match_quality=0.0),
                    "reason_code": exclusion_reason,
                    "reason_text": "Erste auswertbare Ausfuehrung dieser Uebungsidentitaet." if exclusion_reason == "new_baseline" else "Vorhandene Historie hat eine andere Uebungsidentitaet.",
                    "exclusion_reason": exclusion_reason,
                })
                new_exercise += 1
            continue

        used_reference: set[int] = set()
        matched: dict[int, tuple[dict[str, Any], str, float, int, int | None, bool, str | None, dict[str, Any]]] = {}

        def match_exposure(
            pending: list[dict[str, Any]],
            exposure: dict[str, Any],
            *,
            fallback: bool,
            execution_distance: int,
        ) -> list[dict[str, Any]]:
            try:
                reference_distance_days = max(
                    0,
                    (date.fromisoformat(str(session.get("session_date") or "")[:10]) - date.fromisoformat(str(exposure.get("session_date") or "")[:10])).days,
                )
            except ValueError:
                reference_distance_days = None
            used_current: set[int] = set()
            matched_here: list[dict[str, Any]] = []
            level_rank = {"same_stable_slot": 0, "same_set_number": 1, "same_rank_position": 2}
            for current in pending:
                current_id = int(current.get("source_set_id") or 0)
                candidates: list[tuple[int, int, dict[str, Any], str, float]] = []
                for reference in exposure["sets"]:
                    reference_id = int(reference.get("source_set_id") or 0)
                    if reference_id in used_reference:
                        continue
                    slot_match = _fixed_slot_match(current, reference)
                    if slot_match is None:
                        continue
                    level, quality = slot_match
                    candidates.append((level_rank[level], reference_id, reference, level, quality))
                if not candidates:
                    continue
                _, reference_id, reference, level, quality = min(candidates, key=lambda item: (item[0], item[1]))
                used_current.add(current_id)
                used_reference.add(reference_id)
                matched[current_id] = (
                    reference,
                    level,
                    quality,
                    execution_distance,
                    reference_distance_days,
                    fallback,
                    "latest_execution_capacity_exhausted" if fallback else None,
                    exposure,
                )
                matched_here.append(current)
            return [row for row in pending if int(row.get("source_set_id") or 0) not in used_current]

        # The latest actual execution is authoritative. Older executions are
        # considered only for current sets that exceed its one-to-one capacity.
        pending = match_exposure(current_rows, exposures[-1], fallback=False, execution_distance=1)
        for reference_index in range(len(exposures) - 2, -1, -1):
            if not pending:
                break
            exposure = exposures[reference_index]
            pending = match_exposure(
                pending,
                exposure,
                fallback=True,
                execution_distance=len(exposures) - reference_index,
            )

        for current in current_rows:
            current_id = int(current.get("source_set_id") or 0)
            match = matched.get(current_id)
            if match is None:
                result = compare_set_progress(normalize_set_performance(current) or SetPerformance(), None, match_level="known_identity_reference_capacity_exhausted")
                low_level.append(result)
                details.append({
                    **_comparison_dict(
                        result,
                        category="excluded",
                        match_quality=0.0,
                        fallback_reason="no_matching_fixed_slot",
                    ),
                    "reason_code": "missing_reference",
                    "reason_text": "Kein gleicher fester Satz-Slot in der Referenzhistorie.",
                    "exclusion_reason": "missing_reference",
                })
                unmatched_known += 1
                continue
            reference, level, quality, execution_distance, reference_distance_days, fallback, fallback_reason, exposure = match
            # Preserve the old match-level token: zero means the immediately
            # preceding execution. The explicit distance field is one-based.
            match_level = f"{level}:lookback_{max(0, execution_distance - 1)}"
            result = compare_set_progress(
                normalize_set_performance(current) or SetPerformance(),
                normalize_set_performance(reference),
                match_level=match_level,
            )
            low_level.append(result)
            details.append(_comparison_dict(
                result,
                category=result.status or "excluded",
                match_quality=quality,
                reference_distance_executions=execution_distance,
                reference_distance_days=reference_distance_days,
                fallback_to_older_execution=fallback,
                fallback_reason=fallback_reason,
                reference_session_id=int(exposure.get("session_id")) if exposure.get("session_id") is not None else None,
                reference_session_date=str(exposure.get("session_date") or "")[:10] or None,
            ))
            if result.comparable:
                match_qualities.append(quality)

    for item in unknown_items:
        row = item["row"]
        result = compare_set_progress(normalize_set_performance(row) or SetPerformance(), None, match_level="ineligible")
        low_level.append(result)
        details.append({
            **_comparison_dict(result, category="excluded", match_quality=0.0),
            "reason_code": item["reason"] or "excluded",
            "exclusion_reason": item["reason"] or "excluded",
        })

    summary = summarize_session_progress(low_level, DEFAULT_CONFIG)
    total = len(slotted)
    average_quality = sum(match_qualities) / len(match_qualities) if match_qualities else 0.0
    confidence, confidence_score = _confidence(summary.comparable_sets, total, average_quality)
    coverage = summary.comparable_sets / total if total else None
    comparison_success_rate = summary.comparable_sets / len(eligible) if eligible else None
    exclusion_reasons = Counter(
        str(item.get("exclusion_reason") or item.get("reason_code") or "excluded")
        for item in details
        if not item.get("included")
    )
    return {
        "session_id": session["session_id"],
        "session_date": session["session_date"],
        "session_name": session["session_name"],
        "rule_version": RULE_VERSION,
        "total_sets": total,
        "total_work_sets": total,
        "total_eligible_sets": len(eligible),
        "comparable_sets": summary.comparable_sets,
        "progress_sets": summary.progress,
        "stable_sets": summary.stable,
        "regress_sets": summary.regress,
        "excluded_sets": summary.excluded,
        "improved": summary.improved,
        "same": summary.same,
        "worse": summary.worse,
        "new_exercise": new_exercise,
        "unmatched_known_exercise": unmatched_known,
        "unknown": len(unknown_items) + summary.unknown,
        "coverage": coverage,
        "coverage_all_work_sets": coverage,
        "comparison_success_rate": comparison_success_rate,
        "progress_rate": summary.progress_rate,
        "stable_rate": (summary.stable / summary.comparable_sets) if summary.comparable_sets else None,
        "regression_rate": summary.regression_rate,
        "net_progress": summary.net_progress,
        "comparison_confidence": confidence,
        "comparison_confidence_score": confidence_score,
        "average_match_quality": average_quality,
        "status": summary.status,
        "session_result": summary.status,
        "reason_code": summary.reason_code,
        "reason_text": summary.reason_text,
        "exclusion_reasons": dict(exclusion_reasons),
        "set_partition": {
            "total_work_sets": total,
            "compared": summary.comparable_sets,
            "excluded": summary.excluded,
            "exclusion_reasons": dict(exclusion_reasons),
        },
        "set_comparisons": details,
    }


def compute_progress_payloads(
    conn: sqlite3.Connection,
    *,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[dict[str, Any]]:
    aliases = _canonical_aliases(conn)
    sessions = _load_sessions(conn, end_iso=end_iso)
    history: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    payloads: list[dict[str, Any]] = []
    for session_order, session in enumerate(sessions):
        session["_order_idx"] = session_order
        payload = _session_payload(session, history, aliases)
        if start_iso is None or session["session_date"] >= start_iso:
            payloads.append(payload)
        slotted = session.get("_progression_slotted_sets")
        if slotted is None:
            slotted = _slot_sets(session["sets"], aliases)
            session["_progression_slotted_sets"] = slotted
        by_identity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in slotted:
            if _eligible(row, require_current_rpe=False)[0]:
                by_identity[tuple(row["identity"])].append(row)
        for identity, rows in by_identity.items():
            history[identity].append({"session_id": session["session_id"], "session_date": session["session_date"], "session_name": session["session_name"], "session_key": session.get("session_key"), "session_order": session_order, "sets": rows})
    return payloads


def compute_session_progress(conn: sqlite3.Connection, session_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT SUBSTR(date_iso,1,10) FROM workouts WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    end_iso = str(row[0])
    return next(
        (payload for payload in compute_progress_payloads(conn, end_iso=end_iso) if int(payload["session_id"]) == int(session_id)),
        None,
    )


def compute_progress_series(
    conn: sqlite3.Connection,
    *,
    start_iso: str,
    end_iso: str,
    trend_window_sessions: int = 6,
    debug: bool = False,
) -> dict[str, Any]:
    payloads = compute_progress_payloads(conn, start_iso=start_iso, end_iso=end_iso)
    points: list[dict[str, Any]] = []
    raw_rates: list[float | None] = []
    weights: list[float] = []
    for payload in payloads:
        dt = datetime.combine(date.fromisoformat(payload["session_date"]), time.min).replace(tzinfo=ZoneInfo("Europe/Berlin"))
        point = {
            **payload,
            "t": int(dt.timestamp() * 1000),
            "t_iso": payload["session_date"],
            # Compatibility aliases for existing display-only consumers.
            "comparable": payload["comparable_sets"],
            "total": payload["total_work_sets"],
            "eligible": payload["total_eligible_sets"],
            "new": payload["new_exercise"],
            "sets_total_current": payload["total_work_sets"],
            "sets_eligible_current": payload["total_eligible_sets"],
            "sets_compared": payload["comparable_sets"],
            "sets_unmatched": payload["new_exercise"] + payload["unmatched_known_exercise"] + payload["unknown"],
            "comparison_coverage_pct": round(payload["coverage"] * 100) if payload["coverage"] is not None else None,
            "coverage_all_work_sets": payload["coverage_all_work_sets"],
            "comparison_success_rate": payload["comparison_success_rate"],
            "no_baseline": payload["comparable_sets"] == 0 and payload["total_eligible_sets"] > 0,
            "low_comparable": payload["comparable_sets"] < 2,
            "unmatched_reasons": [
                {
                    "set_id": comparison.get("current_set_id"),
                    "reason": (
                        "no_previous_set_at_position"
                        if comparison.get("category") == "unmatched_known_exercise"
                        else comparison.get("reason_code")
                    ),
                }
                for comparison in payload.get("set_comparisons") or []
                if not comparison.get("comparable")
            ],
        }
        raw_rates.append(payload["progress_rate"])
        weights.append(math.sqrt(payload["comparable_sets"]) if payload["comparable_sets"] > 0 else 0.0)
        points.append(point)
    window = max(1, int(trend_window_sessions))
    for idx, point in enumerate(points):
        start = max(0, idx - window + 1)
        pairs = [(raw_rates[i], weights[i]) for i in range(start, idx + 1) if raw_rates[i] is not None and weights[i] > 0]
        point["trend"] = (sum(float(v) * w for v, w in pairs) / sum(w for _, w in pairs)) if pairs else None
    total = sum(p["total_work_sets"] for p in payloads)
    eligible = sum(p["total_eligible_sets"] for p in payloads)
    comparable = sum(p["comparable_sets"] for p in payloads)
    reason_hist = Counter(
        comparison["reason_code"]
        for payload in payloads
        for comparison in payload["set_comparisons"]
    )
    comparison_mode_counts = Counter(
        str(comparison.get("comparison_mode"))
        for payload in payloads
        for comparison in payload["set_comparisons"]
        if comparison.get("comparable") and comparison.get("comparison_mode")
    )
    meta = {
        "mode": "session",
        "rule_version": RULE_VERSION,
        "trend_window_sessions": window,
        "trend_weight": "sqrt(comparable)",
        "baseline_lookback_sessions": None,
        "min_comparable_per_session": 1,
        "coverage": comparable / total if total else None,
        "coverage_all_work_sets": comparable / total if total else None,
        "comparison_success_rate": comparable / eligible if eligible else None,
        "total_work_sets": total,
        "eligible_sets": eligible,
        "comparable_sets": comparable,
        "comparison_mode_counts": dict(comparison_mode_counts),
        "avg_comparable": comparable / len(payloads) if payloads else None,
        "low_comparable_sessions_count": sum(1 for p in points if p["low_comparable"]),
        "plan_change_flag": (sum(p["new_exercise"] for p in payloads) / total >= 0.35) if total else False,
        "range_start": start_iso,
        "range_end": end_iso,
    }
    monthly: dict[str, Counter[str]] = defaultdict(Counter)
    for payload in payloads:
        bucket = monthly[payload["session_date"][:7]]
        for key in ("total_work_sets", "total_eligible_sets", "comparable_sets", "progress_sets", "stable_sets", "regress_sets", "excluded_sets"):
            bucket[key] += int(payload.get(key) or 0)
    meta["monthly_coverage"] = []
    for month in sorted(monthly):
        bucket = monthly[month]
        month_total = int(bucket["total_work_sets"])
        month_comparable = int(bucket["comparable_sets"])
        month_coverage = month_comparable / month_total if month_total else None
        meta["monthly_coverage"].append({
            "month": month,
            "total_work_sets": month_total,
            "eligible_sets": int(bucket["total_eligible_sets"]),
            "comparable_sets": month_comparable,
            "progress_sets": int(bucket["progress_sets"]),
            "stable_sets": int(bucket["stable_sets"]),
            "regress_sets": int(bucket["regress_sets"]),
            "excluded_sets": int(bucket["excluded_sets"]),
            "coverage_pct": round(month_coverage * 100, 1) if month_coverage is not None else None,
            "progress_pct": round((bucket["progress_sets"] / month_comparable) * 100, 1) if month_comparable else None,
        })
    if debug:
        lookback_hist: Counter[int] = Counter()
        for payload in payloads:
            for comparison in payload.get("set_comparisons") or []:
                marker = ":lookback_"
                match_level = str(comparison.get("match_level") or "")
                if marker in match_level:
                    try:
                        lookback_hist[int(match_level.rsplit(marker, 1)[1])] += 1
                    except ValueError:
                        pass
        meta["debug"] = {
            "used_rule_version": RULE_VERSION,
            "full_exercise_history": True,
            "one_to_one_matching": True,
            "total_sessions_found": len(payloads),
            "points_count": len(points),
            "comparable_sessions_count": sum(1 for p in payloads if p["comparable_sets"] > 0),
            "new_only_sessions_count": sum(1 for p in payloads if p["comparable_sets"] == 0 and p["total_eligible_sets"] > 0),
            "first_session_ts": points[0]["t"] if points else None,
            "last_session_ts": points[-1]["t"] if points else None,
            "baseline_hit_rate": comparable / total if total else None,
            "lookback_histogram": dict(lookback_hist),
            "reason_code_histogram": dict(reason_hist),
        }
    return {"points": points, "meta": meta}


def progress_payload_for_gpt(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    comparable = int(payload.get("comparable_sets") or 0)
    unmatched = int(payload.get("excluded_sets") or 0)
    progression_quote = payload.get("progress_rate")
    quote_text = (
        f"Progressionsquote: {float(progression_quote) * 100:.1f} %. "
        if progression_quote is not None
        else "Progressionsquote: nicht verfuegbar. "
    )
    text = (
        f"{quote_text}"
        f"{payload.get('progress_sets', 0)}/{comparable} verbessert, "
        f"{payload.get('stable_sets', 0)}/{comparable} stabil und "
        f"{payload.get('regress_sets', 0)}/{comparable} schlechter. "
        f"{unmatched} Sätze technisch ausgeschlossen. "
        f"Session: {payload.get('status')}."
    )
    def key_part(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    reference_sessions: Counter[tuple[int, str | None]] = Counter()
    for comparison in payload.get("set_comparisons") or []:
        key = (
            str(comparison.get("canonical_exercise") or comparison.get("exercise_name") or ""),
            str(comparison.get("variation") or ""),
        )
        group = grouped.setdefault(
            key,
            {
                # Keep the established public identifier shape for API
                # compatibility. Grouping/comparison identity above is still
                # strictly exercise + variation.
                "canonical_id": "__".join(
                    filter(
                        None,
                        (
                            key_part(key[0]),
                            key_part(comparison.get("device")),
                            key_part(comparison.get("laterality")),
                        ),
                    )
                ) or None,
                "name": comparison.get("exercise_name") or key[0],
                "match_basis": "canonical_id_exact",
                "confidence": 1.0 if comparison.get("match_quality") == 1.0 else comparison.get("match_quality") or 0.0,
                "sets_compared": 0,
                "sets_improved": 0,
                "sets_stable": 0,
                "sets_regressed": 0,
            },
        )
        if comparison.get("comparable"):
            group["sets_compared"] += 1
            reference_id = comparison.get("reference_session_id")
            if reference_id is not None:
                reference_sessions[(int(reference_id), comparison.get("reference_session_date"))] += 1
        if comparison.get("status") == "progress":
            group["sets_improved"] += 1
        elif comparison.get("status") == "stable":
            group["sets_stable"] += 1
        elif comparison.get("status") == "regress":
            group["sets_regressed"] += 1
    available = comparable > 0
    for group in grouped.values():
        group["current"] = {"canonical_id": group["canonical_id"], "name": group["name"]}
        group["previous"] = {"canonical_id": group["canonical_id"], "name": group["name"]}
        numerator = group["sets_improved"] if group["sets_improved"] else group["sets_regressed"]
        group["summary"] = f"{numerator}/{group['sets_compared']} Sätze"
    comparable_groups = [group for group in grouped.values() if group["sets_compared"] > 0]
    top_groups = [group for group in comparable_groups if group["sets_improved"] > 0]
    weak_groups = [group for group in comparable_groups if group["sets_regressed"] > 0]
    primary_reference = reference_sessions.most_common(1)[0][0] if reference_sessions else (None, None)
    # The canonical payload retains all diagnostic rates for internal views.
    # GPT-facing consumers get one aggregate quote only, so the model cannot
    # present coverage, success and net-balance as competing progress quotes.
    hidden_gpt_rates = {
        "progress_rate",
        "stable_rate",
        "regression_rate",
        "net_progress",
        "coverage",
        "coverage_all_work_sets",
        "comparison_success_rate",
        "comparison_confidence_score",
        "average_match_quality",
    }
    gpt_payload = {key: value for key, value in payload.items() if key not in hidden_gpt_rates}
    return {
        **gpt_payload,
        "summary_text": text,
        # Stable compatibility names for existing coach-view clients. Values
        # are aliases of the canonical payload, never recomputed counts.
        "available": available,
        "reason": None if available else "no_comparable_previous_session",
        "sets_compared": comparable,
        "sets_improved": int(payload.get("progress_sets") or 0),
        "sets_stable": int(payload.get("stable_sets") or 0),
        "sets_regressed": int(payload.get("regress_sets") or 0),
        "sets_progress": int(payload.get("progress_sets") or 0),
        "sets_regress": int(payload.get("regress_sets") or 0),
        "last_workout_id": primary_reference[0],
        "last_session_date": primary_reference[1],
        "progression_quote": progression_quote,
        "progression_quote_definition": "sets_improved / sets_compared",
        "summary": text,
        "fallback_summary": {"available": available, "basis": "canonical_progress_engine"},
        "exercise_comparisons": comparable_groups,
        "top_lifts": top_groups,
        "weak_spots": weak_groups,
        "top_lifts_aggregated": top_groups,
        "weak_spots_aggregated": weak_groups,
        "warnings": [f"warmup_status_unclear:{group['canonical_id']}" for group in comparable_groups],
    }
