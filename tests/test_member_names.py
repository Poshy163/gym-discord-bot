import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from app.db import Database
from app.member_names import MemberNames


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "names.sqlite3")
    yield database
    database.close()


def member(name="Josh"):
    return SimpleNamespace(display_name=name, name="account-name", bot=False,
                           display_avatar=SimpleNamespace(url="https://example.com/avatar"))


def client(guild=None):
    return SimpleNamespace(get_guild=lambda _: guild, is_ready=lambda: True,
                           get_user=lambda _: None, fetch_user=AsyncMock(return_value=member()))


def test_empty_member_cache_fetches_server_nickname_and_persists(db):
    guild = SimpleNamespace(get_member=lambda _: None, fetch_member=AsyncMock(return_value=member()))
    bot = client(guild)
    resolver = MemberNames()
    assert asyncio.run(resolver.resolve(bot, db, 10, 20)) == "Josh"
    assert db.get_member(10, 20)["display_name"] == "Josh"
    assert asyncio.run(resolver.resolve(bot, db, 10, 20)) == "Josh"
    guild.fetch_member.assert_awaited_once_with(20)
    bot.fetch_user.assert_not_awaited()


def test_fetched_nickname_refreshes_when_cache_expires(db):
    guild = SimpleNamespace(get_member=lambda _: None,
                            fetch_member=AsyncMock(side_effect=[member("Old"), member("New")]))
    resolver = MemberNames()
    bot = client(guild)
    assert asyncio.run(resolver.resolve(bot, db, 10, 20)) == "Old"
    resolver.cache[(10, 20)] = (0, "Old")
    assert asyncio.run(resolver.resolve(bot, db, 10, 20)) == "New"
    assert db.get_member(10, 20)["display_name"] == "New"


def test_server_nicknames_do_not_leak_between_guilds(db):
    resolver = MemberNames()
    bot = client()
    bot.get_guild = lambda gid: SimpleNamespace(get_member=lambda _: member(f"Name {gid}"))
    assert asyncio.run(resolver.resolve(bot, db, 1, 20)) == "Name 1"
    assert asyncio.run(resolver.resolve(bot, db, 2, 20)) == "Name 2"


def test_failed_fetch_uses_saved_identity_without_marking_members_present(db):
    db.upsert_member(10, 20, "account", "Saved nickname", present=False)
    error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
    guild = SimpleNamespace(get_member=lambda _: None, fetch_member=AsyncMock(side_effect=error))
    assert asyncio.run(MemberNames().resolve(client(guild), db, 10, 20)) == "Saved nickname"
    assert not db.get_member(10, 20)["present"]


def test_no_guild_uses_account_display_name_without_inventing_membership(db):
    assert asyncio.run(MemberNames().resolve(client(), db, 10, 20)) == "Josh"
    assert db.get_member(10, 20) is None


def test_offline_uses_bot_nickname_and_never_emits_numeric_id(db):
    bot = client()
    bot.is_ready = lambda: False
    db.set_user_nickname(20, "Nickname", 20)
    assert asyncio.run(MemberNames().resolve(bot, db, 10, 20)) == "Nickname"
    assert asyncio.run(MemberNames().resolve(bot, db, 10, 21)) == "Member"
    bot.fetch_user.assert_not_awaited()


def test_existing_cache_member_needs_no_network(db):
    guild = SimpleNamespace(get_member=lambda _: member(), fetch_member=AsyncMock())
    assert asyncio.run(MemberNames().resolve(client(guild), db, 10, 20)) == "Josh"
    guild.fetch_member.assert_not_awaited()
