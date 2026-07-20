"""Tests for small pure helpers inside app.bot.

We avoid importing discord runtime state by only touching pure functions.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

# Ensure the bot doesn't try to connect on import — DISCORD_TOKEN isn't
# read until run() so we mainly need a stable DB path.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app.bot import (  # noqa: E402
    _backdate_label,
    _build_progress_payload,
    _day_window_for,
    _e1rm_progression,
    _get_main_activity,
    _parse_recap_json,
    _local_log_dates,
    _looks_like_log_attempt,
    _parse_bodyweight_message,
    _rejected_lifts_note,
    _render_revo_calendar,
    _safe_label,
    _split_date_hint,
    _target_in_channel,
    _today_window,
    _true_weight_kg,
    _true_weight_suffix,
    _zero_quip,
    _ZERO_CALORIE_QUIPS,
    _ZERO_PROTEIN_QUIPS,
    DISPLAY_TZ,
    _calorie_status_for,
    _reply_label,
    db as _bot_db,
)
from app.parser import Lift  # noqa: E402


def test_looks_like_log_attempt_detects_shortcuts():
    # Each freeform logging shortcut should be recognised as a log attempt.
    assert _looks_like_log_attempt("bodyweight 80kg") is True
    assert _looks_like_log_attempt("650kcal") is True
    assert _looks_like_log_attempt("40g protein") is True
    assert _looks_like_log_attempt("bench 100kg") is True


def test_looks_like_log_attempt_ignores_casual_text():
    assert _looks_like_log_attempt("") is False
    assert _looks_like_log_attempt("hey how's it going") is False
    assert _looks_like_log_attempt("gym was good today") is False


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


def test_zero_quip_picks_from_correct_pool():
    # Protein quips for protein, calorie quips otherwise — both non-empty.
    assert _zero_quip("protein") in _ZERO_PROTEIN_QUIPS
    assert _zero_quip("calories") in _ZERO_CALORIE_QUIPS
    assert _zero_quip("anything-else") in _ZERO_CALORIE_QUIPS
    # The pools are distinct so the joke matches the macro.
    assert set(_ZERO_PROTEIN_QUIPS).isdisjoint(_ZERO_CALORIE_QUIPS)


def test_build_progress_payload_is_json_serializable():
    import json

    from app.parser import Lift as _L

    _bot_db.add_lifts(99, 7, "Sam", [_L("bench", 100), _L("squat", 140)])
    _bot_db.calorie_goal_set(99, 7, "Sam", 2400)
    _bot_db.calorie_add(99, 7, "Sam", 600)
    _bot_db.protein_goal_set(99, 7, "Sam", 190)
    _bot_db.set_bodyweight(99, 7, 84.0)
    _bot_db.goal_set(99, 7, "bench", 120, False)

    payload = _build_progress_payload(99, 7, "Sam", 30)
    # Must serialize cleanly for the Gemini prompt.
    blob = json.dumps(payload, default=str)
    assert "lifting" in payload and "nutrition" in payload
    assert payload["nutrition"]["calorie_goal_kcal"] == 2400
    assert payload["bodyweight"]["latest_kg"] == 84.0
    assert any(g["equipment"] == "bench" for g in payload["lifting"]["goals"])
    assert len(blob) > 0


def _rep_row(equipment, weight_kg, reps, at):
    return {"equipment": equipment, "weight_kg": weight_kg, "reps": reps, "logged_at": at}


def test_e1rm_progression_detects_rep_gains_at_same_weight():
    # Same top-set weight, more reps over time → real estimated-1RM gain.
    rows = [
        _rep_row("bench", 100, 5, "2026-06-01T00:00:00+00:00"),
        _rep_row("bench", 100, 8, "2026-06-20T00:00:00+00:00"),
    ]
    out = _e1rm_progression(rows)
    assert len(out) == 1
    b = out[0]
    assert b["equipment"] == "bench"
    # Epley: 100*(1+5/30)=116.7 → 100*(1+8/30)=126.7, +10.
    assert b["first_e1rm_kg"] == 116.7
    assert b["latest_e1rm_kg"] == 126.7
    assert b["gain_kg"] == 10.0
    assert b["sets_counted"] == 2


def test_e1rm_progression_skips_unusable_and_sorts_by_gain():
    rows = [
        _rep_row("ohp", 50, 20, "2026-06-01T00:00:00+00:00"),   # >12 reps → skip
        _rep_row("squat", 140, 3, "2026-06-01T00:00:00+00:00"),
        _rep_row("squat", 150, 5, "2026-06-20T00:00:00+00:00"),
        _rep_row("curl", 20, 10, "2026-06-01T00:00:00+00:00"),
    ]
    out = _e1rm_progression(rows)
    names = [d["equipment"] for d in out]
    assert "ohp" not in names                 # all its sets were unusable
    assert names[0] == "squat"                # biggest gainer first
    assert out[0]["gain_kg"] > 0


def test_parse_recap_json_variants():
    assert _parse_recap_json('{"verdict":"Nice","tip":"Add protein"}') == (
        "Nice", "Add protein"
    )
    # Code-fenced.
    assert _parse_recap_json('```json\n{"verdict":"v","tip":"t"}\n```') == ("v", "t")
    # Prose-wrapped JSON.
    assert _parse_recap_json('Sure: {"verdict":"v2","tip":"t2"} !') == ("v2", "t2")
    # Missing tip is fine.
    assert _parse_recap_json('{"verdict":"only verdict"}') == ("only verdict", None)
    # No JSON at all → whole thing becomes the verdict.
    v, t = _parse_recap_json("totally not json")
    assert v == "totally not json" and t is None
    # Empty input.
    assert _parse_recap_json("") == (None, None)


# --- _target_in_channel: per-channel privacy guard for "look up someone" -----

def _fake_interaction(*, user_id=111, guild_id=1, channel=None):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = guild_id
    interaction.channel = channel
    return interaction


def _member_who_can_see(can_see: bool):
    # MagicMock(spec=discord.Member) passes isinstance(x, discord.Member), so the
    # helper treats it as a real Member and reads channel.permissions_for(member).
    member = MagicMock(spec=discord.Member)
    member.id = 222
    return member


def _channel_granting(view: bool):
    channel = MagicMock()
    perms = MagicMock()
    perms.view_channel = view
    channel.permissions_for.return_value = perms
    return channel


def test_target_in_channel_allows_self_lookup():
    # Looking yourself up never needs a channel check.
    interaction = _fake_interaction(user_id=111, channel=_channel_granting(False))
    me = MagicMock(spec=discord.Member)
    me.id = 111
    assert asyncio.run(_target_in_channel(interaction, me)) is True


def test_target_in_channel_allows_when_no_channel_context():
    # DM / unknown channel: nothing to enforce, defer to the share-a-server guard.
    interaction = _fake_interaction(guild_id=None, channel=None)
    other = _member_who_can_see(False)
    assert asyncio.run(_target_in_channel(interaction, other)) is True


def test_target_in_channel_allows_member_who_can_view():
    interaction = _fake_interaction(channel=_channel_granting(True))
    other = _member_who_can_see(True)
    assert asyncio.run(_target_in_channel(interaction, other)) is True


def test_target_in_channel_blocks_member_who_cannot_view():
    interaction = _fake_interaction(channel=_channel_granting(False))
    other = _member_who_can_see(False)
    assert asyncio.run(_target_in_channel(interaction, other)) is False


# --- backdating nutrition logs: _split_date_hint / windows / labels ----------

def test_split_date_hint_strips_trailing_yesterday():
    now = datetime(2026, 6, 30, 9, 0, tzinfo=DISPLAY_TZ)  # Tuesday
    dt, text = _split_date_hint("500c yesterday", now)
    assert text == "500c"
    assert dt is not None
    assert dt.astimezone(DISPLAY_TZ).date() == date(2026, 6, 29)


def test_split_date_hint_no_hint_returns_unchanged():
    now = datetime(2026, 6, 30, 9, 0, tzinfo=DISPLAY_TZ)
    assert _split_date_hint("coffee", now) == (None, "coffee")
    assert _split_date_hint("500c", now) == (None, "500c")


def test_split_date_hint_days_ago_and_weekday():
    now = datetime(2026, 6, 30, 9, 0, tzinfo=DISPLAY_TZ)  # Tuesday
    dt, text = _split_date_hint("40g protein 3 days ago", now)
    assert text == "40g protein"
    assert dt.astimezone(DISPLAY_TZ).date() == date(2026, 6, 27)
    dt2, text2 = _split_date_hint("500c monday", now)
    assert text2 == "500c"
    assert dt2.astimezone(DISPLAY_TZ).date() == date(2026, 6, 29)  # this Monday


def test_split_date_hint_keeps_weekday_in_food_name():
    # "yesterday" wins over the weekday word, so a food called "sunday roast"
    # survives intact while still being backdated.
    now = datetime(2026, 6, 30, 9, 0, tzinfo=DISPLAY_TZ)
    dt, text = _split_date_hint("sunday roast yesterday", now)
    assert text == "sunday roast"
    assert dt.astimezone(DISPLAY_TZ).date() == date(2026, 6, 29)


def test_day_window_for_none_matches_today():
    assert _day_window_for(None) == _today_window()


def test_day_window_for_brackets_the_day():
    dt = datetime(2026, 6, 29, 15, 0, tzinfo=timezone.utc)
    start, end = _day_window_for(dt)
    assert start <= dt.isoformat() < end
    span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    assert span == timedelta(days=1)


def test_backdate_label_today_and_none_are_empty():
    assert _backdate_label(None) == ""
    assert _backdate_label(datetime.now(timezone.utc)) == ""


def test_backdate_label_for_old_day():
    old = datetime.now(timezone.utc) - timedelta(days=5)
    assert "logged for" in _backdate_label(old)


# --- per-guild gym-channel gate ---------------------------------------------

def test_guild_has_gym_channel_scopes_allow_list_per_guild(monkeypatch):
    import app.bot as bot

    monkeypatch.setattr(bot, "GYM_CHANNEL_IDS", {111, 222})
    # A guild that contains a listed channel is "restricted" (gate applies).
    g_listed = MagicMock()
    g_listed.get_channel = lambda cid: object() if cid == 111 else None
    assert bot._guild_has_gym_channel(g_listed) is True
    # A guild with none of the listed channels is scanned in full.
    g_other = MagicMock()
    g_other.get_channel = lambda cid: None
    assert bot._guild_has_gym_channel(g_other) is False
    # No guild, and an empty allow-list, are both False.
    assert bot._guild_has_gym_channel(None) is False
    monkeypatch.setattr(bot, "GYM_CHANNEL_IDS", set())
    assert bot._guild_has_gym_channel(g_listed) is False


# --- logging streaks ---------------------------------------------------------

def test_logging_streak_pure():
    from app.bot import _logging_streak
    today = date(2026, 6, 30)
    # Three consecutive days ending today.
    assert _logging_streak({date(2026, 6, 30), date(2026, 6, 29), date(2026, 6, 28)}, today) == 3
    # Anchored at yesterday (nothing logged today yet) — still alive.
    assert _logging_streak({date(2026, 6, 29), date(2026, 6, 28)}, today) == 2
    # A gap before today/yesterday means no current streak.
    assert _logging_streak({date(2026, 6, 27)}, today) == 0
    assert _logging_streak(set(), today) == 0


def test_calorie_and_protein_streak_count_local_days():
    from app.bot import _calorie_streak, _protein_streak, DISPLAY_TZ as TZ
    uid = 515151
    today = datetime.now(TZ).date()
    for off in (0, 1, 2):
        d = today - timedelta(days=off)
        noon_utc = datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ).astimezone(timezone.utc)
        _bot_db.calorie_add(0, uid, "x", 100, logged_at=noon_utc)
        _bot_db.protein_add(0, uid, "x", 30, logged_at=noon_utc)
    assert _calorie_streak(uid) == 3
    assert _protein_streak(uid) == 3
    # A user who logged only 3 days ago has no current streak.
    uid2 = 515152
    d = today - timedelta(days=3)
    noon_utc = datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ).astimezone(timezone.utc)
    _bot_db.calorie_add(0, uid2, "x", 100, logged_at=noon_utc)
    assert _calorie_streak(uid2) == 0


def test_protein_weekly_blocks_flags_over_max():
    from app.bot import _protein_weekly_blocks
    g, uid = 880001, 880100
    _bot_db.upsert_member(g, uid, "alice", "Alice")
    _bot_db.protein_goal_set(g, uid, "Alice", 180)
    now = datetime.now(timezone.utc)
    _bot_db.protein_add(g, uid, "Alice", 220, logged_at=now)  # over the 180 max
    start = (now - timedelta(days=3)).isoformat()
    end = (now + timedelta(days=1)).isoformat()
    blocks = _protein_weekly_blocks(g, start, end)
    assert any("Alice" in b for b in blocks)
    assert any("over" in b for b in blocks)


# ---- weekday / weekend targets in log replies -------------------------------

def _at(day):
    """Noon on ``day``, in the display timezone, as a UTC timestamp."""
    local = datetime.combine(day, datetime.min.time(), tzinfo=DISPLAY_TZ)
    return local.replace(hour=12).astimezone(timezone.utc)


def test_backdated_entry_is_scored_against_its_own_days_target():
    from app import targets

    uid = 8801
    _bot_db.calorie_goal_set(1, uid, "Josh", 1500, 2200)
    today = targets.local_today()
    days = [today + timedelta(days=n) for n in range(7)]
    weekday = next(d for d in days if not targets.is_weekend(d))
    weekend = next(d for d in days if targets.is_weekend(d))

    # The same 1,200 kcal reads differently depending on the day it belongs to.
    assert "1,500 cal" in _calorie_status_for(uid, 1200, _at(weekday))
    assert "Using Weekday Targets" in _calorie_status_for(uid, 1200, _at(weekday))
    assert "2,200 cal" in _calorie_status_for(uid, 1200, _at(weekend))
    assert "Using Weekend Targets" in _calorie_status_for(uid, 1200, _at(weekend))


def test_single_target_user_sees_no_weekday_weekend_banner():
    uid = 8802
    _bot_db.calorie_goal_set(1, uid, "Old", 2000)
    today = datetime.now(DISPLAY_TZ).date()
    line = _calorie_status_for(uid, 900, _at(today))
    assert "2,000 cal" in line
    assert "Targets" not in line  # no "Using ... Targets" subtext at all


def test_backdating_into_a_tracking_gap_borrows_todays_target():
    # Stopped tracking yesterday, restarted today: an entry backdated into the
    # gap must not render "200 cal / 0 cal · 200 cal over target".
    from app import targets

    uid = 8803
    today = targets.local_today()
    yesterday = today - timedelta(days=1)
    _bot_db.calorie_goal_set(1, uid, "Josh", 1500)  # backdated to the epoch
    with _bot_db._conn() as c:  # noqa: SLF001 - hand-build the gap
        _bot_db._nutrition_target_write(  # noqa: SLF001
            c, 1, uid, "Josh", "kcal", "default", None, yesterday.isoformat(),
        )
        _bot_db._nutrition_target_write(  # noqa: SLF001
            c, 1, uid, "Josh", "kcal", "default", 1600, today.isoformat(),
        )

    assert _bot_db.nutrition_targets_on(uid, yesterday).kcal.value is None
    assert "1,600 cal" in _calorie_status_for(uid, 200, _at(yesterday))
    # A backdated day that *did* have a target still uses that one, not today's.
    assert "1,500 cal" in _calorie_status_for(uid, 200, _at(today - timedelta(days=2)))


def test_reply_banner_only_mentions_the_macros_the_reply_shows():
    # Flat calories, weekend protein override. A calorie-only reply must not
    # claim "Using Weekend Targets" when the calorie target never varies.
    from app import targets

    uid = 8804
    _bot_db.calorie_goal_set(1, uid, "Josh", 2000)
    _bot_db.protein_goal_set(1, uid, "Josh", 180, 200)
    today = targets.local_today()
    weekend = next(
        today + timedelta(days=n) for n in range(7)
        if targets.is_weekend(today + timedelta(days=n))
    )
    resolved = _bot_db.nutrition_targets_on(uid, weekend)
    assert resolved.label == "Using Weekend Targets"  # something splits
    assert _reply_label(resolved, calories=True, protein=False) is None
    assert _reply_label(resolved, calories=False, protein=True) == "Using Weekend Targets"
    assert _reply_label(resolved, calories=True, protein=True) == "Using Weekend Targets"


# ---- /busy home-club identity (linked caller's OWN landing wins) ------------

def test_busy_fav_landing_prefers_linked_callers_own_over_shared(monkeypatch):
    """Regression: a *linked* caller's /busy must use THEIR fav club + state,
    not the shared env account's. Preferring the shared landing first stamped
    every linked user with the shared owner's club/state — a WA-heavy shared
    account would then scope every SA/VIC/NSW caller's busiest board to WA.
    """
    import app.bot as bot
    from app.revo_client import RewardsLanding

    mine = RewardsLanding(fav_club_id=2, fav_club_name="Cannington", in_club=7)  # WA

    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: {"user_id": uid})
    monkeypatch.setattr(bot, "_client_for_user", lambda row: object())
    monkeypatch.setattr(bot.revo_client, "rewards_landing_with_client", lambda c: mine)
    # The shared landing must not even be consulted for a linked caller.
    monkeypatch.setattr(
        bot.revo_client, "shared_rewards_landing",
        lambda: pytest.fail("shared landing must not be used for a linked caller"),
    )

    landing = bot._busy_fav_landing(4242)
    assert landing is mine
    assert landing.fav_club_name == "Cannington"


def test_busy_fav_landing_falls_back_to_shared_when_unlinked(monkeypatch):
    """An unlinked caller (no stored account) still gets /busy via the shared
    env account — the documented normal deployment."""
    import app.bot as bot
    from app.revo_client import RewardsLanding

    shared = RewardsLanding(fav_club_id=1, fav_club_name="Marion", in_club=12)  # SA
    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: None)
    monkeypatch.setattr(bot.revo_client, "shared_rewards_landing", lambda: shared)

    landing = bot._busy_fav_landing(4242)
    assert landing is shared
    assert landing.fav_club_name == "Marion"


def test_busy_fav_landing_linked_but_empty_falls_back_to_shared(monkeypatch):
    """If a linked caller's own landing yields nothing usable (fav tile absent),
    fall through to the shared account rather than returning an empty landing."""
    import app.bot as bot
    from app.revo_client import RewardsLanding

    empty = RewardsLanding(fav_club_id=None, fav_club_name=None, in_club=None)
    shared = RewardsLanding(fav_club_id=1, fav_club_name="Marion", in_club=12)

    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: {"user_id": uid})
    monkeypatch.setattr(bot, "_client_for_user", lambda row: object())
    monkeypatch.setattr(bot.revo_client, "rewards_landing_with_client", lambda c: empty)
    monkeypatch.setattr(bot.revo_client, "shared_rewards_landing", lambda: shared)

    landing = bot._busy_fav_landing(4242)
    assert landing is shared


# ---- /revo_card credential resolution — OWN creds ONLY, never the shared -----
# account. This is the command's critical safety property: /revo_card surfaces
# the caller's physical entry BARCODE, so falling back to the shared REVO_USER
# account would hand one member the *host's* door barcode.

def _poison_shared_perfectgym(monkeypatch, bot):
    """Make every shared-account entry point fail the test if it is called."""
    import pytest as _pytest
    for name in (
        "shared_client_from_env",
        "shared_club_occupancy",
        "shared_club_list",
    ):
        monkeypatch.setattr(
            bot.revo_perfectgym, name,
            lambda *a, **k: _pytest.fail(
                f"/revo_card must NEVER touch the shared account ({name})"
            ),
        )


def test_revo_card_client_never_uses_shared_account(monkeypatch):
    """A linked caller resolves to THEIR OWN per-user client — and the shared
    REVO_USER account is never consulted on any path."""
    import app.bot as bot

    _poison_shared_perfectgym(monkeypatch, bot)
    sentinel = object()
    monkeypatch.setattr(
        bot.db, "get_revo_account",
        lambda uid: {"user_id": uid, "email": "me@example.com", "password_enc": "x"},
    )
    monkeypatch.setattr(bot, "_perfectgym_client_for_user", lambda row: sentinel)

    assert bot._revo_card_client_for_user(4242) is sentinel


def test_revo_card_client_refuses_unlinked_user(monkeypatch):
    """An unlinked caller gets None (→ the command refuses) and the shared account
    is NOT used as a fallback — even though one may be configured."""
    import app.bot as bot

    _poison_shared_perfectgym(monkeypatch, bot)
    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: None)
    # Building a per-user client must not even be attempted for an unlinked user.
    monkeypatch.setattr(
        bot, "_perfectgym_client_for_user",
        lambda row: pytest.fail("no per-user client for an unlinked caller"),
    )

    assert bot._revo_card_client_for_user(999) is None


def test_revo_card_acknowledges_when_credential_undecryptable(monkeypatch):
    """A REVO_FERNET_KEY rotation leaves the stored credential undecryptable, so
    resolving the per-user client raises RevoUnavailable BEFORE the interaction is
    deferred. The command must still ACKNOWLEDGE the interaction with an ephemeral
    error rather than let the exception propagate (which would leave Discord
    showing 'application did not respond') — and it must never reach defer/network."""
    import app.bot as bot

    monkeypatch.setattr(bot, "REVO_DISABLED", False)
    monkeypatch.setattr(bot.revo_perfectgym, "available", lambda: True)

    def _boom(_uid):
        raise bot.revo_client.RevoUnavailable("Stored Revo credential is unreadable.")

    monkeypatch.setattr(bot, "_revo_card_client_for_user", _boom)

    interaction = MagicMock()
    interaction.user.id = 4242
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()

    # Regression guard: if the resolution isn't wrapped, this asyncio.run re-raises.
    asyncio.run(bot.revo_card_cmd.callback(interaction))

    interaction.response.send_message.assert_awaited_once()
    # Ephemeral (only the caller sees it) and it never deferred / hit the executor.
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_awaited()


# ---- /revo_card barcode rendering — degrades without python-barcode ----------

def test_render_card_barcode_none_when_lib_missing(monkeypatch):
    """When python-barcode can't be imported, the renderer returns None so the
    command degrades to showing the number as text (bot still works)."""
    import app.bot as bot

    real_import = bot.importlib.import_module

    def _fake_import(name, *a, **k):
        if name.startswith("barcode"):
            raise ImportError("no barcode lib")
        return real_import(name, *a, **k)

    monkeypatch.setattr(bot.importlib, "import_module", _fake_import)
    assert bot._render_card_barcode("TEST-BARCODE-0001") is None


def test_render_card_barcode_never_logs_the_number(monkeypatch, caplog):
    """A render failure must log a bare message — NEVER the barcode value (it's a
    physical access credential)."""
    import logging as _logging
    import app.bot as bot

    real_import = bot.importlib.import_module

    class _BoomWriter:
        pass

    class _FakeBarcode:
        @staticmethod
        def get(symbology, number, writer=None):
            raise RuntimeError("render exploded")

    class _FakeWriterMod:
        ImageWriter = _BoomWriter

    def _fake_import(name, *a, **k):
        if name == "barcode":
            return _FakeBarcode
        if name == "barcode.writer":
            return _FakeWriterMod
        return real_import(name, *a, **k)

    monkeypatch.setattr(bot.importlib, "import_module", _fake_import)
    with caplog.at_level(_logging.WARNING):
        assert bot._render_card_barcode("TEST-BARCODE-0001") is None
    assert "TEST-BARCODE-0001" not in caplog.text  # number never hits the log


# ---- pure formatters for /revo_clubs + the membership status line ------------

def test_maps_link_requires_both_coords():
    import app.bot as bot

    assert bot._maps_link(None, 1.0) is None
    assert bot._maps_link(1.0, None) is None
    link = bot._maps_link(-34.829, 138.692)
    assert link == "https://www.google.com/maps/search/?api=1&query=-34.829,138.692"


def test_format_membership_status_line_cases():
    import app.bot as bot
    from app.revo_perfectgym import MembershipStatus as MS

    assert bot._format_membership_status_line(MS("Current", True, True)) == (
        "💳 Membership: Current"
    )
    # payment_ok False → the warning suffix is appended.
    assert bot._format_membership_status_line(MS("Suspended", False, True)) == (
        "💳 Membership: Suspended, payment issue ⚠"
    )
    # Unknown contract status (or None) → no line at all (degrade silently).
    assert bot._format_membership_status_line(MS(None, None, None)) is None
    assert bot._format_membership_status_line(None) is None


def test_summary_status_line_is_self_only():
    """The /revo_summary contract line is SELF-ONLY: it must never surface a third
    party's payment-failure / suspension flag in the command's PUBLIC reply."""
    import app.bot as bot
    from app.revo_perfectgym import MembershipStatus as MS

    suspended = MS("Suspended", False, True)
    # Looking yourself up → your own contract standing is shown.
    assert bot._summary_status_line(suspended, is_self=True) == (
        "💳 Membership: Suspended, payment issue ⚠"
    )
    # Looking someone else up → suppressed entirely (no third-party disclosure),
    # regardless of how bad their standing is.
    assert bot._summary_status_line(suspended, is_self=False) is None
    assert bot._summary_status_line(MS("Current", True, True), is_self=False) is None


def _dir(name, city, lat, lng, state, id_=None):
    from app.revo_perfectgym import ClubDirEntry
    return ClubDirEntry(
        id=id_, name=name, address=f"{name} Road", city=city,
        club_number=None, lat=lat, lng=lng, opening_date=None, state=state,
    )


def _occ(name, count, state="SA", suburb=None):
    from app.revo_perfectgym import ClubOccupancy
    return ClubOccupancy(
        name=name, suburb=suburb or name, state=state, count=count, capacity=None,
    )


def test_find_dir_entry_exact_prefix_substring_and_city():
    import app.bot as bot

    directory = [
        _dir("Modbury", "Modbury", -34.8, 138.6, "SA"),
        _dir("Marion", "Oaklands Park", -35.0, 138.5, "SA"),
    ]
    assert bot._find_dir_entry(directory, "modbury").name == "Modbury"  # exact
    assert bot._find_dir_entry(directory, "mar").name == "Marion"       # prefix
    assert bot._find_dir_entry(directory, "oaklands").name == "Marion"  # city substr
    assert bot._find_dir_entry(directory, "nope") is None
    assert bot._find_dir_entry(directory, "") is None


def test_format_revo_clubs_state_list_joins_counts_and_scopes_state():
    import app.bot as bot

    directory = [
        _dir("Modbury", "Modbury", -34.8, 138.6, "SA"),
        _dir("Marion", "Oaklands Park", -35.0, 138.5, "SA"),
        _dir("Cannington", "Cannington", -32.0, 115.9, "WA"),  # other state
    ]
    occupancy = [_occ("Modbury", 90), _occ("Marion", 40)]  # Cannington count absent

    out = bot._format_revo_clubs_state_list(directory, occupancy, "sa")
    assert "Revo clubs in SA" in out
    # Alphabetical; suburb shown only when it differs from the club name.
    assert "• **Marion** — Oaklands Park (40 in club now)" in out
    assert "• **Modbury** (90 in club now)" in out
    # A WA club is scoped out entirely.
    assert "Cannington" not in out


def test_format_revo_clubs_state_list_count_unavailable_when_board_down():
    import app.bot as bot

    directory = [_dir("Modbury", "Modbury", -34.8, 138.6, "SA")]
    out = bot._format_revo_clubs_state_list(directory, [], "SA")  # no occupancy
    assert "count unavailable" in out


def test_format_revo_club_detail_has_address_maps_count_and_nearest():
    import app.bot as bot
    from app import revo_perfectgym as pg

    directory = [
        _dir("Modbury", "Modbury", -34.829, 138.692, "SA", id_=25),
        _dir("Marion", "Oaklands Park", -35.0, 138.55, "SA", id_=2),
    ]
    occupancy = [_occ("Modbury", 90), _occ("Marion", 40)]
    entry = bot._find_dir_entry(directory, "modbury")
    nearest = pg.nearest_clubs(directory, entry.name, limit=3)

    out = bot._format_revo_club_detail(entry, occupancy, nearest)
    assert "**Modbury** — SA" in out
    assert "google.com/maps" in out
    assert "**90** in club right now" in out
    assert "Nearest other clubs" in out
    # Nearby club carries its distance + live count.
    assert "**Marion**" in out and "km" in out and "40 in club now" in out


def test_format_revo_club_detail_degrades_without_geo_or_count():
    import app.bot as bot

    # No lat/lng → no maps link; no occupancy → "unavailable".
    entry = _dir("Ghost", "Ghosttown", None, None, "SA")
    out = bot._format_revo_club_detail(entry, [], [])
    assert "google.com/maps" not in out
    assert "Live count unavailable right now" in out


# ---- auto-nickname from the PerfectGym first name on /revo_link --------------
# Nicknames are no longer set by hand — a successful link overwrites the member's
# bot-wide nickname with their PerfectGym first name (via their OWN client).

def test_apply_perfectgym_nickname_overwrites_from_first_name(monkeypatch):
    """The member's PerfectGym first name overwrites their bot-wide nickname, using
    THEIR OWN per-user client, always overwriting (set_user_nickname is an upsert)."""
    import app.bot as bot

    row = {"user_id": 4242, "email": "me@example.com", "password_enc": "x"}
    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: row)

    fake_client = MagicMock()
    fake_client.get_first_name.return_value = "Sean"
    used_rows = []

    def _client_for(r):
        used_rows.append(r)
        return fake_client

    monkeypatch.setattr(bot, "_perfectgym_client_for_user", _client_for)

    calls = []
    monkeypatch.setattr(
        bot.db, "set_user_nickname",
        lambda uid, nick, set_by: calls.append((uid, nick, set_by)),
    )

    assert bot._apply_perfectgym_nickname(4242) == "Sean"
    # Overwrite (unconditional upsert) with the fetched first name.
    assert calls == [(4242, "Sean", 4242)]
    # Built the client from THIS member's own row.
    assert used_rows == [row]


def test_apply_perfectgym_nickname_disambiguates_collision(monkeypatch):
    """When another member already holds the fetched first name, the new member
    gets a discriminated nickname ('Josh' -> 'Josh 2') instead of silently
    sharing 'Josh' — a shared nickname would misattribute nickname-targeted chat
    lifts (via _resolve_nickname_target) to whichever row matches first."""
    import app.bot as bot

    row = {"user_id": 999, "email": "bob@example.com", "password_enc": "x"}
    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: row)

    fake_client = MagicMock()
    fake_client.get_first_name.return_value = "Josh"
    monkeypatch.setattr(bot, "_perfectgym_client_for_user", lambda r: fake_client)

    # "Josh" already belongs to a DIFFERENT member (111); "Josh 2" is free.
    def _owner(nick):
        return 111 if nick.strip().lower() == "josh" else None

    monkeypatch.setattr(bot.db, "nickname_owner", _owner)
    calls = []
    monkeypatch.setattr(
        bot.db, "set_user_nickname",
        lambda uid, nick, set_by: calls.append((uid, nick, set_by)),
    )

    assert bot._apply_perfectgym_nickname(999) == "Josh 2"
    assert calls == [(999, "Josh 2", 999)]


def test_unique_nickname_idempotent_for_own_row(monkeypatch):
    """A member's OWN existing nickname is not a collision, so re-linking keeps the
    bare first name rather than climbing to 'Josh 2' on every link."""
    import app.bot as bot

    # The name is held, but by THIS same user — so no discriminator is added.
    monkeypatch.setattr(bot.db, "nickname_owner", lambda nick: 4242)
    assert bot._unique_nickname("Josh", 4242) == "Josh"


def test_unique_nickname_climbs_past_multiple_collisions(monkeypatch):
    """The discriminator increments until a free name is found ('Josh', 'Josh 2'
    both taken -> 'Josh 3')."""
    import app.bot as bot

    taken = {"josh": 111, "josh 2": 222}
    monkeypatch.setattr(
        bot.db, "nickname_owner", lambda nick: taken.get(nick.strip().lower())
    )
    assert bot._unique_nickname("Josh", 999) == "Josh 3"


def test_apply_perfectgym_nickname_non_fatal_when_fetch_fails(monkeypatch):
    """A failed first-name fetch leaves any existing nickname untouched and never
    raises — the link still succeeds."""
    import app.bot as bot

    monkeypatch.setattr(
        bot.db, "get_revo_account",
        lambda uid: {"user_id": 1, "email": "a@b.c", "password_enc": "x"},
    )
    boom = MagicMock()
    boom.get_first_name.side_effect = RuntimeError("login failed")
    monkeypatch.setattr(bot, "_perfectgym_client_for_user", lambda r: boom)
    monkeypatch.setattr(
        bot.db, "set_user_nickname",
        lambda *a, **k: pytest.fail("must not set a nickname when the fetch fails"),
    )
    assert bot._apply_perfectgym_nickname(1) is None


def test_apply_perfectgym_nickname_skips_blank_name(monkeypatch):
    """No first name (None/blank) → no nickname write, returns None."""
    import app.bot as bot

    monkeypatch.setattr(
        bot.db, "get_revo_account",
        lambda uid: {"user_id": 1, "email": "a@b.c", "password_enc": "x"},
    )
    client = MagicMock()
    client.get_first_name.return_value = None
    monkeypatch.setattr(bot, "_perfectgym_client_for_user", lambda r: client)
    monkeypatch.setattr(
        bot.db, "set_user_nickname",
        lambda *a, **k: pytest.fail("must not set a nickname with no first name"),
    )
    assert bot._apply_perfectgym_nickname(1) is None


def test_apply_perfectgym_nickname_no_row_returns_none(monkeypatch):
    """No linked row → nothing to name from → None (never raises)."""
    import app.bot as bot

    monkeypatch.setattr(bot.db, "get_revo_account", lambda uid: None)
    monkeypatch.setattr(
        bot, "_perfectgym_client_for_user",
        lambda r: pytest.fail("no client should be built without a row"),
    )
    assert bot._apply_perfectgym_nickname(999) is None


# ---- /seeprofile roster gather — OWN creds per member, URL never logged ------

def test_seeprofile_gather_uses_each_members_own_client(monkeypatch, caplog):
    """Each member's photo is fetched with THAT member's OWN per-user client (never
    the shared account, never one member's client for another), refreshed for a
    valid signed URL, then downloaded immediately — and the signed URL is not logged."""
    import app.bot as bot

    rows = [
        {"user_id": 1, "email": "a@x", "password_enc": "pa"},
        {"user_id": 2, "email": "b@x", "password_enc": "pb"},
    ]

    clients_by_uid = {}

    def _client_for(row):
        uid = int(row["user_id"])
        c = MagicMock(name=f"client{uid}")
        c._row = row
        c.get_photo_url.return_value = (
            f"https://pgaustoragev2.perfectgymcdn.com/p{uid}.jpg?sig=SECRET-{uid}"
        )
        c.get_first_name.return_value = f"Name{uid}"
        clients_by_uid[uid] = c
        return c

    monkeypatch.setattr(bot, "_perfectgym_client_for_user", _client_for)
    monkeypatch.setattr(
        bot.revo_perfectgym, "download_photo",
        lambda url, timeout=None: b"\xff\xd8\xff" + url.encode()[-1:],
    )
    # The shared account must NEVER be consulted by /seeprofile.
    for name in ("shared_client_from_env", "shared_club_occupancy",
                 "shared_club_list"):
        monkeypatch.setattr(
            bot.revo_perfectgym, name,
            lambda *a, **k: pytest.fail(f"/seeprofile must not touch shared ({name})"),
        )

    with caplog.at_level(logging.DEBUG):
        results, failures = bot._seeprofile_gather(rows)

    assert failures == 0
    assert [uid for uid, _, _ in results] == [1, 2]
    assert [name for _, name, _ in results] == ["Name1", "Name2"]
    # Each photo fetched with a FRESH login (refresh=True) on THAT member's client.
    clients_by_uid[1].get_photo_url.assert_called_once_with(refresh=True)
    clients_by_uid[2].get_photo_url.assert_called_once_with(refresh=True)
    assert clients_by_uid[1]._row is rows[0]
    assert clients_by_uid[2]._row is rows[1]
    # Signed capability URLs never reach the logs.
    assert "SECRET-1" not in caplog.text
    assert "SECRET-2" not in caplog.text
    assert "sig=" not in caplog.text


def test_seeprofile_gather_skips_failures_without_logging_url(monkeypatch, caplog):
    """One bad account (login/photo/download error) is counted + skipped so it
    never sinks the roster — and the URL-bearing error message is never logged."""
    import app.bot as bot

    rows = [
        {"user_id": 1, "email": "a", "password_enc": "x"},
        {"user_id": 2, "email": "b", "password_enc": "x"},
    ]

    good = MagicMock()
    good.get_photo_url.return_value = "https://cdn/p.jpg?sig=GOODSIG"
    good.get_first_name.return_value = "Good"

    def _client_for(row):
        if int(row["user_id"]) == 1:
            bad = MagicMock()
            # A requests HTTPError message embeds the signed URL — must NOT leak.
            bad.get_photo_url.side_effect = RuntimeError(
                "404 Client Error for url: https://cdn/p.jpg?sig=LEAKYSIG"
            )
            return bad
        return good

    monkeypatch.setattr(bot, "_perfectgym_client_for_user", _client_for)
    monkeypatch.setattr(
        bot.revo_perfectgym, "download_photo",
        lambda url, timeout=None: b"\xff\xd8\xffB",
    )

    with caplog.at_level(logging.DEBUG):
        results, failures = bot._seeprofile_gather(rows)

    assert failures == 1
    assert [uid for uid, _, _ in results] == [2]  # bad member skipped, good kept
    # The exception message (which embeds the signed URL) must NOT be logged.
    assert "LEAKYSIG" not in caplog.text
    assert "sig=" not in caplog.text


def test_seeprofile_gather_no_photo_counts_as_failure(monkeypatch):
    """A member with no PhotoUrl (None) is counted as a failure, not a crash."""
    import app.bot as bot

    client = MagicMock()
    client.get_photo_url.return_value = None
    monkeypatch.setattr(bot, "_perfectgym_client_for_user", lambda r: client)
    monkeypatch.setattr(
        bot.revo_perfectgym, "download_photo",
        lambda *a, **k: pytest.fail("must not download when there is no URL"),
    )
    results, failures = bot._seeprofile_gather([{"user_id": 1}])
    assert results == []
    assert failures == 1


# ---- retired manual nick commands: no command objects, no help references ----

def test_manual_nick_commands_are_removed():
    """set_nick / remove_nick are fully gone — no module symbol, no tree command."""
    import app.bot as bot

    assert not hasattr(bot, "set_nick_cmd")
    assert not hasattr(bot, "remove_nick_cmd")
    assert bot.bot.tree.get_command("set_nick") is None
    assert bot.bot.tree.get_command("remove_nick") is None
    # The kept pieces still exist (display + chat targeting rely on them).
    assert hasattr(bot, "_bot_name")
    assert hasattr(bot, "_resolve_nickname_target")
    assert bot.bot.tree.get_command("nicks") is not None


def test_no_dangling_set_remove_nick_strings_in_bot_source():
    """No user-facing (or any) '/set_nick' / '/remove_nick' string survives in the
    bot module — help text, command descriptions, and comments all cleaned up."""
    import app.bot as bot

    src = inspect.getsource(bot)
    assert "/set_nick" not in src
    assert "/remove_nick" not in src


def test_help_text_has_no_retired_nick_commands():
    """Belt-and-braces: the rendered /help and /help_revo_link embeds mention the
    retired commands nowhere."""
    import app.bot as bot

    # These are app_commands.Command objects — inspect the underlying callback.
    help_src = (
        inspect.getsource(bot.help_cmd.callback)
        + inspect.getsource(bot.help_revo_link_cmd.callback)
        + inspect.getsource(bot.nicks_cmd.callback)
    )
    assert "set_nick" not in help_src
    assert "remove_nick" not in help_src
