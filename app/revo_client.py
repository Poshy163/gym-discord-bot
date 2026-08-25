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
from calendar import month_name, monthrange
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional

LOG = logging.getLogger("gymbot.revo")

BASE_URL = "https://revocentral.revofitness.com.au"
LOGIN_PATH = "/portal/login.php"
# club-counter.php now returns a 17-byte "Invalid Access! B" behind a new
# access guard (see docs/REVO_PORTAL.md) — the all-clubs board is dead. The
# member's own favourite-club live count survives on the rewards landing.
CLUB_COUNTER_PATH = "/portal/club-counter.php"
REWARDS_PATH = "/portal/rewards/"
STREAKS_PATH = "/portal/rewards/streaks.php"
TICKETS_PATH = "/portal/rewards/ticket-tally.php"
RAFFLE_PATH = "/portal/rewards/raffle.php"
PRIZE_POOL_PATH = "/portal/rewards/prize-pool.php"

USER_AGENT = "gym-discord-bot/0.1 (+https://github.com/Poshy163/gym-discord-bot)"
REQUEST_TIMEOUT = 20

# The portal's server-side access guard: a PHP ``die("Invalid Access! B")``
# returned with HTTP 200 and a 17-byte body (see docs/REVO_PORTAL.md §1.2). It
# began (2026-07) on ``club-counter.php`` / ``massage-chair.php``, and as of
# ~2026-08 it also covers ``streaks.php`` (HTML *and* the ``?m=&y=`` calendar
# JSON) and ``raffle.php``. Common UA / referer / origin / app-header variations
# all return the same 17 bytes, so request-header spoofing is not a workaround.
# The exact server-side predicate is unknown because no on-device request or
# alternate source network was captured. ``ticket-tally.php``, ``prize-pool.php``
# and the rewards landing are (so far) unaffected.
GUARD_BODY = "Invalid Access! B"

# Live counter is refreshed on the server side fairly slowly; cache for a
# minute to avoid hammering the portal when several people run /busy in quick
# succession.
CLUB_COUNTER_TTL_SECONDS = 60


class RevoUnavailable(RuntimeError):
    """Raised when an optional dependency is missing or auth is unconfigured."""


class RevoAuthError(RuntimeError):
    """Raised when login fails (bad credentials, account locked, etc.)."""


class RevoAccessGuarded(RevoUnavailable):
    """Raised when a page returns the ``Invalid Access! B`` guard (docs §1.2).

    A subclass of :class:`RevoUnavailable` so existing broad ``except`` handlers
    keep degrading gracefully, but distinct so callers that want to say "Revo has
    restricted this page" (rather than "no data" or "wrong credentials") can catch
    it specifically. Retrying the already-tested headers/params is pointless;
    treat it as "this page is unavailable from this client context".
    """


class RevoPageUnreadable(RevoUnavailable):
    """Raised when a portal page answered but its expected shape was absent.

    Revo's access controls return HTTP 200, so status alone is not evidence that
    a source is usable.  Keeping shape drift distinct from an empty *valid* data
    set lets the poller fall back instead of silently treating a login page,
    redirect, or changed guard string as "no attendance".
    """


def is_access_guarded(body: str | None) -> bool:
    """True when *body* is exactly the portal's access-guard ``die()`` string."""
    return bool(body) and body.strip() == GUARD_BODY


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


