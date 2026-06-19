from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

import discord

import config
from features.staff.sessions import bgScanPipeline
from features.staff.sessions.Roblox import robloxInventory, robloxProfiles, robloxUsers
from runtime import googleOAuth, orgProfiles, orbatAudit as orbatAuditRuntime, taskBudgeter

log = logging.getLogger(__name__)

inventoryLabelPrivate = "private"
inventoryLabelPublic = "public"
inventoryLabelUnknown = "unknown"
_spreadsheetMimeType = "application/vnd.google-apps.spreadsheet"
_janeIntelColumnIndex = 11
_janeManagedProtectionPrefix = "Jane-managed BGC:"
_janeIntelStatuses = {
    "Not scanned",
    "Report ready",
    "Needs review",
    "Private inventory",
    "Missing identity",
    "Scan failed",
}


@dataclass(slots=True)
class BgIntelSheetUpdateResult:
    updated: bool = False
    status: str = ""
    spreadsheet_id: str = ""
    spreadsheet_title: str = ""
    sheet_name: str = ""
    row_number: int = 0
    reason: str = ""


@dataclass(slots=True)
class BgSpreadsheetRow:
    discord_id: int
    roblox_user: str
    inventory: str
    no_rover: bool = False

    def sheet_values(self) -> list[str]:
        return [
            str(int(self.discord_id)),
            str(self.roblox_user or ""),
            _inventorySheetValue(self.inventory),
        ]


@dataclass(slots=True)
class BgSpreadsheetResult:
    spreadsheet_id: str = ""
    title: str = ""
    sheet_name: str = ""
    url: str = ""
    rows: list[BgSpreadsheetRow] = field(default_factory=list)
    expected_channel_ids: list[int] = field(default_factory=list)
    posted_channel_ids: list[int] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def private_count(self) -> int:
        return sum(1 for row in self.rows if row.inventory == inventoryLabelPrivate)

    @property
    def public_count(self) -> int:
        return sum(1 for row in self.rows if row.inventory == inventoryLabelPublic)

    @property
    def unknown_count(self) -> int:
        return sum(1 for row in self.rows if row.inventory == inventoryLabelUnknown)

    @property
    def no_rover_count(self) -> int:
        return sum(1 for row in self.rows if row.no_rover)


def _positiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _nonNegativeInt(value: object, default: int = -1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed >= 0 else int(default)


def _inventorySheetValue(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == inventoryLabelPrivate:
        return "Private"
    if normalized == inventoryLabelPublic:
        return "Public"
    if normalized == inventoryLabelUnknown:
        return "Unknown"
    return str(value or "").strip()


def _configValue(name: str, *, guildId: int = 0, default: object = None) -> object:
    return orgProfiles.getOrganizationValue(config, name, guildId=int(guildId or 0), default=default)


def _spreadsheetTemplateId(guildId: int = 0) -> str:
    return str(
        _configValue(
            "bgCheckSpreadsheetTemplateId",
            guildId=guildId,
            default=getattr(config, "bgCheckSpreadsheetTemplateId", ""),
        )
        or ""
    ).strip()


def _spreadsheetFolderId(guildId: int = 0) -> str:
    return str(
        _configValue(
            "bgCheckSpreadsheetFolderId",
            guildId=guildId,
            default=getattr(config, "bgCheckSpreadsheetFolderId", ""),
        )
        or ""
    ).strip()


def _spreadsheetSheetName(guildId: int = 0) -> str:
    return str(
        _configValue(
            "bgCheckSpreadsheetSheetName",
            guildId=guildId,
            default=getattr(config, "bgCheckSpreadsheetSheetName", "Sheet1"),
        )
        or "Sheet1"
    ).strip() or "Sheet1"


def _quotedSheetName(sheetName: str) -> str:
    safeName = str(sheetName or "Sheet1").replace("'", "''")
    return f"'{safeName}'"


def _sheetPropertiesFromMetadata(metadata: dict[str, Any], preferredSheetName: str) -> tuple[int, str, int]:
    sheet = _sheetFromMetadata(metadata, preferredSheetName)
    properties = dict((sheet or {}).get("properties") or {})
    grid = dict(properties.get("gridProperties") or {})
    return (
        _nonNegativeInt(properties.get("sheetId"), -1),
        str(properties.get("title") or preferredSheetName or "Sheet1"),
        _positiveInt(grid.get("rowCount"), 0),
    )


def _sheetFromMetadata(metadata: dict[str, Any], preferredSheetName: str) -> dict[str, Any]:
    sheets = list((metadata or {}).get("sheets") or [])
    preferredName = str(preferredSheetName or "").strip()
    firstSheet: dict[str, Any] | None = None

    for sheet in sheets:
        properties = dict((sheet or {}).get("properties") or {})
        if firstSheet is None:
            firstSheet = dict(sheet or {})
        if preferredName and str(properties.get("title") or "") == preferredName:
            return dict(sheet or {})

    if firstSheet is not None and preferredName.lower() in {"", "sheet1"}:
        return dict(firstSheet)

    if preferredName:
        raise RuntimeError(f"BGC spreadsheet tab was not found in the copied template: {preferredName}")
    raise RuntimeError("BGC spreadsheet template does not contain a writable sheet.")


def _oneOfListValidation(values: list[str]) -> dict[str, Any]:
    return {
        "condition": _oneOfListCondition(values),
        "strict": True,
        "showCustomUi": True,
    }


def _oneOfListCondition(values: list[str]) -> dict[str, Any]:
    return {
        "type": "ONE_OF_LIST",
        "values": [{"userEnteredValue": str(value)} for value in values],
    }


def _columnRange(
    *,
    sheetId: int,
    columnIndex: int,
    rowCount: int,
    includeHeader: bool = False,
) -> dict[str, int]:
    startRowIndex = 0 if includeHeader else 1
    return {
        "sheetId": int(sheetId),
        "startRowIndex": startRowIndex,
        "endRowIndex": max(startRowIndex + 1, int(rowCount or 0)),
        "startColumnIndex": int(columnIndex),
        "endColumnIndex": int(columnIndex) + 1,
    }


def _multiColumnRange(
    *,
    sheetId: int,
    startColumnIndex: int,
    endColumnIndex: int,
    rowCount: int,
) -> dict[str, int]:
    return {
        "sheetId": int(sheetId),
        "startRowIndex": 0,
        "endRowIndex": max(1, int(rowCount or 0)),
        "startColumnIndex": int(startColumnIndex),
        "endColumnIndex": int(endColumnIndex),
    }


def _formatRuleRequest(
    *,
    sheetId: int,
    columnIndex: int,
    rowCount: int,
    text: str,
    backgroundColor: dict[str, float],
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [
                    _columnRange(
                        sheetId=sheetId,
                        columnIndex=columnIndex,
                        rowCount=rowCount,
                    )
                ],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": str(text)}],
                    },
                    "format": {"backgroundColor": backgroundColor},
                },
            },
            "index": 0,
        }
    }


