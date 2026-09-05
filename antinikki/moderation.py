from __future__ import annotations

import asyncio
import datetime as dt
import re

import discord
from discord import app_commands
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


class RoleListView(discord.ui.View):
    def __init__(self, cog: "Moderation", guild: discord.Guild, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.owner_id = owner_id
        self.page = 0
        self.update_buttons()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.guild.roles[1:]) + 9) // 10)

    def update_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who opened this role list can change pages.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.cog.role_list_embed(self.guild, self.page), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.cog.role_list_embed(self.guild, self.page), view=self)


class Moderation(commands.Cog):
    role_slash = app_commands.Group(name="role", description="Manage server roles")
    vc_slash = app_commands.Group(name="vc", description="Manage enforced voice mutes")
    godmode_slash = app_commands.Group(name="godmode", description="Anti-Nuke Admin protection for trusted members")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def allowed(self, ctx: commands.Context) -> bool:
        protection = self.bot.get_cog("AntiNikki")
        allowed = isinstance(ctx.author, discord.Member) and protection is not None and await protection.security_admin(ctx.author)
        if not allowed:
            await ctx.reply("Only the server owner or an Anti-Nuke Admin can use this command.", mention_author=False)
        return allowed

    async def can_manage_godmode(self, guild: discord.Guild, user: discord.Member | discord.User) -> bool:
        protection = self.bot.get_cog("AntiNikki")
        if protection is None or not isinstance(user, discord.Member):
            return user.id == guild.owner_id or user.id in self.bot.settings.owner_ids
        return await protection.security_admin(user)

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

    async def slash_allowed(self, interaction: discord.Interaction) -> bool:
        protection = self.bot.get_cog("AntiNikki")
        allowed = isinstance(interaction.user, discord.Member) and protection is not None and await protection.security_admin(interaction.user)
        if not allowed:
            await interaction.response.send_message("Only the server owner or an Anti-Nuke Admin can use this command.", ephemeral=True)
        return allowed

    async def slash_targetable(self, interaction: discord.Interaction, member: discord.Member) -> bool:
        if not await self.slash_allowed(interaction):
            return False
        guild = interaction.guild
        if guild is None or member.id in {guild.owner_id, self.bot.user.id if self.bot.user else 0}:
            await interaction.response.send_message("The server owner and 1/1 ANTINUKE cannot be targeted.", ephemeral=True)
            return False
        protection = self.bot.get_cog("AntiNikki")
        if protection is not None and await protection.security_admin(member) and interaction.user.id != guild.owner_id:
            await interaction.response.send_message("Only the server owner can moderate another Anti-Nuke Admin.", ephemeral=True)
            return False
        if interaction.user.id != guild.owner_id and member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("You cannot target a member with an equal or higher role.", ephemeral=True)
            return False
        me = guild.me
        if me is None or member.top_role >= me.top_role:
            await interaction.response.send_message("Move the 1/1 ANTINUKE role above that member, then try again.", ephemeral=True)
            return False
        return True

    async def slash_role_allowed(self, interaction: discord.Interaction, role: discord.Role) -> bool:
        if not await self.slash_allowed(interaction):
            return False
        guild = interaction.guild
        me = guild.me if guild else None
        if role.is_default() or role.managed or me is None or role >= me.top_role:
            await interaction.response.send_message("That role is managed, default, or above 1/1 ANTINUKE.", ephemeral=True)
            return False
        if interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
            await interaction.response.send_message("You cannot manage an equal or higher role.", ephemeral=True)
            return False
        return True

    async def slash_record(self, interaction: discord.Interaction, event: str, details: dict[str, object]) -> None:
        await self.bot.db.incident(interaction.guild_id, interaction.user.id, event, "completed", details)

    def role_list_embed(self, guild: discord.Guild, page: int) -> discord.Embed:
        roles = list(reversed(guild.roles[1:]))
        page_count = max(1, (len(roles) + 9) // 10)
        page = min(max(page, 0), page_count - 1)
        selected = roles[page * 10:(page + 1) * 10]
        lines = [f"{role.mention} · `{role.id}`" for role in selected]
        embed = discord.Embed(title="Server Roles", description="\n".join(lines) or "No roles.", color=discord.Color.blurple())
        embed.set_footer(text=f"Page {page + 1}/{page_count} · {len(roles)} roles total")
        return embed

    @app_commands.command(name="hardban", description="Ban a user and delete up to seven days of messages")
    @app_commands.guild_only()
    async def slash_hardban(self, interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided") -> None:
        member = interaction.guild.get_member(user.id)
        if member is not None and not await self.slash_targetable(interaction, member):
            return
        if member is None and not await self.slash_allowed(interaction):
            return
        await interaction.guild.ban(user, reason=f"Hard ban by {interaction.user}: {reason}", delete_message_seconds=604800)
        await self.slash_record(interaction, "hard_ban", {"user_id": user.id, "reason": reason})
        await interaction.response.send_message(f"Hard banned {user.mention} and deleted up to 7 days of messages.", ephemeral=True)

    @app_commands.command(name="softban", description="Delete recent messages and immediately unban the user")
    @app_commands.guild_only()
    async def slash_softban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if not await self.slash_targetable(interaction, member):
            return
        await interaction.guild.ban(member, reason=f"Soft ban by {interaction.user}: {reason}", delete_message_seconds=604800)
        await interaction.guild.unban(member, reason=f"Soft ban completed by {interaction.user}")
        await self.slash_record(interaction, "soft_ban", {"user_id": member.id, "reason": reason})
        await interaction.response.send_message(f"Soft banned {member.mention}; they may rejoin.", ephemeral=True)

    @app_commands.command(name="kick", description="Remove a member from the server")
    @app_commands.guild_only()
    async def slash_kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if not await self.slash_targetable(interaction, member):
            return
        await member.kick(reason=f"Kick by {interaction.user}: {reason}")
        await self.slash_record(interaction, "manual_kick", {"user_id": member.id, "reason": reason})
        await interaction.response.send_message(f"Kicked **{member}**.", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member for up to 28 days")
    @app_commands.guild_only()
    async def slash_timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided") -> None:
        delta = parse_duration(duration)
        if delta is None:
            await interaction.response.send_message("Use a duration like `10m`, `2h`, `3d`, or `1w` (maximum 28 days).", ephemeral=True)
            return
        if not await self.slash_targetable(interaction, member):
            return
        await member.timeout(delta, reason=f"Timeout by {interaction.user}: {reason}")
        await self.slash_record(interaction, "timeout", {"user_id": member.id, "duration": duration, "reason": reason})
        await interaction.response.send_message(f"Timed out {member.mention} for `{duration}`.", ephemeral=True)

    async def slash_force_role(self, interaction: discord.Interaction, member: discord.Member, role_name: str, key: str, reason: str) -> None:
        if not await self.slash_targetable(interaction, member):
            return
        await interaction.response.defer(ephemeral=True)
        role = await self.restriction_role(interaction.guild, role_name)
        await member.add_roles(role, reason=reason)
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        ids = cfg.setdefault(key, [])
        if member.id not in ids:
            ids.append(member.id)
        if key == "muted_users":
            cfg["forced_mutes"] = [user_id for user_id in cfg.get("forced_mutes", []) if user_id != member.id]
        await self.bot.db.set(interaction.guild_id, cfg)
        await self.slash_record(interaction, key.rstrip("s"), {"user_id": member.id, "role_id": role.id})
        await interaction.followup.send(f"{member.mention} is now **{role_name.lower()}**.", ephemeral=True)

    async def slash_release_role(self, interaction: discord.Interaction, member: discord.Member, role_name: str, key: str) -> None:
        if not await self.slash_allowed(interaction):
            return
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is not None:
            await member.remove_roles(role, reason=f"Released by {interaction.user}")
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        cfg[key] = [user_id for user_id in cfg.get(key, []) if user_id != member.id]
        if role_name == "Muted":
            cfg["forced_mutes"] = [user_id for user_id in cfg.get("forced_mutes", []) if user_id != member.id]
        await self.bot.db.set(interaction.guild_id, cfg)
        await interaction.response.send_message(f"Released {member.mention} from **{role_name.lower()}**.", ephemeral=True)

    @app_commands.command(name="jail", description="Apply an enforced Jailed role")
    @app_commands.guild_only()
    async def slash_jail(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        await self.slash_force_role(interaction, member, "Jailed", "jailed_users", f"Jailed by {interaction.user}: {reason}")

    @app_commands.command(name="unjail", description="Remove an enforced Jailed role")
    @app_commands.guild_only()
    async def slash_unjail(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.slash_release_role(interaction, member, "Jailed", "jailed_users")

    @app_commands.command(name="stfu", description="Add the Muted role (does not server-mute voice)")
    @app_commands.guild_only()
    async def slash_stfu(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        await self.slash_force_role(interaction, member, "Muted", "muted_users", f"Muted role added by {interaction.user}: {reason}")

    @app_commands.command(name="unstfu", description="Remove the Muted role")
    @app_commands.guild_only()
    async def slash_unstfu(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.slash_release_role(interaction, member, "Muted", "muted_users")

    @vc_slash.command(name="stfu", description="Server-mute a member and instantly enforce the mute")
    async def slash_vc_stfu(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if not await self.slash_targetable(interaction, member):
            return
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("❌ That member must be connected to a voice channel.", ephemeral=True)
            return
        try:
            await member.edit(mute=True, reason=f"VC stfu by {interaction.user}: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ I need **Mute Members**, and my bot role must be above that member's highest role.", ephemeral=True)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        users = cfg.setdefault("voice_stfu_users", [])
        if member.id not in users:
            users.append(member.id)
        await self.bot.db.set(interaction.guild_id, cfg)
        await self.slash_record(interaction, "voice_stfu", {"user_id": member.id, "reason": reason})
        await interaction.response.send_message(f"🔇 {member.mention} is server-muted and the mute will be enforced.", ephemeral=True)

    @vc_slash.command(name="unstfu", description="Stop enforcing a member's server mute and unmute them")
    async def slash_vc_unstfu(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self.slash_allowed(interaction):
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        cfg["voice_stfu_users"] = [user_id for user_id in cfg.get("voice_stfu_users", []) if user_id != member.id]
        await self.bot.db.set(interaction.guild_id, cfg)
        if member.voice is not None:
            try:
                await member.edit(mute=False, reason=f"VC unstfu by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message("⚠️ Enforcement was removed, but I could not server-unmute them. Check **Mute Members** and role order.", ephemeral=True)
                return
        await interaction.response.send_message(f"🔊 Voice-mute enforcement was removed from {member.mention}.", ephemeral=True)

    @godmode_slash.command(name="set", description="Enable or disable God Mode for a member")
    async def slash_godmode_set(self, interaction: discord.Interaction, member: discord.Member, enabled: bool) -> None:
        if interaction.guild is None or not await self.can_manage_godmode(interaction.guild, interaction.user):
            await interaction.response.send_message("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", ephemeral=True)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        protected = cfg.setdefault("godmode_users", [])
        if enabled and member.id not in protected:
            protected.append(member.id)
        if not enabled:
            protected[:] = [user_id for user_id in protected if user_id != member.id]
        await self.bot.db.set(interaction.guild_id, cfg)
        await interaction.response.send_message(f"🛡️ God Mode is now **{'enabled' if enabled else 'disabled'}** for {member.mention}.", ephemeral=True)

    @godmode_slash.command(name="list", description="List members protected by God Mode")
    async def slash_godmode_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.can_manage_godmode(interaction.guild, interaction.user):
            await interaction.response.send_message("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can view God Mode.", ephemeral=True)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        protected = cfg.get("godmode_users", [])
        value = "\n".join(f"<@{user_id}> (`{user_id}`)" for user_id in protected) or "No members are protected."
        await interaction.response.send_message(embed=discord.Embed(title="God Mode Members", description=value, color=discord.Color.gold()), ephemeral=True)

    @godmode_slash.command(name="add", description="Protect a member with God Mode")
    async def slash_godmode_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None or not await self.can_manage_godmode(interaction.guild, interaction.user):
            await interaction.response.send_message("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", ephemeral=True)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        protected = cfg.setdefault("godmode_users", [])
        if member.id not in protected:
            protected.append(member.id)
        await self.bot.db.set(interaction.guild_id, cfg)
        await interaction.response.send_message(f"🛡️ God Mode enabled for {member.mention}.", ephemeral=True)

    @godmode_slash.command(name="remove", description="Remove God Mode protection from a member")
    async def slash_godmode_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None or not await self.can_manage_godmode(interaction.guild, interaction.user):
            await interaction.response.send_message("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", ephemeral=True)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(interaction.guild_id)
        cfg["godmode_users"] = [user_id for user_id in cfg.get("godmode_users", []) if user_id != member.id]
        await self.bot.db.set(interaction.guild_id, cfg)
        await interaction.response.send_message(f"God Mode disabled for {member.mention}.", ephemeral=True)

    @role_slash.command(name="add", description="Give a role to a member")
    async def slash_role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        if not await self.slash_targetable(interaction, member) or not await self.slash_role_allowed(interaction, role):
            return
        await member.add_roles(role, reason=f"Role added by {interaction.user}")
        await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)

    @role_slash.command(name="remove", description="Remove a role from a member")
    async def slash_role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        if not await self.slash_targetable(interaction, member) or not await self.slash_role_allowed(interaction, role):
            return
        await member.remove_roles(role, reason=f"Role removed by {interaction.user}")
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)

    @role_slash.command(name="create", description="Create a server role")
    async def slash_role_create(self, interaction: discord.Interaction, name: str) -> None:
        if not await self.slash_allowed(interaction):
            return
        role = await interaction.guild.create_role(name=name[:100], reason=f"Role created by {interaction.user}")
        await interaction.response.send_message(f"Created {role.mention}.", ephemeral=True)

    @role_slash.command(name="delete", description="Delete a server role")
    async def slash_role_delete(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        name = role.name
        await role.delete(reason=f"Role deleted by {interaction.user}")
        await interaction.response.send_message(f"Deleted **{name}**.", ephemeral=True)

    @role_slash.command(name="rename", description="Rename a server role")
    async def slash_role_rename(self, interaction: discord.Interaction, role: discord.Role, name: str) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        await role.edit(name=name[:100], reason=f"Role renamed by {interaction.user}")
        await interaction.response.send_message(f"Renamed the role to **{name[:100]}**.", ephemeral=True)

    @role_slash.command(name="color", description="Change a role color using a hex value")
    async def slash_role_color(self, interaction: discord.Interaction, role: discord.Role, hex_color: str) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        try:
            color = discord.Color(int(hex_color.lstrip("#"), 16))
        except ValueError:
            await interaction.response.send_message("Use a hex color such as `#5865F2`.", ephemeral=True)
            return
        await role.edit(color=color, reason=f"Role color changed by {interaction.user}")
        await interaction.response.send_message(f"Updated {role.mention} to `{color}`.", ephemeral=True)

    @role_slash.command(name="icon", description="Set a role icon using an image or emoji")
    async def slash_role_icon(self, interaction: discord.Interaction, role: discord.Role, image: discord.Attachment | None = None, emoji: str | None = None) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        if image is not None:
            if image.size > 262_144 or not image.content_type or not image.content_type.startswith("image/"):
                await interaction.response.send_message("Upload a PNG, JPG, or WEBP image smaller than 256 KB.", ephemeral=True)
                return
            icon: bytes | str | None = await image.read()
        else:
            icon = emoji
        await role.edit(display_icon=icon, reason=f"Role icon changed by {interaction.user}")
        await interaction.response.send_message(f"Updated the icon for {role.mention}.", ephemeral=True)

    @role_slash.command(name="hoist", description="Show or hide a role separately in the member list")
    async def slash_role_hoist(self, interaction: discord.Interaction, role: discord.Role, enabled: bool) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        await role.edit(hoist=enabled, reason=f"Role hoist changed by {interaction.user}")
        await interaction.response.send_message(f"Role hoist is now **{'on' if enabled else 'off'}**.", ephemeral=True)

    @role_slash.command(name="mentionable", description="Set whether everyone may mention a role")
    async def slash_role_mentionable(self, interaction: discord.Interaction, role: discord.Role, enabled: bool) -> None:
        if not await self.slash_role_allowed(interaction, role):
            return
        await role.edit(mentionable=enabled, reason=f"Role mentionability changed by {interaction.user}")
        await interaction.response.send_message(f"Role mentionability is now **{'on' if enabled else 'off'}**.", ephemeral=True)

    @role_slash.command(name="info", description="Show information about a role")
    async def slash_role_info(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self.slash_allowed(interaction):
            return
        embed = discord.Embed(title=role.name, color=role.color)
        embed.description = f"ID: `{role.id}`\nMembers: `{len(role.members)}`\nPosition: `{role.position}`\nManaged: `{role.managed}`\nMentionable: `{role.mentionable}`\nHoisted: `{role.hoist}`"
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @role_slash.command(name="list", description="List the server roles")
    async def slash_role_list(self, interaction: discord.Interaction) -> None:
        if not await self.slash_allowed(interaction):
            return
        view = RoleListView(self, interaction.guild, interaction.user.id)
        await interaction.response.send_message(embed=self.role_list_embed(interaction.guild, 0), view=view, ephemeral=True)

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
            create_private_threads=False, add_reactions=False,
        )
        if name == "Jailed":
            denied.speak = False
            denied.stream = False
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
        if key == "muted_users":
            cfg["forced_mutes"] = [user_id for user_id in cfg.get("forced_mutes", []) if user_id != member.id]
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
        if role_name == "Muted":
            cfg["forced_mutes"] = [user_id for user_id in cfg.get("forced_mutes", []) if user_id != member.id]
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
        await self.force_role(ctx, member, "Muted", "muted_users", f"Muted role added by {ctx.author}: {reason}")

    @commands.command(name="unstfu", aliases=["unmute"])
    @commands.guild_only()
    async def unstfu(self, ctx: commands.Context, member: discord.Member) -> None:
        await self.release_role(ctx, member, "Muted", "muted_users")

    @commands.group(name="vc", invoke_without_command=True)
    @commands.guild_only()
    async def vc(self, ctx: commands.Context) -> None:
        if await self.allowed(ctx):
            await ctx.reply(f"Use `{ctx.prefix}vc stfu @user [reason]` or `{ctx.prefix}vc unstfu @user`.", mention_author=False)

    @vc.command(name="stfu")
    async def vc_stfu(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        if not await self.targetable(ctx, member):
            return
        if member.voice is None or member.voice.channel is None:
            await ctx.reply("❌ That member must be connected to a voice channel.", mention_author=False)
            return
        try:
            await member.edit(mute=True, reason=f"VC stfu by {ctx.author}: {reason}")
        except discord.Forbidden:
            await ctx.reply("⚠️ I need **Mute Members**, and my bot role must be above that member's highest role.", mention_author=False)
            return
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        users = cfg.setdefault("voice_stfu_users", [])
        if member.id not in users:
            users.append(member.id)
        await self.bot.db.set(ctx.guild.id, cfg)
        await self.record(ctx, "voice_stfu", {"user_id": member.id, "reason": reason})
        await ctx.reply(f"🔇 {member.mention} is server-muted and the mute will be enforced.", mention_author=False)

    @vc.command(name="unstfu", aliases=["unmute"])
    async def vc_unstfu(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await self.allowed(ctx):
            return
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        cfg["voice_stfu_users"] = [user_id for user_id in cfg.get("voice_stfu_users", []) if user_id != member.id]
        await self.bot.db.set(ctx.guild.id, cfg)
        if member.voice is not None:
            try:
                await member.edit(mute=False, reason=f"VC unstfu by {ctx.author}")
            except discord.Forbidden:
                await ctx.reply("⚠️ Enforcement was removed, but I could not server-unmute them. Check **Mute Members** and role order.", mention_author=False)
                return
        await ctx.reply(f"🔊 Voice-mute enforcement was removed from {member.mention}.", mention_author=False)

    @commands.group(name="godmode", aliases=["god"], invoke_without_command=True)
    @commands.guild_only()
    async def godmode(self, ctx: commands.Context) -> None:
        if not await self.can_manage_godmode(ctx.guild, ctx.author):
            await ctx.reply("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", mention_author=False)
            return
        await ctx.reply(f"Use `{ctx.prefix}godmode add @user`, `{ctx.prefix}godmode remove @user`, or `{ctx.prefix}godmode list`.", mention_author=False)

    @godmode.command(name="add", aliases=["on", "enable"])
    async def godmode_add(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await self.can_manage_godmode(ctx.guild, ctx.author):
            await ctx.reply("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", mention_author=False); return
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        protected = cfg.setdefault("godmode_users", [])
        if member.id not in protected:
            protected.append(member.id)
        await self.bot.db.set(ctx.guild.id, cfg)
        await ctx.reply(f"🛡️ God Mode enabled for {member.mention}.", mention_author=False)

    @godmode.command(name="remove", aliases=["off", "disable"])
    async def godmode_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await self.can_manage_godmode(ctx.guild, ctx.author):
            await ctx.reply("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can change God Mode.", mention_author=False); return
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        cfg["godmode_users"] = [user_id for user_id in cfg.get("godmode_users", []) if user_id != member.id]
        await self.bot.db.set(ctx.guild.id, cfg)
        await ctx.reply(f"God Mode disabled for {member.mention}.", mention_author=False)

    @godmode.command(name="list")
    async def godmode_list(self, ctx: commands.Context) -> None:
        if not await self.can_manage_godmode(ctx.guild, ctx.author):
            await ctx.reply("❌ Only the server owner, an OWNER_IDS user, or an Anti-Nuke Admin can view God Mode.", mention_author=False); return
        cfg = await self.bot.get_cog("AntiNikki").config(ctx.guild.id)
        protected = cfg.get("godmode_users", [])
        value = "\n".join(f"<@{user_id}> (`{user_id}`)" for user_id in protected) or "No members are protected."
        await ctx.reply(embed=discord.Embed(title="God Mode Members", description=value, color=discord.Color.gold()), mention_author=False)

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
        view = RoleListView(self, ctx.guild, ctx.author.id)
        await ctx.reply(embed=self.role_list_embed(ctx.guild, 0), view=view, mention_author=False)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        cfg = await self.bot.get_cog("AntiNikki").config(after.guild.id)
        if after.id in cfg.get("godmode_users", []):
            removed = [role for role in before.roles if role not in after.roles and not role.is_default() and not role.managed and role < after.guild.me.top_role]
            try:
                if removed:
                    await after.add_roles(*removed, reason="1/1 ANTINUKE God Mode role restoration")
                if after.timed_out_until is not None:
                    await after.timeout(None, reason="1/1 ANTINUKE God Mode timeout reversal")
            except discord.HTTPException:
                pass
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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot or after.channel is None:
            return
        cfg = await self.bot.get_cog("AntiNikki").config(member.guild.id)
        if member.id in cfg.get("godmode_users", []) and (after.mute or after.deaf):
            try:
                await member.edit(mute=False, deafen=False, reason="1/1 ANTINUKE God Mode voice protection")
            except discord.HTTPException:
                pass
            return
        if after.mute:
            return
        if member.id not in cfg.get("voice_stfu_users", []):
            return
        try:
            await member.edit(mute=True, reason="1/1 ANTINUKE enforced VC stfu")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        cfg = await self.bot.get_cog("AntiNikki").config(guild.id)
        if user.id not in cfg.get("godmode_users", []):
            return
        try:
            await guild.unban(user, reason="1/1 ANTINUKE God Mode automatic unban")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = await self.bot.get_cog("AntiNikki").config(member.guild.id)
        if member.id not in cfg.get("godmode_users", []):
            return
        await asyncio.sleep(1)
        kicked = False
        try:
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == member.id and (discord.utils.utcnow() - entry.created_at).total_seconds() < 10:
                    kicked = True
                    break
        except discord.HTTPException:
            return
        if not kicked:
            return
        channel = next((c for c in member.guild.text_channels if c.permissions_for(member.guild.me).create_instant_invite), None)
        if channel is None:
            return
        try:
            invite = await channel.create_invite(max_age=86400, max_uses=1, unique=True, reason="God Mode kick recovery")
            await member.send(f"You were kicked from **{member.guild.name}**. God Mode cannot force an account back into a server, so here is a one-use rejoin link: {invite.url}")
        except discord.HTTPException:
            pass
