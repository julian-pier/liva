from app import _today_agenda_is_training_duplicate, _today_agenda_should_hide_calendar_title


def test_today_agenda_hides_family_name_calendar_entries():
    assert _today_agenda_should_hide_calendar_title("Jonas Berufsschule") is True
    assert _today_agenda_should_hide_calendar_title("Mama Arzt") is True
    assert _today_agenda_should_hide_calendar_title("Papa Termin") is True
    assert _today_agenda_should_hide_calendar_title("Carsten Geburtstag") is True
    assert _today_agenda_should_hide_calendar_title("Heike Besuch") is True


def test_today_agenda_keeps_other_calendar_entries():
    assert _today_agenda_should_hide_calendar_title("Berufsschule Jonas") is False
    assert _today_agenda_should_hide_calendar_title("Familienessen") is False
    assert _today_agenda_should_hide_calendar_title("") is False
    assert _today_agenda_should_hide_calendar_title(None) is False


def test_today_agenda_dedupes_planned_training_against_calendar_training():
    existing = [
        {"kind": "gym", "title": "Push B · OHP + Beinpresse + Chest Volume"},
    ]
    candidate = {"kind": "calendar", "title": "Training · Push B · OHP + Beinpresse + Chest Volume"}
    assert _today_agenda_is_training_duplicate(candidate, existing) is True
