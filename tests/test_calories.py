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
    "", "lunch", "-300", "650 kg", "kj", "650x", "six hundred", "0.7x",
    "0.7x kj",
])
def test_parse_energy_rejects_garbage(text):
    assert parse_energy(text) is None


# ---- multiplier prefix (per-100g label maths) -------------------------------

@pytest.mark.parametrize("text,expected_kcal,expected_unit", [
    # 70g of a 1640 kJ/100g food.
    ("0.7x1640kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.7 x 1640 kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.7*1640kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.7×1640kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.6x430c", 258.0, "kcal"),
    (".5x400 cal", 200.0, "kcal"),
    ("2x600c", 1200.0, "kcal"),
    ("0.5x2000", 1000.0, "kcal"),  # bare number still defaults to kcal
])
def test_parse_energy_multiplier(text, expected_kcal, expected_unit):
    result = parse_energy(text)
    assert result is not None
    kcal, unit = result
    assert kcal == pytest.approx(expected_kcal)
    assert unit == expected_unit


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
    # Multiplier prefix: 70g of a 1640 kJ/100g food, 60g of a 430 cal/100g one.
    ("0.7x1640kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.7 x 1640 kj", 0.7 * 1640.0 / 4.184, "kj"),
    ("0.6x430c", 258.0, "kcal"),
    ("2x600c", 1200.0, "kcal"),
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
    "0.7x1640",        # multiplier on a bare number — still no unit, no log
    "0.7x1640kj lunch",  # multiplier amounts inside a sentence are ignored too
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


def test_calorie_update_last_edits_most_recent(db):
    db.calorie_add(1, 100, "Alice", 500)
    db.calorie_add(1, 100, "Alice", 300)  # most recent
    old = db.calorie_update_last(1, 100, 250, username="Alice")
    assert old["kcal"] == 300
    rows = db.calorie_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert sorted(r["kcal"] for r in rows) == [250, 500]
    # Nothing to edit for a user with no entries.
    assert db.calorie_update_last(1, 999, 100) is None


def test_nutrition_home_guild_prefers_goal_guild(db):
    assert db.nutrition_home_guild(100) is None
    db.calorie_goal_set(2, 100, "Alice", 2000)
    assert db.nutrition_home_guild(100) == 2
    # Protein-only user resolves via the protein goal.
    db.protein_goal_set(3, 200, "Bob", 180)
    assert db.nutrition_home_guild(200) == 3


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


def test_calorie_tracked_users_scoped_to_membership(db):
    # Tracking is global, but a guild's report only lists that guild's current
    # members — matched against the members mirror, not the goal's stored guild.
    db.calorie_goal_set(1, 100, "alice", 2500)
    db.calorie_goal_set(1, 200, "bob", 3000)
    db.calorie_goal_set(2, 300, "carol", 1800)
    db.upsert_member(1, 100, "alice", "Alice")
    db.upsert_member(1, 200, "bob", "Bob")
    db.upsert_member(2, 300, "carol", "Carol")
    assert [r["username"] for r in db.calorie_tracked_users(1)] == ["alice", "bob"]
    assert [r["username"] for r in db.calorie_tracked_users(2)] == ["carol"]


def test_calorie_tracked_users_follows_member_across_servers(db):
    # A user who set their goal in server 2 but is a member of server 1 still
    # appears in server 1's report (and not where they aren't a member).
    db.calorie_goal_set(2, 100, "alice", 2500)
    db.upsert_member(1, 100, "alice", "Alice")
    assert [r["user_id"] for r in db.calorie_tracked_users(1)] == [100]
    assert db.calorie_tracked_users(2) == []
    # Leaving the server drops them from its report; history/goal remain.
    db.upsert_member(1, 100, "alice", "Alice", present=False)
    assert db.calorie_tracked_users(1) == []
    assert db.calorie_goal_get(1, 100)["daily_target_kcal"] == 2500


