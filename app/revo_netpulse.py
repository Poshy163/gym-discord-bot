"""Revo Fitness mobile-backend (Netpulse / EGYM) client.

Revo's phone app (``com.netpulse.mobile.revofitness``) does **not** talk to the
``revocentral`` web portal that :mod:`app.revo_client` scrapes. It talks to a
**Netpulse (EGYM) white-label** backend at ``https://revofitness.netpulse.com/np/``.
This module is the thin, read-only client for the small slice of that backend
that is actually useful to the bot.

Why this exists / what you can (and can't) get from it
------------------------------------------------------
The web portal lost live occupancy when ``club-counter.php`` was access-guarded,
so the Netpulse backend looked like the way back to real per-club headcounts and
per-visit check-ins. A single consented login test settled it:

* **Occupancy (`gym-busyness`) and check-in history are NOT provisioned for
  Revo's tenant.** ``gym-busyness`` returns ``{"message":"The requested
  resource does not exist."}`` and ``check-ins/history`` returns
  ``{"checkIns": []}``. Every club in the directory reports ``"mms":
  "perfectgym"`` — Revo runs member management / access / occupancy on
  **PerfectGym**, not on Netpulse, so those Netpulse endpoints are dark for this
  tenant. Neither can be restored here.
* **What IS provisioned:** the member's **membership** (type / subtype / join
  date) and a full **club directory** (name, suburb/state, hours, geo). That's
  what this client exposes.

Auth (correction to the old web-portal notes)
---------------------------------------------
Netpulse auth is a **form-POST credential login**, not an opaque bearer token:
``POST /np/exerciser/login`` with ``username`` + ``password`` sets a
``JSESSIONID`` cookie and returns the exerciser ``uuid``. Subsequent reads reuse
that cookie — the *same* session-cookie pattern :mod:`app.revo_client` already
uses for the web portal. Static ``X-NP-*`` headers identify the app; no phone
TLS interception is required.

Security (hard rules — mirror :mod:`app.revo_client`)
-----------------------------------------------------
The login and membership responses carry **secrets**: the ``JSESSIONID``
cookie, ``externalAuthToken`` / ``externalIdToken`` / ``externalRefreshToken``,
``egymAccountId``, and a membership ``barcode`` / ``agreementNumber`` /
``barcodeExpiresAt`` (a live digital door-access credential). This client MUST
NOT log them, MUST NOT return them from public methods, and MUST NOT persist
them. The pure parsers below are the scrubbing boundary: they read the raw
payload but return only non-sensitive fields.

Import-safe: ``requests`` is imported lazily so the bot boots without it — check
:func:`available` (or catch :class:`NetpulseUnavailable`) before use.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Optional

LOG = logging.getLogger("gymbot.revo.netpulse")

# Netpulse (EGYM) white-label backend for Revo's app. Reachable; only the
# membership + club-directory slice is provisioned for Revo's tenant.
NETPULSE_BASE = "https://revofitness.netpulse.com/np/"
LOGIN_PATH = "exerciser/login"
CLUBS_PATH = "company/children?responseType=basic"
MEMBERSHIP_PATH = "exerciser/{uuid}/membership"

REQUEST_TIMEOUT = 30

# Static app-identity headers (non-secret) captured from the Revo Netpulse app.
# The deviceUid is intentionally blank — we don't register a device.
DEFAULT_HEADERS = {
    "User-Agent": "okhttp/3.12.3",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Connection": "Keep-Alive",
    "X-NP-API-Version": "1.5",
    "X-NP-App-Version": "9999",
    "X-NP-User-Agent": (
        "clientType=MOBILE_DEVICE; devicePlatform=ANDROID; deviceUid=; "
        "applicationName=Revo Fitness; applicationVersion=9999; "
        "applicationVersionCode=9999"
    ),
}


class NetpulseUnavailable(RuntimeError):
    """Raised when an optional dependency is missing or auth is unconfigured."""


class NetpulseAuthError(RuntimeError):
    """Raised when the Netpulse login fails (bad credentials, etc.)."""


try:  # pragma: no cover - trivial import guard
    import requests  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]


def available() -> bool:
    """True when the optional ``requests`` dep is importable."""
    return requests is not None


# ---------------------------------------------------------------------------
# Parsers (pure, secret-scrubbing boundary — easy to unit test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Membership:
    """Non-sensitive membership facts.

    Deliberately omits every secret the raw payload carries (``barcode``,
    ``agreementNumber``, ``barcodeExpiresAt``, tokens) — those never leave the
    client.
    """
    membership_type: Optional[str]      # e.g. "Basic"
    membership_subtype: Optional[str]   # e.g. "Level 2"
    join_date: Optional[str]            # YYYY-MM-DD (contract-signed / created)
    expired: Optional[bool]


@dataclass(frozen=True)
class DayHours:
    """One weekday's hours for a club, parsed out of Revo's free-text cell.

    ``always_open`` is the common case — 61 of 77 clubs are 24/7 — and when it is
    True the ``open_*`` window is meaningless and left ``None``. ``staffed_*`` is a
    *separate* fact that rides along in the same cell ("Open 24 Hours\\nStaffed from
    9am - 8pm"): the gym is accessible by card, but reception is only attended for
    part of it. Any field may be ``None`` when that fact wasn't stated.
    """
    always_open: bool
    open_from: Optional[time]
    open_to: Optional[time]
    staffed_from: Optional[time]
    staffed_to: Optional[time]
    raw: str


@dataclass(frozen=True)
class Club:
    """A club from the Netpulse company directory. All fields are public info.

    This directory is the **richest public club record Revo publishes** and — unlike
    everything else on this backend — it needs no credentials at all (see
    :func:`fetch_club_directory`). It is the only source for club **opening hours**,
    a **phone number**, and the club's own **IANA timezone**, none of which appear
    in the PerfectGym directory.
    """
    uuid: Optional[str]
    name: Optional[str]
    city: Optional[str]
    state: Optional[str]
    mms: Optional[str]   # member-management system, e.g. "perfectgym"
    url: Optional[str]
    #: IANA zone (e.g. ``Australia/Perth``) — populated for every club, and the
    #: only correct basis for "is it open *right now*" across four AU timezones.
    timezone: Optional[str] = None
    phone: Optional[str] = None
    postal_code: Optional[str] = None
    street: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    #: ``{"Mon": DayHours, …}`` — only the days the payload described.
    hours: dict[str, DayHours] = field(default_factory=dict)


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


def _date_only(value: Any) -> Optional[str]:
    """Take the ``YYYY-MM-DD`` prefix of an ISO datetime string, if present."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    head = value[:10]
    return head if head[4] == "-" and head[7] == "-" else None


def parse_membership(payload: Any) -> Membership:
    """Scrub a ``exerciser/{uuid}/membership`` payload to non-secret fields.

    The join date prefers ``contractSignedDate`` and falls back to ``createdAt``
    (both render midnight-local for this tenant, so we keep the date only).
    """
    obj = _as_obj(payload)
    if not isinstance(obj, dict):
        return Membership(None, None, None, None)
    join = _date_only(obj.get("contractSignedDate")) or _date_only(
        obj.get("createdAt")
    )
    expired = obj.get("expired")
    return Membership(
        membership_type=obj.get("membershipType"),
        membership_subtype=obj.get("membershipSubtype"),
        join_date=join,
        expired=bool(expired) if isinstance(expired, bool) else None,
    )


# Weekday keys as the payload spells them, in week order.
WEEKDAYS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Hours arrive as human copy, not structured data. Every distinct shape present
# across all 77 clubs (539 day-cells), by frequency:
#   420x  "Open 24 Hours\nStaffed from 9am - 8pm"   (two facts, one cell)
#    14x  "00:00-23:59"                             (machine form of 24/7)
#    ~30x "Open from 5:30am - 10pm" / "Open 7:00am-7:00pm" / "Open from 6am - 9pm"
# and 10 clubs whose `workingHours` is null but whose `workingHoursFreeText` says
# the same thing in HTML. Anything outside these shapes must parse to "unknown"
# rather than a guess — a wrong "closed" would send someone home for nothing.
_24H_RE = re.compile(r"open\s*24\s*h(ou)?rs?|24/7", re.I)
_MIDNIGHT_SPAN_RE = re.compile(r"\b00:00\s*-\s*23:59\b")
_TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
_STAFFED_RE = re.compile(rf"staffed\s*(?:from\s*)?{_TIME_RE}\s*-\s*{_TIME_RE}", re.I)
_OPEN_RANGE_RE = re.compile(rf"open\s*(?:from\s*)?{_TIME_RE}\s*-\s*{_TIME_RE}", re.I)
_HHMM_RANGE_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\b")


def _clock(hour: str, minute: Optional[str], meridiem: str) -> Optional[time]:
    """Build a ``time`` from a 12-hour clock triple, or ``None`` if out of range."""
    try:
        h, m = int(hour), int(minute or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= h <= 12) or not (0 <= m <= 59):
        return None
    if meridiem.lower() == "p" and h != 12:
        h += 12
    elif meridiem.lower() == "a" and h == 12:
        h = 0  # 12am is midnight
    return time(hour=h % 24, minute=m)


def _strip_html(fragment: str) -> str:
    """Flatten the HTML variant of the hours copy, keeping block breaks as spaces."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse_day_hours(cell: Any) -> Optional[DayHours]:
    """Parse one weekday's free-text hours cell into a :class:`DayHours`.

    Returns ``None`` for an empty/garbage cell, and for a cell that states nothing
    we recognise — deliberately, so a caller can say "hours unknown" instead of
    inventing a window. HTML is tolerated so the same parser handles both
    ``workingHours`` cells and the ``workingHoursFreeText`` fallback.
    """
    if not isinstance(cell, str) or not cell.strip():
        return None
    raw = cell.strip()
    text = _strip_html(raw) if "<" in raw else re.sub(r"\s+", " ", raw)

    always = bool(_24H_RE.search(text) or _MIDNIGHT_SPAN_RE.search(text))

    staffed_from = staffed_to = None
    sm = _STAFFED_RE.search(text)
    if sm:
        staffed_from = _clock(sm.group(1), sm.group(2), sm.group(3))
        staffed_to = _clock(sm.group(4), sm.group(5), sm.group(6))

    open_from = open_to = None
    if not always:
        # Match the opening range on the text with any "Staffed …" clause removed,
        # so "Open 24 Hours / Staffed from 9am - 8pm" can never be misread as a
        # 9am-8pm *opening* window.
        without_staffed = _STAFFED_RE.sub(" ", text)
        om = _OPEN_RANGE_RE.search(without_staffed)
        if om:
            open_from = _clock(om.group(1), om.group(2), om.group(3))
            open_to = _clock(om.group(4), om.group(5), om.group(6))
        else:
            hm = _HHMM_RANGE_RE.search(without_staffed)
            if hm:
                try:
                    open_from = time(int(hm.group(1)) % 24, int(hm.group(2)))
                    open_to = time(int(hm.group(3)) % 24, int(hm.group(4)))
                except ValueError:
                    open_from = open_to = None

    if not always and open_from is None and staffed_from is None:
        return None  # nothing recognisable — "unknown", not "closed"
    return DayHours(
        always_open=always,
        open_from=open_from,
        open_to=open_to,
        staffed_from=staffed_from,
        staffed_to=staffed_to,
        raw=text,
    )


def parse_hours(entry: dict) -> dict[str, DayHours]:
    """Per-weekday hours for one directory entry.

    Prefers the structured-ish ``workingHours`` map and falls back to the HTML
    ``workingHoursFreeText`` (applied to every weekday) when that map is null —
    which recovers 7 of the 10 clubs whose ``workingHours`` is missing.
    """
    out: dict[str, DayHours] = {}
    wh = entry.get("workingHours")
    if isinstance(wh, dict):
        for day in WEEKDAYS:
            parsed = parse_day_hours(wh.get(day))
            if parsed is not None:
                out[day] = parsed
    if out:
        return out
    fallback = parse_day_hours(entry.get("workingHoursFreeText"))
    return {day: fallback for day in WEEKDAYS} if fallback is not None else {}


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# `postalCode` is entered by hand and is dirty in 33 of 77 rows: most carry a state
# prefix ("WA 6021"), several are a state with no code at all ("SA "), and one is a
# malformed 3-digit "WA 615". Normalise to a bare 4-digit AU postcode or None, so
# no caller ever renders "Modbury SA 5092 SA".
_POSTCODE_RE = re.compile(r"\b(\d{4})\b")


def _postcode(value: Any) -> Optional[str]:
    """A clean 4-digit AU postcode from a messy ``postalCode`` field, else None."""
    if not isinstance(value, str):
        return None
    m = _POSTCODE_RE.search(value)
    return m.group(1) if m else None


def parse_club_directory(payload: Any) -> list[Club]:
    """Parse ``company/children`` into a list of clubs (public data only).

    ``responseType`` turns out to be **ignored** by this endpoint — ``basic``,
    ``full`` and no parameter at all return byte-identical bodies — so every field
    below was always being returned and simply discarded.
    """
    obj = _as_obj(payload)
    if not isinstance(obj, list):
        return []
    clubs: list[Club] = []
    for entry in obj:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        city = state = postal = street = None
        lat = lng = None
        if isinstance(address, dict):
            city = address.get("city")
            # Only ~13/77 rows carry stateOrProvince and one of those spells it
            # "Victoria" rather than "VIC", so this is a weak source — the state a
            # caller should trust is revo_perfectgym.STATE_BY_STATE_ID.
            state = address.get("stateOrProvince")
            postal = _postcode(address.get("postalCode"))
            street = address.get("addressLine1")
            lat, lng = _as_float(address.get("lat")), _as_float(address.get("lng"))
        clubs.append(
            Club(
                uuid=entry.get("uuid"),
                name=entry.get("name"),
                city=city,
                state=state,
                mms=entry.get("mms"),
                url=entry.get("url"),
                timezone=entry.get("timezone") or (
                    address.get("timezone") if isinstance(address, dict) else None
                ),
                phone=entry.get("phone"),
                postal_code=postal,
                street=street,
                lat=lat,
                lng=lng,
                hours=parse_hours(entry),
            )
        )
    return clubs


@dataclass(frozen=True)
class ClubStatus:
    """Whether a club is open / staffed at a given moment, in its own timezone.

    ``open_now`` and ``staffed_now`` are ``None`` when the directory didn't say —
    never a guessed ``False``, because "closed" is the answer that would send
    someone home for nothing.
    """
    open_now: Optional[bool]
    staffed_now: Optional[bool]
    always_open: bool
    local_time: Optional[str]      # HH:MM in the club's timezone
    today_raw: Optional[str]       # the day's copy, verbatim, for display


def _local_now(tz_name: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """``now`` in the club's own timezone, or ``None`` when it can't be resolved.

    Revo spans four AU timezones, so a naive server-local comparison would be up
    to three hours out — enough to report a 5am-opening Perth club as closed.
    """
    if now is not None and now.tzinfo is None:
        return now  # caller supplied an explicit local time (tests)
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
    except Exception:  # pragma: no cover - stdlib since 3.9
        return None
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        return None  # unknown zone (no tzdata on this host) → "unknown", not wrong
    return (now or datetime.now(zone)).astimezone(zone)


def _within(t: time, start: Optional[time], end: Optional[time]) -> Optional[bool]:
    """Is *t* inside ``[start, end)``? Handles a window that crosses midnight."""
    if start is None or end is None:
        return None
    if start == end:
        return True  # a zero-width window is how "all day" sometimes renders
    if start < end:
        return start <= t < end
    return t >= start or t < end  # e.g. 5am-2am next day


def club_status(club: Club, now: Optional[datetime] = None) -> ClubStatus:
    """Evaluate *club*'s opening + staffed state at *now* (default: right now).

    Everything degrades to ``None`` rather than a guess: an unknown timezone, a
    weekday the directory didn't describe, or hours copy we couldn't parse all
    yield "unknown". Only the two unopened clubs and three others lack hours
    entirely, so in practice this answers for 74 of Revo's 79 clubs.
    """
    local = _local_now(club.timezone, now)
    if local is None:
        return ClubStatus(None, None, False, None, None)
    day = WEEKDAYS[local.weekday()]
    hours = club.hours.get(day)
    if hours is None:
        return ClubStatus(None, None, False, local.strftime("%H:%M"), None)
    t = local.time()
    open_now = True if hours.always_open else _within(t, hours.open_from, hours.open_to)
    return ClubStatus(
        open_now=open_now,
        staffed_now=_within(t, hours.staffed_from, hours.staffed_to),
        always_open=hours.always_open,
        local_time=local.strftime("%H:%M"),
        today_raw=hours.raw,
    )


# ---------------------------------------------------------------------------
# Cross-backend club-name matching
# ---------------------------------------------------------------------------

# Netpulse and PerfectGym disagree on four club names — Netpulse appends a state
# or city ("Knoxfield Vic", "Rivervale WA", "Pitt St Sydney") and spells one site
# "Nunawading (Original)" where PerfectGym uses "Nunawading OG". Stripping a
# trailing location token and reducing to bare alphanumerics reconciles all four.
_NAME_SUFFIXES = frozenset({
    "vic", "wa", "sa", "nsw", "qld", "tas", "nt", "act",
    "sydney", "melbourne", "perth", "adelaide", "brisbane",
})


def normalise_club_name(name: Optional[str]) -> str:
    """A join key for a club name, comparable across Revo's two backends.

    Lowercases, drops punctuation/spacing, and strips trailing state/city tokens.
    Matching PerfectGym's ``FullName`` as well as its ``Name`` is what separates
    the two Nunawading sites — they are 138 m apart, so a geographic nearest-match
    silently hands one club the other's hours, which is why this is name-based.
    """
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def index_clubs_by_name(clubs: list[Club]) -> dict[str, Club]:
    """``{normalised_name: Club}`` for joining onto the PerfectGym directory.

    Verified 1:1 across all 77 Netpulse clubs against PerfectGym's 79 — no
    collisions, and the only unmatched PerfectGym entries are the two clubs that
    haven't opened yet (Netpulse lists a club once it trades).
    """
    out: dict[str, Club] = {}
    for club in clubs:
        key = normalise_club_name(club.name)
        if key:
            out.setdefault(key, club)
    return out


# ---------------------------------------------------------------------------
# Credential-free club directory (+ its own long TTL cache)
# ---------------------------------------------------------------------------

# The directory changes when Revo opens a club — months apart — and hours change
# rarer still, so this gets the same 6h TTL as the PerfectGym directory.
CLUB_DIR_TTL_SECONDS = 6 * 3600

_dir_lock = threading.Lock()
_dir_cache: tuple[float, tuple[Club, ...]] = (0.0, ())


def fetch_club_directory() -> list[Club]:
    """Fetch the public club directory with **no credentials at all**.

    ``GET company/children`` answers 200 from a bare session with no
    ``JSESSIONID`` — verified byte-identical to the authenticated response. That
    makes hours/geo/phone/timezone available to any caller without a Revo login,
    with no account to lock out and no session to expire, so this deliberately
    does **not** go through :class:`NetpulseClient`.
    """
    if requests is None:
        raise NetpulseUnavailable(
            "The 'requests' package is required to fetch the Revo club directory."
        )
    r = requests.get(
        NETPULSE_BASE + CLUBS_PATH,
        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return parse_club_directory(r.json())


def shared_club_directory() -> list[Club]:
    """Cached :func:`fetch_club_directory` (6h TTL).

    An empty result is never cached, so a transient outage can't wedge every
    caller on "no clubs" for six hours.
    """
    global _dir_cache
    now = _time.monotonic()
    with _dir_lock:
        fetched_at, clubs = _dir_cache
        if clubs and (now - fetched_at) < CLUB_DIR_TTL_SECONDS:
            return list(clubs)
    clubs_list = fetch_club_directory()
    if clubs_list:
        with _dir_lock:
            _dir_cache = (now, tuple(clubs_list))
    return clubs_list


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class NetpulseClient:
    """Read-only session against the Revo Netpulse backend.

    Only POSTs the login (per the project's read-only rule); everything else is
    a GET. Holds the exerciser ``uuid`` and ``JSESSIONID`` privately and never
    exposes them.
    """

    def __init__(self, email: str, password: str) -> None:
        if requests is None:
            raise NetpulseUnavailable(
                "The 'requests' package is required for the Netpulse client."
            )
        self.email = email
        self._password = password
        self._http = requests.Session()
        self._http.headers.update(DEFAULT_HEADERS)
        self._uuid: Optional[str] = None
        self._logged_in = False
        # Non-secret session context, safe to surface.
        self.home_club_name: Optional[str] = None
        self.chain_name: Optional[str] = None

    def login(self) -> None:
        """POST credentials → JSESSIONID cookie + exerciser uuid.

        Raises :class:`NetpulseAuthError` on failure. Never logs the response
        body (it carries tokens + a door barcode).
        """
        r = self._http.post(
            NETPULSE_BASE + LOGIN_PATH,
            data={"username": self.email, "password": self._password},
            timeout=REQUEST_TIMEOUT,
        )
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200 or not isinstance(body, dict) or "uuid" not in body:
            raise NetpulseAuthError(
                f"Netpulse login failed for {self.email!r} (status {r.status_code})."
            )
        self._uuid = body.get("uuid")
        self.home_club_name = body.get("homeClubName")
        self.chain_name = body.get("chainName")
        self._logged_in = True
        # Note: no exerciser uuid / token / barcode in the log line.
        LOG.info("Netpulse login OK email=%s home_club=%s", self.email, self.home_club_name)

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _get_json(self, path: str) -> Any:
        r = self._http.get(NETPULSE_BASE + path, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def get_membership(self) -> Membership:
        """Return the member's non-secret membership facts."""
        self._ensure_login()
        return parse_membership(
            self._get_json(MEMBERSHIP_PATH.format(uuid=self._uuid))
        )

    def get_clubs(self) -> list[Club]:
        """Return the Netpulse club directory."""
        self._ensure_login()
        return parse_club_directory(self._get_json(CLUBS_PATH))
