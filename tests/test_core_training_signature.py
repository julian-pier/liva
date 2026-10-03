from core.core_training_deck import make_signature


def test_same_params_same_signature():
    sig1 = make_signature("a", "topic", {"x": 1, "y": 2}, {"recovery_state": "ok"})
    sig2 = make_signature("a", "topic", {"y": 2, "x": 1}, {"recovery_state": "ok"})
    assert sig1 == sig2


def test_different_params_different_signature():
    sig1 = make_signature("a", "topic", {"x": 1}, {"recovery_state": "ok"})
    sig2 = make_signature("a", "topic", {"x": 2}, {"recovery_state": "ok"})
    assert sig1 != sig2
