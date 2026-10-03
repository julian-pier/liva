from schoolsync.models import RawLessonBlock
from schoolsync.parser import normalize_blocks


def _block(raw_text: str, class_name: str | None = None) -> RawLessonBlock:
    return RawLessonBlock(
        raw_text=raw_text,
        selector=".lesson-card.clickable",
        class_name=class_name,
        title=None,
        aria_label=None,
        data_date="2026-03-02",
        data_start="08:00",
        data_end="09:55",
        data_start_datetime=None,
        data_end_datetime=None,
        data_subject="KU_GK1",
        data_teacher="HOR",
        data_room="X0.06",
        bbox_top=None,
        bbox_bottom=None,
        bbox_left=None,
    )


def test_status_hint_eva_from_substitution_text():
    entries = normalize_blocks([_block("HOR\nKU_GK1\nX0.06\nVertretungstext: EVA")], default_date="2026-03-02")
    assert len(entries) == 1
    assert entries[0].status_hint == "eva"


def test_status_hint_eva_from_class_marker():
    entries = normalize_blocks([_block("HOR\nKU_GK1\nX0.06", class_name="lesson-card subst-eva")], default_date="2026-03-02")
    assert len(entries) == 1
    assert entries[0].status_hint == "eva"


def test_status_hint_exam_from_pruefung_klausur_text():
    entries = normalize_blocks([_block("HOR\nKU_GK1\nX0.06\nPrüfung: Klausur")], default_date="2026-03-02")
    assert len(entries) == 1
    assert entries[0].status_hint == "exam"
