"""Gym tracking Discord bot.

Auto-detects gym posts in configured channels, parses lifts, and stores them
in SQLite. Exposes slash commands for querying stats, progress, and
leaderboards.
"""

from __future__ import annotations

import csv
import io
import importlib
import logging
import os
import re
import sqlite3
import tempfile
from calendar import monthrange
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback
    ZoneInfo = None  # type: ignore[assignment]

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .aliases import (
    aliases_for,
    all_canonicals,
    canonicalize,
    normalize_token,
)
from .db import Database
from .graphing import daily_best_points, running_best_values
from .message_targeting import strip_leading_user_mention
from .overview import lift_overview
from .parser import (
    Lift,
    estimated_one_rep_max,
    parse_message,
    should_auto_store_lifts,
)
from .presence import (
    PresenceSummary,
    estimate_sleep_window,
    format_duration,
    is_online as _presence_is_online,
    summarize_presence,
)
from . import __version__
from . import revo_client

load_dotenv()

LOG = logging.getLogger("gymbot")


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter — chosen over python-json-logger to avoid
    pulling in a dep just for one optional output mode. Container log shippers
    (Loki, Datadog, etc.) generally prefer one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        import json
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_log_handler = logging.StreamHandler()
if os.getenv("LOG_FORMAT", "text").lower() == "json":
    _log_handler.setFormatter(_JsonFormatter())
else:
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    handlers=[_log_handler],
    force=True,
)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN env var is required")

DB_PATH = os.getenv("DB_PATH", "/data/gym.sqlite3")

# Comma-separated list of channel IDs the bot should auto-scan. Empty = all.
_ch = os.getenv("GYM_CHANNEL_IDS", "").strip()
GYM_CHANNEL_IDS: set[int] = {int(x) for x in _ch.split(",") if x.strip().isdigit()}

# Optional guild ID for instant slash-command sync during development.
_gid = os.getenv("GUILD_ID", "").strip()
DEV_GUILD: discord.Object | None = (
    discord.Object(id=int(_gid)) if _gid.isdigit() else None
)

# A parsed message must yield at least this many lifts before we auto-store it.
# Keeps casual chatter out of the DB.
MIN_LIFTS_FOR_AUTO = int(os.getenv("MIN_LIFTS_FOR_AUTO", "2"))

# Keep parser confirmation replies readable when someone posts a full stats dump.
# Use 0 to show every parsed lift.
PARSE_REPLY_MAX_ITEMS = int(os.getenv("PARSE_REPLY_MAX_ITEMS", "15"))

# On startup, scan recent history of every configured gym channel so posts made
# while the bot was offline (or before it existed) get imported automatically.
BACKFILL_ON_START = os.getenv("BACKFILL_ON_START", "true").lower() in (
    "1", "true", "yes", "y", "on",
)
# How far back to look per channel on startup. Use 0 for "no limit".
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT", "1000"))

# When true, weights are displayed with a (≈N lb) suffix alongside kg. Helps
# anyone reading who isn't on metric.
SHOW_LB = os.getenv("SHOW_LB", "false").lower() in ("1", "true", "yes", "y", "on")

# Guardrail against typos such as "2200kg" becoming a leaderboard PR. Set to
# 0 to disable if your server genuinely needs to log heavier machine numbers.
try:
    MAX_WEIGHT_KG = float(os.getenv("MAX_WEIGHT_KG", "500"))
except ValueError:
    MAX_WEIGHT_KG = 500.0

# Discord user IDs allowed to ❌-undo *any* tracked bot reply, not just their
# own or the lift's target. Comma-separated. Defaults to the repo owner.
_admins = os.getenv("ADMIN_USER_IDS", "1072114272064262154").strip()
ADMIN_USER_IDS: set[int] = {
    int(x) for x in _admins.split(",") if x.strip().isdigit()
}

# Timezone used when rendering dates in user-facing messages. Defaults to
# Australia/Adelaide (the author's crew). Falls back to UTC if zoneinfo isn't
# available or the name is invalid.
_tz_name = os.getenv("DISPLAY_TIMEZONE", "Australia/Adelaide").strip() or "UTC"
if ZoneInfo is not None:
    try:
        DISPLAY_TZ = ZoneInfo(_tz_name)
    except Exception:  # pragma: no cover - bad tz name
        LOG.warning("Unknown DISPLAY_TIMEZONE=%r, falling back to UTC", _tz_name)
        DISPLAY_TZ = timezone.utc
else:  # pragma: no cover
    DISPLAY_TZ = timezone.utc

# Weekly reminder: posts a "drop your current bests" nudge on a schedule.
# REMINDER_CHANNEL_ID is required to enable it. Day/hour default to
# Wednesday 12:00 local (DISPLAY_TIMEZONE).
_rid = os.getenv("REMINDER_CHANNEL_ID", "").strip()
REMINDER_CHANNEL_ID: int | None = int(_rid) if _rid.isdigit() else None
# Python weekday: Monday=0 ... Sunday=6. Default Wednesday=2.
REMINDER_WEEKDAY = int(os.getenv("REMINDER_WEEKDAY", "2"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "12"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))
# Optional role ID to ping in the reminder (as @&123). Leave blank for no ping.
_role = os.getenv("REMINDER_ROLE_ID", "").strip()
REMINDER_ROLE_ID: int | None = int(_role) if _role.isdigit() else None

# Weekly bodyweight check-in reminder. Defaults to Monday 07:30 in the
# DISPLAY_TIMEZONE so the user can update their bodyweight at the start of
# the week. If BODYWEIGHT_REMINDER_CHANNEL_ID is blank, falls back to
# REMINDER_CHANNEL_ID so a single channel setting covers both reminders.
_bw_rid = os.getenv("BODYWEIGHT_REMINDER_CHANNEL_ID", "").strip()
BODYWEIGHT_REMINDER_CHANNEL_ID: int | None = (
    int(_bw_rid) if _bw_rid.isdigit() else REMINDER_CHANNEL_ID
)
BODYWEIGHT_REMINDER_WEEKDAY = int(os.getenv("BODYWEIGHT_REMINDER_WEEKDAY", "0"))
BODYWEIGHT_REMINDER_HOUR = int(os.getenv("BODYWEIGHT_REMINDER_HOUR", "7"))
BODYWEIGHT_REMINDER_MINUTE = int(os.getenv("BODYWEIGHT_REMINDER_MINUTE", "30"))
_bw_role = os.getenv("BODYWEIGHT_REMINDER_ROLE_ID", "").strip()
BODYWEIGHT_REMINDER_ROLE_ID: int | None = (
    int(_bw_role) if _bw_role.isdigit() else None
)

# Daily update: posts yesterday's server activity summary on a schedule.
# DAILY_UPDATE_CHANNEL_ID is required to enable it. Defaults to 08:00 local.
_daily_id = os.getenv("DAILY_UPDATE_CHANNEL_ID", "").strip()
DAILY_UPDATE_CHANNEL_ID: int | None = (
    int(_daily_id) if _daily_id.isdigit() else None
)
DAILY_UPDATE_HOUR = int(os.getenv("DAILY_UPDATE_HOUR", "8"))
DAILY_UPDATE_MINUTE = int(os.getenv("DAILY_UPDATE_MINUTE", "0"))
DAILY_UPDATE_POST_EMPTY = os.getenv("DAILY_UPDATE_POST_EMPTY", "false").lower() in (
    "1", "true", "yes", "y", "on",
)

# Bot "accent" colour for embeds.
EMBED_COLOUR = discord.Colour.from_str("#f26522")

db = Database(DB_PATH)

# Presence tracking (/track) needs the privileged presences + members
# intents. Both must also be flipped on in the Discord Developer Portal
# for the bot user — without those toggles Discord refuses the gateway
# connection. We default this OFF so the bot still boots for users who
# don't need /track; opt in by setting ENABLE_PRESENCE_TRACKING=true and
# enabling the toggles in the portal.
ENABLE_PRESENCE_TRACKING = os.getenv(
    "ENABLE_PRESENCE_TRACKING", "false"
).lower() in ("1", "true", "yes", "y", "on")

intents = discord.Intents.default()
intents.message_content = True
intents.members = False
if ENABLE_PRESENCE_TRACKING:
    intents.presences = True
    intents.members = True
bot = commands.Bot(command_prefix="!gym ", intents=intents)


def _format_weight(weight: float, bw: bool) -> str:
    if bw and weight == 0:
        return "BW"
    base = f"BW+{weight:g}kg" if bw else f"{weight:g}kg"
    if SHOW_LB and weight > 0:
        lb = round(weight * 2.20462, 1)
        lb_str = f"{lb:g}"
        base += f" (≈{lb_str} lb)"
    return base


# Equipment whose plain-kg log values represent machine *assistance* — the
# user is logging how much weight the machine is taking off them, not what
# they pulled. True load = bodyweight − assistance. Weighted variants of
# the same lifts use the BW+X form (bodyweight_add=True), and are handled
# separately in `_true_weight_kg` below.
_BW_ASSISTED_EQUIPMENT: frozenset[str] = frozenset({
    "pull ups", "dips", "chin assist", "push up",
})


def _true_weight_kg(
    equipment: str, weight_kg: float, bw_add: bool,
    bodyweight: float | None,
) -> float | None:
    """Return the *true* kg the lifter moved on a bodyweight-relative lift.

    Examples (with bodyweight = 100kg):
      * `BW+20kg` pull-up  → 120kg lifted.
      * `pull ups 70kg` (machine assist 70kg) → 30kg lifted.
      * `bench press 80kg`                    → None (not a BW lift).

    Returns None when no bodyweight is known, when the equipment is not a
    known bodyweight-relative lift, or when the inputs aren't meaningful
    (e.g. negative result from over-assistance, which we clamp to None so
    the caller doesn't render nonsense like "true: -5kg").
    """
    if bodyweight is None or bodyweight <= 0:
        return None
    if bw_add:
        # Weighted BW lift: added weight is on top of the lifter.
        return float(bodyweight) + float(weight_kg)
    if equipment in _BW_ASSISTED_EQUIPMENT and weight_kg > 0:
        # Plain-kg log on an assisted machine: subtract the assistance.
        true_kg = float(bodyweight) - float(weight_kg)
        if true_kg <= 0:
            # Assistance >= bodyweight is unusual (would mean negative load);
            # skip rather than display a confusing 0/negative number.
            return None
        return true_kg
    return None


def _true_weight_suffix(
    equipment: str, weight_kg: float, bw_add: bool,
    bodyweight: float | None,
) -> str:
    """Return ` (true: 30kg)` or empty string when no true weight applies."""
    true_kg = _true_weight_kg(equipment, weight_kg, bw_add, bodyweight)
    if true_kg is None:
        return ""
    # Round to 1dp to avoid noisy "29.9999kg" from float subtraction.
    return f" (true: {round(true_kg, 1):g}kg)"


def _user_bodyweight(guild_id: int, user_id: int) -> float | None:
    """Latest known bodyweight for a user in a guild, or None."""
    try:
        row = db.get_latest_bodyweight(guild_id, user_id)
    except Exception:  # pragma: no cover - defensive
        LOG.exception("Failed to read bodyweight for user %s", user_id)
        return None
    if row is None:
        return None
    return float(row["weight_kg"])


# Chat-message bodyweight update, e.g. "bodyweight 100kg",
# "body weight: 95.5kg", "bw 80". Matches the *whole* (stripped) message so
# we don't accidentally hijack stats dumps that mention bodyweight in
# passing — those are already filtered from lift parsing by parser.py's
# _SKIP_LINE_TOKENS. Combined with the existing leading-@user targeting,
# this means `@dos bodyweight 100kg` updates dos's bodyweight.
_BODYWEIGHT_MSG_RE = re.compile(
    r"^\s*(?:body\s*weight|bodyweight|bw)\s*[:\-]?\s*"
    r"(\d+(?:\.\d+)?)\s*(?:kg)?\s*\.?\s*$",
    re.IGNORECASE,
)


def _parse_bodyweight_message(text: str) -> float | None:
    """If ``text`` is a bare bodyweight statement, return the kg value.

    Returns ``None`` for anything else so the caller can fall through to
    the regular lift parser.
    """
    if not text:
        return None
    m = _BODYWEIGHT_MSG_RE.match(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):  # pragma: no cover - regex guards this
        return None


# Backdated logging: detect a date hint anywhere in a message so that posts
# like "bench 90kg yesterday" or "squat 100kg on 2026-05-06" can be stored
# against the date the workout actually happened, not the message's own
# timestamp. Patterns are intentionally narrow to avoid hijacking weights:
#   * "yesterday" / "today" / "tonight"
#   * "N day(s) ago"
#   * weekday names ("monday" .. "sunday"), resolved to the most recent past
#     occurrence (today if it matches today's weekday)
#   * ISO calendar dates "YYYY-MM-DD"
_WEEKDAY_LOOKUP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_DATE_HINT_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_DATE_HINT_TODAY = re.compile(r"\b(today|tonight)\b", re.IGNORECASE)
_DATE_HINT_DAYS_AGO = re.compile(
    r"\b(\d{1,2})\s*d(?:ays?)?\s*ago\b", re.IGNORECASE
)
_DATE_HINT_WEEKDAY = re.compile(
    r"\b(?:last\s+)?(monday|mon|tuesday|tue|tues|wednesday|wed|"
    r"thursday|thu|thurs|friday|fri|saturday|sat|sunday|sun)\b",
    re.IGNORECASE,
)
_DATE_HINT_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _resolve_date_hint(
    text: str, now_local: datetime,
) -> datetime | None:
    """Return a UTC datetime for any date hint found in ``text``.

    ``now_local`` is the message's own timestamp converted to ``DISPLAY_TZ``
    and acts as the reference point for relative phrases ("yesterday",
    "monday", "3 days ago"). The returned datetime is anchored at noon
    local time on the resolved date — exact time-of-day doesn't matter for
    daily-grain stats and noon avoids DST/midnight ambiguity. Returns
    ``None`` when no recognised hint is present.
    """
    if not text:
        return None
    today_local = now_local.date()
    target_date = None

    if _DATE_HINT_YESTERDAY.search(text):
        target_date = today_local - timedelta(days=1)
    elif _DATE_HINT_TODAY.search(text):
        target_date = today_local
    else:
        m = _DATE_HINT_DAYS_AGO.search(text)
        if m:
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):  # pragma: no cover - regex guards
                n = 0
            if 1 <= n <= 30:
                target_date = today_local - timedelta(days=n)
        if target_date is None:
            m = _DATE_HINT_ISO.search(text)
            if m:
                try:
                    parsed = datetime.strptime(
                        m.group(1), "%Y-%m-%d"
                    ).date()
                except ValueError:
                    parsed = None
                if parsed is not None and parsed <= today_local + timedelta(
                    days=1
                ):
                    target_date = parsed
        if target_date is None:
            m = _DATE_HINT_WEEKDAY.search(text)
            if m:
                wd = _WEEKDAY_LOOKUP.get(m.group(1).lower())
                if wd is not None:
                    delta = (today_local.weekday() - wd) % 7
                    target_date = today_local - timedelta(days=delta)

    if target_date is None:
        return None
    local_dt = datetime.combine(target_date, dtime(12, 0), DISPLAY_TZ)
    return local_dt.astimezone(timezone.utc)


def _display_name(user: object) -> str:
    return str(
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", "Unknown user")
    )


def _message_lift_target(message: discord.Message) -> tuple[object, str]:
    """Return the lifter and content to parse for a Discord message.

    A leading user mention means "log this for that person", e.g.
    ``@Cookie Monster squat 55kg``. Mentions elsewhere in the sentence remain
    ordinary chat text because they are ambiguous.
    """
    mentioned_id, body = strip_leading_user_mention(message.content)
    if mentioned_id is None:
        return message.author, message.content
    if bot.user and mentioned_id == bot.user.id:
        return message.author, message.content
    target = discord.utils.get(message.mentions, id=mentioned_id)
    if target is None or getattr(target, "bot", False):
        return message.author, message.content
    return target, body


async def _resolve_nickname_target(
    text: str, guild: discord.Guild,
) -> tuple[object | None, str]:
    """If ``text`` starts with a known bot-wide nickname, return ``(member, rest)``.

    Only fires when the nickname is followed by whitespace (so a nick of
    "Ben" does not accidentally eat the start of "Bench press").  Nicknames
    are matched longest-first to avoid a short prefix shadowing a longer one.
    Returns ``(None, text)`` when no nickname prefix is found.
    """
    rows = db.list_user_nicknames()
    # Longest match first prevents a short nick from shadowing a longer one.
    for row in sorted(rows, key=lambda r: len(r["nickname"]), reverse=True):
        nick: str = row["nickname"]
        if not text.lower().startswith(nick.lower()):
            continue
        after_nick = text[len(nick):]
        # Must be followed by whitespace (or end of string) — not a mid-word match.
        if after_nick and not after_nick[0].isspace():
            continue
        rest = after_nick.lstrip()
        if not rest:
            # Nickname with nothing after it — not a lift attribution.
            continue
        uid = int(row["user_id"])
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                continue
        return member, rest
    return None, text


def _target_suffix(author: object, target: object) -> str:
    if getattr(author, "id", None) == getattr(target, "id", None):
        return ""
    return f" for **{_display_name(target)}**"


def _split_reasonable_lifts(lifts: list[Lift]) -> tuple[list[Lift], list[Lift]]:
    if MAX_WEIGHT_KG <= 0:
        return lifts, []
    accepted: list[Lift] = []
    rejected: list[Lift] = []
    for lift in lifts:
        if lift.weight_kg > MAX_WEIGHT_KG:
            rejected.append(lift)
        else:
            accepted.append(lift)
    return accepted, rejected


def _rejected_lifts_note(rejected: list[Lift]) -> str:
    if not rejected:
        return ""
    lines = [
        "",
        (
            f"⚠️ Skipped {_plural(len(rejected), 'lift')} over "
            f"{MAX_WEIGHT_KG:g}kg. If that was real, use `/log` after "
            "raising `MAX_WEIGHT_KG`."
        ),
    ]
    for lift in rejected[:5]:
        lines.append(
            f"• **{_safe_label(lift.equipment)}** — "
            f"{_format_weight(lift.weight_kg, lift.bodyweight_add)}"
        )
    remaining = len(rejected) - 5
    if remaining > 0:
        lines.append(f"• ... and {_plural(remaining, 'more lift')}")
    return "\n".join(lines)


