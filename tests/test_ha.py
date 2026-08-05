"""Tests for the Home Assistant integration.

Three layers, all network-free: the pure entity/metric mappers, the HTTP error
translation (with ``requests`` monkeypatched), and the DB link/dedupe helpers.

The mapper tests deliberately use the exact entity ids and values a Renpho BLE
scale produces, because the whole prefix scheme rests on HA's slugifier turning
"Joshua's Weight" into ``sensor.joshua_s_weight`` — a suffix table that works on
tidied-up names but not on real ones would pass a prettier test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import ha_client
from app.db import Database


def _state(entity_id: str, state: str, unit: str = "",
           last_changed: str = "2026-07-30T03:16:06.204838+00:00",
           **attrs: object) -> dict:
    """One /api/states row, in HA's real shape."""
    attributes: dict[str, object] = dict(attrs)
    if unit:
        attributes["unit_of_measurement"] = unit
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_changed": last_changed,
        "last_updated": last_changed,
        "last_reported": last_changed,
        "context": {"id": "01J", "parent_id": None, "user_id": None},
    }


#: The screenshot's scale, verbatim.
RENPHO_STATES = [
    _state("sensor.joshua_s_weight", "106.30", "kg",
           friendly_name="Joshua's Weight", device_class="weight"),
    _state("sensor.joshua_s_body_fat_percentage", "35.2", "%"),
    _state("sensor.joshua_s_body_mass_index", "36.8"),
    _state("sensor.joshua_s_basal_metabolic_rate", "1838", "kcal"),
    _state("sensor.joshua_s_body_water_percentage", "46.8", "%"),
    _state("sensor.joshua_s_bone_mass", "3.40", "kg"),
    _state("sensor.joshua_s_fat_free_mass", "68.88", "kg"),
    _state("sensor.joshua_s_muscle_mass", "65.48", "kg"),
    _state("sensor.joshua_s_protein_percentage", "14.8", "%"),
    _state("sensor.joshua_s_skeletal_muscle_percentage", "41.7", "%"),
    # Noise that must be ignored.
    _state("light.kitchen", "on"),
    _state("sensor.outside_temperature", "18.4", "°C"),
    _state("sensor.washing_machine_power", "0", "W"),
]


# ---------------------------------------------------------------------------
# Entity classification
# ---------------------------------------------------------------------------

def test_classify_entity_splits_metric_from_person():
    assert ha_client.classify_entity("sensor.joshua_s_weight") == (
        "weight", "joshua_s")
    assert ha_client.classify_entity("sensor.joshua_s_body_fat_percentage") == (
        "body_fat_pct", "joshua_s")
    assert ha_client.classify_entity("sensor.joshua_s_basal_metabolic_rate") == (
        "bmr_kcal", "joshua_s")


def test_classify_entity_prefers_the_longest_suffix():
    """"skeletal_muscle_mass" must not be eaten by the "muscle_mass" suffix.

    Shortest-match would return ("muscle_mass_kg", "joshua_s_skeletal") — a
    person who does not exist, whose entities would never be found again.
    """
    assert ha_client.classify_entity("sensor.joshua_s_skeletal_muscle_mass") == (
        "skeletal_muscle_kg", "joshua_s")
    assert ha_client.classify_entity(
        "sensor.joshua_s_skeletal_muscle_percentage") == (
        "skeletal_muscle_pct", "joshua_s")
    assert ha_client.classify_entity("sensor.joshua_s_muscle_mass") == (
        "muscle_mass_kg", "joshua_s")


def test_classify_entity_allows_a_bare_single_person_install():
    assert ha_client.classify_entity("sensor.weight") == ("weight", "")
    assert ha_client.classify_entity("sensor.body_fat_percentage") == (
        "body_fat_pct", "")


def test_classify_entity_rejects_non_body_entities():
    for eid in ("light.kitchen", "sensor.outside_temperature",
                "sensor.washing_machine_power", "weight", "", "sensor.",
                "binary_sensor.joshua_s_weight"):
        assert ha_client.classify_entity(eid) is None, eid


def test_group_body_entities_buckets_by_person_and_drops_noise():
    grouped = ha_client.group_body_entities(RENPHO_STATES)
    assert set(grouped) == {"joshua_s"}
    assert len(grouped["joshua_s"]) == 10
    assert grouped["joshua_s"]["weight"]["state"] == "106.30"


def test_group_body_entities_separates_two_people():
    states = RENPHO_STATES + [
        _state("sensor.sam_weight", "72.10", "kg"),
        _state("sensor.sam_body_fat_percentage", "18.0", "%"),
    ]
    grouped = ha_client.group_body_entities(states)
    assert set(grouped) == {"joshua_s", "sam"}
    assert len(grouped["sam"]) == 2


def test_group_body_entities_newest_wins_on_a_duplicate_metric():
    """A duplicated integration gives two weight entities for one person."""
    states = [
        _state("sensor.joshua_s_weight", "100.0", "kg",
               last_changed="2026-07-01T00:00:00+00:00"),
        _state("sensor.joshua_s_weight_2", "106.3", "kg",
               last_changed="2026-07-30T00:00:00+00:00"),
    ]
    # "_weight_2" is not a known suffix, so only the first classifies; the point
    # of the test is that a real duplicate (same id shape) keeps the newest.
    dupes = [
        _state("sensor.joshua_s_weight", "100.0", "kg",
               last_changed="2026-07-01T00:00:00+00:00"),
        _state("sensor.joshua_s_weight", "106.3", "kg",
               last_changed="2026-07-30T00:00:00+00:00"),
    ]
    assert ha_client.group_body_entities(states)["joshua_s"]["weight"][
        "state"] == "100.0"
    assert ha_client.group_body_entities(dupes)["joshua_s"]["weight"][
        "state"] == "106.3"


def test_entities_for_prefix_is_case_insensitive():
    assert len(ha_client.entities_for_prefix(RENPHO_STATES, "JOSHUA_S")) == 10
    assert ha_client.entities_for_prefix(RENPHO_STATES, "nobody") == {}


def test_entity_id_for_rebuilds_canonical_ids():
    assert ha_client.entity_id_for("joshua_s", "weight") == (
        "sensor.joshua_s_weight")
    assert ha_client.entity_id_for("", "body_fat_pct") == (
        "sensor.body_fat_percentage")
    assert ha_client.entity_id_for("x", "not_a_metric") == ""


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def test_build_reading_normalises_the_whole_scale():
    mine = ha_client.entities_for_prefix(RENPHO_STATES, "joshua_s")
    reading = ha_client.build_reading(mine)
    assert reading is not None
    assert reading["weight_kg"] == 106.3
    assert reading["entity_id"] == "sensor.joshua_s_weight"
    assert reading["friendly_name"] == "Joshua's Weight"
    assert reading["key"] == "2026-07-30T03:16:06+00:00"
    m = reading["metrics"]
    # Weight is present in metrics too (the embed skips it); the nine others are
    # the ones that need storing.
    assert m["body_fat_pct"]["value"] == 35.2
    assert m["body_fat_pct"]["unit"] == "%"
    assert m["muscle_mass_kg"]["value"] == 65.48
    assert m["bmr_kcal"]["value"] == 1838.0
    # BMI has no unit_of_measurement at all — the registry's unit fills in.
    assert m["bmi"]["value"] == 36.8
    assert m["bmi"]["unit"] == ""


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "none", "", "n/a"])
def test_build_reading_returns_none_for_a_sleeping_scale(bad):
    """A scale reports `unavailable` most of the time. Importing that as a
    weigh-in would be the single most damaging bug in this integration."""
    assert ha_client.build_reading(
        {"weight": _state("sensor.joshua_s_weight", bad, "kg")}
    ) is None


