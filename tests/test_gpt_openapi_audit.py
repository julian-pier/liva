import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _spec():
    return json.loads((REPO / "openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))


def _walk(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_gpt_openapi_files_are_semantically_identical_and_reproducible():
    import ai.actions_v2 as actions_v2

    checked_in = _spec()
    yaml_compatible = json.loads((REPO / "openapi/liva-actions-gpt.yaml").read_text(encoding="utf-8"))
    assert checked_in == yaml_compatible
    assert checked_in == actions_v2.build_gpt_openapi_spec()


def test_gpt_openapi_refs_are_resolvable_and_components_are_reachable():
    spec = _spec()
    schemas = spec["components"]["schemas"]
    refs = list(_walk(spec["paths"]))
    assert all(ref.split("/")[-1] in schemas for ref in refs if ref.startswith("#/components/schemas/"))

    reachable = set()
    pending = [ref.split("/")[-1] for ref in refs if ref.startswith("#/components/schemas/")]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(
            ref.split("/")[-1]
            for ref in _walk(schemas[name])
            if ref.startswith("#/components/schemas/") and ref.split("/")[-1] not in reachable
        )
    assert reachable == set(schemas)


def test_gpt_openapi_paths_are_registered_with_matching_methods():
    from app import app

    spec = _spec()
    for path, operations in spec["paths"].items():
        flask_path = re.sub(r"\{([^}]+)\}", r"<int:\1>", path)
        rule = next((item for item in app.url_map.iter_rules() if item.rule == flask_path), None)
        assert rule is not None, path
        for method in operations:
            if method in {"get", "post", "put", "patch", "delete"}:
                assert method.upper() in rule.methods, (path, method)


def test_gpt_openapi_operations_and_auth_contract_are_complete():
    spec = _spec()
    operations = [
        operation
        for methods in spec["paths"].values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    ids = [operation["operationId"] for operation in operations]
    assert len(ids) == len(set(ids))
    assert all(operation.get("security") == [{"BearerAuth": []}] for operation in operations)
    assert all({"200", "400", "401"}.issubset(operation["responses"]) for operation in operations)


def test_gpt_openapi_uses_typed_core_write_requests():
    schemas = _spec()["components"]["schemas"]
    assert schemas["TrainingAdjustRequest"]["properties"]["exercises"]["items"]["$ref"] == "#/components/schemas/TrainingExerciseInput"
    assert schemas["TrainingSetInput"]["properties"]["rpe"]["description"]
    assert schemas["EditLoggedSetRequest"]["required"] == ["workout_id", "exercise_index", "set_index"]
    assert "logged_at" in schemas["NutritionControlRequest"]["properties"]
    item = schemas["NutritionMealItem"]["properties"]
    assert {"food_id", "food_name", "amount", "unit", "item_type", "kcal", "calories", "protein", "carbs", "fat", "sugar"}.issubset(item)
    assert "operations" in schemas["TrainingPlanPatchRequest"]["properties"]
    assert "operations" in schemas["NutritionPlanPatchRequest"]["properties"]


def test_gpt_openapi_direct_plan_patch_actions_expose_operations():
    spec = _spec()
    schemas = spec["components"]["schemas"]
    for operation_id, schema_name in (
        ("patchTrainingPlanDirect", "TrainingPlanPatchRequest"),
        ("patchNutritionPlanDirect", "NutritionPlanPatchRequest"),
    ):
        operation = next(
            operation
            for methods in spec["paths"].values()
            for method, operation in methods.items()
            if method == "post" and operation.get("operationId") == operation_id
        )
        body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body_schema == {"$ref": f"#/components/schemas/{schema_name}"}
        schema = schemas[schema_name]
        assert schema["required"] == ["operations"]
        assert schema["properties"]["operations"]["items"]["properties"]["match"]
        operation = schema["properties"]["operations"]["items"]["properties"]
        assert operation["replacement"]["properties"]["food_id"]["type"] == "integer"
        assert operation["target"]["properties"]["day_index"]["type"] == "integer"
        assert operation["food_id"]["type"] == "integer"
        assert operation["replacement"]["properties"]["exercise_id"]["type"] == "string"


def test_custom_gpt_plan_guidance_forces_typed_nutrition_patch_flow():
    prompt = (REPO / "docs/prompt_v2_custom_gpt.md").read_text(encoding="utf-8")
    guide = (REPO / "docs/actions_gpt_endpoint_guide.md").read_text(encoding="utf-8")
    assert "getNutritionPlanDetailDirect" in prompt
    assert "patchNutritionPlanDirect" in prompt
    assert "liva_operator_execute" in prompt
    assert "getNutritionPlanDetailDirect" in guide
    assert "patchNutritionPlanDirect" in guide
    assert "patch_nutrition_plan" in guide


def test_liva_act_domain_command_pairs_are_explicit():
    schema = _spec()["components"]["schemas"]["LivaActRequest"]
    matrix = schema["x-domain-command-matrix"]
    assert "change_rpe" in matrix["training"]
    assert "quick_log_meal" in matrix["nutrition"]
    assert "change_rpe" not in matrix["nutrition"]


def test_gpt_openapi_avoids_editor_incompatible_union_forms():
    spec = _spec()
    encoded = json.dumps(spec)
    assert '"oneOf"' not in encoded
    assert not any(isinstance(node.get("type"), list) for node in _schema_nodes(spec))


def _schema_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_nodes(child)
