from __future__ import annotations

from datetime import datetime
import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import discord

import config
from features.staff.honorGuard import sheets as honorGuardSheets
from runtime import normalization
from runtime import orbatAudit as orbatAuditRuntime


log = logging.getLogger(__name__)

def _safeInt(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

@dataclass(frozen=True)
class ResolvedSheetLogEntry:
    discordUserId: int
    quotaDelta: float = 0
    pointsDelta: float = 0


SheetUpdateBuilder = Callable[[Sequence[ResolvedSheetLogEntry]], list[dict[str, Any]]]
SheetWriter = Callable[[list[dict[str, Any]], bool], dict[str, Any]]


def buildHonorGuardSheetUpdates(
    entries: Sequence[ResolvedSheetLogEntry],
) -> list[dict[str, Any]]:
    return [
        {
            "discordUserId": int(entry.discordUserId),
            "quotaDelta": float(entry.quotaDelta),
            "pointsDelta": float(entry.pointsDelta),
        }
        for entry in entries
    ]


async def syncApprovedLogsToSheet(
    discordUserIds: Sequence[int],
    quotaDelta: int,
    pointsDelta: int,
    *,
    organizeAfter: bool = True,
    updateBuilder: SheetUpdateBuilder = buildHonorGuardSheetUpdates,
    sheetWriter: SheetWriter | None = None,
    sheetConfigured: bool | None = None,
) -> dict[str, Any]:
    entries = [
        ResolvedSheetLogEntry(
            discordUserId=userId,
            quotaDelta=float(quotaDelta),
            pointsDelta=float(pointsDelta),
        )
        for userId in normalization.normalizeIntList(discordUserIds)
    ]
    updates = updateBuilder(entries)
    if sheetWriter is not None:
        return sheetWriter(updates, organizeAfter)
    if sheetConfigured is False:
        return {"updatedUsers": 0, "updatedRows": 0, "organized": 0, "error": "sheet-disabled"}

    updatedRows = 0
    failures: list[int] = []
    for entry in entries:
        try:
            honorGuardSheets.applyMemberPointDeltas(
                discordId=int(entry.discordUserId),
                quotaDelta=float(entry.quotaDelta),
                eventDelta=float(entry.pointsDelta),
            )
            updatedRows += 1
        except Exception:
            failures.append(int(entry.discordUserId))
            log.exception("Honor-Guard sheet sync failed for discord user %s", int(entry.discordUserId))
    result: dict[str, Any] = {
        "updatedUsers": updatedRows,
        "updatedRows": updatedRows,
        "organized": 0,
    }
    if failures:
        result["failedUserIds"] = failures
    return result


async def sendHonorGuardSheetChangeLog(
    botClient: discord.Client,
    *,
    reviewerId: int,
    requestedBy: str = "",
    requestMessageUrl: str = "",
    change: str,
    details: str,
    sheetKey: str = "honorGuard_members",
) -> None:
    try:
        reviewerText = f"<@{int(reviewerId)}>" if int(reviewerId or 0) > 0 else "system"
        await orbatAuditRuntime.sendOrbatChangeLog(
            botClient,
            change=change,
            requestedBy=str(requestedBy or "").strip() or reviewerText,
            authorizedBy=reviewerText,
            requestMessageUrl=str(requestMessageUrl or "").strip(),
            details=details,
            sheetKey=sheetKey,
        )
    except Exception:
        log.exception("Failed to post Honor-Guard ORBAT audit log.")

async def sendHonorGuardSheetAudit(
    botClient: discord.Client,
    *,
    reviewerId: int,
    requestedBy: str = "",
    requestMessageUrl: str = "",
    change: str,
    details: str,
    auditLogs: list[str],
) -> None:
    try:
        reviewerText = f"<@{int(reviewerId)}>" if int(reviewerId or 0) > 0 else "system"
        channelId = _safeInt(getattr(config, "honorGuardOrbatAuditChannelId", 0))
        if channelId <= 0:
            return

        channel = botClient.get_channel(channelId)
        if channel is None:
            try:
                channel = await botClient.fetch_channel(channelId)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, discord.InvalidData):
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        now = datetime.now()
        changeText = orbatAuditRuntime._truncateFieldText(change, limit=1000) or "Unknown"
        authorizedText = orbatAuditRuntime._truncateFieldText(reviewerText, limit=1000) or "Unknown"
        requestedText = orbatAuditRuntime._truncateFieldText(requestedBy or authorizedText, limit=1000) or "system"
        requestUrl = str(requestMessageUrl or "").strip()
        requestValue = f"[Open message]({requestUrl})" if requestUrl else "N/A"
        embed = discord.Embed(
            title=str("ORBAT Change").strip() or "Spreadsheet Change",
            color=discord.Color.blurple(),
            timestamp=now,
        )
        embed.add_field(name="Change", value=changeText, inline=False)
        embed.add_field(name="Requested By", value=requestedText, inline=False)
        embed.add_field(name="Authorized By", value=authorizedText, inline=False)
        embed.add_field(name="Request Message", value=requestValue, inline=False)
        embed.add_field(name="Time", value=orbatAuditRuntime._discordTimestamp(now, "f"), inline=False)
        if details:
            detailText = orbatAuditRuntime._truncateFieldText(details, limit=1000)
            embed.add_field(name="Details", value=detailText, inline=False)

        embed.add_field(name="Changes", value="\n".join(auditLogs), inline=False)

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception("Failed to post ORBAT audit log.")


    except Exception:
        log.exception("Failed to post Honor-Guard ORBAT audit."
)
