from __future__ import annotations

from typing import Any
from analysis.training_analysis import classify_pattern
import re


def _norm_text(val: str) -> str:
    return " ".join((val or "").strip().lower().split())


def _slugify(val: str) -> str:
    text = _norm_text(val)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _session_key_from_day(day_idx: int, session_name: str) -> str:
    slug = _slugify(session_name) or f"day-{day_idx + 1}"
    return f"d{day_idx + 1}-{slug}"


def _exercise_key(name: str, variation: str | None, device: str | None, slot: int) -> str:
    return f"{slot}:{_norm_text(name)}:{_norm_text(variation or '')}:{_norm_text(device or '')}"


def expected_exercises_for_session(
    planned_session: dict | None,
    *,
    day_id: str,
    plan_ctx: dict,
    mode: str | None = None,
) -> dict:
    """
    Build the SSOT expected exercise list for a plan session.

    Returns a stable, serializable dict:
    {
      session_key, planned_label, final_label,
      source: {plan_id, day_id, reason},
      exercises: [ {exercise_key, name, variation, device, slot, is_optional, tags, expected_sets, expected_reps, rpe_min, rpe_max, notes} ]
    }
    """
    planned_session = planned_session or {}
    session_key = planned_session.get("session_key") or ""
    planned_label = planned_session.get("session_name") or planned_session.get("label") or ""
    final_label = planned_session.get("label") or planned_label

    base_week = plan_ctx.get("base_week") or []
    plan_id = plan_ctx.get("id")

    plan_day = None
    for idx, day in enumerate(base_week):
        if not isinstance(day, dict):
            continue
        strength = day.get("strength_exercises") or []
        if not isinstance(strength, list) or not strength:
            continue
        name = (day.get("session_name") or "").strip() or f"Session {idx + 1}"
        key = _session_key_from_day(idx, name)
        if key == session_key:
            plan_day = day
            break

    if not plan_day:
        return {
            "session_key": session_key,
            "planned_label": planned_label,
            "final_label": final_label,
            "source": {
                "plan_id": plan_id,
                "day_id": day_id,
                "reason": "NO_STRENGTH_SESSION",
            },
            "exercises": [],
        }

    rows = plan_day.get("strength_exercises") or []
    if not isinstance(rows, list):
        rows = []

    exercises = []
    for slot, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = (row.get("exercise_name") or row.get("name") or "").strip()
        if not name:
            continue
        variation = (row.get("variation") or "").strip() or None
        device = (row.get("device") or "").strip() or None
        reps_min = row.get("reps_min")
        reps_max = row.get("reps_max")
        expected_reps = None
        if reps_min is not None or reps_max is not None:
            try:
                rmin = int(reps_min) if reps_min is not None else None
                rmax = int(reps_max) if reps_max is not None else None
                if rmin is not None and rmax is not None:
                    expected_reps = f"{rmin}-{rmax}" if rmin != rmax else f"{rmin}"
                elif rmin is not None:
                    expected_reps = f"{rmin}"
                elif rmax is not None:
                    expected_reps = f"{rmax}"
            except Exception:
                expected_reps = None

        planned_sets = row.get("sets")
        planned_rpe_min = row.get("rpe_min")
        planned_rpe_max = row.get("rpe_max")
        final_sets = planned_sets
        final_rpe_min = planned_rpe_min
        final_rpe_max = planned_rpe_max

        if (mode or "").strip().lower() == "light":
            def _clamp_rpe(val, lo=6.0, hi=8.5):
                try:
                    return max(lo, min(hi, float(val)))
                except Exception:
                    return None
            # Light should mostly preserve load and intent; trim fatigue via a small
            # set reduction and at most a mild RPE downshift.
            if planned_rpe_min is not None:
                final_rpe_min = _clamp_rpe(float(planned_rpe_min) - 0.5, 6.0, 9.0)
            if planned_rpe_max is not None:
                final_rpe_max = _clamp_rpe(float(planned_rpe_max) - 0.5, 6.5, 9.5)
            if final_rpe_min is not None and final_rpe_max is not None and final_rpe_min > final_rpe_max:
                final_rpe_min = final_rpe_max

            # Reduce volume conservatively: compounds usually stay intact unless the
            # plan is already high-volume; accessories can lose one set.
            if isinstance(planned_sets, int):
                if planned_sets >= 4:
                    final_sets = max(planned_sets - 1, 1)
                elif slot >= 2 and planned_sets >= 3:
                    final_sets = planned_sets - 1

        exercises.append({
            "exercise_key": _exercise_key(name, variation, device, slot),
            "name": name,
            "variation": variation,
            "device": device,
            "slot": slot,
            "is_optional": bool(row.get("optional") or row.get("is_optional")),
            "tags": ["light"] if (mode or "").strip().lower() == "light" else [],
            "planned_sets": planned_sets,
            "final_sets": final_sets,
            "expected_reps": expected_reps,
            "planned_rpe_min": planned_rpe_min,
            "planned_rpe_max": planned_rpe_max,
            "final_rpe_min": final_rpe_min,
            "final_rpe_max": final_rpe_max,
            "notes": row.get("notes"),
        })

    return {
        "session_key": session_key,
        "planned_label": planned_label,
        "final_label": final_label,
        "source": {
            "plan_id": plan_id,
            "day_id": day_id,
            "reason": None,
        },
        "exercises": exercises,
    }