def test_build_reading_returns_none_without_a_weight():
    mine = ha_client.entities_for_prefix(RENPHO_STATES, "joshua_s")
    del mine["weight"]
    assert ha_client.build_reading(mine) is None
    assert ha_client.build_reading({}) is None


def test_build_reading_rejects_non_positive_weight():
    for value in ("0", "-4.2"):
        assert ha_client.build_reading(
            {"weight": _state("sensor.x_weight", value, "kg")}
        ) is None


def test_build_reading_skips_unreadable_sibling_metrics():
    """One `unavailable` sibling must not lose the whole weigh-in."""
    reading = ha_client.build_reading({
        "weight": _state("sensor.x_weight", "80.0", "kg"),
        "body_fat_pct": _state("sensor.x_body_fat_percentage", "unavailable", "%"),
        "bmi": _state("sensor.x_body_mass_index", "24.1"),
    })
    assert reading is not None
    assert "body_fat_pct" not in reading["metrics"]
    assert reading["metrics"]["bmi"]["value"] == 24.1


def test_build_reading_converts_imperial_units():
    reading = ha_client.build_reading({
        "weight": _state("sensor.x_weight", "234.4", "lb"),
        "muscle_mass_kg": _state("sensor.x_muscle_mass", "144.4", "lb"),
    })
    assert reading is not None
    assert reading["weight_kg"] == pytest.approx(106.32, abs=0.01)
    # Mass metrics are stored in kg with the unit rewritten, not left in lb.
    assert reading["metrics"]["muscle_mass_kg"]["value"] == pytest.approx(
        65.5, abs=0.1)
    assert reading["metrics"]["muscle_mass_kg"]["unit"] == "kg"


def test_mass_to_kg_units():
    assert ha_client.mass_to_kg(100, "kg") == 100
    assert ha_client.mass_to_kg(220.462, "lb") == pytest.approx(100, abs=0.01)
    assert ha_client.mass_to_kg(3400, "g") == 3.4
    assert ha_client.mass_to_kg(15, "st") == pytest.approx(95.25, abs=0.01)
    # An absent unit is assumed kg (HA's metric default) rather than rejected.
    assert ha_client.mass_to_kg(80, "") == 80
    assert ha_client.mass_to_kg(80, None) == 80
    assert ha_client.mass_to_kg(80, "furlongs") == 80


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

def test_reading_key_is_stable_to_the_second():
    a = ha_client.parse_ha_time("2026-07-30T03:16:06.204838+00:00")
    b = ha_client.parse_ha_time("2026-07-30T03:16:06.999999+00:00")
    assert ha_client.reading_key(a) == ha_client.reading_key(b)
    assert ha_client.reading_key(None) == ""


def test_reading_key_is_timezone_normalised():
    """The same instant in two representations is one weigh-in.

    Not decorative: HA always sends UTC, but a naive value (from a proxy that
    reformats, or a test) must not create a second key for the same moment.
    """
    utc = ha_client.parse_ha_time("2026-07-30T03:16:06+00:00")
    offset = ha_client.parse_ha_time("2026-07-30T12:46:06+09:30")
    assert ha_client.reading_key(utc) == ha_client.reading_key(offset)


def test_parse_ha_time_handles_both_microsecond_forms():
    """HA drops `.000000`, so both forms arrive in practice."""
    assert ha_client.parse_ha_time("2026-07-30T03:16:06+00:00") is not None
    assert ha_client.parse_ha_time("2026-07-30T03:16:06.204838+00:00") is not None
    assert ha_client.parse_ha_time("2026-07-30T03:16:06Z") is not None
    naive = ha_client.parse_ha_time("2026-07-30T03:16:06")
    assert naive is not None and naive.tzinfo is timezone.utc
    assert ha_client.parse_ha_time("not a time") is None
    assert ha_client.parse_ha_time(None) is None


def test_repeat_of_the_same_weight_is_the_same_reading():
    """HA only bumps last_changed when the value changes, so an identical repeat
    weigh-in yields the same key and is correctly not announced twice."""
    first = ha_client.build_reading(ha_client.entities_for_prefix(
        RENPHO_STATES, "joshua_s"))
    again = ha_client.build_reading(ha_client.entities_for_prefix(
        RENPHO_STATES, "joshua_s"))
    assert first["key"] == again["key"]


# ---------------------------------------------------------------------------
# Summaries and formatting
# ---------------------------------------------------------------------------

def test_summarize_reading_computes_delta_and_days():
    reading = {
        "weight_kg": 106.3,
        "measured_at": datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc),
    }
    out = ha_client.summarize_reading(reading, {
        "weight_kg": 106.7,
        "recorded_at": "2026-07-27T03:00:00+00:00",
    })
    assert out["delta_kg"] == -0.4
    assert out["days_since"] == 3


def test_summarize_reading_without_a_previous_weight():
    reading = {"weight_kg": 80.0,
               "measured_at": datetime(2026, 7, 30, tzinfo=timezone.utc)}
    for previous in (None, {}, {"weight_kg": None}):
        out = ha_client.summarize_reading(reading, previous)
        assert out["delta_kg"] is None and out["days_since"] is None


def test_format_metric_uses_registry_precision():
    assert ha_client.format_metric("body_fat_pct", 35.24) == "35.2%"
    assert ha_client.format_metric("muscle_mass_kg", 65.481) == "65.48 kg"
    assert ha_client.format_metric("bmr_kcal", 1838.4) == "1838 kcal"
    assert ha_client.format_metric("bmi", 36.83) == "36.8"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def test_weight_history_collapses_repeats_and_drops_sentinels():
    series = [
        _state("sensor.x_weight", "107.0", "kg",
               last_changed="2026-07-20T06:00:00+00:00"),
        {"state": "107.0", "last_changed": "2026-07-21T06:00:00+00:00"},
        {"state": "unavailable", "last_changed": "2026-07-21T12:00:00+00:00"},
        {"state": "106.5", "last_changed": "2026-07-22T06:00:00+00:00"},
        {"state": "106.3", "last_changed": "2026-07-23T06:00:00+00:00"},
    ]
    rows = ha_client.weight_history_from_states(series, unit="kg")
    assert [kg for _, kg in rows] == [107.0, 106.5, 106.3]
    assert [dt.day for dt, _ in rows] == [20, 22, 23]


def test_weight_history_uses_the_supplied_unit():
    """History rows are fetched with no_attributes, so they carry no unit."""
    series = [{"state": "234.4", "last_changed": "2026-07-20T06:00:00+00:00"}]
    rows = ha_client.weight_history_from_states(series, unit="lb")
    assert rows[0][1] == pytest.approx(106.32, abs=0.01)


def test_weight_history_handles_empty_and_junk():
    assert ha_client.weight_history_from_states([]) == []
    assert ha_client.weight_history_from_states([
        {"state": "unknown", "last_changed": "2026-07-20T06:00:00+00:00"},
        {"state": "12.0"},                      # no timestamp
        {"state": "0", "last_changed": "2026-07-20T07:00:00+00:00"},
        "not a dict",
    ]) == []


