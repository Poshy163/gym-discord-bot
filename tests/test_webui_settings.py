"""Route tests for the first-boot claim flow and the Settings tab API.

Follows the existing convention in tests/test_webui_routes.py: no
``pytest-aiohttp``, each test drives the app through ``asyncio.run`` with
aiohttp's built-in test client.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.secretbox import SecretBox
from app.settings_service import DbAuth, SettingsService
from app.webui import build_app

PASSWORD = "correct-horse-battery"


def _run(coro):
    return asyncio.run(coro)


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _stack(tmp_path, env=None):
    path = tmp_path / "w.sqlite3"
    db = Database(path)
    box = SecretBox.open_at(str(path))
    auth = DbAuth(db, env or {})
    settings = SettingsService(db, box, env or {})
    app = build_app(db=db, auth=auth, settings=settings)
    return db, auth, settings, app


# ---------------------------------------------------------------------------
# Claim flow
# ---------------------------------------------------------------------------

def test_unclaimed_instance_refuses_every_api_route(tmp_path):
    """Binding the port without a password must not open a single route.

    Deliberately not relying on "no session can exist yet" -- that would be one
    refactor away from leaking member data.
    """
    async def go():
        db, _auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            for route in ("/api/guilds", "/api/members?guild=1",
                          "/api/settings", "/api/audit?guild=1"):
                r = await client.get(route)
                assert r.status == 401, route
        finally:
            await client.close()
            db.close()
    _run(go())


def test_unclaimed_instance_redirects_to_setup(tmp_path):
    async def go():
        db, _auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            r = await client.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/setup"
            r = await client.get("/login", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/setup"
            r = await client.get("/setup")
            assert r.status == 200
            assert "Set password" in await r.text()
        finally:
            await client.close()
            db.close()
    _run(go())


def test_claiming_sets_the_password_and_signs_you_in(tmp_path):
    async def go():
        db, auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            r = await client.post("/setup", data={
                "password": PASSWORD, "password2": PASSWORD,
            })
            assert r.status == 200  # followed the redirect to "/"
            assert auth.is_claimed()
            # Now authenticated, so the API opens up.
            assert (await client.get("/api/settings")).status == 200
        finally:
            await client.close()
            db.close()
    _run(go())


def test_claim_rejects_mismatched_and_short_passwords(tmp_path):
    async def go():
        db, auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            r = await client.post("/setup", data={
                "password": PASSWORD, "password2": "different",
            })
            assert r.status == 400 and not auth.is_claimed()

            r = await client.post("/setup", data={
                "password": "short", "password2": "short",
            })
            assert r.status == 400 and not auth.is_claimed()
        finally:
            await client.close()
            db.close()
    _run(go())


def test_setup_cannot_be_used_twice(tmp_path):
    """Otherwise it would be an unauthenticated password reset."""
    async def go():
        db, auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            await client.post("/setup", data={
                "password": PASSWORD, "password2": PASSWORD,
            })
            r = await client.post("/setup", data={
                "password": "attacker-password-x", "password2": "attacker-password-x",
            }, allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/login"
            assert auth.verify(PASSWORD)
            assert not auth.verify("attacker-password-x")
        finally:
            await client.close()
            db.close()
    _run(go())


def test_claim_is_recorded_in_the_audit_log(tmp_path):
    """The claim is unauthenticated by design, so attribution is the only
    accountability available."""
    async def go():
        db, _auth, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            await client.post("/setup", data={
                "password": PASSWORD, "password2": PASSWORD,
            })
            actions = [r["action"] for r in db.list_audit(0, limit=10)]
            assert "dashboard_claimed" in actions
        finally:
            await client.close()
            db.close()
    _run(go())


def test_environment_password_skips_setup_entirely(tmp_path):
    """An existing deployment upgrades straight into a working login."""
    async def go():
        db, auth, _s, app = _stack(tmp_path, {"WEBUI_PASSWORD": "legacy-secret"})
        client = await _client(app)
        try:
            assert auth.is_claimed()
            r = await client.get("/setup", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/login"
            r = await client.post("/login", data={"password": "legacy-secret"})
            assert r.status == 200
        finally:
            await client.close()
            db.close()
    _run(go())


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

async def _claimed_client(tmp_path, env=None):
    db, auth, settings, app = _stack(tmp_path, env)
    client = await _client(app)
    await client.post("/setup", data={"password": PASSWORD, "password2": PASSWORD})
    return db, auth, settings, client


def test_settings_round_trip(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post("/api/settings",
                                  json={"key": "REMINDER_HOUR", "value": "9"})
            body = await r.json()
            assert r.status == 200 and body["ok"] and body["effective"]

            payload = await (await client.get("/api/settings")).json()
            field = _find(payload, "REMINDER_HOUR")
            assert field["value"] == "9" and field["source"] == "db"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_invalid_value_is_rejected_with_a_field_message(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post("/api/settings",
                                  json={"key": "REMINDER_HOUR", "value": "99"})
            assert r.status == 400
            body = await r.json()
            assert body["field"] == "REMINDER_HOUR" and body["error"]
            # And nothing was stored, so the next boot is unaffected.
            assert "REMINDER_HOUR" not in db.settings_all()
        finally:
            await client.close()
            db.close()
    _run(go())


def test_saving_a_pinned_setting_reports_it_is_not_in_effect(tmp_path):
    """The confusing case made visible: the write succeeds and is real the
    moment the pin is removed, but it changes nothing right now."""
    async def go():
        db, _a, _s, client = await _claimed_client(
            tmp_path, {"REMINDER_HOUR": "21"},
        )
        try:
            r = await client.post("/api/settings",
                                  json={"key": "REMINDER_HOUR", "value": "9"})
            body = await r.json()
            assert body["ok"] and body["effective"] is False
            assert body["pinned_by"] == "environment"

            field = _find(await (await client.get("/api/settings")).json(),
                          "REMINDER_HOUR")
            assert field["pinned"] and field["value"] == "21"
            assert field["stored"] == "9"
        finally:
            await client.close()
            db.close()
    _run(go())


def test_secrets_are_never_returned_in_plaintext(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            await client.post("/api/settings",
                              json={"key": "DISCORD_TOKEN", "value": "super-secret"})
            raw = await (await client.get("/api/settings")).text()
            assert "super-secret" not in raw

            field = _find(await (await client.get("/api/settings")).json(),
                          "DISCORD_TOKEN")
            assert field["is_set"] and field["value"] == ""
            # A Discord token's prefix identifies the application, so not even
            # a last-four hint is given for this one.
            assert set(field["masked"]) == {"•"}
        finally:
            await client.close()
            db.close()
    _run(go())


def test_env_only_settings_are_refused_and_marked_uneditable(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post("/api/settings",
                                  json={"key": "WEBUI_PORT", "value": "1234"})
            assert r.status == 400
            field = _find(await (await client.get("/api/settings")).json(),
                          "WEBUI_PORT")
            assert field["editable"] is False
        finally:
            await client.close()
            db.close()
    _run(go())


def test_non_hot_change_is_staged_as_pending(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post(
                "/api/settings",
                json={"key": "DAILY_UPDATE_HOUR", "value": "9"},
            )
            assert (await r.json())["needs_restart"] is True
            payload = await (await client.get("/api/settings")).json()
            assert "DAILY_UPDATE_HOUR" in payload["pending"]
        finally:
            await client.close()
            db.close()
    _run(go())


def test_hot_change_is_not_pending(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post("/api/settings",
                                  json={"key": "MAX_WEIGHT_KG", "value": "400"})
            assert (await r.json())["needs_restart"] is False
            payload = await (await client.get("/api/settings")).json()
            assert payload["pending"] == []
        finally:
            await client.close()
            db.close()
    _run(go())


def test_password_change_requires_the_old_one_and_clears_sessions(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            r = await client.post("/api/password",
                                  json={"old": "wrong", "new": "a-new-long-password"})
            assert r.status == 400

            r = await client.post(
                "/api/password",
                json={"old": PASSWORD, "new": "a-new-long-password"},
            )
            assert r.status == 200
            # The old session must stop working -- otherwise rotating the
            # password leaves a 7-day cookie minted under the old one alive.
            assert (await client.get("/api/settings")).status == 401
        finally:
            await client.close()
            db.close()
    _run(go())


def test_export_contains_set_values_and_is_authenticated(tmp_path):
    async def go():
        db, _a, _s, client = await _claimed_client(tmp_path)
        try:
            await client.post("/api/settings",
                              json={"key": "REMINDER_HOUR", "value": "9"})
            body = await (await client.get("/api/settings/export")).text()
            assert "REMINDER_HOUR=9" in body
            # Untouched defaults are not emitted, so the file stays readable.
            assert "REMINDER_MINUTE=" not in body
        finally:
            await client.close()
            db.close()
    _run(go())


def test_settings_absent_returns_503_not_a_crash(tmp_path):
    """build_app without a SettingsService is the legacy shape used by the
    existing route tests; the Settings endpoints must degrade, not explode."""
    async def go():
        db = Database(tmp_path / "legacy.sqlite3")
        app = build_app(db=db, password="secret")
        client = await _client(app)
        try:
            await client.post("/login", data={"password": "secret"})
            assert (await client.get("/api/settings")).status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


def test_healthz_is_unauthenticated_but_strict_mode_reports_the_worker(tmp_path):
    async def go():
        db, _a, _s, app = _stack(tmp_path)
        client = await _client(app)
        try:
            assert (await client.get("/healthz")).status == 200
            # No supervisor injected -> the strict probe must not claim healthy.
            r = await client.get("/healthz?require_worker=1")
            assert r.status == 503
        finally:
            await client.close()
            db.close()
    _run(go())


def _find(payload, key):
    for group in payload["groups"]:
        for item in group["items"]:
            if item["key"] == key:
                return item
    raise AssertionError(f"{key} missing from the settings payload")
