from __future__ import annotations

from datetime import datetime
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import config
from db.sqlite import execute, executeReturnId, fetchAll, fetchOne
from features.staff.sessions.Roblox import robloxUsers, roverIdentity


@dataclass(slots=True, frozen=True)
class HonorGuardConfig:
    enabled: bool
    reviewChannelId: int
    logChannelId: int
    archiveChannelId: int
    spreadsheetId: str
    memberSheetName: str
    archiveSheetName: str
    eventHostsSheetName: str


@dataclass(slots=True, frozen=True)
class HonorGuardPointDeltas:
    quotaPoints: float = 0
    promotionEventPoints: float = 0
    promotionAwardedPoints: float = 0

@dataclass(slots=True, frozen=True)
class HonorGuardScaffoldStatus:
    config: HonorGuardConfig
    plannedDbTables: tuple[str, ...]
    plannedModules: tuple[str, ...]
    nextMilestones: tuple[str, ...]
    sheetProblems: tuple[str, ...] = ()


def _normalizePositiveInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _jsonText(value: object) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        text = value.strip()
        return text or "{}"
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        return "{}"


def _jsonDict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _rememberHonorGuardIdentity(
    *,
    userId: int,
    robloxUsername: str,
    guildId: int,
    source: str,
) -> None:
    if int(userId or 0) <= 0 or not str(robloxUsername or "").strip():
        return
    await roverIdentity.rememberKnownRobloxIdentity(
        int(userId),
        str(robloxUsername or "").strip(),
        source=source,
        guildId=int(guildId or 0),
        confidence=80,
    )


