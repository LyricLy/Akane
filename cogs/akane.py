"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import datetime
import timeit
import traceback
from functools import partial
from typing import NamedTuple, TYPE_CHECKING, Optional, Tuple

import discord
from discord.ext import commands, tasks
from jishaku.codeblocks import codeblock_converter

from utils.context import Context
from utils.formats import to_codeblock


class ProfileState(NamedTuple):
    path: str
    name: str


if TYPE_CHECKING:
    from bot import Akane


class AkaneCore(commands.Cog, name="Akane"):
    """Akane specific commands."""

    def __init__(self, bot: Akane):
        self.bot = bot
        self.akane_task.start()
        self.akane_details = {
            False: ProfileState("static/Dusk.png", "Akane Dusk"),
            True: ProfileState("static/Dawn.png", "Akane Dawn"),
        }
        self.akane_time = datetime.datetime.utcnow()
        self.akane_next: Optional[datetime.datetime] = None

    def cog_unload(self):
        self.akane_task.cancel()

    @commands.command(name="hello")
    async def hello(self, ctx: Context) -> None:
        """Say hello to Akane."""
        now = datetime.datetime.utcnow()
        time = now.hour >= 6 and now.hour < 18
        path = self.akane_details[time].path

        file = discord.File(path, filename="akane.jpg")
        embed = discord.Embed(colour=self.bot.colour["dsc"])
        embed.set_image(url="attachment://akane.jpg")
        embed.description = f"Hello, I am {self.akane_details[time].name}, written by Umbra#0009.\n\nYou should see my other side~"

        await ctx.send(embed=embed, file=file)

    @commands.group(invoke_without_command=True)
    async def akane(self, ctx: Context) -> None:
        """This is purely for subcommands."""
        if not ctx.invoked_subcommand:
            return await ctx.send_help(ctx.command)

    @akane.command()
    @commands.is_owner()
    async def core(self, ctx: Context, *, body: codeblock_converter) -> None:
        """Directly evaluate Akane core code."""
        jsk = self.bot.get_command("jishaku python")
        await jsk(ctx, argument=body)

    @akane.command()
    @commands.is_owner()
    async def system(self, ctx: Context, *, body: codeblock_converter) -> None:
        """Directly evaluate Akane system code."""
        jsk = self.bot.get_command("jishaku shell")
        await jsk(ctx, argument=body)

    @akane.command()
    @commands.is_owner()
    async def timeit(
        self,
        ctx: Context,
        iterations: Optional[int] = 100,
        *,
        body: codeblock_converter,
    ) -> None:

        await ctx.message.add_reaction(self.bot.emoji[None])
        timeit_globals = {
            "ctx": ctx,
            "guild": ctx.guild,
            "author": ctx.author,
            "bot": ctx.bot,
            "channel": ctx.channel,
            "message": ctx.message,
            "discord": discord,
            "commands": commands,
        }
        timeit_globals.update(globals())

        func = partial(
            timeit.timeit, body.content, number=iterations, globals=timeit_globals
        )
        run = await self.bot.loop.run_in_executor(None, func)
        await ctx.message.add_reaction(self.bot.emoji[True])

        embed = discord.Embed(
            title=f"timeit of {iterations} iterations took {run:.20f}.",
            colour=self.bot.colour["dsc"],
        )
        embed.add_field(
            name="Body",
            value=to_codeblock(body.content, language=body.language, escape_md=False),
        )

        await ctx.send(embed=embed)

    @akane.command(aliases=["sauce"])
    @commands.is_owner()
    async def source(self, ctx: Context, *, command: str) -> None:
        """Show Akane system code."""
        jsk = self.bot.get_command("jishaku source")
        await jsk(ctx, command_name=command)

    @akane.command(aliases=["debug"])
    @commands.is_owner()
    async def diagnose(self, ctx: Context, *, command_name: str) -> None:
        """Diagnose akane features."""
        jsk = self.bot.get_command("jishaku debug")
        await jsk(ctx, command_string=command_name)

    @akane.command()
    @commands.is_owner()
    async def sleep(self, ctx: Context) -> None:
        """Akane naptime."""
        await ctx.send("さようなら!")
        await self.bot.close()

    def dt(self) -> Tuple[bool, datetime.datetime]:
        now = datetime.datetime.utcnow()
        light = now.hour >= 6 and now.hour < 18
        start = datetime.time(hour=(18 if light else 6))

        if now.time() > start:
            now = now + datetime.timedelta(hours=12)
        then = datetime.datetime.combine(now.date(), start)

        return light, then

    @tasks.loop(minutes=5)
    async def akane_task(self) -> None:
        light, then = self.dt()

        profile = self.akane_details[light]
        name = profile.name
        path = profile.path

        if now := (datetime.datetime.utcnow()) > self.akane_time:
            await self.webhook_send(
                f"In task: Now {now}, mapped time: {self.akane_time}"
            )
            with open(path, "rb") as buffer:
                await self.webhook_send(f"Performing change to: {name}")
                await self.bot.user.edit(username=name, avatar=buffer.read())

        self.akane_time = then

    @akane_task.before_loop
    async def before_akane(self) -> None:
        await self.bot.wait_until_ready()

        light, then = self.dt()

        profile = self.akane_details[light]
        name = profile.name
        path = profile.path
        await self.webhook_send(name)

        if (light and self.bot.user.name != "Akane Dawn") or (
            not light and self.bot.user.name != "Akane Dusk"
        ):
            with open(path, "rb") as buffer:
                await self.webhook_send(f"Drift - changing to: {name}.")
                await self.bot.user.edit(username=name, avatar=buffer.read())

        self.akane_time = then
        await self.webhook_send(f"Before task: waiting until {then}.")
        await discord.utils.sleep_until(then)

    @akane_task.error
    async def akane_error(self, error: Exception):
        error = getattr(error, "original", error)

        if isinstance(error, discord.HTTPException):
            await self.webhook_send("You are ratelimited on profile edits.")
            self.akane_task.cancel()
            self.akane_task.start()
        else:
            embed = discord.Embed(title="Akane Error", colour=discord.Colour.red())
            lines = traceback.format_exception(
                type(error), error, error.__traceback__, 4
            )
            embed.description = to_codeblock("".join(lines))
            await self.webhook_send(embed=embed)

    async def webhook_send(
        self, message: str = "Error", *, embed: discord.Embed = None
    ):
        cog = self.bot.get_cog("Stats")
        if not cog:
            await asyncio.sleep(5)
            return await self.webhook_send(message, embed=embed)
        wh = cog.webhook
        await wh.send(message, embed=embed)


def setup(bot):
    bot.add_cog(AkaneCore(bot))
