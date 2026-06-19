from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.staff.bgItemReview import workflow as itemReviewWorkflow
from features.staff.bgIntelligence import rendering, scoring, service
from features.staff.sessions import bgSpreadsheetQueue
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions

log = logging.getLogger(__name__)

ProgressUpdater = Callable[[str], Awaitable[bool]]


BG_INTEL_BASE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("scan", "Detection Summary"),
    ("sources", "Source Checks"),
    ("profile", "Profile Information"),
    ("connections", "Connections"),
    ("groups", "Groups"),
    ("inventory", "Inventory"),
    ("gamepasses", "Gamepasses"),
    ("games", "Favorites"),
    ("outfits", "Outfits"),
    ("badges", "Badges"),
    ("external", "Safety Records"),
    ("history", "Jane History"),
)
BG_INTEL_DEBUG_SECTION = ("debug", "Debug Timings")


def _bgIntelSections(*, includeDebug: bool = False) -> tuple[tuple[str, str], ...]:
    if not includeDebug:
        return BG_INTEL_BASE_SECTIONS
    return BG_INTEL_BASE_SECTIONS + (BG_INTEL_DEBUG_SECTION,)


def _bgIntelProgressMinIntervalSec() -> float:
    try:
        configured = float(getattr(config, "bgIntelligenceProgressUiMinIntervalSec", 1.0) or 1.0)
    except (TypeError, ValueError):
        configured = 1.0
    return max(0.0, min(configured, 10.0))


def _bgIntelProgressShouldForceUi(status: str) -> bool:
    cleanStatus = str(status or "").strip()
    if not cleanStatus:
        return False
    forcePrefixes = (
        "Checking Discord membership and main-server lookup...",
        "No Roblox account found; checking external safety records only...",
        "Reviewing inventory and item values",
        "Collecting the full badge timeline...",
        "Checking configured badge records...",
        "Correlating known-member alt evidence...",
        "Saving the audit record...",
        "Saving the rerun audit record...",
        "Rendering the overview...",
        "Posting the overview...",
    )
    return cleanStatus.startswith(forcePrefixes)


