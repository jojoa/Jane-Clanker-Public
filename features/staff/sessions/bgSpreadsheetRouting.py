from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import discord

import config
from features.staff.sessions import bgAddQueue
from features.staff.sessions import bgSpreadsheetQueue
from runtime import interaction as interactionRuntime
from runtime import orgProfiles
from runtime import orbatAudit as orbatAuditRuntime
from runtime import taskBudgeter

_deps: dict[str, Any] = {}
log = logging.getLogger(__name__)
_leadingHostPrefixRegex = re.compile(r"^\[[^\]]+\]\s*")
_trailingHostSuffixRegex = re.compile(r"\s*\[[^\]]+\]\s*$")


def configure(**deps: Any) -> None:
    _deps.update(deps)


def _dep(name: str) -> Any:
    value = _deps.get(name)
    if value is None:
        raise RuntimeError(f"bgSpreadsheetRouting dependency not configured: {name}")
    return value


def _bgSpreadsheetChannelIds(session: dict[str, Any]) -> list[int]:
    try:
        guildId = int(session.get("guildId") or 0)
    except (TypeError, ValueError):
        guildId = 0
    try:
        channelId = int(
            orgProfiles.getOrganizationValue(
                config,
                "bgCheckChannelId",
                guildId=guildId,
                default=getattr(config, "bgCheckChannelId", 0),
            )
            or 0
        )
    except (TypeError, ValueError):
        channelId = 0
    return [channelId] if channelId > 0 else []


def _orientationSpreadsheetForumTargets() -> dict[int, int]:
    raw = getattr(config, "orientationSpreadsheetForumTagIds", {}) or {}
    if not isinstance(raw, dict):
        return {}
    targets: dict[int, int] = {}
    for rawChannelId, rawTagId in raw.items():
        try:
            channelId = int(rawChannelId)
            tagId = int(rawTagId)
        except (TypeError, ValueError):
            continue
        if channelId > 0 and tagId > 0:
            targets[channelId] = tagId
    return targets


