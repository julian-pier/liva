from __future__ import annotations

from datetime import timedelta, date, datetime, time
import math
from collections import Counter, deque
from statistics import mean, median
import sqlite3
from zoneinfo import ZoneInfo

from analysis.progression_rules import (
    DEFAULT_CONFIG,
    RULE_VERSION,
    ProgressRuleConfig,
    SetPerformance,
    choose_best_reference_candidate,
    choose_best_set,
    calculate_e1rm,
    classify_top_backoff_slots,
    compare_set_progress,
    find_reference_set,
    normalize_set_performance,
    summarize_session_progress,
)
from analysis.training_progress import compute_progress_series as compute_canonical_progress_series

# fallback muscle weights for coarse pattern matches (used by resolve_muscles in app.py)
PATTERN_TO_MUSCLES = {
    "legs": {"quads": 1.0, "glutes": 0.6, "hamstrings": 0.5, "calves": 0.25},
    "chest": {"chest": 1.0, "triceps": 0.3, "front_delts": 0.2},
    "back": {"upper_back": 1.0, "lats": 0.8, "biceps": 0.3, "rear_delts": 0.2},
    "shoulders_arms": {"front_delts": 0.5, "side_delts": 0.7, "rear_delts": 0.5, "biceps": 0.4, "triceps": 0.4},
    "core": {"core": 1.0},
}

# ============================================================
# SUPERCOMP V3 (readiness% vs baseline)
# ============================================================

SUPERCOMP_PATTERNS_MAIN = ["chest", "back", "legs", "shoulders_arms"]

# Rolling windows
SUPERCOMP_LOAD_BASELINE_WINDOW_DAYS = 90
SUPERCOMP_READINESS_BASELINE_WINDOW_DAYS = 28
SUPERCOMP_ANCHOR_WINDOW_DAYS = 21

# Percent output smoothing (keep drops)
SUPERCOMP_READINESS_SMOOTH_SPAN_DAYS = 3

# Outlier clamp for session stimulus
SUPERCOMP_STIMULUS_CLAMP_PCTL = 0.95
SUPERCOMP_STIMULUS_CLAMP_MULT = 1.5
SUPERCOMP_STIMULUS_CLAMP_MULT_SPARSE = 1.25

# Burn-in init
SUPERCOMP_BURNIN_DAYS = 0

# No readiness guards/caps here -> frontend handles y-range via percentiles.

# Default dynamics per pattern (requested start values)
SUPERCOMP_PARAMS: dict[str, dict] = {
    "chest": {"fatigue_half_life_days": 3.0, "fitness_half_life_days": 18.0, "k_fit": 0.08, "k_fat": 0.24},
    "back": {"fatigue_half_life_days": 3.0, "fitness_half_life_days": 18.0, "k_fit": 0.08, "k_fat": 0.24},
    "shoulders_arms": {"fatigue_half_life_days": 2.5, "fitness_half_life_days": 16.0, "k_fit": 0.075, "k_fat": 0.225},
    "legs": {"fatigue_half_life_days": 4.0, "fitness_half_life_days": 20.0, "k_fit": 0.085, "k_fat": 0.27},
}


# ============================================================
# SUPERCOMP V4 (anchor-index, no baselines/percent)
# ============================================================

SUPERCOMP_V4_PARAMS: dict[str, dict] = {
    "chest": {"fatigue_half_life_days": 3.0, "fitness_half_life_days": 20.0},
    "back": {"fatigue_half_life_days": 3.0, "fitness_half_life_days": 20.0},
    "shoulders_arms": {"fatigue_half_life_days": 2.5, "fitness_half_life_days": 18.0},
    "legs": {"fatigue_half_life_days": 4.0, "fitness_half_life_days": 22.0},
}

SUPERCOMP_V4_GAIN_SCALE = 0.06
SUPERCOMP_V4_COST_SCALE = 0.14
SUPERCOMP_V4_FATIGUE_WEIGHT = 1.0
SUPERCOMP_V4_INDEX_SMOOTH_SPAN_DAYS = 1
SUPERCOMP_V4_INDEX_SCALE = 18.0
SUPERCOMP_V4_TREND_SMOOTH_SPAN_DAYS = 14
SUPERCOMP_V4_PERF_EMA_SPAN_DAYS = 12
# progress gain multiplier for perf_delta (perf_level changes are small)
SUPERCOMP_V4_K_PROG = 2.8
SUPERCOMP_V4_K_PERF_LEVEL = 3.0
SUPERCOMP_V4_STIMULUS_CLAMP_PCTL = 0.90
SUPERCOMP_V4_STIMULUS_CLAMP_MULT = 1.4

# v4 perf-signal uses auto-selected top exercises per pattern (no manual key-lift list).


def _ema_alpha(span_days: int) -> float:
    return 2.0 / (float(max(1, int(span_days))) + 1.0)


def _percentile(values: list[float], p: float) -> float:
    """
    Deterministic percentile (linear interpolation, p in [0..1]).
    """
    vals = sorted(float(x) for x in values if x is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    pp = _clamp(float(p), 0.0, 1.0)
    idx = (len(vals) - 1) * pp
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    frac = idx - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _epley_e1rm(weight: float, reps: int) -> float:
    """
    Deterministic e1RM proxy (Epley).
    """
    w = float(weight or 0.0)
    r = int(reps or 0)
    if w <= 0 or r <= 0:
        return 0.0
    rr = max(1, min(20, r))
    return w * (1.0 + (float(rr) / 30.0))


def _robust_scale_from_values(
    values: list[float],
    baseline_fallback: float | None = None,
    min_scale: float = 1e-3,
) -> tuple[float, float, float]:
    """
    Robust scale for "normalized delta" percent:
      - scale >= min_scale
      - uses IQR, std, and max absolute deviation (prevents tiny-scale explosions on sparse data)
      - final fallback: abs(center)*0.2
    Returns (p25, p75, scale)
    """
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    if not vals:
        b = float(baseline_fallback or 1.0)
        return b, b, max(abs(b) * 0.2, min_scale)

    p25 = _percentile(vals, 0.25)
    p75 = _percentile(vals, 0.75)
    iqr = float(p75 - p25)
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / max(1, (len(vals) - 1))
    std = math.sqrt(var)
    center = float(baseline_fallback if baseline_fallback is not None else median(vals))
    max_abs_dev = max(abs(v - center) for v in vals)
    scale = max(float(iqr), float(std), float(max_abs_dev), abs(center) * 0.2, float(min_scale))
    return float(p25), float(p75), float(scale)


def _rolling_iqr_prev_days(
    dates: list[date],
    values_by_date: dict[str, float],
    window_days: int,
    min_scale: float = 1e-3,
    fallback_scale: float = 1.0,
) -> dict[str, float]:
    """
    Rolling robust scale (IQR) per day using ONLY previous days (excludes current day).
    """
    q: deque[tuple[date, float]] = deque()
    out: dict[str, float] = {}

    for d in dates:
        d_iso = d.isoformat()
        cutoff = d - timedelta(days=window_days)
        while q and q[0][0] < cutoff:
            q.popleft()

        vals = [v for _, v in q]
        _, _, scale = _robust_scale_from_values(vals, baseline_fallback=None, min_scale=min_scale)
        out[d_iso] = max(min_scale, float(scale if vals else fallback_scale))

        if d_iso in values_by_date:
            q.append((d, float(values_by_date[d_iso])))

    return out


def _rolling_median_prev_days(
    dates: list[date],
    values_by_date: dict[str, float],
    window_days: int,
    min_value: float | None = 1e-6,
) -> dict[str, float]:
    """
    Rolling median baseline per day using ONLY previous days (excludes current day).
    """
    q: deque[tuple[date, float]] = deque()
    out: dict[str, float] = {}

    for d in dates:
        d_iso = d.isoformat()
        cutoff = d - timedelta(days=window_days)
        while q and q[0][0] < cutoff:
            q.popleft()

        vals = [v for _, v in q if v is not None]
        if len(vals) >= 5:
            b = median(vals)
        elif vals:
            b = mean(vals)
        else:
            b = 1.0
        bb = float(b)
        if min_value is not None:
            bb = max(float(min_value), bb)
        out[d_iso] = bb

        if d_iso in values_by_date:
            v = float(values_by_date[d_iso])
            if v > 0:
                q.append((d, v))

    return out


def _slope_per_30_days(values: list[float | None]) -> float | None:
    """
    Simple least-squares slope over available values (index as day),
    scaled to "per 30 days". Returns None if not enough points.
    """
    xs = []
    ys = []
    for i, v in enumerate(values):
        if isinstance(v, (int, float)):
            xs.append(float(i))
            ys.append(float(v))
    if len(xs) < 10:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=False))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den <= 1e-12:
        return None
    slope_per_day = num / den
    return slope_per_day * 30.0


