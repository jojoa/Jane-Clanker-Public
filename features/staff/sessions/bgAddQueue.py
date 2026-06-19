from __future__ import annotations

from typing import Any, Iterable

from db.sqlite import execute, fetchAll, fetchOne


def _positiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _uniqueUserIds(userIds: Iterable[object]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for rawUserId in userIds or []:
        userId = _positiveInt(rawUserId)
        if userId <= 0 or userId in seen:
            continue
        seen.add(userId)
        out.append(userId)
    return out


async def addPendingUser(
    *,
    guildId: int,
    userId: int,
    addedBy: int,
    note: str = "",
) -> dict[str, Any]:
    safeGuildId = _positiveInt(guildId)
    safeUserId = _positiveInt(userId)
    if safeGuildId <= 0:
        raise ValueError("guildId is required")
    if safeUserId <= 0:
        raise ValueError("userId is required")

    existing = await fetchOne(
        """
        SELECT userId
        FROM bg_spreadsheet_additions
        WHERE guildId = ?
          AND userId = ?
          AND consumedAt IS NULL
        """,
        (safeGuildId, safeUserId),
    )
    await execute(
        """
        INSERT INTO bg_spreadsheet_additions (
            guildId, userId, addedBy, note, createdAt, consumedAt, consumedSessionId, consumedSpreadsheetId
        )
        VALUES (?, ?, ?, ?, datetime('now'), NULL, NULL, '')
        ON CONFLICT(guildId, userId) DO UPDATE SET
            addedBy = excluded.addedBy,
            note = excluded.note,
            createdAt = datetime('now'),
            consumedAt = NULL,
            consumedSessionId = NULL,
            consumedSpreadsheetId = ''
        """,
        (
            safeGuildId,
            safeUserId,
            _positiveInt(addedBy),
            str(note or "").strip()[:500],
        ),
    )
    return {
        "created": existing is None,
        "pendingCount": await pendingCount(guildId=safeGuildId),
    }


async def pendingCount(*, guildId: int) -> int:
    safeGuildId = _positiveInt(guildId)
    if safeGuildId <= 0:
        return 0
    row = await fetchOne(
        """
        SELECT COUNT(*) AS pendingCount
        FROM bg_spreadsheet_additions
        WHERE guildId = ?
          AND consumedAt IS NULL
        """,
        (safeGuildId,),
    )
    return _positiveInt((row or {}).get("pendingCount"))


async def listPendingUsers(*, guildId: int) -> list[dict[str, Any]]:
    safeGuildId = _positiveInt(guildId)
    if safeGuildId <= 0:
        return []
    return await fetchAll(
        """
        SELECT guildId, userId, addedBy, note, createdAt
        FROM bg_spreadsheet_additions
        WHERE guildId = ?
          AND consumedAt IS NULL
        ORDER BY datetime(createdAt) ASC, userId ASC
        """,
        (safeGuildId,),
    )


async def pendingUserIds(*, guildId: int) -> list[int]:
    rows = await listPendingUsers(guildId=guildId)
    return _uniqueUserIds(row.get("userId") for row in rows)


async def markConsumed(
    *,
    guildId: int,
    userIds: Iterable[object],
    sessionId: int,
    spreadsheetId: str,
) -> int:
    safeGuildId = _positiveInt(guildId)
    safeUserIds = _uniqueUserIds(userIds)
    if safeGuildId <= 0 or not safeUserIds:
        return 0
    placeholders = ",".join("?" for _ in safeUserIds)
    await execute(
        f"""
        UPDATE bg_spreadsheet_additions
        SET consumedAt = datetime('now'),
            consumedSessionId = ?,
            consumedSpreadsheetId = ?
        WHERE guildId = ?
          AND consumedAt IS NULL
          AND userId IN ({placeholders})
        """,
        (
            _positiveInt(sessionId),
            str(spreadsheetId or "").strip()[:120],
            safeGuildId,
            *safeUserIds,
        ),
    )
    return len(safeUserIds)
