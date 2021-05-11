"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import copy
import io
import time
import traceback
from typing import TYPE_CHECKING, Optional, Set, Union, Generator, Any

import discord
from discord.ext import commands

from utils import db, formats
from utils.context import Context

if TYPE_CHECKING:
    from bot import Akane


class BlockTable(db.Table, table_name="owner_blocked"):
    """Keeping track of whom I blocked and why."""

    user_id = db.Column(db.Integer(big=True), primary_key=True)
    reason = db.Column(db.String)


class PerformanceMocker:
    """A mock object that can also be used in await expressions."""

    def __init__(self) -> None:
        self.loop = asyncio.get_event_loop()

    def permissions_for(
        self, obj: Union[discord.Role, discord.Member]
    ) -> discord.Permissions:
        """Lying about permissions to embed, only temporarily."""
        # This makes it so pagination sessions just abruptly end on __init__
        # Most checks based on permission have a bypass for the owner anyway
        # So this lie will not affect the actual command invocation.
        perms = discord.Permissions.all()
        perms.administrator = False
        perms.embed_links = False
        perms.add_reactions = False
        return perms

    def __getattr__(self, attr) -> PerformanceMocker:
        return self

    def __call__(self, *args, **kwargs) -> PerformanceMocker:
        return self

    def __repr__(self) -> str:
        return "<PerformanceMocker>"

    def __await__(self) -> Generator[Any, None, Any]:
        future = self.loop.create_future()
        future.set_result(self)
        return future.__await__()

    async def __aenter__(self) -> PerformanceMocker:
        return self

    async def __aexit__(self, *args) -> PerformanceMocker:
        return self

    def __len__(self) -> 0:
        return 0

    def __bool__(self) -> bool:
        return False


class GlobalChannel(commands.Converter):
    """GlobalChannel converter object."""

    async def convert(
        self, ctx: Context, argument: str
    ) -> Union[
        discord.TextChannel,
        discord.VoiceChannel,
        discord.StageChannel,
        discord.CategoryChannel,
    ]:
        """Perform conversion."""
        try:
            return await commands.TextChannelConverter().convert(ctx, argument)
        except commands.BadArgument:
            # Not found... so fall back to ID + global lookup
            try:
                channel_id = int(argument, base=10)
            except ValueError:
                raise commands.BadArgument(
                    f"Could not find a channel by ID {argument!r}."
                )
            else:
                channel = ctx.bot.get_channel(channel_id)
                if channel is None:
                    raise commands.BadArgument(
                        f"Could not find a channel by ID {argument!r}."
                    )
                return channel


