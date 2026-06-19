from __future__ import annotations

from typing import Optional

from db.sqlite import execute, executeReturnId, fetchAll, fetchOne, runWriteTransaction


async def createBestOfPoll(
    *,
    guildId: int,
    channelId: int,
    createdBy: int,
    title: str,
) -> int:
    return await executeReturnId(
        """
        INSERT INTO best_of_polls
            (guildId, channelId, messageId, createdBy, title, status, createdAt)
        VALUES (?, ?, 0, ?, ?, 'OPEN', datetime('now'))
        """,
        (
            int(guildId),
            int(channelId),
            int(createdBy),
            str(title or "").strip(),
        ),
    )


async def setBestOfPollMessageId(pollId: int, messageId: int) -> None:
    await execute(
        """
        UPDATE best_of_polls
        SET messageId = ?
        WHERE pollId = ?
        """,
        (int(messageId), int(pollId)),
    )


async def getBestOfPoll(pollId: int) -> Optional[dict]:
    return await fetchOne(
        """
        SELECT *
        FROM best_of_polls
        WHERE pollId = ?
        """,
        (int(pollId),),
    )


async def listOpenBestOfPollsForViews() -> list[dict]:
    return await fetchAll(
        """
        SELECT *
        FROM best_of_polls
        WHERE status = 'OPEN' AND messageId > 0
        ORDER BY pollId ASC
        """,
    )


async def getOpenBestOfPollForChannel(*, guildId: int, channelId: int) -> Optional[dict]:
    return await fetchOne(
        """
        SELECT *
        FROM best_of_polls
        WHERE guildId = ? AND channelId = ? AND status = 'OPEN'
        ORDER BY pollId DESC
        LIMIT 1
        """,
        (int(guildId), int(channelId)),
    )


async def listBestOfPollsReadyToFinalize(*, maxOpenHours: int = 24) -> list[dict]:
    hours = max(1, int(maxOpenHours or 24))
    return await fetchAll(
        """
        SELECT *
        FROM best_of_polls
        WHERE status = 'OPEN'
          AND createdAt <= datetime('now', '-' || ? || ' hours')
        ORDER BY pollId ASC
        """,
        (hours,),
    )