def _normalizeKey(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _normalizeStatus(value: object, fallback: str = "PENDING") -> str:
    text = str(value or fallback).strip().upper()
    return text or fallback


def _normalizePointType(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"QUOTA", "PROMOTION_EVENT", "PROMOTION_AWARDED"}:
        return text
    return "PROMOTION_AWARDED"

def _configuredPointMap(configModule: Any, attrName: str) -> dict[str, float]:
    raw = getattr(configModule, attrName, {}) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out

def _configuredComplexPointMap(configModule: Any, attrName: str) -> dict[str, dict[str, float]]:
    raw = getattr(configModule, attrName, {}) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for key, value in raw.items():
        try:
            out[key] = {
                "base": float(value.get("base", 0)),
                "per_intervall": float(value.get("per_intervall", 0)),
                "minimum": float(value.get("minimum", 0)),
            }
        except (TypeError, ValueError):
            continue
    return out


def _attendanceQuotaPoints(configModule: Any, eventType: str) -> float:
    byType = _configuredPointMap(configModule, "honorGuardAttendanceQuotaPointsByEventType")
    return float(byType.get(eventType, 1))


def _attendancePromotionPoints(configModule: Any, eventType: str, durationMinutes: int) -> float:
    byType = _configuredComplexPointMap(configModule, "honorGuardAttendancePromotionPointsByEventType")
    intervall = max(1, int(getattr(configModule, "honorGuardAttendancePromotionPointsIntervallMinutes", 30) or 30))
    points = byType.get(eventType, 0).get("base", 0) + byType.get(eventType, 0).get("per_intervall", 0) * durationMinutes // intervall
    points = max(points, byType.get(eventType, 0).get("minimum", 0))
    return float(points)

def _supervisorPromotionPoints(configModule: Any, eventType: str, durationMinutes: int) -> float:
    byType = _configuredComplexPointMap(configModule, "honorGuardSupervisorPromotionPointsByEventType")
    intervall = max(1, int(getattr(configModule, "honorGuardAttendancePromotionPointsIntervallMinutes", 30) or 30))
    points = byType.get(eventType, 0).get("base", 0) + byType.get(eventType, 0).get("per_intervall", 0) * durationMinutes // intervall
    points = max(points, byType.get(eventType, 0).get("minimum", 0))
    return float(points)



def _ceilPoints(value: float) -> int:
    return int(math.ceil(max(0.0, float(value or 0))))


def calculatePointDeltas(
    *,
    configModule: Any,
    memberGroup: str = "",
    eventType: str = "",
    participationRole: str = "attendee",
    durationMinutes: int = 0,
    attendeeCount: int = 0,
    gradedAttendeeCount: int = 0,
    passed: bool = False,
    screenAssist: bool = False,
) -> HonorGuardPointDeltas:
    normalizedEvent = _normalizeKey(eventType).lower()
    normalizedRole = _normalizeKey(participationRole).lower()
    group = str(memberGroup or "").strip().lower()
    attendeeTotal = max(0, int(attendeeCount or 0))
    gradedTotal = max(0, int(gradedAttendeeCount or 0))

    quotaPoints = 0.0
    promotionEventPoints = 0.0

    attendanceEligible = group in {"enlisted", "nco"}
    officerLike = group in {"officer", "nco", ""}

    if normalizedEvent not in {"jge", "nco_exam"}:
        if normalizedRole == "attendee":
            if attendanceEligible:
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)
                promotionEventPoints = _attendancePromotionPoints(configModule, normalizedEvent, durationMinutes)
            elif normalizedEvent == "inspection":
                promotionEventPoints = _attendancePromotionPoints(configModule, normalizedEvent, durationMinutes) or 8

        elif normalizedRole == "host":
            if officerLike:
                hostMap = _configuredPointMap(configModule, "honorGuardHostPromotionPointsByEventType")
                promotionEventPoints = float(hostMap.get(normalizedEvent, 0))
            if group == "nco":
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)

        elif normalizedRole == "supervisor":
            if officerLike:
                promotionEventPoints = _supervisorPromotionPoints(configModule, normalizedEvent, durationMinutes)
            if group == "nco":
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)

        elif normalizedRole == "cohost":
            if officerLike:
                promotionEventPoints = _attendancePromotionPoints(configModule, normalizedEvent, durationMinutes)
            if group == "nco":
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)

    if normalizedEvent == "jge":
        if normalizedRole == "host":
            rate = float(getattr(configModule, "honorGuardJgePointsPerGradedAttendee", 0.75) or 0.75)
            promotionEventPoints = _ceilPoints(rate * attendeeTotal)
            if group == "nco":
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)
        elif normalizedRole in {"cohost", "supervisor"}:
            rate = float(getattr(configModule, "honorGuardJgePointsPerGradedAttendee", 0.75) or 0.75)
            promotionEventPoints = _ceilPoints(rate * gradedTotal)
            if group == "nco":
                quotaPoints = _attendanceQuotaPoints(configModule, normalizedEvent)
        elif normalizedRole == "attendee" and passed:
            quotaPoints = _attendanceQuotaPoints(configModule, "jge")
            promotionEventPoints = _attendancePromotionPoints(configModule, "jge", durationMinutes)

    if normalizedEvent == "nco_exam":
        rate = float(getattr(configModule, "honorGuardNcoExamPointsPerGradedAttendee", 1.5) or 1.5)
        screenAssistPoints = float(getattr(configModule, "honorGuardNcoExamScreenAssistPoints", 2) or 2)
        if normalizedRole == "host":
            promotionEventPoints = _ceilPoints(rate * attendeeTotal)
        elif normalizedRole in {"cohost", "supervisor"}:
            if screenAssist:
                promotionEventPoints = screenAssistPoints
            if gradedTotal > 0:
                promotionEventPoints += _ceilPoints(rate * gradedTotal)
        elif normalizedRole == "attendee" and passed:
            quotaPoints = _attendanceQuotaPoints(configModule, "nco_exam")
            promotionEventPoints = _attendancePromotionPoints(configModule, "nco_exam", durationMinutes)

    return HonorGuardPointDeltas(
        quotaPoints=float(quotaPoints),
        promotionEventPoints=float(promotionEventPoints),
    )


def loadHonorGuardConfig(*, configModule: Any) -> HonorGuardConfig:
    return HonorGuardConfig(
        enabled=bool(getattr(configModule, "honorGuardEnabled", False)),
        reviewChannelId=_normalizePositiveInt(getattr(configModule, "honorGuardReviewChannelId", 0)),
        logChannelId=_normalizePositiveInt(getattr(configModule, "honorGuardLogChannelId", 0)),
        archiveChannelId=_normalizePositiveInt(getattr(configModule, "honorGuardArchiveChannelId", 0)),
        spreadsheetId=str(getattr(configModule, "honorGuardSpreadsheetId", "") or "").strip(),
        memberSheetName=str(getattr(configModule, "honorGuardMemberSheetName", "") or "").strip(),
        archiveSheetName=str(getattr(configModule, "honorGuardArchiveSheetName", "") or "").strip(),
        eventHostsSheetName=str(getattr(configModule, "honorGuardEventHostsSheetName", "") or "").strip(),
    )


