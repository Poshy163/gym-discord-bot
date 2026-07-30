"""Home Assistant integration client.

Like Hevy (per-member API key) and Strava (per-member OAuth token), every member
brings **their own** Home Assistant: they supply a base URL and a long-lived
access token with ``/setup_ha``, and the token is stored Fernet-encrypted per
user — the plaintext is never persisted, and an operator never holds somebody
else's house key. What they link on top of that is their slice of that server's
entity namespace.

A Renpho/Withings/Xiaomi body-composition scale exposed through HA lands as one
sensor per metric, all sharing a per-person prefix::

    sensor.joshua_s_weight                      106.30 kg
    sensor.joshua_s_body_fat_percentage           35.2 %
    sensor.joshua_s_muscle_mass                  65.48 kg
    sensor.joshua_s_basal_metabolic_rate          1838 kcal
    ...

:func:`classify_entity` splits such an id into ``(metric, person_prefix)``, so
linking stores the *prefix* rather than ten entity ids — a scale that starts
reporting an eleventh metric is picked up on the next poll with no re-link and
no migration.

There is no push: the bot **polls** ``/api/states`` on a timer and treats the
weight sensor's ``last_changed`` as the identity of a weigh-in. That choice is
load-bearing for de-duplication; see :func:`reading_key`.

The HTTP calls are synchronous (``requests``); the bot runs them in an executor
so the event loop isn't blocked. Everything below the "Pure mappers" line is
network-free and unit-tested. This module is import-safe when ``requests`` isn't
installed, so the bot still boots without the Home Assistant feature.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

LOG = logging.getLogger("gym-bot.homeassistant")

_TIMEOUT = 15

#: States Home Assistant uses to mean "there is no reading right now". A scale
#: reports ``unavailable`` whenever it's asleep, which is *most* of the time, so
#: importing one of these as a weigh-in would be the single most likely bug here.
MISSING_STATES = frozenset({"unknown", "unavailable", "none", ""})

#: Cap on a single history backfill. HA's recorder keeps 10 days by default, so
#: this is generous; it exists to bound the response size on installs that keep
#: years of state in a MariaDB recorder.
MAX_BACKFILL_DAYS = 90


class HAError(Exception):
    """Generic Home Assistant API failure."""


class HAUnavailable(HAError):
    """Raised when an optional dependency (requests) is missing."""


class HAAuthError(HAError):
    """Raised when Home Assistant rejects the access token (401/403)."""


class HANotFound(HAError):
    """Raised when an entity id doesn't exist on the server (404)."""


class HABanned(HAError):
    """Raised on 403 — Home Assistant's ``http.ban`` middleware blocked our IP.

    Deliberately *not* an :class:`HAAuthError`. A 403 from HA is never an auth
    problem (a bad token is always 401), and it is the one failure where retrying
    makes things worse: only the operator deleting the entry from
    ``ip_bans.yaml`` and restarting HA clears it.
    """


class HACertError(HAError):
    """Raised when TLS certificate verification failed.

    Separate from :class:`HAUnreachable` because the host was found and answered
    — the fix is the certificate or the verification toggle, not the URL.
    """


class HAUnreachable(HAError):
    """Raised when the host could not be reached at all (DNS, refused, timeout).

    Split out from :class:`HAError` because the overwhelmingly likely cause is
    ``homeassistant.local`` failing to resolve from inside a container, which
    needs a completely different fix from an API error.
    """


# Optional dep — imported lazily so the bot can boot without it.
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
# Configuration
# ---------------------------------------------------------------------------

_TRUE = ("1", "true", "yes", "y", "on")

#: The only values that switch certificate verification OFF. Deliberately the
#: inverse test of :data:`_TRUE`: "is this false" rather than "is this true", so
#: an unrecognised value (a typo, ``enabled``, ``strict``) leaves verification ON.
#: Written the other way round, every value the truthy list didn't happen to
#: contain silently disabled TLS validation on requests carrying the token.
_EXPLICITLY_FALSE = ("0", "false", "no", "n", "off")


def normalize_base_url(raw: str) -> str:
    """Clean an operator-pasted Home Assistant URL into a request base.

    Accepts what people actually paste: a bare host (``homeassistant.local:8123``
    → ``http://…``), a trailing slash, or the API path itself
    (``…:8123/api/`` → ``…:8123``). Returns "" for blank input so
    :attr:`HAConfig.configured` stays false rather than producing a URL that
    fails at request time.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    # A pasted "http://ha:8123/api" would otherwise become ".../api/api/states".
    while url.endswith("/api"):
        url = url[: -len("/api")].rstrip("/")
    return url


@dataclass(frozen=True)
class HAConfig:
    """One member's Home Assistant connection.

    Built per request from their stored row — every member brings their own
    server, so there is no process-wide configuration to read.
    """

    base_url: str = ""
    token: str = ""
    verify_ssl: bool = True

    @property
    def configured(self) -> bool:
        """True when both a base URL and a token are present."""
        return bool(self.base_url and self.token)


def config_for(base_url: str, token: str, *, verify_ssl: bool = True) -> HAConfig:
    """Build a config from a member's stored URL and their decrypted token."""
    return HAConfig(
        base_url=normalize_base_url(base_url),
        token=(token or "").strip(),
        verify_ssl=verify_ssl,
    )


