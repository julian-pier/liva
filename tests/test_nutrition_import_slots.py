import importlib
import json
from datetime import date

import database.connections as connections

from nutrition.planning_mealplan_import import parse_mealplan_text


def test_import_extends_slots_for_large_meal_counts(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Import Test", set_active=True)
    assert err is None
    assert template

    lines = []
    for day in ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]:
        lines.append(f"## {day}")
        for i in range(1, 8):
            lines.append(f"- Meal {i}: Test Meal {i} (1x)")
            lines.append("  - 100 g Reis")
        lines.append("")
    text = "\n".join(lines).strip()

    parsed = parse_mealplan_text(text)
    assert parsed.get("max_slot_overall") == 7
    assert not any(issue.get("severity") == "error" for issue in parsed.get("issues", []))

    result = npdb.import_mealplan_text(text, template["id"], strict=False, apply=True)
    assert result["ok"] is True
    assert not result.get("write_errors")

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    day_rows = cur.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=?",
        (template["id"],),
    ).fetchall()
    for day in day_rows:
        row = cur.execute(
            "SELECT MAX(slot_index) AS max_idx FROM nutrition_week_day_slots WHERE week_template_day_id=?",
            (day["id"],),
        ).fetchone()
        assert row["max_idx"] == 6
    conn.close()


def test_import_applies_same_meals_to_all_days_from_multi_day_header(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Grouped Days Import", set_active=True)
    assert err is None
    assert template

    text = """## Mo, Do
- [06:40] Meal 1: Frühstück zuhause (1x)
  - 1 pcs Brot
  - 15 g Erdnussbutter

## Sa, So
- [11:00] Meal 1: Frühstück zuhause (1x)
  - 70 g Haferflocken (zart)
"""

    result = npdb.import_mealplan_text(text, template["id"], strict=False, apply=True)
    assert result["ok"] is True
    assert not result.get("write_errors")

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT d.weekday, s.slot_index, s.time_text, sm.meal_title
        FROM nutrition_week_template_days d
        JOIN nutrition_week_day_slots s ON s.week_template_day_id=d.id
        LEFT JOIN nutrition_slot_meals sm
          ON sm.week_template_day_id=d.id
         AND sm.slot_index=s.slot_index
        WHERE d.week_template_id=? AND sm.id IS NOT NULL
        ORDER BY d.weekday, s.slot_index
        """,
        (template["id"],),
    ).fetchall()
    conn.close()

    assert [(row["weekday"], row["slot_index"], row["time_text"], row["meal_title"]) for row in rows] == [
        (0, 0, "06:40", "Frühstück zuhause"),
        (3, 0, "06:40", "Frühstück zuhause"),
        (5, 0, "11:00", "Frühstück zuhause"),
        (6, 0, "11:00", "Frühstück zuhause"),
    ]


def test_new_templates_start_with_four_default_slots_per_day(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Four Slots", set_active=True)
    assert err is None
    assert template

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT d.weekday, COUNT(*) AS slot_count, MAX(s.slot_index) AS max_idx
        FROM nutrition_week_template_days d
        JOIN nutrition_week_day_slots s ON s.week_template_day_id=d.id
        WHERE d.week_template_id=?
        GROUP BY d.weekday
        ORDER BY d.weekday
        """,
        (template["id"],),
    ).fetchall()
    conn.close()

    assert len(rows) == 7
    assert all(row["slot_count"] == 4 for row in rows)
    assert all(row["max_idx"] == 3 for row in rows)


def test_create_week_day_slot_reuses_first_free_gap(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Gap Reuse", set_active=True)
    assert err is None
    assert template

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    day = cur.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0",
        (template["id"],),
    ).fetchone()
    slot_to_delete = cur.execute(
        "SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=1",
        (day["id"],),
    ).fetchone()
    meal2_template = cur.execute(
        "SELECT id FROM nutrition_slot_templates WHERE title='Meal 2'",
    ).fetchone()
    conn.close()

    ok, err = npdb.delete_week_day_slot(slot_to_delete["id"])
    assert ok is True
    assert err is None

    created, err = npdb.create_week_day_slot(day["id"], meal2_template["id"])
    assert err is None
    assert created["slot_index"] == 1