def test_global_goal_consolidation_migration(db, tmp_path):
    # Simulate an older DB with a separate goal row per server for one user,
    # then re-open it so the startup migration consolidates to the latest.
    db.calorie_goal_set(1, 100, "alice", 2500)
    with db._conn() as c:  # noqa: SLF001 - exercising the migration directly
        c.execute(
            "INSERT INTO calorie_goals "
            "(guild_id, user_id, username, daily_target_kcal, set_at) "
            "VALUES (2, 100, 'alice', 1800, '2000-01-01T00:00:00+00:00')"
        )
        c.execute(
            "INSERT INTO protein_goals "
            "(guild_id, user_id, username, daily_target_g, set_at) "
            "VALUES (2, 100, 'alice', 99, '2000-01-01T00:00:00+00:00')"
        )
        c.execute(
            "INSERT INTO protein_goals "
            "(guild_id, user_id, username, daily_target_g, set_at) "
            "VALUES (1, 100, 'alice', 180, '2099-01-01T00:00:00+00:00')"
        )
    db.close()
    reopened = Database(tmp_path / "gym.sqlite3")
    try:
        with reopened._conn() as c:  # noqa: SLF001
            cal = c.execute(
                "SELECT daily_target_kcal FROM calorie_goals WHERE user_id = 100"
            ).fetchall()
            pro = c.execute(
                "SELECT daily_target_g FROM protein_goals WHERE user_id = 100"
            ).fetchall()
        # One row each, keeping the most-recently-set target.
        assert [r["daily_target_kcal"] for r in cal] == [2500]
        assert [r["daily_target_g"] for r in pro] == [180]
    finally:
        reopened.close()


def test_calorie_goal_is_global_per_user(db):
    db.calorie_goal_set(1, 100, "alice", 2500)
    # The goal resolves from any server (and DMs, which resolve to a server).
    assert db.calorie_goal_get(1, 100)["daily_target_kcal"] == 2500
    assert db.calorie_goal_get(2, 100)["daily_target_kcal"] == 2500
    assert db.calorie_goal_get(999, 100)["daily_target_kcal"] == 2500
    # Re-setting from another server consolidates to one row (no per-guild drift).
    db.calorie_goal_set(2, 100, "alice", 2200)
    assert db.calorie_goal_get(1, 100)["daily_target_kcal"] == 2200
    # A different user is unaffected.
    db.calorie_goal_set(1, 200, "bob", 3000)
    assert db.calorie_goal_get(5, 200)["daily_target_kcal"] == 3000
    # Removing stops tracking everywhere.
    assert db.calorie_goal_remove(2, 100) is True
    assert db.calorie_goal_get(1, 100) is None


def test_calorie_total_aggregates_across_servers(db):
    base = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    db.calorie_add(1, 100, "alice", 500, logged_at=base)
    db.calorie_add(2, 100, "alice", 700, logged_at=base)  # other server, same day
    total, n = db.calorie_total_between(
        1, 100, "2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00",
    )
    assert total == 1200 and n == 2


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


def test_calorie_food_protein_optional_and_preserved(db):
    # Save with protein.
    db.calorie_food_set(1, 100, "shake", "Protein Shake", 250, 30)
    row = db.calorie_food_get(1, 100, "shake")
    assert row["kcal"] == 250 and row["protein_g"] == 30
    # Re-save with only a new calorie amount (protein omitted) → protein kept.
    db.calorie_food_set(1, 100, "shake", "Protein Shake", 260)
    row = db.calorie_food_get(1, 100, "shake")
    assert row["kcal"] == 260 and row["protein_g"] == 30
    # Explicit 0 clears it; a brand-new food has NULL protein.
    db.calorie_food_set(1, 100, "shake", "Protein Shake", 260, 0)
    assert db.calorie_food_get(1, 100, "shake")["protein_g"] == 0
    db.calorie_food_set(1, 100, "coffee", "Coffee", 5)
    assert db.calorie_food_get(1, 100, "coffee")["protein_g"] is None


