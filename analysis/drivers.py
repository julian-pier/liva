from __future__ import annotations

from datetime import date, datetime
from math import isfinite, log
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo
import copy
import sqlite3
import time

from analysis.radar import load_daily_series


TZ_BERLIN = ZoneInfo("Europe/Berlin")

METRIC_META: dict[str, dict[str, str]] = {
    "rmssd": {"label": "RMSSD", "unit": "ms"},
    "rhr": {"label": "RHR", "unit": "bpm"},
    "weight": {"label": "Gewicht", "unit": "kg"},
    "kcal": {"label": "Kalorien", "unit": "kcal"},
    "volume": {"label": "Volumen", "unit": "t"},
    "cardio": {"label": "Cardio", "unit": "min"},
}

METRIC_ORDER = ["rmssd", "rhr", "weight", "kcal", "volume", "cardio"]
ALLOWED_DAYS = {28, 56, 90}
ALLOWED_MODES = {"raw", "detrended"}
ALLOWED_LAGS = {"auto", "0", "1", "2", "3"}

_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 75.0


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    if not isfinite(x):
        return None
    return x


def _date_range(end_date: date, days: int) -> list[str]:
    start = end_date.toordinal() - max(1, int(days)) + 1
    return [date.fromordinal(start + i).isoformat() for i in range(max(1, int(days)))]


def _metric_transform(metric: str, v: float | None) -> float | None:
    if v is None:
        return None
    if metric == "volume":
        # internal tonnage (kg*reps) -> tons for human-readable scatter/chips
        return v / 1000.0
    return v


def _build_series_arrays(
    *,
    series_maps: dict[str, dict[str, float]],
    metrics: list[str],
    dates: list[str],
) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {}
    for m in metrics:
        dmap = series_maps.get(m, {})
        arr: list[float | None] = []
        for d in dates:
            arr.append(_metric_transform(m, _to_float(dmap.get(d))))
        out[m] = arr
    return out


def _first_diff(arr: list[float | None]) -> list[float | None]:
    if not arr:
        return []
    out: list[float | None] = [None]
    for i in range(1, len(arr)):
        a = arr[i]
        b = arr[i - 1]
        if a is None or b is None:
            out.append(None)
        else:
            out.append(a - b)
    return out


def _rankdata_average_ties(values: list[float]) -> list[float]:
    pairs = sorted((v, i) for i, v in enumerate(values))
    ranks = [0.0] * len(values)
    i = 0
    n = len(pairs)
    while i < n:
        j = i + 1
        while j < n and pairs[j][0] == pairs[i][0]:
            j += 1
        # 1-based average rank in tie block [i..j-1]
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[pairs[k][1]] = avg_rank
        i = j
    return ranks


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 3:
        return None
    mx = mean(x)
    my = mean(y)
    num = 0.0
    dx2 = 0.0
    dy2 = 0.0
    for i in range(n):
        dx = x[i] - mx
        dy = y[i] - my
        num += dx * dy
        dx2 += dx * dx
        dy2 += dy * dy
    den = (dx2 * dy2) ** 0.5
    if den <= 0:
        return None
    return num / den