@dataclass(frozen=True)
class RewardsLanding:
    """The member-specific bits scraped from ``/portal/rewards/``.

    This is what survives now that ``club-counter.php`` and ``streaks.php`` are
    access-guarded: the account's *own* favourite club (id + name), its live
    head-count, and the current weekly streak tile. Any field may be ``None``
    if its tile did not render.
    """
    fav_club_id: Optional[int]
    fav_club_name: Optional[str]
    in_club: Optional[int]
    streak_weeks: Optional[int] = None


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
    """Parse the (now-unavailable) all-clubs board from ``club-counter.php``.

    Returns ``(clubs_by_name, favorite_club_id)``.

    .. deprecated::
        ``club-counter.php`` now returns a 17-byte ``"Invalid Access! B"``
        behind a new access guard, so on the live portal this parser finds
        none of ``clubCounterLists`` / ``barGraphData`` / ``favoriteClubId``
        and returns ``({}, None)``. The all-clubs "busiest right now" board
        cannot be restored from the web. Use :func:`parse_rewards_landing` /
        :meth:`RevoClient.get_rewards_landing` for the surviving fav-club live
        count. Kept only so the parser + its tests still document the old shape.
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


# The rewards landing renders the member's favourite-club tile as a single
# <a href="…/club-counter.php?id=<ID>"> block containing three single-digit
# <span> head-count cells and a "rounded-full" white pill div with the club
# name. This is the only live occupancy signal left after the club-counter
# page was access-guarded.
_FAV_CLUB_ID_RE = re.compile(r"club-counter\.php\?id=(\d+)")
_FAV_CLUB_ANCHOR_RE = re.compile(
    r"<a\b[^>]*club-counter\.php\?id=\d+[^>]*>(.*?)</a>", re.I | re.S
)
_FAV_DIGIT_SPAN_RE = re.compile(r"<span[^>]*>\s*(\d)\s*</span>", re.I | re.S)
_FAV_PILL_RE = re.compile(
    r"<div[^>]*\brounded-full\b[^>]*>(.*?)</div>", re.I | re.S
)
_LANDING_STREAK_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"'][^\"']*/rewards/streaks\.php(?:\?[^\"']*)?[\"']"
    r"[^>]*>(.*?)</a>",
    re.I | re.S,
)
_COUNTER_SPAN_RE = re.compile(r"<span\b[^>]*>\s*(\d+)\s*</span>", re.I | re.S)


def parse_rewards_landing(
    html: str,
) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """Parse ``/portal/rewards/`` for the fav-club tile.

    Returns ``(fav_club_id, fav_club_name, in_club)`` — any element may be
    ``None`` if the landing didn't render the tile. The head-count is the three
    zero-padded ``<span>`` digit cells concatenated (e.g. ``0``,``0``,``2`` → 2).
    """
    id_m = _FAV_CLUB_ID_RE.search(html)
    fav_id = int(id_m.group(1)) if id_m else None

    anchor = _FAV_CLUB_ANCHOR_RE.search(html)
    block = anchor.group(1) if anchor else ""

    in_club: Optional[int] = None
    name: Optional[str] = None
    if block:
        digits = _FAV_DIGIT_SPAN_RE.findall(block)
        if digits:
            in_club = int("".join(digits))
        pill = _FAV_PILL_RE.search(block)
        if pill:
            name = _strip_tags(pill.group(1)) or None
    return fav_id, name, in_club


def parse_rewards_landing_streak(html: str) -> Optional[int]:
    """Read the weekly streak counter duplicated on the rewards landing.

    The dedicated streak page is currently access-guarded, but the still-live
    landing links to it with a flame tile whose visible ``<span>`` is the same
    weekly count.  Scope the parse to that anchor so ticket, occupancy, and draw
    counters elsewhere on the page cannot be mistaken for a streak.  Joining
    span values supports both one ``13`` span and Revo's usual ``1``/``3``
    split-counter shape.
    """
    anchor = _LANDING_STREAK_ANCHOR_RE.search(html)
    if not anchor:
        return None
    parts = _COUNTER_SPAN_RE.findall(anchor.group(1))
    return int("".join(parts)) if parts else None


def parse_streak_weeks(html: str) -> Optional[int]:
    """Pull the headline "N WEEKS" streak count from the streaks page.

    The digits are joined before being read, rather than captured as one ``\\d+``
    run. Today this counter is a single unpadded ``<span>`` (``3``), but *every*
    other counter on this portal — tickets, both draw countdowns — is split into
    zero-padded one-digit spans. If Revo ever renders this one the same way, a
    ``(\\d+)`` capture over the tag-stripped text would read ``1 3 WEEKS`` as
    **3**: not a crash, not a ``None``, just a wrong-but-plausible streak that
    nothing would flag. Joining the run handles both shapes identically.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    m = re.search(r"((?:\d\s*){1,4})WEEKS?", text, re.IGNORECASE)
    if not m:
        return None
    digits = re.findall(r"\d", m.group(1))
    return int("".join(digits)) if digits else None


def parse_streak_calendar(
    body: str,
    *,
    month: int | None = None,
    year: int | None = None,
) -> dict[int, bool]:
    """Decode the JSON returned by ``streaks.php?m=&y=`` into ``{day: attended}``.

    The endpoint returns an inline JSON document (Content-Type is mislabelled
    as ``text/html``) shaped like::

        {
          "month_name": "April",
          "weeks_data": {
            "week1": {"1": null, "2": null, "3": "0", "4": "0", ...},
            "week2": {"8": "0", "9": "1", ...},
            ...
            "week6": []
          }
        }

    Slot keys are grid positions (1..42 across six rows of seven) — *not*
    days-of-month. ``null`` cells are leading/trailing padding for days that
    belong to the neighbouring month; ``"0"`` / ``"1"`` are real days, with
    ``"1"`` meaning the user checked in. We walk the slots in left-to-right
    week-by-week order and assign ascending day-of-month numbers to the
    non-null cells.

    When ``month`` and ``year`` are supplied, the response month name and exact
    number of real day cells must match the requested month. Returns a
    ``{day_of_month: attended}`` dict, or an empty dict when the body is missing,
    malformed, incomplete, or contains a value other than exact ``0``/``1``.
    Failing the whole parse is deliberate: skipping one bad cell would shift
    every later value onto the wrong calendar date.
    """
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return {}
    weeks = payload.get("weeks_data") if isinstance(payload, dict) else None
    if not isinstance(weeks, dict):
        return {}

    expected_days: int | None = None
    if (month is None) != (year is None):
        return {}
    if month is not None and year is not None:
        try:
            expected_days = monthrange(year, month)[1]
            expected_name = month_name[month]
        except (IndexError, ValueError):
            return {}
        if payload.get("month_name") != expected_name:
            return {}

    out: dict[int, bool] = {}
    dom = 1
    # Week keys are insertion-ordered ("week1".."week6") in the wire format,
    # but sort defensively so a future server-side reshuffle doesn't break us.
    for key in sorted(weeks.keys(), key=lambda k: int(re.sub(r"\D", "", k) or 0)):
        cells = weeks[key]
        # An empty trailing week is encoded as a JSON list ([]) rather than {}.
        if isinstance(cells, list):
            iterable: list[Any] = list(cells)
        elif isinstance(cells, dict):
            try:
                ordered_keys = sorted(cells.keys(), key=lambda k: int(k))
            except (TypeError, ValueError):
                return {}
            iterable = [cells[k] for k in ordered_keys]
        else:
            return {}
        for v in iterable:
            if v is None:
                continue
            if isinstance(v, str) and v in {"0", "1"}:
                attended = v == "1"
            elif type(v) is int and v in {0, 1}:
                attended = v == 1
            else:
                return {}
            out[dom] = attended
            dom += 1
    if expected_days is not None and len(out) != expected_days:
        return {}
    return out