def buildScaffoldStatus(*, configModule: Any) -> HonorGuardScaffoldStatus:
    try:
        from features.staff.honorGuard import sheets as honorGuardSheets

        sheetProblems = honorGuardSheets.configurationProblems(configModule=configModule)
    except Exception as exc:
        sheetProblems = (f"Sheet adapter check failed: {exc.__class__.__name__}",)

    return HonorGuardScaffoldStatus(
        config=loadHonorGuardConfig(configModule=configModule),
        plannedDbTables=(
            "hg_submissions",
            "hg_submission_events",
            "hg_point_awards",
            "hg_attendance_records",
            "hg_sentry_logs",
            "hg_quota_cycles",
            "hg_event_records",
        ),
        plannedModules=(
            "cogs.staff.honorGuardCog",
            "features.staff.honorGuard.service",
            "features.staff.honorGuard.sheets",
            "features.staff.honorGuard",
        ),
        nextMilestones=(
            "Build the command/view review flow on top of hg_submissions.",
            "Call syncApprovedSubmissionToSheet after reviewer approval.",
            "Live-test member row lookup and archive handling against the HG spreadsheet.",
            "Add bi-weekly quota reset automation after sheet columns are confirmed.",
        ),
        sheetProblems=sheetProblems,
    )


async def listPointAwardPendingStatuses() -> List[Dict]:
    submissions = await fetchAll(
        "SELECT * FROM hg_submissions WHERE submissionType = 'POINT_AWARD' AND status = 'PENDING' ORDER BY createdAt ASC, submissionId ASC",
    )
    enrichedSubmissions = []
    for submission in submissions:
        metadata = _jsonDict(submission.get("metadataJson"))
        enriched = dict(submission)
        enriched["reason"] = str(metadata.get("reason") or "").strip()
        enrichedSubmissions.append(enriched)

    return enrichedSubmissions

async def listSoloSentryPendingStatuses() -> List[Dict]:
    submissions = await fetchAll(
        "SELECT * FROM hg_submissions WHERE submissionType = 'SOLO_SENTRY' AND status = 'PENDING' ORDER BY createdAt ASC, submissionId ASC",
    )
    enrichedSubmissions = []
    for submission in submissions:
        metadata = _jsonDict(submission.get("metadataJson"))
        enriched = dict(submission)
        enriched["minutes"] = int(metadata.get("minutes") or 0)
        enriched["imageUrls"] = [
            str(value).strip()
            for value in metadata.get("imageUrls", [])
            if str(value).strip()
        ] if isinstance(metadata.get("imageUrls"), list) else []
        enrichedSubmissions.append(enriched)

    return enrichedSubmissions

async def listEventPendingStatuses() -> List[Dict]:
    submissions = await fetchAll(
        "SELECT * FROM hg_submissions WHERE submissionType = 'EVENT_RECORD' AND status = 'PENDING' ORDER BY createdAt ASC, submissionId ASC",
    )
    enrichedSubmissions = []
    for submission in submissions:
        enriched = dict(submission)
        enrichedSubmissions.append(enriched)

    return enrichedSubmissions

async def createPointAwardSubmission(
    *,
    guildId: int,
    channelId: int,
    submitterId: int,
    awardedUserId: int,
    reason: str,
    awardedPoints: float = 0,
    awardedUserDisplayName: str = "",
) -> int:
    awardedDelta = float(awardedPoints or 0)
    return await createSubmission(
        guildId=int(guildId),
        channelId=int(channelId),
        submitterId=int(submitterId),
        submissionType="POINT_AWARD",
        targetUserId=int(awardedUserId or 0),
        targetDisplayName=str(awardedUserDisplayName or "").strip(),
        deltas=HonorGuardPointDeltas(
            promotionAwardedPoints=awardedDelta,
        ),
        metadata={
            "reason": str(reason or "").strip(),
            "awardedPoints": awardedDelta,
        },
    )


async def getPointAwardSubmission(submissionId: int) -> Optional[dict[str, Any]]:
    submission = await getSubmission(int(submissionId))
    if submission is None:
        return None
    metadata = _jsonDict(submission.get("metadataJson"))
    enriched = dict(submission)
    enriched["reason"] = str(metadata.get("reason") or "").strip()
    return enriched


async def setPointAwardMessageId(submissionId: int, messageId: int) -> None:
    await setSubmissionMessageId(int(submissionId), int(messageId or 0))


