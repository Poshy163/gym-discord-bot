"""Tests for the shared presentation layer.

``app/ui.py`` is deliberately pure so the rules it encodes can be asserted
directly. The ones worth guarding hardest are the two that caused real bugs:
the target/ceiling polarity split, and staying inside Discord's hard limits.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
import pytest

from app import ui


def cal(v: float) -> str:
    return f"{round(v):,} cal"


def g(v: float) -> str:
    return f"{round(v)} g"


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------

def test_score_target_and_ceiling_are_mirror_images():
    """The whole point of having two scorers: at the same fraction of the
    number, 'reaching it' and 'staying under it' must disagree."""
    # Low: bad when you're chasing a target, good when you're avoiding a max.
    assert ui.score_target(500, 2500) == ui.WARNING
    assert ui.score_ceiling(50, 250) == ui.SUCCESS
    # At the number: the win for a target, the edge for a ceiling.
    assert ui.score_target(2500, 2500) == ui.SUCCESS
    assert ui.score_ceiling(250, 250) == ui.WARNING
    # Past it: overshoot for a target, a real breach for a ceiling.
    assert ui.score_target(4000, 2500) == ui.WARNING
    assert ui.score_ceiling(400, 250) == ui.DANGER


def test_score_target_tolerates_a_small_overshoot():
    """Landing a few percent over a calorie goal is hitting it, not missing."""
    assert ui.score_target(2600, 2500) == ui.SUCCESS   # +4%
    assert ui.score_target(2749, 2500) == ui.SUCCESS   # +10%, inside the band
    assert ui.score_target(2800, 2500) == ui.WARNING   # +12%, outside


def test_scorers_treat_an_absent_target_as_unscorable():
    assert ui.score_target(1200, 0) == ui.BRAND
    assert ui.score_ceiling(1200, 0) == ui.BRAND


def test_score_trend_inverts_for_metrics_where_down_is_good():
    assert ui.score_trend(2.5) == ui.SUCCESS
    assert ui.score_trend(-2.5) == ui.DANGER
    assert ui.score_trend(-2.5, good="down") == ui.SUCCESS   # losing weight
    assert ui.score_trend(2.5, good="down") == ui.DANGER
    assert ui.score_trend(0) == ui.NEUTRAL


def test_score_streak_escalates_and_tops_out_at_gold():
    assert ui.score_streak(0) == ui.NEUTRAL
    assert ui.score_streak(2) == ui.BRAND
    assert ui.score_streak(5) == ui.WARNING
    assert ui.score_streak(20) == ui.DANGER
    assert ui.score_streak(40) == ui.GOLD


def test_score_age_flags_stale_data():
    assert ui.score_age(3) == ui.NEUTRAL
    assert ui.score_age(70) == ui.WARNING
    assert ui.score_age(200) == ui.DANGER


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def test_bar_keeps_a_fixed_cell_count_so_stacked_rows_align():
    for value in (0, 250, 500, 1000, 4000):
        assert len(ui.bar(value, 1000, width=10)) == 10


def test_bar_is_always_exactly_width_cells():
    """Bars stack inside fenced blocks. A bar that grows when a value goes over
    target makes every column to its right ragged — and an earlier version that
    appended ``█`` past the end also mixed glyph families, so the overflow
    rendered as full-height blocks towering over the short ``▰``/``▱`` line."""
    for value in (0, 90, 180, 200, 500, 100_000):
        assert len(ui.bar(value, 180, width=10)) == 10
    assert len(ui.bar(9999, 100, width=12)) == 12
    # Only the two parallelogram glyphs are ever emitted.
    assert set(ui.bar(500, 180, width=10)) <= {ui.FILL, ui.TRACK}


def test_over_by_carries_the_magnitude_a_clamped_bar_cannot():
    """10-over and 2,400-over must not read identically — that's the whole
    signal a ceiling tracker gives. The bar can't say it, so this column does."""
    assert ui.over_by(190, 180) == "+10"
    assert ui.over_by(2580, 180) == "+2,400"
    assert ui.over_by(180, 180) == ""      # exactly at the max is not over
    assert ui.over_by(90, 180) == ""
    assert ui.over_by(90, 0) == ""         # no ceiling set
    assert ui.over_by(190, 180, g) == "+10 g"


