from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
import time

import config
from features.staff.orbat.a1 import cellRange, columnIndex, indexToColumn
from features.staff.orbat.multiEngine import getMultiOrbatEngine


_memberSheetKey = "honorGuard_members"
_archiveSheetKey = "honorGuard_archive"
_eventHostsSheetKey = "honorGuard_eventHosts"
_engine = getMultiOrbatEngine()
_rowLookupCache: dict[tuple[str, str, str, str], tuple[float, dict[str, int]]] = {}


@dataclass(slots=True, frozen=True)
class HonorGuardMemberColumns:
    discordId: str
    robloxUsername: str
    rank: str
    activityStatus: str
    quotaPoints: str
    eventPoints: str
    platoonPoints: str
    awardedPoints: str
    totalPoints: str
    juniorExamPassed: str
    ncoExamPassed: str


@dataclass(slots=True, frozen=True)
class HonorGuardMemberRow:
    row: int
    discordId: int
    robloxUsername: str
    rank: str
    activityStatus: str
    quotaPoints: float
    eventPoints: float
    platoonPoints: float
    awardedPoints: float
    totalPoints: float
    juniorExamPassed: str
    ncoExamPassed: str


@dataclass(slots=True, frozen=True)
class HonorGuardMemberPointUpdate:
    row: int
    robloxUsername: str
    previousQuotaPoints: float
    quotaPoints: float
    previousEventPoints: float
    eventPoints: float
    previousPlatoonPoints: float
    platoonPoints: float
    previousAwardedPoints: float
    awardedPoints: float
    previousTotalPoints: float
    totalPoints: float
    activityStatus: str
    passedJGE: bool
    passedNCOE: bool

@dataclass(slots=True, frozen=True)
class HonorGuardMemberPlatoonUpdate:
    row: int
    robloxUsername: str
    previousEventPoints: float
    eventPoints: float

@dataclass(slots=True, frozen=True)
class HonorGuardArchiveRecord:
    eventType: str = ""
    eventTimeUtc: str = ""
    eventTitle: str = ""
    host: str = ""
    coHosts: str = ""
    supervisors: str = ""
    eventDuration: str = ""
    eventDetail: str = ""
    attendeeCount: int = 0
    notes: str = ""
    eventId: str = ""


@dataclass(slots=True, frozen=True)
class HonorGuardEventHostUpdate:
    row: int
    host: str
    eventType: str
    column: str
    previousValue: int
    value: int

@dataclass(slots=True)
class HonorGuardMemberColumnsBatch:
    discordId: int
    robloxUsername: str
    rank: str
    quotaPoints: float
    activityStatus: str
    eventPoints: float
    awardedPoints: float
    totalPoints: float
    juniorExamPassed: bool
    ncoExamPassed: bool
    promotionEligible: bool

@dataclass(slots=True)
class HonorGuardPlatoonColumnsBatch:
    discordId: int
    robloxUsername: str
    rank: str
    platoonPoints: float

@dataclass(slots=True)
class ApprovedLogUpdate:
    robloxUsername: str
    discordId: int
    platoon: str
    quotaDelta: float
    eventDelta: float
    platoonDelta: float
    awardedDelta: float
    juniorExamPassed: bool | None
    ncoExamPassed: bool | None

@dataclass(slots=True)
class HonorGuardMemberRowBatch:
    row: int
    discordId: int
    robloxUsername: str
    rank: str
    quotaPoints: float
    activityStatus: str
    eventPoints: float
    awardedPoints: float
    totalPoints: float
    juniorExamPassed: bool
    ncoExamPassed: bool
    promotionEligible: bool

@dataclass(slots=True)
class HonorGuardPlatoonRowBatch:
    row: int
    discordId: int
    robloxUsername: str
    rank: str
    platoonPoints: float

@dataclass(slots=True)
class MemberResolvedUpdate:
    member: HonorGuardMemberRowBatch
    update: ApprovedLogUpdate

@dataclass(slots=True)
class PlatoonResolvedUpdate:
    platoon: HonorGuardPlatoonRowBatch
    update: ApprovedLogUpdate

def _normalizeColumn(value: object) -> str:
    return str(value or "").strip().upper()


