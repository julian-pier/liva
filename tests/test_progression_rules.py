import pytest

from analysis.progression_rules import (
    DEFAULT_CONFIG,
    RULE_VERSION,
    SetPerformance,
    calculate_effort_adjusted_e1rm,
    compare_set_progress,
    has_regression_trend,
    map_progress_status,
    summarize_session_progress,
)


def s(weight, reps, rpe=8, **extra):
    return SetPerformance(
        weight=weight,
        reps=reps,
        rpe=rpe,
        exercise_name=extra.pop("exercise_name", "bench"),
        canonical_exercise_id=extra.pop("canonical_exercise_id", "bench"),
        device=extra.pop("device", "smith"),
        variation=extra.pop("variation", "normal"),
        variation_id=extra.pop("variation_id", "normal"),
        laterality=extra.pop("laterality", "bilateral"),
        execution_mode=extra.pop("execution_mode", "standard"),
        session_name=extra.pop("session_name", "push"),
        **extra,
    )


def test_rule_version_is_v6():
    assert RULE_VERSION == "progression_rules_v6"


def test_effort_adjusted_score_uses_clamped_rir():
    assert calculate_effort_adjusted_e1rm(82, 10, 9) == pytest.approx(82 * (1 + 11 / 30))
    assert calculate_effort_adjusted_e1rm(80, 8, 5) == pytest.approx(80 * (1 + 12 / 30))
    assert calculate_effort_adjusted_e1rm(80, 8, 11) == pytest.approx(80 * (1 + 8 / 30))


def test_missing_reference_is_technically_excluded():
    result = compare_set_progress(s(100, 8), None)
    assert result.status is None
    assert result.comparable is False
    assert result.exclusion_reason == "missing_reference"
    assert map_progress_status(result.status, comparable=False) == "neutral"


@pytest.mark.parametrize(
    ("current", "reference"),
    [
        (s(100, 8, variation="pause", variation_id="pause"), s(100, 8)),
    ],
)
def test_exercise_or_variation_changes_are_excluded(current, reference):
    result = compare_set_progress(current, reference)
    assert result.status is None
    assert result.comparable is False
    assert result.exclusion_reason == "different_variation"


@pytest.mark.parametrize(
    ("current", "reference"),
    [
        (s(100, 8, device="dumbbell"), s(100, 8)),
        (s(100, 8, laterality="unilateral"), s(100, 8)),
        (s(100, 8, execution_mode="paused"), s(100, 8)),
    ],
)
def test_only_exercise_and_variation_define_comparison_identity(current, reference):
    result = compare_set_progress(current, reference)
    assert result.comparable is True
    assert result.status == "stable"


def test_current_rpe_is_required_but_historical_reference_rpe_may_be_missing():
    incomplete = compare_set_progress(s(102.5, 8, None), s(100, 8, 8))
    assert incomplete.comparable is False
    assert incomplete.exclusion_reason == "missing_current_rpe"

    historical_missing = compare_set_progress(s(102.5, 8, 8), s(100, 8, None))
    assert historical_missing.comparable is True
    assert historical_missing.status == "progress"

    same_legacy_output = compare_set_progress(s(100, 8, 8), s(100, 8, None))
    assert same_legacy_output.status == "stable"
    assert same_legacy_output.deltas["score_delta_pct"] == pytest.approx(0.0)


def test_legacy_missing_current_rpe_uses_raw_mode_but_new_missing_rpe_is_excluded():
    legacy = compare_set_progress(
        s(102.5, 8, None, logged_at="2026-07-26T23:59:00Z"),
        s(100, 8, None, logged_at="2026-07-19T19:00:00Z"),
    )
    assert legacy.comparable is True
    assert legacy.comparison_mode == "legacy_raw_e1rm"
    assert legacy.status == "progress"

    new_missing_rpe = compare_set_progress(
        s(102.5, 8, None, logged_at="2026-07-27T00:00:00Z"),
        s(100, 8, 8, logged_at="2026-07-20T19:00:00Z"),
    )
    assert new_missing_rpe.comparable is False
    assert new_missing_rpe.exclusion_reason == "missing_current_rpe"


def test_legacy_load_progression_uses_the_tighter_minus_one_percent_floor():
    # +1% load but -1.65% raw e1RM: V5 adjusted could accept -2%, the
    # deterministic legacy mode must remain stable at its -1% floor.
    result = compare_set_progress(
        s(101, 7, None, logged_at="2026-07-01T19:00:00Z"),
        s(100, 8, None, logged_at="2026-06-24T19:00:00Z"),
    )
    assert result.comparison_mode == "legacy_raw_e1rm"
    assert result.status == "stable"


def test_successful_load_progression_is_progress():
    result = compare_set_progress(s(85, 7, 8), s(82, 10, 9))
    assert result.status == "progress"
    assert result.reason_code == "successful_load_progression"
    assert result.deltas["score_delta_pct"] == pytest.approx(-1.398, abs=0.01)
    assert result.deltas["load_delta_pct"] == pytest.approx(3.6585, abs=0.01)


