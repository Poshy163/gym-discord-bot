"""Cardio program parsing, progression, and persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.cardio import (
    CardioParseError,
    Segment,
    apply_difficulty,
    format_segment,
    parse_chat_message,
    parse_program,
)
from app.db import Database


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "cardio.sqlite3")
    yield database
    database.close()


def test_parse_mixed_program_with_optional_levels():
    segments = parse_program(
        "15 mins elliptical level 12, "
        "30 on stair master, "
        "treadmill 15 minutes"
    )
    assert segments == [
        Segment("elliptical", 15, 12),
        Segment("stair master", 30, None),
        Segment("treadmill", 15, None),
    ]


def test_parse_program_accepts_at_level_and_newlines():
    segments = parse_program(
        "12m rowing at level 7\n20 minutes exercise bike"
    )
    assert segments == [
        Segment("rowing", 12, 7),
        Segment("exercise bike", 20, None),
    ]
    assert format_segment(segments[0]) == "12 mins rowing · level 7"


def test_parse_exact_chat_example_with_lv_incline_and_speed():
    text = (
        "15 mins elliptical lv12, 30mins on stair master lv10, "
        "15mins on treadmill 10 degrees 10 speed"
    )
    assert parse_chat_message(text) == [
        Segment("elliptical", 15, level=12),
        Segment("stair master", 30, level=10),
        Segment(
            "treadmill", 15, incline_degrees=10, speed=10,
        ),
    ]
    assert (
        format_segment(parse_chat_message(text)[2])
        == "15 mins treadmill · incline 10° · speed 10"
    )


@pytest.mark.parametrize(
    "text",
    [
        "15 minutes until dinner",
        "wait 15 minutes on the treadmill",
        "I walked for 15 minutes today",
        "gym was good today",
    ],
)
def test_passive_parser_ignores_conversation(text):
    assert parse_chat_message(text) is None


def test_activity_name_may_contain_the_word_level():
    assert parse_program("10 mins next level fitness treadmill") == [
        Segment("next level fitness treadmill", 10),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "elliptical",
        "0 mins elliptical",
        "15 mins",
        "15 mins elliptical level nope",
    ],
)
def test_parse_program_rejects_invalid_parts(text):
    with pytest.raises(CardioParseError):
        parse_program(text)


def test_standard_progression_raises_level_after_easy_then_right():
    original = [
        Segment("elliptical", 15, 12),
        Segment("treadmill", 15),
    ]
    first = apply_difficulty(original, "easy", "standard")
    assert first.segments == original
    assert first.score == 2

    second = apply_difficulty(
        first.segments, "just_right", "standard", first.score, first.cursor,
    )
    assert second.segments[0] == Segment("elliptical", 15, 13)
    assert second.segments[1] == original[1]
    assert second.direction == "increase"
    assert second.score == 0
    assert second.cursor == 1


def test_progression_adds_time_when_machine_has_no_level_and_rotates():
    original = [
        Segment("elliptical", 15, 12),
        Segment("treadmill", 15),
    ]
    result = apply_difficulty(
        original, "easy", "aggressive", score=0, cursor=1,
    )
    assert result.after == Segment("treadmill", 20)
    assert result.cursor == 0


def test_progression_increases_speed_when_present():
    original = [
        Segment("treadmill", 15, incline_degrees=10, speed=10),
    ]
    result = apply_difficulty(original, "easy", "aggressive")
    assert result.after == Segment(
        "treadmill", 15, incline_degrees=10, speed=10.5,
    )


def test_repeated_hard_sessions_deload_recent_segment():
    original = [
        Segment("elliptical", 15, 12),
        Segment("treadmill", 15),
    ]
    first = apply_difficulty(original, "hard", "standard", cursor=1)
    assert first.score == -2
    second = apply_difficulty(
        first.segments, "hard", "standard", first.score, first.cursor,
    )
    assert second.after == Segment("elliptical", 15, 11)
    assert second.direction == "decrease"


def test_program_round_trip_and_case_insensitive_replace(db):
    first = db.cardio_program_set(
        7, "Sam", "Cardio Day", "standard",
        [Segment("elliptical", 15, 12), Segment("treadmill", 15)],
    )
    assert first["name"] == "Cardio Day"
    assert len(first["segments"]) == 2

    replaced = db.cardio_program_set(
        7, "Sam", " cardio   day ", "gentle",
        [Segment("stair master", 30, 8)],
    )
    assert replaced["id"] == first["id"]
    assert replaced["pace"] == "gentle"
    assert len(db.cardio_program_list(7)) == 1
    loaded = db.cardio_program_get(7, "CARDIO DAY")
    assert loaded is not None
    assert loaded["segments"][0]["activity"] == "stair master"


def test_completion_snapshots_old_program_and_updates_next_workout(db):
    program = db.cardio_program_set(
        7, "Sam", "Cardio Day", "aggressive",
        [Segment("elliptical", 15, 12), Segment("treadmill", 15)],
    )
    current = [
        Segment(row["activity"], row["minutes"], row["level"])
        for row in program["segments"]
    ]
    progressed = apply_difficulty(current, "easy", "aggressive")
    when = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
    session_id = db.cardio_session_add(
        7,
        current,
        "easy",
        program_id=program["id"],
        program_name=program["name"],
        next_segments=progressed.segments,
        progression_score=progressed.score,
        progression_cursor=progressed.cursor,
        logged_at=when,
    )

    history = db.cardio_session_list(7)
    assert history[0]["id"] == session_id
    assert history[0]["segments"][0]["level"] == 12
    assert history[0]["logged_at"].startswith("2026-08-06")
    updated = db.cardio_program_get(7, "cardio day")
    assert updated is not None
    assert updated["segments"][0]["level"] == 13


def test_chat_session_dedupes_by_source_message_and_keeps_machine_settings(db):
    segments = [
        Segment("treadmill", 15, incline_degrees=10, speed=10),
    ]
    session_id = db.cardio_session_add(
        7,
        segments,
        "unrated",
        message_id=12345,
        channel_id=55,
    )
    row = db.cardio_session_get_by_message(12345)
    assert row is not None and row["id"] == session_id
    history = db.cardio_session_list(7)
    assert history[0]["segments"][0]["incline_degrees"] == 10
    assert history[0]["segments"][0]["speed"] == 10
    with pytest.raises(sqlite3.IntegrityError):
        db.cardio_session_add(
            7, segments, "unrated", message_id=12345, channel_id=55,
        )
    db.cardio_track_reply(
        999, 7, 7, session_id,
        original_message_id=12345, channel_id=55,
    )
    assert db.cardio_get_reply(999)["session_id"] == session_id
    assert db.cardio_delete_reply(999) is True
    assert db.cardio_get_reply(999) is None


def test_removing_program_keeps_session_history(db):
    program = db.cardio_program_set(
        7, "Sam", "Intervals", "standard", [Segment("bike", 20, 5)],
    )
    db.cardio_session_add(
        7,
        [Segment("bike", 20, 5)],
        "just_right",
        program_id=program["id"],
        program_name=program["name"],
    )
    assert db.cardio_program_remove(7, "intervals") is True
    history = db.cardio_session_list(7)
    assert history[0]["program_id"] is None
    assert history[0]["program_name"] == "Intervals"


def test_strava_link_snapshot_dedupes_and_can_detach(db):
    program = db.cardio_program_set(
        7, "Sam", "Morning Cardio", "standard",
        [Segment("elliptical", 15, 12)],
    )
    session_id = db.cardio_session_add(
        7,
        [Segment("elliptical", 15, 12)],
        "just_right",
        program_id=program["id"],
        program_name=program["name"],
        strava_activity_id=987654321,
        strava_name="Morning Elliptical",
        strava_sport_type="Elliptical",
        strava_distance_m=4200,
        strava_moving_time_s=901,
        strava_average_heartrate=144.5,
        strava_calories=260,
        strava_url="https://www.strava.com/activities/987654321",
    )

    linked = db.cardio_session_get_by_strava(7, 987654321)
    assert linked is not None and linked["id"] == session_id
    history = db.cardio_session_list(7)
    assert history[0]["strava_name"] == "Morning Elliptical"
    assert history[0]["strava_moving_time_s"] == 901
    assert history[0]["strava_average_heartrate"] == 144.5
    with pytest.raises(sqlite3.IntegrityError):
        db.cardio_session_add(
            7,
            [Segment("elliptical", 15, 12)],
            "just_right",
            strava_activity_id=987654321,
        )

    assert db.cardio_session_unlink_strava(7, 987654321) is True
    assert db.cardio_session_get_by_strava(7, 987654321) is None
    retained = db.cardio_session_list(7)[0]
    assert retained["id"] == session_id
    assert retained["segments"][0]["level"] == 12
    assert retained["strava_name"] is None


def test_cardio_program_get_by_id_enforces_owner(db):
    program = db.cardio_program_set(
        7, "Sam", "Intervals", "standard", [Segment("bike", 20, 5)],
    )
    assert db.cardio_program_get_by_id(7, program["id"])["name"] == "Intervals"
    assert db.cardio_program_get_by_id(8, program["id"]) is None


def test_migration_adds_chat_and_machine_setting_columns(tmp_path):
    path = tmp_path / "old-cardio.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE cardio_programs (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            name_key TEXT NOT NULL,
            name TEXT NOT NULL,
            pace TEXT NOT NULL,
            progression_score INTEGER NOT NULL,
            progression_cursor INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (user_id, name_key)
        );
        CREATE TABLE cardio_program_segments (
            program_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            activity TEXT NOT NULL,
            minutes REAL NOT NULL,
            level REAL,
            PRIMARY KEY (program_id, position)
        );
        CREATE TABLE cardio_sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            program_id INTEGER,
            program_name TEXT,
            difficulty TEXT NOT NULL,
            logged_at TEXT NOT NULL
        );
        CREATE TABLE cardio_session_segments (
            session_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            activity TEXT NOT NULL,
            minutes REAL NOT NULL,
            level REAL,
            PRIMARY KEY (session_id, position)
        );
        """
    )
    conn.close()

    migrated = Database(path)
    migrated.close()
    conn = sqlite3.connect(path)
    try:
        for pragma in (
            "PRAGMA table_info(cardio_program_segments)",
            "PRAGMA table_info(cardio_session_segments)",
        ):
            columns = {row[1] for row in conn.execute(pragma)}
            assert {"incline_degrees", "speed"} <= columns
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cardio_sessions)")
        }
        assert {
            "message_id",
            "channel_id",
            "strava_activity_id",
            "strava_name",
            "strava_sport_type",
            "strava_distance_m",
            "strava_moving_time_s",
            "strava_average_heartrate",
            "strava_calories",
            "strava_url",
        } <= session_columns
    finally:
        conn.close()