def test_history_for_entity_never_trusts_position():
    """HA drops empty entities from the outer list, so position 0 may be the
    wrong entity. Matching on the id is the only safe read."""
    wanted = [
        _state("sensor.sam_weight", "72.0", "kg"),
        {"state": "71.5", "last_changed": "2026-07-21T06:00:00+00:00"},
    ]
    other = [_state("sensor.joshua_s_weight", "106.3", "kg")]
    rows = ha_client.history_for_entity([other, wanted], "sensor.sam_weight")
    assert rows[0]["entity_id"] == "sensor.sam_weight"
    assert len(rows) == 2


def test_history_for_entity_falls_back_for_a_single_series():
    rows = [{"state": "80.0", "last_changed": "2026-07-21T06:00:00+00:00"}]
    assert ha_client.history_for_entity([rows], "sensor.x_weight") == rows
    assert ha_client.history_for_entity([], "sensor.x_weight") == []
    assert ha_client.history_for_entity([[]], "sensor.x_weight") == []


# ---------------------------------------------------------------------------
# The scale's own history attribute (the preferred source)
# ---------------------------------------------------------------------------
# Shape taken verbatim from a live Renpho Fitness Scale BLE integration.

WEIGHT_WITH_HISTORY = {
    "entity_id": "sensor.renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s_weight",
    "state": "106.7",
    "attributes": {
        "device_class": "weight",
        "friendly_name": "Renpho-Scale AA:BB:CC:DD:EE:FF Joshua's Weight",
        "state_class": "measurement",
        "unit_of_measurement": "kg",
        "weight_history": [
            {"measurement_id": "a1df05dcc1474350a1a842a2bed8a445",
             "timestamp": "2026-07-30T03:19:55.817623+00:00",
             "timestamp_display": "Jul 30, 2026 at 12:49:55 PM ACST",
             "weight": 106.3, "weight_unit": "kg",
             "resistance_1": 507, "resistance_2": 505, "body_fat": 35.2},
            {"measurement_id": "7e0465f4f92e4715b9b4a1222c872d00",
             "timestamp": "2026-07-30T03:28:09.706088+00:00",
             "weight": 106.4, "weight_unit": "kg", "body_fat": 35.3},
            {"measurement_id": "259fd928ca2e482d897ad0f0b1c2d3e4",
             "timestamp": "2026-07-30T03:28:33.458605+00:00",
             "weight": 106.7, "weight_unit": "kg", "body_fat": 35.4},
        ],
    },
    "last_changed": "2026-07-30T03:50:49.623982+00:00",
    "last_updated": "2026-07-30T03:50:49.623982+00:00",
}


def test_readings_from_history_attr_parses_the_renpho_shape():
    rows = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)
    assert rows is not None and len(rows) == 3
    # Oldest-first, because every set_bodyweight re-derives the protein target
    # from the weight it is handed.
    assert [r["weight_kg"] for r in rows] == [106.3, 106.4, 106.7]
    assert [r["measured_at"].minute for r in rows] == [19, 28, 28]
    # Keyed by the scale's own measurement id, namespaced so it can never
    # collide with a last_changed timestamp key.
    assert rows[0]["key"] == "id:a1df05dcc1474350a1a842a2bed8a445"
    assert rows[0]["measurement_id"] == "a1df05dcc1474350a1a842a2bed8a445"
    # The entry's own extra fields are mapped onto stored metrics.
    assert rows[0]["metrics"]["body_fat_pct"]["value"] == 35.2
    # Unknown fields (resistance) are ignored rather than stored as junk.
    assert set(rows[0]["metrics"]) == {"body_fat_pct"}


def test_readings_from_history_attr_is_immune_to_a_restart():
    """The whole point of preferring this source: the entity's last_changed moved
    to the restart time (03:50) while every measurement kept its own id and
    timestamp, so nothing re-imports."""
    rows = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)
    keys_before = [r["key"] for r in rows]
    restarted = {
        **WEIGHT_WITH_HISTORY,
        "last_changed": "2026-07-31T09:00:00+00:00",
        "last_updated": "2026-07-31T09:00:00+00:00",
    }
    assert [r["key"] for r in
            ha_client.readings_from_history_attr(restarted)] == keys_before


def test_readings_from_history_attr_returns_none_without_the_attribute():
    """None means "fall back to last_changed + the recorder", which is a
    different thing from "this scale has no past weigh-ins"."""
    assert ha_client.readings_from_history_attr(
        _state("sensor.x_weight", "80.0", "kg")) is None
    assert ha_client.readings_from_history_attr({}) is None
    assert ha_client.readings_from_history_attr(
        {"attributes": {"weight_history": "not a list"}}) is None
    # An empty list is the attribute being present and empty — still the
    # preferred source, just with nothing in it yet.
    assert ha_client.readings_from_history_attr(
        {"attributes": {"weight_history": []}}) == []


def test_readings_from_history_attr_drops_unusable_entries():
    state = {"entity_id": "sensor.x_weight", "attributes": {
        "unit_of_measurement": "kg",
        "weight_history": [
            {"measurement_id": "a", "timestamp": "2026-07-20T06:00:00+00:00",
             "weight": 80.0},
            {"measurement_id": "b", "weight": 81.0},            # no timestamp
            {"measurement_id": "c", "timestamp": "2026-07-21T06:00:00+00:00"},
            {"measurement_id": "d", "timestamp": "nonsense", "weight": 82.0},
            {"measurement_id": "e", "timestamp": "2026-07-22T06:00:00+00:00",
             "weight": 0},                                       # non-positive
            "not a dict",
        ],
    }}
    rows = ha_client.readings_from_history_attr(state)
    assert [r["key"] for r in rows] == ["id:a"]


def test_readings_from_history_attr_falls_back_to_a_timestamp_key():
    """A partially-populated history (no ids) must still import."""
    state = {"entity_id": "sensor.x_weight", "attributes": {
        "unit_of_measurement": "kg",
        "weight_history": [
            {"timestamp": "2026-07-20T06:00:00+00:00", "weight": 80.0},
        ],
    }}
    rows = ha_client.readings_from_history_attr(state)
    assert rows[0]["key"] == "2026-07-20T06:00:00+00:00"
    assert rows[0]["measurement_id"] == ""


def test_readings_from_history_attr_converts_units():
    state = {"entity_id": "sensor.x_weight", "attributes": {
        "weight_history": [
            {"measurement_id": "a", "timestamp": "2026-07-20T06:00:00+00:00",
             "weight": 234.4, "weight_unit": "lb", "muscle_mass": 144.4,
             "body_fat": 35.2},
        ],
    }}
    rows = ha_client.readings_from_history_attr(state)
    assert rows[0]["weight_kg"] == pytest.approx(106.32, abs=0.01)
    # A mass metric inside the entry is converted too; a percentage is not.
    assert rows[0]["metrics"]["muscle_mass_kg"]["value"] == pytest.approx(
        65.5, abs=0.1)
    assert rows[0]["metrics"]["muscle_mass_kg"]["unit"] == "kg"
    assert rows[0]["metrics"]["body_fat_pct"]["value"] == 35.2


def test_readings_from_history_attr_accepts_alternative_attribute_names():
    state = {"entity_id": "sensor.x_weight", "attributes": {
        "measurements": [
            {"id": "m1", "time": "2026-07-20T06:00:00+00:00", "value": 80.0,
             "unit": "kg"},
        ],
    }}
    rows = ha_client.readings_from_history_attr(state)
    assert rows[0]["key"] == "id:m1" and rows[0]["weight_kg"] == 80.0