def compute_supercomp_v4_payload(conn: sqlite3.Connection, debug: bool = False) -> dict:
    """
    Supercomp v4:
      - One intuitive index per pattern (anchor at first stimulus day -> index=0)
      - Index is NOT percent, NOT rolling-normalized
      - Daily simulation (no "hold last")
      - Trend line is weighted avg (stimulus_sum weights)
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, date_iso FROM workouts ORDER BY date_iso ASC, id ASC")
    workouts = cur.fetchall()
    if not workouts:
        return {"x": [], "dates": [], "series": {}, "trend": {"index_smooth": []}, "meta": {"version": "supercomp_v4"}}

    patterns = ["chest", "back", "legs", "shoulders_arms"]
    start_all = date.fromisoformat(workouts[0]["date_iso"])
    last_w = max(date.fromisoformat(w["date_iso"]) for w in workouts)
    end_all = max(date.today(), last_w)

    # ------------------------------------------------------------
    # Aggregate daily raw load score per pattern (local via classify_pattern)
    # ------------------------------------------------------------
    daily_load_raw: dict[str, dict[str, float]] = {}
    daily_load_list: dict[str, list[float]] = {p: [] for p in patterns}

    for w in workouts:
        info = compute_pattern_stimulus_for_workout(int(w["id"]), conn)
        if not info:
            continue
        d_iso = info["date_iso"]
        daily_load_raw.setdefault(d_iso, {pp: 0.0 for pp in patterns})
        for p in patterns:
            score = float(info["patterns"][p]["set_score_sum"] or 0.0)
            if score > 0:
                daily_load_raw[d_iso][p] += score

    for d_iso, mp in daily_load_raw.items():
        for p in patterns:
            v = float(mp.get(p, 0.0))
            if v > 0:
                daily_load_list[p].append(v)

    # Outlier clamp threshold per pattern (daily, P95*mult)
    clamp_threshold: dict[str, float] = {}
    for p in patterns:
        vals = daily_load_list[p]
        if vals:
            clamp_threshold[p] = float(_percentile(vals, SUPERCOMP_V4_STIMULUS_CLAMP_PCTL) * SUPERCOMP_V4_STIMULUS_CLAMP_MULT)
        else:
            clamp_threshold[p] = 0.0

    daily_load_clamped: dict[str, dict[str, float]] = {d_iso: {pp: 0.0 for pp in patterns} for d_iso in daily_load_raw}
    outlier_clamped_count: dict[str, int] = {p: 0 for p in patterns}
    for d_iso, mp in daily_load_raw.items():
        for p in patterns:
            raw = float(mp.get(p, 0.0))
            if raw <= 0:
                continue
            thr = float(clamp_threshold.get(p, 0.0))
            if thr > 0 and raw > thr:
                daily_load_clamped[d_iso][p] = thr
                outlier_clamped_count[p] += 1
            else:
                daily_load_clamped[d_iso][p] = raw

    # Scale stimulus so a "typical" session ~= 1.0 (median over all time)
    load_scale: dict[str, float] = {}
    for p in patterns:
        vals = [float(v) for v in daily_load_list[p] if isinstance(v, (int, float)) and v > 0]
        load_scale[p] = max(0.1, float(median(vals)) if vals else 1.0)

    # stimulus per day (>=0)
    stimulus_by_day: dict[str, dict[str, float]] = {}
    for d in _daterange(start_all, end_all):
        d_iso = d.isoformat()
        stimulus_by_day[d_iso] = {p: 0.0 for p in patterns}
        if d_iso in daily_load_clamped:
            for p in patterns:
                raw = float(daily_load_clamped[d_iso].get(p, 0.0))
                if raw > 0:
                    stimulus_by_day[d_iso][p] = max(0.0, raw / load_scale[p])

    # first stimulus date per pattern + global start
    first_stim_date: dict[str, str | None] = {p: None for p in patterns}
    for p in patterns:
        for d in _daterange(start_all, end_all):
            d_iso = d.isoformat()
            if float(stimulus_by_day[d_iso][p]) > 0:
                first_stim_date[p] = d_iso
                break

    first_dates = [date.fromisoformat(d) for d in first_stim_date.values() if d]
    if not first_dates:
        return {"x": [], "dates": [], "series": {}, "trend": {"index_smooth": []}, "meta": {"version": "supercomp_v4"}}

    start = min(first_dates)
    dates = list(_daterange(start, end_all))
    x = [d.isoformat() for d in dates]

    # weights for trend
    stimulus_sum: dict[str, float] = {p: 0.0 for p in patterns}
    stimulus_count: dict[str, int] = {p: 0 for p in patterns}
    for d_iso in x:
        for p in patterns:
            v = float(stimulus_by_day.get(d_iso, {}).get(p, 0.0))
            if v > 0:
                stimulus_sum[p] += v
                stimulus_count[p] += 1

    # ------------------------------------------------------------
    # Performance trend (e1RM) per pattern from auto-selected key exercises
    # ------------------------------------------------------------
    perf_level_by_pattern: dict[str, list[float | None]] = {p: [None for _ in x] for p in patterns}
    perf_delta_by_pattern: dict[str, list[float | None]] = {p: [None for _ in x] for p in patterns}
    perf_key_lifts_used: dict[str, list[str]] = {p: [] for p in patterns}
    perf_anchor_value_by_pattern: dict[str, float | None] = {p: None for p in patterns}
    perf_points_count_by_pattern: dict[str, int] = {p: 0 for p in patterns}
    perf_last_date_by_pattern: dict[str, str | None] = {p: None for p in patterns}
    perf_drift_by_pattern: dict[str, float | None] = {p: None for p in patterns}

    # auto select top-3 exercises per pattern by session_count (distinct workouts)
    ex_counts: dict[str, dict[tuple[str, str], set[int]]] = {p: {} for p in patterns}
    ex_display: dict[tuple[str, str], str] = {}
    cur.execute("""
        SELECT e.workout_id AS workout_id, e.name AS name, COALESCE(e.variation,'') AS variation
        FROM exercises e
        ORDER BY e.workout_id ASC, e.id ASC
    """)
    for r in cur.fetchall():
        name = (r["name"] or "").strip()
        variation = (r["variation"] or "").strip()
        if not name:
            continue
        p = classify_pattern(name, variation)
        if p not in patterns:
            continue
        key = (name.lower(), variation.lower())
        ex_counts[p].setdefault(key, set()).add(int(r["workout_id"] or 0))
        if key not in ex_display:
            ex_display[key] = f"{name}{(' · ' + variation) if variation else ''}"

    top_ex_by_pattern: dict[str, list[tuple[str, str]]] = {p: [] for p in patterns}
    for p in patterns:
        items = [(k, len(ws)) for k, ws in ex_counts[p].items() if k[0]]
        items.sort(key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
        top_ex_by_pattern[p] = [k for k, _ in items[:3]]
        perf_key_lifts_used[p] = [ex_display.get(k, k[0]) for k in top_ex_by_pattern[p]]

    # best e1RM per (pattern, exercise_key, date) for selected exercises
    best_e1rm: dict[str, dict[tuple[str, str], dict[str, float]]] = {p: {} for p in patterns}
    perf_obs_by_pattern: dict[str, list[bool]] = {p: [False for _ in x] for p in patterns}
    chosen_flat = {k for p in patterns for k in top_ex_by_pattern[p]}
    if chosen_flat:
        cur.execute("""
            SELECT w.date_iso AS date_iso, e.name AS name, COALESCE(e.variation,'') AS variation,
                   s.weight AS weight, s.reps AS reps
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = e.workout_id
            WHERE COALESCE(s.reps, 0) > 0
              AND COALESCE(s.weight, 0) > 0
            ORDER BY w.date_iso ASC, w.id ASC, e.id ASC, COALESCE(s.set_number, 0) ASC, s.id ASC
        """)
        for r in cur.fetchall():
            d_iso = (r["date_iso"] or "").strip()
            if not d_iso:
                continue
            name = (r["name"] or "").strip()
            variation = (r["variation"] or "").strip()
            key = (name.lower(), variation.lower())
            if key not in chosen_flat:
                continue
            p = classify_pattern(name, variation)
            if p not in patterns or key not in top_ex_by_pattern[p]:
                continue
            e1rm = _epley_e1rm(float(r["weight"] or 0.0), int(r["reps"] or 0))
            if e1rm <= 0:
                continue
            best_e1rm.setdefault(p, {}).setdefault(key, {})
            prev = best_e1rm[p][key].get(d_iso)
            if prev is None or e1rm > float(prev):
                best_e1rm[p][key][d_iso] = float(e1rm)

        idx_by_date = {d_iso: i for i, d_iso in enumerate(x)}
        for p in patterns:
            for ex_key, mp in (best_e1rm.get(p, {}) or {}).items():
                for d_iso in mp:
                    i = idx_by_date.get(d_iso)
                    if i is not None:
                        perf_obs_by_pattern[p][i] = True

    alpha_perf = _ema_alpha(SUPERCOMP_V4_PERF_EMA_SPAN_DAYS)
    for p in patterns:
        anchor_iso = first_stim_date[p]
        ex_keys = top_ex_by_pattern.get(p, [])
        if not anchor_iso or not ex_keys:
            continue

        rels_by_ex: dict[tuple[str, str], list[float | None]] = {k: [None for _ in x] for k in ex_keys}
        ex_anchor_e1rm: dict[tuple[str, str], float] = {}

        for ex_key in ex_keys:
            prev_ema = None
            anchor_vals: list[float] = []
            for i, d_iso in enumerate(x):
                obs = best_e1rm.get(p, {}).get(ex_key, {}).get(d_iso)
                if obs is not None:
                    v = float(obs)
                    prev_ema = v if prev_ema is None else (alpha_perf * v + (1.0 - alpha_perf) * prev_ema)
                if prev_ema is None or d_iso < anchor_iso:
                    rels_by_ex[ex_key][i] = None
                    continue
                if len(anchor_vals) < 3:
                    anchor_vals.append(float(prev_ema))
                    if len(anchor_vals) == 3:
                        ex_anchor_e1rm[ex_key] = float(median(anchor_vals))
                anchor = float(ex_anchor_e1rm.get(ex_key, 0.0))
                if anchor <= 0:
                    rels_by_ex[ex_key][i] = None
                    continue
                rel = (float(prev_ema) / max(1e-6, anchor)) - 1.0
                rels_by_ex[ex_key][i] = _clamp(rel, -0.2, 0.6)

        perf = []
        for i in range(len(x)):
            vals = [rels_by_ex[k][i] for k in ex_keys if isinstance(rels_by_ex[k][i], (int, float))]
            perf.append((sum(vals) / len(vals)) if vals else None)
        perf_level_by_pattern[p] = perf
        perf_drift_by_pattern[p] = _slope_per_30_days(perf)
        perf_points_count_by_pattern[p] = int(sum(1 for v in perf if isinstance(v, (int, float))))
        last_i = max((i for i, v in enumerate(perf) if isinstance(v, (int, float))), default=None)
        perf_last_date_by_pattern[p] = x[last_i] if last_i is not None else None

        anchors = [float(v) for v in ex_anchor_e1rm.values() if float(v) > 0]
        perf_anchor_value_by_pattern[p] = float(sum(anchors) / len(anchors)) if anchors else None

        prev = None
        deltas: list[float | None] = []
        for v in perf:
            if not isinstance(v, (int, float)):
                deltas.append(None)
                continue
            if prev is None:
                deltas.append(0.0)
            else:
                deltas.append(max(0.0, float(v) - float(prev)))
            prev = float(v)
        perf_delta_by_pattern[p] = deltas

    # ------------------------------------------------------------
    # Simulate per pattern -> index + smooth
    # ------------------------------------------------------------
    alpha_index_smooth = _ema_alpha(SUPERCOMP_V4_INDEX_SMOOTH_SPAN_DAYS)

    series: dict[str, dict] = {}
    for p in patterns:
        anchor_iso = first_stim_date[p]
        if not anchor_iso:
            empty = [None for _ in x]
            series[p] = {
                "index_raw": empty,
                "index_smooth": empty,
                "first_date": None,
                # backward-compat aliases
                "index": empty,
            }
            continue

        prm = SUPERCOMP_V4_PARAMS[p]
        decay_fit = _decay_from_half_life_days(float(prm["fitness_half_life_days"]))
        decay_fat = _decay_from_half_life_days(float(prm["fatigue_half_life_days"]))

        def _simulate_anchor_readiness() -> float:
            fitness = 0.0
            fatigue = 0.0
            stim_days_index: list[float] = []
            for i, d_iso in enumerate(x):
                fitness *= decay_fit
                fatigue *= decay_fat
                stim = max(0.0, float(stimulus_by_day.get(d_iso, {}).get(p, 0.0)))
                perf_level = perf_level_by_pattern.get(p, [None for _ in x])[i]
                perf_delta = perf_delta_by_pattern.get(p, [None for _ in x])[i]
                if stim > 0:
                    fitness += SUPERCOMP_V4_GAIN_SCALE * stim
                    fatigue += SUPERCOMP_V4_COST_SCALE * stim
                    if isinstance(perf_delta, (int, float)) and perf_delta > 0:
                        fitness += SUPERCOMP_V4_K_PROG * float(perf_delta)
                perf_bonus = (
                    SUPERCOMP_V4_K_PERF_LEVEL * float(perf_level)
                    if isinstance(perf_level, (int, float))
                    else 0.0
                )
                index_state = float(fitness - (SUPERCOMP_V4_FATIGUE_WEIGHT * fatigue) + perf_bonus)
                if d_iso >= anchor_iso and stim > 0:
                    stim_days_index.append(index_state)
                    if len(stim_days_index) >= 3:
                        break
            if stim_days_index:
                return float(median(stim_days_index))
            return 0.0

        anchor_readiness = _simulate_anchor_readiness()

        fitness = 0.0
        fatigue = 0.0

        idx_raw: list[float | None] = []
        stim_dbg: list[float | None] = []
        perf_level_dbg: list[float | None] = []
        perf_delta_dbg: list[float | None] = []
        for i, d_iso in enumerate(x):
            fitness *= decay_fit
            fatigue *= decay_fat

            stim = float(stimulus_by_day.get(d_iso, {}).get(p, 0.0))
            stim = max(0.0, stim)
            fitness += SUPERCOMP_V4_GAIN_SCALE * stim
            fatigue += SUPERCOMP_V4_COST_SCALE * stim

            perf_level = perf_level_by_pattern.get(p, [None for _ in x])[i]
            perf_delta = perf_delta_by_pattern.get(p, [None for _ in x])[i]
            if isinstance(perf_delta, (int, float)) and perf_delta > 0:
                fitness += SUPERCOMP_V4_K_PROG * float(perf_delta)

            perf_bonus = (
                SUPERCOMP_V4_K_PERF_LEVEL * float(perf_level)
                if isinstance(perf_level, (int, float))
                else 0.0
            )
            index_state = float(fitness - (SUPERCOMP_V4_FATIGUE_WEIGHT * fatigue) + perf_bonus)

            # before first_stim_date[p] -> no line
            if d_iso < anchor_iso:
                idx_raw.append(None)
            else:
                idx_raw.append(float((index_state - anchor_readiness) * SUPERCOMP_V4_INDEX_SCALE))

            stim_dbg.append(stim if debug else None)
            perf_level_dbg.append(perf_level if debug else None)
            perf_delta_dbg.append(perf_delta if debug else None)

        # smooth index (EMA) but keep nulls before anchor
        idx_smooth: list[float | None] = []
        prev = None
        for v in idx_raw:
            if not isinstance(v, (int, float)):
                idx_smooth.append(None)
                continue
            if prev is None:
                prev = float(v)
            prev = (alpha_index_smooth * float(v)) + ((1.0 - alpha_index_smooth) * prev)
            idx_smooth.append(float(prev))

        series[p] = {
            "index_raw": idx_raw,
            "index_smooth": idx_smooth,
            "first_date": anchor_iso,
            # backward-compat aliases
            "index": idx_raw,
        }
        if debug:
            series[p]["stimulus_per_day"] = stim_dbg
            series[p]["perf_level"] = perf_level_dbg
            series[p]["perf_delta"] = perf_delta_dbg
            series[p]["key_lifts_used"] = perf_key_lifts_used.get(p, [])

    # trend weighted avg of available patterns (raw) + EMA smoothing for readability
    trend_raw: list[float | None] = []
    for i in range(len(x)):
        wsum = 0.0
        vsum = 0.0
        any_v = False
        for p in patterns:
            v = series[p]["index_raw"][i]
            if not isinstance(v, (int, float)):
                continue
            w = float(stimulus_sum.get(p, 0.0)) if float(stimulus_sum.get(p, 0.0)) > 0 else 1.0
            wsum += w
            vsum += w * float(v)
            any_v = True
        trend_raw.append((vsum / max(1e-9, wsum)) if any_v else None)

    alpha_trend = _ema_alpha(SUPERCOMP_V4_TREND_SMOOTH_SPAN_DAYS)
    trend_smooth: list[float | None] = []
    prev_t = None
    for v in trend_raw:
        if not isinstance(v, (int, float)):
            trend_smooth.append(None)
            continue
        if prev_t is None:
            prev_t = float(v)
        prev_t = (alpha_trend * float(v)) + ((1.0 - alpha_trend) * prev_t)
        trend_smooth.append(float(prev_t))

    meta = {
        "version": "supercomp_v4",
        "params": {
            "gain_scale": SUPERCOMP_V4_GAIN_SCALE,
            "cost_scale": SUPERCOMP_V4_COST_SCALE,
            "fatigue_weight": SUPERCOMP_V4_FATIGUE_WEIGHT,
            "index_smooth_span_days": SUPERCOMP_V4_INDEX_SMOOTH_SPAN_DAYS,
            "index_scale": SUPERCOMP_V4_INDEX_SCALE,
            "trend_smooth_span_days": SUPERCOMP_V4_TREND_SMOOTH_SPAN_DAYS,
            "perf_ema_span_days": SUPERCOMP_V4_PERF_EMA_SPAN_DAYS,
            "k_prog": SUPERCOMP_V4_K_PROG,
            "k_perf_level": SUPERCOMP_V4_K_PERF_LEVEL,
            "stimulus_clamp": {
                "pctl": SUPERCOMP_V4_STIMULUS_CLAMP_PCTL,
                "mult": SUPERCOMP_V4_STIMULUS_CLAMP_MULT,
                "thresholds": clamp_threshold,
            },
            "pattern_params": SUPERCOMP_V4_PARAMS,
        },
        "pattern_quality": {
            p: {
                "stimulus_sum": float(stimulus_sum.get(p, 0.0)),
                "stimulus_count": int(stimulus_count.get(p, 0)),
                "first_stim_date": first_stim_date[p],
                "load_scale": float(load_scale.get(p, 1.0)),
                "outlier_sessions_detected": int(outlier_clamped_count.get(p, 0)),
                "perf_anchor_value": perf_anchor_value_by_pattern.get(p),
                "perf_drift_per_30d": perf_drift_by_pattern.get(p),
                "perf_points_count": int(perf_points_count_by_pattern.get(p, 0)),
                "perf_last_date": perf_last_date_by_pattern.get(p),
                "key_lifts_used": perf_key_lifts_used.get(p, []),
            }
            for p in patterns
        },
    }

    return {
        "x": x,
        "dates": x,
        "series": series,
        "trend": {
            "index_raw": trend_raw,
            "index_smooth": trend_smooth,
        },
        "meta": meta,
    }


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def compute_supercomp_payload(
    conn: sqlite3.Connection,
    components: bool = False,
    markers: bool = False,
    debug: bool = False,
) -> dict:
    """
    New Supercomp payload:
      - readiness% vs rolling baseline (0 = baseline)
      - optional components (fitness/fatigue/baseline)
      - optional session markers
      - debug meta + quality metrics only when debug=1
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, date_iso FROM workouts ORDER BY date_iso ASC, id ASC")
    workouts = cur.fetchall()
    if not workouts:
        return {"dates": [], "series": {}, "meta": {"version": "supercomp_v3"}}

    patterns = ["chest", "back", "legs", "shoulders_arms", "core"]
    start = date.fromisoformat(workouts[0]["date_iso"])
    # include future-dated workouts (if any) so aggregation doesn't KeyError
    last_w = max(date.fromisoformat(w["date_iso"]) for w in workouts)
    end = max(date.today(), last_w)

    dates: list[date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)

    # ------------------------------------------------------------
    # Session raw load score per workout/pattern (already local via classify_pattern)
    # ------------------------------------------------------------
    session_rows: list[dict] = []
    session_scores_by_pattern: dict[str, list[float]] = {p: [] for p in patterns}

    for w in workouts:
        info = compute_pattern_stimulus_for_workout(w["id"], conn)
        if not info:
            continue
        d_iso = info["date_iso"]
        for p in patterns:
            score = float(info["patterns"][p]["set_score_sum"] or 0.0)
            eff_sets = float(info["patterns"][p]["effective_sets"] or 0.0)
            hard_sets = int(info["patterns"][p]["hard_sets"] or 0)
            if score <= 0:
                continue
            session_rows.append({
                "workout_id": int(w["id"]),
                "date_iso": d_iso,
                "pattern": p,
                "load_score": score,
                "eff_sets": eff_sets,
                "hard_sets": hard_sets,
            })
            session_scores_by_pattern[p].append(score)

    # Daily load score per pattern (sum of all workouts that day)
    daily_load_by_date_pattern: dict[str, dict[str, float]] = {}
    workout_ids_by_date_pattern: dict[str, dict[str, list[int]]] = {}
    for r in session_rows:
        d_iso = r["date_iso"]
        p = r["pattern"]
        daily_load_by_date_pattern.setdefault(d_iso, {pp: 0.0 for pp in patterns})
        daily_load_by_date_pattern[d_iso][p] += float(r["load_score"])
        workout_ids_by_date_pattern.setdefault(d_iso, {pp: [] for pp in patterns})
        workout_ids_by_date_pattern[d_iso][p].append(int(r["workout_id"]))

    # sparse pattern handling (minimal): only slightly faster detraining
    total_days = len(dates)
    total_weeks = int(math.ceil(total_days / 7.0)) if total_days > 0 else 1
    stimulus_days_raw: dict[str, int] = {}
    for p in patterns:
        stimulus_days_raw[p] = sum(
            1 for d_iso, mp in daily_load_by_date_pattern.items()
            if float(mp.get(p, 0.0)) > 0
        )
    stimulus_count_raw: dict[str, int] = {p: len(session_scores_by_pattern[p]) for p in patterns}
    per_pattern_cfg: dict[str, dict] = {}
    for p in patterns:
        sparse = (stimulus_count_raw[p] < 30) or (stimulus_days_raw[p] < int(0.5 * total_weeks))
        per_pattern_cfg[p] = {
            "sparse_pattern": bool(sparse),
            "fitness_half_life_mult": 0.85 if sparse else 1.0,
            "anchor_window_days": 35 if sparse else SUPERCOMP_ANCHOR_WINDOW_DAYS,
            "stimulus_clamp_mult": SUPERCOMP_STIMULUS_CLAMP_MULT,
            "stimulus_clamp_mult_sparse": SUPERCOMP_STIMULUS_CLAMP_MULT_SPARSE,
        }

    # clamp thresholds (P95 * mult), deterministic (sparse -> tighter clamp)
    clamp_threshold: dict[str, float] = {}
    for p in patterns:
        vals = session_scores_by_pattern[p]
        if not vals:
            clamp_threshold[p] = 0.0
            continue
        p95 = _percentile(vals, SUPERCOMP_STIMULUS_CLAMP_PCTL)
        mult = per_pattern_cfg[p]["stimulus_clamp_mult"] if len(vals) >= 8 else per_pattern_cfg[p]["stimulus_clamp_mult_sparse"]
        clamp_threshold[p] = float(p95) * float(mult)

    load_baseline_by_pattern: dict[str, dict[str, float]] = {p: {} for p in patterns}
    for p in patterns:
        values_by_date = {d_iso: float(v.get(p, 0.0)) for d_iso, v in daily_load_by_date_pattern.items() if float(v.get(p, 0.0)) > 0}
        load_baseline_by_pattern[p] = _rolling_median_prev_days(
            dates=dates,
            values_by_date=values_by_date,
            window_days=SUPERCOMP_LOAD_BASELINE_WINDOW_DAYS,
            min_value=0.1,
        )

    anchor_load_baseline: dict[str, float] = {}
    for p in patterns:
        # anchor window can be longer for sparse patterns
        anchor_days = int(per_pattern_cfg[p]["anchor_window_days"])
        anchor_end = start + timedelta(days=anchor_days)
        anchor_dates = [dd for dd in dates if start <= dd < anchor_end]
        vals = []
        for dd in anchor_dates:
            di = dd.isoformat()
            v = float((daily_load_by_date_pattern.get(di) or {}).get(p, 0.0))
            if v > 0:
                vals.append(v)
        if len(vals) >= 10:
            anchor_load_baseline[p] = float(median(vals))
        else:
            # fallback to global median of daily loads
            all_vals = [float(v.get(p, 0.0)) for v in daily_load_by_date_pattern.values() if float(v.get(p, 0.0)) > 0]
            anchor_load_baseline[p] = float(median(all_vals)) if all_vals else 1.0
        anchor_load_baseline[p] = max(0.1, anchor_load_baseline[p])

    # ------------------------------------------------------------
    # Aggregate day stimulus per pattern: non-negative + outlier-clamped (daily)
    # ------------------------------------------------------------
    stim_rolling_by_date: dict[str, dict[str, float]] = {d.isoformat(): {p: 0.0 for p in patterns} for d in dates}
    stim_anchor_by_date: dict[str, dict[str, float]] = {d.isoformat(): {p: 0.0 for p in patterns} for d in dates}
    outlier_clamped_count: dict[str, int] = {p: 0 for p in patterns}
    stimulus_count: dict[str, int] = {p: 0 for p in patterns}
    stimulus_sum: dict[str, float] = {p: 0.0 for p in patterns}

    # Outlier clamp at DAILY level (not per-set/exercise) -> avoids 251 markers + V-dips
    daily_load_clamped: dict[str, dict[str, float]] = {d.isoformat(): {p: 0.0 for p in patterns} for d in dates}
    for d_iso, mp in daily_load_by_date_pattern.items():
        for p in patterns:
            raw = float(mp.get(p, 0.0))
            if raw <= 0:
                continue
            thr = float(clamp_threshold.get(p, 0.0))
            if thr > 0 and raw > thr:
                daily_load_clamped[d_iso][p] = thr
                outlier_clamped_count[p] += 1
            else:
                daily_load_clamped[d_iso][p] = raw

    for d in dates:
        d_iso = d.isoformat()
        for p in patterns:
            raw = float(daily_load_clamped[d_iso].get(p, 0.0))
            if raw <= 0:
                continue

            baseline_load_rolling = float(load_baseline_by_pattern[p].get(d_iso, 1.0))
            stim_roll = raw / max(0.1, baseline_load_rolling)
            stim_roll = max(0.0, float(stim_roll))

            stim_long = raw / max(0.1, float(anchor_load_baseline.get(p, 1.0)))
            stim_long = max(0.0, float(stim_long))

            stim_rolling_by_date[d_iso][p] = stim_roll
            stim_anchor_by_date[d_iso][p] = stim_long

            stimulus_count[p] += 1
            stimulus_sum[p] += stim_roll

    # ------------------------------------------------------------
    # Simulation: fatigue (fast) / fitness (slow) -> readiness
    # ------------------------------------------------------------
    def _simulate_states(fitness0: dict[str, float], fatigue0: dict[str, float]) -> tuple[dict, dict, dict]:
        """
        Minimal physio model:
          - fitness decays always + gains only from stimulus
          - fatigue decays always + rises from stimulus
          - daily update ALWAYS (even when stimulus==0)
        """
        fitness = {p: float(fitness0.get(p, 1.0)) for p in patterns}
        fatigue = {p: float(fatigue0.get(p, 0.0)) for p in patterns}
        readiness_by_date: dict[str, dict[str, float]] = {dd.isoformat(): {pp: 0.0 for pp in patterns} for dd in dates}
        fitness_by_date: dict[str, dict[str, float]] = {dd.isoformat(): {pp: 0.0 for pp in patterns} for dd in dates}
        fatigue_by_date: dict[str, dict[str, float]] = {dd.isoformat(): {pp: 0.0 for pp in patterns} for dd in dates}

        for dd in dates:
            d_iso = dd.isoformat()
            for p in patterns:
                prm = SUPERCOMP_PARAMS.get(p, SUPERCOMP_PARAMS["chest"])
                fit_hl = float(prm["fitness_half_life_days"]) * float(per_pattern_cfg[p]["fitness_half_life_mult"])
                decay_fit = _decay_from_half_life_days(fit_hl)
                decay_fat = _decay_from_half_life_days(prm["fatigue_half_life_days"])

                fitness[p] *= decay_fit
                fatigue[p] *= decay_fat

                stim_roll = max(0.0, float(stim_rolling_by_date[d_iso][p]))
                stim_long = max(0.0, float(stim_anchor_by_date[d_iso][p]))

                fitness[p] += float(prm["k_fit"]) * stim_long
                fatigue[p] += float(prm["k_fat"]) * stim_roll

                readiness = float(fitness[p] - fatigue[p])

                readiness_by_date[d_iso][p] = readiness
                fitness_by_date[d_iso][p] = float(fitness[p])
                fatigue_by_date[d_iso][p] = float(fatigue[p])

        return readiness_by_date, fitness_by_date, fatigue_by_date

    # Pass 1 -> compute anchor baseline candidate from a neutral init (removes start "einbalancieren")
    readiness_by_date_1, fitness_by_date_1, fatigue_by_date_1 = _simulate_states(
        fitness0={p: 1.0 for p in patterns},
        fatigue0={p: 0.0 for p in patterns},
    )

    # Compute anchor baselines from days 15-35 (or fallback) using pass1 readiness -> constant per response
    baseline_anchor_init: dict[str, float] = {}
    anchor_p25: dict[str, float] = {}
    anchor_p75: dict[str, float] = {}
    anchor_scale: dict[str, float] = {}
    for p in SUPERCOMP_PATTERNS_MAIN:
        readiness_list = [float(readiness_by_date_1[dd.isoformat()][p]) for dd in dates]
        alpha_r0 = _ema_alpha(SUPERCOMP_READINESS_SMOOTH_SPAN_DAYS)
        r_smooth = []
        prev_r = readiness_list[0] if readiness_list else 1.0
        for i, r0 in enumerate(readiness_list):
            if i == 0:
                prev_r = r0
            prev_r = (alpha_r0 * r0) + ((1.0 - alpha_r0) * prev_r)
            r_smooth.append(prev_r)

        anchor_base = None
        use_stable_window = (not per_pattern_cfg[p]["sparse_pattern"]) and (stimulus_days_raw.get(p, 0) >= 10)
        if use_stable_window:
            vals = [float(v) for v in r_smooth[14:35] if isinstance(v, (int, float))]
            if len(vals) >= 10:
                anchor_base = float(median(vals))
        if anchor_base is None:
            first60 = r_smooth[:min(60, len(r_smooth))]
            vals60 = [float(v) for v in first60 if isinstance(v, (int, float))]
            if len(vals60) >= 10:
                anchor_base = float(median(vals60))
            else:
                all_vals = [float(v) for v in r_smooth if isinstance(v, (int, float))]
                anchor_base = float(median(all_vals)) if all_vals else 1.0
        baseline_anchor_init[p] = float(anchor_base)
        # placeholder -> computed from pass2 readiness_smooth (more representative)
        anchor_p25[p] = 0.0
        anchor_p75[p] = 0.0
        anchor_scale[p] = max(abs(baseline_anchor_init[p]) * 0.2, 1e-3)

    # Pass 2 -> stable init: fitness0 = anchor_baseline_value, fatigue0 small
    readiness_by_date, fitness_by_date, fatigue_by_date = _simulate_states(
        fitness0={p: baseline_anchor_init.get(p, 1.0) for p in patterns},
        fatigue0={p: 0.0 for p in patterns},
    )

    # ------------------------------------------------------------
    # Baseline + output percent-like values via normalized delta (no readiness/baseline ratio)
    # ------------------------------------------------------------
    readiness_rel_rolling_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    readiness_rel_anchor_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    readiness_rel_rolling_smooth_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    readiness_rel_anchor_smooth_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    baseline_rolling_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    baseline_anchor_by_pattern: dict[str, float] = {p: 1.0 for p in SUPERCOMP_PATTERNS_MAIN}
    fitness_series_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    fatigue_series_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}
    fitness_trend_by_pattern: dict[str, list[float]] = {p: [] for p in SUPERCOMP_PATTERNS_MAIN}

    alpha_r = _ema_alpha(SUPERCOMP_READINESS_SMOOTH_SPAN_DAYS)
    alpha_fit_trend = _ema_alpha(30)
    for p in SUPERCOMP_PATTERNS_MAIN:
        # anchor baseline on readiness_smooth in days 15-35 (burn-in fix)
        readiness_list = [float(readiness_by_date[d.isoformat()][p]) for d in dates]
        # smooth readiness itself (not %), keeps anchor stable
        readiness_smooth = []
        prev_r = readiness_list[0] if readiness_list else 1.0
        for i, r0 in enumerate(readiness_list):
            if i == 0:
                prev_r = r0
            prev_r = (alpha_r * r0) + ((1.0 - alpha_r) * prev_r)
            readiness_smooth.append(prev_r)

        # anchor baseline is constant per response (computed from pass1)
        anchor_base = float(baseline_anchor_init.get(p, 1.0))
        baseline_anchor_by_pattern[p] = float(anchor_base)

        # robust scale from pass2 readiness_smooth (normalized delta scale)
        stable_vals = readiness_smooth[14:35] if len(readiness_smooth) >= 35 else readiness_smooth[:min(60, len(readiness_smooth))]
        p25, p75, sc_stable = _robust_scale_from_values(stable_vals, baseline_fallback=anchor_base, min_scale=1e-3)
        first60_vals = readiness_smooth[:min(60, len(readiness_smooth))]
        _, _, sc_60 = _robust_scale_from_values(first60_vals, baseline_fallback=anchor_base, min_scale=1e-3)
        base_abs_floor = max(abs(anchor_base) * 0.2, 1e-3)
        anchor_p25[p] = float(p25)
        anchor_p75[p] = float(p75)
        anchor_scale[p] = float(max(sc_stable, sc_60, base_abs_floor))

        # rolling median baseline on readiness values (previous 28 days), fallback to anchor for early days
        values_by_date = {d_iso: readiness_by_date[d_iso][p] for d_iso in readiness_by_date}
        base_prev = _rolling_median_prev_days(
            dates=dates,
            values_by_date=values_by_date,
            window_days=SUPERCOMP_READINESS_BASELINE_WINDOW_DAYS,
            min_value=None,
        )
        roll_scale_prev = _rolling_iqr_prev_days(
            dates=dates,
            values_by_date=values_by_date,
            window_days=SUPERCOMP_READINESS_BASELINE_WINDOW_DAYS,
            min_scale=1e-3,
            fallback_scale=float(anchor_scale.get(p, 1.0)),
        )

        prev_roll_smooth = 0.0
        prev_anchor_smooth = 0.0
        prev_fit_trend = 0.0
        for i, d in enumerate(dates):
            d_iso = d.isoformat()
            r = float(readiness_by_date[d_iso][p])
            # rolling baseline: use anchor fallback for first days (prevents warmup rockets)
            b_roll = float(base_prev.get(d_iso, anchor_base))
            if i < 7 and b_roll <= 0:
                b_roll = anchor_base

            sc_anchor = max(1e-3, float(anchor_scale.get(p, 1.0)))
            sc_roll = max(1e-3, float(roll_scale_prev.get(d_iso, sc_anchor)))
            pct_roll = 100.0 * ((r - b_roll) / sc_roll)
            pct_anchor = 100.0 * ((r - anchor_base) / sc_anchor)

            readiness_rel_rolling_by_pattern[p].append(pct_roll)
            readiness_rel_anchor_by_pattern[p].append(pct_anchor)
            baseline_rolling_by_pattern[p].append(b_roll)
            fitness_series_by_pattern[p].append(float(fitness_by_date[d_iso][p]))
            fatigue_series_by_pattern[p].append(float(fatigue_by_date[d_iso][p]))

            # smoothing (separate per series)
            if i == 0:
                prev_roll_smooth = pct_roll
                prev_anchor_smooth = pct_anchor
                prev_fit_trend = float(fitness_by_date[d_iso][p])
            prev_roll_smooth = (alpha_r * pct_roll) + ((1.0 - alpha_r) * prev_roll_smooth)
            prev_anchor_smooth = (alpha_r * pct_anchor) + ((1.0 - alpha_r) * prev_anchor_smooth)
            readiness_rel_rolling_smooth_by_pattern[p].append(prev_roll_smooth)
            readiness_rel_anchor_smooth_by_pattern[p].append(prev_anchor_smooth)

            # fitness trend (EMA 30d), expressed as % vs anchor fitness
            fit_val = float(fitness_by_date[d_iso][p])
            prev_fit_trend = (alpha_fit_trend * fit_val) + ((1.0 - alpha_fit_trend) * prev_fit_trend)
            fitness_trend_by_pattern[p].append(prev_fit_trend)

    # ------------------------------------------------------------
    # Build payload (query flags)
    # ------------------------------------------------------------
    payload_series: dict[str, dict] = {}
    for p in SUPERCOMP_PATTERNS_MAIN:
        payload_series[p] = {
            "readiness_rel_rolling": readiness_rel_rolling_by_pattern[p],
            "readiness_rel_anchor": readiness_rel_anchor_by_pattern[p],
            "readiness_rel_rolling_smooth": readiness_rel_rolling_smooth_by_pattern[p],
            "readiness_rel_anchor_smooth": readiness_rel_anchor_smooth_by_pattern[p],
            "fitness_trend": [
                100.0 * ((float(v) - float(baseline_anchor_by_pattern[p])) / max(1e-3, float(anchor_scale.get(p, 1.0))))
                for v in fitness_trend_by_pattern[p]
            ],
            "baseline_anchor": baseline_anchor_by_pattern[p],
            "baseline_rolling": baseline_rolling_by_pattern[p],

            # backward-compat aliases (kept small)
            "readiness": readiness_rel_anchor_by_pattern[p],
            "readiness_smooth": readiness_rel_anchor_smooth_by_pattern[p],
        }
        if components or debug:
            payload_series[p] |= {
                "fitness": fitness_series_by_pattern[p],
                "fatigue": fatigue_series_by_pattern[p],
                "fitness_trend_raw": fitness_trend_by_pattern[p],
            }

    payload = {
        "dates": [d.isoformat() for d in dates],
        "series": payload_series,
        "meta": {
            "version": "supercomp_v3",
            "readiness_units": "%_vs_baseline",
            "load_baseline_window_days": SUPERCOMP_LOAD_BASELINE_WINDOW_DAYS,
            "readiness_baseline_window_days": SUPERCOMP_READINESS_BASELINE_WINDOW_DAYS,
            "anchor_window_days": SUPERCOMP_ANCHOR_WINDOW_DAYS,
            "readiness_smooth_span_days": SUPERCOMP_READINESS_SMOOTH_SPAN_DAYS,
            "burn_in_days": SUPERCOMP_BURNIN_DAYS,
            "stimulus_clamp": {
                "pctl": SUPERCOMP_STIMULUS_CLAMP_PCTL,
                "mult": SUPERCOMP_STIMULUS_CLAMP_MULT,
                "mult_sparse": SUPERCOMP_STIMULUS_CLAMP_MULT_SPARSE,
                "thresholds": {p: clamp_threshold.get(p, 0.0) for p in SUPERCOMP_PATTERNS_MAIN},
            },
            "pattern_params": {p: SUPERCOMP_PARAMS[p] for p in SUPERCOMP_PATTERNS_MAIN},
        }
    }

    # always provide small weights for frontend trend weighting
    payload["meta"]["pattern_quality"] = {
        p: {
            "stimulus_sum": float(stimulus_sum.get(p, 0.0)),
            "stimulus_count": int(stimulus_count.get(p, 0)),
            "baseline_anchor": float(baseline_anchor_by_pattern.get(p, 1.0)),
            "anchor_scale": float(anchor_scale.get(p, 1.0)),
            "anchor_p25": float(anchor_p25.get(p, 0.0)),
            "anchor_p75": float(anchor_p75.get(p, 0.0)),
            "sparse_pattern": bool(per_pattern_cfg[p]["sparse_pattern"]),
            "anchor_window_days": int(per_pattern_cfg[p]["anchor_window_days"]),
            "burn_in_days": int(SUPERCOMP_BURNIN_DAYS),
            "outlier_sessions_detected": int(outlier_clamped_count.get(p, 0)),
            "fitness_half_life_mult": float(per_pattern_cfg[p]["fitness_half_life_mult"]),
        }
        for p in SUPERCOMP_PATTERNS_MAIN
    }

    if markers:
        # aggregated markers: ~1 per (date,pattern)
        agg: dict[tuple[str, str], dict] = {}
        for d in dates:
            d_iso = d.isoformat()
            for p in SUPERCOMP_PATTERNS_MAIN:
                stim = float(stim_rolling_by_date[d_iso][p])
                if stim <= 0:
                    continue
                wids = list(dict.fromkeys((workout_ids_by_date_pattern.get(d_iso) or {}).get(p, [])))
                agg[(d_iso, p)] = {
                    "date_iso": d_iso,
                    "pattern": p,
                    "stimulus": stim,
                    "workout_ids": wids,
                }
        payload["sessions_markers"] = list(agg.values())

    if debug:
        # optional debug: stimulus per day per pattern (lets us confirm "rises without stimulus" truly has stimulus==0)
        for p in SUPERCOMP_PATTERNS_MAIN:
            payload_series[p]["stimulus_per_day"] = [float(stim_rolling_by_date[d.isoformat()][p]) for d in dates]

        pattern_quality: dict[str, dict] = {}
        for p in SUPERCOMP_PATTERNS_MAIN:
            fit_vals = fitness_series_by_pattern[p]
            fat_vals = fatigue_series_by_pattern[p]
            stim_days = sum(1 for d in dates if stim_rolling_by_date[d.isoformat()][p] > 0)
            stim_sum = float(stimulus_sum.get(p, 0.0))
            drift_anchor = _slope_per_30_days(readiness_rel_anchor_by_pattern[p])
            drift_roll = _slope_per_30_days(readiness_rel_rolling_by_pattern[p])
            drift_fit = _slope_per_30_days(fitness_trend_by_pattern[p])
            avg_fit = mean(fit_vals[-28:]) if len(fit_vals) >= 7 else mean(fit_vals) if fit_vals else 0.0
            avg_fat = mean(fat_vals[-28:]) if len(fat_vals) >= 7 else mean(fat_vals) if fat_vals else 0.0
            pattern_quality[p] = {
                "drift_anchor_per_30d": drift_anchor,
                "drift_rolling_per_30d": drift_roll,
                "fitness_trend_slope_per_30d": drift_fit,
                "avg_fitness": avg_fit,
                "avg_fatigue": avg_fat,
                "stimulus_days": stim_days,
                "stimulus_count": int(stimulus_count.get(p, 0)),
                "stimulus_sum": stim_sum,
                "outlier_sessions_detected": int(outlier_clamped_count.get(p, 0)),
                "baseline_anchor": baseline_anchor_by_pattern[p],
            }
        # merge debug-only extras
        payload["meta"]["pattern_quality"] = {
            p: (payload["meta"]["pattern_quality"].get(p, {}) | pattern_quality.get(p, {}))
            for p in SUPERCOMP_PATTERNS_MAIN
        }

    return payload


