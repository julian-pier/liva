from datetime import date, timedelta
from statistics import mean

from database.connections import (
    get_nutrition_db,
    get_runs_db,
    get_training_db,
)

# ============================================================
#   HELPERS (lokal, bewusst hier)
# ============================================================

def safe_num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def safe_round(v, digits=0):
    if v is None:
        return None
    return round(v, digits)


# ============================================================
#   GEWICHT & KALORIEN – 7-TAGE-TRENDS
# ============================================================

def get_last_7_and_prev_7():
    conn = get_nutrition_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT date_iso, weight_kg, kcal
        FROM weight_logs
        ORDER BY date_iso DESC
        LIMIT 60
    """)
    rows = cur.fetchall()
    conn.close()

    weights = [safe_num(r[1]) for r in rows if r[1] is not None]
    kcals   = [safe_num(r[2]) for r in rows if r[2] is not None]

    last7_w = weights[:7]
    prev7_w = weights[7:14]
    last7_k = kcals[:7]

    def avg(x):
        return sum(x) / len(x) if x else None

    avg_w_7     = avg(last7_w)
    avg_w_prev  = avg(prev7_w)
    avg_k_7     = avg(last7_k)

    weight_diff = (
        avg_w_7 - avg_w_prev
        if avg_w_7 is not None and avg_w_prev is not None
        else None
    )

    maintenance = None
    if avg_k_7 is not None and weight_diff is not None:
        if abs(weight_diff) < 0.15:
            maintenance = avg_k_7
        else:
            maintenance = avg_k_7 - (7700 * weight_diff / 7)

    return {
        "avg_weight_7":      safe_round(avg_w_7, 1),
        "avg_weight_prev7":  safe_round(avg_w_prev, 1),
        "weight_diff":       safe_round(weight_diff, 1),
        "avg_kcal_7":        safe_round(avg_k_7, 0),
        "maintenance":       safe_round(maintenance, 0),
    }


# ============================================================
#   MAINTENANCE VIA REGRESSION
# ============================================================

def get_regression_maintenance():
    conn = get_nutrition_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT weight_kg, kcal
        FROM weight_logs
        WHERE weight_kg IS NOT NULL AND kcal IS NOT NULL
        ORDER BY date_iso ASC
    """)
    rows = cur.fetchall()
    conn.close()

    if len(rows) < 3:
        return None

    weights = [safe_num(r[0]) for r in rows]
    kcals   = [safe_num(r[1]) for r in rows]

    deltas = [weights[i] - weights[i-1] for i in range(1, len(weights))]
    kcal_m = [(kcals[i] + kcals[i-1]) / 2 for i in range(1, len(kcals))]

    best_M   = None
    best_err = float("inf")

    for M in range(2500, 4500):
        err = sum(
            abs((kcal_m[i] - M) / 7700 - deltas[i])
            for i in range(len(deltas))
        )
        if err < best_err:
            best_err = err
            best_M = M

    return best_M


# ============================================================
#   GEWICHTSPROGNOSE
# ============================================================

def predict_weight_30_days(current_weight, daily_kcal, maintenance):
    if current_weight is None or daily_kcal is None or maintenance is None:
        return None
    delta = (daily_kcal - maintenance) * 30 / 7700
    return round(current_weight + delta, 2)


# ============================================================
#   DASHBOARD SUMMARY
# ============================================================

def get_dashboard_summary():
    today = date.today()
    y = today - timedelta(days=1)
    w = today - timedelta(days=6)
    m = today - timedelta(days=29)

    y_iso = y.isoformat()
    w_iso = w.isoformat()
    m_iso = m.isoformat()
    t_iso = today.isoformat()

    # ============================================================
    # GESTERN
    # ============================================================
    conn = get_nutrition_db()
    cur = conn.cursor()

    cur.execute("SELECT kcal, protein FROM weight_logs WHERE date_iso=?", (y_iso,))
    row = cur.fetchone()

    gestern = {
        "kcal": int(row[0]) if row and row[0] else None,
        "protein": int(row[1]) if row and row[1] else None,
    }

    trends = get_last_7_and_prev_7()

    # ============================================================
    # WOCHE – RUN KM (eigene Verbindung!)
    # ============================================================
    conn_r = get_runs_db()
    cur_r = conn_r.cursor()

    cur_r.execute(
        """
        SELECT COALESCE(SUM(distance), 0)
        FROM runs
        WHERE substr(date, 1, 10) BETWEEN ? AND ?
        """,
        (w_iso, t_iso)
    )

    km_week = round((cur_r.fetchone()[0] or 0) / 1000, 1)
    conn_r.close()

    woche = {
        "weight_diff": trends.get("weight_diff"),
        "km": km_week,
    }

    # ============================================================
    # MONAT – KCAL
    # ============================================================
    cur.execute(
        "SELECT kcal FROM weight_logs WHERE date_iso BETWEEN ? AND ?",
        (m_iso, t_iso)
    )
    rows = cur.fetchall()

    kcal_vals = [safe_num(r[0]) for r in rows if r[0] is not None]

    conn.close()

    # ============================================================
    # MONAT – SESSIONS
    # ============================================================
    conn_t = get_training_db()
    cur_t = conn_t.cursor()

    cur_t.execute(
        "SELECT COUNT(*) FROM workouts WHERE date_iso BETWEEN ? AND ?",
        (m_iso, t_iso)
    )
    sessions_month = int(cur_t.fetchone()[0] or 0)

    conn_t.close()

    monat = {
        "avg_kcal": int(mean(kcal_vals)) if kcal_vals else None,
        "sessions": sessions_month,
    }

    return {
        "gestern": gestern,
        "woche": woche,
        "monat": monat,
        "trends": trends,
    }




__all__ = [
    "get_last_7_and_prev_7",
    "get_regression_maintenance",
    "predict_weight_30_days",
    "get_dashboard_summary",
]