def test_measurement_key_is_namespaced():
    assert ha_client.measurement_key("abc") == "id:abc"
    assert ha_client.measurement_key("") == ""
    # Cannot collide with a timestamp key, so a member can move between the two
    # schemes without re-importing or skipping anything.
    assert not ha_client.reading_key(
        ha_client.parse_ha_time("2026-07-30T03:16:06+00:00"),
    ).startswith("id:")


def test_merge_live_metrics_fills_a_sparse_history_entry():
    """The newest history entry and the live sensors are the same weigh-in, but
    Renpho logs only body fat in the entry and all ten as sensors."""
    rows = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)
    newest = rows[-1]
    assert set(newest["metrics"]) == {"body_fat_pct"}
    live = ha_client.build_reading(
        ha_client.entities_for_prefix(RENPHO_STATES, "joshua_s"))
    merged = ha_client.merge_live_metrics(newest, live["metrics"])
    assert len(merged["metrics"]) == 9
    # Live wins on conflict — it is what HA is displaying now.
    assert merged["metrics"]["body_fat_pct"]["value"] == 35.2
    # Weight is never smuggled in as a metric.
    assert "weight" not in merged["metrics"]
    # The original is untouched.
    assert set(newest["metrics"]) == {"body_fat_pct"}
    # Identity fields survive the merge.
    assert merged["key"] == newest["key"]
    assert merged["measured_at"] == newest["measured_at"]


def test_merge_live_metrics_with_nothing_live():
    rows = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)
    assert ha_client.merge_live_metrics(rows[-1], {})["metrics"] == (
        rows[-1]["metrics"])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("http://homeassistant.local:8123", "http://homeassistant.local:8123"),
    ("http://homeassistant.local:8123/", "http://homeassistant.local:8123"),
    ("homeassistant.local:8123", "http://homeassistant.local:8123"),
    ("http://ha:8123/api", "http://ha:8123"),
    ("http://ha:8123/api/", "http://ha:8123"),
    ("https://ha.example.com", "https://ha.example.com"),
    ("  http://ha:8123  ", "http://ha:8123"),
    ("", ""),
])
def test_normalize_base_url(raw, expected):
    assert ha_client.normalize_base_url(raw) == expected


def test_config_for_normalises_and_reports_configured():
    assert not ha_client.config_for("", "").configured
    assert not ha_client.config_for("ha:8123", "").configured
    assert not ha_client.config_for("", "t").configured
    cfg = ha_client.config_for("ha:8123/", "  tok  ")
    assert cfg.configured
    assert cfg.base_url == "http://ha:8123"
    assert cfg.token == "tok"
    assert cfg.verify_ssl is True
    assert ha_client.config_for(
        "ha:8123", "t", verify_ssl=False).verify_ssl is False


# ---------------------------------------------------------------------------
# HTTP error translation
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, payload: object = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Minimal stand-in for the ``requests`` module.

    The exception hierarchy mirrors the real one, including ``SSLError``
    subclassing ``ConnectionError`` — that relationship is the whole reason
    ha_client has to catch SSLError first, so a stub that flattened it would let
    the bug back in unnoticed.
    """

    class RequestException(Exception):
        pass

    class ConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    class SSLError(ConnectionError):
        pass

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None, verify=None):
        self.calls.append({
            "url": url, "headers": headers or {}, "params": params or {},
            "timeout": timeout, "verify": verify,
        })
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeExceptions:
    """Stands in for ``requests.exceptions``, which ha_client references by path."""

    SSLError = _FakeRequests.SSLError
    ConnectionError = _FakeRequests.ConnectionError
    Timeout = _FakeRequests.Timeout
    RequestException = _FakeRequests.RequestException


_FakeRequests.exceptions = _FakeExceptions


@pytest.fixture()
def cfg():
    return ha_client.HAConfig(base_url="http://ha:8123", token="tok")


def _patch(monkeypatch, fake):
    monkeypatch.setattr(ha_client, "requests", fake)
    return fake


def test_get_sends_a_bearer_token_and_no_trailing_slash(monkeypatch, cfg):
    fake = _patch(monkeypatch, _FakeRequests(_FakeResponse(200, [])))
    ha_client.fetch_states(cfg)
    call = fake.calls[0]
    # Capital-B "Bearer" is required: HA compares the scheme case-sensitively.
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["url"] == "http://ha:8123/api/states"
    assert call["verify"] is True


def test_ping_hits_the_slash_terminated_api_route(monkeypatch, cfg):
    fake = _patch(monkeypatch, _FakeRequests(
        _FakeResponse(200, {"message": "API running."})))
    out = ha_client.ping(cfg)
    assert out["ok"] is True
    assert out["message"] == "API running."
    # /api/ is the one route where the trailing slash is mandatory.
    assert fake.calls[0]["url"] == "http://ha:8123/api/"


def test_401_is_an_auth_error(monkeypatch, cfg):
    _patch(monkeypatch, _FakeRequests(_FakeResponse(401, None, "unauthorized")))
    with pytest.raises(ha_client.HAAuthError):
        ha_client.fetch_states(cfg)


def test_403_is_an_ip_ban_not_an_auth_error(monkeypatch, cfg):
    """A 403 from HA is never a bad token — it is ip_bans.yaml. Retrying makes
    it worse, so it must not be handled as a credential problem."""
    _patch(monkeypatch, _FakeRequests(_FakeResponse(403, None, "forbidden")))
    with pytest.raises(ha_client.HABanned) as exc:
        ha_client.fetch_states(cfg)
    assert not isinstance(exc.value, ha_client.HAAuthError)
    assert "ip_bans" in str(exc.value)


def test_404_on_an_entity_does_not_try_to_decode_the_body(monkeypatch, cfg):
    # HA answers "Entity not found." as plain text; .json() would raise.
    _patch(monkeypatch, _FakeRequests(
        _FakeResponse(404, None, "Entity not found.")))
    with pytest.raises(ha_client.HANotFound):
        ha_client.fetch_state(cfg, "sensor.nope")


def test_mdns_resolution_failure_names_the_real_fix(monkeypatch):
    """`homeassistant.local` from inside Docker is the likeliest first failure,
    and a raw getaddrinfo error tells the operator nothing."""
    cfg = ha_client.HAConfig(
        base_url="http://homeassistant.local:8123", token="tok")
    fake = _FakeRequests(raises=_FakeRequests.ConnectionError(
        "[Errno -2] Name or service not known"))
    _patch(monkeypatch, fake)
    with pytest.raises(ha_client.HAUnreachable) as exc:
        ha_client.fetch_states(cfg)
    message = str(exc.value)
    assert "mDNS" in message and "LAN IP" in message


def test_connection_failure_to_an_ip_is_reported_plainly(monkeypatch):
    cfg = ha_client.HAConfig(base_url="http://192.168.1.50:8123", token="tok")
    _patch(monkeypatch, _FakeRequests(
        raises=_FakeRequests.ConnectionError("Connection refused")))
    with pytest.raises(ha_client.HAUnreachable) as exc:
        ha_client.fetch_states(cfg)
    assert "192.168.1.50:8123" in str(exc.value)


def test_timeout_is_unreachable(monkeypatch, cfg):
    _patch(monkeypatch, _FakeRequests(raises=_FakeRequests.Timeout("slow")))
    with pytest.raises(ha_client.HAUnreachable):
        ha_client.fetch_states(cfg)


def test_history_disables_significant_changes_explicitly(monkeypatch, cfg):
    """`significant_changes_only` defaults to TRUE in HA and is only disabled by
    an explicit "0" — a bare flag leaves the filtering on and silently drops
    weigh-ins."""
    fake = _patch(monkeypatch, _FakeRequests(_FakeResponse(200, [[]])))
    since = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    ha_client.fetch_history(cfg, ["sensor.x_weight"], since)
    call = fake.calls[0]
    assert call["params"]["significant_changes_only"] == "0"
    assert call["params"]["filter_entity_id"] == "sensor.x_weight"
    assert "minimal_response" in call["params"]
    assert "no_attributes" in call["params"]
    assert call["url"].endswith("/api/history/period/2026-07-16T00:00:00+00:00")


def test_history_with_no_entities_makes_no_request(monkeypatch, cfg):
    fake = _patch(monkeypatch, _FakeRequests(_FakeResponse(200, [])))
    assert ha_client.fetch_history(cfg, [], datetime.now(timezone.utc)) == []
    assert fake.calls == []


def test_unconfigured_client_raises_before_any_request(monkeypatch):
    fake = _patch(monkeypatch, _FakeRequests(_FakeResponse(200, [])))
    with pytest.raises(ha_client.HAError):
        ha_client.fetch_states(ha_client.HAConfig())
    assert fake.calls == []


def test_import_safe_without_requests(monkeypatch):
    """The bot must boot without the optional dep, and say so rather than
    raising AttributeError on None."""
    monkeypatch.setattr(ha_client, "requests", None)
    assert ha_client.available() is False
    with pytest.raises(ha_client.HAUnavailable):
        ha_client.fetch_states(
            ha_client.HAConfig(base_url="http://ha:8123", token="t"))
    # The pure mappers keep working — they are what the tests above rely on.
    assert ha_client.classify_entity("sensor.joshua_s_weight") is not None


def test_verify_ssl_is_passed_through(monkeypatch):
    fake = _patch(monkeypatch, _FakeRequests(_FakeResponse(200, [])))
    ha_client.fetch_states(ha_client.HAConfig(
        base_url="https://ha.example.com", token="t", verify_ssl=False))
    assert fake.calls[0]["verify"] is False


# ---------------------------------------------------------------------------
# Database: links, de-dupe and body metrics
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_ha_link_get_unlink(db):
    assert db.ha_get(1) is None
    db.ha_link(1, 42, "joshua_s", weight_entity="sensor.joshua_s_weight",
               friendly_name="Joshua's Weight")
    row = db.ha_get(1)
    assert int(row["guild_id"]) == 42
    assert row["entity_prefix"] == "joshua_s"
    assert row["weight_entity"] == "sensor.joshua_s_weight"
    assert row["alerts_enabled"] == 1
    assert row["last_synced_at"] is None
    assert row["backfilled_at"] is None
    assert len(db.list_ha_accounts()) == 1

    # Re-linking updates in place rather than duplicating the row.
    db.ha_link(1, 99, "JOSHUA", weight_entity="SENSOR.J_WEIGHT")
    row = db.ha_get(1)
    assert int(row["guild_id"]) == 99
    # Prefixes and entity ids are lower-cased on the way in, because HA ids are
    # lower-case and a mixed-case paste must still match.
    assert row["entity_prefix"] == "joshua"
    assert row["weight_entity"] == "sensor.j_weight"
    assert len(db.list_ha_accounts()) == 1

    assert db.ha_unlink(1) is True
    assert db.ha_unlink(1) is False
    assert db.ha_get(1) is None


def test_relink_keeps_sync_state_but_clears_the_backfill_marker(db):
    """Correcting a typo'd prefix must not re-announce a whole history, but the
    new prefix's history does need importing once."""
    db.ha_link(1, 42, "joshau_s")
    db.ha_mark_synced(1)
    db.ha_mark_backfilled(1)
    db.ha_link(1, 42, "joshua_s")
    row = db.ha_get(1)
    assert row["last_synced_at"] is not None
    assert row["backfilled_at"] is None


