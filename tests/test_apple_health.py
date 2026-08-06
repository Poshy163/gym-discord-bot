from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import apple_health, strava_web
from app.db import Database


def _payload(**changes):
    data = {
        "id": "healthkit-uuid-123",
        "activity": "Running",
        "started_at": "2026-08-06T07:00:00+09:30",
        "ended_at": "2026-08-06T07:30:00+09:30",
        "active_kcal": 300,
        "distance_m": 5000,
        "avg_heart_rate": 145,
        "effort": 7,
        "source_name": "Apple Watch",
    }
    data.update(changes)
    return data


def test_parse_workout_normalizes_dates_and_metrics():
    workout = apple_health.parse_workout(
        _payload(), now=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert workout.activity == "Running"
    assert workout.started_at.isoformat() == "2026-08-05T21:30:00+00:00"
    assert workout.duration_s == 1800
    assert workout.active_kcal == 300
    assert workout.distance_m == 5000
    assert workout.avg_heart_rate == 145
    assert workout.effort == 7
    assert len(workout.workout_id) == 64


def test_parse_workout_accepts_shortcut_aliases_and_duration():
    workout = apple_health.parse_workout(
        {
            "workoutType": "IndoorCycle",
            "startDate": "2026-08-06T07:00:00Z",
            "duration": 45,
            "activeCalories": "410",
            "distanceMeters": 12000,
            "averageHeartRate": 151,
            "sourceName": "Watch",
        },
        now=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )
    assert workout.duration_s == 2700
    assert workout.ended_at.isoformat() == "2026-08-06T07:45:00+00:00"
    assert workout.active_kcal == 410


def test_identity_is_stable_for_replays():
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    first = apple_health.parse_workout(_payload(), now=now)
    second = apple_health.parse_workout(_payload(active_kcal=350), now=now)
    assert first.workout_id == second.workout_id


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"activity": ""}, "missing activity"),
        ({"started_at": "2026-08-06T07:00:00"}, "include a timezone"),
        ({"ended_at": "2026-08-06T06:00:00+09:30"}, "must be after"),
        ({"effort": 11}, "between 1 and 10"),
    ],
)
def test_invalid_workout_is_rejected(changes, message):
    with pytest.raises(apple_health.WorkoutValidationError, match=message):
        apple_health.parse_workout(
            _payload(**changes),
            now=datetime(2026, 8, 7, tzinfo=timezone.utc),
        )


def test_payload_batch_limit_and_shape():
    assert len(apple_health.payload_items({"workouts": [_payload()]})) == 1
    assert apple_health.payload_items(_payload())[0]["activity"] == "Running"
    with pytest.raises(apple_health.WorkoutValidationError, match="100"):
        apple_health.payload_items({"workouts": [_payload()] * 101})
    with pytest.raises(apple_health.WorkoutValidationError, match="object"):
        apple_health.payload_items({"workouts": ["not an object"]})


def test_database_link_dedupe_recent_and_unlink(tmp_path):
    db = Database(tmp_path / "apple.sqlite3")
    try:
        token = "private-token"
        digest = apple_health.token_hash(token)
        db.apple_health_link(42, digest)
        row = db.apple_health_get(42)
        assert row["token_hash"] == digest
        assert token not in row["token_hash"]
        assert db.apple_health_get_by_token_hash(digest)["user_id"] == 42

        workout = apple_health.parse_workout(
            _payload(), now=datetime(2026, 8, 7, tzinfo=timezone.utc),
        )
        kwargs = {
            "workout_id": workout.workout_id,
            "activity": workout.activity,
            "started_at": workout.started_at,
            "ended_at": workout.ended_at,
            "duration_s": workout.duration_s,
            "active_kcal": workout.active_kcal,
            "distance_m": workout.distance_m,
            "avg_heart_rate": workout.avg_heart_rate,
            "effort": workout.effort,
        }
        assert db.apple_health_add_workout(42, **kwargs) is True
        assert db.apple_health_add_workout(42, **kwargs) is False
        assert db.apple_health_count(42) == 1
        assert db.apple_health_recent(42)[0]["activity"] == "Running"
        assert db.apple_health_get(42)["last_received_at"] is not None

        assert db.apple_health_unlink(42) is True
        assert db.apple_health_get(42) is None
        assert db.apple_health_count(42) == 0
    finally:
        db.close()


def test_web_route_requires_bearer_and_forwards_json():
    seen = []

    async def callback(_code, _state, _error):
        return "ok"

    async def event(_payload):
        return None

    async def health(token, payload):
        seen.append((token, payload))
        return 200, {"ok": True, "accepted": 1}

    async def go():
        app = strava_web.build_app(
            verify_token="verify",
            on_callback=callback,
            on_event=event,
            schedule=lambda _coro: None,
            on_apple_health=health,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            missing = await client.post(
                "/apple-health/workouts", json=_payload(),
            )
            assert missing.status == 401
            good = await client.post(
                "/apple-health/workouts",
                json=_payload(),
                headers={"Authorization": "Bearer member-secret"},
            )
            assert good.status == 200
            assert await good.json() == {"ok": True, "accepted": 1}
        finally:
            await client.close()

    asyncio.run(go())
    assert seen == [("member-secret", _payload())]
