"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
import traceback
from collections import Counter, deque
from typing import (TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple,
                    Union)

import aiohttp
import discord
import mystbin
import nhentaio
from discord.ext import commands

import config
from utils.config import Config
from utils.context import Context

if TYPE_CHECKING:
    from asyncpg import Pool

DESCRIPTION = """
Hello! I am a bot written by Umbreon#0009 to provide some nice utilities.
"""

LOGGER = logging.getLogger(__name__)

EXTENSIONS = (
    "jishaku",
    "cogs.admin",
    "cogs.akane",
    "cogs.config",
    "cogs.external",
    "cogs.fun",
    "cogs.help",
    "cogs.lewd",
    "cogs.manga",
    "cogs.meta",
    "cogs.mod",
    "cogs.nihongo",
    "cogs.reddit",
    "cogs.reminders",
    "cogs.rng",
    "cogs.rtfx",
    "cogs.snipe",
    "cogs.stars",
    "cogs.stats",
    "cogs.tags",
    "cogs.time",
    "cogs.todo",
    "cogs.token",
    "cogs.twitch",
    "cogs.urban",
    "cogs.welcome",
    "cogs.private.campfire",
    "cogs.private.dunston",
    "cogs.private.notes",
    "cogs.private.private",
    "cogs.private.vwoes",
)


def _prefix_callable(bot: Akane, msg: discord.Message) -> Iterable[str]:
    user_id = bot.user.id
    base = [f"<@!{user_id}> ", f"<@{user_id}> "]
    if msg.guild is None:
        base.append("a!")
        base.append("A!")
    else:
        base.extend(bot.prefixes.get(msg.guild.id, ["a!", "A!"]))
    return base