def test_get_slot_ingredients_rematches_missing_food_ids_by_name(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Rematch Missing Food IDs", set_active=True)
    assert err is None
    assert template

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    now = "2026-04-05T12:00:00"
    cur.execute(
        """
        INSERT INTO nutrition_foods
            (name, brand, unit_default, portion_g, common_portion_size, mfp_search_hint, category,
             kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, salt_per_100,
             tags, is_favorite, is_active, name_normalized, created_at, updated_at)
        VALUES
            ('Whey', NULL, 'g', NULL, NULL, NULL, NULL, 400, 80, 8, 6, NULL, NULL, NULL, 0, 1, 'whey', ?, ?),
            ('Apfel', NULL, 'g', NULL, 180, NULL, NULL, 52, 0.3, 14, 0.2, NULL, NULL, NULL, 0, 1, 'apfel', ?, ?)
        """,
        (now, now, now, now),
    )
    slot = cur.execute(
        """
        SELECT s.id
        FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        WHERE d.week_template_id=? AND d.weekday=0 AND s.slot_index=0
        """,
        (template["id"],),
    ).fetchone()
    cur.execute(
        """
        INSERT INTO nutrition_slot_meals
            (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
        SELECT week_template_day_id, slot_index, 'Test', 1.0, NULL, ?, ?
        FROM nutrition_week_day_slots WHERE id=?
        """,
        (now, now, slot["id"]),
    )
    slot_meal_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO nutrition_slot_meal_items
            (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
        VALUES
            (?, 30, 'g', 'Whey', NULL, 'food', NULL, NULL, 'req-1', 'fp-1', ?, ?),
            (?, 1, 'pcs', 'Apfel', NULL, 'food', NULL, NULL, 'req-2', 'fp-2', ?, ?)
        """,
        (slot_meal_id, now, now, slot_meal_id, now, now),
    )
    conn.commit()
    conn.close()

    data, err = npdb.get_slot_ingredients(slot["id"])
    assert err is None
    assert data
    names = {item["food_name"]: item["food_id"] for item in data["ingredients"]}
    assert names["Whey"]
    assert names["Apfel"]


def test_new_template_inherits_global_macro_settings(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT NULL
        )
        """
    )
    cur.executemany(
        "INSERT OR REPLACE INTO nutrition_settings(key, value) VALUES (?, ?)",
        [
            ("active_mode", "lean_bulk"),
            ("mode_lean_bulk_target", "3800"),
            ("mode_lean_bulk_protein_target", "145"),
            ("mode_lean_bulk_carbs_target", "550"),
            ("mode_lean_bulk_fat_target", "100"),
            ("mode_lean_bulk_green_low", "3650"),
            ("mode_lean_bulk_green_high", "4450"),
            ("mode_lean_bulk_yellow_low", "3500"),
            ("mode_lean_bulk_yellow_high", "4800"),
        ],
    )
    conn.commit()
    conn.close()

    template, err = npdb.create_week_template("Macro Inherit", set_active=True)
    assert err is None
    assert template

    settings, err = npdb.get_week_template_macro_settings(template["id"])
    assert err is None
    assert settings["active_mode"] == "lean_bulk"
    assert settings["modes"]["lean_bulk"]["kcal_target"] == 3800
    assert settings["modes"]["lean_bulk"]["protein_target"] == 145
    assert settings["modes"]["lean_bulk"]["carbs_target"] == 550
    assert settings["modes"]["lean_bulk"]["fat_target"] == 100
    assert settings["modes"]["lean_bulk"]["green_low"] == 3650
    assert settings["modes"]["lean_bulk"]["green_high"] == 4450
    assert settings["modes"]["lean_bulk"]["yellow_low"] == 3500
    assert settings["modes"]["lean_bulk"]["yellow_high"] == 4800


def test_live_macro_targets_use_current_active_mealplan(tmp_path, monkeypatch):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT NULL
        )
        """
    )
    cur.executemany(
        "INSERT OR REPLACE INTO nutrition_settings(key, value) VALUES (?, ?)",
        [
            ("active_mode", "lean_bulk"),
            ("mode_lean_bulk_target", "3800"),
            ("mode_lean_bulk_protein_target", "145"),
            ("mode_lean_bulk_carbs_target", "550"),
            ("mode_lean_bulk_fat_target", "100"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(npdb, "_berlin_today", lambda: date(2026, 4, 16))

    template, err = npdb.create_week_template("Current Mealplan Targets", set_active=True)
    assert err is None
    assert template

    updated, err = npdb.update_week_template_macro_settings(
        template["id"],
        {
            "active_mode": "cut",
            "modes": {
                "cut": {
                    "kcal_target": 2600,
                    "protein_target": 180,
                    "carbs_target": 323,
                    "fat_target": 65,
                    "green_low": 2450,
                    "green_high": 2700,
                    "yellow_low": 2300,
                    "yellow_high": 2900,
                }
            },
        },
    )
    assert err is None
    assert updated["active_mode"] == "cut"

    live = npdb.get_live_macro_targets()

    assert live["kcal"] == 2600
    assert live["p"] == 180
    assert live["c"] == 323
    assert live["f"] == 65
    assert live["source"] == "active_week_template"
    assert live["mode"] == "cut"
    assert live["template_id"] == template["id"]
    assert live["weekday"] == 3
    assert live["kcal"] != 3800


def test_copied_template_keeps_own_macro_settings(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    source, err = npdb.create_week_template("Source Macro Template", set_active=True)
    assert err is None
    assert source

    updated, err = npdb.update_week_template_macro_settings(
        source["id"],
        {
            "active_mode": "custom",
            "modes": {
                "custom": {
                    "kcal_target": 4123,
                    "protein_target": 210,
                    "carbs_target": 390,
                    "fat_target": 115,
                    "green_low": 4000,
                    "green_high": 4250,
                    "yellow_low": 3900,
                    "yellow_high": 4400,
                }
            },
        },
    )
    assert err is None
    assert updated["active_mode"] == "custom"

    copied, err = npdb.create_week_template("Copied Macro Template", source_template_id=source["id"], set_active=True)
    assert err is None
    assert copied

    copied_settings, err = npdb.get_week_template_macro_settings(copied["id"])
    assert err is None
    assert copied_settings["active_mode"] == "custom"
    assert copied_settings["modes"]["custom"]["kcal_target"] == 4123
    assert copied_settings["modes"]["custom"]["protein_target"] == 210
    assert copied_settings["modes"]["custom"]["carbs_target"] == 390
    assert copied_settings["modes"]["custom"]["fat_target"] == 115
    assert copied_settings["modes"]["custom"]["green_low"] == 4000
    assert copied_settings["modes"]["custom"]["green_high"] == 4250
    assert copied_settings["modes"]["custom"]["yellow_low"] == 3900
    assert copied_settings["modes"]["custom"]["yellow_high"] == 4400


def test_week_plan_payload_rematches_slot_item_foods_without_food_id(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    template, err = npdb.create_week_template("Mini-Cut Regression", set_active=True)
    assert err is None
    assert template

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    now = "2026-04-05T12:00:00"
    cur.execute(
        """
        INSERT INTO nutrition_foods
            (name, brand, unit_default, portion_g, common_portion_size, mfp_search_hint, category,
             kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, salt_per_100,
             tags, is_favorite, is_active, name_normalized, created_at, updated_at)
        VALUES
            ('Whey', NULL, 'g', NULL, NULL, NULL, NULL, 385, 75, 8, 5, 5, NULL, NULL, 0, 1, 'whey', ?, ?)
        """,
        (now, now),
    )
    day = cur.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0",
        (template["id"],),
    ).fetchone()
    slot = cur.execute(
        "SELECT id, slot_index FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=0",
        (day["id"],),
    ).fetchone()
    cur.execute(
        """
        INSERT INTO nutrition_slot_meals
            (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
        VALUES (?, ?, 'Whey Test', 1.0, NULL, ?, ?)
        """,
        (day["id"], slot["slot_index"], now, now),
    )
    slot_meal_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO nutrition_slot_meal_items
            (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
        VALUES
            (?, 30, 'g', 'Whey', NULL, 'food', NULL, NULL, 'req-whey', 'fp-whey', ?, ?)
        """,
        (slot_meal_id, now, now),
    )
    conn.commit()
    conn.close()

    payload = npdb.get_active_week_plan_resolved()
    monday = next(day for day in payload["week_template"]["days"] if day["weekday"] == 0)
    first_slot = next(slot for slot in monday["slots"] if slot["slot_index"] == 0)

    assert first_slot["items"][0]["food_id"]
    assert first_slot["items"][0]["name"] == "Whey"
    assert round(monday["planned"]["kcal"]) == 116
    assert round(monday["planned"]["p"]) == 22
    assert round(monday["planned"]["c"]) == 2
    assert round(monday["planned"]["f"]) == 2


