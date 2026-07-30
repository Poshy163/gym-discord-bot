"""Tests for the Home Assistant helpers that live inside app.bot.

The client-side mappers are covered in tests/test_ha.py; these cover the part
that touches the database — import de-duplication, the Home-Assistant-restart
replay guard, weight validation, and the embed text.

Ids are hand-picked and unique to this module (guild 771000, users 771001+)
because every test module importing app.bot shares one in-memory Database with
no per-test reset.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-used")

from app import bot as bot_mod  # noqa: E402
from app import ha_client  # noqa: E402
from app.bot import (  # noqa: E402
    _ha_delta_str,
    _ha_import_reading,
    _ha_metric_lines,
    _ha_resolve_target,
    _ha_states_for,
    _ha_sync_account,
    _ha_valid_weight,
    _ha_weighin_embed,
    db as _bot_db,
)

GUILD = 771000
_next_user = iter(range(771001, 771999))


def _user() -> int:
    """A fresh user id, so no test can see another's rows."""
    return next(_next_user)


def _state(entity_id: str, state: str, unit: str = "",
           last_changed: str = "2026-07-30T03:16:06+00:00") -> dict:
    attrs: dict[str, object] = {}
    if unit:
        attrs["unit_of_measurement"] = unit
    return {
        "entity_id": entity_id, "state": state, "attributes": attrs,
        "last_changed": last_changed, "last_updated": last_changed,
    }


def _at(day: int, hour: int = 6) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Weight validation
# ---------------------------------------------------------------------------

def test_ha_valid_weight_rejects_impossible_readings():
    """db.set_bodyweight does no range checking, so this path must.

    6553.5 is a real BLE failure mode — an unscaled 16-bit register — and one
    stored row of it poisons every leaderboard's true-weight line.
    """
    assert _ha_valid_weight(106.3) is True
    assert _ha_valid_weight(0) is False
    assert _ha_valid_weight(-5) is False
    assert _ha_valid_weight(6553.5) is False


# ---------------------------------------------------------------------------
# Import + de-duplication
# ---------------------------------------------------------------------------

def test_import_writes_bodyweight_and_metrics():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    result = _ha_import_reading(uid, GUILD, 106.3, _at(30), {
        "body_fat_pct": {"value": 35.2, "unit": "%"},
        "muscle_mass_kg": {"value": 65.48, "unit": "kg"},
    })
    assert result is not None
    assert result["weight_kg"] == 106.3
    assert result["metrics_written"] == 2
    # It lands in `bodyweights`, which is what keeps TDEE, protein targets,
    # goals and the graphs in sync rather than siloing scale data.
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 106.3
    assert _bot_db.latest_body_metrics(uid)["body_fat_pct"]["value"] == 35.2


def test_import_is_idempotent_for_the_same_reading():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(uid, GUILD, 106.3, _at(30)) is not None
    # Same last_changed → same key → already imported. This is what makes an
    # /ha_sync overlapping the poll loop safe.
    assert _ha_import_reading(uid, GUILD, 106.3, _at(30)) is None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 1


def test_import_rejects_an_implausible_weight_without_claiming_it():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(uid, GUILD, 6553.5, _at(30)) is None
    assert _bot_db.get_latest_bodyweight(GUILD, uid) is None
    # The key was never claimed, so a corrected reading at the same instant
    # still imports.
    assert _ha_import_reading(uid, GUILD, 106.3, _at(30)) is not None


def test_import_needs_a_timestamp():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(uid, GUILD, 106.3, None) is None


def test_replay_guard_blocks_a_restored_state():
    """Restarting Home Assistant re-creates every entity with a FRESH
    last_changed and an unchanged value. Without the guard that reads as a brand
    new weigh-in, so every HA update logged and announced a duplicate.
    """
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(uid, GUILD, 106.3, _at(30)) is not None
    # Same weight, new timestamp — i.e. after a restart.
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(31), replay_guard_kg=106.3,
    ) is None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 1


def test_replay_guard_still_allows_a_real_change():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    _ha_import_reading(uid, GUILD, 106.3, _at(30))
    assert _ha_import_reading(
        uid, GUILD, 106.1, _at(31), replay_guard_kg=106.3,
    ) is not None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 2


def test_replay_guard_allows_a_return_to_an_earlier_weight():
    """The guard compares only against the immediately preceding reading, so a
    genuine 106.3 → 106.5 → 106.3 sequence is three weigh-ins, not two."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(uid, GUILD, 106.3, _at(28)) is not None
    assert _ha_import_reading(
        uid, GUILD, 106.5, _at(29), replay_guard_kg=106.3) is not None
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(30), replay_guard_kg=106.5) is not None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 3


def test_import_records_the_guard_value_on_the_account():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    _ha_import_reading(uid, GUILD, 106.3, _at(30))
    assert _bot_db.ha_get(uid)["last_weight_kg"] == 106.3


def test_import_backdates_the_weigh_in():
    """A backfilled reading must keep Home Assistant's timestamp, or the whole
    imported history collapses onto today."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    _ha_import_reading(uid, GUILD, 108.0, _at(20))
    _ha_import_reading(uid, GUILD, 106.3, _at(30), replay_guard_kg=108.0)
    rows = _bot_db.bodyweight_history(GUILD, uid)
    assert [r["weight_kg"] for r in rows] == [108.0, 106.3]
    assert rows[0]["recorded_at"].startswith("2026-07-20")


