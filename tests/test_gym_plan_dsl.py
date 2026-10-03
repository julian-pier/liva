from plans.gym_plan_dsl import (
    _ensure_plan_shape,
    apply_rpe_cap,
    apply_week_rules_to_plan,
    default_rules_json,
    export_gym_plan_dsl,
    parse_gym_plan_dsl,
    scale_sets,
)


def _mo_event(parsed):
    mo = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Mo")
    return mo["events"][0]


def test_parse_day_gym_event_with_two_exercises():
    text = """
## Mo
- [18:30] Gym – Upper A (1x)
  - Schrägbankdrücken (Smith): 4x6-10 @RPE 8/8/8/9
  - Rudern Kabel: 3x8-12 @RPE 8/8/9
""".strip()

    parsed = parse_gym_plan_dsl(text, known_exercises={"Schrägbankdrücken", "Rudern Kabel"})
    event = _mo_event(parsed)
    assert event["kind"] == "gym"
    assert len(event["items"]) == 2
    assert event["items"][0]["kind"] == "exercise"
    assert event["items"][0]["variation"] == "Smith"


def test_parse_run_event_with_hr_zone():
    text = """
## Di
- [16:30] Run – Easy Z2 (1x)
  - 25 min @Puls 65–75% HFmax (locker)
""".strip()

    parsed = parse_gym_plan_dsl(text)
    di = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Di")
    item = di["events"][0]["items"][0]
    assert di["events"][0]["kind"] == "run"
    assert item["kind"] == "run_detail"
    assert item["duration_min"] == 25
    assert item["duration_max"] == 25
    assert item["hr_low"] == 65
    assert item["hr_high"] == 75


def test_parse_rest_event():
    text = """
## Mi
- [—] Rest (1x)
""".strip()

    parsed = parse_gym_plan_dsl(text)
    mi = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Mi")
    assert mi["events"][0]["kind"] == "rest"
    assert mi["events"][0]["time"] is None


def test_parse_block_section():
    text = """
## Squat Block (8 Wochen)
- [Di] Back Squat (schwer) (1x)
  - Woche 1-3: 4x5 @RPE 7-8
""".strip()

    parsed = parse_gym_plan_dsl(text)
    assert "Squat Block (8 Wochen)" in parsed["blocks_json"]
    block = parsed["blocks_json"]["Squat Block (8 Wochen)"]
    assert len(block) == 1
    assert block[0]["day"] == "Di"
    assert block[0]["title"] == "Back Squat (schwer)"


def test_ref_block_detection():
    text = """
## Fr
- [18:00] Gym – Lower (1x)
  - Back Squat (Block): siehe "Squat Block (8 Wochen)" unten
""".strip()

    parsed = parse_gym_plan_dsl(text)
    fr = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Fr")
    item = fr["events"][0]["items"][0]
    assert item["kind"] == "ref_block"
    assert item["block_name"] == "Squat Block (8 Wochen)"
    assert item["display_name"] == "Back Squat"


def test_rpe_mismatch_warning():
    text = """
## So
- [18:00] Gym – Fullbody (1x)
  - Bench Press: 4x5-6 @RPE 8/8
""".strip()

    parsed = parse_gym_plan_dsl(text)
    kinds = {w["kind"] for w in parsed["warnings"]}
    assert "rpe_mismatch" in kinds


def test_roundtrip_export_parse_core_stability():
    text = """
## Mo
- [18:30] Gym – Upper A (1x)
  - Schrägbankdrücken (Smith): 4x6-10 @RPE 8/8/8/9

## Squat Block (8 Wochen)
- [Di] Back Squat (schwer) (1x)
  - Woche 1-3: 4x5 @RPE 7-8
""".strip()

    p1 = parse_gym_plan_dsl(text)
    out = export_gym_plan_dsl(p1["plan_json"], p1["blocks_json"], {})
    p2 = parse_gym_plan_dsl(out)
    e1 = _mo_event(p1)
    e2 = _mo_event(p2)
    assert e1["items"][0]["name"] == e2["items"][0]["name"]
    assert "Squat Block (8 Wochen)" in p2["blocks_json"]


