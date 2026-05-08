"""Tests for the chat-message date hint helper that powers backdated logging.

`_resolve_date_hint` reads natural phrases ("yesterday", "monday",
"3 days ago", "2026-05-04") out of a message and returns a UTC datetime
anchored at noon on the resolved date in the bot's display timezone.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Pin display tz so tests are deterministic regardless of host config.
os.environ.setdefault("DISPLAY_TIMEZONE", "Australia/Adelaide")
os.environ.setdefault("PLATE_KG", "20")

from app.bot import DISPLAY_TZ, _resolve_date_hint  # noqa: E402


def _now() -> datetime:
    # A fixed reference: Wednesday 2026-05-06 09:00 local. Using a
    # weekday in the middle of the week so weekday matching can step
    # both forwards (interpreted as last week) and stay on today.
    return datetime(2026, 5, 6, 9, 0, tzinfo=DISPLAY_TZ)


def _local_date(text: str):
    out = _resolve_date_hint(text, _now())
    assert out is not None, text
    assert out.tzinfo == timezone.utc
    return out.astimezone(DISPLAY_TZ).date()


def test_no_hint_returns_none():
    assert _resolve_date_hint("bench 100kg", _now()) is None
    assert _resolve_date_hint("", _now()) is None


def test_yesterday():
    d = _local_date("bench 90kg yesterday")
    assert d.isoformat() == "2026-05-05"


def test_today_and_tonight():
    assert _local_date("squat 100kg today").isoformat() == "2026-05-06"
    assert _local_date("hit 5x5 tonight").isoformat() == "2026-05-06"


def test_n_days_ago():
    assert _local_date("ohp 60kg 3 days ago").isoformat() == "2026-05-03"
    assert _local_date("did 40kg 1 day ago").isoformat() == "2026-05-05"


def test_n_days_ago_caps_out_of_range():
    # The helper rejects > 30 days to avoid hijacking unrelated numbers.
    assert _resolve_date_hint("60 days ago bench", _now()) is None


def test_weekday_in_past_resolves_to_most_recent():
    # Today is Wednesday — "monday" => 2 days back.
    assert _local_date("monday: bench 100kg").isoformat() == "2026-05-04"
    # Sunday => 3 days back.
    assert _local_date("did legs sunday").isoformat() == "2026-05-03"


def test_weekday_today_keeps_today():
    # Wednesday on a Wednesday should be "today" not "a week ago".
    assert _local_date("bench 100kg wednesday").isoformat() == "2026-05-06"


def test_iso_date():
    assert _local_date("bench 100kg 2026-05-01").isoformat() == "2026-05-01"


def test_iso_far_future_rejected():
    # 99 years in the future is almost certainly a typo, not a real date.
    assert _resolve_date_hint("squat 100kg 2099-01-01", _now()) is None


def test_iso_invalid_date_rejected():
    # 2026-02-30 isn't a real date — must not crash, must not match.
    assert _resolve_date_hint("squat 100kg 2026-02-30", _now()) is None


def test_anchored_at_noon_local():
    # Noon local avoids midnight-vs-DST ambiguity. Confirm the time.
    out = _resolve_date_hint("bench yesterday", _now())
    assert out is not None
    local = out.astimezone(DISPLAY_TZ)
    assert local.hour == 12 and local.minute == 0
