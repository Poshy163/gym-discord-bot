"""Pure helper functions for training analytics.

Everything here is deliberately I/O-free so it can be unit-tested without a
database or Discord client. Each function takes plain Python values and
returns plain Python values.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable


# -- Plate calculator -------------------------------------------------------

# Standard kilo plate stack found in most commercial gyms. Ordered largest
# first so the greedy algorithm picks the heaviest plate that still fits.
DEFAULT_KG_PLATES: tuple[float, ...] = (
    25.0, 20.0, 15.0, 10.0, 5.0, 2.5, 1.25,
)
DEFAULT_BAR_KG: float = 20.0


def plate_breakdown(
    target_kg: float,
    bar_kg: float = DEFAULT_BAR_KG,
    plates: Iterable[float] = DEFAULT_KG_PLATES,
) -> tuple[list[tuple[float, int]], float]:
    """Greedy plate breakdown for one side of a barbell.

    Returns ``(per_side_pairs, leftover_kg)`` where ``per_side_pairs`` is a
    list of ``(plate_weight, count_per_side)`` and ``leftover_kg`` is the
    residual that couldn't be matched (always >= 0). The total target
    weight equals ``bar_kg + 2 * sum(p * n) + leftover_kg``.

    A target lighter than the bar returns an empty list and reports the
    deficit as a negative leftover so the caller can warn about it.
    """
    if target_kg < bar_kg:
        return [], target_kg - bar_kg
    per_side_target = (target_kg - bar_kg) / 2.0
    remaining = per_side_target
    breakdown: list[tuple[float, int]] = []
    # Sort defensively — caller might pass plates in any order.
    for plate in sorted(plates, reverse=True):
        if plate <= 0:
            continue
        # Use integer division on a scaled value to avoid float drift on
        # 0.25kg-aligned plates (the smallest standard increment).
        count = int(round(remaining * 100)) // int(round(plate * 100))
        if count > 0:
            breakdown.append((plate, count))
            remaining -= count * plate
    # Round the leftover to drop sub-gram float noise.
    leftover = round(remaining * 2, 3)
    return breakdown, leftover


# -- Streaks ----------------------------------------------------------------

def daily_streak(
    log_dates: Iterable[date], today: date,
) -> tuple[int, int]:
    """Compute (current_daily_streak, longest_daily_streak) from a set of
    distinct training dates.

    ``current_daily_streak`` counts back from ``today`` (or yesterday, so a
    user who hasn't logged yet today doesn't break their streak). It hits
    0 only after a full day off the wagon.
    """
    days = {d for d in log_dates}
    if not days:
        return 0, 0

    # Current streak: walk back from today; allow one missed day (today
    # itself) so the streak doesn't reset before the user has trained today.
    current = 0
    cursor = today
    if cursor not in days:
        cursor -= timedelta(days=1)
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)

    # Longest streak: scan sorted dates and track the longest consecutive run.
    longest = 0
    run = 0
    prev: date | None = None
    for d in sorted(days):
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        if run > longest:
            longest = run
        prev = d
    return current, longest


def weekly_streak(
    log_dates: Iterable[date], today: date,
) -> tuple[int, int]:
    """Compute (current_week_streak, longest_week_streak) using ISO weeks.

    A "week" counts if the user logged at least one lift between Monday and
    Sunday of that ISO week. Current streak walks back from this week and
    tolerates the current week being empty (so Monday-morning users don't
    appear to have lost their streak).
    """
    weeks = {(d.isocalendar().year, d.isocalendar().week) for d in log_dates}
    if not weeks:
        return 0, 0

    def _prev_week(y: int, w: int) -> tuple[int, int]:
        # Step back 7 days from any date in (y, w) and re-derive the ISO
        # tuple — handles year boundaries (week 1 -> week 52/53) correctly.
        anchor = date.fromisocalendar(y, w, 1) - timedelta(days=7)
        iso = anchor.isocalendar()
        return iso.year, iso.week

    iso_today = today.isocalendar()
    current = 0
    cursor = (iso_today.year, iso_today.week)
    if cursor not in weeks:
        cursor = _prev_week(*cursor)
    while cursor in weeks:
        current += 1
        cursor = _prev_week(*cursor)

    longest = 0
    run = 0
    prev: tuple[int, int] | None = None
    for w in sorted(weeks):
        if prev is not None and _prev_week(*w) == prev:
            run += 1
        else:
            run = 1
        if run > longest:
            longest = run
        prev = w
    return current, longest


# -- Goal projection --------------------------------------------------------

def project_goal_eta(
    history: list[tuple[datetime, float]], target_kg: float, today: datetime,
) -> tuple[float | None, date | None, str]:
    """Estimate when a lifter will hit ``target_kg`` for one lift.

    ``history`` is a list of ``(timestamp, weight)`` tuples in any order.
    Returns ``(kg_per_week, eta_date, reason)``:

    * ``kg_per_week`` — average weekly gain (latest-first / weeks elapsed),
      or None if it can't be computed.
    * ``eta_date`` — projected date the goal is hit, or None.
    * ``reason`` — short human message explaining edge cases ("already hit",
      "not enough data", "no progress", or empty string on success).

    Uses a simple endpoint-to-endpoint slope rather than a regression so
    it stays predictable for a small number of points (typical case).
    """
    if not history:
        return None, None, "no history yet for this lift"
    history = sorted(history, key=lambda r: r[0])
    first_ts, first_kg = history[0]
    latest_ts, latest_kg = history[-1]
    if latest_kg >= target_kg:
        return None, None, "already at or above target"
    if len(history) < 2 or latest_ts <= first_ts:
        return None, None, "need at least two dated entries to project"
    weeks_elapsed = (latest_ts - first_ts).total_seconds() / (7 * 86400)
    if weeks_elapsed <= 0:
        return None, None, "need at least two dated entries to project"
    gain = latest_kg - first_kg
    rate = gain / weeks_elapsed
    if rate <= 0:
        return rate, None, "no upward progress yet — can't project"
    weeks_needed = (target_kg - latest_kg) / rate
    eta = (today + timedelta(weeks=weeks_needed)).date()
    return rate, eta, ""


def project_bodyweight_eta(
    history: list[tuple[datetime, float]], target_kg: float, today: datetime,
) -> tuple[float | None, date | None, str]:
    """Estimate when a user's bodyweight will reach ``target_kg``.

    Unlike :func:`project_goal_eta` (lifts only go up), a bodyweight goal can
    sit above OR below the current weight — cutting and bulking both project.
    Uses a least-squares slope over the weigh-ins rather than endpoints
    because daily bodyweight is noisy (water, food in transit).

    Returns ``(kg_per_week, eta_date, reason)`` with the same contract as
    :func:`project_goal_eta`: rate/eta None plus a short human reason when a
    projection isn't possible, empty reason on success. The rate is signed
    (negative = losing).
    """
    if not history:
        return None, None, "no weigh-ins yet — log one with `/bodyweight`"
    history = sorted(history, key=lambda r: r[0])
    latest_ts, latest_kg = history[-1]
    if abs(latest_kg - target_kg) < 0.05:
        return None, None, "already at target"
    if len(history) < 2 or latest_ts <= history[0][0]:
        return None, None, "need at least two dated weigh-ins to project"

    # Least-squares slope in kg/day (same shape as tdee._linreg_slope_per_day,
    # duplicated to keep both modules dependency-free and independently pure).
    t0 = history[0][0]
    xs = [(ts - t0).total_seconds() / 86400.0 for ts, _ in history]
    ys = [kg for _, kg in history]
    n = len(history)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None, None, "need at least two dated weigh-ins to project"
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    rate = slope * 7.0

    needed = target_kg - latest_kg   # signed: negative when cutting
    if rate == 0 or (needed > 0) != (rate > 0):
        direction = "down" if needed < 0 else "up"
        return rate, None, f"weight isn't trending {direction} yet — can't project"
    weeks_needed = needed / rate
    # A projection years out is noise, not a plan.
    if weeks_needed > 104:
        return rate, None, "at the current rate that's over two years away"
    eta = (today + timedelta(weeks=weeks_needed)).date()
    return rate, eta, ""
