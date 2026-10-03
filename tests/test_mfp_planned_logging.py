import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mfp_planned_log_buttons_use_planned_time_payload():
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")

    assert "async function logPlanned(slotId, timeMode = 'planned')" in source
    assert "payload.custom_time_text = plannedTime;" in source
    assert "payload.logged_at = isoAtDate(plannedTime);" in source
    assert "logPlanned(slotId, 'now')" not in source
    assert "logPlanned(Number(next.slot_id), 'now')" not in source


def test_planned_logging_api_defaults_to_planned_time():
    source = (ROOT / "nutrition/nutrition_planning_api.py").read_text(encoding="utf-8")

    assert 'time_mode=(payload.get("time_mode") or "planned")' in source


def test_open_planned_meal_items_include_current_food_macros():
    from nutrition import nutrition_planning_db as nutrition_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE nutrition_foods (
            id INTEGER PRIMARY KEY, name TEXT, unit_default TEXT,
            common_portion_size REAL, portion_g REAL, kcal_per_100 REAL,
            p_per_100 REAL, c_per_100 REAL, f_per_100 REAL, sugar_per_100 REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO nutrition_foods VALUES (7, 'Whey', 'g', 30, NULL, 380, 78, 8, 6, 5)"
    )

    items = nutrition_db._hydrate_logging_food_items(
        conn.cursor(),
        [{"item_type": "food", "food_id": 7, "food_name": "Whey", "amount": 20, "unit": "g"}],
    )

    assert items[0]["kcal_per_100"] == 380
    assert items[0]["p_per_100"] == 78
    assert items[0]["c_per_100"] == 8
    assert items[0]["f_per_100"] == 6
    assert items[0]["unit_default"] == "g"


def test_mfp_day_feed_separates_open_and_logged_meals():
    template = (ROOT / "templates/meals.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")

    assert 'id="day-feed-list"' in template
    assert "planned-done-list" not in template
    assert "planned-missing-list" not in template
    assert "plannedDoneList" not in source
    assert "plannedMissingList" not in source
    assert ".filter(isPlannedMealOpen)" in source
    assert "if (meal.logged_meal || meal.logged_meal_id) return true;" in source
    assert "state.day?.logged_meals" in source
    assert "logged?.slot_id || logged?.created_from_planned_slot_id" in source
    assert "return ['logged', 'changed', 'telegram_confirmed', 'manual_override'].includes(status);" in source
    assert 'class="day-group is-open"' in source
    assert 'class="day-group is-eaten"' in source
    assert "const nextPlanned = isSelectedDayToday() ? planned[0] : null;" in source
    assert "function renderDayFeed()" in source


def test_mfp_mobile_is_one_continuous_day_flow_without_training_style_tabs():
    template = (ROOT / "templates/meals.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static/css/meals.css").read_text(encoding="utf-8")

    assert 'data-meals-mobile-mode' not in template
    assert 'class="mobile-quickbar"' in template
    assert template.index('class="mobile-quickbar"') < template.index('class="day-feed"')
    assert 'id="meals-date-display"' in template
    assert "function formatGermanDate(isoDate)" in source
    assert "syncDateDisplay();" in source
    assert ".day-feed{width:100%}" in stylesheet
    assert 'id="mobile-log-next-btn"' not in template
    assert "20260917_mfp_units_v1" in template
    assert 'class="quick-dialog-body"' in template
    assert 'aria-keyshortcuts="Control+Enter Meta+Enter"' in template
    assert 'class="editor-dialog-body"' in template
    assert 'class="editor-search-shell"' in template
    assert 'data-editor-action="minus"' in source
    assert 'data-editor-action="plus"' in source
    assert 'data-editor-macros=' in source
    assert 'id="editor-meal-title" type="text"' in template
    assert 'class="meal-editor-heading"' in template
    assert "title: String(els.editorMealTitle.value || '').trim() || 'Meal'" in source
    assert 'id="food-editor-modal"' in template
    assert 'data-editor-action="edit-food"' in source
    assert "method: 'PUT'" in source
    assert '`/api/nutrition/foods/${state.foodEditor.foodId}`' in source
    assert '<section class="day-group is-eaten">' in source
    assert '<details class="day-group is-eaten"' not in source


def test_mfp_touch_inputs_keep_ios_safari_at_16px_or_larger():
    stylesheet = (ROOT / "static/css/meals.css").read_text(encoding="utf-8")

    assert "@media (hover: none) and (pointer: coarse)" in stylesheet
    assert "font-size: max(1rem, 16px) !important;" in stylesheet


