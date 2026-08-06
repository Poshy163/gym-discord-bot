"""Tests for small pure helpers inside app.bot.

We avoid importing discord runtime state by only touching pure functions.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
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
    _coach_body_composition_block,
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
from app.presence import PresenceSummary  # noqa: E402


def test_presence_transition_closes_stale_activity_when_offline(monkeypatch):
    from app import bot as bot_mod

    before = SimpleNamespace(
        id=100, status=SimpleNamespace(value="offline"),
    )
    after = SimpleNamespace(
        id=100, status=SimpleNamespace(value="offline"),
    )
    monkeypatch.setattr(bot_mod, "_get_all_activities_info", lambda _m: [])
    log_set = MagicMock(return_value=False)
    monkeypatch.setattr(bot_mod.db, "activity_log_set", log_set)

    bot_mod._record_presence_transition(1, before, after)

    log_set.assert_called_once_with(1, 100, [])


def test_presence_transition_forwards_late_activity_metadata(monkeypatch):
    from app import bot as bot_mod

    before = SimpleNamespace(
        id=100, status=SimpleNamespace(value="online"),
    )
    after = SimpleNamespace(
        id=100, status=SimpleNamespace(value="online"),
    )
    old = [("Rust", None, None)]
    enriched = [("Rust", "https://cdn/rust.png", 1234)]
    monkeypatch.setattr(
        bot_mod, "_get_all_activities_info",
        lambda member: old if member is before else enriched,
    )
    log_set = MagicMock(return_value=False)
    monkeypatch.setattr(bot_mod.db, "activity_log_set", log_set)

    bot_mod._record_presence_transition(1, before, after)

    log_set.assert_called_once_with(1, 100, enriched)


def test_presence_schedule_embed_discloses_partial_coverage():
    from app import bot as bot_mod

    status_at = datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc)
    summary = PresenceSummary(
        online_seconds=2 * 3600,
        offline_seconds=4 * 3600,
        final_status="online",
        final_status_at=status_at,
        transitions=3,
    )
    user = SimpleNamespace(display_name="Alice")

    embed = bot_mod._render_schedule_embed(user, summary, days=1)

    assert "25% coverage" in embed.description
    assert "Treat the totals as partial" in embed.description
    now_field = next(field for field in embed.fields if field.name == "Now")
    assert str(int(status_at.timestamp())) in now_field.value
    assert "25% window coverage" in embed.footer.text


def test_stop_background_loops_cancels_and_drains_schedulers(monkeypatch):
    from app import bot as bot_mod

    async def scenario():
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        task = asyncio.create_task(worker())
        await started.wait()

        class _Scheduler:
            def get_task(self):
                return task

            def cancel(self):
                task.cancel()

        monkeypatch.setattr(
            bot_mod, "_BACKGROUND_LOOP_NAMES", ("_shutdown_test_loop",),
        )
        monkeypatch.setattr(
            bot_mod, "_shutdown_test_loop", _Scheduler(), raising=False,
        )

        await bot_mod._stop_background_loops()

        assert task.cancelled()
        assert finalized.is_set()

    asyncio.run(scenario())


def test_finish_bot_shutdown_awaits_close_and_drains_stragglers(monkeypatch):
    from app import bot as bot_mod

    loop = asyncio.new_event_loop()
    close_release = asyncio.Event()
    close_finished = False
    stray_finished = False

    async def close():
        nonlocal close_finished
        await close_release.wait()
        close_finished = True

    async def stray():
        nonlocal stray_finished
        try:
            await asyncio.Event().wait()
        finally:
            stray_finished = True

    monkeypatch.setattr(bot_mod.bot, "close", close)
    close_task = loop.create_task(close())
    stray_task = loop.create_task(stray())
    loop.run_until_complete(asyncio.sleep(0))
    assert not close_task.done()

    loop.call_soon(close_release.set)
    try:
        bot_mod._finish_bot_shutdown(loop, close_task)
        assert close_finished is True
        assert stray_finished is True
        assert stray_task.cancelled()
        assert not asyncio.all_tasks(loop)
    finally:
        loop.close()


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
    measured = datetime.now(timezone.utc) - timedelta(days=1)
    _bot_db.set_bodyweight(99, 7, 84.0, recorded_at=measured)
    _bot_db.add_body_metrics(
        99, 7, {"body_fat_pct": (21.8, "%")}, recorded_at=measured,
    )
    _bot_db.goal_set(99, 7, "bench", 120, False)

    payload = _build_progress_payload(
        99, 7, "Sam", 30, include_body_composition=True,
    )
    # Must serialize cleanly for the Gemini prompt.
    blob = json.dumps(payload, default=str)
    assert "lifting" in payload and "nutrition" in payload
    assert payload["nutrition"]["calorie_goal_kcal"] == 2400
    assert payload["bodyweight"]["latest_kg"] == 84.0
    composition = payload["bodyweight"]["smart_scale_composition"]
    assert composition["metrics"][0]["metric"] == "body_fat_pct"
    assert "hydration" in composition["source_note"]
    public_payload = _build_progress_payload(99, 7, "Sam", 30)
    assert public_payload["bodyweight"]["smart_scale_composition"] is None
    assert any(g["equipment"] == "bench" for g in payload["lifting"]["goals"])
    assert len(blob) > 0


def test_coach_composition_covers_full_window_and_excludes_future():
    user_id = 990_007
    guild_id = 990_099
    now = datetime.now(timezone.utc)
    readings = [(now - timedelta(days=300), 30.0)]
    readings.extend(
        (now - timedelta(days=day), 20.0 + day / 100)
        for day in range(181, 0, -1)
    )
    # This value would become the apparent latest if the upper window bound
    # were missing.
    readings.append((now + timedelta(days=1), 99.0))
    for measured, value in readings:
        _bot_db.set_bodyweight(
            guild_id, user_id, 80.0, recorded_at=measured,
        )
        _bot_db.add_body_metrics(
            guild_id,
            user_id,
            {"body_fat_pct": (value, "%")},
            recorded_at=measured,
        )

    block = _coach_body_composition_block(user_id, 365)
    assert block is not None
    metric = block["metrics"][0]
    assert metric["measurements"] == 182
    assert metric["change_in_window"] == pytest.approx(-9.99, abs=0.1)
    assert metric["latest"] != 99.0


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


# ---- /revo_clubs state selection ---------------------------------------------

def test_normalise_state_accepts_codes_names_and_punctuation():
    import app.bot as bot

    for text in ("sa", "SA", " Sa ", "s.a.", "South Australia", "south  australia"):
        assert bot._normalise_state(text) == "SA", text
    assert bot._normalise_state("New South Wales") == "NSW"
    assert bot._normalise_state("victoria") == "VIC"
    # Territories Revo doesn't operate in still resolve — the caller answers
    # "no clubs there", which is clearer than "unknown state".
    assert bot._normalise_state("QLD") == "QLD"
    assert bot._normalise_state("banana") is None
    assert bot._normalise_state("") is None
    assert bot._normalise_state(None) is None


def test_revo_states_present_ranks_by_club_count():
    import app.bot as bot

    directory = [
        _dir("Modbury", "Modbury", -34.8, 138.6, "SA"),
        _dir("Marion", "Oaklands Park", -35.0, 138.5, "SA"),
        _dir("Cannington", "Cannington", -32.0, 115.9, "WA"),
        _dir("Nowhere", None, None, None, None),  # unknown state is skipped
    ]
    assert bot._revo_states_present(directory) == ["SA", "WA"]
    assert bot._revo_states_present([]) == []


def test_revo_clubs_state_embed_scopes_and_totals():
    import app.bot as bot

    directory = [
        _dir("Modbury", "Modbury", -34.8, 138.6, "SA"),
        _dir("Marion", "Oaklands Park", -35.0, 138.5, "SA"),
        _dir("Cannington", "Cannington", -32.0, 115.9, "WA"),
    ]
    occupancy = [_occ("Modbury", 90), _occ("Marion", 40)]

    embed = bot._revo_clubs_state_embed(directory, occupancy, "sa")
    assert embed.title == "🏋️ Revo clubs in SA"
    body = "\n".join(f.value for f in embed.fields)
    assert "**Marion** — Oaklands Park (40 in club now)" in body
    assert "**Modbury** (90 in club now)" in body
    assert "Cannington" not in body           # other state scoped out
    assert "2 clubs" in embed.description
    assert "130" in embed.description          # live total across the state


def test_revo_clubs_state_embed_omits_total_when_board_down():
    import app.bot as bot

    directory = [_dir("Modbury", "Modbury", -34.8, 138.6, "SA")]
    embed = bot._revo_clubs_state_embed(directory, [], "SA")
    # No occupancy at all → report the club count but never a bogus "0 in club".
    assert embed.description == "1 club"
    assert "count unavailable" in embed.fields[0].value


def test_revo_clubs_state_embed_empty_state_names_the_real_ones():
    import app.bot as bot

    directory = [_dir("Cannington", "Cannington", -32.0, 115.9, "WA")]
    embed = bot._revo_clubs_state_embed(directory, [], "QLD")
    assert embed.fields == []
    assert "No clubs here yet" in embed.description
    assert "WA" in embed.description


def test_revo_clubs_state_embed_columns_stay_inside_discord_limits():
    """WA is 37 clubs — as one blob that approaches the 2 000-char message cap,
    which is why the state list is an embed split into columns."""
    import app.bot as bot

    directory = [
        _dir(f"Club {i:02d}", f"Suburb {i:02d}", -32.0, 115.9, "WA")
        for i in range(37)
    ]
    occupancy = [_occ(f"Club {i:02d}", i * 3, state="WA") for i in range(37)]
    embed = bot._revo_clubs_state_embed(directory, occupancy, "WA")

    assert len(embed.fields) == 3                     # 37 clubs → three columns
    assert all(f.inline for f in embed.fields)
    assert len(embed) <= 6000                         # whole-embed cap
    for f in embed.fields:
        assert len(f.value) <= bot.EMBED_FIELD_LIMIT  # per-field cap
    # Every club is present exactly once across the columns.
    body = "\n".join(f.value for f in embed.fields)
    assert body.count("• **Club ") == 37


def test_revo_state_autocomplete_falls_back_when_directory_is_cold(monkeypatch):
    """Autocomplete has ~3s and must never trigger a PerfectGym login, so a cold
    cache degrades to the curated state list instead of blocking."""
    import asyncio
    import app.bot as bot
    from app import revo_client, revo_perfectgym as pg

    monkeypatch.setattr(pg, "cached_club_list", lambda: [])

    async def choices(q):
        return [c.value for c in await bot._revo_state_autocomplete(None, q)]

    assert asyncio.run(choices("")) == revo_client.known_states()
    assert asyncio.run(choices("s")) == ["SA"]
    assert asyncio.run(choices("south australia")) == ["SA"]  # full name matches
    assert asyncio.run(choices("zz")) == []


def test_revo_state_autocomplete_prefers_live_directory(monkeypatch):
    import asyncio
    import app.bot as bot
    from app import revo_perfectgym as pg

    directory = [
        _dir("Modbury", "Modbury", -34.8, 138.6, "SA"),
        _dir("Marion", "Oaklands Park", -35.0, 138.5, "SA"),
        _dir("Cannington", "Cannington", -32.0, 115.9, "WA"),
    ]
    monkeypatch.setattr(pg, "cached_club_list", lambda: directory)
    out = asyncio.run(bot._revo_state_autocomplete(None, ""))
    assert [c.value for c in out] == ["SA", "WA"]  # busiest state first


def test_revo_state_autocomplete_survives_a_broken_cache(monkeypatch):
    import asyncio
    import app.bot as bot
    from app import revo_client, revo_perfectgym as pg

    def boom():
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(pg, "cached_club_list", boom)
    out = asyncio.run(bot._revo_state_autocomplete(None, ""))
    assert [c.value for c in out] == revo_client.known_states()


def test_clip_field_trims_on_a_line_break():
    import app.bot as bot

    text = "\n".join(f"• line {i}" for i in range(400))
    out = bot._clip_field(text)
    assert len(out) <= bot.EMBED_FIELD_LIMIT
    assert out.endswith("…")
    # Cut on a newline, so the last surviving line is whole rather than sliced.
    assert out.splitlines()[-2].startswith("• line ")
    # Short input is returned untouched.
    assert bot._clip_field("• short") == "• short"


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


# ---- nutrition reply prefixes (a wire format, not decoration) ----------------

def test_nutrition_reply_prefixes_keep_matching_legacy_replies():
    """A ❌ reaction finds one of our nutrition replies by its leading glyph, so
    the ~800 replies already sitting in Discord history must keep matching after
    the icon vocabulary changed — otherwise undo silently dies on all of them."""
    import app.bot as bot

    for legacy in ("🍽️", "🥗"):
        assert legacy in bot._NUTRITION_REPLY_PREFIXES, legacy
        assert f"{legacy} **+650 cal**".startswith(bot._NUTRITION_REPLY_PREFIXES)

    # ...and the current ones match too.
    for current in ("🍎 **+650 cal**", "🥩 **+40 g** protein",
                    "🍎🥩 Logged **650 cal** + **40 g** protein",
                    "🤖 Estimated **650 cal**"):
        assert current.startswith(bot._NUTRITION_REPLY_PREFIXES), current

    # A lift reply must NOT match — it has its own undo path.
    assert not "Added **80kg** to **bench press**".startswith(
        bot._NUTRITION_REPLY_PREFIXES
    )


def test_new_nutrition_replies_no_longer_use_the_retired_food_icons():
    """One food icon (🍎). 🍽️/🥗 survive only in the undo prefix tuple and the
    comments explaining why they must."""
    import pathlib
    import app.bot as bot

    src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip() for line in src.splitlines()
        if ("🍽️" in line or "🥗" in line)
        and not line.lstrip().startswith("#")
        and "legacy" not in line
    ]
    assert offenders == [], f"retired food icons still emitted: {offenders}"


# ---- /busy state boards ------------------------------------------------------

def _occ_list():
    from app.revo_perfectgym import ClubOccupancy
    return [
        ClubOccupancy(name="Marion", suburb="Marion", state="SA", count=133,
                      capacity=None),
        ClubOccupancy(name="Modbury", suburb="Modbury", state="SA", count=13,
                      capacity=None),
        ClubOccupancy(name="Munno Para", suburb="Munno Para", state="SA",
                      count=115, capacity=None),
        ClubOccupancy(name="Cannington", suburb="Cannington", state="WA",
                      count=200, capacity=None),
    ]


def test_busy_state_embed_ranks_and_scopes_to_one_state():
    """top_busiest has always accepted a state; nothing exposed it, so the
    board could only ever be scoped to your own home club's state."""
    import app.bot as bot
    from app import ui

    embed = bot._busy_state_embed(_occ_list(), "SA")
    assert embed.title == "🏋️ Busiest Revo clubs in SA"
    assert "3 clubs" in embed.description
    assert "261" in embed.description          # 133 + 13 + 115, SA only
    body = "\n".join(f.value for f in embed.fields)
    assert "Cannington" not in body            # WA scoped out
    # Ranked busiest-first.
    assert body.index("Marion") < body.index("Munno Para") < body.index("Modbury")
    assert ui.overflows(embed) is None


