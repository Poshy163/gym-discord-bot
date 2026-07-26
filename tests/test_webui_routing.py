"""Per-tab URLs: /overview, /members, /members/<id>.

Each tab is a real route rather than client-only state, so Back returns to the
previous tab instead of leaving the dashboard, and any view survives a refresh
or being shared. The failure mode these guard against is silent: a tab present
in the nav but missing a route looks fine until someone refreshes on it.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.secretbox import SecretBox
from app.settings_service import DbAuth, SettingsService
from app.webui import DASHBOARD_TABS, _safe_next, build_app

PASSWORD = "correct-horse-battery"


def _run(coro):
    return asyncio.run(coro)


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _stack(tmp_path, env=None):
    path = tmp_path / "r.sqlite3"
    db = Database(path)
    box = SecretBox.open_at(str(path))
    auth = DbAuth(db, env or {})
    settings = SettingsService(db, box, env or {})
    return db, build_app(db=db, auth=auth, settings=settings)


async def _logged_in(tmp_path):
    db, app = _stack(tmp_path)
    client = await _client(app)
    await client.post("/setup", data={"password": PASSWORD, "password2": PASSWORD})
    return db, client


def test_every_nav_tab_has_a_route(tmp_path):
    """A tab in the nav without a route 404s on refresh — and nothing else
    would catch it, because client-side navigation to it works fine."""
    async def go():
        db, client = await _logged_in(tmp_path)
        try:
            for slug, _icon in DASHBOARD_TABS:
                r = await client.get(f"/{slug}")
                assert r.status == 200, f"/{slug} is in the nav but not routed"
                assert "Gym Dashboard" in await r.text()
        finally:
            await client.close()
            db.close()
    _run(go())


def test_member_deep_link_serves_the_dashboard(tmp_path):
    async def go():
        db, client = await _logged_in(tmp_path)
        try:
            r = await client.get("/members/123456789")
            assert r.status == 200
        finally:
            await client.close()
            db.close()
    _run(go())


def test_tab_routes_do_not_shadow_the_api_or_auth(tmp_path):
    """The routes are registered per-slug rather than as a catch-all precisely
    so they cannot swallow these."""
    async def go():
        db, client = await _logged_in(tmp_path)
        try:
            assert (await client.get("/api/guilds")).status == 200
            assert (await client.get("/healthz")).status == 200
            assert (await client.get("/logo.svg")).status == 200
            r = await client.get("/setup", allow_redirects=False)
            assert r.status == 302  # already claimed
        finally:
            await client.close()
            db.close()
    _run(go())


def test_unknown_path_is_not_swallowed(tmp_path):
    async def go():
        db, client = await _logged_in(tmp_path)
        try:
            assert (await client.get("/not-a-tab")).status == 404
        finally:
            await client.close()
            db.close()
    _run(go())


def test_deep_link_survives_the_login_round_trip(tmp_path):
    """Without ?next=, a bookmarked member page would dump you on the overview
    after logging in and make you navigate back to where you asked to go."""
    async def go():
        db, app = _stack(tmp_path)
        client = await _client(app)
        try:
            await client.post("/setup", data={"password": PASSWORD,
                                              "password2": PASSWORD})
            await client.post("/logout")

            r = await client.get("/members/42?guild=7", allow_redirects=False)
            assert r.status == 302
            assert "next=" in r.headers["Location"]

            r = await client.post(
                "/login?next=%2Fmembers%2F42%3Fguild%3D7",
                data={"password": PASSWORD}, allow_redirects=False,
            )
            assert r.status == 302
            assert r.headers["Location"] == "/members/42?guild=7"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_next_cannot_be_used_as_an_open_redirect(tmp_path):
    """?next= is attacker-controllable on a login page, so anything that isn't
    a same-origin absolute path must collapse to "/"."""
    async def go():
        db, app = _stack(tmp_path)
        client = await _client(app)
        try:
            await client.post("/setup", data={"password": PASSWORD,
                                              "password2": PASSWORD})
            await client.post("/logout")
            for hostile in ("https://evil.example.com/",
                            "//evil.example.com/",
                            "http://evil.example.com",
                            # Browsers normalise \ to /, so this is
                            # protocol-relative — the bypass that defeats a
                            # naive "starts with / and not //" check.
                            "/\\evil.example.com",
                            "/\\/evil.example.com",
                            "\\\\evil.example.com",
                            # Would otherwise be smuggled into the Location header.
                            "/overview\r\nX-Injected: 1",
                            "/overview\nSet-Cookie: a=b"):
                r = await client.post(
                    "/login", params={"next": hostile},
                    data={"password": PASSWORD}, allow_redirects=False,
                )
                assert r.status == 302
                assert r.headers["Location"] == "/", hostile
        finally:
            await client.close()
            db.close()
    _run(go())


@pytest.mark.parametrize("hostile", [
    "https://evil.example.com/",
    "http://evil.example.com",
    "//evil.example.com/",
    # Browsers normalise backslashes to forward slashes, so these are
    # protocol-relative. This is the bypass that defeats a naive
    # "startswith('/') and not startswith('//')" check.
    "/\\evil.example.com",
    "/\\/evil.example.com",
    "\\\\evil.example.com",
    # Would be smuggled into the Location response header.
    "/overview\r\nX-Injected: 1",
    "/overview\nSet-Cookie: a=b",
    "/overview\tx",
    "/overview\x00",
    # Not a path at all.
    "javascript:alert(1)",
    "overview",
    "",
    None,
])
def test_safe_next_rejects_everything_that_is_not_a_local_path(hostile):
    assert _safe_next(hostile) == "/"


@pytest.mark.parametrize("ok", [
    "/overview",
    "/members/42",
    "/members/42?guild=7",
    "/settings",
])
def test_safe_next_passes_local_paths_through(ok):
    assert _safe_next(ok) == ok


def test_tab_list_is_injected_into_the_page(tmp_path):
    """The nav is generated from DASHBOARD_TABS, so the placeholder must be
    gone and every slug present — otherwise the SPA boots with no tabs."""
    async def go():
        db, client = await _logged_in(tmp_path)
        try:
            body = await (await client.get("/overview")).text()
            assert "/*TABS*/" not in body
            for slug, _icon in DASHBOARD_TABS:
                assert f'"{slug}"' in body
        finally:
            await client.close()
            db.close()
    _run(go())
