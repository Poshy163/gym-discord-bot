"""Gym tracking Discord bot.

Auto-detects gym posts in configured channels, parses lifts, and stores them
in SQLite. Exposes slash commands for querying stats, progress, and
leaderboards.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .aliases import canonicalize
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

db = Database(DB_PATH)

intents = discord.Intents.default()
intents.message_content = True
intents.members = False
bot = commands.Bot(command_prefix="!gym ", intents=intents)


def _format_weight(weight: float, bw: bool) -> str:
    if bw and weight == 0:
        return "BW"
    if bw:
        return f"BW+{weight:g}kg"
    return f"{weight:g}kg"


def _format_date(iso: str | None) -> str:
    """Return 'YYYY-MM-DD' from a stored ISO timestamp."""
    if not iso:
        return "?"
    return iso[:10]


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
        inserted = await _store_lifts(message, lifts)
        if inserted > 0:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
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
                await message.reply(reply, mention_author=False)
            except discord.HTTPException:
                pass
            LOG.info(
                "Stored %d lifts from %s in #%s",
                inserted, message.author, message.channel,
            )

    await bot.process_commands(message)


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
    canon = canonicalize(equipment)
    guild_id = interaction.guild_id or 0
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
    canon = canonicalize(equipment)
    guild_id = interaction.guild_id or 0
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
    canon = canonicalize(equipment)
    if not canon:
        await interaction.response.send_message(
            "Please provide an equipment name.", ephemeral=True
        )
        return

    lift = Lift(equipment=canon, weight_kg=weight_kg,
                bodyweight_add=bodyweight, raw=f"/log {equipment} {weight_kg}")
    inserted = db.add_lifts(
        guild_id=interaction.guild_id or 0,
        user_id=interaction.user.id,
        username=interaction.user.display_name,
        lifts=[lift],
        message_id=None,
        channel_id=interaction.channel_id,
        logged_at=datetime.now(timezone.utc),
    )
    if inserted:
        await interaction.response.send_message(
            f"Logged {canon}: {_format_weight(weight_kg, bodyweight)}."
        )
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
    canon = canonicalize(equipment)
    guild_id = interaction.guild_id or 0
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
    canon = canonicalize(equipment)
    guild_id = interaction.guild_id or 0
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
    lines = [
        f"**gym-bot v{__version__}**",
        f"discord.py: {discord.__version__}",
        f"auto-scan channels: {len(GYM_CHANNEL_IDS) or 'all'}",
        f"backfill on start: {'on' if BACKFILL_ON_START else 'off'}"
        f" (limit={BACKFILL_LIMIT or 'unlimited'})",
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
    # Only allow server members with Manage Messages to trigger a rescan.
    perms = interaction.channel.permissions_for(interaction.user)  # type: ignore[arg-type]
    if not perms.manage_messages:
        await interaction.response.send_message(
            "You need Manage Messages to run backfill.", ephemeral=True
        )
        return

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
    description="Admin: delete every row for a specific equipment name.",
)
@app_commands.describe(
    equipment="Equipment name to remove (use the exact stored name)",
)
async def purge_cmd(
    interaction: discord.Interaction, equipment: str
) -> None:
    perms = interaction.channel.permissions_for(interaction.user)  # type: ignore[arg-type]
    if not perms.manage_messages:
        await interaction.response.send_message(
            "You need Manage Messages to purge data.", ephemeral=True
        )
        return
    canon = canonicalize(equipment)
    n = db.delete_equipment(interaction.guild_id or 0, canon)
    await interaction.response.send_message(
        f"Removed {n} row(s) for `{canon}`.", ephemeral=True
    )


@bot.tree.command(
    name="rename",
    description="Admin: merge every row from one equipment name into another.",
)
@app_commands.describe(
    old="The current (bad / misparsed) equipment name.",
    new="The correct equipment to merge the rows into.",
)
async def rename_cmd(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    perms = interaction.channel.permissions_for(interaction.user)  # type: ignore[arg-type]
    if not perms.manage_messages:
        await interaction.response.send_message(
            "You need Manage Messages to rename data.", ephemeral=True
        )
        return

    src = canonicalize(old)
    dst = canonicalize(new)
    if src == dst:
        await interaction.response.send_message(
            "Source and destination are the same after canonicalization.",
            ephemeral=True,
        )
        return
    n = db.rename_equipment(interaction.guild_id or 0, src, dst)
    await interaction.response.send_message(
        f"Re-labelled {n} row(s): `{src}` → `{dst}`.", ephemeral=True
    )


@bot.tree.command(
    name="delete_entry",
    description="Delete one day's entries for a lift (yours by default).",
)
@app_commands.describe(
    equipment="Equipment / lift name",
    date="Date of the entry to remove (YYYY-MM-DD)",
    user="Target user (admin only; defaults to you).",
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
    if user is not None and user.id != interaction.user.id:
        perms = interaction.channel.permissions_for(interaction.user)  # type: ignore[arg-type]
        if not perms.manage_messages:
            await interaction.response.send_message(
                "You need Manage Messages to delete someone else's entry.",
                ephemeral=True,
            )
            return

    canon = canonicalize(equipment)
    n = db.delete_entry(
        interaction.guild_id or 0, canon, date, user_id=target.id
    )
    await interaction.response.send_message(
        f"Deleted {n} entry(ies) for {target.display_name} — `{canon}` on {date}.",
        ephemeral=True,
    )


# ---- autocomplete for equipment names ------------------------------------


async def _equipment_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    guild_id = interaction.guild_id or 0
    names = db.known_equipment(guild_id)
    cur = current.lower()
    filtered = [n for n in names if cur in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in filtered]


progress_cmd.autocomplete("equipment")(_equipment_autocomplete)
leaderboard_cmd.autocomplete("equipment")(_equipment_autocomplete)
log_cmd.autocomplete("equipment")(_equipment_autocomplete)
history_cmd.autocomplete("equipment")(_equipment_autocomplete)
machine_cmd.autocomplete("equipment")(_equipment_autocomplete)
purge_cmd.autocomplete("equipment")(_equipment_autocomplete)
rename_cmd.autocomplete("old")(_equipment_autocomplete)
rename_cmd.autocomplete("new")(_equipment_autocomplete)
delete_entry_cmd.autocomplete("equipment")(_equipment_autocomplete)


def main() -> None:
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
