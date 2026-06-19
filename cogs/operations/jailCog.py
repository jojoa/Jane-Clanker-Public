from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.operations.jail import service as jailService
from runtime import cogGuards as runtimeCogGuards
from runtime import commandScopes as runtimeCommandScopes
from runtime import interaction as interactionRuntime

log = logging.getLogger(__name__)

_defaultJailedRoleId = 1477066703564177510


def _manageableRoleForBot(botMember: discord.Member, role: discord.Role) -> bool:
    if role.managed:
        return False
    if role >= botMember.top_role:
        return False
    return True


def _jsonIntList(value: object) -> list[int]:
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            raw = []
    out: list[int] = []
    for item in raw:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            out.append(parsed)
    return out


class JailCog(runtimeCogGuards.InteractionGuardMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _resolveJailedRole(self, guild: discord.Guild) -> discord.Role | None:
        roleId = int(getattr(config, "jailedRoleId", _defaultJailedRoleId) or _defaultJailedRoleId)
        return guild.get_role(roleId)

    def _resolveJailChannel(
        self,
        guild: discord.Guild,
        jailedRole: discord.Role,
    ) -> discord.abc.GuildChannel | None:
        configuredId = int(getattr(config, "jailedChannelId", 0) or 0)
        if configuredId > 0:
            configured = guild.get_channel(configuredId)
            if isinstance(configured, discord.abc.GuildChannel):
                return configured

        defaultRole = guild.default_role
        candidates: list[discord.abc.GuildChannel] = []
        for channel in guild.channels:
            if not isinstance(channel, discord.abc.GuildChannel):
                continue
            jailedView = channel.permissions_for(jailedRole).view_channel
            defaultView = channel.permissions_for(defaultRole).view_channel
            if jailedView and not defaultView:
                candidates.append(channel)
        if not candidates:
            return None

        textFirst = [
            channel
            for channel in candidates
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
        ]
        if textFirst:
            return textFirst[0]
        return candidates[0]

    async def _setMemberChannelIsolation(
        self,
        member: discord.Member,
        jailChannel: discord.abc.GuildChannel | None,
        *,
        reason: str,
    ) -> list[int]:
        enforceIsolation = bool(getattr(config, "jailEnforceChannelIsolation", True))
        if not enforceIsolation:
            return []

        isolated: list[int] = []
        allowedChannelId = int(jailChannel.id) if jailChannel is not None else 0
        for channel in member.guild.channels:
            if not isinstance(channel, discord.abc.GuildChannel):
                continue
            if channel.id == allowedChannelId:
                continue
            if not channel.permissions_for(member).view_channel:
                continue

            overwrite = channel.overwrites_for(member)
            overwrite.view_channel = False
            if hasattr(overwrite, "send_messages"):
                overwrite.send_messages = False
            if hasattr(overwrite, "send_messages_in_threads"):
                overwrite.send_messages_in_threads = False
            if hasattr(overwrite, "add_reactions"):
                overwrite.add_reactions = False
            if hasattr(overwrite, "connect"):
                overwrite.connect = False
            if hasattr(overwrite, "speak"):
                overwrite.speak = False

            try:
                await channel.set_permissions(member, overwrite=overwrite, reason=reason)
                isolated.append(int(channel.id))
            except (discord.Forbidden, discord.HTTPException):
                continue

        if jailChannel is not None:
            overwrite = jailChannel.overwrites_for(member)
            overwrite.view_channel = True
            if hasattr(overwrite, "send_messages"):
                overwrite.send_messages = True
            if hasattr(overwrite, "send_messages_in_threads"):
                overwrite.send_messages_in_threads = True
            if hasattr(overwrite, "connect"):
                overwrite.connect = True
            if hasattr(overwrite, "speak"):
                overwrite.speak = True
            try:
                await jailChannel.set_permissions(member, overwrite=overwrite, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                pass

        return isolated

    async def _clearMemberIsolation(
        self,
        member: discord.Member,
        channelIds: list[int],
        *,
        reason: str,
    ) -> int:
        cleared = 0
        seen: set[int] = set()
        for rawChannelId in channelIds:
            try:
                channelId = int(rawChannelId)
            except (TypeError, ValueError):
                continue
            if channelId <= 0 or channelId in seen:
                continue
            seen.add(channelId)
            channel = member.guild.get_channel(channelId)
            if not isinstance(channel, discord.abc.GuildChannel):
                continue
            try:
                await channel.set_permissions(member, overwrite=None, reason=reason)
                cleared += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        return cleared

    # Legacy slash handler kept for future reuse.
    async def jail(self, interaction: discord.Interaction, user: discord.Member) -> None:
        member = await self._requireAdministrator(interaction)
        if member is None:
            return
        if not await self._requireTestGuild(interaction):
            return

        guild = interaction.guild
        botMember = guild.me
        if botMember is None:
            await self._safeReply(interaction, "Jane cannot resolve her own permissions right now.")
            return
        if not botMember.guild_permissions.manage_roles:
            await self._safeReply(interaction, "Jane needs `Manage Roles` permission for this command.")
            return
        if not botMember.guild_permissions.moderate_members:
            await self._safeReply(interaction, "Jane needs `Moderate Members` permission for full jail handling.")
            return

        if int(user.id) == int(guild.owner_id):
            await self._safeReply(interaction, "You can't jail the server owner.")
            return

        jailedRole = self._resolveJailedRole(guild)
        if jailedRole is None:
            await self._safeReply(
                interaction,
                f"Jailed role not found. Expected role ID `{int(getattr(config, 'jailedRoleId', _defaultJailedRoleId) or _defaultJailedRoleId)}`.",
            )
            return
        if jailedRole >= botMember.top_role:
            await self._safeReply(interaction, "Jane cannot manage the jailed role due to role hierarchy.")
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)

        allCurrentRoleIds = [
            int(role.id)
            for role in user.roles
            if int(role.id) != int(guild.default_role.id)
        ]
        removableRoles = [
            role
            for role in user.roles
            if int(role.id) not in {int(guild.default_role.id), int(jailedRole.id)}
            and _manageableRoleForBot(botMember, role)
        ]
        unmanageableRoleIds = [
            int(role.id)
            for role in user.roles
            if int(role.id) not in {int(guild.default_role.id), int(jailedRole.id)}
            and not _manageableRoleForBot(botMember, role)
        ]

        reason = f"Jailed by {member} ({member.id})"
        if removableRoles:
            try:
                await user.remove_roles(*removableRoles, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed removing roles during jail for user %s", user.id)

        if jailedRole not in user.roles:
            try:
                await user.add_roles(jailedRole, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                await self._safeReply(interaction, "Failed to assign jailed role due to permissions.")
                return

        jailChannel = self._resolveJailChannel(guild, jailedRole)
        isolatedChannelIds = await self._setMemberChannelIsolation(
            user,
            jailChannel,
            reason=reason,
        )

        recordId = await jailService.createJailRecord(
            guildId=int(guild.id),
            userId=int(user.id),
            jailedBy=int(member.id),
            jailedRoleId=int(jailedRole.id),
            jailChannelId=int(jailChannel.id) if jailChannel is not None else None,
            savedRoleIds=allCurrentRoleIds,
            unmanageableRoleIds=unmanageableRoleIds,
            isolatedChannelIds=isolatedChannelIds,
        )

        jailChannelText = f"<#{int(jailChannel.id)}>" if jailChannel is not None else "not found"
        unmanageableCount = len(unmanageableRoleIds)
        removedCount = len(removableRoles)
        await self._safeReply(
            interaction,
            (
                f"Jailed {user.mention}.\n"
                f"- Saved roles: `{len(allCurrentRoleIds)}`\n"
                f"- Removed roles: `{removedCount}`\n"
                f"- Unmanageable roles: `{unmanageableCount}`\n"
                f"- Jail channel: {jailChannelText}\n"
                f"- Channel isolation updates: `{len(isolatedChannelIds)}`\n"
                f"- Record ID: `{recordId}`\n"
                f"- Completed at: <t:{int(datetime.now(timezone.utc).timestamp())}:s>"
            ),
            ephemeral=True,
        )

    # Legacy slash handler kept for future reuse.
    async def unjail(self, interaction: discord.Interaction, user: discord.Member) -> None:
        member = await self._requireAdministrator(interaction)
        if member is None:
            return
        if not await self._requireTestGuild(interaction):
            return

        guild = interaction.guild
        botMember = guild.me
        if botMember is None:
            await self._safeReply(interaction, "Jane cannot resolve her own permissions right now.")
            return
        if not botMember.guild_permissions.manage_roles:
            await self._safeReply(interaction, "Jane needs `Manage Roles` permission for this command.")
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)

        activeRecord = await jailService.getActiveJailRecord(int(guild.id), int(user.id))
        if not activeRecord:
            await self._safeReply(
                interaction,
                f"{user.mention} does not have an active jail record.",
                ephemeral=True,
            )
            return

        jailedRoleId = int(activeRecord.get("jailedRoleId") or 0)
        savedRoleIds = _jsonIntList(activeRecord.get("savedRoleIdsJson"))
        isolatedChannelIds = _jsonIntList(activeRecord.get("isolatedChannelIdsJson"))
        reason = f"Unjailed by {member} ({member.id})"

        rolesToRestore: list[discord.Role] = []
        for roleId in savedRoleIds:
            role = guild.get_role(int(roleId))
            if role is None:
                continue
            if int(role.id) in {int(guild.default_role.id), int(jailedRoleId)}:
                continue
            if not _manageableRoleForBot(botMember, role):
                continue
            rolesToRestore.append(role)

        restored = 0
        for role in rolesToRestore:
            if role in user.roles:
                continue
            try:
                await user.add_roles(role, reason=reason)
                restored += 1
            except (discord.Forbidden, discord.HTTPException):
                continue

        removedJailRole = False
        jailedRole = guild.get_role(jailedRoleId) if jailedRoleId > 0 else None
        if jailedRole is not None and jailedRole in user.roles:
            try:
                await user.remove_roles(jailedRole, reason=reason)
                removedJailRole = True
            except (discord.Forbidden, discord.HTTPException):
                removedJailRole = False

        clearedOverwrites = await self._clearMemberIsolation(
            user,
            isolatedChannelIds,
            reason=reason,
        )

        await jailService.releaseActiveJailRecord(
            guildId=int(guild.id),
            userId=int(user.id),
            releasedBy=int(member.id),
        )

        await self._safeReply(
            interaction,
            (
                f"Unjailed {user.mention}.\n"
                f"- Restored roles: `{restored}`\n"
                f"- Removed jailed role: `{'yes' if removedJailRole else 'no'}`\n"
                f"- Cleared channel overwrites: `{clearedOverwrites}`\n"
                f"- Completed at: <t:{int(datetime.now(timezone.utc).timestamp())}:s>"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JailCog(bot), guilds=runtimeCommandScopes.getTestGuildObjects())