# Each ticket-tally history entry renders as a three-column grid "list" block.
# Its children have already changed order once. Parse direct children, identify
# the date and delta cells by shape, and treat the remaining cell as the source.
# Another reorder then cannot silently pair a source with the wrong date, while
# an added/removed column fails closed rather than being truncated to three.
_TICKET_DELTA_RE = re.compile(r"[-+]?(\d+)\s*Tickets?", re.I)
_TICKET_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


def _strip_tags(fragment: str) -> str:
    """Collapse an HTML fragment down to its visible text."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


class _TicketHistoryHTMLParser(HTMLParser):
    """Collect direct child cells from each ticket-history candidate block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.block_depth: int | None = None
        self.cell_depth: int | None = None
        self.cell_parts: list[str] = []
        self.current_cells: list[str] = []
        self.blocks: list[list[str]] = []
        self.candidate_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag != "div":
            if self.cell_depth is not None and tag in {"br", "p"}:
                self.cell_parts.append(" ")
            return

        self.div_depth += 1
        if self.block_depth is None:
            attr_map = {key.casefold(): value or "" for key, value in attrs}
            classes = set(attr_map.get("class", "").split())
            if {"list", "grid-cols-3"}.issubset(classes):
                self.candidate_count += 1
                self.block_depth = self.div_depth
                self.current_cells = []
            return

        if self.div_depth == self.block_depth + 1:
            self.cell_depth = self.div_depth
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.cell_depth is not None:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "div":
            return
        if self.block_depth is not None:
            if self.cell_depth == self.div_depth:
                self.current_cells.append(_strip_tags("".join(self.cell_parts)))
                self.cell_depth = None
                self.cell_parts = []
            elif self.block_depth == self.div_depth:
                self.blocks.append(self.current_cells)
                self.block_depth = None
                self.current_cells = []
        self.div_depth = max(0, self.div_depth - 1)


def _parse_tickets_with_completeness(
    html: str,
) -> tuple[Optional[int], list[TicketRow], int]:
    """Parse ticket data and count candidate history blocks that were malformed.

    Returns ``(available_tickets, history_rows_newest_first)``. The ``Available``
    pseudo-row that appears alongside the headline counter is filtered out.

    The available balance comes from the headline counter (a run of single-digit
    ``<span>`` cells before "Tickets Available"). Each history row is a
    ``<div class="list … grid-cols-3 …">`` block whose three children contain a
    date, the ``+N Tickets`` delta, and the source label in any order. Deltas of
    ``+2`` (recent grants) and ``+1`` (older ones, pre-~08/05/2026) both parse.
    """
    text = re.sub(r"<script[\s\S]*?</script>", " ", html)
    text = _strip_tags(text)

    avail: Optional[int] = None
    m = re.search(r"((?:\d\s*){1,6})Tickets\s+Available", text)
    if m:
        digits = re.findall(r"\d", m.group(1))
        if digits:
            avail = int("".join(digits))

    parser = _TicketHistoryHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is deliberately forgiving
        return avail, [], 1

    rows: list[TicketRow] = []
    malformed_blocks = parser.candidate_count - len(parser.blocks)
    for cells in parser.blocks:
        if len(cells) != 3:
            malformed_blocks += 1
            continue
        date_idx = next(
            (i for i, cell in enumerate(cells) if _TICKET_DATE_RE.fullmatch(cell)),
            None,
        )
        delta_idx = next(
            (i for i, cell in enumerate(cells) if _TICKET_DELTA_RE.fullmatch(cell)),
            None,
        )
        if date_idx is None or delta_idx is None or date_idx == delta_idx:
            malformed_blocks += 1
            continue
        source_idx = next(
            (i for i in range(3) if i not in (date_idx, delta_idx)),
            None,
        )
        if source_idx is None:
            malformed_blocks += 1
            continue
        date_m = _TICKET_DATE_RE.fullmatch(cells[date_idx])
        delta_m = _TICKET_DELTA_RE.fullmatch(cells[delta_idx])
        if date_m is None or delta_m is None:  # narrowed above; type-safe guard
            malformed_blocks += 1
            continue
        if _ddmmyyyy_to_iso(date_m.group(0)) is None:
            malformed_blocks += 1
            continue
        source_cell = cells[source_idx]
        if not source_cell:
            malformed_blocks += 1
            continue
        # The headline "Tickets Available" counter is not a grid-cols-3 block,
        # but keep filtering an "Available" source defensively.
        if source_cell.casefold() == "available":
            continue
        rows.append(
            TicketRow(
                delta=int(delta_m.group(1)),
                source=source_cell,
                date=date_m.group(0),
            )
        )
    return avail, rows, malformed_blocks