def test_ha_reading_dedupe(db):
    db.ha_link(1, 42, "joshua_s")
    key = "2026-07-30T03:16:06+00:00"
    assert db.ha_reading_imported(1, key) is False
    assert db.ha_mark_reading(1, key) is True
    # The second claim loses — this is the lock that stops two overlapping
    # syncs from double-posting one weigh-in.
    assert db.ha_mark_reading(1, key) is False
    assert db.ha_reading_imported(1, key) is True
    # Per-user isolation: two people can weigh in at the same instant.
    assert db.ha_reading_imported(2, key) is False
    assert db.ha_mark_reading(2, key) is True
    # An empty key is never claimable.
    assert db.ha_mark_reading(1, "") is False


def test_claiming_a_reading_does_not_touch_the_replay_guard(db):
    """The claim is taken *before* the write. Recording the guard here meant a
    failed write left it describing a weigh-in that does not exist, which then
    blocked its own retry."""
    db.ha_link(1, 42, "joshua_s")
    assert db.ha_mark_reading(1, "2026-07-30T03:16:06+00:00") is True
    row = db.ha_get(1)
    assert row["last_weight_kg"] is None
    assert row["last_reading_at"] is None


def test_ha_note_reading_records_the_guard_after_the_write(db):
    """The guard is what stops a Home Assistant restart — which re-stamps every
    restored sensor's last_changed — re-importing an unchanged weight."""
    db.ha_link(1, 42, "joshua_s")
    when = datetime(2026, 7, 30, 3, 16, 6, tzinfo=timezone.utc)
    db.ha_note_reading(1, 106.3, when)
    row = db.ha_get(1)
    assert row["last_weight_kg"] == 106.3
    # The MEASUREMENT time, not the de-duplication key: it is also what tells a
    # scale that starts publishing its own measurement log which entries were
    # already imported under the old timestamp-keyed scheme.
    assert row["last_reading_at"].startswith("2026-07-30T03:16:06")


def test_unlink_keeps_the_import_ledger_so_relinking_is_safe(db):
    """Unlink/relink is the obvious thing to try when sync looks stuck. Clearing
    the ledger made it re-import every weigh-in still inside the backfill window,
    and set_bodyweight always appends — so the member ended up with each of those
    recorded twice and announced again."""
    db.ha_link(1, 42, "joshua_s")
    db.ha_mark_reading(1, "k1")
    db.ha_unlink(1)
    assert db.ha_reading_imported(1, "k1") is True
    db.ha_link(1, 42, "joshua_s")
    assert db.ha_reading_imported(1, "k1") is True


def test_relinking_a_different_prefix_clears_the_replay_guard(db):
    """The guard describes the previous scale's last reading. Carried over, a
    stale weight silently swallows the new scale's first weigh-in."""
    db.ha_link(1, 42, "old_scale")
    db.ha_note_reading(1, 106.3, datetime(2026, 7, 30, tzinfo=timezone.utc))
    # Same prefix: the guard is still valid, so it survives.
    db.ha_link(1, 42, "old_scale")
    assert db.ha_get(1)["last_weight_kg"] == 106.3
    # Different prefix: it does not.
    db.ha_link(1, 42, "new_scale")
    row = db.ha_get(1)
    assert row["last_weight_kg"] is None
    assert row["last_reading_at"] is None


