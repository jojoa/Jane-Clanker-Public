from __future__ import annotations

import json
from typing import Any, Optional

import discord

import config
from features.staff.bgItemReview import service
from features.staff.bgflags import service as flagService
from features.staff.sessions.Roblox import robloxAssets
from runtime import interaction as interactionRuntime
from runtime import orgProfiles
from runtime import permissions as runtimePermissions
from runtime import webhooks as runtimeWebhooks

def _positiveInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _orgConfigValue(name: str, *, guildId: int = 0, default: object = None) -> object:
    return orgProfiles.getOrganizationValue(
        config,
        name,
        guildId=int(guildId or 0),
        default=default,
    )


def _queueEnabled(guildId: int = 0) -> bool:
    return bool(
        _orgConfigValue(
            "bgItemReviewQueueEnabled",
            guildId=guildId,
            default=getattr(config, "bgItemReviewQueueEnabled", True),
        )
    )


def _queueChannelId(guildId: int = 0) -> int:
    return _positiveInt(
        _orgConfigValue(
            "bgItemReviewQueueChannelId",
            guildId=guildId,
            default=getattr(config, "bgItemReviewQueueChannelId", 0),
        )
    )


def _reviewerRoleId(guildId: int = 0) -> int:
    return _positiveInt(
        _orgConfigValue(
            "bgItemReviewReviewerRoleId",
            guildId=guildId,
            default=getattr(config, "bgItemReviewReviewerRoleId", getattr(config, "bgReviewModeratorRoleId", 0)),
        )
    )


def _webhookName(guildId: int = 0) -> str:
    return str(
        _orgConfigValue(
            "bgItemReviewWebhookName",
            guildId=guildId,
            default=getattr(config, "bgItemReviewWebhookName", "Jane BG Item Review"),
        )
        or "Jane BG Item Review"
    ).strip() or "Jane BG Item Review"


def _maxPagesPerType(guildId: int = 0) -> int:
    return max(
        1,
        _positiveInt(
            _orgConfigValue(
                "bgItemReviewMaxPagesPerType",
                guildId=guildId,
                default=getattr(config, "bgItemReviewMaxPagesPerType", 4),
            )
        )
        or 4,
    )


def _candidateLimit(guildId: int = 0) -> int:
    return max(
        1,
        _positiveInt(
            _orgConfigValue(
                "bgItemReviewCandidateLimit",
                guildId=guildId,
                default=getattr(config, "bgItemReviewCandidateLimit", 60),
            )
        )
        or 60,
    )


def _canReview(member: discord.Member) -> bool:
    guildId = _positiveInt(getattr(getattr(member, "guild", None), "id", 0))
    roleId = _reviewerRoleId(guildId)
    if roleId > 0 and runtimePermissions.hasAnyRole(member, [roleId]):
        return True
    if runtimePermissions.hasBgCheckCertifiedRole(member):
        return True
    return runtimePermissions.hasAdminOrManageGuild(member)


def canReviewMember(member: discord.Member) -> bool:
    return _canReview(member)


async def _resolveChannel(
    botClient: discord.Client,
    channelId: int,
) -> Optional[discord.TextChannel | discord.Thread]:
    if int(channelId or 0) <= 0:
        return None
    channel = botClient.get_channel(int(channelId))
    if channel is None:
        channel = await interactionRuntime.safeFetchChannel(botClient, int(channelId))
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


async def _fetchMessage(
    botClient: discord.Client,
    *,
    channelId: int,
    messageId: int,
) -> Optional[discord.Message]:
    channel = await _resolveChannel(botClient, int(channelId))
    if channel is None or int(messageId or 0) <= 0:
        return None
    return await interactionRuntime.safeFetchMessage(channel, int(messageId))


def _statusLabel(status: str) -> str:
    mapping = {
        service.STATUS_PENDING: "Pending Review",
        service.STATUS_FLAGGED: "Flagged",
        service.STATUS_SAFE: "Marked Safe",
        service.STATUS_IGNORED: "Ignored",
    }
    return mapping.get(service.normalizeStatus(status), service.normalizeStatus(status).replace("_", " ").title())


