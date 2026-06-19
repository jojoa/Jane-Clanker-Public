from __future__ import annotations

import asyncio
import base64
import json
from binascii import Error as BinasciiError
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from features.staff.sessions.Roblox import robloxPayloads, robloxTransport
from features.staff.sessions.Roblox.robloxModels import RobloxBadgeAwardsResult, RobloxUniverseBadgesResult, RobloxUserBadgesResult

_cacheGet = robloxTransport.cacheGet
_cacheSet = robloxTransport.cacheSet
_requestJson = robloxTransport.requestJson
_extractBadgeId = robloxPayloads.extractBadgeId
_DOTNET_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def _badgeAwardEndpointError(status: int, data: object) -> str:
    message = ""
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = str(first.get("message") or "").strip()
    if int(status or 0) == 403:
        suffix = f": {message}" if message else ""
        return f"Badge award-date lookup is unavailable from Roblox (403){suffix}."
    suffix = f": {message}" if message else ""
    return f"Badge lookup failed ({status}){suffix}."


def _extractAwardedDate(row: dict) -> Optional[str]:
    for key in ("awardedDate", "awarded_date"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _dotnetTicksToIso(value: object) -> Optional[str]:
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return None
    if ticks <= 0:
        return None
    try:
        parsed = _DOTNET_EPOCH + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _decodeBadgeCursorBoundaryAward(cursor: object) -> tuple[Optional[int], Optional[str]]:
    if not isinstance(cursor, str) or not cursor.strip():
        return None, None
    token = cursor.strip()
    padding = "=" * (-len(token) % 4)
    decodedValues: list[str] = []
    try:
        decodedValues.append(
            base64.b64decode((token + padding).encode("ascii"), validate=True).decode(
                "utf-8",
                errors="ignore",
            )
        )
    except (BinasciiError, UnicodeDecodeError, ValueError):
        pass
    try:
        decodedValues.append(
            base64.urlsafe_b64decode((token + padding).encode("ascii")).decode(
                "utf-8",
                errors="ignore",
            )
        )
    except (BinasciiError, UnicodeDecodeError, ValueError):
        pass
    for decoded in decodedValues:
        firstLine = decoded.splitlines()[0].strip()
        if not firstLine:
            continue
        try:
            payload = json.loads(firstLine)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("key") or "").strip()
        badgeText, separator, ticksText = key.partition(":")
        if separator != ":":
            continue
        try:
            badgeId = int(badgeText)
        except (TypeError, ValueError):
            continue
        awardedDate = _dotnetTicksToIso(ticksText)
        if badgeId > 0 and awardedDate:
            return badgeId, awardedDate
    return None, None


def _annotateCursorBoundaryBadge(
    badges: list[dict],
    cursor: object,
    *,
    source: str,
) -> None:
    badgeId, awardedDate = _decodeBadgeCursorBoundaryAward(cursor)
    if badgeId is None or awardedDate is None:
        return
    for badge in badges:
        if not isinstance(badge, dict):
            continue
        if _extractBadgeId(badge) != badgeId:
            continue
        if not badge.get("awardedDate"):
            badge["awardedDate"] = awardedDate
            badge["awardedDateSource"] = source
        break


def _badgeHistoryHardMaxPages() -> int:
    try:
        configured = int(getattr(config, "bgIntelligenceBadgeHistoryHardMaxPages", 100) or 100)
    except (TypeError, ValueError):
        configured = 100
    return max(1, min(configured, 500))


def _badgeAwardLookupConcurrency() -> int:
    try:
        configured = int(getattr(config, "robloxBadgeAwardLookupConcurrency", 1) or 1)
    except (TypeError, ValueError):
        configured = 1
    return max(1, min(configured, 4))


def _badgeAwardLookupDelaySec() -> float:
    try:
        configured = float(getattr(config, "robloxBadgeAwardLookupDelaySec", 0.5) or 0.0)
    except (TypeError, ValueError):
        configured = 0.5
    return max(0.0, min(configured, 5.0))


def _badgeInventoryApiKey() -> str:
    for attribute in ("robloxInventoryApiKey", "robloxOpenCloudApiKey"):
        value = str(getattr(config, attribute, "") or "").strip()
        if value:
            return value
    return ""


