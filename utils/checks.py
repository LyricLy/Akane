"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

from typing import Callable, Dict

from discord.ext import commands

from utils.context import Context


async def check_permissions(
    ctx: Context, perms: Dict[str, bool], *, check: Callable = all
) -> bool:
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    resolved = ctx.channel.permissions_for(ctx.author)
    return check(
        getattr(resolved, name, None) == value for name, value in perms.items()
    )


def has_permissions(*, check: Callable = all, **perms) -> commands.check:
    async def pred(ctx):
        return await check_permissions(ctx, perms, check=check)

    return commands.check(pred)


async def check_guild_permissions(
    ctx: Context, perms: Dict[str, bool], *, check: Callable = all
) -> bool:
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    if ctx.guild is None:
        return False

    resolved = ctx.author.guild_permissions
    return check(
        getattr(resolved, name, None) == value for name, value in perms.items()
    )


def has_guild_permissions(*, check: Callable = all, **perms) -> commands.check:
    async def pred(ctx):
        return await check_guild_permissions(ctx, perms, check=check)

    return commands.check(pred)


# These do not take channel overrides into account
def is_mod() -> commands.check:
    async def pred(ctx: Context) -> bool:
        return await check_guild_permissions(ctx, {"manage_guild": True})

    return commands.check(pred)


def is_admin() -> commands.check:
    async def pred(ctx: Context) -> bool:
        return await check_guild_permissions(ctx, {"administrator": True})

    return commands.check(pred)


def mod_or_permissions(**perms) -> commands.check:
    perms["manage_guild"] = True

    async def predicate(ctx: Context) -> bool:
        return await check_guild_permissions(ctx, perms, check=any)

    return commands.check(predicate)


def admin_or_permissions(**perms) -> commands.check:
    perms["administrator"] = True

    async def predicate(ctx: Context) -> bool:
        return await check_guild_permissions(ctx, perms, check=any)

    return commands.check(predicate)


def is_in_guilds(*guild_ids) -> commands.check:
    def predicate(ctx: Context) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        return guild.id in guild_ids

    return commands.check(predicate)


def can_use_spoiler() -> commands.check:
    def predicate(ctx: Context) -> bool:
        if ctx.guild is None:
            raise commands.BadArgument("Cannot be used in private messages.")

        my_permissions = ctx.channel.permissions_for(ctx.guild.me)
        if not (
            my_permissions.read_message_history
            and my_permissions.manage_messages
            and my_permissions.add_reactions
        ):
            raise commands.BadArgument(
                "Need Read Message History, Add Reactions and Manage Messages "
                "to permission to use this. Sorry if I spoiled you."
            )
        return True

    return commands.check(predicate)
