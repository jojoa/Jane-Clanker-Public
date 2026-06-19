from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import CategoryChannel, Interaction, Member, VoiceChannel
from discord.ext import commands

import config
import runtime.interaction as interactionRuntime
from runtime import taskBudgeter
from config import permanentVoiceChatChannelIds, serverId, voiceChannelCreationCategory
from features.staff.voiceChat.breakRoomPerms import getBreakroomPerms
from features.staff.voiceChat.gameNightPerms import getGameNightPerms
from features.staff.voiceChat.shiftPerms import getShiftPerms
from features.staff.voiceChat.supervisorCommsPerms import getSupervisorCommsPerms

log = logging.getLogger(__name__)

_TRACKED_VOICE_CHAT_TYPES = (
    "Shift",
    "Gamenight",
    "Breakroom",
    "Supervisor comms",
    "TQUAL Comms",
    "Combat Training",
    "ANROSOC",
    "Site Command Office",
)
_VOICE_CHAT_NAME_PREFIXES = {
    "Shift": "Shift Comms",
    "Gamenight": "Gamenight",
    "Breakroom": "Break Room",
    "Supervisor comms": "Supervisor Communications",
}
_TEMP_VOICE_CHAT_GROUP_ORDER = (
    "Shift",
    "Gamenight",
    "Breakroom",
    "Supervisor comms",
)
_STATIC_CHANNEL_LAYOUT = (
    "ANRO Stage",
    "TQUAL Stage",
    "TQUAL Trainings VC",
    "TQUAL Exams VC",
    "Shift Comms",
    "Gamenight",
    "Breakroom",
    "Supervisor comms",
    "Site Command Office",
)
_STATIC_CHANNEL_NAMES = {name.casefold() for name in _STATIC_CHANNEL_LAYOUT}
_TEMP_CHANNEL_NAME_PATTERNS: dict[str, re.Pattern[str]] = {
    "Shift": re.compile(r"^Shift Comms (?P<number>\d+)$", re.IGNORECASE),
    "Gamenight": re.compile(r"^Gamenight (?P<number>\d+)$", re.IGNORECASE),
    "Breakroom": re.compile(r"^Break Room (?P<number>\d+)$", re.IGNORECASE),
    "Supervisor comms": re.compile(r"^Supervisor comms (?P<number>\d+)$", re.IGNORECASE),
}

_onlineVoiceChatsTotal = 0
_onlineVoiceChatsTable = {voiceChatType: [] for voiceChatType in _TRACKED_VOICE_CHAT_TYPES}
_rebalanceLock = asyncio.Lock()


async def _safeEphemeral(interaction: Interaction | None, message: str) -> None:
    if interaction is None:
        return
    await interactionRuntime.safeInteractionReply(
        interaction,
        content=message,
        ephemeral=True,
    )


def _getVoiceChatGuild(bot: commands.Bot) -> discord.Guild | None:
    return bot.get_guild(serverId)


def _getVoiceChatCategory(guild: discord.Guild | None) -> CategoryChannel | None:
    if guild is None:
        return None
    category = guild.get_channel(voiceChannelCreationCategory)
    return category if isinstance(category, CategoryChannel) else None


def isPermanentChannel(channel: VoiceChannel | None) -> bool:
    if channel is None:
        return False
    if channel.name.casefold() in _STATIC_CHANNEL_NAMES:
        return True
    return int(channel.id) in {int(rawId) for rawId in permanentVoiceChatChannelIds}


def isManagedVoiceChannel(channel: VoiceChannel | None) -> bool:
    if channel is None:
        return False
    if int(getattr(channel, "category_id", 0) or 0) != int(voiceChannelCreationCategory):
        return False
    return not isPermanentChannel(channel)


def _trackedVoiceChatTypes() -> dict[str, list[VoiceChannel]]:
    return _onlineVoiceChatsTable


def getManagedVoiceChatType(channel: VoiceChannel | None) -> str | None:
    if channel is None:
        return None
    if not isManagedVoiceChannel(channel):
        return None
    for channelType, pattern in _TEMP_CHANNEL_NAME_PATTERNS.items():
        if pattern.match(str(channel.name or "").strip()):
            return channelType
    return None


def _extractChannelNumber(channelType: str, channelName: str) -> int:
    pattern = _TEMP_CHANNEL_NAME_PATTERNS.get(channelType)
    if pattern is None:
        return 0
    match = pattern.match(str(channelName or "").strip())
    if match is None:
        return 0
    try:
        return int(match.group("number"))
    except (TypeError, ValueError):
        return 0