def _safe_label(text: str, *, limit: int = 60) -> str:
    """Make user-supplied text safe to echo back into a Discord message.

    Strips Discord mention/emoji syntax (so we never accidentally ping
    @everyone via a malformed lift label), escapes Markdown special chars
    that would break the embed, and truncates to ``limit`` chars.
    """
    cleaned = discord.utils.escape_mentions(text or "")
    cleaned = discord.utils.escape_markdown(cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or "(unknown)"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _format_lift_lines(
    lifts: list[Lift], limit: int | None = None,
    bodyweight: float | None = None,
) -> list[str]:
    if limit is None:
        limit = PARSE_REPLY_MAX_ITEMS
    shown = lifts if limit <= 0 else lifts[:limit]
    lines = [
        f"• **{lift.equipment}** — "
        f"{_format_weight(lift.weight_kg, lift.bodyweight_add)}"
        f"{_true_weight_suffix(lift.equipment, lift.weight_kg, lift.bodyweight_add, bodyweight)}"
        for lift in shown
    ]
    remaining = len(lifts) - len(shown)
    if remaining > 0:
        lines.append(f"• ... and {_plural(remaining, 'more lift')}")
    return lines


def _format_date(iso: str | None) -> str:
    """Return 'YYYY-MM-DD' for an ISO timestamp, converted to DISPLAY_TZ so
    dates match the reader's local calendar day (esp. important for Adelaide
    lifters posting after midnight UTC)."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        # Stored older rows might not include tz info.
        return iso[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")


def _format_local_day_age(iso: str) -> tuple[str, int]:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10], 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_date = dt.astimezone(DISPLAY_TZ).date()
    today = datetime.now(DISPLAY_TZ).date()
    return local_date.strftime("%Y-%m-%d"), max(0, (today - local_date).days)


def _local_date_window(date: str) -> tuple[str, str]:
    day = datetime.strptime(date, "%Y-%m-%d").date()
    start_local = datetime.combine(day, dtime.min, tzinfo=DISPLAY_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _resolve(guild_id: int, name: str) -> str:
    """Resolve an equipment label, checking the guild's custom alias table
    before falling back to the built-in canonicalization."""
    if not name:
        return ""
    key = normalize_token(name)
    if not key:
        return ""
    hit = db.alias_resolve(guild_id, key)
    if hit:
        return hit
    return canonicalize(name)


def _should_auto_store(lifts: list[Lift]) -> bool:
    return should_auto_store_lifts(lifts, MIN_LIFTS_FOR_AUTO)


def _custom_alias_map(guild_id: int) -> dict[str, str]:
    """Snapshot of the guild's custom aliases as ``{normalized: canonical}``.

    Built fresh per call — the alias table is tiny (handful of rows per
    guild) so a cache would only add invalidation complexity. If it ever
    grows, swap in a TTL cache here.
    """
    return {
        r["alias_normalized"]: r["canonical"]
        for r in db.alias_list(guild_id)
    }


async def _resolve_or_warn(
    interaction: discord.Interaction, name: str,
    *, kind: str = "equipment",
) -> str | None:
    """Centralised "did you mean…?" guard for slash command equipment input.

    Returns the canonical name on success, or ``None`` after sending an
    ephemeral error to the user (caller should ``return`` immediately).
    Suggests the closest known equipment via difflib when the input doesn't
    match anything we've seen.
    """
    if not name or not name.strip():
        await interaction.response.send_message(
            f"Please provide an {kind} name.", ephemeral=True
        )
        return None
    canon = _resolve(interaction.guild_id or 0, name)
    if not canon:
        await interaction.response.send_message(
            f"Couldn't read `{name}` as an {kind} name.", ephemeral=True
        )
        return None
    return canon


def _suggest_equipment(guild_id: int, name: str, n: int = 3) -> list[str]:
    """Closest known equipment matches for a (possibly mis-spelled) input.

    Sources include both the built-in canonicals and anything actually
    stored in this guild — that way 'incine' suggests both 'incline bench
    press' (built-in) and any custom names members have used.
    """
    import difflib

    pool = set(all_canonicals())
    pool.update(db.known_equipment(guild_id))
    return difflib.get_close_matches(name.lower(), [p.lower() for p in pool], n=n, cutoff=0.6)


async def _equipment_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest equipment names for slash-command parameters.

    Pulls from both the built-in canonicals and anything the guild has
    actually used, so users can autocomplete custom-aliased names too.
    Returns up to 25 choices (Discord's hard cap).
    """
    guild_id = interaction.guild_id or 0
    pool = sorted(set(all_canonicals()) | set(db.known_equipment(guild_id)))
    needle = (current or "").lower().strip()
    if needle:
        # Prefer prefix matches, then anywhere-substring matches.
        prefix = [p for p in pool if p.lower().startswith(needle)]
        contains = [p for p in pool if needle in p.lower() and p not in prefix]
        results = (prefix + contains)[:25]
    else:
        results = pool[:25]
    return [app_commands.Choice(name=p, value=p) for p in results]


def _compute_streak_weeks(dates: list[str]) -> int:
    """Given a sorted ascending list of YYYY-MM-DD strings, return the number
    of consecutive ISO weeks up to the most recent logged week that contain
    at least one lift. Returns 0 for an empty list or if the user hasn't
    logged in the current or previous ISO week."""
    if not dates:
        return 0
    today_local = datetime.now(DISPLAY_TZ).date()
    today_year, today_week, _ = today_local.isocalendar()

    weeks: set[tuple[int, int]] = set()
    for d in dates:
        try:
            parsed = datetime.fromisoformat(d).date()
        except ValueError:
            continue
        yr, wk, _ = parsed.isocalendar()
        weeks.add((yr, wk))
    if not weeks:
        return 0

    # Walk back one week at a time until we hit a missing week.
    def prev_iso_week(y: int, w: int) -> tuple[int, int]:
        # Subtract 7 days from any date in that week and recompute.
        any_day = datetime.fromisocalendar(y, w, 1).date() - timedelta(days=7)
        ay, aw, _ = any_day.isocalendar()
        return ay, aw

    # Start from the most recent week that has lifts AND is within one week
    # of "now" (so a two-week-absent streak doesn't get counted as current).
    if (today_year, today_week) in weeks:
        cursor = (today_year, today_week)
    else:
        prev = prev_iso_week(today_year, today_week)
        if prev in weeks:
            cursor = prev
        else:
            return 0

    streak = 0
    while cursor in weeks:
        streak += 1
        cursor = prev_iso_week(*cursor)
    return streak


def _new_prs_for_lifts(
    guild_id: int, user_id: int, lifts: list[Lift]
) -> list[tuple[Lift, float | None]]:
    """Return the subset of ``lifts`` that set a new personal best for the
    user, paired with the previous best (or None if it's the first entry).
    Only considers positive weight (pure-BW 0kg entries don't celebrate)."""
    prs: list[tuple[Lift, float | None]] = []
    for lift in lifts:
        if lift.weight_kg <= 0:
            continue
        prev = db.previous_best(guild_id, user_id, lift.equipment)
        if prev is None or lift.weight_kg > prev:
            prs.append((lift, prev))
    return prs


async def _store_lifts(
    message: discord.Message, lifts: list[Lift], target_user: object | None = None,
    *, logged_at: datetime | None = None,
) -> int:
    target = target_user or message.author
    when = logged_at or message.created_at.astimezone(timezone.utc)
    return db.add_lifts(
        guild_id=message.guild.id if message.guild else 0,
        user_id=int(getattr(target, "id")),
        username=_display_name(target),
        lifts=lifts,
        message_id=message.id,
        channel_id=message.channel.id,
        logged_at=when,
    )


async def _handle_bodyweight_message(
    message: discord.Message, target: object, weight_kg: float,
) -> None:
    """Persist a chat-message bodyweight update and reply with confirmation.

    Mirrors the validation done by `/bodyweight`: positive values only,
    capped by ``MAX_WEIGHT_KG`` so a fat-fingered "1500" can't poison
    every leaderboard line.
    """
    guild_id = message.guild.id if message.guild else 0
    target_id = int(getattr(target, "id"))
    if weight_kg <= 0:
        try:
            await message.reply(
                "Bodyweight must be a positive number of kg.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    if MAX_WEIGHT_KG > 0 and weight_kg > MAX_WEIGHT_KG:
        try:
            await message.reply(
                f"That bodyweight looks too high to be real "
                f"({weight_kg:g}kg > {MAX_WEIGHT_KG:g}kg).",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    try:
        db.set_bodyweight(guild_id, target_id, weight_kg)
    except Exception:
        LOG.exception("Failed to store bodyweight for user %s", target_id)
        return

    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    suffix = _target_suffix(message.author, target)
    try:
        await message.reply(
            f"Recorded bodyweight **{weight_kg:g}kg**{suffix}. The bot will "
            "now show the true load on bodyweight-relative lifts (e.g. "
            "assisted pull-ups, weighted dips).",
            mention_author=False,
        )
    except discord.HTTPException:
        pass
    LOG.info(
        "Stored bodyweight %.2fkg for %s in #%s",
        weight_kg, target, message.channel,
    )


@bot.event
async def on_ready() -> None:
    LOG.info(
        "Logged in as %s (id=%s) — gym-bot v%s",
        bot.user, bot.user.id if bot.user else "?", __version__,
    )
    if ENABLE_PRESENCE_TRACKING:
        LOG.info(
            "Presence tracking ENABLED (privileged intents in use). "
            "Make sure Presence + Server Members intents are toggled on "
            "in the Discord Developer Portal."
        )
    try:
        if DEV_GUILD is not None:
            bot.tree.copy_global_to(guild=DEV_GUILD)
            synced = await bot.tree.sync(guild=DEV_GUILD)
        else:
            synced = await bot.tree.sync()
        LOG.info("Synced %d slash commands", len(synced))
    except Exception:  # pragma: no cover - discord runtime only
        LOG.exception("Failed to sync commands")

    if BACKFILL_ON_START and GYM_CHANNEL_IDS:
        bot.loop.create_task(_run_startup_backfill())

    if REMINDER_CHANNEL_ID and not weekly_reminder.is_running():
        weekly_reminder.start()
        LOG.info(
            "Weekly reminder scheduled for %s %02d:%02d (%s) in channel %s",
            _WEEKDAY_NAMES[REMINDER_WEEKDAY % 7],
            REMINDER_HOUR, REMINDER_MINUTE, DISPLAY_TZ, REMINDER_CHANNEL_ID,
        )

    if BODYWEIGHT_REMINDER_CHANNEL_ID and not bodyweight_reminder.is_running():
        bodyweight_reminder.start()
        LOG.info(
            "Bodyweight reminder scheduled for %s %02d:%02d (%s) in channel %s",
            _WEEKDAY_NAMES[BODYWEIGHT_REMINDER_WEEKDAY % 7],
            BODYWEIGHT_REMINDER_HOUR, BODYWEIGHT_REMINDER_MINUTE,
            DISPLAY_TZ, BODYWEIGHT_REMINDER_CHANNEL_ID,
        )

    if DAILY_UPDATE_CHANNEL_ID and not daily_update.is_running():
        daily_update.start()
        LOG.info(
            "Daily update scheduled for %02d:%02d (%s) in channel %s",
            DAILY_UPDATE_HOUR, DAILY_UPDATE_MINUTE,
            DISPLAY_TZ, DAILY_UPDATE_CHANNEL_ID,
        )

    if (
        not REVO_DISABLED
        and revo_client.available()
        and not revo_attendance_poll.is_running()
    ):
        revo_attendance_poll.start()
        LOG.info(
            "Revo attendance poll scheduled every %d minutes",
            REVO_POLL_MINUTES,
        )


_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

_CHECKIN_DEFAULT_EQUIPMENT = [
    "bench press",
    "incline bench press",
    "shoulder press",
    "lat pulldown",
    "low row",
    "pec dec",
    "rear delt fly",
    "tricep pushdown",
    "preacher curl",
    "hammer curl",
    "lateral raise",
    "leg press",
    "leg extension",
    "leg curl",
    "calf raise",
    "squat",
]


# The loop fires once every 24 hours at REMINDER_HOUR:REMINDER_MINUTE in
# DISPLAY_TZ; we then check the weekday inside the task so a single loop
# definition suffices regardless of which day the user configures.
def _scheduled_time(hour: int, minute: int) -> dtime:
    hh = max(0, min(23, hour))
    mm = max(0, min(59, minute))
    return dtime(hour=hh, minute=mm, tzinfo=DISPLAY_TZ)


def _reminder_time() -> dtime:
    return _scheduled_time(REMINDER_HOUR, REMINDER_MINUTE)


def _daily_update_time() -> dtime:
    return _scheduled_time(DAILY_UPDATE_HOUR, DAILY_UPDATE_MINUTE)


@tasks.loop(time=_reminder_time())
async def weekly_reminder() -> None:
    if REMINDER_CHANNEL_ID is None:
        return
    now_local = datetime.now(DISPLAY_TZ)
    if now_local.weekday() != REMINDER_WEEKDAY % 7:
        return
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDER_CHANNEL_ID)
        except discord.HTTPException:
            LOG.warning(
                "Reminder: cannot access channel %s", REMINDER_CHANNEL_ID
            )
            return
    mention = (
        f"<@&{REMINDER_ROLE_ID}> " if REMINDER_ROLE_ID else ""
    )
    text = (
        f"{mention}🏋️ **Weekly gym check-in!**\n"
        "Drop your current bests below so the bot picks them up.\n"
        "Example:\n"
        "```\nBench press: 80kg\nSquat: 100kg\nLat pulldown: 55kg\n```\n"
        "Tip: `/summary` shows where you're at, `/goals` tracks what you're "
        "chasing, and the bot reacts ✅ when it logs your post."
    )
    try:
        allowed = discord.AllowedMentions(roles=True)
        await channel.send(text, allowed_mentions=allowed)
        LOG.info("Weekly reminder posted to #%s", channel)
    except discord.HTTPException:
        LOG.exception("Failed to post weekly reminder")


@weekly_reminder.before_loop
async def _before_weekly_reminder() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


def _bodyweight_reminder_time() -> dtime:
    return _scheduled_time(BODYWEIGHT_REMINDER_HOUR, BODYWEIGHT_REMINDER_MINUTE)


@tasks.loop(time=_bodyweight_reminder_time())
async def bodyweight_reminder() -> None:
    """Weekly nudge to update bodyweight via `/bodyweight`.

    Mirrors `weekly_reminder`: the loop fires daily at the configured time
    in DISPLAY_TIMEZONE and we filter for the right weekday in-task. Default
    schedule is Monday 07:30 in Australia/Adelaide (matches DISPLAY_TIMEZONE
    default), so a fresh bodyweight is on file at the start of each week.
    """
    if BODYWEIGHT_REMINDER_CHANNEL_ID is None:
        return
    now_local = datetime.now(DISPLAY_TZ)
    if now_local.weekday() != BODYWEIGHT_REMINDER_WEEKDAY % 7:
        return
    channel = bot.get_channel(BODYWEIGHT_REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(BODYWEIGHT_REMINDER_CHANNEL_ID)
        except discord.HTTPException:
            LOG.warning(
                "Bodyweight reminder: cannot access channel %s",
                BODYWEIGHT_REMINDER_CHANNEL_ID,
            )
            return
    mention = (
        f"<@&{BODYWEIGHT_REMINDER_ROLE_ID}> "
        if BODYWEIGHT_REMINDER_ROLE_ID else ""
    )
    text = (
        f"{mention}⚖️ **Weekly bodyweight check-in!**\n"
        "Drop your current weight in chat — the bot picks it up automatically. "
        "Just type one of:\n"
        "```\nbw 83.4\nbodyweight 83.4kg\n```\n"
        "Or run `/bodyweight weight_kg:<your kg>` if you prefer a slash "
        "command.\n"
        "Why it matters — the bot uses your bodyweight to show **true load** "
        "on bodyweight-relative lifts:\n"
        "• Assisted pull-up at 70kg with 100kg bodyweight → "
        "**30kg actual lifted**.\n"
        "• `BW+20kg` weighted dip at 100kg bodyweight → **120kg actual**."
    )
    try:
        allowed = discord.AllowedMentions(roles=True)
        await channel.send(text, allowed_mentions=allowed)
        LOG.info("Bodyweight reminder posted to #%s", channel)
    except discord.HTTPException:
        LOG.exception("Failed to post bodyweight reminder")


@bodyweight_reminder.before_loop
async def _before_bodyweight_reminder() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


def _daily_window(days_ago: int = 1) -> tuple[str, str, str]:
    days = max(0, min(30, days_ago))
    day = datetime.now(DISPLAY_TZ).date() - timedelta(days=days)
    start_local = datetime.combine(day, dtime.min, tzinfo=DISPLAY_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        day.strftime("%Y-%m-%d"),
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _daily_update_text(
    guild_id: int,
    date_label: str,
    start_iso: str,
    end_iso: str,
    *,
    post_empty: bool = False,
) -> str | None:
    activity = db.daily_activity(guild_id, start_iso, end_iso, limit=5)
    totals = activity["totals"]
    total_lifts = int(totals["total_lifts"] or 0)
    if total_lifts == 0:
        if not post_empty:
            return None
        return (
            f"📊 **Daily gym update — {date_label}**\n"
            "No lifts logged for this day. Fresh slate next session."
        )

    lifters = int(totals["lifters"] or 0)
    unique_equip = int(totals["unique_equip"] or 0)
    sessions = int(totals["sessions"] or 0)
    lines = [
        f"📊 **Daily gym update — {date_label}**",
        (
            f"{_plural(total_lifts, 'lift')} logged by "
            f"{_plural(lifters, 'lifter')} across "
            f"{_plural(unique_equip, 'exercise')} from "
            f"{_plural(sessions, 'session')}."
        ),
    ]

    prs = activity["prs"]
    if prs:
        lines.append("\n🎉 **PRs**")
        for row in prs:
            previous = row["prev_best"]
            if previous is None:
                tail = "first logged"
            else:
                gain = row["weight_kg"] - previous
                tail = f"+{gain:g}kg"
            lines.append(
                f"• **{row['username']}** — {row['equipment']}: "
                f"{_format_weight(row['weight_kg'], bool(row['bw']))} ({tail})"
            )

    top_users = activity["top_users"]
    if top_users:
        lines.append("\n🏅 **Most active**")
        for row in top_users:
            lifts = int(row["n"])
            exercises = int(row["equip"])
            lines.append(
                f"• **{row['username']}** — {_plural(lifts, 'lift')}, "
                f"{_plural(exercises, 'exercise')}"
            )

    popular = activity["popular_equipment"]
    if popular:
        lines.append("\n🏋️ **Popular lifts**")
        for row in popular:
            entries = int(row["n"])
            users = int(row["users"])
            lines.append(
                f"• **{row['equipment']}** — {_plural(entries, 'entry', 'entries')}, "
                f"{_plural(users, 'lifter')}"
            )

    lines.append("\nUse `/summary`, `/leaderboard`, or `/goals` to dig in.")
    return "\n".join(lines)


@tasks.loop(time=_daily_update_time())
async def daily_update() -> None:
    if DAILY_UPDATE_CHANNEL_ID is None:
        return
    channel = bot.get_channel(DAILY_UPDATE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DAILY_UPDATE_CHANNEL_ID)
        except discord.HTTPException:
            LOG.warning(
                "Daily update: cannot access channel %s", DAILY_UPDATE_CHANNEL_ID
            )
            return
    guild = getattr(channel, "guild", None)
    if guild is None:
        LOG.warning("Daily update channel %s is not in a guild", DAILY_UPDATE_CHANNEL_ID)
        return

    date_label, start_iso, end_iso = _daily_window(days_ago=1)
    text = _daily_update_text(
        guild.id,
        date_label,
        start_iso,
        end_iso,
        post_empty=DAILY_UPDATE_POST_EMPTY,
    )
    if text is None:
        LOG.info("Daily update skipped for %s: no activity", date_label)
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        LOG.info("Daily update posted to #%s for %s", channel, date_label)
    except discord.HTTPException:
        LOG.exception("Failed to post daily update")


@daily_update.before_loop
async def _before_daily_update() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


async def _backfill_channel(
    channel: discord.abc.Messageable, limit: int | None
) -> tuple[int, int, int, int]:
    """Scan a channel's history and store any detected lifts.

    Returns (messages_scanned, messages_with_lifts, lifts_inserted,
    skipped_suppressed). Dedupe on (message_id, equipment) means re-runs
    are safe.
    """
    scanned = matched = inserted = skipped = 0
    async for msg in channel.history(limit=limit, oldest_first=True):
        if msg.author.bot or not msg.guild:
            continue
        scanned += 1
        # Skip messages whose lifts the user explicitly undid; otherwise a
        # restart would resurrect them on every boot.
        if db.is_message_suppressed(msg.guild.id, msg.id):
            skipped += 1
            continue
        guild_aliases = _custom_alias_map(msg.guild.id)
        target, content = _message_lift_target(msg)
        # Nickname-prefix targeting: "Sean bench 30kg" resolves to the user
        # nicknamed Sean, exactly like a leading @mention would.
        if target == msg.author and msg.guild:
            nick_target, nick_content = await _resolve_nickname_target(
                msg.content, msg.guild
            )
            if nick_target is not None:
                target, content = nick_target, nick_content
        lifts = parse_message(content, custom_aliases=guild_aliases)
        lifts, _rejected = _split_reasonable_lifts(lifts)
        if not lifts:
            continue
        if not _should_auto_store(lifts):
            continue
        n = await _store_lifts(
            msg, lifts, target,
            logged_at=_resolve_date_hint(
                content, msg.created_at.astimezone(DISPLAY_TZ),
            ),
        )
        if n:
            matched += 1
            inserted += n
    return scanned, matched, inserted, skipped


async def _run_startup_backfill() -> None:
    limit = BACKFILL_LIMIT if BACKFILL_LIMIT > 0 else None
    for channel_id in GYM_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                LOG.warning("Backfill: cannot access channel %s", channel_id)
                continue
        LOG.info("Backfill: scanning #%s (limit=%s)", channel, limit)
        try:
            scanned, matched, inserted, skipped = await _backfill_channel(
                channel, limit,
            )
        except discord.Forbidden:
            LOG.warning("Backfill: missing permission to read #%s", channel)
            continue
        LOG.info(
            "Backfill done for #%s: scanned=%d, posts_with_lifts=%d, "
            "new_lifts=%d, skipped_suppressed=%d",
            channel, scanned, matched, inserted, skipped,
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    if GYM_CHANNEL_IDS and message.channel.id not in GYM_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    guild_aliases = _custom_alias_map(message.guild.id)
    target, content = _message_lift_target(message)
    # Nickname-prefix targeting: "Sean bench 30kg" resolves to the user
    # nicknamed Sean, exactly like a leading @mention would.
    if target == message.author and message.guild:
        nick_target, nick_content = await _resolve_nickname_target(
            message.content, message.guild
        )
        if nick_target is not None:
            target, content = nick_target, nick_content

    # Quick bodyweight update path: `bodyweight 100kg`, `body weight: 95.5`,
    # `bw 80`, or `@dos bodyweight 100kg` (leading mention re-targets just
    # like for lifts). Handled before parse_message so it doesn't get
    # filtered out as bodyweight chatter.
    bw_kg = _parse_bodyweight_message(content)
    if bw_kg is not None:
        await _handle_bodyweight_message(message, target, bw_kg)
        await bot.process_commands(message)
        return

    lifts = parse_message(content, custom_aliases=guild_aliases)
    lifts, rejected_lifts = _split_reasonable_lifts(lifts)
    # Backdated logging: phrases like "yesterday", "3 days ago", "monday",
    # or an ISO date in the message override the message's own timestamp
    # so a workout posted the morning after still files under the prior day.
    backdated_at = _resolve_date_hint(
        content, message.created_at.astimezone(DISPLAY_TZ),
    )
    # Auto-store when either:
    #  * the message is a clear "stats dump" (>= MIN_LIFTS_FOR_AUTO lifts), or
    #  * at least one lift was parsed with an explicit unit (kg / plates / BW+),
    #    which is a strong enough signal on its own (e.g. "Bench 100kg today").
    should_store = _should_auto_store(lifts)
    if lifts and should_store:
        # Detect PRs BEFORE inserting, so we can compare against the prior state.
        guild_id = message.guild.id if message.guild else 0
        target_user_id = int(getattr(target, "id"))
        prs = _new_prs_for_lifts(guild_id, target_user_id, lifts)

        inserted = await _store_lifts(
            message, lifts, target, logged_at=backdated_at,
        )
        if inserted > 0:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            # Extra hype reaction when the post contained a PR — gives a
            # visible signal in the channel that something special just
            # happened, without spamming a second bot reply.
            if prs:
                try:
                    await message.add_reaction("🎉")
                except discord.HTTPException:
                    pass
            # Check goal hits (PRs that meet or beat the user's goal).
            goal_hits = _check_goal_hits(guild_id, target_user_id, prs)

            # Reply with a short confirmation so the user can see exactly
            # what the bot understood from their message. Look up the target
            # lifter's latest bodyweight once so we can tag bodyweight-relative
            # lifts (assisted pull-ups, weighted dips, etc.) with their true
            # load — the suffix is a no-op for everyone else.
            target_bw = _user_bodyweight(guild_id, target_user_id)
            backdate_note = ""
            if backdated_at is not None:
                msg_local_date = message.created_at.astimezone(DISPLAY_TZ).date()
                used_local_date = backdated_at.astimezone(DISPLAY_TZ).date()
                if used_local_date != msg_local_date:
                    backdate_note = (
                        f" _(logged for {used_local_date.strftime('%Y-%m-%d')})_"
                    )
            try:
                if len(lifts) == 1:
                    lift = lifts[0]
                    reply = (
                        f"Added **{_format_weight(lift.weight_kg, lift.bodyweight_add)}"
                        f"{_true_weight_suffix(lift.equipment, lift.weight_kg, lift.bodyweight_add, target_bw)}**"
                        f" to **{lift.equipment}**"
                        f"{_target_suffix(message.author, target)}."
                    )
                else:
                    lines = [
                        f"Added {_plural(inserted, 'lift')}"
                        f"{_target_suffix(message.author, target)}:"
                    ]
                    lines.extend(_format_lift_lines(lifts, bodyweight=target_bw))
                    reply = "\n".join(lines)
                if backdate_note:
                    reply = reply + backdate_note
                if prs:
                    pr_lines = ["", "🎉 **New PR!**"]
                    for lift, prev in prs:
                        true_suf = _true_weight_suffix(
                            lift.equipment, lift.weight_kg,
                            lift.bodyweight_add, target_bw,
                        )
                        if prev is None:
                            pr_lines.append(
                                f"• **{lift.equipment}**: first logged at "
                                f"{_format_weight(lift.weight_kg, lift.bodyweight_add)}"
                                f"{true_suf}"
                            )
                        else:
                            gain = lift.weight_kg - prev
                            pr_lines.append(
                                f"• **{lift.equipment}**: "
                                f"{_format_weight(prev, lift.bodyweight_add)} → "
                                f"{_format_weight(lift.weight_kg, lift.bodyweight_add)}"
                                f"{true_suf} "
                                f"(+{gain:g}kg)"
                            )
                    reply += "\n" + "\n".join(pr_lines)
                if goal_hits:
                    reply += "\n\n🎯 **Goal hit!**"
                    for eq, tgt, bw in goal_hits:
                        reply += (
                            f"\n• **{eq}** — target "
                            f"{_format_weight(tgt, bw)} reached "
                            "(goal cleared)"
                        )
                reply += _rejected_lifts_note(rejected_lifts)
                reply += (
                    "\n-# React ❌ to this reply if I got it wrong — "
                    "the logger or target lifter can undo this entry."
                )
                sent = await message.reply(reply, mention_author=False)
                try:
                    db.track_reply(
                        reply_message_id=sent.id,
                        guild_id=guild_id,
                        user_id=message.author.id,
                        message_id=message.id,
                        lift_ids=None,
                        target_user_id=target_user_id,
                    )
                except Exception:  # pragma: no cover - non-critical
                    LOG.exception("Failed to track reply for undo")
            except discord.HTTPException:
                pass
            LOG.info(
                "Stored %d lifts from %s in #%s",
                inserted, target, message.channel,
            )
        else:
            # Lifts were detected but every one was a duplicate — give a quiet
            # signal so the author knows the bot saw it but didn't re-store.
            try:
                await message.add_reaction("🔁")
            except discord.HTTPException:
                pass
    elif rejected_lifts:
        try:
            await message.add_reaction("⚠️")
            await message.reply(
                _rejected_lifts_note(rejected_lifts).lstrip(),
                mention_author=False,
            )
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


@bot.event
async def on_message_edit(
    before: discord.Message, after: discord.Message,
) -> None:
    """Re-parse edited gym posts so corrections flow into the DB."""
    if after.author.bot or not after.guild:
        return
    if GYM_CHANNEL_IDS and after.channel.id not in GYM_CHANNEL_IDS:
        return
    if before.content == after.content:
        return  # ignore embed/attachment-only edits

    guild_id = after.guild.id
    aliases = _custom_alias_map(guild_id)
    target, content = _message_lift_target(after)
    # Nickname-prefix targeting consistent with on_message.
    if target == after.author and after.guild:
        nick_target, nick_content = await _resolve_nickname_target(
            after.content, after.guild
        )
        if nick_target is not None:
            target, content = nick_target, nick_content
    target_user_id = int(getattr(target, "id"))
    # Editing a post is a fresh signal of intent — clear any prior
    # backfill suppression so the corrected version can be re-imported.
    db.unsuppress_message(guild_id, after.id)
    db.retarget_replies_for_message(guild_id, after.id, target_user_id)
    new_lifts = parse_message(content, custom_aliases=aliases)
    new_lifts, _rejected = _split_reasonable_lifts(new_lifts)
    existing_rows = db.lifts_for_message(guild_id, after.id)
    wrong_target_ids = [
        int(row["id"]) for row in existing_rows
        if int(row["user_id"]) != target_user_id
    ]
    retargeted_removed = db.delete_lifts_by_ids(
        guild_id, None, wrong_target_ids,
    )
    existing_rows = [
        row for row in existing_rows if int(row["user_id"]) == target_user_id
    ]
    existing = {r["equipment"]: r for r in existing_rows}
    should_store = _should_auto_store(new_lifts)

    if not existing and not should_store:
        if retargeted_removed:
            try:
                await after.add_reaction("✏️")
            except discord.HTTPException:
                pass
        return

    if existing and new_lifts and not should_store:
        new_lifts = [
            lift for lift in new_lifts
            if lift.structured and lift.equipment in existing
        ]

    if not new_lifts:
        removed = retargeted_removed + db.delete_lifts_by_ids(
            guild_id, target_user_id, [int(r["id"]) for r in existing_rows]
        )
        if removed:
            try:
                await after.add_reaction("✏️")
            except discord.HTTPException:
                pass
            LOG.info(
                "Edit removed all stored lifts from message %s in #%s: -%d",
                after.id, after.channel, removed,
            )
        return

    fresh: list[Lift] = []
    updated = 0
    parsed_equipment = {lift.equipment for lift in new_lifts}
    stale_ids = [
        int(row["id"])
        for equipment, row in existing.items()
        if equipment not in parsed_equipment
    ]
    removed = retargeted_removed + db.delete_lifts_by_ids(
        guild_id, target_user_id, stale_ids,
    )

    for lift in new_lifts:
        prev = existing.get(lift.equipment)
        if prev is None:
            fresh.append(lift)
            continue
        if abs(prev["weight_kg"] - lift.weight_kg) > 1e-6 or \
                bool(prev["bw"]) != lift.bodyweight_add:
            db.update_lift_weight(
                int(prev["id"]), lift.weight_kg, lift.bodyweight_add,
                getattr(lift, "reps", None),
            )
            updated += 1

    inserted = 0
    if fresh:
        inserted = await _store_lifts(
            after, fresh, target,
            logged_at=_resolve_date_hint(
                content, after.created_at.astimezone(DISPLAY_TZ),
            ),
        )

    if inserted or updated or removed:
        try:
            await after.add_reaction("✏️")
        except discord.HTTPException:
            pass
        LOG.info(
            "Edit applied to message %s in #%s: +%d new, %d updated, -%d removed",
            after.id, after.channel, inserted, updated, removed,
        )


def _check_goal_hits(
    guild_id: int, user_id: int,
    prs: list[tuple[Lift, float | None]],
) -> list[tuple[str, float, bool]]:
    """For each PR that meets or exceeds an active goal, return the cleared
    goals as (equipment, target_kg, bw) tuples. Cleared goals are deleted
    from the DB so they don't keep firing on subsequent posts."""
    cleared: list[tuple[str, float, bool]] = []
    for lift, _prev in prs:
        goal = db.goal_get(guild_id, user_id, lift.equipment)
        if goal is None:
            continue
        if lift.weight_kg >= goal["target_kg"]:
            cleared.append((
                lift.equipment, goal["target_kg"], bool(goal["bw"])
            ))
            db.goal_remove(guild_id, user_id, lift.equipment)
    return cleared


@bot.event
async def on_raw_reaction_add(
    payload: discord.RawReactionActionEvent,
) -> None:
    """Logger-or-target reaction undo for a tracked bot reply."""
    if payload.user_id == (bot.user.id if bot.user else 0):
        return
    if str(payload.emoji) not in ("❌", "✖️", "🚫"):
        return
    rec = db.get_reply(payload.message_id)
    if rec is None:
        return
    target_user_id = int(rec["target_user_id"])
    allowed = {int(rec["user_id"]), target_user_id} | ADMIN_USER_IDS
    if payload.user_id not in allowed:
        return  # Someone else tried to undo — ignore silently.

    # Race protection: claim the reply by deleting its tracking row first.
    # If two ❌ reactions land at once, only one gets rowcount==1 and goes
    # on to delete the lifts; the other no-ops.
    if db.delete_reply(payload.message_id) == 0:
        return

    guild_id = rec["guild_id"]
    removed = 0
    if rec["lift_ids"]:
        ids = [int(x) for x in rec["lift_ids"].split(",") if x]
        removed = db.delete_lifts_by_ids(guild_id, target_user_id, ids)
    elif rec["message_id"] is not None:
        removed = db.delete_lifts_for_message(
            guild_id, target_user_id, rec["message_id"]
        )
    # Always suppress, even when removed==0: the user's clear intent is
    # "don't keep this post". If the rows were already gone (e.g. a prior
    # /undo), a future backfill could still re-import the same source
    # message without this guard.
    if rec["message_id"] is not None:
        db.suppress_message(guild_id, int(rec["message_id"]))

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return
    try:
        reply_msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    by_admin = (
        payload.user_id in ADMIN_USER_IDS
        and payload.user_id not in {int(rec["user_id"]), target_user_id}
    )
    actor = "an admin" if by_admin else "the user"
    note = (
        f"↩️ Undid {_plural(removed, 'stored lift')} at {actor}'s request."
        if removed
        else "↩️ Nothing to undo (already removed)."
    )
    try:
        await reply_msg.edit(content=f"{reply_msg.content}\n\n{note}")
    except discord.HTTPException:
        pass
    # Also drop the original gym post's ✅ reaction so the visual state
    # matches reality.
    if rec["message_id"]:
        try:
            original = await channel.fetch_message(rec["message_id"])
            await original.remove_reaction("✅", bot.user)  # type: ignore[arg-type]
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="stats", description="Show a user's personal bests.")
@app_commands.describe(user="The user to look up (defaults to you).")
async def stats_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    rows = db.personal_bests(guild_id, target.id)
    if not rows:
        await interaction.response.send_message(
            f"No lifts logged for {target.display_name} yet.", ephemeral=True
        )
        return

    lines = [f"**{target.display_name} — personal bests**"]
    for r in rows:
        date = _format_date(r["set_on"])
        lines.append(
            f"• {r['equipment']}: {_format_weight(r['best'], bool(r['bw']))}"
            f"  _(set {date})_"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="progress", description="Show monthly progression on one lift.")
@app_commands.describe(
    equipment="Equipment / lift name",
    user="The user to look up (defaults to you).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def progress_cmd(
    interaction: discord.Interaction,
    equipment: str,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.progress(guild_id, target.id, canon)
    if not rows:
        await interaction.response.send_message(
            f"No {canon} history for {target.display_name}.", ephemeral=True
        )
        return

    lines = [f"**{target.display_name} — {canon} by month**"]
    prev: float | None = None
    for r in rows:
        best = r["best"]
        delta = ""
        if prev is not None:
            d = best - prev
            if d:
                delta = f"  ({'+' if d > 0 else ''}{d:g}kg)"
        date = _format_date(r["first_seen"])
        lines.append(
            f"• {r['month']} (first logged {date}): "
            f"{_format_weight(best, bool(r['bw']))}{delta}"
        )
        prev = best
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="leaderboard", description="Top lifters for an equipment.")
@app_commands.describe(equipment="Equipment / lift name")
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def leaderboard_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.leaderboard(guild_id, canon)
    if not rows:
        await interaction.response.send_message(
            f"No entries for {canon} yet.", ephemeral=True
        )
        return

    lines = [f"**Leaderboard — {canon}**"]
    medals = ["🥇", "🥈", "🥉"]
    # Pull every lifter's most recent bodyweight in one query so we can show
    # the *true* load on bodyweight-relative lifts (assisted pull-ups, etc.).
    user_ids = [int(r["user_id"]) for r in rows]
    bw_map = db.latest_bodyweights_bulk(guild_id, user_ids)
    for i, r in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        date = _format_date(r["set_on"])
        true_suf = _true_weight_suffix(
            canon, float(r["best"]), bool(r["bw"]),
            bw_map.get(int(r["user_id"])),
        )
        lines.append(
            f"{prefix} {r['username']} — "
            f"{_format_weight(r['best'], bool(r['bw']))}{true_suf}"
            f"  _(set {date})_"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="bodyweight",
    description="Record your current bodyweight (or view it if no weight given).",
)
@app_commands.describe(
    weight_kg="Your current bodyweight in kg. Omit to view your last entry.",
    user="Whose bodyweight to set/view (defaults to you).",
)
async def bodyweight_cmd(
    interaction: discord.Interaction,
    weight_kg: float | None = None,
    user: discord.Member | None = None,
) -> None:
    guild_id = interaction.guild_id or 0
    target = user or interaction.user
    # If no value supplied, just report the latest entry. Useful for sanity
    # checking what the bot is using to compute true weights.
    if weight_kg is None:
        row = db.get_latest_bodyweight(guild_id, target.id)
        if row is None:
            await interaction.response.send_message(
                f"No bodyweight on file for **{_display_name(target)}** yet. "
                "Use `/bodyweight weight_kg:<kg>` to record one — it will be "
                "used to show the true load on pull-ups, dips, and other "
                "bodyweight-relative lifts.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"**{_display_name(target)}**'s bodyweight: "
            f"**{float(row['weight_kg']):g}kg** "
            f"(updated {_format_date(row['recorded_at'])}).",
            ephemeral=True,
        )
        return

    if weight_kg <= 0:
        await interaction.response.send_message(
            "Bodyweight must be a positive number of kg.", ephemeral=True
        )
        return
    # Reuse MAX_WEIGHT_KG as a sanity ceiling so a fat-fingered "1500" can't
    # silently make every leaderboard line look ridiculous.
    if MAX_WEIGHT_KG > 0 and weight_kg > MAX_WEIGHT_KG:
        await interaction.response.send_message(
            f"That bodyweight looks too high to be real ({weight_kg:g}kg > "
            f"{MAX_WEIGHT_KG:g}kg).",
            ephemeral=True,
        )
        return

    db.set_bodyweight(guild_id, target.id, weight_kg)
    suffix = _target_suffix(interaction.user, target)
    await interaction.response.send_message(
        f"Recorded bodyweight **{weight_kg:g}kg**{suffix}. The bot will now "
        "show your true load on bodyweight-relative lifts (e.g. assisted "
        "pull-ups, weighted dips)."
    )


@bot.tree.command(name="log", description="Manually log a single lift.")
@app_commands.describe(
    equipment="Equipment / lift name",
    weight_kg="Weight in kg (use 0 with bodyweight=True for pure BW work)",
    user="Who this lift belongs to (defaults to you).",
    bodyweight="True if this weight is added on top of bodyweight",
    date="Optional: 'yesterday', 'monday', '3 days ago', or YYYY-MM-DD",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def log_cmd(
    interaction: discord.Interaction,
    equipment: str,
    weight_kg: float,
    user: discord.Member | None = None,
    bodyweight: bool = False,
    date: str | None = None,
) -> None:
    if weight_kg < 0:
        await interaction.response.send_message(
            "Weight must be zero or positive.", ephemeral=True
        )
        return
    if weight_kg == 0 and not bodyweight:
        await interaction.response.send_message(
            "Use `bodyweight:True` for pure BW work, or enter a positive kg value.",
            ephemeral=True,
        )
        return
    if MAX_WEIGHT_KG > 0 and weight_kg > MAX_WEIGHT_KG:
        await interaction.response.send_message(
            f"That looks too high to log safely ({weight_kg:g}kg > "
            f"{MAX_WEIGHT_KG:g}kg). If it is intentional, raise `MAX_WEIGHT_KG`.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    target = user or interaction.user
    canon = _resolve(guild_id, equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name.", ephemeral=True
        )
        return

    lift = Lift(equipment=canon, weight_kg=weight_kg,
                bodyweight_add=bodyweight, raw=f"/log {equipment} {weight_kg}")
    logged_at = datetime.now(timezone.utc)
    if date:
        resolved = _resolve_date_hint(date, datetime.now(DISPLAY_TZ))
        if resolved is None:
            await interaction.response.send_message(
                f"Couldn't understand `date={date}`. Try `yesterday`, "
                "`monday`, `3 days ago`, or `YYYY-MM-DD`.",
                ephemeral=True,
            )
            return
        logged_at = resolved
    prev = db.previous_best(guild_id, target.id, canon)
    inserted_ids = db.add_lifts_returning_ids(
        guild_id=guild_id,
        user_id=target.id,
        username=_display_name(target),
        lifts=[lift],
        message_id=None,
        channel_id=interaction.channel_id,
        logged_at=logged_at,
    )
    if inserted_ids:
        suffix = _target_suffix(interaction.user, target)
        target_bw = _user_bodyweight(guild_id, target.id)
        true_suf = _true_weight_suffix(canon, weight_kg, bodyweight, target_bw)
        msg = (
            f"Logged {canon}: {_format_weight(weight_kg, bodyweight)}"
            f"{true_suf}{suffix}."
        )
        if date:
            used_local = logged_at.astimezone(DISPLAY_TZ).date()
            msg += f" _(logged for {used_local.strftime('%Y-%m-%d')})_"
        is_pr = weight_kg > 0 and (prev is None or weight_kg > prev)
        if is_pr:
            if prev is None:
                msg += "\n🎉 **New PR!** (first entry for this lift)"
            else:
                gain = weight_kg - prev
                msg += (
                    f"\n🎉 **New PR!** "
                    f"{_format_weight(prev, bodyweight)} → "
                    f"{_format_weight(weight_kg, bodyweight)} (+{gain:g}kg)"
                )
        # Goal hit check — uses the same semantics as auto-parse.
        goal = db.goal_get(guild_id, target.id, canon)
        if goal and weight_kg >= goal["target_kg"]:
            msg += (
                f"\n🎯 **Goal hit!** Target "
                f"{_format_weight(goal['target_kg'], bool(goal['bw']))} "
                "reached (goal cleared)."
            )
            db.goal_remove(guild_id, target.id, canon)
        msg += (
            "\n-# React ❌ to this response or use `/undo` "
            "if this was logged by mistake. The logger or target lifter can react."
        )
        await interaction.response.send_message(msg)
        try:
            sent = await interaction.original_response()
            db.track_reply(
                reply_message_id=sent.id,
                guild_id=guild_id,
                user_id=interaction.user.id,
                message_id=None,
                lift_ids=inserted_ids,
                target_user_id=target.id,
            )
        except Exception:  # pragma: no cover - discord runtime only
            LOG.exception("Failed to track /log response for undo")
    else:
        await interaction.response.send_message(
            "Could not log that entry.", ephemeral=True
        )


@bot.tree.command(
    name="history",
    description="Timeline of every logged entry for one lift.",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    user="The user to look up (defaults to you).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def history_cmd(
    interaction: discord.Interaction,
    equipment: str,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon)
    if not rows:
        await interaction.response.send_message(
            f"No {canon} history for {target.display_name}.", ephemeral=True
        )
        return

    lines = [f"**{target.display_name} — {canon} timeline**"]
    prev: float | None = None
    for r in rows:
        w = r["weight_kg"]
        delta = ""
        if prev is not None:
            d = w - prev
            if d:
                delta = f"  ({'+' if d > 0 else ''}{d:g}kg)"
        # If we captured rep count, show an Epley 1RM estimate alongside the
        # raw weight — only meaningful for low-rep working sets.
        reps = r["reps"] if "reps" in r.keys() else None
        one_rm = estimated_one_rep_max(w, reps) if reps else None
        rm_str = f"  _est. 1RM ≈ {one_rm:g}kg_" if one_rm else ""
        rep_str = f"  ×{reps}" if reps else ""
        lines.append(
            f"• {_format_date(r['logged_at'])}: "
            f"{_format_weight(w, bool(r['bw']))}{rep_str}{delta}{rm_str}"
        )
        prev = w
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="parse",
    description="Reparse a message by ID in this channel and store detected lifts.",
)
@app_commands.describe(message_id="The ID of the message to reparse")
async def parse_cmd(
    interaction: discord.Interaction, message_id: str
) -> None:
    if not message_id.isdigit():
        await interaction.response.send_message(
            "message_id must be numeric.", ephemeral=True
        )
        return
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message(
            "Message not found in this channel.", ephemeral=True
        )
        return

    target, content = _message_lift_target(msg)
    lifts = parse_message(
        content,
        custom_aliases=_custom_alias_map(interaction.guild_id or 0),
    )
    lifts, rejected_lifts = _split_reasonable_lifts(lifts)
    if not lifts:
        note = _rejected_lifts_note(rejected_lifts).lstrip()
        await interaction.response.send_message(
            note or "No lifts detected in that message.", ephemeral=True
        )
        return
    inserted = await _store_lifts(
        msg, lifts, target,
        logged_at=_resolve_date_hint(content, msg.created_at.astimezone(DISPLAY_TZ)),
    )
    date = _format_date(msg.created_at.isoformat())
    lines = [
        f"Stored {_plural(inserted, 'new lift')} for {_display_name(target)} "
        f"_(posted {date})_:"
    ]
    lines.extend(_format_lift_lines(lifts))
    note = _rejected_lifts_note(rejected_lifts)
    if note:
        lines.append(note)
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="machine",
    description="Timeline of everyone's entries for one lift.",
)
@app_commands.describe(equipment="Equipment / lift name")
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def machine_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.machine_history(guild_id, canon)
    if not rows:
        await interaction.response.send_message(
            f"No entries for {canon} yet.", ephemeral=True
        )
        return

    lines = [f"**Timeline — {canon}**"]
    # Track each user's previous weight so we can show deltas per person.
    last_by_user: dict[str, float] = {}
    for r in rows:
        user = r["username"]
        w = r["weight_kg"]
        delta = ""
        prev = last_by_user.get(user)
        if prev is not None:
            d = w - prev
            if d:
                delta = f"  ({'+' if d > 0 else ''}{d:g}kg)"
        last_by_user[user] = w
        lines.append(
            f"• {_format_date(r['logged_at'])} — **{user}**: "
            f"{_format_weight(w, bool(r['bw']))}{delta}"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="version", description="Show the bot's version info.")
async def version_cmd(interaction: discord.Interaction) -> None:
    if REMINDER_CHANNEL_ID:
        reminder_line = (
            f"reminder: {_WEEKDAY_NAMES[REMINDER_WEEKDAY % 7]} "
            f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} ({DISPLAY_TZ}) "
            f"in <#{REMINDER_CHANNEL_ID}>"
        )
    else:
        reminder_line = "reminder: off"
    if DAILY_UPDATE_CHANNEL_ID:
        daily_line = (
            f"daily update: {DAILY_UPDATE_HOUR:02d}:{DAILY_UPDATE_MINUTE:02d} "
            f"({DISPLAY_TZ}) in <#{DAILY_UPDATE_CHANNEL_ID}>"
        )
    else:
        daily_line = "daily update: off"
    if BODYWEIGHT_REMINDER_CHANNEL_ID:
        bw_reminder_line = (
            f"bodyweight reminder: "
            f"{_WEEKDAY_NAMES[BODYWEIGHT_REMINDER_WEEKDAY % 7]} "
            f"{BODYWEIGHT_REMINDER_HOUR:02d}:{BODYWEIGHT_REMINDER_MINUTE:02d} "
            f"({DISPLAY_TZ}) in <#{BODYWEIGHT_REMINDER_CHANNEL_ID}>"
        )
    else:
        bw_reminder_line = "bodyweight reminder: off"
    lines = [
        f"**gym-bot v{__version__}**",
        f"discord.py: {discord.__version__}",
        f"auto-scan channels: {len(GYM_CHANNEL_IDS) or 'all'}",
        f"backfill on start: {'on' if BACKFILL_ON_START else 'off'}"
        f" (limit={BACKFILL_LIMIT or 'unlimited'})",
        f"show lb: {'on' if SHOW_LB else 'off'}",
        f"max auto/log weight: {MAX_WEIGHT_KG:g}kg"
        if MAX_WEIGHT_KG > 0 else "max auto/log weight: off",
        f"display timezone: {DISPLAY_TZ}",
        reminder_line,
        bw_reminder_line,
        daily_line,
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_cmd(interaction: discord.Interaction) -> None:
    # Gateway (websocket) latency reported by discord.py, in ms.
    gateway_ms = round(bot.latency * 1000)
    # Round-trip latency: how long between Discord sending us the interaction
    # and us acknowledging it.
    sent_at = interaction.created_at
    rtt_ms = round((datetime.now(timezone.utc) - sent_at).total_seconds() * 1000)
    await interaction.response.send_message(
        f"Pong! 🏓  gateway: {gateway_ms} ms · round-trip: {rtt_ms} ms"
    )


@bot.tree.command(
    name="backfill",
    description="Rescan this channel's history and import any missed lifts.",
)
@app_commands.describe(
    limit="Max messages to scan (default 1000, use 0 for no limit).",
)
async def backfill_cmd(
    interaction: discord.Interaction, limit: int = 1000
) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    lim = limit if limit and limit > 0 else None
    try:
        scanned, matched, inserted, skipped = await _backfill_channel(
            interaction.channel, lim
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Backfill complete — scanned {scanned} messages, "
        f"{matched} had lifts, {inserted} new lifts stored, "
        f"{skipped} skipped (suppressed).",
        ephemeral=True,
    )


# Marker text the reaction-undo handler appends to the bot's reply when it
# successfully removes lifts. Used by /cleanup_resurrected to find historical
# undo events whose source posts may have been re-imported by a later
# backfill (before the suppression mechanism existed). We only match the
# "actually removed" footer — "Nothing to undo" replies aren't useful
# evidence that a post should stay suppressed.
_UNDO_FOOTER_MARKER = "↩️ Undid"


async def _scan_channel_for_undone_messages(
    channel: discord.abc.Messageable, limit: int | None,
) -> tuple[int, set[int]]:
    """Walk channel history and collect source-message ids that were undone.

    A "previously undone" reply is one of *our own* messages whose content
    contains the undo footer and that was sent as a reply to the original
    gym post. The referenced message id is the source post we should
    suppress and clean up.

    Returns (messages_scanned, source_message_ids).
    """
    bot_user_id = bot.user.id if bot.user else 0
    scanned = 0
    source_ids: set[int] = set()
    async for msg in channel.history(limit=limit, oldest_first=True):
        scanned += 1
        if msg.author.id != bot_user_id:
            continue
        if _UNDO_FOOTER_MARKER not in msg.content:
            continue
        ref = msg.reference
        ref_id = getattr(ref, "message_id", None) if ref is not None else None
        if ref_id is not None:
            source_ids.add(int(ref_id))
    return scanned, source_ids


@bot.tree.command(
    name="cleanup_resurrected",
    description=(
        "Admin: remove lifts that a backfill re-added after they were undone."
    ),
)
@app_commands.describe(
    limit="Max messages to scan per channel (default 5000, 0 for no limit).",
    all_channels=(
        "Scan every configured gym channel (default). Set false to scan "
        "only the channel the command was used in."
    ),
    dry_run=(
        "Preview only — don't delete or suppress anything (default True). "
        "Set False to actually apply the cleanup."
    ),
)
async def cleanup_resurrected_cmd(
    interaction: discord.Interaction,
    limit: int = 5000,
    all_channels: bool = True,
    dry_run: bool = True,
) -> None:
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            "Admins only.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    lim = limit if limit and limit > 0 else None

    if all_channels and GYM_CHANNEL_IDS:
        channel_ids = list(GYM_CHANNEL_IDS)
    elif interaction.channel is not None:
        channel_ids = [interaction.channel.id]
    else:
        await interaction.followup.send(
            "No channel to scan.", ephemeral=True
        )
        return

    guild_id = interaction.guild_id or 0
    total_scanned = 0
    total_sources = 0
    total_removable = 0
    total_suppressed_new = 0
    per_channel: list[str] = []

    for channel_id in channel_ids:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                per_channel.append(f"• <#{channel_id}>: cannot access")
                continue
        try:
            scanned, source_ids = await _scan_channel_for_undone_messages(
                channel, lim,
            )
        except discord.Forbidden:
            per_channel.append(
                f"• {getattr(channel, 'mention', f'#{channel_id}')}: "
                "missing read-history permission"
            )
            continue

        ch_removable = 0
        ch_suppressed_new = 0
        for msg_id in source_ids:
            existing = db.count_lifts_for_message(guild_id, msg_id)
            already_suppressed = db.is_message_suppressed(guild_id, msg_id)
            ch_removable += existing
            if not already_suppressed:
                ch_suppressed_new += 1
            if not dry_run:
                if existing:
                    db.delete_lifts_for_message_any_user(guild_id, msg_id)
                db.suppress_message(guild_id, msg_id)

        total_scanned += scanned
        total_sources += len(source_ids)
        total_removable += ch_removable
        total_suppressed_new += ch_suppressed_new
        per_channel.append(
            f"• {getattr(channel, 'mention', f'#{channel_id}')}: "
            f"scanned {scanned}, undone-posts found {len(source_ids)}, "
            f"lifts {'would remove' if dry_run else 'removed'} "
            f"{ch_removable}, "
            f"{'would suppress' if dry_run else 'newly suppressed'} "
            f"{ch_suppressed_new}"
        )

    header_label = "DRY-RUN preview" if dry_run else "Cleanup complete"
    summary_lines = [
        f"**{header_label}.**",
        f"Channels scanned: {len(channel_ids)}",
        f"Messages scanned: {total_scanned}",
        f"Previously-undone source posts: {total_sources}",
        f"Resurrected lifts {'to remove' if dry_run else 'removed'}: "
        f"{total_removable}",
        f"{'Suppressions to add' if dry_run else 'New suppression rows'}: "
        f"{total_suppressed_new}",
    ]
    if dry_run:
        summary_lines.append(
            "_Re-run with `dry_run:false` to apply._"
        )
    if BACKFILL_LIMIT and lim and lim > BACKFILL_LIMIT:
        summary_lines.append(
            f"_Note: scan limit ({lim}) exceeds BACKFILL_LIMIT "
            f"({BACKFILL_LIMIT}); rows beyond BACKFILL_LIMIT can't be "
            "re-imported anyway, so suppressing them is precautionary._"
        )
    summary_lines.append("")
    summary_lines.extend(per_channel)

    # Discord caps individual messages at 2000 chars. Split the summary
    # into chunks so a long per-channel report doesn't get rejected.
    await _send_chunked_followup(interaction, summary_lines)


async def _send_chunked_followup(
    interaction: discord.Interaction, lines: list[str], limit: int = 1900,
) -> None:
    """Send `lines` as one or more ephemeral followups, each under `limit`
    chars. Splits on line boundaries so we don't break formatting.
    """
    buf: list[str] = []
    size = 0
    for line in lines:
        # +1 for the newline we'll add when joining.
        if size + len(line) + 1 > limit and buf:
            await interaction.followup.send("\n".join(buf), ephemeral=True)
            buf = []
            size = 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        await interaction.followup.send("\n".join(buf), ephemeral=True)


@bot.tree.command(
    name="suppress_message",
    description="Admin: mark a source post id as 'do not import'.",
)
@app_commands.describe(
    message_id="The original gym post's message ID to suppress.",
    delete_existing=(
        "Also delete any currently-stored lifts tied to this message "
        "(default True)."
    ),
)
async def suppress_message_cmd(
    interaction: discord.Interaction,
    message_id: str,
    delete_existing: bool = True,
) -> None:
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            "Admins only.", ephemeral=True
        )
        return
    if not message_id.isdigit():
        await interaction.response.send_message(
            "message_id must be a numeric Discord message ID.",
            ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    mid = int(message_id)
    removed = 0
    if delete_existing:
        removed = db.delete_lifts_for_message_any_user(guild_id, mid)
    already = db.is_message_suppressed(guild_id, mid)
    db.suppress_message(guild_id, mid)
    await interaction.response.send_message(
        f"Suppressed message `{mid}`. Lifts removed: {removed}. "
        f"{'Already suppressed before this call.' if already else 'New suppression row.'}",
        ephemeral=True,
    )


# Owner-only: download the live SQLite DB. Hard-coded to one user id so a
# misconfigured ADMIN_USER_IDS env doesn't accidentally leak the DB.
_DB_DUMP_OWNER_ID = 1072114272064262154


@bot.tree.command(
    name="db_dump",
    description="Owner only: DM yourself a copy of the live SQLite database.",
)
async def db_dump_cmd(interaction: discord.Interaction) -> None:
    if interaction.user.id != _DB_DUMP_OWNER_ID:
        await interaction.response.send_message(
            "This command is restricted.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    db_path = Path(DB_PATH)
    if not db_path.exists():
        await interaction.followup.send(
            f"DB file not found at `{db_path}`.", ephemeral=True
        )
        return

    # Snapshot via SQLite's online backup API so we get a consistent copy
    # even if writes are happening. Using a temp file keeps the live DB
    # untouched and avoids reading partial WAL state.
    with tempfile.NamedTemporaryFile(
        prefix="gym-db-", suffix=".sqlite3", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(tmp_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        size_bytes = tmp_path.stat().st_size
        # Discord's per-attachment limit for non-Nitro bots is 25 MiB.
        if size_bytes > 24 * 1024 * 1024:
            await interaction.followup.send(
                f"DB snapshot is {size_bytes/1024/1024:.1f} MiB — too "
                "large to attach. Pull it directly from the host volume.",
                ephemeral=True,
            )
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"gym-{stamp}.sqlite3"
        try:
            user = interaction.user
            dm = await user.create_dm()
            await dm.send(
                content=(
                    f"Snapshot of `{db_path.name}` "
                    f"({size_bytes/1024:.1f} KiB) taken at {stamp}."
                ),
                file=discord.File(str(tmp_path), filename=filename),
            )
            await interaction.followup.send(
                f"Sent {filename} to your DMs.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't DM you — open your DMs from server members and "
                "try again.",
                ephemeral=True,
            )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


@bot.tree.command(
    name="chat_dump",
    description="Owner only: DM yourself a transcript of recent channel messages.",
)
@app_commands.describe(
    limit="How many recent messages to grab (1-5000, default 1000).",
    include_bots="Include bot messages too (default False).",
)
async def chat_dump_cmd(
    interaction: discord.Interaction,
    limit: int = 1000,
    include_bots: bool = False,
) -> None:
    if interaction.user.id != _DB_DUMP_OWNER_ID:
        await interaction.response.send_message(
            "This command is restricted.", ephemeral=True
        )
        return

    channel = interaction.channel
    if channel is None or not hasattr(channel, "history"):
        await interaction.response.send_message(
            "This channel doesn't support history reads.", ephemeral=True
        )
        return

    limit = max(1, min(5000, limit))
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Pull oldest→newest so the transcript reads top-to-bottom in time
    # order, which is what a human (or another LLM) would want for
    # spotting friction patterns.
    lines: list[str] = []
    skipped_bots = 0
    fetched = 0
    try:
        async for msg in channel.history(limit=limit, oldest_first=True):
            fetched += 1
            if msg.author.bot and not include_bots:
                skipped_bots += 1
                continue
            ts = msg.created_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%SZ"
            )
            author = f"{msg.author.display_name} ({msg.author.id})"
            content = msg.content or ""
            # Note attachments / embeds inline so context isn't lost when
            # the message itself was just a screenshot or share.
            extras: list[str] = []
            if msg.attachments:
                extras.append(
                    "attachments=" + ", ".join(
                        a.filename for a in msg.attachments
                    )
                )
            if msg.embeds:
                extras.append(f"embeds={len(msg.embeds)}")
            if msg.reference and msg.reference.message_id:
                extras.append(f"reply_to={msg.reference.message_id}")
            extras_str = f" [{'; '.join(extras)}]" if extras else ""
            # Indent multi-line content so block boundaries stay obvious.
            body = content.replace("\n", "\n    ")
            lines.append(
                f"[{ts}] {author} (msg {msg.id}){extras_str}\n    {body}"
            )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.",
            ephemeral=True,
        )
        return

    header = (
        f"# Channel transcript\n"
        f"# guild_id={interaction.guild_id} channel_id={channel.id} "
        f"channel_name={getattr(channel, 'name', '?')}\n"
        f"# fetched={fetched} kept={len(lines)} "
        f"skipped_bots={skipped_bots} include_bots={include_bots}\n"
        f"# generated_at={datetime.now(timezone.utc).isoformat()}\n\n"
    )
    blob = header + "\n\n".join(lines)
    data = blob.encode("utf-8")

    if len(data) > 24 * 1024 * 1024:
        await interaction.followup.send(
            f"Transcript is {len(data)/1024/1024:.1f} MiB — too large "
            "to attach. Lower the limit and try again.",
            ephemeral=True,
        )
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chan_name = getattr(channel, "name", "channel")
    filename = f"chat-{chan_name}-{stamp}.txt"
    with tempfile.NamedTemporaryFile(
        prefix="gym-chat-", suffix=".txt", delete=False,
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                content=(
                    f"Transcript of #{chan_name} — kept {len(lines)} of "
                    f"{fetched} messages ({len(data)/1024:.1f} KiB)."
                ),
                file=discord.File(str(tmp_path), filename=filename),
            )
            await interaction.followup.send(
                f"Sent {filename} to your DMs.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't DM you — open your DMs from server members and "
                "try again.",
                ephemeral=True,
            )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


@bot.tree.command(
    name="purge",
    description="Delete every row for a specific equipment name.",
)
@app_commands.describe(
    equipment="Equipment name to remove (use the exact stored name)",
    confirm="Set to True to actually delete (default False shows a preview).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def purge_cmd(
    interaction: discord.Interaction, equipment: str,
    confirm: bool = False,
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    if not canon:
        await interaction.response.send_message(
            f"Couldn't read `{equipment}` as an equipment name.",
            ephemeral=True,
        )
        return
    available = db.count_equipment_rows(guild_id, canon)
    if available == 0:
        suggestions = _suggest_equipment(guild_id, canon)
        hint = (
            f"\nDid you mean: {', '.join('`' + s + '`' for s in suggestions)}?"
            if suggestions else ""
        )
        await interaction.response.send_message(
            f"No rows found for `{canon}`.{hint}", ephemeral=True
        )
        return
    if not confirm:
        await interaction.response.send_message(
            f"Would delete **{available}** row(s) for `{canon}`. "
            "Re-run with `confirm:True` to actually purge.",
            ephemeral=True,
        )
        return
    n = db.delete_equipment(guild_id, canon)
    await interaction.response.send_message(
        f"Removed {n} row(s) for `{canon}`.", ephemeral=True
    )


@bot.tree.command(
    name="rename",
    description="Re-label rows from one equipment name to another (yours, someone else's, or guild-wide).",
)
@app_commands.describe(
    old="The current (bad / misparsed) equipment name.",
    new="The correct equipment to merge the rows into.",
    user="Whose entries to rename. Defaults to you.",
    scope=(
        "'mine' (default) renames only your rows; "
        "'all' renames every matching row in the guild."
    ),
    confirm="Required when scope=all (guild-wide rename) — set True to proceed.",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="mine", value="mine"),
    app_commands.Choice(name="all", value="all"),
])
@app_commands.autocomplete(
    old=_equipment_autocomplete,
    new=_equipment_autocomplete,
)
async def rename_cmd(
    interaction: discord.Interaction,
    old: str,
    new: str,
    user: discord.Member | None = None,
    scope: app_commands.Choice[str] | None = None,
    confirm: bool = False,
) -> None:
    # Resolve who the rename targets. Precedence:
    #   * explicit `user` argument wins
    #   * scope=all means guild-wide (no user filter)
    #   * default is the caller themselves
    scope_value = scope.value if scope else "mine"
    if user is not None:
        target_user_id: int | None = user.id
        target_label = user.display_name
    elif scope_value == "all":
        target_user_id = None
        target_label = "everyone"
    else:
        target_user_id = interaction.user.id
        target_label = "your"

    guild_id = interaction.guild_id or 0
    src = _resolve(guild_id, old)
    dst = _resolve(guild_id, new)
    if not src or not dst:
        await interaction.response.send_message(
            "Both `old` and `new` must be non-empty equipment names.",
            ephemeral=True,
        )
        return
    if src == dst:
        await interaction.response.send_message(
            f"Source and destination both resolve to `{src}` — nothing to do.",
            ephemeral=True,
        )
        return

    # Bail early if there's nothing to rename, so we don't post a misleading
    # "0 row(s)" success message in the channel.
    available = db.count_equipment_rows(guild_id, src, target_user_id)
    if available == 0:
        scope_text = (
            "you" if target_user_id == interaction.user.id and user is None
            else target_label
        )
        suggestions = _suggest_equipment(guild_id, src)
        hint = (
            f"\nDid you mean: {', '.join('`' + s + '`' for s in suggestions)}?"
            if suggestions else ""
        )
        await interaction.response.send_message(
            f"No `{src}` rows found for {scope_text}.{hint}",
            ephemeral=True,
        )
        return

    # Guild-wide renames (no user filter) require explicit confirmation —
    # they're easy to fire accidentally and affect everyone's history.
    if target_user_id is None and not confirm:
        await interaction.response.send_message(
            f"Would rename **{available}** row(s) guild-wide: "
            f"`{src}` → `{dst}`. Re-run with `confirm:True` to proceed.",
            ephemeral=True,
        )
        return

    n = db.rename_equipment(guild_id, src, dst, user_id=target_user_id)
    if target_user_id is None:
        scope_msg = "guild-wide"
    elif target_user_id == interaction.user.id and user is None:
        scope_msg = "your entries"
    else:
        scope_msg = f"{target_label}'s entries"
    # When scoped to the caller, send ephemerally so we don't clutter the
    # channel with everyone's individual cleanups.
    ephemeral = target_user_id == interaction.user.id and user is None
    await interaction.response.send_message(
        f"Re-labelled {n} row(s) ({scope_msg}): `{src}` → `{dst}`.",
        ephemeral=ephemeral,
    )


@bot.tree.command(
    name="delete_entry",
    description="Delete one day's entries for a lift (yours by default).",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    date="Date of the entry to remove (YYYY-MM-DD)",
    user="Target user (defaults to you).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def delete_entry_cmd(
    interaction: discord.Interaction,
    equipment: str,
    date: str,
    user: discord.Member | None = None,
) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        await interaction.response.send_message(
            "`date` must be in YYYY-MM-DD format.", ephemeral=True
        )
        return

    target = user or interaction.user
    canon = _resolve(interaction.guild_id or 0, equipment)
    start_iso, end_iso = _local_date_window(date)
    n = db.delete_entry_between(
        interaction.guild_id or 0, canon, start_iso, end_iso, user_id=target.id
    )
    await interaction.response.send_message(
        f"Deleted {n} entry(ies) for {target.display_name} — `{canon}` on {date}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="change_weight",
    description="Change the latest matching weight for you or another user.",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    weight_kg="New weight to store in kg",
    user="Target user (defaults to you).",
    date="Optional local date to restrict the edit to (YYYY-MM-DD).",
    bodyweight="Whether this is a bodyweight-relative lift (e.g. BW+20kg).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def change_weight_cmd(
    interaction: discord.Interaction,
    equipment: str,
    weight_kg: float,
    user: discord.Member | None = None,
    date: str | None = None,
    bodyweight: bool = False,
) -> None:
    if weight_kg < 0:
        await interaction.response.send_message(
            "`weight_kg` must be zero or higher.", ephemeral=True,
        )
        return
    if MAX_WEIGHT_KG > 0 and weight_kg > MAX_WEIGHT_KG:
        await interaction.response.send_message(
            f"That looks too high to store safely ({weight_kg:g}kg > "
            f"{MAX_WEIGHT_KG:g}kg). If it is intentional, raise `MAX_WEIGHT_KG`.",
            ephemeral=True,
        )
        return
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        await interaction.response.send_message(
            "`date` must be in YYYY-MM-DD format.", ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    target = user or interaction.user
    canon = _resolve(guild_id, equipment)
    start_iso = end_iso = None
    if date:
        start_iso, end_iso = _local_date_window(date)
    previous = db.update_latest_lift_weight(
        guild_id, target.id, canon, weight_kg, bodyweight, start_iso, end_iso,
    )
    if previous is None:
        suffix = f" on {date}" if date else ""
        await interaction.response.send_message(
            f"No `{canon}` entry found for {target.display_name}{suffix}.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Updated {target.display_name}'s `{canon}` "
        f"({_format_date(previous['logged_at'])}): "
        f"{_format_weight(previous['weight_kg'], bool(previous['bw']))} → "
        f"{_format_weight(weight_kg, bodyweight)}.",
        ephemeral=target.id == interaction.user.id,
    )


@bot.tree.command(
    name="swap_weights",
    description="Swap weights between two latest matching lift entries.",
)
@app_commands.describe(
    first_equipment="First equipment / lift name",
    second_equipment="Second equipment / lift name",
    user="Target user (defaults to you).",
    date="Optional local date to restrict the swap to (YYYY-MM-DD).",
)
@app_commands.autocomplete(
    first_equipment=_equipment_autocomplete,
    second_equipment=_equipment_autocomplete,
)
async def swap_weights_cmd(
    interaction: discord.Interaction,
    first_equipment: str,
    second_equipment: str,
    user: discord.Member | None = None,
    date: str | None = None,
) -> None:
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        await interaction.response.send_message(
            "`date` must be in YYYY-MM-DD format.", ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    target = user or interaction.user
    first = _resolve(guild_id, first_equipment)
    second = _resolve(guild_id, second_equipment)
    if not first or not second:
        await interaction.response.send_message(
            "Both equipment names must be non-empty.", ephemeral=True,
        )
        return
    if first == second:
        await interaction.response.send_message(
            "Pick two different equipment names to swap.", ephemeral=True,
        )
        return

    start_iso = end_iso = None
    if date:
        start_iso, end_iso = _local_date_window(date)
    swapped = db.swap_latest_lift_weights(
        guild_id, target.id, first, second, start_iso, end_iso,
    )
    if swapped is None:
        suffix = f" on {date}" if date else ""
        await interaction.response.send_message(
            f"Could not find both `{first}` and `{second}` entries for "
            f"{target.display_name}{suffix}.",
            ephemeral=True,
        )
        return
    first_row, second_row = swapped
    await interaction.response.send_message(
        f"Swapped {target.display_name}'s weights: "
        f"`{first}` {_format_weight(first_row['weight_kg'], bool(first_row['bw']))} "
        f"↔ `{second}` {_format_weight(second_row['weight_kg'], bool(second_row['bw']))}.",
        ephemeral=target.id == interaction.user.id,
    )


# ---------------------------------------------------------------------------
# Quality-of-life commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="help", description="Show what this bot can do.")
async def help_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title=f"🏋️ gym-bot v{__version__}",
        description=(
            "I read gym posts, parse the lifts, and track progress. Just post "
            "your gym stats — no command needed — and I'll store them.\n\n"
            "Example message I understand:\n"
            "```\nBench press: 80kg\nIncline bench 70\n"
            "Leg press: 6 plates\nDips: BW+20kg\n```"
        ),
        colour=EMBED_COLOUR,
    )
    embed.add_field(
        name="📊 Stats & progress",
        value=(
            "`/stats [user]` — personal bests\n"
            "`/summary [user]` — profile overview\n"
            "`/overview <equipment> [user]` — lift consistency\n"
            "`/checkin [user]` — copy/paste stat template\n"
            "`/stale [user] [days]` — lifts not updated lately\n"
            "`/progress <equipment> [user]` — best per month\n"
            "`/graph <equipment> [user]` — plot a PNG chart\n"
            "`/history <equipment> [user]` — your timeline\n"
            "`/recent [user]` — your last 10 entries\n"
            "`/session [user]` — full breakdown of last session\n"
            "`/streak [user]` — daily & weekly training streaks\n"
            "`/tonnage [user] [days]` — total kg moved in a window\n"
            "`/projection <equipment> [target_kg] [user]` — ETA to a goal\n"
            "`/plates <target_kg> [bar_kg]` — plate-loading helper\n"
            "`/leaderboard <equipment>` — top 25 in server\n"
            "`/machine <equipment>` — everyone's timeline\n"
            "`/compare <user> [equipment]` — head-to-head\n"
            "`/serverstats` — server-wide overview"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 Goals",
        value=(
            "`/goal_set <equipment> <target_kg> [bodyweight]`\n"
            "`/goals [user]` — progress bars\n"
            "`/goal_remove <equipment>`"
        ),
        inline=False,
    )
    embed.add_field(
        name="✏️ Logging & editing",
        value=(
            "`/log <equipment> <weight_kg> [user] [bodyweight]` — manual entry\n"
            "`/bodyweight [weight_kg] [user]` — record your bodyweight so the bot "
            "shows your true load on pull-ups, dips, etc. "
            "(`bodyweight 100kg` in chat works too, and `@user bodyweight 100kg` "
            "sets someone else's)\n"
            "`/bodyweight_history [user] [limit]` — list past weigh-ins\n"
            "`/bodyweight_graph [user]` — plot a PNG chart of weigh-ins\n"
            "`/undo` — remove your most recent entry\n"
            "React ❌ on my reply to undo that specific post "
            "(logger or target lifter only)\n"
            "`/parse <message_id>` — reparse a message\n"
            "`/delete_entry <equipment> <date>` — remove one day\n"
            "`/change_weight <equipment> <weight_kg> [user] [date]` — edit a weight\n"
            "`/swap_weights <first> <second> [user] [date]` — swap two weights\n"
            "`/rename <old> <new> [user] [scope:all]` — relabel your "
            "entries (or someone else's, or guild-wide)\n"
            "Prefix a gym post with `@user` to log it for them: "
            "`@user squat 55kg`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔎 Discovery",
        value=(
            "`/equipment_list` — what the bot knows about\n"
            "`/aliases <equipment>` — spellings I accept\n"
            "`/daily_update [days_ago]` — post a daily recap\n"
            "`/export [user]` — download lifts as CSV\n"
            "`/ping` · `/version`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠 Maintenance",
        value=(
            "`/backfill [limit]` — rescan this channel\n"
            "`/purge <equipment>` — delete all rows for a lift\n"
            "`/alias_add <phrase> <equipment>` — teach a custom name\n"
            "`/alias_remove <phrase>` · `/alias_list`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏋️ Revo Fitness",
        value=(
            "`/busy [club]` — live club occupancy\n"
            "`/revo_link <email> <password>` — link your account (reply is private)\n"
            "`/help_revo_link` — public explainer for `/revo_link`\n"
            "`/revo_unlink` — remove the link\n"
            "`/revo_streak` — your weekly check-in streak\n"
            "`/revo_streak_compare` — streak leaderboard for all linked members\n"
            "`/revo_calendar` — monthly check-in calendar\n"
            "`/revo_calendar_compare` — side-by-side calendars for all linked members"
        ),
        inline=False,
    )
    embed.set_footer(text="Weights parsed as kg. Plates assumed 20kg each.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="summary",
    description="A profile overview: totals, top PRs, most trained, biggest gains.",
)
@app_commands.describe(user="The user to look up (defaults to you).")
async def summary_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    totals = db.user_summary(guild_id, target.id)
    if not totals:
        await interaction.response.send_message(
            f"No lifts logged for {target.display_name} yet.", ephemeral=True
        )
        return

    top = db.user_top_prs(guild_id, target.id, limit=5)
    trained = db.user_most_trained(guild_id, target.id, limit=5)
    gains = db.user_biggest_gains(guild_id, target.id, limit=5)
    streak = _compute_streak_weeks(db.user_log_dates(guild_id, target.id))

    embed = discord.Embed(
        title=f"📋 {target.display_name} — gym summary",
        colour=EMBED_COLOUR,
    )
    streak_str = ""
    if streak == 1:
        streak_str = " · 🔥 **1 week** streak"
    elif streak > 1:
        streak_str = f" · 🔥 **{streak} weeks** streak"
    embed.add_field(
        name="Totals",
        value=(
            f"**{totals['total_lifts']}** lifts · "
            f"**{totals['unique_equip']}** exercises · "
            f"**{totals['sessions']}** sessions"
            f"{streak_str}\n"
            f"First: {_format_date(totals['first_at'])} · "
            f"Last: {_format_date(totals['last_at'])}"
        ),
        inline=False,
    )
    if top:
        lines = [
            f"• **{r['equipment']}** — "
            f"{_format_weight(r['best'], bool(r['bw']))}"
            for r in top
        ]
        embed.add_field(
            name="Heaviest PRs", value="\n".join(lines), inline=False
        )
    if trained:
        lines = [f"• **{r['equipment']}** — {r['n']}×" for r in trained]
        embed.add_field(
            name="Most trained", value="\n".join(lines), inline=False
        )
    if gains:
        lines = []
        for r in gains:
            sign = "+" if r["delta"] >= 0 else ""
            lines.append(
                f"• **{r['equipment']}**: {r['first_w']:g}kg "
                f"({_format_date(r['first_at'])}) → "
                f"{r['last_w']:g}kg ({_format_date(r['last_at'])}) "
                f"{sign}{r['delta']:g}kg"
            )
        embed.add_field(
            name="Biggest gains", value="\n".join(lines), inline=False
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="recent",
    description="Show a user's most recent lift entries.",
)
@app_commands.describe(
    user="The user to look up (defaults to you).",
    limit="How many entries to show (1-25, default 10).",
)
async def recent_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    limit: int = 10,
) -> None:
    target = user or interaction.user
    lim = max(1, min(25, limit))
    rows = db.user_recent(interaction.guild_id or 0, target.id, lim)
    if not rows:
        await interaction.response.send_message(
            f"No lifts logged for {target.display_name} yet.", ephemeral=True
        )
        return
    lines = [f"**{target.display_name} — last {len(rows)} entries**"]
    for r in rows:
        lines.append(
            f"• {_format_date(r['logged_at'])} — "
            f"**{r['equipment']}**: "
            f"{_format_weight(r['weight_kg'], bool(r['bw']))}"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="export_lifts",
    description="Export every logged lift for a user as a CSV attachment.",
)
@app_commands.describe(
    user="The user whose lifts to export (defaults to you).",
)
async def export_lifts_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    rows = db.user_all_lifts(guild_id, target.id)
    if not rows:
        await interaction.response.send_message(
            f"No lifts logged for {_display_name(target)} yet.",
            ephemeral=True,
        )
        return

    # Stream into an in-memory CSV — every column we keep on a lift row
    # plus an Epley 1RM estimate so the export is useful on its own without
    # the caller having to recompute it.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "logged_at", "equipment", "weight_kg", "bodyweight_add",
        "reps", "estimated_1rm_kg", "message_id", "channel_id", "raw",
    ])
    for r in rows:
        reps = r["reps"] if "reps" in r.keys() else None
        one_rm = estimated_one_rep_max(float(r["weight_kg"]), reps) if reps else None
        writer.writerow([
            r["logged_at"],
            r["equipment"],
            r["weight_kg"],
            1 if r["bw"] else 0,
            reps if reps is not None else "",
            f"{one_rm:g}" if one_rm else "",
            r["message_id"] if r["message_id"] is not None else "",
            r["channel_id"] if r["channel_id"] is not None else "",
            r["raw"] or "",
        ])
    data = buf.getvalue().encode("utf-8")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", _display_name(target)) or "user"
    filename = f"lifts-{safe_name}-{stamp}.csv"
    file = discord.File(io.BytesIO(data), filename=filename)
    suffix = _target_suffix(interaction.user, target)
    await interaction.response.send_message(
        f"Exported {_plural(len(rows), 'lift')} for "
        f"**{_display_name(target)}**{suffix}.",
        file=file,
    )


@bot.tree.command(
    name="plates",
    description="Calculate the plate breakdown for a target barbell weight.",
)
@app_commands.describe(
    target_kg="Total weight on the bar in kg.",
    bar_kg="Bar weight in kg (default 20kg Olympic bar).",
)
async def plates_cmd(
    interaction: discord.Interaction,
    target_kg: float,
    bar_kg: float = 20.0,
) -> None:
    from .training_math import plate_breakdown
    if target_kg <= 0 or bar_kg < 0:
        await interaction.response.send_message(
            "target_kg must be positive and bar_kg can't be negative.",
            ephemeral=True,
        )
        return
    pairs, leftover = plate_breakdown(target_kg, bar_kg=bar_kg)
    if not pairs and leftover < 0:
        await interaction.response.send_message(
            f"**{target_kg:g}kg** is lighter than the bar "
            f"(**{bar_kg:g}kg**). Drop the bar or raise the target.",
            ephemeral=True,
        )
        return
    if not pairs:
        await interaction.response.send_message(
            f"Just the bar (**{bar_kg:g}kg**). Add some plates!",
        )
        return
    per_side = " + ".join(f"{p:g} × {n}" for p, n in pairs)
    plates_total = sum(p * n for p, n in pairs)
    loaded = bar_kg + 2 * plates_total
    msg = (
        f"**{target_kg:g}kg** on a **{bar_kg:g}kg** bar →\n"
        f"Per side: {per_side}\n"
        f"Loaded: **{loaded:g}kg**"
    )
    if leftover > 0:
        msg += (
            f"\n_Note: {leftover:g}kg short of target — the standard "
            "kg plate stack can't hit the exact number._"
        )
    await interaction.response.send_message(msg)


@bot.tree.command(
    name="streak",
    description="Show a user's current and longest training streaks.",
)
@app_commands.describe(
    user="The user to look up (defaults to you).",
)
async def streak_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    from .training_math import daily_streak, weekly_streak
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    raw_dates = db.user_log_dates(guild_id, target.id)
    if not raw_dates:
        await interaction.response.send_message(
            f"No training history yet for {target.display_name}.",
            ephemeral=True,
        )
        return
    parsed = [datetime.fromisoformat(d).date() for d in raw_dates]
    today = datetime.now(DISPLAY_TZ).date()
    cur_d, long_d = daily_streak(parsed, today)
    cur_w, long_w = weekly_streak(parsed, today)
    fire = "🔥" if cur_d >= 3 or cur_w >= 3 else ""
    lines = [
        f"**{target.display_name} — training streaks** {fire}".rstrip(),
        f"• Daily: **{cur_d}** in a row (longest **{long_d}**)",
        f"• Weekly: **{cur_w}** in a row (longest **{long_w}**)",
        f"• Total active days logged: **{len(parsed)}**",
    ]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="tonnage",
    description="Total weight moved by a user over a recent window.",
)
@app_commands.describe(
    user="The user to look up (defaults to you).",
    days=(
        "Number of days back to include (default 7, max 365). Use 0 for "
        "all-time."
    ),
)
async def tonnage_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    days: int = 7,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    days = max(0, min(365, days))
    if days == 0:
        since_iso = None
        window_label = "all time"
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
        window_label = f"last {days} day{'s' if days != 1 else ''}"
    total_kg, n = db.total_tonnage(guild_id, target.id, since_iso)
    if n == 0:
        await interaction.response.send_message(
            f"{target.display_name} hasn't logged anything in the "
            f"{window_label}.",
            ephemeral=True,
        )
        return
    avg = total_kg / n if n else 0.0
    await interaction.response.send_message(
        f"**{target.display_name}** moved **{total_kg:g} kg** across "
        f"**{n}** {('lift' if n == 1 else 'lifts')} in the {window_label} "
        f"(avg **{avg:g} kg** per entry)."
    )


@bot.tree.command(
    name="session",
    description="Show a user's most recent training session.",
)
@app_commands.describe(
    user="The user to look up (defaults to you).",
)
async def session_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    day, rows = db.last_session_for_user(guild_id, target.id)
    if not rows or day is None:
        await interaction.response.send_message(
            f"No sessions logged for {target.display_name} yet.",
            ephemeral=True,
        )
        return
    target_bw = _user_bodyweight(guild_id, target.id)
    total_kg = sum(float(r["weight_kg"] or 0) for r in rows)
    lines = [
        f"**{target.display_name} — last session ({day})**",
        f"_{len(rows)} entries · {total_kg:g} kg total_",
        "",
    ]
    for r in rows:
        eq = r["equipment"]
        w = r["weight_kg"]
        bw = bool(r["bw"])
        true_suf = _true_weight_suffix(eq, w, bw, target_bw)
        reps = r["reps"] if "reps" in r.keys() else None
        rep_str = f" ×{reps}" if reps else ""
        lines.append(
            f"• **{eq}**: {_format_weight(w, bw)}{true_suf}{rep_str}"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="projection",
    description="Estimate when you'll hit a goal weight at your current pace.",
)
@app_commands.describe(
    equipment="Equipment / lift name.",
    target_kg=(
        "Target weight in kg. Omit to use your existing /goal_set target "
        "for this lift."
    ),
    user="The user to project for (defaults to you).",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def projection_cmd(
    interaction: discord.Interaction,
    equipment: str,
    target_kg: float | None = None,
    user: discord.Member | None = None,
) -> None:
    from .training_math import project_goal_eta
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    # Fall back to the user's set goal if no explicit target was given —
    # /projection plays nicely with the existing goals workflow.
    if target_kg is None:
        goal = db.goal_get(guild_id, target.id, canon)
        if goal is None:
            await interaction.response.send_message(
                f"No goal set for **{canon}** — pass `target_kg:` or run "
                "`/goal_set` first.",
                ephemeral=True,
            )
            return
        target_kg = float(goal["target_kg"])
    if target_kg <= 0:
        await interaction.response.send_message(
            "target_kg must be positive.", ephemeral=True
        )
        return
    rows = db.history(guild_id, target.id, canon, limit=500)
    history: list[tuple[datetime, float]] = []
    for r in rows:
        ts = datetime.fromisoformat(r["logged_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        history.append((ts, float(r["weight_kg"])))
    rate, eta, reason = project_goal_eta(
        history, target_kg=target_kg, today=datetime.now(timezone.utc),
    )
    if eta is None:
        await interaction.response.send_message(
            f"Can't project **{canon}** to **{target_kg:g}kg** — {reason}.",
            ephemeral=True,
        )
        return
    weeks = max(0.0, (eta - datetime.now(DISPLAY_TZ).date()).days / 7.0)
    await interaction.response.send_message(
        f"**{target.display_name} — {canon}** projection\n"
        f"• Current pace: **{rate:+.2f} kg/week**\n"
        f"• Target: **{target_kg:g}kg**\n"
        f"• Projected hit: **{eta.isoformat()}** "
        f"(~{weeks:.1f} weeks away)\n"
        f"_Linear estimate from first→latest entry. Real progress is "
        "rarely a straight line, but it's a useful nudge._"
    )


@bot.tree.command(
    name="checkin",
    description="Generate a copy/paste gym stats check-in template.",
)
@app_commands.describe(
    user="Whose current bests to prefill (defaults to you).",
    include_missing="Include common lifts you have not logged yet.",
)
async def checkin_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    include_missing: bool = True,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    rows = db.personal_bests(guild_id, target.id)
    bests = {r["equipment"]: r for r in rows}

    ordered: list[str] = []
    for equipment in _CHECKIN_DEFAULT_EQUIPMENT:
        if include_missing or equipment in bests:
            ordered.append(equipment)
    for equipment in sorted(bests):
        if equipment not in ordered:
            ordered.append(equipment)

    if not ordered:
        ordered = list(_CHECKIN_DEFAULT_EQUIPMENT)

    template_lines: list[str] = []
    for equipment in ordered:
        row = bests.get(equipment)
        value = _format_weight(row["best"], bool(row["bw"])) if row else ""
        template_lines.append(f"{equipment}: {value}".rstrip())

    body = "\n".join(template_lines)
    if len(body) > 1500:
        body = body[:1500].rstrip() + "\n..."
    await interaction.response.send_message(
        f"**{target.display_name} — check-in template**\n"
        "Update the numbers, delete anything irrelevant, then post it:\n"
        f"```\n{body}\n```",
        ephemeral=True,
    )


@bot.tree.command(
    name="stale",
    description="Show lifts a user has not updated recently.",
)
@app_commands.describe(
    user="The user to check (defaults to you).",
    days="How old a lift must be before it counts as stale (default 30).",
)
async def stale_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    days: int = 30,
) -> None:
    target = user or interaction.user
    threshold = max(1, min(365, days))
    rows = db.user_latest_by_equipment(interaction.guild_id or 0, target.id)
    stale_rows = []
    for row in rows:
        local_date, age_days = _format_local_day_age(row["logged_at"])
        if age_days >= threshold:
            stale_rows.append((age_days, local_date, row))
    stale_rows.sort(reverse=True, key=lambda item: (item[0], item[2]["equipment"]))

    if not rows:
        await interaction.response.send_message(
            f"No lifts logged for {target.display_name} yet.", ephemeral=True
        )
        return
    if not stale_rows:
        await interaction.response.send_message(
            f"Nothing stale for {target.display_name} at {threshold}+ days.",
            ephemeral=True,
        )
        return

    lines = [
        f"**{target.display_name} — lifts not updated in {threshold}+ days**"
    ]
    for age_days, local_date, row in stale_rows[:15]:
        lines.append(
            f"• **{row['equipment']}** — "
            f"{_format_weight(row['weight_kg'], bool(row['bw']))} "
            f"on {local_date} ({_plural(age_days, 'day')} ago)"
        )
    remaining = len(stale_rows) - 15
    if remaining > 0:
        lines.append(f"• ... and {_plural(remaining, 'more lift')}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="undo",
    description="Remove your most recently logged entry (or the last N).",
)
@app_commands.describe(
    count="How many recent entries to remove (default 1, max 10).",
)
async def undo_cmd(
    interaction: discord.Interaction, count: int = 1,
) -> None:
    n = max(1, min(10, count))
    guild_id = interaction.guild_id or 0
    rows = db.pop_last_n_for_user(
        guild_id, interaction.user.id, n,
    )
    if not rows:
        await interaction.response.send_message(
            "You don't have any entries to undo.", ephemeral=True
        )
        return
    # Suppress the source posts so a reboot's backfill doesn't re-add them.
    for r in rows:
        msg_id = r["message_id"]
        if msg_id is not None:
            db.suppress_message(guild_id, int(msg_id))
    if len(rows) == 1:
        r = rows[0]
        msg = (
            f"Removed your most recent entry — **{r['equipment']}**: "
            f"{_format_weight(r['weight_kg'], bool(r['bw']))} "
            f"_(logged {_format_date(r['logged_at'])})_."
        )
    else:
        lines = [f"Removed your last {len(rows)} entries:"]
        for r in rows:
            lines.append(
                f"• **{r['equipment']}** — "
                f"{_format_weight(r['weight_kg'], bool(r['bw']))} "
                f"_(logged {_format_date(r['logged_at'])})_"
            )
        msg = "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="compare",
    description="Compare personal bests between you and another user.",
)
@app_commands.describe(
    user="User to compare against.",
    equipment="Optional: only compare this lift.",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def compare_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    equipment: str | None = None,
) -> None:
    if user.id == interaction.user.id:
        await interaction.response.send_message(
            "Pick someone other than yourself.", ephemeral=True
        )
        return

    guild_id = interaction.guild_id or 0
    a_rows = {r["equipment"]: r for r in db.personal_bests(guild_id, interaction.user.id)}
    b_rows = {r["equipment"]: r for r in db.personal_bests(guild_id, user.id)}

    if equipment:
        canon = _resolve(guild_id, equipment)
        keys = [canon]
    else:
        keys = sorted(set(a_rows) | set(b_rows))

    if not keys or not any(k in a_rows or k in b_rows for k in keys):
        await interaction.response.send_message(
            "No data to compare.", ephemeral=True
        )
        return

    a_name = interaction.user.display_name
    b_name = user.display_name
    lines = [f"**{a_name}** vs **{b_name}**"]
    a_wins = b_wins = ties = 0
    for k in keys:
        ra = a_rows.get(k)
        rb = b_rows.get(k)
        aw = ra["best"] if ra else None
        bw = rb["best"] if rb else None
        if aw is None and bw is None:
            continue
        if aw is None:
            lines.append(
                f"• **{k}** — _{a_name}: —_ vs "
                f"{_format_weight(bw, bool(rb['bw']))}"
            )
            b_wins += 1
            continue
        if bw is None:
            lines.append(
                f"• **{k}** — {_format_weight(aw, bool(ra['bw']))} vs _{b_name}: —_"
            )
            a_wins += 1
            continue
        if aw > bw:
            marker = "🟢"
            a_wins += 1
        elif bw > aw:
            marker = "🔴"
            b_wins += 1
        else:
            marker = "⚪"
            ties += 1
        lines.append(
            f"{marker} **{k}** — "
            f"{_format_weight(aw, bool(ra['bw']))} vs "
            f"{_format_weight(bw, bool(rb['bw']))}"
        )

    if not equipment:
        lines.append(
            f"\n**Score:** {a_name} {a_wins} · {b_name} {b_wins} · tied {ties}"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="serverstats",
    description="Server-wide totals, top lifters, and most popular equipment.",
)
async def serverstats_cmd(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild_id or 0
    totals = db.server_totals(guild_id)
    if not totals:
        await interaction.response.send_message(
            "No lifts logged in this server yet.", ephemeral=True
        )
        return
    top_users = db.server_top_users(guild_id, limit=5)
    popular = db.server_popular_equipment(guild_id, limit=5)

    name = interaction.guild.name if interaction.guild else "this server"
    embed = discord.Embed(
        title=f"🏟 {name} — gym stats",
        colour=EMBED_COLOUR,
    )
    embed.add_field(
        name="Totals",
        value=(
            f"**{totals['total_lifts']}** lifts · "
            f"**{totals['lifters']}** lifters · "
            f"**{totals['unique_equip']}** exercises · "
            f"**{totals['sessions']}** sessions\n"
            f"First: {_format_date(totals['first_at'])} · "
            f"Last: {_format_date(totals['last_at'])}"
        ),
        inline=False,
    )
    if top_users:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i+1}.'} **{r['username']}** — "
            f"{r['n']} lifts ({r['equip']} exercises)"
            for i, r in enumerate(top_users)
        ]
        embed.add_field(
            name="Most active", value="\n".join(lines), inline=False
        )
    if popular:
        lines = [
            f"• **{r['equipment']}** — {r['n']} entries ({r['users']} lifters)"
            for r in popular
        ]
        embed.add_field(
            name="Most popular equipment", value="\n".join(lines), inline=False
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="export",
    description="Export lifts as a CSV file.",
)
@app_commands.describe(
    user="Only export this user's lifts (defaults to you).",
)
async def export_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    rows = db.export_rows(interaction.guild_id or 0, user_id=target.id)
    if not rows:
        await interaction.response.send_message(
            f"No lifts to export for {target.display_name}.", ephemeral=True
        )
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["logged_at", "username", "equipment",
                     "weight_kg", "bodyweight_add", "raw"])
    for r in rows:
        writer.writerow([
            r["logged_at"], r["username"], r["equipment"],
            r["weight_kg"], int(bool(r["bw"])), r["raw"] or "",
        ])
    data = buf.getvalue().encode("utf-8")
    fname = f"gym_{target.display_name}_{datetime.now().strftime('%Y%m%d')}.csv"
    file = discord.File(io.BytesIO(data), filename=fname)
    await interaction.response.send_message(
        f"Exported {len(rows)} row(s) for **{target.display_name}**.",
        file=file,
        ephemeral=True,
    )


@bot.tree.command(
    name="aliases",
    description="Show the spellings the bot accepts for an equipment name.",
)
@app_commands.describe(equipment="Equipment / lift name")
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def aliases_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    al = aliases_for(canon)
    # Also surface any server-local custom aliases that resolve to the same
    # canonical, so users can see what this server has configured.
    custom = [
        r["alias_normalized"] for r in db.alias_list(guild_id)
        if r["canonical"] == canon
    ]
    if not al and not custom:
        await interaction.response.send_message(
            f"`{canon}` isn't one of the bot's known canonical names, so "
            "there are no built-in aliases. It'll still be stored under this "
            "name if you log it, though.",
            ephemeral=True,
        )
        return
    parts = []
    if al:
        parts.append(
            "Built-in: " + ", ".join(f"`{a}`" for a in al)
        )
    if custom:
        parts.append(
            "Custom (this server): " + ", ".join(f"`{a}`" for a in custom)
        )
    await interaction.response.send_message(
        f"**{canon}** — accepted spellings:\n" + "\n".join(parts),
        ephemeral=True,
    )


@bot.tree.command(
    name="equipment_list",
    description="List every equipment name the bot knows about.",
)
async def equipment_list_cmd(interaction: discord.Interaction) -> None:
    names = sorted(all_canonicals())
    # Chunk into columns-ish lines to keep the message short.
    lines = [f"**Known equipment ({len(names)}):**"]
    lines.extend(f"• {n}" for n in names)
    msg = "\n".join(lines)
    # Discord hard-cap is 2000 chars; this list is tiny but guard anyway.
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…"
    await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="goal_set",
    description="Set a personal weight goal for a lift.",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    target_kg="Target weight in kg",
    bodyweight="True if the target is BW+X (e.g. weighted dips)",
)
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def goal_set_cmd(
    interaction: discord.Interaction,
    equipment: str,
    target_kg: float,
    bodyweight: bool = False,
) -> None:
    if target_kg <= 0:
        await interaction.response.send_message(
            "Target must be a positive number of kg.", ephemeral=True
        )
        return
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name.", ephemeral=True
        )
        return
    db.goal_set(guild_id, interaction.user.id, canon, target_kg, bodyweight)
    best = db.previous_best(guild_id, interaction.user.id, canon)
    progress_line = ""
    if best is not None:
        pct = min(100, round(best / target_kg * 100))
        progress_line = (
            f"\nCurrent best: {_format_weight(best, bodyweight)} "
            f"({pct}% of target)"
        )
    await interaction.response.send_message(
        f"🎯 Goal set for **{canon}**: "
        f"{_format_weight(target_kg, bodyweight)}.{progress_line}\n"
        "I'll celebrate when you hit it.",
        ephemeral=True,
    )


@bot.tree.command(
    name="goal_remove",
    description="Remove one of your goals.",
)
@app_commands.describe(equipment="Equipment / lift name")
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def goal_remove_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    n = db.goal_remove(guild_id, interaction.user.id, canon)
    if n:
        await interaction.response.send_message(
            f"Removed your goal for **{canon}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"No goal set for **{canon}**.", ephemeral=True
        )


@bot.tree.command(
    name="goals",
    description="Show a user's active goals and progress.",
)
@app_commands.describe(user="The user to look up (defaults to you).")
async def goals_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    rows = db.goal_list(guild_id, target.id)
    if not rows:
        await interaction.response.send_message(
            f"{target.display_name} has no goals set. "
            "Use `/goal_set` to add one.",
            ephemeral=True,
        )
        return

    lines = [f"**{target.display_name} — goals**"]
    for r in rows:
        tgt = r["target_kg"]
        bw = bool(r["bw"])
        cur = r["current_best"] or 0.0
        if tgt > 0:
            pct = min(100, int(round(cur / tgt * 100)))
        else:
            pct = 100
        # Simple 10-segment bar.
        filled = int(round(pct / 10))
        bar = "█" * filled + "░" * (10 - filled)
        remaining = max(0.0, tgt - cur)
        tail = (
            "· **hit!**" if cur >= tgt and tgt > 0
            else f"· {remaining:g}kg to go"
        )
        lines.append(
            f"• **{r['equipment']}** — "
            f"{_format_weight(cur, bw)} → {_format_weight(tgt, bw)}\n"
            f"    `{bar}` {pct}% {tail}"
        )
    await interaction.response.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# Custom aliases
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="alias_add",
    description="Teach the bot a custom name for a lift.",
)
@app_commands.describe(
    phrase="The phrase / nickname to recognise (e.g. 'hack sled')",
    equipment="Canonical equipment to map it to (e.g. 'leg press')",
)
async def alias_add_cmd(
    interaction: discord.Interaction, phrase: str, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    key = normalize_token(phrase)
    if not key:
        await interaction.response.send_message(
            "That phrase doesn't contain any usable characters.",
            ephemeral=True,
        )
        return
    # Resolve the canonical being pointed at (respecting built-in and
    # existing custom aliases so "/alias_add foo chest fly" lands on "pec dec").
    canon = _resolve(guild_id, equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name to map to.", ephemeral=True
        )
        return
    db.alias_set(guild_id, key, canon, interaction.user.id)
    await interaction.response.send_message(
        f"Added alias: `{key}` → **{canon}**.\n"
        "Custom aliases now apply to slash commands and auto-parsed messages.",
        ephemeral=True,
    )


@bot.tree.command(
    name="alias_remove",
    description="Remove a custom alias.",
)
@app_commands.describe(phrase="The phrase to un-map")
async def alias_remove_cmd(
    interaction: discord.Interaction, phrase: str
) -> None:
    key = normalize_token(phrase)
    n = db.alias_remove(interaction.guild_id or 0, key)
    if n:
        await interaction.response.send_message(
            f"Removed alias `{key}`.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"No custom alias `{key}` to remove.", ephemeral=True
        )


@bot.tree.command(
    name="alias_list",
    description="List custom aliases configured in this server.",
)
async def alias_list_cmd(interaction: discord.Interaction) -> None:
    rows = db.alias_list(interaction.guild_id or 0)
    if not rows:
        await interaction.response.send_message(
            "No custom aliases configured. Add one with `/alias_add`.",
            ephemeral=True,
        )
        return
    # Group by canonical for readability.
    by_canon: dict[str, list[str]] = {}
    for r in rows:
        by_canon.setdefault(r["canonical"], []).append(r["alias_normalized"])
    lines = [f"**Custom aliases ({len(rows)}):**"]
    for canon in sorted(by_canon):
        lines.append(
            f"• **{canon}**: " + ", ".join(f"`{a}`" for a in by_canon[canon])
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="daily_update",
    description="Post a daily gym recap for this server.",
)
@app_commands.describe(
    days_ago="Which day to recap: 1=yesterday, 0=today, max 30.",
)
async def daily_update_cmd(
    interaction: discord.Interaction,
    days_ago: int = 1,
) -> None:
    date_label, start_iso, end_iso = _daily_window(days_ago=days_ago)
    text = _daily_update_text(
        interaction.guild_id or 0,
        date_label,
        start_iso,
        end_iso,
        post_empty=True,
    )
    await interaction.response.send_message(
        text,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------------------------------------------------------------------
# Lift consistency overview
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="overview",
    description="Show consistency and progress for one user's lift.",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    user="The user to summarise (defaults to you).",
)
async def overview_cmd(
    interaction: discord.Interaction,
    equipment: str,
    user: discord.Member | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon, limit=1000)
    if not rows:
        await interaction.response.send_message(
            f"No {canon} history for {target.display_name}.", ephemeral=True
        )
        return

    stats = lift_overview(
        ((r["logged_at"], float(r["weight_kg"])) for r in rows),
        DISPLAY_TZ,
    )
    if stats is None:
        await interaction.response.send_message(
            "Couldn't build an overview — no datable entries.", ephemeral=True
        )
        return
    bodyweight = any(bool(r["bw"]) for r in rows)

    trend = stats.improvement_kg
    trend_text = "flat"
    if trend > 0:
        trend_text = f"+{trend:g}kg"
    elif trend < 0:
        trend_text = f"{trend:g}kg"

    avg_gap = (
        f"{stats.avg_gap_days:.1f} days"
        if stats.avg_gap_days is not None else "only one day logged"
    )
    longest_gap = (
        f"{stats.longest_gap_days} days"
        if stats.longest_gap_days is not None else "only one day logged"
    )
    stale = (
        "today" if stats.days_since_latest == 0
        else f"{_plural(stats.days_since_latest, 'day')} ago"
    )

    lines = [
        f"**{target.display_name} — {canon} overview**",
        (
            f"Consistency: **{stats.consistency_score}/100** · "
            f"current streak: **{_plural(stats.current_week_streak, 'week')}**"
        ),
        (
            f"Logged **{stats.total_logs}** times across "
            f"**{_plural(stats.active_days, 'day')}** and "
            f"**{stats.active_weeks}/{stats.total_weeks} active weeks**."
        ),
        (
            f"Latest: **{_format_weight(stats.latest_kg, bodyweight)}** "
            f"({stale}) · best: **{_format_weight(stats.best_kg, bodyweight)}**"
        ),
        (
            f"Change: {_format_weight(stats.first_kg, bodyweight)} "
            f"({_format_date(stats.first_day.isoformat())}) → "
            f"{_format_weight(stats.latest_kg, bodyweight)} "
            f"({_format_date(stats.latest_day.isoformat())}) · **{trend_text}**"
        ),
        (
            f"Spacing: avg gap **{avg_gap}** · longest gap **{longest_gap}** · "
            f"last 30 days: **{_plural(stats.logs_last_30_days, 'log')}**"
        ),
    ]
    await interaction.response.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# Progress graph
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="graph",
    description="Plot a lift's daily-best progress as a PNG chart.",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    user="The user to plot (defaults to you).",
)
async def graph_cmd(
    interaction: discord.Interaction,
    equipment: str,
    user: discord.Member | None = None,
) -> None:
    # Lazy import so the bot still boots if matplotlib isn't installed.
    try:
        matplotlib = importlib.import_module("matplotlib")
        matplotlib.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
        mdates = importlib.import_module("matplotlib.dates")
        ticker = importlib.import_module("matplotlib.ticker")
    except ImportError:
        await interaction.response.send_message(
            "Graphing isn't available — matplotlib isn't installed. "
            "Add it to `requirements.txt` and redeploy.",
            ephemeral=True,
        )
        return

    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon, limit=1000)
    if not rows:
        await interaction.response.send_message(
            f"No {canon} history for {target.display_name}.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    points = daily_best_points(
        ((r["logged_at"], float(r["weight_kg"])) for r in rows),
        DISPLAY_TZ,
    )
    if not points:
        await interaction.followup.send(
            "Couldn't plot — no datable entries.", ephemeral=True
        )
        return

    xs = [point.when for point in points]
    ys = [point.weight_kg for point in points]
    running_best = running_best_values(ys)

    fig, ax = plt.subplots(figsize=(8.8, 4.8), dpi=150)
    fig.patch.set_facecolor("#f6f3ee")
    ax.set_facecolor("#fffdfa")
    primary = "#f26522"
    best_colour = "#24756f"

    ax.plot(
        xs, ys,
        marker="o", markersize=6.5, markerfacecolor="#fffdfa",
        markeredgewidth=2.0, linewidth=2.4,
        color=primary, label="Daily best",
    )
    if len(xs) > 1:
        ax.step(
            xs, running_best, where="post", linewidth=1.8,
            linestyle=(0, (4, 3)), color=best_colour,
            label="Best to date",
        )

    ax.set_title(
        f"{target.display_name} — {canon}", loc="left",
        fontsize=14, fontweight="bold", pad=16,
    )
    subtitle = (
        f"{len(rows)} log{'s' if len(rows) != 1 else ''} · "
        f"{len(points)} day{'s' if len(points) != 1 else ''} · "
        f"peak {max(ys):g}kg"
    )
    ax.text(
        0, 1.015, subtitle, transform=ax.transAxes,
        fontsize=9, color="#6b625a", va="bottom",
    )
    ax.set_ylabel("kg")
    ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=6))
    ax.grid(axis="y", color="#d9d3cb", linewidth=0.8, alpha=0.85)
    ax.grid(axis="x", color="#eee9e1", linewidth=0.7, alpha=0.55)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#b8afa4")
    ax.spines["bottom"].set_color("#b8afa4")
    ax.tick_params(colors="#332f2a", labelsize=9)

    ymin = min(ys)
    ymax = max(ys)
    ypad = max(5.0, (ymax - ymin) * 0.18)
    ax.set_ylim(max(0, ymin - ypad), ymax + ypad)

    label_indexes = (
        range(len(xs))
        if len(xs) <= 8
        else sorted({ys.index(ymax), len(xs) - 1})
    )
    for idx in label_indexes:
        ax.annotate(
            f"{ys[idx]:g}kg",
            xy=(xs[idx], ys[idx]), xytext=(0, 9),
            textcoords="offset points", ha="center", va="bottom",
            fontsize=8, color="#332f2a",
        )

    if len(xs) > 1:
        span = max(xs) - min(xs)
        pad = timedelta(days=max(1.0, span.days * 0.06))
        ax.set_xlim(min(xs) - pad, max(xs) + pad)
        locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.legend(loc="upper left", frameon=False, fontsize=9)
    else:
        ax.set_xlim(xs[0] - timedelta(days=1), xs[0] + timedelta(days=1))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))

    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    fname = f"{canon.replace(' ', '_')}_{target.display_name}.png"
    file = discord.File(buf, filename=fname)
    collapsed_days = sum(1 for point in points if point.entries > 1)
    note = ""
    if collapsed_days:
        plural = "s" if collapsed_days != 1 else ""
        note = f" · daily bests shown ({collapsed_days} multi-log day{plural})"
    await interaction.followup.send(
        f"📈 **{target.display_name} — {canon}** "
        f"(peak {max(ys):g}kg{note})",
        file=file,
    )


