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


def test_workout_embed_labels_the_routine_when_it_adds_information():
    summary = hevy_client.summarize_workout({
        "title": "Wednesday session", "routine_id": "r1",
        "exercises": [{"title": "Bench Press (Barbell)",
                       "sets": [{"weight_kg": 100, "reps": 5}]}],
    })
    routines = {"r1": {"title": "Arms", "folder": "Push Pull"}}
    embed = bot_mod._hevy_workout_embed("poshy", summary, None, None, None, routines)
    assert embed.title == "Wednesday session — Push Pull / Arms"


def test_workout_embed_skips_a_redundant_routine_label():
    """Most members name the workout after the routine — "Arms — from Arms"
    is noise, so an identical title suppresses the label."""
    summary = hevy_client.summarize_workout({
        "title": "Arms", "routine_id": "r1",
        "exercises": [{"title": "Bench Press (Barbell)",
                       "sets": [{"weight_kg": 100, "reps": 5}]}],
    })
    routines = {"r1": {"title": "Arms", "folder": None}}
    embed = bot_mod._hevy_workout_embed("poshy", summary, None, None, None, routines)
    assert embed.title == "Arms"


# ---------------------------------------------------------------------------
# /hevy admin subcommands — settings written through the dashboard's own path
# ---------------------------------------------------------------------------

def test_admin_save_writes_through_settings_service(monkeypatch, tmp_path):
    """The command must produce a real, validated, history-tracked settings
    write — the same row the dashboard would create — then rebind in-process."""
    from unittest.mock import AsyncMock, MagicMock
    from app.db import Database
    from app import secretbox

    database = Database(tmp_path / "gym.sqlite3")
    box = secretbox.SecretBox.open_at(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)
    monkeypatch.setattr(bot_mod, "_box", box)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", {42}, raising=False)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop(), raising=False)
    rebinds: list[bool] = []
    monkeypatch.setattr(bot_mod, "_bind_config", lambda cfg: rebinds.append(True))

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()

    result = asyncio.run(bot_mod._hevy_admin_save(
        interaction, "HEVY_POLL_MINUTES", "30",
    ))

    assert result is not None and result["ok"] is True
    interaction.response.defer.assert_awaited()  # 3s deadline: defer first
    assert rebinds, "the worker must rebind its own config after a write"
    # The write is real and validated, not an in-memory flag.
    from app import config as config_mod
    assert config_mod.load(database)["HEVY_POLL_MINUTES"] == 30
    row = database._connection.execute(
        "SELECT value FROM app_settings WHERE key = 'HEVY_POLL_MINUTES'"
    ).fetchone()
    assert row["value"] == "30"
    database.close()