def _badgeDetailSampleLimit() -> int:
    try:
        configured = int(getattr(config, "bgIntelligenceBadgeHistoryDetailSampleLimit", 25) or 25)
    except (TypeError, ValueError):
        configured = 25
    return max(0, min(configured, 50))


def _badgeDetailConcurrency() -> int:
    try:
        configured = int(getattr(config, "robloxBadgeDetailLookupConcurrency", 4) or 4)
    except (TypeError, ValueError):
        configured = 4
    return max(1, min(configured, 8))


def _openCloudBadgeInventoryRow(row: dict) -> Optional[dict]:
    badgeIdValue = None
    badgeDetails = row.get("badgeDetails")
    if isinstance(badgeDetails, dict):
        badgeIdValue = badgeDetails.get("badgeId")
    if badgeIdValue is None:
        badgeIdValue = row.get("badgeId") or row.get("badge_id") or row.get("id")
    try:
        badgeId = int(badgeIdValue) if badgeIdValue is not None else 0
    except (TypeError, ValueError):
        badgeId = 0
    if badgeId <= 0:
        return None

    badgeRow = {
        "id": badgeId,
        "badgeId": badgeId,
    }
    addTime = row.get("addTime") or row.get("addedTime")
    if isinstance(addTime, str) and addTime.strip():
        badgeRow["awardedDate"] = addTime.strip()
        badgeRow["awardedDateSource"] = "open_cloud_inventory"
    return badgeRow


async def _fetchRobloxBadgeDetail(badgeId: int) -> Optional[dict]:
    try:
        normalizedBadgeId = int(badgeId)
    except (TypeError, ValueError):
        return None
    if normalizedBadgeId <= 0:
        return None

    cached = _cacheGet(
        "badge_details",
        normalizedBadgeId,
        ttlName="robloxBadgeDetailCacheTtlSec",
        defaultTtlSec=86400,
    )
    if isinstance(cached, dict):
        return dict(cached)

    url = f"https://badges.roblox.com/v1/badges/{normalizedBadgeId}"
    status, data = await _requestJson("GET", url, timeoutSec=10)
    if status != 200 or not isinstance(data, dict):
        return None

    stats = data.get("statistics") if isinstance(data.get("statistics"), dict) else {}
    detail = {
        "name": data.get("displayName") or data.get("name"),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "awardedCount": stats.get("awardedCount"),
    }
    _cacheSet(
        "badge_details",
        normalizedBadgeId,
        dict(detail),
        ttlName="robloxBadgeDetailCacheTtlSec",
        defaultTtlSec=86400,
    )
    return detail


async def _populateBadgeHistoryDetails(badges: list[dict]) -> None:
    sampleLimit = _badgeDetailSampleLimit()
    if sampleLimit <= 0:
        return

    targets: list[tuple[dict, int]] = []
    seenIds: set[int] = set()
    for badge in badges:
        if len(targets) >= sampleLimit:
            break
        if not isinstance(badge, dict):
            continue
        badgeId = _extractBadgeId(badge)
        if badgeId is None or badgeId in seenIds:
            continue
        if badge.get("name") and badge.get("created") and badge.get("updated"):
            continue
        seenIds.add(badgeId)
        targets.append((badge, badgeId))
    if not targets:
        return

    semaphore = asyncio.Semaphore(_badgeDetailConcurrency())

    async def _loadDetail(badgeId: int) -> Optional[dict]:
        async with semaphore:
            return await _fetchRobloxBadgeDetail(badgeId)

    details = await asyncio.gather(
        *[_loadDetail(badgeId) for _, badgeId in targets],
        return_exceptions=True,
    )
    for (badge, _), detail in zip(targets, details):
        if isinstance(detail, Exception) or not isinstance(detail, dict):
            continue
        for key in ("name", "created", "updated", "awardedCount"):
            value = detail.get(key)
            if value is not None and not badge.get(key):
                badge[key] = value