def test_mfp_removes_the_low_value_shift_flow():
    template = (ROOT / "templates/meals.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")

    assert 'id="planned-shift-modal"' not in template
    assert "planned-shift" not in source
    assert "Später" not in template
    assert "window.prompt" not in source


def test_mfp_keeps_compact_mobile_daily_actions_and_logging_hooks():
    template = (ROOT / "templates/meals.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")

    assert 'id="mobile-quickadd-btn"' in template
    assert 'id="mobile-log-next-btn"' not in template
    assert 'id="mobile-refresh-btn"' not in template
    assert 'data-action="planned-log"' in source
    assert 'data-action="logged-duplicate"' in source
    assert 'data-action="logged-delete"' in source


def test_mfp_supports_copying_yesterday_and_single_historical_meals():
    template = (ROOT / "templates/meals.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")
    api_source = (ROOT / "nutrition/nutrition_planning_api.py").read_text(encoding="utf-8")

    assert 'id="copy-yesterday-btn"' in template
    assert 'id="copy-day-modal"' in template
    assert "data-action=\"logged-copy-today\"" in source
    assert "copyMealsToDate(ids, state.date)" in source
    assert '@nutrition_planning_api.post("/api/nutrition/logging/meals/copy")' in api_source


def test_copying_meals_links_matching_target_plan_slots():
    db_source = (ROOT / "nutrition/nutrition_planning_db.py").read_text(encoding="utf-8")

    assert "target_day = get_logging_day_payload(normalized_target)" in db_source
    assert "target = matching_target(meal)" in db_source
    assert 'reason_code="COPIED_FROM_DAY"' in db_source
    assert "claimed_slot_ids.add(slot_id)" in db_source
    assert '"source IN (\'planned\', \'day_copy\')"' in db_source


def test_copy_matching_meal_closes_the_target_plan_slot(monkeypatch):
    from nutrition import nutrition_planning_db as nutrition_db

    class Cursor:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return [{"id": 41, "log_date": "2026-09-08"}]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    source_meal = {
        "id": 41,
        "title": "Frühstück",
        "meal_slot": "Meal 1",
        "time_text": "06:45",
        "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
    }
    target_day = {
        "planned_meals": [{
            "slot_id": 91,
            "status": "open",
            "title": "Frühstück · Brot",
            "meal_slot": "Meal 1",
            "time_text": "06:40",
            "items": [{"food_id": 27, "food_name": "Brot", "amount": 1, "unit": "pcs"}],
        }]
    }
    calls = []
    monkeypatch.setattr(nutrition_db, "ensure_nutrition_planning_schema", lambda: None)
    monkeypatch.setattr(nutrition_db, "get_nutrition_db", lambda: Connection())
    monkeypatch.setattr(nutrition_db, "_load_logged_meals_for_date", lambda _cur, _date: [source_meal])
    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda _date: target_day)
    monkeypatch.setattr(nutrition_db, "log_planned_meal", lambda slot_id, **kwargs: (calls.append((slot_id, kwargs)) or target_day, None))
    monkeypatch.setattr(nutrition_db, "create_free_logged_meal", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must link to planned slot")))

    payload, error = nutrition_db.copy_logged_meals_to_date([41], target_date="2026-09-09")

    assert error is None
    assert payload is target_day
    assert calls[0][0] == 91
    assert calls[0][1]["reason_code"] == "COPIED_FROM_DAY"
    assert calls[0][1]["custom_time_text"] == "06:45"


def test_mfp_ctrl_z_has_undo_stack_and_keeps_text_input_undo():
    source = (ROOT / "static/js/meals.js").read_text(encoding="utf-8")

    assert "undoStack: []" in source
    assert "function pushUndo(label, run)" in source
    assert "function clearUndoStack()" in source
    assert "async function runUndo()" in source
    assert "(event.ctrlKey || event.metaKey) && !event.shiftKey && key === 'z' && !inTyping" in source
    assert "runUndo().catch((err) => console.error(err));" in source
    assert "pushUndo('Geplantes Meal loggen'" in source
    assert "pushUndo('Meal löschen'" in source
    assert "pushUndo('Meal bearbeiten'" in source


def test_dashboard_mealplan_releases_hold_before_rerender():
    source = (ROOT / "static/js/dashboard_vnext.js").read_text(encoding="utf-8")

    assert 'let holdReleasedBeforeRender = false;' in source
    assert 'cancelMealplanHold(rowEl, { preserveComplete: true, flushPending: false });' in source
    assert 'holdReleasedBeforeRender = true;' in source
    assert source.index('cancelMealplanHold(rowEl, { preserveComplete: true, flushPending: false });') < source.index('renderMealplanData(dayPayload);')
