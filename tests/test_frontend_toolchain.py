from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_tailwind_shadcn_toolchain_is_reproducible():
    package = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    components = (ROOT / "frontend" / "components.json").read_text(encoding="utf-8")
    vite = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert '"tailwindcss": "4.' in package
    assert '"shadcn"' in package
    assert '"ui:add": "shadcn add"' in package
    assert '"node": "24.18.0"' in package
    assert '"style": "radix-nova"' in components
    assert '"cssVariables": true' in components
    assert "../static/dist/liva-ui" in vite
    assert "entryFileNames: 'liva-ui.js'" in vite


def test_ui_lab_mounts_the_built_react_entrypoint():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "ui_lab.html").read_text(encoding="utf-8")
    main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")
    ui_app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert '@app.get("/ui-lab")' in app_source
    assert "@require_admin" in app_source
    assert 'id="liva-ui-root"' in template
    assert "dist/liva-ui/liva-ui.css" in template
    assert "dist/liva-ui/liva-ui.js" in template
    assert 'document.getElementById("liva-dashboard-root")' in main
    assert 'document.getElementById("liva-ui-root")' in main
    assert "function DashboardPalettePanel" in ui_app
    assert 'Tabs defaultValue="palette"' in ui_app
    assert '"--background", "#0e1013", "#e7ecf2"' in ui_app
    assert 'Dashboard-Palette' in ui_app


def test_dashboard_uses_modern_entrypoint_with_legacy_fallback():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "dashboard_modern.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "frontend" / "src" / "DashboardApp.tsx").read_text(encoding="utf-8")

    assert 'request.args.get("legacy") != "1"' in app_source
    assert 'render_template("dashboard_modern.html"' in app_source
    assert 'id="liva-dashboard-root"' in template
    assert 'data-liva-surface="dashboard"' in template
    assert "dashboard-20260911-ipad-pairs-v3" in template
    assert 'eyebrow="Cardio"' in dashboard
    assert 'title="Nächster Lauf"' in dashboard
    assert "Athletica und Intervals erreichbar" not in dashboard
    assert "toggleTheme" not in dashboard
    assert "Farbschema wechseln" not in dashboard
    for endpoint in (
        "/api/dashboard/today",
        "/api/dashboard/signals",
        "/api/dashboard/today_agenda",
        "/api/dashboard/training_today_card",
        "/api/nutrition/logging/day",
        "/api/dashboard/endurance-approval",
        "/api/dashboard/block_status",
    ):
        assert endpoint in dashboard
    assert 'const coaching = training?.items || []' in dashboard
    assert 'AbortSignal.timeout(timeoutMs)' in dashboard
    assert 'Object.entries(requests).forEach' in dashboard
    assert 'pollPendingTrainingCard(url, controller.signal)' in dashboard
    assert 'loading.training && !data.training' in dashboard
    assert 'training?.status === "not_applicable"' in dashboard
    assert 'status: "failed"' in dashboard
    assert 'data.training?.status === "stale"' not in dashboard
    assert "function Sparkline" in dashboard
    assert 'title="Aktivitätsbilanz"' in dashboard
    assert "Trainingsansicht" in dashboard
    assert 'href="/training">Einheit öffnen' not in dashboard
    assert 'href="/planung">Tagesplanung' not in dashboard
    assert 'href="/auswertung?bereich=ernaehrung">Ernährung auswerten' not in dashboard
    assert 'href="/mfp">Ernährung loggen' in dashboard
    assert "/api/nutrition/logging/planned/${slotId}/log" in dashboard
    assert 'time_mode: "planned"' in dashboard
    assert "Meal jetzt loggen" in dashboard


