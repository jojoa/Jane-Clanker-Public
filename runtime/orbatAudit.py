from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import quote

import discord

import config
from features.staff.departmentOrbat import layouts as departmentOrbatLayouts

log = logging.getLogger(__name__)


def _safeInt(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalizeKey(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _nowUtc() -> datetime:
    return datetime.now(timezone.utc)


def _discordTimestamp(value: datetime, style: str = "f") -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:{style}>"


def _truncateFieldText(value: Any, *, limit: int = 1000) -> str:
    text = str(value or "").strip()
    if len(text) <= max(1, int(limit or 1)):
        return text
    return f"{text[: max(1, int(limit or 1)) - 3]}..."


def buildDiscordMessageUrl(
    guildId: Any,
    channelId: Any,
    messageId: Any,
) -> str:
    guildIdInt = _safeInt(guildId)
    channelIdInt = _safeInt(channelId)
    messageIdInt = _safeInt(messageId)
    if guildIdInt <= 0 or channelIdInt <= 0 or messageIdInt <= 0:
        return ""
    return f"https://discord.com/channels/{guildIdInt}/{channelIdInt}/{messageIdInt}"


def _sheetLink(spreadsheetId: str, sheetName: Optional[str]) -> str:
    base = f"https://docs.google.com/spreadsheets/d/{spreadsheetId}/edit"
    if not sheetName:
        return base
    rangeRef = quote(f"{sheetName}!A1", safe="!")
    return f"{base}#gid=0&range={rangeRef}"


def _resolveFromMultiSheetKey(sheetKey: str) -> Optional[dict[str, str]]:
    normalized = _normalizeKey(sheetKey)
    for entry in getattr(config, "multiOrbatSheets", []) or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if _normalizeKey(key) != normalized:
            continue
        spreadsheetId = str(entry.get("spreadsheetId") or "").strip()
        sheetName = str(entry.get("sheetName") or "").strip()
        if not spreadsheetId:
            return None
        return {
            "label": str(entry.get("displayName") or key or sheetName or "Sheet"),
            "spreadsheetId": spreadsheetId,
            "sheetName": sheetName,
        }
    return None


def _resolveFromDivisionKey(divisionKey: str) -> Optional[dict[str, str]]:
    target = _normalizeKey(divisionKey)
    if not target:
        return None

    for layout in departmentOrbatLayouts.loadDepartmentLayouts():
        if not isinstance(layout, dict):
            continue
        if _normalizeKey(layout.get("divisionKey")) != target:
            continue
        sheetName = str(layout.get("sheetName") or "").strip()
        spreadsheetId = str(
            layout.get("spreadsheetId")
            or getattr(config, "deptSpreadsheetId", "")
            or ""
        ).strip()
        if not spreadsheetId:
            return None
        return {
            "label": str(layout.get("displayName") or layout.get("divisionKey") or sheetName or "Sheet"),
            "spreadsheetId": spreadsheetId,
            "sheetName": sheetName,
        }
    return None


def _resolveSheetRef(
    *,
    sheetKey: Optional[str] = None,
    divisionKey: Optional[str] = None,
    spreadsheetId: Optional[str] = None,
    sheetName: Optional[str] = None,
    label: Optional[str] = None,
) -> Optional[dict[str, str]]:
    if sheetKey:
        resolved = _resolveFromMultiSheetKey(str(sheetKey))
        if resolved is not None:
            return resolved
    if divisionKey:
        resolved = _resolveFromDivisionKey(str(divisionKey))
        if resolved is not None:
            return resolved

    fallbackSpreadsheetId = str(spreadsheetId or "").strip()
    fallbackSheetName = str(sheetName or "").strip()
    if not fallbackSpreadsheetId:
        return None
    return {
        "label": str(label or fallbackSheetName or "Sheet"),
        "spreadsheetId": fallbackSpreadsheetId,
        "sheetName": fallbackSheetName,
    }


def _buildSheetLines(
    *,
    sheetRefs: Optional[list[dict[str, Any]]],
    sheetKey: Optional[str],
    divisionKey: Optional[str],
    spreadsheetId: Optional[str],
    sheetName: Optional[str],
    label: Optional[str],
) -> list[str]:
    resolvedRefs: list[dict[str, str]] = []

    if sheetRefs:
        for ref in sheetRefs:
            if not isinstance(ref, dict):
                continue
            resolved = _resolveSheetRef(
                sheetKey=ref.get("sheetKey"),
                divisionKey=ref.get("divisionKey"),
                spreadsheetId=ref.get("spreadsheetId"),
                sheetName=ref.get("sheetName"),
                label=ref.get("label"),
            )
            if resolved:
                resolvedRefs.append(resolved)

    if not resolvedRefs:
        resolved = _resolveSheetRef(
            sheetKey=sheetKey,
            divisionKey=divisionKey,
            spreadsheetId=spreadsheetId,
            sheetName=sheetName,
            label=label,
        )
        if resolved:
            resolvedRefs.append(resolved)

    if not resolvedRefs:
        return ["(sheet link unavailable)"]

    lines: list[str] = []
    seenKeys: set[str] = set()
    for ref in resolvedRefs:
        refKey = "|".join([ref.get("label", ""), ref.get("spreadsheetId", ""), ref.get("sheetName", "")])
        if refKey in seenKeys:
            continue
        seenKeys.add(refKey)
        sheetLabel = str(ref.get("label") or "Sheet").strip()
        link = _sheetLink(str(ref.get("spreadsheetId") or "").strip(), str(ref.get("sheetName") or "").strip())
        lines.append(f"[{sheetLabel}]({link})")
    return lines or ["(sheet link unavailable)"]


def _roleSyncActions(result: Mapping[str, Any], *keys: str) -> list[str]:
    labels = {
        "created": "created row",
        "moved": "moved row",
        "updated": "updated fields",
        "rankUpdated": "updated rank",
    }
    actions: list[str] = []
    for key in keys:
        if result.get(key):
            actions.append(labels.get(key, key))
    return actions


def buildRoleSyncChangeLog(
    *,
    memberMention: str,
    result: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    syncType = str(result.get("syncType") or "").strip().lower()
    memberText = str(memberMention or "").strip() or "Unknown user"
    robloxUsername = str(result.get("robloxUsername") or "").strip() or "Unknown"

    if syncType == "recruitment.anrorsplacement":
        actions = _roleSyncActions(result, "created", "moved", "updated")
        if not actions:
            return None
        detailsParts = [
            f"User: {memberText}",
            f"Roblox: {robloxUsername}",
            f"Actions: {', '.join(actions)}",
        ]
        sectionName = str(result.get("section") or "").strip()
        if sectionName:
            detailsParts.append(f"Section: {sectionName}")
        roleFlags: list[str] = []
        if result.get("hasAnrorsMemberRole"):
            roleFlags.append("member role")
        if result.get("hasAnrorsRmPlusRole"):
            roleFlags.append("RM+ role")
        if roleFlags:
            detailsParts.append(f"Role state: {', '.join(roleFlags)}")
        return {
            "change": "Updated Recruitment ORBAT from Discord role sync.",
            "requestedBy": "automatic role sync",
            "authorizedBy": "Discord role sync",
            "details": " | ".join(detailsParts),
            "sheetKey": "recruitment",
        }

    if syncType == "department.anrdrankbyrole":
        actions = _roleSyncActions(result, "created", "rankUpdated")
        if not actions:
            return None
        divisionKey = str(result.get("divisionKey") or "").strip() or "Department"
        detailsParts = [
            f"User: {memberText}",
            f"Roblox: {robloxUsername}",
            f"Division: {divisionKey}",
            f"Actions: {', '.join(actions)}",
        ]
        targetRank = str(result.get("targetRank") or "").strip()
        if targetRank:
            detailsParts.append(f"Target rank: {targetRank}")
        previousRank = str(result.get("previousRank") or "").strip()
        if previousRank:
            detailsParts.append(f"Previous rank: {previousRank}")
        matchedRoleId = _safeInt(result.get("matchedRoleId"))
        if matchedRoleId > 0:
            detailsParts.append(f"Trigger role: <@&{matchedRoleId}>")
        return {
            "change": f"Updated {divisionKey} Department ORBAT from Discord role sync.",
            "requestedBy": "automatic role sync",
            "authorizedBy": "Discord role sync",
            "details": " | ".join(detailsParts),
            "sheetKey": str(result.get("sheetKey") or "").strip() or None,
            "divisionKey": divisionKey,
        }

    return None


async def sendOrbatChangeLog(
    botClient: discord.Client,
    *,
    change: str,
    authorizedBy: str,
    requestedBy: Optional[str] = None,
    requestMessageUrl: Optional[str] = None,
    details: Optional[str] = None,
    sheetKey: Optional[str] = None,
    divisionKey: Optional[str] = None,
    spreadsheetId: Optional[str] = None,
    sheetName: Optional[str] = None,
    label: Optional[str] = None,
    sheetRefs: Optional[list[dict[str, Any]]] = None,
    occurredAt: Optional[datetime] = None,
    title: str = "ORBAT Change",
) -> None:
    channelId = _safeInt(getattr(config, "orbatAuditChannelId", 0))
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

    now = occurredAt or _nowUtc()
    changeText = _truncateFieldText(change, limit=1000) or "Unknown"
    authorizedText = _truncateFieldText(authorizedBy, limit=1000) or "Unknown"
    requestedText = _truncateFieldText(requestedBy or authorizedText, limit=1000) or "system"
    requestUrl = str(requestMessageUrl or "").strip()
    requestValue = f"[Open message]({requestUrl})" if requestUrl else "N/A"
    embed = discord.Embed(
        title=str(title or "Spreadsheet Change").strip() or "Spreadsheet Change",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    embed.add_field(name="Change", value=changeText, inline=False)
    embed.add_field(name="Requested By", value=requestedText, inline=False)
    embed.add_field(name="Authorized By", value=authorizedText, inline=False)
    embed.add_field(name="Request Message", value=requestValue, inline=False)
    embed.add_field(name="Time", value=_discordTimestamp(now, "f"), inline=False)
    sheetLines = _buildSheetLines(
        sheetRefs=sheetRefs,
        sheetKey=sheetKey,
        divisionKey=divisionKey,
        spreadsheetId=spreadsheetId,
        sheetName=sheetName,
        label=label,
    )
    embed.add_field(name="Sheet", value="\n".join(sheetLines), inline=False)
    if details:
        detailText = _truncateFieldText(details, limit=1000)
        embed.add_field(name="Details", value=detailText, inline=False)

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.exception("Failed to post ORBAT audit log.")