class Admin(commands.Cog):
    """Admin-only commands that make the bot dynamic."""

    def __init__(self, bot: Akane) -> None:
        self.bot = bot
        self.my_guilds: Set[int] = {705500489248145459, 766520806289178646}

    def cleanup_code(self, content: str) -> str:
        """Automatically removes code blocks from the code."""
        if content.startswith("```") and content.endswith("```"):
            return "\n".join(content.split("\n")[1:-1])

        # remove `foo`
        return content.strip("` \n")

    async def cog_check(self, ctx: Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    def get_syntax_error(self, err: SyntaxError) -> str:
        """Grabs the syntax error."""
        if err.text is None:
            return f"```py\n{err.__class__.__name__}: {err}\n```"
        return (
            f'```py\n{err.text}{"^":>{err.offset}}\n{err.__class__.__name__}: {err}```'
        )

    @commands.command()
    async def leave(self, ctx: Context) -> None:
        """Leaves the current guild."""
        await ctx.guild.leave()

    @commands.command()
    async def load(self, ctx: Context, *, module: str) -> None:
        """Loads a module."""
        module = f"cogs.{module}"

        try:
            self.bot.load_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f"{err.__class__.__name__}: {err}")
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.command()
    async def unload(self, ctx: Context, *, module: str) -> None:
        """Unloads a module."""
        module = f"cogs.{module}"

        try:
            self.bot.unload_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f"{err.__class__.__name__}: {err}")
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.command(name="reload")
    async def _reload(self, ctx: Context, *, module: str) -> None:
        """Reloads a module."""
        module = f"cogs.{module}"

        try:
            self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            return self.bot.load_extension(module)
        except commands.ExtensionError as err:
            await ctx.send(f"{err.__class__.__name__}: {err}")
        else:
            await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.group(invoke_without_command=True)
    async def sql(self, ctx: Context, *, query: str) -> None:
        """Run some SQL."""
        query = self.cleanup_code(query)

        is_multistatement = query.count(";") > 1
        if is_multistatement:
            # fetch does not support multiple statements
            strategy = ctx.db.execute
        else:
            strategy = ctx.db.fetch

        try:
            start = time.perf_counter()
            results = await strategy(query)
            dati = (time.perf_counter() - start) * 1000.0
        except Exception:
            return await ctx.send(f"```py\n{traceback.format_exc()}\n```")

        rows = len(results)
        if is_multistatement or rows == 0:
            return await ctx.send(f"`{dati:.2f}ms: {results}`")

        headers = list(results[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in results)
        render = table.render()

        fmt = (
            f"```\n{render}\n```\n*Returned {formats.plural(rows):row} in {dati:.2f}ms*"
        )
        if len(fmt) > 2000:
            filep = io.BytesIO(fmt.encode("utf-8"))
            await ctx.send(
                "Too many results...", file=discord.File(filep, "results.txt")
            )
        else:
            await ctx.send(fmt)

    @sql.command(name="table")
    async def sql_table(self, ctx: Context, *, table_name: str) -> None:
        """Runs a query describing the table schema."""
        query = """SELECT column_name, data_type, column_default, is_nullable
                   FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE table_name = $1
                """

        results = await ctx.db.fetch(query, table_name)

        headers = list(results[0].keys())
        table = formats.TabularData()
        table.set_columns(headers)
        table.add_rows(list(r.values()) for r in results)
        render = table.render()

        fmt = f"```\n{render}\n```"
        if len(fmt) > 2000:
            filep = io.BytesIO(fmt.encode("utf-8"))
            await ctx.send(
                "Too many results...", file=discord.File(filep, "results.txt")
            )
        else:
            await ctx.send(fmt)

    @commands.command()
    async def sudo(
        self, ctx, channel: Optional[GlobalChannel], who: discord.User, *, command: str
    ) -> None:
        """Run a command as another user optionally in another channel."""
        msg = copy.copy(ctx.message)
        channel = channel or ctx.channel
        msg.channel = channel
        msg.author = channel.guild.get_member(who.id) or who
        msg.content = ctx.prefix + command
        new_ctx = await self.bot.get_context(msg, cls=type(ctx))
        new_ctx._db = ctx._db
        await self.bot.invoke(new_ctx)

    @commands.command()
    async def perf(self, ctx: Context, *, command: str) -> None:
        """Checks the timing of a command, attempting to suppress HTTP and DB calls."""

        msg = copy.copy(ctx.message)
        msg.content = ctx.prefix + command

        new_ctx = await self.bot.get_context(msg, cls=type(ctx))
        new_ctx._db = PerformanceMocker()

        # Intercepts the Messageable interface a bit
        new_ctx._state = PerformanceMocker()
        new_ctx.channel = PerformanceMocker()

        if new_ctx.command is None:
            return await ctx.send("No command found")

        start = time.perf_counter()
        try:
            await new_ctx.command.invoke(new_ctx)
        except commands.CommandError:
            end = time.perf_counter()
            success = False
            try:
                await ctx.send(f"```py\n{traceback.format_exc()}\n```")
            except discord.HTTPException:
                pass
        else:
            end = time.perf_counter()
            success = True

        await ctx.send(
            f"Status: {ctx.tick(success)} Time: {(end - start) * 1000:.2f}ms"
        )

    async def ban_all(self, dick_id: int) -> None:
        """Ban em from all your guilds."""
        for gid in self.my_guilds:
            g = self.bot.get_guild(gid)
            await g.ban(discord.Object(id=dick_id))

    async def unban_all(self, not_dick_id: int) -> None:
        """Unban em from all your guilds."""
        for gid in self.my_guilds:
            g = self.bot.get_guild(gid)
            await g.unban(discord.Object(id=not_dick_id))

    @commands.group(name="ublock", invoke_without_command=True)
    async def _block(self, ctx: Context, user_id: int, *, reason: str) -> None:
        """Let's make a private 'why I blocked them case'."""
        query = """ INSERT INTO owner_blocked (user_id, reason)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id)
                    DO UPDATE SET reason = $2
                """
        coros = [self.bot.pool.execute(query, user_id, reason), self.ban_all(user_id)]
        config = self.bot.get_cog("Config")
        if config:
            coros.append(config.global_block(ctx, user_id))
        await asyncio.gather(*coros)
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

    @_block.command(name="query", aliases=["q"])
    async def _block_query(self, ctx: Context, user_id: int) -> None:
        """Show why the target was blocked."""
        query = """ SELECT reason FROM owner_blocked WHERE user_id = $1; """
        result = await self.bot.pool.fetchrow(query, user_id)

        if not result:
            return await ctx.send("Huh, you've not complained about them yet.")

        embed = discord.Embed(description=result["reason"])
        await ctx.send(embed=embed)

    @_block.command(name="remove", aliases=["r"])
    async def _block_remove(
        self, ctx: Context, user_id: int, unban: bool = False
    ) -> None:
        """Remove a block entry."""
        query = """ DELETE FROM owner_blocked WHERE user_id = $1; """
        coros = [self.bot.pool.execute(query, user_id), self.unban_all(user_id)]
        config = self.bot.get_cog("Config")
        if unban and config:
            coros.append(config.global_unblock(ctx, user_id))
        await asyncio.gather(*coros)
        return await ctx.message.add_reaction(self.bot.emoji[True])


def setup(bot: Akane):
    """Cog entrypoint."""
    bot.add_cog(Admin(bot))
