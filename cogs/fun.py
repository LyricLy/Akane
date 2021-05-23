"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from __future__ import annotations

import asyncio
import io
import math
import random
import re
import textwrap
import time
from functools import partial
from string import ascii_lowercase
from textwrap import fill
from typing import TYPE_CHECKING, List, Optional

import discord
import googletrans
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from utils import checks, db, lang
from utils.context import Context
from utils.converters import RedditMediaURL
from utils.formats import plural

if TYPE_CHECKING:
    from bot import Akane

ABT_REG = re.compile(r"~([a-zA-Z]+)~")
MESSAGE_LINK_RE = re.compile(
    r"^(?:https?://)(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(?P<guild>\d{16,20})/(?P<channel>\d{16,20})/(?P<message>\d{16,20})/?$"
)

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


class Fun(commands.Cog):
    """Some fun stuff, not fleshed out yet."""

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

    # @commands.Cog.listener("on_message")
    async def quote(self, message: discord.Message) -> None:
        """ """
        if message.author.bot or message.embeds or message.guild is None:
            return

        perms = message.channel.permissions_for(message.guild.me)
        if perms.send_messages is False or perms.embed_links is False:
            return

        if not (
            match := re.search(
                MESSAGE_LINK_RE,
                message.content,
            )
        ):
            return

        data = match.groupdict()
        guild_id = int(data["guild"])
        channel_id = int(data["channel"])
        message_id = int(data["message"])

        if guild_id != message.guild.id:
            return

        channel = message.guild.get_channel(channel_id)
        if channel is None:
            # deleted or private?
            return

        try:
            quote_message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            # Bot has no access I guess.
            return

        embed = discord.Embed(
            title=f"Quote from {quote_message.author} in {channel.name}"
        )
        embed.set_author(
            name=quote_message.author.name, icon_url=quote_message.author.avatar_url
        )
        embed.description = quote_message.content or "No message content."
        fmt = "This message had:\n"
        if quote_message.embeds:
            fmt += "one or more Embeds\n"
        if quote_message.attachments:
            fmt += "one or more Attachments\n"

        if len(fmt.split("\n")) >= 3:
            embed.add_field(name="Also...", value=fmt)

        embed.timestamp = quote_message.created_at

        await message.channel.send(embed=embed)

    @commands.group(invoke_without_command=True, skip_extra=False)
    async def abt(self, ctx, *, content: commands.clean_content):
        """I love this language."""
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
        """Uno reverse."""
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
        """A small embed with some statistics Akane tracks."""
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

    @commands.command(usage="<url>")
    @commands.cooldown(1, 5.0, commands.BucketType.member)
    async def vreddit(self, ctx, *, reddit: RedditMediaURL):
        """Downloads a v.redd.it submission.
        Regular reddit URLs or v.redd.it URLs are supported.
        """

        filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
        async with ctx.session.get(reddit.url) as resp:
            if resp.status != 200:
                return await ctx.send("Could not download video.")

            if int(resp.headers["Content-Length"]) >= filesize:
                return await ctx.send("Video is too big to be uploaded.")

            data = await resp.read()
            await ctx.send(
                file=discord.File(io.BytesIO(data), filename=reddit.filename)
            )

    def _draw_words(self, text: str) -> io.BytesIO:
        """."""
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
                f"{idx}: {person.mention} - {time:.4f} seconds for {len(randomized_words) / time * 12:.2f}WPM"
                for idx, (person, time) in enumerate(winners.items(), start=1)
            )

            await ctx.send(embed=embed)
        finally:
            task.cancel()

    def safe_chan(
        self, member: discord.Member, channels: List[discord.VoiceChannel]
    ) -> Optional[discord.VoiceChannel]:
        """ """
        random.shuffle(channels)
        for channel in channels:
            if channel.permissions_for(member).connect:
                return channel
        return None

    @commands.command(hidden=True, name="scatter", aliases=["scattertheweak"])
    @checks.has_guild_permissions(administrator=True)
    async def scatter(self, ctx: Context) -> None:
        """ """
        if ctx.author.voice is None:
            return

        members = ctx.author.voice.channel.members
        for member in members:
            target = self.safe_chan(member, ctx.guild.voice_channels)
            if target is None:
                continue
            await member.move_to(target)

    @commands.command(hidden=True, name="snap")
    @checks.has_guild_permissions(administrator=True)
    async def snap(self, ctx: Context) -> None:
        """ """
        members = []
        for vc in ctx.guild.voice_channels:
            members.extend(vc.members)

        upper = math.ceil(len(members) / 2)
        choices = random.choices(members, k=upper)

        for m in choices:
            await m.move_to(None)


def setup(bot: Akane):
    bot.add_cog(Fun(bot))
