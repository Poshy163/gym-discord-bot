"""Tests for the Hevy integration: pure workout mappers, key encryption, and the
DB account/import-dedup helpers."""

from __future__ import annotations

import pytest

from app import hevy_client
from app.db import Database
from app.parser import Lift


WORKOUT = {
    "id": "abc-123",
    "title": "Push Day",
    "start_time": "2026-06-01T08:00:00Z",
    "end_time": "2026-06-01T09:15:00Z",
    "exercises": [
        {
            "title": "Bench Press (Barbell)",
            "sets": [
                {"weight_kg": 60, "reps": 10, "type": "warmup"},
                {"weight_kg": 100, "reps": 5},
                {"weight_kg": 100, "reps": 5},
            ],
        },
        {
            "title": "Plank",
            "sets": [{"weight_kg": None, "reps": None, "duration_seconds": 60}],
        },
    ],
}


def test_workout_to_lifts_maps_weighted_sets_only():
    lifts = hevy_client.workout_to_lifts(WORKOUT)
    # 3 weighted bench sets; the weightless plank set is skipped.
    assert len(lifts) == 3
    assert all(isinstance(x, Lift) for x in lifts)
    assert [x.weight_kg for x in lifts] == [60.0, 100.0, 100.0]
    assert [x.reps for x in lifts] == [10, 5, 5]
    # All bench sets canonicalize to the same equipment name.
    assert len({x.equipment for x in lifts}) == 1
    assert lifts[0].confident is True


def test_workout_to_lifts_handles_empty_and_bad_values():
    assert hevy_client.workout_to_lifts({}) == []
    weird = {"exercises": [
        {"title": "", "sets": [{"weight_kg": 50, "reps": 5}]},        # no title
        {"title": "Squat", "sets": [{"weight_kg": "oops", "reps": 5}]},  # bad weight
        {"title": "Curl", "sets": [{"weight_kg": -5, "reps": 5}]},     # non-positive
    ]}
    assert hevy_client.workout_to_lifts(weird) == []


def test_summarize_workout_totals_and_top_set():
    s = hevy_client.summarize_workout(WORKOUT)
    assert s["id"] == "abc-123"
    assert s["title"] == "Push Day"
    assert s["exercise_count"] == 2
    assert s["set_count"] == 4
    # 60*10 + 100*5 + 100*5 = 1600 (plank contributes 0).
    assert s["volume_kg"] == 1600
    assert s["top"] == {"title": "Bench Press (Barbell)", "weight_kg": 100.0, "reps": 5}


def test_summarize_workout_full_stats():
    s = hevy_client.summarize_workout(WORKOUT)
    # Working vs warmup split (one bench set is a warmup).
    assert s["working_set_count"] == 3
    assert s["warmup_set_count"] == 1
    # Reps across all sets: 10 + 5 + 5 (+0 for the plank).
    assert s["total_reps"] == 20
    # 08:00 → 09:15 = 75 minutes.
    assert s["duration_seconds"] == 75 * 60
    # Per-exercise breakdown, in order.
    assert [e["title"] for e in s["exercises"]] == [
        "Bench Press (Barbell)", "Plank",
    ]
    bench = s["exercises"][0]
    assert bench["sets"] == 3 and bench["best_weight_kg"] == 100.0
    assert bench["best_reps"] == 5 and bench["volume_kg"] == 1600
    plank = s["exercises"][1]
    assert plank["best_weight_kg"] is None and plank["volume_kg"] == 0


def test_fetch_recent_workouts_pages_until_short_page(monkeypatch):
    # Two full pages then a short one signals the end.
    pages = {
        1: [{"id": str(i)} for i in range(10)],
        2: [{"id": str(i)} for i in range(10, 20)],
        3: [{"id": str(i)} for i in range(20, 23)],
    }
    monkeypatch.setattr(
        hevy_client, "fetch_workouts",
        lambda api_key, page=1, page_size=10: pages.get(page, []),
    )
    got = hevy_client.fetch_recent_workouts("key", limit=50)
    assert [w["id"] for w in got] == [str(i) for i in range(23)]


def test_fetch_recent_workouts_trims_to_limit(monkeypatch):
    # Always-full pages: must stop at the requested limit.
    monkeypatch.setattr(
        hevy_client, "fetch_workouts",
        lambda api_key, page=1, page_size=10: [
            {"id": str((page - 1) * 10 + i)} for i in range(10)
        ],
    )
    got = hevy_client.fetch_recent_workouts("key", limit=25)
    assert len(got) == 25 and got[-1]["id"] == "24"