async def updatePointAwardStatus(
    submissionId: int,
    status: str,
    *,
    reviewerId: int,
    note: str | None = None,
    threadId: int | None = None,
) -> None:
    details = {"threadId": int(threadId)} if int(threadId or 0) > 0 else None
    await setSubmissionStatus(
        submissionId=int(submissionId),
        status=str(status or "").strip().upper(),
        reviewerId=int(reviewerId or 0),
        note=str(note or "").strip(),
        details=details,
    )
    if _normalizeStatus(status) == "APPROVED":
        submission = await getSubmission(int(submissionId))
        if submission is not None:
            await ensurePointAwardRecordsForSubmission(
                submission=submission,
                sheetSynced=bool(int(submission.get("sheetSynced") or 0)),
            )


async def _appendSubmissionEvent(
    *,
    submissionId: int,
    actorId: int = 0,
    eventType: str,
    fromStatus: str = "",
    toStatus: str = "",
    note: str = "",
    details: object = None,
) -> int:
    return await executeReturnId(
        """
        INSERT INTO hg_submission_events
            (submissionId, actorId, eventType, fromStatus, toStatus, note, detailsJson)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(submissionId),
            int(actorId or 0),
            str(eventType or "EVENT").strip().upper(),
            str(fromStatus or "").strip().upper(),
            str(toStatus or "").strip().upper(),
            str(note or "").strip(),
            _jsonText(details),
        ),
    )


async def createSubmission(
    *,
    guildId: int,
    channelId: int,
    submitterId: int,
    submissionType: str,
    targetUserId: int = 0,
    targetDisplayName: str = "",
    eventDate: str = "",
    deltas: HonorGuardPointDeltas | None = None,
    metadata: object = None,
) -> int:
    pointDeltas = deltas or HonorGuardPointDeltas()
    submissionId = await executeReturnId(
        """
        INSERT INTO hg_submissions
            (
                guildId, channelId, submitterId, targetUserId,
                targetDisplayName, submissionType, eventDate,
                quotaPoints, promotionEventPoints, promotionAwardedPoints,
                metadataJson
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guildId),
            int(channelId),
            int(submitterId),
            int(targetUserId or 0),
            str(targetDisplayName or "").strip(),
            str(submissionType or "").strip().upper(),
            str(eventDate or "").strip(),
            float(pointDeltas.quotaPoints),
            float(pointDeltas.promotionEventPoints),
            float(pointDeltas.promotionAwardedPoints),
            _jsonText(metadata),
        ),
    )
    await _appendSubmissionEvent(
        submissionId=submissionId,
        actorId=submitterId,
        eventType="CREATED",
        toStatus="PENDING",
        details=metadata,
    )
    return submissionId


async def setSubmissionMessageId(submissionId: int, messageId: int) -> None:
    await execute(
        """
        UPDATE hg_submissions
        SET messageId = ?, updatedAt = datetime('now')
        WHERE submissionId = ?
        """,
        (int(messageId or 0), int(submissionId)),
    )


async def getSubmission(submissionId: int) -> Optional[dict[str, Any]]:
    return await fetchOne(
        "SELECT * FROM hg_submissions WHERE submissionId = ?",
        (int(submissionId),),
    )


async def setSubmissionStatus(
    *,
    submissionId: int,
    status: str,
    reviewerId: int = 0,
    note: str = "",
    details: object = None,
) -> None:
    submission = await getSubmission(int(submissionId))
    if submission is None:
        raise ValueError(f"Honor Guard submission not found: {submissionId}")
    fromStatus = _normalizeStatus(submission.get("status"))
    toStatus = _normalizeStatus(status)
    await execute(
        """
        UPDATE hg_submissions
        SET status = ?,
            reviewerId = ?,
            reviewNote = ?,
            reviewedAt = CASE WHEN ? IN ('APPROVED', 'REJECTED', 'CANCELED') THEN datetime('now') ELSE reviewedAt END,
            updatedAt = datetime('now')
        WHERE submissionId = ?
        """,
        (
            toStatus,
            int(reviewerId or 0),
            str(note or "").strip(),
            toStatus,
            int(submissionId),
        ),
    )
    await _appendSubmissionEvent(
        submissionId=int(submissionId),
        actorId=int(reviewerId or 0),
        eventType=f"STATUS_{toStatus}",
        fromStatus=fromStatus,
        toStatus=toStatus,
        note=note,
        details=details,
    )


