"""Tests for the pure analytics helpers in app.training_math."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.training_math import (
    DEFAULT_BAR_KG,
    daily_streak,
    plate_breakdown,
    project_goal_eta,
    weekly_streak,
)


# -- plate_breakdown --------------------------------------------------------

def test_plate_breakdown_exact_100kg():
    pairs, leftover = plate_breakdown(100.0)
    # 100 - 20 bar = 80 / 2 per side = 40 = 25 + 15
    assert pairs == [(25.0, 1), (15.0, 1)]
    assert leftover == 0.0


def test_plate_breakdown_handles_quarter_plates():
    pairs, leftover = plate_breakdown(62.5)
    # per side = 21.25 = 20 + 1.25
    assert (20.0, 1) in pairs
    assert (1.25, 1) in pairs
    assert leftover == 0.0


def test_plate_breakdown_below_bar_reports_negative_leftover():
    pairs, leftover = plate_breakdown(15.0, bar_kg=DEFAULT_BAR_KG)
    assert pairs == []
    # 15kg target, 20kg bar → user is 5kg short of even loading the bar.
    assert leftover == -5.0


def test_plate_breakdown_uneven_target_reports_residual():
    pairs, leftover = plate_breakdown(101.0)
    # 101 - 20 = 81 / 2 = 40.5 per side. 40 fits as 25+15, 0.5 left per side
    # → 1.0kg total residual.
    assert pairs == [(25.0, 1), (15.0, 1)]
    assert leftover == pytest.approx(1.0)


def test_plate_breakdown_custom_bar():
    pairs, leftover = plate_breakdown(50.0, bar_kg=10.0)
    # 50 - 10 = 40 / 2 = 20 per side
    assert pairs == [(20.0, 1)]
    assert leftover == 0.0


# -- daily_streak -----------------------------------------------------------

def test_daily_streak_no_data():
    assert daily_streak([], date(2026, 5, 8)) == (0, 0)


def test_daily_streak_today_breaks_after_full_day_off():
    today = date(2026, 5, 8)
    # Logged yesterday and the two before — current streak = 3.
    days = [today - timedelta(days=i) for i in range(1, 4)]
    current, longest = daily_streak(days, today)
    assert current == 3
    assert longest == 3


def test_daily_streak_today_continues_streak():
    today = date(2026, 5, 8)
    days = [today - timedelta(days=i) for i in range(0, 4)]
    current, longest = daily_streak(days, today)
    assert current == 4
    assert longest == 4


def test_daily_streak_resets_after_two_day_gap():
    today = date(2026, 5, 8)
    # Last log was 2 days ago — current streak is 0 (yesterday is missing).
    days = [today - timedelta(days=2)]
    current, _ = daily_streak(days, today)
    assert current == 0


def test_daily_streak_longest_finds_historical_run():
    today = date(2026, 5, 8)
    days = [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
        date(2026, 1, 4), date(2026, 1, 5),  # 5-day run
        date(2026, 4, 1),  # isolated
    ]
    current, longest = daily_streak(days, today)
    assert longest == 5
    assert current == 0


# -- weekly_streak ----------------------------------------------------------

def test_weekly_streak_empty():
    assert weekly_streak([], date(2026, 5, 8)) == (0, 0)


def test_weekly_streak_three_consecutive_weeks():
    today = date(2026, 5, 8)  # Friday, week 19
    # One log per week for the previous 3 ISO weeks.
    days = [today - timedelta(weeks=i) for i in range(0, 3)]
    current, longest = weekly_streak(days, today)
    assert current == 3
    assert longest == 3


def test_weekly_streak_tolerates_empty_current_week():
    today = date(2026, 5, 8)
    # Last log was last week.
    days = [today - timedelta(weeks=1), today - timedelta(weeks=2)]
    current, _ = weekly_streak(days, today)
    assert current == 2


# -- project_goal_eta -------------------------------------------------------

UTC = timezone.utc


def test_project_goal_eta_already_hit():
    now = datetime(2026, 5, 8, tzinfo=UTC)
    history = [(datetime(2026, 1, 1, tzinfo=UTC), 100.0)]
    rate, eta, reason = project_goal_eta(history, target_kg=80.0, today=now)
    assert eta is None
    assert "already" in reason


def test_project_goal_eta_needs_two_points():
    now = datetime(2026, 5, 8, tzinfo=UTC)
    history = [(datetime(2026, 1, 1, tzinfo=UTC), 50.0)]
    rate, eta, reason = project_goal_eta(history, target_kg=100.0, today=now)
    assert rate is None
    assert eta is None


def test_project_goal_eta_linear_progress():
    # Gained 10kg over 10 weeks → 1kg/week. Need 20 more → 20 weeks out.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(weeks=10)
    history = [(start, 50.0), (end, 60.0)]
    today = end
    rate, eta, _ = project_goal_eta(history, target_kg=80.0, today=today)
    assert rate == pytest.approx(1.0)
    assert eta == (today + timedelta(weeks=20)).date()


def test_project_goal_eta_no_progress():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(weeks=10)
    history = [(start, 50.0), (end, 50.0)]
    rate, eta, reason = project_goal_eta(history, target_kg=80.0, today=end)
    assert rate == 0.0
    assert eta is None
    assert "progress" in reason