def parse_tickets(html: str) -> tuple[Optional[int], list[TicketRow]]:
    """Parse the ticket-tally page.

    Returns ``(available_tickets, history_rows_newest_first)``. The public parser
    remains tolerant for fixture/diagnostic use; :meth:`RevoClient.get_tickets`
    additionally rejects any malformed candidate row so polling cannot accept a
    silently truncated ledger.
    """
    available, rows, _malformed = _parse_tickets_with_completeness(html)
    return available, rows


def latest_attended_day(calendar: dict[int, bool]) -> Optional[int]:
    """Return the highest day-of-month with a check-in in *calendar*, or None.

    ``calendar`` is the ``{day_of_month: attended}`` mapping returned by
    :meth:`RevoClient.get_streak_calendar`. The attendance poller uses this as
    the *real* per-visit check-in signal — far more timely and granular than
    ``ticket-tally.php``, whose "Attendance" rows are only a roughly-weekly
    reward grant (dated to issuance, not to the day the member trained).
    """
    days = [d for d, attended in calendar.items() if attended]
    return max(days) if days else None


def _ddmmyyyy_to_iso(date_str: str) -> Optional[str]:
    """Convert a portal ``dd/mm/yyyy`` date to ISO ``YYYY-MM-DD`` (None if bad)."""
    clean = (date_str or "").strip()
    if _TICKET_DATE_RE.fullmatch(clean) is None:
        return None
    try:
        parsed = time.strptime(clean, "%d/%m/%Y")
    except ValueError:
        return None
    return time.strftime("%Y-%m-%d", parsed)


def latest_attendance_ticket_date(rows: list["TicketRow"]) -> Optional[str]:
    """Most recent ``Attendance`` grant date from ticket-tally, as ISO YYYY-MM-DD.

    This is the *fallback* attendance signal the poller uses when the per-day
    streaks calendar is access-guarded (see :data:`GUARD_BODY` and
    docs/REVO_PORTAL.md §3.3). The ``Attendance`` ticket is a roughly-weekly
    reward grant dated to *issuance*, so it lags real visits by days and misses
    most of them — but it is the only attendance signal that still renders once
    ``streaks.php`` is guarded. Only ``Attendance`` rows count: ``Monthiversary``
    / ``Welcome`` / ``BONUSDAILY`` are not gym visits.

    ISO strings sort lexicographically, so ``max`` picks the newest grant.
    """
    isos: list[str] = []
    for r in rows:
        if r.source.lower() != "attendance":
            continue
        iso = _ddmmyyyy_to_iso(r.date)
        if iso is not None:
            isos.append(iso)
    return max(isos) if isos else None


@dataclass(frozen=True)
class AttendanceInfo:
    """Latest attendance the bot could observe, plus where it came from.

    ``source`` is:
      * ``"calendar"`` — the per-day streaks calendar; ``date`` is a real visit
        day and ``streak_weeks`` may be populated.
      * ``"tickets"`` — the ticket-tally ``Attendance`` grant, used only while the
        calendar is access-guarded; ``date`` is a coarse, lagging *issuance* date
        (the member trained on or before it, often days earlier). ``streak_weeks``
        may still be populated from the surviving rewards-landing tile.
      * ``None`` — nothing found (no visits recorded, or the fallback was empty).

    ``date`` is ISO ``YYYY-MM-DD`` or ``None``.

    ``streak_readable`` says whether the streaks page actually answered. It
    separates "the streak is genuinely unknown/absent" (``True`` with
    ``streak_weeks is None``) from "we could not read it at all" (``False`` —
    access-guarded or a transient error). Callers that *cache* the streak need
    that distinction: overwriting a good cached value with ``None`` just because
    the page was unreachable would silently degrade the streak leaderboard.
    """
    date: Optional[str]
    source: Optional[str]
    streak_weeks: Optional[int]
    streak_readable: bool = False


# Weekly-streak milestones worth celebrating in the attendance feed.
STREAK_MILESTONES: tuple[int, ...] = (4, 8, 12, 26, 52)


def streak_milestone(prev: Optional[int], new: Optional[int]) -> Optional[int]:
    """Return the milestone newly reached when a streak grows ``prev`` → ``new``.

    Used by the attendance poller to fire a one-off celebration the first time
    a member's weekly streak crosses one of :data:`STREAK_MILESTONES`. Returns
    the *highest* milestone in the half-open interval ``(prev, new]`` (so a jump
    that skips several only celebrates the biggest), or ``None`` when no
    milestone was crossed.

    A ``None`` ``prev`` (we've never recorded a streak for this member yet)
    yields ``None`` so a freshly-linked account doesn't get spammed with a
    milestone for its backfilled streak.
    """
    if new is None or prev is None:
        return None
    crossed = [m for m in STREAK_MILESTONES if prev < m <= new]
    return max(crossed) if crossed else None


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


# The raffle page renders BOTH opt buttons and hides the one that doesn't apply,
# so which button is hidden tells us the member's current opt-in state. Note both
# carry `data-opt-val="1"`: `optval` is a pure *toggle*, not a 0/1 setter, so there
# is no read-only variant of the request — the DOM is the only way to read this.
_OPT_BUTTON_RE = re.compile(
    r"<button\b[^>]*\bid=\"opt(In|Out)\"[^>]*>", re.I
)
_DISPLAY_NONE_RE = re.compile(r"display:\s*none", re.I)


