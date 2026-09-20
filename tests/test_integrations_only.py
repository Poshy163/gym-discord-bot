"""The restricted profile stops side effects, not just navigation links."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from app import config, features, webui
from app.db import Database
from app.secretbox import SecretBox
from app.settings_service import SettingsService


def test_profile_overrides_other_switches_without_erasing_them():
    cfg = config.load(env={"INTEGRATIONS_ONLY": "true", "HA_DISABLED": "false",
                           "REMINDER_CHANNEL_ID": "123",
                           "ENABLE_MESSAGE_LOGGING": "true"})
    assert cfg["HA_DISABLED"] is True
    assert cfg["ENABLE_MESSAGE_LOGGING"] is False
    assert cfg["BODYWEIGHT_REMINDER_CHANNEL_ID"] is None
    assert cfg["WEEKLY_REPORT_CHANNEL_ID"] is None
    assert cfg.raw("REMINDER_CHANNEL_ID") == "123"
    assert cfg["HEVY_DISABLED"] is False
    assert cfg["STRAVA_DISABLED"] is False


def test_restricted_routes_settings_and_history_preservation(tmp_path):
    async def run():
        path = tmp_path / "gym.sqlite3"
        db = Database(path)
        settings = SettingsService(db, SecretBox.open_at(str(path)), {})
        settings.set("INTEGRATIONS_ONLY", "true", actor="test")
        db.set_guild_meta(1, "Test Gym", 1)
        db.upsert_member(1, 2, "member", "Test Member")
        db.hevy_link(2, 1, "private-hevy-token")
        db.hevy_link(3, 99, "other-guild-token")
        db.link_strava_account(2, 123, "private-access-token", "private-refresh-token",
                               123456, "activity:read", "Athlete")
        db.meta_set("preserved-history-marker", "untouched")
        app = webui.build_app(db=db, password="test", settings=settings)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/calories?guild=1")).status == 401
            await client.post("/login", data={"password": "test"})
            for path in ("/calories", "/protein", "/members/1", "/activity",
                         "/api/calories?guild=1", "/api/nutrition/targets",
                         "/api/messages/log?guild=1", "/api/roles?guild=1"):
                response = await client.get(path)
                assert response.status == 404, path
            response = await client.post("/api/nutrition/targets", json={})
            assert response.status == 404
            for path in ("/overview", "/hevy", "/strava", "/settings"):
                response = await client.get(path)
                assert response.status == 200
                assert "const INTEGRATIONS_ONLY=true;" in await response.text()
            data = await (await client.get("/api/settings")).json()
            keys = {item["key"] for g in data["groups"] for item in g["items"]}
            assert {"INTEGRATIONS_ONLY", "HEVY_DISABLED", "STRAVA_DISABLED"} <= keys
            assert not keys & features.HIDDEN_SETTINGS
            assert "REVO_DISABLED" not in keys
            overview = await (await client.get("/api/overview?guild=1")).json()
            assert overview["hevy_enabled"] and overview["strava_enabled"]
            assert len(overview["hevy"]) == len(overview["strava"]) == 1
            assert set(overview["hevy"][0]) == {"name", "linked_at"}
            assert set(overview["strava"][0]) == {"name", "linked_at"}
            assert "token" not in str(overview)
            assert "live" not in overview and "recent_audit" not in overview
            assert db.meta_get("preserved-history-marker") == "untouched"
            assert db.hevy_get(2)["api_key_enc"] == "private-hevy-token"
            settings.set("INTEGRATIONS_ONLY", "false", actor="test")
            assert (await client.get("/calories")).status == 200
        db.close()
    asyncio.run(run())


def test_chat_and_old_commands_cannot_write_in_restricted_mode(monkeypatch):
    os.environ.setdefault("DB_PATH", ":memory:")
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    from app import bot
    monkeypatch.setattr(bot, "INTEGRATIONS_ONLY", True)
    database = MagicMock()
    monkeypatch.setattr(bot, "db", database)

    async def run():
        # Deliberately featureless inputs: the mode must return before reading
        # any message, downloading attachments, or writing tracking data.
        await bot.on_message(object())
        await bot.on_message_edit(object(), object())
        await bot.on_raw_reaction_add(object())
        interaction = SimpleNamespace(
            type=None, data={"name": "calories"}, response=AsyncMock(),
        )
        assert not await bot._tree_interaction_check(interaction)
        interaction.response.send_message.assert_awaited_once()
        assert not database.mock_calls
    asyncio.run(run())


def test_command_sync_removes_disabled_commands(monkeypatch):
    os.environ.setdefault("DB_PATH", ":memory:")
    os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")
    import discord
    from discord import app_commands

    from app import bot

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    async def callback(interaction):
        pass

    for name in ("hevy", "strava_status", "calories", "protein", "coach"):
        tree.add_command(app_commands.Command(name=name, description=name, callback=callback))
    monkeypatch.setattr(bot, "bot", SimpleNamespace(tree=tree))
    monkeypatch.setattr(bot, "INTEGRATIONS_ONLY", True)
    monkeypatch.setattr(bot, "DEV_GUILD", None)
    monkeypatch.setattr(bot, "COMMAND_SCOPE", "global")
    monkeypatch.setattr(tree, "sync", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot, "db", MagicMock())
    asyncio.run(bot._sync_commands(force=True))
    assert {c.name for c in tree.get_commands()} == {"hevy", "strava_status"}
    tree.sync.assert_awaited_once()