async def createPointAward(
    *,
    guildId: int,
    targetUserId: int = 0,
    pointType: str,
    points: float,
    reason: str = "",
    awardedBy: int = 0,
    approvedBy: int = 0,
    submissionId: int = 0,
    sheetSynced: bool = False,
) -> int:
    awardId = await executeReturnId(
        """
        INSERT INTO hg_point_awards
            (
                submissionId, guildId, targetUserId, pointType,
                points, reason, awardedBy, approvedBy, sheetSynced
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(submissionId or 0),
            int(guildId),
            int(targetUserId or 0),
            _normalizePointType(pointType),
            float(points or 0),
            str(reason or "").strip(),
            int(awardedBy or 0),
            int(approvedBy or 0),
            1 if sheetSynced else 0,
        ),
    )
    return awardId


async def ensurePointAwardRecordsForSubmission(
    *,
    submission: dict[str, Any],
    sheetSynced: bool,
) -> list[int]:
    if _normalizeStatus(submission.get("status")) != "APPROVED":
        return []
    if str(submission.get("submissionType") or "").strip().upper() != "POINT_AWARD":
        return []

    submissionId = int(submission.get("submissionId") or 0)
    if submissionId <= 0:
        return []

    existingRows = await fetchAll(
        "SELECT awardId, pointType, sheetSynced FROM hg_point_awards WHERE submissionId = ?",
        (submissionId,),
    )
    existingByType = {
        str(row.get("pointType") or "").strip().upper(): row
        for row in existingRows
    }

    metadata = _jsonDict(submission.get("metadataJson"))
    awardedPoints = int(submission.get("promotionAwardedPoints") or 0)

    reason = str(metadata.get("reason") or "").strip()

    desiredRows: list[tuple[str, int]] = []
    if awardedPoints > 0:
        desiredRows.append(("PROMOTION_AWARDED", awardedPoints))

    createdAwardIds: list[int] = []
    for pointType, points in desiredRows:
        existing = existingByType.get(pointType)
        if existing is None:
            awardId = await createPointAward(
                submissionId=submissionId,
                guildId=int(submission.get("guildId") or 0),
                targetUserId=int(submission.get("targetUserId") or 0),
                pointType=pointType,
                points=points,
                reason=reason,
                awardedBy=int(submission.get("submitterId") or 0),
                approvedBy=int(submission.get("reviewerId") or 0),
                sheetSynced=sheetSynced,
            )
            createdAwardIds.append(int(awardId))
            continue

        if bool(int(existing.get("sheetSynced") or 0)) == bool(sheetSynced):
            continue

        await execute(
            """
            UPDATE hg_point_awards
            SET sheetSynced = ?
            WHERE awardId = ?
            """,
            (1 if sheetSynced else 0, int(existing.get("awardId") or 0)),
        )

    return createdAwardIds


async def createAttendanceRecord(
    *,
    eventRecordId: int,
    targetUserId: int,
    memberGroup: str,
    participationRole: str = "ATTENDEE",
    createdBy: int = 0,
) -> int:
    recordId = await executeReturnId(
        """
        INSERT INTO hg_attendance_records
            (
                eventRecordId, targetUserId,
                participationRole, memberGroup,
                createdBy
            )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(eventRecordId),
            int(targetUserId),
            _participationRole(participationRole).upper(),
            str(memberGroup or "").strip().upper(),
            int(createdBy or 0),
        ),
    )
    return recordId

async def removeAttendanceRecord(
    *,
    eventRecordId: int,
    targetUserId: int,
) -> int:
    await execute(
        """
        DELETE FROM hg_attendance_records
        WHERE eventRecordId = ?
          AND targetUserId = ?
        """,
        (
            int(eventRecordId),
            int(targetUserId),
        ),
    )
    return 1