def _statusColor(status: str) -> discord.Color:
    normalized = service.normalizeStatus(status)
    if normalized == service.STATUS_FLAGGED:
        return discord.Color.red()
    if normalized == service.STATUS_SAFE:
        return discord.Color.green()
    if normalized == service.STATUS_IGNORED:
        return discord.Color.dark_grey()
    return discord.Color.blurple()


def _truncate(value: object, *, limit: int = 60) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, int(limit) - 3)].rstrip() + "..."


def _robloxCatalogUrl(assetId: object) -> str | None:
    assetId = _positiveInt(assetId)
    if assetId <= 0:
        return None
    return f"https://www.roblox.com/catalog/{assetId}"


def _markdownLink(label: object, url: str | None, *, fallback: object = "") -> str:
    cleanLabel = str(label or fallback or "Unknown item").strip() or str(fallback or "Unknown item").strip() or "Unknown item"
    cleanLabel = cleanLabel.replace("[", "(").replace("]", ")")
    if not url:
        return cleanLabel
    return f"[{cleanLabel}]({url})"


def _parseQueueContext(queueRow: dict[str, Any]) -> dict[str, Any]:
    raw = str(queueRow.get("contextJson") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _contextJson(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict) or not context:
        return ""
    return json.dumps(context, separators=(",", ":"), ensure_ascii=True)


def _bgIntelDisputeContextSummary(context: dict[str, Any]) -> str | None:
    normalizedType = str(context.get("kind") or "").strip().lower()
    if normalizedType != "bg_intel_dispute":
        return None
    requestedByReviewerId = _positiveInt(context.get("requestedByReviewerId"))
    reportId = _positiveInt(context.get("reportId"))
    basis = str(context.get("flagBasis") or "").strip()
    parts = ["BG intel dispute"]
    if requestedByReviewerId > 0:
        parts.append(f"requested by <@{requestedByReviewerId}>")
    if reportId > 0:
        parts.append(f"report #{reportId}")
    summary = " | ".join(parts)
    if basis:
        summary += f"\nBasis: {basis}"
    return summary


def _queueRowSummaryLine(queueRow: dict[str, Any]) -> str:
    queueId = _positiveInt(queueRow.get("queueId"))
    assetId = _positiveInt(queueRow.get("assetId"))
    assetName = _truncate(queueRow.get("assetName") or f"Asset {assetId}", limit=44) or f"Asset {assetId}"
    creatorName = _truncate(queueRow.get("creatorName") or "unknown", limit=24) or "unknown"
    return f"`#{queueId}` `{assetId}` {assetName} | {creatorName}"


async def buildQueueSummaryEmbed(*, guildId: int = 0) -> discord.Embed:
    normalizedGuildId = int(guildId or 0) or None
    counts = await service.listQueueCounts(guildId=normalizedGuildId)
    pendingRows = await service.listQueueEntriesByStatus(
        [service.STATUS_PENDING],
        guildId=normalizedGuildId,
        limit=5,
    )
    flaggedRows = await service.listQueueEntriesByStatus(
        [service.STATUS_FLAGGED],
        guildId=normalizedGuildId,
        limit=5,
    )

    queueChannelId = _queueChannelId(int(guildId or 0))
    if queueChannelId > 0:
        description = f"Queue channel: <#{queueChannelId}>"
    else:
        description = "Queue channel not configured."

    embed = discord.Embed(
        title="BG Item Review Queue",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Pending", value=f"`{int(counts.get(service.STATUS_PENDING, 0))}`", inline=True)
    embed.add_field(name="Flagged", value=f"`{int(counts.get(service.STATUS_FLAGGED, 0))}`", inline=True)
    embed.add_field(name="Safe", value=f"`{int(counts.get(service.STATUS_SAFE, 0))}`", inline=True)
    otherCount = int(counts.get(service.STATUS_IGNORED, 0))
    if otherCount > 0:
        embed.add_field(name="Other", value=f"`{otherCount}`", inline=True)
    embed.add_field(name="Total", value=f"`{int(counts.get('total', 0))}`", inline=True)

    pendingText = "\n".join(_queueRowSummaryLine(row) for row in pendingRows[:5]) or "No pending items."
    flaggedText = "\n".join(_queueRowSummaryLine(row) for row in flaggedRows[:5]) or "No flagged items."
    embed.add_field(name="Recent Pending", value=pendingText[:1024], inline=False)
    embed.add_field(name="Recent Flagged", value=flaggedText[:1024], inline=False)
    embed.set_footer(text="BG item review slash commands are disabled.")
    return embed


async def postQueueSummaryMessage(
    botClient: discord.Client,
    *,
    guildId: int = 0,
) -> Optional[discord.Message]:
    channelId = _queueChannelId(guildId)
    channel = await _resolveChannel(botClient, channelId)
    if channel is None:
        return None

    webhookName = _webhookName(guildId)
    embed = await buildQueueSummaryEmbed(guildId=guildId)
    sentMessage = await runtimeWebhooks.sendOwnedWebhookMessageDetailed(
        botClient=botClient,
        channel=channel,
        webhookName=webhookName,
        embed=embed,
        username="Jane Item Review",
        reason="Jane BG item review summary",
    )
    if sentMessage is not None:
        return sentMessage
    return await interactionRuntime.safeChannelSend(channel, embed=embed)


async def _buildQueueEmbed(queueRow: dict[str, Any]) -> discord.Embed:
    queueId = int(queueRow.get("queueId") or 0)
    assetId = int(queueRow.get("assetId") or 0)
    status = service.normalizeStatus(queueRow.get("status"))
    sources = await service.listSourcesForQueue(queueId, limit=3)
    context = _parseQueueContext(queueRow)

    embed = discord.Embed(
        title=str(queueRow.get("assetName") or f"Asset {assetId}").strip() or f"Asset {assetId}",
        description=f"Status: **{_statusLabel(status)}**",
        color=_statusColor(status),
    )
    creatorText = str(queueRow.get("creatorName") or "").strip()
    creatorId = _positiveInt(queueRow.get("creatorId"))
    if creatorId > 0:
        creatorText = f"{creatorText} (`{creatorId}`)".strip()
    if not creatorText:
        creatorText = "`unknown`"
    priceValue = queueRow.get("priceRobux")
    if priceValue is None:
        priceText = "`unknown`"
    else:
        priceText = f"`{int(priceValue):,} R$`"
    embed.add_field(name="Creator", value=creatorText, inline=True)
    embed.add_field(name="Price", value=priceText, inline=True)
    embed.add_field(
        name="Seen",
        value=f"`{int(queueRow.get('seenCount') or 0):,}` time(s)",
        inline=True,
    )
    itemType = str(queueRow.get("itemType") or "").strip()
    if itemType:
        embed.add_field(name="Type", value=f"`{itemType}`", inline=True)
    embed.add_field(name="Asset ID", value=f"`{assetId}`", inline=True)
    hashValue = str(queueRow.get("thumbnailHash") or "").strip()
    if hashValue:
        embed.add_field(name="Hash", value=f"`{hashValue}`", inline=True)

    contextSummary = _bgIntelDisputeContextSummary(context)
    if contextSummary:
        embed.add_field(name="Queue Context", value=contextSummary[:1024], inline=False)

    disputeItem = context.get("disputedItem") if isinstance(context.get("disputedItem"), dict) else {}
    if disputeItem:
        disputeName = str(disputeItem.get("name") or queueRow.get("assetName") or f"Asset {assetId}").strip() or f"Asset {assetId}"
        disputeUrl = _robloxCatalogUrl(disputeItem.get("id") or assetId)
        disputeBits = [_markdownLink(disputeName, disputeUrl, fallback=f"Asset {assetId}")]
        creatorName = str(disputeItem.get("creatorName") or "").strip()
        creatorId = _positiveInt(disputeItem.get("creatorId"))
        if creatorName or creatorId > 0:
            creatorLine = creatorName or "creator"
            if creatorId > 0:
                creatorLine += f" (`{creatorId}`)"
            disputeBits.append(f"Creator: {creatorLine}")
        itemType = str(disputeItem.get("itemType") or "").strip()
        if itemType:
            disputeBits.append(f"Type: `{itemType}`")
        embed.add_field(name="Disputed Item", value="\n".join(disputeBits)[:1024], inline=False)

    flagBasis = str(context.get("flagBasis") or "").strip()
    if flagBasis:
        embed.add_field(name="Flag Basis", value=flagBasis[:1024], inline=False)

    referenceItem = context.get("referenceItem") if isinstance(context.get("referenceItem"), dict) else {}
    if referenceItem:
        referenceId = _positiveInt(referenceItem.get("id"))
        referenceName = str(referenceItem.get("name") or f"Asset {referenceId}").strip() or f"Asset {referenceId}"
        referenceUrl = _robloxCatalogUrl(referenceId)
        referenceBits = [_markdownLink(referenceName, referenceUrl, fallback=f"Asset {referenceId}")]
        referenceReason = str(referenceItem.get("reason") or "").strip()
        if referenceReason:
            referenceBits.append(referenceReason)
        embed.add_field(name="Matched Against", value="\n".join(referenceBits)[:1024], inline=False)
        referenceThumbnailUrl = str(referenceItem.get("thumbnailUrl") or "").strip()
        if referenceThumbnailUrl:
            embed.set_thumbnail(url=referenceThumbnailUrl)

    lastSourceUserId = _positiveInt(queueRow.get("sourceUserId"))
    lastSourceRoblox = str(queueRow.get("sourceRobloxUsername") or "").strip()
    if lastSourceUserId > 0 or lastSourceRoblox:
        sourceBits: list[str] = []
        if lastSourceUserId > 0:
            sourceBits.append(f"<@{lastSourceUserId}>")
        if lastSourceRoblox:
            sourceBits.append(f"`{lastSourceRoblox}`")
        embed.add_field(name="Last Source", value=" / ".join(sourceBits), inline=False)

    if sources:
        recentLines: list[str] = []
        for row in sources:
            lineBits: list[str] = []
            sourceUserId = _positiveInt(row.get("sourceUserId"))
            sourceRobloxUsername = str(row.get("sourceRobloxUsername") or "").strip()
            if sourceUserId > 0:
                lineBits.append(f"<@{sourceUserId}>")
            if sourceRobloxUsername:
                lineBits.append(f"`{sourceRobloxUsername}`")
            if not lineBits:
                continue
            recentLines.append(" / ".join(lineBits))
        if recentLines:
            embed.add_field(name="Recent Sources", value="\n".join(recentLines[:3]), inline=False)

    reviewNote = str(queueRow.get("reviewNote") or "").strip()
    if reviewNote:
        embed.add_field(name="Reviewer Note", value=reviewNote[:1024], inline=False)

    reviewedBy = _positiveInt(queueRow.get("reviewedBy"))
    if reviewedBy > 0:
        embed.set_footer(text=f"Queue #{queueId} | Reviewed by {reviewedBy}")
    else:
        embed.set_footer(text=f"Queue #{queueId}")

    thumbnailUrl = str(queueRow.get("thumbnailUrl") or "").strip()
    if thumbnailUrl:
        embed.set_image(url=thumbnailUrl)
    return embed


class BgItemReviewView(discord.ui.View):
    def __init__(self, queueId: int) -> None:
        super().__init__(timeout=None)
        self.queueId = int(queueId)
        self.flagBtn.custom_id = f"bgitemreview:flag:{self.queueId}"
        self.safeBtn.custom_id = f"bgitemreview:safe:{self.queueId}"

    async def _guard(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not _canReview(member):
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Only BG reviewers can use this queue.",
                ephemeral=True,
            )
            return False
        return True

    async def _applyDecision(self, interaction: discord.Interaction, newStatus: str) -> None:
        if not await self._guard(interaction):
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        queueRow = await service.getQueueEntry(self.queueId)
        if not queueRow:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="This item review entry no longer exists.",
                ephemeral=True,
            )
            return

        currentStatus = service.normalizeStatus(queueRow.get("status"))
        if currentStatus in service.FINAL_STATUSES:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=f"This item has already been marked as {_statusLabel(currentStatus).lower()}.",
                ephemeral=True,
            )
            return
        updated = await service.updateQueueStatus(
            self.queueId,
            status=newStatus,
            reviewerId=int(interaction.user.id),
        )
        if not updated:
            await refreshQueueMessage(interaction.client, self.queueId, message=interaction.message)
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="This item was already reviewed by another action.",
                ephemeral=True,
            )
            return
        await service.addAction(
            self.queueId,
            actorId=int(interaction.user.id),
            action=newStatus,
        )
        await refreshQueueMessage(interaction.client, self.queueId, message=interaction.message)
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"Queue item marked as {_statusLabel(newStatus).lower()}.",
            ephemeral=True,
        )

    @discord.ui.button(label="Flag", style=discord.ButtonStyle.danger, row=0)
    async def flagBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            BgItemReviewFlagModal(self.queueId),
        )

    @discord.ui.button(label="Safe", style=discord.ButtonStyle.success, row=0)
    async def safeBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._applyDecision(interaction, service.STATUS_SAFE)


