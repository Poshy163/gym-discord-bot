"""Hevy (hevyapp.com) integration client.

Unlike Strava (OAuth + webhooks), Hevy exposes a simple **per-user API key** REST
API: the member generates a key in the Hevy app (Settings → API — requires Hevy
Pro) and the bot calls ``https://api.hevyapp.com/v1`` with an ``api-key`` header.
There's no push, so the bot **polls** each linked member's recent workouts and
both imports them as lifts and posts a feed embed.

The API key is stored **encrypted at rest** (Fernet) — the plaintext is never
persisted. This module is import-safe even when ``requests`` / ``cryptography``
aren't installed, so the bot still boots without the Hevy feature.

The HTTP calls are synchronous (``requests``); the bot runs them in an executor
so the event loop isn't blocked. The ``workout_to_lifts`` / ``summarize_workout``
mappers are pure (no network) and unit-tested.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

from .aliases import canonicalize
from .parser import Lift

LOG = logging.getLogger("gym-bot.hevy")

API_BASE = "https://api.hevyapp.com/v1"
_TIMEOUT = 15

#: Attempts for a 429. Hevy rate-limits per key, and a single sync can burst a
#: dozen requests — the workout page, the exercise-template catalogue, then the
#: body-measurement pages, one per ten entries. Without a retry the whole sync is
#: abandoned on the first refusal and its workouts wait for the next poll.
_RATE_LIMIT_ATTEMPTS = 4

#: Seconds to wait before retrying a 429 when Hevy sends no ``Retry-After``,
#: doubling each attempt (2, 4, 8).
_RATE_LIMIT_BACKOFF = 2.0


class HevyError(Exception):
    """Generic Hevy API failure."""


class HevyUnavailable(HevyError):
    """Raised when an optional dependency (requests/cryptography) is missing."""


class HevyAuthError(HevyError):
    """Raised when Hevy rejects the API key (401/403)."""


class HevyNotFound(HevyError):
    """Raised on a 404 — the resource doesn't exist yet.

    Distinct from :class:`HevyError` because "no measurement for that date" is a
    normal, expected answer on the write-back path, not a failure."""


class HevyRateLimited(HevyError):
    """Raised on a 429. ``retry_after`` carries Hevy's own hint when it sent one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HevyConflict(HevyError):
    """Raised on a 409 — the resource already exists.

    ``POST /body_measurements`` answers 409 when the account already has an entry
    for that date, which is the signal to switch from create to merge-and-update.
    """


# Optional deps — imported lazily so the bot can boot without them.
try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:  # pragma: no cover - trivial import guard
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]


def available() -> bool:
    """True when the optional ``requests`` dep is importable."""
    return requests is not None


# ---------------------------------------------------------------------------
# API-key encryption (mirrors the Strava/Revo Fernet scheme)
# ---------------------------------------------------------------------------

_FERNET_ENVS = ("HEVY_FERNET_KEY", "STRAVA_FERNET_KEY", "REVO_FERNET_KEY")