# ============================================================
# PATTERN-KLASSIFIKATION
# ============================================================

def classify_pattern(name: str, variation: str = "") -> str | None:
    if not name:
        return None

    text = f"{name} {variation}".lower()

    def has(*subs: str) -> bool:
        return any(s in text for s in subs)

    # ----- LEGS -----
    if has(
        "squat", "kniebeuge", "hack squat", "front squat", "backsquat",
        "leg press", "beinpresse", "beinpress",
        "leg curl", "beinbeuger",
        "leg extension", "beinstrecker",
        "lunge", "split squat", "bulgarian",
        "adductor", "abductor", "adduktor", "abduktor",
        "calf", "wade", "wadenheben",
        "hip thrust", "glute bridge", "glute"
    ):
        return "legs"

    # ----- CHEST -----
    if has(
        "bench", "bankdrück", "bankdrueck",
        "chest press", "brustpresse",
        "butterfly", "pec fly", "pec deck",
        "fly", "incline fly",
        "dip", "push up", "liegestütz",
        "schrägbank", "schraegbank"
    ):
        return "chest"

    # ----- BACK -----
    if has(
        "row", "rudern", "t-bar",
        "latzug", "lat pulldown", "pull up", "chin up",
        "klimmzug", "pullover", "überzüge", "ueberzuege"
    ):
        return "back"

    # ----- SHOULDERS & ARMS -----
    if has(
        "ohp", "overhead press", "shoulder press", "military press",
        "arnold press",
        "lateral raise", "seitheben", "front raise", "y-raise",
        "rear delt", "reverse fly", "shrug", "upright row",
        "curl", "biceps", "bizeps", "hammer curl",
        "triceps", "trizeps", "pushdown",
        "french press", "skullcrusher",
        "katana", "bayesian",
        "wrist curl", "wrist extension"
    ):
        return "shoulders_arms"

    # ----- CORE -----
    if has(
        "crunch", "sit up", "leg raise",
        "plank", "russian twist",
        "dead bug", "hollow",
        "rotator"
    ):
        return "core"

    return None


