"""Route-level tests for the dashboard's invite + role-grant endpoints.

``pytest-aiohttp`` isn't a dependency, so each test drives the aiohttp app
through ``asyncio.run`` with aiohttp's built-in test client. The Discord-side
actions are stubbed with fake injected callables, so nothing touches a real
gateway — we assert routing, auth, payload handling, and the 503 fallback when
an action isn't wired up.
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.webui import build_app


def _run(coro):
    return asyncio.run(coro)


async def _login(client: TestClient) -> None:
    resp = await client.post("/login", data={"password": "secret"})
    assert resp.status == 200  # follows the redirect to "/"


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_invite_requires_auth(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret", invite_user=_fake_invite([]))
        client = await _client(app)
        try:
            r = await client.post("/api/invite", json={"guild": "1", "user_id": "5"})
            assert r.status == 401
        finally:
            await client.close()
            db.close()
    _run(go())


def _fake_invite(sink):
    async def fake(guild_id, user_id, channel_id, actor_name):
        sink.append((guild_id, user_id, channel_id, actor_name))
        return {"ok": True, "link": "https://discord.gg/abc", "dmed": True}
    return fake


def test_invite_happy_path_passes_args_through(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        sink = []
        app = build_app(db=db, password="secret", invite_user=_fake_invite(sink))
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/invite",
                json={"guild": "1", "user_id": "555", "channel_id": "77"},
            )
            assert r.status == 200
            body = await r.json()
            assert body["ok"] and body["link"] == "https://discord.gg/abc"
            assert sink and sink[0][0] == 1 and sink[0][1] == 555
            assert sink[0][2] == 77
            assert sink[0][3].startswith("web:")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_messages_channels_and_log(tmp_path):
    """The Messages tab lists channels with counts and serves a channel's log in
    chat order, carrying each author's mirrored name/avatar."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice", avatar="http://a/alice.png")
        db.message_log_add(1, 100, "first", message_id=1, channel_id=7,
                           channel_name="general", at=base)
        db.message_log_add(1, 200, "second", message_id=2, channel_id=7,
                           channel_name="general", at=base + timedelta(minutes=1))
        db.message_log_add(1, 100, "elsewhere", message_id=3, channel_id=8,
                           channel_name="gym", at=base + timedelta(minutes=2))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            chans = await (await client.get("/api/messages/channels?guild=1")).json()
            by_cid = {c["channel_id"]: c for c in chans["channels"]}
            assert by_cid["7"]["count"] == 2 and by_cid["7"]["channel_name"] == "general"
            assert by_cid["8"]["count"] == 1
            # Most recently active channel (gym) sorts first.
            assert chans["channels"][0]["channel_id"] == "8"

            log = await (await client.get("/api/messages/log?guild=1&channel=7")).json()
            assert [m["content"] for m in log["messages"]] == ["first", "second"]
            assert log["messages"][0]["display_name"] == "Alice"
            assert log["messages"][0]["avatar"] == "http://a/alice.png"
            # Unknown author falls back to the id, no avatar.
            assert log["messages"][1]["display_name"] == "200"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_blacklist_add_purges_logs_announces_and_excludes(tmp_path):
    """Blacklisting via the dashboard purges the user's logs, fires the public
    announce callable, and hides them from the channel feed; removal works too."""
    async def go():
        from datetime import datetime, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.upsert_member(1, 300, "carol", "Carol")
        db.message_log_add(1, 300, "spam", message_id=9, channel_id=7,
                           channel_name="general", at=now)

        sink = []
        async def fake_announce(gid, uid, reason, actor):
            sink.append((gid, uid, reason, actor))
            return {"ok": True, "channel": "general"}

        app = build_app(db=db, password="secret", announce_blacklist=fake_announce)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/blacklist/add",
                                  json={"guild": "1", "user_id": "300", "reason": "bot spam"})
            body = await r.json()
            assert body["ok"] and body["announced"] is True
            # Announce callable got the reason; logs purged; audited.
            assert sink == [(1, 300, "bot spam", sink[0][3])]
            assert db.message_count_since(1, 300) == 0
            assert db.message_is_blacklisted(1, 300) is True

            # Channel 7 had only Carol's now-purged message → no channels left.
            chans = await (await client.get("/api/messages/channels?guild=1")).json()
            assert chans["channels"] == []
            assert chans["blacklist"][0]["user_id"] == "300"
            assert chans["blacklist"][0]["reason"] == "bot spam"

            rm = await client.post("/api/blacklist/remove",
                                  json={"guild": "1", "user_id": "300"})
            assert (await rm.json())["ok"] is True
            assert db.message_is_blacklisted(1, 300) is False
        finally:
            await client.close()
            db.close()
    _run(go())


def test_invite_503_when_not_wired(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")  # no invite_user
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/invite", json={"guild": "1", "user_id": "5"})
            assert r.status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


def test_invite_bad_payload_is_400(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret", invite_user=_fake_invite([]))
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/invite", json={"guild": "1"})  # no user_id
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_role_add_and_remove(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        calls = []

        async def fake_role(guild_id, user_id, role_id, add, actor_name):
            calls.append((guild_id, user_id, role_id, add, actor_name))
            return {"ok": True}

        app = build_app(db=db, password="secret", set_member_role=fake_role)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/role",
                json={"guild": "1", "user": "100", "role_id": "10", "action": "add"},
            )
            assert r.status == 200 and (await r.json())["ok"]
            r = await client.post(
                "/api/member/role",
                json={"guild": "1", "user": "100", "role_id": "10", "action": "remove"},
            )
            assert r.status == 200
            assert [c[3] for c in calls] == [True, False]
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_role_rejects_bad_action(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")

        async def fake_role(*a):
            return {"ok": True}

        app = build_app(db=db, password="secret", set_member_role=fake_role)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/role",
                json={"guild": "1", "user": "100", "role_id": "10", "action": "nuke"},
            )
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_moderation_reports_timeout_state(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")

        async def fake_mod(guild_id, user_id):
            return {"ok": True, "timed_out": True,
                    "timed_out_until": "2030-01-01T00:00:00+00:00",
                    "can_moderate": True}

        app = build_app(db=db, password="secret", member_moderation=fake_mod)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/member/moderation?guild=1&user=100")
            assert r.status == 200
            body = await r.json()
            assert body["timed_out"] is True and body["can_moderate"] is True
        finally:
            await client.close()
            db.close()
    _run(go())


def test_untimeout_passes_through_and_audits(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        calls = []

        async def fake_remove(guild_id, user_id, actor_name):
            calls.append((guild_id, user_id, actor_name))
            return {"ok": True, "changed": True}

        app = build_app(db=db, password="secret", remove_timeout=fake_remove)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/untimeout", json={"guild": "1", "user": "100"},
            )
            assert r.status == 200 and (await r.json())["changed"] is True
            assert calls and calls[0][0] == 1 and calls[0][1] == 100
            assert calls[0][2].startswith("web:")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_untimeout_503_when_not_wired(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")  # no remove_timeout
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/untimeout", json={"guild": "1", "user": "100"},
            )
            assert r.status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


def test_channels_endpoint_returns_injected_list(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")

        async def fake_channels(guild_id):
            return [{"id": "9", "name": "general"}]

        app = build_app(db=db, password="secret", list_channels=fake_channels)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/channels?guild=1")
            assert r.status == 200
            assert (await r.json())["channels"][0]["name"] == "general"
        finally:
            await client.close()
            db.close()
    _run(go())