def parse_raffle_optin(html: str) -> Optional[bool]:
    """Whether the member is currently entered in the monthly raffle draw.

    ``True`` = opted in, ``False`` = opted out, ``None`` = couldn't tell (page
    reshaped, or neither button rendered) — never a silent ``False``.

    Read from which of the two opt buttons the server hides: the portal renders
    ``#optIn`` and ``#optOut`` together and shows only the *action available*, so a
    visible ``#optIn`` means the member is currently **out**.

    This matters because :func:`parse_raffle` will happily scrape countdowns out of
    the ``#nextDrawWrapper`` block **even when the portal hides it**, which it does
    for an opted-out member. Without this, the bot cheerfully tells someone their
    tickets are "in the draw" when they aren't entered at all.
    """
    states: dict[str, bool] = {}
    for m in _OPT_BUTTON_RE.finditer(html):
        states[m.group(1).lower()] = bool(_DISPLAY_NONE_RE.search(m.group(0)))
    hidden_in, hidden_out = states.get("in"), states.get("out")
    if hidden_in is None or hidden_out is None:
        return None
    if hidden_in == hidden_out:
        # Both shown or both hidden — the portal is in a shape we don't understand.
        return None
    return hidden_in  # optIn hidden ⇒ nothing to join ⇒ already in


@dataclass(frozen=True)
class RaffleInfo:
    """Raffle countdowns plus whether this member is actually entered.

    ``opted_in`` is ``None`` when the page didn't render a readable opt state, so
    callers can stay quiet rather than assert a wrong answer. When it is ``False``
    the countdowns are still *factually* the next draw dates, but the portal hides
    them from this member — so a caller must not imply their tickets are in play.
    """
    monthly_draw_days: Optional[int]
    major_draw_days: Optional[int]
    opted_in: Optional[bool]


# prize-pool.php renders two prize blurbs in DOM order [monthly, major], each as
# a <div class="py-3 px-1"><p>…</p></div> block. Free-text scrape — if Revo
# rewords or moves the blurbs, the missing side degrades to None.
_PRIZE_BLURB_RE = re.compile(
    r'<div\b[^>]*class="[^"]*\bpy-3\b[^"]*\bpx-1\b[^"]*"[^>]*>.*?<p\b[^>]*>(.*?)</p>',
    re.I | re.S,
)


def parse_prize_pool(html: str) -> dict[str, Optional[str]]:
    """Extract the monthly + major prize copy from ``prize-pool.php``.

    Returns ``{"monthly": str|None, "major": str|None}``. Blurbs are read in
    DOM order (monthly first, major second); either is ``None`` if that block
    is absent.
    """
    blurbs = [_strip_tags(m.group(1)) or None for m in _PRIZE_BLURB_RE.finditer(html)]
    return {
        "monthly": blurbs[0] if len(blurbs) >= 1 else None,
        "major": blurbs[1] if len(blurbs) >= 2 else None,
    }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

@dataclass
class _CountersCache:
    fetched_at: float = 0.0
    clubs: dict[str, ClubInfo] = field(default_factory=dict)
    favorite: Optional[int] = None