# ---------------------------------------------------------------------------
# Bodyweight history
# ---------------------------------------------------------------------------


def _parse_iso_to_local_date(iso: str):
    """Convert a stored ISO timestamp to a DISPLAY_TZ-local ``date``."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).date()


@bot.tree.command(
    name="bodyweight_history",
    description="List your recorded bodyweight measurements.",
)
@app_commands.describe(
    user="The user to look up (defaults to you).",
    limit="How many recent measurements to show (default 20, max 100).",
)
async def bodyweight_history_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    limit: int | None = None,
) -> None:
    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    cap = max(1, min(100, int(limit) if limit else 20))
    rows = db.bodyweight_history(guild_id, target.id, limit=1000)
    if not rows:
        await interaction.response.send_message(
            f"No bodyweight entries logged for {target.display_name} yet. "
            "Set one with `/bodyweight <weight_kg>`.",
            ephemeral=True,
        )
        return

    # Newest-first slice for display, but use full list for trend numbers.
    weights = [float(r["weight_kg"]) for r in rows]
    first, latest = weights[0], weights[-1]
    delta = latest - first
    sign = "+" if delta >= 0 else ""
    peak = max(weights)
    trough = min(weights)

    recent = list(reversed(rows))[:cap]
    lines = [
        f"• `{_format_date(r['recorded_at'])}` — **{float(r['weight_kg']):g}kg**"
        for r in recent
    ]
    truncated = ""
    if len(rows) > cap:
        truncated = f"\n_…showing {cap} of {len(rows)} entries._"

    embed = discord.Embed(
        title=f"⚖️ {target.display_name} — bodyweight history",
        description="\n".join(lines) + truncated,
        colour=EMBED_COLOUR,
    )
    embed.add_field(
        name="Trend",
        value=(
            f"latest **{latest:g}kg** · first **{first:g}kg** "
            f"({sign}{delta:g}kg overall)\n"
            f"peak {peak:g}kg · low {trough:g}kg · "
            f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}"
        ),
        inline=False,
    )
    embed.set_footer(text="Tip: /bodyweight_graph plots this as a chart.")
    await interaction.response.send_message(
        embed=embed, ephemeral=target.id == interaction.user.id,
    )


@bot.tree.command(
    name="bodyweight_graph",
    description="Plot your bodyweight history as a PNG chart.",
)
@app_commands.describe(user="The user to plot (defaults to you).")
async def bodyweight_graph_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    # Lazy import — same pattern as /graph so the bot still boots without
    # matplotlib installed.
    try:
        matplotlib = importlib.import_module("matplotlib")
        matplotlib.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
        mdates = importlib.import_module("matplotlib.dates")
        ticker = importlib.import_module("matplotlib.ticker")
    except ImportError:
        await interaction.response.send_message(
            "Graphing isn't available — matplotlib isn't installed. "
            "Add it to `requirements.txt` and redeploy.",
            ephemeral=True,
        )
        return

    target = user or interaction.user
    guild_id = interaction.guild_id or 0
    rows = db.bodyweight_history(guild_id, target.id, limit=1000)
    if not rows:
        await interaction.response.send_message(
            f"No bodyweight entries logged for {target.display_name} yet. "
            "Set one with `/bodyweight <weight_kg>`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    # Collapse multiple same-day measurements to the day's mean — keeps the
    # line readable when someone weighs in twice on the same morning.
    by_day: dict = {}
    for r in rows:
        d = _parse_iso_to_local_date(r["recorded_at"])
        if d is None:
            continue
        by_day.setdefault(d, []).append(float(r["weight_kg"]))
    if not by_day:
        await interaction.followup.send(
            "Couldn't plot — no datable entries.", ephemeral=True,
        )
        return

    xs = sorted(by_day)
    ys = [sum(by_day[d]) / len(by_day[d]) for d in xs]

    fig, ax = plt.subplots(figsize=(8.8, 4.8), dpi=150)
    fig.patch.set_facecolor("#f6f3ee")
    ax.set_facecolor("#fffdfa")
    primary = "#3b7dd8"

    ax.plot(
        xs, ys,
        marker="o", markersize=6.5, markerfacecolor="#fffdfa",
        markeredgewidth=2.0, linewidth=2.4,
        color=primary, label="Bodyweight",
    )

    ax.set_title(
        f"{target.display_name} — bodyweight", loc="left",
        fontsize=14, fontweight="bold", pad=16,
    )
    delta = ys[-1] - ys[0]
    sign = "+" if delta >= 0 else ""
    subtitle = (
        f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} · "
        f"{len(xs)} day{'s' if len(xs) != 1 else ''} · "
        f"{ys[0]:g}kg → {ys[-1]:g}kg ({sign}{delta:g}kg)"
    )
    ax.text(
        0, 1.015, subtitle, transform=ax.transAxes,
        fontsize=9, color="#6b625a", va="bottom",
    )
    ax.set_ylabel("kg")
    ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=6))
    ax.grid(axis="y", color="#d9d3cb", linewidth=0.8, alpha=0.85)
    ax.grid(axis="x", color="#eee9e1", linewidth=0.7, alpha=0.55)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#b8afa4")
    ax.spines["bottom"].set_color("#b8afa4")
    ax.tick_params(colors="#332f2a", labelsize=9)

    ymin, ymax = min(ys), max(ys)
    ypad = max(1.0, (ymax - ymin) * 0.25)
    ax.set_ylim(ymin - ypad, ymax + ypad)

    # Annotate first, peak, trough, and latest only — avoids label spaghetti
    # for users with many weigh-ins.
    label_idx = sorted({0, len(xs) - 1, ys.index(ymax), ys.index(ymin)})
    for idx in label_idx:
        ax.annotate(
            f"{ys[idx]:g}kg",
            xy=(xs[idx], ys[idx]), xytext=(0, 9),
            textcoords="offset points", ha="center", va="bottom",
            fontsize=8, color="#332f2a",
        )

    if len(xs) > 1:
        span = xs[-1] - xs[0]
        pad = timedelta(days=max(1.0, span.days * 0.06))
        ax.set_xlim(xs[0] - pad, xs[-1] + pad)
        locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    else:
        ax.set_xlim(xs[0] - timedelta(days=1), xs[0] + timedelta(days=1))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))

    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    fname = f"bodyweight_{target.display_name}.png"
    file = discord.File(buf, filename=fname)
    await interaction.followup.send(
        f"⚖️ **{target.display_name} — bodyweight** "
        f"({ys[0]:g}kg → {ys[-1]:g}kg, {sign}{delta:g}kg)",
        file=file,
    )


# ---- autocomplete for equipment names ------------------------------------


async def _equipment_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest equipment names, prioritising ones the invoking user has
    logged recently so their own lifts are a single key-tap away."""
    guild_id = interaction.guild_id or 0
    cur = current.lower().strip()

    # User's own recent equipment first (empty list for new users).
    own = db.recent_user_equipment(guild_id, interaction.user.id, limit=25)
    all_names = db.known_equipment(guild_id)
    # Merge while preserving order and uniqueness.
    ordered: list[str] = []
    seen: set[str] = set()
    for n in (*own, *all_names):
        if n in seen:
            continue
        seen.add(n)
        ordered.append(n)

    if cur:
        ordered = [n for n in ordered if cur in n.lower()]

    # If the user hasn't typed anything and we have nothing stored yet,
    # fall back to the bot's built-in canonical list so autocomplete is
    # still useful on a fresh server.
    if not ordered:
        ordered = sorted(all_canonicals())
        if cur:
            ordered = [n for n in ordered if cur in n.lower()]

    return [app_commands.Choice(name=n, value=n) for n in ordered[:25]]