def test_calorie_food_is_per_user_global_across_guilds(db):
    # Saved foods are shared across servers for a given user, but isolated
    # between users.
    db.calorie_food_set(1, 100, "coffee", "Coffee", 5)
    db.calorie_food_set(1, 200, "coffee", "Coffee", 9)   # different user
    # Same user re-sets the food from another server → it carries (one row).
    db.calorie_food_set(2, 100, "coffee", "Coffee", 1)
    # User 100 sees the same food (latest value) in *both* guilds and elsewhere.
    assert db.calorie_food_get(1, 100, "coffee")["kcal"] == 1
    assert db.calorie_food_get(2, 100, "coffee")["kcal"] == 1
    assert db.calorie_food_get(999, 100, "coffee")["kcal"] == 1  # any server
    # User 200 is unaffected.
    assert db.calorie_food_get(1, 200, "coffee")["kcal"] == 9

    # A food set in guild 1 is listed from guild 2 too (global per user).
    db.calorie_food_set(1, 100, "protein shake", "Protein Shake", 250)
    names = [r["display"] for r in db.calorie_food_list(2, 100)]
    assert names == ["Coffee", "Protein Shake"]  # deduped, ordered by display

    # Removing from any server removes it everywhere.
    assert db.calorie_food_remove(2, 100, "coffee") is True
    assert db.calorie_food_get(1, 100, "coffee") is None


def test_calorie_food_preserves_protein_across_guilds(db):
    db.calorie_food_set(1, 100, "shake", "Shake", 250, 30)
    # Re-saving from another server with only kcal keeps the protein value.
    db.calorie_food_set(2, 100, "shake", "Shake", 260)
    row = db.calorie_food_get(1, 100, "shake")
    assert row["kcal"] == 260 and row["protein_g"] == 30


def test_calorie_reply_tracking_roundtrip(db):
    eid = db.calorie_add(1, 100, "alice", 1730, note="oops", message_id=555)
    db.track_calorie_reply(
        reply_message_id=999, guild_id=1, user_id=100, target_user_id=100,
        calorie_id=eid, original_message_id=555,
    )
    rec = db.get_calorie_reply(999)
    assert rec is not None
    assert rec["calorie_id"] == eid
    assert rec["original_message_id"] == 555
    # First delete claims it (race protection); second is a no-op.
    assert db.delete_calorie_reply(999) == 1
    assert db.delete_calorie_reply(999) == 0
    assert db.get_calorie_reply(999) is None


def test_update_calorie_entry(db):
    # Mirrors a `1730c` → `1730kj` correction: 1730 kcal becomes ~413.
    eid = db.calorie_add(1, 100, "alice", 1730, note="oops", message_id=42)
    db.update_calorie_entry(eid, 413.0, note=None, raw="1730kj")
    row = db.get_calorie_entry_by_message(1, 42)
    assert row["kcal"] == 413.0
    # Note cleared too.
    full = db.calorie_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert full[0]["note"] is None


def test_get_calorie_reply_by_original(db):
    eid = db.calorie_add(1, 100, "alice", 500, message_id=42)
    db.track_calorie_reply(
        reply_message_id=900, guild_id=1, user_id=100, target_user_id=100,
        calorie_id=eid, original_message_id=42,
    )
    rec = db.get_calorie_reply_by_original(42)
    assert rec is not None and rec["reply_message_id"] == 900
    assert db.get_calorie_reply_by_original(999) is None


def test_get_calorie_entry_by_message(db):
    # Legacy ❌-undo resolves the entry from the source message id.
    eid = db.calorie_add(1, 100, "alice", 1730, note="oops", message_id=777)
    row = db.get_calorie_entry_by_message(1, 777)
    assert row is not None and row["id"] == eid and row["user_id"] == 100
    assert db.get_calorie_entry_by_message(1, 999) is None
    # Slash-command entries (no message_id) aren't resolvable this way.
    db.calorie_add(1, 100, "alice", 200)
    assert db.get_calorie_entry_by_message(1, 0) is None


def test_delete_calorie_entry_scoped(db):
    eid = db.calorie_add(1, 100, "alice", 1730, note="oops")
    # Wrong user can't delete it.
    assert db.delete_calorie_entry(1, 999, eid) is None
    # Correct (guild, user) removes it and returns the row.
    removed = db.delete_calorie_entry(1, 100, eid)
    assert removed is not None and removed["kcal"] == 1730
    # Already gone.
    assert db.delete_calorie_entry(1, 100, eid) is None
    remaining = db.calorie_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert remaining == []


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