# ============================================================
# MVPS / SUPERCOMP HELPERS
# ============================================================

def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _decay_from_half_life_days(half_life_days: float) -> float:
    """
    daily decay multiplier for dt=1:
      value *= exp(-ln(2) / half_life_days)
    """
    h = max(1e-6, float(half_life_days))
    return math.exp(-math.log(2.0) / h)


def _softcap_log(x: float, cap: float, k: float = 1.25) -> tuple[float, bool]:
    """
    Soft cap without pinning:
      if x <= cap -> x
      else -> cap + log(1 + k*(x-cap)) / k
    Returns (y, cap_hit)
    """
    if x <= cap:
        return x, False
    d = (x - cap) * max(1e-9, float(k))
    return cap + (math.log1p(d) / max(1e-9, float(k))), True


def _softfloor_log(x: float, floor: float, k: float = 1.25) -> tuple[float, bool]:
    """
    Soft floor without pinning:
      if x >= floor -> x
      else -> floor - log(1 + k*(floor-x)) / k
    Returns (y, floor_hit)
    """
    if x >= floor:
        return x, False
    d = (floor - x) * max(1e-9, float(k))
    return floor - (math.log1p(d) / max(1e-9, float(k))), True


def _rpe_norm(rpe: float | None, default: float = 7.0) -> float:
    """
    RPE6 -> 0.0 ... RPE10 -> 1.0 (clamped)
    (wenn RPE fehlt -> konservativ default=7.0)
    """
    r = default if rpe is None else float(rpe)
    return _clamp((r - 6.0) / 4.0, 0.0, 1.0)


