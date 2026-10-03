from app import _today_agenda_merge_school_items, _today_agenda_school_items_should_merge, _today_agenda_school_slot


def test_school_items_merge_for_directly_adjacent_school_blocks():
    current = {
        "kind": "school",
        "_slot_index": 1,
        "end_time": "09:30",
    }
    item = {
        "kind": "school",
        "_slot_index": 2,
        "start_time": "09:55",
    }
    assert _today_agenda_school_items_should_merge(current, item) is True


def test_school_items_do_not_merge_for_different_subjects():
    current = {
        "kind": "school",
        "_slot_index": 1,
        "end_time": "09:30",
    }
    item = {
        "kind": "meal",
        "start_time": "09:30",
    }
    assert _today_agenda_school_items_should_merge(current, item) is False


def test_school_slot_uses_canonical_standard_times():
    slot = _today_agenda_school_slot("09:55", "11:45")
    assert slot == {"slot_index": 2, "start_time": "09:55", "end_time": "11:25"}


def test_school_items_merge_only_with_no_other_agenda_item_between():
    items = [
        {"kind": "school", "title": "Mathe", "subtitle": "", "source_label": "Schule", "start_time": "08:00", "end_time": "09:30", "time_label": "08:00–09:30", "all_day": False, "_teacher": "Herr A", "_room": "R1", "_slot_index": 1},
        {"kind": "school", "title": "Deutsch", "subtitle": "", "source_label": "Schule", "start_time": "09:55", "end_time": "11:25", "time_label": "09:55–11:25", "all_day": False, "_teacher": "Frau B", "_room": "R2", "_slot_index": 2},
        {"kind": "meal", "title": "Snack", "subtitle": "", "source_label": "Meals", "start_time": "09:30", "end_time": None, "time_label": "09:30", "all_day": False},
        {"kind": "school", "title": "Englisch", "subtitle": "", "source_label": "Schule", "start_time": "11:45", "end_time": "13:15", "time_label": "11:45–13:15", "all_day": False, "_teacher": "Herr C", "_room": "R3", "_slot_index": 3},
    ]
    merged = _today_agenda_merge_school_items(items)
    assert [item["kind"] for item in merged] == ["school", "meal", "school"]
    assert merged[0]["time_label"] == "08:00–11:25"
    assert merged[1]["title"] == "Snack"
    assert merged[2]["time_label"] == "11:45–13:15"