def spearman_corr(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    rx = _rankdata_average_ties(x)
    ry = _rankdata_average_ties(y)
    return _pearson(rx, ry)


def _percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    arr = sorted(vals)
    if len(arr) == 1:
        return arr[0]
    pp = max(0.0, min(1.0, p))
    idx = (len(arr) - 1) * pp
    lo = int(idx)
    hi = min(len(arr) - 1, lo + 1)
    frac = idx - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _linreg(x: list[float], y: list[float]) -> tuple[float, float] | None:
    if len(x) < 10:
        return None
    mx = mean(x)
    my = mean(y)
    num = 0.0
    den = 0.0
    for i in range(len(x)):
        dx = x[i] - mx
        num += dx * (y[i] - my)
        den += dx * dx
    if den <= 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    return slope, intercept


def _mode_label(mode: str) -> str:
    return "De-trended" if mode == "detrended" else "Raw"


def _build_insight(
    *,
    r: float | None,
    n: int,
    lag: int,
    driver_key: str,
    outcome_key: str,
    mode: str,
) -> str:
    if n < 20 or r is None:
        return "Zu wenig Daten für belastbare Aussage."
    driver = METRIC_META.get(driver_key, {}).get("label", driver_key)
    outcome = METRIC_META.get(outcome_key, {}).get("label", outcome_key)
    relation = "hängt mit höherem" if r > 0 else "hängt mit niedrigerem"
    lag_txt = "heute" if lag == 0 else ("gestern" if lag == 1 else f"vor {lag} Tagen")
    prefix = "Änderung in" if mode == "detrended" else "Mehr"
    if mode == "detrended":
        return f"{prefix} {driver} {lag_txt} {relation} {outcome} heute zusammen."
    return f"Mehr {driver} {lag_txt} {relation} {outcome} heute zusammen."


def _pairs_for_lag(
    *,
    dates_ext: list[str],
    outcome_arr: list[float | None],
    driver_arr: list[float | None],
    window_start_idx: int,
    lag: int,
) -> list[dict[str, float | str]]:
    pairs: list[dict[str, float | str]] = []
    for i in range(window_start_idx, len(dates_ext)):
        j = i - lag
        if j < 0:
            continue
        y = outcome_arr[i]
        x = driver_arr[j]
        if x is None or y is None:
            continue
        pairs.append({"date": dates_ext[i], "x": float(x), "y": float(y)})
    return pairs


def _eval_driver(
    *,
    dates_ext: list[str],
    outcome_arr: list[float | None],
    driver_arr: list[float | None],
    window_start_idx: int,
    lag: int,
) -> tuple[float | None, int, list[dict[str, float | str]]]:
    pairs = _pairs_for_lag(
        dates_ext=dates_ext,
        outcome_arr=outcome_arr,
        driver_arr=driver_arr,
        window_start_idx=window_start_idx,
        lag=lag,
    )
    x = [p["x"] for p in pairs]
    y = [p["y"] for p in pairs]
    r = spearman_corr(x, y) if len(pairs) >= 3 else None
    return r, len(pairs), pairs


def _active_from_pairs(
    *,
    pairs: list[dict[str, float | str]],
    driver_key: str,
    outcome_key: str,
    lag: int,
    mode: str,
    r: float | None,
    n: int,
) -> dict[str, Any]:
    xs = [float(p["x"]) for p in pairs]
    ys = [float(p["y"]) for p in pairs]

    if len(xs) >= 5:
        x_lo = _percentile(xs, 0.02)
        x_hi = _percentile(xs, 0.98)
        y_lo = _percentile(ys, 0.02)
        y_hi = _percentile(ys, 0.98)
    else:
        x_lo = min(xs) if xs else None
        x_hi = max(xs) if xs else None
        y_lo = min(ys) if ys else None
        y_hi = max(ys) if ys else None

    points = []
    for p in pairs:
        x = float(p["x"])
        y = float(p["y"])
        outlier = False
        x_plot = x
        y_plot = y
        if x_lo is not None and x_hi is not None and x_lo < x_hi:
            if x < x_lo or x > x_hi:
                outlier = True
            x_plot = _clip(x, x_lo, x_hi)
        if y_lo is not None and y_hi is not None and y_lo < y_hi:
            if y < y_lo or y > y_hi:
                outlier = True
            y_plot = _clip(y, y_lo, y_hi)
        points.append(
            {
                "date": p["date"],
                "x": x,
                "y": y,
                "x_plot": x_plot,
                "y_plot": y_plot,
                "outlier": outlier,
            }
        )

    trend = None
    reg = _linreg(xs, ys)
    if reg is not None:
        slope, intercept = reg
        trend = {"method": "linear", "slope": slope, "intercept": intercept}

    driver_label = METRIC_META.get(driver_key, {}).get("label", driver_key)
    driver_unit = METRIC_META.get(driver_key, {}).get("unit", "")
    outcome_label = METRIC_META.get(outcome_key, {}).get("label", outcome_key)

    x_prefix = "Δ " if mode == "detrended" else ""
    y_prefix = "Δ " if mode == "detrended" else ""
    lag_txt = f" (lag {lag}d)" if lag > 0 else ""
    x_label = f"{x_prefix}{driver_label}{lag_txt}" + (f" [{driver_unit}]" if driver_unit else "")
    y_label = f"{y_prefix}{outcome_label}"

    subtitle_r = "—" if r is None else f"{r:+.2f}"
    subtitle = f"Spearman r = {subtitle_r} (N={n}), Lag {lag}d, {_mode_label(mode)}"

    insight = _build_insight(
        r=r,
        n=n,
        lag=lag,
        driver_key=driver_key,
        outcome_key=outcome_key,
        mode=mode,
    )

    return {
        "driver_key": driver_key,
        "lag": lag,
        "r": r,
        "n": n,
        "scatter": {
            "x_label": x_label,
            "y_label": y_label,
            "points": points,
            "trend": trend,
            "subtitle": subtitle,
        },
        "insight": insight,
    }


def _cache_get(key: tuple) -> dict[str, Any] | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, payload = entry
    if (time.time() - ts) > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def _cache_put(key: tuple, payload: dict[str, Any]) -> None:
    _CACHE[key] = (time.time(), copy.deepcopy(payload))


def build_drivers_payload(
    *,
    training_conn: sqlite3.Connection,
    nutrition_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    outcome: str,
    days: int,
    mode: str,
    lag: str,
    active_driver: str | None,
    end_date: date | None = None,
) -> dict[str, Any]:
    end = end_date or datetime.now(TZ_BERLIN).date()
    out_key = outcome if outcome in METRIC_META else "rmssd"
    days_n = days if days in ALLOWED_DAYS else 56
    mode_n = mode if mode in ALLOWED_MODES else "detrended"
    lag_n = lag if lag in ALLOWED_LAGS else "auto"

    cache_key = (end.isoformat(), out_key, days_n, mode_n, lag_n, active_driver or "")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    max_lag = 3
    ext_days = days_n + max_lag + 2

    series_maps = load_daily_series(
        training_conn=training_conn,
        nutrition_conn=nutrition_conn,
        hrv_conn=hrv_conn,
        runs_conn=runs_conn,
        end_date=end,
        days=ext_days,
    )

    dates_ext = _date_range(end, ext_days)
    dates_window = dates_ext[-days_n:]
    window_start_idx = len(dates_ext) - days_n

    metrics = [m for m in METRIC_ORDER if m in series_maps]
    aligned = _build_series_arrays(series_maps=series_maps, metrics=metrics, dates=dates_ext)

    if mode_n == "detrended":
        aligned_proc = {m: _first_diff(aligned[m]) for m in metrics}
    else:
        aligned_proc = aligned

    outcome_arr = aligned_proc.get(out_key, [None] * len(dates_ext))

    lag_candidates = [0, 1, 2, 3]
    drivers = [m for m in metrics if m != out_key]
    ranked: list[dict[str, Any]] = []
    eval_details: dict[tuple[str, int], tuple[float | None, int, list[dict[str, float | str]]]] = {}

    for dkey in drivers:
        darr = aligned_proc.get(dkey, [None] * len(dates_ext))

        if lag_n == "auto":
            best_lag = 0
            best_r = None
            best_n = 0
            best_abs = -1.0
            for l in lag_candidates:
                r, n, pairs = _eval_driver(
                    dates_ext=dates_ext,
                    outcome_arr=outcome_arr,
                    driver_arr=darr,
                    window_start_idx=window_start_idx,
                    lag=l,
                )
                eval_details[(dkey, l)] = (r, n, pairs)
                if n < 20 or r is None:
                    continue
                cur = abs(r)
                if cur > best_abs:
                    best_abs = cur
                    best_lag = l
                    best_r = r
                    best_n = n
            if best_r is None:
                # fallback: choose lag with max N to still render scatter
                max_n = -1
                for l in lag_candidates:
                    r, n, _ = eval_details.get((dkey, l), (None, 0, []))
                    if n > max_n:
                        max_n = n
                        best_lag = l
                        best_r = r
                        best_n = n
        else:
            forced_lag = int(lag_n)
            r, n, pairs = _eval_driver(
                dates_ext=dates_ext,
                outcome_arr=outcome_arr,
                driver_arr=darr,
                window_start_idx=window_start_idx,
                lag=forced_lag,
            )
            eval_details[(dkey, forced_lag)] = (r, n, pairs)
            best_lag = forced_lag
            best_r = r
            best_n = n

        score = (abs(best_r) * log(max(2, best_n))) if (best_r is not None and best_n >= 20) else -1.0
        ranked.append(
            {
                "driver_key": dkey,
                "label": METRIC_META.get(dkey, {}).get("label", dkey),
                "unit": METRIC_META.get(dkey, {}).get("unit", ""),
                "best_lag": best_lag,
                "r": round(best_r, 4) if best_r is not None and best_n >= 20 else None,
                "n": best_n,
                "direction": "pos" if (best_r or 0) > 0 else ("neg" if (best_r or 0) < 0 else "flat"),
                "notes": "Spearman" if best_n >= 20 else "zu wenig Daten",
                "_score": score,
            }
        )

    ranked.sort(key=lambda x: (x.get("_score", -1.0), x.get("n", 0)), reverse=True)
    ranked = ranked[:6]
    for row in ranked:
        row.pop("_score", None)

    selected_driver = None
    if active_driver and active_driver in drivers and active_driver != out_key:
        selected_driver = active_driver
    elif ranked:
        selected_driver = ranked[0]["driver_key"]
    elif drivers:
        selected_driver = drivers[0]

    if selected_driver:
        if lag_n == "auto":
            selected_lag = next((int(r["best_lag"]) for r in ranked if r["driver_key"] == selected_driver), 0)
        else:
            selected_lag = int(lag_n)
        if (selected_driver, selected_lag) in eval_details:
            r, n, pairs = eval_details[(selected_driver, selected_lag)]
        else:
            r, n, pairs = _eval_driver(
                dates_ext=dates_ext,
                outcome_arr=outcome_arr,
                driver_arr=aligned_proc.get(selected_driver, [None] * len(dates_ext)),
                window_start_idx=window_start_idx,
                lag=selected_lag,
            )
        active = _active_from_pairs(
            pairs=pairs,
            driver_key=selected_driver,
            outcome_key=out_key,
            lag=selected_lag,
            mode=mode_n,
            r=r,
            n=n,
        )
    else:
        active = {
            "driver_key": None,
            "lag": 0,
            "r": None,
            "n": 0,
            "scatter": {
                "x_label": "—",
                "y_label": METRIC_META.get(out_key, {}).get("label", out_key),
                "points": [],
                "trend": None,
                "subtitle": "Zu wenig Daten",
            },
            "insight": "Zu wenig Daten für belastbare Aussage.",
        }

    payload = {
        "meta": {"outcome": out_key, "days": days_n, "mode": mode_n, "lag": lag_n},
        "outcome": {
            "key": out_key,
            "label": METRIC_META.get(out_key, {}).get("label", out_key),
            "unit": METRIC_META.get(out_key, {}).get("unit", ""),
        },
        "drivers_ranked": ranked,
        "active": active,
        "options": {
            "outcomes": [{"key": k, **METRIC_META[k]} for k in METRIC_ORDER],
            "days": sorted(ALLOWED_DAYS),
            "modes": ["raw", "detrended"],
            "lags": ["auto", "0", "1", "2", "3"],
        },
    }

    _cache_put(cache_key, payload)
    return payload