def test_import_honours_a_supplied_measurement_key():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(30), key="id:abc123") is not None
    # Same id, different timestamp — i.e. after a Home Assistant restart. The id
    # is what makes this a no-op without needing the weight-comparison guard.
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(31), key="id:abc123") is None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 1


def test_two_weigh_ins_at_the_same_weight_both_import_with_ids():
    """The case the weight-comparison guard gets wrong and measurement ids get
    right: stepping on the scale twice an hour apart and reading the same kg is
    two real weigh-ins. So the guard is not applied on the id path.
    """
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "j")
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(30, 6), key="id:first") is not None
    assert _ha_import_reading(
        uid, GUILD, 106.3, _at(30, 7), key="id:second") is not None
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 2


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

def test_states_for_resolves_by_prefix():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "joshua_s",
                    weight_entity="sensor.joshua_s_weight")
    states = [
        _state("sensor.joshua_s_weight", "106.3", "kg"),
        _state("sensor.joshua_s_body_fat_percentage", "35.2", "%"),
        _state("sensor.sam_weight", "72.0", "kg"),
    ]
    mine = _ha_states_for(_bot_db.ha_get(uid), states)
    assert set(mine) == {"weight", "body_fat_pct"}
    assert mine["weight"]["entity_id"] == "sensor.joshua_s_weight"