progress_cmd.autocomplete("equipment")(_equipment_autocomplete)
leaderboard_cmd.autocomplete("equipment")(_equipment_autocomplete)
log_cmd.autocomplete("equipment")(_equipment_autocomplete)
history_cmd.autocomplete("equipment")(_equipment_autocomplete)
machine_cmd.autocomplete("equipment")(_equipment_autocomplete)
purge_cmd.autocomplete("equipment")(_equipment_autocomplete)
rename_cmd.autocomplete("old")(_equipment_autocomplete)
rename_cmd.autocomplete("new")(_equipment_autocomplete)
delete_entry_cmd.autocomplete("equipment")(_equipment_autocomplete)
change_weight_cmd.autocomplete("equipment")(_equipment_autocomplete)
swap_weights_cmd.autocomplete("first_equipment")(_equipment_autocomplete)
swap_weights_cmd.autocomplete("second_equipment")(_equipment_autocomplete)
compare_cmd.autocomplete("equipment")(_equipment_autocomplete)
aliases_cmd.autocomplete("equipment")(_equipment_autocomplete)
goal_set_cmd.autocomplete("equipment")(_equipment_autocomplete)
goal_remove_cmd.autocomplete("equipment")(_equipment_autocomplete)
overview_cmd.autocomplete("equipment")(_equipment_autocomplete)
graph_cmd.autocomplete("equipment")(_equipment_autocomplete)
alias_add_cmd.autocomplete("equipment")(_equipment_autocomplete)


