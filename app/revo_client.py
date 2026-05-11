"""Revo Fitness client portal scraper used by the bot.

The portal exposes no JSON API at any membership tier — every page server-renders
its data into HTML or inline `<script>` blocks. We log in with form-encoded
credentials, persist the `Member` cookie in a `requests.Session`, and parse the
relevant fragments out of the HTML.

See ``docs/REVO_PORTAL.md`` for the full reverse-engineering notes (endpoint
inventory, gating, security caveats).

This module is import-safe even if ``requests`` / ``cryptography`` aren't
installed — the bot can run without the Revo features. Callers should check
:func:`available` (or just catch :class:`RevoUnavailable`) before using the
client.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

LOG = logging.getLogger("gymbot.revo")

BASE_URL = "https://revocentral.revofitness.com.au"
LOGIN_PATH = "/portal/login.php"
CLUB_COUNTER_PATH = "/portal/club-counter.php"
STREAKS_PATH = "/portal/rewards/streaks.php"
TICKETS_PATH = "/portal/rewards/ticket-tally.php"
RAFFLE_PATH = "/portal/rewards/raffle.php"

USER_AGENT = "gym-discord-bot/0.1 (+https://github.com/Poshy163/gym-discord-bot)"
REQUEST_TIMEOUT = 20

# Live counter is refreshed on the server side fairly slowly; cache for a
# minute to avoid hammering the portal when several people run /busy in quick
# succession.
CLUB_COUNTER_TTL_SECONDS = 60


class RevoUnavailable(RuntimeError):
    """Raised when an optional dependency is missing or auth is unconfigured."""


class RevoAuthError(RuntimeError):
    """Raised when login fails (bad credentials, account locked, etc.)."""


# Optional deps — only imported lazily so the bot can boot without them.
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
# Credential encryption
# ---------------------------------------------------------------------------

_FERNET_ENV = "REVO_FERNET_KEY"


def _fernet() -> "Fernet":
    if Fernet is None:
        raise RevoUnavailable(
            "The 'cryptography' package is required to store Revo credentials."
        )
    key = os.environ.get(_FERNET_ENV, "").strip()
    if not key:
        raise RevoUnavailable(
            f"Set ${_FERNET_ENV} to a Fernet key (generate one with "
            "`python -c 'from cryptography.fernet import Fernet;"
            " print(Fernet.generate_key().decode())'`)."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:  # pragma: no cover - bad key shape
        raise RevoUnavailable(f"Invalid {_FERNET_ENV}: {exc}") from exc


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password for at-rest storage. Returns urlsafe base64 string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> str:
    """Inverse of :func:`encrypt_password`."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - corrupted DB row
        raise RevoUnavailable("Stored Revo credential is unreadable.") from exc


# ---------------------------------------------------------------------------
# HTML parsers (pure functions, easy to unit test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClubInfo:
    name: str
    club_id: int
    in_club: int
    hourly: dict[int, int] | None  # {hour_of_day: count} for 1..24


@dataclass(frozen=True)
class TicketRow:
    delta: int
    source: str
    date: str  # dd/mm/yyyy as displayed by the portal


def parse_member_cookie(raw: str | None) -> tuple[Optional[int], Optional[int]]:
    """Decode the URL-encoded PHP-serialised ``Member`` cookie.

    Returns ``(member_id, membership_level)`` — either may be ``None`` if the
    cookie is missing or in an unexpected shape (we deliberately avoid pulling
    a PHP unserializer dep just for two integers).
    """
    if not raw:
        return None, None
    decoded = urllib.parse.unquote(raw)
    mid = re.search(r's:2:"id";i:(\d+);', decoded)
    lvl = re.search(r's:15:"membershipLevel";i:(\d+);', decoded)
    return (
        int(mid.group(1)) if mid else None,
        int(lvl.group(1)) if lvl else None,
    )


def parse_club_counter(html: str) -> tuple[dict[str, ClubInfo], Optional[int]]:
    """Parse ``/portal/club-counter.php``.

    Returns ``(clubs_by_name, favorite_club_id)``.
    """
    clubs_match = re.search(r"clubCounterLists\s*=\s*(\{.*?\})\s*;", html, re.S)
    bars_match = re.search(r"barGraphData\s*=\s*(\[.*?\])\s*;", html, re.S)
    fav_match = re.search(r"favoriteClubId\s*=\s*(\d+)", html)

    clubs_raw: dict[str, dict[str, Any]] = (
        json.loads(clubs_match.group(1)) if clubs_match else {}
    )
    bars_raw: list[dict[str, int]] = (
        json.loads(bars_match.group(1)) if bars_match else []
    )

    out: dict[str, ClubInfo] = {}
    for idx, (name, info) in enumerate(clubs_raw.items()):
        try:
            in_club = int(info["in_club"])
        except (KeyError, TypeError, ValueError):
            in_club = 0
        hourly: dict[int, int] | None = None
        if idx < len(bars_raw) and isinstance(bars_raw[idx], dict):
            try:
                hourly = {int(k): int(v) for k, v in bars_raw[idx].items()}
            except (TypeError, ValueError):
                hourly = None
        out[name] = ClubInfo(
            name=name,
            club_id=int(info.get("id", 0) or 0),
            in_club=in_club,
            hourly=hourly,
        )

    favorite = int(fav_match.group(1)) if fav_match else None
    return out, favorite


