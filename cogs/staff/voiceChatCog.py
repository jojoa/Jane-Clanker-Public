from __future__ import annotations

from typing import Awaitable, Callable, Optional

import runtime.interaction as interactionRuntime
from runtime import permissions as runtimePermissions
from discord import Interaction, Member, VoiceChannel, VoiceState, app_commands
from discord.ext import commands

from config import (
    serverId,
    canCreateVoiceChatBasic,
    canCreateVoiceChatAll,
)
from features.staff.voiceChat.voiceChatManager import (
    cleanVoiceChatsCategory,
    createBreakroomVoiceChatWithPerms,
    createGamenightVoiceChatWithPerms,
    createShiftVoiceChatWithPerms,
    createSupervisorCommsVoiceChatWithPerms,
    deleteVoiceChannel,
    deleteVoiceChannels,
    getManagedVoiceChatType,
    handleDeletedVoiceChannel,
    isManagedVoiceChannel,
    syncTrackedVoiceChats,
)

_VOICE_CHAT_CHOICES = [
    app_commands.Choice(name="Shift", value="Shift"),
    app_commands.Choice(name="Gamenight", value="Gamenight"),
    app_commands.Choice(name="Breakroom", value="Breakroom"),
    app_commands.Choice(name="Supervisor comms", value="Supervisor comms"),
]
_DELETABLE_VOICE_CHAT_CHOICES = [
    *_VOICE_CHAT_CHOICES,
]
_VOICE_CHAT_PERMISSION_CHECKS: dict[str, Callable[[Member], bool]] = {
    "Shift": lambda member: _hasAnyRole(member, canCreateVoiceChatAll),
    "Gamenight": lambda member: _hasAnyRole(member, canCreateVoiceChatBasic),
    "Breakroom": lambda member: _hasAnyRole(member, canCreateVoiceChatBasic),
    "Supervisor comms": lambda member: _hasAnyRole(member, canCreateVoiceChatAll),
}


def _hasRole(member: Member, roleId: Optional[int]) -> bool:
    return runtimePermissions.hasAnyRole(member, [roleId])


def _hasAnyRole(member: Member, roleIds: list[int]) -> bool:
    return runtimePermissions.hasAnyRole(member, roleIds)


class VoiceChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await syncTrackedVoiceChats(self.bot)

    async def _safeEphemeral(self, interaction: Interaction, message: str) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=message,
            ephemeral=True,
        )

    async def _safeDefer(self, interaction: Interaction) -> None:
        await interactionRuntime.safeInteractionDefer(
            interaction,
            ephemeral=True,
        )

    def _canCreateVoiceChat(self, member: Member, voiceChatType: str) -> bool:
        checker = _VOICE_CHAT_PERMISSION_CHECKS.get(voiceChatType)
        return bool(checker and checker(member))

    async def _createRequestedVoiceChat(
        self,
        *,
        voiceChatType: str,
        cohost1: Member | None,
        cohost2: Member | None,
    ) -> VoiceChannel | None:
        creator: Callable[[], Awaitable[VoiceChannel | None]]
        if voiceChatType == "Shift":
            creator = lambda: createShiftVoiceChatWithPerms(self.bot, cohost1, cohost2)
        elif voiceChatType == "Gamenight":
            creator = lambda: createGamenightVoiceChatWithPerms(self.bot, cohost1, cohost2)
        elif voiceChatType == "Breakroom":
            creator = lambda: createBreakroomVoiceChatWithPerms(self.bot, cohost1, cohost2)
        elif voiceChatType == "Supervisor comms":
            creator = lambda: createSupervisorCommsVoiceChatWithPerms(self.bot, cohost1, cohost2)
        else:
            return None
        return await creator()

    @app_commands.command(name="create-voice-chat", description="Create a temporary voice chat channel.")
    @app_commands.guild_only()
    @app_commands.rename(type_of_voice_chat="type-of-voice-chat")
    @app_commands.rename(cohost1="cohost-1", cohost2="cohost-2")
    @app_commands.describe(
        cohost1="Optional first cohost for this voice chat.",
        cohost2="Optional second cohost for this voice chat.",
    )
    @app_commands.choices(type_of_voice_chat=_VOICE_CHAT_CHOICES)
    async def createVoiceChat(
        self,
        interaction: Interaction,
        type_of_voice_chat: app_commands.Choice[str],
        cohost1: Member | None = None,
        cohost2: Member | None = None,
    ) -> None:
        voiceChatType = type_of_voice_chat.value
        if not self._canCreateVoiceChat(interaction.user, voiceChatType):
            await self._safeEphemeral(
                interaction,
                f"Sorry, {interaction.user.mention} you can't create this type of Voice Chat.",
            )
            return

        await self._safeDefer(interaction)
        voiceChannel = await self._createRequestedVoiceChat(
            voiceChatType=voiceChatType,
            cohost1=cohost1,
            cohost2=cohost2,
        )
        if voiceChannel is None:
            await self._safeEphemeral(interaction, "I couldn't create that voice chat.")
            return

        await self._safeEphemeral(interaction, f"Created {voiceChannel.mention}.")

    @app_commands.command(name="clean-voice-chats", description="Clean temporary voice chat channels in the configured category.")
    @app_commands.guild_only()
    async def cleanVoiceChats(self, interaction: Interaction) -> None:
        if not _hasAnyRole(interaction.user, canCreateVoiceChatAll):
            await self._safeEphemeral(interaction, "Sorry, you can't clean voice chat channels.")
            return

        await self._safeDefer(interaction)
        await cleanVoiceChatsCategory(interaction=interaction, bot=self.bot)

    @app_commands.command(
        name="delete-voice-chat",
        description="Delete a specific temporary voice chat or every temporary voice chat of a type.",
    )
    @app_commands.guild_only()
    @app_commands.rename(type_of_voice_chat="type-of-voice-chat")
    @app_commands.rename(voice_chat_id="voice-chat-id")
    @app_commands.choices(type_of_voice_chat=_DELETABLE_VOICE_CHAT_CHOICES)
    async def deleteVoiceChat(
        self,
        interaction: Interaction,
        type_of_voice_chat: app_commands.Choice[str] | None = None,
        voice_chat_id: VoiceChannel | None = None,
    ) -> None:
        guild = self.bot.get_guild(serverId)
        if guild is None:
            await self._safeEphemeral(interaction, "Discord server not found.")
            return

        if voice_chat_id is not None:
            await self._safeDefer(interaction)
            if not isManagedVoiceChannel(voice_chat_id):
                await self._safeEphemeral(interaction, "That voice chat is static and can't be deleted.")
                return
            channelType = getManagedVoiceChatType(voice_chat_id)
            if channelType is None:
                await self._safeEphemeral(interaction, "I couldn't determine that voice chat's type.")
                return
            if not self._canCreateVoiceChat(interaction.user, channelType):
                await self._safeEphemeral(interaction, "Sorry, you can't delete that type of voice chat.")
                return
            await deleteVoiceChannel(self.bot, voice_chat_id, interaction)
            return

        if type_of_voice_chat is None:
            await self._safeEphemeral(interaction, "Choose a voice chat type or provide a voice chat ID.")
            return
        if not self._canCreateVoiceChat(interaction.user, type_of_voice_chat.value):
            await self._safeEphemeral(interaction, "Sorry, you can't delete that type of voice chat.")
            return

        await self._safeDefer(interaction)
        await deleteVoiceChannels(
            bot=self.bot,
            interaction=interaction,
            category=type_of_voice_chat.value,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: VoiceChannel) -> None:
        if isinstance(channel, VoiceChannel):
            await handleDeletedVoiceChannel(self.bot, channel)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState) -> None:
        channel: VoiceChannel | None
        if before.channel is None and after.channel is not None:
            channel = after.channel
        elif before.channel is not None and after.channel is None:
            channel = before.channel
        else:
            return

        if channel is None or len(channel.members) != 0:
            return
        if not isManagedVoiceChannel(channel):
            return
        await deleteVoiceChannel(self.bot, channel, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceChatCog(bot))