def test_ha_mark_synced_and_backfilled(db):
    db.ha_link(1, 42, "joshua_s")
    db.ha_mark_synced(1)
    db.ha_mark_backfilled(1)
    row = db.ha_get(1)
    assert row["last_synced_at"] is not None
    assert row["backfilled_at"] is not None


def _add_metrics_for_weighin(db, when, metrics, *, user_id=1, guild_id=42):
    """Store the parent weigh-in before its composition, like the HA importer."""
    db.set_bodyweight(guild_id, user_id, 100.0, recorded_at=when)
    return db.add_body_metrics(
        guild_id, user_id, metrics, recorded_at=when,
    )


def test_add_body_metrics_and_latest(db):
    when = datetime(2026, 7, 30, 3, 16, 6, tzinfo=timezone.utc)
    written = _add_metrics_for_weighin(db, when, {
        "body_fat_pct": (35.2, "%"),
        "muscle_mass_kg": (65.48, "kg"),
        "bmi": (36.8, ""),
    })
    assert written == 3
    latest = db.latest_body_metrics(1)
    assert set(latest) == {"body_fat_pct", "muscle_mass_kg", "bmi"}
    assert latest["body_fat_pct"]["value"] == 35.2
    assert latest["muscle_mass_kg"]["unit"] == "kg"
    assert latest["body_fat_pct"]["source"] == "home_assistant"


def test_add_body_metrics_is_idempotent_per_weigh_in(db):
    when = datetime(2026, 7, 30, 3, 16, 6, tzinfo=timezone.utc)
    payload = {"body_fat_pct": (35.2, "%")}
    assert _add_metrics_for_weighin(db, when, payload) == 1
    # Re-importing the same reading writes nothing, so a repeated sync is safe.
    assert db.add_body_metrics(42, 1, payload, recorded_at=when) == 0


def test_add_body_metrics_requires_its_parent_weigh_in(db):
    """A deleted HA weigh-in must not have its composition resurrected later."""
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    assert db.add_body_metrics(
        42, 1, {"body_fat_pct": (35.2, "%")}, recorded_at=when,
    ) == 0
    assert db.latest_body_metrics(1) == {}


def test_add_body_metrics_refuses_weight(db):
    """Weight belongs in `bodyweights` via set_bodyweight, which is where the
    protein link and the TDEE model hang off it."""
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    db.set_bodyweight(42, 1, 106.3, recorded_at=when)
    assert db.add_body_metrics(
        42, 1, {"weight": (106.3, "kg")}, recorded_at=when) == 0
    assert db.latest_body_metrics(1) == {}


def test_add_body_metrics_skips_unparseable_values(db):
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    written = _add_metrics_for_weighin(db, when, {
        "bmi": ("oops", ""),
        "body_fat_pct": (35.2, "%"),
    })
    assert written == 1
    assert set(db.latest_body_metrics(1)) == {"body_fat_pct"}


def test_add_body_metrics_skips_non_finite_values(db):
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    written = _add_metrics_for_weighin(db, when, {
        "bmi": (float("nan"), ""),
        "body_fat_pct": (35.2, "%"),
    })
    assert written == 1
    assert set(db.latest_body_metrics(1)) == {"body_fat_pct"}


def test_latest_body_metrics_picks_the_newest_per_metric(db):
    older = datetime(2026, 7, 20, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 30, tzinfo=timezone.utc)
    _add_metrics_for_weighin(
        db, older, {"body_fat_pct": (37.0, "%"), "bmi": (38.0, "")},
    )
    # Only body fat is re-measured; BMI must still resolve to the older row.
    _add_metrics_for_weighin(
        db, newer, {"body_fat_pct": (35.2, "%")},
    )
    latest = db.latest_body_metrics(1)
    assert latest["body_fat_pct"]["value"] == 35.2
    assert latest["bmi"]["value"] == 38.0


def test_body_metric_history_is_oldest_first(db):
    for day, value in ((20, 37.0), (25, 36.0), (30, 35.2)):
        _add_metrics_for_weighin(
            db,
            datetime(2026, 7, day, tzinfo=timezone.utc),
            {"body_fat_pct": (value, "%")},
        )
    rows = db.body_metric_history(1, "body_fat_pct")
    assert [r["value"] for r in rows] == [37.0, 36.0, 35.2]
    assert db.body_metric_history(1, "bmi") == []


def test_body_metrics_are_per_user(db):
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    _add_metrics_for_weighin(db, when, {"bmi": (36.8, "")})
    assert db.latest_body_metrics(2) == {}


def test_body_metric_history_limit_keeps_the_newest_rows(db):
    """Smart-scale history caps must trim the old end, not the current one."""
    for day in range(1, 11):
        _add_metrics_for_weighin(
            db,
            datetime(2026, 7, day, tzinfo=timezone.utc),
            {"body_fat_pct": (30.0 + day, "%")},
        )
    rows = db.body_metric_history(1, "body_fat_pct", limit=3)
    assert [r["value"] for r in rows] == [38.0, 39.0, 40.0]


def test_body_metric_histories_batches_metrics_with_independent_caps(db):
    for day in range(1, 5):
        _add_metrics_for_weighin(
            db,
            datetime(2026, 7, day, tzinfo=timezone.utc),
            {
                "body_fat_pct": (30.0 + day, "%"),
                "bmi": (20.0 + day, ""),
            },
        )
    histories = db.body_metric_histories(
        1, ("body_fat_pct", "bmi"), limit_per_metric=2,
    )
    assert [r["value"] for r in histories["body_fat_pct"]] == [33.0, 34.0]
    assert [r["value"] for r in histories["bmi"]] == [23.0, 24.0]


