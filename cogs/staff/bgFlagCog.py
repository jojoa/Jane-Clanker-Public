from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.staff.bgItemReview import service as itemReviewService
from features.staff.bgItemReview import workflow as itemReviewWorkflow
from features.staff.bgflags import service as flagService
from runtime import interaction as interactionRuntime
from runtime import viewBases as runtimeViewBases
from runtime import webhooks as runtimeWebhooks
from features.staff.sessions.Roblox import robloxAssets, robloxBadges, robloxThumbnails

_modsOnlyMessage = "Mods only."

_addableFlagTypeChoices = [
    discord.SelectOption(label="Group", value="group", description="Flag membership in a Roblox group ID."),
    discord.SelectOption(label="Keyword", value="keyword", description="Flag matching text found in groups or items."),
    discord.SelectOption(label="Item", value="item", description="Flag an exact Roblox catalog item ID."),
    discord.SelectOption(label="Creator", value="creator", description="Flag items made by a Roblox creator ID."),
    discord.SelectOption(label="Badge", value="badge", description="Flag a Roblox badge ID."),
    discord.SelectOption(label="Favorite Game", value="game", description="Flag a Roblox favorite game/universe ID."),
    discord.SelectOption(label="Favorite Game Keyword", value="game_keyword", description="Flag matching favorite game text."),
]
_addableFlagTypeValues = {str(choice.value) for choice in _addableFlagTypeChoices}
_addableNumericRuleTypes = {"group", "item", "creator", "badge", "game"}
_severityChoices = [
    discord.SelectOption(label="Light", value="25", description="Low confidence or mild relevance."),
    discord.SelectOption(label="Medium", value="50", description="Normal flag strength.", default=True),
    discord.SelectOption(label="High", value="75", description="Strong flag strength."),
    discord.SelectOption(label="SEVERE", value="100", description="Highest severity."),
]
_severityLabels = {
    0: "default",
    25: "Light",
    50: "Medium",
    75: "High",
    100: "SEVERE",
}
_rulesCategoryChoices = [
    discord.SelectOption(label="Groups", value="groups"),
    discord.SelectOption(label="Items / Accessories", value="items"),
    discord.SelectOption(label="Favorite Games", value="games"),
    discord.SelectOption(label="Keywords", value="keywords"),
    discord.SelectOption(label="Badges", value="badges"),
    discord.SelectOption(label="Legacy / Other", value="legacy"),
]
_rulesCategoryTypeMap = {
    "groups": {"group"},
    "items": {"item", "creator"},
    "games": {"game", "game_keyword"},
    "keywords": {"keyword", "group_keyword", "item_keyword", "game_keyword"},
    "badges": {"badge"},
    "legacy": {"username", "roblox_user", "watchlist", "banned_user"},
}


def _hasModPerm(member: discord.Member) -> bool:
    rawRoleIds = (
        getattr(config, "moderatorRoleId", None),
        getattr(config, "bgReviewModeratorRoleId", None),
        getattr(config, "bgItemReviewReviewerRoleId", None),
    )
    roleIds = {
        int(rawRoleId)
        for rawRoleId in rawRoleIds
        if rawRoleId is not None and int(rawRoleId or 0) > 0
    }
    if not roleIds:
        return True
    return any(int(role.id) in roleIds for role in member.roles)


def _isOpenBgFlagGuild(guildId: int) -> bool:
    configured = getattr(config, "bgFlagOpenGuildIds", []) or []
    try:
        return int(guildId or 0) in {int(rawId) for rawId in configured if int(rawId or 0) > 0}
    except (TypeError, ValueError):
        return False


async def _requireModPermission(interaction: discord.Interaction) -> bool:
    member = interaction.user
    guildId = int(getattr(getattr(interaction, "guild", None), "id", 0) or 0)
    if _isOpenBgFlagGuild(guildId):
        return True
    if isinstance(member, discord.Member) and _hasModPerm(member):
        return True
    await interactionRuntime.safeInteractionReply(
        interaction,
        content=_modsOnlyMessage,
        ephemeral=True,
    )
    return False


def _normalizeAddableRuleType(value: str) -> Optional[str]:
    normalized = str(value or "").strip().lower()
    if normalized in _addableFlagTypeValues:
        return normalized
    return None


