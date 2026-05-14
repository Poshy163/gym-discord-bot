"""Database integration tests.

These exercise the small but easy-to-break behaviours where SQL gets subtle:
per-user vs guild-wide rename scoping, rename collisions vs the dedupe
index, ``pop_last_n_for_user`` order, and the race-claim semantics of
``delete_reply`` (must return rowcount so concurrent callers can't both
think they "won" the deletion).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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


def _add(
    db, guild, user, eq, w, *, msg_id=None, logged_at=None, bw=False, reps=None,
):
    """Compact helper for seeding a single lift row."""
    return db.add_lifts(
        guild_id=guild, user_id=user, username=f"u{user}",
        lifts=[_Lift(eq, w, bodyweight_add=bw, reps=reps)], message_id=msg_id,
        logged_at=logged_at,
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


def test_rename_repoints_user_scoped_goal(db):
    db.goal_set(1, 100, "leg press", 120, False)
    _add(db, 1, 100, "leg press", 80, msg_id=1)
    db.rename_equipment(1, "leg press", "angled leg press", user_id=100)
    assert db.goal_get(1, 100, "leg press") is None
    goal = db.goal_get(1, 100, "angled leg press")
    assert goal is not None
    assert goal["target_kg"] == 120


def test_rename_merges_goal_collisions_using_higher_target(db):
    db.goal_set(1, 100, "leg press", 120, False)
    db.goal_set(1, 100, "angled leg press", 150, False)
    _add(db, 1, 100, "leg press", 80, msg_id=1)
    db.rename_equipment(1, "leg press", "angled leg press")
    assert db.goal_get(1, 100, "leg press") is None
    goal = db.goal_get(1, 100, "angled leg press")
    assert goal is not None
    assert goal["target_kg"] == 150


def test_progress_first_seen_is_date_best_was_first_reached(db):
    _add(
        db, 1, 100, "bench", 60, msg_id=1,
        logged_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 80, msg_id=2,
        logged_at=datetime(2026, 4, 10, tzinfo=timezone.utc), bw=True,
    )
    _add(
        db, 1, 100, "bench", 80, msg_id=3,
        logged_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    rows = db.progress(1, 100, "bench")
    assert len(rows) == 1
    assert rows[0]["best"] == 80
    assert rows[0]["first_seen"].startswith("2026-04-10")
    assert rows[0]["bw"] == 1


def test_daily_activity_counts_popular_lifts_and_prs(db):
    _add(
        db, 1, 100, "bench", 60, msg_id=1,
        logged_at=datetime(2026, 4, 24, 23, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 80, msg_id=2,
        logged_at=datetime(2026, 4, 25, 9, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 200, "squat", 100, msg_id=3,
        logged_at=datetime(2026, 4, 25, 10, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 70, msg_id=4,
        logged_at=datetime(2026, 4, 26, 1, tzinfo=timezone.utc),
    )
    summary = db.daily_activity(
        1,
        "2026-04-25T00:00:00+00:00",
        "2026-04-26T00:00:00+00:00",
    )
    assert summary["totals"]["total_lifts"] == 2
    assert summary["totals"]["lifters"] == 2
    assert summary["popular_equipment"][0]["equipment"] == "bench"
    prs = {(row["username"], row["equipment"]) for row in summary["prs"]}
    assert prs == {("u100", "bench"), ("u200", "squat")}


def test_daily_activity_reports_best_same_day_pr_per_lift(db):
    _add(
        db, 1, 100, "squat", 60, msg_id=1,
        logged_at=datetime(2026, 4, 25, 12, 40, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "squat", 70, msg_id=2,
        logged_at=datetime(2026, 4, 25, 12, 42, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "leg press", 235, msg_id=3,
        logged_at=datetime(2026, 4, 24, 12, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "leg press", 275, msg_id=4,
        logged_at=datetime(2026, 4, 25, 13, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "leg press", 290, msg_id=5,
        logged_at=datetime(2026, 4, 25, 13, 5, tzinfo=timezone.utc),
    )

    summary = db.daily_activity(
        1,
        "2026-04-25T00:00:00+00:00",
        "2026-04-26T00:00:00+00:00",
        limit=10,
    )

    prs = {row["equipment"]: row for row in summary["prs"]}
    assert prs["squat"]["weight_kg"] == 70
    assert prs["squat"]["prev_best"] == 60
    assert prs["leg press"]["weight_kg"] == 290
    assert prs["leg press"]["prev_best"] == 275


def test_daily_activity_uses_lift_time_not_insert_order_for_prs(db):
    _add(
        db, 1, 100, "bench", 110, msg_id=1,
        logged_at=datetime(2026, 4, 26, 9, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 100, msg_id=2,
        logged_at=datetime(2026, 4, 25, 9, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 90, msg_id=3,
        logged_at=datetime(2026, 4, 24, 9, tzinfo=timezone.utc),
    )

    summary = db.daily_activity(
        1,
        "2026-04-25T00:00:00+00:00",
        "2026-04-26T00:00:00+00:00",
    )

    assert len(summary["prs"]) == 1
    assert summary["prs"][0]["equipment"] == "bench"
    assert summary["prs"][0]["weight_kg"] == 100
    assert summary["prs"][0]["prev_best"] == 90


def test_delete_entry_between_uses_timestamp_range(db):
    _add(
        db, 1, 100, "bench", 60, msg_id=1,
        logged_at=datetime(2026, 4, 24, 13, 0, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 70, msg_id=2,
        logged_at=datetime(2026, 4, 24, 14, 0, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 80, msg_id=3,
        logged_at=datetime(2026, 4, 25, 14, 0, tzinfo=timezone.utc),
    )
    deleted = db.delete_entry_between(
        1,
        "bench",
        "2026-04-24T13:30:00+00:00",
        "2026-04-25T13:30:00+00:00",
        user_id=100,
    )
    assert deleted == 1
    assert db.count_equipment_rows(1, "bench", user_id=100) == 2


def test_update_latest_lift_weight_can_target_another_user(db):
    _add(db, 1, 100, "bench", 60, msg_id=1)
    _add(db, 1, 200, "bench", 70, msg_id=2)
    previous = db.update_latest_lift_weight(1, 200, "bench", 90, False)
    assert previous is not None
    assert previous["weight_kg"] == 70
    assert db.progress(1, 100, "bench")[0]["best"] == 60
    assert db.progress(1, 200, "bench")[0]["best"] == 90


def test_update_latest_lift_weight_respects_date_window(db):
    _add(
        db, 1, 100, "bench", 60, msg_id=1,
        logged_at=datetime(2026, 4, 24, 13, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 70, msg_id=2,
        logged_at=datetime(2026, 4, 25, 13, tzinfo=timezone.utc),
    )
    previous = db.update_latest_lift_weight(
        1,
        100,
        "bench",
        65,
        False,
        "2026-04-24T00:00:00+00:00",
        "2026-04-25T00:00:00+00:00",
    )
    assert previous is not None
    assert previous["weight_kg"] == 60
    rows = db.history(1, 100, "bench")
    assert [r["weight_kg"] for r in rows] == [65, 70]


def test_swap_latest_lift_weights_between_two_entries(db):
    _add(db, 1, 100, "leg curl", 45, msg_id=1, reps=8)
    _add(db, 1, 100, "leg extension", 80, msg_id=2, bw=True, reps=10)
    swapped = db.swap_latest_lift_weights(1, 100, "leg curl", "leg extension")
    assert swapped is not None
    rows = {
        row["equipment"]: row
        for row in db.user_latest_by_equipment(1, 100)
    }
    assert rows["leg curl"]["weight_kg"] == 80
    assert rows["leg curl"]["bw"] == 1
    assert rows["leg extension"]["weight_kg"] == 45
    assert rows["leg extension"]["bw"] == 0


def test_user_latest_by_equipment_returns_latest_rows(db):
    _add(
        db, 1, 100, "bench", 60, msg_id=1,
        logged_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 80, msg_id=2,
        logged_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "squat", 100, msg_id=3,
        logged_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    rows = {row["equipment"]: row for row in db.user_latest_by_equipment(1, 100)}
    assert rows["bench"]["weight_kg"] == 80
    assert rows["bench"]["n"] == 2
    assert rows["squat"]["weight_kg"] == 100


def test_user_all_lifts_returns_every_row_chronologically(db):
    """``/export_lifts`` relies on this for the CSV — so we need every row
    for the user (no ``LIMIT``), in time order, and scoped to the right
    user/guild."""
    _add(
        db, 1, 100, "bench", 80, msg_id=1,
        logged_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "bench", 60, msg_id=2,
        logged_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    _add(
        db, 1, 100, "squat", 100, msg_id=3,
        logged_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    # Other user / other guild rows must be ignored.
    _add(db, 1, 200, "bench", 70, msg_id=4)
    _add(db, 2, 100, "bench", 70, msg_id=5)

    rows = db.user_all_lifts(1, 100)
    assert [r["weight_kg"] for r in rows] == [60, 100, 80]
    assert [r["equipment"] for r in rows] == ["bench", "squat", "bench"]


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


def test_reply_tracking_can_target_another_lifter(db):
    db.track_reply(
        reply_message_id=20, guild_id=1, user_id=100,
        message_id=10, lift_ids=[], target_user_id=200,
    )
    row = db.get_reply(20)
    assert row is not None
    assert row["user_id"] == 100
    assert row["target_user_id"] == 200


def test_retarget_replies_for_edited_message(db):
    db.track_reply(
        reply_message_id=20, guild_id=1, user_id=100,
        message_id=10, lift_ids=[], target_user_id=100,
    )
    updated = db.retarget_replies_for_message(1, 10, 200)
    assert updated == 1
    row = db.get_reply(20)
    assert row is not None
    assert row["target_user_id"] == 200


def test_delete_lifts_by_ids_can_delete_after_retarget(db):
    _add(db, 1, 100, "bench", 60, msg_id=1)
    _add(db, 1, 200, "bench", 70, msg_id=2)
    rows = db.lifts_for_message(1, 1)
    deleted = db.delete_lifts_by_ids(1, None, [int(rows[0]["id"])])
    assert deleted == 1
    assert db.count_equipment_rows(1, "bench", user_id=100) == 0
    assert db.count_equipment_rows(1, "bench", user_id=200) == 1


# --- Bodyweight tracking --------------------------------------------------

def test_bodyweight_round_trip(db):
    """Latest-write-wins is purely chronological (recorded_at, then id)."""
    db.set_bodyweight(1, 100, 95.0)
    db.set_bodyweight(1, 100, 96.5)
    row = db.get_latest_bodyweight(1, 100)
    assert row is not None
    assert row["weight_kg"] == 96.5


def test_bodyweight_missing_user_returns_none(db):
    assert db.get_latest_bodyweight(1, 999) is None


def test_bodyweight_scoped_per_guild(db):
    db.set_bodyweight(1, 100, 90.0)
    db.set_bodyweight(2, 100, 110.0)
    assert db.get_latest_bodyweight(1, 100)["weight_kg"] == 90.0
    assert db.get_latest_bodyweight(2, 100)["weight_kg"] == 110.0


def test_bodyweight_bulk_returns_only_known(db):
    db.set_bodyweight(1, 100, 90.0)
    db.set_bodyweight(1, 200, 80.0)
    out = db.latest_bodyweights_bulk(1, [100, 200, 300])
    assert out == {100: 90.0, 200: 80.0}


def test_bodyweight_bulk_empty_input(db):
    assert db.latest_bodyweights_bulk(1, []) == {}


def test_bodyweight_bulk_picks_latest_with_id_tiebreaker(db):
    """When two rows share recorded_at, the higher id (later insert) wins.

    Mirrors the ORDER BY in `get_latest_bodyweight`, so the bulk query used
    by /leaderboard never returns a stale value on tied timestamps.
    """
    same_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    db.set_bodyweight(1, 100, 90.0, recorded_at=same_ts)
    db.set_bodyweight(1, 100, 92.0, recorded_at=same_ts)  # later id wins
    out = db.latest_bodyweights_bulk(1, [100])
    assert out == {100: 92.0}
    # And it agrees with the single-user accessor.
    assert db.get_latest_bodyweight(1, 100)["weight_kg"] == 92.0


def test_bodyweight_history_returns_oldest_first_and_scoped(db):
    """`/bodyweight_history` and `/bodyweight_graph` rely on chronological
    order plus strict guild + user scoping."""
    t0 = datetime(2025, 1, 1, 7, 0, tzinfo=timezone.utc)
    t1 = datetime(2025, 1, 8, 7, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 15, 7, 0, tzinfo=timezone.utc)
    db.set_bodyweight(1, 100, 95.0, recorded_at=t1)
    db.set_bodyweight(1, 100, 94.0, recorded_at=t0)  # inserted out of order
    db.set_bodyweight(1, 100, 96.0, recorded_at=t2)
    # Noise in other guild / user — must not bleed in.
    db.set_bodyweight(2, 100, 200.0, recorded_at=t1)
    db.set_bodyweight(1, 999, 50.0, recorded_at=t1)

    rows = db.bodyweight_history(1, 100)
    assert [float(r["weight_kg"]) for r in rows] == [94.0, 95.0, 96.0]

    # Limit clamps the result.
    assert len(db.bodyweight_history(1, 100, limit=2)) == 2

    # Empty for users with no entries.
    assert db.bodyweight_history(1, 12345) == []
