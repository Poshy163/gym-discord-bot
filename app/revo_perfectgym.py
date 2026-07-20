"""Revo Fitness live-occupancy client (PerfectGym ClientPortal2 backend).

Why this exists / what it restores
----------------------------------
The web portal's all-clubs board (``club-counter.php``) was access-guarded in
2026-07 (see ``docs/REVO_PORTAL.md`` §1.2), so :mod:`app.revo_client` could only
scrape the logged-in account's *own* favourite-club count off the rewards
landing — ``/busy`` was degraded to that single number. The Netpulse mobile
backend was a dead end too: every club there reports ``"mms": "perfectgym"``,
i.e. Revo runs **member management / access / occupancy on PerfectGym**, not on
Netpulse (see ``app/revo_netpulse.py``).

The live "Members in club" counter that the Revo **iOS app** shows is served by
that same PerfectGym backend — a single authenticated GET returns the live
head-count for **every** club at once. This module is the thin, read-only client
for it, and it is what restores ``/busy`` to a real all-clubs live board.

Confirmed contract (do not re-probe)
------------------------------------
* **LOGIN:** ``POST /ClientPortal2/Auth/Login`` with JSON
  ``{"RememberMe":false,"Login":<email>,"Password":<pw>}`` → ``200`` and a
  ``Set-Cookie: CpAuthToken`` that a :class:`requests.Session` carries. The
  response body is the member profile (``{"User":{"Member":{"Id",...}}}``) — it
  is **PII**; we read only the non-secret ``HomeClubId`` from it and never log
  the body.
* **OCCUPANCY (all clubs, one call):** ``GET /ClientPortal2/Clubs/Clubs/
  GetMembersInClubs`` (CpAuthToken cookie; no CSRF for a GET) → ``200`` JSON
  ``{"UsersInClubList":[{"ClubName","ClubAddress","UsersLimit",
  "UsersCountCurrentlyInClub"}, ...]}``. There is **no club-id field**, so the
  home-club identity is resolved by *name* (via the rewards-landing fav club).
* On session expiry the occupancy GET redirects/401s; we re-login once and retry
  (mirrors :meth:`app.revo_client.RevoClient._get`).

Security (hard rules — mirror :mod:`app.revo_client` / :mod:`app.revo_netpulse`)
--------------------------------------------------------------------------------
The login carries **secrets**: the ``CpAuthToken`` cookie and the member profile
(name / email / photo / ids — PII). This client MUST NOT log them, MUST NOT
return them from public methods, and MUST NOT persist them. From the profile we
keep only: the non-secret ``HomeClubId`` integer, a *non-sensitive* membership
summary (:class:`MembershipStatus` — contract/payment/card flags, no identity),
the member's ``FirstName`` (a non-secret display name, served by
:meth:`~PerfectGymClient.get_first_name`), and — privately — the ``UserNumber``
and the signed ``PhotoUrl``.

``UserNumber`` is the **physical entry BARCODE** (an access credential). It is the
**one** sensitive value a public method may return, exposed ONLY by
:meth:`PerfectGymClient.get_card_number` for the ephemeral ``/revo_card`` reply.
It MUST NEVER be logged, persisted, or placed in any other dataclass/repr.

``PhotoUrl`` is a **short-lived signed CDN capability URL** (``…&sig=…``, valid
~10 min): whoever holds it can fetch the member's photo without auth, so it MUST
NEVER be logged or persisted. It is surfaced only by
:meth:`PerfectGymClient.get_photo_url` (pass ``refresh=True`` to re-login for a
fresh signature) so ``/seeprofile`` can download the bytes immediately; the login
log line stays email + home_club_id only. :func:`download_photo` fetches those
bytes (the signature *is* the credential — an unauthenticated GET).

The pure parsers are the scrubbing boundaries: :func:`parse_members_in_clubs`
(public occupancy fields), :func:`parse_club_list` (public directory/geo, no PII),
and :func:`parse_membership_status` (non-sensitive contract flags only).

Confirmed extra reads (do not re-probe)
---------------------------------------
* **CLUB DIRECTORY (public, no PII):** ``GET /ClientPortal2/Geo/GetClubList`` →
  array of ``{Id, Name, Address, City:{Name}, ClubNumber, Latitude, Longitude,
  OpeningDate, StateId}``. Occupancy has no club-id, so this is joined *by name*
  for ids/geo. ``StateId`` is an opaque grouping key — state comes from
  :func:`revo_client.state_for_club`, not from it. Cached with the long
  ``CLUB_DIR_TTL_SECONDS`` (directory ≈ static), separate from the 60s occupancy
  cache.

Import-safe: ``requests`` is imported lazily so the bot boots without it — check
:func:`available` (or catch :class:`PerfectGymUnavailable`) before use.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import revo_client  # for state_for_club() — the primary state source

LOG = logging.getLogger("gymbot.revo.perfectgym")

# PerfectGym white-label client portal for Revo. Same backend the iOS app uses
# for its live Member-in-club counter.
PERFECTGYM_BASE = "https://revofitness.perfectgym.com/ClientPortal2"
LOGIN_PATH = "/Auth/Login"
OCCUPANCY_PATH = "/Clubs/Clubs/GetMembersInClubs"
# Public, no-PII club directory (id + geo + opening date). The occupancy board
# above carries no club-id, so we join to this directory *by name* for ids/geo.
GEO_CLUBLIST_PATH = "/Geo/GetClubList"

REQUEST_TIMEOUT = 30

# The live counter moves slowly and a single GET returns every club, so cache
# it for a minute — a burst of /busy calls (from anyone) then reuses one fetch
# instead of re-hitting PerfectGym. Mirrors revo_client.CLUB_COUNTER_TTL_SECONDS.
OCCUPANCY_TTL_SECONDS = 60

# The club *directory* (names/ids/geo/opening dates) barely changes — a club
# opens every few months — so it gets a MUCH longer TTL than the 60s occupancy
# cache. Kept as a separate cache so a directory fetch never evicts (or is
# evicted by) the fast-moving occupancy numbers.
CLUB_DIR_TTL_SECONDS = 6 * 3600

# Static, non-secret request identity. Content-Type must be JSON for the login
# POST; the Origin/Referer mirror the SPA the endpoint expects.
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": "gym-discord-bot/0.1 (+https://github.com/Poshy163/gym-discord-bot)",
    "Origin": "https://revofitness.perfectgym.com",
    "Referer": "https://revofitness.perfectgym.com/ClientPortal2/",
}

# Env vars for the shared read-only account (same account revo_client uses) so
# /busy keeps working for users who haven't linked their own credentials.
_ENV_USER = "REVO_USER"
_ENV_PASS = "REVO_PASS"

# Australian state/territory tokens, longest-first so "NSW"/"VIC" win over a
# shorter accidental match when scanning an address tail.
_AU_STATES = ("NSW", "VIC", "QLD", "TAS", "ACT", "SA", "WA", "NT")


class PerfectGymUnavailable(RuntimeError):
    """Raised when an optional dependency is missing or auth is unconfigured."""


class PerfectGymAuthError(RuntimeError):
    """Raised when the PerfectGym login (or re-login) fails."""


try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]


def available() -> bool:
    """True when the optional ``requests`` dep is importable."""
    return requests is not None


# ---------------------------------------------------------------------------
# Parser (pure, secret-scrubbing boundary — easy to unit test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClubOccupancy:
    """Live occupancy for one club — all public info.

    Deliberately carries no ids/tokens/PII from the login: only the club name,
    a best-effort suburb + state, the live head-count, and the capacity (which
    the backend leaves ``null`` for most clubs).
    """
    name: str
    suburb: Optional[str]
    state: Optional[str]
    count: int
    capacity: Optional[int]


def _as_obj(payload: Any) -> Any:
    """Accept an already-decoded object or a JSON string; ``None`` on garbage."""
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, (str, bytes)):
        try:
            return json.loads(payload)
        except (ValueError, TypeError):
            return None
    return None


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a live count to int (a real ``0`` is meaningful — closed/overnight)."""
    if isinstance(value, bool):  # bool is an int subclass; treat as garbage
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return default