# ---------------------------------------------------------------------------
# Token encryption (mirrors the Hevy/Strava/Revo Fernet scheme)
# ---------------------------------------------------------------------------

#: Falls back through every other integration's key, so one generated key serves
#: them all. See app/secretbox.py:FERNET_KEYS for why REVO_FERNET_KEY is last.
_FERNET_ENVS = (
    "HA_FERNET_KEY", "HEVY_FERNET_KEY", "STRAVA_FERNET_KEY", "REVO_FERNET_KEY",
)


def _fernet() -> "Fernet":
    if Fernet is None:
        raise HAUnavailable(
            "The 'cryptography' package is required to store Home Assistant "
            "access tokens."
        )
    key = ""
    for env in _FERNET_ENVS:
        key = os.environ.get(env, "").strip()
        if key:
            break
    if not key:
        raise HAUnavailable(
            "Set $HA_FERNET_KEY (or reuse $HEVY_FERNET_KEY / $STRAVA_FERNET_KEY "
            "/ $REVO_FERNET_KEY) to a Fernet key."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # pragma: no cover - bad key shape
        raise HAUnavailable(f"Invalid Fernet key: {exc}") from exc


def encrypt_token(plaintext: str) -> str:
    """Encrypt an access token for at-rest storage. Returns a urlsafe string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(token: str) -> str:
    """Inverse of :func:`encrypt_token`."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - corrupted DB row
        raise HAUnavailable(
            "Stored Home Assistant token is unreadable — re-run /setup_ha."
        ) from exc


def fernet_ready() -> bool:
    """True when a Fernet key is configured, so /setup_ha can store a token."""
    if Fernet is None:
        return False
    return any(os.environ.get(env, "").strip() for env in _FERNET_ENVS)


def verify_ssl_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Resolve ``HA_VERIFY_SSL``, failing closed on an unrecognised value.

    Deliberately the inverse test of a truthy list -- "is this explicitly false"
    rather than "is this true" -- so a typo leaves certificate verification ON.
    Written the other way round, every value the truthy list didn't happen to
    contain silently disabled TLS validation on token-bearing requests.
    """
    src = os.environ if env is None else env
    raw = (src.get("HA_VERIFY_SSL", "1") or "1").strip().lower()
    if raw and raw not in _EXPLICITLY_FALSE and raw not in _TRUE:
        LOG.warning(
            "HA_VERIFY_SSL=%r isn't a recognised on/off value — keeping "
            "certificate verification ON. Use 0 to switch it off.", raw,
        )
    return raw not in _EXPLICITLY_FALSE


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

#: Substrings that identify a name-resolution failure across the several libc,
#: urllib3 and OS spellings of it. One per tuple entry, spelled out rather than
#: implicitly concatenated across lines — a dropped comma in a list of split
#: strings silently merges two entries into one that matches nothing.
_DNS_FAILURE_MARKERS: tuple[str, ...] = (
    "getaddrinfo",
    "Name or service not known",
    "nodename nor servname",
    "Temporary failure in name resolution",
    "NameResolutionError",
    "No address associated with hostname",
)


def _unreachable_hint(cfg: HAConfig, exc: Exception) -> str:
    """Explain a connection failure in terms of the thing to change.

    The ``.local`` case is called out by name because it is the single most
    likely first-run failure: ``homeassistant.local`` is mDNS, and glibc/musl
    inside a Docker container won't resolve it without avahi and host multicast —
    so the URL that works perfectly in the operator's browser fails here, with a
    DNS error that looks nothing like the real cause.
    """
    text = str(exc)
    host = cfg.base_url.split("://", 1)[-1].split("/")[0]
    # Drop any userinfo before echoing the host into a Discord reply or a log
    # line. Home Assistant has no use for basic auth, but a "user:pass@host" URL
    # is accepted by the URL parser, and the whole point of this function is to
    # quote the host back at the operator.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    resolution_failed = any(
        marker in text
        for marker in _DNS_FAILURE_MARKERS
    )
    if resolution_failed and host.split(":")[0].endswith(".local"):
        return (
            f"Couldn't resolve {host}. '.local' addresses are mDNS, which "
            f"usually doesn't work from inside Docker — use the LAN IP instead "
            f"(e.g. http://192.168.1.50:8123)."
        )
    if resolution_failed:
        return (f"Couldn't resolve {host} — check the address, then re-run "
                "/setup_ha.")
    return f"Couldn't reach {host or 'Home Assistant'}: {text[:160]}"


def _get(cfg: HAConfig, path: str, params: dict | None = None) -> Any:
    """GET ``path`` from Home Assistant and return the decoded JSON.

    ``path`` starts with "/api/" and is concatenated onto a base URL that has no
    trailing slash — HA registers exact aiohttp routes with no trailing-slash
    aliases, so ``//api/states`` is a 404. (``urljoin`` is deliberately avoided:
    it would discard any path prefix a reverse proxy needs.)

    Raises :class:`HAAuthError` on 401 (a revoked or mistyped token),
    :class:`HABanned` on 403, :class:`HANotFound` on 404,
    :class:`HAUnreachable` when the host can't be reached, and :class:`HAError`
    for anything else.
    """
    if requests is None:
        raise HAUnavailable("The 'requests' package is required for Home Assistant.")
    if not cfg.configured:
        raise HAError("Home Assistant is not configured (missing URL or token).")
    try:
        resp = requests.get(
            f"{cfg.base_url}{path}",
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "Content-Type": "application/json",
            },
            params=params or {},
            timeout=_TIMEOUT,
            verify=cfg.verify_ssl,
        )
    except requests.exceptions.SSLError as exc:  # type: ignore[union-attr]
        # Must precede ConnectionError, which it subclasses. Otherwise a
        # certificate problem is reported as "couldn't reach the host" and points
        # the operator at the URL field, which is not the thing that's wrong.
        raise HACertError(
            "Home Assistant's TLS certificate wasn't accepted. If it's "
            "self-signed, switch off 'Verify the TLS certificate' in "
            "Settings → Home Assistant; otherwise the certificate is expired or "
            "for a different hostname."
        ) from exc
    except requests.ConnectionError as exc:  # type: ignore[union-attr]
        raise HAUnreachable(_unreachable_hint(cfg, exc)) from exc
    except requests.Timeout as exc:  # type: ignore[union-attr]
        raise HAUnreachable(
            f"Home Assistant didn't answer within {_TIMEOUT}s."
        ) from exc
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise HAError(f"Home Assistant request failed: {exc}") from exc
    if resp.status_code == 401:
        raise HAAuthError(
            "Home Assistant rejected the access token. Create a new long-lived "
            "token under your Home Assistant profile → Security."
        )
    if resp.status_code == 403:
        raise HABanned(
            "Home Assistant refused the connection (403) — this machine's IP is "
            "in its ip_bans.yaml. Remove the entry there and restart Home "
            "Assistant; retrying won't help."
        )
    if resp.status_code == 404:
        raise HANotFound(f"Home Assistant has no such endpoint or entity: {path}")
    if resp.status_code >= 400:
        raise HAError(
            f"Home Assistant API error {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise HAError("Home Assistant returned a non-JSON response.") from exc


def ping(cfg: HAConfig) -> dict:
    """Verify the URL + token against ``GET /api/``.

    Returns ``{"ok": True, "message": ..., "version": ..., "location": ...}``.
    The version/location come from ``/api/config`` and are best-effort — a token
    scoped oddly, or an older HA, still counts as reachable.
    """
    # The trailing slash is mandatory on this one route and only this one.
    data = _get(cfg, "/api/")
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or "")
    out = {"ok": True, "message": message or "API running.",
           "version": "", "location": ""}
    try:
        conf = fetch_config(cfg)
    except HAError:  # pragma: no cover - optional enrichment
        return out
    if isinstance(conf, dict):
        out["version"] = str(conf.get("version") or "")
        out["location"] = str(conf.get("location_name") or "")
    # unit_system is deliberately not read: HA reports metric mass as "g", so it
    # is worse than useless for kg-vs-lb. Units come per-entity from
    # attributes.unit_of_measurement, re-read on every poll because changing an
    # entity's display unit changes both the value and the unit with no other
    # signal.
    return out


def fetch_config(cfg: HAConfig) -> dict:
    """``GET /api/config`` — version, location_name, time_zone, unit_system."""
    data = _get(cfg, "/api/config")
    return data if isinstance(data, dict) else {}


def fetch_states(cfg: HAConfig) -> list[dict]:
    """``GET /api/states`` — every entity's current state.

    One request returns the whole machine, which is why the poll loop fetches
    once per cycle and fans it out across all linked members rather than issuing
    a request per person.
    """
    data = _get(cfg, "/api/states")
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def fetch_state(cfg: HAConfig, entity_id: str) -> dict:
    """``GET /api/states/<entity_id>`` — one entity, or :class:`HANotFound`.

    The 404 body is plain text (``Entity not found.``), not JSON, which is why
    :func:`_get` raises on the status before attempting to decode. A 404 here is
    permanent, not transient: REST exposes no entity registry, so a renamed
    entity is indistinguishable from a deleted one and the member must re-link.
    """
    data = _get(cfg, f"/api/states/{entity_id}")
    if not isinstance(data, dict):
        raise HAError(f"Unexpected state payload for {entity_id}.")
    return data


def fetch_history(
    cfg: HAConfig, entity_ids: Iterable[str], since: datetime,
    *, end: datetime | None = None,
) -> list[list[dict]]:
    """``GET /api/history/period/<since>`` for ``entity_ids``.

    Returns HA's shape: a list of per-entity lists of state snapshots, oldest
    first. ``minimal_response`` + ``no_attributes`` keep the payload small — the
    trade-off is that only the *first* snapshot in each list carries attributes
    (so units come from the live state instead; see
    :func:`weight_history_from_states`).

    ``significant_changes_only=0`` is sent **explicitly and with a value**. That
    parameter is not the presence flag the published docs imply: HA reads it as
    ``request.query.get("significant_changes_only", "1") != "0"``, so it defaults
    to *on*, and appending a bare ``&significant_changes_only`` leaves it on. For
    a scale every distinct reading is a real weigh-in, so filtering must be
    switched off — with an explicit ``0``, which is the only value that does it.
    """
    ids = ",".join(e for e in entity_ids if e)
    if not ids:
        return []
    params: dict[str, Any] = {
        "filter_entity_id": ids,
        # These two ARE presence flags (`"x" in request.query`), so an empty
        # value is what enables them.
        "minimal_response": "",
        "no_attributes": "",
        "significant_changes_only": "0",
    }
    if end is not None:
        params["end_time"] = _iso_utc(end)
    data = _get(cfg, f"/api/history/period/{_iso_utc(since)}", params)
    if not isinstance(data, list):
        return []
    return [series for series in data if isinstance(series, list)]


def history_for_entity(
    series_list: Iterable[Iterable[Mapping[str, Any]]], entity_id: str,
) -> list[Mapping[str, Any]]:
    """Pick one entity's series out of a ``/api/history/period`` response.

    Never index the outer list positionally. HA seeds its order from
    ``filter_entity_id`` but then drops entities with no rows, so with two
    requested ids and one empty result, position 0 is whichever entity *did*
    have history — not the one you asked for first. The entity id is on the
    series' first element, which is the only element carrying attributes under
    ``minimal_response``.
    """
    wanted = (entity_id or "").strip().lower()
    fallback: list[Mapping[str, Any]] | None = None
    for series in series_list:
        rows = [r for r in series if isinstance(r, Mapping)]
        if not rows:
            continue
        if fallback is None:
            fallback = rows
        if str(rows[0].get("entity_id") or "").strip().lower() == wanted:
            return rows
    # No row carried an entity_id (some proxies strip it). With a single-entity
    # request the only non-empty series must be the one we asked for.
    return fallback or []


# ---------------------------------------------------------------------------
# Pure mappers (no network — unit-tested)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    """One body-composition measurement the bot understands.

    ``suffixes`` are the entity-id endings seen across HA scale integrations
    (Renpho BLE, Withings, Xiaomi/Mi Scale, Garmin, Fitbit). ``kind`` drives
    unit handling: ``mass`` values are normalised to kg, ``pct``/``plain`` are
    taken as-is.
    """

    key: str
    label: str
    unit: str
    emoji: str
    precision: int
    kind: str  # "mass" | "pct" | "plain"
    suffixes: tuple[str, ...]


#: Registry of understood metrics. Order is irrelevant — lookup sorts suffixes
#: longest-first so "body_fat_percentage" can never be shadowed by "body_fat".
METRICS: tuple[Metric, ...] = (
    Metric("weight", "Weight", "kg", "⚖️", 2, "mass",
           ("weight", "weight_kg", "bodyweight", "body_weight", "body_mass")),
    Metric("body_fat_pct", "Body fat", "%", "🥓", 1, "pct",
           ("body_fat_percentage", "body_fat_pct", "body_fat", "fat_percentage",
            "fat_pct", "fat_ratio")),
    Metric("muscle_mass_kg", "Muscle mass", "kg", "💪", 2, "mass",
           ("muscle_mass", "muscle_mass_kg")),
    Metric("skeletal_muscle_pct", "Skeletal muscle", "%", "🦾", 1, "pct",
           ("skeletal_muscle_percentage", "skeletal_muscle_pct")),
    Metric("skeletal_muscle_kg", "Skeletal muscle mass", "kg", "🦾", 2, "mass",
           ("skeletal_muscle_mass",)),
    Metric("fat_free_mass_kg", "Fat-free mass", "kg", "🫀", 2, "mass",
           ("fat_free_mass", "lean_body_mass", "lean_mass")),
    Metric("bone_mass_kg", "Bone mass", "kg", "🦴", 2, "mass",
           ("bone_mass",)),
    Metric("body_water_pct", "Body water", "%", "💧", 1, "pct",
           ("body_water_percentage", "water_percentage", "body_water",
            "hydration_percentage", "water_pct")),
    Metric("protein_pct", "Protein", "%", "🍗", 1, "pct",
           ("protein_percentage", "protein_pct")),
    Metric("bmi", "BMI", "", "📐", 1, "plain",
           ("body_mass_index", "bmi")),
    Metric("bmr_kcal", "BMR", "kcal", "🔥", 0, "plain",
           ("basal_metabolic_rate", "bmr")),
    Metric("visceral_fat", "Visceral fat", "", "🫃", 1, "plain",
           ("visceral_fat_index", "visceral_fat_level", "visceral_fat")),
    Metric("subcutaneous_fat_pct", "Subcutaneous fat", "%", "🧴", 1, "pct",
           ("subcutaneous_fat_percentage", "subcutaneous_fat_pct")),
    Metric("metabolic_age", "Metabolic age", "yr", "🎂", 0, "plain",
           ("metabolic_age", "body_age")),
    Metric("body_score", "Body score", "", "⭐", 0, "plain",
           ("body_score", "health_score")),
)

METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}

#: The metric that defines a weigh-in. Without it there is nothing to log.
WEIGHT_KEY = "weight"

#: (suffix, metric) pairs, longest suffix first. Longest-first is what makes
#: "joshua_s_skeletal_muscle_mass" resolve to skeletal muscle rather than being
#: eaten by the "muscle_mass" suffix with a person prefix of "joshua_s_skeletal".
_SUFFIXES: tuple[tuple[str, Metric], ...] = tuple(
    sorted(
        ((suffix, m) for m in METRICS for suffix in m.suffixes),
        key=lambda pair: (-len(pair[0]), pair[0]),
    )
)

#: Entity domains that can carry a body measurement. ``sensor`` only, on purpose:
#: ``number``/``input_number`` helpers with names like ``goal_weight`` or
#: ``target_weight`` are common, and linking a member to their *goal* by accident
#: is worse than not finding a hand-maintained weight helper.
_DOMAINS = ("sensor",)


def classify_entity(entity_id: str) -> tuple[str, str] | None:
    """Split an entity id into ``(metric_key, person_prefix)``, or None.

    >>> classify_entity("sensor.joshua_s_weight")
    ('weight', 'joshua_s')
    >>> classify_entity("sensor.body_fat_percentage")
    ('body_fat_pct', '')
    >>> classify_entity("light.kitchen")

    A prefix of "" is legitimate — a single-person install often has bare
    ``sensor.weight`` entities.
    """
    eid = (entity_id or "").strip().lower()
    if "." not in eid:
        return None
    domain, _, object_id = eid.partition(".")
    if domain not in _DOMAINS or not object_id:
        return None
    for suffix, metric in _SUFFIXES:
        if object_id == suffix:
            return metric.key, ""
        if object_id.endswith(f"_{suffix}"):
            return metric.key, object_id[: -(len(suffix) + 1)]
    return None


def person_prefix(entity_id: str) -> str | None:
    """The person part of a body-metric entity id, or None if unrecognised."""
    hit = classify_entity(entity_id)
    return None if hit is None else hit[1]


def entity_id_for(prefix: str, metric_key: str, *, domain: str = "sensor") -> str:
    """Rebuild the canonical entity id for ``prefix`` + ``metric_key``.

    Uses each metric's *first* suffix as canonical. Only used for messages and
    as a last-resort lookup — the poll path always matches against real states,
    so a differently-named entity still works.
    """
    metric = METRICS_BY_KEY.get(metric_key)
    if metric is None:
        return ""
    suffix = metric.suffixes[0]
    return f"{domain}.{prefix}_{suffix}" if prefix else f"{domain}.{suffix}"


def group_body_entities(states: Iterable[dict]) -> dict[str, dict[str, dict]]:
    """Bucket ``/api/states`` rows into ``{person_prefix: {metric_key: state}}``.

    Rows that aren't body metrics are dropped, so this doubles as the discovery
    listing behind ``/ha_entities``.

    Two entities can claim the same person+metric — ``sensor.joshua_s_weight``
    alongside ``sensor.joshua_s_bodyweight``, say, since both suffixes mean
    weight. A **readable** state always beats an ``unknown``/``unavailable`` one;
    only between two readable states does the more recent win. Ordering on recency
    alone let a dead duplicate that happened to change state most recently shadow
    the live sensor and silently stop every import for that member.
    """
    out: dict[str, dict[str, dict]] = {}
    for state in states:
        if not isinstance(state, dict):
            continue
        hit = classify_entity(str(state.get("entity_id") or ""))
        if hit is None:
            continue
        metric_key, prefix = hit
        bucket = out.setdefault(prefix, {})
        existing = bucket.get(metric_key)
        if existing is None or _better_state(state, existing):
            bucket[metric_key] = state
    return out


def _has_value(state: Mapping[str, Any]) -> bool:
    """True when this state carries a number rather than a missing-data sentinel."""
    return _as_float(state.get("state")) is not None


def _better_state(candidate: Mapping[str, Any],
                  incumbent: Mapping[str, Any]) -> bool:
    """Should ``candidate`` replace ``incumbent`` for one person+metric?"""
    if _has_value(candidate) != _has_value(incumbent):
        return _has_value(candidate)
    return _state_time(candidate) >= _state_time(incumbent)


def entities_for_prefix(
    states: Iterable[dict], prefix: str,
) -> dict[str, dict]:
    """``{metric_key: state}`` for one person's entities."""
    return group_body_entities(states).get((prefix or "").strip().lower(), {})