def test_auto_link_thursday_squat_to_block_ref():
    text = """
## Do
- [18:30] Gym – Lower B (Quads/Volumen) (1x)
  - Squats (LH): 4x6-10 @RPE 7/7/8/8

## Squat Block (8 Wochen)
- [Di] Squats (LH) – schwer (1x)
  - Woche 1–3: 1x6-8 @RPE 8 + 3x6-8 @RPE 7/7/8
- [Do] Squats (LH) – Volumen (1x)
  - Woche 1–3: 4x6-10 @RPE 7/7/8/8
""".strip()
    parsed = parse_gym_plan_dsl(text)
    do = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Do")
    item = do["events"][0]["items"][0]
    assert item["kind"] == "ref_block"
    assert item["display_name"] == "Squats"
    assert item["variation"] == "LH"
    assert item["block_name"] == "Squat Block (8 Wochen)"
    assert item["block_day"] == "Do"
    assert any(w["kind"] == "auto_block_ref" for w in parsed["warnings"])


def test_parse_run_duration_range_with_bpm():
    text = """
## Di
- [16:30] Run – Easy Z2 (1x)
  - 25-30 min @Puls 155–165 bpm
""".strip()
    parsed = parse_gym_plan_dsl(text)
    di = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Di")
    item = di["events"][0]["items"][0]
    assert item["duration_min"] == 25
    assert item["duration_max"] == 30
    assert item["hr_low"] == 155
    assert item["hr_high"] == 165


def test_export_includes_week_rules_section():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 9/9/9
""".strip()
    )
    rules = {
        "W1": {"tag": "Build", "rpe_cap": 9, "strength_factor": 1.0, "run_factor": 1.0, "deload": False},
        "W2": {"tag": "Overreach", "rpe_cap": 9.5, "strength_factor": 1.05, "run_factor": 1.0, "deload": False},
        "W3": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.8, "run_factor": 0.8, "deload": True},
    }
    out = export_gym_plan_dsl(parsed["plan_json"], parsed["blocks_json"], rules)
    assert "## Wochenregeln" in out
    assert "- W2: phase=Overreach; rpe_cap=9.5; strength_factor=1.05; run_factor=1; deload=false" in out


def test_parse_week_rules_section_from_export_format():
    text = """
## Wochenregeln
- W1: phase=Build; rpe_cap=9; strength_factor=1; run_factor=1; deload=false
- W2: phase=Overreach; rpe_cap=9.5; strength_factor=1.1; run_factor=1; deload=no
- W3: phase=Deload; rpe_cap=7; strength_factor=0.8; run_factor=0.8; deload=true
""".strip()
    parsed = parse_gym_plan_dsl(text)
    assert parsed["rules_json"]["W1"]["tag"] == "Build"
    assert parsed["rules_json"]["W2"]["rpe_cap"] == 9.5
    assert parsed["rules_json"]["W2"]["deload"] is False
    assert parsed["rules_json"]["W3"]["deload"] is True


def test_apply_rpe_cap_examples_and_edges():
    assert apply_rpe_cap([9, 8, 8], 7) == [7.0, 6.0, 6.0]
    assert apply_rpe_cap([8, 8, 9], 7) == [6.0, 6.0, 7.0]
    assert apply_rpe_cap([9, 9, 9], 7) == [7.0, 7.0, 7.0]
    assert apply_rpe_cap([7, 6, 6], 7) == [7, 6, 6]
    assert apply_rpe_cap([7, 6, 6], 9) == [7, 6, 6]
    assert apply_rpe_cap([], 7) == []
    invalid = apply_rpe_cap([9, float("nan"), 8], 7)
    assert invalid[0] == 9
    assert invalid[2] == 8
    assert str(invalid[1]).lower() == "nan"
    assert apply_rpe_cap([9, 8, 1], 6, {"min_rpe": 5.0, "round_step": 0.5}) == [6.0, 5.0, 5.0]


def test_scale_sets_respects_factor_priority_and_caps():
    assert scale_sets(3, 1.0, "secondary") == 3
    assert scale_sets(3, 1.1, "assist") == 3
    assert scale_sets(3, 1.2, "assist") == 4
    assert scale_sets(3, 1.5, "assist") == 4
    assert scale_sets(3, 1.5, "main", {"main_max_step_up": 0}) == 3
    assert scale_sets(3, 0.7, "assist") == 2
    assert scale_sets(1, 0.3, "assist") == 1


def test_deload_rule_scales_sets_and_rpe_cap():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 9/8/8
- [19:30] Run – Easy Z2 (1x)
  - 30 min @Puls 65–75% HFmax
""".strip()
    )
    rules = {
        "W1": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.7, "run_factor": 0.8, "deload": True},
    }
    adjusted = apply_week_rules_to_plan(parsed["plan_json"], rules, "W1")
    mo = next(d for d in adjusted["days"] if d["day"] == "Mo")
    gym = next(e for e in mo["events"] if e["kind"] == "gym")
    run = next(e for e in mo["events"] if e["kind"] == "run")
    ex = gym["items"][0]
    run_item = run["items"][0]
    assert ex["sets"] == 2
    assert ex["rpe_list"] == [7.0, 6.0]
    assert run_item["duration_min"] == 25
    assert run_item["duration_max"] == 25
    assert "25 min" in run_item["text"]