def test_dashboard_and_subpages_share_sidebar_icons_and_behavior():
    dashboard = (ROOT / "frontend" / "src" / "DashboardApp.tsx").read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "dashboard_modern.html").read_text(encoding="utf-8")
    sidebar = (ROOT / "static" / "js" / "sidebar.js").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    for storage_key in (
        "liva.sidebarCollapsed",
        "liva.sidebarCollapsed.tablet",
        "liva.sidebarCollapsed.desktop",
    ):
        assert storage_key in sidebar

    assert 'document.addEventListener("click", (event)' in sidebar
    assert 'closest("a, button")' in sidebar
    assert 'label: "Heute"' in sidebar
    assert 'icon: "home"' in sidebar
    assert 'class="mobile-bottom-nav"' in sidebar
    assert 'class="mobile-dock-sheet"' in sidebar
    assert 'data-testid="mobile-bottom-navigation"' in sidebar
    assert "function MobileNav" not in dashboard
    assert "function DesktopNav" not in dashboard
    assert "sidebarCollapsed" not in dashboard
    assert "applySidebarCollapsed" not in app_js

    lucide_paths = (
        'd="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"',
        'd="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"',
        'd="M3 3v16a2 2 0 0 0 2 2h16"',
    )
    for path in lucide_paths:
        assert path in sidebar

    for page in (base, template):
        assert page.count("data-liva-sidebar-host") == 1
        assert "css/sidebar.css" in page
        assert "js/sidebar.js" in page

    assert "liva-peak-sidebar.svg" in sidebar
    assert "liva-peak.svg" in template
    assert 'className="shared-sidebar-main pb-24 sm:pb-0"' in dashboard


def test_mobile_content_keeps_native_viewport_scale_and_uses_one_global_shell():
    stylesheet = (ROOT / "static" / "css" / "sidebar.css").read_text(encoding="utf-8")
    sidebar = (ROOT / "static" / "js" / "sidebar.js").read_text(encoding="utf-8")

    assert "--mobile-content-scale: 1;" in stylesheet
    assert "--mobile-content-gutter: 26px;" in stylesheet
    assert "zoom: var(--mobile-content-scale);" not in stylesheet
    assert "body.has-mobile-dock .app-page-shell" in stylesheet
    assert "body.has-mobile-dock #liva-dashboard-root .shared-sidebar-main" in stylesheet
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in stylesheet
    mobile_primary = sidebar.split("const mobilePrimary", 1)[1].split("const mobileOverflow", 1)[0]
    assert mobile_primary.index('label: "Training"') < mobile_primary.index('label: "Ernährung"') < mobile_primary.index('label: "Heute"') < mobile_primary.index('label: "Auswertung"')
    assert "mobile-bottom-home" in sidebar
    assert "liva-peak-sidebar.svg" in sidebar


def test_mobile_interaction_rules_preserve_editable_inputs_and_mfp_uses_native_time():
    foundation = (ROOT / "static" / "css" / "foundation.css").read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    meals_template = (ROOT / "templates" / "meals.html").read_text(encoding="utf-8")
    meals_css = (ROOT / "static" / "css" / "meals.css").read_text(encoding="utf-8")
    meals_js = (ROOT / "static" / "js" / "meals.js").read_text(encoding="utf-8")

    assert "touch-action: manipulation;" in foundation
    assert '<nav id="mobile-nav"' not in base
    assert 'class="mobile-nav"' not in base
    assert "body.has-mobile-dock :is(.app-page-shell, dialog)" in foundation
    assert "[contenteditable=\"true\"]" in foundation
    assert "font-size: max(1rem, 16px);" in foundation
    assert meals_template.count('type="time"') == 2
    assert "dialog-time-control" not in meals_template
    assert "zoom:1.333333" not in meals_css
    assert "function moveCaretToEnd" in meals_js
    assert "input.setSelectionRange(end, end);" in meals_js


def test_core_shadcn_primitives_are_checked_in():
    component_dir = ROOT / "frontend" / "src" / "components" / "ui"
    for name in (
        "badge",
        "button",
        "card",
        "dialog",
        "dropdown-menu",
        "input",
        "select",
        "skeleton",
        "tabs",
        "tooltip",
    ):
        assert (component_dir / f"{name}.tsx").is_file()
