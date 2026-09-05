from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks


log = logging.getLogger("antinikki.voice")


def ids(value: str | None) -> set[int]:
    try:
        return {int(x) for x in json.loads(value or "[]")}
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def dump_ids(value: set[int]) -> str:
    return json.dumps(sorted(value))


class MemberPicker(discord.ui.View):
    def __init__(self, cog: "VoiceMaster", action: str, owner_id: int, banned: set[int] | None = None) -> None:
        super().__init__(timeout=60)
        self.cog, self.action, self.owner_id = cog, action, owner_id
        picker = discord.ui.UserSelect(placeholder="Choose a server member", min_values=1, max_values=1)
        if action == "unban" and banned:
            picker.placeholder = "Choose a previously banned member"
            picker.default_values = []
        picker.callback = self.selected
        self.add_item(picker)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ This selection menu belongs to somebody else.", ephemeral=True)
            return False
        return True

    async def selected(self, interaction: discord.Interaction) -> None:
        picker = self.children[0]
        assert isinstance(picker, discord.ui.UserSelect)
        await self.cog.member_action(interaction, self.action, picker.values[0])
        self.stop()


class RenameModal(discord.ui.Modal, title="Rename Voice Channel"):
    name = discord.ui.TextInput(label="New Channel Name", min_length=1, max_length=100)

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.rename(interaction, str(self.name).strip())


class LimitModal(discord.ui.Modal, title="Voice Channel User Limit"):
    limit = discord.ui.TextInput(label="0 = Unlimited, or 1–99", min_length=1, max_length=2)

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.limit).strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            await interaction.response.send_message("❌ Enter a whole number from 0 to 99.", ephemeral=True)
            return
        await self.cog.set_limit(interaction, int(raw))


class VoiceMasterPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def run(self, interaction: discord.Interaction, action: str) -> None:
        cog = interaction.client.get_cog("VoiceMaster")
        if cog is None:
            await interaction.response.send_message("⚠️ VoiceMaster is starting. Try again shortly.", ephemeral=True)
            return
        await cog.panel_action(interaction, action)

    @discord.ui.button(label="▣", style=discord.ButtonStyle.secondary, row=0, custom_id="vm:lock")
    async def lock(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "lock")
    @discord.ui.button(label="□", style=discord.ButtonStyle.secondary, row=0, custom_id="vm:unlock")
    async def unlock(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "unlock")
    @discord.ui.button(label="♟", style=discord.ButtonStyle.secondary, row=0, custom_id="vm:permit")
    async def permit(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "permit")
    @discord.ui.button(label="⚒", style=discord.ButtonStyle.secondary, row=0, custom_id="vm:ban")
    async def ban(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "ban")
    @discord.ui.button(label="➤", style=discord.ButtonStyle.secondary, row=0, custom_id="vm:kick")
    async def kick(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "kick")
    @discord.ui.button(label="♛", style=discord.ButtonStyle.secondary, row=1, custom_id="vm:claim")
    async def claim(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "claim")
    @discord.ui.button(label="↪", style=discord.ButtonStyle.secondary, row=1, custom_id="vm:transfer")
    async def transfer(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "transfer")
    @discord.ui.button(label="✎", style=discord.ButtonStyle.secondary, row=1, custom_id="vm:rename")
    async def rename(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "rename")
    @discord.ui.button(label="＋", style=discord.ButtonStyle.secondary, row=1, custom_id="vm:limit")
    async def limit(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "limit")
    @discord.ui.button(label="ⓘ", style=discord.ButtonStyle.secondary, row=1, custom_id="vm:info")
    async def info(self, i: discord.Interaction, _: discord.ui.Button) -> None: await self.run(i, "info")


