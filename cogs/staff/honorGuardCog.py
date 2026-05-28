from __future__ import annotations

import asyncio
import re

from datetime import date
from typing import Optional, Sequence

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

import config
from cogs.staff.honorGuardViews import HonorGuardEventFinishModal, HonorGuardEventView, HonorGuardPointAwardReviewView, HonorGuardSoloSentryReviewView
from features.staff.clockins.engine import ClockinEngine
from features.staff.clockins.honorGuardAdapter import HonorGuardAdapter
from features.staff.honorGuard import buildScaffoldStatus
from features.staff.honorGuard import rendering as honorGuardRendering
from features.staff.honorGuard import service as honorGuardService
from runtime import cogGuards as runtimeCogGuards
from runtime import interaction as interactionRuntime
from runtime import normalization
from runtime import permissions as runtimePermissions

PLUGIN_MANIFEST = {
    "displayName": "Honor Guard",
    "category": "staff",
    "description": "Honor Guard ORBAT integration status and review-flow backend.",
}

_userIdRegex = re.compile(r"\d{15,22}")

def _displayChannel(channelId: int) -> str:
    return f"<#{channelId}>" if int(channelId or 0) > 0 else "`not set`"


def _displayText(value: str) -> str:
    text = str(value or "").strip()
    return f"`{text}`" if text else "`not set`"


def _isImageAttachment(attachment: discord.Attachment) -> bool:
    contentType = (attachment.content_type or "").lower()
    if contentType.startswith("image/"):
        return True
    filename = (attachment.filename or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _evidenceLinks(attachments: Sequence[discord.Attachment]) -> list[str]:
    return [attachment.url for attachment in attachments if _isImageAttachment(attachment)]


def _reviewerMention() -> str:
    roleId = int(
        getattr(
            config,
            "honorGuardReviewerPingRoleId",
            getattr(config, "honorGuardReviewerRoleId", 0),
        )
        or 0
    )
    if roleId > 0:
        return f"<@&{roleId}>"
    return ""


def _hasRole(member: discord.Member, roleId: Optional[int]) -> bool:
    return runtimePermissions.hasAnyRole(member, [roleId])


def _normalizeRoleIdList(rawValues: object) -> set[int]:
    return normalization.normalizeIntSet(rawValues)

def _toPositiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)

def _parseUserIdList(rawText: str) -> list[int]:
    out: list[int] = []
    for match in _userIdRegex.findall(str(rawText or "")):
        parsed = _toPositiveInt(match)
        if parsed <= 0 or parsed in out:
            continue
        out.append(parsed)
    return out


