import importlib

import database.connections as connections


def test_search_logging_foods_finds_fresh_non_recent_food(tmp_path):
    nutrition_db = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(nutrition_db)

    import nutrition.nutrition_planning_db as npdb

    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    conn = connections.get_nutrition_db()
    cur = conn.cursor()

    for idx in range(40):
      cur.execute(
          """
          INSERT INTO nutrition_foods (
              name, brand, unit_default, kcal_per_100, p_per_100, c_per_100, f_per_100,
              is_favorite, is_active, name_normalized, created_at, updated_at
          ) VALUES (?, ?, 'g', 100, 10, 10, 1, 1, 1, ?, '2026-05-04', '2026-05-04')
          """,
          (f"Favorite Food {idx:02d}", "Test", f"favorite food {idx:02d}"),
      )

    cur.execute(
        """
        INSERT INTO nutrition_foods (
            name, brand, unit_default, kcal_per_100, p_per_100, c_per_100, f_per_100,
            is_favorite, is_active, name_normalized, created_at, updated_at
        ) VALUES (?, ?, 'g', 63, 11, 4, 0.2, 0, 1, ?, '2026-05-04', '2026-05-04')
        """,
        ("Arla Skyr Pfirsich", "Arla", "arla skyr pfirsich"),
    )
    conn.commit()
    conn.close()

    results = npdb.search_logging_foods("pfirsich", limit=16)

    assert any((row.get("name") or "") == "Arla Skyr Pfirsich" for row in results)