def test_states_for_falls_back_to_the_stored_weight_entity():
    """A hand-renamed weight sensor won't share the prefix; the linked entity id
    is the escape hatch so those members aren't locked out."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "joshua_s",
                    weight_entity="sensor.bathroom_scale_kg")
    states = [
        _state("sensor.bathroom_scale_kg", "106.3", "kg"),
        _state("sensor.joshua_s_body_fat_percentage", "35.2", "%"),
    ]
    mine = _ha_states_for(_bot_db.ha_get(uid), states)
    assert mine["weight"]["entity_id"] == "sensor.bathroom_scale_kg"
    assert "body_fat_pct" in mine


def test_states_for_returns_nothing_for_an_unknown_prefix():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "nobody")
    mine = _ha_states_for(
        _bot_db.ha_get(uid), [_state("sensor.joshua_s_weight", "106.3", "kg")],
    )
    assert mine == {}


# ---------------------------------------------------------------------------
# Link resolution
# ---------------------------------------------------------------------------
# The states below are the shape of a real install: one person owns two weight
# sensors, and only one of them works.

def _two_source_states() -> list[dict]:
    apple = [
        # An Apple Health / Google Fit bridge creates these and then never
        # writes to them, so they sit at `unavailable` forever.
        _state("sensor.joshua_s_iphone_weight", "unavailable", "kg"),
        _state("sensor.joshua_s_iphone_body_fat_percentage", "unavailable", "%"),
        _state("sensor.joshua_s_iphone_lean_body_mass", "unavailable", "kg"),
    ]
    scale = [
        _state(f"sensor.renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s_{suffix}",
               value, unit)
        for suffix, value, unit in (
            ("weight", "106.7", "kg"),
            ("body_fat_percentage", "35.4", "%"),
            ("muscle_mass", "65.51", "kg"),
            ("body_mass_index", "28.1", ""),
        )
    ]
    return apple + scale


def test_resolve_prefers_a_live_sensor_over_a_tighter_name_match():
    """"joshua" prefix-matches the dead iPhone bridge but only appears *inside*
    the real scale's machine-generated prefix. Ranking the tighter match first
    linked the bridge, which then silently never synced — the worst possible
    outcome, because it looks like success.
    """
    found = _ha_resolve_target("joshua", _two_source_states())
    assert found is not None
    assert found["prefix"] == "renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s"
    assert found["reading"] is not None


@pytest.mark.parametrize("typed", [
    "joshua", "Joshua", "joshua_s", "renpho", "RENPHO",
    "sensor.renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s_weight",
])
def test_resolve_finds_the_scale_however_it_is_typed(typed):
    found = _ha_resolve_target(typed, _two_source_states())
    assert found is not None
    assert found["prefix"] == "renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s"


def test_resolve_honours_an_exact_prefix_even_without_a_reading():
    """Exactness is the one way to ask for a bucket that isn't reading — which is
    also what a brand-new scale looks like before anyone stands on it."""
    found = _ha_resolve_target("joshua_s_iphone", _two_source_states())
    assert found is not None
    assert found["prefix"] == "joshua_s_iphone"
    assert found["reading"] is None


def test_resolve_honours_an_explicit_entity_id():
    found = _ha_resolve_target(
        "sensor.joshua_s_iphone_weight", _two_source_states())
    assert found is not None and found["prefix"] == "joshua_s_iphone"


def test_resolve_matches_on_the_friendly_name():
    states = [
        {**_state("sensor.scale_a1b2_person_1_weight", "80.0", "kg"),
         "attributes": {"unit_of_measurement": "kg",
                        "friendly_name": "Sam's Weight"}},
    ]
    found = _ha_resolve_target("sam", states)
    assert found is not None and found["prefix"] == "scale_a1b2_person_1"


def test_resolve_returns_none_for_an_unknown_name():
    assert _ha_resolve_target("nobody", _two_source_states()) is None
    assert _ha_resolve_target("", _two_source_states()) is None
    assert _ha_resolve_target("light.kitchen", _two_source_states()) is None


def test_resolve_reports_the_metric_count_and_entity():
    found = _ha_resolve_target("renpho", _two_source_states())
    assert len(found["metrics"]) == 4
    assert found["weight_entity"] == (
        "sensor.renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s_weight")


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delta,days,expected", [
    (-0.4, 3, "▼ 0.40 kg in 3 days"),
    (0.3, 1, "▲ 0.30 kg since yesterday"),
    (-1.25, 0, "▼ 1.25 kg since earlier today"),
    (0.0, 5, "no change in 5 days"),
    (0.002, 5, "no change in 5 days"),
    (-0.4, None, "▼ 0.40 kg"),
    (None, 3, ""),
])
def test_ha_delta_str(delta, days, expected):
    assert _ha_delta_str(delta, days) == expected


def test_metric_lines_follow_registry_order_not_dict_order():
    """Two members' embeds must list metrics identically even when their scales
    report in different orders."""
    metrics = {
        "bmi": {"value": 36.8, "unit": ""},
        "body_fat_pct": {"value": 35.2, "unit": "%"},
        "weight": {"value": 106.3, "unit": "kg"},
    }
    lines = _ha_metric_lines(metrics)
    # Weight is excluded (it is the embed's headline), and body fat precedes BMI
    # because that is the registry order.
    assert len(lines) == 2
    assert "Body fat" in lines[0] and "35.2%" in lines[0]
    assert "BMI" in lines[1] and "36.8" in lines[1]


def test_metric_lines_empty():
    assert _ha_metric_lines({}) == []
    assert _ha_metric_lines({"weight": {"value": 80.0, "unit": "kg"}}) == []


def test_weighin_embed_shape():
    reading = {
        "weight_kg": 106.3,
        "measured_at": datetime(2026, 7, 30, 3, 16, 6, tzinfo=timezone.utc),
        "entity_id": "sensor.joshua_s_weight",
        "delta_kg": -0.4,
        "days_since": 3,
        "metrics": {"body_fat_pct": {"value": 35.2, "unit": "%"}},
    }
    embed = _ha_weighin_embed("Joshua", reading, protein_grams=213)
    assert "Joshua" in embed.title
    assert "106.30 kg" in embed.description
    assert "▼ 0.40 kg in 3 days" in embed.description
    names = [f.name for f in embed.fields]
    assert "Body composition" in names
    assert "Protein target" in names
    assert "sensor.joshua_s_weight" in embed.footer.text
    assert embed.timestamp == reading["measured_at"]


def test_weighin_embed_without_a_previous_weight_or_metrics():
    reading = {
        "weight_kg": 80.0,
        "measured_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "entity_id": "sensor.x_weight",
        "delta_kg": None, "days_since": None, "metrics": {},
    }
    embed = _ha_weighin_embed("Sam", reading, protein_grams=None)
    assert embed.description == "**80.00 kg**" or "80.00 kg" in embed.description
    assert [f.name for f in embed.fields] == []


def test_weighin_embed_escapes_a_hostile_display_name():
    reading = {
        "weight_kg": 80.0,
        "measured_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "entity_id": "sensor.x_weight",
        "delta_kg": None, "days_since": None, "metrics": {},
    }
    embed = _ha_weighin_embed("@everyone **bold**", reading, None)
    assert "@everyone" not in embed.title


# ---------------------------------------------------------------------------
# End-to-end shape: states → reading → import
# ---------------------------------------------------------------------------

def test_full_path_from_states_to_a_stored_weigh_in():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "joshua_s",
                    weight_entity="sensor.joshua_s_weight")
    states = [
        _state("sensor.joshua_s_weight", "106.30", "kg"),
        _state("sensor.joshua_s_body_fat_percentage", "35.2", "%"),
        _state("sensor.joshua_s_muscle_mass", "65.48", "kg"),
        _state("light.kitchen", "on"),
    ]
    mine = _ha_states_for(_bot_db.ha_get(uid), states)
    reading = ha_client.build_reading(mine)
    assert reading is not None
    result = _ha_import_reading(
        uid, GUILD, reading["weight_kg"], reading["measured_at"],
        reading["metrics"],
    )
    assert result is not None and result["metrics_written"] == 2
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 106.3


def test_sleeping_scale_never_reaches_the_database():
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "joshua_s")
    states = [_state("sensor.joshua_s_weight", "unavailable", "kg")]
    mine = _ha_states_for(_bot_db.ha_get(uid), states)
    assert ha_client.build_reading(mine) is None
    assert _bot_db.get_latest_bodyweight(GUILD, uid) is None


def test_history_backfill_then_live_reading_ordering():
    """Weigh-ins must be written oldest-first, because every set_bodyweight
    re-derives a bodyweight-linked protein target from the weight it is handed —
    so the newest reading has to land last or the ceiling comes from last week.
    """
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "joshua_s")
    history = [(_at(20), 108.0), (_at(24), 107.2), (_at(28), 106.5)]
    live = (_at(30), 106.3)
    pending = [*history, live]
    guard = None
    for when, kg in pending:
        _ha_import_reading(uid, GUILD, kg, when, None, replay_guard_kg=guard)
        guard = kg
    rows = _bot_db.bodyweight_history(GUILD, uid)
    assert [r["weight_kg"] for r in rows] == [108.0, 107.2, 106.5, 106.3]
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 106.3


# ---------------------------------------------------------------------------
# _ha_sync_account: the whole per-member cycle
# ---------------------------------------------------------------------------

def _scale_state(entries: list[dict], *, current: str,
                 prefix: str = "scale_joshua_s") -> list[dict]:
    """A weight entity publishing its own measurement log, plus one sibling."""
    return [
        {
            "entity_id": f"sensor.{prefix}_weight",
            "state": current,
            "attributes": {
                "unit_of_measurement": "kg",
                "friendly_name": "Joshua's Weight",
                "weight_history": entries,
            },
            "last_changed": "2026-07-30T03:50:49+00:00",
            "last_updated": "2026-07-30T03:50:49+00:00",
        },
        _state(f"sensor.{prefix}_body_fat_percentage", "35.4", "%"),
    ]


def _entry(minutes_ago: int, weight: float, mid: str) -> dict:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"measurement_id": mid, "timestamp": when.isoformat(),
            "weight": weight, "weight_unit": "kg", "body_fat": 35.0}


def _sync(uid: int, states: list[dict], monkeypatch, **kwargs):
    """Run _ha_sync_account with the Discord side stubbed. Returns (result, sends)."""
    channel = AsyncMock()
    monkeypatch.setattr(bot_mod, "_ha_alert_channel", AsyncMock(
        return_value=channel))
    monkeypatch.setattr(bot_mod.bot, "get_guild", lambda gid: None)
    row = _bot_db.ha_get(uid)
    result = asyncio.run(_ha_sync_account(row, states, **kwargs))
    embeds = [c.kwargs.get("embed") for c in channel.send.call_args_list]
    return result, [e for e in embeds if e is not None]


def test_sync_first_link_imports_history_as_one_summary(monkeypatch):
    uid = _user()
    entries = [_entry(40, 106.3, "m1"), _entry(20, 106.4, "m2"),
               _entry(5, 106.7, "m3")]
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    result, embeds = _sync(uid, _scale_state(entries, current="106.7"),
                           monkeypatch)
    assert result["new"] == 3
    assert result["backfill"] is True
    assert result["latest_kg"] == 106.7
    # One summary, not three alerts.
    assert len(embeds) == 1
    assert "linked their scale" in embeds[0].title
    assert [r["weight_kg"] for r in _bot_db.bodyweight_history(GUILD, uid)] == [
        106.3, 106.4, 106.7]
    # The newest weigh-in gets the full live metric set, older ones only what the
    # history entry carried.
    assert _bot_db.latest_body_metrics(uid)["body_fat_pct"]["value"] == 35.4


def test_sync_is_a_no_op_when_nothing_changed(monkeypatch):
    uid = _user()
    entries = [_entry(5, 106.7, "m1")]
    states = _scale_state(entries, current="106.7")
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    _sync(uid, states, monkeypatch)
    result, embeds = _sync(uid, states, monkeypatch)
    assert result["new"] == 0
    assert embeds == []


def test_sync_routine_double_weigh_in_posts_one_embed_each(monkeypatch):
    """Two readings between polls is not a link event. Announcing them with the
    backfill summary told the member they had just linked their scale.
    """
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    first = [_entry(60, 106.3, "m1")]
    _sync(uid, _scale_state(first, current="106.3"), monkeypatch)

    later = first + [_entry(10, 106.5, "m2"), _entry(2, 106.8, "m3")]
    result, embeds = _sync(uid, _scale_state(later, current="106.8"),
                           monkeypatch)
    assert result["new"] == 2
    assert result["backfill"] is False
    assert len(embeds) == 2
    assert all("weighed in" in e.title for e in embeds)
    assert not any("linked their scale" in e.title for e in embeds)
    # Each is diffed against the one before it, not against the same old weight.
    assert "0.20 kg" in embeds[0].description
    assert "0.30 kg" in embeds[1].description


def test_sync_respects_the_per_member_alert_opt_out(monkeypatch):
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    _bot_db.ha_set_alerts(uid, False)
    result, embeds = _sync(
        uid, _scale_state([_entry(5, 106.7, "m1")], current="106.7"), monkeypatch)
    # Still imported — opting out silences the announcement, it doesn't stop the
    # weigh-in counting towards bodyweight history and targets.
    assert result["new"] == 1
    assert embeds == []
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 106.7


def test_sync_ignores_entries_older_than_the_backfill_window(monkeypatch):
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    old = _entry(60 * 24 * 400, 120.0, "ancient")   # ~400 days ago
    recent = _entry(5, 106.7, "m1")
    result, _ = _sync(uid, _scale_state([old, recent], current="106.7"),
                      monkeypatch)
    assert result["new"] == 1
    assert [r["weight_kg"] for r in _bot_db.bodyweight_history(GUILD, uid)] == [
        106.7]


def test_sync_always_keeps_the_newest_entry_even_at_zero_backfill(monkeypatch):
    """HA_BACKFILL_DAYS=0 must mean "from now on", not "never import anything"."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 0)
    entries = [_entry(120, 106.0, "old"), _entry(5, 106.7, "new")]
    result, _ = _sync(uid, _scale_state(entries, current="106.7"), monkeypatch)
    assert result["new"] == 1
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 106.7


