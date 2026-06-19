"""Tests for Strava glue inside app.bot that doesn't need a live Discord/Strava.

Only the pure/DB-touching helpers are exercised (deauthorization handling); the
network + Discord-send paths are covered indirectly via app.strava_client tests.
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app.bot import _strava_handle_deauth, db  # noqa: E402


def _link(user_id: int, athlete_id: int) -> None:
    db.link_strava_account(
        user_id=user_id, athlete_id=athlete_id,
        access_token_enc="a", refresh_token_enc="r", expires_at=1,
        scope=None, athlete_name=None,
    )


def test_deauth_unlinks_matching_athlete():
    _link(user_id=100, athlete_id=4242)
    assert db.get_strava_account(100) is not None
    _strava_handle_deauth(
        {"object_type": "athlete", "object_id": 4242, "updates": {"authorized": "false"}}
    )
    assert db.get_strava_account(100) is None


def test_deauth_ignores_when_still_authorized():
    _link(user_id=101, athlete_id=5252)
    # An athlete update that isn't a deauthorization must not unlink.
    _strava_handle_deauth(
        {"object_type": "athlete", "object_id": 5252, "updates": {"weight": "82"}}
    )
    assert db.get_strava_account(101) is not None


def test_deauth_unknown_athlete_is_noop():
    # No matching link → should not raise.
    _strava_handle_deauth(
        {"object_type": "athlete", "object_id": 999999, "updates": {"authorized": "false"}}
    )
