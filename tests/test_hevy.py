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