def test_busy_state_embed_names_the_quietest_club():
    """The other half of "where should I go?" — a busiest board alone answers
    the opposite of the question most people are asking."""
    import app.bot as bot

    embed = bot._busy_state_embed(_occ_list(), "SA")
    quiet = [f.value for f in embed.fields if f.name == "Quietest right now"]
    assert quiet and "Modbury" in quiet[0]


def test_busy_state_embed_limits_the_board_to_five():
    import app.bot as bot
    from app.revo_perfectgym import ClubOccupancy

    many = [
        ClubOccupancy(name=f"Club {i:02d}", suburb=None, state="SA", count=i,
                      capacity=None)
        for i in range(30)
    ]
    embed = bot._busy_state_embed(many, "SA")
    board = [f.value for f in embed.fields if f.name.startswith("Busiest")][0]
    assert board.count("Club ") == 5


def test_busy_state_embed_empty_state_names_the_reporting_ones():
    import app.bot as bot

    embed = bot._busy_state_embed(_occ_list(), "QLD")
    assert "No live counts for QLD" in embed.title
    assert "SA" in embed.description and "WA" in embed.description
    assert embed.fields == []


def test_busiest_board_bars_are_relative_to_the_top_club():
    """PerfectGym leaves UsersLimit null for almost every club, so a
    percentage-of-capacity bar would be blank nearly everywhere."""
    import app.bot as bot
    from app import ui

    embed = ui.card("t")
    assert bot._busiest_board_field(embed, _occ_list(), state="SA") is True
    board = embed.fields[0].value
    assert ui.FILL * 8 in board            # the busiest club fills its bar
    assert bot._busiest_board_field(ui.card("t"), [], state="SA") is False


# ---- AI failure copy ---------------------------------------------------------

def test_estimate_failed_does_not_double_the_robot_icon():
    """friendly_message already opens with 🤖, so the old f-string rendered
    "🤖 Couldn't estimate that: 🤖 The AI ..." — which is what the user saw."""
    import app.bot as bot
    from app import gemini_client

    friendly = gemini_client.friendly_message(
        gemini_client.GeminiError("x", status_code=400, status="INVALID_ARGUMENT")
    )
    out = bot._estimate_failed(friendly)
    assert out.count("🤖") == 1, out
    assert out.startswith("🤖 Couldn't estimate that")

    # A parse failure arrives without an icon and still reads correctly.
    plain = bot._estimate_failed("that didn't look like food")
    assert plain == "🤖 Couldn't estimate that: that didn't look like food"