def _as_opt_int(value: Any) -> Optional[int]:
    """Coerce a capacity to int, preserving ``None`` (most clubs have no limit)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_opt_float(value: Any) -> Optional[float]:
    """Coerce a lat/lng to float, preserving ``None`` (a not-yet-geocoded club)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def _as_opt_str(value: Any) -> Optional[str]:
    """Return a stripped non-empty string, else ``None`` (whitespace is missing)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# Address tails only *sometimes* carry a "<Suburb> <STATE> <postcode>" pattern
# (only ~14/78 clubs do); most are bare street lines. So state_for_club(name) is
# the primary source and this regex is the fallback for the rest.
_ADDR_STATE_RE = re.compile(
    r"\b(NSW|VIC|QLD|TAS|ACT|SA|WA|NT)\b\.?\s*\d{0,4}\.?\s*$",
    re.IGNORECASE,
)


def _suburb_state_from_address(address: Any) -> tuple[Optional[str], Optional[str]]:
    """Best-effort ``(suburb, state)`` from the tail of a ``ClubAddress``.

    Returns ``(None, None)`` when the address doesn't end in a recognisable
    ``<Suburb> <STATE> <postcode?>`` tail. The suburb is the last
    comma-delimited segment before the state token, accepted only if it reads
    like a place name (no digits) — otherwise the caller falls back to the club
    name (Revo clubs are named after their suburb).
    """
    if not isinstance(address, str) or not address.strip():
        return None, None
    m = _ADDR_STATE_RE.search(address)
    if not m:
        return None, None
    state = m.group(1).upper()
    before = address[: m.start()].rstrip(" ,.")
    if not before:
        return None, state
    segment = before.split(",")[-1].strip(" .")
    # Reject street-y segments ("976 North East Road") — a suburb has no digits
    # and is short; fall back to the club name in that case (suburb=None here).
    if not segment or any(ch.isdigit() for ch in segment) or len(segment.split()) > 4:
        return None, state
    return segment, state


def parse_members_in_clubs(payload: Any) -> list[ClubOccupancy]:
    """Parse ``Clubs/Clubs/GetMembersInClubs`` into a list of :class:`ClubOccupancy`.

    Accepts the raw ``{"UsersInClubList": [...]}`` dict, a bare list, or a JSON
    string. Unknown / malformed entries are skipped. Zero counts are kept (a
    club can genuinely have 0 members overnight — that's not "missing data").
    """
    obj = _as_obj(payload)
    if isinstance(obj, dict):
        entries = obj.get("UsersInClubList")
    elif isinstance(obj, list):
        entries = obj
    else:
        entries = None
    if not isinstance(entries, list):
        return []

    out: list[ClubOccupancy] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("ClubName")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        suburb_addr, state_addr = _suburb_state_from_address(entry.get("ClubAddress"))
        # Primary: our curated name→state directory; fallback: the parsed tail.
        state = revo_client.state_for_club(name) or state_addr
        # Suburb from the address when clean, else the club name (== suburb for
        # Revo's naming), so this field is always populated with something useful.
        suburb = suburb_addr or name
        out.append(
            ClubOccupancy(
                name=name,
                suburb=suburb,
                state=state,
                count=_as_int(entry.get("UsersCountCurrentlyInClub")),
                capacity=_as_opt_int(entry.get("UsersLimit")),
            )
        )
    return out


def find_club(clubs: list[ClubOccupancy], query: str) -> Optional[ClubOccupancy]:
    """Case-insensitive lookup over the occupancy list (mirrors ``revo_client.find_club``).

    Tries exact name, then prefix, then substring — over both the club name and
    its derived suburb (so a user can type the suburb of a club that's named
    slightly differently).
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    def keys(c: ClubOccupancy) -> tuple[str, ...]:
        return tuple(k.lower() for k in (c.name, c.suburb or "") if k)

    for c in clubs:  # exact
        if any(k == q for k in keys(c)):
            return c
    for c in clubs:  # prefix
        if any(k.startswith(q) for k in keys(c)):
            return c
    for c in clubs:  # substring
        if any(q in k for k in keys(c)):
            return c
    return None


def top_busiest(
    clubs: list[ClubOccupancy],
    limit: int = 5,
    state: Optional[str] = None,
) -> list[ClubOccupancy]:
    """Return the ``limit`` busiest clubs, highest live count first.

    Optionally scoped to a single ``state`` (case-insensitive AU code). Ties
    break on club name so the ordering is stable/deterministic.
    """
    items = list(clubs)
    if state:
        st = state.strip().upper()
        items = [c for c in items if (c.state or "").upper() == st]
    items.sort(key=lambda c: (-c.count, c.name.lower()))
    return items[: max(0, limit)]


# ---------------------------------------------------------------------------
# Club directory + geo (public, no PII) — Geo/GetClubList
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClubDirEntry:
    """One club from the public ``Geo/GetClubList`` directory — no PII.

    Every field here is public marketing/geo data (the same list the "find a
    club" map draws from): the club's stable ``id``, name, street address, city,
    marketing ``club_number``, latitude/longitude, ISO opening date, and the
    derived AU ``state`` code. Carries nothing from the login profile.
    """
    id: Optional[int]
    name: str
    address: Optional[str]
    city: Optional[str]
    club_number: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    opening_date: Optional[str]
    state: Optional[str]


def _city_name(value: Any) -> Optional[str]:
    """City may arrive as ``{"Name": ...}`` (current shape) or a bare string."""
    if isinstance(value, dict):
        return _as_opt_str(value.get("Name"))
    return _as_opt_str(value)


def parse_club_list(payload: Any) -> list[ClubDirEntry]:
    """Parse ``Geo/GetClubList`` into a list of :class:`ClubDirEntry` (public data).

    Accepts the raw array, a ``{"...": [...]}`` wrapper, or a JSON string. The
    ``state`` is derived from :func:`revo_client.state_for_club` (the bot's single
    curated name→state source) — the payload's numeric ``StateId`` is only an
    internal grouping key with no public code mapping, so it is intentionally
    ignored. Entries without a usable ``Name`` are skipped; missing lat/lng are
    preserved as ``None`` (some just-announced clubs aren't geocoded yet).
    """
    obj = _as_obj(payload)
    if isinstance(obj, dict):
        # Tolerate a wrapped array under any common key.
        entries = obj.get("ClubList") or obj.get("Clubs") or obj.get("Data")
    elif isinstance(obj, list):
        entries = obj
    else:
        entries = None
    if not isinstance(entries, list):
        return []

    out: list[ClubDirEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _as_opt_str(entry.get("Name"))
        if not name:
            continue
        out.append(
            ClubDirEntry(
                id=_as_opt_int(entry.get("Id")),
                name=name,
                address=_as_opt_str(entry.get("Address")),
                city=_city_name(entry.get("City")),
                club_number=_as_opt_str(entry.get("ClubNumber")),
                lat=_as_opt_float(entry.get("Latitude")),
                lng=_as_opt_float(entry.get("Longitude")),
                opening_date=_as_opt_str(entry.get("OpeningDate")),
                # Primary + only source, consistent with parse_members_in_clubs.
                state=revo_client.state_for_club(name),
            )
        )
    return out


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in km between two lat/lng points (mean Earth radius).

    Pure and dependency-free — enough for "which clubs are near this one", where a
    few-hundred-metres error over city distances is irrelevant.
    """
    r = 6371.0088  # mean Earth radius (km)
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlmb = math.radians(b_lng - a_lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def nearest_clubs(
    entries: list[ClubDirEntry],
    origin_name: str,
    limit: int = 5,
) -> list[ClubDirEntry]:
    """Clubs sorted by distance from the named origin club (closest first).

    Distance is measured **between clubs** in the directory — there is no external
    geocoder, so ``origin_name`` must match a directory entry (case-insensitive).
    The origin itself and any entry missing coordinates are excluded. Returns an
    empty list when the origin is unknown or has no coordinates. Ties break on
    name for a stable ordering.
    """
    q = (origin_name or "").strip().lower()
    if not q:
        return []
    origin = next((e for e in entries if e.name.lower() == q), None)
    if origin is None or origin.lat is None or origin.lng is None:
        return []
    scored: list[tuple[float, str, ClubDirEntry]] = []
    for e in entries:
        if e is origin or e.lat is None or e.lng is None:
            continue
        d = haversine_km(origin.lat, origin.lng, e.lat, e.lng)
        scored.append((d, e.name.lower(), e))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored[: max(0, limit)]]


@dataclass(frozen=True)
class LocatedOccupancy:
    """A :class:`ClubOccupancy` enriched with directory id/geo/opening date.

    The occupancy board has no club-id, so :func:`join_occupancy_to_dir` attaches
    the directory's ``id``/``lat``/``lng``/``opening_date`` by name. Still no PII —
    both inputs are public. Unmatched occupancy rows keep ``None`` geo fields.
    """
    occupancy: ClubOccupancy
    id: Optional[int]
    lat: Optional[float]
    lng: Optional[float]
    opening_date: Optional[str]


def join_occupancy_to_dir(
    occupancy: list[ClubOccupancy],
    directory: list[ClubDirEntry],
) -> list[LocatedOccupancy]:
    """Attach directory id/geo/opening-date to each occupancy row by club name.

    Matching is case-insensitive on the club name. Every occupancy row is kept, in
    order; an unmatched row is returned "as-is" (wrapped with ``None`` geo fields)
    so a caller never loses a live count just because the directory lacks the club.
    """
    by_name = {e.name.lower(): e for e in directory}
    out: list[LocatedOccupancy] = []
    for occ in occupancy:
        e = by_name.get(occ.name.lower())
        out.append(
            LocatedOccupancy(
                occupancy=occ,
                id=e.id if e else None,
                lat=e.lat if e else None,
                lng=e.lng if e else None,
                opening_date=e.opening_date if e else None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Membership status (non-sensitive slice of the login profile)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MembershipStatus:
    """The non-sensitive membership summary from the login profile.

    Deliberately a *narrow* projection of ``User.Member.NotificationsData`` — it
    carries only the contract health flags a member sees on the portal dashboard
    and NOTHING that identifies them: no UserNumber/barcode, no email, no photo,
    no ids, no token. Safe to log or embed. ``payment_ok`` is the inverse of the
    backend's ``HasInvalidContractPaymentMethod`` (portal shows the positive).
    """
    contract_status: Optional[str]
    payment_ok: Optional[bool]
    has_card: Optional[bool]


def _as_opt_bool(value: Any) -> Optional[bool]:
    """Return a real bool as-is, else ``None`` (a missing flag is unknown, not False)."""
    return value if isinstance(value, bool) else None


def parse_membership_status(profile: Any) -> MembershipStatus:
    """Project a login profile's ``NotificationsData`` into a :class:`MembershipStatus`.

    Never raises: a missing/garbage profile or absent ``NotificationsData`` yields
    an all-``None`` status (unknown, not "bad"). ``payment_ok`` inverts
    ``HasInvalidContractPaymentMethod`` so a missing flag stays ``None`` rather than
    silently reading as "payment ok".
    """
    member = _member(_as_obj(profile))
    nd = member.get("NotificationsData") if isinstance(member, dict) else None
    if not isinstance(nd, dict):
        return MembershipStatus(None, None, None)
    has_invalid = _as_opt_bool(nd.get("HasInvalidContractPaymentMethod"))
    return MembershipStatus(
        contract_status=_as_opt_str(nd.get("ContractStatus")),
        payment_ok=(not has_invalid) if has_invalid is not None else None,
        has_card=_as_opt_bool(nd.get("HasMemberCardAssigned")),
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

def _member(body: Any) -> Optional[dict]:
    """Return the ``User.Member`` profile dict, or ``None`` on any wrong shape.

    Central navigator so the (non-secret) HomeClubId reader, the membership-status
    projection and the (sensitive) card-number reader all agree on where the
    profile lives without each re-walking the nesting.
    """
    if not isinstance(body, dict):
        return None
    user = body.get("User")
    if not isinstance(user, dict):
        return None
    member = user.get("Member")
    return member if isinstance(member, dict) else None


def _home_club_id(body: Any) -> Optional[int]:
    """Pull the non-secret ``User.Member.HomeClubId`` int from a login profile.

    Everything else in the profile (name / email / photo / member id) is PII and
    is deliberately ignored — only this integer is kept.
    """
    member = _member(body)
    if member is None:
        return None
    hc = member.get("HomeClubId")
    return hc if isinstance(hc, int) and not isinstance(hc, bool) else None


def _first_name(body: Any) -> Optional[str]:
    """Pull the non-secret ``User.Member.FirstName`` display name from a profile.

    A plain first name is not an access credential — it is the value we use to
    auto-populate the bot's display nickname on ``/revo_link`` and to label the
    ``/seeprofile`` roster. Returns ``None`` for a missing/blank/garbage profile.
    """
    member = _member(body)
    if member is None:
        return None
    fn = member.get("FirstName")
    return fn.strip() if isinstance(fn, str) and fn.strip() else None


def _photo_url(body: Any) -> Optional[str]:
    """Pull ``User.Member.PhotoUrl`` — a SIGNED, short-lived CDN capability URL.

    !!! SENSITIVE (capability URL) !!! The ``&sig=`` query param grants ~10 minutes
    of *unauthenticated* read access to the member's photo; holding the URL is
    enough to fetch the image. It MUST NEVER be logged or persisted — it is stashed
    privately and surfaced only by :meth:`PerfectGymClient.get_photo_url` so the
    bytes can be downloaded immediately (within the signature's validity).
    """
    member = _member(body)
    if member is None:
        return None
    pu = member.get("PhotoUrl")
    return pu.strip() if isinstance(pu, str) and pu.strip() else None


def _user_number(body: Any) -> Optional[str]:
    """Extract ``User.Member.UserNumber`` — the physical gym-entry BARCODE.

    !!! SENSITIVE — this is an ACCESS CREDENTIAL, not just an id !!!
    ``UserNumber`` is the number encoded on the member's card/turnstile barcode;
    possessing it is enough to walk through a Revo door. It is the ONE sensitive
    value :meth:`PerfectGymClient.get_card_number` is allowed to surface, and only
    for the ephemeral ``/revo_card`` reply. It MUST NEVER be logged, cached to
    disk, put in any other dataclass/repr, or returned by any other method.
    """
    member = _member(body)
    if member is None:
        return None
    un = member.get("UserNumber")
    if isinstance(un, str) and un.strip():
        return un.strip()
    if isinstance(un, int) and not isinstance(un, bool):
        return str(un)
    return None


def _needs_relogin(response: Any) -> bool:
    """True when a data GET indicates the session lapsed (401 or a redirect).

    A live session returns ``200`` JSON; PerfectGym bounces an expired session
    to the auth flow via a ``3xx`` redirect (we send ``allow_redirects=False``)
    or a ``401``.
    """
    status = getattr(response, "status_code", None)
    if status == 401:
        return True
    return isinstance(status, int) and 300 <= status < 400


class PerfectGymClient:
    """Read-only session against Revo's PerfectGym ClientPortal2 backend.

    Only POSTs the login (per the project's read-only rule); occupancy is a GET.
    Thread-safe — an internal lock serialises login retries so a burst of
    concurrent ``/busy`` calls doesn't fire N parallel logins on cookie expiry.
    Holds the ``CpAuthToken`` cookie inside the private session and never exposes
    it; only the non-secret :attr:`home_club_id` is surfaced.
    """

    def __init__(self, email: str, password: str) -> None:
        if requests is None:
            raise PerfectGymUnavailable(
                "The 'requests' package is required for the PerfectGym client."
            )
        self.email = email
        self._password = password
        self._http = requests.Session()
        self._http.headers.update(DEFAULT_HEADERS)
        self._lock = threading.Lock()
        self._logged_in = False
        # Non-secret session context (the only *public* thing kept from the
        # profile). home_club_id is a plain int; everything below is private.
        self.home_club_id: Optional[int] = None
        # Non-sensitive membership summary (no PII) — served by
        # get_membership_status(); private so it stays off the public surface.
        self._membership: Optional[MembershipStatus] = None
        # SENSITIVE: the entry barcode (UserNumber). Private, never logged, only
        # ever handed out by get_card_number() for the ephemeral /revo_card path.
        self._user_number: Optional[str] = None
        # Non-secret display name from the profile — served by get_first_name(),
        # used to auto-name the member in the bot on /revo_link + /seeprofile.
        self._first_name: Optional[str] = None
        # SENSITIVE (capability URL): the signed, ~10-min PhotoUrl. Private, NEVER
        # logged; only get_photo_url() hands it out (refresh=True re-logs in for a
        # fresh signature) so /seeprofile can download the bytes right away.
        self._photo_url: Optional[str] = None

    # ---- auth ----------------------------------------------------------

    def login(self) -> None:
        """POST JSON credentials → ``CpAuthToken`` cookie + stash home_club_id.

        Raises :class:`PerfectGymAuthError` on failure. Never logs the response
        body (member profile PII) or the token cookie.
        """
        with self._lock:
            self._login_locked()

    def _login_locked(self) -> None:
        r = self._http.post(
            PERFECTGYM_BASE + LOGIN_PATH,
            data=json.dumps(
                {"RememberMe": False, "Login": self.email, "Password": self._password}
            ),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            body = r.json()
        except ValueError:
            body = None
        has_token = any(c.name == "CpAuthToken" for c in self._http.cookies)
        has_profile = isinstance(body, dict) and isinstance(body.get("User"), dict)
        if r.status_code != 200 or not (has_token or has_profile):
            raise PerfectGymAuthError(
                f"PerfectGym login failed for {self.email!r} (status {r.status_code})."
            )
        self.home_club_id = _home_club_id(body)
        # Stash the two profile projections we serve later, straight off this
        # login body (the confirmed contract: the login response *is* the
        # profile). The membership summary is non-sensitive; the UserNumber is
        # the sensitive entry barcode kept private for get_card_number().
        self._membership = parse_membership_status(body)
        self._user_number = _user_number(body)
        # Non-secret first name + the signed (sensitive) photo URL. Re-read on
        # every login so a refresh=True call refreshes the ~10-min photo signature.
        self._first_name = _first_name(body)
        self._photo_url = _photo_url(body)
        self._logged_in = True
        # Non-secret log line only: email + the home-club integer. Never the
        # token, the profile body, the membership flags, or the barcode.
        LOG.info(
            "PerfectGym login OK email=%s home_club_id=%s",
            self.email, self.home_club_id,
        )

    def _do_get(self, path: str) -> Any:
        return self._http.get(
            PERFECTGYM_BASE + path,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )

    def _get_json(self, path: str) -> Any:
        """GET ``path`` as JSON, re-logging in once on session expiry."""
        if not self._logged_in:
            self.login()
        r = self._do_get(path)
        if _needs_relogin(r):
            LOG.info("PerfectGym session expired, re-logging in")
            self._logged_in = False
            self.login()
            r = self._do_get(path)
            if _needs_relogin(r):
                raise PerfectGymAuthError(
                    "PerfectGym session could not be re-established "
                    f"(status {getattr(r, 'status_code', '?')})."
                )
        r.raise_for_status()
        return r.json()

    # ---- public read endpoints ----------------------------------------

    def get_club_occupancy(self) -> list[ClubOccupancy]:
        """Return live occupancy for every club (one backend call)."""
        return parse_members_in_clubs(self._get_json(OCCUPANCY_PATH))

    def get_club_list(self) -> list[ClubDirEntry]:
        """Return the public club directory (ids + geo + opening dates), no PII.

        This is the raw fetch; the long-TTL caching lives at module level in
        :func:`shared_club_list` / :func:`club_list_with_client`
        (``CLUB_DIR_TTL_SECONDS``), kept separate from the 60s occupancy cache
        because the directory barely changes.
        """
        return parse_club_list(self._get_json(GEO_CLUBLIST_PATH))

    def get_membership_status(self) -> MembershipStatus:
        """Return the non-sensitive membership summary (contract/payment/card).

        Sourced from the login profile stashed at auth time; logs in first if the
        session isn't established yet. Contains no PII, no barcode, no token.
        """
        if not self._logged_in:
            self.login()
        return self._membership or MembershipStatus(None, None, None)

    def get_card_number(self) -> Optional[str]:
        """Return the member's entry BARCODE (``UserNumber``) — SENSITIVE.

        !!! This is the ONE sensitive value a public method may return. !!!
        It is a physical access credential (what a Revo turnstile scans), intended
        ONLY for the ephemeral ``/revo_card`` reply where bot.py renders it to a
        barcode image. The caller MUST NOT log it, persist it, or place it in any
        embed/message that survives. No other method exposes it, and it is kept out
        of every dataclass/repr in this module.
        """
        if not self._logged_in:
            self.login()
        return self._user_number

    def get_first_name(self) -> Optional[str]:
        """Return the member's non-secret first name from the login profile.

        Logs in first if the session isn't established yet. Used to auto-populate
        the bot's display nickname on ``/revo_link`` and to label the
        ``/seeprofile`` roster. Not an access credential — safe to display.
        """
        if not self._logged_in:
            self.login()
        return self._first_name

    def get_photo_url(self, refresh: bool = False) -> Optional[str]:
        """Return the member's SIGNED profile-photo URL — a capability URL.

        !!! Capability URL — the caller MUST NOT log or persist it. !!! The
        ``&sig=`` param grants ~10 minutes of unauthenticated read to the photo,
        so the caller must download the bytes *immediately* (see
        :func:`download_photo`) and let the URL expire.

        Because the signature is short-lived, pass ``refresh=True`` to force a
        fresh login first — that regenerates a currently-valid URL. With
        ``refresh=False`` this returns whatever was stashed at the last login,
        which may already have expired.
        """
        if refresh:
            # Force a fresh login so the returned signature is currently valid.
            # Mirrors the _get_json expiry path: flip the flag, then re-auth.
            self._logged_in = False
            self.login()
        elif not self._logged_in:
            self.login()
        return self._photo_url


def download_photo(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    """Download the bytes behind a signed, short-lived member-photo URL.

    The ``&sig=`` query param *is* the credential, so this is an unauthenticated
    GET (no ``CpAuthToken`` cookie needed) against the CDN host. Callers MUST
    invoke it immediately after ``get_photo_url(refresh=True)`` — within the
    ~10-minute signature validity — and MUST NOT log the ``url`` (a capability
    URL) or the returned bytes. Raises :class:`PerfectGymUnavailable` when the
    optional ``requests`` dep is missing; propagates transport/HTTP errors to the
    caller (which catches them WITHOUT logging the URL-bearing message).
    """
    if requests is None:
        raise PerfectGymUnavailable(
            "The 'requests' package is required to download member photos."
        )
    # A bare Session (not the authenticated client session) — the signature is
    # the only credential the CDN needs, and we don't want the CpAuthToken cookie
    # riding along to a third-party storage host.
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# Module-level "shared" client + TTL cache for read-only commands like /busy.
# ---------------------------------------------------------------------------

_shared_lock = threading.Lock()
_shared_client: Optional[PerfectGymClient] = None


@dataclass
class _OccupancyCache:
    fetched_at: float = 0.0
    clubs: tuple[ClubOccupancy, ...] = field(default_factory=tuple)


@dataclass
class _DirectoryCache:
    fetched_at: float = 0.0
    entries: tuple[ClubDirEntry, ...] = field(default_factory=tuple)


_shared_occupancy = _OccupancyCache()
# Separate from occupancy: the directory changes on the order of months, so it
# gets its own slot and the long CLUB_DIR_TTL_SECONDS TTL.
_shared_directory = _DirectoryCache()


def shared_client_from_env() -> PerfectGymClient:
    """Build (and cache) a :class:`PerfectGymClient` from ``REVO_USER`` /
    ``REVO_PASS``. Used by anonymous read-only commands (keeps /busy working for
    users who haven't linked their own credentials).
    """
    global _shared_client
    if not available():
        raise PerfectGymUnavailable(
            "Install 'requests' to enable Revo features (pip install requests)."
        )
    with _shared_lock:
        if _shared_client is None:
            email = os.environ.get(_ENV_USER, "").strip()
            password = os.environ.get(_ENV_PASS, "").strip()
            if not email or not password:
                raise PerfectGymUnavailable(
                    "Set REVO_USER and REVO_PASS to enable shared Revo access "
                    "(used by /busy)."
                )
            _shared_client = PerfectGymClient(email, password)
        return _shared_client


def _cache_get(now: float) -> Optional[list[ClubOccupancy]]:
    with _shared_lock:
        cache = _shared_occupancy
        if cache.clubs and (now - cache.fetched_at) < OCCUPANCY_TTL_SECONDS:
            return list(cache.clubs)
    return None


def _cache_put(now: float, clubs: list[ClubOccupancy]) -> None:
    # Only cache a non-empty fetch: caching an empty list would wedge /busy on
    # "unavailable" for the whole TTL even after the backend recovers.
    if not clubs:
        return
    global _shared_occupancy
    with _shared_lock:
        _shared_occupancy = _OccupancyCache(fetched_at=now, clubs=tuple(clubs))


def shared_club_occupancy() -> list[ClubOccupancy]:
    """Cached all-clubs occupancy via the shared env account."""
    now = time.monotonic()
    cached = _cache_get(now)
    if cached is not None:
        return cached
    clubs = shared_client_from_env().get_club_occupancy()
    _cache_put(now, clubs)
    return clubs


def club_occupancy_with_client(client: PerfectGymClient) -> list[ClubOccupancy]:
    """Cached all-clubs occupancy using *any* authenticated client.

    Unlike the rewards landing (which is per-account and must not be shared),
    the occupancy board is the *same public all-clubs list for everyone*, so a
    per-user fetch safely populates — and reuses — the shared TTL cache.
    """
    now = time.monotonic()
    cached = _cache_get(now)
    if cached is not None:
        return cached
    clubs = client.get_club_occupancy()
    _cache_put(now, clubs)
    return clubs


def _dir_cache_get(now: float) -> Optional[list[ClubDirEntry]]:
    with _shared_lock:
        cache = _shared_directory
        if cache.entries and (now - cache.fetched_at) < CLUB_DIR_TTL_SECONDS:
            return list(cache.entries)
    return None


def _dir_cache_put(now: float, entries: list[ClubDirEntry]) -> None:
    # Same guard as occupancy: never cache an empty fetch (it would wedge every
    # directory read on "unavailable" for the whole 6h TTL).
    if not entries:
        return
    global _shared_directory
    with _shared_lock:
        _shared_directory = _DirectoryCache(fetched_at=now, entries=tuple(entries))


def shared_club_list() -> list[ClubDirEntry]:
    """Cached public club directory via the shared env account (6h TTL).

    The directory is public (no PII), so unlike the per-account rewards landing it
    is safe to share-cache across all callers.
    """
    now = time.monotonic()
    cached = _dir_cache_get(now)
    if cached is not None:
        return cached
    entries = shared_client_from_env().get_club_list()
    _dir_cache_put(now, entries)
    return entries


def club_list_with_client(client: PerfectGymClient) -> list[ClubDirEntry]:
    """Cached public club directory using *any* authenticated client (6h TTL).

    The directory is the same public list for everyone, so a per-user fetch safely
    populates — and reuses — the shared long-TTL cache.
    """
    now = time.monotonic()
    cached = _dir_cache_get(now)
    if cached is not None:
        return cached
    entries = client.get_club_list()
    _dir_cache_put(now, entries)
    return entries
