from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.community.events import service as eventService
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions
from runtime import timezones as timezoneRuntime


log = logging.getLogger(__name__)
_EMPTY_TEXT = "(none)"
_optionsKeyValueRegex = re.compile(r"^\s*([a-zA-Z0-9_-]+)\s*[:=]\s*(.+?)\s*$")


def _parsePositiveInt(raw: str) -> Optional[int]:
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parseRoleIdsFromText(raw: str) -> list[int]:
    text = str(raw or "").strip()
    if not text:
        return []
    ids: list[int] = []
    for token in re.findall(r"\d+", text):
        parsed = _parsePositiveInt(token)
        if parsed is None or parsed in ids:
            continue
        ids.append(parsed)
    return ids


def _parseBoolText(raw: str, *, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parseEventOptions(optionsText: str) -> tuple[int, bool, list[int]]:
    capacity = 0
    lockRsvpAtStart = False
    pingRoleIds: list[int] = []

    raw = str(optionsText or "").strip()
    if not raw:
        return capacity, lockRsvpAtStart, pingRoleIds

    chunks = []
    for segment in raw.replace("\n", ";").split(";"):
        text = segment.strip()
        if text:
            chunks.append(text)

    for chunk in chunks:
        match = _optionsKeyValueRegex.match(chunk)
        if not match:
            continue
        key = str(match.group(1) or "").strip().lower()
        value = str(match.group(2) or "").strip()
        if key in {"cap", "capacity", "attendeecap"}:
            parsed = _parsePositiveInt(value)
            capacity = int(parsed or 0)
        elif key in {"lock", "rsvplock", "lockrsvp", "lockatstart"}:
            lockRsvpAtStart = _parseBoolText(value, default=False)
        elif key in {"pings", "roles", "pingroles", "pingroleid", "pingroleids"}:
            pingRoleIds = _parseRoleIdsFromText(value)

    return capacity, lockRsvpAtStart, pingRoleIds


def _parseEventPingRoleIds(event: dict) -> list[int]:
    raw = str(event.get("pingRoleIdsJson") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for value in data:
        parsed = _parsePositiveInt(str(value))
        if parsed is None or parsed in out:
            continue
        out.append(parsed)
    return out


def _normalizeGuildRoleIds(guild: discord.Guild, roleIds: list[int]) -> list[int]:
    out: list[int] = []
    for roleId in roleIds:
        parsed = _parsePositiveInt(str(roleId))
        if parsed is None:
            continue
        if guild.get_role(parsed) is None:
            continue
        if parsed not in out:
            out.append(parsed)
    return out


def _formatMentions(userIds: list[int]) -> str:
    if not userIds:
        return _EMPTY_TEXT
    return "\n".join(f"<@{int(userId)}>" for userId in userIds)


def _formatInlineUserMentions(userIds: list[int]) -> str:
    return " ".join(f"<@{int(userId)}>" for userId in userIds if int(userId) > 0)


def _formatInlineRoleMentions(roleIds: list[int]) -> str:
    return " ".join(f"<@&{int(roleId)}>" for roleId in roleIds if int(roleId) > 0)


def _combineMentionChunks(*chunks: str) -> str:
    return " ".join(str(chunk).strip() for chunk in chunks if str(chunk).strip()).strip()


def _eventAllowedMentions() -> discord.AllowedMentions:
    return discord.AllowedMentions(users=True, roles=True, everyone=False)


def _splitRsvps(rows: list[dict]) -> tuple[list[int], list[int]]:
    attending: list[int] = []
    tentative: list[int] = []
    for row in rows:
        try:
            userId = int(row.get("userId") or 0)
        except (TypeError, ValueError):
            continue
        if userId <= 0:
            continue
        response = str(row.get("response") or "").strip().upper()
        if response == "ATTENDING":
            attending.append(userId)
        elif response == "TENTATIVE":
            tentative.append(userId)
    return attending, tentative


def _parseEventAtUtc(eventAtRaw: str) -> Optional[datetime]:
    raw = str(eventAtRaw or "").strip()
    if not raw:
        return None
    try:
        eventAtUtc = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if eventAtUtc.tzinfo is None:
        eventAtUtc = eventAtUtc.replace(tzinfo=timezone.utc)
    return eventAtUtc.astimezone(timezone.utc)


def _formatUpcomingEventTitle(event: dict, guildId: int) -> str:
    title = str(event.get("title") or "Scheduled Event").strip() or "Scheduled Event"
    messageId = int(event.get("messageId") or 0)
    channelId = int(event.get("channelId") or 0)
    if guildId > 0 and messageId > 0 and channelId > 0:
        jumpUrl = f"https://discord.com/channels/{guildId}/{channelId}/{messageId}"
        return f"[{title}]({jumpUrl})"
    return title


def _buildEventEmbed(event: dict, rsvpRows: list[dict]) -> discord.Embed:
    title = str(event.get("title") or "Scheduled Event").strip() or "Scheduled Event"
    subtitle = str(event.get("subtitle") or "").strip()
    description = subtitle if subtitle else "RSVP below."
    embed = discord.Embed(title=title, description=description)

    eventAtRaw = str(event.get("eventAtUtc") or "").strip()
    eventAtUtc = None
    try:
        eventAtUtc = datetime.fromisoformat(eventAtRaw)
    except ValueError:
        eventAtUtc = None
    if eventAtUtc is not None:
        if eventAtUtc.tzinfo is None:
            eventAtUtc = eventAtUtc.replace(tzinfo=timezone.utc)
        unixTs = int(eventAtUtc.timestamp())
        whenValue = f"<t:{unixTs}:F>\n<t:{unixTs}:R>"
    else:
        whenValue = eventAtRaw or "Unknown"
    embed.add_field(name="When", value=whenValue, inline=False)

    attendingIds, tentativeIds = _splitRsvps(rsvpRows)
    embed.add_field(
        name=f":white_check_mark: Attending ({len(attendingIds)})",
        value=_formatMentions(attendingIds),
        inline=True,
    )
    embed.add_field(
        name=f":yellow_square: May Attend ({len(tentativeIds)})",
        value=_formatMentions(tentativeIds),
        inline=True,
    )
    creatorId = int(event.get("creatorId") or 0)
    creatorText = f"Hosted by <@{creatorId}>" if creatorId > 0 else "Hosted by unknown"
    embed.add_field(name="\u200b", value=creatorText, inline=False)

    maxAttendees = max(0, int(event.get("maxAttendees") or 0))
    lockRsvpAtStart = bool(int(event.get("lockRsvpAtStart") or 0))
    pingRoleIds = _parseEventPingRoleIds(event)
    settingsLines: list[str] = []
    if maxAttendees > 0:
        settingsLines.append(f"Capacity: `{maxAttendees}`")
    if lockRsvpAtStart:
        settingsLines.append("Lock RSVP at start: `Yes`")
    if settingsLines:
        embed.add_field(name="RSVP Settings", value="\n".join(settingsLines), inline=False)

    if pingRoleIds:
        pingRolesText = _formatInlineRoleMentions(pingRoleIds)
        embed.add_field(name="Ping Roles", value=pingRolesText, inline=False)
    return embed


class EventCreateModal(discord.ui.Modal, title="Create Event"):
    titleInput = discord.ui.TextInput(
        label="Event Title",
        required=True,
        max_length=100,
        placeholder="Example: Joint Shift with NES",
    )
    subtitleInput = discord.ui.TextInput(
        label="Subtitle (optional)",
        required=False,
        max_length=150,
        placeholder="Example: Hosted by CE",
    )
    dateInput = discord.ui.TextInput(
        label="Date (YYYY-MM-DD)",
        required=False,
        max_length=10,
        placeholder="Optional (defaults to today)",
    )
    timeAndTimezoneInput = discord.ui.TextInput(
        label="Time + Timezone",
        required=True,
        max_length=120,
        placeholder="19:30 EST (or UTC-6)",
    )
    optionsInput = discord.ui.TextInput(
        label="Options (optional)",
        required=False,
        max_length=250,
        placeholder="attendeeCap=25; pingRoleID: 12345, 54321",
    )

    def __init__(
        self,
        cog: "EventCog",
        *,
        lockRsvpAtStart: bool,
    ):
        super().__init__()
        self.cog = cog
        self.lockRsvpAtStart = bool(lockRsvpAtStart)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handleCreateEventModal(interaction, self)


class EventEditModal(discord.ui.Modal, title="Edit Event"):
    titleInput = discord.ui.TextInput(
        label="Event Title",
        required=True,
        max_length=100,
    )
    subtitleInput = discord.ui.TextInput(
        label="Subtitle (optional)",
        required=False,
        max_length=150,
    )
    dateInput = discord.ui.TextInput(
        label="Date (YYYY-MM-DD)",
        required=True,
        max_length=10,
    )
    timeAndTimezoneInput = discord.ui.TextInput(
        label="Time + Timezone",
        required=True,
        max_length=120,
        placeholder="19:30 EST (or UTC-6)",
    )
    optionsInput = discord.ui.TextInput(
        label="Options (optional: cap / lock / pings)",
        required=False,
        max_length=250,
        placeholder="attendeeCap=25; pingRoleID: 12345, 54321",
    )

    def __init__(self, cog: "EventCog", eventId: int, event: dict):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        self.titleInput.default = str(event.get("title") or "").strip()
        self.subtitleInput.default = str(event.get("subtitle") or "").strip()

        eventAtRaw = str(event.get("eventAtUtc") or "").strip()
        timezoneName = str(event.get("timezone") or "UTC").strip() or "UTC"
        timezoneLabel = timezoneRuntime.displayTimezoneLabel(timezoneName)
        dtLocal = None
        try:
            dtUtc = datetime.fromisoformat(eventAtRaw)
            if dtUtc.tzinfo is None:
                dtUtc = dtUtc.replace(tzinfo=timezone.utc)
            tzInfo, _ = timezoneRuntime.resolveTimezoneToken(timezoneName, allowIana=True)
            dtLocal = dtUtc.astimezone(tzInfo)
        except Exception:
            dtLocal = None

        if dtLocal is not None:
            self.dateInput.default = dtLocal.strftime("%Y-%m-%d")
            self.timeAndTimezoneInput.default = f"{dtLocal.strftime('%H:%M')} {timezoneLabel}"
        else:
            self.timeAndTimezoneInput.default = f"00:00 {timezoneLabel}"

        maxAttendees = max(0, int(event.get("maxAttendees") or 0))
        lockRsvpAtStart = bool(int(event.get("lockRsvpAtStart") or 0))
        pingRoleIds = _parseEventPingRoleIds(event)
        optionsParts: list[str] = []
        if maxAttendees > 0:
            optionsParts.append(f"cap={maxAttendees}")
        optionsParts.append(f"lock={'yes' if lockRsvpAtStart else 'no'}")
        if pingRoleIds:
            optionsParts.append(f"pings={','.join(str(roleId) for roleId in pingRoleIds)}")
        self.optionsInput.default = "; ".join(optionsParts)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handleEditEventModal(interaction, self.eventId, self)


class EventView(discord.ui.View):
    def __init__(self, cog: "EventCog", eventId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.eventId = int(eventId)

    @discord.ui.button(
        label="Attending",
        style=discord.ButtonStyle.success,
        custom_id="scheduled_event:rsvp_attending",
        row=0,
    )
    async def attendingBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleRsvp(interaction, self.eventId, "ATTENDING")

    @discord.ui.button(
        label="May Attend",
        style=discord.ButtonStyle.secondary,
        custom_id="scheduled_event:rsvp_tentative",
        row=0,
    )
    async def tentativeBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleRsvp(interaction, self.eventId, "TENTATIVE")

    @discord.ui.button(
        label="Edit Event",
        style=discord.ButtonStyle.primary,
        custom_id="scheduled_event:edit",
        row=1,
    )
    async def editBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.openEditEvent(interaction, self.eventId)

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.danger,
        custom_id="scheduled_event:delete",
        row=1,
    )
    async def deleteBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.deleteEvent(interaction, self.eventId)


class EventCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._eventLocks: dict[int, asyncio.Lock] = {}
        self._reminderTask: asyncio.Task | None = None

    async def cog_load(self) -> None:
        restored = await self._restoreEventViews()
        log.info("Scheduled event views restored: %d", restored)
        if self._reminderTask is None or self._reminderTask.done():
            self._reminderTask = asyncio.create_task(self._runEventReminderLoop())

    def cog_unload(self) -> None:
        if self._reminderTask and not self._reminderTask.done():
            self._reminderTask.cancel()
        self._reminderTask = None

    def _getEventLock(self, eventId: int) -> asyncio.Lock:
        lock = self._eventLocks.get(int(eventId))
        if lock is None:
            lock = asyncio.Lock()
            self._eventLocks[int(eventId)] = lock
        return lock

    async def _getMessageChannel(self, channelId: int) -> Optional[discord.TextChannel | discord.Thread]:
        if int(channelId) <= 0:
            return None
        channel = self.bot.get_channel(int(channelId))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channelId))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def _allowedCreatorRoleIds(self) -> set[int]:
        roleIds: set[int] = set()
        for raw in (
            getattr(config, "middleRankRoleId", None),
            getattr(config, "highRankRoleId", None),
        ):
            parsed = _parsePositiveInt(str(raw)) if raw is not None else None
            if parsed is not None:
                roleIds.add(parsed)
        return roleIds

    def _canCreateEvent(self, member: discord.Member) -> bool:
        if runtimePermissions.hasAdminOrManageGuild(member):
            return True
        allowedRoleIds = self._allowedCreatorRoleIds()
        if not allowedRoleIds:
            return False
        return any(int(role.id) in allowedRoleIds for role in member.roles)

    async def _ensureEventCreatePermission(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not interaction.channel:
            await self._safeEphemeral(interaction, "This command can only be used in a server channel.")
            return False
        if not isinstance(interaction.user, discord.Member):
            await self._safeEphemeral(interaction, "This command can only be used in a server channel.")
            return False
        if not self._canCreateEvent(interaction.user):
            await self._safeEphemeral(
                interaction,
                "You do not have permission to create events.",
            )
            return False
        return True

    def _parseScheduleAndOptions(
        self,
        *,
        dateText: str,
        timeAndTimezoneText: str,
        optionsText: str,
        guild: Optional[discord.Guild],
    ) -> tuple[datetime, str, int, bool, list[int]]:
        eventAtUtc, timezoneName = timezoneRuntime.parseDateTimeWithTimezone(
            str(dateText),
            str(timeAndTimezoneText),
            allowIana=False,
        )
        capacity, lockRsvpAtStart, pingRoleIds = _parseEventOptions(str(optionsText or ""))
        if guild is not None:
            pingRoleIds = _normalizeGuildRoleIds(guild, pingRoleIds)
        return eventAtUtc, timezoneName, capacity, lockRsvpAtStart, pingRoleIds

    async def _restoreEventViews(self) -> int:
        restored = 0
        activeEvents = await eventService.listActiveScheduledEvents()
        for event in activeEvents:
            messageId = int(event.get("messageId") or 0)
            eventId = int(event.get("eventId") or 0)
            if messageId <= 0 or eventId <= 0:
                continue
            self.bot.add_view(EventView(self, eventId), message_id=messageId)
            restored += 1
        return restored

    async def _safeEphemeral(self, interaction: discord.Interaction, content: str) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=content,
            ephemeral=True,
        )

    async def _runEventReminderLoop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        while True:
            try:
                await self._runEventRemindersTick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled event reminder loop failed.")
            await asyncio.sleep(60)

    async def _runEventRemindersTick(self) -> None:
        nowUtc = datetime.now(timezone.utc)
        allEvents = await eventService.listActiveScheduledEvents()
        leadMinutes = 20
        for event in allEvents:
            if str(event.get("status") or "").strip().upper() != "ACTIVE":
                continue
            if str(event.get("reminderSentAt") or "").strip():
                continue
            messageId = int(event.get("messageId") or 0)
            channelId = int(event.get("channelId") or 0)
            eventId = int(event.get("eventId") or 0)
            if eventId <= 0 or messageId <= 0 or channelId <= 0:
                continue

            eventAtRaw = str(event.get("eventAtUtc") or "").strip()
            try:
                eventAtUtc = datetime.fromisoformat(eventAtRaw)
            except ValueError:
                continue
            if eventAtUtc.tzinfo is None:
                eventAtUtc = eventAtUtc.replace(tzinfo=timezone.utc)
            dueAtUtc = eventAtUtc - timedelta(minutes=leadMinutes)
            if nowUtc < dueAtUtc:
                continue

            rsvpRows = await eventService.listScheduledEventRsvps(eventId)
            attendingIds, _ = _splitRsvps(rsvpRows)
            if not attendingIds:
                # No attendees yet; keep waiting so late RSVPs still get a reminder thread.
                continue
            creatorId = int(event.get("creatorId") or 0)
            attendeePingIds = [int(userId) for userId in attendingIds if int(userId) > 0]
            if creatorId > 0 and creatorId not in attendeePingIds:
                attendeePingIds.insert(0, creatorId)
            pingRoleIds = _parseEventPingRoleIds(event)
            mentionText = _combineMentionChunks(
                _formatInlineRoleMentions(pingRoleIds),
                _formatInlineUserMentions(attendeePingIds),
            )

            channel = await self._getMessageChannel(channelId)
            if channel is None:
                continue
            if isinstance(channel, discord.Thread):
                try:
                    await channel.send(
                        content=mentionText,
                        allowed_mentions=_eventAllowedMentions(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    continue
                await eventService.markScheduledEventReminderSent(eventId, reminderThreadId=None)
                continue

            try:
                eventMessage = await channel.fetch_message(messageId)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue

            rawThreadName = str(event.get("title") or "").strip()
            threadName = (rawThreadName or f"Event {eventId}").strip()[:100]
            if not threadName:
                threadName = f"Event {eventId}"
            reminderThreadId: int | None = None
            try:
                thread = await eventMessage.create_thread(name=threadName, auto_archive_duration=1440)
                reminderThreadId = int(thread.id)
                await thread.send(
                    content=mentionText,
                    allowed_mentions=_eventAllowedMentions(),
                )
            except (discord.Forbidden, discord.HTTPException):
                # Fallback if thread creation fails: ping in-channel once.
                try:
                    await channel.send(
                        content=mentionText,
                        allowed_mentions=_eventAllowedMentions(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    continue

            await eventService.markScheduledEventReminderSent(
                eventId,
                reminderThreadId=reminderThreadId,
            )

    async def _refreshEventMessage(
        self,
        eventId: int,
        *,
        message: Optional[discord.Message] = None,
    ) -> None:
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            return
        rsvps = await eventService.listScheduledEventRsvps(int(eventId))
        embed = _buildEventEmbed(event, rsvps)
        view = EventView(self, int(eventId))
        if str(event.get("status") or "").strip().upper() != "ACTIVE":
            for child in view.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

        targetMessage = message
        if targetMessage is None:
            channelId = int(event.get("channelId") or 0)
            messageId = int(event.get("messageId") or 0)
            if channelId <= 0 or messageId <= 0:
                return
            channel = await self._getMessageChannel(channelId)
            if channel is None:
                return
            try:
                targetMessage = await channel.fetch_message(messageId)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return

        try:
            await targetMessage.edit(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _isCreator(self, interaction: discord.Interaction, event: dict) -> bool:
        creatorId = int(event.get("creatorId") or 0)
        return int(interaction.user.id) == creatorId

    # Legacy slash handler kept for future reuse.
    async def events(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await self._safeEphemeral(interaction, "This command can only be used in a server.")
            return

        rows = await eventService.listActiveScheduledEventsWithRsvpCounts()
        nowUtc = datetime.now(timezone.utc)
        upcomingRows: list[tuple[datetime, dict]] = []
        for row in rows:
            eventAtUtc = _parseEventAtUtc(str(row.get("eventAtUtc") or ""))
            if eventAtUtc is None:
                continue
            if eventAtUtc < nowUtc:
                continue
            upcomingRows.append((eventAtUtc, row))

        if not upcomingRows:
            embed = discord.Embed(
                title="Upcoming Events",
                description="Server timezone: `UTC`\n\nNo upcoming events are scheduled.",
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed)
            return

        lines: list[str] = []
        currentDayKey: Optional[tuple[int, int, int]] = None
        maxDescriptionLength = 3900
        renderedCount = 0

        for eventAtUtc, row in upcomingRows:
            dayKey = (eventAtUtc.year, eventAtUtc.month, eventAtUtc.day)
            if dayKey != currentDayKey:
                if lines:
                    lines.append("")
                lines.append(f"**{eventAtUtc.strftime('%A')} - {eventAtUtc.strftime('%B')} {eventAtUtc.day}**")
                currentDayKey = dayKey

            unixTs = int(eventAtUtc.timestamp())
            attendingCount = int(row.get("attendingCount") or 0)
            maxAttendees = max(0, int(row.get("maxAttendees") or 0))
            attendanceText = f"({attendingCount}/{maxAttendees})" if maxAttendees > 0 else f"({attendingCount})"
            eventTitle = _formatUpcomingEventTitle(row, int(interaction.guild.id))
            eventLine = f"- <t:{unixTs}:t> {eventTitle} {attendanceText} <t:{unixTs}:R>"
            tentativeDescription = "Server timezone: `UTC`\n\n" + "\n".join(lines + [eventLine])
            if len(tentativeDescription) > maxDescriptionLength:
                break
            lines.append(eventLine)
            renderedCount += 1

        if renderedCount < len(upcomingRows):
            remaining = len(upcomingRows) - renderedCount
            lines.append("")
            lines.append(f"...and {remaining} more event(s).")

        description = "Server timezone: `UTC`\n\n" + "\n".join(lines)
        embed = discord.Embed(
            title="Upcoming Events",
            description=description,
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # Legacy slash handler kept for future reuse.
    async def event(
        self,
        interaction: discord.Interaction,
        lock_rsvp_at_start: bool = False,
    ) -> None:
        if not await self._ensureEventCreatePermission(interaction):
            return
        modal = EventCreateModal(
            self,
            lockRsvpAtStart=lock_rsvp_at_start,
        )
        await interactionRuntime.safeInteractionSendModal(interaction, modal)

    async def handleCreateEventModal(
        self,
        interaction: discord.Interaction,
        modal: EventCreateModal,
    ) -> None:
        if not await self._ensureEventCreatePermission(interaction):
            return
        try:
            eventAtUtc, timezoneName = timezoneRuntime.parseDateTimeWithTimezone(
                str(modal.dateInput.value),
                str(modal.timeAndTimezoneInput.value),
                allowIana=False,
            )
        except ValueError as exc:
            await self._safeEphemeral(interaction, str(exc))
            return

        capacity, _, pingRoleIds = _parseEventOptions(str(modal.optionsInput.value or ""))
        if interaction.guild is not None:
            pingRoleIds = _normalizeGuildRoleIds(interaction.guild, pingRoleIds)

        lockRsvpAtStart = bool(modal.lockRsvpAtStart)

        duplicate = await eventService.findDuplicateActiveEvent(
            guildId=int(interaction.guild.id),
            creatorId=int(interaction.user.id),
            title=str(modal.titleInput.value or "").strip(),
            eventAtUtcIso=eventAtUtc.isoformat(),
        )
        if duplicate:
            await self._safeEphemeral(
                interaction,
                f"You already have an active event with this title and start time (Event ID: {int(duplicate.get('eventId') or 0)}).",
            )
            return

        eventId = await eventService.createScheduledEvent(
            guildId=int(interaction.guild.id),
            channelId=int(interaction.channel.id),
            creatorId=int(interaction.user.id),
            title=str(modal.titleInput.value or "").strip(),
            subtitle=str(modal.subtitleInput.value or "").strip() or None,
            eventAtUtcIso=eventAtUtc.isoformat(),
            timezoneName=timezoneName,
            maxAttendees=capacity,
            lockRsvpAtStart=lockRsvpAtStart,
            pingRoleIds=pingRoleIds,
        )
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            await self._safeEphemeral(interaction, "Could not create event.")
            return

        embed = _buildEventEmbed(event, [])
        view = EventView(self, int(eventId))

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._safeEphemeral(interaction, "This channel type is not supported for events.")
            return
        roleMentionText = _formatInlineRoleMentions(pingRoleIds)
        try:
            message = await channel.send(
                content=roleMentionText or None,
                embed=embed,
                view=view,
                allowed_mentions=_eventAllowedMentions(),
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._safeEphemeral(interaction, "I could not post the event in this channel.")
            return

        await eventService.setScheduledEventMessageId(int(eventId), int(message.id))
        await self._safeEphemeral(interaction, "Event created.")

    async def handleRsvp(self, interaction: discord.Interaction, eventId: int, response: str) -> None:
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            await self._safeEphemeral(interaction, "This event no longer exists.")
            return
        if str(event.get("status") or "").strip().upper() != "ACTIVE":
            await self._safeEphemeral(interaction, "This event is no longer active.")
            return

        async with self._getEventLock(int(eventId)):
            event = await eventService.getScheduledEvent(int(eventId))
            if not event:
                await self._safeEphemeral(interaction, "This event no longer exists.")
                return

            lockAtStart = bool(int(event.get("lockRsvpAtStart") or 0))
            if lockAtStart:
                eventAtRaw = str(event.get("eventAtUtc") or "").strip()
                try:
                    eventAtUtc = datetime.fromisoformat(eventAtRaw)
                except ValueError:
                    eventAtUtc = None
                if eventAtUtc is not None:
                    if eventAtUtc.tzinfo is None:
                        eventAtUtc = eventAtUtc.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) >= eventAtUtc:
                        await self._safeEphemeral(
                            interaction,
                            "RSVP is locked because this event has started.",
                        )
                        return

            maxAttendees = max(0, int(event.get("maxAttendees") or 0))
            rsvpRows = await eventService.listScheduledEventRsvps(int(eventId))
            attendingIds, _ = _splitRsvps(rsvpRows)
            normalizedResponse = str(response or "").strip().upper()
            userId = int(interaction.user.id)
            alreadyAttending = userId in attendingIds
            if normalizedResponse == "ATTENDING" and maxAttendees > 0 and not alreadyAttending:
                if len(attendingIds) >= maxAttendees:
                    await self._safeEphemeral(
                        interaction,
                        f"This event is full ({maxAttendees} attending).",
                    )
                    return

            await eventService.setScheduledEventRsvp(int(eventId), int(interaction.user.id), str(response))
            # If a reminder was marked as sent with no thread and the event hasn't started yet,
            # clear it so the 20-minute reminder can still fire once attendees exist.
            reminderSentAt = str(event.get("reminderSentAt") or "").strip()
            reminderThreadId = int(event.get("reminderThreadId") or 0)
            if reminderSentAt and reminderThreadId <= 0:
                eventAtRaw = str(event.get("eventAtUtc") or "").strip()
                try:
                    eventAtUtc = datetime.fromisoformat(eventAtRaw)
                except ValueError:
                    eventAtUtc = None
                if eventAtUtc is not None:
                    if eventAtUtc.tzinfo is None:
                        eventAtUtc = eventAtUtc.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < eventAtUtc:
                        await eventService.clearScheduledEventReminder(int(eventId))
            if isinstance(interaction.message, discord.Message):
                await self._refreshEventMessage(int(eventId), message=interaction.message)
            else:
                await self._refreshEventMessage(int(eventId))

        pretty = "attending" if str(response).upper() == "ATTENDING" else "may attend"
        await self._safeEphemeral(interaction, f"Updated RSVP: {pretty}.")

    async def openEditEvent(self, interaction: discord.Interaction, eventId: int) -> None:
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            await self._safeEphemeral(interaction, "This event no longer exists.")
            return
        if not await self._isCreator(interaction, event):
            await self._safeEphemeral(interaction, "Only the event creator can edit this event.")
            return
        modal = EventEditModal(self, int(eventId), event)
        await interactionRuntime.safeInteractionSendModal(interaction, modal)

    async def handleEditEventModal(
        self,
        interaction: discord.Interaction,
        eventId: int,
        modal: EventEditModal,
    ) -> None:
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            await self._safeEphemeral(interaction, "This event no longer exists.")
            return
        if not await self._isCreator(interaction, event):
            await self._safeEphemeral(interaction, "Only the event creator can edit this event.")
            return

        try:
            eventAtUtc, timezoneName, capacity, lockRsvpAtStart, pingRoleIds = self._parseScheduleAndOptions(
                dateText=str(modal.dateInput.value),
                timeAndTimezoneText=str(modal.timeAndTimezoneInput.value),
                optionsText=str(modal.optionsInput.value or ""),
                guild=interaction.guild if interaction.guild is not None else None,
            )
        except ValueError as exc:
            await self._safeEphemeral(interaction, str(exc))
            return

        duplicate = await eventService.findDuplicateActiveEvent(
            guildId=int(event.get("guildId") or 0),
            creatorId=int(event.get("creatorId") or 0),
            title=str(modal.titleInput.value or "").strip(),
            eventAtUtcIso=eventAtUtc.isoformat(),
            excludeEventId=int(eventId),
        )
        if duplicate:
            await self._safeEphemeral(
                interaction,
                f"Another active event already has this title and start time (Event ID: {int(duplicate.get('eventId') or 0)}).",
            )
            return

        await eventService.updateScheduledEventDetails(
            int(eventId),
            title=str(modal.titleInput.value or "").strip(),
            subtitle=str(modal.subtitleInput.value or "").strip() or None,
            eventAtUtcIso=eventAtUtc.isoformat(),
            timezoneName=timezoneName,
            maxAttendees=capacity,
            lockRsvpAtStart=lockRsvpAtStart,
            pingRoleIds=pingRoleIds,
        )
        if isinstance(interaction.message, discord.Message):
            await self._refreshEventMessage(int(eventId), message=interaction.message)
        else:
            await self._refreshEventMessage(int(eventId))
        await self._safeEphemeral(interaction, "Event updated.")

    async def deleteEvent(self, interaction: discord.Interaction, eventId: int) -> None:
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        event = await eventService.getScheduledEvent(int(eventId))
        if not event:
            await self._safeEphemeral(interaction, "This event no longer exists.")
            return
        if not await self._isCreator(interaction, event):
            await self._safeEphemeral(interaction, "Only the event creator can delete this event.")
            return

        await eventService.markScheduledEventDeleted(int(eventId))
        if isinstance(interaction.message, discord.Message):
            try:
                await interaction.message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                await self._refreshEventMessage(int(eventId), message=interaction.message)
        await self._safeEphemeral(interaction, "Event deleted.")


async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))
