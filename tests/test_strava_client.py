"""Tests for the Strava integration (pure helpers + DB linkage).

The live HTTP/OAuth paths aren't exercised against Strava — we cover the
pure activity parser, the formatting helpers, token-expiry logic, the
authorize-URL builder, the Fernet token helpers, and the DB methods backing
``/strava_link`` / ``/strava_unlink`` / the webhook handler.
"""
from __future__ import annotations

import time

import pytest

from app import strava_client
from app.db import Database


# ---------------------------------------------------------------------------
# Activity parsing
# ---------------------------------------------------------------------------

def _run_payload(**overrides):
    base = {
        "id": 123456789,
        "athlete": {"id": 42},
        "name": "Morning Run",
        "sport_type": "Run",
        "type": "Run",
        "distance": 5012.3,
        "moving_time": 1500,        # 25:00
        "elapsed_time": 1600,
        "total_elevation_gain": 38.0,
        "average_speed": 3.34,      # m/s
        "average_heartrate": 152.4,
        "max_heartrate": 171.0,
        "calories": 410.0,
        "private": False,
    }
    base.update(overrides)
    return base


def test_parse_activity_extracts_fields():
    act = strava_client.parse_activity(_run_payload())
    assert act.id == 123456789
    assert act.athlete_id == 42
    assert act.name == "Morning Run"
    assert act.sport_type == "Run"
    assert act.distance_m == pytest.approx(5012.3)
    assert act.moving_time_s == 1500
    assert act.average_heartrate == pytest.approx(152.4)
    assert act.private is False
    assert act.url == "https://www.strava.com/activities/123456789"


def test_parse_activity_tolerates_missing_optionals():
    act = strava_client.parse_activity({"id": 1, "name": "x"})
    assert act.sport_type == "Workout"  # falls back when type/sport_type absent
    assert act.distance_m == 0.0
    assert act.average_heartrate is None
    assert act.calories is None
    assert act.athlete_id is None


def test_parse_activity_prefers_sport_type_over_type():
    act = strava_client.parse_activity(
        {"id": 2, "type": "Workout", "sport_type": "WeightTraining"}
    )
    assert act.sport_type == "WeightTraining"


def test_parse_activity_captures_map_and_photo():
    act = strava_client.parse_activity(
        {
            "id": 3,
            "map": {"summary_polyline": "abc", "polyline": "fulldetail"},
            "photos": {"primary": {"urls": {"100": "small.jpg", "600": "big.jpg"}}},
        }
    )
    # Full polyline preferred over the summary one.
    assert act.map_polyline == "fulldetail"
    # Largest photo size chosen.
    assert act.photo_url == "big.jpg"


def test_parse_activity_no_map_or_photo():
    act = strava_client.parse_activity({"id": 4, "name": "Gym"})
    assert act.map_polyline == ""
    assert act.photo_url is None


def test_parse_activity_rich_fields():
    act = strava_client.parse_activity(
        {
            "id": 5,
            "type": "Ride",
            "sport_type": "Ride",
            "start_date": "2026-06-19T03:11:00Z",
            "description": "windy out there",
            "gear": {"name": "Canyon Endurace"},
            "max_speed": 12.5,
            "average_watts": 180.4,
            "kilojoules": 642.0,
            "average_cadence": 84.0,
            "average_temp": 19.0,
            "pr_count": 2,
            "achievement_count": 5,
            "kudos_count": 7,
        }
    )
    assert act.gear_name == "Canyon Endurace"
    assert act.average_watts == pytest.approx(180.4)
    assert act.kilojoules == pytest.approx(642.0)
    assert act.average_cadence == pytest.approx(84.0)
    assert act.average_temp == pytest.approx(19.0)
    assert act.max_speed_ms == pytest.approx(12.5)
    assert act.pr_count == 2
    assert act.achievement_count == 5
    assert act.description == "windy out there"
    # start_unix → epoch for 2026-06-19T03:11:00Z.
    assert strava_client.start_unix(act) == 1781838660


def test_parse_activity_rich_fields_absent_default_safely():
    act = strava_client.parse_activity({"id": 6, "name": "Gym"})
    assert act.average_watts is None
    assert act.average_cadence is None
    assert act.average_temp is None
    assert act.gear_name is None
    assert act.pr_count == 0
    assert act.max_speed_ms == 0.0
    assert strava_client.start_unix(act) is None


def test_decode_polyline_known_vector():
    # Canonical Google example → three coordinates.
    pts = strava_client.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert len(pts) == 3
    assert pts[0][0] == pytest.approx(38.5, abs=1e-4)
    assert pts[0][1] == pytest.approx(-120.2, abs=1e-4)
    assert pts[1][0] == pytest.approx(40.7, abs=1e-4)
    assert pts[2][1] == pytest.approx(-126.453, abs=1e-4)