class Akane(commands.Bot):
    """ The actual robot herself! """

    pool: Pool

    def __init__(self):
        intents = discord.Intents.all()

        super().__init__(
            command_prefix=_prefix_callable,
            description=DESCRIPTION,
            activity=discord.Game(name="a!help for help."),
            allowed_mentions=discord.AllowedMentions.none(),
            intents=intents,
        )
        self.session = aiohttp.ClientSession()
        self.mb_client = mystbin.Client(session=self.session)
        self.hentai_client = nhentaio.Client()
        self._prev_events = deque(maxlen=10)
        self.prefixes = Config("prefixes.json")
        self.blacklist = Config("blacklist.json")

        self.emoji = {
            True: "<:TickYes:735498312861351937>",
            False: "<:CrossNo:735498453181923377>",
            None: "<:QuestionMaybe:738038828928860269>",
        }
        self.colour: Dict[str, Union[Tuple[int, ...], discord.Colour]] = {
            "dsc": discord.Colour(0xEC9FED),
            "rgb": (236, 159, 237),
            "hsv": (299, 33, 93),
        }

        # in case of even further spam, add a cooldown mapping
        # for people who excessively spam commands
        self.spam_control = commands.CooldownMapping.from_cooldown(
            10, 12.0, commands.BucketType.user
        )

        # A counter to auto-ban frequent spammers
        # Triggering the rate limit 5 times in a row will auto-ban the user from the bot.
        self._auto_spam_count = Counter()

        for extension in EXTENSIONS:
            try:
                self.load_extension(extension)
            except Exception:
                print(f"Failed to load extension {extension}.", file=sys.stderr)
                traceback.print_exc()

    async def on_socket_response(self, msg: Any) -> None:
        """ Websocket responses. """
        self._prev_events.append(msg)

    async def on_command_error(
        self, ctx: Context, error: commands.CommandError
    ) -> None:
        """ When a command errors out. """
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send("This command cannot be used in private messages.")
        elif isinstance(error, commands.DisabledCommand):
            pass
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if not isinstance(original, discord.HTTPException):
                print(f"In {ctx.command.qualified_name}:", file=sys.stderr)
                traceback.print_tb(original.__traceback__)
                print(f"{original.__class__.__name__}: {original}", file=sys.stderr)
        elif isinstance(error, commands.ArgumentParsingError):
            await ctx.send(error)

    def get_guild_prefixes(
        self, guild: discord.Guild, *, local_inject=_prefix_callable
    ) -> List[str]:
        """ Get prefixes per guild. """
        proxy_msg = discord.Object(id=0)
        proxy_msg.guild = guild
        return local_inject(self, proxy_msg)

    def get_raw_guild_prefixes(self, guild_id: int) -> List[str]:
        """ The raw prefixes. """
        return self.prefixes.get(guild_id, ["a!", "A!"])

    async def set_guild_prefixes(
        self, guild: discord.Guild, prefixes: List[str]
    ) -> None:
        """ Set the prefixes. """
        if not prefixes:
            await self.prefixes.put(guild.id, [])
        elif len(prefixes) > 10:
            raise RuntimeError("Cannot have more than 10 custom prefixes.")
        else:
            await self.prefixes.put(guild.id, sorted(set(prefixes), reverse=True))

    async def add_to_blacklist(self, object_id: int) -> None:
        """ Add object to blacklist. """
        await self.blacklist.put(object_id, True)

    async def remove_from_blacklist(self, object_id: int) -> None:
        """ Remove object from blacklist. """
        try:
            await self.blacklist.remove(object_id)
        except KeyError:
            pass

    async def on_ready(self) -> None:
        """ When the websocket reports ready. """
        if not hasattr(self, "uptime"):
            self.uptime = datetime.datetime.utcnow()

        print(f"Ready: {self.user} (ID: {self.user.id})")

    async def on_resumed(self) -> None:
        """ When the websocket resumes a connection. """
        print("Resumed...")

    @discord.utils.cached_property
    def stat_webhook(self) -> discord.Webhook:
        """ Get webhook stats. """
        hook = discord.Webhook.from_url(
            config.stat_webhook, adapter=discord.AsyncWebhookAdapter(self.session)
        )
        return hook

    def log_spammer(
        self,
        ctx: Context,
        message: discord.Message,
        retry_after: float,
        *,
        autoblock: bool = False,
    ) -> Optional[discord.WebhookMessage]:
        """ Deals with events that spam the log. """
        guild_name = getattr(ctx.guild, "name", "No Guild (DMs)")
        guild_id = getattr(ctx.guild, "id", None)
        fmt = "User %s (ID %s) in guild %r (ID %s) spamming, retry_after: %.2fs"
        LOGGER.warning(
            fmt, message.author, message.author.id, guild_name, guild_id, retry_after
        )
        if not autoblock:
            return

        webhook = self.stat_webhook
        embed = discord.Embed(title="Auto-blocked Member", colour=0xDDA453)
        embed.add_field(
            name="Member",
            value=f"{message.author} (ID: {message.author.id})",
            inline=False,
        )
        embed.add_field(
            name="Guild Info", value=f"{guild_name} (ID: {guild_id})", inline=False
        )
        embed.add_field(
            name="Channel Info",
            value=f"{message.channel} (ID: {message.channel.id})",
            inline=False,
        )
        embed.timestamp = datetime.datetime.utcnow()
        return webhook.send(embed=embed)

    async def process_commands(self, message: discord.Message) -> None:
        """ Bot's process command override. """
        ctx = await self.get_context(message, cls=Context)

        if ctx.command is None:
            return

        if ctx.author.id in self.blacklist:
            return

        if ctx.guild is not None and ctx.guild.id in self.blacklist:
            return

        bucket = self.spam_control.get_bucket(message)
        current = message.created_at.replace(tzinfo=datetime.timezone.utc).timestamp()
        retry_after = bucket.update_rate_limit(current)
        author_id = message.author.id
        if retry_after and author_id != self.owner_id:
            self._auto_spam_count[author_id] += 1
            if self._auto_spam_count[author_id] >= 5:
                await self.add_to_blacklist(author_id)
                del self._auto_spam_count[author_id]
                await self.log_spammer(ctx, message, retry_after, autoblock=True)
            else:
                self.log_spammer(ctx, message, retry_after)
            return
        else:
            self._auto_spam_count.pop(author_id, None)

        try:
            await self.invoke(ctx)
        finally:
            # Just in case we have any outstanding DB connections
            await ctx.release()

    async def on_message(self, message: discord.Message) -> None:
        """ Fires when a message is received. """
        if message.author.bot:
            return
        await self.process_commands(message)

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if after.author.id == self.owner_id:
            if not before.embeds and after.embeds:
                return
            await self.process_commands(after)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """ When the bot joins a guild. """
        if guild.id in self.blacklist:
            await guild.leave()

    async def close(self) -> None:
        """ When the bot closes. """
        await asyncio.gather(
            super().close(),
            self.session.close(),
            self.mb_client.close(),
        )
        self.hentai_client.close()

    def run(self) -> None:
        """ Run my Akane please. """
        try:
            super().run(config.token, reconnect=True)
        finally:
            with open("prev_events.log", "w", encoding="utf-8") as file_path:
                for data in self._prev_events:
                    try:
                        last_log = json.dumps(data, ensure_ascii=True, indent=4)
                    except Exception:
                        file_path.write(f"{data}\n")
                    else:
                        file_path.write(f"{last_log}\n")

    @property
    def config(self):
        """ Bot's config. """
        return __import__("config")
