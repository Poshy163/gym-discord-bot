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
from datetime import datetime
from typing import Any

from .aliases import canonicalize
from .parser import Lift

LOG = logging.getLogger("gym-bot.hevy")

API_BASE = "https://api.hevyapp.com/v1"
_TIMEOUT = 15


class HevyError(Exception):
    """Generic Hevy API failure."""


class HevyUnavailable(HevyError):
    """Raised when an optional dependency (requests/cryptography) is missing."""


class HevyAuthError(HevyError):
    """Raised when Hevy rejects the API key (401/403)."""


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

def _get(api_key: str, path: str, params: dict | None = None) -> Any:
    if requests is None:
        raise HevyUnavailable("The 'requests' package is required for Hevy.")
    try:
        resp = requests.get(
            f"{API_BASE}{path}",
            headers={"api-key": api_key, "Accept": "application/json"},
            params=params or {},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise HevyError(f"Hevy request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise HevyAuthError("Hevy rejected the API key (401/403).")
    if resp.status_code >= 400:
        raise HevyError(f"Hevy API error {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HevyError("Hevy returned a non-JSON response.") from exc


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
    volume = 0.0
    top: tuple[str, float, int] | None = None
    ex_summaries: list[dict] = []
    for ex in exercises:
        title = (ex.get("title") or "").strip()
        ex_sets = ex.get("sets") or []
        ex_volume = 0.0
        ex_reps = 0
        ex_top: tuple[float, int] | None = None
        for s in ex_sets:
            weight = _as_float(s.get("weight_kg")) or 0.0
            reps = _as_int(s.get("reps")) or 0
            set_count += 1
            if (s.get("type") or "normal") == "warmup":
                warmup_sets += 1
            else:
                working_sets += 1
            total_reps += reps
            ex_reps += reps
            volume += weight * reps
            ex_volume += weight * reps
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
        "exercise_count": len(exercises),
        "set_count": set_count,
        "working_set_count": working_sets,
        "warmup_set_count": warmup_sets,
        "total_reps": total_reps,
        "volume_kg": round(volume),
        "duration_seconds": duration_seconds,
        "top": (
            {"title": top[0], "weight_kg": top[1], "reps": top[2]}
            if top else None
        ),
        "exercises": ex_summaries,
        "start_time": workout.get("start_time") or workout.get("created_at"),
        "end_time": workout.get("end_time"),
    }