def _normalizeSeverityChoice(value: object) -> int:
    try:
        severity = int(value or 50)
    except (TypeError, ValueError):
        return 50
    if severity in _severityLabels and severity > 0:
        return severity
    return 50


def _severityText(value: object) -> str:
    severity = flagService.normalizeSeverity(value)
    label = _severityLabels.get(severity)
    if label:
        return label if severity > 0 else "default"
    return str(severity) if severity > 0 else "default"


def _ruleValueLabel(ruleType: str) -> str:
    return {
        "group": "Roblox Group ID",
        "keyword": "Keyword",
        "item": "Roblox Item ID",
        "creator": "Roblox Creator ID",
        "badge": "Roblox Badge ID",
        "game": "Roblox Universe ID",
        "game_keyword": "Favorite Game Keyword",
    }.get(str(ruleType or "").strip().lower(), "Rule Value")


def _ruleValuePlaceholder(ruleType: str) -> str:
    return {
        "group": "Example: 5502618",
        "keyword": "Example: suspicious phrase",
        "item": "Example: 123456789",
        "creator": "Example: 123456789",
        "badge": "Example: 123456789",
        "game": "Example: 123456789",
        "game_keyword": "Example: game keyword",
    }.get(str(ruleType or "").strip().lower(), "ID or keyword")


def _thumbnailUrlFromRows(rows: list[dict], targetId: int) -> Optional[str]:
    for row in list(rows or []):
        if int(row.get("id") or row.get("targetId") or 0) != int(targetId):
            continue
        imageUrl = str(row.get("imageUrl") or "").strip()
        state = str(row.get("state") or "").strip().lower()
        if imageUrl and state in {"", "completed"}:
            return imageUrl
    return None


async def _proposalPreviewImageUrl(proposal: dict) -> Optional[str]:
    ruleType = str(proposal.get("ruleType") or "").strip().lower()
    if ruleType not in {"item", "group", "badge", "game"}:
        return None
    try:
        targetId = int(str(proposal.get("ruleValue") or "").strip())
    except (TypeError, ValueError):
        return None
    if targetId <= 0:
        return None

    try:
        if ruleType == "item":
            result = await robloxAssets.fetchRobloxAssetThumbnails([targetId])
            return _thumbnailUrlFromRows(list(result.thumbnails or []), targetId)
        thumbnailKind = {
            "group": "group",
            "badge": "badge",
            "game": "game",
        }.get(ruleType)
        if not thumbnailKind:
            return None
        return await robloxThumbnails.fetchRobloxThumbnailUrl(thumbnailKind, targetId)
    except Exception:
        return None


def _normalizeRuleValue(ruleType: str, rawValue: object) -> tuple[str | None, str | None]:
    normalizedRuleType = _normalizeAddableRuleType(ruleType)
    if not normalizedRuleType:
        return None, "Invalid rule type."

    valueText = str(rawValue or "").strip()
    if not valueText:
        return None, "Rule value is required."

    if normalizedRuleType in _addableNumericRuleTypes:
        try:
            parsed = int(valueText)
        except ValueError:
            return None, f"{_ruleValueLabel(normalizedRuleType)} must be numeric."
        if parsed <= 0:
            return None, f"{_ruleValueLabel(normalizedRuleType)} must be greater than 0."
        return str(parsed), None
    return valueText.lower(), None


def _proposalDecision(counts: dict[str, int]) -> str:
    flagVotes = int(counts.get(flagService.PROPOSAL_VOTE_FLAG, 0) or 0)
    notFlagVotes = int(counts.get(flagService.PROPOSAL_VOTE_NOT_FLAG, 0) or 0)
    if notFlagVotes > flagVotes:
        return flagService.PROPOSAL_STATUS_REJECTED
    return ""


def _ruleField(rule: dict) -> tuple[str, str]:
    ruleId = int(rule.get("ruleId") or 0)
    ruleType = str(rule.get("ruleType") or "").strip().lower()
    ruleValue = str(rule.get("ruleValue") or "").strip()
    note = str(rule.get("note") or "").strip()
    severity = int(rule.get("severity") or 0)
    fieldName = f"#{ruleId} [{ruleType}]"
    fieldValue = (
        f"Value: `{ruleValue}`\n"
        f"Severity: `{_severityText(severity)}`\n"
        f"Note: {note if note else '(none)'}"
    )
    return fieldName, fieldValue


