import asyncio
import io
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
from app import bot as mod


def test_repair_preserves_mentions_fields_and_other_attachments(monkeypatch):
    embed = discord.Embed(title="20 weighed in", description="<@20> <@!20> weight unchanged")
    embed.set_footer(text="Home Assistant")
    embed.add_field(name="Metric", value="80 kg")
    old = SimpleNamespace(filename="bodyweight_20.png")
    other = SimpleNamespace(filename="keep.txt")
    msg = SimpleNamespace(id=100, author=SimpleNamespace(id=1), embeds=[embed],
                          attachments=[old, other], edit=AsyncMock())
    foreign = SimpleNamespace(id=99, author=SimpleNamespace(id=2), edit=AsyncMock())
    hevy = discord.Embed(title="Workout", url="https://hevy.com/workout/20")
    hevy.set_author(name="20 finished a Hevy workout")
    hevy.set_footer(text="via Hevy")
    workout = SimpleNamespace(id=98, author=SimpleNamespace(id=1), embeds=[hevy],
                              attachments=[], edit=AsyncMock())

    async def history(*, limit):
        for message in [msg, foreign, workout]:
            yield message
    channel = SimpleNamespace(guild=SimpleNamespace(id=10), history=history)
    monkeypatch.setattr(mod, "bot", SimpleNamespace(user=SimpleNamespace(id=1),
                                                   get_channel=lambda _: channel))
    monkeypatch.setattr(mod, "_refresh_linked_member_names", AsyncMock(return_value={(10, 20): "Josh"}))
    monkeypatch.setattr(mod, "db", SimpleNamespace(ha_latest_replies=lambda: [
        {"user_id": 20, "chart_message_id": 100}]))
    monkeypatch.setattr(mod, "HEVY_FEED_CHANNEL_ID", 55)
    monkeypatch.setattr(mod, "_ha_alert_channel_id", lambda: 55)
    monkeypatch.setattr(mod, "_updated_bodyweight_chart", AsyncMock(return_value=object()))
    monkeypatch.setattr(mod, "_bodyweight_chart_file", lambda _: discord.File(io.BytesIO(b"png"), filename="bodyweight_josh.png"))
    result = asyncio.run(mod._repair_integration_names())
    assert result == {"resolved": 1, "scanned": 3, "updated": 2, "charts": 1, "failed": 0}
    args = msg.edit.call_args.kwargs
    assert args["embeds"][0].title == "Josh weighed in"
    assert args["embeds"][0].description == "<@20> <@!20> weight unchanged"
    assert args["embeds"][0].fields[0].value == "80 kg"
    assert args["attachments"][0] is other
    assert args["attachments"][1].filename == "bodyweight_josh.png"
    assert not args["allowed_mentions"].users
    args = workout.edit.call_args.kwargs
    assert args["embeds"][0].author.name == "Josh finished a Hevy workout"
    assert args["embeds"][0].url == "https://hevy.com/workout/20"
    assert "attachments" not in args
    foreign.edit.assert_not_awaited()
