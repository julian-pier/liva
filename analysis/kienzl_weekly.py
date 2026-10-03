from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlite3

from kienzl import kienzl
from database.connections import get_nutrition_db, get_training_db
from ai.env import get_openai_api_key_info


def _to_float(x):
    try:
        if x is None:
            return None
        v = float(x)
    except Exception:
        return None
    return v


def _e1rm(weight_kg: float | None, reps: int | None) -> float | None:
    if weight_kg is None or reps is None:
        return None
    try:
        if weight_kg <= 0 or reps <= 0:
            return None
        return float(weight_kg) * (1.0 + (float(reps) / 30.0))
    except Exception:
        return None


def _mean(xs: list[float]) -> float | None:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float | None:
    xs = [float(x) for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def _median(xs: list[float]) -> float | None:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    xs.sort()
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def _fmt_weight(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if v.is_integer():
        return str(int(v))
    s = f"{v:.1f}"
    return s.rstrip("0").rstrip(".")


def _fmt_set_example(reps: int | None, weight_kg: float | None) -> str | None:
    if reps is None or weight_kg is None:
        return None
    w = _fmt_weight(weight_kg)
    if not w:
        return None
    try:
        r = int(reps)
    except Exception:
        return None
    if r <= 0:
        return None
    return f"{r}x{w}"


@dataclass
class _ExerciseAgg:
    name: str
    device: str | None
    laterality: str | None
    sessions: set[int]
    sets: int
    tonnage_kg: float
    first_date: str | None
    last_date: str | None
    top_sets: list[dict]


def _parse_hrv_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
    except Exception:
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except Exception:
        return None


def _nutrition_targets(*, tz_name: str, end_date: date) -> dict:
    """
    Reads targets from nutrition_settings if available, else falls back to defaults.
    Defaults per request: kcal=3800, protein=150, kcal_band=300.
    """
    default = {"kcal": 3800, "protein_g": 150, "kcal_band": 300, "source": "default"}
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT key, value FROM nutrition_settings").fetchall()
    except Exception:
        conn.close()
        return default
    finally:
        try:
            conn.close()
        except Exception:
            pass

    settings = {str(r["key"]): (r["value"] if r["value"] is not None else "") for r in rows}
    active_mode = str(settings.get("active_mode") or settings.get("selected_mode") or "").strip()

    kcal_target = None
    if active_mode:
        for k in (f"mode_{active_mode}_target", f"target_{active_mode}"):
            if k in settings and str(settings.get(k)).strip():
                kcal_target = settings.get(k)
                break
    if kcal_target is None:
        # legacy key observed: target_lean_bulk
        kcal_target = settings.get("target_lean_bulk") or settings.get("mode_lean_bulk_target")

    kcal_band = None
    if active_mode:
        kcal_band = settings.get(f"mode_{active_mode}_yellow_tol") or settings.get(f"mode_{active_mode}_green_tol")
    if kcal_band is None:
        kcal_band = settings.get("mode_lean_bulk_yellow_tol") or settings.get("mode_lean_bulk_green_tol")

    protein_target = None
    # If you ever store explicit protein targets, pick them up here.
    if active_mode:
        for k in (
            f"mode_{active_mode}_protein_target",
            f"mode_{active_mode}_protein_g",
            f"mode_{active_mode}_protein",
        ):
            if k in settings and str(settings.get(k)).strip():
                protein_target = settings.get(k)
                break
    if protein_target is None:
        for k in ("protein_target_g", "protein_target", "target_protein_g", "target_protein"):
            if k in settings and str(settings.get(k)).strip():
                protein_target = settings.get(k)
                break

    def to_int(v):
        try:
            return int(round(float(v)))
        except Exception:
            return None

    out = default.copy()
    any_from_settings = False
    kt = to_int(kcal_target)
    if kt:
        out["kcal"] = kt
        any_from_settings = True
    kb = to_int(kcal_band)
    if kb:
        out["kcal_band"] = kb
        any_from_settings = True

    pt = to_int(protein_target)
    if pt and pt > 0:
        out["protein_g"] = pt
        any_from_settings = True

    out["source"] = ("config" if any_from_settings else "default")
    return out


def compute_kienzl_weekly_input(*, days: int = 90, tz_name: str = "Europe/Berlin", week_end: date | None = None) -> dict:
    tz = ZoneInfo(tz_name)
    end_d = week_end or datetime.now(tz).date()
    start_d = end_d - timedelta(days=max(1, int(days)) - 1)

    start_iso = start_d.isoformat()
    end_iso = end_d.isoformat()

    # -----------------------------
    # Gym (workouts/sets)
    # -----------------------------
    ex_map: dict[tuple[str, str | None, str | None], _ExerciseAgg] = {}

    tr = get_training_db()
    tr.row_factory = sqlite3.Row
    rows = tr.execute(
        """
        SELECT
          w.id AS workout_id,
          w.date_iso AS date_iso,
          e.name AS exercise,
          e.device AS device,
          e.laterality AS laterality,
          s.id AS set_id,
          s.set_number AS set_number,
          s.reps AS reps,
          s.weight AS weight,
          s.rpe AS rpe
        FROM sets s
        JOIN exercises e ON e.id = s.exercise_id
        JOIN workouts w ON w.id = e.workout_id
        WHERE w.date_iso >= ? AND w.date_iso <= ?
          AND e.name IS NOT NULL AND TRIM(e.name) != ''
        ORDER BY w.date_iso ASC, w.id ASC, e.id ASC, COALESCE(s.set_number, 0) ASC, s.id ASC
        """,
        (start_iso, end_iso),
    ).fetchall()
    tr.close()

    # top set per (workout_id, exercise, device, laterality)
    best_by_session: dict[tuple[int, str, str | None, str | None], dict] = {}
    rpe_max_by_session: dict[tuple[int, str, str | None, str | None], float] = {}
    rpe_max_by_workout: dict[int, float] = {}
    last_rpe_by_exercise: dict[tuple[str, str | None, str | None], float] = {}

    for r in rows:
        ex = (r["exercise"] or "").strip()
        if not ex:
            continue
        dev = (r["device"] or None)
        lat = (r["laterality"] or None)
        key = (ex, dev, lat)
        if key not in ex_map:
            ex_map[key] = _ExerciseAgg(
                name=ex,
                device=dev,
                laterality=lat,
                sessions=set(),
                sets=0,
                tonnage_kg=0.0,
                first_date=None,
                last_date=None,
                top_sets=[],
            )

        agg = ex_map[key]
        workout_id = int(r["workout_id"])
        date_iso = (r["date_iso"] or "").strip()
        agg.sessions.add(workout_id)
        agg.sets += 1

        w = _to_float(r["weight"])
        reps = int(r["reps"]) if r["reps"] is not None else None
        if w is not None and reps is not None:
            agg.tonnage_kg += float(w) * float(reps)

        if date_iso:
            if agg.first_date is None or date_iso < agg.first_date:
                agg.first_date = date_iso
            if agg.last_date is None or date_iso > agg.last_date:
                agg.last_date = date_iso

        ses_key = (workout_id, ex, dev, lat)

        rpe_v = _to_float(r["rpe"])
        if rpe_v is not None:
            prev_rpe = rpe_max_by_session.get(ses_key)
            if prev_rpe is None or rpe_v > prev_rpe:
                rpe_max_by_session[ses_key] = rpe_v
            prev_w = rpe_max_by_workout.get(workout_id)
            if prev_w is None or rpe_v > prev_w:
                rpe_max_by_workout[workout_id] = rpe_v
            last_rpe_by_exercise[(ex, dev, lat)] = rpe_v

        e1 = _e1rm(w, reps)
        score = e1 if e1 is not None else (w if w is not None else 0.0)
        cur = best_by_session.get(ses_key)
        if cur is None or score > float(cur.get("_score") or 0.0):
            best_by_session[ses_key] = {
                "_score": score,
                "date": date_iso,
                "workout_id": workout_id,
                "set_id": int(r["set_id"]) if r["set_id"] is not None else None,
                "set_index": int(r["set_number"]) if r["set_number"] is not None else None,
                "weight_kg": w,
                "reps": reps,
                "rpe": rpe_v,
                "rpe_raw": rpe_v,
                "rpe_source": "set" if rpe_v is not None else None,
                "e1rm": e1,
            }

    for (workout_id, ex, dev, lat), top in best_by_session.items():
        if top.get("rpe") is None:
            rpe_best = rpe_max_by_session.get((workout_id, ex, dev, lat))
            if rpe_best is not None:
                top["rpe"] = rpe_best
                top["rpe_source"] = "exercise_session_max"
            else:
                rpe_w = rpe_max_by_workout.get(int(workout_id))
                if rpe_w is not None:
                    top["rpe"] = rpe_w
                    top["rpe_source"] = "workout_max"
                else:
                    rpe_last = last_rpe_by_exercise.get((ex, dev, lat))
                    if rpe_last is not None:
                        top["rpe"] = rpe_last
                        top["rpe_source"] = "exercise_last_seen"
        agg = ex_map.get((ex, dev, lat))
        if not agg:
            continue
        agg.top_sets.append({k: v for k, v in top.items() if not k.startswith("_")})

    # finalize exercise list (internal details; do NOT expose full top sets in payload)
    exercises = []
    for agg in ex_map.values():
        agg.top_sets.sort(key=lambda x: (x.get("date") or "", x.get("workout_id") or 0, x.get("set_id") or 0))
        last5 = agg.top_sets[-5:] if agg.top_sets else []
        first5 = agg.top_sets[:5] if agg.top_sets else []

        last_e1rms = [x.get("e1rm") for x in last5 if x.get("e1rm") is not None]
        first_e1rms = [x.get("e1rm") for x in first5 if x.get("e1rm") is not None]

        score_recent = None
        if last_e1rms and first_e1rms:
            m_last = _median(last_e1rms)
            m_first = _median(first_e1rms)
            if m_last is not None and m_first is not None:
                score_recent = float(m_last - m_first)

        exercises.append(
            {
                "exercise": agg.name,
                "device": agg.device,
                "laterality": agg.laterality,
                "sessions": len(agg.sessions),
                "sets": agg.sets,
                "tonnage_kg": round(float(agg.tonnage_kg), 1),
                "first_date": agg.first_date,
                "last_date": agg.last_date,
                "_first5": first5,
                "_last5": last5,
                "_score_recent": score_recent,
            }
        )

    # keep payload compact: select most relevant
    exercises.sort(key=lambda x: (-int(x.get("sessions") or 0), -int(x.get("sets") or 0), str(x.get("exercise") or "").lower()))
    top_exercises = exercises[:18]

    # -----------------------------
    # Nutrition + weight (nutrition db)
    # -----------------------------
    nut = get_nutrition_db()
    nut.row_factory = sqlite3.Row

    nut_rows = nut.execute(
        """
        SELECT date_iso, kcal, protein, carbs, fat
        FROM nutrition_daily
        WHERE date_iso >= ? AND date_iso <= ?
        ORDER BY date_iso ASC
        """,
        (start_iso, end_iso),
    ).fetchall()

    weight_rows = nut.execute(
        """
        SELECT date_iso, weight_kg
        FROM weight_logs
        WHERE date_iso >= ? AND date_iso <= ?
        ORDER BY date_iso ASC, id ASC
        """,
        (start_iso, end_iso),
    ).fetchall()
    nut.close()

    kcal = [r["kcal"] for r in nut_rows if r["kcal"] is not None]
    protein = [r["protein"] for r in nut_rows if r["protein"] is not None]
    carbs = [r["carbs"] for r in nut_rows if r["carbs"] is not None]
    fat = [r["fat"] for r in nut_rows if r["fat"] is not None]

    w_kg = [r["weight_kg"] for r in weight_rows if r["weight_kg"] is not None]

    # recent window
    recent_start = (end_d - timedelta(days=13)).isoformat()
    kcal_14 = [r["kcal"] for r in nut_rows if (r["kcal"] is not None and (r["date_iso"] or "") >= recent_start)]
    protein_14 = [r["protein"] for r in nut_rows if (r["protein"] is not None and (r["date_iso"] or "") >= recent_start)]

    targets = _nutrition_targets(tz_name=tz_name, end_date=end_d)
    kcal_target = int(targets["kcal"])
    protein_target_g = int(targets["protein_g"])
    kcal_band = int(targets["kcal_band"])
    target_source = str(targets.get("source") or "default")

    nutrition_summary = {
        "days_logged": len(nut_rows),
        "avg_kcal": int(round((_mean(kcal) or 0) / 10.0) * 10) if _mean(kcal) is not None else None,
        "std_kcal": int(round((_std(kcal) or 0) / 10.0) * 10) if _std(kcal) is not None else None,
        "avg_protein_g": int(round(_mean(protein))) if _mean(protein) is not None else None,
        "std_protein_g": int(round(_std(protein))) if _std(protein) is not None else None,
        "avg_carbs_g": round(_mean(carbs), 1) if _mean(carbs) is not None else None,
        "avg_fat_g": round(_mean(fat), 1) if _mean(fat) is not None else None,
        "avg_kcal_14d": int(round((_mean(kcal_14) or 0) / 10.0) * 10) if _mean(kcal_14) is not None else None,
        "avg_protein_14d_g": int(round(_mean(protein_14))) if _mean(protein_14) is not None else None,
    }

    avg_kcal = nutrition_summary.get("avg_kcal")
    std_kcal = nutrition_summary.get("std_kcal")
    avg_prot = nutrition_summary.get("avg_protein_g")
    std_prot = nutrition_summary.get("std_protein_g")
    avg_prot_14 = nutrition_summary.get("avg_protein_14d_g")

    kcal_cv = (round(float(std_kcal) / float(avg_kcal), 3) if (avg_kcal and std_kcal and avg_kcal > 0) else None)
    protein_cv = (round(float(std_prot) / float(avg_prot), 3) if (avg_prot and std_prot and avg_prot > 0) else None)

    # adherence counts
    kcal_days = 0
    kcal_in_band = 0
    protein_days = 0
    protein_hit = 0
    for r in nut_rows:
        if r["kcal"] is not None:
            kcal_days += 1
            try:
                if abs(float(r["kcal"]) - float(kcal_target)) <= float(kcal_band):
                    kcal_in_band += 1
            except Exception:
                pass
        if r["protein"] is not None:
            protein_days += 1
            try:
                if float(r["protein"]) >= float(protein_target_g):
                    protein_hit += 1
            except Exception:
                pass

    protein_days_hit_target_pct = (round((protein_hit / protein_days) * 100.0, 1) if protein_days > 0 else None)
    kcal_days_in_band_pct = (round((kcal_in_band / kcal_days) * 100.0, 1) if kcal_days > 0 else None)
    protein_days_missed = int(protein_days - protein_hit) if protein_days > 0 else None
    kcal_days_out_of_band = int(kcal_days - kcal_in_band) if kcal_days > 0 else None

    # flags (consistency + under target)
    kcal_consistent = bool((kcal_cv is not None and kcal_cv <= 0.06) or (std_kcal is not None and std_kcal <= 200))
    protein_consistent = bool((protein_cv is not None and protein_cv <= 0.10) or (std_prot is not None and std_prot <= 15))
    protein_14d_drop = bool((avg_prot_14 is not None and avg_prot is not None) and (avg_prot_14 <= (avg_prot - 10)))

    kcal_under_target = bool(avg_kcal is not None and avg_kcal < (kcal_target - kcal_band))
    protein_under_target = bool(avg_prot is not None and avg_prot < protein_target_g)

    nutrition_flags = {
        "kcal_consistent": bool(kcal_consistent),
        "protein_consistent": bool(protein_consistent),
        "protein_14d_drop": bool(protein_14d_drop),
        "kcal_under_target": bool(kcal_under_target),
        "protein_under_target": bool(protein_under_target),
    }

    weight_summary = {
        "days_logged": len(weight_rows),
        "start_kg": _to_float(w_kg[0]) if w_kg else None,
        "end_kg": _to_float(w_kg[-1]) if w_kg else None,
        "delta_kg": round(float(_to_float(w_kg[-1]) - _to_float(w_kg[0])), 2) if len(w_kg) >= 2 else None,
    }

    # -----------------------------
    # Recovery (RMSSD + BPM)
    # -----------------------------
    rmssd_7 = None
    rmssd_28 = None
    rmssd_delta_pct = None
    bpm_7 = None
    bpm_28 = None
    bpm_delta = None
    rec_missing = True

    try:
        from database.connections import get_hrv_db

        hrv = get_hrv_db()
        hrv.row_factory = sqlite3.Row
        start_28 = (end_d - timedelta(days=27)).isoformat()
        h_rows = hrv.execute(
            """
            SELECT ts_measurement, rmssd, hr
            FROM hrv_measurements
            WHERE ts_measurement IS NOT NULL
              AND SUBSTR(ts_measurement, 1, 10) >= ?
              AND SUBSTR(ts_measurement, 1, 10) <= ?
            ORDER BY ts_measurement ASC
            """,
            (start_28, end_iso),
        ).fetchall()
        hrv.close()

        rmssd_vals_7 = []
        rmssd_vals_28 = []
        bpm_vals_7 = []
        bpm_vals_28 = []

        start_7 = end_d - timedelta(days=6)
        for r in h_rows:
            dt = _parse_hrv_ts(r["ts_measurement"])
            if not dt:
                continue
            d_local = dt.astimezone(tz).date()
            if not (end_d - timedelta(days=27) <= d_local <= end_d):
                continue
            rv = _to_float(r["rmssd"])
            hv = _to_float(r["hr"])
            if rv is not None:
                rmssd_vals_28.append(rv)
                if d_local >= start_7:
                    rmssd_vals_7.append(rv)
            if hv is not None:
                bpm_vals_28.append(hv)
                if d_local >= start_7:
                    bpm_vals_7.append(hv)

        if len(rmssd_vals_7) >= 4 and len(rmssd_vals_28) >= 10 and len(bpm_vals_7) >= 4 and len(bpm_vals_28) >= 10:
            rec_missing = False
            rmssd_7 = round(float(_mean(rmssd_vals_7)), 1) if _mean(rmssd_vals_7) is not None else None
            rmssd_28 = round(float(_mean(rmssd_vals_28)), 1) if _mean(rmssd_vals_28) is not None else None
            bpm_7 = round(float(_mean(bpm_vals_7)), 1) if _mean(bpm_vals_7) is not None else None
            bpm_28 = round(float(_mean(bpm_vals_28)), 1) if _mean(bpm_vals_28) is not None else None
            if rmssd_7 is not None and rmssd_28 and rmssd_28 != 0:
                rmssd_delta_pct = round(((rmssd_7 - rmssd_28) / rmssd_28) * 100.0, 1)
            if bpm_7 is not None and bpm_28 is not None:
                bpm_delta = round((bpm_7 - bpm_28), 1)
    except Exception:
        rec_missing = True

    rmssd_down = bool(rmssd_delta_pct is not None and rmssd_delta_pct <= -8.0)
    bpm_up = bool(bpm_delta is not None and bpm_delta >= 3.0)
    recovery_ok = bool((not rec_missing) and (not rmssd_down) and (not bpm_up))

    recovery = {
        "rmssd_7d_avg": rmssd_7,
        "rmssd_28d_avg": rmssd_28,
        "rmssd_delta_pct": rmssd_delta_pct,
        "bpm_7d_avg": bpm_7,
        "bpm_28d_avg": bpm_28,
        "bpm_delta": bpm_delta,
        "flags": {
            "rmssd_down": bool(rmssd_down),
            "bpm_up": bool(bpm_up),
            "recovery_ok": bool(recovery_ok),
            "recovery_missing": bool(rec_missing),
        },
    }

    # -----------------------------
    # Gym compact selection
    # -----------------------------
    candidates = [e for e in top_exercises if int(e.get("sessions") or 0) >= 6]

    def ex_key(e: dict) -> tuple[str, str | None, str | None]:
        return (str(e.get("exercise") or ""), e.get("device") or None, e.get("laterality") or None)

    def score_of(e: dict) -> float:
        s = e.get("_score_recent")
        try:
            return float(s)
        except Exception:
            return 0.0

    prefer_names = {"bench", "ohp", "latzug", "rdls", "squat", "deadlift"}

    anchor = None
    prefer = [e for e in candidates if str(e.get("exercise") or "").strip().lower() in prefer_names]
    if prefer:
        prefer.sort(key=lambda x: (-int(x.get("sessions") or 0), -int(x.get("sets") or 0), -float(x.get("tonnage_kg") or 0.0)))
        anchor = prefer[0]
    elif candidates:
        candidates_sorted = sorted(candidates, key=lambda x: (-score_of(x), -int(x.get("sessions") or 0), -int(x.get("sets") or 0)))
        anchor = candidates_sorted[0]

    problem = None
    if candidates:
        problem = sorted(candidates, key=lambda x: (score_of(x), -int(x.get("sessions") or 0)))[0]

    second = None
    remaining = [e for e in candidates if e is not anchor and e is not problem]
    if remaining:
        remaining.sort(key=lambda x: (-float(x.get("tonnage_kg") or 0.0), -int(x.get("sessions") or 0)))
        second = remaining[0]

    def signal_for_score(s: float) -> str:
        if s < -1.0:
            return "recent_drop"
        if s > 1.0:
            return "improving"
        if abs(s) < 0.5:
            return "stalled"
        return "stable"

    def display_ex(e: dict | None) -> str:
        if not e:
            return "—"
        name = str(e.get("exercise") or "").strip() or "—"
        dev = (e.get("device") or "").strip()
        return f"{name} ({dev or '—'})"

    def coach_exercise_name(e: dict | None) -> str:
        if not e:
            return "—"
        name = str(e.get("exercise") or "").strip() or "—"
        dev = str((e.get("device") or "")).strip()
        if dev:
            suffix = f" ({dev})"
            if name.lower().endswith(suffix.lower()):
                name = name[: -len(suffix)].rstrip()
        # Safety: keep coach names device-free if the name itself includes a known device suffix.
        m = re.match(r"^(.*)\\s\\(([^)]+)\\)$", name)
        if m:
            token = (m.group(2) or "").strip().lower()
            known = {
                "lh",
                "sz",
                "kh",
                "egym",
                "kabel",
                "maschine",
                "smith",
                "bodyweight",
                "bw",
            }
            if token in known:
                name = (m.group(1) or "").strip() or name
        return name or "—"

    def best_set_from_last5(e: dict | None) -> dict | None:
        if not e:
            return None
        last5 = e.get("_last5") or []
        if not isinstance(last5, list) or not last5:
            return None
        best = None
        best_score = None
        for s in last5:
            if not isinstance(s, dict):
                continue
            e1 = _to_float(s.get("e1rm"))
            w = _to_float(s.get("weight_kg"))
            r = s.get("reps")
            try:
                rr = int(r) if r is not None else None
            except Exception:
                rr = None
            fallback = (w * rr) if (w is not None and rr is not None) else None
            sc = e1 if e1 is not None else fallback
            if sc is None:
                continue
            if best is None or best_score is None or float(sc) > float(best_score):
                best = s
                best_score = sc
        return best

    ex1 = best_set_from_last5(anchor)
    ex2_base = second if second is not None else problem
    ex2 = best_set_from_last5(ex2_base)

    set_examples = []
    if ex1:
        set_examples.append(_fmt_set_example(ex1.get("reps"), ex1.get("weight_kg")) or "—")
    else:
        set_examples.append("—")
    if ex2:
        set_examples.append(_fmt_set_example(ex2.get("reps"), ex2.get("weight_kg")) or "—")
    else:
        set_examples.append("—")

    def brief_obj(e: dict | None, *, include_signal: bool = False) -> dict | None:
        if not e:
            return None
        out = {
            "exercise": str(e.get("exercise") or "").strip() or None,
            "device": (e.get("device") or None),
            "laterality": (e.get("laterality") or None),
            "sessions": int(e.get("sessions") or 0),
            "sets": int(e.get("sets") or 0),
            "tonnage_kg": float(e.get("tonnage_kg") or 0.0),
        }
        if include_signal:
            out["signal"] = signal_for_score(score_of(e))
        return out

    gym = {
        "mention_exercises": [
            coach_exercise_name(anchor),
            coach_exercise_name(problem),
            coach_exercise_name(second),
        ],
        "mention_exercises_ui": [display_ex(anchor), display_ex(problem), display_ex(second)],
        "set_examples": set_examples[:2],
        "counts": {"exercise_count": int(len(exercises)), "set_rows": int(len(rows))},
        "anchor": brief_obj(anchor),
        "second": brief_obj(second),
        "problem": brief_obj(problem, include_signal=True),
    }

    # -----------------------------
    # Weight compact
    # -----------------------------
    start_w = _to_float(weight_summary.get("start_kg"))
    end_w = _to_float(weight_summary.get("end_kg"))
    span = None
    if start_w is not None and end_w is not None:
        span = f"{start_w:.1f} -> {end_w:.1f}"

    weight = {
        "span": span,
        "delta_kg": (round(float(weight_summary.get("delta_kg")), 1) if weight_summary.get("delta_kg") is not None else None),
        "days_logged": int(weight_summary.get("days_logged") or 0),
        "start_kg": (round(start_w, 1) if start_w is not None else None),
        "end_kg": (round(end_w, 1) if end_w is not None else None),
    }

    nutrition = {
        "targets": {"kcal": int(kcal_target), "protein_g": int(protein_target_g), "kcal_band": int(kcal_band), "source": target_source},
        "coverage": {
            "days_logged": int(nutrition_summary.get("days_logged") or 0),
            "days_with_kcal": int(kcal_days),
            "days_with_protein": int(protein_days),
        },
        "summary": {
            "kcal_avg": nutrition_summary.get("avg_kcal"),
            "kcal_std": nutrition_summary.get("std_kcal"),
            "kcal_cv": kcal_cv,
            "protein_avg_g": nutrition_summary.get("avg_protein_g"),
            "protein_std_g": nutrition_summary.get("std_protein_g"),
            "protein_cv": protein_cv,
            "protein_14d_avg_g": nutrition_summary.get("avg_protein_14d_g"),
        },
        "adherence": {
            "protein_days_hit_target_pct": protein_days_hit_target_pct,
            "kcal_days_in_band_pct": kcal_days_in_band_pct,
            "protein_days_missed": protein_days_missed,
            "kcal_days_out_of_band": kcal_days_out_of_band,
        },
        "flags": nutrition_flags,
    }

    return {
        "week_end": end_iso,
        "range_start": start_iso,
        "range_end": end_iso,
        "range_days": int(days),
        "tz": tz_name,
        "gym": gym,
        "nutrition": nutrition,
        "recovery": recovery,
        "weight": weight,
    }


def sanity_check_weekly_payload(payload: dict) -> None:
    assert isinstance(payload, dict)
    assert isinstance(payload.get("tz"), str)
    assert isinstance(payload.get("week_end"), str)
    assert isinstance(payload.get("range_start"), str)
    assert isinstance(payload.get("range_end"), str)
    assert isinstance(payload.get("range_days"), int)

    gym = payload.get("gym") or {}
    assert isinstance(gym.get("mention_exercises"), list) and len(gym.get("mention_exercises")) == 3
    for s in gym.get("mention_exercises"):
        assert isinstance(s, str)
        assert "(" not in s and ")" not in s
    assert isinstance(gym.get("mention_exercises_ui"), list) and len(gym.get("mention_exercises_ui")) == 3
    for s in gym.get("mention_exercises_ui"):
        assert isinstance(s, str)
        if s != "—":
            assert "(" in s and ")" in s
    assert isinstance(gym.get("set_examples"), list) and len(gym.get("set_examples")) == 2
    for s in gym.get("set_examples"):
        assert isinstance(s, str)
        if s != "—":
            assert re.match(r"^\d+x\d+(\.\d+)?$", s)

    raw = json.dumps(payload, ensure_ascii=False)
    assert "top_sets_first5" not in raw
    assert "top_sets_last5" not in raw

    nutrition = payload.get("nutrition") or {}
    targets = nutrition.get("targets") or {}
    assert isinstance(targets.get("kcal"), int)
    assert isinstance(targets.get("protein_g"), int)
    assert isinstance(targets.get("kcal_band"), int)
    assert targets.get("source") in ("config", "default")

    adherence = nutrition.get("adherence") or {}
    for k in ("protein_days_hit_target_pct", "kcal_days_in_band_pct"):
        v = adherence.get(k)
        assert (v is None) or (isinstance(v, (int, float)) and 0.0 <= float(v) <= 100.0)
    for k in ("protein_days_missed", "kcal_days_out_of_band"):
        v = adherence.get(k)
        assert (v is None) or (isinstance(v, int) and v >= 0)

    coverage = nutrition.get("coverage") or {}
    days_logged = int(coverage.get("days_logged") or 0)
    days_with_protein = int(coverage.get("days_with_protein") or 0)
    days_with_kcal = int(coverage.get("days_with_kcal") or 0)
    assert 0 <= days_with_protein <= days_logged
    assert 0 <= days_with_kcal <= days_logged
    if adherence.get("protein_days_missed") is not None:
        assert adherence.get("protein_days_missed") <= days_with_protein
    if adherence.get("kcal_days_out_of_band") is not None:
        assert adherence.get("kcal_days_out_of_band") <= days_with_kcal
    assert isinstance((nutrition.get("flags") or {}), dict)

    recovery = payload.get("recovery") or {}
    assert isinstance((recovery.get("flags") or {}), dict)



def upsert_kienzl_weekly_analyse(
    *,
    week_end: str,
    range_start: str,
    range_end: str,
    text: str,
    input_payload: dict,
    model: str | None,
    created_at: str,
) -> None:
    conn = get_training_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO kienzl_analyse_weekly (week_end, range_start, range_end, text, input_json, model, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(week_end) DO UPDATE SET
          range_start=excluded.range_start,
          range_end=excluded.range_end,
          text=excluded.text,
          input_json=excluded.input_json,
          model=excluded.model,
          created_at=excluded.created_at
        """,
        (
            week_end,
            range_start,
            range_end,
            (text or "").strip(),
            json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")),
            model,
            created_at,
        ),
    )
    conn.commit()
    conn.close()


def get_latest_kienzl_weekly_analyse() -> dict | None:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT week_end, range_start, range_end, text, model, created_at
        FROM kienzl_analyse_weekly
        ORDER BY created_at DESC, week_end DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def refresh_kienzl_weekly_analyse(*, days: int = 90, tz_name: str = "Europe/Berlin") -> dict:
    """
    Builds payload (gym + nutrition), calls OpenAI (if available) and stores the weekly text.
    Returns the stored row (latest).
    """
    kienzl.ensure_kienzl_schema_and_seed()

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    week_end_date = now.date()
    week_end = week_end_date.isoformat()

    payload = compute_kienzl_weekly_input(days=days, tz_name=tz_name, week_end=week_end_date)
    sanity_check_weekly_payload(payload)

    def _sanitize_openai_error(msg: str) -> str:
        # Don't leak key-like strings (even masked snippets).
        s = (msg or "").strip()
        s = re.sub(r"sk-[^\\s\"']+", "sk-***", s)
        s = re.sub(r"https?://\\S+", "<url>", s)
        return s[:400]

    def _friendly_openai_error(msg: str) -> str:
        m = _sanitize_openai_error(msg)
        if "openai_http_error:401" in m or "Incorrect API key" in m or "invalid_api_key" in m:
            return "OpenAI Auth-Fehler (API Key falsch/abgelaufen)."
        if "openai_http_error:429" in m:
            return "OpenAI Rate-Limit/Quota (429)."
        if "openai_http_error:5" in m:
            return "OpenAI Serverfehler (5xx)."
        return f"OpenAI Fehler: {m}"

    text = ""
    model = None
    openai_ok = True
    openai_error = None
    key_info = get_openai_api_key_info()
    key_source = key_info.get("source") or "none"
    key_fp = key_info.get("fingerprint") or None
    try:
        from ai.kienzl_weekly_client import generate_kienzl_weekly_text

        text, model = generate_kienzl_weekly_text(payload=payload)
    except Exception as e:
        openai_ok = False
        raw = _sanitize_openai_error(str(e) or "unknown")
        openai_error = _friendly_openai_error(raw)
        # keep it visible in UI; no hard fail
        if "OPENAI_API_KEY" in raw or "api_key" in raw:
            text = "KIENZL: Weekly Analyse nicht verfügbar (OPENAI_API_KEY fehlt)."
        else:
            text = f"KIENZL: Weekly Analyse nicht verfügbar ({openai_error})."
        model = None

    created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    upsert_kienzl_weekly_analyse(
        week_end=week_end,
        range_start=str(payload.get("range_start") or ""),
        range_end=str(payload.get("range_end") or ""),
        text=text,
        input_payload=payload,
        model=model,
        created_at=created_at,
    )

    row = get_latest_kienzl_weekly_analyse() or {
        "week_end": week_end,
        "range_start": payload["range"]["start_date"],
        "range_end": payload["range"]["end_date"],
        "text": text,
        "model": model,
        "created_at": created_at,
    }
    row["openai_ok"] = bool(openai_ok and model)
    row["openai_error"] = openai_error if not row["openai_ok"] else None
    row["openai_key_source"] = key_source
    row["openai_key_fingerprint"] = key_fp
    return row