def _cleanOrientationHostName(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    while text.startswith("["):
        updated = _leadingHostPrefixRegex.sub("", text).strip()
        if not updated or updated == text:
            break
        text = updated
    while text.endswith("]"):
        updated = _trailingHostSuffixRegex.sub("", text).strip()
        if not updated or updated == text:
            break
        text = updated
    return " ".join(text.split()).strip()


async def _orientationHostName(
    bot: discord.Client,
    guild: discord.Guild,
    session: dict[str, Any],
) -> str:
    try:
        hostId = int(session.get("hostId") or 0)
    except (TypeError, ValueError):
        hostId = 0
    if hostId <= 0:
        return "Unknown"

    member = guild.get_member(hostId)
    if member is None:
        try:
            member = await taskBudgeter.runDiscord(lambda: guild.fetch_member(hostId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is not None:
        cleaned = _cleanOrientationHostName(member.nick or member.display_name or member.name)
        if cleaned:
            return cleaned

    user = bot.get_user(hostId)
    if user is None:
        try:
            user = await taskBudgeter.runDiscord(lambda: bot.fetch_user(hostId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            user = None
    if user is not None:
        cleaned = _cleanOrientationHostName(
            getattr(user, "display_name", None) or getattr(user, "global_name", None) or getattr(user, "name", None)
        )
        if cleaned:
            return cleaned
    return f"user-{hostId}"


def _orientationSpreadsheetDateText(result: bgSpreadsheetQueue.BgSpreadsheetResult) -> str:
    title = str(getattr(result, "title", "") or "").strip()
    if title:
        try:
            parsed = datetime.strptime(title.rsplit(" ", 1)[-1], "%Y-%m-%d")
            return f"{parsed.month}/{parsed.day}/{parsed.year}"
        except ValueError:
            pass
    now = datetime.now().astimezone()
    return f"{now.month}/{now.day}/{now.year}"


async def _postOrientationSpreadsheetForumEntries(
    bot: discord.Client,
    guild: discord.Guild,
    session: dict[str, Any],
    result: bgSpreadsheetQueue.BgSpreadsheetResult,
) -> None:
    if not result.url:
        return
    targets = _orientationSpreadsheetForumTargets()
    if not targets:
        return

    hostName = await _orientationHostName(bot, guild, session)
    dateText = _orientationSpreadsheetDateText(result)
    postTitle = f"{hostName}'s Orientation, {dateText}"[:100]
    for channelId, tagId in targets.items():
        channel = await _dep("getCachedChannel")(bot, int(channelId))
        if not isinstance(channel, discord.ForumChannel):
            continue
        selectedTags = [
            tag
            for tag in list(getattr(channel, "available_tags", []) or [])
            if int(getattr(tag, "id", 0) or 0) == int(tagId)
        ][:1]
        try:
            await taskBudgeter.runDiscord(
                lambda: channel.create_thread(
                    name=postTitle,
                    content=str(result.url).strip(),
                    applied_tags=selectedTags,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.debug(
                "Failed to create orientation spreadsheet forum post in channel %s for session %s.",
                channelId,
                session.get("sessionId"),
                exc_info=True,
            )


async def routeBgcSpreadsheet(
    bot: discord.Client,
    sessionId: int,
    guild: discord.Guild,
) -> bgSpreadsheetQueue.BgSpreadsheetResult:
    await _dep("ensureBgReviewBuckets")(bot, sessionId, guild)
    session = await _dep("service").getSession(sessionId)
    if not session:
        return bgSpreadsheetQueue.BgSpreadsheetResult(
            skipped_reason="Orientation session could not be found."
        )
    guildId = int(session.get("guildId") or getattr(guild, "id", 0) or 0)
    attendees = _dep("bgCandidates")(await _dep("service").getAttendees(sessionId))
    if not attendees:
        return bgSpreadsheetQueue.BgSpreadsheetResult(
            skipped_reason="No passing attendees need a BGC spreadsheet."
        )
    manualUserIds = await bgAddQueue.pendingUserIds(guildId=guildId)
    spreadsheetAttendees = list(attendees)
    seenUserIds = {int(attendee.get("userId") or 0) for attendee in spreadsheetAttendees}
    for userId in manualUserIds:
        if int(userId) in seenUserIds:
            continue
        seenUserIds.add(int(userId))
        spreadsheetAttendees.append({"userId": int(userId)})

    result = await bgSpreadsheetQueue.createSpreadsheetForAttendees(
        spreadsheetAttendees,
        sourceGuild=guild,
        titlePrefix="Orientation",
        guildId=guildId,
    )
    if not result.url:
        return result

    consumedManualUserIds = sorted({int(row.discord_id) for row in result.rows} & set(manualUserIds))
    if consumedManualUserIds:
        try:
            await bgAddQueue.markConsumed(
                guildId=guildId,
                userIds=consumedManualUserIds,
                sessionId=sessionId,
                spreadsheetId=result.spreadsheet_id,
            )
        except Exception:
            log.exception("Failed to consume /bg-add rows after BGC spreadsheet creation for session %s.", sessionId)

    hostId = int(session.get("hostId") or 0)
    try:
        detailParts = [f"Session: {int(sessionId)}", f"Candidates: {len(attendees)}"]
        if consumedManualUserIds:
            detailParts.append(f"Manual additions: {len(consumedManualUserIds)}")
        await bgSpreadsheetQueue.sendBgSpreadsheetChangeLog(
            bot,
            result=result,
            change="Created orientation BGC spreadsheet.",
            authorizedBy=f"<@{hostId}>" if hostId > 0 else "orientation workflow",
            requestedBy=f"<@{hostId}>" if hostId > 0 else "orientation workflow",
            requestMessageUrl=orbatAuditRuntime.buildDiscordMessageUrl(
                session.get("guildId"),
                session.get("channelId"),
                session.get("messageId"),
            ),
            details=" | ".join(detailParts),
        )
    except Exception:
        log.exception("Failed to post BGC spreadsheet audit log for orientation session %s.", sessionId)

    channelIds = sorted({int(channelId) for channelId in _bgSpreadsheetChannelIds(session) if int(channelId or 0) > 0})
    result.expected_channel_ids = list(channelIds)
    for channelId in channelIds:
        channel = await _dep("getCachedChannel")(bot, int(channelId))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            continue
        sentMessage = await interactionRuntime.safeChannelSend(
            channel,
            content=f"Orientation BGC Spreadsheet created: {result.url}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if sentMessage is None:
            continue
        result.posted_channel_ids.append(int(channelId))
    await _postOrientationSpreadsheetForumEntries(bot, guild, session, result)
    return result