def test_sync_handles_a_prefix_that_resolves_to_nothing(monkeypatch):
    """A renamed or not-yet-created entity must be a quiet no-op, not a crash."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "nobody_at_all")
    result, embeds = _sync(
        uid, _scale_state([_entry(5, 106.7, "m1")], current="106.7"), monkeypatch)
    assert result["new"] == 0
    assert embeds == []
    assert _bot_db.ha_get(uid)["last_synced_at"] is not None


def test_sync_ignores_an_unavailable_scale(monkeypatch):
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "scale_joshua_s")
    states = _scale_state([], current="unavailable")
    result, embeds = _sync(uid, states, monkeypatch)
    assert result["new"] == 0 and embeds == []


def test_sync_falls_back_to_last_changed_without_a_history_attribute(monkeypatch):
    """A scale that publishes no measurement log still works, keyed on the
    sensor's last_changed."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "plain_joshua")
    states = [
        _state("sensor.plain_joshua_weight", "99.5", "kg",
               last_changed="2026-07-30T06:00:00+00:00"),
        _state("sensor.plain_joshua_body_fat_percentage", "30.0", "%",
               last_changed="2026-07-30T06:00:00+00:00"),
    ]
    # No backfill, so no recorder call and no executor is needed.
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 0)
    result, embeds = _sync(uid, states, monkeypatch)
    assert result["new"] == 1
    assert len(embeds) == 1
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == 99.5
    assert _bot_db.ha_get(uid)["last_weight_kg"] == 99.5
    # The guard value is recorded so a Home Assistant restart is recognised.
    restarted = [
        _state("sensor.plain_joshua_weight", "99.5", "kg",
               last_changed="2026-08-01T09:00:00+00:00"),
    ]
    result2, embeds2 = _sync(uid, restarted, monkeypatch)
    assert result2["new"] == 0
    assert embeds2 == []