def _formatVisualRefSyncResult(result: dict) -> str:
    assetCount = int(result.get("assetCount") or 0)
    validCount = int(result.get("validatedCount") or 0)
    invalidCount = int(result.get("invalidCount") or 0)
    errorCount = int(result.get("errorCount") or 0)
    pendingCount = int(result.get("pendingCount") or 0)
    checkedCount = int(result.get("checkedCount") or 0)
    removedCount = int(result.get("removedCount") or 0)
    parts = [
        f"assets={assetCount}",
        f"valid={validCount}",
        f"invalid={invalidCount}",
        f"errors={errorCount}",
    ]
    if pendingCount > 0:
        parts.append(f"pending={pendingCount}")
    if checkedCount > 0:
        parts.append(f"checked={checkedCount}")
    if removedCount > 0:
        parts.append(f"removed={removedCount}")
    issues = [str(value).strip() for value in list(result.get("sampleIssues") or []) if str(value).strip()]
    if issues:
        parts.append("issues=" + "; ".join(issues[:3]))
    return ", ".join(parts)


def _buildQueueFlagEmbed(queueRows: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Queued Item Flags",
        description="Reviewer-flagged queue items that now feed visual matching.",
        color=discord.Color.blurple(),
    )
    if not queueRows:
        embed.add_field(name="Items", value="No flagged queue items found.", inline=False)
        return embed

    lines: list[str] = []
    for row in queueRows[:15]:
        queueId = int(row.get("queueId") or 0)
        assetId = int(row.get("assetId") or 0)
        assetName = str(row.get("assetName") or f"Asset {assetId}").strip() or f"Asset {assetId}"
        creatorName = str(row.get("creatorName") or "unknown").strip() or "unknown"
        reviewNote = str(row.get("reviewNote") or "").strip()
        line = f"`#{queueId}` `{assetId}` {assetName} | {creatorName}"
        if reviewNote:
            trimmedNote = reviewNote if len(reviewNote) <= 80 else reviewNote[:77].rstrip() + "..."
            line += f"\n{trimmedNote}"
        lines.append(line)

    embed.add_field(name="Flagged Queue Items", value="\n".join(lines)[:1024], inline=False)
    if len(queueRows) > 15:
        embed.set_footer(text=f"Showing 15 of {len(queueRows)} flagged queue items.")
    return embed


async def _buildProposalEmbed(proposal: dict) -> discord.Embed:
    proposalId = int(proposal.get("proposalId") or 0)
    status = flagService.normalizeProposalStatus(proposal.get("status"))
    counts = await flagService.proposalVoteCounts(proposalId)
    flagVotes = int(counts.get(flagService.PROPOSAL_VOTE_FLAG, 0) or 0)
    notFlagVotes = int(counts.get(flagService.PROPOSAL_VOTE_NOT_FLAG, 0) or 0)

    statusLabel = {
        flagService.PROPOSAL_STATUS_OPEN: "Active / Voting",
        flagService.PROPOSAL_STATUS_APPROVED: "Approved",
        flagService.PROPOSAL_STATUS_REJECTED: "Rejected / Removed",
        flagService.PROPOSAL_STATUS_CLOSED: "Closed",
    }.get(status, status.title())
    color = discord.Color.blurple()
    if status == flagService.PROPOSAL_STATUS_APPROVED:
        color = discord.Color.red()
    elif status == flagService.PROPOSAL_STATUS_REJECTED:
        color = discord.Color.green()
    elif status == flagService.PROPOSAL_STATUS_CLOSED:
        color = discord.Color.dark_grey()

    embed = discord.Embed(
        title=f"BG Flag Vote #{proposalId}",
        description=f"Status: **{statusLabel}**",
        color=color,
    )
    embed.add_field(name="Rule Type", value=f"`{str(proposal.get('ruleType') or '').strip()}`", inline=True)
    embed.add_field(name="Value", value=f"`{str(proposal.get('ruleValue') or '').strip()}`", inline=True)
    embed.add_field(name="Severity", value=f"`{_severityText(proposal.get('severity'))}`", inline=True)
    note = str(proposal.get("note") or "").strip()
    if note:
        embed.add_field(name="Note", value=note[:1024], inline=False)
    proposedBy = int(proposal.get("proposedBy") or 0)
    if proposedBy > 0:
        embed.add_field(name="Proposed By", value=f"<@{proposedBy}>", inline=True)
    embed.add_field(
        name="Votes",
        value=(
            f"Flag: `{flagVotes}`\n"
            f"Not a flag: `{notFlagVotes}`\n"
            "Rule is removed only if `Not a Flag` votes outnumber `Flag` votes.\n"
            f"Votes close after `{flagService.PROPOSAL_VOTE_WINDOW_HOURS}` hours."
        ),
        inline=True,
    )
    resultingRuleId = int(proposal.get("resultingRuleId") or 0)
    if resultingRuleId > 0:
        if status == flagService.PROPOSAL_STATUS_REJECTED:
            resultText = f"Rule `#{resultingRuleId}` was removed."
        else:
            resultText = f"Rule `#{resultingRuleId}` is active."
        embed.add_field(name="Rule", value=resultText, inline=False)
    previewImageUrl = await _proposalPreviewImageUrl(proposal)
    if previewImageUrl:
        embed.set_thumbnail(url=previewImageUrl)
    if status == flagService.PROPOSAL_STATUS_CLOSED:
        embed.set_footer(text="Voting is closed. The rule remains active.")
    else:
        embed.set_footer(text="Reviewers can change their vote while this proposal remains active.")
    return embed