@dataclass
class _RewardsCache:
    fetched_at: float = 0.0
    landing: Optional["RewardsLanding"] = None


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
        """Fetch the (now-dead) all-clubs board — see :func:`parse_club_counter`.

        ``club-counter.php`` is access-guarded now, so this degrades gracefully
        to ``({}, None)``. Prefer :meth:`get_rewards_landing`.
        """
        return parse_club_counter(self._get(CLUB_COUNTER_PATH))

    def get_rewards_landing(self) -> RewardsLanding:
        """Scrape the rewards landing for its surviving member summary tiles."""
        html = self._get(REWARDS_PATH)
        fav_id, fav_name, in_club = parse_rewards_landing(html)
        return RewardsLanding(
            fav_club_id=fav_id, fav_club_name=fav_name, in_club=in_club,
            streak_weeks=parse_rewards_landing_streak(html),
        )

    def get_prize_pool(self) -> dict[str, Optional[str]]:
        """Fetch the current monthly + major prize copy."""
        return parse_prize_pool(self._get(PRIZE_POOL_PATH))

    def get_streak_weeks(self) -> Optional[int]:
        # The still-readable rewards landing duplicates the weekly streak tile.
        # Prefer it so a guard on the dedicated page no longer takes streaks
        # offline. Fall back to the old page in case Revo later removes the tile
        # while restoring streaks.php.
        try:
            landing_streak = self.get_rewards_landing().streak_weeks
            if landing_streak is not None:
                return landing_streak
        except RevoAuthError:
            raise
        except Exception:  # pragma: no cover - transient landing failure
            LOG.warning(
                "Revo rewards-landing streak fetch failed for %s",
                self.email,
                exc_info=True,
            )
        html = self._get(STREAKS_PATH)
        if is_access_guarded(html):
            raise RevoAccessGuarded("Revo has access-guarded the streaks page.")
        streak = parse_streak_weeks(html)
        if streak is None:
            raise RevoPageUnreadable("Revo streak response had no streak counter.")
        return streak

    def get_streak_calendar(self, month: int, year: int) -> dict[int, bool]:
        """Per-day attendance for the given calendar month.

        Calls the undocumented JSON variant of the streaks page exposed via
        ``streaks.php?m=<MM>&y=<YYYY>`` (discovered in the rewards
        ``script.js``). Returns ``{day_of_month: attended_bool}``; raises
        :class:`RevoPageUnreadable` if the response redirects or loses its
        expected day-cell shape.

        Suitable for building per-user attendance timelines (the ticket-tally
        page only exposes the most recent ~10 entries).
        """
        if not 1 <= month <= 12:
            raise ValueError(f"month must be 1..12, got {month!r}")
        if not 2000 <= year <= 2100:
            raise ValueError(f"year out of plausible range: {year!r}")
        if not self._logged_in:
            self.login()

        def _do_get() -> "requests.Response":
            return self._http.get(
                BASE_URL + STREAKS_PATH,
                params={"m": month, "y": year},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )

        r = _do_get()
        # Same session-expiry handling as _get(): re-login on redirect to login.
        if r.status_code in (301, 302) and "login.php" in r.headers.get("Location", ""):
            LOG.info("Revo session expired during calendar fetch, re-logging in")
            self.login()
            r = _do_get()
        if r.status_code in (301, 302):
            raise RevoPageUnreadable(
                "Revo streak calendar redirected to an unexpected page."
            )
        r.raise_for_status()
        if is_access_guarded(r.text):
            raise RevoAccessGuarded("Revo has access-guarded the streaks calendar.")
        calendar = parse_streak_calendar(r.text, month=month, year=year)
        if not calendar:
            raise RevoPageUnreadable(
                "Revo streak calendar response had no readable day cells."
            )
        return calendar

    def get_tickets(self) -> tuple[Optional[int], list[TicketRow]]:
        html = self._get(TICKETS_PATH)
        if is_access_guarded(html):
            raise RevoAccessGuarded("Revo has access-guarded the ticket ledger.")
        available, rows, malformed_blocks = _parse_tickets_with_completeness(html)
        if malformed_blocks:
            raise RevoPageUnreadable(
                "Revo ticket ledger contained an unreadable history row."
            )
        if available is None or (available > 0 and not rows):
            raise RevoPageUnreadable(
                "Revo ticket ledger response did not have a readable history."
            )
        return available, rows

    def get_latest_attendance(self, month: int, year: int) -> AttendanceInfo:
        """Most recent attendance date, resilient to the streaks-page guard.

        Prefers the per-day streaks calendar (a real visit day, plus the weekly
        streak). When ``streaks.php`` is access-guarded (:data:`GUARD_BODY`) or
        answers with an unreadable shape, falls back to the newest ``Attendance``
        grant on ``ticket-tally.php``, which still renders. The returned
        :class:`AttendanceInfo` carries the
        ``source`` so the poller can word an announcement honestly — a ticket
        grant date is coarser and later than the day the member actually trained.

        Only covers *this* month; the poller bridges the previous month near a
        month boundary for the calendar path (the ticket page already spans
        months, so the fallback needs no bridging).
        """
        try:
            cal = self.get_streak_calendar(month, year)
            if not cal:
                raise RevoPageUnreadable(
                    "Revo streak calendar returned no readable day cells."
                )
        except (RevoAccessGuarded, RevoPageUnreadable):
            iso = latest_attendance_ticket_date(self.get_tickets()[1])
            streak: Optional[int] = None
            streak_readable = False
            try:
                # The rewards landing carries this even while the calendar is
                # guarded, so ticket-based attendance need not lose the weekly
                # streak as well.
                streak = self.get_streak_weeks()
                streak_readable = True
            except Exception:  # pragma: no cover - optional tail, never sink feed
                LOG.warning(
                    "Revo fallback streak fetch failed for %s",
                    self.email,
                    exc_info=True,
                )
            return AttendanceInfo(
                date=iso,
                source="tickets" if iso else None,
                streak_weeks=streak,
                streak_readable=streak_readable,
            )

        # The streak is an optional *tail* on the announcement, never a reason to
        # lose the attendance itself, so it is read defensively and separately:
        #   * read it even when this month holds no visit yet — the cached value
        #     backs the streak leaderboard, and skipping the read here would let
        #     it freeze at a stale number for anyone between check-ins;
        #   * swallow ANY failure, not just the access guard. A transient 5xx or
        #     timeout on streaks.php must not abort the poll and drop a real
        #     "trained today" announcement (which is what propagating it did).
        streak: Optional[int] = None
        streak_readable = False
        try:
            streak = self.get_streak_weeks()
            streak_readable = streak is not None
        except RevoAccessGuarded:  # streaks HTML guarded even when calendar isn't
            streak = None
        except Exception:  # pragma: no cover - network
            LOG.warning("Revo streak fetch failed for %s", self.email, exc_info=True)
            streak = None

        latest_day = latest_attended_day(cal)
        if latest_day is None:
            return AttendanceInfo(
                date=None, source=None,
                streak_weeks=streak, streak_readable=streak_readable,
            )
        iso = f"{year:04d}-{month:02d}-{latest_day:02d}"
        return AttendanceInfo(
            date=iso, source="calendar",
            streak_weeks=streak, streak_readable=streak_readable,
        )

    def get_raffle(self) -> RaffleInfo:
        """Draw countdowns **and** this member's raffle opt-in state, one fetch.

        Both come off the same page, so they're parsed together rather than making
        the caller GET it twice — and pairing them is what stops a caller from
        announcing a countdown to someone who isn't entered.

        Raises :class:`RevoAccessGuarded` when ``raffle.php`` returns the access
        guard and :class:`RevoPageUnreadable` when none of its expected fields
        parse. An opted-out page is still valid because ``opted_in`` is then
        explicitly ``False``.
        """
        html = self._get(RAFFLE_PATH)
        if is_access_guarded(html):
            raise RevoAccessGuarded("Revo has access-guarded the raffle page.")
        days = parse_raffle(html)
        opted_in = parse_raffle_optin(html)
        if (
            days["monthly_draw_days"] is None
            and days["major_draw_days"] is None
            and opted_in is None
        ):
            raise RevoPageUnreadable(
                "Revo raffle response had no countdown or opt-in state."
            )
        return RaffleInfo(
            monthly_draw_days=days["monthly_draw_days"],
            major_draw_days=days["major_draw_days"],
            opted_in=opted_in,
        )


