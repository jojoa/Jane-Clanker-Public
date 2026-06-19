from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import config
from features.staff.sessions import bgSpreadsheetQueue, service
from features.staff.sessions.Roblox import robloxUsers
from runtime import taskBudgeter

log = logging.getLogger(__name__)

_activeStatuses = {"OPEN", "FULL", "GRADING"}
_warmupTasks: dict[int, asyncio.Task[None]] = {}
_warmedUsersBySession: dict[int, set[int]] = {}
_lookupSemaphore = asyncio.Semaphore(1)


def _positiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _enabled() -> bool:
    return bool(getattr(config, "orientationRoverWarmupEnabled", True))


def _secondsConfig(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        raw = getattr(config, name, default)
        value = int(default if raw is None else raw)
    except (TypeError, ValueError):
        value = default
    return max(int(minimum), value)


def _parseDatetime(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    for parser in (
        datetime.fromisoformat,
        lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _isActiveOrientationSession(session: Optional[dict[str, Any]]) -> bool:
    if not session:
        return False
    sessionType = str(session.get("sessionType") or "").strip().lower()
    status = str(session.get("status") or "").strip().upper()
    return sessionType == "orientation" and status in _activeStatuses


def _initialDelaySec(session: dict[str, Any]) -> int:
    delaySec = _secondsConfig("orientationRoverWarmupDelaySec", 600)
    createdAt = _parseDatetime(session.get("createdAt"))
    if createdAt is None:
        return delaySec
    elapsedSec = (datetime.utcnow() - createdAt).total_seconds()
    return max(0, int(delaySec - elapsedSec))


async def _hasReusableIdentity(attendee: dict[str, Any]) -> bool:
    userId = _positiveInt(attendee.get("userId"))
    if userId <= 0:
        return True
    attendeeRobloxId = _positiveInt(attendee.get("robloxUserId"))
    attendeeRobloxUsername = str(attendee.get("robloxUsername") or "").strip()
    if attendeeRobloxId > 0 and attendeeRobloxUsername:
        return True
    try:
        stored = await robloxUsers.getStoredRobloxIdentity(userId)
    except Exception:
        log.exception("Failed to read stored Roblox identity for warmup user %s.", userId)
        return False
    if stored is None:
        return False
    return _positiveInt(stored.robloxId) > 0 and bool(str(stored.robloxUsername or "").strip())


async def _warmupUser(userId: int, guildIds: list[int]) -> None:
    async with _lookupSemaphore:
        await taskBudgeter.runLowPriorityRoblox(
            lambda: bgSpreadsheetQueue.fetchRobloxUserWithFallbacks(int(userId), guildIds)
        )


async def warmupOrientationRoverLookupsOnce(
    bot: discord.Client,
    sessionId: int,
) -> int:
    del bot
    normalizedSessionId = _positiveInt(sessionId)
    if normalizedSessionId <= 0:
        return 0

    session = await service.getSession(normalizedSessionId)
    if not _isActiveOrientationSession(session):
        _warmedUsersBySession.pop(normalizedSessionId, None)
        return 0

    attendees = await service.getAttendees(normalizedSessionId)
    if not attendees:
        return 0

    sourceGuildId = _positiveInt(session.get("guildId") if session else 0)
    guildIds = bgSpreadsheetQueue.roverLookupGuildIds(
        sourceGuildId=sourceGuildId,
        guildId=sourceGuildId,
    )
    warmedUsers = _warmedUsersBySession.setdefault(normalizedSessionId, set())
    attempted = 0

    for attendee in attendees:
        row = dict(attendee or {})
        userId = _positiveInt(row.get("userId"))
        if userId <= 0 or userId in warmedUsers:
            continue
        if await _hasReusableIdentity(row):
            warmedUsers.add(userId)
            continue
        try:
            await _warmupUser(userId, guildIds)
            warmedUsers.add(userId)
            attempted += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Failed low-priority orientation RoVer warmup for session %s user %s.",
                normalizedSessionId,
                userId,
            )
        await asyncio.sleep(0)

    return attempted


async def _warmupLoop(bot: discord.Client, sessionId: int) -> None:
    session = await service.getSession(sessionId)
    if not _isActiveOrientationSession(session):
        return

    delaySec = _initialDelaySec(session)
    if delaySec > 0:
        await asyncio.sleep(delaySec)

    while True:
        session = await service.getSession(sessionId)
        if not _isActiveOrientationSession(session):
            _warmedUsersBySession.pop(sessionId, None)
            return
        await warmupOrientationRoverLookupsOnce(bot, sessionId)
        intervalSec = _secondsConfig("orientationRoverWarmupIntervalSec", 60)
        if intervalSec <= 0:
            return
        await asyncio.sleep(intervalSec)


def _taskDone(sessionId: int, task: asyncio.Task[None]) -> None:
    current = _warmupTasks.get(sessionId)
    if current is task:
        _warmupTasks.pop(sessionId, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "Orientation RoVer warmup task failed for session %s.",
            sessionId,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def scheduleOrientationRoverWarmup(bot: discord.Client, sessionId: int) -> bool:
    if not _enabled():
        return False
    normalizedSessionId = _positiveInt(sessionId)
    if normalizedSessionId <= 0:
        return False
    existing = _warmupTasks.get(normalizedSessionId)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(
        _warmupLoop(bot, normalizedSessionId),
        name=f"orientation-rover-warmup:{normalizedSessionId}",
    )
    _warmupTasks[normalizedSessionId] = task
    task.add_done_callback(lambda finished: _taskDone(normalizedSessionId, finished))
    return True