class BgItemReviewFlagModal(discord.ui.Modal, title="Flag Item Review Entry"):
    note = discord.ui.TextInput(
        label="Flag Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Why should this item be treated as flagged?",
        required=True,
        max_length=400,
    )

    def __init__(self, queueId: int) -> None:
        super().__init__()
        self.queueId = int(queueId)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not _canReview(member):
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="Only BG reviewers can use this queue.",
                ephemeral=True,
            )
            return

        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True)
        queueRow = await service.getQueueEntry(self.queueId)
        if not queueRow:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="This item review entry no longer exists.",
                ephemeral=True,
            )
            return

        currentStatus = service.normalizeStatus(queueRow.get("status"))
        if currentStatus in service.FINAL_STATUSES:
            await interactionRuntime.safeInteractionReply(
                interaction,
                content=f"This item has already been marked as {_statusLabel(currentStatus).lower()}.",
                ephemeral=True,
            )
            return

        noteText = str(self.note).strip()
        updated = await service.updateQueueStatus(
            self.queueId,
            status=service.STATUS_FLAGGED,
            reviewerId=int(interaction.user.id),
            note=noteText,
        )
        if not updated:
            await refreshQueueMessage(interaction.client, self.queueId)
            await interactionRuntime.safeInteractionReply(
                interaction,
                content="This item was already reviewed by another action.",
                ephemeral=True,
            )
            return
        await service.addAction(
            self.queueId,
            actorId=int(interaction.user.id),
            action=service.STATUS_FLAGGED,
            note=noteText,
        )
        await refreshQueueMessage(interaction.client, self.queueId)
        await interactionRuntime.safeInteractionReply(
            interaction,
            content="Queue item marked as flagged.",
            ephemeral=True,
        )


