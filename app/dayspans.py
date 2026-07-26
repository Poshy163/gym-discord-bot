"""Local-day windows and logging streaks, computed from the database alone.

These moved out of ``app/bot.py`` because both the bot and the supervisor need
them and neither needs Discord to compute them. That matters concretely: the
dashboard calls ``today_window`` and the two streak helpers **synchronously**
(app/webui.py:211, :430-431), so they cannot be proxied to the bot process over
async RPC -- doing so would put coroutine objects into ``json_response``. The
supervisor already holds the same SQLite file, so it just calls these directly,
which also keeps the member page working while the bot is down.

Everything here takes its timezone explicitly rather than reading a module
global, so a timezone change applies on the next call in both processes.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable

#: How far back we scan when computing a logging streak. A run longer than this
#: is rare and still renders (capped at the window); keeps the query cheap.
STREAK_WINDOW_DAYS = 60


def today_window(tz: Any) -> tuple[str, str]:
    """UTC ISO bounds of the current local day in ``tz``."""
    today = datetime.now(tz).date()
    start_local = datetime.combine(today, dtime.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def streak_window(tz: Any) -> tuple[date, str, str]:
    """``(today_local, start_iso, end_iso)`` covering the streak look-back."""
    today = datetime.now(tz).date()
    start = datetime.combine(
        today - timedelta(days=STREAK_WINDOW_DAYS), dtime.min, tzinfo=tz,
    )
    end = datetime.combine(today + timedelta(days=1), dtime.min, tzinfo=tz)
    return (
        today,
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
    )


def entry_local_days(entries: Iterable[Any], tz: Any) -> set[date]:
    """Set of local calendar dates an entry list touches.

    Buckets by *local* day (not the UTC date prefix) so streaks line up with the
    user's own calendar -- important in +HH:MM zones where a morning meal is the
    previous UTC day.
    """
    out: set[date] = set()
    for r in entries:
        try:
            dt = datetime.fromisoformat(r["logged_at"])
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.add(dt.astimezone(tz).date())
    return out


def logging_streak(days: set[date], today: date) -> int:
    """Consecutive local days with an entry, ending today *or* yesterday.

    Anchoring at either keeps the streak "alive" the morning after -- it only
    resets once a whole day passes with nothing logged. Pure + testable.
    """
    if today in days:
        cursor = today
    elif (today - timedelta(days=1)) in days:
        cursor = today - timedelta(days=1)
    else:
        return 0
    n = 0
    while cursor in days:
        n += 1
        cursor -= timedelta(days=1)
    return n


def calorie_streak(db: Any, user_id: int, tz: Any) -> int:
    """Current consecutive-day calorie-logging streak for ``user_id`` (global)."""
    today, start_iso, end_iso = streak_window(tz)
    days = entry_local_days(
        db.calorie_entries_between(0, user_id, start_iso, end_iso), tz,
    )
    return logging_streak(days, today)


def protein_streak(db: Any, user_id: int, tz: Any) -> int:
    """Current consecutive-day protein-logging streak for ``user_id`` (global)."""
    today, start_iso, end_iso = streak_window(tz)
    days = entry_local_days(
        db.protein_entries_between(0, user_id, start_iso, end_iso), tz,
    )
    return logging_streak(days, today)
