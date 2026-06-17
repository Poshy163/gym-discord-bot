"""Tests for small pure helpers inside app.bot.

We avoid importing discord runtime state by only touching pure functions.
"""

from __future__ import annotations

import os

import discord

# Ensure the bot doesn't try to connect on import — DISCORD_TOKEN isn't
# read until run() so we mainly need a stable DB path.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app.bot import (  # noqa: E402
    _get_main_activity,
    _local_log_dates,
    _parse_bodyweight_message,
    _rejected_lifts_note,
    _render_revo_calendar,
    _safe_label,
    _true_weight_kg,
    _true_weight_suffix,
    db as _bot_db,
)
from app.parser import Lift  # noqa: E402


def test_safe_label_escapes_mentions():
    out = _safe_label("@everyone bench")
    assert "@everyone" not in out
    # discord.utils.escape_mentions inserts a zero-width space between '@'
    # and the keyword; the literal trigger string must not survive.


def test_safe_label_escapes_markdown():
    out = _safe_label("**bench** _press_")
    # Asterisks/underscores should be backslash-escaped so they don't bold.
    assert "\\*" in out
    assert "\\_" in out


def test_safe_label_truncates_long_input():
    out = _safe_label("a" * 500, limit=20)
    assert len(out) <= 20
    assert out.endswith("…")


def test_safe_label_handles_empty():
    assert _safe_label("") == "(unknown)"
    assert _safe_label("   ") == "(unknown)"


def test_safe_label_strips_newlines():
    out = _safe_label("bench\npress")
    assert "\n" not in out


def test_rejected_lifts_note_sanitizes_equipment():
    rejected = [
        Lift(
            equipment="@everyone bench **boom**",
            weight_kg=9999.0,
            raw="@everyone bench **boom**: 9999kg",
            confident=True,
        ),
    ]
    note = _rejected_lifts_note(rejected)
    assert "@everyone" not in note
    # Markdown bold from the label must be escaped, but our own ** wrapper
    # around the label remains.
    assert "\\*\\*boom\\*\\*" in note


def test_rejected_lifts_note_empty_returns_blank():
    assert _rejected_lifts_note([]) == ""


def test_render_revo_calendar_keeps_header_and_emoji_columns_aligned():
    attended = {8: True, 11: True, 12: True, 14: True, 15: True}
    assert _render_revo_calendar(5, 2026, attended) == (
        "```\n"
        "Mo  Tu  We  Th  Fr  Sa  Su\n"
        "⬛  ⬛  ⬛  ⬛  ⬜  ⬜  ⬜\n"
        "⬜  ⬜  ⬜  ⬜  🔥  ⬜  ⬜\n"
        "🔥  🔥  ⬜  🔥  🔥  ⬜  ⬜\n"
        "⬜  ⬜  ⬜  ⬜  ⬜  ⬜  ⬜\n"
        "⬜  ⬜  ⬜  ⬜  ⬜  ⬜  ⬜\n"
        "```"
    )


def test_get_main_activity_recognizes_discord_game():
    class MemberStub:
        activities = [discord.Game("Rust")]

    assert _get_main_activity(MemberStub()) == "Rust"


# --- True-weight helper ---------------------------------------------------

def test_true_weight_assisted_pull_up_subtracts_assistance():
    # 100kg lifter on assisted pull-up machine set to 70kg of help.
    assert _true_weight_kg("pull ups", 70, False, 100) == 30
    assert _true_weight_suffix("pull ups", 70, False, 100) == " (true: 30kg)"


def test_true_weight_weighted_dip_adds_to_bodyweight():
    # 100kg lifter doing BW+20kg dips → 120kg true load.
    assert _true_weight_kg("dips", 20, True, 100) == 120
    assert _true_weight_suffix("dips", 20, True, 100) == " (true: 120kg)"


def test_true_weight_no_bodyweight_returns_none():
    assert _true_weight_kg("pull ups", 70, False, None) is None
    assert _true_weight_suffix("pull ups", 70, False, None) == ""


def test_true_weight_skipped_for_non_bw_equipment():
    # Bench press isn't bodyweight-relative, so no true-weight annotation.
    assert _true_weight_kg("bench press", 80, False, 100) is None
    assert _true_weight_suffix("bench press", 80, False, 100) == ""


