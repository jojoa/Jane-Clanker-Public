from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import config
from db.sqlite import execute, executeMany, executeReturnId, fetchAll, fetchOne, runWriteTransaction
from features.staff.sessions.Roblox import robloxAssets, robloxInventoryVisual

_VALID_VISUAL_REF_STATE = "VALID"
_INVALID_VISUAL_REF_STATE = "INVALID"
_ERROR_VISUAL_REF_STATE = "ERROR"
_PENDING_VISUAL_REF_STATE = "PENDING"
PROPOSAL_STATUS_OPEN = "OPEN"
PROPOSAL_STATUS_APPROVED = "APPROVED"
PROPOSAL_STATUS_REJECTED = "REJECTED"
PROPOSAL_STATUS_CLOSED = "CLOSED"
PROPOSAL_STATUSES = {
    PROPOSAL_STATUS_OPEN,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_CLOSED,
}
PROPOSAL_VOTE_WINDOW_HOURS = 24
PROPOSAL_VOTE_FLAG = "FLAG"
PROPOSAL_VOTE_NOT_FLAG = "NOT_FLAG"
PROPOSAL_VOTES = {PROPOSAL_VOTE_FLAG, PROPOSAL_VOTE_NOT_FLAG}


def normalizeSeverity(value: object) -> int:
    try:
        severity = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, severity))


def normalizeProposalStatus(value: object) -> str:
    text = str(value or "").strip().upper()
    return text if text in PROPOSAL_STATUSES else PROPOSAL_STATUS_OPEN


def normalizeProposalVote(value: object) -> str:
    text = str(value or "").strip().upper()
    return text if text in PROPOSAL_VOTES else ""


async def addRule(
    ruleType: str,
    ruleValue: str,
    note: Optional[str],
    createdBy: Optional[int],
    severity: int = 0,
) -> int:
    return await executeReturnId(
        """
        INSERT INTO bg_flag_rules (ruleType, ruleValue, note, severity, createdBy)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ruleType, ruleValue, note, normalizeSeverity(severity), createdBy),
    )


async def upsertRule(
    ruleType: str,
    ruleValue: str,
    note: Optional[str],
    createdBy: Optional[int],
    severity: int = 0,
) -> tuple[int, bool]:
    normalizedRuleType = str(ruleType or "").strip().lower()
    normalizedRuleValue = str(ruleValue or "").strip()
    normalizedNote = str(note or "").strip() or None
    normalizedSeverity = normalizeSeverity(severity)
    normalizedCreatedBy = int(createdBy or 0)
    if not normalizedRuleType or not normalizedRuleValue:
        return 0, False

    async def _tx(db) -> tuple[int, bool]:
        async with db.execute(
            """
            SELECT *
            FROM bg_flag_rules
            WHERE ruleType = ? AND ruleValue = ?
            ORDER BY ruleId ASC
            LIMIT 1
            """,
            (normalizedRuleType, normalizedRuleValue),
        ) as cur:
            row = await cur.fetchone()

        if row is not None:
            existing = dict(row)
            ruleId = int(existing.get("ruleId") or 0)
            await db.execute(
                """
                UPDATE bg_flag_rules
                SET note = CASE
                        WHEN COALESCE(note, '') = '' AND ? IS NOT NULL THEN ?
                        ELSE note
                    END,
                    severity = CASE
                        WHEN COALESCE(severity, 0) <= 0 AND ? > 0 THEN ?
                        ELSE severity
                    END,
                    createdBy = CASE
                        WHEN COALESCE(createdBy, 0) <= 0 AND ? > 0 THEN ?
                        ELSE createdBy
                    END
                WHERE ruleId = ?
                """,
                (
                    normalizedNote,
                    normalizedNote,
                    normalizedSeverity,
                    normalizedSeverity,
                    normalizedCreatedBy,
                    normalizedCreatedBy,
                    ruleId,
                ),
            )
            return ruleId, False

        cur = await db.execute(
            """
            INSERT INTO bg_flag_rules (ruleType, ruleValue, note, severity, createdBy)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalizedRuleType,
                normalizedRuleValue,
                normalizedNote,
                normalizedSeverity,
                normalizedCreatedBy if normalizedCreatedBy > 0 else None,
            ),
        )
        return int(cur.lastrowid), True

    return await runWriteTransaction(_tx)


