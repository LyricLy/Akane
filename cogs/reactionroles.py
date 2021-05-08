"""
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import asyncpg
import discord
from discord.ext import commands

from utils import db
from utils.cache import ExpiringCache, cache

if TYPE_CHECKING:
    from bot import Akane


class ReactionToleTable(db.Table, table_name="reaction_roles"):
    """
    Create our table for reaction role data.
    """

    guild_id = db.PrimaryKeyColumn(db.Integer(big=True))
    data = db.Column(db.JSON())


class NonLocalEmoji(Exception):
    """
    Custom exceptoin for an emoji that doesn't belong to the local guild.
    """


class ReactionRoleConfig:
    """ """

    __slots__ = ("id", "bot", "data")
    accepted = ("role", "approval_channel", "emoji")

    bot: Akane
    guild_id: int
    data: Dict[str, Union[int, str]]

    @classmethod
    async def from_record(
        cls, record: asyncpg.Record, bot: Akane
    ) -> ReactionRoleConfig:
        self = cls()

        self.bot = bot
        self.guild_id = record["guild_id"]
        self.data = record["data"]

        return self

    def __getitem__(
        self, item: str
    ) -> Union[
        str, discord.Role, discord.TextChannel, discord.Emoji, List[discord.Message]
    ]:
        if item not in self.accepted:
            raise ValueError(
                f"Requested an invalid item from the config. Must be one of {self.accepted}."
            )

        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise ValueError("Seems this guild is not available?")

        if item == "role":
            role = guild.get_role(self.data["role_id"])
            return role

        elif item == "approval_channel":
            return guild.get_channel(self.data.get("approval_channel"))

        elif item == "emoji":
            raw = self.data["emoji"]
            try:
                emoji_id = int(raw)
            except ValueError:
                pass
            else:
                return discord.utils.get(guild.emojis, id=emoji_id)
            return raw  # default emoji, I think

        elif item == "messages":
            raw: List[int] = self.data["messages"]


class ReactionRoles(commands.Cog):
    def __init__(self, bot: Akane) -> None:
        self.bot = bot

    @cache()
    async def get_reaction_role_config(self, guild_id: int) -> ReactionRoleConfig:
        query = """
                --begin-sql
                SELECT *
                FROM reaction_roles
                WHERE guild_id = $1;
                """

        record = await self.bot.pool.fetchrow(query, guild_id)
        if record:
            return await ReactionRoleConfig.from_record(record, self.bot)
        return None

    async def verify_emoji(self, guild: discord.Guild, item: int) -> bool:
        if not (emoji := discord.utils.get(guild.emojis, id=item)):
            try:
                emoji = await guild.fetch_emoji(item)
            except discord.NotFound:
                raise NonLocalEmoji("Emoji is from another guild.")

        return emoji