def _viewForProposal(proposal: dict) -> "BgFlagProposalVoteView":
    view = BgFlagProposalVoteView(int(proposal.get("proposalId") or 0))
    if flagService.normalizeProposalStatus(proposal.get("status")) != flagService.PROPOSAL_STATUS_OPEN:
        for child in view.children:
            child.disabled = True
    return view


async def _refreshProposalMessage(
    botClient: discord.Client,
    proposalId: int,
    *,
    message: discord.Message | None = None,
) -> bool:
    proposal = await flagService.getProposal(int(proposalId))
    if not proposal:
        return False
    channelId = int(proposal.get("channelId") or 0)
    messageId = int(proposal.get("messageId") or 0)
    if message is None and channelId > 0 and messageId > 0:
        message = await itemReviewWorkflow._fetchMessage(
            botClient,
            channelId=channelId,
            messageId=messageId,
        )
    if message is None:
        return False

    guildId = int(proposal.get("guildId") or 0)
    webhookName = itemReviewWorkflow._webhookName(guildId)
    embed = await _buildProposalEmbed(proposal)
    view = _viewForProposal(proposal)
    edited = await runtimeWebhooks.editOwnedWebhookMessage(
        botClient=botClient,
        message=message,
        webhookName=webhookName,
        embed=embed,
        view=view,
        reason="Jane BG flag proposal update",
    )
    if edited:
        return True
    return await interactionRuntime.safeMessageEdit(message, embed=embed, view=view)


async def _postProposalMessage(
    botClient: discord.Client,
    proposal: dict,
) -> dict[str, int | str | bool]:
    guildId = int(proposal.get("guildId") or 0)
    channelId = itemReviewWorkflow._queueChannelId(guildId)
    channel = await itemReviewWorkflow._resolveChannel(botClient, channelId)
    if channel is None:
        return {"ok": False, "reason": "BG item review queue channel is not configured or could not be resolved."}

    webhookName = itemReviewWorkflow._webhookName(guildId)
    embed = await _buildProposalEmbed(proposal)
    view = _viewForProposal(proposal)
    sentMessage = await runtimeWebhooks.sendOwnedWebhookMessageDetailed(
        botClient=botClient,
        channel=channel,
        webhookName=webhookName,
        embed=embed,
        view=view,
        username="Jane Item Review",
        reason="Jane BG flag proposal",
    )
    if sentMessage is None:
        sentMessage = await interactionRuntime.safeChannelSend(channel, embed=embed, view=view)
        if sentMessage is None:
            return {"ok": False, "reason": "Jane could not post the flag vote in the item review channel."}
        if hasattr(botClient, "add_view"):
            botClient.add_view(view, message_id=int(sentMessage.id))

    await flagService.setProposalMessage(
        int(proposal.get("proposalId") or 0),
        channelId=int(getattr(sentMessage.channel, "id", channelId) or channelId),
        messageId=int(sentMessage.id),
    )
    return {
        "ok": True,
        "channelId": int(getattr(sentMessage.channel, "id", channelId) or channelId),
        "messageId": int(sentMessage.id),
    }


