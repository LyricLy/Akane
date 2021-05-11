"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from collections import namedtuple
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union

import discord
import nhentaio
from aiohttp import BasicAuth
from asyncpg import Connection, Pool, Record
from discord.ext import commands, menus

from utils import cache, checks, db
from utils.context import Context
from utils.formats import to_codeblock
from utils.paginator import RoboPages

if TYPE_CHECKING:
    from bot import Akane

SIX_DIGITS = re.compile(r"\{(\d{5,7})\}")
RATING = {"e": "explicit", "q": "questionable", "s": "safe"}
Booru = namedtuple("Booru", "auth endpoint")


class BlacklistedBooru(commands.CommandError):
    """Error raised when you request a blacklisted tag."""

    def __init__(self, tags: set):
        self.blacklisted_tags = tags
        self.blacklist_tags_fmt = " | ".join(list(tags))
        super().__init__("Bad Booru tags.")

    def __str__(self):
        return f"Found blacklisted tags in query: `{self.blacklist_tags_fmt}`."


class BadNHentaiID(commands.CommandError):
    """Error raised when you request a blacklisted tag."""

    def __init__(self, hentai_id: int, message: str):
        self.nhentai_id = hentai_id
        super().__init__(message)

    def __str__(self):
        return f"Invalid NHentai ID: `{self.nhentai_id}`."


class NHentaiEmbed(discord.Embed):
    @classmethod
    def from_gallery(cls, gallery: nhentaio.Gallery) -> NHentaiEmbed:
        self = cls(title=gallery.title, url=gallery.url)
        self.timestamp = gallery.uploaded
        self.add_field(name="Page count", value=gallery.page_count)
        self.add_field(name="Local name", value="N/A")
        self.add_field(name="# of favourites", value=gallery.favourites)
        self.set_image(url=gallery.cover.url)

        fmt = "Top 5 tags (in order of count):-\n"
        tags = sorted(gallery.tags, key=lambda t: t.count)
        fmt2 = ", ".join(tag.name.title() for tag in tags[:5])

        self.description = fmt + fmt2

        return self


class BooruConfigTable(db.Table, table_name="booru_config"):
    """Database ORM fun."""

    guild_id = db.Column(db.Integer(big=True), primary_key=True)
    blacklist = db.Column(db.Array(db.String()))
    auto_six_digits = db.Column(db.Boolean)


class BooruConfig:
    """Config object per guild."""

    blacklist: set[str]
    auto_six_digits: bool

    __slots__ = ("guild_id", "bot", "record", "blacklist", "auto_six_digits")

    def __init__(self, *, guild_id: int, bot: Akane, record: Optional[Record] = None):
        self.guild_id = guild_id
        self.bot = bot
        self.record = record

        if record:
            self.blacklist = set(record["blacklist"])
            self.auto_six_digits = record["auto_six_digits"]
        else:
            self.blacklist = set()
            self.auto_six_digits = False


class LewdPageSource(menus.ListPageSource):
    """Page source for Menus."""

    def __init__(self, entries: Sequence[Any], *, per_page: int = 1):
        self.entries = entries
        super().__init__(entries, per_page=per_page)

    async def format_page(self, menu: menus.Menu, page):
        return page


class GelbooruEntry:
    """Quick object namespace."""

    def __init__(self, payload: dict):
        """."""
        self.image = True if (payload["width"] != 0) else False
        self.source = payload.get("source")
        self.gb_id = payload.get("id")
        self.rating = RATING.get(payload.get("rating"))
        self.score = payload.get("score")
        self.url = payload.get("file_url")
        self.raw_tags = payload.get("tags")

    @property
    def tags(self) -> List[str]:
        return self.raw_tags.split(" ")