def _viewForRow(queueRow: dict[str, Any]) -> BgItemReviewView:
    view = BgItemReviewView(int(queueRow.get("queueId") or 0))
    if service.normalizeStatus(queueRow.get("status")) in service.FINAL_STATUSES:
        for child in view.children:
            child.disabled = True
    return view


async def refreshQueueMessage(
    botClient: discord.Client,
    queueId: int,
    *,
    message: Optional[discord.Message] = None,
) -> bool:
    queueRow = await service.getQueueEntry(int(queueId))
    if not queueRow:
        return False
    channelId = _positiveInt(queueRow.get("reviewChannelId"))
    messageId = _positiveInt(queueRow.get("reviewMessageId"))
    webhookName = _webhookName(_positiveInt(queueRow.get("guildId")))
    if message is None and channelId > 0 and messageId > 0:
        message = await _fetchMessage(
            botClient,
            channelId=channelId,
            messageId=messageId,
        )
    if message is None:
        return False
    embed = await _buildQueueEmbed(queueRow)
    view = _viewForRow(queueRow)
    edited = await runtimeWebhooks.editOwnedWebhookMessage(
        botClient=botClient,
        message=message,
        webhookName=webhookName,
        embed=embed,
        view=view,
        reason="Jane BG item review update",
    )
    if edited:
        return True
    return await interactionRuntime.safeMessageEdit(message, embed=embed, view=view)