class BgRulesPanelView(discord.ui.View):
    def __init__(self, rules: list[dict], *, pageSize: int = 5) -> None:
        super().__init__(timeout=600)
        self.rules = list(rules or [])
        self.pageSize = max(1, min(10, int(pageSize)))
        self.selectedCategory = "groups"
        self.pageIndex = 0
        self._syncControls()

    def _filteredRules(self) -> list[dict]:
        allowedTypes = _rulesCategoryTypeMap.get(self.selectedCategory, set())
        if not allowedTypes:
            return []
        return [
            rule for rule in self.rules
            if str(rule.get("ruleType") or "").strip().lower() in allowedTypes
        ]

    def _pageCount(self, filtered: list[dict]) -> int:
        if not filtered:
            return 1
        return max(1, (len(filtered) + self.pageSize - 1) // self.pageSize)

    def _buildEmbed(self) -> discord.Embed:
        filtered = self._filteredRules()
        pageCount = self._pageCount(filtered)
        self.pageIndex = min(max(0, self.pageIndex), pageCount - 1)
        start = self.pageIndex * self.pageSize
        end = min(len(filtered), start + self.pageSize)
        pageRows = filtered[start:end]

        categoryLabel = next(
            (choice.label for choice in _rulesCategoryChoices if choice.value == self.selectedCategory),
            self.selectedCategory.title(),
        )
        embed = discord.Embed(
            title="BG Flag Rules",
            description=(
                f"Category: **{categoryLabel}**\n"
                f"Page **{self.pageIndex + 1}/{pageCount}** | "
                f"Showing **{start + 1 if filtered else 0}-{end}** of **{len(filtered)}**"
            ),
            color=discord.Color.blurple(),
        )
        if not pageRows:
            embed.add_field(name="Rules", value="No rules in this category.", inline=False)
        else:
            for rule in pageRows:
                fieldName, fieldValue = _ruleField(rule)
                embed.add_field(name=fieldName, value=fieldValue, inline=False)
        embed.set_footer(text="Use the dropdown to switch category and buttons to change pages.")
        return embed

    def _syncControls(self) -> None:
        filtered = self._filteredRules()
        pageCount = self._pageCount(filtered)
        self.pageIndex = min(max(0, self.pageIndex), pageCount - 1)
        self.prevBtn.disabled = self.pageIndex <= 0
        self.nextBtn.disabled = self.pageIndex >= pageCount - 1
        self.typeSelect.placeholder = f"Category: {self.selectedCategory}"

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._syncControls()
        await runtimeViewBases.safeRefreshInteractionMessage(
            interaction,
            embed=self._buildEmbed(),
            view=self,
        )

    @discord.ui.select(
        row=0,
        options=_rulesCategoryChoices,
        placeholder="Select rule category",
        min_values=1,
        max_values=1,
    )
    async def typeSelect(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        value = str(select.values[0] if select.values else "").strip().lower()
        if value not in _rulesCategoryTypeMap:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Invalid rule category selection.",
                ephemeral=True,
            )
            return
        self.selectedCategory = value
        self.pageIndex = 0
        await self._refresh(interaction)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prevBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.pageIndex = max(0, self.pageIndex - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def nextBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.pageIndex += 1
        await self._refresh(interaction)


class BgFlagProposalVoteView(discord.ui.View):
    def __init__(self, proposalId: int) -> None:
        super().__init__(timeout=None)
        self.proposalId = int(proposalId)
        self.flagBtn.custom_id = f"bgflagproposal:flag:{self.proposalId}"
        self.notFlagBtn.custom_id = f"bgflagproposal:notflag:{self.proposalId}"

    async def _vote(self, interaction: discord.Interaction, vote: str) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)

        proposal = await flagService.getProposal(self.proposalId)
        if not proposal:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="This flag proposal no longer exists.",
                ephemeral=True,
            )
            return
        status = flagService.normalizeProposalStatus(proposal.get("status"))
        if status == flagService.PROPOSAL_STATUS_OPEN and await flagService.closeExpiredProposal(self.proposalId):
            await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
            await _refreshProposalMessage(
                interaction.client,
                self.proposalId,
                message=interaction.message,
            )
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=(
                    f"Voting for this flag proposal closed after "
                    f"{flagService.PROPOSAL_VOTE_WINDOW_HOURS} hours. The rule remains active."
                ),
                ephemeral=True,
            )
            return
        if status == flagService.PROPOSAL_STATUS_OPEN:
            proposal = await flagService.getProposal(self.proposalId) or proposal
            status = flagService.normalizeProposalStatus(proposal.get("status"))
        if status != flagService.PROPOSAL_STATUS_OPEN:
            closedText = (
                "Voting for this flag proposal has closed."
                if status == flagService.PROPOSAL_STATUS_CLOSED
                else "This flag proposal is already resolved."
            )
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=closedText,
                ephemeral=True,
            )
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        await flagService.upsertProposalVote(
            self.proposalId,
            voterId=int(interaction.user.id),
            vote=vote,
        )
        counts = await flagService.proposalVoteCounts(self.proposalId)
        decision = _proposalDecision(counts)
        reply = "Vote recorded."

        if decision == flagService.PROPOSAL_STATUS_REJECTED:
            rejected = await flagService.rejectProposal(
                self.proposalId,
                resolvedBy=int(interaction.user.id),
            )
            if rejected:
                if str(proposal.get("ruleType") or "").strip().lower() == "item":
                    syncResult = await flagService.syncItemVisualReferences(force=False)
                    reply = (
                        "Vote recorded. Not-a-flag votes now outnumber flag votes, so Jane removed the rule. "
                        f"Visual refs synced: {_formatVisualRefSyncResult(syncResult)}."
                    )
                else:
                    reply = "Vote recorded. Not-a-flag votes now outnumber flag votes, so Jane removed the rule."
        else:
            reply = "Vote recorded. The rule remains active."

        await _refreshProposalMessage(
            interaction.client,
            self.proposalId,
            message=interaction.message,
        )
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=reply,
            ephemeral=True,
        )

    @discord.ui.button(label="Flag", style=discord.ButtonStyle.danger, row=0)
    async def flagBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._vote(interaction, flagService.PROPOSAL_VOTE_FLAG)

    @discord.ui.button(label="Not a Flag", style=discord.ButtonStyle.success, row=0)
    async def notFlagBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._vote(interaction, flagService.PROPOSAL_VOTE_NOT_FLAG)


