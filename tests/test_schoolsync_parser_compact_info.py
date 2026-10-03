from schoolsync.models import RawLessonBlock
from schoolsync.parser import normalize_blocks


def _mk_block(*, raw_text: str, subject: str | None, teacher: str | None, room: str | None) -> RawLessonBlock:
    return RawLessonBlock(
        raw_text=raw_text,
        selector=".lesson-card.clickable",
        class_name=None,
        title=None,
        aria_label=None,
        data_date="2026-06-10",
        data_start="11:45",
        data_end="13:15",
        data_start_datetime=None,
        data_end_datetime=None,
        data_subject=subject,
        data_teacher=teacher,
        data_room=room,
        bbox_top=None,
        bbox_bottom=None,
        bbox_left=None,
    )


def test_compact_info_line_recovers_teacher_subject_and_room():
    block = _mk_block(
        raw_text="B1.07\nInfo: KLGBI_GK2B1.07",
        subject="BI_GK2",
        teacher=None,
        room="B1.07",
    )

    out = normalize_blocks([block], default_date="2026-06-10", apply_lesson_filter=False)

    assert len(out) == 1
    assert out[0].subject == "BI_GK2"
    assert out[0].teacher == "KLG"
    assert out[0].room == "B1.07"
