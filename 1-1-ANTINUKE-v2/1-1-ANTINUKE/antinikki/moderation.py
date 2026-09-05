from __future__ import annotations

import datetime as dt
import re

import discord
from discord.ext import commands


def parse_duration(value: str) -> dt.timedelta | None:
    match = re.fullmatch(r"(\d+)([smhdw])", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    if seconds < 1 or seconds > 2_419_200:
        return None
    return dt.timedelta(seconds=seconds)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def allowed(self, ctx: commands.Context) -> bool:
        protection = self.bot.get_cog("AntiNikki")
        allowed = isinstance(ctx.author, discord.Member) and protection is not None and await protection.security_admin(ctx.author)
        if not allowed:
            await ctx.reply("Only the server owner or an Anti-Nuke Admin can use this command.", mention_author=False)
        return allowed

    async def targetable(self, ctx: commands.Context, member: discord.Member) -> bool:
        if not await self.allowed(ctx):
            return False
        if member.id in {ctx.guild.owner_id, self.bot.user.id if self.bot.user else 0}:
            await ctx.reply("The server owner and 1/1 ANTINUKE cannot be targeted.", mention_author=False)
            return False
        protection = self.bot.get_cog("AntiNikki")
        if protection is not None and await protection.security_admin(member) and ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("Only the server owner can moderate another Anti-Nuke Admin.", mention_author=False)
            return False
        if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
            await ctx.reply("You cannot target a member with an equal or higher role.", mention_author=False)
            return False
        me = ctx.guild.me
        if me is None or member.top_role >= me.top_role:
            await ctx.reply("Move the 1/1 ANTINUKE role above that member, then try again.", mention_author=False)
            return False
        return True

    async def manageable_role(self, ctx: commands.Context, role: discord.Role) -> bool:
        if not await self.allowed(ctx):
            return False
        me = ctx.guild.me
        if role.is_default() or role.managed or me is None or role >= me.top_role:
            await ctx.reply("That role is managed, default, or above 1/1 ANTINUKE.", mention_author=False)
            return False
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            await ctx.reply("You cannot manage an equal or higher role.", mention_author=False)
            return False
        return True

    async def record(self, ctx: commands.Context, event: str, details: dict[str, object]) -> None:
        await self.bot.db.incident(ctx.guild.id, ctx.author.id, event, "completed", details)

    @commands.command(name="hardban", aliases=["hban"])
    @commands.guild_only()
    async def hardban(self, ctx: commands.Context, target: discord.User | int, *, reason: str = "No reason provided") -> None:
        member = ctx.guild.get_member(target.id if isinstance(target, discord.User) else target)
        if member is not None and not await self.targetable(ctx, member):
            return
        if member is None and not await self.allowed(ctx):
            return
        user = target if isinstance(target, discord.User) else discord.Object(id=target)
        await ctx.guild.ban(user, reason=f"Hard ban by {ctx.author}: {reason}", delete_message_seconds=604800)
        await self.record(ctx, "hard_ban", {"user_id": user.id, "reason": reason})
        await ctx.reply(f"Hard banned <@{user.id}> and deleted up to 7 days of messages.", mention_author=False)

    @commands.command(name="softban", aliases=["sban"])
    @commands.guild_only()
    async def softban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        if not await self.targetable(ctx, member):
            return
        await ctx.guild.ban(member, reason=f"Soft ban by {ctx.author}: {reason}", delete_message_seconds=604800)
        await ctx.guild.unban(member, reason=f"Soft ban completed by {ctx.author}")
        await self.record(ctx, "soft_ban", {"user_id": member.id, "reason": reason})
        await ctx.reply(f"Soft banned {member.mention}; their recent messages were removed and they may rejoin.", mention_author=False)

    @commands.command(name="kick")
    @commands.guild_only()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        if not await self.targetable(ctx, member):
            return
        await member.kick(reason=f"Kick by {ctx.author}: {reason}")
        await self.record(ctx, "manual_kick", {"user_id": member.id, "reason": reason})
        await ctx.reply(f"Kicked **{member}**.", mention_author=False)

    @commands.command(name="timeout", aliases=["to"])
    @commands.guild_only()
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided") -> None:
        delta = parse_duration(duration)
        if delta is None:
            await ctx.reply("Use a duration like `10m`, `2h`, `3d`, or `1w` (maximum 28 days).", mention_author=False)
            return
        if not await self.targetable(ctx, member):
            return
        await member.timeout(delta, reason=f"Timeout by {ctx.author}: {reason}")
        await self.record(ctx, "timeout", {"user_id": member.id, "duration": duration, "reason": reason})
        await ctx.reply(f"Timed out {member.mention} for `{duration}`.", mention_author=False)

    async def restriction_role(self, guild: discord.Guild, name: str) -> discord.Role:
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, reason="1/1 ANTINUKE moderation setup")
        denied = discord.PermissionOverwrite(
            send_messages=False, send_messages_in_threads=False, create_public_threads=False,
            create_private_threads=False, add_reactions=False, speak=False, stream=False,
        )
        for channel in guild.channels:
            try:
                await channel.set_permissions(role, overwrite=denied, reason="1/1 ANTINUKE restriction role")
            except discord.HTTPException:
                continue
        return role

    async def force_role(self, ctx: commands.Context, member: discord.Member, role_name: str, key: str, reason: str) -> None:
        if not await self.targetable(ctx, member):
            return
        role = await self.restriction_role(ctx.guild, role_name)
        await member.add_roles(role, reason=reason)
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        ids = cfg.setdefault(key, [])
        if member.id not in ids:
            ids.append(member.id)
        await self.bot.db.set(ctx.guild.id, cfg)
        await self.record(ctx, key.rstrip("s"), {"user_id": member.id, "role_id": role.id})
        await ctx.reply(f"{member.mention} is now **{role_name.lower()}**.", mention_author=False)

    async def release_role(self, ctx: commands.Context, member: discord.Member, role_name: str, key: str) -> None:
        if not await self.allowed(ctx):
            return
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role is not None:
            await member.remove_roles(role, reason=f"Released by {ctx.author}")
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        cfg[key] = [user_id for user_id in cfg.get(key, []) if user_id != member.id]
        await self.bot.db.set(ctx.guild.id, cfg)
        await ctx.reply(f"Released {member.mention} from **{role_name.lower()}**.", mention_author=False)

    @commands.command(name="jail")
    @commands.guild_only()
    async def jail(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        await self.force_role(ctx, member, "Jailed", "jailed_users", f"Jailed by {ctx.author}: {reason}")

    @commands.command(name="unjail")
    @commands.guild_only()
    async def unjail(self, ctx: commands.Context, member: discord.Member) -> None:
        await self.release_role(ctx, member, "Jailed", "jailed_users")

    @commands.command(name="stfu")
    @commands.guild_only()
    async def stfu(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        await self.force_role(ctx, member, "Muted", "forced_mutes", f"Enforced mute by {ctx.author}: {reason}")

    @commands.command(name="unstfu", aliases=["unmute"])
    @commands.guild_only()
    async def unstfu(self, ctx: commands.Context, member: discord.Member) -> None:
        await self.release_role(ctx, member, "Muted", "forced_mutes")

    @commands.group(name="role", invoke_without_command=True)
    @commands.guild_only()
    async def role(self, ctx: commands.Context) -> None:
        if await self.allowed(ctx):
            await ctx.reply(f"Use `{ctx.prefix}role add/remove/create/delete/rename/color/icon/hoist/mentionable/info/list`.", mention_author=False)

    @role.command(name="add", aliases=["give"])
    async def role_add(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        if not await self.targetable(ctx, member) or not await self.manageable_role(ctx, role):
            return
        await member.add_roles(role, reason=f"Role added by {ctx.author}")
        await ctx.reply(f"Added {role.mention} to {member.mention}.", mention_author=False)

    @role.command(name="remove", aliases=["take"])
    async def role_remove(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        if not await self.targetable(ctx, member) or not await self.manageable_role(ctx, role):
            return
        await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        await ctx.reply(f"Removed {role.mention} from {member.mention}.", mention_author=False)

    @role.command(name="create")
    async def role_create(self, ctx: commands.Context, *, name: str) -> None:
        if not await self.allowed(ctx):
            return
        role = await ctx.guild.create_role(name=name[:100], reason=f"Role created by {ctx.author}")
        await ctx.reply(f"Created {role.mention}.", mention_author=False)

    @role.command(name="delete")
    async def role_delete(self, ctx: commands.Context, role: discord.Role) -> None:
        if not await self.manageable_role(ctx, role):
            return
        name = role.name
        await role.delete(reason=f"Role deleted by {ctx.author}")
        await ctx.reply(f"Deleted **{name}**.", mention_author=False)

    @role.command(name="rename")
    async def role_rename(self, ctx: commands.Context, role: discord.Role, *, name: str) -> None:
        if not await self.manageable_role(ctx, role):
            return
        await role.edit(name=name[:100], reason=f"Role renamed by {ctx.author}")
        await ctx.reply(f"Renamed the role to **{name[:100]}**.", mention_author=False)

    @role.command(name="color", aliases=["colour"])
    async def role_color(self, ctx: commands.Context, role: discord.Role, color: discord.Color) -> None:
        if not await self.manageable_role(ctx, role):
            return
        await role.edit(color=color, reason=f"Role color changed by {ctx.author}")
        await ctx.reply(f"Updated {role.mention} to `{color}`.", mention_author=False)

    @role.command(name="hoist")
    async def role_hoist(self, ctx: commands.Context, role: discord.Role, value: bool) -> None:
        if not await self.manageable_role(ctx, role):
            return
        await role.edit(hoist=value, reason=f"Role hoist changed by {ctx.author}")
        await ctx.reply(f"Role hoist is now **{'on' if value else 'off'}**.", mention_author=False)

    @role.command(name="mentionable")
    async def role_mentionable(self, ctx: commands.Context, role: discord.Role, value: bool) -> None:
        if not await self.manageable_role(ctx, role):
            return
        await role.edit(mentionable=value, reason=f"Role mentionability changed by {ctx.author}")
        await ctx.reply(f"Role mentionability is now **{'on' if value else 'off'}**.", mention_author=False)

    @role.command(name="icon")
    async def role_icon(self, ctx: commands.Context, role: discord.Role, *, emoji: str | None = None) -> None:
        if not await self.manageable_role(ctx, role):
            return
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > 262_144 or not attachment.content_type or not attachment.content_type.startswith("image/"):
                await ctx.reply("Attach a PNG, JPG, or WEBP image smaller than 256 KB.", mention_author=False)
                return
            await role.edit(display_icon=await attachment.read(), reason=f"Role icon changed by {ctx.author}")
        elif emoji:
            await role.edit(display_icon=emoji, reason=f"Role icon changed by {ctx.author}")
        else:
            await role.edit(display_icon=None, reason=f"Role icon removed by {ctx.author}")
        await ctx.reply(f"Updated the icon for {role.mention}.", mention_author=False)

    @role.command(name="info")
    async def role_info(self, ctx: commands.Context, role: discord.Role) -> None:
        if not await self.allowed(ctx):
            return
        embed = discord.Embed(title=role.name, color=role.color)
        embed.description = f"ID: `{role.id}`\nMembers: `{len(role.members)}`\nPosition: `{role.position}`\nManaged: `{role.managed}`\nMentionable: `{role.mentionable}`\nHoisted: `{role.hoist}`"
        await ctx.reply(embed=embed, mention_author=False)

    @role.command(name="list")
    async def role_list(self, ctx: commands.Context) -> None:
        if not await self.allowed(ctx):
            return
        lines = [f"{role.mention} · `{role.id}`" for role in reversed(ctx.guild.roles[1:])]
        await ctx.reply(embed=discord.Embed(title="Server Roles", description="\n".join(lines)[:4000] or "No roles.", color=discord.Color.blurple()), mention_author=False)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        cfg = await self.bot.get_cog("AntiNikki").config(after.guild.id)
        required = []
        if after.id in cfg.get("forced_mutes", []):
            required.append("Muted")
        if after.id in cfg.get("jailed_users", []):
            required.append("Jailed")
        for name in required:
            role = discord.utils.get(after.guild.roles, name=name)
            if role is not None and role not in after.roles:
                try:
                    await after.add_roles(role, reason=f"1/1 ANTINUKE enforced {name.lower()}")
                except discord.HTTPException:
                    pass