async def _fetchRobloxUserBadgesOpenCloud(
    normalizedUserId: int,
    *,
    pageLimit: int,
    pageCountLimit: int,
) -> RobloxUserBadgesResult:
    apiKey = _badgeInventoryApiKey()
    if not apiKey:
        return RobloxUserBadgesResult([], 0, error="Missing Roblox Open Cloud API key for badge history.")

    cacheKey = ("v3-opencloud", normalizedUserId, pageLimit, pageCountLimit)
    cached = _cacheGet(
        "user_badges",
        cacheKey,
        ttlName="robloxBadgeHistoryCacheTtlSec",
        defaultTtlSec=86400,
    )
    if isinstance(cached, RobloxUserBadgesResult):
        return cached

    url = f"https://apis.roblox.com/cloud/v2/users/{normalizedUserId}/inventory-items"
    headers = {"x-api-key": apiKey}
    params = {
        "maxPageSize": str(pageLimit),
        "filter": "badges=true;gamePasses=false",
    }
    cursor: Optional[str] = None
    badges: list[dict] = []
    status = 200

    try:
        for _ in range(pageCountLimit):
            if cursor:
                params["pageToken"] = cursor
            else:
                params.pop("pageToken", None)
            status, data = await _requestJson("GET", url, headers=headers, params=params, timeoutSec=10)
            if status != 200 or not isinstance(data, dict):
                detail = ""
                if isinstance(data, dict):
                    detail = str(data.get("message") or data.get("error") or "").strip()
                    if not detail and isinstance(data.get("errors"), list) and data["errors"]:
                        first = data["errors"][0]
                        if isinstance(first, dict):
                            detail = str(first.get("message") or first.get("error") or "").strip()
                        elif isinstance(first, str):
                            detail = first.strip()
                suffix = f": {detail}" if detail else ""
                return RobloxUserBadgesResult(
                    badges,
                    status,
                    nextCursor=cursor,
                    error=f"Badge history lookup failed ({status}){suffix}.",
                )
            rawItems = data.get("inventoryItems") or data.get("items")
            if not isinstance(rawItems, list):
                return RobloxUserBadgesResult(
                    badges,
                    status,
                    nextCursor=cursor,
                    error="Badge history lookup returned invalid data.",
                )
            for rawItem in rawItems:
                if not isinstance(rawItem, dict):
                    continue
                badgeRow = _openCloudBadgeInventoryRow(rawItem)
                if badgeRow is not None:
                    badges.append(badgeRow)
            cursor = data.get("nextPageToken")
            if not cursor:
                break
    except Exception as exc:
        return RobloxUserBadgesResult(badges, 0, nextCursor=cursor, error=str(exc))

    badges.sort(key=lambda badge: str(badge.get("awardedDate") or ""), reverse=True)
    await _populateBadgeHistoryDetails(badges)
    result = RobloxUserBadgesResult(badges, status, nextCursor=cursor)
    _cacheSet(
        "user_badges",
        cacheKey,
        result,
        ttlName="robloxBadgeHistoryCacheTtlSec",
        defaultTtlSec=86400,
    )
    return result


_PUBLIC_INVENTORY_ASSET_TYPE_IDS: tuple[int, ...] = (
    1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 17, 18, 19, 24,
    27, 28, 29, 30, 31, 32, 38, 40, 41, 42, 43, 44, 45,
    46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 61, 62,
    64, 65, 66, 67, 68, 69, 70, 71, 72, 76, 77,
)


