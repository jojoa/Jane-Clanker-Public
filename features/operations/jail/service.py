from __future__ import annotations

import json
from typing import Optional

from db.sqlite import execute, fetchOne, runWriteTransaction


def _jsonList(values: list[int]) -> str:
    out: list[int] = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            out.append(parsed)
    return json.dumps(out)


async def closeActiveJailRecord(guildId: int, userId: int) -> None:
    await execute(
        """
        UPDATE jail_records
        SET status = 'REPLACED',
            updatedAt = datetime('now')
        WHERE guildId = ? AND userId = ? AND status = 'ACTIVE'
        """,
        (int(guildId), int(userId)),
    )


async def createJailRecord(
    *,
    guildId: int,
    userId: int,
    jailedBy: int,
    jailedRoleId: int,
    jailChannelId: int | None,
    savedRoleIds: list[int],
    unmanageableRoleIds: list[int],
    isolatedChannelIds: list[int],
) -> int:
    async def _tx(db) -> int:
        await db.execute(
            """
            UPDATE jail_records
            SET status = 'REPLACED',
                updatedAt = datetime('now')
            WHERE guildId = ? AND userId = ? AND status = 'ACTIVE'
            """,
            (int(guildId), int(userId)),
        )
        cur = await db.execute(
            """
            INSERT INTO jail_records
                (guildId, userId, jailedBy, jailedRoleId, jailChannelId, savedRoleIdsJson, unmanageableRoleIdsJson, isolatedChannelIdsJson, status, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', datetime('now'), datetime('now'))
            """,
            (
                int(guildId),
                int(userId),
                int(jailedBy),
                int(jailedRoleId),
                int(jailChannelId) if jailChannelId else None,
                _jsonList(savedRoleIds),
                _jsonList(unmanageableRoleIds),
                _jsonList(isolatedChannelIds),
            ),
        )
        return int(cur.lastrowid or 0)

    return await runWriteTransaction(_tx)


async def getActiveJailRecord(guildId: int, userId: int) -> Optional[dict]:
    return await fetchOne(
        """
        SELECT *
        FROM jail_records
        WHERE guildId = ? AND userId = ? AND status = 'ACTIVE'
        ORDER BY recordId DESC
        LIMIT 1
        """,
        (int(guildId), int(userId)),
    )


async def releaseActiveJailRecord(
    *,
    guildId: int,
    userId: int,
    releasedBy: int,
) -> None:
    await execute(
        """
        UPDATE jail_records
        SET status = 'RELEASED',
            releasedBy = ?,
            releasedAt = datetime('now'),
            updatedAt = datetime('now')
        WHERE guildId = ? AND userId = ? AND status = 'ACTIVE'
        """,
        (
            int(releasedBy),
            int(guildId),
            int(userId),
        ),
    )
