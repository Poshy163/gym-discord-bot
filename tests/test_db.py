"""Database integration tests.

These exercise the small but easy-to-break behaviours where SQL gets subtle:
per-user vs guild-wide rename scoping, rename collisions vs the dedupe
index, ``pop_last_n_for_user`` order, and the race-claim semantics of
``delete_reply`` (must return rowcount so concurrent callers can't both
think they "won" the deletion).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.db import Database


@dataclass
class _Lift:
    """Stand-in for app.parser.Lift — only the fields ``add_lifts`` reads."""
    equipment: str
    weight_kg: float
    bodyweight_add: bool = False
    raw: str = ""
    reps: int | None = None


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def _add(db, guild, user, eq, w, *, msg_id=None):
    """Compact helper for seeding a single lift row."""
    return db.add_lifts(
        guild_id=guild, user_id=user, username=f"u{user}",
        lifts=[_Lift(eq, w)], message_id=msg_id,
    )


def test_rename_scoped_to_one_user(db):
    """A user-scoped rename must not touch other people's rows."""
    _add(db, 1, 100, "leg press", 80, msg_id=1)
    _add(db, 1, 200, "leg press", 90, msg_id=2)
    n = db.rename_equipment(1, "leg press", "angled leg press", user_id=100)
    assert n == 1
    # User 100's row was renamed; user 200's was left alone.
    assert db.count_equipment_rows(1, "angled leg press", user_id=100) == 1
    assert db.count_equipment_rows(1, "leg press", user_id=200) == 1


def test_rename_guild_wide_renames_all_users(db):
    _add(db, 1, 100, "leg press", 80, msg_id=1)
    _add(db, 1, 200, "leg press", 90, msg_id=2)
    n = db.rename_equipment(1, "leg press", "angled leg press")
    assert n == 2
    assert db.count_equipment_rows(1, "leg press") == 0
    assert db.count_equipment_rows(1, "angled leg press") == 2


def test_rename_handles_dedupe_collision(db):
    """If src and dst already coexist on the same message_id (which the
    unique index forbids), the source row must be dropped rather than
    crashing the rename. Otherwise a single legacy row would block users
    from cleaning up their history."""
    _add(db, 1, 100, "leg press", 80, msg_id=42)
    _add(db, 1, 100, "angled leg press", 80, msg_id=42)
    # The colliding "leg press" row gets dropped before the UPDATE runs,
    # so only the pre-existing "angled leg press" row remains.
    db.rename_equipment(1, "leg press", "angled leg press", user_id=100)
    assert db.count_equipment_rows(1, "leg press", user_id=100) == 0
    assert db.count_equipment_rows(1, "angled leg press", user_id=100) == 1


def test_rename_guild_wide_repoints_custom_aliases(db):
    """When a guild-wide rename happens, any custom alias pointing at the
    old canonical must be updated, otherwise future parses still write to
    the old name."""
    db.alias_set(1, "lp", "leg press", added_by=999)
    _add(db, 1, 100, "leg press", 80, msg_id=1)
    db.rename_equipment(1, "leg press", "angled leg press")
    aliases = {r["alias_normalized"]: r["canonical"] for r in db.alias_list(1)}
    assert aliases["lp"] == "angled leg press"


def test_pop_last_n_for_user_returns_newest_first(db):
    """``/undo count:N`` reads the returned rows for its receipt message,
    so the order matters — we want newest first."""
    _add(db, 1, 100, "bench", 60, msg_id=1)
    _add(db, 1, 100, "bench", 70, msg_id=2)
    _add(db, 1, 100, "bench", 80, msg_id=3)
    rows = db.pop_last_n_for_user(1, 100, 2)
    assert [r["weight_kg"] for r in rows] == [80, 70]
    # Remaining row count drops accordingly.
    assert db.count_equipment_rows(1, "bench", user_id=100) == 1


def test_pop_last_n_clamps_to_available(db):
    _add(db, 1, 100, "bench", 60, msg_id=1)
    rows = db.pop_last_n_for_user(1, 100, 10)
    assert len(rows) == 1


def test_delete_reply_rowcount_is_race_safe(db):
    """The reaction-undo handler relies on ``delete_reply`` returning a
    rowcount so two concurrent reactions can't both "win". First call
    must return 1, second must return 0."""
    db.track_reply(
        reply_message_id=20, guild_id=1, user_id=100,
        message_id=10, lift_ids=[],
    )
    assert db.delete_reply(20) == 1
    assert db.delete_reply(20) == 0
