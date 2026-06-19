from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from db.sqlite import fetchAll
from features.community.events import service as eventService
from features.staff.applications import service as applicationsService

log = logging.getLogger(__name__)


def _nowUtc() -> datetime:
    return datetime.now(timezone.utc)


class WebhookHealthWatcher:
    def __init__(
        self,
        *,
        botClient: Any,
        taskBudgeter: Any,
        auditStream: Any,
        checkIntervalSec: int = 600,
        initialDelaySec: int = 180,
        maxRowsPerRun: int = 50,
    ) -> None:
        self.botClient = botClient
        self.taskBudgeter = taskBudgeter
        self.auditStream = auditStream
        self.checkIntervalSec = max(120, int(checkIntervalSec))
        self.initialDelaySec = max(0, int(initialDelaySec))
        self.maxRowsPerRun = max(1, int(maxRowsPerRun))
        self.workerTask: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._lastRunAt: datetime | None = None
        self._lastSummary: dict[str, int] = {
            "checked": 0,
            "missing": 0,
            "errors": 0,
        }
        self._missingDedupUntil: dict[str, datetime] = {}
        self._dedupTtl = timedelta(hours=2)

    async def _runDiscordBackground(self, opFactory: Any) -> Any:
        runner = getattr(self.taskBudgeter, "runLowPriorityDiscord", None)
        if callable(runner):
            return await runner(opFactory)
        return await self.taskBudgeter.runDiscord(opFactory)

    async def _resolveChannel(self, channelId: int) -> Any | None:
        channel = self.botClient.get_channel(int(channelId))
        if channel is not None:
            return channel
        try:
            channel = await self._runDiscordBackground(lambda: self.botClient.fetch_channel(int(channelId)))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, discord.InvalidData):
            return None
        return channel

    async def _messageState(self, *, channelId: int, messageId: int) -> str:
        channel = self.botClient.get_channel(int(channelId))
        if channel is None:
            try:
                channel = await self._runDiscordBackground(
                    lambda: self.botClient.fetch_channel(int(channelId))
                )
            except discord.NotFound:
                return "missing"
            except (discord.Forbidden, discord.HTTPException, discord.InvalidData):
                return "inaccessible"
        if channel is None or not hasattr(channel, "fetch_message"):
            return "missing"
        try:
            await self._runDiscordBackground(lambda: channel.fetch_message(int(messageId)))
            return "ok"
        except discord.NotFound:
            return "missing"
        except (discord.Forbidden, discord.HTTPException):
            return "inaccessible"

    async def _cleanupMissingDivisionHubMessage(self, *, messageId: int) -> bool:
        try:
            await applicationsService.deleteHubMessage(int(messageId))
            return True
        except Exception:
            log.exception(
                "Webhook health cleanup failed for missing application hub message %s.",
                messageId,
            )
            return False

    async def _cleanupMissingScheduledEvent(self, *, eventId: int) -> bool:
        try:
            await eventService.markScheduledEventDeleted(int(eventId))
            return True
        except Exception:
            log.exception(
                "Webhook health cleanup failed for missing scheduled event %s.",
                eventId,
            )
            return False

    def _dedupKey(self, source: str, itemId: int, *, dedupToken: str | None = None) -> str:
        cleanToken = str(dedupToken or "").strip()
        if cleanToken:
            return f"{source}:{cleanToken}"
        return f"{source}:{int(itemId)}"

    async def _logMissing(
        self,
        *,
        source: str,
        guildId: int,
        itemId: int,
        message: str,
        details: dict[str, Any],
        severity: str = "WARN",
        dedupToken: str | None = None,
    ) -> None:
        key = self._dedupKey(source, itemId, dedupToken=dedupToken)
        now = _nowUtc()
        cutoff = self._missingDedupUntil.get(key)
        if cutoff is not None and now < cutoff:
            return
        self._missingDedupUntil[key] = now + self._dedupTtl
        await self.auditStream.logEvent(
            source="webhook-health",
            action=message,
            guildId=int(guildId or 0),
            targetType=source,
            targetId=str(itemId),
            severity=str(severity or "WARN").strip().upper() or "WARN",
            details=details,
            authorizedBy="automatic watcher",
            postToDiscord=True,
        )

    async def runCheck(self) -> dict[str, int]:
        summary = {"checked": 0, "missing": 0, "errors": 0}

        # Applications hubs
        hubRows = await fetchAll(
            """
            SELECT messageId, guildId, channelId, divisionKey
            FROM division_hub_messages
            ORDER BY messageId ASC
            LIMIT ?
            """,
            (self.maxRowsPerRun,),
        )
        for row in hubRows:
            summary["checked"] += 1
            messageId = int(row.get("messageId") or 0)
            channelId = int(row.get("channelId") or 0)
            guildId = int(row.get("guildId") or 0)
            if messageId <= 0 or channelId <= 0:
                continue
            try:
                state = await self._messageState(channelId=channelId, messageId=messageId)
                if state == "ok":
                    continue
                summary["missing"] += 1
                if state == "missing":
                    cleaned = await self._cleanupMissingDivisionHubMessage(messageId=messageId)
                    divisionKey = str(row.get("divisionKey") or "").strip().lower()
                    await self._logMissing(
                        source="division_hub_message",
                        guildId=guildId,
                        itemId=messageId,
                        message="Application hub message reference cleaned" if cleaned else "Application hub message missing",
                        details={
                            "channelId": channelId,
                            "divisionKey": divisionKey,
                            "cleanupApplied": bool(cleaned),
                        },
                        severity="INFO" if cleaned else "WARN",
                        dedupToken=(
                            f"cleanup:{guildId}:{channelId}:{divisionKey}"
                            if cleaned and divisionKey
                            else None
                        ),
                    )
                    continue
                await self._logMissing(
                    source="division_hub_message",
                    guildId=guildId,
                    itemId=messageId,
                    message="Application hub message inaccessible",
                    details={
                        "channelId": channelId,
                        "divisionKey": str(row.get("divisionKey") or ""),
                    },
                )
            except Exception:
                summary["errors"] += 1
                log.exception("Webhook health check failed for application hub message %s.", messageId)

        remainingRows = max(0, self.maxRowsPerRun - int(summary["checked"]))
        if remainingRows <= 0:
            self._lastRunAt = _nowUtc()
            self._lastSummary = dict(summary)
            return summary

        # Scheduled events posts
        eventRows = await fetchAll(
            """
            SELECT eventId, guildId, channelId, messageId, title
            FROM scheduled_events
            WHERE status = 'ACTIVE' AND messageId > 0
            ORDER BY eventId ASC
            LIMIT ?
            """,
            (remainingRows,),
        )
        for row in eventRows:
            summary["checked"] += 1
            eventId = int(row.get("eventId") or 0)
            channelId = int(row.get("channelId") or 0)
            messageId = int(row.get("messageId") or 0)
            guildId = int(row.get("guildId") or 0)
            if messageId <= 0 or channelId <= 0:
                continue
            try:
                state = await self._messageState(channelId=channelId, messageId=messageId)
                if state == "ok":
                    continue
                summary["missing"] += 1
                if state == "missing":
                    cleaned = await self._cleanupMissingScheduledEvent(eventId=eventId)
                    await self._logMissing(
                        source="scheduled_event",
                        guildId=guildId,
                        itemId=eventId,
                        message="Scheduled event reference cleaned" if cleaned else "Scheduled event message missing",
                        details={
                            "channelId": channelId,
                            "messageId": messageId,
                            "title": str(row.get("title") or ""),
                            "cleanupApplied": bool(cleaned),
                        },
                        severity="INFO" if cleaned else "WARN",
                    )
                    continue
                await self._logMissing(
                    source="scheduled_event",
                    guildId=guildId,
                    itemId=eventId,
                    message="Scheduled event message inaccessible",
                    details={
                        "channelId": channelId,
                        "messageId": messageId,
                        "title": str(row.get("title") or ""),
                    },
                )
            except Exception:
                summary["errors"] += 1
                log.exception("Webhook health check failed for scheduled event %s.", eventId)

        self._lastRunAt = _nowUtc()
        self._lastSummary = dict(summary)
        return summary

    def getStats(self) -> dict[str, Any]:
        return {
            "lastRunAt": self._lastRunAt.isoformat() if self._lastRunAt else "",
            "summary": dict(self._lastSummary),
            "workerRunning": bool(self.workerTask and not self.workerTask.done()),
        }

    async def _runLoop(self) -> None:
        waitUntilReady = getattr(self.botClient, "wait_until_ready", None)
        if callable(waitUntilReady):
            await waitUntilReady()
        if self.initialDelaySec > 0:
            await asyncio.sleep(self.initialDelaySec)
        while True:
            try:
                async with self._lock:
                    await self.runCheck()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Webhook health watcher loop error.")
            await asyncio.sleep(self.checkIntervalSec)

    def start(self) -> None:
        if self.workerTask is not None and not self.workerTask.done():
            return
        self.workerTask = asyncio.create_task(self._runLoop())

    async def stop(self) -> None:
        if self.workerTask is None:
            return
        task = self.workerTask
        self.workerTask = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