# -- numbers ----------------------------------------------------------------

_LB_PER_KG = 2.2046226218487757
_KG_PER_STONE = 6.35029318


def _as_float(value: Any) -> float | None:
    """Parse a HA state string to a float, or None for missing/garbage."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in MISSING_STATES:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def mass_to_kg(value: float, unit: str | None) -> float:
    """Normalise a mass reading to kilograms.

    Home Assistant reports whatever the integration set, and an imperial install
    gets ``lb``. Unknown units are assumed to be kg — HA's metric default —
    rather than raising, because a missing ``unit_of_measurement`` on an
    otherwise-good weight sensor shouldn't stop a weigh-in importing.
    """
    u = (unit or "").strip().lower()
    if u in ("lb", "lbs", "pound", "pounds"):
        return value / _LB_PER_KG
    if u in ("g", "gram", "grams"):
        return value / 1000.0
    if u in ("st", "stone", "stones"):
        return value * _KG_PER_STONE
    if u in ("oz", "ounce", "ounces"):
        return value / (_LB_PER_KG * 16.0)
    return value


def _attr(state: Mapping[str, Any], name: str) -> str:
    attrs = state.get("attributes")
    if isinstance(attrs, Mapping):
        return str(attrs.get(name) or "")
    return ""


def parse_ha_time(value: Any) -> datetime | None:
    """Parse a HA ``last_changed``/``last_updated`` stamp to a tz-aware datetime.

    HA emits ISO-8601 UTC (``2026-07-30T03:16:06.123456+00:00``). A naive value
    is assumed UTC, matching :func:`app.bot._parse_hevy_time`.
    """
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _state_time(state: Mapping[str, Any]) -> datetime:
    """When this state was measured, for ordering. Epoch when unparseable."""
    dt = (
        parse_ha_time(state.get("last_changed"))
        or parse_ha_time(state.get("last_updated"))
    )
    return dt or datetime.fromtimestamp(0, tz=timezone.utc)


def _iso_utc(dt: datetime) -> str:
    """Format a datetime as the UTC ISO-8601 string HA's history path wants."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def reading_key(measured_at: datetime | None) -> str:
    """Stable de-duplication key for a weigh-in.

    A weigh-in is identified by **when the weight sensor last changed**, to
    whole seconds in UTC. Two consequences worth knowing, both deliberate:

    * HA only bumps ``last_changed`` when the *value* changes, so stepping on
      the scale twice and reading exactly the same kg is one weigh-in, not two.
      That is the behaviour we want — the alternative spams the channel every
      poll for an unchanged reading.
    * The key is derived from HA's own timestamp rather than "now", so a bot
      restart or a re-link both recompute the same key and cannot double-post
      history.
    """
    if measured_at is None:
        return ""
    return _iso_utc(measured_at)


