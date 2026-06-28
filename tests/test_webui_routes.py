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