# ---------------------------------------------------------------------------
# Source health probe
# ---------------------------------------------------------------------------

# Status values for :class:`SourceHealth`.
HEALTH_OK = "ok"            # page answered with usable data
HEALTH_EMPTY = "empty"      # page answered, but parsed to nothing (shape drift?)
HEALTH_GUARDED = "guarded"  # the `Invalid Access! B` server-side block (§1.2)
HEALTH_ERROR = "error"      # auth/network/HTTP failure


@dataclass(frozen=True)
class SourceHealth:
    """Whether one Revo page the bot depends on is currently usable."""
    label: str
    status: str
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == HEALTH_OK


def _probe(label: str, fn) -> SourceHealth:
    """Run one probe, classifying guard / error / empty / ok.

    ``fn`` returns a truthy value when the page yielded usable data. The guard is
    caught *before* the generic handler because ``RevoAccessGuarded`` subclasses
    ``RevoUnavailable`` — an operator needs "Revo is blocking this" to read
    differently from "our credentials/network broke".
    """
    try:
        got = fn()
    except RevoAccessGuarded:
        return SourceHealth(label, HEALTH_GUARDED, "blocked by Revo (Invalid Access)")
    except RevoAuthError as exc:
        return SourceHealth(label, HEALTH_ERROR, f"auth failed: {exc}")
    except Exception as exc:  # pragma: no cover - network
        return SourceHealth(label, HEALTH_ERROR, str(exc)[:120])
    return SourceHealth(label, HEALTH_OK if got else HEALTH_EMPTY)


def probe_sources(client: "RevoClient", month: int, year: int) -> list[SourceHealth]:
    """Check every portal page the bot reads, newest-breakage-first order.

    Exists because this portal degrades *silently*: the guard returns HTTP 200,
    so a page can stop working while every request still "succeeds". That is
    exactly how the attendance tracker went quiet (§3.2.4) — nothing raised, the
    calendar just parsed to empty. This turns that class of failure into
    something a human can see in one command instead of a log dig.

    Costs one request per source, so callers should rate-limit it (the portal
    notes ask for gentle traffic) — it is a diagnostic, not a poll.
    """
    return [
        # The attendance feed's primary source, and the one currently guarded.
        _probe("Check-in calendar", lambda: client.get_streak_calendar(month, year)),
        _probe("Weekly streak", lambda: client.get_streak_weeks() is not None),
        # The fallback that keeps the attendance feed alive while the above is out.
        _probe("Tickets", lambda: client.get_tickets()[0] is not None),
        _probe("Raffle", lambda: client.get_raffle()),
        _probe("Prize pool", lambda: any(client.get_prize_pool().values())),
        _probe(
            "Rewards landing",
            lambda: client.get_rewards_landing().fav_club_id is not None,
        ),
    ]


def attendance_feed_state(sources: list[SourceHealth]) -> tuple[str, str]:
    """Summarise what :func:`probe_sources` means for the attendance tracker.

    Returns ``(state, explanation)`` where *state* is ``"ok"`` (per-day calendar
    working), ``"degraded"`` (calendar out, ticket fallback carrying it) or
    ``"down"`` (neither source usable).
    """
    by_label = {s.label: s for s in sources}
    calendar = by_label.get("Check-in calendar")
    tickets = by_label.get("Tickets")
    if calendar is not None and calendar.ok:
        return "ok", "Per-day check-ins are being read normally."
    if tickets is not None and tickets.ok:
        return (
            "degraded",
            "The per-day calendar is unavailable, so check-ins are detected from "
            "the weekly ticket grant instead — later and less precise, but the "
            "feed still fires.",
        )
    return "down", "No attendance source is readable, so check-ins can't be detected."


# ---------------------------------------------------------------------------
# Module-level "shared" client for read-only commands like /busy.
# ---------------------------------------------------------------------------

_shared_lock = threading.Lock()
_shared_client: RevoClient | None = None
_shared_counters = _CountersCache()
_shared_rewards = _RewardsCache()


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