# ---------------------------------------------------------------------------
# /ha_entities: discovery listing and its privacy rule
# ---------------------------------------------------------------------------

def _run_entities(caller_id: int, states: list[dict], monkeypatch):
    """Invoke /ha_entities as `caller_id` and return the embed it sent."""
    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error",
                        AsyncMock(return_value=states))
    interaction = AsyncMock()
    interaction.user.id = caller_id
    asyncio.run(bot_mod.ha_entities_cmd.callback(interaction))
    return interaction.followup.send.call_args.kwargs["embed"]


def test_entities_shows_nobody_elses_weight(monkeypatch):
    """A Home Assistant server is a household, so it carries sensors for people
    who are not in the Discord at all and therefore have no /ha_alerts opt-out to
    reach for. Only the caller's own bucket shows a number; the rest show *when*
    they last read, which is what you actually identify yours by.
    """
    mine_uid, other_uid = _user(), _user()
    _bot_db.ha_link(other_uid, GUILD, "housemate")
    states = [
        _state("sensor.housemate_weight", "72.10", "kg"),
        _state("sensor.unclaimed_weight", "80.50", "kg"),
    ]
    embed = _run_entities(mine_uid, states, monkeypatch)
    values = [f.value for f in embed.fields]
    housemate = next(v for v in values if "housemate" in v)
    assert "linked to another member" in housemate
    assert "72.10" not in housemate
    unclaimed = next(v for v in values if "unclaimed" in v)
    assert "80.50" not in unclaimed
    assert "last read" in unclaimed


def test_entities_shows_your_own_weight_and_marks_it_yours(monkeypatch):
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "mine_own")
    embed = _run_entities(
        uid, [_state("sensor.mine_own_weight", "99.90", "kg")], monkeypatch)
    value = embed.fields[0].value
    assert "yours" in value and "99.90" in value


