from memory.memory_models import resolve_logical_files_for_domains
from memory.memory_rules import propose_memory_updates


def test_domain_mapping_resolves_training_and_projects():
    logical_files = resolve_logical_files_for_domains(["training", "projects"])
    assert "athlete_dossier" in logical_files
    assert "training_history" in logical_files
    assert "project_memory" in logical_files
    assert logical_files.index("athlete_dossier") < logical_files.index("project_memory")


def test_propose_rules_accept_stable_pattern_and_reject_noise():
    result = propose_memory_updates(
        conversation_summary="Weekly review after repeated observations.",
        candidates=[
            {
                "id": "stable_1",
                "text": "He tends to recover better with a fixed heavy-first structure and repeated deloads every fourth week.",
                "domain": "training",
                "kind": "pattern",
                "confidence": 0.88,
                "importance": "high",
                "evidence_count": 3,
            },
            {
                "id": "noise_1",
                "text": "Maybe today he was a little distracted before breakfast.",
                "domain": "reviews",
                "confidence": 0.35,
                "importance": "low",
                "evidence_count": 1,
                "speculative": True,
            },
        ],
    )

    accepted_ids = {item["id"] for item in result["accepted_candidates"]}
    rejected_ids = {item["id"] for item in result["rejected_candidates"]}
    plan_targets = {item["logical_file"] for item in result["write_plan"]}
    plan_strategies = {item["strategy"] for item in result["write_plan"]}

    assert "stable_1" in accepted_ids
    assert "noise_1" in rejected_ids
    assert "core_patterns" in plan_targets
    assert "replace_section" in plan_strategies
    assert result["requires_archive"] is True