def parse_streak_weeks(html: str) -> Optional[int]:
    """Pull the headline "N WEEKS" streak count from the streaks page."""
    text = re.sub(r"<[^>]+>", " ", html)
    m = re.search(r"(\d+)\s*WEEKS?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


_TICKET_ROW_RE = re.compile(
    r"\+?(\d+)\s*Tickets\s*([A-Za-z]+)\s*(\d{2}/\d{2}/\d{4})"
)


def parse_tickets(html: str) -> tuple[Optional[int], list[TicketRow]]:
    """Parse the ticket-tally page.

    Returns ``(available_tickets, history_rows_newest_first)``. The ``Available``
    pseudo-row that appears alongside the headline counter is filtered out.
    """
    text = re.sub(r"<script[\s\S]*?</script>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    avail: Optional[int] = None
    m = re.search(r"((?:\d\s*){1,6})Tickets\s+Available", text)
    if m:
        digits = re.findall(r"\d", m.group(1))
        if digits:
            avail = int("".join(digits))

    rows = [
        TicketRow(delta=int(d), source=src, date=date)
        for d, src, date in _TICKET_ROW_RE.findall(text)
        if src != "Available"
    ]
    return avail, rows


def parse_raffle(html: str) -> dict[str, Optional[int]]:
    """Extract monthly + major draw countdowns (in days)."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    def _pick(label: str) -> Optional[int]:
        m = re.search(rf"{label}\s*Draw\s*((?:\d\s*){{1,3}})Days?", text)
        if not m:
            return None
        digits = re.findall(r"\d", m.group(1))
        return int("".join(digits)) if digits else None

    return {
        "monthly_draw_days": _pick("Monthly"),
        "major_draw_days": _pick("Major"),
    }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

@dataclass
class _CountersCache:
    fetched_at: float = 0.0
    clubs: dict[str, ClubInfo] = field(default_factory=dict)
    favorite: Optional[int] = None


class RevoClient:
    """Authenticated session against the Revo portal.

    Thread-safe — internal lock serialises login retries so a burst of
    concurrent ``/busy`` invocations doesn't trigger N parallel logins on
    cookie expiry.
    """

    def __init__(self, email: str, password: str) -> None:
        if requests is None:
            raise RevoUnavailable(
                "The 'requests' package is required for the Revo client."
            )
        self.email = email
        self._password = password
        self._http = requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._logged_in = False
        self.member_id: Optional[int] = None
        self.membership_level: Optional[int] = None

    # ---- auth ----------------------------------------------------------

    def login(self) -> None:
        """Submit the login form. Raises :class:`RevoAuthError` on failure."""
        with self._lock:
            self._login_locked()

    def _login_locked(self) -> None:
        r = self._http.post(
            BASE_URL + LOGIN_PATH,
            data={"user": self.email, "password": self._password},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        # Successful login lands on /portal/rewards/. Failure re-renders the
        # login form (still 200), so we use the URL as the success signal.
        if "/portal/rewards" not in r.url:
            raise RevoAuthError(
                f"Revo login failed for {self.email!r} (landed on {r.url})."
            )
        self.member_id, self.membership_level = parse_member_cookie(
            self._http.cookies.get("Member")
        )
        self._logged_in = True
        LOG.info(
            "Revo login OK email=%s member_id=%s level=%s",
            self.email, self.member_id, self.membership_level,
        )

    def _get(self, path: str) -> str:
        """GET ``path`` with auto-relogin on session expiry."""
        if not self._logged_in:
            self.login()
        r = self._http.get(
            BASE_URL + path, timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
        # Session-expired pages redirect back to /portal/login.php.
        if r.status_code in (301, 302) and "login.php" in r.headers.get("Location", ""):
            LOG.info("Revo session expired, re-logging in")
            self.login()
            r = self._http.get(
                BASE_URL + path, timeout=REQUEST_TIMEOUT, allow_redirects=False,
            )
        if r.status_code in (301, 302):
            # Still redirecting — usually means the route is gated (level 2,
            # mobile-only, etc.). Surface as an empty body; callers decide
            # how to handle it.
            LOG.debug(
                "Revo %s redirected to %s (status=%s)",
                path, r.headers.get("Location"), r.status_code,
            )
            return ""
        r.raise_for_status()
        return r.text

    # ---- public read endpoints ----------------------------------------

    def get_club_counter(self) -> tuple[dict[str, ClubInfo], Optional[int]]:
        return parse_club_counter(self._get(CLUB_COUNTER_PATH))

    def get_streak_weeks(self) -> Optional[int]:
        return parse_streak_weeks(self._get(STREAKS_PATH))

    def get_tickets(self) -> tuple[Optional[int], list[TicketRow]]:
        return parse_tickets(self._get(TICKETS_PATH))

    def get_raffle(self) -> dict[str, Optional[int]]:
        return parse_raffle(self._get(RAFFLE_PATH))


# ---------------------------------------------------------------------------
# Module-level "shared" client for read-only commands like /busy.
# ---------------------------------------------------------------------------

_shared_lock = threading.Lock()
_shared_client: RevoClient | None = None
_shared_counters = _CountersCache()


def shared_client_from_env() -> RevoClient:
    """Build (and cache) a :class:`RevoClient` from ``REVO_USER`` /
    ``REVO_PASS`` env vars. Used by anonymous read-only commands.
    """
    global _shared_client
    if not available():
        raise RevoUnavailable(
            "Install 'requests' to enable Revo features (pip install requests)."
        )
    with _shared_lock:
        if _shared_client is None:
            email = os.environ.get("REVO_USER", "").strip()
            password = os.environ.get("REVO_PASS", "").strip()
            if not email or not password:
                raise RevoUnavailable(
                    "Set REVO_USER and REVO_PASS to enable shared Revo access "
                    "(used by /busy)."
                )
            _shared_client = RevoClient(email, password)
        return _shared_client


def shared_club_counter() -> tuple[dict[str, ClubInfo], Optional[int]]:
    """Cached wrapper around :meth:`RevoClient.get_club_counter`."""
    global _shared_counters
    now = time.monotonic()
    with _shared_lock:
        cache = _shared_counters
        if cache.clubs and (now - cache.fetched_at) < CLUB_COUNTER_TTL_SECONDS:
            return cache.clubs, cache.favorite
    client = shared_client_from_env()
    clubs, favorite = client.get_club_counter()
    with _shared_lock:
        _shared_counters = _CountersCache(
            fetched_at=now, clubs=clubs, favorite=favorite,
        )
    return clubs, favorite


def club_counter_with_client(
    client: RevoClient,
) -> tuple[dict[str, ClubInfo], Optional[int]]:
    """Cached club-counter fetch using *any* authenticated client.

    Mirrors :func:`shared_club_counter` but lets callers fall back to a
    user-supplied :class:`RevoClient` (e.g. one built from the invoking
    user's linked credentials) when no shared env-var account is set.
    Results populate the same TTL cache so subsequent /busy calls — from
    anyone — reuse the data.
    """
    global _shared_counters
    now = time.monotonic()
    with _shared_lock:
        cache = _shared_counters
        if cache.clubs and (now - cache.fetched_at) < CLUB_COUNTER_TTL_SECONDS:
            return cache.clubs, cache.favorite
    clubs, favorite = client.get_club_counter()
    with _shared_lock:
        _shared_counters = _CountersCache(
            fetched_at=now, clubs=clubs, favorite=favorite,
        )
    return clubs, favorite


# Known SA (South Australia) Revo club suburbs — used by filter_sa_clubs().
# Names are compared case-insensitively; each entry is a full club name as it
# appears in the portal.  Add new entries here when Revo opens SA locations.
_SA_CLUB_NAMES: frozenset[str] = frozenset({
    # Currently open
    "aldinga",
    "christies beach",
    "gepps cross",
    "glenelg",
    "marion",
    "mawson lakes",
    "mile end",
    "munno para",
    "noarlunga",
    "noarlunga centre",
    "norwood",
    "parafield",
    "tea tree plaza",
    "victor harbor",
    # Coming soon (2026)
    "elizabeth",
    "golden grove",
    "marleston",
    "mount barker",
    "port adelaide",
    "trinity gardens",
})


def filter_sa_clubs(clubs: dict[str, ClubInfo]) -> dict[str, ClubInfo]:
    """Return only clubs whose name matches a known SA location."""
    return {
        name: info
        for name, info in clubs.items()
        if name.lower() in _SA_CLUB_NAMES
    }


def find_club(clubs: dict[str, ClubInfo], query: str) -> ClubInfo | None:
    """Case-insensitive substring lookup over club names."""
    q = (query or "").strip().lower()
    if not q:
        return None
    # Exact (case-insensitive) name first.
    for name, info in clubs.items():
        if name.lower() == q:
            return info
    # Then prefix.
    for name, info in clubs.items():
        if name.lower().startswith(q):
            return info
    # Then substring.
    for name, info in clubs.items():
        if q in name.lower():
            return info
    return None
