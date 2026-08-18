"""Route-level tests for the dashboard's invite + role-grant endpoints.

``pytest-aiohttp`` isn't a dependency, so each test drives the aiohttp app
through ``asyncio.run`` with aiohttp's built-in test client. The Discord-side
actions are stubbed with fake injected callables, so nothing touches a real
gateway — we assert routing, auth, payload handling, and the 503 fallback when
an action isn't wired up.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.webui import (
    REMEMBER_SESSION_TTL_SECONDS,
    SESSION_COOKIE,
    build_app,
)


def _run(coro):
    return asyncio.run(coro)


async def _login(client: TestClient) -> None:
    resp = await client.post("/login", data={"password": "secret"})
    assert resp.status == 200  # follows the redirect to "/"


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _session_token(response) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["Set-Cookie"])
    return cookies[SESSION_COOKIE].value


def test_login_locks_out_after_repeated_failures(tmp_path):
    """Five wrong passwords from one IP lock it out — even a subsequent correct
    password is refused with 429 until the cooldown."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            for _ in range(5):
                r = await client.post("/login", data={"password": "nope"})
                assert r.status == 401
            # Locked now: the *correct* password is still refused.
            r = await client.post("/login", data={"password": "secret"})
            assert r.status == 429
            assert r.headers.get("Retry-After")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_responses_carry_security_headers(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            r = await client.get("/login")
            assert r.headers.get("X-Frame-Options") == "DENY"
            assert r.headers.get("X-Content-Type-Options") == "nosniff"
            assert r.headers.get("Cache-Control") == "no-store"
            assert "Keep me signed in for 30 days" in await r.text()
        finally:
            await client.close()
            db.close()
    _run(go())


def test_remembered_login_survives_restart_and_logout_revokes_it(tmp_path):
    async def go():
        path = tmp_path / "remember.sqlite3"
        db = Database(path)
        client = await _client(build_app(db=db, password="secret"))
        try:
            r = await client.post(
                "/login",
                data={"password": "secret", "remember": "1"},
                allow_redirects=False,
            )
            assert r.status == 302
            assert f"Max-Age={REMEMBER_SESSION_TTL_SECONDS}" in r.headers["Set-Cookie"]
            assert "HttpOnly" in r.headers["Set-Cookie"]
            assert "SameSite=Lax" in r.headers["Set-Cookie"]
            token = _session_token(r)
        finally:
            await client.close()
            db.close()

        # A database copy cannot be turned directly into a login: it contains
        # only the digest, while the browser owns the random bearer token.
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT token_hash FROM web_sessions",
            ).fetchone()
        assert row == (hashlib.sha256(token.encode()).hexdigest(),)
        assert token not in row

        # Rebuilding both Database and aiohttp app simulates a full
        # supervisor/container restart, not merely a Discord-worker restart.
        db = Database(path)
        client = await _client(build_app(db=db, password="secret"))
        cookie = {"Cookie": f"{SESSION_COOKIE}={token}"}
        try:
            assert (await client.get("/api/guilds", headers=cookie)).status == 200
            r = await client.post(
                "/logout", headers=cookie, allow_redirects=False,
            )
            assert r.status == 302
        finally:
            await client.close()
            db.close()

        db = Database(path)
        client = await _client(build_app(db=db, password="secret"))
        try:
            assert (await client.get("/api/guilds", headers=cookie)).status == 401
        finally:
            await client.close()
            db.close()
    _run(go())