def test_body_metric_summaries_use_the_exact_requested_window(db):
    for day, value in ((1, 31.0), (2, 30.0), (3, 29.0), (5, 1.0)):
        _add_metrics_for_weighin(
            db,
            datetime(2026, 7, day, tzinfo=timezone.utc),
            {"body_fat_pct": (value, "%")},
        )
    rows = db.body_metric_summaries_between(
        1,
        ("body_fat_pct",),
        "2026-07-02T00:00:00+00:00",
        "2026-07-03T23:59:59+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["first_value"] == 30.0
    assert rows[0]["latest_value"] == 29.0
    assert rows[0]["samples"] == 2


def test_latest_body_metric_snapshot_does_not_mix_weigh_ins(db):
    older = datetime(2026, 7, 20, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 30, tzinfo=timezone.utc)
    _add_metrics_for_weighin(
        db, older, {"body_fat_pct": (37.0, "%"), "bmi": (38.0, "")},
    )
    _add_metrics_for_weighin(
        db, newer, {"body_fat_pct": (35.2, "%")},
    )
    snapshot = db.latest_body_metric_snapshot(1)
    assert snapshot is not None
    assert snapshot["recorded_at"].startswith("2026-07-30")
    assert set(snapshot["metrics"]) == {"body_fat_pct"}
    assert snapshot["weight_kg"] == 100.0


def test_web_delete_keeps_metrics_until_last_duplicate_parent_is_gone(db):
    when = datetime(2026, 7, 30, tzinfo=timezone.utc)
    _add_metrics_for_weighin(
        db, when, {"body_fat_pct": (35.2, "%")},
    )
    # The old schema permits two bodyweight rows for one measurement instant.
    db.set_bodyweight(42, 1, 100.0, recorded_at=when)
    ids = [r["id"] for r in db.bodyweight_history(42, 1)]
    assert len(ids) == 2

    assert db.web_delete_bodyweight(42, ids[0], "operator") is True
    assert db.latest_body_metrics(1)
    assert db.web_delete_bodyweight(42, ids[1], "operator") is True
    assert db.latest_body_metrics(1) == {}


def test_migration_removes_legacy_orphan_body_metrics(tmp_path):
    path = tmp_path / "ha-orphans.sqlite3"
    first = Database(path)
    when = "2026-07-30T00:00:00+00:00"
    # Reproduce a row written by the pre-fix metrics-only path after its parent
    # weigh-in had been deleted. The public writer now rejects this state.
    with first._conn() as c:
        c.execute(
            "INSERT INTO body_metrics "
            "(user_id, guild_id, metric, value, unit, source, recorded_at) "
            "VALUES (1, 42, 'body_fat_pct', 35.2, '%', "
            "'home_assistant', ?)",
            (when,),
        )
    first.close()

    migrated = Database(path)
    with migrated._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM body_metrics").fetchone()[0] == 0
    migrated.close()


def test_bodyweight_history_limit_keeps_the_newest_rows(db):
    """A scale writes far more rows than hand-logging, which is what exposed
    this: an ASC LIMIT truncated the newest end, so callers reading rows[-1] as
    "latest" reported a months-old weight as current."""
    for day in range(1, 11):
        db.set_bodyweight(
            42, 1, 100.0 + day,
            recorded_at=datetime(2026, 7, day, tzinfo=timezone.utc),
        )
    rows = db.bodyweight_history(42, 1, limit=3)
    # Newest three, still oldest-first.
    assert [r["weight_kg"] for r in rows] == [108.0, 109.0, 110.0]
    # And the last row really is the latest weigh-in.
    assert rows[-1]["weight_kg"] == db.get_latest_bodyweight(42, 1)["weight_kg"]
    # An unbounded read is unchanged: everything, oldest-first.
    assert len(db.bodyweight_history(42, 1, limit=1000)) == 10


def test_ha_release_reading_lets_a_failed_write_retry(db):
    """The claim is taken before the weigh-in is written. If the write then fails
    and the claim is kept, the next poll skips the weigh-in as already imported
    and it is lost for good."""
    db.ha_link(1, 42, "joshua_s")
    key = "id:abc"
    assert db.ha_mark_reading(1, key) is True
    assert db.ha_reading_imported(1, key) is True
    assert db.ha_release_reading(1, key) is True
    assert db.ha_reading_imported(1, key) is False
    # And the retry succeeds.
    assert db.ha_mark_reading(1, key) is True
    # Releasing something never claimed is a no-op, not an error.
    assert db.ha_release_reading(1, "nope") is False
    assert db.ha_release_reading(1, "") is False


# ---------------------------------------------------------------------------
# Credential-adjacent hardening
# ---------------------------------------------------------------------------

#: Structurally a JWT -- three base64url segments -- but not a real token.
#: The middle segment is deliberately longer than 63 characters, because that
#: is the DNS label limit and therefore the thing that makes HA_BASE_URL
#: reject an access token pasted into the URL field.
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJleGFtcGxlLW5vdC1hLXJlYWwtdG9rZW4iLCJpYXQiOjAsImV4cCI6MCwibm90ZSI6InRlc3QtZml4dHVyZSJ9."
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


@pytest.mark.parametrize("value,ok", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("n", False),
    # Anything unrecognised must fail CLOSED. Written as "is this in the truthy
    # list", every typo silently disabled TLS validation on token-bearing calls.
    ("enabled", True), ("strict", True), ("required", True), ("2", True),
    ("", True),
])
def test_verify_ssl_fails_closed(value, ok):
    assert ha_client.verify_ssl_from_env({"HA_VERIFY_SSL": value}) is ok


def test_unrecognised_verify_ssl_is_logged(caplog):
    with caplog.at_level("WARNING"):
        ha_client.verify_ssl_from_env({"HA_VERIFY_SSL": "enabled"})
    assert "keeping certificate verification ON" in caplog.text


def test_ssl_failure_is_not_reported_as_unreachable(monkeypatch):
    """requests.exceptions.SSLError subclasses ConnectionError, so without an
    explicit branch ahead of it a certificate problem is reported as "couldn't
    reach the host" and sends the operator to the URL field instead."""
    _patch(monkeypatch, _FakeRequests(
        raises=_FakeRequests.SSLError("certificate verify failed")))
    cfg = ha_client.HAConfig(base_url="https://ha.example.com", token="t")
    with pytest.raises(ha_client.HACertError) as exc:
        ha_client.fetch_states(cfg)
    assert not isinstance(exc.value, ha_client.HAUnreachable)
    assert "self-signed" in str(exc.value)


def test_unreachable_hint_strips_userinfo_from_the_host():
    """The hint quotes the host back at the operator, so a password pasted into
    the URL field must not survive into a Discord reply or a log line."""
    cfg = ha_client.HAConfig(
        base_url="http://admin:hunter2@ha.example.com:8123", token="t")
    message = ha_client._unreachable_hint(cfg, Exception("Connection refused"))
    assert "hunter2" not in message and "admin" not in message
    assert "ha.example.com:8123" in message


def test_group_prefers_a_readable_state_over_a_dead_duplicate():
    """Two entities can mean the same metric for one person (`_weight` and
    `_bodyweight`). Ordering on recency alone let a dead duplicate that changed
    state most recently shadow the live sensor and stop every import."""
    states = [
        _state("sensor.joshua_s_weight", "106.3", "kg",
               last_changed="2026-07-30T03:00:00+00:00"),
        # Newer, but carries no reading.
        _state("sensor.joshua_s_bodyweight", "unavailable", "kg",
               last_changed="2026-07-30T09:00:00+00:00"),
    ]
    weight = ha_client.group_body_entities(states)["joshua_s"]["weight"]
    assert weight["state"] == "106.3"
    # Between two readable states, recency still decides.
    both_live = [
        _state("sensor.joshua_s_weight", "106.3", "kg",
               last_changed="2026-07-30T03:00:00+00:00"),
        _state("sensor.joshua_s_bodyweight", "107.0", "kg",
               last_changed="2026-07-30T09:00:00+00:00"),
    ]
    assert ha_client.group_body_entities(both_live)["joshua_s"]["weight"][
        "state"] == "107.0"


def test_summarize_reading_reports_no_delta_against_a_newer_weight():
    """A backdated import can land behind a weight the member typed in since.
    Reporting a delta then states the change backwards, and with days_since
    clamped at 0 it reads as "since earlier today"."""
    reading = {
        "weight_kg": 106.3,
        "measured_at": datetime(2026, 7, 25, tzinfo=timezone.utc),
    }
    out = ha_client.summarize_reading(reading, {
        "weight_kg": 105.0,
        "recorded_at": "2026-07-30T00:00:00+00:00",   # newer than the reading
    })
    assert out["delta_kg"] is None
    assert out["days_since"] is None
    # The normal direction is unaffected.
    ok = ha_client.summarize_reading(reading, {
        "weight_kg": 107.0, "recorded_at": "2026-07-20T00:00:00+00:00",
    })
    assert ok["delta_kg"] == -0.7 and ok["days_since"] == 5


# ---------------------------------------------------------------------------
# Per-member credentials
# ---------------------------------------------------------------------------

