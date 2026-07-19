"""Tests for app.voicetime pure aggregation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.voicetime import VoiceSummary, summarize_voice


UTC = timezone.utc
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _at(hours: float) -> str:
    return _iso(T0 + timedelta(hours=hours))


def test_empty_events_returns_zero():
    s = summarize_voice([], T0, T0 + timedelta(hours=4))
    assert s.in_call_seconds == 0
    assert s.muted_seconds == 0
    assert s.deafened_seconds == 0
    assert not s.in_call_now
    assert not s.muted_now
    assert s.current_muted_seconds == 0


def test_window_end_not_after_start_is_empty():
    s = summarize_voice([("join", _at(-1))], T0, T0)
    assert s.in_call_seconds == 0


def test_simple_join_leave_no_mute():
    events = [("join", _at(0)), ("leave", _at(2))]
    s = summarize_voice(events, T0, T0 + timedelta(hours=4), live_in_call=False)
    assert s.in_call_seconds == 2 * 3600
    assert s.muted_seconds == 0
    assert not s.in_call_now


def test_carry_in_join_still_in_call_counts_whole_window():
    # Joined an hour before the window opened and never left; live state
    # confirms they're still connected, so the whole window is in-call time and
    # the current streak reflects the true (pre-window) join instant.
    events = [("join", _at(-1))]
    end = T0 + timedelta(hours=4)
    s = summarize_voice(events, T0, end, live_in_call=True)
    assert s.in_call_seconds == 4 * 3600
    assert s.in_call_now
    # from -1h to +4h = 5h of continuous connection
    assert s.current_in_call_seconds == 5 * 3600


def test_join_already_muted_starts_clock_at_join():
    # The handler logs `join` then `mute_on` at the same instant when a member
    # joins already muted. The mute clock starts at join.
    events = [("join", _at(0)), ("mute_on", _at(0))]
    end = T0 + timedelta(hours=2)
    s = summarize_voice(events, T0, end, live_in_call=True, live_muted=True)
    assert s.in_call_seconds == 2 * 3600
    assert s.muted_seconds == 2 * 3600
    assert s.muted_now
    assert s.current_muted_seconds == 2 * 3600


def test_leave_while_muted_terminates_interval():
    # Muted at +0.5h, left at +1h without a mute_off. The mute interval ends at
    # the leave; nothing accrues after.
    events = [("join", _at(0)), ("mute_on", _at(0.5)), ("leave", _at(1))]
    s = summarize_voice(
        events, T0, T0 + timedelta(hours=3), live_in_call=False,
    )
    assert s.in_call_seconds == 1 * 3600
    assert s.muted_seconds == 0.5 * 3600
    assert not s.muted_now
    assert not s.in_call_now
    assert s.current_muted_seconds == 0


def test_move_channels_while_muted_persists():
    # Moving rooms mid-call is not a break: in-call and mute both continue.
    events = [("join", _at(0)), ("mute_on", _at(0.5)), ("move", _at(1))]
    end = T0 + timedelta(hours=2)
    s = summarize_voice(events, T0, end, live_in_call=True, live_muted=True)
    assert s.in_call_seconds == 2 * 3600
    assert s.muted_seconds == 1.5 * 3600
    # Continuous connection since the original join, not reset by the move.
    assert s.current_in_call_seconds == 2 * 3600
    assert s.current_muted_seconds == 1.5 * 3600


def test_restart_mid_mute_drops_phantom_when_not_live():
    # Open mute_on with no mute_off and no leave (bot restarted). Live state
    # says the member is no longer in the call -> the unverified tail is dropped
    # rather than accruing to window end.
    events = [("join", _at(0)), ("mute_on", _at(1))]
    s = summarize_voice(
        events, T0, T0 + timedelta(hours=3),
        live_in_call=False, live_muted=False,
    )
    # Only the verified [join, mute_on] hour of in-call time survives.
    assert s.in_call_seconds == 1 * 3600
    assert s.muted_seconds == 0
    assert not s.in_call_now
    assert not s.muted_now


def test_open_mute_accrues_when_live_confirms():
    # Same open mute_on, but the member is verifiably still muted in-call now.
    events = [("join", _at(0)), ("mute_on", _at(1))]
    end = T0 + timedelta(hours=3)
    s = summarize_voice(
        events, T0, end, live_in_call=True, live_muted=True,
    )
    assert s.in_call_seconds == 3 * 3600
    assert s.muted_seconds == 2 * 3600  # muted from +1h to +3h
    assert s.muted_now
    assert s.current_muted_seconds == 2 * 3600


def test_unverified_tail_trusts_log_when_flags_absent():
    # No live flags (e.g. the web feed can't check) -> fall back to the log's
    # own end state and credit the tail.
    events = [("join", _at(0)), ("mute_on", _at(1))]
    end = T0 + timedelta(hours=3)
    s = summarize_voice(events, T0, end)
    assert s.in_call_seconds == 3 * 3600
    assert s.muted_seconds == 2 * 3600
    assert s.muted_now


def test_deafen_recorded_separately_as_subset_of_mute():
    events = [
        ("join", _at(0)),
        ("mute_on", _at(1)),
        ("deaf_on", _at(2)),
        ("deaf_off", _at(3)),
    ]
    end = T0 + timedelta(hours=4)
    s = summarize_voice(
        events, T0, end,
        live_in_call=True, live_muted=True, live_deafened=False,
    )
    assert s.in_call_seconds == 4 * 3600
    assert s.muted_seconds == 3 * 3600      # +1h..+4h
    assert s.deafened_seconds == 1 * 3600   # +2h..+3h
    assert s.muted_fraction() == 0.75
    assert s.deafened_fraction() == 0.25
    assert s.muted_now
    assert not s.deafened_now


def test_carry_in_muted_spans_window():
    # Joined and muted before the window; no in-window events.
    events = [("join", _at(-2)), ("mute_on", _at(-1))]
    end = T0 + timedelta(hours=2)
    s = summarize_voice(events, T0, end, live_in_call=True, live_muted=True)
    assert s.in_call_seconds == 2 * 3600
    assert s.muted_seconds == 2 * 3600
    # streaks measured to true transition instants (before the window)
    assert s.current_in_call_seconds == 4 * 3600  # since -2h
    assert s.current_muted_seconds == 3 * 3600     # since -1h


def test_mute_off_ends_muted():
    events = [("join", _at(0)), ("mute_on", _at(1)), ("mute_off", _at(2))]
    end = T0 + timedelta(hours=3)
    s = summarize_voice(events, T0, end, live_in_call=True, live_muted=False)
    assert s.in_call_seconds == 3 * 3600
    assert s.muted_seconds == 1 * 3600  # +1h..+2h only
    assert not s.muted_now


def test_join_resets_stale_mute_from_previous_session():
    # Session 1 leaves while muted (no mute_off). Session 2 rejoins unmuted; the
    # join must clear the stale mute so session 2 accrues no muted time.
    events = [
        ("join", _at(0)),
        ("mute_on", _at(0.5)),
        ("leave", _at(1)),
        ("join", _at(1.5)),
        ("leave", _at(2)),
    ]
    s = summarize_voice(
        events, T0, T0 + timedelta(hours=3), live_in_call=False,
    )
    assert s.in_call_seconds == 1.5 * 3600   # 1h + 0.5h
    assert s.muted_seconds == 0.5 * 3600     # session 1 only
    assert not s.muted_now


def test_events_after_window_end_ignored():
    events = [("join", _at(0)), ("leave", _at(1)), ("join", _at(5))]
    s = summarize_voice(events, T0, T0 + timedelta(hours=3), live_in_call=False)
    assert s.in_call_seconds == 1 * 3600
    assert not s.in_call_now


def test_fractions_zero_without_call_time():
    s = summarize_voice([], T0, T0 + timedelta(hours=1))
    assert s.muted_fraction() == 0.0
    assert s.deafened_fraction() == 0.0


def test_active_seconds_is_in_call_minus_muted():
    # 4h in call, muted only the middle hour → 3h audible/active.
    events = [
        ("join", _at(0)),
        ("mute_on", _at(1)),
        ("mute_off", _at(2)),
        ("leave", _at(4)),
    ]
    s = summarize_voice(events, T0, T0 + timedelta(hours=5), live_in_call=False)
    assert s.in_call_seconds == 4 * 3600
    assert s.muted_seconds == 1 * 3600
    assert s.active_seconds == 3 * 3600


def test_active_seconds_zero_when_muted_entire_call():
    # Joined already muted and never unmuted → no audible time at all.
    events = [("join", _at(0)), ("mute_on", _at(0)), ("leave", _at(2))]
    s = summarize_voice(events, T0, T0 + timedelta(hours=3), live_in_call=False)
    assert s.in_call_seconds == 2 * 3600
    assert s.muted_seconds == 2 * 3600
    assert s.active_seconds == 0


def test_active_seconds_equals_in_call_when_never_muted():
    events = [("join", _at(0)), ("leave", _at(2))]
    s = summarize_voice(events, T0, T0 + timedelta(hours=3), live_in_call=False)
    assert s.muted_seconds == 0
    assert s.active_seconds == s.in_call_seconds == 2 * 3600


def test_active_seconds_never_negative_on_float_drift():
    # Two separately-summed float accumulators can leave muted a hair above
    # in-call; active must clamp to 0 rather than report a tiny negative.
    s = VoiceSummary(in_call_seconds=100.0, muted_seconds=100.000000001)
    assert s.active_seconds == 0.0


def test_current_streak_measured_to_now_argument():
    # now overrides window_end for the streak length (the command passes the
    # real wall-clock now, matching window_end).
    events = [("join", _at(0)), ("mute_on", _at(1))]
    end = T0 + timedelta(hours=2)
    s = summarize_voice(
        events, T0, end, now=end, live_in_call=True, live_muted=True,
    )
    assert s.current_in_call_seconds == 2 * 3600
    assert s.current_muted_seconds == 1 * 3600
