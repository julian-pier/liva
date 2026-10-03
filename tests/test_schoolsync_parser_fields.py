from schoolsync.models import RawLessonBlock
from schoolsync.parser import normalize_blocks


def _mk_block(*, raw_text: str, subject: str | None, teacher: str | None, room: str | None, start: str = "08:00", end: str = "08:45") -> RawLessonBlock:
    return RawLessonBlock(
        raw_text=raw_text,
        selector=".lesson-card.clickable",
        class_name=None,
        title=None,
        aria_label=None,
        data_date="2026-03-23",
        data_start=start,
        data_end=end,
        data_start_datetime=None,
        data_end_datetime=None,
        data_subject=subject,
        data_teacher=teacher,
        data_room=room,
        bbox_top=None,
        bbox_bottom=None,
        bbox_left=None,
    )


def test_swapped_teacher_subject_is_fixed():
    b = _mk_block(raw_text="HOR\nKU_GK1\nX0.06", subject="HOR", teacher=None, room="X0.06")
    out = normalize_blocks([b], default_date="2026-03-23", apply_lesson_filter=False)
    assert len(out) == 1
    assert out[0].subject == "KU_GK1"
    assert out[0].teacher == "HOR"


def test_info_line_can_recover_subject_teacher():
    b = _mk_block(
        raw_text="BAU\nM_LK1\nX0.03\nInfo: BAU M_LK1 X0.03",
        subject="BAU",
        teacher=None,
        room="X0.03",
    )
    out = normalize_blocks([b], default_date="2026-03-23", apply_lesson_filter=False)
    assert len(out) == 1
    assert out[0].subject == "M_LK1"
    assert out[0].teacher == "BAU"


def test_info_prefixed_subject_field_is_cleaned_to_real_subject():
    b = _mk_block(
        raw_text="HOR\nX0.06\nInfo: HOR KU_GK1 X0.06",
        subject="Info: HOR KU_GK1 X0.06",
        teacher="HOR",
        room="X0.06",
    )
    out = normalize_blocks([b], default_date="2026-03-23", apply_lesson_filter=False)
    assert len(out) == 1
    assert out[0].subject == "KU_GK1"
    assert out[0].teacher == "HOR"


def test_info_prefixed_compact_subject_field_is_cleaned_to_real_subject():
    b = _mk_block(
        raw_text="WEI\nInfo: WEIIF_GK1B0.13",
        subject="Info: WEIIF_GK1B0.13",
        teacher="WEI",
        room=None,
    )
    out = normalize_blocks([b], default_date="2026-03-23", apply_lesson_filter=False)
    assert len(out) == 1
    assert out[0].subject == "IF_GK1"
    assert out[0].teacher == "WEI"


def test_empty_phantom_entry_is_skipped():
    b = _mk_block(raw_text="", subject=None, teacher=None, room=None)
    out = normalize_blocks([b], default_date="2026-03-23", apply_lesson_filter=False)
    assert out == []