class VoiceMaster(commands.Cog):
    voice = app_commands.Group(name="voice", description="Configure and manage Join-to-Create voice channels")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._panel_send_locks: dict[int, asyncio.Lock] = {}
        self._last_panel_sent: dict[int, dt.datetime] = {}
        self.cleanup.start()

    def cog_unload(self) -> None:
        self.cleanup.cancel()

    @staticmethod
    def panel_view() -> VoiceMasterPanel:
        return VoiceMasterPanel()

    async def hub(self, hub_id: int):
        return await self.bot.db.voice_fetchone("SELECT * FROM voice_hubs WHERE hub_id=?", (hub_id,))

    async def record(self, channel_id: int):
        return await self.bot.db.voice_fetchone("SELECT * FROM temp_voice_channels WHERE channel_id=?", (channel_id,))

    async def current(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return None, None
        voice = interaction.user.voice
        if not voice or not voice.channel:
            return None, None
        return voice.channel, await self.record(voice.channel.id)

    async def staff(self, member: discord.Member, row: Any) -> bool:
        hub = await self.hub(int(row["hub_id"]))
        staff_roles = ids(hub["staff_roles"] if hub else None)
        return member.guild.owner_id == member.id or member.guild_permissions.manage_channels or bool({r.id for r in member.roles} & staff_roles)

    async def authorized(self, interaction: discord.Interaction, row: Any, owner_required: bool = True) -> bool:
        assert isinstance(interaction.user, discord.Member)
        allowed = interaction.user.id == int(row["owner_id"]) or await self.staff(interaction.user, row)
        if owner_required and not allowed:
            await interaction.response.send_message("❌ You don't own this voice channel.", ephemeral=True)
        return allowed

    async def send_log(self, guild: discord.Guild, row: Any, actor: discord.abc.User, action: str, target: discord.abc.User | None = None) -> None:
        hub = await self.hub(int(row["hub_id"]))
        channel = guild.get_channel(int(hub["log_channel_id"])) if hub and hub["log_channel_id"] else None
        if not isinstance(channel, discord.TextChannel): return
        embed = discord.Embed(title=f"VoiceMaster · {action}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{actor.mention} (`{actor.id}`)")
        embed.add_field(name="Channel", value=f"<#{row['channel_id']}> (`{row['channel_id']}`)")
        if target: embed.add_field(name="Target", value=f"{target.mention} (`{target.id}`)", inline=False)
        try: await channel.send(embed=embed)
        except discord.HTTPException: pass

    async def panel_action(self, interaction: discord.Interaction, action: str) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None:
            await interaction.response.send_message("❌ You must be connected to one of the temporary voice channels to use this panel.", ephemeral=True)
            return
        if action == "info":
            await self.show_info(interaction, channel, row); return
        if action == "claim":
            await self.claim_channel(interaction, channel, row); return
        if not await self.authorized(interaction, row): return
        if action in {"lock", "unlock"}:
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.connect = False if action == "lock" else None
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite if not overwrite.is_empty() else None, reason=f"VoiceMaster {action}")
            await self.bot.db.voice_execute("UPDATE temp_voice_channels SET locked=? WHERE channel_id=?", (int(action == "lock"), channel.id))
            await interaction.response.send_message(f"{'🔒 Your voice channel has been locked.' if action == 'lock' else '🔓 Your voice channel has been unlocked.'}", ephemeral=True)
            await self.send_log(interaction.guild, row, interaction.user, action.title()); return
        if action in {"permit", "ban", "kick", "transfer"}:
            await interaction.response.send_message("Select a member:", view=MemberPicker(self, action, interaction.user.id, ids(row["banned"])), ephemeral=True); return
        if action == "rename": await interaction.response.send_modal(RenameModal(self)); return
        if action == "limit": await interaction.response.send_modal(LimitModal(self)); return

    async def member_action(self, interaction: discord.Interaction, action: str, selected: discord.Member | discord.User) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None or not isinstance(selected, discord.Member):
            await interaction.response.send_message("❌ The channel or member is no longer available.", ephemeral=True); return
        if not await self.authorized(interaction, row): return
        if selected.bot and action != "kick":
            await interaction.response.send_message("❌ Bots cannot be selected for that action.", ephemeral=True); return
        permitted, banned = ids(row["permitted"]), ids(row["banned"])
        if action == "permit":
            permitted.add(selected.id); banned.discard(selected.id)
            ow = channel.overwrites_for(selected); ow.view_channel = True; ow.connect = True
            await channel.set_permissions(selected, overwrite=ow, reason="VoiceMaster permit")
            await self.bot.db.voice_execute("UPDATE temp_voice_channels SET permitted=?, banned=? WHERE channel_id=?", (dump_ids(permitted), dump_ids(banned), channel.id))
            msg = f"✅ {selected.mention} has been permitted to join your voice channel."
        elif action == "ban":
            hub = await self.hub(int(row["hub_id"])); immune = ids(hub["immune_roles"] if hub else None)
            if {r.id for r in selected.roles} & immune:
                await interaction.response.send_message("❌ That member has a voice-ban immune role.", ephemeral=True); return
            banned.add(selected.id); permitted.discard(selected.id)
            ow = channel.overwrites_for(selected); ow.connect = False
            await channel.set_permissions(selected, overwrite=ow, reason="VoiceMaster channel ban")
            if selected.voice and selected.voice.channel == channel: await selected.move_to(None, reason="VoiceMaster channel ban")
            await self.bot.db.voice_execute("UPDATE temp_voice_channels SET permitted=?, banned=? WHERE channel_id=?", (dump_ids(permitted), dump_ids(banned), channel.id))
            msg = f"🔨 {selected.mention} has been banned from your voice channel."
        elif action == "kick":
            if not selected.voice or selected.voice.channel != channel:
                await interaction.response.send_message("❌ That member is not in your voice channel.", ephemeral=True); return
            await selected.move_to(None, reason="VoiceMaster kick"); msg = f"🥾 {selected.mention} has been removed from your voice channel."
        else:
            if not selected.voice or selected.voice.channel != channel:
                await interaction.response.send_message("❌ Ownership can only be transferred to somebody inside the channel.", ephemeral=True); return
            await self.bot.db.voice_execute("UPDATE temp_voice_channels SET owner_id=? WHERE channel_id=?", (selected.id, channel.id))
            msg = f"🔄 Ownership has been transferred to {selected.mention}."
        await interaction.response.send_message(msg, ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, action.title(), selected)

    async def claim_channel(self, interaction: discord.Interaction, channel: discord.VoiceChannel, row: Any) -> None:
        lock = self._guild_locks.setdefault(interaction.guild.id, asyncio.Lock())
        async with lock:
            row = await self.record(channel.id)
            if row is None:
                await interaction.response.send_message("❌ This temporary channel no longer exists.", ephemeral=True); return
            owner = interaction.guild.get_member(int(row["owner_id"]))
            if owner and owner.voice and owner.voice.channel == channel and not await self.staff(interaction.user, row):
                await interaction.response.send_message("❌ The current owner is still connected.", ephemeral=True); return
            hub = await self.hub(int(row["hub_id"])); grace = int(hub["grace_seconds"] if hub else 0)
            if row["owner_left_at"] and grace:
                left = dt.datetime.fromisoformat(row["owner_left_at"])
                remaining = grace - int((discord.utils.utcnow().replace(tzinfo=None) - left).total_seconds())
                if remaining > 0:
                    await interaction.response.send_message(f"⏳ You can claim this channel in {remaining} seconds.", ephemeral=True); return
            await self.bot.db.voice_execute("UPDATE temp_voice_channels SET owner_id=?, owner_left_at=NULL WHERE channel_id=?", (interaction.user.id, channel.id))
        await interaction.response.send_message("👑 You are now the owner of this voice channel.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Ownership claimed")

    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None or not await self.authorized(interaction, row): return
        name = re.sub(r"[\r\n]", " ", name).strip()
        if not name: await interaction.response.send_message("❌ Channel name cannot be empty.", ephemeral=True); return
        hub = await self.hub(int(row["hub_id"])); cooldown = int(hub["rename_cooldown"] if hub else 30)
        if row["last_rename_at"]:
            last = dt.datetime.fromisoformat(row["last_rename_at"])
            remain = cooldown - int((discord.utils.utcnow().replace(tzinfo=None) - last).total_seconds())
            if remain > 0: await interaction.response.send_message(f"⏳ Rename again in {remain} seconds.", ephemeral=True); return
        await channel.edit(name=name[:100], reason="VoiceMaster rename")
        await self.bot.db.voice_execute("UPDATE temp_voice_channels SET custom_name=?, last_rename_at=? WHERE channel_id=?", (name[:100], discord.utils.utcnow().replace(tzinfo=None).isoformat(), channel.id))
        await interaction.response.send_message(f"✏️ Your voice channel has been renamed to **{name[:100]}**.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Renamed")

    async def set_limit(self, interaction: discord.Interaction, limit: int) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None or not await self.authorized(interaction, row): return
        await channel.edit(user_limit=limit, reason="VoiceMaster user limit")
        await self.bot.db.voice_execute("UPDATE temp_voice_channels SET user_limit=? WHERE channel_id=?", (limit, channel.id))
        await interaction.response.send_message(f"👥 User limit changed to **{'Unlimited' if limit == 0 else limit}**.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "User limit changed")

    async def show_info(self, interaction: discord.Interaction, channel: discord.VoiceChannel, row: Any) -> None:
        created = discord.utils.snowflake_time(channel.id)
        age = discord.utils.format_dt(created, "R")
        embed = discord.Embed(title="VOICE CHANNEL INFORMATION", color=discord.Color.blurple())
        embed.description = (f"**Channel:** {channel.mention}\n**Owner:** <@{row['owner_id']}>\n**Channel ID:** `{channel.id}`\n"
                             f"**Created:** {age}\n**Members:** {len(channel.members)}\n**Limit:** {channel.user_limit or 'Unlimited'}\n"
                             f"**Status:** {'🔒 Locked' if row['locked'] else '🔓 Unlocked'}\n**Permitted:** {len(ids(row['permitted']))}\n"
                             f"**Banned:** {len(ids(row['banned']))}\n**Bitrate:** {channel.bitrate // 1000} kbps\n**Region:** {channel.rtc_region or 'Automatic'}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot: return
        if after.channel and await self.hub(after.channel.id): await self.create_for(member, after.channel)
        if before.channel:
            row = await self.record(before.channel.id)
            if row:
                if member.id == int(row["owner_id"]):
                    await self.bot.db.voice_execute("UPDATE temp_voice_channels SET owner_left_at=? WHERE channel_id=?", (discord.utils.utcnow().replace(tzinfo=None).isoformat(), before.channel.id))
                await asyncio.sleep(1)
                if not before.channel.members: await self.delete_temp(before.channel, row)
        if after.channel:
            row = await self.record(after.channel.id)
            if row and member.id == int(row["owner_id"]):
                await self.bot.db.voice_execute("UPDATE temp_voice_channels SET owner_left_at=NULL WHERE channel_id=?", (after.channel.id,))
                panel_lock = self._panel_send_locks.setdefault(after.channel.id, asyncio.Lock())
                async with panel_lock:
                    now = discord.utils.utcnow()
                    last = self._last_panel_sent.get(after.channel.id)
                    if last is None or (now - last).total_seconds() >= 15:
                        try:
                            hub = await self.hub(int(row["hub_id"]))
                            await after.channel.send(embed=self.panel_embed(hub), view=VoiceMasterPanel())
                            self._last_panel_sent[after.channel.id] = now
                        except discord.HTTPException:
                            log.warning("Could not post the VoiceMaster panel in voice channel %s", after.channel.id)

    async def create_for(self, member: discord.Member, source: discord.VoiceChannel) -> None:
        lock = self._guild_locks.setdefault(member.guild.id, asyncio.Lock())
        async with lock:
            if not member.voice or member.voice.channel != source: return
            hub = await self.hub(source.id)
            existing = await self.bot.db.voice_fetchall("SELECT channel_id FROM temp_voice_channels WHERE guild_id=? AND owner_id=?", (member.guild.id, member.id))
            if len(existing) >= int(hub["max_channels"]):
                old = member.guild.get_channel(int(existing[0]["channel_id"]))
                if isinstance(old, discord.VoiceChannel): await member.move_to(old, reason="Existing VoiceMaster channel"); return
            category = member.guild.get_channel(int(hub["category_id"]))
            if not isinstance(category, discord.CategoryChannel): return
            number = len(await self.bot.db.voice_fetchall("SELECT channel_id FROM temp_voice_channels WHERE guild_id=?", (member.guild.id,))) + 1
            name = str(hub["name_template"]).format(user=member.name, display_name=member.display_name, number=number)[:100]
            overwrites = {member.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=not bool(hub["default_locked"])), member: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=False)}
            for rid in ids(hub["auto_roles"]):
                role = member.guild.get_role(rid)
                if role: overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            channel = await member.guild.create_voice_channel(name, category=category, user_limit=int(hub["default_limit"]), overwrites=overwrites, reason="VoiceMaster Join-to-Create")
            await self.bot.db.voice_execute("INSERT INTO temp_voice_channels(channel_id,guild_id,hub_id,owner_id,locked,user_limit) VALUES(?,?,?,?,?,?)", (channel.id, member.guild.id, source.id, member.id, int(hub["default_locked"]), int(hub["default_limit"])))
            try: await member.move_to(channel, reason="VoiceMaster Join-to-Create")
            except discord.HTTPException: await self.delete_temp(channel, await self.record(channel.id))
            row = await self.record(channel.id)
            if row: await self.send_log(member.guild, row, member, "Channel created")

    async def delete_temp(self, channel: discord.VoiceChannel, row: Any) -> None:
        await self.bot.db.voice_execute("DELETE FROM temp_voice_channels WHERE channel_id=?", (channel.id,))
        self._last_panel_sent.pop(channel.id, None)
        self._panel_send_locks.pop(channel.id, None)
        try:
            await self.send_log(channel.guild, row, channel.guild.me, "Channel deleted")
            await channel.delete(reason="Empty VoiceMaster channel")
        except discord.HTTPException: pass

    def panel_embed(self, hub: Any | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="VoiceMaster Interface",
            description="Manage your voice channel by\nusing the buttons below.",
            color=discord.Color(0x252956),
        )
        embed.add_field(name="Button Usage", value=(
            "▣ — **Lock** the voice channel\n"
            "□ — **Unlock** the voice channel\n"
            "♟ — **Permit** a member to the voice channel\n"
            "⚒ — **Ban** a member from the voice channel\n"
            "➤ — **Kick** a member from the voice channel\n"
            "♛ — **Claim** the voice channel\n"
            "↪ — **Transfer** the voice channel\n"
            "✎ — **Rename** the voice channel\n"
            "＋ — **Manage** the user limit\n"
            "ⓘ — **View** channel information"
        ), inline=False)
        thumbnail = hub["thumbnail"] if hub and hub["thumbnail"] else None
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        elif self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        return embed

    async def admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need Manage Server to configure VoiceMaster.", ephemeral=True); return False
        return True

    @voice.command(name="setup", description="Create or update a Join-to-Create hub")
    @app_commands.describe(hub="Voice channel members join", category="Category for temporary channels", panel_channel="Text channel for the shared panel", log_channel="Optional action log channel")
    async def setup(self, interaction: discord.Interaction, hub: discord.VoiceChannel, category: discord.CategoryChannel, panel_channel: discord.TextChannel, log_channel: discord.TextChannel | None = None) -> None:
        if not await self.admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        me = interaction.guild.me
        channel_permissions = panel_channel.permissions_for(me)
        missing = []
        if not channel_permissions.view_channel: missing.append("View Channel")
        if not channel_permissions.send_messages: missing.append("Send Messages")
        if not channel_permissions.embed_links: missing.append("Embed Links")
        if not me.guild_permissions.manage_channels: missing.append("Manage Channels")
        if not me.guild_permissions.move_members: missing.append("Move Members")
        if missing:
            await interaction.followup.send("⚠️ I cannot finish setup. Give my bot role these permissions: **" + ", ".join(missing) + "**.", ephemeral=True)
            return
        try:
            await self.bot.db.voice_execute("INSERT INTO voice_hubs(guild_id,hub_id,category_id,panel_channel_id,log_channel_id) VALUES(?,?,?,?,?) ON CONFLICT(hub_id) DO UPDATE SET category_id=excluded.category_id,panel_channel_id=excluded.panel_channel_id,log_channel_id=excluded.log_channel_id", (interaction.guild_id, hub.id, category.id, panel_channel.id, log_channel.id if log_channel else None))
            row = await self.hub(hub.id)
            await panel_channel.send(embed=self.panel_embed(row), view=VoiceMasterPanel())
        except discord.Forbidden:
            await interaction.followup.send("⚠️ Discord blocked me from posting the panel. Allow **View Channel**, **Send Messages**, and **Embed Links** in the selected panel channel.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            log.exception("VoiceMaster setup failed in guild %s", interaction.guild_id)
            await interaction.followup.send(f"⚠️ Discord rejected the setup request (`{exc.code}`). Check the bot role and try again.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Join-to-Create configured on {hub.mention}. The shared panel was posted in {panel_channel.mention}.", ephemeral=True)

    @voice.command(name="panel", description="Post a new persistent VoiceMaster control panel")
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if not await self.admin(interaction): return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel): await interaction.response.send_message("❌ Choose a text channel.", ephemeral=True); return
        await target.send(embed=self.panel_embed(), view=VoiceMasterPanel())
        await interaction.response.send_message(f"✅ Panel posted in {target.mention}.", ephemeral=True)

    @voice.command(name="settings", description="View VoiceMaster hub settings")
    async def settings(self, interaction: discord.Interaction, hub: discord.VoiceChannel | None = None) -> None:
        if not await self.admin(interaction): return
        rows = [await self.hub(hub.id)] if hub else await self.bot.db.voice_fetchall("SELECT * FROM voice_hubs WHERE guild_id=?", (interaction.guild_id,))
        rows = [r for r in rows if r]
        if not rows: await interaction.response.send_message("No Join-to-Create hubs are configured. Use `/voice setup`.", ephemeral=True); return
        embed = discord.Embed(title="VoiceMaster Settings", color=discord.Color.blurple())
        for r in rows[:10]: embed.add_field(name=f"Hub: <#{r['hub_id']}>", value=f"Category: <#{r['category_id']}>\nPanel: <#{r['panel_channel_id']}>\nLimit: {r['default_limit'] or 'Unlimited'} · Grace: {r['grace_seconds']}s · Rename cooldown: {r['rename_cooldown']}s", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @voice.command(name="configure", description="Change limits, names, grace time, and default lock state")
    async def configure(self, interaction: discord.Interaction, hub: discord.VoiceChannel, name_template: str | None = None, default_limit: app_commands.Range[int, 0, 99] | None = None, default_locked: bool | None = None, grace_seconds: app_commands.Range[int, 0, 3600] | None = None, rename_cooldown: app_commands.Range[int, 5, 3600] | None = None, max_channels: app_commands.Range[int, 1, 10] | None = None, thumbnail_url: str | None = None) -> None:
        if not await self.admin(interaction): return
        row = await self.hub(hub.id)
        if not row: await interaction.response.send_message("❌ That is not a configured hub.", ephemeral=True); return
        values = {"name_template": name_template, "default_limit": default_limit, "default_locked": int(default_locked) if default_locked is not None else None, "grace_seconds": grace_seconds, "rename_cooldown": rename_cooldown, "max_channels": max_channels, "thumbnail": thumbnail_url}
        for key, value in values.items():
            if value is not None: await self.bot.db.voice_execute(f"UPDATE voice_hubs SET {key}=? WHERE hub_id=?", (value, hub.id))
        await interaction.response.send_message("✅ VoiceMaster settings updated.", ephemeral=True)

    @voice.command(name="staff", description="Add or remove a staff override role")
    async def staff_command(self, interaction: discord.Interaction, hub: discord.VoiceChannel, action: Literal["add", "remove"], role: discord.Role) -> None:
        if not await self.admin(interaction): return
        row = await self.hub(hub.id)
        if not row: await interaction.response.send_message("❌ That is not a configured hub.", ephemeral=True); return
        roles = ids(row["staff_roles"]); roles.add(role.id) if action == "add" else roles.discard(role.id)
        await self.bot.db.voice_execute("UPDATE voice_hubs SET staff_roles=? WHERE hub_id=?", (dump_ids(roles), hub.id))
        await interaction.response.send_message(f"✅ {role.mention} was {action}ed as VoiceMaster staff.", ephemeral=True)

    @voice.command(name="whitelist", description="Automatically permit a role in new temporary channels")
    async def whitelist(self, interaction: discord.Interaction, hub: discord.VoiceChannel, action: Literal["add", "remove"], role: discord.Role) -> None:
        if not await self.admin(interaction): return
        row = await self.hub(hub.id)
        if not row: await interaction.response.send_message("❌ That is not a configured hub.", ephemeral=True); return
        roles = ids(row["auto_roles"]); roles.add(role.id) if action == "add" else roles.discard(role.id)
        await self.bot.db.voice_execute("UPDATE voice_hubs SET auto_roles=? WHERE hub_id=?", (dump_ids(roles), hub.id))
        await interaction.response.send_message(f"✅ Updated automatic access for {role.mention}.", ephemeral=True)

    @voice.command(name="blacklist", description="Make a role immune from temporary voice bans")
    async def blacklist(self, interaction: discord.Interaction, hub: discord.VoiceChannel, action: Literal["add", "remove"], role: discord.Role) -> None:
        if not await self.admin(interaction): return
        row = await self.hub(hub.id)
        if not row: await interaction.response.send_message("❌ That is not a configured hub.", ephemeral=True); return
        roles = ids(row["immune_roles"]); roles.add(role.id) if action == "add" else roles.discard(role.id)
        await self.bot.db.voice_execute("UPDATE voice_hubs SET immune_roles=? WHERE hub_id=?", (dump_ids(roles), hub.id))
        await interaction.response.send_message(f"✅ Updated voice-ban immunity for {role.mention}.", ephemeral=True)

    @voice.command(name="cleanup", description="Remove empty or missing temporary voice channels")
    async def cleanup_command(self, interaction: discord.Interaction) -> None:
        if not await self.admin(interaction): return
        count = await self.cleanup_guild(interaction.guild)
        await interaction.response.send_message(f"✅ Cleaned up **{count}** stale/empty temporary channels.", ephemeral=True)

    @voice.command(name="unban", description="Allow a member previously banned from your temporary voice channel")
    async def unban(self, interaction: discord.Interaction, member: discord.Member) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None:
            await interaction.response.send_message("❌ Join your temporary voice channel first.", ephemeral=True); return
        if not await self.authorized(interaction, row): return
        banned = ids(row["banned"])
        if member.id not in banned:
            await interaction.response.send_message("❌ That member is not banned from this channel.", ephemeral=True); return
        banned.discard(member.id)
        ow = channel.overwrites_for(member); ow.connect = None
        await channel.set_permissions(member, overwrite=ow if not ow.is_empty() else None, reason="VoiceMaster unban")
        await self.bot.db.voice_execute("UPDATE temp_voice_channels SET banned=? WHERE channel_id=?", (dump_ids(banned), channel.id))
        await interaction.response.send_message(f"✅ {member.mention} has been unbanned from your voice channel.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Member unbanned", member)

    @voice.command(name="private", description="Hide or reveal your temporary voice channel")
    async def private(self, interaction: discord.Interaction, enabled: bool) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None:
            await interaction.response.send_message("❌ Join your temporary voice channel first.", ephemeral=True); return
        if not await self.authorized(interaction, row): return
        ow = channel.overwrites_for(interaction.guild.default_role); ow.view_channel = False if enabled else None
        await channel.set_permissions(interaction.guild.default_role, overwrite=ow, reason="VoiceMaster private mode")
        await self.bot.db.voice_execute("UPDATE temp_voice_channels SET private=? WHERE channel_id=?", (int(enabled), channel.id))
        await interaction.response.send_message(f"{'🔐 Your channel is now private.' if enabled else '👁️ Your channel is now visible.'}", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Private enabled" if enabled else "Private disabled")

    @voice.command(name="bitrate", description="Change your temporary voice channel bitrate")
    async def bitrate(self, interaction: discord.Interaction, kbps: app_commands.Range[int, 8, 384]) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None:
            await interaction.response.send_message("❌ Join your temporary voice channel first.", ephemeral=True); return
        if not await self.authorized(interaction, row): return
        maximum = interaction.guild.bitrate_limit // 1000
        value = min(int(kbps), maximum)
        await channel.edit(bitrate=value * 1000, reason="VoiceMaster bitrate")
        await interaction.response.send_message(f"✅ Bitrate changed to **{value} kbps**.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Bitrate changed")

    @voice.command(name="region", description="Change your temporary channel RTC region")
    async def region(self, interaction: discord.Interaction, region: Literal["automatic", "brazil", "hongkong", "india", "japan", "rotterdam", "russia", "singapore", "southafrica", "sydney", "us-central", "us-east", "us-south", "us-west"]) -> None:
        channel, row = await self.current(interaction)
        if not isinstance(channel, discord.VoiceChannel) or row is None:
            await interaction.response.send_message("❌ Join your temporary voice channel first.", ephemeral=True); return
        if not await self.authorized(interaction, row): return
        await channel.edit(rtc_region=None if region == "automatic" else region, reason="VoiceMaster region")
        await interaction.response.send_message(f"✅ Voice region changed to **{region}**.", ephemeral=True)
        await self.send_log(interaction.guild, row, interaction.user, "Region changed")

    @voice.command(name="delete", description="Staff: delete a broken temporary voice channel")
    async def delete_command(self, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        row = await self.record(channel.id)
        if row is None or not isinstance(interaction.user, discord.Member) or not await self.staff(interaction.user, row):
            await interaction.response.send_message("❌ This requires VoiceMaster staff access and a tracked channel.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        await self.delete_temp(channel, row)
        await interaction.followup.send("✅ Temporary channel deleted.", ephemeral=True)

    @voice.command(name="stats", description="Show VoiceMaster usage statistics")
    async def stats(self, interaction: discord.Interaction) -> None:
        if not await self.admin(interaction): return
        hubs = await self.bot.db.voice_fetchall("SELECT hub_id FROM voice_hubs WHERE guild_id=?", (interaction.guild_id,))
        active = await self.bot.db.voice_fetchall("SELECT channel_id FROM temp_voice_channels WHERE guild_id=?", (interaction.guild_id,))
        await interaction.response.send_message(embed=discord.Embed(title="VoiceMaster Statistics", description=f"Configured hubs: **{len(hubs)}**\nActive temporary channels: **{len(active)}**", color=discord.Color.blurple()), ephemeral=True)

    @voice.command(name="reset", description="Remove a Join-to-Create hub configuration")
    async def reset(self, interaction: discord.Interaction, hub: discord.VoiceChannel, confirm: bool = False) -> None:
        if not await self.admin(interaction): return
        if not confirm: await interaction.response.send_message("⚠️ Run this again with `confirm: True` to remove the hub. Existing temporary channels are not deleted.", ephemeral=True); return
        await self.bot.db.voice_execute("DELETE FROM voice_hubs WHERE guild_id=? AND hub_id=?", (interaction.guild_id, hub.id))
        await interaction.response.send_message("✅ Join-to-Create hub configuration removed.", ephemeral=True)

    async def cleanup_guild(self, guild: discord.Guild) -> int:
        count = 0
        rows = await self.bot.db.voice_fetchall("SELECT * FROM temp_voice_channels WHERE guild_id=?", (guild.id,))
        for row in rows:
            channel = guild.get_channel(int(row["channel_id"]))
            if channel is None:
                await self.bot.db.voice_execute("DELETE FROM temp_voice_channels WHERE channel_id=?", (row["channel_id"],)); count += 1
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                await self.delete_temp(channel, row); count += 1
        return count

    @tasks.loop(minutes=5)
    async def cleanup(self) -> None:
        for guild in self.bot.guilds:
            try: await self.cleanup_guild(guild)
            except Exception: log.exception("VoiceMaster cleanup failed for guild %s", guild.id)

    @cleanup.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()
