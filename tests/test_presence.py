"""Tests for app.presence pure aggregation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.presence import (
    format_duration,
    is_online,
    nightly_sleep_sessions,
    sleep_stats,
    summarize_activities,
    summarize_activity_sets,
    summarize_presence,
)


def _sess(date, start_local, end_local, hours):
    return {
        "date": date, "start": "", "end": "",
        "start_local": start_local, "end_local": end_local,
        "duration_hours": hours,
    }


UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_is_online_buckets():
    assert is_online("online")
    assert is_online("idle")
    assert is_online("dnd")
    assert not is_online("offline")
    assert not is_online("invisible")


def test_format_duration_compact():
    assert format_duration(0) == "0m"
    assert format_duration(59) == "0m"
    assert format_duration(60) == "1m"
    assert format_duration(3600) == "1h"
    assert format_duration(3660) == "1h 1m"
    assert format_duration(86400) == "1d"
    assert format_duration(90061) == "1d 1h 1m"


def test_summary_empty_returns_zero():
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    s = summarize_presence([], start, end)
    assert s.online_seconds == 0
    assert s.offline_seconds == 0
    assert s.transitions == 0
    assert s.final_status is None


def test_carry_in_status_before_window():
    # User went online before the window opened; should count the whole
    # window as online with zero recorded transitions inside it.
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=4)
    events = [("online", _iso(start - timedelta(hours=1)))]
    s = summarize_presence(events, start, end)
    assert s.online_seconds == 4 * 3600
    assert s.offline_seconds == 0
    assert s.transitions == 0
    assert s.final_status == "online"


def test_single_transition_in_window():
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=4)
    # Offline carry-in, then online at +1h.
    events = [
        ("offline", _iso(start - timedelta(hours=2))),
        ("online", _iso(start + timedelta(hours=1))),
    ]
    s = summarize_presence(events, start, end)
    assert s.offline_seconds == 1 * 3600
    assert s.online_seconds == 3 * 3600
    assert s.transitions == 1
    assert s.final_status == "online"
    assert s.last_online_at == start + timedelta(hours=1)


def test_events_outside_window_are_ignored():
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    events = [
        ("online", _iso(start - timedelta(days=10))),
        ("offline", _iso(end + timedelta(hours=5))),  # past end
    ]
    s = summarize_presence(events, start, end)
    # carry-in online, the past-end event is ignored.
    assert s.online_seconds == 2 * 3600
    assert s.transitions == 0


def test_weekday_and_hour_buckets_split_at_midnight():
    # Sunday 23:00 UTC -> Monday 02:00 UTC (3 hours total online).
    start = datetime(2026, 5, 3, 23, 0, tzinfo=UTC)  # Sunday
    end = datetime(2026, 5, 4, 2, 0, tzinfo=UTC)     # Monday
    events = [("online", _iso(start))]
    s = summarize_presence(events, start, end)
    assert s.online_seconds == 3 * 3600
    # Sunday=6 gets 1 hour (23:00-00:00); Monday=0 gets 2 hours (00:00-02:00).
    assert s.by_weekday[6] == 3600
    assert s.by_weekday[0] == 2 * 3600
    # Hours: 23, 0, 1 each get 1 hour.
    assert s.by_hour[23] == 3600
    assert s.by_hour[0] == 3600
    assert s.by_hour[1] == 3600


def test_transitions_count_only_real_changes():
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=10)
    events = [
        ("offline", _iso(start - timedelta(hours=1))),
        ("online", _iso(start + timedelta(hours=1))),
        ("idle", _iso(start + timedelta(hours=2))),  # online -> idle, both online
        ("offline", _iso(start + timedelta(hours=5))),
        ("offline", _iso(start + timedelta(hours=6))),  # duplicate, still counts as recorded change? No: status equal
    ]
    s = summarize_presence(events, start, end)
    # Real status changes within window: offline->online, online->idle,
    # idle->offline. The duplicate offline->offline is filtered.
    assert s.transitions == 3


def test_nightly_sleep_sessions_basic():
    # Online during the day, offline overnight for ~8h.
    start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    events = [
        ("online", _iso(start)),
        ("offline", _iso(datetime(2026, 5, 1, 23, 0, tzinfo=UTC))),
        ("online", _iso(datetime(2026, 5, 2, 7, 0, tzinfo=UTC))),
    ]
    sessions = nightly_sleep_sessions(events, start, end)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["duration_hours"] == 8.0
    assert s["start"] == datetime(2026, 5, 1, 23, 0, tzinfo=UTC).isoformat()
    assert s["end"] == datetime(2026, 5, 2, 7, 0, tzinfo=UTC).isoformat()
    # Attributed to the local wake date.
    assert s["date"] == "2026-05-02"


def test_nightly_sleep_sessions_ignores_short_offline():
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    events = [
        ("online", _iso(start)),
        ("offline", _iso(datetime(2026, 5, 1, 3, 0, tzinfo=UTC))),
        ("online", _iso(datetime(2026, 5, 1, 4, 0, tzinfo=UTC))),  # only 1h
    ]
    # Below the 3h minimum -> not a sleep session.
    assert nightly_sleep_sessions(events, start, end) == []


def test_nightly_sleep_sessions_merges_brief_online_flicker():
    # An 8h offline block split by a 1-minute reconnect should stay one night.
    start = datetime(2026, 5, 1, 20, 0, tzinfo=UTC)
    end = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    events = [
        ("online", _iso(start)),
        ("offline", _iso(datetime(2026, 5, 1, 22, 0, tzinfo=UTC))),
        ("online", _iso(datetime(2026, 5, 2, 2, 0, tzinfo=UTC))),
        ("offline", _iso(datetime(2026, 5, 2, 2, 1, tzinfo=UTC))),  # 1-min blip
        ("online", _iso(datetime(2026, 5, 2, 6, 0, tzinfo=UTC))),
    ]
    sessions = nightly_sleep_sessions(events, start, end)
    assert len(sessions) == 1
    assert sessions[0]["duration_hours"] == 8.0


def test_nightly_sleep_sessions_empty_window():
    start = datetime(2026, 5, 1, tzinfo=UTC)
    assert nightly_sleep_sessions([], start, start) == []


# --- concurrent-activity aggregation --------------------------------------

def test_summarize_activity_sets_credits_each_overlapping_game():
    start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    # Plays tModLoader for the full 2h; Excel joins for the middle hour.
    events = [
        (["tModLoader"], _iso(start)),
        (["tModLoader", "Excel"], _iso(start + timedelta(minutes=30))),
        (["tModLoader"], _iso(start + timedelta(minutes=90))),
    ]
    totals = summarize_activity_sets(events, start, end)
    assert totals["tModLoader"] == 2 * 3600  # whole window
    assert totals["Excel"] == 3600           # only the overlapping hour
    # Sorted descending by time.
    assert list(totals) == ["tModLoader", "Excel"]


def test_summarize_activity_sets_carry_in_and_stop():
    start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    events = [
        (["Rust"], _iso(start - timedelta(hours=1))),   # carry-in
        ([], _iso(start + timedelta(hours=1))),          # stopped at the 1h mark
    ]
    totals = summarize_activity_sets(events, start, end)
    assert totals == {"Rust": 3600}  # 1h inside the window, none after stop


def test_summarize_activities_adapter_matches_single_track():
    start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    events = [("Halo", _iso(start)), (None, _iso(start + timedelta(hours=1)))]
    assert summarize_activities(events, start, end) == {"Halo": 3600}


def test_summarize_activity_sets_empty_window():
    start = datetime(2026, 5, 1, tzinfo=UTC)
    assert summarize_activity_sets([(["X"], _iso(start))], start, start) == {}


# --- sleep_stats ----------------------------------------------------------

def test_sleep_stats_empty():
    s = sleep_stats([])
    assert s["nights"] == 0
    assert s["avg_hours"] is None
    assert s["debt_hours"] == 0.0
    assert s["series"] == []


def test_sleep_stats_core_numbers():
    sessions = [
        _sess("2026-06-01", "2026-05-31 23:00", "2026-06-01 06:00", 7.0),
        _sess("2026-06-02", "2026-06-02 01:00", "2026-06-02 10:00", 9.0),
    ]
    s = sleep_stats(sessions, target_hours=8.0)
    assert s["nights"] == 2
    assert s["avg_hours"] == 8.0
    assert s["min_hours"] == 7.0 and s["max_hours"] == 9.0
    assert s["std_hours"] == 1.0           # consistency
    assert s["debt_hours"] == 0.0          # 8*2 - 16
    assert [p["hours"] for p in s["series"]] == [7.0, 9.0]


def test_sleep_stats_bedtime_is_circular():
    # Bedtimes 23:00 and 01:00 average to ~midnight, not noon.
    sessions = [
        _sess("2026-06-01", "2026-05-31 23:00", "2026-06-01 07:00", 8.0),
        _sess("2026-06-02", "2026-06-02 01:00", "2026-06-02 09:00", 8.0),
    ]
    s = sleep_stats(sessions)
    assert s["bedtime"] == "00:00"


def test_sleep_stats_debt_when_under_target():
    sessions = [_sess("2026-06-01", "2026-06-01 02:00", "2026-06-01 08:00", 6.0)]
    s = sleep_stats(sessions, target_hours=8.0)
    assert s["debt_hours"] == 2.0  # slept 2h under target


def test_sleep_stats_weekday_vs_weekend():
    # 2026-06-06 is a Saturday, 2026-06-08 a Monday.
    sessions = [
        _sess("2026-06-06", "2026-06-06 02:00", "2026-06-06 12:00", 10.0),
        _sess("2026-06-08", "2026-06-08 00:00", "2026-06-08 07:00", 7.0),
    ]
    s = sleep_stats(sessions)
    assert s["weekend_avg"] == 10.0
    assert s["weekday_avg"] == 7.0