#: Attribute names under which a scale integration publishes its own list of
#: past measurements on the weight entity. The Renpho BLE integration uses
#: ``weight_history``; the others are the shapes seen on comparable integrations.
_HISTORY_ATTRS = ("weight_history", "measurements", "history")

#: Per-entry keys, in preference order, for each field of a history entry.
_H_ID = ("measurement_id", "id", "uuid")
_H_TIME = ("timestamp", "time", "measured_at", "date")
_H_WEIGHT = ("weight", "weight_kg", "value")
_H_UNIT = ("weight_unit", "unit", "unit_of_measurement")

#: History-entry fields that map onto a metric the bot stores. A history entry is
#: much thinner than the live sibling sensors — Renpho carries only body fat —
#: which is exactly why the newest entry is paired with the live siblings instead.
_H_METRICS = {
    "body_fat": "body_fat_pct",
    "body_fat_percentage": "body_fat_pct",
    "bmi": "bmi",
    "body_mass_index": "bmi",
    "muscle_mass": "muscle_mass_kg",
    "bone_mass": "bone_mass_kg",
    "body_water": "body_water_pct",
    "body_water_percentage": "body_water_pct",
    "basal_metabolic_rate": "bmr_kcal",
    "bmr": "bmr_kcal",
}


def _first(entry: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in entry and entry[name] not in (None, ""):
            return entry[name]
    return None


def measurement_key(measurement_id: str) -> str:
    """De-duplication key for a scale-supplied measurement id.

    Namespaced with an ``id:`` prefix so it can never collide with a
    :func:`reading_key` timestamp, which matters because one member can switch
    between the two schemes (a scale integration update that starts or stops
    publishing its history) without their imported weigh-ins being re-announced
    or silently skipped.
    """
    return f"id:{str(measurement_id).strip()}" if measurement_id else ""


def readings_from_history_attr(
    weight_state: Mapping[str, Any],
) -> list[dict] | None:
    """Parse a weight entity's own history attribute into readings, or None.

    None means "this entity doesn't publish one" — fall back to the live state
    plus HA's recorder. A list (possibly empty) means it does.

    This is the *preferred* source, for three reasons found by probing a real
    install rather than by reading docs:

    * The entries carry a stable ``measurement_id``, which is a strictly better
      de-duplication key than ``last_changed``. It survives a Home Assistant
      restart, and it distinguishes two genuine weigh-ins that happen to land on
      the same kilogram — both of which the timestamp scheme gets wrong.
    * They carry the *measurement's* timestamp, not the moment HA noticed it.
    * They need no recorder. A scale's entities are frequently excluded from the
      recorder (they were on the install this was built against), which makes
      ``/api/history/period`` return nothing at all for them.

    Returned oldest-first. Entries without a usable weight or timestamp are
    dropped rather than guessed at.
    """
    attrs = weight_state.get("attributes")
    if not isinstance(attrs, Mapping):
        return None
    raw = None
    for name in _HISTORY_ATTRS:
        candidate = attrs.get(name)
        if isinstance(candidate, (list, tuple)):
            raw = candidate
            break
    if raw is None:
        return None

    entity_id = str(weight_state.get("entity_id") or "")
    fallback_unit = str(attrs.get("unit_of_measurement") or "")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        value = _as_float(_first(entry, _H_WEIGHT))
        when = parse_ha_time(_first(entry, _H_TIME))
        if value is None or value <= 0 or when is None:
            continue
        # A history entry carries one unit, for its weight. Mass metrics in the
        # same entry are assumed to share it, which is what every scale seen does
        # — they report a whole measurement in one unit system. Percentages and
        # indices are unaffected either way, being unitless.
        unit = str(_first(entry, _H_UNIT) or fallback_unit)
        mid = _first(entry, _H_ID)
        metrics: dict[str, dict] = {}
        for field, metric_key in _H_METRICS.items():
            if metric_key in metrics:
                continue
            extra = _as_float(entry.get(field))
            if extra is None:
                continue
            metric = METRICS_BY_KEY[metric_key]
            if metric.kind == "mass":
                extra = mass_to_kg(extra, unit)
            metrics[metric_key] = {
                "value": round(extra, 4),
                "unit": "kg" if metric.kind == "mass" else metric.unit,
                "label": metric.label,
                "emoji": metric.emoji,
                "precision": metric.precision,
                "entity_id": entity_id,
                "measured_at": when,
            }
        out.append({
            "weight_kg": round(mass_to_kg(value, unit), 3),
            "measured_at": when,
            # Fall back to the timestamp scheme for an entry with no id, so a
            # partially-populated history still imports.
            "key": measurement_key(mid) if mid else reading_key(when),
            "measurement_id": str(mid or ""),
            "entity_id": entity_id,
            "friendly_name": str(attrs.get("friendly_name") or ""),
            "metrics": metrics,
        })
    out.sort(key=lambda r: r["measured_at"])
    return out


#: How close two same-weight readings must be to count as one weigh-in. A scale
#: reports the weight the instant you step on and then re-reports the *same*
#: measurement seconds later once it has an impedance reading, under a fresh
#: measurement id — 16 seconds apart on the install this was built against. Id
#: de-duplication cannot see that, so the pair announced twice.
SAME_WEIGHT_WINDOW_SECONDS = 900


def collapse_same_weight(
    readings: list[dict], *, window_seconds: int = SAME_WEIGHT_WINDOW_SECONDS,
) -> list[dict]:
    """Fold re-reports of one weigh-in into a single reading. Oldest-first in/out.

    Two readings collapse when they carry the **same weight** and land within
    ``window_seconds`` of each other. The survivor keeps the *earliest* key and
    timestamp — so it still de-duplicates against whatever was already imported
    when the pair straddles two polls — and the *union* of the metrics, with the
    later (richer) values winning, because the second report is the one that
    carries body composition.

    Weight equality is the discriminator on purpose. Two genuine weigh-ins
    seconds apart differ in kilograms (106.4 then 106.7 on the real install);
    a re-report of the same measurement does not. And treating an identical
    repeat as one weigh-in is the behaviour :func:`reading_key` already
    documents, so this is consistent rather than a new rule.
    """
    out: list[dict] = []
    for reading in readings:
        weight = reading.get("weight_kg")
        when = reading.get("measured_at")
        match = None
        if isinstance(when, datetime):
            for kept in out:
                kept_when = kept.get("measured_at")
                if not isinstance(kept_when, datetime):
                    continue
                if abs(float(kept.get("weight_kg", 0)) - float(weight or 0)) >= 0.005:
                    continue
                if abs((when - kept_when).total_seconds()) <= window_seconds:
                    match = kept
                    break
        if match is None:
            out.append(dict(reading))
            continue
        merged = dict(match.get("metrics") or {})
        merged.update(reading.get("metrics") or {})
        match["metrics"] = merged
        # Track what was folded in, so a caller can say so if it wants to.
        match.setdefault("folded", []).append(reading.get("key", ""))
    return out


def merge_live_metrics(reading: dict, live: Mapping[str, Any]) -> dict:
    """Fill a history entry's sparse metrics from the live sibling sensors.

    The newest history entry and the live sensors describe the same weigh-in, but
    the entry carries only what the integration chose to log (Renpho: body fat)
    while the siblings carry all ten. Live values win on conflict — they are what
    Home Assistant is displaying right now.
    """
    merged = dict(reading)
    metrics = dict(reading.get("metrics") or {})
    metrics.update({
        k: v for k, v in (live or {}).items() if k != WEIGHT_KEY
    })
    merged["metrics"] = metrics
    return merged


def build_reading(states_by_metric: Mapping[str, Mapping[str, Any]]) -> dict | None:
    """Turn one person's ``{metric_key: state}`` into a normalised reading.

    Returns None when there's no usable weight — an asleep scale reporting
    ``unavailable``, or a prefix that only matched non-weight sensors. Every
    mass metric is converted to kg; percentages and indices pass through.

    The returned dict is the unit of work for the rest of the integration::

        {"weight_kg": 106.3,
         "measured_at": datetime(...),
         "key": "2026-07-30T03:16:06+00:00",
         "metrics": {"body_fat_pct": {"value": 35.2, "unit": "%", ...}, ...}}
    """
    weight_state = states_by_metric.get(WEIGHT_KEY)
    if not isinstance(weight_state, Mapping):
        return None
    raw = _as_float(weight_state.get("state"))
    if raw is None or raw <= 0:
        return None
    weight_kg = mass_to_kg(raw, _attr(weight_state, "unit_of_measurement"))
    measured_at = _state_time(weight_state)

    metrics: dict[str, dict] = {}
    for key, state in states_by_metric.items():
        metric = METRICS_BY_KEY.get(key)
        if metric is None or not isinstance(state, Mapping):
            continue
        value = _as_float(state.get("state"))
        if value is None:
            continue
        unit = _attr(state, "unit_of_measurement")
        if metric.kind == "mass":
            value = mass_to_kg(value, unit)
            unit = "kg"
        metrics[key] = {
            "value": round(value, 4),
            "unit": unit or metric.unit,
            "label": metric.label,
            "emoji": metric.emoji,
            "precision": metric.precision,
            "entity_id": str(state.get("entity_id") or ""),
            "measured_at": _state_time(state),
        }

    return {
        "weight_kg": round(weight_kg, 3),
        "measured_at": measured_at,
        "key": reading_key(measured_at),
        "entity_id": str(weight_state.get("entity_id") or ""),
        "friendly_name": _attr(weight_state, "friendly_name"),
        "metrics": metrics,
    }


def summarize_reading(reading: Mapping[str, Any], previous: Mapping[str, Any] | None,
                      ) -> dict:
    """Add the deltas a channel alert wants: kg change and days since.

    ``previous`` is ``{"weight_kg": float, "recorded_at": datetime|str}`` — the
    member's last known weigh-in from *any* source, so a chat-logged ``bw 107``
    still gives the scale something to compare against.
    """
    out = dict(reading)
    out["delta_kg"] = None
    out["days_since"] = None
    if not previous:
        return out
    prev_kg = _as_float(previous.get("weight_kg"))
    if prev_kg is None:
        return out
    prev_at = previous.get("recorded_at")
    prev_dt = prev_at if isinstance(prev_at, datetime) else parse_ha_time(prev_at)
    now_dt = reading.get("measured_at")
    if isinstance(prev_dt, datetime) and isinstance(now_dt, datetime):
        if prev_dt.tzinfo is None:
            prev_dt = prev_dt.replace(tzinfo=timezone.utc)
        if prev_dt > now_dt:
            # The comparison point is NEWER than this reading — a backdated
            # import landing behind a weight the member typed in since. Reporting
            # a delta here states the change backwards and, with days_since
            # clamped at 0, labels it "since earlier today". No delta is the
            # honest answer.
            return out
        out["days_since"] = max(
            0, round((now_dt - prev_dt).total_seconds() / 86400),
        )
    out["delta_kg"] = round(float(reading.get("weight_kg", 0.0)) - prev_kg, 2)
    return out


def format_metric(metric_key: str, value: float, unit: str = "") -> str:
    """Render a stored metric for display, e.g. ``35.2%`` or ``65.48 kg``."""
    metric = METRICS_BY_KEY.get(metric_key)
    precision = metric.precision if metric else 1
    shown_unit = unit or (metric.unit if metric else "")
    text = f"{value:.{precision}f}"
    if shown_unit == "%":
        return f"{text}%"
    return f"{text} {shown_unit}".strip()


def weight_history_from_states(
    series: Iterable[Mapping[str, Any]], *, unit: str | None = None,
) -> list[tuple[datetime, float]]:
    """Extract ``(measured_at, weight_kg)`` pairs from one history series.

    Handles HA's ``minimal_response`` shape, where only the first snapshot is a
    full state object and the rest carry just ``state`` + ``last_changed`` — the
    unit therefore comes from ``unit`` (read off the live state) with the first
    snapshot's own attributes as a fallback. Consecutive duplicate readings are
    collapsed, ``unknown``/``unavailable`` are dropped, and the result is
    oldest-first and de-duplicated by timestamp.
    """
    resolved_unit = (unit or "").strip()
    rows: list[tuple[datetime, float]] = []
    seen: set[str] = set()
    for snapshot in series:
        if not isinstance(snapshot, Mapping):
            continue
        if not resolved_unit:
            resolved_unit = _attr(snapshot, "unit_of_measurement")
        value = _as_float(snapshot.get("state"))
        if value is None or value <= 0:
            continue
        when = (
            parse_ha_time(snapshot.get("last_changed"))
            or parse_ha_time(snapshot.get("last_updated"))
        )
        if when is None:
            continue
        key = reading_key(when)
        if key in seen:
            continue
        seen.add(key)
        rows.append((when, round(mass_to_kg(value, resolved_unit), 3)))
    rows.sort(key=lambda pair: pair[0])
    # Collapse runs of the same weight: HA records a row per recorder write, and
    # only the first of a run is the moment the member actually stepped on.
    collapsed: list[tuple[datetime, float]] = []
    for when, kg in rows:
        if collapsed and abs(collapsed[-1][1] - kg) < 1e-9:
            continue
        collapsed.append((when, kg))
    return collapsed
