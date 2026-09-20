"""Bounded, resumable updates of existing Strava feed map attachments."""
from __future__ import annotations

import asyncio
import io
import re

import discord

from . import strava_client

ACTIVITY_URL = re.compile(r"https://(?:www\.)?strava\.com/activities/(\d+)/?")
AUTHOR = re.compile(r"^New \*\*[^\n]+\*\* by <@!?(\d+)>")


async def refresh_maps(*, channel, bot_id, db, fetch_activity, token, style,
                       limit=25, scan_limit=500, before=None, dry_run=False):
    """Edit maps only; never post, delete, or advance activity import cursors.

    On failure next_before excludes only successfully processed messages, so
    resuming retries the failed item. A dry run only inventories candidates.
    """
    if not token:
        raise ValueError("Configure a Mapbox token before refreshing maps.")
    if not 1 <= limit <= 25 or not 1 <= scan_limit <= 2000:
        raise ValueError("Use limit 1–25 and scan_limit 1–2000.")
    cursor = int(before) if before else None
    if cursor is not None and cursor <= 0:
        raise ValueError("before must be a positive message ID.")
    accounts = {int(r["user_id"]): r for r in db.list_strava_accounts()}
    ledger = {}
    for user_id in accounts:
        for row in db.list_strava_activity_imports(user_id):
            if row["channel_id"] == channel.id and row["message_id"]:
                ledger[(int(row["message_id"]), int(row["activity_id"]))] = user_id
    result = {"scanned": 0, "candidates": 0, "updated": 0, "already_current": 0,
              "skipped": 0, "failed": 0, "next_before": str(cursor) if cursor else None,
              "done": False, "dry_run": dry_run}
    history = channel.history(limit=scan_limit, oldest_first=False,
                              before=discord.Object(id=cursor) if cursor else None)
    async def refresh_one(message):
        if message.author.id != bot_id:
            return
        matches = [(i, ACTIVITY_URL.fullmatch(e.url or ""))
                   for i, e in enumerate(message.embeds)]
        matches = [(i, m) for i, m in matches if m]
        if len(matches) != 1:
            return
        index, match = matches[0]
        activity_id = int(match.group(1))
        embed = message.embeds[index]
        image_url = str(embed.image.url or "")
        route_attachments = [a for a in message.attachments if a.filename == "route.png"]
        is_map = image_url.startswith(strava_client.MAPBOX_STATIC_BASE + "/")
        is_route = image_url == "attachment://route.png" or any(
            image_url == a.url for a in route_attachments)
        if not (is_map or is_route):
            result["skipped"] += 1  # Includes athlete photos and indoor posts.
            return
        key = f"strava_map_refresh:{channel.id}:{message.id}"
        if db.meta_get(key) == style or (
            is_map and image_url.startswith(
                f"{strava_client.MAPBOX_STATIC_BASE}/{style}/static/")):
            result["already_current"] += 1
            return
        author = AUTHOR.match(embed.description or "")
        user_id = ledger.get((message.id, activity_id))
        if user_id is None and author:
            user_id = int(author.group(1))
        if user_id not in accounts:
            result["skipped"] += 1
            return
        result["candidates"] += 1
        if dry_run:
            return
        # Fetch the current row each time: the preceding request may have
        # rotated an expired access/refresh token.
        account = db.get_strava_account(user_id)
        if account is None:
            result["skipped"] += 1
            return
        activity = await fetch_activity(account, activity_id)
        if isinstance(activity, str):
            raise RuntimeError("Activity fetch failed; retry later.")  # noqa: TRY004
        if (activity.id != activity_id or activity.athlete_id != account["athlete_id"]
                or activity.private or not activity.map_polyline or activity.photo_url):
            result["skipped"] += 1
            return
        url = strava_client.mapbox_route_url(activity.map_polyline, token, style=style)
        image = await asyncio.to_thread(strava_client.download_mapbox_route_png, url) if url else None
        if image is None:
            raise RuntimeError("Map unavailable; original retained.")
        # Preserve every other embed, attachment, field, and the message
        # itself. Upload even short maps to validate the image before editing.
        embeds = [e.copy() for e in message.embeds]
        embeds[index].set_image(url="attachment://route.png")
        attachments = [a for a in message.attachments if a.filename != "route.png"]
        file = discord.File(io.BytesIO(image), filename="route.png")
        try:
            await message.edit(embeds=embeds, attachments=[*attachments, file],
                               allowed_mentions=discord.AllowedMentions.none())
        finally:
            file.close()
        db.meta_set(key, style)
        result["updated"] += 1
    async for message in history:
        result["scanned"] += 1
        try:
            await refresh_one(message)
        except Exception:  # noqa: BLE001
            # Error strings can contain route URLs/tokens. Return counters only.
            result["failed"] += 1
            break
        result["next_before"] = str(message.id)
        if result["candidates"] >= limit:
            break
    else:
        result["done"] = result["scanned"] < scan_limit
    return result
