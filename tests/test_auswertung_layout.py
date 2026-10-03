from pathlib import Path


def test_auswertung_has_a_combined_analysis_workbench_alongside_specialist_views():
    root = Path(__file__).parents[1]
    auswertung = (root / "templates" / "auswertung.html").read_text(encoding="utf-8")
    training = (root / "templates" / "partials" / "_training_analysis_content.html").read_text(encoding="utf-8")
    nutrition = (root / "templates" / "partials" / "_nutrition_evaluation_content.html").read_text(encoding="utf-8")
    sidebar = (root / "static" / "js" / "sidebar.js").read_text(encoding="utf-8")
    controls = (root / "static" / "js" / "auswertung.js").read_text(encoding="utf-8")
    nutrition_controls = (root / "static" / "js" / "makros.js").read_text(encoding="utf-8")
    styles = (root / "static" / "css" / "auswertung.css").read_text(encoding="utf-8")
    overview = (root / "static" / "js" / "overall_analysis.js").read_text(encoding="utf-8")
    overall = (root / "templates" / "partials" / "_overall_analysis_content.html").read_text(encoding="utf-8")

    assert 'data-area="training"' in auswertung
    assert 'data-area="ernaehrung"' in auswertung
    assert 'data-area="gesamt"' in auswertung
    assert 'id="evaluation-panel-gesamt"' in auswertung
    assert '_overall_analysis_content.html' in auswertung
    assert '_training_analysis_content.html' in auswertung
    assert '_nutrition_evaluation_content.html' in auswertung
    assert 'Belastung &amp; Leistungsreaktion' in training
    assert '<h2>Fortschritt</h2>' in training
    assert 'id="makros-app"' in nutrition
    assert 'id="makros-range"' not in nutrition
    assert 'class="evaluation-area-switch"' in auswertung
    assert 'class="evaluation-timeline"' in auswertung
    assert 'id="evaluation-range-start"' in auswertung
    assert 'id="evaluation-range-end"' in auswertung
    assert 'class="evaluation-control-rail"' not in auswertung
    assert 'data-days=' not in auswertung
    assert 'liva:analysis-range-change' in controls
    assert 'liva:analysis-area-change' in controls
    assert 'event?.detail?.area === "ernaehrung"' in nutrition_controls
    assert 'event?.detail?.area === "training"' in training
    assert 'oneCalendarYearBefore' in controls
    assert 'timelineBoundsFor' in controls
    assert 'Math.max(90, Math.round(selectedDays * 1.8))' in controls
    assert 'new Date(maxDate.getTime() - 89 * MS_PER_DAY)' in controls
    assert 'data-evaluation-range="90" class="is-active"' in auswertung
    assert 'data-evaluation-range="28" class="is-active"' not in auswertung
    assert '.evaluation-range-presets {\n  display: none;' in styles
    assert '@media (max-width: 720px)' in styles
    assert '.evaluation-range-presets { display: flex;' in styles
    assert 'localStorage' not in controls
    assert 'getInterval' in controls
    assert 'detail: { area, ...interval, days }' in controls
    assert 'data-evaluation-range' in auswertung
    assert 'liva:analysis-range-change' in overview
    assert '/api/makros/series' in overview
    assert '/api/analyse/default_progress?${params}&trend_window_sessions=6' in overview
    assert '/api/hrv/series?${params}' not in overview
    assert '/api/hrv/recovery?${params}' in overview
    assert 'filter((row) => row.source === "polar")' in overview
    assert 'mapped(polarRecoveryRows, "date", "rmssd")' in overview
    assert '/api/analysis/calendar_load?${params}' in overview
    assert '/api/analysis/phases?${params}' in overview
    assert 'bodyweight_kg' in overview
    assert 'performance_trend' not in overview
    assert 'progressionPoints' in overview
    assert 'Progressionsquote' in overview
    assert 'fixedRange: [0, 100]' in overview
    assert 'data-series="performance"' not in overall
    assert 'data-series="flags"' in overall and 'Ereignisse' in overall
    assert 'flagDatasets' in overview
    assert 'label: "Krankheit"' in overview
    assert 'label: "Alkohol"' in overview
    assert 'color: "--event-sick"' in overview
    assert 'color: "--event-alcohol"' in overview
    assert 'y: 94' not in overview
    assert 'protein: { label: "Protein", unit: "g", color: "--series-protein"' in overview
    assert 'carbs: { label: "Carbs", unit: "g", color: "--series-carbs"' in overview
    assert 'fat: { label: "Fett", unit: "g", color: "--series-fat"' in overview
    assert 'training: { label: "Trainingslast", unit: "Index", color: "--series-training"' in overview
    assert 'nightPulse: { label: "Nachtpuls", unit: "bpm", color: "--series-night-pulse"' in overview
    assert 'sleep: { label: "Schlaf", unit: "h", color: "--series-sleep"' in overview
    assert 'data-series="protein"' in styles
    assert 'data-series="nightPulse"' in styles
    assert 'repeating-linear-gradient' not in styles
    assert 'dash:' not in overview
    assert 'borderDash: []' in overview
    assert 'function axisColor()' in overview
    for series in ("protein", "carbs", "fat", "progression", "nightPulse", "sleep", "calendar", "flags"):
        assert f'data-series="{series}"' in overall
    assert 'id="overall-analysis-phase"' in overall
    assert 'id="overall-analysis-scale"' in overall
    assert 'overall-analysis__reading' not in overall
    assert 'Dein Verlauf, übereinander' not in overall
    assert 'Verläufe gemeinsam lesen' not in auswertung
    assert 'const active = new Set(["weight"])' in overview
    for token in ("--series-weight", "--series-kcal", "--series-protein", "--series-carbs", "--series-fat", "--series-training", "--series-progression", "--series-hrv", "--series-night-pulse", "--series-sleep", "--series-calendar"):
        assert token in overview and token in styles
    assert 'Vergleich · je eigene Skala' in overview
    assert 'display: true' in overview
    assert 'AbortController' in overview
    assert 'async function renderNutritionFirst' in overview
    assert 'const nutritionResult = await nutritionRequest' in overview
    assert overview.index('const nutritionResult = await nutritionRequest') < overview.index('const requests = [nutritionRequest')
    assert 'canvas.addEventListener("wheel"' in overview
    assert 'canvas.addEventListener("pointerdown"' in overview
    assert 'path: "/auswertung"' in sidebar
    assert 'path: "/analyse"' not in sidebar
    assert 'path: "/makros"' not in sidebar