def test_admin_save_refuses_non_admins(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", {42}, raising=False)
    interaction = MagicMock()
    interaction.user.id = 7   # not an admin
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()

    out = asyncio.run(bot_mod._hevy_admin_save(
        interaction, "HEVY_POLL_MINUTES", "5",
    ))
    assert out is None
    interaction.response.send_message.assert_awaited()   # the refusal
    interaction.response.defer.assert_not_awaited()      # never got that far


def test_admin_save_rejects_an_invalid_value(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from app.db import Database
    from app import secretbox

    database = Database(tmp_path / "gym.sqlite3")
    box = secretbox.SecretBox.open_at(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)
    monkeypatch.setattr(bot_mod, "_box", box)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", {42}, raising=False)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop(), raising=False)
    monkeypatch.setattr(bot_mod, "_bind_config", lambda cfg: None)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()

    result = asyncio.run(bot_mod._hevy_admin_save(
        interaction, "HEVY_POLL_MINUTES", "not-a-number",
    ))
    assert result is not None and result.get("ok") is False
    assert bot_mod._hevy_admin_reply(result, "x").startswith("❌")
    database.close()


def test_import_stamps_shape_even_for_already_imported_workouts(monkeypatch,
                                                                tmp_path):
    """The self-healing backfill: an account linked before the shape columns
    existed gets its page-1 workouts stamped on the next ordinary poll, because
    the stamp runs above the dedup early-return."""
    from app.db import Database

    database = Database(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)
    database.hevy_link(7, 42, "enc")
    workout = {
        "id": "w1", "title": "Arms", "routine_id": "r1",
        "start_time": "2026-08-19T13:16:09+00:00",
        "updated_at": "2026-08-19T13:40:46.788Z",
        "exercises": [{"title": "Bench Press (Barbell)",
                       "sets": [{"weight_kg": 100, "reps": 5}]}],
    }
    # Simulate the pre-shape era: claimed, no shape.
    database.hevy_mark_workout(7, "w1")
    assert database.hevy_routine_usage(7) == []

    out = bot_mod._hevy_import_workout(7, 42, "poshy", workout)
    assert out is None                          # dedup still holds — no re-import
    usage = database.hevy_routine_usage(7)
    assert [(r["routine_id"], r["sessions"]) for r in usage] == [("r1", 1)]
    database.close()


def test_import_survives_a_failing_shape_stamp(monkeypatch, tmp_path):
    """The shape is bookkeeping, lifts are the product — a stamp failure must
    never cost the import."""
    from app.db import Database

    database = Database(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)
    database.hevy_link(7, 42, "enc")

    def _boom(*_a, **_k):
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(database, "hevy_record_shape", _boom)
    workout = {
        "id": "w1", "title": "Arms",
        "start_time": "2026-08-19T13:16:09+00:00",
        "exercises": [{"title": "Bench Press (Barbell)",
                       "sets": [{"weight_kg": 100, "reps": 5}]}],
    }
    out = bot_mod._hevy_import_workout(7, 42, "poshy", workout)
    assert out is not None and len(out["lifts"]) == 1
    database.close()


def test_breakdown_budget_never_slices_a_line(monkeypatch):
    """The 1024-char field cap is met by dropping whole trailing lines into the
    "…and N more" count, never by cutting a line mid-markdown."""
    summary = hevy_client.summarize_workout({
        "title": "Everything day",
        "exercises": [
            # Bulky on purpose: the field must overflow 1024 chars so the
            # budget path (whole-line drop + "…and N more" tail) engages.
            {"title": f"Extremely Long Exercise Name Variant {i} (Machine)" * 2,
             "superset_id": i % 3,
             "sets": [{"weight_kg": 50 + i + j, "reps": 8, "rpe": 9}
                      for j in range(3)]}
            for i in range(16)
        ],
    })
    embed = bot_mod._hevy_workout_embed("poshy", summary)
    field = next(f for f in embed.fields if f.name == "Exercises")
    assert len(field.value) <= 1024
    lines = field.value.split("\n")
    # Every line is intact: it either starts with the superset glyph or bold
    # markdown, and bold never dangles unclosed.
    for line in lines:
        assert line.startswith(("🔗 ", "**", "…")), repr(line)
        assert line.count("**") % 2 == 0, repr(line)
    assert lines[-1].startswith("…and ")


def test_stair_machine_shows_floors_with_its_template(monkeypatch):
    templates = hevy_client.index_templates([
        {"id": "TPL-STAIR", "title": "Stair Machine", "type": "floors_duration"},
    ])
    summary = hevy_client.summarize_workout({
        "title": "Cardio",
        "exercises": [{
            "title": "Stair Machine", "exercise_template_id": "TPL-STAIR",
            "sets": [{"duration_seconds": 600, "custom_metric": 42}],
        }],
    })
    embed = bot_mod._hevy_workout_embed("poshy", summary, None, templates)
    field = next(f for f in embed.fields if f.name == "Exercises")
    assert "42 floors" in field.value
    # Without the template the raw number stays out of the embed.
    embed2 = bot_mod._hevy_workout_embed("poshy", summary)
    field2 = next(f for f in embed2.fields if f.name == "Exercises")
    assert "42" not in field2.value


# ---------------------------------------------------------------------------
# Events sync: cursor policy, admission rules, correction notices
# ---------------------------------------------------------------------------

def _dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_cursor_stays_put_on_a_capped_read():
    """Events are newest-first: a capped read holds the NEWEST 50, so any
    advance would jump the cursor clean over the unread older remainder."""
    out = bot_mod._hevy_events_next_cursor(
        _dt(2026, 8, 1), [_dt(2026, 8, 19)], capped=True,
        request_start=_dt(2026, 8, 20),
    )
    assert out is None


def test_cursor_advances_to_hevys_own_clock():
    out = bot_mod._hevy_events_next_cursor(
        _dt(2026, 8, 1), [_dt(2026, 8, 3), _dt(2026, 8, 5)], capped=False,
        request_start=_dt(2026, 8, 20),
    )
    assert out == _dt(2026, 8, 5)


def test_cursor_quiet_poll_advances_with_a_margin():
    out = bot_mod._hevy_events_next_cursor(
        _dt(2026, 8, 1), [], capped=False, request_start=_dt(2026, 8, 20, 12, 0),
    )
    assert out == _dt(2026, 8, 20, 11, 58)


def test_cursor_never_goes_backwards():
    out = bot_mod._hevy_events_next_cursor(
        _dt(2026, 8, 20), [_dt(2026, 8, 19)], capped=False,
        request_start=_dt(2026, 8, 20, 0, 1),
    )
    assert out is None


def test_best_change_lines_are_symmetric():
    lines = bot_mod._hevy_best_change_lines(
        {"bench press": 100.0, "squat": 180.0, "curl": 30.0},
        {"bench press": 140.0, "squat": 170.0, "curl": 30.0},
    )
    assert any("140" in l and "100" in l for l in lines)   # raised
    assert any("170" in l and "180" in l for l in lines)   # lowered
    assert not any("curl" in l for l in lines)              # unchanged: silent
    assert bot_mod._hevy_best_change_lines({}, {}) == []


@pytest.fixture()
def events_env(monkeypatch, tmp_path):
    from app.db import Database

    database = Database(tmp_path / "gym.sqlite3")
    monkeypatch.setattr(bot_mod, "db", database)
    monkeypatch.setattr(bot_mod, "HEVY_EDIT_SYNC", True, raising=False)
    monkeypatch.setattr(bot_mod.bot, "loop", _SyncLoop(), raising=False)
    database.hevy_link(7, 42, "enc")

    def _seed(wid, weights, stamp="stamp-1"):
        from app.parser import Lift
        database.hevy_mark_workout(7, wid)
        lifts = [Lift(equipment="bench press", weight_kg=w, reps=5,
                      raw="hevy:Bench", confident=True, structured=True)
                 for w in weights]
        n = database.add_lifts(guild_id=42, user_id=7, username="u",
                               lifts=lifts, hevy_workout_id=wid)
        database.hevy_record_import(7, wid, n)
        database.hevy_record_shape(7, wid, None, "Arms",
                                   "2026-08-19T13:16:09+00:00", stamp)
        return n

    return SimpleNamespace(db=database, seed=_seed,
                           row=lambda: database.hevy_get(7))


def test_apply_deleted_event_retracts_and_reports(events_env):
    env = events_env
    env.seed("w1", [100.0, 120.0])
    outcome, notice = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "deleted", "workout_id": "w1", "workout": None,
                    "at": "2026-08-19T14:00:00Z"},
        {}, "poshy",
    ))
    assert outcome == "retracted"
    assert notice is not None
    assert "withdrawn" in (notice.fields[0].value if notice.fields else
                           notice.description)
    assert env.db.bests_for_equipment(7, ["bench press"]) == {}


