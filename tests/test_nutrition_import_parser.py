from nutrition.planning_mealplan_import import parse_mealplan_text


def test_parser_parses_days_and_meals():
    sample = """## Mo\n- Meal 1: PBJ Brot (1x)\n  - 1 pcs Brot\n  - 15 g Erdnussbutter\n  - 15 g Marmelade\n\n## Di\n- Meal 2: Porridge Cinnamon (1x)\n  - 150 g Haferflocken (zart)\n  - 375 ml Milch\n"""
    result = parse_mealplan_text(sample)
    assert result["days"]
    assert result["days"][0]["weekday"] == 0
    first_meal = result["days"][0]["meals"][0]
    assert first_meal["slot"] == 1
    assert len(first_meal["items"]) == 3
    assert all(item.get("parsed") for item in first_meal["items"])
    assert not any(issue.get("severity") == "error" for issue in result["issues"])


def test_parser_reports_errors_for_orphaned_items():
    sample = """  - 10 g Zucker\n"""
    result = parse_mealplan_text(sample)
    assert any(issue.get("message") for issue in result["issues"])
    assert any(issue.get("severity") == "error" for issue in result["issues"])


def test_parser_duplicates_meals_for_multi_day_headers():
    sample = """## Mo, Do
- [06:40] Meal 1: Frühstück zuhause (1x)
  - 1 pcs Brot
  - 15 g Erdnussbutter

## Di, Mi, Fr
- [09:30] Meal 2: Schul-Meal (1x)
  - 225 g Hähnchenbrust
"""
    result = parse_mealplan_text(sample)

    assert [day["weekday"] for day in result["days"]] == [0, 1, 2, 3, 4]
    monday = result["days"][0]
    thursday = result["days"][3]
    assert monday["meals"][0]["time_text"] == "06:40"
    assert thursday["meals"][0]["title"] == "Frühstück zuhause"
    assert len(monday["meals"][0]["items"]) == 2
    assert len(thursday["meals"][0]["items"]) == 2

    for weekday in (1, 2, 4):
        day = next(day for day in result["days"] if day["weekday"] == weekday)
        assert day["meals"][0]["slot"] == 2
        assert day["meals"][0]["title"] == "Schul-Meal"

    assert not any(issue.get("severity") == "error" for issue in result["issues"])


def test_parser_supports_timed_generic_meal_titles_for_multi_day_headers():
    sample = """## Mo, Do
- [20:30] Abendessen: Abendessen + Milch + Whey (1x)
  - 1 pcs Abendessen (Pauschale)
  - 500 ml Milch
  - 30 g Whey
"""
    result = parse_mealplan_text(sample)

    monday = next(day for day in result["days"] if day["weekday"] == 0)
    thursday = next(day for day in result["days"] if day["weekday"] == 3)
    assert monday["meals"][0]["slot"] == 1
    assert monday["meals"][0]["title"] == "Abendessen + Milch + Whey"
    assert monday["meals"][0]["time_text"] == "20:30"
    assert len(thursday["meals"][0]["items"]) == 3
    assert not any(issue.get("severity") == "error" for issue in result["issues"])