async def fetchRobloxBadgeAwards(
    robloxUserId: int,
    badgeIds: set[int],
    batchSize: int = 50,
) -> RobloxBadgeAwardsResult:
    if not badgeIds:
        return RobloxBadgeAwardsResult([], 200)

    url = f"https://badges.roblox.com/v1/users/{robloxUserId}/badges/awarded-dates"
    badges: list[dict] = []
    ids = sorted({int(value) for value in list(badgeIds or set()) if int(value or 0) > 0})
    missingIds: list[int] = []
    for badgeId in ids:
        cached = _cacheGet(
            "badge_awards",
            (int(robloxUserId), int(badgeId)),
            ttlName="robloxBadgeAwardCacheTtlSec",
            defaultTtlSec=86400,
        )
        if isinstance(cached, dict):
            badges.append(dict(cached))
        else:
            missingIds.append(int(badgeId))
    if not missingIds:
        return RobloxBadgeAwardsResult(badges, 200)

    try:
        normalizedBatchSize = int(batchSize or 100)
    except (TypeError, ValueError):
        normalizedBatchSize = 100
    normalizedBatchSize = max(1, min(normalizedBatchSize, 100))
    semaphore = asyncio.Semaphore(_badgeAwardLookupConcurrency())

    async def _fetchChunk(chunk: list[int]) -> tuple[int, list[dict], Optional[str]]:
        params = {"badgeIds": ",".join(str(b) for b in chunk)}
        async with semaphore:
            status, data = await _requestJson("GET", url, params=params, timeoutSec=10)
            delaySec = _badgeAwardLookupDelaySec()
            if delaySec > 0:
                await asyncio.sleep(delaySec)
        if status != 200 or not isinstance(data, dict):
            return int(status or 0), [], _badgeAwardEndpointError(int(status or 0), data)
        rows = data.get("data")
        if not isinstance(rows, list):
            return int(status or 0), [], "Badge lookup returned invalid data."
        parsedRows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            badgeId = row.get("badgeId") or row.get("badge_id") or row.get("id")
            awardedDate = _extractAwardedDate(row)
            try:
                badgeId = int(badgeId) if badgeId is not None else None
            except (TypeError, ValueError):
                badgeId = None
            if not badgeId or not awardedDate:
                continue
            parsedRows.append(
                {
                    "badgeId": badgeId,
                    "awardedDate": awardedDate,
                    "awardedDateSource": "awarded_dates_endpoint",
                }
            )
        return int(status or 200), parsedRows, None

    try:
        results = await asyncio.gather(
            *[
                _fetchChunk(missingIds[start : start + normalizedBatchSize])
                for start in range(0, len(missingIds), normalizedBatchSize)
            ],
            return_exceptions=True,
        )
        errors: list[str] = []
        errorStatus = 0
        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
                continue
            status, rows, error = result
            if error:
                errorStatus = int(status or errorStatus or 0)
                errors.append(error)
                continue
            badges.extend(rows)
            for row in rows:
                badgeId = row.get("badgeId")
                if badgeId is None:
                    continue
                _cacheSet(
                    "badge_awards",
                    (int(robloxUserId), int(badgeId)),
                    dict(row),
                    ttlName="robloxBadgeAwardCacheTtlSec",
                    defaultTtlSec=86400,
                )
        if errors:
            return RobloxBadgeAwardsResult(
                badges,
                errorStatus,
                error=errors[0],
            )
    except Exception as exc:
        return RobloxBadgeAwardsResult(badges, 0, error=str(exc))

    return RobloxBadgeAwardsResult(badges, 200)


