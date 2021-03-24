"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import io
import random
import re
import textwrap
import time
from functools import partial
from string import ascii_lowercase
from textwrap import fill
from typing import TYPE_CHECKING, Optional

import discord
import googletrans
from discord.ext import commands, tasks
from lru import LRU
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from utils import db, lang
from utils.checks import can_use_spoiler
from utils.context import Context
from utils.formats import plural

if TYPE_CHECKING:
    from bot import Akane

ABT_REG = "~([a-zA-Z]+)~"

MENTION_CHANNEL_ID = 722930330897743894
DM_CHANNEL_ID = 722930296756109322
SPOILER_EMOJI_ID = 738038828928860269


class StatisticsTable(db.Table, table_name="statistics"):
    id = db.PrimaryKeyColumn()
    message_deletes = db.Column(db.Integer(big=True))
    bulk_message_deletes = db.Column(db.Integer(big=True))
    message_edits = db.Column(db.Integer(big=True))
    bans = db.Column(db.Integer(big=True))
    unbans = db.Column(db.Integer(big=True))
    channel_deletes = db.Column(db.Integer(big=True))
    channel_creates = db.Column(db.Integer(big=True))
    command_count = db.Column(db.Integer(big=True))


class SpoilerCache:
    __slots__ = ("author_id", "channel_id", "title", "text", "attachments")

    def __init__(self, data):
        self.author_id = data["author_id"]
        self.channel_id = data["channel_id"]
        self.title = data["title"]
        self.text = data["text"]
        self.attachments = data["attachments"]

    def has_single_image(self):
        return self.attachments and self.attachments[0].filename.lower().endswith(
            (".gif", ".png", ".jpg", ".jpeg")
        )

    def to_embed(self, bot):
        embed = discord.Embed(title=f"{self.title} Spoiler", colour=0x01AEEE)
        if self.text:
            embed.description = self.text

        if self.has_single_image():
            if self.text is None:
                embed.title = f"{self.title} Spoiler Image"
            embed.set_image(url=self.attachments[0].url)
            attachments = self.attachments[1:]
        else:
            attachments = self.attachments

        if attachments:
            value = "\n".join(f"[{a.filename}]({a.url})" for a in attachments)
            embed.add_field(name="Attachments", value=value, inline=False)

        user = bot.get_user(self.author_id)
        if user:
            embed.set_author(name=str(user), icon_url=user.avatar_url_as(format="png"))

        return embed

    def to_spoiler_embed(self, ctx, storage_message):
        description = (
            "React with <:QuestionMaybe:738038828928860269> to reveal the spoiler."
        )
        embed = discord.Embed(title=f"{self.title} Spoiler", description=description)
        if self.has_single_image() and self.text is None:
            embed.title = f"{self.title} Spoiler Image"

        embed.set_footer(text=storage_message.id)
        embed.colour = 0x01AEEE
        embed.set_author(
            name=ctx.author, icon_url=ctx.author.avatar_url_as(format="png")
        )
        return embed


class SpoilerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(1, 10.0, commands.BucketType.user))

    def _bucket_key(self, tup):
        return tup

    def is_rate_limited(self, message_id, user_id):
        bucket = self.get_bucket((message_id, user_id))
        return bucket.update_rate_limit() is not None