async def createSoloSentryLog(
    *,
    guildId: int,
    userId: int,
    dutyDate: str,
    minutes: int = 0,
    submissionId: int = 0,
    status: str = "PENDING",
    configModule: Any = config,
) -> int:
    promotionPoints = float(getattr(configModule, "honorGuardSoloSentryDutyPromotionPoints", 1) or 1)
    sentryLogId = await executeReturnId(
        """
        INSERT INTO hg_sentry_logs
            (
                submissionId, guildId, userId, dutyDate, minutes,
                promotionEventPoints, status
            )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(submissionId or 0),
            int(guildId),
            int(userId),
            str(dutyDate or "").strip(),
            int(minutes or 0),
            promotionPoints,
            _normalizeStatus(status),
        ),
    )
    return sentryLogId


async def findExistingSentryLogForDate(*, userId: int, dutyDate: str) -> Optional[dict[str, Any]]:
    return await fetchOne(
        """
        SELECT *
        FROM hg_sentry_logs
        WHERE userId = ?
          AND dutyDate = ?
          AND status IN ('PENDING', 'APPROVED')
        ORDER BY sentryLogId DESC
        LIMIT 1
        """,
        (int(userId), str(dutyDate or "").strip()),
    )


async def createSoloSentrySubmission(
    *,
    guildId: int,
    channelId: int,
    submitterId: int,
    dutyDate: str,
    targetUserId: int = 0,
    targetDisplayName: str = "",
    minutes: int = 0,
    imageUrls: list[str] | None = None,
    configModule: Any = config,
) -> int:
    userId = int(targetUserId or submitterId)
    existing = await findExistingSentryLogForDate(userId=userId, dutyDate=dutyDate)
    if existing is not None:
        raise ValueError("A pending or approved Honor Guard sentry log already exists for that user/date.")

    deltas = HonorGuardPointDeltas(
        promotionEventPoints=float(getattr(configModule, "honorGuardSoloSentryDutyPromotionPoints", 1) or 1),
    )
    submissionId = await createSubmission(
        guildId=guildId,
        channelId=channelId,
        submitterId=submitterId,
        submissionType="SOLO_SENTRY",
        targetUserId=userId,
        targetDisplayName=targetDisplayName,
        eventDate=dutyDate,
        deltas=deltas,
        metadata={
            "minutes": int(minutes or 0),
            "imageUrls": list(imageUrls or []),
        },
    )
    await createSoloSentryLog(
        guildId=guildId,
        userId=userId,
        dutyDate=dutyDate,
        minutes=minutes,
        submissionId=submissionId,
        status="PENDING",
        configModule=configModule,
    )
    return submissionId


async def getSoloSentrySubmission(submissionId: int) -> Optional[dict[str, Any]]:
    submission = await getSubmission(int(submissionId))
    if submission is None:
        return None
    if str(submission.get("submissionType") or "").strip().upper() != "SOLO_SENTRY":
        return None

    metadata = _jsonDict(submission.get("metadataJson"))
    enriched = dict(submission)
    enriched["minutes"] = int(
        metadata.get("minutes")
    )
    enriched["imageUrls"] = [
        str(value).strip()
        for value in metadata.get("imageUrls", [])
        if str(value).strip()
    ] if isinstance(metadata.get("imageUrls"), list) else []
    return enriched


async def setSentryLogStatus(
    *,
    sentryLogId: int,
    status: str,
    reviewerId: int = 0,
    note: str = "",
) -> None:
    await execute(
        """
        UPDATE hg_sentry_logs
        SET status = ?,
            reviewerId = ?,
            reviewNote = ?,
            reviewedAt = CASE WHEN ? IN ('APPROVED', 'REJECTED', 'CANCELED') THEN datetime('now') ELSE reviewedAt END
        WHERE sentryLogId = ?
        """,
        (
            _normalizeStatus(status),
            int(reviewerId or 0),
            str(note or "").strip(),
            _normalizeStatus(status),
            int(sentryLogId),
        ),
    )


async def updateSoloSentrySubmissionStatus(
    submissionId: int,
    status: str,
    *,
    reviewerId: int,
    note: str | None = None,
    threadId: int | None = None,
) -> None:
    details = {"threadId": int(threadId)} if int(threadId or 0) > 0 else None
    await setSubmissionStatus(
        submissionId=int(submissionId),
        status=str(status or "").strip().upper(),
        reviewerId=int(reviewerId or 0),
        note=str(note or "").strip(),
        details=details,
    )
    sentryLog = await fetchOne(
        "SELECT sentryLogId FROM hg_sentry_logs WHERE submissionId = ? ORDER BY sentryLogId DESC LIMIT 1",
        (int(submissionId),),
    )
    if sentryLog is None:
        return
    await setSentryLogStatus(
        sentryLogId=int(sentryLog.get("sentryLogId") or 0),
        status=str(status or "").strip().upper(),
        reviewerId=int(reviewerId or 0),
        note=str(note or "").strip(),
    )

async def createEventSubmission(
    *,
    eventRecordId: int,
    event: dict[str, Any],
    submitterId: int,
    imageUrls: list[str] | None,
    evidenceMessageUrl: str
) -> int:
    submissionId = await createSubmission(
        guildId=int(event["guildId"]),
        channelId=int(event["channelId"]),
        submitterId=int(submitterId),
        submissionType="EVENT_RECORD",
        targetUserId=int(event.get("hostUserId") or 0),
        eventDate=event.get("eventDate") or "",
        metadata={
            "eventRecordId": int(eventRecordId),
            "durationMinutes": int(event.get("durationMinutes") or 0),
            "imageUrls": list(imageUrls or []),
            "evidenceMessageUrl": str(evidenceMessageUrl or "").strip(),
        }
    )
    await execute("""UPDATE hg_event_records SET submissionId = ? WHERE eventRecordId = ? """, (submissionId, int(eventRecordId)))

async def createEventRecord(
    *,
    guildId: int,
    eventType: str,
    eventTitle: str = "",
    eventDate: str = "",
    hostUserId: int = 0,
    attendeeCount: int = 0,
    metadata: object = None,
    createdById: int = 0,
) -> int:
    timestamp = datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return await executeReturnId(
        """
        INSERT INTO hg_event_records
            (
                guildId, eventType, eventTitle, eventDate, hostUserId,
                attendeeCount, metadataJson, createdBy, startedAt
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guildId),
            _eventType(eventType),
            str(eventTitle or "").strip(),
            str(eventDate or "").strip(),
            int(hostUserId or 0),
            int(attendeeCount or 0),
            _jsonText(metadata),
            int(createdById or 0),
            timestamp
        ),
    )