def test_ai_error_embed_gives_admins_googles_wording_on_a_config_fault():
    """A rejected key is 400 INVALID_ARGUMENT. Only the operator can fix it, so
    the card carries the real message instead of making them read logs."""
    import app.bot as bot
    from app import gemini_client, ui

    exc = gemini_client.GeminiError(
        "API key not valid. Please pass a valid API key.",
        status_code=400, status="INVALID_ARGUMENT",
    )
    embed = bot._ai_error_embed(exc)
    assert embed.colour == ui.DANGER          # config fault, not a blip
    admin = [f.value for f in embed.fields if f.name == "For admins"]
    assert admin and "API key not valid" in admin[0]
    assert ui.overflows(embed) is None


def test_ai_error_embed_stays_amber_and_terse_for_a_transient_blip():
    import app.bot as bot
    from app import gemini_client, ui

    exc = gemini_client.GeminiError(
        "The model is overloaded.", status_code=503, status="UNAVAILABLE",
        retryable=True,
    )
    embed = bot._ai_error_embed(exc)
    assert embed.colour == ui.WARNING
    assert not [f for f in embed.fields if f.name == "For admins"]
    assert "temporary" in embed.footer.text


# ---- ❌ as the universal removal affordance ----------------------------------

def test_slash_nutrition_replies_match_the_undo_prefix_gate():
    """A ❌ only does anything if the reply's leading glyph is in the gate. The
    /calories add and /protein add replies are new entrants to that path, so
    pin them."""
    import app.bot as bot

    for reply in (
        "🍎 Logged **banh mi** = 500 cal",          # /calories add
        "🥩 Logged **40 g** protein",                # /protein add
        "🤖 Estimated **large coffee** ≈ 5 cal",     # /calories estimate
    ):
        assert reply.startswith(bot._NUTRITION_REPLY_PREFIXES), reply


def test_attach_undo_links_rows_to_the_reply_and_adds_the_reaction():
    """The reaction handler resolves a slash reply by the reply's OWN id — a
    followup has no reference to a source post — so the rows must carry that id
    before ❌ can find them."""
    import app.bot as bot

    calls = {"cal": None, "pro": None, "reaction": None}

    class _Msg:
        id = 4242

        async def add_reaction(self, emoji):
            calls["reaction"] = emoji

    class _Interaction:
        async def original_response(self):
            return _Msg()

    monkey = {
        "set_calorie_message_id": lambda cid, mid: calls.__setitem__("cal", (cid, mid)),
        "set_protein_message_id": lambda pid, mid: calls.__setitem__("pro", (pid, mid)),
    }
    real = {k: getattr(bot.db, k) for k in monkey}
    try:
        for k, v in monkey.items():
            setattr(bot.db, k, v)
        asyncio.run(bot._attach_undo(_Interaction(), calorie_id=7, protein_id=9))
    finally:
        for k, v in real.items():
            setattr(bot.db, k, v)

    assert calls["cal"] == (7, 4242)
    assert calls["pro"] == (9, 4242)
    assert calls["reaction"] == "❌"


def test_attach_undo_is_a_no_op_without_any_rows():
    """Nothing was written, so there's nothing to offer removing."""
    import app.bot as bot

    class _Interaction:
        async def original_response(self):
            raise AssertionError("must not fetch the reply when nothing was logged")

    asyncio.run(bot._attach_undo(_Interaction()))


def test_attach_undo_survives_a_failed_reply_fetch():
    """The entry is already saved — a failure here costs the affordance, never
    the log."""
    import app.bot as bot

    class _Interaction:
        async def original_response(self):
            raise discord.HTTPException(MagicMock(status=404), "gone")

    asyncio.run(bot._attach_undo(_Interaction(), calorie_id=1))


def test_ai_estimate_footer_offers_only_the_reaction():
    """/calories undo pops the most recent calorie row and would leave an AI
    estimate's paired protein row behind; ❌ removes both, so it's the only
    removal path the card advertises."""
    import pathlib
    import app.bot as bot

    src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        if "AI estimate —" in line:
            assert "❌" in line
            assert "/calories undo" not in line, line


# ---- sub-1-kcal entries -------------------------------------------------------

def test_rounds_to_zero_kcal_catches_amounts_that_display_as_nothing():
    """`1kj` is 0.239 kcal — it clears a `> 0` guard, gets stored, and then
    renders as "🍎 +0 cal", an entry claiming to be nothing."""
    import app.bot as bot
    from app import calories

    for text in ("1kj", "2kj", "0.5kj"):
        kcal = calories.parse_chat_message(text)[0]
        assert calories.format_kcal(kcal) == "0 cal"     # what the user saw
        assert bot._rounds_to_zero_kcal(kcal), text      # now rejected

    # A real entry, and the boundary, both survive.
    assert not bot._rounds_to_zero_kcal(calories.parse_chat_message("1c")[0])
    assert not bot._rounds_to_zero_kcal(0.5)
    assert not bot._rounds_to_zero_kcal(650)
    # Zero and negatives are the existing guard's job, not this one.
    assert not bot._rounds_to_zero_kcal(0)
    assert not bot._rounds_to_zero_kcal(-5)


def test_every_calorie_entry_path_guards_the_rounds_to_zero_case():
    """Five call sites decide whether to store an amount; a guard missing from
    any one of them puts a "+0 cal" entry back in the database."""
    import pathlib
    import app.bot as bot

    src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
    guarded = src.count("_rounds_to_zero_kcal(kcal)")
    assert guarded == 5, f"expected 5 guarded paths, found {guarded}"


# ---- the ❌ gate must survive the move to embeds ------------------------------

class _FakeEmbed:
    def __init__(self, title):
        self.title = title


class _FakeReply:
    def __init__(self, content="", embeds=()):
        self.content = content
        self.embeds = list(embeds)


def test_is_nutrition_reply_matches_embed_cards():
    """The chat replies are cards now. An embed-only message has an EMPTY
    content, so the old content-only test would have stopped matching every new
    reply — and ❌ would have quietly done nothing on all of them."""
    import app.bot as bot

    for title in ("🍎 +650 cal", "🥩 +40 g protein", "🤖 Estimated large coffee"):
        assert bot._is_nutrition_reply(_FakeReply(embeds=[_FakeEmbed(title)]))


def test_is_nutrition_reply_still_matches_plain_text_history():
    """~800 plain-text replies predate the cards and must stay undoable."""
    import app.bot as bot

    for content in ("🍽️ **+650 cal**", "🥗 Logged **650 cal**", "🍎 Logged x"):
        assert bot._is_nutrition_reply(_FakeReply(content=content))


def test_is_nutrition_reply_ignores_everything_else():
    import app.bot as bot

    assert not bot._is_nutrition_reply(_FakeReply(content="Added **80kg**"))
    assert not bot._is_nutrition_reply(_FakeReply())
    assert not bot._is_nutrition_reply(
        _FakeReply(embeds=[_FakeEmbed("🏋️ Last session")])
    )
    assert not bot._is_nutrition_reply(_FakeReply(embeds=[_FakeEmbed(None)]))


def test_log_card_title_carries_a_prefix_the_gate_accepts():
    """The card's own title is what the gate reads, so the two can't drift."""
    import app.bot as bot
    from app import ui

    for icon in (ui.FOOD, ui.PROTEIN, ui.AI):
        card = bot._log_card(icon, "+650 cal", "status", ui.SUCCESS)
        assert bot._is_nutrition_reply(_FakeReply(embeds=[card]))
        assert ui.overflows(card) is None


def test_log_card_title_accepts_the_combined_macro_prefix():
    """A message that logs both macros replies with ONE card titled with both
    icons. It still has to read as a nutrition reply, or ❌ stops undoing the
    exact replies that have two entries behind them."""
    import app.bot as bot
    from app import ui

    card = bot._log_card(
        f"{ui.FOOD}{ui.PROTEIN}", "Logged **650 cal** + **40 g** protein",
        "meters", ui.WARNING,
    )
    assert bot._is_nutrition_reply(_FakeReply(embeds=[card]))
    assert ui.overflows(card) is None


def _stub_combined(monkeypatch, *, kcal_target=2500.0, protein_max=180.0):
    """Point the combined-card renderer at fixed targets and streaks."""
    import app.bot as bot
    from app import targets as targets_mod

    monkeypatch.setattr(bot, "_calorie_streak", lambda u: 12)
    monkeypatch.setattr(bot, "_protein_streak", lambda u: 4)
    monkeypatch.setattr(
        bot, "_reply_targets",
        lambda u, at=None: targets_mod.Resolved(
            day=None,
            kcal=targets_mod.MacroTarget(kcal_target, None, split=False),
            protein=targets_mod.MacroTarget(protein_max, None, split=False),
        ),
    )