def _movement_factor(name: str, variation: str = "") -> float:
    """
    Compound > Iso Heuristik (compound gewinnt bei Konflikt).
    """
    text = f"{name} {variation}".lower()

    compound_kw = (
        "bench", "press", "row", "pull", "squat", "deadlift", "rdl", "ohp",
        "dip", "chin", "pullup", "pull-up", "klimmzug", "latzug", "lunge",
        "hip thrust", "hack squat", "leg press", "chest press", "shoulder press",
    )
    iso_kw = (
        "curl", "raise", "fly", "extension", "pushdown", "push-down", "lateral",
        "bayesian", "pec deck", "butterfly", "rear delt", "reverse fly", "seitheben",
        "trizeps", "bizeps", "wrist curl", "wrist extension",
    )

    has_compound = any(k in text for k in compound_kw)
    has_iso = any(k in text for k in iso_kw)

    if has_compound:
        return 1.25
    if has_iso:
        return 0.95
    return 1.0


def _volume_modifiers_v2(sets_proxy: float, p: dict) -> tuple[float, float]:
    """
    Returns (stim_zone, fat_mul)
    - Unter MEV: stim_zone < 1, fatigue niedriger
    - OPT: stim_zone leicht > 1
    - >MRV: stim_zone sinkt, fatigue steigt
    """
    s = max(0.0, float(sets_proxy or 0.0))
    if s <= 0:
        return 0.0, 1.0

    if s < p["mev"]:
        return 0.85 + 0.025 * s, 0.9
    if s <= p["opt_high"]:
        return 1.05, 1.0
    if s <= p["mrv"]:
        return max(0.6, 1.00 - 0.02 * (s - p["opt_high"])), 1.15
    return 0.80, 1.30


