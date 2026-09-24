"""Resolve linked members without requiring the privileged member cache."""
import time

import discord


class MemberNames:
    def __init__(self):
        self.cache = {}

    async def resolve(self, bot, db, guild_id: int, user_id: int) -> str:
        key = (guild_id, user_id)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        guild = bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None and guild is not None and bot.is_ready():
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                pass
        if member is not None:
            name = member.display_name
            # Persist just the member's identity for dashboard and offline use.
            # This does not enable member/role/event tracking or gateway intents.
            db.upsert_member(guild_id, user_id, member.name, name,
                             is_bot=member.bot,
                             avatar=str(member.display_avatar.url))
            self.cache[key] = (time.monotonic() + 300, name)
            return name
        stored = db.get_member(guild_id, user_id)
        name = (stored["display_name"] or stored["username"]) if stored else None
        if not name or name == str(user_id):
            name = db.get_user_nickname(user_id)
        if not name:
            user = bot.get_user(user_id)
            if user is None and bot.is_ready():
                try:
                    user = await bot.fetch_user(user_id)
                except discord.HTTPException:
                    pass
            name = user.display_name if user else "Member"
        self.cache[key] = (time.monotonic() + 60, name)
        return name
