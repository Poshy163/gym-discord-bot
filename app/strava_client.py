"""Strava API client used by the bot.

Unlike the Revo portal (HTML scraping with a username/password), Strava exposes
a real OAuth2 JSON API. The flow is:

1. The user authorises the bot in the browser (``/strava_link`` hands them an
   authorize URL). Strava redirects back to our callback with a short-lived
   ``code``.
2. We exchange that ``code`` for an ``access_token`` (≈6h lifetime) and a
   long-lived ``refresh_token``. Both are stored **encrypted at rest** (Fernet),
   mirroring :mod:`app.revo_client`.
3. New-activity notifications arrive in real time via Strava's *webhook* push
   subscription — Strava POSTs a tiny event to our public callback the instant a
   workout is saved. We then fetch the full activity and post it to Discord.

This module is import-safe even if ``requests`` / ``cryptography`` aren't
installed — the bot boots without Strava and the commands/webhook just report
"unavailable". It deliberately imports **no** ``discord`` symbols: it returns
plain dataclasses and the bot layer builds the embeds.

See ``docs/STRAVA.md`` for the host-side setup (registering the API app,
configuring the public callback URL, creating the push subscription).
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

LOG = logging.getLogger("gymbot.strava")

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"
# Strava is migrating its API host (www.strava.com/api/v3 → www.api-v3.strava.com)
# over the 2026-06-01 → 2027-06-01 window. Reading the base from an env var means
# the eventual cut-over is a one-env-var change (set STRAVA_API_BASE) with no code
# deploy — the default preserves today's behaviour exactly. Everything below
# (SUBSCRIPTION_URL and the f"{API_BASE}/..." call sites) derives from this, so a
# single override moves them all. Auth for activity/athlete calls rides in the
# Authorization: Bearer header, not the host, so the switch is host-only.
API_BASE = os.getenv("STRAVA_API_BASE", "https://www.strava.com/api/v3").rstrip("/")
SUBSCRIPTION_URL = f"{API_BASE}/push_subscriptions"

# read = public profile; activity:read = the athlete's activities (incl. the
# ones webhooks fire for). activity:read_all would also expose private/followers
# -only activities — we keep to the narrower scope by default.
DEFAULT_SCOPE = "read,activity:read"

USER_AGENT = "gym-discord-bot/0.1 (+https://github.com/Poshy163/gym-discord-bot)"
REQUEST_TIMEOUT = 20

# Refresh a little before the real expiry so an in-flight request never races
# the boundary.
_EXPIRY_SKEW_SECONDS = 120


class StravaUnavailable(RuntimeError):
    """Raised when an optional dependency is missing or config is incomplete."""


class StravaAuthError(RuntimeError):
    """Raised when an OAuth exchange/refresh fails (revoked, bad code, etc.).

    ``status_code`` carries the HTTP status when the error came from a non-2xx
    response, so callers can tell a *permanent* rejection (401/403 — e.g. the
    API app is Inactive) apart from a *transient* one (429 rate-limit / 5xx
    outage) that is worth retrying. ``None`` when not HTTP-derived.
    """

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


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
# Config / credential encryption
# ---------------------------------------------------------------------------

# A single Fernet key can serve both integrations: prefer a Strava-specific key
# but fall back to the Revo one so hosts only have to manage one secret.
_FERNET_ENVS = ("STRAVA_FERNET_KEY", "REVO_FERNET_KEY")


def _fernet() -> "Fernet":
    if Fernet is None:
        raise StravaUnavailable(
            "The 'cryptography' package is required to store Strava tokens."
        )
    key = ""
    for env in _FERNET_ENVS:
        key = os.environ.get(env, "").strip()
        if key:
            break
    if not key:
        raise StravaUnavailable(
            "Set $STRAVA_FERNET_KEY (or $REVO_FERNET_KEY) to a Fernet key "
            "(generate one with `python -c 'from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())'`)."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # pragma: no cover - bad key shape
        raise StravaUnavailable(f"Invalid Fernet key: {exc}") from exc


def encrypt_token(plaintext: str) -> str:
    """Encrypt an OAuth token for at-rest storage. Returns a urlsafe string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(token: str) -> str:
    """Inverse of :func:`encrypt_token`."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - corrupted DB row
        raise StravaUnavailable("Stored Strava token is unreadable.") from exc


@dataclass(frozen=True)
class StravaConfig:
    """Host-supplied Strava app credentials + public callback base."""

    client_id: str
    client_secret: str
    redirect_uri: str          # public https URL of /strava/callback
    webhook_callback_url: str  # public https URL of /strava/webhook
    verify_token: str          # shared secret echoed during subscription setup

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def config_from_env() -> StravaConfig:
    """Build a :class:`StravaConfig` from the ``STRAVA_*`` env vars.

    ``STRAVA_PUBLIC_URL`` is the externally reachable base (e.g.
    ``https://bot.example.com``); the callback/webhook paths are derived from
    it. The redirect/webhook URLs can also be overridden explicitly.
    """
    base = os.environ.get("STRAVA_PUBLIC_URL", "").strip().rstrip("/")
    redirect = os.environ.get("STRAVA_REDIRECT_URI", "").strip() or (
        f"{base}/strava/callback" if base else ""
    )
    webhook = os.environ.get("STRAVA_WEBHOOK_CALLBACK_URL", "").strip() or (
        f"{base}/strava/webhook" if base else ""
    )
    return StravaConfig(
        client_id=os.environ.get("STRAVA_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("STRAVA_CLIENT_SECRET", "").strip(),
        redirect_uri=redirect,
        webhook_callback_url=webhook,
        verify_token=os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "gymbot").strip(),
    )


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenSet:
    """An OAuth token triple. ``expires_at`` is epoch seconds (Strava's own)."""

    access_token: str
    refresh_token: str
    expires_at: int

    def is_expired(self, *, skew: int = _EXPIRY_SKEW_SECONDS) -> bool:
        return time.time() >= (self.expires_at - skew)


def build_authorize_url(
    cfg: StravaConfig, state: str, scope: str = DEFAULT_SCOPE
) -> str:
    """Return the Strava authorize URL the user clicks to grant access.

    ``state`` is an opaque value we round-trip to tie the redirect back to the
    Discord user who initiated the link (see the pending-auth table).
    """
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": scope,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _require_requests() -> None:
    if requests is None:
        raise StravaUnavailable(
            "The 'requests' package is required for the Strava client."
        )


def _token_request(data: dict[str, Any]) -> TokenSet:
    _require_requests()
    r = requests.post(TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise StravaAuthError(
            f"Strava token endpoint returned {r.status_code}: {r.text[:200]}"
        )
    body = r.json()
    try:
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=int(body["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StravaAuthError(f"Malformed token response: {body!r}") from exc


def exchange_code(cfg: StravaConfig, code: str) -> tuple[TokenSet, dict[str, Any]]:
    """Exchange an authorization ``code`` for tokens.

    Returns ``(tokens, athlete)`` — Strava includes a summary athlete object on
    the initial exchange, which we use to capture the athlete id + display name
    without a second call.
    """
    _require_requests()
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        raise StravaAuthError(
            f"Code exchange failed ({r.status_code}): {r.text[:200]}"
        )
    body = r.json()
    try:
        tokens = TokenSet(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=int(body["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StravaAuthError(f"Malformed exchange response: {body!r}") from exc
    athlete = body.get("athlete") or {}
    return tokens, athlete


def refresh_tokens(cfg: StravaConfig, refresh_token: str) -> TokenSet:
    """Use a refresh token to mint a fresh access token.

    Strava *rotates* refresh tokens, so callers must persist the returned
    ``refresh_token`` (it may differ from the one passed in).
    """
    return _token_request(
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


def deauthorize(access_token: str) -> None:
    """Best-effort revoke of an access token (used on /strava_unlink)."""
    if requests is None:
        return
    try:
        requests.post(
            DEAUTHORIZE_URL,
            data={"access_token": access_token},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:  # pragma: no cover - best effort
        LOG.warning("Strava deauthorize failed", exc_info=True)


# ---------------------------------------------------------------------------
# Activity fetch + parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StravaActivity:
    id: int
    athlete_id: Optional[int]
    name: str
    sport_type: str            # e.g. "Run", "Ride", "WeightTraining"
    distance_m: float
    moving_time_s: int
    elapsed_time_s: int
    total_elevation_gain_m: float
    average_speed_ms: float    # metres/second
    average_heartrate: Optional[float]
    max_heartrate: Optional[float]
    calories: Optional[float]
    suffer_score: Optional[float]
    start_date_local: str      # ISO-8601 as Strava reports it
    private: bool
    url: str
    map_polyline: str          # Google-encoded route polyline ("" if none)
    photo_url: Optional[str]   # primary activity photo, if the athlete added one
    # Richer detail (mostly present on the detailed/webhook payload only).
    start_date: str = ""       # UTC ISO-8601, used for a "when" timestamp
    description: Optional[str] = None    # the athlete's own caption text
    gear_name: Optional[str] = None      # bike/shoes name
    max_speed_ms: float = 0.0
    average_watts: Optional[float] = None
    kilojoules: Optional[float] = None
    average_cadence: Optional[float] = None
    average_temp: Optional[float] = None  # °C
    pr_count: int = 0
    achievement_count: int = 0
    kudos_count: int = 0


def _extract_photo_url(data: dict[str, Any]) -> Optional[str]:
    """Largest available primary-photo URL, or None.

    ``photos.primary.urls`` is a ``{size_str: url}`` map (e.g. ``{"100": ...,
    "600": ...}``); we pick the biggest size.
    """
    primary = ((data.get("photos") or {}).get("primary")) or {}
    urls = primary.get("urls") or {}
    if not isinstance(urls, dict) or not urls:
        return None
    try:
        return max(urls.items(), key=lambda kv: int(kv[0]))[1]
    except (ValueError, TypeError):
        return next(iter(urls.values()), None)


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google-encoded polyline into ``[(lat, lon), ...]``.

    Strava encodes activity routes with the standard Google polyline algorithm
    (precision 5). Returns an empty list for falsy/garbled input.
    """
    coords: list[tuple[float, float]] = []
    index = lat = lng = 0
    length = len(encoded or "")
    try:
        while index < length:
            for is_lng in (False, True):
                result = 1
                shift = 0
                while True:
                    b = ord(encoded[index]) - 63 - 1
                    index += 1
                    result += b << shift
                    shift += 5
                    if b < 0x1F:
                        break
                delta = (~result >> 1) if (result & 1) else (result >> 1)
                if is_lng:
                    lng += delta
                else:
                    lat += delta
            coords.append((lat * 1e-5, lng * 1e-5))
    except IndexError:  # truncated/garbled string — return what we decoded
        return coords
    return coords


def _opt_float(value: Any) -> Optional[float]:
    """Coerce a value to float, or None if absent/non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def start_unix(activity: "StravaActivity") -> Optional[int]:
    """Epoch seconds for the activity start, for a Discord ``<t:..>`` stamp."""
    raw = activity.start_date or activity.start_date_local
    if not raw:
        return None
    try:
        # Strava uses a trailing "Z"; fromisoformat needs an explicit offset.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp())


def parse_activity(data: dict[str, Any]) -> StravaActivity:
    """Build a :class:`StravaActivity` from a Strava activity JSON object."""
    aid = int(data.get("id", 0) or 0)
    athlete = data.get("athlete") or {}
    amap = data.get("map") or {}
    return StravaActivity(
        id=aid,
        athlete_id=int(athlete["id"]) if athlete.get("id") is not None else None,
        name=str(data.get("name") or "Workout"),
        sport_type=str(data.get("sport_type") or data.get("type") or "Workout"),
        distance_m=float(data.get("distance") or 0.0),
        moving_time_s=int(data.get("moving_time") or 0),
        elapsed_time_s=int(data.get("elapsed_time") or 0),
        total_elevation_gain_m=float(data.get("total_elevation_gain") or 0.0),
        average_speed_ms=float(data.get("average_speed") or 0.0),
        average_heartrate=(
            float(data["average_heartrate"])
            if data.get("average_heartrate") is not None
            else None
        ),
        max_heartrate=(
            float(data["max_heartrate"])
            if data.get("max_heartrate") is not None
            else None
        ),
        calories=(
            float(data["calories"]) if data.get("calories") is not None else None
        ),
        suffer_score=(
            float(data["suffer_score"])
            if data.get("suffer_score") is not None
            else None
        ),
        start_date_local=str(data.get("start_date_local") or ""),
        private=bool(data.get("private", False)),
        url=f"https://www.strava.com/activities/{aid}" if aid else "",
        map_polyline=str(amap.get("polyline") or amap.get("summary_polyline") or ""),
        photo_url=_extract_photo_url(data),
        start_date=str(data.get("start_date") or ""),
        description=(str(data["description"]) if data.get("description") else None),
        gear_name=((data.get("gear") or {}).get("name") or None),
        max_speed_ms=float(data.get("max_speed") or 0.0),
        average_watts=_opt_float(data.get("average_watts")),
        kilojoules=_opt_float(data.get("kilojoules")),
        average_cadence=_opt_float(data.get("average_cadence")),
        average_temp=_opt_float(data.get("average_temp")),
        pr_count=int(data.get("pr_count") or 0),
        achievement_count=int(data.get("achievement_count") or 0),
        kudos_count=int(data.get("kudos_count") or 0),
    )


def get_activity(access_token: str, activity_id: int) -> StravaActivity:
    """Fetch a single activity. ``access_token`` must be currently valid."""
    _require_requests()
    r = requests.get(
        f"{API_BASE}/activities/{activity_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        params={"include_all_efforts": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code == 401:
        raise StravaAuthError("Access token rejected fetching activity (401).")
    r.raise_for_status()
    return parse_activity(r.json())


def get_latest_activity(access_token: str) -> "StravaActivity | None":
    """Fetch the athlete's single most recent activity, or None if they have
    none. Returns the *summary* representation (no calories/suffer score), which
    is enough for the embed.
    """
    _require_requests()
    r = requests.get(
        f"{API_BASE}/athlete/activities",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        params={"per_page": 1},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code == 401:
        raise StravaAuthError("Access token rejected listing activities (401).")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        return None
    return parse_activity(data[0])


def get_activities_since(
    access_token: str, after_epoch: int, per_page: int = 100,
) -> list["StravaActivity"]:
    """List the athlete's activities started after ``after_epoch`` (one page).

    Used for the weekly recap. ``per_page`` caps the count — a week of training
    comfortably fits in one page for normal users.
    """
    _require_requests()
    r = requests.get(
        f"{API_BASE}/athlete/activities",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        params={"after": int(after_epoch), "per_page": per_page},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code == 401:
        raise StravaAuthError("Access token rejected listing activities (401).")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [parse_activity(d) for d in data]


def get_athlete(access_token: str) -> dict[str, Any]:
    """Fetch the authenticated athlete (used to capture a display name)."""
    _require_requests()
    r = requests.get(
        f"{API_BASE}/athlete",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code == 401:
        raise StravaAuthError("Access token rejected fetching athlete (401).")
    r.raise_for_status()
    return r.json()


def athlete_display_name(athlete: dict[str, Any]) -> str:
    """Best-effort human name from a Strava athlete object."""
    first = (athlete.get("firstname") or "").strip()
    last = (athlete.get("lastname") or "").strip()
    name = f"{first} {last}".strip()
    return name or (athlete.get("username") or "Strava athlete")


# ---------------------------------------------------------------------------
# Webhook push-subscription management
# ---------------------------------------------------------------------------

def create_subscription(cfg: StravaConfig) -> int:
    """Create the push subscription. Returns the subscription id.

    Strava immediately GETs ``cfg.webhook_callback_url`` to validate the
    ``verify_token`` before this POST returns, so the web server must already be
    publicly reachable when this is called.
    """
    _require_requests()
    r = requests.post(
        SUBSCRIPTION_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "callback_url": cfg.webhook_callback_url,
            "verify_token": cfg.verify_token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise StravaAuthError(
            f"Subscription create failed ({r.status_code}): {r.text[:300]}",
            status_code=r.status_code,
        )
    return int(r.json()["id"])


def view_subscriptions(cfg: StravaConfig) -> list[dict[str, Any]]:
    """List existing push subscriptions for this app.

    Strava's view/delete subscription endpoints take the app credentials as GET
    *query params* (a GET has no body), so ``client_secret`` unavoidably rides in
    the request URL. That URL must therefore NEVER reach a log line. In
    particular we do **not** call ``r.raise_for_status()``: its ``HTTPError``
    stringifies the full request URL (secret and all), and callers log the
    exception. Instead we mirror :func:`create_subscription` and raise the
    response *body*, which is both URL-free and carries Strava's real reason —
    e.g. a 403 with an "Inactive" application status once Standard Tier requires
    a paid subscription for API access.
    """
    _require_requests()
    r = requests.get(
        SUBSCRIPTION_URL,
        params={"client_id": cfg.client_id, "client_secret": cfg.client_secret},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        raise StravaAuthError(
            f"Subscription view failed ({r.status_code}): {r.text[:300]}",
            status_code=r.status_code,
        )
    data = r.json()
    return data if isinstance(data, list) else []


def delete_subscription(cfg: StravaConfig, subscription_id: int) -> None:
    """Delete a push subscription by id.

    Like :func:`view_subscriptions`, the credentials ride in the query string
    (secret in the URL), so this deliberately raises only the response *body* on
    failure — never ``r.raise_for_status()`` — keeping the secret-bearing URL out
    of any exception message that a caller might log.
    """
    _require_requests()
    r = requests.delete(
        f"{SUBSCRIPTION_URL}/{subscription_id}",
        params={"client_id": cfg.client_id, "client_secret": cfg.client_secret},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code not in (200, 204):
        raise StravaAuthError(
            f"Subscription delete failed ({r.status_code}): {r.text[:200]}",
            status_code=r.status_code,
        )


# ---------------------------------------------------------------------------
# Pure formatting helpers (easy to unit test; no discord types)
# ---------------------------------------------------------------------------

# Sport types that are distance-first (pace matters); everything else is
# treated as a duration-first effort.
_DISTANCE_SPORTS = frozenset(
    {
        "Run", "TrailRun", "VirtualRun",
        "Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide",
        "Walk", "Hike", "Swim", "Kayaking", "Canoeing", "Rowing", "Velomobile",
        "InlineSkate", "IceSkate", "NordicSki", "BackcountrySki", "RollerSki",
        "Handcycle",
    }
)

_SPORT_EMOJI = {
    "Run": "🏃", "TrailRun": "🏃", "VirtualRun": "🏃",
    "Ride": "🚴", "VirtualRide": "🚴", "MountainBikeRide": "🚵",
    "GravelRide": "🚴", "EBikeRide": "🚴",
    "Swim": "🏊",
    "Walk": "🚶", "Hike": "🥾",
    "WeightTraining": "🏋️", "Workout": "🏋️", "Crossfit": "🏋️",
    "Yoga": "🧘", "Pilates": "🧘",
    "Rowing": "🚣", "Kayaking": "🛶", "Canoeing": "🛶",
    "Hiit": "🔥", "Elliptical": "🏃", "StairStepper": "🪜",
    "AlpineSki": "⛷️", "NordicSki": "🎿", "Snowboard": "🏂",
    "IceSkate": "⛸️", "InlineSkate": "🛼",
    "Soccer": "⚽", "Tennis": "🎾", "Golf": "⛳",
}


def sport_emoji(sport_type: str) -> str:
    return _SPORT_EMOJI.get(sport_type, "💪")


def is_distance_sport(sport_type: str) -> bool:
    return sport_type in _DISTANCE_SPORTS


# Unit conversion constants.
_METRES_PER_MILE = 1609.344
_FEET_PER_METRE = 3.28084
_MPH_PER_MS = 2.236936


def format_distance(metres: float, imperial: bool = False) -> str:
    """Human distance. Metric: m under 1 km, else km. Imperial: ft under ~0.1
    mi, else miles."""
    if metres <= 0:
        return "—"
    if imperial:
        miles = metres / _METRES_PER_MILE
        if miles < 0.1:
            return f"{metres * _FEET_PER_METRE:.0f} ft"
        return f"{miles:.2f} mi"
    if metres < 1000:
        return f"{metres:.0f} m"
    return f"{metres / 1000:.2f} km"


def format_duration(seconds: int) -> str:
    """``H:MM:SS`` (drops the hour when zero) — matches Strava's own style."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_pace(
    distance_m: float, moving_time_s: int, imperial: bool = False
) -> Optional[str]:
    """Pace in min/km (or min/mi imperial), or None when not meaningful."""
    if distance_m <= 0 or moving_time_s <= 0:
        return None
    unit = _METRES_PER_MILE if imperial else 1000.0
    secs = moving_time_s / (distance_m / unit)
    m, s = divmod(int(round(secs)), 60)
    return f"{m}:{s:02d} {'/mi' if imperial else '/km'}"


def format_speed(average_speed_ms: float, imperial: bool = False) -> Optional[str]:
    """Average speed in km/h (or mph imperial), or None when zero."""
    if average_speed_ms <= 0:
        return None
    if imperial:
        return f"{average_speed_ms * _MPH_PER_MS:.1f} mph"
    return f"{average_speed_ms * 3.6:.1f} km/h"


def format_elevation(metres: float, imperial: bool = False) -> str:
    """Elevation in metres (or feet imperial)."""
    if imperial:
        return f"{metres * _FEET_PER_METRE:.0f} ft"
    return f"{metres:.0f} m"


def format_temp(celsius: float, imperial: bool = False) -> str:
    """Temperature in °C (or °F imperial)."""
    if imperial:
        return f"{celsius * 9 / 5 + 32:.0f}°F"
    return f"{celsius:.0f}°C"


# ---------------------------------------------------------------------------
# Map rendering (optional Mapbox static-map overlay)
# ---------------------------------------------------------------------------

MAPBOX_STATIC_BASE = "https://api.mapbox.com/styles/v1/mapbox"

# Mapbox caps static-image request URLs; long routes can blow past it, in which
# case we signal the caller to fall back to the local silhouette.
_MAPBOX_URL_LIMIT = 8000


# Marker colours (start = green, finish = red), matching the local renderer.
_START_PIN = "19d36b"
_FINISH_PIN = "e02020"


def _mapbox_url(
    overlays: list[str], style: str, width: int, height: int, token: str,
) -> str:
    overlay_str = ",".join(overlays)
    return (
        f"{MAPBOX_STATIC_BASE}/{style}/static/{overlay_str}/auto/"
        f"{width}x{height}@2x?padding=40&access_token={token}"
    )


def mapbox_route_url(
    polyline: str,
    token: str,
    *,
    style: str = "outdoors-v12",
    width: int = 640,
    height: int = 440,
    color: str = "fc4c02",
    stroke: int = 5,
    markers: bool = True,
) -> Optional[str]:
    """Build a Mapbox Static Images URL overlaying *polyline* on a real map.

    The encoded polyline is passed straight through as a path overlay (Mapbox
    decodes precision-5 polylines natively); ``auto`` fits the viewport to the
    route, rendered at ``@2x`` for retina sharpness. When *markers* is set, a
    green start pin and red finish pin are added at the route endpoints.

    Returns None when there's no route or the URL would exceed Mapbox's length
    limit even after dropping the markers (caller should fall back to a local
    render). The bounded ``width``/``height`` (≤1280) keep the ``@2x`` output
    inside Mapbox's image-size cap.
    """
    if not polyline or not token:
        return None
    encoded = urllib.parse.quote(polyline, safe="")
    path = f"path-{stroke}+{color}({encoded})"

    overlays = [path]
    if markers:
        pts = decode_polyline(polyline)
        if len(pts) >= 2:
            (slat, slon), (elat, elon) = pts[0], pts[-1]
            # Pins are drawn after the path so they sit on top; lon precedes lat.
            overlays.append(f"pin-s+{_START_PIN}({slon:.5f},{slat:.5f})")
            overlays.append(f"pin-s+{_FINISH_PIN}({elon:.5f},{elat:.5f})")

    url = _mapbox_url(overlays, style, width, height, token)
    if len(url) > _MAPBOX_URL_LIMIT and len(overlays) > 1:
        # Markers pushed us over — retry with just the path.
        url = _mapbox_url([path], style, width, height, token)
    if len(url) > _MAPBOX_URL_LIMIT:
        return None
    return url
