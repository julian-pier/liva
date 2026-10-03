# analysis/runs_analysis.py

"""
Runs / Cardio Analyse

Zuständigkeiten:
- Klassifikation von Läufen (Easy, Steady, Tempo, Threshold, Interval, Long)
- KEINE DB-Verbindungen
- KEINE Flask-Logik
- Reine Analyse / Business-Logik
"""


# ============================================================
# RUN TYPE CLASSIFICATION
# ============================================================

def classify_run(
    km,
    pace_min_km,
    avg_hr=None,
    threshold_pace=4.50,
    long_run_km=12
):
    """
    Klassifiziert einen Lauf anhand Distanz & Pace.
    Rückgabe:
    Easy | Steady | Tempo | Threshold | Interval | Long
    """

    if km is None or pace_min_km is None:
        return "Unknown"

    # Interval
    if pace_min_km < threshold_pace - 0.35:
        return "Interval"

    # Long Run
    if km >= long_run_km:
        return "Long"

    # Threshold
    if pace_min_km <= threshold_pace + 0.05:
        return "Threshold"

    # Tempo
    if pace_min_km <= threshold_pace + 0.20:
        return "Tempo"

    # Moderate / Steady
    if pace_min_km <= threshold_pace + 0.45:
        return "Steady"

    return "Easy"


# ============================================================
# OPTIONAL: FUTURE EXTENSIONS
# ============================================================

def estimate_run_load(km: float, pace_min_km: float) -> float:
    """
    Grobe Trainingslast-Schätzung für Läufe.
    (Noch nicht aktiv – vorbereitet für später.)
    """

    if km is None or pace_min_km is None:
        return 0.0

    intensity_factor = max(0.5, min(1.5, 5.0 / pace_min_km))
    return round(km * intensity_factor, 2)


__all__ = [
    "classify_run",
    "estimate_run_load",
]