def test_meter_still_distinguishes_a_small_overshoot_from_a_huge_one():
    """The property the removed overflow cells were carrying, now carried by
    the percentage and the absolute number instead."""
    small, _ = ui.meter(190, 180, g, ceiling=True)
    huge, _ = ui.meter(2580, 180, g, ceiling=True)
    assert small != huge
    assert "106%" in small and "1,433%" in huge
    assert "10 g" in small and "2400 g" in huge


def test_bar_with_no_target_is_all_track():
    assert ui.bar(500, 0, width=8) == ui.TRACK * 8


def test_sparkline_and_strip():
    assert ui.sparkline([]) == ""
    assert ui.sparkline([5, 5, 5]) == SPARK_MID * 3
    spark = ui.sparkline([1, 2, 3, 4])
    assert spark[0] == ui.SPARK[0] and spark[-1] == ui.SPARK[-1]
    assert ui.sparkline(list(range(100)), width=10) != ""
    assert len(ui.sparkline(list(range(100)), width=10)) == 10
    # strip keeps the most recent entries, right-aligned in time.
    assert ui.strip([True, False, True], width=14) == f"{ui.FILL}{ui.TRACK}{ui.FILL}"
    assert len(ui.strip([True] * 40, width=14)) == 14


SPARK_MID = ui.SPARK[len(ui.SPARK) // 2]


# ---------------------------------------------------------------------------
# meter — the shared renderer
# ---------------------------------------------------------------------------

def test_meter_target_wording_and_colour():
    text, colour = ui.meter(1700, 2500, cal)
    assert "**68%**" in text
    assert "1,700 cal / 2,500 cal" in text
    assert "**800 cal** left today" in text
    assert colour == ui.WARNING       # still climbing
    assert text.splitlines()[1].startswith("-# ")   # detail is second-tier


def test_meter_ceiling_says_headroom_not_left_today():
    text, colour = ui.meter(96, 180, g, ceiling=True, label="headroom")
    assert "**84 g** headroom" in text
    assert "left today" not in text
    assert colour == ui.SUCCESS       # comfortably under a max is the win


def test_meter_warns_on_both_sides_of_the_split():
    """The old pair only warned for protein; calories going over said nothing.
    One renderer means the glyph can't go missing on one of them again."""
    over_target, _ = ui.meter(3000, 2500, cal)
    over_ceiling, _ = ui.meter(220, 180, g, ceiling=True)
    assert ui.CAUTION in over_target
    assert ui.CAUTION in over_ceiling
    assert "over target" in over_target
    assert "over your max" in over_ceiling


def test_meter_full_bar_means_opposite_things_and_is_coloured_accordingly():
    """The bug this module was written to kill: a full bar is praise for a
    target and a warning for a ceiling."""
    _, target_colour = ui.meter(2500, 2500, cal)
    _, ceiling_colour = ui.meter(180, 180, g, ceiling=True)
    assert target_colour == ui.SUCCESS
    assert ceiling_colour == ui.WARNING
    assert target_colour != ceiling_colour


def test_meter_without_a_target_does_not_divide_by_zero():
    text, colour = ui.meter(1200, 0, cal)
    assert "1,200 cal" in text
    assert "%" not in text            # nothing to be a percentage of
    assert "left today" not in text
    assert colour == ui.BRAND


def test_meter_accepts_a_trailing_note():
    text, _ = ui.meter(100, 200, cal, note="(weekend targets)")
    assert text.endswith("(weekend targets)")


# ---------------------------------------------------------------------------
# Numbers and text
# ---------------------------------------------------------------------------

def test_kg_and_num_round_and_separate_thousands():
    assert ui.kg(82.5) == "82.5kg"
    assert ui.kg(80.0) == "80kg"
    assert ui.kg(1250) == "1,250kg"
    assert ui.kg(20, bw=True) == "BW+20kg"
    # The float that motivated this: 1712.7659574468084 via '{:g}'.
    assert ui.num(1712.7659574468084, "kg") == "1,713 kg"
    assert ui.num(2491, "cal/day") == "2,491 cal/day"


def test_delta_keeps_the_sign_but_flips_the_colour():
    assert ui.delta(2.5) == f"{ui.UP} +2.5kg"
    assert ui.delta(-2.5) == f"{ui.DOWN} -2.5kg"
    assert ui.delta(0) == f"{ui.FLAT} no change"
    # Losing bodyweight is favourable, but the number is still negative.
    assert ui.delta(-2.5, good="down") == f"{ui.UP} -2.5kg"
    assert ui.delta(2.5, good="down") == f"{ui.DOWN} +2.5kg"


def test_pct_handles_an_unscorable_total():
    assert ui.pct(50, 200) == "25%"
    assert ui.pct(1, 0) == "—"


def test_rank_is_two_cells_and_mono_avoids_medals():
    assert ui.rank(0) == "🥇"
    assert ui.rank(3) == "` 4`"
    # Inside a fence a medal would not be one cell, so mono mode is plain.
    assert ui.rank(0, mono=True) == " 1."
    assert ui.rank(9, mono=True) == "10."
    assert all(not any(ch in ui.rank(i, mono=True) for ch in ui.MEDALS)
               for i in range(5))


def test_plural_separates_thousands():
    assert ui.plural(1, "club") == "1 club"
    assert ui.plural(2, "club") == "2 clubs"
    assert ui.plural(1200, "entry", "entries") == "1,200 entries"


# ---------------------------------------------------------------------------
# table
# ---------------------------------------------------------------------------

def test_table_is_fenced_and_columns_line_up():
    out = ui.table([["Mon", "1,850 cal"], ["Tuesday", "900 cal"]], align="<>")
    assert out.startswith("```\n") and out.endswith("\n```")
    body = [ln for ln in out.splitlines() if ln not in ("```",)]
    # Same column start on every row is the property the fence exists for.
    assert len({ln.index("cal") for ln in body}) == 1


def test_table_drops_whole_rows_and_says_so():
    out = ui.table([[str(i)] for i in range(50)], max_rows=5)
    assert "… 45 more" in out
    assert out.count("\n") <= 8


def test_table_of_nothing_is_nothing():
    assert ui.table([]) == ""


# ---------------------------------------------------------------------------
# Embed construction and limits
# ---------------------------------------------------------------------------

class _FakeAsset:
    url = "https://cdn.example/avatar.png"


class _FakeMember:
    id = 7
    display_name = "Poshy"
    display_avatar = _FakeAsset()


def test_card_puts_a_member_in_the_author_row_with_their_avatar():
    e = ui.card("📊 Personal bests", member=_FakeMember())
    assert e.author.name == "Poshy"
    assert e.author.icon_url == _FakeAsset.url
    # One face per card: the thumbnail stays free for a *different* entity.
    assert e.thumbnail.url is None


def test_card_timestamp_is_opt_in():
    assert ui.card("x").timestamp is None
    assert ui.card("x", timestamp=True).timestamp is not None
    fixed = datetime(2026, 7, 27, tzinfo=timezone.utc)
    assert ui.card("x", timestamp=fixed).timestamp == fixed


def test_card_clips_every_field_to_its_discord_limit():
    e = ui.card("T" * 500, description="D" * 5000, footer="F" * 3000)
    assert ui.overflows(e) is None


def test_block_skips_empty_values_so_callers_need_no_guard():
    e = ui.card("t")
    ui.block(e, "Try this", "")
    ui.block(e, "Real", "content")
    assert [f.name for f in e.fields] == ["Real"]


def test_tiles_pads_to_a_full_row_of_three():
    """A lone inline field renders as a narrow column with dead space, which
    reads as broken — so trailing slots get blank spacers."""
    e = ui.card("t")
    ui.tiles(e, ("A", "1"))
    assert len(e.fields) == 3
    assert all(f.inline for f in e.fields)

    e2 = ui.card("t")
    ui.tiles(e2, ("A", "1"), ("B", "2"), ("C", "3"))
    assert len(e2.fields) == 3       # already whole, nothing added


def test_columns_splits_a_long_list_and_stays_inside_the_field_cap():
    lines = [f"• **Club {i:02d}** — Suburb {i:02d} (42 in club now)"
             for i in range(37)]
    e = ui.card("directory")
    ui.columns(e, lines)
    assert len(e.fields) == 3
    assert ui.overflows(e) is None
    joined = "\n".join(f.value for f in e.fields)
    assert joined.count("• **Club ") == 37     # nothing lost in the split


def test_columns_keeps_a_short_list_as_one_block():
    e = ui.card("d")
    ui.columns(e, ["a", "b", "c"])
    assert len(e.fields) == 1
    assert e.fields[0].inline is False


def test_overflows_detects_each_limit():
    e = ui.card("t")
    e.add_field(name="n", value="v" * (ui.FIELD_VALUE_LIMIT + 1))
    assert "value" in ui.overflows(e)

    e2 = discord.Embed(title="ok")
    for i in range(ui.FIELD_COUNT_LIMIT + 1):
        e2.add_field(name=str(i), value="x")
    assert "fields" in ui.overflows(e2)

    assert ui.overflows(ui.card("fine", description="short")) is None


def test_fit_prefers_a_line_break():
    text = "\n".join(f"line {i}" for i in range(300))
    out = ui.fit(text, 200)
    assert len(out) <= 200
    assert out.endswith("…")
    assert out.splitlines()[-2].startswith("line ")


# ---------------------------------------------------------------------------
# Null and failure builders
# ---------------------------------------------------------------------------

def test_empty_is_grey_and_always_offers_a_way_out():
    e = ui.empty("No lifts logged yet", hint="Post `bench 80kg`.", cmd="/log")
    assert e.colour == ui.NEUTRAL           # absence is not an error
    assert e.title.startswith(ui.EMPTY)
    assert e.footer.text == "/log"


def test_denied_and_unavailable_are_neutral_not_red():
    assert ui.denied("Admins only.").colour == ui.NEUTRAL
    assert ui.unavailable("Revo", why="Nobody linked an account.").colour == ui.NEUTRAL


def test_error_separates_what_the_user_can_fix_from_what_they_cannot():
    e = ui.error("Broke", "detail", fix="Retry.", admin="Set the key.",
                 diagnostic="v1")
    assert e.colour == ui.DANGER
    assert [f.name for f in e.fields] == ["Try this", "For admins"]
    assert e.footer.text == "v1"


def test_ok_stays_plain_text():
    """A one-line success in an embed is over-serving."""
    assert ui.ok("Renamed 4 entries") == f"{ui.OK} Renamed 4 entries"
    assert isinstance(ui.ok("x"), str)


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------

def test_chunk_splits_on_line_boundaries_under_the_message_cap():
    text = "\n".join(f"• line {i}" for i in range(600))
    parts = ui.chunk(text)
    assert len(parts) > 1
    assert all(len(p) <= ui.MESSAGE_LIMIT for p in parts)
    # Split cleanly: no line is torn in half across the boundary.
    assert sum(p.count("• line ") for p in parts) == 600


def test_chunk_leaves_short_text_alone():
    assert ui.chunk("hello") == ["hello"]


def test_chunk_hard_splits_a_single_overlong_line():
    parts = ui.chunk("x" * 5000)
    assert all(len(p) <= ui.MESSAGE_LIMIT for p in parts)
    assert "".join(parts) == "x" * 5000


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def test_ts_treats_naive_iso_as_utc():
    """Every logged_at in this schema is UTC; a naive one is not local time."""
    naive = ui.ts("2026-07-27T12:00:00")
    aware = ui.ts("2026-07-27T12:00:00+00:00")
    assert naive == aware


def test_when_renders_a_discord_timestamp_and_degrades_quietly():
    assert ui.when("2026-07-27T12:00:00+00:00").startswith("<t:")
    assert ui.when("not a date") == "—"
    assert ui.when(None) == "—"
    assert ui.day("2026-07-27T12:00:00+00:00").endswith(":D>")


# ---------------------------------------------------------------------------
# preview fixtures
# ---------------------------------------------------------------------------

def test_preview_cases_all_render_inside_discord_limits():
    """The preview page doubles as a smoke test — if a helper starts emitting
    something Discord would reject, this fails before a user sees a 400."""
    cases = ui.preview_cases()
    assert cases
    for title, items in cases:
        for item in items:
            if isinstance(item, discord.Embed):
                assert ui.overflows(item) is None, f"{title}: {ui.overflows(item)}"


# ---------------------------------------------------------------------------
# The card builders in app.bot that can be exercised without an Interaction.
# Each one is a send that would 400 in production if it overflowed.
# ---------------------------------------------------------------------------

def _bot():
    import os
    os.environ.setdefault("DB_PATH", ":memory:")
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    import app.bot as bot
    return bot


def test_shared_empty_and_gate_builders_are_within_limits():
    bot = _bot()
    builders = [
        bot._no_lifts_embed("Poshy"),
        bot._no_history_embed("incline bench press", "Poshy"),
        bot._revo_off_embed(),
        bot._revo_missing_deps_embed(),
        bot._revo_missing_deps_embed(crypto=True),
        bot._presence_disabled_embed(),
        bot._voice_disabled_embed(),
        bot._calories_not_set_embed(),
        bot._calories_not_set_embed("Cookie Monster"),
        bot._protein_not_set_embed(),
        bot._protein_not_set_embed("Dos"),
    ]
    for e in builders:
        assert ui.overflows(e) is None, f"{e.title}: {ui.overflows(e)}"
        assert e.colour == ui.NEUTRAL, f"{e.title} should read as absence"


def test_gate_embeds_keep_configuration_out_of_member_facing_prose():
    """Env var names are an admin's lever. A member who sees one can't act on
    it, so it belongs in the 'For admins' field, never the description."""
    bot = _bot()
    for embed, var in (
        (bot._revo_off_embed(), "REVO_DISABLED"),
        (bot._presence_disabled_embed(), "ENABLE_PRESENCE_TRACKING"),
        (bot._voice_disabled_embed(), "ENABLE_VOICE_TRACKING"),
    ):
        assert var not in (embed.description or ""), embed.title
        admin = " ".join(f.value for f in embed.fields if f.name == "For admins")
        assert var in admin, embed.title


def test_no_lifts_embed_escapes_a_hostile_display_name():
    bot = _bot()
    e = bot._no_lifts_embed("@everyone **boom**")
    assert "@everyone" not in e.title
    assert "**boom**" not in e.title


def test_personal_bests_card_survives_a_long_roster():
    """Jaidyn has 31 distinct exercises; the card must not blow the description
    budget as that grows."""
    bot = _bot()

    class _Row(dict):
        def keys(self):
            return super().keys()

    rows = [
        _Row(equipment=f"a rather long exercise name {i}", best=100 + i,
             bw=0, set_on="2026-07-20T00:00:00+00:00")
        for i in range(40)
    ]

    class _M:
        id = 1
        display_name = "Jaidyn"
        display_avatar = type("A", (), {"url": "https://x/y.png"})()

    e = bot._personal_bests_card(0, _M(), rows)
    assert ui.overflows(e) is None


def test_help_sections_each_fit_and_cover_every_category():
    bot = _bot()
    sections = bot._help_sections()
    assert "Overview" in sections
    for name, embed in sections.items():
        assert ui.overflows(embed) is None, f"{name}: {ui.overflows(embed)}"
        # The old two-embed form was 6,440 against a 6,000 message cap; every
        # paged section should sit far below it with room to grow.
        assert len(embed) < 2000, f"{name} is {len(embed)}"
    # Discord's Select maxes out at 25 options.
    assert len(sections) <= 25
