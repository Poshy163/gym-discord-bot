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


# ---------------------------------------------------------------------------
# Rate limiting (429) — hit for real while validating against a live account
# ---------------------------------------------------------------------------

class _RateLimitedThenOk:
    """429 for the first ``fails`` calls, then a normal response."""

    class RequestException(Exception):
        pass

    def __init__(self, fails: int, retry_after: str | None = None):
        self.fails = fails
        self.retry_after = retry_after
        self.calls = 0

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            resp = _FakeResponse(429)
            resp.headers = (
                {"Retry-After": self.retry_after} if self.retry_after else {}
            )
            return resp
        ok = _FakeResponse(200, {"workouts": [{"id": "w1"}]})
        ok.headers = {}
        return ok


def test_rate_limited_request_retries_and_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(hevy_client.time, "sleep", sleeps.append)
    fake = _RateLimitedThenOk(fails=2)
    monkeypatch.setattr(hevy_client, "requests", fake)
    assert hevy_client.fetch_workouts("k") == [{"id": "w1"}]
    assert fake.calls == 3
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_rate_limit_honours_hevys_own_retry_after(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(hevy_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        hevy_client, "requests", _RateLimitedThenOk(fails=1, retry_after="5"),
    )
    hevy_client.fetch_workouts("k")
    assert sleeps == [5.0]


def test_rate_limit_ignores_an_absurd_retry_after(monkeypatch):
    """A punitive or malformed hint must not stall a poll for an hour."""
    sleeps: list[float] = []
    monkeypatch.setattr(hevy_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        hevy_client, "requests", _RateLimitedThenOk(fails=1, retry_after="3600"),
    )
    hevy_client.fetch_workouts("k")
    assert sleeps == [2.0]  # fell back to the backoff


def test_rate_limit_gives_up_and_raises(monkeypatch):
    monkeypatch.setattr(hevy_client.time, "sleep", lambda _s: None)
    fake = _RateLimitedThenOk(fails=99)
    monkeypatch.setattr(hevy_client, "requests", fake)
    with pytest.raises(hevy_client.HevyRateLimited):
        hevy_client.fetch_workouts("k")
    assert fake.calls == hevy_client._RATE_LIMIT_ATTEMPTS
    # Still a HevyError, so every existing caller's except-branch still catches it.
    assert issubclass(hevy_client.HevyRateLimited, hevy_client.HevyError)


def test_hevy_names_land_on_the_same_equipment_as_chat_logged_ones():
    """Checked against a real Hevy workout: these three forked their own
    equipment, so a Hevy "Butterfly (Pec Deck)" and a typed "pec dec" were two
    different lifts with two different PRs."""
    from app.aliases import canonicalize
    assert canonicalize("Butterfly (Pec Deck)") == "pec dec"
    assert canonicalize("Seated Shoulder Press (Machine)") == "shoulder press"
    # These already worked — pinned so the new aliases don't disturb them.
    assert canonicalize("Lat Pulldown (Cable)") == "lat pulldown"
    assert canonicalize("Preacher Curl (Machine)") == "preacher curl"
    assert canonicalize("Hammer Curl (Dumbbell)") == "hammer curl"
    assert canonicalize("Lateral Raise (Machine)") == "lateral raise"
    # Assisted pull-ups must stay on a bodyweight-assisted equipment key, where
    # the logged kg is read as machine assistance rather than load.
    assert canonicalize("Pull Up (Assisted)") in {"pull ups", "chin assist"}


# ---------------------------------------------------------------------------
# One-off re-canonicalisation of already-imported Hevy lifts
# ---------------------------------------------------------------------------

def _seed_legacy_hevy_lift(path, equipment: str, raw_title: str) -> None:
    """Write a lift as an older build would have stored it, then clear the
    migration marker so the next open re-runs the one-shot."""
    import sqlite3 as _sq
    c = _sq.connect(path)
    c.execute(
        "INSERT INTO lifts (guild_id, user_id, username, equipment, weight_kg,"
        " reps, raw, logged_at) VALUES (1, 7, 'poshy', ?, 100, 6, ?, ?)",
        (equipment, f"hevy:{raw_title}", "2026-08-19T13:16:09+00:00"),
    )
    # Only the v2 marker: v1 does the same re-derivation and in any real
    # database ran long ago, so clearing it too would let v1 silently do
    # v2's work and the test would prove nothing.
    c.execute("DELETE FROM app_meta WHERE key = 'hevy_equip_recanon_v2'")
    c.commit()
    c.close()


def test_recanon_v2_refiles_hevy_machine_names(tmp_path):
    """The lifts were already imported and their workout id is recorded for
    good, so nothing will ever re-read them — the migration is the only way
    their equipment can be corrected."""
    path = tmp_path / "gym.sqlite3"
    Database(path).close()
    _seed_legacy_hevy_lift(path, "butterfly", "Butterfly (Pec Deck)")
    _seed_legacy_hevy_lift(path, "seated shoulder press",
                           "Seated Shoulder Press (Machine)")
    _seed_legacy_hevy_lift(path, "lat pulldown", "Lat Pulldown (Cable)")

    db2 = Database(path)
    try:
        rows = {
            r["raw"]: r["equipment"]
            for r in db2._connection.execute(
                "SELECT raw, equipment FROM lifts WHERE raw LIKE 'hevy:%'")
        }
        assert rows["hevy:Butterfly (Pec Deck)"] == "pec dec"
        assert rows["hevy:Seated Shoulder Press (Machine)"] == "shoulder press"
        # Untouched: it was already correct, so it must not appear in the notice.
        assert rows["hevy:Lat Pulldown (Cable)"] == "lat pulldown"

        notice = db2.take_hevy_recanon_notice()
        assert notice == {
            "butterfly\u2192pec dec": 1,
            "seated shoulder press\u2192shoulder press": 1,
        }
        # Read-and-clear: a restart loop must not announce the same rename twice.
        assert db2.take_hevy_recanon_notice() == {}
    finally:
        db2.close()


def test_recanon_v2_is_silent_when_nothing_changes(tmp_path):
    path = tmp_path / "gym.sqlite3"
    Database(path).close()
    _seed_legacy_hevy_lift(path, "lat pulldown", "Lat Pulldown (Cable)")
    db2 = Database(path)
    try:
        assert db2.take_hevy_recanon_notice() == {}
    finally:
        db2.close()


def test_recanon_v2_runs_only_once(tmp_path):
    path = tmp_path / "gym.sqlite3"
    Database(path).close()
    _seed_legacy_hevy_lift(path, "butterfly", "Butterfly (Pec Deck)")
    Database(path).close()          # migration runs, notice written
    db3 = Database(path)             # reopening must not re-run it
    try:
        assert db3.take_hevy_recanon_notice() == {
            "butterfly\u2192pec dec": 1,
        }
        db3.close()
        db4 = Database(path)
        assert db4.take_hevy_recanon_notice() == {}
        db4.close()
    finally:
        pass


# ---------------------------------------------------------------------------
# Weighted vs assisted bodyweight lifts (resolved by the template's `type`)
# ---------------------------------------------------------------------------

BW_WORKOUT = {
    "id": "bw-1", "title": "Calisthenics",
    "start_time": "2026-08-19T08:00:00Z",
    "exercises": [
        {"title": "Pull Up (Weighted)", "exercise_template_id": "TPL-W",
         "sets": [{"weight_kg": 20, "reps": 5}]},
        {"title": "Pull Up (Assisted)", "exercise_template_id": "TPL-A",
         "sets": [{"weight_kg": 36, "reps": 6}]},
    ],
}

BW_TEMPLATES = hevy_client.index_templates([
    # Hevy's real values, confirmed against the live /exercise_templates feed.
    {"id": "TPL-W", "title": "Pull Up (Weighted)", "type": "bodyweight_weighted",
     "primary_muscle_group": "lats"},
    {"id": "TPL-A", "title": "Pull Up (Assisted)", "type": "bodyweight_assisted",
     "primary_muscle_group": "lats"},
])


def test_weighted_bodyweight_lift_is_added_not_subtracted():
    """Both titles canonicalize to `pull ups`, which the bot reads as an
    assistance-logged lift. Without the template a +20kg weighted pull-up became
    20kg of *assistance* — bodyweight minus 20 instead of plus, a ~40kg error
    that makes adding weight look like a regression."""
    weighted, assisted = hevy_client.workout_to_lifts(BW_WORKOUT, BW_TEMPLATES)
    assert weighted.weight_kg == 20.0 and weighted.bodyweight_add is True
    assert assisted.weight_kg == 36.0 and assisted.bodyweight_add is False


def test_bodyweight_lift_without_a_template_keeps_the_old_reading():
    """Assistance is the commoner logging style and what the equipment name
    already implies, so an unresolved template must not flip the meaning."""
    for lift in hevy_client.workout_to_lifts(BW_WORKOUT):
        assert lift.bodyweight_add is False
    for lift in hevy_client.workout_to_lifts(BW_WORKOUT, {}):
        assert lift.bodyweight_add is False


def test_ordinary_lifts_are_never_marked_bodyweight_relative():
    templates = hevy_client.index_templates([
        {"id": "TPL-B", "title": "Bench Press (Barbell)", "type": "weight_reps"},
    ])
    w = {"exercises": [{"title": "Bench Press (Barbell)",
                        "exercise_template_id": "TPL-B",
                        "sets": [{"weight_kg": 100, "reps": 5}]}]}
    lift, = hevy_client.workout_to_lifts(w, templates)
    assert lift.bodyweight_add is False and lift.equipment == "bench press"


def test_index_templates_keeps_the_type():
    assert BW_TEMPLATES["TPL-W"]["type"] == "bodyweight_weighted"
    assert BW_TEMPLATES["TPL-A"]["type"] == "bodyweight_assisted"


# ---------------------------------------------------------------------------
# Routines, folders, and live-API quirks (verified against a real account)
# ---------------------------------------------------------------------------

def test_summarize_reads_the_real_superset_key():
    """The live payload uses ``superset_id``; Hevy's OpenAPI spec documents
    ``supersets_id`` and is wrong. Trusting the spec meant the field read None
    forever."""
    s = hevy_client.summarize_workout({"exercises": [
        {"title": "A", "superset_id": 0, "sets": [{"weight_kg": 50, "reps": 5}]},
        {"title": "B", "supersets_id": 1, "sets": [{"weight_kg": 50, "reps": 5}]},
    ]})
    assert s["exercises"][0]["superset_id"] == 0
    assert s["exercises"][1]["superset_id"] == 1  # spec spelling still accepted


def test_fetch_routine_folders_accepts_the_misnamed_key(monkeypatch):
    """Live quirk: /routine_folders returns its list under "routines"."""
    monkeypatch.setattr(hevy_client, "requests", _FakeRequests({
        ("GET", "/routine_folders"): _FakeResponse(200, {
            "page": 1, "page_count": 1,
            "routines": [{"id": 42, "title": "Push Pull", "index": 0}],
        }),
    }))
    got = hevy_client.fetch_routine_folders("k")
    assert got == [{"id": 42, "title": "Push Pull", "index": 0}]


def test_index_routines_resolves_folder_titles():
    routines = [
        {"id": "r1", "title": "Arms", "folder_id": 42},
        {"id": "r2", "title": "Legs", "folder_id": None},
        {"id": "", "title": "ignored"},
    ]
    folders = [{"id": 42, "title": "Push Pull"}]
    got = hevy_client.index_routines(routines, folders)
    assert got == {
        "r1": {"title": "Arms", "folder": "Push Pull"},
        "r2": {"title": "Legs", "folder": None},
    }


# ---------------------------------------------------------------------------
# Workout shape (routine usage) + supersets + custom metrics
# ---------------------------------------------------------------------------

def test_superset_marks_include_superset_zero():
    """Hevy numbers supersets from 0 — a truthiness check would silently
    unmark the first superset in every workout."""
    exercises = [
        {"title": "A", "superset_id": 0},
        {"title": "B", "superset_id": 0},
        {"title": "C", "superset_id": None},
        {"title": "D", "superset_id": 1},   # lone survivor of a deleted partner
    ]
    assert hevy_client.superset_marks(exercises) == [True, True, False, False]
    assert hevy_client.superset_marks([]) == []


def test_summarize_sums_custom_metric_per_exercise_only():
    s = hevy_client.summarize_workout({"exercises": [
        {"title": "Stair Machine", "sets": [
            {"duration_seconds": 300, "custom_metric": 20},
            {"duration_seconds": 300, "custom_metric": 15},
        ]},
        {"title": "Bench", "sets": [{"weight_kg": 100, "reps": 5}]},
    ]})
    assert s["exercises"][0]["custom_metric"] == 35.0
    assert s["exercises"][1]["custom_metric"] is None   # absent, not 0
    assert "custom_metric" not in s or not isinstance(s.get("custom_metric"), float)


def test_hevy_record_shape_is_update_only(db):
    """A workout id that was never claimed gets nothing — the shape stamp must
    never manufacture a ledger row and weaken the claim-then-write dedup."""
    db.hevy_link(1, 42, "enc")
    db.hevy_record_shape(1, "ghost", "r1", "T", "2026-08-19T13:16:09+00:00", None)
    assert db.hevy_workout_imported(1, "ghost") is False
    assert db.hevy_routine_usage(1) == []


def test_hevy_record_shape_stores_null_not_now(db):
    """A missing started_at must stay NULL — defaulting to "now" would make
    every historical workout look like it happened at the last poll, and the
    usage reader would count it."""
    db.hevy_link(1, 42, "enc")
    db.hevy_mark_workout(1, "w1")
    db.hevy_record_shape(1, "w1", "r1", "Arms", None, None)
    assert db.hevy_routine_usage(1) == []   # no started_at -> not counted


def test_hevy_routine_usage_groups_and_falls_back(db):
    db.hevy_link(1, 42, "enc")
    for wid, rid, title, at in [
        ("w1", "r1", "Arms", "2026-08-01T10:00:00+00:00"),
        ("w2", "r1", "Arms", "2026-08-08T10:00:00+00:00"),
        ("w3", "r1", "Arms v2", "2026-08-15T10:00:00+00:00"),
        ("w4", None, "Freestyle", "2026-08-10T10:00:00+00:00"),
    ]:
        db.hevy_mark_workout(1, wid)
        db.hevy_record_shape(1, wid, rid, title, at, None)
    usage = db.hevy_routine_usage(1)
    assert [(r["routine_id"], r["sessions"]) for r in usage] == [
        ("r1", 3), (None, 1),
    ]
    top = usage[0]
    # The freshest title represents the group (deleted-routine fallback).
    assert top["last_title"] == "Arms v2"
    assert top["first_at"] == "2026-08-01T10:00:00+00:00"
    assert top["last_at"] == "2026-08-15T10:00:00+00:00"


def test_hevy_unlink_clears_shapes_with_the_ledger(db):
    db.hevy_link(1, 42, "enc")
    db.hevy_mark_workout(1, "w1")
    db.hevy_record_shape(1, "w1", "r1", "Arms", "2026-08-01T10:00:00+00:00", None)
    db.hevy_unlink(1)
    assert db.hevy_routine_usage(1) == []


# ---------------------------------------------------------------------------
# Events sync: client pagers, parsers, and the DB replace/retract primitives
# ---------------------------------------------------------------------------

def test_parse_workout_event_normalises_both_kinds():
    up = hevy_client.parse_workout_event({
        "type": "updated",
        "workout": {"id": "w1", "title": "Arms",
                    "updated_at": "2026-08-19T13:40:46.788Z"},
    })
    assert up == {"kind": "updated", "workout_id": "w1",
                  "workout": {"id": "w1", "title": "Arms",
                              "updated_at": "2026-08-19T13:40:46.788Z"},
                  "at": "2026-08-19T13:40:46.788Z"}
    de = hevy_client.parse_workout_event({
        "type": "deleted", "id": "w2", "deleted_at": "2026-08-19T14:00:00Z",
    })
    assert de == {"kind": "deleted", "workout_id": "w2", "workout": None,
                  "at": "2026-08-19T14:00:00Z"}
    assert hevy_client.parse_workout_event({"type": "updated"}) is None
    assert hevy_client.parse_workout_event({"type": "exploded"}) is None
    assert hevy_client.parse_workout_event("junk") is None


def test_walk_workout_events_reports_the_cap(monkeypatch):
    """capped=True is the signal the cursor must NOT advance: events come
    newest-first, so the unread remainder is older than everything read."""
    pages = {
        1: {"page_count": 3, "events": [{"type": "deleted", "id": "e1"}] * 10},
        2: {"page_count": 3, "events": [{"type": "deleted", "id": "e2"}] * 10},
        3: {"page_count": 3, "events": [{"type": "deleted", "id": "e3"}] * 4},
    }
    monkeypatch.setattr(
        hevy_client, "fetch_workout_events",
        lambda api_key, since, page=1, page_size=10: pages.get(page, {}),
    )
    events, capped = hevy_client.walk_workout_events("k", "s", max_pages=2)
    assert len(events) == 20 and capped is True
    events, capped = hevy_client.walk_workout_events("k", "s", max_pages=5)
    assert len(events) == 24 and capped is False


def test_fetch_workout_unwraps_and_maps_404(monkeypatch):
    monkeypatch.setattr(hevy_client, "requests", _FakeRequests({
        ("GET", "/workouts/w1"): _FakeResponse(200, {"workout": {"id": "w1"}}),
        ("GET", "/workouts/w2"): _FakeResponse(200, {"id": "w2"}),
        ("GET", "/workouts/gone"): _FakeResponse(404),
    }))
    assert hevy_client.fetch_workout("k", "w1") == {"id": "w1"}
    assert hevy_client.fetch_workout("k", "w2") == {"id": "w2"}
    assert hevy_client.fetch_workout("k", "gone") is None


def _seed_import(db, wid, lifts_weights, *, guild=42, user=1):
    """Claim a workout and give it provenance-linked lifts."""
    db.hevy_mark_workout(user, wid)
    lifts = [Lift(equipment="bench press", weight_kg=w, reps=5,
                  raw=f"hevy:Bench", confident=True, structured=True)
             for w in lifts_weights]
    n = db.add_lifts(guild_id=guild, user_id=user, username="u",
                     lifts=lifts, hevy_workout_id=wid)
    db.hevy_record_import(user, wid, n)
    return n


def test_hevy_retract_workout_removes_exactly_its_rows(db):
    db.hevy_link(1, 42, "enc")
    _seed_import(db, "w1", [100.0, 105.0])
    _seed_import(db, "w2", [90.0])
    # A chat-logged lift with no provenance must survive any retraction.
    db.add_lifts(guild_id=42, user_id=1, username="u",
                 lifts=[Lift(equipment="bench press", weight_kg=80.0,
                             raw="bench 80", confident=True, structured=True)])

    deleted, equips, guild = db.hevy_retract_workout(1, "w1")
    assert deleted == 2 and equips == {"bench press"} and guild == 42
    assert db.bests_for_equipment(1, ["bench press"]) == {"bench press": 90.0}
    # Idempotent: a replayed event is a no-op.
    assert db.hevy_retract_workout(1, "w1") == (0, set(), None)
    # The ledger row survives, marked — so the page-1 poll cannot re-import.
    row = db.hevy_get_import(1, "w1")
    assert row["retracted_at"] is not None and row["lifts_linked"] == 0


def test_hevy_replace_lifts_swaps_in_one_transaction(db):
    db.hevy_link(1, 42, "enc")
    _seed_import(db, "w1", [100.0])
    new_lifts = [Lift(equipment="bench press", weight_kg=140.0, reps=5,
                      raw="hevy:Bench", confident=True, structured=True)]
    deleted, inserted = db.hevy_replace_lifts(
        1, "w1", 42, "u", new_lifts, None, hevy_updated_at="stamp-2",
    )
    assert (deleted, inserted) == (1, 1)
    assert db.bests_for_equipment(1, ["bench press"]) == {"bench press": 140.0}
    row = db.hevy_get_import(1, "w1")
    assert row["lifts_linked"] == 1 and row["updated_at"] == "stamp-2"


def test_events_cursor_is_monotonic_across_spellings(db):
    """Hevy stamps fractional seconds with a Z suffix while the store writes
    +00:00 — a string compare would let '...00.500Z' lose to '...00Z' and stall
    the cursor. The guard parses."""
    from datetime import datetime, timezone

    db.hevy_link(1, 42, "enc")
    t1 = datetime(2026, 8, 19, 13, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 19, 13, 0, 0, 500000, tzinfo=timezone.utc)
    db.hevy_set_events_since(1, t1)
    first = db.hevy_get(1)["events_since"]
    db.hevy_set_events_since(1, t2)          # half a second later: advances
    assert db.hevy_get(1)["events_since"] != first
    db.hevy_set_events_since(1, t1)          # earlier: refused
    assert db.hevy_get(1)["events_since"] != first


def test_lift_provenance_reads_guild_from_the_rows(db):
    """A re-link rewrites hevy_account.guild_id; the rows must keep their own."""
    db.hevy_link(1, 42, "enc")
    _seed_import(db, "w1", [100.0], guild=42)
    db.hevy_link(1, 99, "enc-2")     # member re-links from another server
    guild, equips, count = db.hevy_lift_provenance(1, "w1")
    assert guild == 42 and count == 1


# ---------------------------------------------------------------------------
# Hevy machine map (dashboard-editable template -> equipment routing)
# ---------------------------------------------------------------------------

_CATALOGUE = [
    {"id": "T-BENCH", "title": "Bench Press (Barbell)",
     "primary_muscle_group": "chest", "is_custom": False},
    {"id": "T-BFLY", "title": "Butterfly (Pec Deck)",
     "primary_muscle_group": "chest", "is_custom": False},
    {"id": "T-CUSTOM", "title": "Josh's Weird Machine",
     "primary_muscle_group": "other", "is_custom": True},
]


def test_equipment_map_refresh_seeds_and_is_idempotent(db):
    assert db.hevy_equipment_map_refresh(_CATALOGUE) == 3
    rows = {r["template_id"]: r for r in db.hevy_equipment_map_all()}
    assert rows["T-BENCH"]["equipment"] == "bench press"
    assert rows["T-BFLY"]["equipment"] == "pec dec"
    assert rows["T-CUSTOM"]["is_custom"] == 1
    # Unchanged catalogue -> zero writes.
    assert db.hevy_equipment_map_refresh(_CATALOGUE) == 0


def test_equipment_map_override_survives_a_refresh(db):
    """The whole reason ``overridden`` exists: the daily catalogue refresh must
    never revert an operator's dashboard decision."""
    db.hevy_equipment_map_refresh(_CATALOGUE)
    assert db.hevy_equipment_map_set("T-BFLY", "chest press", "op") == "chest press"
    db.hevy_equipment_map_refresh(_CATALOGUE)
    row = {r["template_id"]: r for r in db.hevy_equipment_map_all()}["T-BFLY"]
    assert row["equipment"] == "chest press" and row["overridden"] == 1
    # Clearing hands it back to the automatic resolution and unpins.
    assert db.hevy_equipment_map_set("T-BFLY", None, "op") == "pec dec"
    row = {r["template_id"]: r for r in db.hevy_equipment_map_all()}["T-BFLY"]
    assert row["overridden"] == 0
    assert db.hevy_equipment_map_set("nope", "x", "op") is None


def test_equipment_map_override_is_canonicalised_itself(db):
    """An operator typing an alias ("ohp") must land on the canonical name, or
    the map would fork the very buckets it exists to merge."""
    db.hevy_equipment_map_refresh(_CATALOGUE)
    assert db.hevy_equipment_map_set("T-BENCH", "OHP", "op") == "shoulder press"


def test_equipment_overrides_feed_the_importer(db):
    db.hevy_equipment_map_refresh(_CATALOGUE)
    assert db.hevy_equipment_overrides() == {}
    db.hevy_equipment_map_set("T-BFLY", "chest press", "op")
    assert db.hevy_equipment_overrides() == {"T-BFLY": "chest press"}


def test_workout_to_lifts_prefers_the_override():
    workout = {"exercises": [{
        "title": "Butterfly (Pec Deck)", "exercise_template_id": "T-BFLY",
        "sets": [{"weight_kg": 100, "reps": 6}],
    }]}
    lift, = hevy_client.workout_to_lifts(
        workout, None, {"T-BFLY": "chest press"},
    )
    assert lift.equipment == "chest press"
    # Without the override the alias table decides, as before.
    lift, = hevy_client.workout_to_lifts(workout)
    assert lift.equipment == "pec dec"