async def replaceBestOfCandidates(
    pollId: int,
    candidates: list[dict],
) -> None:
    params: list[tuple] = []
    for entry in candidates:
        params.append(
            (
                int(pollId),
                int(entry["userId"]),
                int(entry["priorityRank"]),
                str(entry["priorityLabel"]),
                str(entry.get("displayName") or "").strip(),
                int(entry["sortOrder"]),
            )
        )

    async def _tx(db) -> None:
        await db.execute(
            "DELETE FROM best_of_poll_candidates WHERE pollId = ?",
            (int(pollId),),
        )
        if params:
            await db.executemany(
                """
                INSERT INTO best_of_poll_candidates
                    (pollId, userId, priorityRank, priorityLabel, displayName, sortOrder)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                params,
            )

    await runWriteTransaction(_tx)


async def listBestOfCandidates(pollId: int) -> list[dict]:
    return await fetchAll(
        """
        SELECT *
        FROM best_of_poll_candidates
        WHERE pollId = ?
        ORDER BY priorityRank DESC, sortOrder ASC, userId ASC
        """,
        (int(pollId),),
    )


async def upsertBestOfSectionVote(
    *,
    pollId: int,
    voterId: int,
    sectionLabel: str,
    candidateUserId: int,
) -> None:
    await execute(
        """
        INSERT INTO best_of_poll_section_votes
            (pollId, voterId, sectionLabel, candidateUserId, updatedAt)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(pollId, voterId, sectionLabel)
        DO UPDATE SET
            candidateUserId = excluded.candidateUserId,
            updatedAt = datetime('now')
        """,
        (
            int(pollId),
            int(voterId),
            str(sectionLabel or "").strip(),
            int(candidateUserId),
        ),
    )


async def listBestOfSectionVotesForVoter(pollId: int, voterId: int) -> list[dict]:
    rows = await fetchAll(
        """
        SELECT *
        FROM best_of_poll_section_votes
        WHERE pollId = ? AND voterId = ?
        ORDER BY sectionLabel ASC
        """,
        (int(pollId), int(voterId)),
    )
    if rows:
        return rows

    # Backward-compatibility fallback for old single-vote rows.
    oldRow = await fetchOne(
        """
        SELECT *
        FROM best_of_poll_votes
        WHERE pollId = ? AND voterId = ?
        """,
        (int(pollId), int(voterId)),
    )
    if not oldRow:
        return []
    return [
        {
            "pollId": int(oldRow.get("pollId") or pollId),
            "voterId": int(oldRow.get("voterId") or voterId),
            "sectionLabel": "ALL",
            "candidateUserId": int(oldRow.get("candidateUserId") or 0),
            "updatedAt": oldRow.get("updatedAt"),
        }
    ]


async def upsertBestOfVote(
    *,
    pollId: int,
    voterId: int,
    candidateUserId: int,
) -> None:
    await execute(
        """
        INSERT INTO best_of_poll_votes
            (pollId, voterId, candidateUserId, updatedAt)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(pollId, voterId)
        DO UPDATE SET
            candidateUserId = excluded.candidateUserId,
            updatedAt = datetime('now')
        """,
        (
            int(pollId),
            int(voterId),
            int(candidateUserId),
        ),
    )


async def getBestOfVote(pollId: int, voterId: int) -> Optional[dict]:
    return await fetchOne(
        """
        SELECT *
        FROM best_of_poll_votes
        WHERE pollId = ? AND voterId = ?
        """,
        (int(pollId), int(voterId)),
    )


async def countBestOfVotes(pollId: int) -> int:
    sectionRow = await fetchOne(
        """
        SELECT COUNT(DISTINCT voterId) AS countValue
        FROM best_of_poll_section_votes
        WHERE pollId = ?
        """,
        (int(pollId),),
    )
    if sectionRow and int(sectionRow.get("countValue") or 0) > 0:
        return int(sectionRow.get("countValue") or 0)

    row = await fetchOne(
        """
        SELECT COUNT(DISTINCT voterId) AS countValue
        FROM best_of_poll_votes
        WHERE pollId = ?
        """,
        (int(pollId),),
    )
    if not row:
        return 0
    return int(row.get("countValue") or 0)


async def countBestOfSectionVotes(pollId: int) -> int:
    sectionRow = await fetchOne(
        """
        SELECT COUNT(*) AS countValue
        FROM best_of_poll_section_votes
        WHERE pollId = ?
        """,
        (int(pollId),),
    )
    if sectionRow and int(sectionRow.get("countValue") or 0) > 0:
        return int(sectionRow.get("countValue") or 0)

    row = await fetchOne(
        """
        SELECT COUNT(*) AS countValue
        FROM best_of_poll_votes
        WHERE pollId = ?
        """,
        (int(pollId),),
    )
    if not row:
        return 0
    return int(row.get("countValue") or 0)


async def listBestOfSectionVoteCounts(pollId: int) -> list[dict]:
    rows = await fetchAll(
        """
        SELECT sectionLabel, candidateUserId, COUNT(*) AS voteCount
        FROM best_of_poll_section_votes
        WHERE pollId = ?
        GROUP BY sectionLabel, candidateUserId
        ORDER BY sectionLabel ASC, voteCount DESC, candidateUserId ASC
        """,
        (int(pollId),),
    )
    if rows:
        return rows
    # Backward-compatibility fallback for old single-vote rows.
    oldRows = await fetchAll(
        """
        SELECT candidateUserId, COUNT(*) AS voteCount
        FROM best_of_poll_votes
        WHERE pollId = ?
        GROUP BY candidateUserId
        ORDER BY voteCount DESC, candidateUserId ASC
        """,
        (int(pollId),),
    )
    out: list[dict] = []
    for row in oldRows:
        out.append(
            {
                "sectionLabel": "ALL",
                "candidateUserId": int(row.get("candidateUserId") or 0),
                "voteCount": int(row.get("voteCount") or 0),
            }
        )
    return out


async def listBestOfVoteCounts(pollId: int) -> list[dict]:
    sectionRows = await fetchAll(
        """
        SELECT candidateUserId, COUNT(*) AS voteCount
        FROM best_of_poll_section_votes
        WHERE pollId = ?
        GROUP BY candidateUserId
        ORDER BY voteCount DESC, candidateUserId ASC
        """,
        (int(pollId),),
    )
    if sectionRows:
        return sectionRows

    return await fetchAll(
        """
        SELECT candidateUserId, COUNT(*) AS voteCount
        FROM best_of_poll_votes
        WHERE pollId = ?
        GROUP BY candidateUserId
        ORDER BY voteCount DESC, candidateUserId ASC
        """,
        (int(pollId),),
    )


async def closeBestOfPoll(*, pollId: int, closedBy: int) -> None:
    await execute(
        """
        UPDATE best_of_polls
        SET status = 'CLOSED',
            closedBy = ?,
            closedAt = datetime('now')
        WHERE pollId = ?
        """,
        (
            int(closedBy),
            int(pollId),
        ),
    )