async def _postQueueMessage(
    botClient: discord.Client,
    queueRow: dict[str, Any],
) -> bool:
    guildId = _positiveInt(queueRow.get("guildId"))
    channelId = _queueChannelId(guildId)
    channel = await _resolveChannel(botClient, channelId)
    if channel is None:
        return False

    webhookName = _webhookName(guildId)
    embed = await _buildQueueEmbed(queueRow)
    view = _viewForRow(queueRow)
    sentMessage = await runtimeWebhooks.sendOwnedWebhookMessageDetailed(
        botClient=botClient,
        channel=channel,
        webhookName=webhookName,
        embed=embed,
        view=view,
        username="Jane Item Review",
        reason="Jane BG item review queue",
    )
    if sentMessage is None:
        sentMessage = await interactionRuntime.safeChannelSend(channel, embed=embed, view=view)
        if sentMessage is None:
            return False
    await service.setReviewMessage(
        int(queueRow.get("queueId") or 0),
        int(channel.id),
        int(sentMessage.id),
    )
    return True


async def restorePersistentViews(botClient: discord.Client) -> int:
    return 0


async def _resolveAssetReviewDetails(
    assetId: int,
    *,
    fallbackName: str | None = None,
    fallbackCreatorId: int | None = None,
    fallbackCreatorName: str | None = None,
    fallbackItemType: str | None = None,
) -> dict[str, Any]:
    normalizedAssetId = _positiveInt(assetId)
    if normalizedAssetId <= 0:
        return {}
    priceRows, _ = await robloxAssets.fetchCatalogAssetPrices([normalizedAssetId])
    validationRows = await robloxAssets.validateRobloxAssetVisualReferences([normalizedAssetId])
    priceInfo = dict((priceRows or {}).get(normalizedAssetId) or {})
    validation = {}
    for row in list(validationRows or []):
        if _positiveInt(getattr(row, "get", lambda *_: 0)("assetId")) == normalizedAssetId:
            validation = dict(row)
            break
    return {
        "id": normalizedAssetId,
        "name": str(priceInfo.get("name") or fallbackName or f"Asset {normalizedAssetId}").strip() or f"Asset {normalizedAssetId}",
        "itemType": str(priceInfo.get("assetTypeName") or fallbackItemType or "").strip() or None,
        "creatorId": _positiveInt(priceInfo.get("creatorId")) or _positiveInt(fallbackCreatorId) or None,
        "creatorName": str(priceInfo.get("creatorName") or fallbackCreatorName or "").strip() or None,
        "priceRobux": priceInfo.get("price"),
        "thumbnailHash": str(validation.get("thumbnailHash") or "").strip(),
        "thumbnailUrl": str(validation.get("thumbnailUrl") or "").strip() or None,
        "thumbnailState": str(validation.get("thumbnailState") or "").strip() or None,
        "validationState": str(validation.get("validationState") or "").strip().upper() or "UNKNOWN",
    }