def test_calorie_logged_days_distinct(db):
    db.calorie_add(1, 100, "Sam", 500, logged_at=datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    db.calorie_add(1, 100, "Sam", 300, logged_at=datetime(2026, 6, 1, 20, tzinfo=timezone.utc))
    db.calorie_add(1, 100, "Sam", 400, logged_at=datetime(2026, 6, 3, 9, tzinfo=timezone.utc))
    days = db.calorie_logged_days(1, 100, "2026-06-01", "2026-06-10")
    assert sorted(days) == ["2026-06-01", "2026-06-03"]
    # Out-of-window entries don't count.
    assert db.calorie_logged_days(1, 100, "2026-06-02", "2026-06-03") == []


# ---- parse_meal_items -------------------------------------------------------

def test_parse_meal_items_basic():
    from app.calories import parse_meal_items
    assert parse_meal_items("coffee, 2x oats, protein shake") == [
        (1, "coffee"), (2, "oats"), (1, "protein shake"),
    ]
    assert parse_meal_items("coffee + oats") == [(1, "coffee"), (1, "oats")]
    assert parse_meal_items("Coffee") == [(1, "coffee")]


def test_parse_meal_items_merges_duplicates():
    from app.calories import parse_meal_items
    assert parse_meal_items("coffee, coffee") == [(2, "coffee")]
    assert parse_meal_items("2 coffee, coffee x3") == [(5, "coffee")]


def test_parse_meal_items_rejects_bad_input():
    from app.calories import parse_meal_items
    assert parse_meal_items("") is None
    assert parse_meal_items("a, b\nc") is None
    assert parse_meal_items(", ,") is None
    # Over the 12-item cap.
    too_many = ", ".join(f"food{i}" for i in range(13))
    assert parse_meal_items(too_many) is None


# ---- saved meals + reminder prefs (DB) --------------------------------------

def test_calorie_meal_set_get_roundtrip(db):
    db.calorie_meal_set(100, "breakfast", "Breakfast", [(1, "coffee"), (2, "oats")])
    got = db.calorie_meal_get(100, "breakfast")
    assert got is not None
    display, items = got
    assert display == "Breakfast"
    assert items == [(1, "coffee"), (2, "oats")]
    # Upsert replaces items.
    db.calorie_meal_set(100, "breakfast", "breakfast", [(1, "coffee")])
    assert db.calorie_meal_get(100, "breakfast") == ("breakfast", [(1, "coffee")])
    # Scoped per user.
    assert db.calorie_meal_get(999, "breakfast") is None


def test_calorie_meal_list_and_remove(db):
    db.calorie_meal_set(100, "breakfast", "Breakfast", [(1, "coffee")])
    db.calorie_meal_set(100, "arvo snack", "Arvo Snack", [(1, "shake")])
    names = [r["name"] for r in db.calorie_meal_list(100)]
    assert names == ["arvo snack", "breakfast"]  # alphabetical by display
    assert db.calorie_meal_remove(100, "breakfast") is True
    assert db.calorie_meal_remove(100, "breakfast") is False
    assert [r["name"] for r in db.calorie_meal_list(100)] == ["arvo snack"]


def test_calorie_reminder_prefs_roundtrip(db):
    assert db.calorie_reminder_get(100) is None
    db.calorie_reminder_set(100, 20, 30)
    row = db.calorie_reminder_get(100)
    assert (row["hour"], row["minute"], row["last_sent"]) == (20, 30, None)
    db.calorie_reminder_mark_sent(100, "2026-07-02")
    assert db.calorie_reminder_get(100)["last_sent"] == "2026-07-02"
    # Re-setting the time re-arms (clears last_sent).
    db.calorie_reminder_set(100, 21, 0)
    assert db.calorie_reminder_get(100)["last_sent"] is None
    assert len(db.calorie_reminder_list()) == 1
    assert db.calorie_reminder_remove(100) is True
    assert db.calorie_reminder_remove(100) is False
    assert db.calorie_reminder_list() == []
