from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import discord

from db.sqlite import executeReturnId, fetchAll

log = logging.getLogger(__name__)


def _safeInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed


def _jsonText(value: object) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "{}"
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        return "{}"


def _nowIso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditStream:
    def __init__(
        self,
        *,
        botClient: Any,
        configModule: Any,
        taskBudgeter: Any,
    ) -> None:
        self.botClient = botClient
        self.config = configModule
        self.taskBudgeter = taskBudgeter

    async def _runBackgroundDiscord(self, opFactory: Any) -> Any:
        runner = getattr(self.taskBudgeter, "runLowPriorityDiscord", None)
        if callable(runner):
            return await runner(opFactory)
        return await self.taskBudgeter.runDiscord(opFactory)

    def _auditChannelId(self) -> int:
        preferred = _safeInt(getattr(self.config, "auditLogChannelId", 0))
        if preferred > 0:
            return preferred
        fallback = _safeInt(getattr(self.config, "orbatAuditChannelId", 0))
        return fallback if fallback > 0 else 0

    async def logEvent(
        self,
        *,
        source: str,
        action: str,
        guildId: int = 0,
        actorId: int = 0,
        targetType: str = "",
        targetId: str = "",
        severity: str = "INFO",
        details: object = None,
        authorizedBy: str = "",
        postToDiscord: bool = False,
    ) -> int:
        safeSource = str(source or "runtime").strip()[:80]
        safeAction = str(action or "event").strip()[:160]
        safeTargetType = str(targetType or "").strip()[:80]
        safeTargetId = str(targetId or "").strip()[:160]
        safeSeverity = str(severity or "INFO").strip().upper()[:16]
        safeAuthorizedBy = str(authorizedBy or "").strip()[:160]
        safeDetails = _jsonText(details)

        eventId = await executeReturnId(
            """
            INSERT INTO audit_events (
                guildId, actorId, source, action, targetType, targetId,
                severity, detailsJson, authorizedBy, createdAt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guildId or 0),
                int(actorId or 0),
                safeSource,
                safeAction,
                safeTargetType,
                safeTargetId,
                safeSeverity,
                safeDetails,
                safeAuthorizedBy,
                _nowIso(),
            ),
        )

        if postToDiscord:
            try:
                await self._postDiscordEvent(
                    eventId=eventId,
                    source=safeSource,
                    action=safeAction,
                    guildId=int(guildId or 0),
                    actorId=int(actorId or 0),
                    targetType=safeTargetType,
                    targetId=safeTargetId,
                    severity=safeSeverity,
                    detailsJson=safeDetails,
                    authorizedBy=safeAuthorizedBy,
                )
            except Exception:
                log.exception("Failed to post audit event to Discord (eventId=%s).", eventId)

        return eventId

    async def tail(
        self,
        *,
        limit: int = 25,
        guildId: int = 0,
    ) -> list[dict[str, Any]]:
        safeLimit = max(1, min(100, int(limit or 25)))
        if guildId > 0:
            return await fetchAll(
                """
                SELECT eventId, guildId, actorId, source, action, targetType, targetId,
                       severity, detailsJson, authorizedBy, createdAt
                FROM audit_events
                WHERE guildId = ?
                ORDER BY eventId DESC
                LIMIT ?
                """,
                (int(guildId), safeLimit),
            )
        return await fetchAll(
            """
            SELECT eventId, guildId, actorId, source, action, targetType, targetId,
                   severity, detailsJson, authorizedBy, createdAt
            FROM audit_events
            ORDER BY eventId DESC
            LIMIT ?
            """,
            (safeLimit,),
        )

    async def listEvents(
        self,
        *,
        limit: int = 500,
        guildId: int = 0,
    ) -> list[dict[str, Any]]:
        safeLimit = max(1, min(5000, int(limit or 500)))
        if guildId > 0:
            return await fetchAll(
                """
                SELECT eventId, guildId, actorId, source, action, targetType, targetId,
                       severity, detailsJson, authorizedBy, createdAt
                FROM audit_events
                WHERE guildId = ?
                ORDER BY eventId DESC
                LIMIT ?
                """,
                (int(guildId), safeLimit),
            )
        return await fetchAll(
            """
            SELECT eventId, guildId, actorId, source, action, targetType, targetId,
                   severity, detailsJson, authorizedBy, createdAt
            FROM audit_events
            ORDER BY eventId DESC
            LIMIT ?
            """,
            (safeLimit,),
        )

    async def _postDiscordEvent(
        self,
        *,
        eventId: int,
        source: str,
        action: str,
        guildId: int,
        actorId: int,
        targetType: str,
        targetId: str,
        severity: str,
        detailsJson: str,
        authorizedBy: str,
    ) -> None:
        channelId = self._auditChannelId()
        if channelId <= 0:
            return

        channel = self.botClient.get_channel(channelId)
        if channel is None:
            try:
                channel = await self._runBackgroundDiscord(lambda: self.botClient.fetch_channel(channelId))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, discord.InvalidData):
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        color = discord.Color.blurple()
        if severity == "WARN":
            color = discord.Color.orange()
        elif severity in {"ERROR", "CRITICAL"}:
            color = discord.Color.red()

        embed = discord.Embed(
            title=f"Audit Event #{eventId}",
            color=color,
            timestamp=datetime.now(timezone.utc),
            description=f"`{source}` - {action}",
        )
        if guildId > 0:
            embed.add_field(name="Guild", value=str(guildId), inline=True)
        if actorId > 0:
            embed.add_field(name="Actor", value=f"<@{actorId}> (`{actorId}`)", inline=True)
        embed.add_field(name="Severity", value=severity or "INFO", inline=True)
        if targetType or targetId:
            embed.add_field(
                name="Target",
                value=f"type: `{targetType or 'n/a'}`\nid: `{targetId or 'n/a'}`",
                inline=False,
            )
        if authorizedBy:
            embed.add_field(name="Authorized By", value=authorizedBy, inline=False)
        detailsText = detailsJson or "{}"
        if len(detailsText) > 1000:
            detailsText = f"{detailsText[:997]}..."
        embed.add_field(name="Details", value=f"```json\n{detailsText}\n```", inline=False)

        await self._runBackgroundDiscord(lambda: channel.send(embed=embed))
