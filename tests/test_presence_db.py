"""Tests for the presence_* helpers on Database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_track_add_remove_and_list(db):
    assert db.presence_track_add(1, 100, started_by=999) is True
    # Idempotent: second add returns False but doesn't error.
    assert db.presence_track_add(1, 100, started_by=999) is False
    assert db.presence_is_tracked(1, 100) is True
    assert db.presence_is_tracked(1, 200) is False

    rows = db.presence_track_list(1)
    assert len(rows) == 1
    assert int(rows[0]["user_id"]) == 100
    assert int(rows[0]["started_by"]) == 999

    assert db.presence_track_remove(1, 100) is True
    assert db.presence_track_remove(1, 100) is False
    assert db.presence_is_tracked(1, 100) is False


def test_log_event_dedupes_consecutive_duplicates(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert db.presence_log_event(1, 100, "online", at=base) is True
    # Same status as last -> no new row.
    assert db.presence_log_event(
        1, 100, "online", at=base + timedelta(minutes=10),
    ) is False
    assert db.presence_log_event(
        1, 100, "offline", at=base + timedelta(minutes=20),
    ) is True
    # Distinct user is independent.
    assert db.presence_log_event(1, 200, "online", at=base) is True

    rows_100 = db.presence_events_for(1, 100)
    assert [r["status"] for r in rows_100] == ["online", "offline"]
    rows_200 = db.presence_events_for(1, 200)
    assert [r["status"] for r in rows_200] == ["online"]


def test_events_for_carries_in_prior_event(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    db.presence_log_event(1, 100, "online", at=base)
    db.presence_log_event(1, 100, "offline", at=base + timedelta(days=2))
    db.presence_log_event(1, 100, "online", at=base + timedelta(days=5))

    # Window starts on day 3, so should include the day 2 'offline' as
    # carry-in plus the day 5 'online' inside the window.
    rows = db.presence_events_for(
        1, 100,
        since=base + timedelta(days=3),
        until=base + timedelta(days=10),
    )
    statuses = [r["status"] for r in rows]
    assert statuses == ["offline", "online"]


def test_remove_with_purge_clears_history(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    db.presence_track_add(1, 100, started_by=1)
    db.presence_log_event(1, 100, "online", at=base)
    db.presence_log_event(1, 100, "offline", at=base + timedelta(hours=1))

    assert db.presence_track_remove(1, 100, purge=True) is True
    assert db.presence_events_for(1, 100) == []


def test_remove_without_purge_keeps_history(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    db.presence_track_add(1, 100, started_by=1)
    db.presence_log_event(1, 100, "online", at=base)

    assert db.presence_track_remove(1, 100) is True
    rows = db.presence_events_for(1, 100)
    assert len(rows) == 1
    assert rows[0]["status"] == "online"


def test_activity_log_event_ignores_initial_empty_activity(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)

    assert db.activity_log_event(1, 100, None, at=base) is False

    rows = db.activity_events_for(1, 100)
    assert rows == []


def test_activity_log_event_records_games_and_stops(db):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)

    assert db.activity_log_event(1, 100, "Rust", at=base) is True
    assert db.activity_log_event(
        1, 100, "Rust", at=base + timedelta(minutes=5),
    ) is False
    assert db.activity_log_event(
        1, 100, None, at=base + timedelta(minutes=10),
    ) is True

    rows = db.activity_events_for(1, 100)
    assert [r["activity"] for r in rows] == ["Rust", None]


# --- web-dashboard activity snapshots -------------------------------------

def test_activity_image_captured_and_current(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    db.activity_log_event(1, 100, "Rocket League", t0, image_url="http://img/rl.png")
    cur = db.activity_current(1, 100)
    assert cur["activity"] == "Rocket League"
    assert cur["image_url"] == "http://img/rl.png"
    # Same game again is de-duped on name (no new row), even without an image.
    assert db.activity_log_event(1, 100, "Rocket League", t0 + timedelta(minutes=5)) is False
    # Stopping playing logs a null-activity row and becomes "current".
    assert db.activity_log_event(1, 100, None, t0 + timedelta(hours=1)) is True
    assert db.activity_current(1, 100)["activity"] is None


def test_activity_image_map_keeps_known_art(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # First session has art, a later "current" event for the same game doesn't.
    db.activity_log_event(1, 100, "Halo", t0, image_url="http://img/halo.png")
    db.activity_log_event(1, 100, None, t0 + timedelta(hours=1))
    db.activity_log_event(1, 100, "Halo", t0 + timedelta(hours=2))  # no image now
    assert db.activity_current(1, 100)["image_url"] is None
    # ...but the image map still resolves the art from the earlier session.
    assert db.activity_image_map(1, 100) == {"Halo": "http://img/halo.png"}


def test_presence_current_returns_latest(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    db.presence_log_event(1, 100, "online", t0)
    db.presence_log_event(1, 100, "dnd", t0 + timedelta(hours=1))
    assert db.presence_current(1, 100)["status"] == "dnd"
    assert db.presence_current(1, 200) is None


# --- message logging (web-dashboard activity feed) ------------------------

def test_message_log_add_and_recent_newest_first(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert db.message_log_add(
        1, 100, "first", channel_id=7, channel_name="general",
        message_id=1001, at=t0,
    ) is True
    assert db.message_log_add(
        1, 100, "second", channel_id=7, channel_name="general",
        message_id=1002, at=t0 + timedelta(minutes=5),
    ) is True

    rows = db.message_log_recent(1, 100)
    assert [r["content"] for r in rows] == ["second", "first"]
    assert rows[0]["channel_name"] == "general"


def test_message_log_add_idempotent_on_message_id(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert db.message_log_add(1, 100, "hi", message_id=42, at=t0) is True
    # Re-dispatch of the same message: ignored, no duplicate row.
    assert db.message_log_add(1, 100, "hi", message_id=42, at=t0) is False
    assert db.message_count_since(1, 100) == 1


def test_message_count_and_recent_respect_since_and_limit(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(5):
        db.message_log_add(
            1, 100, f"m{i}", message_id=2000 + i, at=base + timedelta(days=i),
        )
    # Distinct user is independent.
    db.message_log_add(1, 200, "other", message_id=3000, at=base)

    # Window starting on day 2 only sees m2, m3, m4.
    since = base + timedelta(days=2)
    assert db.message_count_since(1, 100, since) == 3
    assert db.message_count_since(1, 100) == 5
    assert db.message_count_since(1, 200) == 1

    limited = db.message_log_recent(1, 100, since=since, limit=2)
    assert [r["content"] for r in limited] == ["m4", "m3"]


def test_message_active_users_counts_and_orders(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # User 100: two messages, latest on day 1.
    db.message_log_add(1, 100, "a", message_id=1, at=base)
    db.message_log_add(1, 100, "b", message_id=2, at=base + timedelta(days=1))
    # User 200: one message, latest on day 3 (more recent than 100).
    db.message_log_add(1, 200, "c", message_id=3, at=base + timedelta(days=3))
    # Different guild is independent.
    db.message_log_add(2, 300, "d", message_id=4, at=base)

    rows = db.message_active_users(1)
    # Most recently active first: 200 (day 3) then 100 (day 1).
    assert [int(r["user_id"]) for r in rows] == [200, 100]
    by_uid = {int(r["user_id"]): r for r in rows}
    assert by_uid[100]["count"] == 2
    assert by_uid[200]["count"] == 1

    # `since` filters out user 100's older window.
    recent = db.message_active_users(1, since=base + timedelta(days=2))
    assert [int(r["user_id"]) for r in recent] == [200]


def test_message_log_attachments_stored_and_backfilled(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    img = '[{"url": "http://x/a.png", "kind": "image"}]'
    gif = '[{"url": "http://x/b.mp4", "kind": "video"}]'
    # A message logged with media stores it.
    assert db.message_log_add(1, 100, "pic", message_id=1, channel_id=7,
                              attachments=img, at=base) is True
    assert db.message_channel_log(1, 7)[0]["attachments"] == img

    # A previously text-only message...
    assert db.message_log_add(1, 100, "hi", message_id=2, channel_id=7,
                              at=base) is True
    # ...gets its media backfilled on a re-scan (returns False — not a new row).
    assert db.message_log_add(1, 100, "hi", message_id=2, channel_id=7,
                              attachments=gif, at=base) is False
    rows = {r["content"]: r["attachments"] for r in db.message_channel_log(1, 7)}
    assert rows["hi"] == gif
    # Existing media is never overwritten by a later re-scan.
    assert db.message_log_add(1, 100, "hi", message_id=2, channel_id=7,
                              attachments=img, at=base) is False
    rows = {r["content"]: r["attachments"] for r in db.message_channel_log(1, 7)}
    assert rows["hi"] == gif


def test_message_log_update_content_overwrites_media(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    img = '[{"url": "/media/1/5.png", "kind": "image", "stored": true}]'
    gif = '[{"url": "/media/1/6.mp4", "kind": "video", "stored": true}]'
    db.message_log_add(1, 100, "before", message_id=9, channel_id=7,
                       attachments=img, at=base)
    # An edit replaces both text and media (unlike the add-time backfill) and
    # stamps edited_at.
    assert db.message_log_update_content(1, 9, "after", gif) is True
    row = db.message_channel_log(1, 7)[0]
    assert row["content"] == "after"
    assert row["attachments"] == gif
    assert row["edited_at"] is not None
    # No row for an unknown message id.
    assert db.message_log_update_content(1, 999, "x", None) is False


def test_message_log_mark_deleted_keeps_content(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.message_log_add(1, 100, "bye", message_id=11, channel_id=7, at=base)
    assert db.message_log_mark_deleted(1, 11) is True
    row = db.message_channel_log(1, 7)[0]
    # Content is preserved; only the deletion marker is set.
    assert row["content"] == "bye"
    assert row["deleted_at"] is not None
    # Idempotent — re-flagging an already-deleted row is a no-op.
    assert db.message_log_mark_deleted(1, 11) is False
    # Unknown message id is a no-op too.
    assert db.message_log_mark_deleted(1, 12345) is False


def test_message_log_latest_at_tracks_newest(db):
    assert db.message_log_latest_at(1) is None
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.message_log_add(1, 100, "old", message_id=1, at=base)
    db.message_log_add(1, 200, "new", message_id=2, at=base + timedelta(days=2))
    # Older message in a different guild doesn't bleed through.
    db.message_log_add(2, 300, "other", message_id=3, at=base + timedelta(days=9))

    latest = db.message_log_latest_at(1)
    assert latest == (base + timedelta(days=2)).isoformat()
    assert db.message_log_latest_at(99) is None


def test_message_channels_and_channel_log(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.upsert_member(1, 100, "alice", "Alice", avatar="http://a.png")
    db.message_log_add(1, 100, "g1", message_id=1, channel_id=7,
                       channel_name="general", at=base)
    db.message_log_add(1, 200, "g2", message_id=2, channel_id=7,
                       channel_name="general", at=base + timedelta(minutes=1))
    db.message_log_add(1, 100, "gym1", message_id=3, channel_id=8,
                       channel_name="gym", at=base + timedelta(minutes=5))

    chans = {int(r["channel_id"]): r for r in db.message_channels(1)}
    assert chans[7]["count"] == 2 and chans[7]["channel_name"] == "general"
    assert chans[8]["count"] == 1
    # Most recently active channel first.
    assert int(db.message_channels(1)[0]["channel_id"]) == 8

    rows = db.message_channel_log(1, 7)
    # Chat order (oldest first), with author info joined from members.
    assert [r["content"] for r in rows] == ["g1", "g2"]
    assert rows[0]["display_name"] == "Alice"
    assert rows[0]["avatar"] == "http://a.png"
    assert rows[1]["display_name"] is None  # user 200 not mirrored


def test_message_blacklist_add_keeps_messages(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.message_log_add(1, 100, "keep", message_id=1, at=base)
    db.message_log_add(1, 200, "still here", message_id=2, at=base)

    assert db.message_is_blacklisted(1, 200) is False
    assert db.message_blacklist_add(1, 200, "spamming", "web:1.2.3.4") is True
    # Blacklisting blocks the user from adding data but never deletes messages.
    assert db.message_count_since(1, 200) == 1
    assert db.message_count_since(1, 100) == 1
    assert db.message_is_blacklisted(1, 200) is True
    assert db.message_blacklisted_ids(1) == {200}

    rows = db.message_blacklist_list(1)
    assert len(rows) == 1
    assert int(rows[0]["user_id"]) == 200
    assert rows[0]["reason"] == "spamming"
    assert rows[0]["added_by"] == "web:1.2.3.4"

    # Re-adding updates the reason (upsert), not a second row.
    assert db.message_blacklist_add(1, 200, "still spamming") is True
    rows = db.message_blacklist_list(1)
    assert len(rows) == 1 and rows[0]["reason"] == "still spamming"

    assert db.message_blacklist_remove(1, 200) is True
    assert db.message_blacklist_remove(1, 200) is False
    assert db.message_blacklisted_ids(1) == set()


def test_voice_events_logged_newest_first_with_member(db):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.upsert_member(1, 100, "alice", "Alice", avatar="http://a.png")
    db.voice_log_event(1, 100, "join", channel_id=5, channel_name="General",
                       at=base)
    db.voice_log_event(1, 100, "move", channel_id=6, channel_name="Gym",
                       at=base + timedelta(minutes=2))
    db.voice_log_event(1, 100, "leave", channel_id=6, channel_name="Gym",
                       at=base + timedelta(minutes=5))
    # Different guild is independent.
    db.voice_log_event(2, 200, "join", channel_id=9, channel_name="Other",
                       at=base)

    rows = db.voice_events_recent(1)
    assert [r["event"] for r in rows] == ["leave", "move", "join"]
    assert rows[0]["channel_name"] == "Gym"
    assert rows[0]["display_name"] == "Alice"
    assert rows[0]["avatar"] == "http://a.png"

    # `limit` and `since` both narrow the result.
    assert len(db.voice_events_recent(1, limit=1)) == 1
    recent = db.voice_events_recent(1, since=base + timedelta(minutes=3))
    assert [r["event"] for r in recent] == ["leave"]


# --- concurrent activity snapshots ----------------------------------------

def test_activity_log_set_records_concurrent_games(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # Two games at once, the second carrying art.
    assert db.activity_log_set(
        1, 100, [("tModLoader", None), ("Excel", "http://img/xl.png")], at=t0,
    ) is True
    # Same set of names again is de-duped even though images could differ.
    assert db.activity_log_set(
        1, 100, [("tModLoader", "http://img/tml.png"), ("Excel", None)],
        at=t0 + timedelta(minutes=5),
    ) is False
    # Dropping one game is a change -> new row.
    assert db.activity_log_set(
        1, 100, [("tModLoader", None)], at=t0 + timedelta(minutes=10),
    ) is True

    sets = db.activity_sets_for(1, 100)
    assert [names for names, _at in sets] == [
        ["tModLoader", "Excel"], ["tModLoader"],
    ]
    # Primary (first) game mirrors into the legacy column for back-compat.
    cur = db.activity_current(1, 100)
    assert cur["activity"] == "tModLoader"


def test_activity_current_set_decodes_snapshot(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    db.activity_log_set(1, 100, [("Rust", "http://img/rust.png"), ("Discord", None)], at=t0)
    acts, at = db.activity_current_set(1, 100)
    assert acts == [
        {"n": "Rust", "i": "http://img/rust.png"},
        {"n": "Discord", "i": None},
    ]
    assert at == t0.isoformat()
    # Stopping everything yields an empty set, not None.
    db.activity_log_set(1, 100, [], at=t0 + timedelta(hours=1))
    acts2, _ = db.activity_current_set(1, 100)
    assert acts2 == []


def test_activity_image_map_spans_concurrent_activities(db):
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # Art arrives for a *secondary* activity in one snapshot...
    db.activity_log_set(
        1, 100, [("Rust", None), ("Crosshair X", "http://img/cx.png")], at=t0,
    )
    # ...and for the primary in a later one.
    db.activity_log_set(
        1, 100, [("Rust", "http://img/rust.png")], at=t0 + timedelta(hours=1),
    )
    assert db.activity_image_map(1, 100) == {
        "Crosshair X": "http://img/cx.png",
        "Rust": "http://img/rust.png",
    }


def test_activity_log_event_still_drives_set_storage(db):
    # The legacy single-activity wrapper round-trips through the set model.
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert db.activity_log_event(1, 100, "Halo", at=t0) is True
    assert db.activity_sets_for(1, 100) == [(["Halo"], t0.isoformat())]
