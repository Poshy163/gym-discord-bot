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
from types import SimpleNamespace  # noqa: E402

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


# ---------------------------------------------------------------------------
# One-time two-way bodyweight reconcile
# ---------------------------------------------------------------------------

class _Row(dict):
    """Stands in for a sqlite3.Row (which supports both [] and .keys())."""


@pytest.fixture()
def reconcile_env(monkeypatch, tmp_path):
    from zoneinfo import ZoneInfo
    from app.db import Database

    monkeypatch.setattr(bot_mod, "HEVY_PUSH_BODYWEIGHT", True, raising=False)
    monkeypatch.setattr(bot_mod, "_hevy_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "DISPLAY_TZ", ZoneInfo("UTC"))
    monkeypatch.setattr(bot_mod, "MAX_WEIGHT_KG", 500, raising=False)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop(), raising=False)
    monkeypatch.setattr(hevy_client, "decrypt_key", lambda token: "plain-key")

    database = Database(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)

    pushed: list[tuple] = []
    monkeypatch.setattr(
        hevy_client, "push_body_measurement",
        lambda api_key, day, fields: pushed.append((day, fields)) or "created",
    )

    def _remote(entries):
        monkeypatch.setattr(
            hevy_client, "fetch_body_measurements",
            lambda api_key, limit=200: list(entries),
        )

    database.hevy_link(7, 42, "enc")
    return SimpleNamespace(
        db=database, pushed=pushed, set_remote=_remote,
        row=lambda: database.hevy_get(7),
    )