def test_apply_updated_event_replaces_and_reports_the_moved_best(events_env):
    env = events_env
    env.seed("w1", [100.0])
    workout = {
        "id": "w1", "title": "Arms",
        "start_time": "2026-08-19T13:16:09+00:00",
        "updated_at": "stamp-2",
        "exercises": [{"title": "Bench Press (Barbell)",
                       "sets": [{"weight_kg": 140, "reps": 5}]}],
    }
    outcome, notice = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "updated", "workout_id": "w1",
                    "workout": workout, "at": "stamp-2"},
        {}, "poshy",
    ))
    assert outcome == "replaced"
    assert notice is not None and "140" in notice.fields[0].value
    assert env.db.bests_for_equipment(7, ["bench press"]) == {
        "bench press": 140.0,
    }


def test_apply_updated_event_short_circuits_on_the_same_stamp(events_env):
    env = events_env
    env.seed("w1", [100.0], stamp="stamp-1")
    workout = {"id": "w1", "updated_at": "stamp-1", "exercises": []}
    outcome, notice = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "updated", "workout_id": "w1",
                    "workout": workout, "at": "stamp-1"},
        {}, "poshy",
    ))
    assert outcome == "unchanged" and notice is None
    # Nothing was deleted by the no-op.
    assert env.db.bests_for_equipment(7, ["bench press"]) == {
        "bench press": 100.0,
    }


