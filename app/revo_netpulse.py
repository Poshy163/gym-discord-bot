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
from dataclasses import dataclass
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
class Club:
    """A club from the Netpulse company directory. All fields are public info."""
    uuid: Optional[str]
    name: Optional[str]
    city: Optional[str]
    state: Optional[str]
    mms: Optional[str]   # member-management system, e.g. "perfectgym"
    url: Optional[str]


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


def parse_club_directory(payload: Any) -> list[Club]:
    """Parse ``company/children?responseType=basic`` into a list of clubs."""
    obj = _as_obj(payload)
    if not isinstance(obj, list):
        return []
    clubs: list[Club] = []
    for entry in obj:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        city = state = None
        if isinstance(address, dict):
            city = address.get("city")
            state = address.get("stateOrProvince")
        clubs.append(
            Club(
                uuid=entry.get("uuid"),
                name=entry.get("name"),
                city=city,
                state=state,
                mms=entry.get("mms"),
                url=entry.get("url"),
            )
        )
    return clubs


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