def test_reconcile_fills_each_side_with_what_the_other_is_missing(reconcile_env):
    """The headline behaviour: an existing member's history merges both ways on
    the first poll after linking — nothing is duplicated, nothing is lost."""
    env = reconcile_env
    # The bot knows about two days Hevy has never seen.
    env.db.set_bodyweight(42, 7, 82.4,
                          recorded_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
    env.db.set_bodyweight(42, 7, 81.9,
                          recorded_at=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    # Hevy knows about one day the bot has never seen, plus one they share.
    env.set_remote([
        {"date": "2026-08-05", "weight_kg": 81.9},
        {"date": "2026-07-20", "weight_kg": 83.2, "fat_percent": 19.0,
         "lean_mass_kg": 64.0},
    ])

    out = _run(bot_mod._hevy_reconcile_measurements(env.row()))

    assert out == {"pushed": 1, "imported": 1}
    # Only the day Hevy lacked was pushed; the shared day was left alone.
    assert [day for day, _ in env.pushed] == ["2026-08-01"]
    # The day only Hevy had is now a real weigh-in, at local midday.
    history = env.db.bodyweight_history(42, 7, limit=50)
    assert [round(r["weight_kg"], 1) for r in history] == [83.2, 82.4, 81.9]
    assert history[0]["recorded_at"].startswith("2026-07-20T12:00")
    # ...with its body composition alongside it.
    fat = env.db.body_metric_history(7, "body_fat_pct", limit=10)
    assert [round(p["value"], 1) for p in fat] == [19.0]


def test_reconcile_runs_once_and_is_a_noop_when_forced_again(reconcile_env):
    """Re-running must not double-log: everything imported the first time is now
    one of the bot's own days, so the second pass sees both sides agreeing."""
    env = reconcile_env
    env.set_remote([{"date": "2026-07-20", "weight_kg": 83.2}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row()))["imported"] == 1
    # Marked done, so an ordinary poll skips it entirely.
    assert env.row()["measurements_synced_at"] is not None
    assert _run(bot_mod._hevy_reconcile_measurements(env.row())) == {
        "pushed": 0, "imported": 0,
    }
    # Even forced, it finds nothing new — no duplicate weigh-in.
    out = _run(bot_mod._hevy_reconcile_measurements(env.row(), force=True))
    assert out == {"pushed": 0, "imported": 0}
    assert len(env.db.bodyweight_history(42, 7, limit=50)) == 1


def test_reconcile_catches_up_an_already_linked_account(reconcile_env):
    """The marker is NULL for every account linked before this existed, which is
    what makes them pick the merge up on their next ordinary poll."""
    env = reconcile_env
    assert env.row()["measurements_synced_at"] is None
    env.set_remote([{"date": "2026-07-20", "weight_kg": 83.2}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row()))["imported"] == 1


def test_reconcile_ignores_a_circumference_only_entry(reconcile_env):
    """Hevy lets an entry record only tape measurements. That describes no
    weigh-in and must not become one."""
    env = reconcile_env
    env.set_remote([{"date": "2026-07-20", "waist": 80.0, "chest_cm": 95.0}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row()))["imported"] == 0
    assert env.db.bodyweight_history(42, 7, limit=50) == []


def test_reconcile_rejects_an_implausible_remote_weight(reconcile_env):
    env = reconcile_env
    env.set_remote([{"date": "2026-07-20", "weight_kg": 6553.5}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row()))["imported"] == 0
    assert env.db.bodyweight_history(42, 7, limit=50) == []


def test_reconcile_leaves_the_marker_unset_when_hevy_is_unreachable(reconcile_env,
                                                                    monkeypatch):
    """A failed fetch must not count as "reconciled", or the one chance to merge
    is burned by a transient outage."""
    env = reconcile_env

    def _boom(*_a, **_k):
        raise hevy_client.HevyError("timeout")

    monkeypatch.setattr(hevy_client, "fetch_body_measurements", _boom)
    assert _run(bot_mod._hevy_reconcile_measurements(env.row())) == {
        "pushed": 0, "imported": 0,
    }
    assert env.row()["measurements_synced_at"] is None


def test_reconcile_is_skipped_when_the_push_setting_is_off(reconcile_env,
                                                           monkeypatch):
    env = reconcile_env
    monkeypatch.setattr(bot_mod, "HEVY_PUSH_BODYWEIGHT", False, raising=False)
    env.set_remote([{"date": "2026-07-20", "weight_kg": 83.2}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row())) == {
        "pushed": 0, "imported": 0,
    }
    assert env.row()["measurements_synced_at"] is None


def test_reconcile_refuses_to_import_ancient_history(reconcile_env, monkeypatch):
    """The bug that put a lone 2025 weigh-in on a 2026 chart: a year-old Hevy
    entry lands in a stretch with no other data, where the trend either side of
    it is pure interpolation across the gap."""
    env = reconcile_env
    env.db.set_bodyweight(42, 7, 103.9,
                          recorded_at=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc))
    env.set_remote([
        {"date": "2025-11-06", "weight_kg": 103.0},   # far outside the horizon
        {"date": "2026-08-18", "weight_kg": 104.0},   # recent — still imported
    ])

    out = _run(bot_mod._hevy_reconcile_measurements(env.row()))

    assert out["imported"] == 1
    days = [r["recorded_at"][:10]
            for r in env.db.bodyweight_history(42, 7, limit=50)]
    assert "2025-11-06" not in days
    assert "2026-08-18" in days
    # Pushing outward is deliberately not age-limited — filling Hevy's own
    # history costs the member nothing.
    assert [day for day, _ in env.pushed] == ["2026-08-19"]


def test_reconcile_cannot_double_import_a_day_with_a_stale_view(reconcile_env,
                                                                 monkeypatch):
    """The production symptom: two identical 103.0 kg rows at the same instant.

    The "days Hevy has that the bot doesn't" set is computed once, before the
    import loop starts writing, and bodyweights has no unique constraint to
    reject a repeat. This pins the guard by making that view permanently stale --
    whatever made it stale in production -- and asserting the day still lands
    exactly once.
    """
    env = reconcile_env
    monkeypatch.setattr(env.db, "bodyweight_history", lambda *a, **k: [])
    env.set_remote([{"date": "2026-08-18", "weight_kg": 103.0}])

    first = _run(bot_mod._hevy_reconcile_measurements(env.row(), force=True))
    second = _run(bot_mod._hevy_reconcile_measurements(env.row(), force=True))

    assert first["imported"] == 1
    assert second["imported"] == 0
    monkeypatch.undo()
    history = env.db.bodyweight_history(42, 7, limit=50)
    assert len(history) == 1, f"double import: {[dict(r) for r in history]}"


def test_reconcile_claim_blocks_a_concurrent_run(reconcile_env):
    """A second run holding the same stale account row must not start. The claim
    is an atomic UPDATE ... WHERE measurements_synced_at IS NULL, so only the
    writer that actually matched a NULL row proceeds."""
    env = reconcile_env
    stale = env.row()
    assert stale["measurements_synced_at"] is None
    assert env.db.hevy_claim_measurement_sync(7) is True
    # The loser still believes it has work to do, and must bail anyway.
    assert env.db.hevy_claim_measurement_sync(7) is False
    assert _run(bot_mod._hevy_reconcile_measurements(stale)) == {
        "pushed": 0, "imported": 0,
    }
    # Releasing hands the retry back.
    env.db.hevy_release_measurement_sync(7)
    assert env.row()["measurements_synced_at"] is None


def test_reconcile_hands_the_claim_back_when_hevy_fails(reconcile_env,
                                                         monkeypatch):
    """Claiming before the work must not cost the retry: a transient outage has
    to leave the marker NULL, or the one chance to merge is burned."""
    env = reconcile_env

    def _boom(*_a, **_k):
        raise hevy_client.HevyError("timeout")

    monkeypatch.setattr(hevy_client, "fetch_body_measurements", _boom)
    _run(bot_mod._hevy_reconcile_measurements(env.row()))
    assert env.row()["measurements_synced_at"] is None

    # ...and the retry then works.
    env.set_remote([{"date": "2026-08-18", "weight_kg": 104.0}])
    assert _run(bot_mod._hevy_reconcile_measurements(env.row()))["imported"] == 1


# ---------------------------------------------------------------------------
# The one-off "these lifts were re-filed" chat notice
# ---------------------------------------------------------------------------

def test_recanon_notice_is_posted_once_to_the_feed(monkeypatch):
    """A rename merges two histories into one PR and one leaderboard line, so a
    member's best can move without them lifting anything. That gets said out
    loud rather than appearing as an unexplained change."""
    from unittest.mock import AsyncMock

    sent: list = []
    channel = SimpleNamespace(send=AsyncMock(side_effect=lambda **kw: sent.append(kw)))
    monkeypatch.setattr(bot_mod, "HEVY_FEED_CHANNEL_ID", 999, raising=False)
    monkeypatch.setattr(bot_mod.bot, "get_channel", lambda cid: channel)

    notices = [{"butterfly\u2192pec dec": 16,
                "seated shoulder press\u2192shoulder press": 2}]
    monkeypatch.setattr(
        bot_mod.db, "take_hevy_recanon_notice",
        lambda: notices.pop() if notices else {},
    )

    _run(bot_mod._hevy_announce_recanon())
    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert "18" in (embed.description or "")          # 16 + 2 lifts
    body = embed.fields[0].value
    assert "**butterfly** → **pec dec** (16 lifts)" in body
    assert "**seated shoulder press** → **shoulder press** (2 lifts)" in body
    # Biggest rename first.
    assert body.index("butterfly") < body.index("seated shoulder")

    # Claimed read-and-clear, so a restart loop cannot repeat it.
    _run(bot_mod._hevy_announce_recanon())
    assert len(sent) == 1


def test_recanon_notice_is_dropped_when_no_feed_channel(monkeypatch):
    """Holding it forever would mean announcing a months-old rename the day a
    feed channel is finally configured."""
    monkeypatch.setattr(bot_mod, "HEVY_FEED_CHANNEL_ID", None, raising=False)
    monkeypatch.setattr(
        bot_mod.db, "take_hevy_recanon_notice", lambda: {"a\u2192b": 1},
    )
    _run(bot_mod._hevy_announce_recanon())  # must not raise


def test_recanon_notice_silent_when_nothing_was_renamed(monkeypatch):
    from unittest.mock import AsyncMock

    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(bot_mod, "HEVY_FEED_CHANNEL_ID", 999, raising=False)
    monkeypatch.setattr(bot_mod.bot, "get_channel", lambda cid: channel)
    monkeypatch.setattr(bot_mod.db, "take_hevy_recanon_notice", lambda: {})
    _run(bot_mod._hevy_announce_recanon())
    channel.send.assert_not_awaited()
