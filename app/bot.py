"""Gym tracking Discord bot.

Auto-detects gym posts in configured channels, parses lifts, and stores them
in SQLite. Exposes slash commands for querying stats, progress, and
leaderboards.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import importlib
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import signal
import sqlite3
import tempfile
import threading
from calendar import monthrange
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .aliases import (
    _ALIAS_GROUPS as _BUILTIN_ALIAS_GROUPS,  # noqa: PLC2701  (internal use)
    aliases_for,
    all_canonicals,
    canonicalize,
    normalize_token,
)

from .bodycomp import daily_mean_points as bodycomp_daily_points
from .bodycomp import metric_summary as bodycomp_metric_summary
from .db import KEEP, Database, _normalize_iso as db_normalize_iso
from .graphing import (
    BodyweightTrend,
    bodyweight_trend,
    daily_best_points,
    running_best_values,
    trend_values,
)
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
    nightly_sleep_sessions,
    sleep_stats,
    summarize_activity_sets,
    summarize_presence,
)
from .voicetime import summarize_voice
from . import __version__
from . import ai_food
from . import calories
from . import cardio
from . import config as config_mod
from . import dayspans
from . import food_lookup
from . import game_icons
from . import gemini_client
from . import ha_client
from . import hevy_client
from . import nutrition
from . import protein as protein_mod
from . import revo_client
from . import revo_netpulse
from . import revo_perfectgym
from . import scaling
from . import secretbox
from . import targets as targets_mod
from . import strava_client
from . import strava_web
from . import tdee as tdee_lib
from . import ui
from . import webui
from . import workerlink

# Set of all alias phrases (canonical + aliases) the built-in table already
# recognises, normalised. Used by /log to decide whether a logged equipment
# label needs to be auto-registered as a custom alias so future free-form
# messages like "sit stand squat 60kg" get picked up.
_BUILTIN_KNOWN_PHRASES: set[str] = {
    normalize_token(p)
    for canon, aliases in _BUILTIN_ALIAS_GROUPS.items()
    for p in (canon, *aliases)
}

load_dotenv()

LOG = logging.getLogger("gymbot")


# Moved to app/config.py so the supervisor can format its logs identically
# without importing the bot. Re-exported under the old private name.
_JsonFormatter = config_mod.JsonFormatter


# ---------------------------------------------------------------------------
# Configuration.
#
# Every name bound by _bind_config() below is read LIVE from module scope inside
# the function bodies further down this file, so rebinding one here changes
# behaviour on the next call with no restart. The exceptions are the gateway
# intents (folded in once, just below) and the @tasks.loop schedules (fixed at
# decoration time) — those settings are marked apply="worker" in app/config.py
# and get a process restart from the supervisor instead.
#
# Nothing in this file calls os.getenv any more. Resolution order is always
# environment > database > code default; see app/config.py for why.
# ---------------------------------------------------------------------------

# Opened from the environment alone: this is the path used to OPEN the settings
# store, so it is the one value that genuinely cannot live inside it. Under the
# supervisor the schema is already migrated, so we must not race it.
DB_PATH = config_mod.bootstrap_db_path()
_UNDER_SUPERVISOR = os.getenv(workerlink.ROLE_ENV) == "worker"

db = Database(
    DB_PATH,
    migrate=not _UNDER_SUPERVISOR,
    busy_timeout_ms=5000 if _UNDER_SUPERVISOR else 0,
)

_box = secretbox.SecretBox.open_at(DB_PATH)
CFG = config_mod.load(db, decrypt=_box.decryptor())
config_mod.install_logging(CFG)


def _bind_config(cfg: config_mod.Config) -> None:
    """(Re)bind every config-derived module global from ``cfg``.

    Called once at import, and again by the supervisor's ``reload_config`` RPC
    when only apply="hot" settings changed.

    Assignments must always REPLACE the object, never mutate one in place.
    ``_run_startup_backfill`` iterates GYM_CHANNEL_IDS across awaits, and
    mutating that set mid-iteration would raise RuntimeError, skip the tail of
    the function and leave ``db.audit_live`` False for the rest of the process.
    """
    g = globals()

    # -- Discord ----------------------------------------------------------
    g["GYM_CHANNEL_IDS"] = set(cfg["GYM_CHANNEL_IDS"])
    g["DEV_GUILD"] = (
        discord.Object(id=cfg["GUILD_ID"]) if cfg["GUILD_ID"] else None
    )
    g["COMMAND_SCOPE"] = cfg["COMMAND_SCOPE"]
    g["ADMIN_USER_IDS"] = set(cfg["ADMIN_USER_IDS"])

    # -- Core parsing -----------------------------------------------------
    g["MIN_LIFTS_FOR_AUTO"] = cfg["MIN_LIFTS_FOR_AUTO"]
    g["PARSE_REPLY_MAX_ITEMS"] = cfg["PARSE_REPLY_MAX_ITEMS"]
    g["BACKFILL_ON_START"] = cfg["BACKFILL_ON_START"]
    g["BACKFILL_LIMIT"] = cfg["BACKFILL_LIMIT"]
    g["SHOW_LB"] = cfg["SHOW_LB"]
    g["MAX_WEIGHT_KG"] = cfg["MAX_WEIGHT_KG"]

    # app.targets owns the timezone, but it is imported long before the
    # database is open, so it can only see the environment on its own. Push the
    # resolved value in first — otherwise a timezone set in the dashboard would
    # be silently ignored — then re-export the names this file uses.
    targets_mod.configure(cfg["DISPLAY_TIMEZONE"])
    g["DISPLAY_TZ"] = targets_mod.DISPLAY_TZ
    g["_tz_name"] = targets_mod.TZ_NAME

    # -- Weekly reminder --------------------------------------------------
    g["REMINDER_CHANNEL_ID"] = cfg["REMINDER_CHANNEL_ID"]
    g["REMINDER_WEEKDAY"] = cfg["REMINDER_WEEKDAY"]
    g["REMINDER_HOUR"] = cfg["REMINDER_HOUR"]
    g["REMINDER_MINUTE"] = cfg["REMINDER_MINUTE"]
    g["REMINDER_ROLE_ID"] = cfg["REMINDER_ROLE_ID"]

    # -- Bodyweight reminder ----------------------------------------------
    g["BODYWEIGHT_REMINDER_CHANNEL_ID"] = cfg["BODYWEIGHT_REMINDER_CHANNEL_ID"]
    g["BODYWEIGHT_REMINDER_WEEKDAY"] = cfg["BODYWEIGHT_REMINDER_WEEKDAY"]
    g["BODYWEIGHT_REMINDER_HOUR"] = cfg["BODYWEIGHT_REMINDER_HOUR"]
    g["BODYWEIGHT_REMINDER_MINUTE"] = cfg["BODYWEIGHT_REMINDER_MINUTE"]
    g["BODYWEIGHT_REMINDER_ROLE_ID"] = cfg["BODYWEIGHT_REMINDER_ROLE_ID"]

    # -- Daily update -----------------------------------------------------
    g["DAILY_UPDATE_CHANNEL_ID"] = cfg["DAILY_UPDATE_CHANNEL_ID"]
    g["DAILY_UPDATE_HOUR"] = cfg["DAILY_UPDATE_HOUR"]
    g["DAILY_UPDATE_MINUTE"] = cfg["DAILY_UPDATE_MINUTE"]
    g["DAILY_UPDATE_POST_EMPTY"] = cfg["DAILY_UPDATE_POST_EMPTY"]

    # -- Weekly report ----------------------------------------------------
    g["WEEKLY_REPORT_CHANNEL_ID"] = cfg["WEEKLY_REPORT_CHANNEL_ID"]
    g["WEEKLY_REPORT_WEEKDAY"] = cfg["WEEKLY_REPORT_WEEKDAY"]
    g["WEEKLY_REPORT_HOUR"] = cfg["WEEKLY_REPORT_HOUR"]
    g["WEEKLY_REPORT_MINUTE"] = cfg["WEEKLY_REPORT_MINUTE"]

    # -- Backups (the loop itself runs in the supervisor) -----------------
    g["BACKUP_DIR"] = cfg["BACKUP_DIR"]
    g["BACKUP_KEEP"] = cfg["BACKUP_KEEP"]
    g["BACKUP_HOUR"] = cfg["BACKUP_HOUR"]
    g["BACKUP_MINUTE"] = cfg["BACKUP_MINUTE"]

    # -- Feature toggles --------------------------------------------------
    g["ENABLE_PRESENCE_TRACKING"] = cfg["ENABLE_PRESENCE_TRACKING"]
    g["ENABLE_MESSAGE_LOGGING"] = cfg["ENABLE_MESSAGE_LOGGING"]
    g["ENABLE_VOICE_TRACKING"] = cfg["ENABLE_VOICE_TRACKING"]
    g["ENABLE_MEDIA_DOWNLOAD"] = cfg["ENABLE_MEDIA_DOWNLOAD"]
    g["AUTO_UNTIMEOUT"] = cfg["AUTO_UNTIMEOUT"]
    # Renamed from the old dashboard-enabled flag: the dashboard now always
    # exists, so "is there a dashboard" would be permanently true — and this
    # flag is what requests the PRIVILEGED Server Members intent, which Discord
    # refuses for anyone who has not enabled it in the Developer Portal. See
    # settings_service.seed_from_env_once for how existing deployments carry
    # their current value across the upgrade.
    g["ENABLE_MEMBER_MIRROR"] = cfg["ENABLE_MEMBER_MIRROR"]

    # -- Message logging & media ------------------------------------------
    g["MESSAGE_LOG_BACKFILL_DAYS"] = cfg["MESSAGE_LOG_BACKFILL_DAYS"]
    g["MEDIA_MAX_MB"] = cfg["MEDIA_MAX_MB"]
    g["MEDIA_DIR"] = cfg["MEDIA_DIR"]

    # -- Game icons -------------------------------------------------------
    g["GAME_ICONS_CACHE"] = cfg["GAME_ICONS_CACHE"]
    g["GAME_ICONS_REFRESH_DAYS"] = cfg["GAME_ICONS_REFRESH_DAYS"]

    # -- Dashboard (bind host/port belong to the supervisor) --------------
    g["WEBUI_PASSWORD"] = cfg["WEBUI_PASSWORD"]
    g["WEBUI_DISABLED"] = cfg["WEBUI_DISABLED"]
    g["WEBUI_BIND_HOST"] = cfg["WEBUI_BIND_HOST"]
    g["WEBUI_PORT"] = cfg["WEBUI_PORT"]

    # -- Revo -------------------------------------------------------------
    g["REVO_DISABLED"] = cfg["REVO_DISABLED"]
    g["REVO_POLL_MINUTES"] = cfg["REVO_POLL_MINUTES"]
    g["REVO_DEFAULT_NOTIFY_CHANNEL_ID"] = cfg["REVO_NOTIFY_CHANNEL_ID"]

    # -- Strava -----------------------------------------------------------
    g["STRAVA_DISABLED"] = cfg["STRAVA_DISABLED"]
    g["STRAVA_FEED_CHANNEL_ID"] = cfg["STRAVA_FEED_CHANNEL_ID"]
    g["STRAVA_BIND_HOST"] = cfg["STRAVA_BIND_HOST"]
    g["STRAVA_PORT"] = cfg["STRAVA_PORT"]
    g["STRAVA_MAPBOX_TOKEN"] = cfg["STRAVA_MAPBOX_TOKEN"]
    g["STRAVA_MAP_STYLE"] = cfg["STRAVA_MAP_STYLE"]
    g["STRAVA_SPORT_ALLOW"] = set(cfg["STRAVA_SPORT_TYPES"])
    g["STRAVA_MIN_DISTANCE_M"] = cfg["STRAVA_MIN_DISTANCE_M"]
    g["STRAVA_MIN_DURATION_S"] = cfg["STRAVA_MIN_DURATION_S"]
    g["STRAVA_IMPERIAL"] = cfg["STRAVA_IMPERIAL"]
    g["STRAVA_AUTO_SUBSCRIBE"] = cfg["STRAVA_AUTO_SUBSCRIBE"]

    # -- Hevy -------------------------------------------------------------
    g["HEVY_DISABLED"] = cfg["HEVY_DISABLED"]
    g["HEVY_FEED_CHANNEL_ID"] = cfg["HEVY_FEED_CHANNEL_ID"]
    g["HEVY_POLL_MINUTES"] = cfg["HEVY_POLL_MINUTES"]

    # -- Home Assistant ---------------------------------------------------
    # No URL or token here: each member's own credential lives encrypted in
    # ha_server, set with /setup_ha. HA_FERNET_KEY is absent for the usual
    # reason — app/ha_client.py reads it with os.getenv, so it is a sibling_env
    # key the supervisor exports into this process's environment.
    g["HA_DISABLED"] = cfg["HA_DISABLED"]
    g["HA_POLL_MINUTES"] = cfg["HA_POLL_MINUTES"]
    g["HA_BACKFILL_DAYS"] = cfg["HA_BACKFILL_DAYS"]
    g["HA_IGNORE_ENTITIES"] = set(cfg["HA_IGNORE_ENTITIES"])
    g["HA_VERIFY_SSL"] = cfg["HA_VERIFY_SSL"]


_bind_config(CFG)

TOKEN = CFG["DISCORD_TOKEN"]
if not TOKEN:
    # NOT a message-carrying SystemExit. The supervisor only spawns us when a
    # token exists, so reaching here means it was cleared between spawn and
    # import. Exit 78 (EX_CONFIG) tells the supervisor "waiting for
    # configuration", which is a quiet idle state rather than a crash loop.
    raise SystemExit(78)

# Bot "accent" colour for embeds.
EMBED_COLOUR = discord.Colour.from_str("#f26522")

# Load the icon cache (or bundled seed) now — cheap and network-free — so icons
# work the moment the dashboard serves a request; the live refresh runs in the
# supervisor.
game_icons.configure(GAME_ICONS_CACHE)

intents = discord.Intents.default()
intents.message_content = True
intents.members = False
if ENABLE_PRESENCE_TRACKING:
    intents.presences = True
    intents.members = True
if ENABLE_MEMBER_MIRROR:
    # Required to enumerate members/roles and receive on_member_* events.
    intents.members = True
if AUTO_UNTIMEOUT:
    # on_member_update only fires (so we can catch a new timeout) with the
    # members intent. Enable the non-privileged moderation events too so the
    # bot also receives them where available.
    intents.members = True
    intents.moderation = True
bot = commands.Bot(command_prefix="!gym ", intents=intents)

# Make every slash command usable in DMs and as a user-installed app, not just
# in guild channels. These tree-level defaults are inherited by all commands
# that don't carry their own @app_commands.allowed_contexts / allowed_installs
# decorator (explicit per-command decorators still win). DM commands also
# require the "User Install" integration to be enabled for the app in the
# Discord Developer Portal — that's a portal toggle, not something code can set.
bot.tree.allowed_contexts = app_commands.AppCommandContext(
    guild=True, dm_channel=True, private_channel=True,
)
bot.tree.allowed_installs = app_commands.AppInstallationType(
    guild=True, user=True,
)


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


# Resolution order mirrors _resolve_date_hint so we strip exactly the token it
# acted on (and nothing else — important so a weekday/"today" word that's part
# of a food name elsewhere in the message survives).
_DATE_HINT_PATTERNS = (
    _DATE_HINT_YESTERDAY,
    _DATE_HINT_TODAY,
    _DATE_HINT_DAYS_AGO,
    _DATE_HINT_ISO,
    _DATE_HINT_WEEKDAY,
)


def _split_date_hint(
    text: str, now_local: datetime,
) -> tuple[datetime | None, str]:
    """Resolve a backdating hint and strip just that phrase from the text.

    Returns ``(utc_dt_or_None, text_without_that_hint)``. Lets the strict
    "amount only" nutrition parsers still match a backdated post like
    ``500c yesterday`` -> ``500c``. Only the single matched hint is removed, so
    ``sunday roast yesterday`` resolves to yesterday and leaves ``sunday roast``
    intact. No hint -> the text is returned unchanged.
    """
    if not text:
        return None, text
    for pat in _DATE_HINT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        dt = _resolve_date_hint(m.group(0), now_local)
        if dt is None:
            continue
        cleaned = text[:m.start()] + " " + text[m.end():]
        return dt, " ".join(cleaned.split())
    return None, text


def _day_window_for(dt_utc: datetime | None) -> tuple[str, str]:
    """UTC ISO ``(start, end)`` bounds of the local day containing ``dt_utc``.

    Defaults to today's window when ``dt_utc`` is None, so a backdated entry's
    running total is shown against the right day rather than today's."""
    if dt_utc is None:
        return _today_window()
    local_day = dt_utc.astimezone(DISPLAY_TZ).date()
    start_local = datetime.combine(local_day, dtime.min, tzinfo=DISPLAY_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _band_targets(
    user_id: int,
) -> tuple[targets_mod.Resolved, targets_mod.Resolved]:
    """``(weekday, weekend)`` targets as the rules currently stand.

    Resolved against the next day of each kind rather than today, so the preview
    reflects rules that only start applying later (an edit made today, or a
    scheduled change) instead of the ones today happens to sit under.
    """
    rows = db.nutrition_target_rows(user_id)
    today = targets_mod.local_today()
    days = [today + timedelta(days=n) for n in range(7)]
    weekday = next(d for d in days if not targets_mod.is_weekend(d))
    weekend = next(d for d in days if targets_mod.is_weekend(d))
    return targets_mod.resolve(rows, weekday), targets_mod.resolve(rows, weekend)


def _band_breakdown_lines(
    day_totals: dict[str, float],
    day_targets: dict[date, targets_mod.Resolved],
    fmt, macro: str, noun: str = "target",
) -> list[str]:
    """Weekday-vs-weekend averages for a week grid.

    Empty unless the user actually runs a split — for one all-week target the
    two rows would just restate the overall average twice.
    """
    if not any(r.macro(macro).split for r in day_targets.values()):
        return []
    intake = {
        day: day_totals[day.isoformat()]
        for day in day_targets if day.isoformat() in day_totals
    }
    stats = targets_mod.band_stats(intake, day_targets, macro)
    lines = []
    for band in ("weekday", "weekend"):
        s = stats.get(band)
        if s is None:
            continue
        vs = f" vs {fmt(s.avg_target)}" if s.avg_target is not None else ""
        adherence = (
            f" · {s.adherence:.0%} of {noun}" if s.adherence is not None else ""
        )
        lines.append(
            f"`{band.title():8}` {s.days}d · avg {fmt(s.avg_intake)}"
            f"{vs}{adherence}"
        )
    return lines


def _targets_on(
    user_id: int, logged_at: datetime | None = None,
) -> targets_mod.Resolved:
    """The user's calorie/protein targets for the local day ``logged_at`` falls
    on — today when it's None.

    Always resolve against the day an entry belongs to, never today: logging
    ``200c yesterday`` on a Monday has to score that entry against Sunday's
    weekend target, not Monday's.
    """
    return db.nutrition_targets_on(user_id, targets_mod.local_day_of(logged_at))


def _backdate_label(logged_at: datetime | None) -> str:
    """A short ' · logged for …' suffix when ``logged_at`` is a past local day,
    so a backdated entry's reply makes the date obvious. Empty for today."""
    if logged_at is None:
        return ""
    local_day = logged_at.astimezone(DISPLAY_TZ).date()
    today = datetime.now(DISPLAY_TZ).date()
    if local_day == today:
        return ""
    if local_day == today - timedelta(days=1):
        return " · logged for yesterday"
    return f" · logged for {local_day.strftime('%a %d %b')}"


def _slash_logged_at(day: str | None) -> tuple[datetime | None, bool]:
    """Resolve a slash-command ``day`` argument to a UTC datetime.

    Returns ``(logged_at, ok)``. No day given -> ``(None, True)`` (today).
    A recognised hint ("yesterday", "monday", "3 days ago", "2026-06-28") ->
    ``(dt, True)``. Anything unparseable -> ``(None, False)`` so the caller can
    reject it instead of silently logging for today.
    """
    if day is None or not day.strip():
        return None, True
    dt = _resolve_date_hint(day, datetime.now(DISPLAY_TZ))
    return dt, dt is not None


_BAD_DAY_MSG = (
    "Couldn't read that day — try `yesterday`, `monday`, `3 days ago`, or a "
    "date like `2026-06-28`."
)


# These five moved to app/dayspans.py so the supervisor can compute them from
# the database without the bot running — the dashboard calls today_window and
# both streak helpers synchronously, so they can never become async RPC hops.
# The wrappers below keep every existing call site (and the tests that import
# these names) working unchanged, and read DISPLAY_TZ live so a timezone change
# applies on the next call.
_STREAK_WINDOW_DAYS = dayspans.STREAK_WINDOW_DAYS


def _logging_streak(days: set[date], today: date) -> int:
    """Consecutive local days with an entry, ending today *or* yesterday."""
    return dayspans.logging_streak(days, today)


def _entry_local_days(entries: "list[sqlite3.Row]") -> set[date]:
    """Set of DISPLAY_TZ calendar dates an entry list touches."""
    return dayspans.entry_local_days(entries, DISPLAY_TZ)


def _streak_window_iso() -> tuple[date, str, str]:
    """``(today_local, start_iso, end_iso)`` covering the streak look-back."""
    return dayspans.streak_window(DISPLAY_TZ)


def _calorie_streak(user_id: int) -> int:
    """Current consecutive-day calorie-logging streak for ``user_id`` (global)."""
    return dayspans.calorie_streak(db, user_id, DISPLAY_TZ)


def _protein_streak(user_id: int) -> int:
    """Current consecutive-day protein-logging streak for ``user_id`` (global)."""
    return dayspans.protein_streak(db, user_id, DISPLAY_TZ)


def _streak_suffix(streak: int) -> str:
    """' · 🔥 N day streak' for a 2+ day streak, else empty (no day-one noise)."""
    return f" · 🔥 {streak} day streak" if streak >= 2 else ""


# Day counts that earn a public shout-out — mirrors the Revo weekly-streak
# milestones, which until now were the ONLY streak celebrations in the bot.
_STREAK_DAY_MILESTONES = (7, 14, 21, 30, 50, 75, 100, 150, 200, 250, 300, 365)


def _streak_milestone_banner(
    macro: str, streak: int, *, first_today: bool,
) -> str:
    """A channel-visible celebration line the day a logging streak reaches a
    milestone. Because the streak advances only on the first entry of a day,
    gating on ``first_today`` fires it exactly once per crossing (and never on
    a backdated fill-in). Empty string when nothing to celebrate."""
    if not first_today or streak not in _STREAK_DAY_MILESTONES:
        return ""
    return (
        f"\n🎉 **Milestone!** {streak}-day {macro} logging streak — "
        "keep it lit. 🔥"
    )


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


def _lift_weight_ok(lift: Lift) -> bool:
    """A lift's stored weight is sane. Bodyweight-only lifts legitimately carry
    weight 0 (``Dips: BW``), so the >0 floor applies only to non-BW lifts —
    this is what rejects junk like ``Farmer Walk - 0 Kgs`` without dropping a
    real bodyweight exercise. The upper cap is MAX_WEIGHT_KG (0 = disabled)."""
    if lift.weight_kg < 0:
        return False
    if lift.weight_kg == 0 and not lift.bodyweight_add:
        return False
    if MAX_WEIGHT_KG > 0 and lift.weight_kg > MAX_WEIGHT_KG:
        return False
    return True


def _split_reasonable_lifts(lifts: list[Lift]) -> tuple[list[Lift], list[Lift]]:
    accepted: list[Lift] = []
    rejected: list[Lift] = []
    for lift in lifts:
        (accepted if _lift_weight_ok(lift) else rejected).append(lift)
    return accepted, rejected


def _rejected_lifts_note(rejected: list[Lift]) -> str:
    if not rejected:
        return ""
    # Two rejection reasons read very differently, so label them separately.
    too_heavy = [
        lift for lift in rejected
        if MAX_WEIGHT_KG > 0 and lift.weight_kg > MAX_WEIGHT_KG
    ]
    nonpositive = [lift for lift in rejected if lift.weight_kg <= 0]
    lines: list[str] = [""]
    if too_heavy:
        lines.append(
            f"⚠️ Skipped {_plural(len(too_heavy), 'lift')} over "
            f"{MAX_WEIGHT_KG:g}kg. If that was real, use `/log` after "
            "raising `MAX_WEIGHT_KG`."
        )
    if nonpositive:
        lines.append(
            f"⚠️ Skipped {_plural(len(nonpositive), 'lift')} with no real "
            "weight (0kg). Post the weight (e.g. `farmer walk 40kg`), or add "
            "`BW` for a bodyweight-only exercise."
        )
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


def _plain_label(text: str, *, limit: int = 60) -> str:
    """:func:`_safe_label` for embed slots that render as plain text.

    Embed titles, field *names* and footers don't render Markdown, so escaping it
    there is worse than not escaping it: ``sensor.joshua_s_weight`` comes out as
    ``sensor.joshua\\_s\\_weight``, backslashes and all. Mention syntax is still
    neutralised — embeds don't ping, but a zero-width space costs nothing and
    keeps the guarantee independent of where the string is later reused.
    """
    cleaned = discord.utils.escape_mentions(text or "")
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or "(unknown)"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


# Discord's hard cap on a single embed field's value. Overshooting it doesn't
# truncate — the whole send fails with a 400 — so anything built from a
# variable-length list goes through _clip_field().
EMBED_FIELD_LIMIT = 1024


def _clip_field(text: str, *, limit: int = EMBED_FIELD_LIMIT) -> str:
    """Trim an embed field value to *limit*, cutting on a line break when it can.

    Dropping whole lines beats slicing mid-line, which would leave a half-written
    club name or an unclosed `**` behind and corrupt the rest of the field.
    """
    if len(text) <= limit:
        return text
    marker = "\n…"
    keep = limit - len(marker)
    cut = text[:keep]
    nl = cut.rfind("\n")
    if nl > keep // 2:  # only snap back to a line break if we keep most of it
        cut = cut[:nl]
    return cut.rstrip() + marker


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


def _parse_iso(iso: str | None) -> datetime | None:
    """A stored ISO timestamp as a tz-aware datetime, or None if unparseable.

    Older rows were written without an offset, so a naive value is read as UTC
    — the same assumption every other reader here makes.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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


# ---------------------------------------------------------------------------
# DM command context resolution.
#
# Slash commands are now usable in DMs (see the tree defaults above), but a DM
# interaction has no ``guild_id`` — yet almost every command's data is keyed by
# guild. We resolve an "effective" guild for DM interactions in a single place
# (``_tree_interaction_check``) and stash it on ``interaction.extras`` so the
# command bodies can read it via ``_ctx_guild_id`` / ``_ctx_guild`` exactly as
# if it had come from the interaction.
# ---------------------------------------------------------------------------

# Commands that are useful in a DM even when we can't resolve a server (they
# don't read guild-scoped data, or they set the default themselves). These are
# allowed through ``_tree_interaction_check`` without a resolved guild.
_CONTEXT_FREE: frozenset[str] = frozenset({
    "ping", "help", "version", "server",
    "revo_link", "revo_unlink", "help_revo_link",
    "strava_link", "strava_unlink",
})


def _shared_guild_ids(user_id: int) -> list[int]:
    """Guild IDs the bot shares with ``user_id``.

    Prefers the live member cache (no API calls); falls back to the SQLite
    member mirror so it still resolves when the members intent is off. The
    result is restricted to guilds the bot is currently in.
    """
    ids: set[int] = set()
    bot_guild_ids = {g.id for g in bot.guilds}
    for g in bot.guilds:
        if g.get_member(user_id) is not None:
            ids.add(g.id)
    for gid in db.member_guild_ids(user_id):
        if not bot_guild_ids or gid in bot_guild_ids:
            ids.add(gid)
    return sorted(ids)


def _effective_guild_for_dm(user_id: int) -> int | None:
    """Pick which server a DM command should act on for ``user_id``.

    Their stored default wins (when they still share it); otherwise the single
    server they share with the bot is used automatically. Returns ``None`` when
    the choice is ambiguous (multiple shared servers, no default) or when no
    shared server is known — the caller decides how to handle that.
    """
    shared = _shared_guild_ids(user_id)
    if not shared:
        return None
    stored = db.dm_guild_get(user_id)
    if stored is not None and stored in shared:
        return stored
    if len(shared) == 1:
        return shared[0]
    return None


def _dm_storage_guild(user_id: int) -> int | None:
    """A shared guild to attribute a DM-logged *global* entry to when the user
    hasn't pinned one with ``/server``.

    Calories/protein/foods/bodyweight are global, so the stored ``guild_id`` is
    only a label (it decides which server's report/dashboard surfaces the row).
    We prefer the guild that already holds their nutrition goal so it lands with
    their other data, else the lowest shared guild id. None when they share no
    server with the bot.
    """
    shared = _shared_guild_ids(user_id)
    if not shared:
        return None
    home = db.nutrition_home_guild(user_id)
    if home is not None and home in shared:
        return home
    return shared[0]


def _guild_has_gym_channel(guild: "discord.Guild | None") -> bool:
    """True if any configured ``GYM_CHANNEL_IDS`` channel lives in ``guild``.

    The allow-list is global (just channel IDs), so we scope it per-guild with
    this: a guild that contains at least one listed channel is restricted to
    those channels, while a guild with none is scanned in full. That keeps a
    busy server focused on its gym channel yet lets the bot work out-of-the-box
    in every other server it joins — without naming a channel in each one.
    """
    if not GYM_CHANNEL_IDS or guild is None:
        return False
    return any(guild.get_channel(cid) is not None for cid in GYM_CHANNEL_IDS)


async def _tree_interaction_check(interaction: discord.Interaction) -> bool:
    """Resolve a guild for DM interactions before any command runs.

    Guild interactions pass straight through. For DMs we stash the effective
    guild on ``interaction.extras['guild_id']``; if we can't determine one we
    let context-free commands (and autocomplete) through but ask everyone else
    to disambiguate with ``/server``.

    Blacklisted members are blocked from running any command here (their chat is
    still logged, but they can't add anything to the bot).
    """
    is_autocomplete = interaction.type is discord.InteractionType.autocomplete

    # Resolve the effective guild (own guild, or DM-resolved default) up front so
    # the blacklist gate applies to both guild and DM interactions.
    eff_gid = interaction.guild_id
    if eff_gid is None:
        eff_gid = _effective_guild_for_dm(interaction.user.id)
        if eff_gid is not None:
            interaction.extras["guild_id"] = eff_gid

    if eff_gid is not None and not is_autocomplete:
        try:
            blocked = db.message_is_blacklisted(eff_gid, interaction.user.id)
        except Exception:
            LOG.exception("Blacklist check failed in tree check")
            blocked = False
        if blocked:
            await interaction.response.send_message(
                "You've been blacklisted from using this bot in this server.",
                ephemeral=True,
            )
            return False

    if interaction.guild_id is not None:
        return True

    if eff_gid is not None:
        return True

    # Autocomplete can't render a normal reply — just allow it (callbacks fall
    # back to an empty guild and return no suggestions).
    if is_autocomplete:
        return True

    cmd = interaction.command
    name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "")
    if name in _CONTEXT_FREE:
        return True

    await interaction.response.send_message(
        "I couldn't tell which server you mean. DM me from a server we share, "
        "or set your default with `/server`.",
        ephemeral=True,
    )
    return False


bot.tree.interaction_check = _tree_interaction_check  # type: ignore[method-assign]


def _ctx_guild_id(interaction: discord.Interaction) -> int:
    """The guild a command should act on: the interaction's own guild, or the
    DM-resolved one stashed by ``_tree_interaction_check`` (0 if neither)."""
    if interaction.guild_id is not None:
        return interaction.guild_id
    gid = interaction.extras.get("guild_id")
    return int(gid) if gid else 0


def _ctx_guild(interaction: discord.Interaction) -> "discord.Guild | None":
    """The resolved :class:`discord.Guild` object, or ``None`` in an
    unresolved DM context."""
    gid = _ctx_guild_id(interaction)
    return bot.get_guild(gid) if gid else None


async def _target_visible(interaction: discord.Interaction, target) -> bool:
    """Cross-server privacy guard: True if the caller may look up ``target``.

    You can always look up yourself. In a real in-guild invocation a
    ``discord.Member`` option is, by construction, already a member of that
    guild, so there's nothing to verify (and this avoids depending on the
    optional member mirror). In a DM we must confirm the target actually
    belongs to the effective guild before exposing their info — you can't
    query someone you don't share a server with.
    """
    if target.id == interaction.user.id:
        return True
    gid = _ctx_guild_id(interaction)
    if not gid:
        return False
    if interaction.guild_id is not None:
        return True
    guild = bot.get_guild(gid)
    if guild is not None and guild.get_member(target.id) is not None:
        return True
    if db.member_present(gid, target.id):
        return True
    # Cache/mirror can be sparse (members intent off) — confirm with a live
    # lookup before refusing.
    if guild is not None:
        try:
            return await guild.fetch_member(target.id) is not None
        except (discord.NotFound, discord.HTTPException):
            return False
    return False


async def _deny_invisible_target(interaction: discord.Interaction, target) -> bool:
    """Send the standard refusal when ``target`` isn't visible to the caller.

    Returns True (and replies ephemerally) when the lookup must be blocked, so
    callers can ``if await _deny_invisible_target(...): return``.
    """
    if await _target_visible(interaction, target):
        return False
    msg = "You don't share a server with that user, so I can't look up their info."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return True


async def _target_in_channel(interaction: discord.Interaction, target) -> bool:
    """True if ``target`` can see the channel the command was used in.

    Per-channel privacy guard for "look up someone else" lookups: you can only
    pull another member's data from a channel they themselves can read. You can
    always look up yourself. In a DM (no channel) there's nothing to enforce, so
    we defer to the share-a-server guard and allow.
    """
    if target.id == interaction.user.id:
        return True
    channel = interaction.channel
    if channel is None or interaction.guild_id is None:
        return True
    # Need a Member (not a bare User) to evaluate channel permissions.
    member = target if isinstance(target, discord.Member) else None
    if member is None:
        guild = _ctx_guild(interaction)
        member = guild.get_member(target.id) if guild is not None else None
    if member is None:
        return False
    try:
        return channel.permissions_for(member).view_channel
    except (AttributeError, TypeError):
        # Channel type without per-member perms (shouldn't happen for the
        # text channels these commands run in) — don't block on it.
        return True


async def _deny_channel_outsider(interaction: discord.Interaction, target) -> bool:
    """Send the standard refusal when ``target`` can't see the current channel.

    Returns True (and replies ephemerally) when the lookup must be blocked, so
    callers can ``if await _deny_channel_outsider(...): return``.
    """
    if await _target_in_channel(interaction, target):
        return False
    msg = (
        "That member can't see this channel, so I won't show their info here — "
        "ask from a channel they're in."
    )
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return True


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
    canon = _resolve(_ctx_guild_id(interaction), name)
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
    guild_id = _ctx_guild_id(interaction)
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


def _local_log_dates(guild_id: int, user_id: int) -> list[date]:
    """Distinct local-day dates the user logged a lift on, ascending.

    Buckets each stored UTC timestamp into ``DISPLAY_TZ`` *before* taking the
    calendar date, so a 7am session in Adelaide (UTC+9:30) counts on the day
    it actually happened rather than slipping into the previous UTC day.
    This is the correct input for the daily/weekly streak helpers.
    """
    seen: set[date] = set()
    for ts in db.user_log_timestamps(guild_id, user_id):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seen.add(dt.astimezone(DISPLAY_TZ).date())
    return sorted(seen)


def _compute_streak_weeks(dates: list[date]) -> int:
    """Number of consecutive ISO weeks up to the most recent logged week that
    contain at least one lift. ``dates`` are local-day :class:`date` objects
    (see :func:`_local_log_dates`). Returns 0 for an empty list or if the user
    hasn't logged in the current or previous ISO week."""
    if not dates:
        return 0
    today_local = datetime.now(DISPLAY_TZ).date()
    today_year, today_week, _ = today_local.isocalendar()

    weeks: set[tuple[int, int]] = set()
    for parsed in dates:
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


def _msg_guild_id(message: discord.Message) -> int:
    """The server a chat message logs to: its own guild, or — for a DM — the
    sender's effective server (their ``/server`` default, or the single server
    we share). Falls back to a deterministic shared server for global nutrition/
    bodyweight logging when the DM is otherwise ambiguous; 0 when nothing
    resolves."""
    if message.guild is not None:
        return message.guild.id
    return (
        _effective_guild_for_dm(message.author.id)
        or _dm_storage_guild(message.author.id)
        or 0
    )


async def _store_lifts(
    message: discord.Message, lifts: list[Lift], target_user: object | None = None,
    *, logged_at: datetime | None = None,
) -> int:
    target = target_user or message.author
    when = logged_at or message.created_at.astimezone(timezone.utc)
    return db.add_lifts(
        guild_id=_msg_guild_id(message),
        user_id=int(getattr(target, "id")),
        username=_display_name(target),
        lifts=lifts,
        message_id=message.id,
        channel_id=message.channel.id,
        logged_at=when,
        actor_id=message.author.id,
        actor_name=_display_name(message.author),
    )


async def _handle_bodyweight_message(
    message: discord.Message, target: object, weight_kg: float,
) -> None:
    """Persist a chat-message bodyweight update and reply with confirmation.

    Mirrors the validation done by `/bodyweight`: positive values only,
    capped by ``MAX_WEIGHT_KG`` so a fat-fingered "1500" can't poison
    every leaderboard line.
    """
    guild_id = _msg_guild_id(message)
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
        protein_grams = db.set_bodyweight(
            guild_id, target_id, weight_kg,
            actor_id=message.author.id,
            actor_name=_display_name(message.author),
        )
    except Exception:
        LOG.exception("Failed to store bodyweight for user %s", target_id)
        return

    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    suffix = _target_suffix(message.author, target)
    protein_line = (
        f"\n🥩 Protein max updated to {protein_grams} g (tied to bodyweight)."
        if protein_grams is not None else ""
    )
    chart = await _updated_bodyweight_chart(
        target_id, _display_name(target),
    )
    attachment = (
        {"file": _bodyweight_chart_file(chart)} if chart is not None else {}
    )
    reply_text = (
        f"Recorded bodyweight **{weight_kg:g}kg**{suffix}. The bot will "
        "now show the true load on bodyweight-relative lifts (e.g. "
        "assisted pull-ups, weighted dips)."
        f"{protein_line}"
    )
    try:
        await message.reply(
            reply_text,
            mention_author=False,
            **attachment,
        )
    except discord.HTTPException as exc:
        # Missing Attach Files must not suppress the successful weigh-in
        # confirmation. Retry only definite file/payload failures, not a
        # transient server error that could duplicate an accepted message.
        if chart is not None and _attachment_retryable(exc):
            try:
                await message.reply(reply_text, mention_author=False)
            except discord.HTTPException:
                pass
    LOG.info(
        "Stored bodyweight %.2fkg for %s in #%s",
        weight_kg, target, message.channel,
    )


# Default evening time for the streak-saver DM someone opts into via the button.
_STREAK_REMIND_HOUR = 20


class StreakReminderView(discord.ui.View):
    """One-tap opt-in to the evening streak-saver DM, offered at the moment a
    calorie streak hits a milestone. This is the discovery fix for
    ``/calories remind`` — the feature was fully built but sat behind the 21st
    subcommand of the /calories group, so ``calorie_reminder_prefs`` had zero
    rows. The button writes that row directly for the streak's owner."""

    def __init__(self, target_id: int) -> None:
        # A day to decide; after that the button goes inert (non-persistent).
        super().__init__(timeout=86_400)
        self.target_id = target_id

    @discord.ui.button(
        label="🔔 Protect this streak", style=discord.ButtonStyle.success,
    )
    async def enroll(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "That's not your streak to protect 😄", ephemeral=True,
            )
            return
        try:
            db.calorie_reminder_set(self.target_id, _STREAK_REMIND_HOUR, 0)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to set streak reminder for %s", self.target_id)
            await interaction.response.send_message(
                "Couldn't set that up — try `/calories remind`.", ephemeral=True,
            )
            return
        button.disabled = True
        button.label = "🔔 Reminder on (8pm)"
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            "Done — I'll DM you around 8pm on any day you haven't logged, so a "
            "missed evening doesn't end your streak. Change the time or turn it "
            "off any time with `/calories remind`.",
            ephemeral=True,
        )


def _remember_reply(
    reply: "discord.Message", target_id: int, *,
    calorie_id: int = 0, protein_id: int = 0,
    headline: str | None = None, footnote: str | None = None,
) -> None:
    """Record a posted nutrition reply so its totals can be corrected later.

    Best-effort and deliberately silent: a reply that can't be remembered just
    won't be restated, which is exactly the behaviour every reply had before.
    """
    if not (calorie_id or protein_id):
        return
    try:
        db.track_nutrition_reply(
            reply.id, int(getattr(reply.channel, "id", 0) or 0), target_id,
            calorie_id=calorie_id, protein_id=protein_id,
            headline=headline, footnote=footnote,
        )
    except Exception:  # pragma: no cover - defensive; never fail a logged entry
        LOG.exception("Couldn't remember nutrition reply %s", reply.id)


def _log_card(
    icon: str,
    headline: str,
    status: str,
    colour: discord.Colour,
    *,
    author: object | None = None,
    streak: int = 0,
    banner: str = "",
    note: str | None = None,
) -> discord.Embed:
    """The shared card for "I logged this" confirmations.

    One shape for every nutrition write — chat posts, slash commands, AI
    estimates — so the highest-frequency output in the bot stops looking like
    three different features. ``headline`` is what was added, ``status`` the
    two-tier meter for the day so far.

    The title carries the domain icon because the ❌ handler identifies our
    replies by that leading glyph (see :func:`_is_nutrition_reply`).
    """
    embed = ui.card(
        f"{icon} {headline}",
        description=status,
        colour=colour,
        member=author,
        footer=(
            f"{ui.STREAK} {streak}-day streak · react ❌ to remove"
            if streak >= 2 else "react ❌ to remove"
        ),
        timestamp=True,
    )
    if note:
        ui.block(embed, "Note", note)
    if banner:
        ui.block(embed, f"{ui.PARTY} Milestone", banner.strip().lstrip("🎉 *"))
    return embed


async def _reply_calorie_logged(
    message: discord.Message, target: object, goal: sqlite3.Row,
    added_kcal: float, label: str | None, *, entry_id: int = 0,
    logged_at: datetime | None = None,
) -> None:
    """React ✅ and reply with what was added + that day's running total.

    Shared by both chat calorie paths so a `200 calories` post and a saved-food
    shortcut give the same `/calories add`-style feedback. When ``entry_id`` is
    given, the reply is tracked + gets a ❌ affordance so reacting removes that
    specific entry. ``logged_at`` (set when the post was backdated, e.g.
    ``200c yesterday``) anchors the running total to that day and notes it."""
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    total, _n = db.calorie_total_between(
        guild_id, target_id, *_day_window_for(logged_at),
    )
    streak = _calorie_streak(target_id)
    # First live entry of the day is what advances the streak, so it's the one
    # moment to celebrate a milestone (skip backdated fill-ins).
    banner = _streak_milestone_banner(
        "calorie", streak, first_today=(logged_at is None and _n == 1),
    )
    # Offer one-tap streak protection at the celebration moment — but only to
    # people who haven't already opted in, and not to veterans past 30 days who
    # clearly don't want the nudge.
    view: discord.ui.View | None = None
    if banner and streak <= 30 and db.calorie_reminder_get(target_id) is None:
        view = StreakReminderView(target_id)
    suffix = _target_suffix(message.author, target)
    status, colour = _calorie_status_pair(target_id, total, logged_at)
    try:
        reply = await message.reply(
            embed=_log_card(
                ui.FOOD,
                f"+{calories.format_kcal(added_kcal)}"
                f"{suffix}{_backdate_label(logged_at)}",
                status, colour,
                author=target, streak=streak, banner=banner,
                note=_safe_label(label, limit=64) if label else None,
            ),
            mention_author=False,
            view=view,
        )
    except discord.HTTPException:
        return
    if entry_id:
        # Track so a ❌ on this reply removes exactly this entry, and add the
        # ❌ as a one-tap affordance.
        db.track_calorie_reply(
            reply.id, guild_id, message.author.id, target_id, entry_id, message.id,
        )
        _remember_reply(reply, target_id, calorie_id=entry_id)
        try:
            await reply.add_reaction("❌")
        except discord.HTTPException:
            pass


# Cheeky one-liners for when someone logs exactly zero. Keyed by the macro so
# the joke lands ("0 calories? breatharian arc"). Picked at random.
_ZERO_CALORIE_QUIPS = (
    "0 calories? The breatharian arc is wild. Nothing logged. 💀",
    "Logging air, are we? 0 cal isn't a meal — skipped.",
    "0 cal — the legendary nothing-burger. Not logging that one. 🍔",
    "Zero calories? Bold. Photosynthesis isn't tracked here. Skipped.",
    "0c logged would be a lie and we don't lie here. Nothing added.",
)
_ZERO_PROTEIN_QUIPS = (
    "0g protein? Bold strategy for the gains, Cotton. Nothing logged. 💀",
    "0p... so you ate vibes? Skipped.",
    "Zero protein logged — the muscles are filing a complaint. Not added.",
    "0g? That's not a meal, that's a meditation. Skipped.",
    "Logging 0 protein is just typing for fun. Nothing added. 🥩❌",
)


def _rounds_to_zero_kcal(kcal: float) -> bool:
    """True when an amount is positive but displays as ``0 cal``.

    ``1kj`` is 0.239 kcal: it clears a ``> 0`` guard, gets stored, and then
    renders as "🍎 +0 cal" — an entry that claims to be nothing. Anything under
    half a calorie is a typo or a unit mix-up, never a meal, so it's treated the
    same as a literal zero.
    """
    return 0 < kcal < 0.5


def _zero_quip(kind: str) -> str:
    """Random cheeky reply for a logged value of exactly zero."""
    pool = _ZERO_PROTEIN_QUIPS if kind == "protein" else _ZERO_CALORIE_QUIPS
    return random.choice(pool)


def _is_nutrition_tracking(guild_id: int, target: object) -> bool:
    """True when ``target`` has a calorie or protein target set.

    Gates the "did you mean to log that?" nudge, so the bot stays silent for
    people who never asked it to watch their food.
    """
    target_id = int(getattr(target, "id", 0) or 0)
    if not target_id:
        return False
    return (
        db.calorie_goal_get(guild_id, target_id) is not None
        or db.protein_goal_get(guild_id, target_id) is not None
    )


async def _handle_calorie_message(
    message: discord.Message, target: object,
    kcal: float, unit: str, note: str | None,
    *, logged_at: datetime | None = None,
) -> None:
    """Persist a chat-message calorie entry (`650kcal`, `2700kj maccas`).

    Mirrors `/calories add`: the target must have run `/calories setup`
    first, and the per-entry typo cap applies. Replies with a ✅ reaction and
    the running total; a ❌ on that reply reverses it. ``logged_at`` backdates the
    entry (e.g. `650kcal yesterday`) — None files it under the message time.
    """
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    goal = db.calorie_goal_get(guild_id, target_id)
    if goal is None:
        suffix = _target_suffix(message.author, target)
        try:
            await message.reply(
                f"No calorie target set{suffix} yet — run `/calories setup` "
                "with a daily target first (e.g. `2500` or `8700kj`).",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    if kcal <= 0 or _rounds_to_zero_kcal(kcal):
        try:
            await message.reply(_zero_quip("calories"), mention_author=False)
        except discord.HTTPException:
            pass
        return
    if kcal > _MAX_ENTRY_KCAL:
        try:
            await message.reply(
                f"That's over {_MAX_ENTRY_KCAL:,} cal in one entry — looks "
                "like a typo, so I didn't log it.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    try:
        entry_id = db.calorie_add(
            guild_id, target_id, _display_name(target), kcal,
            note=note, raw=message.content.strip()[:80],
            logged_at=logged_at, message_id=message.id,
            actor_id=message.author.id,
            actor_name=_display_name(message.author),
        )
    except Exception:
        LOG.exception("Failed to store calorie entry for user %s", target_id)
        return

    LOG.info(
        "Stored %.0f kcal for %s in #%s", kcal, target, message.channel,
    )
    await _reply_calorie_logged(
        message, target, goal, kcal, note,
        entry_id=entry_id, logged_at=logged_at,
    )


def _match_calorie_food(
    message: discord.Message, target: object, content: str,
) -> tuple[sqlite3.Row, int] | None:
    """If ``content`` is exactly one of ``target``'s saved foods (optionally
    with a serving count), return ``(food_row, servings)``. Requires the user
    to be calorie-tracking; returns None otherwise so the message falls
    through to the lift parser."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    if db.calorie_goal_get(guild_id, target_id) is None:
        return None
    parsed = calories.parse_food_phrase(content)
    if parsed is None:
        return None
    servings, name = parsed
    row = db.calorie_food_get(guild_id, target_id, name)
    if row is None:
        return None
    return row, servings


async def _handle_calorie_food_message(
    message: discord.Message, target: object,
    food_row: sqlite3.Row, servings: int,
    *, logged_at: datetime | None = None,
) -> None:
    """Log a saved-food chat shortcut (`coffee`, `2 protein shake`).

    Always logs the food's calories. If the food was saved with a protein value
    and the user is protein-tracking, the protein is logged too and a combined
    reply (🍎🥩) is posted so a ❌ removes both entries at once. ``logged_at``
    backdates both entries (e.g. `coffee yesterday`)."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    kcal = float(food_row["kcal"]) * servings
    if kcal <= 0 or _rounds_to_zero_kcal(kcal) or kcal > _MAX_ENTRY_KCAL:
        try:
            await message.reply(
                f"That's over {_MAX_ENTRY_KCAL:,} cal in one entry — skipped.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    display = food_row["display"]
    note = display if servings == 1 else f"{display} ×{servings}"
    goal = db.calorie_goal_get(guild_id, target_id)
    if goal is None:  # pragma: no cover - _match_calorie_food already checked
        return
    raw = message.content.strip()[:80]
    try:
        entry_id = db.calorie_add(
            guild_id, target_id, _display_name(target), kcal,
            note=note, raw=raw, logged_at=logged_at, message_id=message.id,
            actor_id=message.author.id,
            actor_name=_display_name(message.author),
        )
    except Exception:
        LOG.exception("Failed to store food entry for user %s", target_id)
        return

    # Optionally log this food's protein too. Gated on the user actually
    # tracking protein and the food carrying a value within sane bounds.
    food_protein = food_row["protein_g"] if "protein_g" in food_row.keys() else None
    pro_goal = db.protein_goal_get(guild_id, target_id)
    grams = float(food_protein) * servings if food_protein is not None else 0.0
    logged_protein = False
    protein_id = 0
    if pro_goal is not None and 0 < grams <= _MAX_PROTEIN_ENTRY_G:
        try:
            protein_id = db.protein_add(
                guild_id, target_id, _display_name(target), grams,
                note=note, raw=raw, logged_at=logged_at, message_id=message.id,
                actor_id=message.author.id,
                actor_name=_display_name(message.author),
            )
            logged_protein = True
        except Exception:
            LOG.exception("Failed to store food protein for user %s", target_id)

    LOG.info(
        "Stored food '%s' ×%d (%.0f kcal%s) for %s in #%s",
        display, servings, kcal,
        f", {grams:.0f}g protein" if logged_protein else "",
        target, message.channel,
    )

    if not logged_protein:
        await _reply_calorie_logged(
            message, target, goal, kcal, note,
            entry_id=entry_id, logged_at=logged_at,
        )
        return

    # Combined card (🍎🥩) — mirrors _handle_combined_nutrition so the shared ❌
    # undo path removes both the calorie and protein entries via the source id.
    window = _day_window_for(logged_at)
    cal_total, _ = db.calorie_total_between(guild_id, target_id, *window)
    pro_total, _ = db.protein_total_between(guild_id, target_id, *window)
    suffix = _target_suffix(message.author, target)
    status, colour = _combined_status(
        target_id, cal_total=cal_total, pro_total=pro_total,
        logged_at=logged_at,
    )
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    try:
        reply = await message.reply(
            # Both macro icons title the card: they mark it as a combined reply
            # for _is_nutrition_reply, and the streaks already ride on their own
            # meter lines, so the footer stays the plain ❌ hint.
            embed=_log_card(
                f"{ui.FOOD}{ui.PROTEIN}",
                f"+{calories.format_kcal(kcal)} + "
                f"{protein_mod.format_grams(grams)} protein"
                f"{suffix}{_backdate_label(logged_at)}",
                status, colour,
                author=target, note=_safe_label(note, limit=64) if note else None,
            ),
            mention_author=False,
        )
    except discord.HTTPException:
        return
    _remember_reply(
        reply, target_id, calorie_id=entry_id, protein_id=protein_id,
    )
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


def _match_calorie_meal(
    message: discord.Message, target: object, content: str,
) -> tuple[str, list[tuple[int, sqlite3.Row]], list[str]] | None:
    """If ``content`` is exactly one of ``target``'s saved meals, return
    ``(display, [(servings, food_row), ...], missing_names)``. Requires the
    user to be calorie-tracking. Foods deleted since the meal was saved end
    up in ``missing_names`` so the reply can flag them."""
    # Same cheap pre-filter as parse_food_phrase: meal names are short single
    # lines, so don't run DB lookups against paragraphs of chat.
    if not content or "\n" in content or len(content) > 64:
        return None
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    if db.calorie_goal_get(guild_id, target_id) is None:
        return None
    name = calories.normalize_food(content)
    if not name:
        return None
    meal = db.calorie_meal_get(target_id, name)
    if meal is None:
        return None
    display, items = meal
    resolved: list[tuple[int, sqlite3.Row]] = []
    missing: list[str] = []
    for servings, food_name in items:
        row = db.calorie_food_get(guild_id, target_id, food_name)
        if row is None:
            missing.append(food_name)
        else:
            resolved.append((servings, row))
    if not resolved:
        return None
    return display, resolved, missing


def _meal_totals(
    items: list[tuple[int, sqlite3.Row]],
) -> tuple[float, float]:
    """Sum ``(kcal, protein_g)`` across resolved meal items."""
    kcal = sum(float(row["kcal"]) * n for n, row in items)
    grams = sum(
        float(row["protein_g"]) * n
        for n, row in items
        if row["protein_g"] is not None
    )
    return kcal, grams


async def _handle_calorie_meal_message(
    message: discord.Message, target: object,
    display: str, items: list[tuple[int, sqlite3.Row]], missing: list[str],
    *, logged_at: datetime | None = None,
) -> None:
    """Log a saved-meal chat shortcut ("breakfast") as ONE calorie entry (plus
    one protein entry when tracked), so a single ❌ removes the whole meal.
    Mirrors :func:`_handle_calorie_food_message`."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    goal = db.calorie_goal_get(guild_id, target_id)
    if goal is None:  # pragma: no cover - _match_calorie_meal already checked
        return
    kcal, grams = _meal_totals(items)
    if kcal <= 0 or _rounds_to_zero_kcal(kcal) or kcal > _MAX_ENTRY_KCAL:
        try:
            await message.reply(
                f"**{display}** adds up to {calories.format_kcal(kcal)} — "
                "outside what I'll log in one entry. Check its foods with "
                "`/calories meal_list`.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    note = f"{display} (meal)"
    raw = message.content.strip()[:80]
    try:
        db.calorie_add(
            guild_id, target_id, _display_name(target), kcal,
            note=note, raw=raw, logged_at=logged_at, message_id=message.id,
            actor_id=message.author.id,
            actor_name=_display_name(message.author),
        )
    except Exception:
        LOG.exception("Failed to store meal entry for user %s", target_id)
        return

    pro_goal = db.protein_goal_get(guild_id, target_id)
    logged_protein = False
    if pro_goal is not None and 0 < grams <= _MAX_PROTEIN_ENTRY_G:
        try:
            db.protein_add(
                guild_id, target_id, _display_name(target), grams,
                note=note, raw=raw, logged_at=logged_at, message_id=message.id,
                actor_id=message.author.id,
                actor_name=_display_name(message.author),
            )
            logged_protein = True
        except Exception:
            LOG.exception("Failed to store meal protein for user %s", target_id)

    LOG.info(
        "Stored meal '%s' (%.0f kcal%s) for %s in #%s",
        display, kcal, f", {grams:.0f}g protein" if logged_protein else "",
        target, message.channel,
    )
    window = _day_window_for(logged_at)
    cal_total, _ = db.calorie_total_between(guild_id, target_id, *window)
    day_targets = _reply_targets(target_id, logged_at)
    day_label = _reply_label(
        day_targets, calories=True, protein=logged_protein,
    )
    suffix = _target_suffix(message.author, target)
    parts = [f"**{calories.format_kcal(kcal)}**"]
    if logged_protein:
        parts.append(f"**{protein_mod.format_grams(grams)}** protein")
    lines = [
        f"{ui.FOOD}{ui.PROTEIN} Logged {' + '.join(parts)} — {note}{suffix}"
        f"{_backdate_label(logged_at)}",
        _calorie_status_line(
            cal_total, day_targets.kcal.value or 0.0,
            None if logged_protein else day_label,
        )
        + _streak_suffix(_calorie_streak(target_id)),
    ]
    if logged_protein:
        pro_total, _ = db.protein_total_between(guild_id, target_id, *window)
        lines.append(
            _protein_status_line(
                pro_total, day_targets.protein.value or 0.0, day_label,
            )
        )
    if missing:
        lines.append(
            f"*(skipped deleted food(s): {', '.join(missing)} — re-save the "
            "meal with `/calories meal_set`)*"
        )
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    try:
        reply = await message.reply("\n".join(lines), mention_author=False)
    except discord.HTTPException:
        return
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


# Caps for AI-estimated entries. Estimates are logged like any other entry but
# marked in the note; a stricter per-entry cap than typed amounts keeps a
# hallucinated number from wrecking a day's stats.
_MAX_AI_ESTIMATE_KCAL = 5_000


async def _attach_undo(
    interaction: discord.Interaction | None,
    *,
    calorie_id: int = 0,
    protein_id: int = 0,
    message: "discord.Message | None" = None,
) -> None:
    """Link freshly-written nutrition rows to this reply and add the ❌.

    ``_handle_nutrition_reaction_undo`` resolves a slash-command reply by the
    reply's *own* message id — a followup has no ``reference`` pointing at a
    source post — so the rows have to carry that id before the reaction can
    find them. The id isn't known until after the insert, hence the backfill.

    Best-effort throughout: the entry is already saved, so a failure here costs
    the one-tap affordance, never the log.
    """
    if not (calorie_id or protein_id):
        return
    # A followup's handle is the object `followup.send` returned —
    # `original_response()` would hand back the deferred first response
    # instead, and the reaction would land on the wrong message.
    msg = message
    if msg is None:
        if interaction is None:
            return
        try:
            msg = await interaction.original_response()
        except discord.HTTPException:
            return
    try:
        if calorie_id:
            db.set_calorie_message_id(calorie_id, msg.id)
        if protein_id:
            db.set_protein_message_id(protein_id, msg.id)
    except Exception:  # pragma: no cover - defensive; never fail a logged entry
        LOG.exception("Failed to link nutrition entry to reply %s", msg.id)
        return
    try:
        await msg.add_reaction("❌")
    except discord.HTTPException:
        pass


def _estimate_failed(reason: str) -> str:
    """Prefix a failed-estimate reason without doubling the leading icon.

    ``gemini_client.friendly_message`` already opens with 🤖, so the old
    f-string rendered "🤖 Couldn't estimate that: 🤖 The AI ...". Parse errors
    come back without one, hence the check rather than a blanket strip.
    """
    reason = (reason or "").strip()
    if reason.startswith(ui.AI):
        return f"{ui.AI} Couldn't estimate that — {reason[len(ui.AI):].lstrip()}"
    return f"{ui.AI} Couldn't estimate that: {reason}"


def _store_ai_nutrition(
    guild_id: int, target: object, kcal: float, protein_g: float, *,
    note: str, raw: str, logged_at: datetime | None = None,
    message_id: int = 0, actor: object | None = None,
) -> tuple[int, int, list[str], list[str]]:
    """Store an AI-derived calorie (+ protein) entry pair and meter both.

    Returns ``(calorie_id, protein_id, amount_parts, status_lines)``. Callers
    write their own headline around ``amount_parts`` — a guessed plate and a
    transcribed packet say different things — but the storing, the "only log
    protein you're actually tracking" rule and the two meters are identical,
    and used to be copied out once per surface.

    ``protein_id`` is 0 when protein wasn't logged, which is the flag callers
    need for :func:`_attach_undo`. ``actor`` is the person who *posted*, when
    that differs from the person being logged for.
    """
    target_id = int(getattr(target, "id"))
    by_proxy = (
        {} if actor is None
        else {"actor_id": int(getattr(actor, "id")),
              "actor_name": _display_name(actor)}
    )
    cal_id = db.calorie_add(
        guild_id, target_id, _display_name(target), kcal,
        note=note, raw=raw, logged_at=logged_at, message_id=message_id,
        **by_proxy,
    )
    pro_goal = db.protein_goal_get(guild_id, target_id)
    logged_protein = (
        pro_goal is not None and 0 < protein_g <= _MAX_PROTEIN_ENTRY_G
    )
    pro_id = 0
    if logged_protein:
        try:
            pro_id = db.protein_add(
                guild_id, target_id, _display_name(target), protein_g,
                note=note, raw=raw, logged_at=logged_at,
                message_id=message_id, **by_proxy,
            )
        except Exception:
            # The calories are already banked; losing the protein row costs
            # half the entry, not the whole log.
            LOG.exception("Failed to store AI protein for user %s", target_id)
            logged_protein = False

    window = _day_window_for(logged_at)
    total, _n = db.calorie_total_between(guild_id, target_id, *window)
    day_targets = _reply_targets(target_id, logged_at)
    # One "Using Weekend Targets" note for the reply, hung off the last meter.
    day_label = _reply_label(day_targets, calories=True, protein=logged_protein)
    parts = [f"**{calories.format_kcal(kcal)}**"]
    status = [
        _calorie_status_line(
            total, day_targets.kcal.value or 0.0,
            None if logged_protein else day_label,
        )
        + _streak_suffix(_calorie_streak(target_id))
    ]
    if logged_protein:
        parts.append(f"**{protein_mod.format_grams(protein_g)}** protein")
        pro_total, _ = db.protein_total_between(guild_id, target_id, *window)
        status.append(_protein_status_line(
            pro_total, day_targets.protein.value or 0.0, day_label,
        ))
    return cal_id, pro_id, parts, status


async def _ai_estimate_meal(
    description: str,
) -> "ai_food.MealEstimate | str | gemini_client.GeminiError":
    """Run the Gemini meal-estimate prompt off-thread.

    Returns the estimate, a short user-facing error string (the reply came back
    but couldn't be parsed), or the :class:`GeminiError` itself when the call
    failed. Handing back the exception rather than flattening it to prose lets
    the slash-command path show admins what Google actually said — a rejected
    key and a garbled reply need very different fixes.
    """
    try:
        raw = await asyncio.to_thread(
            gemini_client.generate,
            f"Estimate this: {description}",
            system=ai_food.ESTIMATE_SYSTEM,
            temperature=0.2,           # estimation, not creativity
            # The token cap is shared with the model's "thinking" pass, and
            # thinking-capable models that can't disable it spend ~300 tokens
            # reasoning even on this tiny prompt — so without generous headroom
            # the JSON gets truncated (finishReason=MAX_TOKENS) and parses as
            # garbage. 200 was the original bug; 768 clears every 2.5/3.x model.
            max_output_tokens=768,
            response_mime_type="application/json",
        )
    except gemini_client.GeminiError as exc:
        return exc
    result = ai_food.parse_estimate(raw)
    if isinstance(result, str):
        # Couldn't turn the reply into an estimate — log the raw reply so any
        # recurrence is diagnosable (prose, non-object JSON, a decline, etc.).
        LOG.warning("AI estimate parse failed (%s); raw reply: %r", result, raw[:500])
    return result


# Attachment guardrails for photo estimates. Discord caps most uploads well
# under this anyway; the cap protects the Gemini request from camera raws.
_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_PHOTO_MIME_OK = ("image/png", "image/jpeg", "image/webp", "image/heic")


def _photo_problem(att: discord.Attachment) -> str | None:
    """Why *att* can't be sent to the vision model, or None when it can."""
    mime = (att.content_type or "").split(";")[0].strip().lower()
    if mime not in _PHOTO_MIME_OK:
        return (
            "That doesn't look like a photo — attach a PNG/JPEG/WebP image of "
            "the food, or of its nutrition panel."
        )
    if att.size > _PHOTO_MAX_BYTES:
        return "That image is over 10 MB — crop it to the food and retry."
    return None


def _first_photo(
    attachments: "list[discord.Attachment]",
) -> "discord.Attachment | None":
    """The first attachment that looks like a usable photo, or None."""
    for att in attachments or ():
        if _photo_problem(att) is None:
            return att
    return None


async def _ai_read_photo(
    mime: str, blob: bytes, description: str | None = None,
) -> "ai_food.MealEstimate | ai_food.LabelInfo | str | gemini_client.GeminiError":
    """Run the Gemini photo prompt off-thread.

    Same contract as :func:`_ai_estimate_meal`, with one extra outcome: a
    :class:`ai_food.LabelInfo` when the photo turned out to be a nutrition
    panel rather than a plate of food. ``description`` is whatever the person
    typed alongside the photo — it goes in as context ("this is two serves"),
    not as a separate estimate.
    """
    prompt = "Read this food photo."
    if description:
        prompt += f" The person adds: {description}"
    try:
        raw = await asyncio.to_thread(
            gemini_client.generate,
            prompt,
            system=ai_food.PHOTO_SYSTEM,
            # Low: a panel in shot is a transcription job, and the estimating
            # branch wants a consistent answer rather than an imaginative one.
            temperature=0.1,
            # Headroom for the thinking pass on models that can't turn it off
            # (see _ai_estimate_meal) — else the JSON truncates mid-object.
            max_output_tokens=768,
            response_mime_type="application/json",
            images=[(mime, blob)],
        )
    except gemini_client.GeminiError as exc:
        return exc
    result = ai_food.parse_photo(raw)
    if isinstance(result, str):
        LOG.warning("AI photo parse failed (%s); raw reply: %r", result, raw[:500])
    return result


async def _handle_estimate_message(
    message: discord.Message, target: object, description: str,
    *, logged_at: datetime | None = None,
) -> bool:
    """Chat AI estimate: `~large big mac meal` → Gemini guesses kcal/protein
    and logs them. Attach a photo and `~` alone is enough — the picture is the
    description. Returns True when the message was consumed (logged or a reply
    was sent), False to fall through to the other parsers (user not tracking,
    or AI not configured)."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    goal = db.calorie_goal_get(guild_id, target_id)
    if goal is None or not gemini_client.available():
        return False
    photo = _first_photo(getattr(message, "attachments", None) or [])
    if photo is None and len(description) < 3:
        return False  # a bare "~" with nothing to go on isn't an estimate
    # Best-effort typing indicator while the AI thinks — never let a missing
    # permission on it kill the actual estimate.
    try:
        await message.channel.typing()
    except discord.HTTPException:
        pass

    if photo is not None:
        try:
            blob = await photo.read()
        except discord.HTTPException:
            try:
                await message.reply(
                    f"{ui.AI} Couldn't download that photo — try re-posting it.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return True
        mime = (photo.content_type or "").split(";")[0].strip().lower()
        result = await _ai_read_photo(mime, blob, description or None)
    else:
        result = await _ai_estimate_meal(description)

    if isinstance(result, (str, gemini_client.GeminiError)):
        reason = (
            gemini_client.friendly_message(result)
            if isinstance(result, gemini_client.GeminiError) else result
        )
        try:
            await message.reply(
                _estimate_failed(reason), mention_author=False,
            )
        except discord.HTTPException:
            pass
        return True
    if isinstance(result, ai_food.LabelInfo):
        # The photo was a packet, not a plate. A panel is per 100 g, so it
        # needs a serving before anything can be logged — the caption can
        # carry one (`~110g`), and otherwise the reply hands back a line to
        # post, since a transcription is worth keeping even unlogged.
        await _log_photo_label(
            message, target, result,
            serving_g=_caption_serving_g(description), logged_at=logged_at,
        )
        return True
    await _log_ai_estimate(message, target, goal, result, logged_at=logged_at)
    return True


def _caption_serving_g(description: str) -> float | None:
    """Grams stated in a photo caption (`~110g`, `~x1.1`), or None.

    Reuses the text logger's scale token so there's exactly one way to say
    "110 g of this" whether you're typing the label values or photographing
    them.
    """
    if not description:
        return None
    split_result = scaling.split(description)
    if split_result is None or split_result[1] is None:
        return None
    return split_result[1].factor * 100.0


def _label_readout(info: "ai_food.LabelInfo") -> tuple[list[str], float | None]:
    """The per-100g readout for a transcribed panel, plus its kcal per 100 g.

    Leads with the food icon, not a label glyph: the ❌ handler only acts on
    replies whose first character is in ``_NUTRITION_REPLY_PREFIXES``.
    """
    kj100, kcal100 = info.kj_per_100g, info.kcal_per_100g
    if kcal100 is None and kj100 is not None:
        kcal100 = calories.kj_to_kcal(kj100)
    if kj100 is None and kcal100 is not None:
        kj100 = calories.kcal_to_kj(kcal100)
    lines = [f"{ui.FOOD} **{info.name or 'that label'}** — per 100 g:"]
    if kj100 is not None:
        lines.append(f"• Energy: **{kj100:,.0f} kJ** ({kcal100:,.0f} cal)")
    if info.protein_per_100g is not None:
        lines.append(f"• Protein: **{info.protein_per_100g:g} g**")
    if info.serving_g:
        lines.append(f"• Stated serving: {info.serving_g:g} g")
    return lines, kcal100


def _label_howto(info: "ai_food.LabelInfo") -> str | None:
    """The single line to post to log a transcribed panel at your own serving.

    A transcription is worth handing back even when we can't log it yet, and
    the scale-token syntax means the answer is one line the person can retype
    for any weight rather than a sum they have to do.
    """
    kj100 = info.kj_per_100g
    if kj100 is None and info.kcal_per_100g is not None:
        kj100 = calories.kcal_to_kj(info.kcal_per_100g)
    if kj100 is None:
        return None
    amounts = f"{kj100:.0f}kj"
    if info.protein_per_100g is not None:
        amounts += f" {info.protein_per_100g:g}p"
    serving = f"{info.serving_g:g}" if info.serving_g else "110"
    return (
        f"\nTo log it, post the values and what you ate — `{amounts} {serving}g`."
    )


class _PhotoLabelReply(NamedTuple):
    """A rendered panel-photo reply, split where its meters sit.

    ``headline`` is the transcription plus the "logged N g" line and
    ``footnote`` the closing subtext — everything either side of the meters,
    kept apart so a later change to the day can swap the middle without
    re-transcribing the packet.
    """

    lines: list[str]
    calorie_id: int
    protein_id: int
    headline: str
    footnote: str | None


def _photo_label_outcome(
    guild_id: int, target: object, info: "ai_food.LabelInfo", *,
    serving_g: float | None, logged_at: datetime | None, raw: str,
    message_id: int = 0, actor: object | None = None,
) -> _PhotoLabelReply:
    """Render a panel photo, logging it too when a serving weight is known.

    The ids are 0 when nothing was stored, which is the normal outcome — most
    panel photos arrive before the person has said how much they ate. Shared by
    ``/estimate`` and the chat ``~`` path so a packet reads the same either way.
    """
    lines, kcal100 = _label_readout(info)
    if serving_g is None or kcal100 is None:
        howto = _label_howto(info)
        if howto:
            lines.append(howto)
        lines.append(ui.subtext("Read by AI — double-check against the label."))
        return _PhotoLabelReply(lines, 0, 0, "\n".join(lines), None)

    scale = float(serving_g) / 100.0
    kcal = kcal100 * scale
    if not (0 < kcal <= _MAX_ENTRY_KCAL):
        lines.append(
            f"\nThat works out to {calories.format_kcal(kcal)} — outside what "
            "I'll log in one entry, so nothing was stored."
        )
        return _PhotoLabelReply(lines, 0, 0, "\n".join(lines), None)
    protein_g = (
        info.protein_per_100g * scale
        if info.protein_per_100g is not None else 0.0
    )
    name = info.name or "label"
    cal_id, pro_id, parts, status = _store_ai_nutrition(
        guild_id, target, kcal, protein_g,
        note=f"{name} ({serving_g:g} g, label)", raw=raw,
        logged_at=logged_at, message_id=message_id, actor=actor,
    )
    lines.append(
        f"\n{ui.FOOD} Logged {' + '.join(parts)} for **{serving_g:g} g**"
        f"{_backdate_label(logged_at)}"
    )
    headline = "\n".join(lines)
    footnote = ui.subtext(
        "Read by AI — react ❌ to remove if it misread the label."
    )
    lines.extend(status)
    lines.append(footnote)
    return _PhotoLabelReply(lines, cal_id, pro_id, headline, footnote)


async def _log_photo_label(
    message: discord.Message, target: object, info: "ai_food.LabelInfo", *,
    serving_g: float | None = None, logged_at: datetime | None = None,
) -> None:
    """Chat-side panel photo: post the readout and log it when a serving was
    given in the caption."""
    out = _photo_label_outcome(
        _msg_guild_id(message), target, info,
        serving_g=serving_g, logged_at=logged_at,
        raw=message.content.strip()[:80], message_id=message.id,
        actor=message.author,
    )
    try:
        reply = await message.reply("\n".join(out.lines), mention_author=False)
    except discord.HTTPException:
        return
    if out.calorie_id or out.protein_id:
        _remember_reply(
            reply, int(getattr(target, "id")), calorie_id=out.calorie_id,
            protein_id=out.protein_id, headline=out.headline,
            footnote=out.footnote,
        )
        try:
            await reply.add_reaction("❌")
        except discord.HTTPException:
            pass


async def _log_ai_estimate(
    message: discord.Message, target: object, goal: sqlite3.Row,
    est: ai_food.MealEstimate, *, logged_at: datetime | None = None,
) -> None:
    """Store an AI meal estimate (calories + protein when tracked) and post
    the combined reply with the ❌-undo affordance. Chat (`~`) path only —
    `/estimate` has its own interaction-based reply."""
    guild_id = _msg_guild_id(message)
    if not (0 < est.kcal <= _MAX_AI_ESTIMATE_KCAL):
        try:
            await message.reply(
                f"🤖 The AI guessed {calories.format_kcal(est.kcal)} — outside "
                "what I'll auto-log. Log it manually if it's real.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    label = est.name or "AI estimate"
    try:
        _cal_id, _pro_id, parts, status = _store_ai_nutrition(
            guild_id, target, est.kcal, est.protein_g or 0.0,
            note=f"{label} (AI estimate)", raw=message.content.strip()[:80],
            logged_at=logged_at, message_id=message.id, actor=message.author,
        )
    except Exception:
        LOG.exception(
            "Failed to store AI estimate for user %s", getattr(target, "id", "?"),
        )
        return
    suffix = _target_suffix(message.author, target)
    conf = f" · confidence: {est.confidence}" if est.confidence else ""
    headline = (
        f"🤖 Estimated **{label}** ≈ {' + '.join(parts)}{conf}{suffix}"
        f"{_backdate_label(logged_at)}"
    )
    footnote = ui.subtext(
        "AI estimate — react ❌ to remove, `/calories edit` to correct."
    )
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    try:
        reply = await message.reply(
            "\n".join([headline, *status, footnote]), mention_author=False,
        )
    except discord.HTTPException:
        return
    _remember_reply(
        reply, int(getattr(target, "id")), calorie_id=_cal_id,
        protein_id=_pro_id, headline=headline, footnote=footnote,
    )
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


def _protein_status_line(
    total: float, ceiling: float, label: str | None = None,
) -> str:
    """Protein progress against a daily **ceiling** — staying under is the win.

    The opposite polarity to :func:`_calorie_status_line`, which is exactly why
    both go through :func:`ui.meter`: the two used one renderer and one colour,
    so a full bar silently meant "well done" on one card and "stop" on the
    other, and only the protein one bothered to warn when it was breached.
    """
    return _protein_status(total, ceiling, label)[0]


def _protein_status(
    total: float, ceiling: float, label: str | None = None,
) -> tuple[str, discord.Colour]:
    """As above, plus the colour an embed built around it should wear."""
    text, colour = ui.meter(
        total, ceiling, protein_mod.format_grams,
        ceiling=True, label="headroom",
    )
    return text + _target_label_suffix(label), colour


async def _handle_protein_message(
    message: discord.Message, target: object, grams: float,
    *, logged_at: datetime | None = None, note: str | None = None,
) -> None:
    """Persist a chat-message protein entry (`40p`, `40g protein`).

    ``logged_at`` backdates the entry (e.g. `40p yesterday`); None files it
    under the message time. ``note`` annotates the reply card — used to show
    the per-100g scaling a `43p 110g` post applied."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    goal = db.protein_goal_get(guild_id, target_id)
    if goal is None:
        suffix = _target_suffix(message.author, target)
        try:
            await message.reply(
                f"No protein target set{suffix} yet — run "
                "`/protein setup <grams>` first (e.g. `180`).",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    if grams <= 0:
        try:
            await message.reply(_zero_quip("protein"), mention_author=False)
        except discord.HTTPException:
            pass
        return
    if grams > _MAX_PROTEIN_ENTRY_G:
        try:
            await message.reply(
                f"That's over {_MAX_PROTEIN_ENTRY_G}g of protein in one entry — "
                "looks like a typo, so I didn't log it.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return
    try:
        entry_id = db.protein_add(
            guild_id, target_id, _display_name(target), grams,
            raw=message.content.strip()[:80],
            logged_at=logged_at, message_id=message.id,
            actor_id=message.author.id,
            actor_name=_display_name(message.author),
        )
    except Exception:
        LOG.exception("Failed to store protein entry for user %s", target_id)
        return
    LOG.info("Stored %.0fg protein for %s in #%s", grams, target, message.channel)
    await _reply_protein_logged(
        message, target, goal, grams, logged_at=logged_at, note=note,
        entry_id=entry_id,
    )


async def _reply_protein_logged(
    message: discord.Message, target: object, goal: sqlite3.Row, grams: float,
    *, logged_at: datetime | None = None, note: str | None = None,
    entry_id: int = 0,
) -> None:
    """React ✅ and reply with what was added + that day's running total vs max.

    ``logged_at`` (a backdated post) anchors the total to that day and notes
    it. ``note`` is a short annotation for the card (e.g. the serving weight a
    per-100g post was scaled by)."""
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    total, _n = db.protein_total_between(
        guild_id, target_id, *_day_window_for(logged_at),
    )
    streak = _protein_streak(target_id)
    banner = _streak_milestone_banner(
        "protein", streak, first_today=(logged_at is None and _n == 1),
    )
    suffix = _target_suffix(message.author, target)
    status, colour = _protein_status_pair(target_id, total, logged_at)
    try:
        reply = await message.reply(
            embed=_log_card(
                ui.PROTEIN,
                f"+{protein_mod.format_grams(grams)} protein"
                f"{suffix}{_backdate_label(logged_at)}",
                status, colour,
                author=target, streak=streak, banner=banner,
                note=_safe_label(note, limit=64) if note else None,
            ),
            mention_author=False,
        )
    except discord.HTTPException:
        return
    _remember_reply(reply, target_id, protein_id=entry_id)
    # ❌ affordance — the legacy undo path resolves the entry via this reply's
    # reference, so no tracking row is needed.
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


async def _handle_combined_nutrition(
    message: discord.Message, target: object, kcal: float, grams: float,
    *, logged_at: datetime | None = None, basis: str | None = None,
) -> None:
    """Log a message that carries BOTH a calorie and a protein amount
    (`500c and 40p`) — stores each entry the user is tracking and posts one
    combined reply. ``logged_at`` backdates both (e.g. `500c and 40p yesterday`).
    ``basis`` names the per-100g scaling applied (`895kj 14.7p 110g`), shown
    under the totals so the arithmetic can be checked against the label."""
    guild_id = _msg_guild_id(message)
    target_id = int(getattr(target, "id"))
    cal_goal = db.calorie_goal_get(guild_id, target_id)
    pro_goal = db.protein_goal_get(guild_id, target_id)
    suffix = _target_suffix(message.author, target)
    if cal_goal is None and pro_goal is None:
        try:
            await message.reply(
                f"No calorie or protein target set{suffix} yet — run "
                "`/calories setup` and/or `/protein setup` first.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    raw = message.content.strip()[:80]
    logged: list[str] = []      # "**500 cal**", "**40 g** protein"
    skipped: list[str] = []
    cal_id = pro_id = 0
    # The day's running total per macro — None for a macro this reply doesn't
    # show, which is what tells the renderer to leave its meter off entirely.
    cal_total: float | None = None
    pro_total: float | None = None

    window = _day_window_for(logged_at)
    if cal_goal is not None:
        if 0 < kcal <= _MAX_ENTRY_KCAL:
            cal_id = db.calorie_add(
                guild_id, target_id, _display_name(target), kcal,
                raw=raw, logged_at=logged_at, message_id=message.id,
                actor_id=message.author.id,
                actor_name=_display_name(message.author),
            )
            cal_total, _n = db.calorie_total_between(
                guild_id, target_id, *window,
            )
            logged.append(f"**{calories.format_kcal(kcal)}**")
        else:
            skipped.append("calories (looks like a typo)")
    elif kcal > 0:
        skipped.append("calories (run `/calories setup`)")

    if pro_goal is not None:
        if 0 < grams <= _MAX_PROTEIN_ENTRY_G:
            pro_id = db.protein_add(
                guild_id, target_id, _display_name(target), grams,
                raw=raw, logged_at=logged_at, message_id=message.id,
                actor_id=message.author.id,
                actor_name=_display_name(message.author),
            )
            pro_total, _n = db.protein_total_between(
                guild_id, target_id, *window,
            )
            logged.append(f"**{protein_mod.format_grams(grams)}** protein")
        else:
            skipped.append("protein (looks like a typo)")
    elif grams > 0:
        skipped.append("protein (run `/protein setup`)")

    if not logged:
        note = ", ".join(skipped) if skipped else "nothing"
        try:
            await message.reply(f"Didn't log {note}.", mention_author=False)
        except discord.HTTPException:
            pass
        return

    LOG.info(
        "Stored combined entry (%s) for %s in #%s",
        " + ".join(logged), target, message.channel,
    )
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    # Notes that ride under the meters: the per-100g basis a `895kj 14.7p 110g`
    # post scaled by, then anything this message asked for but couldn't log.
    notes = [
        n for n in (basis, f"skipped {', '.join(skipped)}" if skipped else "")
        if n
    ]
    tail = "".join(f"\n{ui.subtext(n)}" for n in notes)
    # Stashed with the reply so a later restate can put the tail back verbatim —
    # re-deriving it would lose the scaling this particular post applied. The
    # headline rides along for the plain-text replies still in history; a card
    # keeps its own title through a restate.
    headline = (
        f"{ui.FOOD}{ui.PROTEIN} Logged {' + '.join(logged)}{suffix}"
        f"{_backdate_label(logged_at)}"
    )
    # One renderer for the meters, each macro's own streak, and the single
    # "Using Weekend Targets" caption — shared with the card
    # _restate_one_reply rewrites when an earlier entry later changes.
    status, colour = _combined_status(
        target_id, cal_total=cal_total, pro_total=pro_total,
        logged_at=logged_at,
    )
    try:
        # Both macro icons title the card, marking it a combined reply; the undo
        # handler removes every nutrition entry tied to the source message.
        reply = await message.reply(
            embed=_log_card(
                f"{ui.FOOD}{ui.PROTEIN}",
                f"Logged {' + '.join(logged)}{suffix}"
                f"{_backdate_label(logged_at)}",
                status + tail, colour,
                author=target,
            ),
            mention_author=False,
        )
    except discord.HTTPException:
        return
    _remember_reply(
        reply, target_id, calorie_id=cal_id, protein_id=pro_id,
        headline=headline, footnote=tail.lstrip("\n") or None,
    )
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


# The game-icon refresh moved to app/supervisor.py: it has no Discord
# dependency, and the cache it fills is read by the dashboard, which now
# outlives this process.

_CMD_SIG_KEY = "command_sync_sig"


def _command_tree_signature() -> str:
    """Stable fingerprint of the current slash-command surface + scope.

    Lets us skip Discord's (daily-rate-limited) command sync when nothing has
    changed since the last successful sync — the fix for re-syncing on every
    ``on_ready`` (which re-fires on each gateway reconnect)."""
    def sig(c: object) -> dict:
        d = {
            "n": c.name,
            "d": getattr(c, "description", "") or "",
            "ctx": str(getattr(c, "allowed_contexts", None)),
            "ins": str(getattr(c, "allowed_installs", None)),
        }
        if isinstance(c, app_commands.Group):
            d["s"] = [sig(s) for s in sorted(c.commands, key=lambda x: x.name)]
        else:
            d["p"] = [
                {
                    "n": p.name, "d": p.description or "",
                    "t": str(p.type), "r": p.required,
                    "c": [str(ch.value) for ch in (p.choices or [])],
                }
                for p in getattr(c, "parameters", [])
            ]
        return d

    cmds = sorted(bot.tree.get_commands(), key=lambda c: c.name)
    blob = json.dumps(
        {
            "scope": COMMAND_SCOPE,
            # Derived from DEV_GUILD rather than a second global, so the
            # reload_config rebind has only one name to keep correct. Kept as a
            # STRING to match the pre-refactor `_gid` (which came straight from
            # os.getenv): an int here would change the fingerprint for every
            # existing install and force one spurious sync against Discord's
            # daily-rate-limited command endpoint on upgrade.
            "guild": str(DEV_GUILD.id) if DEV_GUILD is not None else "",
            "cmds": [sig(c) for c in cmds],
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _sync_commands(*, force: bool = False) -> dict:
    """Sync the slash-command tree, skipping the network call when the command
    surface is unchanged since the last successful sync.

    ``force`` overrides the skip (used by ``/sync``). Returns
    ``{"action": "skipped"|"guild"|"global", "count": int}``. The hash is only
    stored on a successful sync, so a failed sync is retried on the next
    ``on_ready``/reconnect rather than being silently marked done.
    """
    signature = _command_tree_signature()
    if not force and db.meta_get(_CMD_SIG_KEY) == signature:
        LOG.info("Slash commands unchanged since last sync — skipping.")
        return {"action": "skipped", "count": 0}
    if COMMAND_SCOPE == "guild" and DEV_GUILD is not None:
        # Dev mode: instant, but only this one guild gets the commands.
        bot.tree.copy_global_to(guild=DEV_GUILD)
        synced = await bot.tree.sync(guild=DEV_GUILD)
        action = "guild"
        LOG.info(
            "Synced %d slash commands to guild %s only "
            "(COMMAND_SCOPE=guild — other servers see nothing)",
            len(synced), DEV_GUILD.id,
        )
    else:
        # Global: every server the bot is in gets the commands. If a guild was
        # previously used for instant sync, clear that guild-scoped copy first
        # so its commands don't show up twice next to the global ones.
        if DEV_GUILD is not None:
            bot.tree.clear_commands(guild=DEV_GUILD)
            await bot.tree.sync(guild=DEV_GUILD)
        synced = await bot.tree.sync()
        action = "global"
        LOG.info(
            "Synced %d global slash commands "
            "(can take up to ~1h to appear in every server)",
            len(synced),
        )
    db.meta_set(_CMD_SIG_KEY, signature)
    return {"action": action, "count": len(synced)}


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
        await _sync_commands()
    except Exception:  # pragma: no cover - discord runtime only
        LOG.exception("Failed to sync commands")

    # Not gated on GYM_CHANNEL_IDS any more. That list narrows where the bot
    # *listens*; when it's empty the bot listens everywhere — and the old gate
    # read the empty list as "nowhere to catch up on", so the default config
    # logged live in every channel and caught up in none of them.
    if BACKFILL_ON_START:
        bot.loop.create_task(_run_startup_backfill())
    else:
        # No backfill to wait on — start auditing live data changes now.
        db.audit_live = True
    if not online_heartbeat.is_running():
        online_heartbeat.start()

    # Seed the dashboard message log from recent history (all channels), so the
    # activity feed isn't empty on a fresh deploy. Independent of the lift/calorie
    # backfill above and of GYM_CHANNEL_IDS.
    if ENABLE_MESSAGE_LOGGING and MESSAGE_LOG_BACKFILL_DAYS > 0:
        bot.loop.create_task(_backfill_message_logs())

    if ENABLE_MEMBER_MIRROR:
        LOG.info(
            "Member/role mirroring ENABLED (Server Members intent in use). "
            "Make sure the Server Members intent is toggled on in the Discord "
            "Developer Portal."
        )
        bot.loop.create_task(_webui_sync_all_guilds())

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

    if not streak_saver_loop.is_running():
        streak_saver_loop.start()
        LOG.info("Streak-saver reminder loop started (15 min cadence)")

    # Nightly backups moved to app/supervisor.py so they keep running even when
    # this process is stopped, restarting or quarantined — which is exactly
    # when a good snapshot matters most.

    if DAILY_UPDATE_CHANNEL_ID and not daily_update.is_running():
        daily_update.start()
        LOG.info(
            "Daily update scheduled for %02d:%02d (%s) in channel %s",
            DAILY_UPDATE_HOUR, DAILY_UPDATE_MINUTE,
            DISPLAY_TZ, DAILY_UPDATE_CHANNEL_ID,
        )

    if WEEKLY_REPORT_CHANNEL_ID and not weekly_report.is_running():
        weekly_report.start()
        LOG.info(
            "Weekly report scheduled for %s %02d:%02d (%s) in channel %s",
            _WEEKDAY_NAMES[WEEKLY_REPORT_WEEKDAY % 7],
            WEEKLY_REPORT_HOUR, WEEKLY_REPORT_MINUTE,
            DISPLAY_TZ, WEEKLY_REPORT_CHANNEL_ID,
        )

    if ENABLE_PRESENCE_TRACKING:
        _seed_tracked_presence_snapshots()

    if ENABLE_VOICE_TRACKING:
        # Reconcile mute/deafen state for anyone already in a call, so time
        # muted across the restart is attributed correctly.
        try:
            _seed_voice_state_snapshots()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to seed voice state snapshots")

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

    if _hevy_enabled() and not hevy_poll.is_running():
        hevy_poll.start()
        LOG.info(
            "Hevy workout poll scheduled every %d minutes%s",
            HEVY_POLL_MINUTES,
            f" → feed <#{HEVY_FEED_CHANNEL_ID}>" if HEVY_FEED_CHANNEL_ID else "",
        )
        if HEVY_FEED_CHANNEL_ID is None:
            LOG.warning(
                "HEVY_FEED_CHANNEL_ID is unset — Hevy workouts will import as "
                "lifts but won't be posted to any channel. Set it to enable the "
                "workout feed."
            )

    if _ha_enabled() and not ha_poll.is_running():
        ha_poll.start()
        alert_channel = _ha_alert_channel_id()
        LOG.info(
            "Home Assistant weigh-in poll scheduled every %d minutes for %d "
            "connected member(s)%s",
            HA_POLL_MINUTES, db.count_ha_servers(),
            f" → <#{alert_channel}>" if alert_channel else "",
        )
        if not alert_channel:
            LOG.warning(
                "BODYWEIGHT_REMINDER_CHANNEL_ID is unset — Home Assistant "
                "weigh-ins will be recorded as bodyweight but not announced "
                "anywhere. Set it to enable the weigh-in alerts."
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


@tasks.loop(minutes=15)
async def streak_saver_loop() -> None:
    """DM opted-in users who haven't logged calories by their chosen time.

    Runs every 15 minutes and fires for a user once their local reminder time
    has passed, at most once per day (``last_sent`` gate). Users who already
    logged today are marked sent without a DM so the check doesn't repeat all
    evening. Opt-in via `/calories remind`.
    """
    now_local = datetime.now(DISPLAY_TZ)
    today = now_local.date().isoformat()
    for row in db.calorie_reminder_list():
        # Isolate each user: one bad row / closed DM must never take the
        # whole loop down (tasks.loop stops on an unhandled exception).
        try:
            await _streak_saver_check_user(row, now_local, today)
        except Exception:
            LOG.exception(
                "Streak-saver check failed for user %s", row["user_id"],
            )


async def _streak_saver_check_user(
    row: sqlite3.Row, now_local: datetime, today: str,
) -> None:
    """Evaluate one opted-in user and DM them if their nudge is due."""
    user_id = int(row["user_id"])
    if row["last_sent"] == today:
        return
    due = dtime(hour=int(row["hour"]), minute=int(row["minute"]))
    if now_local.time() < due:
        return
    # Stopped tracking since opting in → quietly skip (prefs kept so
    # tracking again revives the reminder without another /calories remind).
    if db.calorie_goal_get(0, user_id) is None:
        db.calorie_reminder_mark_sent(user_id, today)
        return
    _total, n = db.calorie_total_between(0, user_id, *_today_window())
    if n > 0:
        db.calorie_reminder_mark_sent(user_id, today)
        return
    # Mark before sending: one attempt per day even when the DM bounces
    # (closed DMs would otherwise retry every 15 minutes all night).
    db.calorie_reminder_mark_sent(user_id, today)
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return
    streak = _calorie_streak(user_id)
    streak_part = (
        f"your **{streak}-day** logging streak ends at midnight"
        if streak >= 2 else "today's diary is still empty"
    )
    try:
        await user.send(
            f"🔔 Nothing logged yet today — {streak_part}. "
            "Reply here with e.g. `650c`, a saved food name, or "
            "`~describe your dinner` and I'll log it. "
            "(`/calories remind off:true` stops these.)"
        )
        LOG.info("Streak-saver DM sent to %s", user_id)
    except discord.HTTPException:
        LOG.info("Streak-saver DM to %s failed (DMs closed?)", user_id)


@streak_saver_loop.before_loop
async def _before_streak_saver() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


# The nightly backup moved to app/supervisor.py (backup_loop). It only ever
# needed the database, and running it in the supervisor means snapshots keep
# being written while this process is stopped, restarting or quarantined.
# Because the supervisor re-reads config on each pass, the BACKUP_* settings
# also became changeable without a restart.


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


def _daily_nutrition_lines(
    guild_id: int, day: date, start_iso: str, end_iso: str,
) -> list[str]:
    """Per-member calorie (and protein) totals for one local day, for the daily
    recap. Only members who actually logged that day appear, ranked by calories.

    Nutrition is global-per-user, so membership is scoped to this guild via
    ``calorie_tracked_users``/``protein_tracked_users`` (which match the members
    mirror); ``day`` resolves each person's effective target for that date."""
    entries: list[tuple[str, int, float, float, int]] = []
    for row in db.calorie_tracked_users(guild_id, day):
        uid = int(row["user_id"])
        total, _ = db.calorie_total_between(guild_id, uid, start_iso, end_iso)
        if total <= 0:
            continue
        name = db.get_user_nickname(uid) or row["username"]
        entries.append((
            name, uid, total, float(row["daily_target_kcal"] or 0.0),
            _calorie_streak(uid),
        ))

    # Protein for the same day, keyed by user so we can inline it and also list
    # anyone who logged protein but no calories.
    pro: dict[int, tuple[str, float]] = {}
    for row in db.protein_tracked_users(guild_id, day):
        uid = int(row["user_id"])
        ptotal, _ = db.protein_total_between(guild_id, uid, start_iso, end_iso)
        if ptotal > 0:
            pro[uid] = (db.get_user_nickname(uid) or row["username"], ptotal)

    if not entries and not pro:
        return []

    entries.sort(key=lambda e: e[2], reverse=True)
    lines = [f"\n{ui.FOOD} **Nutrition**"]
    for name, uid, total, target, streak in entries[:8]:
        tgt = f" / {calories.format_kcal(target)}" if target else ""
        streak_txt = f" 🔥{streak}" if streak >= 2 else ""
        extra = ""
        if uid in pro:
            _, ptotal = pro.pop(uid)
            extra = f" · {protein_mod.format_grams(ptotal)} protein"
        lines.append(
            f"• **{name}** — {calories.format_kcal(total)}{tgt}"
            f"{streak_txt}{extra}"
        )
    # Protein-only loggers (tracking protein but not calories).
    for _uid, (name, ptotal) in pro.items():
        lines.append(
            f"• **{name}** — {protein_mod.format_grams(ptotal)} protein"
        )
    return lines


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
    try:
        day = date.fromisoformat(date_label)
    except ValueError:  # pragma: no cover - date_label is always ISO here
        day = targets_mod.local_today()
    nutrition = _daily_nutrition_lines(guild_id, day, start_iso, end_iso)

    if total_lifts == 0:
        # Only stay silent when NOTHING happened — a day with nutrition activity
        # but no lifts still posts (manual lifting is quiet; nutrition isn't).
        if not nutrition:
            if not post_empty:
                return None
            return (
                f"📊 **Daily gym update — {date_label}**\n"
                "No lifts logged for this day. Fresh slate next session."
            )
        lines = [
            f"📊 **Daily gym update — {date_label}**",
            "No lifts logged, but the kitchen was busy:",
        ]
        lines.extend(nutrition)
        lines.append("\nUse `/calories week` or `/summary` to dig in.")
        return "\n".join(lines)

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

    lines.extend(nutrition)

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
    # The recap grows with the day's activity — PRs, top lifters, popular
    # lifts and per-member nutrition all append — so a busy day can pass
    # Discord's 2 000-char message cap. Unsplit, that 400s into the handler
    # below and the whole recap is lost silently.
    try:
        for part in ui.chunk(text):
            await channel.send(
                part, allowed_mentions=discord.AllowedMentions.none(),
            )
        LOG.info("Daily update posted to #%s for %s", channel, date_label)
    except discord.HTTPException:
        LOG.exception("Failed to post daily update")


@daily_update.before_loop
async def _before_daily_update() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


def _weekly_report_time() -> dtime:
    return _scheduled_time(WEEKLY_REPORT_HOUR, WEEKLY_REPORT_MINUTE)


def _week_window() -> tuple[str, str, str]:
    """``(label, start_iso, end_iso)`` covering the 7 local days ending today.

    Sunday's report therefore spans Monday 00:00 through Sunday 24:00 in
    DISPLAY_TIMEZONE, converted to UTC for querying.
    """
    today = datetime.now(DISPLAY_TZ).date()
    start_day = today - timedelta(days=6)
    start_local = datetime.combine(start_day, dtime.min, tzinfo=DISPLAY_TZ)
    end_local = datetime.combine(
        today + timedelta(days=1), dtime.min, tzinfo=DISPLAY_TZ,
    )
    label = f"{start_day.strftime('%d %b')} – {today.strftime('%d %b')}"
    return (
        label,
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _weekly_gym_text(
    guild_id: int, label: str, start_iso: str, end_iso: str,
) -> str:
    """7-day gym recap. Unlike the daily update, an empty week still posts —
    the silence *is* the report."""
    activity = db.daily_activity(guild_id, start_iso, end_iso, limit=5)
    totals = activity["totals"]
    total_lifts = int(totals["total_lifts"] or 0)
    lines = [f"📈 **Weekly gym report — {label}**"]
    if total_lifts == 0:
        lines.append("No lifts logged this week. New week, new chances. 💪")
        return "\n".join(lines)

    lines.append(
        f"{_plural(total_lifts, 'lift')} logged by "
        f"{_plural(int(totals['lifters'] or 0), 'lifter')} across "
        f"{_plural(int(totals['unique_equip'] or 0), 'exercise')} from "
        f"{_plural(int(totals['sessions'] or 0), 'session')}."
    )

    prs = activity["prs"]
    if prs:
        lines.append("\n🎉 **PRs this week**")
        for row in prs:
            previous = row["prev_best"]
            tail = (
                "first logged" if previous is None
                else f"+{row['weight_kg'] - previous:g}kg"
            )
            lines.append(
                f"• **{row['username']}** — {row['equipment']}: "
                f"{_format_weight(row['weight_kg'], bool(row['bw']))} ({tail})"
            )

    top_users = activity["top_users"]
    if top_users:
        lines.append("\n🏅 **Most active**")
        for row in top_users:
            lines.append(
                f"• **{row['username']}** — {_plural(int(row['n']), 'lift')}, "
                f"{_plural(int(row['equip']), 'exercise')}"
            )

    popular = activity["popular_equipment"]
    if popular:
        lines.append("\n🏋️ **Popular lifts**")
        for row in popular:
            lines.append(
                f"• **{row['equipment']}** — "
                f"{_plural(int(row['n']), 'entry', 'entries')}, "
                f"{_plural(int(row['users']), 'lifter')}"
            )
    return "\n".join(lines)


def _calorie_week_days(
    guild_id: int, user_id: int, start_iso: str, end_iso: str,
) -> dict[str, float]:
    """Per-local-day kcal totals within the window, keyed YYYY-MM-DD."""
    days: dict[str, float] = {}
    for row in db.calorie_entries_between(guild_id, user_id, start_iso, end_iso):
        dt = datetime.fromisoformat(row["logged_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(DISPLAY_TZ).date().isoformat()
        days[day] = days.get(day, 0.0) + float(row["kcal"])
    return days


# Shared guardrails appended to every AI system prompt. Keeps replies grounded
# in the data we actually pass (no invented numbers), free of preamble, and out
# of medical-advice territory.
_AI_GUARDRAILS = (
    "Ground every claim in the data provided — never invent or assume numbers; "
    "if something isn't in the data, say so briefly instead of guessing. Don't "
    "open with filler like 'Sure' or 'Here is' — lead with the content. Avoid "
    "medical, diagnostic, or prescriptive health claims."
)


_CALORIE_SUMMARY_SYSTEM = (
    "You are an upbeat, knowledgeable nutrition coach writing a short, personal "
    "weekly recap for ONE member of a Discord gym community.\n\n"
    "You receive JSON describing their week:\n"
    "- daily_target_kcal: their average goal per day across the week\n"
    "- split_targets: null if one target applies every day. Otherwise an object "
    "with weekday_target_kcal and weekend_target_kcal — they deliberately eat "
    "differently on Sat/Sun, so judge each day against its OWN target and never "
    "call a bigger weekend day a slip-up when it's within the weekend target\n"
    "- per_day: each logged day with its weekday, intake (kcal), the target "
    "(kcal) in force that day, and vs_target (intake minus that day's target; "
    "negative = under, positive = over). target_kcal and vs_target are null on "
    "a day they weren't tracking a target — say nothing about hitting or "
    "missing a target on those days\n"
    "- days_logged: how many of the 7 days they recorded anything\n"
    "- week_avg_kcal / week_total_kcal: averages and totals across logged days\n"
    "- days_over_target / days_under_target: judged per day against that day's "
    "own target\n"
    "- adherence: mean of (intake / that day's target) across logged days; 1.0 "
    "is landing exactly on target\n"
    "- weekday_avg_kcal / weekend_avg_kcal: averages within each band (null if "
    "they logged nothing in that band)\n"
    "- highest_day / lowest_day: their biggest and smallest days\n"
    "- previous_week_avg_kcal: last week's average for trend comparison "
    "(null if they didn't log last week)\n\n"
    "Respond with ONLY a compact JSON object, no markdown or text around it:\n"
    '{"verdict": "...", "tip": "..."}\n'
    "- verdict: 2-3 warm sentences addressing them by name, calling out 2-3 "
    "CONCRETE patterns using the real numbers (logging consistency, how close "
    "they ran to target, weekday-vs-weekend swings, a standout high/low day, and "
    "the trend vs last week when available). Under 400 characters.\n"
    "- tip: ONE specific, actionable tip tailored to exactly what you saw this "
    "week (not generic advice). Under 150 characters.\n"
    "Plain text inside the values (no markdown). No extra keys.\n\n"
    "Tone: friendly, encouraging, a little playful — a coach genuinely rooting "
    "for them. Never shame them, never give medical advice or rigid rules.\n\n"
    "Note on gaps: only logged days are included. A low days_logged or a day "
    "missing from per_day means they didn't record it, NOT that they ate "
    "nothing — treat it as a tracking gap (gently encourage logging), never as "
    "a 0-calorie day or fasting.\n\n" + _AI_GUARDRAILS
)


def _weekday_full(iso_date: str) -> str:
    """Full weekday name (e.g. 'Monday') for a YYYY-MM-DD string."""
    try:
        return date.fromisoformat(iso_date).strftime("%A")
    except ValueError:
        return iso_date


def _build_calorie_ai_payload(
    name: str, target_rows: Sequence[Mapping], days: dict[str, float],
    prev_days: dict[str, float],
) -> str:
    """Assemble the rich JSON describing one member's week for Gemini.

    Every day is scored against the target that was in force *that* day, so a
    2,200-calorie Saturday on a weekend target reads as on-plan rather than as a
    700-calorie blowout. ``target_rows`` is the user's raw rule set, resolved per
    day here.
    """
    sorted_days = sorted(days.items())
    values = list(days.values())
    avg = sum(values) / len(values)
    hi_date, hi_kcal = max(sorted_days, key=lambda kv: kv[1])
    lo_date, lo_kcal = min(sorted_days, key=lambda kv: kv[1])
    prev_avg = (
        round(sum(prev_days.values()) / len(prev_days)) if prev_days else None
    )

    intake = {date.fromisoformat(d): v for d, v in sorted_days}
    resolved = targets_mod.resolve_days(target_rows, intake)
    # A day can have no target at all — they weren't tracking then. It stays
    # null rather than becoming a zero the model would read as "1,450 over".
    day_target = {d: r.kcal.value for d, r in resolved.items()}
    targeted = {d: t for d, t in day_target.items() if t}
    over = sum(1 for d, t in targeted.items() if intake[d] > t)
    under = sum(1 for d, t in targeted.items() if intake[d] < t)
    stats = targets_mod.band_stats(intake, resolved, targets_mod.MACRO_KCAL)
    ratios = [intake[d] / t for d, t in targeted.items()]

    split = None
    if any(r.kcal.split for r in resolved.values()):
        weekday = stats.get("weekday")
        weekend = stats.get("weekend")
        split = {
            "weekday_target_kcal": (
                round(weekday.avg_target) if weekday and weekday.avg_target
                else None
            ),
            "weekend_target_kcal": (
                round(weekend.avg_target) if weekend and weekend.avg_target
                else None
            ),
        }

    def _per_day(iso: str, kcal: float) -> dict:
        target = day_target.get(date.fromisoformat(iso))
        return {
            "weekday": _weekday_full(iso),
            "date": iso,
            "kcal": round(kcal),
            "target_kcal": round(target) if target else None,
            "vs_target": round(kcal - target) if target else None,
        }

    return json.dumps({
        "name": name,
        "daily_target_kcal": (
            round(sum(targeted.values()) / len(targeted)) if targeted else None
        ),
        "split_targets": split,
        "days_logged": len(days),
        "days_in_week": 7,
        "week_avg_kcal": round(avg),
        "week_total_kcal": round(sum(values)),
        "days_over_target": over,
        "days_under_target": under,
        "adherence": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "weekday_avg_kcal": (
            round(stats["weekday"].avg_intake) if "weekday" in stats else None
        ),
        "weekend_avg_kcal": (
            round(stats["weekend"].avg_intake) if "weekend" in stats else None
        ),
        "highest_day": {"weekday": _weekday_full(hi_date), "kcal": round(hi_kcal)},
        "lowest_day": {"weekday": _weekday_full(lo_date), "kcal": round(lo_kcal)},
        "previous_week_avg_kcal": prev_avg,
        "per_day": [_per_day(d, v) for d, v in sorted_days],
    })


async def _calorie_ai_summaries(
    guild_id: int, start_iso: str, end_iso: str,
) -> list[str]:
    """One summary block per calorie-tracking member.

    Uses Gemini when configured; otherwise (or when a call fails) falls back
    to a plain stats line so the report never silently drops someone.
    """
    # Previous 7-day window, for a week-over-week trend signal in the prompt.
    try:
        prev_start_iso = (
            datetime.fromisoformat(start_iso) - timedelta(days=7)
        ).isoformat()
    except ValueError:
        prev_start_iso = start_iso

    blocks: list[str] = []
    for row in db.calorie_tracked_users(guild_id):
        user_id = int(row["user_id"])
        name = db.get_user_nickname(user_id) or row["username"]
        # "Joshua @poshy" — friendly name plus the (non-pinging) mention.
        who = f"**{name}** <@{user_id}>"
        target_rows = db.nutrition_target_rows(user_id)
        days = _calorie_week_days(guild_id, user_id, start_iso, end_iso)
        if not days:
            blocks.append(f"{who} — no intake logged this week.")
            continue
        avg = sum(days.values()) / len(days)
        # Average the targets that were actually in force on the days they
        # logged, so the fallback line stays honest for a weekday/weekend split.
        target = targets_mod.mean_target(
            target_rows, [date.fromisoformat(d) for d in days],
        ) or float(row["daily_target_kcal"])
        stats = (
            f"{len(days)}/7 days logged · avg "
            f"{calories.format_kcal(avg)}/day · target "
            f"{calories.format_kcal(target)}/day"
        )
        verdict, tip = None, None
        if gemini_client.available():
            prev_days = _calorie_week_days(
                guild_id, user_id, prev_start_iso, start_iso,
            )
            payload = _build_calorie_ai_payload(
                name, target_rows, days, prev_days,
            )
            try:
                raw = await asyncio.to_thread(
                    gemini_client.generate,
                    f"Weekly calorie data for {name}:\n{payload}",
                    system=_CALORIE_SUMMARY_SYSTEM,
                    temperature=0.6,  # a touch warmer for a personal recap
                    # Headroom for the thinking pass on models that can't turn
                    # it off (see _ai_estimate_meal) — else the JSON truncates.
                    max_output_tokens=768,
                    response_mime_type="application/json",
                )
                verdict, tip = _parse_recap_json(raw)
            except gemini_client.GeminiError as exc:
                LOG.warning(
                    "Gemini calorie summary failed for %s: %s", name, exc,
                )
        if verdict:
            block = f"{who} ({stats})\n💬 {verdict}"
            if tip:
                block += f"\n💡 {tip}"
            blocks.append(block)
        else:
            blocks.append(f"{who} — {stats}")
    return blocks


def _parse_recap_json(raw: str) -> tuple[str | None, str | None]:
    """Pull ``(verdict, tip)`` from the model's JSON recap.

    Tolerant of stray prose or code fences around the object so a slightly
    chatty model doesn't break rendering; returns ``(None, None)`` if nothing
    usable is found (caller falls back to the plain stats line).
    """
    if not raw:
        return None, None
    text = raw.strip()
    # Strip a ```json ... ``` fence if the model added one.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Grab the outermost {...} span, or repair an unterminated object (some
        # models drop the closing brace even on a "complete" reply), and retry.
        start, end = text.find("{"), text.rfind("}")
        span = text[start:end + 1] if start != -1 and end > start else None
        data = None
        for cand in (span, ai_food.repair_unterminated_json(text)):
            if not cand:
                continue
            try:
                data = json.loads(cand)
                break
            except (ValueError, TypeError):
                data = None
        if data is None:
            # No JSON at all — treat the whole thing as a verdict.
            return text[:400] or None, None
    if not isinstance(data, dict):
        return None, None
    verdict = str(data.get("verdict") or "").strip() or None
    tip = str(data.get("tip") or "").strip() or None
    return (verdict[:500] if verdict else None), (tip[:200] if tip else None)


def _protein_weekly_blocks(
    guild_id: int, start_iso: str, end_iso: str,
) -> list[str]:
    """One stats line per protein-tracking member for the weekly report.

    Plain stats (no AI): days logged, average vs the daily max, and how often
    they went over — the protein tracker is about staying *under* a ceiling."""
    blocks: list[str] = []
    for row in db.protein_tracked_users(guild_id):
        user_id = int(row["user_id"])
        name = db.get_user_nickname(user_id) or row["username"]
        who = f"**{name}** <@{user_id}>"
        target_rows = db.nutrition_target_rows(user_id)
        days = _protein_week_days(guild_id, user_id, start_iso, end_iso)
        if not days:
            blocks.append(f"{who} — no protein logged this week.")
            continue
        # "Over" is judged per day against that day's own ceiling — someone on a
        # higher weekend max isn't over on a big Saturday.
        intake = {date.fromisoformat(d): v for d, v in days.items()}
        resolved = targets_mod.resolve_days(target_rows, intake)
        avg = sum(days.values()) / len(days)
        over = sum(
            1 for d, v in intake.items()
            if resolved[d].protein.value and v > resolved[d].protein.value
        )
        target = targets_mod.mean_target(
            target_rows, intake, targets_mod.MACRO_PROTEIN,
        ) or float(row["daily_target_g"])
        tail = f" · ⚠️ over {over}/{len(days)} days" if over else ""
        blocks.append(
            f"{who} — {len(days)}/7 days · avg "
            f"{protein_mod.format_grams(avg)}/day vs max "
            f"{protein_mod.format_grams(target)}{tail}"
        )
    return blocks


async def _post_weekly_report(
    channel: discord.abc.Messageable, guild_id: int,
) -> None:
    """Send the gym recap, then calorie + protein check-in embeds if anyone's
    tracking. Shared by the scheduled task and /weekly_report."""
    label, start_iso, end_iso = _week_window()
    # The gym recap was the only plain-text section among three embeds, and the
    # only one with no length guard — a busy week could push it past the
    # 2 000-char message cap and lose the lot. As an embed it gets a 4 096-char
    # description, and chunking covers the remainder.
    text = _weekly_gym_text(guild_id, label, start_iso, end_iso)
    head, *rest = text.split("\n", 1)
    body = rest[0] if rest else ""
    for i, part in enumerate(ui.chunk(body, ui.DESC_LIMIT)):
        await channel.send(
            embed=ui.card(
                f"{ui.CHART} Weekly gym report — {label}" if i == 0 else None,
                description=part,
                colour=ui.BRAND,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    strava_blocks = await _strava_weekly_blocks()
    if strava_blocks:
        strava_embed = discord.Embed(
            title=f"🏃 Weekly Strava recap — {label}",
            description="\n".join(strava_blocks)[:4000],
            colour=STRAVA_COLOUR,
        )
        strava_embed.set_footer(text="via Strava")
        await channel.send(
            embed=strava_embed, allowed_mentions=discord.AllowedMentions.none(),
        )
    cal_blocks = await _calorie_ai_summaries(guild_id, start_iso, end_iso)
    if cal_blocks:
        embed = discord.Embed(
            title=f"🍎 Weekly calorie check-in — {label}",
            description="\n\n".join(cal_blocks)[:4000],
            colour=EMBED_COLOUR,
        )
        if gemini_client.available():
            embed.set_footer(text=f"AI summaries · {gemini_client.model_name()}")
        await channel.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none(),
        )
    pro_blocks = _protein_weekly_blocks(guild_id, start_iso, end_iso)
    if pro_blocks:
        embed = discord.Embed(
            title=f"🥩 Weekly protein check-in — {label}",
            description="\n\n".join(pro_blocks)[:4000],
            colour=EMBED_COLOUR,
        )
        await channel.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none(),
        )


@tasks.loop(time=_weekly_report_time())
async def weekly_report() -> None:
    if WEEKLY_REPORT_CHANNEL_ID is None:
        return
    now_local = datetime.now(DISPLAY_TZ)
    if now_local.weekday() != WEEKLY_REPORT_WEEKDAY % 7:
        return
    channel = bot.get_channel(WEEKLY_REPORT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WEEKLY_REPORT_CHANNEL_ID)
        except discord.HTTPException:
            LOG.warning(
                "Weekly report: cannot access channel %s",
                WEEKLY_REPORT_CHANNEL_ID,
            )
            return
    guild = getattr(channel, "guild", None)
    if guild is None:
        LOG.warning(
            "Weekly report channel %s is not in a guild",
            WEEKLY_REPORT_CHANNEL_ID,
        )
        return
    try:
        await _post_weekly_report(channel, guild.id)
        LOG.info("Weekly report posted to #%s", channel)
    except discord.HTTPException:
        LOG.exception("Failed to post weekly report")


@weekly_report.before_loop
async def _before_weekly_report() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


@bot.tree.command(
    name="weekly_report",
    description="Post the weekly gym + calorie report for the past 7 days.",
)
async def weekly_report_cmd(interaction: discord.Interaction) -> None:
    guild_id = _ctx_guild_id(interaction)
    if not guild_id or interaction.channel is None:
        await interaction.response.send_message(
            "This command needs a server. DM me from one we share, or set your "
            "default with `/server`.", ephemeral=True,
        )
        return
    # Gemini round-trips (one per tracked member) can take a while.
    await interaction.response.defer(thinking=True)
    label, start_iso, end_iso = _week_window()
    text = _weekly_gym_text(guild_id, label, start_iso, end_iso)
    await interaction.followup.send(
        text, allowed_mentions=discord.AllowedMentions.none(),
    )
    cal_blocks = await _calorie_ai_summaries(
        guild_id, start_iso, end_iso,
    )
    if cal_blocks:
        embed = discord.Embed(
            title=f"🍎 Weekly calorie check-in — {label}",
            description="\n\n".join(cal_blocks)[:4000],
            colour=EMBED_COLOUR,
        )
        if gemini_client.available():
            embed.set_footer(text=f"AI summaries · {gemini_client.model_name()}")
        await interaction.channel.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none(),
        )
    pro_blocks = _protein_weekly_blocks(guild_id, start_iso, end_iso)
    if pro_blocks:
        embed = discord.Embed(
            title=f"🥩 Weekly protein check-in — {label}",
            description="\n\n".join(pro_blocks)[:4000],
            colour=EMBED_COLOUR,
        )
        await interaction.channel.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none(),
        )


def _backfill_nutrition_entry(
    msg: discord.Message, target: object, content: str,
) -> tuple[float, float]:
    """Store any nutrition log in ``content`` that isn't recorded already.

    Covers the same three chat forms the live path does — calories, protein,
    and the combined ``500c and 40p`` — rather than calories alone, which used
    to make a restart silently drop every protein log posted while the bot was
    down. Deduped by message id and dated to the message, so re-scans are free.

    Returns the ``(kcal, protein_g)`` actually stored — zeroes when nothing
    was, which is the common case. The amounts come back rather than a bare
    flag so the catch-up can report what it added instead of only how many
    posts it touched. The live path is :func:`_handle_calorie_message` and
    friends.
    """
    hint_dt, parse_content = _split_date_hint(
        content, msg.created_at.astimezone(DISPLAY_TZ),
    )
    kcal = grams = 0.0
    cal_hit = calories.parse_chat_message(parse_content)
    if cal_hit is not None:
        kcal = cal_hit[0]
    else:
        pro_hit = protein_mod.parse_protein_chat_message(parse_content)
        if pro_hit is not None:
            grams = pro_hit
        else:
            both = nutrition.parse_combined(parse_content)
            if both is None:
                return 0.0, 0.0
            kcal, grams = both

    guild_id = msg.guild.id if msg.guild else 0
    target_id = int(getattr(target, "id"))
    logged_at = hint_dt or msg.created_at.astimezone(timezone.utc)
    raw = msg.content.strip()[:80]
    note = nutrition.chat_scale_note(parse_content)
    added_kcal = added_grams = 0.0

    # Each macro is stored only if the person tracks it and the amount clears
    # the same typo guards the live path applies — a backfill must not become a
    # side door around them.
    if (
        kcal > 0 and not _rounds_to_zero_kcal(kcal) and kcal <= _MAX_ENTRY_KCAL
        and db.calorie_goal_get(guild_id, target_id) is not None
    ):
        try:
            if db.calorie_add(
                guild_id, target_id, _display_name(target), kcal,
                note=note, raw=raw, logged_at=logged_at, message_id=msg.id,
            ):
                added_kcal = kcal
        except Exception:
            LOG.exception(
                "Backfill: failed to store calorie entry for %s", target_id,
            )
    if (
        0 < grams <= _MAX_PROTEIN_ENTRY_G
        and db.protein_goal_get(guild_id, target_id) is not None
    ):
        try:
            if db.protein_add(
                guild_id, target_id, _display_name(target), grams,
                note=note, raw=raw, logged_at=logged_at, message_id=msg.id,
            ):
                added_grams = grams
        except Exception:
            LOG.exception(
                "Backfill: failed to store protein entry for %s", target_id,
            )
    return added_kcal, added_grams


class _CaughtUpPerson:
    """What one member's catch-up added, for the summary line."""

    __slots__ = ("grams", "kcal", "lifts", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self.kcal = 0.0
        self.grams = 0.0
        self.lifts = 0

    def add(self, *, kcal: float = 0.0, grams: float = 0.0, lifts: int = 0) -> None:
        self.kcal += kcal
        self.grams += grams
        self.lifts += lifts

    def summary(self) -> str:
        """"526 cal + 40 g protein + 3 lifts" — only the parts that happened."""
        bits = []
        if self.kcal:
            bits.append(f"**{calories.format_kcal(self.kcal)}**")
        if self.grams:
            bits.append(f"**{protein_mod.format_grams(self.grams)}** protein")
        if self.lifts:
            bits.append(f"**{ui.plural(self.lifts, 'lift')}**")
        return " + ".join(bits)


class _CatchUpResult(NamedTuple):
    """What one channel scan found. ``people`` keys are member ids."""

    scanned: int
    posts_with_lifts: int
    lifts: int
    suppressed: int
    entries: int
    people: dict[int, _CaughtUpPerson]

    @property
    def anything(self) -> bool:
        return bool(self.lifts or self.entries)


def _tally(
    people: dict[int, _CaughtUpPerson], target: object,
) -> _CaughtUpPerson:
    """The running tally for ``target``, created on first sight."""
    target_id = int(getattr(target, "id", 0) or 0)
    person = people.get(target_id)
    if person is None:
        person = people[target_id] = _CaughtUpPerson(_display_name(target))
    return person


def _format_downtime(delta: timedelta) -> str:
    """A rough "2h 14m" for how long the bot was away.

    Rounded and units-trimmed on purpose: the point is "you were gone a while",
    not a stopwatch reading.
    """
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if not minutes else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if not hours else f"{days}d {hours}h"


async def _backfill_channel(
    channel: discord.abc.Messageable, limit: int | None,
    *, since: datetime | None = None, react: bool = False,
) -> "_CatchUpResult":
    """Scan a channel's history and store any detected lifts or nutrition logs.

    ``since`` scans forward from that moment — how the startup catch-up asks
    for "everything posted while I was down". Without it the scan walks *back*
    from the newest message, which is what ``/backfill 200`` means.

    Getting that direction wrong is what made the catch-up useless: the old
    call paired ``oldest_first=True`` with a limit, and discord.py reads that
    as "start at the channel's first ever message", so a busy channel spent
    every boot re-reading its opening 1,000 posts and never reached today's.

    Lifts dedupe on (message_id, equipment); nutrition entries dedupe on
    message_id — so re-runs are safe. ``react`` adds the ✅ the live path would
    have added, so a catch-up is visible in the channel rather than silent.
    """
    scanned = matched = inserted = skipped = cal_inserted = 0
    people: dict[int, _CaughtUpPerson] = {}
    history = (
        channel.history(limit=limit, after=since, oldest_first=True)
        if since is not None
        else channel.history(limit=limit)
    )
    async for msg in history:
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
        # Nutrition logs ("650kcal", "200c", "40p", "@user 2700kj") never
        # contain a lift, so check them first and move on when one matches.
        added_kcal, added_grams = _backfill_nutrition_entry(msg, target, content)
        if added_kcal or added_grams:
            cal_inserted += 1
            _tally(people, target).add(kcal=added_kcal, grams=added_grams)
            if react:
                try:
                    await msg.add_reaction("✅")
                except discord.HTTPException:
                    pass
            continue
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
            _tally(people, target).add(lifts=n)
    return _CatchUpResult(scanned, matched, inserted, skipped, cal_inserted, people)


# When the bot was last known to be running. Written every few minutes, and
# read once on boot to work out how far back the catch-up has to reach.
_LAST_ONLINE_KEY = "last_online_at"
# A first boot (or a marker lost to a wiped DB) has nothing to scan *from*.
# Reaching back further than this would mean re-reading months of history to
# find logs that, if they mattered, someone has long since re-posted.
_MAX_CATCHUP_DAYS = 14
# Downtime shorter than this is a redeploy, not an outage; the messages posted
# during it are still worth catching, hence the small floor rather than none.
_CATCHUP_GRACE = timedelta(minutes=1)


@tasks.loop(minutes=5)
async def online_heartbeat() -> None:
    """Record that the bot is up, so the next boot knows what it missed."""
    try:
        db.meta_set(_LAST_ONLINE_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:  # pragma: no cover - defensive; a missed beat is harmless
        LOG.exception("Couldn't record the online heartbeat")


def _catchup_since() -> datetime:
    """The moment the startup scan should pick up from.

    The last recorded heartbeat, or — on a first boot, or one where that marker
    predates the cap — a bounded lookback, so a fresh deploy doesn't try to
    re-read the entire channel.
    """
    floor = datetime.now(timezone.utc) - timedelta(days=_MAX_CATCHUP_DAYS)
    last = _parse_iso(db.meta_get(_LAST_ONLINE_KEY))
    if last is None:
        LOG.info(
            "Catch-up: no last-online marker, scanning the last %d days",
            _MAX_CATCHUP_DAYS,
        )
        return floor
    return max(last - _CATCHUP_GRACE, floor)


def _catchup_channels() -> list[discord.abc.Messageable]:
    """The channels the startup scan should cover.

    Deliberately the same set the live path listens to. GYM_CHANNEL_IDS
    narrows both when it's set; when it's empty the bot logs from *every*
    channel it can read, and the catch-up has to do the same — the old version
    looped over GYM_CHANNEL_IDS alone, so on the default (empty) config it
    scanned nothing at all while live logging worked everywhere.
    """
    if GYM_CHANNEL_IDS:
        out = []
        for channel_id in GYM_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel is None:
                LOG.warning("Catch-up: cannot access channel %s", channel_id)
                continue
            out.append(channel)
        return out
    out = []
    me = None
    for guild in bot.guilds:
        me = guild.me
        for channel in getattr(guild, "text_channels", []):
            perms = channel.permissions_for(me) if me else None
            if perms is None or (perms.read_messages and perms.read_message_history):
                out.append(channel)
    return out


# How many people to name individually before the summary gives up and counts
# them, so a long outage across a busy server can't post a wall of text.
_CATCHUP_NAMES_SHOWN = 8


def _catchup_summary(result: "_CatchUpResult", downtime: str | None) -> str:
    """The "here's what I missed" message for one channel.

    ✅ marks on the individual posts say *that* something landed; this says
    *what*, in one place, without anyone having to open /today and compare it
    against what they remember typing.
    """
    away = f" after {downtime} offline" if downtime else ""
    posts = ui.plural(result.entries + result.posts_with_lifts, "post")
    lines = [f"{ui.FOOD}{ui.PROTEIN} Caught up{away} — imported {posts} I missed:"]
    ranked = sorted(
        result.people.values(),
        key=lambda p: (p.kcal + p.grams * 10 + p.lifts * 100),
        reverse=True,
    )
    for person in ranked[:_CATCHUP_NAMES_SHOWN]:
        body = person.summary()
        if body:
            lines.append(f"• **{_safe_label(person.name, limit=32)}** — {body}")
    extra = len(ranked) - _CATCHUP_NAMES_SHOWN
    if extra > 0:
        lines.append(f"• …and {ui.plural(extra, 'other')}")
    lines.append(ui.subtext(
        "Each post above is marked ✅. Check the day with `/today`, or fix an "
        "amount with `/calories edit` · `/protein edit`."
    ))
    return "\n".join(lines)


async def _run_startup_backfill() -> None:
    """Catch up on everything posted while the bot was down.

    Scans forward from the last heartbeat rather than over a fixed slice of
    history, which makes a quiet restart nearly free and a long outage still
    complete. Newly imported logs get the ✅ they'd have got live, and each
    channel that gained anything gets a summary saying what landed — a restart
    shouldn't leave people wondering whether their logs made it.
    """
    since = _catchup_since()
    last_online = _parse_iso(db.meta_get(_LAST_ONLINE_KEY))
    downtime = (
        _format_downtime(datetime.now(timezone.utc) - last_online)
        if last_online is not None else None
    )
    limit = BACKFILL_LIMIT if BACKFILL_LIMIT > 0 else None
    channels = _catchup_channels()
    LOG.info(
        "Catch-up: scanning %d channel(s) for anything since %s",
        len(channels), since.isoformat(timespec="seconds"),
    )
    scanned_total = lifts_total = entries_total = 0
    for channel in channels:
        try:
            result = await _backfill_channel(
                channel, limit, since=since, react=True,
            )
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            LOG.warning("Catch-up: HTTP error scanning #%s", channel)
            continue
        scanned_total += result.scanned
        lifts_total += result.lifts
        entries_total += result.entries
        if not result.anything:
            continue  # a quiet restart says nothing at all
        LOG.info(
            "Catch-up for #%s: scanned=%d, new_lifts=%d, new_nutrition=%d, "
            "skipped_suppressed=%d",
            channel, result.scanned, result.lifts, result.entries,
            result.suppressed,
        )
        try:
            await channel.send(
                _catchup_summary(result, downtime),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            LOG.warning("Catch-up: couldn't post the summary in #%s", channel)
    LOG.info(
        "Catch-up done: scanned=%d, new_lifts=%d, new_nutrition=%d",
        scanned_total, lifts_total, entries_total,
    )
    db.meta_set(_LAST_ONLINE_KEY, datetime.now(timezone.utc).isoformat())
    # History import is done — from here on, data mutations are live user
    # actions worth recording in the dashboard audit log.
    db.audit_live = True


async def _backfill_message_logs() -> None:  # pragma: no cover - discord runtime
    """Seed the message log from recent channel history.

    Scans every readable text channel in every guild for the last
    ``MESSAGE_LOG_BACKFILL_DAYS`` days so the dashboard's activity feed has data
    immediately on startup. The full window is re-scanned every boot (rather than
    resuming from the newest stored message) so any messages that were previously
    deleted are restored — logging dedupes on message id, so re-scans are cheap
    and never duplicate.
    """
    if not ENABLE_MESSAGE_LOGGING or MESSAGE_LOG_BACKFILL_DAYS <= 0:
        return
    after = datetime.now(timezone.utc) - timedelta(
        days=MESSAGE_LOG_BACKFILL_DAYS
    )
    for guild in bot.guilds:
        logged = 0
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.read_messages and perms.read_message_history):
                continue
            try:
                async for msg in channel.history(
                    limit=None, after=after, oldest_first=True,
                ):
                    media = await _message_media(msg)
                    if not msg.content and not media:
                        continue
                    if db.message_log_add(
                        guild.id, msg.author.id, msg.content,
                        channel_id=channel.id, channel_name=channel.name,
                        message_id=msg.id, at=msg.created_at,
                        attachments=media,
                    ):
                        logged += 1
            except discord.Forbidden:
                continue
            except discord.HTTPException:
                LOG.warning(
                    "Message-log backfill: HTTP error scanning #%s", channel,
                )
                continue
        LOG.info(
            "Message-log backfill for %s: %d new messages (since %s)",
            guild, logged, after.isoformat(timespec="seconds"),
        )


def _looks_like_log_attempt(text: str) -> bool:
    """True if a DM message looks like one of the freeform logging shortcuts.
    Used to decide whether to nudge a user about ``/server`` when we can't pin
    their DM to a guild, instead of replying to every casual DM."""
    if not text:
        return False
    if _parse_bodyweight_message(text) is not None:
        return True
    if calories.parse_chat_message(text) is not None:
        return True
    if protein_mod.parse_protein_chat_message(text) is not None:
        return True
    if nutrition.parse_combined(text) is not None:
        return True
    if cardio.parse_chat_message(text) is not None:
        return True
    lifts, _ = _split_reasonable_lifts(parse_message(text))
    return bool(lifts and _should_auto_store(lifts))


def _attachment_kind(att: discord.Attachment) -> str:
    """Classify an attachment as ``image`` / ``video`` / ``file`` from its
    content-type, falling back to the filename extension."""
    ct = (att.content_type or "").lower()
    name = (att.filename or "").lower()
    if ct.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic")
    ):
        return "image"
    if ct.startswith("video/") or name.endswith(
        (".mp4", ".webm", ".mov", ".mkv", ".avi")
    ):
        return "video"
    return "file"


async def _store_attachment(att: discord.Attachment, guild_id: int) -> str | None:
    """Download one attachment into ``MEDIA_DIR`` and return its dashboard-served
    relative path (``<guild_id>/<id><ext>``), or None if it couldn't be saved.

    Idempotent: a file already on disk (from a previous live-log or backfill
    pass) is reused rather than re-downloaded. Oversized attachments and any
    download failure return None so the caller keeps the (expiring) remote URL.
    """
    if MEDIA_MAX_MB > 0 and att.size and att.size > MEDIA_MAX_MB * 1024 * 1024:
        return None
    ext = os.path.splitext(att.filename or "")[1].lower()
    # Keep only a safe alphanumeric extension; drop anything weird.
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", ext or ""):
        ext = ""
    rel = f"{guild_id}/{att.id}{ext}"
    dest = os.path.join(MEDIA_DIR, str(guild_id), f"{att.id}{ext}")
    try:
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return rel  # already stored
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        await att.save(dest, use_cached=False)
        return rel
    except (discord.HTTPException, OSError) as exc:
        LOG.warning("Couldn't store attachment %s: %s", att.id, exc)
        # Clean up a half-written file so a retry can succeed.
        try:
            if os.path.exists(dest) and os.path.getsize(dest) == 0:
                os.remove(dest)
        except OSError:
            pass
        return None


async def _message_media(message: discord.Message) -> str | None:
    """Collect every attachment (images, videos, GIFs and any other uploaded
    file) plus embedded image/GIF media from a message, as a JSON list of
    ``{"url", "kind", "name", "stored"}`` items, or None when there's none.

    Uploaded attachments are downloaded into ``MEDIA_DIR`` and referenced by a
    local ``/media/...`` path (``stored: true``) so they outlive Discord's
    expiring CDN links and survive the message being deleted. Embed media
    (Tenor/Giphy GIFs, link-preview images) stays as the original remote URL
    (``stored: false``) — it's hosted elsewhere and doesn't expire with Discord.
    Powers the photos/GIFs/files shown in the dashboard Messages tab.
    """
    items: list[dict] = []
    seen: set[str] = set()

    def _add(url: str | None, kind: str, *, name: str | None = None,
             stored: bool = False) -> None:
        if url and url not in seen:
            seen.add(url)
            item = {"url": url, "kind": kind, "stored": stored}
            if name:
                item["name"] = name
            items.append(item)

    for att in message.attachments:
        kind = _attachment_kind(att)
        rel = await _store_attachment(att, message.guild.id) if (
            ENABLE_MEDIA_DOWNLOAD and message.guild is not None
        ) else None
        if rel is not None:
            _add(f"/media/{rel}", kind, name=att.filename, stored=True)
        else:
            _add(att.url, kind, name=att.filename, stored=False)
    for emb in message.embeds:
        etype = (getattr(emb, "type", "") or "").lower()
        vid = getattr(getattr(emb, "video", None), "url", None)
        thumb = getattr(getattr(emb, "thumbnail", None), "url", None)
        img = getattr(getattr(emb, "image", None), "url", None)
        if etype == "gifv":
            # Animated GIF (Tenor/Giphy): the mp4 loops like a GIF; fall back to
            # the still thumbnail if there's no video url.
            _add(vid, "video") if vid else _add(thumb, "image")
        elif etype == "image":
            _add(img or thumb or getattr(emb, "url", None), "image")
    # Stickers render as images (PNG/APNG/GIF). Lottie stickers are vector JSON
    # the dashboard can't display, so they're skipped here (the message is still
    # logged via the on_message sticker check).
    for sticker in getattr(message, "stickers", []) or []:
        fmt = getattr(getattr(sticker, "format", None), "name", "") or ""
        if fmt.lower() != "lottie":
            _add(getattr(sticker, "url", None), "image", name=sticker.name)
    return json.dumps(items) if items else None


@bot.event
async def on_message(message: discord.Message) -> None:
    # Message logging for the web dashboard. Logs every author (including bots,
    # so the bot's own announcements show up, and blacklisted users — blacklist
    # only blocks adding data, not logging) in every channel. Runs before the
    # bot/guild and gym-channel gates. Failures here must never break handling.
    if ENABLE_MESSAGE_LOGGING and message.guild is not None:
        try:
            media = await _message_media(message)
            if message.content or media or message.stickers:
                db.message_log_add(
                    message.guild.id, message.author.id, message.content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    message_id=message.id,
                    at=message.created_at,
                    attachments=media,
                )
        except Exception:
            LOG.exception("Failed to log message for activity feed")
    if message.author.bot:
        return
    in_dm = message.guild is None
    # Resolve which server this message logs to: its own guild, or — for a DM —
    # the sender's effective server (their /server default, or the one server we
    # share). This is what makes the freeform shortcuts work in DMs too.
    gid = message.guild.id if message.guild else _effective_guild_for_dm(
        message.author.id
    )
    # Ambiguous DM (in 2+ servers, no /server default): calories/protein/foods/
    # bodyweight are all global, so we can still log those — attribute them to a
    # deterministic shared server. Only the lift path (per-server) still asks for
    # /server, guarded below.
    dm_ambiguous = in_dm and gid is None
    if dm_ambiguous:
        gid = _dm_storage_guild(message.author.id)
    if gid is None:
        # DM with no shared server at all — nothing we can attribute it to.
        # Nudge only when it looks like a real logging attempt.
        if in_dm and _looks_like_log_attempt(message.content):
            try:
                await message.reply(
                    "I couldn't log that — we don't share a server. Add me to "
                    "your server (or join one I'm in), then try again.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
        return
    # Blacklisted members can't add anything to the bot — their message is still
    # logged above, but we ignore it for all data entry (lifts, calories,
    # protein, bodyweight, saved foods) and prefix commands.
    try:
        if db.message_is_blacklisted(gid, message.author.id):
            LOG.info(
                "Blacklist: ignored message from %s in guild %s",
                message.author.id, gid,
            )
            return
    except Exception:
        LOG.exception("Blacklist check failed in on_message")
    # The gym-channel allow-list only restricts guilds that actually contain a
    # listed channel — a server configured with specific channels stays focused
    # there, while every OTHER server the bot is in scans all channels (so chat
    # logging / saved-food shortcuts just work everywhere). DMs always go through.
    if (
        not in_dm and GYM_CHANNEL_IDS
        and message.channel.id not in GYM_CHANNEL_IDS
        and _guild_has_gym_channel(message.guild)
    ):
        await bot.process_commands(message)
        return

    guild_aliases = _custom_alias_map(gid)
    if in_dm:
        # DMs always log for the sender themselves — no @mention / nickname
        # re-targeting (there's nobody else in a DM). Prefer their guild member
        # object so we store their server nickname rather than their handle.
        guild_obj = bot.get_guild(gid)
        member = guild_obj.get_member(message.author.id) if guild_obj else None
        target, content = (member or message.author), message.content
    else:
        target, content = _message_lift_target(message)
        # Nickname-prefix targeting: "Sean bench 30kg" resolves to the user
        # nicknamed Sean, exactly like a leading @mention would.
        if target == message.author:
            nick_target, nick_content = await _resolve_nickname_target(
                message.content, message.guild
            )
            if nick_target is not None:
                target, content = nick_target, nick_content

    # Don't let anyone log *for* a blacklisted member either (e.g. "@dos 100kg"
    # or a nickname-prefixed line). The author was already checked above.
    target_id = int(getattr(target, "id", 0) or 0)
    if (
        target_id and target_id != message.author.id
        and db.message_is_blacklisted(gid, target_id)
    ):
        LOG.info(
            "Blacklist: ignored log targeting %s in guild %s", target_id, gid,
        )
        return

    # Quick bodyweight update path: `bodyweight 100kg`, `body weight: 95.5`,
    # `bw 80`, or `@dos bodyweight 100kg` (leading mention re-targets just
    # like for lifts). Handled before parse_message so it doesn't get
    # filtered out as bodyweight chatter.
    bw_kg = _parse_bodyweight_message(content)
    if bw_kg is not None:
        await _handle_bodyweight_message(message, target, bw_kg)
        await bot.process_commands(message)
        return

    # Backdating for nutrition logs: a trailing "yesterday" / "monday" /
    # "3 days ago" / ISO date files the entry under that day. We strip just that
    # phrase so the strict amount-only parsers below still match ("500c
    # yesterday" -> "500c"); the original `content` is left untouched for the
    # lift path, which does its own date resolution.
    nut_dt, nut_content = _split_date_hint(
        content, message.created_at.astimezone(DISPLAY_TZ),
    )

    # AI meal estimate: `~large big mac meal` asks Gemini to guess calories
    # and protein, then logs them. Attach a photo and a bare `~` is enough —
    # the picture is the description, and a caption on top is extra context.
    # The `~` prefix is an explicit opt-in per message (a double `~~` is
    # Discord strikethrough, not an estimate), which is what keeps progress
    # pics and whiteboard shots out of the vision model. The handler falls
    # through silently when the user isn't calorie-tracking or AI isn't
    # configured; the length floor now lives with it, since it depends on
    # whether there's a photo to fall back on.
    if nut_content.startswith("~") and not nut_content.startswith("~~"):
        handled = await _handle_estimate_message(
            message, target, nut_content.lstrip("~ ").strip(),
            logged_at=nut_dt,
        )
        if handled:
            await bot.process_commands(message)
            return

    # Quick calorie logging path: `650kcal`, `200 cal toastie`, `2700kj`,
    # or `@user 650kcal` to log for someone else. The unit is mandatory
    # (kcal/cal/kj) so plain lift numbers never match this branch. A scale
    # token (`2700kj 110g`) logs a per-100g label at the serving eaten; the
    # note records what it was scaled by so the card can show its working.
    cal_hit = calories.parse_chat_message(nut_content)
    if cal_hit is not None:
        cal_kcal, cal_unit, cal_note = cal_hit
        await _handle_calorie_message(
            message, target, cal_kcal, cal_unit,
            cal_note or nutrition.chat_scale_note(nut_content),
            logged_at=nut_dt,
        )
        await bot.process_commands(message)
        return

    # Saved-food shortcut: a message that's exactly one of the target's saved
    # foods ("coffee", "2 protein shake") logs that food's calories. Gated on
    # the user actually tracking + the name being defined, so normal chatter
    # falls through to the lift parser. Try the full text first so a food named
    # with a weekday/"today" word isn't mistaken for a date hint; only then the
    # hint-stripped version, which backdates it.
    food_hit = _match_calorie_food(message, target, content)
    food_dt = None
    if food_hit is None and nut_dt is not None:
        food_hit = _match_calorie_food(message, target, nut_content)
        food_dt = nut_dt
    if food_hit is not None:
        await _handle_calorie_food_message(
            message, target, *food_hit, logged_at=food_dt,
        )
        await bot.process_commands(message)
        return

    # Saved-meal shortcut: same idea as foods, but for named bundles
    # ("breakfast" = coffee + oats + shake) logged as one entry.
    meal_hit = _match_calorie_meal(message, target, content)
    meal_dt = None
    if meal_hit is None and nut_dt is not None:
        meal_hit = _match_calorie_meal(message, target, nut_content)
        meal_dt = nut_dt
    if meal_hit is not None:
        await _handle_calorie_meal_message(
            message, target, *meal_hit, logged_at=meal_dt,
        )
        await bot.process_commands(message)
        return

    # Protein chat shortcut: `40p`, `40g protein`, `protein 40`. Requires an
    # explicit protein marker so a bare number/weight never matches.
    protein_grams = protein_mod.parse_protein_chat_message(nut_content)
    if protein_grams is not None:
        await _handle_protein_message(
            message, target, protein_grams, logged_at=nut_dt,
            note=nutrition.chat_scale_note(nut_content),
        )
        await bot.process_commands(message)
        return

    # Combined log: a message carrying BOTH a calorie and a protein amount,
    # e.g. `500c and 40p`. Only fires when both tokens are present. This is
    # where per-100g posts land — a panel gives both macros, so `895kj 14.7p
    # 110g` scales the pair together off one stated serving.
    combined = nutrition.parse_combined(nut_content)
    if combined is not None:
        await _handle_combined_nutrition(
            message, target, *combined, logged_at=nut_dt,
            basis=nutrition.chat_scale_note(nut_content),
        )
        await bot.process_commands(message)
        return

    # Native cardio shortcut: the whole message must be a structured list of
    # recognised cardio activities, so normal chat that merely mentions
    # "15 minutes" is ignored. Examples:
    #   15 mins elliptical lv12, 30mins on stair master lv10
    #   15mins on treadmill 10 degrees 10 speed
    cardio_segments = cardio.parse_chat_message(nut_content)
    if cardio_segments is not None:
        await _handle_cardio_message(
            message, target, cardio_segments, logged_at=nut_dt,
        )
        await bot.process_commands(message)
        return

    # Every nutrition path missed. If the message was clearly *aiming* at a
    # food log, say so rather than falling through in silence: auto-logging is
    # all-or-nothing, and a miss is indistinguishable from a hit until the
    # day's total comes up short. Gated on the target actually tracking, and
    # on nothing word-shaped being left over, so ordinary chat is never nagged.
    hint = nutrition.near_miss(nut_content)
    # `_msg_guild_id`, not `gid`: it resolves the same storage server the
    # nutrition handlers look their goals up in, including in an ambiguous DM.
    if hint is not None and _is_nutrition_tracking(_msg_guild_id(message), target):
        try:
            await message.add_reaction("❓")
        except discord.HTTPException:
            pass
        try:
            await message.reply(f"❓ {hint}", mention_author=False)
        except discord.HTTPException:
            pass
        await bot.process_commands(message)
        return

    lifts = parse_message(content, custom_aliases=guild_aliases)
    lifts, rejected_lifts = _split_reasonable_lifts(lifts)
    # We got past every (global) nutrition path without a match. Lifts are
    # per-server, so in an ambiguous DM we can't tell which server to credit —
    # ask the user to pick one rather than guess. Nutrition already worked above.
    if dm_ambiguous and lifts and _should_auto_store(lifts):
        try:
            await message.reply(
                "I can log calories, protein and bodyweight from a DM anywhere, "
                "but **lifts** are per-server — set which one with `/server`, "
                "then re-post.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        await bot.process_commands(message)
        return
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
        guild_id = _msg_guild_id(message)
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


# A day of logs is a handful of messages; this caps the blast radius if a
# backfill or a bulk delete ever points at one. Anything past it is left alone
# and logged, rather than quietly editing a hundred messages.
_MAX_RESTATED_REPLIES = 25
_RESTATED_NOTE = "totals corrected after an earlier entry changed"


def _restated_status_lines(
    target_id: int, *, cal_total: float | None, pro_total: float | None,
    logged_at: datetime | None,
) -> list[str]:
    """The meter block for a reply, recomputed as at its own entry."""
    day_targets = _reply_targets(target_id, logged_at)
    day_label = _reply_label(
        day_targets, calories=cal_total is not None, protein=pro_total is not None,
    )
    lines: list[str] = []
    if cal_total is not None:
        lines.append(
            _calorie_status_line(
                cal_total, day_targets.kcal.value or 0.0,
                None if pro_total is not None else day_label,
            )
            + _streak_suffix(_calorie_streak(target_id))
        )
    if pro_total is not None:
        # Each macro carries its OWN streak: one streak stretched across both
        # lines would credit protein with days it hadn't earned.
        lines.append(
            _protein_status_line(
                pro_total, day_targets.protein.value or 0.0, day_label,
            )
            + _streak_suffix(_protein_streak(target_id))
        )
    return lines


def _combined_status(
    target_id: int, *, cal_total: float | None, pro_total: float | None,
    logged_at: datetime | None = None,
) -> tuple[str, discord.Colour]:
    """The meter block and card colour for a reply that logged both macros.

    The single renderer for a combined card, shared by the two paths that post
    one (a `500c and 40p` message and a saved food that carries protein) and by
    :func:`_restate_one_reply`, so a card can't be rewritten into a different
    shape than it was posted in.
    """
    lines = _restated_status_lines(
        target_id, cal_total=cal_total, pro_total=pro_total,
        logged_at=logged_at,
    )
    colours: list[discord.Colour] = []
    if cal_total is not None:
        colours.append(_calorie_status_pair(target_id, cal_total, logged_at)[1])
    if pro_total is not None:
        colours.append(_protein_status_pair(target_id, pro_total, logged_at)[1])
    return "\n".join(lines), _worst_colour(colours)


def _running_totals(rows: "list[sqlite3.Row]", column: str) -> dict[int, float]:
    """Map each entry id to the day's total *as at* that entry.

    That's what a confirmation card showed when it was posted, so it's what
    has to be recomputed to correct one. Rows arrive oldest-first.
    """
    out: dict[int, float] = {}
    running = 0.0
    for row in rows:
        running += float(row[column])
        out[int(row["id"])] = running
    return out


async def _restate_day_replies(
    target: object, *, logged_at: datetime | None = None, guild_id: int = 0,
) -> int:
    """Correct the running totals on this day's other nutrition replies.

    Each confirmation shows the day's total at the moment it was posted, so
    removing or editing an entry leaves every *later* card overstating the day
    by the amount that just went away — and those cards stay on screen,
    disagreeing with `/today` and with each other. The one the user acted on
    is already struck through by the caller; this fixes the rest.

    Best-effort throughout: the data is already correct, so a failure here
    costs an out-of-date message, never an entry. Returns how many replies were
    actually edited, for the log.
    """
    target_id = int(getattr(target, "id", 0) or 0)
    if not target_id:
        return 0
    window = _day_window_for(logged_at)
    try:
        rows = db.nutrition_replies_for_day(target_id, *window)
    except Exception:
        LOG.exception("Couldn't list nutrition replies for user %s", target_id)
        return 0
    if not rows:
        return 0
    if len(rows) > _MAX_RESTATED_REPLIES:
        LOG.warning(
            "Restating only the %d most recent of %d replies for user %s",
            _MAX_RESTATED_REPLIES, len(rows), target_id,
        )
        rows = rows[-_MAX_RESTATED_REPLIES:]

    cal_running = _running_totals(
        db.calorie_entries_between(guild_id, target_id, *window), "kcal",
    )
    pro_running = _running_totals(
        db.protein_entries_between(guild_id, target_id, *window), "grams",
    )

    edited = 0
    for row in rows:
        # A macro whose entry is gone drops off the card rather than showing a
        # stale meter — the other one is still true and still worth showing.
        cal_total = cal_running.get(int(row["calorie_id"] or 0))
        pro_total = pro_running.get(int(row["protein_id"] or 0))
        if cal_total is None and pro_total is None:
            continue
        if await _restate_one_reply(
            row, target, cal_total=cal_total, pro_total=pro_total,
            logged_at=logged_at,
        ):
            edited += 1
    if edited:
        LOG.info("Restated %d nutrition replies for user %s", edited, target_id)
    return edited


async def _restate_one_reply(
    row: "sqlite3.Row", target: object, *, cal_total: float | None,
    pro_total: float | None, logged_at: datetime | None,
) -> bool:
    """Rewrite one reply's meters in place. True when it was actually edited."""
    reply_id = int(row["reply_message_id"])
    channel = bot.get_channel(int(row["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(row["channel_id"]))
        except discord.HTTPException:
            return False
    try:
        msg = await channel.fetch_message(reply_id)
    except discord.NotFound:
        # Deleted by hand — drop the row so it can't be retried every time.
        db.forget_nutrition_reply(reply_id)
        return False
    except discord.HTTPException:
        return False
    # An already-undone reply reads as struck-through/greyed; rewriting its
    # meters would make a removed entry look live again.
    if msg.content.lstrip().startswith("~~"):
        db.forget_nutrition_reply(reply_id)
        return False

    target_id = int(getattr(target, "id"))
    try:
        if msg.embeds:
            old = msg.embeds[0]
            if cal_total is not None and pro_total is not None:
                # A combined card carries two meters; recomputing only one of
                # them would quietly drop the other off the card.
                status, colour = _combined_status(
                    target_id, cal_total=cal_total, pro_total=pro_total,
                    logged_at=logged_at,
                )
            elif cal_total is not None:
                status, colour = _calorie_status_pair(
                    target_id, cal_total, logged_at,
                )
            else:
                status, colour = _protein_status_pair(
                    target_id, pro_total or 0.0, logged_at,
                )
            # The per-100g basis / "skipped …" subtext sits under the meters and
            # is still true after a restate, so carry it back over.
            if row["footnote"]:
                status += f"\n{row['footnote']}"
            # Rebuild from the live embed so the title, note field, author and
            # milestone banner survive — only the meter and its colour move.
            embed = old.copy()
            embed.description = status
            embed.colour = colour
            embed.set_footer(text=f"{ui.EDIT} {_RESTATED_NOTE} · react ❌ to remove")
            await msg.edit(embed=embed)
        else:
            headline = row["headline"]
            if not headline:
                return False  # posted before the render cache existed
            status = _restated_status_lines(
                target_id, cal_total=cal_total, pro_total=pro_total,
                logged_at=logged_at,
            )
            body = [headline, *status]
            if row["footnote"]:
                body.append(row["footnote"])
            body.append(ui.subtext(f"{ui.EDIT} {_RESTATED_NOTE}"))
            await msg.edit(content="\n".join(body))
    except discord.HTTPException:
        return False
    return True


async def _refresh_calorie_reply(
    after: discord.Message, target: object, goal: sqlite3.Row,
    kcal: float, note: str | None,
) -> None:
    """Edit the bot's tracked reply for an edited calorie message to show the
    new amount + recomputed total. Best-effort; no-op if it wasn't tracked."""
    crec = db.get_calorie_reply_by_original(after.id)
    if crec is None:
        return
    try:
        reply_msg = await after.channel.fetch_message(int(crec["reply_message_id"]))
    except discord.HTTPException:
        return
    guild_id = after.guild.id if after.guild else 0
    target_id = int(getattr(target, "id"))
    total, _n = db.calorie_total_between(guild_id, target_id, *_today_window())
    suffix = _target_suffix(after.author, target)
    # Today's target, to match the today-scoped total just computed.
    status, colour = _calorie_status_pair(target_id, total)
    embed = _log_card(
        ui.FOOD, f"+{calories.format_kcal(kcal)}{suffix}", status, colour,
        author=target, note=_safe_label(note, limit=64) if note else None,
    )
    embed.set_footer(text=f"{ui.EDIT} updated from an edit · react ❌ to remove")
    try:
        await reply_msg.edit(content=None, embed=embed)
    except discord.HTTPException:
        pass


async def _revive_calorie_reply(
    after: discord.Message, target: object, goal: sqlite3.Row,
    kcal: float, note: str | None, *, entry_id: int,
) -> bool:
    """Re-point this message's existing reply at a newly re-created entry.

    Returns False when there's no reply to reuse, so the caller posts a fresh
    one. Used when an edit removed the entry and a later edit brought it back:
    the reply is still on screen reading "Entry removed", so it should become
    the new confirmation rather than gaining a sibling.
    """
    crec = db.get_calorie_reply_by_original(after.id)
    if crec is None:
        return False
    reply_id = int(crec["reply_message_id"])
    try:
        reply_msg = await after.channel.fetch_message(reply_id)
    except discord.HTTPException:
        # Reply deleted — drop the stale row so it can't strand a future edit.
        db.delete_calorie_reply(reply_id)
        return False

    guild_id = after.guild.id if after.guild else 0
    target_id = int(getattr(target, "id"))
    total, _n = db.calorie_total_between(guild_id, target_id, *_today_window())
    suffix = _target_suffix(after.author, target)
    status, colour = _calorie_status_pair(target_id, total)
    embed = _log_card(
        ui.FOOD, f"+{calories.format_kcal(kcal)}{suffix}", status, colour,
        author=target, note=_safe_label(note, limit=64) if note else None,
    )
    embed.set_footer(text=f"{ui.EDIT} updated from an edit · react ❌ to remove")
    try:
        await reply_msg.edit(content=None, embed=embed)
    except discord.HTTPException:
        return False
    # Re-arm the ❌ against the *new* entry id — the tracked row still names the
    # deleted one, so without this the reaction would find nothing to remove.
    db.delete_calorie_reply(reply_id)
    db.track_calorie_reply(
        reply_id, guild_id, after.author.id, target_id, entry_id, after.id,
    )
    try:
        await reply_msg.add_reaction("❌")
    except discord.HTTPException:
        pass
    return True


async def _handle_calorie_edit(
    after: discord.Message, target: object, content: str,
) -> bool:
    """Reconcile a calorie entry when its source chat message is edited.

    Returns True when the message is (or was) a calorie log so the caller skips
    the lift-edit path. Handles a unit/amount change (update — e.g. `1730c` →
    `1730kj`), the calorie text being edited away (delete), and a plain message
    becoming a calorie log (add).
    """
    guild_id = after.guild.id if after.guild else 0
    target_id = int(getattr(target, "id"))
    existing = db.get_calorie_entry_by_message(guild_id, after.id)
    new_hit = calories.parse_chat_message(content)
    if new_hit is None:
        # A combined "500c and 40p" message still carries a calorie amount —
        # treat its calorie part as the entry so an edit updates (not deletes)
        # the stored calories.
        combined = nutrition.parse_combined(content)
        if combined is not None:
            new_hit = (combined[0], "kcal", None)
    if existing is None and new_hit is None:
        return False  # not a calorie message before or after

    goal = db.calorie_goal_get(guild_id, target_id)

    # Edited the calorie text away → remove the entry + tidy the reply.
    if existing is not None and new_hit is None:
        removed = db.delete_calorie_entry(
            guild_id, int(existing["user_id"]), int(existing["id"]),
        )
        crec = db.get_calorie_reply_by_original(after.id)
        if crec is not None:
            # The tracking row stays. It's the only link back to this reply,
            # and editing the message again — to a valid amount — needs to find
            # it rather than post a second reply beneath the first. The entry it
            # points at is gone, which the ❌ handler already copes with: the
            # delete returns nothing and it says "already removed".
            try:
                rm = await after.channel.fetch_message(
                    int(crec["reply_message_id"])
                )
                # Replace the card rather than the content — the reply is an
                # embed now, and setting content alone would leave the old
                # figures sitting underneath, still coloured as if live.
                await rm.edit(content=None, embed=ui.card(
                    f"{ui.FOOD} Entry removed",
                    description="The message that logged this was edited, so "
                                "the entry went with it.",
                    colour=ui.NEUTRAL,
                ))
                await rm.clear_reaction("❌")
            except discord.HTTPException:
                pass
        try:
            await after.add_reaction("✏️")
        except discord.HTTPException:
            pass
        if crec is not None:
            # The reply now reads "Entry removed", so it must not be restated.
            db.forget_nutrition_reply(int(crec["reply_message_id"]))
        if removed is not None:
            await _restate_day_replies(
                target, logged_at=_parse_iso(removed["logged_at"]),
                guild_id=guild_id,
            )
        return True

    kcal, _unit, note = new_hit  # type: ignore[misc]
    # Same note the live path builds, so editing `895kj 14.7p 110g` keeps the
    # card showing what the figure was scaled by instead of dropping it.
    note = note or nutrition.chat_scale_note(content)
    if goal is None:
        return existing is not None  # can't log without a target
    if kcal <= 0 or _rounds_to_zero_kcal(kcal) or kcal > _MAX_ENTRY_KCAL:
        return True  # ignore implausible edits

    if existing is None:
        # A non-calorie message was edited into one → log it fresh.
        entry_id = db.calorie_add(
            guild_id, target_id, _display_name(target), kcal,
            note=note, raw=after.content.strip()[:80], message_id=after.id,
            actor_id=after.author.id,
            actor_name=_display_name(after.author),
        )
        # Reuse the reply we already posted for this message if it's still
        # around. Editing through an unparseable value and back — `1kj` → `2k`
        # → `2kj` — lands here with the first reply sitting right above,
        # already reading "Entry removed"; posting a second one leaves two bot
        # messages for one post, only one of them true.
        if await _revive_calorie_reply(
            after, target, goal, kcal, note, entry_id=entry_id,
        ):
            try:
                await after.add_reaction("✏️")
            except discord.HTTPException:
                pass
            return True
        await _reply_calorie_logged(
            after, target, goal, kcal, note, entry_id=entry_id,
        )
        return True

    # Update the existing entry when the amount/note changed.
    if (
        abs(float(existing["kcal"]) - kcal) > 1e-6
        or (existing["note"] or None) != note
    ):
        db.update_calorie_entry(
            int(existing["id"]), kcal, note, after.content.strip()[:80],
        )
        await _refresh_calorie_reply(after, target, goal, kcal, note)
        try:
            await after.add_reaction("✏️")
        except discord.HTTPException:
            pass
        # Correcting `1730c` to `1730kj` moves the day's total by ~1,300 cal,
        # so every confirmation posted after this one is now wrong too.
        await _restate_day_replies(
            target, logged_at=_parse_iso(existing["logged_at"]),
            guild_id=guild_id,
        )
    return True


@bot.event
async def on_message_edit(
    before: discord.Message, after: discord.Message,
) -> None:
    """Re-parse edited gym posts so corrections flow into the DB."""
    # Keep the message log faithful to the current message: reflect edited text
    # and any media added/swapped by the edit. Runs for every author (bots too)
    # and channel, before the gym-post gates below, mirroring on_message's logger.
    if ENABLE_MESSAGE_LOGGING and after.guild is not None:
        try:
            media = await _message_media(after)
            # Ensure a row exists (edits can predate logging), then overwrite it.
            db.message_log_add(
                after.guild.id, after.author.id, after.content,
                channel_id=after.channel.id,
                channel_name=getattr(after.channel, "name", None),
                message_id=after.id, at=after.created_at, attachments=media,
            )
            db.message_log_update_content(
                after.guild.id, after.id, after.content, media,
                edited_at=after.edited_at,
            )
        except Exception:
            LOG.exception("Failed to log message edit for activity feed")
    if after.author.bot or not after.guild:
        return
    if GYM_CHANNEL_IDS and after.channel.id not in GYM_CHANNEL_IDS:
        return
    if before.content == after.content:
        return  # ignore embed/attachment-only edits for the lift re-parse

    guild_id = after.guild.id
    # Blacklisted members can't add anything to the bot — including by *editing*
    # a message into a lift/calorie (mirrors the on_message gate).
    if db.message_is_blacklisted(guild_id, after.author.id):
        LOG.info(
            "Blacklist: ignored edit from %s in guild %s",
            after.author.id, guild_id,
        )
        return
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
    # ...and not *for* a blacklisted member either.
    if (
        target_user_id != after.author.id
        and db.message_is_blacklisted(guild_id, target_user_id)
    ):
        LOG.info(
            "Blacklist: ignored edit targeting %s in guild %s",
            target_user_id, guild_id,
        )
        return
    # Editing a post is a fresh signal of intent — clear any prior
    # backfill suppression so the corrected version can be re-imported.
    db.unsuppress_message(guild_id, after.id)
    # Calorie logs reconcile separately (units/totals), before the lift path.
    if await _handle_calorie_edit(after, target, content):
        return
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


# A ❌ reaction is matched to one of our nutrition replies by its leading glyph,
# so this tuple is a *wire format*, not decoration: every reply already sitting
# in Discord's history must keep matching or its undo silently stops working.
# 🍽️ and 🥗 are retired from new replies (one food icon, 🍎) but stay here
# permanently for the ~800 replies posted before that change. 🤖 covers the
# AI-estimate replies (`~...` and /estimate).
_NUTRITION_REPLY_PREFIXES = (
    ui.FOOD, ui.PROTEIN, ui.AI,   # current
    "🍽️", "🥗",                   # legacy — do not remove
)


def _is_nutrition_reply(msg: "discord.Message") -> bool:
    """True when *msg* is one of our nutrition confirmations.

    Checks the embed title as well as the content: the chat replies are cards
    now, and an embed-only message has an EMPTY ``content``, so a
    content-only test would silently stop matching every new reply — and ❌
    would quietly do nothing on all of them. Plain-text replies (the ones
    already in history, and the slash-command ones) still match on content.
    """
    if msg.content.startswith(_NUTRITION_REPLY_PREFIXES):
        return True
    for embed in msg.embeds or ():
        if (embed.title or "").startswith(_NUTRITION_REPLY_PREFIXES):
            return True
    return False


async def _handle_nutrition_reaction_undo(
    payload: discord.RawReactionActionEvent,
) -> None:
    """Remove the calorie and/or protein entries a chat message logged when its
    logger/target/admin reacts ❌ on the bot's reply.

    Covers calorie (🍎), protein (🥩) and combined (🍎🥩) replies. Tracked
    single-calorie replies use their tracking row; the rest (protein, combined,
    legacy) resolve via the reply's referenced source message and remove every
    nutrition entry tied to it.
    """
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
    # Only act on our own nutrition-log replies (skip lift replies and
    # already-undone ones whose text now starts with "~~").
    if reply_msg.author.id != (bot.user.id if bot.user else 0):
        return
    if not _is_nutrition_reply(reply_msg):
        return

    guild_id = payload.guild_id or 0
    logger_id: int | None = None
    removed_bits: list[str] = []
    removed_at: datetime | None = None    # the day whose later cards go stale

    crec = db.get_calorie_reply(payload.message_id)
    if crec is not None:
        # Tracked single-calorie reply.
        target_user_id = int(crec["target_user_id"])
        logger_id = int(crec["user_id"])
        if payload.user_id not in ({logger_id, target_user_id} | ADMIN_USER_IDS):
            return
        if db.delete_calorie_reply(payload.message_id) == 0:
            return  # race: another ❌ already claimed it
        original_id = crec["original_message_id"]
        r = db.delete_calorie_entry(
            guild_id, target_user_id, int(crec["calorie_id"]),
            actor_id=payload.user_id,
        )
        if r is not None:
            removed_bits.append(calories.format_kcal(float(r["kcal"])))
            removed_at = _parse_iso(r["logged_at"])
    else:
        # Protein / combined / legacy: resolve via the referenced message and
        # remove every nutrition entry it created. Chat replies point back to
        # the source message; a slash-command followup (/estimate) has
        # no reference, so fall back to the reply's own id — its entries are
        # linked directly to it.
        ref = reply_msg.reference
        original_id = (ref.message_id if ref else None) or payload.message_id
        cal_entry = db.get_calorie_entry_by_message(guild_id, int(original_id))
        pro_entry = db.get_protein_entry_by_message(guild_id, int(original_id))
        if cal_entry is None and pro_entry is None:
            return
        target_user_id = int((cal_entry or pro_entry)["user_id"])
        if payload.user_id not in ({target_user_id} | ADMIN_USER_IDS):
            try:
                original = await channel.fetch_message(int(original_id))
                logger_id = original.author.id
            except discord.HTTPException:
                logger_id = None
            if payload.user_id != logger_id:
                return
        if cal_entry is not None:
            r = db.delete_calorie_entry(
                guild_id, target_user_id, int(cal_entry["id"]),
                actor_id=payload.user_id,
            )
            if r is not None:
                removed_bits.append(calories.format_kcal(float(r["kcal"])))
                removed_at = _parse_iso(r["logged_at"])
        if pro_entry is not None:
            r = db.delete_protein_entry(
                guild_id, target_user_id, int(pro_entry["id"]),
                actor_id=payload.user_id,
            )
            if r is not None:
                removed_bits.append(
                    f"{protein_mod.format_grams(float(r['grams']))} protein"
                )
                removed_at = removed_at or _parse_iso(r["logged_at"])

    # Suppress the source message so a restart backfill won't re-import it.
    if original_id is not None:
        db.suppress_message(guild_id, int(original_id))

    by_admin = (
        payload.user_id in ADMIN_USER_IDS
        and payload.user_id != target_user_id
        and payload.user_id != logger_id
    )
    actor = "an admin" if by_admin else "the user"
    if removed_bits:
        note = (
            f"↩️ Removed **{' + '.join(removed_bits)}** at {actor}'s request."
        )
    else:
        note = "↩️ Nothing to undo (already removed)."
    # Strike through the original log so the edited message clearly reads as
    # "removed", then append the confirmation note. An embed reply is greyed
    # and restated instead — striking markdown inside a card leaves the tiles
    # and colour looking live, which reads as though nothing happened.
    try:
        if reply_msg.embeds:
            old = reply_msg.embeds[0]
            await reply_msg.edit(embed=ui.card(
                old.title, description=note, colour=ui.NEUTRAL,
            ))
        else:
            struck = "\n".join(
                f"~~{line}~~" if line.strip() else line
                for line in reply_msg.content.split("\n")
            )
            await reply_msg.edit(content=f"{struck}\n\n{note}")
    except discord.HTTPException:
        pass
    # Clear the ❌ affordance now that it's done (best-effort).
    try:
        await reply_msg.clear_reaction("❌")
    except discord.HTTPException:
        try:
            await reply_msg.remove_reaction("❌", bot.user)  # type: ignore[arg-type]
        except discord.HTTPException:
            pass
    # This reply is now struck through, so it can't be restated later.
    db.forget_nutrition_reply(payload.message_id)
    # Every *later* confirmation from the same day was printed with this entry
    # still in the total, so each one now overstates the day by the amount just
    # removed. Correct them rather than leaving a trail of numbers that
    # disagree with /today and with each other.
    if removed_bits:
        await _restate_day_replies(
            discord.Object(id=target_user_id),
            logged_at=removed_at, guild_id=guild_id,
        )
    # Drop the ✅ on the user's original message so the visual state matches.
    if original_id:
        try:
            original = await channel.fetch_message(int(original_id))
            await original.remove_reaction("✅", bot.user)  # type: ignore[arg-type]
        except discord.HTTPException:
            pass


@bot.event
async def on_raw_message_delete(
    payload: discord.RawMessageDeleteEvent,
) -> None:  # pragma: no cover - discord runtime
    """Flag a deleted message in the log instead of dropping it.

    Uses the *raw* event so it fires even for messages that aren't in the
    gateway cache (e.g. older posts). The content and any downloaded media are
    kept — we only stamp ``deleted_at`` so the dashboard can show it was removed.
    """
    if not ENABLE_MESSAGE_LOGGING or payload.guild_id is None:
        return
    try:
        db.message_log_mark_deleted(payload.guild_id, payload.message_id)
    except Exception:
        LOG.exception("Failed to flag deleted message in log")


@bot.event
async def on_raw_bulk_message_delete(
    payload: discord.RawBulkMessageDeleteEvent,
) -> None:  # pragma: no cover - discord runtime
    """Flag a bulk deletion (channel purge / moderation sweep) in the log."""
    if not ENABLE_MESSAGE_LOGGING or payload.guild_id is None:
        return
    for mid in payload.message_ids:
        try:
            db.message_log_mark_deleted(payload.guild_id, mid)
        except Exception:
            LOG.exception("Failed to flag bulk-deleted message in log")


async def _handle_cardio_reaction_undo(
    payload: discord.RawReactionActionEvent,
) -> bool:
    """Undo a passively logged cardio session from its confirmation reply."""
    rec = db.cardio_get_reply(payload.message_id)
    if rec is None:
        return False
    target_user_id = int(rec["target_user_id"])
    allowed = {
        int(rec["logger_user_id"]), target_user_id,
    } | ADMIN_USER_IDS
    if payload.user_id not in allowed:
        return True
    if not db.cardio_delete_reply(payload.message_id):
        return True
    removed = db.cardio_session_remove(
        target_user_id, int(rec["session_id"]),
    )
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return True
    try:
        reply = await channel.fetch_message(payload.message_id)
        await reply.edit(
            content=None,
            embed=ui.card(
                "🏃 Cardio removed",
                description=(
                    "The tracked session was removed."
                    if removed else "That session was already removed."
                ),
                colour=ui.NEUTRAL,
            ),
        )
        await reply.clear_reaction("❌")
    except discord.HTTPException:
        pass
    original_id = rec["original_message_id"]
    if original_id is not None:
        try:
            original = await channel.fetch_message(int(original_id))
            await original.remove_reaction("✅", bot.user)  # type: ignore[arg-type]
        except discord.HTTPException:
            pass
    return True


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
        # Not a lift reply — maybe cardio, a Home Assistant weigh-in, or a
        # calorie reply.
        if await _handle_cardio_reaction_undo(payload):
            return
        # Weigh-ins go first because they are identified by a tracking row or by
        # their own embed footer, so they can decline cleanly; the nutrition
        # handler resolves via the reply's *referenced* message and has no such
        # signal to bow out on.
        if await _handle_ha_reaction_undo(payload):
            return
        await _handle_nutrition_reaction_undo(payload)
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
        removed = db.delete_lifts_by_ids(
            guild_id, target_user_id, ids, actor_id=payload.user_id,
        )
    elif rec["message_id"] is not None:
        removed = db.delete_lifts_for_message(
            guild_id, target_user_id, rec["message_id"],
            actor_id=payload.user_id,
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


def _nothing_logged_embed(
    target: object, viewer: object, macro: str, *, how: str,
) -> discord.Embed:
    """"Nothing logged this week", addressed to whoever is actually reading.

    Looking up your own week gets the how-to; looking up someone else's gets a
    plain statement about them, because "post an amount in chat" is advice the
    reader cannot act on for another member.
    """
    if int(getattr(target, "id", 0)) == int(getattr(viewer, "id", -1)):
        return ui.empty(
            f"You haven't logged any {macro} this week",
            hint=f"Log some and it'll show up here — {how}.",
            cmd="/today shows a single day",
        )
    return ui.empty(
        f"{_safe_label(_display_name(target), limit=32)} hasn't logged any "
        f"{macro} this week",
        hint="Nothing to chart yet.",
    )


def _no_lifts_embed(name: str) -> discord.Embed:
    """The one empty state six commands were each spelling out for themselves."""
    return ui.empty(
        f"No lifts logged for {_safe_label(name, limit=32)} yet",
        hint="Post a session in chat — e.g. `bench press 80kg` — and I'll "
             "store it automatically. No command needed.",
        cmd="/log adds one by hand",
    )


def _no_history_embed(equipment: str, name: str) -> discord.Embed:
    """Empty state for the per-lift views (/history, /progress, /overview).

    Names the lift back, because the usual cause is a spelling the alias table
    doesn't know — not an absence of training.
    """
    eq = _safe_label(equipment)
    return ui.empty(
        f"No {eq} history for {_safe_label(name, limit=32)}",
        hint=f"Either it hasn't been logged yet, or I know it by another "
             f"name — `/aliases {eq}` shows the spellings I accept.",
        cmd="/equipment_list shows every lift I track",
    )


def _personal_bests_card(
    guild_id: int, target: object, rows: "list[sqlite3.Row]",
) -> discord.Embed:
    """One member's personal bests as a card.

    Shared by `/stats` and the "Gym stats" context menu, which were two
    byte-identical copies that differed only in ephemerality. The weights sit
    in a fenced table because a proportional font gives a ragged right edge on
    a column of numbers, which is the one thing this card exists to show.
    """
    bw = _user_bodyweight(guild_id, int(getattr(target, "id")))
    table_rows = []
    for r in rows:
        eq = r["equipment"]
        true_suffix = _true_weight_suffix(eq, float(r["best"]), bool(r["bw"]), bw)
        table_rows.append([
            eq[:20],
            _format_weight(r["best"], bool(r["bw"])),
            true_suffix.strip().removeprefix("(").removesuffix(")"),
        ])
    embed = ui.card(
        f"{ui.CHART} Personal bests",
        description=ui.table(table_rows, align="<>", max_rows=25),
        colour=ui.BRAND,
        member=target,
        footer=ui.plural(len(rows), "exercise"),
        timestamp=True,
    )
    newest = max((r["set_on"] for r in rows if r["set_on"]), default=None)
    if newest:
        ui.block(embed, "Most recent PR", f"{ui.when(newest)}")
    return embed


@bot.tree.command(name="stats", description="Show a user's personal bests.")
@app_commands.describe(user="The user to look up (defaults to you).")
async def stats_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    rows = db.personal_bests(guild_id, target.id)
    if not rows:
        await interaction.response.send_message(
            embed=_no_lifts_embed(target.display_name), ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=_personal_bests_card(guild_id, target, rows),
    )


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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.progress(guild_id, target.id, canon)
    if not rows:
        await interaction.response.send_message(
            embed=_no_history_embed(canon, target.display_name), ephemeral=True,
        )
        return

    bests = [float(r["best"]) for r in rows]
    net = bests[-1] - bests[0]
    table_rows = []
    prev: float | None = None
    for r in rows:
        best = float(r["best"])
        table_rows.append([
            r["month"],
            _format_weight(best, bool(r["bw"])),
            (f"{'+' if best > prev else ''}{best - prev:g}"
             if prev is not None and best != prev else ""),
        ])
        prev = best

    embed = ui.card(
        f"{ui.CHART} {_safe_label(canon)} — by month",
        # The shape of the trend first, the numbers second. The old form led
        # with an audit date in parentheses and buried the weight behind it.
        description=f"`{ui.sparkline(bests)}`\n"
                    + ui.subtext(
                        f"{ui.kg(min(bests))} low · {ui.kg(max(bests))} high"
                    ),
        colour=ui.score_trend(net),
        member=target,
        footer=ui.plural(len(rows), "month"),
        timestamp=True,
    )
    ui.tiles(
        embed,
        ("Now", f"**{_format_weight(bests[-1], bool(rows[-1]['bw']))}**"),
        ("Best", f"**{_format_weight(max(bests), bool(rows[-1]['bw']))}**"),
        ("Net", ui.delta(net)),
    )
    ui.block(embed, "Month by month", ui.table(
        table_rows, align="<>>", headers=["month", "best", "chg"], max_rows=18,
    ))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top lifters for an equipment.")
@app_commands.describe(equipment="Equipment / lift name")
@app_commands.autocomplete(equipment=_equipment_autocomplete)
async def leaderboard_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.leaderboard(guild_id, canon)
    if not rows:
        await interaction.response.send_message(
            embed=ui.empty(
                f"Nobody has logged {_safe_label(canon)} yet",
                hint=f"Post `{_safe_label(canon)} 60kg` in chat to open the board.",
                cmd="/equipment_list shows what I know about",
            ),
            ephemeral=True,
        )
        return

    # Pull every lifter's most recent bodyweight in one query so we can show
    # the *true* load on bodyweight-relative lifts (assisted pull-ups, etc.).
    user_ids = [int(r["user_id"]) for r in rows]
    bw_map = db.latest_bodyweights_bulk(guild_id, user_ids)

    def _weight(r) -> str:
        return _format_weight(r["best"], bool(r["bw"]))

    def _true(r) -> str:
        return _true_weight_suffix(
            canon, float(r["best"]), bool(r["bw"]), bw_map.get(int(r["user_id"])),
        ).strip().removeprefix("(").removesuffix(")")

    leader = _ctx_guild(interaction)
    leader_member = (
        leader.get_member(int(rows[0]["user_id"])) if leader else None
    )
    embed = ui.card(
        f"{ui.TROPHY} {_safe_label(canon)} leaderboard",
        colour=ui.BRAND,
        subject=leader_member,
        footer=f"{ui.plural(len(rows), 'lifter')} · true load shown where a "
               "bodyweight is on file",
        timestamp=True,
    )
    # Podium as three tiles, then the rest as one fenced table. The medals stay
    # out of the fence deliberately: an emoji is not one monospace cell, so
    # mixing 🥇 with " 4." is what made the old name column jump at rank four.
    ui.tiles(embed, *[
        (
            f"{ui.rank(i)} {_safe_label(r['username'], limit=20)}",
            f"**{_weight(r)}**"
            + (f"\n{ui.subtext(_true(r))}" if _true(r) else "")
            + f"\n{ui.when(r['set_on'])}",
        )
        for i, r in enumerate(rows[:3])
    ])
    if len(rows) > 3:
        top = float(rows[0]["best"])
        ui.block(embed, "The chase", ui.table(
            [
                [
                    ui.rank(i, mono=True),
                    _safe_label(r["username"], limit=16),
                    _weight(r),
                    ui.bar(float(r["best"]), top, width=8),
                ]
                for i, r in enumerate(rows[3:], start=3)
            ],
            align="<<>",
            max_rows=22,
        ))
    await interaction.response.send_message(embed=embed)


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
    user: discord.Member | discord.User | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    target = user or interaction.user
    # If no value supplied, just report the latest entry. Useful for sanity
    # checking what the bot is using to compute true weights.
    if weight_kg is None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if await _deny_invisible_target(interaction, target):
            return
        row = db.get_latest_bodyweight(guild_id, target.id)
        if row is None:
            await interaction.followup.send(
                f"No bodyweight on file for **{_display_name(target)}** yet. "
                "Use `/bodyweight weight_kg:<kg>` to record one — it will be "
                "used to show the true load on pull-ups, dips, and other "
                "bodyweight-relative lifts.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"**{_display_name(target)}**'s bodyweight: "
            f"**{float(row['weight_kg']):g}kg** "
            f"(updated {_format_date(row['recorded_at'])}).",
            ephemeral=True,
        )
        return

    # Validate before deferring so obvious input errors remain instant. The
    # successful path may wait on SQLite and a cold Matplotlib import.
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

    await interaction.response.defer(thinking=True)
    if await _deny_invisible_target(interaction, target):
        return
    protein_grams = db.set_bodyweight(
        guild_id, target.id, weight_kg,
        actor_id=interaction.user.id,
        actor_name=_display_name(interaction.user),
    )
    suffix = _target_suffix(interaction.user, target)
    protein_line = (
        f"\n🥩 Protein max updated to {protein_grams} g (tied to bodyweight)."
        if protein_grams is not None else ""
    )
    chart = await _updated_bodyweight_chart(
        target.id, _display_name(target),
    )
    attachment = (
        {"file": _bodyweight_chart_file(chart)} if chart is not None else {}
    )
    reply_text = (
        f"Recorded bodyweight **{weight_kg:g}kg**{suffix}. The bot will now "
        "show your true load on bodyweight-relative lifts (e.g. assisted "
        "pull-ups, weighted dips)."
        f"{protein_line}"
    )
    try:
        await interaction.followup.send(reply_text, **attachment)
    except discord.HTTPException as exc:
        if chart is None or not _attachment_retryable(exc):
            raise
        await interaction.followup.send(reply_text)


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

    guild_id = _ctx_guild_id(interaction)
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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
        actor_id=interaction.user.id,
        actor_name=_display_name(interaction.user),
    )
    if inserted_ids:
        # If this is a brand-new equipment name (not in the built-in alias
        # table and not already a custom alias), teach the parser about it
        # so future free-form messages like "<name> 60kg" auto-log.
        alias_key = normalize_token(equipment)
        if (
            alias_key
            and alias_key not in _BUILTIN_KNOWN_PHRASES
            and not db.alias_resolve(guild_id, alias_key)
        ):
            try:
                db.alias_set(guild_id, alias_key, canon, interaction.user.id)
            except Exception:  # pragma: no cover - defensive, never block /log
                LOG.exception("Failed to auto-register alias for %s", equipment)
        suffix = _target_suffix(interaction.user, target)
        target_bw = _user_bodyweight(guild_id, target.id)
        true_suf = _true_weight_suffix(canon, weight_kg, bodyweight, target_bw)
        backdate = ""
        if date:
            used_local = logged_at.astimezone(DISPLAY_TZ).date()
            backdate = f" · logged for {used_local.strftime('%Y-%m-%d')}"
        is_pr = weight_kg > 0 and (prev is None or weight_kg > prev)
        # Goal hit check — uses the same semantics as auto-parse.
        goal = db.goal_get(guild_id, target.id, canon)
        goal_hit = bool(goal and weight_kg >= goal["target_kg"])
        if goal_hit:
            db.goal_remove(guild_id, target.id, canon)

        undo_note = (
            "React ❌ or use /undo if this was a mistake — logger or lifter"
        )
        if not (is_pr or goal_hit):
            # A routine entry is a receipt: cheap, one line, no card. Only the
            # moments worth celebrating get promoted to an embed.
            await interaction.response.send_message(
                f"{ui.OK} Logged **{_safe_label(canon)}** "
                f"{_format_weight(weight_kg, bodyweight)}{true_suf}{suffix}"
                f"{backdate}\n{ui.subtext(undo_note)}"
            )
        else:
            # Gold outranks green: clearing a goal you set beats beating a
            # number you happened to be at.
            embed = ui.card(
                f"{ui.GOAL} Goal hit — {_safe_label(canon)}" if goal_hit
                else f"{ui.PARTY} New PR — {_safe_label(canon)}",
                colour=ui.GOLD if goal_hit else ui.SUCCESS,
                member=target,
                footer=undo_note,
                timestamp=True,
            )
            if prev is None:
                embed.description = (
                    f"**{_format_weight(weight_kg, bodyweight)}**{true_suf}\n"
                    f"{ui.subtext('first entry for this lift')}"
                )
            else:
                embed.description = (
                    f"`{ui.bar(prev, weight_kg, width=12)}` "
                    + ui.arrow(
                        _format_weight(prev, bodyweight),
                        _format_weight(weight_kg, bodyweight),
                    )
                    + f"\n{ui.subtext(ui.delta(weight_kg - prev))}"
                )
            if goal_hit:
                ui.block(
                    embed, "Target cleared",
                    f"{_format_weight(goal['target_kg'], bool(goal['bw']))} "
                    "— goal removed, set the next one with `/goal_set`",
                )
            if backdate:
                ui.block(embed, "Backdated", backdate.lstrip(" ·"))
            await interaction.response.send_message(embed=embed)
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon)
    if not rows:
        await interaction.response.send_message(
            embed=_no_history_embed(canon, target.display_name), ephemeral=True,
        )
        return

    # Five independent quantities per entry — date, weight, reps, change,
    # estimated 1RM — so they go in real columns rather than a run-on line
    # built from four optional suffix variables.
    table_rows = []
    prev: float | None = None
    has_reps = False
    for r in rows:
        w = float(r["weight_kg"])
        reps = r["reps"] if "reps" in r.keys() else None
        one_rm = estimated_one_rep_max(w, reps) if reps else None
        if reps:
            has_reps = True
        table_rows.append([
            _format_date(r["logged_at"]),
            _format_weight(w, bool(r["bw"])),
            f"x{reps}" if reps else "",
            (f"{'+' if w > prev else ''}{w - prev:g}"
             if prev is not None and w != prev else ""),
            f"{one_rm:g}" if one_rm else "",
        ])
        prev = w

    first, last = float(rows[0]["weight_kg"]), float(rows[-1]["weight_kg"])
    headers = ["date", "weight", "reps", "chg", "e1RM"]
    if not has_reps:
        headers = headers[:2] + ["", "chg", ""]
    embed = ui.card(
        f"{ui.LIFT} {_safe_label(canon)} — timeline",
        description=ui.table(
            table_rows, align="<>><>", headers=headers, max_rows=25,
        ),
        colour=ui.score_trend(last - first),
        member=target,
        footer=f"{ui.plural(len(rows), 'entry', 'entries')} · most recent last"
               + ("" if has_reps else " · add `x8` to a post to track e1RM"),
        timestamp=True,
    )
    ui.tiles(
        embed,
        ("Latest", f"**{_format_weight(last, bool(rows[-1]['bw']))}**"),
        ("Best", f"**{_format_weight(max(float(r['weight_kg']) for r in rows), bool(rows[-1]['bw']))}**"),
        ("Change", ui.delta(last - first)),
    )
    await interaction.response.send_message(embed=embed)


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
        custom_aliases=_custom_alias_map(_ctx_guild_id(interaction)),
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
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.machine_history(guild_id, canon)
    if not rows:
        await interaction.response.send_message(
            embed=ui.empty(
                f"Nothing logged for {_safe_label(canon)} yet",
                hint=f"Post `{_safe_label(canon)} 60kg` in chat and it starts "
                     "tracking from there.",
                cmd="/equipment_list shows every lift I know",
            ),
            ephemeral=True,
        )
        return

    # Track each user's previous weight so we can show deltas per person.
    lines: list[str] = []
    last_by_user: dict[str, float] = {}
    for r in rows:
        user = r["username"]
        w = r["weight_kg"]
        change = ""
        prev = last_by_user.get(user)
        if prev is not None and w != prev:
            change = f"  ({'+' if w > prev else ''}{w - prev:g}kg)"
        last_by_user[user] = w
        lines.append(
            f"• {_format_date(r['logged_at'])} — **{_safe_label(user, limit=24)}**: "
            f"{_format_weight(w, bool(r['bw']))}{change}"
        )
    # An embed description holds 4 096 characters against a message's 2 000, and
    # ui.card clips rather than letting an over-length send 400 — this command
    # used to build an unbounded string and simply fail on a popular lift.
    embed = ui.card(
        f"{ui.LIFT} {_safe_label(canon)} — timeline",
        description="\n".join(lines),
        colour=ui.BRAND,
        footer=f"{ui.plural(len(rows), 'entry', 'entries')} · most recent last",
        timestamp=True,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="sync",
    description="(Owner) Force a re-sync of the bot's slash commands with Discord.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def sync_cmd(interaction: discord.Interaction) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            embed=ui.denied("Owner only — this manages the bot itself."),
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        result = await _sync_commands(force=True)
    except Exception as exc:  # pragma: no cover - discord runtime
        LOG.exception("Manual /sync failed")
        await interaction.followup.send(
            f"❌ Sync failed: `{exc}`", ephemeral=True,
        )
        return
    if result["action"] == "guild":
        where = "to this guild only (COMMAND_SCOPE=guild) — visible immediately"
    else:
        where = "globally — can take up to ~1h to appear in every server"
    await interaction.followup.send(
        f"✅ Synced **{result['count']}** commands {where}. "
        "If they don't show, hard-refresh Discord (Ctrl+R).",
        ephemeral=True,
    )


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


def _guild_name(guild_id: int) -> str:
    """Best-effort display name for a guild id (live object, then DB mirror)."""
    g = bot.get_guild(guild_id)
    if g is not None:
        return g.name
    for row in db.list_guilds():
        if int(row["guild_id"]) == guild_id and row["name"]:
            return row["name"]
    return f"Server {guild_id}"


async def _server_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Offer the servers the caller shares with the bot for /server."""
    current = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for gid in _shared_guild_ids(interaction.user.id):
        name = _guild_name(gid)
        if current and current not in name.lower() and current not in str(gid):
            continue
        out.append(app_commands.Choice(name=name[:100], value=str(gid)))
        if len(out) >= 25:
            break
    return out


@bot.tree.command(
    name="server",
    description="Pick which server your DM commands use (when you share several with me).",
)
@app_commands.describe(
    server="The server to use by default in DMs. Leave empty to see your current choice.",
)
@app_commands.autocomplete(server=_server_autocomplete)
async def server_cmd(
    interaction: discord.Interaction, server: str | None = None,
) -> None:
    shared = _shared_guild_ids(interaction.user.id)
    if not shared:
        await interaction.response.send_message(
            "I don't share any servers with you yet, so there's nothing to set. "
            "Add me to a server (or join one I'm in) first.",
            ephemeral=True,
        )
        return

    # No argument: report the current default and the available servers.
    if server is None:
        stored = db.dm_guild_get(interaction.user.id)
        effective = _effective_guild_for_dm(interaction.user.id)
        lines = ["**Your DM server**"]
        if stored is not None and stored in shared:
            lines.append(f"• Default: **{_guild_name(stored)}**")
        elif effective is not None:
            lines.append(
                f"• Default: **{_guild_name(effective)}** "
                "_(auto — the only server we share)_"
            )
        else:
            lines.append(
                "• Default: _none set_ — pick one below so DM commands know "
                "which server to use."
            )
        lines.append("")
        lines.append("**Servers we share:**")
        for gid in shared:
            lines.append(f"• {_guild_name(gid)} (`{gid}`)")
        lines.append("")
        lines.append("Run `/server server:<name>` to set your default.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    # Setting: validate the choice is a server we actually share.
    if not server.isdigit() or int(server) not in shared:
        await interaction.response.send_message(
            "Pick one of the servers we share (use the autocomplete).",
            ephemeral=True,
        )
        return
    gid = int(server)
    db.dm_guild_set(interaction.user.id, gid)
    await interaction.response.send_message(
        f"✅ Your DM commands will now use **{_guild_name(gid)}**. "
        "Change it any time with `/server`.",
        ephemeral=True,
    )


@bot.tree.command(
    name="backfill",
    description="Rescan this channel's recent history for missed lifts and logs.",
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
        # No `since`, so this walks back from the newest message — "rescan the
        # last N" is what someone typing a limit means.
        result = await _backfill_channel(interaction.channel, lim, react=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.",
            ephemeral=True,
        )
        return
    cal_part = (
        f", {result.entries} new nutrition entries" if result.entries else ""
    )
    await interaction.followup.send(
        f"Backfill complete — scanned {result.scanned} messages, "
        f"{result.posts_with_lifts} had lifts, {result.lifts} new lifts "
        f"stored, {result.suppressed} skipped (suppressed){cal_part}.",
        ephemeral=True,
    )
    # A manual rescan already reports back to whoever ran it, so it doesn't
    # also announce itself to the channel.


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
            embed=ui.denied(
                "Admins only — this rescans and rewrites stored lifts.",
                allowed="Your own entries are yours: `/undo` removes the last "
                        "one, `/delete_entry` removes a specific day.",
            ),
            ephemeral=True,
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

    guild_id = _ctx_guild_id(interaction)
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
            embed=ui.denied(
                "Admins only — suppressing a message hides it from every "
                "member's stats, not just yours.",
                allowed="React ❌ on my reply to undo something you logged.",
            ),
            ephemeral=True,
        )
        return
    if not message_id.isdigit():
        await interaction.response.send_message(
            "message_id must be a numeric Discord message ID.",
            ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
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


# --- Context menus (right-click / long-press actions) ----------------------
# Native Discord actions so the common "act on someone's already-typed message"
# flows work with no message-ID copying (which on mobile needs Developer Mode).
# Thin wrappers over the same handlers as /parse, /suppress_message and /stats.


@bot.tree.context_menu(name="Log lifts from this")
async def ctx_log_lifts(
    interaction: discord.Interaction, message: discord.Message,
) -> None:
    """Parse and store the lifts in the right-clicked message (honours a leading
    @mention target), the way /parse does — but without typing a message ID."""
    target, content = _message_lift_target(message)
    lifts = parse_message(
        content, custom_aliases=_custom_alias_map(_ctx_guild_id(interaction)),
    )
    lifts, rejected_lifts = _split_reasonable_lifts(lifts)
    if not lifts:
        note = _rejected_lifts_note(rejected_lifts).lstrip()
        await interaction.response.send_message(
            note or "No lifts detected in that message.", ephemeral=True,
        )
        return
    inserted = await _store_lifts(
        message, lifts, target,
        logged_at=_resolve_date_hint(
            content, message.created_at.astimezone(DISPLAY_TZ),
        ),
    )
    lines = [
        f"Stored {_plural(inserted, 'new lift')} for {_display_name(target)}:"
    ]
    lines.extend(_format_lift_lines(lifts))
    note = _rejected_lifts_note(rejected_lifts)
    if note:
        lines.append(note)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.context_menu(name="Ignore (not a lift)")
async def ctx_suppress_message(
    interaction: discord.Interaction, message: discord.Message,
) -> None:
    """Admin: mark a message 'do not import' and remove any lifts it created —
    the /suppress_message action, one tap on the offending message."""
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied(
                "Admins only — this removes the message's lifts for everyone.",
                allowed="React ❌ on my reply to undo something you logged.",
            ),
            ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    removed = db.delete_lifts_for_message_any_user(guild_id, message.id)
    already = db.is_message_suppressed(guild_id, message.id)
    db.suppress_message(guild_id, message.id)
    await interaction.response.send_message(
        f"Won't import that message. Lifts removed: {removed}."
        + ("" if not already else " (was already suppressed)"),
        ephemeral=True,
    )


@bot.tree.context_menu(name="Gym stats")
async def ctx_gym_stats(
    interaction: discord.Interaction, member: discord.Member,
) -> None:
    """Show a member's personal bests — the /stats action, with no typing, for
    the members who've never run a slash command."""
    if await _deny_invisible_target(interaction, member):
        return
    guild_id = _ctx_guild_id(interaction)
    rows = db.personal_bests(guild_id, member.id)
    if not rows:
        await interaction.response.send_message(
            embed=_no_lifts_embed(member.display_name), ephemeral=True,
        )
        return
    # Same card as /stats, kept ephemeral: this fires from a right-click on
    # someone else's message, so it shouldn't post about them in the channel.
    await interaction.response.send_message(
        embed=_personal_bests_card(guild_id, member, rows), ephemeral=True,
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
            embed=ui.denied(
                "Owner only — this exports raw data for the whole server.",
            ),
            ephemeral=True,
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
            embed=ui.denied(
                "Owner only — this exports raw data for the whole server.",
            ),
            ephemeral=True,
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
    # /purge wipes a lift name for the WHOLE guild — admins only, and every
    # run is written to the audit log (see db.delete_equipment).
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied(
                "Admins only — /purge deletes a lift name for the whole server.",
                allowed="`/undo` removes your last entry; `/delete_entry` "
                        "removes one of your days.",
            ),
            ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
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
    n = db.delete_equipment(
        guild_id, canon,
        actor_id=interaction.user.id, actor_name=interaction.user.display_name,
    )
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

    # Renaming someone else's rows or the whole guild is an admin action;
    # renaming your own is always allowed. Every rename is audited.
    rewrites_others = user is not None or scope_value == "all"
    if rewrites_others and interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied(
                "Admins only — renaming another member's or the whole "
                "server's rows.",
                allowed="You can always rename your own — leave `user` and "
                        "`scope` unset.",
            ),
            ephemeral=True,
        )
        return

    guild_id = _ctx_guild_id(interaction)
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

    n = db.rename_equipment(
        guild_id, src, dst, user_id=target_user_id,
        actor_id=interaction.user.id, actor_name=interaction.user.display_name,
    )
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
    # Deleting another member's entries is an admin action; your own is open.
    if target.id != interaction.user.id and interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied(
                "Admins only — deleting another member's entries.",
                allowed="You can always delete your own — leave `user` unset.",
            ),
            ephemeral=True,
        )
        return
    if await _deny_invisible_target(interaction, target):
        return
    canon = _resolve(_ctx_guild_id(interaction), equipment)
    start_iso, end_iso = _local_date_window(date)
    n = db.delete_entry_between(
        _ctx_guild_id(interaction), canon, start_iso, end_iso, user_id=target.id,
        actor_id=interaction.user.id, actor_name=interaction.user.display_name,
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

    guild_id = _ctx_guild_id(interaction)
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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

    guild_id = _ctx_guild_id(interaction)
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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


def _help_sections() -> dict[str, discord.Embed]:
    """One embed per category, built fresh so the enabled-integration set is
    always current.

    This is paged rather than sent as two big embeds because Discord caps a
    *message* at 6 000 characters across all of its embeds. The previous
    two-embed form measured 6 440 with Strava and Hevy on — i.e. `/help`, the
    front door, failed outright — and one field was 53 characters from the
    separate 1 024-per-field cap. Paging removes both ceilings permanently: no
    single view grows past a few hundred characters however many commands land.
    """
    sections: dict[str, discord.Embed] = {}

    def section(key: str, title: str, body: str, *, footer: str | None = None):
        sections[key] = ui.card(
            title, description=body, colour=ui.BRAND, footer=footer,
        )

    section(
        "Overview",
        f"{ui.LIFT} gym-bot v{__version__}",
        "I read gym posts, parse the lifts, and track progress. Just post "
        "your gym stats — no command needed — and I'll store them.\n\n"
        "Example message I understand:\n"
        "```\nBench press: 80kg\nIncline bench 70\n"
        "Leg press: 6 plates\nDips: BW+20kg\n```\n"
        "Pick a category below for the full command list.",
        footer="Weights parsed as kg. Plates assumed 20kg each.",
    )
    section(
        "Stats & progress", f"{ui.CHART} Stats & progress",
        (
            "`/stats [user]` — personal bests\n"
            "`/summary [user]` — profile overview\n"
            "`/coach [user] [days]` — AI progress report from all your data\n"
            "`/overview <equipment> [user]` — lift consistency\n"
            "`/checkin [user]` — copy/paste stat template\n"
            "`/stale [user] [days]` — lifts not updated lately\n"
            "`/progress <equipment> [user]` — best per month\n"
            "`/graph <equipment> [user]` — plot a PNG chart\n"
            "`/history <equipment> [user]` — your timeline\n"
            "`/recent [user]` — your last 10 entries\n"
            "`/session [user]` — full breakdown of last session\n"
            "`/streak [user]` — streaks (daily = Revo gym attendance)\n"
            "`/tonnage [user] [days]` — total kg moved in a window\n"
            "`/projection <equipment> [target_kg] [user]` — ETA to a goal\n"
            "`/plates <target_kg> [bar_kg]` — plate-loading helper\n"
            "`/leaderboard <equipment>` — top 25 in server\n"
            "`/machine <equipment>` — everyone's timeline\n"
            "`/compare <user> [equipment]` — head-to-head\n"
            "`/serverstats` — server-wide overview"
        ),
    )
    section(
        "Goals", f"{ui.GOAL} Goals",
        (
            "`/goal_set <equipment> <target_kg> [bodyweight]`\n"
            "`/goals [user]` — progress bars\n"
            "`/goal_remove <equipment>`"
        ),
    )
    section(
        "Cardio", "🏃 Cardio programs",
        (
            "`/cardio create <name> <workout> [progression]` — save a routine\n"
            "`/cardio view [name]` — show the next version of a routine\n"
            "`/cardio complete <difficulty> [name]` — log + adapt it\n"
            "`/cardio log <workout> [difficulty]` — track a one-off session\n"
            "`/cardio history [user]` — recent sessions\n"
            "`/cardio remove <name>` — remove a routine (history stays)\n\n"
            "Or post the workout directly in chat: `15 mins elliptical lv12, "
            "30mins on stair master lv10, 15mins on treadmill 10 degrees "
            "10 speed`. Level, incline and speed are optional. Easy/right/hard "
            "ratings adjust one part at a time."
        ),
        footer="Cardio programs and history follow you across servers and DMs.",
    )
    section(
        "Calories", f"{ui.FOOD} Calories",
        (
            "`/calories setup <target>` — set a daily target "
            "(kcal or kJ, e.g. `2500` or `8700kj`)\n"
            "`/calories add <amount> [note]` — log intake "
            "(`650`, `650c`, `2700kj`, or a saved food)\n"
            "Or just type `650kcal` / `200c` / `2700kj` on its own in chat — "
            "I'll react ✅ with your running total\n"
            "Label maths: `0.7x1640kj` = 0.7 × 1640 kJ (per-100g label, ate "
            "70g) — same idea for protein: `0.7x43p`\n"
            "Backdate with `yesterday` / `monday` / `3 days ago`\n"
            "`/today [user]` — today's calories *and* protein in one card · "
            "`/calories week [user]`\n"
            "`/calories edit <amount>` — fix your last entry · "
            "React ❌ on my reply to remove that entry\n"
            "`/calories leaderboard` — longest 🔥 logging streak\n"
            "`/calories remind [time]` — evening DM if you haven't logged\n"
            "`/calories export [user]` — CSVs of calories/protein/bodyweight\n"
            "`/calories stop` — stop tracking (history kept)\n"
            "Tracking is global (all servers + DMs); tracked members get an "
            "AI summary in the Sunday `/weekly_report`"
        ),
    )
    section(
        "Smart food logging", f"{ui.FOOD} Smart food logging",
        (
            "`/calories food_set <name> <amount> [protein]` — save a food "
            "shortcut, then log it by typing `coffee` or `2 coffee` in chat\n"
            "`/calories food_list` · `/calories food_remove <name>`\n"
            "`/calories meal_set <name> <foods>` — bundle saved foods "
            "(`breakfast` = `coffee, 2x oats`); log it with one word\n"
            "`/calories meal_list` · `/calories meal_remove <name>`\n"
            "`/calories food_lookup <query> [save]` — per-100g values from "
            "Open Food Facts (name or barcode)\n"
            "`/estimate [description] [photo]` — AI-guess calories + protein "
            "from words, a photo of the food, or a photo of its nutrition "
            "panel; or type `~large big mac meal` in chat, or `~` with a "
            "photo attached (`~110g` logs a panel at that serving)\n"
            "`/calories tdee [user] [days]` — estimate your real maintenance "
            "calories from your own logs + weigh-ins"
        ),
    )
    section(
        "Protein", f"{ui.PROTEIN} Protein",
        (
            "Optional daily-max tracker — flags when you go *over*.\n"
            "`/protein setup <grams>` — set your daily max (e.g. `180`)\n"
            "`/protein add <grams> [note]` — log protein\n"
            "Or just type `40p` / `40g protein` in chat\n"
            "Label maths: `0.7x43p` logs 0.7 × 43 g (per-100g label, ate 70g)\n"
            "Log both at once: `500c and 40p` (or `0.7x1640kj and 0.7x43p`)\n"
            "`/today [user]` — today's protein *and* calories in one card · "
            "`/protein week [user]`\n"
            "`/protein edit <grams>` — fix your last entry · "
            "React ❌ on my reply to remove that entry\n"
            "`/protein stop` — stop tracking (history kept)"
        ),
    )
    section(
        "Logging & editing", f"{ui.EDIT} Logging & editing",
        (
            "`/log <equipment> <weight_kg> [user] [bodyweight]` — manual entry\n"
            "`/bodyweight [weight_kg] [user]` — record your bodyweight so the bot "
            "shows your true load on pull-ups, dips, etc. "
            "(`bodyweight 100kg` in chat works too, and `@user bodyweight 100kg` "
            "sets someone else's)\n"
            "`/bodyweight_history [user] [limit]` — list past weigh-ins\n"
            "`/bodyweight_graph [user]` — plot a PNG chart of weigh-ins\n"
            "`/bodyweight_goal [target_kg]` — set a weight goal + projected ETA\n"
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
    )
    section(
        "Discovery & maintenance", f"{ui.CHART} Discovery & maintenance",
        (
            "`/equipment_list` — what the bot knows about\n"
            "`/aliases <equipment>` — spellings I accept\n"
            "`/daily_update [days_ago]` — post a daily recap\n"
            "`/weekly_report` — post the 7-day gym + calorie report\n"
            "`/export [user]` — download lifts as CSV\n"
            "`/server [name]` — pick which server your DM commands read\n"
            "`/help` — this menu · `/ping` · `/version`\n"
            "\n"
            "**Maintenance**\n"
            "`/backfill [limit]` — rescan this channel\n"
            "`/purge <equipment>` — delete all rows for a lift\n"
            "`/alias_add <phrase> <equipment>` — teach a custom name\n"
            "`/alias_remove <phrase>` · `/alias_list`"
        ),
    )
    section(
        "Revo Fitness", "🏢 Revo Fitness",
        (
            "`/busy [state] [club]` — live occupancy: your home club, a "
            "state's busiest 5 (`SA`, `WA`, `VIC`, `NSW`), or one named club\n"
            "`/revo_clubs [state] [club]` — every club in a state "
            "(`SA`, `WA`, `VIC`, `NSW`) with live head-counts, or one club + "
            "nearest gyms. No args uses your home state\n"
            "`/revo_link <email> <password>` — link your account (reply is private)\n"
            "`/help_revo_link` — public explainer for `/revo_link`\n"
            "`/revo_unlink` — remove the link\n"
            "`/revo_streak` — your weekly check-in streak\n"
            "`/revo_streak_compare` — streak leaderboard for all linked members\n"
            "`/revo_calendar` — monthly check-in calendar\n"
            "`/revo_calendar_compare` — side-by-side calendars for all linked members\n"
            "`/revo_summary` — streak, check-ins, tickets & next draw in one\n"
            "`/revo_tickets` — ticket balance & recent earning history\n"
            "`/revo_raffle` — your tickets + monthly/major draw countdowns\n"
            "`/revo_card` — privately show YOUR entry barcode (ephemeral, your own card)\n"
            "`/seeprofile` — roster of every linked member's Revo photo + name"
        ),
    )
    if not STRAVA_DISABLED:
        section(
            "Strava", "🏃 Strava",
            (
                "`/strava_link` — link your Strava (workouts auto-post to the feed)\n"
                "`/strava_status` — check if you're linked\n"
                "`/strava_latest [member]` — show the most recent activity\n"
                "`/strava_unlink` — revoke access & remove your tokens"
            ),
        )
        sections["Strava"].colour = ui.STRAVA
    if _hevy_enabled():
        section(
            "Hevy", "📱 Hevy",
            (
                "`/hevy_help` — how it works + how to link\n"
                "`/hevy_link` — link Hevy (workouts import as lifts + post to the feed)\n"
                "`/hevy_recent` — show the most recent workout\n"
                "`/hevy_sync` — re-sync your last 50 workouts\n"
                "`/hevy_status` — check if you're linked\n"
                "`/hevy_unlink` — remove your stored API key"
            ),
        )
        sections["Hevy"].colour = ui.HEVY
    if _ha_enabled():
        section(
            "Home Assistant", "🏠 Home Assistant",
            (
                "`/ha_help` — how the smart-scale sync works\n"
                "`/setup_ha` — connect your own Home Assistant (url + token)\n"
                "`/ha_link` — pick which of its sensors are yours\n"
                "`/ha_entities` — list the body sensors it can see\n"
                "`/ha_body [member]` — latest body-composition numbers\n"
                "`/ha_graph <metric> [member]` — plot a composition trend\n"
                "`/ha_status` — check your connection\n"
                "`/ha_unlink` — disconnect and delete your token"
            ),
        )
        sections["Home Assistant"].colour = ui.HOME_ASSISTANT
    return sections


@bot.tree.command(name="help", description="Show what this bot can do.")
async def help_cmd(interaction: discord.Interaction) -> None:
    sections = _help_sections()
    view = ui.Sections(
        sections, owner_id=interaction.user.id, placeholder="Browse commands…",
    )
    await interaction.response.send_message(
        embed=sections["Overview"], view=view, ephemeral=True,
    )
    # Held so on_timeout can grey the dropdown out instead of leaving a control
    # that looks live and silently does nothing.
    with contextlib.suppress(discord.HTTPException):
        view._message = await interaction.original_response()


@bot.tree.command(
    name="summary",
    description="A profile overview: totals, top PRs, most trained, biggest gains.",
)
@app_commands.describe(user="The user to look up (defaults to you).")
async def summary_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None
) -> None:
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    totals = db.user_summary(guild_id, target.id)
    if not totals:
        await interaction.response.send_message(
            embed=_no_lifts_embed(target.display_name), ephemeral=True,
        )
        return

    top = db.user_top_prs(guild_id, target.id, limit=5)
    trained = db.user_most_trained(guild_id, target.id, limit=5)
    gains = db.user_biggest_gains(guild_id, target.id, limit=5)
    streak = _compute_streak_weeks(_local_log_dates(guild_id, target.id))

    # The name lives in the author row, so the title drops it rather than
    # heading the card with it twice.
    embed = ui.card(
        f"{ui.CHART} Gym summary",
        colour=ui.score_streak(streak * 7) if streak else ui.BRAND,
        member=target,
        footer=f"training since {_format_date(totals['first_at'])}",
        timestamp=True,
    )
    # Counters as tiles: Discord packs three inline fields per row, which is
    # exactly what a row of stats wants. inline=False on all four made the
    # whole card one narrow column of bullet lists.
    ui.tiles(
        embed,
        ("Lifts", f"**{totals['total_lifts']:,}**"),
        ("Exercises", f"**{totals['unique_equip']:,}**"),
        ("Sessions", f"**{totals['sessions']:,}**"),
    )
    ui.tiles(
        embed,
        ("Streak", f"**{ui.plural(streak, 'week')}** {ui.STREAK}" if streak
                   else "—"),
        ("Last lift", ui.when(totals["last_at"])),
        ("First lift", ui.day(totals["first_at"])),
    )
    if top:
        ui.block(embed, "Heaviest PRs", ui.table(
            [[_safe_label(r["equipment"], limit=20),
              _format_weight(r["best"], bool(r["bw"]))] for r in top],
            align="<>",
        ))
    if trained:
        busiest = max(int(r["n"]) for r in trained)
        ui.block(embed, "Most trained", ui.table(
            [[_safe_label(r["equipment"], limit=20), f"{r['n']}x",
              ui.bar(int(r["n"]), busiest, width=8)] for r in trained],
            align="<>",
        ))
    if gains:
        ui.block(embed, "Biggest gains", "\n".join(
            f"**{_safe_label(r['equipment'], limit=24)}** "
            f"{ui.arrow(ui.kg(r['first_w']), ui.kg(r['last_w']))} "
            f"{ui.delta(float(r['delta']))}"
            for r in gains
        ))
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
    if await _deny_invisible_target(interaction, target):
        return
    lim = max(1, min(25, limit))
    rows = db.user_recent(_ctx_guild_id(interaction), target.id, lim)
    if not rows:
        await interaction.response.send_message(
            embed=_no_lifts_embed(target.display_name), ephemeral=True,
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    rows = db.user_all_lifts(guild_id, target.id)
    if not rows:
        await interaction.response.send_message(
            embed=_no_lifts_embed(_display_name(target)), ephemeral=True,
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


# How many months of Revo attendance to walk back when tracing a daily
# attendance streak. Bounds the network calls for someone with an absurdly
# long run; the typical case fetches just the current + previous month.
_MAX_ATTENDANCE_MONTHS = 13


def _revo_attended_dates(client, now_local: datetime) -> set[date]:
    """Set of local dates the user checked in to Revo, walking back month by
    month from ``now_local`` while a streak could still cross the boundary.

    Always fetches the current + previous month (so a run ending yesterday
    near a month boundary is caught), then keeps going back only while the
    1st of the month just fetched was attended — i.e. while the run might
    extend further. Capped at :data:`_MAX_ATTENDANCE_MONTHS`.
    """
    attended: set[date] = set()
    y, m = now_local.year, now_local.month
    for i in range(_MAX_ATTENDANCE_MONTHS):
        cal = client.get_streak_calendar(m, y)  # {day_of_month: attended}
        for d, did in cal.items():
            if did:
                try:
                    attended.add(date(y, m, d))
                except ValueError:  # pragma: no cover - defensive
                    continue
        crosses_boundary = cal.get(1) is True
        # i==0 is the current month; always grab the previous one too. After
        # that, only continue when the run still touches the 1st.
        if i >= 1 and not crosses_boundary:
            break
        first_of_month = date(y, m, 1)
        prev = first_of_month - timedelta(days=1)
        y, m = prev.year, prev.month
    return attended


def _compute_attendance_streak(row) -> tuple[int, int] | None:
    """``(current, longest)`` daily gym-attendance streak from Revo, or None
    on auth/network failure (caller falls back to log-based). Blocking — run
    it in an executor."""
    from .training_math import daily_streak
    now_local = datetime.now(DISPLAY_TZ)
    try:
        client = _client_for_user(row)
        attended = _revo_attended_dates(client, now_local)
    except revo_client.RevoAuthError:
        _drop_cached_client(int(row["user_id"]))
        return None
    except Exception:  # pragma: no cover - network
        LOG.exception("Revo attendance streak fetch failed")
        return None
    return daily_streak(attended, now_local.date())


@bot.tree.command(
    name="streak",
    description="Show a user's training streaks (daily = Revo gym attendance).",
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    log_dates = _local_log_dates(guild_id, target.id)

    # Daily streak is gym attendance from Revo when the target has linked an
    # account; that needs a (blocking) network fetch, so defer first. Falls
    # back to log-based days if Revo is off, unlinked, or unreachable.
    revo_row = None if REVO_DISABLED else db.get_revo_account(target.id)
    attendance: tuple[int, int] | None = None
    if revo_row is not None and revo_client.available():
        await interaction.response.defer(thinking=True)
        attendance = await bot.loop.run_in_executor(
            None, _compute_attendance_streak, revo_row,
        )
        respond = interaction.followup.send
    else:
        respond = interaction.response.send_message

    if not log_dates and attendance is None:
        await respond(
            embed=ui.empty(
                f"No training history yet for "
                f"{_safe_label(target.display_name, limit=32)}",
                hint="Streaks build from logged sessions — post one in chat "
                     "and the counter starts today.",
            ),
            ephemeral=True,
        )
        return

    today = datetime.now(DISPLAY_TZ).date()
    cur_w, long_w = weekly_streak(log_dates, today) if log_dates else (0, 0)
    if attendance is not None:
        cur_d, long_d = attendance
        daily_name = "Daily · gym attendance"
        source_note = "daily streak from Revo check-ins"
    else:
        cur_d, long_d = daily_streak(log_dates, today) if log_dates else (0, 0)
        daily_name = "Daily · from logs"
        source_note = (
            "daily streak from logged sessions"
            if revo_row is not None
            else "link Revo with /revo_link for real gym attendance"
        )
    # Colour is the intensity signal. The old single 🔥 fired on a bare
    # `>= 3` threshold, so a 3-day run and a 90-day run looked identical.
    embed = ui.card(
        f"{ui.STREAK} Training streaks",
        colour=ui.score_streak(max(cur_d, cur_w * 7)),
        member=target,
        footer=source_note,
    )
    recent = {d for d in log_dates}
    ui.tiles(
        embed,
        ("Weekly", f"**{ui.plural(cur_w, 'week')}**\n"
                   f"{ui.subtext(f'best {long_w}')}"),
        (daily_name, f"**{ui.plural(cur_d, 'day')}**\n"
                     f"{ui.subtext(f'best {long_d}')}"),
        ("Active days", f"**{len(log_dates):,}**"),
    )
    ui.block(embed, "Last 14 days", "`{}`\n{}".format(
        ui.strip([
            (today - timedelta(days=n)) in recent for n in range(13, -1, -1)
        ]),
        ui.subtext("oldest left · today right"),
    ))
    await respond(embed=embed)


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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
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
            embed=ui.empty(
                f"Nothing logged in the {window_label}",
                hint=f"{_safe_label(target.display_name, limit=32)} has no "
                     f"entries in this window — try a longer one with "
                     "`days:`, or `days:0` for all time.",
            ),
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    day, rows = db.last_session_for_user(guild_id, target.id)
    if not rows or day is None:
        await interaction.response.send_message(
            embed=ui.empty(
                f"No sessions logged for "
                f"{_safe_label(target.display_name, limit=32)} yet",
                hint="Post a session in chat and I'll group it automatically.",
                cmd="/log adds one by hand",
            ),
            ephemeral=True,
        )
        return
    target_bw = _user_bodyweight(guild_id, target.id)
    total_kg = sum(float(r["weight_kg"] or 0) for r in rows)
    heaviest = max(rows, key=lambda r: float(r["weight_kg"] or 0))
    embed = ui.card(
        f"{ui.LIFT} Last session",
        description=f"{ui.day(day)} · {ui.when(day)}",
        colour=ui.BRAND,
        member=target,
        footer=ui.plural(len(rows), "entry", "entries"),
    )
    ui.tiles(
        embed,
        ("Exercises", f"**{len(rows)}**"),
        ("Total load", f"**{ui.num(total_kg, 'kg')}**"),
        ("Top lift", f"**{_format_weight(heaviest['weight_kg'], bool(heaviest['bw']))}**"
                     f"\n{ui.subtext(_safe_label(heaviest['equipment'], limit=18))}"),
    )
    table_rows = []
    for r in rows:
        reps = r["reps"] if "reps" in r.keys() else None
        table_rows.append([
            _safe_label(r["equipment"], limit=20),
            _format_weight(r["weight_kg"], bool(r["bw"])),
            f"x{reps}" if reps else "",
        ])
    ui.block(embed, "Lifts", ui.table(table_rows, align="<>", max_rows=25))
    await interaction.response.send_message(embed=embed)


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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
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
    if await _deny_invisible_target(interaction, target):
        return
    threshold = max(1, min(365, days))
    rows = db.user_latest_by_equipment(_ctx_guild_id(interaction), target.id)
    stale_rows = []
    for row in rows:
        local_date, age_days = _format_local_day_age(row["logged_at"])
        if age_days >= threshold:
            stale_rows.append((age_days, local_date, row))
    stale_rows.sort(reverse=True, key=lambda item: (item[0], item[2]["equipment"]))

    if not rows:
        await interaction.response.send_message(
            embed=_no_lifts_embed(target.display_name), ephemeral=True,
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
    guild_id = _ctx_guild_id(interaction)
    rows = db.pop_last_n_for_user(
        guild_id, interaction.user.id, n,
        actor_id=interaction.user.id,
    )
    if not rows:
        await interaction.response.send_message(
            embed=ui.empty(
                "Nothing to undo",
                hint="You have no lift entries on record in this server.",
            ),
            ephemeral=True,
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
            embed=ui.empty(
                "Pick someone else to compare against",
                hint="`/compare` puts two lifters side by side — `/stats` "
                     "shows your own bests.",
            ),
            ephemeral=True,
        )
        return

    guild_id = _ctx_guild_id(interaction)
    a_rows = {r["equipment"]: r for r in db.personal_bests(guild_id, interaction.user.id)}
    b_rows = {r["equipment"]: r for r in db.personal_bests(guild_id, user.id)}

    if equipment:
        canon = _resolve(guild_id, equipment)
        keys = [canon]
    else:
        keys = sorted(set(a_rows) | set(b_rows))

    if not keys or not any(k in a_rows or k in b_rows for k in keys):
        await interaction.response.send_message(
            embed=ui.empty(
                "Nothing to compare yet",
                hint="Neither of you has a personal best on record for that "
                     "lift.",
            ),
            ephemeral=True,
        )
        return

    a_name = _safe_label(interaction.user.display_name, limit=20)
    b_name = _safe_label(user.display_name, limit=20)
    # ASCII markers inside the fence — an emoji is not one monospace cell and
    # would shear the columns. The 🟢/🔴/⚪ vocabulary stays on the score tiles
    # outside it. The two "missing data" branches used to drop the marker
    # entirely, which broke the alignment they were sitting in.
    table_rows: list[list[str]] = []
    a_wins = b_wins = ties = 0
    for k in keys:
        ra, rb = a_rows.get(k), b_rows.get(k)
        aw = ra["best"] if ra else None
        bw = rb["best"] if rb else None
        if aw is None and bw is None:
            continue
        if aw is None:
            marker, b_wins = "-", b_wins + 1
        elif bw is None:
            marker, a_wins = "+", a_wins + 1
        elif aw > bw:
            marker, a_wins = "+", a_wins + 1
        elif bw > aw:
            marker, b_wins = "-", b_wins + 1
        else:
            marker, ties = "=", ties + 1
        table_rows.append([
            marker,
            _safe_label(k, limit=18),
            _format_weight(aw, bool(ra["bw"])) if ra else "—",
            _format_weight(bw, bool(rb["bw"])) if rb else "—",
        ])

    embed = ui.card(
        f"{ui.TROPHY} {a_name} vs {b_name}",
        colour=(ui.SUCCESS if a_wins > b_wins
                else ui.DANGER if b_wins > a_wins else ui.BRAND),
        subject=user,
        footer="+ you lead · - they lead · = tied",
        timestamp=True,
    )
    if not equipment:
        ui.tiles(
            embed,
            (a_name, f"{ui.UP} **{a_wins}**"),
            ("Tied", f"{ui.FLAT} **{ties}**"),
            (b_name, f"{ui.DOWN} **{b_wins}**"),
        )
    ui.block(embed, "Lift by lift", ui.table(
        table_rows, align="<<>>", headers=["", "lift", a_name[:9], b_name[:9]],
        max_rows=20,
    ))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="serverstats",
    description="Server-wide totals, top lifters, and most popular equipment.",
)
async def serverstats_cmd(interaction: discord.Interaction) -> None:
    guild_id = _ctx_guild_id(interaction)
    totals = db.server_totals(guild_id)
    if not totals:
        await interaction.response.send_message(
            "No lifts logged in this server yet.", ephemeral=True
        )
        return
    top_users = db.server_top_users(guild_id, limit=5)
    popular = db.server_popular_equipment(guild_id, limit=5)

    _g = _ctx_guild(interaction)
    name = _g.name if _g else "this server"
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
    if await _deny_invisible_target(interaction, target):
        return
    rows = db.export_rows(_ctx_guild_id(interaction), user_id=target.id)
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
    guild_id = _ctx_guild_id(interaction)
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
    guild_id = _ctx_guild_id(interaction)
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
    guild_id = _ctx_guild_id(interaction)
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
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
    guild_id = _ctx_guild_id(interaction)
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
    n = db.alias_remove(_ctx_guild_id(interaction), key)
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
    rows = db.alias_list(_ctx_guild_id(interaction))
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
        _ctx_guild_id(interaction),
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon, limit=1000)
    if not rows:
        await interaction.response.send_message(
            embed=_no_history_embed(canon, target.display_name), ephemeral=True,
        )
        return

    stats = lift_overview(
        ((r["logged_at"], float(r["weight_kg"])) for r in rows),
        DISPLAY_TZ,
    )
    if stats is None:
        await interaction.response.send_message(
            embed=ui.empty(
                "Couldn't build an overview",
                hint=f"There are {_plural(len(rows), 'entry', 'entries')} for "
                     f"{_safe_label(canon)}, but none carry a usable date.",
            ),
            ephemeral=True,
        )
        return
    bodyweight = any(bool(r["bw"]) for r in rows)

    # Eleven statistics were sharing five prose lines joined by '·'. They split
    # into a headline gauge (the one score that summarises the rest) and tiles.
    embed = ui.card(
        f"{ui.CHART} {_safe_label(canon)} — overview",
        description=f"`{ui.gauge(stats.consistency_score)}` "
                    f"**{stats.consistency_score}**/100 consistency",
        colour=(ui.DANGER if stats.consistency_score < 40
                else ui.WARNING if stats.consistency_score < 70
                else ui.SUCCESS),
        member=target,
        footer=f"{ui.plural(stats.total_logs, 'log')} over "
               f"{ui.plural(stats.active_days, 'active day')}",
        timestamp=True,
    )
    ui.tiles(
        embed,
        ("Latest", f"**{_format_weight(stats.latest_kg, bodyweight)}**\n"
                   f"{ui.subtext(ui.when(stats.latest_day.isoformat()))}"),
        ("Best", f"**{_format_weight(stats.best_kg, bodyweight)}**"),
        ("Change", ui.delta(stats.improvement_kg)),
    )
    ui.tiles(
        embed,
        ("Week streak", f"**{stats.current_week_streak}**"),
        ("Active weeks", f"**{stats.active_weeks}**/{stats.total_weeks}"),
        ("Last 30 days", f"**{ui.plural(stats.logs_last_30_days, 'log')}**"),
    )
    gap_bits = []
    if stats.avg_gap_days is not None:
        gap_bits.append(f"average **{stats.avg_gap_days:.1f} days**")
    if stats.longest_gap_days is not None:
        gap_bits.append(f"longest **{ui.plural(stats.longest_gap_days, 'day')}**")
    ui.block(
        embed, "Spacing between sessions",
        " · ".join(gap_bits) or "Only one day logged so far.",
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Progress graph
# ---------------------------------------------------------------------------


# Chart palette. Matches the embed the PNG is attached to — the old cream
# canvas glared out of a dark card, which is the single loudest thing about
# the old charts.
_CHART_BG = "#202225"
_CHART_INK = "#e2e4e7"
_CHART_MUTED = "#9298a1"
_CHART_GRID = "#33363b"
_BODYWEIGHT_TREND = "#4ef08a"
_BODYWEIGHT_GOAL = "#6aa9ff"
_BODYWEIGHT_WARN = "#f0a04e"
# Matplotlib's pyplot state is process-global and not thread-safe. Automatic
# weigh-in charts render off the Discord event loop, so every chart call site
# shares this lock rather than letting a message and HA poll draw concurrently.
_CHART_RENDER_LOCK = threading.RLock()


def _run_serialized_matplotlib(renderer, /, *args, **kwargs):
    """Run any Matplotlib renderer exclusively and clean leaked figures.

    Revo's comparison image composes individual calendar renderers, so the lock
    is re-entrant. Tracking figure numbers around the whole renderer guarantees
    cleanup even when older drawing code fails before its explicit ``close``.
    """
    with _CHART_RENDER_LOCK:
        matplotlib = importlib.import_module("matplotlib")
        matplotlib.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
        existing = set(plt.get_fignums())
        try:
            return renderer(*args, **kwargs)
        finally:
            for number in set(plt.get_fignums()) - existing:
                plt.close(number)


def _render_trend_chart(
    plt, mdates, ticker,
    xs: list,
    ys: list[float],
    trend: list[float],
    *,
    title: str,
    subtitle: str,
    trend_label: str,
    trend_colour: str,
    unit: str = "kg",
    fmt=lambda v: f"{v:g}kg",
    bodyweight: BodyweightTrend | None = None,
    entries: int = 0,
    logged_days: int = 0,
    latest_recorded_kg: float | None = None,
) -> "io.BytesIO":
    """Actual readings in thin grey, one bold trend line over the top.

    The readings stay visible so nothing is hidden, but they stop competing:
    on a noisy series a single spike used to dominate the whole picture and
    read as the story. Returns a PNG buffer.
    """
    if bodyweight is not None:
        return _render_bodyweight_dashboard(
            plt,
            mdates,
            ticker,
            bodyweight,
            title=title,
            entries=entries,
            logged_days=logged_days,
            latest_recorded_kg=latest_recorded_kg,
        )

    fig, ax = plt.subplots(figsize=(8.8, 4.4), dpi=150)
    try:
        fig.patch.set_facecolor(_CHART_BG)
        ax.set_facecolor(_CHART_BG)

        ax.plot(
            xs, ys, marker="o", markersize=4.5, markerfacecolor=_CHART_BG,
            markeredgewidth=1.4, linewidth=1.1, color=_CHART_MUTED, alpha=0.75,
            label="logged", zorder=2,
        )
        ax.plot(
            xs, trend, linewidth=3.0, color=trend_colour, label=trend_label,
            zorder=3, solid_capstyle="round",
        )
        floor = min(min(ys), min(trend)) - (max(ys) - min(ys) + 1) * 2
        ax.fill_between(xs, trend, floor, color=trend_colour, alpha=0.09, zorder=1)

        ax.set_title(title, loc="left", fontsize=15, fontweight="bold",
                     color=_CHART_INK, pad=20)
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9,
                color=_CHART_MUTED, va="bottom")
        ax.set_ylabel(unit, color=_CHART_MUTED)

        ax.grid(axis="y", color=_CHART_GRID, linewidth=0.8, alpha=0.7)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(_CHART_GRID)
        ax.tick_params(colors=_CHART_MUTED, labelsize=9)

        lo, hi = min(ys), max(ys)
        pad = max(0.8, (hi - lo) * 0.18)
        ax.set_ylim(lo - pad, hi + pad)
        ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=5))

        if len(xs) > 1:
            span = max(xs) - min(xs)
            xpad = timedelta(days=max(1.0, span.days * 0.04))
            ax.set_xlim(min(xs) - xpad, max(xs) + xpad)
            locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
            # ConciseDateFormatter parks a "2026-Jul" offset in the corner,
            # which reads as a stray annotation. The span is already in the
            # subtitle.
            ax.xaxis.get_offset_text().set_visible(False)
        else:
            ax.set_xlim(xs[0] - timedelta(days=1), xs[0] + timedelta(days=1))
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))

        # First and last only — enough to anchor the scale without label
        # spaghetti. With one point those are the same anchor, so label it once.
        anchors = (
            ((0, "bottom", 10),)
            if len(xs) == 1 else
            ((0, "bottom", 10), (len(xs) - 1, "top", -12))
        )
        for idx, va, dy in anchors:
            ax.annotate(
                fmt(ys[idx]), xy=(xs[idx], ys[idx]), xytext=(0, dy),
                textcoords="offset points", ha="center", va=va,
                fontsize=9, fontweight="bold", color=_CHART_INK,
            )

        legend = ax.legend(loc="best", frameon=False, fontsize=8.5)
        for text in legend.get_texts():
            text.set_color(_CHART_MUTED)

        fig.tight_layout(pad=1.1)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        buf.seek(0)
        return buf
    finally:
        # Automatic HA/message charts are unattended. Never leak a pyplot
        # figure when plotting, layout, encoding or the buffer write fails.
        plt.close(fig)


def _render_bodyweight_dashboard(
    plt,
    mdates,
    ticker,
    series: BodyweightTrend,
    *,
    title: str,
    entries: int,
    logged_days: int,
    latest_recorded_kg: float | None,
) -> "io.BytesIO":
    """Render the goal-aware bodyweight dashboard used in Discord.

    The composition intentionally follows the supplied reference: history and
    projection dominate the upper panel, while the smaller lower panel answers
    the separate question "how quickly is my trend changing right now?".
    """

    fig, (ax, rate_ax) = plt.subplots(
        2,
        1,
        figsize=(13, 7.5),
        dpi=140,
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )
    try:
        fig.patch.set_facecolor(_CHART_BG)
        for axis in (ax, rate_ax):
            axis.set_facecolor(_CHART_BG)
            axis.grid(True, color=_CHART_GRID, linewidth=0.8, alpha=0.7)
            axis.set_axisbelow(True)
            axis.tick_params(colors=_CHART_MUTED, labelsize=10)
            for spine in axis.spines.values():
                spine.set_visible(False)

        span_days = max(
            0,
            (series.logged_when[-1].date() - series.logged_when[0].date()).days,
        )
        trend_delta = series.trend_kg[-1] - series.trend_kg[0]
        trend_week = trend_delta / max(span_days, 1) * 7.0
        coverage = logged_days / max(span_days, 1)
        period = (
            f"{series.logged_when[0]:%d %b}–"
            f"{series.logged_when[-1]:%d %b %Y}"
        )
        subtitle = (
            f"{entries} "
            f"{'entry' if entries == 1 else 'entries'} over {span_days} days "
            f"({logged_days} logged, {coverage:.0%} coverage)  ·  trend "
            f"{series.trend_kg[0]:.1f} → {series.trend_kg[-1]:.1f}kg "
            f"({trend_delta:+.1f}kg, {trend_week:+.2f}kg/wk)"
        )
        fig.text(
            0.077,
            0.955,
            title,
            fontsize=19,
            fontweight="bold",
            color=_CHART_INK,
            va="bottom",
        )
        fig.text(
            0.923,
            0.955,
            period,
            fontsize=11,
            fontweight="bold",
            color=_CHART_MUTED,
            ha="right",
            va="bottom",
        )
        fig.text(
            0.077,
            0.925,
            subtitle,
            fontsize=10.5,
            color=_CHART_MUTED,
            va="bottom",
        )

        if series.goal_kg is not None:
            goal_text = f"goal {series.goal_kg:g}kg"
            if series.goal_eta is not None:
                goal_text += f"  ·  ETA {series.goal_eta:%d %b %Y}"
            else:
                goal_text += "  ·  ETA pending"
            fig.text(
                0.923,
                0.925,
                goal_text,
                fontsize=11,
                fontweight="bold",
                color=_BODYWEIGHT_GOAL,
                ha="right",
                va="bottom",
            )
        else:
            fig.text(
                0.923,
                0.925,
                "set a goal with /bodyweight_goal",
                fontsize=9.5,
                color=_CHART_MUTED,
                ha="right",
                va="bottom",
            )

        noise = series.noise_sd_kg
        ax.fill_between(
            series.trend_when,
            [value - noise for value in series.trend_kg],
            [value + noise for value in series.trend_kg],
            color=_BODYWEIGHT_TREND,
            alpha=0.13,
            linewidth=0,
            label=f"±1 SD noise (±{noise:.1f}kg)",
        )

        # Connect nearby readings only. Long gaps remain visually honest rather
        # than implying measurements the member never made.
        segment_x: list[datetime] = []
        segment_y: list[float] = []
        previous: datetime | None = None
        for when, weight in zip(series.logged_when, series.logged_kg):
            if previous is not None and (when - previous).days > 2:
                if len(segment_x) > 1:
                    ax.plot(
                        segment_x,
                        segment_y,
                        color=_CHART_MUTED,
                        linewidth=1.0,
                        alpha=0.55,
                        zorder=2,
                    )
                segment_x, segment_y = [], []
            segment_x.append(when)
            segment_y.append(weight)
            previous = when
        if len(segment_x) > 1:
            ax.plot(
                segment_x,
                segment_y,
                color=_CHART_MUTED,
                linewidth=1.0,
                alpha=0.55,
                zorder=2,
            )
        ax.plot(
            series.logged_when,
            series.logged_kg,
            linestyle="none",
            marker="o",
            markersize=4.5,
            markerfacecolor=_CHART_BG,
            markeredgecolor=_CHART_MUTED,
            markeredgewidth=1.4,
            zorder=3,
            label="logged",
        )
        ax.plot(
            series.trend_when,
            series.trend_kg,
            color=_BODYWEIGHT_TREND,
            linewidth=3.0,
            zorder=4,
            solid_capstyle="round",
            label="7-day EWMA trend",
        )
        if series.projection_when:
            ax.plot(
                series.projection_when,
                series.projection_kg,
                color=_BODYWEIGHT_TREND,
                linewidth=1.8,
                linestyle=(0, (4, 3)),
                alpha=0.68,
                zorder=3,
                label="projection",
            )

        right_edge = (
            series.projection_when[-1]
            if series.projection_when
            else series.logged_when[-1]
        )
        total_span = max(1, (right_edge - series.logged_when[0]).days)
        x_pad = timedelta(days=max(2, round(total_span * 0.025)))
        left_edge = series.logged_when[0] - x_pad
        right_edge = right_edge + x_pad
        ax.set_xlim(left_edge, right_edge)

        y_values = [
            *series.logged_kg,
            *series.trend_kg,
            *series.projection_kg,
        ]
        if series.goal_kg is not None:
            y_values.append(series.goal_kg)
        low, high = min(y_values), max(y_values)
        y_pad = max(0.8, (high - low) * 0.06)
        ax.set_ylim(low - y_pad, high + y_pad)
        if high - low <= 24:
            ax.yaxis.set_major_locator(ticker.MultipleLocator(2))
        else:
            ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=7))

        if series.goal_kg is not None:
            ax.axhline(
                series.goal_kg,
                xmin=0.0,
                xmax=1.0,
                color=_BODYWEIGHT_GOAL,
                linewidth=1.2,
                linestyle=(0, (2, 3)),
                alpha=0.9,
            )
            ax.text(
                0.76,
                series.goal_kg,
                f"goal {series.goal_kg:g}kg",
                transform=ax.get_yaxis_transform(),
                ha="center",
                va="bottom",
                fontsize=9.5,
                color=_BODYWEIGHT_GOAL,
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": _CHART_BG,
                    "edgecolor": "none",
                    "alpha": 0.92,
                },
            )

        ax.plot(
            series.logged_when,
            [0.012] * len(series.logged_when),
            "|",
            transform=ax.get_xaxis_transform(),
            color=_CHART_MUTED,
            markersize=7,
            alpha=0.8,
            zorder=2,
        )
        ax.annotate(
            f"{series.trend_kg[-1]:.1f}kg",
            (series.trend_when[-1], series.trend_kg[-1]),
            xytext=(10, 23),
            textcoords="offset points",
            va="bottom",
            fontsize=12,
            fontweight="bold",
            color=_BODYWEIGHT_TREND,
        )
        ax.annotate(
            f"{series.trend_when[-1]:%d %b %Y}",
            (series.trend_when[-1], series.trend_kg[-1]),
            xytext=(10, 7),
            textcoords="offset points",
            va="bottom",
            fontsize=9.5,
            fontweight="bold",
            color=_CHART_INK,
            bbox={
                "boxstyle": "round,pad=0.22",
                "facecolor": _CHART_BG,
                "edgecolor": "none",
                "alpha": 0.92,
            },
        )

        ax.set_ylabel("kg", color=_CHART_MUTED)
        legend = ax.legend(
            loc="upper right",
            frameon=False,
            fontsize=9.5,
            labelcolor=_CHART_MUTED,
        )
        for text in legend.get_texts():
            text.set_color(_CHART_MUTED)

        # A 0.5–1.0% weekly change band gives the rate panel practical context.
        # It follows goal direction, so the same chart remains useful for a
        # deliberate bulk without labelling all gain as failure.
        current = series.trend_kg[-1]
        band_slow = current * 0.5 / 100.0
        band_fast = current * 1.0 / 100.0
        if series.goal_kg is not None:
            direction = -1 if series.goal_kg < current else 1
        else:
            direction = -1 if trend_delta <= 0 else 1
        if direction < 0:
            band_bottom, band_top = -band_fast, -band_slow
        else:
            band_bottom, band_top = band_slow, band_fast
        rate_ax.fill_between(
            [left_edge, right_edge],
            [band_bottom, band_bottom],
            [band_top, band_top],
            color=_BODYWEIGHT_TREND,
            alpha=0.14,
            linewidth=0,
        )
        rate_ax.axhline(0, color=_CHART_MUTED, linewidth=0.9, alpha=0.65)

        rate_values = [
            float("nan") if value is None else value
            for value in series.rate_kg_week
        ]
        in_band = [
            value is not None and band_bottom <= value <= band_top
            for value in series.rate_kg_week
        ]
        outside_band = [
            value is not None and not inside
            for value, inside in zip(series.rate_kg_week, in_band)
        ]
        rate_ax.fill_between(
            series.trend_when,
            0,
            rate_values,
            where=outside_band,
            interpolate=True,
            color=_BODYWEIGHT_WARN,
            alpha=0.58,
            linewidth=0,
        )
        rate_ax.fill_between(
            series.trend_when,
            0,
            rate_values,
            where=in_band,
            interpolate=True,
            color=_BODYWEIGHT_TREND,
            alpha=0.58,
            linewidth=0,
        )
        rate_ax.plot(
            series.trend_when,
            rate_values,
            color=_CHART_INK,
            linewidth=1.5,
            alpha=0.85,
        )

        finite_rates = [
            value for value in series.rate_kg_week if value is not None
        ]
        if finite_rates:
            final_rate = finite_rates[-1]
            final_ok = band_bottom <= final_rate <= band_top
            final_colour = (
                _BODYWEIGHT_TREND if final_ok else _BODYWEIGHT_WARN
            )
            rate_ax.plot(
                [series.trend_when[-1]],
                [final_rate],
                "o",
                markersize=6,
                color=final_colour,
                zorder=4,
            )
            rate_ax.annotate(
                f"{final_rate:+.2f}",
                (series.trend_when[-1], final_rate),
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                fontsize=10,
                color=final_colour,
                fontweight="bold",
            )
        rate_lows = [*finite_rates, band_bottom, 0.0]
        rate_highs = [*finite_rates, band_top, 0.0]
        rate_ax.set_ylim(min(rate_lows) - 0.15, max(rate_highs) + 0.12)
        rate_ax.set_ylabel("kg/wk", color=_CHART_MUTED, fontsize=9.5)
        rate_ax.text(
            0.996,
            0.90,
            "14-day rate  ·  target zone: 0.5–1.0% bodyweight/wk",
            transform=rate_ax.transAxes,
            fontsize=9,
            color=_CHART_MUTED,
            ha="right",
            va="top",
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": _CHART_BG,
                "edgecolor": "none",
                "alpha": 0.85,
            },
        )

        locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
        rate_ax.xaxis.set_major_locator(locator)
        rate_ax.xaxis.set_major_formatter(
            mdates.ConciseDateFormatter(locator),
        )
        rate_ax.tick_params(axis="x", labelsize=11, pad=7)

        # Keep the exact latest raw reading available without competing with
        # the smoother endpoint label; it matters when several readings were
        # averaged on the final day.
        if (
            latest_recorded_kg is not None
            and abs(latest_recorded_kg - series.logged_kg[-1]) >= 0.05
        ):
            ax.text(
                0.006,
                0.985,
                f"latest logged {latest_recorded_kg:g}kg",
                transform=ax.transAxes,
                fontsize=8.5,
                color=_CHART_MUTED,
                va="top",
            )

        fig.subplots_adjust(
            top=0.885,
            bottom=0.075,
            left=0.077,
            right=0.923,
        )
        buffer = io.BytesIO()
        fig.savefig(
            buffer,
            format="png",
            dpi=140,
            facecolor=fig.get_facecolor(),
        )
        buffer.seek(0)
        return buffer
    finally:
        plt.close(fig)


def _render_trend_chart_threadsafe(
    xs: list,
    ys: list[float],
    trend: list[float],
    **kwargs,
) -> "io.BytesIO":
    """Load and use Matplotlib under its one process-wide rendering lock."""
    with _CHART_RENDER_LOCK:
        matplotlib = importlib.import_module("matplotlib")
        matplotlib.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
        mdates = importlib.import_module("matplotlib.dates")
        ticker = importlib.import_module("matplotlib.ticker")
        return _render_trend_chart(
            plt, mdates, ticker, xs, ys, trend, **kwargs,
        )


class _BodyweightChart(NamedTuple):
    """A rendered bodyweight graph plus the numbers used in its caption."""

    buffer: io.BytesIO
    filename: str
    entries: int
    days: int
    first_kg: float
    latest_kg: float
    latest_recorded_kg: float
    delta_kg: float


def _build_bodyweight_chart(
    user_id: int, display_name: str,
) -> _BodyweightChart | None:
    """Render the current global bodyweight history for one member.

    This is synchronous so it can be used by ``asyncio.to_thread``. It returns
    ``None`` only when no usable dated history exists and deliberately lets an
    ``ImportError`` escape so the explicit graph command can explain a missing
    Matplotlib install while automatic updates quietly keep their text reply.
    """
    rows = db.bodyweight_history(0, user_id, limit=1000)
    points = bodycomp_daily_points(
        ((r["recorded_at"], float(r["weight_kg"])) for r in rows),
        DISPLAY_TZ,
    )
    if not points:
        return None

    xs = [point.when for point in points]
    ys = [point.value for point in points]
    delta = ys[-1] - ys[0]
    latest_recorded = float(rows[-1]["weight_kg"])
    averaged = len(rows) > len(points)
    sign = "+" if delta >= 0 else ""
    if len(points) == 1:
        span = (
            f"daily mean {ys[-1]:g}kg · latest logged {latest_recorded:g}kg"
            if averaged else
            f"latest {latest_recorded:g}kg"
        )
    else:
        prefix = "daily means " if averaged else ""
        span = f"{prefix}{ys[0]:g}kg → {ys[-1]:g}kg ({sign}{delta:g}kg)"
        if averaged and latest_recorded != ys[-1]:
            span += f" · latest logged {latest_recorded:g}kg"
    subtitle = (
        f"{_plural(len(rows), 'entry', 'entries')} · "
        f"{_plural(len(points), 'day')} · {span}"
    )

    goal_row = db.bodyweight_goal_get(user_id)
    goal_kg = float(goal_row["target_kg"]) if goal_row is not None else None
    series = bodyweight_trend(xs, ys, goal_kg=goal_kg)
    buf = _render_trend_chart_threadsafe(
        xs, ys, series.logged_trend_kg,
        title=f"{_plain_label(display_name, limit=80)} — bodyweight",
        subtitle=subtitle,
        trend_label="7-day EWMA trend",
        trend_colour=_BODYWEIGHT_TREND,
        bodyweight=series,
        entries=len(rows),
        logged_days=len(points),
        latest_recorded_kg=latest_recorded,
    )

    safe_name = re.sub(
        r"[^a-z0-9_-]+", "_", display_name.lower(),
    ).strip("_") or "member"
    return _BodyweightChart(
        buffer=buf,
        filename=f"bodyweight_{safe_name}.png",
        entries=len(rows),
        days=len(points),
        first_kg=ys[0],
        latest_kg=ys[-1],
        latest_recorded_kg=latest_recorded,
        delta_kg=delta,
    )


async def _updated_bodyweight_chart(
    user_id: int, display_name: str,
) -> _BodyweightChart | None:
    """Best-effort refreshed chart for a successful bodyweight update."""
    try:
        return await asyncio.to_thread(
            _build_bodyweight_chart, user_id, display_name,
        )
    except ImportError:
        LOG.info(
            "Bodyweight graph skipped for %s: matplotlib is unavailable",
            user_id,
        )
    except Exception:  # pragma: no cover - a chart must not undo a stored weight
        LOG.exception("Failed to render bodyweight graph for %s", user_id)
    return None


def _bodyweight_chart_file(chart: _BodyweightChart) -> discord.File:
    """Turn one fresh chart buffer into the single-use Discord attachment."""
    chart.buffer.seek(0)
    return discord.File(chart.buffer, filename=chart.filename)


def _attachment_retryable(exc: discord.HTTPException) -> bool:
    """Whether retrying the same message without its file can succeed."""
    return getattr(exc, "status", None) in {400, 403, 413}


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
    target = user or interaction.user
    await interaction.response.defer(thinking=True)
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
    canon = _resolve(guild_id, equipment)
    rows = db.history(guild_id, target.id, canon, limit=1000)
    if not rows:
        await interaction.followup.send(
            f"No {canon} history for {target.display_name}.", ephemeral=True
        )
        return

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
    peak = max(ys)
    net = ys[-1] - ys[0]
    subtitle = (
        f"{_plural(len(rows), 'log')} · {_plural(len(points), 'day')} "
        f"· peak {peak:g}kg"
    )
    # For a lift the meaningful trend line is the personal best over time, not
    # a rolling mean: a heavy day followed by a deload isn't noise to smooth
    # away, and the number people care about is the best they've hit.
    try:
        buf = await asyncio.to_thread(
            _render_trend_chart_threadsafe,
            xs, ys, running_best,
            title=f"{_display_name(target)} — {canon}",
            subtitle=subtitle,
            trend_label="best to date",
            trend_colour=f"#{ui.score_trend(net).value:06x}",
        )
    except ImportError:
        await interaction.followup.send(
            "Graphing isn't available — matplotlib isn't installed. "
            "Add it to `requirements.txt` and redeploy.",
            ephemeral=True,
        )
        return
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
    if await _deny_invisible_target(interaction, target):
        return
    guild_id = _ctx_guild_id(interaction)
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
    user: discord.Member | discord.User | None = None,
) -> None:
    target = user or interaction.user
    # Visibility, SQLite and the chart backend can all wait; acknowledge before
    # any of them so this command is safe on a cold process and in DMs.
    await interaction.response.defer(thinking=True)
    if await _deny_invisible_target(interaction, target):
        return

    try:
        chart = await asyncio.to_thread(
            _build_bodyweight_chart, target.id, _display_name(target),
        )
    except ImportError:
        await interaction.followup.send(
            "Graphing isn't available — matplotlib isn't installed. "
            "Add it to `requirements.txt` and redeploy.",
            ephemeral=True,
        )
        return
    except Exception:  # pragma: no cover - defensive runtime path
        LOG.exception("Failed to render bodyweight graph for %s", target.id)
        await interaction.followup.send(
            "Couldn't render that bodyweight graph right now.",
            ephemeral=True,
        )
        return
    if chart is None:
        await interaction.followup.send(
            f"No bodyweight entries logged for {target.display_name} yet. "
            "Set one with `/bodyweight <weight_kg>`.",
            ephemeral=True,
        )
        return

    averaged = chart.entries > chart.days
    if chart.days == 1:
        summary = (
            f"latest logged {chart.latest_recorded_kg:g}kg · "
            f"day average {chart.latest_kg:g}kg"
            if averaged else
            f"latest {chart.latest_recorded_kg:g}kg · first recorded day"
        )
    else:
        sign = "+" if chart.delta_kg >= 0 else ""
        summary = (
            f"{'daily means ' if averaged else ''}"
            f"{chart.first_kg:g}kg → {chart.latest_kg:g}kg, "
            f"{sign}{chart.delta_kg:g}kg"
        )
        if averaged and chart.latest_recorded_kg != chart.latest_kg:
            summary += f" · latest logged {chart.latest_recorded_kg:g}kg"
    await interaction.followup.send(
        f"⚖️ **{target.display_name} — bodyweight** "
        f"({summary})",
        file=_bodyweight_chart_file(chart),
    )


# Sanity bounds for a bodyweight target — same range the weigh-in parser
# accepts, so a goal can never sit outside what can actually be logged.
_BW_GOAL_MIN_KG = 30.0
_BW_GOAL_MAX_KG = 300.0

# Match the graph's projection fit: an older bulk/cut phase should not steer
# the ETA for what the member's weight is doing now.
_BW_TREND_WINDOW_DAYS = 28


def _bodyweight_goal_status(user_id: int, display_name: str) -> str:
    """Status + ETA text for a user's bodyweight goal (assumes goal exists)."""
    goal = db.bodyweight_goal_get(user_id)
    target_kg = float(goal["target_kg"])
    rows = db.bodyweight_history(0, user_id)
    points = bodycomp_daily_points(
        ((row["recorded_at"], float(row["weight_kg"])) for row in rows),
        DISPLAY_TZ,
    )
    if not points:
        return (
            f"🎯 Goal: **{target_kg:g} kg** — no weigh-ins yet, so no "
            "projection. Log one with `/bodyweight` or `bw 82.4` in chat."
        )
    series = bodyweight_trend(
        [point.when for point in points],
        [point.value for point in points],
        goal_kg=target_kg,
    )
    trend_kg = series.trend_kg[-1]
    latest_kg = float(rows[-1]["weight_kg"])
    to_go = target_kg - trend_kg
    current = f"trend {trend_kg:.1f} kg"
    if abs(latest_kg - trend_kg) >= 0.05:
        current += f", latest logged {latest_kg:g} kg"
    head = (
        f"🎯 **{display_name}** — bodyweight goal **{target_kg:g} kg** "
        f"({current}, {to_go:+.1f} kg to go)"
    )
    rate = series.projection_rate_kg_week
    eta = series.goal_eta
    if eta is not None and rate is not None:
        weeks = max(0.0, (eta - datetime.now(DISPLAY_TZ).date()).days / 7.0)
        return (
            f"{head}\nTrend over the last {_BW_TREND_WINDOW_DAYS} days: "
            f"**{rate:+.2f} kg/week** → on track for about "
            f"**{eta.strftime('%d %b %Y')}** (~{weeks:.0f} weeks)."
        )
    if abs(to_go) < 0.05:
        return f"{head}\nAlready at target."
    if rate is None:
        return (
            f"{head}\nNeed at least three dated weigh-ins in the last "
            f"{_BW_TREND_WINDOW_DAYS} days to project an ETA."
        )
    direction = "down" if to_go < 0 else "up"
    if (to_go > 0) != (rate > 0):
        return (
            f"{head}\nTrend: **{rate:+.2f} kg/week** — weight isn't trending "
            f"{direction} yet, so there is no ETA."
        )
    return (
        f"{head}\nTrend: **{rate:+.2f} kg/week** — at the current rate the "
        "goal is over two years away, so the ETA is too uncertain to show."
    )


@bot.tree.command(
    name="bodyweight_goal",
    description="Set a bodyweight target and see your projected ETA from the trend.",
)
@app_commands.describe(
    target_kg="Your goal bodyweight in kg (leave blank to view progress).",
    remove="Clear your bodyweight goal.",
)
async def bodyweight_goal_cmd(
    interaction: discord.Interaction,
    target_kg: float | None = None,
    remove: bool = False,
) -> None:
    user_id = interaction.user.id
    if remove:
        cleared = db.bodyweight_goal_remove(user_id)
        await interaction.response.send_message(
            "🗑️ Bodyweight goal cleared." if cleared
            else "You didn't have a bodyweight goal set.",
            ephemeral=True,
        )
        return
    if target_kg is None:
        if db.bodyweight_goal_get(user_id) is None:
            await interaction.response.send_message(
                "No bodyweight goal set — run `/bodyweight_goal "
                "target_kg:<kg>` to set one.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            _bodyweight_goal_status(user_id, _display_name(interaction.user)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if not (_BW_GOAL_MIN_KG <= target_kg <= _BW_GOAL_MAX_KG):
        await interaction.response.send_message(
            f"That target needs to be {_BW_GOAL_MIN_KG:g}–"
            f"{_BW_GOAL_MAX_KG:g} kg.",
            ephemeral=True,
        )
        return
    db.bodyweight_goal_set(user_id, _display_name(interaction.user), target_kg)
    await interaction.response.send_message(
        _bodyweight_goal_status(user_id, _display_name(interaction.user))
        + "\nCheck back with `/bodyweight_goal` any time — the ETA re-reads "
        "your latest weigh-ins.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---- autocomplete for equipment names ------------------------------------


async def _equipment_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest equipment names, prioritising ones the invoking user has
    logged recently so their own lifts are a single key-tap away."""
    guild_id = _ctx_guild_id(interaction)
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

# REVO_DISABLED, REVO_POLL_MINUTES and REVO_DEFAULT_NOTIFY_CHANNEL_ID are bound
# by _bind_config() near the top of this file. REVO_USER, REVO_PASS and
# REVO_FERNET_KEY stay environment-only: app/revo_client.py reads them directly
# with os.getenv and is deliberately not touched by the config refactor, so the
# supervisor exports the resolved values into this process's environment.

# Per-user RevoClient cache so the poller doesn't construct + re-login a
# fresh session on every cycle.
_revo_user_clients: dict[int, "revo_client.RevoClient"] = {}
_revo_clients_lock = __import__("threading").Lock()


def _revo_off_embed() -> discord.Embed:
    """The Revo-is-switched-off gate, in one place.

    Twelve commands each carried their own copy in two different wordings, and
    four of those pasted the env var name into a message shown to members who
    have no way to set it. Configuration names belong in the admin field.
    """
    return ui.unavailable(
        "Revo Fitness",
        why="The gym integration is switched off, so club occupancy, "
            "check-in streaks and member cards aren't available.",
        admin_fix="Clear `REVO_DISABLED` and restart the bot.",
    )


def _revo_missing_deps_embed(*, crypto: bool = False) -> discord.Embed:
    """Revo is enabled but its optional dependencies aren't installed."""
    packages = "`requests` and `cryptography`" if crypto else "`requests`"
    return ui.unavailable(
        "Revo Fitness",
        why="The gym integration is enabled but can't run on this host yet.",
        admin_fix=f"Install {packages}, then restart the bot.",
    )


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


# Netpulse (EGYM mobile backend) sessions are a *separate* login from the web
# portal client above — different host + cookie — so they get their own cache,
# keyed the same way and built from the same stored credentials.
_revo_netpulse_clients: dict[int, "revo_netpulse.NetpulseClient"] = {}


def _netpulse_client_for_user(row) -> "revo_netpulse.NetpulseClient":
    """Return a cached NetpulseClient for a linked-account row."""
    user_id = int(row["user_id"])
    with _revo_clients_lock:
        client = _revo_netpulse_clients.get(user_id)
        if client is None:
            password = revo_client.decrypt_password(row["password_enc"])
            client = revo_netpulse.NetpulseClient(row["email"], password)
            _revo_netpulse_clients[user_id] = client
    return client


# PerfectGym (ClientPortal2) sessions are a *third* separate login — different
# host + cookie again (the live-occupancy backend behind /busy). Same cache
# pattern, keyed by user and built from the same stored credentials.
_revo_perfectgym_clients: dict[int, "revo_perfectgym.PerfectGymClient"] = {}


def _perfectgym_client_for_user(row) -> "revo_perfectgym.PerfectGymClient":
    """Return a cached PerfectGymClient for a linked-account row."""
    user_id = int(row["user_id"])
    with _revo_clients_lock:
        client = _revo_perfectgym_clients.get(user_id)
        if client is None:
            password = revo_client.decrypt_password(row["password_enc"])
            client = revo_perfectgym.PerfectGymClient(row["email"], password)
            _revo_perfectgym_clients[user_id] = client
    return client


def _drop_cached_client(user_id: int) -> None:
    # Drop all three backend sessions together: an auth failure on one usually
    # means the stored password changed, which invalidates the others too.
    with _revo_clients_lock:
        _revo_user_clients.pop(user_id, None)
        _revo_netpulse_clients.pop(user_id, None)
        _revo_perfectgym_clients.pop(user_id, None)


def _unique_nickname(first_name: str, user_id: int) -> str:
    """Return a nickname based on ``first_name`` that no OTHER member holds.

    Why disambiguate: auto-populated PerfectGym first names collide (two
    'Josh'es), and the same ``user_nicknames`` table drives chat lift
    attribution (:func:`_resolve_nickname_target`). A bare-name write on a
    collision would make the resolver log one member's ``Josh squat 150kg``
    under whichever row it matches first — silently misattributing, with the
    second member unable to ever self-attribute. We append the smallest numeric
    discriminator that makes the name unique — ``Josh``, ``Josh 2``, ``Josh 3``
    — checked case-insensitively to mirror the resolver's prefix match. The
    member's OWN existing row never counts as a collision, so re-linking is
    idempotent (a returning member keeps their current name).
    """
    base = first_name.strip()
    candidate = base
    suffix = 2
    while True:
        owner = db.nickname_owner(candidate)
        if owner is None or owner == user_id:
            return candidate
        candidate = f"{base} {suffix}"
        suffix += 1


def _apply_perfectgym_nickname(user_id: int) -> "str | None":
    """Best-effort: set the bot-wide nickname to the member's PerfectGym first name.

    Called right after a successful ``/revo_link`` (inside the executor, since it
    logs in). ALWAYS overwrites this member's existing nickname (owner-approved)
    so the display name tracks the freshly-linked account — ``db.set_user_nickname``
    is an upsert, so this is a plain overwrite. The first name is first
    disambiguated against OTHER members via :func:`_unique_nickname` so two
    members sharing a first name never collide (which would misattribute
    nickname-targeted chat lifts). Fully non-fatal: if the account row is gone,
    the first name can't be fetched, or the write fails, it returns ``None`` and
    leaves any existing nickname untouched rather than raising. Uses the member's
    OWN per-user client (never the shared account). Returns the applied nickname
    (possibly with a discriminator), or ``None`` when nothing was set.
    """
    row = db.get_revo_account(user_id)
    if row is None:
        return None
    try:
        first_name = _perfectgym_client_for_user(row).get_first_name()
    except Exception:  # pragma: no cover - network/creds; non-fatal by design
        LOG.warning("revo_link: PerfectGym first-name fetch failed", exc_info=True)
        return None
    if not first_name:
        return None
    nickname = _unique_nickname(first_name, user_id)
    try:
        db.set_user_nickname(user_id, nickname, user_id)
    except Exception:  # pragma: no cover - db; non-fatal by design
        LOG.warning("revo_link: set_user_nickname failed", exc_info=True)
        return None
    return nickname


def _seeprofile_gather(rows) -> "tuple[list[tuple[int, str | None, bytes]], int]":
    """Fetch each linked member's OWN photo + first name, downloading bytes now.

    Runs synchronously (N logins + N downloads) — call it via ``run_in_executor``.
    For EACH row it builds that member's OWN per-user PerfectGym client (never the
    shared account, and never one member's client for another member's photo),
    forces a fresh login for a currently-valid signed photo URL, then downloads the
    image bytes immediately (the signed URL expires in ~10 min, so we never hand
    the raw URL to Discord). Returns ``(results, failure_count)`` where results is a
    list of ``(user_id, first_name, image_bytes)``.

    Resilience + privacy: a per-member failure (login/creds error, no photo, or a
    download error) is counted and skipped so one bad account never sinks the whole
    roster. The signed photo URL is a capability URL — it is NEVER logged (nor are
    the bytes); only the exception *type name* is logged, because a ``requests``
    HTTP error's message embeds the signed URL.
    """
    results: "list[tuple[int, str | None, bytes]]" = []
    failures = 0
    for row in rows:
        try:
            uid = int(row["user_id"])
            client = _perfectgym_client_for_user(row)  # this member's OWN creds
            # refresh=True → re-login so the signature is fresh; download at once.
            url = client.get_photo_url(refresh=True)
            if not url:
                failures += 1
                continue
            first_name = client.get_first_name()
            data = revo_perfectgym.download_photo(url)  # url never logged
            if not data:
                failures += 1
                continue
            results.append((uid, first_name, data))
        except Exception as exc:
            # NEVER log the exception message/exc_info here: a requests HTTPError
            # embeds the signed capability URL. The type name is safe + enough.
            LOG.warning("seeprofile: skipped a member (%s)", type(exc).__name__)
            failures += 1
    return results, failures


def _seeprofile_display_name(
    interaction: discord.Interaction, uid: int, first_name: "str | None"
) -> str:
    """Label for a roster embed: the PerfectGym first name, else the guild name.

    Prefers the member's PerfectGym ``first_name`` (what they're actually called);
    falls back to their guild display name, then a global username, then a plain
    ``Member`` so the embed always has a title even for an uncached user.
    """
    if first_name:
        return first_name
    guild = interaction.guild
    if guild is not None:
        member = guild.get_member(uid)
        if member is not None:
            return member.display_name
    user = bot.get_user(uid)
    if user is not None:
        return user.display_name
    return "Member"


def _photo_file_ext(data: bytes) -> str:
    """Best-effort image extension from the leading magic bytes (default ``jpg``).

    Discord renders an ``attachment://`` embed image by the attachment's filename
    extension, so we match it to the actual bytes (PerfectGym photos are usually
    JPEG). Falls back to ``jpg`` for anything unrecognised — good enough for a
    thumbnail and never fatal.
    """
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _format_busy_line(landing: "revo_client.RewardsLanding") -> str:
    """Degraded single-club line from the web rewards landing (fallback source)."""
    name = landing.fav_club_name or "Your club"
    state = revo_client.state_for_club(landing.fav_club_name or "")
    location = f"{name}, {state}" if state else name
    if landing.in_club is None:
        return f"🏋️ **{location}** — live count unavailable right now."
    return f"🏋️ **{location}**: {landing.in_club} in club right now"


def _format_busy_club_line(club: "revo_perfectgym.ClubOccupancy") -> str:
    """Live count for one club, adding "X% of Y capacity" only when a cap exists."""
    location = f"{club.name}, {club.state}" if club.state else club.name
    line = f"🏋️ **{location}**: {club.count} in club right now"
    # UsersLimit is null for almost every club — only show the percentage when
    # the backend actually gives a capacity (and it's a sane, non-zero number).
    if club.capacity:
        pct = round(club.count / club.capacity * 100)
        line += f" — {pct}% of {club.capacity} capacity"
    return line


def _format_busiest_board(
    clubs: list["revo_perfectgym.ClubOccupancy"],
    *,
    state: str | None = None,
    limit: int = 5,
) -> str:
    """Render a "Busiest right now" top-N board (scoped to *state* if given)."""
    top = revo_perfectgym.top_busiest(clubs, limit=limit, state=state)
    if not top:
        return ""
    scope = f" in {state}" if state else " nationwide"
    lines = [f"{ui.STREAK} **Busiest right now{scope}**"]
    for i, c in enumerate(top):
        lines.append(f"{ui.rank(i)} {c.name} — {c.count}")
    return "\n".join(lines)


def _busiest_board_field(
    embed: discord.Embed,
    clubs: list["revo_perfectgym.ClubOccupancy"],
    *,
    state: str | None = None,
    limit: int = 5,
) -> bool:
    """Add the busiest-clubs board to *embed*. Returns False if there's nothing.

    Bars are scaled to the busiest club rather than to capacity: PerfectGym
    leaves ``UsersLimit`` null for almost every club, so a percentage-of-
    capacity bar would be blank for nearly all of them. Relative-to-the-top
    still answers the question people actually ask — where is quiet right now.
    """
    top = revo_perfectgym.top_busiest(clubs, limit=limit, state=state)
    if not top:
        return False
    busiest = max(c.count for c in top) or 1
    ui.block(
        embed,
        f"Busiest right now{f' in {state}' if state else ' nationwide'}",
        ui.table(
            [
                [ui.rank(i, mono=True), _safe_label(c.name, limit=18),
                 str(c.count), ui.bar(c.count, busiest, width=8)]
                for i, c in enumerate(top)
            ],
            align="<<>",
            max_rows=limit,
        ),
    )
    return True


def _quietest_line(
    clubs: list["revo_perfectgym.ClubOccupancy"], *, state: str | None = None,
) -> str:
    """The least busy club in scope — the other half of "where should I go?"."""
    scoped = [
        c for c in clubs
        if not state or (c.state or "").upper() == state.upper()
    ]
    if not scoped:
        return ""
    quiet = min(scoped, key=lambda c: (c.count, c.name.lower()))
    return f"**{_safe_label(quiet.name)}** — {quiet.count} in club"


def _busy_state_embed(
    clubs: list["revo_perfectgym.ClubOccupancy"], state: str, *, limit: int = 5,
) -> discord.Embed:
    """`/busy state:SA` — the busiest 5 in one state, plus where's quiet.

    ``top_busiest`` has always accepted a state; nothing exposed it, so the
    board could only ever be scoped to whichever state your home club sits in.
    """
    scoped = [c for c in clubs if (c.state or "").upper() == state.upper()]
    if not scoped:
        present = sorted({(c.state or "").upper() for c in clubs if c.state})
        return ui.empty(
            f"No live counts for {state}",
            hint="Revo's board is currently reporting "
                 + (", ".join(present) if present else "no states") + ".",
        )
    total = sum(c.count for c in scoped)
    embed = ui.card(
        f"{ui.LIFT} Busiest Revo clubs in {state}",
        description=f"**{total:,}** people across "
                    f"{ui.plural(len(scoped), 'club')} right now",
        colour=ui.BRAND,
        footer="live counts · Revo",
        timestamp=True,
    )
    _busiest_board_field(embed, scoped, state=state, limit=limit)
    quiet = _quietest_line(scoped, state=state)
    if quiet:
        ui.block(embed, "Quietest right now", quiet)
    return embed


def _busy_fav_landing(user_id: int) -> "revo_client.RewardsLanding | None":
    """Web rewards-landing fav club for /busy — home-club identity, and the
    degraded single-club count when PerfectGym is down.

    A *linked* caller's OWN landing wins so /busy shows THEIR home club and
    scopes the busiest board to THEIR state; the shared env account is only the
    unlinked / degradation fallback. Preferring the shared account first (as this
    used to) stamps every linked user with the shared owner's club + state — a
    WA-heavy shared account would then scope every SA/VIC/NSW caller's board to
    WA. So try the caller's own credentials first, shared only if they're
    unlinked or their landing yields nothing usable.
    """
    row = db.get_revo_account(user_id)
    if row is not None:
        try:
            landing = revo_client.rewards_landing_with_client(_client_for_user(row))
            if landing.fav_club_name or landing.in_club is not None:
                return landing
        except revo_client.RevoAuthError:
            _drop_cached_client(user_id)
        except Exception:  # pragma: no cover - network
            LOG.exception("Revo per-user rewards-landing (busy identity) failed")
    try:
        landing = revo_client.shared_rewards_landing()
        if landing.fav_club_name or landing.in_club is not None:
            return landing
    except revo_client.RevoUnavailable:
        pass
    except Exception:  # pragma: no cover - network
        LOG.exception("Revo shared rewards-landing (busy identity) failed")
    return None


def _maps_link(lat: float | None, lng: float | None) -> str | None:
    """Google-Maps search URL for a lat/lng pair, or ``None`` when not geocoded.

    The club directory leaves ``lat``/``lng`` null for just-announced clubs, so a
    caller must tolerate ``None`` (and simply omit the link).
    """
    if lat is None or lng is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def _perfectgym_occupancy(user_id: int) -> list["revo_perfectgym.ClubOccupancy"]:
    """All-clubs live occupancy: shared env account first, then the caller's linked
    creds. Returns ``[]`` on any failure so callers degrade gracefully.

    The occupancy board is the *same public all-clubs list for everyone*, so the
    shared account is tried first (keeps it working for unlinked users) and a burst
    of calls reuses one fetch via the module TTL cache. Mirrors the resolution the
    /busy command used inline before it was hoisted here for reuse by /revo_clubs.

    Rows are then enriched against the club directory (see
    :func:`revo_perfectgym.enrich_occupancy`) to fill in the state the occupancy
    payload omits and to drop clubs that haven't opened yet.
    """
    try:
        clubs = revo_perfectgym.shared_club_occupancy()
        if clubs:
            return _enrich_occupancy(user_id, clubs)
    except revo_perfectgym.PerfectGymUnavailable:
        pass  # no shared account configured — try the user's own creds
    except revo_perfectgym.PerfectGymAuthError:
        LOG.warning("PerfectGym shared login failed (occupancy)")
    except Exception:  # pragma: no cover - network
        LOG.exception("PerfectGym shared occupancy fetch failed")

    row = db.get_revo_account(user_id)
    if row is not None:
        try:
            client = _perfectgym_client_for_user(row)
            clubs = revo_perfectgym.club_occupancy_with_client(client)
            if clubs:
                return _enrich_occupancy(user_id, clubs)
        except revo_perfectgym.PerfectGymAuthError:
            _drop_cached_client(user_id)
            LOG.warning("PerfectGym per-user login failed (occupancy)")
        except Exception:  # pragma: no cover - network
            LOG.exception("PerfectGym per-user occupancy fetch failed")
    return []


def _enrich_occupancy(
    user_id: int, clubs: list["revo_perfectgym.ClubOccupancy"],
) -> list["revo_perfectgym.ClubOccupancy"]:
    """Join live counts to the directory for state + open-yet, best-effort.

    The directory is separately cached for 6h, so this is nearly always free. It is
    strictly an improvement pass: if the directory is unavailable the raw board is
    returned unchanged, exactly as before, rather than failing the command.
    """
    try:
        directory = _perfectgym_directory(user_id)
    except Exception:  # pragma: no cover - defensive; _perfectgym_directory catches
        return clubs
    if not directory:
        return clubs
    return revo_perfectgym.enrich_occupancy(clubs, directory)


def _perfectgym_directory(user_id: int) -> list["revo_perfectgym.ClubDirEntry"]:
    """Public club directory (ids + geo + opening dates): shared account first,
    then the caller's linked creds. Returns ``[]`` on any failure.

    The directory is public (no PII) and identical for everyone, so — like the
    occupancy board — the shared account is fine and a per-user fetch safely
    populates the shared long-TTL cache. Best-effort: a directory outage degrades
    /revo_clubs and the /busy maps-link enrichment, never hard-errors them.
    """
    try:
        entries = revo_perfectgym.shared_club_list()
        if entries:
            return entries
    except revo_perfectgym.PerfectGymUnavailable:
        pass  # no shared account configured — try the user's own creds
    except revo_perfectgym.PerfectGymAuthError:
        LOG.warning("PerfectGym shared login failed (directory)")
    except Exception:  # pragma: no cover - network
        LOG.exception("PerfectGym shared directory fetch failed")

    row = db.get_revo_account(user_id)
    if row is not None:
        try:
            client = _perfectgym_client_for_user(row)
            entries = revo_perfectgym.club_list_with_client(client)
            if entries:
                return entries
        except revo_perfectgym.PerfectGymAuthError:
            _drop_cached_client(user_id)
            LOG.warning("PerfectGym per-user login failed (directory)")
        except Exception:  # pragma: no cover - network
            LOG.exception("PerfectGym per-user directory fetch failed")
    return []


def _find_dir_entry(
    directory: list["revo_perfectgym.ClubDirEntry"],
    query: str,
) -> "revo_perfectgym.ClubDirEntry | None":
    """Case-insensitive club lookup over the directory (name then city).

    Mirrors :func:`revo_perfectgym.find_club` (exact → prefix → substring) but over
    :class:`ClubDirEntry`, so /revo_clubs can resolve a named club even when the
    live occupancy board is temporarily down (directory is separately cached).
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    def keys(e: "revo_perfectgym.ClubDirEntry") -> tuple[str, ...]:
        return tuple(k.lower() for k in (e.name, e.city or "") if k)

    for e in directory:  # exact
        if any(k == q for k in keys(e)):
            return e
    for e in directory:  # prefix
        if any(k.startswith(q) for k in keys(e)):
            return e
    for e in directory:  # substring
        if any(q in k for k in keys(e)):
            return e
    return None


# Spellings we accept for an Australian state, mapped to the code the club
# directory uses. Keyed by what a member might realistically type into the
# `state` option — full names, the dotted form, and the two territories that
# Revo doesn't operate in yet (so they get "no clubs there" rather than a
# confusing "unknown state"). Extend alongside _CLUB_NAMES_BY_STATE.
_REVO_STATE_ALIASES: dict[str, str] = {
    "sa": "SA", "s.a.": "SA", "south australia": "SA", "southaustralia": "SA",
    "wa": "WA", "w.a.": "WA", "western australia": "WA",
    "westernaustralia": "WA",
    "vic": "VIC", "v.i.c.": "VIC", "victoria": "VIC",
    "nsw": "NSW", "n.s.w.": "NSW", "new south wales": "NSW",
    "newsouthwales": "NSW",
    "qld": "QLD", "queensland": "QLD",
    "tas": "TAS", "tasmania": "TAS",
    "nt": "NT", "northern territory": "NT",
    "act": "ACT", "australian capital territory": "ACT",
}


def _normalise_state(text: str | None) -> str | None:
    """Map free-form state text to a directory state code, or None.

    Accepts the code itself, the full state name, and the dotted form, all
    case- and space-insensitively — so `sa`, `SA`, `S.A.` and
    `South Australia` all resolve to ``"SA"``.
    """
    key = " ".join((text or "").strip().lower().split())
    if not key:
        return None
    return _REVO_STATE_ALIASES.get(key)


def _revo_states_present(
    directory: list["revo_perfectgym.ClubDirEntry"],
) -> list[str]:
    """State codes that actually have clubs in *directory*, club-count desc.

    Driven by live directory data rather than the curated name→state table, so
    a state that Revo opens in shows up in autocomplete the moment its first
    club appears in the portal.
    """
    counts: dict[str, int] = {}
    for e in directory:
        st = (e.state or "").upper()
        if st:
            counts[st] = counts.get(st, 0) + 1
    return sorted(counts, key=lambda s: (-counts[s], s))


def _revo_clubs_state_lines(
    directory: list["revo_perfectgym.ClubDirEntry"],
    occupancy: list["revo_perfectgym.ClubOccupancy"],
    state: str,
) -> tuple[str, list[str], int | None]:
    """``(state_code, one line per club, total people in state)``.

    Joins the public directory to the live occupancy board *by name*, so each club
    shows its current head-count when the board is up (and "count unavailable" when
    it isn't). Alphabetical for a stable, scannable list. The total is None when
    the board is down for every club, so callers can omit it rather than print 0.

    A club Revo has announced but not opened is listed with its opening date
    instead of a count. It is genuinely useful to see ("a gym is coming to my
    suburb"), but it must not read as a live number: the directory carries these
    clubs early and the occupancy board reports ``0`` for them, so left alone they
    look like the emptiest gym in the state.
    """
    st = state.strip().upper()
    occ_by_name = {c.name.lower(): c for c in occupancy}
    entries = sorted(
        (e for e in directory if (e.state or "").upper() == st),
        key=lambda e: e.name.lower(),
    )
    lines: list[str] = []
    total: int | None = None
    for e in entries:
        occ = occ_by_name.get(e.name.lower())
        suburb = e.city or (occ.suburb if occ else None)
        if not revo_perfectgym.has_opened(e):
            count_txt = f"opens {_format_opening_date(e.opening_date)}"
        elif occ is not None:
            count_txt = f"{occ.count} in club now"
            total = (total or 0) + occ.count
        else:
            count_txt = "count unavailable"
        if suburb and suburb.lower() != e.name.lower():
            lines.append(f"• **{e.name}** — {suburb} ({count_txt})")
        else:
            lines.append(f"• **{e.name}** ({count_txt})")
    return st, lines, total


def _format_opening_date(opening_date: str | None) -> str:
    """A club ``OpeningDate`` as e.g. ``18 Aug 2026``, or ``"soon"`` if unusable.

    Formatted straight off the date part rather than through :func:`_format_date`:
    the directory's value is a *naive local* datetime, so reading it as UTC and
    converting to DISPLAY_TZ would shift the day backwards for eastern-state clubs.
    """
    if not opening_date:
        return "soon"
    try:
        return date.fromisoformat(opening_date[:10]).strftime("%d %b %Y").lstrip("0")
    except (ValueError, TypeError):
        return "soon"


def _format_revo_clubs_state_list(
    directory: list["revo_perfectgym.ClubDirEntry"],
    occupancy: list["revo_perfectgym.ClubOccupancy"],
    state: str,
) -> str:
    """Plain-text rendering of :func:`_revo_clubs_state_lines` (DM/fallback path)."""
    st, lines, _total = _revo_clubs_state_lines(directory, occupancy, state)
    if not lines:
        return f"🏋️ **Revo clubs in {st}**\n_No clubs found for this state._"
    return "\n".join([f"🏋️ **Revo clubs in {st}**", *lines])


def _revo_clubs_state_embed(
    directory: list["revo_perfectgym.ClubDirEntry"],
    occupancy: list["revo_perfectgym.ClubOccupancy"],
    state: str,
) -> discord.Embed:
    """The state directory as an embed, split into columns when it's long.

    WA alone is 37 clubs — as one text blob that is ~1.6k characters and one
    bad suburb name away from Discord's 2 000-char message cap. Splitting into
    up to three inline fields keeps every chunk far below the 1 024-char field
    limit and reads as a directory instead of a wall.
    """
    st, lines, total = _revo_clubs_state_lines(directory, occupancy, state)
    embed = discord.Embed(title=f"🏋️ Revo clubs in {st}", colour=EMBED_COLOUR)
    if not lines:
        embed.colour = discord.Colour(0x99AAB5)
        embed.description = (
            "No clubs here yet. Revo currently operates in "
            f"{', '.join(_revo_states_present(directory)) or 'no listed states'}."
        )
        return embed

    summary = _plural(len(lines), "club")
    if total is not None:
        summary += f" · **{total}** in club right now"
    embed.description = summary

    # One column up to 12 clubs, then two, then three — so a small state stays a
    # single readable list and a big one doesn't become a scroll.
    columns = 1 if len(lines) <= 12 else (2 if len(lines) <= 24 else 3)
    size = -(-len(lines) // columns)  # ceil, so the last column is the short one
    for i in range(0, len(lines), size):
        chunk = lines[i:i + size]
        embed.add_field(
            name="​", value=_clip_field("\n".join(chunk)), inline=columns > 1,
        )
    embed.set_footer(
        text="/revo_clubs club:<name> for one club's address, map & nearest gyms",
    )
    return embed


def _club_hours_entry(
    entry: "revo_perfectgym.ClubDirEntry",
) -> "revo_netpulse.Club | None":
    """The Netpulse directory row for a PerfectGym club, or None if unavailable.

    Opening hours, a phone number and the club's IANA timezone exist **only** on
    the Netpulse directory, which is public — no login, no shared account, nothing
    to lock out — so this is safe to call for any user. Best-effort throughout: a
    Netpulse outage, a missing dep, or a club Netpulse doesn't list yet (it only
    lists clubs once they trade) all return None and the caller omits the line.

    Matching is by normalised name against both `name` and `full_name`; the two
    Nunawading sites are 138 m apart, so a nearest-coordinate join would silently
    give one of them the other's hours.
    """
    if not revo_netpulse.available():
        return None
    try:
        clubs = revo_netpulse.shared_club_directory()
    except Exception:  # pragma: no cover - network
        LOG.warning("Netpulse club directory fetch failed", exc_info=True)
        return None
    index = revo_netpulse.index_clubs_by_name(clubs)
    for candidate in (entry.name, entry.full_name):
        key = revo_netpulse.normalise_club_name(candidate)
        if key and key in index:
            return index[key]
    return None


def _format_club_hours_line(club: "revo_netpulse.Club | None") -> str | None:
    """"🕒 Open 24 hours · staffed until 8pm" for one club, or None if unknown.

    Everything here is best-effort: an unknown timezone, an undescribed weekday or
    unparseable hours copy all yield ``None`` and the line is simply omitted. We
    never render "closed" from a guess — that's the one wrong answer that would
    actually cost someone a trip.
    """
    if club is None:
        return None
    status = revo_netpulse.club_status(club)
    if status.open_now is None and status.staffed_now is None:
        return None
    if status.always_open:
        parts = ["Open 24 hours"]
    elif status.open_now:
        parts = ["**Open now**"]
    elif status.open_now is False:
        parts = ["**Closed right now**"]
    else:
        parts = []
    if status.staffed_now is True:
        parts.append("staffed now")
    elif status.staffed_now is False:
        parts.append("unstaffed right now")
    if not parts:
        return None
    line = f"🕒 {' · '.join(parts)}"
    if status.today_raw:
        # The verbatim copy carries the actual windows ("Staffed from 9am - 8pm"),
        # which is more useful than re-rendering our parse of it.
        line += f"\n-# {status.today_raw}"
    return line


def _format_revo_club_detail(
    entry: "revo_perfectgym.ClubDirEntry",
    occupancy: list["revo_perfectgym.ClubOccupancy"],
    nearest: list["revo_perfectgym.ClubDirEntry"],
    hours_club: "revo_netpulse.Club | None" = None,
) -> str:
    """Render one club's card: address, maps link, state, live count + nearest 3.

    ``occupancy`` is the full live board (joined by name for the club's own count
    and each nearby club's count); ``nearest`` is :func:`revo_perfectgym.nearest_clubs`
    output. ``hours_club`` is the matching Netpulse directory row, the only source
    of opening hours and a phone number. Missing pieces (no geo, board down, no
    hours) are simply omitted.
    """
    occ_by_name = {c.name.lower(): c for c in occupancy}
    header = f"🏋️ **{entry.name}**"
    if entry.state:
        header += f" — {entry.state}"
    lines = [header]
    if entry.address:
        addr = entry.address
        if entry.city and entry.city.lower() not in addr.lower():
            addr = f"{addr}, {entry.city}"
        # Several PerfectGym addresses already end in the postcode, so only add it
        # when it isn't there (Netpulse's copy is normalised to 4 digits or None).
        if (
            hours_club is not None and hours_club.postal_code
            and hours_club.postal_code not in addr
        ):
            addr = f"{addr} {hours_club.postal_code}"
        lines.append(f"📍 {addr}")
    # Prefer Netpulse's coordinates for the pin. PerfectGym's directory rounds:
    # 15 clubs sit at ≤3 decimal places (≥110 m) and two at 2 (~1.1 km), which
    # puts 25 of 77 pins more than 200 m from the door and Pitt St nearly a
    # kilometre out. Netpulse carries 6–14 places for almost every club.
    lat, lng = entry.lat, entry.lng
    if hours_club is not None and hours_club.lat is not None:
        lat, lng = hours_club.lat, hours_club.lng
    link = _maps_link(lat, lng)
    if link:
        lines.append(f"🗺️ [Open in Google Maps]({link})")
    if not revo_perfectgym.has_opened(entry):
        lines.append(f"🚧 Not open yet — opens {_format_opening_date(entry.opening_date)}")
        return "\n".join(lines)
    hours_line = _format_club_hours_line(hours_club)
    if hours_line:
        lines.append(hours_line)
    own = occ_by_name.get(entry.name.lower())
    lines.append(
        f"👥 **{own.count}** in club right now" if own is not None
        else "👥 Live count unavailable right now"
    )
    if hours_club is not None and hours_club.phone:
        lines.append(f"☎️ {hours_club.phone}")
    if nearest:
        lines.append("")
        lines.append("📌 **Nearest other clubs**")
        for e in nearest:
            piece = f"• **{e.name}**"
            if (
                entry.lat is not None and entry.lng is not None
                and e.lat is not None and e.lng is not None
            ):
                km = revo_perfectgym.haversine_km(entry.lat, entry.lng, e.lat, e.lng)
                piece += f" — {km:.1f} km"
            near_occ = occ_by_name.get(e.name.lower())
            if near_occ is not None:
                piece += f" ({near_occ.count} in club now)"
            lines.append(piece)
    return "\n".join(lines)


def _format_membership_status_line(
    status: "revo_perfectgym.MembershipStatus | None",
) -> str | None:
    """"💳 Membership: {contract}" (+ ", payment issue ⚠" when payment isn't ok).

    Returns ``None`` when the contract status is unknown, so callers degrade
    silently rather than printing an empty/uninformative line.
    """
    if status is None or not status.contract_status:
        return None
    line = f"💳 Membership: {status.contract_status}"
    if status.payment_ok is False:
        line += ", payment issue ⚠"
    return line


def _summary_status_line(
    status: "revo_perfectgym.MembershipStatus | None",
    *,
    is_self: bool,
) -> str | None:
    """Contract-status line for /revo_summary — SELF-ONLY.

    /revo_summary replies PUBLICLY (it defers without ephemeral) and can be aimed
    at any other linked member, so this must never surface a THIRD PARTY's contract
    health: the rendered line can read "Suspended, payment issue ⚠" — a payment-
    failure / suspension flag that is materially more sensitive than the already-
    public membership tier. Returns ``None`` for a non-self lookup so only the
    caller ever sees their own contract/payment standing (mirrors the self-only,
    ephemeral /revo_card path).
    """
    if not is_self:
        return None
    return _format_membership_status_line(status)


def _revo_card_client_for_user(
    user_id: int,
) -> "revo_perfectgym.PerfectGymClient | None":
    """Resolve the PerfectGym client for /revo_card — the caller's OWN linked
    account ONLY. Returns ``None`` when the user hasn't linked.

    !!! CRITICAL SAFETY PROPERTY !!! /revo_card surfaces the physical entry
    BARCODE (an access credential). This resolver reads ONLY
    ``db.get_revo_account(user_id)`` and NEVER falls back to the shared
    ``REVO_USER`` account the way /busy does — falling back would hand one member
    the *host's* door barcode. Any change here must preserve "own creds only".
    """
    row = db.get_revo_account(user_id)
    if row is None:
        return None
    return _perfectgym_client_for_user(row)


def _render_card_barcode(number: str) -> "io.BytesIO | None":
    """Render *number* as a Code128 PNG for /revo_card, or ``None`` if the optional
    ``python-barcode`` lib is missing (the command then degrades to text).

    Lazily imported (mirrors the optional-dep pattern) so the bot boots without the
    lib. Code128 is used as the default symbology — the reply carries the "if it
    doesn't scan, use the Revo app" caveat. NEVER logs *number* (a physical access
    credential): the failure path logs a bare message with no value.
    """
    try:
        barcode_mod = importlib.import_module("barcode")
        writer_mod = importlib.import_module("barcode.writer")
    except Exception:
        return None
    try:
        code = barcode_mod.get("code128", number, writer=writer_mod.ImageWriter())
        buf = io.BytesIO()
        code.write(buf)  # PNG via the Pillow-backed ImageWriter
        buf.seek(0)
        return buf
    except Exception:  # pragma: no cover - defensive; render lib edge cases
        LOG.warning("revo_card: barcode render failed")  # no number in the log
        return None


async def _revo_state_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest state codes for /busy and /revo_clubs.

    Prefers the states actually present in the (6h-cached) directory so the list
    tracks Revo's real footprint; falls back to the curated table when the
    directory hasn't been fetched yet, since autocomplete must answer within
    3 seconds and can't afford a cold network round-trip.
    """
    try:
        states = _revo_states_present(revo_perfectgym.cached_club_list())
    except Exception:  # pragma: no cover - defensive; autocomplete must not raise
        states = []
    if not states:
        states = revo_client.known_states()
    q = (current or "").strip().lower()
    matched = [s for s in states if not q or s.lower().startswith(q)]
    if not matched:  # let a full name like "south australia" match too
        code = _normalise_state(current)
        matched = [code] if code in states else []
    return [app_commands.Choice(name=s, value=s) for s in matched[:25]]


@bot.tree.command(
    name="busy",
    description="Live Revo occupancy: your club, a state's top 5, or one club.",
)
@app_commands.describe(
    club="Optional club name/suburb. Omit to see your home club + the busiest board.",
    state="Busiest 5 clubs in a state (SA, WA, VIC, NSW). Defaults to your home state.",
)
@app_commands.autocomplete(state=_revo_state_autocomplete)
async def busy_cmd(
    interaction: discord.Interaction,
    club: str | None = None,
    state: str | None = None,
) -> None:
    # /busy reads Revo's real all-clubs live counter again — the same PerfectGym
    # ClientPortal2 backend the iOS app uses (docs/REVO_PORTAL.md §8). It replaces
    # the web club-counter.php board that was access-guarded in 2026-07. If
    # PerfectGym is unavailable we degrade to the web rewards-landing fav-club
    # count, then to a clear "temporarily unavailable" — /busy never hard-errors.
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_perfectgym.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    # Reject an unparseable state before spending a live fetch on it.
    wanted_state = _normalise_state(state)
    if state and wanted_state is None:
        await interaction.response.send_message(
            f"**{_safe_label(state)}** isn't an Australian state I know. Try "
            f"one of: {', '.join(revo_client.known_states())}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id

    def _busy_maps_link(occ: list["revo_perfectgym.ClubOccupancy"], name: str) -> str | None:
        """Best-effort Google-Maps link for the *named* club (geo enrichment).

        Joins the live board to the public club directory (``join_occupancy_to_dir``)
        so a named club can carry a maps link. The directory is separately cached and
        the whole thing is wrapped defensively — a directory outage silently yields no
        link, never breaking /busy.
        """
        directory = _perfectgym_directory(user_id)
        if not directory:
            return None
        located = {
            j.occupancy.name.lower(): j
            for j in revo_perfectgym.join_occupancy_to_dir(occ, directory)
        }.get(name.lower())
        return _maps_link(located.lat, located.lng) if located else None

    def _do() -> tuple[str | discord.Embed, bool]:
        """Build the /busy reply. Returns ``(message_or_embed, ephemeral)``."""
        # Shared env account first, then the caller's linked creds (hoisted so
        # /revo_clubs reuses the exact same resolution + TTL cache).
        occ = _perfectgym_occupancy(user_id)

        # ---- with a club argument: look that club up in the live board ----
        if club:
            # A bare state typed into the club box is a natural mistake, and
            # substring matching would answer "SA" with Salisbury.
            as_state = _normalise_state(club)
            if as_state and occ and not revo_perfectgym.find_club(occ, club):
                return _busy_state_embed(occ, as_state), False
            if occ:
                match = revo_perfectgym.find_club(occ, club)
                if match:
                    embed = ui.card(
                        f"{ui.LIFT} {_safe_label(match.name)}"
                        + (f" — {match.state}" if match.state else ""),
                        description=f"## {match.count} in club right now",
                        colour=ui.BRAND,
                        url=_busy_maps_link(occ, match.name),
                        footer="live count · Revo",
                        timestamp=True,
                    )
                    if match.capacity:
                        pct = match.count / match.capacity
                        ui.block(
                            embed, "Capacity",
                            f"`{ui.bar(match.count, match.capacity, width=12)}` "
                            f"{ui.pct(match.count, match.capacity)} of "
                            f"{match.capacity}",
                        )
                    if match.state:
                        _busiest_board_field(embed, occ, state=match.state)
                    return embed, False
                return (
                    f"Couldn't find a Revo club matching "
                    f"**{_safe_label(club)}**. Try the suburb name, or "
                    "`/busy state:` for a whole state's board.",
                    True,
                )
            # PerfectGym down — degrade to the fav-club web count if it's this club.
            landing = _busy_fav_landing(user_id)
            if landing and landing.fav_club_name:
                fav = landing.fav_club_name.lower()
                q = club.strip().lower()
                if q in fav or fav in q:
                    return (
                        _format_busy_line(landing)
                        + "\n_(live all-clubs board temporarily unavailable — "
                        "showing your linked club.)_",
                        False,
                    )
            return (
                "Revo's live counter is temporarily unavailable. Try again "
                "shortly, or check the Live Member Counter in the Revo app.",
                True,
            )

        # ---- an explicit state: that state's top 5, nothing personal ----
        if wanted_state:
            if occ:
                return _busy_state_embed(occ, wanted_state), False
            return (
                "Revo's live counter is temporarily unavailable. Try again "
                "shortly, or check the Live Member Counter in the Revo app.",
                True,
            )

        # ---- no argument: home club count + busiest board ----
        if occ:
            landing = _busy_fav_landing(user_id)
            home_name = landing.fav_club_name if landing else None
            home = revo_perfectgym.find_club(occ, home_name) if home_name else None
            home_state: str | None = None
            embed = ui.card(
                f"{ui.LIFT} Revo — live occupancy",
                colour=ui.BRAND,
                footer="live counts · Revo",
                timestamp=True,
            )
            if home:
                home_state = home.state
                embed.description = (
                    f"**{_safe_label(home.name)}**"
                    + (f" — {home.state}" if home.state else "")
                    + f"\n## {home.count} in club right now"
                )
                if home.capacity:
                    ui.block(
                        embed, "Capacity",
                        f"`{ui.bar(home.count, home.capacity, width=12)}` "
                        f"{ui.pct(home.count, home.capacity)} of {home.capacity}",
                    )
            elif home_name and landing and landing.in_club is not None:
                # Fav known but somehow absent from the board — show its web count.
                home_state = revo_client.state_for_club(home_name)
                embed.description = (
                    f"**{_safe_label(home_name)}**"
                    f"\n## {landing.in_club} in club right now"
                )
            # Scope the busiest board to the caller's state when we know it,
            # else nationwide. The field name makes the scope explicit.
            had_board = _busiest_board_field(embed, occ, state=home_state)
            quiet = _quietest_line(occ, state=home_state)
            if quiet:
                ui.block(embed, "Quietest right now", quiet, inline=False)
            if not embed.description:
                if not had_board:
                    return (
                        "Revo's live counter returned no clubs — try again "
                        "shortly.",
                        True,
                    )
                embed.description = (
                    "You haven't linked a Revo account, so here's the "
                    "national board. `/revo_link` adds your home club."
                )
            return embed, False

        # ---- PerfectGym down entirely: degrade to the web fav-club count ----
        landing = _busy_fav_landing(user_id)
        if landing and (landing.fav_club_name or landing.in_club is not None):
            return (
                _format_busy_line(landing)
                + "\n_(live all-clubs board temporarily unavailable.)_",
                False,
            )

        env_configured = bool(
            os.environ.get("REVO_USER", "").strip()
            and os.environ.get("REVO_PASS", "").strip()
        )
        if db.get_revo_account(user_id) is None and not env_configured:
            return (
                "🔒 Revo's live counter needs a logged-in session, but no shared "
                "account is configured and you haven't linked yours.\n"
                "Run `/help_revo_link` for a walkthrough, then `/revo_link "
                "email:<you> password:<…>` to enable `/busy`.",
                True,
            )
        return (
            "Revo's live counter is temporarily unavailable. Try again shortly, "
            "or check the Live Member Counter in the Revo app.",
            True,
        )

    result, ephemeral = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, discord.Embed):
        await interaction.followup.send(embed=result, ephemeral=ephemeral)
    else:
        await interaction.followup.send(result, ephemeral=ephemeral)


@bot.tree.command(
    name="revo_clubs",
    description="Revo club directory: clubs by state, or one club's details + nearest gyms.",
)
@app_commands.describe(
    club="Optional club name. Omit to list a whole state.",
    state="List every club in a state (SA, WA, VIC, NSW). Defaults to your home state.",
)
@app_commands.autocomplete(state=_revo_state_autocomplete)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_clubs_cmd(
    interaction: discord.Interaction,
    club: str | None = None,
    state: str | None = None,
) -> None:
    # Public club directory (Geo/GetClubList) — no PII — joined to the live
    # occupancy board so each club shows its current head-count. `state` lists a
    # whole state; a club name shows that club's address, a Google-Maps link,
    # state, live count and the nearest 3 other clubs; neither falls back to the
    # caller's home state. Mirrors /busy's gating + shared-vs-linked credential
    # resolution (public data, shared ok).
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_perfectgym.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    # Reject an unparseable state before spending a directory fetch on it.
    wanted_state = _normalise_state(state)
    if state and wanted_state is None:
        await interaction.response.send_message(
            f"**{_safe_label(state)}** isn't an Australian state I know. Try "
            f"one of: {', '.join(revo_client.known_states())}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id

    def _do() -> tuple[str | discord.Embed, bool]:
        directory = _perfectgym_directory(user_id)
        if not directory:
            return (
                "Revo's club directory is temporarily unavailable. Try again "
                "shortly, or check the Revo app.",
                True,
            )
        occupancy = _perfectgym_occupancy(user_id)  # best-effort live counts

        # ---- a named club: detail card + nearest 3 ----
        if club:
            # A bare state in the club box is a natural mistake ("/revo_clubs
            # club:SA") and substring matching would silently answer it with
            # e.g. Salisbury, so resolve it as a state first.
            as_state = _normalise_state(club)
            if as_state and _find_dir_entry(directory, club) is None:
                return _revo_clubs_state_embed(directory, occupancy, as_state), False
            pool = directory
            if wanted_state:  # both given → search within that state
                pool = [e for e in directory if (e.state or "").upper() == wanted_state]
            entry = _find_dir_entry(pool, club)
            if entry is None:
                where = f" in {wanted_state}" if wanted_state else ""
                return (
                    f"Couldn't find a Revo club matching "
                    f"**{_safe_label(club)}**{where}. Try the suburb name, or "
                    "use `state:` to list a whole state.",
                    True,
                )
            nearest = revo_perfectgym.nearest_clubs(directory, entry.name, limit=3)
            return (
                _format_revo_club_detail(
                    entry, occupancy, nearest, _club_hours_entry(entry),
                ),
                False,
            )

        # ---- an explicit state ----
        if wanted_state:
            return _revo_clubs_state_embed(directory, occupancy, wanted_state), False

        # ---- no arg: every club in the caller's home state ----
        landing = _busy_fav_landing(user_id)
        home_name = landing.fav_club_name if landing else None
        home_state = revo_client.state_for_club(home_name) if home_name else None
        if not home_state:
            available = ", ".join(_revo_states_present(directory)) or "SA, WA, VIC, NSW"
            return (
                "Couldn't work out your home state. Pick one with "
                f"`/revo_clubs state:` ({available}), name a club with "
                "`/revo_clubs club:Modbury`, or link your account with "
                "`/revo_link` so I know your home club.",
                True,
            )
        return _revo_clubs_state_embed(directory, occupancy, home_state), False

    result, ephemeral = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, discord.Embed):
        await interaction.followup.send(embed=result, ephemeral=ephemeral)
    else:
        await interaction.followup.send(result, ephemeral=ephemeral)


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
            "• `/revo_card` — privately show **your own** entry barcode "
            "(always ephemeral — only you ever see it)\n"
            "• `/seeprofile` — a roster of every linked member's Revo photo\n"
            "• Automatic attendance pings when you tap into your gym "
            "(posted in the configured notify channel)\n"
            "• Your favourite club is auto-detected from your last visits\n"
            "• You're auto-named in the bot from your Revo first name"
        ),
        inline=False,
    )
    embed.add_field(
        name="How your credentials are stored",
        value=(
            "• Your password is encrypted with **Fernet (AES-128-CBC + HMAC)** "
            "before being written to the database — the plaintext never "
            "touches disk.\n"
            "• Only the bot host (which holds the encryption key) can decrypt "
            "it; rotating that key invalidates every stored password.\n"
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
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(crypto=True), ephemeral=True,
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
            # club-counter.php is access-guarded now; the favourite club id
            # survives on the rewards landing (see F4 in docs/REVO_PORTAL.md).
            favorite = client.get_rewards_landing().fav_club_id
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
        # Best-effort: auto-name the member from their PerfectGym first name so
        # they show up as e.g. "Sean" across the bot. Always overwrites; never
        # fatal (a failed fetch just leaves any existing nickname in place).
        first_name = _apply_perfectgym_nickname(interaction.user.id)
        return (client.member_id, client.membership_level, favorite, first_name)

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        await interaction.followup.send(
            f"Couldn't link your Revo account ({result}).", ephemeral=True,
        )
        return
    member_id, level, favorite, first_name = result
    notify_line = (
        f"\n• Attendance notifications → <#{notify_id}>"
        if notify_id else "\n• No notification channel set — pings disabled."
    )
    # Only advertise the auto-nickname when we actually captured a first name.
    nick_line = (
        f"\n• You'll show up as **{first_name}** in the bot." if first_name else ""
    )
    await interaction.followup.send(
        "✅ Revo account linked!\n"
        f"• Member id: `{member_id}`\n"
        f"• Membership level: `{level}`\n"
        f"• Favorite club id: `{favorite}`"
        f"{notify_line}{nick_line}\n\n"
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
            embed=_revo_off_embed(), ephemeral=True,
        )
        return

    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    # Only makes sense against a server — we need member context to filter
    # accounts. In a DM this is the resolved/default server.
    guild = _ctx_guild(interaction)
    if guild is None:
        await interaction.response.send_message(
            "This command needs a server. DM me from one we share, or set your "
            "default with `/server`.", ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    accounts = db.list_revo_accounts()
    # Filter to accounts whose Discord user is actually a member of this guild.
    # We intentionally do NOT filter by notify_guild_id because a user may have
    # linked without setting up attendance notifications, or may have set them
    # up in a different server.
    # get_member() is cache-only; with members intent disabled the cache is
    # sparse, so fall back to fetch_member() (a live API call) on a cache miss.
    guild_member_ids: set[int] = set()
    guild_member_names: dict[int, str] = {}
    for r in accounts:
        uid = int(r["user_id"])
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            guild_member_ids.add(uid)
            guild_member_names[uid] = member.display_name
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
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    today = datetime.now()
    m = month or today.month
    y = year or today.year

    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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

    today = datetime.now()
    today_day = today.day if (m == today.month and y == today.year) else None
    month_name = datetime(y, m, 1).strftime("%B %Y")
    attended_count = sum(1 for v in result.values() if v)
    _, days_in_month = monthrange(y, m)
    current_streak, best_streak = _calc_streaks(result, days_in_month, today_day)
    display = _bot_name(target.id, target.display_name)
    header = (
        f"🔥 **{display}** — Revo check-ins for **{month_name}** "
        f"({attended_count} day{'s' if attended_count != 1 else ''})"
    )
    streak_line = (
        f"🔥 Streak: **{current_streak}** day{'s' if current_streak != 1 else ''} "
        f"· Best: **{best_streak}** day{'s' if best_streak != 1 else ''}"
    )
    legend = "🔥 attended · ⬜ missed · ⬛ out of month"
    body = f"{header}\n{streak_line}\n-# {legend}"
    try:
        image = await asyncio.to_thread(
            _render_revo_calendar_image,
            m,
            y,
            result,
            display=display,
            attended_count=attended_count,
            current_streak=current_streak,
            best_streak=best_streak,
        )
    except ImportError:
        image = None
    except Exception:
        LOG.exception("Failed to render Revo calendar image")
        image = None

    if image is not None:
        filename = f"revo_calendar_{target.id}_{y}_{m:02d}.png"
        await interaction.followup.send(
            body,
            file=discord.File(image, filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    grid = _render_revo_calendar(m, y, result)
    body = f"{header}\n{grid}\n{streak_line}\n-# {legend}"
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
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return
    guild = _ctx_guild(interaction)
    if guild is None:
        await interaction.response.send_message(
            "This command needs a server. DM me from one we share, or set your "
            "default with `/server`.", ephemeral=True,
        )
        return

    today = datetime.now()
    m = month or today.month
    y = year or today.year

    await interaction.response.defer(thinking=True)

    # Resolve which linked accounts belong to this guild.
    accounts = db.list_revo_accounts()
    guild_member_ids: set[int] = set()
    guild_member_names: dict[int, str] = {}
    for r in accounts:
        uid = int(r["user_id"])
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            guild_member_ids.add(uid)
            guild_member_names[uid] = member.display_name
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

    today = datetime.now()
    today_day = today.day if (m == today.month and y == today.year) else None
    _, days_in_month = monthrange(y, m)

    month_name = datetime(y, m, 1).strftime("%B %Y")
    n = len(results)

    image_entries: list[tuple[int, str, int, int, int, "dict[int, bool]"]] = []
    unavailable = 0
    for uid, cal in results:
        if cal is None:
            unavailable += 1
            continue
        count = _count(cal)
        cur_streak, best_streak = _calc_streaks(cal, days_in_month, today_day)
        display_name = _bot_name(uid, guild_member_names.get(uid, f"User {uid}"))
        image_entries.append((uid, display_name, count, cur_streak, best_streak, cal))

    if image_entries:
        try:
            image = await asyncio.to_thread(
                _render_revo_calendar_compare_long_image,
                m,
                y,
                image_entries,
            )
        except ImportError:
            image = None
        except Exception:
            LOG.exception("Failed to render Revo calendar compare image")
            image = None
        if image is not None:
            body = (
                f"**🔥 Revo Check-ins — {month_name}** "
                f"({n} member{'s' if n != 1 else ''})\n"
                "-# Full calendars stacked into one image. "
                "🔥 attended · ⬜ missed · ⬛ out of month"
            )
            if unavailable:
                body += f" · {unavailable} unavailable"
            await interaction.followup.send(
                body,
                file=discord.File(
                    image,
                    filename=f"revo_calendar_compare_{y}_{m:02d}.png",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    lines = [
        f"**🔥 Revo Check-ins — {month_name}** ({n} member{'s' if n != 1 else ''})",
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
            cur_streak, best_streak = _calc_streaks(cal, days_in_month, today_day)
            lines.append(_render_revo_calendar(m, y, cal))
            lines.append(
                f"-# 🔥 Streak: {cur_streak} · Best: {best_streak}"
            )
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


# ---------------------------------------------------------------------------
# Revo tickets / raffle / summary (read-only, real per-account data)
# ---------------------------------------------------------------------------

def _format_draw_countdown(days: int | None, label: str) -> str:
    """Render one raffle-countdown line, e.g. ``Monthly draw: in 3 days``."""
    if days is None:
        return f"{label} draw: *unknown*"
    if days <= 0:
        return f"{label} draw: **today / imminent** 🎉"
    return f"{label} draw: in **{days} day{'s' if days != 1 else ''}**"


def _raffle_optin_note(opted_in: bool | None) -> str | None:
    """A warning line when the member isn't actually entered in the monthly draw.

    Silent when opted in, and silent when unknown — the portal only renders a
    readable opt state on the raffle page, and guessing "not entered" at someone who
    is would be worse than saying nothing. Only shown for the member's *own* account
    (opt state is personal), which is why callers pass it through the same
    `personal` gate as the ticket balance.
    """
    if opted_in is False:
        return (
            "-# ⚠️ You're **not entered** in the monthly draw — your tickets won't "
            "be drawn until you opt in on the Revo rewards page."
        )
    return None


@bot.tree.command(
    name="revo_tickets",
    description="Show a Revo Fitness ticket balance and recent earning history.",
)
@app_commands.describe(member="The server member to look up. Defaults to yourself.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_tickets_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return

    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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

    def _do() -> "tuple[int | None, list[revo_client.TicketRow]] | str":
        try:
            client = _client_for_user(row)
            return client.get_tickets()
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(int(row["user_id"]))
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo tickets fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        msg = (
            f"Couldn't fetch your tickets ({result})."
            if target == interaction.user
            else f"Couldn't fetch {target.mention}'s tickets ({result})."
        )
        await interaction.followup.send(msg, ephemeral=True)
        return

    avail, rows = result
    display = _bot_name(target.id, target.display_name)
    avail_txt = f"**{avail}**" if avail is not None else "*unknown*"
    lines = [f"🎟️ **{display}** — Revo tickets available: {avail_txt}"]
    if rows:
        lines.append("-# Recent activity:")
        for r in rows[:8]:
            lines.append(f"• `{r.date}`  +{r.delta}  {r.source}")
    else:
        lines.append("-# No recent ticket activity found.")
    await interaction.followup.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="revo_raffle",
    description="Show your Revo ticket balance and the monthly + major draw countdowns.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_raffle_cmd(interaction: discord.Interaction) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    # Draw countdowns are global (same for everyone), but the ticket balance is
    # per-account. Prefer the invoking user's linked account so we can show
    # their own tickets; fall back to the shared env account for countdowns.
    row = db.get_revo_account(interaction.user.id)
    personal = row is not None

    def _do() -> "tuple[int | None, dict[str, int | None], dict[str, str | None]] | str":
        try:
            if row is not None:
                client = _client_for_user(row)
            else:
                client = revo_client.shared_client_from_env()
            raffle = client.get_raffle()
            tickets = None
            try:
                tickets, _rows = client.get_tickets()
            except Exception:  # pragma: no cover - non-fatal
                LOG.warning("Revo raffle: ticket fetch failed", exc_info=True)
            prize: dict[str, str | None] = {"monthly": None, "major": None}
            try:
                prize = client.get_prize_pool()
            except Exception:  # pragma: no cover - non-fatal
                LOG.warning("Revo raffle: prize-pool fetch failed", exc_info=True)
            return tickets, raffle, prize
        except revo_client.RevoUnavailable as exc:
            return f"no-credentials: {exc}"
        except revo_client.RevoAuthError as exc:
            if row is not None:
                _drop_cached_client(int(row["user_id"]))
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo raffle fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        if result.startswith("no-credentials"):
            await interaction.followup.send(
                "🔒 The raffle countdown needs a logged-in session. Link your "
                "account with `/revo_link` (or ask the host to set a shared "
                "account) and try again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Couldn't fetch the raffle info ({result}).", ephemeral=True,
        )
        return

    tickets, raffle, prize = result
    lines = ["🎰 **Revo Raffle**"]
    if personal and tickets is not None:
        display = _bot_name(interaction.user.id, interaction.user.display_name)
        lines.append(
            f"🎟️ **{display}** — **{tickets}** ticket{'s' if tickets != 1 else ''} "
            "in the draw"
        )
    lines.append(_format_draw_countdown(raffle.monthly_draw_days, "Monthly"))
    if prize.get("monthly"):
        lines.append(f"-# 🏆 {prize['monthly']}")
    # Only the caller's own opt state is theirs to be told about, and it's only
    # meaningful next to their own ticket count.
    if personal:
        note = _raffle_optin_note(raffle.opted_in)
        if note:
            lines.append(note)
    lines.append(_format_draw_countdown(raffle.major_draw_days, "Major"))
    if prize.get("major"):
        lines.append(f"-# 🏆 {prize['major']}")
    if not personal:
        lines.append("-# Link your account with `/revo_link` to show *your* ticket count.")
    await interaction.followup.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="revo_summary",
    description="A combined Revo dashboard: streak, this month's check-ins, tickets and next draw.",
)
@app_commands.describe(member="The server member to look up. Defaults to yourself.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_summary_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_client.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
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

    today = datetime.now()
    m, y = today.month, today.year

    def _do() -> "dict[str, object] | str":
        try:
            client = _client_for_user(row)
            streak = client.get_streak_weeks()
            avail, _rows = client.get_tickets()
            raffle = client.get_raffle()
            calendar = client.get_streak_calendar(m, y)
            prize: dict[str, str | None] = {"monthly": None, "major": None}
            try:
                prize = client.get_prize_pool()
            except Exception:  # pragma: no cover - non-fatal
                LOG.warning("Revo summary: prize-pool fetch failed", exc_info=True)
            # Membership tier + join date live on the EGYM/Netpulse mobile
            # backend, not the web portal. Best-effort: a Netpulse outage or a
            # missing dep must never sink the rest of the summary.
            membership = None
            try:
                if revo_netpulse.available():
                    membership = _netpulse_client_for_user(row).get_membership()
            except Exception:  # pragma: no cover - non-fatal, best effort
                LOG.warning(
                    "Revo summary: Netpulse membership fetch failed", exc_info=True,
                )
            # PerfectGym contract health (Current/Suspended + payment flag) — the
            # target's OWN linked client, non-sensitive (no barcode/PII). Same
            # best-effort contract as the Netpulse line above: a PerfectGym outage
            # or missing dep must never sink the summary.
            membership_status = None
            try:
                if revo_perfectgym.available():
                    membership_status = (
                        _perfectgym_client_for_user(row).get_membership_status()
                    )
            except Exception:  # pragma: no cover - non-fatal, best effort
                LOG.warning(
                    "Revo summary: PerfectGym membership status fetch failed",
                    exc_info=True,
                )
            return {
                "streak": streak,
                "tickets": avail,
                "raffle": raffle,
                "calendar": calendar,
                "prize": prize,
                "membership": membership,
                "membership_status": membership_status,
            }
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(int(row["user_id"]))
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("Revo summary fetch failed")
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        msg = (
            f"Couldn't build your summary ({result})."
            if target == interaction.user
            else f"Couldn't build {target.mention}'s summary ({result})."
        )
        await interaction.followup.send(msg, ephemeral=True)
        return

    streak = result["streak"]
    tickets = result["tickets"]
    raffle = result["raffle"]
    calendar = result["calendar"] or {}
    prize = result.get("prize") or {"monthly": None, "major": None}
    month_checkins = sum(1 for v in calendar.values() if v)
    month_name = today.strftime("%B")
    display = _bot_name(target.id, target.display_name)

    streak_txt = (
        f"**{streak} week{'s' if streak != 1 else ''}**"
        if streak is not None
        else "*unknown*"
    )
    tickets_txt = f"**{tickets}**" if tickets is not None else "*unknown*"
    lines = [
        f"🏋️ **{display}** — Revo summary",
        f"🔥 Weekly streak: {streak_txt}",
        f"📅 {month_name} check-ins: **{month_checkins}**",
        f"🎟️ Tickets available: {tickets_txt}",
        _format_draw_countdown(raffle.monthly_draw_days, "Monthly"),
    ]
    # Raffle opt state is personal, and this reply is public — only ever tell the
    # member about their OWN, mirroring the _summary_status_line rule below.
    if target == interaction.user:
        optin_note = _raffle_optin_note(raffle.opted_in)
        if optin_note:
            lines.append(optin_note)
    membership = result.get("membership")
    if membership is not None:
        tier = " · ".join(
            p for p in (membership.membership_type, membership.membership_subtype)
            if p
        )
        since = ""
        if membership.join_date:
            try:
                d = datetime.strptime(membership.join_date, "%Y-%m-%d")
                since = f" — member since {d.day} {d.strftime('%b %Y')}"
            except ValueError:
                since = ""
        if tier or since:
            lines.insert(1, f"💳 {tier or 'Member'}{since}")
    # PerfectGym contract status (best-effort; silent when unknown or PG is down).
    # SELF-ONLY — see _summary_status_line: this reply is public, so a third party's
    # payment-failure / suspension flag must never be broadcast to the channel.
    status_line = _summary_status_line(
        result.get("membership_status"), is_self=(target == interaction.user)
    )
    if status_line:
        lines.insert(1, status_line)
    if prize.get("monthly"):
        lines.append(f"-# 🏆 {prize['monthly']}")
    lines.append(_format_draw_countdown(raffle.major_draw_days, "Major"))
    if prize.get("major"):
        lines.append(f"-# 🏆 {prize['major']}")
    await interaction.followup.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="revo_card",
    description="Privately show YOUR Revo entry barcode (ephemeral, your own card only).",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def revo_card_cmd(interaction: discord.Interaction) -> None:
    # SENSITIVE: this surfaces the caller's physical entry BARCODE (a door-access
    # credential). Two hard rules enforced here:
    #   1. EPHEMERAL on *every* path — no one else ever sees the barcode.
    #   2. OWN creds ONLY (_revo_card_client_for_user reads only the caller's linked
    #      account, NEVER the shared REVO_USER account — that would leak the host's
    #      barcode). The barcode is rendered to a PNG and never logged.
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_perfectgym.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    # OWN-only resolution (no shared fallback). Constructing the client does no
    # network I/O (login is lazy), but resolving it EAGERLY decrypts the stored
    # credential, which raises RevoUnavailable when the row is undecryptable —
    # e.g. after a REVO_FERNET_KEY rotation leaves the ciphertext unreadable (the
    # shared REVO_USER/REVO_PASS are plain env vars, so /busy keeps working and
    # masks the breakage). That happens BEFORE we've acknowledged the interaction,
    # so it must not propagate — Discord would show "application did not respond".
    # Degrade to an ephemeral error the way every other Revo command does.
    try:
        client = _revo_card_client_for_user(interaction.user.id)
    except revo_client.RevoUnavailable:
        LOG.warning("revo_card: stored credential undecryptable", exc_info=True)
        await interaction.response.send_message(
            "Couldn't read your stored Revo credential (it may need re-linking). "
            "Please run `/revo_link` again.",
            ephemeral=True,
        )
        return
    if client is None:
        await interaction.response.send_message(
            "Link your own account first with `/revo_link` (this shows YOUR "
            "entry barcode).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    user_id = interaction.user.id

    def _do() -> "dict[str, object] | str":
        try:
            number = client.get_card_number()  # SENSITIVE — never logged
            status = None
            try:
                status = client.get_membership_status()
            except Exception:  # pragma: no cover - non-fatal
                LOG.warning("revo_card: membership status fetch failed", exc_info=True)
            return {"number": number, "status": status}
        except revo_perfectgym.PerfectGymAuthError as exc:
            _drop_cached_client(user_id)
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            LOG.exception("revo_card fetch failed")  # never logs the number
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _do)
    if isinstance(result, str):
        await interaction.followup.send(
            f"Couldn't fetch your card ({result}).", ephemeral=True,
        )
        return

    number = result["number"]
    status = result["status"]
    if not number:
        await interaction.followup.send(
            "Revo didn't return a card number for your account — check the Revo "
            "app.",
            ephemeral=True,
        )
        return

    contract = _format_membership_status_line(status)
    contract_txt = f"\n{contract}" if contract else ""
    caveat = "\n-# Code128 barcode. If it doesn't scan, use the Revo app."

    png = await bot.loop.run_in_executor(None, _render_card_barcode, number)  # type: ignore[arg-type]
    if png is not None:
        await interaction.followup.send(
            content=(
                "🎫 **Your Revo entry barcode** — private to you." + contract_txt
                + caveat
            ),
            file=discord.File(png, filename="revo_card.png"),
            ephemeral=True,
        )
    else:
        # python-barcode not installed → degrade to the number as text. This
        # reply is ephemeral (only the caller — the barcode's owner — sees it).
        await interaction.followup.send(
            content=(
                f"🎫 **Your Revo entry barcode**: `{number}`" + contract_txt
                + "\n-# Barcode image unavailable (install `python-barcode`). "
                "If the number doesn't scan, use the Revo app."
            ),
            ephemeral=True,
        )


@bot.tree.command(
    name="seeprofile",
    description="Roster of every linked member's Revo profile photo + first name.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def seeprofile_cmd(interaction: discord.Interaction) -> None:
    # Owner-approved: this shows EVERY linked member's Revo photo. Hard rules:
    #   * Each photo is fetched with THAT member's OWN per-user client
    #     (_perfectgym_client_for_user) — never the shared REVO_USER account, and
    #     never one member's client for another member's photo.
    #   * The signed photo URL is a ~10-min capability URL: we download the bytes
    #     immediately and attach them as in-memory files (attachment://) so the
    #     raw URL never lands in message history — and it is NEVER logged.
    if REVO_DISABLED:
        await interaction.response.send_message(
            embed=_revo_off_embed(), ephemeral=True,
        )
        return
    if not revo_perfectgym.available():
        await interaction.response.send_message(
            embed=_revo_missing_deps_embed(), ephemeral=True,
        )
        return

    rows = db.list_revo_accounts()
    if not rows:
        await interaction.response.send_message(
            "Nobody's linked a Revo account yet — run `/revo_link` to be first.",
            ephemeral=True,
        )
        return

    # Slow: N logins + N downloads run in an executor. Public (a shared roster).
    await interaction.response.defer(thinking=True)

    results, failures = await bot.loop.run_in_executor(
        None, _seeprofile_gather, rows,
    )

    if not results:
        # Every linked member failed to load (bad creds / no photo / download
        # error) — one friendly message rather than an empty roster.
        await interaction.followup.send(
            "Couldn't load any linked members' Revo photos right now — try again "
            "in a bit."
        )
        return

    # Discord caps a message at 10 embeds (and 10 attachments), so paginate the
    # roster across as many followup messages as it takes.
    chunk_size = 10
    chunks = [results[i:i + chunk_size] for i in range(0, len(results), chunk_size)]
    note = ""
    if failures:
        note = (
            f"-# (couldn't load {failures} "
            f"member{'s' if failures != 1 else ''})"
        )
    for idx, chunk in enumerate(chunks):
        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        for uid, first_name, data in chunk:
            fname = f"revo_{uid}.{_photo_file_ext(data)}"
            files.append(discord.File(io.BytesIO(data), filename=fname))
            embed = discord.Embed(
                title=_seeprofile_display_name(interaction, uid, first_name),
                colour=EMBED_COLOUR,
            )
            embed.set_image(url=f"attachment://{fname}")
            embeds.append(embed)
        # The "couldn't load N" note rides on the final message only.
        content = note if (note and idx == len(chunks) - 1) else None
        await interaction.followup.send(content=content, embeds=embeds, files=files)


def _render_revo_calendar(
    month: int,
    year: int,
    attended: "dict[int, bool]",
) -> str:
    """Render a Mon-Sun emoji grid in a code block.

    🔥 = attended, ⬜ = missed, ⬛ = out-of-month padding.

    The weekday header and each emoji row use the same separator inside one
    code block. Two spaces gives the emoji cells enough visual room in
    Discord, where emoji glyphs are wider than plain text characters.
    """
    first_weekday, days_in_month = monthrange(year, month)  # Monday=0
    FIRE = "🔥"
    BLANK = "⬜"
    PAD = "⬛"
    CELL_SEP = "  "
    cells: list[str] = [PAD] * first_weekday
    for day in range(1, days_in_month + 1):
        cells.append(FIRE if attended.get(day) else BLANK)
    while len(cells) % 7 != 0:
        cells.append(PAD)
    rows = [
        CELL_SEP.join(cells[row_start : row_start + 7])
        for row_start in range(0, len(cells), 7)
    ]
    header = CELL_SEP.join(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"))
    grid = "\n".join(rows)
    return f"```\n{header}\n{grid}\n```"


def _render_revo_calendar_image_unlocked(
    month: int,
    year: int,
    attended: "dict[int, bool]",
    *,
    display: str,
    attended_count: int,
    current_streak: int,
    best_streak: int,
) -> io.BytesIO:
    """Render a Discord-friendly Revo calendar PNG.

    Uses matplotlib's headless Agg backend, so it works inside the Linux Docker
    image without an X server. Kept lazy-imported like /graph so the bot can
    still boot in environments without matplotlib.
    """
    plt = importlib.import_module("matplotlib.pyplot")
    patches = importlib.import_module("matplotlib.patches")

    first_weekday, days_in_month = monthrange(year, month)  # Monday=0
    cells: list[int | None] = [None] * first_weekday
    cells.extend(range(1, days_in_month + 1))
    while len(cells) % 7 != 0:
        cells.append(None)
    weeks = [cells[i : i + 7] for i in range(0, len(cells), 7)]

    bg = "#1f2028"
    panel = "#2b2630"
    panel_edge = "#423847"
    text = "#f5f0f6"
    muted = "#a9a0ad"
    out_month = "#45424b"
    missed = "#e4d2ff"
    missed_text = "#3a3142"
    attended_fill = "#ff8a1d"
    attended_text = "#1e1205"

    fig, ax = plt.subplots(figsize=(7.2, 4.8), dpi=120)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.add_patch(
        patches.FancyBboxPatch(
            (0.04, 0.06),
            0.92,
            0.88,
            boxstyle="round,pad=0.012,rounding_size=0.025",
            facecolor=panel,
            edgecolor=panel_edge,
            linewidth=1.2,
        )
    )

    short_display = display if len(display) <= 30 else f"{display[:27]}..."
    month_name = datetime(year, month, 1).strftime("%B %Y")
    ax.text(
        0.08,
        0.87,
        short_display,
        color=text,
        fontsize=15,
        fontweight="bold",
        va="center",
    )
    ax.text(
        0.92,
        0.87,
        f"{attended_count} day{'s' if attended_count != 1 else ''}",
        color=attended_fill,
        fontsize=15,
        fontweight="bold",
        ha="right",
        va="center",
    )
    ax.text(
        0.08,
        0.80,
        f"Revo check-ins for {month_name}",
        color=muted,
        fontsize=9,
        va="center",
    )

    left, right = 0.08, 0.92
    top, bottom = 0.68, 0.20
    gap = 0.012
    cell_w = (right - left - gap * 6) / 7
    cell_h = (top - bottom - gap * (len(weeks) - 1)) / len(weeks)
    weekday_labels = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

    for col, label in enumerate(weekday_labels):
        x = left + col * (cell_w + gap) + cell_w / 2
        ax.text(
            x,
            top + 0.055,
            label,
            color=text,
            fontsize=10,
            fontweight="bold",
            ha="center",
            va="center",
        )

    for row, week in enumerate(weeks):
        for col, day in enumerate(week):
            x = left + col * (cell_w + gap)
            y = top - (row + 1) * cell_h - row * gap
            if day is None:
                fill = out_month
                number_color = muted
            elif attended.get(day):
                fill = attended_fill
                number_color = attended_text
            else:
                fill = missed
                number_color = missed_text
            ax.add_patch(
                patches.FancyBboxPatch(
                    (x, y),
                    cell_w,
                    cell_h,
                    boxstyle="round,pad=0.002,rounding_size=0.01",
                    facecolor=fill,
                    edgecolor=fill,
                    linewidth=0,
                )
            )
            if day is None:
                continue
            ax.text(
                x + 0.010,
                y + cell_h - 0.014,
                str(day),
                color=number_color,
                fontsize=7,
                fontweight="bold",
                ha="left",
                va="top",
            )
            if attended.get(day):
                _draw_revo_flame(
                    ax,
                    patches,
                    x + cell_w / 2,
                    y + cell_h / 2 - 0.002,
                    min(cell_w, cell_h) * 0.72,
                )

    legend_y = 0.125
    for idx, (label, colour) in enumerate(
        (("attended", attended_fill), ("missed", missed), ("out of month", out_month))
    ):
        x = 0.08 + idx * 0.17
        if label == "attended":
            _draw_revo_flame(ax, patches, x, legend_y, 0.030)
        else:
            ax.add_patch(
                patches.Circle((x, legend_y), 0.012, facecolor=colour, edgecolor=colour)
            )
        ax.text(x + 0.020, legend_y, label, color=muted, fontsize=8, va="center")
    ax.text(
        0.92,
        legend_y,
        f"Streak {current_streak} · Best {best_streak}",
        color=text,
        fontsize=9,
        fontweight="bold",
        ha="right",
        va="center",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    buf.seek(0)
    return buf


def _render_revo_calendar_image(
    month: int,
    year: int,
    attended: "dict[int, bool]",
    *,
    display: str,
    attended_count: int,
    current_streak: int,
    best_streak: int,
) -> io.BytesIO:
    return _run_serialized_matplotlib(
        _render_revo_calendar_image_unlocked,
        month,
        year,
        attended,
        display=display,
        attended_count=attended_count,
        current_streak=current_streak,
        best_streak=best_streak,
    )


def _render_revo_calendar_compare_long_image_unlocked(
    month: int,
    year: int,
    entries: "list[tuple[int, str, int, int, int, dict[int, bool]]]",
) -> io.BytesIO:
    """Render full-size member calendars stacked vertically in one PNG."""
    plt = importlib.import_module("matplotlib.pyplot")

    card_images = []
    for _uid, display, count, cur_streak, best_streak, attended in entries:
        card = _render_revo_calendar_image(
            month,
            year,
            attended,
            display=display,
            attended_count=count,
            current_streak=cur_streak,
            best_streak=best_streak,
        )
        card.seek(0)
        card_images.append(plt.imread(card, format="png"))

    if not card_images:
        raise ValueError("No calendars to render")

    dpi = 120
    width_px = max(img.shape[1] for img in card_images)
    height_px = sum(img.shape[0] for img in card_images)
    gap_px = 18
    height_px += gap_px * (len(card_images) - 1)

    fig, axes = plt.subplots(
        len(card_images),
        1,
        figsize=(width_px / dpi, height_px / dpi),
        dpi=dpi,
        gridspec_kw={
            "height_ratios": [img.shape[0] for img in card_images],
            "hspace": gap_px / max(1, height_px),
        },
    )
    fig.patch.set_facecolor("#1f2028")
    if len(card_images) == 1:
        axes = [axes]
    for ax, img in zip(axes, card_images):
        ax.imshow(img)
        ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return buf


def _render_revo_calendar_compare_long_image(
    month: int,
    year: int,
    entries: "list[tuple[int, str, int, int, int, dict[int, bool]]]",
) -> io.BytesIO:
    return _run_serialized_matplotlib(
        _render_revo_calendar_compare_long_image_unlocked,
        month,
        year,
        entries,
    )


def _draw_revo_flame(
    ax,
    patches,
    center_x: float,
    center_y: float,
    size: float,
    *,
    linewidth: float = 1.4,
) -> None:
    """Draw a small app-style flame without relying on emoji fonts."""

    def scaled(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(center_x + px * size, center_y + py * size) for px, py in points]

    outer_points = scaled([
        (0.04, 0.58),
        (0.34, 0.24),
        (0.40, -0.08),
        (0.16, -0.48),
        (-0.22, -0.42),
        (-0.42, -0.12),
        (-0.20, 0.20),
        (-0.06, 0.08),
    ])
    ax.add_patch(
        patches.Polygon(
            outer_points,
            closed=True,
            facecolor="#ffd23c",
            edgecolor="#2a1a0a",
            linewidth=linewidth,
            joinstyle="round",
        )
    )

    inner_points = scaled([
        (0.10, 0.16),
        (0.24, -0.08),
        (0.04, -0.36),
        (-0.17, -0.16),
        (-0.04, 0.02),
    ])
    ax.add_patch(
        patches.Polygon(
            inner_points,
            closed=True,
            facecolor="#ff5a1f",
            edgecolor="none",
        )
    )


def _calc_streaks(
    attended: "dict[int, bool]",
    days_in_month: int,
    today_day: int | None = None,
) -> tuple[int, int]:
    """Return (current_streak, best_streak) for the month.

    *current_streak* is the consecutive-attended run ending on ``today_day``
    (or the last day of the month when ``today_day`` is ``None``).
    *best_streak* is the longest such run anywhere in the month up to that day.
    """
    end = today_day if today_day is not None else days_in_month
    current = 0
    best = 0
    run = 0
    for d in range(1, end + 1):
        if attended.get(d):
            run += 1
            best = max(best, run)
        else:
            run = 0
    current = run
    return current, best


def _bot_name(user_id: int, discord_fallback: str) -> str:
    """Return a display string for ``user_id``.

    Format: ``Discord Name (Nickname)`` when a bot-wide nickname has been
    assigned, otherwise just the Discord display name.
    """
    nick = db.get_user_nickname(user_id)
    if nick is None:
        return discord_fallback
    return f"{discord_fallback} ({nick})"


# NOTE: the manual set_nick + remove_nick commands were retired — bot-wide
# nicknames now auto-populate from the member's PerfectGym first name on
# /revo_link (see _apply_perfectgym_nickname). The db helpers + _bot_name +
# _resolve_nickname_target are still used for display + chat targeting.


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
            "No nicknames set yet — they're filled in automatically when you "
            "run `/revo_link`.",
            ephemeral=True,
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

    Strategy: read the per-day streaks calendar (the real attendance signal)
    and track the most recent attended day in ``last_checkin_date``. When a
    newer day appears, post to the user's configured notify channel and advance
    the cursor. The *first* poll after linking is silent (no cursor yet →
    record baseline) so users aren't spammed with backfilled history.

    Why the calendar and not ticket-tally: the "Attendance" rows in
    ticket-tally are only a roughly-weekly *reward* grant, dated to issuance —
    they lag the actual visit by days and miss most check-ins entirely. The
    calendar lights up per training day, much closer to real time.
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
    prev_checkin = row["last_checkin_date"]  # ISO YYYY-MM-DD or None
    prev_streak = row["last_streak_weeks"]

    now_local = datetime.now(DISPLAY_TZ)
    today_iso = now_local.strftime("%Y-%m-%d")

    # Once today is on the board there is nothing left to learn until midnight:
    # the cursor only ever advances to a NEWER day, and the calendar has no
    # finer resolution than the day (see docs/REVO_PORTAL.md §3.2.2). Skipping
    # the round-trip here is what makes a tight poll interval affordable — it
    # removes every request between the day's check-in and midnight, which on a
    # training day is the majority of them.
    if prev_checkin == today_iso:
        return

    def _fetch() -> "tuple[str | None, int | None] | str":
        """Return (latest_checkin_iso, streak_weeks) or an error string."""
        try:
            client = _client_for_user(row)
            cal = client.get_streak_calendar(now_local.month, now_local.year)
            latest_day = revo_client.latest_attended_day(cal)
            latest_iso: str | None = (
                datetime(now_local.year, now_local.month, latest_day).strftime("%Y-%m-%d")
                if latest_day
                else None
            )
            # Near a month boundary the current month may not yet hold the most
            # recent visit — bridge by also checking the previous month.
            if latest_iso is None and now_local.day <= 2:
                prev_month = now_local.replace(day=1) - timedelta(days=1)
                prev_cal = client.get_streak_calendar(prev_month.month, prev_month.year)
                prev_day = revo_client.latest_attended_day(prev_cal)
                if prev_day:
                    latest_iso = datetime(
                        prev_month.year, prev_month.month, prev_day
                    ).strftime("%Y-%m-%d")
            streak = None
            try:
                streak = client.get_streak_weeks()
            except Exception:  # pragma: no cover
                LOG.warning("Revo streak fetch failed for user %s", user_id, exc_info=True)
            return latest_iso, streak
        except revo_client.RevoAuthError as exc:
            _drop_cached_client(user_id)
            return f"auth-failed: {exc}"
        except Exception as exc:  # pragma: no cover - network
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _fetch)
    if isinstance(result, str):
        LOG.warning("Revo poll skipped user %s: %s", user_id, result)
        return
    latest_iso, streak = result

    # Advance the cursor to the newest date we know about, then persist first so
    # a notify-failure doesn't replay forever. ISO dates sort lexicographically.
    cursor = max(d for d in (prev_checkin, latest_iso) if d) if (prev_checkin or latest_iso) else None
    db.update_revo_checkin_state(user_id, cursor, streak)

    if latest_iso is None:
        return
    if prev_checkin is None:
        # First poll after link — establish baseline silently.
        LOG.info("Revo baseline established for user %s (date=%s)", user_id, latest_iso)
        return
    if latest_iso <= prev_checkin:
        return
    if notify_channel_id is None:
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
    checkin_date = datetime.strptime(latest_iso, "%Y-%m-%d").date()
    today = now_local.date()

    # Turn the anecdote "it seems to land ~2:30am / ~2:30pm" into data. Revo
    # publishes attendance into the rewards system on its own schedule, and this
    # is the only place we can observe when a day actually appeared: log the
    # wall clock at detection plus how old the visit already was. A few weeks of
    # these lines pins the real cadence (and would catch it changing).
    LOG.info(
        "Revo check-in detected user=%s visit_date=%s detected_at=%s (%s UTC) "
        "lag_days=%d",
        user_id, latest_iso, now_local.strftime("%Y-%m-%d %H:%M %Z"),
        now_local.astimezone(timezone.utc).strftime("%H:%M"),
        (today - checkin_date).days,
    )

    # Deliberately NOT "just checked in". The underlying signal is a per-DAY
    # attendance flag that Revo batches into the rewards system, so by the time
    # we see it the session can be many hours old — claiming "just" is usually
    # wrong. "Trained today" is what the data actually supports.
    if checkin_date == today:
        text = f"🏋️ <@{user_id}> trained at Revo today!{streak_tail}"
    else:
        when = "yesterday" if checkin_date == today - timedelta(days=1) else (
            f"{checkin_date.strftime('%a')} {checkin_date.day} {checkin_date.strftime('%b')}"
        )
        text = f"🏋️ <@{user_id}> trained at Revo ({when}){streak_tail}"

    # Celebrate the first check-in that pushes the weekly streak past a
    # milestone (4/8/12/26/52 weeks).
    milestone = revo_client.streak_milestone(prev_streak, streak)
    if milestone is not None:
        text += (
            f"\n🎉 **Milestone!** That's a **{milestone}-week** Revo streak. "
            "Keep it going! 💪"
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
# Strava integration. See docs/STRAVA.md.
# ---------------------------------------------------------------------------
# Unlike Revo (HTML scraping + polling), Strava uses OAuth2 + real-time webhook
# push. We run a small aiohttp server (app/strava_web.py) on the bot's event
# loop to receive the OAuth redirect and the webhook events; on a new activity
# we fetch the full details and post an embed to a single shared feed channel.

# All STRAVA_* and HEVY_* tunables are bound by _bind_config() near the top of
# this file. The credential-shaped ones (STRAVA_CLIENT_ID/SECRET,
# STRAVA_PUBLIC_URL, the redirect/webhook overrides, the verify token, and the
# Fernet keys) stay environment-only because app/strava_client.py and
# app/hevy_client.py read them directly with os.getenv; the supervisor exports
# the resolved values into this process's environment.

STRAVA_COLOUR = discord.Colour.from_str("#fc4c02")  # Strava brand orange
# Hevy's own dark brand tone (#1d2330) sits at roughly 1.1:1 contrast against
# Discord's dark background (#313338) — an invisible rail on what is, by volume,
# the most-posted embed in the bot. Their brand blue reads in both themes.
HEVY_COLOUR = ui.HEVY

# aiohttp AppRunner handle, set in setup_hook so a future shutdown path could
# clean it up.
_strava_runner = None

# AppRunner handle for the operator web dashboard (app/webui.py).
#: AppRunner for the supervisor control socket (app/workerlink.py). Only set
#: when this process was spawned by the supervisor.
_rpc_runner = None

# Live webhook subscription id (set once known via ensure/subscribe). The event
# handler uses it to reject deliveries from any other subscription. None means
# "not yet known" → fail open so valid events during startup aren't dropped.
_strava_subscription_id: int | None = None


def _strava_cfg() -> "strava_client.StravaConfig":
    return strava_client.config_from_env()


def _strava_enabled() -> bool:
    """True when Strava is switched on, deps are present, and the host app
    credentials are configured."""
    return (
        not STRAVA_DISABLED
        and strava_client.available()
        and _strava_cfg().configured
    )


def _hevy_enabled() -> bool:
    """True when Hevy is switched on, deps + a Fernet key are present. Linking
    works without a feed channel (lifts still import); the feed embed is just
    skipped when HEVY_FEED_CHANNEL_ID isn't set."""
    return (
        not HEVY_DISABLED
        and hevy_client.available()
        and hevy_client.fernet_ready()
    )


def _ha_enabled() -> bool:
    """True when Home Assistant is switched on and the deps + a Fernet key exist.

    Deliberately says nothing about whether any *member* has set a server up —
    every member brings their own with ``/setup_ha``, so this is only about
    whether the feature can work at all. It also doesn't require an announcement
    channel: weigh-ins still import as bodyweight without one, the same way Hevy
    imports lifts with no feed channel."""
    return (
        not HA_DISABLED
        and ha_client.available()
        and ha_client.fernet_ready()
    )


def _ha_cfg_for(row) -> "ha_client.HAConfig | None":
    """Build a connection config from a member's stored server row, or None.

    None means the stored token can't be read — a rotated Fernet key, or a row
    written before the key existed. The caller tells them to re-run ``/setup_ha``
    rather than failing silently on every poll."""
    if row is None:
        return None
    # Tolerate a row that carries no credential columns at all — an account row
    # straight from ha_get rather than the joined shape list_ha_synced yields.
    # The caller reads this as "not connected", which is the truth.
    keys = row.keys()
    if "token_enc" not in keys or "base_url" not in keys:
        return None
    if not row["token_enc"] or not row["base_url"]:
        return None
    try:
        token = ha_client.decrypt_token(row["token_enc"])
    except ha_client.HAError:
        LOG.warning(
            "Home Assistant: unreadable token for user %s — skipping",
            row["user_id"],
        )
        return None
    return ha_client.config_for(
        row["base_url"], token, verify_ssl=HA_VERIFY_SSL,
    )


#: Where weigh-in announcements go. Reuses the channel the bot already nags
#: people to weigh in on rather than adding a fourth feed-channel setting — the
#: reminder and the answer to it belong in the same place. Unset means import
#: silently — there is no per-member opt-out.
def _ha_alert_channel_id() -> int | None:
    return BODYWEIGHT_REMINDER_CHANNEL_ID


def _ha_visible_states(states: list[dict]) -> list[dict]:
    """Drop entities the operator asked to ignore, before anything else sees them.

    Applied at the two points states enter the feature — the poll and the command
    helper — so the exclusion holds for the listing, for linking and for syncing
    without each of those having to remember.

    This exists for phantom entities. A phone or fitness-tracker bridge (Apple
    Health, Google Fit) creates ``sensor.<name>_iphone_weight`` and its siblings
    and then never writes to them, so they sit at ``unavailable`` forever while
    cluttering discovery and inviting somebody to link the wrong one.
    """
    if not HA_IGNORE_ENTITIES:
        return states
    out = []
    for state in states:
        eid = str(state.get("entity_id") or "").lower()
        if any(fragment in eid for fragment in HA_IGNORE_ENTITIES):
            continue
        out.append(state)
    return out


async def _ha_alert_channel():
    """Resolve the announcement channel, or None.

    Falls back to ``fetch_channel`` on a cache miss (Strava's behaviour, not
    Hevy's — a cold cache silently dropping every weigh-in is the kind of bug
    that gets reported as "the bot stopped working")."""
    cid = _ha_alert_channel_id()
    if not cid:
        return None
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except discord.HTTPException:
            LOG.warning("Home Assistant: cannot access alert channel %s", cid)
            return None
    return channel


# Per-user locks serialising token refreshes. Strava *rotates* refresh tokens,
# so two activities arriving together must not refresh in parallel — the second
# refresh would invalidate the first's freshly-issued token (or vice versa).
_strava_token_locks: dict[int, threading.Lock] = {}
_strava_token_locks_guard = threading.Lock()


def _strava_token_lock(user_id: int) -> threading.Lock:
    with _strava_token_locks_guard:
        lock = _strava_token_locks.get(user_id)
        if lock is None:
            lock = _strava_token_locks[user_id] = threading.Lock()
        return lock


def _strava_access_token(row) -> str:
    """Return a currently-valid access token for a linked-account row.

    Refreshes via the stored refresh token when the cached access token has
    expired, persisting the rotated pair (Strava rotates refresh tokens too).
    Synchronous (uses ``requests``) — always call via ``run_in_executor``.

    Refreshes are serialised per user and re-read the latest persisted tokens
    under the lock, so concurrent webhook deliveries don't double-refresh and
    invalidate each other's rotated refresh token.
    """
    user_id = int(row["user_id"])

    def _tokens_from(r) -> "strava_client.TokenSet":
        return strava_client.TokenSet(
            access_token=strava_client.decrypt_token(r["access_token_enc"]),
            refresh_token=strava_client.decrypt_token(r["refresh_token_enc"]),
            expires_at=int(r["expires_at"]),
        )

    tokens = _tokens_from(row)
    if not tokens.is_expired():
        return tokens.access_token

    cfg = _strava_cfg()
    with _strava_token_lock(user_id):
        # Another thread may have refreshed while we waited — re-read first.
        latest = db.get_strava_account(user_id) or row
        tokens = _tokens_from(latest)
        if not tokens.is_expired():
            return tokens.access_token
        fresh = strava_client.refresh_tokens(cfg, tokens.refresh_token)
        db.update_strava_tokens(
            user_id,
            strava_client.encrypt_token(fresh.access_token),
            strava_client.encrypt_token(fresh.refresh_token),
            fresh.expires_at,
        )
        return fresh.access_token


def _build_strava_embed(
    activity: "strava_client.StravaActivity", who: str,
) -> discord.Embed:
    """Render a Strava activity as a Discord embed. Distance sports get
    pace/elevation; everything else is duration-first."""
    emoji = strava_client.sport_emoji(activity.sport_type)
    when = strava_client.start_unix(activity)
    desc = f"New **{activity.sport_type}** by {who}"
    if when:
        desc += f" · <t:{when}:R>"
    if activity.description:
        # The athlete's own caption, quoted and length-capped.
        caption = activity.description.strip().replace("\n", "\n> ")
        desc += f"\n> {caption[:500]}"
    embed = discord.Embed(
        title=f"{emoji} {activity.name}"[:256],
        url=activity.url or None,
        colour=STRAVA_COLOUR,
        description=desc,
    )
    is_distance = (
        strava_client.is_distance_sport(activity.sport_type)
        and activity.distance_m > 0
    )
    imp = STRAVA_IMPERIAL
    if is_distance:
        embed.add_field(
            name="Distance",
            value=strava_client.format_distance(activity.distance_m, imp),
        )
        embed.add_field(
            name="Time",
            value=strava_client.format_duration(activity.moving_time_s),
        )
        pace = strava_client.format_pace(
            activity.distance_m, activity.moving_time_s, imp,
        )
        speed = strava_client.format_speed(activity.average_speed_ms, imp)
        if pace:
            embed.add_field(name="Pace", value=pace)
        elif speed:
            embed.add_field(name="Speed", value=speed)
        max_speed = strava_client.format_speed(activity.max_speed_ms, imp)
        if max_speed:
            embed.add_field(name="Max speed", value=max_speed)
        if activity.total_elevation_gain_m:
            embed.add_field(
                name="Elevation",
                value=strava_client.format_elevation(
                    activity.total_elevation_gain_m, imp,
                ),
            )
    else:
        embed.add_field(
            name="Duration",
            value=strava_client.format_duration(
                activity.moving_time_s or activity.elapsed_time_s
            ),
        )
    if activity.average_heartrate:
        hr = f"{activity.average_heartrate:.0f} bpm"
        if activity.max_heartrate:
            hr += f" (max {activity.max_heartrate:.0f})"
        embed.add_field(name="Heart rate", value=hr)
    if activity.average_watts:
        power = f"{activity.average_watts:.0f} W"
        if activity.kilojoules:
            power += f" · {activity.kilojoules:.0f} kJ"
        embed.add_field(name="Power", value=power)
    if activity.average_cadence:
        # Strava reports running cadence per leg — double it for steps/min.
        is_run = activity.sport_type in ("Run", "TrailRun", "VirtualRun")
        cad = activity.average_cadence * (2 if is_run else 1)
        embed.add_field(
            name="Cadence", value=f"{cad:.0f} {'spm' if is_run else 'rpm'}",
        )
    if activity.calories:
        embed.add_field(name="Calories", value=f"{activity.calories:.0f} kcal")
    if activity.suffer_score:
        embed.add_field(name="Relative effort", value=f"{activity.suffer_score:.0f}")
    if activity.average_temp is not None:
        embed.add_field(
            name="Temp",
            value=strava_client.format_temp(activity.average_temp, imp),
        )
    achievements = []
    if activity.pr_count:
        achievements.append(f"🏅 {activity.pr_count} PR{'s' if activity.pr_count != 1 else ''}")
    if activity.achievement_count:
        achievements.append(f"⭐ {activity.achievement_count}")
    if achievements:
        embed.add_field(name="Achievements", value="  ".join(achievements))
    footer = f"via Strava · {activity.gear_name}" if activity.gear_name else "via Strava"
    embed.set_footer(text=footer)
    return embed


def _render_strava_route_png_unlocked(polyline: str) -> "io.BytesIO | None":
    """Render an activity's GPS route as a PNG (Strava-style silhouette).

    Returns a seekable buffer, or None when there's no usable route or
    matplotlib isn't available. Uses the headless Agg backend like the other
    chart helpers, with a latitude-corrected aspect so the path isn't squashed.
    """
    points = strava_client.decode_polyline(polyline)
    if len(points) < 2:
        return None
    try:
        plt = importlib.import_module("matplotlib.pyplot")
    except Exception:  # pragma: no cover - matplotlib not installed
        return None
    import math

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    bg = "#1f2028"
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=120)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    ax.axis("off")
    ax.plot(
        lons, lats, color="#fc4c02", linewidth=3.0,
        solid_capstyle="round", solid_joinstyle="round",
    )
    # Start (green) / finish (red) markers.
    ax.plot(lons[0], lats[0], marker="o", color="#19d36b", markersize=7, zorder=3)
    ax.plot(lons[-1], lats[-1], marker="o", color="#ff3b30", markersize=7, zorder=3)
    mean_lat = sum(lats) / len(lats)
    ax.set_aspect(1.0 / max(0.1, math.cos(math.radians(mean_lat))))
    ax.margins(0.08)
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", facecolor=fig.get_facecolor(),
        bbox_inches="tight", pad_inches=0.1,
    )
    plt.close(fig)
    buf.seek(0)
    return buf


def _render_strava_route_png(polyline: str) -> "io.BytesIO | None":
    """Render a route under the shared Matplotlib lock, best effort."""
    try:
        return _run_serialized_matplotlib(
            _render_strava_route_png_unlocked,
            polyline,
        )
    except Exception:  # pragma: no cover - optional backend or drawing failure
        return None


def _strava_embed_and_file(
    activity: "strava_client.StravaActivity", who: str,
    *,
    render_route: bool = True,
) -> "tuple[discord.Embed, discord.File | None]":
    """Build the activity embed plus an optional attached image.

    Prefers a real Strava photo (if the athlete added one); otherwise renders
    the GPS route. Strength/indoor activities with neither just get the embed.
    """
    embed = _build_strava_embed(activity, who)
    if activity.photo_url:
        embed.set_image(url=activity.photo_url)
        return embed, None
    if activity.map_polyline:
        # Prefer a real basemap via Mapbox (Discord fetches the URL directly);
        # otherwise render the bare-line silhouette locally.
        if STRAVA_MAPBOX_TOKEN:
            map_url = strava_client.mapbox_route_url(
                activity.map_polyline, STRAVA_MAPBOX_TOKEN,
                style=STRAVA_MAP_STYLE,
            )
            if map_url:
                embed.set_image(url=map_url)
                return embed, None
        if not render_route:
            # Rename updates preserve the original ``route.png`` attachment;
            # point the replacement embed at it without rendering a throwaway
            # copy on the event loop.
            embed.set_image(url="attachment://route.png")
            return embed, None
        buf = _render_strava_route_png(activity.map_polyline)
        if buf is not None:
            embed.set_image(url="attachment://route.png")
            return embed, discord.File(buf, filename="route.png")
    return embed, None


async def _strava_on_callback(
    code: str | None, state: str | None, error: str | None,
) -> str:
    """Handle the OAuth redirect — exchange the code, persist tokens, confirm.

    Returns the HTML body shown in the user's browser.
    """
    if error:
        return strava_web.error_page(f"Strava reported: {error}")
    if not code or not state:
        return strava_web.error_page("Missing authorization code in the redirect.")
    user_id = db.pop_strava_pending(state)
    if user_id is None:
        return strava_web.error_page(
            "This link has expired or was already used. Run /strava_link again."
        )
    cfg = _strava_cfg()

    def _exchange() -> "tuple[strava_client.TokenSet, int | None, str] | str":
        try:
            tokens, athlete = strava_client.exchange_code(cfg, code)
            name = strava_client.athlete_display_name(athlete)
            athlete_id = (
                int(athlete["id"]) if athlete.get("id") is not None else None
            )
            return tokens, athlete_id, name
        except strava_client.StravaAuthError as exc:
            return f"auth: {exc}"
        except Exception as exc:  # pragma: no cover - network
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _exchange)
    if isinstance(result, str):
        LOG.warning("Strava code exchange failed for user %s: %s", user_id, result)
        return strava_web.error_page("Token exchange failed. Please try again.")
    tokens, athlete_id, name = result
    try:
        db.link_strava_account(
            user_id=user_id,
            athlete_id=athlete_id,
            access_token_enc=strava_client.encrypt_token(tokens.access_token),
            refresh_token_enc=strava_client.encrypt_token(tokens.refresh_token),
            expires_at=tokens.expires_at,
            scope=strava_client.DEFAULT_SCOPE,
            athlete_name=name,
        )
    except Exception:
        LOG.exception("Strava link db write failed for user %s", user_id)
        return strava_web.error_page("Couldn't save your link. Please try again.")

    LOG.info("Strava linked user=%s athlete=%s (%s)", user_id, athlete_id, name)
    # Make sure the app-wide webhook subscription exists so this athlete's
    # future activities actually push (no manual /strava_subscribe needed).
    if STRAVA_AUTO_SUBSCRIBE:
        bot.loop.create_task(_strava_ensure_subscription())
    try:  # best-effort DM confirmation
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user is not None:
            tail = (
                f" New workouts will post to <#{STRAVA_FEED_CHANNEL_ID}>."
                if STRAVA_FEED_CHANNEL_ID else ""
            )
            await user.send(
                f"✅ Your Strava account (**{name}**) is now linked!{tail}"
            )
    except discord.HTTPException:
        pass
    return strava_web.success_page(name)


def _strava_handle_deauth(payload: dict) -> None:
    """Unlink an athlete who revoked the bot's access in Strava settings.

    Strava sends ``object_type=athlete`` with ``updates={"authorized": "false"}``
    on deauthorization; ``object_id`` is the athlete id. Without handling this we
    keep stale encrypted tokens and fail every refresh forever.
    """
    updates = payload.get("updates") or {}
    if str(updates.get("authorized", "")).lower() != "false":
        return
    try:
        athlete_id = int(payload.get("object_id"))
    except (TypeError, ValueError):
        return
    row = db.get_strava_account_by_athlete(athlete_id)
    if row is not None:
        db.unlink_strava_account(int(row["user_id"]))
        LOG.info(
            "Strava athlete %s deauthorized — unlinked user %s",
            athlete_id, row["user_id"],
        )


def _strava_should_post(activity: "strava_client.StravaActivity") -> bool:
    """Apply the optional sport-type / minimum-distance / duration filters."""
    if STRAVA_SPORT_ALLOW and activity.sport_type.lower() not in STRAVA_SPORT_ALLOW:
        return False
    if (
        STRAVA_MIN_DISTANCE_M
        and strava_client.is_distance_sport(activity.sport_type)
        and activity.distance_m < STRAVA_MIN_DISTANCE_M
    ):
        return False
    if STRAVA_MIN_DURATION_S and (
        activity.moving_time_s or activity.elapsed_time_s
    ) < STRAVA_MIN_DURATION_S:
        return False
    return True


async def _strava_feed_channel():
    """Resolve the configured feed channel, or None if unreachable/unset."""
    if STRAVA_FEED_CHANNEL_ID is None:
        return None
    channel = bot.get_channel(STRAVA_FEED_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STRAVA_FEED_CHANNEL_ID)
        except discord.HTTPException:
            LOG.warning("Strava: cannot reach feed channel %s", STRAVA_FEED_CHANNEL_ID)
            return None
    return channel


async def _strava_fetch_activity(
    row, activity_id: int,
) -> "strava_client.StravaActivity | str":
    def _fetch() -> "strava_client.StravaActivity | str":
        try:
            token = _strava_access_token(row)
            return strava_client.get_activity(token, activity_id)
        except strava_client.StravaAuthError as exc:
            return f"auth: {exc}"
        except Exception as exc:  # pragma: no cover - network
            return f"error: {exc}"

    return await bot.loop.run_in_executor(None, _fetch)


async def _strava_announce_activity(
    row,
    activity: strava_client.StravaActivity,
    *,
    source: str,
) -> str:
    """Process and optionally announce one activity exactly once.

    The durable claim is shared by webhooks and manual backfills, preventing a
    race from producing two feed posts. Returns a compact status for backfill
    summaries; the webhook path only logs failures.
    """
    user_id = int(row["user_id"])
    activity_id = int(activity.id)
    if not db.claim_strava_activity(user_id, activity_id, source):
        return "duplicate"

    if activity.private:
        db.finish_strava_activity(user_id, activity_id)
        db.update_strava_last_activity(user_id, activity_id)
        LOG.info("Strava activity %s is private — not posting", activity_id)
        return "private"
    if not _strava_should_post(activity):
        db.finish_strava_activity(user_id, activity_id)
        db.update_strava_last_activity(user_id, activity_id)
        LOG.info("Strava activity %s filtered out — not posting", activity_id)
        return "filtered"

    channel = await _strava_feed_channel()
    if channel is None:
        db.release_strava_activity(user_id, activity_id)
        return "no_channel"

    who = f"<@{user_id}>"
    try:
        embed, file = await asyncio.to_thread(
            _strava_embed_and_file,
            activity,
            who,
        )
    except Exception:
        db.release_strava_activity(user_id, activity_id)
        LOG.exception("Strava: failed to render activity %s", activity_id)
        return "error"

    verb = "backfilled" if source == "backfill" else "just logged"
    kwargs: dict[str, object] = {
        "content": f"{strava_client.sport_emoji(activity.sport_type)} "
        f"{who} {verb} a workout on Strava!",
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions(users=True),
    }
    if file is not None:
        kwargs["file"] = file
    try:
        msg = await channel.send(**kwargs)
    except discord.HTTPException:
        db.release_strava_activity(user_id, activity_id)
        LOG.exception("Strava: failed to post activity %s", activity_id)
        return "error"
    db.finish_strava_activity(
        user_id,
        activity_id,
        message_id=msg.id,
        channel_id=channel.id,
    )
    db.update_strava_last_activity(user_id, activity_id, msg.id, channel.id)
    return "posted"


async def _strava_handle_create(row, athlete_id: int, activity_id: int) -> None:
    if (
        row["last_activity_id"] is not None
        and int(row["last_activity_id"]) == activity_id
    ):
        return  # duplicate webhook delivery from a pre-ledger install
    result = await _strava_fetch_activity(row, activity_id)
    if isinstance(result, str):
        LOG.warning(
            "Strava activity fetch failed (athlete=%s activity=%s): %s",
            athlete_id, activity_id, result,
        )
        return
    status = await _strava_announce_activity(row, result, source="webhook")
    if status in {"error", "no_channel"}:
        LOG.warning(
            "Strava activity was not announced (athlete=%s activity=%s status=%s)",
            athlete_id, activity_id, status,
        )


def _strava_backfill_candidates(
    activities: Sequence[strava_client.StravaActivity],
    last_activity_id: int | None,
) -> list[strava_client.StravaActivity]:
    """Return missed activities oldest-first, using the saved cursor.

    Strava lists newest-first. If the exact cursor has fallen outside the
    requested date window, activity ids provide a conservative continuation:
    Strava ids increase as activities are created, so older history is not
    replayed.
    """
    newest_first = sorted(
        {activity.id: activity for activity in activities}.values(),
        key=lambda activity: (
            strava_client.start_unix(activity) or 0,
            activity.id,
        ),
        reverse=True,
    )
    if last_activity_id is None:
        missed = newest_first
    else:
        cursor_index = next(
            (
                index
                for index, activity in enumerate(newest_first)
                if activity.id == last_activity_id
            ),
            None,
        )
        if cursor_index is not None:
            missed = newest_first[:cursor_index]
        else:
            missed = [
                activity
                for activity in newest_first
                if activity.id > last_activity_id
            ]
    return list(reversed(missed))


async def _strava_fetch_backfill_summaries(
    row,
    after_epoch: int,
) -> list[strava_client.StravaActivity] | str:
    def _fetch() -> list[strava_client.StravaActivity] | str:
        try:
            token = _strava_access_token(row)
            return strava_client.get_all_activities_since(token, after_epoch)
        except strava_client.StravaAuthError as exc:
            return f"auth: {exc}"
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - network
            return f"error: {exc}"

    return await bot.loop.run_in_executor(None, _fetch)


async def _strava_posted_message(row, activity_id: int):
    """Return the Discord Message we posted for *activity_id*, or None.

    New posts are tracked in the durable per-activity ledger. The account-level
    fields remain as a fallback for announcements made before that ledger was
    introduced.
    """
    imported = db.get_strava_activity_import(int(row["user_id"]), activity_id)
    if imported is not None and imported["status"] == "complete":
        msg_id, ch_id = imported["message_id"], imported["channel_id"]
    elif (
        row["last_activity_id"] is not None
        and int(row["last_activity_id"]) == activity_id
    ):
        msg_id, ch_id = row["last_message_id"], row["last_channel_id"]
    else:
        return None
    if not msg_id or not ch_id:
        return None
    channel = bot.get_channel(int(ch_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(ch_id))
        except discord.HTTPException:
            return None
    try:
        return await channel.fetch_message(int(msg_id))
    except discord.HTTPException:
        return None


async def _strava_handle_update(row, activity_id: int) -> None:
    """Edit the posted embed when an activity is renamed (or remove it if it
    was flipped to private)."""
    msg = await _strava_posted_message(row, activity_id)
    if msg is None:
        return
    result = await _strava_fetch_activity(row, activity_id)
    if isinstance(result, str):
        return
    activity = result
    if activity.private:
        try:
            await msg.delete()
            LOG.info("Strava activity %s went private — removed post", activity_id)
        except discord.HTTPException:
            pass
        return
    who = f"<@{int(row['user_id'])}>"
    embed, _file = _strava_embed_and_file(
        activity,
        who,
        render_route=False,
    )
    # Edit the embed only; the original image attachment (if any) is preserved
    # and the route doesn't change on a rename.
    try:
        await msg.edit(embed=embed)
        LOG.info("Strava activity %s updated — edited post", activity_id)
    except discord.HTTPException:
        LOG.warning("Strava: failed to edit post for activity %s", activity_id)


async def _strava_handle_delete(row, activity_id: int) -> None:
    msg = await _strava_posted_message(row, activity_id)
    if msg is None:
        return
    try:
        await msg.delete()
        LOG.info("Strava activity %s deleted — removed post", activity_id)
    except discord.HTTPException:
        pass


async def _strava_weekly_blocks() -> list[str]:
    """One summary line per linked athlete with activity in the last 7 days.

    Returns ``[]`` when Strava is off/unconfigured or nobody trained — the
    weekly report then simply omits the section.
    """
    if not _strava_enabled():
        return []
    accounts = db.list_strava_accounts()
    if not accounts:
        return []
    after = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())

    def _gather() -> list[tuple[int, int, float, int, float]]:
        out: list[tuple[int, int, float, int, float]] = []
        for row in accounts:
            try:
                token = _strava_access_token(row)
                acts = strava_client.get_activities_since(token, after)
            except Exception:  # pragma: no cover - network
                LOG.warning(
                    "Strava weekly fetch failed for user %s",
                    row["user_id"], exc_info=True,
                )
                continue
            acts = [a for a in acts if not a.private]
            if not acts:
                continue
            out.append((
                int(row["user_id"]),
                len(acts),
                sum(a.distance_m for a in acts),
                sum(a.moving_time_s for a in acts),
                sum(a.total_elevation_gain_m for a in acts),
            ))
        return out

    rows = await bot.loop.run_in_executor(None, _gather)
    if not rows:
        return []
    return _strava_weekly_lines(rows, STRAVA_IMPERIAL)


def _strava_weekly_lines(
    rows: list[tuple[int, int, float, int, float]], imperial: bool,
) -> list[str]:
    """Format aggregated ``(user_id, count, distance_m, secs, elev_m)`` rows
    into one Discord line each, ordered by distance then time. Pure — split out
    from the network gather so it's unit-testable."""
    ordered = sorted(rows, key=lambda r: (r[2], r[3]), reverse=True)
    lines: list[str] = []
    for uid, n, dist, secs, elev in ordered:
        parts = [f"**{n}** activit{'y' if n == 1 else 'ies'}"]
        if dist > 0:
            parts.append(strava_client.format_distance(dist, imperial))
        parts.append(strava_client.format_duration(secs))
        if elev > 0:
            parts.append(strava_client.format_elevation(elev, imperial) + " climb")
        lines.append(f"<@{uid}> — " + " · ".join(parts))
    return lines


def _strava_event_subscription_ok(payload: dict) -> bool:
    """Reject events from a subscription id other than ours, once we know it.

    Fails open when our id isn't known yet (startup) or the payload omits one,
    so genuine events are never dropped — it only filters clearly-foreign ones.
    """
    if _strava_subscription_id is None:
        return True
    raw = payload.get("subscription_id")
    if raw is None:
        return True
    try:
        return int(raw) == _strava_subscription_id
    except (TypeError, ValueError):
        return True


async def _strava_on_event(payload: dict) -> None:
    """Process one webhook event — create/update/delete + deauthorization."""
    if STRAVA_DISABLED:
        return
    if not _strava_event_subscription_ok(payload):
        LOG.warning(
            "Strava event from unexpected subscription_id %s (expected %s) — ignoring",
            payload.get("subscription_id"), _strava_subscription_id,
        )
        return
    object_type = payload.get("object_type")
    if object_type == "athlete":
        _strava_handle_deauth(payload)
        return
    if object_type != "activity":
        return  # unknown object type — ignore
    try:
        athlete_id = int(payload.get("owner_id"))
        activity_id = int(payload.get("object_id"))
    except (TypeError, ValueError):
        return
    row = db.get_strava_account_by_athlete(athlete_id)
    if row is None:
        LOG.info("Strava event for unlinked athlete %s — ignoring", athlete_id)
        return

    aspect = payload.get("aspect_type")
    if aspect == "create":
        await _strava_handle_create(row, athlete_id, activity_id)
    elif aspect == "update":
        await _strava_handle_update(row, activity_id)
    elif aspect == "delete":
        await _strava_handle_delete(row, activity_id)


def _strava_scrub_secret(text: str, secret: str) -> str:
    """Redact the Strava client_secret from an error string before it is logged.

    The view/delete subscription endpoints carry client_secret as a *query param*
    (Strava's API requires it there). We raise only the URL-free response body for
    HTTP errors, but a *transient* requests failure (ConnectionError/timeout)
    stringifies the full request URL — query string and all — so the secret can
    still ride inside such an exception. This is the belt-and-braces net: strip
    the secret out so no log line (transient retry warning, stale-drop warning)
    can ever leak it. No-op when the secret is empty to avoid redacting nothing.
    """
    return text.replace(secret, "<redacted>") if secret else text


async def _strava_ensure_subscription() -> str:
    """Make sure the app's single webhook subscription exists and points at our
    callback. Idempotent — safe to call on startup and on every link.

    Returns a short status string ("exists:<id>" / "created:<id>" /
    "autherror:<msg>" / "error:<msg>" / "unconfigured"). The two error prefixes
    are distinct on purpose: "autherror:" is a *permanent* Strava rejection
    (401/403 — e.g. the API app is Inactive/Forbidden) that retrying cannot fix,
    while "error:" is a *transient* failure (network, tunnel not up yet) worth a
    retry. Neither can contain the client_secret — view/delete now raise only the
    URL-free response body (see strava_client.view_subscriptions).

    If a subscription exists with a *different* callback URL (e.g. the public URL
    changed), it's deleted and recreated, since Strava only allows one per
    application.
    """
    global _strava_subscription_id
    cfg = _strava_cfg()
    if not cfg.configured or not cfg.webhook_callback_url:
        return "unconfigured"

    def _do() -> str:
        subs = strava_client.view_subscriptions(cfg)
        for s in subs:
            if s.get("callback_url") == cfg.webhook_callback_url:
                return f"exists:{s['id']}"
        # Wrong/stale callback — clear it so we can create the right one.
        for s in subs:
            try:
                strava_client.delete_subscription(cfg, int(s["id"]))
            except Exception as drop_exc:  # pragma: no cover - best effort
                # Not exc_info=True: a transient ConnectionError's traceback would
                # embed the secret-bearing request URL. Log the scrubbed message.
                LOG.warning(
                    "Strava: failed to drop stale subscription: %s",
                    _strava_scrub_secret(str(drop_exc), cfg.client_secret),
                )
        return f"created:{strava_client.create_subscription(cfg)}"

    try:
        result = await bot.loop.run_in_executor(None, _do)
    except strava_client.StravaAuthError as exc:
        # Only 401/403 is a *permanent* rejection (e.g. the API app is Inactive —
        # Standard Tier now needs a Strava subscription): flag it so callers don't
        # retry and can show the settings/api hint. A 429 (rate limit) or 5xx
        # (Strava outage) reaches here as a StravaAuthError too, but is transient —
        # fall through to the retrying "error:" branch. The message is Strava's
        # URL-free response body; scrub anyway on the transient path for parity.
        if exc.status_code in (401, 403):
            return f"autherror:{exc}"
        return f"error:{_strava_scrub_secret(str(exc), cfg.client_secret)}"
    except Exception as exc:  # pragma: no cover - network
        # Transient (network/tunnel-not-up) — safe/worth retrying. A requests
        # ConnectionError stringifies the full request URL *including* the query
        # string (which carries client_secret for view/delete); scrub it so the
        # retry-warning log can never leak the secret.
        return f"error:{_strava_scrub_secret(str(exc), cfg.client_secret)}"
    # Cache the live subscription id so the webhook handler can reject events
    # from any other (stale/spoofed) subscription.
    if ":" in result:
        try:
            _strava_subscription_id = int(result.split(":", 1)[1])
        except ValueError:
            pass
    return result


async def _strava_autosubscribe_startup() -> None:  # pragma: no cover - runtime
    """Ensure the subscription a few seconds after boot, retrying so a tunnel
    that isn't quite up yet doesn't permanently skip setup.

    The retry loop exists purely for *transient* failures (the public tunnel not
    being reachable yet). A *permanent* auth failure ("autherror:", e.g. the
    Strava app is Inactive/Forbidden) is not retryable, so we bail immediately
    with one actionable warning rather than hammering the API four times.
    """
    for attempt in range(4):
        await asyncio.sleep(8 if attempt else 4)
        result = await _strava_ensure_subscription()
        if result.startswith(("exists", "created")):
            LOG.info("Strava webhook subscription ready (%s)", result)
            return
        if result == "unconfigured":
            return
        if result.startswith("autherror:"):
            # Permanent rejection — retrying won't help. Log the fix, then stop.
            LOG.warning(
                "Strava webhook subscription blocked: %s. The Strava API app is "
                "likely Inactive/Forbidden — Standard Tier now requires a Strava "
                "subscription for API access; check "
                "https://www.strava.com/settings/api.",
                result.split(":", 1)[1],
            )
            return
        LOG.warning(
            "Strava auto-subscribe attempt %d/4 failed: %s", attempt + 1, result,
        )
    LOG.warning(
        "Strava auto-subscribe gave up — check the public callback is reachable, "
        "then run /strava_subscribe once."
    )


async def _start_strava_server() -> None:  # pragma: no cover - discord runtime
    """Start the Strava OAuth/webhook web server (no-op when disabled)."""
    global _strava_runner
    if not _strava_enabled():
        if not STRAVA_DISABLED and strava_client.available() and not _strava_cfg().configured:
            LOG.info(
                "Strava idle — open the dashboard's Settings tab and fill in "
                "the Strava client ID, client secret and public URL to enable "
                "workout posting."
            )
        return
    cfg = _strava_cfg()
    app = strava_web.build_app(
        verify_token=cfg.verify_token,
        on_callback=_strava_on_callback,
        on_event=_strava_on_event,
        schedule=lambda coro: bot.loop.create_task(coro),
    )
    try:
        _strava_runner = await strava_web.start_server(
            app, STRAVA_BIND_HOST, STRAVA_PORT,
        )
        LOG.info(
            "Strava enabled — feed channel=%s callback=%s",
            STRAVA_FEED_CHANNEL_ID, cfg.redirect_uri,
        )
        if STRAVA_FEED_CHANNEL_ID is None:
            LOG.warning(
                "STRAVA_FEED_CHANNEL_ID is unset — workouts will be received "
                "but not posted anywhere."
            )
        if STRAVA_AUTO_SUBSCRIBE:
            bot.loop.create_task(_strava_autosubscribe_startup())
    except Exception:
        LOG.exception("Failed to start Strava web server")


# The operator dashboard used to be started here. It now lives in the
# supervisor (app/supervisor.py), which serves it whether or not this process
# is running — that is the whole point of the split, since a dashboard you can
# only reach once the bot is working cannot be where you go to fix the bot.
#
# The _webui_* handlers below are unchanged; they are exposed to the supervisor
# through RPC_METHODS instead of being passed to build_app directly.

#: Calls the supervisor may make into this process. Everything here needs a
#: live gateway — anything computable from SQLite alone stays in the supervisor
#: so the dashboard keeps working while the bot is down.
RPC_METHODS: dict[str, object] = {}


def _register_rpc_methods() -> None:
    """Populate RPC_METHODS once every handler above has been defined."""
    RPC_METHODS.update({
        "resync": _webui_resync_guild,
        "list_channels": _webui_list_channels,
        "invite_user": _webui_invite_user,
        "set_member_role": _webui_set_member_role,
        "member_moderation": _webui_member_moderation,
        "remove_timeout": _webui_remove_timeout,
        "set_auto_untimeout": _webui_set_auto_untimeout,
        "announce_blacklist": _webui_announce_blacklist,
        "voice_snapshot": _webui_voice_snapshot,
        "presence_track": _webui_presence_track,
        "reload_config": _rpc_reload_config,
        "status": _rpc_status,
    })


def _rpc_reload_config() -> dict:
    """Re-read settings and rebind the hot ones without restarting."""
    _bind_config(config_mod.load(db, decrypt=_box.decryptor()))
    # Rebinding the global is not enough for a @tasks.loop: discord.py captured
    # the interval when the decorator ran at import, so a dashboard change to
    # REVO_POLL_MINUTES would silently do nothing until the next restart while
    # the UI reported success. change_interval reschedules the running loop.
    if revo_attendance_poll.is_running():
        try:
            if revo_attendance_poll.minutes != REVO_POLL_MINUTES:
                revo_attendance_poll.change_interval(minutes=REVO_POLL_MINUTES)
                LOG.info("Revo poll interval now %d minutes", REVO_POLL_MINUTES)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to apply the new Revo poll interval")
    LOG.info("Configuration reloaded from the dashboard.")
    return {"ok": True}


def _rpc_status() -> dict:
    # discord.py reports latency as NaN before the first heartbeat ack and inf
    # while the websocket is None. round() raises on both, which would make
    # every liveness ping fail during connect and reconnect windows — and the
    # dashboard would report a healthy bot as offline for all its live actions.
    latency = bot.latency
    return {
        "ready": bot.is_ready(),
        "guilds": len(bot.guilds),
        "latency_ms": round(latency * 1000) if math.isfinite(latency) else None,
        "user": str(bot.user) if bot.user else None,
    }


@bot.event
async def setup_hook() -> None:  # pragma: no cover - discord runtime
    """Start the auxiliary servers on the bot's event loop before connecting."""
    await _start_strava_server()
    if os.getenv(workerlink.ROLE_ENV) == "worker":
        sock = os.getenv(workerlink.SOCKET_ENV)
        if sock and workerlink.unix_sockets_supported():
            _register_rpc_methods()
            try:
                global _rpc_runner
                _rpc_runner = await workerlink.serve(sock, RPC_METHODS)
            except Exception:
                LOG.exception(
                    "Could not start the control socket — the dashboard's "
                    "live Discord actions will report the bot as offline."
                )


# Tear the auxiliary web servers down cleanly on shutdown by wrapping
# Client.close (discord.py has no dedicated teardown hook).
_strava_orig_close = bot.close

# Every discord.ext.tasks loop started from on_ready. Client.close shuts down
# Discord's gateway and HTTP session, but it does not own these module-level
# loops, so they must be cancelled and awaited before the event loop closes.
_BACKGROUND_LOOP_NAMES = (
    "online_heartbeat",
    "weekly_reminder",
    "bodyweight_reminder",
    "streak_saver_loop",
    "daily_update",
    "weekly_report",
    "revo_attendance_poll",
    "hevy_poll",
    "ha_poll",
)


async def _stop_background_loops() -> None:
    """Cancel and drain every scheduler started by :func:`on_ready`."""

    running: list[asyncio.Task] = []
    for name in _BACKGROUND_LOOP_NAMES:
        scheduler = globals().get(name)
        if scheduler is None:
            continue
        task = scheduler.get_task()
        if task is None or task.done():
            continue
        scheduler.cancel()
        running.append(task)
    if running:
        await asyncio.gather(*running, return_exceptions=True)


async def _close_with_strava() -> None:  # pragma: no cover - discord runtime
    global _strava_runner, _rpc_runner
    await _stop_background_loops()
    for name, runner in (("Strava", _strava_runner), ("control socket", _rpc_runner)):
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                LOG.warning("%s web server cleanup failed", name, exc_info=True)
    _strava_runner = None
    _rpc_runner = None
    await _strava_orig_close()


bot.close = _close_with_strava  # type: ignore[method-assign]


# ===========================================================================
# Web dashboard data mirror (app/webui.py).
#
# Keeps the members / member_roles / guild_roles / guild_meta tables in step
# with Discord and writes role/member changes to the audit log. Everything here
# is gated on ENABLE_MEMBER_MIRROR (which also turns on the Server Members intent), so
# it's inert for deployments that don't run the dashboard.
# ===========================================================================

def _member_display(member: "discord.Member") -> str:
    return getattr(member, "display_name", None) or member.name


def _member_avatar(member) -> str | None:
    """Resolved Discord avatar URL (server avatar > user avatar > default).

    ``display_avatar`` always returns something — a custom avatar when set, or
    the user's default embed avatar — so the dashboard never has a broken image.
    Stored at a fixed size to keep the CDN URL stable and the thumbnails crisp.
    """
    try:
        asset = member.display_avatar
        sized = asset.with_size(128) if hasattr(asset, "with_size") else asset
        return str(sized.url)
    except Exception:  # pragma: no cover - defensive
        return None


def _role_payload(role: "discord.Role") -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "color": role.colour.value,
        "position": role.position,
        "managed": role.managed,
    }


def _sync_guild_snapshot(guild: "discord.Guild") -> None:
    """Full refresh of one guild's roles + members into the DB mirror.

    Synchronous (DB calls take the connection lock); callers run it directly
    on the loop — it's fast for hobby-sized guilds and only runs on
    startup / manual resync.
    """
    try:
        db.set_guild_meta(guild.id, guild.name, guild.member_count or 0)
        roles = [_role_payload(r) for r in guild.roles if not r.is_default()]
        db.sync_guild_roles(guild.id, roles)
        for m in guild.members:
            db.upsert_member(
                guild.id, m.id, m.name, _member_display(m),
                is_bot=m.bot, present=True,
                joined_at=m.joined_at.isoformat() if m.joined_at else None,
                avatar=_member_avatar(m),
            )
            db.set_member_roles(
                guild.id, m.id,
                [r.id for r in m.roles if not r.is_default()],
            )
    except Exception:  # pragma: no cover - defensive
        LOG.exception(
            "Dashboard guild sync failed for %s", getattr(guild, "id", "?"),
        )


async def _webui_sync_all_guilds() -> None:  # pragma: no cover - discord runtime
    """Mirror every guild the bot is in. Scheduled once from on_ready."""
    for guild in bot.guilds:
        _sync_guild_snapshot(guild)
    LOG.info("Dashboard mirror synced %d guild(s)", len(bot.guilds))


async def _webui_resync_guild(guild_id: int) -> bool:  # pragma: no cover
    """Re-pull one guild on demand (the dashboard's ↻ Sync button)."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    # Members may not all be cached; pull them if the count looks short.
    try:
        if (guild.member_count or 0) > len(guild.members):
            async for _ in guild.fetch_members(limit=None):
                pass
    except Exception:
        LOG.info("fetch_members failed for %s", guild_id, exc_info=True)
    _sync_guild_snapshot(guild)
    return True


async def _webui_list_channels(guild_id: int) -> list[dict]:  # pragma: no cover
    """Text channels of a guild the bot could create an invite in.

    Channels aren't mirrored into SQLite, so this reads the live guild. Returns
    a name/id list ordered the way Discord shows them.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return []
    me = guild.me
    out: list[dict] = []
    for ch in guild.text_channels:
        perms = ch.permissions_for(me) if me is not None else None
        if perms is not None and not perms.create_instant_invite:
            continue
        out.append({"id": str(ch.id), "name": ch.name})
    return out


async def _webui_voice_snapshot(guild_id: int) -> list[dict]:  # pragma: no cover
    """Live "who's in VC right now" for a guild: each voice channel that has
    members, with the occupants. Reads the live guild (voice state isn't
    mirrored into SQLite), so it's always accurate even across bot restarts.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return []
    out: list[dict] = []
    for ch in guild.voice_channels:
        members = [
            {
                "user_id": str(m.id),
                "display_name": m.display_name,
                "self_mute": bool(m.voice and m.voice.self_mute),
                "self_deaf": bool(m.voice and m.voice.self_deaf),
                "streaming": bool(m.voice and m.voice.self_stream),
            }
            for m in ch.members
        ]
        if members:
            out.append({
                "channel_id": str(ch.id),
                "channel_name": ch.name,
                "members": members,
            })
    return out


def _invite_channel(guild: "discord.Guild"):
    """Pick a channel to mint an invite from: the system channel if usable,
    else the first text channel the bot can invite into."""
    me = guild.me
    sysc = guild.system_channel
    if sysc is not None and (
        me is None or sysc.permissions_for(me).create_instant_invite
    ):
        return sysc
    for ch in guild.text_channels:
        if me is None or ch.permissions_for(me).create_instant_invite:
            return ch
    return None


async def _webui_invite_user(
    guild_id: int, user_id: int, channel_id: int | None, actor_name: str,
) -> dict:  # pragma: no cover
    """Create an invite for ``guild_id`` and try to DM it to ``user_id``.

    Discord can't force-add a user by ID (that needs their OAuth consent), so we
    mint an invite link and attempt to DM it. The link is always returned so the
    operator can share it manually if the DM can't be delivered (closed DMs / no
    shared server). Audited as ``invite_create``.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}

    channel = None
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "create_invite"):
            return {"ok": False, "error": "Invite channel not found."}
    else:
        channel = _invite_channel(guild)
    if channel is None:
        return {
            "ok": False,
            "error": "No channel I can create an invite in (need the "
                     "Create Invite permission).",
        }

    try:
        invite = await channel.create_invite(
            max_age=7 * 24 * 3600, max_uses=1, unique=True,
            reason=f"Dashboard invite by {actor_name}",
        )
    except discord.Forbidden:
        return {"ok": False, "error": "I lack permission to create invites here."}
    except discord.HTTPException as exc:
        return {"ok": False, "error": f"Discord rejected the invite: {exc}"}

    dmed = False
    dm_error = None
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException):
        user = None
        dm_error = "No Discord user with that ID."
    if user is not None:
        try:
            await user.send(
                f"You've been invited to **{guild.name}**: {invite.url}"
            )
            dmed = True
        except discord.Forbidden:
            dm_error = "Their DMs are closed, or we share no server yet."
        except discord.HTTPException as exc:
            dm_error = f"DM failed: {exc}"

    subject_name = None
    if user is not None:
        subject_name = getattr(user, "display_name", None) or user.name
    db.add_audit(
        guild_id, "member", "invite_create",
        actor_name=actor_name,
        subject_id=user_id, subject_name=subject_name,
        detail=(
            f"invite {invite.url} for #{getattr(channel, 'name', channel_id)}"
            + (" (DM sent)" if dmed else " (link only)")
        ),
    )
    return {"ok": True, "link": invite.url, "dmed": dmed, "error": dm_error}


def _announce_channel(guild: "discord.Guild"):  # pragma: no cover - discord runtime
    """Pick a public channel to post a blacklist notice in: a configured gym
    channel if one is in this guild, else the system channel, else the first text
    channel the bot can send in."""
    me = guild.me

    def _sendable(ch) -> bool:
        return ch is not None and (
            me is None or ch.permissions_for(me).send_messages
        )

    for cid in GYM_CHANNEL_IDS:
        ch = guild.get_channel(cid)
        if ch is not None and _sendable(ch):
            return ch
    if _sendable(guild.system_channel):
        return guild.system_channel
    for ch in guild.text_channels:
        if _sendable(ch):
            return ch
    return None


async def _webui_announce_blacklist(
    guild_id: int, user_id: int, reason: str | None, actor_name: str,
) -> dict:  # pragma: no cover - discord runtime
    """Post a public message pinging a just-blacklisted user with the reason, so
    the action is visible to the server. Only the target user is mentionable
    (everyone/role pings are disabled regardless of the reason text). Returns
    ``{ok, error, channel}``.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    channel = _announce_channel(guild)
    if channel is None:
        return {"ok": False, "error": "No channel I can post in."}
    reason_txt = (reason or "").strip() or "No reason given."
    try:
        await channel.send(
            f"🚫 <@{user_id}> has been blacklisted — they can no longer log "
            f"anything to the bot.\n**Reason:** {reason_txt}",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False,
                users=[discord.Object(id=user_id)],
            ),
        )
    except discord.Forbidden:
        return {"ok": False, "error": "I lack permission to post there."}
    except discord.HTTPException as exc:
        return {"ok": False, "error": f"Discord rejected the message: {exc}"}
    return {"ok": True, "channel": getattr(channel, "name", str(channel))}


def _role_forbidden_reason(guild: "discord.Guild", role) -> str:  # pragma: no cover
    """Explain *why* Discord refused a role change, in operator-actionable terms.

    The usual culprit isn't a missing permission but role hierarchy: a bot can
    only manage roles positioned **below its own highest role** — and crucially
    **Administrator does NOT bypass this**. So an admin bot still can't grant a
    role that sits at or above its own role in Server Settings → Roles.
    """
    me = guild.me
    if me is not None and not (
        me.guild_permissions.manage_roles or me.guild_permissions.administrator
    ):
        return "I don't have the Manage Roles permission in this server."
    if getattr(role, "managed", False):
        return (
            f"“{role.name}” is managed by an integration, bot, or "
            "Server Boost, so it can't be assigned manually."
        )
    if me is not None and role >= me.top_role:
        return (
            f"“{role.name}” is at or above my own highest role "
            f"(“{me.top_role.name}”). Discord won't let me manage it "
            "even with Administrator. In Server Settings → Roles, drag my "
            "role above it, then try again."
        )
    return (
        "Discord refused the change — this is almost always role hierarchy. "
        "Make sure my role is dragged above the target role in Server Settings "
        "→ Roles (Administrator doesn't override this)."
    )


async def _webui_set_member_role(
    guild_id: int, user_id: int, role_id: int, add: bool, actor_name: str,
) -> dict:  # pragma: no cover
    """Add or remove one role on a guild member (dashboard role editor).

    Members only — the user must already be in the guild. Discord enforces role
    hierarchy at apply time (a role at/above the bot's top role is refused even
    with Administrator), surfaced here as a specific, actionable error. Audited
    as ``role_add`` / ``role_remove``.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "error": "Role not found."}
    if role.is_default():
        return {"ok": False, "error": "Can't assign the @everyone role."}

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return {"ok": False, "error": "That user isn't a member of this server."}

    try:
        if add:
            await member.add_roles(role, reason=f"Dashboard by {actor_name}")
        else:
            await member.remove_roles(role, reason=f"Dashboard by {actor_name}")
    except discord.Forbidden:
        me = guild.me
        LOG.warning(
            "Role change forbidden: role '%s' pos=%s, bot top role '%s' pos=%s, "
            "manage_roles=%s admin=%s managed=%s",
            role.name, role.position,
            me.top_role.name if me else "?",
            me.top_role.position if me else "?",
            me.guild_permissions.manage_roles if me else "?",
            me.guild_permissions.administrator if me else "?",
            getattr(role, "managed", False),
        )
        return {"ok": False, "error": _role_forbidden_reason(guild, role)}
    except discord.HTTPException as exc:
        return {"ok": False, "error": f"Discord rejected the change: {exc}"}

    # Mirror into SQLite immediately so the dashboard reflects it without waiting
    # for the gateway event, and audit under the web operator.
    db.set_member_roles(
        guild_id, user_id,
        [r.id for r in member.roles if not r.is_default()],
    )
    db.add_audit(
        guild_id, "role", "role_add" if add else "role_remove",
        actor_name=actor_name,
        subject_id=user_id, subject_name=_member_display(member),
        detail=(
            f"{'gained' if add else 'lost'} role {role.name} (by {actor_name})"
        ),
    )
    return {"ok": True}


async def _webui_resolve_member(guild, user_id):  # pragma: no cover
    """get_member with a live fetch_member fallback for a sparse cache."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            member = None
    return member


def _bot_can_moderate(guild: "discord.Guild", member: "discord.Member") -> bool:
    """Whether the bot could currently time-out / un-time-out ``member``.

    Mirrors Discord's rules: needs Moderate Members, must outrank the target by
    role position, and can't act on the guild owner. Best-effort — the action
    itself still handles a Forbidden gracefully.
    """
    me = guild.me
    if me is None or not me.guild_permissions.moderate_members:
        return False
    if guild.owner_id == member.id:
        return False
    return me.top_role > member.top_role


async def _webui_member_moderation(guild_id: int, user_id: int) -> dict:  # pragma: no cover
    """Live moderation state for a member: any active timeout, whether the bot
    can act on it, and whether the member is on the auto un-timeout protected
    list. Read from the cache so the dashboard can show/hide the "Remove
    timeout" control and the protection toggle accurately."""
    protected = db.auto_untimeout_is_protected(guild_id, user_id)
    base = {
        "ok": True,
        "auto_untimeout_available": AUTO_UNTIMEOUT,
        "auto_untimeout": protected,
    }
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    member = await _webui_resolve_member(guild, user_id)
    if member is None:
        return {
            **base, "timed_out_until": None, "timed_out": False,
            "can_moderate": False,
        }
    until = member.timed_out_until
    active = until is not None and until > datetime.now(timezone.utc)
    return {
        **base,
        "timed_out_until": until.isoformat() if until else None,
        "timed_out": active,
        "can_moderate": _bot_can_moderate(guild, member),
    }


async def _webui_set_auto_untimeout(
    guild_id: int, user_id: int, enable: bool, actor_name: str,
) -> dict:  # pragma: no cover
    """Add/remove a member from the auto un-timeout protected list (the
    dashboard's per-member Moderation toggle). Audited as
    ``auto_untimeout_enable`` / ``auto_untimeout_disable``."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    member = await _webui_resolve_member(guild, user_id)
    subject_name = _member_display(member) if member else str(user_id)
    if enable:
        changed = db.auto_untimeout_add(guild_id, user_id, added_by=actor_name)
        if changed:
            db.add_audit(
                guild_id, "member", "auto_untimeout_enable",
                actor_name=actor_name,
                subject_id=user_id, subject_name=subject_name,
                detail=f"auto un-timeout protection enabled (by {actor_name})",
            )
        return {"ok": True, "protected": True, "changed": changed}
    changed = db.auto_untimeout_remove(guild_id, user_id)
    if changed:
        db.add_audit(
            guild_id, "member", "auto_untimeout_disable",
            actor_name=actor_name,
            subject_id=user_id, subject_name=subject_name,
            detail=f"auto un-timeout protection disabled (by {actor_name})",
        )
    return {"ok": True, "protected": False, "changed": changed}


async def _webui_remove_timeout(
    guild_id: int, user_id: int, actor_name: str,
) -> dict:  # pragma: no cover
    """Clear a member's timeout (the dashboard's "Remove timeout" button).

    Requires the bot to have Moderate Members and to outrank the member;
    Discord enforces both, surfaced here as an error. Audited as
    ``timeout_remove``."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    member = await _webui_resolve_member(guild, user_id)
    if member is None:
        return {"ok": False, "error": "That user isn't a member of this server."}

    until = member.timed_out_until
    if until is None or until <= datetime.now(timezone.utc):
        return {"ok": True, "changed": False, "error": "Member isn't timed out."}

    try:
        await member.timeout(None, reason=f"Dashboard by {actor_name}")
    except discord.Forbidden:
        return {
            "ok": False,
            "error": "I can't moderate that member — I need Moderate Members "
                     "and a higher role than them.",
        }
    except discord.HTTPException as exc:
        return {"ok": False, "error": f"Discord rejected the change: {exc}"}

    db.add_audit(
        guild_id, "member", "timeout_remove",
        actor_name=actor_name,
        subject_id=user_id, subject_name=_member_display(member),
        detail=f"timeout removed (by {actor_name})",
    )
    return {"ok": True, "changed": True}


async def _webui_presence_track(
    guild_id: int, user_id: int, start: bool, actor_name: str,
) -> dict:  # pragma: no cover
    """Start or stop presence/activity tracking for a member from the dashboard.

    Mirrors the owner-only ``/track start`` and ``/track stop`` slash commands:
    refuses bots, seeds an initial snapshot on start so the activity view has
    something to show immediately, and audits the change. Requires
    ``ENABLE_PRESENCE_TRACKING`` (the dashboard hides the control otherwise, but
    we re-check here in case it's toggled off after a page load)."""
    if not ENABLE_PRESENCE_TRACKING:
        return {
            "ok": False,
            "error": "Presence tracking is disabled — set "
                     "ENABLE_PRESENCE_TRACKING=true and enable the Presence "
                     "intent, then restart the bot.",
        }
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "Server not found or bot not in it."}
    member = await _webui_resolve_member(guild, user_id)
    if member is None:
        return {"ok": False, "error": "That user isn't a member of this server."}
    if member.bot:
        return {"ok": False, "error": "I won't track other bots."}

    if start:
        inserted = db.presence_track_add(guild_id, user_id, started_by=0)
        # Seed status + current activity so the activity view isn't blank until
        # the next gateway transition.
        try:
            _seed_presence_snapshot(guild_id, member)
        except Exception:
            LOG.exception("Failed to seed initial presence snapshot (dashboard)")
        if inserted:
            db.add_audit(
                guild_id, "member", "presence_track_start",
                actor_name=actor_name,
                subject_id=user_id, subject_name=_member_display(member),
                detail=f"presence tracking started (by {actor_name})",
            )
        return {"ok": True, "tracked": True, "changed": inserted}

    removed = db.presence_track_remove(guild_id, user_id)
    if removed:
        db.add_audit(
            guild_id, "member", "presence_track_stop",
            actor_name=actor_name,
            subject_id=user_id, subject_name=_member_display(member),
            detail=f"presence tracking stopped (by {actor_name})",
        )
    return {"ok": True, "tracked": False, "changed": removed}


@bot.event
async def on_member_join(member: "discord.Member") -> None:  # pragma: no cover
    if not ENABLE_MEMBER_MIRROR:
        return
    db.upsert_member(
        member.guild.id, member.id, member.name, _member_display(member),
        is_bot=member.bot, present=True,
        joined_at=member.joined_at.isoformat() if member.joined_at else None,
        avatar=_member_avatar(member),
    )
    db.set_member_roles(
        member.guild.id, member.id,
        [r.id for r in member.roles if not r.is_default()],
    )
    db.add_audit(
        member.guild.id, "member", "join",
        subject_id=member.id, subject_name=_member_display(member),
        detail="joined the server",
    )


@bot.event
async def on_member_remove(member: "discord.Member") -> None:  # pragma: no cover
    if not ENABLE_MEMBER_MIRROR:
        return
    db.set_member_present(member.guild.id, member.id, False)
    db.add_audit(
        member.guild.id, "member", "leave",
        subject_id=member.id, subject_name=_member_display(member),
        detail="left the server",
    )


def _can_read_audit_log(guild: "discord.Guild") -> bool:
    """Whether we'll get an ``on_audit_log_entry_create`` for changes in this
    guild — i.e. the bot has the View Audit Log permission. When True, role and
    nickname changes are audited from that event instead (so they include the
    actor who made the change); ``on_member_update`` only mirrors state. When
    False we fall back to auditing here, without an actor.
    """
    me = guild.me
    return bool(me and guild.me.guild_permissions.view_audit_log)


async def _maybe_auto_untimeout(
    before: "discord.Member", after: "discord.Member",
) -> None:  # pragma: no cover - discord runtime
    """If a *protected* member just became (or had extended) an active timeout,
    clear it.

    Fires on the not-timed-out → timed-out transition (and on a re-applied /
    extended timeout, since the ``until`` value changes). Our own clearing makes
    the next update timed-out → none, which this skips. Only members on the
    guild's per-user protected list (managed from the dashboard's Moderation
    panel) are acted on. Best-effort: a missing permission or a target we don't
    outrank is recorded, not raised.
    """
    if not AUTO_UNTIMEOUT:
        return
    before_until = before.timed_out_until
    after_until = after.timed_out_until
    now = datetime.now(timezone.utc)
    # Only act when there's an active timeout whose value just changed — this
    # ignores our own removal (after_until becomes None) and timeouts that have
    # already expired.
    if after_until is None or after_until <= now or after_until == before_until:
        return
    guild = after.guild
    # Per-user opt-in: only auto-remove timeouts for protected members.
    if not db.auto_untimeout_is_protected(guild.id, after.id):
        return
    name = _member_display(after)
    if not _bot_can_moderate(guild, after):
        db.add_audit(
            guild.id, "member", "timeout_auto_skip",
            subject_id=after.id, subject_name=name,
            detail="timeout detected but I can't moderate this member "
                   "(need Moderate Members + a higher role)",
        )
        return
    try:
        await after.timeout(None, reason="Auto un-timeout")
    except discord.Forbidden:
        db.add_audit(
            guild.id, "member", "timeout_auto_skip",
            subject_id=after.id, subject_name=name,
            detail="couldn't auto-remove timeout — Discord refused (permissions)",
        )
        return
    except discord.HTTPException as exc:
        LOG.warning("Auto un-timeout failed for %s: %s", after.id, exc)
        return
    LOG.info("Auto-removed timeout on %s in guild %s", after.id, guild.id)
    db.add_audit(
        guild.id, "member", "timeout_auto_removed",
        subject_id=after.id, subject_name=name,
        detail="timeout auto-removed (auto un-timeout)",
    )


@bot.event
async def on_member_update(
    before: "discord.Member", after: "discord.Member",
) -> None:  # pragma: no cover - discord runtime
    # Auto un-timeout runs independently of the dashboard mirror, so do it first
    # and don't let the WEBUI gate below skip it.
    try:
        await _maybe_auto_untimeout(before, after)
    except Exception:
        LOG.exception("Auto un-timeout handler failed")
    if not ENABLE_MEMBER_MIRROR:
        return
    gid = after.guild.id
    name = _member_display(after)
    # Prefer the audit-log event (it knows the actor); only audit here when we
    # can't read the guild audit log. The mirror is always kept current.
    audit_here = not _can_read_audit_log(after.guild)
    before_roles = {r.id for r in before.roles if not r.is_default()}
    after_roles = {r.id for r in after.roles if not r.is_default()}
    if before_roles != after_roles:
        db.set_member_roles(gid, after.id, list(after_roles))
        if audit_here:
            for rid in after_roles - before_roles:
                role = after.guild.get_role(rid)
                db.add_audit(
                    gid, "role", "role_add",
                    subject_id=after.id, subject_name=name,
                    detail=f"gained role {role.name if role else rid}",
                )
            for rid in before_roles - after_roles:
                role = before.guild.get_role(rid)
                db.add_audit(
                    gid, "role", "role_remove",
                    subject_id=after.id, subject_name=name,
                    detail=f"lost role {role.name if role else rid}",
                )
    # Server nickname change (username changes arrive via on_user_update).
    if before.nick != after.nick:
        db.upsert_member(
            gid, after.id, after.name, name,
            is_bot=after.bot, present=True,
            joined_at=after.joined_at.isoformat() if after.joined_at else None,
            avatar=_member_avatar(after),
        )
        if audit_here:
            db.add_audit(
                gid, "member", "nick_change",
                subject_id=after.id, subject_name=name,
                detail=f"nickname: {before.nick or '—'} → {after.nick or '—'}",
            )


@bot.event
async def on_audit_log_entry_create(
    entry: "discord.AuditLogEntry",
) -> None:  # pragma: no cover - discord runtime
    """Attribute role/nickname changes to the moderator who made them.

    Discord's gateway member-update event doesn't say *who* changed a role, but
    the guild audit log does. This event delivers each new audit-log entry in
    real time (needs the bot's View Audit Log permission + the non-privileged
    moderation intent, which is on by default), so we record the actor here.
    """
    if not ENABLE_MEMBER_MIRROR or entry.guild is None:
        return
    gid = entry.guild.id
    actor = entry.user
    actor_id = actor.id if actor else None
    actor_name = (_member_display(actor) if actor else None)
    target = entry.target
    subject_id = getattr(target, "id", None)
    subject_name = None
    if subject_id is not None:
        member = entry.guild.get_member(subject_id)
        if member is not None:
            subject_name = _member_display(member)
        else:
            row = db.get_member(gid, subject_id)
            subject_name = row["display_name"] if row else str(subject_id)

    action = entry.action
    A = discord.AuditLogAction
    if action is A.member_role_update:
        added = getattr(entry.after, "roles", None) or []
        removed = getattr(entry.before, "roles", None) or []
        for role in added:
            rname = getattr(role, "name", str(getattr(role, "id", role)))
            db.add_audit(
                gid, "role", "role_add",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=subject_id, subject_name=subject_name,
                detail=f"gained role {rname} (by {actor_name or 'unknown'})",
            )
        for role in removed:
            rname = getattr(role, "name", str(getattr(role, "id", role)))
            db.add_audit(
                gid, "role", "role_remove",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=subject_id, subject_name=subject_name,
                detail=f"lost role {rname} (by {actor_name or 'unknown'})",
            )
    elif action is A.member_update:
        # Nickname changes (only audit when the nick actually changed).
        before_nick = getattr(entry.before, "nick", None)
        after_nick = getattr(entry.after, "nick", None)
        if before_nick != after_nick and (
            hasattr(entry.before, "nick") or hasattr(entry.after, "nick")
        ):
            db.add_audit(
                gid, "member", "nick_change",
                actor_id=actor_id, actor_name=actor_name,
                subject_id=subject_id, subject_name=subject_name,
                detail=(
                    f"nickname: {before_nick or '—'} → {after_nick or '—'}"
                    f" (by {actor_name or 'unknown'})"
                ),
            )
    elif action is A.kick:
        if subject_id:
            db.set_member_present(gid, subject_id, False)
        db.add_audit(
            gid, "member", "kick",
            actor_id=actor_id, actor_name=actor_name,
            subject_id=subject_id, subject_name=subject_name,
            detail=f"kicked by {actor_name or 'unknown'}"
            + (f" — {entry.reason}" if entry.reason else ""),
        )
    elif action is A.ban:
        if subject_id:
            db.set_member_present(gid, subject_id, False)
        db.add_audit(
            gid, "member", "ban",
            actor_id=actor_id, actor_name=actor_name,
            subject_id=subject_id, subject_name=subject_name,
            detail=f"banned by {actor_name or 'unknown'}"
            + (f" — {entry.reason}" if entry.reason else ""),
        )
    elif action is A.unban:
        db.add_audit(
            gid, "member", "unban",
            actor_id=actor_id, actor_name=actor_name,
            subject_id=subject_id, subject_name=subject_name,
            detail=f"unbanned by {actor_name or 'unknown'}",
        )


@bot.event
async def on_user_update(
    before: "discord.User", after: "discord.User",
) -> None:  # pragma: no cover - discord runtime
    if not ENABLE_MEMBER_MIRROR or before.name == after.name:
        return
    # A username change is global; reflect it in every guild we share.
    for guild in bot.guilds:
        m = guild.get_member(after.id)
        if m is None:
            continue
        db.upsert_member(
            guild.id, m.id, after.name, _member_display(m),
            is_bot=m.bot, present=True,
            joined_at=m.joined_at.isoformat() if m.joined_at else None,
            avatar=_member_avatar(m),
        )
        db.add_audit(
            guild.id, "member", "username_change",
            subject_id=after.id, subject_name=_member_display(m),
            detail=f"username: {before.name} → {after.name}",
        )


@bot.event
async def on_guild_role_create(role: "discord.Role") -> None:  # pragma: no cover
    if not ENABLE_MEMBER_MIRROR or role.is_default():
        return
    db.upsert_role(
        role.guild.id, role.id, role.name, role.colour.value,
        role.position, role.managed,
    )
    db.add_audit(
        role.guild.id, "role", "role_create",
        subject_name=role.name, detail=f"role created: {role.name}",
    )


@bot.event
async def on_guild_role_delete(role: "discord.Role") -> None:  # pragma: no cover
    if not ENABLE_MEMBER_MIRROR or role.is_default():
        return
    db.delete_role(role.guild.id, role.id)
    db.add_audit(
        role.guild.id, "role", "role_delete",
        subject_name=role.name, detail=f"role deleted: {role.name}",
    )


@bot.event
async def on_guild_role_update(
    before: "discord.Role", after: "discord.Role",
) -> None:  # pragma: no cover - discord runtime
    if not ENABLE_MEMBER_MIRROR or after.is_default():
        return
    db.upsert_role(
        after.guild.id, after.id, after.name, after.colour.value,
        after.position, after.managed,
    )
    if before.name != after.name:
        db.add_audit(
            after.guild.id, "role", "role_rename",
            subject_name=after.name,
            detail=f"role renamed: {before.name} → {after.name}",
        )


@bot.event
async def on_guild_join(guild: "discord.Guild") -> None:  # pragma: no cover
    if not ENABLE_MEMBER_MIRROR:
        return
    _sync_guild_snapshot(guild)


@bot.tree.command(
    name="strava_link",
    description="Link your Strava account so your new workouts post to the feed.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_link_cmd(interaction: discord.Interaction) -> None:
    if STRAVA_DISABLED:
        await interaction.response.send_message(
            "Strava integration is disabled (STRAVA_DISABLED=1).", ephemeral=True,
        )
        return
    if not strava_client.available():
        await interaction.response.send_message(
            "Strava client unavailable — install `requests` and `cryptography`.",
            ephemeral=True,
        )
        return
    cfg = _strava_cfg()
    if not cfg.configured or not cfg.redirect_uri:
        await interaction.response.send_message(
            "Strava isn't configured on the host yet (the bot owner needs to "
            "set `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` and "
            "`STRAVA_PUBLIC_URL`).",
            ephemeral=True,
        )
        return
    state = secrets.token_urlsafe(24)
    db.create_strava_pending(state, interaction.user.id)
    url = strava_client.build_authorize_url(cfg, state)
    await interaction.response.send_message(
        "🔗 **Link your Strava account**\n"
        f"1. Open this link and approve access: {url}\n"
        "2. You'll get a confirmation here once it's done.\n\n"
        "Your tokens are stored **encrypted**. Revoke any time with "
        "`/strava_unlink`.",
        ephemeral=True,
    )


@bot.tree.command(
    name="strava_unlink",
    description="Unlink your Strava account and revoke the bot's access.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_unlink_cmd(interaction: discord.Interaction) -> None:
    row = db.get_strava_account(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You don't have a linked Strava account.", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    def _revoke() -> None:
        try:
            token = _strava_access_token(row)
            strava_client.deauthorize(token)
        except Exception:  # pragma: no cover - best effort
            LOG.info("Strava deauthorize on unlink failed", exc_info=True)

    await bot.loop.run_in_executor(None, _revoke)
    db.unlink_strava_account(interaction.user.id)
    await interaction.followup.send(
        "🗑️ Strava unlinked and access revoked. Encrypted tokens removed.",
        ephemeral=True,
    )


@bot.tree.command(
    name="strava_status",
    description="Show whether your Strava account is linked.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_status_cmd(interaction: discord.Interaction) -> None:
    row = db.get_strava_account(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "No Strava account linked. Use `/strava_link` to connect one.",
            ephemeral=True,
        )
        return
    name = row["athlete_name"] or "your account"
    feed = (
        f"\nNew workouts post to <#{STRAVA_FEED_CHANNEL_ID}>."
        if STRAVA_FEED_CHANNEL_ID else
        "\n⚠️ No feed channel configured — workouts won't post yet."
    )
    await interaction.response.send_message(
        f"✅ Linked as **{name}** (athlete id `{row['athlete_id']}`).{feed}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Hevy slash commands + poll loop
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="hevy_link",
    description="Link your Hevy account so your workouts import as lifts + post to the feed.",
)
@app_commands.describe(
    api_key="Your Hevy API key (Hevy app → Settings → API; requires Hevy Pro).",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_link_cmd(
    interaction: discord.Interaction, api_key: str,
) -> None:
    if not _hevy_enabled():
        await interaction.response.send_message(
            "Hevy integration isn't available (disabled, or the host is missing "
            "`requests`/`cryptography` or a Fernet key).",
            ephemeral=True,
        )
        return
    gid = _ctx_guild_id(interaction)
    if not gid:
        await interaction.response.send_message(
            "I couldn't tell which server to link this to — DM me from a server "
            "we share, or set your default with `/server`.",
            ephemeral=True,
        )
        return
    api_key = api_key.strip()
    await interaction.response.defer(thinking=True, ephemeral=True)

    def _verify() -> dict:
        return hevy_client.verify_key(api_key)

    try:
        result = await bot.loop.run_in_executor(None, _verify)
    except hevy_client.HevyAuthError:
        await interaction.followup.send(
            "❌ Hevy rejected that API key. Copy it again from the Hevy app → "
            "Settings → API (you'll need Hevy Pro).",
            ephemeral=True,
        )
        return
    except hevy_client.HevyError as exc:
        await interaction.followup.send(
            f"⚠️ Couldn't reach Hevy to verify the key: {exc}", ephemeral=True,
        )
        return
    try:
        db.hevy_link(
            interaction.user.id, gid, hevy_client.encrypt_key(api_key),
        )
    except hevy_client.HevyUnavailable as exc:
        await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        return
    feed = (
        f" New workouts will import as lifts and post to <#{HEVY_FEED_CHANNEL_ID}>."
        if HEVY_FEED_CHANNEL_ID else
        " New workouts will import as lifts (no feed channel configured)."
    )
    await interaction.followup.send(
        f"✅ Hevy linked ({result.get('count', 0)} workouts found). Your key is "
        f"stored **encrypted**.{feed}\nWorkouts sync about every "
        f"{HEVY_POLL_MINUTES} min. Unlink any time with `/hevy_unlink`.",
        ephemeral=True,
    )


@bot.tree.command(
    name="hevy_unlink",
    description="Unlink your Hevy account and delete your stored API key.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_unlink_cmd(interaction: discord.Interaction) -> None:
    removed = db.hevy_unlink(interaction.user.id)
    await interaction.response.send_message(
        "🗑️ Hevy unlinked — your encrypted API key was removed."
        if removed else "You don't have a linked Hevy account.",
        ephemeral=True,
    )


@bot.tree.command(
    name="hevy_status",
    description="Show whether your Hevy account is linked.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_status_cmd(interaction: discord.Interaction) -> None:
    row = db.hevy_get(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "No Hevy account linked. Use `/hevy_link` to connect one.",
            ephemeral=True,
        )
        return
    feed = (
        f"\nWorkouts import as lifts and post to <#{HEVY_FEED_CHANNEL_ID}>."
        if HEVY_FEED_CHANNEL_ID else
        "\nWorkouts import as lifts (no feed channel configured)."
    )
    last = row["last_synced_at"] or "not yet"
    await interaction.response.send_message(
        f"✅ Hevy linked. Last sync: {last}.{feed}", ephemeral=True,
    )


@bot.tree.command(
    name="hevy_sync",
    description="Re-sync your last 50 Hevy workouts (imports any the bot missed).",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_sync_cmd(interaction: discord.Interaction) -> None:
    if not _hevy_enabled():
        await interaction.response.send_message(
            "Hevy integration isn't available right now.", ephemeral=True,
        )
        return
    row = db.hevy_get(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You haven't linked Hevy yet — see `/hevy_help`.", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    result = await _hevy_sync_account(row, force_backfill=True)
    if result.get("error") == "auth":
        await interaction.followup.send(
            "❌ Hevy rejected your API key — re-link with `/hevy_link`.",
            ephemeral=True,
        )
        return
    if result.get("error"):
        await interaction.followup.send(
            "Couldn't reach Hevy just now — try again shortly.", ephemeral=True,
        )
        return
    if result["new"] == 0:
        await interaction.followup.send(
            "✅ Already up to date — no new workouts to import.", ephemeral=True,
        )
        return
    feed = (
        f" A summary was posted to <#{HEVY_FEED_CHANNEL_ID}>."
        if HEVY_FEED_CHANNEL_ID else ""
    )
    prs = f", {result['prs']} new PR{'s' if result['prs'] != 1 else ''}" if result["prs"] else ""
    await interaction.followup.send(
        f"✅ Imported **{result['new']}** workout"
        f"{'s' if result['new'] != 1 else ''} — {result['lifts']} lifts, "
        f"{result['volume_kg']:,} kg volume{prs}.{feed}",
        ephemeral=True,
    )


@bot.tree.command(
    name="hevy_help",
    description="How the Hevy integration works and how to link your account.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_help_cmd(interaction: discord.Interaction) -> None:
    feed = (
        f" and posts a summary to <#{HEVY_FEED_CHANNEL_ID}>"
        if HEVY_FEED_CHANNEL_ID else ""
    )
    embed = discord.Embed(
        title="🏋️ Hevy integration",
        colour=HEVY_COLOUR,
        description=(
            "Link your [Hevy](https://www.hevy.com/) account and I'll pull in "
            f"each workout you finish — logging every weighted set as a lift{feed}."
        ),
    )
    embed.add_field(
        name="1 · Get your API key",
        value=(
            "In the Hevy app: **Settings → API → Generate API Key** "
            "(requires **Hevy Pro**), then copy it."
        ),
        inline=False,
    )
    embed.add_field(
        name="2 · Link it",
        value=(
            "Run `/hevy_link api_key:<your key>` — do this in a **DM** with me to "
            "keep the key private. It's stored **encrypted**; the plaintext is "
            "never saved or shown back."
        ),
        inline=False,
    )
    embed.add_field(
        name="How syncing works",
        value=(
            "Hevy has no instant push, so I **check for new workouts about every "
            f"{HEVY_POLL_MINUTES} min** — expect up to that long after you finish "
            "before it appears. Each workout imports once; the **first** sync "
            "after linking pulls your history **silently** (no feed spam)."
        ),
        inline=False,
    )
    embed.add_field(
        name="What you get",
        value=(
            "• Every weighted working set becomes a **lift** (feeds PRs, "
            "leaderboards and your stats)\n"
            "• A per-workout summary: exercises, sets, reps, volume, duration, "
            "a per-exercise breakdown and your top set"
        ),
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/hevy_link` — link your account\n"
            "`/hevy_recent` — show your most recent workout\n"
            "`/hevy_sync` — re-sync your last 50 workouts\n"
            "`/hevy_status` — check link + last sync time\n"
            "`/hevy_unlink` — remove your key and stop syncing\n"
            "`/hevy_help` — this message"
        ),
        inline=False,
    )
    if not _hevy_enabled():
        embed.add_field(
            name="⚠️ Currently unavailable",
            value=(
                "The host hasn't finished setting up Hevy (missing dependencies "
                "or encryption key), so linking is disabled for now."
            ),
            inline=False,
        )
    embed.set_footer(text="Hevy Pro required · your key is encrypted at rest")
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _hevy_duration_str(seconds: int | None) -> str | None:
    """Render a workout's elapsed time as ``1h 23m`` / ``45m`` / ``30s``."""
    if not seconds or seconds <= 0:
        return None
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{s}s"


def _hevy_exercise_line(ex: dict) -> str:
    """One breakdown line: name + set count + top set (or reps for bodyweight)."""
    sets = ex.get("sets") or 0
    bits = [f"{sets} set{'s' if sets != 1 else ''}"]
    if ex.get("best_weight_kg"):
        reps = f"×{ex['best_reps']}" if ex.get("best_reps") else ""
        bits.append(f"top {ex['best_weight_kg']:g}kg{reps}")
    elif ex.get("reps"):
        bits.append(f"{ex['reps']} reps")
    return f"**{_safe_label(ex.get('title') or 'Exercise')}** · " + " · ".join(bits)


def _collapse_prs(
    prs: list[tuple[Lift, float | None]],
) -> dict[str, tuple[float, float | None]]:
    """Collapse ``_new_prs_for_lifts`` output to the best new PR per equipment:
    ``{equipment: (weight_kg, previous_best_or_None)}``."""
    best: dict[str, tuple[float, float | None]] = {}
    for lift, prev in prs:
        cur = best.get(lift.equipment)
        if cur is None or lift.weight_kg > cur[0]:
            best[lift.equipment] = (lift.weight_kg, prev)
    return best


def _hevy_pr_lines(prs_by_equip: dict[str, tuple[float, float | None]]) -> list[str]:
    lines = []
    for equip, (weight, prev) in prs_by_equip.items():
        was = f" (was {prev:g}kg)" if prev else " — first time!"
        lines.append(f"**{_safe_label(equip)}** {weight:g}kg{was}")
    return lines


def _hevy_workout_embed(
    member_name: str, summary: dict,
    prs_by_equip: dict[str, tuple[float, float | None]] | None = None,
) -> discord.Embed:
    """Build the feed embed for a completed Hevy workout — the full stat line,
    a per-exercise breakdown, any new personal bests, and the heaviest set."""
    warm = summary.get("warmup_set_count") or 0
    sets_str = f"**{summary.get('working_set_count', summary['set_count'])}** sets"
    if warm:
        sets_str += f" (+{warm} warmup)"
    desc = [
        f"**{summary['exercise_count']}** exercises",
        sets_str,
        f"**{summary.get('total_reps', 0):,}** reps",
        f"**{summary['volume_kg']:,} kg** volume",
    ]
    dur = _hevy_duration_str(summary.get("duration_seconds"))
    if dur:
        desc.append(f"⏱️ **{dur}**")
    embed = discord.Embed(
        title=summary["title"], colour=HEVY_COLOUR, description=" · ".join(desc),
    )
    embed.set_author(name=f"🏋️ {member_name} finished a Hevy workout")

    # Per-exercise breakdown, capped so the field stays within Discord's 1024
    # char limit (and doesn't dominate the feed for a 20-exercise session).
    exercises = summary.get("exercises") or []
    lines: list[str] = []
    for ex in exercises[:12]:
        lines.append(_hevy_exercise_line(ex))
    if len(exercises) > 12:
        lines.append(f"…and {len(exercises) - 12} more")
    if lines:
        embed.add_field(
            name="Exercises", value="\n".join(lines)[:1024], inline=False,
        )

    if prs_by_equip:
        pr_lines = _hevy_pr_lines(prs_by_equip)
        embed.add_field(
            name="🎉 New PR!" if len(pr_lines) == 1 else "🎉 New PRs!",
            value="\n".join(pr_lines)[:1024], inline=False,
        )
    top = summary.get("top")
    if top:
        reps = f" × {top['reps']}" if top.get("reps") else ""
        embed.add_field(
            name="🏆 Top set",
            value=f"{_safe_label(top['title'])} — {top['weight_kg']:g}kg{reps}",
            inline=False,
        )
    when = _parse_hevy_time(summary.get("start_time"))
    if when is not None:
        embed.timestamp = when
    embed.set_footer(text="via Hevy")
    return embed


def _hevy_backfill_embed(member_name: str, stats: dict) -> discord.Embed:
    """One-shot summary embed for a backfill/manual sync (instead of spamming a
    separate embed per historical workout)."""
    desc = [
        f"**{stats['workouts']}** workouts",
        f"**{stats['lifts']}** lifts",
        f"**{stats['volume_kg']:,} kg** total volume",
    ]
    embed = discord.Embed(
        title="📥 Hevy history synced",
        colour=HEVY_COLOUR,
        description=" · ".join(desc),
    )
    embed.set_author(name=f"🏋️ {member_name}'s workouts imported")
    if stats.get("pr_count"):
        embed.add_field(
            name="🎉 Personal bests",
            value=f"{stats['pr_count']} new PR"
            f"{'s' if stats['pr_count'] != 1 else ''} set across these workouts",
            inline=False,
        )
    first, last = stats.get("first"), stats.get("last")
    if first and last:
        f_dt, l_dt = _parse_hevy_time(first), _parse_hevy_time(last)
        if f_dt and l_dt:
            rng = (
                f"<t:{int(f_dt.timestamp())}:D> → <t:{int(l_dt.timestamp())}:D>"
            )
            embed.add_field(name="Date range", value=rng, inline=False)
    embed.set_footer(text="via Hevy")
    return embed


def _hevy_import_workout(
    user_id: int, guild_id: int, username: str, workout: dict,
) -> dict | None:
    """Import one Hevy workout's lifts (deduped), detecting any new PRs first.

    Returns ``{"summary", "lifts", "prs", "when"}`` for a freshly-imported
    workout, or None if it has no id or was already imported. PRs are computed
    against the DB **before** the insert so the comparison excludes this
    workout's own sets."""
    wid = str(workout.get("id") or "")
    if not wid or db.hevy_workout_imported(user_id, wid):
        return None
    if not db.hevy_mark_workout(user_id, wid):
        return None  # raced with another poll
    lifts = hevy_client.workout_to_lifts(workout)
    prs = _collapse_prs(_new_prs_for_lifts(guild_id, user_id, lifts)) if lifts else {}
    when = _parse_hevy_time(workout.get("start_time"))
    if lifts:
        try:
            db.add_lifts(
                guild_id=guild_id, user_id=user_id, username=username,
                lifts=lifts, logged_at=when,
            )
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Hevy: failed to import workout %s", wid)
    return {
        "summary": hevy_client.summarize_workout(workout),
        "lifts": lifts, "prs": prs, "when": when,
    }


async def _hevy_sync_account(row, *, force_backfill: bool = False) -> dict:
    """Import a linked member's new Hevy workouts as lifts and post to the feed.

    On the first sync (or a forced backfill) it pulls up to 50 recent workouts,
    imports their history with the original dates, and posts a **single** summary
    announcement rather than one embed per workout. On routine polls it imports
    just the new workouts and posts a full-stats embed (with PR callouts) for
    each. Returns a small stats dict for callers that want to report the result
    (e.g. the manual ``/hevy sync`` command)."""
    result = {"new": 0, "lifts": 0, "volume_kg": 0, "prs": 0, "backfill": False}
    user_id = int(row["user_id"])
    guild_id = int(row["guild_id"])
    try:
        api_key = hevy_client.decrypt_key(row["api_key_enc"])
    except hevy_client.HevyError:
        LOG.warning("Hevy: unreadable API key for user %s — skipping", user_id)
        return result

    first_sync = row["last_synced_at"] is None
    # Accounts linked before the 50-workout backfill existed have a NULL
    # backfilled_at — catch them up automatically on their next poll.
    never_backfilled = (
        "backfilled_at" not in row.keys() or row["backfilled_at"] is None
    )
    backfill = first_sync or force_backfill or never_backfilled
    result["backfill"] = backfill

    def _fetch() -> list[dict]:
        if backfill:
            return hevy_client.fetch_recent_workouts(api_key, limit=50)
        return hevy_client.fetch_workouts(api_key, page=1, page_size=10)

    try:
        workouts = await bot.loop.run_in_executor(None, _fetch)
    except hevy_client.HevyAuthError:
        LOG.warning("Hevy: API key for user %s was rejected (revoked?)", user_id)
        result["error"] = "auth"
        return result
    except hevy_client.HevyError as exc:
        LOG.info("Hevy: fetch failed for user %s: %s", user_id, exc)
        result["error"] = "fetch"
        return result

    member = None
    guild = bot.get_guild(guild_id)
    if guild is not None:
        member = guild.get_member(user_id)
    username = _display_name(member) if member else str(user_id)
    feed_channel = (
        bot.get_channel(HEVY_FEED_CHANNEL_ID) if HEVY_FEED_CHANNEL_ID else None
    )

    # Hevy returns newest-first; import oldest-first so logs read chronologically
    # and PRs accrue in the right order.
    imported: list[dict] = []
    for workout in reversed(workouts):
        r = _hevy_import_workout(user_id, guild_id, username, workout)
        if r is not None:
            imported.append(r)

    result["new"] = len(imported)
    result["lifts"] = sum(len(r["lifts"]) for r in imported)
    result["volume_kg"] = sum(r["summary"]["volume_kg"] for r in imported)
    result["prs"] = sum(len(r["prs"]) for r in imported)

    if feed_channel is not None and imported:
        if backfill:
            # One summary instead of up to 50 individual embeds.
            starts = [r["summary"].get("start_time") for r in imported]
            starts = [s for s in starts if s]
            stats = {
                "workouts": result["new"], "lifts": result["lifts"],
                "volume_kg": result["volume_kg"], "pr_count": result["prs"],
                "first": min(starts) if starts else None,
                "last": max(starts) if starts else None,
            }
            try:
                await feed_channel.send(
                    embed=_hevy_backfill_embed(username, stats),
                )
            except discord.HTTPException:  # pragma: no cover - best effort
                LOG.info("Hevy: failed to post backfill summary for %s", user_id)
        else:
            for r in imported:
                if not r["lifts"]:
                    continue
                try:
                    await feed_channel.send(
                        embed=_hevy_workout_embed(
                            username, r["summary"], r["prs"],
                        ),
                    )
                except discord.HTTPException:  # pragma: no cover - best effort
                    LOG.info("Hevy: failed to post feed embed for %s", user_id)

    db.hevy_mark_synced(user_id)
    if backfill:
        db.hevy_mark_backfilled(user_id)
    return result


def _parse_hevy_time(value: object) -> datetime | None:
    """Parse a Hevy ISO-8601 timestamp into a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@tasks.loop(minutes=HEVY_POLL_MINUTES)
async def hevy_poll() -> None:
    """Poll every linked Hevy account for new workouts."""
    if not _hevy_enabled():
        return
    for row in db.list_hevy_accounts():
        try:
            await _hevy_sync_account(row)
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Hevy poll failed for user %s", row["user_id"])


@hevy_poll.before_loop
async def _hevy_poll_before() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


@bot.tree.command(
    name="hevy_recent",
    description="Show the most recent Hevy workout (yours, or another member's).",
)
@app_commands.describe(member="Whose latest workout to show. Defaults to you.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def hevy_recent_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if not _hevy_enabled():
        await interaction.response.send_message(
            "Hevy integration isn't available right now.", ephemeral=True,
        )
        return
    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    row = db.hevy_get(target.id)
    if row is None:
        msg = (
            "You haven't linked Hevy yet — see `/hevy_help`."
            if target.id == interaction.user.id
            else f"{target.mention} hasn't linked a Hevy account."
        )
        await interaction.response.send_message(
            msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await interaction.response.defer(thinking=True)

    def _fetch() -> "dict | None | str":
        try:
            api_key = hevy_client.decrypt_key(row["api_key_enc"])
            workouts = hevy_client.fetch_workouts(api_key, page=1, page_size=1)
            return workouts[0] if workouts else None
        except hevy_client.HevyAuthError:
            return "auth"
        except hevy_client.HevyError as exc:
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _fetch)
    if isinstance(result, str):
        if result == "auth":
            msg = (
                "❌ Hevy rejected your API key — re-link with `/hevy_link`."
                if target.id == interaction.user.id
                else f"{target.display_name}'s Hevy key was rejected (they may "
                "need to re-link)."
            )
        else:
            LOG.info("Hevy recent fetch failed for %s: %s", target.id, result)
            msg = "Couldn't reach Hevy just now — try again shortly."
        await interaction.followup.send(
            msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if result is None:
        await interaction.followup.send(
            f"No Hevy workouts found for {target.display_name}.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    summary = hevy_client.summarize_workout(result)
    embed = _hevy_workout_embed(_display_name(target), summary)
    await interaction.followup.send(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------------------------------------------------------------------
# Home Assistant slash commands + poll loop
# ---------------------------------------------------------------------------
# One server, one operator-held token, everybody's sensors. So unlike Hevy there
# is no per-member credential to store — a member links the *entity prefix* that
# their smart scale writes to, and the poll fetches /api/states once per cycle
# and fans that single response out across every linked member.
#
# A new weigh-in goes through db.set_bodyweight, deliberately, so it lands in the
# same table a chat-typed "bw 106.3" does and keeps feeding TDEE, the protein
# link, bodyweight goals and the graphs. The extra metrics a scale reports (body
# fat %, muscle mass, BMI, ...) have nowhere to go in that model, so they go to
# body_metrics alongside it.

def _ha_metric_lines(metrics: dict) -> list[str]:
    """Display lines for a reading's non-weight metrics, in registry order.

    Registry order rather than dict order so two members' embeds list their
    metrics identically even when their scales report in different orders."""
    lines: list[str] = []
    for metric in ha_client.METRICS:
        if metric.key == ha_client.WEIGHT_KEY:
            continue
        entry = metrics.get(metric.key)
        if not entry:
            continue
        shown = ha_client.format_metric(
            metric.key, float(entry["value"]), str(entry.get("unit") or ""),
        )
        lines.append(f"{metric.emoji} **{metric.label}** · {shown}")
    return lines


def _ha_delta_str(delta_kg: float | None, days_since: int | None) -> str:
    """``▼ 0.40 kg since 3 days ago`` — or "" when there's nothing to compare to.

    No colour language on the direction. The bot does not know whether this
    member is cutting or bulking, so calling a loss "good" is as likely to be
    wrong as right."""
    if delta_kg is None:
        return ""
    if abs(delta_kg) < 0.005:
        text = "no change"
    else:
        arrow = "▲" if delta_kg > 0 else "▼"
        text = f"{arrow} {abs(delta_kg):.2f} kg"
    if days_since is None:
        return text
    if days_since <= 0:
        return f"{text} since earlier today"
    if days_since == 1:
        return f"{text} since yesterday"
    return f"{text} in {days_since} days"


def _ha_weighin_embed(member_name: str, reading: dict, protein_grams: int | None,
                      ) -> discord.Embed:
    """The channel announcement for one new weigh-in."""
    weight_kg = float(reading["weight_kg"])
    delta = _ha_delta_str(reading.get("delta_kg"), reading.get("days_since"))
    description = f"**{weight_kg:.2f} kg**"
    if delta:
        description += f" · {delta}"
    if SHOW_LB:
        description += f"\n{weight_kg * 2.2046226218487757:.1f} lb"
    embed = discord.Embed(
        title=f"⚖️ {_plain_label(member_name)} weighed in",
        description=description,
        colour=ui.HOME_ASSISTANT,
    )
    lines = _ha_metric_lines(reading.get("metrics") or {})
    if lines:
        embed.add_field(
            name="Body composition",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    if protein_grams:
        embed.add_field(
            name="Protein target",
            value=f"Updated to **{protein_grams} g** (tied to your bodyweight).",
            inline=False,
        )
    when = reading.get("measured_at")
    if isinstance(when, datetime):
        embed.timestamp = when
    entity = _plain_label(str(reading.get("entity_id") or ""), limit=80)
    embed.set_footer(text=f"Home Assistant · {entity}" if entity
                     else "Home Assistant")
    return embed


def _ha_backfill_embed(member_name: str, stats: dict) -> discord.Embed:
    """One summary for a first-link history import, instead of N alerts."""
    count = int(stats.get("count") or 0)
    lines = [f"Imported **{count}** past weigh-in{'s' if count != 1 else ''}."]
    first, last = stats.get("first"), stats.get("last")
    if isinstance(first, datetime) and isinstance(last, datetime) and count > 1:
        lines.append(
            f"{first.strftime('%d %b')} → {last.strftime('%d %b')}"
        )
    lo, hi = stats.get("min_kg"), stats.get("max_kg")
    if lo is not None and hi is not None:
        lines.append(f"Range: **{lo:.2f}–{hi:.2f} kg**")
    latest = stats.get("latest_kg")
    if latest is not None:
        lines.append(f"Latest: **{latest:.2f} kg**")
    embed = discord.Embed(
        title=f"⚖️ {_plain_label(member_name)} linked their scale",
        description="\n".join(lines),
        colour=ui.HOME_ASSISTANT,
    )
    embed.set_footer(text="Home Assistant · history backfill")
    return embed


async def _ha_send_announcement(
    channel,
    embed: discord.Embed,
    chart: _BodyweightChart | None = None,
):
    """Send one HA announcement, retrying text/embed-only if files are blocked."""
    kwargs = {
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if chart is None:
        return await channel.send(**kwargs)
    try:
        return await channel.send(
            **kwargs, file=_bodyweight_chart_file(chart),
        )
    except discord.HTTPException as exc:
        if not _attachment_retryable(exc):
            raise
        LOG.info(
            "Home Assistant: chart attachment rejected in channel %s; "
            "retrying announcement without it",
            getattr(channel, "id", "?"),
        )
        return await channel.send(**kwargs)


def _ha_valid_weight(weight_kg: float) -> bool:
    """Reject a weight before it reaches the database.

    ``db.set_bodyweight`` does no range checking — every other caller validates
    first, and this one must too. Without it a scale glitching to 6553.5 kg (a
    real BLE failure mode: an unscaled 16-bit register) is stored permanently and
    poisons every leaderboard's true-weight line."""
    if weight_kg <= 0:
        return False
    return not (MAX_WEIGHT_KG > 0 and weight_kg > MAX_WEIGHT_KG)


def _ha_store_metrics(
    user_id: int, guild_id: int, metrics: dict | None, measured_at: datetime,
) -> int:
    """Write a reading's non-weight metrics. Returns rows written.

    Idempotent by the table's UNIQUE key, which is what lets a re-reported
    weigh-in top up the body composition it didn't have the first time without
    the caller checking anything."""
    if not metrics:
        return 0
    payload = {
        k: (float(v["value"]), str(v.get("unit") or ""))
        for k, v in metrics.items()
        if k != ha_client.WEIGHT_KEY
    }
    if not payload:
        return 0
    try:
        return db.add_body_metrics(
            guild_id, user_id, payload, recorded_at=measured_at,
        )
    except Exception:  # pragma: no cover - defensive
        LOG.exception(
            "Home Assistant: failed to store body metrics for %s", user_id,
        )
        return 0


def _ha_import_reading(
    user_id: int, guild_id: int, weight_kg: float, measured_at: datetime,
    metrics: dict | None = None, replay_guard_kg: float | None = None,
    key: str | None = None,
) -> dict | None:
    """Import one weigh-in, deduped. Returns a small result dict, or None.

    None means "nothing to do": already imported, a restored state, out of
    range, or lost the claim race. The claim (``ha_mark_reading``) happens
    *before* the write and before any announcement, so two overlapping sync
    attempts for the same member cannot double-log or double-post —
    whichever call loses the INSERT OR IGNORE simply returns None.

    ``key`` is the de-duplication key. It comes from the scale's own
    ``measurement_id`` when the integration publishes one and falls back to the
    weight sensor's ``last_changed`` otherwise; see
    :func:`ha_client.readings_from_history_attr` for why the former is preferred.

    ``replay_guard_kg`` is the last weight imported from Home Assistant, and only
    applies to the ``last_changed`` path. When the incoming weight matches it,
    this is a *restored* state rather than a new weigh-in: restarting Home
    Assistant re-creates every entity with a fresh ``last_changed``, producing an
    unseen key for a value that never changed, so without the guard every HA
    update announced a duplicate weigh-in for every linked member. Nothing
    legitimate is lost — HA does not bump ``last_changed`` for an unchanged
    value, so a genuinely repeated weight never reaches here with a new key.
    """
    key = key or ha_client.reading_key(measured_at)
    if not key or not _ha_valid_weight(weight_kg):
        if key and not _ha_valid_weight(weight_kg):
            LOG.warning(
                "Home Assistant: ignoring implausible weight %.2f kg for user %s",
                weight_kg, user_id,
            )
        return None
    if db.ha_reading_imported(user_id, key):
        # Already logged — but a scale that re-reports a weigh-in with body
        # composition attached can deliver the richer half on a later poll than
        # the bare weight. Top the metrics up (the UNIQUE key makes it a no-op if
        # they're already there) and still return None, so nothing is re-logged
        # and nothing is announced twice.
        if metrics:
            _ha_store_metrics(user_id, guild_id, metrics, measured_at)
        return None
    if replay_guard_kg is not None and abs(weight_kg - replay_guard_kg) < 0.005:
        LOG.debug(
            "Home Assistant: %s kg for user %s is a restored state, not a new "
            "weigh-in", weight_kg, user_id,
        )
        return None
    if not db.ha_mark_reading(user_id, key):
        return None  # raced with another sync attempt
    protein_grams = None
    try:
        protein_grams = db.set_bodyweight(
            guild_id, user_id, weight_kg, recorded_at=measured_at,
        )
    except Exception:  # pragma: no cover - defensive
        LOG.exception("Home Assistant: failed to store weigh-in for %s", user_id)
        # Hand the claim back. It was taken before the write, so keeping it after
        # a failure would make the next poll skip this weigh-in as "already
        # imported" and lose it for good.
        db.ha_release_reading(user_id, key)
        return None
    # Only now that a row exists: the guard has to describe a weigh-in that was
    # actually written, or a failed write leaves it blocking its own retry.
    db.ha_note_reading(user_id, weight_kg, measured_at)
    written = _ha_store_metrics(user_id, guild_id, metrics, measured_at)
    return {
        "weight_kg": weight_kg,
        "measured_at": measured_at,
        "metrics_written": written,
        "protein_grams": protein_grams,
    }


def _ha_states_for(row, states: list[dict]) -> dict[str, dict]:
    """The linked member's ``{metric_key: state}``, prefix first.

    The stored ``weight_entity`` is a fallback for the case the prefix scheme
    cannot cover: a weight sensor whose id doesn't share a prefix with the rest
    (renamed by hand in HA, or a different integration supplying the scale)."""
    prefix = str(row["entity_prefix"] or "")
    mine = dict(ha_client.entities_for_prefix(states, prefix))
    if ha_client.WEIGHT_KEY in mine:
        return mine
    wanted = str(row["weight_entity"] or "").strip().lower()
    if not wanted:
        return mine
    for state in states:
        if str(state.get("entity_id") or "").strip().lower() == wanted:
            mine[ha_client.WEIGHT_KEY] = state
            break
    return mine


async def _ha_fetch_history(
    cfg, entity_id: str, days: int, unit: str = "",
) -> list[tuple[datetime, float]] | None:
    """Past weigh-ins for one weight entity, oldest-first.

    Returns **None** when the call failed and ``[]`` when it succeeded with
    nothing to show. The distinction matters: the caller marks the account
    backfilled afterwards, and treating a timed-out recorder as "no history"
    would burn the one-time import on a transient error. Either way the live
    reading still imports — a missing history must not block today's weigh-in."""
    if cfg is None or not entity_id or days <= 0:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)

    def _fetch() -> list[tuple[datetime, float]]:
        series = ha_client.fetch_history(cfg, [entity_id], since)
        rows = ha_client.history_for_entity(series, entity_id)
        if not rows:
            return []
        # ``unit`` comes from the live state the caller already holds. History rows
        # are fetched with no_attributes so they carry no unit of their own, and
        # re-fetching it here meant a failed lookup fell back to "assume kg" —
        # which on an imperial install silently stored 234 lb as 234 kg, under the
        # implausible-weight cap and therefore undetected.
        return ha_client.weight_history_from_states(rows, unit=unit)

    try:
        return await bot.loop.run_in_executor(None, _fetch)
    except ha_client.HAError as exc:
        LOG.info("Home Assistant: history fetch failed for %s: %s", entity_id, exc)
        return None


async def _ha_sync_account(row, states: list[dict]) -> dict:
    """Import a linked member's new weigh-ins and announce the newest one.

    ``states`` is the shared ``/api/states`` response, fetched once per poll.

    On the first sync it also pulls ``HA_BACKFILL_DAYS`` of history from HA's
    recorder and posts a **single** summary rather than one alert per
    historical weigh-in. Returns a stats dict rather than raising.
    """
    result: dict = {
        "new": 0, "metrics": 0, "backfill": False, "latest_kg": None,
        "protein_grams": None,
    }
    user_id = int(row["user_id"])
    guild_id = int(row["guild_id"])

    mine = _ha_states_for(row, states)
    reading = ha_client.build_reading(mine)

    first_sync = row["last_synced_at"] is None
    # A row written before backfilling existed has a NULL backfilled_at — catch
    # it up once, the way Hevy does.
    never_backfilled = (
        "backfilled_at" not in row.keys() or row["backfilled_at"] is None
    )
    backfill = (first_sync or never_backfilled) and HA_BACKFILL_DAYS > 0
    result["backfill"] = backfill
    # Whether a multi-weigh-in import is announced as "linked their scale".
    # Kept separate from `backfill` so a HA_BACKFILL_DAYS=0 install still
    # recognises a first sync as a link event, not a routine weigh-in.
    is_first_import = first_sync or never_backfilled

    # The comparison point has to be read before anything is written, and it
    # comes from `bodyweights` rather than from HA — so a chat-logged "bw 107"
    # yesterday still gives today's scale reading something to diff against.
    previous = db.get_latest_bodyweight(guild_id, user_id)
    prev = (
        {"weight_kg": previous["weight_kg"],
         "recorded_at": previous["recorded_at"]}
        if previous is not None else None
    )

    # Build one chronological work list. Ordering is load-bearing: every
    # set_bodyweight re-derives a bodyweight-linked protein target from the
    # weight it was handed, so the newest reading must be written LAST or the
    # member's protein ceiling ends up derived from a weigh-in from last week.
    #
    # Preferred source is the scale's own history attribute, which carries stable
    # measurement ids and needs no recorder. Only when the entity doesn't publish
    # one do we fall back to the live state plus /api/history/period.
    weight_state = mine.get(ha_client.WEIGHT_KEY)
    attr_readings = (
        ha_client.readings_from_history_attr(weight_state)
        if weight_state is not None else None
    )

    pending: list[dict] = []
    from_attribute = bool(attr_readings)
    if attr_readings:
        # Older entries are gated by the backfill window; the newest is always
        # included, so HA_BACKFILL_DAYS=0 still means "from now on" rather than
        # "never".
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=min(max(HA_BACKFILL_DAYS, 0), ha_client.MAX_BACKFILL_DAYS),
        )
        newest = attr_readings[-1]
        for entry in attr_readings:
            if entry is newest or entry["measured_at"] >= cutoff:
                pending.append(entry)
        # Fold re-reports of one weigh-in together FIRST, before either guard
        # below looks at the list. A scale reports the weight the moment you step
        # on, then re-reports the same measurement seconds later with body
        # composition attached under a fresh measurement id — which id
        # de-duplication cannot see, so the pair announced itself twice. Folding
        # early also matters because the survivor keeps the *earliest* key, which
        # is what lets the already-imported check below recognise it.
        pending = ha_client.collapse_same_weight(pending)
        # The last entry and the live sensors are usually the same weigh-in, so
        # hand it the full metric set rather than the one or two the log carries.
        # Only when the weights agree, though: an integration that writes the
        # sensor before appending to its log leaves a live reading that is a
        # *newer* measurement, and merging then files this weigh-in's body
        # composition against the previous one. Queue it separately instead.
        if reading is not None:
            live_kg = float(reading["weight_kg"])
            latest = pending[-1] if pending else None
            same = (
                latest is not None
                and abs(live_kg - float(latest["weight_kg"])) < 0.005
            )
            if same:
                pending[-1] = ha_client.merge_live_metrics(
                    latest, reading.get("metrics") or {},
                )
            elif reading["measured_at"] > newest["measured_at"]:
                pending.append(reading)
        # Anything at or before the newest weigh-in already imported contributes
        # its metrics but can never become a new weigh-in. That is what makes a
        # *source switch* safe: a member synced on the last_changed path has those
        # weigh-ins recorded under ISO-timestamp keys, and the same measurements
        # arriving under `id:` keys would otherwise re-import and re-announce
        # every one of them. Marking rather than dropping them keeps the body
        # composition a re-report carries, which dropping threw away.
        through = (
            ha_client.parse_ha_time(row["last_reading_at"])
            if "last_reading_at" in row.keys() else None
        )
        if through is not None:
            pending = [
                {**entry, "metrics_only": True}
                if entry["measured_at"] <= through else entry
                for entry in pending
            ]
    else:
        if reading is not None:
            pending.append({**reading, "live": True})
        if backfill:
            entity_id = str(
                (reading or {}).get("entity_id") or row["weight_entity"] or ""
            )
            history = await _ha_fetch_history(
                _ha_cfg_for(row), entity_id,
                min(HA_BACKFILL_DAYS, ha_client.MAX_BACKFILL_DAYS),
                unit=str(
                    (weight_state.get("attributes") or {}).get(
                        "unit_of_measurement") or ""
                ) if weight_state is not None else "",
            )
            if history is None:
                # The recorder call failed. Do NOT mark the account backfilled, or
                # a transient error burns the one-time history import for good.
                backfill = False
                result["backfill"] = False
                history = []
            live_key = reading["key"] if reading else ""
            for when, kg in history:
                if ha_client.reading_key(when) == live_key:
                    continue  # already covered, and with its metrics
                pending.append({
                    "weight_kg": kg, "measured_at": when,
                    "key": ha_client.reading_key(when), "metrics": None,
                })
        pending.sort(key=lambda item: item["measured_at"])

    # The replay guard compares against the last weight Home Assistant reported,
    # and applies to exactly one entry: the one read from the live state on the
    # timestamp-keyed path. That is the only place a Home Assistant restart can
    # manufacture a new key for an unchanged value.
    #
    # It must NOT touch recorder-history rows — they are older than the weight the
    # guard came from, so a genuine past weigh-in that happened to equal today's
    # would be silently dropped. Nor the measurement-id path, where ids are stable
    # across restarts and the guard would instead discard a real second weigh-in
    # at an unchanged weight.
    guard = row["last_weight_kg"] if "last_weight_kg" in row.keys() else None
    guard = float(guard) if guard is not None else None

    imported: list[dict] = []
    for entry in pending:
        kg = float(entry["weight_kg"])
        if entry.get("metrics_only"):
            _ha_store_metrics(
                user_id, guild_id, entry.get("metrics"), entry["measured_at"],
            )
            continue
        r = _ha_import_reading(
            user_id, guild_id, kg, entry["measured_at"], entry.get("metrics"),
            replay_guard_kg=guard if entry.get("live") else None,
            key=entry.get("key"),
        )
        if r is not None:
            r["reading"] = entry
            imported.append(r)

    db.ha_mark_synced(user_id)
    if backfill:
        db.ha_mark_backfilled(user_id)

    if not imported:
        return result

    newest = max(imported, key=lambda r: r["measured_at"])
    result["new"] = len(imported)
    result["metrics"] = sum(r["metrics_written"] for r in imported)
    result["latest_kg"] = newest["weight_kg"]
    result["protein_grams"] = newest["protein_grams"]

    channel = await _ha_alert_channel()
    if channel is None:
        return result

    member = None
    guild = bot.get_guild(guild_id)
    if guild is not None:
        member = guild.get_member(user_id)
    username = _display_name(member) if member else str(user_id)
    # The whole pending batch is committed now, so one render captures the
    # actual latest global timeline. Attach it to the summary or newest routine
    # announcement rather than posting a second standalone message.
    chart = await _updated_bodyweight_chart(user_id, username)

    # A first-time history import gets ONE summary; anything else gets an embed
    # per weigh-in. Gated on `is_first_import` rather than on the count or on
    # `backfill`, because a member who stood on the scale twice between polls
    # is not linking their scale, and must not be publicly told they just did.
    routine_posts: list[tuple[discord.Message, datetime]] = []
    routine_chart_message_id: int | None = None
    try:
        if is_first_import and len(imported) > 1:
            weights = [r["weight_kg"] for r in imported]
            times = [r["measured_at"] for r in imported]
            posted = await _ha_send_announcement(
                channel,
                _ha_backfill_embed(username, {
                    "count": len(imported),
                    "first": min(times), "last": max(times),
                    "min_kg": min(weights), "max_kg": max(weights),
                    "latest_kg": newest["weight_kg"],
                }),
                chart,
            )
            # The summary stands for the whole batch, so one ❌ undoes all of it —
            # which is exactly what somebody wants after a bad first import.
            await _ha_offer_undo(
                posted, user_id, guild_id,
                [r["measured_at"] for r in imported],
                chart_message_id=posted.id if chart is not None else None,
            )
        else:
            # Oldest-first, each diffed against the one before it, so two
            # readings minutes apart read as a sequence rather than as two
            # unrelated jumps from last week's weight. Capped so a scale that
            # dumps a burst can't flood the channel.
            shown = sorted(imported, key=lambda r: r["measured_at"])[-3:]
            running = prev
            for index, entry in enumerate(shown):
                # The reading the import actually used, so the embed's metrics
                # match the row written rather than the current live state.
                source = entry.get("reading") or {}
                payload = ha_client.summarize_reading(
                    {
                        "weight_kg": entry["weight_kg"],
                        "measured_at": entry["measured_at"],
                        "entity_id": source.get("entity_id")
                        or (reading or {}).get("entity_id", ""),
                        "metrics": source.get("metrics")
                        or (reading or {}).get("metrics") or {},
                    },
                    running,
                )
                posted = await _ha_send_announcement(
                    channel,
                    _ha_weighin_embed(
                        username, payload, entry["protein_grams"],
                    ),
                    chart if index == len(shown) - 1 else None,
                )
                routine_posts.append((posted, entry["measured_at"]))
                if index == len(shown) - 1 and chart is not None:
                    routine_chart_message_id = posted.id
                running = {
                    "weight_kg": entry["weight_kg"],
                    "recorded_at": entry["measured_at"],
                }
    except discord.HTTPException:  # pragma: no cover - best effort
        LOG.info("Home Assistant: failed to announce weigh-in for %s", user_id)
    # Do not expose the undo reaction until the batch's chart holder is known.
    # If a send failed part-way through, the successfully posted prefix still
    # gets normal per-message undo tracking, simply without a chart association.
    for posted, measured_at in routine_posts:
        await _ha_offer_undo(
            posted,
            user_id,
            guild_id,
            [measured_at],
            chart_message_id=routine_chart_message_id,
        )
    return result


async def _ha_offer_undo(
    message: "discord.Message", user_id: int, guild_id: int,
    measured_ats: list[datetime], *, chart_message_id: int | None = None,
) -> None:
    """Record what an announcement covers and add the ❌ affordance.

    Best effort on both halves: a missing reaction permission must not turn a
    successful import into an error, and the tracking row is still worth having
    because somebody can add the ❌ themselves."""
    stamps = [_ha_stamp(when) for when in measured_ats if when is not None]
    if not stamps:
        return
    try:
        db.ha_track_reply(
            message.id,
            user_id,
            guild_id,
            stamps,
            chart_message_id=chart_message_id,
        )
    except Exception:  # pragma: no cover - defensive
        LOG.exception("Home Assistant: failed to track weigh-in reply")
        return
    try:
        await message.add_reaction("❌")
    except discord.HTTPException:  # pragma: no cover - best effort
        LOG.info("Home Assistant: couldn't add the undo reaction")


def _ha_when(value: object) -> str:
    """Render a stored UTC timestamp for Discord, in the reader's own timezone.

    ``<t:epoch:f>`` is localised by each client, which beats formatting against
    DISPLAY_TIMEZONE: members are not all in the operator's timezone, and a raw
    stored value is in UTC and reads as simply wrong to everybody. Falls back to
    the raw string if it can't be parsed, since a slightly ugly timestamp is
    better than none.
    """
    when = value if isinstance(value, datetime) else ha_client.parse_ha_time(value)
    if when is None:
        return str(value) if value else "not yet"
    return f"<t:{int(when.timestamp())}:f>"


def _ha_stamp(when: datetime) -> str:
    """The exact string ``set_bodyweight`` stored this weigh-in under.

    Undo matches on the timestamp, so this has to agree with the database's own
    normalisation byte for byte — hence calling it rather than formatting here."""
    return db_normalize_iso(when)


def _ha_undo_scope(payload, rec) -> tuple[int, list[str]] | None:
    """Who owns the weigh-ins an ❌ is trying to undo, and which ones.

    ``rec`` is the tracking row when the announcement was recorded. Returns None
    when the reactor isn't allowed: the member themselves or an admin, matching
    the lift-undo rule."""
    user_id = int(rec["user_id"])
    if payload.user_id not in ({user_id} | ADMIN_USER_IDS):
        return None
    stamps = [s for s in str(rec["recorded_ats"] or "").split(",") if s]
    return (user_id, stamps) if stamps else None


async def _ha_undo_untracked(payload, message) -> tuple[int, list[str]] | None:
    """Work out what an *untracked* weigh-in announcement covers, from its embed.

    Announcements posted before the tracking table existed carry no row, and the
    ask was explicitly that those be undoable too. The embed has everything
    needed: the weight in its description and the measurement time as its
    timestamp. Looking the pair up in ``bodyweights`` also yields the user id,
    which the embed only carries as a display name.

    Returns None when this isn't one of ours, the weigh-in can't be found, or the
    reactor isn't the member or an admin.
    """
    if not message.embeds:
        return None
    embed = message.embeds[0]
    footer = (getattr(embed.footer, "text", "") or "")
    if not footer.startswith("Home Assistant"):
        return None
    if embed.timestamp is None:
        # The batch summary has no timestamp, so there is nothing to match on.
        return None
    match = re.search(r"\*\*([\d.]+)\s*kg\*\*", embed.description or "")
    if match is None:
        return None
    try:
        weight = float(match.group(1))
    except ValueError:
        return None
    # Try the reactor's own weigh-ins first. Two members weighing the same at the
    # same instant is unlikely but possible, and resolving to the wrong person's
    # row is the one outcome worth engineering against. Only an admin acting on
    # somebody else's announcement falls through to the unrestricted search.
    row = db.find_bodyweight_near(weight, embed.timestamp,
                                  user_id=payload.user_id)
    if row is None and payload.user_id in ADMIN_USER_IDS:
        row = db.find_bodyweight_near(weight, embed.timestamp)
    if row is None:
        return None
    user_id = int(row["user_id"])
    if payload.user_id not in ({user_id} | ADMIN_USER_IDS):
        return None
    return user_id, [str(row["recorded_at"])]


async def _handle_ha_reaction_undo(payload) -> bool:
    """❌ on a weigh-in announcement removes that import. True if handled.

    Deliberately last in the reaction chain, after lifts and nutrition: those
    identify their messages by a tracking row, and this one falls back to reading
    the embed, so it must not get first refusal on somebody else's message.
    """
    rec = db.ha_get_reply(payload.message_id)
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            return False
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return False
    if message.author.id != (bot.user.id if bot.user else 0):
        return False

    if rec is not None:
        scope = _ha_undo_scope(payload, rec)
        if scope is None:
            return False
        chart_message_id = (
            int(rec["chart_message_id"])
            if "chart_message_id" in rec.keys()
            and rec["chart_message_id"] is not None
            else None
        )
        # Claim it before deleting anything, so two simultaneous reactions can't
        # both run the delete.
        if db.ha_delete_reply(payload.message_id) == 0:
            return False
        guild_id = int(rec["guild_id"])
    else:
        scope = await _ha_undo_untracked(payload, message)
        if scope is None:
            return False
        guild_id = _msg_guild_id(message)
        chart_message_id = None
    user_id, stamps = scope

    actor_name = None
    reactor = None
    if message.guild is not None:
        reactor = message.guild.get_member(payload.user_id)
    if reactor is not None:
        actor_name = _display_name(reactor)
    removed = db.delete_weighins_at(
        user_id, stamps, guild_id=guild_id,
        actor_id=payload.user_id, actor_name=actor_name,
    )
    by_admin = payload.user_id in ADMIN_USER_IDS and payload.user_id != user_id
    who = "an admin" if by_admin else "the member"
    note = (
        f"↩️ Removed {_plural(removed, 'weigh-in')} at {who}'s request. "
        f"It won't be re-imported."
        if removed else "↩️ Nothing to undo (already removed)."
    )
    if chart_message_id is not None and chart_message_id != payload.message_id:
        try:
            holder = await channel.fetch_message(chart_message_id)
            if holder.author.id == (bot.user.id if bot.user else 0):
                await holder.edit(attachments=[])
        except discord.HTTPException:  # pragma: no cover - best effort
            LOG.info(
                "Home Assistant: couldn't remove the stale batch graph from %s",
                chart_message_id,
            )
    try:
        # The announcement may carry the automatic history graph. Once its
        # weigh-in (or whole backfill batch) is gone, keeping that PNG visible
        # would leave a stale snapshot attached to an "Removed" message.
        await message.edit(content=note, embed=None, attachments=[])
    except discord.HTTPException:  # pragma: no cover - best effort
        LOG.info("Home Assistant: couldn't edit the undone announcement")
    return True


async def _ha_fetch_states_for(row) -> list[dict] | None:
    """Fetch one member's states, or None on any failure (already logged).

    Each member has their own Home Assistant, so this is one request per member
    per cycle rather than a single shared dump. A member whose server is down,
    whose token was revoked, or whose reverse proxy is misbehaving must not stop
    anybody else syncing — hence None rather than an exception."""
    cfg = _ha_cfg_for(row)
    if cfg is None or not cfg.configured:
        return None

    def _fetch() -> list[dict]:
        return ha_client.fetch_states(cfg)

    try:
        return _ha_visible_states(await bot.loop.run_in_executor(None, _fetch))
    except ha_client.HAAuthError:
        LOG.warning(
            "Home Assistant rejected user %s's access token — their sync is "
            "paused until they re-run /setup_ha.", row["user_id"],
        )
    except ha_client.HAError as exc:
        LOG.info(
            "Home Assistant: state fetch failed for user %s: %s",
            row["user_id"], exc,
        )
    return None


@tasks.loop(minutes=HA_POLL_MINUTES)
async def ha_poll() -> None:
    """Poll each member's own Home Assistant for new weigh-ins."""
    if not _ha_enabled():
        return
    for row in db.list_ha_synced():
        try:
            states = await _ha_fetch_states_for(row)
            if states is None:
                continue
            await _ha_sync_account(row, states)
        except Exception:  # pragma: no cover - defensive
            LOG.exception(
                "Home Assistant poll failed for user %s", row["user_id"],
            )


@ha_poll.before_loop
async def _ha_poll_before() -> None:  # pragma: no cover - discord runtime
    await bot.wait_until_ready()


def _ha_describe_prefix(prefix: str, mine: dict) -> dict | None:
    """Summarise one person-prefix bucket for linking and for ``/ha_entities``."""
    weight = mine.get(ha_client.WEIGHT_KEY)
    if weight is None:
        return None
    friendly = str((weight.get("attributes") or {}).get("friendly_name") or "")
    return {
        "prefix": prefix,
        "weight_entity": str(weight.get("entity_id") or ""),
        "friendly_name": friendly,
        "metrics": sorted(mine),
        "reading": ha_client.build_reading(mine),
    }


def _ha_resolve_target(raw: str, states: list[dict]) -> dict | None:
    """Turn what a member typed into a linkable prefix, using live states.

    Accepts a full entity id, a prefix, or any distinctive word from either the
    prefix or the sensor's friendly name — people paste all three, and real
    prefixes are not typeable: the scale this was built against produces
    ``renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s``, so "joshua" has to work.

    Candidates are ranked, not taken first-match, because one person legitimately
    owns several weight sensors. On the install this was built against, "joshua"
    matches both ``joshua_s_iphone`` (Apple Health, permanently ``unavailable``
    because nothing writes to it) and the real Renpho scale. First-match linked
    the iPhone and then silently never synced, so a bucket with a *live reading*
    outranks one with a weight entity but no value.
    """
    text = (raw or "").strip().lower().replace(" ", "_")
    if not text:
        return None
    grouped = ha_client.group_body_entities(states)

    if "." in text:
        hit = ha_client.classify_entity(text)
        if hit is None:
            return None
        return _ha_describe_prefix(hit[1], grouped.get(hit[1]) or {})

    scored: list[tuple[tuple, dict]] = []
    for prefix, mine in grouped.items():
        described = _ha_describe_prefix(prefix, mine)
        if described is None:
            continue
        haystack = f"{prefix} {described['friendly_name']}".lower().replace(
            " ", "_")
        if text == prefix:
            match = 0
        elif prefix and (prefix.startswith(text) or text.startswith(prefix)):
            # `prefix and` is load-bearing. A single-person install has bare
            # entities like `sensor.weight`, whose person-prefix is "" — and
            # `text.startswith("")` is True for every query, so the empty bucket
            # would match anything typed and outrank the real scale (which usually
            # matches only on `in`). It is reachable by an exact "" or by entity id.
            match = 1
        elif text in haystack:
            match = 2
        else:
            continue
        # Lower sorts first. Liveness is ranked ABOVE match quality on purpose:
        # "joshua" prefix-matches `joshua_s_iphone` but only appears *inside*
        # `renpho_scale_..._joshua_s`, so ranking the tighter match first picks
        # the dead Apple Health bridge over the real scale. An exact prefix (or a
        # full entity id, handled above) is the one way to ask for a bucket that
        # isn't reading — which is also what a brand-new scale looks like before
        # anyone stands on it.
        scored.append((
            (0 if match == 0 else 1,
             0 if described["reading"] is not None else 1,
             match,
             -len(described["metrics"]),
             len(prefix)),
            described,
        ))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0])
    return scored[0][1]


async def _ha_states_or_error(
    interaction: discord.Interaction, cfg: "ha_client.HAConfig | None",
) -> list[dict] | None:
    """Fetch a member's ``/api/states``, replying with a diagnosis on failure.

    Returns None when it replied — callers just return. Each message names the
    thing *they* can change, because "couldn't connect" sends someone hunting
    through logs for something the bot already knows.
    """
    if cfg is None or not cfg.configured:
        await interaction.followup.send(
            "I couldn't read your stored Home Assistant token. Re-run "
            "`/setup_ha` to set it again.",
            ephemeral=True,
        )
        return None

    def _fetch() -> list[dict]:
        return ha_client.fetch_states(cfg)

    try:
        return _ha_visible_states(await bot.loop.run_in_executor(None, _fetch))
    except ha_client.HAAuthError:
        await interaction.followup.send(
            "❌ Home Assistant rejected your access token — it was probably "
            "revoked. Make a new one (Home Assistant → your profile → "
            "**Security** → Long-lived access tokens) and run `/setup_ha` again.",
            ephemeral=True,
        )
        return None
    except ha_client.HABanned as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return None
    except ha_client.HACertError as exc:
        await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        return None
    except ha_client.HAUnreachable as exc:
        # The client's message already names the likely fix (usually the mDNS
        # hostname), so don't bury it under a generic hint.
        await interaction.followup.send(
            f"⚠️ {exc}\nRun `/setup_ha` with a corrected address if that's wrong.",
            ephemeral=True,
        )
        return None
    except ha_client.HAError as exc:
        await interaction.followup.send(
            f"⚠️ Home Assistant returned an error: {exc}", ephemeral=True,
        )
        return None


@bot.tree.command(
    name="setup_ha",
    description="Connect your own Home Assistant so your weigh-ins sync automatically.",
)
@app_commands.describe(
    url="Where your Home Assistant lives, e.g. https://home.example.com",
    token="A long-lived access token: HA → your profile → Security → Create token.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def setup_ha_cmd(
    interaction: discord.Interaction, url: str, token: str,
) -> None:
    """Store, verify and (where unambiguous) finish a member's HA setup."""
    if not _ha_enabled():
        await interaction.response.send_message(
            "Home Assistant integration isn't available (disabled, or the host "
            "is missing `requests`/`cryptography` or an encryption key).",
            ephemeral=True,
        )
        return
    gid = _ctx_guild_id(interaction)
    if not gid:
        await interaction.response.send_message(
            "I couldn't tell which server to link this to — DM me from a server "
            "we share, or set your default with `/server`.",
            ephemeral=True,
        )
        return

    problem = config_mod.bad_base_url(url) or config_mod.bad_access_token(token)
    if problem:
        await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        return

    # Ephemeral from the very first response: the token is an argument, so the
    # reply must never be public even on the error paths.
    await interaction.response.defer(thinking=True, ephemeral=True)
    cfg = ha_client.config_for(url, token, verify_ssl=HA_VERIFY_SSL)

    def _verify() -> dict:
        return ha_client.ping(cfg)

    try:
        info = await bot.loop.run_in_executor(None, _verify)
    except ha_client.HAAuthError:
        await interaction.followup.send(
            "❌ Home Assistant answered, but rejected that token. Create a fresh "
            "one under your profile → **Security** → Long-lived access tokens "
            "and paste the whole thing.",
            ephemeral=True,
        )
        return
    except ha_client.HAError as exc:
        await interaction.followup.send(
            f"⚠️ Couldn't reach Home Assistant: {exc}", ephemeral=True,
        )
        return

    try:
        db.ha_server_set(
            interaction.user.id, cfg.base_url, ha_client.encrypt_token(cfg.token),
            ha_version=info.get("version") or None,
            location=info.get("location") or None,
        )
    except ha_client.HAUnavailable as exc:
        await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        return

    version = f" (HA {info['version']})" if info.get("version") else ""
    lines = [
        f"✅ Connected to `{cfg.base_url}`{version}. Your token is stored "
        f"**encrypted**, and the bot only ever reads — it never changes anything "
        f"in your home."
    ]

    # Finish the job where it is unambiguous. Most people have exactly one set of
    # body sensors, and making them run a second command to confirm the only
    # option is friction for its own sake.
    states = await _ha_states_or_error(interaction, cfg)
    if states is None:
        return
    grouped = ha_client.group_body_entities(states)
    live = {p: m for p, m in grouped.items()
            if ha_client.build_reading(m) is not None}
    existing = db.ha_get(interaction.user.id)

    if len(live) == 1 and existing is None:
        prefix = next(iter(live))
        found = _ha_describe_prefix(prefix, live[prefix])
        db.ha_link(
            interaction.user.id, gid, prefix,
            weight_entity=found["weight_entity"],
            friendly_name=found["friendly_name"] or None,
        )
        channel_id = _ha_alert_channel_id()
        lines.append(
            f"Linked your **{len(live[prefix])} body sensors** automatically "
            f"(`{prefix or '(no prefix)'}`) — it was the only set with a reading."
        )
        lines.append(
            f"Weigh-ins sync about every {HA_POLL_MINUTES} min and log as your "
            f"bodyweight, so TDEE, protein targets and the graphs follow along"
            + (f", and post to <#{channel_id}>." if channel_id else ".")
        )
        if channel_id:
            lines.append(
                "The attached public graph uses your **global bodyweight "
                "history**, including manual readings logged in DMs or other "
                "shared servers."
            )
        lines.append("`/ha_body` shows your numbers.")
    elif existing is not None:
        lines.append(
            f"You're already linked to `{existing['entity_prefix'] or '(no prefix)'}`"
            " — that's unchanged. Use `/ha_link` to point at a different set."
        )
    elif not live:
        lines.append(
            "I couldn't find any body-composition sensors with a reading on it "
            "yet. Stand on your scale once, then run `/ha_entities`."
        )
    else:
        names = ", ".join(f"`{p or '(no prefix)'}`" for p in sorted(live)[:6])
        lines.append(
            f"Found **{len(live)}** sets of body sensors: {names}. Pick yours "
            f"with `/ha_link entity:<name>`."
        )
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="ha_link",
    description="Link your Home Assistant body sensors so weigh-ins sync automatically.",
)
@app_commands.describe(
    entity="Your weight sensor or its name prefix, e.g. sensor.joshua_s_weight or joshua_s.",
    member="(Admins) link on someone else's behalf.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_link_cmd(
    interaction: discord.Interaction,
    entity: str,
    member: discord.Member | None = None,
) -> None:
    if not _ha_enabled():
        await interaction.response.send_message(
            "Home Assistant integration isn't available (disabled, or the host "
            "is missing `requests`/`cryptography` or an encryption key).",
            ephemeral=True,
        )
        return
    target = member or interaction.user
    if target.id != interaction.user.id and interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied(
                "Admins only — linking someone else's scale writes to their "
                "weight history.",
                allowed="Link your own with `/ha_link entity:<your sensor>`.",
            ),
            ephemeral=True,
        )
        return
    gid = _ctx_guild_id(interaction)
    if not gid:
        await interaction.response.send_message(
            "I couldn't tell which server to link this to — DM me from a server "
            "we share, or set your default with `/server`.",
            ephemeral=True,
        )
        return

    server = db.ha_server_get(target.id)
    if server is None:
        await interaction.response.send_message(
            "Connect a Home Assistant first with `/setup_ha url:<address> "
            "token:<token>`."
            if target.id == interaction.user.id else
            f"{target.mention} hasn't connected a Home Assistant yet — only they "
            "can do that, with `/setup_ha`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    # The entities come from the TARGET member's own server, not a shared one.
    states = await _ha_states_or_error(interaction, _ha_cfg_for(server))
    if states is None:
        return

    found = _ha_resolve_target(entity, states)
    if found is None:
        grouped = ha_client.group_body_entities(states)
        known = ", ".join(f"`{p or '(no prefix)'}`" for p in sorted(grouped)[:8])
        await interaction.followup.send(
            f"❌ I couldn't find a weight sensor for `{_safe_label(entity)}`.\n"
            + (f"People I can see on your Home Assistant: {known}.\n"
               if known else
               "I couldn't see *any* body-composition sensors — is the scale "
               "integration set up, and has someone stood on it at least once?\n")
            + "Run `/ha_entities` to list what's there.",
            ephemeral=True,
        )
        return

    # Refuse a prefix somebody else already claimed. Without this the admin gate
    # above protects nothing: a member reads another person's prefix off
    # /ha_entities, links themselves to it, and that person's weigh-ins import
    # and announce under the wrong name — publicly misattributing their weigh-ins
    # and corrupting both people's weight history. Admins may reassign, since
    # that is how a genuine mix-up gets fixed.
    owner = db.ha_prefix_owner(found["prefix"])
    if (owner is not None and owner != target.id
            and interaction.user.id not in ADMIN_USER_IDS):
        await interaction.followup.send(
            f"❌ `{found['prefix'] or '(no prefix)'}` is already linked to "
            "another member. If that's actually your scale, ask an admin to move "
            "it — or pick the right one from `/ha_entities`.",
            ephemeral=True,
        )
        return

    db.ha_link(
        target.id, gid, found["prefix"],
        weight_entity=found["weight_entity"],
        friendly_name=found["friendly_name"] or None,
    )
    if owner is not None and owner != target.id:
        # An admin reassignment. The previous owner's link has to go, or two rows
        # point at one scale and every weigh-in imports twice.
        db.ha_unlink(owner)
        LOG.info(
            "Home Assistant: prefix %s reassigned from user %s to %s by %s",
            found["prefix"], owner, target.id, interaction.user.id,
        )
    metric_count = len(found["metrics"])
    channel_id = _ha_alert_channel_id()
    where = (
        f" New weigh-ins post to <#{channel_id}>."
        if channel_id else
        " New weigh-ins are recorded silently (no announcement channel is set)."
    )
    who = "You're" if target.id == interaction.user.id else f"{target.mention} is"
    await interaction.followup.send(
        f"✅ {who} linked to `{found['prefix'] or '(no prefix)'}` — "
        f"{metric_count} sensor{'s' if metric_count != 1 else ''} found, "
        f"tracking `{found['weight_entity']}`.{where}\n"
        f"Weigh-ins sync about every {HA_POLL_MINUTES} min and log as "
        f"bodyweight, so TDEE, protein targets and the graphs all follow along. "
        + (
            "The public graph uses the member's **global bodyweight history**, "
            "including manual readings from DMs or other shared servers. "
            if channel_id else ""
        )
        + "`/ha_unlink` stops syncing.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="ha_entities",
    description="List the body-composition sensors the bot can see on Home Assistant.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_entities_cmd(interaction: discord.Interaction) -> None:
    if not _ha_enabled():
        await interaction.response.send_message(
            "Home Assistant integration isn't available right now.",
            ephemeral=True,
        )
        return
    server = db.ha_server_get(interaction.user.id)
    if server is None:
        await interaction.response.send_message(
            "Connect your Home Assistant first with `/setup_ha url:<address> "
            "token:<token>` — then this lists the body sensors it can see.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    states = await _ha_states_or_error(interaction, _ha_cfg_for(server))
    if states is None:
        return
    grouped = ha_client.group_body_entities(states)
    if not grouped:
        await interaction.followup.send(
            "I couldn't see any body-composition sensors on Home Assistant. "
            "Check the scale's integration is set up, and that someone has "
            "stood on it at least once — most scales create their entities "
            "only after the first reading.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Home Assistant · body sensors",
        description=(
            "Link yours with `/ha_link entity:<name>` — any distinctive word "
            "works, you don't have to type the whole thing."
        ),
        colour=ui.HOME_ASSISTANT,
    )
    # Who already owns which prefix. A Home Assistant server is a household, so it
    # routinely carries sensors for people who are not in this Discord at all and
    # have no say in whether their weight shows up here. So only the caller's own
    # bucket shows a weight. Every other bucket shows *when* it last read, which
    # is what you actually identify yours by — you just stood on it.
    owners = {
        str(r["entity_prefix"] or ""): int(r["user_id"])
        for r in db.list_ha_accounts()
    }
    # Buckets nothing is writing to are collected separately. Giving a phantom
    # Apple Health group the same full-size field as a working scale is what makes
    # the list confusing and the wrong choice tempting; one grey line at the
    # bottom says it is there without competing for attention.
    dead: list[str] = []
    shown = 0
    for prefix in sorted(grouped):
        mine = grouped[prefix]
        owner = owners.get(prefix)
        reading = ha_client.build_reading(mine)
        someone_else = owner is not None and owner != interaction.user.id
        if reading is None and not someone_else:
            dead.append(prefix or "(no prefix)")
            continue
        if shown >= 20:
            continue
        shown += 1
        bits = [f"{len(mine)} sensor{'s' if len(mine) != 1 else ''}"]
        if someone_else:
            bits.append("🔗 linked to another member")
        else:
            if owner is not None:
                bits.append("🔗 **yours**")
                bits.append(f"**{reading['weight_kg']:.2f} kg**")
            when = reading.get("measured_at") if reading else None
            if isinstance(when, datetime):
                bits.append(f"last read <t:{int(when.timestamp())}:R>")
        # The prefix is the thing to type but it is often machine-generated
        # (`renpho_scale_aa_bb_cc_dd_ee_ff_joshua_s`), so lead with the friendly
        # name and show the prefix as the code to copy.
        friendly = str(
            (mine[ha_client.WEIGHT_KEY].get("attributes") or {}).get(
                "friendly_name") or ""
        ) if ha_client.WEIGHT_KEY in mine else ""
        embed.add_field(
            name=_plain_label(friendly or prefix or "(no prefix)", limit=100),
            value=f"`{prefix or '(no prefix)'}`\n" + " · ".join(bits),
            inline=False,
        )
    listed = ", ".join(f"`{p}`" for p in dead[:6])
    more = f" …and {len(dead) - 6} more" if len(dead) > 6 else ""
    hint = (
        "A phone or fitness bridge usually creates these and never fills them "
        "in. Hide them for good by adding a fragment like `_iphone` to "
        "**Settings → Home Assistant → Ignore entities containing**."
    )
    if shown == 0:
        # Everything is dead. A list of corpses is not the useful answer here;
        # what to do about it is.
        await interaction.followup.send(
            "I can see body sensors, but none of them have a reading: "
            f"{listed}{more}\nStand on the scale once and try again — most only "
            f"publish a value after their first measurement.\n{hint}",
            ephemeral=True,
        )
        return
    if dead:
        embed.add_field(
            name="Nothing writing to these",
            value=f"{listed}{more}\n{hint}",
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="ha_unlink",
    description="Stop syncing weigh-ins from Home Assistant.",
)
@app_commands.describe(member="(Admins) unlink someone else.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_unlink_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    target = member or interaction.user
    if target.id != interaction.user.id and interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(
            embed=ui.denied("Admins only — this changes someone else's link."),
            ephemeral=True,
        )
        return
    # Both halves go: the entity link and the stored credential. Leaving a token
    # encrypted in the database after someone asked to disconnect would be the
    # wrong default, even though it is unreadable without the key.
    removed = db.ha_unlink(target.id)
    forgot = db.ha_server_forget(target.id)
    await interaction.response.send_message(
        "🗑️ Home Assistant disconnected — your stored access token was deleted "
        "and no more weigh-ins will sync. Your recorded weight history is kept, "
        "and so is the record of which weigh-ins were already imported, so "
        "reconnecting later won't import them twice."
        if (removed or forgot) else "No Home Assistant connection to remove.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="ha_status",
    description="Show your Home Assistant link and the last weigh-in that synced.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_status_cmd(interaction: discord.Interaction) -> None:
    server = db.ha_server_get(interaction.user.id)
    row = db.ha_get(interaction.user.id)
    if server is None and row is None:
        await interaction.response.send_message(
            "No Home Assistant connected. Set one up with `/setup_ha "
            "url:<address> token:<token>` — see `/ha_help`.",
            ephemeral=True,
        )
        return
    channel_id = _ha_alert_channel_id()
    lines: list[str] = []
    if server is None:
        # Possible after an upgrade: the link predates per-member credentials.
        lines.append(
            "⚠️ No Home Assistant connected — run `/setup_ha` to reconnect. "
            "Your entity link and weight history are still here."
        )
    else:
        version = f" (HA {server['ha_version']})" if server["ha_version"] else ""
        lines.append(f"🏠 Connected to `{server['base_url']}`{version}")
    if row is None:
        lines.append(
            "No sensors linked yet — `/ha_entities` to see what's there, then "
            "`/ha_link`."
        )
    else:
        lines.append(
            f"✅ Linked to `{row['entity_prefix'] or '(no prefix)'}`"
            + (f" via `{row['weight_entity']}`" if row["weight_entity"] else "")
        )
        lines.append(f"Last checked: {_ha_when(row['last_synced_at'])}")
        lines.append(
            "Announcements: "
            + (f"**on** → <#{channel_id}>" if channel_id
               else "**on**, but no announcement channel is configured")
        )
    latest = db.get_latest_bodyweight(0, interaction.user.id)
    if latest is not None:
        lines.append(
            f"Latest weight on file: **{float(latest['weight_kg']):.2f} kg** "
            f"({_ha_when(latest['recorded_at'])})"
        )
    if not _ha_enabled():
        lines.append(
            "⚠️ The integration is currently unavailable, so nothing is syncing."
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="ha_body",
    description="Show the latest body-composition numbers from your smart scale.",
)
@app_commands.describe(member="Whose numbers to show. Defaults to you.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_body_cmd(
    interaction: discord.Interaction,
    member: discord.Member | discord.User | None = None,
) -> None:
    target = member or interaction.user
    # The privacy check can require a live guild-member fetch in DMs and every
    # database read may briefly wait on the shared SQLite writer. This command
    # is always private, so defer before either operation.
    await interaction.response.defer(thinking=True, ephemeral=True)
    if await _deny_invisible_target(interaction, target):
        return
    # Reads what was stored, not Home Assistant, so this keeps working while the
    # server is down or the member has since unlinked.
    snapshot = db.latest_body_metric_snapshot(target.id)
    latest = db.get_latest_bodyweight(0, target.id)
    if snapshot is None and latest is None:
        msg = (
            "No body measurements on file yet. Link a smart scale with "
            "`/ha_link`, or log a weight with `/bodyweight`."
            if target.id == interaction.user.id else
            f"No body measurements on file for {target.mention}."
        )
        await interaction.followup.send(
            msg, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    embed = discord.Embed(
        title=f"Body composition · {_plain_label(_display_name(target))}",
        colour=ui.HOME_ASSISTANT,
    )
    # A later hand-entered bodyweight must not be combined with an older set of
    # scale metrics and presented as one reading.  When composition exists, the
    # snapshot contains the weight measured at that exact timestamp.
    shown_weight = (
        float(snapshot["weight_kg"])
        if snapshot is not None
        else float(latest["weight_kg"])
    )
    embed.description = f"⚖️ **{shown_weight:.2f} kg**"
    metrics = snapshot["metrics"] if snapshot is not None else {}
    lines: list[str] = []
    for metric in ha_client.METRICS:
        row = metrics.get(metric.key)
        if row is None:
            continue
        shown = ha_client.format_metric(
            metric.key, float(row["value"]), str(row["unit"] or ""),
        )
        suffix = ""
        history = db.body_metric_history(target.id, metric.key, limit=180)
        summary = bodycomp_metric_summary(
            (r["recorded_at"], float(r["value"])) for r in history
        )
        if summary is not None and summary.samples >= 2:
            if round(summary.change, metric.precision) == 0:
                suffix = " · → no net change"
            else:
                arrow = "▲" if summary.change > 0 else "▼"
                delta = ha_client.format_metric(
                    metric.key, abs(summary.change),
                    str(row["unit"] or metric.unit),
                )
                suffix = (
                    f" · {arrow} {delta} since "
                    f"<t:{int(summary.first_at.timestamp())}:d>"
                )
        lines.append(
            f"{metric.emoji} **{metric.label}** · {shown}{suffix}"
        )
    if lines:
        embed.add_field(
            name="From your scale", value=_clip_field("\n".join(lines)),
            inline=False,
        )
    when = (
        snapshot["recorded_at"]
        if snapshot is not None else latest["recorded_at"]
    )
    measured = ha_client.parse_ha_time(when)
    if measured is not None:
        # The embed's own timestamp rather than text in the footer: Discord
        # localises it per reader, and it does NOT render <t:…> markup inside a
        # footer, so spelling the time out there would show UTC to everybody.
        embed.timestamp = measured
        embed.set_footer(
            text=(
                "Measured · smart-scale composition is an estimate · "
                "/ha_graph shows trends"
                if snapshot is not None else "Weight only · no scale composition"
            )
        )
    else:
        embed.set_footer(text="Weight only, no scale linked yet")
    await interaction.followup.send(
        embed=embed, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="ha_graph",
    description="Plot one smart-scale body-composition metric over time.",
)
@app_commands.describe(
    metric="The body-composition measurement to plot.",
    member="Whose measurements to plot. Defaults to you.",
)
@app_commands.choices(metric=[
    app_commands.Choice(name=f"{m.emoji} {m.label}", value=m.key)
    for m in ha_client.METRICS
    if m.key != ha_client.WEIGHT_KEY
])
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_graph_cmd(
    interaction: discord.Interaction,
    metric: str,
    member: discord.Member | discord.User | None = None,
) -> None:
    target = member or interaction.user
    spec = ha_client.METRICS_BY_KEY.get(metric)
    if spec is None or spec.key == ha_client.WEIGHT_KEY:
        await interaction.response.send_message(
            "Pick one of the body-composition metrics shown by Discord. "
            "For weight itself, use `/bodyweight_graph`.",
            ephemeral=True,
        )
        return
    # Everything below can wait on a database writer, a DM membership lookup,
    # or a cold charting import. Acknowledge before any of those operations so
    # Discord's interaction deadline cannot expire.
    await interaction.response.defer(thinking=True, ephemeral=True)
    if await _deny_invisible_target(interaction, target):
        return
    rows = db.body_metric_history(target.id, spec.key, limit=1000)
    if not rows:
        await interaction.followup.send(
            f"No {spec.label.lower()} history is stored for "
            f"{_plain_label(_display_name(target))} yet.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    points = bodycomp_daily_points(
        ((r["recorded_at"], float(r["value"])) for r in rows),
        DISPLAY_TZ,
    )
    if len(points) < 2:
        await interaction.followup.send(
            f"Need measurements on at least two different days to plot a "
            f"{spec.label.lower()} trend.",
            ephemeral=True,
        )
        return

    xs = [point.when for point in points]
    ys = [point.value for point in points]
    trend = trend_values(ys)
    delta = ys[-1] - ys[0]
    first_shown = ha_client.format_metric(spec.key, ys[0], spec.unit)
    latest_shown = ha_client.format_metric(spec.key, ys[-1], spec.unit)
    subtitle = (
        f"{_plural(len(rows), 'measurement')} · "
        f"{_plural(len(points), 'day')} · "
        f"{first_shown} → {latest_shown}"
    )
    # Load and render off the event loop. Matplotlib keeps process-global
    # backend/pyplot state, so the worker helper serialises this with automatic
    # bodyweight charts too.
    try:
        buf = await asyncio.to_thread(
            _render_trend_chart_threadsafe,
            xs, ys, trend,
            title=f"{_display_name(target)} — {spec.label}",
            subtitle=subtitle,
            trend_label="3-day trend",
            trend_colour=f"#{ui.HOME_ASSISTANT.value:06x}",
            unit=spec.unit or spec.label,
            fmt=lambda value: ha_client.format_metric(
                spec.key, value, spec.unit,
            ),
        )
    except ImportError:
        await interaction.followup.send(
            "Graphing isn't available — matplotlib isn't installed. "
            "Add it to `requirements.txt` and redeploy.",
            ephemeral=True,
        )
        return
    safe_member = re.sub(
        r"[^a-z0-9_-]+", "_", _display_name(target).lower(),
    ).strip("_") or "member"
    filename = f"ha_{spec.key}_{safe_member}.png"
    file = discord.File(buf, filename=filename)
    arrow = "→" if round(delta, spec.precision) == 0 else (
        "▲" if delta > 0 else "▼"
    )
    change = ha_client.format_metric(spec.key, abs(delta), spec.unit)
    embed = discord.Embed(
        title=f"{spec.emoji} {spec.label} trend · "
              f"{_plain_label(_display_name(target))}",
        description=(
            f"Latest **{latest_shown}** · {arrow} {change} across "
            f"{_plural(len(points), 'day')}"
        ),
        colour=ui.HOME_ASSISTANT,
    )
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(
        text=(
            "Consumer smart-scale estimate · hydration and timing can move "
            "individual readings"
        )
    )
    await interaction.followup.send(
        embed=embed,
        file=file,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="ha_help",
    description="How the Home Assistant smart-scale sync works.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ha_help_cmd(interaction: discord.Interaction) -> None:
    channel_id = _ha_alert_channel_id()
    embed = discord.Embed(
        title="Home Assistant · smart-scale sync",
        description=(
            "If your scale is in [Home Assistant](https://www.home-assistant.io/)"
            ", the bot can read your weigh-ins straight out of it — no typing "
            "numbers into chat."
        ),
        colour=ui.HOME_ASSISTANT,
    )
    embed.add_field(
        name="What happens",
        value=(
            f"Every {HA_POLL_MINUTES} min the bot checks your sensors. A new "
            "weigh-in is logged as your **bodyweight**, so it feeds TDEE, your "
            "protein target, bodyweight goals, `/bodyweight_graph` and the "
            "leaderboard's true-load lines"
            + (f", and gets posted to <#{channel_id}>." if channel_id
               else " (no announcement channel is configured).")
            + "\nEverything else your scale measures — body fat, muscle mass, "
              "BMI, BMR, water — is kept too; see `/ha_body` for one coherent "
              "reading and `/ha_graph` for a trend."
        ),
        inline=False,
    )
    embed.add_field(
        name="Setting it up",
        value=(
            "**1.** In Home Assistant, click your name (bottom left) → "
            "**Security** → *Long-lived access tokens* → **Create token**. Copy "
            "it — Home Assistant only shows it once.\n"
            "**2.** Run `/setup_ha url:<your address> token:<the token>`. Best "
            "done in a **DM with me** so the token isn't typed into a channel; "
            "the reply is always ephemeral either way.\n"
            "If your scale is the only thing there with body sensors, that's it "
            "— I link it for you. Otherwise pick yours with `/ha_link`."
        ),
        inline=False,
    )
    embed.add_field(
        name="Your server, your token",
        value=(
            "Everyone connects their **own** Home Assistant, so people on "
            "different servers can all use this. Your token is stored "
            "**encrypted** and nobody else — including admins — can read it. "
            "`/ha_unlink` deletes it."
        ),
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/setup_ha` — connect your Home Assistant\n"
            "`/ha_entities` — list the body sensors it can see\n"
            "`/ha_link` — pick which sensors are yours\n"
            "`/ha_body` — your latest body-composition numbers\n"
            "`/ha_graph` — chart one body-composition trend\n"
            "`/ha_status` — connection + link + last check\n"
            "`/ha_unlink` — disconnect and delete your token\n"
            "`/ha_help` — this message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Privacy",
        value=(
            "When an announcement channel is configured, weigh-ins and a "
            "refreshed graph are public there. The graph is the member's "
            "**global bodyweight history**, including manual readings logged "
            "in DMs or other shared servers. Link only sensors you're fine "
            "broadcasting. `/ha_unlink` stops syncing and announcing."
        ),
        inline=False,
    )
    embed.add_field(
        name="Got a bad reading?",
        value=(
            "React ❌ on the announcement and that weigh-in is removed, along "
            "with the body-composition numbers measured with it. It won't be "
            "re-imported. Works on old announcements too, and on a first-link "
            "summary it undoes the whole batch."
        ),
        inline=False,
    )
    if not _ha_enabled():
        embed.add_field(
            name="⚠️ Currently unavailable",
            value=(
                "The host is missing a dependency or an encryption key, so "
                "connections can't be stored right now."
            ),
            inline=False,
        )
    embed.set_footer(text="Read-only · the bot never changes anything in your home")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="strava_latest",
    description="Show the most recent Strava activity (yours, or another member's).",
)
@app_commands.describe(member="Whose latest workout to show. Defaults to you.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_latest_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if STRAVA_DISABLED:
        await interaction.response.send_message(
            "Strava integration is disabled.", ephemeral=True,
        )
        return
    target = member or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    row = db.get_strava_account(target.id)
    if row is None:
        if target.id == interaction.user.id:
            msg = "You haven't linked Strava yet. Use `/strava_link`."
        else:
            msg = f"{target.mention} hasn't linked a Strava account."
        await interaction.response.send_message(
            msg, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await interaction.response.defer(thinking=True)

    def _fetch() -> "strava_client.StravaActivity | None | str":
        try:
            token = _strava_access_token(row)
            summary = strava_client.get_latest_activity(token)
            if summary is None:
                return None
            # Upgrade to the detailed activity for parity with the live feed
            # (calories, gear, caption, full-res route). Fall back to the
            # summary if that extra call fails.
            try:
                return strava_client.get_activity(token, summary.id)
            except Exception:  # pragma: no cover - network
                return summary
        except strava_client.StravaAuthError as exc:
            return f"auth: {exc}"
        except Exception as exc:  # pragma: no cover - network
            return f"error: {exc}"

    result = await bot.loop.run_in_executor(None, _fetch)
    if isinstance(result, str):
        LOG.warning("Strava latest fetch failed for %s: %s", target.id, result)
        await interaction.followup.send(
            "Couldn't reach Strava just now — try again shortly.",
            ephemeral=True,
        )
        return
    if result is None:
        await interaction.followup.send(
            f"No Strava activities found for {target.display_name}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    # Don't reveal someone else's private activity (our scope normally filters
    # these out, but guard defensively).
    if result.private and target.id != interaction.user.id:
        await interaction.followup.send(
            f"{target.display_name}'s most recent activity is private.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    embed, file = await asyncio.to_thread(
        _strava_embed_and_file,
        result,
        target.mention,
    )
    kwargs: dict[str, object] = {
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if file is not None:
        kwargs["file"] = file
    await interaction.followup.send(**kwargs)


@bot.tree.command(
    name="strava_backfill",
    description="Post Strava activities missed while the integration was offline.",
)
@app_commands.describe(
    days="How far back to check (default 30, maximum 365).",
    limit="Maximum feed posts this run (default 25, maximum 100).",
    member="Whose missed activities to recover. Defaults to you.",
    all_linked="Owner only: recover missed activities for every linked member.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_backfill_cmd(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 1, 365] = 30,
    limit: app_commands.Range[int, 1, 100] = 25,
    member: discord.Member | None = None,
    all_linked: bool = False,
) -> None:
    """Recover feed posts after an API/subscription outage.

    Activities are posted oldest-first and the durable per-activity ledger
    makes repeated runs safe. On a partial failure we stop advancing that
    athlete, so the next run resumes at the failed item instead of jumping it.
    """
    if not _strava_enabled():
        await interaction.response.send_message(
            "Strava isn't available right now. If the API app was inactive, "
            "restore its subscription/configuration first.",
            ephemeral=True,
        )
        return
    if STRAVA_FEED_CHANNEL_ID is None:
        await interaction.response.send_message(
            "No Strava feed channel is configured, so there is nowhere to "
            "post recovered activities.",
            ephemeral=True,
        )
        return
    if member is not None and all_linked:
        await interaction.response.send_message(
            "Choose either one member or `all_linked`, not both.",
            ephemeral=True,
        )
        return

    owner = _is_owner(interaction.user.id)
    if all_linked and not owner:
        await interaction.response.send_message(
            embed=ui.denied("Owner only — recovering every linked member."),
            ephemeral=True,
        )
        return
    target = member or interaction.user
    if member is not None and target.id != interaction.user.id and not owner:
        await interaction.response.send_message(
            embed=ui.denied("You can backfill your own Strava activities."),
            ephemeral=True,
        )
        return

    if all_linked:
        accounts = db.list_strava_accounts()
    else:
        row = db.get_strava_account(target.id)
        accounts = [row] if row is not None else []
    if not accounts:
        await interaction.response.send_message(
            (
                "No linked Strava accounts found."
                if all_linked
                else "That member hasn't linked Strava. Use `/strava_link` first."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    if await _strava_feed_channel() is None:
        await interaction.followup.send(
            "I can't access the configured Strava feed channel. Fix the "
            "channel/permissions and run this again.",
            ephemeral=True,
        )
        return

    after_epoch = int(
        (datetime.now(timezone.utc) - timedelta(days=int(days))).timestamp()
    )
    jobs: list[
        tuple[
            int,
            sqlite3.Row,
            strava_client.StravaActivity,
        ]
    ] = []
    fetch_errors: list[int] = []
    for account in accounts:
        summaries = await _strava_fetch_backfill_summaries(account, after_epoch)
        if isinstance(summaries, str):
            LOG.warning(
                "Strava backfill list failed for user %s: %s",
                account["user_id"], summaries,
            )
            fetch_errors.append(int(account["user_id"]))
            continue
        last_id = (
            int(account["last_activity_id"])
            if account["last_activity_id"] is not None
            else None
        )
        for summary in _strava_backfill_candidates(summaries, last_id):
            jobs.append((
                strava_client.start_unix(summary) or 0,
                account,
                summary,
            ))

    # Advancing oldest-first is important: if the command hits its limit, the
    # saved cursor leaves the remaining newer activities ready for the next run.
    jobs.sort(key=lambda item: (item[0], item[2].id))
    selected = jobs[:int(limit)]
    posted = already_done = hidden = failed = 0
    deferred_after_failure = 0
    blocked_users: set[int] = set()
    for _started, account, summary in selected:
        user_id = int(account["user_id"])
        if user_id in blocked_users:
            deferred_after_failure += 1
            continue
        detailed = await _strava_fetch_activity(account, summary.id)
        if isinstance(detailed, str):
            failed += 1
            blocked_users.add(user_id)
            LOG.warning(
                "Strava backfill activity fetch failed (user=%s activity=%s): %s",
                user_id, summary.id, detailed,
            )
            continue
        status = await _strava_announce_activity(
            account,
            detailed,
            source="backfill",
        )
        if status == "posted":
            posted += 1
        elif status == "duplicate":
            # A completed ledger row can be ahead of the legacy cursor if the
            # process stopped immediately after recording the post.
            db.update_strava_last_activity(user_id, summary.id)
            already_done += 1
        elif status in {"private", "filtered"}:
            hidden += 1
        else:
            failed += 1
            blocked_users.add(user_id)

    remaining = max(0, len(jobs) - len(selected)) + deferred_after_failure
    lines = [
        (
            f"✅ Strava backfill checked **{len(accounts)}** linked account"
            f"{'' if len(accounts) == 1 else 's'} over the last "
            f"**{int(days)} days**."
        ),
        f"• Posted: **{posted}**",
        f"• Already handled: **{already_done}**",
        f"• Private/filtered: **{hidden}**",
    ]
    if fetch_errors or failed:
        lines.append(
            f"• Could not finish: **{len(fetch_errors) + failed}** "
            "(later activities were left queued for a safe retry)"
        )
    if remaining:
        lines.append(
            f"• Still queued: **{remaining}** — run `/strava_backfill` again "
            "to continue."
        )
    elif not jobs and not fetch_errors:
        lines.append("Everything in that window was already up to date.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---- owner-only webhook subscription management ---------------------------

@bot.tree.command(
    name="strava_subscribe",
    description="(Owner) Create the Strava webhook push subscription.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_subscribe_cmd(interaction: discord.Interaction) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            embed=ui.denied("Owner only — this manages the bot itself."),
            ephemeral=True,
        )
        return
    cfg = _strava_cfg()
    if not cfg.configured or not cfg.webhook_callback_url:
        await interaction.response.send_message(
            "Strava isn't configured yet — an admin needs to fill in the Strava section of the dashboard Settings tab.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    # Reuse the idempotent ensure path so the cached subscription id stays in
    # sync and a stale-callback subscription is recreated correctly.
    result = await _strava_ensure_subscription()
    if result.startswith("created"):
        msg = (
            f"✅ Created subscription `{result.split(':', 1)[1]}` → "
            f"`{cfg.webhook_callback_url}`."
        )
    elif result.startswith("exists"):
        msg = f"ℹ️ Subscription already active: `{result.split(':', 1)[1]}`."
    elif result == "unconfigured":
        msg = "Strava isn't configured yet — an admin needs to fill in the Strava section of the dashboard Settings tab."
    elif result.startswith("autherror:"):
        # Permanent rejection (app Inactive/Forbidden) — point the owner at the
        # real fix rather than showing a generic "failed". The message is
        # Strava's URL-free body, so it can't leak the client_secret.
        msg = (
            f"⚠️ Strava rejected the request: {result.split(':', 1)[1]}\n"
            "The API app is likely **Inactive/Forbidden** — Standard Tier now "
            "requires a Strava subscription for API access. Check "
            "<https://www.strava.com/settings/api>."
        )
    else:
        msg = f"⚠️ Failed: {result}"
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(
    name="strava_subscription",
    description="(Owner) Strava health: subscription, linked athletes, feed channel.",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_subscription_cmd(interaction: discord.Interaction) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            embed=ui.denied("Owner only — this manages the bot itself."),
            ephemeral=True,
        )
        return
    cfg = _strava_cfg()
    if not cfg.configured:
        await interaction.response.send_message(
            "Strava isn't configured.", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    def _do() -> str:
        lines = ["**🏃 Strava health**"]
        try:
            subs = strava_client.view_subscriptions(cfg)
        except Exception as exc:  # pragma: no cover - network
            subs = None
            # Scrub in case a transient requests error carries the secret-bearing
            # URL — this string is shown in Discord, so it must be clean too.
            lines.append(
                "• Subscription: ⚠️ check failed "
                f"({_strava_scrub_secret(str(exc), cfg.client_secret)})"
            )
        if subs is not None:
            if subs:
                for s in subs:
                    ok = (
                        "✅"
                        if s.get("callback_url") == cfg.webhook_callback_url
                        else "⚠️ callback mismatch"
                    )
                    lines.append(
                        f"• Subscription `{s.get('id')}` → "
                        f"`{s.get('callback_url')}` {ok}"
                    )
            else:
                lines.append("• Subscription: ❌ none — run `/strava_subscribe`")
        linked = len(db.list_strava_accounts())
        feed = (
            f"<#{STRAVA_FEED_CHANNEL_ID}>" if STRAVA_FEED_CHANNEL_ID else "*(unset)*"
        )
        lines.append(f"• Linked athletes: **{linked}**")
        lines.append(f"• Feed channel: {feed}")
        lines.append(f"• Callback: `{cfg.webhook_callback_url}`")
        return "\n".join(lines)

    result = await bot.loop.run_in_executor(None, _do)
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(
    name="strava_unsubscribe",
    description="(Owner) Delete the Strava webhook push subscription.",
)
@app_commands.describe(subscription_id="The subscription id to delete.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def strava_unsubscribe_cmd(
    interaction: discord.Interaction, subscription_id: int,
) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            embed=ui.denied("Owner only — this manages the bot itself."),
            ephemeral=True,
        )
        return
    cfg = _strava_cfg()
    if not cfg.configured:
        await interaction.response.send_message(
            "Strava isn't configured.", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True, ephemeral=True)

    def _do() -> str:
        try:
            strava_client.delete_subscription(cfg, subscription_id)
            return f"🗑️ Deleted subscription `{subscription_id}`."
        except Exception as exc:  # pragma: no cover - network
            # Scrub: a transient requests error can embed the secret-bearing URL,
            # and this string is shown in Discord.
            return f"⚠️ Failed: {_strava_scrub_secret(str(exc), cfg.client_secret)}"

    result = await bot.loop.run_in_executor(None, _do)
    await interaction.followup.send(result, ephemeral=True)


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

    DnD is collapsed into "online" (still active, just busy).
    Invisible is collapsed into "offline".
    Idle is kept separate so we can see it in the logs.
    """
    raw = str(status.value)
    if raw == "dnd":
        return "online"
    if raw == "invisible":
        return "offline"
    return raw  # "online", "idle", or "offline"


def _get_all_activities_info(
    member: discord.Member,
) -> list[tuple[str, str | None, int | None]]:
    """The member's game/app activities as ordered ``(name, image_url, app_id)``.

    A user can run several at once — e.g. a game alongside a launcher, or two
    games — and we record every one so concurrent play shows up in the
    dashboard rather than only the first. Ignores Spotify and custom statuses;
    everything else with a name (games, rich-presence apps, streams, embedded
    voice activities) is fair game. Whitelisting specific classes is fragile —
    discord.py occasionally delivers rich-presence payloads as subclasses we
    didn't list, and they silently get dropped. ``image_url`` is the
    rich-presence large image when the activity exposes one (many plain
    "playing X" presences don't); ``app_id`` is the Discord application id when
    present, letting the dashboard fetch an icon for apps that ship no image and
    aren't in the games list (CurseForge, Crunchyroll, …). Names are
    de-duplicated, keeping the first occurrence (and its image/app id).
    """
    out: list[tuple[str, str | None, int | None]] = []
    seen: set[str] = set()
    for act in member.activities:
        if isinstance(act, (discord.Spotify, discord.CustomActivity)):
            continue
        name = getattr(act, "name", None) or getattr(act, "title", None)
        if not name or str(name) in seen:
            continue
        image: str | None = None
        try:
            image = getattr(act, "large_image_url", None)
        except Exception:  # pragma: no cover - asset URL build can throw
            image = None
        app_id = getattr(act, "application_id", None)
        seen.add(str(name))
        out.append((
            str(name),
            str(image) if image else None,
            int(app_id) if app_id else None,
        ))
    return out


def _get_main_activity_info(
    member: discord.Member,
) -> tuple[str | None, str | None]:
    """Return ``(name, image_url)`` for the member's primary game/app activity
    (the first of :func:`_get_all_activities_info`)."""
    acts = _get_all_activities_info(member)
    return (acts[0][0], acts[0][1]) if acts else (None, None)


def _get_main_activity(member: discord.Member) -> str | None:
    """Primary game/app name only (see :func:`_get_main_activity_info`)."""
    return _get_main_activity_info(member)[0]


def _seed_presence_snapshot(guild_id: int, member: discord.Member) -> None:
    """Persist the member's current status and primary activity.

    Presence updates are transition-based. Seeding a snapshot means /track can
    show a game/app that was already active when tracking started, or when the
    bot came back online after a restart.
    """
    db.presence_log_event(guild_id, member.id, _discord_status_to_str(member.status))
    db.activity_log_set(guild_id, member.id, _get_all_activities_info(member))


def _record_presence_transition(
    guild_id: int, before: discord.Member, after: discord.Member,
) -> tuple[list[str], list[str]]:
    """Persist one tracked Discord presence update.

    Discord may send rich-presence metadata after the activity name, or retain
    an activity in the offline payload. Comparing complete activity tuples lets
    the DB enrich metadata; forcing an empty set offline closes stale sessions.
    """
    before_status = _discord_status_to_str(before.status)
    after_status = _discord_status_to_str(after.status)
    if before_status != after_status:
        db.presence_log_event(guild_id, after.id, after_status)

    before_info = _get_all_activities_info(before)
    after_info = _get_all_activities_info(after)
    if not _presence_is_online(after_status):
        # Always reconcile storage. Discord's before payload can already be
        # empty while the last persisted snapshot is still a running game.
        db.activity_log_set(guild_id, after.id, [])
        after_info = []
    elif before_info != after_info:
        db.activity_log_set(guild_id, after.id, after_info)
    return [a[0] for a in before_info], [a[0] for a in after_info]


def _seed_tracked_presence_snapshots() -> None:
    """Refresh current status/activity for tracked users visible in cache."""
    for guild in bot.guilds:
        for row in db.presence_track_list(guild.id):
            member = guild.get_member(int(row["user_id"]))
            if member is None:
                continue
            try:
                _seed_presence_snapshot(guild.id, member)
            except Exception:
                LOG.exception(
                    "Failed to seed tracked presence snapshot for user %s in guild %s",
                    row["user_id"], guild.id,
                )


@bot.event
async def on_presence_update(
    before: discord.Member, after: discord.Member,
) -> None:  # pragma: no cover - discord runtime path
    """Record status and activity transitions for tracked users only."""
    if not ENABLE_PRESENCE_TRACKING:
        return
    try:
        if after.guild is None:
            return
        if not db.presence_is_tracked(after.guild.id, after.id):
            return
        before_acts, after_acts = _record_presence_transition(
            after.guild.id, before, after,
        )
        if before_acts != after_acts:
            LOG.info(
                "Activity change for %s in %s: %r -> %r (raw=%s)",
                after.id, after.guild.id, before_acts, after_acts,
                [type(a).__name__ + ":" + str(getattr(a, "name", "?"))
                 for a in after.activities],
            )
    except Exception:
        LOG.exception("Failed to record presence update")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:  # pragma: no cover - discord runtime path
    """Log voice-channel join / leave / move transitions plus mute/deafen ones.

    Channel changes become 'join'/'leave'/'move' rows. Mute and deafen toggles
    (which also fire this event, usually with no channel change) become
    'mute_on'/'mute_off'/'deaf_on'/'deaf_off' rows — but only while the member
    is in a channel, so app.voicetime can turn them into muted/deafened time.

    We define muted = self_mute OR server mute and deafened = self_deaf OR
    server deaf, recording the two raw signals independently (Discord auto-mutes
    on deafen, so they usually move together, but the stats keep them apart).
    A leave needs no synthetic mute_off: voicetime only counts muted/deafened
    time while in a call, so leaving is already a hard boundary.
    """
    if not ENABLE_VOICE_TRACKING:
        return
    try:
        if member.guild is None:
            return
        bc, ac = before.channel, after.channel
        bid = bc.id if bc else None
        aid = ac.id if ac else None

        def _log(event: str, ch) -> None:
            db.voice_log_event(
                member.guild.id, member.id, event,
                channel_id=ch.id if ch else None,
                channel_name=ch.name if ch else None,
            )

        b_muted = bool(before.self_mute or before.mute)
        a_muted = bool(after.self_mute or after.mute)
        b_deaf = bool(before.self_deaf or before.deaf)
        a_deaf = bool(after.self_deaf or after.deaf)

        if bid != aid:
            # Channel presence changed.
            if bc is None:
                _log("join", ac)
                # A fresh join can't trust `before` (it's the not-in-voice
                # state), so seed the current mute/deafen so an already-muted
                # join starts the clock at the join instant.
                if a_muted:
                    _log("mute_on", ac)
                if a_deaf:
                    _log("deaf_on", ac)
            elif ac is None:
                _log("leave", bc)
                # No mute_off needed on leave (see docstring).
            else:
                _log("move", ac)
                # Mute/deafen carries across a move; only log a real change.
                if a_muted != b_muted:
                    _log("mute_on" if a_muted else "mute_off", ac)
                if a_deaf != b_deaf:
                    _log("deaf_on" if a_deaf else "deaf_off", ac)
            return

        # Same channel: a mute/deafen/stream toggle. Only track while in voice.
        if ac is None:
            return
        if a_muted != b_muted:
            _log("mute_on" if a_muted else "mute_off", ac)
        if a_deaf != b_deaf:
            _log("deaf_on" if a_deaf else "deaf_off", ac)
    except Exception:
        LOG.exception("Failed to record voice state update")


def _seed_voice_state_snapshots() -> None:  # pragma: no cover - discord runtime
    """Reconcile logged voice state against reality for anyone in a call now.

    Voice tracking is transition-based, so a restart can leave the log stale: a
    member may have joined, muted, or unmuted while the bot was down. On_ready
    we walk every occupied voice channel and, for each member, replay their
    recent events (app.voicetime, no live flags = the log's own view) and
    compare it to what Discord reports live. We write only the corrective
    transitions needed to make them match — a 'join' when the log lost the
    session entirely, and a mute/deafen toggle when the raw signal drifted.
    Mirrors _seed_tracked_presence_snapshots. No-op when nothing diverged.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)  # carry-in reaches further back than this
    for guild in bot.guilds:
        for ch in guild.voice_channels:
            for m in ch.members:
                try:
                    rows = db.voice_events_for(guild.id, m.id, since=since, until=now)
                    summary = summarize_voice(
                        [(r["event"], r["at"]) for r in rows], since, now,
                    )
                    live_muted = bool(m.voice and (m.voice.self_mute or m.voice.mute))
                    live_deaf = bool(m.voice and (m.voice.self_deaf or m.voice.deaf))
                    log_muted = summary.muted_now
                    log_deaf = summary.deafened_now

                    def _log(event: str) -> None:
                        db.voice_log_event(
                            guild.id, m.id, event,
                            channel_id=ch.id, channel_name=ch.name,
                        )

                    if not summary.in_call_now:
                        # Log lost the session (joined during downtime / purged
                        # history). Re-anchor; a join resets mute/deafen state.
                        _log("join")
                        log_muted = log_deaf = False
                    if live_muted != log_muted:
                        _log("mute_on" if live_muted else "mute_off")
                    if live_deaf != log_deaf:
                        _log("deaf_on" if live_deaf else "deaf_off")
                except Exception:
                    LOG.exception(
                        "Failed to seed voice snapshot for user %s in guild %s",
                        m.id, guild.id,
                    )


track_group = app_commands.Group(
    name="track",
    description="Track and view a user's online/offline schedule.",
)


def _presence_disabled_embed() -> discord.Embed:
    """Presence tracking gate. The env var and portal toggles are an admin's
    problem, so they live in the admin field rather than in prose aimed at a
    member who cannot act on either."""
    return ui.unavailable(
        "Presence tracking",
        why="Nobody's online/offline history is being recorded.",
        admin_fix="Set `ENABLE_PRESENCE_TRACKING=true`, enable the Presence "
                  "and Server Members intents in the Discord Developer "
                  "Portal, then restart the bot.",
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
            embed=_presence_disabled_embed(), ephemeral=True,
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
    guild_id = _ctx_guild_id(interaction)
    inserted = db.presence_track_add(guild_id, user.id, interaction.user.id)
    # Seed status + activity so /track has something useful immediately,
    # even if the user was already playing a game before tracking started.
    # Re-fetch from the guild cache: slash-command Member args come from
    # interaction.resolved which lacks presence data, so user.status would
    # always be offline and user.activities always empty here.
    _g = _ctx_guild(interaction)
    cached = _g.get_member(user.id) if _g is not None else None
    try:
        _seed_presence_snapshot(guild_id, cached or user)
    except Exception:
        LOG.exception("Failed to seed initial presence snapshot")
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
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can stop presence tracking.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
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
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can view the tracking list.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
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


def _status_dot(status: str) -> str:
    """Return a coloured dot emoji for a given status string."""
    if status == "idle":
        return "🟡"
    if _presence_is_online(status):
        return "🟢"
    return "⚫"


def _render_schedule_embed(
    user: discord.abc.User,
    summary: PresenceSummary,
    days: int,
    activity_totals: dict[str, float] | None = None,
) -> discord.Embed:
    """Build the /track schedule embed from a PresenceSummary."""
    expected_seconds = max(1.0, days * 86400.0)
    coverage = min(1.0, summary.observed_seconds / expected_seconds)
    recorded = format_duration(summary.observed_seconds)
    coverage_note = (
        f"Recorded **{recorded}** of that window "
        f"(**{coverage * 100:.0f}% coverage**)."
    )
    if coverage < 0.75:
        coverage_note += " Treat the totals as partial."
    embed = discord.Embed(
        title=f"📅 Presence schedule — {user.display_name}",
        colour=EMBED_COLOUR,
        description=(
            f"Last **{days}** day{'s' if days != 1 else ''} of recorded activity.\n"
            f"{coverage_note}"
        ),
    )
    online = format_duration(summary.online_seconds)
    offline = format_duration(summary.offline_seconds)
    embed.add_field(name="🟢 Online", value=online, inline=True)
    embed.add_field(name="⚫ Offline", value=offline, inline=True)
    if summary.final_status:
        dot = _status_dot(summary.final_status)
        since = (
            f"\nSince <t:{int(summary.final_status_at.timestamp())}:R>"
            if summary.final_status_at is not None else ""
        )
        embed.add_field(
            name="Now", value=f"{dot} {summary.final_status}{since}", inline=True,
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
    # Top activities
    if activity_totals:
        top = list(activity_totals.items())[:5]
        act_lines = [
            f"🎮 **{name}** — {format_duration(secs)}"
            for name, secs in top
        ]
        embed.add_field(
            name="Top activities", value="\n".join(act_lines), inline=False,
        )
    embed.set_footer(
        text=(
            f"{summary.transitions} status changes recorded"
            f" • {coverage * 100:.0f}% window coverage"
        )
    )
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
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if days < 1 or days > 90:
        await interaction.response.send_message(
            "`days` must be between 1 and 90.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
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
    act_sets = db.activity_sets_for(
        guild_id, user.id, since=window_start, until=now,
    )
    activity_totals = summarize_activity_sets(
        act_sets, window_start, now,
    ) if act_sets else {}
    embed = _render_schedule_embed(user, summary, days, activity_totals or None)
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
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if days < 1 or days > 90:
        await interaction.response.send_message(
            "`days` must be between 1 and 90.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    rows = db.presence_events_for(
        guild_id, user.id, since=window_start, until=now,
    )
    # Filter to only events strictly inside the window (not the carry-in),
    # then normalize only dnd/invisible (old rows) — keep idle separate.
    _STATUS_NORM = {"dnd": "online", "invisible": "offline"}
    normalized: list[dict] = []
    for r in rows:
        if r["at"] < window_start.isoformat():
            continue
        normed = dict(r, status=_STATUS_NORM.get(r["status"], r["status"]))
        if normalized and normalized[-1]["status"] == normed["status"]:
            continue  # drop consecutive duplicate after normalization
        normalized.append(normed)

    # Discord's gateway is noisy: multi-client status merge, AFK timer
    # flapping, and brief reconnects produce sub-minute flips that don't
    # reflect real availability. Discord's own AFK threshold is 5 min, so
    # anything held <4 min is almost certainly a client artifact. Drop
    # those and re-merge surrounding same-status segments.
    _FLICKER_THRESHOLD_S = 240
    filtered: list[dict] = []
    for i, r in enumerate(normalized):
        is_last = i == len(normalized) - 1
        if not is_last:
            this_at = datetime.fromisoformat(r["at"])
            next_at = datetime.fromisoformat(normalized[i + 1]["at"])
            if this_at.tzinfo is None:
                this_at = this_at.replace(tzinfo=timezone.utc)
            if next_at.tzinfo is None:
                next_at = next_at.replace(tzinfo=timezone.utc)
            if (next_at - this_at).total_seconds() < _FLICKER_THRESHOLD_S:
                continue
        if filtered and filtered[-1]["status"] == r["status"]:
            continue  # re-merge after dropping short flickers
        filtered.append(r)
    inner = filtered

    # Activity events within the window (strictly inside, no carry-in needed
    # for display).
    act_rows = db.activity_events_for(
        guild_id, user.id, since=window_start, until=now,
    )
    act_inner = [r for r in act_rows if r["at"] >= window_start.isoformat()]

    if not inner and not act_inner:
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

    # Merge presence + activity events into a single timeline, sorted by time.
    # Each entry: {"kind": "status"|"activity", "at": iso_str, ...fields}
    timeline: list[dict] = []
    for r in inner:
        timeline.append({"kind": "status", "status": r["status"], "at": r["at"]})
    for r in act_inner:
        timeline.append({"kind": "activity", "activity": r["activity"], "at": r["at"]})
    timeline.sort(key=lambda e: e["at"])

    # Take the most-recent _RAW_EVENT_LIMIT entries.
    shown = timeline[-_RAW_EVENT_LIMIT:]
    truncated = len(timeline) > _RAW_EVENT_LIMIT

    # Pre-build a flat list of just the status entries so we can compute
    # "for X" duration: each status is held until the next status change.
    status_only = [e for e in timeline if e["kind"] == "status"]

    lines: list[str] = []
    status_idx = 0  # cursor into status_only for duration lookup
    for entry in shown:
        ts_dt = datetime.fromisoformat(entry["at"])
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        unix = int(ts_dt.timestamp())

        if entry["kind"] == "status":
            status = entry["status"]
            dot = _status_dot(status)
            # Find where this entry sits in status_only to get next status ts.
            while status_idx < len(status_only) - 1 and status_only[status_idx]["at"] != entry["at"]:
                status_idx += 1
            is_last_status = status_idx >= len(status_only) - 1
            if is_last_status:
                dur_str = "  ·  **current**"
            else:
                next_ts = datetime.fromisoformat(status_only[status_idx + 1]["at"])
                if next_ts.tzinfo is None:
                    next_ts = next_ts.replace(tzinfo=timezone.utc)
                dur_str = f"  ·  for {format_duration((next_ts - ts_dt).total_seconds())}"
            lines.append(f"{dot} **{status}**  <t:{unix}:f>{dur_str}")
        else:
            activity = entry["activity"]
            if activity:
                lines.append(f"  🎮 *started* **{activity}**  <t:{unix}:f>")
            else:
                lines.append(f"  🎮 *stopped*  <t:{unix}:f>")

    header = (
        f"**{user.display_name}** — last **{days}** day{'s' if days != 1 else ''}"
        f" ({len(inner)} status · {len(act_inner)} activity)"
    )
    if truncated:
        header += f"\n*Showing most recent {_RAW_EVENT_LIMIT} entries only.*"

    embed = discord.Embed(
        title=f"📋 Raw presence log — {user.display_name}",
        description=header + "\n\n" + "\n".join(lines),
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


def _collect_sleep_export(
    guild_id: int, user_id: int, days: int,
) -> tuple[list[dict], list[dict], datetime, datetime]:
    """Pull presence events and derive nightly sleep sessions for a window.

    Returns ``(sessions, raw_events, window_start, window_end)`` where
    ``raw_events`` are the ``{status, at}`` rows inside the window (carry-in
    dropped) and ``sessions`` come from :func:`nightly_sleep_sessions`.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    rows = db.presence_events_for(
        guild_id, user_id, since=window_start, until=now,
    )
    events = [(r["status"], r["at"]) for r in rows]
    sessions = nightly_sleep_sessions(
        events, window_start, now, display_tz=DISPLAY_TZ,
    )
    raw_events = [
        {"status": r["status"], "at": r["at"]}
        for r in rows
        if r["at"] >= window_start.isoformat()
    ]
    return sessions, raw_events, window_start, now


@track_group.command(
    name="export",
    description="(Owner) DM yourself a user's recorded sleep/presence data.",
)
@app_commands.describe(
    user="The member whose sleep data to export.",
    days="How many days back to include (1-365, default 30).",
    fmt="File format: csv (nightly sleep table) or json (full raw dump).",
)
@app_commands.choices(fmt=[
    app_commands.Choice(name="CSV (nightly sleep table)", value="csv"),
    app_commands.Choice(name="JSON (full raw dump)", value="json"),
])
async def track_export_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    days: int = 30,
    fmt: app_commands.Choice[str] | None = None,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can export sleep data.", ephemeral=True,
        )
        return
    if days < 1 or days > 365:
        await interaction.response.send_message(
            "`days` must be between 1 and 365.", ephemeral=True,
        )
        return

    fmt_value = fmt.value if fmt is not None else "csv"
    await interaction.response.defer(thinking=True, ephemeral=True)

    guild_id = _ctx_guild_id(interaction)
    sessions, raw_events, window_start, window_end = _collect_sleep_export(
        guild_id, user.id, days,
    )
    if not raw_events:
        await interaction.followup.send(
            f"No presence data recorded for {user.mention} in the last "
            f"{days} day{'s' if days != 1 else ''} — an owner needs to "
            "`/track start` them first.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    if fmt_value == "json":
        payload = {
            "user_id": user.id,
            "display_name": user.display_name,
            "guild_id": guild_id,
            "timezone": str(DISPLAY_TZ),
            "days": days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "nightly_sessions": sessions,
            "raw_presence_events": raw_events,
        }
        buf = io.BytesIO(
            json.dumps(payload, indent=2).encode("utf-8")
        )
        suffix = "json"
    else:
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow(
            ["date", "sleep_start_local", "wake_local", "duration_hours"]
        )
        for s in sessions:
            writer.writerow(
                [s["date"], s["start_local"], s["end_local"], s["duration_hours"]]
            )
        buf = io.BytesIO(sio.getvalue().encode("utf-8"))
        suffix = "csv"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"sleep-{user.id}-{stamp}.{suffix}"
    try:
        dm = await interaction.user.create_dm()
        await dm.send(
            content=(
                f"Sleep export for **{user.display_name}** — last {days} "
                f"day{'s' if days != 1 else ''}, {len(sessions)} sleep "
                f"session{'s' if len(sessions) != 1 else ''} from "
                f"{len(raw_events)} status events ({DISPLAY_TZ})."
            ),
            file=discord.File(buf, filename=filename),
        )
        await interaction.followup.send(
            f"Sent `{filename}` to your DMs.", ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I can't DM you — open your DMs for server members and try again.",
            ephemeral=True,
        )


def _ai_error_embed(exc: gemini_client.GeminiError) -> discord.Embed:
    """A tidy, user-facing embed for an AI failure.

    Surfaces ``gemini_client.friendly_message`` (e.g. "model is swamped, try
    again") instead of dumping the raw API JSON at the user. Transient overloads
    get an amber card; genuine misconfiguration gets a red one so it's visually
    distinct from a "just retry" blip.
    """
    config_problem = (
        getattr(exc, "status_code", None) in (400, 401, 403, 404)
        or (getattr(exc, "status", None) or "").upper()
        in ("UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_ARGUMENT",
            "NOT_FOUND")
        or "configured" in gemini_client.friendly_message(exc)
    )
    embed = ui.card(
        f"{ui.AI} AI unavailable",
        description=gemini_client.friendly_message(exc),
        colour=ui.DANGER if config_problem else ui.WARNING,
        footer=(
            "This is usually temporary — give it a moment."
            if getattr(exc, "retryable", False) else None
        ),
    )
    # A misconfiguration is nobody's fault but the operator's, and only they can
    # fix it — so give them Google's own wording instead of making them go
    # trawling container logs for it.
    if config_problem:
        ui.block(
            embed, "For admins",
            f"`{_safe_label(str(exc), limit=300)}`\n"
            f"model `{gemini_client.model_name()}` · "
            f"check the Gemini settings in the dashboard",
        )
    return embed


_SLEEP_ANALYSIS_SYSTEM = (
    "You are a sleep-pattern analyst. You are given a person's sleep sessions "
    "derived from their Discord online/offline presence (a proxy, not a sleep "
    "tracker, so treat it as approximate). Identify concrete trends and quantify "
    "them with the actual numbers: typical bedtime and wake time, average and "
    "variability (consistency) of sleep duration, weekday-vs-weekend "
    "differences, and any drift or notable change across the window. Lead with "
    "the single most useful insight. Use short Discord-markdown bullets with "
    "**bold** labels; put the one-line caveat about it being a presence proxy at "
    "the very end. Keep the whole reply under 1500 characters for a Discord "
    "embed.\n\n" + _AI_GUARDRAILS
)


@track_group.command(
    name="analyze",
    description="Use Gemini to summarise trends in a user's sleep data.",
)
@app_commands.describe(
    user="The member whose sleep data to analyse.",
    days="How many days back to analyse (1-365, default 30).",
)
async def track_analyze_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    days: int = 30,
) -> None:
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if days < 1 or days > 365:
        await interaction.response.send_message(
            "`days` must be between 1 and 365.", ephemeral=True,
        )
        return
    if not gemini_client.available():
        await interaction.response.send_message(
            "Gemini isn't configured. Set `GEMINI_API_KEY` (and optionally "
            "`GEMINI_MODEL`) in the bot's environment and restart.",
            ephemeral=True,
        )
        return

    # Public like /track schedule and /track raw — the underlying presence
    # data is already visible via those. Errors below stay ephemeral so a
    # failed call doesn't clutter the channel.
    await interaction.response.defer(thinking=True)

    guild_id = _ctx_guild_id(interaction)
    sessions, raw_events, _, _ = _collect_sleep_export(guild_id, user.id, days)
    if not sessions:
        await interaction.followup.send(
            f"Not enough sleep data for {user.mention} in the last {days} "
            f"day{'s' if days != 1 else ''} to analyse "
            f"({len(raw_events)} status events recorded).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    prompt = (
        f"Timezone: {DISPLAY_TZ}. Window: last {days} days. "
        f"{len(sessions)} sleep sessions follow as JSON "
        "(times are local; duration_hours is the offline stretch):\n\n"
        + json.dumps(sessions, indent=2)
    )
    try:
        text = await asyncio.to_thread(
            gemini_client.generate, prompt, system=_SLEEP_ANALYSIS_SYSTEM,
            temperature=0.3,  # precise/consistent for trend analysis
            max_output_tokens=900,
        )
    except gemini_client.GeminiError as exc:
        LOG.warning("Gemini sleep analysis failed: %s", exc)
        await interaction.followup.send(
            embed=_ai_error_embed(exc), ephemeral=True,
        )
        return

    # Embed descriptions cap at 4096 chars; we asked for <1500 but guard anyway.
    description = text[:4000]
    embed = discord.Embed(
        title=f"💤 Sleep trends — {user.display_name}",
        description=description,
        colour=EMBED_COLOUR,
    )
    embed.set_footer(
        text=(
            f"{len(sessions)} sessions · last {days}d · "
            f"{gemini_client.model_name()}"
        )
    )
    await interaction.followup.send(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------------------------------------------------------------------
# AI progress coach — hands a person's *entire* tracked dataset to Gemini for a
# holistic strength + nutrition progress report. Reuses the same Gemini plumbing
# as /track analyze.
# ---------------------------------------------------------------------------

_COACH_SYSTEM = (
    "You are an experienced, encouraging strength, cardio & nutrition coach. "
    "You are given ONE person's raw training and nutrition data as JSON (weights in kg; "
    "'bw' true means the weight is added to bodyweight, e.g. weighted dips). "
    "Write a concise, personalised progress report with short sections: "
    "**Overview**, **What's going well**, **Where you're lagging / plateauing**, "
    "**Nutrition**, and **Next steps** (2-4 concrete, specific actions). "
    "Reference actual lifts and numbers from the data, call out plateaus, "
    "muscle-group imbalances, training frequency (avg_sessions_per_week), "
    "cardio consistency/difficulty, and goal progress. Cardio levels and speed "
    "are machine-specific settings (speed has no assumed unit), not comparable "
    "between machines. "
    "Use 'estimated_1rm_progression' to spot strength gains even "
    "when top-set weight is flat — e.g. more reps at the same load is real "
    "progress (cite the e1RM gain in kg). Be motivating but honest. Use short "
    "bullet points. Keep the whole reply under 1800 characters for a Discord "
    "embed.\n\n"
    "IMPORTANT — missing data vs real zeros: this bot only has what the user "
    "manually logs. A zero, null, empty, or stale value almost always means "
    "THEY DIDN'T LOG IT, not that the true value is zero. Specifically: if "
    "'calorie_tracking_active' or 'protein_tracking_active' is false, that macro "
    "is NOT being tracked — say so and skip judging it (never claim they ate 0 "
    "calories or are starving). If 'days_since_last_lift' or "
    "'days_since_bodyweight' is large, treat it as a possible logging gap and "
    "gently nudge them to log/track, rather than concluding they stopped "
    "training or lost progress. Frame gaps as 'no data logged recently — worth "
    "tracking again', never as a regression.\n\n"
    "Body-composition values come from a consumer bioimpedance smart scale. "
    "They are hydration-sensitive estimates, not diagnoses: describe only a "
    "sustained direction across several readings, call sparse/noisy data "
    "inconclusive, and never attach a medical judgement to BMI, body-fat, "
    "muscle, water, metabolic age, or similar values.\n\n"
    "Style: address them by name, lead each section with a bold emoji header "
    "(e.g. '**📊 Overview**'), keep bullets tight (one line each, the specific "
    "number first), and make 'Next steps' genuinely actionable (a weight to "
    "chase, a lift to add, a frequency to hit) — not platitudes. Prioritise the "
    "1-2 highest-impact observations over an exhaustive list.\n\n" + _AI_GUARDRAILS
    + "\n\nExample of the expected style and length (adapt to the real data, "
    "never copy these numbers):\n"
    "**📊 Overview**\n"
    "Solid 6 weeks, Sam — 17 sessions (2.8/wk) and your e1RM is climbing on the "
    "big lifts.\n"
    "**✅ Going well**\n"
    "• Bench e1RM 102→111kg (+9) — reps up at 90kg before the top set moved.\n"
    "• Squat PR 140kg, your most-trained lift (22 logs).\n"
    "**⚠️ Lagging**\n"
    "• Overhead press flat at 50kg for 5 weeks.\n"
    "• No pulling logged — rows/pull-ups missing vs all that pressing.\n"
    "**🍎 Nutrition**\n"
    "• Protein only logged 3/30 days — too sparse to judge; worth tracking.\n"
    "**🎯 Next steps**\n"
    "• Add a weekly row variation, target 60kg×8.\n"
    "• Push OHP with 3×5 @ 52.5kg next session.\n"
    "• Log protein daily for one week to get a real baseline."
)


def _e1rm_progression(rows: list) -> list[dict]:
    """Per-exercise estimated-1RM trend from rep-bearing sets.

    ``rows`` is the output of ``db.user_rep_sets`` (oldest-first, grouped by
    equipment). For each exercise we take the Epley 1RM of every qualifying set
    and report the first, best, and latest estimate plus the gain — so the coach
    can spot strength progress even when the top-set weight has been flat (e.g.
    100kg×5 → 100kg×8 is a real e1RM jump). Returns the biggest movers first.
    """
    by_equip: dict[str, list[tuple[float, str]]] = {}
    for r in rows:
        e1 = estimated_one_rep_max(float(r["weight_kg"]), int(r["reps"]))
        if e1 is None:
            continue
        by_equip.setdefault(r["equipment"], []).append((e1, r["logged_at"]))
    out: list[dict] = []
    for equip, vals in by_equip.items():
        if not vals:
            continue
        first_e, first_at = vals[0]
        latest_e, latest_at = vals[-1]
        best_e, best_at = max(vals, key=lambda v: v[0])
        out.append({
            "equipment": equip,
            "first_e1rm_kg": round(first_e, 1),
            "latest_e1rm_kg": round(latest_e, 1),
            "best_e1rm_kg": round(best_e, 1),
            "gain_kg": round(latest_e - first_e, 1),
            "sets_counted": len(vals),
            "best_at": best_at,
        })
    # Biggest estimated-strength movers first; cap to keep the payload tight.
    out.sort(key=lambda d: d["gain_kg"], reverse=True)
    return out[:10]


def _build_progress_payload(
    guild_id: int,
    user_id: int,
    name: str,
    days: int,
    *,
    include_body_composition: bool = False,
) -> dict:
    """Assemble a compact JSON-able snapshot of everything we track for one
    person: lifting summary/PRs/gains/goals, bodyweight trend, training
    frequency, cardio programs/sessions, and calorie/protein goals + totals."""
    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(days=days)).isoformat()
    end_iso = now.isoformat()

    def _days_since(iso: str | None) -> int | None:
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt.astimezone(timezone.utc)).days)

    summary = db.user_summary(guild_id, user_id)
    tonnage, _n = db.total_tonnage(guild_id, user_id)
    revo = db.get_revo_account(user_id)
    cal_goal = db.calorie_goal_get(guild_id, user_id)
    pro_goal = db.protein_goal_get(guild_id, user_id)
    cal_total, cal_entries = db.calorie_total_between(
        guild_id, user_id, start_iso, end_iso,
    )
    pro_total, pro_entries = db.protein_total_between(
        guild_id, user_id, start_iso, end_iso,
    )
    bw = db.bodyweight_history(guild_id, user_id, limit=60)
    dates = db.user_log_dates(guild_id, user_id)
    last_lift_at = summary["last_at"] if summary else None
    last_bw_at = bw[-1]["recorded_at"] if bw else None
    # Distinct local days with a nutrition entry in the window — lets the model
    # tell "0 because untracked" from "0 because genuinely fasted".
    cal_days = len(db.calorie_logged_days(guild_id, user_id, start_iso, end_iso))
    pro_days = len(db.protein_logged_days(guild_id, user_id, start_iso, end_iso))
    cardio_programs = db.cardio_program_list(user_id)
    cardio_sessions = db.cardio_session_list(
        user_id, limit=1000, start_iso=start_iso, end_iso=end_iso,
    )
    cardio_activity: dict[str, dict[str, float | str | int]] = {}
    cardio_total_minutes = 0.0
    cardio_difficulty = {key: 0 for key in _CARDIO_DIFFICULTY_LABELS}
    for session in cardio_sessions:
        cardio_difficulty.setdefault(str(session["difficulty"]), 0)
        cardio_difficulty[str(session["difficulty"])] += 1
        session_activities: set[str] = set()
        for segment in cardio.segments_from_rows(session["segments"]):
            cardio_total_minutes += segment.minutes
            key = segment.activity.casefold()
            aggregate = cardio_activity.setdefault(
                key,
                {"activity": segment.activity, "minutes": 0.0, "sessions": 0},
            )
            aggregate["minutes"] = float(aggregate["minutes"]) + segment.minutes
            if key not in session_activities:
                aggregate["sessions"] = int(aggregate["sessions"]) + 1
                session_activities.add(key)

    # Derived signals the model would otherwise have to (badly) infer.
    window_cutoff = (now - timedelta(days=days)).date().isoformat()
    sessions_in_window = sum(1 for d in dates if d >= window_cutoff)
    weeks = max(1.0, days / 7.0)
    avg_sessions_per_week = round(sessions_in_window / weeks, 1)
    bw_change_kg = (
        round(bw[-1]["weight_kg"] - bw[0]["weight_kg"], 1)
        if len(bw) >= 2 else None
    )

    return {
        "name": name,
        "window_days": days,
        "timezone": str(DISPLAY_TZ),
        # Top-level so the model can't miss it: what's actually being tracked,
        # and how fresh each stream is. Empty/zero elsewhere should be read
        # through these flags (see system prompt).
        "tracking_status": {
            "calorie_tracking_active": cal_goal is not None,
            "protein_tracking_active": pro_goal is not None,
            "has_any_lifts": summary is not None,
            "has_cardio_program_or_sessions": bool(
                cardio_programs or cardio_sessions
            ),
            "days_since_last_lift": _days_since(last_lift_at),
            "days_since_bodyweight": _days_since(last_bw_at),
            "nutrition_days_logged_in_window": {
                "calories": cal_days,
                "protein": pro_days,
                "out_of": days,
            },
        },
        "lifting": {
            "summary": dict(summary) if summary else None,
            "total_tonnage_kg": round(tonnage, 1),
            "personal_bests": [
                dict(r) for r in db.user_top_prs(guild_id, user_id, 12)
            ],
            "most_trained": [
                dict(r) for r in db.user_most_trained(guild_id, user_id, 8)
            ],
            "biggest_gains": [
                dict(r) for r in db.user_biggest_gains(guild_id, user_id, 8)
            ],
            "latest_per_exercise": [
                dict(r)
                for r in db.user_latest_by_equipment(guild_id, user_id)[:40]
            ],
            "goals": [dict(r) for r in db.goal_list(guild_id, user_id)],
            "training_dates_recent": dates[-60:],
            "total_training_days": len(dates),
            "sessions_in_window": sessions_in_window,
            "avg_sessions_per_week": avg_sessions_per_week,
            "estimated_1rm_progression": _e1rm_progression(
                db.user_rep_sets(guild_id, user_id)
            ),
            "last_lift_at": last_lift_at,
        },
        "cardio": {
            "active_programs": [
                {
                    "name": program["name"],
                    "progression_pace": program["pace"],
                    "progression_score": program["progression_score"],
                    "segments": [
                        {
                            "activity": segment.activity,
                            "minutes": segment.minutes,
                            "level": segment.level,
                            "incline_degrees": segment.incline_degrees,
                            "speed": segment.speed,
                        }
                        for segment in cardio.segments_from_rows(program["segments"])
                    ],
                }
                for program in cardio_programs
            ],
            "sessions_in_window": len(cardio_sessions),
            "total_minutes_in_window": round(cardio_total_minutes, 1),
            "avg_sessions_per_week": round(len(cardio_sessions) / weeks, 1),
            "difficulty_ratings": cardio_difficulty,
            "by_activity": sorted(
                cardio_activity.values(),
                key=lambda item: float(item["minutes"]),
                reverse=True,
            ),
            "recent_sessions": [
                {
                    "program": session["program_name"] or "One-off",
                    "difficulty": session["difficulty"],
                    "logged_at": session["logged_at"],
                    "segments": [
                        {
                            "activity": segment.activity,
                            "minutes": segment.minutes,
                            "level": segment.level,
                            "incline_degrees": segment.incline_degrees,
                            "speed": segment.speed,
                        }
                        for segment in cardio.segments_from_rows(
                            session["segments"]
                        )
                    ],
                }
                for session in cardio_sessions[:12]
            ],
        },
        "bodyweight": {
            "recent": [
                {"kg": r["weight_kg"], "at": r["recorded_at"]} for r in bw
            ],
            "latest_kg": bw[-1]["weight_kg"] if bw else None,
            "change_recent_kg": bw_change_kg,
            "measurements": len(bw),
            "smart_scale_composition": (
                _coach_body_composition_block(user_id, days)
                if include_body_composition else None
            ),
        },
        "nutrition": {
            "calorie_goal_kcal": (
                cal_goal["daily_target_kcal"] if cal_goal else None
            ),
            "calorie_total_window": round(cal_total),
            "calorie_entries_window": cal_entries,
            "calorie_days_logged_window": cal_days,
            "protein_goal_g": pro_goal["daily_target_g"] if pro_goal else None,
            "protein_total_window": round(pro_total),
            "protein_entries_window": pro_entries,
            "protein_days_logged_window": pro_days,
            # None unless they run different weekday/weekend targets — the goal
            # numbers above are then just today's, and the coach needs to know
            # a big Saturday may be entirely on plan.
            "split_targets": _split_targets_payload(user_id),
        },
        # Real gym-attendance ground truth: lets the coach tell "didn't train"
        # from "trained but didn't log" — e.g. a live Revo streak next to stale
        # lift data means nudge the *logging*, not the training.
        "attendance": {
            "revo_linked": revo is not None,
            "revo_streak_weeks": (
                revo["last_streak_weeks"] if revo else None
            ),
            "revo_last_checkin": (
                revo["last_checkin_date"] if revo else None
            ),
            "days_since_revo_checkin": _days_since(
                revo["last_checkin_date"] if revo else None
            ),
        },
        # Inferred from Discord presence, only for members who opted into /track.
        # None otherwise so the model doesn't invent sleep advice from nothing.
        "sleep": _coach_sleep_block(guild_id, user_id, days),
    }


def _coach_sleep_block(guild_id: int, user_id: int, days: int) -> dict | None:
    """Nightly-sleep summary for the /coach payload, or None when the member
    isn't presence-tracked (sleep is inferred from Discord online/offline, so
    it only exists for /track opt-ins)."""
    if not ENABLE_PRESENCE_TRACKING or not db.presence_is_tracked(
        guild_id, user_id,
    ):
        return None
    try:
        sessions, _raw, _ws, _we = _collect_sleep_export(guild_id, user_id, days)
    except Exception:  # pragma: no cover - defensive; presence is best-effort
        return None
    if not sessions:
        return None
    stats = sleep_stats(sessions)
    return {
        "nights_observed": stats["nights"],
        "avg_sleep_hours": stats["avg_hours"],
        "weekday_avg_hours": stats["weekday_avg"],
        "weekend_avg_hours": stats["weekend_avg"],
        "typical_bedtime": stats["bedtime"],
        "typical_wake": stats["wake"],
        "note": "Inferred from Discord presence — an approximation, not a "
                "sleep tracker.",
    }


def _coach_body_composition_block(user_id: int, days: int) -> dict | None:
    """Compact, caveated smart-scale trends for the AI coach.

    Values are filtered to the requested window and kept deliberately
    pre-analysed: the model receives latest/change/sample count, not hundreds of
    near-duplicate scale rows.  Direction is neutral because a gain or loss only
    has meaning in the context of the member's own goal.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days)))
    specs = {
        metric.key: metric for metric in ha_client.METRICS
        if metric.key != ha_client.WEIGHT_KEY
    }
    trends: list[dict] = []
    rows = db.body_metric_summaries_between(
        user_id, specs, cutoff.isoformat(), now.isoformat(),
    )
    for row in rows:
        metric = specs.get(str(row["metric"]))
        if metric is None:
            continue
        first_at = ha_client.parse_ha_time(row["first_at"])
        latest_at = ha_client.parse_ha_time(row["latest_at"])
        try:
            first = float(row["first_value"])
            latest = float(row["latest_value"])
            samples = int(row["samples"])
        except (TypeError, ValueError):
            continue
        if (
            first_at is None or latest_at is None
            or not math.isfinite(first) or not math.isfinite(latest)
        ):
            continue
        trends.append({
            "metric": metric.key,
            "label": metric.label,
            "unit": metric.unit,
            "latest": round(latest, metric.precision),
            "change_in_window": (
                round(latest - first, metric.precision)
                if samples >= 2 else None
            ),
            "measurements": samples,
            "first_at": first_at.isoformat(),
            "latest_at": latest_at.isoformat(),
        })
    if not trends:
        return None
    return {
        "source_note": (
            "Consumer bioimpedance smart-scale estimates; hydration-sensitive. "
            "Use multi-reading direction only, not medical interpretation."
        ),
        "metrics": trends,
    }


def _split_targets_payload(user_id: int) -> dict | None:
    """The user's weekday/weekend targets for AI payloads, or None if they run
    one target every day."""
    wd, we = _band_targets(user_id)
    if not (wd.kcal.split or wd.protein.split):
        return None
    return {
        "weekday_calorie_goal_kcal": wd.kcal.value,
        "weekend_calorie_goal_kcal": we.kcal.value,
        "weekday_protein_goal_g": wd.protein.value,
        "weekend_protein_goal_g": we.protein.value,
    }


@bot.tree.command(
    name="coach",
    description="AI progress report built from all of a member's tracked data.",
)
@app_commands.describe(
    user="Whose data to analyse (defaults to you).",
    days="Window for nutrition stats, 1-365 (default 30).",
)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def coach_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    days: int = 30,
) -> None:
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    if not gemini_client.available():
        await interaction.response.send_message(
            "Gemini isn't configured. Set `GEMINI_API_KEY` (and optionally "
            "`GEMINI_MODEL`) in the bot's environment and restart.",
            ephemeral=True,
        )
        return
    days = max(1, min(365, days))
    guild_id = _ctx_guild_id(interaction)
    # Need *something* to analyse — lifts, nutrition tracking, or bodyweight.
    has_data = (
        db.user_summary(guild_id, target.id) is not None
        or db.calorie_goal_get(guild_id, target.id) is not None
        or db.protein_goal_get(guild_id, target.id) is not None
        or bool(db.bodyweight_history(guild_id, target.id, limit=1))
        or bool(db.cardio_program_list(target.id))
        or bool(db.cardio_session_list(target.id, limit=1))
    )
    if not has_data:
        await interaction.response.send_message(
            f"{target.display_name} has no tracked data yet — log some lifts, "
            "cardio, or set up calorie/protein tracking first.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    include_body_composition = target.id == interaction.user.id
    # Always keep a self-report private. Besides protecting the rest of its
    # personal nutrition/bodyweight context, this closes the race where a scale
    # sync lands after a "has composition?" check but before payload assembly.
    composition_private = include_body_composition
    await interaction.response.defer(
        thinking=True, ephemeral=composition_private,
    )
    payload = _build_progress_payload(
        guild_id,
        target.id,
        _display_name(target),
        days,
        include_body_composition=include_body_composition,
    )
    prompt = (
        f"Timezone: {DISPLAY_TZ}. The full tracked dataset for one person "
        "follows as JSON:\n\n" + json.dumps(payload, indent=2, default=str)
    )
    try:
        text = await asyncio.to_thread(
            gemini_client.generate, prompt, system=_COACH_SYSTEM,
            temperature=0.5,  # balanced — analytical but not robotic
            # /coach is deferred, so we can afford a small "thinking" pass for
            # better multi-factor analysis. Budget the token cap to cover both
            # the reasoning and the ~1800-char answer.
            thinking_budget=768,
            max_output_tokens=2200,
        )
    except gemini_client.GeminiError as exc:
        LOG.warning("Gemini coach analysis failed: %s", exc)
        await interaction.followup.send(
            embed=_ai_error_embed(exc), ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"📈 Progress report — {target.display_name}",
        description=text[:4000],
        colour=EMBED_COLOUR,
    )
    embed.set_footer(
        text=(
            f"all-time lifts · {days}d cardio + nutrition · "
            f"{gemini_client.model_name()}"
        )
    )
    await interaction.followup.send(
        embed=embed,
        ephemeral=composition_private,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@track_group.command(
    name="now",
    description="(Owner) Show the live presence Discord is currently reporting for a user.",
)
@app_commands.describe(user="The member to inspect.")
async def track_now_cmd(
    interaction: discord.Interaction, user: discord.Member,
) -> None:
    """Diagnostic: dumps the raw activity payload from Discord's cache.

    Use when /track schedule shows no games — this tells you whether
    Discord is sending activities at all (privacy/detection setting on
    their end) versus the bot dropping them on the parse path.
    """
    if not ENABLE_PRESENCE_TRACKING:
        await interaction.response.send_message(
            embed=_presence_disabled_embed(), ephemeral=True,
        )
        return
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Only bot owners can use this diagnostic.", ephemeral=True,
        )
        return

    # Slash-command Member args are resolved from interaction.resolved,
    # which Discord's API does NOT populate with presence/activity. Always
    # re-fetch from the guild cache or this diagnostic will lie.
    _g = _ctx_guild(interaction)
    cached = _g.get_member(user.id) if _g is not None else None
    member = cached or user
    status = _discord_status_to_str(member.status)
    parsed = _get_main_activity(member)
    raw_activities = list(member.activities)
    cache_note = (
        "✅ resolved from guild cache" if cached is not None
        else "⚠️ falling back to interaction payload (no presence data)"
    )

    lines = [
        cache_note,
        f"**Status:** `{member.status}` → stored as `{status}`",
        f"**Parsed activity:** `{parsed!r}`",
        f"**Raw `member.activities` ({len(raw_activities)}):**",
    ]
    if not raw_activities:
        lines.append("  *(empty — Discord is not reporting any activity)*")
    for act in raw_activities:
        cls = type(act).__name__
        name = getattr(act, "name", None) or getattr(act, "title", None)
        act_type = getattr(act, "type", None)
        lines.append(f"  • `{cls}` name={name!r} type={act_type!r}")

    embed = discord.Embed(
        title=f"🔍 Live presence — {user.display_name}",
        description="\n".join(lines),
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(track_group)


# ---------------------------------------------------------------------------
# Voice-time stats (/voice)
# ---------------------------------------------------------------------------

voice_group = app_commands.Group(
    name="voice",
    description="Voice-call time stats (in-call / muted / deafened).",
)

def _voice_disabled_embed() -> discord.Embed:
    return ui.unavailable(
        "Voice tracking",
        why="Voice join, leave, mute and deafen activity isn't being recorded.",
        admin_fix="Set `ENABLE_VOICE_TRACKING=true` and restart the bot.",
    )


def _pct(part: float, whole: float) -> str:
    """Render ``part/whole`` as a rounded percentage string (``0%`` if empty)."""
    return f"{round(part / whole * 100) if whole else 0}%"


@voice_group.command(
    name="stats",
    description="Show how long a member spent in voice, muted and deafened.",
)
@app_commands.describe(
    user="The member to summarise.",
    days="How many days back to summarise (1-90, default 7).",
)
async def voice_stats_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    days: int = 7,
) -> None:
    if not ENABLE_VOICE_TRACKING:
        await interaction.response.send_message(embed=_voice_disabled_embed(), ephemeral=True)
        return
    if days < 1 or days > 90:
        await interaction.response.send_message(
            "`days` must be between 1 and 90.", ephemeral=True,
        )
        return

    guild_id = _ctx_guild_id(interaction)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    rows = db.voice_events_for(guild_id, user.id, since=window_start, until=now)

    # Slash-command Member args come from interaction.resolved, which lacks
    # voice state — re-fetch from the guild cache so the "still muted now"
    # verification (and the open-interval gate) reads the real live state.
    _g = _ctx_guild(interaction)
    cached = _g.get_member(user.id) if _g is not None else None
    vc = cached.voice if cached is not None else None
    live_in_call = bool(vc and vc.channel)
    live_muted = bool(vc and (vc.self_mute or vc.mute))
    live_deaf = bool(vc and (vc.self_deaf or vc.deaf))

    summary = summarize_voice(
        [(r["event"], r["at"]) for r in rows],
        window_start, now, now=now,
        live_in_call=live_in_call,
        live_muted=live_muted,
        live_deafened=live_deaf,
    )

    if summary.in_call_seconds == 0 and not summary.in_call_now:
        await interaction.response.send_message(
            f"No voice activity recorded for {user.mention} in the last "
            f"{days} day{'s' if days != 1 else ''}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    embed = discord.Embed(
        title=f"🔊 Voice stats — {user.display_name}",
        colour=EMBED_COLOUR,
        description=f"Last **{days}** day{'s' if days != 1 else ''} of voice activity.",
    )
    embed.add_field(
        name="🔊 In voice",
        value=format_duration(summary.in_call_seconds),
        inline=True,
    )
    # Active = in-call while unmuted (the audible complement of Muted). All four
    # totals stay inline: Discord packs 3 per row, so this reads as
    # "In voice · Active · Muted" on the first row with Deafened — a sub-detail
    # of Muted — wrapping onto the second, which keeps the grouping sensible.
    embed.add_field(
        name="🎙️ Active",
        value=(
            f"{format_duration(summary.active_seconds)} "
            f"({_pct(summary.active_seconds, summary.in_call_seconds)})"
        ),
        inline=True,
    )
    embed.add_field(
        name="🔈 Muted",
        value=(
            f"{format_duration(summary.muted_seconds)} "
            f"({_pct(summary.muted_seconds, summary.in_call_seconds)})"
        ),
        inline=True,
    )
    embed.add_field(
        name="🔇 Deafened",
        value=(
            f"{format_duration(summary.deafened_seconds)} "
            f"({_pct(summary.deafened_seconds, summary.in_call_seconds)})"
        ),
        inline=True,
    )

    # Live "right now" streaks, when Discord confirms the member is connected.
    if summary.in_call_now:
        now_lines = [f"🔊 In voice for {format_duration(summary.current_in_call_seconds)}"]
        if summary.deafened_now:
            now_lines.append(
                f"🔇 Deafened for {format_duration(summary.current_deafened_seconds)}"
            )
        elif summary.muted_now:
            now_lines.append(
                f"🔈 Muted for {format_duration(summary.current_muted_seconds)}"
            )
        embed.add_field(name="Now", value="\n".join(now_lines), inline=False)

    embed.set_footer(
        text="Deafened time is a subset of muted time (deafening mutes your mic)."
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(voice_group)


# ---------------------------------------------------------------------------
# Native cardio programs (/cardio)
# ---------------------------------------------------------------------------

cardio_group = app_commands.Group(
    name="cardio",
    description="Create cardio programs, log sessions, and progress them.",
)

_CARDIO_PACE_LABELS = {
    "gentle": "Gentle",
    "standard": "Standard",
    "aggressive": "Aggressive",
}
_CARDIO_DIFFICULTY_LABELS = {
    "easy": "Felt easy",
    "just_right": "About right",
    "hard": "Too hard",
    "unrated": "Not rated",
}
_CARDIO_PACE_CHOICES = [
    app_commands.Choice(name="Gentle — slower increases", value="gentle"),
    app_commands.Choice(name="Standard — balanced progression", value="standard"),
    app_commands.Choice(name="Aggressive — faster increases", value="aggressive"),
]
_CARDIO_DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy — ready to progress", value="easy"),
    app_commands.Choice(name="About right", value="just_right"),
    app_commands.Choice(name="Too hard — may ease the next session back", value="hard"),
]


def _cardio_display_segment(segment: cardio.Segment) -> str:
    safe = cardio.Segment(
        _safe_label(segment.activity, limit=80),
        segment.minutes,
        segment.level,
        segment.incline_degrees,
        segment.speed,
    )
    return cardio.format_segment(safe)


async def _handle_cardio_message(
    message: discord.Message,
    target: object,
    segments: list[cardio.Segment],
    *,
    logged_at: datetime | None = None,
) -> None:
    """Store and confirm one strict whole-message cardio log."""
    if db.cardio_session_get_by_message(message.id) is not None:
        return
    target_id = int(getattr(target, "id"))
    try:
        session_id = db.cardio_session_add(
            target_id,
            segments,
            "unrated",
            logged_at=logged_at or message.created_at.astimezone(timezone.utc),
            message_id=message.id,
            channel_id=message.channel.id,
        )
    except sqlite3.IntegrityError:
        # A duplicate gateway dispatch raced the read above; the unique
        # source-message index already preserved the correct single session.
        return
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    details = "\n".join(
        f"`{index}.` {_cardio_display_segment(segment)}"
        for index, segment in enumerate(segments, 1)
    )
    total = cardio.format_number(cardio.total_minutes(segments))
    backdate = _backdate_label(logged_at)
    suffix = _target_suffix(message.author, target)
    target_line = f"Logged{suffix}:\n" if suffix else ""
    embed = discord.Embed(
        title=f"🏃 Cardio logged — {total} mins",
        description=(
            f"{target_line}{details}{backdate}\n\n"
            "-# React ❌ to remove this session · use `/cardio create` to "
            "turn it into an adaptive program"
        ),
        colour=EMBED_COLOUR,
    )
    try:
        reply = await message.reply(embed=embed, mention_author=False)
    except discord.HTTPException:
        return
    try:
        db.cardio_track_reply(
            reply.id,
            message.author.id,
            target_id,
            session_id,
            original_message_id=message.id,
            channel_id=message.channel.id,
        )
    except Exception:  # pragma: no cover - log is saved; undo is best effort
        LOG.exception("Couldn't remember cardio reply %s", reply.id)
        return
    try:
        await reply.add_reaction("❌")
    except discord.HTTPException:
        pass


def _cardio_program_embed(program: dict) -> discord.Embed:
    segments = cardio.segments_from_rows(program["segments"])
    lines = [
        f"`{index}.` {_cardio_display_segment(segment)}"
        for index, segment in enumerate(segments, 1)
    ]
    pace = str(program["pace"])
    threshold = cardio.PACE_THRESHOLDS[pace]
    score = int(program["progression_score"])
    if score > 0:
        progress = f"progress score **{score}/{threshold}**"
    elif score < 0:
        progress = f"recovery score **{abs(score)}/{threshold}**"
    else:
        progress = "progress score **0**"
    embed = discord.Embed(
        title=f"🏃 Cardio program — {_safe_label(program['name'], limit=80)}",
        description="\n".join(lines),
        colour=EMBED_COLOUR,
    )
    embed.add_field(
        name="Session",
        value=f"**{cardio.format_number(cardio.total_minutes(segments))} mins**",
        inline=True,
    )
    embed.add_field(
        name="Progression",
        value=f"**{_CARDIO_PACE_LABELS[pace]}** · {progress}",
        inline=True,
    )
    embed.set_footer(
        text="Complete it with /cardio complete and rate how it felt.",
    )
    return embed


async def _cardio_program_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    needle = current.casefold().strip()
    return [
        app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
        for row in db.cardio_program_list(interaction.user.id)
        if not needle or needle in row["name"].casefold()
    ][:25]


def _cardio_parse_or_error(raw: str) -> tuple[list[cardio.Segment] | None, str | None]:
    try:
        return cardio.parse_program(raw), None
    except cardio.CardioParseError as exc:
        return None, str(exc)


@cardio_group.command(
    name="create",
    description="Create or replace a reusable cardio program.",
)
@app_commands.describe(
    name='Program name, e.g. "Cardio Day".',
    workout="Parts like: 15 mins elliptical lv12, 15 mins treadmill speed 10.",
    progression="How quickly easy/completed sessions should increase the program.",
)
@app_commands.choices(progression=_CARDIO_PACE_CHOICES)
async def cardio_create_cmd(
    interaction: discord.Interaction,
    name: str,
    workout: str,
    progression: str = "standard",
) -> None:
    clean_name = " ".join(name.split())
    if not clean_name or len(clean_name) > 60:
        await interaction.response.send_message(
            "Use a program name between 1 and 60 characters.", ephemeral=True,
        )
        return
    segments, error = _cardio_parse_or_error(workout)
    if segments is None:
        await interaction.response.send_message(error, ephemeral=True)
        return
    program = db.cardio_program_set(
        interaction.user.id,
        _display_name(interaction.user),
        clean_name,
        progression,
        segments,
    )
    await interaction.response.send_message(embed=_cardio_program_embed(program))


@cardio_group.command(
    name="view",
    description="Show a cardio program (defaults to your most recent one).",
)
@app_commands.describe(name="Program name; leave blank for your most recent.")
@app_commands.autocomplete(name=_cardio_program_autocomplete)
async def cardio_view_cmd(
    interaction: discord.Interaction, name: str | None = None,
) -> None:
    program = db.cardio_program_get(interaction.user.id, name)
    if program is None:
        suffix = f" named **{_safe_label(name)}**" if name else ""
        await interaction.response.send_message(
            f"You don't have a cardio program{suffix}. Create one with "
            "`/cardio create`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    await interaction.response.send_message(embed=_cardio_program_embed(program))


@cardio_group.command(
    name="complete",
    description="Log a saved program and adapt the next session from its difficulty.",
)
@app_commands.describe(
    difficulty="How the whole session felt.",
    name="Program name; leave blank for your most recent.",
)
@app_commands.choices(difficulty=_CARDIO_DIFFICULTY_CHOICES)
@app_commands.autocomplete(name=_cardio_program_autocomplete)
async def cardio_complete_cmd(
    interaction: discord.Interaction,
    difficulty: str,
    name: str | None = None,
) -> None:
    program = db.cardio_program_get(interaction.user.id, name)
    if program is None:
        await interaction.response.send_message(
            "I couldn't find that program. Use `/cardio create` first.",
            ephemeral=True,
        )
        return
    completed = cardio.segments_from_rows(program["segments"])
    result = cardio.apply_difficulty(
        completed,
        difficulty,
        str(program["pace"]),
        int(program["progression_score"]),
        int(program["progression_cursor"]),
    )
    db.cardio_session_add(
        interaction.user.id,
        completed,
        difficulty,
        program_id=int(program["id"]),
        program_name=str(program["name"]),
        next_segments=result.segments,
        progression_score=result.score,
        progression_cursor=result.cursor,
    )

    total = cardio.format_number(cardio.total_minutes(completed))
    lines = [
        f"Logged **{total} mins** from **{_safe_label(program['name'])}**.",
        f"Difficulty: **{_CARDIO_DIFFICULTY_LABELS[difficulty]}**.",
    ]
    if result.before is not None and result.after is not None:
        verb = "Progressed" if result.direction == "increase" else "Eased back"
        lines.append(
            f"{verb} next session: "
            f"~~{_cardio_display_segment(result.before)}~~ → "
            f"**{_cardio_display_segment(result.after)}**."
        )
    else:
        threshold = cardio.PACE_THRESHOLDS[str(program["pace"])]
        if result.score > 0:
            lines.append(
                f"Building toward the next increase: **{result.score}/{threshold}**."
            )
        elif result.score < 0:
            lines.append(
                "Holding the routine while you recover "
                f"(**{abs(result.score)}/{threshold}** toward a small deload)."
            )
        else:
            lines.append("Routine held steady for next time.")
    await interaction.response.send_message(
        embed=discord.Embed(
            title="✅ Cardio complete",
            description="\n".join(lines),
            colour=EMBED_COLOUR,
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@cardio_group.command(
    name="log",
    description="Log a one-off cardio session without creating a program.",
)
@app_commands.describe(
    workout="Parts like: 15 mins elliptical lv12, 15 mins treadmill speed 10.",
    difficulty="How the whole session felt.",
)
@app_commands.choices(difficulty=_CARDIO_DIFFICULTY_CHOICES)
async def cardio_log_cmd(
    interaction: discord.Interaction,
    workout: str,
    difficulty: str = "just_right",
) -> None:
    segments, error = _cardio_parse_or_error(workout)
    if segments is None:
        await interaction.response.send_message(error, ephemeral=True)
        return
    db.cardio_session_add(interaction.user.id, segments, difficulty)
    details = "\n".join(
        f"`{index}.` {_cardio_display_segment(segment)}"
        for index, segment in enumerate(segments, 1)
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            title=(
                "✅ Cardio logged — "
                f"{cardio.format_number(cardio.total_minutes(segments))} mins"
            ),
            description=(
                f"{details}\n\nDifficulty: "
                f"**{_CARDIO_DIFFICULTY_LABELS[difficulty]}**"
            ),
            colour=EMBED_COLOUR,
        )
    )


@cardio_group.command(
    name="history",
    description="Show recent tracked cardio sessions.",
)
@app_commands.describe(
    user="The member to look up (defaults to you).",
    limit="Number of sessions to show, 1-20.",
)
async def cardio_history_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    limit: int = 10,
) -> None:
    target = user or interaction.user
    if await _deny_invisible_target(interaction, target):
        return
    if await _deny_channel_outsider(interaction, target):
        return
    rows = db.cardio_session_list(target.id, limit=max(1, min(20, limit)))
    if not rows:
        await interaction.response.send_message(
            f"{target.display_name} hasn't logged any cardio yet.",
            ephemeral=True,
        )
        return
    lines = []
    for row in rows:
        segments = cardio.segments_from_rows(row["segments"])
        when = _format_date(row["logged_at"])
        program = row["program_name"] or "One-off"
        difficulty = _CARDIO_DIFFICULTY_LABELS.get(
            row["difficulty"], str(row["difficulty"]),
        )
        parts = [_cardio_display_segment(segment) for segment in segments[:3]]
        if len(segments) > 3:
            parts.append(f"+{len(segments) - 3} more")
        lines.append(
            f"**{when}** · {_safe_label(program, limit=40)} · "
            f"**{cardio.format_number(cardio.total_minutes(segments))} mins** "
            f"· {difficulty}\n-# {' · '.join(parts)}"
        )
    shown = list(lines)
    while len("\n".join(shown)) > 3900 and len(shown) > 1:
        shown.pop()
    omitted = len(lines) - len(shown)
    if omitted:
        shown.append(f"-# …and {omitted} older session{'s' if omitted != 1 else ''}.")
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"🏃 Cardio history — {target.display_name}",
            description="\n".join(shown),
            colour=EMBED_COLOUR,
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@cardio_group.command(
    name="remove",
    description="Delete a saved cardio program (completed history is kept).",
)
@app_commands.describe(name="Program to delete.")
@app_commands.autocomplete(name=_cardio_program_autocomplete)
async def cardio_remove_cmd(
    interaction: discord.Interaction, name: str,
) -> None:
    removed = db.cardio_program_remove(interaction.user.id, name)
    if not removed:
        await interaction.response.send_message(
            "I couldn't find that program.", ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Removed **{_safe_label(name)}**. Your completed cardio history is "
        "still kept.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(cardio_group)


# ---------------------------------------------------------------------------
# Calorie tracking (/calories)
# ---------------------------------------------------------------------------

calories_group = app_commands.Group(
    name="calories",
    description="Track daily calorie intake against a personal target.",
)

# Sanity caps so a fat-fingered "25000" target or "65000kj" entry doesn't
# wreck someone's stats. Entries are per-item, targets are per-day.
_MAX_TARGET_KCAL = 20_000
# Sanity ceiling for a single intake entry. A whole big day still fits, but
# joke/typo posts like "9999c" bounce instead of poisoning averages and TDEE.
_MAX_ENTRY_KCAL = 6_000

def _calories_not_set_embed(name: str | None = None) -> discord.Embed:
    """Not-tracking-yet state, for yourself or for someone you looked up."""
    if name is not None:
        return ui.empty(f"{_safe_label(name, limit=32)} isn't tracking calories")
    return ui.empty(
        "You're not tracking calories yet",
        hint="Set a daily target and I'll count everything you post from "
             "then on — `2500`, or `8700kj` if your labels are in kilojoules.",
        cmd="/calories setup <target> to start",
    )

# What someone types into a `weekend:` option to say "no separate weekend
# target, just use my normal one".
_CLEAR_WORDS = {"none", "off", "no", "same", "clear", "-", "unset"}


def _parse_weekend_option(raw: str | None, parse, limit: float):
    """Read a ``weekend:`` command option into what the DB layer expects.

    Three outcomes, because "don't touch it" and "clear it" are different
    things: ``KEEP`` when the option was omitted (re-running setup to nudge the
    weekday number shouldn't quietly discard a weekend target), ``None`` when
    they asked to clear it, otherwise the parsed amount. Returns
    ``(value, error)`` — ``error`` is a ready-to-send string when unparseable.
    """
    if raw is None:
        return KEEP, None
    text = raw.strip().lower()
    if text in _CLEAR_WORDS:
        return None, None
    parsed = parse(raw)
    amount = parsed[0] if isinstance(parsed, tuple) else parsed
    if amount is None or amount <= 0:
        return KEEP, "Couldn't read that weekend amount."
    if amount > limit:
        return KEEP, (
            f"That weekend target is over {limit:,.0f}/day — looks like a typo."
        )
    return amount, None


def _today_window() -> tuple[str, str]:
    """UTC ISO bounds of the current local (DISPLAY_TIMEZONE) day."""
    today = datetime.now(DISPLAY_TZ).date()
    start_local = datetime.combine(today, dtime.min, tzinfo=DISPLAY_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _target_label_suffix(label: str | None) -> str:
    """Discord subtext naming the active target set, or nothing.

    ``label`` is None unless the user actually runs different weekday and
    weekend targets, so someone with one all-week goal never sees a banner
    telling them which of one set is in force.
    """
    return f"\n-# {label}" if label else ""


def _calorie_status_line(
    total: float, target: float, label: str | None = None,
) -> str:
    """Calorie progress against a daily **target** — reaching it is the win."""
    return _calorie_status(total, target, label)[0]


def _calorie_status(
    total: float, target: float, label: str | None = None,
) -> tuple[str, discord.Colour]:
    """As above, plus the colour an embed built around it should wear."""
    text, colour = ui.meter(total, target, calories.format_kcal)
    return text + _target_label_suffix(label), colour


def _reply_targets(
    user_id: int, logged_at: datetime | None = None,
) -> targets_mod.Resolved:
    """The targets to render a *log reply* against.

    Like :func:`_targets_on`, except a day with no target at all borrows today's.
    That happens when someone stopped tracking for a stretch, started again, and
    is now backdating an entry into the gap: scoring it against nothing would
    render "200 cal / 0 cal · 200 cal over target".

    Analytics must keep using :func:`_targets_on` — there a day with no target
    is a real gap, and quietly lending it today's number would invent adherence
    figures for days the user wasn't tracking.
    """
    resolved = _targets_on(user_id, logged_at)
    if logged_at is None or (
        resolved.kcal.value is not None and resolved.protein.value is not None
    ):
        return resolved

    def _borrow(
        own: targets_mod.MacroTarget, now: targets_mod.MacroTarget,
    ) -> targets_mod.MacroTarget:
        if own.value is not None:
            return own
        # A borrowed number belongs to *today's* band, so the entry day's
        # "Using Weekend Targets" caption wouldn't describe it. Say nothing.
        return targets_mod.MacroTarget(now.value, now.scope, split=False)

    today = _targets_on(user_id)
    return targets_mod.Resolved(
        day=resolved.day,
        kcal=_borrow(resolved.kcal, today.kcal),
        protein=_borrow(resolved.protein, today.protein),
    )


def _reply_label(
    resolved: targets_mod.Resolved, *, calories: bool, protein: bool,
) -> str | None:
    """The single weekday/weekend banner for a reply, describing only the macros
    that reply actually shows.

    ``Resolved.label`` is true when *either* macro splits, so a calorie-only
    reply would otherwise announce "Using Weekend Targets" to someone whose
    calories are the same every day and whose protein happens to differ.
    """
    macros = []
    if calories:
        macros.append(targets_mod.MACRO_KCAL)
    if protein:
        macros.append(targets_mod.MACRO_PROTEIN)
    for macro in macros:
        label = resolved.label_for(macro)
        if label:
            return label
    return None


def _calorie_status_pair(
    user_id: int, total: float, logged_at: datetime | None = None,
) -> tuple[str, discord.Colour]:
    """:func:`_calorie_status_for` plus the colour a card around it should wear."""
    resolved = _reply_targets(user_id, logged_at)
    return _calorie_status(
        total, resolved.kcal.value or 0.0,
        resolved.label_for(targets_mod.MACRO_KCAL),
    )


def _protein_status_pair(
    user_id: int, total: float, logged_at: datetime | None = None,
) -> tuple[str, discord.Colour]:
    """:func:`_protein_status_for` plus its card colour."""
    resolved = _reply_targets(user_id, logged_at)
    return _protein_status(
        total, resolved.protein.value or 0.0,
        resolved.label_for(targets_mod.MACRO_PROTEIN),
    )


def _calorie_status_for(
    user_id: int, total: float, logged_at: datetime | None = None,
) -> str:
    """Calorie status line against the target in force on the entry's own day.

    The one-call form callers should reach for: it can't accidentally score a
    backdated Sunday entry against Monday's target the way passing a
    separately-fetched goal row around could.
    """
    resolved = _reply_targets(user_id, logged_at)
    return _calorie_status_line(
        total, resolved.kcal.value or 0.0,
        resolved.label_for(targets_mod.MACRO_KCAL),
    )


def _protein_status_for(
    user_id: int, total: float, logged_at: datetime | None = None,
) -> str:
    """Protein status line against the ceiling in force on the entry's own day."""
    resolved = _reply_targets(user_id, logged_at)
    return _protein_status_line(
        total, resolved.protein.value or 0.0,
        resolved.label_for(targets_mod.MACRO_PROTEIN),
    )


@calories_group.command(
    name="setup",
    description="Set your daily calorie target — accepts kcal or kJ.",
)
@app_commands.describe(
    target='Daily target, e.g. "2500", "2500c", or "8700kj".',
    weekend='Optional different target for Sat/Sun, e.g. "3000". "same" clears it.',
)
async def calories_setup_cmd(
    interaction: discord.Interaction, target: str,
    weekend: str | None = None,
) -> None:
    parsed = calories.parse_energy(target)
    if parsed is None or parsed[0] <= 0:
        await interaction.response.send_message(
            "Couldn't read that amount — try `2500`, `2500c`, or `8700kj`.",
            ephemeral=True,
        )
        return
    kcal, unit = parsed
    if kcal > _MAX_TARGET_KCAL:
        await interaction.response.send_message(
            f"That's over {_MAX_TARGET_KCAL:,} cal/day — looks like a typo. "
            "If you meant kilojoules, write it as e.g. `8700kj`.",
            ephemeral=True,
        )
        return
    weekend_kcal, err = _parse_weekend_option(
        weekend, calories.parse_energy, _MAX_TARGET_KCAL,
    )
    if err:
        await interaction.response.send_message(
            f"{err} Try `3000`, `3000c`, `12500kj`, or `same` to drop it.",
            ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    db.calorie_goal_set(
        guild_id, interaction.user.id, _display_name(interaction.user), kcal,
        weekend_kcal,
    )
    converted = (
        f" (converted from {target.strip()})" if unit == "kj" else ""
    )
    # Re-resolve rather than echo the inputs: it accounts for a weekend override
    # that was already in place and left untouched by this call.
    wd, we = _band_targets(interaction.user.id)
    if wd.kcal.split:
        head = (
            f"🍎 Calorie targets set — weekdays "
            f"**{calories.format_kcal(wd.kcal.value or 0.0)}**{converted}, "
            f"weekends **{calories.format_kcal(we.kcal.value or 0.0)}**."
        )
    else:
        head = (
            f"🍎 Daily calorie target set to "
            f"**{calories.format_kcal(kcal)}**{converted}, every day."
        )
    await interaction.response.send_message(
        f"{head}\n"
        "Log what you eat with `/calories add` (kcal or kJ — I'll convert), "
        "check in with `/today`, and you'll be included in the "
        "Sunday weekly report."
    )


@calories_group.command(
    name="targets",
    description="Show your weekday and weekend calorie + protein targets.",
)
@app_commands.describe(user="The member to look up (defaults to you).")
async def calories_targets_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    if await _deny_channel_outsider(interaction, target_user):
        return
    wd, we = _band_targets(target_user.id)
    today = _targets_on(target_user.id)
    if today.kcal.value is None and today.protein.value is None:
        msg = (
            "You're not tracking calories or protein yet — start with "
            "`/calories setup` or `/protein setup`."
            if target_user == interaction.user
            else f"{target_user.display_name} isn't tracking nutrition."
        )
        await interaction.response.send_message(msg, ephemeral=True)
        return

    def _amounts(band: targets_mod.Resolved) -> str:
        bits = []
        if band.kcal.value is not None:
            bits.append(calories.format_kcal(band.kcal.value))
        if band.protein.value is not None:
            bits.append(f"{protein_mod.format_grams(band.protein.value)} protein")
        return " · ".join(bits) or "—"

    if today.split:
        lines = [
            f"`Weekday  ` {_amounts(wd)}"
            + (" ← **today**" if not today.is_weekend else ""),
            f"`Weekend  ` {_amounts(we)}"
            + (" ← **today**" if today.is_weekend else ""),
            f"\n-# {today.label}",
        ]
    else:
        lines = [
            f"`Every day` {_amounts(today)}",
            "\n-# One target, seven days a week. Add a weekend one with "
            "`/calories setup <target> weekend:<target>`.",
        ]
    embed = discord.Embed(
        title=f"🎯 Nutrition targets — {target_user.display_name}",
        description="\n".join(lines),
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


@calories_group.command(
    name="add",
    description="Log calories you just ate — kcal or kJ, I'll convert.",
)
@app_commands.describe(
    amount='Energy ("650", "650c", "2700kj") or a saved food ("coffee", "2 coffee").',
    note="What it was (optional, shows in /today).",
    day='Backdate it: "yesterday", "monday", "3 days ago", or "2026-06-28".',
)
async def calories_add_cmd(
    interaction: discord.Interaction,
    amount: str,
    note: str | None = None,
    day: str | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    goal = db.calorie_goal_get(guild_id, interaction.user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(), ephemeral=True,
        )
        return
    logged_at, day_ok = _slash_logged_at(day)
    if not day_ok:
        await interaction.response.send_message(_BAD_DAY_MSG, ephemeral=True)
        return

    # Energy amount first ("650", "2700kj"); fall back to a saved-food name.
    parsed = calories.parse_energy(amount)
    if parsed is not None and parsed[0] > 0:
        kcal, unit = parsed
        logged_label = amount.strip()
        converted = f" = {calories.format_kcal(kcal)}" if unit == "kj" else ""
    else:
        food = calories.parse_food_phrase(amount)
        row = (
            db.calorie_food_get(guild_id, interaction.user.id, food[1])
            if food is not None else None
        )
        if row is None:
            await interaction.response.send_message(
                "Couldn't read that — try `650`, `2700kj`, or a saved food "
                "name like `coffee` (define one with `/calories food_set`).",
                ephemeral=True,
            )
            return
        servings = food[0]
        kcal = float(row["kcal"]) * servings
        base = row["display"]
        logged_label = base if servings == 1 else f"{base} ×{servings}"
        converted = f" = {calories.format_kcal(kcal)}"
        if note is None:
            note = logged_label

    if kcal > _MAX_ENTRY_KCAL:
        await interaction.response.send_message(
            f"That's over {_MAX_ENTRY_KCAL:,} cal in one entry — looks like "
            "a typo. If you meant kilojoules, write it as e.g. `2700kj`.",
            ephemeral=True,
        )
        return
    cal_id = db.calorie_add(
        guild_id, interaction.user.id, _display_name(interaction.user),
        kcal, note=note, raw=amount.strip(), logged_at=logged_at,
    )
    total, _n = db.calorie_total_between(
        guild_id, interaction.user.id, *_day_window_for(logged_at),
    )
    # Skip the note suffix when it just repeats the food name we already show.
    note_part = f" — {note}" if note and note != logged_label else ""
    await interaction.response.send_message(
        f"{ui.FOOD} Logged **{logged_label}**{converted}{note_part}"
        f"{_backdate_label(logged_at)}\n"
        + _calorie_status_for(interaction.user.id, total, logged_at)
        + _streak_suffix(_calorie_streak(interaction.user.id))
        + "\n" + ui.subtext("react ❌ to remove")
    )
    await _attach_undo(interaction, calorie_id=cal_id)


@calories_group.command(
    name="week",
    description="Per-day calorie totals for the last 7 days.",
)
@app_commands.describe(user="The member to look up (defaults to you).")
async def calories_week_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    if await _deny_channel_outsider(interaction, target_user):
        return
    guild_id = _ctx_guild_id(interaction)
    goal = db.calorie_goal_get(guild_id, target_user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(
                None if target_user == interaction.user
                else target_user.display_name
            ),
            ephemeral=True,
        )
        return
    _label, start_iso, end_iso = _week_window()
    day_totals = _calorie_week_days(guild_id, target_user.id, start_iso, end_iso)
    today = datetime.now(DISPLAY_TZ).date()
    week = [today - timedelta(days=n) for n in range(6, -1, -1)]
    # Each day is scored against the target that was in force on that day, so a
    # goal changed mid-week doesn't retroactively re-grade the days before it.
    day_targets = targets_mod.resolve_days(
        db.nutrition_target_rows(target_user.id), week,
    )
    # A diverging chart, not a progress bar: scored against a daily target,
    # most days end up over, and a progress bar pins full for all of them —
    # going flat exactly where the week is most interesting. Bar length is the
    # distance from target, so the outlier day is the longest bar.
    rows: list[tuple[str, float | None, float]] = []
    logged_days = 0
    target_sum = 0.0
    for day in week:
        key = day.isoformat()
        day_name = _WEEKDAY_NAMES[day.weekday()][:3]
        target_kcal = day_targets[day].kcal.value or 0.0
        total = day_totals.get(key)
        if total is not None:
            logged_days += 1
            target_sum += target_kcal
        rows.append((day_name, total, target_kcal))
    if not logged_days:
        # Seven rows of empty track and em dashes is a chart of nothing — say
        # so instead of drawing it. Public and third-person when it's someone
        # else's week: the normal view is public, and "post an amount in chat"
        # is advice the reader can't act on for another member.
        await interaction.response.send_message(
            embed=_nothing_logged_embed(
                target_user, interaction.user, "calories",
                how="`650`, `650c`, `2700kj` in chat, or `/calories add`",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    lines = [
        ui.diverging(rows, calories.format_kcal),
        ui.subtext("left of the line = under target · right = over"),
    ]
    if logged_days:
        avg = sum(day_totals.values()) / logged_days
        avg_target = target_sum / logged_days
        lines.append(
            f"Avg on logged days: **{calories.format_kcal(avg)}** vs "
            f"target {calories.format_kcal(avg_target)}"
        )
        lines.extend(_band_breakdown_lines(
            day_totals, day_targets, calories.format_kcal,
            targets_mod.MACRO_KCAL,
        ))
    streak = _calorie_streak(target_user.id)
    embed = ui.card(
        f"{ui.FOOD} Calories this week",
        description="\n".join(lines),
        colour=ui.score_target(
            sum(day_totals.values()) / logged_days if logged_days else 0.0,
            target_sum / logged_days if logged_days else 0.0,
        ),
        member=target_user,
        footer=(
            f"{ui.STREAK} {ui.plural(streak, 'day')} logging streak"
            if streak >= 2 else None
        ),
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


@calories_group.command(
    name="edit",
    description="Fix the amount of your most recent calorie entry.",
)
@app_commands.describe(
    amount='Corrected energy ("650", "650c", "2700kj").',
    note="Optional new note (leave blank to keep the old one).",
)
async def calories_edit_cmd(
    interaction: discord.Interaction, amount: str, note: str | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    goal = db.calorie_goal_get(guild_id, interaction.user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(), ephemeral=True,
        )
        return
    parsed = calories.parse_energy(amount)
    if parsed is None or parsed[0] <= 0:
        await interaction.response.send_message(
            "Couldn't read that — try `650`, `650c`, or `2700kj`.",
            ephemeral=True,
        )
        return
    kcal, unit = parsed
    if kcal > _MAX_ENTRY_KCAL:
        await interaction.response.send_message(
            f"That's over {_MAX_ENTRY_KCAL:,} cal in one entry — looks like a typo.",
            ephemeral=True,
        )
        return
    old = db.calorie_update_last(
        guild_id, interaction.user.id, kcal, note=note, raw=amount.strip(),
        username=_display_name(interaction.user),
    )
    if old is None:
        await interaction.response.send_message(
            "No calorie entries to edit yet.", ephemeral=True,
        )
        return
    converted = f" = {calories.format_kcal(kcal)}" if unit == "kj" else ""
    total, _n = db.calorie_total_between(
        guild_id, interaction.user.id, *_today_window(),
    )
    await interaction.response.send_message(
        f"✏️ Updated last entry "
        f"**{calories.format_kcal(float(old['kcal']))}** → "
        f"**{calories.format_kcal(kcal)}**{converted}\n"
        + _calorie_status_for(interaction.user.id, total)
    )
    # The edited entry is the most recent one, so anything posted after it in
    # the same day was printed against the old figure.
    await _restate_day_replies(
        interaction.user, logged_at=_parse_iso(old["logged_at"]),
        guild_id=guild_id,
    )


@calories_group.command(
    name="leaderboard",
    description="Who's on the longest calorie-logging streak.",
)
async def calories_leaderboard_cmd(interaction: discord.Interaction) -> None:
    guild_id = _ctx_guild_id(interaction)
    rows = db.calorie_tracked_users(guild_id)
    if not rows:
        await interaction.response.send_message(
            "Nobody's tracking calories in this server yet.", ephemeral=True,
        )
        return
    ranked = sorted(
        (
            (_calorie_streak(int(r["user_id"])),
             db.get_user_nickname(int(r["user_id"])) or r["username"])
            for r in rows
        ),
        key=lambda t: (-t[0], t[1].lower()),
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (streak, name) in enumerate(ranked):
        if streak <= 0:
            continue
        prefix = medals[i] if i < len(medals) else f"`{i + 1}.`"
        lines.append(
            f"{prefix} **{_safe_label(name)}** — 🔥 {streak} day"
            f"{'s' if streak != 1 else ''}"
        )
    desc = (
        "\n".join(lines) if lines
        else "No active streaks yet — log something today to start one! 🔥"
    )
    embed = discord.Embed(
        title="🔥 Calorie logging streaks",
        description=desc,
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


@calories_group.command(
    name="stop",
    description="Stop tracking calories (your logged history is kept).",
)
async def calories_stop_cmd(interaction: discord.Interaction) -> None:
    guild_id = _ctx_guild_id(interaction)
    removed = db.calorie_goal_remove(guild_id, interaction.user.id)
    if not removed:
        await interaction.response.send_message(
            "You weren't tracking calories.", ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "🍎 Calorie tracking stopped — your history is kept, and "
        "`/calories setup` re-enables it any time."
    )


@calories_group.command(
    name="food_set",
    description="Save a food → calorie shortcut you can log by name.",
)
@app_commands.describe(
    name="Food name, e.g. coffee or protein shake.",
    amount='Calories per serving — kcal or kJ ("120", "120c", "500kj").',
    protein='Optional protein per serving in grams ("30", "30g"). '
            "Logged automatically when you log this food.",
)
async def calories_food_set_cmd(
    interaction: discord.Interaction, name: str, amount: str,
    protein: str | None = None,
) -> None:
    norm = calories.normalize_food(name)
    if not norm:
        await interaction.response.send_message(
            "Give the food a name, e.g. `/calories food_set coffee 5`.",
            ephemeral=True,
        )
        return
    parsed = calories.parse_energy(amount)
    if parsed is None or parsed[0] <= 0:
        await interaction.response.send_message(
            "Couldn't read that amount — try `120`, `120c`, or `500kj`.",
            ephemeral=True,
        )
        return
    kcal, _unit = parsed
    if kcal > _MAX_ENTRY_KCAL:
        await interaction.response.send_message(
            f"That's over {_MAX_ENTRY_KCAL:,} cal per serving — looks like a "
            "typo. If you meant kilojoules, write it as e.g. `500kj`.",
            ephemeral=True,
        )
        return
    # Optional protein. ``None`` is preserved on update by calorie_food_set, so
    # re-saving a food with only a new calorie amount keeps its protein.
    protein_g: float | None = None
    if protein is not None and protein.strip():
        protein_g = protein_mod.parse_protein_amount(protein)
        if protein_g is None or protein_g < 0:
            await interaction.response.send_message(
                "Couldn't read that protein amount — try `30` or `30g`.",
                ephemeral=True,
            )
            return
        if protein_g > _MAX_PROTEIN_ENTRY_G:
            await interaction.response.send_message(
                f"That's over {_MAX_PROTEIN_ENTRY_G}g protein per serving — "
                "looks like a typo.",
                ephemeral=True,
            )
            return
    guild_id = _ctx_guild_id(interaction)
    db.calorie_food_set(
        guild_id, interaction.user.id, norm, name.strip(), kcal, protein_g,
    )
    db.add_audit(
        guild_id, "data", "food_set",
        actor_id=interaction.user.id, subject_id=interaction.user.id,
        detail=f"saved food {name.strip()} = {calories.format_kcal(kcal)}"
        + (f", {protein_mod.format_grams(protein_g)} protein"
           if protein_g is not None else ""),
    )
    # Re-read so the confirmation reflects the stored protein (which may have
    # been preserved from a previous save when protein was omitted this time).
    saved = db.calorie_food_get(guild_id, interaction.user.id, norm)
    stored_protein = (
        saved["protein_g"] if saved and saved["protein_g"] is not None else None
    )
    cal_tracking = db.calorie_goal_get(guild_id, interaction.user.id) is not None
    pro_tracking = db.protein_goal_get(guild_id, interaction.user.id) is not None
    macro = calories.format_kcal(kcal)
    if stored_protein is not None:
        macro += f" + {protein_mod.format_grams(float(stored_protein))} protein"
    if not cal_tracking:
        tail = "Run `/calories setup` to start tracking, then log it by name."
    elif stored_protein is not None and not pro_tracking:
        tail = (
            f"Log it by typing `{norm}` in chat. Run `/protein setup` too and "
            "it'll log protein as well."
        )
    else:
        tail = f"Log it by typing `{norm}` (or `2 {norm}`) in chat, or `/calories add {norm}`."
    await interaction.response.send_message(
        f"🍴 Saved **{name.strip()}** = {macro}. {tail}"
    )


@calories_group.command(
    name="food_list", description="List your saved food shortcuts.",
)
async def calories_food_list_cmd(interaction: discord.Interaction) -> None:
    rows = db.calorie_food_list(_ctx_guild_id(interaction), interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You haven't saved any foods yet. Add one with "
            "`/calories food_set <name> <amount>`.",
            ephemeral=True,
        )
        return
    lines = []
    for r in rows:
        line = f"• **{r['display']}** — {calories.format_kcal(float(r['kcal']))}"
        if r["protein_g"] is not None:
            line += f" · {protein_mod.format_grams(float(r['protein_g']))} protein"
        lines.append(line)
    embed = discord.Embed(
        title="🍴 Your saved foods",
        description="\n".join(lines)[:4000],
        colour=EMBED_COLOUR,
    )
    embed.set_footer(text="Log one by typing its name in chat, e.g. coffee")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@calories_group.command(
    name="food_remove", description="Delete one of your saved food shortcuts.",
)
@app_commands.describe(name="The saved food to remove.")
async def calories_food_remove_cmd(
    interaction: discord.Interaction, name: str,
) -> None:
    norm = calories.normalize_food(name)
    removed = db.calorie_food_remove(
        _ctx_guild_id(interaction), interaction.user.id, norm,
    )
    if removed:
        db.add_audit(
            _ctx_guild_id(interaction), "data", "food_delete",
            actor_id=interaction.user.id, subject_id=interaction.user.id,
            detail=f"removed food {name.strip()}",
        )
    msg = (
        f"🗑️ Removed **{name.strip()}**." if removed
        else f"No saved food called **{name.strip()}**. "
        "See `/calories food_list`."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@calories_group.command(
    name="tdee",
    description="Estimate your maintenance calories from your own logs + weigh-ins.",
)
@app_commands.describe(
    user="The member to analyse (defaults to you).",
    days="Window to analyse, 14–90 days (default 28).",
)
async def calories_tdee_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    days: app_commands.Range[int, 14, 90] = 28,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    if await _deny_channel_outsider(interaction, target_user):
        return
    guild_id = _ctx_guild_id(interaction)
    goal = db.calorie_goal_get(guild_id, target_user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(
                None if target_user == interaction.user
                else target_user.display_name
            ),
            ephemeral=True,
        )
        return

    now_local = datetime.now(DISPLAY_TZ)
    start_local = datetime.combine(
        now_local.date() - timedelta(days=days), dtime.min, tzinfo=DISPLAY_TZ,
    )
    start_iso = start_local.astimezone(timezone.utc).isoformat()
    end_iso = now_local.astimezone(timezone.utc).isoformat()

    weights: list[tuple[datetime, float]] = []
    for row in db.bodyweight_history(guild_id, target_user.id):
        dt = datetime.fromisoformat(row["recorded_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= start_local.astimezone(timezone.utc):
            weights.append((dt, float(row["weight_kg"])))
    day_totals = {
        date.fromisoformat(k): v
        for k, v in _calorie_week_days(
            guild_id, target_user.id, start_iso, end_iso,
        ).items()
    }

    est, reason = tdee_lib.estimate_tdee(weights, day_totals)
    if est is None:
        await interaction.response.send_message(
            f"Can't estimate maintenance yet: {reason}.", ephemeral=True,
        )
        return

    # The projection multiplies a daily gap by seven, so it has to run against
    # the mean of the seven targets actually coming up — for a 1,500/2,200
    # weekday/weekend split that's 1,700/day, not either number on its own.
    today = targets_mod.local_today()
    target_rows = db.nutrition_target_rows(target_user.id)
    week_ahead = [today + timedelta(days=n) for n in range(7)]
    target_kcal = targets_mod.mean_target(target_rows, week_ahead)
    if target_kcal is None:  # pragma: no cover - goal is not None above
        target_kcal = float(goal["daily_target_kcal"])
    gap = target_kcal - est.tdee_kcal   # negative = target sits in a deficit
    projected_kg_wk = gap * 7.0 / tdee_lib.KCAL_PER_KG
    direction = "below" if gap < 0 else "above"
    wd, we = _band_targets(target_user.id)
    if wd.kcal.split:
        target_desc = (
            f"Your targets (weekdays {calories.format_kcal(wd.kcal.value or 0)}, "
            f"weekends {calories.format_kcal(we.kcal.value or 0)} — averaging "
            f"{calories.format_kcal(target_kcal)}/day)"
        )
    else:
        target_desc = f"Your target ({calories.format_kcal(target_kcal)})"
    trend = (
        f"{est.kg_per_week:+.2f} kg/week "
        f"({est.start_kg:.1f} → {est.end_kg:.1f} kg)"
    )
    lines = [
        f"Based on the last **{est.days_spanned} days**: {est.weighins} "
        f"weigh-ins, calories logged on {est.logged_days}/"
        f"{est.days_spanned + 1} days ({est.coverage:.0%}).",
        f"Average intake: **{calories.format_kcal(est.avg_intake_kcal)}**/day "
        f"({calories.kcal_to_kj(est.avg_intake_kcal):,.0f} kJ)",
        f"Weight trend: **{trend}**",
        "",
        f"**Estimated maintenance ≈ {calories.format_kcal(est.tdee_kcal)}/day** "
        f"({calories.kcal_to_kj(est.tdee_kcal):,.0f} kJ)",
        f"{target_desc} sits "
        f"≈ **{calories.format_kcal(abs(gap))} {direction}** maintenance → "
        f"≈ **{projected_kg_wk:+.2f} kg/week** if you keep hitting it.",
    ]
    if est.coverage < 0.8:
        lines.append(
            "\n⚠️ Unlogged days in the window make this rougher than it "
            "could be — log every day for a tighter estimate."
        )
    lines.append(
        "\n*Rule-of-thumb maths (7 700 cal ≈ 1 kg) on your own data — "
        "water weight and unlogged snacks both move it.*"
    )
    embed = discord.Embed(
        title=f"📐 Maintenance estimate — {target_user.display_name}",
        description="\n".join(lines),
        colour=EMBED_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


@calories_group.command(
    name="export",
    description="Download your nutrition history (calories, protein, bodyweight) as CSV.",
)
@app_commands.describe(user="Only export this member's data (defaults to you).")
async def calories_export_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    guild_id = _ctx_guild_id(interaction)
    all_time = ("0000-01-01T00:00:00+00:00", "9999-01-01T00:00:00+00:00")

    files: list[discord.File] = []
    counts: list[str] = []

    cal_rows = db.calorie_entries_between(guild_id, target_user.id, *all_time)
    if cal_rows:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["logged_at", "kcal", "note", "raw"])
        for r in cal_rows:
            w.writerow([r["logged_at"], r["kcal"], r["note"] or "", r["raw"] or ""])
        files.append(discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8")), filename="calories.csv",
        ))
        counts.append(f"{len(cal_rows)} calorie entries")

    pro_rows = db.protein_entries_between(guild_id, target_user.id, *all_time)
    if pro_rows:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["logged_at", "grams", "note", "raw"])
        for r in pro_rows:
            w.writerow([r["logged_at"], r["grams"], r["note"] or "", r["raw"] or ""])
        files.append(discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8")), filename="protein.csv",
        ))
        counts.append(f"{len(pro_rows)} protein entries")

    bw_rows = db.bodyweight_history(guild_id, target_user.id)
    if bw_rows:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["recorded_at", "weight_kg"])
        for r in bw_rows:
            w.writerow([r["recorded_at"], r["weight_kg"]])
        files.append(discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8")), filename="bodyweight.csv",
        ))
        counts.append(f"{len(bw_rows)} weigh-ins")

    if not files:
        await interaction.response.send_message(
            f"No nutrition data to export for {target_user.display_name}.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Exported {', '.join(counts)} for **{target_user.display_name}**.",
        files=files,
        ephemeral=True,
    )


@calories_group.command(
    name="food_lookup",
    description="Look up a packaged food's per-100g values (Open Food Facts).",
)
@app_commands.describe(
    query="Product name or barcode, e.g. 'vegemite' or '9300650658516'.",
    save="Optionally save the top result as a food shortcut under this name "
         "(values are per 100 g).",
)
async def calories_food_lookup_cmd(
    interaction: discord.Interaction, query: str, save: str | None = None,
) -> None:
    if not food_lookup.available():
        await interaction.response.send_message(
            "Food lookup isn't available on this bot (missing HTTP "
            "dependency).", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    try:
        results = await asyncio.to_thread(food_lookup.lookup, query.strip())
    except food_lookup.FoodLookupError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}")
        return
    if not results:
        await interaction.followup.send(
            f"No usable match for **{query.strip()}** on Open Food Facts — "
            "try a simpler name or the barcode."
        )
        return

    top = results[0]
    title = top.name if top.brand is None else f"{top.name} ({top.brand})"
    kj = top.kj_per_100g
    kcal = top.kcal_per_100g
    if kcal is None and kj is not None:
        kcal = calories.kj_to_kcal(kj)
    if kj is None and kcal is not None:
        kj = calories.kcal_to_kj(kcal)
    lines = [f"**{title}** — per 100 g:"]
    if kj is not None:
        lines.append(f"• Energy: **{kj:,.0f} kJ** ({kcal:,.0f} cal)")
    if top.protein_per_100g is not None:
        lines.append(f"• Protein: **{top.protein_per_100g:g} g**")
    if top.serving_g:
        lines.append(f"• Stated serving: {top.serving_g:g} g")
    if kj is not None:
        amounts = [f"{kj:.0f}kj"]
        if top.protein_per_100g is not None:
            amounts.append(f"{top.protein_per_100g:g}p")
        serving = f"{top.serving_g:g}" if top.serving_g else "60"
        lines.append(
            f"\nLog it by posting the label values plus what you ate: "
            f"`{' '.join(amounts)} {serving}g`. Swap in your own grams — "
            "no dividing by 100."
        )
    if len(results) > 1:
        alts = ", ".join(
            r.name if r.brand is None else f"{r.name} ({r.brand})"
            for r in results[1:4]
        )
        lines.append(f"\nOther matches: {alts}")

    if save is not None and save.strip():
        norm = calories.normalize_food(save)
        if norm and kcal is not None and 0 < kcal <= _MAX_ENTRY_KCAL:
            db.calorie_food_set(
                _ctx_guild_id(interaction), interaction.user.id, norm,
                save.strip(), kcal, top.protein_per_100g,
            )
            lines.append(
                f"\n🍴 Saved **{save.strip()}** = "
                f"{calories.format_kcal(kcal)} per serving — note the serving "
                "is **100 g**, so `2 " + norm + "` in chat logs 200 g."
            )
        else:
            lines.append(
                "\n(Couldn't save it — no usable calorie value on the record.)"
            )
    lines.append("\n*Data: Open Food Facts (crowdsourced — sanity-check it).*")
    await interaction.followup.send(
        "\n".join(lines), allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="estimate",
    description="AI-estimate calories + protein from words or a photo, and log it.",
)
@app_commands.describe(
    description="What you ate, e.g. 'large Big Mac meal'. Optional when you attach a photo.",
    photo="A photo of the food, or of its nutrition panel.",
    grams="For a nutrition-panel photo: how many grams you ate.",
    day='Backdate it: "yesterday", "monday", "3 days ago", or "2026-06-28".',
)
async def estimate_cmd(
    interaction: discord.Interaction,
    description: str | None = None,
    photo: discord.Attachment | None = None,
    grams: app_commands.Range[float, 1, 2000] | None = None,
    day: str | None = None,
) -> None:
    """One AI entry point for both macros, from either words or a picture.

    This replaced `/calories estimate` and `/calories label`, which split the
    same job by input type and made you know in advance which one your photo
    would turn out to be — a packet's panel and a plate of food are the same
    question ("what did I just eat?") and now take the same command. The photo
    branch lets the model decide which it's looking at.
    """
    if description is None and photo is None:
        await interaction.response.send_message(
            "Describe what you ate, attach a photo of it, or both — e.g. "
            "`/estimate description: large Big Mac meal`.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    goal = db.calorie_goal_get(guild_id, interaction.user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(), ephemeral=True,
        )
        return
    if not gemini_client.available():
        await interaction.response.send_message(
            "🤖 AI features aren't configured on this bot.", ephemeral=True,
        )
        return
    logged_at, day_ok = _slash_logged_at(day)
    if not day_ok:
        await interaction.response.send_message(_BAD_DAY_MSG, ephemeral=True)
        return
    if photo is not None:
        problem = _photo_problem(photo)
        if problem is not None:
            await interaction.response.send_message(problem, ephemeral=True)
            return

    await interaction.response.defer(thinking=True)
    described = (description or "").strip()
    if photo is not None:
        try:
            blob = await photo.read()
        except discord.HTTPException:
            await interaction.followup.send("Couldn't download that attachment.")
            return
        mime = (photo.content_type or "").split(";")[0].strip().lower()
        result = await _ai_read_photo(mime, blob, described or None)
        raw = f"photo {photo.filename}"[:80]
    else:
        result = await _ai_estimate_meal(described)
        raw = described[:80]

    if isinstance(result, gemini_client.GeminiError):
        # The card carries Google's own wording in a "For admins" field when
        # the fault is configuration, so the owner doesn't have to go digging
        # through container logs to find out the key was rejected.
        await interaction.followup.send(embed=_ai_error_embed(result))
        return
    if isinstance(result, str):
        await interaction.followup.send(_estimate_failed(result))
        return

    if isinstance(result, ai_food.LabelInfo):
        # The photo was a packet. Per-100g values need a serving before they
        # mean anything, so `grams:` logs it and its absence just transcribes.
        out = _photo_label_outcome(
            guild_id, interaction.user, result,
            serving_g=grams, logged_at=logged_at, raw=raw,
        )
        msg = await interaction.followup.send("\n".join(out.lines))
        _remember_reply(
            msg, interaction.user.id, calorie_id=out.calorie_id,
            protein_id=out.protein_id, headline=out.headline,
            footnote=out.footnote,
        )
        await _attach_undo(
            None, calorie_id=out.calorie_id, protein_id=out.protein_id,
            message=msg,
        )
        return

    if not (0 < result.kcal <= _MAX_AI_ESTIMATE_KCAL):
        await interaction.followup.send(
            f"🤖 The AI guessed {calories.format_kcal(result.kcal)} — outside "
            "what I'll auto-log. Log it manually if it's real."
        )
        return
    label = result.name or described[:60] or "AI estimate"
    cal_id, pro_id, parts, status = _store_ai_nutrition(
        guild_id, interaction.user, result.kcal, result.protein_g or 0.0,
        note=f"{label} (AI estimate)", raw=raw, logged_at=logged_at,
    )
    conf = f" · confidence: {result.confidence}" if result.confidence else ""
    headline = (
        f"🤖 Estimated **{label}** ≈ {' + '.join(parts)}{conf}"
        f"{_backdate_label(logged_at)}"
    )
    footnote = ui.subtext(
        "AI estimate — react ❌ to remove, `/calories edit` to correct."
    )
    msg = await interaction.followup.send(
        "\n".join([headline, *status, footnote])
    )
    _remember_reply(
        msg, interaction.user.id, calorie_id=cal_id, protein_id=pro_id,
        headline=headline, footnote=footnote,
    )
    # The entries were inserted without a message_id — a followup's id isn't
    # known until it's sent — so link them now, which is what lets a ❌ on this
    # reply find them.
    await _attach_undo(None, calorie_id=cal_id, protein_id=pro_id, message=msg)


@calories_group.command(
    name="meal_set",
    description="Bundle saved foods into a meal you can log with one word.",
)
@app_commands.describe(
    name="Meal name, e.g. breakfast.",
    foods='Comma-separated saved foods, e.g. "coffee, 2x oats, protein shake".',
)
async def calories_meal_set_cmd(
    interaction: discord.Interaction, name: str, foods: str,
) -> None:
    norm = calories.normalize_food(name)
    if not norm:
        await interaction.response.send_message(
            "Give the meal a name, e.g. `/calories meal_set breakfast "
            "coffee, oats`.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    if db.calorie_food_get(guild_id, interaction.user.id, norm) is not None:
        await interaction.response.send_message(
            f"You already have a saved *food* called **{name.strip()}** — "
            "pick a different meal name so chat logging stays unambiguous.",
            ephemeral=True,
        )
        return
    items = calories.parse_meal_items(foods)
    if items is None:
        await interaction.response.send_message(
            "Couldn't read that list — use comma-separated saved foods like "
            "`coffee, 2x oats, protein shake` (max 12).", ephemeral=True,
        )
        return
    resolved: list[tuple[int, sqlite3.Row]] = []
    unknown: list[str] = []
    for servings, food_name in items:
        row = db.calorie_food_get(guild_id, interaction.user.id, food_name)
        if row is None:
            unknown.append(food_name)
        else:
            resolved.append((servings, row))
    if unknown:
        await interaction.response.send_message(
            f"Not saved food(s): **{', '.join(unknown)}** — define them with "
            "`/calories food_set` first (see `/calories food_list`).",
            ephemeral=True,
        )
        return
    db.calorie_meal_set(interaction.user.id, norm, name.strip(), items)
    kcal, grams = _meal_totals(resolved)
    macro = calories.format_kcal(kcal)
    if grams > 0:
        macro += f" + {protein_mod.format_grams(grams)} protein"
    pieces = ", ".join(
        (f"{n}× " if n > 1 else "") + row["display"] for n, row in resolved
    )
    await interaction.response.send_message(
        f"{ui.OK} Saved meal **{name.strip()}** = {pieces} ({macro} right now — "
        f"it re-reads the foods each time you log it).\n"
        f"Log it by typing `{norm}` in chat."
    )


@calories_group.command(
    name="meal_list", description="List your saved meals.",
)
async def calories_meal_list_cmd(interaction: discord.Interaction) -> None:
    rows = db.calorie_meal_list(interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You haven't saved any meals yet. Bundle saved foods with "
            "`/calories meal_set <name> <foods>`.", ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    lines = []
    for r in rows:
        meal = db.calorie_meal_get(interaction.user.id, r["name"])
        if meal is None:
            continue
        _display, items = meal
        resolved = []
        missing = 0
        for servings, food_name in items:
            food = db.calorie_food_get(guild_id, interaction.user.id, food_name)
            if food is None:
                missing += 1
            else:
                resolved.append((servings, food))
        kcal, grams = _meal_totals(resolved)
        macro = calories.format_kcal(kcal)
        if grams > 0:
            macro += f" · {protein_mod.format_grams(grams)} protein"
        pieces = ", ".join(
            (f"{n}× " if n > 1 else "") + food["display"]
            for n, food in resolved
        )
        line = f"• **{r['display']}** — {pieces} ({macro})"
        if missing:
            line += f" ⚠️ {missing} deleted food(s)"
        lines.append(line)
    embed = discord.Embed(
        title=f"{ui.FOOD} Your saved meals",
        description="\n".join(lines)[:4000],
        colour=EMBED_COLOUR,
    )
    embed.set_footer(text="Log one by typing its name in chat, e.g. breakfast")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@calories_group.command(
    name="meal_remove", description="Delete one of your saved meals.",
)
@app_commands.describe(name="The saved meal to remove.")
async def calories_meal_remove_cmd(
    interaction: discord.Interaction, name: str,
) -> None:
    norm = calories.normalize_food(name)
    removed = db.calorie_meal_remove(interaction.user.id, norm)
    msg = (
        f"🗑️ Removed meal **{name.strip()}**." if removed
        else f"No saved meal called **{name.strip()}**. "
        "See `/calories meal_list`."
    )
    await interaction.response.send_message(msg, ephemeral=True)


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    """Parse a reminder time: '20:30', '8pm', '8:30pm', '20'. None if bad."""
    m = re.match(
        r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", text, re.IGNORECASE,
    )
    if m is None:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


@calories_group.command(
    name="remind",
    description="Evening DM if you haven't logged calories that day (streak saver).",
)
@app_commands.describe(
    time='When to nudge you, e.g. "20:30" or "8pm" (default 20:00, bot-local time).',
    off="Turn the reminder off.",
)
async def calories_remind_cmd(
    interaction: discord.Interaction,
    time: str | None = None,
    off: bool = False,
) -> None:
    if off:
        removed = db.calorie_reminder_remove(interaction.user.id)
        await interaction.response.send_message(
            "🔕 Streak-saver reminder off." if removed
            else "You didn't have a reminder set.",
            ephemeral=True,
        )
        return
    guild_id = _ctx_guild_id(interaction)
    if db.calorie_goal_get(guild_id, interaction.user.id) is None:
        await interaction.response.send_message(
            embed=_calories_not_set_embed(), ephemeral=True,
        )
        return
    hhmm = _parse_hhmm(time) if time is not None else (20, 0)
    if hhmm is None:
        await interaction.response.send_message(
            "Couldn't read that time — try `20:30` or `8pm`.", ephemeral=True,
        )
        return
    hour, minute = hhmm
    db.calorie_reminder_set(interaction.user.id, hour, minute)
    await interaction.response.send_message(
        f"🔔 Streak saver on — if you haven't logged anything by "
        f"**{hour:02d}:{minute:02d}** ({_tz_name}) I'll DM you a nudge. "
        "Make sure DMs from this server are open. `/calories remind off:true` "
        "turns it off.",
        ephemeral=True,
    )


bot.tree.add_command(calories_group)


# ---------------------------------------------------------------------------
# Protein tracking (/protein ...) — optional daily-ceiling tracker.
# ---------------------------------------------------------------------------

protein_group = app_commands.Group(
    name="protein",
    description="Track daily protein (grams) against a personal daily max.",
)

_MAX_PROTEIN_TARGET_G = 500
_MAX_PROTEIN_ENTRY_G = 400

def _protein_not_set_embed(name: str | None = None) -> discord.Embed:
    if name is not None:
        return ui.empty(f"{_safe_label(name, limit=32)} isn't tracking protein")
    return ui.empty(
        "You're not tracking protein yet",
        hint="Set a daily max and I'll flag when you go over it — e.g. `180`.",
        cmd="/protein setup <grams> to start",
    )


def _protein_week_days(
    guild_id: int, user_id: int, start_iso: str, end_iso: str,
) -> dict[str, float]:
    """Per-local-day gram totals within the window, keyed YYYY-MM-DD."""
    days: dict[str, float] = {}
    for row in db.protein_entries_between(guild_id, user_id, start_iso, end_iso):
        dt = datetime.fromisoformat(row["logged_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(DISPLAY_TZ).date().isoformat()
        days[day] = days.get(day, 0.0) + float(row["grams"])
    return days


# Words that, in the ``target`` slot, mean "tie my protein max to my
# bodyweight" instead of a fixed number. Case-insensitive.
_PROTEIN_BW_WORDS = {"bodyweight", "bw"}


@protein_group.command(
    name="setup", description="Set your daily protein max (grams).",
)
@app_commands.describe(
    target='Daily max in grams (e.g. "180"), or "bodyweight" to tie it to your weight (1 g/kg).',
    weekend='Optional different max for Sat/Sun, e.g. "200". "same" clears it.',
)
async def protein_setup_cmd(
    interaction: discord.Interaction, target: str,
    weekend: str | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    # "bodyweight"/"bw" ties the ceiling to the scale at 1 g/kg, updating itself
    # on every weigh-in. A weekend override contradicts "same number every day",
    # so refuse it here rather than silently linking and dropping their input.
    if target.strip().lower() in _PROTEIN_BW_WORDS:
        if weekend is not None:
            await interaction.response.send_message(
                "A bodyweight-linked max already applies every day, so it can't "
                "take a separate weekend number. Run `/protein setup "
                "target:bodyweight` on its own, or set a fixed number if you "
                "want a weekend split.",
                ephemeral=True,
            )
            return
        row = db.get_latest_bodyweight(guild_id, interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "I don't have a bodyweight on file for you yet — log one first "
                "with `/bodyweight` or by typing `bw 80kg` in chat, then run "
                "`/protein setup target:bodyweight`.",
                ephemeral=True,
            )
            return
        weight = float(row["weight_kg"])
        grams = round(weight)
        # Neutralise a live weekend protein override so the derived number is
        # what resolves on Saturdays too; leave weekend untouched otherwise.
        _wd, we = _band_targets(interaction.user.id)
        weekend_g = None if we.protein.split else KEEP
        db.protein_bw_link_set(
            interaction.user.id, _display_name(interaction.user),
        )
        db.protein_goal_set(
            guild_id, interaction.user.id, _display_name(interaction.user),
            grams, weekend_g,
        )
        await interaction.response.send_message(
            f"🥩 Protein max tied to your bodyweight: **{weight:g} kg → "
            f"{grams} g/day**, every day. Log a new bodyweight any time "
            "(`/bodyweight` or `bw 80kg`) and this updates itself.\n"
            "Switch back to a fixed number any time with `/protein setup "
            "<grams>`."
        )
        return

    grams = protein_mod.parse_protein_amount(target)
    if grams is None or grams <= 0:
        await interaction.response.send_message(
            "Couldn't read that — give a number of grams (e.g. `180`) or "
            "`bodyweight` to tie it to your weight.",
            ephemeral=True,
        )
        return
    if grams > _MAX_PROTEIN_TARGET_G:
        await interaction.response.send_message(
            f"That's over {_MAX_PROTEIN_TARGET_G}g/day — looks like a typo.",
            ephemeral=True,
        )
        return
    weekend_g, err = _parse_weekend_option(
        weekend, protein_mod.parse_protein_amount, _MAX_PROTEIN_TARGET_G,
    )
    if err:
        await interaction.response.send_message(
            f"{err} Try `200`, or `same` to drop it.", ephemeral=True,
        )
        return
    # A plain number is an explicit fixed target: break any bodyweight link so a
    # later weigh-in doesn't quietly overwrite the number they just chose.
    was_linked = db.protein_bw_link_remove(interaction.user.id)
    db.protein_goal_set(
        guild_id, interaction.user.id, _display_name(interaction.user), grams,
        weekend_g,
    )
    wd, we = _band_targets(interaction.user.id)
    if wd.protein.split:
        head = (
            f"🥩 Protein maxes set — weekdays "
            f"**{protein_mod.format_grams(wd.protein.value or 0.0)}**, weekends "
            f"**{protein_mod.format_grams(we.protein.value or 0.0)}**."
        )
    else:
        head = (
            f"🥩 Daily protein max set to "
            f"**{protein_mod.format_grams(grams)}**, every day."
        )
    unlink_note = (
        "\n🔗 Unlinked from bodyweight — this is now a fixed number."
        if was_linked else ""
    )
    await interaction.response.send_message(
        f"{head}{unlink_note}\n"
        "Log it with `/protein add <grams>` or just type `40p` in chat, and "
        "check in with `/today`. I'll flag when you go over."
    )


@protein_group.command(
    name="add", description="Log protein you just ate (grams).",
)
@app_commands.describe(
    grams='Grams of protein, e.g. "40".',
    note="What it was (optional, shows in /today).",
    day='Backdate it: "yesterday", "monday", "3 days ago", or "2026-06-28".',
)
async def protein_add_cmd(
    interaction: discord.Interaction, grams: str, note: str | None = None,
    day: str | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    goal = db.protein_goal_get(guild_id, interaction.user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_protein_not_set_embed(), ephemeral=True,
        )
        return
    logged_at, day_ok = _slash_logged_at(day)
    if not day_ok:
        await interaction.response.send_message(_BAD_DAY_MSG, ephemeral=True)
        return
    amount = protein_mod.parse_protein_amount(grams)
    if amount is None or amount <= 0:
        await interaction.response.send_message(
            "Couldn't read that — give a number of grams, e.g. `40`.",
            ephemeral=True,
        )
        return
    if amount > _MAX_PROTEIN_ENTRY_G:
        await interaction.response.send_message(
            f"That's over {_MAX_PROTEIN_ENTRY_G}g in one entry — looks like a typo.",
            ephemeral=True,
        )
        return
    pro_id = db.protein_add(
        guild_id, interaction.user.id, _display_name(interaction.user),
        amount, note=note, raw=grams.strip(), logged_at=logged_at,
    )
    total, _n = db.protein_total_between(
        guild_id, interaction.user.id, *_day_window_for(logged_at),
    )
    note_part = f" — {note}" if note else ""
    await interaction.response.send_message(
        f"{ui.PROTEIN} Logged **{protein_mod.format_grams(amount)}** "
        f"protein{note_part}"
        f"{_backdate_label(logged_at)}\n"
        + _protein_status_for(interaction.user.id, total, logged_at)
        + _streak_suffix(_protein_streak(interaction.user.id))
        + "\n" + ui.subtext("react ❌ to remove")
    )
    await _attach_undo(interaction, protein_id=pro_id)


@protein_group.command(
    name="week", description="Per-day protein totals for the last 7 days.",
)
@app_commands.describe(user="The member to look up (defaults to you).")
async def protein_week_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    if await _deny_channel_outsider(interaction, target_user):
        return
    guild_id = _ctx_guild_id(interaction)
    goal = db.protein_goal_get(guild_id, target_user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_protein_not_set_embed(
                None if target_user == interaction.user
                else target_user.display_name
            ),
            ephemeral=True,
        )
        return
    _label, start_iso, end_iso = _week_window()
    day_totals = _protein_week_days(guild_id, target_user.id, start_iso, end_iso)
    today = datetime.now(DISPLAY_TZ).date()
    week = [today - timedelta(days=n) for n in range(6, -1, -1)]
    # Each day against the ceiling that was in force on that day.
    day_targets = targets_mod.resolve_days(
        db.nutrition_target_rows(target_user.id), week,
    )
    # Same diverging chart as /calories week — see the note there. It matters
    # more here: this tracker exists to catch going *over*, and a clamped
    # progress bar drew a 5 g overshoot and an 86 g one identically.
    rows: list[tuple[str, float | None, float]] = []
    logged_days = 0
    target_sum = 0.0
    for day in week:
        key = day.isoformat()
        day_name = _WEEKDAY_NAMES[day.weekday()][:3]
        target_g = day_targets[day].protein.value or 0.0
        total = day_totals.get(key)
        if total is not None:
            logged_days += 1
            target_sum += target_g
        rows.append((day_name, total, target_g))
    # The legend lives outside the fence — see ui.diverging: an ASCII header
    # can't line up with glyph-padded bar rows.
    if not logged_days:
        await interaction.response.send_message(
            embed=_nothing_logged_embed(
                target_user, interaction.user, "protein",
                how="`40p` in chat, or `/protein add`",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    lines = [
        ui.diverging(rows, protein_mod.format_grams),
        ui.subtext("left of the line = under your max · right = over"),
    ]
    if logged_days:
        avg = sum(day_totals.values()) / logged_days
        lines.append(
            f"Avg on logged days: **{protein_mod.format_grams(avg)}** vs "
            f"max {protein_mod.format_grams(target_sum / logged_days)}"
        )
        lines.extend(_band_breakdown_lines(
            day_totals, day_targets, protein_mod.format_grams,
            targets_mod.MACRO_PROTEIN, noun="max",
        ))
    streak = _protein_streak(target_user.id)
    embed = ui.card(
        f"{ui.PROTEIN} Protein this week",
        description="\n".join(lines),
        colour=ui.score_ceiling(
            sum(day_totals.values()) / logged_days if logged_days else 0.0,
            target_sum / logged_days if logged_days else 0.0,
        ),
        member=target_user,
        footer=(
            f"{ui.STREAK} {ui.plural(streak, 'day')} logging streak"
            if streak >= 2 else None
        ),
    )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


@protein_group.command(
    name="edit", description="Fix the amount of your most recent protein entry.",
)
@app_commands.describe(
    grams='Corrected grams of protein, e.g. "40".',
    note="Optional new note (leave blank to keep the old one).",
)
async def protein_edit_cmd(
    interaction: discord.Interaction, grams: str, note: str | None = None,
) -> None:
    guild_id = _ctx_guild_id(interaction)
    goal = db.protein_goal_get(guild_id, interaction.user.id)
    if goal is None:
        await interaction.response.send_message(
            embed=_protein_not_set_embed(), ephemeral=True,
        )
        return
    amount = protein_mod.parse_protein_amount(grams)
    if amount is None or amount <= 0:
        await interaction.response.send_message(
            "Couldn't read that — give a number of grams, e.g. `40`.",
            ephemeral=True,
        )
        return
    if amount > _MAX_PROTEIN_ENTRY_G:
        await interaction.response.send_message(
            f"That's over {_MAX_PROTEIN_ENTRY_G}g in one entry — looks like a typo.",
            ephemeral=True,
        )
        return
    old = db.protein_update_last(
        guild_id, interaction.user.id, amount, note=note, raw=grams.strip(),
        username=_display_name(interaction.user),
    )
    if old is None:
        await interaction.response.send_message(
            "No protein entries to edit yet.", ephemeral=True,
        )
        return
    total, _n = db.protein_total_between(
        guild_id, interaction.user.id, *_today_window(),
    )
    await interaction.response.send_message(
        f"✏️ Updated last entry "
        f"**{protein_mod.format_grams(float(old['grams']))}** → "
        f"**{protein_mod.format_grams(amount)}** protein\n"
        + _protein_status_for(interaction.user.id, total)
    )
    await _restate_day_replies(
        interaction.user, logged_at=_parse_iso(old["logged_at"]),
        guild_id=guild_id,
    )


@protein_group.command(
    name="stop", description="Stop tracking protein (your history is kept).",
)
async def protein_stop_cmd(interaction: discord.Interaction) -> None:
    guild_id = _ctx_guild_id(interaction)
    # Drop any bodyweight link too, otherwise the next weigh-in would silently
    # re-arm a target the user just turned off.
    db.protein_bw_link_remove(interaction.user.id)
    removed = db.protein_goal_remove(guild_id, interaction.user.id)
    if not removed:
        await interaction.response.send_message(
            "You weren't tracking protein.", ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "🥩 Protein tracking stopped — your history is kept, and "
        "`/protein setup` re-enables it any time."
    )


bot.tree.add_command(protein_group)


# ---------------------------------------------------------------------------
# /today — the day's nutrition, both macros, one card
# ---------------------------------------------------------------------------
# Top-level rather than under either group because it belongs to neither.
# "How's my day going?" is one question, and answering it used to mean running
# /calories today *and* /protein today and reading two cards side by side —
# after first guessing which group the answer lived under. Defined below both
# groups so every per-macro helper it borrows is already in scope.

#: Severity order for a card that scores more than one macro at once. The day
#: is only as good as its worst macro, so a breached protein ceiling (DANGER)
#: has to outrank calories sitting neatly on target (SUCCESS) — otherwise the
#: rail goes green on the exact day the tracker exists to catch.
_STATUS_SEVERITY = (ui.SUCCESS, ui.BRAND, ui.WARNING, ui.DANGER)


def _worst_colour(colours: Sequence[discord.Colour]) -> discord.Colour:
    """The most alarming colour in ``colours`` — see :data:`_STATUS_SEVERITY`.

    An unranked colour sorts lowest rather than raising: a new scorer colour
    should quietly not win the card, not break the command.
    """
    return max(
        colours,
        key=lambda c: _STATUS_SEVERITY.index(c) if c in _STATUS_SEVERITY else 0,
        default=ui.BRAND,
    )


def _nutrition_not_set_embed(name: str | None = None) -> discord.Embed:
    """Neither macro tracked. Neither per-macro not-set embed can serve here:
    picking one would tell a would-be protein tracker to set calories."""
    if name is not None:
        return ui.empty(f"{_safe_label(name, limit=32)} isn't tracking nutrition")
    return ui.empty(
        "You're not tracking calories or protein yet",
        hint="Set a daily calorie target, a daily protein max, or both — "
             "`/today` then scores the day against whichever you keep.",
        cmd="/calories setup <target> · /protein setup <grams>",
    )


def _today_entry_table(entries: Sequence[sqlite3.Row], column: str, fmt) -> str:
    """The day's entries as one fence so times and amounts line up. The heaviest
    logger here averages 11 entries a day and peaks at 17, so this list is
    routinely long enough for alignment to matter.

    One fence per macro, never a merged one: the amounts are in different units,
    so a shared table would need a unit column on every row.
    """
    rows = []
    for r in entries:
        dt = datetime.fromisoformat(r["logged_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        rows.append([
            dt.astimezone(DISPLAY_TZ).strftime("%H:%M"),
            fmt(float(r[column])),
            _safe_label(r["note"] or "", limit=24) if r["note"] else "",
        ])
    return ui.table(rows, align="<>", max_rows=20)


@bot.tree.command(
    name="today",
    description="Today's calories and protein against your daily targets.",
)
@app_commands.describe(user="The member to look up (defaults to you).")
async def today_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
) -> None:
    target_user = user or interaction.user
    if await _deny_invisible_target(interaction, target_user):
        return
    if await _deny_channel_outsider(interaction, target_user):
        return
    guild_id = _ctx_guild_id(interaction)
    cal_goal = db.calorie_goal_get(guild_id, target_user.id)
    pro_goal = db.protein_goal_get(guild_id, target_user.id)
    if cal_goal is None and pro_goal is None:
        await interaction.response.send_message(
            embed=_nutrition_not_set_embed(
                None if target_user == interaction.user
                else target_user.display_name
            ),
            ephemeral=True,
        )
        return

    start_iso, end_iso = _today_window()
    # One section per macro the member actually keeps. An untracked macro
    # contributes nothing at all — a half-card reading "0 g / 0 g" looks like a
    # broken tracker rather than an optional feature left switched off, and
    # protein being off is the common case.
    sections: list[tuple[str, str, str, str]] = []
    colours: list[discord.Colour] = []
    streaks: list[tuple[str, int]] = []

    if cal_goal is not None:
        entries = db.calorie_entries_between(
            guild_id, target_user.id, start_iso, end_iso,
        )
        total = sum(float(r["kcal"]) for r in entries)
        status, colour = _calorie_status(
            total, float(cal_goal["daily_target_kcal"]), cal_goal["label"],
        )
        colours.append(colour)
        sections.append((
            ui.FOOD, "Calories", status,
            _today_entry_table(entries, "kcal", calories.format_kcal),
        ))
        streaks.append(("calorie", _calorie_streak(target_user.id)))

    if pro_goal is not None:
        entries = db.protein_entries_between(
            guild_id, target_user.id, start_iso, end_iso,
        )
        total = sum(float(r["grams"]) for r in entries)
        status, colour = _protein_status(
            total, float(pro_goal["daily_target_g"]), pro_goal["label"],
        )
        colours.append(colour)
        sections.append((
            ui.PROTEIN, "Protein", status,
            _today_entry_table(entries, "grams", protein_mod.format_grams),
        ))
        streaks.append(("protein", _protein_streak(target_user.id)))

    # The two streaks are independent — calories logged every day and protein
    # logged sporadically is a normal pattern — so they're named whenever both
    # macros are on. One macro keeps the older unqualified wording.
    named = len(streaks) > 1
    footer = " · ".join(
        f"{ui.STREAK} {ui.plural(n, 'day')} "
        f"{name if named else 'logging'} streak"
        for name, n in streaks if n >= 2
    )
    # No card title: Discord already shows "used /today" above the reply, so a
    # "🍎 Today" headline just says it again and pushes the actual figures down.
    # The first section heading (🍎 Calories) carries the card instead.
    embed = ui.card(
        colour=_worst_colour(colours),
        member=target_user,
        footer=footer or None,
    )
    for icon, heading, status, entry_table in sections:
        ui.block(embed, f"{icon} {heading}", status)
        # Meter and entries stay in separate fields: together they can pass the
        # 1024-char field limit, and ui.fit would then clip mid-fence and leave
        # an unclosed ``` that eats the rest of the card. The icon repeats on
        # the entries heading because with both macros on, four stacked fields
        # make "Entries" alone ambiguous about which list it belongs to.
        ui.block(
            embed, f"{icon} Entries",
            entry_table or "Nothing logged yet today.",
        )
    await interaction.response.send_message(
        embed=embed, allowed_mentions=discord.AllowedMentions.none(),
    )


# Exit codes the supervisor reads to explain a stopped bot in plain English
# instead of showing an opaque crash loop. Kept in sync with app/supervisor.py.
EX_INTENT = 76   # a privileged intent is requested but not enabled
EX_AUTH = 77     # Discord rejected the token
EX_CONFIG = 78   # nothing to run yet — an idle state, not a failure


def _finish_bot_shutdown(
    loop: asyncio.AbstractEventLoop,
    shutdown_task: asyncio.Task | None,
) -> None:
    """Finish Discord/aiohttp teardown and drain straggling loop tasks."""

    if shutdown_task is None:
        shutdown_task = loop.create_task(bot.close())
    with contextlib.suppress(Exception):
        loop.run_until_complete(shutdown_task)

    # Defensive final sweep for Discord/aiohttp implementation tasks not owned
    # by Client.close. Cancel and *drain* them so their finally blocks run while
    # call_soon is still legal.
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        with contextlib.suppress(Exception):
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True),
            )
    with contextlib.suppress(Exception):
        loop.run_until_complete(loop.shutdown_asyncgens())
    with contextlib.suppress(Exception):
        loop.run_until_complete(loop.shutdown_default_executor())


def main() -> None:
    """Run the bot, mapping fatal startup problems onto explanatory exit codes.

    Also installs the SIGTERM handler this process has never had. Without it,
    `docker stop` (and every supervisor-initiated restart) waited out the grace
    period and then SIGKILLed, skipping the Strava/RPC runner cleanup and the
    database close, and leaving WAL recovery to the next open.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_task: asyncio.Task | None = None

    def _terminate() -> None:
        nonlocal shutdown_task
        # bot.close is monkeypatched above to also clean up the aiohttp
        # runners and scheduled loops. Keep the task so the outer finally block
        # can await it; bot.start() may return while Client.close is still
        # draining Discord's aiohttp session.
        if shutdown_task is None:
            shutdown_task = loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _terminate)

    try:
        loop.run_until_complete(bot.start(TOKEN))
    except discord.LoginFailure:
        LOG.error(
            "Discord rejected the bot token. Paste a fresh one from the "
            "Developer Portal into the dashboard's Settings tab."
        )
        raise SystemExit(EX_AUTH)
    except discord.PrivilegedIntentsRequired:
        LOG.error(
            "Discord refused the gateway: this bot requests a privileged "
            "intent that is not enabled for the application. Open "
            "https://discord.com/developers/applications -> your app -> Bot -> "
            "Privileged Gateway Intents and turn on the ones named above, or "
            "turn off Presence tracking / Mirror members in the dashboard."
        )
        raise SystemExit(EX_INTENT)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        # Whether shutdown came from SIGTERM, Ctrl+C, a startup exception or a
        # normal gateway return, finish Client.close before closing its loop.
        # The old fire-and-forget signal task was destroyed mid-await, leaving
        # the aiohttp session and every discord.ext.tasks loop pending.
        _finish_bot_shutdown(loop, shutdown_task)
        with contextlib.suppress(Exception):
            db.close()
        loop.close()


if __name__ == "__main__":
    main()