def _janeManagedProtectedRangeIds(sheet: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for protectedRange in list((sheet or {}).get("protectedRanges") or []):
        description = str(protectedRange.get("description") or "")
        if not description.startswith(_janeManagedProtectionPrefix):
            continue
        protectedRangeId = _positiveInt(protectedRange.get("protectedRangeId"))
        if protectedRangeId > 0:
            ids.append(protectedRangeId)
    return ids


def _firstTable(sheet: dict[str, Any]) -> dict[str, Any] | None:
    tables = [dict(table) for table in list((sheet or {}).get("tables") or []) if isinstance(table, dict)]
    return tables[0] if tables else None


def _updateTableRequest(
    *,
    table: dict[str, Any],
    sheetId: int,
    rowCount: int,
) -> dict[str, Any] | None:
    tableId = str((table or {}).get("tableId") or "").strip()
    if not tableId:
        return None
    rangeValue = dict((table or {}).get("range") or {})
    rangeValue.update(
        {
            "sheetId": int(rangeValue.get("sheetId", sheetId)),
            "startRowIndex": int(rangeValue.get("startRowIndex", 0) or 0),
            "endRowIndex": max(2, int(rowCount or rangeValue.get("endRowIndex") or 2)),
            "startColumnIndex": int(rangeValue.get("startColumnIndex", 0) or 0),
            "endColumnIndex": 12,
        }
    )
    return {
        "updateTable": {
            "table": {
                "tableId": tableId,
                "range": rangeValue,
            },
            "fields": "range",
        }
    }


def _conditionalFormatRuleIndexes(
    sheet: dict[str, Any],
    *,
    columnIndex: int,
    text: str,
) -> list[int]:
    matches: list[int] = []
    for index, rule in enumerate(list((sheet or {}).get("conditionalFormats") or [])):
        condition = dict(dict(rule.get("booleanRule") or {}).get("condition") or {})
        if str(condition.get("type") or "") != "TEXT_EQ":
            continue
        values = [str(value.get("userEnteredValue") or "") for value in list(condition.get("values") or [])]
        if str(text) not in values:
            continue
        for rangeValue in list(rule.get("ranges") or []):
            startColumn = _nonNegativeInt(rangeValue.get("startColumnIndex"), -1)
            endColumn = _nonNegativeInt(rangeValue.get("endColumnIndex"), -1)
            if startColumn == int(columnIndex) and endColumn == int(columnIndex) + 1:
                matches.append(index)
                break
    return matches


def _bgSpreadsheetSetupRequests(
    *,
    sheet: dict[str, Any],
    rowCount: int,
    columnCount: int = 12,
) -> list[dict[str, Any]]:
    properties = dict((sheet or {}).get("properties") or {})
    sheetId = _nonNegativeInt(properties.get("sheetId"), -1)
    if sheetId < 0:
        return []

    normalizedRows = max(2, int(rowCount or 0))
    normalizedColumns = max(12, int(columnCount or 0))
    requests: list[dict[str, Any]] = []

    existingConditionalFormatCount = len(list((sheet or {}).get("conditionalFormats") or []))

    for protectedRangeId in _janeManagedProtectedRangeIds(sheet):
        requests.append({"deleteProtectedRange": {"protectedRangeId": protectedRangeId}})

    for ruleIndex in sorted(
        _conditionalFormatRuleIndexes(sheet, columnIndex=8, text="No"),
        reverse=True,
    ):
        requests.append(
            {
                "deleteConditionalFormatRule": {
                    "sheetId": sheetId,
                    "index": ruleIndex,
                }
            }
        )

    if int(columnCount or 0) < 12:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": sheetId,
                    "dimension": "COLUMNS",
                    "length": 12 - int(columnCount or 0),
                }
            }
        )
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheetId,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": _janeIntelColumnIndex,
                    "endColumnIndex": _janeIntelColumnIndex + 1,
                },
                "cell": {"userEnteredValue": {"stringValue": "Jane Intel"}},
                "fields": "userEnteredValue",
            }
        }
    )

    validations = {
        1: ["Flagged", "Cleared", "Resolved"],
        2: ["Yes", "No"],
        6: ["Public", "Private", "Unknown", "~"],
        7: ["Flagged", "Cleared", "Resolved"],
        8: ["Yes", "No"],
        9: ["Accepted", "Denied"],
        _janeIntelColumnIndex: sorted(_janeIntelStatuses),
    }
    table = _firstTable(sheet)
    if table is not None:
        tableRequest = _updateTableRequest(
            table=table,
            sheetId=sheetId,
            rowCount=normalizedRows,
        )
        if tableRequest is not None:
            requests.append(tableRequest)
    else:
        for columnIndex, values in validations.items():
            requests.append(
                {
                    "setDataValidation": {
                        "range": _columnRange(
                            sheetId=sheetId,
                            columnIndex=columnIndex,
                            rowCount=normalizedRows,
                        ),
                        "rule": _oneOfListValidation(values),
                    }
                }
            )

    if table is None:
        requests.append(
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheetId,
                            "startRowIndex": 0,
                            "endRowIndex": normalizedRows,
                            "startColumnIndex": 0,
                            "endColumnIndex": normalizedColumns,
                        }
                    }
                }
            }
        )

    flagColor = {"red": 0.956, "green": 0.8, "blue": 0.8}
    warningColor = {"red": 1.0, "green": 0.898, "blue": 0.6}
    blockedColor = {"red": 0.918, "green": 0.851, "blue": 1.0}
    deniedColor = {"red": 0.918, "green": 0.6, "blue": 0.6}
    formatRequests: list[dict[str, Any]] = []
    for columnIndex in (1, 7):
        formatRequests.append(
            _formatRuleRequest(
                sheetId=sheetId,
                columnIndex=columnIndex,
                rowCount=normalizedRows,
                text="Flagged",
                backgroundColor=flagColor,
            )
        )
    for text in ("Private", "Unknown"):
        formatRequests.append(
            _formatRuleRequest(
                sheetId=sheetId,
                columnIndex=6,
                rowCount=normalizedRows,
                text=text,
                backgroundColor=warningColor,
            )
        )
    formatRequests.append(
        _formatRuleRequest(
            sheetId=sheetId,
            columnIndex=9,
            rowCount=normalizedRows,
            text="Denied",
            backgroundColor=deniedColor,
        )
    )
    for text in ("Needs review", "Private inventory", "Missing identity", "Scan failed"):
        formatRequests.append(
            _formatRuleRequest(
                sheetId=sheetId,
                columnIndex=_janeIntelColumnIndex,
                rowCount=normalizedRows,
                text=text,
                backgroundColor=blockedColor if text != "Scan failed" else deniedColor,
            )
        )
    if existingConditionalFormatCount <= 0:
        requests.extend(formatRequests)

    for description, startColumnIndex, endColumnIndex in (
        ("generated identity columns", 4, 7),
        ("Jane Intel link column", _janeIntelColumnIndex, _janeIntelColumnIndex + 1),
    ):
        requests.append(
            {
                "addProtectedRange": {
                    "protectedRange": {
                        "range": _multiColumnRange(
                            sheetId=sheetId,
                            startColumnIndex=startColumnIndex,
                            endColumnIndex=endColumnIndex,
                            rowCount=normalizedRows,
                        ),
                        "description": f"{_janeManagedProtectionPrefix} {description}",
                        "warningOnly": True,
                    }
                }
            }
        )
    return requests