class Fun(commands.Cog):
    """ Some fun stuff, not fleshed out yet. """

    def __init__(self, bot: Akane):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.message_deletes = 0
        self.bulk_message_deletes = 0
        self.message_edits = 0
        self.bans = 0
        self.unbans = 0
        self.channel_deletes = 0
        self.channel_creates = 0
        self.command_count = 0
        self.bulk_update.start()
        self.translator = googletrans.Translator()
        self._spoiler_cache = LRU(128)
        self._spoiler_cooldown = SpoilerCooldown()

    async def do_ocr(self, url: str) -> Optional[str]:
        async with self.bot.session.get(
            "https://api.tsu.sh/google/ocr", params={"q": url}
        ) as resp:
            # data = await resp.json()
            data = await resp.text()
            return data
        ocr_text = data.get("text")
        ocr_text = (
            ocr_text
            if (len(ocr_text) < 4000)
            else str(await self.bot.mb_client.post(ocr_text))
        )
        return ocr_text

    async def redirect_post(self, ctx, title, text):
        storage = self.bot.get_guild(705500489248145459).get_channel(772045165049020416)

        supported_attachments = (
            ".png",
            ".jpg",
            ".jpeg",
            ".webm",
            ".gif",
            ".mp4",
            ".txt",
        )
        if not all(
            attach.filename.lower().endswith(supported_attachments)
            for attach in ctx.message.attachments
        ):
            raise RuntimeError(
                f'Unsupported file in attachments. Only {", ".join(supported_attachments)} supported.'
            )

        files = []
        total_bytes = 0
        eight_mib = 8 * 1024 * 1024
        for attach in ctx.message.attachments:
            async with ctx.session.get(attach.url) as resp:
                if resp.status != 200:
                    continue

                content_length = int(resp.headers.get("Content-Length"))

                # file too big, skip it
                if (total_bytes + content_length) > eight_mib:
                    continue

                total_bytes += content_length
                fp = io.BytesIO(await resp.read())
                files.append(discord.File(fp, filename=attach.filename))

            if total_bytes >= eight_mib:
                break

        # on mobile, messages that are deleted immediately sometimes persist client side
        await asyncio.sleep(0.2, loop=self.bot.loop)
        await ctx.message.delete()
        data = discord.Embed(title=title)
        if text:
            data.description = text

        data.set_author(name=ctx.author.id)
        data.set_footer(text=ctx.channel.id)

        try:
            message = await storage.send(embed=data, files=files)
        except discord.HTTPException as e:
            raise RuntimeError(
                f"Sorry. Could not store message due to {e.__class__.__name__}: {e}."
            ) from e

        to_dict = {
            "author_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "attachments": message.attachments,
            "title": title,
            "text": text,
        }

        cache = SpoilerCache(to_dict)
        return message, cache

    async def get_spoiler_cache(self, channel_id, message_id):
        try:
            return self._spoiler_cache[message_id]
        except KeyError:
            pass

        storage = self.bot.get_guild(182325885867786241).get_channel(430229522340773899)

        # slow path requires 2 lookups
        # first is looking up the message_id of the original post
        # to get the embed footer information which points to the storage message ID
        # the second is getting the storage message ID and extracting the information from it
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None

        try:
            original_message = await channel.fetch_message(message_id)
            storage_message_id = int(original_message.embeds[0].footer.text)
            message = await storage.fetch_message(storage_message_id)
        except:
            # this message is probably not the proper format or the storage died
            return None

        data = message.embeds[0]
        to_dict = {
            "author_id": int(data.author.name),
            "channel_id": int(data.footer.text),
            "attachments": message.attachments,
            "title": data.title,
            "text": None if not data.description else data.description,
        }
        cache = SpoilerCache(to_dict)
        self._spoiler_cache[message_id] = cache
        return cache

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.emoji.id != SPOILER_EMOJI_ID:
            return

        user = self.bot.get_user(payload.user_id)
        if not user or user.bot:
            return

        if self._spoiler_cooldown.is_rate_limited(payload.message_id, payload.user_id):
            return

        cache = await self.get_spoiler_cache(payload.channel_id, payload.message_id)
        embed = cache.to_embed(self.bot)
        await user.send(embed=embed)

    @commands.command()
    @can_use_spoiler()
    async def spoiler(self, ctx, title, *, text=None):
        """Marks your post a spoiler with a title.

        Once your post is marked as a spoiler it will be
        automatically deleted and the bot will DM those who
        opt-in to view the spoiler.

        The only media types supported are png, gif, jpeg, mp4,
        and webm.

        Only 8MiB of total media can be uploaded at once.
        Sorry, Discord limitation.

        To opt-in to a post's spoiler you must click the reaction.
        """

        if len(title) > 100:
            return await ctx.send("Sorry. Title has to be shorter than 100 characters.")

        try:
            storage_message, cache = await self.redirect_post(ctx, title, text)
        except Exception as e:
            return await ctx.send(str(e))

        spoiler_message = await ctx.send(
            embed=cache.to_spoiler_embed(ctx, storage_message)
        )
        self._spoiler_cache[spoiler_message.id] = cache
        await spoiler_message.add_reaction("<:QuestionMaybe:738038828928860269>")

    @commands.group(invoke_without_command=True, skip_extra=False)
    async def abt(self, ctx, *, content: commands.clean_content):
        """ I love this language. """
        keep = re.findall(ABT_REG, content)

        def trans(m):
            get = m.group(0)
            if get.isupper():
                return lang.ab_charmap[get.lower()].upper()
            return lang.ab_charmap[get]

        repl = re.sub("[a-zA-Z]", trans, content)
        fin = re.sub(ABT_REG, lambda m: keep.pop(0), repl)
        await ctx.send(fin)

    @abt.command(name="r", aliases=["reverse"])
    async def abt_reverse(self, ctx, *, tr_input: str):
        """ Uno reverse. """
        new_str = ""
        br = True
        for char in tr_input:
            if char == "~":
                br = not br
            if br and (char.lower() in ascii_lowercase):
                new_str += [
                    key for key, val in lang.ab_charmap.items() if val == char.lower()
                ][0]
            else:
                new_str += char
        await ctx.send(new_str.replace("~", "").capitalize())

    @commands.command(hidden=True, enabled=False)
    async def ocr(self, ctx, *, image_url: str = None):
        """ Perform an OCR task on an image. """
        if not image_url and not ctx.message.attachments:
            raise commands.BadArgument("Url or attachment required.")
        image_url = image_url or ctx.message.attachments[0].url
        data = await self.do_ocr(image_url) or "No text returned."
        await ctx.send(
            embed=discord.Embed(description=data, colour=self.bot.colour["dsc"])
        )

    @commands.command(hidden=True, enabled=False)
    async def ocrt(self, ctx, *, image_url: str = None):
        """ Perform an OCR and translation on an image. """
        if not image_url and not ctx.message.attachments:
            raise commands.BadArgument("URL or attachment required.")
        image_url = image_url or ctx.message.attachments[0].url
        data = await self.do_ocr(image_url)
        if data:
            return await self.translate(ctx, message=data)
        return await ctx.send("No text returned.")

    @commands.command()
    async def translate(self, ctx, *, message: commands.clean_content):
        """Translates a message to English using Google translate."""
        ret = await self.bot.loop.run_in_executor(
            None, self.translator.translate, message
        )

        embed = discord.Embed(title="Translated", colour=0x000001)
        src = googletrans.LANGUAGES.get(ret.src, "(auto-detected)").title()
        dest = googletrans.LANGUAGES.get(ret.dest, "Unknown").title()
        source_text = ret.origin if len(ret.origin) < 1000 else "Too long to display."
        if len(ret.text) > 1000:
            lines = "\n".join(textwrap.wrap(ret.text))
            url = await self.bot.mb_client.post(lines, syntax="text")
            dest_text = f"[Here!]({url})"
        else:
            dest_text = ret.text
        embed.add_field(name=f"From {src}", value=source_text, inline=False)
        embed.add_field(name=f"To {dest}", value=dest_text, inline=False)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        async with self.lock:
            self.message_deletes += 1

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id in (self.bot.user.id, self.bot.owner_id):
            return
        if self.bot.blacklist.get(message.author.id):
            # Blocked.
            return

        if self.bot.user in message.mentions:
            channel = self.bot.get_channel(MENTION_CHANNEL_ID)
            embed = discord.Embed(title="Akane was mentioned!")
            embed.set_author(
                name=message.author.name, icon_url=message.author.avatar_url
            )
            embed.description = f"{message.content}\n\n[Jump!]({message.jump_url})"
            embed.timestamp = message.created_at
            await channel.send(embed=embed)
        elif not message.guild:
            channel = self.bot.get_channel(DM_CHANNEL_ID)
            embed = discord.Embed(title="Akane was DM'd.")
            embed.set_author(
                name=message.author.name, icon_url=message.author.avatar_url
            )
            embed.description = f"{message.content}"
            embed.timestamp = message.created_at
            await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        async with self.lock:
            self.bulk_message_deletes += 1

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        async with self.lock:
            self.message_edits += 1

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        async with self.lock:
            self.bans += 1

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        async with self.lock:
            self.unbans += 1

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        async with self.lock:
            self.channel_deletes += 1

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        async with self.lock:
            self.channel_creates += 1

    @commands.Cog.listener()
    async def on_command(self, ctx):
        async with self.lock:
            self.command_count += 1

    @tasks.loop(minutes=10)
    async def bulk_update(self):
        await self.bot.wait_until_ready()
        query = """ UPDATE statistics
                    SET message_deletes = message_deletes + $1,
                    bulk_message_deletes = bulk_message_deletes + $2,
                    message_edits = message_edits + $3,
                    bans = bans + $4,
                    unbans = unbans + $5,
                    channel_deletes = channel_deletes + $6,
                    channel_creates = channel_creates + $7,
                    command_count = command_count + $8
                    WHERE id = 1;
                """
        async with self.lock:
            await self.bot.pool.execute(
                query,
                self.message_deletes,
                self.bulk_message_deletes,
                self.message_edits,
                self.bans,
                self.unbans,
                self.channel_deletes,
                self.channel_creates,
                self.command_count,
            )
            self.message_deletes = 0
            self.bulk_message_deletes = 0
            self.message_edits = 0
            self.bans = 0
            self.unbans = 0
            self.channel_deletes = 0
            self.channel_creates = 0
            self.command_count = 0

    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.guild)
    async def statistics(self, ctx):
        """ A small embed with some statistics Akane tracks. """
        query = "SELECT * FROM statistics LIMIT 1;"
        stat_record = await self.bot.pool.fetchrow(query)

        message_deletes = stat_record["message_deletes"] + self.message_deletes
        bulk_message_deletes = (
            stat_record["bulk_message_deletes"] + self.bulk_message_deletes
        )
        message_edits = stat_record["message_edits"] + self.message_edits
        bans = stat_record["bans"] + self.bans
        unbans = stat_record["unbans"] + self.unbans
        channel_deletes = stat_record["channel_deletes"] + self.channel_deletes
        channel_creates = stat_record["channel_creates"] + self.channel_creates
        command_count = stat_record["command_count"] + self.command_count

        embed = discord.Embed(title="Akane Stats")
        embed.description = (
            "Hello! Since 6th of July, 2020, I have witnessed the following events."
        )
        message_str = f"""
        ```prolog
        Message Deletes      : {message_deletes:,}
        Bulk Message Deletes : {bulk_message_deletes:,}
        Message Edits        : {message_edits:,}
        ```
        """
        guild_str = f"""
        ```prolog
        Banned Members       : {bans:,}
        Unbanned Members     : {unbans:,}
        Channel Creation     : {channel_creates:,}
        Channel Deletion     : {channel_deletes:,}
        ```
        """
        embed.add_field(
            name="**Messages**", value=textwrap.dedent(message_str), inline=False
        )
        embed.add_field(
            name="**Guilds**", value=textwrap.dedent(guild_str), inline=False
        )
        embed.set_footer(text=f"I have also run {command_count:,} commands!")

        await ctx.send(embed=embed)

    def _draw_words(self, text: str) -> io.BytesIO:
        """ . """
        text = fill(text, 25)
        font = ImageFont.truetype("static/W6.ttc", 60)
        padding = 50

        images = [Image.new("RGBA", (1, 1), color=0) for _ in range(2)]
        for index, (image, colour) in enumerate(zip(images, ((47, 49, 54), "white"))):
            draw = ImageDraw.Draw(image)
            w, h = draw.multiline_textsize(text, font=font)
            images[index] = image = image.resize((w + padding, h + padding))
            draw = ImageDraw.Draw(image)
            draw.multiline_text(
                (padding / 2, padding / 2), text=text, fill=colour, font=font
            )
        background, foreground = images

        background = background.filter(ImageFilter.GaussianBlur(radius=7))
        background.paste(foreground, (0, 0), foreground)
        buf = io.BytesIO()
        background.save(buf, "png")
        buf.seek(0)
        return buf

    def random_words(self, amount: int) -> str:
        with open("static/words.txt", "r") as fp:
            words = fp.readlines()

        return random.sample(words, amount)

    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    @commands.max_concurrency(1, commands.BucketType.channel, wait=False)
    async def typeracer(self, ctx: Context, amount: int = 5):
        """
        Type racing.

        This command will send an image of words of [amount] length.
        Please type and send this Kana in the same channel to qualify.
        """

        amount = max(min(amount, 50), 1)

        await ctx.send("Type-racing begins in 5 seconds.")
        await asyncio.sleep(5)

        words = self.random_words(amount)
        randomized_words = (" ".join(words)).replace("\n", "").strip().lower()

        func = partial(self._draw_words, randomized_words)
        image = await ctx.bot.loop.run_in_executor(None, func)
        file = discord.File(fp=image, filename="typerace.png")
        await ctx.send(file=file)

        winners = dict()
        is_ended = asyncio.Event()

        start = time.time()

        def check(message: discord.Message) -> bool:
            if (
                message.channel == ctx.channel
                and not message.author.bot
                and message.content.lower() == randomized_words
                and message.author not in winners
            ):
                winners[message.author] = time.time() - start
                is_ended.set()
                ctx.bot.loop.create_task(message.add_reaction(ctx.bot.emoji[True]))

        task = ctx.bot.loop.create_task(ctx.bot.wait_for("message", check=check))

        try:
            await asyncio.wait_for(is_ended.wait(), timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("No participants matched the output.")
        else:
            await ctx.send("Input accepted... Other players have 10 seconds left.")
            await asyncio.sleep(10)
            embed = discord.Embed(
                title=f"{plural(len(winners)):Winner}", colour=discord.Colour.random()
            )
            embed.description = "\n".join(
                f"{idx}: {person.mention} - {time:.4f} seconds for {amount / time * 60:.2f}WPM"
                for idx, (person, time) in enumerate(winners.items(), start=1)
            )

            await ctx.send(embed=embed)
        finally:
            task.cancel()


def setup(bot: Akane):
    bot.add_cog(Fun(bot))