def _bgIntelFlagBasis(flaggedItem: dict[str, Any]) -> str:
    matchType = str(flaggedItem.get("matchType") or "").strip().lower()
    matchMode = str(flaggedItem.get("matchMode") or "").strip().lower()
    keyword = str(flaggedItem.get("keyword") or "").strip()
    referenceItemId = _positiveInt(flaggedItem.get("referenceItemId"))
    if matchType == "visual":
        if referenceItemId > 0:
            return f"Thumbnail similarity to previously flagged item `{referenceItemId}`."
        return "Thumbnail similarity to a previously flagged item."
    if matchType == "keyword":
        if keyword:
            if matchMode == "fuzzy":
                score = flaggedItem.get("fuzzyScore")
                try:
                    fuzzyScore = f"{float(score):.0f}"
                except (TypeError, ValueError):
                    fuzzyScore = "?"
                return f"Item name looked similar to keyword `{keyword}` ({fuzzyScore})."
            if matchMode == "normalized":
                return f"Item name normalized to match keyword `{keyword}`."
            return f"Item name matched keyword `{keyword}`."
        return "Item name matched a configured keyword."
    if matchType == "item":
        return "Exact item ID matched a configured flagged item."
    if matchType == "creator":
        return "Item creator matched a configured flagged creator."
    reason = str(flaggedItem.get("reason") or "").strip()
    return reason or "Flagged during BG intelligence inventory review."