async def fetchRobloxUserBadges(
    robloxUserId: int,
    *,
    limit: int = 100,
    maxPages: int = 2,
) -> RobloxUserBadgesResult:
    try:
        normalizedUserId = int(robloxUserId)
    except (TypeError, ValueError):
        return RobloxUserBadgesResult([], 0, error="Badge history lookup failed (invalid Roblox user ID).")
    if normalizedUserId <= 0:
        return RobloxUserBadgesResult([], 0, error="Badge history lookup failed (invalid Roblox user ID).")

    pageLimit = max(10, min(int(limit or 100), 100))
    try:
        normalizedMaxPages = int(maxPages or 0)
    except (TypeError, ValueError):
        normalizedMaxPages = 2
    if normalizedMaxPages <= 0:
        pageCountLimit = _badgeHistoryHardMaxPages()
    else:
        pageCountLimit = max(1, min(normalizedMaxPages, _badgeHistoryHardMaxPages()))

    if _badgeInventoryApiKey():
        openCloudResult = await _fetchRobloxUserBadgesOpenCloud(
            normalizedUserId,
            pageLimit=pageLimit,
            pageCountLimit=pageCountLimit,
        )
        if not openCloudResult.error or openCloudResult.badges:
            return openCloudResult

    cacheKey = ("v2", normalizedUserId, pageLimit, pageCountLimit)
    cached = _cacheGet(
        "user_badges",
        cacheKey,
        ttlName="robloxBadgeHistoryCacheTtlSec",
        defaultTtlSec=86400,
    )
    if isinstance(cached, RobloxUserBadgesResult):
        return cached
    url = f"https://badges.roblox.com/v1/users/{normalizedUserId}/badges"
    cursor: Optional[str] = None
    badges: list[dict] = []
    status = 200

    try:
        for _ in range(pageCountLimit):
            params = {"limit": str(pageLimit), "sortOrder": "Desc"}
            if cursor:
                params["cursor"] = cursor
            status, data = await _requestJson("GET", url, params=params, timeoutSec=10)
            if status != 200 or not isinstance(data, dict):
                return RobloxUserBadgesResult(
                    badges,
                    status,
                    nextCursor=cursor,
                    error=f"Badge history lookup failed ({status}).",
                )
            raw = data.get("data")
            if not isinstance(raw, list):
                return RobloxUserBadgesResult(
                    badges,
                    status,
                    nextCursor=cursor,
                    error="Badge history lookup returned invalid data.",
                )
            pageBadges: list[dict] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                badgeId = _extractBadgeId(entry)
                if badgeId is None:
                    continue
                name = entry.get("name")
                created = entry.get("created")
                updated = entry.get("updated")
                stats = entry.get("statistics") if isinstance(entry.get("statistics"), dict) else {}
                badgeRow = {
                    "id": badgeId,
                    "name": name if isinstance(name, str) else None,
                    "created": created if isinstance(created, str) else None,
                    "updated": updated if isinstance(updated, str) else None,
                    "awardedCount": stats.get("awardedCount"),
                }
                awardedDate = _extractAwardedDate(entry)
                if awardedDate:
                    badgeRow["awardedDate"] = awardedDate
                    badgeRow["awardedDateSource"] = "user_badges_endpoint"
                pageBadges.append(badgeRow)
            nextCursor = data.get("nextPageCursor")
            _annotateCursorBoundaryBadge(
                pageBadges,
                data.get("previousPageCursor"),
                source="badge_history_previous_cursor",
            )
            _annotateCursorBoundaryBadge(
                pageBadges,
                nextCursor,
                source="badge_history_next_cursor",
            )
            badges.extend(pageBadges)
            cursor = nextCursor
            if not cursor:
                break
    except Exception as exc:
        return RobloxUserBadgesResult(badges, 0, nextCursor=cursor, error=str(exc))

    result = RobloxUserBadgesResult(badges, status, nextCursor=cursor)
    _cacheSet(
        "user_badges",
        cacheKey,
        result,
        ttlName="robloxBadgeHistoryCacheTtlSec",
        defaultTtlSec=86400,
    )
    return result


async def fetchRobloxUniverseBadges(
    universeId: int,
    limit: int = 100,
    cursor: Optional[str] = None,
    sortOrder: str = "Asc",
) -> RobloxUniverseBadgesResult:
    url = f"https://badges.roblox.com/v1/universes/{universeId}/badges"
    params = {"limit": str(limit), "sortOrder": sortOrder}
    if cursor:
        params["cursor"] = cursor

    try:
        status, data = await _requestJson("GET", url, params=params, timeoutSec=10)
    except Exception as exc:
        return RobloxUniverseBadgesResult([], 0, error=str(exc))

    if status != 200 or not isinstance(data, dict):
        return RobloxUniverseBadgesResult([], status, error=f"Badge list failed ({status}).")

    raw = data.get("data")
    if not isinstance(raw, list):
        return RobloxUniverseBadgesResult([], status, error="Badge list returned invalid data.")

    badges: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        badgeId = _extractBadgeId(entry)
        if badgeId is None:
            continue
        name = entry.get("name")
        stats = entry.get("statistics") if isinstance(entry.get("statistics"), dict) else {}
        awardedCount = stats.get("awardedCount") or stats.get("awardedCountFormatted")
        badges.append(
            {
                "id": badgeId,
                "name": name,
                "awardedCount": awardedCount,
            }
        )

    return RobloxUniverseBadgesResult(
        badges,
        status,
        nextCursor=data.get("nextPageCursor"),
    )
