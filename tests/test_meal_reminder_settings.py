import sqlite3

from nutrition.meal_reminder import resolve_meal_number, telegram_push_for_meal_enabled


def _mk_training_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_telegram_push_for_meal_enabled_defaults_true_and_respects_setting(tmp_path):
    db_path = tmp_path / "training.sqlite3"
    _mk_training_db(str(db_path))

    assert telegram_push_for_meal_enabled(str(db_path), 3) is True

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settings_kv (key, value, updated_ts) VALUES (?, ?, ?)",
            ("telegram_push_meal_3_enabled", "0", 0),
        )
        conn.commit()
    finally:
        conn.close()

    assert telegram_push_for_meal_enabled(str(db_path), 3) is False
    assert telegram_push_for_meal_enabled(str(db_path), 4) is True


def test_resolve_meal_number_from_slot_or_title():
    assert resolve_meal_number({"slot_index": 0, "title": "Anything"}) == 1
    assert resolve_meal_number({"title": "Meal 4 - Post Workout"}) == 4
    assert resolve_meal_number({"title": "Snack"}) is None