def test_ordinary_login_is_not_persisted_across_restart(tmp_path):
    async def go():
        path = tmp_path / "ordinary.sqlite3"
        db = Database(path)
        client = await _client(build_app(db=db, password="secret"))
        try:
            r = await client.post(
                "/login", data={"password": "secret"}, allow_redirects=False,
            )
            assert r.status == 302
            assert "Max-Age" not in r.headers["Set-Cookie"]
            token = _session_token(r)
        finally:
            await client.close()
            db.close()

        db = Database(path)
        client = await _client(build_app(db=db, password="secret"))
        try:
            r = await client.get(
                "/api/guilds",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            assert r.status == 401
        finally:
            await client.close()
            db.close()
    _run(go())


def test_expired_remembered_login_is_pruned(tmp_path):
    async def go():
        path = tmp_path / "expired.sqlite3"
        token = "expired-browser-token"
        digest = hashlib.sha256(token.encode()).hexdigest()
        db = Database(path)
        db.web_session_add(
            digest, datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        client = await _client(build_app(db=db, password="secret"))
        try:
            r = await client.get(
                "/api/guilds",
                headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            )
            assert r.status == 401
            assert not db.web_session_valid(digest)
        finally:
            await client.close()
            db.close()
    _run(go())


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
        db.message_log_add(1, 100, "look", message_id=4, channel_id=7,
                           channel_name="general",
                           attachments='[{"url": "http://x/a.png", "kind": "image"}]',
                           at=base + timedelta(minutes=3))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            chans = await (await client.get("/api/messages/channels?guild=1")).json()
            by_cid = {c["channel_id"]: c for c in chans["channels"]}
            assert by_cid["7"]["count"] == 3 and by_cid["7"]["channel_name"] == "general"
            assert by_cid["8"]["count"] == 1
            # Channel 7 has the most recent message ("look"), so it sorts first.
            assert chans["channels"][0]["channel_id"] == "7"

            log = await (await client.get("/api/messages/log?guild=1&channel=7")).json()
            assert [m["content"] for m in log["messages"]] == ["first", "second", "look"]
            assert log["messages"][0]["display_name"] == "Alice"
            assert log["messages"][0]["avatar"] == "http://a/alice.png"
            assert log["messages"][0]["media"] == []
            # Unknown author falls back to the id, no avatar.
            assert log["messages"][1]["display_name"] == "200"
            # Media is parsed back into a list of {url, kind}.
            assert log["messages"][2]["media"] == [
                {"url": "http://x/a.png", "kind": "image"}
            ]
        finally:
            await client.close()
            db.close()
    _run(go())


def test_blacklist_add_keeps_messages_and_announces(tmp_path):
    """Blacklisting via the dashboard blocks the user from adding to the bot,
    fires the public announce callable, and keeps their logged messages; removal
    works too."""
    async def go():
        from datetime import datetime, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.upsert_member(1, 300, "carol", "Carol")
        db.message_log_add(1, 300, "hello", message_id=9, channel_id=7,
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
            assert sink == [(1, 300, "bot spam", sink[0][3])]
            # Messages are NOT deleted; the user is just blacklisted.
            assert db.message_count_since(1, 300) == 1
            assert db.message_is_blacklisted(1, 300) is True

            # Their messages still show in the channel feed.
            chans = await (await client.get("/api/messages/channels?guild=1")).json()
            assert chans["channels"][0]["channel_id"] == "7"
            assert chans["channels"][0]["count"] == 1
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


def test_voice_returns_live_occupancy_and_logged_events(tmp_path):
    """The Voice tab serves live occupancy (from the injected snapshot) plus the
    logged join/leave history (newest first, with member info)."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.voice_log_event(1, 100, "join", channel_id=5, channel_name="General",
                           at=base)
        db.voice_log_event(1, 100, "leave", channel_id=5, channel_name="General",
                           at=base + timedelta(minutes=3))

        async def fake_snapshot(gid):
            return [{"channel_id": "5", "channel_name": "General",
                     "members": [{"user_id": "100", "display_name": "Alice"}]}]

        app = build_app(db=db, password="secret", voice_snapshot=fake_snapshot)
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/voice?guild=1")).json()
            assert d["occupancy"][0]["channel_name"] == "General"
            assert d["occupancy"][0]["members"][0]["display_name"] == "Alice"
            # Newest first; member name joined from the mirror.
            assert [e["event"] for e in d["events"]] == ["leave", "join"]
            assert d["events"][0]["display_name"] == "Alice"
            assert d["events"][0]["channel"] == "General"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_voice_without_snapshot_still_serves_events(tmp_path):
    """No injected snapshot → empty occupancy, but the logged history still loads."""
    async def go():
        from datetime import datetime, timezone

        db = Database(tmp_path / "g.sqlite3")
        db.voice_log_event(1, 100, "join", channel_id=5, channel_name="General",
                           at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        app = build_app(db=db, password="secret")  # no voice_snapshot
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/voice?guild=1")).json()
            assert d["occupancy"] == []
            assert [e["event"] for e in d["events"]] == ["join"]
        finally:
            await client.close()
            db.close()
    _run(go())


def test_voice_totals_summarise_sorted_with_active_and_muted_math(tmp_path):
    """The 7-day totals give one summarised row per active member, sorted by
    in-call desc, with active = in-call − muted and the muted %. A member in the
    live snapshot has their still-open interval credited via the live flags; one
    absent from it is summarised from the log alone (its clean leave here)."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.upsert_member(1, 200, "bob", "Bob")
        # Alice: joined ~2h ago, muted ~1h ago, still connected & self-muted now
        # (present in the snapshot) → ~2h in-call, ~1h muted, ~1h active.
        db.voice_log_event(1, 100, "join", channel_id=5, channel_name="General",
                           at=now - timedelta(hours=2))
        db.voice_log_event(1, 100, "mute_on", channel_id=5, channel_name="General",
                           at=now - timedelta(hours=1))
        # Bob: a clean 30-min call that already ended; absent from the snapshot.
        db.voice_log_event(1, 200, "join", channel_id=5, channel_name="General",
                           at=now - timedelta(minutes=90))
        db.voice_log_event(1, 200, "leave", channel_id=5, channel_name="General",
                           at=now - timedelta(minutes=60))

        async def fake_snapshot(gid):
            # Snapshot carries only self mute/deaf (no server mute/deaf).
            return [{"channel_id": "5", "channel_name": "General",
                     "members": [{"user_id": "100", "display_name": "Alice",
                                  "self_mute": True, "self_deaf": False}]}]

        app = build_app(db=db, password="secret", voice_snapshot=fake_snapshot)
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/voice?guild=1")).json()
            totals = d["totals"]
            # Sorted by in-call desc: Alice (~2h) ahead of Bob (~30m).
            assert [t["user_id"] for t in totals] == ["100", "200"]
            a, b = totals
            assert a["display_name"] == "Alice"
            # Open mute interval credited to now via the live self_mute flag.
            assert abs(a["in_call"] - 2 * 3600) < 5
            assert abs(a["muted"] - 1 * 3600) < 5
            assert abs(a["active"] - (a["in_call"] - a["muted"])) < 1e-6
            assert a["deafened"] == 0
            # Bob: closed 30-min call, never muted → all active.
            assert abs(b["in_call"] - 30 * 60) < 5
            assert b["muted"] == 0
            assert abs(b["active"] - b["in_call"]) < 1e-6
        finally:
            await client.close()
            db.close()
    _run(go())


def test_voice_totals_absent_member_gets_closed_tail(tmp_path):
    """A member absent from the live occupancy snapshot is summarised with
    all-False live flags, so an unterminated trailing interval (bot restarted
    mid-call) is dropped rather than accruing phantom time to now — only the
    verified, already-closed portion of their history counts."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 300, "carol", "Carol")
        # A clean 1h call that ended, then an open rejoin with no leave.
        db.voice_log_event(1, 300, "join", channel_id=5, channel_name="General",
                           at=now - timedelta(hours=3))
        db.voice_log_event(1, 300, "leave", channel_id=5, channel_name="General",
                           at=now - timedelta(hours=2))
        db.voice_log_event(1, 300, "join", channel_id=5, channel_name="General",
                           at=now - timedelta(minutes=30))

        async def fake_snapshot(gid):
            return []  # Carol is not connected right now.

        app = build_app(db=db, password="secret", voice_snapshot=fake_snapshot)
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/voice?guild=1")).json()
            totals = d["totals"]
            assert [t["user_id"] for t in totals] == ["300"]
            # Only the verified 1h closed segment survives; the open rejoin's
            # tail would have added ~30m of phantom time if not gated off.
            assert abs(totals[0]["in_call"] - 1 * 3600) < 5
            assert abs(totals[0]["active"] - 1 * 3600) < 5
        finally:
            await client.close()
            db.close()
    _run(go())


def test_voice_totals_includes_live_member_with_no_recent_event(tmp_path):
    """A member who joined >7 days ago and has sat in-call ever since (no new
    join/leave/move/mute event in the window) has no voice row at >= since, so
    voice_user_ids_since alone would drop them. The candidate set unions the
    live occupancy, so their carry-in join still accrues the full 7-day tail and
    they top the table — instead of showing in live occupancy but vanishing from
    the totals directly below it."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 400, "dave", "Dave")
        # Single join 8 days ago: nothing in the 7-day window, yet still connected.
        db.voice_log_event(1, 400, "join", channel_id=5, channel_name="General",
                           at=now - timedelta(days=8))

        async def fake_snapshot(gid):
            return [{"channel_id": "5", "channel_name": "General",
                     "members": [{"user_id": "400", "display_name": "Dave"}]}]

        app = build_app(db=db, password="secret", voice_snapshot=fake_snapshot)
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/voice?guild=1")).json()
            # Present in live occupancy...
            assert d["occupancy"][0]["members"][0]["user_id"] == "400"
            # ...and no longer missing from the totals below it.
            assert [t["user_id"] for t in d["totals"]] == ["400"]
            # Full 7-day tail credited via the carry-in join + live_in_call.
            assert abs(d["totals"][0]["in_call"] - 7 * 86400) < 5
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_overlaps_and_resolves_icons(tmp_path):
    """The Activity tab credits overlapping games to each title, lists every
    currently-running game, honours the ?days window, and falls back to the
    curated icon map when an activity carries no rich-presence image."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        db.presence_log_event(1, 100, "online", at=now - timedelta(hours=3))
        # tModLoader for 2h; Excel overlaps the last hour; both still running.
        db.activity_log_set(1, 100, [("tModLoader", None)],
                            at=now - timedelta(hours=2))
        db.activity_log_set(1, 100, [("tModLoader", None), ("Excel", None)],
                            at=now - timedelta(hours=1))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/activity?guild=1&days=7")).json()
            assert d["window_days"] == 7
            u = d["users"][0]
            games = {g["name"]: g for g in u["top_games"]}
            # Overlap: tModLoader ~2h, Excel ~1h (both credited).
            assert games["tModLoader"]["seconds"] > games["Excel"]["seconds"]
            assert games["Excel"]["seconds"] >= 3000
            # tModLoader resolves a curated icon despite no stored image.
            assert games["tModLoader"]["image"]
            # Both currently-running games are reported.
            assert {g["name"] for g in u["current_games"]} == {"tModLoader", "Excel"}
            # Presence stats say how much of the selected window was actually
            # observed, instead of presenting partial capture as a full 7 days.
            assert u["presence"]["online_seconds"] >= 3 * 3600 - 10
            assert 1 < u["presence"]["coverage_percent"] < 2
            assert u["presence"]["online_percent"] == 100.0
            assert u["status_for_seconds"] >= 3 * 3600 - 10
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_hides_stale_current_game_when_presence_is_offline(tmp_path):
    """An explicit offline status wins over a stale Discord activity payload."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        db.presence_log_event(1, 100, "online", at=now - timedelta(hours=3))
        db.activity_log_set(
            1, 100, [("Rust", None)], at=now - timedelta(hours=2),
        )
        # Simulate an older bot missing the corresponding empty activity event.
        db.presence_log_event(1, 100, "offline", at=now - timedelta(hours=1))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/activity?guild=1")).json()
            u = d["users"][0]
            assert u["status"] == "offline"
            assert u["current_games"] == []

            log = await (await client.get(
                "/api/activity/log?guild=1&user=100"
            )).json()
            assert log["games"][0]["playing_now"] is False
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_resolves_app_icon_for_non_game_app(tmp_path, monkeypatch):
    """An app with no rich-presence image and no entry in the games map still
    gets an icon resolved via its Discord application id (RPC endpoint)."""
    from app import game_icons

    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        # CurseForge: no image, not in the games list, but carries an app id.
        db.activity_log_set(1, 100, [("CurseForge", None, 999001)],
                            at=now - timedelta(hours=1))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/activity?guild=1")).json()
            g = d["users"][0]["current_games"][0]
            assert g["name"] == "CurseForge"
            assert g["image"] == "https://cdn/app/999001.png"
            assert "_app" not in g  # internal field is stripped from the payload
        finally:
            await client.close()
            db.close()

    game_icons._APP_ICONS.clear()
    monkeypatch.setattr(game_icons, "_APP_CACHE_PATH", None)

    async def fake_fetch(app_id, session):
        return f"https://cdn/app/{app_id}.png"

    monkeypatch.setattr(game_icons, "_fetch_app_icon", fake_fetch)
    _run(go())


def test_activity_includes_server_leaderboard(tmp_path):
    """The Activity payload carries a server-wide 'most played' leaderboard
    aggregating every tracked user, with the players on each game."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.upsert_member(1, 200, "bob", "Bob")
        db.presence_track_add(1, 100, started_by=0)
        db.presence_track_add(1, 200, started_by=0)
        # Both play Rust; only Alice plays tModLoader → Rust ranks first.
        db.activity_log_set(1, 100, [("Rust", None)], at=now - timedelta(hours=3))
        db.activity_log_set(1, 100, [("tModLoader", None)], at=now - timedelta(hours=2))
        db.activity_log_set(1, 200, [("Rust", None)], at=now - timedelta(hours=3))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/activity?guild=1&days=7")).json()
            lb = {r["name"]: r for r in d["leaderboard"]}
            assert "Rust" in lb and "tModLoader" in lb
            assert lb["Rust"]["seconds"] >= lb["tModLoader"]["seconds"]
            assert set(lb["Rust"]["players"]) == {"Alice", "Bob"}
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_log_lists_sessions_newest_first_with_a_rollup(tmp_path):
    """The per-member log answers "when did he play what": one entry per stretch
    of a title, newest first, alongside the same window's most-played rollup."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        # Siege 4h→2h ago alongside Steam, Steam alone until 1h ago, then
        # Siege again and still running.
        db.activity_log_set(1, 100, [("Siege", None), ("Steam", None)],
                            at=now - timedelta(hours=4))
        db.activity_log_set(1, 100, [("Steam", None)], at=now - timedelta(hours=2))
        db.activity_log_set(1, 100, [], at=now - timedelta(hours=1))
        db.activity_log_set(1, 100, [("Siege", None)],
                            at=now - timedelta(minutes=30))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get(
                "/api/activity/log?guild=1&user=100&days=7"
            )).json()
            assert d["display_name"] == "Alice"
            assert d["window_days"] == 7 and d["tracked"] is True
            assert d["truncated"] is False and d["session_count"] == 3

            # Newest first: Siege's live stint, then the two that opened
            # together 4h ago (Steam ran on after Siege stopped).
            starts = [s["start"] for s in d["sessions"]]
            assert starts == sorted(starts, reverse=True)
            assert [s["name"] for s in d["sessions"]] == [
                "Siege", "Steam", "Siege",
            ]
            live = d["sessions"][0]
            assert live["open"] is True                    # still playing
            assert 1700 <= live["seconds"] <= 1900         # ~30 min
            assert live["start_local"] < live["end_local"]

            games = {g["name"]: g for g in d["games"]}
            # Steam (3h) outranks Siege (2h + 30m) and only Siege is live.
            assert list(games) == ["Steam", "Siege"]
            assert games["Siege"]["sessions"] == 2
            assert games["Siege"]["playing_now"] is True
            assert games["Steam"]["playing_now"] is False
            # The rollup is the sum of the log — the two are shown side by side.
            assert games["Siege"]["seconds"] == round(
                sum(s["seconds"] for s in d["sessions"] if s["name"] == "Siege")
            )
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_log_rejects_a_missing_user(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/activity/log?guild=1")
            assert r.status == 400
            assert (await r.json())["ok"] is False
            # A non-numeric id is the same mistake, not a 500.
            assert (await client.get(
                "/api/activity/log?guild=1&user=alice"
            )).status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_log_requires_auth(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            r = await client.get("/api/activity/log?guild=1&user=100")
            assert r.status == 401
        finally:
            await client.close()
            db.close()
    _run(go())


def test_activity_log_reports_history_for_an_untracked_member(tmp_path):
    """Stopping tracking doesn't erase what was recorded — the log still shows
    it, flagged so the panel can say the history stops there."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.activity_log_set(1, 100, [("Rust", None)], at=now - timedelta(hours=3))
        db.activity_log_set(1, 100, [], at=now - timedelta(hours=2))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get(
                "/api/activity/log?guild=1&user=100"
            )).json()
            assert d["tracked"] is False
            assert [s["name"] for s in d["sessions"]] == ["Rust"]
            assert d["sessions"][0]["open"] is False
        finally:
            await client.close()
            db.close()
    _run(go())


def test_overview_live_block(tmp_path):
    """The Overview payload includes a 'live' block: who's online/playing now,
    the day's top games, average sleep, and a 7-day message series."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        db.presence_log_event(1, 100, "online", at=now - timedelta(hours=2))
        db.activity_log_set(1, 100, [("Rust", None)], at=now - timedelta(hours=1))
        db.message_log_add(1, 100, "hi", message_id=1, at=now - timedelta(hours=1))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/overview?guild=1")).json()
            live = d["live"]
            assert live["tracked"] == 1 and live["online"] == 1
            assert live["playing"] == 1
            assert live["top_today"][0]["name"] == "Rust"
            assert len(live["messages_7d"]) == 8  # today + 7 days back
            assert sum(p["count"] for p in live["messages_7d"]) >= 1
        finally:
            await client.close()
            db.close()
    _run(go())


def test_sleep_tab_reports_nightly_sessions(tmp_path):
    """The Sleep tab derives nightly sessions from presence and summarises them
    (count + average) per tracked user."""
    async def go():
        from datetime import datetime, timedelta, timezone

        db = Database(tmp_path / "g.sqlite3")
        now = datetime.now(timezone.utc)
        db.upsert_member(1, 100, "alice", "Alice")
        db.presence_track_add(1, 100, started_by=0)
        # Anchor to "now" so the night always lands inside the 7-day window:
        # awake 2 days ago, ~8h offline, then awake again.
        db.presence_log_event(1, 100, "online", at=now - timedelta(days=2))
        db.presence_log_event(1, 100, "offline",
                              at=now - timedelta(days=2) + timedelta(hours=2))
        db.presence_log_event(1, 100, "online",
                              at=now - timedelta(days=2) + timedelta(hours=10))

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/sleep?guild=1")).json()
            assert d["window_days"] == 7  # 7-day default
            u = d["users"][0]
            assert u["display_name"] == "Alice"
            assert u["nights"] == 1
            assert u["avg_hours"] == 8.0
            assert u["sessions"][0]["duration_hours"] == 8.0
            # Insights block is present and summarises the single night.
            assert u["stats"]["nights"] == 1
            assert u["stats"]["avg_hours"] == 8.0
            assert len(u["stats"]["series"]) == 1
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


def test_member_track_start_and_stop(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        calls = []

        async def fake_track(guild_id, user_id, start, actor_name):
            calls.append((guild_id, user_id, start, actor_name))
            return {"ok": True, "tracked": start, "changed": True}

        app = build_app(db=db, password="secret", presence_track=fake_track)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/track",
                json={"guild": "1", "user": "100", "action": "start"},
            )
            assert r.status == 200 and (await r.json())["tracked"] is True
            r = await client.post(
                "/api/member/track",
                json={"guild": "1", "user": "100", "action": "stop"},
            )
            assert r.status == 200 and (await r.json())["tracked"] is False
            assert [c[2] for c in calls] == [True, False]
            assert calls[0][0] == 1 and calls[0][1] == 100
            assert calls[0][3].startswith("web:")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_track_rejects_bad_action(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")

        async def fake_track(*a):
            return {"ok": True}

        app = build_app(db=db, password="secret", presence_track=fake_track)
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/track",
                json={"guild": "1", "user": "100", "action": "pause"},
            )
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_track_503_when_not_wired(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")  # no presence_track
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post(
                "/api/member/track", json={"guild": "1", "user": "100"},
            )
            assert r.status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_exposes_presence_tracking_state(tmp_path):
    """api_member reports whether the member is tracked and whether the control
    should appear (handler wired + feature enabled)."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        db.upsert_member(1, 100, "alice", "Alice")

        async def fake_track(*a):
            return {"ok": True}

        app = build_app(
            db=db, password="secret",
            presence_track=fake_track, presence_enabled=True,
        )
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/member?guild=1&user=100")).json()
            assert d["presence_tracking_available"] is True
            assert d["presence_tracked"] is False
            db.presence_track_add(1, 100, started_by=0)
            d = await (await client.get("/api/member?guild=1&user=100")).json()
            assert d["presence_tracked"] is True
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_exposes_nutrition_streaks(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        db.upsert_member(1, 100, "alice", "Alice")
        app = build_app(
            db=db, password="secret",
            calorie_streak=lambda uid: 5, protein_streak=lambda uid: 3,
        )
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/member?guild=1&user=100")).json()
            assert d["calorie_streak"] == 5
            assert d["protein_streak"] == 3
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_streaks_none_when_not_wired(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        db.upsert_member(1, 100, "alice", "Alice")
        app = build_app(db=db, password="secret")  # no streak handlers
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/member?guild=1&user=100")).json()
            assert d["calorie_streak"] is None
            assert d["protein_streak"] is None
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_track_control_hidden_when_feature_disabled(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        db.upsert_member(1, 100, "alice", "Alice")

        async def fake_track(*a):
            return {"ok": True}

        # Handler wired but presence_enabled defaults False → control hidden.
        app = build_app(db=db, password="secret", presence_track=fake_track)
        client = await _client(app)
        try:
            await _login(client)
            d = await (await client.get("/api/member?guild=1&user=100")).json()
            assert d["presence_tracking_available"] is False
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


def test_media_route_serves_stored_files_with_auth(tmp_path):
    """Downloaded attachments are served from the media dir to logged-in
    operators only, confined to the media root (no path traversal)."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        media = tmp_path / "media"
        (media / "1").mkdir(parents=True)
        (media / "1" / "42.png").write_bytes(b"\x89PNG\r\n\x1a\nDATA")
        # A secret well outside the media root we must never serve.
        (tmp_path / "secret.txt").write_text("top secret")

        app = build_app(db=db, password="secret", media_dir=str(media))
        client = await _client(app)
        try:
            # Unauthenticated requests are rejected.
            assert (await client.get("/media/1/42.png")).status == 401
            await _login(client)
            ok = await client.get("/media/1/42.png")
            assert ok.status == 200
            assert (await ok.read()) == b"\x89PNG\r\n\x1a\nDATA"
            # Missing file -> 404.
            assert (await client.get("/media/1/nope.png")).status == 404
            # Path traversal is blocked (403 or 404, never the secret bytes).
            trav = await client.get("/media/../secret.txt")
            assert trav.status in (403, 404)
            assert "top secret" not in (await trav.text())
        finally:
            await client.close()
            db.close()
    _run(go())


def test_media_route_404_when_storage_disabled(tmp_path):
    """With no media_dir injected (download disabled), the route 404s rather
    than erroring."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")  # media_dir=None
        client = await _client(app)
        try:
            await _login(client)
            assert (await client.get("/media/1/42.png")).status == 404
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_autountimeout_toggle(tmp_path):
    """The per-member protection toggle routes through the injected handler and
    503s when it isn't wired up."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        sink = []

        async def fake_set(gid, uid, enable, actor):
            sink.append((gid, uid, enable, actor))
            return {"ok": True, "protected": enable, "changed": True}

        app = build_app(db=db, password="secret", set_auto_untimeout=fake_set)
        client = await _client(app)
        try:
            # Auth required.
            r = await client.post("/api/member/autountimeout",
                                  json={"guild": "1", "user": "5", "enable": True})
            assert r.status == 401
            await _login(client)
            r = await client.post("/api/member/autountimeout",
                                  json={"guild": "1", "user": "5", "enable": True})
            assert r.status == 200
            assert (await r.json())["protected"] is True
            assert sink[0][0] == 1 and sink[0][1] == 5 and sink[0][2] is True
            assert sink[0][3].startswith("web:")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_autountimeout_503_when_unwired(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")  # no handler injected
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/member/autountimeout",
                                  json={"guild": "1", "user": "5", "enable": True})
            assert r.status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


# ---- nutrition targets -----------------------------------------------------

def _weekday_and_weekend():
    """A weekday and a weekend day on or after today, since rules written now
    take effect today."""
    from datetime import timedelta

    from app import targets

    day = targets.local_today()
    days = [day + timedelta(days=n) for n in range(7)]
    return (
        next(d for d in days if not targets.is_weekend(d)),
        next(d for d in days if targets.is_weekend(d)),
    )


def test_nutrition_targets_saves_weekday_and_weekend(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5",
                "calorie_weekday": 1500, "calorie_weekend": 2200,
                "protein_weekday": 180, "protein_weekend": "",
            })
            assert r.status == 200
            weekday, weekend = _weekday_and_weekend()
            assert db.calorie_goal_get(1, 5, weekday)["daily_target_kcal"] == 1500
            assert db.calorie_goal_get(1, 5, weekend)["daily_target_kcal"] == 2200
            # A blank weekend protein box means "same all week", not "off".
            assert db.protein_goal_get(1, 5, weekend)["daily_target_g"] == 180
            # The change is auditable.
            actions = [a["action"] for a in db.list_audit(1, subject_id=5)]
            assert "nutrition_targets_set" in actions
        finally:
            await client.close()
            db.close()
    _run(go())


def test_nutrition_targets_blank_weekday_turns_a_tracker_off(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            db.calorie_goal_set(1, 5, "u5", 1500, 2200)
            db.protein_goal_set(1, 5, "u5", 180)
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5",
                "calorie_weekday": "", "calorie_weekend": "",
                "protein_weekday": 190, "protein_weekend": "",
            })
            assert r.status == 200
            weekday, weekend = _weekday_and_weekend()
            # Calories off on BOTH bands — the weekend override must not survive.
            assert db.calorie_goal_get(1, 5, weekday) is None
            assert db.calorie_goal_get(1, 5, weekend) is None
            assert db.protein_goal_get(1, 5)["daily_target_g"] == 190
        finally:
            await client.close()
            db.close()
    _run(go())


def test_nutrition_targets_rejects_weekend_without_weekday(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5",
                "calorie_weekday": "", "calorie_weekend": 2200,
            })
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_nutrition_targets_rejects_out_of_range(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5", "calorie_weekday": 999_999,
            })
            assert r.status == 400
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5", "calorie_weekday": -5,
            })
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_nutrition_targets_requires_auth(tmp_path):
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            r = await client.post("/api/nutrition/targets", json={
                "guild": "1", "user": "5", "calorie_weekday": 1500,
            })
            assert r.status in (401, 403)
        finally:
            await client.close()
            db.close()
    _run(go())