def _bgIntelReportChannelId() -> int:
    try:
        return int(getattr(config, "bgIntelligenceReportChannelId", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def _resolveBgIntelReportChannel(
    client: discord.Client,
    *,
    fallbackChannel: object = None,
) -> object:
    channelId = _bgIntelReportChannelId()
    if channelId > 0:
        channel = client.get_channel(channelId)
        if channel is None:
            try:
                channel = await client.fetch_channel(channelId)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
    return fallbackChannel


async def _updateBgIntelSheetLinkSafe(
    *,
    report: object,
    riskScore: scoring.RiskScore,
    reportId: int,
    message: discord.Message | None,
    guildId: int,
) -> bgSpreadsheetQueue.BgIntelSheetUpdateResult:
    messageUrl = str(getattr(message, "jump_url", "") or "").strip()
    try:
        return await bgSpreadsheetQueue.updateLatestBgIntelSheetLink(
            report=report,
            riskScore=riskScore,
            reportId=int(reportId or 0),
            messageUrl=messageUrl,
            guildId=int(guildId or 0),
        )
    except Exception:
        log.exception("BG intelligence sheet update failed.")
        return bgSpreadsheetQueue.BgIntelSheetUpdateResult(reason="Sheet update failed internally.")


@dataclass
class BgIntelDebugTracker:
    enabled: bool = False
    startedAt: float = field(default_factory=perf_counter)
    currentStep: str | None = None
    currentStartedAt: float | None = None
    steps: list[dict[str, object]] = field(default_factory=list)
    progressUiSeconds: float = 0.0

    def _appendStep(self, label: str, seconds: float) -> None:
        if not self.enabled:
            return
        self.steps.append(
            {
                "label": str(label or "Working...").strip() or "Working...",
                "seconds": round(max(0.0, float(seconds or 0.0)), 3),
            }
        )

    def _closeCurrentStep(self) -> None:
        if not self.enabled or not self.currentStep or self.currentStartedAt is None:
            return
        self._appendStep(self.currentStep, perf_counter() - self.currentStartedAt)
        self.currentStep = None
        self.currentStartedAt = None

    def record(self, label: str, seconds: float) -> None:
        self._appendStep(label, seconds)

    async def update(self, updater: ProgressUpdater, status: str) -> bool:
        if not self.enabled:
            return await updater(status)
        self._closeCurrentStep()
        progressStartedAt = perf_counter()
        sent = await updater(status)
        self.progressUiSeconds += max(0.0, perf_counter() - progressStartedAt)
        self.currentStep = str(status or "Working...").strip() or "Working..."
        self.currentStartedAt = perf_counter()
        return sent

    def finish(self) -> dict[str, object] | None:
        if not self.enabled:
            return None
        self._closeCurrentStep()
        totalSeconds = max(0.0, perf_counter() - self.startedAt)
        summary = {
            "totalSeconds": round(totalSeconds, 3),
            "steps": list(self.steps),
        }
        if self.progressUiSeconds > 0:
            summary["uiSeconds"] = round(self.progressUiSeconds, 3)
        return summary


@dataclass
class BgIntelProgressRelay:
    updater: ProgressUpdater
    minIntervalSec: float = field(default_factory=_bgIntelProgressMinIntervalSec)
    lastSentAt: float | None = None
    nowFactory: Callable[[], float] = perf_counter

    async def update(self, status: str) -> bool:
        cleanStatus = str(status or "Working...").strip() or "Working..."
        now = self.nowFactory()
        shouldForce = _bgIntelProgressShouldForceUi(cleanStatus)
        if not shouldForce and self.lastSentAt is not None and (now - self.lastSentAt) < self.minIntervalSec:
            return False
        sent = await self.updater(cleanStatus)
        if sent:
            self.lastSentAt = self.nowFactory()
        return sent


class BgIntelSectionSelect(discord.ui.Select):
    def __init__(self, selectedSection: str = "overview", *, includeDebug: bool = False) -> None:
        super().__init__(
            placeholder="Expand a BG intelligence section",
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(label=label, value=section, default=section == selectedSection)
                for section, label in _bgIntelSections(includeDebug=includeDebug)
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BgIntelDetailsView):
            return
        section = str(self.values[0] if self.values else "overview")
        await view.showSection(interaction, section)


class BgIntelDmInventoryButton(discord.ui.Button):
    def __init__(self, *, enabled: bool) -> None:
        super().__init__(
            label="DM Inventory Request",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BgIntelDetailsView):
            await view.sendInventoryNotice(interaction)


class BgIntelDisputeFlagButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Dispute Flag",
            style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BgIntelDetailsView):
            await view.requestDisputeFlag(interaction)


class BgIntelDisputeSelect(discord.ui.Select):
    def __init__(self, detailsView: "BgIntelDetailsView", items: list[dict[str, object]]) -> None:
        options: list[discord.SelectOption] = []
        for index, item in enumerate(list(items or [])[:25]):
            itemId = int(item.get("id") or 0)
            itemName = str(item.get("name") or f"Asset {itemId}").strip() or f"Asset {itemId}"
            reason = str(item.get("matchType") or "flagged").replace("_", " ").strip() or "flagged"
            options.append(
                discord.SelectOption(
                    label=itemName[:100],
                    value=str(index),
                    description=f"{reason[:50]} | asset {itemId}"[:100],
                )
            )
        super().__init__(
            placeholder="Choose a flagged item to dispute",
            min_values=1,
            max_values=1,
            row=0,
            options=options,
        )
        self.detailsView = detailsView
        self.items = list(items or [])[:25]

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            selectedIndex = int(self.values[0] if self.values else "0")
        except (TypeError, ValueError):
            selectedIndex = -1
        if selectedIndex < 0 or selectedIndex >= len(self.items):
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="That flagged item could not be resolved.",
                ephemeral=True,
            )
        await self.detailsView.disputeFlag(interaction, self.items[selectedIndex])


class BgIntelDisputeSelectView(discord.ui.View):
    def __init__(self, detailsView: "BgIntelDetailsView", items: list[dict[str, object]]) -> None:
        super().__init__(timeout=120)
        self.ownerId = int(detailsView.ownerId)
        self.add_item(BgIntelDisputeSelect(detailsView, items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == int(self.ownerId):
            return True
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="This BG intelligence panel belongs to the reviewer who ran the scan.",
            ephemeral=True,
        )
        return False


class BgIntelReportButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Full Text Report",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BgIntelDetailsView):
            await view.sendTextReport(interaction)


class BgIntelRerunButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Rerun Scan",
            style=discord.ButtonStyle.primary,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BgIntelDetailsView):
            await view.requestRerun(interaction)


class BgIntelSummaryButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Decision Summary",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, BgIntelDetailsView):
            await view.sendDecisionSummary(interaction)


class BgIntelRobloxUsernameModal(discord.ui.Modal):
    def __init__(self, detailsView: "BgIntelDetailsView") -> None:
        super().__init__(title="Rerun BG Intel")
        self.detailsView = detailsView
        existingUsername = str(getattr(detailsView.report, "robloxUsername", "") or "").strip()
        self.robloxUsername = discord.ui.TextInput(
            label="Roblox username",
            placeholder="Enter the Roblox username to pair with this Discord ID",
            default=existingUsername[:20],
            min_length=3,
            max_length=20,
            required=True,
        )
        self.add_item(self.robloxUsername)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != int(self.detailsView.ownerId):
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="This BG intelligence panel belongs to the reviewer who ran the scan.",
                ephemeral=True,
            )
        await self.detailsView.rerunScan(
            interaction,
            robloxUsernameOverride=str(self.robloxUsername.value or "").strip(),
        )


