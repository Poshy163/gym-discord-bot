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