def _deleteExtraRowsRequest(
    *,
    sheetId: int,
    existingRowCount: int,
    desiredRowCount: int,
) -> dict[str, Any] | None:
    normalizedSheetId = _nonNegativeInt(sheetId, -1)
    normalizedExisting = _positiveInt(existingRowCount, 0)
    normalizedDesired = max(1, _positiveInt(desiredRowCount, 1))
    if normalizedSheetId < 0 or normalizedExisting <= normalizedDesired:
        return None
    return {
        "deleteDimension": {
            "range": {
                "sheetId": normalizedSheetId,
                "dimension": "ROWS",
                "startIndex": normalizedDesired,
                "endIndex": normalizedExisting,
            }
        }
    }


def _uniqueUserIds(userIds: Iterable[object]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for rawUserId in userIds or []:
        userId = _positiveInt(rawUserId)
        if userId <= 0 or userId in seen:
            continue
        seen.add(userId)
        normalized.append(userId)
    return normalized


def roverLookupGuildIds(*, sourceGuildId: int = 0, guildId: int = 0) -> list[int]:
    rawGuildIds = [
        sourceGuildId,
        guildId,
        orgProfiles.getOrganizationValue(
            config,
            "primaryGuildId",
            guildId=sourceGuildId or guildId,
            default=getattr(config, "serverId", 0),
        ),
        orgProfiles.getOrganizationValue(
            config,
            "bgCheckSourceGuildId",
            guildId=sourceGuildId or guildId,
            default=getattr(config, "bgCheckSourceGuildId", 0),
        ),
        getattr(config, "recruitmentSourceGuildId", 0),
        getattr(config, "serverId", 0),
        *(getattr(config, "recruitmentRoverGuildIds", []) or []),
    ]
    out: list[int] = []
    seen: set[int] = set()
    for rawGuildId in rawGuildIds:
        parsed = _positiveInt(rawGuildId)
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _lookupGuildIds(*, sourceGuildId: int = 0, guildId: int = 0) -> list[int]:
    return roverLookupGuildIds(sourceGuildId=sourceGuildId, guildId=guildId)


def _spreadsheetTitle(titlePrefix: str) -> str:
    cleanPrefix = str(titlePrefix or "BGC Spreadsheet").strip() or "BGC Spreadsheet"
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"{cleanPrefix} {stamp}"


def _spreadsheetFailureMessage(exc: Exception) -> str:
    text = str(exc or "").strip()
    if isinstance(exc, FileNotFoundError) and "Google OAuth token file is missing" in text:
        return text
    if "Google OAuth token" in text:
        return text
    if "BGC spreadsheet template ID is not configured" in text:
        return text
    if text:
        return f"BGC spreadsheet creation failed: {text}"
    return "BGC spreadsheet creation failed."


def _sheetAuditDetails(result: BgSpreadsheetResult, *, extraDetails: str = "") -> str:
    parts = [
        f"Rows: {int(result.row_count)}",
        f"Inventory private: {int(result.private_count)}",
        f"Inventory public: {int(result.public_count)}",
        f"Inventory unknown: {int(result.unknown_count)}",
    ]
    if int(result.no_rover_count) > 0:
        parts.append(f"No RoVer match: {int(result.no_rover_count)}")
    if extraDetails:
        parts.append(str(extraDetails).strip())
    return " | ".join(part for part in parts if str(part).strip())


async def sendBgSpreadsheetChangeLog(
    botClient: discord.Client,
    *,
    result: BgSpreadsheetResult,
    change: str,
    authorizedBy: str,
    requestedBy: str = "",
    requestMessageUrl: str = "",
    details: str = "",
) -> None:
    if not result.spreadsheet_id:
        return
    await orbatAuditRuntime.sendOrbatChangeLog(
        botClient,
        title="Spreadsheet Change",
        change=change,
        authorizedBy=str(authorizedBy or "").strip() or "system",
        requestedBy=str(requestedBy or "").strip() or str(authorizedBy or "").strip() or "system",
        requestMessageUrl=str(requestMessageUrl or "").strip(),
        details=_sheetAuditDetails(result, extraDetails=details),
        spreadsheetId=result.spreadsheet_id,
        sheetName=result.sheet_name or None,
        label=result.title or "BGC Spreadsheet",
    )


async def _progressUpdate(
    progress: Any,
    *,
    stepIndex: int,
    detail: str,
    pendingCount: Optional[int] = None,
    finished: bool = False,
    failed: bool = False,
) -> None:
    if progress is None or not hasattr(progress, "update"):
        return
    await progress.update(
        stepIndex=stepIndex,
        detail=detail,
        pendingCount=pendingCount,
        finished=finished,
        failed=failed,
    )


async def _resolveRobloxUserText(lookup: robloxUsers.RoverLookupResult) -> str:
    username = str(lookup.robloxUsername or "").strip()
    if username:
        return username
    robloxId = _positiveInt(lookup.robloxId)
    if robloxId <= 0:
        return ""
    profile = await robloxProfiles.fetchRobloxUserProfile(robloxId)
    if not profile.error and profile.username:
        return str(profile.username).strip()
    return str(robloxId)


async def _inventoryLabel(robloxUserId: int) -> str:
    result = await robloxInventory.fetchRobloxInventory(int(robloxUserId), maxPages=1)
    if result.error:
        if bgScanPipeline.isPrivateInventoryStatus(int(result.status or 0), result.error):
            return inventoryLabelPrivate
        log.info(
            "BGC spreadsheet inventory probe for Roblox user %s returned unknown status %s: %s",
            robloxUserId,
            result.status,
            result.error,
        )
        return inventoryLabelUnknown
    return inventoryLabelPublic


async def _storedRobloxIdentity(discordUserId: int) -> Optional[Any]:
    try:
        return await robloxUsers.getStoredRobloxIdentity(int(discordUserId))
    except Exception:
        log.exception("Failed to load stored Roblox identity for Discord user %s.", discordUserId)
        return None


async def fetchRobloxUserWithFallbacks(discordUserId: int, guildIds: list[int]) -> robloxUsers.RoverLookupResult:
    fallback = None
    for guildId in guildIds or [0]:
        lookup = await robloxUsers.fetchRobloxUser(
            int(discordUserId),
            guildId=int(guildId) if int(guildId or 0) > 0 else None,
        )
        if lookup.robloxId or str(lookup.robloxUsername or "").strip():
            return lookup
        if fallback is None:
            fallback = lookup
    return fallback or await robloxUsers.fetchRobloxUser(int(discordUserId))


async def _fetchRobloxUserWithFallbacks(discordUserId: int, guildIds: list[int]) -> robloxUsers.RoverLookupResult:
    return await fetchRobloxUserWithFallbacks(discordUserId, guildIds)


async def _buildRowForUserId(userId: int, *, guildIds: list[int]) -> BgSpreadsheetRow:
    lookup = await _storedRobloxIdentity(int(userId))
    if lookup is None:
        lookup = await fetchRobloxUserWithFallbacks(int(userId), guildIds)
    robloxUserId = _positiveInt(lookup.robloxId)
    robloxUserText = await _resolveRobloxUserText(lookup)
    if robloxUserId <= 0:
        return BgSpreadsheetRow(
            discord_id=int(userId),
            roblox_user=robloxUserText,
            inventory=inventoryLabelUnknown,
            no_rover=True,
        )
    return BgSpreadsheetRow(
        discord_id=int(userId),
        roblox_user=robloxUserText,
        inventory=await _inventoryLabel(robloxUserId),
    )


async def _buildRowForAttendee(attendee: dict[str, Any], *, guildIds: list[int]) -> BgSpreadsheetRow:
    userId = int(attendee.get("userId") or 0)
    storedRobloxUserId = _positiveInt(attendee.get("robloxUserId"))
    storedRobloxUsername = str(attendee.get("robloxUsername") or "").strip()

    if not storedRobloxUserId or not storedRobloxUsername:
        lookup = await _storedRobloxIdentity(userId)
        if lookup is None:
            lookup = await fetchRobloxUserWithFallbacks(userId, guildIds)
        if not storedRobloxUserId:
            storedRobloxUserId = _positiveInt(lookup.robloxId)
        if not storedRobloxUsername:
            storedRobloxUsername = str(lookup.robloxUsername or "").strip()

    if not storedRobloxUsername and storedRobloxUserId > 0:
        profile = await robloxProfiles.fetchRobloxUserProfile(storedRobloxUserId)
        if not profile.error and profile.username:
            storedRobloxUsername = str(profile.username).strip()

    robloxUserText = storedRobloxUsername or (str(storedRobloxUserId) if storedRobloxUserId > 0 else "")
    if storedRobloxUserId <= 0:
        return BgSpreadsheetRow(
            discord_id=userId,
            roblox_user=robloxUserText,
            inventory=inventoryLabelUnknown,
            no_rover=True,
        )
    return BgSpreadsheetRow(
        discord_id=userId,
        roblox_user=robloxUserText,
        inventory=await _inventoryLabel(storedRobloxUserId),
    )


async def _buildRowsConcurrently(
    items: list[Any],
    *,
    progress: Any,
    rowBuilder: Any,
) -> list[BgSpreadsheetRow]:
    total = len(items)
    if total <= 0:
        return []
    concurrency = max(1, int(getattr(config, "bgSpreadsheetLookupConcurrency", 6) or 6))
    rowsByIndex: list[Optional[BgSpreadsheetRow]] = [None] * total
    queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
    for index, item in enumerate(items):
        queue.put_nowait((index, item))

    progressLock = asyncio.Lock()
    completed = 0

    async def _worker() -> None:
        nonlocal completed
        while True:
            try:
                index, item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                rowsByIndex[index] = await rowBuilder(item)
                async with progressLock:
                    completed += 1
                    if completed == 1 or completed % 5 == 0 or completed == total:
                        await _progressUpdate(
                            progress,
                            stepIndex=3,
                            detail=(
                                "Resolving Roblox accounts and inventory privacy...\n"
                                f"Checked: `{completed}/{total}`"
                            ),
                            pendingCount=total,
                        )
            finally:
                queue.task_done()

    tasks = [
        asyncio.create_task(_worker())
        for _ in range(min(concurrency, total))
    ]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        raise
    return [row for row in rowsByIndex if row is not None]


async def buildRowsForUserIds(
    userIds: Iterable[object],
    *,
    sourceGuild: Optional[discord.Guild] = None,
    progress: Any = None,
) -> list[BgSpreadsheetRow]:
    normalizedUserIds = _uniqueUserIds(userIds)
    sourceGuildId = _positiveInt(getattr(sourceGuild, "id", 0))
    guildIds = roverLookupGuildIds(sourceGuildId=sourceGuildId)
    return await _buildRowsConcurrently(
        normalizedUserIds,
        progress=progress,
        rowBuilder=lambda userId: _buildRowForUserId(int(userId), guildIds=guildIds),
    )


async def buildRowsForAttendees(
    attendees: Iterable[dict[str, Any]],
    *,
    sourceGuild: Optional[discord.Guild] = None,
    guildId: int = 0,
    progress: Any = None,
) -> list[BgSpreadsheetRow]:
    normalizedAttendees: list[dict[str, Any]] = []
    seen: set[int] = set()
    for attendee in attendees or []:
        userId = _positiveInt((attendee or {}).get("userId"))
        if userId <= 0 or userId in seen:
            continue
        seen.add(userId)
        normalizedAttendees.append(dict(attendee))
    sourceGuildId = _positiveInt(getattr(sourceGuild, "id", 0))
    guildIds = roverLookupGuildIds(sourceGuildId=sourceGuildId, guildId=guildId)
    return await _buildRowsConcurrently(
        normalizedAttendees,
        progress=progress,
        rowBuilder=lambda attendee: _buildRowForAttendee(attendee, guildIds=guildIds),
    )


async def createSpreadsheetForRows(
    rows: list[BgSpreadsheetRow],
    *,
    sourceGuild: Optional[discord.Guild] = None,
    titlePrefix: str = "BGC Spreadsheet",
    guildId: int = 0,
    progress: Any = None,
) -> BgSpreadsheetResult:
    if not rows:
        return BgSpreadsheetResult(skipped_reason="No users were provided for the BGC spreadsheet.")
    title = _spreadsheetTitle(titlePrefix)
    await _progressUpdate(
        progress,
        stepIndex=4,
        detail="Copying the BGC template, writing columns E, F, and G, and trimming empty rows...",
        pendingCount=len(rows),
    )
    try:
        spreadsheetId, copiedTitle, url, sheetName = await taskBudgeter.runSheetsThread(
            _copyTemplateAndWriteRows,
            title=title,
            rows=rows,
            guildId=int(guildId or getattr(sourceGuild, "id", 0) or 0),
        )
    except Exception as exc:
        message = _spreadsheetFailureMessage(exc)
        if isinstance(exc, FileNotFoundError) or "Google OAuth token" in str(exc):
            log.warning("BGC spreadsheet creation unavailable: %s", message)
        else:
            log.exception("BGC spreadsheet creation failed.")
        await _progressUpdate(
            progress,
            stepIndex=5,
            detail=message,
            pendingCount=len(rows),
            failed=True,
        )
        return BgSpreadsheetResult(rows=rows, skipped_reason=message)
    return BgSpreadsheetResult(
        spreadsheet_id=spreadsheetId,
        title=copiedTitle,
        sheet_name=sheetName,
        url=url,
        rows=rows,
    )


def _copyTemplateAndWriteRows(
    *,
    title: str,
    rows: list[BgSpreadsheetRow],
    guildId: int = 0,
) -> tuple[str, str, str, str]:
    templateId = _spreadsheetTemplateId(guildId)
    if not templateId:
        raise RuntimeError("BGC spreadsheet template ID is not configured.")

    folderId = _spreadsheetFolderId(guildId)
    sheetName = _spreadsheetSheetName(guildId)

    drive = googleOAuth.buildService("drive", "v3")
    sheets = googleOAuth.buildService("sheets", "v4")
    body: dict[str, Any] = {"name": title}
    if folderId:
        body["parents"] = [folderId]

    copied = (
        drive.files()
        .copy(
            fileId=templateId,
            body=body,
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        )
        .execute()
    )
    spreadsheetId = str(copied.get("id") or "").strip()
    if not spreadsheetId:
        raise RuntimeError("Google Drive did not return a spreadsheet ID for the BGC copy.")

    drive.permissions().create(
        fileId=spreadsheetId,
        body={
            "type": "anyone",
            "role": "writer",
            "allowFileDiscovery": False,
        },
        supportsAllDrives=True,
        fields="id",
    ).execute()

    sheetMetadata = (
        sheets.spreadsheets()
        .get(
            spreadsheetId=spreadsheetId,
            fields=(
                "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
                "tables,conditionalFormats,protectedRanges(protectedRangeId,description))"
            ),
        )
        .execute()
    )
    sheet = _sheetFromMetadata(sheetMetadata, sheetName)
    properties = dict(sheet.get("properties") or {})
    grid = dict(properties.get("gridProperties") or {})
    sheetId = _nonNegativeInt(properties.get("sheetId"), -1)
    resolvedSheetName = str(properties.get("title") or sheetName or "Sheet1")
    existingRowCount = _positiveInt(grid.get("rowCount"), 0)
    columnCount = _positiveInt(grid.get("columnCount"), 12)
    setupRowCount = max(2, len(rows) + 1)
    batchRequests: list[dict[str, Any]] = []
    if rows:
        endRow = len(rows) + 1
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheetId,
            range=f"{_quotedSheetName(resolvedSheetName)}!E2:G{endRow}",
            valueInputOption="USER_ENTERED",
            body={"values": [row.sheet_values() for row in rows]},
        ).execute()
        trimRequest = _deleteExtraRowsRequest(
            sheetId=sheetId,
            existingRowCount=existingRowCount,
            desiredRowCount=endRow,
        )
        if trimRequest is not None:
            batchRequests.append(trimRequest)
    batchRequests.extend(
        _bgSpreadsheetSetupRequests(
            sheet=sheet,
            rowCount=setupRowCount,
            columnCount=columnCount,
        )
    )
    if batchRequests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": batchRequests},
        ).execute()

    copiedTitle = str(copied.get("name") or title)
    url = str(copied.get("webViewLink") or "").strip()
    if not url:
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheetId}/edit"
    return spreadsheetId, copiedTitle, url, resolvedSheetName