async def removeRule(ruleId: int) -> None:
    await execute("DELETE FROM bg_flag_rules WHERE ruleId = ?", (ruleId,))


async def getRule(ruleId: int) -> Optional[Dict]:
    return await fetchOne(
        "SELECT * FROM bg_flag_rules WHERE ruleId = ?",
        (int(ruleId),),
    )


async def listRules(ruleType: Optional[str] = None) -> List[Dict]:
    if ruleType:
        return await fetchAll(
            "SELECT * FROM bg_flag_rules WHERE ruleType = ? ORDER BY ruleId ASC",
            (ruleType,),
        )
    return await fetchAll("SELECT * FROM bg_flag_rules ORDER BY ruleId ASC")


async def createProposal(
    *,
    guildId: int,
    ruleType: str,
    ruleValue: str,
    note: Optional[str],
    proposedBy: Optional[int],
    severity: int = 0,
) -> int:
    async def _tx(db) -> int:
        normalizedRuleType = str(ruleType or "").strip().lower()
        normalizedRuleValue = str(ruleValue or "").strip()
        normalizedNote = str(note or "").strip() or None
        normalizedSeverity = normalizeSeverity(severity)
        normalizedProposer = int(proposedBy or 0)
        ruleCur = await db.execute(
            """
            INSERT INTO bg_flag_rules (ruleType, ruleValue, note, severity, createdBy)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalizedRuleType,
                normalizedRuleValue,
                normalizedNote,
                normalizedSeverity,
                normalizedProposer,
            ),
        )
        ruleId = int(ruleCur.lastrowid)
        proposalCur = await db.execute(
            """
            INSERT INTO bg_flag_proposals (
                guildId, ruleType, ruleValue, note, severity, proposedBy, resultingRuleId
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guildId or 0),
                normalizedRuleType,
                normalizedRuleValue,
                normalizedNote,
                normalizedSeverity,
                normalizedProposer,
                ruleId,
            ),
        )
        proposalId = int(proposalCur.lastrowid)
        if normalizedProposer > 0:
            await db.execute(
                """
                INSERT INTO bg_flag_proposal_votes (proposalId, voterId, vote)
                VALUES (?, ?, ?)
                """,
                (proposalId, normalizedProposer, PROPOSAL_VOTE_FLAG),
            )
        return proposalId

    return await runWriteTransaction(_tx)


async def deleteProposal(proposalId: int) -> None:
    proposal = await getProposal(int(proposalId))
    ruleId = int((proposal or {}).get("resultingRuleId") or 0)
    await execute("DELETE FROM bg_flag_proposal_votes WHERE proposalId = ?", (int(proposalId),))
    await execute("DELETE FROM bg_flag_proposals WHERE proposalId = ?", (int(proposalId),))
    if ruleId > 0:
        await removeRule(ruleId)


async def getProposal(proposalId: int) -> Optional[Dict]:
    return await fetchOne(
        "SELECT * FROM bg_flag_proposals WHERE proposalId = ?",
        (int(proposalId),),
    )


async def setProposalMessage(proposalId: int, *, channelId: int, messageId: int) -> None:
    await execute(
        """
        UPDATE bg_flag_proposals
        SET channelId = ?, messageId = ?, updatedAt = datetime('now')
        WHERE proposalId = ?
        """,
        (int(channelId), int(messageId), int(proposalId)),
    )


