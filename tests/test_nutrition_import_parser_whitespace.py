from nutrition.planning_mealplan_import import parse_mealplan_text


def test_parser_tolerates_tabs_and_crlf_when_not_strict():
    sample = "## Mo\r\n- Meal 1: Test (1x)\r\n\t- 100 g Reis\r\n\t- 10 g Salz\r\n"
    result = parse_mealplan_text(sample, strict=False)
    assert result["days"]
    assert result["days"][0]["meals"][0]["items"]
    assert not any(issue.get("severity") == "error" for issue in result["issues"])


def test_parser_strict_requires_indent():
    sample = "## Mo\r\n- Meal 1: Test (1x)\r\n - 100 g Reis\r\n"
    result = parse_mealplan_text(sample, strict=True)
    assert any(issue.get("severity") == "error" for issue in result["issues"])


def test_parser_parses_week_with_tabs_and_crlf():
    sample = (
        "## Mo\r\n- Meal 1: PBJ Brot (1x)\r\n\t- 1 pcs Brot\r\n\t- 15 g Erdnussbutter\r\n"
        "## Di\r\n- Meal 2: Porridge (1x)\r\n\t- 150 g Haferflocken\r\n\t- 375 ml Milch\r\n"
    )
    result = parse_mealplan_text(sample, strict=False)
    assert result["days"]
    assert not any(issue.get("severity") == "error" for issue in result["issues"])