def test_token_encryption_roundtrip(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("HA_FERNET_KEY", Fernet.generate_key().decode())
    sealed = ha_client.encrypt_token("long-lived-token")
    assert sealed != "long-lived-token"
    assert ha_client.decrypt_token(sealed) == "long-lived-token"
    assert ha_client.fernet_ready() is True


@pytest.mark.parametrize("name", [
    "HA_FERNET_KEY", "HEVY_FERNET_KEY", "STRAVA_FERNET_KEY", "REVO_FERNET_KEY",
])
def test_fernet_falls_back_through_every_other_integrations_key(monkeypatch, name):
    """One generated key has to serve all four clients, which is why the chain
    ends at REVO_FERNET_KEY — see app/secretbox.py:FERNET_KEYS."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv(name, Fernet.generate_key().decode())
    assert ha_client.fernet_ready() is True
    assert ha_client.decrypt_token(ha_client.encrypt_token("t")) == "t"


def test_fernet_not_ready_without_any_key(monkeypatch):
    for name in ("HA_FERNET_KEY", "HEVY_FERNET_KEY", "STRAVA_FERNET_KEY",
                 "REVO_FERNET_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert ha_client.fernet_ready() is False
    with pytest.raises(ha_client.HAUnavailable):
        ha_client.encrypt_token("t")


def test_a_token_sealed_with_another_key_is_reported_not_crashed(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("HA_FERNET_KEY", Fernet.generate_key().decode())
    sealed = ha_client.encrypt_token("t")
    monkeypatch.setenv("HA_FERNET_KEY", Fernet.generate_key().decode())
    with pytest.raises(ha_client.HAUnavailable) as exc:
        ha_client.decrypt_token(sealed)
    assert "/setup_ha" in str(exc.value)


# ---------------------------------------------------------------------------
# Re-reported weigh-ins (one measurement, two measurement ids)
# ---------------------------------------------------------------------------
# Taken from a real Renpho scale: it reports the weight the instant you step on,
# then re-reports the SAME measurement ~16s later once impedance is available,
# under a fresh measurement id. Id de-duplication cannot see that, and the pair
# announced itself twice.

REREPORTED = {
    "entity_id": "sensor.scale_joshua_s_weight",
    "state": "106.3",
    "attributes": {
        "unit_of_measurement": "kg",
        "weight_history": [
            {"measurement_id": "2d16547578", "weight": 107.3, "weight_unit": "kg",
             "timestamp": "2026-07-30T04:45:19.725299+00:00", "body_fat": 22.2},
            {"measurement_id": "9780b4f22e", "weight": 106.3, "weight_unit": "kg",
             "timestamp": "2026-07-30T04:47:39.717892+00:00"},
            {"measurement_id": "81f32f5bdb", "weight": 106.3, "weight_unit": "kg",
             "timestamp": "2026-07-30T04:47:55.374189+00:00", "body_fat": 21.8},
        ],
    },
    "last_changed": "2026-07-30T04:47:55.374491+00:00",
}


def test_collapse_folds_a_re_reported_weigh_in():
    rows = ha_client.readings_from_history_attr(REREPORTED)
    assert len(rows) == 3
    folded = ha_client.collapse_same_weight(rows)
    # The 16-seconds-apart pair at 106.3 becomes one weigh-in; 107.3 survives.
    assert [r["weight_kg"] for r in folded] == [107.3, 106.3]
    kept = folded[-1]
    # The EARLIEST key survives, so it still de-duplicates against whatever was
    # already imported when the pair straddles two polls.
    assert kept["key"] == "id:9780b4f22e"
    assert kept["measured_at"].second == 39
    # ...but it carries the later, richer body composition.
    assert kept["metrics"]["body_fat_pct"]["value"] == 21.8
    assert kept["folded"] == ["id:81f32f5bdb"]


def test_collapse_keeps_genuine_consecutive_weigh_ins():
    """The real pair 24 seconds apart differed in weight (106.4 then 106.7), and
    weight is the discriminator, so both survive."""
    rows = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)
    folded = ha_client.collapse_same_weight(rows)
    assert [r["weight_kg"] for r in folded] == [106.3, 106.4, 106.7]


def test_collapse_respects_the_window():
    """Same weight, but hours apart — two real weigh-ins, not a re-report."""
    def at(hour):
        return {"weight_kg": 100.0, "key": f"id:{hour}",
                "measured_at": datetime(2026, 7, 30, hour, tzinfo=timezone.utc),
                "metrics": {}}
    folded = ha_client.collapse_same_weight([at(6), at(7)])
    assert len(folded) == 2
    # Inside the window they fold.
    assert len(ha_client.collapse_same_weight(
        [at(6), at(6)], window_seconds=900)) == 1


def test_collapse_is_a_no_op_on_an_empty_or_single_list():
    assert ha_client.collapse_same_weight([]) == []
    one = ha_client.readings_from_history_attr(WEIGHT_WITH_HISTORY)[:1]
    assert len(ha_client.collapse_same_weight(one)) == 1


# ---------------------------------------------------------------------------
# ha_server: the per-member credential row
# ---------------------------------------------------------------------------

def test_ha_server_set_get_forget(db):
    assert db.ha_server_get(1) is None
    db.ha_server_set(1, "https://home.example.com", "enc", ha_version="2026.8",
                     location="Home")
    row = db.ha_server_get(1)
    assert row["base_url"] == "https://home.example.com"
    assert row["token_enc"] == "enc"
    assert row["ha_version"] == "2026.8"
    assert row["verified_at"] is not None
    assert db.count_ha_servers() == 1
    # Re-running /setup_ha replaces the credential in place.
    db.ha_server_set(1, "https://home.example.com", "enc2")
    assert db.ha_server_get(1)["token_enc"] == "enc2"
    assert db.count_ha_servers() == 1
    assert db.ha_server_forget(1) is True
    assert db.ha_server_forget(1) is False
    assert db.ha_server_get(1) is None


def test_rotating_a_token_keeps_the_entity_link(db):
    """Making a fresh token in Home Assistant must not cost someone their setup."""
    db.ha_server_set(1, "https://home.example.com", "enc")
    db.ha_link(1, 42, "joshua_s", weight_entity="sensor.joshua_s_weight")
    db.ha_server_set(1, "https://home.example.com", "enc-rotated")
    assert db.ha_get(1)["entity_prefix"] == "joshua_s"
    assert db.ha_server_get(1)["token_enc"] == "enc-rotated"


def test_list_ha_synced_needs_both_halves(db):
    """The poll's work list. A member with only one half is not pollable."""
    db.ha_server_set(1, "https://a.example.com", "enc")      # server, no link
    db.ha_link(2, 42, "sam")                                  # link, no server
    assert db.list_ha_synced() == []
    db.ha_link(1, 42, "joshua_s")
    rows = db.list_ha_synced()
    assert len(rows) == 1
    # The joined shape carries the credential, so the poll can build a config.
    assert rows[0]["user_id"] == 1
    assert rows[0]["base_url"] == "https://a.example.com"
    assert rows[0]["token_enc"] == "enc"


def test_two_members_keep_separate_servers(db):
    db.ha_server_set(1, "https://home.joshua.example", "enc-a")
    db.ha_server_set(2, "http://192.168.1.9:8123", "enc-b")
    db.ha_link(1, 42, "joshua_s")
    db.ha_link(2, 42, "sam")
    urls = {r["user_id"]: r["base_url"] for r in db.list_ha_synced()}
    assert urls == {1: "https://home.joshua.example", 2: "http://192.168.1.9:8123"}