def test_apply_event_refuses_legacy_imports(events_env):
    """A pre-provenance import has NULL lifts_linked: replacing it would delete
    0 rows and insert N, silently doubling the member's lifts."""
    env = events_env
    env.db.hevy_mark_workout(7, "old")     # claimed, but never linked
    workout = {"id": "old", "updated_at": "s",
               "exercises": [{"title": "Bench Press (Barbell)",
                              "sets": [{"weight_kg": 100, "reps": 5}]}]}
    outcome, notice = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "updated", "workout_id": "old",
                    "workout": workout, "at": "s"},
        {}, "poshy",
    ))
    assert outcome == "skipped-legacy" and notice is None
    assert env.db.bests_for_equipment(7, ["bench press"]) == {}


def test_apply_event_never_creates(events_env):
    """Events act only on workouts already in the ledger — an edit to an
    ancient workout the backfill never reached must not import lifts."""
    env = events_env
    workout = {"id": "never-seen", "updated_at": "s",
               "exercises": [{"title": "Bench Press (Barbell)",
                              "sets": [{"weight_kg": 100, "reps": 5}]}]}
    outcome, _ = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "updated", "workout_id": "never-seen",
                    "workout": workout, "at": "s"},
        {}, "poshy",
    ))
    assert outcome == "skipped-unknown"
    assert env.db.bests_for_equipment(7, ["bench press"]) == {}


def test_apply_event_refuses_on_provenance_drift(events_env):
    """An admin purge deleted some of this workout's rows on purpose; a replace
    would resurrect them, so the edit is refused."""
    env = events_env
    env.seed("w1", [100.0, 120.0])
    with env.db._conn() as c:
        c.execute("DELETE FROM lifts WHERE user_id = 7 AND weight_kg = 120.0")
    workout = {"id": "w1", "updated_at": "stamp-9",
               "exercises": [{"title": "Bench Press (Barbell)",
                              "sets": [{"weight_kg": 140, "reps": 5}]}]}
    outcome, _ = _run(bot_mod._hevy_apply_event(
        env.row(), {"kind": "updated", "workout_id": "w1",
                    "workout": workout, "at": "stamp-9"},
        {}, "poshy",
    ))
    assert outcome == "skipped-drift"
    assert env.db.bests_for_equipment(7, ["bench press"]) == {
        "bench press": 100.0,
    }


def test_drain_seeds_the_cursor_with_zero_requests(events_env, monkeypatch):
    env = events_env

    def _boom(*_a, **_k):
        raise AssertionError("the seeding drain must not call Hevy")

    monkeypatch.setattr(hevy_client, "walk_workout_events", _boom)
    out = _run(bot_mod._hevy_drain_events(
        env.row(), "key", {}, "poshy",
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    ))
    assert out == {"replaced": 0, "retracted": 0, "notices": []}
    assert env.row()["events_since"] is not None