async def setEventRecordMessageId(eventRecordId: int, messageId: int) -> None:
    await execute(
        """
        UPDATE hg_event_records
        SET messageId = ?, updatedAt = datetime('now')
        WHERE eventRecordId = ?
        """,
        (int(messageId or 0), int(eventRecordId)),
    )


async def updateEventRecordStatus(eventRecordId: int, status: str) -> None:
    if status in {"FINISHED", "CANCELED"}:
        await execute(
            "UPDATE hg_event_records SET status = ?, finishedAt = datetime('now') WHERE eventRecordId = ?",
            (status, eventRecordId),
        )
    else:
        await execute(
            "UPDATE hg_event_records SET status = ?, finishedAt = NULL WHERE eventRecordId = ?",
            (status, eventRecordId),
        )

async def listOpenEventSessions() -> List[Dict]:
    records = await fetchAll(
        """
        SELECT *
        FROM hg_event_records
        WHERE status = 'OPEN'
        ORDER BY createdAt ASC, eventRecordId ASC
        """,
    )
    return records

async def listHonorGuardAttendees(eventRecordId: int) -> List[Dict]:
    return await fetchAll(
        """
        SELECT *
        FROM hg_attendance_records
        WHERE eventRecordId = ?
        ORDER BY createdAt ASC, recordId ASC
        """,
        (int(eventRecordId),),
    )

async def getEventSubmission(submissionId: int) -> Optional[dict[str, Any]]:
    submission = await getSubmission(int(submissionId))
    if submission is None:
        return None
    if str(submission.get("submissionType") or "").strip().upper() != "EVENT_RECORD":
        return None

    enriched = dict(submission)
    return enriched



async def getEventRecord(eventRecordId: int) -> Optional[dict[str, Any]]:
    record = await fetchOne(
        "SELECT * FROM hg_event_records WHERE eventRecordId = ?",
        (int(eventRecordId),),
    )
    if record is None:
        return None
    enriched = dict(record)
    enriched["startedAt"] = datetime.fromisoformat(str(record.get("createdAt") or "")).astimezone(datetime.timezone.utc)

    return enriched


async def syncEventRecordToSheets(eventRecordId: int) -> dict[str, Any]:
    ##NOT USED ATM
    record = await getEventRecord(int(eventRecordId))
    if record is None:
        raise ValueError(f"Honor Guard event record not found: {eventRecordId}")

    from features.staff.honorGuard import sheets as honorGuardSheets

    hostText = str(record.get("hostRobloxUsername") or "").strip()
    if not hostText and int(record.get("hostUserId") or 0) > 0:
        hostText = str(int(record.get("hostUserId") or 0))
    metadata = _jsonDict(record.get("metadataJson"))
    scheduleEventId = str(metadata.get("scheduleEventId") or metadata.get("eventId") or "").strip()
    eventType = str(record.get("eventType") or "").strip()
    eventTitle = str(record.get("eventTitle") or "").strip()
    eventDetail = str(metadata.get("eventDetail") or eventTitle).strip()
    honorGuardSheets.archiveEvent(
        honorGuardSheets.HonorGuardArchiveRecord(
            eventType=eventType,
            eventTimeUtc=str(record.get("eventDate") or "").strip(),
            eventTitle=eventTitle,
            host=hostText,
            coHosts=str(metadata.get("coHosts") or "").strip(),
            supervisors=str(metadata.get("supervisors") or "").strip(),
            eventDuration=str(metadata.get("eventDuration") or "").strip(),
            eventDetail=eventDetail,
            attendeeCount=int(record.get("attendeeCount") or 0),
            notes=str(metadata.get("notes") or "").strip(),
            eventId=scheduleEventId,
        )
    )
    eventHostUpdate = None
    if hostText:
        eventHostUpdate = honorGuardSheets.incrementEventHostStats(host=hostText, eventType=eventType)
    return {
        "eventRecordId": int(eventRecordId),
        "archiveSynced": True,
        "eventHostUpdate": eventHostUpdate,
    }