def shared_rewards_landing() -> RewardsLanding:
    """Cached wrapper around :meth:`RevoClient.get_rewards_landing`.

    Backs ``/busy`` now that the all-clubs board is gone: it returns the
    *shared env account's own* favourite club and its live head-count. The
    :data:`CLUB_COUNTER_TTL_SECONDS` cache keeps a burst of ``/busy`` calls
    from re-hitting the portal.

    Only the shared account is cached here: unlike the old all-clubs board, a
    rewards landing is *per-account*, so caching a per-user landing under a
    global key would leak one member's club/count to another. Per-user callers
    use :func:`rewards_landing_with_client` (uncached).
    """
    global _shared_rewards
    now = time.monotonic()
    with _shared_lock:
        cache = _shared_rewards
        if cache.landing is not None and (now - cache.fetched_at) < CLUB_COUNTER_TTL_SECONDS:
            return cache.landing
    landing = shared_client_from_env().get_rewards_landing()
    # Don't cache a degenerate landing (parse miss / tile absent): caching an
    # all-None result would wedge /busy on "unavailable" for the full TTL even
    # after the portal recovers. Only a landing carrying real data is worth
    # holding onto; an empty one falls through and is re-fetched next call.
    if landing.fav_club_id is not None or landing.in_club is not None:
        with _shared_lock:
            _shared_rewards = _RewardsCache(fetched_at=now, landing=landing)
    return landing


def rewards_landing_with_client(client: RevoClient) -> RewardsLanding:
    """Rewards-landing fetch using the invoking user's *own* linked client.

    Uncached on purpose (see :func:`shared_rewards_landing`): the landing is
    account-specific, so it must not share the global shared-account cache.
    """
    return client.get_rewards_landing()


# Known Revo club suburbs grouped by Australian state — used by
# filter_clubs_by_state().  Names are compared case-insensitively; each entry
# is a full club name as it appears in the portal.  Add new entries here when
# Revo opens new locations.
_CLUB_NAMES_BY_STATE: dict[str, frozenset[str]] = {
    "SA": frozenset({
        # Currently open
        "angle vale",
        "beverley",
        "blair athol",
        "blakeview",
        "glenelg",
        "happy valley",
        "marion",
        "modbury",       # Westfield Tea Tree Plus — portal may use either name
        "tea tree plaza",
        "munno para",
        "noarlunga",
        "parafield",
        "salisbury downs",
        "seaford meadows",
        "windsor gardens",
        "woodcroft",
        "woodville",
        # Coming soon (2026)
        "elizabeth",
        "golden grove",
        "marleston",
        "mount barker",
        "port adelaide",
        "trinity gardens",
    }),
    "WA": frozenset({
        # Currently open
        "australind",
        "balcatta",
        "banksia grove",
        "belmont",        # Cloverdale address
        "bunbury",
        "butler",
        "canning vale",
        "cannington",
        "claremont",
        "clarkson",
        "cockburn",
        "dayton",
        "ellenbrook",
        "girrawheen",
        "innaloo",
        "joondalup",
        "kelmscott",
        "kwinana",
        "malaga",
        "mandurah",
        "midland",
        "mirrabooka",
        "morley",
        "mount hawthorn",
        "myaree",
        "north beach",
        "northbridge",
        "o'connor",
        "oconnor",
        "rivervale",
        "rockingham",
        "scarborough",
        "victoria park",
        "wanneroo",
        "warwick",
        "woodbridge",
        # Coming soon (2026)
        "forrestdale",
    }),
    "VIC": frozenset({
        # Currently open
        "ballarat",
        "braybrook",
        "chadstone",
        "cranbourne",
        "epping",
        "frankston",
        "hoppers crossing",
        "knoxfield",
        "langwarrin",
        "maribyrnong",
        "mentone",
        "moorabbin airport",
        "narre warren",
        "noble park",
        "nunawading",
        "plenty valley",
        "richmond",
        "southland",       # Cheltenham address
        "springvale",
        # Coming soon (2026)
        "footscray",
        "bayswater north",
    }),
    "NSW": frozenset({
        # Currently open
        "castle hill",
        "charlestown",
        "jesmond",
        "pitt st",
        "pitt street",
        "shellharbour",
    }),
}

# Backwards-compat alias for the old SA-only constant.
_SA_CLUB_NAMES: frozenset[str] = _CLUB_NAMES_BY_STATE["SA"]


def known_states() -> list[str]:
    """Return the list of state codes for which we have a hardcoded club list."""
    return list(_CLUB_NAMES_BY_STATE.keys())


def state_for_club(name: str) -> str | None:
    """Return the state code (e.g. ``"SA"``) for a club name, or ``None`` if unknown."""
    key = (name or "").strip().lower()
    for state, names in _CLUB_NAMES_BY_STATE.items():
        if key in names:
            return state
    return None


def filter_clubs_by_state(
    clubs: dict[str, ClubInfo], state: str,
) -> dict[str, ClubInfo]:
    """Return only clubs whose name matches a known location in *state*.

    *state* is a case-insensitive Australian state code (``"SA"``, ``"WA"``,
    ``"VIC"``, ``"NSW"``).  Unknown states return an empty dict.
    """
    names = _CLUB_NAMES_BY_STATE.get((state or "").upper())
    if not names:
        return {}
    return {
        name: info
        for name, info in clubs.items()
        if name.lower() in names
    }


def filter_sa_clubs(clubs: dict[str, ClubInfo]) -> dict[str, ClubInfo]:
    """Return only clubs whose name matches a known SA location."""
    return filter_clubs_by_state(clubs, "SA")


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