def test_decode_polyline_empty_and_garbled():
    assert strava_client.decode_polyline("") == []
    assert strava_client.decode_polyline(None) == []
    # Truncated input returns whatever decoded cleanly rather than raising.
    assert isinstance(strava_client.decode_polyline("_p~iF~ps|U_"), list)


def test_mapbox_route_url():
    url = strava_client.mapbox_route_url("abc`@def", "pk.test")
    assert url is not None
    assert url.startswith("https://api.mapbox.com/styles/v1/mapbox/outdoors-v12/static/")
    assert "access_token=pk.test" in url
    # Polyline is URL-encoded inside the path overlay (no raw backtick/@).
    assert "abc%60%40def" in url
    assert "path-5+fc4c02" in url


def test_mapbox_route_url_adds_start_finish_pins():
    # A real (decodable) polyline → green start + red finish pins appended.
    url = strava_client.mapbox_route_url("_p~iF~ps|U_ulLnnqC_mqNvxq`@", "pk.test")
    assert url is not None
    assert "pin-s+19d36b(" in url   # start (green)
    assert "pin-s+e02020(" in url   # finish (red)


def test_mapbox_route_url_custom_style():
    url = strava_client.mapbox_route_url(
        "abc", "pk.test", style="satellite-streets-v12",
    )
    assert "/mapbox/satellite-streets-v12/static/" in url


def test_mapbox_route_url_none_cases():
    assert strava_client.mapbox_route_url("", "pk.test") is None
    assert strava_client.mapbox_route_url("abc", "") is None
    # An absurdly long polyline overflows the URL limit → fall back signal.
    assert strava_client.mapbox_route_url("x" * 9000, "pk.test") is None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def test_format_distance():
    assert strava_client.format_distance(0) == "—"
    assert strava_client.format_distance(850) == "850 m"
    assert strava_client.format_distance(5012.3) == "5.01 km"


def test_format_duration_drops_zero_hours():
    assert strava_client.format_duration(0) == "0:00"
    assert strava_client.format_duration(1500) == "25:00"
    assert strava_client.format_duration(3661) == "1:01:01"


def test_format_pace_and_speed():
    # 5 km in 25:00 → 5:00 /km, and 3.34 m/s ≈ 12.0 km/h.
    assert strava_client.format_pace(5000, 1500) == "5:00 /km"
    assert strava_client.format_pace(0, 1500) is None
    assert strava_client.format_pace(5000, 0) is None
    assert strava_client.format_speed(3.34) == "12.0 km/h"
    assert strava_client.format_speed(0) is None


def test_sport_emoji_and_distance_classification():
    assert strava_client.sport_emoji("Run") == "🏃"
    assert strava_client.sport_emoji("WeightTraining") == "🏋️"
    assert strava_client.sport_emoji("Unknownsport") == "💪"  # fallback
    assert strava_client.is_distance_sport("Ride") is True
    assert strava_client.is_distance_sport("WeightTraining") is False


def test_athlete_display_name():
    assert strava_client.athlete_display_name(
        {"firstname": "Jo", "lastname": "Lee"}
    ) == "Jo Lee"
    assert strava_client.athlete_display_name({"username": "joey"}) == "joey"
    assert strava_client.athlete_display_name({}) == "Strava athlete"


# ---------------------------------------------------------------------------
# Token / OAuth helpers
# ---------------------------------------------------------------------------

def test_tokenset_expiry():
    fresh = strava_client.TokenSet("a", "r", int(time.time()) + 3600)
    stale = strava_client.TokenSet("a", "r", int(time.time()) - 10)
    assert fresh.is_expired() is False
    assert stale.is_expired() is True
    # Skew makes a token "expired" shortly before the real boundary.
    near = strava_client.TokenSet("a", "r", int(time.time()) + 30)
    assert near.is_expired(skew=120) is True


def test_build_authorize_url_round_trips_params():
    cfg = strava_client.StravaConfig(
        client_id="123",
        client_secret="secret",
        redirect_uri="https://bot.example.com/strava/callback",
        webhook_callback_url="https://bot.example.com/strava/webhook",
        verify_token="tok",
    )
    url = strava_client.build_authorize_url(cfg, state="abc123")
    assert url.startswith(strava_client.AUTHORIZE_URL + "?")
    assert "client_id=123" in url
    assert "state=abc123" in url
    assert "response_type=code" in url
    # redirect_uri is url-encoded.
    assert "redirect_uri=https%3A%2F%2Fbot.example.com%2Fstrava%2Fcallback" in url