def test_combined_status_shows_both_meters_with_their_own_streaks(monkeypatch):
    """One card, two meters — and each macro's own streak. Stretching the
    calorie streak across both lines credits protein with days it never
    earned."""
    import app.bot as bot

    _stub_combined(monkeypatch)
    status, _colour = bot._combined_status(
        1, cal_total=1200.0, pro_total=90.0,
    )

    # Each meter is two tiers (percentage + bar, then the raw numbers), so the
    # streak rides on the second line of its own macro's meter.
    lines = status.split("\n")
    assert len(lines) == 4
    assert "left today" in lines[1] and "12 day streak" in lines[1]
    assert "headroom" in lines[3] and "4 day streak" in lines[3]


def test_combined_status_takes_the_more_alarming_colour(monkeypatch):
    """Calories landing on target while the protein ceiling is breached must
    not paint the card green — that's the exact day the ceiling exists for."""
    import app.bot as bot
    from app import ui

    _stub_combined(monkeypatch)
    _status, colour = bot._combined_status(
        1, cal_total=2450.0, pro_total=260.0,   # on target / way over max
    )
    assert colour == ui.DANGER


def test_combined_status_drops_the_macro_that_was_not_logged(monkeypatch):
    """`500c and 40p` from someone who only tracks calories gets a one-meter
    card, not a protein bar reading 0 g / 0 g."""
    import app.bot as bot

    _stub_combined(monkeypatch)
    status, _colour = bot._combined_status(1, cal_total=1200.0, pro_total=None)
    assert "\n" in status          # meter is itself two tiers
    assert "streak" in status
    assert "headroom" not in status  # the protein meter's wording


def test_log_card_footer_always_advertises_the_reaction():
    import app.bot as bot
    from app import ui

    plain = bot._log_card(ui.FOOD, "+650 cal", "s", ui.SUCCESS)
    streaked = bot._log_card(ui.FOOD, "+650 cal", "s", ui.SUCCESS, streak=37)
    assert "❌" in plain.footer.text
    assert "❌" in streaked.footer.text
    assert "37-day streak" in streaked.footer.text
    # A day-one streak isn't a streak worth announcing.
    assert "streak" not in bot._log_card(
        ui.FOOD, "x", "s", ui.SUCCESS, streak=1,
    ).footer.text


# ---- /today — the merged calories+protein day card ---------------------------
# It replaced /calories today and /protein today. The merge introduced two ways
# to be wrong that neither single-macro command could be, so both are guarded
# here: scoring a card that shows two macros at once, and rendering a macro the
# member doesn't track.

_CAL_GOAL = {"daily_target_kcal": 2500.0, "label": None, "split": False}
_PRO_GOAL = {"daily_target_g": 180.0, "label": None, "split": False}


def _run_today(monkeypatch, *, cal_goal, pro_goal, cal=(), pro=()):
    """Invoke /today against stubbed goals and entries; return the sent kwargs."""
    import app.bot as bot

    monkeypatch.setattr(bot, "_deny_invisible_target", AsyncMock(return_value=False))
    monkeypatch.setattr(bot, "_deny_channel_outsider", AsyncMock(return_value=False))
    monkeypatch.setattr(bot.db, "calorie_goal_get", lambda g, u, day=None: cal_goal)
    monkeypatch.setattr(bot.db, "protein_goal_get", lambda g, u, day=None: pro_goal)
    monkeypatch.setattr(bot.db, "calorie_entries_between", lambda g, u, a, z: list(cal))
    monkeypatch.setattr(bot.db, "protein_entries_between", lambda g, u, a, z: list(pro))
    monkeypatch.setattr(bot, "_calorie_streak", lambda u: 5)
    monkeypatch.setattr(bot, "_protein_streak", lambda u: 3)

    interaction = MagicMock()
    interaction.user.id = 4242
    interaction.guild_id = 7
    interaction.response.send_message = AsyncMock()
    asyncio.run(bot.today_cmd.callback(interaction, None))
    return interaction.response.send_message.call_args.kwargs


def _entry(key, value, note="x"):
    return {"logged_at": "2026-06-28T02:00:00+00:00", key: value, "note": note}


def test_today_refuses_privately_when_nothing_is_tracked(monkeypatch):
    """Neither macro on: one combined empty state, ephemeral. Falling back to
    either per-macro embed would tell a would-be protein tracker to set
    calories."""
    kwargs = _run_today(monkeypatch, cal_goal=None, pro_goal=None)

    assert kwargs["ephemeral"] is True
    assert "calories or protein" in kwargs["embed"].title


def test_today_card_takes_the_more_alarming_of_the_two_colours(monkeypatch):
    """The merge's headline risk: calories on target (SUCCESS) next to a
    breached protein ceiling (DANGER) must not paint the card green on the exact
    day the ceiling tracker exists to catch."""
    from app import ui

    kwargs = _run_today(
        monkeypatch, cal_goal=_CAL_GOAL, pro_goal=_PRO_GOAL,
        cal=[_entry("kcal", 2400.0)], pro=[_entry("grams", 260.0)],
    )

    assert kwargs["embed"].colour == ui.DANGER


def test_today_omits_the_macro_that_is_not_tracked(monkeypatch):
    """Protein-only trackers get a protein card, not a half-empty one reading
    '0 cal / 0 cal' — and no apple anywhere on it, for a food they aren't
    counting."""
    from app import ui

    embed = _run_today(
        monkeypatch, cal_goal=None, pro_goal=_PRO_GOAL,
        pro=[_entry("grams", 45.0)],
    )["embed"]

    # No title at all: Discord's own "used /today" line already names the
    # command, so the first section heading leads the card.
    assert not embed.title
    assert not any(ui.FOOD in f.name for f in embed.fields)
    assert [f.name for f in embed.fields] == [
        f"{ui.PROTEIN} Protein", f"{ui.PROTEIN} Entries",
    ]


def test_today_names_each_streak_only_when_both_macros_are_on(monkeypatch):
    """Two independent streaks in one footer need labels; one doesn't, and keeps
    the wording the single-macro cards used."""
    both = _run_today(
        monkeypatch, cal_goal=_CAL_GOAL, pro_goal=_PRO_GOAL,
    )["embed"]
    assert "calorie streak" in both.footer.text
    assert "protein streak" in both.footer.text

    one = _run_today(monkeypatch, cal_goal=_CAL_GOAL, pro_goal=None)["embed"]
    assert one.footer.text == "🔥 5 days logging streak"


def test_today_survives_a_heavy_day_on_both_macros(monkeypatch):
    """Meter and entry list sit in separate fields precisely so a long day can't
    breach the 1024-char field limit and get clipped mid-fence."""
    from app import ui

    embed = _run_today(
        monkeypatch, cal_goal=_CAL_GOAL, pro_goal=_PRO_GOAL,
        cal=[_entry("kcal", 1234.0, "a fairly long note here")] * 30,
        pro=[_entry("grams", 44.0, "another longish note x")] * 30,
    )["embed"]

    assert ui.overflows(embed) is None


# ---- /estimate: one AI entry point for words and photos ----------------------
#
# /calories estimate and /calories label used to split this job by input type,
# which asked people to know in advance which kind of photo they were about to
# take. The merged command routes on what the model saw instead, so the tests
# that matter are the routing ones.

class _FakeAttachment:
    def __init__(self, content_type="image/jpeg", size=1024, filename="food.jpg"):
        self.content_type = content_type
        self.size = size
        self.filename = filename

    async def read(self):
        return b"\xff\xd8\xff"


def test_caption_serving_g_reuses_the_text_scale_token():
    """`~110g` with a photo means the same thing as `110g` after a label's
    numbers — one way to say a serving, not two."""
    import app.bot as bot

    assert bot._caption_serving_g("110g") == pytest.approx(110.0)
    assert bot._caption_serving_g("110 g") == pytest.approx(110.0)
    assert bot._caption_serving_g("x1.1") == pytest.approx(110.0)
    # No serving stated → nothing to scale by.
    assert bot._caption_serving_g("chicken and rice") is None
    assert bot._caption_serving_g("") is None
    # A caption that contradicts itself is refused, not guessed at.
    assert bot._caption_serving_g("110g 120g") is None


