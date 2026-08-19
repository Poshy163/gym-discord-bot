"""Tests for the Hevy glue inside app.bot that doesn't need a live Discord/Hevy.

Focused on the body-measurement write-back: it is the only Hevy path that
*writes* to a member's external account, so its guards (feature flag, not
linked, nothing worth sending, API failure) are the ones worth pinning down.
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

import asyncio  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

import app.bot as bot_mod  # noqa: E402
from app import hevy_client  # noqa: E402


class _SyncLoop:
    """Runs the executor callable inline so ``run_in_executor`` works without a
    real event loop (every Hevy call goes through one)."""

    async def run_in_executor(self, _executor, func, *args):
        return func(*args)


@pytest.fixture()
def push_env(monkeypatch):
    """Hevy enabled, push on, a linked account, and a recording fake client."""
    monkeypatch.setattr(bot_mod, "HEVY_PUSH_BODYWEIGHT", True, raising=False)
    monkeypatch.setattr(bot_mod, "_hevy_enabled", lambda: True)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop(), raising=False)
    monkeypatch.setattr(
        bot_mod.db, "hevy_get", lambda uid: {"api_key_enc": "enc"},
    )
    monkeypatch.setattr(hevy_client, "decrypt_key", lambda token: "plain-key")
    pushed: list[tuple] = []

    def _push(api_key, date, fields):
        pushed.append((api_key, date, fields))
        return "created"

    monkeypatch.setattr(hevy_client, "push_body_measurement", _push)
    return pushed


def _run(coro):
    return asyncio.run(coro)


def test_push_sends_weight_and_scale_metrics(push_env):
    out = _run(bot_mod._hevy_push_bodyweight(
        7, 82.4,
        datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc),
        {"body_fat_pct": {"value": 18.5}, "fat_free_mass_kg": {"value": 65.0}},
    ))
    assert out == "created"
    api_key, date, fields = push_env[0]
    assert api_key == "plain-key"
    assert date == "2026-06-01"
    assert fields == {
        "weight_kg": 82.4, "fat_percent": 18.5, "lean_mass_kg": 65.0,
    }


def test_push_sends_weight_alone_when_there_is_no_scale_data(push_env):
    assert _run(bot_mod._hevy_push_bodyweight(7, 80.0)) == "created"
    assert push_env[0][2] == {"weight_kg": 80.0}


def test_push_reads_a_naive_timestamp_as_utc(push_env, monkeypatch):
    """A naive timestamp is stamped UTC before the local-day conversion, so it
    cannot land a day out or raise on the comparison."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(bot_mod, "DISPLAY_TZ", ZoneInfo("Australia/Adelaide"))
    # Naive 2026-06-01T21:30 -> UTC -> 2026-06-02T07:00 Adelaide.
    _run(bot_mod._hevy_push_bodyweight(7, 80.0, datetime(2026, 6, 1, 21, 30)))  # noqa: DTZ001
    assert push_env[0][1] == "2026-06-02"


def test_push_is_skipped_when_the_setting_is_off(push_env, monkeypatch):
    monkeypatch.setattr(bot_mod, "HEVY_PUSH_BODYWEIGHT", False, raising=False)
    assert _run(bot_mod._hevy_push_bodyweight(7, 80.0)) is None
    assert push_env == []


def test_push_is_skipped_for_an_unlinked_member(push_env, monkeypatch):
    monkeypatch.setattr(bot_mod.db, "hevy_get", lambda uid: None)
    assert _run(bot_mod._hevy_push_bodyweight(7, 80.0)) is None
    assert push_env == []


def test_push_is_skipped_when_the_weight_is_implausible(push_env):
    """The scale-glitch guard lives in the mapper, so nothing reaches Hevy."""
    assert _run(bot_mod._hevy_push_bodyweight(7, 6553.5)) is None
    assert push_env == []


def test_push_swallows_a_rejected_key(push_env, monkeypatch):
    """The weigh-in is already in the database by the time this runs — a Hevy
    outage or a revoked key must never surface as a failed weigh-in."""
    def _boom(*_a, **_k):
        raise hevy_client.HevyAuthError("nope")

    monkeypatch.setattr(hevy_client, "push_body_measurement", _boom)
    assert _run(bot_mod._hevy_push_bodyweight(7, 80.0)) is None


def test_push_swallows_a_transport_failure(push_env, monkeypatch):
    def _boom(*_a, **_k):
        raise hevy_client.HevyError("timeout")

    monkeypatch.setattr(hevy_client, "push_body_measurement", _boom)
    assert _run(bot_mod._hevy_push_bodyweight(7, 80.0)) is None


def test_lift_free_workout_still_builds_a_feed_embed():
    """The regression this fixes: a calisthenics session produced no lifts, was
    marked imported, and was then skipped for the feed — so it vanished."""
    summary = hevy_client.summarize_workout({
        "id": "c1", "title": "Calisthenics",
        "start_time": "2026-06-03T08:00:00Z",
        "exercises": [{"title": "Pull Up", "sets": [{"reps": 12}]}],
    })
    embed = bot_mod._hevy_workout_embed("alice", summary)
    assert embed.title == "Calisthenics"
    # No "0 kg volume" line for a session that lifted nothing.
    assert "volume" not in (embed.description or "")
    assert "12" in (embed.description or "")


def test_push_dates_the_entry_in_the_display_timezone(push_env, monkeypatch):
    """Hevy keys measurements by calendar date, and every other calendar day in
    this bot is local. Dating in UTC would file an ordinary Adelaide morning
    weigh-in under the previous day, and let two different local days collide
    on a single Hevy entry (the second silently overwriting the first)."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(bot_mod, "DISPLAY_TZ", ZoneInfo("Australia/Adelaide"))
    # 2026-08-16T21:30Z is Monday 07:00 in Adelaide (UTC+9:30) — a normal
    # morning weigh-in that UTC would misfile as Sunday the 16th.
    _run(bot_mod._hevy_push_bodyweight(
        7, 80.0, datetime(2026, 8, 16, 21, 30, tzinfo=timezone.utc),
    ))
    assert push_env[0][1] == "2026-08-17"


def test_two_weigh_ins_on_one_local_day_share_a_hevy_entry(push_env, monkeypatch):
    """The flip side of the same rule: a morning and an evening weigh-in on the
    same local day must collapse onto one date, which is what makes the
    create-then-merge path idempotent."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(bot_mod, "DISPLAY_TZ", ZoneInfo("Australia/Adelaide"))
    morning = datetime(2026, 8, 16, 21, 30, tzinfo=timezone.utc)   # Mon 07:00
    evening = datetime(2026, 8, 17, 10, 30, tzinfo=timezone.utc)   # Mon 20:00
    _run(bot_mod._hevy_push_bodyweight(7, 80.0, morning))
    _run(bot_mod._hevy_push_bodyweight(7, 80.5, evening))
    assert [call[1] for call in push_env] == ["2026-08-17", "2026-08-17"]