def _trackVoiceChannel(channelType: str, voiceChannel: VoiceChannel) -> None:
    global _onlineVoiceChatsTotal
    localTable = _onlineVoiceChatsTable.setdefault(channelType, [])
    if any(int(existing.id) == int(voiceChannel.id) for existing in localTable):
        return
    localTable.append(voiceChannel)
    _onlineVoiceChatsTotal += 1


def _untrackVoiceChannel(voiceChannel: VoiceChannel) -> str | None:
    global _onlineVoiceChatsTotal
    for channelType, trackedChannels in _onlineVoiceChatsTable.items():
        for index, trackedChannel in enumerate(list(trackedChannels)):
            if int(trackedChannel.id) != int(voiceChannel.id):
                continue
            trackedChannels.pop(index)
            _onlineVoiceChatsTotal = max(0, _onlineVoiceChatsTotal - 1)
            return channelType
    return None


def resetVoiceChatsTable() -> None:
    global _onlineVoiceChatsTotal
    global _onlineVoiceChatsTable
    _onlineVoiceChatsTotal = 0
    _onlineVoiceChatsTable = {voiceChatType: [] for voiceChatType in _TRACKED_VOICE_CHAT_TYPES}


def _nextVoiceChannelNumber(channelType: str, existingChannels: list[VoiceChannel]) -> int:
    highestNumber = 0
    for channel in existingChannels:
        highestNumber = max(highestNumber, _extractChannelNumber(channelType, str(channel.name or "")))
    return highestNumber + 1


def _nextVoiceChannelName(channelType: str, existingChannels: list[VoiceChannel]) -> str:
    channelPrefix = _VOICE_CHAT_NAME_PREFIXES[channelType]
    channelNumber = _nextVoiceChannelNumber(channelType, existingChannels)
    return f"{channelPrefix} {channelNumber}"


async def syncTrackedVoiceChats(
    bot: commands.Bot,
    *,
    rebalance: bool = False,
) -> dict[str, list[VoiceChannel]]:
    resetVoiceChatsTable()
    guild = _getVoiceChatGuild(bot)
    category = _getVoiceChatCategory(guild)
    if category is None:
        return _trackedVoiceChatTypes()

    for voiceChannel in list(category.voice_channels):
        channelType = getManagedVoiceChatType(voiceChannel)
        if channelType is None:
            continue
        _trackVoiceChannel(channelType, voiceChannel)
    if rebalance:
        await _rebalanceVoiceChatCategory(bot)
    return _trackedVoiceChatTypes()


