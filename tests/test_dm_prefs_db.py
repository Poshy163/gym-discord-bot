"""Tests for the DM-context data layer: the per-user default-guild preference,
the present-member check behind the cross-server privacy guard, and the
guild-membership lookup used to auto-pick a server in DMs.
"""

from __future__ import annotations

import pytest

from app.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_dm_guild_get_defaults_to_none(db):
    assert db.dm_guild_get(100) is None


def test_dm_guild_set_and_get_round_trip(db):
    db.dm_guild_set(100, 42)
    assert db.dm_guild_get(100) == 42


def test_dm_guild_set_overwrites_previous(db):
    db.dm_guild_set(100, 42)
    db.dm_guild_set(100, 99)
    assert db.dm_guild_get(100) == 99


def test_dm_guild_set_none_clears(db):
    db.dm_guild_set(100, 42)
    db.dm_guild_set(100, None)
    assert db.dm_guild_get(100) is None


def test_member_present_true_only_for_present_members(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.upsert_member(1, 101, "bob", "Bob", present=False)
    assert db.member_present(1, 100) is True
    assert db.member_present(1, 101) is False


def test_member_present_false_for_unknown_or_other_guild(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    assert db.member_present(1, 999) is False  # never seen
    assert db.member_present(2, 100) is False  # different guild


def test_member_guild_ids_lists_only_present_memberships(db):
    db.upsert_member(1, 100, "alice", "Alice", present=True)
    db.upsert_member(2, 100, "alice", "Alice", present=True)
    db.upsert_member(3, 100, "alice", "Alice", present=False)
    assert db.member_guild_ids(100) == [1, 2]


def test_member_guild_ids_empty_when_unknown(db):
    assert db.member_guild_ids(100) == []