def compute_pattern_stimulus_for_workout(workout_id: int, conn) -> dict | None:
    """
    Pro Workout: stimulus inputs pro Pattern (lokal!)
    Returns:
      - date_iso
      - patterns[p]: {set_score_sum, effective_sets, hard_sets}
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT date_iso FROM workouts WHERE id=?", (workout_id,))
    row = cur.fetchone()
    if not row:
        return None

    date_iso = row["date_iso"]

    cur.execute(
        "SELECT id, name, variation FROM exercises WHERE workout_id=?",
        (workout_id,)
    )
    exercises = cur.fetchall()

    patterns = ["chest", "back", "legs", "shoulders_arms", "core"]
    out = {
        p: {"set_score_sum": 0.0, "effective_sets": 0.0, "hard_sets": 0}
        for p in patterns
    }

    for ex in exercises:
        pattern = classify_pattern(ex["name"], ex["variation"] or "")
        if pattern not in out:
            continue

        mf = _movement_factor(ex["name"], ex["variation"] or "")

        cur.execute("""
            SELECT reps, weight, rpe
            FROM sets
            WHERE exercise_id=?
            ORDER BY set_number ASC, id ASC
        """, (ex["id"],))
        rows = cur.fetchall()

        for s in rows:
            reps = s["reps"] or 0
            if reps <= 0:
                continue

            rpe = s["rpe"]
            rn = _rpe_norm(rpe, default=7.0)
            hard = 1 if (rpe is not None and float(rpe) >= 8.0) else 0

            # Effective-Set Proxy: 0.7 .. 1.3 (RPE-abhängig)
            set_w = _clamp(0.7 + 0.6 * rn, 0.7, 1.3)

            weight = s["weight"]
            if weight is not None and float(weight) > 0:
                tonnage = float(reps) * float(weight)
                tonnage_scaled = math.log1p(tonnage / 100.0)
            else:
                tonnage_scaled = float(reps)

            set_score = tonnage_scaled * (0.6 + 0.8 * rn) * mf

            out[pattern]["set_score_sum"] += set_score
            out[pattern]["effective_sets"] += set_w
            out[pattern]["hard_sets"] += hard

    # Diminishing returns über Sets (concave)
    for p in out:
        eff = out[p]["effective_sets"]
        ssum = out[p]["set_score_sum"]
        if eff > 0 and ssum > 0:
            vol_factor = eff ** 0.85
            out[p]["set_score_sum"] = (ssum / eff) * vol_factor

    return {"date_iso": date_iso, "patterns": out}


def _rolling_median_baselines(
    dates: list[date],
    workout_scores_by_date: dict[str, dict],
    patterns: list[str],
    window_days: int = 90,
    min_baseline: float = 0.1,
    ema_span_days: int = 21,
) -> dict[str, dict[str, float]]:
    """
    Rolling baseline pro Tag pro Pattern (median über Trainingstage im Fenster).
    Baseline für ein Datum nutzt NUR vergangene Trainingstage (exkl. aktueller Tag).
    """
    history: dict[str, deque[tuple[date, float]]] = {p: deque() for p in patterns}
    baselines: dict[str, dict[str, float]] = {}
    alpha = 2.0 / (float(ema_span_days) + 1.0) if ema_span_days and ema_span_days > 0 else 1.0
    ema_prev: dict[str, float] = {p: 1.0 for p in patterns}

    for d in dates:
        d_iso = d.isoformat()
        cutoff = d - timedelta(days=window_days)

        baselines[d_iso] = {"raw": {}, "ema": {}}
        for p in patterns:
            q = history[p]
            while q and q[0][0] < cutoff:
                q.popleft()

            vals = [v for _, v in q if v > 0]
            if len(vals) >= 5:
                b = median(vals)
            elif vals:
                b = mean(vals)
            else:
                b = 1.0
            b = max(min_baseline, float(b))
            ema_prev[p] = (alpha * b) + ((1.0 - alpha) * ema_prev[p])
            baselines[d_iso]["raw"][p] = b
            baselines[d_iso]["ema"][p] = max(min_baseline, float(ema_prev[p]))

        # After baselines: add today's score if present (>0)
        if d_iso in workout_scores_by_date:
            day = workout_scores_by_date[d_iso]
            for p in patterns:
                score = float((day.get("set_score_sum") or {}).get(p, 0.0))
                if score > 0:
                    history[p].append((d, score))

    return baselines


def _simulate_fitness_fatigue(
    dates: list[date],
    day_inputs: dict[str, dict],
    baselines: dict[str, dict[str, float]],
    params: dict[str, dict],
    patterns: list[str],
    exp: float = 0.9,
    trend_span_days: int = 10,
    mvps_floor: float = 0.4,
    mvps_cap: float = 3.6,
    cap_k: float = 1.25,
    mvps_norm_span_days: int = 90,
    stimulus_scale_span_days: int = 90,
    stimulus_scale_decay: float = 0.999,
    debug: bool = False,
) -> list[dict]:
    """
    Day-by-day simulation:
      - decay fitness slowly
      - decay fatigue faster
      - training -> fatigue impulse > fitness impulse
    """
    fitness = {p: 0.0 for p in patterns}
    fatigue = {p: 0.0 for p in patterns}
    trend = {p: 1.0 for p in patterns}
    alpha_trend = 2.0 / (float(trend_span_days) + 1.0)

    # scale-only normalization of mvps_raw using a slow EMA of magnitude (never flips sign)
    # baseline_scale ~= 1 + EMA(|mvps_raw_pre_norm-1|)  -> always >= 1.0
    alpha_norm = 2.0 / (float(mvps_norm_span_days) + 1.0)
    baseline_scale = {p: 1.0 for p in patterns}

    # stimulus scaling (prevents "baseline" from acting like an offset; keeps stimulus_eff stable)
    alpha_stim = 2.0 / (float(stimulus_scale_span_days) + 1.0)
    stimulus_scale = {p: 1.0 for p in patterns}

    main = ["chest", "back", "legs", "shoulders_arms"]
    result: list[dict] = []

    for d in dates:
        d_iso = d.isoformat()

        # decay (dt=1) -> ALWAYS updates, even on rest days
        for p in patterns:
            fitness[p] *= _decay_from_half_life_days(params[p]["fitness_half_life_days"])
            fatigue[p] *= _decay_from_half_life_days(params[p]["fatigue_half_life_days"])

        # impulses
        day = day_inputs.get(d_iso)
        stimulus_today_raw: dict[str, float] = {p: 0.0 for p in patterns}
        stimulus_eff: dict[str, float] = {p: 0.0 for p in patterns}
        stimulus_eff_clamped: dict[str, bool] = {p: False for p in patterns}
        acute_cost: dict[str, float] = {p: 0.0 for p in patterns}
        clamp_hit: dict[str, bool] = {p: False for p in patterns}
        baseline_scale_clamped: dict[str, bool] = {p: False for p in patterns}
        mvps_raw_pre_norm: dict[str, float] = {p: 1.0 for p in patterns}
        mvps_norm: dict[str, float] = {p: 1.0 for p in patterns}
        mvps_smooth: dict[str, float] = {p: 1.0 for p in patterns}
        mvps_final_raw: dict[str, float] = {p: 1.0 for p in patterns}
        mvps_final_trend: dict[str, float] = {p: 1.0 for p in patterns}

        for p in patterns:
            score = 0.0
            eff_sets = 0.0
            hard_sets = 0
            if day:
                score = float((day.get("set_score_sum") or {}).get(p, 0.0))
                eff_sets = float((day.get("effective_sets") or {}).get(p, 0.0))
                hard_sets = int((day.get("hard_sets") or {}).get(p, 0))

            if score > 0:
                baseline = float(((baselines.get(d_iso) or {}).get("ema") or {}).get(p, 1.0))
                stim_zone, fat_mul = _volume_modifiers_v2(eff_sets, params[p])
                stimulus = (max(0.0, score) / max(0.1, baseline)) ** exp
                stimulus *= stim_zone
                stimulus = max(0.0, float(stimulus))
                stimulus_today_raw[p] = stimulus

                # stimulus_scale updates ONLY on non-zero days, and never collapses to 0
                target = max(stimulus_scale[p] * float(stimulus_scale_decay), stimulus)
                stimulus_scale[p] = (alpha_stim * target) + ((1.0 - alpha_stim) * stimulus_scale[p])
                stimulus_scale[p] = max(1e-6, float(stimulus_scale[p]))

                seff = stimulus_today_raw[p] / max(1e-6, stimulus_scale[p])
                if seff < 0.0:
                    stimulus_eff_clamped[p] = True
                    seff = 0.0
                stimulus_eff[p] = seff

                # state updates use stimulus_eff ONLY (no feedback from mvps_norm/mvps_final)
                fatigue[p] += params[p]["k_fat"] * stimulus_eff[p] * fat_mul
                fitness[p] += params[p]["k_fit"] * stimulus_eff[p]
                acute_cost[p] = params[p]["acute_k"] * stimulus_eff[p]

        for p in patterns:
            # enforce a visible same-day drop via acute_cost (stimulus_eff -> acute_cost)
            mvps_raw_pre_norm[p] = 1.0 + fitness[p] - fatigue[p] - acute_cost[p]

            # scale-only normalization (slow EMA) -> no daily offset/subtraction
            # keep baseline_scale positive so it cannot create sign flips/plateaus
            mag = 1.0 + abs(mvps_raw_pre_norm[p] - 1.0)
            baseline_scale[p] = (alpha_norm * mag) + ((1.0 - alpha_norm) * baseline_scale[p])
            if baseline_scale[p] < 1.0:
                baseline_scale_clamped[p] = True
                baseline_scale[p] = 1.0
            mvps_norm[p] = 1.0 + ((mvps_raw_pre_norm[p] - 1.0) / max(1e-6, baseline_scale[p]))

            # patterns: no smoothing (keep micro-sawtooth)
            mvps_smooth[p] = mvps_norm[p]

            y, hit_floor = _softfloor_log(mvps_smooth[p], mvps_floor, k=cap_k)
            y2, hit_cap = _softcap_log(y, mvps_cap, k=cap_k)
            mvps_final_raw[p] = y2
            clamp_hit[p] = bool(hit_floor or hit_cap)

        # trend: EMA over raw (smooth is allowed here)
        trend = {p: (alpha_trend * mvps_final_raw[p]) + ((1.0 - alpha_trend) * trend[p]) for p in patterns}
        mvps_final_trend = {p: trend[p] for p in patterns}

        row = {
            "date_iso": d_iso,
            # Pattern lines -> raw (sawtooth)
            "mvps": {p: mvps_final_raw[p] for p in main} | {
                "overall": (sum(mvps_final_raw[p] for p in main) / len(main))
            },
            # Trend lines -> EMA
            "trend": {p: mvps_final_trend[p] for p in main} | {
                "overall": (sum(mvps_final_trend[p] for p in main) / len(main))
            },
        }

        if debug:
            dbg_inputs = day_inputs.get(d_iso) or {}
            row["debug"] = {
                # stimulus pipeline
                "stimulus_today_raw": {p: stimulus_today_raw[p] for p in main},
                "stimulus_scale": {p: float(stimulus_scale[p]) for p in main},
                "stimulus_eff": {p: stimulus_eff[p] for p in main},
                "stimulus_eff_clamped": {p: bool(stimulus_eff_clamped[p]) for p in main},
                "fitness": {p: fitness[p] for p in main},
                "fatigue": {p: fatigue[p] for p in main},
                "acute_cost": {p: acute_cost[p] for p in main},
                "mvps_raw_pre_norm": {p: mvps_raw_pre_norm[p] for p in main},
                "mvps_norm": {p: mvps_norm[p] for p in main},
                "mvps_smooth": {p: mvps_smooth[p] for p in main},
                "mvps_final_raw": {p: mvps_final_raw[p] for p in main},
                "mvps_final_trend": {p: mvps_final_trend[p] for p in main},
                "clamp_hit": {p: bool(clamp_hit[p]) for p in main},
                "hard_sets": {p: int((dbg_inputs.get("hard_sets") or {}).get(p, 0)) for p in main},
                "eff_sets": {p: float((dbg_inputs.get("effective_sets") or {}).get(p, 0.0)) for p in main},
                "set_score_sum": {p: float((dbg_inputs.get("set_score_sum") or {}).get(p, 0.0)) for p in main},
                "baseline": {p: float(((baselines.get(d_iso) or {}).get("ema") or {}).get(p, 1.0)) for p in main},
                "baseline_raw": {p: float(((baselines.get(d_iso) or {}).get("raw") or {}).get(p, 1.0)) for p in main},
                "baseline_scale": {p: float(baseline_scale[p]) for p in main},
                "baseline_scale_clamped": {p: bool(baseline_scale_clamped[p]) for p in main},
            }

        result.append(row)

    return result


# Central parameters (tuneable)
MVPS_V2_WINDOW_DAYS = 90
MVPS_V2_EXP = 0.9
MVPS_V2_TREND_SPAN_DAYS = 10
MVPS_V2_FATIGUE_HALF_LIFE_DAYS = 3.0
MVPS_V2_FITNESS_HALF_LIFE_DAYS = 32.0
MVPS_V2_MVPS_FLOOR = 0.4
MVPS_V2_MVPS_CAP = 3.6
MVPS_V2_CAP_K = 1.25
MVPS_V2_BASELINE_EMA_SPAN_DAYS = 90
MVPS_V2_MVPS_NORM_SPAN_DAYS = 90
MVPS_V2_STIMULUS_SCALE_SPAN_DAYS = 90
MVPS_V2_STIMULUS_SCALE_DECAY = 0.999
MVPS_V2_ACUTE_K = 0.22

MVPS_V2_PARAMS: dict[str, dict] = {
    # ratios: fatigue gain >> fitness gain + explicit acute_k enforces drop on stimulus day
    "chest": {"fitness_half_life_days": 32.0, "fatigue_half_life_days": 2.6, "k_fit": 0.15, "k_fat": 0.45, "acute_k": 0.22, "mev": 6, "opt_high": 12, "mrv": 18},
    "back": {"fitness_half_life_days": 34.0, "fatigue_half_life_days": 2.7, "k_fit": 0.155, "k_fat": 0.465, "acute_k": 0.225, "mev": 6, "opt_high": 12, "mrv": 18},
    "legs": {"fitness_half_life_days": 36.0, "fatigue_half_life_days": 3.0, "k_fit": 0.16, "k_fat": 0.50, "acute_k": 0.245, "mev": 7, "opt_high": 14, "mrv": 20},
    "shoulders_arms": {"fitness_half_life_days": 28.0, "fatigue_half_life_days": 2.4, "k_fit": 0.145, "k_fat": 0.435, "acute_k": 0.215, "mev": 6, "opt_high": 14, "mrv": 22},
    "core": {"fitness_half_life_days": 24.0, "fatigue_half_life_days": 2.3, "k_fit": 0.13, "k_fat": 0.39, "acute_k": 0.18, "mev": 4, "opt_high": 10, "mrv": 16},
}


def mvps_timeseries_payload(rows: list[dict], debug: bool = False) -> dict:
    """
    API payload:
      {
        dates: [...],
        series: { chest: {...}, ... , overall: {...} },
        meta: {...},
        legacy: [...]
      }
    """
    patterns = ["chest", "back", "legs", "shoulders_arms"]
    dates = [r["date_iso"] for r in rows]

    def arr(getter):
        return [getter(r) for r in rows]

    series: dict[str, dict] = {}

    for p in patterns:
        series[p] = {
            "mvps": arr(lambda r, p=p: (r.get("mvps") or {}).get(p)),
            "trend": arr(lambda r, p=p: (r.get("trend") or {}).get(p)),
        }

    # Overall: average over main patterns
    series["overall"] = {
        "mvps": arr(lambda r: (r.get("mvps") or {}).get("overall")),
        "trend": arr(lambda r: (r.get("trend") or {}).get("overall")),
    }

    payload = {
        "dates": dates,
        "series": series,
        "meta": {
            "window_days": MVPS_V2_WINDOW_DAYS,
            "exp": MVPS_V2_EXP,
            "baseline_ema_span_days": MVPS_V2_BASELINE_EMA_SPAN_DAYS,
            "mvps_norm_span_days": MVPS_V2_MVPS_NORM_SPAN_DAYS,
            "stimulus_scale_span_days": MVPS_V2_STIMULUS_SCALE_SPAN_DAYS,
            "stimulus_scale_decay": MVPS_V2_STIMULUS_SCALE_DECAY,
            "mvps_floor": MVPS_V2_MVPS_FLOOR,
            "mvps_cap": MVPS_V2_MVPS_CAP,
            "mvps_cap_k": MVPS_V2_CAP_K,
            "trend_span_days": MVPS_V2_TREND_SPAN_DAYS,
            "pattern_params": {k: v for k, v in MVPS_V2_PARAMS.items() if k in patterns},
            "debug_fields_available": True,
            "mvps_pipeline": ["mvps_raw_pre_norm", "mvps_norm", "mvps_smooth", "mvps_final_raw", "mvps_final_trend"],
        },
        "legacy": rows,
    }

    if debug:
        debug_by_pattern: dict[str, dict[str, list]] = {}
        for p in patterns:
            debug_by_pattern[p] = {
                "stimulus_today_raw": arr(lambda r, p=p: ((r.get("debug") or {}).get("stimulus_today_raw") or {}).get(p)),
                "stimulus_scale": arr(lambda r, p=p: ((r.get("debug") or {}).get("stimulus_scale") or {}).get(p)),
                "stimulus_eff": arr(lambda r, p=p: ((r.get("debug") or {}).get("stimulus_eff") or {}).get(p)),
                "stimulus_eff_clamped": arr(lambda r, p=p: ((r.get("debug") or {}).get("stimulus_eff_clamped") or {}).get(p)),
                "fitness": arr(lambda r, p=p: ((r.get("debug") or {}).get("fitness") or {}).get(p)),
                "fatigue": arr(lambda r, p=p: ((r.get("debug") or {}).get("fatigue") or {}).get(p)),
                "acute_cost": arr(lambda r, p=p: ((r.get("debug") or {}).get("acute_cost") or {}).get(p)),
                "mvps_raw_pre_norm": arr(lambda r, p=p: ((r.get("debug") or {}).get("mvps_raw_pre_norm") or {}).get(p)),
                "mvps_norm": arr(lambda r, p=p: ((r.get("debug") or {}).get("mvps_norm") or {}).get(p)),
                "mvps_smooth": arr(lambda r, p=p: ((r.get("debug") or {}).get("mvps_smooth") or {}).get(p)),
                "mvps_final_raw": arr(lambda r, p=p: ((r.get("debug") or {}).get("mvps_final_raw") or {}).get(p)),
                "mvps_final_trend": arr(lambda r, p=p: ((r.get("debug") or {}).get("mvps_final_trend") or {}).get(p)),
                "baseline": arr(lambda r, p=p: ((r.get("debug") or {}).get("baseline") or {}).get(p)),
                "baseline_raw": arr(lambda r, p=p: ((r.get("debug") or {}).get("baseline_raw") or {}).get(p)),
                "baseline_scale": arr(lambda r, p=p: ((r.get("debug") or {}).get("baseline_scale") or {}).get(p)),
                "baseline_scale_clamped": arr(lambda r, p=p: ((r.get("debug") or {}).get("baseline_scale_clamped") or {}).get(p)),
                "clamp_hit": arr(lambda r, p=p: ((r.get("debug") or {}).get("clamp_hit") or {}).get(p)),
                "hard_sets": arr(lambda r, p=p: ((r.get("debug") or {}).get("hard_sets") or {}).get(p)),
                "eff_sets": arr(lambda r, p=p: ((r.get("debug") or {}).get("eff_sets") or {}).get(p)),
            }

        payload["meta"]["debug_by_pattern"] = debug_by_pattern

        # Quality metrics (debug-only)
        pattern_quality: dict[str, dict] = {}
        warnings: list[str] = []
        for p in patterns:
            stim_eff = debug_by_pattern[p]["stimulus_eff"]
            mv = series[p]["mvps"]
            raw = debug_by_pattern[p]["mvps_raw_pre_norm"]
            norm = debug_by_pattern[p]["mvps_norm"]
            fit = debug_by_pattern[p]["fitness"]
            bscale = debug_by_pattern[p]["baseline_scale"]

            n = len(mv)
            stim_days = [i for i, s in enumerate(stim_eff) if isinstance(s, (int, float)) and s > 0]

            # drop_rate on stimulus days
            drops = 0
            for i in stim_days:
                if i <= 0:
                    continue
                if mv[i] is not None and mv[i - 1] is not None and mv[i] < mv[i - 1]:
                    drops += 1
            drop_rate = drops / max(1, len([i for i in stim_days if i > 0]))

            # norm amplification rate (should be near 0)
            amp = 0
            amp_n = 0
            for r, m in zip(raw, norm, strict=False):
                if not isinstance(r, (int, float)) or not isinstance(m, (int, float)):
                    continue
                amp_n += 1
                if abs(m - 1.0) > abs(r - 1.0) + 1e-9:
                    amp += 1
            norm_amplification_rate = (amp / amp_n) if amp_n else 0.0

            # stimulus negative count (should be 0)
            stim_neg = sum(1 for s in stim_eff if isinstance(s, (int, float)) and s < 0)

            # fitness drift (last14 - first14) on training days only (stimulus_eff>0)
            def _avg(xs: list) -> float | None:
                vals = [float(x) for x in xs if isinstance(x, (int, float))]
                return (sum(vals) / len(vals)) if vals else None

            drift = None
            first_n = min(14, n)
            last_n = min(14, n)
            if n >= 14:
                first_idx = list(range(0, first_n))
                last_idx = list(range(n - last_n, n))
                first_fit = [fit[i] for i in first_idx if isinstance(stim_eff[i], (int, float)) and stim_eff[i] > 0]
                last_fit = [fit[i] for i in last_idx if isinstance(stim_eff[i], (int, float)) and stim_eff[i] > 0]
                if len(first_fit) >= 3 and len(last_fit) >= 3:
                    drift = (_avg(last_fit) or 0.0) - (_avg(first_fit) or 0.0)

            # baseline scale min/max
            bvals = [float(x) for x in bscale if isinstance(x, (int, float))]
            bmin = min(bvals) if bvals else None
            bmax = max(bvals) if bvals else None

            pattern_quality[p] = {
                "stimulus_days": len(stim_days),
                "drop_days": drops,
                "drop_rate": drop_rate,
                "norm_amplification_rate": norm_amplification_rate,
                "fitness_drift": drift,
                "stimulus_eff_negative_count": stim_neg,
                "baseline_scale_min": bmin,
                "baseline_scale_max": bmax,
            }

            if norm_amplification_rate > 0.05:
                warnings.append(f"{p}: norm_amplification_rate>5% (baseline_scale bug)")
            if stim_neg > 0:
                warnings.append(f"{p}: stimulus_eff_negative_count>0 (must clamp)")
            if drift is not None and drift < -1e-6:
                warnings.append(f"{p}: fitness_drift<0 (stimulus_eff too low vs decay)")
            if bmin is not None and bmin < 1.0:
                warnings.append(f"{p}: baseline_scale_min<1.0 (must clamp)")

        payload["meta"]["pattern_quality"] = pattern_quality
        if warnings:
            payload["meta"]["warning"] = "; ".join(warnings)

    else:
        # keep legacy lightweight by stripping per-day debug unless explicitly requested
        payload["legacy"] = [{k: r[k] for k in ("date_iso", "mvps", "trend") if k in r} for r in rows]

    return payload


# ============================================================
# TOP-SET HELPERS (DIP-SAFE)
# ============================================================

def _top_sets_per_workout(
    conn: sqlite3.Connection,
    name: str,
    device: str = "",
    laterality: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[sqlite3.Row]:
    """
    Liefert pro WORKOUT genau EINEN Datapoint:
    - bestes Set über ALLE exercise_ids innerhalb dieses Workouts (falls Übung doppelt geloggt)
    - Ranking: weight DESC, reps DESC, rpe DESC, set_number ASC, id ASC
    - plus sets_count (Anzahl gültiger Sets für diese Übung im Workout)

    => verhindert Warmup/Backoff-Dips zu 100%
    """

    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = [
        "TRIM(e.name) = TRIM(?)",
        "COALESCE(e.device,'') = ?",
    ]
    params: list = [name, device or ""]

    if laterality is not None and laterality != "":
        where.append("COALESCE(e.laterality,'bilateral') = ?")
        params.append(laterality)

    if start_iso:
        where.append("w.date_iso >= ?")
        params.append(start_iso)

    if end_iso:
        where.append("w.date_iso <= ?")
        params.append(end_iso)

    where_sql = " AND ".join(where)

    q = f"""
    WITH ex AS (
        SELECT
            w.id      AS workout_id,
            w.date_iso AS date_iso,
            e.id      AS exercise_id
        FROM exercises e
        JOIN workouts w ON w.id = e.workout_id
        WHERE {where_sql}
    ),
    all_sets AS (
        SELECT
            ex.workout_id,
            ex.date_iso,
            s.id AS set_id,
            s.weight,
            s.reps,
            s.rpe,
            s.set_number,
            ROW_NUMBER() OVER (
                PARTITION BY ex.workout_id
                ORDER BY
                    s.weight DESC,
                    s.reps DESC,
                    COALESCE(-s.rpe, -8.0) DESC,
                    COALESCE(s.set_number, 999) ASC,
                    s.id ASC
            ) AS rn
        FROM ex
        JOIN sets s ON s.exercise_id = ex.exercise_id
        WHERE s.weight IS NOT NULL
          AND s.reps   IS NOT NULL
          AND s.weight > 0
          AND s.reps   > 0
    ),
    counts AS (
        SELECT workout_id, COUNT(*) AS sets_count
        FROM all_sets
        GROUP BY workout_id
    )
    SELECT
        a.date_iso,
        a.workout_id,
        a.weight,
        a.reps,
        a.rpe,
        c.sets_count
    FROM all_sets a
    JOIN counts c USING(workout_id)
    WHERE a.rn = 1
    ORDER BY a.date_iso ASC, a.workout_id ASC;
    """

    return cur.execute(q, params).fetchall()


def compare_top_sets(new: dict | None, old: dict | None) -> bool | None:
    if not new or not old:
        return None
    # Long-running legacy histories contain valid weight/reps pairs without
    # RPE. New live logs still require RPE in the canonical session engine,
    # but trend/plateau reads may compare those historical rows via raw e1RM.
    if new.get("rpe") is None:
        current_score = calculate_e1rm(new.get("weight"), new.get("reps"))
        reference_score = calculate_e1rm(old.get("weight"), old.get("reps"))
        if current_score is None or reference_score is None or reference_score <= 0:
            return None
        score_delta_pct = ((current_score / reference_score) - 1.0) * 100.0
        try:
            load_delta_pct = ((float(new.get("weight")) / float(old.get("weight"))) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            load_delta_pct = 0.0
        return bool(
            (
                load_delta_pct >= DEFAULT_CONFIG.load_progression_min_pct
                and score_delta_pct >= DEFAULT_CONFIG.load_progression_score_floor_pct
            )
            or score_delta_pct >= DEFAULT_CONFIG.progress_score_min_pct
        )
    identity = {
        "exercise_name": "top_set_series",
        "canonical_exercise": "top_set_series",
        "device": "series",
        "variation": "default",
        "laterality": "bilateral",
    }
    result = compare_set_progress(
        normalize_set_performance({**(new or {}), **identity}) or SetPerformance(**identity),
        normalize_set_performance({**(old or {}), **identity}) if old else None,
    )
    if not result.comparable:
        return None
    return result.status == "progress"


def classify_status_from_history(rows: list[dict]) -> str:
    """Compatibility label derived exclusively from the shared comparator."""
    if not rows or len(rows) < 2:
        return "neutral"
    if rows[-1].get("rpe") is None:
        current_score = calculate_e1rm(rows[-1].get("weight"), rows[-1].get("reps"))
        reference_score = calculate_e1rm(rows[-2].get("weight"), rows[-2].get("reps"))
        if current_score is None or reference_score is None or reference_score <= 0:
            return "neutral"
        score_delta_pct = ((current_score / reference_score) - 1.0) * 100.0
        try:
            load_delta_pct = ((float(rows[-1].get("weight")) / float(rows[-2].get("weight"))) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            load_delta_pct = 0.0
        if (
            load_delta_pct >= DEFAULT_CONFIG.load_progression_min_pct
            and score_delta_pct >= DEFAULT_CONFIG.load_progression_score_floor_pct
        ) or score_delta_pct >= DEFAULT_CONFIG.progress_score_min_pct:
            return "progress"
        if score_delta_pct <= DEFAULT_CONFIG.regress_score_max_pct:
            return "regress"
        return "plateau"
    identity = {
        "exercise_name": "history_series",
        "canonical_exercise": "history_series",
        "device": "series",
        "variation": "default",
        "laterality": "bilateral",
    }
    result = compare_set_progress(
        normalize_set_performance({**rows[-1], **identity}) or SetPerformance(**identity),
        normalize_set_performance({**rows[-2], **identity}),
        match_level="history_series",
    )
    return {
        "progress": "progress",
        "stable": "plateau",
        "regress": "regress",
    }.get(result.status, "neutral")


def reps_to_reps10(reps: int | None, rpe: float | None) -> float:
    """
    Reps@10: reps + (10 - rpe)
    (wenn rpe fehlt -> rpe=10 -> reps@10=reps)
    """
    if reps is None:
        return 0.0
    try:
        r = float(reps)
    except Exception:
        return 0.0

    p = 10.0
    if rpe is not None:
        try:
            p = float(rpe)
        except Exception:
            p = 10.0

    adj = (10.0 - p)
    return max(0.0, round((r + adj) * 10) / 10.0)


def perf_score(weight: float | None, reps10: float | None) -> float:
    """
    simple e1RM-like proxy (wie dein JS):
    w * (1 + reps10/30)
    """
    if not weight or not reps10:
        return 0.0
    try:
        w = float(weight)
        r = float(reps10)
    except Exception:
        return 0.0
    return w * (1.0 + (r / 30.0))



def compute_pattern_loads_for_workout(workout_id: int, conn) -> dict | None:
    cur = conn.cursor()

    cur.execute("SELECT date_iso FROM workouts WHERE id=?", (workout_id,))
    row = cur.fetchone()
    if not row:
        return None

    date_iso = row["date_iso"]

    cur.execute(
        "SELECT id, name, variation FROM exercises WHERE workout_id=?",
        (workout_id,)
    )
    exercises = cur.fetchall()

    patterns = ["chest", "back", "legs", "shoulders_arms", "core"]
    loads = {p: 0.0 for p in patterns}
    sets_eff = {p: 0.0 for p in patterns}

    set_factor = [1.0, 0.9, 0.8, 0.6, 0.4, 0.25, 0.15]

    for ex in exercises:
        pattern = classify_pattern(ex["name"], ex["variation"] or "")
        if pattern not in loads:
            continue

        cur.execute("""
            SELECT reps, weight, rpe
            FROM sets
            WHERE exercise_id=?
            ORDER BY set_number
        """, (ex["id"],))
        rows = cur.fetchall()

        for i, s in enumerate(rows):
            reps = s["reps"] or 0
            weight = s["weight"] or 0
            if reps <= 0 or weight <= 0:
                continue

            rpe = s["rpe"] or 9
            rpe_factor = (
                1.25 if rpe >= 10 else
                1.15 if rpe >= 9 else
                1.05 if rpe >= 8 else
                1.0
            )

            sf = set_factor[i] if i < len(set_factor) else set_factor[-1]

            loads[pattern] += reps * weight * rpe_factor * sf
            sets_eff[pattern] += sf

    return {
        "date_iso": date_iso,
        "pattern_loads": loads,
        "pattern_sets": sets_eff,
    }


# ============================================================
# VOLUME MODIFIER (MEV / MAV / MRV)
# ============================================================

def _volume_modifiers(eff_sets: float, p: dict) -> tuple[float, float]:
    if eff_sets <= 0:
        return 0.0, 1.0

    if eff_sets <= p["mev"]:
        return 0.6 * eff_sets / p["mev"], 0.8

    if eff_sets <= p["opt_high"]:
        span = p["opt_high"] - p["mev"]
        return 0.6 + 0.4 * ((eff_sets - p["mev"]) / span), 1.0

    if eff_sets <= p["mrv"]:
        span = p["mrv"] - p["opt_high"]
        over = (eff_sets - p["opt_high"]) / span
        return 1.0 - 0.3 * over, 1.1 + 0.4 * over

    return 0.5, 1.8


# ============================================================
# MVPS TIMESERIES (wie gehabt)
# ============================================================

def compute_mvps_timeseries(conn, debug: bool = False) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, date_iso FROM workouts ORDER BY date_iso ASC")
    workouts = cur.fetchall()
    if not workouts:
        return []

    patterns = ["chest", "back", "legs", "shoulders_arms", "core"]

    # Aggregate per date (supports multiple workouts/day)
    day_inputs: dict[str, dict] = {}
    for w in workouts:
        info = compute_pattern_stimulus_for_workout(w["id"], conn)
        if not info:
            continue
        d_iso = info["date_iso"]

        day_inputs.setdefault(d_iso, {
            "set_score_sum": {p: 0.0 for p in patterns},
            "effective_sets": {p: 0.0 for p in patterns},
            "hard_sets": {p: 0 for p in patterns},
        })

        for p in patterns:
            day_inputs[d_iso]["set_score_sum"][p] += float(info["patterns"][p]["set_score_sum"])
            day_inputs[d_iso]["effective_sets"][p] += float(info["patterns"][p]["effective_sets"])
            day_inputs[d_iso]["hard_sets"][p] += int(info["patterns"][p]["hard_sets"])

    # date range: from first workout to today
    start = date.fromisoformat(workouts[0]["date_iso"])
    end = date.today()
    dates = []
    cur_date = start
    while cur_date <= end:
        dates.append(cur_date)
        cur_date += timedelta(days=1)

    window_days = MVPS_V2_WINDOW_DAYS
    baselines = _rolling_median_baselines(
        dates=dates,
        workout_scores_by_date=day_inputs,
        patterns=patterns,
        window_days=window_days,
        min_baseline=0.1,
        ema_span_days=MVPS_V2_BASELINE_EMA_SPAN_DAYS,
    )

    return _simulate_fitness_fatigue(
        dates=dates,
        day_inputs=day_inputs,
        baselines=baselines,
        params=MVPS_V2_PARAMS,
        patterns=patterns,
        exp=MVPS_V2_EXP,
        trend_span_days=MVPS_V2_TREND_SPAN_DAYS,
        mvps_floor=MVPS_V2_MVPS_FLOOR,
        mvps_cap=MVPS_V2_MVPS_CAP,
        cap_k=MVPS_V2_CAP_K,
        mvps_norm_span_days=MVPS_V2_MVPS_NORM_SPAN_DAYS,
        stimulus_scale_span_days=MVPS_V2_STIMULUS_SCALE_SPAN_DAYS,
        stimulus_scale_decay=MVPS_V2_STIMULUS_SCALE_DECAY,
        debug=debug,
    )


def mvps_synthetic_scenario(days: int = 8) -> list[dict]:
    """
    Dev helper: 1 training day -> rest -> visible rebound.
    Call:
      python -c "from analysis.training_analysis import mvps_synthetic_scenario; import json; print(json.dumps(mvps_synthetic_scenario(), indent=2)[:1200])"
    """
    patterns = ["chest", "back", "legs", "shoulders_arms", "core"]
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=i) for i in range(days)]

    # one hard chest stimulus on day 0 (baseline set so stimulus~1.0)
    day_inputs = {
        dates[0].isoformat(): {
            "set_score_sum": {p: 0.0 for p in patterns} | {"chest": 10.0},
            "effective_sets": {p: 0.0 for p in patterns} | {"chest": 12.0},
            "hard_sets": {p: 0 for p in patterns} | {"chest": 6},
        }
    }

    # static baseline score-scale (so score/baseline ~= 1.0)
    base = {p: 1.0 for p in patterns} | {"chest": 10.0}
    baselines = {d.isoformat(): {"raw": dict(base), "ema": dict(base)} for d in dates}

    rows = _simulate_fitness_fatigue(
        dates=dates,
        day_inputs=day_inputs,
        baselines=baselines,
        params=MVPS_V2_PARAMS,
        patterns=patterns,
        exp=MVPS_V2_EXP,
        trend_span_days=MVPS_V2_TREND_SPAN_DAYS,
        mvps_floor=MVPS_V2_MVPS_FLOOR,
        mvps_cap=MVPS_V2_MVPS_CAP,
        cap_k=MVPS_V2_CAP_K,
        mvps_norm_span_days=MVPS_V2_MVPS_NORM_SPAN_DAYS,
        stimulus_scale_span_days=MVPS_V2_STIMULUS_SCALE_SPAN_DAYS,
        stimulus_scale_decay=MVPS_V2_STIMULUS_SCALE_DECAY,
        debug=True,
    )
    return rows


# ============================================================
# OVERALL PROGRESS (Top-Set statt First-Set)
# ============================================================


def get_last_two_top_sets(conn, name, device, laterality):
    rows = _top_sets_per_workout(
        conn,
        name=name,
        device=device or "",
        laterality=laterality or "bilateral",
        start_iso=None,
        end_iso=None,
    )
    if len(rows) < 2:
        return None

    # rows sind ASC -> letzte 2
    a = rows[-1]
    b = rows[-2]

    return {
        "latest": {
            "date": a["date_iso"],
            "weight": a["weight"],
            "reps": a["reps"],
            "rpe": a["rpe"],
            "sets_count": a["sets_count"],
        },
        "previous": {
            "date": b["date_iso"],
            "weight": b["weight"],
            "reps": b["reps"],
            "rpe": b["rpe"],
            "sets_count": b["sets_count"],
        }
    }


def compute_trend_from_history(rows, lookback=4):
    """
    Trend = Median(2 letzte) vs. Median(2 erste) innerhalb der letzten lookback Einheiten.
    - up:   Median zweite Hälfte > Median erste Hälfte
    - flat: gleich
    - down: kleiner
    Fallback für <4 Einheiten: Median der zweiten Hälfte vs. ersten Hälfte (split in zwei Teile).
    """

    if not rows:
        return "neutral"

    def to_num(x):
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    # Clean: nur gültige Punkte
    clean = []
    for r in rows:
        w = to_num(r.get("weight"))
        reps = to_num(r.get("reps"))
        if w is None or reps is None:
            continue
        clean.append({"weight": w, "reps": reps})

    if len(clean) < 2:
        return "neutral"

    recent = clean[-max(2, int(lookback)):]
    n = len(recent)
    if n < 2:
        return "neutral"

    # Score: Gewicht dominiert, Reps sind Feinabstufung
    # (0.5kg -> +50 Punkte; +1 Rep -> +1 Punkt)
    scores = [(r["weight"] * 100.0) + r["reps"] for r in recent]

    # Split in zwei Hälften (für n=4 -> 2 und 2, genau wie gewünscht)
    mid = n // 2
    first = scores[:mid]
    second = scores[mid:]

    if not first or not second:
        return "neutral"

    m1 = median(first)
    m2 = median(second)

    eps = 1e-9
    if m2 > m1 + eps:
        return "up"
    if m2 < m1 - eps:
        return "down"
    return "flat"


def weekly_set_progress(conn, start_iso: str, end_iso: str, prev_start_iso: str, prev_end_iso: str) -> dict:
    """Aggregate canonical session payloads for a weekly display window."""
    del prev_start_iso, prev_end_iso
    series = compute_canonical_progress_series(conn, start_iso=start_iso, end_iso=end_iso)
    points = series.get("points") or []
    comparable = sum(int(point.get("comparable_sets") or 0) for point in points)
    improved = sum(int(point.get("improved") or 0) for point in points)
    same = sum(int(point.get("same") or 0) for point in points)
    worse = sum(int(point.get("worse") or 0) for point in points)
    return {
        "rule_version": RULE_VERSION,
        "improved": improved,
        "same": same,
        "worse": worse,
        "total": comparable,
        "percent": round(100.0 * improved / comparable, 1) if comparable else None,
        "net_progress": (improved - worse) / comparable if comparable else None,
        "basis_small": comparable < 20,
    }


def moving_average(values, window=3):
    """
    Simple moving average, returns list with same length as input.
    Ignores None values inside the window.
    """
    if not values:
        return []

    window = max(1, int(window))
    out = []

    for i in range(len(values)):
        start = max(0, i - window + 1)
        chunk = [v for v in values[start:i+1] if v is not None]
        if not chunk:
            out.append(None)
        else:
            out.append(sum(chunk) / len(chunk))

    return out



def _safe_float(value):
    try:
        if value is None:
            return None
        v = float(value)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _safe_int(value):
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _norm_txt(value: str | None) -> str:
    return (value or "").strip().lower()


def _week_floor(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _bin_start_ms_local(d: date) -> int:
    """
    Returns local (Europe/Berlin) midnight timestamp in milliseconds.
    """
    dt = datetime.combine(d, time.min).replace(tzinfo=ZoneInfo("Europe/Berlin"))
    return int(dt.timestamp() * 1000)


def _series_bin_points(start_d: date, end_d: date, *, binning: str) -> list[date]:
    if start_d > end_d:
        return []
    out: list[date] = []
    if binning == "day":
        d = start_d
        while d <= end_d:
            out.append(d)
            d += timedelta(days=1)
        return out

    d = _week_floor(start_d)
    end_floor = _week_floor(end_d)
    while d <= end_floor:
        out.append(d)
        d += timedelta(days=7)
    return out


def _set_perf_to_row(s: SetPerformance | None) -> dict:
    if s is None:
        return {}
    return {
        "exercise_name": s.exercise_name,
        "canonical_exercise": s.canonical_exercise,
        "variation": s.variation,
        "device": s.device,
        "laterality": s.laterality,
        "session_name": s.session_name,
        "set_slot": s.set_number,
        "set_number": s.set_number,
        "order_idx": s.order_idx,
        "weight": s.weight,
        "reps": s.reps,
        "rpe": s.rpe,
        "e1rm": s.e1rm,
        "tb_type": s.tb_type,
        "tb_slot": s.tb_slot,
        "source_set_id": s.source_set_id,
    }


def _set_lookup_keys(row: dict) -> list[tuple]:
    ex = row.get("canonical_exercise") or row.get("exercise_name", "")
    session_name = row.get("session_name", "")
    slot = int(row.get("tb_slot") or row.get("set_slot") or 0)
    tb_type = row.get("tb_type")
    return [
        (ex, session_name, tb_type, slot),
        (ex, tb_type, slot),
        (ex, session_name),
        (ex,),
    ]


def _build_session_lookup_index(session_sets: list[dict]) -> dict[int, dict[tuple, list[dict]]]:
    idx: dict[int, dict[tuple, list[dict]]] = {0: {}, 1: {}, 2: {}, 3: {}}
    for s in session_sets:
        keys = _set_lookup_keys(s)
        for lvl, key in enumerate(keys):
            idx[lvl].setdefault(key, []).append(s)
    return idx


def _pick_best_candidate(candidates: list[dict], current: dict) -> dict | None:
    picked = choose_best_reference_candidate(
        normalize_set_performance(current) or SetPerformance(),
        [s for s in (normalize_set_performance(c) for c in candidates) if s is not None],
    )
    return _set_perf_to_row(picked) if picked else None


def _lookup_baseline_in_prior_sessions(
    sessions: list[dict],
    cur_session_idx: int,
    cur_set: dict,
    baseline_lookback_sessions: int,
) -> tuple[dict | None, int | None]:
    max_back = max(1, int(baseline_lookback_sessions))
    lower_bound = max(0, cur_session_idx - max_back)
    previous_sessions = [s["sets"] for s in sessions[lower_bound:cur_session_idx]]
    ref, match_level = find_reference_set(
        normalize_set_performance(cur_set) or SetPerformance(),
        previous_sessions,
        DEFAULT_CONFIG,
    )
    if ref is None:
        return None, None
    lookback = None
    if ":lookback_" in match_level:
        try:
            lookback = int(match_level.rsplit(":lookback_", 1)[1]) - 1
        except Exception:
            lookback = None
    row = _set_perf_to_row(ref)
    row["_match_level"] = match_level
    return row, lookback


def _classify_set_progress(
    ref_row: dict | None,
    cur_row: dict,
    *,
    rpe_epsilon: float = 0.25,
    e1rm_epsilon: float = 0.005,
) -> str:
    config = ProgressRuleConfig(
        e1rm_epsilon_pct=float(e1rm_epsilon),
        rpe_epsilon=float(rpe_epsilon),
        weight_epsilon=DEFAULT_CONFIG.weight_epsilon,
        min_comparable_per_session=DEFAULT_CONFIG.min_comparable_per_session,
        top_backoff_pct=DEFAULT_CONFIG.top_backoff_pct,
        top_backoff_abs_threshold=DEFAULT_CONFIG.top_backoff_abs_threshold,
    )
    result = compare_set_progress(
        normalize_set_performance(cur_row) or SetPerformance(),
        normalize_set_performance(ref_row),
        config,
        match_level=str((ref_row or {}).get("_match_level") or "legacy_wrapper"),
    )
    return result.status


def _rolling_average_strict(values: list[float | None], window: int) -> list[float | None]:
    window = max(1, int(window))
    out: list[float | None] = []
    for i in range(len(values)):
        if i < window - 1:
            out.append(None)
            continue
        chunk = values[i - window + 1 : i + 1]
        if any(v is None for v in chunk):
            out.append(None)
            continue
        out.append(sum(float(v) for v in chunk) / float(window))
    return out


def _rolling_weighted_average(
    values: list[float | None],
    weights: list[float | None],
    window: int,
    *,
    min_valid_points: int = 1,
) -> list[float | None]:
    window = max(1, int(window))
    min_valid = max(1, int(min_valid_points))
    out: list[float | None] = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        v_chunk = values[start : i + 1]
        w_chunk = weights[start : i + 1]
        sum_w = 0.0
        sum_vw = 0.0
        valid_points = 0
        for v, w in zip(v_chunk, w_chunk, strict=False):
            if v is None:
                continue
            ww = float(w or 0.0)
            if ww <= 0:
                continue
            sum_w += ww
            sum_vw += (float(v) * ww)
            valid_points += 1
        if valid_points < min_valid:
            out.append(None)
            continue
        out.append((sum_vw / sum_w) if sum_w > 0 else None)
    return out


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except Exception:
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


def _normalize_exercise_match_name(value: str | None) -> str:
    return " ".join(_norm_txt(value).split())


def _exercise_history_key(row: dict) -> str:
    canonical = _norm_txt(row.get("canonical_exercise") or "")
    if canonical:
        return f"canonical:{canonical}"

    name = _normalize_exercise_match_name(row.get("exercise_name"))
    device = _normalize_exercise_match_name(row.get("device"))
    variation = _normalize_exercise_match_name(row.get("variation"))
    laterality = _normalize_exercise_match_name(row.get("laterality"))

    return f"name:{name}|device:{device}|variation:{variation}|laterality:{laterality}"


def _sorted_comparable_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("set_number") or item.get("set_slot") or 0),
            int(item.get("order_idx") or 0),
            int(item.get("source_set_id") or 0),
        ),
    )


def _is_current_set_eligible(row: dict) -> tuple[bool, str | None]:
    weight = _safe_float(row.get("weight"))
    reps = _safe_float(row.get("reps"))
    if weight is None or weight <= 0:
        return False, "missing_weight"
    if reps is None or reps <= 0:
        return False, "missing_reps"
    for warmup_key in ("is_warmup", "warmup"):
        if row.get(warmup_key) is True:
            return False, "flagged_warmup"
    for work_key in ("is_working_set", "working_set"):
        if row.get(work_key) is False:
            return False, "flagged_non_working_set"
    return True, None


def compute_default_set_progress_series(
    conn: sqlite3.Connection,
    *,
    start_iso: str,
    end_iso: str,
    trend_window_sessions: int = 6,
    baseline_lookback_sessions: int = 8,
    min_comparable_per_session: int = 6,
    rpe_epsilon: float = 0.25,
    e1rm_epsilon: float = 0.005,
    debug: bool = False,
) -> dict:
    """
    Default-Graph Aggregation (ungefiltert, session-level):
      - Comparator pro Set gegen letzte vergleichbare Einheit
      - 1 Datenpunkt pro Session/Workout
      - progress_rate = improved / comparable (NEW nicht im Nenner)
    """
    # Compatibility entrypoint. V3 ignores the former general-workout
    # lookback and minimum-count switches; every consumer receives the same
    # full-history, confidence-bearing payload from the canonical engine.
    del baseline_lookback_sessions, min_comparable_per_session, rpe_epsilon, e1rm_epsilon
    return compute_canonical_progress_series(
        conn,
        start_iso=start_iso,
        end_iso=end_iso,
        trend_window_sessions=trend_window_sessions,
        debug=debug,
    )



def get_exercise_history(conn, exercise, device=None, laterality=None, variation=None, start_iso=None, end_iso=None):
    """
    Returns one datapoint per workout/exercise instance:
    top set (highest weight; tie -> higher reps) with reps/rpe + sets_count.
    IMPORTANT: device filter is HARD when provided.
    laterality is optional and only applied when provided.
    Variation is optional and filters by the logged variation string.
    """

    if not exercise:
        return []

    where = ["e.name = :exercise"]
    params = {"exercise": exercise}

    requested_device = (str(device).strip() if device is not None else "")
    requested_device_norm = requested_device.lower()
    exercise_norm = str(exercise or "").strip().lower()
    pushdown_mode_filter = exercise_norm in {"pushdown", "pushdowns"} and requested_device_norm in {"bilat", "unilat", "bilateral", "unilateral"}

    # HARD device filter by default; pushdown bilat/unilat is handled post-query with legacy fallback.
    if requested_device and not pushdown_mode_filter:
        where.append("e.device = :device")
        params["device"] = requested_device

    if laterality is not None and str(laterality).strip() != "":
        where.append("e.laterality = :laterality")
        params["laterality"] = laterality

    if variation is not None and str(variation).strip() != "":
        where.append("LOWER(TRIM(COALESCE(e.variation, ''))) = :variation")
        params["variation"] = variation.strip().lower()

    if start_iso is not None and str(start_iso).strip() != "":
        where.append("SUBSTR(w.date_iso, 1, 10) >= :start_iso")
        params["start_iso"] = str(start_iso).strip()[:10]
    if end_iso is not None and str(end_iso).strip() != "":
        where.append("SUBSTR(w.date_iso, 1, 10) <= :end_iso")
        params["end_iso"] = str(end_iso).strip()[:10]

    sql = f"""
    WITH ranked AS (
        SELECT
            w.date_iso AS date,
            e.id       AS exercise_id,
            s.weight   AS weight,
            s.reps     AS reps,
            s.rpe      AS rpe,
            COALESCE(TRIM(e.device), '') AS device,
            COALESCE(TRIM(e.variation), '') AS variation,
            COUNT(*) OVER (PARTITION BY e.id) AS sets_count,
            ROW_NUMBER() OVER (
                PARTITION BY e.id
                ORDER BY
                    s.weight DESC,
                    COALESCE(s.reps, -1) DESC,
                    COALESCE(-s.rpe, -8.0) DESC,
                    s.id ASC
            ) AS rn
        FROM exercises e
        JOIN workouts w ON w.id = e.workout_id
        JOIN sets s     ON s.exercise_id = e.id
        WHERE {" AND ".join(where)}
    )
    SELECT date, weight, reps, rpe, sets_count, device, variation
    FROM ranked
    WHERE rn = 1
    ORDER BY date;
    """

    cur = conn.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]

    if pushdown_mode_filter:
        want = "unilat" if requested_device_norm in {"unilat", "unilateral"} else "bilat"

        def _pushdown_mode(row: dict) -> str:
            dev = str(row.get("device") or "").strip().lower()
            var = str(row.get("variation") or "").strip().lower()
            if "unilat" in var or "unilateral" in var:
                return "unilat"
            if "bilat" in var or "bilateral" in var:
                return "bilat"
            if dev in {"unilat", "unilateral"}:
                return "unilat"
            if dev in {"bilat", "bilateral"}:
                return "bilat"
            return "bilat"

        rows = [r for r in rows if _pushdown_mode(r) == want]

    for r in rows:
        r.pop("device", None)
        r.pop("variation", None)
    return rows