@pytest.mark.parametrize(
    ("current", "reference"),
    [
        (s(37, 8, 9), s(35, 10, 8)),
        (s(130, 7, 8), s(127, 7, 7)),
    ],
)
def test_load_progression_accepts_one_point_higher_rpe_when_adjusted_score_is_held(current, reference):
    result = compare_set_progress(current, reference)
    assert result.status == "progress"
    assert result.reason_code == "successful_load_progression"
    assert result.deltas["rpe_delta"] == pytest.approx(1.0)
    assert result.deltas["score_delta_pct"] >= DEFAULT_CONFIG.load_progression_score_floor_pct


def test_normal_score_gain_is_progress():
    result = compare_set_progress(s(80, 9, 8), s(80, 8, 8))
    assert result.status == "progress"
    assert result.reason_code == "performance_score_up"


def test_one_more_rep_with_one_more_rpe_is_rep_progression():
    result = compare_set_progress(s(80, 11, 9), s(80, 10, 8))
    assert result.status == "progress"
    assert result.reason_code == "successful_rep_progression"
    assert result.deltas["reps_delta"] == pytest.approx(1.0)
    assert result.deltas["rpe_delta"] == pytest.approx(1.0)
    assert result.deltas["score_delta_pct"] == pytest.approx(0.0)


def test_two_more_reps_with_two_more_rpe_is_rep_progression():
    result = compare_set_progress(s(70, 9, 9), s(70, 7, 7))
    assert result.status == "progress"
    assert result.reason_code == "successful_rep_progression"
    assert result.deltas["reps_delta"] == pytest.approx(2.0)
    assert result.deltas["rpe_delta"] == pytest.approx(2.0)
    assert result.deltas["score_delta_pct"] == pytest.approx(0.0)


def test_large_rpe_increase_blocks_artificial_progress():
    result = compare_set_progress(s(80, 9, 10), s(80, 8, 8))
    assert result.status == "stable"
    assert result.reason_code == "high_effort_progress_blocked"


def test_fewer_reps_with_lower_rpe_is_stable():
    result = compare_set_progress(s(80, 9, 8), s(80, 10, 9))
    assert result.status == "stable"
    assert result.deltas["score_delta_pct"] == pytest.approx(0.0)


def test_fewer_reps_at_same_rpe_is_regress():
    result = compare_set_progress(s(80, 8, 8), s(80, 10, 8))
    assert result.status == "regress"
    assert result.reason_code == "performance_score_down"


def test_aggressive_load_jump_is_regress():
    result = compare_set_progress(s(85, 5, 10), s(80, 10, 8))
    assert result.status == "regress"


def test_lower_load_can_still_be_progress():
    result = compare_set_progress(s(95, 11, 8), s(100, 8, 10))
    assert result.status == "progress"
    assert result.reason_code == "performance_score_up"


@pytest.mark.parametrize(
    ("score_current", "score_reference", "expected"),
    [
        (s(100, 8, 8), s(100, 8, 8), "stable"),
        (s(101, 8, 8), s(100, 8, 8), "progress"),
        (s(97, 8, 8), s(100, 8, 8), "regress"),
    ],
)
def test_hard_classification_corridors(score_current, score_reference, expected):
    assert compare_set_progress(score_current, score_reference).status == expected


def test_session_rollup_uses_plus_zero_minus_one():
    progress = compare_set_progress(s(102, 8), s(100, 8))
    stable = compare_set_progress(s(100, 8), s(100, 8))
    regress = compare_set_progress(s(95, 8), s(100, 8))

    positive = summarize_session_progress([progress] * 4 + [stable] * 3 + [regress])
    assert positive.status == "progress"
    assert positive.progress == 4
    assert positive.stable == 3
    assert positive.regress == 1
    assert positive.net_progress == pytest.approx(0.375)

    negative = summarize_session_progress([progress] + [stable] * 3 + [regress] * 4)
    assert negative.status == "regress"
    assert negative.net_progress == pytest.approx(-0.375)


def test_exclusions_do_not_enter_session_denominator():
    excluded = compare_set_progress(s(100, 8), None)
    stable = compare_set_progress(s(100, 8), s(100, 8))
    summary = summarize_session_progress([excluded, stable])
    assert summary.comparable_sets == 1
    assert summary.excluded == 1
    assert summary.stable == 1


def test_frontend_mapping_has_only_three_performance_results():
    assert map_progress_status("progress", comparable=True) == "progress"
    assert map_progress_status("stable", comparable=True) == "neutral"
    assert map_progress_status("regress", comparable=True) == "regress"
    assert map_progress_status(None, comparable=False) == "neutral"


def test_regression_trend_requires_two_consecutive_regressions_or_one_severe_drop():
    assert has_regression_trend([100, 96]) is False
    assert has_regression_trend([100, 96, 92]) is True
    assert has_regression_trend([100, 92.9]) is True
    assert has_regression_trend([100, 96, 100]) is False