# ---------------------------------------------------------------------------
# Revo Fitness portal integration. See docs/REVO_PORTAL.md.
# ---------------------------------------------------------------------------

# Master kill-switch — set REVO_DISABLED=1 to skip every Revo command/poller
# (useful if the portal blocks our scraper or we hit rate limits).
REVO_DISABLED = os.getenv("REVO_DISABLED", "0").lower() in (
    "1", "true", "yes", "y", "on",
)

# How often to poll linked accounts for new attendance entries. The portal's
# ticket-tally only updates after a check-in; 15 minutes is conservative
# enough to stay well under any plausible rate limit while still feeling
# "live" to a Discord channel.
try:
    REVO_POLL_MINUTES = max(5, int(os.getenv("REVO_POLL_MINUTES", "15")))
except ValueError:
    REVO_POLL_MINUTES = 15

# Optional global default channel for attendance notifications. Each linked
# account can also pin its own notify_channel_id; this is just the fallback
# when the user doesn't specify one at link time.
_revo_ch = os.getenv("REVO_NOTIFY_CHANNEL_ID", "").strip()
REVO_DEFAULT_NOTIFY_CHANNEL_ID: int | None = (
    int(_revo_ch) if _revo_ch.isdigit() else None
)

# Per-user RevoClient cache so the poller doesn't construct + re-login a
# fresh session on every cycle.
_revo_user_clients: dict[int, "revo_client.RevoClient"] = {}
_revo_clients_lock = __import__("threading").Lock()


