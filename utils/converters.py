"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import re
from typing import Any

import yarl
from discord.ext import commands


class MemeDict(dict):
    def __getitem__(self, k: str) -> Any:
        for key in self:
            if k in key:
                return super().__getitem__(key)
        raise KeyError(k)


class RedditMediaURL:
    VALID_PATH = re.compile(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/.+)?")

    def __init__(self, url):
        self.url = url
        self.filename = url.parts[1] + ".mp4"

    @classmethod
    async def convert(cls, ctx, argument):
        try:
            url = yarl.URL(argument)
        except Exception:
            raise commands.BadArgument("Not a valid URL.")

        headers = {"User-Agent": "Discord:RoboDanny:v4.0 (by /u/Rapptz)"}
        await ctx.trigger_typing()
        if url.host == "v.redd.it":
            # have to do a request to fetch the 'main' URL.
            async with ctx.session.get(url, headers=headers) as resp:
                url = resp.url

        is_valid_path = url.host.endswith(".reddit.com") and cls.VALID_PATH.match(
            url.path
        )
        if not is_valid_path:
            raise commands.BadArgument("Not a reddit URL.")

        # Now we go the long way
        async with ctx.session.get(url / ".json", headers=headers) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f"Reddit API failed with {resp.status}.")

            data = await resp.json()
            try:
                submission = data[0]["data"]["children"][0]["data"]
            except (KeyError, TypeError, IndexError):
                raise commands.BadArgument("Could not fetch submission.")

            try:
                media = submission["media"]["reddit_video"]
            except (KeyError, TypeError):
                try:
                    # maybe it's a cross post
                    crosspost = submission["crosspost_parent_list"][0]
                    media = crosspost["media"]["reddit_video"]
                except (KeyError, TypeError, IndexError):
                    raise commands.BadArgument("Could not fetch media information.")

            try:
                fallback_url = yarl.URL(media["fallback_url"])
            except KeyError:
                raise commands.BadArgument("Could not fetch fall back URL.")

            return cls(fallback_url)
