"""Tests for protein parsing/formatting helpers and DB methods."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import protein
from app.db import Database


# ---------------------------------------------------------------------------
# Parsing / formatting
# ---------------------------------------------------------------------------

def test_parse_protein_amount():
    assert protein.parse_protein_amount("180") == 180.0
    assert protein.parse_protein_amount("180g") == 180.0
    assert protein.parse_protein_amount("40 g protein") == 40.0
    assert protein.parse_protein_amount("12.5") == 12.5
    assert protein.parse_protein_amount("abc") is None
    assert protein.parse_protein_amount("") is None
    # Multiplier prefix: 70g of a 43 g/100g food.
    assert protein.parse_protein_amount("0.7x43") == pytest.approx(30.1)
    assert protein.parse_protein_amount("0.7 x 43g") == pytest.approx(30.1)


def test_parse_protein_chat_message_accepts_marked_amounts():
    assert protein.parse_protein_chat_message("40p") == 40.0
    assert protein.parse_protein_chat_message("40 p") == 40.0
    assert protein.parse_protein_chat_message("40g protein") == 40.0
    assert protein.parse_protein_chat_message("40 protein") == 40.0
    assert protein.parse_protein_chat_message("protein 40") == 40.0
    assert protein.parse_protein_chat_message("protein 40g") == 40.0
    assert protein.parse_protein_chat_message("40p!") == 40.0


def test_parse_protein_chat_message_multiplier():
    # Per-100g label maths: 70g of a 43 g/100g food.
    assert protein.parse_protein_chat_message("0.7x43p") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("0.7 x 43 p") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("0.7*43g protein") == pytest.approx(30.1)
    assert protein.parse_protein_chat_message("2x20p") == 40.0
    assert protein.parse_protein_chat_message("protein 0.7x43") == pytest.approx(30.1)
    # Still needs the protein marker — a multiplied bare number is not a log.
    assert protein.parse_protein_chat_message("0.7x43") is None
    assert protein.parse_protein_chat_message("0.7x43g") is None


def test_parse_protein_chat_message_rejects_ambiguous():
    # A bare number or weight must NOT be read as protein.
    assert protein.parse_protein_chat_message("40") is None
    assert protein.parse_protein_chat_message("40g") is None
    assert protein.parse_protein_chat_message("bench 40kg") is None
    assert protein.parse_protein_chat_message("had 40p of protein today") is None
    assert protein.parse_protein_chat_message("") is None


def test_format_grams():
    assert protein.format_grams(40) == "40 g"
    assert protein.format_grams(39.6) == "40 g"
    assert protein.format_grams(0) == "0 g"


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_protein_update_last_edits_most_recent(db):
    db.protein_add(1, 100, "Alice", 40)
    db.protein_add(1, 100, "Alice", 25)  # most recent
    old = db.protein_update_last(1, 100, 55, username="Alice")
    assert old["grams"] == 25
    rows = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert sorted(r["grams"] for r in rows) == [40, 55]
    assert db.protein_update_last(1, 999, 30) is None


def test_protein_goal_set_get_remove(db):
    assert db.protein_goal_get(1, 100) is None
    db.protein_goal_set(1, 100, "alice", 180)
    row = db.protein_goal_get(1, 100)
    assert row["daily_target_g"] == 180.0
    assert row["username"] == "alice"
    # Update is upsert.
    db.protein_goal_set(1, 100, "alice", 200)
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 200.0
    # tracked_users matches goals against current guild membership.
    db.upsert_member(1, 100, "alice", "Alice")
    assert {r["user_id"] for r in db.protein_tracked_users(1)} == {100}
    assert db.protein_goal_remove(1, 100) is True
    assert db.protein_goal_get(1, 100) is None
    assert db.protein_goal_remove(1, 100) is False


def test_protein_goal_and_total_are_global_per_user(db):
    db.protein_goal_set(1, 100, "alice", 180)
    # Goal resolves from any server / DM.
    assert db.protein_goal_get(2, 100)["daily_target_g"] == 180.0
    assert db.protein_goal_get(999, 100)["daily_target_g"] == 180.0
    # Re-setting elsewhere consolidates to one row.
    db.protein_goal_set(2, 100, "alice", 200)
    assert db.protein_goal_get(1, 100)["daily_target_g"] == 200.0
    # Totals aggregate across servers.
    db.protein_add(1, 100, "alice", 40, message_id=1)
    db.protein_add(2, 100, "alice", 30, message_id=2)
    total, n = db.protein_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert total == 70.0 and n == 2
    # Removing stops tracking everywhere.
    assert db.protein_goal_remove(2, 100) is True
    assert db.protein_goal_get(1, 100) is None


def test_protein_add_total_and_dedupe(db):
    db.protein_add(1, 100, "alice", 40, message_id=10)
    db.protein_add(1, 100, "alice", 30, message_id=11)
    # Duplicate message_id is ignored.
    assert db.protein_add(1, 100, "alice", 99, message_id=10) == 0
    total, n = db.protein_total_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert total == 70.0 and n == 2


def test_protein_pop_last(db):
    db.protein_add(
        1, 100, "alice", 40, note="chicken",
        logged_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    )
    db.protein_add(
        1, 100, "alice", 30, note="shake",
        logged_at=datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc),
    )
    popped = db.protein_pop_last(1, 100)
    assert popped["grams"] == 30 and popped["note"] == "shake"
    remaining = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert [r["grams"] for r in remaining] == [40]
    db.protein_pop_last(1, 100)
    assert db.protein_pop_last(1, 100) is None


def test_protein_entries_between_scoped_to_user(db):
    db.protein_add(1, 100, "alice", 40)
    db.protein_add(1, 200, "bob", 50)
    rows = db.protein_entries_between(
        1, 100, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00",
    )
    assert [r["grams"] for r in rows] == [40]


def test_get_and_delete_protein_entry_by_message(db):
    # Backs ❌ reaction-undo on protein/combined replies.
    eid = db.protein_add(1, 100, "alice", 40, message_id=777)
    row = db.get_protein_entry_by_message(1, 777)
    assert row is not None and row["id"] == eid and row["user_id"] == 100
    assert db.get_protein_entry_by_message(1, 999) is None
    # Wrong user can't delete; correct (guild, user) removes it.
    assert db.delete_protein_entry(1, 999, eid) is None
    removed = db.delete_protein_entry(1, 100, eid)
    assert removed is not None and removed["grams"] == 40
    assert db.delete_protein_entry(1, 100, eid) is None
