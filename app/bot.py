"""Gym tracking Discord bot.

Auto-detects gym posts in configured channels, parses lifts, and stores them
in SQLite. Exposes slash commands for querying stats, progress, and
leaderboards.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone

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
from .parser import Lift, parse_message
from . import __version__

load_dotenv()

LOG = logging.getLogger("gymbot")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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

# Bot "accent" colour for embeds.
EMBED_COLOUR = discord.Colour.from_str("#f26522")

db = Database(DB_PATH)

intents = discord.Intents.default()
intents.message_content = True
intents.members = False
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
    for l in lifts:
        if l.weight_kg <= 0:
            continue
        prev = db.previous_best(guild_id, user_id, l.equipment)
        if prev is None or l.weight_kg > prev:
            prs.append((l, prev))
    return prs


async def _store_lifts(
    message: discord.Message, lifts: list[Lift]
) -> int:
    return db.add_lifts(
        guild_id=message.guild.id if message.guild else 0,
        user_id=message.author.id,
        username=message.author.display_name,
        lifts=lifts,
        message_id=message.id,
        channel_id=message.channel.id,
        logged_at=message.created_at.astimezone(timezone.utc),
    )


@bot.event
async def on_ready() -> None:
    LOG.info(
        "Logged in as %s (id=%s) — gym-bot v%s",
        bot.user, bot.user.id if bot.user else "?", __version__,
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


_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


# The loop fires once every 24 hours at REMINDER_HOUR:REMINDER_MINUTE in
# DISPLAY_TZ; we then check the weekday inside the task so a single loop
# definition suffices regardless of which day the user configures.
def _reminder_time() -> dtime:
    hh = max(0, min(23, REMINDER_HOUR))
    mm = max(0, min(59, REMINDER_MINUTE))
    return dtime(hour=hh, minute=mm, tzinfo=DISPLAY_TZ)


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


async def _backfill_channel(
    channel: discord.abc.Messageable, limit: int | None
) -> tuple[int, int, int]:
    """Scan a channel's history and store any detected lifts.

    Returns (messages_scanned, messages_with_lifts, lifts_inserted).
    Dedupe on (message_id, equipment) means re-running is safe.
    """
    scanned = matched = inserted = 0
    async for msg in channel.history(limit=limit, oldest_first=True):
        if msg.author.bot or not msg.guild:
            continue
        scanned += 1
        lifts = parse_message(msg.content)
        if not lifts:
            continue
        if len(lifts) < MIN_LIFTS_FOR_AUTO and not any(l.confident for l in lifts):
            continue
        n = await _store_lifts(msg, lifts)
        if n:
            matched += 1
            inserted += n
    return scanned, matched, inserted


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
            scanned, matched, inserted = await _backfill_channel(channel, limit)
        except discord.Forbidden:
            LOG.warning("Backfill: missing permission to read #%s", channel)
            continue
        LOG.info(
            "Backfill done for #%s: scanned=%d, posts_with_lifts=%d, new_lifts=%d",
            channel, scanned, matched, inserted,
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    if GYM_CHANNEL_IDS and message.channel.id not in GYM_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    lifts = parse_message(message.content)
    # Auto-store when either:
    #  * the message is a clear "stats dump" (>= MIN_LIFTS_FOR_AUTO lifts), or
    #  * at least one lift was parsed with an explicit unit (kg / plates / BW+),
    #    which is a strong enough signal on its own (e.g. "Bench 100kg today").
    should_store = len(lifts) >= MIN_LIFTS_FOR_AUTO or any(
        l.confident for l in lifts
    )
    if lifts and should_store:
        # Detect PRs BEFORE inserting, so we can compare against the prior state.
        guild_id = message.guild.id if message.guild else 0
        prs = _new_prs_for_lifts(guild_id, message.author.id, lifts)

        inserted = await _store_lifts(message, lifts)
        if inserted > 0:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            # Check goal hits (PRs that meet or beat the user's goal).
            goal_hits = _check_goal_hits(guild_id, message.author.id, prs)

            # Reply with a short confirmation so the user can see exactly
            # what the bot understood from their message.
            try:
                if len(lifts) == 1:
                    l = lifts[0]
                    reply = (
                        f"Added **{_format_weight(l.weight_kg, l.bodyweight_add)}** "
                        f"to **{l.equipment}**."
                    )
                else:
                    lines = [f"Added {inserted} lift(s):"]
                    for l in lifts:
                        lines.append(
                            f"• **{l.equipment}** — "
                            f"{_format_weight(l.weight_kg, l.bodyweight_add)}"
                        )
                    reply = "\n".join(lines)
                if prs:
                    pr_lines = ["", "🎉 **New PR!**"]
                    for l, prev in prs:
                        if prev is None:
                            pr_lines.append(
                                f"• **{l.equipment}**: first logged at "
                                f"{_format_weight(l.weight_kg, l.bodyweight_add)}"
                            )
                        else:
                            gain = l.weight_kg - prev
                            pr_lines.append(
                                f"• **{l.equipment}**: "
                                f"{_format_weight(prev, l.bodyweight_add)} → "
                                f"{_format_weight(l.weight_kg, l.bodyweight_add)} "
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
                reply += (
                    "\n-# React ❌ to this reply if I got it wrong — "
                    "only you can undo your own entry."
                )
                sent = await message.reply(reply, mention_author=False)
                try:
                    db.track_reply(
                        reply_message_id=sent.id,
                        guild_id=guild_id,
                        user_id=message.author.id,
                        message_id=message.id,
                        lift_ids=None,
                    )
                except Exception:  # pragma: no cover - non-critical
                    LOG.exception("Failed to track reply for undo")
            except discord.HTTPException:
                pass
            LOG.info(
                "Stored %d lifts from %s in #%s",
                inserted, message.author, message.channel,
            )
        else:
            # Lifts were detected but every one was a duplicate — give a quiet
            # signal so the author knows the bot saw it but didn't re-store.
            try:
                await message.add_reaction("🔁")
            except discord.HTTPException:
                pass

    await bot.process_commands(message)


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
    """Author-only reaction undo. If the original message author reacts ❌
    to one of the bot's reply messages, we delete the rows that reply
    represents."""
    if payload.user_id == (bot.user.id if bot.user else 0):
        return
    if str(payload.emoji) not in ("❌", "✖️", "🚫"):
        return
    rec = db.get_reply(payload.message_id)
    if rec is None:
        return
    if payload.user_id != rec["user_id"]:
        return  # Someone else tried to undo — ignore silently.

    guild_id = rec["guild_id"]
    removed = 0
    if rec["lift_ids"]:
        ids = [int(x) for x in rec["lift_ids"].split(",") if x]
        removed = db.delete_lifts_by_ids(guild_id, rec["user_id"], ids)
    elif rec["message_id"] is not None:
        removed = db.delete_lifts_for_message(
            guild_id, rec["user_id"], rec["message_id"]
        )

    # Mark the reply so it can't be undone twice.
    db.delete_reply(payload.message_id)

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
    note = (
        f"↩️ Undid {removed} stored lift(s) at the user's request."
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
    for i, r in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        date = _format_date(r["set_on"])
        lines.append(
            f"{prefix} {r['username']} — "
            f"{_format_weight(r['best'], bool(r['bw']))}  _(set {date})_"
        )
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="log", description="Manually log a single lift.")
@app_commands.describe(
    equipment="Equipment / lift name",
    weight_kg="Weight in kg (use 0 with bodyweight=True for pure BW work)",
    bodyweight="True if this weight is added on top of bodyweight",
)
async def log_cmd(
    interaction: discord.Interaction,
    equipment: str,
    weight_kg: float,
    bodyweight: bool = False,
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name.", ephemeral=True
        )
        return

    lift = Lift(equipment=canon, weight_kg=weight_kg,
                bodyweight_add=bodyweight, raw=f"/log {equipment} {weight_kg}")
    prev = db.previous_best(guild_id, interaction.user.id, canon)
    inserted = db.add_lifts(
        guild_id=guild_id,
        user_id=interaction.user.id,
        username=interaction.user.display_name,
        lifts=[lift],
        message_id=None,
        channel_id=interaction.channel_id,
        logged_at=datetime.now(timezone.utc),
    )
    if inserted:
        msg = f"Logged {canon}: {_format_weight(weight_kg, bodyweight)}."
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
        goal = db.goal_get(guild_id, interaction.user.id, canon)
        if goal and weight_kg >= goal["target_kg"]:
            msg += (
                f"\n🎯 **Goal hit!** Target "
                f"{_format_weight(goal['target_kg'], bool(goal['bw']))} "
                "reached (goal cleared)."
            )
            db.goal_remove(guild_id, interaction.user.id, canon)
        msg += "\n-# Use `/undo` if this was logged by mistake."
        await interaction.response.send_message(msg)
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
        lines.append(
            f"• {_format_date(r['logged_at'])}: "
            f"{_format_weight(w, bool(r['bw']))}{delta}"
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

    lifts = parse_message(msg.content)
    if not lifts:
        await interaction.response.send_message(
            "No lifts detected in that message.", ephemeral=True
        )
        return
    inserted = await _store_lifts(msg, lifts)
    date = _format_date(msg.created_at.isoformat())
    lines = [
        f"Stored {inserted} new lift(s) for {msg.author.display_name} "
        f"_(posted {date})_:"
    ]
    for l in lifts:
        lines.append(f"• {l.equipment}: {_format_weight(l.weight_kg, l.bodyweight_add)}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(
    name="machine",
    description="Timeline of everyone's entries for one lift.",
)
@app_commands.describe(equipment="Equipment / lift name")
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
    lines = [
        f"**gym-bot v{__version__}**",
        f"discord.py: {discord.__version__}",
        f"auto-scan channels: {len(GYM_CHANNEL_IDS) or 'all'}",
        f"backfill on start: {'on' if BACKFILL_ON_START else 'off'}"
        f" (limit={BACKFILL_LIMIT or 'unlimited'})",
        f"show lb: {'on' if SHOW_LB else 'off'}",
        f"display timezone: {DISPLAY_TZ}",
        reminder_line,
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
        scanned, matched, inserted = await _backfill_channel(
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
        f"{matched} had lifts, {inserted} new lifts stored.",
        ephemeral=True,
    )


@bot.tree.command(
    name="purge",
    description="Delete every row for a specific equipment name.",
)
@app_commands.describe(
    equipment="Equipment name to remove (use the exact stored name)",
)
async def purge_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    canon = _resolve(interaction.guild_id or 0, equipment)
    n = db.delete_equipment(interaction.guild_id or 0, canon)
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
)
@app_commands.choices(scope=[
    app_commands.Choice(name="mine", value="mine"),
    app_commands.Choice(name="all", value="all"),
])
async def rename_cmd(
    interaction: discord.Interaction,
    old: str,
    new: str,
    user: discord.Member | None = None,
    scope: app_commands.Choice[str] | None = None,
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
        await interaction.response.send_message(
            f"No `{src}` rows found for {scope_text}.", ephemeral=True
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
    n = db.delete_entry(
        interaction.guild_id or 0, canon, date, user_id=target.id
    )
    await interaction.response.send_message(
        f"Deleted {n} entry(ies) for {target.display_name} — `{canon}` on {date}.",
        ephemeral=True,
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
            "`/progress <equipment> [user]` — best per month\n"
            "`/graph <equipment> [user]` — plot a PNG chart\n"
            "`/history <equipment> [user]` — your timeline\n"
            "`/recent [user]` — your last 10 entries\n"
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
            "`/log <equipment> <weight_kg> [bodyweight]` — manual entry\n"
            "`/undo` — remove your most recent entry\n"
            "React ❌ on my reply to undo that specific post "
            "(original author only)\n"
            "`/parse <message_id>` — reparse a message\n"
            "`/delete_entry <equipment> <date>` — remove one day\n"
            "`/rename <old> <new> [user] [scope:all]` — relabel your "
            "entries (or someone else's, or guild-wide)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔎 Discovery",
        value=(
            "`/equipment_list` — what the bot knows about\n"
            "`/aliases <equipment>` — spellings I accept\n"
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
    name="undo",
    description="Remove your most recently logged entry.",
)
async def undo_cmd(interaction: discord.Interaction) -> None:
    row = db.pop_last_for_user(
        interaction.guild_id or 0, interaction.user.id
    )
    if row is None:
        await interaction.response.send_message(
            "You don't have any entries to undo.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Removed your most recent entry — **{row['equipment']}**: "
        f"{_format_weight(row['weight_kg'], bool(row['bw']))} "
        f"_(logged {_format_date(row['logged_at'])})_.",
        ephemeral=True,
    )


@bot.tree.command(
    name="compare",
    description="Compare personal bests between you and another user.",
)
@app_commands.describe(
    user="User to compare against.",
    equipment="Optional: only compare this lift.",
)
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
async def aliases_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    guild_id = interaction.guild_id or 0
    canon = _resolve(guild_id, equipment)
    al = aliases_for(canon)
    # Also surface any server-local custom aliases that resolve to the same
    # canonical, so admins can see what they (or a previous admin) added.
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
            f"· **hit!**" if cur >= tgt and tgt > 0
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
    # Resolve the canonical the admin is pointing at (respecting built-in
    # aliases so "/alias_add foo chest fly" still lands on "pec dec").
    canon = canonicalize(equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name to map to.", ephemeral=True
        )
        return
    db.alias_set(guild_id, key, canon, interaction.user.id)
    await interaction.response.send_message(
        f"Added alias: `{key}` → **{canon}**.\n"
        "Note: custom aliases currently only apply to slash-command inputs, "
        "not auto-parsed messages.",
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
            "No custom aliases configured. Admins can add one with "
            "`/alias_add`.",
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


# ---------------------------------------------------------------------------
# Progress graph
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="graph",
    description="Plot a lift's weight over time as a PNG chart.",
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
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
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

    xs: list[datetime] = []
    ys: list[float] = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["logged_at"])
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        xs.append(dt.astimezone(DISPLAY_TZ))
        ys.append(float(r["weight_kg"]))
    if not xs:
        await interaction.followup.send(
            "Couldn't plot — no datable entries.", ephemeral=True
        )
        return

    # Also compute running-best for a reference line so gains are visible.
    running_best: list[float] = []
    cur = 0.0
    for y in ys:
        cur = max(cur, y)
        running_best.append(cur)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.plot(xs, ys, marker="o", linewidth=1.5,
            color="#f26522", label="Logged")
    ax.plot(xs, running_best, linestyle="--", linewidth=1.2,
            color="#444", alpha=0.7, label="Best to date")
    ax.set_title(f"{target.display_name} — {canon}")
    ax.set_ylabel("kg")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right")
    if len(xs) > 1:
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    fname = f"{canon.replace(' ', '_')}_{target.display_name}.png"
    file = discord.File(buf, filename=fname)
    await interaction.followup.send(
        f"📈 **{target.display_name} — {canon}** "
        f"(peak {max(ys):g}kg)",
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
compare_cmd.autocomplete("equipment")(_equipment_autocomplete)
aliases_cmd.autocomplete("equipment")(_equipment_autocomplete)
goal_set_cmd.autocomplete("equipment")(_equipment_autocomplete)
goal_remove_cmd.autocomplete("equipment")(_equipment_autocomplete)
graph_cmd.autocomplete("equipment")(_equipment_autocomplete)
alias_add_cmd.autocomplete("equipment")(_equipment_autocomplete)


def main() -> None:
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