class HonorGuardCog(runtimeCogGuards.InteractionGuardMixin, commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._eventLocks: dict[int, asyncio.Lock] = {}
        self._clockInAdapter = HonorGuardAdapter()
        self._clockInEngine = ClockinEngine(bot, self._clockInAdapter)
        
    async def cog_load(self) -> None:
        await self._restoreReviewViews()

    async def _restoreReviewViews(self) -> None:
        pointAwardRows = await honorGuardService.listPointAwardPendingStatuses()
        for row in pointAwardRows:
            messageId = int(row.get("messageId") or 0)
            if messageId <= 0:
                continue
            self.bot.add_view(
                HonorGuardPointAwardReviewView(self, int(row["submissionId"])),
                message_id=messageId,
            )
        soloSentryRows = await honorGuardService.listSoloSentryPendingStatuses()
        for row in soloSentryRows:
            messageId = int(row.get("messageId") or 0)
            if messageId <= 0:
                continue
            self.bot.add_view(
                HonorGuardSoloSentryReviewView(self, int(row["submissionId"])),
                message_id=messageId,
            )

    def _isInHonorGuard(self, member: discord.Member) -> bool:
        honorGuardRoleIds = int(getattr(config, "honorGuardRoleId", 0) or 0)
        if not honorGuardRoleIds:
            return False
        return _hasRole(member, honorGuardRoleIds)

    def _canAwardPoints(self, member: discord.Member, awarded_user: discord.Member) -> bool:
        if member.id == awarded_user.id:
            return True
        honorGuardReviewerRoleId = int(getattr(config, "honorGuardReviewerRoleId", 0) or 0)
        if honorGuardReviewerRoleId <= 0:
            return True
        return _hasRole(member, honorGuardReviewerRoleId)

    def _canCreateClockIn(self, member: discord.Member, event_type: str) -> bool:
        sgmRole = int(getattr(config, "honorGuardSeniorGuardsmanRoleId", 0) or 0)
        psRole = int(getattr(config, "honorGuardPlatoonSergeantRoleId", 0) or 0)
        poPlusRoles = getattr(config, "honorGuardParadeOfficerPlusRoleIds", []) or []
        ccRole = int(getattr(config, "honorGuardReviewerRoleId", 0) or 0)
        allowed = False
        needsCC = False
        if event_type == "orientation":
            requiredRoleIds = [sgmRole, psRole, *poPlusRoles]
        elif event_type == "sentry":
            requiredRoleIds = [*poPlusRoles]
        elif event_type == "drill":
            requiredRoleIds = [psRole, *poPlusRoles]
        elif event_type == "jge":
            requiredRoleIds = [psRole, *poPlusRoles]
            needsCC = True
        elif event_type == "nco":
            requiredRoleIds = [*poPlusRoles]
            needsCC = True
        else:
            requiredRoleIds = [*poPlusRoles]
        for roleId in requiredRoleIds:
            if roleId != 0 and _hasRole(member, roleId):
                allowed = True
                break
        if needsCC and not _hasRole(member, ccRole):
            allowed = False
        return allowed

    def _honorGuardCommandGuildIds(self) -> set[int]:
        configuredGuildIds = _normalizeRoleIdList(getattr(config, "honorGuardCommandGuildIds", []))
        if configuredGuildIds:
            return configuredGuildIds
        fallbackGuildIds = [
            getattr(config, "serverId", 0),
            getattr(config, "serverIdTesting", 0),
            *(getattr(config, "testGuildIds", []) or []),
        ]
        return _normalizeRoleIdList(fallbackGuildIds)

    async def _ensureHonorGuardCommandGuild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await self._safeReply(
                interaction,
                "This command can only be used in a server channel.",
            )
            return False
        allowedGuildIds = self._honorGuardCommandGuildIds()
        if not allowedGuildIds or int(interaction.guild.id) in allowedGuildIds:
            return True
        await self._safeReply(
            interaction,
            "Honor Guard commands can only be used in the CE server or configured test servers.",
        )
        return False

    @staticmethod
    def _memberDisplayName(member: discord.Member) -> str:
        return str(
            getattr(member, "display_name", None)
            or getattr(member, "global_name", None)
            or getattr(member, "name", None)
            or member.id
        ).strip()

    async def _resolveReviewChannel(
        self,
        guild: discord.Guild,
        fallback: Optional[discord.abc.Messageable],
        *,
        channelId: Optional[int] = None,
    ) -> Optional[discord.abc.Messageable]:
        targetChannelId = int(channelId or 0)
        if targetChannelId > 0:
            channel = self.bot.get_channel(targetChannelId)
            if channel is None:
                channel = guild.get_channel(targetChannelId)
            if channel is None:
                channel = await interactionRuntime.safeFetchChannel(self.bot, targetChannelId)
            if channel is not None:
                return channel
        return fallback

    @app_commands.command(
        name="honor-guard-status",
        description="Show the current Honor Guard ORBAT integration wiring.",
    )
    async def honorGuardStatus(self, interaction: discord.Interaction) -> None:
        member = await self._requireAdminOrManageGuild(interaction)
        if member is None:
            return
        status = buildScaffoldStatus(configModule=config)
        summary = [
            f"Enabled: `{status.config.enabled}`",
            f"Review channel: {_displayChannel(status.config.reviewChannelId)}",
            f"Log channel: {_displayChannel(status.config.logChannelId)}",
            f"Archive channel: {_displayChannel(status.config.archiveChannelId)}",
            f"Spreadsheet: {_displayText(status.config.spreadsheetId)}",
            f"Member sheet: {_displayText(status.config.memberSheetName)}",
            f"Archive sheet: {_displayText(status.config.archiveSheetName)}",
            f"Event hosts sheet: {_displayText(status.config.eventHostsSheetName)}",
        ]
        embed = discord.Embed(
            title="Honor Guard Integration",
            description="Backend tables, point rules, and sheet adapter are wired. Review commands are still separate.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Current Wiring", value="\n".join(summary), inline=False)
        embed.add_field(
            name="DB Tables",
            value="\n".join(f"`{name}`" for name in status.plannedDbTables),
            inline=False,
        )
        if status.sheetProblems:
            embed.add_field(
                name="Sheet Adapter Warnings",
                value="\n".join(f"- {problem}" for problem in status.sheetProblems),
                inline=False,
            )
        embed.add_field(
            name="Next Milestones",
            value="\n".join(status.nextMilestones),
            inline=False,
        )
        await self._safeReply(interaction, embed=embed)

    @app_commands.command(
        name="honorguard-award-points",
        description="Award points to a member of the Honor Guard.",
    )
    @app_commands.describe(
        awarded_user="User you want to award",
        awarded_points="Amount of points you want to award",
        reason="The reason for the award",
    )
    @app_commands.rename(awarded_user="awarded-user")
    @app_commands.rename(awarded_points="awarded-points")
    async def honorGuardAwardPoints(
        self,
        interaction: discord.Interaction,
        awarded_user: discord.Member,
        reason: str,
        awarded_points: float,
    ) -> None:
        if not await self._ensureHonorGuardCommandGuild(interaction):
            return
        if not interaction.channel or not isinstance(interaction.user, discord.Member):
            await self._safeReply(
                interaction,
                "This command can only be used in a server channel.",
            )
            return
        
        if not self._isInHonorGuard(interaction.user):
            await interaction.response.send_message(
                "Only members of the Honor Guard can use this command.",
                ephemeral=True,
            )
            return


        if not self._canAwardPoints(interaction.user, awarded_user):
            await self._safeReply(
                interaction,
                "You do not have permission to award Honor Guard points.",
            )
            return
        if float(awarded_points or 0) <= 0 :
            await self._safeReply(
                interaction,
                "Honor Guard point awards cannot be zero or negative.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        submissionId = await honorGuardService.createPointAwardSubmission(
            guildId=int(interaction.guild.id),
            channelId=int(interaction.channel.id),
            submitterId=int(interaction.user.id),
            awardedUserId=int(awarded_user.id),
            awardedPoints=float(awarded_points or 0),
            reason=str(reason or "").strip(),
            awardedUserDisplayName=self._memberDisplayName(awarded_user),
        )
        submission = await honorGuardService.getPointAwardSubmission(submissionId)
        if not submission:
            await interaction.followup.send(
                "Failed to create point award submission.",
                ephemeral=True,
            )
            return

        embed = honorGuardRendering.buildPointAwardEmbed(submission)
        view = HonorGuardPointAwardReviewView(self, submissionId)
        reviewMessage = await self._postHonorGuardForReview(
            guild=interaction.guild,
            fallbackChannel=interaction.channel,
            embed=embed,
            view=view,
            reviewChannelId=int(getattr(config, "honorGuardReviewChannelId", 0) or 0),
        )
        if not reviewMessage:
            await interaction.followup.send(
                "Submission saved, but I could not post it for review.",
                ephemeral=True,
            )
            return

        await honorGuardService.setPointAwardMessageId(submissionId, reviewMessage.id)
        await interaction.followup.send(
            "Submitted point award log.",
            ephemeral=True,
        )

    @app_commands.command(
        name="honorguard-solo-sentry",
        description="Submit a solo sentry log for Honor Guard review.",
    )
    @app_commands.describe(
        duty_date="Duty date in YYYY-MM-DD format.",
        image="Primary sentry screenshot.",
        extra_image="Second sentry screenshot.",
    )
    @app_commands.rename(duty_date="duty-date")
    @app_commands.rename(extra_image="extra-image")
    async def honorGuardSoloSentry(
        self,
        interaction: discord.Interaction,
        duty_date: str,
        image: discord.Attachment,
        extra_image: discord.Attachment,
    ) -> None:
        if not await self._ensureHonorGuardCommandGuild(interaction):
            return
        if not interaction.channel or not isinstance(interaction.user, discord.Member):
            await self._safeReply(
                interaction,
                "This command can only be used in a server channel.",
            )
            return

        if not self._isInHonorGuard(interaction.user):
            await interaction.response.send_message(
                "Only members of the Honor Guard can use this command.",
                ephemeral=True,
            )
            return

        try:
            normalizedDutyDate = date.fromisoformat(str(duty_date or "").strip()).isoformat()
        except ValueError:
            await self._safeReply(
                interaction,
                "Duty date must use the `YYYY-MM-DD` format.",
            )
            return

        attachments = [image, extra_image]
        imageUrls = _evidenceLinks(attachments)
        if len(imageUrls) < 2:
            await self._safeReply(
                interaction,
                "Two valid image attachments are required for solo sentry logs.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        imageFiles: list[discord.File] = []
        for attachment in attachments:
            if not _isImageAttachment(attachment):
                continue
            try:
                imageFiles.append(await attachment.to_file())
            except (discord.HTTPException, OSError):
                continue
        if len(imageFiles) < 2:
            await interaction.followup.send(
                "I could not copy both sentry screenshots for review. Please try again.",
                ephemeral=True,
            )
            return

        try:
            submissionId = await honorGuardService.createSoloSentrySubmission(
                guildId=int(interaction.guild.id),
                channelId=int(interaction.channel.id),
                submitterId=int(interaction.user.id),
                targetUserId=int(interaction.user.id),
                targetDisplayName=self._memberDisplayName(interaction.user),
                dutyDate=normalizedDutyDate,
                imageUrls=imageUrls,
            )
        except ValueError as exc:
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
            return

        submission = await honorGuardService.getSoloSentrySubmission(submissionId)
        if not submission:
            await interaction.followup.send(
                "Failed to create solo sentry submission.",
                ephemeral=True,
            )
            return

        embed = honorGuardRendering.buildSoloSentrySubmissionEmbed(submission)
        view = HonorGuardSoloSentryReviewView(self, submissionId)
        reviewMessage = await self._postHonorGuardForReview(
            guild=interaction.guild,
            fallbackChannel=interaction.channel,
            embed=embed,
            view=view,
            files=imageFiles,
            reviewChannelId=int(getattr(config, "honorGuardReviewChannelId", 0) or 0),
        )
        if not reviewMessage:
            await interaction.followup.send(
                "Submission saved, but I could not post it for review.",
                ephemeral=True,
            )
            return

        await honorGuardService.setSubmissionMessageId(submissionId, reviewMessage.id)
        await interaction.followup.send(
            "Submitted solo sentry log.",
            ephemeral=True,
        )

    @app_commands.command(
        name="honorguard-event-log",
        description="Create a clock-in for an Honor Guard event.",
    )
    @app_commands.choices(
        event_type=[
            Choice(name="Training", value="drill"),
            Choice(name="Orientation", value="orientation"),
            Choice(name="Sentry", value="sentry"),
            Choice(name="Inspection", value="inspection"),
            Choice(name="Game Night", value="gamenight"),
            Choice(name="Junior Guardsman Exam", value="jge"),
            Choice(name="Non-Commissioned Officer Exam", value="nco_exam"),
        ]
    )
    @app_commands.rename(event_type="event-type")
    @app_commands.rename(event_description="event-description")
    async def honorGuardEventLog(
        self,
        interaction: discord.Interaction,
        event_type : Choice[str],
        event_description: str,
        host: Optional[discord.Member] = None,
        supervisors: Optional[str] = None,
        cohosts: Optional[str] = None,
    ) -> None:
        if not await self._ensureHonorGuardCommandGuild(interaction):
            return
        if not interaction.channel or not isinstance(interaction.user, discord.Member):
            await self._safeReply(
                interaction,
                "This command can only be used in a server channel.",
            )
            return
        if not self._canCreateClockIn(interaction.user, event_type.value):
            await self._safeReply(
                interaction,
                "You do not have permission to create this Honor Guard clock-in.",
            )
            return
        await self._safeReply(
            interaction,
            "Honor Guard event logging is not wired yet.",
        )
        if not host:
            host = interaction.user
        coHostIds = _parseUserIdList(str(cohosts or ""))
        supervisorIds = _parseUserIdList(str(supervisors or ""))
        eventId = await self._clockInEngine.createSession(
            guildId=int(interaction.guild.id),
            channelId=int(interaction.channel.id),
            hostId=int(host.id),
            maxAttendeeLimit=99,
            eventType=event_type.value,
            eventTitle=str(event_description or "").strip(),
            eventDate=date.today().isoformat(),
        )
        officerRoleIds = _normalizeRoleIdList(getattr(config, "honorGuardOfficerRoleIds", []))
        ncoRoleIds = _normalizeRoleIdList(getattr(config, "honorGuardNcoRoleIds", []))
        for supervisorId in supervisorIds:
            user = self.bot.get_user(supervisorId)
            memberGroup = "ENLISTED"
            if runtimePermissions.hasAnyRole(user, officerRoleIds):
                memberGroup = "OFFICER"
            if runtimePermissions.hasAnyRole(user, ncoRoleIds):
                memberGroup = "NCO"
            await self._clockInEngine.addAttendee(eventId, supervisorId, memberGroup=memberGroup, participantRole="SUPERVISOR")
        for coHostId in coHostIds:
            user = self.bot.get_user(coHostId)
            memberGroup = "ENLISTED"
            if runtimePermissions.hasAnyRole(user, officerRoleIds):
                memberGroup = "OFFICER"
            if runtimePermissions.hasAnyRole(user, ncoRoleIds):
                memberGroup = "NCO"
            await self._clockInEngine.addAttendee(eventId, coHostId, memberGroup=memberGroup, participantRole="COHOST")
        event = await self._clockInEngine.getSession(int(eventId))
        if not event:
            await interaction.followup.send(
                "Could not create event clock-in.",
                ephemeral=True,
            )
            return
        embed = self._clockInAdapter.buildEmbed(event, await self._clockInAdapter.listAttendees(eventId))
        view = HonorGuardEventView(self, eventId)
        message = await interactionRuntime.safeChannelSend(interaction.channel, embed=embed, view=view)
        if message is None:
            await interaction.followup.send(
                "Could not create the event clock-in message in this channel.",
                ephemeral=True,
            )
            return
        await self._clockInEngine.setSessionMessageId(int(eventId), int(message.id))
        await interaction.followup.send(
            "Group event clock-in created.",
            ephemeral=True,
        )


    @app_commands.command(
        name="honorguard-schedule-event",
        description="Schedule an event for Honor Guard.",
    )
    async def honorGuardScheduleEvent(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        event_description: str,
    ) -> None:
        _ = member
        _ = event_description
        await self._safeReply(
            interaction,
            "Honor Guard event scheduling is not wired yet.",
        )

    @app_commands.command(
        name="honorguard-quota-cycle",
        description="Cycle the quota for Honor Guard.",
    )
    async def honorGuardQuotaCycle(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        _ = member
        await self._safeReply(
            interaction,
            "Honor Guard quota cycling is not wired yet.",
        )

    async def _canManageEvent(self, interaction: discord.Interaction, event: dict) -> bool:
        if interaction.user.id == int(event.get("hostId") or 0):
            return True
        else:
            attendees = await self._clockInEngine.listAttendees(int(event.get("eventId") or 0))
            for attendee in attendees:
                if attendee.get("participantRole") == "COHOST" and int(attendee.get("userId") or 0) == interaction.user.id:
                    return True
        return False


    async def _updateEventMessage(
        self,
        eventId: int,
        *,
        message: Optional[discord.Message] = None,
    ) -> None:
        await self._clockInEngine.updateClockinMessage(
            int(eventId),
            viewFactory=lambda sessionId: HonorGuardEventView(self, sessionId),
            message=message,
        )

    async def _refreshEventMessageFromInteraction(
        self,
        eventId: int,
        interaction: discord.Interaction,
    ) -> None:
        if isinstance(interaction.message, discord.Message):
            await self._updateEventMessage(eventId, message=interaction.message)
            return
        await self._updateEventMessage(eventId)

    async def _deleteEventClockinMessage(
        self,
        event: dict,
        *,
        message: Optional[discord.Message] = None,
    ) -> None:
        await self._clockInEngine.deleteClockinMessage(
            event,
            message=message,
        )

    async def _collectOneOrTwoImageEvidenceMessage(
        self,
        *,
        channel: discord.abc.Messageable,
        userId: int,
        timeoutSec: float = 180.0,
    ) -> Optional[discord.Message]:
        channelId = getattr(channel, "id", None)
        if channelId is None:
            return None

        def check(message: discord.Message) -> bool:
            # We only accept the submitter's next message in this channel with
            # at least one image attachment.
            if message.author.id != userId:
                return False
            if message.channel.id != channelId:
                return False
            images = [att for att in message.attachments if _isImageAttachment(att)]
            return len(images) in (1, 2)

        try:
            message = await self.bot.wait_for("message", check=check, timeout=timeoutSec)
        except asyncio.TimeoutError:
            return None
        return message

    async def _resolveConfiguredMessageChannel(
        self,
        guild: discord.Guild,
        channelId: int,
    ) -> Optional[discord.abc.Messageable]:
        if int(channelId or 0) <= 0:
            return None
        channel = self.bot.get_channel(int(channelId))
        if channel is None:
            channel = guild.get_channel(int(channelId))
        if channel is None:
            channel = await interactionRuntime.safeFetchChannel(self.bot, int(channelId))
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None


    async def handleEventJoin(self, interaction: discord.Interaction, eventId: int) -> None:
        if interaction.user.bot:
            await interaction.response.send_message("Bots cannot join patrols.", ephemeral=True)
            return
        event = await self._clockInEngine.getSession(int(eventId))
        if not event:
            await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
            return
        if str(event.get("status") or "").upper() != "OPEN":
            await interaction.response.send_message("This event is no longer open.", ephemeral=True)
            return
        if interaction.user.id == int(event.get("hostId") or 0):
            await interaction.response.send_message(
                "You are the host of this event and cannot clock in as an attendee.",
                ephemeral=True,
            )
        if not self._isInHonorGuard(interaction.user):
            await interaction.response.send_message(
                "Only members of the Honor Guard can join this event.",
                ephemeral=True,
            )
            return

        officerRoleIds = _normalizeRoleIdList(getattr(config, "honorGuardOfficerRoleIds", []))
        ncoRoleIds = _normalizeRoleIdList(getattr(config, "honorGuardNcoRoleIds", []))
        memberGroup = "ENLISTED"
        if runtimePermissions.hasAnyRole(interaction.user, officerRoleIds):
            memberGroup = "OFFICER"
        if runtimePermissions.hasAnyRole(interaction.user, ncoRoleIds):
            memberGroup = "NCO"
        
        await self._clockInEngine.addAttendee(int(eventId), int(interaction.user.id), memberGroup=memberGroup)
        await interaction.response.send_message("You have been added to this event.", ephemeral=True)
        await self._refreshEventMessageFromInteraction(eventId, interaction)

    async def handleEventDelete(self, interaction: discord.Interaction, eventId: int) -> None:
        event = await self._clockInEngine.getSession(int(eventId))
        if not event:
            await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
            return
        if not await self._canManageEvent(interaction, event):
            await interaction.response.send_message("Only the event host and supervisors can delete this event.", ephemeral=True)
            return
        await self._clockInEngine.updateSessionStatus(int(eventId), "CANCELED")
        await interaction.response.send_message("Event deleted.", ephemeral=True)
        await self._refreshEventMessageFromInteraction(eventId, interaction)

    async def openEventManage(self, interaction: discord.Interaction, eventId: int) -> None:
        event = await self._clockInEngine.getSession(int(eventId))
        if not event:
            await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
            return
        if not await self._canManageEvent(interaction, event):
            await interaction.response.send_message(
                "Only the event host and supervisors can manage attendees.",
                ephemeral=True,
            )
            return
        if str(event.get("status") or "").upper() != "OPEN":
            await interaction.response.send_message("This event is not open.", ephemeral=True)
            return
        attendees = await self._clockInEngine.listAttendees(int(eventId))
        if not attendees:
            await interaction.response.send_message("No attendees to manage.", ephemeral=True)
            return

        enbed = buildEventManageEmbed(event, attendees, durationMinutes) 
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=enbed,
            view=EventManageView,
            ephemeral=True,
        )
        
    async def openEventFinish(self, interaction: discord.Interaction, eventId: int) -> None:
        event = await self._clockInEngine.getSession(int(eventId))
        if not event:
            await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
            return
        if not await self._canManageEvent(interaction, event):
            await interaction.response.send_message(
                "Only the event host and supervisors can finish this event.",
                ephemeral=True,
            )
            return
        if str(event.get("status") or "").upper() != "OPEN":
            await interaction.response.send_message("This event is not open.", ephemeral=True)
            return

        if str(event.get("eventType") or "").lower() in ("jge", "nco_exam"):
            await interaction.response.send_message(
                "Not implemented yet",
                ephemeral=True,
            )
            return
        else:
            await interactionRuntime.safeInteractionSendModal(
                interaction,
                HonorGuardEventFinishModal(self, eventId),
            )

    async def handleEventFinish(
        self,
        interaction: discord.Interaction,
        eventId: int,
        durationMinutes: int,
    ) -> None:
        durationError = self._validatePatrolDuration(durationMinutes)
        if durationError:
            await interaction.response.send_message(durationError, ephemeral=True)
            return

        lock = self._eventLocks.setdefault(eventId, asyncio.Lock())
        async with lock:
            # Guard against double-finalize clicks while one reviewer is still
            # uploading evidence / posting the review message.
            event = await self._clockInEngine.getSession(int(eventId))
            if not event:
                await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
                return
            if not await self._canManageEvent(interaction, event):
                await interaction.response.send_message(
                    "Only the event host and supervisors can submit this event.",
                    ephemeral=True,
                )
                return
            if str(event.get("status") or "").upper() != "OPEN":
                await interaction.response.send_message("This event is no longer open.", ephemeral=True)
                return
            ## Save Event Duration
            attendees = await self._clockInEngine.listAttendees(int(eventId))
            for attendee in attendees:
                points = honorGuardService.calculatePointDeltas(configModule=config, memberGroup=attendee.get("memberGroup"), eventType=event.get("eventType"), participantRole=attendee.get("participantRole"), attendeeCount=len(attendees), durationMinutes=durationMinutes)
                honorGuardService.updateAttendeePoints(int(attendee.get("attendanceId") or 0), points)
            ## Disable Clock-in Buttons and update message to prevent new attendees and signal pending submission   

            enbed = buildEventFinishEmbed(event, attendees, durationMinutes) 
            await interactionRuntime.safeInteractionReply(
                interaction,
                embed=enbed,
                view=EventFinishView,
                ephemeral=True,
            )

    async def handleEventSubmit(
        self,
        interaction: discord.Interaction,
        eventId: int,
    ) -> None:
        lock = self._eventLocks.setdefault(eventId, asyncio.Lock())
        async with lock:
            # Guard against double-finalize clicks while one reviewer is still
            # uploading evidence / posting the review message.
            event = await self._clockInEngine.getSession(int(eventId))
            if not event:
                await interaction.response.send_message("This event session no longer exists.", ephemeral=True)
                return
            if not await self._canManageEvent(interaction, event):
                await interaction.response.send_message(
                    "Only the event host and supervisors can submit this event.",
                    ephemeral=True,
                )
                return
            if str(event.get("status") or "").upper() != "FINISHED":
                await interaction.response.send_message("This event is not finished.", ephemeral=True)
                return

            channel = interaction.channel
            if channel is None:
                await interaction.response.send_message(
                    "Could not resolve the channel for screenshot upload.",
                    ephemeral=True,
                )
                return
            evidenceChannelId = int(
                getattr(
                    config,
                    "honorGuardEventEvidenceChannelId",
                    getattr(config, "honorGuardChannelId", 0),
                )
                or 0
            )
            evidenceChannel = await self._resolveConfiguredMessageChannel(
                interaction.guild,
                evidenceChannelId,
            )
            if evidenceChannel is None:
                evidenceChannel = interaction.channel

            await interaction.response.send_message(
                f"Upload one or two event screenshots in <#{evidenceChannelId}> within 3 minutes.",
                ephemeral=True,
            )
            # We reuse the evidence collector so solo/group flows behave the same.
            evidenceMessage = await self._collectOneOrTwoImageEvidenceMessage(
                channel=evidenceChannel,
                userId=interaction.user.id,
            )
            if evidenceMessage is None:
                await interaction.followup.send(
                    "Timed out waiting for one or two image screenshots. Event is still open.",
                    ephemeral=True,
                )
                return

            imageUrls = _evidenceLinks(evidenceMessage.attachments)

            submissionId = await honorGuardService.createEventSubmission(
                eventRecordId=int(eventId),
                event=event,
                submitterId=int(interaction.user.id),
                imageUrls=imageUrls,
                evidenceMessageUrl=evidenceMessage.jump_url
            )
            submission = await honorGuardService.getEventSubmission(submissionId)
            if not submission:
                await interaction.followup.send(
                    "Failed to create event submission.",
                    ephemeral=True,
                )
                return

            embed = honorGuardRendering.buildEventReviewEmbed(submission)
            reviewView = HonorGuardEventReviewView(self, submissionId)
            reviewMessage = await self._postHonorGuardForReview(
                guild=interaction.guild,
                fallbackChannel=interaction.channel,
                embed=embed,
                view=reviewView,
                reviewChannelId=int(getattr(config, "honorGuardEventReviewChannelId", 0) or 0),
            )
            if not reviewMessage:
                await interaction.followup.send(
                    "Could not post this submission for review.",
                    ephemeral=True,
                )
                return

            await honorGuardService.setSubmissionMessageId(
                submissionId,
                reviewMessage.id,
                getattr(reviewMessage.channel, "id", None),
            )
            await self._clockInEngine.updateSessionStatus(int(eventId), "SUBMITTED")
            if isinstance(interaction.message, discord.Message):
                await self._deleteEventClockinMessage(event, message=interaction.message)
            else:
                await self._deleteEventClockinMessage(event)

            await interaction.followup.send(
                "Event submitted for review.",
                ephemeral=True,
            )

    async def _postHonorGuardForReview(
        self,
        *,
        guild: discord.Guild,
        fallbackChannel: Optional[discord.abc.Messageable],
        embed: discord.Embed,
        view: discord.ui.View,
        files: Optional[list[discord.File]] = None,
        reviewChannelId: Optional[int] = None,
    ) -> Optional[discord.Message]:
        channel = await self._resolveReviewChannel(
            guild,
            fallbackChannel,
            channelId=reviewChannelId,
        )
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        mention = _reviewerMention()
        content = mention or None
        allowedMentions = discord.AllowedMentions(roles=True, users=True)
        return await interactionRuntime.safeChannelSend(
            channel,
            content=content,
            embed=embed,
            view=view,
            files=files or [],
            allowed_mentions=allowedMentions,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HonorGuardCog(bot))