def test_week_template_timeline_tracks_active_plan_switches(tmp_path, monkeypatch):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    clock = {"value": "2026-03-01T08:00:00"}
    monkeypatch.setattr(npdb, "_utcnow_iso", lambda: clock["value"])

    bulk, err = npdb.create_week_template("Bulk", set_active=True)
    assert err is None
    assert bulk
    updated_bulk, err = npdb.update_week_template_macro_settings(
        bulk["id"],
        {
            "active_mode": "lean_bulk",
            "modes": {
                "lean_bulk": {
                    "kcal_target": 3800,
                    "protein_target": 145,
                    "carbs_target": 580,
                    "fat_target": 100,
                    "green_low": 3650,
                    "green_high": 4450,
                    "yellow_low": 3500,
                    "yellow_high": 4800,
                }
            },
        },
    )
    assert err is None
    assert updated_bulk["active_mode"] == "lean_bulk"

    clock["value"] = "2026-04-05T18:59:00"
    cut, err = npdb.create_week_template("Cut", set_active=True)
    assert err is None
    assert cut
    updated_cut, err = npdb.update_week_template_macro_settings(
        cut["id"],
        {
            "active_mode": "cut",
            "modes": {
                "cut": {
                    "kcal_target": 2650,
                    "protein_target": 180,
                    "carbs_target": 336,
                    "fat_target": 65,
                    "green_low": 2500,
                    "green_high": 2750,
                    "yellow_low": 2350,
                    "yellow_high": 2950,
                }
            },
        },
    )
    assert err is None
    assert updated_cut["active_mode"] == "cut"

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT week_template_id, start_date, end_date, snapshot_json
        FROM nutrition_week_template_timeline
        ORDER BY start_date ASC, id ASC
        """
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["week_template_id"] == bulk["id"]
    assert rows[0]["start_date"] == "2026-03-01"
    assert rows[0]["end_date"] == "2026-04-04"
    assert rows[1]["week_template_id"] == cut["id"]
    assert rows[1]["start_date"] == "2026-04-05"
    assert rows[1]["end_date"] is None

    cut_snapshot = json.loads(rows[1]["snapshot_json"])
    assert cut_snapshot["active_mode"] == "cut"
    assert cut_snapshot["active_mode_settings"]["kcal_target"] == 2650
    assert cut_snapshot["day_targets"]["0"]["kcal"] == 2650


def test_week_template_timeline_splits_when_active_targets_change_on_later_day(tmp_path, monkeypatch):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    clock = {"value": "2026-04-05T10:00:00"}
    monkeypatch.setattr(npdb, "_utcnow_iso", lambda: clock["value"])

    template, err = npdb.create_week_template("Mini-Cut", set_active=True)
    assert err is None
    assert template

    initial, err = npdb.update_week_template_macro_settings(
        template["id"],
        {
            "active_mode": "cut",
            "modes": {
                "cut": {
                    "kcal_target": 2650,
                    "protein_target": 180,
                    "carbs_target": 336,
                    "fat_target": 65,
                    "green_low": 2500,
                    "green_high": 2750,
                    "yellow_low": 2350,
                    "yellow_high": 2950,
                }
            },
        },
    )
    assert err is None
    assert initial["active_mode"] == "cut"

    clock["value"] = "2026-04-07T07:30:00"
    adjusted, err = npdb.update_week_template_macro_settings(
        template["id"],
        {
            "active_mode": "cut",
            "modes": {
                "cut": {
                    "kcal_target": 2600,
                    "protein_target": 180,
                    "carbs_target": 323,
                    "fat_target": 65,
                    "green_low": 2450,
                    "green_high": 2700,
                    "yellow_low": 2300,
                    "yellow_high": 2900,
                }
            },
        },
    )
    assert err is None
    assert adjusted["modes"]["cut"]["kcal_target"] == 2600

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT start_date, end_date, snapshot_json
        FROM nutrition_week_template_timeline
        WHERE week_template_id=?
        ORDER BY start_date ASC, id ASC
        """,
        (template["id"],),
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["start_date"] == "2026-04-05"
    assert rows[0]["end_date"] == "2026-04-06"
    assert rows[1]["start_date"] == "2026-04-07"
    assert rows[1]["end_date"] is None

    first_snapshot = json.loads(rows[0]["snapshot_json"])
    second_snapshot = json.loads(rows[1]["snapshot_json"])
    assert first_snapshot["active_mode_settings"]["kcal_target"] == 2650
    assert second_snapshot["active_mode_settings"]["kcal_target"] == 2600


