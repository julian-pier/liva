from scripts.operator_smoke_suite import OperatorSmokeSuite


def test_operator_smoke_suite_openapi_check_registers_result():
    suite = OperatorSmokeSuite(base_url="http://127.0.0.1:5000", token="test", live=False, cleanup=False)
    suite.check_openapi()
    assert suite.results
    assert suite.results[0].name == "openapi_required_ops"


def test_operator_smoke_suite_url_builder_keeps_query_params():
    suite = OperatorSmokeSuite(base_url="http://127.0.0.1:5000/", token="test", live=False, cleanup=False)
    assert suite.url("/api/nutrition/plan/week", template=17).endswith("/api/nutrition/plan/week?template=17")