def test_true_weight_clamps_over_assistance_to_none():
    # Assistance >= bodyweight would yield <=0 kg lifted, which is nonsense
    # to display. The helper returns None so the suffix stays empty.
    assert _true_weight_kg("pull ups", 120, False, 100) is None
    assert _true_weight_suffix("pull ups", 120, False, 100) == ""


# --- Chat-message bodyweight parser --------------------------------------

def test_parse_bodyweight_message_variants():
    assert _parse_bodyweight_message("bodyweight 100kg") == 100.0
    assert _parse_bodyweight_message("body weight: 95.5kg") == 95.5
    assert _parse_bodyweight_message("BW 80") == 80.0
    assert _parse_bodyweight_message("  bodyweight - 72.3 ") == 72.3
    assert _parse_bodyweight_message("bodyweight 100kg.") == 100.0


def test_parse_bodyweight_message_rejects_non_bodyweight():
    # No number, or it's a lift line, or bodyweight is mentioned mid-sentence.
    assert _parse_bodyweight_message("bodyweight") is None
    assert _parse_bodyweight_message("squat 100kg") is None
    assert _parse_bodyweight_message("bench 80kg bodyweight 100") is None
    assert _parse_bodyweight_message("") is None
    assert _parse_bodyweight_message("BW") is None


# --- Streak date bucketing (timezone regression) -------------------------

def test_local_log_dates_buckets_in_display_timezone():
    """An early-morning session in a +HH:MM tz must count on the local day,
    not slip into the previous UTC day (the old substr-based bug)."""
    from datetime import date, datetime

    from app.bot import DISPLAY_TZ

    guild_id, user_id = 970001, 424242
    # 08:30 on the 17th, local time. In Adelaide (UTC+9:30) this is 23:00 on
    # the 16th UTC — exactly the case the UTC date prefix mis-bucketed.
    local_dt = datetime(2026, 6, 17, 8, 30, tzinfo=DISPLAY_TZ)
    _bot_db.add_lifts(
        guild_id=guild_id,
        user_id=user_id,
        username="tzuser",
        lifts=[Lift(equipment="bench press", weight_kg=80.0, raw="bench 80kg")],
        logged_at=local_dt,
    )
    assert _local_log_dates(guild_id, user_id) == [date(2026, 6, 17)]


# --- Revo attendance-streak date collection -------------------------------

class _FakeRevoClient:
    """Stub for RevoClient.get_streak_calendar driven by a canned mapping of
    (month, year) -> {day: attended}. Records the months fetched."""

    def __init__(self, months):
        self._months = months
        self.calls = []

    def get_streak_calendar(self, m, y):
        self.calls.append((m, y))
        return self._months.get((m, y), {})


def test_revo_attended_dates_crosses_month_boundary():
    from datetime import date, datetime

    from app.bot import DISPLAY_TZ, _revo_attended_dates

    client = _FakeRevoClient({
        # June: attended 1-3, and day 1 is attended → look back into May.
        (6, 2026): {1: True, 2: True, 3: True},
        # May: a run at the end; day 1 not attended → stop here.
        (5, 2026): {29: False, 30: True, 31: True},
    })
    now_local = datetime(2026, 6, 3, 10, 0, tzinfo=DISPLAY_TZ)
    attended = _revo_attended_dates(client, now_local)
    assert date(2026, 6, 1) in attended
    assert date(2026, 5, 31) in attended
    assert date(2026, 5, 29) not in attended  # not attended
    # Stopped after May (its day 1 wasn't attended): April never fetched.
    assert (4, 2026) not in client.calls


def test_revo_attended_dates_always_fetches_previous_month():
    from datetime import datetime

    from app.bot import DISPLAY_TZ, _revo_attended_dates

    client = _FakeRevoClient({})  # nothing attended anywhere
    now_local = datetime(2026, 6, 15, 9, 0, tzinfo=DISPLAY_TZ)
    attended = _revo_attended_dates(client, now_local)
    assert attended == set()
    # Current + previous month, then stop (no boundary to cross).
    assert client.calls == [(6, 2026), (5, 2026)]
