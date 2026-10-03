from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_dashboard_keeps_one_stable_dom_for_every_card():
    template = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "dashboard_vnext.js").read_text(encoding="utf-8")

    for card_id in (
        "dashboard-today-card",
        "dashboard-signals-card",
        "dashboard-mealplan-card",
        "dashboard-endurance-card",
        "today-agenda-card",
        "plan-check-card",
    ):
        assert template.count(f'id="{card_id}"') == 1

    assert "dashboard-mobile-stack" not in template
    assert "applyMobileLayout" not in script
    assert "applyDesktopLayout" not in script
    assert "layoutState" not in script
    assert "Card ownership never changes with viewport size" in script
    assert "function getDashboardCards()" in script
    assert "function markDashboardCardReady(target)" in script
    assert "function primeDashboardCardAnimation()" in script


def test_dashboard_professional_layer_owns_surfaces_and_responsive_contract():
    template = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "css" / "dashboard_professional.css").read_text(
        encoding="utf-8"
    )

    assert "dashboard_professional.css" in template
    assert "--dash-pad: 1rem;" in stylesheet
    assert "--dash-radius: 1.375rem;" in stylesheet
    assert "--dash-card: #151a21;" in stylesheet
    assert "--dash-inner: rgba(148, 163, 184, 0.045);" in stylesheet
    assert "#dashboard-app .dashboard-card," in stylesheet
    assert "#dashboard-app .chip-card" in stylesheet
    assert "#dashboard-app #dashboard-context-row" in stylesheet
    assert "grid-area: auto !important;" in stylesheet
    assert "@media (min-width: 42rem)" in stylesheet
    assert "@media (min-width: 64rem)" in stylesheet
    assert "@media (max-width: 41.999rem)" in stylesheet
    assert "@media (min-width: 768px)" not in stylesheet
    assert "@media (max-width: 767px)" not in stylesheet
    assert "#dashboard-app .dashboard-card::after" in stylesheet
    assert "content: none !important;" in stylesheet
    assert ".mealplan-hold-ring {" in stylesheet
    assert "position: absolute;" in stylesheet
    assert ".mealplan-hold-ring-rect {" in stylesheet
    assert "fill: none;" in stylesheet


def test_shared_foundation_is_loaded_after_legacy_base_styles():
    template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    style_position = template.index("css/style.css")
    foundation_position = template.index("css/foundation.css")
    assert style_position < foundation_position


def test_signals_heatmap_uses_distributed_square_cells_and_date_grounded_weekdays():
    stylesheet = (ROOT / "static" / "css" / "dashboard_professional.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "static" / "js" / "dashboard_vnext.js").read_text(encoding="utf-8")

    assert "repeat(var(--signal-columns, 14), minmax(0, 1fr))" in stylesheet
    assert "width: 0.6875rem !important;" in stylesheet
    assert "height: 0.6875rem !important;" in stylesheet
    assert 'class="signals-days" id="signal-day-labels"' in (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'dayGrid.className = "signal-day-grid"' in script
    assert "weekdayShortLabel(days[i])" in script
    assert "weekdayLongLabel(days[i])" in script
    assert "signals?.weekdays" not in script
    assert "ResizeObserver(() => updateSignalHeatmapLayout())" not in script