def _fernet() -> "Fernet":
    if Fernet is None:
        raise HevyUnavailable(
            "The 'cryptography' package is required to store Hevy API keys."
        )
    key = ""
    for env in _FERNET_ENVS:
        key = os.environ.get(env, "").strip()
        if key:
            break
    if not key:
        raise HevyUnavailable(
            "Set $HEVY_FERNET_KEY (or reuse $STRAVA_FERNET_KEY / $REVO_FERNET_KEY) "
            "to a Fernet key (generate one with `python -c 'from cryptography."
            "fernet import Fernet; print(Fernet.generate_key().decode())'`)."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # pragma: no cover - bad key shape
        raise HevyUnavailable(f"Invalid Fernet key: {exc}") from exc


def encrypt_key(plaintext: str) -> str:
    """Encrypt a Hevy API key for at-rest storage. Returns a urlsafe string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_key(token: str) -> str:
    """Inverse of :func:`encrypt_key`."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - corrupted DB row
        raise HevyUnavailable("Stored Hevy API key is unreadable.") from exc


def fernet_ready() -> bool:
    """True when a Fernet key is configured (so linking can store the key)."""
    if Fernet is None:
        return False
    return any(os.environ.get(env, "").strip() for env in _FERNET_ENVS)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _retry_after(resp: Any) -> float | None:
    """Seconds Hevy asked us to wait, from ``Retry-After``. Pure-ish helper."""
    raw = ""
    try:
        raw = (resp.headers or {}).get("Retry-After", "")
    except Exception:  # pragma: no cover - exotic response objects
        return None
    try:
        wait = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # Ignore a nonsensical or punitive value rather than stalling a poll on it.
    return wait if 0 < wait <= 60 else None


def _request(
    api_key: str, method: str, path: str,
    params: dict | None = None, payload: dict | None = None,
) -> Any:
    """Single choke point for every Hevy call — auth header, timeout, status map.

    Every endpoint goes through here so the api-key header, the timeout and the
    status-code taxonomy are defined once. 404 and 409 get their own exceptions
    because the body-measurement write-back treats both as control flow rather
    than as errors (nothing recorded for that date yet / something already is).
    """
    if requests is None:
        raise HevyUnavailable("The 'requests' package is required for Hevy.")
    headers = {"api-key": api_key, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(
            method.upper(),
            f"{API_BASE}{path}",
            headers=headers,
            params=params or {},
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise HevyError(f"Hevy request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise HevyAuthError("Hevy rejected the API key (401/403).")
    if resp.status_code == 429:
        raise HevyRateLimited(
            "Hevy is rate-limiting this API key (429).", _retry_after(resp),
        )
    if resp.status_code == 404:
        raise HevyNotFound(f"Hevy has no resource at {path}.")
    if resp.status_code == 409:
        raise HevyConflict(f"Hevy already has a resource at {path}.")
    if resp.status_code >= 400:
        raise HevyError(f"Hevy API error {resp.status_code}: {resp.text[:200]}")
    if not (resp.text or "").strip():
        return {}  # 200 with an empty body (some writes answer this way)
    try:
        return resp.json()
    except ValueError as exc:
        raise HevyError("Hevy returned a non-JSON response.") from exc


def _request_retrying(
    api_key: str, method: str, path: str,
    params: dict | None = None, payload: dict | None = None,
) -> Any:
    """:func:`_request`, retrying a 429 with Hevy's own ``Retry-After``.

    Only 429 is retried. A timeout or a 5xx could mean the write landed and the
    response was lost, and body measurements are not idempotent enough to replay
    blindly — a rate-limit refusal is the one failure Hevy guarantees did
    nothing.
    """
    delay = _RATE_LIMIT_BACKOFF
    for attempt in range(_RATE_LIMIT_ATTEMPTS):
        try:
            return _request(api_key, method, path, params=params, payload=payload)
        except HevyRateLimited as exc:
            if attempt == _RATE_LIMIT_ATTEMPTS - 1:
                raise
            wait = exc.retry_after if exc.retry_after is not None else delay
            LOG.info(
                "Hevy: rate-limited on %s, retrying in %.1fs (attempt %d/%d)",
                path, wait, attempt + 1, _RATE_LIMIT_ATTEMPTS,
            )
            time.sleep(wait)
            delay *= 2
    raise HevyRateLimited("Hevy is rate-limiting this API key (429).")


def _get(api_key: str, path: str, params: dict | None = None) -> Any:
    return _request_retrying(api_key, "GET", path, params=params)


def verify_key(api_key: str) -> dict:
    """Validate an API key by fetching the workout count.

    Returns ``{"ok": True, "count": <int>}``. Raises :class:`HevyAuthError` if
    the key is rejected.
    """
    data = _get(api_key, "/workouts/count")
    count = 0
    if isinstance(data, dict):
        count = data.get("workout_count", data.get("count", 0)) or 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    return {"ok": True, "count": count}


def fetch_workouts(api_key: str, page: int = 1, page_size: int = 10) -> list[dict]:
    """Most recent workouts (one page). Hevy returns newest-first."""
    data = _get(api_key, "/workouts", {"page": page, "pageSize": page_size})
    if isinstance(data, dict):
        return data.get("workouts", []) or []
    if isinstance(data, list):
        return data
    return []


def fetch_recent_workouts(api_key: str, limit: int = 50) -> list[dict]:
    """Most recent up to ``limit`` workouts, paging the API (newest-first).

    Hevy caps ``pageSize`` at 10 for ``/workouts``, so this walks pages until it
    has ``limit`` workouts or runs out. Used for the backfill on first link."""
    page_size = 10
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        batch = fetch_workouts(api_key, page=page, page_size=page_size)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break  # last page
        page += 1
        if page > 50:  # hard safety cap (500 workouts) against a bad loop
            break
    return out[:limit]


def fetch_user_info(api_key: str) -> dict:
    """The account's Hevy profile — ``{"id", "name", "url"}``.

    Used to label the feed with the member's real Hevy handle and to link their
    public profile. The payload is wrapped in a ``data`` envelope, unlike every
    other endpoint."""
    data = _get(api_key, "/user/info")
    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        inner = data if isinstance(data, dict) else {}
    return {
        "id": str(inner.get("id") or ""),
        "name": str(inner.get("name") or "").strip(),
        "url": str(inner.get("url") or "").strip(),
    }


def fetch_exercise_templates(api_key: str, limit: int = 500) -> list[dict]:
    """Every exercise template on the account (built-ins plus the user's customs).

    Templates carry the muscle group and equipment category that the workout
    payload itself omits — a workout only names the exercise. This endpoint
    allows ``pageSize=100`` (ten times the rest of the API), so the whole
    catalogue is a handful of calls."""
    page_size = 100
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        data = _get(
            api_key, "/exercise_templates",
            {"page": page, "pageSize": page_size},
        )
        batch = (
            data.get("exercise_templates", []) or []
            if isinstance(data, dict) else []
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break  # last page
        page += 1
        if page > 20:  # hard safety cap (2000 templates) against a bad loop
            break
    return out[:limit]


def fetch_body_measurement(api_key: str, date: str) -> dict | None:
    """The measurement recorded for ``date`` (YYYY-MM-DD), or None if there is none.

    None rather than an exception for the 404: "nothing logged that day" is the
    common case on the write-back path, not a failure."""
    try:
        data = _get(api_key, f"/body_measurements/{date}")
    except HevyNotFound:
        return None
    return data if isinstance(data, dict) else None


def fetch_body_measurements(api_key: str, limit: int = 200) -> list[dict]:
    """The account's recent body measurements, newest-first.

    Pages at Hevy's documented maximum of 10 per request. ``limit`` exists
    because a member who has weighed in daily for years would otherwise cost
    hundreds of round trips on a one-time reconcile; capping it means an old
    account merges its recent history rather than all of it.
    """
    page_size = 10
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        data = _get(
            api_key, "/body_measurements", {"page": page, "pageSize": page_size},
        )
        batch = (
            data.get("body_measurements", []) or []
            if isinstance(data, dict) else []
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break  # last page
        page += 1
        if page > 40:  # hard safety cap (400 entries) against a bad loop
            break
    return out[:limit]


def metrics_from_measurement(measurement: dict) -> dict:
    """Inverse of :func:`measurement_fields` — Hevy's fields as bot metrics.

    Returns the ``{metric_key: (value, unit)}`` shape ``db.add_body_metrics``
    expects, excluding weight (which is a weigh-in, not a composition metric,
    and goes through ``set_bodyweight``). Values outside
    :data:`_MEASUREMENT_BOUNDS` are dropped, so a bad entry in Hevy cannot ride
    back into the bot's own history.
    """
    units = {"fat_percent": "%", "lean_mass_kg": "kg"}
    out: dict[str, tuple[float, str]] = {}
    for metric_key, field in MEASUREMENT_FIELDS.items():
        if metric_key == "weight":
            continue
        value = _as_float((measurement or {}).get(field))
        if value is None:
            continue
        low, high = _MEASUREMENT_BOUNDS[field]
        if not (low <= value <= high):
            continue
        out[metric_key] = (value, units.get(field, ""))
    return out


def measurement_weight(measurement: dict) -> float | None:
    """The weigh-in in a Hevy measurement, or None if it has none or it's daft.

    Hevy lets an entry record only circumferences, so ``weight_kg`` is genuinely
    optional — such an entry describes no weigh-in and must not become one."""
    value = _as_float((measurement or {}).get("weight_kg"))
    if value is None:
        return None
    low, high = _MEASUREMENT_BOUNDS["weight_kg"]
    return value if low <= value <= high else None


def create_body_measurement(api_key: str, payload: dict) -> Any:
    """POST a new body measurement. Raises :class:`HevyConflict` if the date is taken."""
    return _request_retrying(
        api_key, "POST", "/body_measurements", payload=payload,
    )


def update_body_measurement(api_key: str, date: str, payload: dict) -> Any:
    """PUT an existing body measurement.

    Hevy overwrites **every** field on a PUT — anything omitted is set to null —
    so callers must send a complete object. :func:`merge_measurement` builds one.
    """
    return _request_retrying(
        api_key, "PUT", f"/body_measurements/{date}", payload=payload,
    )


def push_body_measurement(api_key: str, date: str, fields: dict) -> str:
    """Write a weigh-in to the member's Hevy body measurements. Idempotent.

    Returns ``"created"``, ``"updated"``, or ``"unchanged"``.

    Create-then-merge rather than a blind PUT, because Hevy's PUT nulls every
    field the payload omits. The bot only ever knows weight, body fat and lean
    mass, so a blind PUT would silently wipe the tape-measure fields (waist,
    chest, biceps, ...) that the member entered by hand in the Hevy app. On a
    409 we therefore re-read the day's entry and merge our values over it,
    leaving every field we have no opinion about exactly as it was.

    ``unchanged`` means Hevy already held these values, so the PUT was skipped —
    a re-import or a re-sent weigh-in costs one request and writes nothing.
    """
    if not fields:
        return "unchanged"
    try:
        create_body_measurement(api_key, {"date": date, **fields})
        return "created"
    except HevyConflict:
        pass
    existing = fetch_body_measurement(api_key, date) or {}
    merged = merge_measurement(existing, fields)
    if merged is None:
        return "unchanged"
    update_body_measurement(api_key, date, merged)
    return "updated"


# ---------------------------------------------------------------------------
# Pure mappers (no network — unit-tested)
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Body-measurement fields Hevy accepts that the bot can actually supply, mapped
#: from the metric keys in :data:`app.ha_client.METRICS`. Hevy also stores 14
#: circumference fields (waist, chest, biceps, ...) — no source the bot reads
#: reports those, so they are deliberately absent here and are preserved rather
#: than overwritten on update (see :func:`merge_measurement`).
MEASUREMENT_FIELDS: dict[str, str] = {
    "weight": "weight_kg",
    "body_fat_pct": "fat_percent",
    "fat_free_mass_kg": "lean_mass_kg",
}

#: Sanity bounds per Hevy field. A smart scale that glitches (a 6553.5 kg BLE
#: register is a real failure mode) must not be mirrored into the member's Hevy
#: account, where the bot cannot easily retract it.
_MEASUREMENT_BOUNDS: dict[str, tuple[float, float]] = {
    "weight_kg": (1.0, 500.0),
    "fat_percent": (1.0, 80.0),
    "lean_mass_kg": (1.0, 500.0),
}


def measurement_fields(
    weight_kg: float | None = None, metrics: dict | None = None,
) -> dict:
    """Build the Hevy body-measurement payload for one weigh-in.

    ``metrics`` is the bot's ``{metric_key: {"value": float, ...}}`` shape as
    produced by the Home Assistant scale import, so a member whose scale reports
    body fat and lean mass mirrors all three numbers into Hevy while a plain
    ``bw 82`` mirrors only the weight. Values outside
    :data:`_MEASUREMENT_BOUNDS` are dropped rather than clamped — a glitched
    reading should be absent from Hevy, not silently rewritten to a plausible
    lie.
    """
    raw: dict[str, Any] = {}
    if weight_kg is not None:
        raw["weight_kg"] = weight_kg
    for metric_key, field in MEASUREMENT_FIELDS.items():
        if field in raw:
            continue
        entry = (metrics or {}).get(metric_key)
        if isinstance(entry, dict):
            raw[field] = entry.get("value")
        elif entry is not None:
            raw[field] = entry
    out: dict[str, float] = {}
    for field, value in raw.items():
        number = _as_float(value)
        if number is None:
            continue
        low, high = _MEASUREMENT_BOUNDS[field]
        if not (low <= number <= high):
            LOG.warning(
                "Hevy: dropping out-of-range %s=%s from the measurement push",
                field, number,
            )
            continue
        out[field] = round(number, 2)
    return out


def merge_measurement(existing: dict, fields: dict) -> dict | None:
    """Overlay ``fields`` on an existing measurement, for a full-object PUT.

    Returns None when Hevy already holds exactly these values, so the caller can
    skip a pointless write. ``date`` is dropped because it lives in the URL, and
    every other field Hevy returned is carried through untouched — that is what
    stops a weight push from nulling the member's hand-entered circumferences.
    """
    merged = {
        k: v for k, v in (existing or {}).items()
        if k != "date" and v is not None
    }
    changed = False
    for field, value in (fields or {}).items():
        before = _as_float(merged.get(field))
        after = _as_float(value)
        if before is None or after is None or abs(before - after) >= 0.005:
            changed = True
        merged[field] = value
    return merged if changed else None


def workout_to_lifts(workout: dict) -> list[Lift]:
    """Map a Hevy workout's exercises/sets to canonical :class:`Lift` rows.

    One ``Lift`` per *weighted* working set (positive ``weight_kg``); sets with
    no weight (bodyweight-only or cardio) are skipped so they don't pollute the
    lift log. Exercise titles are run through :func:`aliases.canonicalize` so a
    Hevy "Bench Press (Barbell)" lands on the same equipment as a chat-logged
    "bench".
    """
    out: list[Lift] = []
    for ex in workout.get("exercises") or []:
        title = (ex.get("title") or "").strip()
        if not title:
            continue
        equipment = canonicalize(title)
        for s in ex.get("sets") or []:
            weight = _as_float(s.get("weight_kg"))
            if weight is None or weight <= 0:
                continue
            reps = _as_int(s.get("reps"))
            out.append(Lift(
                equipment=equipment,
                weight_kg=weight,
                reps=reps,
                raw=f"hevy:{title}",
                confident=True,
                structured=True,
            ))
    return out


def _iso_dt(value: Any) -> datetime | None:
    """Parse a Hevy ISO-8601 timestamp to a datetime, or None (pure helper)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def summarize_workout(workout: dict) -> dict:
    """Full summary for the Discord feed embed.

    Aggregates the whole workout: exercise/set counts (working vs warmup), total
    reps, total volume (kg = Σ weight×reps), elapsed duration, the single
    heaviest set, and a per-exercise breakdown (sets + top set + volume). All
    fields are derived purely from the workout dict (no network) so this stays
    unit-testable. ``set_count`` counts every logged set; ``working_set_count``
    excludes warmups.
    """
    exercises = workout.get("exercises") or []
    set_count = working_sets = warmup_sets = total_reps = 0
    drop_sets = failure_sets = 0
    volume = 0.0
    distance_m = 0.0
    active_seconds = 0
    bodyweight_reps = 0
    best_rpe: float | None = None
    top: tuple[str, float, int] | None = None
    ex_summaries: list[dict] = []
    for ex in exercises:
        title = (ex.get("title") or "").strip()
        ex_sets = ex.get("sets") or []
        ex_volume = 0.0
        ex_reps = 0
        ex_distance = 0.0
        ex_seconds = 0
        ex_rpe: float | None = None
        ex_top: tuple[float, int] | None = None
        for s in ex_sets:
            weight = _as_float(s.get("weight_kg")) or 0.0
            reps = _as_int(s.get("reps")) or 0
            set_type = (s.get("type") or "normal").strip().lower()
            set_count += 1
            if set_type == "warmup":
                warmup_sets += 1
            else:
                working_sets += 1
                if set_type == "dropset":
                    drop_sets += 1
                elif set_type == "failure":
                    failure_sets += 1
            total_reps += reps
            ex_reps += reps
            if weight <= 0:
                bodyweight_reps += reps
            volume += weight * reps
            ex_volume += weight * reps
            metres = _as_float(s.get("distance_meters")) or 0.0
            if metres > 0:
                distance_m += metres
                ex_distance += metres
            seconds = _as_int(s.get("duration_seconds")) or 0
            if seconds > 0:
                active_seconds += seconds
                ex_seconds += seconds
            rpe = _as_float(s.get("rpe"))
            if rpe is not None:
                if best_rpe is None or rpe > best_rpe:
                    best_rpe = rpe
                if ex_rpe is None or rpe > ex_rpe:
                    ex_rpe = rpe
            if weight > 0 and (top is None or weight > top[1]):
                top = (title, weight, reps)
            if weight > 0 and (ex_top is None or weight > ex_top[0]):
                ex_top = (weight, reps)
        ex_summaries.append({
            "title": title or "Exercise",
            "sets": len(ex_sets),
            "reps": ex_reps,
            "best_weight_kg": ex_top[0] if ex_top else None,
            "best_reps": ex_top[1] if ex_top else None,
            "volume_kg": round(ex_volume),
            "template_id": str(ex.get("exercise_template_id") or "") or None,
            "notes": (ex.get("notes") or "").strip() or None,
            "rpe": ex_rpe,
            "distance_m": round(ex_distance) if ex_distance else None,
            "duration_seconds": ex_seconds or None,
            "superset_id": ex.get("supersets_id"),
        })
    start = _iso_dt(workout.get("start_time") or workout.get("created_at"))
    end = _iso_dt(workout.get("end_time"))
    duration_seconds = (
        int((end - start).total_seconds())
        if start and end and end > start else None
    )
    return {
        "id": str(workout.get("id") or ""),
        "title": (workout.get("title") or "Workout").strip() or "Workout",
        "description": (workout.get("description") or "").strip() or None,
        "routine_id": str(workout.get("routine_id") or "") or None,
        "exercise_count": len(exercises),
        "set_count": set_count,
        "working_set_count": working_sets,
        "warmup_set_count": warmup_sets,
        "dropset_count": drop_sets,
        "failure_set_count": failure_sets,
        "total_reps": total_reps,
        "bodyweight_reps": bodyweight_reps,
        "volume_kg": round(volume),
        "distance_m": round(distance_m) if distance_m else None,
        "active_seconds": active_seconds or None,
        "best_rpe": best_rpe,
        "duration_seconds": duration_seconds,
        # False for a pure calisthenics/cardio session: nothing in it becomes a
        # lift, so the feed has to describe it from these totals alone.
        "has_lifts": volume > 0,
        "top": (
            {"title": top[0], "weight_kg": top[1], "reps": top[2]}
            if top else None
        ),
        "exercises": ex_summaries,
        "start_time": workout.get("start_time") or workout.get("created_at"),
        "end_time": workout.get("end_time"),
        "updated_at": workout.get("updated_at"),
    }

#: Hevy's MuscleGroup vocabulary, prettified for display. Anything absent (a new
#: group Hevy adds later) falls back to a title-cased version of the raw value,
#: so an unknown group degrades to a readable label instead of vanishing.
MUSCLE_LABELS: dict[str, str] = {
    "abdominals": "Abs", "shoulders": "Shoulders", "biceps": "Biceps",
    "triceps": "Triceps", "forearms": "Forearms", "quadriceps": "Quads",
    "hamstrings": "Hamstrings", "calves": "Calves", "glutes": "Glutes",
    "abductors": "Abductors", "adductors": "Adductors", "lats": "Lats",
    "upper_back": "Upper back", "traps": "Traps", "lower_back": "Lower back",
    "chest": "Chest", "cardio": "Cardio", "neck": "Neck",
    "full_body": "Full body", "other": "Other",
}


def muscle_label(group: str) -> str:
    """Display name for a Hevy muscle-group key."""
    key = (group or "").strip().lower()
    return MUSCLE_LABELS.get(key) or key.replace("_", " ").capitalize() or "Other"


def muscle_split(summary: dict, templates: dict) -> list[tuple[str, int]]:
    """Volume per primary muscle group, heaviest first.

    ``templates`` maps ``exercise_template_id`` to a template dict; exercises
    whose template isn't in the map are skipped rather than bucketed as "other",
    so a partially-loaded catalogue under-reports instead of lying.

    This is deliberately **display-only** and keyed off the template id. Muscle
    groups are never fed back into :func:`app.aliases.canonicalize`: equipment
    identity drives PRs, leaderboards and a boot-time re-canonicalisation pass
    that deletes colliding rows, and none of that should shift because Hevy
    re-tagged an exercise.
    """
    totals: dict[str, float] = {}
    for ex in summary.get("exercises") or []:
        template = templates.get(ex.get("template_id") or "")
        if not isinstance(template, dict):
            continue
        group = (template.get("primary_muscle_group") or "").strip().lower()
        if not group:
            continue
        totals[group] = totals.get(group, 0.0) + float(ex.get("volume_kg") or 0)
    ranked = sorted(
        ((g, round(v)) for g, v in totals.items() if v > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return ranked


def index_templates(templates: list[dict]) -> dict[str, dict]:
    """Index a template list by id, keeping only the fields the bot displays."""
    out: dict[str, dict] = {}
    for template in templates or []:
        tid = str(template.get("id") or "")
        if not tid:
            continue
        out[tid] = {
            "title": (template.get("title") or "").strip(),
            "primary_muscle_group": (
                template.get("primary_muscle_group") or ""
            ).strip().lower(),
            "equipment_category": (
                template.get("equipment_category") or ""
            ).strip().lower(),
            "is_custom": bool(template.get("is_custom")),
        }
    return out