def test_photo_problem_gates_what_reaches_the_vision_model():
    import app.bot as bot

    assert bot._photo_problem(_FakeAttachment()) is None
    assert bot._photo_problem(_FakeAttachment(content_type="video/mp4"))
    assert bot._photo_problem(_FakeAttachment(size=11 * 1024 * 1024))
    # _first_photo skips the unusable ones rather than failing the message.
    pick = bot._first_photo([
        _FakeAttachment(content_type="application/pdf"),
        _FakeAttachment(filename="ok.png", content_type="image/png"),
    ])
    assert pick is not None and pick.filename == "ok.png"
    assert bot._first_photo([]) is None
    assert bot._first_photo([_FakeAttachment(content_type="text/plain")]) is None


def test_label_howto_hands_back_a_line_you_can_retype():
    """A transcription is worth keeping even when we can't log it yet, and the
    scale token means the answer is one line for any weight rather than a sum."""
    import app.bot as bot
    from app.ai_food import LabelInfo

    line = bot._label_howto(LabelInfo(
        kj_per_100g=895.0, kcal_per_100g=None, protein_per_100g=14.7,
        serving_g=110.0, name="Peanut butter",
    ))
    assert "`895kj 14.7p 110g`" in line
    # No stated serving → a worked example, still valid syntax.
    line = bot._label_howto(LabelInfo(
        kj_per_100g=895.0, kcal_per_100g=None, protein_per_100g=None,
        serving_g=None, name=None,
    ))
    assert "`895kj 110g`" in line
    # Nothing to log against at all.
    assert bot._label_howto(LabelInfo(None, None, 20.0, None, None)) is None


def _stub_store(monkeypatch):
    """Replace the storage helper with a recorder — these tests are about which
    branch runs, not the DB writes, which _store_ai_nutrition owns."""
    import app.bot as bot

    seen = {}

    def _fake(guild_id, target, kcal, protein_g, **kw):
        seen.update(kcal=kcal, protein_g=protein_g, **kw)
        return 11, 22, ["**X cal**", "**Y g** protein"], ["meter"]

    monkeypatch.setattr(bot, "_store_ai_nutrition", _fake)
    return seen


def test_photo_label_outcome_transcribes_without_a_serving(monkeypatch):
    """Most panel photos arrive before you've said how much you ate. That's the
    normal outcome, not a failure — read it back and hand over the line."""
    import app.bot as bot
    from app.ai_food import LabelInfo

    seen = _stub_store(monkeypatch)
    info = LabelInfo(895.0, None, 14.7, 110.0, "Peanut butter")
    out = bot._photo_label_outcome(
        7, MagicMock(id=1), info, serving_g=None, logged_at=None, raw="photo",
    )

    assert (out.calorie_id, out.protein_id) == (0, 0)   # nothing stored
    assert not seen
    body = "\n".join(out.lines)
    assert "per 100 g" in body
    assert "`895kj 14.7p 110g`" in body


def test_photo_label_outcome_logs_once_a_serving_is_known(monkeypatch):
    import app.bot as bot
    from app.ai_food import LabelInfo

    seen = _stub_store(monkeypatch)
    info = LabelInfo(895.0, None, 14.7, None, "Peanut butter")
    out = bot._photo_label_outcome(
        7, MagicMock(id=1), info, serving_g=110.0, logged_at=None, raw="photo",
    )

    assert (out.calorie_id, out.protein_id) == (11, 22)
    # 895 kJ/100 g at 110 g = 984.5 kJ = 235 cal; protein scales with it.
    assert seen["kcal"] == pytest.approx(895.0 * 1.1 / 4.184)
    assert seen["protein_g"] == pytest.approx(14.7 * 1.1)
    assert "110 g" in seen["note"]
    assert "for **110 g**" in "\n".join(out.lines)
    # The meters sit between headline and footnote, so a later change can swap
    # just the middle without re-transcribing the packet.
    assert "for **110 g**" in out.headline
    assert out.footnote and out.footnote.startswith("-#")


def test_photo_label_outcome_refuses_an_implausible_serving(monkeypatch):
    """The per-entry cap applies to a scaled label exactly as to a typed one —
    a misread panel shouldn't bank 40,000 cal."""
    import app.bot as bot
    from app.ai_food import LabelInfo

    _stub_store(monkeypatch)
    out = bot._photo_label_outcome(
        7, MagicMock(id=1), LabelInfo(9999.0, None, 14.7, None, "x"),
        serving_g=2000.0, logged_at=None, raw="photo",
    )
    assert (out.calorie_id, out.protein_id) == (0, 0)
    assert "nothing was stored" in "\n".join(out.lines)


