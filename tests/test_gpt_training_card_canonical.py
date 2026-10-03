from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_never_prefers_or_falls_back_to_core_training_card():
    script = (ROOT / "static" / "js" / "dashboard_vnext.js").read_text(encoding="utf-8")

    assert 'if (payload.coreTrainingCard && typeof payload.coreTrainingCard === "object")' not in script
    assert "snapshot?.coreTrainingCard" not in script
    assert "Es gibt keinen CORE-/Autopilot-Fallback mehr." in script


def test_dashboard_snapshot_does_not_load_legacy_core_training_card():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'data["coreTrainingCard"] = get_core_training_card(day_key)' not in source
    assert 'data["coreTrainingCard"] = None' in source