async def listOpenProposalsWithMessages() -> List[Dict]:
    await closeExpiredProposals()
    return await fetchAll(
        """
        SELECT *
        FROM bg_flag_proposals
        WHERE status = ?
          AND datetime(createdAt) > datetime('now', '-' || ? || ' hours')
          AND COALESCE(channelId, 0) > 0
          AND COALESCE(messageId, 0) > 0
        ORDER BY proposalId ASC
        """,
        (PROPOSAL_STATUS_OPEN, PROPOSAL_VOTE_WINDOW_HOURS),
    )


async def closeExpiredProposal(proposalId: int) -> bool:
    async def _tx(db) -> bool:
        cur = await db.execute(
            """
            UPDATE bg_flag_proposals
            SET status = ?,
                resolvedAt = datetime('now'),
                updatedAt = datetime('now')
            WHERE proposalId = ?
              AND status = ?
              AND datetime(createdAt) <= datetime('now', '-' || ? || ' hours')
            """,
            (
                PROPOSAL_STATUS_CLOSED,
                int(proposalId),
                PROPOSAL_STATUS_OPEN,
                PROPOSAL_VOTE_WINDOW_HOURS,
            ),
        )
        return int(cur.rowcount or 0) > 0

    return await runWriteTransaction(_tx)


async def closeExpiredProposals() -> int:
    async def _tx(db) -> int:
        cur = await db.execute(
            """
            UPDATE bg_flag_proposals
            SET status = ?,
                resolvedAt = datetime('now'),
                updatedAt = datetime('now')
            WHERE status = ?
              AND datetime(createdAt) <= datetime('now', '-' || ? || ' hours')
            """,
            (
                PROPOSAL_STATUS_CLOSED,
                PROPOSAL_STATUS_OPEN,
                PROPOSAL_VOTE_WINDOW_HOURS,
            ),
        )
        return int(cur.rowcount or 0)

    return await runWriteTransaction(_tx)


async def upsertProposalVote(proposalId: int, *, voterId: int, vote: str) -> None:
    normalizedVote = normalizeProposalVote(vote)
    if not normalizedVote:
        raise ValueError("Invalid proposal vote.")
    await execute(
        """
        INSERT INTO bg_flag_proposal_votes (proposalId, voterId, vote)
        VALUES (?, ?, ?)
        ON CONFLICT(proposalId, voterId) DO UPDATE SET
            vote = excluded.vote,
            updatedAt = datetime('now')
        """,
        (int(proposalId), int(voterId or 0), normalizedVote),
    )


async def proposalVoteCounts(proposalId: int) -> dict[str, int]:
    rows = await fetchAll(
        """
        SELECT vote, COUNT(*) AS total
        FROM bg_flag_proposal_votes
        WHERE proposalId = ?
        GROUP BY vote
        """,
        (int(proposalId),),
    )
    counts = {
        PROPOSAL_VOTE_FLAG: 0,
        PROPOSAL_VOTE_NOT_FLAG: 0,
        "total": 0,
    }
    for row in rows:
        vote = normalizeProposalVote(row.get("vote"))
        if not vote:
            continue
        count = int(row.get("total") or 0)
        counts[vote] = count
        counts["total"] += count
    return counts