async def createSpreadsheetForUserIds(
    userIds: Iterable[object],
    *,
    sourceGuild: Optional[discord.Guild] = None,
    titlePrefix: str = "BGC Spreadsheet",
    guildId: int = 0,
    progress: Any = None,
) -> BgSpreadsheetResult:
    normalizedUserIds = _uniqueUserIds(userIds)
    if not normalizedUserIds:
        return BgSpreadsheetResult(skipped_reason="No users were provided for the BGC spreadsheet.")

    rows = await buildRowsForUserIds(
        normalizedUserIds,
        sourceGuild=sourceGuild,
        progress=progress,
    )
    return await createSpreadsheetForRows(
        rows,
        sourceGuild=sourceGuild,
        titlePrefix=titlePrefix,
        guildId=guildId,
        progress=progress,
    )


async def createSpreadsheetForAttendees(
    attendees: Iterable[dict[str, Any]],
    *,
    sourceGuild: Optional[discord.Guild] = None,
    titlePrefix: str = "BGC Spreadsheet",
    guildId: int = 0,
    progress: Any = None,
) -> BgSpreadsheetResult:
    rows = await buildRowsForAttendees(
        attendees,
        sourceGuild=sourceGuild,
        guildId=guildId,
        progress=progress,
    )
    if not rows:
        return BgSpreadsheetResult(skipped_reason="No users were provided for the BGC spreadsheet.")
    return await createSpreadsheetForRows(
        rows,
        sourceGuild=sourceGuild,
        titlePrefix=titlePrefix,
        guildId=guildId,
        progress=progress,
    )