def test_export_can_apply_active_week_rules():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 9/8/8
""".strip()
    )
    rules = {"W1": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.7, "run_factor": 1.0, "deload": True}}
    out = export_gym_plan_dsl(
        parsed["plan_json"],
        parsed["blocks_json"],
        rules,
        week_label="W1",
        apply_week_rules=True,
    )
    assert "Bench (LH): 2x6-8 @RPE 7/6" in out


def test_parse_ergo_event_and_rules_header_variant():
    text = """
## Di
- [19:05] Ergo – Easy Z1-2 (1x)
  - 20-35 min @Puls 140–155 bpm

## Wochenregeln (6 Wochen)
- W1: phase=Build; rpe_cap=9; strength_factor=1.0; run_factor=1.0; deload=false
""".strip()
    parsed = parse_gym_plan_dsl(text)
    di = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Di")
    event = di["events"][0]
    assert event["kind"] == "ergo"
    assert event["items"][0]["kind"] == "run_detail"
    assert parsed["rules_json"]["W1"]["rpe_cap"] == 9
    assert parsed["plan_json"]["weeks"] == 6


def test_hammer_curls_synonym_is_canonicalized_to_hammers():
    parsed = parse_gym_plan_dsl(
        """
## Mi
- [18:30] Gym – PULL (1x)
  - Hammer Curls (KH): 2x6-10 @RPE 8/9
""".strip()
    )
    mi = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Mi")
    event = mi["events"][0]
    assert event["items"][0]["name"] == "Hammers"


def test_apply_week_rules_scales_ergo_duration():
    parsed = parse_gym_plan_dsl(
        """
## Fr
- [12:55] Ergo – Easy Z1-2 (1x)
  - 20-35 min @Puls 140–155 bpm
""".strip()
    )
    rules = {"W1": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.8, "run_factor": 0.9, "deload": True}}
    adjusted = apply_week_rules_to_plan(parsed["plan_json"], rules, "W1")
    fr = next(d for d in adjusted["days"] if d["day"] == "Fr")
    ergo = fr["events"][0]["items"][0]
    assert ergo["duration_min"] == 20
    assert ergo["duration_max"] == 30


def test_week_logic_and_week_rules_headers_are_not_block_entries():
    parsed = parse_gym_plan_dsl(
        """
## Wochenlogik (neu, simpel & nicht “Over”-bescheuert)

## Wochenregeln (angepasst, weniger “Over”-Stress)
- W1: phase=Build; rpe_cap=9; strength_factor=1.0; run_factor=1.0; deload=false
""".strip()
    )
    assert "Wochenlogik (neu, simpel & nicht “Over”-bescheuert)" not in parsed["blocks_json"]
    assert "Wochenregeln (angepasst, weniger “Over”-Stress)" not in parsed["blocks_json"]
    assert parsed["rules_json"]["W1"]["rpe_cap"] == 9


def test_rules_rows_define_plan_weeks_even_without_header_week_count():
    parsed = parse_gym_plan_dsl(
        """
## Wochenregeln
- W1: phase=Build; rpe_cap=9; strength_factor=1.0; run_factor=1.0; deload=false
- W6: phase=Deload; rpe_cap=7; strength_factor=0.8; run_factor=0.9; deload=true
""".strip()
    )
    assert parsed["plan_json"]["weeks"] == 6


def test_parse_run_ref_block_without_display_name():
    parsed = parse_gym_plan_dsl(
        """
## Sa
- [11:00] Run – Long (1x)
  - siehe "Run Block (6 Wochen)" unten