def test_drain_applies_oldest_first_and_advances_the_cursor(events_env,
                                                            monkeypatch):
    env = events_env
    env.seed("w1", [100.0])
    env.db.hevy_set_events_since(
        7, datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc),
    )
    events = [   # newest-first, as the API delivers them
        {"type": "deleted", "id": "w1", "deleted_at": "2026-08-19T15:00:00Z"},
        {"type": "updated", "workout": {
            "id": "w1", "title": "Arms", "updated_at": "2026-08-19T14:00:00Z",
            "start_time": "2026-08-19T13:16:09+00:00",
            "exercises": [{"title": "Bench Press (Barbell)",
                           "sets": [{"weight_kg": 140, "reps": 5}]}],
        }},
    ]
    monkeypatch.setattr(
        hevy_client, "walk_workout_events",
        lambda api_key, since, max_pages=5: (list(events), False),
    )
    out = _run(bot_mod._hevy_drain_events(
        env.row(), "key", {}, "poshy",
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    ))
    # Edit applied first (oldest), then the deletion — the end state is gone.
    assert out["replaced"] == 1 and out["retracted"] == 1
    assert env.db.bests_for_equipment(7, ["bench press"]) == {}
    # Cursor advanced to Hevy's newest event time, not the bot clock.
    assert env.row()["events_since"].startswith("2026-08-19T15:00:00")


def test_drain_leaves_the_cursor_on_a_capped_read(events_env, monkeypatch):
    env = events_env
    env.db.hevy_set_events_since(
        7, datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc),
    )
    before = env.row()["events_since"]
    monkeypatch.setattr(
        hevy_client, "walk_workout_events",
        lambda api_key, since, max_pages=5: (
            [{"type": "deleted", "id": "nope",
              "deleted_at": "2026-08-19T15:00:00Z"}], True,
        ),
    )
    _run(bot_mod._hevy_drain_events(
        env.row(), "key", {}, "poshy",
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    ))
    assert env.row()["events_since"] == before


def test_drain_is_gated_by_the_config_switch(events_env, monkeypatch):
    env = events_env
    monkeypatch.setattr(bot_mod, "HEVY_EDIT_SYNC", False, raising=False)
    out = _run(bot_mod._hevy_drain_events(
        env.row(), "key", {}, "poshy",
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    ))
    assert out == {"replaced": 0, "retracted": 0, "notices": []}
    assert env.row()["events_since"] is None   # not even seeded


# ---------------------------------------------------------------------------
# Shape backfill loop + the one-shot release note
# ---------------------------------------------------------------------------

def test_shape_backfill_fetches_and_stamps(events_env, monkeypatch):
    """36 of 47 imports had no shape on real data — /hevy routines was blind to
    them. The trickle backfill fetches each by id and stamps it; a 404 (deleted
    in Hevy) is marked gone so it is never fetched again."""
    env = events_env
    with env.db._conn() as c:
        for wid in ("old-1", "old-2", "old-gone"):
            c.execute("INSERT INTO hevy_imported (user_id, workout_id,"
                      " imported_at) VALUES (7, ?, '2026-01-01T00:00:00+00:00')",
                      (wid,))

    payloads = {
        "old-1": {"id": "old-1", "title": "Push", "routine_id": "r1",
                  "start_time": "2026-01-01T10:00:00+00:00", "exercises": []},
        "old-2": {"id": "old-2", "title": "Pull", "routine_id": "r1",
                  "start_time": "2026-01-03T10:00:00+00:00", "exercises": []},
        "old-gone": None,   # 404 — deleted in Hevy
    }
    fetched: list[str] = []

    def _fetch(api_key, wid):
        fetched.append(wid)
        return payloads[wid]

    monkeypatch.setattr(hevy_client, "fetch_workout", _fetch)
    filled = _run(bot_mod._hevy_backfill_shapes(env.row(), "key"))

    assert filled == 2 and sorted(fetched) == ["old-1", "old-2", "old-gone"]
    usage = env.db.hevy_routine_usage(7)
    assert [(r["routine_id"], r["sessions"]) for r in usage] == [("r1", 2)]
    # Nothing left to do — and the gone one is never asked for again.
    fetched.clear()
    assert _run(bot_mod._hevy_backfill_shapes(env.row(), "key")) == 0
    assert fetched == []