# ---------------------------------------------------------------------------
# Deleting a bogus weigh-in from the dashboard
# ---------------------------------------------------------------------------
# A smart scale can log a reading nobody wants kept -- a half-finished
# measurement, or one it assigned to the wrong profile. A stray weight is not
# cosmetic: it moves TDEE, the bodyweight-linked protein target and every
# true-load line on the leaderboard.

def test_bodyweight_delete_removes_the_row_and_its_metrics(tmp_path):
    async def go():
        db = Database(tmp_path / "bw.sqlite3")
        when = datetime(2026, 7, 30, 4, 45, 19, tzinfo=timezone.utc)
        db.set_bodyweight(7, 99, 107.3, recorded_at=when)
        db.add_body_metrics(7, 99, {"body_fat_pct": (22.2, "%")},
                            recorded_at=when)
        row_id = db.bodyweight_history(7, 99)[0]["id"]
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/bodyweight/delete",
                                  json={"guild": 7, "id": row_id})
            assert r.status == 200
            assert (await r.json())["ok"] is True
            assert db.bodyweight_history(7, 99) == []
            # The body composition measured with it goes too.
            assert db.latest_body_metrics(99) == {}
        finally:
            await client.close()
            db.close()
    _run(go())


def test_bodyweight_delete_requires_auth(tmp_path):
    async def go():
        db = Database(tmp_path / "bw2.sqlite3")
        db.set_bodyweight(7, 99, 107.3)
        row_id = db.bodyweight_history(7, 99)[0]["id"]
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            r = await client.post("/api/bodyweight/delete",
                                  json={"guild": 7, "id": row_id})
            assert r.status in (401, 403)
            # And nothing was removed.
            assert len(db.bodyweight_history(7, 99)) == 1
        finally:
            await client.close()
            db.close()
    _run(go())