""".strip()
    )
    sa = next(d for d in parsed["plan_json"]["days"] if d["day"] == "Sa")
    event = sa["events"][0]
    item = event["items"][0]
    assert event["kind"] == "run"
    assert item["kind"] == "ref_block"
    assert item["block_name"] == "Run Block (6 Wochen)"


def test_export_run_ref_block_without_display_name():
    parsed = parse_gym_plan_dsl(
        """
## Sa
- [11:00] Run – Long (1x)
  - siehe "Run Block (6 Wochen)" unten
""".strip()
    )
    out = export_gym_plan_dsl(parsed["plan_json"], parsed["blocks_json"], parsed["rules_json"])
    assert '  - siehe "Run Block (6 Wochen)" unten' in out


def test_plan_without_mode_defaults_to_fixed_week():
    plan = _ensure_plan_shape({"days": [{"day": "Mo", "events": [{"kind": "gym", "title": "FB"}]}]})
    assert plan["meta"]["mode"] == "fixed_week"
    assert plan["meta"]["periodization_enabled"] is False


def test_rolling_sequence_normalizes_without_weekdays():
    plan = _ensure_plan_shape(
        {
            "meta": {"mode": "rolling_sequence"},
            "sequence": [
                {"kind": "gym", "title": "FB"},
                {"kind": "gym", "title": "Push A"},
                {"kind": "bike", "title": "Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "Z2"}]},
            ],
        }
    )
    assert plan["meta"]["mode"] == "rolling_sequence"
    assert [event["title"] for event in plan["sequence"]] == ["FB", "Push A", "Z2"]
    assert plan["sequence"][0]["id"]
    assert plan["sequence"][2]["kind"] == "cardio"
    assert plan["sequence"][2]["mode"] == "bike"
    assert isinstance(plan["sequence"][2]["items"], list)
    assert plan["meta"]["rolling_week_pattern"]["Di"] == "optional"


def test_rolling_sequence_normalizes_exercises_alias_into_editor_items():
    plan = _ensure_plan_shape(
        {
            "meta": {"mode": "rolling_sequence"},
            "sequence": [
                {
                    "kind": "gym",
                    "title": "Push A",
                    "exercises": [
                        {
                            "name": "Schrägbankdrücken",
                            "device": "KH",
                            "sets": 3,
                            "rep_range": {"min": 6, "max": 10},
                            "rpe_list": [8, 9, 9],
                            "note": "Anchor",
                        }
                    ],
                }
            ],
        }
    )
    event = plan["sequence"][0]
    item = event["items"][0]
    assert len(event["items"]) == 1
    assert item["kind"] == "exercise"
    assert item["variation"] == "KH"
    assert item["reps"] == {"min": 6, "max": 10}
    assert item["rpe_list"] == [8, 9, 9]


def test_fixed_week_normalizes_strength_exercises_alias_into_editor_items():
    plan = _ensure_plan_shape(
        {
            "days": [
                {
                    "day": "Mo",
                    "events": [
                        {
                            "kind": "gym",
                            "title": "Upper",
                            "strength_exercises": [
                                {
                                    "name": "Rows",
                                    "variation": "Kabel",
                                    "sets": 2,
                                    "reps": {"min": 8, "max": 12},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    item = plan["days"][0]["events"][0]["items"][0]
    assert item["kind"] == "exercise"
    assert item["name"] == "Rows"
    assert item["variation"] == "Kabel"
    assert item["reps"] == {"min": 8, "max": 12}


def test_rolling_pattern_normalization_fills_missing_and_invalid_days():
    plan = _ensure_plan_shape({"meta": {"rolling_week_pattern": {"Mo": "TRAIN", "Di": "foo"}}})
    assert plan["meta"]["rolling_week_pattern"] == {
        "Mo": "train",
        "Di": "optional",
        "Mi": "train",
        "Do": "rest",
        "Fr": "train",
        "Sa": "optional",
        "So": "rest",
    }


def test_parse_rolling_rotation_dsl_sets_mode_and_sequence():
    parsed = parse_gym_plan_dsl(
        """
## Rotation
- [Gym] FB (1x)
  - Bankdrücken: 3x6-10 @RPE 7/8/8
- [Cardio: Bike] Zone 2 (1x)
  - 30 min @Z2
