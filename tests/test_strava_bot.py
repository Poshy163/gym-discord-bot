"""Tests for Strava glue inside app.bot that doesn't need a live Discord/Strava.

Only the pure/DB-touching helpers are exercised (deauthorization handling); the
network + Discord-send paths are covered indirectly via app.strava_client tests.
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402

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


def _target_rows(kcal: float, weekend_kcal: float | None = None):
    """Rule rows shaped the way ``db.nutrition_target_rows`` hands them over."""
    rows = [{
        "macro": "kcal", "scope": "default", "value": kcal,
        "effective_from": "0001-01-01", "set_at": "2026-01-01T00:00:00+00:00",
    }]
    if weekend_kcal is not None:
        rows.append({
            "macro": "kcal", "scope": "weekend", "value": weekend_kcal,
            "effective_from": "0001-01-01",
            "set_at": "2026-01-01T00:00:00+00:00",
        })
    return rows


def test_build_calorie_ai_payload():
    # Mon/Tue/Wed 2026-06-15..17 — all weekdays, so a single all-week target
    # behaves exactly as it did before per-day targets existed.
    days = {"2026-06-15": 1800.0, "2026-06-16": 2200.0, "2026-06-17": 1500.0}
    prev = {"2026-06-08": 2000.0, "2026-06-09": 2000.0}
    payload = json.loads(
        _build_calorie_ai_payload("Josh", _target_rows(2000), days, prev)
    )
    assert payload["name"] == "Josh"
    assert payload["daily_target_kcal"] == 2000
    assert payload["split_targets"] is None
    assert payload["days_logged"] == 3
    assert payload["week_total_kcal"] == 5500
    assert payload["week_avg_kcal"] == round((1800 + 2200 + 1500) / 3)
    assert payload["days_over_target"] == 1   # 2200
    assert payload["days_under_target"] == 2  # 1800, 1500
    assert payload["highest_day"]["kcal"] == 2200
    assert payload["lowest_day"]["kcal"] == 1500
    assert payload["previous_week_avg_kcal"] == 2000
    assert payload["weekday_avg_kcal"] == round((1800 + 2200 + 1500) / 3)
    assert payload["weekend_avg_kcal"] is None
    # per_day is date-sorted with weekday + vs_target signals.
    assert [d["date"] for d in payload["per_day"]] == sorted(days)
    assert payload["per_day"][0]["vs_target"] == -200
    assert payload["per_day"][0]["target_kcal"] == 2000
    assert "weekday" in payload["per_day"][0]


def test_build_calorie_ai_payload_scores_each_day_against_its_own_target():
    # Fri 2026-06-19 (weekday, 2000) and Sat 2026-06-20 (weekend, 2800). The big
    # Saturday is *under* its own target, not 600 over the weekday one.
    days = {"2026-06-19": 1900.0, "2026-06-20": 2600.0}
    payload = json.loads(
        _build_calorie_ai_payload("Josh", _target_rows(2000, 2800), days, {})
    )
    assert payload["split_targets"] == {
        "weekday_target_kcal": 2000, "weekend_target_kcal": 2800,
    }
    assert payload["days_over_target"] == 0
    assert payload["days_under_target"] == 2
    assert payload["weekday_avg_kcal"] == 1900
    assert payload["weekend_avg_kcal"] == 2600
    per_day = {d["date"]: d for d in payload["per_day"]}
    assert per_day["2026-06-20"]["target_kcal"] == 2800
    assert per_day["2026-06-20"]["vs_target"] == -200
    # The headline target averages the days they logged, rather than pretending
    # one number applied to both.
    assert payload["daily_target_kcal"] == 2400


def test_build_calorie_ai_payload_leaves_untargeted_days_null():
    # They stopped tracking on the 16th. That day was still logged, but it had
    # no target — reporting it as "1,450 over" would be an invented number.
    rows = _target_rows(1500) + [{
        "macro": "kcal", "scope": "default", "value": None,
        "effective_from": "2026-06-16", "set_at": "2026-06-16T00:00:00+00:00",
    }]
    days = {"2026-06-15": 1400.0, "2026-06-16": 1450.0}
    payload = json.loads(_build_calorie_ai_payload("Josh", rows, days, {}))
    per_day = {d["date"]: d for d in payload["per_day"]}
    assert per_day["2026-06-16"]["target_kcal"] is None
    assert per_day["2026-06-16"]["vs_target"] is None
    assert payload["days_over_target"] == 0
    assert payload["days_under_target"] == 1
    # The untargeted day doesn't drag the headline target toward zero either.
    assert payload["daily_target_kcal"] == 1500


def test_build_calorie_ai_payload_no_previous_week():
    payload = json.loads(
        _build_calorie_ai_payload(
            "Josh", _target_rows(2000), {"2026-06-15": 1900.0}, {},
        )
    )
    assert payload["previous_week_avg_kcal"] is None


# ---------------------------------------------------------------------------
# Webhook auto-subscribe: permanent (auth) failures must NOT retry, must log an
# actionable hint, and must NEVER put the client_secret in a log line.
# ---------------------------------------------------------------------------

_FAKE_SECRET = "FAKE_SECRET_DO_NOT_LOG"
_INACTIVE_403_BODY = (
    '{"message":"Forbidden","errors":[{"resource":"Application",'
    '"field":"Status","code":"Inactive"}]}'
)


async def _no_sleep(*_a, **_k):
    return None


class _SyncLoop:
    """Runs the executor callable inline so _strava_ensure_subscription's
    ``await bot.loop.run_in_executor(None, fn)`` works without a real loop."""

    async def run_in_executor(self, _executor, func, *args):
        return func(*args)


def test_autosubscribe_stops_and_hints_on_permanent_403(monkeypatch, caplog):
    """End-to-end: a real 403 (Inactive app) from view_subscriptions flows through
    the real ensure + startup path. It must NOT retry (one HTTP call), must log
    the settings/api hint, and the client_secret must never reach the log."""
    # Configure Strava so _strava_cfg() is 'configured' with our fake secret.
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", _FAKE_SECRET)
    monkeypatch.setenv("STRAVA_PUBLIC_URL", "https://bot.example.com/")
    monkeypatch.delenv("STRAVA_REDIRECT_URI", raising=False)
    monkeypatch.delenv("STRAVA_WEBHOOK_CALLBACK_URL", raising=False)

    calls = {"get": 0}

    class _Resp:
        status_code = 403
        text = _INACTIVE_403_BODY

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            calls["get"] += 1
            return _Resp()

    monkeypatch.setattr(strava_client, "requests", _FakeRequests)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop())
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _no_sleep)

    with caplog.at_level(logging.INFO, logger="gymbot"):
        asyncio.run(bot_mod._strava_autosubscribe_startup())

    # Permanent failure ⇒ exactly one attempt, no 4x hammering.
    assert calls["get"] == 1
    # The actionable hint is logged…
    assert "https://www.strava.com/settings/api" in caplog.text
    assert "Inactive" in caplog.text  # Strava's real reason is surfaced
    # …and the secret NEVER is, anywhere in the captured logs.
    assert _FAKE_SECRET not in caplog.text
    assert "client_secret" not in caplog.text


def test_ensure_subscription_classifies_autherror(monkeypatch):
    """_strava_ensure_subscription must tag a StravaAuthError as 'autherror:'
    (permanent) with the URL-free body — not the generic 'error:' prefix."""
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", _FAKE_SECRET)
    monkeypatch.setenv("STRAVA_PUBLIC_URL", "https://bot.example.com/")

    class _Resp:
        status_code = 403
        text = _INACTIVE_403_BODY

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(strava_client, "requests", _FakeRequests)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop())

    result = asyncio.run(bot_mod._strava_ensure_subscription())
    assert result.startswith("autherror:")
    assert "Inactive" in result
    assert _FAKE_SECRET not in result
    assert "client_secret" not in result


def test_ensure_subscription_429_5xx_is_transient_not_autherror(monkeypatch):
    """A 429/5xx reaches ensure() as a StravaAuthError too, but is transient: it
    must classify as the retrying 'error:' prefix, NOT permanent 'autherror:',
    so a rate-limit or Strava outage during startup isn't mistaken for an
    Inactive app (and still never leaks the secret)."""
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", _FAKE_SECRET)
    monkeypatch.setenv("STRAVA_PUBLIC_URL", "https://bot.example.com/")

    class _Resp:
        status_code = 503
        text = "Service Unavailable"

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(strava_client, "requests", _FakeRequests)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop())

    result = asyncio.run(bot_mod._strava_ensure_subscription())
    assert result.startswith("error:")
    assert not result.startswith("autherror:")
    assert _FAKE_SECRET not in result


def test_autosubscribe_retries_transient_then_gives_up(monkeypatch, caplog):
    """A *transient* 'error:' result keeps the original 4x retry loop and the
    'gave up — check the public callback is reachable' give-up message."""
    calls = {"n": 0}

    async def _fake_ensure():
        calls["n"] += 1
        return "error:tunnel not up yet"

    monkeypatch.setattr(bot_mod, "_strava_ensure_subscription", _fake_ensure)
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _no_sleep)

    with caplog.at_level(logging.WARNING, logger="gymbot"):
        asyncio.run(bot_mod._strava_autosubscribe_startup())

    assert calls["n"] == 4  # transient ⇒ full retry budget
    assert "gave up" in caplog.text
    assert "settings/api" not in caplog.text  # not treated as permanent


def test_autosubscribe_no_retry_on_autherror_prefix(monkeypatch, caplog):
    """Unit-level classifier: an 'autherror:' result short-circuits to one
    attempt and the actionable hint, without a full retry loop."""
    calls = {"n": 0}

    async def _fake_ensure():
        calls["n"] += 1
        return f"autherror:Subscription view failed (403): {_INACTIVE_403_BODY}"

    monkeypatch.setattr(bot_mod, "_strava_ensure_subscription", _fake_ensure)
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _no_sleep)

    with caplog.at_level(logging.WARNING, logger="gymbot"):
        asyncio.run(bot_mod._strava_autosubscribe_startup())

    assert calls["n"] == 1
    assert "settings/api" in caplog.text
    assert _FAKE_SECRET not in caplog.text


def test_autosubscribe_transient_error_never_logs_secret(monkeypatch, caplog):
    """A transient requests failure stringifies the secret-bearing request URL
    (query string includes client_secret). The 'error:' path must scrub it so the
    retry/give-up warnings can never leak the secret — while still retrying."""
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", _FAKE_SECRET)
    monkeypatch.setenv("STRAVA_PUBLIC_URL", "https://bot.example.com/")

    # Exactly what requests puts in a ConnectionError str(): the full URL with the
    # secret in the query string. A plain RuntimeError is NOT a StravaAuthError,
    # so ensure() classifies it as the transient "error:" branch.
    leaky = (
        "HTTPSConnectionPool(host='www.strava.com', port=443): Max retries "
        "exceeded with url: /api/v3/push_subscriptions?client_id=123&"
        f"client_secret={_FAKE_SECRET} (Caused by ConnectTimeoutError(...))"
    )

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            raise RuntimeError(leaky)

    monkeypatch.setattr(strava_client, "requests", _FakeRequests)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop())
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _no_sleep)

    result = asyncio.run(bot_mod._strava_ensure_subscription())
    assert result.startswith("error:")            # transient, not autherror
    assert _FAKE_SECRET not in result             # scrubbed at the source
    assert "<redacted>" in result

    with caplog.at_level(logging.WARNING, logger="gymbot"):
        asyncio.run(bot_mod._strava_autosubscribe_startup())
    assert "gave up" in caplog.text               # still retried to exhaustion
    assert _FAKE_SECRET not in caplog.text         # and never leaked the secret
