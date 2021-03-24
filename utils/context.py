"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Iterable, Optional

import aiohttp
import asyncpg
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Akane


class _ContextDBAcquire:
    __slots__ = ("ctx", "timeout")

    def __init__(self, ctx: Context, timeout: int) -> None:
        self.ctx = ctx
        self.timeout = timeout

    def __await__(self):
        return self.ctx._acquire(self.timeout).__await__()

    async def __aenter__(self):
        await self.ctx._acquire(self.timeout)
        return self.ctx.db

    async def __aexit__(self, *args):
        await self.ctx.release()


class Context(commands.Context):

    bot: Akane

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pool = self.bot.pool
        self._db: Optional[asyncpg.Connection] = None

    def __repr__(self) -> str:
        # we need this for our cache key strategy
        return "<Context>"

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.bot.session

    async def disambiguate(self, matches: Iterable[Any], entry: Any):
        if len(matches) == 0:
            raise ValueError("No results found.")

        if len(matches) == 1:
            return matches[0]

        await self.send(
            "There are too many matches... Which one did you mean? **Only say the number**."
        )
        await self.send(
            "\n".join(
                f"{index}: {entry(item)}" for index, item in enumerate(matches, 1)
            )
        )

        def check(m):
            return (
                m.content.isdigit()
                and m.author.id == self.author.id
                and m.channel.id == self.channel.id
            )

        await self.release()

        # only give them 3 tries.
        try:
            for i in range(3):
                try:
                    message = await self.bot.wait_for(
                        "message", check=check, timeout=30.0
                    )
                except asyncio.TimeoutError:
                    raise ValueError("Took too long. Goodbye.")

                index = int(message.content)
                try:
                    return matches[index - 1]
                except Exception:
                    await self.send(
                        f"Please give me a valid number. {2 - i} tries remaining..."
                    )

            raise ValueError("Too many tries. Goodbye.")
        finally:
            await self.acquire()

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        reacquire: bool = True,
        author_id: int = None,
    ):
        """An interactive reaction confirmation dialog.

        Parameters
        -----------
        message: str
            The message to show along with the prompt.
        timeout: float
            How long to wait before returning.
        delete_after: bool
            Whether to delete the confirmation message after we're done.
        reacquire: bool
            Whether to release the database connection and then acquire it
            again when we're done.
        author_id: Optional[int]
            The member who should respond to the prompt. Defaults to the author of the
            Context's message.

        Returns
        --------
        Optional[bool]
            ``True`` if explicit confirm,
            ``False`` if explicit deny,
            ``None`` if deny due to timeout
        """

        if not self.channel.permissions_for(self.me).add_reactions:
            raise RuntimeError("Bot does not have Add Reactions permission.")

        fmt = f"{message}\n\nReact with \N{WHITE HEAVY CHECK MARK} to confirm or \N{CROSS MARK} to deny."

        author_id = author_id or self.author.id
        msg = await self.send(fmt)

        confirm = None

        def check(payload):
            nonlocal confirm

            if payload.message_id != msg.id or payload.user_id != author_id:
                return False

            codepoint = str(payload.emoji)

            if codepoint == "\N{WHITE HEAVY CHECK MARK}":
                confirm = True
                return True
            elif codepoint == "\N{CROSS MARK}":
                confirm = False
                return True

            return False

        for emoji in ("\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"):
            await msg.add_reaction(emoji)

        if reacquire:
            await self.release()

        try:
            await self.bot.wait_for("raw_reaction_add", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            confirm = None

        try:
            if reacquire:
                await self.acquire()

            if delete_after:
                await msg.delete()
        finally:
            return confirm

    def tick(self, opt: Optional[bool], label=None):
        lookup = {
            True: "<:TickYes:735498312861351937>",
            False: "<:CrossNo:735498453181923377>",
            None: "<:QuestionMaybe:738038828928860269>",
        }
        emoji = lookup.get(opt, "❌")
        if label is not None:
            return f"{emoji}: {label}"
        return emoji

    @property
    def db(self) -> asyncpg.Connection:
        return self._db if self._db else self.pool

    async def _acquire(self, timeout: int) -> asyncpg.Connection:
        if self._db is None:
            self._db = await self.pool.acquire(timeout=timeout)
        return self._db

    def acquire(self, *, timeout=None) -> _ContextDBAcquire:
        """Acquires a database connection from the pool. e.g. ::

            async with ctx.acquire():
                await ctx.db.execute(...)

        or: ::

            await ctx.acquire()
            try:
                await ctx.db.execute(...)
            finally:
                await ctx.release()
        """
        return _ContextDBAcquire(self, timeout)

    async def release(self) -> None:
        """Releases the database connection from the pool.

        Useful if needed for "long" interactive commands where
        we want to release the connection and re-acquire later.

        Otherwise, this is called automatically by the bot.
        """
        # from source digging asyncpg source, releasing an already
        # released connection does nothing

        if self._db is not None:
            await self.bot.pool.release(self._db)
            self._db = None

    async def send(self, content=None, **kwargs):
        """ Let's try and override default send. """
        if content and hasattr(content, "__len__") and len(content) > 2000:
            link = await self.bot.mb_client.post(content, syntax="text")
            return await super().send(
                f"Output too long, here it is on Mystb.in: {link}."
            )
        return await super().send(content=content, **kwargs)