def test_bodyweight_delete_reports_a_missing_row(tmp_path):
    async def go():
        db = Database(tmp_path / "bw3.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/bodyweight/delete",
                                  json={"guild": 7, "id": 999999})
            assert r.status == 200
            assert (await r.json())["ok"] is False
        finally:
            await client.close()
            db.close()
    _run(go())


def test_bodyweight_delete_rejects_a_payload_with_no_id(tmp_path):
    async def go():
        db = Database(tmp_path / "bw4.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.post("/api/bodyweight/delete", json={"guild": 7})
            assert r.status == 400
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_payload_carries_bodyweight_ids(tmp_path):
    """The dashboard cannot offer a delete without the row id."""
    async def go():
        db = Database(tmp_path / "bw5.sqlite3")
        db.set_bodyweight(7, 99, 106.3)
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/member?guild=7&user=99")
            assert r.status == 200
            body = await r.json()
            assert body["bodyweights"]
            assert body["bodyweights"][0]["id"]
            assert body["bodyweights"][0]["weight_kg"] == 106.3
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_payload_reports_a_home_assistant_link_without_the_token(tmp_path):
    async def go():
        db = Database(tmp_path / "bw6.sqlite3")
        db.ha_server_set(99, "https://home.example.com", "super-secret-cipher")
        db.ha_link(99, 7, "joshua_s")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/member?guild=7&user=99")
            body = await r.json()
            assert body["ha_server"] == "https://home.example.com"
            assert body["ha_prefix"] == "joshua_s"
            # The stored credential must never reach the dashboard, encrypted or
            # otherwise.
            assert "super-secret-cipher" not in str(body)
            assert "token" not in " ".join(body)
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_payload_carries_bounded_registry_body_metric_histories(tmp_path):
    async def go():
        db = Database(tmp_path / "body-metrics.sqlite3")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(95):
            when = start + timedelta(days=index)
            # Composition rows belong to the weigh-in at the same timestamp.
            db.set_bodyweight(7, 99, 80 + index / 100, recorded_at=when)
            db.add_body_metrics(
                7, 99, {"body_fat_pct": (20 + index / 10, "%")},
                recorded_at=when,
            )
        db.add_body_metrics(
            7, 99,
            {"bmi": (25.4, ""), "future_scale_metric": (123.0, "widgets")},
            recorded_at=start + timedelta(days=94),
        )
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/api/member?guild=7&user=99")
            assert r.status == 200
            body = await r.json()

            metrics = {m["key"]: m for m in body["body_metrics"]}
            # The public payload is driven by ha_client.METRICS, not arbitrary
            # strings that happen to exist in the database.
            assert set(metrics) == {"body_fat_pct", "bmi"}
            fat = metrics["body_fat_pct"]
            assert fat["label"] == "Body fat"
            assert fat["unit"] == "%"
            assert fat["precision"] == 1
            assert len(fat["points"]) == 90
            # The cap keeps the newest window, while preserving plot order.
            assert fat["points"][0]["value"] == 20.5
            assert fat["points"][-1]["value"] == 29.4
            assert fat["points"][-1]["unit"] == "%"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_page_renders_neutral_body_metric_trends_and_refreshes_delete(tmp_path):
    async def go():
        db = Database(tmp_path / "body-metric-view.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/")
            body = await r.text()
            assert "bodyMetricCards(bodyMetrics)" in body
            assert "vs previous" in body
            assert "Date.parse(p.at)" in body
            assert "newest.unit||m.unit" in body
            assert "can shift with hydration, meals, exercise, and time of day" in body
            delete_fn = body[
                body.index("async function delBodyweight"):
                body.index("function toast", body.index("async function delBodyweight"))
            ]
            assert "memberView(uid)" in delete_fn
            assert "member(uid)" not in delete_fn
        finally:
            await client.close()
            db.close()
    _run(go())


def test_recent_weigh_ins_render_in_the_viewers_timezone(tmp_path):
    """The list showed the stored UTC string, so a weigh-in the scale recorded at
    2:17 PM in Adelaide read as 04:47. fmtTs is the dashboard's existing helper
    and localises via the browser."""
    async def go():
        db = Database(tmp_path / "tz.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get("/")
            body = await r.text()
            assert "fmtTs(p.at)" in body
            # The raw-string slicing this replaced must not come back.
            assert 'String(p.at||"").replace' not in body
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_payload_carries_the_food_tally(tmp_path):
    """The dashboard panel and `/calories food_tally` are one rollup, so the
    member page must fold servings the same way — and must report the entries
    it cannot name rather than presenting a partial diary as the whole one."""
    async def go():
        db = Database(tmp_path / "g.sqlite3")
        db.calorie_goal_set(1, 100, "Josh", 2500)
        db.calorie_food_set(1, 100, "flat white", "Flat White", 80)
        for note, kcal in (
            ("Flat White", 80),
            ("Flat White ×2", 160),
            ("Breakfast (meal)", 520),
            ("label (30 g, label)", 200),   # unreadable panel: names no food
            (None, 650),                    # a bare `650kcal` post
        ):
            db.calorie_add(1, 100, "Josh", kcal, note=note)

        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await _login(client)
            r = await client.get(
                "/api/member", params={"guild": "1", "user": "100"},
            )
            assert r.status == 200
            tally = (await r.json())["food_tally"]
            rows = {row["display"]: row for row in tally["rows"]}

            # 1 + 2 servings across 2 posts, not "2 coffees".
            assert rows["Flat White"]["servings"] == 3
            assert rows["Flat White"]["logs"] == 2
            assert rows["Breakfast"]["kind"] == "meal"
            # The placeholder is not a food; it joins the uncounted pair.
            assert "label" not in rows
            assert tally["unnamed_logs"] == 2
            assert tally["unnamed_kcal"] == 850
        finally:
            await client.close()
            db.close()
    _run(go())