def test_summarize_workout_defaults_for_empty():
    s = hevy_client.summarize_workout({})
    assert s["title"] == "Workout"
    assert s["exercise_count"] == 0 and s["set_count"] == 0
    assert s["volume_kg"] == 0 and s["top"] is None
    assert s["working_set_count"] == 0 and s["total_reps"] == 0
    assert s["duration_seconds"] is None and s["exercises"] == []


def test_api_key_encryption_roundtrip(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("HEVY_FERNET_KEY", Fernet.generate_key().decode())
    token = hevy_client.encrypt_key("secret-api-key")
    assert token != "secret-api-key"
    assert hevy_client.decrypt_key(token) == "secret-api-key"
    assert hevy_client.fernet_ready() is True


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gym.sqlite3")
    yield d
    d.close()


def test_hevy_link_get_unlink(db):
    assert db.hevy_get(1) is None
    db.hevy_link(1, 42, "enc-token", hevy_username="alice")
    row = db.hevy_get(1)
    assert int(row["guild_id"]) == 42
    assert row["api_key_enc"] == "enc-token"
    assert row["last_synced_at"] is None
    assert len(db.list_hevy_accounts()) == 1

    # Re-link updates the key/guild without duplicating the row.
    db.hevy_link(1, 99, "enc-token-2")
    row = db.hevy_get(1)
    assert int(row["guild_id"]) == 99
    assert row["api_key_enc"] == "enc-token-2"
    assert len(db.list_hevy_accounts()) == 1

    assert db.hevy_unlink(1) is True
    assert db.hevy_unlink(1) is False
    assert db.hevy_get(1) is None


def test_hevy_import_dedupe(db):
    db.hevy_link(1, 42, "enc")
    assert db.hevy_workout_imported(1, "w1") is False
    assert db.hevy_mark_workout(1, "w1") is True
    # Second mark is a no-op (already imported) — guards against double-logging.
    assert db.hevy_mark_workout(1, "w1") is False
    assert db.hevy_workout_imported(1, "w1") is True
    # Per-user isolation.
    assert db.hevy_workout_imported(2, "w1") is False

    # Unlinking clears import history so a fresh link re-imports cleanly.
    db.hevy_unlink(1)
    assert db.hevy_workout_imported(1, "w1") is False


def test_hevy_mark_synced(db):
    db.hevy_link(1, 42, "enc")
    assert db.hevy_get(1)["last_synced_at"] is None
    db.hevy_mark_synced(1)
    assert db.hevy_get(1)["last_synced_at"] is not None


def test_hevy_backfill_marker(db):
    # New accounts start un-backfilled so the poll catches up their history once.
    db.hevy_link(1, 42, "enc")
    assert db.hevy_get(1)["backfilled_at"] is None
    db.hevy_mark_backfilled(1)
    assert db.hevy_get(1)["backfilled_at"] is not None


# ---------------------------------------------------------------------------
# Richer workout summary (fields the importer used to drop)
# ---------------------------------------------------------------------------

RICH_WORKOUT = {
    "id": "rich-1",
    "title": "Leg Day",
    "description": "Felt strong.",
    "routine_id": "routine-9",
    "start_time": "2026-06-02T08:00:00Z",
    "end_time": "2026-06-02T09:00:00Z",
    "exercises": [
        {
            "title": "Squat (Barbell)",
            "exercise_template_id": "TPL-SQUAT",
            "notes": "Depth felt good",
            "sets": [
                {"weight_kg": 100, "reps": 5, "type": "normal", "rpe": 8},
                {"weight_kg": 100, "reps": 5, "type": "failure", "rpe": 9.5},
                {"weight_kg": 80, "reps": 8, "type": "dropset"},
            ],
        },
        {
            "title": "Bench Press (Barbell)",
            "exercise_template_id": "TPL-BENCH",
            "sets": [{"weight_kg": 60, "reps": 10}],
        },
    ],
}

CARDIO_WORKOUT = {
    "id": "cardio-1",
    "title": "Treadmill",
    "start_time": "2026-06-03T08:00:00Z",
    "exercises": [
        {
            "title": "Running",
            "exercise_template_id": "TPL-RUN",
            "sets": [
                {"distance_meters": 5000, "duration_seconds": 1500},
            ],
        },
        {
            "title": "Pull Up",
            "exercise_template_id": "TPL-PULLUP",
            "sets": [{"reps": 12}, {"reps": 10}],
        },
    ],
}


def test_summarize_workout_captures_rpe_and_set_types():
    s = hevy_client.summarize_workout(RICH_WORKOUT)
    assert s["description"] == "Felt strong."
    assert s["routine_id"] == "routine-9"
    assert s["dropset_count"] == 1
    assert s["failure_set_count"] == 1
    assert s["best_rpe"] == 9.5
    assert s["has_lifts"] is True
    squat = s["exercises"][0]
    assert squat["template_id"] == "TPL-SQUAT"
    assert squat["notes"] == "Depth felt good"
    assert squat["rpe"] == 9.5


def test_summarize_workout_describes_a_lift_free_session():
    """A calisthenics/cardio session has no volume — it must still report totals,
    because the feed embed is the only thing that will ever describe it."""
    s = hevy_client.summarize_workout(CARDIO_WORKOUT)
    assert s["has_lifts"] is False
    assert s["volume_kg"] == 0
    assert s["distance_m"] == 5000
    assert s["active_seconds"] == 1500
    # Reps from weightless sets are counted separately from loaded ones.
    assert s["bodyweight_reps"] == 22
    assert s["total_reps"] == 22


def test_muscle_split_ranks_volume_by_primary_group():
    templates = hevy_client.index_templates([
        {"id": "TPL-SQUAT", "title": "Squat", "primary_muscle_group": "quadriceps"},
        {"id": "TPL-BENCH", "title": "Bench", "primary_muscle_group": "chest"},
    ])
    s = hevy_client.summarize_workout(RICH_WORKOUT)
    split = hevy_client.muscle_split(s, templates)
    # squat 100*5 + 100*5 + 80*8 = 1640; bench 60*10 = 600.
    assert split == [("quadriceps", 1640), ("chest", 600)]
    assert hevy_client.muscle_label("upper_back") == "Upper back"
    # An unknown group degrades to a readable label rather than vanishing.
    assert hevy_client.muscle_label("brand_new") == "Brand new"


def test_muscle_split_skips_exercises_with_no_known_template():
    s = hevy_client.summarize_workout(RICH_WORKOUT)
    assert hevy_client.muscle_split(s, {}) == []


def test_index_templates_ignores_entries_without_an_id():
    got = hevy_client.index_templates([{"title": "no id"}, {"id": "A", "title": "x"}])
    assert list(got) == ["A"]


# ---------------------------------------------------------------------------
# Body-measurement write-back
# ---------------------------------------------------------------------------

def test_measurement_fields_maps_ha_metrics_to_hevy_names():
    fields = hevy_client.measurement_fields(82.4, {
        "body_fat_pct": {"value": 18.5, "unit": "%"},
        "fat_free_mass_kg": {"value": 65.0, "unit": "kg"},
        "bmi": {"value": 24.1, "unit": ""},  # Hevy has no field for this
    })
    assert fields == {
        "weight_kg": 82.4, "fat_percent": 18.5, "lean_mass_kg": 65.0,
    }


def test_measurement_fields_weight_only_and_empty():
    assert hevy_client.measurement_fields(80) == {"weight_kg": 80.0}
    assert hevy_client.measurement_fields(None, None) == {}
    assert hevy_client.measurement_fields(None, {"bmi": {"value": 24}}) == {}


def test_measurement_fields_drops_implausible_values():
    """A glitching BLE scale (the classic 6553.5 kg register) must not be
    mirrored into Hevy, where the bot cannot easily retract it."""
    assert hevy_client.measurement_fields(6553.5) == {}
    assert hevy_client.measurement_fields(0) == {}
    fields = hevy_client.measurement_fields(80, {"body_fat_pct": {"value": 999}})
    assert fields == {"weight_kg": 80.0}


def test_merge_measurement_preserves_hand_entered_fields():
    """Hevy's PUT nulls every omitted field, so the merge must carry through
    measurements the bot has no opinion about — otherwise pushing a weigh-in
    silently wipes the member's tape-measure entries."""
    existing = {
        "date": "2026-06-01", "weight_kg": 81.0,
        "waist": 80.0, "chest_cm": 95.0, "left_bicep_cm": None,
    }
    merged = hevy_client.merge_measurement(existing, {"weight_kg": 82.0})
    assert merged["weight_kg"] == 82.0
    assert merged["waist"] == 80.0 and merged["chest_cm"] == 95.0
    assert "date" not in merged          # it lives in the URL
    assert "left_bicep_cm" not in merged  # nulls aren't echoed back


def test_merge_measurement_returns_none_when_nothing_changed():
    existing = {"date": "2026-06-01", "weight_kg": 82.0, "waist": 80.0}
    assert hevy_client.merge_measurement(existing, {"weight_kg": 82.0}) is None
    # A change below the rounding floor is still "unchanged".
    assert hevy_client.merge_measurement(existing, {"weight_kg": 82.001}) is None


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = "{}" if payload is None else "payload"

    def json(self):
        return self._payload


class _FakeRequests:
    """Stands in for the `requests` module, recording every call."""

    class RequestException(Exception):
        pass

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        key = (method, url.rsplit("/v1", 1)[-1])
        result = self._responses.get(key)
        if callable(result):
            result = result(len(self.calls))
        if result is None:
            raise AssertionError(f"unexpected call {key}")
        return result


def test_push_body_measurement_creates_when_the_date_is_free(monkeypatch):
    fake = _FakeRequests({
        ("POST", "/body_measurements"): _FakeResponse(200),
    })
    monkeypatch.setattr(hevy_client, "requests", fake)
    out = hevy_client.push_body_measurement("k", "2026-06-01", {"weight_kg": 82.0})
    assert out == "created"
    assert fake.calls[0][2] == {"date": "2026-06-01", "weight_kg": 82.0}
    assert len(fake.calls) == 1  # no wasted read


def test_push_body_measurement_merges_on_conflict(monkeypatch):
    """The 409 path must read the day's entry and PUT a *complete* object, so
    the member's hand-entered circumferences survive the update."""
    fake = _FakeRequests({
        ("POST", "/body_measurements"): _FakeResponse(409),
        ("GET", "/body_measurements/2026-06-01"): _FakeResponse(200, {
            "date": "2026-06-01", "weight_kg": 81.0, "waist": 80.0,
        }),
        ("PUT", "/body_measurements/2026-06-01"): _FakeResponse(200),
    })
    monkeypatch.setattr(hevy_client, "requests", fake)
    out = hevy_client.push_body_measurement("k", "2026-06-01", {"weight_kg": 82.0})
    assert out == "updated"
    method, _url, body = fake.calls[-1]
    assert method == "PUT"
    assert body == {"weight_kg": 82.0, "waist": 80.0}


def test_push_body_measurement_skips_a_pointless_write(monkeypatch):
    fake = _FakeRequests({
        ("POST", "/body_measurements"): _FakeResponse(409),
        ("GET", "/body_measurements/2026-06-01"): _FakeResponse(200, {
            "date": "2026-06-01", "weight_kg": 82.0,
        }),
    })
    monkeypatch.setattr(hevy_client, "requests", fake)
    out = hevy_client.push_body_measurement("k", "2026-06-01", {"weight_kg": 82.0})
    assert out == "unchanged"
    assert [c[0] for c in fake.calls] == ["POST", "GET"]  # never PUT


def test_push_body_measurement_is_a_noop_without_fields():
    assert hevy_client.push_body_measurement("k", "2026-06-01", {}) == "unchanged"


def test_auth_and_missing_map_to_typed_errors(monkeypatch):
    monkeypatch.setattr(hevy_client, "requests", _FakeRequests({
        ("GET", "/user/info"): _FakeResponse(401),
    }))
    with pytest.raises(hevy_client.HevyAuthError):
        hevy_client.fetch_user_info("k")
    monkeypatch.setattr(hevy_client, "requests", _FakeRequests({
        ("GET", "/body_measurements/2026-06-01"): _FakeResponse(404),
    }))
    # 404 is "nothing recorded that day", not a failure.
    assert hevy_client.fetch_body_measurement("k", "2026-06-01") is None


def test_fetch_user_info_unwraps_the_data_envelope(monkeypatch):
    monkeypatch.setattr(hevy_client, "requests", _FakeRequests({
        ("GET", "/user/info"): _FakeResponse(200, {
            "data": {"id": "u1", "name": "alice", "url": "https://hevy.com/user/alice"},
        }),
    }))
    assert hevy_client.fetch_user_info("k") == {
        "id": "u1", "name": "alice", "url": "https://hevy.com/user/alice",
    }


def test_hevy_set_profile_only_writes_on_change(db):
    db.hevy_link(1, 42, "enc")
    assert db.hevy_set_profile(1, "alice", "https://hevy.com/user/alice") is True
    row = db.hevy_get(1)
    assert row["hevy_username"] == "alice"
    assert row["hevy_profile_url"] == "https://hevy.com/user/alice"
    # Idempotent: a poll every few minutes must not be a write every few minutes.
    assert db.hevy_set_profile(1, "alice", "https://hevy.com/user/alice") is False
    # Unknown user is a no-op rather than an error.
    assert db.hevy_set_profile(999, "bob", None) is False


def test_relink_clears_the_cached_hevy_profile(db):
    """A re-link may point at a *different* Hevy account. Keeping the old name
    and URL would caption the member's feed embeds with someone else's
    identity, so both are cleared for the next sync to refill."""
    db.hevy_link(1, 42, "enc-v1")
    db.hevy_set_profile(1, "joshl", "https://hevy.com/user/joshl")
    db.hevy_link(1, 42, "enc-v2")
    row = db.hevy_get(1)
    assert row["hevy_username"] is None
    assert row["hevy_profile_url"] is None
    assert row["api_key_enc"] == "enc-v2"