def test_entities_collects_dead_buckets_into_one_line(monkeypatch):
    """A phantom Apple Health group given the same full-size field as a working
    scale is what makes the list confusing and the wrong choice tempting."""
    embed = _run_entities(_user(), [
        _state("sensor.dead_bridge_weight", "unavailable", "kg"),
        _state("sensor.real_scale_weight", "80.0", "kg"),
    ], monkeypatch)
    names = [f.name for f in embed.fields]
    assert "Nothing writing to these" in names
    dead = next(f.value for f in embed.fields if f.name == "Nothing writing to these")
    assert "dead_bridge" in dead
    # ...and it points at the setting that removes them for good.
    assert "Ignore entities containing" in dead
    # The working scale still gets a field of its own.
    assert any("real_scale" in f.value for f in embed.fields
               if f.name != "Nothing writing to these")


def test_entities_says_so_when_nothing_has_a_reading(monkeypatch):
    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error", AsyncMock(
        return_value=[_state("sensor.dead_weight", "unavailable", "kg")]))
    interaction = AsyncMock()
    interaction.user.id = _user()
    asyncio.run(bot_mod.ha_entities_cmd.callback(interaction))
    sent = interaction.followup.send.call_args
    assert sent.kwargs.get("embed") is None
    assert "none of them have a reading" in sent.args[0]


def test_ignored_entities_are_dropped_everywhere(monkeypatch):
    """The fragment filter is applied where states enter the feature, so the
    exclusion holds for the listing, for linking and for syncing alike."""
    states = [
        _state("sensor.joshua_s_iphone_weight", "unavailable", "kg"),
        _state("sensor.joshua_s_iphone_body_fat_percentage", "unavailable", "%"),
        _state("sensor.renpho_joshua_s_weight", "106.3", "kg"),
    ]
    monkeypatch.setattr(bot_mod, "HA_IGNORE_ENTITIES", {"_iphone"})
    kept = bot_mod._ha_visible_states(states)
    assert [s["entity_id"] for s in kept] == ["sensor.renpho_joshua_s_weight"]
    # Linking can no longer resolve to the ignored group, even by exact prefix.
    assert bot_mod._ha_resolve_target("joshua_s_iphone", kept) is None
    assert bot_mod._ha_resolve_target("joshua", kept)["prefix"] == (
        "renpho_joshua_s")
    # Matching is case-insensitive and substring-based, not whole-id.
    monkeypatch.setattr(bot_mod, "HA_IGNORE_ENTITIES", {"IPHONE".lower()})
    assert len(bot_mod._ha_visible_states(states)) == 1
    # Unset means no filtering at all -- same list object back, no copying.
    monkeypatch.setattr(bot_mod, "HA_IGNORE_ENTITIES", set())
    assert bot_mod._ha_visible_states(states) is states


def test_entities_reports_an_empty_server_without_an_embed(monkeypatch):
    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error",
                        AsyncMock(return_value=[_state("light.kitchen", "on")]))
    interaction = AsyncMock()
    interaction.user.id = _user()
    asyncio.run(bot_mod.ha_entities_cmd.callback(interaction))
    sent = interaction.followup.send.call_args
    assert sent.kwargs.get("embed") is None
    assert "couldn't see any body-composition sensors" in sent.args[0]


def test_link_refuses_a_prefix_another_member_already_owns(monkeypatch):
    """Without this the admin gate protects nothing: a member reads someone
    else's prefix off /ha_entities, links themselves to it, and that person's
    weigh-ins import and announce under the wrong name — defeating their
    /ha_alerts opt-out and corrupting both people's weight history.
    """
    owner, thief = _user(), _user()
    states = [_state("sensor.victim_weight", "72.0", "kg")]
    _bot_db.ha_link(owner, GUILD, "victim")

    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error",
                        AsyncMock(return_value=states))
    monkeypatch.setattr(bot_mod, "_ctx_guild_id", lambda i: GUILD)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", set())

    interaction = AsyncMock()
    interaction.user.id = thief
    asyncio.run(bot_mod.ha_link_cmd.callback(interaction, "victim"))
    message = interaction.followup.send.call_args.args[0]
    assert "already linked to another member" in message
    # The victim keeps their link and the thief gets none.
    assert _bot_db.ha_get(owner)["entity_prefix"] == "victim"
    assert _bot_db.ha_get(thief) is None


def test_relinking_your_own_prefix_still_works(monkeypatch):
    uid = _user()
    states = [_state("sensor.mine_weight", "72.0", "kg")]
    _bot_db.ha_link(uid, GUILD, "mine")
    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error",
                        AsyncMock(return_value=states))
    monkeypatch.setattr(bot_mod, "_ctx_guild_id", lambda i: GUILD)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", set())
    interaction = AsyncMock()
    interaction.user.id = uid
    asyncio.run(bot_mod.ha_link_cmd.callback(interaction, "mine"))
    assert "linked" in interaction.followup.send.call_args.args[0]
    assert _bot_db.ha_get(uid)["entity_prefix"] == "mine"