class DanbooruEntry:
    def __init__(self, payload: Dict[str, Optional[Union[str, int, bool]]]) -> None:
        self.ext: str = payload.get("file_ext", "none")
        self.image: bool = True if self.ext in ("png", "jpg", "jpeg", "gif") else False
        self.video: bool = True if self.ext in ("mp4", "gifv", "webm") else False
        self.source: str = payload.get("source")
        self.db_id: int = payload.get("id")
        self.rating: str = RATING.get(payload.get("rating"))
        self.score: int = payload.get("score")
        self.large: bool = payload.get("has_large", False)
        self.file_url: str = payload.get("file_url")
        self.large_url: str = payload.get("large_file_url")
        self.raw_tags: str = payload.get("tag_string")

    @property
    def tags(self) -> List[str]:
        return self.raw_tags.split(" ")

    @property
    def url(self) -> str:
        return self.large_url if self.large else self.file_url


class Lewd(commands.Cog):
    """Lewd cog."""

    def __init__(self, bot: Akane) -> None:
        self.bot = bot
        self.gelbooru_config = Booru(
            BasicAuth(
                bot.config.gelbooru_api["user_id"], bot.config.gelbooru_api["api_key"]
            ),
            "https://gelbooru.com/index.php?page=dapi&s=post&q=index",
        )
        self.danbooru_config = Booru(
            BasicAuth(
                bot.config.danbooru_api["user_id"], bot.config.danbooru_api["api_key"]
            ),
            "https://danbooru.donmai.us/posts.json",
        )

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)

        if isinstance(error, BlacklistedBooru):
            return await ctx.send(error)
        elif isinstance(error, commands.BadArgument):
            return await ctx.send(error)
        elif isinstance(error, commands.NSFWChannelRequired):
            return await ctx.send(
                f"{error.channel} is not a horny channel. No lewdie outside lewdie channels!"
            )
        elif isinstance(error, commands.CommandOnCooldown):
            if ctx.author.id == self.bot.owner_id:
                return await ctx.reinvoke()
            return await ctx.send(
                f"Stop being horny. You're on cooldown for {error.retry_after:.02f}s."
            )

    @cache.cache()
    async def get_booru_config(
        self,
        guild_id: int,
        *,
        connection: Union[Pool, Connection] = None,
    ) -> BooruConfig:
        connection = connection or self.bot.pool
        query = """ SELECT * FROM gelbooru_config WHERE guild_id = $1; """
        record = await connection.fetchrow(query, guild_id)
        return BooruConfig(guild_id=guild_id, bot=self.bot, record=record)

    def _gen_gelbooru_embeds(
        self, payloads: list, config: BooruConfig
    ) -> List[Optional[discord.Embed]]:
        embeds = []
        safe_results: List[GelbooruEntry] = []
        blacklisted_tags = config.blacklist

        for item in payloads:
            new_item = GelbooruEntry(item)

            # blacklist check
            set_blacklist = blacklisted_tags
            set_tags = set(new_item.tags)
            if set_blacklist & set_tags:
                continue
            safe_results.append(new_item)
            safe_results = safe_results[:30]

        for idx, new_item in enumerate(safe_results, start=1):
            embed = discord.Embed(colour=discord.Colour(0x000001))

            if new_item.image:
                embed.set_image(url=new_item.url)
            else:
                # video
                embed.add_field(
                    name="Video - Source:-", value=f"[Click here!]({new_item.url})"
                )

            fmt = f"ID: {new_item.gb_id} | Rating: {new_item.rating.capitalize()}"
            fmt += f"\tResult {idx}/{len(safe_results)}"
            embed.set_footer(text=fmt)

            if new_item.source:
                embed.add_field(name="Source:", value=new_item.source)

            embeds.append(embed)
        return embeds

    def _gen_danbooru_embeds(
        self,
        results: List[Dict[str, Optional[Union[str, int, bool]]]],
        config: BooruConfig,
    ) -> List[Optional[discord.Embed]]:
        embeds = []
        safe_results: List[DanbooruEntry] = []
        blacklist = config.blacklist
        for _result in results:
            result = DanbooruEntry(_result)
            if blacklist & set(result.tags):
                continue
            safe_results.append(result)
            safe_results = safe_results[:30]

        offset = 0
        for idx, result in enumerate(safe_results, start=1):
            embed = discord.Embed(colour=discord.Colour(0xD552C9))
            if result.image:
                embed.set_image(url=result.url)
            elif result.video:
                embed.add_field(
                    name="Video - Source:-", value=f"f[Click here!]({result.url})"
                )
            else:
                offset += 1
                continue

            fmt = f"ID: {result.db_id} | Rating: {result.rating.capitalize()}"
            fmt += f"\tResult {idx}/{len(safe_results)}"
            embed.set_footer(text=fmt)

            if result.source:
                embed.add_field(name="Source:", value=result.source)

            embeds.append(embed)
        return embeds

    @commands.command(usage="<flags>+ | subcommand")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.user, wait=False)
    @commands.is_nsfw()
    async def gelbooru(self, ctx: Context, *, params: str) -> None:
        """This command uses a flag style syntax.
        The following options are valid.

        `*` denotes it is a mandatory argument.

        `+t | ++tags`: The tags to search Gelbooru for. `*` (uses logical AND per tag)
        `+l | ++limit`: The maximum amount of posts to show. Cannot be higher than 30.
        `+p | ++pid`: Page ID to search. Handy when posts begin to repeat.
        `+c | ++cid`: Change ID of the post to search for(?)

        Examples:
        ```
        !gelbooru ++tags lemon
            - search for the 'lemon' tag.
            - NOTE: if your tag has a space in it, replace it with '_'

        !gelbooru ++tags melon -rating:explicit
            - search for the 'melon' tag, removing posts marked as 'explicit`

        !gelbooru ++tags apple orange rating:safe ++pid 2
            - Search for the 'apple' AND 'orange' tags, with only 'safe' results, but on Page 2.
            - NOTE: if not enough searches are returned, page 2 will cause an empty response.
        ```
        """
        aiohttp_params = {}
        aiohttp_params.update({"json": 1})
        parser = argparse.ArgumentParser(
            add_help=False, allow_abbrev=False, prefix_chars="+"
        )
        parser.add_argument("+l", "++limit", type=int, default=40)
        parser.add_argument("+p", "++pid", type=int)
        parser.add_argument("+t", "++tags", nargs="+", required=True)
        parser.add_argument("+c", "++cid", type=int)
        try:
            real_args = parser.parse_args(shlex.split(params))
        except SystemExit as fuck:
            raise commands.BadArgument("Your flags could not be parsed.") from fuck
        except Exception as err:
            await ctx.send(f"Parsing your args failed: {err}")
            return

        current_config = await self.get_booru_config(getattr(ctx.guild, "id", -1))

        if real_args.limit:
            aiohttp_params.update({"limit": int(real_args.limit)})
        if real_args.pid:
            aiohttp_params.update({"pid": real_args.pid})
        if real_args.cid:
            aiohttp_params.update({"cid", real_args.cid})
        lowered_tags = [tag.lower() for tag in real_args.tags]
        tags_set = set(lowered_tags)
        common_elems = tags_set & current_config.blacklist
        if common_elems:
            raise BlacklistedBooru(common_elems)
        aiohttp_params.update({"tags": " ".join(lowered_tags)})

        async with ctx.typing():
            async with self.bot.session.get(
                self.gelbooru_config.endpoint,
                params=aiohttp_params,
                auth=self.gelbooru_config.auth,
            ) as resp:
                data = await resp.text()
                if not data:
                    ctx.command.reset_cooldown(ctx)
                    raise commands.BadArgument("Got an empty response... bad search?")
                json_data = json.loads(data)

            if not json_data:
                ctx.command.reset_cooldown(ctx)
                raise commands.BadArgument("The specified query returned no results.")

            embeds = self._gen_gelbooru_embeds(json_data, current_config)
            if not embeds:
                raise commands.BadArgument(
                    "Your search had results but all of them contain blacklisted tags."
                )
            pages = RoboPages(
                source=LewdPageSource(embeds[:30]),
                delete_message_after=False,
                clear_reactions_after=True,
            )
            await pages.start(ctx)

    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.user, wait=False)
    @commands.is_nsfw()
    async def danbooru(self, ctx: Context, *, params: str) -> None:
        """This command uses a flag style syntax.
        The following options are valid.

        `*` denotes it is a mandatory argument.

        `+t | ++tags`: The tags to search Gelbooru for. `*` (uses logical AND per tag)
        `+l | ++limit`: The maximum amount of posts to show. Cannot be higher than 30.

        Examples:
        ```
        !gelbooru ++tags lemon
            - search for the 'lemon' tag.
            - NOTE: if your tag has a space in it, replace it with '_'

        !danbooru ++tags melon -rating:explicit
            - search for the 'melon' tag, removing posts marked as 'explicit`

        !danbooru ++tags apple orange rating:safe
            - Search for the 'apple' AND 'orange' tags, with only 'safe' results.
        ```
        """
        aiohttp_params = {}
        parser = argparse.ArgumentParser(
            add_help=False, allow_abbrev=False, prefix_chars="+"
        )
        parser.add_argument("+t", "++tags", nargs="+", required=True)
        parser.add_argument("+l", "++limit", type=int, default=40)
        try:
            real_args = parser.parse_args(shlex.split(params))
        except SystemExit as fuck:
            raise commands.BadArgument("Your flags could not be parsed.") from fuck
        except Exception as err:
            await ctx.send(f"Parsing your args failed: {err}.")
            return

        current_config = await self.get_booru_config(getattr(ctx.guild, "id", -1))

        if real_args.limit:
            limit = real_args.limit
            if not 1 < real_args.limit <= 30:
                limit = 30
            aiohttp_params.update({"limit": limit})
        lowered_tags = [tag.lower() for tag in real_args.tags]
        tags = set(lowered_tags)
        common_elems = tags & current_config.blacklist
        if common_elems:
            raise BlacklistedBooru(common_elems)
        aiohttp_params.update({"tags": " ".join(lowered_tags)})

        async with ctx.typing():
            async with self.bot.session.get(
                self.danbooru_config.endpoint,
                params=aiohttp_params,
                auth=self.danbooru_config.auth,
            ) as resp:
                data = await resp.text()
                if not data:
                    ctx.command.reset_cooldown(ctx)
                    raise commands.BadArgument("Got an empty response... bad search?")
                json_data = json.loads(data)

            if not json_data:
                ctx.command.reset_cooldown(ctx)
                raise commands.BadArgument("The specified query returned no results.")

            embeds = self._gen_danbooru_embeds(json_data, current_config)
            if not embeds:
                fmt = (
                    "Your search had results but all of them contained blacklisted tags"
                )
                if "loli" in lowered_tags:
                    fmt += "\nPlease note that Danbooru does not support 'loli'."
                raise commands.BadArgument(fmt)

            pages = RoboPages(
                source=LewdPageSource(embeds[:30]),
                delete_message_after=False,
                clear_reactions_after=True,
            )
            await pages.start(ctx)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(manage_messages=True)
    async def booru(self, ctx: Context) -> None:
        """Booru commands! Please see the subcommands."""
        if not ctx.invoked_subcommand:
            return await ctx.send_help(ctx.command)

    @booru.group(invoke_without_command=True)
    @checks.has_permissions(manage_messages=True)
    async def blacklist(self, ctx: Context) -> None:
        """Blacklist management for gelbooru command."""
        if not ctx.invoked_subcommand:
            config = await self.get_booru_config(ctx.guild.id)
            if config.blacklist:
                fmt = "\n".join(config.blacklist)
            else:
                fmt = "No blacklist recorded."
            embed = discord.Embed(
                description=to_codeblock(fmt, language=""),
                colour=self.bot.colour["dsc"],
            )
            await ctx.send(embed=embed, delete_after=6.0)

    @blacklist.command()
    @checks.has_permissions(manage_messages=True)
    async def add(self, ctx: Context, *tags: str):
        """Add an item to the blacklist."""
        query = """ INSERT INTO gelbooru_config (guild_id, blacklist)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id)
                    DO UPDATE SET blacklist = gelbooru_config.blacklist || $2;
                """
        iterable = [(ctx.guild.id, [tag.lower()]) for tag in tags]
        await self.bot.pool.executemany(query, iterable)
        self.get_booru_config.invalidate(self, ctx.guild.id)
        await ctx.message.add_reaction(self.bot.emoji[True])

    @blacklist.command()
    @checks.has_permissions(manage_messages=True)
    async def remove(self, ctx: Context, *tags: str):
        """Remove an item from the blacklist."""
        query = """ UPDATE gelbooru_config
                    SET blacklist = array_remove(gelbooru_config.blacklist, $2)
                    WHERE guild_id = $1;
                """
        iterable = [(ctx.guild.id, tag) for tag in tags]
        await self.bot.pool.executemany(query, iterable)
        self.get_booru_config.invalidate(self, ctx.guild.id)
        await ctx.message.add_reaction(self.bot.emoji[True])

    @commands.group(invoke_without_command=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.user, wait=False)
    @commands.is_nsfw()
    async def nhentai(self, ctx, hentai_id: int):
        """Naughty. Return info. the cover and links to an nhentai page."""
        gallery: Optional[
            nhentaio.Gallery
        ] = await self.bot.hentai_client.fetch_gallery(hentai_id)

        if not gallery:
            raise BadNHentaiID(hentai_id, "Doesn't seem to be a valid ID.")

        embed = NHentaiEmbed.from_gallery(gallery)
        await ctx.send(embed=embed)

    @nhentai.command(name="toggle")
    @checks.has_guild_permissions(manage_messages=True)
    async def nhentai_toggle(self, ctx: Context) -> None:
        """
        This command will toggle the auto parsing of NHentai IDs in messages in the form of:-
        `{123456}`

        Criteria for parsing:
        - Cannot be done in DM.
        - Must be in an NSFW channel.
        - Must be a user or bot that posts it, no webhooks.
        - If the ID does not match a gallery, it will not respond.

        Toggle will do as it says, switch between True and False. Only when it is True will it parse and respond.
        The reaction added will tell you if it is on (check mark), or off (cross).
        """
        config: BooruConfig = await self.get_booru_config(ctx.guild.id)
        if not config:
            return await ctx.send("No recorded config for this guild.")

        enabled = config.auto_six_digits

        query = """
                --begin-sql
                INSERT INTO gelbooru_config (guild_id, blacklist, auto_six_digits)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id)
                DO UPDATE
                SET auto_six_digits = $4
                WHERE gelbooru_config.guild_id = $1;
                """
        await ctx.bot.pool.execute(query, ctx.guild.id, [], True, not enabled)
        self.get_booru_config.invalidate(self, ctx.guild.id)
        await ctx.message.add_reaction(ctx.bot.emoji[not enabled])

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.webhook_id:
            return

        if not message.channel.is_nsfw():
            return

        config: BooruConfig = await self.get_booru_config(message.guild.id)
        if config.auto_six_digits is False:
            return

        if not (match := SIX_DIGITS.match(message.content)):
            return

        digits = int(match[1])

        try:
            gallery: Optional[
                nhentaio.Gallery
            ] = await self.bot.hentai_client.fetch_gallery(digits)
        except nhentaio.errors.NhentaiError:
            return
        if not gallery:
            return

        tags = set([tag.name for tag in gallery.tags])
        if bl := config.blacklist & tags:
            clean = "|".join(bl)
            return await message.reply(f"This gallery has blacklisted tags: `{clean}`.")

        embed = NHentaiEmbed.from_gallery(gallery)
        await message.reply(embed=embed)


def setup(bot):
    bot.add_cog(Lewd(bot))