class BgIntelDetailsView(discord.ui.View):
    def __init__(
        self,
        *,
        ownerId: int,
        report,
        riskScore: scoring.RiskScore,
        reportId: int,
        includeTextReport: bool = False,
        debugMode: bool = False,
        roverGuildId: int | None = None,
        robloxUsernameOverride: str | None = None,
        notifyPrivateInventory: bool = False,
    ) -> None:
        super().__init__(timeout=900)
        self.ownerId = int(ownerId)
        self.report = report
        self.riskScore = riskScore
        self.reportId = int(reportId or 0)
        self.debugMode = bool(debugMode)
        self.includeTextReport = bool(includeTextReport or self.debugMode)
        self.roverGuildId = int(roverGuildId or 0) or None
        self.robloxUsernameOverride = str(robloxUsernameOverride or "").strip() or None
        self.notifyPrivateInventory = bool(notifyPrivateInventory)
        self.currentSection = "overview"
        self._rebuildControls("overview")

    def _availableSections(self) -> tuple[tuple[str, str], ...]:
        return _bgIntelSections(includeDebug=self.debugMode)

    def _robloxProfileUrl(self) -> str | None:
        try:
            robloxUserId = int(getattr(self.report, "robloxUserId", 0) or 0)
        except (TypeError, ValueError):
            return None
        if robloxUserId <= 0:
            return None
        return f"https://www.roblox.com/users/{robloxUserId}/profile"

    def _flaggedInventoryItems(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for raw in list(getattr(self.report, "flaggedItems", None) or []):
            if not isinstance(raw, dict):
                continue
            try:
                assetId = int(raw.get("id") or 0)
            except (TypeError, ValueError):
                assetId = 0
            if assetId <= 0:
                continue
            items.append(dict(raw))
        return items

    def _shouldShowDisputeButton(self, section: str) -> bool:
        normalizedSection = str(section or "overview").strip().lower()
        return normalizedSection == "inventory" and bool(self._flaggedInventoryItems())

    def _addActionButtons(self, section: str) -> None:
        profileUrl = self._robloxProfileUrl()
        if profileUrl:
            self.add_item(
                discord.ui.Button(
                    label="Roblox Profile",
                    style=discord.ButtonStyle.link,
                    url=profileUrl,
                    row=1,
                )
            )
        self.add_item(BgIntelSummaryButton())
        self.add_item(BgIntelReportButton())
        self.add_item(BgIntelRerunButton())
        inventoryPrivate = str(getattr(self.report, "inventoryScanStatus", "") or "").strip().upper() == "PRIVATE"
        hasDiscordTarget = int(getattr(self.report, "discordUserId", 0) or 0) > 0
        self.add_item(BgIntelDmInventoryButton(enabled=inventoryPrivate and hasDiscordTarget))
        if self._shouldShowDisputeButton(section):
            self.add_item(BgIntelDisputeFlagButton())

    def _rebuildControls(self, section: str) -> None:
        self.clear_items()
        includeDebug = any(item[0] == "debug" for item in self._availableSections())
        self.add_item(BgIntelSectionSelect(section, includeDebug=includeDebug))
        self._addActionButtons(section)
        self._syncSelectedSection(section)

    def _badgeGraphFilename(self) -> str:
        reportId = self.reportId if self.reportId > 0 else 0
        robloxUserId = int(getattr(self.report, "robloxUserId", 0) or 0)
        suffix = reportId or robloxUserId or int(self.ownerId)
        return f"bg-intel-badges-{suffix}.png"

    def _reportTextFilename(self) -> str:
        reportId = self.reportId if self.reportId > 0 else 0
        robloxUserId = int(getattr(self.report, "robloxUserId", 0) or 0)
        suffix = reportId or robloxUserId or int(self.ownerId)
        return f"bg-intel-report-{suffix}.txt"

    def _applyBadgeGraph(self, embed: discord.Embed, section: str) -> discord.File | None:
        normalizedSection = str(section or "overview").strip().lower()
        if normalizedSection not in {"overview", "badges"}:
            return None
        return rendering.applyBadgeTimelineGraph(
            embed,
            self.report,
            filename=self._badgeGraphFilename(),
        )

    def _buildPublicPayload(self, section: str) -> tuple[discord.Embed, list[discord.File]]:
        normalizedSection = str(section or "overview").strip().lower()
        embed = rendering.buildPublicSectionEmbed(
            self.report,
            score=self.riskScore,
            section=normalizedSection,
            reportId=self.reportId if self.reportId > 0 else None,
            includeTextReport=self.includeTextReport,
        )
        graphFile = self._applyBadgeGraph(embed, normalizedSection)
        files: list[discord.File] = []
        if graphFile is not None:
            files.append(graphFile)
        if self.includeTextReport:
            files.append(
                rendering.buildReportTextFile(
                    self.report,
                    score=self.riskScore,
                    reportId=self.reportId if self.reportId > 0 else None,
                    filename=self._reportTextFilename(),
                )
            )
        return embed, files

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == int(self.ownerId):
            return True
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="This BG intelligence panel belongs to the reviewer who ran the scan.",
            ephemeral=True,
        )
        return False

    def _syncSelectedSection(self, section: str) -> None:
        normalizedSection = str(section or "overview").strip().lower()
        validSections = {item[0] for item in self._availableSections()}
        if normalizedSection not in validSections:
            normalizedSection = "overview"
        self.currentSection = normalizedSection
        for child in self.children:
            if isinstance(child, BgIntelSectionSelect):
                for option in child.options:
                    option.default = option.value == normalizedSection

    async def _finishEphemeral(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(content=content, embed=None, view=None, attachments=[])
            return
        except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
            pass
        try:
            await interaction.followup.send(
                content=content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
            return

    async def _fetchMemberForAction(
        self,
        interaction: discord.Interaction,
        discordUserId: int,
    ) -> discord.Member | None:
        if discordUserId <= 0:
            return None
        guild = interaction.guild
        if guild is not None:
            member = guild.get_member(int(discordUserId))
            if member is not None:
                return member
            try:
                return await guild.fetch_member(int(discordUserId))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        try:
            mainGuildId = int(getattr(config, "serverId", 0) or 0)
        except (TypeError, ValueError):
            mainGuildId = 0
        if mainGuildId <= 0 or (guild is not None and int(guild.id) == mainGuildId):
            return None
        mainGuild = interaction.client.get_guild(mainGuildId)
        if mainGuild is None:
            try:
                mainGuild = await interaction.client.fetch_guild(mainGuildId)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                return None
        try:
            return mainGuild.get_member(int(discordUserId)) or await mainGuild.fetch_member(int(discordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            return None

    async def sendDecisionSummary(self, interaction: discord.Interaction) -> None:
        summary = rendering.buildDecisionSummary(
            self.report,
            score=self.riskScore,
            reportId=self.reportId if self.reportId > 0 else None,
        )
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"```text\n{summary[:1850]}\n```",
            ephemeral=True,
            allowedMentions=discord.AllowedMentions.none(),
        )

    async def sendTextReport(self, interaction: discord.Interaction) -> None:
        reportFile = rendering.buildReportTextFile(
            self.report,
            score=self.riskScore,
            reportId=self.reportId if self.reportId > 0 else None,
            filename=self._reportTextFilename(),
        )
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="Attached full BG intelligence text report.",
            file=reportFile,
            ephemeral=True,
            allowedMentions=discord.AllowedMentions.none(),
        )

    async def sendInventoryNotice(self, interaction: discord.Interaction) -> None:
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        discordUserId = int(getattr(self.report, "discordUserId", 0) or 0)
        member = await self._fetchMemberForAction(interaction, discordUserId)
        if member is None:
            return await self._finishEphemeral(
                interaction,
                "I couldn't find that Discord member to send the inventory request.",
            )
        sent = await service.sendPrivateInventoryNotice(member, reviewer=interaction.user)
        self.report.privateInventoryDmSent = bool(sent)
        messageObject = getattr(interaction, "message", None)
        if messageObject is not None:
            self._rebuildControls(self.currentSection)
            embed, attachments = self._buildPublicPayload(self.currentSection)
            await interactionRuntime.safeMessageEdit(
                messageObject,
                embed=embed,
                view=self,
                attachments=attachments,
            )
        message = "Inventory request DM sent." if sent else "I couldn't DM that user. They may have DMs closed."
        await self._finishEphemeral(interaction, message)

    def _needsRobloxUsernameForRerun(self) -> bool:
        try:
            discordUserId = int(getattr(self.report, "discordUserId", 0) or 0)
        except (TypeError, ValueError):
            discordUserId = 0
        try:
            robloxUserId = int(getattr(self.report, "robloxUserId", 0) or 0)
        except (TypeError, ValueError):
            robloxUserId = 0
        return discordUserId > 0 and robloxUserId <= 0

    async def requestRerun(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="This scan can only be rerun inside a server.",
                ephemeral=True,
            )
        if self._needsRobloxUsernameForRerun():
            try:
                await interaction.response.send_modal(BgIntelRobloxUsernameModal(self))
                return
            except (discord.NotFound, discord.HTTPException, AttributeError):
                return await interactionRuntime.safeInteractionReply(
                    interaction,
                    content="I couldn't open the Roblox username prompt. Run `/bg-intel` again with the member field and Roblox username.",
                    ephemeral=True,
                )
        await self.rerunScan(interaction)

    async def rerunScan(
        self,
        interaction: discord.Interaction,
        *,
        robloxUsernameOverride: str | None = None,
    ) -> None:
        if interaction.guild is None:
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="This scan can only be rerun inside a server.",
                ephemeral=True,
            )
        cleanRobloxUsernameOverride = str(robloxUsernameOverride or "").strip()
        if robloxUsernameOverride is not None and not cleanRobloxUsernameOverride:
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="Please enter a Roblox username before rerunning the scan.",
                ephemeral=True,
            )
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)

        async def rawProgress(status: str) -> bool:
            cleanStatus = str(status or "Running scan...").strip() or "Running scan..."
            try:
                await interaction.edit_original_response(
                    content=f"Jane is rerunning the background intel scan.\nStatus: {cleanStatus}",
                    embed=None,
                    view=None,
                    attachments=[],
                )
                return True
            except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
                return False

        debugTracker = BgIntelDebugTracker(enabled=self.debugMode)
        progressRelay = BgIntelProgressRelay(rawProgress)

        async def progress(status: str) -> bool:
            return await debugTracker.update(progressRelay.update, status)

        try:
            await progress("Preparing rerun...")
            discordUserId = int(getattr(self.report, "discordUserId", 0) or 0)
            robloxUserId = int(getattr(self.report, "robloxUserId", 0) or 0)
            effectiveRobloxUsernameOverride = cleanRobloxUsernameOverride or self.robloxUsernameOverride
            member = await self._fetchMemberForAction(interaction, discordUserId) if discordUserId > 0 else None
            if member is not None:
                report = await service.buildReport(
                    member,
                    guild=interaction.guild,
                    reviewBucketOverride="adult",
                    roverGuildId=self.roverGuildId,
                    robloxUsernameOverride=effectiveRobloxUsernameOverride,
                    notifyPrivateInventory=self.notifyPrivateInventory,
                    reviewer=interaction.user,
                    configModule=config,
                    progressCallback=progress,
                    debugTimingRecorder=debugTracker.record,
                )
            elif discordUserId > 0:
                report = await service.buildReportForDiscordId(
                    guild=interaction.guild,
                    discordUserId=discordUserId,
                    displayMember=None,
                    roverGuildId=self.roverGuildId,
                    robloxUsernameOverride=effectiveRobloxUsernameOverride,
                    reviewBucketOverride="adult",
                    configModule=config,
                    progressCallback=progress,
                    debugTimingRecorder=debugTracker.record,
                )
            else:
                report = await service.buildReportForRobloxIdentity(
                    guild=interaction.guild,
                    robloxUserId=robloxUserId if robloxUserId > 0 else None,
                    robloxUsername=effectiveRobloxUsernameOverride or getattr(self.report, "robloxUsername", None),
                    reviewBucketOverride="adult",
                    configModule=config,
                    progressCallback=progress,
                    debugTimingRecorder=debugTracker.record,
                )
            await progress("Scoring the rerun...")
            riskScore = scoring.scoreReport(report, configModule=config)
            await progress("Saving the rerun audit record...")
            channelId = int(getattr(getattr(interaction, "channel", None), "id", 0) or 0)
            reportId = await service.recordReport(
                guildId=int(interaction.guild.id),
                channelId=channelId,
                reviewerId=int(interaction.user.id),
                report=report,
                riskScore=riskScore,
            )
            await progress("Rendering the refreshed overview...")
        except Exception:
            log.exception("BG intelligence rerun failed.")
            return await self._finishEphemeral(
                interaction,
                "BG intelligence rerun failed internally. Check Jane's logs before trusting the result.",
            )

        self.report = report
        self.riskScore = riskScore
        self.reportId = int(reportId or 0)
        if cleanRobloxUsernameOverride:
            self.robloxUsernameOverride = cleanRobloxUsernameOverride
        self._rebuildControls("overview")
        embed, attachments = self._buildPublicPayload("overview")
        await progress("Updating the live panel...")
        message = getattr(interaction, "message", None)
        if message is not None:
            await interactionRuntime.safeMessageEdit(
                message,
                embed=embed,
                view=self,
                attachments=attachments,
            )
            timingSummary = debugTracker.finish()
            if timingSummary is not None:
                setattr(self.report, "debugTimingSummary", timingSummary)
                finalEmbed, finalAttachments = self._buildPublicPayload("overview")
                await interactionRuntime.safeMessageEdit(
                    message,
                    embed=finalEmbed,
                    view=self,
                    attachments=finalAttachments,
                )
        else:
            timingSummary = debugTracker.finish()
            if timingSummary is not None:
                setattr(self.report, "debugTimingSummary", timingSummary)
        await progress("Updating the BGC spreadsheet link...")
        sheetUpdate = await _updateBgIntelSheetLinkSafe(
            report=self.report,
            riskScore=self.riskScore,
            reportId=self.reportId,
            message=message if isinstance(message, discord.Message) else None,
            guildId=int(interaction.guild.id),
        )
        finalMessage = "Rerun complete. The BG intelligence panel was refreshed."
        if sheetUpdate.updated:
            finalMessage += (
                f"\nUpdated `Jane Intel` on `{sheetUpdate.spreadsheet_title or 'BGC spreadsheet'}` "
                f"row `{sheetUpdate.row_number}`."
            )
        elif sheetUpdate.reason:
            finalMessage += f"\nSheet link not updated: {sheetUpdate.reason}"
        await self._finishEphemeral(interaction, finalMessage)

    async def requestDisputeFlag(self, interaction: discord.Interaction) -> None:
        flaggedItems = self._flaggedInventoryItems()
        if not flaggedItems:
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="There are no flagged inventory items to dispute on this report.",
                ephemeral=True,
            )
        if len(flaggedItems) == 1:
            return await self.disputeFlag(interaction, flaggedItems[0])
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="Choose the flagged item to re-post for manual review.",
            view=BgIntelDisputeSelectView(self, flaggedItems),
            ephemeral=True,
            allowedMentions=discord.AllowedMentions.none(),
        )

    async def disputeFlag(self, interaction: discord.Interaction, flaggedItem: dict[str, object]) -> None:
        if interaction.guild is None:
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content="This dispute flow can only run inside a server.",
                ephemeral=True,
            )
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        result = await itemReviewWorkflow.queueBgIntelDisputedItem(
            interaction.client,
            guildId=int(interaction.guild.id),
            reviewerId=int(interaction.user.id),
            report=self.report,
            flaggedItem=flaggedItem,
            reportId=self.reportId,
        )
        if not bool(result.get("ok")):
            reason = str(result.get("reason") or "").strip() or "I couldn't queue that item for manual review."
            return await self._finishEphemeral(interaction, reason)
        queueId = int(result.get("queueId") or 0)
        if bool(result.get("created")):
            message = f"Queued flagged item for manual review as queue #{queueId}."
        else:
            message = f"Re-posted flagged item for manual review on queue #{queueId}."
        await self._finishEphemeral(interaction, message)

    async def showSection(self, interaction: discord.Interaction, section: str) -> None:
        normalizedSection = str(section or "overview").strip().lower()
        self._rebuildControls(normalizedSection)
        embed, attachments = self._buildPublicPayload(normalizedSection)
        try:
            await interaction.response.edit_message(embed=embed, view=self, attachments=attachments)
            return
        except (discord.NotFound, discord.HTTPException):
            message = getattr(interaction, "message", None)
            if message is not None:
                fallbackEmbed, fallbackAttachments = self._buildPublicPayload(normalizedSection)
                edited = await interactionRuntime.safeMessageEdit(
                    message,
                    embed=fallbackEmbed,
                    view=self,
                    attachments=fallbackAttachments,
                )
                if edited:
                    await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
                    return
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="I couldn't expand that section on the webhook message.",
            ephemeral=True,
            allowedMentions=discord.AllowedMentions.none(),
        )


class BgIntelligenceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _bgIntelProgressContent(status: str, *, targetLabel: str) -> str:
        cleanStatus = str(status or "Starting scan...").strip() or "Starting scan..."
        cleanTarget = str(targetLabel or "selected user").strip() or "selected user"
        cleanTarget = cleanTarget.replace("`", "'")[:80]
        return (
            "Jane is running the background intel scan.\n"
            f"Target: `{cleanTarget}`\n"
            f"Status: {cleanStatus}\n"
            "Large badge or inventory histories can take a moment."
        )

    async def _editBgIntelStatus(
        self,
        interaction: discord.Interaction,
        status: str,
        *,
        targetLabel: str,
    ) -> bool:
        try:
            await interaction.edit_original_response(
                content=self._bgIntelProgressContent(status, targetLabel=targetLabel),
            )
            return True
        except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
            return False

    async def _finishBgIntelStatus(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> bool:
        try:
            await interaction.edit_original_response(content=message)
            return True
        except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
            return await interactionRuntime.safeInteractionReply(
                interaction,
                content=message,
                ephemeral=True,
                allowedMentions=discord.AllowedMentions.none(),
            )

    async def _sendBgIntelMessage(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: BgIntelDetailsView,
        files: list[discord.File] | None = None,
    ) -> discord.Message | None:
        fallbackChannel = getattr(interaction, "channel", None)
        channel = await _resolveBgIntelReportChannel(
            interaction.client,
            fallbackChannel=fallbackChannel,
        )
        sentMessage = None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            payload = {
                "embed": embed,
                "view": view,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if files:
                payload["files"] = files
            sentMessage = await interactionRuntime.safeChannelSend(channel, **payload)
        if sentMessage is not None:
            await self._finishBgIntelStatus(
                interaction,
                f"Background-check overview posted in <#{int(sentMessage.channel.id)}>.",
            )
            return sentMessage

        await self._finishBgIntelStatus(
            interaction,
            "I couldn't post the background-check overview in this channel.",
        )
        return None

    async def _safeEphemeral(self, interaction: discord.Interaction, message: str) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=message,
            ephemeral=True,
        )

    def _canUse(self, member: discord.Member) -> bool:
        extraReviewerRoleIds: set[int] = set()
        for rawRoleId in list(getattr(config, "bgCheckMinorReviewRoleIds", []) or []):
            try:
                parsedRoleId = int(rawRoleId)
            except (TypeError, ValueError):
                continue
            if parsedRoleId > 0:
                extraReviewerRoleIds.add(parsedRoleId)
        try:
            primaryMinorRoleId = int(getattr(config, "bgCheckMinorReviewRoleId", 0) or 0)
        except (TypeError, ValueError):
            primaryMinorRoleId = 0
        if primaryMinorRoleId > 0:
            extraReviewerRoleIds.add(primaryMinorRoleId)
        return (
            runtimePermissions.hasBgCheckCertifiedRole(member)
            or runtimePermissions.hasAdminOrManageGuild(member)
            or any(int(role.id) in extraReviewerRoleIds for role in list(member.roles or []))
        )

    @staticmethod
    def _parseDiscordId(rawValue: str | None) -> Optional[int]:
        clean = str(rawValue or "").strip()
        if not clean:
            return None
        if clean.startswith("<@") and clean.endswith(">"):
            clean = clean[2:-1].lstrip("!")
        if not clean.isdigit():
            return None
        parsed = int(clean)
        return parsed if parsed > 0 else None

    async def _fetchGuildMemberById(self, guild: discord.Guild, discordUserId: int) -> discord.Member | None:
        member = guild.get_member(int(discordUserId))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(discordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _mainGuildId(self) -> int:
        try:
            mainGuildId = int(getattr(config, "serverId", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return mainGuildId if mainGuildId > 0 else 0

    def _mainGuild(self) -> discord.Guild | None:
        mainGuildId = self._mainGuildId()
        if mainGuildId <= 0:
            return None
        return self.bot.get_guild(mainGuildId)

    async def _fetchMainGuildMemberById(
        self,
        discordUserId: int,
        *,
        currentGuild: discord.Guild,
    ) -> discord.Member | None:
        mainGuildId = self._mainGuildId()
        if mainGuildId <= 0 or int(mainGuildId) == int(currentGuild.id):
            return None
        mainGuild = self._mainGuild()
        if mainGuild is None:
            try:
                mainGuild = await self.bot.fetch_guild(mainGuildId)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return await self._fetchGuildMemberById(mainGuild, int(discordUserId))

    @app_commands.command(
        name="bg-intel",
        description="Run Jane's standalone Roblox background intelligence report.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        member="Discord member mention or user ID. Optional if a Roblox username is supplied.",
        roblox_username="Optional Roblox username. Can be used without a Discord member.",
        debug="Force-attach the text report and include per-step scan timings. Defaults to no.",
    )
    @app_commands.rename(roblox_username="roblox-username")
    async def bgIntel(
        self,
        interaction: discord.Interaction,
        member: str | None = None,
        roblox_username: str | None = None,
        debug: bool = False,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await self._safeEphemeral(interaction, "This command can only be used inside a server.")
        if not self._canUse(interaction.user):
            return await self._safeEphemeral(interaction, "You do not have permission to run BG intelligence scans.")
        cleanRobloxUsername = str(roblox_username or "").strip()
        cleanMember = str(member or "").strip()
        parsedDiscordId = self._parseDiscordId(cleanMember)
        if cleanMember and parsedDiscordId is None:
            return await self._safeEphemeral(interaction, "Please provide a valid Discord member mention or user ID.")
        if parsedDiscordId is None and not cleanRobloxUsername:
            return await self._safeEphemeral(
                interaction,
                "Please provide a Discord member mention, a Discord user ID, or a Roblox username.",
            )

        await interactionRuntime.safeInteractionDefer(
            interaction,
            ephemeral=True,
            thinking=True,
        )

        targetLabel = str(
            cleanMember
            or cleanRobloxUsername
            or parsedDiscordId
            or "selected user"
        )
        debugEnabled = bool(debug)

        async def rawProgressUpdater(status: str) -> bool:
            return await self._editBgIntelStatus(
                interaction,
                status,
                targetLabel=targetLabel,
            )

        debugTracker = BgIntelDebugTracker(enabled=debugEnabled)
        progressRelay = BgIntelProgressRelay(rawProgressUpdater)

        async def progressUpdater(status: str) -> bool:
            return await debugTracker.update(progressRelay.update, status)

        await progressUpdater("Checking Discord membership and main-server lookup...")

        targetMember = None
        if parsedDiscordId is not None:
            targetMember = await self._fetchGuildMemberById(interaction.guild, parsedDiscordId)
        targetDiscordId = int(getattr(targetMember, "id", 0) or parsedDiscordId or 0)
        mainGuildMember = (
            await self._fetchMainGuildMemberById(targetDiscordId, currentGuild=interaction.guild)
            if targetDiscordId > 0
            else None
        )
        scanMember = targetMember or mainGuildMember
        roverMember = mainGuildMember or targetMember
        roverGuildId = int(getattr(getattr(roverMember, "guild", None), "id", 0) or 0) if roverMember is not None else None
        if targetMember is not None and targetMember.bot:
            return await self._finishBgIntelStatus(
                interaction,
                "That is a bot account. Jane is not emotionally prepared to background-check the appliances.",
            )
        if mainGuildMember is not None and mainGuildMember.bot:
            return await self._finishBgIntelStatus(
                interaction,
                "That is a bot account. Jane is not emotionally prepared to background-check the appliances.",
            )

        try:
            if scanMember is not None:
                report = await service.buildReport(
                    scanMember,
                    guild=interaction.guild,
                    reviewBucketOverride="adult",
                    roverGuildId=roverGuildId,
                    robloxUsernameOverride=cleanRobloxUsername or None,
                    notifyPrivateInventory=False,
                    reviewer=interaction.user,
                    configModule=config,
                    progressCallback=progressUpdater,
                    debugTimingRecorder=debugTracker.record,
                )
            elif parsedDiscordId is not None:
                report = await service.buildReportForDiscordId(
                    guild=interaction.guild,
                    discordUserId=parsedDiscordId,
                    displayMember=mainGuildMember,
                    roverGuildId=roverGuildId,
                    robloxUsernameOverride=cleanRobloxUsername or None,
                    reviewBucketOverride="adult",
                    configModule=config,
                    progressCallback=progressUpdater,
                    debugTimingRecorder=debugTracker.record,
                )
            else:
                report = await service.buildReportForRobloxIdentity(
                    guild=interaction.guild,
                    robloxUsername=cleanRobloxUsername or None,
                    reviewBucketOverride="adult",
                    configModule=config,
                    progressCallback=progressUpdater,
                    debugTimingRecorder=debugTracker.record,
                )
            await progressUpdater("Scoring the completed scan...")
            riskScore = scoring.scoreReport(report, configModule=config)
            try:
                await progressUpdater("Saving the audit record...")
                channelId = int(getattr(getattr(interaction, "channel", None), "id", 0) or 0)
                reportId = await service.recordReport(
                    guildId=int(interaction.guild.id),
                    channelId=channelId,
                    reviewerId=int(interaction.user.id),
                    report=report,
                    riskScore=riskScore,
                )
            except Exception:
                reportId = 0
                log.exception(
                    "BG intelligence audit insert failed for guild=%s target=%s.",
                    int(interaction.guild.id),
                    int(report.discordUserId or 0),
                )
        except Exception:
            log.exception(
                "BG intelligence scan failed for guild=%s target=%s.",
                int(interaction.guild.id),
                int(scanMember.id) if scanMember is not None else int(parsedDiscordId or 0),
            )
            return await self._finishBgIntelStatus(
                interaction,
                "BG intelligence scan failed internally. Check Jane's logs before trusting the result.",
            )

        await progressUpdater("Rendering the overview...")
        view = BgIntelDetailsView(
            ownerId=int(interaction.user.id),
            report=report,
            riskScore=riskScore,
            reportId=reportId,
            includeTextReport=debugEnabled,
            debugMode=debugEnabled,
            roverGuildId=roverGuildId,
            robloxUsernameOverride=cleanRobloxUsername or None,
            notifyPrivateInventory=False,
        )
        embed, files = view._buildPublicPayload("overview")
        await progressUpdater("Posting the overview...")
        sentMessage = await self._sendBgIntelMessage(interaction, embed=embed, view=view, files=files)
        timingSummary = debugTracker.finish()
        if timingSummary is not None:
            setattr(report, "debugTimingSummary", timingSummary)
            if sentMessage is not None:
                finalEmbed, finalFiles = view._buildPublicPayload("overview")
                await interactionRuntime.safeMessageEdit(
                    sentMessage,
                    embed=finalEmbed,
                    view=view,
                    attachments=finalFiles,
                )
        await progressUpdater("Updating the BGC spreadsheet link...")
        sheetUpdate = await _updateBgIntelSheetLinkSafe(
            report=report,
            riskScore=riskScore,
            reportId=reportId,
            message=sentMessage,
            guildId=int(interaction.guild.id),
        )
        postedText = (
            f"Background-check overview posted in <#{int(sentMessage.channel.id)}>."
            if sentMessage is not None
            else "I couldn't post the background-check overview in the configured channel."
        )
        if sheetUpdate.updated:
            await self._finishBgIntelStatus(
                interaction,
                (
                    f"{postedText}\n"
                    f"Updated `Jane Intel` on `{sheetUpdate.spreadsheet_title or 'BGC spreadsheet'}` "
                    f"row `{sheetUpdate.row_number}`."
                ),
            )
        else:
            reason = str(sheetUpdate.reason or "").strip()
            suffix = f"\nSheet link not updated: {reason}" if reason else ""
            await self._finishBgIntelStatus(interaction, postedText + suffix)


async def setup(bot: commands.Bot):
    await bot.add_cog(BgIntelligenceCog(bot))