def _safeSheetFormulaText(value: object) -> str:
    return str(value or "").replace('"', '""')


def _bgIntelSheetStatus(report: Any, riskScore: Any) -> str:
    outcome = str(getattr(riskScore, "outcome", "") or "").strip().lower()
    scored = bool(getattr(riskScore, "scored", True))
    try:
        scoreValue = int(getattr(riskScore, "score", 0) or 0)
    except (TypeError, ValueError):
        scoreValue = 0

    if not bool(getattr(report, "robloxUserId", None)) or outcome == "needs_identity":
        return "Missing identity"
    if scoreValue >= 40:
        return "Needs review"
    if str(getattr(report, "inventoryScanStatus", "") or "").strip().upper() == "PRIVATE":
        return "Private inventory"
    if not scored and outcome == "insufficient_data":
        return "Scan failed"
    return "Report ready"


def _bgIntelSheetCellValue(*, status: str, messageUrl: str) -> str:
    cleanStatus = str(status or "Report ready").strip()
    if cleanStatus not in _janeIntelStatuses:
        cleanStatus = "Report ready"
    cleanUrl = str(messageUrl or "").strip()
    if not cleanUrl:
        return cleanStatus
    return f'=HYPERLINK("{_safeSheetFormulaText(cleanUrl)}","{_safeSheetFormulaText(cleanStatus)}")'