def test_admin_can_reassign_a_prefix_and_the_old_link_goes(monkeypatch):
    """Two rows pointing at one scale would import every weigh-in twice, so a
    reassignment has to remove the previous owner's link."""
    old, new, admin = _user(), _user(), _user()
    states = [_state("sensor.shared_weight", "72.0", "kg")]
    _bot_db.ha_link(old, GUILD, "shared")

    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "_ha_states_or_error",
                        AsyncMock(return_value=states))
    monkeypatch.setattr(bot_mod, "_ctx_guild_id", lambda i: GUILD)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", {admin})

    interaction = AsyncMock()
    interaction.user.id = admin
    member = AsyncMock()
    member.id = new
    asyncio.run(bot_mod.ha_link_cmd.callback(interaction, "shared", member))
    assert _bot_db.ha_get(new)["entity_prefix"] == "shared"
    assert _bot_db.ha_get(old) is None


def test_non_admin_cannot_link_on_someone_elses_behalf(monkeypatch):
    caller, victim = _user(), _user()
    monkeypatch.setattr(bot_mod, "_ha_enabled", lambda: True)
    monkeypatch.setattr(bot_mod, "ADMIN_USER_IDS", set())
    interaction = AsyncMock()
    interaction.user.id = caller
    member = AsyncMock()
    member.id = victim
    asyncio.run(bot_mod.ha_link_cmd.callback(interaction, "anything", member))
    # Denied before any network call or write.
    assert interaction.response.send_message.await_count == 1
    assert _bot_db.ha_get(victim) is None


# ---------------------------------------------------------------------------
# Regressions found by the adversarial review pass
# ---------------------------------------------------------------------------

def test_switching_to_the_measurement_id_source_does_not_re_announce(monkeypatch):
    """A scale whose integration gains a measurement log changes de-duplication
    key scheme mid-life. The same physical weigh-ins then have different keys, so
    without a guard every one inside the window re-imports and re-announces.
    """
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "switcher")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 0)

    # Phase 1: no history attribute, so keyed on last_changed.
    plain = [_state("sensor.switcher_weight", "100.0", "kg",
                    last_changed="2026-07-30T06:00:00+00:00")]
    result, embeds = _sync(uid, plain, monkeypatch)
    assert result["new"] == 1 and len(embeds) == 1

    # Phase 2: the integration update lands and publishes that same weigh-in --
    # plus an older one -- under `id:` keys.
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)
    entries = [
        {"measurement_id": "old", "timestamp": "2026-07-29T06:00:00+00:00",
         "weight": 99.0, "weight_unit": "kg"},
        {"measurement_id": "same", "timestamp": "2026-07-30T06:00:00+00:00",
         "weight": 100.0, "weight_unit": "kg"},
    ]
    result2, embeds2 = _sync(
        uid, _scale_state(entries, current="100.0", prefix="switcher"),
        monkeypatch)
    assert result2["new"] == 0, "already-imported weigh-ins must not re-import"
    assert embeds2 == []
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 1


def test_a_newer_live_reading_is_not_merged_into_an_older_history_entry(
        monkeypatch):
    """An integration that writes the sensor before appending to its log leaves a
    live reading that is a *newer* measurement. Merging its metrics into the
    newest log entry files this weigh-in's body composition against the previous
    one — and drops the weigh-in itself."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "ahead")
    entries = [_entry(30, 106.3, "logged")]
    states = _scale_state(entries, current="106.9", prefix="ahead")
    # Make the live state unambiguously newer than the logged entry.
    states[0]["last_changed"] = datetime.now(timezone.utc).isoformat()
    states[0]["last_updated"] = states[0]["last_changed"]
    result, _ = _sync(uid, states, monkeypatch)
    assert result["new"] == 2
    weights = [r["weight_kg"] for r in _bot_db.bodyweight_history(GUILD, uid)]
    assert weights == [106.3, 106.9]


def test_the_replay_guard_never_drops_a_recorder_history_row(monkeypatch):
    """The guard holds the NEWEST imported weight and history rows are older, so
    applying it to them silently drops a real past weigh-in that happened to
    equal the current one."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "recorded")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)

    async def _history(cfg, entity_id, days, unit=""):
        return [
            (datetime.now(timezone.utc) - timedelta(days=3), 106.5),
            (datetime.now(timezone.utc) - timedelta(days=2), 107.0),
        ]

    monkeypatch.setattr(bot_mod, "_ha_fetch_history", _history)
    states = [_state("sensor.recorded_weight", "106.5", "kg",
                     last_changed=datetime.now(timezone.utc).isoformat())]
    result, _ = _sync(uid, states, monkeypatch)
    # All three: two from history plus the live reading, even though the live
    # weight equals the oldest history row.
    assert result["new"] == 3
    assert [r["weight_kg"] for r in _bot_db.bodyweight_history(GUILD, uid)] == [
        106.5, 107.0, 106.5]