def test_shape_backfill_stops_on_transient_failure(events_env, monkeypatch):
    env = events_env
    with env.db._conn() as c:
        c.execute("INSERT INTO hevy_imported (user_id, workout_id, imported_at)"
                  " VALUES (7, 'old-1', '2026-01-01T00:00:00+00:00')")

    def _boom(*_a, **_k):
        raise hevy_client.HevyError("timeout")

    monkeypatch.setattr(hevy_client, "fetch_workout", _boom)
    assert _run(bot_mod._hevy_backfill_shapes(env.row(), "key")) == 0
    # Still pending: the next poll retries rather than losing it.
    assert env.db.hevy_unshaped_workouts(7) == ["old-1"]


def test_release_note_posts_exactly_once(events_env, monkeypatch):
    from unittest.mock import AsyncMock

    env = events_env
    sent: list = []
    channel = SimpleNamespace(send=AsyncMock(side_effect=lambda **kw: sent.append(kw)))
    monkeypatch.setattr(bot_mod, "HEVY_FEED_CHANNEL_ID", 999, raising=False)
    monkeypatch.setattr(bot_mod.bot, "get_channel", lambda cid: channel)

    _run(bot_mod._announce_release_notes())
    _run(bot_mod._announce_release_notes())   # crash-loop / double on_ready

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert "TL;DR" in (embed.description or "")
    assert len(embed.description) <= 4096


def test_release_note_is_dropped_without_a_channel(events_env, monkeypatch):
    """The claim is kept even when there is nowhere to post: a stale release
    note surfacing the day a feed channel is finally set is worse than none."""
    env = events_env
    monkeypatch.setattr(bot_mod, "HEVY_FEED_CHANNEL_ID", None, raising=False)
    _run(bot_mod._announce_release_notes())
    assert env.db.app_meta_claim(bot_mod._RELEASE_NOTE_KEY) is False


# ---------------------------------------------------------------------------
# /hevy routine detail embed + the clearer exercise lines
# ---------------------------------------------------------------------------

def test_exercise_line_spells_out_differing_sets():
    """The user's real workout: Butterfly logged 100kg then 93kg, and the line
    said only "top 100kg×6" — a reader couldn't tell a planned drop from a
    typo. Now both sets show; uniform ones still collapse."""
    summary = hevy_client.summarize_workout({
        "id": "w", "title": "Arms", "start_time": "2026-08-19T13:16:09+00:00",
        "exercises": [
            {"title": "Butterfly (Pec Deck)",
             "sets": [{"weight_kg": 100, "reps": 6}, {"weight_kg": 93, "reps": 6}]},
            {"title": "Chest Supported T Bar Row",
             "sets": [{"weight_kg": 30, "reps": 6}, {"weight_kg": 30, "reps": 6}]},
        ],
    })
    embed = bot_mod._hevy_workout_embed("Poshy", summary)
    body = embed.fields[0].value
    assert "100kg×6, 93kg×6" in body
    assert "2×30kg×6" in body
    assert "top " not in body


def test_routine_embed_renders_targets_and_supersets():
    detail = hevy_client.summarize_routine({
        "id": "r-1", "title": "Arms",
        "exercises": [
            {"title": "Butterfly (Pec Deck)", "notes": "slow negatives",
             "sets": [{"weight_kg": 100, "reps": 6}, {"weight_kg": 93, "reps": 6}]},
            {"title": "Plank", "superset_id": 0, "sets": [{"duration_seconds": 60}]},
            {"title": "Push Up", "superset_id": 0,
             "sets": [{"reps": 15}, {"reps": 12}]},
        ],
    })
    embed = bot_mod._hevy_routine_embed(detail, "Push Pull")
    assert embed.title == "📋 Arms"
    assert "Push Pull" in embed.author.name and "3 exercises" in embed.author.name
    body = embed.description
    assert "**Butterfly (Pec Deck)** · 100kg×6, 93kg×6" in body
    assert "slow negatives" in body
    assert body.count("🔗") == 2            # both superset partners marked
    assert "15 reps, 12 reps" in body       # rep-only targets still shown
    assert "target sets" in embed.footer.text