class BgFlagAddRuleSetupView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.selectedRuleType = ""
        self.selectedSeverity = 50

    @discord.ui.select(
        row=0,
        options=_addableFlagTypeChoices,
        placeholder="Choose flag type",
        min_values=1,
        max_values=1,
    )
    async def ruleTypeSelect(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.selectedRuleType = _normalizeAddableRuleType(select.values[0] if select.values else "") or ""
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)

    @discord.ui.select(
        row=1,
        options=_severityChoices,
        placeholder="Choose severity",
        min_values=1,
        max_values=1,
    )
    async def severitySelect(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.selectedSeverity = _normalizeSeverityChoice(select.values[0] if select.values else 50)
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, row=2)
    async def continueBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        normalizedRuleType = _normalizeAddableRuleType(self.selectedRuleType)
        if not normalizedRuleType:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Choose a flag type first.",
                ephemeral=True,
            )
            return
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            BgFlagAddRuleDetailsModal(normalizedRuleType, self.selectedSeverity),
        )


class BgFlagAddRuleDetailsModal(discord.ui.Modal, title="Propose BG Flag"):
    def __init__(self, ruleType: str, severity: int) -> None:
        super().__init__()
        self.ruleType = _normalizeAddableRuleType(ruleType) or ""
        self.severity = _normalizeSeverityChoice(severity)
        self.value = discord.ui.TextInput(
            label=_ruleValueLabel(self.ruleType),
            placeholder=_ruleValuePlaceholder(self.ruleType),
            required=True,
            max_length=200,
        )
        self.note = discord.ui.TextInput(
            label="Note (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
        )
        self.add_item(self.value)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _requireModPermission(interaction):
            return

        normalizedRuleType = _normalizeAddableRuleType(self.ruleType)
        normalizedValue, errorText = _normalizeRuleValue(normalizedRuleType or "", str(self.value))
        if not normalizedRuleType or errorText or normalizedValue is None:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=errorText or "Invalid flag proposal.",
                ephemeral=True,
            )
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        if normalizedRuleType == "item":
            validation = await flagService.validateItemVisualReference(int(normalizedValue))
            validationState = str(validation.get("validationState") or "").strip().upper()
            if validationState != "VALID":
                error = str(validation.get("validationError") or "").strip() or "Item thumbnail validation failed."
                await interactionRuntime.safeInteractionReply(
                    interaction,
                    content=f"Jane could not validate item `{normalizedValue}` as a usable visual reference. {error}",
                    ephemeral=True,
                )
                return

        proposalId = await flagService.createProposal(
            guildId=int(interaction.guild_id or 0),
            ruleType=normalizedRuleType,
            ruleValue=normalizedValue,
            note=str(self.note).strip() or None,
            proposedBy=int(interaction.user.id),
            severity=self.severity,
        )
        if normalizedRuleType == "item":
            await flagService.syncItemVisualReferences(force=False)
        proposal = await flagService.getProposal(proposalId)
        if not proposal:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Jane created the proposal, but could not load it back.",
                ephemeral=True,
            )
            return

        postResult = await _postProposalMessage(interaction.client, proposal)
        if not bool(postResult.get("ok")):
            await flagService.deleteProposal(proposalId)
            if normalizedRuleType == "item":
                await flagService.syncItemVisualReferences(force=False)
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=str(postResult.get("reason") or "Jane could not post the flag vote."),
                ephemeral=True,
            )
            return

        await interactionRuntime.safeInteractionReply(
            interaction,
            content=(
                f"Created rule #{int(proposal.get('resultingRuleId') or 0)} and posted flag vote #{proposalId} "
                f"in <#{int(postResult.get('channelId') or 0)}>."
            ),
            ephemeral=True,
        )


