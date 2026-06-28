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
    db.message_log_add(1, 100, "hello", message_id=1, at=base)

    assert db.presence_track_remove(1, 100, purge=True) is True
    assert db.presence_events_for(1, 100) == []
    assert db.message_count_since(1, 100) == 0


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