""".strip()
    )
    assert parsed["plan_json"]["meta"]["mode"] == "rolling_sequence"
    assert len(parsed["plan_json"]["sequence"]) == 2
    assert parsed["plan_json"]["sequence"][1]["kind"] == "cardio"
    assert parsed["plan_json"]["sequence"][1]["mode"] == "bike"


def test_parse_rolling_rotation_with_h1_sets_title_without_context_warning():
    parsed = parse_gym_plan_dsl(
        """
# Rolling Plan
## Rotation
- [Gym] Push A (1x)
""".strip()
    )
    assert parsed["plan_json"]["meta"]["title"] == "Rolling Plan"
    assert not any(w["kind"] == "line_without_context" for w in parsed["warnings"])


def test_export_rolling_rotation_dsl_roundtrip():
    plan = _ensure_plan_shape(
        {
            "meta": {"mode": "rolling_sequence"},
            "sequence": [{"kind": "gym", "title": "Pull A", "frequency": 1, "items": []}],
        }
    )
    text = export_gym_plan_dsl(plan, {}, default_rules_json(8))
    assert "## Rotation" in text
    assert "- [Gym] Pull A (1x)" in text


def test_export_rolling_cardio_keeps_bike_mode_and_roundtrip():
    parsed = parse_gym_plan_dsl(
        """
## Rotation
- [Cardio: Bike] Zone 2 (1x)
  - 30 min @Z2
""".strip()
    )
    text = export_gym_plan_dsl(parsed["plan_json"], parsed["blocks_json"], parsed["rules_json"])
    reparsed = parse_gym_plan_dsl(text)
    assert "[Cardio: Bike] Zone 2 (1x)" in text
    assert reparsed["plan_json"]["meta"]["mode"] == "rolling_sequence"
    assert len(reparsed["plan_json"]["sequence"]) == 1
    assert reparsed["plan_json"]["sequence"][0]["mode"] == "bike"
    assert reparsed["plan_json"]["sequence"][0]["title"] == "Zone 2"


def test_single_rpe_value_expands_to_all_sets():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 8
""".strip()
    )
    item = parsed["plan_json"]["days"][0]["events"][0]["items"][0]
    assert item["rpe_list"] == [8.0, 8.0, 8.0]


def test_cardio_duration_line_sets_duration_min_and_max():
    parsed = parse_gym_plan_dsl(
        """
## Rotation
- [Cardio: Bike] Zone 2 (1x)
  - 30 min @Z2
""".strip()
    )
    item = parsed["plan_json"]["sequence"][0]["items"][0]
    assert item["kind"] == "cardio"
    assert item["duration_min"] == 30
    assert item["duration_max"] == 30
    assert item["intensity"] == "Z2"


def test_fixed_week_export_keeps_weekday_sections():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
""".strip()
    )
    text = export_gym_plan_dsl(parsed["plan_json"], parsed["blocks_json"], parsed["rules_json"])
    assert "## Mo" in text
    assert "## Rotation" not in text


def test_periodization_disabled_does_not_apply_rpe_caps():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 9/8/8
""".strip()
    )
    parsed["plan_json"]["meta"]["periodization_enabled"] = False
    rules = {"W1": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.7, "cardio_factor": 0.8, "deload": True}}
    adjusted = apply_week_rules_to_plan(parsed["plan_json"], rules, "W1", respect_periodization_enabled=True)
    ex = adjusted["days"][0]["events"][0]["items"][0]
    assert ex["sets"] == 3
    assert ex["rpe_list"] == [9.0, 8.0, 8.0]


def test_periodization_enabled_applies_rpe_caps():
    parsed = parse_gym_plan_dsl(
        """
## Mo
- [18:30] Gym – Upper A (1x)
  - Bench (LH): 3x6-8 @RPE 9/8/8
""".strip()
    )
    parsed["plan_json"]["meta"]["periodization_enabled"] = True
    rules = {"W1": {"tag": "Deload", "rpe_cap": 7, "strength_factor": 0.7, "cardio_factor": 0.8, "deload": True}}
    adjusted = apply_week_rules_to_plan(parsed["plan_json"], rules, "W1")
    ex = adjusted["days"][0]["events"][0]["items"][0]
    assert ex["sets"] == 2
    assert ex["rpe_list"] == [7.0, 6.0]