async def syncApprovedSubmissionToSheet(submissionId: int) -> dict[str, Any]:
    submission = await getSubmission(int(submissionId))
    if submission is None:
        raise ValueError(f"Honor Guard submission not found: {submissionId}")
    if _normalizeStatus(submission.get("status")) != "APPROVED":
        raise ValueError("Only approved Honor Guard submissions can be synced.")
    if int(submission.get("sheetSynced") or 0):
        await ensurePointAwardRecordsForSubmission(
            submission=submission,
            sheetSynced=True,
        )
        if str(submission.get("submissionType") or "").strip().upper() == "SOLO_SENTRY":
            await execute(
                """
                UPDATE hg_sentry_logs
                SET sheetSynced = 1
                WHERE submissionId = ?
                """,
                (int(submissionId),),
            )
        return {"alreadySynced": True, "submissionId": int(submissionId)}

    from features.staff.honorGuard import sheets as honorGuardSheets

    if str(submission.get("submissionType") or "").strip().upper() in {"POINT_AWARD", "SOLO_SENTRY"}:
        lookup = await robloxUsers.fetchRobloxUser(
            int(submission.get("targetUserId") or 0),
            int(submission.get("guildId") or 0)
        )
        targetRobloxUsername = str(lookup.robloxUsername or "").strip()

        updateResult = honorGuardSheets.applyMemberPointDeltas(
            discordId=int(submission.get("targetUserId") or 0),
            robloxUsername=targetRobloxUsername,
            quotaDelta=float(submission.get("quotaPoints") or 0),
            promotionEventDelta=float(submission.get("promotionEventPoints") or 0),
            promotionAwardedDelta=float(submission.get("promotionAwardedPoints") or 0),
        )
    else:
        ## Maybe in the future also use a batch writer
        for attendanceRecord in await listHonorGuardAttendees(int(submission.get("metadataJson", {}) or {}).get("eventRecordId", 0)):
            lookup = await robloxUsers.fetchRobloxUser(
                int(attendanceRecord.get("targetUserId") or 0),
                int(submission.get("guildId") or 0)
            )
            targetRobloxUsername = str(lookup.robloxUsername or "").strip()
            honorGuardSheets.applyMemberPointDeltas(
                discordId=int(attendanceRecord.get("targetUserId") or 0),
                robloxUsername=targetRobloxUsername,
                quotaDelta=attendanceRecord.get("quotaPoints") or 0,
                promotionEventDelta=attendanceRecord.get("promotionEventPoints") or 0,
            )

    await execute(
        """
        UPDATE hg_submissions
        SET sheetSynced = 1,
            appliedAt = datetime('now'),
            updatedAt = datetime('now')
        WHERE submissionId = ?
        """,
        (int(submissionId),),
    )
    await ensurePointAwardRecordsForSubmission(
        submission={**submission, "sheetSynced": 1},
        sheetSynced=True,
    )
    if str(submission.get("submissionType") or "").strip().upper() == "SOLO_SENTRY":
        await execute(
            """
            UPDATE hg_sentry_logs
            SET sheetSynced = 1
            WHERE submissionId = ?
            """,
            (int(submissionId),),
        )
    await _appendSubmissionEvent(
        submissionId=int(submissionId),
        actorId=int(submission.get("reviewerId") or 0),
        eventType="SHEET_SYNCED",
        fromStatus="APPROVED",
        toStatus="APPROVED",
        details={
            "row": updateResult.row,
            "quotaPoints": updateResult.quotaPoints,
            "promotionTotalPoints": updateResult.promotionTotalPoints,
        },
    )
    return {
        "alreadySynced": False,
        "submissionId": int(submissionId),
        "row": updateResult.row,
        "robloxUsername": updateResult.robloxUsername,
        "quotaPoints": updateResult.quotaPoints,
        "promotionEventPoints": updateResult.promotionEventPoints,
        "promotionAwardedPoints": updateResult.promotionAwardedPoints,
        "promotionTotalPoints": updateResult.promotionTotalPoints,
        "activityStatus": updateResult.activityStatus,
    }
