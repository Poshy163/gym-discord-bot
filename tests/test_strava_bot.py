"""Tests for Strava glue inside app.bot that doesn't need a live Discord/Strava.

Only the pure/DB-touching helpers are exercised (deauthorization handling); the
network + Discord-send paths are covered indirectly via app.strava_client tests.
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

import json  # noqa: E402

import app.bot as bot_mod  # noqa: E402
from app import strava_client  # noqa: E402
from app.bot import (  # noqa: E402
    _build_calorie_ai_payload,
    _strava_event_subscription_ok,
    _strava_handle_deauth,
    _strava_should_post,
    _strava_weekly_lines,
    _weekday_full,
    db,
)


def _act(**kw):
    base = {
        "id": 1, "sport_type": "Run", "type": "Run",
        "distance": 5000, "moving_time": 1500, "elapsed_time": 1600,
    }
    base.update(kw)
    return strava_client.parse_activity(base)


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


# ---------------------------------------------------------------------------
# Posting filters
# ---------------------------------------------------------------------------

def test_should_post_sport_allowlist(monkeypatch):
    monkeypatch.setattr(bot_mod, "STRAVA_SPORT_ALLOW", {"run"})
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DISTANCE_M", 0.0)
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DURATION_S", 0)
    assert _strava_should_post(_act(sport_type="Run")) is True
    assert _strava_should_post(_act(sport_type="Ride")) is False


def test_should_post_min_distance_only_distance_sports(monkeypatch):
    monkeypatch.setattr(bot_mod, "STRAVA_SPORT_ALLOW", set())
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DISTANCE_M", 1000.0)
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DURATION_S", 0)
    assert _strava_should_post(_act(sport_type="Run", distance=500)) is False
    assert _strava_should_post(_act(sport_type="Run", distance=2000)) is True
    # A strength session has no distance — the distance floor must not block it.
    assert _strava_should_post(_act(sport_type="WeightTraining", distance=0)) is True


def test_should_post_min_duration(monkeypatch):
    monkeypatch.setattr(bot_mod, "STRAVA_SPORT_ALLOW", set())
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DISTANCE_M", 0.0)
    monkeypatch.setattr(bot_mod, "STRAVA_MIN_DURATION_S", 600)
    assert _strava_should_post(_act(moving_time=300, elapsed_time=300)) is False
    assert _strava_should_post(_act(moving_time=900)) is True


# ---------------------------------------------------------------------------
# Weekly recap formatting
# ---------------------------------------------------------------------------

def test_weekly_lines_metric_sorted_and_formatted():
    rows = [
        (200, 1, 0.0, 1800, 0.0),       # 1 activity, no distance/elevation
        (100, 3, 12000.0, 3600, 50.0),  # 3 activities, 12km, 1h, 50m climb
    ]
    lines = _strava_weekly_lines(rows, imperial=False)
    # Sorted by distance desc → user 100 first.
    assert lines[0].startswith("<@100> — **3** activities")
    assert "12.00 km" in lines[0] and "1:00:00" in lines[0] and "50 m climb" in lines[0]
    assert lines[1] == "<@200> — **1** activity · 30:00"


def test_weekly_lines_imperial():
    lines = _strava_weekly_lines([(1, 2, 5000.0, 600, 30.0)], imperial=True)
    assert "mi" in lines[0] and "ft climb" in lines[0]


# ---------------------------------------------------------------------------
# Webhook subscription-id guard
# ---------------------------------------------------------------------------

def test_event_subscription_ok(monkeypatch):
    # Unknown id → fail open (don't drop valid startup events).
    monkeypatch.setattr(bot_mod, "_strava_subscription_id", None)
    assert _strava_event_subscription_ok({"subscription_id": 5}) is True
    # Known id → match required.
    monkeypatch.setattr(bot_mod, "_strava_subscription_id", 5)
    assert _strava_event_subscription_ok({"subscription_id": 5}) is True
    assert _strava_event_subscription_ok({"subscription_id": 9}) is False
    # Missing/garbled id → fail open.
    assert _strava_event_subscription_ok({}) is True
    assert _strava_event_subscription_ok({"subscription_id": "x"}) is True


# ---------------------------------------------------------------------------
# Calorie AI-summary payload
# ---------------------------------------------------------------------------

def test_weekday_full():
    assert _weekday_full("2000-01-01") == "Saturday"
    assert _weekday_full("not-a-date") == "not-a-date"


def test_build_calorie_ai_payload():
    days = {"2026-06-15": 1800.0, "2026-06-16": 2200.0, "2026-06-17": 1500.0}
    prev = {"2026-06-08": 2000.0, "2026-06-09": 2000.0}
    payload = json.loads(_build_calorie_ai_payload("Josh", 2000, days, prev))
    assert payload["name"] == "Josh"
    assert payload["daily_target_kcal"] == 2000
    assert payload["days_logged"] == 3
    assert payload["week_total_kcal"] == 5500
    assert payload["week_avg_kcal"] == round((1800 + 2200 + 1500) / 3)
    assert payload["days_over_target"] == 1   # 2200
    assert payload["days_under_target"] == 2  # 1800, 1500
    assert payload["highest_day"]["kcal"] == 2200
    assert payload["lowest_day"]["kcal"] == 1500
    assert payload["previous_week_avg_kcal"] == 2000
    # per_day is date-sorted with weekday + vs_target signals.
    assert [d["date"] for d in payload["per_day"]] == sorted(days)
    assert payload["per_day"][0]["vs_target"] == -200
    assert "weekday" in payload["per_day"][0]


def test_build_calorie_ai_payload_no_previous_week():
    payload = json.loads(
        _build_calorie_ai_payload("Josh", 2000, {"2026-06-15": 1900.0}, {})
    )
    assert payload["previous_week_avg_kcal"] is None
