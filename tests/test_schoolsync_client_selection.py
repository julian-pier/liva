from schoolsync.client import WebUntisClient
from schoolsync.models import RawLessonBlock
from types import SimpleNamespace


def _block(day: str) -> RawLessonBlock:
    return RawLessonBlock(
        raw_text=f"Teacher\nSUB_{day[-2:]}\nR1.01",
        selector=".lesson-card.clickable",
        class_name=None,
        title=None,
        aria_label=None,
        data_date=day,
        data_start="08:00",
        data_end="09:55",
        data_start_datetime=None,
        data_end_datetime=None,
        data_subject="SUB_GK1",
        data_teacher="TEA",
        data_room="R1.01",
        bbox_top=None,
        bbox_bottom=None,
        bbox_left=None,
    )


def test_choose_best_block_set_prefers_week_candidate_when_today_present():
    today = "2026-06-10"
    after_week = [_block("2026-06-10"), _block("2026-06-11"), _block("2026-06-12")]
    after_month = after_week + [_block("2026-06-17"), _block("2026-06-24")]

    label, blocks = WebUntisClient._choose_best_block_set(  # noqa: SLF001
        [("after_week", after_week), ("after_month", after_month)],
        today_iso=today,
    )

    assert label == "after_week"
    assert blocks == after_week


class _BlankStartPage:
    url = "https://example.webuntis.com/"

    class _Body:
        @staticmethod
        def inner_text(*, timeout: int) -> str:
            assert timeout == 1_000
            return ""

    @classmethod
    def locator(cls, selector: str):
        assert selector == "body"
        return cls._Body()


class _TimetablePage(_BlankStartPage):
    url = "https://example.webuntis.com/WebUntis/#/main/timetable"


def test_empty_host_root_is_detected_as_expired_webuntis_session():
    assert WebUntisClient._was_redirected_to_empty_start_page(_BlankStartPage())  # noqa: SLF001


def test_webuntis_route_is_not_misclassified_as_empty_start_page():
    assert not WebUntisClient._was_redirected_to_empty_start_page(_TimetablePage())  # noqa: SLF001


def test_successful_fetch_persists_rotated_webuntis_session(tmp_path):
    calls = []

    class Context:
        @staticmethod
        def storage_state(*, path: str) -> None:
            calls.append(path)

    state_file = tmp_path / "webuntis_state.json"
    client = WebUntisClient(SimpleNamespace(state_file=state_file))
    client._persist_auth_state(Context())  # noqa: SLF001

    assert calls == [str(state_file)]