def _run_estimate(monkeypatch, *, ai_result, **kwargs):
    """Invoke /estimate with the AI stubbed; return the interaction mock."""
    import app.bot as bot

    monkeypatch.setattr(bot.db, "calorie_goal_get", lambda g, u, day=None: _CAL_GOAL)
    monkeypatch.setattr(bot.gemini_client, "available", lambda: True)
    monkeypatch.setattr(bot, "_ai_read_photo", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(bot, "_ai_estimate_meal", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(bot, "_attach_undo", AsyncMock())

    interaction = MagicMock()
    interaction.user.id = 4242
    interaction.guild_id = 7
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    asyncio.run(bot.estimate_cmd.callback(interaction, **kwargs))
    return interaction


def test_estimate_needs_something_to_go_on(monkeypatch):
    interaction = _run_estimate(monkeypatch, ai_result=None)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    interaction.followup.send.assert_not_awaited()


def test_estimate_logs_a_photo_of_food(monkeypatch):
    """A plate goes straight in — the model already sized the portion."""
    import app.bot as bot
    from app.ai_food import MealEstimate

    seen = _stub_store(monkeypatch)
    interaction = _run_estimate(
        monkeypatch,
        ai_result=MealEstimate(
            kcal=780.0, protein_g=46.0, name="chicken parmi", confidence="medium",
        ),
        photo=_FakeAttachment(),
    )
    body = interaction.followup.send.call_args.args[0]
    assert "chicken parmi" in body
    assert "confidence: medium" in body
    assert seen["kcal"] == 780.0
    assert bot._attach_undo.call_args.kwargs["calorie_id"] == 11
    assert bot._attach_undo.call_args.kwargs["protein_id"] == 22


def test_estimate_routes_a_panel_photo_to_the_label_path(monkeypatch):
    """Same command, same photo slot — the model decides which it was, so a
    packet is transcribed rather than guessed at."""
    from app.ai_food import LabelInfo

    _stub_store(monkeypatch)
    interaction = _run_estimate(
        monkeypatch,
        ai_result=LabelInfo(895.0, None, 14.7, 110.0, "Peanut butter"),
        photo=_FakeAttachment(),
    )
    body = interaction.followup.send.call_args.args[0]
    assert "per 100 g" in body
    assert "Estimated" not in body   # a transcription, not a guess


def test_estimate_rejects_a_non_photo_attachment(monkeypatch):
    interaction = _run_estimate(
        monkeypatch, ai_result=None,
        photo=_FakeAttachment(content_type="video/mp4"),
    )
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    interaction.followup.send.assert_not_awaited()


def _chat_message(content, attachments=()):
    message = MagicMock()
    message.content = content
    message.attachments = list(attachments)
    message.id = 555
    message.author.id = 4242
    message.channel.typing = AsyncMock()
    message.reply = AsyncMock()
    message.add_reaction = AsyncMock()
    return message


def _run_chat_estimate(monkeypatch, message, description, *, ai_result):
    import app.bot as bot

    monkeypatch.setattr(bot, "_msg_guild_id", lambda m: 7)
    monkeypatch.setattr(bot.db, "calorie_goal_get", lambda g, u, day=None: _CAL_GOAL)
    monkeypatch.setattr(bot.gemini_client, "available", lambda: True)
    monkeypatch.setattr(bot, "_ai_read_photo", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(bot, "_ai_estimate_meal", AsyncMock(return_value=ai_result))
    target = MagicMock()
    target.id = 4242
    return asyncio.run(
        bot._handle_estimate_message(message, target, description)
    )


def test_chat_tilde_takes_a_photo_with_no_words(monkeypatch):
    """`~` plus a picture is a complete request — the photo IS the description,
    so the old three-character floor can't apply when one is attached."""
    import app.bot as bot
    from app.ai_food import MealEstimate

    _stub_store(monkeypatch)
    message = _chat_message("~", [_FakeAttachment()])
    handled = _run_chat_estimate(
        monkeypatch, message, "",
        ai_result=MealEstimate(
            kcal=780.0, protein_g=46.0, name="parmi", confidence="high",
        ),
    )

    assert handled is True
    bot._ai_read_photo.assert_awaited()          # vision, not the text prompt
    bot._ai_estimate_meal.assert_not_awaited()
    assert "parmi" in message.reply.call_args.args[0]


def test_chat_tilde_with_no_photo_still_needs_words(monkeypatch):
    """A bare `~` and nothing else falls through to the other parsers rather
    than burning a Gemini call on an empty description."""
    import app.bot as bot

    message = _chat_message("~")
    handled = _run_chat_estimate(monkeypatch, message, "", ai_result=None)

    assert handled is False
    bot._ai_read_photo.assert_not_awaited()
    bot._ai_estimate_meal.assert_not_awaited()
    message.reply.assert_not_awaited()


def test_chat_photo_of_a_panel_logs_at_the_caption_serving(monkeypatch):
    """`~110g` with a packet photo closes the loop in one message: transcribe
    the panel, then scale it by the serving the caption already stated."""
    from app.ai_food import LabelInfo

    seen = _stub_store(monkeypatch)
    message = _chat_message("~110g", [_FakeAttachment()])
    handled = _run_chat_estimate(
        monkeypatch, message, "110g",
        ai_result=LabelInfo(895.0, None, 14.7, None, "Peanut butter"),
    )

    assert handled is True
    assert seen["kcal"] == pytest.approx(895.0 * 1.1 / 4.184)
    body = message.reply.call_args.args[0]
    assert "for **110 g**" in body
    message.add_reaction.assert_not_awaited()   # the ❌ goes on the reply


def test_chat_photo_of_a_panel_without_a_serving_only_reads_it(monkeypatch):
    from app.ai_food import LabelInfo

    seen = _stub_store(monkeypatch)
    message = _chat_message("~", [_FakeAttachment()])
    handled = _run_chat_estimate(
        monkeypatch, message, "",
        ai_result=LabelInfo(895.0, None, 14.7, 110.0, "Peanut butter"),
    )

    assert handled is True
    assert not seen                              # nothing logged
    assert "`895kj 14.7p 110g`" in message.reply.call_args.args[0]


# ---- restating a day's other replies after an entry changes ------------------
#
# Each confirmation prints the day's total *as at that entry*, so removing or
# editing one silently falsifies every card posted after it — they stay on
# screen disagreeing with /today and with each other. These pin the recompute
# and the guards that stop it rewriting things it shouldn't.

def test_running_totals_are_cumulative_not_the_day_total():
    """The point of the whole feature: a card shows the total *at its own
    entry*, not the day's final figure, so restating means re-accumulating."""
    import app.bot as bot

    rows = [
        {"id": 1, "kcal": 500.0},
        {"id": 2, "kcal": 214.0},
        {"id": 3, "kcal": 235.0},
    ]
    assert bot._running_totals(rows, "kcal") == {1: 500.0, 2: 714.0, 3: 949.0}
    assert bot._running_totals([], "kcal") == {}


class _FakeChannel:
    def __init__(self, messages):
        self._messages = messages

    async def fetch_message(self, mid):
        msg = self._messages.get(mid)
        if msg is None:
            raise discord.NotFound(MagicMock(status=404), "gone")
        return msg


class _FakeReply:
    def __init__(self, content="", embeds=()):
        self.content = content
        self.embeds = list(embeds)
        self.edited = None

    async def edit(self, **kwargs):
        self.edited = kwargs


def _restate_harness(monkeypatch, *, rows, messages, cal=(), pro=()):
    import app.bot as bot

    monkeypatch.setattr(bot.db, "nutrition_replies_for_day", lambda u, a, z: list(rows))
    monkeypatch.setattr(bot.db, "calorie_entries_between", lambda g, u, a, z: list(cal))
    monkeypatch.setattr(bot.db, "protein_entries_between", lambda g, u, a, z: list(pro))
    # The meters resolve their targets through _reply_targets, not the raw
    # goal rows, so that's what has to be stubbed for a card to score at all.
    from app import targets as targets_mod

    resolved = targets_mod.Resolved(
        day=date(2026, 6, 28),
        kcal=targets_mod.MacroTarget(value=1250.0),
        protein=targets_mod.MacroTarget(value=106.0),
    )
    monkeypatch.setattr(bot, "_reply_targets", lambda u, at=None: resolved)
    monkeypatch.setattr(bot, "_calorie_streak", lambda u: 5)
    monkeypatch.setattr(bot, "_protein_streak", lambda u: 3)
    monkeypatch.setattr(bot, "get_channel", lambda cid: _FakeChannel(messages), raising=False)
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: _FakeChannel(messages))
    return bot


def _render_row(reply_id, *, calorie_id=0, protein_id=0, headline="H", footnote=None):
    return {
        "reply_message_id": reply_id, "channel_id": 55, "target_user_id": 4242,
        "calorie_id": calorie_id, "protein_id": protein_id,
        "headline": headline, "footnote": footnote,
    }


def test_restate_rewrites_a_later_text_reply_with_the_corrected_total(monkeypatch):
    """The reported bug: remove a 214 cal entry logged at 5pm and the 9pm card
    still claims the higher running total it was printed with."""
    reply = _FakeReply(content="OLD BODY")
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9002, calorie_id=2, protein_id=2,
                          headline="🍎🥩 Logged **235 cal**", footnote="-# note")],
        messages={9002: reply},
        # The 214 cal entry is already gone, so the running total is just 235.
        cal=[{"id": 2, "kcal": 235.0}],
        pro=[{"id": 2, "grams": 16.0}],
    )

    edited = asyncio.run(bot._restate_day_replies(MagicMock(id=4242)))

    assert edited == 1
    body = reply.edited["content"]
    assert body.startswith("🍎🥩 Logged **235 cal**")   # headline preserved
    assert "-# note" in body                            # footnote preserved
    assert "235 cal / 1,250 cal" in body                # meter recomputed
    assert "totals corrected" in body


def test_restate_rewrites_a_card_reply_without_losing_its_fields(monkeypatch):
    """Card replies carry the note and author in embed fields; only the meter
    and its colour should move."""
    from app import ui

    embed = ui.card("🍎 +235 cal", description="STALE METER", colour=ui.SUCCESS)
    ui.block(embed, "Note", "110 g of the per-100 g values")
    reply = _FakeReply(embeds=[embed])
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9003, calorie_id=2)],
        messages={9003: reply},
        cal=[{"id": 2, "kcal": 235.0}],
    )

    assert asyncio.run(bot._restate_day_replies(MagicMock(id=4242))) == 1
    new = reply.edited["embed"]
    assert new.title == "🍎 +235 cal"
    assert [f.name for f in new.fields] == ["Note"]
    assert "STALE METER" not in (new.description or "")
    assert "235 cal / 1,250 cal" in new.description


def test_restate_leaves_an_already_undone_reply_struck_through(monkeypatch):
    """A reply that's been ❌'d reads as struck-through. Rewriting its meters
    would make a removed entry look live again."""
    forgotten = []
    reply = _FakeReply(content="~~🍎🥩 Logged **214 cal**~~\n\n↩️ Removed")
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9001, calorie_id=1)],
        messages={9001: reply},
        cal=[{"id": 1, "kcal": 214.0}],
    )
    monkeypatch.setattr(bot.db, "forget_nutrition_reply", forgotten.append)

    assert asyncio.run(bot._restate_day_replies(MagicMock(id=4242))) == 0
    assert reply.edited is None
    assert forgotten == [9001]


def test_restate_forgets_a_reply_that_was_deleted_by_hand(monkeypatch):
    forgotten = []
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9004, calorie_id=1)],
        messages={},                       # fetch_message raises NotFound
        cal=[{"id": 1, "kcal": 214.0}],
    )
    monkeypatch.setattr(bot.db, "forget_nutrition_reply", forgotten.append)

    assert asyncio.run(bot._restate_day_replies(MagicMock(id=4242))) == 0
    assert forgotten == [9004]


def test_restate_skips_a_reply_whose_entries_are_all_gone(monkeypatch):
    """Nothing left to meter — and the undo path has already struck that one
    through, so touching it would undo the strike."""
    reply = _FakeReply(content="body")
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9005, calorie_id=99)],   # id 99 isn't in the day
        messages={9005: reply},
        cal=[{"id": 1, "kcal": 214.0}],
    )

    assert asyncio.run(bot._restate_day_replies(MagicMock(id=4242))) == 0
    assert reply.edited is None


def test_restate_will_not_rewrite_a_pre_cache_text_reply(monkeypatch):
    """Replies posted before the render cache existed have no headline, so
    they're left alone rather than rebuilt into something they never said."""
    reply = _FakeReply(content="body")
    bot = _restate_harness(
        monkeypatch,
        rows=[_render_row(9006, calorie_id=1, headline=None)],
        messages={9006: reply},
        cal=[{"id": 1, "kcal": 214.0}],
    )

    assert asyncio.run(bot._restate_day_replies(MagicMock(id=4242))) == 0
    assert reply.edited is None