async def _rebalanceVoiceChatCategory(bot: commands.Bot) -> None:
    if _rebalanceLock.locked():
        log.info("Voice chat category rebalance skipped; another rebalance is already running.")
        return

    guild = _getVoiceChatGuild(bot)
    category = _getVoiceChatCategory(guild)
    if category is None:
        return

    async with _rebalanceLock:
        orderedChannels = list(category.channels)
        if not orderedChannels:
            return

        staticChannelsByName = {
            str(channel.name).casefold(): channel
            for channel in orderedChannels
            if str(channel.name).casefold() in _STATIC_CHANNEL_NAMES
        }
        tempChannelsByType: dict[str, list[discord.abc.GuildChannel]] = {
            channelType: [] for channelType in _TEMP_VOICE_CHAT_GROUP_ORDER
        }
        remainderChannels: list[discord.abc.GuildChannel] = []

        for channel in orderedChannels:
            channelName = str(channel.name or "").casefold()
            if channelName in _STATIC_CHANNEL_NAMES:
                continue
            if isinstance(channel, VoiceChannel):
                channelType = getManagedVoiceChatType(channel)
                if channelType is not None and channelType in tempChannelsByType:
                    tempChannelsByType[channelType].append(channel)
                    continue
            remainderChannels.append(channel)

        desiredOrder: list[discord.abc.GuildChannel] = []
        for staticName in _STATIC_CHANNEL_LAYOUT:
            staticChannel = staticChannelsByName.get(staticName.casefold())
            if staticChannel is not None:
                desiredOrder.append(staticChannel)
        for channelType in _TEMP_VOICE_CHAT_GROUP_ORDER:
            desiredOrder.extend(
                sorted(
                    tempChannelsByType[channelType],
                    key=lambda channel: (
                        _extractChannelNumber(channelType, str(channel.name or "")),
                        int(getattr(channel, "id", 0) or 0),
                    ),
                )
            )
        desiredOrder.extend(remainderChannels)

        currentOrderIds = [int(channel.id) for channel in orderedChannels]
        desiredOrderIds = [int(channel.id) for channel in desiredOrder]
        if currentOrderIds == desiredOrderIds:
            return

        targetPositions = sorted(int(channel.position) for channel in orderedChannels)
        plannedEdits: list[tuple[discord.abc.GuildChannel, int]] = []
        for index, channel in enumerate(desiredOrder):
            targetPosition = targetPositions[index]
            if int(getattr(channel, "position", 0) or 0) != targetPosition:
                plannedEdits.append((channel, targetPosition))

        maxEdits = max(0, int(getattr(config, "voiceChatRebalanceMaxPositionEdits", 4) or 4))
        editDelaySec = max(0.0, float(getattr(config, "voiceChatRebalanceEditDelaySec", 1.25) or 1.25))
        editsToRun = plannedEdits[:maxEdits]
        skippedEdits = max(0, len(plannedEdits) - len(editsToRun))
        for index, (channel, targetPosition) in enumerate(editsToRun):
            try:
                await taskBudgeter.runLowPriorityDiscord(
                    lambda channel=channel, targetPosition=targetPosition: channel.edit(position=targetPosition)
                )
                if editDelaySec > 0 and index < len(editsToRun) - 1:
                    await asyncio.sleep(editDelaySec)
            except Exception:
                log.exception("Failed to rebalance voice chat channel order for %s.", channel)
        if skippedEdits:
            log.warning(
                "Voice chat category rebalance stopped after %d position edit(s); %d move(s) left for a later run.",
                len(editsToRun),
                skippedEdits,
            )


async def _createVoiceChannel(
    *,
    bot: commands.Bot,
    channelType: str,
    overwrites: dict[object, discord.PermissionOverwrite],
) -> VoiceChannel | None:
    guild = _getVoiceChatGuild(bot)
    if guild is None:
        log.warning("Failed to create %s vc: guild %s not found.", channelType, serverId)
        return None

    category = _getVoiceChatCategory(guild)
    if category is None:
        log.warning(
            "Failed to create %s vc: category %s not found.",
            channelType,
            voiceChannelCreationCategory,
        )
        return None

    await syncTrackedVoiceChats(bot)
    existingChannels = list(category.voice_channels)
    channelName = _nextVoiceChannelName(channelType, existingChannels)
    try:
        createdChannel = await category.create_voice_channel(channelName, overwrites=overwrites)
    except Exception:
        log.exception("Failed to create %s vc in %s.", channelType, category)
        return None

    if isinstance(createdChannel, VoiceChannel):
        _trackVoiceChannel(channelType, createdChannel)
        await _rebalanceVoiceChatCategory(bot)
    return createdChannel if isinstance(createdChannel, VoiceChannel) else None


async def handleDeletedVoiceChannel(bot: commands.Bot, deletedChannel: VoiceChannel) -> None:
    _untrackVoiceChannel(deletedChannel)


async def deleteVoiceChannel(
    bot: commands.Bot,
    voiceChannel: VoiceChannel,
    interaction: Interaction | None,
    *,
    rebalance: bool = True,
) -> bool:
    guild = _getVoiceChatGuild(bot)
    if guild is None:
        log.warning("Failed to delete vc: guild %s not found.", serverId)
        await _safeEphemeral(interaction, "Failed to delete voice chat.")
        return False

    liveChannel = guild.get_channel(int(voiceChannel.id))
    if not isinstance(liveChannel, VoiceChannel):
        log.info("Voice chat %s was already deleted before cleanup.", int(voiceChannel.id))
        await handleDeletedVoiceChannel(bot, voiceChannel)
        await _safeEphemeral(interaction, "Voice chat was already deleted.")
        return False
    if not isManagedVoiceChannel(liveChannel):
        await _safeEphemeral(interaction, "That voice chat is static and can't be deleted.")
        return False

    try:
        await liveChannel.delete()
    except discord.NotFound:
        log.info(
            "Voice chat %s disappeared before deletion completed.",
            int(liveChannel.id),
        )
        await handleDeletedVoiceChannel(bot, liveChannel)
        if rebalance:
            await _rebalanceVoiceChatCategory(bot)
        await _safeEphemeral(interaction, "Voice chat was already deleted.")
        return False
    except Exception:
        log.exception("Failed to delete vc %s.", liveChannel)
        await _safeEphemeral(interaction, "Failed to delete voice chat.")
        return False

    await handleDeletedVoiceChannel(bot, liveChannel)
    if rebalance:
        await _rebalanceVoiceChatCategory(bot)
    await _safeEphemeral(interaction, "Deleted voice chat.")
    return True


