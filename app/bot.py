"""Gym tracking Discord bot.

Auto-detects gym posts in configured channels, parses lifts, and stores them
in SQLite. Exposes slash commands for querying stats, progress, and
leaderboards.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .aliases import canonicalize
from .db import Database
from .parser import Lift, parse_message

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
    LOG.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    try:
        if DEV_GUILD is not None:
            bot.tree.copy_global_to(guild=DEV_GUILD)
            synced = await bot.tree.sync(guild=DEV_GUILD)
        else:
            synced = await bot.tree.sync()
        LOG.info("Synced %d slash commands", len(synced))
    except Exception:  # pragma: no cover - discord runtime only
        LOG.exception("Failed to sync commands")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    if GYM_CHANNEL_IDS and message.channel.id not in GYM_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    lifts = parse_message(message.content)
    if len(lifts) >= MIN_LIFTS_FOR_AUTO:
        inserted = await _store_lifts(message, lifts)
        if inserted > 0:
            try:
                await message.add_reaction("✅")
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
        lines.append(f"• {r['equipment']}: {_format_weight(r['best'], bool(r['bw']))}")
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
        lines.append(f"• {r['month']}: {_format_weight(best, bool(r['bw']))}{delta}")
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
        lines.append(
            f"{prefix} {r['username']} — {_format_weight(r['best'], bool(r['bw']))}"
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
    lines = [f"Stored {inserted} new lift(s) for {msg.author.display_name}:"]
    for l in lifts:
        lines.append(f"• {l.equipment}: {_format_weight(l.weight_kg, l.bodyweight_add)}")
    await interaction.response.send_message("\n".join(lines))


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


def main() -> None:
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