def _normalizeKey(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _normalizeUsername(value: object) -> str:
    return "".join(ch for ch in str(value or "").strip().casefold() if not ch.isspace())


def _toInt(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default


def _toFloat(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return default

def _toBool(value: object, default: bool = False) -> bool:
    text = str(value or "").strip().upper()
    if text == "TRUE":
        return True
    if text == "FALSE":
        return False
    return default

def _pointValue(value: float) -> int | float:
    numeric = float(value or 0)
    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 3)


def _sheetName(sheetKey: str) -> str:
    return _engine.getSheetName(sheetKey)


def _sheetAvailable(sheetKey: str) -> bool:
    try:
        _engine.getSheetConfig(sheetKey)
        return True
    except KeyError:
        return False
    
def _range(sheetKey, col: str, row: int) -> str:
    return cellRange(_sheetName(sheetKey=sheetKey), col, row)


def configuredSheetKeys() -> tuple[str, ...]:
    return tuple(
        key
        for key in (_memberSheetKey, _archiveSheetKey, _eventHostsSheetKey)
        if _sheetAvailable(key)
    )

def _rowLookupCacheTtlSec() -> float:
    try:
        value = float(getattr(config, "recruitmentRowLookupCacheTtlSec", 120) or 120)
    except (TypeError, ValueError):
        value = 120.0
    return max(0.0, value)

def _isWritableMemberRow(usernameCell: str, rankCell: str) -> bool:
    return isHonorGuardOrbatLabel(usernameCell) and isAllowedHonorGuardRank(rankCell)

def _isWritablePlatoonRow(usernameCell: str, rankCell: str, platoon) -> bool:
    return isHonorGuardOrbatLabel(usernameCell) and isAllowedPlatoonRank(rankCell, platoon)

def normalize(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

def isHonorGuardOrbatLabel(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalize(text)
    if normalized in {"robloxusername", "robloxuser", "ruser"}:
        return False
    lowered = text.lower()
    if "personnel" in lowered:
        return False
    sectionHeaders = {
        normalize(item) for item in (getattr(config, "honorGuardMemberSectionHeaders", []) or getattr(config, "honorGuardPlatoonSectionHeaders")) if item
    }
    if normalized in sectionHeaders:
        return False
    return True

def isAllowedHonorGuardRank(value: str) -> bool:
    rank = str(value or "").strip()
    if not rank:
        return False
    allowed = getattr(config, "honorGuardAllowedRanks", []) or []
    allowedSet = {normalize(item) for item in allowed if item}
    return normalize(rank) in allowedSet

def isAllowedPlatoonRank(value: str, platoon: str) -> bool:
    platoonNorm = platoon.strip().casefold()
    rank = str(value or "").strip()
    if not rank:
        return False

    allowedMap = {
        normalize(k): v
        for k, v in getattr(config, "honorGuardPlatoonAllowedRanks", {}).items()
    }
    allowed = allowedMap.get(platoonNorm, [])

    allowedSet = {normalize(item) for item in allowed if item}
    return normalize(rank) in allowedSet

def configurationProblems(*, configModule: Any = config) -> tuple[str, ...]:
    problems: list[str] = []
    if not bool(getattr(configModule, "honorGuardEnabled", False)):
        problems.append("honorGuardEnabled is false")
    if not str(getattr(configModule, "honorGuardSpreadsheetId", "") or "").strip():
        problems.append("honorGuardSpreadsheetId is not set")
    if not str(getattr(configModule, "honorGuardMemberSheetName", "") or "").strip():
        problems.append("honorGuardMemberSheetName is not set")
    if not str(getattr(configModule, "honorGuardArchiveSheetName", "") or "").strip():
        problems.append("honorGuardArchiveSheetName is not set")
    if not str(getattr(configModule, "honorGuardEventHostsSheetName", "") or "").strip():
        problems.append("honorGuardEventHostsSheetName is not set")
    if not _sheetAvailable(_memberSheetKey):
        problems.append("Honor Guard member sheet is not registered")
    if not _sheetAvailable(_archiveSheetKey):
        problems.append("Honor Guard archive sheet is not registered")
    if not _sheetAvailable(_eventHostsSheetKey):
        problems.append("Honor Guard event hosts sheet is not registered")
    return tuple(problems)


def loadMemberColumns(*, configModule: Any = config) -> HonorGuardMemberColumns:
    return HonorGuardMemberColumns(
        discordId=_normalizeColumn(getattr(configModule, "honorGuardDiscordIdColumn", "A")),
        robloxUsername=_normalizeColumn(getattr(configModule, "honorGuardRobloxUsernameColumn", "B")),
        rank=_normalizeColumn(getattr(configModule, "honorGuardRankColumn", "C")),
        activityStatus=_normalizeColumn(getattr(configModule, "honorGuardActivityStatusColumn", "D")),
        quotaPoints=_normalizeColumn(getattr(configModule, "honorGuardQuotaPointsColumn", "E")),
        eventPoints=_normalizeColumn(getattr(configModule, "honorGuardEventPointsColumn", "F")),
        platoonPoints=_normalizeColumn(getattr(configModule, "honorGuardTotalPlatoonPointsColumn", "G")),
        awardedPoints=_normalizeColumn(getattr(configModule, "honorGuardAwardedPointsColumn", "H")),
        totalPoints=_normalizeColumn(getattr(configModule, "honorGuardTotalPointsColumn", "M")),
        juniorExamPassed=_normalizeColumn(getattr(configModule, "honorGuardJuniorExamPassedColumn", "J")),
        ncoExamPassed=_normalizeColumn(getattr(configModule, "honorGuardNcoExamPassedColumn", "K")),
    )

def loadMemberColumnsBatch(*, configModule: Any = config) -> HonorGuardMemberColumnsBatch:
    return HonorGuardMemberColumnsBatch(
        discordId=_normalizeColumn(getattr(configModule, "honorGuardDiscordIdColumn", "A")),
        robloxUsername=_normalizeColumn(getattr(configModule, "honorGuardRobloxUsernameColumn", "B")),
        rank=_normalizeColumn(getattr(configModule, "honorGuardRankColumn", "C")),
        quotaPoints=_normalizeColumn(getattr(configModule, "honorGuardQuotaPointsColumn", "E")),
        activityStatus=_normalizeColumn(getattr(configModule, "honorGuardActivityStatusColumn", "D")),
        eventPoints=_normalizeColumn(getattr(configModule, "honorGuardEventPointsColumn", "F")),
        awardedPoints=_normalizeColumn(getattr(configModule, "honorGuardAwardedPointsColumn", "G")),
        totalPoints=_normalizeColumn(getattr(configModule, "honorGuardTotalPointsColumn", "H")),
        juniorExamPassed=_normalizeColumn(getattr(configModule, "honorGuardJuniorExamPassedColumn", "J")),
        ncoExamPassed=_normalizeColumn(getattr(configModule, "honorGuardNcoExamPassedColumn", "K")),
        promotionEligible=_normalizeColumn(getattr(configModule, "honorGuardPromotionEligibleFormulaColumn", "Q")),
    )

def loadPlatoonColumnsBatch(*, configModule: Any = config) -> HonorGuardPlatoonColumnsBatch:
    return HonorGuardPlatoonColumnsBatch(
        discordId=_normalizeColumn(getattr(configModule, "honorGuardPlatoonDiscordIdColumn", "A")),
        robloxUsername=_normalizeColumn(getattr(configModule, "honorGuardPlatoonRobloxUsernameColumn", "B")),
        rank=_normalizeColumn(getattr(configModule, "honorGuardPlatoonRankColumn", "C")),
        platoonPoints=_normalizeColumn(getattr(configModule, "honorGuardPlatoonPointsColumn", "E")),
    )

def _columnMap(columns: HonorGuardMemberColumns) -> dict[str, str]:
    return {
        "discordId": columns.discordId,
        "robloxUsername": columns.robloxUsername,
        "rank": columns.rank,
        "activityStatus": columns.activityStatus,
        "quotaPoints": columns.quotaPoints,
        "eventPoints": columns.eventPoints,
        "platoonPoints": columns.platoonPoints,
        "awardedPoints": columns.awardedPoints,
        "totalPoints": columns.totalPoints,
        "juniorExamPassed": columns.juniorExamPassed,
        "ncoExamPassed": columns.ncoExamPassed,
    }


def _readColumn(sheetKey: str, columnLetter: str) -> list[str]:
    if not columnLetter:
        return []
    rows = _engine.getValues(sheetKey, f"{_sheetName(sheetKey)}!{columnLetter}:{columnLetter}")
    return [str(row[0]).strip() if row else "" for row in rows]


def _findRowByNormalizedValue(
    sheetKey: str,
    *,
    columnLetter: str,
    value: object,
    normalizer,
) -> Optional[int]:
    target = normalizer(value)
    if not target:
        return None
    for rowIndex, current in enumerate(_readColumn(sheetKey, columnLetter), start=1):
        if normalizer(current) == target:
            return rowIndex
    return None


def findMemberRow(
    *,
    discordId: int = 0,
    robloxUsername: str = "",
    configModule: Any = config,
) -> Optional[int]:
    columns = loadMemberColumns(configModule=configModule)
    if int(discordId or 0) > 0 and columns.discordId:
        row = _findRowByNormalizedValue(
            _memberSheetKey,
            columnLetter=columns.discordId,
            value=str(int(discordId)),
            normalizer=lambda raw: str(_toInt(raw, default=0)) if _toInt(raw, default=0) > 0 else str(raw).strip(),
        )
        if row:
            return row
    if str(robloxUsername or "").strip() and columns.robloxUsername:
        return _findRowByNormalizedValue(
            _memberSheetKey,
            columnLetter=columns.robloxUsername,
            value=robloxUsername,
            normalizer=_normalizeUsername,
        )
    return None

def findPlatoonRow(robloxUsername: str, sheetKey: str, configModule: Any = config) -> Optional[int]:
    usernameColumn = _normalizeColumn(getattr(configModule, "honorGuardPlatoonUsernameColumn", "A"))
    if not usernameColumn:
        return None
    return _findRowByNormalizedValue(
        sheetKey,
        columnLetter=usernameColumn,
        value=robloxUsername,
        normalizer=_normalizeUsername,
    )


def readMember(
    *,
    discordId: int = 0,
    robloxUsername: str = "",
    row: int = 0,
    configModule: Any = config,
) -> Optional[HonorGuardMemberRow]:
    rowIndex = int(row or 0)
    if rowIndex <= 0:
        resolved = findMemberRow(
            discordId=int(discordId or 0),
            robloxUsername=robloxUsername,
            configModule=configModule,
        )
        if resolved is None:
            return None
        rowIndex = int(resolved)

    columns = loadMemberColumns(configModule=configModule)
    values = _engine.readRowColumns(
        _memberSheetKey,
        row=rowIndex,
        columnMap=_columnMap(columns),
    )
    event = _toFloat(values.get("eventPoints"))
    platoon = _toFloat(values.get("platoonPoints"))
    awarded = _toFloat(values.get("awardedPoints"))
    total = _toFloat(values.get("totalPoints"), event + platoon + awarded)
    return HonorGuardMemberRow(
        row=rowIndex,
        discordId=_toInt(values.get("discordId")),
        robloxUsername=str(values.get("robloxUsername") or "").strip(),
        rank=str(values.get("rank") or "").strip(),
        activityStatus=str(values.get("activityStatus") or "").strip(),
        quotaPoints=_toFloat(values.get("quotaPoints")),
        eventPoints=event,
        platoonPoints=platoon,
        awardedPoints=awarded,
        totalPoints=total,
        juniorExamPassed=str(values.get("juniorExamPassed") or "").strip(),
        ncoExamPassed=str(values.get("ncoExamPassed") or "").strip(),
    )

def _isExcuseStatus(value: object, *, configModule: Any = config) -> bool:
    statusKey = _normalizeKey(value)
    if not statusKey:
        return False
    configured = getattr(configModule, "honorGuardExcuseStatusValues", []) or []
    return statusKey in {_normalizeKey(item) for item in configured}

def applyMemberPointDeltas(
    *,
    discordId: int = 0,
    robloxUsername: str = "",
    quotaDelta: float = 0,
    eventDelta: float = 0,
    platoonDelta: float = 0,
    awardedDelta: float = 0,
    passedJGE: bool = False,
    passedNCOE: bool = False,
    promoteWhenEligible: bool = True,
    markActiveWhenEarlyQuotaMet: bool = True,
    configModule: Any = config,
) -> HonorGuardMemberPointUpdate:
    member = readMember(discordId=discordId, robloxUsername=robloxUsername, configModule=configModule)
    if member is None:
        lookup = robloxUsername or str(discordId or "")
        raise ValueError(f"Honor Guard member not found in sheet: {lookup}")

    columns = loadMemberColumns(configModule=configModule)
    nextQuota = max(0.0, float(member.quotaPoints) + float(quotaDelta or 0))
    nextEvent = max(0.0, float(member.eventPoints) + float(eventDelta or 0))
    nextPlatoon = max(0.0, float(member.platoonPoints) + float(platoonDelta or 0))
    nextAwarded = max(0.0, float(member.awardedPoints) + float(awardedDelta or 0))
    nextTotal = nextEvent + nextPlatoon + nextAwarded

    status = member.activityStatus
    earlyQuota = float(getattr(configModule, "honorGuardEarlyActiveQuotaPoints", 8) or 8)
    if (
        markActiveWhenEarlyQuotaMet
        and columns.activityStatus
        and nextQuota >= earlyQuota
        and not _isExcuseStatus(status, configModule=configModule)
    ):
        status = "Active"

    updates: dict[str, tuple[str, Any]] = {}
    if columns.quotaPoints:
        updates["quotaPoints"] = (columns.quotaPoints, _pointValue(nextQuota))
    if columns.eventPoints:
        updates["eventPoints"] = (columns.eventPoints, _pointValue(nextEvent))
    if columns.platoonPoints:
        updates["platoonPoints"] = (columns.platoonPoints, _pointValue(nextPlatoon))
    if columns.awardedPoints:
        updates["awardedPoints"] = (columns.awardedPoints, _pointValue(nextAwarded))
    if columns.totalPoints:
        updates["totalPoints"] = (columns.totalPoints, _pointValue(nextTotal))
    if columns.activityStatus and status != member.activityStatus:
        updates["activityStatus"] = (columns.activityStatus, status)
    if columns.juniorExamPassed and passedJGE and str(member.juniorExamPassed).strip().lower() != True:
        updates["juniorExamPassed"] = (columns.juniorExamPassed, True)
    if columns.ncoExamPassed and passedNCOE and str(member.ncoExamPassed).strip().lower() != True:
        updates["ncoExamPassed"] = (columns.ncoExamPassed, True)

    _engine.writeRowColumns(_memberSheetKey, row=member.row, columnValues=updates)
    return HonorGuardMemberPointUpdate(
        row=member.row,
        robloxUsername=member.robloxUsername,
        previousQuotaPoints=member.quotaPoints,
        quotaPoints=nextQuota,
        previousEventPoints=member.eventPoints,
        eventPoints=nextEvent,
        previousPlatoonPoints=member.platoonPoints,
        platoonPoints=nextPlatoon,
        previousAwardedPoints=member.awardedPoints,
        awardedPoints=nextAwarded,
        previousTotalPoints=member.totalPoints,
        totalPoints=nextTotal,
        activityStatus=status,
        passedJGE=passedJGE and str(member.juniorExamPassed).strip().lower() != True,
        passedNCOE=passedNCOE and str(member.ncoExamPassed).strip().lower() != True,
    )

def applyMemberPlatoonPoints(
    *,
    platoon: str,
    discordId: int = 0,
    robloxUsername: str = "",
    eventDelta: float = 0,
    configModule: Any = config,
) -> HonorGuardMemberPlatoonUpdate:
    sheetKey = f"honorGuard_platoon_{_normalizeKey(platoon)}"
    row = findPlatoonRow(robloxUsername, sheetKey, configModule=configModule )
    column = _normalizeColumn(getattr(configModule, "honorGuardPlatoonPointsColumn", "D"))
    if row is None:
        return HonorGuardMemberPlatoonUpdate(
            row=0,
            robloxUsername=robloxUsername,
            previousEventPoints=0,
            eventPoints=0,
        )

    rangeA1 = f"{_sheetName(sheetKey)}!{column}{row}:{column}{row}"
    values = _engine.getValues(sheetKey, rangeA1)
    previousValue = _toInt(values[0][0] if values and values[0] else 0)
    nextValue = max(0, previousValue + int(eventDelta or 0))
    _engine.writeRowColumns(
        sheetKey,
        row=row,
        columnValues={"platoonPoints": (column, nextValue)},
    )
    return HonorGuardMemberPlatoonUpdate(
        row=row,
        robloxUsername=robloxUsername,
        previousEventPoints=previousValue,
        eventPoints=nextValue,
    )

def archiveEvent(record: HonorGuardArchiveRecord, *, configModule: Any = config) -> dict[str, Any]:
    columns = list(getattr(configModule, "honorGuardArchiveColumns", []) or [])
    if not columns:
        columns = [
            "eventType",
            "eventTimeUtc",
            "host",
            "coHosts",
            "supervisors",
            "eventDuration",
            "eventDetail",
            "notes",
        ]
    valuesByKey = {
        "eventType": record.eventType.title(),
        "eventTimeUtc": record.eventTimeUtc,
        "eventDate": record.eventTimeUtc,
        "eventTitle": record.eventTitle,
        "host": record.host,
        "coHosts": record.coHosts,
        "supervisors": record.supervisors,
        "eventDuration": record.eventDuration,
        "eventDetail": record.eventDetail,
        "attendeeCount": int(record.attendeeCount or 0),
        "notes": record.notes,
        "eventId": record.eventId,
    }
    rowValues = [valuesByKey.get(str(key or "").strip(), "") for key in columns]
    return _engine.appendValues(
        _archiveSheetKey,
        rangeA1=f"{_sheetName(_archiveSheetKey)}!A:A",
        values=[rowValues],
    )


def _eventHostColumnForEventType(eventType: str, *, configModule: Any = config) -> str:
    eventKey = _normalizeKey(eventType)
    if not eventKey:
        return ""

    configured = getattr(configModule, "honorGuardEventHostEventTypeColumns", {}) or {}
    if isinstance(configured, dict):
        for rawKey, rawColumn in configured.items():
            if _normalizeKey(rawKey) == eventKey:
                return _normalizeColumn(rawColumn)

    examsColumn = _normalizeColumn(getattr(configModule, "honorGuardEventHostExamsColumn", "G"))
    trainingsColumn = _normalizeColumn(getattr(configModule, "honorGuardEventHostTrainingsColumn", "H"))
    tryoutsColumn = _normalizeColumn(getattr(configModule, "honorGuardEventHostTryoutsColumn", "I"))
    inspectionsColumn = _normalizeColumn(getattr(configModule, "honorGuardEventHostInspectionsColumn", "J"))

    fallback: dict[str, str] = {
        "jge": examsColumn,
        "juniorguardsmanexam": examsColumn,
        "ncoexam": examsColumn,
        "exam": examsColumn,
        "examination": examsColumn,
        "orientation": trainingsColumn,
        "training": trainingsColumn,
        "lecture": trainingsColumn,
        "drill": trainingsColumn,
        "tryout": tryoutsColumn,
        "honorguardtryout": tryoutsColumn,
        "inspection": inspectionsColumn,
        "mockinspection": inspectionsColumn,
    }
    return fallback.get(eventKey, "")


def findEventHostRow(host: str, *, configModule: Any = config) -> Optional[int]:
    usernameColumn = _normalizeColumn(getattr(configModule, "honorGuardEventHostUsernameColumn", "A"))
    if not usernameColumn:
        return None
    return _findRowByNormalizedValue(
        _eventHostsSheetKey,
        columnLetter=usernameColumn,
        value=host,
        normalizer=_normalizeUsername,
    )


def incrementEventHostStats(
    *,
    host: str,
    eventType: str,
    delta: int = 1,
    configModule: Any = config,
) -> Optional[HonorGuardEventHostUpdate]:
    column = _eventHostColumnForEventType(eventType, configModule=configModule)
    if not column:
        return None

    row = findEventHostRow(host, configModule=configModule)
    if row is None:
        return None

    rangeA1 = f"{_sheetName(_eventHostsSheetKey)}!{column}{row}:{column}{row}"
    values = _engine.getValues(_eventHostsSheetKey, rangeA1)
    previousValue = _toInt(values[0][0] if values and values[0] else 0)
    nextValue = max(0, previousValue + int(delta or 0))
    _engine.writeRowColumns(
        _eventHostsSheetKey,
        row=row,
        columnValues={"eventHostStat": (column, nextValue)},
    )
    return HonorGuardEventHostUpdate(
        row=row,
        host=str(host or "").strip(),
        eventType=str(eventType or "").strip(),
        column=column,
        previousValue=previousValue,
        value=nextValue,
    )


def memberSheetLastConfiguredColumn(*, configModule: Any = config) -> str:
    columns = loadMemberColumns(configModule=configModule)
    indexes = [columnIndex(col) for col in _columnMap(columns).values() if col]
    return indexToColumn(max(indexes, default=1))

def _aggregateApprovedLogUpdates(updates: list[dict]) -> dict[str, ApprovedLogUpdate]:
    aggregate: dict[str, ApprovedLogUpdate] = {}
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        username = str(raw.get("robloxUsername") or "").strip()
        if not username:
            continue
        discordId = int(raw.get("userId") or 0)
        if not discordId:
            continue
        if str(raw.get("platoon") or "NONE") != "NONE":
            try:
                platoon = str(raw.get("platoon") or "NONE").strip().upper()
            except (TypeError, ValueError):
                platoon = None
            try:
                platoonDelta = float(raw.get("platoonDelta") or 0)
            except (TypeError, ValueError):
                platoonDelta = 0
        else:
            platoon = "NONE"
            platoonDelta = 0
            try:
                eventDelta = float(raw.get("eventDelta") or 0)
            except (TypeError, ValueError):
                eventDelta = 0
        try:
            quotaDelta = float(raw.get("quotaDelta") or 0)
        except (TypeError, ValueError):
            quotaDelta = 0
        try:
            eventDelta = float(raw.get("eventDelta") or 0)
        except (TypeError, ValueError):
            eventDelta = 0
        try: 
            awardedDelta = float(raw.get("awardedDelta"))
        except (TypeError, ValueError):
            awardedDelta = 0
        if raw.get("juniorExamPassed") is None:
            juniorExamPassed = None
        else:
            try:
                juniorExamPassed = str(raw.get("juniorExamPassed", "")).upper() == 'PASS'
            except (TypeError, ValueError):
                juniorExamPassed = None
        if raw.get("ncoExamPassed") is None:
            ncoExamPassed = None
        else:
            try:
                ncoExamPassed = str(raw.get("ncoExamPassed", "")).upper() == 'PASS'
            except (TypeError, ValueError):
                ncoExamPassed = None

        key = username.casefold()
        slot = aggregate.get(key)
        if slot is None:
            aggregate[key] = ApprovedLogUpdate(
                robloxUsername = username,
                discordId = discordId,
                platoon = platoon,
                quotaDelta = quotaDelta,
                eventDelta = eventDelta,
                platoonDelta = platoonDelta,
                awardedDelta = awardedDelta,
                juniorExamPassed = juniorExamPassed,
                ncoExamPassed = ncoExamPassed,
            )
        else:
            slot.quotaDelta += quotaDelta
            slot.eventDelta += eventDelta
            slot.platoonDelta += platoonDelta
            slot.awardedDelta += awardedDelta
            if juniorExamPassed:
                slot.juniorExamPassed = True
            if ncoExamPassed:
                slot.ncoExamPassed = True
    return aggregate

def _loadOrbatData(
        platoon: str,
        *,
        configModule: Any = config,
    ) -> dict[str, HonorGuardMemberRowBatch]:
    columns = loadMemberColumnsBatch(configModule=configModule)
    platoonColumns = loadPlatoonColumnsBatch(configModule=configModule)

    rangesAll = [
        f"{_sheetName(_memberSheetKey)}!{columns.robloxUsername}:{columns.promotionEligible}",
    ]
    if platoon.strip().upper() != "NONE":
        sheetKey = f"honorGuard_platoon_{_normalizeKey(platoon)}"
        sheetName = _sheetName(sheetKey)
        rangesAllPlatoon = (
            f"{sheetName}!{platoonColumns.robloxUsername}:{platoonColumns.platoonPoints}"
        )

    rawOrbatData = _engine.batchGetValues(_memberSheetKey, rangesAll)
    unpackedMemberData = rawOrbatData[0]["values"]
    orbatData: dict[str, HonorGuardMemberRowBatch] = {}
    platoonData: dict[str, HonorGuardPlatoonRowBatch] = {}
    if platoon != "NONE":
        rawPlatoonData = _engine.batchGetValues(sheetKey, rangesAllPlatoon)
        unpackedPlatoonData = rawPlatoonData[0]["values"]

    for rowNumber, row in enumerate(unpackedMemberData, start=1):
        username = str(row[0]).strip() if len(row) > 0 else ""
        rank = str(row[1]).strip() if len(row) > 1 else ""

        if not _isWritableMemberRow(username, rank):
            continue

        orbatData[username.casefold()] = HonorGuardMemberRowBatch(
            row = rowNumber,
            discordId=0,
            robloxUsername=username,
            rank = rank,
            quotaPoints = _toFloat(row[4]) if len(row) > 4 else 0.0,
            activityStatus = str(row[6]).strip() if len(row) >= 6 else 'Inactive',
            eventPoints = _toFloat(row[9]) if len(row) > 9 else 0.0,
            awardedPoints = _toFloat(row[10]) if len(row) > 10 else 0.0,
            totalPoints = _toFloat(row[12]) if len(row) > 12 else 0.0,
            juniorExamPassed = _toBool(row[13]) if len(row) > 13 else False,
            ncoExamPassed = _toBool(row[14]) if len(row) > 14 else False,
            promotionEligible = _toBool(row[15]) if len(row) > 15 else False,
        )


    if platoon != "NONE":
        for rowNumber, row in enumerate(unpackedPlatoonData, start=1):
            username = str(row[0]).strip() if len(row) > 0 else ""
            rank = str(row[1]).strip() if len(row) > 1 else ""
            
            if not _isWritablePlatoonRow(username, rank, platoon):
                continue

            platoonData[username.casefold()] = HonorGuardPlatoonRowBatch(
                row = rowNumber,
                discordId = 0,
                robloxUsername=username,
                rank = rank,
                platoonPoints = _toFloat(row[3]) if len(row) > 3 else 0.0,
            )
    return orbatData, platoonData

def _resolveApprovedLogRows(
    aggregate: dict[str, ApprovedLogUpdate],
    orbatMembers: dict[str, HonorGuardMemberRowBatch],
    orbatPlatoon: dict[str, HonorGuardPlatoonRowBatch] | None = None,
) -> tuple[dict[int, MemberResolvedUpdate], dict[int, PlatoonResolvedUpdate]]:
    updatesByRowPlatoon: dict[int, PlatoonResolvedUpdate] = {}
    updatesByRowMember: dict[int, MemberResolvedUpdate] = {}
    for username, update in aggregate.items():
        member = orbatMembers.get(username)
        if member is None:
            print(f"ERROR | Username {username} not found in orbat.")
            continue
        updatesByRowMember[member.row] = MemberResolvedUpdate(
            member = member,
            update = update,
        )
        if orbatPlatoon is not None and update.platoonDelta != 0:
            platoonMember = orbatPlatoon.get(username)
            if platoonMember is None:
                print(f"ERROR | Username {username} not found in platoon orbat.")
                continue
            updatesByRowPlatoon[platoonMember.row] = PlatoonResolvedUpdate(
                platoon=platoonMember,
                update=update,
            )

    return updatesByRowMember, updatesByRowPlatoon

def _buildApprovedLogBatchData(
    columns: HonorGuardMemberColumnsBatch,
    platoonColumns: HonorGuardPlatoonColumnsBatch,
    updatesByRowMember: dict[int, MemberResolvedUpdate],
    updatesByRowPlatoon: dict[int, PlatoonResolvedUpdate],
    eventPlatoon,
) -> tuple[list[dict], list[int], list[dict], list[int], list[int, dict]]:
    batchDataMember: dict[int, dict[str, tuple[str, Any]]] = {}
    touchedRowsMember: list[int] = []
    batchDataPlatoon: dict[int, dict[str, tuple[str, Any]]] = {}
    touchedRowsPlatoon: list[int] = []
    for row, resolved in updatesByRowMember.items():
        member, update = resolved.member, resolved.update

        nextRank = None
        nextActivityStatus = None
        nextTotalPoints = member.totalPoints + max(0,update.awardedDelta) + max(0,update.eventDelta) + max(0,update.platoonDelta)
        nextQuotaPoints = member.quotaPoints + max(0, update.quotaDelta)
        nextEventPoints = member.eventPoints + max(0, update.eventDelta)
        nextAwardedPoints = member.awardedPoints + max(0, update.awardedDelta)
        nextJuniorExamPassed = None
        nextNcoExamPassed = None
        nextPromotionEligible = None

        if member.activityStatus == 'Inactive' and nextQuotaPoints >= 8:
            nextActivityStatus = 'Active'
        if member.juniorExamPassed != True and update.juniorExamPassed:
            nextJuniorExamPassed = True
        if member.ncoExamPassed != True and update.ncoExamPassed:
            nextNcoExamPassed = True
        if member.rank == "Junior Guardsman" and nextTotalPoints >= 15 and (member.juniorExamPassed or nextJuniorExamPassed):
            nextRank = "Guardsman"
        if member.rank == "Guardsman" and nextTotalPoints >= 50 and (member.ncoExamPassed or nextNcoExamPassed) and (member.activityStatus == 'Active' or nextActivityStatus == 'Active'):
            nextPromotionEligible = True
        if member.rank == "Junior Guardsman" and nextTotalPoints >= 50 and (member.ncoExamPassed or nextNcoExamPassed) and (member.juniorExamPassed or nextJuniorExamPassed) and (member.activityStatus == 'Active' or nextActivityStatus == 'Active'):
            nextRank = "Guardsman"
            nextPromotionEligible = True

        rowData = {}
        if nextRank is not None:
            rowData["rank"] = (columns.rank, nextRank)
        if update.quotaDelta != 0:
            rowData["quotaPoints"] = (columns.quotaPoints, nextQuotaPoints)
        if nextActivityStatus is not None:
            rowData["activityStatus"] = (columns.activityStatus, nextActivityStatus)
        if update.eventDelta != 0:
            rowData["eventPoints"] = (columns.eventPoints, nextEventPoints)
        if update.awardedDelta != 0:
            rowData["awardedPoints"] = (columns.awardedPoints, nextAwardedPoints)
        if nextJuniorExamPassed is not None:
            rowData["juniorExamPassed"] = (columns.juniorExamPassed, nextJuniorExamPassed)
        if nextNcoExamPassed is not None:
            rowData["ncoExamPassed"] = (columns.ncoExamPassed, nextNcoExamPassed)
        if nextPromotionEligible is not None:
            rowData["promotionEligible"] = (columns.promotionEligible, nextPromotionEligible)
        
        batchDataMember[row] = rowData

        touchedRowsMember.append(row)

    for row, resolved in updatesByRowPlatoon.items():
        platoon, update = resolved.platoon, resolved.update
        nextPlatoonPoints = platoon.platoonPoints + max(0, update.platoonDelta)

        rowData = {}
        if nextPlatoonPoints > 0:
            rowData["platoonPoints"] = (platoonColumns.platoonPoints, nextPlatoonPoints)
        batchDataPlatoon[platoon.row] = rowData
        touchedRowsPlatoon.append(row)

    return batchDataMember, touchedRowsMember, batchDataPlatoon, touchedRowsPlatoon

def applyApprovedLogsBatch(
    updates: list[dict],
    eventPlatoon: str,
    *,
    configModule: Any = config,
) -> dict:
    auditLog = []
    if not updates:
        auditLog.append(
            {"error": f"No updates - applyApprovedLogsBatch() stopped. \nUPDATES: {updates}"}
        )
        return auditLog
    configModule = config

    columns = loadMemberColumnsBatch(configModule=configModule)
    platoonColumns = loadPlatoonColumnsBatch(configModule=configModule)

    aggregate = _aggregateApprovedLogUpdates(updates)
    if not aggregate:
        return

    spreadsheetId = str(getattr(config, "honorGuardSpreadsheetId", "") or "").strip()
    if not spreadsheetId:
        auditLog.append(
            {"error": f"Invalid SpreadsheetId - {spreadsheetId}"}
        )
        return

    orbatMembers, orbatPlatoon = _loadOrbatData(eventPlatoon)

    updatesByRowMember, updatesByRowPlatoon = _resolveApprovedLogRows(aggregate, orbatMembers, orbatPlatoon)
    if not updatesByRowMember:
        auditLog.append(
            {"error": "_resolveApprovedLogRows() failed."}
        )
        return auditLog

    batchDataMember, touchedRowsMember, batchDataPlatoon, touchedRowsPlatoon = _buildApprovedLogBatchData(columns, platoonColumns, updatesByRowMember, updatesByRowPlatoon, eventPlatoon)

    _engine.writeRowsColumnsBatch(_memberSheetKey, rows=touchedRowsMember, columnValuesByRow=batchDataMember)
    if len(touchedRowsPlatoon) > 0:
        platoonSheetKey = f"honorGuard_platoon_{_normalizeKey(eventPlatoon)}"
        _engine.writeRowsColumnsBatch(platoonSheetKey, rows=touchedRowsPlatoon, columnValuesByRow=batchDataPlatoon)

    platoonByUsername = {
        p.platoon.robloxUsername.casefold(): p
        for p in updatesByRowPlatoon.values()
    }

    for row, rowData in batchDataMember.items():
        resolved = updatesByRowMember[row]

        member = resolved.member
        update = resolved.update

        attendeePlatoon = platoonByUsername.get(member.robloxUsername.casefold())
        platoonMember = attendeePlatoon.platoon if attendeePlatoon else None

        nextRank = rowData.get("rank", (None, member.rank))[1]
        nextTotalPoints = member.totalPoints + update.awardedDelta + update.eventDelta + update.platoonDelta
        if member.activityStatus == "Inactive" and member.quotaPoints + update.quotaDelta >= 8:
            nextActivityStatus = "Active"
        else:
            nextActivityStatus = member.activityStatus
        nextPromotionEligible = rowData.get(
            "promotionEligible",
            (None, member.promotionEligible),
        )[1]
        nextJuniorExamPassed = rowData.get(
            "juniorExamPassed", (None, member.juniorExamPassed),
        )[1]
        nextNcoExamPassed = rowData.get(
            "ncoExamPassed", (None, member.ncoExamPassed),
        )[1]
        promotion = None

        if member.rank != nextRank:
            promotion = f"Promoted to {nextRank}"
            if nextRank == "Guardsman" and nextActivityStatus == "Active" and nextTotalPoints >= 50 and nextJuniorExamPassed and nextNcoExamPassed:
                promotion = f"Promoted to {nextRank}, Eligible for SGM"
        elif nextPromotionEligible:
            promotion = "Eligible for SGM"
        

        auditLog.append({
            "error": None,
            "userId": update.discordId,
            "promotion": promotion,
            "pointUpdate": HonorGuardMemberPointUpdate(
                row=member.row,
                robloxUsername=member.robloxUsername,
                previousQuotaPoints=member.quotaPoints,
                quotaPoints=member.quotaPoints + update.quotaDelta,
                previousEventPoints=member.eventPoints,
                eventPoints=member.eventPoints + update.eventDelta,
                previousPlatoonPoints=platoonMember.platoonPoints if platoonMember else 0,
                platoonPoints=(platoonMember.platoonPoints + update.platoonDelta) if platoonMember else 0,
                previousAwardedPoints=member.awardedPoints,
                awardedPoints=member.awardedPoints + update.awardedDelta,
                previousTotalPoints=member.totalPoints,
                totalPoints=nextTotalPoints,
                activityStatus=nextActivityStatus,
                passedJGE=update.juniorExamPassed,
                passedNCOE=update.ncoExamPassed,
            ),
        })

 #  for attendee in updatesByRowMember.values():
 #      member = attendee.member
 #      update = attendee.update
 #      attendeePlatoon = platoonByUsername.get(attendee.member.robloxUsername.casefold())
 #      if attendeePlatoon is not None:
 #          platoonMember = attendeePlatoon.platoon
 #      
 #      if member.rank == "Junior Guardsman" and member.:
 #          promotion = "to Guardsman"
 #      if member.rank == "Guardsman" and member.promotionEligible == True:
 #          promotion = "eligible to SGM"
 #      if member.rank =="Junior Guardsman"
 #      auditLog.append(
 #          {
 #              "error": None,
 #              "userId": update.discordId,
 #              "promotion": "True if member.rank or member.promotionEligible == True else False",
 #              "pointUpdate": HonorGuardMemberPointUpdate(
 #                  row=member.row,
 #                  robloxUsername=member.robloxUsername,
 #                  previousQuotaPoints=member.quotaPoints,
 #                  quotaPoints=update.quotaDelta + member.quotaPoints,
 #                  previousEventPoints=member.eventPoints,
 #                  eventPoints=update.eventDelta + member.eventPoints,
 #                  previousPlatoonPoints=platoonMember.platoonPoints if attendeePlatoon is not None else 0,
 #                  platoonPoints=platoonMember.platoonPoints + update.platoonDelta if attendeePlatoon is not None else 0,
 #                  previousAwardedPoints=member.awardedPoints,
 #                  awardedPoints=member.awardedPoints + update.awardedDelta,
 #                  previousTotalPoints=member.totalPoints,
 #                  totalPoints=member.totalPoints + update.platoonDelta + update.awardedDelta + update.eventDelta,
 #                  activityStatus=member.activityStatus,
 #                  passedJGE=update.juniorExamPassed,
 #                  passedNCOE=update.ncoExamPassed,
 #              )
 #          }
 #          
 #      )
        
    return auditLog