async def _buildBgIntelDisputeContext(
    *,
    flaggedItem: dict[str, Any],
    reportId: int,
    reviewerId: int,
) -> dict[str, Any]:
    assetId = _positiveInt(flaggedItem.get("id"))
    details = await _resolveAssetReviewDetails(
        assetId,
        fallbackName=str(flaggedItem.get("name") or "").strip() or None,
        fallbackCreatorId=_positiveInt(flaggedItem.get("creatorId")) or None,
        fallbackCreatorName=str(flaggedItem.get("creatorName") or "").strip() or None,
        fallbackItemType=str(flaggedItem.get("itemType") or "").strip() or None,
    )
    matchType = str(flaggedItem.get("matchType") or "").strip().lower()
    referenceItem: dict[str, Any] | None = None
    referenceItemId = _positiveInt(flaggedItem.get("referenceItemId"))
    if matchType == "visual" and referenceItemId > 0:
        referenceDetails = await _resolveAssetReviewDetails(referenceItemId)
        if referenceDetails:
            referenceItem = {
                "id": int(referenceDetails.get("id") or 0),
                "name": referenceDetails.get("name"),
                "thumbnailUrl": referenceDetails.get("thumbnailUrl"),
                "reason": "Flagged source item used for thumbnail similarity matching.",
            }
    elif matchType == "item" and assetId > 0:
        referenceItem = {
            "id": assetId,
            "name": details.get("name") or flaggedItem.get("name") or f"Asset {assetId}",
            "thumbnailUrl": details.get("thumbnailUrl"),
            "reason": "Configured flagged item rule matched this exact asset.",
        }
    context: dict[str, Any] = {
        "kind": "bg_intel_dispute",
        "reportId": int(reportId or 0),
        "requestedByReviewerId": int(reviewerId or 0),
        "flagBasis": _bgIntelFlagBasis(flaggedItem),
        "disputedItem": {
            "id": int(details.get("id") or assetId),
            "name": details.get("name") or flaggedItem.get("name") or f"Asset {assetId}",
            "creatorId": details.get("creatorId") or _positiveInt(flaggedItem.get("creatorId")) or None,
            "creatorName": details.get("creatorName") or flaggedItem.get("creatorName"),
            "itemType": details.get("itemType") or flaggedItem.get("itemType"),
        },
    }
    if referenceItem:
        context["referenceItem"] = referenceItem
    return {
        "context": context,
        "details": details,
    }