def test_restate_caps_how_many_messages_it_will_edit(monkeypatch):
    """A backfill or bulk delete must not turn into a hundred message edits."""
    import app.bot as bot

    n = bot._MAX_RESTATED_REPLIES + 10
    replies = {9000 + i: _FakeReply(content="body") for i in range(n)}
    rows = [
        _render_row(9000 + i, calorie_id=i + 1, headline="🍎 Logged")
        for i in range(n)
    ]
    _restate_harness(
        monkeypatch, rows=rows, messages=replies,
        cal=[{"id": i + 1, "kcal": 10.0} for i in range(n)],
    )

    edited = asyncio.run(bot._restate_day_replies(MagicMock(id=4242)))
    assert edited == bot._MAX_RESTATED_REPLIES
    # It keeps the most recent ones — those are the ones still on screen.
    assert replies[9000].edited is None
    assert replies[9000 + n - 1].edited is not None


# ---- catching up on what was posted while the bot was down -------------------
#
# Three separate faults made a restart lose logs, and each one alone was enough
# to make the catch-up useless, so each gets its own test.

def _backfill_msg(text, mid=500, author_id=4242, guild_id=1):
    msg = MagicMock()
    msg.content = text
    msg.id = mid
    msg.author.bot = False
    msg.author.id = author_id
    msg.guild.id = guild_id
    msg.created_at = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
    return msg


def _tracking_everything(monkeypatch, bot, *, cal=True, pro=True):
    stored = {"cal": [], "pro": []}
    monkeypatch.setattr(
        bot.db, "calorie_goal_get", lambda g, u, day=None: _CAL_GOAL if cal else None,
    )
    monkeypatch.setattr(
        bot.db, "protein_goal_get", lambda g, u, day=None: _PRO_GOAL if pro else None,
    )
    monkeypatch.setattr(
        bot.db, "calorie_add",
        lambda *a, **kw: (stored["cal"].append((a, kw)), len(stored["cal"]))[1],
    )
    monkeypatch.setattr(
        bot.db, "protein_add",
        lambda *a, **kw: (stored["pro"].append((a, kw)), len(stored["pro"]))[1],
    )
    return stored


@pytest.mark.parametrize("text,cals,pros", [
    ("350 c", 1, 0),
    ("2 x 88 c", 1, 0),          # scale token — 2 c x 88
    ("40p", 0, 1),               # protein alone used to be dropped entirely
    ("40g protein", 0, 1),
    ("500c and 40p", 1, 1),      # so did the combined form
    ("895kj 14.7p 110g", 1, 1),
    ("HELLO?", 0, 0),
    ("just chatting", 0, 0),
])
def test_backfill_covers_every_chat_form_the_live_path_does(
    monkeypatch, text, cals, pros,
):
    """The bug: the catch-up only ran calories.parse_chat_message, so a restart
    silently swallowed every protein and combined log posted while it was
    down."""
    import app.bot as bot

    stored = _tracking_everything(monkeypatch, bot)
    target = MagicMock(id=4242)

    bot._backfill_nutrition_entry(_backfill_msg(text), target, text)

    assert len(stored["cal"]) == cals
    assert len(stored["pro"]) == pros


def test_backfill_respects_the_typo_caps_and_what_is_tracked(monkeypatch):
    """A catch-up must not be a side door around the guards the live path
    applies."""
    import app.bot as bot

    stored = _tracking_everything(monkeypatch, bot)
    target = MagicMock(id=4242)
    bot._backfill_nutrition_entry(_backfill_msg("99999c"), target, "99999c")
    assert stored["cal"] == []

    # Protein-only tracker: the calorie half of a combined log is dropped.
    stored = _tracking_everything(monkeypatch, bot, cal=False)
    bot._backfill_nutrition_entry(
        _backfill_msg("500c and 40p"), target, "500c and 40p",
    )
    assert stored["cal"] == [] and len(stored["pro"]) == 1


def test_catchup_scans_forward_from_the_last_heartbeat(monkeypatch):
    """The headline bug: pairing oldest_first=True with a limit told discord.py
    to start at the channel's *first ever* message, so a busy channel spent
    every boot re-reading its opening 1,000 posts and never reached today's."""
    import app.bot as bot

    seen = {}

    class _Chan:
        def history(self, **kwargs):
            seen.update(kwargs)

            async def _empty():
                return
                yield  # pragma: no cover
            return _empty()

    since = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    asyncio.run(bot._backfill_channel(_Chan(), 1000, since=since))
    assert seen["after"] == since
    assert seen["oldest_first"] is True

    # /backfill has no `since`, so it walks back from the newest message —
    # which is what "rescan the last N" means.
    seen.clear()
    asyncio.run(bot._backfill_channel(_Chan(), 200))
    assert "after" not in seen
    assert seen["limit"] == 200


def test_catchup_since_uses_the_marker_but_is_bounded(monkeypatch):
    import app.bot as bot

    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=3)
    monkeypatch.setattr(bot.db, "meta_get", lambda k: recent.isoformat())
    # A grace window either side, so a message posted during the last
    # heartbeat interval isn't missed.
    assert bot._catchup_since() <= recent
    assert bot._catchup_since() > recent - timedelta(minutes=5)

    # No marker (first boot, or a wiped DB) → a bounded lookback, not all of
    # history.
    monkeypatch.setattr(bot.db, "meta_get", lambda k: None)
    floor = now - timedelta(days=bot._MAX_CATCHUP_DAYS)
    assert abs((bot._catchup_since() - floor).total_seconds()) < 60

    # A marker older than the cap is clamped to it.
    monkeypatch.setattr(
        bot.db, "meta_get", lambda k: (now - timedelta(days=400)).isoformat(),
    )
    assert abs((bot._catchup_since() - floor).total_seconds()) < 60


def test_catchup_covers_every_channel_when_none_are_listed(monkeypatch):
    """GYM_CHANNEL_IDS narrows where the bot *listens*; empty means everywhere.
    The old catch-up looped over that list, so on the default config it scanned
    nothing while live logging worked in every channel."""
    import app.bot as bot

    readable = MagicMock()
    readable.permissions_for.return_value = MagicMock(
        read_messages=True, read_message_history=True,
    )
    locked = MagicMock()
    locked.permissions_for.return_value = MagicMock(
        read_messages=False, read_message_history=False,
    )
    guild = MagicMock(text_channels=[readable, locked])
    monkeypatch.setattr(bot, "GYM_CHANNEL_IDS", [])
    monkeypatch.setattr(type(bot.bot), "guilds", property(lambda s: [guild]))

    picked = bot._catchup_channels()
    assert picked == [readable]     # the one it can actually read


# ---- the catch-up summary message -------------------------------------------
#
# ✅ marks on individual posts say *that* something landed. The summary says
# *what*, so nobody has to open /today and compare it against what they
# remember typing.

def _person(bot, name, **amounts):
    p = bot._CaughtUpPerson(name)
    p.add(**amounts)
    return p


@pytest.mark.parametrize("delta,expected", [
    (timedelta(seconds=20), "1m"),      # rounds up: "0m offline" reads as a bug
    (timedelta(minutes=7), "7m"),
    (timedelta(minutes=59), "59m"),
    (timedelta(hours=2), "2h"),         # a whole number of hours drops the 0m
    (timedelta(hours=2, minutes=14), "2h 14m"),
    (timedelta(days=1, hours=3), "1d 3h"),
    (timedelta(days=9), "9d"),
])
def test_format_downtime_trims_to_the_units_that_matter(delta, expected):
    import app.bot as bot

    assert bot._format_downtime(delta) == expected


def test_caught_up_person_lists_only_what_happened():
    import app.bot as bot

    assert _person(bot, "Josh", kcal=526.0).summary() == "**526 cal**"
    assert _person(bot, "Josh", grams=40.0).summary() == "**40 g** protein"
    assert _person(bot, "Josh", lifts=3).summary() == "**3 lifts**"
    assert _person(bot, "Josh", lifts=1).summary() == "**1 lift**"
    both = _person(bot, "Josh", kcal=526.0, grams=40.0).summary()
    assert both == "**526 cal** + **40 g** protein"
    assert _person(bot, "Josh").summary() == ""


def test_catchup_summary_names_who_got_what():
    import app.bot as bot

    result = bot._CatchUpResult(
        scanned=120, posts_with_lifts=1, lifts=3, suppressed=0, entries=4,
        people={
            1: _person(bot, "Josh", kcal=526.0, grams=40.0),
            2: _person(bot, "Dos", kcal=350.0),
            3: _person(bot, "Sean", lifts=3),
        },
    )
    body = bot._catchup_summary(result, "2h 14m")

    assert "after 2h 14m offline" in body
    assert "imported 5 posts I missed" in body      # 4 nutrition + 1 lift post
    assert "**Josh** — **526 cal** + **40 g** protein" in body
    assert "**Sean** — **3 lifts**" in body
    assert "/today" in body