def test_config_from_env_derives_urls(monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "999")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shh")
    monkeypatch.setenv("STRAVA_PUBLIC_URL", "https://bot.example.com/")
    monkeypatch.delenv("STRAVA_REDIRECT_URI", raising=False)
    monkeypatch.delenv("STRAVA_WEBHOOK_CALLBACK_URL", raising=False)
    cfg = strava_client.config_from_env()
    assert cfg.configured is True
    assert cfg.redirect_uri == "https://bot.example.com/strava/callback"
    assert cfg.webhook_callback_url == "https://bot.example.com/strava/webhook"


def test_config_unconfigured_without_credentials(monkeypatch):
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    assert strava_client.config_from_env().configured is False


# ---------------------------------------------------------------------------
# Fernet token helpers (skipped if cryptography isn't installed)
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("STRAVA_FERNET_KEY", Fernet.generate_key().decode())
    token = strava_client.encrypt_token("refresh-xyz")
    assert token != "refresh-xyz"
    assert strava_client.decrypt_token(token) == "refresh-xyz"


def test_fernet_falls_back_to_revo_key(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    monkeypatch.delenv("STRAVA_FERNET_KEY", raising=False)
    monkeypatch.setenv("REVO_FERNET_KEY", Fernet.generate_key().decode())
    token = strava_client.encrypt_token("abc")
    assert strava_client.decrypt_token(token) == "abc"


def test_encrypt_requires_key(monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("STRAVA_FERNET_KEY", raising=False)
    monkeypatch.delenv("REVO_FERNET_KEY", raising=False)
    with pytest.raises(strava_client.StravaUnavailable):
        strava_client.encrypt_token("abc")


# ---------------------------------------------------------------------------
# Database linkage
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_link_get_unlink_roundtrip(db):
    db.link_strava_account(
        user_id=7, athlete_id=42, access_token_enc="aenc",
        refresh_token_enc="renc", expires_at=1700000000,
        scope="read,activity:read", athlete_name="Jo Lee",
    )
    row = db.get_strava_account(7)
    assert row is not None
    assert row["athlete_id"] == 42
    assert row["access_token_enc"] == "aenc"
    assert row["athlete_name"] == "Jo Lee"
    assert row["last_activity_id"] is None
    # Look up by athlete id (the webhook path).
    assert db.get_strava_account_by_athlete(42)["user_id"] == 7
    assert db.unlink_strava_account(7) is True
    assert db.get_strava_account(7) is None
    assert db.unlink_strava_account(7) is False


def test_update_tokens_and_last_activity(db):
    db.link_strava_account(
        user_id=7, athlete_id=42, access_token_enc="a", refresh_token_enc="r",
        expires_at=1, scope=None, athlete_name=None,
    )
    db.update_strava_tokens(7, "a2", "r2", 1800000000)
    db.update_strava_last_activity(7, 555)
    row = db.get_strava_account(7)
    assert row["access_token_enc"] == "a2"
    assert row["refresh_token_enc"] == "r2"
    assert row["expires_at"] == 1800000000
    assert row["last_activity_id"] == 555


def test_relink_preserves_last_activity(db):
    db.link_strava_account(
        user_id=7, athlete_id=42, access_token_enc="a", refresh_token_enc="r",
        expires_at=1, scope=None, athlete_name=None,
    )
    db.update_strava_last_activity(7, 999)
    # Re-linking (e.g. re-auth) should keep the de-dupe cursor so old
    # activities don't get re-announced.
    db.link_strava_account(
        user_id=7, athlete_id=42, access_token_enc="b", refresh_token_enc="s",
        expires_at=2, scope=None, athlete_name="Jo",
    )
    assert db.get_strava_account(7)["last_activity_id"] == 999


def test_pending_auth_pop_is_single_use(db):
    db.create_strava_pending("state-xyz", user_id=7)
    assert db.pop_strava_pending("state-xyz") == 7
    # Second pop returns None — the handshake is consumed.
    assert db.pop_strava_pending("state-xyz") is None
    assert db.pop_strava_pending("never-existed") is None


def test_list_strava_accounts(db):
    db.link_strava_account(
        user_id=1, athlete_id=10, access_token_enc="a", refresh_token_enc="r",
        expires_at=1, scope=None, athlete_name=None,
    )
    db.link_strava_account(
        user_id=2, athlete_id=20, access_token_enc="a", refresh_token_enc="r",
        expires_at=1, scope=None, athlete_name=None,
    )
    assert {r["user_id"] for r in db.list_strava_accounts()} == {1, 2}