def _normalizeRobloxUsername(value: object) -> str:
    return str(value or "").strip().lower()


def _matchingBgIntelRow(
    rows: list[list[Any]],
    *,
    discordUserId: int,
    robloxUsername: str,
) -> int:
    normalizedRobloxUsername = _normalizeRobloxUsername(robloxUsername)
    for index, row in enumerate(rows, start=2):
        discordCell = str(row[0] if len(row) > 0 else "").strip()
        robloxCell = _normalizeRobloxUsername(row[1] if len(row) > 1 else "")
        if discordUserId > 0 and discordCell == str(discordUserId):
            return index
        if normalizedRobloxUsername and robloxCell == normalizedRobloxUsername:
            return index
    return 0


def _lastNonemptySheetRow(
    sheets: Any,
    *,
    spreadsheetId: str,
    sheetName: str,
    endColumn: str = "L",
) -> int:
    try:
        result = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheetId,
                range=f"{_quotedSheetName(sheetName)}!A1:{endColumn}",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute()
        )
    except Exception:
        log.debug("Failed to read BGC sheet values while sizing setup range.", exc_info=True)
        return 0
    lastRow = 0
    for index, row in enumerate(list(result.get("values") or []), start=1):
        if any(str(cell or "").strip() for cell in list(row or [])):
            lastRow = index
    return lastRow