def _ticket_signature(rows: list["revo_client.TicketRow"]) -> str | None:
    """Stable signature of the most-recent ticket-tally entry."""
    if not rows:
        return None
    head = rows[0]
    return f"{head.date}|{head.delta}|{head.source}"


def _client_for_user(row) -> "revo_client.RevoClient":
    """Return a cached RevoClient for a linked-account row."""
    user_id = int(row["user_id"])
    with _revo_clients_lock:
        client = _revo_user_clients.get(user_id)
        if client is None:
            password = revo_client.decrypt_password(row["password_enc"])
            client = revo_client.RevoClient(row["email"], password)
            _revo_user_clients[user_id] = client
    return client


def _drop_cached_client(user_id: int) -> None:
    with _revo_clients_lock:
        _revo_user_clients.pop(user_id, None)


def _format_busy_line(info: "revo_client.ClubInfo") -> str:
    state = revo_client.state_for_club(info.name)
    location = f"{info.name}, {state}" if state else info.name
    return f"**{location}** — {info.in_club} in club right now (id={info.club_id})"


@bot.tree.command(
    name="busy",
    description="Show how busy a Revo Fitness club is right now.",
)
@app_commands.describe(
    club="Club name (substring match). Omit for the top 5 busiest right now.",
    state='Filter the top-5 by state (SA/WA/VIC/NSW). Defaults to all clubs.',
)
@app_commands.choices(state=[
    app_commands.Choice(name="All clubs", value="all"),
    app_commands.Choice(name="SA (South Australia)", value="SA"),
    app_commands.Choice(name="WA (Western Australia)", value="WA"),
    app_commands.Choice(name="VIC (Victoria)", value="VIC"),
    app_commands.Choice(name="NSW (New South Wales)", value="NSW"),
])
async def busy_cmd(
    interaction: discord.Interaction,
    club: str | None = None,
    state: str = "all",
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled (REVO_DISABLED=1).", ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            "Revo client unavailable — install the `requests` package.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    def _do() -> tuple[dict[str, "revo_client.ClubInfo"], int | None] | str:
        # 1) Try the shared env-var account first — keeps /busy working
        #    even for users who haven't linked yet.
        try:
            return revo_client.shared_club_counter()
        except revo_client.RevoUnavailable:
            pass  # fall through to per-user credentials
        except revo_client.RevoAuthError as exc:
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo shared club-counter fetch failed")
            return f"error: {exc}"

        # 2) Fall back to the invoking user's linked credentials.
        row = db.get_revo_account(interaction.user.id)
        if row is None:
            return "no-credentials"
        try:
            client = _client_for_user(row)
            return revo_client.club_counter_with_client(client)
        except revo_client.RevoUnavailable as exc:
            return f"unavailable: {exc}"
        except revo_client.RevoAuthError as exc:
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo per-user club-counter fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        if result == "no-credentials":
            await interaction.followup.send(
                "🔒 Revo's live counter needs a logged-in session, but no "
                "shared account is configured and you haven't linked yours.\n"
                "Run `/help_revo_link` for a walkthrough, then `/revo_link "
                "email:<you> password:<…>` to enable `/busy` for everyone.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Couldn't fetch the live counter ({result}).", ephemeral=True,
        )
        return

    clubs, _favorite = result
    if not clubs:
        await interaction.followup.send(
            "Revo returned no club data — try again shortly.", ephemeral=True,
        )
        return

    if club:
        match = revo_client.find_club(clubs, club)
        if match is None:
            sample = ", ".join(sorted(clubs)[:8])
            await interaction.followup.send(
                f"No Revo club matched `{club}`. Try one of: {sample}…",
                ephemeral=True,
            )
            return
        await interaction.followup.send(_format_busy_line(match))
        return

    visible = clubs
    if state and state.upper() != "ALL":
        filtered = revo_client.filter_clubs_by_state(clubs, state)
        if filtered:
            visible = filtered
        # if filter matches nothing fall back to all clubs silently
    top = sorted(visible.values(), key=lambda c: c.in_club, reverse=True)[:5]
    lines = ["**Busiest Revo clubs right now**"]
    lines.extend(f"• {_format_busy_line(c)}" for c in top)
    await interaction.followup.send("\n".join(lines))


@bot.tree.command(
    name="help_revo_link",
    description="Public explainer for /revo_link — what it does and how it stores credentials.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def help_revo_link_cmd(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🔗 Linking your Revo Fitness account",
        description=(
            "Run `/revo_link` to connect your Revo portal account to the bot. "
            "Your reply is **always private** (ephemeral), so no one in the "
            "channel sees your email, password, or confirmation."
        ),
        colour=EMBED_COLOUR,
    )
    embed.add_field(
        name="How to link",
        value=(
            "`/revo_link email:<you@example.com> password:<your-revo-password> "
            "[notify_channel_id:<channel id>]`\n"
            "You can run it in a server channel **or** in a DM with me — "
            "either way, only you see the response."
        ),
        inline=False,
    )
    embed.add_field(
        name="What you unlock",
        value=(
            "• `/revo_streak` — your current weekly check-in streak\n"
            "• `/revo_streak_compare` — streak leaderboard for all linked members\n"
            "• `/revo_calendar` — your monthly check-in grid\n"
            "• `/revo_calendar_compare` — side-by-side calendar for everyone in the server\n"
            "• Automatic attendance pings when you tap into your gym "
            "(posted in the configured notify channel)\n"
            "• Your favourite club is auto-detected from your last visits"
        ),
        inline=False,
    )
    embed.add_field(
        name="How your credentials are stored",
        value=(
            "• Your password is encrypted with **Fernet (AES-128-CBC + HMAC)** "
            "before being written to the database — the plaintext never "
            "touches disk.\n"
            "• Only the bot host (with the `REVO_FERNET_KEY` env var) can "
            "decrypt it; rotating that key invalidates every stored password.\n"
            "• Use `/revo_unlink` any time to wipe your encrypted credentials."
        ),
        inline=False,
    )
    embed.add_field(
        name="Safety tips",
        value=(
            "• Use a Revo password you don't reuse anywhere else.\n"
            "• Rotate it on the Revo portal after linking if you're paranoid.\n"
            "• `/busy` works without linking — only personal stats need it."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="revo_link",
    description="Link your Revo Fitness account. Password is encrypted at rest. Reply is private.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(
    email="The email you log into the Revo portal with.",
    password="Your Revo password. Stored encrypted; rotate it after linking.",
    notify_channel_id="Channel ID to post your attendance pings into (optional).",
)
async def revo_link_cmd(
    interaction: discord.Interaction,
    email: str,
    password: str,
    notify_channel_id: str | None = None,
) -> None:
    # Works in DMs or in a guild channel. All replies below are ephemeral
    # so the password / confirmation never appears to other members. Note
    # that the *invocation* of a slash command is already private to the
    # caller in Discord — only the bot ever sees the arguments.
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled (REVO_DISABLED=1).", ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            "Revo client unavailable — install `requests` and `cryptography`.",
            ephemeral=True,
        )
        return

    notify_id: int | None = None
    if notify_channel_id:
        nid = notify_channel_id.strip()
        if not nid.isdigit():
            await interaction.response.send_message(
                "`notify_channel_id` must be a numeric Discord channel id.",
                ephemeral=True,
            )
            return
        notify_id = int(nid)
    if notify_id is None:
        notify_id = REVO_DEFAULT_NOTIFY_CHANNEL_ID

    await interaction.response.defer(thinking=True, ephemeral=True)

    def _do() -> str | tuple[int | None, int | None, int | None]:
        try:
            password_enc = revo_client.encrypt_password(password)
        except revo_client.RevoUnavailable as exc:
            return f"unavailable: {exc}"
        try:
            client = revo_client.RevoClient(email, password)
            client.login()
        except revo_client.RevoAuthError as exc:
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo link login failed")
            return f"error: {exc}"
        favorite = None
        try:
            _clubs, favorite = client.get_club_counter()
        except Exception:  # pragma: no cover - non-fatal
            LOG.warning("Revo link: failed to capture favorite club", exc_info=True)
        try:
            db.link_revo_account(
                user_id=interaction.user.id,
                email=email,
                password_enc=password_enc,
                member_id=client.member_id,
                membership_level=client.membership_level,
                favorite_club_id=favorite,
                notify_guild_id=None,
                notify_channel_id=notify_id,
            )
        except Exception as exc:
            LOG.exception("Revo link db write failed")
            return f"db-error: {exc}"
        # Cache the freshly-authenticated client.
        with _revo_clients_lock:
            _revo_user_clients[interaction.user.id] = client
        return (client.member_id, client.membership_level, favorite)

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        await interaction.followup.send(
            f"Couldn't link your Revo account ({result}).", ephemeral=True,
        )
        return
    member_id, level, favorite = result
    notify_line = (
        f"\n• Attendance notifications → <#{notify_id}>"
        if notify_id else "\n• No notification channel set — pings disabled."
    )
    await interaction.followup.send(
        "✅ Revo account linked!\n"
        f"• Member id: `{member_id}`\n"
        f"• Membership level: `{level}`\n"
        f"• Favorite club id: `{favorite}`"
        f"{notify_line}\n\n"
        "🔐 Your password is stored encrypted. Even so, **consider rotating it** "
        "if you reuse it anywhere else.",
        ephemeral=True,
    )


@bot.tree.command(
    name="revo_unlink",
    description="Remove your linked Revo Fitness account from the bot.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_unlink_cmd(interaction: discord.Interaction) -> None:
    removed = db.unlink_revo_account(interaction.user.id)
    _drop_cached_client(interaction.user.id)
    if removed:
        await interaction.response.send_message(
            "🗑️ Revo account unlinked. Encrypted credentials removed.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "You don't have a linked Revo account.", ephemeral=True,
        )


@bot.tree.command(
    name="revo_streak",
    description="Show a Revo Fitness weekly check-in streak (yours, or another member's).",
)
@app_commands.describe(member="The server member to look up. Defaults to yourself.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_streak_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled.", ephemeral=True,
        )
        return

    target = member or interaction.user
    row = db.get_revo_account(target.id)
    if row is None:
        if target == interaction.user:
            await interaction.response.send_message(
                "Link your account first with `/revo_link`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} hasn't linked a Revo account yet.",
                ephemeral=True,
            )
        return
    await interaction.response.defer(thinking=True)

    def _do() -> str | int | None:
        try:
            client = _client_for_user(row)
            return client.get_streak_weeks()
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(int(row["user_id"]))
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo streak fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        msg = (
            f"Couldn't fetch your streak ({result})."
            if target == interaction.user
            else f"Couldn't fetch {target.mention}'s streak ({result})."
        )
        await interaction.followup.send(msg, ephemeral=True)
        return
    if result is None:
        await interaction.followup.send(
            "Revo didn't show a streak count — visit the portal to check.",
            ephemeral=True,
        )
        return
    display = _bot_name(target.id, target.display_name)
    await interaction.followup.send(
        f"🔥 **{display}** — current Revo weekly streak: "
        f"**{result} week{'s' if result != 1 else ''}**.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="revo_streak_compare",
    description="Compare Revo weekly streaks for everyone in this server who has linked their account.",
)
async def revo_streak_compare_cmd(interaction: discord.Interaction) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled.", ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            "Revo client unavailable — install the `requests` package.",
            ephemeral=True,
        )
        return

    # Only makes sense in a guild — we need member context to filter accounts.
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    guild_id = interaction.guild.id
    accounts = db.list_revo_accounts()
    # Filter to accounts whose Discord user is actually a member of this guild.
    # We intentionally do NOT filter by notify_guild_id because a user may have
    # linked without setting up attendance notifications, or may have set them
    # up in a different server.
    # get_member() is cache-only; with members intent disabled the cache is
    # sparse, so fall back to fetch_member() (a live API call) on a cache miss.
    guild_member_ids: set[int] = set()
    for r in accounts:
        uid = int(r["user_id"])
        member = interaction.guild.get_member(uid)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            guild_member_ids.add(uid)
    guild_accounts = [r for r in accounts if int(r["user_id"]) in guild_member_ids]

    if not guild_accounts:
        await interaction.followup.send(
            "No one in this server has linked a Revo account yet. "
            "Use `/revo_link` to get started.",
            ephemeral=True,
        )
        return

    def _fetch_streaks() -> list[tuple[int, int | None]]:
        """Return list of (user_id, streak_weeks) fetched live."""
        out: list[tuple[int, int | None]] = []
        for row in guild_accounts:
            uid = int(row["user_id"])
            try:
                client = _client_for_user(row)
                streak = client.get_streak_weeks()
            except Exception:
                # Fall back to the cached value stored by the poller.
                streak = row["last_streak_weeks"]
            out.append((uid, streak))
        return out

    results = await bot.loop.run_in_executor(None, _fetch_streaks)

    # Sort: highest streak first; None / 0 go to the bottom.
    results.sort(key=lambda t: t[1] if t[1] is not None else -1, reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = ["**🔥 Revo Streak Leaderboard**"]
    for i, (uid, streak) in enumerate(results):
        badge = medals[i] if i < len(medals) else f"**#{i + 1}**"
        streak_txt = (
            f"**{streak} week{'s' if streak != 1 else ''}**"
            if streak is not None
            else "*unknown*"
        )
        name = _bot_name(uid, f"<@{uid}>")
        lines.append(f"{badge} **{name}** — {streak_txt}")

    await interaction.followup.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@bot.tree.command(
    name="revo_calendar",
    description="Show a Revo per-day check-in calendar for a given month.",
)
@app_commands.describe(
    month="Month number 1-12. Defaults to the current month.",
    year="Year (e.g. 2026). Defaults to the current year.",
    member="The server member to look up. Defaults to yourself.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_calendar_cmd(
    interaction: discord.Interaction,
    month: app_commands.Range[int, 1, 12] | None = None,
    year: app_commands.Range[int, 2020, 2100] | None = None,
    member: discord.Member | None = None,
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled.", ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            "Revo client unavailable — install the `requests` package.",
            ephemeral=True,
        )
        return

    today = datetime.now()
    m = month or today.month
    y = year or today.year

    target = member or interaction.user
    row = db.get_revo_account(target.id)
    if row is None:
        if target == interaction.user:
            await interaction.response.send_message(
                "Link your account first with `/revo_link`.", ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} hasn't linked a Revo account yet.",
                ephemeral=True,
            )
        return

    await interaction.response.defer(thinking=True)

    def _do() -> "dict[int, bool] | str":
        try:
            client = _client_for_user(row)
            return client.get_streak_calendar(m, y)
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(int(row["user_id"]))
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo calendar fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        msg = (
            f"Couldn't fetch your calendar ({result})."
            if target == interaction.user
            else f"Couldn't fetch {target.mention}'s calendar ({result})."
        )
        await interaction.followup.send(msg, ephemeral=True)
        return

    grid = _render_revo_calendar(m, y, result)
    month_name = datetime(y, m, 1).strftime("%B %Y")
    attended_count = sum(1 for v in result.values() if v)
    display = _bot_name(target.id, target.display_name)
    header = (
        f"🔥 **{display}** — Revo check-ins for **{month_name}** "
        f"({attended_count} day{'s' if attended_count != 1 else ''})"
    )
    legend = "🔥 attended · ⬜ missed · ⬛ out of month"
    # Weekday header — monospace via code-free bold isn't reliable, so we
    # sandwich the grid in a small code block just for the header row.
    body = f"{header}\n`Mo Tu We Th Fr Sa Su`\n{grid}\n-# {legend}"
    await interaction.followup.send(
        body,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="revo_calendar_compare",
    description="Compare Revo check-in calendars for all linked members in this server.",
)
@app_commands.describe(
    month="Month number 1-12. Defaults to the current month.",
    year="Year (e.g. 2026). Defaults to the current year.",
)
async def revo_calendar_compare_cmd(
    interaction: discord.Interaction,
    month: app_commands.Range[int, 1, 12] | None = None,
    year: app_commands.Range[int, 2020, 2100] | None = None,
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            "Revo integration is disabled.", ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            "Revo client unavailable — install the `requests` package.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True,
        )
        return

    today = datetime.now()
    m = month or today.month
    y = year or today.year

    await interaction.response.defer(thinking=True)

    # Resolve which linked accounts belong to this guild.
    accounts = db.list_revo_accounts()
    guild_member_ids: set[int] = set()
    for r in accounts:
        uid = int(r["user_id"])
        member = interaction.guild.get_member(uid)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            guild_member_ids.add(uid)
    guild_accounts = [r for r in accounts if int(r["user_id"]) in guild_member_ids]

    if not guild_accounts:
        await interaction.followup.send(
            "No one in this server has linked a Revo account yet. "
            "Use `/revo_link` to get started.",
            ephemeral=True,
        )
        return

    def _fetch_all() -> list[tuple[int, "dict[int, bool] | None"]]:
        out: list[tuple[int, "dict[int, bool] | None"]] = []
        for row in guild_accounts:
            uid = int(row["user_id"])
            try:
                client = _client_for_user(row)
                out.append((uid, client.get_streak_calendar(m, y)))
            except Exception:
                LOG.warning(
                    "revo_calendar_compare: fetch failed for user %s", uid, exc_info=True,
                )
                out.append((uid, None))
        return out

    results = await bot.loop.run_in_executor(None, _fetch_all)

    def _count(cal: "dict[int, bool] | None") -> int:
        return sum(1 for v in cal.values() if v) if cal else -1

    results.sort(key=lambda t: _count(t[1]), reverse=True)

    month_name = datetime(y, m, 1).strftime("%B %Y")
    n = len(results)
    lines = [
        f"**🔥 Revo Check-ins — {month_name}** ({n} member{'s' if n != 1 else ''})",
        "`Mo Tu We Th Fr Sa Su`",
    ]
    for uid, cal in results:
        count = _count(cal)
        name = _bot_name(uid, f"<@{uid}>")
        count_str = (
            f"{count} day{'s' if count != 1 else ''}" if cal is not None else "unavailable"
        )
        lines.append("")
        lines.append(f"**{name}** — {count_str}")
        if cal is not None:
            lines.append(_render_revo_calendar(m, y, cal))
        else:
            lines.append("*Could not fetch calendar.*")

    legend = "-# 🔥 attended · ⬜ missed · ⬛ out of month"
    body = "\n".join(lines)
    if len(body) + len(legend) + 1 > 1990:
        body = body[:1900] + "\n*…truncated (too many members to fit)*"
    else:
        body += "\n" + legend

    await interaction.followup.send(
        body,
        allowed_mentions=discord.AllowedMentions(users=True),
    )


def _render_revo_calendar(month: int, year: int, attended: "dict[int, bool]") -> str:
    """Render a Mon-Sun emoji grid.

    🔥 = attended day, ⬜ = missed day, ⬛ = out-of-month padding.
    Each row is one week; no code-block needed.
    """
    first_weekday, days_in_month = monthrange(year, month)  # Monday=0
    # Header row uses thin-space separated day initials
    header = "Mo Tu We Th Fr Sa Su"
    FIRE = "🔥"
    BLANK = "⬜"
    PAD = "⬛"
    cells: list[str] = [PAD] * first_weekday
    for day in range(1, days_in_month + 1):
        cells.append(FIRE if attended.get(day) else BLANK)
    while len(cells) % 7 != 0:
        cells.append(PAD)
    rows = [" ".join(cells[r : r + 7]) for r in range(0, len(cells), 7)]
    return "\n".join(rows)


def _bot_name(user_id: int, discord_fallback: str) -> str:
    """Return a display string for ``user_id``.

    Format: ``Discord Name (Nickname)`` when a bot-wide nickname has been
    assigned, otherwise just the Discord display name.
    """
    nick = db.get_user_nickname(user_id)
    if nick is None:
        return discord_fallback
    return f"{discord_fallback} ({nick})"


@bot.tree.command(
    name="set_nick",
    description="Assign a bot-wide friendly nickname to a user (shown in all bot responses).",
)
@app_commands.describe(
    member="The user to nickname.",
    nickname="Friendly name to display (e.g. Cookie Monster, Sean). Clear it with /remove_nick.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def set_nick_cmd(
    interaction: discord.Interaction,
    member: discord.Member | discord.User,
    nickname: app_commands.Range[str, 1, 32],
) -> None:
    db.set_user_nickname(member.id, nickname, interaction.user.id)
    await interaction.response.send_message(
        f"✅ **{member.display_name}** will now appear as **{nickname.strip()}** in bot responses.",
        ephemeral=True,
    )


@bot.tree.command(
    name="remove_nick",
    description="Remove a bot-wide nickname from a user.",
)
@app_commands.describe(member="The user whose nickname to remove.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def remove_nick_cmd(
    interaction: discord.Interaction,
    member: discord.Member | discord.User,
) -> None:
    removed = db.remove_user_nickname(member.id)
    if removed:
        await interaction.response.send_message(
            f"🗑️ Nickname for **{member.display_name}** removed.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{member.display_name}** doesn't have a custom nickname set.",
            ephemeral=True,
        )


@bot.tree.command(
    name="nicks",
    description="List all bot-wide nicknames.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def nicks_cmd(interaction: discord.Interaction) -> None:
    rows = db.list_user_nicknames()
    if not rows:
        await interaction.response.send_message(
            "No nicknames set yet. Use `/set_nick` to add one.", ephemeral=True,
        )
        return
    lines = ["**Bot-wide nicknames**"]
    for r in rows:
        lines.append(f"• <@{r['user_id']}> → **{r['nickname']}**")
    await interaction.response.send_message(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
        ephemeral=True,
    )


# ---- attendance poller ----------------------------------------------------

@tasks.loop(minutes=REVO_POLL_MINUTES)
async def revo_attendance_poll() -> None:
    """Walk every linked Revo account and announce new check-ins.

    Strategy: hit ticket-tally, take the newest history row, compare to
    the row we last saw (``last_ticket_signature``). On change, post to the
    user's configured notify channel and update the cursor. We deliberately
    treat the *first* poll after linking as silent (no signature → record
    baseline) so users don't get spammed with backfilled history.
    """
    if REVO_DISABLED or not revo_client.available():
        return
    accounts = db.list_revo_accounts()
    if not accounts:
        return
    for row in accounts:
        try:
            await _poll_one_account(row)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Revo poll failed for user %s", row["user_id"])


async def _poll_one_account(row) -> None:
    user_id = int(row["user_id"])
    notify_channel_id = row["notify_channel_id"]
    last_sig = row["last_ticket_signature"]

    def _fetch() -> tuple[str | None, int | None, "revo_client.TicketRow" | None] | str:
        try:
            client = _client_for_user(row)
            _avail, rows = client.get_tickets()
            streak = None
            try:
                streak = client.get_streak_weeks()
            except Exception:  # pragma: no cover
                LOG.warning("Revo streak fetch failed for user %s", user_id, exc_info=True)
            sig = _ticket_signature(rows)
            head = rows[0] if rows else None
            return sig, streak, head
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(user_id)
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _fetch)
    if isinstance(result, str):
        LOG.warning("Revo poll skipped user %s: %s", user_id, result)
        return
    sig, streak, head = result

    # Persist the cursor first so a notify-failure doesn't replay forever.
    db.update_revo_polling_state(user_id, sig, streak)

    if sig is None or sig == last_sig:
        return
    if last_sig is None:
        # First poll after link — establish baseline silently.
        LOG.info("Revo baseline established for user %s (sig=%s)", user_id, sig)
        return
    if notify_channel_id is None or head is None:
        return

    channel = bot.get_channel(int(notify_channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(notify_channel_id))
        except discord.HTTPException:
            LOG.warning(
                "Revo poll: cannot reach notify channel %s for user %s",
                notify_channel_id, user_id,
            )
            return

    streak_tail = (
        f" — streak: **{streak} week{'s' if streak != 1 else ''}** 🔥"
        if streak else ""
    )
    text = (
        f"🏋️ <@{user_id}> just checked in at Revo "
        f"({head.source}, {head.date}){streak_tail}"
    )

    # Append a streak leaderboard if there are other linked accounts in the
    # same notify guild (using the cached streak values — no extra HTTP calls).
    notify_guild_id = row["notify_guild_id"]
    if notify_guild_id is not None:
        all_accounts = db.list_revo_accounts()
        peers = [
            (int(r["user_id"]), r["last_streak_weeks"])
            for r in all_accounts
            if r["notify_guild_id"] == notify_guild_id
            and r["last_streak_weeks"] is not None
        ]
        if len(peers) > 1:
            peers.sort(key=lambda t: t[1], reverse=True)
            medals = ["🥇", "🥈", "🥉"]
            lb_lines = ["**Streak leaderboard**"]
            for i, (uid, sw) in enumerate(peers):
                badge = medals[i] if i < len(medals) else f"#{i + 1}"
                lb_lines.append(
                    f"{badge} <@{uid}> — "
                    f"**{sw} week{'s' if sw != 1 else ''}**"
                )
            text += "\n" + "\n".join(lb_lines)

    try:
        await channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        LOG.exception("Revo poll: failed to post attendance ping for user %s", user_id)


@revo_attendance_poll.before_loop
async def _before_revo_poll() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Presence tracking (/track ...)
# ---------------------------------------------------------------------------
# Owner-only commands let admins start/stop logging a user's online/offline
# transitions. Anyone can run /track schedule to view aggregated activity
# from the recorded events.

_PRESENCE_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _is_owner(user_id: int) -> bool:
    """True if ``user_id`` is in the configured ADMIN_USER_IDS allow-list."""
    return user_id in ADMIN_USER_IDS


def _discord_status_to_str(status: discord.Status) -> str:
    """Map discord.Status enum -> the short string we persist.

    Idle and DnD are collapsed into "online"; invisible is collapsed into
    "offline". This means a transition from online→idle (or vice versa)
    won't create a new log entry and won't count as a separate period.
    """
    raw = str(status.value)
    if raw in ("idle", "dnd"):
        return "online"
    if raw == "invisible":
        return "offline"
    return raw  # "online" or "offline"


@bot.event
async def on_presence_update(
    before: discord.Member, after: discord.Member,
) -> None:  # pragma: no cover - discord runtime path
    """Record status transitions for tracked users only."""
    if not ENABLE_PRESENCE_TRACKING:
        return
    try:
        if after.guild is None:
            return
        if before.status == after.status:
            return
        if not db.presence_is_tracked(after.guild.id, after.id):
            return
        status = _discord_status_to_str(after.status)
        db.presence_log_event(after.guild.id, after.id, status)
    except Exception:
        LOG.exception("Failed to record presence update")


track_group = app_commands.Group(
    name="track",
    description="Track and view a user's online/offline schedule.",
)


_PRESENCE_DISABLED_MSG = (
    "Presence tracking is disabled. Set `ENABLE_PRESENCE_TRACKING=true` and "
    "enable the **Presence Intent** + **Server Members Intent** toggles in "
    "the Discord Developer Portal, then restart the bot."
)


@track_group.command(
    name="start", description="(Owner) Begin recording a user's online/offline status.",
)
@app_commands.describe(user="The member whose presence to start tracking.")
async def track_start_cmd(
    interaction: discord.Interaction, user: discord.Member,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            _PRESENCE_DISABLED_MSG, ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can start presence tracking.", ephemeral=True,
        )
        return
    if user.bot:
        await interaction.response.send_message(
            "I won't track other bots.", ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    inserted = db.presence_track_add(guild_id, user.id, interaction.user.id)
    # Seed the log with the user's current status so /track schedule has
    # something useful to report immediately rather than waiting for the
    # next transition.
    try:
        db.presence_log_event(
            guild_id, user.id, _discord_status_to_str(user.status),
        )
    except Exception:
        LOG.exception("Failed to seed initial presence event")
    msg = (
        f"Now tracking {user.mention}'s presence." if inserted
        else f"{user.mention} was already being tracked."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@track_group.command(
    name="stop", description="(Owner) Stop recording a user's presence.",
)
@app_commands.describe(
    user="The member to stop tracking.",
    purge="Also delete the recorded event history (default: keep history).",
)
async def track_stop_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    purge: bool = False,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            _PRESENCE_DISABLED_MSG, ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can stop presence tracking.", ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    removed = db.presence_track_remove(guild_id, user.id, purge=purge)
    if not removed and not purge:
        await interaction.response.send_message(
            f"{user.mention} wasn't being tracked.", ephemeral=True,
        )
        return
    suffix = " (history purged)" if purge else ""
    await interaction.response.send_message(
        f"Stopped tracking {user.mention}.{suffix}", ephemeral=True,
    )


@track_group.command(
    name="list", description="(Owner) Show who is currently being tracked.",
)
async def track_list_cmd(interaction: discord.Interaction) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            _PRESENCE_DISABLED_MSG, ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can view the tracking list.", ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    rows = db.presence_track_list(guild_id)
    if not rows:
        await interaction.response.send_message(
            "Nobody is being tracked in this server.", ephemeral=True,
        )
        return
    lines = ["**Tracked users:**"]
    for r in rows:
        lines.append(
            f"• <@{int(r['user_id'])}> — since "
            f"<t:{int(datetime.fromisoformat(r['started_at']).timestamp())}:R>"
        )
    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _render_schedule_embed(
    user: discord.abc.User,
    summary: PresenceSummary,
    days: int,
) -> discord.Embed:
    """Build the /track schedule embed from a PresenceSummary."""
    embed = discord.Embed(
        title=f"📅 Presence schedule — {user.display_name}",
        colour=EMBED_COLOUR,
        description=f"Last **{days}** day{'s' if days != 1 else ''} of recorded activity.",
    )
    online = format_duration(summary.online_seconds)
    offline = format_duration(summary.offline_seconds)
    embed.add_field(name="🟢 Online", value=online, inline=True)
    embed.add_field(name="⚫ Offline", value=offline, inline=True)
    if summary.final_status:
        dot = "🟢" if _presence_is_online(summary.final_status) else "⚫"
        embed.add_field(
            name="Now", value=f"{dot} {summary.final_status}", inline=True,
        )
    if summary.last_online_at is not None:
        ts = int(summary.last_online_at.timestamp())
        embed.add_field(
            name="Last seen online", value=f"<t:{ts}:R>", inline=False,
        )

    # Per-weekday bar (online time only).
    if summary.by_weekday:
        max_wd = max(summary.by_weekday.values()) or 1.0
        wd_lines = []
        for i in range(7):
            secs = summary.by_weekday.get(i, 0.0)
            bar = "▇" * max(1, int(round(secs / max_wd * 10))) if secs else "·"
            wd_lines.append(
                f"`{_PRESENCE_WEEKDAY_NAMES[i]}` {bar}  {format_duration(secs)}"
            )
        embed.add_field(
            name="By weekday", value="\n".join(wd_lines), inline=False,
        )

    # Per-hour bar (online time only). 24 rows is too long; collapse into
    # a single 24-character sparkline so it fits in one field.
    if summary.by_hour:
        max_hr = max(summary.by_hour.values()) or 1.0
        glyphs = " ▁▂▃▄▅▆▇█"
        spark = "".join(
            glyphs[min(len(glyphs) - 1, int(round(
                (summary.by_hour.get(h, 0.0) / max_hr) * (len(glyphs) - 1)
            )))]
            for h in range(24)
        )
        embed.add_field(
            name=f"By hour ({DISPLAY_TZ})",
            value=f"`{spark}`\n`0   6   12  18  23`",
            inline=False,
        )
    # Sleep estimate (needs >= 3 days of data via by_hour buckets)
    sleep = estimate_sleep_window(summary.by_hour, days)
    if sleep is not None:
        s_start, s_end = sleep
        embed.add_field(
            name=f"💤 Est. sleep ({DISPLAY_TZ})",
            value=f"`{s_start:02d}:00` – `{s_end:02d}:59`",
            inline=False,
        )
    embed.set_footer(text=f"{summary.transitions} status changes recorded")
    return embed


@track_group.command(
    name="schedule",
    description="Show a user's online/offline schedule from recorded data.",
)
@app_commands.describe(
    user="The member whose schedule to show.",
    days="How many days back to summarise (1-90, default 7).",
)
async def track_schedule_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    days: int = 7,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            _PRESENCE_DISABLED_MSG, ephemeral=True,
        )
        return
    if days < 1 or days > 90:
        await interaction.response.send_message(
            "`days` must be between 1 and 90.", ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    rows = db.presence_events_for(
        guild_id, user.id, since=window_start, until=now,
    )
    if not rows:
        if not db.presence_is_tracked(guild_id, user.id):
            await interaction.response.send_message(
                f"No presence data for {user.mention} yet — an owner needs "
                "to run `/track start` for them first.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is being tracked, but no status changes "
                "have been seen in that window yet.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return
    events = [(r["status"], r["at"]) for r in rows]
    summary = summarize_presence(
        events, window_start, now, display_tz=DISPLAY_TZ,
    )
    embed = _render_schedule_embed(user, summary, days)
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


# Maximum events to show in a single /track raw reply.
_RAW_EVENT_LIMIT = 40


@track_group.command(
    name="raw",
    description="Show raw online/offline timestamps for a tracked user.",
)
@app_commands.describe(
    user="The member whose raw presence log to show.",
    days="How many days back to show (1-90, default 7).",
)
async def track_raw_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    days: int = 7,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            _PRESENCE_DISABLED_MSG, ephemeral=True,
        )
        return
    if days < 1 or days > 90:
        await interaction.response.send_message(
            "`days` must be between 1 and 90.", ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    rows = db.presence_events_for(
        guild_id, user.id, since=window_start, until=now,
    )
    # Filter to only events strictly inside the window (not the carry-in).
    inner = [r for r in rows if r["at"] >= window_start.isoformat()]
    if not inner:
        if not db.presence_is_tracked(guild_id, user.id):
            await interaction.response.send_message(
                f"No presence data for {user.mention} — an owner needs to "
                "run `/track start` first.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is being tracked but no status changes "
                "were seen in that window yet.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return

    # Take the most-recent _RAW_EVENT_LIMIT events, oldest first.
    shown = inner[-_RAW_EVENT_LIMIT:]
    truncated = len(inner) > _RAW_EVENT_LIMIT

    lines: list[str] = []
    for idx, row in enumerate(shown):
        status = row["status"]
        ts_str: str = row["at"]
        ts_dt = datetime.fromisoformat(ts_str)
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        unix = int(ts_dt.timestamp())
        dot = "🟢" if _presence_is_online(status) else "⚫"

        # Duration since previous event.
        if idx == 0:
            dur_str = ""
        else:
            prev_ts = datetime.fromisoformat(shown[idx - 1]["at"])
            if prev_ts.tzinfo is None:
                prev_ts = prev_ts.replace(tzinfo=timezone.utc)
            diff = ts_dt - prev_ts
            dur_str = f"  ·  after {format_duration(diff.total_seconds())}"

        lines.append(f"{dot} **{status}**  <t:{unix}:f>{dur_str}")

    header = (
        f"**{user.display_name}** — last **{days}** day{'s' if days != 1 else ''}"
        f" ({len(inner)} event{'s' if len(inner) != 1 else ''})"
    )
    if truncated:
        header += f"\n*Showing most recent {_RAW_EVENT_LIMIT} events only.*"

    embed = discord.Embed(
        title=f"📋 Raw presence log — {user.display_name}",
        description=header + "\n\n" + "\n".join(lines),
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(track_group)


def main() -> None:
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
