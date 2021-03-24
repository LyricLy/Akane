"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Union

if TYPE_CHECKING:
    from bot import Akane

import asyncio
import time

import discord
from discord.ext import commands
from utils.context import Context

TEST_MSG = discord.Embed(
    description="🎉 GIVEAWAY 🎉\n\nPrize: Nitro\nTimeleft: 1 day\nReact with 🎉 to participate!"
)


class Selfbot(commands.Cog):
    def __init__(self, bot: Akane) -> None:
        self.bot = bot
        self._tasks: Dict[int, Optional[asyncio.Task]] = {}

    async def cog_command_error(
        self, ctx: Context, error: commands.CommandError
    ) -> None:
        error = getattr(error, "original", error)

        if isinstance(error, commands.MaxConcurrencyReached):
            return await ctx.send("Already have a test running in here.")
        elif isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(
                f"Stop spamming the cancel. Try in {error.retry_after:.2f}"
            )

    @commands.group(name="selfbot", invoke_without_command=True)
    @commands.max_concurrency(1, commands.BucketType.channel, wait=False)
    async def selfbot(
        self, ctx: Context, target: Union[discord.Member, discord.User, discord.Object]
    ):
        """
        Sends an Embed with the generic selfbot reaction message. It will track how long
        it takes the target to react.
        Only one can run per channel so don't abuse it.
        """
        heh = await ctx.send(embed=TEST_MSG)
        await heh.add_reaction("\N{PARTY POPPER}")

        start_time = time.perf_counter()
        task = ctx.bot.wait_for(
            "raw_reaction_add",
            check=lambda p: str(p.emoji) == "\N{PARTY POPPER}"
            and p.user_id == target.id
            and p.message_id == heh.id
            and p.channel_id == ctx.channel.id,
            timeout=None,
        )
        self._tasks[ctx.channel.id] = hmm = asyncio.create_task(task)
        await hmm
        end_time = time.perf_counter()
        self._tasks.pop(ctx.channel.id)

        total = end_time - start_time

        await ctx.send(f"{target} reacted to the selfbot test in {total:.2f} seconds.")

    @selfbot.command(name="cancel")
    @commands.cooldown(1, 20, commands.BucketType.channel)
    async def selfbot_cancel(self, ctx: Context):
        """
        Cancels the running selfbot test in this channel. Due to the nature of Tasks there
        is no clean way to tell you it was cancelled.
        Cooldown of 20s per channel, don't be a douche.
        """
        try:
            task = self._tasks.pop(ctx.channel.id)
        except KeyError:
            return await ctx.message.add_reaction(ctx.bot.emoji[False])
        else:
            if task is None:
                return await ctx.message.add_reaction(ctx.bot.emoji[None])

        try:
            task.cancel()
        except asyncio.CancelledError:
            return await ctx.message.add_reaction(ctx.bot.emoji[True])

    @selfbot.command(name="running")
    @commands.is_owner()
    async def running_tests(self, ctx: Context) -> None:
        """
        Umbra only.
        Lists all channels where there is a test currently running.
        """
        fmt = []
        for item in self._tasks.keys():
            channel = ctx.bot.get_channel(item)
            fmt.append(f"{channel.mention} ({item})")

        await ctx.send(
            embed=discord.Embed(
                description="\n".join(fmt), colour=discord.Colour.random()
            )
        )


def setup(bot: Akane):
    bot.add_cog(Selfbot(bot))