def _applyBgSpreadsheetSetup(
    *,
    spreadsheetId: str,
    guildId: int = 0,
    minimumRowCount: int = 0,
    compactBlankTail: bool = False,
) -> dict[str, Any]:
    spreadsheetId = str(spreadsheetId or "").strip()
    if not spreadsheetId:
        return {"ok": False, "reason": "Missing spreadsheet ID."}
    sheetName = _spreadsheetSheetName(guildId)
    sheets = googleOAuth.buildService("sheets", "v4")
    metadata = (
        sheets.spreadsheets()
        .get(
            spreadsheetId=spreadsheetId,
            fields=(
                "properties(title),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
                "tables,conditionalFormats,protectedRanges(protectedRangeId,description))"
            ),
        )
        .execute()
    )
    sheet = _sheetFromMetadata(metadata, sheetName)
    properties = dict(sheet.get("properties") or {})
    grid = dict(properties.get("gridProperties") or {})
    gridRowCount = _positiveInt(grid.get("rowCount"), 1000)
    columnCount = _positiveInt(grid.get("columnCount"), 12)
    table = _firstTable(sheet)
    tableRange = dict((table or {}).get("range") or {})
    tableEndRow = _positiveInt(tableRange.get("endRowIndex"), 0)
    rowCount = tableEndRow if tableEndRow > 0 else gridRowCount
    requestedMinimumRows = _positiveInt(minimumRowCount, 0)
    if requestedMinimumRows > 0:
        rowCount = max(rowCount, requestedMinimumRows)
    if compactBlankTail and table is not None:
        lastNonemptyRow = _lastNonemptySheetRow(
            sheets,
            spreadsheetId=spreadsheetId,
            sheetName=str(properties.get("title") or sheetName),
        )
        if lastNonemptyRow > 0:
            rowCount = max(2, requestedMinimumRows, lastNonemptyRow)
    requests = _bgSpreadsheetSetupRequests(
        sheet=sheet,
        rowCount=rowCount,
        columnCount=columnCount,
    )
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": requests},
        ).execute()
    return {
        "ok": True,
        "title": str((metadata.get("properties") or {}).get("title") or ""),
        "sheetName": str(properties.get("title") or sheetName),
        "rowCount": rowCount,
        "columnCount": max(12, columnCount),
    }