async def approveProposalWithRule(proposalId: int, *, resolvedBy: int) -> Optional[int]:
    async def _tx(db) -> Optional[int]:
        async with db.execute(
            "SELECT * FROM bg_flag_proposals WHERE proposalId = ?",
            (int(proposalId),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        proposal = dict(row)
        proposalStatus = normalizeProposalStatus(proposal.get("status"))
        if proposalStatus != PROPOSAL_STATUS_OPEN:
            if proposalStatus != PROPOSAL_STATUS_APPROVED:
                return None
            ruleId = proposal.get("resultingRuleId")
            try:
                return int(ruleId) if ruleId else None
            except (TypeError, ValueError):
                return None
        existingRuleId = int(proposal.get("resultingRuleId") or 0)
        if existingRuleId > 0:
            await db.execute(
                """
                UPDATE bg_flag_proposals
                SET status = ?,
                    resolvedBy = ?,
                    resolvedAt = datetime('now'),
                    updatedAt = datetime('now')
                WHERE proposalId = ?
                """,
                (
                    PROPOSAL_STATUS_APPROVED,
                    int(resolvedBy or 0),
                    int(proposalId),
                ),
            )
            return existingRuleId

        insertCur = await db.execute(
            """
            INSERT INTO bg_flag_rules (ruleType, ruleValue, note, severity, createdBy)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(proposal.get("ruleType") or "").strip().lower(),
                str(proposal.get("ruleValue") or "").strip(),
                str(proposal.get("note") or "").strip() or None,
                normalizeSeverity(proposal.get("severity")),
                int(proposal.get("proposedBy") or resolvedBy or 0),
            ),
        )
        ruleId = int(insertCur.lastrowid)
        await db.execute(
            """
            UPDATE bg_flag_proposals
            SET status = ?,
                resultingRuleId = ?,
                resolvedBy = ?,
                resolvedAt = datetime('now'),
                updatedAt = datetime('now')
            WHERE proposalId = ?
            """,
            (
                PROPOSAL_STATUS_APPROVED,
                ruleId,
                int(resolvedBy or 0),
                int(proposalId),
            ),
        )
        return ruleId

    return await runWriteTransaction(_tx)


async def rejectProposal(proposalId: int, *, resolvedBy: int) -> bool:
    async def _tx(db) -> bool:
        async with db.execute(
            "SELECT resultingRuleId FROM bg_flag_proposals WHERE proposalId = ? AND status = ?",
            (int(proposalId), PROPOSAL_STATUS_OPEN),
        ) as rowCur:
            row = await rowCur.fetchone()
        if row is None:
            return False
        ruleId = int(dict(row).get("resultingRuleId") or 0)
        if ruleId > 0:
            await db.execute("DELETE FROM bg_flag_rules WHERE ruleId = ?", (ruleId,))
        cur = await db.execute(
            """
            UPDATE bg_flag_proposals
            SET status = ?,
                resolvedBy = ?,
                resolvedAt = datetime('now'),
                updatedAt = datetime('now')
            WHERE proposalId = ?
              AND status = ?
            """,
            (
                PROPOSAL_STATUS_REJECTED,
                int(resolvedBy or 0),
                int(proposalId),
                PROPOSAL_STATUS_OPEN,
            ),
        )
        return int(cur.rowcount or 0) > 0

    return await runWriteTransaction(_tx)


def _normalizeAssetId(value: object) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _visualHashSize() -> int:
    try:
        configured = int(getattr(config, "bgIntelligenceInventoryVisualHashSize", 16) or 16)
    except (TypeError, ValueError):
        configured = 16
    return max(4, min(configured, 32))


def _visualColorSignatureVersion() -> int:
    try:
        return int(robloxAssets.colorSignatureVersion())
    except Exception:
        return 1


def _utcIsoNow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalizeVisualMetadataRow(assetId: int, row: dict[str, Any] | None = None) -> dict[str, Any]:
    source = dict(row or {})
    assetTypeId = _normalizeAssetId(source.get("assetTypeId"))
    assetTypeName = str(source.get("assetTypeName") or "").strip() or None
    visualCategory = str(source.get("visualCategory") or "").strip()
    if not visualCategory:
        visualCategory = str(robloxInventoryVisual.visualCategoryFromType(assetTypeName, assetTypeId) or "").strip()
    return {
        "assetId": int(assetId),
        "assetName": str(source.get("assetName") or source.get("name") or "").strip() or None,
        "assetTypeId": int(assetTypeId) if assetTypeId is not None else None,
        "assetTypeName": assetTypeName,
        "visualCategory": visualCategory,
    }


async def _fetchItemVisualMetadata(assetIds: list[int]) -> tuple[dict[int, dict[str, Any]], str | None]:
    uniqueIds = sorted({int(assetId) for assetId in list(assetIds or []) if int(assetId or 0) > 0})
    if not uniqueIds:
        return {}, None
    detailsById, error = await robloxAssets.fetchCatalogAssetPrices(uniqueIds)
    metadata: dict[int, dict[str, Any]] = {}
    for assetId, details in dict(detailsById or {}).items():
        if not isinstance(details, dict):
            continue
        metadata[int(assetId)] = _normalizeVisualMetadataRow(
            int(assetId),
            {
                "assetName": details.get("name"),
                "assetTypeId": details.get("assetTypeId"),
                "assetTypeName": details.get("assetTypeName"),
            },
        )
    return metadata, error


def _itemRuleMeta(itemRules: list[dict]) -> dict[int, dict[str, object]]:
    metaByAssetId: dict[int, dict[str, object]] = {}
    for rule in list(itemRules or []):
        assetId = _normalizeAssetId(rule.get("ruleValue"))
        if assetId is None:
            continue
        existing = metaByAssetId.get(assetId)
        note = str(rule.get("note") or "").strip() or None
        ruleId = int(rule.get("ruleId") or 0)
        if existing is None:
            metaByAssetId[assetId] = {
                "sourceRuleId": ruleId if ruleId > 0 else None,
                "sourceRuleCount": 1,
                "note": note,
            }
            continue
        existing["sourceRuleCount"] = int(existing.get("sourceRuleCount") or 0) + 1
        currentSourceRuleId = int(existing.get("sourceRuleId") or 0)
        if ruleId > 0 and (currentSourceRuleId <= 0 or ruleId < currentSourceRuleId):
            existing["sourceRuleId"] = ruleId
        if not existing.get("note") and note:
            existing["note"] = note
    return metaByAssetId


def _normalizeValidationRow(assetId: int, row: dict[str, Any] | None = None) -> dict[str, Any]:
    source = dict(row or {})
    state = str(source.get("validationState") or _PENDING_VISUAL_REF_STATE).strip().upper()
    if state not in {
        _VALID_VISUAL_REF_STATE,
        _INVALID_VISUAL_REF_STATE,
        _ERROR_VISUAL_REF_STATE,
        _PENDING_VISUAL_REF_STATE,
    }:
        state = _PENDING_VISUAL_REF_STATE
    return {
        "assetId": int(assetId),
        "thumbnailHash": str(source.get("thumbnailHash") or "").strip() or None,
        "colorSignature": str(source.get("colorSignature") or "").strip() or None,
        "colorSignatureVersion": int(source.get("colorSignatureVersion") or 0),
        "hashSize": int(source.get("hashSize") or 0),
        "thumbnailUrl": str(source.get("thumbnailUrl") or "").strip() or None,
        "thumbnailState": str(source.get("thumbnailState") or "").strip() or None,
        "validationState": state,
        "validationError": str(source.get("validationError") or "").strip() or None,
        "lastValidatedAt": str(source.get("lastValidatedAt") or "").strip() or None,
    }


async def listItemVisualReferences(*, validOnly: bool = False) -> List[Dict]:
    query = "SELECT * FROM bg_item_visual_refs"
    params: tuple = ()
    if validOnly:
        query += " WHERE validationState = ? AND COALESCE(thumbnailHash, '') <> ''"
        params = (_VALID_VISUAL_REF_STATE,)
    query += " ORDER BY visualCategory ASC, assetTypeId ASC, assetId ASC"
    return await fetchAll(query, params)


async def validateItemVisualReference(assetId: int) -> dict[str, Any]:
    rows = await robloxAssets.validateRobloxAssetVisualReferences([int(assetId)])
    if rows:
        return dict(rows[0])
    return _normalizeValidationRow(
        int(assetId),
        {
            "validationState": _ERROR_VISUAL_REF_STATE,
            "validationError": "Validation did not return a result.",
            "lastValidatedAt": _utcIsoNow(),
        },
    )


async def syncItemVisualReferences(*, force: bool = False) -> dict[str, Any]:
    itemRules = await listRules("item")
    itemMetaByAssetId = _itemRuleMeta(itemRules)
    currentAssetIds = set(itemMetaByAssetId.keys())
    existingRows = await listItemVisualReferences(validOnly=False)
    existingByAssetId = {
        int(row.get("assetId")): row
        for row in existingRows
        if _normalizeAssetId(row.get("assetId")) is not None
    }

    staleAssetIds = sorted(set(existingByAssetId.keys()) - currentAssetIds)
    if staleAssetIds:
        await executeMany(
            "DELETE FROM bg_item_visual_refs WHERE assetId = ?",
            [(int(assetId),) for assetId in staleAssetIds],
        )

    if not currentAssetIds:
        return {
            "ruleCount": 0,
            "assetCount": 0,
            "validatedCount": 0,
            "invalidCount": 0,
            "errorCount": 0,
            "pendingCount": 0,
            "checkedCount": 0,
            "metadataCount": 0,
            "metadataCheckedCount": 0,
            "categoryCounts": {},
            "metadataError": None,
            "removedCount": len(staleAssetIds),
            "sampleIssues": [],
        }

    targetHashSize = _visualHashSize()
    targetColorSignatureVersion = _visualColorSignatureVersion()
    needsValidation: set[int] = set()
    needsMetadata: set[int] = set()
    for assetId in currentAssetIds:
        row = existingByAssetId.get(int(assetId))
        if force or row is None:
            needsValidation.add(int(assetId))
            needsMetadata.add(int(assetId))
            continue
        state = str(row.get("validationState") or "").strip().upper()
        hashSize = int(row.get("hashSize") or 0)
        thumbnailHash = str(row.get("thumbnailHash") or "").strip()
        colorSignature = str(row.get("colorSignature") or "").strip()
        colorSignatureVersion = int(row.get("colorSignatureVersion") or 0)
        visualCategory = str(row.get("visualCategory") or "").strip()
        assetTypeId = _normalizeAssetId(row.get("assetTypeId"))
        if hashSize != targetHashSize:
            needsValidation.add(int(assetId))
        elif colorSignatureVersion != targetColorSignatureVersion:
            needsValidation.add(int(assetId))
        elif state in {_PENDING_VISUAL_REF_STATE, _ERROR_VISUAL_REF_STATE}:
            needsValidation.add(int(assetId))
        elif state == _VALID_VISUAL_REF_STATE and not thumbnailHash:
            needsValidation.add(int(assetId))
        elif state == _VALID_VISUAL_REF_STATE and not colorSignature:
            needsValidation.add(int(assetId))
        if not visualCategory or assetTypeId is None:
            needsMetadata.add(int(assetId))

    validatedRows: dict[int, dict[str, Any]] = {}
    if needsValidation:
        for row in await robloxAssets.validateRobloxAssetVisualReferences(sorted(needsValidation)):
            assetId = _normalizeAssetId(row.get("assetId"))
            if assetId is None:
                continue
            validatedRows[assetId] = _normalizeValidationRow(assetId, row)

    metadataRows: dict[int, dict[str, Any]] = {}
    metadataError: str | None = None
    if needsMetadata:
        try:
            metadataRows, metadataError = await _fetchItemVisualMetadata(sorted(needsMetadata))
        except Exception as exc:
            metadataRows = {}
            metadataError = str(exc)

    upsertRows: list[tuple] = []
    finalRows: list[dict[str, Any]] = []
    for assetId in sorted(currentAssetIds):
        meta = itemMetaByAssetId.get(assetId) or {}
        existing = existingByAssetId.get(assetId)
        if assetId in validatedRows:
            effective = dict(validatedRows[assetId])
        elif existing is not None:
            effective = _normalizeValidationRow(assetId, existing)
        else:
            effective = _normalizeValidationRow(
                assetId,
                {
                    "validationState": _PENDING_VISUAL_REF_STATE,
                    "validationError": "Validation pending.",
                },
            )
        if assetId in metadataRows:
            metadata = dict(metadataRows[assetId])
        elif existing is not None:
            metadata = _normalizeVisualMetadataRow(assetId, existing)
        else:
            metadata = _normalizeVisualMetadataRow(assetId)
        finalRow = {
            "assetId": int(assetId),
            "sourceRuleId": meta.get("sourceRuleId"),
            "sourceRuleCount": int(meta.get("sourceRuleCount") or 0) or 1,
            "note": meta.get("note"),
            **metadata,
            **effective,
        }
        finalRows.append(finalRow)
        upsertRows.append(
            (
                int(finalRow["assetId"]),
                int(finalRow["sourceRuleId"] or 0) or None,
                int(finalRow["sourceRuleCount"] or 1),
                finalRow["note"],
                finalRow["assetName"],
                int(finalRow["assetTypeId"] or 0) or None,
                finalRow["assetTypeName"],
                finalRow["visualCategory"],
                finalRow["thumbnailHash"],
                finalRow["colorSignature"],
                int(finalRow["colorSignatureVersion"] or 0),
                int(finalRow["hashSize"] or 0),
                finalRow["thumbnailUrl"],
                finalRow["thumbnailState"],
                finalRow["validationState"],
                finalRow["validationError"],
                finalRow["lastValidatedAt"],
            )
        )

    await executeMany(
        """
        INSERT INTO bg_item_visual_refs (
            assetId,
            sourceRuleId,
            sourceRuleCount,
            note,
            assetName,
            assetTypeId,
            assetTypeName,
            visualCategory,
            thumbnailHash,
            colorSignature,
            colorSignatureVersion,
            hashSize,
            thumbnailUrl,
            thumbnailState,
            validationState,
            validationError,
            lastValidatedAt
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(assetId) DO UPDATE SET
            sourceRuleId = excluded.sourceRuleId,
            sourceRuleCount = excluded.sourceRuleCount,
            note = excluded.note,
            assetName = excluded.assetName,
            assetTypeId = excluded.assetTypeId,
            assetTypeName = excluded.assetTypeName,
            visualCategory = excluded.visualCategory,
            thumbnailHash = excluded.thumbnailHash,
            colorSignature = excluded.colorSignature,
            colorSignatureVersion = excluded.colorSignatureVersion,
            hashSize = excluded.hashSize,
            thumbnailUrl = excluded.thumbnailUrl,
            thumbnailState = excluded.thumbnailState,
            validationState = excluded.validationState,
            validationError = excluded.validationError,
            lastValidatedAt = excluded.lastValidatedAt,
            updatedAt = datetime('now')
        """,
        upsertRows,
    )

    sampleIssues: list[str] = []
    for row in finalRows:
        state = str(row.get("validationState") or "").upper()
        if state not in {_INVALID_VISUAL_REF_STATE, _ERROR_VISUAL_REF_STATE}:
            continue
        errorText = str(row.get("validationError") or "").strip() or state.title()
        sampleIssues.append(f"{int(row.get('assetId') or 0)}: {errorText}")
        if len(sampleIssues) >= 5:
            break

    return {
        "ruleCount": len(itemRules),
        "assetCount": len(currentAssetIds),
        "validatedCount": sum(1 for row in finalRows if str(row.get("validationState") or "").upper() == _VALID_VISUAL_REF_STATE),
        "invalidCount": sum(1 for row in finalRows if str(row.get("validationState") or "").upper() == _INVALID_VISUAL_REF_STATE),
        "errorCount": sum(1 for row in finalRows if str(row.get("validationState") or "").upper() == _ERROR_VISUAL_REF_STATE),
        "pendingCount": sum(1 for row in finalRows if str(row.get("validationState") or "").upper() == _PENDING_VISUAL_REF_STATE),
        "checkedCount": len(needsValidation),
        "metadataCount": sum(1 for row in finalRows if str(row.get("visualCategory") or "").strip()),
        "metadataCheckedCount": len(needsMetadata),
        "categoryCounts": {
            category: sum(1 for row in finalRows if str(row.get("visualCategory") or "").strip() == category)
            for category in sorted({str(row.get("visualCategory") or "").strip() for row in finalRows if str(row.get("visualCategory") or "").strip()})
        },
        "metadataError": metadataError,
        "removedCount": len(staleAssetIds),
        "sampleIssues": sampleIssues,
    }


async def getValidatedItemVisualReferences(*, ensureSynced: bool = True) -> Dict[int, dict[str, object]]:
    itemRules = await listRules("item")
    currentAssetIds = {assetId for assetId in (_normalizeAssetId(rule.get("ruleValue")) for rule in itemRules) if assetId is not None}

    if ensureSynced and currentAssetIds:
        existingRows = await listItemVisualReferences(validOnly=False)
        existingByAssetId = {
            int(row.get("assetId")): row
            for row in existingRows
            if _normalizeAssetId(row.get("assetId")) is not None
        }
        targetHashSize = _visualHashSize()
        targetColorSignatureVersion = _visualColorSignatureVersion()
        needsSync = set(existingByAssetId.keys()) != currentAssetIds
        if not needsSync:
            for assetId in currentAssetIds:
                row = existingByAssetId.get(assetId)
                state = str((row or {}).get("validationState") or "").strip().upper()
                hashSize = int((row or {}).get("hashSize") or 0)
                thumbnailHash = str((row or {}).get("thumbnailHash") or "").strip()
                colorSignature = str((row or {}).get("colorSignature") or "").strip()
                colorSignatureVersion = int((row or {}).get("colorSignatureVersion") or 0)
                visualCategory = str((row or {}).get("visualCategory") or "").strip()
                assetTypeId = _normalizeAssetId((row or {}).get("assetTypeId"))
                if state == _PENDING_VISUAL_REF_STATE or hashSize != targetHashSize:
                    needsSync = True
                    break
                if colorSignatureVersion != targetColorSignatureVersion:
                    needsSync = True
                    break
                if state == _VALID_VISUAL_REF_STATE and not thumbnailHash:
                    needsSync = True
                    break
                if state == _VALID_VISUAL_REF_STATE and not colorSignature:
                    needsSync = True
                    break
                if not visualCategory or assetTypeId is None:
                    needsSync = True
                    break
        if needsSync:
            await syncItemVisualReferences(force=False)

    rows = await fetchAll(
        """
        SELECT
            assetId,
            assetName,
            assetTypeId,
            assetTypeName,
            visualCategory,
            thumbnailHash,
            colorSignature,
            colorSignatureVersion
        FROM bg_item_visual_refs
        WHERE validationState = ? AND COALESCE(thumbnailHash, '') <> ''
        ORDER BY visualCategory ASC, assetTypeId ASC, assetId ASC
        """,
        (_VALID_VISUAL_REF_STATE,),
    )
    references: dict[int, dict[str, object]] = {}
    for row in rows:
        assetId = _normalizeAssetId(row.get("assetId"))
        thumbnailHash = str(row.get("thumbnailHash") or "").strip()
        if assetId is None or assetId not in currentAssetIds or not thumbnailHash:
            continue
        references[int(assetId)] = {
            "thumbnailHash": thumbnailHash,
            "assetName": str(row.get("assetName") or "").strip(),
            "assetTypeId": int(row.get("assetTypeId") or 0) or None,
            "assetTypeName": str(row.get("assetTypeName") or "").strip(),
            "visualCategory": str(row.get("visualCategory") or "").strip(),
            "colorSignature": str(row.get("colorSignature") or "").strip(),
            "colorSignatureVersion": int(row.get("colorSignatureVersion") or 0),
        }

    return references


async def getValidatedItemVisualHashes(*, ensureSynced: bool = True) -> Dict[int, str]:
    visualReferences = await getValidatedItemVisualReferences(ensureSynced=ensureSynced)
    return {
        int(assetId): str(details.get("thumbnailHash") or "").strip()
        for assetId, details in visualReferences.items()
        if str(details.get("thumbnailHash") or "").strip()
    }