async def queueBgIntelDisputedItem(
    botClient: discord.Client,
    *,
    guildId: int,
    reviewerId: int,
    report: Any,
    flaggedItem: dict[str, Any],
    reportId: int = 0,
) -> dict[str, Any]:
    normalizedGuildId = _positiveInt(guildId)
    if normalizedGuildId <= 0:
        return {"ok": False, "reason": "Missing guild."}
    if not _queueEnabled(normalizedGuildId):
        return {"ok": False, "reason": "Queue disabled."}
    if _queueChannelId(normalizedGuildId) <= 0:
        return {"ok": False, "reason": "No queue channel configured."}

    assetId = _positiveInt(flaggedItem.get("id"))
    if assetId <= 0:
        return {"ok": False, "reason": "Flagged item is missing an asset ID."}

    sourceUserId = _positiveInt(getattr(report, "discordUserId", 0))
    sourceRobloxUserId = _positiveInt(getattr(report, "robloxUserId", 0))
    sourceRobloxUsername = str(getattr(report, "robloxUsername", "") or "").strip() or None

    contextInfo = await _buildBgIntelDisputeContext(
        flaggedItem=flaggedItem,
        reportId=int(reportId or 0),
        reviewerId=int(reviewerId or 0),
    )
    context = dict(contextInfo.get("context") or {})
    details = dict(contextInfo.get("details") or {})
    contextJson = _contextJson(context)
    thumbnailHash = str(details.get("thumbnailHash") or "").strip()

    existing = await service.findCandidateMatch(assetId, thumbnailHash, guildId=normalizedGuildId)
    if existing is not None:
        queueId = int(existing.get("queueId") or 0)
        await service.touchQueueEntry(
            queueId,
            guildId=normalizedGuildId,
            sessionId=0,
            sourceUserId=sourceUserId,
            sourceRobloxUserId=sourceRobloxUserId or None,
            sourceRobloxUsername=sourceRobloxUsername,
            queuedByReviewerId=int(reviewerId or 0),
            contextJson=contextJson,
        )
        await service.addSourceRecord(
            queueId=queueId,
            guildId=normalizedGuildId,
            sessionId=0,
            sourceUserId=sourceUserId,
            sourceRobloxUserId=sourceRobloxUserId or None,
            sourceRobloxUsername=sourceRobloxUsername,
            queuedByReviewerId=int(reviewerId or 0),
        )
        if service.normalizeStatus(existing.get("status")) in service.FINAL_STATUSES:
            await service.reopenQueueEntry(
                queueId,
                reviewerId=int(reviewerId or 0),
                contextJson=contextJson,
            )
        else:
            await service.setQueueContext(queueId, contextJson=contextJson)
        await service.addAction(
            queueId,
            actorId=int(reviewerId or 0),
            action="DISPUTED",
            note=str(context.get("flagBasis") or "").strip() or None,
        )
        refreshed = await refreshQueueMessage(botClient, queueId)
        if not refreshed:
            queueRow = await service.getQueueEntry(queueId)
            if queueRow is not None and _positiveInt(queueRow.get("reviewMessageId")) <= 0:
                refreshed = await _postQueueMessage(botClient, queueRow)
        return {"ok": bool(refreshed), "queueId": queueId, "created": False, "reason": "" if refreshed else "Unable to post queue message."}

    queueId = await service.createQueueEntry(
        guildId=normalizedGuildId,
        sessionId=0,
        assetId=assetId,
        assetName=str(details.get("name") or flaggedItem.get("name") or f"Asset {assetId}").strip() or f"Asset {assetId}",
        itemType=str(details.get("itemType") or flaggedItem.get("itemType") or "").strip() or None,
        creatorId=_positiveInt(details.get("creatorId")) or _positiveInt(flaggedItem.get("creatorId")) or None,
        creatorName=str(details.get("creatorName") or flaggedItem.get("creatorName") or "").strip() or None,
        priceRobux=details.get("priceRobux"),
        thumbnailHash=thumbnailHash,
        thumbnailUrl=str(details.get("thumbnailUrl") or "").strip() or None,
        thumbnailState=str(details.get("thumbnailState") or "").strip() or None,
        sourceUserId=sourceUserId,
        sourceRobloxUserId=sourceRobloxUserId or None,
        sourceRobloxUsername=sourceRobloxUsername,
        queuedByReviewerId=int(reviewerId or 0),
        contextJson=contextJson,
    )
    await service.addSourceRecord(
        queueId=queueId,
        guildId=normalizedGuildId,
        sessionId=0,
        sourceUserId=sourceUserId,
        sourceRobloxUserId=sourceRobloxUserId or None,
        sourceRobloxUsername=sourceRobloxUsername,
        queuedByReviewerId=int(reviewerId or 0),
    )
    await service.addAction(
        queueId,
        actorId=int(reviewerId or 0),
        action="DISPUTED",
        note=str(context.get("flagBasis") or "").strip() or None,
    )
    queueRow = await service.getQueueEntry(queueId)
    if queueRow is None:
        return {"ok": False, "queueId": queueId, "created": True, "reason": "Queue row disappeared after creation."}
    posted = await _postQueueMessage(botClient, queueRow)
    return {"ok": bool(posted), "queueId": queueId, "created": True, "reason": "" if posted else "Unable to post queue message."}


async def queueRejectedAttendeeInventory(
    botClient: discord.Client,
    *,
    session: dict[str, Any] | None,
    attendee: dict[str, Any] | None,
    reviewerId: int,
    guild: discord.Guild | None = None,
) -> dict[str, int | str]:
    return {
        "created": 0,
        "existing": 0,
        "known": 0,
        "errors": 0,
        "reason": "Denied-row inventory item review was removed.",
    }