def test_catchup_summary_survives_an_unknown_downtime():
    """No last-online marker (first boot, wiped DB) — say nothing about how
    long rather than inventing a duration."""
    import app.bot as bot

    body = bot._catchup_summary(
        bot._CatchUpResult(10, 0, 0, 0, 1, {1: _person(bot, "Josh", kcal=350.0)}),
        None,
    )
    assert body.startswith("🍎🥩 Caught up — imported 1 post I missed:")


def test_catchup_summary_counts_the_tail_instead_of_listing_it():
    """A long outage across a busy server must not post a wall of names."""
    import app.bot as bot

    people = {
        i: _person(bot, f"Lifter{i}", kcal=float(100 + i))
        for i in range(bot._CATCHUP_NAMES_SHOWN + 3)
    }
    body = bot._catchup_summary(
        bot._CatchUpResult(200, 0, 0, 0, len(people), people), "3h",
    )

    assert body.count("• **") == bot._CATCHUP_NAMES_SHOWN
    assert "…and 3 others" in body


def test_catch_up_result_knows_when_nothing_happened():
    """A quiet redeploy has to stay silent — otherwise every deploy posts."""
    import app.bot as bot

    assert not bot._CatchUpResult(500, 0, 0, 4, 0, {}).anything
    assert bot._CatchUpResult(500, 0, 0, 0, 1, {}).anything
    assert bot._CatchUpResult(500, 1, 2, 0, 0, {}).anything


def test_backfill_channel_tallies_each_person_separately(monkeypatch):
    """The summary is only as good as the tally, and a channel scan sees posts
    from everyone."""
    import app.bot as bot

    _tracking_everything(monkeypatch, bot)
    monkeypatch.setattr(bot.db, "is_message_suppressed", lambda g, m: False)
    monkeypatch.setattr(bot, "_custom_alias_map", lambda g: {})
    monkeypatch.setattr(bot, "_resolve_nickname_target", AsyncMock(return_value=(None, None)))

    posts = [("350 c", 1, "Josh"), ("40p", 2, "Josh"), ("500c", 3, "Dos")]

    def _target(msg):
        member = MagicMock()
        member.id = msg.author.id
        member.display_name = dict(zip([1, 2], ["Josh", "Josh"])).get(msg.id, "Dos")
        return member, msg.content

    monkeypatch.setattr(bot, "_message_lift_target", _target)

    class _Chan:
        def history(self, **kwargs):
            async def _gen():
                for text, mid, _name in posts:
                    m = _backfill_msg(text, mid=mid, author_id=1 if mid < 3 else 2)
                    yield m
            return _gen()

    result = asyncio.run(bot._backfill_channel(_Chan(), 100))

    assert result.entries == 3
    assert set(result.people) == {1, 2}
    assert result.people[1].kcal == pytest.approx(350.0)
    assert result.people[1].grams == pytest.approx(40.0)
    assert result.people[2].kcal == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Revo attendance poller. The underlying signal is a per-DAY attendance flag
# that Revo batches into its rewards system, so the poller's job is to notice a
# NEWER day appearing — it can never know the time of day a member trained.
# ---------------------------------------------------------------------------

def _revo_row(**kw):
    base = {
        "user_id": 42, "notify_channel_id": 7, "notify_guild_id": None,
        "last_checkin_date": None, "last_streak_weeks": None,
    }
    base.update(kw)
    return base


def _run_poll(monkeypatch, row, *, latest_iso, streak=3, now=None):
    """Drive _poll_one_account with the network + DB + Discord stubbed out.

    Returns (sent_texts, fetch_calls, saved_state).
    """
    import app.bot as bot

    now = now or datetime(2026, 7, 31, 14, 40, tzinfo=timezone.utc)
    sent, fetches, saved = [], [], []

    class _Chan:
        async def send(self, text, **kw):
            sent.append(text)

    class _Client:
        def get_streak_calendar(self, m, y):
            fetches.append(("calendar", m, y))
            if latest_iso is None:
                return {}
            d = datetime.strptime(latest_iso, "%Y-%m-%d")
            return {d.day: True} if (d.month, d.year) == (m, y) else {}

        def get_streak_weeks(self):
            fetches.append(("streak",))
            return streak

    class _Loop:
        # discord.Client.loop raises before the client connects, so the whole
        # bot object is stubbed rather than patched attribute-by-attribute.
        async def run_in_executor(self, executor, fn):
            return fn()

    class _StubBot:
        loop = _Loop()

        def get_channel(self, cid):
            return _Chan()

        async def fetch_channel(self, cid):  # pragma: no cover - not reached
            return _Chan()

    monkeypatch.setattr(bot, "DISPLAY_TZ", timezone.utc)
    monkeypatch.setattr(bot, "bot", _StubBot())
    monkeypatch.setattr(bot, "_client_for_user", lambda r: _Client())
    monkeypatch.setattr(bot.db, "update_revo_checkin_state",
                        lambda uid, cur, st: saved.append((uid, cur, st)))
    monkeypatch.setattr(bot.db, "list_revo_accounts", lambda: [row])
    monkeypatch.setattr(bot, "_bot_name", lambda uid, fallback: "Tester")

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    monkeypatch.setattr(bot, "datetime", _FixedDatetime)

    asyncio.run(bot._poll_one_account(row))
    return sent, fetches, saved


def test_poll_skips_the_round_trip_once_today_is_already_recorded(monkeypatch):
    """The cursor only advances to a NEWER day and the calendar has no
    sub-day resolution, so after today's check-in there is nothing to learn
    until midnight. Those requests are pure waste against a rate-limited
    portal — and skipping them is what makes a tight poll interval affordable."""
    row = _revo_row(last_checkin_date="2026-07-31")
    sent, fetches, saved = _run_poll(monkeypatch, row, latest_iso="2026-07-31")
    assert fetches == []       # no HTTP at all
    assert sent == []
    assert saved == []         # and no pointless DB write


def test_poll_announces_a_new_day_without_claiming_it_was_just_now(monkeypatch):
    """Revo batches attendance in, so a session surfaced at 14:40 may have
    happened at 6am. The wording must not claim "just checked in"."""
    row = _revo_row(last_checkin_date="2026-07-28", last_streak_weeks=3)
    sent, fetches, _saved = _run_poll(monkeypatch, row, latest_iso="2026-07-31")
    assert len(sent) == 1
    assert "trained at Revo today" in sent[0]
    assert "just checked in" not in sent[0]
    assert "<@42>" in sent[0]
    assert fetches  # it really did fetch this time


def test_poll_labels_an_older_visit_by_day(monkeypatch):
    row = _revo_row(last_checkin_date="2026-07-20")
    sent, _f, _s = _run_poll(monkeypatch, row, latest_iso="2026-07-30")
    assert "yesterday" in sent[0]
    assert "today" not in sent[0]


def test_poll_first_run_after_linking_is_silent(monkeypatch):
    """A freshly linked account must not replay its backfilled history."""
    row = _revo_row(last_checkin_date=None)
    sent, _f, saved = _run_poll(monkeypatch, row, latest_iso="2026-07-28")
    assert sent == []
    assert saved == [(42, "2026-07-28", 3)]  # baseline recorded


def test_poll_does_not_re_announce_the_same_day(monkeypatch):
    row = _revo_row(last_checkin_date="2026-07-28")
    sent, _f, _s = _run_poll(monkeypatch, row, latest_iso="2026-07-28")
    assert sent == []


def test_poll_cursor_never_goes_backwards(monkeypatch):
    """A calendar that momentarily reports an older day must not rewind the
    cursor, or the next poll would re-announce a visit already posted."""
    row = _revo_row(last_checkin_date="2026-07-28")
    _sent, _f, saved = _run_poll(monkeypatch, row, latest_iso="2026-07-20")
    assert saved == [(42, "2026-07-28", 3)]


def test_club_detail_pin_prefers_the_precise_coordinate():
    """PerfectGym rounds its coordinates — 25 of 77 pins land >200 m from the
    door and Pitt St nearly 1 km out. Netpulse carries 6-14 decimal places, so
    when its row is available the pin must come from there."""
    import app.bot as bot
    from app.revo_netpulse import Club
    from app.revo_perfectgym import ClubDirEntry

    entry = ClubDirEntry(id=1, name="Pitt St", address="188 Pitt St", city=None,
                         club_number=None, lat=-33.8788, lng=151.2072,
                         opening_date="2022-02-24T00:00:00", state="NSW")
    precise = Club(uuid="u", name="Pitt St Sydney", city=None, state=None,
                   mms="perfectgym", url=None, lat=-33.87025, lng=151.20805)

    with_np = bot._format_revo_club_detail(entry, [], [], precise)
    assert "-33.87025,151.20805" in with_np
    # Falls back to the PerfectGym coordinate when Netpulse has no row.
    without = bot._format_revo_club_detail(entry, [], [], None)
    assert "-33.8788,151.2072" in without
