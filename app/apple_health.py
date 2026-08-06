"""Validation and identity helpers for Apple Health Shortcut imports.

HealthKit data stays on the member's iPhone.  The companion Shortcut sends a
small, explicit workout summary to the bot; this module validates that untrusted
JSON before it reaches SQLite or Discord.
"""
from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

MAX_BATCH_SIZE = 100
MAX_WORKOUT_SECONDS = 7 * 24 * 60 * 60


class WorkoutValidationError(ValueError):
    """An inbound Shortcut workout is missing or contains invalid data."""


@dataclass(frozen=True)
class Workout:
    workout_id: str
    activity: str
    started_at: datetime
    ended_at: datetime
    duration_s: int
    active_kcal: float | None = None
    distance_m: float | None = None
    avg_heart_rate: float | None = None
    elevation_m: float | None = None
    effort: float | None = None
    source_name: str | None = None


def new_token() -> str:
    """Return a high-entropy token suitable for a Shortcut Authorization header."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Hash a bearer token for lookup without persisting the plaintext secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _first(data: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return None


def _text(
    data: Mapping[str, object], *names: str, required: bool = False,
    max_length: int = 200,
) -> str | None:
    value = _first(data, *names)
    if value is None:
        if required:
            raise WorkoutValidationError(f"missing {names[0]}")
        return None
    text = " ".join(str(value).strip().split())
    if not text and required:
        raise WorkoutValidationError(f"missing {names[0]}")
    if len(text) > max_length:
        raise WorkoutValidationError(f"{names[0]} is too long")
    return text or None


def _number(
    data: Mapping[str, object],
    *names: str,
    minimum: float = 0.0,
    maximum: float,
) -> float | None:
    value = _first(data, *names)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise WorkoutValidationError(f"{names[0]} must be a number") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise WorkoutValidationError(
            f"{names[0]} must be between {minimum:g} and {maximum:g}"
        )
    return result


def _date(value: object, field: str) -> datetime:
    text = str(value).strip()
    if not text:
        raise WorkoutValidationError(f"missing {field}")
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkoutValidationError(f"{field} must be an ISO-8601 date") from exc
    if result.tzinfo is None:
        raise WorkoutValidationError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def parse_workout(
    data: Mapping[str, object], *, now: datetime | None = None,
) -> Workout:
    """Validate one workout dictionary from an iPhone Shortcut."""
    if not isinstance(data, Mapping):
        raise WorkoutValidationError("workout must be an object")
    activity = _text(
        data, "activity", "workout_type", "workoutType", "type",
        required=True, max_length=100,
    )
    start_value = _first(data, "started_at", "start_at", "startDate", "start")
    if start_value is None:
        raise WorkoutValidationError("missing started_at")
    started_at = _date(start_value, "started_at")

    duration_s_value = _number(
        data, "duration_seconds", "duration_s",
        minimum=1, maximum=MAX_WORKOUT_SECONDS,
    )
    duration_minutes = _number(
        data, "duration_minutes", "duration",
        minimum=1 / 60, maximum=MAX_WORKOUT_SECONDS / 60,
    )
    if duration_s_value is None and duration_minutes is not None:
        duration_s_value = duration_minutes * 60

    end_value = _first(data, "ended_at", "end_at", "endDate", "end")
    ended_at = _date(end_value, "ended_at") if end_value is not None else None
    if ended_at is None and duration_s_value is None:
        raise WorkoutValidationError("provide ended_at or duration_minutes")
    if ended_at is None:
        ended_at = started_at + timedelta(seconds=duration_s_value)

    elapsed = (ended_at - started_at).total_seconds()
    if elapsed <= 0 or elapsed > MAX_WORKOUT_SECONDS:
        raise WorkoutValidationError(
            "ended_at must be after started_at and within 7 days"
        )
    duration_s = round(duration_s_value if duration_s_value is not None else elapsed)
    if abs(duration_s - elapsed) > max(300, elapsed * 0.25):
        raise WorkoutValidationError(
            "duration does not reasonably match started_at and ended_at"
        )

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if started_at > current + timedelta(hours=24):
        raise WorkoutValidationError("started_at is too far in the future")

    supplied_id = _text(
        data, "id", "workout_id", "uuid", max_length=200,
    )
    if supplied_id:
        workout_id = hashlib.sha256(
            f"apple:{supplied_id}".encode("utf-8")
        ).hexdigest()
    else:
        identity = "|".join(
            (
                activity.casefold(),
                started_at.isoformat(),
                ended_at.isoformat(),
                str(duration_s),
            )
        )
        workout_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    return Workout(
        workout_id=workout_id,
        activity=activity,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        active_kcal=_number(
            data, "active_kcal", "activeCalories", "energy_kcal",
            maximum=100_000,
        ),
        distance_m=_number(
            data, "distance_m", "distanceMeters", maximum=10_000_000,
        ),
        avg_heart_rate=_number(
            data, "avg_heart_rate", "averageHeartRate",
            maximum=300,
        ),
        elevation_m=_number(
            data, "elevation_m", "elevationGainMeters", maximum=100_000,
        ),
        effort=_number(data, "effort", "difficulty", minimum=1, maximum=10),
        source_name=_text(data, "source_name", "sourceName", max_length=100),
    )


def payload_items(payload: object) -> list[Mapping[str, object]]:
    """Extract a single workout or a replay batch, enforcing a safe size."""
    if not isinstance(payload, Mapping):
        raise WorkoutValidationError("request body must be a JSON object")
    raw = payload.get("workouts")
    if raw is None:
        items: list[object] = [payload]
    elif isinstance(raw, list):
        items = raw
    else:
        raise WorkoutValidationError("workouts must be a list")
    if not items:
        raise WorkoutValidationError("workouts cannot be empty")
    if len(items) > MAX_BATCH_SIZE:
        raise WorkoutValidationError(
            f"workouts cannot contain more than {MAX_BATCH_SIZE} items"
        )
    if not all(isinstance(item, Mapping) for item in items):
        raise WorkoutValidationError("every workout must be an object")
    return items  # type: ignore[return-value]