async def applyBgSpreadsheetSetupToTemplate(*, guildId: int = 0) -> dict[str, Any]:
    return await taskBudgeter.runSheetsThread(
        _applyBgSpreadsheetSetup,
        spreadsheetId=_spreadsheetTemplateId(guildId),
        guildId=int(guildId or 0),
        compactBlankTail=True,
    )


def _recentBgSpreadsheetFiles(*, drive: Any, folderId: str, limit: int) -> list[dict[str, Any]]:
    folderId = str(folderId or "").strip()
    if not folderId:
        return []
    normalizedLimit = max(1, min(int(limit or 12), 50))
    response = (
        drive.files()
        .list(
            q=f"'{folderId}' in parents and mimeType='{_spreadsheetMimeType}' and trashed=false",
            spaces="drive",
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=normalizedLimit,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return [dict(row) for row in list(response.get("files") or [])]


def _updateLatestBgIntelSheetLink(
    *,
    report: Any,
    riskScore: Any,
    reportId: int = 0,
    messageUrl: str = "",
    guildId: int = 0,
) -> BgIntelSheetUpdateResult:
    status = _bgIntelSheetStatus(report, riskScore)
    discordUserId = _positiveInt(getattr(report, "discordUserId", 0))
    robloxUsername = str(getattr(report, "robloxUsername", "") or "").strip()
    if discordUserId <= 0 and not robloxUsername:
        return BgIntelSheetUpdateResult(status=status, reason="No Discord ID or Roblox username to match.")

    folderId = _spreadsheetFolderId(guildId)
    if not folderId:
        return BgIntelSheetUpdateResult(status=status, reason="BGC spreadsheet folder is not configured.")
    sheetName = _spreadsheetSheetName(guildId)
    lookupLimit = max(1, int(getattr(config, "bgIntelligenceSheetLookupLimit", 12) or 12))
    drive = googleOAuth.buildService("drive", "v3")
    sheets = googleOAuth.buildService("sheets", "v4")
    files = _recentBgSpreadsheetFiles(drive=drive, folderId=folderId, limit=lookupLimit)
    if not files:
        return BgIntelSheetUpdateResult(status=status, reason="No recent BGC spreadsheets found.")

    for fileRow in files:
        spreadsheetId = str(fileRow.get("id") or "").strip()
        title = str(fileRow.get("name") or "").strip()
        if not spreadsheetId:
            continue
        try:
            valueResult = (
                sheets.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheetId,
                    range=f"{_quotedSheetName(sheetName)}!E2:F",
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
        except Exception:
            log.debug("Failed to read BGC spreadsheet %s while updating Jane Intel.", title or spreadsheetId, exc_info=True)
            continue
        rowNumber = _matchingBgIntelRow(
            list(valueResult.get("values") or []),
            discordUserId=discordUserId,
            robloxUsername=robloxUsername,
        )
        if rowNumber <= 0:
            continue

        try:
            _applyBgSpreadsheetSetup(
                spreadsheetId=spreadsheetId,
                guildId=guildId,
                minimumRowCount=rowNumber,
            )
        except Exception:
            log.debug("Failed to apply BGC spreadsheet setup before Jane Intel update.", exc_info=True)

        cellValue = _bgIntelSheetCellValue(status=status, messageUrl=messageUrl)
        if reportId > 0 and not messageUrl:
            cellValue = f"{status} (report #{int(reportId)})"
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheetId,
            range=f"{_quotedSheetName(sheetName)}!L{rowNumber}",
            valueInputOption="USER_ENTERED",
            body={"values": [[cellValue]]},
        ).execute()
        return BgIntelSheetUpdateResult(
            updated=True,
            status=status,
            spreadsheet_id=spreadsheetId,
            spreadsheet_title=title,
            sheet_name=sheetName,
            row_number=rowNumber,
        )

    return BgIntelSheetUpdateResult(status=status, reason="No matching row found in recent BGC spreadsheets.")


async def updateLatestBgIntelSheetLink(
    *,
    report: Any,
    riskScore: Any,
    reportId: int = 0,
    messageUrl: str = "",
    guildId: int = 0,
) -> BgIntelSheetUpdateResult:
    return await taskBudgeter.runSheetsThread(
        _updateLatestBgIntelSheetLink,
        report=report,
        riskScore=riskScore,
        reportId=int(reportId or 0),
        messageUrl=str(messageUrl or "").strip(),
        guildId=int(guildId or 0),
    )
