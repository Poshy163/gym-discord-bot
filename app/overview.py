"""Consistency overview calculations for one user's lift history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable

from .graphing import daily_best_points


@dataclass(frozen=True)
class LiftOverview:
    total_logs: int
    active_days: int
    active_weeks: int
    total_weeks: int
    consistency_score: int
    current_week_streak: int
    best_kg: float
    first_kg: float
    latest_kg: float
    improvement_kg: float
    first_day: date
    latest_day: date
    days_since_latest: int
    avg_gap_days: float | None
    longest_gap_days: int | None
    logs_last_30_days: int


def _week_key(day: date) -> tuple[int, int]:
    year, week, _ = day.isocalendar()
    return year, week


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _total_weeks_between(first_day: date, latest_day: date) -> int:
    return ((_week_start(latest_day) - _week_start(first_day)).days // 7) + 1


def _previous_week(year: int, week: int) -> tuple[int, int]:
    prev_day = datetime.fromisocalendar(year, week, 1).date() - timedelta(days=7)
    return _week_key(prev_day)


def _current_week_streak(active_days: Iterable[date], today: date) -> int:
    weeks = {_week_key(day) for day in active_days}
    if not weeks:
        return 0
    today_week = _week_key(today)
    previous = _previous_week(*today_week)
    if today_week in weeks:
        cursor = today_week
    elif previous in weeks:
        cursor = previous
    else:
        return 0

    streak = 0
    while cursor in weeks:
        streak += 1
        cursor = _previous_week(*cursor)
    return streak


def lift_overview(
    entries: Iterable[tuple[str, float]],
    display_tz: tzinfo,
    today: date | None = None,
) -> LiftOverview | None:
    """Summarise consistency for one lift from raw timestamp/weight rows."""
    entry_list = list(entries)
    points = daily_best_points(entry_list, display_tz)
    if not points:
        return None

    local_today = today or datetime.now(display_tz).date()
    days = [point.when.date() for point in points]
    weights = [point.weight_kg for point in points]
    gaps = [(days[i] - days[i - 1]).days for i in range(1, len(days))]

    first_day = days[0]
    latest_day = days[-1]
    total_weeks = _total_weeks_between(first_day, latest_day)
    active_weeks = len({_week_key(day) for day in days})
    days_since_latest = max(0, (local_today - latest_day).days)
    coverage = active_weeks / total_weeks if total_weeks > 0 else 1.0

    if days_since_latest <= 7:
        recency = 1.0
    elif days_since_latest <= 14:
        recency = 0.75
    elif days_since_latest <= 30:
        recency = 0.5
    else:
        recency = max(0.0, 1.0 - (days_since_latest / 90.0))

    streak = _current_week_streak(days, local_today)
    streak_score = min(streak / 4.0, 1.0)
    consistency_score = round(
        (coverage * 0.55 + recency * 0.25 + streak_score * 0.20) * 100
    )

    return LiftOverview(
        total_logs=len(entry_list),
        active_days=len(points),
        active_weeks=active_weeks,
        total_weeks=total_weeks,
        consistency_score=max(0, min(100, consistency_score)),
        current_week_streak=streak,
        best_kg=max(weights),
        first_kg=weights[0],
        latest_kg=weights[-1],
        improvement_kg=weights[-1] - weights[0],
        first_day=first_day,
        latest_day=latest_day,
        days_since_latest=days_since_latest,
        avg_gap_days=(sum(gaps) / len(gaps)) if gaps else None,
        longest_gap_days=max(gaps) if gaps else None,
        logs_last_30_days=sum(
            1 for logged_at, _weight in entry_list
            if _logged_at_in_last_days(logged_at, display_tz, local_today, 30)
        ),
    )


def _logged_at_in_last_days(
    logged_at: str,
    display_tz: tzinfo,
    today: date,
    days: int,
) -> bool:
    try:
        dt = datetime.fromisoformat(logged_at)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_day = dt.astimezone(display_tz).date()
    return 0 <= (today - local_day).days <= days