class BgFlagRemoveRuleModal(discord.ui.Modal, title="Remove BG Flag Rule"):
    ruleId = discord.ui.TextInput(
        label="Rule ID",
        placeholder="Numeric rule ID",
        required=True,
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _requireModPermission(interaction):
            return

        try:
            parsedRuleId = int(str(self.ruleId).strip())
        except ValueError:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Rule ID must be numeric.",
                ephemeral=True,
            )
            return

        existingRule = await flagService.getRule(parsedRuleId)
        removedRuleType = str((existingRule or {}).get("ruleType") or "").strip().lower()
        await flagService.removeRule(parsedRuleId)
        extraText = ""
        if removedRuleType == "item":
            await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
            syncResult = await flagService.syncItemVisualReferences(force=False)
            extraText = f" Visual refs synced: {_formatVisualRefSyncResult(syncResult)}."
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"Removed rule #{parsedRuleId}.{extraText}",
            ephemeral=True,
        )


class BgFlagImportBadgesModal(discord.ui.Modal, title="Import Badge Rules"):
    universeId = discord.ui.TextInput(
        label="Universe ID",
        placeholder="Numeric Roblox universe ID",
        required=True,
        max_length=30,
    )
    maxBadges = discord.ui.TextInput(
        label="Max Badges (optional)",
        placeholder="Default from config if blank",
        required=False,
        max_length=6,
    )
    note = discord.ui.TextInput(
        label="Note (optional)",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _requireModPermission(interaction):
            return

        try:
            parsedUniverseId = int(str(self.universeId).strip())
        except ValueError:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Universe ID must be numeric.",
                ephemeral=True,
            )
            return

        rawMax = str(self.maxBadges).strip()
        if rawMax:
            try:
                limit = int(rawMax)
            except ValueError:
                await interactionRuntime.safeInteractionReply(
                    interaction,
                    content="Max badges must be numeric.",
                    ephemeral=True,
                )
                return
        else:
            limit = int(getattr(config, "robloxBadgeImportMax", 200) or 200)

        if limit <= 0:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Max badges must be greater than 0.",
                ephemeral=True,
            )
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)

        existing = await flagService.listRules("badge")
        existingIds: set[int] = set()
        for rule in existing:
            try:
                existingIds.add(int(rule.get("ruleValue")))
            except (TypeError, ValueError):
                continue

        added = 0
        skipped = 0
        cursor: Optional[str] = None
        sortOrder = "Asc"
        batchSize = 100
        noteSuffix = str(self.note).strip() or None

        while added < limit:
            pageLimit = min(batchSize, limit - added)
            result = await robloxBadges.fetchRobloxUniverseBadges(
                parsedUniverseId,
                limit=pageLimit,
                cursor=cursor,
                sortOrder=sortOrder,
            )
            if result.error:
                await interactionRuntime.safeInteractionReply(
                    interaction,
                    content=f"Import stopped: {result.error}",
                    ephemeral=True,
                )
                return
            if not result.badges:
                break

            for badge in result.badges:
                badgeId = badge.get("id")
                if badgeId is None:
                    continue
                if badgeId in existingIds:
                    skipped += 1
                    continue
                badgeName = badge.get("name")
                noteParts = []
                if badgeName:
                    noteParts.append(str(badgeName))
                noteParts.append(f"universe {parsedUniverseId}")
                if noteSuffix:
                    noteParts.append(noteSuffix)
                noteText = " | ".join(noteParts) if noteParts else None
                await flagService.addRule("badge", str(badgeId), noteText, interaction.user.id)
                existingIds.add(badgeId)
                added += 1
                if added >= limit:
                    break

            cursor = result.nextCursor
            if not cursor:
                break

        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"Imported {added} badge rules (skipped {skipped}).",
            ephemeral=True,
        )


class BgFlagPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @discord.ui.button(label="Add Flag", style=discord.ButtonStyle.success, row=0)
    async def addRuleBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="Choose the type and severity for the proposed flag.",
            view=BgFlagAddRuleSetupView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Remove Rule", style=discord.ButtonStyle.danger, row=0)
    async def removeRuleBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            BgFlagRemoveRuleModal(),
        )

    @discord.ui.button(label="List Rules", style=discord.ButtonStyle.secondary, row=0)
    async def listRulesBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        rules = await flagService.listRules(None)
        if not rules:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="No rules found.",
                ephemeral=True,
            )
            return
        view = BgRulesPanelView(rules)
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=view._buildEmbed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Import Badges", style=discord.ButtonStyle.primary, row=0)
    async def importBadgesBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            BgFlagImportBadgesModal(),
        )

    @discord.ui.button(label="Sync Visual Refs", style=discord.ButtonStyle.secondary, row=1)
    async def syncVisualRefsBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        result = await flagService.syncItemVisualReferences(force=True)
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"Visual reference sync complete: {_formatVisualRefSyncResult(result)}.",
            ephemeral=True,
        )

    @discord.ui.button(label="List Queue Flags", style=discord.ButtonStyle.secondary, row=1)
    async def listQueueFlagsBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _requireModPermission(interaction):
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        queueRows = await itemReviewService.listQueueEntriesByStatus(
            [itemReviewService.STATUS_FLAGGED],
            guildId=int(interaction.guild_id or 0) or None,
            limit=15,
        )
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=_buildQueueFlagEmbed(queueRows),
            ephemeral=True,
        )


class BgFlagCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        rows = await flagService.listOpenProposalsWithMessages()
        for row in rows:
            messageId = int(row.get("messageId") or 0)
            proposalId = int(row.get("proposalId") or 0)
            if messageId > 0 and proposalId > 0:
                self.bot.add_view(BgFlagProposalVoteView(proposalId), message_id=messageId)

    @app_commands.command(name="bg-flag", description="Open the background-check flag manager panel.")
    async def bgFlagPanel(self, interaction: discord.Interaction) -> None:
        if not await _requireModPermission(interaction):
            return

        embed = discord.Embed(
            title="BG Flag Manager",
            description=(
                "Use the panel buttons below to manage background-check flags.\n"
                "`Add Flag` posts a reviewer vote in the BG item review channel. "
                "Jane creates the rule immediately and only removes it if not-a-flag votes outnumber flag votes. "
                f"Votes close after {flagService.PROPOSAL_VOTE_WINDOW_HOURS} hours.\n"
                "Exact `item` proposals also feed Jane's visual thumbnail matcher while active.\n"
                "Supported add types: `group`, `keyword`, `item`, `creator`, `badge`, `game`, `game_keyword`."
            ),
            color=discord.Color.blurple(),
        )
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=embed,
            view=BgFlagPanelView(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BgFlagCog(bot))