def test_week_template_timeline_repairs_legacy_single_row_backfill(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    now = "2026-04-09T12:00:00"
    cur.execute(
        """
        CREATE TABLE weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            weight_kg REAL,
            kcal INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fat INTEGER,
            sugar INTEGER,
            created_at TEXT NOT NULL,
            raw_json TEXT
        )
        """
    )
    cur.execute(
        """
        INSERT INTO nutrition_week_templates
            (id, title, is_active, archived_at, last_used_at, created_at, updated_at, macro_active_mode, macro_modes_json)
        VALUES
            (16, 'Standardwoche', 0, NULL, '2026-04-05T18:59:02', '2026-02-08T22:15:58', '2026-04-05T18:59:02', 'lean_bulk',
             '{"active_mode":"lean_bulk","modes":{"lean_bulk":{"kcal_target":3800,"protein_target":145,"carbs_target":580,"fat_target":100,"green_low":3650,"green_high":4450,"yellow_low":3500,"yellow_high":4800}}}'),
            (17, 'Mini-Cut', 1, NULL, '2026-04-05T18:59:08', '2026-04-05T14:06:34', '2026-04-07T16:26:52', 'cut',
             '{"active_mode":"cut","modes":{"cut":{"kcal_target":2650,"protein_target":180,"carbs_target":336,"fat_target":65,"green_low":2400,"green_high":2750,"yellow_low":2250,"yellow_high":2900}}}')
        """
    )
    for template_id, kcal, p, c, f in ((16, 3800, 145, 580, 100), (17, 2650, 180, 336, 65)):
        for weekday in range(7):
            cur.execute(
                """
                INSERT INTO nutrition_week_template_days
                    (week_template_id, weekday, day_type, target_kcal, target_p, target_c, target_f, created_at, updated_at)
                VALUES (?, ?, 'standard', ?, ?, ?, ?, ?, ?)
                """,
                (template_id, weekday, kcal, p, c, f, now, now),
            )
    cur.execute(
        """
        INSERT INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json)
        VALUES ('20.03.26', '2026-03-20', NULL, 4200, NULL, NULL, NULL, NULL, ?, '{}')
        """,
        (now,),
    )
    cur.execute(
        """
        INSERT INTO nutrition_week_template_timeline
            (week_template_id, start_date, end_date, snapshot_json, created_at, updated_at)
        VALUES (17, '2022-04-01', NULL, '{}', ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    conn.close()

    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    conn = connections.get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT week_template_id, start_date, end_date
        FROM nutrition_week_template_timeline
        ORDER BY start_date ASC, id ASC
        """
    ).fetchall()
    conn.close()

    assert [(row["week_template_id"], row["start_date"], row["end_date"]) for row in rows] == [
        (16, "2026-02-08", "2026-04-04"),
        (17, "2026-04-05", None),
    ]