def test_a_failed_history_fetch_does_not_burn_the_backfill(monkeypatch):
    """_ha_fetch_history returning None means the recorder call failed, which must
    not be recorded as "history already imported" — otherwise one timeout costs
    the member their entire history import."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "flaky")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)

    async def _fails(cfg, entity_id, days, unit=""):
        return None

    monkeypatch.setattr(bot_mod, "_ha_fetch_history", _fails)
    states = [_state("sensor.flaky_weight", "90.0", "kg",
                     last_changed=datetime.now(timezone.utc).isoformat())]
    result, _ = _sync(uid, states, monkeypatch)
    # The live reading still imported...
    assert result["new"] == 1
    # ...but the one-time history import is still owed.
    assert _bot_db.ha_get(uid)["backfilled_at"] is None
    assert result["backfill"] is False


def test_a_successful_empty_history_does_mark_the_backfill(monkeypatch):
    """The counterpart: an empty result is a real answer, so don't retry forever."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "empty_hist")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)

    async def _empty(cfg, entity_id, days, unit=""):
        return []

    monkeypatch.setattr(bot_mod, "_ha_fetch_history", _empty)
    states = [_state("sensor.empty_hist_weight", "90.0", "kg",
                     last_changed=datetime.now(timezone.utc).isoformat())]
    _sync(uid, states, monkeypatch)
    assert _bot_db.ha_get(uid)["backfilled_at"] is not None


def test_manual_sync_does_not_announce_a_link(monkeypatch):
    """/ha_sync forces a history fetch. Telling a member who ran it — in public —
    that they had just linked their scale is wrong."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "manual")
    first = [_entry(120, 100.0, "m1")]
    _sync(uid, _scale_state(first, current="100.0", prefix="manual"), monkeypatch)

    later = first + [_entry(20, 101.0, "m2"), _entry(5, 102.0, "m3")]
    result, embeds = _sync(
        uid, _scale_state(later, current="102.0", prefix="manual"),
        monkeypatch, force_backfill=True)
    assert result["new"] == 2
    assert result["backfill"] is True     # history WAS fetched...
    assert len(embeds) == 2               # ...but announced as weigh-ins
    assert not any("linked their scale" in e.title for e in embeds)


def test_unlink_then_relink_does_not_duplicate_weigh_ins(monkeypatch):
    """Unlink/relink is what a member tries when sync looks stuck. It must not
    re-import weigh-ins that are still inside the backfill window."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "stable")
    entries = [_entry(60, 100.0, "m1"), _entry(30, 100.5, "m2")]
    states = _scale_state(entries, current="100.5", prefix="stable")
    _sync(uid, states, monkeypatch)
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 2

    _bot_db.ha_unlink(uid)
    _bot_db.ha_link(uid, GUILD, "stable")
    result, embeds = _sync(uid, states, monkeypatch)
    assert result["new"] == 0
    assert embeds == []
    assert len(_bot_db.bodyweight_history(GUILD, uid)) == 2


def test_the_bare_prefix_bucket_does_not_match_every_query():
    """A single-person install has entities like `sensor.weight`, whose
    person-prefix is "". Since `"joshua".startswith("")` is True, that bucket
    matched every query at the tightest tier and outranked the real scale."""
    states = [
        _state("sensor.weight", "70.0", "kg"),
        _state("sensor.body_fat_percentage", "20.0", "%"),
        _state("sensor.renpho_a1_joshua_s_weight", "106.3", "kg"),
        _state("sensor.renpho_a1_joshua_s_body_fat_percentage", "35.4", "%"),
    ]
    found = _ha_resolve_target("joshua", states)
    assert found is not None and found["prefix"] == "renpho_a1_joshua_s"
    # The bare bucket is still reachable deliberately, by entity id.
    bare = _ha_resolve_target("sensor.weight", states)
    assert bare is not None and bare["prefix"] == ""


def test_history_backfill_uses_the_live_entitys_unit(monkeypatch):
    """History rows are fetched with no_attributes, so they carry no unit. Looking
    it up separately meant a failed lookup fell back to "assume kg", silently
    storing 234 lb as 234 kg — under the implausible-weight cap, so undetected."""
    uid = _user()
    _bot_db.ha_link(uid, GUILD, "imperial")
    monkeypatch.setattr(bot_mod, "HA_BACKFILL_DAYS", 14)
    seen: dict[str, str] = {}

    async def _history(cfg, entity_id, days, unit=""):
        seen["unit"] = unit
        return [(datetime.now(timezone.utc) - timedelta(days=2), 230.0)]

    monkeypatch.setattr(bot_mod, "_ha_fetch_history", _history)
    states = [_state("sensor.imperial_weight", "234.4", "lb",
                     last_changed=datetime.now(timezone.utc).isoformat())]
    _sync(uid, states, monkeypatch)
    # The unit is handed down from the live state rather than re-fetched.
    assert seen["unit"] == "lb"
    # And the live reading itself converted.
    assert _bot_db.get_latest_bodyweight(GUILD, uid)["weight_kg"] == pytest.approx(
        106.32, abs=0.01)