async def deleteVoiceChannels(
    bot: commands.Bot,
    interaction: Interaction | None,
    category: str,
) -> int:
    guild = _getVoiceChatGuild(bot)
    voiceCategory = _getVoiceChatCategory(guild)
    if voiceCategory is None:
        await _safeEphemeral(interaction, "Voice chat category is unavailable.")
        return 0

    localCategoryTable = [
        channel
        for channel in list(voiceCategory.voice_channels)
        if getManagedVoiceChatType(channel) == category
    ]
    if not localCategoryTable:
        await _safeEphemeral(interaction, f"No tracked `{category}` voice chats were found.")
        return 0

    deletedCount = 0
    for voiceChannel in localCategoryTable:
        deleted = await deleteVoiceChannel(
            bot=bot,
            voiceChannel=voiceChannel,
            interaction=None,
            rebalance=False,
        )
        if deleted:
            deletedCount += 1

    if deletedCount > 0:
        await _rebalanceVoiceChatCategory(bot)

    if deletedCount > 0:
        await _safeEphemeral(interaction, f"Deleted {deletedCount} `{category}` voice chat(s).")
    else:
        await _safeEphemeral(interaction, f"No `{category}` voice chats could be deleted.")
    return deletedCount


async def createShiftVoiceChatWithPerms(
    bot: commands.Bot,
    cohost1: Member | None,
    cohost2: Member | None,
) -> VoiceChannel | None:
    return await _createVoiceChannel(
        bot=bot,
        channelType="Shift",
        overwrites=getShiftPerms(cohost1=cohost1, cohost2=cohost2, bot=bot),
    )


async def createGamenightVoiceChatWithPerms(
    bot: commands.Bot,
    cohost1: Member | None,
    cohost2: Member | None,
) -> VoiceChannel | None:
    return await _createVoiceChannel(
        bot=bot,
        channelType="Gamenight",
        overwrites=getGameNightPerms(cohost1=cohost1, cohost2=cohost2, bot=bot),
    )


async def createBreakroomVoiceChatWithPerms(
    bot: commands.Bot,
    cohost1: Member | None = None,
    cohost2: Member | None = None,
) -> VoiceChannel | None:
    return await _createVoiceChannel(
        bot=bot,
        channelType="Breakroom",
        overwrites=getBreakroomPerms(bot=bot, cohost1=cohost1, cohost2=cohost2),
    )


async def createSupervisorCommsVoiceChatWithPerms(
    bot: commands.Bot,
    cohost1: Member | None = None,
    cohost2: Member | None = None,
) -> VoiceChannel | None:
    return await _createVoiceChannel(
        bot=bot,
        channelType="Supervisor comms",
        overwrites=getSupervisorCommsPerms(bot=bot, cohost1=cohost1, cohost2=cohost2),
    )


async def cleanVoiceChatsCategory(bot: commands.Bot, interaction: Interaction) -> int:
    await syncTrackedVoiceChats(bot)
    guild = _getVoiceChatGuild(bot)
    if guild is None:
        await _safeEphemeral(interaction, "Voice chat guild is unavailable.")
        return 0

    category = _getVoiceChatCategory(guild)
    if category is None:
        await _safeEphemeral(interaction, "Voice chat category is unavailable.")
        return 0

    deletedCount = 0
    for voiceChannel in list(category.voice_channels):
        if not isManagedVoiceChannel(voiceChannel):
            log.info("%s is a permanent or unmanaged voice chat channel.", voiceChannel.name)
            continue
        log.info("Removing voice chat channel: %s", voiceChannel.name)
        deleted = await deleteVoiceChannel(
            bot=bot,
            voiceChannel=voiceChannel,
            interaction=None,
            rebalance=False,
        )
        if deleted:
            deletedCount += 1

    if deletedCount > 0:
        await _rebalanceVoiceChatCategory(bot)

    await _safeEphemeral(interaction, f"Cleaned up {deletedCount} voice chat(s).")
    return deletedCount

