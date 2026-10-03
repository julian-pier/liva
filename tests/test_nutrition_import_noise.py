import importlib

import database.connections as connections

from nutrition.planning_mealplan_import import parse_mealplan_text


def _build_noisy_text() -> str:
    return """## Mo
- Meal 1: Test (1x)
  - 100 g Reis
ERROR · Zeile 19
Slot 6 für Mo nicht gefunden
Alle Items zugeordnet.
Parse-Fehler
"""


def test_import_ignores_noise_when_not_strict(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()
    template, err = npdb.create_week_template("Noise Test", set_active=True)
    assert err is None
    assert template

    text = _build_noisy_text()
    parsed = parse_mealplan_text(text)
    assert any(issue.get("severity") == "warning" for issue in parsed.get("issues", []))

    result = npdb.import_mealplan_text(text, template["id"], strict=False, apply=True)
    assert result["ok"] is True


def test_import_fails_on_noise_when_strict(tmp_path):
    db_path = tmp_path / "nutrition.sqlite3"
    connections.NUTRITION_DB = str(db_path)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()
    template, err = npdb.create_week_template("Noise Test Strict", set_active=True)
    assert err is None
    assert template

    text = _build_noisy_text()
    result = npdb.import_mealplan_text(text, template["id"], strict=True, apply=False)
    assert result["ok"] is False
    assert any(issue.get("severity") == "error" for issue in result.get("errors", []))
