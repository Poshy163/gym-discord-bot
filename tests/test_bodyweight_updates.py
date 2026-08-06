"""Automatic bodyweight-chart delivery for manual and HA weigh-ins."""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app import bot as bot_mod  # noqa: E402


_next_user = iter(range(880_000, 880_999))
GUILD = 880_100


def _user(name: str = "Chart User"):
    return SimpleNamespace(
        id=next(_next_user),
        display_name=name,
        global_name=None,
        name=name,
    )


def _chart(name: str = "bodyweight_chart_user.png"):
    return bot_mod._BodyweightChart(
        buffer=io.BytesIO(b"png"),
        filename=name,
        entries=1,
        days=1,
        first_kg=80.0,
        latest_kg=80.0,
        latest_recorded_kg=80.0,
        delta_kg=0.0,
    )


def test_bodyweight_chart_factory_supports_the_first_reading(monkeypatch):
    person = _user("Sam / First")
    bot_mod.db.set_bodyweight(
        GUILD,
        person.id,
        80.0,
        recorded_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    captured = {}

    class _Matplotlib:
        @staticmethod
        def use(_backend):
            return None

    monkeypatch.setattr(
        bot_mod.importlib,
        "import_module",
        lambda name: _Matplotlib() if name == "matplotlib" else object(),
    )

    def _render(_plt, _mdates, _ticker, xs, ys, trend, **kwargs):
        captured.update(xs=xs, ys=ys, trend=trend, kwargs=kwargs)
        return io.BytesIO(b"png")

    monkeypatch.setattr(bot_mod, "_render_trend_chart", _render)
    chart = bot_mod._build_bodyweight_chart(person.id, person.display_name)

    assert chart is not None
    assert chart.entries == chart.days == 1
    assert chart.latest_kg == 80.0
    assert chart.filename == "bodyweight_sam_first.png"
    assert captured["ys"] == captured["trend"] == [80.0]
    assert "latest 80kg" in captured["kwargs"]["subtitle"]


def test_bodyweight_chart_labels_a_same_day_average_and_latest_reading(
    monkeypatch,
):
    person = _user("Two A Day")
    for hour, weight in ((0, 80.0), (8, 82.0)):
        bot_mod.db.set_bodyweight(
            GUILD,
            person.id,
            weight,
            recorded_at=datetime(2026, 8, 5, hour, tzinfo=timezone.utc),
        )
    captured = {}

    class _Matplotlib:
        @staticmethod
        def use(_backend):
            return None

    monkeypatch.setattr(
        bot_mod.importlib,
        "import_module",
        lambda name: _Matplotlib() if name == "matplotlib" else object(),
    )

    def _render(_plt, _mdates, _ticker, xs, ys, trend, **kwargs):
        captured.update(xs=xs, ys=ys, trend=trend, kwargs=kwargs)
        return io.BytesIO(b"png")

    monkeypatch.setattr(bot_mod, "_render_trend_chart", _render)
    chart = bot_mod._build_bodyweight_chart(person.id, person.display_name)

    assert chart is not None
    assert chart.entries == 2
    assert chart.days == 1
    assert chart.latest_kg == 81.0
    assert chart.latest_recorded_kg == 82.0
    assert "daily mean 81kg" in captured["kwargs"]["subtitle"]
    assert "latest logged 82kg" in captured["kwargs"]["subtitle"]


def test_bodyweight_chart_reads_saved_goal_and_builds_projection(monkeypatch):
    person = _user("Goal Setter")
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for day in range(30):
        bot_mod.db.set_bodyweight(
            GUILD,
            person.id,
            100.0 - day * 0.1,
            recorded_at=start + timedelta(days=day),
        )
    bot_mod.db.bodyweight_goal_set(person.id, person.display_name, 95.0)
    captured = {}

    class _Matplotlib:
        @staticmethod
        def use(_backend):
            return None

    monkeypatch.setattr(
        bot_mod.importlib,
        "import_module",
        lambda name: _Matplotlib() if name == "matplotlib" else object(),
    )

    def _render(_plt, _mdates, _ticker, _xs, _ys, _trend, **kwargs):
        captured.update(kwargs)
        return io.BytesIO(b"png")

    monkeypatch.setattr(bot_mod, "_render_trend_chart", _render)
    chart = bot_mod._build_bodyweight_chart(person.id, person.display_name)

    assert chart is not None
    series = captured["bodyweight"]
    assert series.goal_kg == 95.0
    assert series.goal_eta is not None
    assert series.projection_kg[-1] == 95.0
    assert captured["trend_label"] == "7-day EWMA trend"


def test_trend_chart_always_closes_figure_when_plotting_fails():
    fig = MagicMock()
    axis = MagicMock()
    axis.plot.side_effect = RuntimeError("plot failed")
    plt = MagicMock()
    plt.subplots.return_value = (fig, axis)

    with pytest.raises(RuntimeError, match="plot failed"):
        bot_mod._render_trend_chart(
            plt,
            MagicMock(),
            MagicMock(),
            [datetime(2026, 8, 5, tzinfo=timezone.utc)],
            [80.0],
            [80.0],
            title="Bodyweight",
            subtitle="one entry",
            trend_label="trend",
            trend_colour="#ffffff",
        )

    plt.close.assert_called_once_with(fig)


def test_chat_bodyweight_reply_includes_the_refreshed_graph(monkeypatch):
    person = _user()
    chart = _chart()
    chart_builder = AsyncMock(return_value=chart)
    monkeypatch.setattr(bot_mod, "_updated_bodyweight_chart", chart_builder)

    message = AsyncMock()
    message.guild = SimpleNamespace(id=GUILD)
    message.author = person
    message.channel = SimpleNamespace(id=123, name="gym")

    asyncio.run(
        bot_mod._handle_bodyweight_message(message, person, 80.0),
    )

    assert bot_mod.db.get_latest_bodyweight(GUILD, person.id)["weight_kg"] == 80.0
    chart_builder.assert_awaited_once_with(person.id, person.display_name)
    sent = message.reply.call_args.kwargs
    assert sent["file"].filename == chart.filename
    assert sent["mention_author"] is False


def test_chat_bodyweight_still_confirms_when_charting_is_unavailable(monkeypatch):
    person = _user()
    monkeypatch.setattr(
        bot_mod, "_updated_bodyweight_chart", AsyncMock(return_value=None),
    )
    message = AsyncMock()
    message.guild = SimpleNamespace(id=GUILD)
    message.author = person
    message.channel = SimpleNamespace(id=123, name="gym")

    asyncio.run(
        bot_mod._handle_bodyweight_message(message, person, 81.0),
    )

    assert bot_mod.db.get_latest_bodyweight(GUILD, person.id)["weight_kg"] == 81.0
    assert "Recorded bodyweight" in message.reply.call_args.args[0]
    assert "file" not in message.reply.call_args.kwargs


def test_slash_bodyweight_defers_and_includes_the_refreshed_graph(monkeypatch):
    person = _user()
    chart = _chart()
    monkeypatch.setattr(
        bot_mod, "_deny_invisible_target", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        bot_mod, "_updated_bodyweight_chart", AsyncMock(return_value=chart),
    )
    interaction = AsyncMock()
    interaction.user = person
    interaction.guild_id = GUILD

    asyncio.run(
        bot_mod.bodyweight_cmd.callback(interaction, 80.0),
    )

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    sent = interaction.followup.send.call_args
    assert "Recorded bodyweight" in sent.args[0]
    assert sent.kwargs["file"].filename == chart.filename
    assert bot_mod.db.get_latest_bodyweight(GUILD, person.id)["weight_kg"] == 80.0


def test_explicit_bodyweight_graph_reuses_the_shared_factory(monkeypatch):
    person = _user()
    chart = _chart()
    monkeypatch.setattr(
        bot_mod, "_deny_invisible_target", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        bot_mod, "_build_bodyweight_chart", lambda *_args: chart,
    )
    interaction = AsyncMock()
    interaction.user = person

    asyncio.run(bot_mod.bodyweight_graph_cmd.callback(interaction))

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    sent = interaction.followup.send.call_args.kwargs
    assert sent["file"].filename == chart.filename


def test_home_assistant_announcement_includes_one_refreshed_graph(monkeypatch):
    person = _user()
    bot_mod.db.ha_link(person.id, GUILD, "chart_user")
    chart = _chart()
    chart_builder = AsyncMock(return_value=chart)
    channel = AsyncMock()
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 0)
    monkeypatch.setattr(bot_mod, "_updated_bodyweight_chart", chart_builder)
    monkeypatch.setattr(
        bot_mod, "_ha_alert_channel", AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(bot_mod.bot, "get_guild", lambda _gid: None)
    measured = "2026-08-05T03:00:00+00:00"
    states = [{
        "entity_id": "sensor.chart_user_weight",
        "state": "80.0",
        "attributes": {"unit_of_measurement": "kg"},
        "last_changed": measured,
        "last_updated": measured,
    }]

    result = asyncio.run(
        bot_mod._ha_sync_account(bot_mod.db.ha_get(person.id), states),
    )

    assert result["new"] == 1
    chart_builder.assert_awaited_once()
    assert channel.send.await_count == 1
    sent = channel.send.call_args.kwargs
    assert sent["file"].filename == chart.filename
    assert sent["allowed_mentions"].everyone is False


def test_home_assistant_backfill_summary_gets_one_graph(monkeypatch):
    person = _user()
    bot_mod.db.ha_link(person.id, GUILD, "backfill_user")
    chart = _chart()
    chart_builder = AsyncMock(return_value=chart)
    posted = AsyncMock()
    posted.id = 880_501
    channel = AsyncMock()
    channel.send.return_value = posted
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)
    monkeypatch.setattr(bot_mod, "_updated_bodyweight_chart", chart_builder)
    monkeypatch.setattr(
        bot_mod, "_ha_alert_channel", AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(bot_mod.bot, "get_guild", lambda _gid: None)
    entries = [
        {
            "measurement_id": f"backfill-{day}",
            "timestamp": f"2026-08-0{day}T03:00:00+00:00",
            "weight": weight,
            "weight_unit": "kg",
        }
        for day, weight in ((3, 82.0), (4, 81.5), (5, 81.0))
    ]
    states = [{
        "entity_id": "sensor.backfill_user_weight",
        "state": "81.0",
        "attributes": {
            "unit_of_measurement": "kg",
            "weight_history": entries,
        },
        "last_changed": entries[-1]["timestamp"],
        "last_updated": entries[-1]["timestamp"],
    }]

    result = asyncio.run(
        bot_mod._ha_sync_account(bot_mod.db.ha_get(person.id), states),
    )

    assert result["new"] == 3
    assert channel.send.await_count == 1
    assert channel.send.call_args.kwargs["file"].filename == chart.filename
    chart_builder.assert_awaited_once()


def test_home_assistant_multi_reading_poll_attaches_graph_only_to_newest(monkeypatch):
    person = _user()
    bot_mod.db.ha_link(person.id, GUILD, "multi_user")
    bot_mod.db.ha_mark_synced(person.id)
    bot_mod.db.ha_mark_backfilled(person.id)
    chart = _chart()
    chart_builder = AsyncMock(return_value=chart)
    first_post, second_post = AsyncMock(), AsyncMock()
    first_post.id, second_post.id = 880_502, 880_503
    channel = AsyncMock()
    channel.send.side_effect = [first_post, second_post]
    monkeypatch.setattr(bot_mod, "_updated_bodyweight_chart", chart_builder)
    monkeypatch.setattr(
        bot_mod, "_ha_alert_channel", AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(bot_mod.bot, "get_guild", lambda _gid: None)
    entries = [
        {
            "measurement_id": f"routine-{day}",
            "timestamp": f"2026-08-0{day}T03:00:00+00:00",
            "weight": weight,
            "weight_unit": "kg",
        }
        for day, weight in ((4, 81.5), (5, 81.0))
    ]
    states = [{
        "entity_id": "sensor.multi_user_weight",
        "state": "81.0",
        "attributes": {
            "unit_of_measurement": "kg",
            "weight_history": entries,
        },
        "last_changed": entries[-1]["timestamp"],
        "last_updated": entries[-1]["timestamp"],
    }]

    result = asyncio.run(
        bot_mod._ha_sync_account(bot_mod.db.ha_get(person.id), states),
    )

    assert result["new"] == 2
    assert channel.send.await_count == 2
    assert "file" not in channel.send.call_args_list[0].kwargs
    assert channel.send.call_args_list[1].kwargs["file"].filename == chart.filename
    chart_builder.assert_awaited_once()
    first_tracking = bot_mod.db.ha_get_reply(first_post.id)
    second_tracking = bot_mod.db.ha_get_reply(second_post.id)
    assert first_tracking["chart_message_id"] == second_post.id
    assert second_tracking["chart_message_id"] == second_post.id


def test_home_assistant_partial_announcement_batch_still_tracks_undo(monkeypatch):
    person = _user()
    bot_mod.db.ha_link(person.id, GUILD, "partial_user")
    bot_mod.db.ha_mark_synced(person.id)
    bot_mod.db.ha_mark_backfilled(person.id)
    first_post = AsyncMock()
    first_post.id = 880_504
    response = SimpleNamespace(status=500, reason="Server Error", headers={})
    rejected = discord.HTTPException(
        response, {"message": "send failed", "code": 0},
    )
    channel = AsyncMock()
    channel.send.side_effect = [first_post, rejected]
    monkeypatch.setattr(
        bot_mod, "_updated_bodyweight_chart", AsyncMock(return_value=_chart()),
    )
    monkeypatch.setattr(
        bot_mod, "_ha_alert_channel", AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(bot_mod.bot, "get_guild", lambda _gid: None)
    entries = [
        {
            "measurement_id": f"partial-{day}",
            "timestamp": f"2026-08-0{day}T03:00:00+00:00",
            "weight": weight,
            "weight_unit": "kg",
        }
        for day, weight in ((4, 81.5), (5, 81.0))
    ]
    states = [{
        "entity_id": "sensor.partial_user_weight",
        "state": "81.0",
        "attributes": {
            "unit_of_measurement": "kg",
            "weight_history": entries,
        },
        "last_changed": entries[-1]["timestamp"],
        "last_updated": entries[-1]["timestamp"],
    }]

    result = asyncio.run(
        bot_mod._ha_sync_account(bot_mod.db.ha_get(person.id), states),
    )

    assert result["new"] == 2
    assert channel.send.await_count == 2
    tracking = bot_mod.db.ha_get_reply(first_post.id)
    assert tracking is not None
    assert tracking["chart_message_id"] is None
    first_post.add_reaction.assert_awaited_once()


def test_home_assistant_without_an_alert_channel_does_not_render(monkeypatch):
    person = _user()
    bot_mod.db.ha_link(person.id, GUILD, "silent_user")
    chart_builder = AsyncMock(return_value=_chart())
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 0)
    monkeypatch.setattr(bot_mod, "_updated_bodyweight_chart", chart_builder)
    monkeypatch.setattr(
        bot_mod, "_ha_alert_channel", AsyncMock(return_value=None),
    )
    measured = "2026-08-05T04:00:00+00:00"
    states = [{
        "entity_id": "sensor.silent_user_weight",
        "state": "81.0",
        "attributes": {"unit_of_measurement": "kg"},
        "last_changed": measured,
        "last_updated": measured,
    }]

    result = asyncio.run(
        bot_mod._ha_sync_account(bot_mod.db.ha_get(person.id), states),
    )

    assert result["new"] == 1
    chart_builder.assert_not_awaited()


def test_ha_announcement_retries_without_graph_when_attachments_are_blocked():
    response = SimpleNamespace(status=403, reason="Forbidden", headers={})
    rejected = discord.HTTPException(
        response, {"message": "Missing Permissions", "code": 50013},
    )
    posted = AsyncMock()
    channel = AsyncMock()
    channel.send.side_effect = [rejected, posted]
    embed = discord.Embed(title="Weighed in")

    result = asyncio.run(
        bot_mod._ha_send_announcement(channel, embed, _chart()),
    )

    assert result is posted
    assert channel.send.await_count == 2
    assert "file" in channel.send.call_args_list[0].kwargs
    assert "file" not in channel.send.call_args_list[1].kwargs
