"""Tests for the calorie parsing/conversion helpers and DB methods."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.calories import (
    format_kcal,
    kcal_to_kj,
    kj_to_kcal,
    normalize_food,
    parse_chat_message,
    parse_energy,
    parse_food_phrase,
    progress_bar,
)
from app.db import Database


# ---- conversions -----------------------------------------------------------

def test_kj_kcal_roundtrip():
    assert kj_to_kcal(kcal_to_kj(2500)) == pytest.approx(2500)


def test_kj_to_kcal_known_value():
    # Standard label maths: 8700 kJ ≈ 2079 kcal (the adult daily-intake guide).
    assert kj_to_kcal(8700) == pytest.approx(2079.3, abs=0.1)


# ---- parse_energy ----------------------------------------------------------

@pytest.mark.parametrize("text,expected_kcal,expected_unit", [
    ("650", 650.0, "kcal"),
    ("650c", 650.0, "kcal"),
    ("650 C", 650.0, "kcal"),
    ("650cal", 650.0, "kcal"),
    ("650 cals", 650.0, "kcal"),
    ("650kcal", 650.0, "kcal"),
    ("650 calories", 650.0, "kcal"),
    ("1,250 cal", 1250.0, "kcal"),
    ("12.5", 12.5, "kcal"),
])
def test_parse_energy_kcal_forms(text, expected_kcal, expected_unit):
    result = parse_energy(text)
    assert result is not None
    kcal, unit = result
    assert kcal == pytest.approx(expected_kcal)
    assert unit == expected_unit


@pytest.mark.parametrize("text,kj", [
    ("2700kj", 2700.0),
    ("2700 kJ", 2700.0),
    ("2,700 kj", 2700.0),
    ("2700 kilojoules", 2700.0),
    ("418.4 kilojoule", 418.4),
])
def test_parse_energy_kj_forms(text, kj):
    result = parse_energy(text)
    assert result is not None
    kcal, unit = result
    assert kcal == pytest.approx(kj / 4.184)
    assert unit == "kj"


@pytest.mark.parametrize("text", [
    "", "lunch", "-300", "650 kg", "kj", "650x", "six hundred",
])
def test_parse_energy_rejects_garbage(text):
    assert parse_energy(text) is None


# ---- parse_chat_message ----------------------------------------------------

@pytest.mark.parametrize("text,kcal,unit", [
    ("650kcal", 650.0, "kcal"),
    ("650 cal", 650.0, "kcal"),
    ("650 cals", 650.0, "kcal"),
    ("650 calories", 650.0, "kcal"),
    ("650 calories.", 650.0, "kcal"),   # trailing punctuation tolerated
    ("650kcal!", 650.0, "kcal"),
    ("2700kj", 2700.0 / 4.184, "kj"),
    ("2,700 kJ", 2700.0 / 4.184, "kj"),
    ("418.4 kilojoules", 100.0, "kj"),
    # Bare "c" shorthand — with or without a separator.
    ("200 c", 200.0, "kcal"),
    ("200c", 200.0, "kcal"),
    ("500.c", 500.0, "kcal"),
    ("650c", 650.0, "kcal"),
])
def test_parse_chat_message_accepts(text, kcal, unit):
    result = parse_chat_message(text)
    assert result is not None
    got_kcal, got_unit, got_note = result
    assert got_kcal == pytest.approx(kcal)
    assert got_unit == unit
    assert got_note is None  # chat posts never carry a note now


@pytest.mark.parametrize("text", [
    "650",            # bare number = could be a lift, must not match
    "bench 80kg",
    "650kg",
    "ate a lot today",
    "650kcal\nbench press 80kg",  # multi-line dumps go to the lift parser
    "5 c u later",    # trailing words → not a clean amount
    "200 cm",         # not a calorie unit
    "200 cookies",    # word starting with c isn't the c unit
    # The whole point of the strictness: amounts inside a sentence are ignored.
    "1500cal is crazy work",
    "650kcal burrito",
    "200 cal toastie",
    "2,700 kJ maccas run",
    "650 cal - big mac meal",
    "",
])
def test_parse_chat_message_rejects(text):
    assert parse_chat_message(text) is None


# ---- saved-food phrase parsing --------------------------------------------

@pytest.mark.parametrize("text,servings,name", [
    ("coffee", 1, "coffee"),
    ("Coffee", 1, "coffee"),
    ("  protein   shake ", 1, "protein shake"),
    ("2 coffee", 2, "coffee"),
    ("2x coffee", 2, "coffee"),
    ("2 x coffee", 2, "coffee"),
    ("coffee x2", 2, "coffee"),
    ("coffee x 2", 2, "coffee"),
    ("3 protein shake", 3, "protein shake"),
])
def test_parse_food_phrase_accepts(text, servings, name):
    result = parse_food_phrase(text)
    assert result == (servings, name)


def test_parse_food_phrase_clamps_servings():
    assert parse_food_phrase("999 coffee") == (50, "coffee")


@pytest.mark.parametrize("text", [
    "",
    "coffee\nbench press 80kg",   # multi-line never a food shortcut
    "x" * 65,                      # too long
])
def test_parse_food_phrase_rejects(text):
    assert parse_food_phrase(text) is None


def test_normalize_food():
    assert normalize_food("  Protein   Shake ") == "protein shake"
    assert normalize_food("COFFEE") == "coffee"
    assert normalize_food("") == ""


def test_format_kcal_rounds_and_groups():
    assert format_kcal(2079.3) == "2,079 cal"
    assert format_kcal(650) == "650 cal"


def test_progress_bar_clamps():
    assert progress_bar(0, 2000, width=10) == "░" * 10
    assert progress_bar(2000, 2000, width=10) == "█" * 10
    assert progress_bar(9999, 2000, width=10) == "█" * 10  # overshoot clamps
    assert progress_bar(1000, 2000, width=10) == "█" * 5 + "░" * 5
    assert progress_bar(500, 0, width=10) == "·" * 10  # no target


# ---- DB methods ------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_calorie_goal_set_get_update(db):
    db.calorie_goal_set(1, 100, "alice", 2500)
    goal = db.calorie_goal_get(1, 100)
    assert goal is not None
    assert goal["daily_target_kcal"] == 2500
    assert goal["username"] == "alice"
    # Upsert replaces the target.
    db.calorie_goal_set(1, 100, "alice", 2200)
    assert db.calorie_goal_get(1, 100)["daily_target_kcal"] == 2200


def test_calorie_goal_remove_keeps_entries(db):
    db.calorie_goal_set(1, 100, "alice", 2500)
    db.calorie_add(1, 100, "alice", 650)
    assert db.calorie_goal_remove(1, 100) is True
    assert db.calorie_goal_get(1, 100) is None
    # History survives the opt-out.
    total, n = db.calorie_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert n == 1 and total == 650
    # Removing again reports nothing happened.
    assert db.calorie_goal_remove(1, 100) is False


def test_calorie_tracked_users_scoped_to_guild(db):
    db.calorie_goal_set(1, 100, "alice", 2500)
    db.calorie_goal_set(1, 200, "bob", 3000)
    db.calorie_goal_set(2, 300, "carol", 1800)
    users = db.calorie_tracked_users(1)
    assert [r["username"] for r in users] == ["alice", "bob"]


def test_calorie_total_between_window(db):
    db.calorie_add(
        1, 100, "alice", 500,
        logged_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    db.calorie_add(
        1, 100, "alice", 700,
        logged_at=datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc),
    )
    db.calorie_add(
        1, 100, "alice", 999,
        logged_at=datetime(2026, 6, 2, 8, 0, tzinfo=timezone.utc),
    )
    total, n = db.calorie_total_between(
        1, 100, "2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00",
    )
    assert total == 1200 and n == 2


def test_calorie_entries_between_excludes_other_users(db):
    db.calorie_add(1, 100, "alice", 500)
    db.calorie_add(1, 200, "bob", 800)
    rows = db.calorie_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["kcal"] == 500


def test_migration_adds_message_id_to_legacy_calorie_entries(tmp_path):
    """An older DB created before the dedupe work has a calorie_entries table
    without message_id. Opening it must add the column + dedupe index without
    losing existing rows."""
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE calorie_entries (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            kcal      REAL    NOT NULL,
            note      TEXT,
            raw       TEXT,
            logged_at TEXT    NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO calorie_entries (guild_id, user_id, username, kcal, logged_at) "
        "VALUES (1, 100, 'alice', 500, '2026-06-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    d = Database(path)
    try:
        cols = {
            r["name"]
            for r in d._connection.execute("PRAGMA table_info(calorie_entries)")
        }
        assert "message_id" in cols
        # Existing row survived the migration.
        total, n = d.calorie_total_between(
            1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
        )
        assert n == 1 and total == 500
        # Dedupe index is live after migration.
        assert d.calorie_add(1, 100, "alice", 200, message_id=9) > 0
        assert d.calorie_add(1, 100, "alice", 200, message_id=9) == 0
    finally:
        d.close()


def test_calorie_add_dedupes_on_message_id(db):
    """Backfill re-scans must not double-count: a second insert for the same
    message_id is a no-op."""
    first = db.calorie_add(1, 100, "alice", 650, message_id=555)
    assert first > 0
    dup = db.calorie_add(1, 100, "alice", 650, message_id=555)
    assert dup == 0
    total, n = db.calorie_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert n == 1 and total == 650


def test_calorie_add_without_message_id_never_dedupes(db):
    """Slash-command entries (no message_id) are always distinct, even when
    identical."""
    a = db.calorie_add(1, 100, "alice", 200)
    b = db.calorie_add(1, 100, "alice", 200)
    assert a > 0 and b > 0 and a != b
    _total, n = db.calorie_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert n == 2


def test_calorie_food_set_get_update_remove(db):
    db.calorie_food_set(1, 100, "coffee", "Coffee", 5)
    row = db.calorie_food_get(1, 100, "coffee")
    assert row is not None
    assert row["display"] == "Coffee"
    assert row["kcal"] == 5
    # Upsert updates kcal + display, keeps the same key.
    db.calorie_food_set(1, 100, "coffee", "coffee", 8)
    assert db.calorie_food_get(1, 100, "coffee")["kcal"] == 8
    # Remove.
    assert db.calorie_food_remove(1, 100, "coffee") is True
    assert db.calorie_food_get(1, 100, "coffee") is None
    assert db.calorie_food_remove(1, 100, "coffee") is False


def test_calorie_food_scoped_per_user_and_guild(db):
    db.calorie_food_set(1, 100, "coffee", "Coffee", 5)
    db.calorie_food_set(1, 200, "coffee", "Coffee", 9)
    db.calorie_food_set(2, 100, "coffee", "Coffee", 1)
    assert db.calorie_food_get(1, 100, "coffee")["kcal"] == 5
    assert db.calorie_food_get(1, 200, "coffee")["kcal"] == 9
    assert db.calorie_food_get(2, 100, "coffee")["kcal"] == 1
    # food_list is scoped to one (guild, user).
    db.calorie_food_set(1, 100, "protein shake", "Protein Shake", 250)
    names = [r["display"] for r in db.calorie_food_list(1, 100)]
    assert names == ["Coffee", "Protein Shake"]  # ordered by display


def test_calorie_pop_last_removes_newest(db):
    db.calorie_add(
        1, 100, "alice", 500, note="breakfast",
        logged_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    db.calorie_add(
        1, 100, "alice", 700, note="dinner",
        logged_at=datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc),
    )
    popped = db.calorie_pop_last(1, 100)
    assert popped is not None
    assert popped["kcal"] == 700 and popped["note"] == "dinner"
    remaining = db.calorie_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert [r["kcal"] for r in remaining] == [500]
    # Empty case.
    db.calorie_pop_last(1, 100)
    assert db.calorie_pop_last(1, 100) is None
